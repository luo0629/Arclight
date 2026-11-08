"""Check coverage and exact evidence for proposed visual comparisons, without approving assets."""

import json
from uuid import UUID

from sqlalchemy.orm import Session

from arclight.application.agent_tools import AgentToolContext, AgentToolDefinition
from arclight.application.art_research_assets import allowed_for_episode
from arclight.application.content_dependencies import reference_ids
from arclight.application.context_builder import resolve_assets
from arclight.application.context_evidence import read_trace
from arclight.application.context_images import load_context_images
from arclight.application.continuity_research import require_evidence_candidate
from arclight.application.continuity_reviews import shot_sha256
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.context import ContextLimits
from arclight.domain.continuity_research import InvestigateContinuityResult
from arclight.domain.continuity_rules import (
    RULES_TOOL,
    ContinuityRuleCheck,
    ContinuityRuleDefinition,
    ContinuityRulesParameters,
    ContinuityRulesResult,
    RuleCategory,
    RuleCheckStatus,
    RuleConclusion,
    RuleId,
    RuleReferenceRequirement,
    RuleSource,
)
from arclight.domain.versions import digest
from arclight.persistence.models import Episode, Shot, VisualAsset, WorkflowRun


def rule_definitions() -> list[ContinuityRuleDefinition]:
    specifications: list[tuple[RuleId, RuleCategory, list[str], bool]] = [
        (
            "APPEARANCE",
            "CHARACTER_APPEARANCE",
            [
                "逐角色比较身份、发色／发型、面部显著特征及锁定属性。",
                "引用该角色的精确参考图与冻结视觉规范；遮挡不能推定匹配。",
            ],
            False,
        ),
        (
            "COSTUME",
            "COSTUME",
            [
                "逐角色比较服装精确版本、颜色、配饰及穿戴方式。",
                "区分剧本明确换装与同场景无解释变化，不以角色外观参考替代服装参考。",
            ],
            True,
        ),
        (
            "SCENE",
            "SCENE",
            [
                "比较空间布局、地点、时间／天气及冻结场景约束。",
                "使用目标场景的精确参考；有明确转场时允许与邻镜场景不同。",
            ],
            True,
        ),
        (
            "PROP",
            "PROP",
            [
                "逐关键道具比较身份、形状、颜色、数量及应有的出现／缺席。",
                "引用精确道具参考及镜头／剧情要求，不把无结构化道具误判为缺失。",
            ],
            True,
        ),
        (
            "INTENT",
            "STORYBOARD_INTENT",
            [
                "比较人物、动作、表情、剧情信息与分镜意图及相应场景／对白。",
                "不得引用未建立的人物、场景或不存在的对白；画外对白不强制人物入画。",
            ],
            True,
        ),
        (
            "COMPOSITION",
            "STORYBOARD_INTENT",
            [
                "比较景别、视角、主体、构图和分镜指定的画面重点。",
                "依据精确目标镜头及冻结视觉约束；未指定的细节不凭空增加限制。",
            ],
            False,
        ),
        (
            "HOLDER",
            "PROP",
            [
                "逐关键道具比较持有者、持有动作及交接前后的归属。",
                "同场景相邻图与剧情均需引用；明确交接允许持有者变化。",
            ],
            True,
        ),
        (
            "POSITION",
            "ADJACENT_CONTINUITY",
            [
                "比较人物／道具相对位置、动作方向与视线。",
                "结合目标镜头及同场景相邻图；反打或显式调度不能仅按屏幕左右判错。",
            ],
            True,
        ),
        (
            "STATE",
            "PROP",
            [
                "逐关键道具比较开闭、完损、使用阶段等可见状态。",
                "结合剧情及同场景相邻图；明确状态变化应保留依据，遮挡或不可见需标注不确定。",
            ],
            True,
        ),
        (
            "ADJACENT",
            "ADJACENT_CONTINUITY",
            [
                "比较同一精确分镜的前后相邻镜头，区分同场景连续动作与明确转场。",
                "每个相邻镜头均引用其镜头说明和精确已选图；缺图不可冒充已比较。",
            ],
            True,
        ),
    ]
    return [
        ContinuityRuleDefinition(
            rule_id=code, category=category, compare=items, requires_script=script
        )
        for code, category, items, script in specifications
    ]


