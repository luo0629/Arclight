"""Controlled concept requests and asynchronous observations over GenerationService."""

from copy import deepcopy
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_tools import AgentToolContext, AgentToolDefinition
from arclight.application.art_concept_assets import read_concept_reference
from arclight.application.art_concept_jobs import concept_jobs, concept_origin, require_concept_job
from arclight.application.context_evidence import append_resolved_evidence, read_trace, round_key
from arclight.application.context_sources import asset_sources
from arclight.application.dependencies import exact_version
from arclight.application.generation_accounting import costs
from arclight.application.generation_budget import quote_job
from arclight.application.generation_jobs import create_job
from arclight.application.visual_bible import snapshot as visual_snapshot
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.art_concepts import (
    OBSERVE_CONCEPT_TOOL,
    REQUEST_CONCEPT_TOOL,
    ArtConceptObserved,
    ArtConceptPlan,
    ArtConceptRequested,
    ObserveArtConcepts,
    RequestArtConcepts,
)
from arclight.domain.art_direction import COMPARISON_TOOL
from arclight.domain.art_research import ArtResearchMaterial
from arclight.domain.generation_requests import GenerationCreate, GenerationSpec
from arclight.domain.media_capabilities import validate_capabilities
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoopAction
from arclight.persistence.generation import GenerationAttempt, GenerationJob
from arclight.persistence.generation_candidates import GenerationCandidate, GenerationImageImport
from arclight.persistence.models import AgentToolInvocation, Project, WorkflowRun
from arclight.providers.media_capabilities import reference_count, resolve_capabilities

UNCERTAIN = frozenset({"RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW"})


def freeze_concept_plan(
    db: Session, project_id: UUID, episode_id: UUID, plan: ArtConceptPlan
) -> dict:
    capabilities = resolve_capabilities(plan.spec.provider, plan.spec.model)
    inputs = []
    for item in plan.inputs:
        version, asset = exact_version(db, project_id, item.source_asset_version_id)
        if (
            version.stale
            or version.status not in {"APPROVED", "LOCKED"}
            or asset.status == "ARCHIVED"
        ):
            raise DomainError("ASSET_NOT_USABLE", "概念图输入须为有效的精确批准版本", 409)
        inputs.append(
            {
                "asset_id": str(asset.id),
                "asset_version_id": str(version.id),
                "source_asset_version_id": str(version.id),
                "asset_type": asset.asset_type,
                "checksum": version.checksum,
                "metadata_sha256": digest(version.metadata_json),
            }
        )
    validate_capabilities(capabilities, plan.spec, "IMAGE", reference_count(inputs))
    payload = GenerationCreate(
        episode_id=episode_id,
        target_type=plan.target_type,
        task_type="IMAGE",
        inputs=plan.inputs,
        spec=plan.spec,
    )
    quote = quote_job(db, project_id, payload)
    if quote.maximum_cost > plan.maximum_cost:
        raise DomainError("ART_CONCEPT_BUDGET", "单轮可信报价已超过概念图总预算", 409)
    settings = db.info["settings"]
    project = db.get(Project, project_id)
    assert project is not None
    return {
        "plan": plan.model_dump(mode="json"),
        "inputs": inputs,
        "capabilities": capabilities.model_dump(mode="json")
        | {"task_types": sorted(capabilities.task_types)},
        "visual_bible": visual_snapshot(db, project),
        "provider_configuration": settings.image_service.public_configuration()
        if plan.spec.provider == "openai"
        else None,
        "initial_quote": quote.model_dump(mode="json"),
    }


def frozen_plan(db: Session, run_id: UUID) -> tuple[WorkflowRun, dict, ArtConceptPlan]:
    run = db.get(WorkflowRun, run_id)
    frozen = run.input_json.get("art_direction", {}).get("concept") if run else None
    if run is None or frozen is None:
        raise DomainError("ART_CONCEPT_NOT_ENABLED", "原运行未授权概念图探索", 403)
    return run, frozen, ArtConceptPlan.model_validate(frozen["plan"])


