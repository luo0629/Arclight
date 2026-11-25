"""Pause admission and accept exact rendered works, never task-count completion."""

from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_limits import action_usage, validate_agent_execution
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.commands import emit
from arclight.application.human_reviews import require_review_actor
from arclight.application.model_gateway import lock_gate
from arclight.application.producer_interaction import pending_commands, require_producer
from arclight.application.producer_series import has_series_scope, read_scope
from arclight.application.production_versions import bind, find_binding
from arclight.core.errors import DomainError
from arclight.domain.agent_state import transition_agent_step
from arclight.domain.human_reviews import HumanDecision
from arclight.domain.narrative_review import NARRATIVE_HUMAN_HANDLER
from arclight.domain.producer_series import SeriesCompletion, SeriesControl
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.base import utcnow
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import OutboxEvent, WorkflowRun
from arclight.persistence.versions import VersionBinding

CONTROL_KIND = "PRODUCER_SERIES_CONTROL"
COMPLETION_KIND = "PRODUCER_SERIES_COMPLETION"


def paused(db: Session, run: WorkflowRun) -> bool:
    if not has_series_scope(run):
        return False
    # Revision, rather than timestamps, orders commands even with an injected clock.
    rows = db.scalars(
        select(VersionBinding).where(
            VersionBinding.resource_type == CONTROL_KIND,
            VersionBinding.snapshot_json["run_id"].as_string() == str(run.id),
        )
    )
    latest = max(rows, key=lambda item: item.snapshot_json["revision"], default=None)
    return bool(latest and latest.snapshot_json["payload"]["action"] == "PAUSE")


def control_series(
    db: Session, run_id: UUID, actor: str, payload: SeriesControl, key: str
) -> WorkflowRun:
    lock_gate(db)
    run, episode = require_producer(db, run_id, actor, active=False)
    read_scope(db, run)
    value = payload.model_dump(mode="json")
    identity = uuid5(run.id, "series-control:" + key)
    previous = find_binding(db, episode.project_id, CONTROL_KIND, identity)
    if previous:
        if (
            previous.snapshot_json["payload"] != value
            or previous.snapshot_json["actor_id"] != actor
        ):
            raise DomainError("IDEMPOTENCY_CONFLICT", "暂停恢复键不能更换参数", 409)
        return run
    if run.status != "RUNNING" or run.revision != payload.expected_revision:
        raise DomainError("REVISION_CONFLICT", "多集运行已结束或修订变化", 409)
    if pending_commands(db, run, episode.project_id):
        raise DomainError("PRODUCER_COMMAND_PENDING", "请先完成或恢复原生产命令", 409)
    from arclight.application.model_gateway import utc

    loops = db.scalars(
        select(AgentLoop)
        .join(AgentRunLimit, AgentRunLimit.run_id == AgentLoop.run_id)
        .where(
            AgentRunLimit.root_run_id == run.id,
        )
    )
    if any(
        loop.lease_token is not None
        and loop.lease_expires_at is not None
        and utc(loop.lease_expires_at) > utcnow()
        for loop in loops
    ):
        raise DomainError("AGENT_LOOP_BUSY", "请等待正在执行的原子动作返回后暂停", 409)
    if paused(db, run) == (payload.action == "PAUSE"):
        raise DomainError("PRODUCER_SERIES_CONTROL", "当前状态不接受此暂停恢复动作", 409)
    with db.begin_nested():
        run.revision += 1
        bind(
            db,
            episode.project_id,
            CONTROL_KIND,
            identity,
            {"run_id": str(run.id), "actor_id": actor, "revision": run.revision, "payload": value},
        )
        if payload.action == "RESUME":
            # A failed admission cannot leave a resume receipt, even if its caller catches it.
            validate_agent_execution(db, run.id, "producer")
            # Republish interrupted children whose broker deliveries may have expired during pause.
            waiting = (
                select(AgentLoop.id)
                .where(
                    AgentLoop.run_id == WorkflowRun.id,
                    AgentLoop.status.in_(
                        ("WAITING_HUMAN", "WAITING_EXTERNAL", "RECOVERY_REQUIRED")
                    ),
                )
                .exists()
            )
            interrupted = db.execute(
                select(WorkflowRun, OutboxEvent)
                .join(AgentRunLimit, AgentRunLimit.run_id == WorkflowRun.id)
                .join(OutboxEvent, OutboxEvent.run_id == WorkflowRun.id)
                .where(
                    AgentRunLimit.root_run_id == run.id,
                    WorkflowRun.id != run.id,
                    WorkflowRun.status == "RUNNING",
                    WorkflowRun.error_code.is_(None),
                    WorkflowRun.pending_review["resource_type"].as_string().is_(None),
                    OutboxEvent.revision == WorkflowRun.revision,
                    ~waiting,
                )
            )
            for child, delivery in interrupted:
                child.status = "QUEUED"
                delivery.published_at = None
        emit(
            db,
            run,
            "producer.series.paused" if payload.action == "PAUSE" else "producer.series.resumed",
            {"reason": payload.reason},
        )
    return run


