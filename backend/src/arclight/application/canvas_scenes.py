"""Project one exact script's scenes and their production facts onto the canvas."""

from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.continuity_neighbors import selected_neighbor
from arclight.application.short_drama import progress_for
from arclight.application.short_drama_budget import MEDIA_SCOPE
from arclight.application.short_drama_visuals import SHOT_SELECTION, production_first_frame
from arclight.domain.enums import RunStatus
from arclight.domain.versions import digest
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    Episode,
    GenerationAttempt,
    GenerationCandidate,
    GenerationJob,
    Script,
    Shot,
    Storyboard,
    WorkflowRun,
)
from arclight.persistence.timelines import Timeline, TimelineClip, TimelineTrack
from arclight.persistence.versions import VersionBinding


def _script_and_run(
    db: Session, episode: Episode, scripts: list[Script]
) -> tuple[Script | None, WorkflowRun | None]:
    run = db.scalar(
        select(WorkflowRun)
        .where(
            WorkflowRun.episode_id == episode.id,
            WorkflowRun.workflow_type == "producer",
            WorkflowRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
            WorkflowRun.input_json["short_drama"]["script_version_id"].as_string().is_not(None),
        )
        .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        .limit(1)
    )
    if run is not None:
        exact = run.input_json["short_drama"]["script_version_id"]
        return next((script for script in scripts if str(script.id) == exact), None), run
    return next(
        (script for script in reversed(scripts) if script.status not in {"REJECTED", "ARCHIVED"}),
        None,
    ), None


def _storyboard(
    db: Session, script: Script, boards: list[Storyboard], run: WorkflowRun | None
) -> Storyboard | None:
    exact_boards = [
        board
        for board in reversed(boards)
        if board.script_version_id == script.id
        and board.status not in {"REJECTED", "ARCHIVED", "STALE"}
    ]
    if run is None:
        return next(iter(exact_boards), None)
    recorded = progress_for(db, run).get("storyboard_id")
    if recorded is not None:
        return next((board for board in exact_boards if str(board.id) == recorded), None)
    child_ids = set(
        db.scalars(
            select(WorkflowRun.id).where(
                WorkflowRun.episode_id == script.episode_id,
                WorkflowRun.input_json["producer_parent_run_id"].as_string() == str(run.id),
            )
        )
    )
    return next((board for board in exact_boards if board.run_id in child_ids), None)


def _timeline_media(db: Session, board: Storyboard) -> dict[str, tuple[str, str]]:
    timeline = db.scalar(
        select(Timeline.id)
        .join(AssetVersion, AssetVersion.id == Timeline.id)
        .where(
            Timeline.storyboard_id == board.id,
            Timeline.episode_id == board.episode_id,
            Timeline.sealed.is_(True),
            AssetVersion.stale.is_(False),
            AssetVersion.status.not_in(["ARCHIVED", "REJECTED", "STALE"]),
        )
        .order_by(Timeline.version_no.desc())
        .limit(1)
    )
    if timeline is None:
        return {}
    selections: dict[str, tuple[str, str]] = {}
    for shot_id, version_id, checksum in db.execute(
        select(
            TimelineClip.shot_id, TimelineClip.source_asset_version_id, TimelineClip.source_sha256
        )
        .join(
            TimelineTrack,
            (TimelineTrack.id == TimelineClip.track_id)
            & (TimelineTrack.timeline_id == TimelineClip.timeline_id),
        )
        .where(
            TimelineClip.timeline_id == timeline,
            TimelineClip.shot_id.is_not(None),
            TimelineTrack.kind == "VIDEO",
        )
        .order_by(TimelineClip.start_ms, TimelineClip.ordinal)
    ):
        # The storyboard card represents the first appearance when a cut repeats a shot.
        selections.setdefault(str(shot_id), (str(version_id), checksum))
    return selections


