"""Observe current Producer facts without granting model-controlled state authority."""

from contextlib import suppress
from uuid import UUID, uuid5

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import AgentToolContext, available_agent_tools
from arclight.application.art_concept_jobs import concept_jobs, concept_origin
from arclight.application.commands import require_run
from arclight.application.costs import cost_facts
from arclight.application.gateway_accounting import latest_receipt
from arclight.application.gateway_text import execution_actor
from arclight.application.human_reviews import require_review_actor, review_blocker
from arclight.application.model_gateway import lock_gate, utc
from arclight.application.producer_delegation import (
    require_producer_parent,
    validate_producer_task,
    validate_saved_child,
)
from arclight.application.producer_plan_history import (
    ProducerPlanRecord,
    canonical_task,
    find_task_delegation,
    read_plan_record,
)
from arclight.application.producer_research import investigate_project
from arclight.application.production_events import consume_stage_event, recheck_stage_event
from arclight.application.production_versions import bind, find_binding
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentHumanDecision, HumanDecisionRequest
from arclight.domain.agent_tools import IdempotencyKey
from arclight.domain.costs import CostEntry
from arclight.domain.human_reviews import HumanDecision, ReviewSnapshot
from arclight.domain.producer_observation import (
    ObservedProducerTask,
    ProducerAgentHumanEvidence,
    ProducerFormalHumanEvidence,
    ProducerHumanEvidence,
    ProducerObservation,
    ProducerObservationRequest,
)
from arclight.domain.producer_research import (
    RESEARCH_TOOL,
    InvestigateProjectParameters,
    InvestigateProjectResult,
    ProducerOverviewQuery,
    ProducerReviewQuery,
)
from arclight.domain.production_events import StageEvent, StageReceipt
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision, AgentLoopQuestion
from arclight.persistence.base import utcnow
from arclight.persistence.generation import GenerationJob
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    Script,
    Storyboard,
    VersionBinding,
    VisualAsset,
    WorkflowRun,
)
from arclight.persistence.production_events import ProductionEvent, ProductionEventReceipt

OBSERVATION_KIND = "PRODUCER_OBSERVATION"
OBSERVATION_REQUEST_KIND = "PRODUCER_OBSERVATION_REQUEST"
_TERMINAL_FAILURE = {"FAILED", "REJECTED"}
_UNRESOLVED_MEDIA = {"RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW"}


def _root(db: Session, run_id: UUID, actor: str) -> tuple[WorkflowRun, AgentRunLimit]:
    lock_gate(db)
    root = require_run(db, run_id, actor, lock=True)
    control = require_producer_parent(db, root.id, root.episode_id, actor)
    return root, control


def _require_research(db: Session, root: WorkflowRun, actor: str, settings: Settings) -> None:
    from arclight.application.producer_planning import producer_registry

    permitted = available_agent_tools(
        db, producer_registry(settings), root.id, "producer", actor, "producer"
    )
    if RESEARCH_TOOL not in {tool.name for tool in permitted}:
        raise DomainError("USER_NOT_AUTHORIZED", "生产观察工具授权已撤销", 403)


def _base(
    db: Session,
    root: WorkflowRun,
    control: AgentRunLimit,
    actor: str,
    request: ProducerObservationRequest,
) -> ProducerPlanRecord:
    record = read_plan_record(
        db, control.project_id, request.base_plan_id, actor, require_current=True
    )
    if record.root.id != root.id or record.plan_sha256 != request.base_plan_sha256:
        raise DomainError("PRODUCER_OBSERVATION_CONFLICT", "观察不属于原生产计划", 409)
    if not set(request.requested_task_keys) <= {task.task_key for task in record.plan.tasks}:
        raise DomainError("PRODUCER_OBSERVATION_CONFLICT", "观察引用了计划外的任务", 409)
    return record


def _family(db: Session, root: WorkflowRun, child_id: UUID, actor: str) -> WorkflowRun:
    child = db.get(WorkflowRun, child_id, populate_existing=True)
    control = db.get(AgentRunLimit, child_id, populate_existing=True)
    if (
        child is None
        or child.episode_id != root.episode_id
        or control is None
        or control.root_run_id != root.id
        or execution_actor(db, child) != actor
        or control.actor_id != actor
    ):
        raise DomainError("PRODUCER_OBSERVATION_SCOPE", "观察来源不属于原生产运行", 409)
    return child


