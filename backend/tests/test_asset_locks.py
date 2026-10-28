"""ASSET-07 selection and lock CAS, owner gates and migration preservation."""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from test_asset_selection import approved, choose, current, restore
from test_asset_versions import draft, private_objects, state, versions  # noqa: F401
from test_assets import archive, create, rename
from test_collaboration import create_project, register

from arclight.persistence.models import AssetSelectionLock, ProjectMember


def lock(client, row, version, action="lock", **values):
    return client.post(
        versions(row) + "/" + version["id"] + "/" + action,
        json={
            "expected_revision": row["revision"],
            "expected_lock_version": row["lock_version"],
            **values,
        },
    )


def selected(client, project_id):
    row = create(client, project_id)
    version = approved(client, row)
    response = choose(client, current(client, row), version)
    assert response.status_code == 200, response.text
    return response.json(), version


def test_lock_unlock_preserve_exact_selection_and_block_mutations(client, episode):
    row, version = selected(client, episode["project_id"])
    response = lock(client, row, version)
    assert response.status_code == 200, response.text
    locked = response.json()
    assert locked["status"] == "LOCKED" and locked["lock_version"] == 2
    assert locked["current_version_id"] == version["id"]
    assert rename(client, locked).status_code == 409
    assert archive(client, locked).status_code == 409
    assert choose(client, locked, version).status_code == 409
    assert client.post(versions(row), json=draft(locked)).status_code == 409
    assert restore(client, locked, version).status_code == 409
    assert state(client, locked, version, "STALE").status_code == 409
    assert client.get(versions(row) + "/" + version["id"] + "/image").status_code == 200
    locked_version = client.get(versions(row) + "/" + version["id"]).json()
    assert locked_version["status"] == "LOCKED"
    assert locked_version["checksum"] == version["checksum"]
    assert lock(client, row, version, "unlock").status_code == 409
    unlocked = lock(client, locked, version, "unlock")
    assert unlocked.status_code == 200, unlocked.text
    row = unlocked.json()
    assert row["status"] == "DRAFT" and row["lock_version"] == 3
    assert row["current_version_id"] == version["id"]
    assert client.get(versions(row) + "/" + version["id"]).json()["status"] == "APPROVED"
    assert rename(client, row).status_code == 200
    with client.app.state.session_factory() as db:
        assert db.scalar(select(AssetSelectionLock)) is None


def test_wrong_lock_token_and_nonselected_version_do_not_mutate(client, episode):
    row, version = selected(client, episode["project_id"])
    assert lock(client, row, version, expected_lock_version=0).status_code == 409
    assert current(client, row) == row
    other = approved(client, row)
    row = current(client, row)
    assert lock(client, row, other).status_code == 409
    response = client.post(
        versions(row) + "/" + other["id"] + "/select",
        json={
            "expected_revision": row["revision"],
            "expected_lock_version": 0,
        },
    )
    assert response.status_code == 409
    assert current(client, row)["current_version_id"] == version["id"]


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_only_owner_can_lock_or_unlock(client, role):
    register(client)
    project = create_project(client)
    row, version = selected(client, project["id"])
    locked = lock(client, row, version).json()
    member = register(client, role + "@example.com")
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role=role
                )
            )
    for action in ("lock", "unlock"):
        assert lock(client, locked, version, action).status_code == (
            404 if role == "outsider" else 403
        )


@pytest.mark.legacy_schema
def test_lock_migration_roundtrip_and_guard(client, episode, migrated):
    command.downgrade(migrated, "0016")
    command.upgrade(migrated, "0018")
    row, version = selected(client, episode["project_id"])
    locked = lock(client, row, version).json()
    with pytest.raises(RuntimeError, match="Active selection locks exist"):
        command.downgrade(migrated, "0016")
    assert current(client, row)["status"] == "LOCKED"
    assert lock(client, locked, version, "unlock").status_code == 200
    command.downgrade(migrated, "0016")
    command.upgrade(migrated, "0018")
    config = Config(migrated.config_file_name, output_buffer=StringIO())
    with pytest.raises(RuntimeError, match="online preservation check"):
        command.downgrade(config, "0017:0016", sql=True)
    # Verify the current schema after the historical preservation assertions.
    command.upgrade(migrated, "head")
    command.check(migrated)


@pytest.mark.parametrize("actions", [("lock", "lock"), ("lock", "select"), ("unlock", "unlock")])
def test_concurrent_lock_selection_and_unlock_are_serialized(client, episode, settings, actions):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL concurrency verified in isolated CI database")
    row, version = selected(client, episode["project_id"])
    if actions[0] == "unlock":
        row = lock(client, row, version).json()

    def perform(action):
        return (
            choose(client, row, version)
            if action == "select"
            else lock(client, row, version, action)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(perform, actions))
    assert sorted(response.status_code for response in results) == [200, 409]
