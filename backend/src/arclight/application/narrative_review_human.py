"""Separate human content decisions, never automatic media or series completion."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.commands import emit
from arclight.application.dependencies import lock_dependency_graph
from arclight.application.human_reviews import ReviewHandler, require_review_actor
from arclight.application.narrative_review import (
    current_work,
    read_report,
    read_work,
    report_identity,
)
from arclight.core.errors import DomainError
from arclight.domain.agent_state import restore_agent_state, transition_agent_step
from arclight.domain.human_reviews import HumanDecision, ReviewSnapshot
from arclight.domain.narrative_review import (
    NARRATIVE_HUMAN_HANDLER,
    NARRATIVE_REVIEW_VERSION,
    NarrativeReport,
    NarrativeReviewWork,
)
from arclight.domain.versions import digest
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import WorkflowRun


def snapshot(
    run: WorkflowRun, work: NarrativeReviewWork, report: NarrativeReport
) -> ReviewSnapshot:
    identities = {target.script_version_id for target in work.request.targets}
    identities.add(work.request.baseline.series_plan_version_id)
    if work.request.baseline.ledger_version_id:
        identities.add(work.request.baseline.ledger_version_id)
    for target in work.request.targets:
        identities.update(target.audio_version_ids)
        if target.storyboard_version_id:
            identities.add(target.storyboard_version_id)
    return ReviewSnapshot(
        project_id=work.project_id,
        episode_id=run.episode_id,
        run_id=run.id,
        thread_id=str(run.id),
        kind="NARRATIVE",
        object_id=report.report_id,
        version_id=report.report_id,
        content_sha256=digest(report.model_dump(mode="json")),
        input_sha256=report.input_sha256,
        source_version_ids=sorted(identities, key=str),
        expected_revision=run.revision,
    )


def read_snapshot(db: Session, frozen: ReviewSnapshot) -> ReviewSnapshot:
    report = read_report(db, frozen.project_id, frozen.object_id)
    work = current_work(db, frozen.project_id, report.run_id)
    run = db.get(WorkflowRun, report.run_id)
    assert run is not None
    if report.input_sha256 != digest(work.model_dump(mode="json")) or run.status not in {
        "RUNNING",
        "WAITING_REVIEW",
    }:
        raise DomainError("REVIEW_INPUT_CHANGED", "叙事审查报告不再属于当前待审输入", 409)
    return snapshot(run, work, report)


def check_decision(
    db: Session, request: HumanReviewRequest, decision: HumanDecision, actor: str
) -> None:
    lock_dependency_graph(db, request.project_id)
    report = read_report(db, request.project_id, UUID(request.snapshot_json["object_id"]))
    work = current_work(db, request.project_id, report.run_id)
    require_review_actor(db, work.project_id, actor, owner=True)
    if not decision.comment.strip() or decision.performance_intent is not None:
        raise DomainError("REVIEW_DECISION_INVALID", "请记录连续观片和内容核对的处理理由", 422)
    if decision.decision == "REJECT":
        return
    checks = report.assessment.checks if report.assessment else []
    if any(issue.severity == "BLOCKER" for check in checks for issue in check.issues):
        raise DomainError(
            "NARRATIVE_REVISION_REQUIRED", "阻塞叙事问题须改稿并重审，不能静默批准", 409
        )
    answer = decision.narrative_content
    episodes = {
        item.location.episode_id for item in work.evidence if item.kind == "SCENE" and item.location
    }
    warnings = [issue for check in checks for issue in check.issues if issue.severity == "WARNING"]
    if (
        answer is None
        or answer.report_id != report.report_id
        or answer.input_sha256 != report.input_sha256
        or set(answer.episode_ids) != episodes
        or len(answer.limitation_resolutions) != len(report.limitations)
        or len(answer.warning_resolutions) != len(warnings)
    ):
        raise DomainError(
            "NARRATIVE_VIEWING_REQUIRED",
            "须确认全部送审剧集连续观片，并逐项记录证据限制及警告的处理",
            422,
        )


def apply_decision(
    db: Session, request: HumanReviewRequest, decision: HumanReviewDecision, actor: str
) -> dict:
    answer = HumanDecision.model_validate(decision.decision_json)
    check_decision(db, request, answer, actor)
    assert request.run_id is not None
    run = db.get(WorkflowRun, request.run_id, populate_existing=True, with_for_update=True)
    assert run is not None
    state = restore_agent_state(run.agent_state_json, None, scope_for_run(run, request.project_id))
    approved = answer.decision == "APPROVE"
    run.agent_state_json = transition_agent_step(
        state,
        scope_for_run(run, request.project_id),
        expected_revision=state.revision,
        target="SUCCEEDED" if approved else "REJECTED",
        step_id="storyboard",
    ).model_dump(mode="json")
    run.status, run.pending_review = ("SUCCEEDED" if approved else "REJECTED"), None
    run.revision += 1
    emit(
        db,
        run,
        "narrative_review.approved" if approved else "narrative_review.rejected",
        {"report_id": str(request.snapshot_json["object_id"]), "decision_id": str(decision.id)},
    )
    return {"status": "APPROVED" if approved else "REJECTED", "revision": run.revision}


NARRATIVE_HANDLERS = {
    NARRATIVE_HUMAN_HANDLER: ReviewHandler(read_snapshot, apply_decision, check_decision)
}


def completed_review_parent(db: Session, parent: WorkflowRun, child: WorkflowRun) -> bool:
    """Only a receipted narrative re-review may spend against a finished ancestor.

    This is not permission to execute the ancestor again. The shared limiter still
    checks the actor, exact input hash, root policy, deadline, depth and all usage.
    """
    if (
        parent.workflow_type != "continuity_review"
        or child.workflow_type != "continuity_review"
        or parent.workflow_version != NARRATIVE_REVIEW_VERSION
        or child.workflow_version != NARRATIVE_REVIEW_VERSION
        or parent.status not in {"SUCCEEDED", "REJECTED"}
    ):
        return False
    identity = report_identity(parent.id)
    if child.input_json.get("narrative_review", {}).get("previous_report_id") != str(identity):
        return False
    request = db.scalar(
        select(HumanReviewRequest).where(
            HumanReviewRequest.run_id == parent.id,
            HumanReviewRequest.handler == NARRATIVE_HUMAN_HANDLER,
        )
    )
    if request is None:
        return False
    decision = db.scalar(
        select(HumanReviewDecision).where(HumanReviewDecision.request_id == request.id)
    )
    resume = db.scalar(select(HumanReviewResume).where(HumanReviewResume.request_id == request.id))
    if decision is None or resume is None or resume.decision_id != decision.id:
        return False
    original = read_work(db, request.project_id, parent.id)
    successor = read_work(db, request.project_id, child.id)
    report = read_report(db, request.project_id, identity)
    expected_status = "APPROVED" if parent.status == "SUCCEEDED" else "REJECTED"
    expected_decision = "APPROVE" if parent.status == "SUCCEEDED" else "REJECT"
    if (
        request.snapshot_json.get("object_id") != str(identity)
        or request.snapshot_json.get("version_id") != str(identity)
        or request.snapshot_json.get("run_id") != str(parent.id)
        or request.snapshot_json.get("input_sha256") != report.input_sha256
        or request.snapshot_json.get("content_sha256") != digest(report.model_dump(mode="json"))
        or request.request_sha256 != digest(request.snapshot_json)
        or decision.decision_json.get("request_sha256") != request.request_sha256
        or decision.decision_json.get("decision") != expected_decision
        or resume.result_json.get("status") != expected_status
        or resume.result_json.get("run_id") != str(parent.id)
        or resume.result_json.get("version_id") != str(identity)
    ):
        return False
    old_episodes = {
        item.location.episode_id
        for item in original.evidence
        if item.kind == "SCENE" and item.location
    }
    new_episodes = {
        item.location.episode_id
        for item in successor.evidence
        if item.kind == "SCENE" and item.location
    }
    return (
        original.request.scope == successor.request.scope
        and old_episodes == new_episodes
        and (
            original.request.scope != "SCENE"
            or original.request.targets[0].scene_ids == successor.request.targets[0].scene_ids
        )
    )
