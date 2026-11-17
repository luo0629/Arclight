"""Immutable story snapshots and bounded projections, separate from the production DAG."""

import json
from collections import defaultdict
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from arclight.core.errors import DomainError

MAX_LEDGER_BYTES = 1_048_576
MAX_QUERY_BYTES = 65_536
StoryTime = Annotated[int, Field(strict=True, ge=-1_000_000_000, le=1_000_000_000)]
Position = tuple[int, int, int]
LedgerText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
StateKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class LedgerValue(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, revalidate_instances="always"
    )


class LedgerSceneTime(LedgerValue):
    id: UUID
    episode_id: UUID
    episode_no: int = Field(strict=True, ge=1, le=10_000)
    script_version_id: UUID
    scene_id: UUID
    scene_no: int = Field(strict=True, ge=1, le=10_000)
    segment_no: int = Field(default=1, strict=True, ge=1, le=1000)
    story_time: StoryTime

    @property
    def presentation(self) -> Position:
        return self.episode_no, self.scene_no, self.segment_no


class LedgerKnowledge(LedgerValue):
    character_id: UUID
    story_start: StoryTime
    story_end: StoryTime | None = None

    @model_validator(mode="after")
    def ordered_interval(self) -> Self:
        if self.story_end is not None and self.story_end <= self.story_start:
            raise ValueError("Knowledge intervals must be nonempty [start, end)")
        return self


def intervals_overlap[Boundary: (int, Position)](
    left_start: Boundary,
    left_end: Boundary | None,
    right_start: Boundary,
    right_end: Boundary | None,
) -> bool:
    return (left_end is None or right_start < left_end) and (
        right_end is None or left_start < right_end
    )


class LedgerEntry(LedgerValue):
    id: UUID
    kind: Literal["EVENT", "RELATIONSHIP", "KNOWLEDGE", "INJURY", "COSTUME", "PROP_OWNERSHIP"]
    status: Literal["AUTHOR_PLANNED", "PROPOSED", "CONFIRMED"]
    subject_id: UUID | None = None
    target_id: UUID | None = None
    state_key: StateKey
    value: LedgerText
    story_start: StoryTime
    story_end: StoryTime | None = None
    disclosure_start: UUID
    disclosure_end: UUID | None = None
    source_script_version_id: UUID | None = None
    source_scene_id: UUID | None = None
    known_by: list[LedgerKnowledge] = Field(default_factory=list, max_length=100)
    causes: list[UUID] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def complete_entry(self) -> Self:
        if self.story_end is not None and self.story_end <= self.story_start:
            raise ValueError("Story intervals must be nonempty [start, end)")
        if (self.source_script_version_id is None) != (self.source_scene_id is None):
            raise ValueError("An exact source requires both script and scene")
        if self.status != "AUTHOR_PLANNED" and self.source_script_version_id is None:
            raise ValueError("Proposed and confirmed entries require an exact source")
        if self.status != "AUTHOR_PLANNED" and self.kind != "EVENT" and self.subject_id is None:
            raise ValueError("State entries require a subject")
        if (
            self.kind == "RELATIONSHIP"
            and self.status != "AUTHOR_PLANNED"
            and (self.target_id is None or self.subject_id == self.target_id)
        ):
            raise ValueError("A relationship requires distinct directed subjects")
        if len(set(self.causes)) != len(self.causes) or self.id in self.causes:
            raise ValueError("Causal references must be unique and cannot reference themselves")
        for index, left in enumerate(self.known_by):
            for right in self.known_by[index + 1 :]:
                if left.character_id == right.character_id and intervals_overlap(
                    left.story_start, left.story_end, right.story_start, right.story_end
                ):
                    raise ValueError("Knowledge intervals for one character cannot overlap")
        return self


