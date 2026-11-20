"""Durable narrative review intake and immutable report history on existing tables."""

from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_limits import configure_agent_run
from arclight.application.agent_runtime_state import initialize_run_state
from arclight.application.audit import audit
from arclight.application.commands import emit, require_project
from arclight.application.dependencies import lock_dependency_graph
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.model_gateway import lock_gate
from arclight.application.model_services import text_snapshot
from arclight.application.narrative_review_evidence import capture_review
from arclight.application.production_versions import (
    bind,
    capture_text,
    checked_snapshot,
    find_binding,
)
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.narrative_review import (
    NARRATIVE_REPORT_RESOURCE,
    NARRATIVE_REVIEW_VERSION,
    NARRATIVE_WORK_RESOURCE,
    NarrativeReport,
    NarrativeReviewCreate,
    NarrativeReviewWork,
)
from arclight.domain.versions import digest
from arclight.persistence.models import Episode, OutboxEvent, WorkflowRun


def receipt(**values) -> dict:
    return {
        "schema_version": 1,
        "compatibility": "EXACT_DEFINITIONS",
        "components": [],
        "parameters": {},
        "parameter_sha256": digest({}),
        **values,
    }


def report_identity(run_id: UUID) -> UUID:
    return uuid5(run_id, "narrative-report/1")


def read_report(db: Session, project_id: UUID, report_id: UUID) -> NarrativeReport:
    row = find_binding(db, project_id, NARRATIVE_REPORT_RESOURCE, report_id)
    if row is None:
        raise DomainError("NOT_FOUND", "叙事审查报告不存在", 404)
    data = checked_snapshot(db, row)
    report = NarrativeReport.model_validate(data["narrative_report"])
    if (
        report.project_id != project_id
        or report.report_id != report_id
        or data["report_sha256"] != digest(report.model_dump(mode="json"))
    ):
        raise DomainError("REVIEW_SCOPE_INVALID", "叙事报告的精确身份不一致", 409)
    return report


def read_work(db: Session, project_id: UUID, run_id: UUID) -> NarrativeReviewWork:
    row = find_binding(db, project_id, NARRATIVE_WORK_RESOURCE, run_id)
    if row is None:
        raise DomainError("NOT_FOUND", "叙事送审记录不存在", 404)
    work = NarrativeReviewWork.model_validate(checked_snapshot(db, row)["narrative_work"])
    run = db.get(WorkflowRun, run_id, populate_existing=True)
    if (
        run is None
        or run.workflow_type != "continuity_review"
        or run.workflow_version != NARRATIVE_REVIEW_VERSION
        or work.project_id != project_id
        or work.run_id != run_id
        or run.input_json.get("narrative_input_sha256") != digest(work.model_dump(mode="json"))
        or run.input_json.get("narrative_review") != work.request.model_dump(mode="json")
    ):
        raise DomainError("REVIEW_SCOPE_INVALID", "叙事工作流与冻结输入不一致", 409)
    return work


def current_work(db: Session, project_id: UUID, run_id: UUID) -> NarrativeReviewWork:
    work = read_work(db, project_id, run_id)
    run = db.get(WorkflowRun, run_id)
    assert run is not None
    if successor(db, project_id, report_identity(run_id)) is not None:
        raise DomainError("REVIEW_INPUT_CHANGED", "该报告已有另存的后续重审，旧报告仅保留历史", 409)
    validate_publication(db, run_id)
    current = capture_review(db, project_id, run_id, execution_actor(db, run), work.request)
    if current != work:
        raise DomainError(
            "REVIEW_INPUT_CHANGED", "叙事审查的精确输入已经变化，请另存改稿并重新送审", 409
        )
    return work


def successor(db: Session, project_id: UUID, report_id: UUID) -> UUID | None:
    return db.scalar(
        select(WorkflowRun.id)
        .join(Episode, Episode.id == WorkflowRun.episode_id)
        .where(
            Episode.project_id == project_id,
            WorkflowRun.workflow_version == NARRATIVE_REVIEW_VERSION,
            WorkflowRun.input_json["narrative_review"]["previous_report_id"].as_string()
            == str(report_id),
        )
        .limit(1)
    )


def start_review(
    db: Session, settings: Settings, project_id: UUID, actor: str, payload: NarrativeReviewCreate
) -> WorkflowRun:
    lock_gate(db)
    require_project(db, project_id, actor, write=True)
    lock_dependency_graph(db, project_id)
    identity = uuid5(project_id, "narrative-review:" + str(payload.creation_key))
    fingerprint = digest({"actor": actor, "request": payload.model_dump(mode="json")})
    previous = db.get(WorkflowRun, identity, populate_existing=True)
    if previous is not None:
        if (
            previous.request_fingerprint != fingerprint
            or previous.workflow_version != NARRATIVE_REVIEW_VERSION
        ):
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一叙事送审键不能更换输入或限制", 409)
        read_work(db, project_id, identity)
        return previous
    parent = (
        read_report(db, project_id, payload.previous_report_id)
        if payload.previous_report_id
        else None
    )
    work = capture_review(db, project_id, identity, actor, payload)
    scenes = [item.location for item in work.evidence if item.kind == "SCENE"]
    if parent is not None:
        if successor(db, project_id, parent.report_id) is not None:
            raise DomainError(
                "NARRATIVE_REREVIEW_CONFLICT", "原报告已有后续重审，请沿最新报告继续", 409
            )
        original = read_work(db, project_id, parent.run_id)
        if (
            original.request.scope != payload.scope
            or (
                payload.scope == "SCENE"
                and original.request.targets[0].scene_ids != payload.targets[0].scene_ids
            )
            or {
                item.location.episode_id
                for item in original.evidence
                if item.kind == "SCENE" and item.location
            }
            != {item.episode_id for item in scenes if item}
        ):
            raise DomainError(
                "NARRATIVE_REREVIEW_SCOPE_INVALID", "重审须保留原审查范围、剧集及场景身份", 409
            )
    anchor = scenes[0]
    assert anchor is not None
    run = WorkflowRun(
        id=identity,
        episode_id=anchor.episode_id,
        workflow_type="continuity_review",
        workflow_version=NARRATIVE_REVIEW_VERSION,
        trace_id="narrative:" + str(identity),
        idempotency_key="narrative:" + str(payload.creation_key),
        request_fingerprint=fingerprint,
        input_json={
            "created_by": actor,
            "narrative_review": payload.model_dump(mode="json"),
            "narrative_input_sha256": digest(work.model_dump(mode="json")),
        },
    )
    snapshot = text_snapshot(db, actor)
    if snapshot:
        run.input_json = run.input_json | {"_text_service": snapshot}
    db.add(run)
    db.flush()
    initialize_run_state(run, project_id)
    bind(
        db,
        project_id,
        NARRATIVE_WORK_RESOURCE,
        run.id,
        receipt(narrative_work=work.model_dump(mode="json")),
    )
    configure_agent_run(
        db, run.id, actor, payload.agent_limits, parent_run_id=parent.run_id if parent else None
    )
    capture_text(db, project_id, run, settings, actor=actor)
    db.add(OutboxEvent(run_id=run.id, revision=run.revision))
    audit(db, project_id, actor, "narrative_review.started", run.id, {"scope": payload.scope})
    emit(db, run, "workflow.queued", {"revision": run.revision, "scope": payload.scope})
    return run
