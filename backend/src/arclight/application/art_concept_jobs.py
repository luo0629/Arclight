"""Authoritative links from a frozen art plan to original generation jobs and fees."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.generation_accounting import costs
from arclight.core.errors import DomainError
from arclight.domain.art_concepts import CONCEPT_WORKFLOW_VERSION, ArtConceptPlan
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.generation import GenerationJob
from arclight.persistence.generation_candidates import GenerationCandidate
from arclight.persistence.models import AssetVersion, DependencyManifest, WorkflowRun


def concept_origin(db: Session, job: GenerationJob) -> dict | None:
    manifest = db.get(DependencyManifest, job.input_manifest_id)
    origin = manifest.snapshot_json.get("context", {}).get("art_concept") if manifest else None
    if not isinstance(origin, dict):
        return None
    try:
        run = db.get(WorkflowRun, UUID(origin["run_id"]))
        control = db.get(AgentRunLimit, run.id) if run else None
        frozen = run.input_json["art_direction"]["concept"] if run else None
        plan = ArtConceptPlan.model_validate(frozen["plan"]) if frozen else None
    except (KeyError, ValueError, TypeError):
        return None
    if (
        run is None
        or control is None
        or plan is None
        or run.workflow_version != CONCEPT_WORKFLOW_VERSION
        or origin.get("plan_sha256") != digest(frozen)
        or origin.get("root_run_id") != str(control.root_run_id)
        or job.episode_id != run.episode_id
        or job.project_id != control.project_id
        or job.created_by != control.actor_id
        or job.target_asset_id is not None
        or job.target_type != plan.target_type
        or job.task_type != "IMAGE"
        or job.idempotency_key != f"art-concept:{run.id}:{origin.get('round_no')}"
    ):
        return None
    return origin


def concept_jobs(db: Session, *, run_id: UUID | None = None, root_run_id: UUID | None = None):
    statement = select(GenerationJob).join(
        DependencyManifest, DependencyManifest.id == GenerationJob.input_manifest_id
    )
    context = DependencyManifest.snapshot_json["context"]["art_concept"]
    if run_id is not None:
        statement = statement.where(context["run_id"].as_string() == str(run_id))
    if root_run_id is not None:
        statement = statement.where(context["root_run_id"].as_string() == str(root_run_id))
    return [
        job
        for job in db.scalars(statement.order_by(GenerationJob.created_at, GenerationJob.id))
        if concept_origin(db, job) is not None
    ]


def require_concept_job(db: Session, run_id: UUID, job_id: UUID) -> GenerationJob:
    job = db.get(GenerationJob, job_id, populate_existing=True)
    origin = concept_origin(db, job) if job else None
    if job is None or origin is None or origin["run_id"] != str(run_id):
        raise DomainError("NOT_FOUND", "概念图任务不属于当前冻结美术运行", 404)
    return job


def validate_concept_execution(db: Session, job: GenerationJob) -> None:
    origin = concept_origin(db, job)
    if origin is None:
        manifest = db.get(DependencyManifest, job.input_manifest_id)
        if manifest and "art_concept" in manifest.snapshot_json.get("context", {}):
            raise DomainError("ART_CONCEPT_INPUT_CHANGED", "原受控概念图来源或冻结输入已失效", 409)
        return
    from arclight.application.agent_limits import validate_agent_execution
    from arclight.persistence.models import AgentToolGrant

    run_id = UUID(origin["run_id"])
    validate_agent_execution(db, run_id, "storyboard")
    grant = db.scalar(
        select(AgentToolGrant).where(
            AgentToolGrant.run_id == run_id, AgentToolGrant.agent_name == "art_director"
        )
    )
    if (
        grant is None
        or grant.actor_id != job.created_by
        or not {"request_art_concepts", "observe_art_concepts"} <= set(grant.allowed_tools_json)
    ):
        raise DomainError("USER_NOT_AUTHORIZED", "原概念图生成授权已撤销", 403)


def concept_costs(db: Session, root_run_id: UUID, *, exclude_jobs=frozenset()):
    actual = pending = Decimal("0")
    for job in concept_jobs(db, root_run_id=root_run_id):
        if job.id in exclude_jobs:
            continue
        spent, held = costs(db, job.project_id, job_id=job.id)
        actual += spent
        pending += held
    return actual, pending


def concept_round(db: Session, job: GenerationJob) -> dict:
    actual, reserved = costs(db, job.project_id, job_id=job.id)
    uncertain = job.status in {"RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW"}
    candidates = []
    for candidate in db.scalars(
        select(GenerationCandidate)
        .where(GenerationCandidate.job_id == job.id, GenerationCandidate.source == "GENERATED")
        .order_by(GenerationCandidate.ordinal)
    ):
        version = (
            db.get(AssetVersion, candidate.asset_version_id) if candidate.asset_version_id else None
        )
        candidates.append(
            {
                "ordinal": candidate.ordinal,
                "asset_id": str(version.asset_id) if version else None,
                "asset_version_id": str(version.id) if version else None,
                "status": version.status if version else None,
                "error_code": candidate.error_code,
            }
        )
    return {
        "job_id": str(job.id),
        "status": job.status,
        **(concept_origin(db, job) or {}),
        "actual_cost": None if reserved or uncertain else str(actual),
        "reserved_cost": str(reserved),
        "outcome_uncertain": uncertain,
        "candidates": candidates,
    }


def enforce_concept_reservation(db: Session, job: GenerationJob, amount: Decimal) -> None:
    origin = concept_origin(db, job)
    if origin is None:
        return
    run = db.get(WorkflowRun, UUID(origin["run_id"]))
    assert run is not None
    plan = ArtConceptPlan.model_validate(run.input_json["art_direction"]["concept"]["plan"])
    used = sum(
        (
            sum(costs(db, item.project_id, job_id=item.id))
            for item in concept_jobs(db, run_id=run.id)
        ),
        Decimal("0"),
    )
    if used + amount > plan.maximum_cost:
        raise DomainError("ART_CONCEPT_BUDGET", "概念图重试或新请求将超过原冻结总预算", 409)
    from arclight.application.agent_limits import action_usage
    from arclight.domain.agent_limits import AgentRunLimits

    root_id = UUID(origin["root_run_id"])
    root = db.get(AgentRunLimit, root_id)
    if root is None:
        raise DomainError("AGENT_LIMITS_REQUIRED", "概念图缺少根运行限制", 409)
    limit = AgentRunLimits.model_validate(root.limits_json)
    usage = action_usage(db, root_id)
    if limit.maximum_cost is not None and (
        usage["unknown_cost"]
        or usage["actual_cost"] + usage["pending_estimate"] + amount > limit.maximum_cost
    ):
        raise DomainError("AGENT_BUDGET_EXCEEDED", "概念图将超过跨调用共享总预算", 409)
