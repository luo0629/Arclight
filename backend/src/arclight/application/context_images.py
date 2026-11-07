"""One controlled object reader shared by context previews and model consumption."""

from uuid import UUID

from sqlalchemy.orm import Session

from arclight.application.asset_objects import read_object
from arclight.application.context_builder import resolve_assets
from arclight.application.context_evidence import read_trace
from arclight.application.context_sources import image_reference
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelCapabilities, ContextImage
from arclight.domain.context import ContextLimits
from arclight.persistence.models import AssetVersion, Episode, VisualAsset, WorkflowRun


def load_context_images(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    stage: str,
    actor: str,
    file_ids: list[UUID],
    *,
    through_round: int | None = None,
    capabilities: AgentModelCapabilities | None = None,
) -> dict[UUID, ContextImage]:
    maximum = capabilities.max_input_images if capabilities else 8
    per_image = capabilities.max_image_bytes if capabilities else 1048576
    total = capabilities.max_total_image_bytes if capabilities else 4194304
    if len(file_ids) > maximum:
        raise DomainError("GATEWAY_CAPABILITY_REJECTED", "图片数量超出模型输入能力", 422)
    if not file_ids:
        return {}
    trace = read_trace(db, run, stage, actor, through_round)
    sources = list((trace["startup"] or {}).get("sources", []))
    for item in trace["evidence"]:
        sources.extend(item["sources"])
    references = {
        item["object_ref"]["file_id"]: item["object_ref"]
        for item in sources
        if item["kind"] == "ASSET_VERSION" and item.get("object_ref")
    }
    selected = []
    for file_id in file_ids:
        reference = references.get(str(file_id))
        if reference is None:
            raise DomainError("NOT_FOUND", "图片不属于当前上下文", 404)
        if not 0 < reference["byte_size"] <= per_image:
            raise DomainError("GATEWAY_CAPABILITY_REJECTED", "图片超出模型单图输入限额", 422)
        selected.append(reference)
    if sum(item["byte_size"] for item in selected) > total:
        raise DomainError("GATEWAY_CAPABILITY_REJECTED", "图片超出模型总输入限额", 422)
    episode = db.get(Episode, run.episode_id)
    if episode is None:
        raise DomainError("CONTEXT_REFERENCE_INVALID", "上下文作用范围缺失", 409)
    candidate = trace["task_context"]["scope"].get("candidate_version_id")
    ordinary_versions = set()
    for item in selected:
        version = db.get(AssetVersion, UUID(item["asset_version_id"]))
        if (
            version is not None
            and version.status not in {"APPROVED", "LOCKED"}
            and version.metadata_json.get("required_review") == "ART_PROPOSAL"
        ):
            from arclight.application.art_concept_assets import read_concept_reference

            fresh = read_concept_reference(db, settings, run.id, version.id)
            if fresh["image"] != item:
                raise DomainError("CONTEXT_REFERENCE_INVALID", "概念图与原取证原图不一致", 409)
        else:
            ordinary_versions.add(item["asset_version_id"])
    resolve_assets(
        db,
        episode.project_id,
        ordinary_versions,
        ContextLimits.model_validate(trace["task_context"]["limits"]),
        candidate=UUID(candidate) if candidate else None,
        follow_profile_references=False,
    )
    images = {}
    for reference in selected:
        identity = UUID(reference["file_id"])
        if identity in images:
            continue
        version_id = UUID(reference["asset_version_id"])
        if image_reference(db, episode.project_id, version_id) != reference:
            raise DomainError("CONTEXT_REFERENCE_INVALID", "上下文图片引用不匹配", 409)
        version = db.get(AssetVersion, version_id)
        assert version is not None
        asset = db.get(VisualAsset, version.asset_id)
        assert asset is not None
        scope = asset.metadata_json.get("usage_scope", {})
        if scope.get("kind", "PROJECT") != "PROJECT" and str(episode.id) not in scope.get(
            "episode_ids", []
        ):
            raise DomainError("CONTEXT_SCOPE_NOT_ALLOWED", "图片不在当前剧集使用范围", 403)
        data = read_object(
            settings, episode.project_id, version.id, version.object_key, reference["checksum"]
        )
        if len(data) != reference["byte_size"]:
            raise DomainError("CONTEXT_REFERENCE_INVALID", "上下文图片长度不匹配", 409)
        images[identity] = ContextImage(identity, reference["media_type"], data)
    return images
