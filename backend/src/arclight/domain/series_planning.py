"""Typed future narrative plans stored in the existing immutable outline history.

The legacy outline model remains unchanged because its schema is part of frozen
workflow component identities. Narrative links never create production DAG edges
or confirmed story facts.
"""

from collections.abc import Hashable, Sequence
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from arclight.agents.story_outline import OutlineEpisode, StoryOutlineContent

PlanText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
PlanId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
    ),
]
EpisodeNumber = Annotated[int, Field(strict=True, ge=1, le=1000)]


class PlanItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, revalidate_instances="always"
    )


class NarrativeThread(PlanItem):
    id: PlanId
    kind: Literal["MAIN", "SUBPLOT"]
    title: str = Field(min_length=1, max_length=200)
    central_conflict: PlanText
    planned_resolution: PlanText


class ArcMilestone(PlanItem):
    episode_no: EpisodeNumber
    change: PlanText


class CharacterArc(PlanItem):
    character_id: UUID
    character_revision: int = Field(strict=True, ge=0)
    starting_goal: PlanText
    internal_conflict: PlanText
    planned_growth: PlanText
    milestones: list[ArcMilestone] = Field(min_length=1, max_length=1000)


class RelationshipArc(PlanItem):
    id: PlanId
    source_character_id: UUID
    target_character_id: UUID
    initial_relationship: PlanText
    planned_development: PlanText
    milestones: list[ArcMilestone] = Field(min_length=1, max_length=1000)


class Foreshadowing(PlanItem):
    id: PlanId
    thread_id: PlanId
    setup_episode_no: EpisodeNumber
    setup: PlanText
    expected_payoff_episode_no: EpisodeNumber
    planned_payoff: PlanText


class ThreadBeat(PlanItem):
    thread_id: PlanId
    progression: PlanText


class EpisodeProgression(PlanItem):
    episode_no: EpisodeNumber
    thread_beats: list[ThreadBeat] = Field(min_length=1, max_length=100)
    caused_by_episode_nos: list[EpisodeNumber] = Field(max_length=1000)
    causal_link: PlanText
    ending_handoff: PlanText


def unique(values: Sequence[Hashable], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label}不能重复")


class SeriesPlan(PlanItem):
    schema_version: Literal["1.0"]
    kind: Literal["PLANNED"]
    threads: list[NarrativeThread] = Field(min_length=1, max_length=100)
    character_arcs: list[CharacterArc] = Field(min_length=1, max_length=200)
    relationship_arcs: list[RelationshipArc] = Field(max_length=200)
    foreshadowing: list[Foreshadowing] = Field(min_length=1, max_length=500)
    episode_progressions: list[EpisodeProgression] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def exact_references(self) -> Self:
        unique([item.id for item in self.threads], "主支线ID")
        unique([item.character_id for item in self.character_arcs], "人物弧线")
        unique([item.id for item in self.relationship_arcs], "关系ID")
        unique([item.id for item in self.foreshadowing], "伏笔ID")
        if not any(item.kind == "MAIN" for item in self.threads):
            raise ValueError("全剧规划至少需要一条主线")
        threads = {item.id for item in self.threads}
        characters = {item.character_id for item in self.character_arcs}
        arcs: list[CharacterArc | RelationshipArc] = [*self.character_arcs, *self.relationship_arcs]
        for arc in arcs:
            unique([item.episode_no for item in arc.milestones], "人物或关系发展集号")
        for relation in self.relationship_arcs:
            if relation.source_character_id == relation.target_character_id:
                raise ValueError("人物关系的两端不能是同一人物")
            if not {relation.source_character_id, relation.target_character_id} <= characters:
                raise ValueError("人物关系必须引用本规划中的人物弧线")
        for clue in self.foreshadowing:
            if clue.thread_id not in threads:
                raise ValueError("伏笔必须引用本规划中的主支线")
            if clue.expected_payoff_episode_no < clue.setup_episode_no:
                raise ValueError("预计回收集不能早于设置集")
        used_threads: set[str] = set()
        for episode in self.episode_progressions:
            unique([item.thread_id for item in episode.thread_beats], "同集主支线推进")
            unique(episode.caused_by_episode_nos, "因果来源集号")
            if any(number >= episode.episode_no for number in episode.caused_by_episode_nos):
                raise ValueError("因果来源只能引用此前的剧集")
            references = {item.thread_id for item in episode.thread_beats}
            if not references <= threads:
                raise ValueError("分集推进必须引用本规划中的主支线")
            used_threads.update(references)
        if used_threads != threads:
            raise ValueError("每条主支线必须有明确的分集推进")
        return self


class SeriesOutlineContent(PlanItem):
    schema_version: Literal["2.0"]
    target_episode_count: int = Field(strict=True, ge=1, le=1000)
    episode_duration_ms: int = Field(strict=True, ge=2500, le=1800000)
    episodes: list[OutlineEpisode] = Field(min_length=1, max_length=1000)
    series_plan: SeriesPlan

    @model_validator(mode="after")
    def complete_narrative(self) -> Self:
        expected = list(range(1, self.target_episode_count + 1))
        if [item.episode_no for item in self.episodes] != expected:
            raise ValueError("分集必须按目标集数从1开始连续编号，不能缺集或重复")
        plan = self.series_plan
        if [item.episode_no for item in plan.episode_progressions] != expected:
            raise ValueError("全剧推进必须与大纲每集一一对应并按集号排列")
        numbers = set(expected)
        arcs: list[CharacterArc | RelationshipArc] = [*plan.character_arcs, *plan.relationship_arcs]
        for arc in arcs:
            if any(item.episode_no not in numbers for item in arc.milestones):
                raise ValueError("人物或关系发展集号超出大纲范围")
        for clue in plan.foreshadowing:
            if (
                clue.setup_episode_no not in numbers
                or clue.expected_payoff_episode_no not in numbers
            ):
                raise ValueError("伏笔设置或预计回收集号超出大纲范围")
        for episode in plan.episode_progressions:
            if not set(episode.caused_by_episode_nos) <= numbers:
                raise ValueError("因果来源集号超出大纲范围")
        return self


OutlineContent = Annotated[
    StoryOutlineContent | SeriesOutlineContent, Field(discriminator="schema_version")
]


def read_outline_content(value: dict) -> StoryOutlineContent | SeriesOutlineContent:
    if value.get("schema_version") == "2.0":
        return SeriesOutlineContent.model_validate(value)
    return StoryOutlineContent.model_validate(value)
