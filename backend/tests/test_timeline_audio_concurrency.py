from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_continuity_reviews import review_case
from test_dependency_validation import registry_version

from arclight.domain.timelines import TRACK_KINDS
from arclight.persistence.models import AssetVersion, VisualAsset
from arclight.persistence.timelines import Timeline, TimelineClip, TimelineClipAudio, TimelineTrack

__all__ = ["review_case"]


@pytest.fixture
def open_audio_parent(client, episode, review_case, settings):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL row-lock concurrency requires PostgreSQL")
    with client.app.state.session_factory.begin() as db:
        project_id = UUID(episode["project_id"])
        asset = VisualAsset(project_id=project_id, asset_type="TIMELINE", name="Audio race fixture")
        db.add(asset)
        db.flush()
        version = AssetVersion(
            asset_id=asset.id,
            version_no=1,
            checksum="a" * 64,
            object_key="database:audio-race",
            created_by="service-owner",
            metadata_json={"timeline_audio_clip_count": 1},
        )
        db.add(version)
        db.flush()
        parent = Timeline(
            id=version.id,
            episode_id=UUID(episode["id"]),
            storyboard_id=UUID(review_case["target"]["storyboard_id"]),
            version_no=1,
            default_duration_ms=3000,
            duration_ms=3000,
            output_profile_json={"width": 256, "height": 256, "fps": 24},
            creation_key=uuid4(),
            request_sha256="a" * 64,
            created_by="service-owner",
        )
        db.add(parent)
        db.flush()
        tracks = {}
        for ordinal, kind in enumerate(TRACK_KINDS):
            track = TimelineTrack(timeline_id=parent.id, ordinal=ordinal, kind=kind)
            db.add(track)
            db.flush()
            tracks[kind] = track.id
        image = db.get(AssetVersion, UUID(review_case["target"]["asset_version_id"]))
        db.add(
            TimelineClip(
                timeline_id=parent.id,
                track_id=tracks["VIDEO"],
                ordinal=0,
                source_asset_version_id=image.id,
                source_sha256=image.checksum,
                shot_id=UUID(review_case["target"]["shot_id"]),
                start_ms=0,
                duration_ms=3000,
            )
        )
        _, audio = registry_version(db, project_id, kind="AUDIO")
        clips = []
        for ordinal in range(2):
            clip = TimelineClip(
                timeline_id=parent.id,
                track_id=tracks["MUSIC"],
                ordinal=ordinal,
                source_asset_version_id=audio.id,
                source_sha256=audio.checksum,
                start_ms=ordinal * 1000,
                duration_ms=1000,
            )
            db.add(clip)
            db.flush()
            clips.append(clip.id)
        db.add(
            TimelineClipAudio(
                id=clips[0], timeline_id=parent.id, settings_json={"purpose_role": "BACKGROUND"}
            )
        )
        db.flush()
        return parent.id, clips[1]


def await_parent_lock(db, worker_pid, future):
    deadline = monotonic() + 10
    while monotonic() < deadline:
        if db.scalar(text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": worker_pid}):
            assert not future.done()
            return
        if future.done():
            pytest.fail("Concurrent operation bypassed the parent timeline row lock")
        Event().wait(0.01)
    pytest.fail("Concurrent operation did not wait on the parent timeline row lock")


@pytest.mark.parametrize("first", ["seal", "insert"])
def test_postgres_audio_insert_and_timeline_seal_serialize_on_the_exact_parent(
    client, open_audio_parent, first
):
    parent_id, clip_id = open_audio_parent
    started = Event()
    worker = {}
    factory = client.app.state.session_factory

    def insert(db):
        db.add(
            TimelineClipAudio(
                id=clip_id,
                timeline_id=parent_id,
                settings_json={"purpose_role": "BACKGROUND"},
            )
        )
        db.flush()

    def seal(db):
        db.execute(text("UPDATE timelines SET sealed=true WHERE id=:id"), {"id": parent_id})

    def competing():
        with factory.begin() as db:
            worker["pid"] = db.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            (insert if first == "seal" else seal)(db)

    with factory() as db, ThreadPoolExecutor(max_workers=1) as pool:
        (seal if first == "seal" else insert)(db)
        future = pool.submit(competing)
        try:
            assert started.wait(timeout=10)
            await_parent_lock(db, worker["pid"], future)
        finally:
            db.commit()
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"):
            future.result(timeout=20)
    with factory() as db:
        assert db.get(Timeline, parent_id).sealed is (first == "seal")
        expected = 1 if first == "seal" else 2
        assert (
            db.scalar(
                select(func.count())
                .select_from(TimelineClipAudio)
                .where(TimelineClipAudio.timeline_id == parent_id)
            )
            == expected
        )
