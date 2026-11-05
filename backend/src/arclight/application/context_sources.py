"""Stable source identities and authorized object references; no storage locators."""

from copy import deepcopy
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.dependencies import exact_version
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.versions import digest
from arclight.domain.visual_bible import FIELD_NAMES
from arclight.persistence.models import (
    DependencyManifest,
    DependencyOutput,
    Episode,
    StoredFile,
    StoryAnalysis,
    StoryEntry,
    StoryOutline,
    StorySource,
    StorySourceRevision,
    VisualBibleVersion,
)


def invalid() -> None:
    raise DomainError("CONTEXT_REFERENCE_INVALID", "上下文精确来源缺失或不匹配", 409)


def image_reference(db: Session, project_id: UUID, version_id: UUID) -> dict | None:
    version, _ = exact_version(db, project_id, version_id)
    row = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version_id))
    if row is None:
        if version.metadata_json.get("media"):
            invalid()
        return None  # A text/profile registry version need not have an image object.
    if row.project_id != project_id or row.checksum != version.checksum:
        invalid()
    if row.media_type not in {"image/png", "image/jpeg"}:
        return None
    return {
        "kind": "CONTROLLED_IMAGE",
        "file_id": str(row.id),
        "asset_version_id": str(version_id),
        "checksum": row.checksum,
        "byte_size": row.byte_size,
        "media_type": row.media_type,
    }


def source_snapshot(db: Session, project_id: UUID, identity: UUID) -> dict:
    row = db.get(StorySource, identity)
    episode = db.get(Episode, row.episode_id) if row else None
    if episode is None or episode.project_id != project_id:
        raise DomainError("NOT_FOUND", "上下文故事来源不存在", 404)
    assert row is not None
    revision = db.scalar(select(StorySourceRevision).where(StorySourceRevision.source_id == row.id))
    if revision is None:
        invalid()
    assert revision is not None
    return {
        "kind": "STORY_SOURCE",
        "source_id": str(row.id),
        "episode_id": str(row.episode_id),
        "revision_id": str(revision.id),
        "root_source_id": str(revision.root_source_id),
        "parent_source_id": str(revision.parent_source_id) if revision.parent_source_id else None,
        "version_no": revision.version_no,
        "sha256": row.sha256,
        "byte_size": row.byte_size,
        "encoding": row.encoding,
        "name": row.original_name,
        "origin_kind": revision.origin_kind,
        "rights_status": revision.rights_status,
    }


def asset_sources(db: Session, project_id: UUID, assets: list[dict]) -> list[dict]:
    sources = []
    for asset in assets:
        identity = UUID(asset["asset_version_id"])
        version, _ = exact_version(db, project_id, identity)
        if version.checksum != asset["checksum"] or str(version.asset_id) != asset["asset_id"]:
            invalid()
        output = db.scalar(
            select(DependencyOutput).where(DependencyOutput.asset_version_id == identity)
        )
        manifest = db.get(DependencyManifest, output.dependency_manifest_id) if output else None
        if output and (manifest is None or manifest.project_id != project_id):
            invalid()
        sources.append(
            {
                "kind": "ASSET_VERSION",
                "asset_id": asset["asset_id"],
                "asset_version_id": str(identity),
                "checksum": version.checksum,
                "version_no": version.version_no,
                "created_by": version.created_by,
                "metadata_sha256": digest(version.metadata_json),
                "provenance": deepcopy(version.metadata_json.get("provenance")),
                "media_origin": {
                    field: version.metadata_json.get("media", {}).get(field)
                    for field in ("original_name", "source_note", "rights_note", "rights_confirmed")
                }
                if version.metadata_json.get("media")
                else None,
                "dependency_manifest_id": str(output.dependency_manifest_id) if output else None,
                "dependency_manifest_sha256": digest(manifest.snapshot_json) if manifest else None,
                "object_ref": image_reference(db, project_id, identity),
            }
        )
    return sources