def concept_ready(db: Session, job: GenerationJob) -> bool:
    if job.status in UNCERTAIN or job.status in {"FAILED", "CANCELLED"}:
        return True
    if job.status != "SUCCEEDED":
        return False
    attempts = list(
        db.scalars(
            select(GenerationAttempt).where(
                GenerationAttempt.job_id == job.id, GenerationAttempt.status == "SUCCEEDED"
            )
        )
    )
    if not attempts:
        return True  # Legacy/invalid success is observed as a missing-media outcome.
    for attempt in attempts:
        expected = db.scalar(
            select(GenerationImageImport).where(GenerationImageImport.attempt_id == attempt.id)
        )
        registered = list(
            db.scalars(
                select(GenerationCandidate).where(
                    GenerationCandidate.attempt_id == attempt.id,
                    GenerationCandidate.source == "GENERATED",
                )
            )
        )
        if expected is not None and len(registered) < expected.output_count:
            return False
    return True


def _previous_comparison(
    db: Session, run_id: UUID, parameters: RequestArtConcepts, previous: GenerationJob
) -> UUID:
    if previous.status in UNCERTAIN or not concept_ready(db, previous):
        raise DomainError("ART_CONCEPT_UNRESOLVED", "上一轮须先沿原生成任务处理明确结果", 409)
    invocations = db.scalars(
        select(AgentToolInvocation)
        .where(
            AgentToolInvocation.run_id == run_id,
            AgentToolInvocation.tool_name == COMPARISON_TOOL,
            AgentToolInvocation.status == "SUCCEEDED",
        )
        .order_by(AgentToolInvocation.created_at.desc())
    )
    for invocation in invocations:
        feedback = invocation.feedback_json or {}
        comparison = (feedback.get("result") or {}).get("comparison", {})
        if comparison.get("comparison_sha256") != parameters.previous_comparison_sha256:
            continue
        action = db.scalar(
            select(AgentLoopAction).where(
                AgentLoopAction.kind == "TOOL",
                AgentLoopAction.request_json["run_id"].as_string() == str(run_id),
                AgentLoopAction.request_json["idempotency_key"].as_string()
                == invocation.idempotency_key,
            )
        )
        if action is None or action.result_json != feedback:
            continue
        versions = {
            str(item)
            for item in db.scalars(
                select(GenerationCandidate.asset_version_id).where(
                    GenerationCandidate.job_id == previous.id,
                    GenerationCandidate.asset_version_id.is_not(None),
                )
            )
        }
        references = action.request_json["arguments"]["draft"]["proposal"]["references"]
        if versions <= {item["asset"]["asset_version_id"] for item in references}:
            return invocation.id
    raise DomainError(
        "ART_CONCEPT_COMPARISON_REQUIRED", "修订须引用上一轮实际看图的真实比较哈希", 422
    )


