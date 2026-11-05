"""Freeze domain confirmation at capture time; later status changes cannot promote history."""

from uuid import UUID

from sqlalchemy.orm import Session

from arclight.application.dependencies import exact_version
from arclight.core.errors import DomainError
from arclight.domain.agent_memory import MEMORY_VERSION, MemoryReference, MemorySource
from arclight.domain.versions import digest
from arclight.persistence.agent_preferences import AgentPreference
from arclight.persistence.models import StoryEntry, StoryOutline, VisualBibleVersion


def source_identity(source: dict) -> UUID:
    return UUID(source.get("asset_version_id") or source.get("source_id") or source["id"])


def capture_memory_sources(
    db: Session,
    project_id: UUID,
    sources: list[dict],
    *,
    candidate_ids: set[UUID] | None = None,
) -> dict:
    rows = []
    for index, source in enumerate(sources):
        identity = source_identity(source)
        value: dict = {
            "source_index": index,
            "source_id": identity,
            "source_sha256": digest(source),
            "category": "EVIDENCE",
        }
        rule = None
        actor = None
        if source["kind"] == "ASSET_VERSION" and identity not in (candidate_ids or set()):
            version, _ = exact_version(db, project_id, identity)
            if version.status in {"APPROVED", "LOCKED"}:
                rule = "APPROVED_ASSET_VERSION"
        elif source["kind"] == "STORY_ENTRY":
            entry = db.get(StoryEntry, identity)
            if entry is not None and entry.project_id == project_id:
                # The mutable entry's creator need not be its current revision's editor.
                rule = "PROJECT_STORY_ENTRY"
        elif source["kind"] == "VISUAL_BIBLE":
            spec = db.get(VisualBibleVersion, identity)
            if spec is not None and spec.project_id == project_id:
                rule, actor = "PROJECT_VISUAL_SPEC", spec.created_by
        elif source["kind"] == "OUTLINE":
            outline = db.get(StoryOutline, identity)
            if (
                outline is not None
                and outline.project_id == project_id
                and outline.status == "APPROVED"
            ):
                rule, actor = "CONFIRMED_OUTLINE", outline.confirmed_by
        elif source["kind"] == "AGENT_PREFERENCE":
            preference = db.get(AgentPreference, identity)
            if (
                preference is None
                or preference.project_id != project_id
                or preference.value is None
            ):
                raise DomainError("CONTEXT_REFERENCE_INVALID", "偏好确认来源不匹配", 409)
            rule, actor = "USER_CONFIRMED_PREFERENCE", preference.confirmed_by
        if rule is not None:
            value.update(
                category="LONG_TERM_PREFERENCE"
                if source["kind"] == "AGENT_PREFERENCE"
                else "DOMAIN_FACT",
                confirmation_rule=rule,
                confirmed_by=actor,
            )
        rows.append(MemorySource.model_validate(value).model_dump(mode="json"))
    return {"memory_version": MEMORY_VERSION, "memory_sources": rows}


def memory_references(
    container: dict, manifest_id: UUID, number: int
) -> tuple[list[MemoryReference], bool]:
    sources = container["sources"]
    legacy = "memory_version" not in container
    authorities = container.get("memory_sources")
    if legacy:
        if authorities is not None:
            raise DomainError("AGENT_MEMORY_INVALID", "记忆确认元数据不完整", 409)
        authorities = [
            {
                "source_index": index,
                "source_id": str(source_identity(source)),
                "source_sha256": digest(source),
                "category": "EVIDENCE",
            }
            for index, source in enumerate(sources)
        ]
    elif container["memory_version"] != MEMORY_VERSION or not isinstance(authorities, list):
        raise DomainError("AGENT_MEMORY_VERSION_INVALID", "记忆确认版本无效", 409)
    if len(authorities) != len(sources):
        raise DomainError("AGENT_MEMORY_INVALID", "确认来源不能丢失或额外注入", 409)
    rows = []
    for index, (source, authority) in enumerate(zip(sources, authorities, strict=True)):
        row = MemoryReference.model_validate(
            authority | {"manifest_id": manifest_id, "evidence_round": number}
        )
        if (
            row.source_index != index
            or row.source_id != source_identity(source)
            or row.source_sha256 != digest(source)
        ):
            raise DomainError("AGENT_MEMORY_INVALID", "记忆必须引用原清单的精确来源", 409)
        rows.append(row)
    return rows, legacy
