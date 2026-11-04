"""Read common run/result facts without copying secrets or creating another ledger."""

from decimal import Decimal
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import case, func, literal, select
from sqlalchemy.orm import Session

from arclight.application.gateway_accounting import latest_receipt
from arclight.application.production_versions import checked_snapshot, find_binding, input_summary
from arclight.core.errors import DomainError
from arclight.domain.enums import RunStatus
from arclight.domain.model_gateway import GatewayUsage
from arclight.domain.versions import digest
from arclight.domain.workflow_contracts import (
    GatewayCallUsage,
    InputReference,
    ProductionComponent,
    RunBudget,
    RunInputSnapshot,
    RunModelVersion,
    RunUsage,
    RunVersionSnapshot,
    WorkflowResult,
    WorkflowRunContract,
)
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import (
    AuditEvent,
    DependencyManifest,
    Episode,
    Script,
    StoryAnalysis,
    Storyboard,
    StoryOutline,
    VersionBinding,
    WorkflowEvent,
    WorkflowRun,
)

_TERMINAL_EVENTS = {"workflow.finished", "workflow.failed", "workflow.cancelled"}
_ACTIVE = {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_REVIEW}


def _binding(db: Session, project_id: UUID, run_id: UUID) -> VersionBinding | None:
    return find_binding(db, project_id, "WORKFLOW", run_id)


def _result(db: Session, run: WorkflowRun, project_id: UUID) -> WorkflowResult | None:
    artifacts: list[UUID] = []
    labels: list[str] = []
    needs_review = run.status == RunStatus.WAITING_REVIEW
    for model, status, label in (
        (Script, Script.status, "剧本"),
        (Storyboard, Storyboard.status, "分镜"),
        (StoryAnalysis, literal(None), "故事分析"),
        (StoryOutline, StoryOutline.status, "大纲"),
    ):
        row = db.execute(
            select(model.id, status.label("status")).where(
                model.run_id == run.id, model.episode_id == run.episode_id
            )
        ).first()
        if row is not None:
            artifacts.append(row.id)
            labels.append(label)
            if row.status in {"DRAFT", "READY_FOR_REVIEW"}:
                needs_review = True
    if not artifacts:
        return None
    return WorkflowResult(
        result_id=uuid5(run.id, "workflow-result:1.0:" + digest(sorted(map(str, artifacts)))),
        project_id=project_id,
        episode_id=run.episode_id,
        agent_name="text_workflow",
        status=RunStatus(run.status),
        summary="已登记" + "、".join(labels),
        artifact_ids=artifacts,
        warnings=[run.error_code] if run.error_code else [],
        requires_human_review=needs_review,
    )


def _versions(db: Session, run: WorkflowRun, binding: VersionBinding | None) -> RunVersionSnapshot:
    state = "LEGACY_UNBOUND"
    components: list[ProductionComponent] = []
    model: RunModelVersion | None = None
    parameter_sha256: str | None = None
    if binding is not None:
        state = "INCOMPATIBLE"
        try:
            snapshot = checked_snapshot(db, binding)
            model = RunModelVersion(
                provider=snapshot["model"]["provider"],
                model=snapshot["model"]["model"],
                supplier=snapshot["model"].get("supplier"),
            )
            parameter_sha256 = snapshot["parameter_sha256"]
            components = [
                ProductionComponent.model_validate(
                    {
                        key: item[key]
                        for key in ("id", "family", "kind", "name", "version", "digest")
                    }
                )
                for item in snapshot["components"]
            ]
            if (
                snapshot["workflow_type"] == run.workflow_type
                and snapshot["workflow_version"] == run.workflow_version
                and snapshot["inputs"] == input_summary(run.input_json)
                and run.error_code != "PRODUCTION_VERSION_INCOMPATIBLE"
            ):
                state = "BOUND"
        except (DomainError, KeyError, TypeError, ValueError):
            components = []
            model = None
            parameter_sha256 = None
    return RunVersionSnapshot.model_validate(
        {
            "state": state,
            "workflow_version": run.workflow_version,
            "components": components,
            "model": model,
            "parameter_sha256": parameter_sha256,
        }
    )


def _input(run: WorkflowRun, binding: VersionBinding | None) -> RunInputSnapshot:
    source = binding.snapshot_json["inputs"] if binding else input_summary(run.input_json)
    references: list[InputReference] = []
    seen: set[tuple[str, str]] = set()
    # Only UUIDs and checksums cross this boundary, even for untrusted legacy JSON.
    for entry in source["references"]:
        for field, value in entry.items():
            try:
                reference = InputReference.model_validate({"field": field, "value": value})
            except ValidationError:
                continue
            identity = (reference.field, str(reference.value))
            if identity not in seen:
                references.append(reference)
                seen.add(identity)
    manifests = binding.snapshot_json.get("context_manifest_ids", {}) if binding else {}
    return RunInputSnapshot(
        origin="VERSION_BINDING" if binding else "LEGACY_RUN",
        sha256=source["sha256"],
        references=references,
        context_manifest_ids=[UUID(value) for value in sorted(set(manifests.values()))],
    )


