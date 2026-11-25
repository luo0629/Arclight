"""Deterministic performance checks on the existing director runtime and receipts."""

from dataclasses import dataclass

from pydantic import BaseModel, ValidationError
from pydantic.json_schema import models_json_schema
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import (
    ScriptContent,
    ScriptContentV3,
    StoryboardContentV3,
)
from arclight.core.errors import DomainError
from arclight.domain.director_performance import (
    PERFORMANCE_PLAN_PROMPT,
    PERFORMANCE_PLAN_VERSION,
    PERFORMANCE_REVISION_PROMPT,
    PERFORMANCE_REVISION_VERSION,
    PERFORMANCE_WORKING_PROMPT,
    PERFORMANCE_WORKING_VERSION,
    PerformanceCheckParameters,
    PerformanceDirectorPlan,
    PerformanceDirectorSeed,
    PerformanceRevisionDecision,
    PerformanceRevisionSeed,
    PerformanceWorkingFragment,
    PerformanceWorkingScene,
    PerformanceWorkingSeed,
    PerformanceWorkingStoryboard,
    is_performance_director,
)
from arclight.domain.director_planning import (
    PLAN_VERSION,
    PLANNING_PROMPT,
    RESEARCH_TOOL,
    DirectorEvidence,
    DirectorPlan,
    DirectorSeed,
)
from arclight.domain.director_revision import (
    REVISION_PROMPT,
    REVISION_VERSION,
    WORKING_PROMPT,
    WORKING_VERSION,
    DirectorCheckParameters,
    DirectorCheckResult,
    DirectorIssue,
    DirectorMetrics,
    DirectorRevisionDecision,
    DirectorRevisionSeed,
    DirectorSceneMetrics,
    DirectorWorkingFragment,
    DirectorWorkingScene,
    DirectorWorkingSeed,
    DirectorWorkingStoryboard,
)
from arclight.domain.lip_sync import MAX_SYNC_LINES, MAX_SYNC_MS
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoopAction
from arclight.persistence.models import Episode, WorkflowRun
from arclight.providers.media_capabilities import adapter_contract, resolve_capabilities


@dataclass(frozen=True)
class DirectorContracts:
    plan_version: str
    plan_prompt: str
    plan: type[DirectorPlan]
    plan_seed: type[DirectorSeed]
    working_version: str
    working_prompt: str
    fragment: type[DirectorWorkingFragment]
    working_seed: type[DirectorWorkingSeed]
    scene: type[DirectorWorkingScene]
    work: type[DirectorWorkingStoryboard]
    revision_version: str
    revision_prompt: str
    revision_seed: type[DirectorRevisionSeed]
    parameters: type[DirectorCheckParameters]
    decision: type[DirectorRevisionDecision]


def director_contracts(workflow_version: str) -> DirectorContracts:
    if is_performance_director(workflow_version):
        return DirectorContracts(
            PERFORMANCE_PLAN_VERSION,
            PERFORMANCE_PLAN_PROMPT,
            PerformanceDirectorPlan,
            PerformanceDirectorSeed,
            PERFORMANCE_WORKING_VERSION,
            PERFORMANCE_WORKING_PROMPT,
            PerformanceWorkingFragment,
            PerformanceWorkingSeed,
            PerformanceWorkingScene,
            PerformanceWorkingStoryboard,
            PERFORMANCE_REVISION_VERSION,
            PERFORMANCE_REVISION_PROMPT,
            PerformanceRevisionSeed,
            PerformanceCheckParameters,
            PerformanceRevisionDecision,
        )
    return DirectorContracts(
        PLAN_VERSION,
        PLANNING_PROMPT,
        DirectorPlan,
        DirectorSeed,
        WORKING_VERSION,
        WORKING_PROMPT,
        DirectorWorkingFragment,
        DirectorWorkingSeed,
        DirectorWorkingScene,
        DirectorWorkingStoryboard,
        REVISION_VERSION,
        REVISION_PROMPT,
        DirectorRevisionSeed,
        DirectorCheckParameters,
        DirectorRevisionDecision,
    )


