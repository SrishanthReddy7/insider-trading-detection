from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.db.models import Document, DocumentAccessLog, Trade
from app.services.document import get_document_company


@dataclass(frozen=True)
class CorrelationHit:
    employee_id: str
    symbol: str
    document_id: int
    trade_id: int
    score: int
    access_time: datetime
    trade_time: datetime


def _clip(x: int) -> int:
    return max(0, min(100, x))


def _extract_tickers(doc: Document) -> set[str]:
    try:
        entities = json.loads(doc.mnpi_entities or "[]")
    except Exception:
        entities = []
    return {e.get("value") for e in entities if e.get("type") == "ticker" and isinstance(e.get("value"), str)}


def correlate(db: Session, window_hours: int) -> list[CorrelationHit]:
    """
    Links:
    employee -> accessed document (with tickers) -> traded symbol
    """
    # Pull recent trades
    now = datetime.utcnow()
    since = now - timedelta(hours=window_hours)
    trades = db.execute(select(Trade).where(Trade.traded_at >= since).order_by(Trade.traded_at.desc()).limit(500)).scalars().all()

    hits: list[CorrelationHit] = []

    for tr in trades:
        access_since = tr.traded_at - timedelta(hours=window_hours)
        access_until = tr.traded_at
        logs = db.execute(
            select(DocumentAccessLog, Document)
            .join(Document, Document.id == DocumentAccessLog.document_id)
            .where(
                and_(
                    DocumentAccessLog.employee_id == tr.employee_id,
                    DocumentAccessLog.accessed_at >= access_since,
                    DocumentAccessLog.accessed_at <= access_until,
                    Document.mnpi_score >= 50,
                )
            )
            .order_by(DocumentAccessLog.accessed_at.desc())
            .limit(50)
        ).all()

        for (log, doc) in logs:
            tickers = _extract_tickers(doc)
            if tr.symbol in tickers:
                dt_hours = max(0.0, (tr.traded_at - log.accessed_at).total_seconds() / 3600.0)
                time_boost = int(round(_clip(40 + (24 - min(24.0, dt_hours)) * 2)))  # closer => higher
                score = _clip(int(round(0.5 * doc.mnpi_score + 0.3 * tr.risk_score + 0.2 * time_boost)))
                hits.append(
                    CorrelationHit(
                        employee_id=tr.employee_id,
                        symbol=tr.symbol,
                        document_id=doc.id,
                        trade_id=tr.id,
                        score=score,
                        access_time=log.accessed_at,
                        trade_time=tr.traded_at,
                    )
                )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:200]


@dataclass(frozen=True)
class SuspiciousTradeResult:
    is_suspicious: bool
    trade_id: int | None
    company: str
    time_difference_hours: float | None
    risk_score: int
    risk_level: str
    reasons: list[str]
    matched_document_id: int | None
    matched_access_time: datetime | None


def correlate_trade_with_access(
    *,
    employee_id: str,
    symbol: str,
    trade_time: datetime,
    access_logs: list[tuple[DocumentAccessLog, Document]],
) -> SuspiciousTradeResult:
    """
    MVP correlation for insider-trading checks:
    - same employee
    - same company/ticker
    - trade within 3 days of access
    """
    reasons: list[str] = []
    risk_score = 0

    matched_doc_id: int | None = None
    matched_access_time: datetime | None = None
    min_dt_hours: float | None = None

    symbol_upper = (symbol or "").upper()
    same_company = False
    within_3_days = False
    has_trigger_words = False

    for log, doc in access_logs:
        doc_company = get_document_company(doc)
        if doc_company != symbol_upper:
            continue
        same_company = True
        matched_doc_id = doc.id
        matched_access_time = log.accessed_at

        dt_seconds = (trade_time - log.accessed_at).total_seconds()
        if dt_seconds < 0:
            continue
        dt_hours = dt_seconds / 3600.0
        if min_dt_hours is None or dt_hours < min_dt_hours:
            min_dt_hours = dt_hours
        if dt_seconds <= 3 * 24 * 3600:
            within_3_days = True
        if doc.extracted_text:
            lower = doc.extracted_text.lower()
            if any(x in lower for x in ("confidential", "earnings", "merger", "acquisition")):
                has_trigger_words = True

    if has_trigger_words:
        risk_score += 20
        reasons.append("trigger_words_found")
    if same_company:
        risk_score += 30
        reasons.append("company_match")
    if within_3_days:
        risk_score += 30
        reasons.append("trade_within_3_days")

    if risk_score > 70:
        risk_level = "HIGH"
    elif risk_score > 40:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    return SuspiciousTradeResult(
        is_suspicious=within_3_days and same_company,
        trade_id=None,
        company=symbol_upper,
        time_difference_hours=min_dt_hours,
        risk_score=risk_score,
        risk_level=risk_level,
        reasons=reasons,
        matched_document_id=matched_doc_id,
        matched_access_time=matched_access_time,
    )


def detect_suspicious_trades_from_access(
    *,
    document: Document,
    access_log: DocumentAccessLog,
    employee_trades: list[Trade],
    window_days: int = 3,
    high_risk_threshold: int = 70,
) -> list[SuspiciousTradeResult]:
    """
    Triggered when an employee accesses a document:
    find matching recent buys and compute risk/signals.
    """
    doc_company = get_document_company(document)
    if not doc_company:
        return []

    out: list[SuspiciousTradeResult] = []
    for tr in employee_trades:
        if (tr.side or "").lower() != "buy":
            continue
        if (tr.symbol or "").upper() != doc_company:
            continue

        dt_seconds = (tr.traded_at - access_log.accessed_at).total_seconds()
        if dt_seconds < 0:
            continue
        if dt_seconds > window_days * 24 * 3600:
            continue

        reasons: list[str] = []
        risk_score = 0

        is_high_risk_doc = int(document.mnpi_score or 0) >= high_risk_threshold
        if is_high_risk_doc:
            risk_score += 30
            reasons.append("Employee accessed sensitive document")
        risk_score += 30
        reasons.append("Trade matches document company")
        risk_score += 30
        reasons.append(f"Trade occurred within {window_days} days of access")

        if risk_score > 70:
            risk_level = "HIGH"
        elif risk_score > 40:
            risk_level = "MEDIUM"
        else:
            risk_level = "LOW"

        out.append(
            SuspiciousTradeResult(
                is_suspicious=True,
                trade_id=tr.id,
                company=doc_company,
                time_difference_hours=dt_seconds / 3600.0,
                risk_score=risk_score,
                risk_level=risk_level,
                reasons=reasons,
                matched_document_id=document.id,
                matched_access_time=access_log.accessed_at,
            )
        )

    out.sort(key=lambda x: x.risk_score, reverse=True)
    return out

