from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Index, Integer, LargeBinary, String, Text, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator):
    """Always store UTC; always return aware-UTC (SQLite drops tzinfo)."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime refused")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Secret(Base):
    __tablename__ = "secrets"
    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    blob: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(50))
    target: Mapped[str] = mapped_column(String(200))
    detail: Mapped[Any] = mapped_column(JSON, default=dict)   # never contains secret values


class AdminUser(Base):
    __tablename__ = "admin_users"
    username: Mapped[str] = mapped_column(String(100), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ApiToken(Base):
    """Read-only API tokens for agents and other apps. Only a hash of the secret is kept."""
    __tablename__ = "api_tokens"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    secret_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_used: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ObservationRow(Base):
    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(100))
    station_id: Mapped[str] = mapped_column(String(100))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    values: Mapped[Any] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("source", "station_id", "ts"), Index("ix_obs_ts", "ts"))


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(200))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    geometry: Mapped[Any] = mapped_column(JSON, nullable=True)
    expires: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    properties: Mapped[Any] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("source", "external_id"), Index("ix_events_ts", "ts"))


class RadarFrameRow(Base):
    __tablename__ = "radar_frames"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(100))
    radar_id: Mapped[str] = mapped_column(String(50))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    path: Mapped[str] = mapped_column(Text)
    georef: Mapped[Any] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("source", "radar_id", "ts"), Index("ix_frames_ts", "ts"))


class AlertLogRow(Base):
    """Every alert that was raised, and how delivery went per output. Never contains secrets."""
    __tablename__ = "alert_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    rule_id: Mapped[str] = mapped_column(String(40), index=True)
    rule_name: Mapped[str] = mapped_column(String(80))
    key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10))          # alert | clear | test
    severity: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[Any] = mapped_column(JSON, default=dict)
    results: Mapped[Any] = mapped_column(JSON, default=dict)   # {output: {"ok": bool, "error": str | None}}
