"""Explicit performance-director contracts; archived director schemas stay frozen."""

from typing import Literal

from pydantic import Field, model_validator

from arclight.agents.contracts import (
    PerformanceRoute,
    SceneV3,
    ScriptContentV3,
    ShotContentV3,
)
from arclight.domain.director_planning import (
    PLANNING_PROMPT,
    DirectorPlan,
    DirectorSeed,
)
from arclight.domain.director_revision import (
    REVISION_PROMPT,
    WORKING_PROMPT,
    DirectorCheckParameters,
    DirectorPatch,
    DirectorRevisionDecision,
    DirectorRevisionOptions,
    DirectorRevisionSeed,
    DirectorWorkingFragment,
    DirectorWorkingScene,
    DirectorWorkingSeed,
    DirectorWorkingStoryboard,
)

PERFORMANCE_DIRECTOR_VERSION = "director/4.0"
PERFORMANCE_PLAN_VERSION = "director-performance-plan/1"
PERFORMANCE_WORKING_VERSION = "director-performance-working/1"
PERFORMANCE_REVISION_VERSION = "director-performance-revision/1"

PERFORMANCE_PLAN_PROMPT = (
    PLANNING_PROMPT
    + """
这是表演导演版本。保留完整3.0剧情场景、人物目的、冲突、动作和反应，不降级旧剧本视图。
调查精确批准剧本全部场景、视觉规范和实际生成能力，generation_routes只选本循环实际调查的
provider/model；声明不等于已真实验收，mock不是真实生产能力。能力缺口应提出人工问题。
逐场计划必须将冲突、人物目的、情绪转折、互动、走位、视线及道具接续落实为可制作动作。
"""
)
PERFORMANCE_WORKING_PROMPT = (
    WORKING_PROMPT
    + """
每镜必须交付schema_version=3.0和完整performance。记录场景/叙事目的、情绪节奏，
每个出镜人物的目的、动作/情绪/位置/视线起止，两人互动的行动与反应，空间/道具起止。
台词必须保留精确line_id、speaker、text和情绪/语速；明确visible/voice_over/off_screen/obscured（口部被遮挡）；豁免须说明具体画面依据。
visible必须口型required并dynamic_required；旁白/画外音须给出不适用口型的合理理由。
动作、互动、可见说话或状态变化必须dynamic_required，不能用静图运镜冒充人物运动。
同场景后续镜头明确continuity_from_shot，continuous保持位置、视线、空间和道具接续；
合理跳变用ellipsis且说明原因。场景内镜号和接续引用均使用本场景编号，服务端统一偏移。
不得删除原剧情冲突/动作、把原角色对白全部转画外音来规避能力；能力不足应修订或请求人工。
"""
)
PERFORMANCE_REVISION_PROMPT = (
    REVISION_PROMPT
    + """
本版本检查还包含精确台词、表演/接续、动态要求及真实能力调查证据。
保留完整3.0剧本及每镜performance，不能通过隐藏动作、角色互动或口型要求消除能力问题。
工具的能力不足、未知口型或mock结果必须修订路线或HUMAN_REVIEW；人工回答不豁免真实检查。
"""
)


class PerformanceDirectorOptions(DirectorRevisionOptions):
    performance: Literal[True] = True

    @model_validator(mode="after")
    def require_performance_revision(self):
        if not self.professional or self.director_revision_rounds is None:
            raise ValueError("Performance direction requires professional bounded revision")
        if self.director_art_collaboration:
            raise ValueError("Performance direction currently requires a separate art review")
        return self


class PerformanceDirectorSeed(DirectorSeed):
    script: ScriptContentV3


class PerformanceDirectorPlan(DirectorPlan):
    schema_version: Literal["director-performance-plan/1"] = PERFORMANCE_PLAN_VERSION  # type: ignore[assignment]
    generation_routes: list[PerformanceRoute] = Field(default_factory=list, max_length=10)


class PerformanceWorkingShot(ShotContentV3):
    """Repairable numbering is checked by the source-bound publication tool."""

    shot_no: int = Field(strict=True, ge=-10000, le=10000)
    scene_no: int = Field(strict=True, ge=-10000, le=10000)
    duration_ms: int = Field(strict=True, ge=-1800000, le=1800000)

    @model_validator(mode="after")
    def check_details(self):
        return self

    @model_validator(mode="after")
    def check_performance(self):
        return self


class PerformanceWorkingScene(DirectorWorkingScene):
    shots: list[PerformanceWorkingShot] = Field(min_length=1, max_length=100)  # type: ignore[assignment]


class PerformanceWorkingFragment(DirectorWorkingFragment):
    schema_version: Literal["director-performance-working/1"] = PERFORMANCE_WORKING_VERSION  # type: ignore[assignment]
    shots: list[PerformanceWorkingShot] = Field(min_length=1, max_length=100)  # type: ignore[assignment]


class PerformanceWorkingSeed(DirectorWorkingSeed):
    plan: PerformanceDirectorPlan
    scene: SceneV3
    previous_shots: list[PerformanceWorkingShot] = Field(max_length=2)  # type: ignore[assignment]


class PerformanceWorkingStoryboard(DirectorWorkingStoryboard):
    scenes: list[PerformanceWorkingScene] = Field(min_length=1, max_length=50)  # type: ignore[assignment]


class PerformancePatch(DirectorPatch):
    shots: list[PerformanceWorkingShot] = Field(max_length=20)  # type: ignore[assignment]


class PerformanceCheckParameters(DirectorCheckParameters):
    patch: PerformancePatch | None = None


class PerformanceRevisionSeed(DirectorRevisionSeed):
    source: PerformanceWorkingStoryboard
    script: ScriptContentV3
    plan: PerformanceDirectorPlan


class PerformanceRevisionDecision(DirectorRevisionDecision):
    schema_version: Literal["director-performance-revision/1"] = PERFORMANCE_REVISION_VERSION  # type: ignore[assignment]


def is_performance_director(workflow_version: str) -> bool:
    return workflow_version == PERFORMANCE_DIRECTOR_VERSION
