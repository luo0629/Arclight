"""Versioned, content-free runtime state for resumable Agent workflows."""

import json
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from arclight.domain.generation_requests import safe_metadata

AgentStepId = Literal[
    "analyze",
    "outline",
    "rewrite",
    "script",
    "script_review",
    "storyboard",
    "storyboard_review",
]
AgentStepStatus = Literal[
    "PENDING",
    "RUNNING",
    "WAITING_REVIEW",
    "SUCCEEDED",
    "REJECTED",
    "FAILED",
    "SKIPPED",
]
AgentRunStatus = Literal[
    "QUEUED", "RUNNING", "WAITING_REVIEW", "SUCCEEDED", "REJECTED", "FAILED", "CANCELLED"
]
AgentTerminationReason = Literal["COMPLETED", "REVIEW_REJECTED", "EXECUTION_FAILED", "CANCELLED"]
Checksum = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

PLAN_OPERATIONS: dict[AgentStepId, str] = {
    "analyze": "ANALYZE_STORY",
    "outline": "PLAN_OUTLINE",
    "rewrite": "REWRITE_SCRIPT",
    "script": "DRAFT_SCRIPT",
    "script_review": "REVIEW_SCRIPT",
    "storyboard": "DRAFT_STORYBOARD",
    "storyboard_review": "REVIEW_STORYBOARD",
}

WORKFLOW_PLANS: dict[str, tuple[AgentStepId, ...]] = {
    "story_analysis": ("analyze",),
    "story_outline": ("outline",),
    "script_rewrite": ("rewrite", "script_review"),
    "storyboard": ("storyboard", "storyboard_review"),
    "script_storyboard": ("script", "script_review", "storyboard", "storyboard_review"),
}

_ALLOWED_STEP_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"RUNNING", "WAITING_REVIEW", "SUCCEEDED", "FAILED", "SKIPPED"}),
    "RUNNING": frozenset({"WAITING_REVIEW", "SUCCEEDED", "FAILED", "SKIPPED"}),
    "WAITING_REVIEW": frozenset({"SUCCEEDED", "REJECTED", "FAILED", "SKIPPED"}),
    "SUCCEEDED": frozenset(),
    "REJECTED": frozenset(),
    "FAILED": frozenset(),
    "SKIPPED": frozenset(),
}


class AgentStateError(ValueError):
    """Invalid scope, revision, checkpoint version, or state transition."""


class AgentRunScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    project_id: UUID
    workflow_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    workflow_version: str = Field(min_length=1, max_length=32, pattern=r"^[\w./-]+$")
    input_sha256: Checksum


class AgentPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: AgentStepId
    operation: str = Field(min_length=1, max_length=40, pattern=r"^[A-Z][A-Z_]*$")


class AgentStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: AgentStepId
    status: AgentStepStatus = "PENDING"


class AgentDraftReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: AgentStepId
    artifact_id: UUID