def offset_working_shot(shot, offset: int):
    value = shot.model_copy(update={"shot_no": offset + shot.shot_no}, deep=True)
    if hasattr(value, "performance") and value.performance.continuity_from_shot is not None:
        value.performance.continuity_from_shot += offset
    return value


def require_performance_script(script: ScriptContent) -> ScriptContentV3:
    if not isinstance(script, ScriptContentV3):
        raise DomainError(
            "PERFORMANCE_SCRIPT_V3_REQUIRED", "表演导演需要精确批准的3.0剧情剧本", 422
        )
    return script


def performance_text_definitions(workflow_version: str) -> list[tuple[str, str, dict]]:
    from arclight.domain.director_planning import (
        InvestigateProductionParameters,
        InvestigateProductionResult,
    )

    contracts = director_contracts(workflow_version)
    models: dict[str, type[BaseModel]] = {
        "planning": contracts.plan,
        "planning_seed": contracts.plan_seed,
        "fragment": contracts.fragment,
        "fragment_seed": contracts.working_seed,
        "work": contracts.work,
        "revision": contracts.decision,
        "revision_seed": contracts.revision_seed,
        "check_parameters": contracts.parameters,
        "check_result": DirectorCheckResult,
        "research_parameters": InvestigateProductionParameters,
        "research_result": InvestigateProductionResult,
        "candidate": StoryboardContentV3,
    }
    roots, schema = models_json_schema([(model, "validation") for model in models.values()])
    # Shared V3 script/performance definitions are stored once, with every complete root retained.
    schema.update({name: roots[model, "validation"] for name, model in models.items()})
    return [
        (
            "AGENT",
            workflow_version,
            {
                "operation": "storyboard",
                "workflow_version": workflow_version,
                "runtime": "persistent-agent/1",
                "planning_version": contracts.plan_version,
                "fragment_version": contracts.working_version,
                "revision_version": contracts.revision_version,
                "assembly": "complete-performance-scenes-checked-local-patches/1",
                "repair_policy": "frozen-bounded-performance-receipts/1",
                "human_escalation": "server-initial-question/1",
            },
        ),
        (
            "PROMPT",
            workflow_version,
            {
                "planning": contracts.plan_prompt,
                "fragment": contracts.working_prompt,
                "revision": contracts.revision_prompt,
            },
        ),
        (
            "SCHEMA",
            workflow_version,
            schema,
        ),
    ]


def _capability_fingerprint(declaration: dict) -> str:
    """Compare capability declarations independently of unordered task type serialization."""
    normalized = {**declaration, "task_types": sorted(declaration.get("task_types", []))}
    return digest(normalized)


def _capability_receipts(
    db: Session, run: WorkflowRun, plan: DirectorPlan | None
) -> set[tuple[str, str, str]]:
    from arclight.persistence.agent_loops import AgentLoop

    allowed = {item.model_dump_json() for item in plan.evidence} if plan else None
    found = set()
    actions = db.scalars(
        select(AgentLoopAction)
        .join(AgentLoop, AgentLoop.id == AgentLoopAction.loop_id)
        .where(AgentLoop.run_id == run.id)
    )
    for action in actions:
        feedback = action.result_json or {}
        if (
            action.kind != "TOOL"
            or action.request_json.get("tool_name") != RESEARCH_TOOL
            or feedback.get("status") != "SUCCEEDED"
        ):
            continue
        for material in feedback.get("result", {}).get("materials", []):
            if material.get("source_kind") != "CAPABILITY":
                continue
            reference = DirectorEvidence.model_validate(
                {key: value for key, value in material.items() if key != "content"}
            )
            if allowed is not None and reference.model_dump_json() not in allowed:
                continue
            content = material["content"]
            declaration = content["declaration"]
            if digest(content) != reference.content_sha256:
                continue
            found.add(
                (declaration["provider"], content["model"], _capability_fingerprint(declaration))
            )
    return found