def _content_approval(db: Session, project_id: UUID, payload: SeriesCompletion):
    from arclight.application.narrative_review import current_work, read_report
    from arclight.application.narrative_review_human import check_decision

    report = read_report(db, project_id, payload.narrative_report_id)
    work = current_work(db, project_id, report.run_id)
    run = db.get(WorkflowRun, report.run_id)
    request = db.scalar(
        select(HumanReviewRequest).where(
            HumanReviewRequest.run_id == report.run_id,
            HumanReviewRequest.handler == NARRATIVE_HUMAN_HANDLER,
        )
    )
    if run is None or run.status != "SUCCEEDED" or request is None:
        raise DomainError(
            "PRODUCER_CONTENT_APPROVAL_REQUIRED", "叙事审查须先完成独立人工内容批准", 409
        )
    decision = db.scalar(
        select(HumanReviewDecision).where(HumanReviewDecision.request_id == request.id)
    )
    resume = db.scalar(select(HumanReviewResume).where(HumanReviewResume.request_id == request.id))
    if decision is None or resume is None:
        raise DomainError("PRODUCER_CONTENT_APPROVAL_REQUIRED", "内容决定和恢复回执均不可缺少", 409)
    answer = HumanDecision.model_validate(decision.decision_json)
    if (
        request.project_id != project_id
        or request.snapshot_json.get("object_id") != str(report.report_id)
        or request.snapshot_json.get("version_id") != str(report.report_id)
        or request.snapshot_json.get("run_id") != str(run.id)
        or request.snapshot_json.get("content_sha256") != digest(report.model_dump(mode="json"))
        or request.snapshot_json.get("input_sha256") != report.input_sha256
        or report.input_sha256 != digest(work.model_dump(mode="json"))
        or request.request_sha256 != digest(request.snapshot_json)
        or answer.request_sha256 != request.request_sha256
        or answer.version_id != report.report_id
        or answer.decision != "APPROVE"
        or resume.decision_id != decision.id
        or resume.result_json.get("status") != "APPROVED"
        or resume.result_json.get("run_id") != str(run.id)
        or resume.result_json.get("version_id") != str(report.report_id)
    ):
        raise DomainError("PRODUCER_CONTENT_APPROVAL_REQUIRED", "精确内容批准与原回执不匹配", 409)
    check_decision(db, request, answer, decision.actor_id)
    require_review_actor(db, project_id, resume.actor_id, owner=True)
    return work, report, decision, resume


