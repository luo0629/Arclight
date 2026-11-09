from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_continuity_reviews import review_case
from test_timeline_saves import save_counts, save_url, saved_case, saved_timeline
from test_timelines import created_timeline, timeline_case

from arclight.application import timeline_store
from arclight.application.timelines import save_timeline
from arclight.domain.timeline_visuals import ClipVisual
from arclight.domain.timelines import TimelineSave, TimelineSnapshot
from arclight.persistence.models import AssetDependency, AssetVersion, VisualAsset
from arclight.persistence.timelines import Timeline, TimelineClipVisual

__all__ = ["review_case", "saved_case", "timeline_case", "visual_case"]


def configured_body(body):
    value = deepcopy(body)
    snapshot = value["snapshot"]
    snapshot["schema_version"] = 2
    track = next(track for track in snapshot["tracks"] if track["kind"] == "VIDEO")
    source = track["clips"]
    clips = []
    cursor = 0
    for ordinal, (index, duration) in enumerate(
        zip([4, 2, 0, 3, 1], [1500, 2100, 1800, 2400, 1300], strict=True)
    ):
        clip = deepcopy(source[index])
        clip.update(ordinal=ordinal, start_ms=cursor, duration_ms=duration, visual={})
        if ordinal:
            clip["visual"]["transition"] = {"kind": "FADE", "duration_ms": 301}
        clips.append(clip)
        cursor += duration
    clips[0]["visual"].update(
        crop={"x": 0, "y": 1000, "width": 8000, "height": 8000},
        motion={
            "kind": "LINEAR",
            "end_crop": {"x": 2000, "y": 1000, "width": 8000, "height": 8000},
        },
    )
    clips[1]["visual"].update(
        crop={"x": 1000, "y": 1000, "width": 8000, "height": 8000},
        motion={
            "kind": "LINEAR",
            "end_crop": {"x": 1800, "y": 1800, "width": 6400, "height": 6400},
        },
    )
    track["clips"] = clips
    snapshot["duration_ms"] = cursor
    return value


@pytest.fixture
def visual_case(saved_case):
    original, body, _ = saved_case
    return original, configured_body(body)


def visual_counts(client):
    values = save_counts(client)
    with client.app.state.session_factory() as db:
        return (*values, db.scalar(select(func.count(TimelineClipVisual.id))))


def test_five_shots_reorder_retime_and_freeze_exact_crop_motion_and_transitions(
    client, visual_case
):
    original, body = visual_case
    before = deepcopy(original)
    value = saved_timeline(client, original, body)
    assert value["snapshot"]["schema_version"] == 2
    assert value["snapshot"]["duration_ms"] == 9100
    assert value["version_no"] == 2 and value["status"] == "DRAFT"
    assert value["based_on_timeline_id"] == original["id"]
    clips = value["snapshot"]["tracks"][0]["clips"]
    old = original["snapshot"]["tracks"][0]["clips"]
    assert [c["shot_id"] for c in clips] == [old[index]["shot_id"] for index in [4, 2, 0, 3, 1]]
    assert [c["duration_ms"] for c in clips] == [1500, 2100, 1800, 2400, 1300]
    assert [c["start_ms"] for c in clips] == [0, 1500, 3600, 5400, 7800]
    expected = [
        ClipVisual.model_validate(c["visual"]).model_dump(mode="json")
        for c in body["snapshot"]["tracks"][0]["clips"]
    ]
    assert [c["visual"] for c in clips] == expected
    assert value["content_sha256"] != original["content_sha256"]
    assert client.get(save_url(original)).json() == before
    assert client.get(save_url(value)).json() == value
    with client.app.state.session_factory() as db:
        rows = list(db.scalars(select(TimelineClipVisual)))
        assert len(rows) == 5
        assert {row.id for row in rows} == {UUID(c["id"]) for c in clips}
        assert all(row.timeline_id == UUID(value["id"]) for row in rows)
        assert not {row.id for row in rows} & {UUID(c["id"]) for c in old}
        for row in rows:
            assert row.settings_json == next(c["visual"] for c in clips if c["id"] == str(row.id))

        def inputs(identity):
            return set(
                db.scalars(
                    select(AssetDependency.source_asset_version_id).where(
                        AssetDependency.target_asset_version_id == UUID(identity)
                    )
                )
            )

        assert inputs(original["id"]) == inputs(value["id"])
        assert len(inputs(value["id"])) == 6
        for clip in old:
            version = db.get(AssetVersion, UUID(clip["source_asset_version_id"]))
            assert version.checksum == clip["source_sha256"] and not version.stale
            assert db.get(VisualAsset, version.asset_id).current_version_id == version.id
    windows = TimelineSnapshot.model_validate(value["snapshot"]).video_fades()
    assert windows[UUID(clips[0]["id"])].outgoing_ms == 150
    assert windows[UUID(clips[1]["id"])].incoming_ms == 151


