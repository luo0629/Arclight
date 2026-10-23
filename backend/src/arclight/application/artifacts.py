from collections.abc import Callable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from arclight.agents.contracts import ScriptContent, StoryboardContent, read_content
from arclight.application.content_references import (
    validate_script_assets,
    validate_storyboard_assets,
)
from arclight.persistence.models import Episode, Script, Shot, Storyboard
from arclight.workflows.production import Artifacts


class ArtifactStore(Artifacts):
    def __init__(self, factory: sessionmaker, run_id: UUID, episode_id: UUID):
        self.factory = factory
        self.run_id = run_id
        self.episode_id = episode_id

    def script(self, generate: Callable[[], dict]) -> tuple[str, dict]:
        with self.factory.begin() as db:
            db.execute(
                select(Episode).where(Episode.id == self.episode_id).with_for_update(key_share=True)
            )
            existing = db.scalar(select(Script).where(Script.run_id == self.run_id))
            if existing:
                return str(existing.id), read_content(existing.content_json)
            parsed = ScriptContent.model_validate(generate())
            episode = db.get(Episode, self.episode_id)
            if episode is None:
                raise ValueError("剧集不存在")
            validate_script_assets(db, episode.project_id, parsed)
            content = parsed.model_dump(mode="json")
            version = (
                db.scalar(
                    select(func.max(Script.version_no)).where(Script.episode_id == self.episode_id)
                )
                or 0
            )
            script = Script(
                episode_id=self.episode_id,
                run_id=self.run_id,
                version_no=version + 1,
                content_json=content,
            )
            db.add(script)
            db.flush()
            return str(script.id), content

    def storyboard(self, script_id: str, generate: Callable[[], dict]) -> tuple[str, dict]:
        with self.factory.begin() as db:
            db.execute(
                select(Episode).where(Episode.id == self.episode_id).with_for_update(key_share=True)
            )
            existing = db.scalar(select(Storyboard).where(Storyboard.run_id == self.run_id))
            if existing:
                return str(existing.id), read_content(existing.content_json)
            parsed = StoryboardContent.model_validate(generate())
            script = db.get(Script, UUID(script_id))
            episode = db.get(Episode, self.episode_id)
            if script is None or episode is None or script.episode_id != self.episode_id:
                raise ValueError("分镜必须引用当前剧集的剧本版本")
            source = ScriptContent.model_validate(script.content_json)
            validate_script_assets(db, episode.project_id, source)
            parsed.validate_against(source)
            validate_storyboard_assets(db, episode.project_id, parsed)
            content = parsed.model_dump(mode="json")
            version = (
                db.scalar(
                    select(func.max(Storyboard.version_no)).where(
                        Storyboard.episode_id == self.episode_id
                    )
                )
                or 0
            )
            storyboard = Storyboard(
                episode_id=self.episode_id,
                run_id=self.run_id,
                script_version_id=UUID(script_id),
                version_no=version + 1,
                content_json=content,
            )
            db.add(storyboard)
            db.flush()
            for shot in content["shots"]:
                db.add(
                    Shot(
                        storyboard_id=storyboard.id,
                        shot_no=shot["shot_no"],
                        duration_ms=shot["duration_ms"],
                        description=shot["visual_description"],
                        content_json=shot,
                    )
                )
            return str(storyboard.id), content