def completion_evidence(db: Session, scope: dict, actor: str, payload: SeriesCompletion) -> dict:
    from arclight.application.render_deliveries import read_delivery, read_delivery_report

    project_id = UUID(scope["project_id"])
    expected = {UUID(item["episode_id"]) for item in scope["episodes"]}
    ids = [item.episode_id for item in payload.deliveries]
    if set(ids) != expected or len(ids) != len(set(ids)):
        raise DomainError("PRODUCER_DELIVERY_SCOPE", "成片清单须精确覆盖本次全部剧集", 409)
    if payload.scope == "SERIES" and {item["episode_no"] for item in scope["episodes"]} != set(
        range(1, scope["target_episode_count"] + 1)
    ):
        raise DomainError(
            "PRODUCER_DELIVERY_SCOPE", "部分集只能确认批次成果，不能宣称整剧完成", 409
        )
    work, report, decision, resume = _content_approval(db, project_id, payload)
    if (
        str(work.request.baseline.series_plan_version_id)
        != scope["request"]["series_plan_version_id"]
    ):
        raise DomainError("PRODUCER_DELIVERY_SCOPE", "内容审查不是本次批准规划", 409)
    if payload.scope == "SERIES" and work.request.scope != "SERIES":
        raise DomainError("PRODUCER_CONTENT_APPROVAL_REQUIRED", "整剧完成需要全剧范围内容审查", 409)
    scripts = {item.script_version_id: item for item in work.request.targets}
    deliveries = []
    for item in payload.deliveries:
        delivery = read_delivery(db, item.episode_id, item.artifact_id, actor)
        machine = (
            read_delivery_report(db, item.episode_id, item.artifact_id, actor)
            if delivery.is_delivery and not delivery.stale
            else None
        )
        if machine is None or not machine.acceptance.is_delivery:
            raise DomainError(
                "PRODUCER_FORMAL_DELIVERY_REQUIRED",
                "需要当前有效的正式成片和机器报告，预览或旧稿不算完成",
                409,
            )
        script_ids = {
            source.asset_version_id for source in delivery.sources if source.asset_type == "SCRIPT"
        }
        if len(script_ids) != 1 or not script_ids <= scripts.keys():
            raise DomainError("PRODUCER_DELIVERY_SCOPE", "成片须引用内容批准的同一精确剧本", 409)
        target = scripts[next(iter(script_ids))]
        from arclight.persistence.models import GenerationCandidate, Script

        script = db.get(Script, target.script_version_id)
        if (
            script is None
            or script.episode_id != item.episode_id
            or (
                target.scene_ids
                and set(target.scene_ids)
                != {UUID(scene["scene_id"]) for scene in script.content_json["scenes"]}
            )
        ):
            raise DomainError("PRODUCER_DELIVERY_SCOPE", "内容批准须覆盖成片同集完整剧本", 409)
        if target.storyboard_version_id is None or target.storyboard_version_id not in {
            source.asset_version_id for source in delivery.sources
        }:
            raise DomainError("PRODUCER_DELIVERY_SCOPE", "成片与叙事审查分镜不一致", 409)
        reviewed_video_ids = {
            candidate.asset_version_id
            for identity in target.video_candidate_ids
            if (candidate := db.get(GenerationCandidate, identity)) is not None
        }
        for kind, reviewed in (
            ("VIDEO", reviewed_video_ids),
            ("AUDIO", set(target.audio_version_ids)),
        ):
            actual = {
                source.asset_version_id for source in delivery.sources if source.asset_type == kind
            }
            if not actual <= reviewed:
                raise DomainError(
                    "PRODUCER_DELIVERY_SCOPE", "成片实际视频／配音须属于同一内容批准范围", 409
                )
        deliveries.append(
            {
                "episode_id": str(item.episode_id),
                "artifact_id": str(delivery.id),
                "asset_version_id": str(delivery.asset_version_id),
                "sha256": delivery.sha256,
                "manifest_id": str(delivery.manifest_id),
                "manifest_sha256": delivery.manifest_sha256,
                "report_id": str(machine.report_id),
                "report_sha256": machine.report_sha256,
            }
        )
    return {
        "deliveries": deliveries,
        "narrative_report_id": str(report.report_id),
        "narrative_input_sha256": report.input_sha256,
        "content_decision_id": str(decision.id),
        "content_resume_id": str(resume.id),
        "whole_series_complete": payload.scope == "SERIES",
        "phase": "SERIES_COMPLETED" if payload.scope == "SERIES" else "BATCH_COMPLETED",
    }


