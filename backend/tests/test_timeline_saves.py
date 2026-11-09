"""Compare-and-append timeline edits reuse real RENDER-01 media and review gates."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_continuity_reviews import review_case  # noqa: F401
from test_dependency_invalidation import apply
from test_dependency_validation import registry_version
from test_timelines import created_timeline, timeline_case, timeline_url  # noqa: F401

from arclight.application.production_state import production_facts
from arclight.persistence.models import (
    AssetDependency,
    AssetVersion,
    AuditEvent,
    Script,
    Storyboard,
    VisualAsset,
)
from arclight.persistence.timelines import Timeline, TimelineClip, TimelineRevision, TimelineTrack

__all__ = ["review_case", "timeline_case"]


def save_url(value):
    return f"/api/v1/timelines/{value['id']}"


@pytest.fixture
def saved_case(client, episode, timeline_case):
    create, _ = timeline_case
    original = created_timeline(client, episode, create)
    return (
        original,
        {
            "creation_key": str(uuid4()),
            "expected_version_no": original["version_no"],
            "snapshot": deepcopy(original["snapshot"]),
        },
        create,
    )


def duration_edit(body, duration):
    value = deepcopy(body)
    video = next(track for track in value["snapshot"]["tracks"] if track["kind"] == "VIDEO")
    value["snapshot"]["default_duration_ms"] = duration
    value["snapshot"]["duration_ms"] = duration * len(video["clips"])
    for index, clip in enumerate(sorted(video["clips"], key=lambda clip: clip["ordinal"])):
        clip.update(start_ms=index * duration, duration_ms=duration)
    return value


def saved_timeline(client, original, body):
    response = client.patch(save_url(original), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def save_counts(client):
    with client.app.state.session_factory() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (
                Timeline,
                TimelineTrack,
                TimelineClip,
                TimelineRevision,
                AssetVersion,
                AssetDependency,
            )
        )


def test_save_appends_new_draft_with_exact_parent_and_independent_frozen_history(
    client, episode, saved_case
):
    original, body, _ = saved_case
    value = saved_timeline(client, original, duration_edit(body, 2000))
    assert value["version_no"] == 2 and value["based_on_timeline_id"] == original["id"]
    assert value["status"] == "DRAFT" and value["stale"] is False
    assert value["snapshot"]["duration_ms"] == 10000
    assert value["content_sha256"] != original["content_sha256"]
    assert value["dependency_manifest_id"] != original["dependency_manifest_id"]
    assert client.get(save_url(original)).json() == original
    assert client.get(save_url(value)).json() == value
    assert client.get(timeline_url(episode)).json() == [value, original]
    old_tracks = original["snapshot"]["tracks"]
    new_tracks = value["snapshot"]["tracks"]
    assert not {t["id"] for t in old_tracks} & {t["id"] for t in new_tracks}
    assert not {c["id"] for c in old_tracks[0]["clips"]} & {c["id"] for c in new_tracks[0]["clips"]}
    for before, after in zip(old_tracks[0]["clips"], new_tracks[0]["clips"], strict=True):
        for name in (
            "shot_id",
            "source_asset_version_id",
            "source_sha256",
            "review_id",
            "review_storyboard_id",
            "review_shot_id",
        ):
            assert after[name] == before[name]
    with client.app.state.session_factory() as db:
        receipt = db.get(TimelineRevision, UUID(value["id"]))
        assert receipt.based_on_timeline_id == UUID(original["id"])
        assert receipt.expected_version_no == 1
        version = db.get(AssetVersion, receipt.id)
        assert version.metadata_json["based_on_timeline_id"] == original["id"]
        assert db.get(VisualAsset, version.asset_id).current_version_id is None
        assert (
            production_facts(db, UUID(episode["id"]), "service-owner").timeline.version_id
            == receipt.id
        )


def test_replay_is_canonical_and_survives_later_head_without_repeating_effects(
    client, episode, saved_case
):
    original, body, create = saved_case
    before = save_counts(client)
    cross_command = {**body, "creation_key": create["creation_key"]}
    conflict = client.patch(save_url(original), json=cross_command)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert save_counts(client) == before
    first = saved_timeline(client, original, body)
    before = save_counts(client)
    reversed_body = deepcopy(body)
    reversed_body["snapshot"]["tracks"].reverse()
    for track in reversed_body["snapshot"]["tracks"]:
        track["clips"].reverse()
    assert saved_timeline(client, original, reversed_body) == first
    assert save_counts(client) == before
    changed = duration_edit(body, 2000)
    response = client.patch(save_url(original), json=changed)
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    assert save_counts(client) == before
    create["creation_key"] = str(uuid4())
    newest = created_timeline(client, episode, create)
    assert newest["version_no"] == 3
    assert saved_timeline(client, original, body) == first
    before = save_counts(client)
    response = client.patch(save_url(newest), json=body)
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    assert save_counts(client) == before
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.action == "timeline.saved")
            )
            == 1
        )


@pytest.mark.parametrize("advance", ["save", "create"])
def test_changed_head_conflicts_without_partial_versions_receipts_or_edges(
    client, episode, saved_case, advance
):
    original, body, create = saved_case
    wrong = {**body, "expected_version_no": original["version_no"] + 1}
    before = save_counts(client)
    rejected = client.patch(save_url(original), json=wrong)
    assert rejected.status_code == 409
    assert save_counts(client) == before
    if advance == "save":
        saved_timeline(client, original, body)
    else:
        create["creation_key"] = str(uuid4())
        created_timeline(client, episode, create)
    body["creation_key"] = str(uuid4())
    before = save_counts(client)
    response = client.patch(save_url(original), json=body)
    assert response.status_code == 409 and response.json()["error"]["code"] == "VERSION_CONFLICT"
    assert "第2版" in response.json()["error"]["message"]
    assert save_counts(client) == before
    assert client.get(save_url(original)).json() == original


@pytest.mark.parametrize(
    "problem", ["hash", "review", "scope", "profile", "missing_shot", "line", "stale"]
)
def test_save_revalidates_exact_references_scope_and_validity_atomically(
    client, saved_case, problem
):
    original, body, _ = saved_case
    clip = body["snapshot"]["tracks"][0]["clips"][-1]
    if problem == "hash":
        clip["source_sha256"] = "a" * 64
    elif problem == "review":
        clip["review_id"] = str(uuid4())
    elif problem == "scope":
        body["snapshot"]["episode_id"] = str(uuid4())
    elif problem == "profile":
        body["snapshot"]["output_profile"]["fps"] = 60
    elif problem == "missing_shot":
        clip["shot_id"] = body["snapshot"]["tracks"][0]["clips"][0]["shot_id"]
    elif problem == "line":
        with client.app.state.session_factory() as db:
            board = db.get(Storyboard, UUID(body["snapshot"]["storyboard_id"]))
            clip.update(script_version_id=str(board.script_version_id), line_id=str(uuid4()))
    else:
        with client.app.state.session_factory.begin() as db:
            db.get(AssetVersion, UUID(clip["source_asset_version_id"])).stale = True
    before = save_counts(client)
    response = client.patch(save_url(original), json=body)
    assert response.status_code in {409, 422}, response.text
    assert save_counts(client) == before
    assert client.get(save_url(original)).json() == original


def test_all_track_references_survive_save_and_have_independent_dependencies(
    client, episode, saved_case
):
    """Approved audio/subtitle producer contract fixtures; no claim of TTS or audio file quality."""
    original, body, _ = saved_case
    with client.app.state.session_factory.begin() as db:
        board = db.get(Storyboard, UUID(body["snapshot"]["storyboard_id"]))
        script = db.get(Script, board.script_version_id)
        line_id = script.content_json["scenes"][0]["dialogue"][0]["line_id"]
        _, audio = registry_version(db, UUID(episode["project_id"]), kind="AUDIO")
        _, subtitle = registry_version(db, UUID(episode["project_id"]), kind="SUBTITLE")
        audio_id = audio.id
        for track in body["snapshot"]["tracks"][1:]:
            clip = {
                "id": str(uuid4()),
                "ordinal": 0,
                "start_ms": 0,
                "duration_ms": 1000,
                "source_asset_version_id": str(
                    subtitle.id if track["kind"] == "SUBTITLE" else audio.id
                ),
                "source_sha256": "a" * 64,
            }
            if track["kind"] in {"VOICE", "SUBTITLE"}:
                clip.update(script_version_id=str(script.id), line_id=line_id)
            if track["kind"] == "SUBTITLE":
                clip["audio_version_id"] = str(audio.id)
            track["clips"] = [clip]
    before = save_counts(client)
    invalid_review = deepcopy(body)
    invalid_review["snapshot"]["tracks"][1]["clips"][0]["review_id"] = str(uuid4())
    assert client.patch(save_url(original), json=invalid_review).status_code == 409
    foreign_shot = deepcopy(body)
    foreign_shot["snapshot"]["tracks"][1]["clips"][0]["shot_id"] = str(uuid4())
    assert client.patch(save_url(original), json=foreign_shot).status_code == 422
    assert save_counts(client) == before
    value = saved_timeline(client, original, body)
    assert all(track["clips"] for track in value["snapshot"]["tracks"])
    with client.app.state.session_factory.begin() as db:
        assert (
            db.scalar(
                select(func.count(AssetDependency.id)).where(
                    AssetDependency.target_asset_version_id == UUID(value["id"])
                )
            )
            == 8
        )
        apply(db, UUID(episode["project_id"]), db.get(AssetVersion, audio_id))
    assert client.get(save_url(value)).json()["stale"] is True
    assert client.get(save_url(original)).json() == original


def test_save_receipt_is_scoped_and_immutable_without_rewriting_parent_history(
    client, saved_case, settings
):
    original, body, _ = saved_case
    value = saved_timeline(client, original, body)
    for statement in (
        update(TimelineRevision).values(expected_version_no=2),
        delete(TimelineRevision),
    ):
        with pytest.raises(DBAPIError), client.app.state.session_factory.begin() as db:
            db.execute(statement)
    with pytest.raises(DBAPIError), client.app.state.session_factory.begin() as db:
        db.add(
            TimelineRevision(
                id=UUID(original["id"]),
                based_on_timeline_id=UUID(value["id"]),
                expected_version_no=2,
                created_by="service-owner",
            )
        )
        db.flush()
    if settings.database_url.startswith("sqlite"):
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(
                text("INSERT OR REPLACE INTO timeline_revisions SELECT * FROM timeline_revisions")
            )
    assert client.get(save_url(original)).json() == original
    assert client.get(save_url(value)).json() == value


def test_saving_an_approved_selected_timeline_never_inherits_approval_or_selection(
    client, saved_case
):
    original, body, _ = saved_case
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, UUID(original["id"]))
        version.status = "APPROVED"
        db.get(VisualAsset, version.asset_id).current_version_id = version.id
    approved = client.get(save_url(original)).json()
    value = saved_timeline(client, original, body)
    assert value["status"] == "DRAFT"
    assert client.get(save_url(original)).json() == approved
    with client.app.state.session_factory() as db:
        assert db.get(VisualAsset, UUID(value["asset_id"])).current_version_id == UUID(
            original["id"]
        )


def test_save_membership_archive_delete_and_revocation_are_rechecked(client, episode, saved_case):
    from datetime import UTC, datetime

    from test_collaboration import register

    from arclight.persistence.models import Episode, Project, ProjectMember

    original, body, _ = saved_case
    owner_header = client.headers["Authorization"]
    member = register(client, "timeline-editor@example.com")
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
    assert client.get(save_url(original)).json() == original
    assert client.patch(save_url(original), json=body).status_code == 403
    with client.app.state.session_factory.begin() as db:
        db.get(ProjectMember, identity).role = "editor"
    value = saved_timeline(client, original, body)
    with client.app.state.session_factory.begin() as db:
        db.delete(db.get(ProjectMember, identity))
    assert client.patch(save_url(original), json=body).status_code == 404
    client.headers["Authorization"] = owner_header
    for model, field in ((Episode, "status"), (Project, "archived")):
        with client.app.state.session_factory.begin() as db:
            resource = UUID(episode["id"] if model is Episode else episode["project_id"])
            setattr(db.get(model, resource), field, "ARCHIVED" if model is Episode else True)
        assert client.get(save_url(value)).json() == value
        assert client.patch(save_url(original), json=body).status_code == 409
        with client.app.state.session_factory.begin() as db:
            setattr(db.get(model, resource), field, "DRAFT" if model is Episode else False)
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).deleted_at = datetime.now(UTC)
    assert client.patch(save_url(original), json=body).status_code == 404
    assert client.get(save_url(value)).status_code == 404
