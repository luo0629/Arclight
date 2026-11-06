"""Read-only checks of patches against a server-owned working draft and user scope."""

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import Dialogue, ScriptContent
from arclight.agents.script_rewrite import (
    ParagraphTarget,
    RewriteRequest,
    RewriteTarget,
)
from arclight.application.agent_tools import AgentToolContext, AgentToolDefinition
from arclight.application.content_duration import validate_duration
from arclight.application.content_references import validate_script_assets
from arclight.application.rewrite_content import (
    check_instructions,
    content_hash,
    merge_rewrite,
    paragraph_field,
    paragraph_text,
    selected_scenes,
)
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.screenwriter_revision import (
    PATCH_TOOL,
    REVISION_WORKFLOWS,
    CheckPatchParameters,
    CheckPatchResult,
    DeterministicIssue,
    RevisionSeed,
)
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import WorkflowRun


def effective_target(
    original: ScriptContent, current: ScriptContent, target: RewriteTarget
) -> RewriteTarget:
    """A paragraph's new length may change; its original Unicode prefix/suffix cannot."""
    if not isinstance(target, ParagraphTarget):
        return target
    old_scene = selected_scenes(original, target)[0]
    old = paragraph_text(old_scene, target)
    new_scene = selected_scenes(current, target)[0]
    field = paragraph_field(new_scene, target)
    new = field.text if isinstance(field, Dialogue) else field.action
    before, after = old[: target.start], old[target.end :]
    if not new.startswith(before) or (after and not new.endswith(after)):
        raise DomainError("SCREENWRITER_PATCH_SCOPE", "原Unicode选区外的文本不能变化", 422)
    end = len(new) - len(after)
    if end <= target.start:
        raise DomainError("SCREENWRITER_PATCH_SCOPE", "工作选区不得为空", 422)
    return target.model_copy(update={"end": end})


def deterministic_issues(
    db: Session, run: WorkflowRun, source: ScriptContent, settings: Settings
) -> list[DeterministicIssue]:
    issues = []
    try:
        validate_duration(
            source, run.input_json["target_duration_ms"], settings.text_duration_tolerance_percent
        )
        from arclight.persistence.models import Episode

        episode = db.get(Episode, run.episode_id)
        assert episode is not None
        validate_script_assets(db, episode.project_id, source)
        if "rewrite_input" in run.input_json:
            request = RewriteRequest.model_validate(run.input_json["rewrite_input"]["request"])
            scenes = selected_scenes(source, request.target)
            if isinstance(request.target, ParagraphTarget):
                field = paragraph_field(scenes[0], request.target)
                lines = [field] if isinstance(field, Dialogue) else []
            else:
                lines = [line for scene in scenes for line in scene.dialogue]
            check_instructions(request.instructions, lines)
    except DomainError as exc:
        issues.append(DeterministicIssue(code=exc.code, message=exc.message))
    except ValueError:
        issues.append(
            DeterministicIssue(
                code="REWRITE_INSTRUCTIONS_INVALID", message="工作稿未满足原改写指令"
            )
        )
    return issues


def patch_seed(db: Session, run: WorkflowRun, operation_key: str) -> tuple[AgentLoop, RevisionSeed]:
    if run.workflow_version not in REVISION_WORKFLOWS:
        raise DomainError("SCREENWRITER_PATCH_SCOPE", "此运行未授权评价补丁检查", 422)
    stage = "rewrite" if run.workflow_type == "script_rewrite" else "script"
    loop = db.scalar(
        select(AgentLoop).where(
            AgentLoop.run_id == run.id,
            AgentLoop.operation_sha256
            == digest({"run": str(run.id), "stage": stage, "operation": operation_key}),
        )
    )
    if loop is None:
        raise DomainError("SCREENWRITER_PATCH_SCOPE", "补丁必须引用当前持久评价操作", 422)
    try:
        seed = RevisionSeed.model_validate(loop.seed_json["messages"][0]["data"])
    except (ValidationError, KeyError, IndexError):
        raise DomainError("SCREENWRITER_PATCH_SCOPE", "此操作不是评价工作稿", 422) from None
    return loop, seed