def reference_requirements(
    evidence: InvestigateContinuityResult, sources: dict[tuple[str, UUID], RuleSource]
) -> tuple[dict[str, list[RuleReferenceRequirement]], list[UUID]]:
    materials = evidence.materials
    shot = next(
        m.content["shot"]
        for m in materials
        if m.source_kind == "SHOT" and m.source_id == evidence.target.shot_id
    )
    script = next(m.content for m in materials if m.source_kind == "SCRIPT")
    scene: dict = next((s for s in script["scenes"] if s["scene_no"] == shot["scene_no"]), {})
    assets = {str(m.source_id): m for m in materials if m.source_kind == "ASSET"}
    groups: dict[str, list[RuleReferenceRequirement]] = {
        key: [] for key in ("CHARACTER", "COSTUME", "SCENE", "PROP", "ADJACENT")
    }

    def bind(role, subject, reference, adjacent=None):
        pending = {reference["asset_version_id"]} if reference else set()
        visited: set[str] = set()
        image_sources = []
        while pending:
            identity = min(pending)
            pending.remove(identity)
            if identity in visited:
                continue
            visited.add(identity)
            material = assets.get(identity)
            if material is None:
                continue
            source = sources["ASSET", material.source_id]
            if source.image_file_id is not None:
                image_sources.append(source)
            pending.update(
                reference_ids(material.content["asset"]["profile"].get("standard_image", {}))
                - visited
            )
        groups[role].append(
            RuleReferenceRequirement(
                subject=subject,
                role=role,
                image_sources=image_sources,
                adjacent_shot=sources["SHOT", adjacent.source_id] if adjacent else None,
            )
        )

    details = {item["character"]: item for item in shot.get("character_details", [])}
    characters = {item["character"]: item["asset"] for item in script["character_assets"]}
    for name in shot.get("characters", []):
        detail = details.get(name, {})
        bind("CHARACTER", name, detail.get("asset") or characters.get(name))
        bind("COSTUME", name, detail.get("costume"))
    bind(
        "SCENE",
        "目标场景",
        shot.get("scene_asset")
        or shot.get("location_asset")
        or scene.get("scene_asset")
        or scene.get("location_asset"),
    )
    props = {
        ref["asset_version_id"]: ref for value in (shot, scene) for ref in value.get("props", [])
    }
    for identity, material in assets.items():
        asset = material.content["asset"]
        if asset["asset_type"] == "PROP":
            props.setdefault(identity, {"asset_version_id": identity})
    for identity, ref in sorted(props.items()):
        bind("PROP", identity, ref)
    scene_changes = []
    for material in materials:
        if material.source_kind != "SHOT" or material.source_id == evidence.target.shot_id:
            continue
        neighbor = material.content["shot"]
        bind("ADJACENT", str(material.source_id), neighbor.get("selected_visual_asset"), material)
        if neighbor["scene_no"] != shot["scene_no"]:
            scene_changes.append(material.source_id)
    return groups, scene_changes