def _budget(db: Session, binding: VersionBinding | None) -> RunBudget:
    values: dict = {
        "origin": "UNKNOWN",
        "limit": None,
        "task_limit": None,
        "episode_limit": None,
        "cost_estimate": None,
    }
    if binding:
        snapshot = binding.snapshot_json
        manifests = snapshot.get("context_manifest_ids", {})
        for identity in sorted(set(manifests.values())):
            manifest = db.get(DependencyManifest, UUID(identity))
            if manifest is None or manifest.project_id != binding.project_id:
                raise DomainError("RUN_CONTRACT_INVALID", "运行输入清单缺失或不匹配", 409)
            budget = manifest.snapshot_json["context"]["task_context"]["required"]["constraints"][
                "budget"
            ]
            values.update(
                origin="STARTUP_CONTEXT",
                limit=budget["project_limit"],
                task_limit=budget["task_limit"],
                episode_limit=budget["episode_limit"],
            )
            break  # All stages capture the same transaction's project budget.
        policy = snapshot["parameters"].get("gateway_policy", {})
        maximum = policy.get("text_maximum_cost")
        if snapshot["model"]["provider"] == "mock":
            maximum = "0"
        if maximum is not None:
            # This is a frozen conservative quote, not an actual bill.
            stages = sum(item["kind"] == "AGENT" for item in snapshot["components"])
            values["cost_estimate"] = Decimal(maximum) * stages
    return RunBudget.model_validate(values)


def _usage(db: Session, project_id: UUID, run_id: UUID) -> RunUsage:
    scope = (
        GatewayCall.workflow_id == run_id,
        GatewayCall.project_id == project_id,
        GatewayCall.generation_attempt_id.is_(None),
    )
    total = db.scalar(select(func.count()).select_from(GatewayCall).where(*scope)) or 0
    calls = db.scalars(
        select(GatewayCall)
        .where(*scope)
        .order_by(GatewayCall.created_at, GatewayCall.id)
        .limit(100)
    )
    result: list[GatewayCallUsage] = []
    for call in calls:
        receipt = latest_receipt(db, call.id)
        usage = GatewayUsage.model_validate(receipt.usage_json) if receipt else GatewayUsage()
        result.append(
            GatewayCallUsage.model_validate(
                {
                    "call_id": call.id,
                    "task": call.task,
                    "provider": call.route_json["provider"],
                    "model": call.route_json["model"],
                    "status": receipt.status if receipt else "RESULT_UNKNOWN",
                    "outcome_source": receipt.kind if receipt else "MISSING",
                    "quoted_cost": call.maximum_cost,
                    "usage": usage,
                }
            )
        )
    return RunUsage(total_calls=total, calls=result)


def run_contract(db: Session, run: WorkflowRun) -> WorkflowRunContract:
    episode = db.get(Episode, run.episode_id)
    assert episode is not None
    binding = _binding(db, episode.project_id, run.id)
    times = db.execute(
        select(
            func.min(
                case((WorkflowEvent.event_type == "workflow.started", WorkflowEvent.created_at))
            ),
            func.max(
                case((WorkflowEvent.event_type.in_(_TERMINAL_EVENTS), WorkflowEvent.created_at))
            ),
            func.sum(case((WorkflowEvent.event_type == "workflow.retry_requested", 1), else_=0)),
        ).where(WorkflowEvent.run_id == run.id)
    ).one()
    requested_by = db.scalar(
        select(AuditEvent.actor_id)
        .where(
            AuditEvent.project_id == episode.project_id,
            AuditEvent.resource_id == run.id,
            AuditEvent.action == "workflow.started",
        )
        .order_by(AuditEvent.created_at, AuditEvent.id)
        .limit(1)
    )
    return WorkflowRunContract(
        run_id=run.id,
        project_id=episode.project_id,
        episode_id=run.episode_id,
        requested_by=requested_by,
        task_type=run.workflow_type,
        requested_at=run.created_at,
        started_at=times[0],
        finished_at=times[1] if run.status not in _ACTIVE else None,
        retry_count=times[2] or 0,
        input_snapshot=_input(run, binding),
        workflow_version=run.workflow_version,
        versions=_versions(db, run, binding),
        budget=_budget(db, binding),
        usage=_usage(db, episode.project_id, run.id),
        trace_id=run.trace_id,
        status=RunStatus(run.status),
        result=_result(db, run, episode.project_id),
    )


def serialize_run(db: Session, run: WorkflowRun) -> dict:
    """Keep the legacy resource fields and add a strictly validated public view."""
    try:
        contract = run_contract(db, run)
    except (ValidationError, KeyError, TypeError, ValueError):
        raise DomainError("RUN_CONTRACT_INVALID", "运行合同包含不兼容的数据", 409) from None
    return {
        "id": run.id,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "episode_id": run.episode_id,
        "workflow_type": run.workflow_type,
        "workflow_version": run.workflow_version,
        "status": run.status,
        "trace_id": run.trace_id,
        "pending_review": run.pending_review,
        "error_code": run.error_code,
        "revision": run.revision,
        "contract": contract,
    }
