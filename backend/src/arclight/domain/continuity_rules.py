"""Versioned comparison requirements; visual observations remain proposed evidence."""

from typing import Final, Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.continuity_reviews import (
    Brief,
    Checksum,
    ReviewRegion,
    ReviewTarget,
    ReviewUncertainty,
    Risk,
)

RULES_VERSION: Final = "continuity-rules/1"
RULES_TOOL = "check_continuity_rules"
RuleId = Literal[
    "APPEARANCE",
    "COSTUME",
    "SCENE",
    "PROP",
    "INTENT",
    "COMPOSITION",
    "HOLDER",
    "POSITION",
    "STATE",
    "ADJACENT",
]
RuleCategory = Literal[
    "CHARACTER_APPEARANCE",
    "COSTUME",
    "SCENE",
    "PROP",
    "STORYBOARD_INTENT",
    "ADJACENT_CONTINUITY",
]
RuleCheckStatus = Literal["NOT_CHECKED", "MATCH", "DIFFERENCE", "UNDETERMINED", "NOT_APPLICABLE"]
RuleConclusion = Literal["PASS", "REVISION_REQUIRED", "HUMAN_REVIEW"]
SourceKind = Literal["CANDIDATE", "STORYBOARD", "SCRIPT", "SHOT", "ASSET", "VISUAL"]


class RuleCitation(AgentToolContract):
    source_kind: SourceKind
    source_id: UUID
    content_sha256: Checksum
    region: ReviewRegion | None = None
    basis: Brief


class RuleComparison(AgentToolContract):
    rule_id: RuleId
    status: Literal["MATCH", "DIFFERENCE", "UNDETERMINED", "NOT_APPLICABLE"]
    summary: Brief
    risk: Risk = "NONE"
    uncertainty: ReviewUncertainty | None = None
    citations: list[RuleCitation] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def consistent_observation(self):
        if self.status in {"MATCH", "NOT_APPLICABLE"} and (
            self.risk != "NONE" or self.uncertainty is not None
        ):
            raise ValueError(
                "Matching or inapplicable comparisons cannot suppress risk or uncertainty"
            )
        if self.status == "DIFFERENCE" and self.risk == "NONE":
            raise ValueError("A difference requires an explicit risk")
        if self.status == "UNDETERMINED" and self.uncertainty is None:
            raise ValueError("Undetermined comparisons require a specific uncertainty reason")
        if self.status != "NOT_APPLICABLE" and not self.citations:
            raise ValueError("Comparisons require exact cited evidence")
        return self


class ContinuityRulesParameters(AgentToolContract):
    target: ReviewTarget
    evidence_manifest_id: UUID
    comparisons: list[RuleComparison] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def unique_comparisons(self):
        if len({item.rule_id for item in self.comparisons}) != len(self.comparisons):
            raise ValueError("Each comparison rule can be reported only once")
        return self


class ContinuityRuleDefinition(AgentToolContract):
    rule_id: RuleId
    category: RuleCategory
    compare: list[Brief] = Field(min_length=1, max_length=8)
    requires_script: bool = Field(strict=True)


class RuleSource(AgentToolContract):
    source_kind: SourceKind
    source_id: UUID
    content_sha256: Checksum
    image_file_id: UUID | None = None


class RuleReferenceRequirement(AgentToolContract):
    subject: Brief
    role: Literal["CHARACTER", "COSTUME", "SCENE", "PROP", "ADJACENT"]
    image_sources: list[RuleSource] = Field(max_length=18)
    adjacent_shot: RuleSource | None = None


class ContinuityRuleCheck(AgentToolContract):
    definition: ContinuityRuleDefinition
    applicable: bool = Field(strict=True)
    references: list[RuleReferenceRequirement] = Field(max_length=64)
    status: RuleCheckStatus
    gaps: list[str] = Field(max_length=192)
    comparison: RuleComparison | None = None


class ContinuityRulesResult(AgentToolContract):
    schema_version: Literal["continuity-rule-check/1"] = "continuity-rule-check/1"
    rules_version: Literal["continuity-rules/1"] = RULES_VERSION
    evidence_manifest_id: UUID
    target: ReviewTarget
    checks: list[ContinuityRuleCheck] = Field(min_length=10, max_length=10)
    context_sources: list[RuleSource] = Field(max_length=18)
    scene_change_shot_ids: list[UUID] = Field(max_length=2)
    complete: bool = Field(strict=True)
    suggested_conclusion: RuleConclusion | None
    advisory_only: Literal[True] = True
