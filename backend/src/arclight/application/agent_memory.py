"""Read-only memory partitions over the existing state and immutable context evidence."""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from arclight.application.agent_memory_sources import memory_references
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.context_evidence import read_trace
from arclight.core.errors import DomainError
from arclight.domain.agent_memory import MAX_MEMORY_SOURCES, AgentMemory, WorkingMemory
from arclight.domain.agent_state import AgentStateError, state_for_scope
from arclight.domain.context import ContextLimits, check_size
from arclight.domain.versions import digest
from arclight.persistence.models import WorkflowRun


def read_agent_memory(db: Session, run: WorkflowRun, stage: str, actor: str) -> dict:
    """Return data only. Neither working drafts nor model claims can write these partitions."""
    trace = read_trace(db, run, stage, actor)
    return project_agent_memory(run, trace)


def project_agent_memory(run: WorkflowRun, trace: dict) -> dict:
    """Project an already authorized trace, also used before committing new evidence."""
    scope = scope_for_run(run, UUID(trace["task_context"]["project_id"]))
    working = WorkingMemory()
    if run.agent_state_json is not None:
        try:
            state = state_for_scope(run.agent_state_json, scope)
        except AgentStateError:
            raise DomainError("RUN_STATE_INVALID", "运行记忆与冻结输入不匹配", 409) from None
        working = WorkingMemory(
            state_revision=state.revision,
            plan=state.plan,
            steps=state.steps,
            observation_ids=state.observation_refs,
            tool_invocation_ids=state.tool_call_refs,
            drafts=state.working_drafts,
        )
    try:
        rows = []
        legacy = trace["startup"] is None
        if trace["startup"] is not None:
            initial, old = memory_references(
                trace["startup"], UUID(trace["startup_manifest_id"]), 0
            )
            rows.extend(initial)
            legacy |= old
        for evidence in trace["evidence"]:
            references, old = memory_references(
                evidence, UUID(evidence["manifest_id"]), evidence["round"]
            )
            rows.extend(references)
            legacy |= old
        receipt = trace["consumption"]
        if receipt and receipt.get("resolved_input"):
            references, old = memory_references(
                receipt["resolved_input"],
                UUID(receipt["consumption_manifest_id"]),
                len(receipt["evidence_manifest_ids"]),
            )
            rows.extend(references)
            legacy |= old
        if len(rows) > MAX_MEMORY_SOURCES:
            raise DomainError("AGENT_MEMORY_LIMIT", "必需记忆来源超过数量限额，不能裁剪", 422)
        memory = AgentMemory(
            run_id=run.id,
            project_id=scope.project_id,
            stage=trace["stage"],
            input_sha256=scope.input_sha256,
            startup_manifest_id=UUID(trace["startup_manifest_id"]),
            constraints_sha256=digest(trace["task_context"]["required"]["constraints"]),
            legacy_without_authority=legacy,
            working=working,
            domain_facts=[row for row in rows if row.category == "DOMAIN_FACT"],
            evidence=[row for row in rows if row.category == "EVIDENCE"],
            long_term_preferences=[row for row in rows if row.category == "LONG_TERM_PREFERENCE"],
        )
    except ValidationError:
        raise DomainError("AGENT_MEMORY_INVALID", "记忆分类合同无效", 409) from None
    result = {"observation": trace, "memory": memory.model_dump(mode="json")}
    check_size(result, ContextLimits.model_validate(trace["task_context"]["limits"]))
    return result