def _event(db: Session, root: WorkflowRun, control: AgentRunLimit, actor: str, event_id: UUID):
    row = db.get(ProductionEvent, event_id, populate_existing=True)
    if row is None:
        raise DomainError("NOT_FOUND", "生产阶段事件不存在", 404)
    event = StageEvent.model_validate(row.event_json)
    if (
        event.event_id != row.id
        or event.project_id != control.project_id
        or event.episode_id != root.episode_id
        or event.actor_id != actor
        or digest(row.event_json) != row.request_sha256
        or row.source_kind != event.source_kind
        or row.source_id != event.source_id
        or row.source_revision != event.source_revision
    ):
        raise DomainError("PRODUCER_OBSERVATION_SCOPE", "生产阶段事件身份或范围无效", 409)
    if event.source_kind == "TEXT":
        _family(db, root, event.source_id, actor)
    elif event.source_kind == "MEDIA":
        job = db.get(GenerationJob, event.source_id, populate_existing=True)
        origin = concept_origin(db, job) if job is not None else None
        if origin is None or origin["root_run_id"] != str(root.id):
            raise DomainError("PRODUCER_OBSERVATION_SCOPE", "媒体事件缺少原生产父链", 409)
        _family(db, root, UUID(origin["run_id"]), actor)
    else:
        from arclight.application.continuity_routing import read_routed_review

        _, route = read_routed_review(db, event.source_id, lock_run=False)
        _family(db, root, route.run_id, actor)
    return event


def _current_children(db: Session, record: ProducerPlanRecord) -> set[UUID]:
    result = set()
    for task in record.plan.tasks:
        receipt = find_task_delegation(db, record.root.id, canonical_task(record, task.task_key))
        if receipt is not None:
            result.add(receipt.child_run_id)
    return result


