"""Give the shared Director the constraints of automatic short-drama production."""

from uuid import UUID

from sqlalchemy.orm import Session

from arclight.application.short_drama_budget import root_for_workflow
from arclight.domain.agent_model import AgentMessage, AgentUserMessage
from arclight.domain.character_names import canonical_name
from arclight.domain.director_revision import DirectorIssue, DirectorWorkingStoryboard
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.models import WorkflowRun

DIRECTOR_INSTRUCTIONS = """
本次分镜将直接用于短剧制作。按原场景和顺序为每条台词指定恰好一个镜头，保留全部台词。
配音由后续独立环节制作，逐镜为本镜台词留足自然语速所需时长。太密集时按原顺序拆镜。
音效是可选的。只可在 sound_effects 中引用后续数据所列的项目音效原名，可留空。
遵守后续数据中的镜头总数上限。保留原剧本的表演及对白要求。
"""


def director_model_input(
    db: Session, run: WorkflowRun, prompt: str, data: dict
) -> tuple[str, list[AgentMessage]]:
    messages: list[AgentMessage] = [AgentUserMessage(data=data)]
    if root_for_workflow(db, run.id) is None:
        return prompt, messages

    from arclight.application.short_drama_audio import available_sound_effects
    from arclight.application.short_drama_visuals import MAX_SHOTS

    control = db.get(AgentRunLimit, run.id)
    assert control is not None
    effects = available_sound_effects(db, control.project_id, limit=50)
    messages.append(
        AgentUserMessage(
            data={
                "production": "SHORT_DRAMA",
                "maximum_shots": MAX_SHOTS,
                "sound_effects": [asset.name for asset, _ in effects.values()],
            }
        )
    )
    return prompt + DIRECTOR_INSTRUCTIONS, messages


def short_drama_media_issues(
    db: Session, run: WorkflowRun, project_id: UUID, work: DirectorWorkingStoryboard
) -> list[DirectorIssue]:
    if root_for_workflow(db, run.id) is None:
        return []

    from arclight.application.short_drama_audio import available_sound_effects
    from arclight.application.short_drama_visuals import MAX_SHOTS

    issues = []
    if sum(len(scene.shots) for scene in work.scenes) > MAX_SHOTS:
        issues.append(
            DirectorIssue(code="SHORT_DRAMA_SHOT_LIMIT", message=f"镜头总数最多为 {MAX_SHOTS}")
        )
    names = {
        canonical_name(cue)
        for scene in work.scenes
        for shot in scene.shots
        for cue in shot.sound_effects
    }
    if names:
        effects = available_sound_effects(db, project_id, names=names)
        for scene in work.scenes:
            for slot, shot in enumerate(scene.shots):
                if any(canonical_name(cue) not in effects for cue in shot.sound_effects):
                    issues.append(
                        DirectorIssue(
                            code="SHORT_DRAMA_SFX_REQUIRED",
                            scene_id=scene.scene_id,
                            slot=slot,
                            field="sound_effects",
                            message="须使用唯一的已授权项目音效名称，该镜头也可省略音效",
                        )
                    )
    return issues
