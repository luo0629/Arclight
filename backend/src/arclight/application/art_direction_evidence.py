"""Bind art decisions to successful actions of their own persistent operation."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.art_proposals import visual_version_sha256
from arclight.application.art_protection import compare_art_proposal
from arclight.application.art_research_assets import read_reference
from arclight.application.director_research import approved_input
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.art_direction import (
    COMPARISON_TOOL,
    DIRECTION_VERSION,
    ArtDirectionDraft,
    ArtDirectionResult,
    ArtDirectionSeed,
    ArtEvidence,
    CompareArtResult,
)
from arclight.domain.art_proposals import ExistingAssetTarget, VersionArtSource, VisualBibleConcept
from arclight.domain.art_protection import ArtComparison
from arclight.domain.art_research import InvestigateArtResult
from arclight.domain.context import ContextLimits
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.models import VisualBibleVersion, WorkflowRun


def operation_sha256(run_id: UUID) -> str:
    return digest({"run": str(run_id), "stage": "storyboard", "operation": DIRECTION_VERSION})


def direction_loop(db: Session, run_id: UUID) -> tuple[AgentLoop, ArtDirectionSeed]:
    loop = db.scalar(
        select(AgentLoop).where(
            AgentLoop.run_id == run_id,
            AgentLoop.operation_sha256 == operation_sha256(run_id),
        )
    )
    if loop is None:
        raise DomainError("ART_DIRECTION_INVALID", "美术比较须属于原持久决策循环", 409)
    request = AgentModelRequest.model_validate(loop.seed_json)
    first = request.messages[0]
    assert isinstance(first, AgentUserMessage)
    return loop, ArtDirectionSeed.model_validate(first.data)


def _materials(actions: list[AgentLoopAction]) -> tuple[dict[str, dict], set[UUID]]:
    observed: dict[str, dict] = {}
    seen_images: set[UUID] = set()
    for action in actions:
        if action.result_json is None:
            continue
        if action.kind == "MODEL":
            request = AgentModelRequest.model_validate(action.request_json["request"])
            seen_images.update(request.image_ids())
        elif (
            action.kind == "TOOL"
            and action.request_json.get("tool_name") == "investigate_art"
            and action.result_json.get("status") == "SUCCEEDED"
        ):
            result = InvestigateArtResult.model_validate(action.result_json["result"])
            for material in result.materials:
                evidence = ArtEvidence.model_validate(material.model_dump(exclude={"content"}))
                observed[evidence.model_dump_json()] = material.content
    return observed, seen_images


def validate_draft(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    actor: str,
    draft: ArtDirectionDraft,
) -> ArtComparison:
    loop, seed = direction_loop(db, run.id)
    script, trace = approved_input(db, run, actor, settings)
    proposal = draft.proposal
    if (
        seed.script_version_id != script.id
        or seed.script_sha256 != digest(script.content_json)
        or proposal.project_id != seed.project_id
        or proposal.episode_id != seed.episode_id
        or proposal.origin != "AGENT"
        or proposal.visual_goal != seed.task.visual_goal
        or not set(seed.task.constraints) <= set(proposal.constraints)
    ):
        raise DomainError("ART_DIRECTION_SCOPE", "美术提案不能更换冻结目标、约束、剧本或范围", 422)
    if proposal.questions:
        raise DomainError("ART_CLARIFICATION_REQUIRED", "未决问题须先通过原运行请求人工澄清", 422)
    actions = list(db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop.id)))
    observed, seen_images = _materials(actions)
    if not {item.model_dump_json() for item in draft.evidence} <= observed.keys():
        raise DomainError("ART_EVIDENCE_INVALID", "美术提案只能引用本循环实际成功调查的材料", 422)
    selected = [(item, observed[item.model_dump_json()]) for item in draft.evidence]
    if not {"VISUAL", "MODEL"} <= {item.source_kind for item, _ in selected} or not any(
        item.source_kind == "MODEL" and content["usable_for_art"] for item, content in selected
    ):
        raise DomainError("ART_EVIDENCE_REQUIRED", "提案缺少完整规范或可用多模态模型能力证据", 422)
    directories = {
        item.source_id: content for item, content in selected if item.source_kind == "DIRECTORY"
    }
    if not set(seed.task.target_asset_ids) <= directories.keys():
        raise DomainError("ART_EVIDENCE_REQUIRED", "须先调查全部指定资产的精确目录与锁定基准", 422)
    allowed_sources = {("SCRIPT", script.id, seed.script_sha256)}
    for item, content in selected:
        if item.source_kind == "ASSET":
            allowed_sources.add(("ASSET_VERSION", item.source_id, content["checksum"]))
        elif item.source_kind == "VISUAL":
            bible_id = content["global"]["style"]["version_id"]
            if bible_id:
                bible = db.get(VisualBibleVersion, UUID(bible_id))
                assert bible is not None
                allowed_sources.add(("VISUAL_BIBLE", bible.id, visual_version_sha256(bible)))
    required_sources = {item for item in allowed_sources if item[0] != "ASSET_VERSION"}
    declared_sources = {
        (source.resource_type, source.resource_id, source.content_sha256)
        for source in proposal.sources
        if isinstance(source, VersionArtSource)
    }
    if not required_sources <= declared_sources or any(
        not isinstance(source, VersionArtSource)
        or (source.resource_type, source.resource_id, source.content_sha256) not in allowed_sources
        for source in proposal.sources
    ):
        raise DomainError(
            "ART_EVIDENCE_INVALID", "提案来源须为冻结剧本或本轮实际读取的精确版本", 422
        )
    for option in proposal.options:
        for design in option.designs:
            if isinstance(design, VisualBibleConcept):
                continue
            if isinstance(design.target, ExistingAssetTarget):
                if (
                    seed.task.target_asset_ids
                    and design.target.asset_id not in seed.task.target_asset_ids
                ):
                    raise DomainError("ART_DIRECTION_SCOPE", "不能修改指定范围外的资产", 422)
                directory = directories.get(design.target.asset_id)
                if (
                    directory is None
                    or not directory["allowed_for_episode"]
                    or directory["baseline"] != design.target.model_dump(mode="json")
                ):
                    raise DomainError(
                        "ART_EVIDENCE_INVALID", "修改目标须绑定实际调查的目录基准", 422
                    )
    materials = {
        item.source_id: content for item, content in selected if item.source_kind == "ASSET"
    }
    limits = ContextLimits.model_validate(trace["task_context"]["limits"])
    for reference in proposal.references:
        reference_content = materials.get(reference.asset.asset_version_id)
        if (
            reference_content is None
            or reference.usage != "APPROVED_REFERENCE"
            or reference_content["asset_id"] != str(reference.asset.asset_id)
            or reference_content["rights_status"] != "CONFIRMED"
            or UUID(reference_content["image"]["file_id"]) not in seen_images
        ):
            raise DomainError("ART_EVIDENCE_REQUIRED", "参考须为本轮实际查看的已批准授权图片", 422)
        fresh = read_reference(
            db, settings, seed.project_id, seed.episode_id, reference.asset.asset_version_id, limits
        )
        if fresh.reason:
            raise DomainError("ART_EVIDENCE_REQUIRED", "参考的授权、使用范围或原图已不可用", 409)
    manifest_by_version = {
        item.source_id: item.manifest_id for item in draft.evidence if item.source_kind == "ASSET"
    }
    references = {ref.reference_id: ref for ref in proposal.references}
    for assessment in draft.assessments:
        required = {
            manifest_by_version[references[key].asset.asset_version_id]
            for key in assessment.reference_ids
        }
        if not required <= set(assessment.evidence_manifest_ids):
            raise DomainError("ART_EVIDENCE_REQUIRED", "方案比较须列出实际参考图片的取证回执", 422)
    if len(proposal.references) < seed.task.minimum_reference_images:
        raise DomainError("ART_EVIDENCE_REQUIRED", "必要参考图片不足，须请求补充证据", 422)
    return compare_art_proposal(db, proposal, actor)


def validate_delivery(
    db: Session, settings: Settings, run: WorkflowRun, actor: str, result: ArtDirectionResult
) -> None:
    comparison = validate_draft(db, settings, run, actor, result.draft)
    loop, _ = direction_loop(db, run.id)
    for action in db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop.id)):
        if (
            action.kind == "TOOL"
            and action.request_json.get("tool_name") == COMPARISON_TOOL
            and action.result_json
            and action.result_json.get("status") == "SUCCEEDED"
        ):
            receipt = CompareArtResult.model_validate(action.result_json["result"])
            if (
                receipt.draft_sha256 == digest(result.draft.model_dump(mode="json"))
                and receipt.comparison == comparison
                and receipt.comparison.comparison_sha256 == result.comparison_sha256
            ):
                return
    raise DomainError(
        "ART_COMPARISON_REQUIRED", "最终提案须匹配本循环真实比较回执，修改后须重新比较", 422
    )
