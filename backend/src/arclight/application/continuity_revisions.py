"""The sole continuity revision admission path into the existing GenerationService."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.continuity_escalations import require_completed_after_escalation
from arclight.application.continuity_revision_store import (
    candidate_revision,
    job_revision,
    revision_record,
)
from arclight.application.continuity_routing import read_routed_review
from arclight.application.generation_jobs import create_job
from arclight.application.generation_scheduling import policy_for as source_policy_for
from arclight.application.human_reviews import require_review_actor
from arclight.application.media_services import selection_for_job
from arclight.application.production_events import begin_serialized
from arclight.application.production_versions import bind, check_media, component, find_binding
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.continuity_revisions import (
    MAX_REVISION_ROUNDS,
    REVISION_RESOURCE,
    RevisionOrigin,
    RevisionRecord,
    RevisionRequest,
    amend_prompt,
)
from arclight.domain.generation_requests import GenerationCreate, GenerationSpec
from arclight.domain.versions import digest
from arclight.persistence.generation_candidates import GenerationCandidate
from arclight.persistence.models import DependencyManifest, GenerationJob, VisualAsset


def request_revision(
    db: Session,
    settings: Settings,
    project_id: UUID,
    actor: str,
    key: str,
    request: RevisionRequest,
) -> tuple[RevisionRecord, bool]:
    request = RevisionRequest.model_validate(request.model_dump(mode="json"))
    begin_serialized(db)
    require_project(db, project_id, actor)
    identity = uuid5(project_id, "continuity-revision/1:" + key)
    fingerprint = digest(request.model_dump(mode="json"))

    def existing_record() -> RevisionRecord | None:
        if find_binding(db, project_id, REVISION_RESOURCE, identity) is None:
            return None
        saved = revision_record(db, project_id, identity)
        if saved.request_sha256 != fingerprint or saved.created_by != actor:
            raise DomainError("IDEMPOTENCY_CONFLICT", "修订幂等键已绑定其他请求或操作者", 409)
        return saved

    # A replay is read-only but still requires current write authority.
    previous = existing_record()
    if previous is not None:
        require_project(db, project_id, actor, write=True)
        return previous, False
    record, route = read_routed_review(db, request.review_id)
    review = record.review
    if review.project_id != project_id:
        raise DomainError("NOT_FOUND", "审查记录不存在", 404)
    require_project(db, project_id, actor, write=True)
    previous = existing_record()
    if previous is not None:
        return previous, False
    require_review_actor(db, project_id, actor, owner=False)
    require_review_actor(db, project_id, route.routed_by, owner=False)
    require_completed_after_escalation(db, record)
    if review.conclusion != "REVISION_REQUIRED":
        raise DomainError("REVISION_NOT_ALLOWED", "只有有效的修订结论可以创建修订任务", 409)
    if candidate_revision(db, project_id, review.target.candidate_id) is not None:
        raise DomainError(
            "REVISION_ALREADY_REQUESTED", "该候选已有修订；失败请重试原任务，新图须重审", 409
        )
    issues = {item.issue_id: item for item in review.issues}
    if not set(request.issue_ids) <= issues.keys() or any(
        issues[identity].suggested_action not in {"REVISE_PROMPT", "REGENERATE"}
        for identity in request.issue_ids
    ):
        raise DomainError("REVISION_SCOPE_INVALID", "修改范围必须引用本次审查的可修订问题", 422)
    evidence_ids = sorted(
        {identity for issue in request.issue_ids for identity in issues[issue].evidence_ids},
        key=str,
    )
    candidate = db.get(GenerationCandidate, review.target.candidate_id)
    source = db.get(GenerationJob, candidate.job_id) if candidate else None
    if source is None or source.project_id != project_id or source.target_type != "SHOT_IMAGE":
        raise DomainError("REVISION_SOURCE_INVALID", "修订必须源于同项目的镜头候选任务", 409)
    manifest = db.get(DependencyManifest, source.input_manifest_id)
    if manifest is None or manifest.id != review.target.input_manifest_id:
        raise DomainError("REVISION_SOURCE_INVALID", "候选与原生成输入不匹配", 409)
    check_media(db, project_id, source.id, manifest.id)
    parent = job_revision(db, source)
    round_no = parent.round + 1 if parent else 1
    stopped = round_no > MAX_REVISION_ROUNDS
    origin = RevisionOrigin(
        revision_id=identity,
        project_id=project_id,
        request=request,
        request_sha256=fingerprint,
        review_sha256=digest(record.model_dump(mode="json")),
        target=review.target,
        evidence_ids=evidence_ids,
        source_job_id=source.id,
        source_manifest_id=manifest.id,
        root_candidate_id=parent.root_candidate_id if parent else review.target.candidate_id,
        parent_revision_id=parent.revision_id if parent else None,
        round=round_no,
        status="NEEDS_REVIEW" if stopped else "QUEUED",
        created_by=actor,
        created_at=datetime.now(UTC),
    )
    job = None
    if not stopped:
        context = manifest.snapshot_json["context"]
        composition = context.get("prompt_composition")
        if not isinstance(composition, dict):
            raise DomainError("REVISION_SOURCE_INVALID", "原任务缺少可追溯的提示词组合清单", 409)
        original = GenerationSpec.model_validate(context["generation"])
        spec, amended = amend_prompt(original, composition, request)
        asset = db.get(VisualAsset, review.target.asset_id, populate_existing=True)
        assert asset is not None
        inputs = [
            {key: item[key] for key in ("source_asset_version_id", "dependency_type", "selectors")}
            for item in manifest.snapshot_json["inputs"]
        ]
        if not any(
            item["source_asset_version_id"] == str(review.target.storyboard_id)
            and item["dependency_type"] == "CONTENT_DEPENDENCY"
            for item in inputs
        ):
            inputs.append(
                {
                    "source_asset_version_id": str(review.target.storyboard_id),
                    "dependency_type": "CONTENT_DEPENDENCY",
                    "selectors": ["shot:" + str(review.target.shot_id)],
                }
            )
        payload = GenerationCreate.model_validate(
            {
                "episode_id": review.episode_id,
                "batch_id": context.get("budget_batch_id"),
                "target_type": "SHOT_IMAGE",
                "target_asset_id": asset.id,
                "expected_asset": {
                    "revision": asset.revision,
                    "lock_version": asset.lock_version,
                    "current_version_id": asset.current_version_id,
                },
                "task_type": "IMAGE",
                "inputs": inputs,
                "spec": spec.model_dump(mode="json"),
            }
        )
        service = selection_for_job(db, source)
        original_policy = source_policy_for(db, source)
        policy = settings.media_execution_policy.model_copy(
            update={
                "max_attempts": min(
                    original_policy.max_attempts, settings.media_execution_policy.max_attempts
                ),
                "max_elapsed_seconds": min(
                    original_policy.max_elapsed_seconds,
                    settings.media_execution_policy.max_elapsed_seconds,
                ),
            }
        )
        job = create_job(
            db,
            project_id,
            actor,
            "continuity-revision:" + str(identity),
            payload,
            execution_policy=policy,
            frozen_prompt_composition=amended,
            frozen_service=service.private_snapshot() if service else None,
            continuity_revision_origin=origin.model_dump(mode="json"),
        )
    revision = RevisionRecord(
        **origin.model_dump(),
        job_id=job.id if job else None,
        input_manifest_id=job.input_manifest_id if job else None,
    )
    entries = [item.model_dump(mode="json") for item in review.versions]
    entries.extend(
        [
            component(
                db,
                project_id,
                "MEDIA",
                "AGENT",
                "continuity-revision-service",
                "1",
                {"max_rounds": MAX_REVISION_ROUNDS},
            ),
            component(
                db,
                project_id,
                "MEDIA",
                "SCHEMA",
                "continuity-revision-request",
                "1",
                RevisionRequest.model_json_schema(),
            ),
        ]
    )
    bind(
        db,
        project_id,
        REVISION_RESOURCE,
        identity,
        {
            "schema_version": 1,
            "compatibility": "EXACT_DEFINITIONS",
            "components": entries,
            "parameters": request.model_dump(mode="json"),
            "parameter_sha256": fingerprint,
            "continuity_revision": deepcopy(revision.model_dump(mode="json")),
        },
    )
    audit(
        db,
        project_id,
        actor,
        "continuity_revision.exhausted" if stopped else "continuity_revision.requested",
        identity,
        revision.model_dump(mode="json"),
    )
    db.flush()
    return revision, True
