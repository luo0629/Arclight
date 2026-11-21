"""Seed persisted pre-0056 snapshots without reopening the current write contract."""

from uuid import UUID, uuid4

from test_timelines import created_timeline

from arclight.application import timelines
from arclight.domain.timelines import TimelineSave, TimelineSnapshot
from arclight.persistence.timelines import Timeline


def seed_schema1_timeline(client, episode, create, monkeypatch):
    def legacy_snapshot(**values):
        tracks = values.pop("tracks")
        return TimelineSnapshot(
            schema_version=1,
            tracks=[
                track.model_copy(
                    update={
                        "clips": [clip.model_copy(update={"visual": None}) for clip in track.clips]
                    }
                )
                for track in tracks
            ],
            **values,
        )

    # For this insert only, freeze the old representation through the real store.
    with monkeypatch.context() as patch:
        patch.setattr(timelines, "CurrentTimelineSnapshot", legacy_snapshot)
        original = created_timeline(client, episode, create)
    assert original["snapshot"]["schema_version"] == 1
    return original


def seed_schema1_revision(client, original):
    snapshot = TimelineSnapshot.model_validate(original["snapshot"])
    assert snapshot.schema_version == 1
    creation_key = uuid4()
    # Use the real store and revision guards, not a legacy HTTP request.
    with client.app.state.session_factory.begin() as db:
        base = db.get(Timeline, UUID(original["id"]))
        assert base is not None
        value = timelines.save_timeline(
            db,
            client.app.state.settings,
            base.id,
            base.created_by,
            TimelineSave.model_construct(
                creation_key=creation_key, expected_version_no=base.version_no, snapshot=snapshot
            ),
        )
    return value.model_dump(mode="json"), {
        "creation_key": str(creation_key),
        "expected_version_no": original["version_no"],
        "snapshot": original["snapshot"],
    }