def _human(
    db: Session,
    root: WorkflowRun,
    control: AgentRunLimit,
    record: ProducerPlanRecord,
    decision_id: UUID | None,
) -> ProducerHumanEvidence | None:
    if decision_id is None:
        return None
    children = _current_children(db, record)
    formal = db.get(HumanReviewDecision, decision_id, populate_existing=True)
    if formal is not None:
        review = db.get(HumanReviewRequest, formal.request_id, populate_existing=True)
        if (
            review is None
            or review.project_id != control.project_id
            or review.episode_id != root.episode_id
            or review.run_id not in children
        ):
            raise DomainError("PRODUCER_OBSERVATION_SCOPE", "人工决定不属于当前计划", 409)
        source_run = _family(db, root, review.run_id, control.actor_id)
        require_review_actor(db, review.project_id, formal.actor_id, owner=True)
        frozen = ReviewSnapshot.model_validate(review.snapshot_json)
        decision = HumanDecision.model_validate(formal.decision_json)
        if (
            digest(review.snapshot_json) != review.request_sha256
            or frozen.project_id != review.project_id
            or frozen.episode_id != review.episode_id
            or frozen.run_id != review.run_id
            or decision.version_id != frozen.version_id
            or decision.request_sha256 != review.request_sha256
        ):
            raise DomainError("PRODUCER_OBSERVATION_CONFLICT", "人工决定的精确版本无效", 409)
        resumed = db.scalar(
            select(HumanReviewResume).where(HumanReviewResume.request_id == review.id)
        )
        if resumed is None:
            blocker = review_blocker(db, review)
            if blocker is not None:
                raise DomainError(blocker, "人工决定来源已经变化", 409)
        else:
            if resumed.decision_id != formal.id or resumed.actor_id != formal.actor_id:
                raise DomainError("PRODUCER_OBSERVATION_CONFLICT", "人工恢复身份无效", 409)
            # Applied text approvals no longer satisfy the original pending-review snapshot.
            # Their immutable content and every consumed input must still match exactly.
            if frozen.kind in {"SCRIPT", "STORYBOARD"}:
                content = (
                    db.get(Script, frozen.version_id, populate_existing=True)
                    if frozen.kind == "SCRIPT"
                    else db.get(Storyboard, frozen.version_id, populate_existing=True)
                )
                inputs = []
                for identity in sorted(frozen.source_version_ids):
                    source_version = db.get(AssetVersion, identity, populate_existing=True)
                    inputs.append(
                        {
                            "id": str(identity),
                            "checksum": source_version.checksum if source_version else None,
                            "status": source_version.status if source_version else None,
                            "stale": source_version.stale if source_version else None,
                            "revision": source_version.revision if source_version else None,
                        }
                    )
                if (
                    content is None
                    or content.episode_id != root.episode_id
                    or content.run_id != source_run.id
                    or digest(content.content_json) != frozen.content_sha256
                    or digest({"startup": source_run.input_json, "sources": inputs})
                    != frozen.input_sha256
                ):
                    raise DomainError(
                        "REVIEW_INPUT_CHANGED", "已决定文本的精确内容或输入已变化", 409
                    )
            for identity in [frozen.version_id, *frozen.source_version_ids]:
                version = db.get(AssetVersion, identity, populate_existing=True)
                if version is not None and (version.stale or version.status == "ARCHIVED"):
                    raise DomainError("REVIEW_INPUT_CHANGED", "人工决定引用的精确输入已失效", 409)
        return ProducerFormalHumanEvidence(
            decision_id=formal.id,
            source_run_id=source_run.id,
            actor_id=formal.actor_id,
            request_id=review.id,
            request_sha256=review.request_sha256,
            snapshot=frozen,
            decision=decision,
            resume_id=resumed.id if resumed else None,
        )
    saved = db.get(AgentLoopDecision, decision_id, populate_existing=True)
    question = db.get(AgentLoopQuestion, saved.question_id) if saved else None
    loop = db.get(AgentLoop, question.loop_id, populate_existing=True) if question else None
    if saved is None or question is None or loop is None:
        raise DomainError("NOT_FOUND", "已保存的人工决定不存在", 404)
    require_review_actor(db, control.project_id, saved.actor_id, owner=False)
    source = _family(db, root, loop.run_id, loop.actor_id)
    agent_decision = AgentHumanDecision.model_validate(saved.decision_json)
    seed = AgentModelRequest.model_validate(loop.seed_json)
    initial = seed.messages[0]
    data = initial.data if isinstance(initial, AgentUserMessage) else {}
    base = data.get("base_plan", {})
    base_id = base.get("plan_id") if isinstance(base, dict) else None
    if (
        loop.project_id != control.project_id
        or seed.run_id != loop.run_id
        or (source.id == root.id and seed.agent_name != "producer")
        or (
            source.id not in children
            and not (
                source.id == root.id
                and (loop.id == record.plan_id or base_id == str(record.plan_id))
            )
        )
        or agent_decision.question_id != question.id
        or question.input_sha256 != loop.input_sha256
        or agent_decision.input_sha256 != loop.input_sha256
        or loop.input_sha256 != scope_for_run(source, control.project_id).input_sha256
        or digest(loop.seed_json) != loop.request_sha256
    ):
        raise DomainError("PRODUCER_OBSERVATION_SCOPE", "人工回答的原计划或输入无效", 409)
    return ProducerAgentHumanEvidence(
        decision_id=saved.id,
        source_run_id=source.id,
        actor_id=saved.actor_id,
        loop_id=loop.id,
        question_id=question.id,
        input_sha256=loop.input_sha256,
        question=HumanDecisionRequest.model_validate(question.question_json),
        decision=agent_decision,
    )