def check_performance_storyboard(
    db: Session,
    content: StoryboardContentV3,
    script: ScriptContent,
    *,
    run: WorkflowRun | None = None,
    plan: DirectorPlan | None = None,
) -> list[DirectorIssue]:
    """Return visible blockers; callers must never turn these into implicit waivers."""
    issues: list[DirectorIssue] = []
    try:
        content.validate_against(script)
    except ValueError as exc:
        issues.append(DirectorIssue(code="PERFORMANCE_SCRIPT_REFERENCE", message=str(exc)[:500]))
    expected = [line.line_id for scene in script.scenes for line in scene.dialogue]
    actual = [line for shot in content.shots for line in shot.dialogue_line_ids]
    if actual != expected:
        issues.append(
            DirectorIssue(
                code="PERFORMANCE_DIALOGUE_ORDER",
                message="必须按原顺序完整覆盖精确台词且每句恰好一次",
            )
        )
    if isinstance(script, ScriptContentV3):
        for scene in script.scenes:
            if scene.dramatic_beat.dialogue_interactions and not any(
                shot.performance.interactions
                for shot in content.shots
                if shot.scene_id == scene.scene_id
            ):
                issues.append(
                    DirectorIssue(
                        code="PERFORMANCE_INTERACTION_MISSING",
                        scene_id=scene.scene_id,
                        message="原场景存在冲突对话互动，表演分镜必须保留行动与反应，不能全部转画外音",
                    )
                )
    receipts = _capability_receipts(db, run, plan) if run else None
    routes = []
    for route in content.generation_routes:
        try:
            profile = resolve_capabilities(route.provider, route.model)
        except DomainError as exc:
            issues.append(
                DirectorIssue(
                    code=exc.code,
                    message=f"生成路线{route.provider}/{route.model}不可用：{exc.message}",
                )
            )
            continue
        if (
            receipts is not None
            and (
                route.provider,
                route.model,
                _capability_fingerprint(profile.model_dump(mode="json")),
            )
            not in receipts
        ):
            issues.append(
                DirectorIssue(
                    code="PERFORMANCE_CAPABILITY_UNVERIFIED",
                    message=f"生成路线{route.provider}/{route.model}缺少本计划实际成功的精确能力调查回执",
                )
            )
            continue
        adapter = adapter_contract(route.provider)
        if adapter is not None and adapter.simulation:
            issues.append(
                DirectorIssue(
                    code="PERFORMANCE_MOCK_NOT_PRODUCTION",
                    message="Mock声明只供确定性回归，不能证明真实表演生产能力",
                )
            )
            continue
        routes.append(profile)
    scene_slots: dict[int, int] = {}
    for shot in content.shots:
        slot = scene_slots.get(shot.scene_no, 0)
        scene_slots[shot.scene_no] = slot + 1
        dynamic = shot.performance.motion_requirement == "dynamic_required"
        if dynamic:
            issues.append(
                DirectorIssue(
                    code="PERFORMANCE_MOTION_UNVERIFIED",
                    scene_id=shot.scene_id,
                    slot=slot,
                    message="VIDEO及参考图声明不能证明人物动作、互动和道具接续已真实验收；当前缺少对应时序证据，须保留要求并转人工",
                )
            )
        kind = "VIDEO" if dynamic else "IMAGE"
        required_refs = len(
            {
                item.asset_version_id
                for item in [
                    *(
                        detail.asset
                        for detail in shot.character_details
                        if detail.asset is not None
                    ),
                    *shot.props,
                    *([shot.scene_asset] if shot.scene_asset else []),
                    *([shot.location_asset] if shot.location_asset else []),
                ]
            }
        )
        if dynamic:
            required_refs = max(1, required_refs)
        capable = [
            profile
            for profile in routes
            if kind in profile.task_types
            and profile.max_reference_images >= required_refs
            and (
                not dynamic
                or (
                    profile.max_video_seconds is not None
                    and shot.duration_ms <= profile.max_video_seconds * 1000
                )
            )
        ]
        if not capable:
            issues.append(
                DirectorIssue(
                    code="PERFORMANCE_CAPABILITY_UNSUPPORTED",
                    scene_id=shot.scene_id,
                    slot=slot,
                    message=f"镜头需要{kind}、至少{required_refs}张精确参考及足够时长，已调查真实路线无法满足；须修订或人工决定，不能降为静图运镜",
                )
            )
        visible = sum(line.lipsync == "required" for line in shot.performance.dialogue)
        if visible:
            bounded = visible <= MAX_SYNC_LINES and shot.duration_ms <= MAX_SYNC_MS
            issues.append(
                DirectorIssue(
                    code="PERFORMANCE_LIPSYNC_UNVERIFIED"
                    if bounded
                    else "PERFORMANCE_LIPSYNC_LIMIT",
                    scene_id=shot.scene_id,
                    slot=slot,
                    message=(
                        "可走本机口型校正；仍须选用实际台词音频、校正并复核该镜头"
                        if bounded
                        else "可见对白超出本机口型范围；请拆分镜头，不能用豁免代替"
                    ),
                )
            )
    return issues