class AgentRunState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal[1]
    run_id: UUID
    project_id: UUID
    workflow_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    workflow_version: str = Field(min_length=1, max_length=32, pattern=r"^[\w./-]+$")
    input_sha256: Checksum
    revision: int = Field(strict=True, ge=0)
    status: AgentRunStatus
    plan: list[AgentPlanStep] = Field(max_length=16)
    steps: list[AgentStep] = Field(max_length=16)
    tool_call_refs: list[UUID] = Field(default_factory=list, max_length=256)
    observation_refs: list[UUID] = Field(default_factory=list, max_length=256)
    working_drafts: list[AgentDraftReference] = Field(default_factory=list, max_length=16)
    termination_reason: AgentTerminationReason | None = None
    extensions: dict[str, dict[str, JsonValue]] = Field(default_factory=dict, max_length=16)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value):
        if type(value) is not int:
            raise ValueError("Agent state schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def consistent_state(self):
        plan_ids = [step.step_id for step in self.plan]
        step_ids = [step.step_id for step in self.steps]
        if (
            not plan_ids
            or tuple(plan_ids) != WORKFLOW_PLANS.get(self.workflow_type)
            or plan_ids != step_ids
            or len(set(plan_ids)) != len(plan_ids)
        ):
            raise ValueError("Agent plan and step identities must match and be unique")
        if any(item.operation != PLAN_OPERATIONS[item.step_id] for item in self.plan):
            raise ValueError("Agent plan operation does not match the registered step")
        if len(set(self.tool_call_refs)) != len(self.tool_call_refs):
            raise ValueError("Duplicate tool call reference")
        if len(set(self.observation_refs)) != len(self.observation_refs):
            raise ValueError("Duplicate observation reference")
        if len({(item.step_id, item.artifact_id) for item in self.working_drafts}) != len(
            self.working_drafts
        ):
            raise ValueError("Duplicate working draft reference")
        if any(item.step_id not in plan_ids for item in self.working_drafts):
            raise ValueError("Working draft must belong to the Agent plan")
        if len({item.step_id for item in self.working_drafts}) != len(self.working_drafts):
            raise ValueError("A completed step cannot change its working draft reference")
        expected_reason = {
            "SUCCEEDED": "COMPLETED",
            "REJECTED": "REVIEW_REJECTED",
            "FAILED": "EXECUTION_FAILED",
            "CANCELLED": "CANCELLED",
        }.get(self.status)
        if self.termination_reason != expected_reason:
            raise ValueError("Agent terminal status and termination reason must agree")
        if self.termination_reason == "COMPLETED" and any(
            step.status != "SUCCEEDED" for step in self.steps
        ):
            raise ValueError("Completed Agent state requires all steps to succeed")
        if self.termination_reason == "REVIEW_REJECTED" and self.status != "REJECTED":
            raise ValueError("Review rejection requires a rejected Agent state")
        if self.termination_reason == "EXECUTION_FAILED" and self.status != "FAILED":
            raise ValueError("Execution failure requires a failed Agent state")
        if self.termination_reason == "CANCELLED" and self.status != "CANCELLED":
            raise ValueError("Cancellation requires a cancelled Agent state")
        if (
            self.status == "WAITING_REVIEW"
            and sum(step.status == "WAITING_REVIEW" for step in self.steps) != 1
        ):
            raise ValueError("Waiting Agent state requires exactly one review step")
        if len(self.extensions) > 16:
            raise ValueError("Too many Agent state extensions")
        for namespace, values in self.extensions.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}\.[a-z][a-z0-9_-]{0,31}", namespace):
                raise ValueError("Agent extensions must use a registered namespace")
            safe_metadata(values)
        if len(json.dumps(self.extensions, ensure_ascii=False).encode()) > 16384:
            raise ValueError("Agent state extensions exceed the size limit")
        return self


def state_for_scope(state: AgentRunState | dict, scope: AgentRunScope) -> AgentRunState:
    try:
        parsed = state if isinstance(state, AgentRunState) else AgentRunState.model_validate(state)
    except Exception as exc:
        raise AgentStateError(
            "Agent runtime state is invalid or uses an unsupported version"
        ) from exc
    expected = (
        scope.run_id,
        scope.project_id,
        scope.workflow_type,
        scope.workflow_version,
        scope.input_sha256,
    )
    actual = (
        parsed.run_id,
        parsed.project_id,
        parsed.workflow_type,
        parsed.workflow_version,
        parsed.input_sha256,
    )
    if actual != expected:
        raise AgentStateError("Agent runtime state scope or frozen input differs from its run")
    return parsed


def initial_agent_state(scope: AgentRunScope) -> AgentRunState:
    step_ids = WORKFLOW_PLANS.get(scope.workflow_type)
    if step_ids is None:
        raise AgentStateError("Workflow type has no registered Agent state plan")
    return AgentRunState(
        schema_version=1,
        run_id=scope.run_id,
        project_id=scope.project_id,
        workflow_type=scope.workflow_type,
        workflow_version=scope.workflow_version,
        input_sha256=scope.input_sha256,
        revision=0,
        status="QUEUED",
        plan=[
            AgentPlanStep(step_id=step_id, operation=PLAN_OPERATIONS[step_id])
            for step_id in step_ids
        ],
        steps=[AgentStep(step_id=step_id) for step_id in step_ids],
    )


