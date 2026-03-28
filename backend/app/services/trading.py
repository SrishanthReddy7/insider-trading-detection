from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Trade


@dataclass(frozen=True)
class TradeScores:
    pnl_1d: float
    anomaly_score: int
    risk_score: int


def _clip(x: float) -> float:
    return float(max(0.0, min(100.0, x)))


def score_trade(db: Session, employee_id: str, symbol: str, quantity: float, price: float, traded_at: datetime) -> TradeScores:
    """
    MVP scoring:
    - baseline anomaly: z-score of notional vs employee's last 60 days
    - pseudo PnL: random-ish but deterministic from inputs (demo only)
    """
    notional = float(abs(quantity) * price)
    since = traded_at - timedelta(days=60)

    rows = db.execute(
        select(Trade.quantity, Trade.price).where(Trade.employee_id == employee_id, Trade.traded_at >= since)
    ).all()
    notionals = np.array([float(abs(q) * p) for (q, p) in rows] + [notional], dtype=float)

    if len(notionals) <= 3:
        z = 0.0
    else:
        mu = float(notionals[:-1].mean())
        sigma = float(notionals[:-1].std(ddof=1) or 1.0)
        z = (notional - mu) / sigma

    anomaly = _clip(50.0 + 15.0 * z)  # centered at 50

    seed = (hash(employee_id) ^ hash(symbol) ^ int(notional) ^ int(traded_at.timestamp())) & 0xFFFFFFFF
    rng = np.random.default_rng(seed)
    direction = 1.0 if quantity > 0 else -1.0
    pnl_1d = float(direction * notional * float(rng.normal(0.002, 0.01)))  # ~ +/-1% typical

    pnl_boost = _clip(50.0 + (pnl_1d / max(1.0, notional)) * 5000.0)  # rough scaling
    risk = int(round(_clip(0.65 * anomaly + 0.35 * pnl_boost)))

    return TradeScores(pnl_1d=pnl_1d, anomaly_score=int(round(anomaly)), risk_score=risk)

