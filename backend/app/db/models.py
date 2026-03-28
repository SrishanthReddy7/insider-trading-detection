from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class DocumentSource(str, enum.Enum):
    upload = "upload"
    email = "email"
    chat = "chat"


class AlertType(str, enum.Enum):
    mnpi = "mnpi"
    trade = "trade"
    correlation = "correlation"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    source: Mapped[DocumentSource] = mapped_column(Enum(DocumentSource), default=DocumentSource.upload)
    storage_path: Mapped[str] = mapped_column(String(500))

    extracted_text: Mapped[str] = mapped_column(Text, default="")
    company: Mapped[str] = mapped_column(String(32), default="", index=True)
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    mnpi_score: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    mnpi_labels: Mapped[str] = mapped_column(Text, default="[]")  # json string for MVP
    mnpi_entities: Mapped[str] = mapped_column(Text, default="[]")  # json string for MVP
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)

    access_logs: Mapped[list["DocumentAccessLog"]] = relationship(back_populates="document")


class DocumentAccessLog(Base):
    __tablename__ = "document_access_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    employee_id: Mapped[str] = mapped_column(String(64), index=True)
    access_type: Mapped[str] = mapped_column(String(32), default="view")  # view|download
    accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, index=True)

    document: Mapped[Document] = relationship(back_populates="access_logs")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))  # buy|sell
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    traded_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), index=True)

    pnl_1d: Mapped[float] = mapped_column(Float, default=0.0)  # demo metric
    anomaly_score: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    risk_score: Mapped[int] = mapped_column(Integer, default=0)  # 0-100


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_type: Mapped[AlertType] = mapped_column(Enum(AlertType))
    severity: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    title: Mapped[str] = mapped_column(String(255))
    details: Mapped[str] = mapped_column(Text, default="{}")  # json string for MVP

    employee_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    document_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    trade_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), default=datetime.utcnow, index=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


Index("ix_trades_employee_time", Trade.employee_id, Trade.traded_at)
Index("ix_access_employee_time", DocumentAccessLog.employee_id, DocumentAccessLog.accessed_at)

