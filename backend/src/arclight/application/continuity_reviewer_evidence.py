"""Require actual investigation, image consumption and comparison in the same loop."""

from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.art_proposals import visual_version_sha256
from arclight.application.continuity_reviews import validate_review_contract
from arclight.application.continuity_rules import check_continuity_rules
from arclight.application.dependencies import exact_version
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.continuity_research import (
    RESEARCH_TOOL,
    ContinuityMaterial,
    InvestigateContinuityResult,
)
from arclight.domain.continuity_reviewer import ReviewLoopSeed, review_operation
from arclight.domain.continuity_reviews import ContinuityReview, ReviewReference, ReviewTarget
from arclight.domain.continuity_rules import (
    RULES_TOOL,
    ContinuityRulesParameters,
    ContinuityRulesResult,
)
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.models import VisualBibleVersion, WorkflowRun


def reviewer_loop(
    db: Session, run_id: UUID, target: ReviewTarget
) -> tuple[AgentLoop, ReviewLoopSeed]:
    identity = digest(
        {"run": str(run_id), "stage": "storyboard", "operation": review_operation(target)}
    )
    loop = db.scalar(
        select(AgentLoop).where(AgentLoop.run_id == run_id, AgentLoop.operation_sha256 == identity)
    )
    if loop is None:
        raise DomainError("REVIEW_LOOP_INVALID", "审查须属于原持久取证循环", 409)
    request = AgentModelRequest.model_validate(loop.seed_json)
    first = request.messages[0]
    if not isinstance(first, AgentUserMessage):
        raise DomainError("REVIEW_LOOP_INVALID", "审查缺少冻结目标", 409)
    seed = ReviewLoopSeed.model_validate(first.data)
    if request.agent_name != "continuity_reviewer" or seed.target != target:
        raise DomainError("REVIEW_LOOP_INVALID", "不能替换原审查循环或目标", 409)
    return loop, seed


def material_reference(
    db: Session, seed: ReviewLoopSeed, material: ContinuityMaterial
) -> ReviewReference | None:
    kind, identity, content = material.source_kind, material.source_id, material.content
    role, checksum, extra = "REFERENCE", None, {}
    if kind == "CANDIDATE":
        role, checksum = "CANDIDATE", content["asset"]["checksum"]
    elif kind == "STORYBOARD":
        role, checksum = "STORYBOARD", content["content_sha256"]
    elif kind == "SHOT":
        role = "TARGET_SHOT" if identity == seed.target.shot_id else "ADJACENT_SHOT"
        checksum = content["shot_sha256"]
    elif kind == "VISUAL":
        bible = db.get(VisualBibleVersion, identity)
        if bible is None:
            return None  # Default project constraints are not a fictitious bible version.
        role, checksum = "VISUAL_BIBLE", visual_version_sha256(bible)
    else:
        version, asset = exact_version(db, seed.project_id, identity)
        checksum = version.checksum
        extra = {"asset_id": asset.id, "asset_type": asset.asset_type}
    return ReviewReference.model_validate(
        {
            "reference_id": uuid5(seed.result_id, f"{role}:{identity}"),
            "role": role,
            "resource_id": identity,
            "content_sha256": checksum,
            **extra,
        }
    )


