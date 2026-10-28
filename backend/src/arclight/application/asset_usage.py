"""Read actual exact references from canonical content, never declared scope."""

from collections.abc import Generator, Iterator
from itertools import islice
from uuid import UUID

from sqlalchemy import String, cast, select
from sqlalchemy.orm import Session

from arclight.core.errors import DomainError
from arclight.persistence.models import AssetVersion, Episode, Script, Storyboard


def references(content: dict, kind: str) -> Iterator[tuple[str, dict, dict]]:
    """Only contract reference fields count; names, notes and UUID text do not."""
    if kind == "script":
        for i, item in enumerate(content.get("character_assets", [])):
            yield f"character_assets.{i}.asset", item["asset"], {}
        locations = content.get("scenes", [])
    else:
        locations = content.get("shots", [])
    for i, location in enumerate(locations):
        prefix = f"{'scenes' if kind == 'script' else 'shots'}.{i}"
        position = {
            "scene_id": location.get("scene_id"),
            "scene_no": location.get("scene_no"),
            "scene_title": location.get("title") if kind == "script" else None,
            "shot_no": location.get("shot_no") if kind == "storyboard" else None,
        }
        for field in ("scene_asset", "location_asset", "selected_visual_asset"):
            if reference := location.get(field):
                yield f"{prefix}.{field}", reference, position
        for index, reference in enumerate(location.get("props", [])):
            yield f"{prefix}.props.{index}", reference, position
        for index, character in enumerate(location.get("character_details", [])):
            for field in ("asset", "costume"):
                if reference := character.get(field):
                    yield f"{prefix}.character_details.{index}.{field}", reference, position


def actual_usage(
    db: Session,
    project_id: UUID,
    asset_id: UUID,
    version_id: UUID | None,
    episode_id: UUID | None,
    limit: int,
    offset: int,
) -> list[dict]:
    versions = {
        key: number
        for key, number in db.execute(
            select(AssetVersion.id, AssetVersion.version_no).where(
                AssetVersion.asset_id == asset_id
            )
        )
    }
    if version_id is not None and version_id not in versions:
        raise DomainError("NOT_FOUND", "资产版本不存在", 404)
    if episode_id is not None:
        episode = db.get(Episode, episode_id)
        if episode is None or episode.project_id != project_id:
            raise DomainError("NOT_FOUND", "剧集不存在", 404)
    selected = {str(key): number for key, number in versions.items() if version_id in (None, key)}

    def rows() -> Generator[dict, None, None]:
        # Stable order: scripts, then storyboards; immutable creation order within each.
        # Stream candidate snapshots in bounded batches and stop after the requested page.
        for model, kind in ((Script, "script"), (Storyboard, "storyboard")):
            statement = (
                select(model, Episode)
                .join(Episode, model.episode_id == Episode.id)
                .where(
                    Episode.project_id == project_id,
                    cast(model.content_json, String).contains(str(asset_id)),
                )
                .order_by(model.created_at, model.id)
            )
            if episode_id is not None:
                statement = statement.where(Episode.id == episode_id)
            result = db.execute(statement.execution_options(yield_per=50))
            try:
                for content, episode in result:
                    for path, reference, position in references(content.content_json, kind):
                        exact = reference.get("asset_version_id")
                        if reference.get("asset_id") != str(asset_id) or exact not in selected:
                            continue
                        yield {
                            "id": f"{content.id}:{path}",
                            "asset_version_id": exact,
                            "asset_version_no": selected[exact],
                            "episode_id": episode.id,
                            "episode_no": episode.episode_no,
                            "episode_title": episode.title,
                            "episode_status": episode.status,
                            "content_kind": kind,
                            "content_id": content.id,
                            "content_version_no": content.version_no,
                            "content_status": content.status,
                            "reference_path": path,
                            **position,
                        }
            finally:
                result.close()

    iterator = rows()
    try:
        return list(islice(iterator, offset, offset + limit))
    finally:
        iterator.close()
