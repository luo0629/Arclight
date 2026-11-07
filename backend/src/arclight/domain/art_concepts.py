"""Opt-in concept exploration; old art seeds and final-result schemas stay frozen."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.agent_tools import AgentToolContract, RetrieveEvidenceResult
from arclight.domain.art_research import ArtResearchMaterial
from arclight.domain.dependencies import DependencyInput
from arclight.domain.generation_requests import GenerationSpec

CONCEPT_WORKFLOW_VERSION = "art-proposal/2.0"
CONCEPT_DIRECTION_VERSION = "art-direction/2"
REQUEST_CONCEPT_TOOL = "request_art_concepts"
OBSERVE_CONCEPT_TOOL = "observe_art_concepts"
CONCEPT_PROMPT = """
本次额外允许按服务端冻结concept_plan调用request_art_concepts，再用observe_art_concepts
等待原job的异步结果；不得自行调用Provider或修改能力、参考输入、数量和费用上限。
round_no从1连续递增；rationale说明这一轮的视觉目标或修订依据。第2轮起必须引用上一轮
实际完成看图比较的compare_art_options反馈中的comparison_sha256，并说明据此如何修订提示词。
观察成功后真正查看所附图片，再比较优点、问题和取舍。概念图使用COMPARISON参考；
证据同调查material。候选仅供比较，正式参考与授权确认须经原人工美术审核。
每个可用概念轮次至少在一个方案中引用该轮全部候选并说明比较依据。
FAILED、CANCELLED或媒体检查失败应解释；RESULT_UNKNOWN／MANUAL_REVIEW须人工处理，
不能盲目重发。申请新一轮不能解除锁定、跳过人工批准或重新设置全局限额。
"""


class ArtConceptPlan(AgentToolContract):
    target_type: Literal["CHARACTER", "SCENE", "PROP", "STYLE"] = "STYLE"
    spec: GenerationSpec
    inputs: list[DependencyInput] = Field(default_factory=list, max_length=16)
    maximum_rounds: int = Field(default=2, strict=True, ge=1, le=4)
    maximum_candidates: int = Field(default=4, strict=True, ge=1, le=8)
    maximum_cost: Decimal = Field(ge=0, le=10000, max_digits=12, decimal_places=6)

    @model_validator(mode="after")
    def bounded_plan(self):
        if self.spec.batch_size > self.maximum_candidates or self.spec.batch_size > 4:
            raise ValueError("Concept batches must fit the frozen candidate limit")
        if self.spec.context:
            raise ValueError("Concept context is constructed from the frozen art run")
        identities = [(row.source_asset_version_id, row.dependency_type) for row in self.inputs]
        if len(set(identities)) != len(identities):
            raise ValueError("Concept inputs must be unique exact versions")
        return self


class RequestArtConcepts(AgentToolContract):
    round_no: int = Field(strict=True, ge=1, le=4)
    prompt: str = Field(min_length=1, max_length=16000)
    rationale: str = Field(min_length=1, max_length=2000)
    previous_comparison_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ArtConceptRequested(AgentToolContract):
    job_id: UUID
    round_no: int
    input_manifest_id: UUID
    maximum_cost: Decimal
    candidate_count: int
    rationale: str


class ObserveArtConcepts(AgentToolContract):
    job_id: UUID


class ArtConceptObserved(RetrieveEvidenceResult):
    job_id: UUID
    round_no: int
    status: str
    candidate_version_ids: list[UUID] = Field(max_length=8)
    materials: list[ArtResearchMaterial] = Field(max_length=8)
    actual_cost: Decimal | None
    reserved_cost: Decimal
    outcome_uncertain: bool
    errors: list[str] = Field(default_factory=list, max_length=16)
