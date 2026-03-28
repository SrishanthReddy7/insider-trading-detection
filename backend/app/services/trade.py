from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Trade

@dataclass(frozen=True)
class TradeRecordInput:
    employee_id: str
    symbol: str
    trade_time: datetime
    trade_type: str


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


def normalize_trade_type(trade_type: str) -> str:
    v = (trade_type or "").strip().lower()
    return "buy" if v not in {"buy", "sell"} else v


def fetch_employee_trades(db: Session, employee_id: str) -> list[Trade]:
    return (
        db.execute(select(Trade).where(Trade.employee_id == employee_id).order_by(Trade.traded_at.desc()).limit(1000))
        .scalars()
        .all()
    )
