"""Preview deterministic matches; create identities only after an exact human decision."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC
from typing import Literal, cast
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContent
from arclight.agents.story_analysis import StoryAnalysisContent
from arclight.api.collaboration import audit
from arclight.api.schemas import Input
from arclight.application.commands import require_episode
from arclight.application.story_context import build_story_context, validate_entry
from arclight.core.errors import DomainError
from arclight.domain.character_names import canonical_name
from arclight.domain.story_bible import StoryEntryInput
from arclight.persistence.models import (
    CharacterConfirmation,
    Episode,
    Script,
    StoryAnalysis,
    StoryEntry,
    WorkflowRun,
)

SourceType = Literal["scripts", "story-analyses"]


class CharacterDecision(Input):
    # Source labels are exact contract keys, including any original whitespace.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    source_name: str = Field(min_length=1, max_length=100)
    expected_fingerprint: str = Field(pattern="^[a-f0-9]{64}$")
    decision: Literal["REUSE", "CREATE"]
    entry_id: UUID | None = None

    @model_validator(mode="after")
    def check_target(self):
        if (self.decision == "REUSE") != (self.entry_id is not None):
            raise ValueError("复用须选择已有角色；新增不得指定角色 ID")
        return self


@dataclass(frozen=True)
class Candidate:
    name: str
    aliases: list[str]
    asset: dict | None = None


def digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def load_source(db: Session, episode_id: UUID, source_type: SourceType, version_id: UUID):
    model = Script if source_type == "scripts" else StoryAnalysis
    source = cast(Script | StoryAnalysis | None, db.get(model, version_id))
    if source is None or source.episode_id != episode_id:
        raise DomainError("NOT_FOUND", "角色来源版本不存在", 404)
    if isinstance(source, Script):
        parsed = ScriptContent.model_validate(source.content_json)
        assets = {item.character: item.asset.model_dump() for item in parsed.character_assets}
        candidates = [Candidate(name, [], assets.get(name)) for name in parsed.characters]
    else:
        analysis = StoryAnalysisContent.model_validate(source.content_json)
        candidates = [Candidate(item.name, item.aliases) for item in analysis.characters]
    # Narration is never a person, even if a legacy contract listed it as one.
    return source, [item for item in candidates if canonical_name(item.name) != "旁白"]


def character_rows(db: Session, project_id: UUID) -> list[StoryEntry]:
    return list(
        db.scalars(
            select(StoryEntry)
            .where(StoryEntry.project_id == project_id, StoryEntry.kind == "CHARACTER")
            .order_by(StoryEntry.id)
        )
    )


def entry_snapshot(row: StoryEntry) -> dict:
    value = StoryEntryInput(kind="CHARACTER", name=row.name, **row.data_json)
    return {
        "id": str(row.id),
        "revision": row.revision,
        "archived": row.archived,
        **value.model_dump(mode="json"),
    }


def matches(candidate: Candidate, entries: list[dict]) -> list[dict]:
    names = {canonical_name(name) for name in [candidate.name, *candidate.aliases]}
    return [
        entry
        for entry in entries
        if names & {canonical_name(name) for name in [entry["name"], *entry["aliases"]]}
    ]


def confirmation_query(source_type: SourceType, version_id: UUID):
    column = (
        CharacterConfirmation.script_id
        if source_type == "scripts"
        else CharacterConfirmation.analysis_id
    )
    return select(CharacterConfirmation).where(column == version_id)


def read_confirmation(row: CharacterConfirmation) -> dict:
    return {
        "id": str(row.id),
        "created_by": row.created_by,
        "created_at": (
            row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
        )
        .astimezone(UTC)
        .isoformat(),
        **row.snapshot_json,
    }


def executing(db: Session, project_id: UUID) -> bool:
    return (
        db.scalar(
            select(WorkflowRun.id)
            .join(Episode)
            .where(Episode.project_id == project_id, WorkflowRun.status.in_(["QUEUED", "RUNNING"]))
            .limit(1)
        )
        is not None
    )


def preview(
    db: Session, episode_id: UUID, source_type: SourceType, version_id: UUID, actor: str
) -> dict:
    episode = require_episode(db, episode_id, actor)
    source, candidates = load_source(db, episode_id, source_type, version_id)
    entries = [entry_snapshot(row) for row in character_rows(db, episode.project_id)]
    confirmations = {
        row.source_name: row for row in db.scalars(confirmation_query(source_type, version_id))
    }
    return {
        "source_type": source_type,
        "version_id": str(version_id),
        "fingerprint": digest({"source": source.content_json, "entries": entries}),
        "executing": executing(db, episode.project_id),
        "entries": entries,
        "candidates": [
            {
                "name": candidate.name,
                "aliases": candidate.aliases,
                "matches": [entry["id"] for entry in matches(candidate, entries)],
                "confirmation": read_confirmation(confirmations[candidate.name])
                if candidate.name in confirmations
                else None,
            }
            for candidate in candidates
        ],
    }


def confirm(
    db: Session,
    episode_id: UUID,
    source_type: SourceType,
    version_id: UUID,
    actor: str,
    decision: CharacterDecision,
) -> dict:
    # Project row lock serializes all story CRUD, starts, archival and confirmations.
    episode = require_episode(db, episode_id, actor, write=True)
    source, candidates = load_source(db, episode_id, source_type, version_id)
    candidate = next((item for item in candidates if item.name == decision.source_name), None)
    if candidate is None:
        raise DomainError("INVALID_CHARACTER", "来源版本没有此角色", 422)
    fingerprint = digest(decision.model_dump(mode="json", exclude={"expected_fingerprint"}))
    existing = db.scalar(
        confirmation_query(source_type, version_id).where(
            CharacterConfirmation.source_name == candidate.name
        )
    )
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise DomainError(
                "CHARACTER_ALREADY_CONFIRMED", "此版本角色已确认，请查看确认记录", 409
            )
        return read_confirmation(existing)
    if executing(db, episode.project_id):
        raise DomainError("ACTIVE_WORKFLOW", "请等待项目中的生成任务结束后确认角色", 409)
    rows = character_rows(db, episode.project_id)
    entries = [entry_snapshot(row) for row in rows]
    if digest({"source": source.content_json, "entries": entries}) != decision.expected_fingerprint:
        raise DomainError(
            "STALE_CHARACTER_MATCH", "角色设定已变化，请刷新匹配；当前选择已保留", 409
        )
    if decision.decision == "CREATE":
        if matches(candidate, entries):
            raise DomainError(
                "CHARACTER_NAME_CONFLICT", "已有名称或别名匹配，请复用或恢复已有角色", 409
            )
        value = StoryEntryInput(kind="CHARACTER", name=candidate.name, aliases=candidate.aliases)
        validate_entry(db, episode.project_id, value)
        row = StoryEntry(
            project_id=episode.project_id,
            kind="CHARACTER",
            name=value.name,
            data_json=value.model_dump(mode="json", exclude={"kind", "name"}),
            created_by=actor,
        )
        db.add(row)
        db.flush()
        build_story_context(db, episode.project_id, validate_assets=False)
        audit(
            db,
            episode.project_id,
            actor,
            "story_entry.created",
            row.id,
            {"source_type": source_type, "version_id": str(version_id), "revision": 0},
        )
    else:
        target = next(
            (row for row in rows if row.id == decision.entry_id and not row.archived), None
        )
        if target is None:
            raise DomainError(
                "INVALID_CHARACTER_TARGET", "目标角色不存在、已归档或不属于本项目", 422
            )
        row = target
        if (
            candidate.asset
            and row.data_json.get("asset")
            and candidate.asset != row.data_json["asset"]
        ):
            raise DomainError(
                "CHARACTER_ASSET_CONFLICT", "来源与目标角色绑定的精确资产版本不同，请核对", 409
            )
    record = CharacterConfirmation(
        project_id=episode.project_id,
        script_id=version_id if source_type == "scripts" else None,
        analysis_id=version_id if source_type == "story-analyses" else None,
        source_name=candidate.name,
        entry_id=row.id,
        request_fingerprint=fingerprint,
        created_by=actor,
        snapshot_json={
            "source_type": source_type,
            "version_id": str(version_id),
            "source_sha256": digest(source.content_json),
            "source_name": candidate.name,
            "source_aliases": candidate.aliases,
            "source_asset": candidate.asset,
            "decision": decision.decision,
            "entry": entry_snapshot(row),
        },
    )
    db.add(record)
    db.flush()
    audit(
        db,
        episode.project_id,
        actor,
        "character.confirmed",
        record.id,
        {"source_type": source_type, "version_id": str(version_id), "entry_id": str(row.id)},
    )
    return read_confirmation(record)