def test_visual_defaults_and_reordered_payload_replay_once_without_changing_history(
    client, visual_case
):
    original, body = visual_case
    value = saved_timeline(client, original, body)
    counts = visual_counts(client)
    retry = deepcopy(body)
    for clip in retry["snapshot"]["tracks"][0]["clips"]:
        clip["visual"] = ClipVisual.model_validate(clip["visual"]).model_dump(mode="json")
    retry["snapshot"]["tracks"][0]["clips"].reverse()
    retry["snapshot"]["tracks"].reverse()
    assert saved_timeline(client, original, retry) == value
    assert visual_counts(client) == counts
    assert client.get(save_url(original)).json() == original


def test_visual_only_edit_keeps_previous_versions_and_stale_cas_has_no_effects(client, visual_case):
    original, body = visual_case
    second = saved_timeline(client, original, body)
    counts = visual_counts(client)
    stale = deepcopy(body)
    stale["creation_key"] = str(uuid4())
    response = client.patch(save_url(original), json=stale)
    assert response.status_code == 409 and response.json()["error"]["code"] == "VERSION_CONFLICT"
    assert visual_counts(client) == counts
    next_body = {
        "creation_key": str(uuid4()),
        "expected_version_no": 2,
        "snapshot": deepcopy(second["snapshot"]),
    }
    next_body["snapshot"]["tracks"][0]["clips"][-1]["visual"]["crop"] = {"x": 1000, "width": 9000}
    third = saved_timeline(client, second, next_body)
    assert third["version_no"] == 3 and third["based_on_timeline_id"] == second["id"]
    assert third["content_sha256"] != second["content_sha256"]
    assert client.get(save_url(second)).json() == second
    assert client.get(save_url(original)).json() == original
    assert saved_timeline(client, original, body) == second
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(TimelineClipVisual.id))) == 10


def test_invalid_visual_parameters_and_changed_source_fail_atomically(client, visual_case):
    original, body = visual_case
    before = visual_counts(client)
    mutations = []
    missing = deepcopy(body)
    del missing["snapshot"]["tracks"][0]["clips"][2]["visual"]
    mutations.append((missing, 422))
    outside = deepcopy(body)
    outside["snapshot"]["tracks"][0]["clips"][-1]["visual"]["crop"] = {"x": 9000, "width": 2000}
    mutations.append((outside, 422))
    untrusted = deepcopy(body)
    untrusted["snapshot"]["tracks"][0]["clips"][-1]["visual"]["filter"] = (
        "movie=https://untrusted.example/input"
    )
    mutations.append((untrusted, 422))
    first = deepcopy(body)
    first["snapshot"]["tracks"][0]["clips"][0]["visual"]["transition"] = {
        "kind": "FADE",
        "duration_ms": 301,
    }
    mutations.append((first, 422))
    source = deepcopy(body)
    source["snapshot"]["tracks"][0]["clips"][-1]["source_sha256"] = "0" * 64
    mutations.append((source, 409))
    for invalid, status in mutations:
        response = client.patch(save_url(original), json=invalid)
        assert response.status_code == status, response.text
        assert visual_counts(client) == before
    value = saved_timeline(client, original, body)
    assert value["version_no"] == 2


