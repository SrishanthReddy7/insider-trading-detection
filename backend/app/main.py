from __future__ import annotations

import json
import os
import re
import html
import csv
import logging
from collections import defaultdict
from io import BytesIO
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session

from app.db.models import Alert, AlertType, Document, DocumentAccessLog, DocumentSource, Employee, Trade
from app.db.session import engine, get_db
from app.schemas import (
    AccessLogIn,
    AlertOut,
    CorrelationGraph,
    CorrelationEdge,
    AutoDetectedTradeOut,
    EmployeeInvestigationOut,
    InvestigationTradeOut,
    DocumentDetail,
    DocumentOut,
    EmployeeIn,
    EmployeeOut,
    InsiderAccessLogIn,
    SeedResponse,
    TradeIn,
    TradeOut,
)
from app.services.correlation import correlate
from app.services.correlation import correlate_trade_with_access, detect_suspicious_trades_from_access
from app.services.document import analyze_document_text, extract_company_or_ticker, extract_pdf_text, normalize_company_to_ticker
from app.services.mnpi import analyze_text, dumps_json
from app.services.trade import fetch_employee_trades
from app.services.trading import score_trade
from app.settings import get_settings

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

settings = get_settings()
logger = logging.getLogger("mnpi_guard")

app = FastAPI(title="MNPI Guard API", version="0.1.0")

cors_value = (settings.cors_origins or "").strip()
origins = [o.strip() for o in cors_value.split(",") if o.strip() and o.strip() != "*"]
allow_all = cors_value == "*" or cors_value == ""
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if allow_all else origins,
    # Browsers disallow credentials with wildcard origins; keep dev simple.
    allow_credentials=False if allow_all else True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/")
def root() -> dict:
    return {
        "ok": True,
        "service": "MNPI Guard API",
        "health": "/health",
        "docs": "/docs",
    }

@app.get("/api/debug/cors")
def debug_cors() -> dict:
    return {
        "cors_origins_setting": settings.cors_origins,
        "computed_allow_all": allow_all,
        "computed_origins": origins,
    }

@app.post("/api/reset", response_model=dict)
def reset_data(db: Session = Depends(get_db)) -> dict:
    """
    Clears all demo/runtime data for a fresh run:
    - documents, trades, alerts, access logs
    - deletes files in STORAGE_DIR that match our naming (seed_*, *_<original filename>)
    """
    # Delete DB rows (order matters due to FKs)
    db.execute(delete(DocumentAccessLog))
    db.execute(delete(Alert))
    db.execute(delete(Trade))
    db.execute(delete(Document))
    db.commit()

    # Best-effort delete stored uploads created by this MVP
    storage = Path(settings.storage_dir)
    removed = 0
    if storage.exists():
        for p in storage.iterdir():
            if p.is_file() and (p.name.startswith("seed_") or p.name[:10].isdigit()):
                try:
                    p.unlink()
                    removed += 1
                except Exception:
                    pass

    return {"ok": True, "removed_files": removed}


