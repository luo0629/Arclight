"""Generated subtitles reuse version history, Owner approval and CAS selection locks."""

from uuid import UUID, uuid4

import pytest
from render_manifest_helpers import render_case, review_case, timeline_case
from test_asset_versions import draft
from test_audio_version_lifecycle import read_ok, selection_payload
from test_dependency_validation import registry_version
from test_model_services import account
from test_subtitle_production import counts, subtitle_case, subtitle_source, version_url

from arclight.application.render_inputs import file_storage
from arclight.persistence.models import AssetVersion, ProjectMember, StoredFile, VisualAsset

__all__ = ["render_case", "review_case", "subtitle_case", "subtitle_source", "timeline_case"]


def asset_url(case):
    return version_url(case).rsplit("/versions/", 1)[0]


def approve_subtitle(client, case):
    url = version_url(case)
    for status in ("READY_FOR_REVIEW", "APPROVED"):
        current = read_ok(client, url)
        response = client.patch(
            url + "/state", json={"status": status, "expected_revision": current["revision"]}
        )
        assert response.status_code == 200, response.text
    return read_ok(client, url)


def select_subtitle(client, case):
    response = client.post(
        version_url(case) + "/select", json=selection_payload(read_ok(client, asset_url(case)))
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_subtitle_review_selection_and_lock_are_explicit_and_preserve_exact_history(
    client, subtitle_case
):
    case = subtitle_case
    initial = read_ok(client, asset_url(case))
    assert initial["asset_type"] == "SUBTITLE"
    assert initial["metadata"] == {
        "episode_id": case["episode"]["id"],
        "subtitle_production": 1,
    }
    assert initial["current_version_id"] is None
    assert read_ok(client, version_url(case))["status"] == "DRAFT"
    assert client.get(asset_url(case) + "/versions").json()[0]["id"] == case["track"]["id"]
    denied = client.post(version_url(case) + "/select", json=selection_payload(initial))
    assert denied.status_code == 409, denied.text
    approved = approve_subtitle(client, case)
    assert approved["id"] == case["track"]["id"]
    assert read_ok(client, asset_url(case))["current_version_id"] is None
    selected = select_subtitle(client, case)
    assert selected["current_version_id"] == case["track"]["id"]
    locked = client.post(version_url(case) + "/lock", json=selection_payload(selected))
    assert locked.status_code == 200, locked.text
    assert locked.json()["status"] == "LOCKED"
    assert read_ok(client, version_url(case))["status"] == "LOCKED"


@pytest.mark.parametrize(
    "role,read_status,write_status", [(None, 404, 404), ("viewer", 200, 403), ("editor", 200, 403)]
)
def test_subtitle_lifecycle_reading_and_owner_review_preserve_project_permissions(
    client, subtitle_case, role, read_status, write_status
):
    case = subtitle_case
    ready = client.patch(
        version_url(case) + "/state", json={"status": "READY_FOR_REVIEW", "expected_revision": 0}
    )
    assert ready.status_code == 200, ready.text
    current = read_ok(client, asset_url(case))
    member = account(client)
    if role:
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(case["episode"]["project_id"]), user_id=UUID(member), role=role
                )
            )
    for url in (asset_url(case), asset_url(case) + "/versions", version_url(case)):
        response = client.get(url)
        assert response.status_code == read_status, response.text
    response = client.patch(
        version_url(case) + "/state",
        json={"status": "APPROVED", "expected_revision": ready.json()["revision"]},
    )
    assert response.status_code == write_status, response.text
    response = client.post(version_url(case) + "/lock", json=selection_payload(current))
    assert response.status_code == write_status, response.text


def test_subtitle_selection_and_lock_keep_double_cas_and_unlock_when_srt_is_missing(
    client, settings, subtitle_case
):
    case = subtitle_case
    initial_version = read_ok(client, version_url(case))
    approve_subtitle(client, case)
    response = client.patch(
        version_url(case) + "/state",
        json={"status": "ARCHIVED", "expected_revision": initial_version["revision"]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"
    initial_asset = read_ok(client, asset_url(case))
    selected = select_subtitle(client, case)
    for field in ("expected_revision", "expected_lock_version"):
        payload = selection_payload(selected)
        payload[field] = selection_payload(initial_asset)[field]
        response = client.post(version_url(case) + "/lock", json=payload)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "VERSION_CONFLICT"
    response = client.post(version_url(case) + "/lock", json=selection_payload(selected))
    assert response.status_code == 200, response.text
    locked = response.json()
    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(case["track"]["file_id"]))
        file_storage(settings, file).delete(file.object_key)
    response = client.post(version_url(case) + "/unlock", json=selection_payload(locked))
    assert response.status_code == 200, response.text
    unlocked = response.json()
    assert unlocked["status"] != "LOCKED"
    assert read_ok(client, version_url(case))["status"] == "APPROVED"
    response = client.post(version_url(case) + "/lock", json=selection_payload(unlocked))
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "OBJECT_UNAVAILABLE"


@pytest.mark.parametrize("operation", ["ready", "approve", "select"])
def test_positive_subtitle_lifecycle_rechecks_srt_bytes_without_hiding_version_history(
    client, settings, subtitle_case, operation
):
    case = subtitle_case
    if operation in {"approve", "select"}:
        ready = client.patch(
            version_url(case) + "/state",
            json={"status": "READY_FOR_REVIEW", "expected_revision": 0},
        )
        assert ready.status_code == 200, ready.text
    if operation == "select":
        response = client.patch(
            version_url(case) + "/state",
            json={"status": "APPROVED", "expected_revision": ready.json()["revision"]},
        )
        assert response.status_code == 200, response.text
    before = read_ok(client, version_url(case))
    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(case["track"]["file_id"]))
        file_storage(settings, file).delete(file.object_key)
    if operation == "select":
        response = client.post(
            version_url(case) + "/select", json=selection_payload(read_ok(client, asset_url(case)))
        )
    else:
        response = client.patch(
            version_url(case) + "/state",
            json={
                "status": "READY_FOR_REVIEW" if operation == "ready" else "APPROVED",
                "expected_revision": before["revision"],
            },
        )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "OBJECT_UNAVAILABLE"
    assert read_ok(client, version_url(case)) == before


