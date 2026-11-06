"""Persist critique/patch/recheck rounds before creating one immutable domain candidate."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.agents.contracts import ScriptContent
from arclight.agents.script_rewrite import RewriteRequest
from arclight.application.agent_loop_recovery import (
    run_persistent_agent_loop,
    with_initial_question,
)
from arclight.application.agent_tools import AgentToolRegistry, build_agent_tool_registry
from arclight.application.rewrite_content import content_hash
from arclight.application.screenwriter_creation import creation_attempt, creation_loops, operation
from arclight.application.screenwriter_patch import (
    deterministic_issues,
    effective_target,
    merge_checked_patch,
    patch_seed,
)
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentLoopResult, HumanDecisionRequest
from arclight.domain.screenwriter_planning import ScreenwriterPlan
from arclight.domain.screenwriter_revision import (
    PATCH_TOOL,
    REVISION_PROMPT,
    REVISION_VERSION,
    CheckPatchParameters,
    CheckPatchResult,
    CritiqueIssue,
    RevisionSeed,
    ScriptCritique,
)
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.models import WorkflowRun


def _registry(settings: Settings) -> AgentToolRegistry:
    catalog = build_agent_tool_registry(settings=settings)
    tools = [catalog.get(name) for name in ("investigate_story", PATCH_TOOL)]
    assert all(tool is not None for tool in tools)
    return AgentToolRegistry(tool for tool in tools if tool is not None)


def _patch(db: Session, loop: AgentLoop, checksum: str) -> CheckPatchParameters:
    for action in db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop.id)):
        feedback = action.result_json or {}
        if (
            action.kind != "TOOL"
            or action.request_json.get("tool_name") != PATCH_TOOL
            or feedback.get("status") != "SUCCEEDED"
        ):
            continue
        result = CheckPatchResult.model_validate(feedback["result"])
        if result.patch_sha256 == checksum and result.accepted:
            parameters = CheckPatchParameters.model_validate(action.request_json["arguments"])
            if digest(parameters.model_dump(mode="json")) == checksum:
                return parameters
    raise DomainError("SCREENWRITER_PATCH_UNCHECKED", "修订必须引用本轮成功的精确工具回执", 422)


def _validate(
    db: Session, run_id: UUID, key: str, report: ScriptCritique, settings: Settings, human: bool
) -> None:
    run = db.get(WorkflowRun, run_id)
    assert run is not None
    loop, seed = patch_seed(db, run, key)
    if report.source_sha256 != seed.source_sha256:
        raise DomainError("SCREENWRITER_CRITIQUE_SCOPE", "评价不能更换精确工作稿", 422)
    scenes = {UUID(scene.scene_id): scene for scene in seed.source_script.scenes}
    for issue in report.issues:
        scene = scenes.get(issue.scene_id) if issue.scene_id is not None else None
        if issue.scene_id is not None and scene is None:
            raise DomainError("SCREENWRITER_CRITIQUE_SCOPE", "问题场景不属于本工作稿", 422)
        if issue.line_id is not None and (
            scene is None or str(issue.line_id) not in {line.line_id for line in scene.dialogue}
        ):
            raise DomainError("SCREENWRITER_CRITIQUE_SCOPE", "问题台词不属于指定场景", 422)
    if report.decision == "ACCEPT":
        if seed.initial_rewrite_required:
            raise DomainError(
                "SCREENWRITER_REWRITE_REQUIRED", "独立改写必须先完成用户指定的修改", 422
            )
        issues = deterministic_issues(db, run, seed.source_script, settings)
        if issues:
            raise DomainError(issues[0].code, issues[0].message, 422)
    elif report.decision == "REVISE":
        assert report.patch_sha256 is not None
        parameters = _patch(db, loop, report.patch_sha256)
        merge_checked_patch(db, run, seed, parameters, settings)
    elif human:
        raise DomainError(
            "SCREENWRITER_ISSUES_UNRESOLVED", "回答后仍有问题，应继续请求澄清或停止", 409
        )


def _round(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    plan: ScreenwriterPlan,
    source: ScriptContent,
    round_no: int,
    remaining: int,
    previous: list[CritiqueIssue],
    *,
    human: bool = False,
) -> AgentLoopResult[ScriptCritique]:
    with factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert run is not None
        prefix = f"{REVISION_VERSION}:round:{round_no}:" + ("human:" if human else "critique:")
        key = prefix + f"attempt:{creation_attempt(run)}"
        candidates = [
            loop for loop in creation_loops(db, run_id) if operation(loop).startswith(prefix)
        ]
        selected = next((loop for loop in candidates if loop.status == "COMPLETED"), None)
        selected = selected or (candidates[-1] if candidates else None)
        if selected and (selected.status != "STOPPED" or operation(selected) == key):
            request = AgentModelRequest.model_validate(selected.seed_json)
            key = request.operation_key
            first = request.messages[0]
            assert isinstance(first, AgentUserMessage)
            saved = RevisionSeed.model_validate(first.data)
            if saved.source_sha256 != content_hash(source.model_dump(mode="json")):
                raise DomainError("SCREENWRITER_CRITIQUE_SCOPE", "重入不能更换已评价工作稿", 409)
        else:
            rewrite = run.input_json.get("rewrite_input")
            original = RewriteRequest.model_validate(rewrite["request"]) if rewrite else None
            target = (
                effective_target(
                    ScriptContent.model_validate(rewrite["source_script"]), source, original.target
                )
                if original and rewrite
                else None
            )
            seed = RevisionSeed(
                operation_key=key,
                round=round_no,
                remaining_patches=remaining,
                initial_rewrite_required=bool(rewrite and round_no == 0),
                source_sha256=content_hash(source.model_dump(mode="json")),
                source_script=source,
                evidence=plan.evidence,
                objective=plan.objective,
                permitted_target=target,
                instructions=original.instructions if original else None,
                deterministic_issues=deterministic_issues(db, run, source, settings),
                previous_issues=previous,
            )
            request = AgentModelRequest(
                run_id=run.id,
                agent_name="screenwriter",
                stage="rewrite" if rewrite else "script",
                operation_key=key,
                agent_version=run.workflow_version,
                prompt_version=REVISION_VERSION,
                result_schema_version=REVISION_VERSION,
                system_instruction=REVISION_PROMPT,
                messages=[AgentUserMessage(data=seed.model_dump(mode="json"))],
                max_tokens=8000,
            )
            if human:
                problems = "；".join(f"{issue.category}：{issue.problem}" for issue in previous)
                request = with_initial_question(
                    request,
                    HumanDecisionRequest(
                        reason="CONSTRAINT_CONFLICT",
                        question=(
                            "编剧需要你的决定："
                            + problems
                            + "。请澄清分歧；额度耗尽时可停止并重新发起。"
                        )[:1000],
                    ),
                )
    return run_persistent_agent_loop(
        factory,
        settings,
        request,
        _registry(settings),
        ScriptCritique,
        validate_result=lambda db, report: _validate(db, run_id, key, report, settings, human),
    )


def revise_script(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    plan: ScreenwriterPlan,
    content: ScriptContent,
) -> tuple[AgentLoopResult[ScriptCritique], ScriptContent | None]:
    """Rebuild work from exact successful receipts; retries cannot reset repair counts."""
    with factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert run is not None
        maximum = run.input_json["screenwriter"]["revision_rounds"]
        # The user's initial rewrite is distinct from subsequent repair rounds.
        maximum += int(run.workflow_type == "script_rewrite")
    previous: list[CritiqueIssue] = []
    for round_no in range(maximum + 1):
        outcome = _round(
            factory, settings, run_id, plan, content, round_no, maximum - round_no, previous
        )
        if outcome.status != "COMPLETED":
            return outcome, None
        assert outcome.result is not None
        report = outcome.result
        if report.decision == "ESCALATE":
            outcome = _round(
                factory,
                settings,
                run_id,
                plan,
                content,
                round_no,
                maximum - round_no,
                report.issues,
                human=True,
            )
            if outcome.status != "COMPLETED":
                return outcome, None
            assert outcome.result is not None
            report = outcome.result
        if report.decision == "ACCEPT":
            return outcome, content
        assert report.decision == "REVISE" and report.patch_sha256 is not None
        with factory() as db:
            run = db.get(WorkflowRun, run_id)
            loop = db.get(AgentLoop, outcome.loop_id)
            assert run is not None and loop is not None
            seed = RevisionSeed.model_validate(loop.seed_json["messages"][0]["data"])
            content = merge_checked_patch(
                db, run, seed, _patch(db, loop, report.patch_sha256), settings
            )
        previous = report.issues
    raise DomainError("SCREENWRITER_REVISION_EXHAUSTED", "修订次数已耗尽，工作稿及问题保留", 409)