def _specialist_outputs(
    db: Session,
    child: WorkflowRun,
    outcomes: list[AgentLoop],
) -> tuple[set[UUID], set[UUID], list[dict], bool]:
    identities: set[UUID] = set()
    references: set[UUID] = set()
    snapshots: list[dict] = []
    invalid = False
    if child.workflow_type == "art_proposal":
        from arclight.application.art_workflow import delivered
        from arclight.persistence.art_proposals import (
            ArtApprovedVersion,
            ArtProposalRecord,
            ArtProposalVersion,
        )

        delivery = delivered(db, child.id)
        if delivery is not None:
            proposal = db.get(ArtProposalRecord, delivery.proposal_id, populate_existing=True)
            version = db.get(ArtProposalVersion, delivery.version_id, populate_existing=True)
            assert proposal is not None and version is not None
            identities.add(version.id)
            snapshots.append(
                {
                    "delivery": delivery.model_dump(mode="json"),
                    "archived": proposal.archived,
                    "revision": proposal.revision,
                    "version_no": proposal.version_no,
                    "input_sha256": version.input_sha256,
                    "proposal_sha256": digest(version.proposal_json),
                }
            )
            invalid |= proposal.archived
            for output in db.scalars(
                select(ArtApprovedVersion)
                .where(
                    ArtApprovedVersion.proposal_version_id == version.id,
                )
                .order_by(ArtApprovedVersion.id)
            ):
                identities.add(output.id)
                snapshots.append(
                    {"approved_version_id": str(output.id), "content_sha256": output.content_sha256}
                )
                if output.asset_id is not None:
                    asset = db.get(VisualAsset, output.asset_id, populate_existing=True)
                    snapshots.append(
                        {
                            "asset_id": str(output.asset_id),
                            "status": asset.status if asset else None,
                            "revision": asset.revision if asset else None,
                        }
                    )
                    invalid |= asset is None or asset.status in {"ARCHIVED", "REJECTED", "STALE"}
                if output.visual_bible_version_id is not None:
                    identities.add(output.visual_bible_version_id)
    if child.workflow_type == "continuity_review":
        from arclight.application.continuity_review_store import require_review_record
        from arclight.application.continuity_reviewer_evidence import reviewer_loop
        from arclight.application.continuity_reviews import validate_review_contract
        from arclight.application.continuity_routing import completed_review, require_latest_review
        from arclight.application.production_versions import checked_snapshot
        from arclight.domain.continuity_reviewer import REVIEWER_VERSION
        from arclight.domain.continuity_reviews import REVIEW_SCHEMA_VERSION, REVIEW_VERSION
        from arclight.domain.continuity_workflow import REVIEW_WORK_RESOURCE, CandidateReviewWork

        # The production review's current candidate follows the same durable work
        # order used when Owner selection completes the original workflow.
        latest_work = db.scalar(
            select(VersionBinding)
            .where(
                VersionBinding.resource_type == REVIEW_WORK_RESOURCE,
                VersionBinding.snapshot_json["continuity_work"]["run_id"].as_string()
                == str(child.id),
            )
            .order_by(VersionBinding.created_at.desc(), VersionBinding.id.desc())
            .limit(1)
        )
        current_work = (
            CandidateReviewWork.model_validate(checked_snapshot(db, latest_work)["continuity_work"])
            if latest_work is not None
            else None
        )
        superseded_targets: set[UUID] = set()
        current_targets: set[UUID] = set()
        for loop in outcomes:
            seed = AgentModelRequest.model_validate(loop.seed_json)
            if (
                seed.agent_name != "continuity_reviewer"
                or not seed.operation_key.startswith(REVIEWER_VERSION + ":")
                or seed.result_schema_version not in {REVIEW_VERSION, REVIEW_SCHEMA_VERSION}
                or loop.status != "COMPLETED"
            ):
                continue
            review = completed_review(db, loop)
            original, _ = reviewer_loop(db, child.id, review.target, result_id=review.result_id)
            record = require_review_record(db, loop.project_id, review.result_id, loop.actor_id)
            if original.id != loop.id or record.review != review:
                raise DomainError("PRODUCER_OBSERVATION_CONFLICT", "审查交付与原合同不一致", 409)
            current = current_work is None or current_work.target == review.target
            error_code = None
            try:
                require_latest_review(db, record)
            except DomainError as exc:
                if exc.status_code in {401, 403}:
                    raise
                error_code = exc.code
                if exc.code == "REVIEW_RESULT_STALE":
                    if current:
                        superseded_targets.add(review.target.candidate_id)
                    current = False
                elif current:
                    invalid = True
            if current:
                current_targets.add(review.target.candidate_id)
                identities.add(review.result_id)
                references.update(reference.resource_id for reference in review.references)
                try:
                    validate_review_contract(db, review, loop.actor_id)
                except DomainError as exc:
                    if exc.status_code in {401, 403}:
                        raise
                    invalid = True
                    error_code = exc.code
            snapshots.append(
                {
                    "review_id": str(review.result_id),
                    "record_sha256": digest(record.model_dump(mode="json")),
                    "current": current,
                    "current_error_code": error_code,
                }
            )
        invalid |= bool(superseded_targets - current_targets)
    return identities, references, snapshots, invalid