class StoryLedgerContent(LedgerValue):
    schema_version: Literal["1.0"] = "1.0"
    scene_times: list[LedgerSceneTime] = Field(default_factory=list, max_length=2000)
    entries: list[LedgerEntry] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def consistent_snapshot(self) -> Self:
        mappings = {item.id: item for item in self.scene_times}
        entries = {item.id: item for item in self.entries}
        if len(mappings) != len(self.scene_times) or len(entries) != len(self.entries):
            raise ValueError("Mapping and entry identities must be unique")
        presentations = [item.presentation for item in self.scene_times]
        exact_scenes = [
            (item.episode_id, item.script_version_id, item.scene_id, item.segment_no)
            for item in self.scene_times
        ]
        if len(set(presentations)) != len(presentations):
            raise ValueError("A presentation position cannot identify multiple exact scenes")
        if len(set(exact_scenes)) != len(exact_scenes):
            raise ValueError("Each exact scene segment has one story-time mapping")
        episode_numbers: dict[UUID, int] = {}
        numbered_episodes: dict[int, UUID] = {}
        scene_positions: dict[tuple[UUID, UUID], tuple[UUID, int]] = {}
        script_episodes: dict[UUID, UUID] = {}
        for item in self.scene_times:
            if (
                episode_numbers.setdefault(item.episode_id, item.episode_no) != item.episode_no
                or numbered_episodes.setdefault(item.episode_no, item.episode_id) != item.episode_id
                or script_episodes.setdefault(item.script_version_id, item.episode_id)
                != item.episode_id
                or scene_positions.setdefault(
                    (item.script_version_id, item.scene_id), (item.episode_id, item.scene_no)
                )
                != (item.episode_id, item.scene_no)
            ):
                raise ValueError("Exact episode and scene positions must remain consistent")
        sources = {(item.script_version_id, item.scene_id) for item in self.scene_times}
        for entry in self.entries:
            if entry.disclosure_start not in mappings or (
                entry.disclosure_end is not None and entry.disclosure_end not in mappings
            ):
                raise ValueError("Disclosure boundaries must reference exact scene mappings")
            if entry.disclosure_end is not None and (
                mappings[entry.disclosure_end].presentation
                <= mappings[entry.disclosure_start].presentation
            ):
                raise ValueError("Disclosure intervals must be nonempty [start, end)")
            if (
                entry.source_script_version_id is not None
                and (entry.source_script_version_id, entry.source_scene_id) not in sources
            ):
                raise ValueError("Exact source script and scene must occur in the mapping")
            for cause_id in entry.causes:
                cause = entries.get(cause_id)
                if cause is None or cause.story_start > entry.story_start:
                    raise ValueError("A cause must exist and cannot occur after its effect")
                if entry.status == "CONFIRMED" and cause.status != "CONFIRMED":
                    raise ValueError("A confirmed fact cannot depend on an unconfirmed cause")
        validate_causality(self.entries)
        validate_state_conflicts(self.entries, mappings)
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode()) > (
            MAX_LEDGER_BYTES
        ):
            raise ValueError("The story ledger snapshot exceeds its byte limit")
        return self


def validate_causality(entries: list[LedgerEntry]) -> None:
    remaining = {item.id: len(item.causes) for item in entries}
    effects: dict[UUID, list[UUID]] = defaultdict(list)
    for item in entries:
        for cause in item.causes:
            effects[cause].append(item.id)
    ready = [identity for identity, count in remaining.items() if count == 0]
    visited = 0
    while ready:
        identity = ready.pop()
        visited += 1
        for effect in effects[identity]:
            remaining[effect] -= 1
            if remaining[effect] == 0:
                ready.append(effect)
    if visited != len(entries):
        raise ValueError("Narrative causality cannot contain cycles")


def validate_state_conflicts(
    entries: list[LedgerEntry], mappings: dict[UUID, LedgerSceneTime]
) -> None:
    states: dict[tuple, list[LedgerEntry]] = defaultdict(list)
    for item in entries:
        if item.kind == "EVENT" or item.status != "CONFIRMED":
            continue
        # Ownership is keyed by the prop, never by its proposed owner.
        identity: tuple = (item.kind, item.subject_id, item.state_key)
        if item.kind in {"RELATIONSHIP", "KNOWLEDGE"}:
            identity += (item.target_id,)
        for previous in states[identity]:
            if (item.value, item.target_id) == (previous.value, previous.target_id):
                continue
            story_overlap = intervals_overlap(
                item.story_start, item.story_end, previous.story_start, previous.story_end
            )
            disclosure_overlap = intervals_overlap(
                mappings[item.disclosure_start].presentation,
                mappings[item.disclosure_end].presentation if item.disclosure_end else None,
                mappings[previous.disclosure_start].presentation,
                mappings[previous.disclosure_end].presentation if previous.disclosure_end else None,
            )
            if story_overlap and disclosure_overlap:
                raise ValueError("Confirmed state values conflict in an overlapping scope")
        states[identity].append(item)