def test_subtitle_lifecycle_keeps_image_creation_restoration_rebuild_and_visual_routes_closed(
    client, subtitle_case
):
    case = subtitle_case
    asset = read_ok(client, asset_url(case))
    before = counts(client)
    for suffix in ("/image", "/rebuild-inputs", "/continuity"):
        response = client.get(version_url(case) + suffix)
        assert response.status_code == 404, response.text
    requests = [
        (asset_url(case) + "/versions", draft(asset)),
        (
            version_url(case) + "/restore-as-new",
            {
                "creation_key": str(uuid4()),
                "expected_revision": asset["revision"],
                "comment": "字幕不能走图片恢复入口",
            },
        ),
        (
            version_url(case) + "/rebuild-manifest",
            {
                "creation_key": str(uuid4()),
                "expected_revision": asset["revision"],
                "replacements": {},
            },
        ),
    ]
    for url, payload in requests:
        response = client.post(url, json=payload)
        assert response.status_code == 404, response.text
    assert counts(client) == before


@pytest.mark.parametrize("metadata", [{}, {"episode_id": "from_episode"}])
def test_legacy_subtitle_asset_metadata_remains_readable_without_a_production_marker(
    client, episode, metadata
):
    with client.app.state.session_factory.begin() as db:
        asset, version = registry_version(db, UUID(episode["project_id"]), kind="SUBTITLE")
        if metadata:
            asset.metadata_json = {"episode_id": episode["id"]}
        aid, vid = str(asset.id), str(version.id)
    url = f"/api/v1/projects/{episode['project_id']}/assets/{aid}"
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert "subtitle_production" not in response.json()["metadata"]
    assert client.get(url + "/versions").json()[0]["id"] == vid


@pytest.mark.parametrize("marker", ["asset", "version", "both"])
def test_production_markers_cannot_be_approved_without_an_actual_immutable_production_record(
    client, episode, marker
):
    with client.app.state.session_factory.begin() as db:
        asset = VisualAsset(
            project_id=UUID(episode["project_id"]),
            asset_type="SUBTITLE",
            name="isolated malformed production",
            metadata_json=(
                {"episode_id": episode["id"], "subtitle_production": 1}
                if marker in {"asset", "both"}
                else {}
            ),
        )
        db.add(asset)
        db.flush()
        version = AssetVersion(
            asset_id=asset.id,
            version_no=1,
            checksum="a" * 64,
            object_key="test-only:no-production",
            creation_key=uuid4(),
            created_by="service-owner",
            metadata_json=(
                {"subtitle_production_id": str(uuid4())} if marker in {"version", "both"} else {}
            ),
        )
        db.add(version)
        db.flush()
        aid, vid = str(asset.id), str(version.id)
    url = f"/api/v1/projects/{episode['project_id']}/assets/{aid}/versions/{vid}"
    before = counts(client)
    response = client.patch(
        url + "/state", json={"status": "READY_FOR_REVIEW", "expected_revision": 0}
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SUBTITLE_HISTORY_INVALID"
    assert counts(client) == before
    assert read_ok(client, url)["status"] == "DRAFT"


@pytest.mark.parametrize("operation", ["ready", "approve", "select", "lock"])
def test_unmarked_legacy_subtitles_cannot_gain_new_positive_lifecycle_without_provenance(
    client, episode, operation
):
    with client.app.state.session_factory.begin() as db:
        status = {"ready": "DRAFT", "approve": "READY_FOR_REVIEW"}.get(operation, "APPROVED")
        asset, version = registry_version(
            db, UUID(episode["project_id"]), kind="SUBTITLE", status=status
        )
        if operation == "lock":
            asset.current_version_id = version.id
        aid, vid = str(asset.id), str(version.id)
    url = f"/api/v1/projects/{episode['project_id']}/assets/{aid}/versions/{vid}"
    before = counts(client)
    if operation in {"ready", "approve"}:
        response = client.patch(
            url + "/state",
            json={
                "status": "READY_FOR_REVIEW" if operation == "ready" else "APPROVED",
                "expected_revision": 0,
            },
        )
    else:
        response = client.post(
            url + f"/{operation}",
            json=selection_payload(read_ok(client, url.rsplit("/versions/", 1)[0])),
        )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SUBTITLE_PROVENANCE_REQUIRED"
    assert counts(client) == before
    assert read_ok(client, url)["status"] == status
