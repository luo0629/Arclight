"""Assemble the advisory continuity report from checked model observations, without I/O."""

from uuid import UUID, uuid5

from pydantic import BaseModel

from arclight.application.gateway_agent import AgentOutputProjection
from arclight.domain.agent_model import AgentModelRequest, AgentToolMessage, AgentUserMessage
from arclight.domain.agent_runtime import AgentDecision
from arclight.domain.continuity_research import (
    RESEARCH_TOOL,
    ContinuityMaterial,
    InvestigateContinuityResult,
)
from arclight.domain.continuity_reviewer import (
    REVIEW_COMPLETION_SCHEMA,
    ReviewCompletion,
    ReviewLoopSeed,
)
from arclight.domain.continuity_reviews import (
    Action,
    ContinuityReview,
    ReviewConfidence,
    ReviewEvidence,
    ReviewIssue,
    ReviewReference,
    ReviewUncertainty,
)
from arclight.domain.continuity_rules import RULES_TOOL, ContinuityRulesResult, SourceKind
from arclight.domain.versions import digest


def _reference(seed: ReviewLoopSeed, material: ContinuityMaterial) -> ReviewReference | None:
    kind, identity, content = material.source_kind, material.source_id, material.content
    if kind == "SCRIPT":
        for item in seed.required_references:
            if (
                item.role == "REFERENCE"
                and item.asset_type == "SCRIPT"
                and item.resource_id == identity
            ):
                return item
        raise ValueError("Comparison script must be the exact frozen script")
    extra = {}
    if kind == "CANDIDATE":
        role, checksum = "CANDIDATE", content["asset"]["checksum"]
    elif kind == "STORYBOARD":
        role, checksum = "STORYBOARD", content["content_sha256"]
    elif kind == "SHOT":
        role = "TARGET_SHOT" if identity == seed.target.shot_id else "ADJACENT_SHOT"
        checksum = content["shot_sha256"]
    elif kind == "VISUAL":
        style = content["global"]["style"]
        if not style.get("version_id"):
            return None  # Defaults are comparison context, not an asset version.
        if style["version_id"] != str(identity):
            raise ValueError("Comparison visual bible must identify its frozen version")
        role = "VISUAL_BIBLE"
        checksum = digest(
            {key: style[key] for key in ("version_id", "version_no", "fields", "locked_fields")}
        )
    else:
        asset = content["asset"]
        role, checksum = "REFERENCE", asset["checksum"]
        extra = {"asset_id": asset["asset_id"], "asset_type": asset["asset_type"]}
    return ReviewReference.model_validate(
        {
            "reference_id": uuid5(seed.result_id, f"{role}:{identity}"),
            "role": role,
            "resource_id": identity,
            "content_sha256": checksum,
            **extra,
        }
    )