def _outputs(db: Session, child: WorkflowRun) -> tuple[list[UUID], str, bool]:
    snapshots: list[dict] = []
    identities: set[UUID] = set()
    invalid = False
    text_outputs: list[Script | Storyboard] = [
        *db.scalars(select(Script).where(Script.run_id == child.id).order_by(Script.id)),
        *db.scalars(
            select(Storyboard).where(Storyboard.run_id == child.id).order_by(Storyboard.id)
        ),
    ]
    for output in text_outputs:
        identities.add(output.id)
        snapshots.append(
            {
                "id": str(output.id),
                "status": output.status,
                "content_sha256": digest(output.content_json),
            }
        )
        invalid |= output.status in {"STALE", "ARCHIVED", "REJECTED"}
    # Specialist outcomes remain in the original durable loop; hash them, then re-read
    # any exact asset versions they identify rather than trusting model status fields.
    outcomes = list(
        db.scalars(select(AgentLoop).where(AgentLoop.run_id == child.id).order_by(AgentLoop.id))
    )
    specialist_ids, references, specialist_snapshots, specialist_invalid = _specialist_outputs(
        db, child, outcomes
    )
    identities.update(specialist_ids)
    snapshots.extend(specialist_snapshots)
    invalid |= specialist_invalid

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {
                    "asset_version_id",
                    "script_version_id",
                    "storyboard_version_id",
                    "resource_id",
                }:
                    with suppress(ValueError, TypeError, AttributeError):
                        references.add(UUID(item))
                elif isinstance(item, dict | list):
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    if child.workflow_type != "continuity_review":
        for loop in outcomes:
            collect(loop.outcome_json)
    for identity in sorted(identities | references):
        row = db.get(AssetVersion, identity, populate_existing=True)
        if row is not None:
            snapshots.append(
                {
                    "id": str(row.id),
                    "status": row.status,
                    "stale": row.stale,
                    "revision": row.revision,
                    "checksum": row.checksum,
                    "metadata_sha256": digest(row.metadata_json),
                }
            )
            invalid |= row.stale or row.status in {"STALE", "ARCHIVED", "REJECTED"}
    if len(identities) > 128:
        raise DomainError("PRODUCER_OBSERVATION_CONFLICT", "专业结果超过观察版本上限", 409)
    return (
        sorted(identities),
        digest(
            {
                "versions": snapshots,
                "loops": [
                    {"id": str(loop.id), "status": loop.status, "outcome": loop.outcome_json}
                    for loop in sorted(outcomes, key=lambda item: str(item.id))
                ],
                "state": child.agent_state_json,
                "pending_review": child.pending_review,
                "resume": child.resume_json,
            }
        ),
        invalid,
    )


def validate_child_outputs(db: Session, child: WorkflowRun) -> None:
    """A saved successful delegation cannot grant reuse of stale or rejected output."""
    if _outputs(db, child)[2]:
        raise DomainError("PRODUCER_OUTPUT_STALE", "原专业结果已失效，请局部重规划", 409)


def _unknown_result(db: Session, child: WorkflowRun) -> bool:
    for call in db.scalars(select(GatewayCall).where(GatewayCall.workflow_id == child.id)):
        receipt = latest_receipt(db, call.id)
        if receipt is None or receipt.status in {"RESULT_UNKNOWN", "SUBMITTED", "RUNNING"}:
            return True
    return any(job.status in _UNRESOLVED_MEDIA for job in concept_jobs(db, run_id=child.id))


def _tasks(
    db: Session,
    settings: Settings,
    root: WorkflowRun,
    control: AgentRunLimit,
    record: ProducerPlanRecord,
    actor: str,
    request: ProducerObservationRequest,
    human: ProducerHumanEvidence | None,
) -> tuple[list[ObservedProducerTask], list[str]]:
    observed = []
    human_adjustment = human is not None and (
        human.kind == "FORMAL" or human.decision.decision == "ANSWER"
    )
    affected: set[str] = set()
    context = AgentToolContext(db, root.id, control.project_id, root.episode_id, actor, "producer")
    for planned in record.plan.tasks:
        origin = canonical_task(record, planned.task_key)
        receipt = find_task_delegation(db, root.id, origin)
        input_error = None
        try:
            validate_producer_task(db, root, planned, actor, settings)
        except DomainError as exc:
            if exc.status_code in {401, 403}:
                raise
            input_error = exc.code
        child = None
        identities: list[UUID] = []
        output_sha = digest({})
        unknown = invalid = False
        if receipt is not None:
            validate_saved_child(context, root, planned, receipt, control)
            child = db.get(WorkflowRun, receipt.child_run_id, populate_existing=True)
            assert child is not None
            identities, output_sha, invalid = _outputs(db, child)
            unknown = _unknown_result(db, child)
        stale_or_failed = bool(
            input_error or invalid or (child and child.status in _TERMINAL_FAILURE)
        )
        requested = planned.task_key in request.requested_task_keys
        explicitly_reopen_cancelled = bool(
            child is not None
            and child.status == "CANCELLED"
            and requested
            and request.trigger == "HUMAN_DECISION"
            and human_adjustment
        )
        if (
            stale_or_failed
            or explicitly_reopen_cancelled
            or any(key in affected for key in planned.depends_on)
            or (
                requested
                and (
                    request.trigger == "BUDGET_CHANGED"
                    or (request.trigger == "HUMAN_DECISION" and human_adjustment)
                )
                and child is None
            )
        ):
            affected.add(planned.task_key)
        replaceable = (
            planned.task_key in affected
            and not unknown
            and (
                child is None
                or explicitly_reopen_cancelled
                or child.status in _TERMINAL_FAILURE
                or (child.status == "SUCCEEDED" and bool(input_error or invalid))
            )
        )
        observed.append(
            ObservedProducerTask(
                task_key=planned.task_key,
                origin=origin,
                child_run_id=child.id if child else None,
                status=child.status if child else None,
                revision=child.revision if child else None,
                input_error_code=input_error,
                output_version_ids=identities,
                output_sha256=output_sha,
                unknown_external_result=unknown,
                replaceable=replaceable,
            )
        )
    return observed, [task.task_key for task in record.plan.tasks if task.task_key in affected]