def request_concepts(context: AgentToolContext, parameters: RequestArtConcepts) -> dict:
    db = context.db
    run, frozen, plan = frozen_plan(db, context.run_id)
    request_sha = digest(parameters.model_dump(mode="json"))
    jobs = concept_jobs(db, run_id=run.id)
    for job in jobs:
        origin = concept_origin(db, job)
        assert origin is not None
        if origin["round_no"] == parameters.round_no:
            if origin["request_sha256"] != request_sha:
                raise DomainError(
                    "IDEMPOTENCY_CONFLICT", "同一概念图轮次不能更换输入或修订依据", 409
                )
            return deepcopy(origin["receipt"]) | {
                "job_id": str(job.id),
                "input_manifest_id": str(job.input_manifest_id),
            }
    if parameters.round_no != len(jobs) + 1 or parameters.round_no > plan.maximum_rounds:
        raise DomainError("ART_CONCEPT_ROUND_LIMIT", "概念图轮次须连续且不能超过冻结上限", 409)
    if (len(jobs) + 1) * plan.spec.batch_size > plan.maximum_candidates:
        raise DomainError("ART_CONCEPT_CANDIDATE_LIMIT", "候选总数量已达到冻结上限", 409)
    previous_invocation_id = None
    if jobs:
        previous_invocation_id = _previous_comparison(db, run.id, parameters, jobs[-1])
    elif parameters.previous_comparison_sha256 is not None:
        raise DomainError("ART_CONCEPT_COMPARISON_REQUIRED", "首轮不能声明不存在的概念图比较", 422)
    fresh = freeze_concept_plan(db, context.project_id, context.episode_id, plan)
    if {key: value for key, value in frozen.items() if key != "initial_quote"} != {
        key: value for key, value in fresh.items() if key != "initial_quote"
    }:
        raise DomainError(
            "ART_CONCEPT_INPUT_CHANGED", "概念图能力、配置、输入或规范已变化，请新建任务", 409
        )
    spec = GenerationSpec.model_validate(
        plan.spec.model_dump(mode="json")
        | {
            "prompt": "\n".join(
                [
                    run.input_json["art_direction"]["task"]["visual_goal"],
                    *run.input_json["art_direction"]["task"]["constraints"],
                    plan.spec.prompt,
                    parameters.prompt,
                ]
            ),
            "variation": f"art:{run.id}:{parameters.round_no}",
        }
    )
    payload = GenerationCreate(
        episode_id=context.episode_id,
        target_type=plan.target_type,
        task_type="IMAGE",
        inputs=plan.inputs,
        spec=spec,
    )
    quote = quote_job(db, context.project_id, payload)
    spent = sum((sum(costs(db, context.project_id, job_id=item.id)) for item in jobs), Decimal("0"))
    if spent + quote.maximum_cost > plan.maximum_cost:
        raise DomainError("ART_CONCEPT_BUDGET", "概念图已用及预留费用将超过冻结预算", 409)
    control = db.get(AgentRunLimit, run.id)
    assert control is not None
    receipt = {
        "round_no": parameters.round_no,
        "maximum_cost": str(quote.maximum_cost),
        "candidate_count": spec.batch_size,
        "rationale": parameters.rationale,
    }
    origin = {
        "run_id": str(run.id),
        "root_run_id": str(control.root_run_id),
        "round_no": parameters.round_no,
        "plan_sha256": digest(frozen),
        "request_sha256": request_sha,
        "rationale": parameters.rationale,
        "previous_comparison_invocation_id": str(previous_invocation_id)
        if previous_invocation_id
        else None,
        "previous_comparison_sha256": parameters.previous_comparison_sha256,
        "receipt": receipt,
    }
    job = create_job(
        db,
        context.project_id,
        context.actor_id,
        f"art-concept:{run.id}:{parameters.round_no}",
        payload,
        art_concept_origin=origin,
    )
    return ArtConceptRequested.model_validate(
        {"job_id": job.id, "input_manifest_id": job.input_manifest_id, **receipt}
    ).model_dump(mode="json")


def observe_concepts(
    context: AgentToolContext, settings: Settings, parameters: ObserveArtConcepts
) -> dict:
    db = context.db
    job = require_concept_job(db, context.run_id, parameters.job_id)
    if not concept_ready(db, job):
        raise DomainError("ART_CONCEPT_PENDING", "原概念图任务尚未取得可观察结果", 409)
    run, _, _ = frozen_plan(db, context.run_id)
    trace = read_trace(db, run, "storyboard", context.actor_id)
    origin = concept_origin(db, job)
    assert origin is not None
    actual, reserved = costs(db, job.project_id, job_id=job.id)
    candidates = list(
        db.scalars(
            select(GenerationCandidate)
            .where(GenerationCandidate.job_id == job.id, GenerationCandidate.source == "GENERATED")
            .order_by(GenerationCandidate.created_at, GenerationCandidate.ordinal)
        )
    )

    def resolve(root, rounds, limits):
        identity = round_key(root.id, len(rounds) + 1)
        materials, assets, images, errors = [], [], [], []
        for candidate in candidates:
            if candidate.error_code:
                errors.append(candidate.error_code)
                continue
            assert candidate.asset_version_id is not None
            content = read_concept_reference(db, settings, run.id, candidate.asset_version_id)
            assets.append(content["asset"])
            images.append(UUID(content["image"]["file_id"]))
            materials.append(
                {
                    "manifest_id": identity,
                    "source_kind": "ASSET",
                    "source_id": candidate.asset_version_id,
                    "bound_manifest_id": identity,
                    "content_sha256": digest(content),
                    "content": content,
                }
            )
        if job.status == "SUCCEEDED" and not candidates:
            errors.append("ART_CONCEPT_MEDIA_MISSING")
        result = ArtConceptObserved(
            manifest_id=identity,
            startup_manifest_id=root.id,
            evidence_round=len(rounds) + 1,
            image_file_ids=images,
            job_id=job.id,
            round_no=origin["round_no"],
            status=job.status,
            candidate_version_ids=[
                row.asset_version_id for row in candidates if row.asset_version_id
            ],
            materials=[ArtResearchMaterial.model_validate(item) for item in materials],
            actual_cost=None if reserved or job.status in UNCERTAIN else actual,
            reserved_cost=reserved,
            outcome_uncertain=job.status in UNCERTAIN,
            errors=errors,
        ).model_dump(mode="json")
        return {
            "sources": asset_sources(db, context.project_id, assets),
            "assets": assets,
            "art_concept_observation": result,
        }

    row, _ = append_resolved_evidence(
        db,
        run,
        "storyboard",
        context.actor_id,
        creation_key=uuid4(),
        expected_round=len(trace["evidence"]),
        request_sha256=digest(parameters.model_dump(mode="json")),
        resolve=resolve,
    )
    return deepcopy(row.snapshot_json["context"]["art_concept_observation"])


