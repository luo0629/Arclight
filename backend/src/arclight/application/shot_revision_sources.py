"""Freeze and revalidate human revision sources; execution stays in GenerationService."""

from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from arclight.application.commands import require_project
from arclight.application.generation_budget import quote_job
from arclight.application.generation_targets import require_available_shot
from arclight.application.media_services import selection_for_job
from arclight.application.production_versions import check_media
from arclight.application.visual_bible import state as visual_bible_state
from arclight.core.errors import DomainError
from arclight.domain.continuity_revisions import PromptAmendment, append_prompt_amendments
from arclight.domain.generation_requests import GenerationCreate
from arclight.domain.media_capabilities import validate_capabilities
from arclight.domain.shot_revisions import (
    OPERATION_LABELS,
    ShotRevisionIntent,
    ShotRevisionOrigin,
    ShotRevisionPreview,
    ShotRevisionSource,
)
from arclight.domain.versions import digest
from arclight.persistence.generation_candidates import GenerationCandidate
from arclight.persistence.models import AssetVersion, DependencyManifest, GenerationJob, VisualAsset
from arclight.providers.media_capabilities import reference_count, resolve_capabilities


@dataclass(frozen=True)
class RevisionSource:
    source: ShotRevisionSource
    job: GenerationJob
    manifest: DependencyManifest
    asset: VisualAsset
    composition: dict
    constraints: list[str]


@dataclass(frozen=True)
class PreparedShotRevision:
    preview: ShotRevisionPreview
    payload: GenerationCreate
    composition: dict
    frozen_service: dict | None
    source_job: GenerationJob


def read_revision_source(
    db: Session, project_id: UUID, actor: str, intent: ShotRevisionIntent
) -> RevisionSource:
    project = require_project(db, project_id, actor, write=True)
    candidate = db.get(GenerationCandidate, intent.candidate_id, populate_existing=True)
    if candidate is None or candidate.project_id != project_id:
        raise DomainError("NOT_FOUND", "镜头候选不存在", 404)
    version = db.get(AssetVersion, candidate.asset_version_id, populate_existing=True)
    job = db.get(GenerationJob, candidate.job_id, populate_existing=True)
    asset = db.get(VisualAsset, version.asset_id, populate_existing=True) if version else None
    manifest = db.get(DependencyManifest, candidate.input_manifest_id)
    if (
        version is None
        or job is None
        or asset is None
        or manifest is None
        or job.project_id != project_id
        or asset.project_id != project_id
        or asset.asset_type != "SHOT_IMAGE"
        or job.task_type != "IMAGE"
        or job.target_type != "SHOT_IMAGE"
        or candidate.input_manifest_id != job.input_manifest_id
        or not candidate.media_check_json
        or version.stale
        or version.status in {"ARCHIVED", "LOCKED"}
        or asset.status in {"ARCHIVED", "LOCKED"}
    ):
        raise DomainError("SHOT_REVISION_SOURCE_INVALID", "来源候选已失效、锁定或不可修订", 409)
    context = manifest.snapshot_json["context"]
    expected = {"storyboard_id": str(intent.storyboard_id), "shot_id": str(intent.shot_id)}
    from arclight.application.continuity_workflow import frozen_review_binding

    if frozen_review_binding(context) != expected or job.episode_id is None:
        raise DomainError("SHOT_REVISION_TARGET_INVALID", "候选不属于此精确分镜与镜头", 409)
    check_media(db, project_id, job.id, manifest.id)
    composition = context.get("prompt_composition")
    if not isinstance(composition, dict):
        raise DomainError("REVISION_SOURCE_INVALID", "原任务缺少可追溯的提示词组合", 409)
    visual = visual_bible_state(db, project)
    visual_digest = digest(
        {
            "version_id": str(visual["id"]) if visual["id"] else None,
            "version_no": visual["version_no"],
            "fields": visual["fields"],
            "locked_fields": visual["locked_fields"],
            "legacy_snapshot": visual["legacy_snapshot"],
        }
    )
    visual_sources = [
        item
        for item in composition.get("sources", [])
        if item.get("source_type") in {"VISUAL_BIBLE_VERSION", "LEGACY_STYLE_GUIDE"}
    ]
    if len(visual_sources) != 1 or visual_sources[0].get("snapshot_sha256") != visual_digest:
        raise DomainError(
            "SHOT_REVISION_STYLE_CHANGED", "视觉规范已变化，请按当前分镜重新生成", 409
        )
    blocks = {item["key"]: item["content"] for item in composition.get("blocks", [])}
    if (
        (intent.operation == "RECOMPOSE" and "composition" in visual["locked_fields"])
        or (intent.operation == "REPLACE_SCENE" and blocks.get("location_constraints"))
        or (intent.operation == "CHANGE_OUTFIT" and blocks.get("character_constraints"))
    ):
        raise DomainError(
            "SHOT_REVISION_PROTECTED",
            "本次修改涉及锁定规范或已批准参考，请先另存并批准对应规范／参考及分镜",
            409,
        )
    source = ShotRevisionSource(
        candidate_id=candidate.id,
        asset_id=asset.id,
        version_id=version.id,
        version_no=version.version_no,
        storyboard_id=intent.storyboard_id,
        shot_id=intent.shot_id,
        episode_id=job.episode_id,
        job_id=job.id,
        input_manifest_id=manifest.id,
        provider=job.provider,
        model=job.model,
        snapshot_digest=digest(
            {
                "version": {
                    "id": str(version.id),
                    "revision": version.revision,
                    "status": version.status,
                    "checksum": version.checksum,
                    "media_check": candidate.media_check_json,
                },
                "asset": {
                    "revision": asset.revision,
                    "lock_version": asset.lock_version,
                    "current_version_id": str(asset.current_version_id)
                    if asset.current_version_id
                    else None,
                    "status": asset.status,
                },
                "composition": digest(composition),
                "visual_bible": visual_digest,
            }
        ),
    )
    constraints = ["原精确分镜、角色身份、场景／道具参考和负面约束保持；新图须重新审查。"]
    constraints.extend(f"锁定视觉规范：{field}" for field in visual["locked_fields"])
    return RevisionSource(source, job, manifest, asset, composition, constraints)


