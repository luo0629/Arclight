"""Versioned multi-episode extensions; legacy Producer schemas stay unchanged."""

import json
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.art_proposals import Checksum
from arclight.domain.producer_interaction import ProducerRunCreate
from arclight.domain.producer_planning import (
    PLANNING_PROMPT,
    WORKFLOW_VERSION,
    PlanText,
    ProducerPlan,
    ProducerPlanEvidence,
    ProducerTask,
    TaskKey,
)
from arclight.domain.producer_replanning import (
    REPLAN_VERSION,
    REPLANNING_PROMPT,
    ProducerReplan,
    ProducerReplanBase,
    ProducerReplanSeed,
)
from arclight.domain.script_narrative import NarrativeRequest

SERIES_PRODUCER_VERSION = "producer-series/1"
SERIES_PLAN_VERSION = "producer-series-plan/1"
SERIES_REPLAN_VERSION = "producer-series-replan/1"
PRODUCER_VERSIONS = frozenset({WORKFLOW_VERSION, SERIES_PRODUCER_VERSION})
REPLAN_VERSIONS = frozenset({REPLAN_VERSION, SERIES_REPLAN_VERSION})
SERIES_INSTRUCTION = """
本次是固定范围的多集制作，不是锚点集的单集任务。逐字复制series_scope_sha256。
每个task_key必须在task_contexts中恰有一条：明确episode_id、叙事请求和实际消费的
shared_version_ids及前集prerequisite_script_version_ids。只能引用冻结范围内剧集。
先逐集调查真实状态和预算；故事因果、伏笔和人物关系不是制作依赖，不得直接转成循环。
未批准的前集事实不得作为后集来源；缺输入时请求人工，不用虚构版本补齐未来任务。
编剧必须使用精确NarrativeRequest，导演使用表演模式。共享版本不是目录当前指针；
只能复用在该剧情时点有效的明确批准版本，不能省略需要的状态证据。
重规划保留有效任务及其task_contexts；只有真实失败或已失效部分可以替换。
所有集共用同一root预算、调用次数和绝对deadline。未知费用必须对账，不可零价处理。
计划完成或任务数量不是作品完成；逐集正式成片、机器报告和全剧人工连续观片确认独立。
"""


class SeriesEpisode(AgentToolContract):
    episode_id: UUID
    requires_episode_ids: list[UUID] = Field(default_factory=list, max_length=100)


class SeriesProducerScope(AgentToolContract):
    series_plan_version_id: UUID
    ledger_version_id: UUID | None
    expected_ledger_head_revision: int = Field(strict=True, ge=0)
    episodes: list[SeriesEpisode] = Field(min_length=1, max_length=100)
    confirmation: PlanText

    @model_validator(mode="after")
    def production_dag(self):
        ids = [item.episode_id for item in self.episodes]
        if len(set(ids)) != len(ids):
            raise ValueError("Series episodes must be unique")
        visited: set[UUID] = set()
        # This order is an explicit production schedule, not narrative chronology.
        for item in self.episodes:
            if len(set(item.requires_episode_ids)) != len(item.requires_episode_ids):
                raise ValueError("Episode prerequisites must be unique")
            if not set(item.requires_episode_ids) <= visited:
                raise ValueError("Production prerequisites must precede their target")
            visited.add(item.episode_id)
        return self


class SeriesProducerCreate(ProducerRunCreate):
    scope: SeriesProducerScope


class SeriesTaskContext(AgentToolContract):
    task_key: TaskKey
    episode_id: UUID
    narrative: NarrativeRequest | None = None
    shared_version_ids: list[UUID] = Field(default_factory=list, max_length=32)
    prerequisite_script_version_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def exact_inputs(self):
        for ids in (self.shared_version_ids, self.prerequisite_script_version_ids):
            if len(set(ids)) != len(ids):
                raise ValueError("Series input versions must be unique")
        return self


class SeriesProducerPlan(ProducerPlan):
    series_schema_version: Literal["producer-series-plan/1"] = "producer-series-plan/1"
    series_scope_sha256: Checksum
    tasks: list[ProducerTask] = Field(min_length=1, max_length=128)
    evidence: list[ProducerPlanEvidence] = Field(min_length=1, max_length=128)
    task_contexts: list[SeriesTaskContext] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def bounded_ordered_plan(self):
        keys = [item.tool_call_key for item in self.evidence]
        contexts = [item.task_key for item in self.task_contexts]
        if len(set(keys)) != len(keys) or len(set(contexts)) != len(contexts):
            raise ValueError("Series evidence and contexts must be unique")
        preceding: set[str] = set()
        for item in self.tasks:
            if item.task_key in preceding or not set(item.depends_on) <= preceding:
                raise ValueError("Series production tasks must form an ordered DAG")
            preceding.add(item.task_key)
        if preceding != set(contexts):
            raise ValueError("Every series task needs exactly one explicit context")
        if len(json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode()) > 262144:
            raise ValueError("Series plan exceeds its byte limit")
        return self


class SeriesProducerReplan(ProducerReplan):
    plan: SeriesProducerPlan


class SeriesProducerReplanBase(ProducerReplanBase):
    plan: SeriesProducerPlan


class SeriesProducerReplanSeed(ProducerReplanSeed):
    base_plan: SeriesProducerReplanBase


class SeriesControl(AgentToolContract):
    action: Literal["PAUSE", "RESUME"]
    expected_revision: int = Field(strict=True, ge=0)
    reason: PlanText


class SeriesDelivery(AgentToolContract):
    episode_id: UUID
    artifact_id: UUID


class SeriesCompletion(AgentToolContract):
    expected_revision: int = Field(strict=True, ge=0)
    scope: Literal["BATCH", "SERIES"]
    deliveries: list[SeriesDelivery] = Field(min_length=1, max_length=100)
    narrative_report_id: UUID
    continuous_viewing_confirmed: Literal[True]
    comment: PlanText


def plan_model(version: str) -> type[ProducerPlan]:
    return SeriesProducerPlan if version == SERIES_PRODUCER_VERSION else ProducerPlan


def planning_instruction(version: str) -> str:
    return (
        PLANNING_PROMPT + SERIES_INSTRUCTION
        if version == SERIES_PRODUCER_VERSION
        else PLANNING_PROMPT
    )


def replan_model(version: str) -> type[ProducerReplan]:
    return SeriesProducerReplan if version == SERIES_PRODUCER_VERSION else ProducerReplan


def replan_version(version: str) -> str:
    return SERIES_REPLAN_VERSION if version == SERIES_PRODUCER_VERSION else REPLAN_VERSION


def replanning_instruction(version: str) -> str:
    return (
        REPLANNING_PROMPT + SERIES_INSTRUCTION
        if version == SERIES_PRODUCER_VERSION
        else REPLANNING_PROMPT
    )