def check_continuity_rules(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    actor: str,
    parameters: ContinuityRulesParameters,
) -> dict:
    trace = read_trace(db, run, "storyboard", actor)
    row = next(
        (
            item
            for item in trace["evidence"]
            if item["manifest_id"] == str(parameters.evidence_manifest_id)
        ),
        None,
    )
    if row is None or not row.get("continuity_investigation"):
        raise DomainError("REVIEW_REFERENCE_INVALID", "规则检查须引用本运行的精确取证清单", 422)
    evidence = InvestigateContinuityResult.model_validate(row["continuity_investigation"]["result"])
    if (
        evidence.target != parameters.target
        or evidence.manifest_id != parameters.evidence_manifest_id
    ):
        raise DomainError("REVIEW_TARGET_INVALID", "规则检查与取证目标不一致", 409)
    candidate_material = next(m for m in evidence.materials if m.source_kind == "CANDIDATE")
    episode = db.get(Episode, run.episode_id)
    assert episode is not None
    require_evidence_candidate(db, run, episode.project_id, actor, row["continuity_investigation"])
    references = resolve_assets(
        db,
        episode.project_id,
        {str(m.source_id) for m in evidence.materials if m.source_kind == "ASSET"},
        ContextLimits.model_validate(trace["task_context"]["limits"]),
        follow_profile_references=False,
    )
    for reference in references:
        asset = db.get(VisualAsset, UUID(reference["asset_id"]))
        assert asset is not None
        if not allowed_for_episode(asset, run.episode_id):
            raise DomainError("CONTEXT_SCOPE_NOT_ALLOWED", "参考不在当前剧集使用范围", 403)
    for material in evidence.materials:
        if material.source_kind != "SHOT":
            continue
        shot = db.get(Shot, material.source_id, populate_existing=True)
        if shot is None or shot.storyboard_id != parameters.target.storyboard_id:
            raise DomainError("REVIEW_REFERENCE_INVALID", "取证镜头不属于目标分镜", 422)
        if shot_sha256(shot) != material.content["shot_sha256"]:
            raise DomainError("REVIEW_REFERENCE_CHANGED", "取证镜头与原精确内容不一致", 409)
        if shot.status != "APPROVED":
            raise DomainError("CONTEXT_INPUT_NOT_USABLE", "取证镜头已失效", 409)
    loaded = load_context_images(
        db,
        settings,
        run,
        "storyboard",
        actor,
        evidence.image_file_ids,
        through_round=evidence.evidence_round,
    )
    sources: dict[tuple[str, UUID], RuleSource] = {}
    for material in evidence.materials:
        if material.content_sha256 != digest(material.content):
            raise DomainError("REVIEW_REFERENCE_CHANGED", "取证材料摘要不匹配", 409)
        image = material.content.get("image")
        sources[material.source_kind, material.source_id] = RuleSource(
            source_kind=material.source_kind,
            source_id=material.source_id,
            content_sha256=material.content_sha256,
            image_file_id=UUID(image["file_id"])
            if image and UUID(image["file_id"]) in loaded
            else None,
        )
    groups, scene_changes = reference_requirements(evidence, sources)
    same_scene = [
        ref
        for ref in groups["ADJACENT"]
        if ref.adjacent_shot is not None and ref.adjacent_shot.source_id not in scene_changes
    ]
    roles = {
        "APPEARANCE": groups["CHARACTER"],
        "COSTUME": groups["COSTUME"],
        "SCENE": groups["SCENE"],
        "PROP": groups["PROP"],
        "INTENT": [],
        "COMPOSITION": [],
        "HOLDER": groups["PROP"] + same_scene,
        "POSITION": same_scene,
        "STATE": groups["PROP"] + same_scene,
        "ADJACENT": groups["ADJACENT"],
    }
    comparisons = {item.rule_id: item for item in parameters.comparisons}
    for submitted in parameters.comparisons:
        for citation in submitted.citations:
            source = sources.get((citation.source_kind, citation.source_id))
            if source is None:
                raise DomainError("REVIEW_REFERENCE_INVALID", "比较引用不属于指定取证清单", 422)
            if source.content_sha256 != citation.content_sha256:
                raise DomainError("REVIEW_REFERENCE_CHANGED", "比较引用须保留精确材料摘要", 409)
            if citation.region is not None and source.image_file_id is None:
                raise DomainError(
                    "REVIEW_EVIDENCE_INVALID", "图片区域须定位到本轮实际读取的受控图片", 422
                )
    checks = []
    for definition in rule_definitions():
        code = definition.rule_id
        applicable = {
            "APPEARANCE": bool(groups["CHARACTER"]),
            "COSTUME": bool(groups["CHARACTER"]),
            "PROP": bool(groups["PROP"]),
            "HOLDER": bool(groups["PROP"]),
            "STATE": bool(groups["PROP"]),
            "ADJACENT": bool(groups["ADJACENT"]),
        }.get(code, True)
        comparison = comparisons.get(code)
        if not applicable and comparison and comparison.status != "NOT_APPLICABLE":
            raise DomainError(
                "REVIEW_RULE_NOT_APPLICABLE", "比较项与冻结镜头的实际适用范围不符", 422
            )
        gaps = []
        candidate = sources["CANDIDATE", candidate_material.source_id]
        if applicable:
            if candidate.image_file_id is None:
                gaps.append("CANDIDATE_IMAGE_NOT_LOADED")
            for requirement in roles[code]:
                if not requirement.image_sources:
                    gaps.append(
                        "MISSING_REFERENCE_IMAGE:" + requirement.role + ":" + requirement.subject
                    )
            if comparison:
                cited = {(item.source_kind, item.source_id) for item in comparison.citations}
                required = {
                    ("CANDIDATE", evidence.target.candidate_id),
                    ("SHOT", evidence.target.shot_id),
                }
                required.update(
                    key
                    for key in sources
                    if key[0] == "VISUAL" or definition.requires_script and key[0] == "SCRIPT"
                )
                for key in sorted(required):
                    if key not in cited:
                        gaps.append("UNCITED_CONTEXT:" + key[0] + ":" + str(key[1]))
                for requirement in roles[code]:
                    if requirement.image_sources and not any(
                        (source.source_kind, source.source_id) in cited
                        for source in requirement.image_sources
                    ):
                        gaps.append(
                            "UNCITED_REFERENCE:" + requirement.role + ":" + requirement.subject
                        )
                    if (
                        requirement.adjacent_shot
                        and ("SHOT", requirement.adjacent_shot.source_id) not in cited
                    ):
                        gaps.append(
                            "UNCITED_ADJACENT_SHOT:" + str(requirement.adjacent_shot.source_id)
                        )
                if comparison.status == "NOT_APPLICABLE":
                    gaps.append("APPLICABLE_RULE_CANNOT_BE_SKIPPED")
        status: RuleCheckStatus
        if not applicable:
            status = "NOT_APPLICABLE"
        elif comparison is None:
            status = "NOT_CHECKED"
        elif gaps:
            status = "UNDETERMINED"
        else:
            status = comparison.status
        checks.append(
            ContinuityRuleCheck(
                definition=definition,
                applicable=applicable,
                references=roles[code],
                status=status,
                gaps=gaps,
                comparison=comparison,
            )
        )
    complete = bool(comparisons) and all(
        not item.gaps and item.status in {"MATCH", "DIFFERENCE", "NOT_APPLICABLE"}
        for item in checks
    )
    human = not complete or any(
        item.uncertainty or item.risk in {"HIGH", "CRITICAL"} for item in parameters.comparisons
    )
    conclusion: RuleConclusion | None = None
    if comparisons:
        if human:
            conclusion = "HUMAN_REVIEW"
        elif any(item.status == "DIFFERENCE" for item in parameters.comparisons):
            conclusion = "REVISION_REQUIRED"
        else:
            conclusion = "PASS"
    result = ContinuityRulesResult(
        evidence_manifest_id=evidence.manifest_id,
        target=evidence.target,
        checks=checks,
        context_sources=list(sources.values()),
        scene_change_shot_ids=scene_changes,
        complete=complete,
        suggested_conclusion=conclusion,
    )
    value = result.model_dump(mode="json")
    if len(json.dumps(value, ensure_ascii=False).encode()) > 65536:
        raise DomainError("CONTEXT_TOO_LARGE", "规则检查反馈超过64 KiB，请缩小本轮比较", 422)
    return value


def continuity_rules_tool(settings: Settings) -> AgentToolDefinition:
    def check(context: AgentToolContext, parameters: AgentToolContract) -> dict:
        assert isinstance(parameters, ContinuityRulesParameters)
        run = context.db.get(WorkflowRun, context.run_id)
        assert run is not None
        return check_continuity_rules(context.db, settings, run, context.actor_id, parameters)

    return AgentToolDefinition(
        name=RULES_TOOL,
        allowed_agents=frozenset({"continuity_reviewer"}),
        allowed_steps=frozenset({"storyboard"}),
        parameters_model=ContinuityRulesParameters,
        result_model=ContinuityRulesResult,
        scope_validator=lambda context, parameters: None,
        handler=check,
        requires_write=True,
    )