def _external_costs(db: Session, root: WorkflowRun, project_id: UUID) -> list[dict]:
    facts = cost_facts(project_id)
    rows = db.execute(
        select(facts)
        .where(~((facts.c.source == "TEXT") & (facts.c.task_id == root.id)))
        .order_by(facts.c.source, facts.c.id)
    ).mappings()
    return [
        CostEntry(**row, cost_is_estimate=row["actual_cost"] is None).model_dump(mode="json")
        for row in rows
    ]


def _stable_research(result: InvestigateProjectResult) -> dict:
    stable = result.model_dump(
        mode="json", exclude={"read_at", "evidence_sha256", "runtime_budget"}
    )
    if stable["budget"] is not None:
        stable["budget"].pop("project_costs")
        stable["budget"].pop("episode_costs")
    stable["unknown"] = [
        row
        for row in stable["unknown"]
        if row["code"]
        not in {
            "AGENT_LIMITS_UNAVAILABLE",
            "AGENT_COST_UNKNOWN",
            "UNPRICED_COST",
            "UNCONFIRMED_COST",
        }
    ]
    return stable


def _snapshot(request: ProducerObservationRequest, observation: ProducerObservation) -> str:
    return digest(
        {
            "request": request.model_dump(mode="json"),
            "overview": observation.overview.model_dump(mode="json"),
            "reviews": observation.reviews.model_dump(mode="json"),
            "tasks": [task.model_dump(mode="json") for task in observation.tasks],
            "affected_task_keys": observation.affected_task_keys,
            "authority_sha256": observation.authority_sha256,
            "human_evidence": observation.human_evidence.model_dump(mode="json")
            if observation.human_evidence
            else None,
            "stage_receipt": observation.stage_receipt.model_dump(mode="json")
            if observation.stage_receipt
            else None,
        }
    )


def _collect(
    db: Session,
    settings: Settings,
    root: WorkflowRun,
    control: AgentRunLimit,
    record: ProducerPlanRecord,
    actor: str,
    request: ProducerObservationRequest,
    identity: UUID,
    version_no: int,
    stage_receipt: StageReceipt | None,
) -> ProducerObservation:
    human = _human(db, root, control, record, request.human_decision_id)
    tasks, affected = _tasks(db, settings, root, control, record, actor, request, human)
    overview = InvestigateProjectResult.model_validate(
        investigate_project(
            db,
            settings,
            root,
            actor,
            InvestigateProjectParameters(query=ProducerOverviewQuery(path="overview")),
        )
    )
    reviews = InvestigateProjectResult.model_validate(
        investigate_project(
            db,
            settings,
            root,
            actor,
            InvestigateProjectParameters(query=ProducerReviewQuery(path="reviews")),
        )
    )
    authority = digest(
        {
            "base_plan_id": str(record.plan_id),
            "base_plan_sha256": record.plan_sha256,
            "root_input": scope_for_run(root, control.project_id).input_sha256,
            "limits": control.limits_json,
            "deadline_at": utc(control.deadline_at).isoformat(),
            "overview": _stable_research(overview),
            "reviews": _stable_research(reviews),
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "external_costs": _external_costs(db, root, control.project_id),
            "human": human.model_dump(mode="json") if human else None,
        }
    )
    observation = ProducerObservation(
        id=identity,
        run_id=root.id,
        project_id=control.project_id,
        episode_id=root.episode_id,
        base_plan_id=record.plan_id,
        base_plan_sha256=record.plan_sha256,
        version_no=version_no,
        trigger=request.trigger,
        reason=request.reason,
        read_at=utcnow(),
        snapshot_sha256="0" * 64,
        authority_sha256=authority,
        overview=overview,
        reviews=reviews,
        tasks=tasks,
        affected_task_keys=affected,
        source_event_id=request.source_event_id,
        human_decision_id=request.human_decision_id,
        human_evidence=human,
        stage_receipt=stage_receipt,
    )
    return observation.model_copy(update={"snapshot_sha256": _snapshot(request, observation)})


