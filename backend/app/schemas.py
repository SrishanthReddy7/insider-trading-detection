from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class DocumentOut(BaseModel):
    id: int
    filename: str
    source: str
    company: str
    mnpi_score: int
    risk_score: int
    restricted: bool
    created_at: datetime


class DocumentDetail(DocumentOut):
    extracted_text: str
    mnpi_labels: list[str] = Field(default_factory=list)
    mnpi_entities: list[dict[str, Any]] = Field(default_factory=list)


class AccessLogIn(BaseModel):
    employee_id: str
    access_type: Literal["view", "download"] = "view"


class EmployeeIn(BaseModel):
    id: str
    name: str


class EmployeeOut(BaseModel):
    id: str
    name: str


class TradeIn(BaseModel):
    employee_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float
    price: float
    traded_at: datetime


class TradeOut(BaseModel):
    id: int
    employee_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    traded_at: datetime
    pnl_1d: float
    anomaly_score: int
    risk_score: int


class AlertOut(BaseModel):
    id: int
    alert_type: str
    severity: int
    title: str
    employee_id: str | None
    document_id: int | None
    trade_id: int | None
    created_at: datetime
    resolved: bool
    details: dict[str, Any]


class CorrelationEdge(BaseModel):
    employee_id: str
    symbol: str
    document_id: int
    trade_id: int
    score: int
    access_time: datetime
    trade_time: datetime


class CorrelationGraph(BaseModel):
    edges: list[CorrelationEdge]


class SeedResponse(BaseModel):
    documents: int
    trades: int
    alerts: int


class InsiderAccessLogIn(BaseModel):
    employee_id: str
    document_id: int


class AutoDetectedTradeOut(BaseModel):
    employee_id: str
    symbol: str
    quantity: float
    trade_time: datetime
    document_id: int
    company: str
    time_difference_days: float
    risk_level: str
    reasons: list[str]


class InvestigationTradeOut(BaseModel):
    employee_id: str
    symbol: str
    quantity: float
    access_time: datetime
    trade_time: datetime
    time_difference_days: float
    risk_tag: Literal["HIGH", "LOW"]


class EmployeeInvestigationOut(BaseModel):
    employee_id: str
    document_id: int
    document_company: str
    document_created_at: datetime
    access_time: datetime
    access_source: str
    note: str | None = None
    trades_after_access: list[InvestigationTradeOut]
    total_trades_after_access: int
    matching_trades_count: int
    employee_total_trades_in_db: int = 0
    employee_earliest_trade_at: datetime | None = None
    employee_latest_trade_at: datetime | None = None
    hint: str | None = None

