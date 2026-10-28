"""Explicit exact-version selection and append-only restoration."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_asset_versions import private_objects, save, state, versions  # noqa: F401
from test_assets import archive, create, path
from test_collaboration import create_project, register

from arclight.persistence.models import AuditEvent, ProjectMember


def current(client, row):
    return client.get(path(row["project_id"]) + "/" + row["id"]).json()


def approved(client, row):
    draft = save(client, row)
    ready = state(client, row, draft, "READY_FOR_REVIEW").json()
    return state(client, row, ready, "APPROVED").json()


def choose(client, row, version):
    return client.post(
        versions(row) + "/" + version["id"] + "/select", json={"expected_revision": row["revision"]}
    )


def restore(client, row, version, **values):
    return client.post(
        versions(row) + "/" + version["id"] + "/restore-as-new",
        json={
            "creation_key": str(uuid4()),
            "expected_revision": row["revision"],
            "comment": "恢复历史构图",
            **values,
        },
    )


def test_selection_is_explicit_exact_and_does_not_change_history(client, episode):
    row = create(client, episode["project_id"])
    first = approved(client, row)
    row = current(client, row)
    assert row["current_version_id"] is None
    chosen = choose(client, row, first)
    assert chosen.status_code == 200, chosen.text
    row = chosen.json()
    assert row["current_version_id"] == first["id"]
    second = approved(client, row)
    assert current(client, row)["current_version_id"] == first["id"]
    assert client.get(versions(row) + "/" + first["id"]).json() == first
    row = current(client, row)
    assert choose(client, row, second).json()["current_version_id"] == second["id"]
    assert choose(client, row, first).status_code == 409
    with client.app.state.session_factory() as db:
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "asset.version_selected")
        ).all()
        assert len(events) == 2
        assert events[-1].details_json["previous_version_id"] == first["id"]


def test_restore_is_new_candidate_with_exact_bytes_and_retry_receipt(client, episode):
    row = create(client, episode["project_id"])
    first = approved(client, row)
    row = choose(client, current(client, row), first).json()
    key = str(uuid4())
    response = restore(client, row, first, creation_key=key)
    assert response.status_code == 201, response.text
    new = response.json()
    assert new["id"] != first["id"] and new["version_no"] == 2
    assert new["status"] == "DRAFT" and not new["stale"]
    assert new["restored_from_version_id"] == first["id"]
    assert new["restoration_note"] == "恢复历史构图"
    assert new["checksum"] == first["checksum"]
    assert current(client, row)["current_version_id"] == first["id"]
    assert client.get(versions(row) + "/" + first["id"]).json() == first
    assert restore(client, row, first, creation_key=key).json() == new
    assert restore(client, row, first, creation_key=key, comment="不同意图").status_code == 409
    assert choose(client, current(client, row), new).status_code == 409
    for version in (first, new):
        image = client.get(versions(row) + "/" + version["id"] + "/image")
        assert image.status_code == 200
        if version == first:
            original_bytes = image.content
        else:
            assert image.content == original_bytes


def test_rejected_and_stale_restore_require_fresh_review(client, episode):
    row = create(client, episode["project_id"])
    first = approved(client, row)
    stale = state(client, row, first, "STALE").json()
    row = current(client, row)
    assert choose(client, row, stale).status_code == 409
    new = restore(client, row, stale).json()
    assert new["status"] == "DRAFT" and not new["stale"]
    assert state(client, row, new, "APPROVED").status_code == 409
    ready = state(client, row, new, "READY_FOR_REVIEW").json()
    rejected = state(client, row, ready, "REJECTED").json()
    assert choose(client, current(client, row), rejected).status_code == 409
    assert client.get(versions(row) + "/" + first["id"]).json() == stale


def test_cross_asset_archive_and_missing_image_are_rejected(client, episode, settings):
    row = create(client, episode["project_id"])
    other = create(client, episode["project_id"])
    version = approved(client, row)
    row = current(client, row)
    assert choose(client, other, version).status_code == 404
    assert restore(client, other, version).status_code == 404
    archived = archive(client, row, True).json()
    assert choose(client, archived, version).status_code == 409
    assert restore(client, archived, version).status_code == 409
    row = archive(client, archived, False).json()
    for image in settings.asset_storage_path.rglob("*.image"):
        image.write_bytes(b"corrupt test image")
    assert choose(client, row, version).status_code == 409
    assert restore(client, row, version).status_code == 409
    assert current(client, row) == row


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_selection_and_restoration_permissions(client, role):
    register(client)
    project = create_project(client)
    row = create(client, project["id"])
    version = approved(client, row)
    row = current(client, row)
    member = register(client, role + "@example.com")
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role=role
                )
            )
    expected = {"editor": 200, "viewer": 403, "outsider": 404}[role]
    response = choose(client, row, version)
    assert response.status_code == expected
    if role == "editor":
        row = response.json()
    assert restore(client, row, version).status_code == (201 if role == "editor" else expected)


def test_concurrent_selection_only_accepts_one_revision(client, episode, settings):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrency verified in isolated CI database")
    row = create(client, episode["project_id"])
    first = approved(client, row)
    second = approved(client, current(client, row))
    row = current(client, row)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(choose, client, row, version) for version in (first, second)]
        assert sorted(result.result().status_code for result in results) == [200, 409]