def validate_reviewer_delivery(
    db: Session, settings: Settings, run: WorkflowRun, actor: str, result: ContinuityReview
) -> None:
    loop, seed = reviewer_loop(db, run.id, result.target)
    if (result.project_id, result.episode_id, result.result_id, result.rerun) != (
        seed.project_id,
        seed.episode_id,
        seed.result_id,
        seed.rerun,
    ) or result.versions != seed.versions:
        raise DomainError("REVIEW_SCOPE_INVALID", "审查不能更换冻结作用域、版本或重跑身份", 409)
    validate_review_contract(db, result, actor)
    actions = list(
        db.scalars(
            select(AgentLoopAction)
            .where(AgentLoopAction.loop_id == loop.id)
            .order_by(AgentLoopAction.created_at, AgentLoopAction.id)
        )
    )
    observed, seen_images, comparisons = {}, set(), []
    for action in actions:
        if not action.result_json:
            continue
        if action.kind == "MODEL":
            seen_images.update(
                AgentModelRequest.model_validate(action.request_json["request"]).image_ids()
            )
        elif action.kind == "TOOL" and action.result_json.get("status") == "SUCCEEDED":
            if action.request_json.get("tool_name") == RESEARCH_TOOL:
                investigation = InvestigateContinuityResult.model_validate(
                    action.result_json["result"]
                )
                if investigation.target != seed.target:
                    raise DomainError("REVIEW_TARGET_INVALID", "本循环不能混用其他候选的取证", 409)
                observed[investigation.manifest_id] = investigation
            elif action.request_json.get("tool_name") == RULES_TOOL:
                comparisons.append((action, seen_images.copy(), observed.copy()))
    if not comparisons:
        raise DomainError("REVIEW_COMPARISON_REQUIRED", "最终审查须有本循环真实比较回执", 422)
    action, consumed, investigated = comparisons[-1]
    assert action.result_json is not None
    parameters = ContinuityRulesParameters.model_validate(action.request_json["arguments"])
    evidence = investigated.get(parameters.evidence_manifest_id)
    if evidence is None:
        raise DomainError("REVIEW_EVIDENCE_REQUIRED", "比较须使用本循环已取得的证据", 422)
    current = ContinuityRulesResult.model_validate(
        check_continuity_rules(db, settings, run, actor, parameters)
    )
    if current.model_dump(mode="json") != action.result_json["result"]:
        raise DomainError("REVIEW_REFERENCE_CHANGED", "比较回执与当前精确证据不匹配", 409)
    if not parameters.comparisons:
        raise DomainError("REVIEW_COMPARISON_REQUIRED", "规则索引不能代替实际比较观察", 422)
    if result.conclusion != "HUMAN_REVIEW" and result.conclusion != current.suggested_conclusion:
        raise DomainError("REVIEW_CONCLUSION_INVALID", "不能降低证据不足、差异或严重风险结论", 422)
    if not current.complete and not result.confidence.uncertainty:
        raise DomainError("REVIEW_UNCERTAINTY_REQUIRED", "证据不足须明确保留不确定原因", 422)
    materials = {(m.source_kind, m.source_id): m for m in evidence.materials}
    allowed = {}
    for material in materials.values():
        reference = material_reference(db, seed, material)
        if reference is not None:
            allowed[reference.role, reference.resource_id] = reference
    declared = {(r.role, r.resource_id): r for r in result.references}
    for key, reference in declared.items():
        canonical = allowed.get(key)
        if canonical is None or reference.model_dump(
            exclude={"reference_id"}
        ) != canonical.model_dump(exclude={"reference_id"}):
            raise DomainError("REVIEW_REFERENCE_UNOBSERVED", "审查引用须来自本循环实际取证", 422)
    if not {(r.role, r.resource_id) for r in seed.required_references} <= declared.keys():
        raise DomainError("REVIEW_REFERENCE_MISSING", "不能省略冻结审查输入", 422)
    by_id = {r.reference_id: r for r in result.references}
    located = {
        (by_id[e.reference_id].role, by_id[e.reference_id].resource_id) for e in result.evidence
    }
    # A model turn must have consumed the images before making its comparison.
    needed_images = set()
    candidate = materials["CANDIDATE", seed.target.candidate_id]
    needed_images.add(UUID(candidate.content["image"]["file_id"]))
    ranks = ["NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
    for check in current.checks:
        comparison = check.comparison
        if comparison is None:
            continue
        cited = set()
        for citation in comparison.citations:
            material = materials[citation.source_kind, citation.source_id]
            reference = material_reference(db, seed, material)
            image = material.content.get("image")
            if image:
                needed_images.add(UUID(image["file_id"]))
            if reference is not None:
                cited.add((reference.role, reference.resource_id))
        if not cited <= located:
            raise DomainError("REVIEW_EVIDENCE_MISSING", "结果须保留实际比较引用及简短依据", 422)
        if comparison.uncertainty and comparison.uncertainty not in result.confidence.uncertainty:
            raise DomainError("REVIEW_UNCERTAINTY_REQUIRED", "不能删除比较中的不确定原因", 422)
        if comparison.status == "DIFFERENCE" and not any(
            issue.category == check.definition.category
            and ranks.index(issue.risk) >= ranks.index(comparison.risk)
            and cited
            <= {
                (by_id[e.reference_id].role, by_id[e.reference_id].resource_id)
                for e in result.evidence
                if e.evidence_id in issue.evidence_ids
            }
            for issue in result.issues
        ):
            raise DomainError("REVIEW_ISSUE_MISSING", "不能省略已比较差异、风险及对应证据", 422)
    if not needed_images <= consumed:
        raise DomainError("REVIEW_IMAGE_NOT_OBSERVED", "比较前须由模型实际读取候选及引用图片", 422)
