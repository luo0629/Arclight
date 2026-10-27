"""ASSET-01 directory behavior, preserving exact media references and project gates."""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_collaboration import create_project, register
from test_text_contract_v2 import make_asset

from arclight.persistence.models import AssetVersion, AuditEvent, ProjectMember, VisualAsset


def path(project_id):
    return f"/api/v1/projects/{project_id}/assets"


def create(client, project_id, kind="CHARACTER", name="雨"):
    response = client.post(path(project_id), json={"asset_type": kind, "name": name})
    assert response.status_code == 201, response.text
    return response.json()


def rename(client, row, name="改名"):
    return client.put(
        path(row["project_id"]) + "/" + row["id"],
        json={"name": name, "expected_revision": row["revision"]},
    )


def archive(client, row, archived=True):
    return client.patch(
        path(row["project_id"]) + "/" + row["id"],
        json={"archived": archived, "expected_revision": row["revision"]},
    )


def test_four_types_crud_pagination_and_audit(client, episode):
    project_id = episode["project_id"]
    rows = [create(client, project_id, kind) for kind in ("CHARACTER", "SCENE", "PROP", "STYLE")]
    first = rows[0]
    assert first["revision"] == first["lock_version"] == 0
    assert first["status"] == "DRAFT" and first["current_version_id"] is None
    assert client.get(path(project_id) + "?limit=2").json() == rows[:2]
    assert client.get(path(project_id) + "?limit=2&offset=2").json() == rows[2:]
    edited = rename(client, first, " 雨夜 ").json()
    assert edited["name"] == "雨夜" and edited["revision"] == 1
    assert edited["id"] == first["id"]
    assert rename(client, first).status_code == 409
    archived = archive(client, edited).json()
    assert archived["status"] == "ARCHIVED" and archived["revision"] == 2
    assert rename(client, archived).status_code == 409
    assert archive(client, edited, False).status_code == 409
    assert archive(client, archived).status_code == 409
    restored = archive(client, archived, False).json()
    assert restored["status"] == "DRAFT" and restored["revision"] == 3
    assert client.get(path(project_id) + "/" + first["id"]).json() == restored
    assert client.delete(path(project_id) + "/" + first["id"]).status_code == 405
    with client.app.state.session_factory() as db:
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.resource_id == UUID(first["id"]))
        ).all()
        assert {event.action for event in events} == {
            "asset.created",
            "asset.updated",
            "asset.archived",
            "asset.restored",
        }


@pytest.mark.parametrize(
    "values",
    [
        {"asset_type": "IMAGE"},
        {"asset_type": "character"},
        {"name": " "},
        {"name": "a" * 161},
        {"name": None},
        {"status": "APPROVED"},
        {"current_version_id": str(uuid4())},
        {"revision": 3},
    ],
)
def test_creation_rejects_invalid_or_privileged_fields(client, episode, values):
    project_id = episode["project_id"]
    response = client.post(path(project_id), json={"asset_type": "PROP", "name": "灯", **values})
    assert response.status_code == 422
    assert client.get(path(project_id)).json() == []


@pytest.mark.parametrize("expected", [True, -1, "0", None])
def test_strict_revision_and_no_type_status_or_pointer_edits(client, episode, expected):
    row = create(client, episode["project_id"])
    url = path(row["project_id"]) + "/" + row["id"]
    assert client.put(url, json={"name": "改", "expected_revision": expected}).status_code == 422
    assert (
        client.patch(url, json={"archived": True, "expected_revision": expected}).status_code == 422
    )
    for key, value in (
        ("asset_type", "SCENE"),
        ("status", "APPROVED"),
        ("current_version_id", None),
        ("lock_version", 9),
    ):
        assert (
            client.put(url, json={"name": "改", "expected_revision": 0, key: value}).status_code
            == 422
        )
    assert client.patch(url, json={"archived": 1, "expected_revision": 0}).status_code == 422
    assert client.get(url).json() == row


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_role_gates_and_cross_project_isolation(client, role):
    owner = register(client)
    project = create_project(client)
    row = create(client, project["id"])
    other = create_project(client)
    other_url = path(other["id"]) + "/" + row["id"]
    assert client.get(other_url).status_code == 404
    assert client.put(other_url, json={"name": "x", "expected_revision": 0}).status_code == 404
    assert (
        client.patch(other_url, json={"archived": True, "expected_revision": 0}).status_code == 404
    )
    member = register(client, f"{role}@example.com")
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role=role
                )
            )
    url = path(project["id"])
    assert client.get(url).status_code == (404 if role == "outsider" else 200)
    assert client.get(url + "/" + row["id"]).status_code == (404 if role == "outsider" else 200)
    expected = {"editor": 200, "viewer": 403, "outsider": 404}[role]
    renamed = rename(client, row)
    assert renamed.status_code == expected
    assert archive(client, renamed.json() if role == "editor" else row).status_code == expected
    assert client.post(url, json={"asset_type": "STYLE", "name": "墨"}).status_code == (
        201 if role == "editor" else expected
    )
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    client.patch(f"/api/v1/projects/{project['id']}", json={"archived": True})
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert rename(client, row).status_code == (
        404 if role == "outsider" else 409 if role == "editor" else 403
    )


