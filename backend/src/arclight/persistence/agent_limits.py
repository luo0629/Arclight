"""Immutable execution policy and references to existing authoritative call facts."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity, utcnow


class AgentRunLimit(Base):
    __tablename__ = "agent_run_limits"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    root_run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    parent_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("workflow_runs.id"))
    actor_id: Mapped[str] = mapped_column(String(100))
    input_sha256: Mapped[str] = mapped_column(String(64))
    depth: Mapped[int]
    limits_json: Mapped[dict] = mapped_column(JSON_TYPE)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("depth BETWEEN 0 AND 8", name="agent_limit_depth"),
        CheckConstraint(
            "(depth = 0 AND parent_run_id IS NULL AND root_run_id = run_id) OR "
            "(depth > 0 AND parent_run_id IS NOT NULL AND root_run_id <> run_id)",
            name="agent_limit_parent",
        ),
    )


class AgentAction(Entity, Base):
    __tablename__ = "agent_actions"

    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_run_limits.run_id"), index=True)
    root_run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    agent_name: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(40))
    step_key: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    tool_name: Mapped[str | None] = mapped_column(String(64))
    gateway_call_id: Mapped[UUID | None] = mapped_column(ForeignKey("model_gateway_calls.id"))
    tool_invocation_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_tool_invocations.id"))
    __table_args__ = (
        UniqueConstraint("gateway_call_id", name="uq_agent_action_gateway_call"),
        UniqueConstraint("tool_invocation_id", name="uq_agent_action_tool_invocation"),
        CheckConstraint("kind IN ('MODEL', 'TOOL')", name="agent_action_kind"),
        CheckConstraint(
            "(kind = 'MODEL' AND tool_name IS NULL) OR (kind = 'TOOL' AND tool_name IS NOT NULL)",
            name="agent_action_tool",
        ),
        CheckConstraint(
            "(gateway_call_id IS NOT NULL AND tool_invocation_id IS NULL) OR "
            "(gateway_call_id IS NULL AND tool_invocation_id IS NOT NULL AND kind = 'TOOL')",
            name="agent_action_source",
        ),
    )