def _saved(db: Session, root: WorkflowRun, control: AgentRunLimit, identity: UUID):
    binding = find_binding(db, control.project_id, OBSERVATION_KIND, identity)
    if binding is None:
        return None
    try:
        data = binding.snapshot_json
        request = ProducerObservationRequest.model_validate(data["request"])
        observation = ProducerObservation.model_validate(data["observation"])
        if (
            data["request_sha256"] != digest(request.model_dump(mode="json"))
            or observation.id != identity
            or observation.run_id != root.id
            or observation.project_id != control.project_id
            or observation.episode_id != root.episode_id
            or observation.base_plan_id != request.base_plan_id
            or observation.base_plan_sha256 != request.base_plan_sha256
            or observation.trigger != request.trigger
            or observation.reason != request.reason
            or observation.source_event_id != request.source_event_id
            or observation.human_decision_id != request.human_decision_id
            or observation.snapshot_sha256 != _snapshot(request, observation)
        ):
            raise ValueError("Observation identity or digest changed")
    except (ValidationError, KeyError, ValueError, TypeError) as exc:
        raise DomainError("PRODUCER_OBSERVATION_CONFLICT", "持久观察合同无效", 409) from exc
    if request.human_decision_id is not None:
        formal = db.get(HumanReviewDecision, request.human_decision_id, populate_existing=True)
        agent = db.get(AgentLoopDecision, request.human_decision_id, populate_existing=True)
        subject = formal.actor_id if formal else agent.actor_id if agent else None
        if subject is None:
            raise DomainError("PRODUCER_OBSERVATION_CONFLICT", "观察的人工决定已丢失", 409)
        require_review_actor(db, control.project_id, subject, owner=formal is not None)
    return request, observation


def read_observation(
    db: Session, run_id: UUID, observation_id: UUID, actor: str
) -> ProducerObservation:
    root, control = _root(db, run_id, actor)
    saved = _saved(db, root, control, observation_id)
    if saved is None:
        raise DomainError("NOT_FOUND", "生产观察不存在", 404)
    return saved[1]


def validate_observation_current(
    db: Session,
    settings: Settings,
    root: WorkflowRun,
    observation: ProducerObservation,
    actor: str,
) -> None:
    current_root, control = _root(db, root.id, actor)
    _require_research(db, current_root, actor, settings)
    saved = _saved(db, current_root, control, observation.id)
    if saved is None or saved[1] != observation:
        raise DomainError("PRODUCER_OBSERVATION_CONFLICT", "重规划须引用原持久观察", 409)
    request, _ = saved
    record = _base(db, current_root, control, actor, request)
    current = _collect(
        db,
        settings,
        current_root,
        control,
        record,
        actor,
        request,
        observation.id,
        observation.version_no,
        observation.stage_receipt,
    )
    if current.authority_sha256 != observation.authority_sha256:
        raise DomainError("PRODUCER_OBSERVATION_STALE", "权威状态已变化，请重新观察", 409)


