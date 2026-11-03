"""Calls and receipts are separate append-only facts; circuit state is mutable."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class GatewayGate(Base):
    __tablename__ = "model_gateway_gate"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)


class GatewayCircuit(Base):
    __tablename__ = "model_gateway_circuits"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    failures: Mapped[int] = mapped_column(default=0)
    opened_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    probe_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    probe_call_id: Mapped[UUID | None]
    paused: Mapped[bool] = mapped_column(default=False)
    revision: Mapped[int] = mapped_column(default=0)


class GatewayCall(Entity, Base):
    __tablename__ = "model_gateway_calls"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    episode_id: Mapped[UUID | None] = mapped_column(ForeignKey("episodes.id"))
    workflow_id: Mapped[UUID | None] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    generation_attempt_id: Mapped[UUID | None] = mapped_column(ForeignKey("generation_attempts.id"))
    actor: Mapped[str] = mapped_column(String(100), index=True)
    operation_key: Mapped[str] = mapped_column(String(200))
    sequence: Mapped[int]
    task: Mapped[str] = mapped_column(String(40))
    input_sha256: Mapped[str] = mapped_column(String(64))
    circuit_id: Mapped[str] = mapped_column(ForeignKey("model_gateway_circuits.id"))
    route_json: Mapped[dict] = mapped_column(JSON_TYPE)
    parameters_json: Mapped[dict] = mapped_column(JSON_TYPE)
    maximum_cost: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    __table_args__ = (UniqueConstraint("project_id", "operation_key", "sequence"),)


class GatewayReceipt(Entity, Base):
    __tablename__ = "model_gateway_receipts"
    call_id: Mapped[UUID] = mapped_column(ForeignKey("model_gateway_calls.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24))
    error_code: Mapped[str | None] = mapped_column(String(64))
    usage_json: Mapped[dict] = mapped_column(JSON_TYPE)
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    evidence: Mapped[str | None] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint("call_id", "kind"),)