def test_visual_history_cannot_be_modified_deleted_replaced_or_added_after_sealing(
    client, visual_case, settings
):
    original, body = visual_case
    value = saved_timeline(client, original, body)
    for statement in (
        update(TimelineClipVisual).values(settings_json={}),
        delete(TimelineClipVisual),
    ):
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(statement)
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.add(
            TimelineClipVisual(
                id=UUID(original["snapshot"]["tracks"][0]["clips"][0]["id"]),
                timeline_id=UUID(original["id"]),
                settings_json={},
            )
        )
        db.flush()
    if settings.database_url.startswith("sqlite"):
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(
                text(
                    "INSERT OR REPLACE INTO timeline_clip_visuals "
                    "SELECT * FROM timeline_clip_visuals"
                )
            )
    assert client.get(save_url(value)).json() == value
    assert client.get(save_url(original)).json() == original


@pytest.mark.parametrize("omit_all", [False, True])
def test_partial_visual_write_cannot_seal_and_rolls_back_the_whole_save(
    client, visual_case, monkeypatch, settings, omit_all
):
    original, body = visual_case
    before = visual_counts(client)
    original_add = Session.add
    omitted = False

    def omit_one_visual(db, instance, *args, **kwargs):
        nonlocal omitted
        if isinstance(instance, TimelineClipVisual) and (omit_all or not omitted):
            omitted = True
            return None
        return original_add(db, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "add", omit_one_visual)
    with (
        pytest.raises(DBAPIError, match="incomplete timeline visuals"),
        client.app.state.session_factory.begin() as db,
    ):
        actor = db.get(Timeline, UUID(original["id"])).created_by
        save_timeline(db, settings, UUID(original["id"]), actor, TimelineSave.model_validate(body))
    assert omitted
    assert visual_counts(client) == before
    assert client.get(save_url(original)).json() == original


def test_visual_save_uses_existing_membership_roles_and_rechecks_revocation(
    client, episode, visual_case
):
    from test_collaboration import register

    from arclight.persistence.models import ProjectMember

    original, body = visual_case
    owner = client.headers["Authorization"]
    member = register(client, "timeline-visual-editor@example.com")
    assert client.patch(save_url(original), json=body).status_code == 404
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(episode["project_id"]),
            user_id=UUID(member["user"]["id"]),
            role="viewer",
        )
        db.add(membership)
        db.flush()
        identity = membership.id
    before = visual_counts(client)
    assert client.patch(save_url(original), json=body).status_code == 403
    assert visual_counts(client) == before
    with client.app.state.session_factory.begin() as db:
        db.get(ProjectMember, identity).role = "editor"
    value = saved_timeline(client, original, body)
    assert value["snapshot"]["schema_version"] == 2
    with client.app.state.session_factory.begin() as db:
        db.delete(db.get(ProjectMember, identity))
    assert client.patch(save_url(original), json=body).status_code == 404
    client.headers["Authorization"] = owner
    assert client.get(save_url(value)).json() == value


def test_legacy_version_without_schema_marker_reads_and_upgrades_without_hash_drift(
    client, episode, timeline_case, monkeypatch
):
    create, _ = timeline_case
    version_model = timeline_store.AssetVersion

    def legacy_version(**kwargs):
        kwargs["metadata_json"].pop("timeline_schema_version", None)
        return version_model(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(timeline_store, "AssetVersion", legacy_version)
        original = created_timeline(client, episode, create)
    assert original["snapshot"]["schema_version"] == 1
    with client.app.state.session_factory() as db:
        assert (
            "timeline_schema_version"
            not in db.get(AssetVersion, UUID(original["id"])).metadata_json
        )
    body = configured_body(
        {
            "creation_key": str(uuid4()),
            "expected_version_no": 1,
            "snapshot": deepcopy(original["snapshot"]),
        }
    )
    value = saved_timeline(client, original, body)
    with client.app.state.session_factory() as db:
        assert db.get(AssetVersion, UUID(value["id"])).metadata_json["timeline_schema_version"] == 2
    assert client.get(save_url(original)).json() == original
    assert client.get(save_url(value)).json() == value