def build_review_report(
    seed: ReviewLoopSeed,
    investigation: InvestigateContinuityResult,
    checked: ContinuityRulesResult,
) -> ContinuityReview:
    """Retain observed bases, not the model's second transcription of them."""
    if (
        investigation.target != seed.target
        or checked.target != seed.target
        or checked.evidence_manifest_id != investigation.manifest_id
        or checked.suggested_conclusion is None
    ):
        raise ValueError("Completion requires successful comparisons from the same investigation")
    sources: dict[tuple[SourceKind, UUID], tuple[ContinuityMaterial, ReviewReference | None]] = {}
    references: dict[tuple[str, UUID], ReviewReference] = {}
    for material in investigation.materials:
        if material.manifest_id != investigation.manifest_id or material.content_sha256 != digest(
            material.content
        ):
            raise ValueError("Comparison material must retain its manifest and exact content")
        source_key = (material.source_kind, material.source_id)
        if source_key in sources:
            raise ValueError("Duplicate comparison material")
        reference = _reference(seed, material)
        sources[source_key] = material, reference
        if reference is not None:
            references[reference.role, reference.resource_id] = reference
    for required in seed.required_references:
        if references.get((required.role, required.resource_id)) != required:
            raise ValueError("Completion cannot omit or change a frozen reference")

    evidence: dict[UUID, ReviewEvidence] = {}
    issues: list[ReviewIssue] = []
    uncertainty: list[ReviewUncertainty] = []
    for check in checked.checks:
        comparison = check.comparison
        if comparison is None:
            continue
        if comparison.uncertainty is not None and comparison.uncertainty not in uncertainty:
            uncertainty.append(comparison.uncertainty)
        cites: list[UUID] = []
        for citation in comparison.citations:
            material, reference = sources[citation.source_kind, citation.source_id]
            if citation.content_sha256 != material.content_sha256:
                raise ValueError("A comparison citation must keep the material digest")
            if reference is None:
                continue
            key = uuid5(
                seed.result_id,
                "evidence:"
                + digest(
                    {
                        "reference_id": str(reference.reference_id),
                        **citation.model_dump(mode="json"),
                    }
                ),
            )
            if key not in evidence:
                evidence[key] = ReviewEvidence(
                    evidence_id=key,
                    reference_id=reference.reference_id,
                    region=citation.region,
                    basis=citation.basis,
                )
            if key not in cites:
                cites.append(key)
        if comparison.status == "DIFFERENCE":
            if comparison.risk == "NONE":
                raise ValueError("An observed difference must have explicit risk")
            issues.append(
                ReviewIssue(
                    issue_id=uuid5(seed.result_id, f"issue:{comparison.rule_id}"),
                    category=check.definition.category,
                    risk=comparison.risk,
                    summary=comparison.summary,
                    evidence_ids=cites,
                    suggested_action="HUMAN_REVIEW"
                    if comparison.risk in {"HIGH", "CRITICAL"}
                    else "REVISE_PROMPT",
                )
            )
    if not checked.complete and not uncertainty:
        uncertainty.append(
            ReviewUncertainty(
                reason="MISSING_REFERENCE", detail="部分适用规则缺少可核对的观察或引用。"
            )
        )
    risk_order = ("NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL")
    conclusion = checked.suggested_conclusion
    action: Action = (
        "NONE"
        if conclusion == "PASS"
        else ("REVISE_PROMPT" if conclusion == "REVISION_REQUIRED" else "HUMAN_REVIEW")
    )
    summary = {
        "PASS": "所查候选及已核对的参考未见连续性矛盾。",
        "REVISION_REQUIRED": f"已核对本候选，发现 {len(issues)} 项可引用的连续性差异。",
        "HUMAN_REVIEW": (
            f"发现 {len(issues)} 项连续性差异、{len(uncertainty)} 项不确定性；"
            "请人工复核原图及引用。"
        ),
    }[conclusion]
    return ContinuityReview(
        result_id=seed.result_id,
        project_id=seed.project_id,
        episode_id=seed.episode_id,
        target=seed.target,
        versions=seed.versions,
        rerun=seed.rerun,
        references=list(references.values()),
        evidence=list(evidence.values()),
        conclusion=conclusion,
        suggested_action=action,
        summary=summary,
        risk=max((issue.risk for issue in issues), key=risk_order.index, default="NONE"),
        confidence=ReviewConfidence(
            level="LOW" if uncertainty else "HIGH", uncertainty=uncertainty
        ),
        issues=issues,
    )


def _materialize(request: AgentModelRequest, value: BaseModel) -> AgentDecision[ContinuityReview]:
    if not isinstance(value, AgentDecision):
        raise ValueError("Unexpected continuity completion contract")
    completion = value.result
    if completion is None:
        return AgentDecision[ContinuityReview](
            decision=value.decision, result=None, human_request=value.human_request
        )
    if not isinstance(completion, ReviewCompletion):
        raise ValueError("Unexpected continuity completion result")
    first = request.messages[0]
    if not isinstance(first, AgentUserMessage):
        raise ValueError("Missing the frozen reviewer seed")
    seed = ReviewLoopSeed.model_validate(first.data)
    investigation = None
    checked = None
    for message in request.messages:
        if not isinstance(message, AgentToolMessage) or message.feedback.status != "SUCCEEDED":
            continue
        feedback = message.feedback
        if feedback.tool_name == RESEARCH_TOOL:
            investigation = InvestigateContinuityResult.model_validate(feedback.result)
            checked = None  # New evidence requires its own comparison before completion.
        elif feedback.tool_name == RULES_TOOL:
            checked = ContinuityRulesResult.model_validate(feedback.result)
    if (
        checked is None
        or investigation is None
        or checked.evidence_manifest_id != completion.evidence_manifest_id
    ):
        raise ValueError("Completion must name the latest successful comparison in this loop")
    return AgentDecision[ContinuityReview](
        decision="COMPLETE",
        result=build_review_report(seed, investigation, checked),
        human_request=None,
    )


REVIEW_OUTPUT: AgentOutputProjection[AgentDecision[ContinuityReview]] = AgentOutputProjection(
    schema_version=REVIEW_COMPLETION_SCHEMA,
    wire_model=AgentDecision[ReviewCompletion],
    materialize=_materialize,
)