def complete_series(
    db: Session, run_id: UUID, actor: str, payload: SeriesCompletion
) -> WorkflowRun:
    lock_gate(db)
    run, episode = require_producer(db, run_id, actor, active=False)
    scope = read_scope(db, run)
    require_review_actor(db, episode.project_id, actor, owner=True)
    previous = find_binding(db, episode.project_id, COMPLETION_KIND, run.id)
    if previous:
        if previous.snapshot_json["payload"] != payload.model_dump(mode="json"):
            raise DomainError("IDEMPOTENCY_CONFLICT", "完成清单不可覆盖，返修须另存", 409)
        return run
    if run.revision != payload.expected_revision or run.status != "RUNNING":
        raise DomainError("REVISION_CONFLICT", "多集运行已结束或修订变化", 409)
    if paused(db, run) or pending_commands(db, run, episode.project_id):
        raise DomainError("PRODUCER_COMMAND_PENDING", "请先恢复并完成原生产命令", 409)
    validate_agent_execution(db, run.id, "producer")
    children = db.scalars(
        select(WorkflowRun)
        .join(AgentRunLimit, AgentRunLimit.run_id == WorkflowRun.id)
        .where(
            AgentRunLimit.root_run_id == run.id,
            WorkflowRun.id != run.id,
        )
    )
    if (
        any(
            child.status not in {"SUCCEEDED", "FAILED", "REJECTED", "CANCELLED"}
            for child in children
        )
        or action_usage(db, run.id)["unknown_cost"]
    ):
        raise DomainError("PRODUCER_PREREQUISITE_PENDING", "活动子任务或未知费用尚未闭环", 409)
    evidence = completion_evidence(db, scope, actor, payload)
    bind(
        db,
        episode.project_id,
        COMPLETION_KIND,
        run.id,
        {
            "run_id": str(run.id),
            "actor_id": actor,
            "scope_sha256": digest(scope),
            "payload": payload.model_dump(mode="json"),
            **evidence,
        },
    )
    assert run.agent_state_json is not None
    run.agent_state_json = transition_agent_step(
        run.agent_state_json,
        scope_for_run(run, episode.project_id),
        expected_revision=run.agent_state_json["revision"],
        step_id="producer",
        target="SUCCEEDED",
    ).model_dump(mode="json")
    run.status, run.revision = "SUCCEEDED", run.revision + 1
    emit(
        db,
        run,
        "producer.series.completed",
        {"scope": payload.scope, "episode_count": len(payload.deliveries)},
    )
    return run


def public_series_progress(db: Session, run: WorkflowRun, actor: str) -> dict | None:
    if not has_series_scope(run):
        return None
    scope = read_scope(db, run)
    row = find_binding(db, UUID(scope["project_id"]), COMPLETION_KIND, run.id)
    completion, error = None, None
    if row:
        completion = row.snapshot_json
        try:
            current = completion_evidence(
                db, scope, actor, SeriesCompletion.model_validate(completion["payload"])
            )
            if any(current[key] != completion[key] for key in current):
                raise DomainError("PRODUCER_DELIVERY_SCOPE", "完成后的来源已有变化", 409)
        except DomainError as exc:
            error = exc.code
    return {
        "scope": scope,
        "scope_sha256": digest(scope),
        "paused": paused(db, run),
        "completion": completion,
        "completion_current": bool(completion and not error),
        "whole_series_complete": bool(
            completion and completion["whole_series_complete"] and not error
        ),
        "completion_blocking_code": error,
    }