def _legacy_checkpoint_state(values: dict, scope: AgentRunScope) -> AgentRunState:
    """Migrate the pre-ARUN checkpoint shape (version 0) without changing its inputs."""
    if values.get("run_id") is not None and str(values["run_id"]) != str(scope.run_id):
        raise AgentStateError("Legacy checkpoint belongs to a different run")
    state = initial_agent_state(scope)
    steps = {item.step_id: item.status for item in state.steps}
    drafts: list[AgentDraftReference] = []
    review_rejected = False
    completed = False

    def set_step(step_id: AgentStepId, status: AgentStepStatus) -> None:
        if step_id in steps:
            steps[step_id] = status

    if "script" in steps and values.get("script_version_id"):
        set_step("script", "SUCCEEDED")
        drafts.append(
            AgentDraftReference(step_id="script", artifact_id=values["script_version_id"])
        )
        if "script_approved" not in values:
            set_step("script_review", "WAITING_REVIEW")
        elif values["script_approved"] is True:
            set_step("script_review", "SUCCEEDED")
            set_step("storyboard", "RUNNING")
        elif values["script_approved"] is False:
            set_step("script_review", "REJECTED")
            review_rejected = True
    if "rewrite" in steps and values.get("script_version_id"):
        set_step("rewrite", "SUCCEEDED")
        drafts.append(
            AgentDraftReference(step_id="rewrite", artifact_id=values["script_version_id"])
        )
        if "script_approved" not in values:
            set_step("script_review", "WAITING_REVIEW")
        elif values["script_approved"] is True:
            set_step("script_review", "SUCCEEDED")
            completed = True
        elif values["script_approved"] is False:
            set_step("script_review", "REJECTED")
            review_rejected = True
    if "storyboard" in steps and values.get("storyboard_version_id"):
        set_step("storyboard", "SUCCEEDED")
        drafts.append(
            AgentDraftReference(step_id="storyboard", artifact_id=values["storyboard_version_id"])
        )
        if "storyboard_approved" not in values:
            set_step("storyboard_review", "WAITING_REVIEW")
        elif values["storyboard_approved"] is True:
            set_step("storyboard_review", "SUCCEEDED")
            completed = True
        elif values["storyboard_approved"] is False:
            set_step("storyboard_review", "REJECTED")
            review_rejected = True
    if "analyze" in steps and values.get("analysis_id"):
        set_step("analyze", "SUCCEEDED")
        drafts.append(AgentDraftReference(step_id="analyze", artifact_id=values["analysis_id"]))
        completed = True
    if "outline" in steps and values.get("outline_id"):
        set_step("outline", "SUCCEEDED")
        drafts.append(AgentDraftReference(step_id="outline", artifact_id=values["outline_id"]))
        completed = True

    observations: set[UUID] = set()
    context_refs = values.get("context_refs", {})
    if isinstance(context_refs, dict):
        for trace in context_refs.values():
            if not isinstance(trace, dict):
                continue
            for key in ("startup_manifest_id", "evidence_manifest_ids"):
                candidates = trace.get(key, []) if key.endswith("_ids") else [trace.get(key)]
                if isinstance(candidates, list):
                    for candidate in candidates:
                        try:
                            if candidate:
                                observations.add(UUID(str(candidate)))
                        except (TypeError, ValueError):
                            continue

    reason: AgentTerminationReason | None = None
    status: AgentRunStatus = "RUNNING" if drafts or observations else "QUEUED"
    if review_rejected:
        status, reason = "REJECTED", "REVIEW_REJECTED"
        for step_id, step_status in tuple(steps.items()):
            if step_status in {"PENDING", "RUNNING", "WAITING_REVIEW"}:
                steps[step_id] = "SKIPPED"
    elif completed:
        status, reason = "SUCCEEDED", "COMPLETED"
    elif "WAITING_REVIEW" in steps.values():
        status = "WAITING_REVIEW"

    return AgentRunState.model_validate(
        {
            **state.model_dump(mode="json"),
            "revision": 1 if drafts or observations else 0,
            "status": status,
            "steps": [{"step_id": key, "status": value} for key, value in steps.items()],
            "working_drafts": drafts,
            "observation_refs": sorted(observations, key=str),
            "termination_reason": reason,
        }
    )


