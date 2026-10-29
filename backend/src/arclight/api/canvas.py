from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from pydantic import Field
from sqlalchemy import select, update

from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import Input
from arclight.application.commands import require_episode
from arclight.application.dependencies import query_dependencies
from arclight.core.errors import DomainError
from arclight.persistence.models import (
    AssetVersion,
    CanvasLayout,
    Episode,
    Project,
    Script,
    Shot,
    Storyboard,
    VisualAsset,
)

router = APIRouter(prefix="/api/v1", tags=["canvas"])
Coordinate = Annotated[float, Field(ge=-100000, le=100000, allow_inf_nan=False)]
MAX_CANVAS_NODES = 2000
MAX_CANVAS_EDGES = 10000
DEPENDENCY_LABELS = {
    "CONTENT_DEPENDENCY": "内容依赖",
    "STYLE_DEPENDENCY": "风格依赖",
    "REFERENCE_DEPENDENCY": "参考依赖",
    "SEQUENCE_DEPENDENCY": "顺序依赖",
    "RENDER_DEPENDENCY": "渲染依赖",
}


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
    # Share the writer's project lock so versions and edges form one coherent read.
    db.execute(
        select(Project.id).where(Project.id == episode.project_id).with_for_update(read=True)
    )
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
    add_asset_graph(db, episode, nodes, edges)
    return {"nodes": nodes, "edges": edges}


def add_asset_graph(db, episode, nodes, edges):
    """Project versions are authoritative; layout never contributes business edges."""
    assets = list(
        db.scalars(
            select(VisualAsset)
            .where(VisualAsset.project_id == episode.project_id)
            .order_by(VisualAsset.created_at, VisualAsset.id)
            .limit(MAX_CANVAS_NODES + 1)
        )
    )
    versions = list(
        db.scalars(
            select(AssetVersion)
            .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
            .where(VisualAsset.project_id == episode.project_id)
            .order_by(AssetVersion.asset_id, AssetVersion.version_no)
            .limit(MAX_CANVAS_NODES + 1)
        )
    )
    dependencies = query_dependencies(db, episode.project_id, limit=MAX_CANVAS_EDGES + 1)
    if len(assets) > MAX_CANVAS_NODES or len(versions) > MAX_CANVAS_NODES:
        raise DomainError("CANVAS_TOO_LARGE", "项目版本过多，超过画布完整展示上限", 409)
    by_asset = {asset.id: asset for asset in assets}
    by_version = {
        node["resource_id"]: node for node in nodes if node["kind"] in {"script", "storyboard"}
    }
    for asset in assets:
        if asset.asset_type in {"SCRIPT", "STORYBOARD"}:
            continue
        nodes.append(
            {
                "id": "asset:" + str(asset.id),
                "kind": "asset",
                "resource_id": str(asset.id),
                "asset_id": str(asset.id),
                "asset_type": asset.asset_type,
                "title": asset.name,
                "status": asset.status,
            }
        )
    for version in versions:
        asset = by_asset[version.asset_id]
        node = by_version.get(str(version.id))
        if node is None:
            node = {
                "id": "asset-version:" + str(version.id),
                "kind": "asset_version",
                "resource_id": str(version.id),
                "title": asset.name,
                "status": version.status,
                "version_no": version.version_no,
            }
            nodes.append(node)
            by_version[str(version.id)] = node
            if asset.asset_type not in {"SCRIPT", "STORYBOARD"}:
                edges.append(
                    {
                        "id": "asset-membership:" + str(version.id),
                        "source": "asset:" + str(asset.id),
                        "target": node["id"],
                        "relation": "structure",
                    }
                )
        local_image = (
            asset.asset_type in {"CHARACTER", "SCENE", "PROP", "STYLE"}
            and version.creation_key is not None
            and version.metadata_json.get("storage_backend") in {"LOCAL", "S3"}
        )
        node.update(
            asset_id=str(asset.id),
            asset_type=asset.asset_type,
            stale=version.stale,
            current=asset.current_version_id == version.id,
            checksum=version.checksum,
            dependency_manifest_id=version.metadata_json.get("dependency_manifest_id"),
            preview_path=(
                f"/projects/{episode.project_id}/assets/{asset.id}/versions/{version.id}/image"
                if local_image
                else None
            ),
        )
    # Upgrade a real text relationship instead of drawing two overlapping edges.
    by_pair = {(edge["source"], edge["target"]): edge for edge in edges}
    for dependency in dependencies:
        source = by_version[str(dependency.source_asset_version_id)]
        target = by_version[str(dependency.target_asset_version_id)]
        pair = (source["id"], target["id"])
        edge = by_pair.pop(pair, None)
        if edge is None:
            edge = {"id": "dependency:" + str(dependency.id), "source": pair[0], "target": pair[1]}
            edges.append(edge)
        edge.update(
            relation="dependency",
            dependency_id=str(dependency.id),
            dependency_type=dependency.dependency_type,
            dependency_manifest_id=(
                str(dependency.dependency_manifest_id)
                if dependency.dependency_manifest_id
                else None
            ),
            label=DEPENDENCY_LABELS[dependency.dependency_type],
        )
    if len(nodes) > MAX_CANVAS_NODES or len(edges) > MAX_CANVAS_EDGES:
        raise DomainError("CANVAS_TOO_LARGE", "项目依赖过多，超过画布完整展示上限", 409)


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
    # Match producer lock order: project graph, then episode.
    db.execute(select(Project.id).where(Project.id == episode.project_id).with_for_update())
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
