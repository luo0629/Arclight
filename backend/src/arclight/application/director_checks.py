"""Server-owned director check seeds and bounded, read-only local patch previews."""

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_tools import AgentToolContext, AgentToolDefinition
from arclight.application.director_validation import check_work
from arclight.application.generation_leases import clock
from arclight.application.model_gateway import utc
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.director_creation import ITERATIVE_DIRECTOR_VERSIONS
from arclight.domain.director_revision import (
    CHECK_TOOL,
    REVISION_VERSION,
    DirectorCheckParameters,
    DirectorCheckResult,
    DirectorIssue,
    DirectorRevisionSeed,
    DirectorWorkingStoryboard,
)
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import WorkflowRun


def check_seed(db: Session, run: WorkflowRun, key: str) -> tuple[AgentLoop, DirectorRevisionSeed]:
    if run.workflow_version not in ITERATIVE_DIRECTOR_VERSIONS:
        raise DomainError("DIRECTOR_REVISION_SCOPE", "此运行未授权导演检查修订", 422)
    loop = db.scalar(
        select(AgentLoop).where(
            AgentLoop.run_id == run.id,
            AgentLoop.operation_sha256
            == digest({"run": str(run.id), "stage": "storyboard", "operation": key}),
        )
    )
    try:
        if loop is None:
            raise ValueError
        request = loop.seed_json
        if (
            request["agent_name"] != "director"
            or request["result_schema_version"] != REVISION_VERSION
        ):
            raise ValueError
        seed = DirectorRevisionSeed.model_validate(request["messages"][0]["data"])
        if (
            seed.operation_key != key
            or digest(seed.source.model_dump(mode="json")) != seed.source_sha256
        ):
            raise ValueError
    except (ValidationError, ValueError, KeyError, IndexError):
        raise DomainError(
            "DIRECTOR_REVISION_SCOPE", "检查必须引用原精确持久评价操作", 422
        ) from None
    return loop, seed


def merge_patch(
    seed: DirectorRevisionSeed, parameters: DirectorCheckParameters
) -> DirectorWorkingStoryboard:
    if (
        parameters.operation_key != seed.operation_key
        or parameters.source_sha256 != seed.source_sha256
    ):
        raise DomainError("DIRECTOR_REVISION_SCOPE", "补丁不能更换操作或精确来源哈希", 422)
    patch = parameters.patch
    if patch is None:
        return seed.source.model_copy(deep=True)
    if seed.remaining_patches == 0:
        raise DomainError("DIRECTOR_REVISION_EXHAUSTED", "修订次数已耗尽，须携问题转人工", 409)
    selected = next(
        (scene for scene in seed.source.scenes if scene.scene_id == patch.scene_id), None
    )
    if (
        selected is None
        or not 0 <= patch.start <= patch.end <= len(selected.shots)
        or patch.end - patch.start > 20
        or (patch.start == patch.end and not patch.shots)
        or len(selected.shots) - (patch.end - patch.start) + len(patch.shots) == 0
    ):
        raise DomainError(
            "DIRECTOR_PATCH_SCOPE", "补丁须选择一个场景内最多20镜头的连续范围且保留场景", 422
        )
    if any(
        shot.scene_id != selected.scene_id or shot.scene_no != selected.scene_no
        for shot in patch.shots
    ):
        raise DomainError("DIRECTOR_PATCH_SCOPE", "替换镜头不能更换所选场景身份", 422)
    work = seed.source.model_copy(deep=True)
    scene = next(scene for scene in work.scenes if scene.scene_id == patch.scene_id)
    scene.shots[patch.start : patch.end] = [shot.model_copy(deep=True) for shot in patch.shots]
    number = 0
    for scene in work.scenes:
        for shot in scene.shots:
            number += 1
            shot.shot_no = number
    work = DirectorWorkingStoryboard.model_validate(work.model_dump(mode="json"))
    if work == seed.source:
        raise DomainError("DIRECTOR_PATCH_EMPTY", "补丁须实际修订工作稿", 422)
    return work


def director_check_tool(settings: Settings) -> AgentToolDefinition:
    def check(context: AgentToolContext, parameters) -> dict:
        assert isinstance(parameters, DirectorCheckParameters)
        run = context.db.get(WorkflowRun, context.run_id)
        assert run is not None
        loop, seed = check_seed(context.db, run, parameters.operation_key)
        # Resuming keeps the last durable status until the next outcome. The live
        # fenced lease distinguishes execution from an unanswered or abandoned pause.
        if (
            loop.status not in {"RUNNING", "WAITING_HUMAN", "RECOVERY_REQUIRED"}
            or loop.lease_token is None
            or loop.lease_expires_at is None
            or utc(loop.lease_expires_at) <= clock(context.db)
        ):
            raise DomainError("DIRECTOR_REVISION_SCOPE", "只能检查当前活动的导演评价操作", 422)
        # Recheck live permission and frozen input even when the patch itself is malformed.
        from arclight.application.director_validation import revision_input

        revision_input(context.db, run, seed.plan, settings)
        checksum = digest(parameters.model_dump(mode="json"))
        try:
            work = merge_patch(seed, parameters)
        except (DomainError, ValueError) as exc:
            result = DirectorCheckResult(
                accepted=False,
                valid=False,
                check_sha256=checksum,
                issue_count=1,
                issues_truncated=False,
                issues=[
                    DirectorIssue(
                        code=exc.code if isinstance(exc, DomainError) else "DIRECTOR_PATCH_INVALID",
                        message=exc.message
                        if isinstance(exc, DomainError)
                        else "补丁超过工作稿合同或字节限制",
                    )
                ],
            )
        else:
            result = check_work(context.db, run, work, seed.plan, settings, checksum)
        return result.model_dump(mode="json")

    return AgentToolDefinition(
        name=CHECK_TOOL,
        allowed_agents=frozenset({"director"}),
        allowed_steps=frozenset({"storyboard"}),
        parameters_model=DirectorCheckParameters,
        result_model=DirectorCheckResult,
        scope_validator=lambda context, parameters: None,
        handler=check,
    )