def restore_agent_state(
    stored: AgentRunState | dict | None,
    checkpoint_values: Mapping[str, Any] | None,
    scope: AgentRunScope,
) -> AgentRunState:
    candidates: list[AgentRunState] = []
    if stored is not None:
        candidates.append(state_for_scope(stored, scope))
    if checkpoint_values:
        if not isinstance(checkpoint_values, dict):
            raise AgentStateError("Agent checkpoint state must be an object")
        if "agent_state" in checkpoint_values and checkpoint_values["agent_state"] is not None:
            candidates.append(state_for_scope(checkpoint_values["agent_state"], scope))
        else:
            candidates.append(_legacy_checkpoint_state(checkpoint_values, scope))
    if not candidates:
        return initial_agent_state(scope)
    latest = max(candidates, key=lambda item: item.revision)
    for candidate in candidates:
        if candidate.revision == latest.revision and candidate != latest:
            stored_steps = tuple((step.step_id, step.status) for step in latest.steps)
            checkpoint_steps = tuple((step.step_id, step.status) for step in candidate.steps)
            raise AgentStateError(
                "Agent checkpoint and run state conflict at the same revision "
                f"({latest.revision}:{latest.status}/{stored_steps} vs "
                f"{candidate.revision}:{candidate.status}/{checkpoint_steps})"
            )
    return latest


def state_from_checkpoint(
    checkpoint_values: Mapping[str, Any], scope: AgentRunScope, seed: AgentRunState
) -> AgentRunState:
    return restore_agent_state(seed, checkpoint_values, scope)


def record_agent_tool_invocation(
    value: AgentRunState | dict, scope: AgentRunScope, invocation_id: UUID
) -> AgentRunState:
    """Bind the durable invocation and its feedback to this run as private references."""
    state = state_for_scope(value, scope)
    if state.status != "RUNNING" or not any(step.status == "RUNNING" for step in state.steps):
        raise AgentStateError("Tool invocation requires an active Agent step")
    if invocation_id in state.tool_call_refs or invocation_id in state.observation_refs:
        return state
    if len(state.tool_call_refs) >= 256 or len(state.observation_refs) >= 256:
        raise AgentStateError("Agent tool and observation reference limits are exhausted")
    return AgentRunState.model_validate(
        {
            **state.model_dump(mode="json"),
            "revision": state.revision + 1,
            "tool_call_refs": [*state.tool_call_refs, invocation_id],
            "observation_refs": [*state.observation_refs, invocation_id],
        }
    )


def context_observation_ids(state_values: Mapping[str, Any], stage: str) -> tuple[UUID, ...]:
    refs = state_values.get("context_refs", {}).get(stage)
    if not isinstance(refs, dict):
        return ()
    identities: set[UUID] = set()
    candidates = [refs.get("startup_manifest_id"), *refs.get("evidence_manifest_ids", [])]
    for candidate in candidates:
        try:
            if candidate:
                identities.add(UUID(str(candidate)))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(identities, key=str))


