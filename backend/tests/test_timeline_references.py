"""Real database contracts for future audio/subtitle timeline writers, without audio generation."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from test_collaboration import create_project
from test_continuity_reviews import review_case  # noqa: F401
from test_dependency_validation import registry_version

from arclight.domain.timelines import TRACK_KINDS
from arclight.persistence.models import AssetVersion, Script, Storyboard
from arclight.persistence.timelines import Timeline, TimelineClip, TimelineTrack

__all__ = ["review_case"]


@pytest.fixture
def reference_case(client, episode, review_case):
    with client.app.state.session_factory.begin() as db:
        board = db.get(Storyboard, UUID(review_case["target"]["storyboard_id"]))
        _, version = registry_version(db, UUID(episode["project_id"]), kind="TIMELINE")
        parent = Timeline(
            id=version.id,
            episode_id=UUID(episode["id"]),
            storyboard_id=board.id,
            version_no=1,
            default_duration_ms=3000,
            duration_ms=3000,
            output_profile_json={"width": 1080, "height": 1920, "fps": 30},
            creation_key=uuid4(),
            request_sha256="a" * 64,
            created_by="service-owner",
        )
        db.add(parent)
        db.flush()
        tracks = {}
        for index, kind in enumerate(TRACK_KINDS):
            row = TimelineTrack(timeline_id=parent.id, kind=kind, ordinal=index)
            db.add(row)
            db.flush()
            tracks[kind] = row.id
        _, audio = registry_version(db, UUID(episode["project_id"]), kind="AUDIO")
        _, subtitle = registry_version(db, UUID(episode["project_id"]), kind="SUBTITLE")
        image = db.get(AssetVersion, UUID(review_case["target"]["asset_version_id"]))
        script = db.get(Script, board.script_version_id)
        return {
            "timeline_id": parent.id,
            "tracks": tracks,
            "script_version_id": script.id,
            "line_id": script.content_json["scenes"][0]["dialogue"][0]["line_id"],
            "audio": audio.id,
            "subtitle": subtitle.id,
            "image": image.id,
            "shot_id": UUID(review_case["target"]["shot_id"]),
            "checksum": audio.checksum,
            "image_checksum": image.checksum,
        }


def reference_clip(case, kind):
    return {
        "timeline_id": case["timeline_id"],
        "track_id": case["tracks"][kind],
        "ordinal": 0,
        "start_ms": 0,
        "duration_ms": 3000,
        "source_asset_version_id": case["image"]
        if kind == "VIDEO"
        else case["subtitle"]
        if kind == "SUBTITLE"
        else case["audio"],
        "source_sha256": case["image_checksum"] if kind == "VIDEO" else case["checksum"],
        **({"shot_id": case["shot_id"]} if kind == "VIDEO" else {}),
        **(
            {"script_version_id": case["script_version_id"], "line_id": case["line_id"]}
            if kind in {"VOICE", "SUBTITLE"}
            else {}
        ),
        **({"audio_version_id": case["audio"]} if kind == "SUBTITLE" else {}),
    }


def test_database_preserves_exact_audio_subtitle_script_and_line_references(client, reference_case):
    case = reference_case
    with client.app.state.session_factory.begin() as db:
        for kind in TRACK_KINDS:
            db.add(TimelineClip(**reference_clip(case, kind)))
        db.flush()
        db.get(Timeline, case["timeline_id"]).sealed = True
        db.flush()
    with client.app.state.session_factory() as db:
        rows = list(
            db.scalars(select(TimelineClip).where(TimelineClip.timeline_id == case["timeline_id"]))
        )
        assert len(rows) == 5
        subtitle = next(row for row in rows if row.audio_version_id)
        assert subtitle.source_asset_version_id == case["subtitle"]
        assert subtitle.audio_version_id == case["audio"]
        assert subtitle.script_version_id == case["script_version_id"]
        assert subtitle.line_id == case["line_id"]


@pytest.mark.parametrize(
    "problem",
    [
        "hash",
        "source_type",
        "track_scope",
        "shot_scope",
        "script_scope",
        "unpaired_line",
        "audio_type",
        "audio_scope",
        "media_scope",
        "time_range",
    ],
)
def test_database_rejects_wrong_exact_reference_scope_type_hash_and_timing(
    client, episode, reference_case, problem
):
    case = reference_case
    kind = "SUBTITLE" if problem.startswith("audio") else "VOICE"
    value = reference_clip(case, kind)
    if problem == "hash":
        value["source_sha256"] = "b" * 64
    elif problem == "source_type":
        value["source_asset_version_id"] = case["image"]
        value["source_sha256"] = case["image_checksum"]
    elif problem == "track_scope":
        value["track_id"] = uuid4()
    elif problem == "shot_scope":
        value = reference_clip(case, "VIDEO")
        value["shot_id"] = uuid4()
    elif problem == "script_scope":
        value["script_version_id"] = uuid4()
    elif problem == "unpaired_line":
        value["script_version_id"] = None
    elif problem == "audio_type":
        value["audio_version_id"] = case["image"]
    elif problem in {"audio_scope", "media_scope"}:
        foreign = create_project(client)
        with client.app.state.session_factory.begin() as db:
            _, audio = registry_version(db, UUID(foreign["id"]), kind="AUDIO")
            value["audio_version_id" if problem == "audio_scope" else "source_asset_version_id"] = (
                audio.id
            )
    else:
        value["start_ms"] = 100
    with pytest.raises(DBAPIError), client.app.state.session_factory.begin() as db:
        db.add(TimelineClip(**value))
        db.flush()


def test_database_cannot_publish_an_incomplete_timeline(client, reference_case):
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.get(Timeline, reference_case["timeline_id"]).sealed = True
        db.flush()
