"""RENDER-01: real selection-to-timeline API, exact history and existing DAG integration."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_asset_selection import choose, current
from test_collaboration import register
from test_concurrency import race, require_postgres
from test_continuity_reviews import review_case, saved  # noqa: F401
from test_continuity_selection import admission_fixture, approve, retarget, target_rows
from test_dependency_invalidation import apply
from test_generation_candidates import candidates, generate, ingest

from arclight.application.asset_objects import object_path
from arclight.application.continuity_reviews import shot_sha256
from arclight.application.production_state import production_facts
from arclight.domain.versions import digest
from arclight.persistence.models import (
    AssetDependency,
    AssetVersion,
    AuditEvent,
    DependencyManifest,
    Episode,
    Project,
    ProjectMember,
    Shot,
    VisualAsset,
)
from arclight.persistence.timelines import Timeline, TimelineClip, TimelineTrack

__all__ = ["review_case"]


def timeline_url(episode):
    return f"/api/v1/episodes/{episode['id']}/timelines"


@pytest.fixture
def timeline_case(client, episode, review_case):
    """Five real stored candidates, real review validation/approval/selection; admission fixture."""
    with client.app.state.session_factory() as db:
        shots = list(
            db.scalars(
                select(Shot)
                .where(Shot.storyboard_id == UUID(review_case["target"]["storyboard_id"]))
                .order_by(Shot.shot_no)
            )
        )
        identities = [(str(shot.id), shot_sha256(shot)) for shot in shots]
    assert len(identities) == 5
    selections, reviews = [], []
    for index, (shot_id, checksum) in enumerate(identities):
        value = deepcopy(review_case)
        if index:
            job, attempt = generate(client, episode["project_id"])
            assert ingest(client, attempt) == 1
            candidate = candidates(client, episode["project_id"], job)[0]
            value = retarget(client, value, candidate)
        value["target"]["shot_id"] = shot_id
        value["references"][1].update(resource_id=shot_id, content_sha256=checksum)
        record, created = saved(client, value)
        assert created
        admission_fixture(client, record)
        approved = approve(client, value)
        asset, _ = target_rows(client, value)
        selected = choose(client, current(client, asset), approved)
        assert selected.status_code == 200, selected.text
        selections.append({"shot_id": shot_id, "asset_version_id": approved["id"]})
        reviews.append(value)
    return {
        "creation_key": str(uuid4()),
        "storyboard_id": review_case["target"]["storyboard_id"],
        "selected_shots": selections,
    }, reviews


def created_timeline(client, episode, body):
    response = client.post(timeline_url(episode), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def timeline_counts(client):
    with client.app.state.session_factory() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (Timeline, TimelineTrack, TimelineClip, AssetVersion, DependencyManifest)
        )


def test_five_selected_shots_create_continuous_exact_frozen_default_timeline(
    client, episode, timeline_case
):
    body, reviews = timeline_case
    submitted = deepcopy(body)
    submitted["selected_shots"].reverse()
    value = created_timeline(client, episode, submitted)
    assert value["status"] == "DRAFT" and value["stale"] is False
    assert value["version_no"] == 1
    snapshot = value["snapshot"]
    assert snapshot["duration_ms"] == 15000
    assert snapshot["default_duration_ms"] == 3000
    assert snapshot["output_profile"] == {"width": 1080, "height": 1920, "fps": 30}
    assert [track["kind"] for track in snapshot["tracks"]] == [
        "VIDEO",
        "VOICE",
        "SFX",
        "MUSIC",
        "SUBTITLE",
    ]
    assert all(not track["clips"] for track in snapshot["tracks"][1:])
    for index, clip in enumerate(snapshot["tracks"][0]["clips"]):
        source = body["selected_shots"][index]
        assert clip["shot_id"] == source["shot_id"]
        assert clip["source_asset_version_id"] == source["asset_version_id"]
        assert clip["start_ms"] == index * 3000 and clip["duration_ms"] == 3000
        assert clip["review_id"] == reviews[index]["result_id"]
        with client.app.state.session_factory() as db:
            version = db.get(AssetVersion, UUID(source["asset_version_id"]))
            assert clip["source_sha256"] == version.checksum
            assert db.get(VisualAsset, version.asset_id).current_version_id == version.id
    assert value["content_sha256"] == digest(snapshot)
    assert client.get(f"/api/v1/timelines/{value['id']}").json() == value
    assert client.get(timeline_url(episode)).json() == [value]
    with client.app.state.session_factory() as db:
        output = db.get(AssetVersion, UUID(value["id"]))
        assert db.get(VisualAsset, output.asset_id).current_version_id is None
        edges = list(
            db.scalars(
                select(AssetDependency).where(AssetDependency.target_asset_version_id == output.id)
            )
        )
        assert {str(edge.source_asset_version_id) for edge in edges} == {
            body["storyboard_id"],
            *(item["asset_version_id"] for item in body["selected_shots"]),
        }
        assert {edge.dependency_type for edge in edges} == {
            "SEQUENCE_DEPENDENCY",
            "RENDER_DEPENDENCY",
        }


def test_replay_is_order_independent_conflicts_are_atomic_and_versions_paginate(
    client, episode, timeline_case
):
    body, _ = timeline_case
    original = created_timeline(client, episode, body)
    counts = timeline_counts(client)
    replay = deepcopy(body)
    replay["selected_shots"].reverse()
    assert created_timeline(client, episode, replay) == original
    assert timeline_counts(client) == counts
    replay["default_duration_ms"] = 2000
    conflict = client.post(timeline_url(episode), json=replay)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert timeline_counts(client) == counts
    replay["creation_key"] = str(uuid4())
    newer = created_timeline(client, episode, replay)
    assert newer["version_no"] == 2 and newer["snapshot"]["duration_ms"] == 10000
    assert client.get(timeline_url(episode), params={"limit": 1}).json() == [newer]
    assert client.get(timeline_url(episode), params={"before_version": 2}).json() == [original]
    assert client.get(timeline_url(episode), params={"limit": 101}).status_code == 422
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.action == "timeline.created")
            )
            == 2
        )


@pytest.mark.parametrize("problem", ["incomplete", "duplicate", "foreign_shot", "foreign_board"])
def test_incomplete_duplicate_and_wrong_scope_inputs_fail_without_partial_history(
    client, episode, timeline_case, problem
):
    body, _ = timeline_case
    value = deepcopy(body)
    if problem == "incomplete":
        value["selected_shots"].pop()
    elif problem == "duplicate":
        value["selected_shots"][1] = value["selected_shots"][0]
    elif problem == "foreign_shot":
        value["selected_shots"][0]["shot_id"] = str(uuid4())
    else:
        value["storyboard_id"] = str(uuid4())
    before = timeline_counts(client)
    assert client.post(timeline_url(episode), json=value).status_code in {404, 422}
    assert timeline_counts(client) == before


@pytest.mark.parametrize("problem", ["unselected", "draft", "stale", "archived", "board"])
def test_current_selection_and_lifecycle_are_rechecked_for_every_exact_input(
    client, episode, timeline_case, problem
):
    body, _ = timeline_case
    # Use the last shot so a failed final check also proves the preceding work rolls back.
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, UUID(body["selected_shots"][-1]["asset_version_id"]))
        asset = db.get(VisualAsset, version.asset_id)
        if problem == "unselected":
            asset.current_version_id = None
        elif problem == "draft":
            version.status = "DRAFT"
        elif problem == "stale":
            version.stale = True
        elif problem == "archived":
            asset.status = "ARCHIVED"
        else:
            db.get(AssetVersion, UUID(body["storyboard_id"])).stale = True
    before = timeline_counts(client)
    result = client.post(timeline_url(episode), json=body)
    assert result.status_code == 409, result.text
    assert timeline_counts(client) == before


def test_selected_legacy_candidate_without_review_is_rejected(client, episode, timeline_case):
    body, _ = timeline_case
    job, attempt = generate(client, episode["project_id"])
    assert ingest(client, attempt) == 1
    candidate = candidates(client, episode["project_id"], job)[0]
    identity = UUID(candidate["asset_version_id"])
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, identity)
        version.status = "APPROVED"
        db.get(VisualAsset, version.asset_id).current_version_id = identity
    body["selected_shots"][-1]["asset_version_id"] = str(identity)
    before = timeline_counts(client)
    result = client.post(timeline_url(episode), json=body)
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    assert timeline_counts(client) == before


def test_latest_review_revokes_new_use_but_preserves_existing_timeline(
    client, episode, timeline_case
):
    body, reviews = timeline_case
    original = created_timeline(client, episode, body)
    rerun = deepcopy(reviews[-1])
    rerun["result_id"] = str(uuid4())
    record, _ = saved(client, reviews[-1])
    rerun["rerun"] = {
        "previous_review_id": reviews[-1]["result_id"],
        "previous_sha256": digest(record.model_dump(mode="json")),
        "reason": "重新审查当前精确候选",
    }
    saved(client, rerun)
    body["creation_key"] = str(uuid4())
    before = timeline_counts(client)
    result = client.post(timeline_url(episode), json=body)
    assert result.status_code == 409, result.text
    assert timeline_counts(client) == before
    assert client.get(f"/api/v1/timelines/{original['id']}").json() == original


def test_missing_object_and_wrong_shot_review_are_not_laundered(
    client, episode, timeline_case, settings
):
    body, _ = timeline_case
    swapped = deepcopy(body)
    first, last = swapped["selected_shots"][0], swapped["selected_shots"][-1]
    first["asset_version_id"], last["asset_version_id"] = (
        last["asset_version_id"],
        first["asset_version_id"],
    )
    before = timeline_counts(client)
    assert client.post(timeline_url(episode), json=swapped).status_code == 409
    identity = UUID(body["selected_shots"][-1]["asset_version_id"])
    object_path(settings.asset_storage_path, UUID(episode["project_id"]), identity).unlink()
    result = client.post(timeline_url(episode), json=body)
    assert result.status_code in {404, 409}, result.text
    assert timeline_counts(client) == before


def test_upstream_invalidation_keeps_frozen_history_and_production_facts(
    client, episode, timeline_case
):
    body, _ = timeline_case
    original = created_timeline(client, episode, body)
    with client.app.state.session_factory.begin() as db:
        facts = production_facts(db, UUID(episode["id"]), "service-owner")
        assert facts.timeline.version_id == UUID(original["id"])
        assert facts.timeline.status == "DRAFT" and not facts.timeline.approved
        assert set(facts.timeline.source_version_ids) == {
            UUID(body["storyboard_id"]),
            *(UUID(item["asset_version_id"]) for item in body["selected_shots"]),
        }
        assert "TIMELINE" not in facts.unavailable_producers
        source = db.get(AssetVersion, UUID(body["selected_shots"][0]["asset_version_id"]))
        apply(db, UUID(episode["project_id"]), source)
    read = client.get(f"/api/v1/timelines/{original['id']}").json()
    assert read["stale"] is True and read["snapshot"] == original["snapshot"]
    assert read["content_sha256"] == original["content_sha256"]
    with client.app.state.session_factory() as db:
        facts = production_facts(db, UUID(episode["id"]), "service-owner")
        assert facts.timeline.stale is True


def test_project_membership_archive_delete_and_revocation_apply_to_timelines(
    client, episode, timeline_case
):
    body, _ = timeline_case
    original = created_timeline(client, episode, body)
    detail = f"/api/v1/timelines/{original['id']}"
    owner_header = client.headers["Authorization"]
    member = register(client, "timeline-viewer@example.com")
    assert client.get(detail).status_code == 404
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(episode["project_id"]),
            user_id=UUID(member["user"]["id"]),
            role="viewer",
        )
        db.add(membership)
        db.flush()
        membership_id = membership.id
    assert client.get(detail).json() == original
    assert client.post(timeline_url(episode), json=body).status_code == 403
    with client.app.state.session_factory.begin() as db:
        db.get(ProjectMember, membership_id).role = "editor"
    assert created_timeline(client, episode, body) == original
    editor_body = {**body, "creation_key": str(uuid4())}
    assert created_timeline(client, episode, editor_body)["version_no"] == 2
    with client.app.state.session_factory.begin() as db:
        db.delete(db.get(ProjectMember, membership_id))
    assert client.get(detail).status_code == 404
    assert client.post(timeline_url(episode), json=body).status_code == 404
    client.headers["Authorization"] = owner_header
    for model, field in ((Episode, "status"), (Project, "archived")):
        with client.app.state.session_factory.begin() as db:
            identity = episode["id"] if model is Episode else episode["project_id"]
            setattr(db.get(model, UUID(identity)), field, "ARCHIVED" if model is Episode else True)
        assert client.get(detail).json() == original
        assert client.post(timeline_url(episode), json=body).status_code == 409
        with client.app.state.session_factory.begin() as db:
            setattr(db.get(model, UUID(identity)), field, "DRAFT" if model is Episode else False)
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).deleted_at = datetime.now(UTC)
    assert client.get(detail).status_code == 404
    assert client.get(timeline_url(episode)).status_code == 404


def test_sealed_parent_tracks_and_clips_reject_rewriting_deletion_and_appending(
    client, episode, timeline_case
):
    body, _ = timeline_case
    original = created_timeline(client, episode, body)
    for model in (Timeline, TimelineTrack, TimelineClip):
        field = "duration_ms" if model in {Timeline, TimelineClip} else "ordinal"
        for statement in (update(model).values({field: 1}), delete(model)):
            with pytest.raises(DBAPIError), client.app.state.session_factory.begin() as db:
                db.execute(statement)
    with pytest.raises(DBAPIError), client.app.state.session_factory.begin() as db:
        db.add(TimelineTrack(timeline_id=UUID(original["id"]), kind="VIDEO", ordinal=0))
        db.flush()
    with pytest.raises(DBAPIError), client.app.state.session_factory.begin() as db:
        clip = original["snapshot"]["tracks"][0]["clips"][0]
        db.add(
            TimelineClip(
                **{
                    **clip,
                    "id": uuid4(),
                    "shot_id": UUID(clip["shot_id"]),
                    "source_asset_version_id": UUID(clip["source_asset_version_id"]),
                    "review_id": UUID(clip["review_id"]),
                    "review_storyboard_id": UUID(clip["review_storyboard_id"]),
                    "review_shot_id": UUID(clip["review_shot_id"]),
                },
                timeline_id=UUID(original["id"]),
                track_id=UUID(original["snapshot"]["tracks"][0]["id"]),
            )
        )
        db.flush()
    assert client.get(f"/api/v1/timelines/{original['id']}").json() == original


def test_sqlite_replace_cannot_overwrite_sealed_history(client, episode, timeline_case, settings):
    if not settings.database_url.startswith("sqlite"):
        pytest.skip("SQLite REPLACE semantics")
    body, _ = timeline_case
    original = created_timeline(client, episode, body)
    for table in ("timelines", "timeline_tracks", "timeline_clips"):
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(text(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"))
    assert client.get(f"/api/v1/timelines/{original['id']}").json() == original


def test_postgres_duplicate_creates_have_one_version_manifest_and_audit(
    client, episode, timeline_case, settings
):
    require_postgres(settings)
    body, _ = timeline_case
    before = timeline_counts(client)
    responses = race(client.app, timeline_url(episode), body, dict(client.headers))
    assert [item.status_code for item in responses] == [201, 201], [item.text for item in responses]
    assert responses[0].json() == responses[1].json()
    after = timeline_counts(client)
    assert tuple(b - a for a, b in zip(before, after, strict=True)) == (1, 5, 5, 1, 1)
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.action == "timeline.created")
            )
            == 1
        )


def test_postgres_truncate_cannot_discard_frozen_history(client, episode, timeline_case, settings):
    require_postgres(settings)
    body, _ = timeline_case
    original = created_timeline(client, episode, body)
    for table in ("timelines", "timeline_tracks", "timeline_clips"):
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(text(f"TRUNCATE {table} CASCADE"))
    assert client.get(f"/api/v1/timelines/{original['id']}").json() == original
