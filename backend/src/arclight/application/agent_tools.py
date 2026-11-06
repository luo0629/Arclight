"""Server-side registration, authorization, and idempotent execution of Agent tools."""

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import null, select
from sqlalchemy.orm import Session

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.commands import emit, require_project
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_state import (
    AgentStateError,
    AgentStepId,
    record_agent_tool_invocation,
    state_for_scope,
)
from arclight.domain.agent_tools import (
    AgentName,
    AgentToolCall,
    AgentToolContract,
    AgentToolError,
    AgentToolFeedback,
    AgentToolGrantRequest,
    ReadStoryEntryParameters,
    ReadStoryEntryResult,
    RetrieveEvidenceParameters,
    RetrieveEvidenceResult,
    validation_fields,
)
from arclight.domain.enums import RunStatus
from arclight.persistence.models import (
    AgentToolGrant,
    AgentToolInvocation,
    Episode,
    StoryEntry,
    User,
    WorkflowRun,
)


@dataclass(frozen=True, slots=True)
class AgentToolContext:
    """Trusted scope resolved from persisted run state, never from model arguments."""

    db: Session
    run_id: UUID
    project_id: UUID
    episode_id: UUID
    actor_id: str
    agent_name: AgentName


@dataclass(frozen=True, slots=True)
class AgentToolDefinition:
    name: str
    allowed_agents: frozenset[AgentName]
    allowed_steps: frozenset[AgentStepId]
    parameters_model: type[AgentToolContract]
    result_model: type[AgentToolContract]
    scope_validator: Callable[[AgentToolContext, AgentToolContract], None]
    handler: Callable[[AgentToolContext, AgentToolContract], Any]
    requires_write: bool = False


class AgentToolRegistry:
    """Immutable-by-convention server catalog; model output can only select a name."""

    def __init__(self, tools: Iterable[AgentToolDefinition] = ()):
        self._tools: dict[str, AgentToolDefinition] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: AgentToolDefinition) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", tool.name):
            raise ValueError("Agent tool names must be stable lowercase identifiers")
        if not tool.allowed_agents:
            raise ValueError("Agent tools require an explicit Agent allowlist")
        if not tool.allowed_steps:
            raise ValueError("Agent tools require an explicit active-step allowlist")
        if tool.name in self._tools:
            raise ValueError(f"Duplicate Agent tool registration: {tool.name}")
        if not issubclass(tool.parameters_model, AgentToolContract):
            raise TypeError("Agent tool parameters must use a Pydantic AgentToolContract")
        if not issubclass(tool.result_model, AgentToolContract):
            raise TypeError("Agent tool results must use a Pydantic AgentToolContract")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AgentToolDefinition | None:
        return self._tools.get(name)


def available_agent_tools(
    db: Session,
    registry: AgentToolRegistry,
    run_id: UUID,
    agent_name: AgentName,
    actor_id: str,
    stage: str,
) -> tuple[AgentToolDefinition, ...]:
    """Select declarations using the same live grant, scope and step as execution."""
    run, episode = _locked_run(db, run_id)
    _require_enabled_actor(db, actor_id)
    require_project(db, episode.project_id, actor_id, write=True)
    if run.status != RunStatus.RUNNING or run.agent_state_json is None:
        raise DomainError("WORKFLOW_NOT_ACTIVE", "Agent 当前没有活动步骤", 409)
    try:
        scope = scope_for_run(run, episode.project_id)
        state = state_for_scope(run.agent_state_json, scope)
    except AgentStateError:
        raise DomainError("RUN_STATE_INVALID", "工作流运行状态无效", 409) from None
    if state.status != "RUNNING" or [
        step.step_id for step in state.steps if step.status == "RUNNING"
    ] != [stage]:
        raise DomainError("WORKFLOW_NOT_ACTIVE", "模型请求不属于当前 Agent 步骤", 409)
    grant = db.scalar(
        select(AgentToolGrant).where(
            AgentToolGrant.run_id == run.id, AgentToolGrant.agent_name == agent_name
        )
    )
    if grant is None:
        return ()
    if grant.project_id != episode.project_id or grant.actor_id != actor_id:
        raise DomainError("AGENT_TOOL_GRANT_FORBIDDEN", "工具授权不属于当前调用者", 403)
    return tuple(
        tool
        for name in sorted(grant.allowed_tools_json)
        if (tool := registry.get(name)) is not None
        and agent_name in tool.allowed_agents
        and stage in tool.allowed_steps
    )