def performance_publication_content(
    work: PerformanceWorkingStoryboard, plan: PerformanceDirectorPlan
) -> StoryboardContentV3:
    return StoryboardContentV3.model_validate(
        {
            "schema_version": "3.0",
            "generation_routes": [route.model_dump() for route in plan.generation_routes],
            "shots": [
                shot.model_dump(mode="json") for scene in work.scenes for shot in scene.shots
            ],
        }
    )


def check_performance_work(db, run, work, plan, settings, checksum) -> DirectorCheckResult:
    from arclight.application.content_duration import duration_bounds
    from arclight.application.content_references import validate_storyboard_assets
    from arclight.application.director_validation import revision_input
    from arclight.application.short_drama_director import short_drama_media_issues

    script = revision_input(db, run, plan, settings)
    episode = db.get(Episode, run.episode_id)
    assert episode is not None
    issues = short_drama_media_issues(db, run, episode.project_id, work)
    if [scene.scene_id for scene in work.scenes] != [scene.scene_id for scene in script.scenes]:
        issues.append(
            DirectorIssue(code="DIRECTOR_SCENE_ORDER", message="须完整保留剧本场景及原顺序")
        )
    try:
        content = performance_publication_content(work, plan)
    except ValidationError as exc:
        issues.append(DirectorIssue(code="PERFORMANCE_CONTRACT", message=str(exc)[:500]))
    else:
        issues.extend(check_performance_storyboard(db, content, script, run=run, plan=plan))
        try:
            validate_storyboard_assets(db, episode.project_id, content, episode_id=episode.id)
        except DomainError as exc:
            issues.append(DirectorIssue(code=exc.code, message=exc.message))
    estimates = {scene.scene_id: scene.estimated_duration_ms for scene in plan.scenes}
    metrics = []
    for scene in work.scenes:
        actual = sum(shot.duration_ms for shot in scene.shots)
        estimate = estimates.get(scene.scene_id, 0)
        metrics.append(
            DirectorSceneMetrics(
                scene_id=scene.scene_id,
                duration_ms=actual,
                expected_duration_ms=estimate,
                shot_count=len(scene.shots),
            )
        )
        if actual != estimate:
            issues.append(
                DirectorIssue(
                    code="DIRECTOR_SCENE_DURATION",
                    scene_id=scene.scene_id,
                    message=f"场景镜头合计{actual}ms，须等于原计划{estimate}ms",
                )
            )
    lower, upper = duration_bounds(
        run.input_json["target_duration_ms"], settings.text_duration_tolerance_percent
    )
    duration = sum(scene.duration_ms for scene in metrics)
    if not lower <= duration <= upper:
        issues.append(
            DirectorIssue(
                code="STORYBOARD_DURATION_OUT_OF_RANGE",
                message=f"镜头总时长{duration}ms不在允许的[{lower},{upper}]ms内",
            )
        )
    lines = [
        line for scene in work.scenes for shot in scene.shots for line in shot.dialogue_line_ids
    ]
    return DirectorCheckResult(
        accepted=True,
        valid=not issues,
        check_sha256=checksum,
        content_sha256=digest(work.model_dump(mode="json")),
        issue_count=len(issues),
        issues_truncated=len(issues) > 48,
        issues=issues[:48],
        metrics=DirectorMetrics(
            expected_lines=sum(len(scene.dialogue) for scene in script.scenes),
            covered_lines=len(set(lines)),
            dialogue_occurrences=len(lines),
            shot_count=sum(len(scene.shots) for scene in work.scenes),
            duration_ms=duration,
            lower_duration_ms=lower,
            upper_duration_ms=upper,
            scenes=metrics,
        ),
    )