def capture_sources(db: Session, project_id: UUID, inputs: dict, context: dict) -> list[dict]:
    """Resolve only IDs already selected by the authorized task, never current pointers."""
    sources = asset_sources(db, project_id, context["required"].get("assets", []))
    from arclight.application.agent_preferences import preference_source

    sources.extend(preference_source(value) for value in context["required"].get("preferences", []))
    containers = [inputs]
    outline = inputs.get("outline_context")
    if outline:
        row = db.get(StoryOutline, UUID(outline["version_id"]))
        if row is None or row.project_id != project_id:
            invalid()
        assert row is not None
        sources.append(
            {
                "kind": "OUTLINE",
                "id": str(row.id),
                "version_no": row.version_no,
                "sha256": digest(row.content_json),
            }
        )
        containers.append(outline["source_snapshot"])
    if "source_snapshot" in inputs:
        containers.append(inputs["source_snapshot"])
    seen = set()
    for container in containers:
        proof = container.get("source_revision")
        if proof and proof["source_id"] not in seen:
            value = source_snapshot(db, project_id, UUID(proof["source_id"]))
            if value["sha256"] != proof["sha256"] or value["version_no"] != proof["version_no"]:
                invalid()
            sources.append(value)
            seen.add(proof["source_id"])
        if container.get("analysis_id"):
            analysis = db.get(StoryAnalysis, UUID(container["analysis_id"]))
            episode = db.get(Episode, analysis.episode_id) if analysis else None
            if episode is None or episode.project_id != project_id:
                invalid()
            assert analysis is not None
            sources.append(
                {
                    "kind": "ANALYSIS",
                    "id": str(analysis.id),
                    "version_no": analysis.version_no,
                    "sha256": digest(analysis.content_json),
                }
            )
    for entry in context["required"].get("story", {}).get("entries", []):
        sources.append(
            {
                "kind": "STORY_ENTRY",
                "id": entry["id"],
                "revision": entry["revision"],
                "sha256": digest(entry),
            }
        )
    style = context["required"]["global"].get("style", {})
    if style.get("version_id"):
        sources.append(
            {
                "kind": "VISUAL_BIBLE",
                "id": style["version_id"],
                "version_no": style["version_no"],
                "sha256": digest(
                    {"fields": style["fields"], "locked_fields": style["locked_fields"]}
                ),
            }
        )
    return sources


def validate_sources(db: Session, project_id: UUID, sources: list[dict]) -> None:
    """Mutable directory revisions may advance; exact immutable identities must remain."""
    for source in sources:
        kind = source["kind"]
        if kind == "ASSET_VERSION":
            version, _ = exact_version(db, project_id, UUID(source["asset_version_id"]))
            if (
                str(version.asset_id) != source["asset_id"]
                or version.checksum != source["checksum"]
                or digest(version.metadata_json) != source["metadata_sha256"]
                or version.version_no != source["version_no"]
            ):
                invalid()
            reference = source.get("object_ref")
            if reference is not None and image_reference(db, project_id, version.id) != reference:
                invalid()
            output = db.scalar(
                select(DependencyOutput).where(DependencyOutput.asset_version_id == version.id)
            )
            identity = source.get("dependency_manifest_id")
            if (str(output.dependency_manifest_id) if output else None) != identity:
                invalid()
            if identity:
                manifest = db.get(DependencyManifest, UUID(identity))
                if (
                    manifest is None
                    or manifest.project_id != project_id
                    or digest(manifest.snapshot_json) != source["dependency_manifest_sha256"]
                ):
                    invalid()
        elif kind == "STORY_SOURCE":
            if source_snapshot(db, project_id, UUID(source["source_id"])) != source:
                invalid()
        elif kind == "AGENT_PREFERENCE":
            from arclight.application.agent_preferences import validate_preference_source

            validate_preference_source(db, project_id, source)
        elif kind in {"STORY_ENTRY", "VISUAL_BIBLE", "OUTLINE", "ANALYSIS"}:
            model = {
                "STORY_ENTRY": StoryEntry,
                "VISUAL_BIBLE": VisualBibleVersion,
                "OUTLINE": StoryOutline,
                "ANALYSIS": StoryAnalysis,
            }[kind]
            row = db.get(model, UUID(source["id"]))
            owner_project = getattr(row, "project_id", None)
            if isinstance(row, StoryAnalysis):
                episode = db.get(Episode, row.episode_id)
                owner_project = episode.project_id if episode else None
            if row is None or owner_project != project_id:
                invalid()
            if isinstance(row, VisualBibleVersion):
                value = {
                    "fields": {field: getattr(row, field) for field in FIELD_NAMES},
                    "locked_fields": row.locked_fields,
                }
                if digest(value) != source["sha256"] or row.version_no != source["version_no"]:
                    invalid()
            elif isinstance(row, (StoryOutline, StoryAnalysis)):
                if (
                    digest(row.content_json) != source["sha256"]
                    or row.version_no != source["version_no"]
                ):
                    invalid()
        else:
            invalid()


def verify_images(db: Session, settings: Settings, project_id: UUID, sources: list[dict]) -> None:
    from arclight.application.asset_objects import read_object
    from arclight.persistence.models import AssetVersion

    for source in sources:
        reference = source.get("object_ref") if source["kind"] == "ASSET_VERSION" else None
        if reference is None:
            continue
        version = db.get(AssetVersion, UUID(source["asset_version_id"]))
        if version is None:
            invalid()
        assert version is not None
        data = read_object(
            settings, project_id, version.id, version.object_key, reference["checksum"]
        )
        if len(data) != reference["byte_size"]:
            invalid()


def public_startup(inputs: dict) -> dict:
    """The checkpoint and trace share one projection of the original public inputs."""
    value = deepcopy({name: item for name, item in inputs.items() if not name.startswith("_")})
    containers = [value]
    outline = value.get("outline_context")
    if isinstance(outline, dict):
        containers.append(outline.get("source_snapshot", {}))
    if isinstance(value.get("source_snapshot"), dict):
        containers.append(value["source_snapshot"])
    for container in containers:
        proof = container.get("source_revision")
        if isinstance(proof, dict):
            proof.pop("object_ref", None)
    return value