def _claim_request(
    db: Session,
    root: WorkflowRun,
    control: AgentRunLimit,
    actor: str,
    identity: UUID,
    request: ProducerObservationRequest,
) -> str:
    snapshot = {
        "schema_version": 1,
        "run_id": str(root.id),
        "project_id": str(control.project_id),
        "episode_id": str(root.episode_id),
        "actor_id": actor,
        "request": request.model_dump(mode="json"),
        "request_sha256": digest(request.model_dump(mode="json")),
    }
    allowed = {"NONE"} if request.source_event_id is None else {"CONSUME"}
    if request.source_event_id is not None and request.trigger in {
        "RECHECK",
        "BUDGET_CHANGED",
        "HUMAN_DECISION",
    }:
        allowed.add("RECHECK")
    existing = find_binding(db, control.project_id, OBSERVATION_REQUEST_KIND, identity)
    if existing is not None:
        saved = existing.snapshot_json
        action = saved.get("event_action") if isinstance(saved, dict) else None
        if (
            not isinstance(action, str)
            or action not in allowed
            or saved != snapshot | {"event_action": action}
        ):
            raise DomainError("PRODUCER_OBSERVATION_CONFLICT", "观察键已有不同请求意图", 409)
        return action

    action = "NONE" if request.source_event_id is None else "CONSUME"
    if request.source_event_id is not None:
        prior = db.scalar(
            select(ProductionEventReceipt).where(
                ProductionEventReceipt.event_id == request.source_event_id,
                ProductionEventReceipt.evaluation_key == request.source_event_id,
            )
        )
        if (
            prior is not None
            and StageReceipt.model_validate(prior.result_json).disposition == "BLOCKED"
            and "RECHECK" in allowed
        ):
            action = "RECHECK"
    # The caller holds the shared gateway/root locks. Claim before leaving this
    # transaction, so a conflicting key cannot enter the separate coordinator.
    # Freeze the action as well: a committed BLOCKED receipt after an interruption
    # must not silently turn the original consume into a new explicit recheck.
    bind(
        db,
        control.project_id,
        OBSERVATION_REQUEST_KIND,
        identity,
        snapshot | {"event_action": action},
    )
    return action


def observe_producer(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    actor: str,
    request_id: str,
    request: ProducerObservationRequest,
) -> ProducerObservation:
    request_id = TypeAdapter(IdempotencyKey).validate_python(request_id)
    request = ProducerObservationRequest.model_validate(request.model_dump(mode="json"))
    identity = uuid5(run_id, "producer-observation:" + request_id)
    with factory.begin() as db:
        root, control = _root(db, run_id, actor)
        _require_research(db, root, actor, settings)
        saved = _saved(db, root, control, identity)
        if saved is not None:
            if saved[0] != request:
                raise DomainError("PRODUCER_OBSERVATION_CONFLICT", "观察键已有不同请求", 409)
            return saved[1]
        record = _base(db, root, control, actor, request)
        _human(db, root, control, record, request.human_decision_id)
        if request.source_event_id is not None:
            _event(db, root, control, actor, request.source_event_id)
        event_action = _claim_request(db, root, control, actor, identity, request)
    receipt = None
    if request.source_event_id is not None:
        receipt = (
            recheck_stage_event(factory, request.source_event_id, actor, identity)
            if event_action == "RECHECK"
            else consume_stage_event(factory, request.source_event_id)
        )
    with factory.begin() as db:
        root, control = _root(db, run_id, actor)
        _require_research(db, root, actor, settings)
        saved = _saved(db, root, control, identity)
        if saved is not None:
            if saved[0] != request:
                raise DomainError("PRODUCER_OBSERVATION_CONFLICT", "观察键已有不同请求", 409)
            return saved[1]
        record = _base(db, root, control, actor, request)
        previous = db.scalars(
            select(VersionBinding).where(
                VersionBinding.project_id == control.project_id,
                VersionBinding.resource_type == OBSERVATION_KIND,
            )
        )
        version_no = 1 + max(
            (
                row.snapshot_json["observation"]["version_no"]
                for row in previous
                if row.snapshot_json.get("observation", {}).get("run_id") == str(root.id)
            ),
            default=0,
        )
        observation = _collect(
            db, settings, root, control, record, actor, request, identity, version_no, receipt
        )
        bind(
            db,
            control.project_id,
            OBSERVATION_KIND,
            identity,
            {
                "schema_version": 1,
                "request": request.model_dump(mode="json"),
                "request_sha256": digest(request.model_dump(mode="json")),
                "observation": observation.model_dump(mode="json"),
            },
        )
        db.add(
            AuditEvent(
                project_id=control.project_id,
                actor_id=actor,
                action="producer.observed",
                resource_id=identity,
                details_json={
                    "run_id": str(root.id),
                    "base_plan_id": str(record.plan_id),
                    "snapshot_sha256": observation.snapshot_sha256,
                    "trigger": request.trigger,
                    "reason": request.reason,
                    "affected_task_keys": observation.affected_task_keys,
                },
            )
        )
        return observation
