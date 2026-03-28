from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO

from app.db.models import Document

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


TRIGGER_WORDS = ["confidential", "earnings", "merger", "acquisition"]
_TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")
_DOLLAR_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")

_COMPANY_TO_TICKER: dict[str, str] = {
    "APPLE": "AAPL",
    "APPLE INC": "AAPL",
    "MICROSOFT": "MSFT",
    "MICROSOFT CORP": "MSFT",
    "AMAZON": "AMZN",
    "AMAZON.COM": "AMZN",
    "GOOGLE": "GOOGL",
    "ALPHABET": "GOOGL",
    "META": "META",
    "META PLATFORMS": "META",
    "TESLA": "TSLA",
    "NVIDIA": "NVDA",
    "NETFLIX": "NFLX",
    "SALESFORCE": "CRM",
    "ORACLE": "ORCL",
    "INTEL": "INTC",
    "AMD": "AMD",
    "IBM": "IBM",
    "QUALCOMM": "QCOM",
    "ADOBE": "ADBE",
    "PAYPAL": "PYPL",
    "UBER": "UBER",
    "AIRBNB": "ABNB",
    "JPMORGAN": "JPM",
    "GOLDMAN SACHS": "GS",
    "BANK OF AMERICA": "BAC",
    "WALMART": "WMT",
    "COCA COLA": "KO",
    "PEPSICO": "PEP",
    "EXXON": "XOM",
    "CHEVRON": "CVX",
    "BOEING": "BA",
    "DISNEY": "DIS",
    "ACME": "ACME",
    "BETA": "BETA",
}
_TICKERS = set(_COMPANY_TO_TICKER.values())
_NOISE_TOKENS = {"AND", "THE", "FOR", "WITH", "THIS", "THAT", "CONFIDENTIAL", "REPORT", "EARNINGS"}


@dataclass(frozen=True)
class DocumentAnalysis:
    extracted_text: str
    trigger_matches: list[str]
    company: str
    risk_score: int


def extract_pdf_text(raw: bytes) -> str:
    if PdfReader is None:
        raise ValueError("PDF support unavailable. Install pypdf.")
    reader = PdfReader(BytesIO(raw))
    parts: list[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    text = "\n".join(parts).strip()
    if not text:
        raise ValueError("Could not extract text from PDF.")
    return text


def extract_company_or_ticker(text: str) -> str:
    upper = (text or "").upper()
    # 1) explicit ticker mention, e.g. $AAPL
    for m in _DOLLAR_TICKER_RE.finditer(upper):
        tok = m.group(1).upper()
        if 3 <= len(tok) <= 5:
            return tok

    # 2) known company aliases
    for company, ticker in _COMPANY_TO_TICKER.items():
        if re.search(rf"\b{re.escape(company)}\b", upper):
            return ticker

    # 3) fallback direct ticker mention in text
    for m in _TICKER_RE.finditer(upper):
        token = m.group(0).upper()
        if token in _NOISE_TOKENS:
            continue
        if token in _TICKERS or (3 <= len(token) <= 5):
            return token
    return ""


def normalize_company_to_ticker(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    upper = raw.upper().replace(".", "").strip()

    # Already ticker-like
    if re.fullmatch(r"[A-Z]{3,5}", upper):
        return upper

    if upper in _COMPANY_TO_TICKER:
        return _COMPANY_TO_TICKER[upper]

    # tolerant cleanup for names like "Apple Inc."
    cleaned = re.sub(r"[^A-Z0-9\s]", " ", upper)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned in _COMPANY_TO_TICKER:
        return _COMPANY_TO_TICKER[cleaned]
    for company, ticker in _COMPANY_TO_TICKER.items():
        if company in cleaned:
            return ticker
    return ""


def analyze_document_text(text: str) -> DocumentAnalysis:
    lower = (text or "").lower()
    matches = [w for w in TRIGGER_WORDS if w in lower]
    extracted_company = extract_company_or_ticker(text)
    company = normalize_company_to_ticker(extracted_company)

    score = 0
    if matches:
        score += 20
    if company:
        score += 30

    return DocumentAnalysis(
        extracted_text=text,
        trigger_matches=matches,
        company=company,
        risk_score=score,
    )


def risk_level(score: int) -> str:
    if score > 70:
        return "HIGH"
    if score > 40:
        return "MEDIUM"
    return "LOW"


def get_document_company(doc: Document) -> str:
    if doc.company:
        return normalize_company_to_ticker(doc.company)
    extracted = extract_company_or_ticker(doc.extracted_text or "")
    return normalize_company_to_ticker(extracted)