def _locked_run(db: Session, run_id: UUID) -> tuple[WorkflowRun, Episode]:
    run = db.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise DomainError("NOT_FOUND", "工作流不存在", 404)
    episode = db.get(Episode, run.episode_id, populate_existing=True)
    if episode is None:
        raise DomainError("NOT_FOUND", "工作流作用范围不存在", 404)
    return run, episode


def _require_enabled_actor(db: Session, actor_id: str) -> None:
    if actor_id == "service-owner":
        return
    try:
        user_id = UUID(actor_id)
    except ValueError as exc:
        raise DomainError("FORBIDDEN", "工具授权用户无效", 403) from exc
    user = db.get(User, user_id, populate_existing=True)
    if user is None or user.disabled:
        raise DomainError("FORBIDDEN", "工具授权用户不可用", 403)


def grant_agent_tools(
    db: Session,
    request: AgentToolGrantRequest,
    actor_id: str,
    registry: AgentToolRegistry,
) -> AgentToolGrant:
    """Persist the user's explicit per-run allowlist after current project authorization."""
    run, episode = _locked_run(db, request.run_id)
    if run.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
        raise DomainError("WORKFLOW_NOT_ACTIVE", "只能为活动工作流授权工具", 409)
    _require_enabled_actor(db, actor_id)
    require_project(db, episode.project_id, actor_id, write=True)

    for tool_name in request.allowed_tools:
        tool = registry.get(tool_name)
        if tool is None:
            raise DomainError("AGENT_TOOL_UNKNOWN", "授权列表包含未注册工具", 422)
        if request.agent_name not in tool.allowed_agents:
            raise DomainError("AGENT_TOOL_AGENT_SCOPE", "该工具不属于当前 Agent", 403)

    grant = db.scalar(
        select(AgentToolGrant)
        .where(
            AgentToolGrant.run_id == run.id,
            AgentToolGrant.agent_name == request.agent_name,
        )
        .with_for_update()
    )
    allowed_tools = sorted(request.allowed_tools)
    if grant is None:
        grant = AgentToolGrant(
            run_id=run.id,
            project_id=episode.project_id,
            agent_name=request.agent_name,
            actor_id=actor_id,
            allowed_tools_json=allowed_tools,
            revision=0,
        )
        db.add(grant)
        db.flush()
        return grant
    if grant.actor_id != actor_id or grant.project_id != episode.project_id:
        raise DomainError("AGENT_TOOL_GRANT_FORBIDDEN", "当前用户不能修改此工具授权", 403)
    if grant.allowed_tools_json != allowed_tools:
        grant.allowed_tools_json = allowed_tools
        grant.revision += 1
        db.flush()
    return grant