def merge_checked_patch(
    db: Session,
    run: WorkflowRun,
    seed: RevisionSeed,
    parameters: CheckPatchParameters,
    settings: Settings,
) -> ScriptContent:
    if parameters.operation_key != seed.operation_key:
        raise DomainError("SCREENWRITER_PATCH_SCOPE", "补丁不能借用其他评价操作的回执", 422)
    if parameters.source_sha256 != seed.source_sha256 or (
        content_hash(seed.source_script.model_dump(mode="json")) != seed.source_sha256
    ):
        raise DomainError("SCREENWRITER_PATCH_SCOPE", "补丁来源不是当前精确工作稿", 422)
    if seed.remaining_patches == 0:
        raise DomainError("SCREENWRITER_REVISION_EXHAUSTED", "修订次数已耗尽，须携问题转人工", 409)
    selected = selected_scenes(seed.source_script, parameters.target)
    if seed.permitted_target is not None:
        allowed = selected_scenes(seed.source_script, seed.permitted_target)
        if not {scene.scene_id for scene in selected} <= {scene.scene_id for scene in allowed}:
            raise DomainError("SCREENWRITER_PATCH_SCOPE", "补丁不能扩大用户选择的场景", 422)
        if (
            isinstance(seed.permitted_target, ParagraphTarget)
            and parameters.target != seed.permitted_target
        ):
            raise DomainError(
                "SCREENWRITER_PATCH_SCOPE", "补丁不能更换原动作／台词Unicode选区", 422
            )
        if parameters.instructions != seed.instructions:
            raise DomainError("SCREENWRITER_PATCH_SCOPE", "补丁不能更换用户改写指令", 422)
    request = RewriteRequest(
        base_version_id=run.id, target=parameters.target, instructions=parameters.instructions
    )
    content = merge_rewrite(
        {
            "source_script": seed.source_script.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
        },
        parameters.patch,
    )
    issues = deterministic_issues(db, run, content, settings)
    if issues:
        raise DomainError(issues[0].code, issues[0].message, 422)
    return content


def patch_tool(settings: Settings) -> AgentToolDefinition:
    def check(context: AgentToolContext, parameters) -> dict:
        assert isinstance(parameters, CheckPatchParameters)
        run = context.db.get(WorkflowRun, context.run_id)
        assert run is not None
        loop, seed = patch_seed(context.db, run, parameters.operation_key)
        if loop.status != "RUNNING":
            raise DomainError("SCREENWRITER_PATCH_SCOPE", "只能检查当前活动评价的补丁", 422)
        try:
            content = merge_checked_patch(context.db, run, seed, parameters, settings)
            result = CheckPatchResult(
                accepted=True,
                patch_sha256=digest(parameters.model_dump(mode="json")),
                content_sha256=content_hash(content.model_dump(mode="json")),
                issues=[],
            )
        except (DomainError, ValueError) as exc:
            issue = DeterministicIssue(
                code=exc.code if isinstance(exc, DomainError) else "SCREENWRITER_PATCH_INVALID",
                message=exc.message
                if isinstance(exc, DomainError)
                else "补丁违反原范围、身份、引用或指令合同",
            )
            result = CheckPatchResult(
                accepted=False,
                patch_sha256=digest(parameters.model_dump(mode="json")),
                issues=[issue],
            )
        return result.model_dump(mode="json")

    return AgentToolDefinition(
        name=PATCH_TOOL,
        allowed_agents=frozenset({"screenwriter"}),
        allowed_steps=frozenset({"script", "rewrite"}),
        parameters_model=CheckPatchParameters,
        result_model=CheckPatchResult,
        scope_validator=lambda context, parameters: None,
        handler=check,
    )