def prepare_shot_revision(
    db: Session, project_id: UUID, actor: str, intent: ShotRevisionIntent
) -> PreparedShotRevision:
    found = read_revision_source(db, project_id, actor, intent)
    require_available_shot(
        db,
        project_id,
        {"storyboard_id": str(intent.storyboard_id), "shot_id": str(intent.shot_id)},
        found.job.episode_id,
    )
    context = found.manifest.snapshot_json["context"]
    from arclight.application.continuity_workflow import (
        frozen_review_binding,
        generation_spec_for_new_job,
    )

    original = generation_spec_for_new_job(context)
    spec, composition = append_prompt_amendments(
        original,
        found.composition,
        [
            PromptAmendment(
                block="camera_language" if intent.operation == "RECOMPOSE" else "shot_description",
                instruction=(
                    f"仅调整{OPERATION_LABELS[intent.operation]}：{intent.instruction.strip()}。"
                    "保留原精确分镜、角色身份、已批准参考、锁定规范及其余约束。"
                ),
            )
        ],
        {
            "source_key": f"human_shot_revision:{intent.candidate_id}",
            "source_type": "HUMAN_SHOT_REVISION",
            "candidate_id": str(intent.candidate_id),
            "operation": intent.operation,
        },
    )
    asset = found.asset
    payload = GenerationCreate.model_validate(
        {
            "episode_id": found.job.episode_id,
            "target_type": "SHOT_IMAGE",
            "target_asset_id": asset.id,
            "expected_asset": {
                "revision": asset.revision,
                "lock_version": asset.lock_version,
                "current_version_id": asset.current_version_id,
            },
            "task_type": "IMAGE",
            "review_target": frozen_review_binding(context),
            "inputs": [
                {
                    key: item[key]
                    for key in ("source_asset_version_id", "dependency_type", "selectors")
                }
                for item in found.manifest.snapshot_json["inputs"]
            ],
            "spec": spec.model_dump(mode="json"),
        }
    )
    capability = resolve_capabilities(spec.provider, spec.model)
    validate_capabilities(
        capability, spec, "IMAGE", reference_count(found.manifest.snapshot_json["inputs"])
    )
    service = selection_for_job(db, found.job)
    frozen_service = service.private_snapshot() if service else None
    quote = quote_job(db, payload, actor=actor, frozen_service=frozen_service)
    confirmation = digest(
        {
            "intent": intent.model_dump(mode="json"),
            "source": found.source.model_dump(mode="json"),
            "payload": payload.model_dump(mode="json"),
            "quote": quote.model_dump(mode="json") if quote is not None else None,
            "capability": capability.model_dump(mode="json"),
        }
    )
    preview = ShotRevisionPreview(
        intent=intent,
        source=found.source,
        quote=quote,
        confirmation_digest=confirmation,
        preserved_constraints=found.constraints,
    )
    return PreparedShotRevision(preview, payload, composition, frozen_service, found.job)


def human_revision_origin(db: Session, job: GenerationJob) -> ShotRevisionOrigin | None:
    manifest = db.get(DependencyManifest, job.input_manifest_id)
    composition = manifest.snapshot_json["context"].get("prompt_composition") if manifest else None
    value = composition.get("human_shot_revision") if isinstance(composition, dict) else None
    if value is None:
        return None
    try:
        origin = ShotRevisionOrigin.model_validate(deepcopy(value))
        if origin.created_by != job.created_by or origin.source.asset_id != job.target_asset_id:
            raise ValueError("Revision owner or target mismatch")
        return origin
    except (ValidationError, ValueError) as exc:
        raise DomainError("SHOT_REVISION_SOURCE_INVALID", "修订来源记录不完整", 409) from exc


def require_human_revision_execution(db: Session, job: GenerationJob) -> None:
    origin = human_revision_origin(db, job)
    if origin is not None:
        current = read_revision_source(db, job.project_id, origin.created_by, origin.intent)
        if current.source != origin.source:
            raise DomainError(
                "SHOT_REVISION_SOURCE_CHANGED", "修订原图或规范已变化，请重新预览", 409
            )