def transition_agent_step(
    value: AgentRunState | dict,
    scope: AgentRunScope,
    *,
    expected_revision: int,
    step_id: AgentStepId,
    target: AgentStepStatus,
    next_step_id: AgentStepId | None = None,
    next_step_status: AgentStepStatus | None = None,
    draft_id: UUID | None = None,
    observation_ids: tuple[UUID, ...] = (),
    termination_reason: AgentTerminationReason | None = None,
) -> AgentRunState:
    state = state_for_scope(value, scope)
    if expected_revision != state.revision:
        raise AgentStateError("Stale Agent state revision")
    steps = [item.model_dump() for item in state.steps]
    index = next((i for i, item in enumerate(steps) if item["step_id"] == step_id), None)
    if index is None:
        raise AgentStateError("Step does not belong to the Agent plan")
    current = steps[index]["status"]
    if target != current and target not in _ALLOWED_STEP_TRANSITIONS[current]:
        raise AgentStateError(f"Illegal Agent step transition: {current} -> {target}")
    if target != current and any(item["status"] != "SUCCEEDED" for item in steps[:index]):
        raise AgentStateError("Agent plan steps must advance in order")
    if next_step_id is not None:
        if next_step_status not in {"RUNNING", "WAITING_REVIEW"} or index + 1 >= len(steps):
            raise AgentStateError("Invalid next Agent step")
        next_step = steps[index + 1]
        if next_step["step_id"] != next_step_id:
            raise AgentStateError("Agent plan cannot skip steps")
        if next_step["status"] not in {"PENDING", next_step_status}:
            raise AgentStateError("Next Agent step already advanced")
        next_step["status"] = next_step_status
    steps[index]["status"] = target

    status: AgentRunStatus = state.status
    reason = termination_reason or state.termination_reason
    if target == "RUNNING" or next_step_status == "RUNNING":
        status = "RUNNING"
    elif target == "WAITING_REVIEW" or next_step_status == "WAITING_REVIEW":
        status = "WAITING_REVIEW"
    elif target == "REJECTED":
        status, reason = "REJECTED", reason or "REVIEW_REJECTED"
        for item in steps[index + 1 :]:
            if item["status"] in {"PENDING", "RUNNING", "WAITING_REVIEW"}:
                item["status"] = "SKIPPED"
    elif target == "FAILED":
        status, reason = "FAILED", reason or "EXECUTION_FAILED"
        for item in steps[index + 1 :]:
            if item["status"] in {"PENDING", "RUNNING", "WAITING_REVIEW"}:
                item["status"] = "SKIPPED"
    elif next_step_status == "RUNNING":
        status = "RUNNING"
    elif target == "SUCCEEDED" and all(item["status"] == "SUCCEEDED" for item in steps):
        status, reason = "SUCCEEDED", reason or "COMPLETED"

    drafts = list(state.working_drafts)
    if draft_id is not None:
        draft = AgentDraftReference(step_id=step_id, artifact_id=draft_id)
        if draft not in drafts:
            drafts.append(draft)
    observations = list(state.observation_refs)
    for identity in observation_ids:
        if identity not in observations:
            observations.append(identity)
    changes = {
        "status": status,
        "steps": steps,
        "working_drafts": drafts,
        "observation_refs": observations,
        "termination_reason": reason,
    }
    candidate = AgentRunState.model_validate({**state.model_dump(mode="json"), **changes})
    if candidate == state:
        return state
    if state.termination_reason is not None:
        raise AgentStateError("Terminal Agent state cannot advance without explicit retry")
    return candidate.model_copy(update={"revision": state.revision + 1})


def terminate_agent_state(
    value: AgentRunState | dict,
    scope: AgentRunScope,
    *,
    reason: Literal["EXECUTION_FAILED", "CANCELLED"],
    step_id: AgentStepId | None = None,
) -> AgentRunState:
    state = state_for_scope(value, scope)
    if state.status in {"SUCCEEDED", "REJECTED", "FAILED", "CANCELLED"}:
        if state.termination_reason == reason:
            return state
        raise AgentStateError("Terminal Agent state cannot be terminated again")
    steps = [item.model_dump() for item in state.steps]
    if reason == "EXECUTION_FAILED":
        index = next((i for i, item in enumerate(steps) if item["status"] == "RUNNING"), None)
        if index is None and step_id is not None:
            index = next((i for i, item in enumerate(steps) if item["step_id"] == step_id), None)
        if index is None:
            index = next((i for i, item in enumerate(steps) if item["status"] == "PENDING"), None)
        if index is not None:
            steps[index]["status"] = "FAILED"
            later = steps[index + 1 :]
        else:
            later = []
        for item in later:
            if item["status"] in {"PENDING", "RUNNING", "WAITING_REVIEW"}:
                item["status"] = "SKIPPED"
        status: AgentRunStatus = "FAILED"
    else:
        for item in steps:
            if item["status"] in {"PENDING", "RUNNING", "WAITING_REVIEW"}:
                item["status"] = "SKIPPED"
        status = "CANCELLED"
    return AgentRunState.model_validate(
        {
            **state.model_dump(mode="json"),
            "revision": state.revision + 1,
            "status": status,
            "steps": steps,
            "termination_reason": reason,
        }
    )


def retry_agent_state(value: AgentRunState | dict) -> AgentRunState:
    try:
        state = value if isinstance(value, AgentRunState) else AgentRunState.model_validate(value)
    except Exception as exc:
        raise AgentStateError("Cannot retry an invalid Agent state") from exc
    if state.status != "FAILED" or state.termination_reason != "EXECUTION_FAILED":
        raise AgentStateError("Only an execution-failed Agent state can be retried")
    steps = [item.model_dump() for item in state.steps]
    for item in steps:
        if item["status"] in {"FAILED", "SKIPPED"}:
            item["status"] = "PENDING"
    return AgentRunState.model_validate(
        {
            **state.model_dump(mode="json"),
            "revision": state.revision + 1,
            "status": "RUNNING",
            "steps": steps,
            "termination_reason": None,
        }
    )