def _production_selections(
    db: Session, run: WorkflowRun, project_id: UUID, shots: list[Shot]
) -> dict[tuple[str, str], dict]:
    identities = {
        uuid5(run.id, f"visual:{shot.id}:{task}"): (shot, task)
        for shot in shots
        for task in ("IMAGE", "VIDEO")
    }
    authorization = digest(run.input_json["short_drama"])
    records = {}
    for receipt in db.scalars(
        select(VersionBinding).where(
            VersionBinding.project_id == project_id,
            VersionBinding.resource_type == SHOT_SELECTION,
            VersionBinding.resource_id.in_(identities),
        )
    ):
        shot, task = identities[receipt.resource_id]
        record = receipt.snapshot_json
        if (
            record["run_id"] == str(run.id)
            and record["storyboard_id"] == str(shot.storyboard_id)
            and record["selection"]["shot_id"] == str(shot.id)
            and record["authorization_sha256"] == authorization
        ):
            records[str(shot.id), task] = record
    return records


def _prepared_cover(
    db: Session,
    run: WorkflowRun,
    project_id: UUID,
    shot: Shot,
    versions: dict[str, dict],
    record: dict | None,
) -> str | None:
    if record is None or record.get("review_id") is not None:
        return None
    selection = record["selection"]
    target = {"storyboard_id": str(shot.storyboard_id), "shot_id": str(shot.id)}
    cover = versions.get(selection["asset_version_id"])
    if cover is None or cover.get("preview_kind") != "IMAGE":
        return None
    version = db.get(AssetVersion, UUID(selection["asset_version_id"]))
    if version is not None and production_first_frame(db, project_id, version, run.id, target):
        return cover["id"]
    return None


def _shot_media(
    db: Session,
    episode: Episode,
    board: Storyboard,
    shots: list[Shot],
    nodes: list[dict],
    run: WorkflowRun | None,
) -> dict[str, dict]:
    versions = {node["resource_id"]: node for node in nodes if node["kind"] == "asset_version"}
    selected = _timeline_media(db, board) if run is None else {}
    production_selections = (
        _production_selections(db, run, episode.project_id, shots) if run is not None else {}
    )
    motion: dict[str, list[tuple[dict, dict | None]]] = {}
    jobs: dict[str, tuple[str, str | None]] = {}
    binding = DependencyManifest.snapshot_json["context"]["continuity_review"]
    query = (
        select(
            GenerationJob,
            DependencyManifest,
            GenerationCandidate,
            GenerationAttempt.error_code,
        )
        .join(DependencyManifest, DependencyManifest.id == GenerationJob.input_manifest_id)
        .outerjoin(
            GenerationAttempt,
            (GenerationAttempt.job_id == GenerationJob.id)
            & (GenerationAttempt.attempt_no == GenerationJob.attempt_count),
        )
        .outerjoin(GenerationCandidate, GenerationCandidate.job_id == GenerationJob.id)
        .where(
            GenerationJob.project_id == episode.project_id,
            GenerationJob.episode_id == episode.id,
            GenerationJob.target_type.in_(["SHOT_IMAGE", "VIDEO"]),
            binding["storyboard_id"].as_string() == str(board.id),
            binding["shot_id"].as_string().in_([str(shot.id) for shot in shots]),
        )
        .order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc())
    )
    if run is not None:
        query = query.join(
            VersionBinding,
            (VersionBinding.project_id == GenerationJob.project_id)
            & (VersionBinding.resource_type == MEDIA_SCOPE)
            & (VersionBinding.resource_id == GenerationJob.id)
            & (VersionBinding.snapshot_json["run_id"].as_string() == str(run.id)),
        )
    for job, manifest, candidate, error in db.execute(query):
        context = manifest.snapshot_json["context"]
        shot_id = context["continuity_review"]["shot_id"]
        jobs.setdefault(shot_id, (job.status, error))
        if candidate is None:
            continue
        version_id = str(candidate.asset_version_id)
        version = versions.get(version_id)
        if not version or version.get("stale"):
            continue
        exact = version_id, version["checksum"]
        if run is not None:
            record = production_selections.get((shot_id, job.task_type))
            if (
                record is None
                or record.get("review_id") is None
                or record["job_id"] != str(job.id)
                or record["candidate_id"] != str(candidate.id)
                or record["selection"]["asset_version_id"] != version_id
                or version["status"] not in {"APPROVED", "LOCKED"}
            ):
                continue
            selected.setdefault(shot_id, exact)
        if version.get("preview_kind") != "VIDEO":
            continue
        if exact != selected.get(shot_id) and not (
            version.get("current") and version["status"] in {"APPROVED", "LOCKED"}
        ):
            continue
        first_frame = next(
            (ref for ref in context.get("video_images", []) if ref.get("role") == "first_frame"),
            None,
        )
        cover = versions.get(first_frame["asset_version_id"]) if first_frame else None
        if (
            first_frame
            and cover
            and (
                cover.get("preview_kind") != "IMAGE" or cover["checksum"] != first_frame["checksum"]
            )
        ):
            cover = None
        motion.setdefault(shot_id, []).append((version, cover))

    result: dict[str, dict] = {}
    for shot in shots:
        shot_id = str(shot.id)
        clips = motion.get(shot_id, [])
        chosen = selected.get(shot_id)
        if chosen is not None:
            clips = [
                clip for clip in clips if (clip[0]["resource_id"], clip[0]["checksum"]) == chosen
            ]
        status, failure = jobs.get(shot_id, ("PENDING", None))
        media: dict = {"status": status, "error_code": failure}
        if len(clips) == 1:
            version, cover = clips[0]
            media.update(
                status="READY",
                error_code=None,
                media_node_id=version["id"],
                cover_node_id=cover["id"] if cover else None,
            )
        else:
            still = None
            if run is None and chosen is None:
                still = shot.content_json.get("selected_visual_asset") or selected_neighbor(
                    db, episode.project_id, shot.id
                )
            requires_motion = (shot.content_json.get("performance") or {}).get(
                "motion_requirement"
            ) == "dynamic_required"
            image_id, checksum = chosen or ((still or {}).get("asset_version_id", ""), None)
            image = versions.get(image_id)
            if (
                image is not None
                and image.get("preview_kind") == "IMAGE"
                and image["status"] in {"APPROVED", "LOCKED"}
                and not image.get("stale")
                and (checksum is None or image["checksum"] == checksum)
            ):
                if requires_motion:
                    media.update(cover_node_id=image["id"])
                else:
                    media.update(status="READY", error_code=None, media_node_id=image["id"])
            if run is not None and chosen is None and media.get("media_node_id") is None:
                prepared = _prepared_cover(
                    db,
                    run,
                    episode.project_id,
                    shot,
                    versions,
                    production_selections.get((shot_id, "IMAGE")),
                )
                if prepared is not None:
                    media["cover_node_id"] = prepared
        result[shot_id] = media
    return result