def test_archived_deleted_project_and_missing_asset_gates(client):
    register(client)
    project = create_project(client)
    row = create(client, project["id"])
    url = path(project["id"])
    assert client.get(url + "/" + str(uuid4())).status_code == 404
    for query in ("limit=0", "limit=101", "offset=-1"):
        assert client.get(url + "?" + query).status_code == 422
    assert (
        client.patch(f"/api/v1/projects/{project['id']}", json={"archived": True}).status_code
        == 200
    )
    assert client.get(url).status_code == 200
    assert rename(client, row).status_code == 409
    assert archive(client, row).status_code == 409
    assert client.post(url, json={"asset_type": "PROP", "name": "灯"}).status_code == 409
    assert (
        client.request(
            "DELETE", f"/api/v1/projects/{project['id']}", json={"confirm_name": project["name"]}
        ).status_code
        == 200
    )
    assert client.get(url).status_code == 404
    assert client.get(url + "/" + row["id"]).status_code == 404
    assert rename(client, row).status_code == 404
    assert archive(client, row).status_code == 404
    assert client.post(url, json={"asset_type": "PROP", "name": "灯"}).status_code == 404


def test_archive_preserves_versions_selection_and_narrative_reference(client, episode):
    project_id = episode["project_id"]
    with client.app.state.session_factory.begin() as db:
        reference = make_asset(db, project_id, "CHARACTER")
        asset = db.get(VisualAsset, UUID(reference["asset_id"]))
        asset.current_version_id = UUID(reference["asset_version_id"])
        asset.lock_version = 7
    url = path(project_id) + "/" + reference["asset_id"]
    row = client.get(url).json()
    story_path = f"/api/v1/projects/{project_id}/story-entries"
    story = client.post(
        story_path, json={"kind": "CHARACTER", "name": "雨", "asset": reference}
    ).json()
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(reference["asset_version_id"]))
        before = {
            column.name: getattr(version, column.name) for column in version.__table__.columns
        }
    row = rename(client, row).json()
    archived = archive(client, row).json()
    assert archived["current_version_id"] == reference["asset_version_id"]
    assert archived["lock_version"] == 7
    assert client.get(story_path + "/" + story["id"]).json()["asset"] == reference
    assert (
        client.post(
            story_path, json={"kind": "CHARACTER", "name": "新角色", "asset": reference}
        ).json()["error"]["code"]
        == "ASSET_NOT_USABLE"
    )
    restored = archive(client, archived, False).json()
    assert (
        restored["status"] == "APPROVED"
        and restored["current_version_id"] == row["current_version_id"]
    )
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(reference["asset_version_id"]))
        assert {
            column.name: getattr(version, column.name) for column in version.__table__.columns
        } == before


def test_locked_asset_cannot_be_renamed_or_archived_and_legacy_types_stay_untouched(
    client, episode
):
    with client.app.state.session_factory.begin() as db:
        reference = make_asset(db, episode["project_id"], "CHARACTER")
        db.get(VisualAsset, UUID(reference["asset_id"])).status = "LOCKED"
        other = make_asset(db, episode["project_id"], "COSTUME")
    url = path(episode["project_id"])
    rows = client.get(url).json()
    assert len(rows) == 1
    assert rename(client, rows[0]).status_code == archive(client, rows[0]).status_code == 409
    assert client.get(url + "/" + other["asset_id"]).status_code == 404
    with client.app.state.session_factory() as db:
        assert db.get(VisualAsset, UUID(other["asset_id"])).asset_type == "COSTUME"


def test_migration_round_trip_preserves_existing_assets_and_guards_revision(
    client, episode, migrated
):
    # Use historical data so this ASSET-01 downgrade remains legal.
    with client.app.state.session_factory.begin() as db:
        legacy = VisualAsset(project_id=UUID(episode["project_id"]), asset_type="PROP", name="旧灯")
        db.add(legacy)
        db.flush()
        legacy_id = legacy.id
    row = client.get(path(episode["project_id"]) + "/" + str(legacy_id)).json()
    with client.app.state.session_factory.begin() as db:
        reference = make_asset(db, episode["project_id"], "CHARACTER")
        media = db.get(VisualAsset, UUID(reference["asset_id"]))
        media.current_version_id = UUID(reference["asset_version_id"])
        media.lock_version = 9
        db.flush()
        db.refresh(media)
        before = {
            cls: {column.name: getattr(item, column.name) for column in item.__table__.columns}
            for cls, item in (
                (VisualAsset, media),
                (AssetVersion, db.get(AssetVersion, media.current_version_id)),
            )
        }
    command.downgrade(migrated, "0011")
    command.upgrade(migrated, "head")
    command.check(migrated)
    with client.app.state.session_factory() as db:
        for cls, values in before.items():
            item = db.get(cls, values["id"])
            assert {
                column.name: getattr(item, column.name) for column in item.__table__.columns
            } == values
    assert client.get(path(row["project_id"]) + "/" + row["id"]).json() == row
    assert rename(client, row).status_code == 200
    with pytest.raises(RuntimeError, match="Asset directory history exists"):
        command.downgrade(migrated, "0011")
    command.upgrade(migrated, "head")
    assert client.get(path(row["project_id"]) + "/" + row["id"]).json()["revision"] == 1


def test_offline_downgrade_requires_preservation_check(migrated):
    config = Config(migrated.config_file_name, output_buffer=StringIO())
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    with pytest.raises(RuntimeError, match="online preservation check"):
        command.downgrade(config, "0012:0011", sql=True)


@pytest.mark.parametrize("mutation", [rename, archive])
def test_concurrent_directory_updates_have_one_winner_on_postgres(
    client, episode, settings, mutation
):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL row locking is verified in the dedicated CI database")
    row = create(client, episode["project_id"])
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(rename, client, row), executor.submit(mutation, client, row)]
        responses = [future.result() for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert client.get(path(row["project_id"]) + "/" + row["id"]).json()["revision"] == 1


def test_negative_directory_revision_rejected_by_database(client, episode):
    row = create(client, episode["project_id"])
    with pytest.raises(IntegrityError), client.app.state.session_factory.begin() as db:
        db.get(VisualAsset, UUID(row["id"])).revision = -1
    assert client.get(path(row["project_id"]) + "/" + row["id"]).json() == row
