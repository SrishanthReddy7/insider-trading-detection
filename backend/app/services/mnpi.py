from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache

import joblib


_TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")
_DOLLAR_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")
_TICKER_HINT_RE = re.compile(r"\b(?:ticker|symbol)\s*[:=]?\s*([A-Z]{1,5})\b", re.I)
_CONFIDENTIAL_RE = re.compile(r"\b(confidential|non[-\s]?public|do not distribute|inside information)\b", re.I)
_EARNINGS_RE = re.compile(r"\b(earnings|guidance|quarter|q[1-4]|eps)\b", re.I)
_MA_RE = re.compile(r"\b(m&a|merger|acquisition|term sheet|due diligence|deal)\b", re.I)
_DATE_RE = re.compile(r"\b(next week|tomorrow|today|this quarter|next quarter|EOD)\b", re.I)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_NEGATION_RE = re.compile(r"\b(no|not|without|nothing|does\s+not|don't|doesn't|isn't|is\s+not|non[-\s]?material)\b", re.I)

# Keep this small and explicit for MVP to reduce obvious false positives.
_TICKER_STOPWORDS = {
    "A",
    "AN",
    "AND",
    "ARE",
    "AS",
    "AT",
    "BY",
    "DO",
    "EOD",
    "FOR",
    "IN",
    "INSIDE",
    "IS",
    "IT",
    "M",
    "MNPI",
    "NO",
    "NOT",
    "OF",
    "ON",
    "OR",
    "Q1",
    "Q2",
    "Q3",
    "Q4",
    "THE",
    "TO",
    "US",
    "WE",
}

# Demo mapping. Extend with your firm coverage universe.
_COMPANY_TO_TICKER = {
    "ACME": "ACME",
    "BETA": "BETA",
    "APPLE": "AAPL",
    "MICROSOFT": "MSFT",
    "ALPHABET": "GOOGL",
    "GOOGLE": "GOOGL",
    "AMAZON": "AMZN",
    "META": "META",
    "NVIDIA": "NVDA",
    "TESLA": "TSLA",
}


def _extract_tickers(text: str) -> set[str]:
    t = text or ""
    tickers: set[str] = set()

    # High-confidence patterns first.
    tickers.update(m.group(1).upper() for m in _DOLLAR_TICKER_RE.finditer(t))
    tickers.update(m.group(1).upper() for m in _TICKER_HINT_RE.finditer(t))

    upper_text = t.upper()
    for company, ticker in _COMPANY_TO_TICKER.items():
        if re.search(rf"\b{re.escape(company)}\b", upper_text):
            tickers.add(ticker)

    # Fallback extraction with stopword filtering.
    for m in _TICKER_RE.finditer(t):
        token = m.group(0).upper()
        if token in _TICKER_STOPWORDS:
            continue
        if token.isdigit():
            continue
        tickers.add(token)

    return tickers


def _proximity_hits(text: str, tickers: set[str]) -> tuple[int, list[str]]:
    if not text or not tickers:
        return 0, []

    snippets: list[str] = []
    hits = 0
    sentences = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]

    for s in sentences:
        s_upper = s.upper()
        has_ticker = any(re.search(rf"\b{re.escape(t)}\b", s_upper) for t in tickers)
        if not has_ticker:
            continue

        sensitive_match = (
            _has_non_negated_match(_CONFIDENTIAL_RE, s)
            or _has_non_negated_match(_EARNINGS_RE, s)
            or _has_non_negated_match(_MA_RE, s)
            or _has_non_negated_match(_DATE_RE, s)
        )
        if sensitive_match:
            hits += 1
            if len(snippets) < 5:
                snippets.append(s[:220])

    return hits, snippets


@lru_cache(maxsize=1)
def _load_optional_model():
    model_path = (os.getenv("MNPI_MODEL_PATH") or "").strip()
    if not model_path:
        return None
    try:
        return joblib.load(model_path)
    except Exception:
        return None