def add_scene_graph(
    db: Session,
    episode: Episode,
    scripts: list[Script],
    boards: list[Storyboard],
    shots: list[Shot],
    nodes: list[dict],
    edges: list[dict],
) -> None:
    script, run = _script_and_run(db, episode, scripts)
    if script is None:
        return
    board = _storyboard(db, script, boards, run)
    board_shots = [shot for shot in shots if board is not None and shot.storyboard_id == board.id]
    media = (
        _shot_media(db, episode, board, board_shots, nodes, run) if board and board_shots else {}
    )
    scenes = sorted(script.content_json.get("scenes", []), key=lambda row: row["scene_no"])
    scene_ids = {scene["scene_no"]: scene.get("scene_id") for scene in scenes}
    by_scene: dict[str, list[dict]] = {}
    for shot in board_shots:
        scene_id = scene_ids.get(shot.content_json.get("scene_no"))
        if scene_id and shot.content_json.get("scene_id") in {scene_id, None}:
            by_scene.setdefault(scene_id, []).append(
                {
                    "id": f"shot:{shot.id}",
                    "shot_no": shot.shot_no,
                    "description": shot.description,
                    **media[str(shot.id)],
                }
            )
    previous = f"script:{script.id}"
    for scene in scenes:
        scene_id = scene.get("scene_id")
        if not scene_id:
            continue
        node_id = f"scene:{scene_id}"
        nodes.append(
            {
                "id": node_id,
                "kind": "scene",
                "title": scene["title"],
                "status": script.status,
                "description": (
                    (scene.get("dramatic_beat") or {}).get("conflict") or scene["action"]
                )[:300],
                "script_version_id": str(script.id),
                "scene_id": scene_id,
                "scene_no": scene["scene_no"],
                "shots": by_scene.get(scene_id, []),
            }
        )
        edges.append({"id": f"scene-order:{scene_id}", "source": previous, "target": node_id})
        previous = node_id