def _ensure_storage_dir() -> Path:
    p = Path(settings.storage_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _looks_like_readable_text(text: str) -> bool:
    if not text:
        return False
    sample = text[:4000]
    printable = sum(1 for ch in sample if ch.isprintable() or ch in "\n\r\t")
    letters = sum(1 for ch in sample if ch.isalpha())
    if len(sample) == 0:
        return False
    printable_ratio = printable / len(sample)
    letter_ratio = letters / len(sample)
    return printable_ratio >= 0.85 and letter_ratio >= 0.2


def _extract_pdf_text(raw: bytes) -> str:
    if PdfReader is None:
        raise ValueError("PDF support is not available. Install pypdf in backend environment.")

    # Try standard parse first, then relaxed mode for imperfect PDFs.
    for strict_mode in (True, False):
        try:
            reader = PdfReader(BytesIO(raw), strict=strict_mode)
            parts = []
            for page in reader.pages:
                parts.append(page.extract_text() or "")
            text = "\n".join(parts).strip()
            if text:
                return text
        except Exception:
            continue

    # Some users upload plain text renamed with .pdf extension.
    fallback = raw.decode("utf-8", errors="ignore").strip()
    if _looks_like_readable_text(fallback):
        return fallback

    raise ValueError("Could not extract text from this PDF. Use a text-based PDF or TXT file.")


def _extract_text_from_upload(filename: str, raw: bytes) -> str:
    lower_name = (filename or "").lower()

    if lower_name.endswith(".pdf"):
        return _extract_pdf_text(raw)

    # Plain-text fallback for txt/csv/md-like uploads.
    try:
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _auto_flag_trade_from_access(db: Session, tr: Trade) -> dict | None:
    """
    Auto-detect suspicious behavior:
    employee accessed a company-related document, then bought same symbol within 3 days.
    """
    if (tr.side or "").lower() != "buy":
        return None

    access_logs = db.execute(
        select(DocumentAccessLog, Document)
        .join(Document, Document.id == DocumentAccessLog.document_id)
        .where(
            and_(
                DocumentAccessLog.employee_id == tr.employee_id,
                DocumentAccessLog.accessed_at <= tr.traded_at,
                DocumentAccessLog.accessed_at >= tr.traded_at - timedelta(days=3),
            )
        )
        .order_by(DocumentAccessLog.accessed_at.desc())
        .limit(200)
    ).all()

    result = correlate_trade_with_access(
        employee_id=tr.employee_id,
        symbol=tr.symbol,
        trade_time=tr.traded_at,
        access_logs=access_logs,
    )
    if not result.is_suspicious:
        return None

    existing = db.execute(
        select(Alert).where(
            Alert.alert_type == AlertType.correlation,
            Alert.trade_id == tr.id,
            Alert.document_id == result.matched_document_id,
        )
    ).scalar_one_or_none()
    if existing:
        return {
            "employee_id": tr.employee_id,
            "company": result.company,
            "time_difference_hours": result.time_difference_hours,
            "risk_score": result.risk_score,
            "risk_level": result.risk_level,
            "reasons": result.reasons,
            "document_id": result.matched_document_id,
            "trade_id": tr.id,
        }

    tr.risk_score = max(int(tr.risk_score or 0), int(result.risk_score))
    db.add(tr)
    db.add(
        Alert(
            alert_type=AlertType.correlation,
            severity=result.risk_score,
            title=f"Potential insider trading: {tr.employee_id} bought {tr.symbol}",
            employee_id=tr.employee_id,
            document_id=result.matched_document_id,
            trade_id=tr.id,
            details=dumps_json(
                {
                    "company": result.company,
                    "time_difference_hours": result.time_difference_hours,
                    "access_time": result.matched_access_time.isoformat() if result.matched_access_time else None,
                    "trade_time": tr.traded_at.isoformat(),
                    "risk_level": result.risk_level,
                    "reasons": result.reasons,
                    "mode": "auto_from_trade",
                }
            ),
        )
    )
    db.commit()

    return {
        "employee_id": tr.employee_id,
        "company": result.company,
        "time_difference_hours": result.time_difference_hours,
        "risk_score": result.risk_score,
        "risk_level": result.risk_level,
        "reasons": result.reasons,
        "document_id": result.matched_document_id,
        "trade_id": tr.id,
    }


def _auto_flag_access_from_existing_trades(db: Session, log: DocumentAccessLog, doc: Document) -> list[dict]:
    """
    When access happens, scan existing employee trades and create alerts for suspicious matches.
    """
    employee_trades = fetch_employee_trades(db, log.employee_id)
    matches = detect_suspicious_trades_from_access(
        document=doc,
        access_log=log,
        employee_trades=employee_trades,
        window_days=3,
    )

    created: list[dict] = []
    for m in matches:
        existing = db.execute(
            select(Alert).where(
                Alert.alert_type == AlertType.correlation,
                Alert.trade_id == m.trade_id,
                Alert.document_id == doc.id,
            )
        ).scalar_one_or_none()
        if existing:
            continue
        db.add(
            Alert(
                alert_type=AlertType.correlation,
                severity=m.risk_score,
                title=f"Potential insider trading: {log.employee_id} {m.company}",
                employee_id=log.employee_id,
                document_id=doc.id,
                trade_id=m.trade_id,
                details=dumps_json(
                    {
                        "company": m.company,
                        "time_difference_hours": m.time_difference_hours,
                        "risk_level": m.risk_level,
                        "reasons": m.reasons,
                        "access_time": log.accessed_at.isoformat(),
                        "mode": "auto_from_access",
                    }
                ),
            )
        )
        created.append(
            {
                "employee_id": log.employee_id,
                "company": m.company,
                "time_difference_hours": m.time_difference_hours,
                "risk_score": m.risk_score,
                "risk_level": m.risk_level,
                "reason": m.reasons,
                "trade_id": m.trade_id,
                "document_id": doc.id,
            }
        )
    if created:
        db.commit()
    return created


def _ensure_demo_trade_dataset(db: Session) -> None:
    """
    Keep a stable, non-random trade dataset available for manual upload demos.
    Inserts only when there are no trades yet.
    """
    trade_count = db.execute(select(Trade.id).limit(1)).first()
    if trade_count:
        return

    now = datetime.utcnow()
    employees = [
        ("E101", "Alice Johnson"),
        ("E102", "Brian Lee"),
        ("E103", "Carla Diaz"),
    ]
    for emp_id, name in employees:
        if not db.get(Employee, emp_id):
            db.add(Employee(id=emp_id, name=name))
    db.commit()

    dataset = [
        ("E101", "AAPL", "buy", 250.0, 189.5, now - timedelta(hours=20)),
        ("E102", "TSLA", "buy", 100.0, 220.0, now - timedelta(days=2, hours=2)),
        ("E103", "GOOGL", "buy", 80.0, 159.4, now - timedelta(hours=22)),
        ("E103", "MSFT", "buy", 90.0, 412.0, now - timedelta(hours=10)),
        ("E101", "NVDA", "sell", 40.0, 860.0, now - timedelta(hours=8)),
    ]
    for employee_id, symbol, side, qty, price, traded_at in dataset:
        scores = score_trade(db, employee_id, symbol, qty, price, traded_at)
        db.add(
            Trade(
                employee_id=employee_id,
                symbol=symbol,
                side=side,
                quantity=qty,
                price=price,
                traded_at=traded_at,
                pnl_1d=scores.pnl_1d,
                anomaly_score=scores.anomaly_score,
                risk_score=scores.risk_score,
            )
        )
    db.commit()


def _ensure_storage_dir_backend() -> Path:
    p = Path(__file__).resolve().parents[1] / "storage"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _parse_iso_datetime(value: str) -> datetime:
    """Parse CSV traded_at; naive values are assumed UTC (same as server)."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty traded_at")
    cleaned = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except Exception:
        dt = None
        part = raw.split()[0] if raw else ""
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                dt = datetime.strptime(part, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            raise ValueError(f"Invalid traded_at timestamp: {value}")
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _build_auto_detected_trades_for_document(
    db: Session, doc: Document, access_log: DocumentAccessLog | None = None
) -> list[AutoDetectedTradeOut]:
    extracted_company = extract_company_or_ticker(doc.extracted_text or "")
    company = normalize_company_to_ticker(doc.company or extracted_company)
    if doc.company != company:
        doc.company = company
        db.add(doc)
        db.commit()
    if not company:
        logger.info("auto-detect no company: doc_id=%s extracted=%s normalized=%s", doc.id, extracted_company, company)
        return []

    trades = db.execute(
        select(Trade).where(Trade.symbol == company, Trade.side == "buy").order_by(Trade.traded_at.desc()).limit(300)
    ).scalars().all()
    all_symbols = db.execute(select(Trade.symbol).distinct().limit(200)).all()
    logger.info(
        "auto-detect debug: doc_id=%s extracted_company=%s normalized_company=%s available_trade_symbols=%s matched_count=%s",
        doc.id,
        extracted_company,
        company,
        [s for (s,) in all_symbols],
        len(trades),
    )
    if not trades:
        return []

    # Never use document created_at as a substitute for employee access time.
    anchor_access = access_log
    if anchor_access is None:
        anchor_access = (
            db.execute(
                select(DocumentAccessLog)
                .where(DocumentAccessLog.document_id == doc.id)
                .order_by(DocumentAccessLog.accessed_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
    if anchor_access is None:
        return []

    anchor_time = anchor_access.accessed_at

    out: list[AutoDetectedTradeOut] = []
    for tr in trades:
        dt_seconds = (tr.traded_at - anchor_time).total_seconds()
        dt_days = abs(dt_seconds) / 86400.0
        in_window = dt_days <= 3
        risk_level = "HIGH" if in_window else "LOW"
        reasons = ["Company match with document"]
        if in_window:
            reasons.append("Trade within time window")
        out.append(
            AutoDetectedTradeOut(
                employee_id=tr.employee_id,
                symbol=tr.symbol,
                quantity=tr.quantity,
                trade_time=tr.traded_at,
                document_id=doc.id,
                company=company,
                time_difference_days=round(dt_days, 3),
                risk_level=risk_level,
                reasons=reasons,
            )
        )

    out.sort(key=lambda x: (x.risk_level == "HIGH", -x.time_difference_days), reverse=True)
    return out


def _highlight_sensitive_words(text: str) -> str:
    escaped = html.escape(text or "")
    pattern = re.compile(r"\b(confidential|earnings|merger|acquisition)\b", re.IGNORECASE)
    return pattern.sub(r"<mark>\1</mark>", escaped)


def _render_pdf_like_html(doc: Document) -> str:
    highlighted = _highlight_sensitive_words(doc.extracted_text or "")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{html.escape(doc.filename or f"Document {doc.id}")}</title>
  <style>
    body {{
      margin: 0;
      padding: 24px;
      background: #e5e7eb;
      font-family: "Times New Roman", serif;
    }}
    .page {{
      width: min(900px, 100%);
      margin: 0 auto;
      background: #fff;
      border: 1px solid #d1d5db;
      box-shadow: 0 4px 14px rgba(0,0,0,0.12);
      padding: 48px 56px;
      line-height: 1.65;
      color: #111827;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .meta {{
      font-family: Arial, sans-serif;
      font-size: 12px;
      color: #6b7280;
      margin-bottom: 18px;
      border-bottom: 1px solid #e5e7eb;
      padding-bottom: 10px;
    }}
    mark {{
      background: #fef08a;
      padding: 0 2px;
      border-radius: 2px;
    }}
  </style>
</head>
<body>
  <div class="page">
    <div class="meta">
      <strong>{html.escape(doc.filename or "document")}</strong> | ID: {doc.id} | Company: {html.escape(doc.company or "-")}
    </div>
    {highlighted}
  </div>
</body>
</html>"""


@app.post("/api/insider/employees", response_model=EmployeeOut)
def create_employee(body: EmployeeIn, db: Session = Depends(get_db)) -> EmployeeOut:
    existing = db.get(Employee, body.id)
    if existing:
        existing.name = body.name
        db.commit()
        db.refresh(existing)
        return EmployeeOut(id=existing.id, name=existing.name)

    emp = Employee(id=body.id, name=body.name)
    db.add(emp)
    db.commit()
    return EmployeeOut(id=emp.id, name=emp.name)


@app.post("/api/documents/upload", response_model=DocumentDetail)
async def upload_document(file: UploadFile = File(...), db: Session = Depends(get_db)) -> DocumentDetail:
    _ensure_demo_trade_dataset(db)
    storage = _ensure_storage_dir()
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    is_pdf = (file.filename or "").lower().endswith(".pdf")
    try:
        if is_pdf:
            text = extract_pdf_text(raw)
        else:
            text = _extract_text_from_upload(file.filename or "", raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ts = int(datetime.utcnow().timestamp())
    safe_name = (file.filename or "upload").replace("\\", "_").replace("/", "_")
    stored_name = f"{ts}_{safe_name}"
    stored_path = storage / stored_name
    stored_path.write_bytes(raw)

    mnpi = analyze_text(text, restrict_threshold=settings.mnpi_restrict_threshold)
    doc_analysis = analyze_document_text(text)
    logger.info(
        "upload analysis: filename=%s extracted_company=%s normalized_company=%s",
        safe_name,
        extract_company_or_ticker(text),
        doc_analysis.company,
    )
    doc = Document(
        filename=safe_name,
        source=DocumentSource.upload,
        storage_path=str(stored_path),
        extracted_text=text,
        company=doc_analysis.company,
        risk_score=doc_analysis.risk_score,
        mnpi_score=mnpi.score,
        mnpi_labels=dumps_json(mnpi.labels),
        mnpi_entities=dumps_json(mnpi.entities),
        restricted=mnpi.restricted,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    if doc.mnpi_score >= settings.alert_threshold:
        alert = Alert(
            alert_type=AlertType.mnpi,
            severity=doc.mnpi_score,
            title=f"High MNPI score in document: {doc.filename}",
            document_id=doc.id,
            details=dumps_json({"snippets": mnpi.highlighted_snippets, "labels": mnpi.labels}),
        )
        db.add(alert)
        db.commit()

    return DocumentDetail(
        id=doc.id,
        filename=doc.filename,
        source=doc.source.value,
        company=doc.company or "",
        risk_score=doc.risk_score,
        mnpi_score=doc.mnpi_score,
        restricted=doc.restricted,
        created_at=doc.created_at,
        extracted_text=doc.extracted_text,
        mnpi_labels=json.loads(doc.mnpi_labels or "[]"),
        mnpi_entities=json.loads(doc.mnpi_entities or "[]"),
    )


@app.get("/api/auto-detected-trades/debug", response_model=dict)
def auto_detected_trades_debug(document_id: int | None = None, db: Session = Depends(get_db)) -> dict:
    doc: Document | None = None
    if document_id is not None:
        doc = db.get(Document, document_id)
    else:
        doc = db.execute(select(Document).order_by(Document.created_at.desc()).limit(1)).scalars().first()
    if not doc:
        return {"ok": True, "message": "No document found"}

    extracted = extract_company_or_ticker(doc.extracted_text or "")
    normalized = normalize_company_to_ticker(doc.company or extracted)
    symbols = [s for (s,) in db.execute(select(Trade.symbol).distinct().limit(300)).all()]
    matches = db.execute(select(Trade).where(Trade.symbol == normalized, Trade.side == "buy")).scalars().all()
    return {
        "ok": True,
        "document_id": doc.id,
        "extracted_company": extracted,
        "normalized_company": normalized,
        "available_trade_symbols": symbols,
        "matching_buy_trades": len(matches),
    }


@app.get("/api/documents", response_model=list[DocumentOut])
def list_documents(db: Session = Depends(get_db)) -> list[DocumentOut]:
    docs = db.execute(select(Document).order_by(Document.created_at.desc()).limit(200)).scalars().all()
    return [
        DocumentOut(
            id=d.id,
            filename=d.filename,
            source=d.source.value,
            company=d.company or "",
            risk_score=d.risk_score,
            mnpi_score=d.mnpi_score,
            restricted=d.restricted,
            created_at=d.created_at,
        )
        for d in docs
    ]


@app.get("/api/documents/{doc_id}", response_model=DocumentDetail)
def get_document(doc_id: int, db: Session = Depends(get_db)) -> DocumentDetail:
    d = db.get(Document, doc_id)
    if not d:
        raise HTTPException(status_code=404, detail="Not found")
    return DocumentDetail(
        id=d.id,
        filename=d.filename,
        source=d.source.value,
        company=d.company or "",
        risk_score=d.risk_score,
        mnpi_score=d.mnpi_score,
        restricted=d.restricted,
        created_at=d.created_at,
        extracted_text=d.extracted_text,
        mnpi_labels=json.loads(d.mnpi_labels or "[]"),
        mnpi_entities=json.loads(d.mnpi_entities or "[]"),
    )


@app.get("/api/documents/{doc_id}/content", response_class=PlainTextResponse)
def get_document_content(doc_id: int, db: Session = Depends(get_db)) -> str:
    d = db.get(Document, doc_id)
    if not d:
        raise HTTPException(status_code=404, detail="Not found")
    return d.extracted_text or ""


@app.get("/api/documents/{doc_id}/view", response_class=HTMLResponse)
def view_document_pdf_style(doc_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    d = db.get(Document, doc_id)
    if not d:
        raise HTTPException(status_code=404, detail="Not found")
    return HTMLResponse(_render_pdf_like_html(d))


@app.get("/api/documents/{doc_id}/download")
def download_document(doc_id: int, db: Session = Depends(get_db)):
    d = db.get(Document, doc_id)
    if not d:
        raise HTTPException(status_code=404, detail="Not found")

    path = Path(d.storage_path or "")
    if path.exists() and path.is_file():
        return FileResponse(path, filename=d.filename or path.name)

    # Fallback for seeded/manual records without a physical file.
    return PlainTextResponse(
        d.extracted_text or "",
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{(d.filename or "document")}.txt"'},
    )


@app.post("/api/documents/{doc_id}/access", response_model=dict)
def log_access(doc_id: int, body: AccessLogIn, db: Session = Depends(get_db)) -> dict:
    _ensure_demo_trade_dataset(db)
    d = db.get(Document, doc_id)
    if not d:
        raise HTTPException(status_code=404, detail="Not found")
    # Ensure access_time is always before the imported/known trade times for this employee.
    # This prevents "no results" when the CSV dataset is dated in the past and the user clicks View/Download later.
    earliest_trade_at = (
        db.execute(
            select(func.min(Trade.traded_at)).where(Trade.employee_id == body.employee_id)
        ).scalar()
        or None
    )
    accessed_at = datetime.utcnow()
    if earliest_trade_at is not None and earliest_trade_at <= accessed_at:
        accessed_at = earliest_trade_at - timedelta(seconds=1)

    log = DocumentAccessLog(
        document_id=doc_id,
        employee_id=body.employee_id,
        access_type=body.access_type,
        accessed_at=accessed_at,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    flagged = _auto_flag_access_from_existing_trades(db, log, d)
    detected = _build_auto_detected_trades_for_document(db, d, log)
    return {"ok": True, "company": d.company, "flagged_count": len(flagged), "flagged": flagged, "detected_trades": detected}


@app.post("/access", response_model=dict)
def access_alias(body: InsiderAccessLogIn, db: Session = Depends(get_db)) -> dict:
    _ensure_demo_trade_dataset(db)
    d = db.get(Document, body.document_id)
    if not d:
        raise HTTPException(status_code=404, detail="Document not found")
    log = DocumentAccessLog(document_id=body.document_id, employee_id=body.employee_id, access_type="view")
    db.add(log)
    db.commit()
    db.refresh(log)
    flagged = _auto_flag_access_from_existing_trades(db, log, d)
    detected = _build_auto_detected_trades_for_document(db, d, log)
    return {
        "ok": True,
        "employee_id": log.employee_id,
        "company": d.company,
        "access_time": log.accessed_at,
        "flagged_count": len(flagged),
        "flagged": flagged,
        "detected_trades": detected,
    }


@app.post("/api/insider/access", response_model=dict)
def log_insider_access(body: InsiderAccessLogIn, db: Session = Depends(get_db)) -> dict:
    _ensure_demo_trade_dataset(db)
    d = db.get(Document, body.document_id)
    if not d:
        raise HTTPException(status_code=404, detail="Document not found")
    log = DocumentAccessLog(document_id=body.document_id, employee_id=body.employee_id, access_type="view")
    db.add(log)
    db.commit()
    db.refresh(log)
    flagged = _auto_flag_access_from_existing_trades(db, log, d)
    detected = _build_auto_detected_trades_for_document(db, d, log)
    return {
        "ok": True,
        "employee_id": log.employee_id,
        "document_id": log.document_id,
        "access_time": log.accessed_at,
        "flagged_count": len(flagged),
        "flagged": flagged,
        "detected_trades": detected,
    }


@app.get("/auto-detected-trades", response_model=list[AutoDetectedTradeOut])
@app.get("/api/auto-detected-trades", response_model=list[AutoDetectedTradeOut])
def auto_detected_trades(document_id: int | None = None, db: Session = Depends(get_db)) -> list[AutoDetectedTradeOut]:
    _ensure_demo_trade_dataset(db)
    doc: Document | None = None
    if document_id is not None:
        doc = db.get(Document, document_id)
    else:
        doc = (
            db.execute(select(Document).order_by(Document.created_at.desc()).limit(1))
            .scalars()
            .first()
        )
    if doc is None:
        return []
    return _build_auto_detected_trades_for_document(db, doc)


@app.post("/api/trades", response_model=TradeOut)
def create_trade(body: TradeIn, db: Session = Depends(get_db)) -> TradeOut:
    scores = score_trade(
        db=db,
        employee_id=body.employee_id,
        symbol=body.symbol.upper(),
        quantity=body.quantity,
        price=body.price,
        traded_at=body.traded_at,
    )
    tr = Trade(
        employee_id=body.employee_id,
        symbol=body.symbol.upper(),
        side=body.side,
        quantity=body.quantity,
        price=body.price,
        traded_at=body.traded_at,
        pnl_1d=scores.pnl_1d,
        anomaly_score=scores.anomaly_score,
        risk_score=scores.risk_score,
    )
    db.add(tr)
    db.commit()
    db.refresh(tr)

    if tr.risk_score >= settings.alert_threshold:
        alert = Alert(
            alert_type=AlertType.trade,
            severity=tr.risk_score,
            title=f"High trade risk: {tr.employee_id} {tr.symbol}",
            employee_id=tr.employee_id,
            trade_id=tr.id,
            details=dumps_json({"anomaly_score": tr.anomaly_score, "pnl_1d": tr.pnl_1d}),
        )
        db.add(alert)
        db.commit()

    _auto_flag_trade_from_access(db, tr)

    return TradeOut(
        id=tr.id,
        employee_id=tr.employee_id,
        symbol=tr.symbol,
        side=tr.side,
        quantity=tr.quantity,
        price=tr.price,
        traded_at=tr.traded_at,
        pnl_1d=tr.pnl_1d,
        anomaly_score=tr.anomaly_score,
        risk_score=tr.risk_score,
    )


_TRADE_IMPORT_SKIP_LINE = re.compile(
    r"^(Page\s+\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\s*$|--\s*\d+\s+of\s+\d+\s*--)\s*$",
    re.I,
)


def _clean_trade_import_lines(text: str) -> list[str]:
    out: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or _TRADE_IMPORT_SKIP_LINE.match(s):
            continue
        out.append(s)
    return out


def _parse_trade_csv_rows(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Returns (normalized_headers, list of row dicts). Supports comma CSV and whitespace/PDF-extracted tables."""
    lines = _clean_trade_import_lines(text)
    if not lines:
        raise ValueError("CSV has no rows")

    first = lines[0]
    if "," in first:
        parsed = list(csv.reader(lines))
        if not parsed:
            raise ValueError("CSV has no rows")
        raw_headers = parsed[0]
        headers = [(h or "").strip().lstrip("\ufeff").lower().replace(" ", "_") for h in raw_headers]
        rows: list[dict[str, str]] = []
        for parts in parsed[1:]:
            padded = list(parts) + [""] * max(0, len(headers) - len(parts))
            row = {headers[i]: (padded[i] if i < len(padded) else "").strip() for i in range(len(headers))}
            if any(row.values()):
                rows.append(row)
        return headers, rows

    # Whitespace-separated (e.g. PDF copy/paste): employee_id symbol side quantity price traded_at
    header_parts = re.split(r"\s+", first.lower())
    expected = ["employee_id", "symbol", "side", "quantity", "price", "traded_at"]
    if len(header_parts) >= 6 and header_parts[:6] == expected:
        headers = expected
        body = lines[1:]
    else:
        raise ValueError(
            "Trade file must be comma-separated CSV, or a space-separated table with header: "
            "employee_id symbol side quantity price traded_at"
        )

    rows_ws: list[dict[str, str]] = []
    for ln in body:
        parts = re.split(r"\s+", ln.strip())
        if len(parts) < 6:
            continue
        emp, sym, side, qty_s, px_s = parts[0], parts[1], parts[2], parts[3], parts[4]
        traded_at = parts[5]
        rows_ws.append(
            {
                "employee_id": emp,
                "symbol": sym,
                "side": side,
                "quantity": qty_s,
                "price": px_s,
                "traded_at": traded_at,
            }
        )
    if not rows_ws:
        raise ValueError("No data rows found after header")
    return headers, rows_ws


@app.post("/api/trades/import-csv", response_model=dict)
async def import_trades_csv(
    file: UploadFile = File(...),
    align_to_access: bool = Query(
        default=False,
        description="If true, override CSV traded_at to just after document access (demo). Default false: keep dataset times.",
    ),
    document_id: int | None = Query(
        default=None,
        description="When set, align to access on this document only (View/Download first). Omit to use each employee's latest access on any document.",
    ),
    replace_employees_in_csv: bool = Query(
        default=True,
        description="Delete existing trades for employee_ids present in this file before insert (repeat runs without duplicate rows).",
    ),
    backfill_missing_access_before_trade: bool = Query(
        default=True,
        description="If document_id is provided and an employee has no access log on that document, create one just before that employee's earliest imported trade.",
    ),
    db: Session = Depends(get_db),
) -> dict:
    """
    Import trades from comma-separated CSV or whitespace tables (e.g. PDF copy/paste).
    Columns: employee_id, symbol, side, quantity, price, traded_at
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty CSV file")
    # Support PDF uploads (like `external_stock_trades_20_1_people.csv.pdf`) by extracting text first.
    is_pdf = (file.filename or "").lower().endswith(".pdf")
    if is_pdf:
        try:
            text = _extract_pdf_text(raw)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Could not extract trade text from PDF: {e}")
    else:
        try:
            text = raw.decode("utf-8-sig", errors="ignore")
        except Exception:
            raise HTTPException(status_code=400, detail="Could not decode CSV file")

    try:
        headers, data_rows = _parse_trade_csv_rows(text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    required_cols = {"employee_id", "symbol", "side", "quantity", "price", "traded_at"}
    if not required_cols.issubset(set(headers)):
        raise HTTPException(
            status_code=400,
            detail=f"CSV must include columns: {', '.join(sorted(required_cols))}. Found: {headers}",
        )

    if document_id is not None and db.get(Document, document_id) is None:
        raise HTTPException(status_code=404, detail="document_id not found")

    emp_ids: set[str] = set()
    for row in data_rows:
        eid = (row.get("employee_id") or "").strip()
        if eid:
            emp_ids.add(eid)
    if replace_employees_in_csv and emp_ids:
        db.execute(delete(Trade).where(Trade.employee_id.in_(emp_ids)))
        db.commit()

    # Ensure investigation can run even if user skipped View/Download:
    # create per-employee access logs before each employee's first imported trade.
    # This never uses document created_at as access time.
    if document_id is not None and backfill_missing_access_before_trade:
        earliest_trade_by_emp: dict[str, datetime] = {}
        for row in data_rows:
            eid = (row.get("employee_id") or "").strip()
            if not eid:
                continue
            try:
                row_trade_at = _parse_iso_datetime(row.get("traded_at") or "")
            except Exception:
                continue
            cur = earliest_trade_by_emp.get(eid)
            if cur is None or row_trade_at < cur:
                earliest_trade_by_emp[eid] = row_trade_at

        for eid, first_trade_at in earliest_trade_by_emp.items():
            existing_access = (
                db.execute(
                    select(DocumentAccessLog)
                    .where(
                        DocumentAccessLog.document_id == document_id,
                        DocumentAccessLog.employee_id == eid,
                    )
                    .order_by(DocumentAccessLog.accessed_at.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            desired_accessed_at = first_trade_at - timedelta(seconds=1)
            if existing_access is None:
                db.add(
                    DocumentAccessLog(
                        document_id=document_id,
                        employee_id=eid,
                        access_type="import_backfill",
                        accessed_at=desired_accessed_at,
                    )
                )
            elif existing_access.accessed_at >= first_trade_at:
                # Override late access so that investigation can list "trades after access".
                existing_access.access_type = existing_access.access_type or "import_backfill"
                existing_access.access_type = "import_backfill"
                existing_access.accessed_at = desired_accessed_at
        db.commit()

    slot_by_emp: dict[str, int] = defaultdict(int)
    created = 0
    for idx, row in enumerate(data_rows, start=2):
        try:
            employee_id = row.get("employee_id", "").strip()
            symbol = row.get("symbol", "").strip().upper()
            side = (row.get("side") or "buy").strip().lower()
            quantity = float(row.get("quantity") or 0)
            price = float(row.get("price") or 0)
            traded_at = _parse_iso_datetime(row.get("traded_at") or "")
            if not employee_id or not symbol:
                raise ValueError("employee_id/symbol missing")
            if side not in {"buy", "sell"}:
                raise ValueError("side must be buy or sell")
            if align_to_access:
                if document_id is not None:
                    log = db.execute(
                        select(DocumentAccessLog)
                        .where(
                            DocumentAccessLog.employee_id == employee_id,
                            DocumentAccessLog.document_id == document_id,
                        )
                        .order_by(DocumentAccessLog.accessed_at.desc())
                        .limit(1)
                    ).scalars().first()
                else:
                    log = db.execute(
                        select(DocumentAccessLog)
                        .where(DocumentAccessLog.employee_id == employee_id)
                        .order_by(DocumentAccessLog.accessed_at.desc())
                        .limit(1)
                    ).scalars().first()
                if log is not None:
                    n = slot_by_emp[employee_id]
                    traded_at = log.accessed_at + timedelta(minutes=1, seconds=min(n, 59))
                    slot_by_emp[employee_id] = n + 1
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid row {idx}: {e}")

        scores = score_trade(db, employee_id, symbol, quantity, price, traded_at)
        tr = Trade(
            employee_id=employee_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            traded_at=traded_at,
            pnl_1d=scores.pnl_1d,
            anomaly_score=scores.anomaly_score,
            risk_score=scores.risk_score,
        )
        db.add(tr)
        db.commit()
        db.refresh(tr)
        _auto_flag_trade_from_access(db, tr)
        created += 1

    return {
        "ok": True,
        "imported_trades": created,
        "align_to_access": align_to_access,
        "document_id": document_id,
        "replace_employees_in_csv": replace_employees_in_csv,
        "backfill_missing_access_before_trade": backfill_missing_access_before_trade,
    }


@app.post("/api/demo/seed-recent-buys", response_model=dict)
def seed_recent_buys_dataset(db: Session = Depends(get_db)) -> dict:
    """
    Generates a small recent-buy dataset for demo/testing auto insider detection.
    """
    now = datetime.utcnow()
    dataset = [
        # employee_id, symbol, side, quantity, price, traded_at
        ("E123", "ACME", "buy", 1200.0, 10.25, now - timedelta(hours=6)),
        ("E123", "ZZZZ", "buy", 90.0, 44.10, now - timedelta(hours=28)),
        ("E777", "BETA", "buy", 800.0, 21.30, now - timedelta(hours=20)),
        ("E777", "MSFT", "sell", 150.0, 410.00, now - timedelta(hours=16)),
        ("E555", "AAPL", "buy", 300.0, 188.70, now - timedelta(days=2, hours=3)),
    ]

    created = 0
    auto_flagged = 0
    for (employee_id, symbol, side, qty, price, traded_at) in dataset:
        scores = score_trade(
            db=db,
            employee_id=employee_id,
            symbol=symbol,
            quantity=qty,
            price=price,
            traded_at=traded_at,
        )
        tr = Trade(
            employee_id=employee_id,
            symbol=symbol,
            side=side,
            quantity=qty,
            price=price,
            traded_at=traded_at,
            pnl_1d=scores.pnl_1d,
            anomaly_score=scores.anomaly_score,
            risk_score=scores.risk_score,
        )
        db.add(tr)
        db.commit()
        db.refresh(tr)
        created += 1
        if _auto_flag_trade_from_access(db, tr):
            auto_flagged += 1

    return {"ok": True, "created_trades": created, "auto_flagged": auto_flagged}


@app.post("/seed-demo", response_model=dict)
@app.post("/api/seed-demo", response_model=dict)
def seed_demo(db: Session = Depends(get_db)) -> dict:
    # Reset all demo data
    db.execute(delete(DocumentAccessLog))
    db.execute(delete(Alert))
    db.execute(delete(Trade))
    db.execute(delete(Document))
    db.execute(delete(Employee))
    db.commit()

    employees = [
        Employee(id="E101", name="Alice Johnson"),
        Employee(id="E102", name="Brian Lee"),
        Employee(id="E103", name="Carla Diaz"),
    ]
    for emp in employees:
        db.add(emp)
    db.commit()

    now = datetime.utcnow()
    docs_data = [
        ("aapl_strategy.pdf", "Confidential AAPL investment memo and earnings guidance update.", "AAPL", 82),
        ("tsla_board.pdf", "Merger discussion related to TSLA supplier financing.", "TSLA", 78),
        ("public_report.pdf", "General market commentary and macro updates.", "SPY", 22),
    ]
    docs: list[Document] = []
    for fname, text, company, score in docs_data:
        d = Document(
            filename=fname,
            source=DocumentSource.upload,
            storage_path=str(_ensure_storage_dir() / f"seed_{fname}"),
            extracted_text=text,
            company=company,
            risk_score=50 if score >= 70 else 20,
            mnpi_score=score,
            mnpi_labels=dumps_json([]),
            mnpi_entities=dumps_json([{"type": "ticker", "value": company}]),
            restricted=score >= 75,
        )
        db.add(d)
        db.flush()
        docs.append(d)
    db.commit()

    trade_rows = [
        Trade(employee_id="E101", symbol="AAPL", side="buy", quantity=250, price=189.5, traded_at=now - timedelta(hours=20)),
        Trade(employee_id="E101", symbol="MSFT", side="buy", quantity=120, price=412.0, traded_at=now - timedelta(hours=8)),
        Trade(employee_id="E101", symbol="TSLA", side="sell", quantity=40, price=220.0, traded_at=now - timedelta(days=3, hours=2)),
        Trade(employee_id="E101", symbol="AMZN", side="buy", quantity=85, price=178.4, traded_at=now - timedelta(days=1, hours=5)),
        Trade(employee_id="E101", symbol="GOOGL", side="buy", quantity=95, price=159.2, traded_at=now - timedelta(hours=30)),
        Trade(employee_id="E101", symbol="NVDA", side="sell", quantity=50, price=867.0, traded_at=now - timedelta(hours=15)),
        Trade(employee_id="E101", symbol="META", side="buy", quantity=70, price=498.0, traded_at=now - timedelta(days=2, hours=12)),
        Trade(employee_id="E102", symbol="TSLA", side="buy", quantity=100, price=220.0, traded_at=now - timedelta(days=2, hours=2)),
        Trade(employee_id="E102", symbol="AAPL", side="buy", quantity=75, price=191.0, traded_at=now - timedelta(hours=18)),
        Trade(employee_id="E102", symbol="MSFT", side="sell", quantity=30, price=410.0, traded_at=now - timedelta(days=1, hours=4)),
        Trade(employee_id="E102", symbol="GOOGL", side="buy", quantity=65, price=158.0, traded_at=now - timedelta(hours=12)),
        Trade(employee_id="E102", symbol="NVDA", side="buy", quantity=42, price=862.0, traded_at=now - timedelta(days=4)),
        Trade(employee_id="E103", symbol="GOOGL", side="buy", quantity=80, price=159.4, traded_at=now - timedelta(hours=22)),
        Trade(employee_id="E103", symbol="MSFT", side="buy", quantity=90, price=412.0, traded_at=now - timedelta(hours=10)),
        Trade(employee_id="E103", symbol="AAPL", side="sell", quantity=20, price=190.5, traded_at=now - timedelta(days=1, hours=7)),
        Trade(employee_id="E103", symbol="TSLA", side="buy", quantity=55, price=219.3, traded_at=now - timedelta(days=2, hours=3)),
        Trade(employee_id="E103", symbol="AMZN", side="buy", quantity=100, price=177.8, traded_at=now - timedelta(hours=28)),
        Trade(employee_id="E103", symbol="META", side="buy", quantity=45, price=500.1, traded_at=now - timedelta(days=3, hours=3)),
        Trade(employee_id="E103", symbol="NFLX", side="sell", quantity=18, price=642.0, traded_at=now - timedelta(hours=9)),
        Trade(employee_id="E103", symbol="JPM", side="buy", quantity=210, price=198.5, traded_at=now - timedelta(days=1, hours=2)),
    ]
    for tr in trade_rows:
        scores = score_trade(db, tr.employee_id, tr.symbol, tr.quantity, tr.price, tr.traded_at)
        tr.pnl_1d = scores.pnl_1d
        tr.anomaly_score = scores.anomaly_score
        tr.risk_score = scores.risk_score
        db.add(tr)
    db.commit()

    # Access events that trigger automatic scan against existing trades
    access_events = [
        DocumentAccessLog(document_id=docs[0].id, employee_id="E101", access_type="view", accessed_at=now - timedelta(hours=30)),
        DocumentAccessLog(document_id=docs[1].id, employee_id="E102", access_type="download", accessed_at=now - timedelta(days=2, hours=6)),
        DocumentAccessLog(document_id=docs[2].id, employee_id="E103", access_type="view", accessed_at=now - timedelta(hours=26)),
    ]
    db.add_all(access_events)
    db.commit()
    for log in access_events:
        db.refresh(log)

    flagged_total = 0
    for log in access_events:
        doc = db.get(Document, log.document_id)
        if not doc:
            continue
        flagged_total += len(_auto_flag_access_from_existing_trades(db, log, doc))

    return {
        "ok": True,
        "employees": len(employees),
        "documents": len(docs),
        "trades": len(trade_rows),
        "flagged_alerts": flagged_total,
    }


@app.get("/employee-investigation/{employee_id}", response_model=EmployeeInvestigationOut)
@app.get("/api/employee-investigation/{employee_id}", response_model=EmployeeInvestigationOut)
def employee_investigation(
    employee_id: str,
    document_id: int | None = Query(
        default=None,
        description="Document ID to anchor access time. Strongly recommended (latest uploaded doc).",
    ),
    db: Session = Depends(get_db),
) -> EmployeeInvestigationOut:
    """
    Investigation uses the employee's access time on the given document (or latest access for that employee).
    Only trades strictly after that access time are returned.
    """
    note_parts: list[str] = []

    if document_id is not None:
        doc = db.get(Document, document_id)
        if doc is None:
            doc = db.execute(select(Document).order_by(Document.created_at.desc()).limit(1)).scalars().first()
            if doc is None:
                raise HTTPException(
                    status_code=400,
                    detail="No documents available yet. Upload a document first.",
                )
            note_parts.append(
                f"Requested document_id {document_id} was not found. Using latest document {doc.id} instead."
            )
            document_id = doc.id

        latest_access = (
            db.execute(
                select(DocumentAccessLog)
                .where(
                    DocumentAccessLog.employee_id == employee_id,
                    DocumentAccessLog.document_id == document_id,
                )
                .order_by(DocumentAccessLog.accessed_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if latest_access is None:
            note_parts.append(
                "No access logged for this employee on the requested document. "
                "Using a synthetic access time before the employee's earliest trade."
            )
    else:
        latest_access = (
            db.execute(
                select(DocumentAccessLog)
                .where(DocumentAccessLog.employee_id == employee_id)
                .order_by(DocumentAccessLog.accessed_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if latest_access is None:
            doc = db.execute(select(Document).order_by(Document.created_at.desc()).limit(1)).scalars().first()
            if doc is None:
                raise HTTPException(
                    status_code=400,
                    detail="No documents available yet. Upload a document first.",
                )
            note_parts.append(
                "No document access log found for this employee. "
                "Using a synthetic access time before the employee's earliest trade."
            )
        else:
            doc = db.get(Document, latest_access.document_id)
            if doc is None:
                doc = db.execute(select(Document).order_by(Document.created_at.desc()).limit(1)).scalars().first()
                if doc is None:
                    raise HTTPException(
                        status_code=400,
                        detail="No documents available yet. Upload a document first.",
                    )
                note_parts.append(
                    f"Access log referenced missing document {latest_access.document_id}. Using latest document {doc.id}."
                )

    bounds = db.execute(
        select(func.min(Trade.traded_at), func.max(Trade.traded_at)).where(Trade.employee_id == employee_id)
    ).first()
    earliest_trade: datetime | None = bounds[0] if bounds else None
    latest_trade: datetime | None = bounds[1] if bounds else None

    if latest_access is not None:
        access_time = latest_access.accessed_at
        access_source = f"document_access_log:{(latest_access.access_type or 'view')}"
    elif earliest_trade is not None:
        access_time = earliest_trade - timedelta(seconds=1)
        access_source = "synthetic:before_first_trade"
    else:
        access_time = datetime.utcnow()
        access_source = "synthetic:no_trades"

    note = " ".join(note_parts) if note_parts else None

    doc_company = normalize_company_to_ticker(doc.company or extract_company_or_ticker(doc.extracted_text or ""))
    trades = db.execute(
        select(Trade).where(Trade.employee_id == employee_id, Trade.traded_at > access_time).order_by(Trade.traded_at.asc())
    ).scalars().all()

    employee_total_trades = int(db.scalar(select(func.count()).select_from(Trade).where(Trade.employee_id == employee_id)) or 0)

    out: list[InvestigationTradeOut] = []
    matching = 0
    for tr in trades:
        is_match = (tr.symbol or "").upper() == doc_company
        if is_match:
            matching += 1
        dt_days = (tr.traded_at - access_time).total_seconds() / 86400.0
        out.append(
            InvestigationTradeOut(
                employee_id=employee_id,
                symbol=tr.symbol,
                quantity=tr.quantity,
                access_time=access_time,
                trade_time=tr.traded_at,
                time_difference_days=round(dt_days, 3),
                risk_tag="HIGH" if is_match else "LOW",
            )
        )

    hint: str | None = None
    if not out:
        if employee_total_trades == 0:
            hint = (
                "No trades in database for this employee yet. Import a CSV (Trading Monitor). "
                "Use a comma CSV or the space-separated table export; PDF binary upload may fail—use .csv or paste text."
            )
        elif latest_trade is not None and latest_trade <= access_time:
            hint = (
                f"Access time is after your latest imported trade ({latest_trade.isoformat()}). "
                "This app only lists trades strictly after View/Download. Clear data + re-run with View/Download before importing, "
                "or check “Stamp trade times after access” and re-import."
            )
        else:
            hint = (
                "No trades with traded_at after this access time. If dates look correct, confirm Employee ID matches the CSV "
                "and the scoped document is the one you opened."
            )

    return EmployeeInvestigationOut(
        employee_id=employee_id,
        document_id=doc.id,
        document_company=doc_company,
        document_created_at=doc.created_at,
        access_time=access_time,
        access_source=access_source,
        note=note,
        trades_after_access=out,
        total_trades_after_access=len(out),
        matching_trades_count=matching,
        employee_total_trades_in_db=employee_total_trades,
        employee_earliest_trade_at=earliest_trade,
        employee_latest_trade_at=latest_trade,
        hint=hint,
    )


@app.post("/api/insider/scan-recent", response_model=dict)
def scan_recent_trades_for_insider_flags(days: int = 7, db: Session = Depends(get_db)) -> dict:
    days = max(1, min(days, 30))
    since = datetime.utcnow() - timedelta(days=days)
    recent_trades = db.execute(
        select(Trade).where(Trade.traded_at >= since).order_by(Trade.traded_at.desc()).limit(1000)
    ).scalars().all()

    flagged: list[dict] = []
    for tr in recent_trades:
        hit = _auto_flag_trade_from_access(db, tr)
        if hit:
            flagged.append(hit)

    return {"ok": True, "scanned_trades": len(recent_trades), "flagged": flagged, "flagged_count": len(flagged)}


@app.get("/trades/{employee_id}", response_model=list[TradeOut])
def get_employee_trades_alias(employee_id: str, db: Session = Depends(get_db)) -> list[TradeOut]:
    trades = db.execute(
        select(Trade).where(Trade.employee_id == employee_id).order_by(Trade.traded_at.desc()).limit(300)
    ).scalars().all()
    return [
        TradeOut(
            id=t.id,
            employee_id=t.employee_id,
            symbol=t.symbol,
            side=t.side,
            quantity=t.quantity,
            price=t.price,
            traded_at=t.traded_at,
            pnl_1d=t.pnl_1d,
            anomaly_score=t.anomaly_score,
            risk_score=t.risk_score,
        )
        for t in trades
    ]


@app.get("/api/trades", response_model=list[TradeOut])
def list_trades(db: Session = Depends(get_db)) -> list[TradeOut]:
    trades = db.execute(select(Trade).order_by(Trade.traded_at.desc()).limit(300)).scalars().all()
    return [
        TradeOut(
            id=t.id,
            employee_id=t.employee_id,
            symbol=t.symbol,
            side=t.side,
            quantity=t.quantity,
            price=t.price,
            traded_at=t.traded_at,
            pnl_1d=t.pnl_1d,
            anomaly_score=t.anomaly_score,
            risk_score=t.risk_score,
        )
        for t in trades
    ]


@app.get("/api/trades/{employee_id}", response_model=list[TradeOut])
def get_employee_trades(employee_id: str, db: Session = Depends(get_db)) -> list[TradeOut]:
    return get_employee_trades_alias(employee_id, db)


@app.get("/api/correlation", response_model=CorrelationGraph)
def get_correlation(db: Session = Depends(get_db)) -> CorrelationGraph:
    hits = correlate(db, window_hours=settings.correlation_window_hours)
    edges = [
        CorrelationEdge(
            employee_id=h.employee_id,
            symbol=h.symbol,
            document_id=h.document_id,
            trade_id=h.trade_id,
            score=h.score,
            access_time=h.access_time,
            trade_time=h.trade_time,
        )
        for h in hits
    ]
    # One correlation alert per (trade_id, document_id); same hit is recomputed on every refresh.
    for h in hits[:10]:
        if h.score < settings.correlation_alert_threshold:
            continue
        existing = db.execute(
            select(Alert).where(
                Alert.alert_type == AlertType.correlation,
                Alert.trade_id == h.trade_id,
                Alert.document_id == h.document_id,
            )
        ).scalar_one_or_none()
        if existing:
            continue
        lag_minutes = int(round((h.trade_time - h.access_time).total_seconds() / 60.0))
        db.add(
            Alert(
                alert_type=AlertType.correlation,
                severity=h.score,
                title=f"Potential insider trading: {h.employee_id} {h.symbol}",
                employee_id=h.employee_id,
                document_id=h.document_id,
                trade_id=h.trade_id,
                details=dumps_json(
                    {
                        "score": h.score,
                        "access_time": h.access_time.isoformat(),
                        "trade_time": h.trade_time.isoformat(),
                        "lag_minutes": lag_minutes,
                        "mode": "correlation_dashboard",
                    }
                ),
            )
        )
    db.commit()
    return CorrelationGraph(edges=edges)


@app.get("/api/alerts", response_model=list[AlertOut])
def list_alerts(db: Session = Depends(get_db)) -> list[AlertOut]:
    alerts = db.execute(select(Alert).order_by(Alert.created_at.desc()).limit(300)).scalars().all()
    out: list[AlertOut] = []
    for a in alerts:
        try:
            details = json.loads(a.details or "{}")
        except Exception:
            details = {}
        out.append(
            AlertOut(
                id=a.id,
                alert_type=a.alert_type.value,
                severity=a.severity,
                title=a.title,
                employee_id=a.employee_id,
                document_id=a.document_id,
                trade_id=a.trade_id,
                created_at=a.created_at,
                resolved=a.resolved,
                details=details,
            )
        )
    return out


@app.get("/alerts", response_model=list[AlertOut])
def list_alerts_alias(db: Session = Depends(get_db)) -> list[AlertOut]:
    return list_alerts(db)


@app.post("/api/seed", response_model=SeedResponse)
def seed(db: Session = Depends(get_db)) -> SeedResponse:
    """
    Creates demo docs + access logs + trades so the dashboards show data.
    """
    # Documents
    docs_payload = [
        (
            "deal_notes_ACME.txt",
            "CONFIDENTIAL: ACME Q2 earnings guidance looks strong. Do not distribute. Ticker ACME. Next week finalize term sheet.",
        ),
        (
            "chat_log_misc.txt",
            "Team: please review the quarterly OKRs. Nothing market-moving here.",
        ),
        (
            "mna_pipeline.txt",
            "Inside information: potential merger discussion with BETA. Due diligence starts tomorrow. BETA",
        ),
    ]

    # Create a realistic time gap: access happens before trade
    now = datetime.utcnow()
    access_time_e123 = now - timedelta(hours=2, minutes=15)
    access_time_e777 = now - timedelta(hours=1, minutes=5)

    created_docs = 0
    for (fname, text) in docs_payload:
        mnpi = analyze_text(text, restrict_threshold=settings.mnpi_restrict_threshold)
        d = Document(
            filename=fname,
            source=DocumentSource.upload,
            storage_path=str(_ensure_storage_dir() / f"seed_{fname}"),
            extracted_text=text,
            mnpi_score=mnpi.score,
            mnpi_labels=dumps_json(mnpi.labels),
            mnpi_entities=dumps_json(mnpi.entities),
            restricted=mnpi.restricted,
        )
        db.add(d)
        db.flush()
        created_docs += 1

        db.add(
            DocumentAccessLog(
                document_id=d.id,
                employee_id="E123",
                access_type="view",
                accessed_at=access_time_e123,
            )
        )
        if "BETA" in text:
            db.add(
                DocumentAccessLog(
                    document_id=d.id,
                    employee_id="E777",
                    access_type="download",
                    accessed_at=access_time_e777,
                )
            )

        if d.mnpi_score >= settings.alert_threshold:
            db.add(
                Alert(
                    alert_type=AlertType.mnpi,
                    severity=d.mnpi_score,
                    title=f"High MNPI score in document: {d.filename}",
                    document_id=d.id,
                    details=dumps_json({"labels": mnpi.labels}),
                )
            )

    # Trades (placed after access)
    trades_payload = [
        TradeIn(employee_id="E123", symbol="ACME", side="buy", quantity=5000, price=10.5, traded_at=now),
        TradeIn(employee_id="E777", symbol="BETA", side="buy", quantity=8000, price=22.0, traded_at=now),
        TradeIn(employee_id="E123", symbol="ZZZZ", side="buy", quantity=10, price=100.0, traded_at=now - timedelta(minutes=22)),
    ]

    created_trades = 0
    for t in trades_payload:
        scores = score_trade(db, t.employee_id, t.symbol, t.quantity, t.price, t.traded_at)
        tr = Trade(
            employee_id=t.employee_id,
            symbol=t.symbol,
            side=t.side,
            quantity=t.quantity,
            price=t.price,
            traded_at=t.traded_at,
            pnl_1d=scores.pnl_1d,
            anomaly_score=scores.anomaly_score,
            risk_score=scores.risk_score,
        )
        db.add(tr)
        db.flush()
        created_trades += 1
        if tr.risk_score >= settings.alert_threshold:
            db.add(
                Alert(
                    alert_type=AlertType.trade,
                    severity=tr.risk_score,
                    title=f"High trade risk: {tr.employee_id} {tr.symbol}",
                    employee_id=tr.employee_id,
                    trade_id=tr.id,
                    details=dumps_json({"anomaly_score": tr.anomaly_score}),
                )
            )

    db.commit()

    # Correlation (also generates alerts)
    _ = correlate(db, window_hours=settings.correlation_window_hours)

    # Count alerts
    alert_count = db.execute(select(Alert)).scalars().all()

    return SeedResponse(documents=created_docs, trades=created_trades, alerts=len(alert_count))


@app.on_event("startup")
def _startup() -> None:
    # Create tables for MVP (keeps setup simple). Alembic is included for later hardening.
    from app.db.base import Base

    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        table_info = conn.exec_driver_sql("PRAGMA table_info(documents)").fetchall()
        existing_cols = {row[1] for row in table_info}
        if "company" not in existing_cols:
            conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN company VARCHAR(32) DEFAULT ''")
        if "risk_score" not in existing_cols:
            conn.exec_driver_sql("ALTER TABLE documents ADD COLUMN risk_score INTEGER DEFAULT 0")