def validate_concept_delivery(db: Session, run_id: UUID, draft) -> None:
    references = {item.asset.asset_version_id for item in draft.proposal.references}
    for job in concept_jobs(db, run_id=run_id):
        if job.status in UNCERTAIN or not concept_ready(db, job):
            raise DomainError("ART_CONCEPT_UNRESOLVED", "概念图结果尚未明确，须等待或人工处理", 409)
        observed = list(
            db.scalars(
                select(AgentToolInvocation).where(
                    AgentToolInvocation.run_id == run_id,
                    AgentToolInvocation.tool_name == OBSERVE_CONCEPT_TOOL,
                    AgentToolInvocation.status == "SUCCEEDED",
                )
            )
        )
        receipts = [
            ArtConceptObserved.model_validate((row.feedback_json or {})["result"])
            for row in observed
        ]
        if not any(row.job_id == job.id for row in receipts):
            raise DomainError(
                "ART_CONCEPT_OBSERVATION_REQUIRED", "交付须包含每轮原生成结果的实际观察", 422
            )
        candidates = {
            item
            for item in db.scalars(
                select(GenerationCandidate.asset_version_id).where(
                    GenerationCandidate.job_id == job.id,
                    GenerationCandidate.asset_version_id.is_not(None),
                )
            )
        }
        if not candidates <= references:
            raise DomainError(
                "ART_CONCEPT_COMPARISON_REQUIRED",
                "交付须实际比较全部可用概念候选并保留轮次依据",
                422,
            )


def concept_tools(settings: Settings) -> list[AgentToolDefinition]:
    from dataclasses import replace

    def scope(context: AgentToolContext, parameters: AgentToolContract) -> None:
        frozen_plan(context.db, context.run_id)
        if isinstance(parameters, ObserveArtConcepts):
            require_concept_job(context.db, context.run_id, parameters.job_id)

    def request(context: AgentToolContext, parameters: AgentToolContract) -> dict:
        assert isinstance(parameters, RequestArtConcepts)
        return request_concepts(context, parameters)

    def observe(context: AgentToolContext, parameters: AgentToolContract) -> dict:
        assert isinstance(parameters, ObserveArtConcepts)
        return observe_concepts(context, settings, parameters)

    def ready(context: AgentToolContext, parameters: AgentToolContract) -> bool:
        assert isinstance(parameters, ObserveArtConcepts)
        return concept_ready(
            context.db, require_concept_job(context.db, context.run_id, parameters.job_id)
        )

    request_tool = AgentToolDefinition(
        name=REQUEST_CONCEPT_TOOL,
        allowed_agents=frozenset({"art_director"}),
        allowed_steps=frozenset({"storyboard"}),
        parameters_model=RequestArtConcepts,
        result_model=ArtConceptRequested,
        scope_validator=scope,
        handler=request,
        requires_write=True,
    )
    return [
        request_tool,
        replace(
            request_tool,
            name=OBSERVE_CONCEPT_TOOL,
            parameters_model=ObserveArtConcepts,
            result_model=ArtConceptObserved,
            handler=observe,
            is_ready=ready,
        ),
    ]
