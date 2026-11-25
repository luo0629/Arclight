"""Create media jobs and freeze exact source identities in the existing DAG manifest."""

import hashlib
import json
from copy import deepcopy
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.dependencies import exact_version, lock_dependency_graph
from arclight.application.generation_budget import quote_job
from arclight.application.generation_events import enqueue_dispatch
from arclight.application.generation_scheduling import schedule_for
from arclight.application.generation_targets import require_available_target
from arclight.application.generation_video_inputs import freeze_video_images
from arclight.application.visual_bible import state as visual_bible_state
from arclight.core.errors import DomainError
from arclight.domain.dependencies import validate_dependency_type
from arclight.domain.generation_prompts import compose_image_prompt
from arclight.domain.generation_requests import GenerationCreate, GenerationSpec
from arclight.domain.generation_scheduling import DEFAULT_EXECUTION_POLICY, ExecutionPolicy
from arclight.domain.media_capabilities import validate_capabilities
from arclight.domain.versions import digest
from arclight.persistence.models import (
    DependencyInvalidation,
    DependencyManifest,
    Episode,
    GenerationJob,
    VisualAsset,
)
from arclight.providers.media_capabilities import reference_count, resolve_capabilities


def request_fingerprint(payload: GenerationCreate) -> str:
    values = payload.model_dump(mode="json")
    if values["review_target"] is None:
        del values["review_target"]
    if values["scheduling_mode"] == "INTERACTIVE":
        del values["scheduling_mode"]
    # Preserve idempotent replays of requests captured before capability options existed.
    for field, default in (("batch_size", 1), ("require_query", False), ("require_cancel", False)):
        if values["spec"][field] == default:
            del values["spec"][field]
    return hashlib.sha256(
        json.dumps(
            values,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def validate_target(db: Session, project_id: UUID, payload: GenerationCreate) -> VisualAsset | None:
    if payload.episode_id is not None:
        episode = db.get(Episode, payload.episode_id, populate_existing=True)
        if episode is None or episode.project_id != project_id:
            raise DomainError("NOT_FOUND", "目标剧集不存在", 404)
        if episode.status == "ARCHIVED":
            raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    if payload.target_asset_id is None:
        return None
    asset = db.get(VisualAsset, payload.target_asset_id, populate_existing=True)
    if asset is None or asset.project_id != project_id:
        raise DomainError("NOT_FOUND", "目标资产不存在", 404)
    if asset.asset_type != payload.target_type:
        raise DomainError("INVALID_GENERATION_TARGET", "目标资产类型不一致", 422)
    if asset.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "目标资产只读", 409)
    expected = payload.expected_asset
    assert expected is not None
    if (asset.revision, asset.lock_version, asset.current_version_id) != (
        expected.revision,
        expected.lock_version,
        expected.current_version_id,
    ):
        raise DomainError("VERSION_CONFLICT", "目标资产已变化，请刷新后创建任务", 409)
    return asset


def create_job(
    db: Session,
    project_id: UUID,
    actor: str,
    key: str,
    payload: GenerationCreate,
    *,
    execution_policy: ExecutionPolicy = DEFAULT_EXECUTION_POLICY,
    frozen_prompt_composition: dict | None = None,
    art_concept_origin: dict | None = None,
    frozen_service: dict | None = None,
    continuity_revision_origin: dict | None = None,
    audio_line_origin: dict | None = None,
    short_drama_run_id: UUID | None = None,
) -> GenerationJob:
    require_project(db, project_id, actor)
    lock_dependency_graph(db, project_id)
    project = require_project(db, project_id, actor, write=True)
    fingerprint = request_fingerprint(payload)
    existing = db.scalar(
        select(GenerationJob).where(
            GenerationJob.project_id == project_id,
            GenerationJob.idempotency_key == key,
        )
    )
    if existing is not None:
        from arclight.application.short_drama_budget import root_for_job

        scope = root_for_job(db, existing)
        if (scope.id if scope else None) != short_drama_run_id:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一任务不能更换短剧制作范围", 409)
        if existing.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一幂等键不能用于不同媒体请求", 409)
        manifest = db.get(DependencyManifest, existing.input_manifest_id)
        if (
            manifest is None
            or manifest.snapshot_json["context"].get("art_concept") != art_concept_origin
            or manifest.snapshot_json["context"].get("continuity_revision")
            != continuity_revision_origin
            or manifest.snapshot_json["context"].get("audio_line") != audio_line_origin
        ):
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一任务不能更换受控生成来源", 409)
        return existing
    asset = validate_target(db, project_id, payload)
    from arclight.application.continuity_workflow import freeze_review_binding

    review_binding = freeze_review_binding(db, project_id, payload)
    if review_binding is not None:
        from arclight.application.generation_targets import require_available_shot

        require_available_shot(
            db, project_id, review_binding, payload.episode_id, task_type=payload.task_type
        )
    from arclight.application.media_services import bind_selection, select_for_spec

    service = select_for_spec(db, actor, payload.spec, payload.task_type, frozen_service)
    if service is not None:
        require_project(db, project_id, service.created_by, write=True)
    budget_quote = quote_job(db, payload, actor=actor, frozen_service=frozen_service)
    require_available_target(db, payload.target_asset_id)
    inputs = []
    prompt_sources = []
    for item in payload.inputs:
        version, source = exact_version(db, project_id, item.source_asset_version_id)
        validate_dependency_type(source.asset_type, payload.target_type, item.dependency_type)
        authorized_frame = False
        if (
            short_drama_run_id is not None
            and payload.task_type == "VIDEO"
            and item.dependency_type == "REFERENCE_DEPENDENCY"
            and review_binding is not None
        ):
            from arclight.application.short_drama_visuals import production_first_frame

            authorized_frame = production_first_frame(
                db, project_id, version, short_drama_run_id, review_binding
            )
        if (
            version.stale
            or not authorized_frame
            and version.status not in {"APPROVED", "LOCKED"}
            or source.status == "ARCHIVED"
        ):
            raise DomainError("ASSET_NOT_USABLE", "输入版本必须已批准且有效", 409)
        inputs.append(
            {
                **item.model_dump(mode="json"),
                "asset_id": str(source.id),
                "asset_type": source.asset_type,
                "asset_name": source.name,
                "version_no": version.version_no,
                "checksum": version.checksum,
            }
        )
        if payload.task_type == "IMAGE" and source.asset_type in {
            "CHARACTER",
            "SCENE",
            "PROP",
            "STYLE",
        }:
            asset_snapshot = version.metadata_json.get("asset_snapshot")
            if (
                not isinstance(asset_snapshot, dict)
                or asset_snapshot.get("id") != str(source.id)
                or asset_snapshot.get("project_id") != str(project_id)
                or asset_snapshot.get("asset_type") != source.asset_type
                or not isinstance(asset_snapshot.get("name"), str)
            ):
                raise DomainError(
                    "PROMPT_SOURCE_SNAPSHOT_MISSING",
                    "精确资产版本缺少匹配的提示词资料快照，请创建新版本",
                    409,
                )
            prompt_sources.append(
                {
                    "source_key": f"asset_version:{version.id}",
                    "source_type": "ASSET_VERSION",
                    "asset_type": source.asset_type,
                    "asset_id": str(source.id),
                    "asset_version_id": str(version.id),
                    "version_no": version.version_no,
                    "checksum": version.checksum,
                    "asset_snapshot_sha256": digest(asset_snapshot),
                    "dependency_type": item.dependency_type,
                    "selectors": item.selectors,
                    "asset_snapshot": asset_snapshot,
                }
            )
    capabilities = resolve_capabilities(payload.spec.provider, payload.spec.model)
    effective_spec = payload.spec
    # Only authorized replay supplies a composition from its checked immutable manifest.
    prompt_composition = deepcopy(frozen_prompt_composition)
    if payload.task_type == "IMAGE" and prompt_composition is None:
        visual = visual_bible_state(db, project)
        visual_snapshot = {
            "source_key": (
                f"visual_bible:{visual['id']}" if visual["id"] else "visual_bible:legacy:0"
            ),
            "source_type": ("VISUAL_BIBLE_VERSION" if visual["id"] else "LEGACY_STYLE_GUIDE"),
            "version_id": str(visual["id"]) if visual["id"] else None,
            "version_no": visual["version_no"],
            "fields": visual["fields"],
            "locked_fields": visual["locked_fields"],
            "snapshot_sha256": digest(
                {
                    "version_id": str(visual["id"]) if visual["id"] else None,
                    "version_no": visual["version_no"],
                    "fields": visual["fields"],
                    "locked_fields": visual["locked_fields"],
                    "legacy_snapshot": visual["legacy_snapshot"],
                }
            ),
        }
        composed_prompt, composed_negative, prompt_composition = compose_image_prompt(
            prompt=payload.spec.prompt,
            negative_prompt=payload.spec.negative_prompt,
            context=payload.spec.context,
            parameters=payload.spec.parameters,
            output_spec=payload.spec.output_spec,
            seed=payload.spec.seed,
            variation=payload.spec.variation,
            batch_size=payload.spec.batch_size,
            asset_sources=prompt_sources,
            visual_bible=visual_snapshot,
        )
        try:
            effective_spec = GenerationSpec.model_validate(
                payload.spec.model_dump(mode="json")
                | {"prompt": composed_prompt, "negative_prompt": composed_negative}
            )
        except ValidationError as error:
            raise DomainError(
                "PROMPT_COMPOSITION_INVALID", "组合后的提示词不符合媒体输入合同", 422
            ) from error
    validate_capabilities(capabilities, effective_spec, payload.task_type, reference_count(inputs))
    video_images = []
    if payload.task_type == "VIDEO":
        if "video_images" in payload.spec.context:
            raise DomainError("VIDEO_INPUT_INVALID", "视频首帧须使用精确参考依赖", 422)
        video_images = freeze_video_images(
            db,
            project_id,
            actor,
            payload.episode_id,
            inputs,
            short_drama_run_id=short_drama_run_id,
            shot_target=review_binding,
        )
    configuration = service.configuration if service else None
    manifest = DependencyManifest(
        project_id=project_id,
        target_asset_id=asset.id if asset else None,
        creation_key=uuid4(),
        request_fingerprint=fingerprint,
        created_by=actor,
        snapshot_json={
            "schema_version": 1,
            "invalidation_seq": db.scalar(
                select(func.max(DependencyInvalidation.event_no)).where(
                    DependencyInvalidation.project_id == project_id
                )
            )
            or 0,
            "target": {
                "asset_id": str(asset.id) if asset else None,
                "asset_type": payload.target_type,
                "episode_id": str(payload.episode_id) if payload.episode_id else None,
                "expected_asset": payload.expected_asset.model_dump(mode="json")
                if payload.expected_asset
                else None,
            },
            "inputs": inputs,
            "context": {
                "generation": effective_spec.model_dump(mode="json"),
                **({"video_images": video_images} if video_images else {}),
                **({"provider_configuration": configuration} if configuration else {}),
                "origin": "generation_request",
                **({"continuity_review": review_binding} if review_binding else {}),
                **({"art_concept": deepcopy(art_concept_origin)} if art_concept_origin else {}),
                **(
                    {"continuity_revision": deepcopy(continuity_revision_origin)}
                    if continuity_revision_origin is not None
                    else {}
                ),
                **(
                    {"audio_line": deepcopy(audio_line_origin)}
                    if audio_line_origin is not None
                    else {}
                ),
                "provider_capabilities": capabilities.model_dump(mode="json"),
                "budget_quote": budget_quote.model_dump(mode="json") if budget_quote else None,
                **(
                    {"prompt_composition": prompt_composition}
                    if prompt_composition is not None
                    else {}
                ),
            },
        },
    )
    db.add(manifest)
    db.flush()
    job = GenerationJob(
        project_id=project_id,
        episode_id=payload.episode_id,
        target_type=payload.target_type,
        target_asset_id=payload.target_asset_id,
        input_manifest_id=manifest.id,
        task_type=payload.task_type,
        provider=payload.spec.provider,
        model=payload.spec.model,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        created_by=actor,
        expected_asset_revision=asset.revision if asset else None,
        expected_lock_version=asset.lock_version if asset else None,
        expected_current_version_id=asset.current_version_id if asset else None,
    )
    db.add(job)
    db.flush()
    bind_selection(db, job, service)
    from arclight.persistence.generation_budget import GenerationBudgetQuote

    if budget_quote is not None:
        db.add(
            GenerationBudgetQuote(
                job_id=job.id,
                maximum_cost=budget_quote.maximum_cost,
                currency=budget_quote.currency,
                pricing_version=budget_quote.pricing_version,
                candidate_count=payload.spec.batch_size,
            )
        )
    from arclight.application.generation_accounting import reserve_next

    db.flush()
    if short_drama_run_id is not None:
        from arclight.application.short_drama_budget import bind_media_job

        bind_media_job(db, short_drama_run_id, job)
    reserve_next(db, job, budget_quote.maximum_cost if budget_quote else None)
    from arclight.application.production_versions import capture_media

    capture_media(
        db,
        project_id,
        job.id,
        manifest.id,
        payload.task_type,
        effective_spec,
        inputs,
        prompt_composition=prompt_composition,
    )
    schedule_for(db, job, execution_policy)
    from arclight.application.generation_queues import classify

    classify(db, job, payload)
    enqueue_dispatch(db, job)
    audit(
        db,
        project_id,
        actor,
        "generation.created",
        job.id,
        {
            "input_manifest_id": str(manifest.id),
            "target_type": job.target_type,
            "provider": job.provider,
            "model": job.model,
        },
    )
    return job
