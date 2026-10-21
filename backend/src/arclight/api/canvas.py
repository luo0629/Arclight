from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from pydantic import Field
from sqlalchemy import select, update

from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import Input
from arclight.application.commands import require_episode
from arclight.core.errors import DomainError
from arclight.persistence.models import CanvasLayout, Episode, Script, Shot, Storyboard

router = APIRouter(prefix="/api/v1", tags=["canvas"])
Coordinate = Annotated[float, Field(ge=-100000, le=100000, allow_inf_nan=False)]


class Position(Input):
    x: Coordinate
    y: Coordinate


class Viewport(Position):
    zoom: float = Field(ge=0.1, le=3, allow_inf_nan=False)


class LayoutInput(Input):
    expected_revision: int = Field(ge=0)
    positions: dict[str, Position] = Field(max_length=2000)
    viewport: Viewport


def graph_data(db, episode):
    nodes = [
        {
            "id": "episode:" + str(episode.id),
            "kind": "episode",
            "title": episode.title,
            "status": episode.status,
        }
    ]
    edges = []
    scripts = list(
        db.scalars(
            select(Script).where(Script.episode_id == episode.id).order_by(Script.version_no)
        )
    )
    boards = list(
        db.scalars(
            select(Storyboard)
            .where(Storyboard.episode_id == episode.id)
            .order_by(Storyboard.version_no)
        )
    )
    for script in scripts:
        node_id = "script:" + str(script.id)
        nodes.append(
            {
                "id": node_id,
                "kind": "script",
                "resource_id": str(script.id),
                "title": script.content_json.get("title", "剧本"),
                "version_no": script.version_no,
                "status": script.status,
            }
        )
        edges.append({"id": "input:" + str(script.id), "source": nodes[0]["id"], "target": node_id})
    for board in boards:
        node_id = "storyboard:" + str(board.id)
        nodes.append(
            {
                "id": node_id,
                "kind": "storyboard",
                "resource_id": str(board.id),
                "title": "分镜",
                "version_no": board.version_no,
                "status": board.status,
            }
        )
        edges.append(
            {
                "id": "script-input:" + str(board.id),
                "source": "script:" + str(board.script_version_id),
                "target": node_id,
            }
        )
    board_ids = [board.id for board in boards]
    for shot in db.scalars(
        select(Shot)
        .where(Shot.storyboard_id.in_(board_ids))
        .order_by(Shot.storyboard_id, Shot.shot_no)
    ):
        node_id = "shot:" + str(shot.id)
        nodes.append(
            {
                "id": node_id,
                "kind": "shot",
                "resource_id": str(shot.id),
                "title": f"镜头 {shot.shot_no:02d}",
                "status": shot.status,
                "description": shot.description,
                "duration_ms": shot.duration_ms,
            }
        )
        edges.append(
            {
                "id": "shot-input:" + str(shot.id),
                "source": "storyboard:" + str(shot.storyboard_id),
                "target": node_id,
            }
        )
    return {"nodes": nodes, "edges": edges}


@router.get("/episodes/{episode_id}/canvas")
def get_canvas(episode_id: UUID, db: Database, actor: Actor) -> dict:
    episode = require_episode(db, episode_id, actor)
    layout = db.scalar(select(CanvasLayout).where(CanvasLayout.episode_id == episode_id))
    return {
        **graph_data(db, episode),
        "layout": {
            "revision": layout.revision if layout else 0,
            "positions": layout.positions if layout else {},
            "viewport": layout.viewport if layout else {"x": 0, "y": 0, "zoom": 1},
        },
    }


@router.put("/episodes/{episode_id}/canvas/layout")
def put_layout(episode_id: UUID, payload: LayoutInput, db: Database, actor: Actor) -> dict:
    episode = require_episode(db, episode_id, actor, write=True)
    db.execute(select(Episode.id).where(Episode.id == episode_id).with_for_update())
    valid_ids = {node["id"] for node in graph_data(db, episode)["nodes"]}
    if not set(payload.positions) <= valid_ids:
        raise DomainError("INVALID_CANVAS_NODE", "布局包含不属于当前剧集的节点", 422)
    layout = db.scalar(select(CanvasLayout).where(CanvasLayout.episode_id == episode_id))
    values = {
        "positions": {key: pos.model_dump() for key, pos in payload.positions.items()},
        "viewport": payload.viewport.model_dump(),
    }
    revision: int | None
    if layout is None:
        if payload.expected_revision != 0:
            raise DomainError("LAYOUT_CONFLICT", "画布已变化，请刷新后重试", 409)
        layout = CanvasLayout(episode_id=episode_id, revision=1, **values)
        db.add(layout)
        db.flush()
        revision = 1
    else:
        revision = db.scalar(
            update(CanvasLayout)
            .where(CanvasLayout.id == layout.id, CanvasLayout.revision == payload.expected_revision)
            .values(revision=CanvasLayout.revision + 1, **values)
            .returning(CanvasLayout.revision)
            .execution_options(synchronize_session=False)
        )
        if revision is None:
            raise DomainError("LAYOUT_CONFLICT", "其他成员已保存布局，请刷新后重试", 409)
    return {"revision": revision, **values}