def _fingerprint(
    request: AgentToolCall, project_id: UUID, actor_id: str, agent_name: AgentName
) -> str:
    canonical = json.dumps(
        {
            "run_id": str(request.run_id),
            "project_id": str(project_id),
            "actor_id": actor_id,
            "agent_name": agent_name,
            "tool_name": request.tool_name,
            "arguments": request.arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _feedback(
    request: AgentToolCall,
    status: Literal["SUCCEEDED", "FAILED"],
    *,
    result: Any = None,
    error: AgentToolError | None = None,
) -> AgentToolFeedback:
    return AgentToolFeedback(
        run_id=request.run_id,
        tool_name=request.tool_name,
        idempotency_key=request.idempotency_key,
        status=status,
        result=result,
        error=error,
    )


def _failure(
    request: AgentToolCall,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    fields: list[str] | None = None,
) -> AgentToolFeedback:
    return _feedback(
        request,
        "FAILED",
        error=AgentToolError(code=code, message=message, retryable=retryable, fields=fields or []),
    )


def _domain_failure(request: AgentToolCall, error: DomainError) -> AgentToolFeedback:
    try:
        tool_error = AgentToolError(
            code=error.code,
            message="领域操作拒绝执行",
            retryable=error.status_code >= 500,
        )
    except ValidationError:
        return _failure(request, "EXECUTION_FAILED", "工具执行失败")
    return _feedback(request, "FAILED", error=tool_error)


def _context_error(request: AgentToolCall, error: DomainError) -> AgentToolFeedback:
    if error.code == "NOT_FOUND":
        return _failure(request, "USER_NOT_AUTHORIZED", "工作流或项目不可访问")
    if error.code in {"FORBIDDEN", "PROJECT_ARCHIVED"}:
        return _failure(request, "USER_NOT_AUTHORIZED", "当前用户无权执行此工具")
    return _domain_failure(request, error)


def execute_agent_tool(
    db: Session,
    registry: AgentToolRegistry,
    agent_name: AgentName,
    request: AgentToolCall,
    *,
    step_key: str | None = None,
) -> AgentToolFeedback:
    """Execute one server-registered tool inside the caller's DB transaction.

    The coordinator supplies ``agent_name``; it is not part of model-generated tool
    arguments. Registered handlers must use application/domain services for writes and
    must not commit the session or perform an external side effect.
    """
    from arclight.application.agent_limits import (
        check_tool_action,
        record_tool_action,
    )
    from arclight.application.model_gateway import lock_gate

    try:
        # Serialize first policy configuration against the first tool, too.
        # Keep the existing run/project order used by grants and declarations.
        lock_gate(db)
        run, episode = _locked_run(db, request.run_id)
    except DomainError as exc:
        return _context_error(request, exc)

    grant = db.scalar(
        select(AgentToolGrant)
        .where(
            AgentToolGrant.run_id == run.id,
            AgentToolGrant.agent_name == agent_name,
        )
        .with_for_update()
    )
    if grant is None or grant.project_id != episode.project_id:
        return _failure(request, "USER_NOT_AUTHORIZED", "当前用户未授权此工具")
    tool = registry.get(request.tool_name)
    if tool is None:
        return _failure(request, "UNKNOWN_TOOL", "工具未注册")
    if agent_name not in tool.allowed_agents:
        return _failure(request, "AGENT_NOT_ALLOWED", "当前 Agent 不允许使用此工具")
    if request.tool_name not in grant.allowed_tools_json:
        return _failure(request, "USER_NOT_AUTHORIZED", "当前用户未授权此工具")
    try:
        _require_enabled_actor(db, grant.actor_id)
        require_project(db, episode.project_id, grant.actor_id, write=tool.requires_write)
    except DomainError as exc:
        return _context_error(request, exc)
    fingerprint = _fingerprint(request, episode.project_id, grant.actor_id, agent_name)
    previous = db.scalar(
        select(AgentToolInvocation).where(
            AgentToolInvocation.run_id == run.id,
            AgentToolInvocation.idempotency_key == request.idempotency_key,
        )
    )
    if previous is not None:
        if (
            previous.request_fingerprint != fingerprint
            or previous.project_id != episode.project_id
            or previous.actor_id != grant.actor_id
            or previous.agent_name != agent_name
            or previous.tool_name != request.tool_name
        ):
            return _failure(
                request,
                "IDEMPOTENCY_CONFLICT",
                "相同幂等键不能用于不同工具请求",
            )
        try:
            feedback = AgentToolFeedback.model_validate(previous.feedback_json)
            if (
                feedback.run_id != request.run_id
                or feedback.tool_name != request.tool_name
                or feedback.idempotency_key != request.idempotency_key
                or feedback.status != previous.status
            ):
                return _failure(request, "INVOCATION_RECORD_INVALID", "工具调用记录无效")
            return feedback
        except ValidationError:
            return _failure(request, "INVOCATION_RECORD_INVALID", "工具调用记录无效")

    if run.status != RunStatus.RUNNING:
        return _failure(request, "WORKFLOW_NOT_ACTIVE", "工作流当前不接受工具调用")
    if run.agent_state_json is None:
        return _failure(request, "RUN_STATE_INVALID", "工作流运行状态无效")
    try:
        scope = scope_for_run(run, episode.project_id)
        agent_state = state_for_scope(run.agent_state_json, scope)
    except AgentStateError:
        return _failure(request, "RUN_STATE_INVALID", "工作流运行状态无效")
    running_steps = [step.step_id for step in agent_state.steps if step.status == "RUNNING"]
    if agent_state.status != "RUNNING" or len(running_steps) != 1:
        return _failure(request, "WORKFLOW_NOT_ACTIVE", "Agent 当前没有活动步骤")
    if running_steps[0] not in tool.allowed_steps:
        return _failure(request, "AGENT_NOT_ALLOWED", "当前 Agent 步骤不允许使用此工具")
    if len(agent_state.tool_call_refs) >= 256 or len(agent_state.observation_refs) >= 256:
        return _failure(request, "REFERENCE_LIMIT", "工作流工具引用数量已达上限")

    try:
        execution = check_tool_action(
            db,
            run.id,
            agent_name,
            running_steps[0],
            tool.name,
            grant.actor_id,
            step_key or "tool-" + hashlib.sha256(request.idempotency_key.encode()).hexdigest(),
        )
    except DomainError as exc:
        return _context_error(request, exc)

    invocation = AgentToolInvocation(
        run_id=run.id,
        project_id=episode.project_id,
        agent_name=agent_name,
        actor_id=grant.actor_id,
        tool_name=request.tool_name,
        idempotency_key=request.idempotency_key,
        request_fingerprint=fingerprint,
        status="RUNNING",
        feedback_json=null(),
    )
    db.add(invocation)
    db.flush()
    record_tool_action(db, execution, invocation.id)

    context = AgentToolContext(
        db=db,
        run_id=run.id,
        project_id=episode.project_id,
        episode_id=episode.id,
        actor_id=grant.actor_id,
        agent_name=agent_name,
    )
    try:
        with db.begin_nested():
            try:
                parameters = tool.parameters_model.model_validate(request.arguments)
            except ValidationError as exc:
                raise _ToolFailure(
                    _failure(
                        request,
                        "INVALID_ARGUMENTS",
                        "工具参数不符合合同",
                        fields=validation_fields(exc.errors(include_input=False)),
                    )
                ) from exc
            tool.scope_validator(context, parameters)
            result_value = tool.handler(context, parameters)
            try:
                result = tool.result_model.model_validate(result_value)
                result_json = result.model_dump(mode="json")
                feedback = _feedback(request, "SUCCEEDED", result=result_json)
            except ValidationError as exc:
                raise _ToolFailure(
                    _failure(
                        request,
                        "INVALID_TOOL_RESULT",
                        "工具结果不符合合同",
                        fields=validation_fields(exc.errors(include_input=False)),
                    )
                ) from exc
    except _ToolFailure as exc:
        feedback = exc.feedback
    except DomainError as exc:
        feedback = _domain_failure(request, exc)
    except Exception:
        feedback = _failure(request, "EXECUTION_FAILED", "工具执行失败")

    agent_state = record_agent_tool_invocation(agent_state, scope, invocation.id)
    run.agent_state_json = agent_state.model_dump(mode="json")
    invocation.status = feedback.status
    invocation.feedback_json = feedback.model_dump(mode="json")
    emit(
        db,
        run,
        "agent.tool.finished",
        {
            "invocation_id": str(invocation.id),
            "tool_name": request.tool_name,
            "status": feedback.status,
            "error_code": feedback.error.code if feedback.error else None,
        },
    )
    db.flush()
    return feedback


class _ToolFailure(Exception):
    def __init__(self, feedback: AgentToolFeedback):
        self.feedback = feedback
        super().__init__(feedback.error.code if feedback.error else feedback.status)


def _story_entry(context: AgentToolContext, entry_id: UUID) -> StoryEntry:
    entry = context.db.scalar(
        select(StoryEntry).where(
            StoryEntry.id == entry_id,
            StoryEntry.project_id == context.project_id,
            StoryEntry.archived.is_(False),
        )
    )
    if entry is None:
        raise DomainError("REFERENCE_SCOPE_INVALID", "引用不在当前项目范围", 404)
    return entry


def _validate_story_entry_reference(
    context: AgentToolContext, parameters: AgentToolContract
) -> None:
    assert isinstance(parameters, ReadStoryEntryParameters)
    _story_entry(context, parameters.entry_id)


def _read_story_entry(context: AgentToolContext, parameters: AgentToolContract) -> dict[str, Any]:
    assert isinstance(parameters, ReadStoryEntryParameters)
    entry = _story_entry(context, parameters.entry_id)
    return {
        "entry_id": entry.id,
        "project_id": entry.project_id,
        "kind": entry.kind,
        "name": entry.name,
        "revision": entry.revision,
        "data": entry.data_json,
    }


def build_agent_tool_registry(*, settings: Settings | None = None) -> AgentToolRegistry:
    """Reuse the catalog; configured runtimes may also append isolated retrieval evidence."""
    registry = AgentToolRegistry(
        [
            AgentToolDefinition(
                name="read_story_entry",
                allowed_agents=frozenset({"screenwriter", "director", "continuity_reviewer"}),
                allowed_steps=frozenset(
                    {"script", "script_review", "storyboard", "storyboard_review"}
                ),
                parameters_model=ReadStoryEntryParameters,
                result_model=ReadStoryEntryResult,
                scope_validator=_validate_story_entry_reference,
                handler=_read_story_entry,
            )
        ]
    )
    if settings is not None:
        from arclight.application.context_evidence import append_evidence, read_trace
        from arclight.application.director_checks import director_check_tool
        from arclight.application.director_research import investigate_production
        from arclight.application.screenwriter_patch import patch_tool
        from arclight.application.screenwriter_research import investigate_story
        from arclight.domain.context_trace import EvidenceRequest
        from arclight.domain.director_planning import (
            RESEARCH_TOOL,
            InvestigateProductionParameters,
            InvestigateProductionResult,
        )
        from arclight.domain.screenwriter_research import (
            InvestigateStoryParameters,
            InvestigateStoryResult,
        )

        registry.register(patch_tool(settings))
        registry.register(director_check_tool(settings))

        def investigate_director(context: AgentToolContext, parameters: AgentToolContract) -> dict:
            assert isinstance(parameters, InvestigateProductionParameters)
            run = context.db.get(WorkflowRun, context.run_id)
            assert run is not None
            return investigate_production(context.db, settings, run, context.actor_id, parameters)

        registry.register(
            AgentToolDefinition(
                name=RESEARCH_TOOL,
                allowed_agents=frozenset({"director"}),
                allowed_steps=frozenset({"storyboard"}),
                parameters_model=InvestigateProductionParameters,
                result_model=InvestigateProductionResult,
                scope_validator=lambda context, parameters: None,
                handler=investigate_director,
                requires_write=True,
            )
        )

        def investigate(context: AgentToolContext, parameters: AgentToolContract) -> dict:
            assert isinstance(parameters, InvestigateStoryParameters)
            run = context.db.get(WorkflowRun, context.run_id)
            assert run is not None and run.agent_state_json is not None
            state = state_for_scope(run.agent_state_json, scope_for_run(run, context.project_id))
            stage = next(step.step_id for step in state.steps if step.status == "RUNNING")
            return investigate_story(context.db, settings, run, stage, context.actor_id, parameters)

        registry.register(
            AgentToolDefinition(
                name="investigate_story",
                allowed_agents=frozenset({"screenwriter"}),
                allowed_steps=frozenset({"outline", "rewrite", "script"}),
                parameters_model=InvestigateStoryParameters,
                result_model=InvestigateStoryResult,
                scope_validator=lambda context, parameters: None,
                handler=investigate,
                requires_write=True,
            )
        )

        def retrieve(context: AgentToolContext, parameters: AgentToolContract) -> dict:
            assert isinstance(parameters, RetrieveEvidenceParameters)
            run = context.db.get(WorkflowRun, context.run_id)
            assert run is not None
            assert run.agent_state_json is not None
            state = state_for_scope(run.agent_state_json, scope_for_run(run, context.project_id))
            stage = next(step.step_id for step in state.steps if step.status == "RUNNING")
            trace = read_trace(context.db, run, stage, context.actor_id)
            manifest, _ = append_evidence(
                context.db,
                run,
                stage,
                context.actor_id,
                EvidenceRequest(
                    creation_key=uuid4(),
                    expected_round=len(trace["evidence"]),
                    **parameters.model_dump(exclude={"include_images"}),
                ),
                settings,
            )
            return {
                "manifest_id": manifest.id,
                "startup_manifest_id": UUID(trace["startup_manifest_id"]),
                "evidence_round": len(trace["evidence"]) + 1,
                "image_file_ids": [
                    UUID(source["object_ref"]["file_id"])
                    for source in manifest.snapshot_json["context"]["sources"]
                    if parameters.include_images and source.get("object_ref")
                ],
            }

        registry.register(
            AgentToolDefinition(
                name="retrieve_context_evidence",
                allowed_agents=frozenset(
                    {
                        "producer",
                        "screenwriter",
                        "director",
                        "art_director",
                        "continuity_reviewer",
                    }
                ),
                allowed_steps=frozenset({"analyze", "outline", "rewrite", "script", "storyboard"}),
                parameters_model=RetrieveEvidenceParameters,
                result_model=RetrieveEvidenceResult,
                scope_validator=lambda context, parameters: None,
                handler=retrieve,
                requires_write=True,
            )
        )
    return registry
