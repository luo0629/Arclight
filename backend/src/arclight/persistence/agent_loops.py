"""Durable Agent operations, immutable intents and decisions; billing stays in the gateway."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class AgentLoop(Entity, Base):
    __tablename__ = "agent_loops"
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(100))
    operation_sha256: Mapped[str] = mapped_column(String(64))
    request_sha256: Mapped[str] = mapped_column(String(64))
    input_sha256: Mapped[str] = mapped_column(String(64))
    result_schema_sha256: Mapped[str] = mapped_column(String(64))
    thread_id: Mapped[str] = mapped_column(String(100), unique=True)
    seed_json: Mapped[dict] = mapped_column(JSON_TYPE)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING")
    outcome_json: Mapped[dict | None] = mapped_column(JSON_TYPE, nullable=True)
    lease_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("run_id", "operation_sha256"),)


class AgentLoopAction(Entity, Base):
    __tablename__ = "agent_loop_actions"
    loop_id: Mapped[UUID] = mapped_column(ForeignKey("agent_loops.id"), index=True)
    action_key: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    request_sha256: Mapped[str] = mapped_column(String(64))
    request_json: Mapped[dict] = mapped_column(JSON_TYPE)
    gateway_call_id: Mapped[UUID | None] = mapped_column(ForeignKey("model_gateway_calls.id"))
    result_json: Mapped[dict | None] = mapped_column(JSON_TYPE, nullable=True)
    __table_args__ = (UniqueConstraint("loop_id", "action_key"),)


class AgentLoopQuestion(Entity, Base):
    __tablename__ = "agent_loop_questions"
    loop_id: Mapped[UUID] = mapped_column(ForeignKey("agent_loops.id"), index=True)
    model_turn: Mapped[int]
    input_sha256: Mapped[str] = mapped_column(String(64))
    question_json: Mapped[dict] = mapped_column(JSON_TYPE)
    __table_args__ = (UniqueConstraint("loop_id", "model_turn"),)


class AgentLoopDecision(Entity, Base):
    __tablename__ = "agent_loop_decisions"
    question_id: Mapped[UUID] = mapped_column(ForeignKey("agent_loop_questions.id"), unique=True)
    actor_id: Mapped[str] = mapped_column(String(100))
    decision_json: Mapped[dict] = mapped_column(JSON_TYPE)


class AgentLoopStamp(Entity, Base):
    """Authorize checkpoint content without treating a checkpoint as a business fact."""

    __tablename__ = "agent_loop_stamps"
    loop_id: Mapped[UUID] = mapped_column(ForeignKey("agent_loops.id"), index=True)
    state_sha256: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("loop_id", "state_sha256"),)