def _ml_score(text: str) -> int | None:
    model = _load_optional_model()
    if model is None:
        return None

    try:
        if hasattr(model, "predict_proba"):
            prob = float(model.predict_proba([text])[0][1])
            return _clip_score(int(round(prob * 100)))
        pred = int(model.predict([text])[0])
        return 85 if pred == 1 else 15
    except Exception:
        return None


def _is_negated(sentence: str, start_idx: int) -> bool:
    lookback_start = max(0, start_idx - 28)
    left_context = sentence[lookback_start:start_idx]
    return bool(_NEGATION_RE.search(left_context))


def _has_non_negated_match(pattern: re.Pattern[str], sentence: str) -> bool:
    for m in pattern.finditer(sentence):
        if not _is_negated(sentence, m.start()):
            return True
    return False


def _first_non_negated_match(pattern: re.Pattern[str], text: str):
    for m in pattern.finditer(text):
        sentence_start = text.rfind(".", 0, m.start()) + 1
        newline_start = text.rfind("\n", 0, m.start()) + 1
        chunk_start = max(sentence_start, newline_start)
        sentence_end_candidates = [i for i in (text.find(".", m.end()), text.find("\n", m.end())) if i != -1]
        chunk_end = min(sentence_end_candidates) if sentence_end_candidates else len(text)
        sentence = text[chunk_start:chunk_end]
        local_start = max(0, m.start() - chunk_start)
        if not _is_negated(sentence, local_start):
            return m
    return None


@dataclass(frozen=True)
class MnpiResult:
    score: int
    labels: list[str]
    entities: list[dict]
    restricted: bool
    highlighted_snippets: list[str]


def _clip_score(x: int) -> int:
    return max(0, min(100, x))


def analyze_text(text: str, restrict_threshold: int) -> MnpiResult:
    """
    MVP hybrid detector:
    - Regex/rule signals -> score
    - Extracts naive "ticker" entities
    """
    t = text or ""
    labels: list[str] = []
    score = 0
    sentences = [s.strip() for s in _SENTENCE_RE.split(t) if s.strip()]

    has_conf = any(_has_non_negated_match(_CONFIDENTIAL_RE, s) for s in sentences)
    has_earn = any(_has_non_negated_match(_EARNINGS_RE, s) for s in sentences)
    has_ma = any(_has_non_negated_match(_MA_RE, s) for s in sentences)
    has_date = any(_has_non_negated_match(_DATE_RE, s) for s in sentences)

    if has_conf:
        labels.append("confidential_marker")
        score += 30
    if has_earn:
        labels.append("earnings_related")
        score += 20
    if has_ma:
        labels.append("mna_related")
        score += 25
    if has_date:
        labels.append("time_sensitive")
        score += 10

    tickers = sorted(_extract_tickers(t))
    entities: list[dict] = []
    if tickers:
        labels.append("ticker_mentioned")
        score += min(20, 2 * len(tickers))
        entities.extend([{"type": "ticker", "value": x} for x in tickers[:20]])

    proximity_count, proximity_snippets = _proximity_hits(t, set(tickers))
    if proximity_count > 0:
        labels.append("sensitive_ticker_proximity")
        score += min(25, proximity_count * 8)

    ml_score = _ml_score(t)
    if ml_score is not None:
        labels.append("ml_model_signal")
        # Blend deterministic rules and model probability for stability.
        score = int(round(0.65 * score + 0.35 * ml_score))

    snippets: list[str] = []
    for pat in (_CONFIDENTIAL_RE, _EARNINGS_RE, _MA_RE):
        m = _first_non_negated_match(pat, t)
        if m:
            start = max(0, m.start() - 60)
            end = min(len(t), m.end() + 60)
            snippets.append(t[start:end].replace("\n", " ").strip())
    snippets.extend([s for s in proximity_snippets if s not in snippets])

    score = _clip_score(score)
    restricted = score >= restrict_threshold
    return MnpiResult(score=score, labels=labels, entities=entities, restricted=restricted, highlighted_snippets=snippets)


def dumps_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)

