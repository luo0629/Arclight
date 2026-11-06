"""Located, deterministic checks reuse the original shot, asset and duration contracts."""

from typing import TypedDict

from pydantic import ValidationError
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContent, ShotContent, StoryboardContent
from arclight.application.content_duration import duration_bounds
from arclight.application.content_references import validate_storyboard_assets
from arclight.application.director_research import approved_input
from arclight.application.gateway_text import execution_actor
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.director_planning import DirectorPlan
from arclight.domain.director_revision import (
    DirectorCheckResult,
    DirectorIssue,
    DirectorMetrics,
    DirectorSceneMetrics,
    DirectorWorkingStoryboard,
)
from arclight.domain.versions import digest
from arclight.persistence.models import Episode, WorkflowRun


class _ShotLocation(TypedDict):
    scene_id: str
    slot: int


def revision_input(
    db: Session, run: WorkflowRun, plan: DirectorPlan, settings: Settings
) -> ScriptContent:
    script, _ = approved_input(db, run, execution_actor(db, run), settings)
    if script.id != plan.script_version_id or digest(script.content_json) != plan.script_sha256:
        raise DomainError("DIRECTOR_REVISION_SCOPE", "检查须保留原精确批准剧本和计划", 409)
    return ScriptContent.model_validate(script.content_json)


def check_work(
    db: Session,
    run: WorkflowRun,
    work: DirectorWorkingStoryboard,
    plan: DirectorPlan,
    settings: Settings,
    checksum: str,
) -> DirectorCheckResult:
    script = revision_input(db, run, plan, settings)
    episode = db.get(Episode, run.episode_id)
    assert episode is not None
    issues: list[DirectorIssue] = []
    expected = {line.line_id: scene for scene in script.scenes for line in scene.dialogue}
    ordered_lines = {line: index for index, line in enumerate(expected)}
    script_scenes = {scene.scene_id: scene for scene in script.scenes}
    estimates = {scene.scene_id: scene.estimated_duration_ms for scene in plan.scenes}
    if [scene.scene_id for scene in work.scenes] != [scene.scene_id for scene in script.scenes]:
        issues.append(
            DirectorIssue(code="DIRECTOR_SCENE_ORDER", message="须完整保留剧本场景及原顺序")
        )
    seen: set[str] = set()
    covered: set[str] = set()
    last_line = -1
    number = 0
    occurrences = 0
    metrics = []
    for scene in work.scenes:
        origin = script_scenes.get(scene.scene_id)
        if origin is None or scene.scene_no != origin.scene_no:
            issues.append(
                DirectorIssue(
                    code="DIRECTOR_SCENE_IDENTITY",
                    message="工作场景身份或编号不属于原剧本",
                    scene_id=scene.scene_id,
                )
            )
        for slot, shot in enumerate(scene.shots):
            number += 1
            location: _ShotLocation = {"scene_id": scene.scene_id, "slot": slot}
            if shot.shot_no != number:
                issues.append(
                    DirectorIssue(
                        code="DIRECTOR_SHOT_ORDER",
                        message="全局镜头编号须从1连续递增",
                        field="shot_no",
                        **location,
                    )
                )
            if shot.scene_id != scene.scene_id or shot.scene_no != scene.scene_no:
                issues.append(
                    DirectorIssue(
                        code="DIRECTOR_SHOT_SCENE",
                        message="镜头须保留所属场景的精确身份及编号",
                        field="scene_id",
                        **location,
                    )
                )
            if not 500 <= shot.duration_ms <= 30000:
                issues.append(
                    DirectorIssue(
                        code="DIRECTOR_SHOT_DURATION",
                        message="每镜时长须在[500,30000]毫秒内",
                        field="duration_ms",
                        **location,
                    )
                )
            for line_id in shot.dialogue_line_ids:
                occurrences += 1
                owner = expected.get(line_id)
                if owner is None or owner.scene_id != scene.scene_id:
                    issues.append(
                        DirectorIssue(
                            code="DIRECTOR_DIALOGUE_REFERENCE",
                            message="对白引用须属于原剧本当前场景",
                            line_id=line_id,
                            **location,
                        )
                    )
                else:
                    covered.add(line_id)
                if line_id in seen:
                    issues.append(
                        DirectorIssue(
                            code="DIRECTOR_DIALOGUE_DUPLICATE",
                            message="同一对白只能被镜头覆盖一次",
                            line_id=line_id,
                            **location,
                        )
                    )
                seen.add(line_id)
                if line_id in ordered_lines:
                    index = ordered_lines[line_id]
                    if index < last_line:
                        issues.append(
                            DirectorIssue(
                                code="DIRECTOR_DIALOGUE_ORDER",
                                message="对白引用须保持原剧本顺序",
                                line_id=line_id,
                                **location,
                            )
                        )
                    last_line = index
            try:
                strict = ShotContent.model_validate(shot.model_dump(mode="json"))
            except ValidationError as exc:
                fields = [".".join(map(str, item["loc"])) for item in exc.errors()[:16]]
                issues.append(
                    DirectorIssue(
                        code="DIRECTOR_SHOT_CONTRACT",
                        message="镜头违反原严格字段或重复引用合同",
                        field=",".join(fields)[:160],
                        **location,
                    )
                )
                continue
            # Check one strict shot with local number 1; global order was checked above.
            candidate = StoryboardContent(shots=[strict.model_copy(update={"shot_no": 1})])
            try:
                candidate.validate_against(script)
                validate_storyboard_assets(db, episode.project_id, candidate)
            except (ValueError, DomainError) as exc:
                issues.append(
                    DirectorIssue(
                        code=exc.code
                        if isinstance(exc, DomainError)
                        else "DIRECTOR_SHOT_REFERENCE",
                        message=exc.message
                        if isinstance(exc, DomainError)
                        else "镜头须保持有效说话人、场景及剧本精确资产绑定",
                        **location,
                    )
                )
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
                    message=f"场景镜头合计{actual}ms，须等于原计划{estimate}ms",
                    scene_id=scene.scene_id,
                )
            )
    for line_id, origin_scene in expected.items():
        if line_id not in covered:
            issues.append(
                DirectorIssue(
                    code="DIRECTOR_DIALOGUE_MISSING",
                    message="原剧本对白尚未被镜头覆盖",
                    scene_id=origin_scene.scene_id,
                    line_id=line_id,
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
    return DirectorCheckResult(
        accepted=True,
        valid=not issues,
        check_sha256=checksum,
        content_sha256=digest(work.model_dump(mode="json")),
        issue_count=len(issues),
        issues_truncated=len(issues) > 48,
        issues=issues[:48],
        metrics=DirectorMetrics(
            expected_lines=len(expected),
            covered_lines=len(covered),
            dialogue_occurrences=occurrences,
            shot_count=number,
            duration_ms=duration,
            lower_duration_ms=lower,
            upper_duration_ms=upper,
            scenes=metrics,
        ),
    )


def publication_content(work: DirectorWorkingStoryboard) -> StoryboardContent:
    """Reparse every shot; working model instances must never bypass strict validators."""
    return StoryboardContent.model_validate(
        {"shots": [shot.model_dump(mode="json") for scene in work.scenes for shot in scene.shots]}
    )