class LedgerQuery(LedgerValue):
    episode_id: UUID
    script_version_id: UUID
    scene_id: UUID
    segment_no: int = Field(default=1, strict=True, ge=1, le=1000)
    perspective: Literal["AUTHOR", "CHARACTER"]
    character_id: UUID | None = None
    include_planned: bool = Field(default=False, strict=True)
    include_proposed: bool = Field(default=False, strict=True)
    limit: int = Field(default=100, strict=True, ge=1, le=100)
    offset: int = Field(default=0, strict=True, ge=0, le=1000)

    @model_validator(mode="after")
    def explicit_perspective(self) -> Self:
        if self.perspective == "CHARACTER":
            if self.character_id is None or self.include_planned or self.include_proposed:
                raise ValueError(
                    "A character requires an identity and cannot read plans or proposals"
                )
        elif self.character_id is not None:
            raise ValueError("An author query cannot carry a character identity")
        return self


def entry_visible(
    entry: LedgerEntry, target: LedgerSceneTime, mappings: dict[UUID, LedgerSceneTime]
) -> bool:
    return (
        entry.story_start <= target.story_time
        and (entry.story_end is None or target.story_time < entry.story_end)
        and mappings[entry.disclosure_start].presentation <= target.presentation
        and (
            entry.disclosure_end is None
            or target.presentation < mappings[entry.disclosure_end].presentation
        )
    )


def query_ledger(content: StoryLedgerContent, query: LedgerQuery, *, approved_root: bool) -> dict:
    """Project narrative data; only the service may supply approval authority."""
    content = StoryLedgerContent.model_validate(content)
    query = LedgerQuery.model_validate(query)
    target = next(
        (
            item
            for item in content.scene_times
            if (
                item.episode_id,
                item.script_version_id,
                item.scene_id,
                item.segment_no,
            )
            == (query.episode_id, query.script_version_id, query.scene_id, query.segment_no)
        ),
        None,
    )
    if target is None:
        raise DomainError("STORY_LEDGER_TARGET_INVALID", "目标场景片段缺少精确时序映射", 422)
    mappings = {item.id: item for item in content.scene_times}
    groups: dict[str, list[LedgerEntry]] = {"facts": [], "planned": [], "proposals": []}
    for entry in sorted(content.entries, key=lambda item: (item.story_start, str(item.id))):
        if entry.status == "AUTHOR_PLANNED":
            if query.perspective == "AUTHOR" and query.include_planned:
                groups["planned"].append(entry)
            continue
        if not entry_visible(entry, target, mappings):
            continue
        if entry.status == "CONFIRMED" and approved_root:
            if query.perspective == "CHARACTER" and not any(
                item.character_id == query.character_id
                and item.story_start <= target.story_time
                and (item.story_end is None or target.story_time < item.story_end)
                for item in entry.known_by
            ):
                continue
            groups["facts"].append(entry)
        elif query.perspective == "AUTHOR" and query.include_proposed:
            groups["proposals"].append(entry)
    selected: list[tuple[str, LedgerEntry]] = [
        (group, entry) for group, entries in groups.items() for entry in entries
    ]
    total = len(selected)
    selected = selected[query.offset : query.offset + query.limit]
    while True:
        visible_ids = {entry.id for _, entry in selected}
        result: dict = {
            "target": target.model_dump(mode="json"),
            "perspective": query.perspective,
            "character_id": str(query.character_id) if query.character_id else None,
            "facts": [],
            "planned": [],
            "proposals": [],
            "total": total,
            "limit": query.limit,
            "offset": query.offset,
            "returned_count": len(selected),
            "next_offset": query.offset + len(selected)
            if query.offset + len(selected) < total
            else None,
            "truncated": query.offset + len(selected) < total,
        }
        for group, entry in selected:
            value = entry.model_dump(mode="json", exclude={"known_by"})
            value["causes"] = [
                str(identity) for identity in entry.causes if identity in visible_ids
            ]
            if group == "proposals":
                value["status"] = "PROPOSED"
            result[group].append(value)
        if len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_QUERY_BYTES:
            return result
        selected.pop()
