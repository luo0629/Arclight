"""Replay exact successful check receipts to reconstruct bounded local revisions."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.agents.contracts import StoryboardContent
from arclight.application.agent_loop_recovery import (
    run_persistent_agent_loop,
    with_initial_question,
)
from arclight.application.agent_tools import AgentToolRegistry, build_agent_tool_registry
from arclight.application.director_checks import check_seed, merge_patch
from arclight.application.director_creation import creation_attempt
from arclight.application.director_validation import check_work, publication_content, revision_input
from arclight.application.screenwriter_creation import creation_loops, operation
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentLoopResult, HumanDecisionRequest
from arclight.domain.director_planning import RESEARCH_TOOL, DirectorPlan
from arclight.domain.director_revision import (
    CHECK_TOOL,
    REVISION_PROMPT,
    REVISION_VERSION,
    DirectorCheckParameters,
    DirectorCheckResult,
    DirectorRevisionDecision,
    DirectorRevisionSeed,
    DirectorWorkingStoryboard,
)
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.models import WorkflowRun


def _registry(settings: Settings) -> AgentToolRegistry:
    catalog = build_agent_tool_registry(settings=settings)
    tools = [catalog.get(name) for name in (RESEARCH_TOOL, CHECK_TOOL)]
    assert all(tool is not None for tool in tools)
    return AgentToolRegistry(tool for tool in tools if tool is not None)


def checked_receipt(
    db: Session, loop: AgentLoop, checksum: str
) -> tuple[DirectorCheckParameters, DirectorCheckResult]:
    for action in db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop.id)):
        feedback = action.result_json or {}
        if (
            action.kind != "TOOL"
            or action.request_json.get("tool_name") != CHECK_TOOL
            or feedback.get("status") != "SUCCEEDED"
        ):
            continue
        result = DirectorCheckResult.model_validate(feedback["result"])
        parameters = DirectorCheckParameters.model_validate(action.request_json["arguments"])
        if result.accepted and result.check_sha256 == checksum == digest(
            parameters.model_dump(mode="json")
        ):
            return parameters, result
    raise DomainError("DIRECTOR_CHECK_UNVERIFIED", "决定须引用本轮实际成功的精确检查回执", 422)


def _validate(
    db: Session,
    run_id: UUID,
    key: str,
    report: DirectorRevisionDecision,
    settings: Settings,
    human: bool,
) -> None:
    run = db.get(WorkflowRun, run_id)
    assert run is not None
    loop, seed = check_seed(db, run, key)
    if report.source_sha256 != seed.source_sha256:
        raise DomainError("DIRECTOR_REVISION_SCOPE", "决定不能更换精确工作稿", 422)
    parameters, checked = checked_receipt(db, loop, report.check_sha256)
    work = merge_patch(seed, parameters)
    live = check_work(db, run, work, seed.plan, settings, report.check_sha256)
    if checked.content_sha256 != live.content_sha256:
        raise DomainError("DIRECTOR_CHECK_UNVERIFIED", "工具回执与应用的补丁不一致", 422)
    if report.decision == "ACCEPT":
        if parameters.patch is not None or not checked.valid or not live.valid:
            raise DomainError("DIRECTOR_CHECK_FAILED", "完整候选须通过本轮无补丁的真实检查", 422)
    elif report.decision == "REVISE":
        if parameters.patch is None:
            raise DomainError("DIRECTOR_PATCH_REQUIRED", "修订须选择实际检查过的局部补丁", 422)
    elif human or parameters.patch is not None:
        raise DomainError("DIRECTOR_ISSUES_UNRESOLVED", "人工回答后仍有问题须继续澄清或停止", 409)


def _round(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    plan: DirectorPlan,
    source: DirectorWorkingStoryboard,
    round_no: int,
    remaining: int,
    previous: str,
    *,
    human: bool = False,
) -> AgentLoopResult[DirectorRevisionDecision]:
    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        assert run is not None
        script = revision_input(db, run, plan, settings)
        prefix = f"{REVISION_VERSION}:round:{round_no}:" + ("human:" if human else "check:")
        key = prefix + f"attempt:{creation_attempt(run)}"
        candidates = [
            loop
            for loop in creation_loops(db, run_id, "director")
            if operation(loop).startswith(prefix)
        ]
        selected = next((loop for loop in candidates if loop.status == "COMPLETED"), None)
        selected = selected or (candidates[-1] if candidates else None)
        if selected and (selected.status != "STOPPED" or operation(selected) == key):
            request = AgentModelRequest.model_validate(selected.seed_json)
            key = request.operation_key
            saved = DirectorRevisionSeed.model_validate(selected.seed_json["messages"][0]["data"])
            if saved.source_sha256 != digest(source.model_dump(mode="json")):
                raise DomainError("DIRECTOR_REVISION_SCOPE", "恢复不能更换已检查工作稿", 409)
        else:
            parameters = DirectorCheckParameters(
                operation_key=key, source_sha256=digest(source.model_dump(mode="json"))
            )
            checks = check_work(
                db, run, source, plan, settings, digest(parameters.model_dump(mode="json"))
            )
            seed = DirectorRevisionSeed(
                operation_key=key,
                round=round_no,
                remaining_patches=remaining,
                source_sha256=parameters.source_sha256,
                source=source,
                script=script,
                plan=plan,
                checks=checks,
                previous_reason=previous,
            )
            request = AgentModelRequest(
                run_id=run.id,
                agent_name="director",
                stage="storyboard",
                operation_key=key,
                agent_version=run.workflow_version,
                prompt_version=REVISION_VERSION,
                result_schema_version=REVISION_VERSION,
                system_instruction=REVISION_PROMPT,
                messages=[AgentUserMessage(data=seed.model_dump(mode="json"))],
                max_tokens=8000,
            )
            if human:
                problems = "；".join(issue.message for issue in checks.issues[:5])
                request = with_initial_question(
                    request,
                    HumanDecisionRequest(
                        reason="CONSTRAINT_CONFLICT",
                        question=(
                            "导演需要你的决定："
                            + (problems or previous)
                            + "。回答须保留原输入和限额；修订耗尽时可停止并重新发起。"
                        )[:1000],
                    ),
                )
    return run_persistent_agent_loop(
        factory,
        settings,
        request,
        _registry(settings),
        DirectorRevisionDecision,
        validate_result=lambda db, report: _validate(db, run_id, key, report, settings, human),
    )


def revise_storyboard(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    plan: DirectorPlan,
    source: DirectorWorkingStoryboard,
) -> tuple[AgentLoopResult[DirectorRevisionDecision], StoryboardContent | None]:
    with factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert run is not None
        maximum = run.input_json["director"]["revision_rounds"]
    previous = ""
    for round_no in range(maximum + 1):
        with factory.begin() as db:
            run = db.get(WorkflowRun, run_id)
            assert run is not None
            checks = check_work(db, run, source, plan, settings, "0" * 64)
            human_prefix = f"{REVISION_VERSION}:round:{round_no}:human:"
            had_human = any(
                operation(loop).startswith(human_prefix)
                for loop in creation_loops(db, run_id, "director")
            )
        # The saved native question exists before a model can ignore an exhausted limit.
        exhausted = had_human or (round_no == maximum and not checks.valid)
        outcome = _round(
            factory,
            settings,
            run_id,
            plan,
            source,
            round_no,
            maximum - round_no,
            previous,
            human=exhausted,
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
                source,
                round_no,
                maximum - round_no,
                report.reason,
                human=True,
            )
            if outcome.status != "COMPLETED":
                return outcome, None
            assert outcome.result is not None
            report = outcome.result
        if report.decision == "ACCEPT":
            with factory.begin() as db:
                run = db.get(WorkflowRun, run_id)
                assert run is not None
                if not check_work(db, run, source, plan, settings, report.check_sha256).valid:
                    raise DomainError("DIRECTOR_CHECK_FAILED", "交付前工作稿仍有未解决问题", 422)
            return outcome, publication_content(source)
        with factory() as db:
            loop = db.get(AgentLoop, outcome.loop_id)
            assert loop is not None
            seed = DirectorRevisionSeed.model_validate(loop.seed_json["messages"][0]["data"])
            parameters, _ = checked_receipt(db, loop, report.check_sha256)
            source = merge_patch(seed, parameters)
        previous = report.reason
    raise DomainError("DIRECTOR_REVISION_EXHAUSTED", "导演修订次数已耗尽，工作稿和问题保留", 409)
