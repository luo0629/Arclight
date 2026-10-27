"""ASSET-02 metadata, attribution, scope and preservation contracts."""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from test_assets import archive, create, path, rename
from test_collaboration import create_project, register
from test_text_contract_v2 import make_asset

from arclight.persistence.models import AssetVersion, ProjectMember, VisualAsset


def update_metadata(client, row, metadata=None, **extra):
    return client.put(
        path(row["project_id"]) + "/" + row["id"] + "/metadata",
        json={
            "metadata": metadata or {"description": "更正简介"},
            "expected_revision": row["revision"],
            **extra,
        },
    )


@pytest.mark.parametrize("kind", ["CHARACTER", "SCENE", "PROP", "STYLE"])
def test_metadata_round_trip_attribution_and_shared_revision(client, episode, kind):
    metadata = {
        "description": " 雨夜设定 ",
        "tags": [" 主角 ", "雨"],
        "appearance": {
            "age_range": "青年",
            "appearance": "圆脸",
            "hairstyle": "黑短发",
            "clothing": "蓝衣",
            "build": "瘦高",
            "expressions": "微笑",
            "colors": "蓝灰",
            "materials": "棉布",
        },
        "usage_scope": {"kind": "EPISODES", "episode_ids": [episode["id"]]},
    }
    response = client.post(
        path(episode["project_id"]),
        json={
            "asset_type": kind,
            "name": " 雨 ",
            "metadata": metadata,
            "creation_source": {"kind": "IMPORTED", "note": " 人工授权登记 "},
        },
    )
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["created_by"] == "service-owner"
    assert row["creation_source"] == {"kind": "IMPORTED", "note": "人工授权登记"}
    assert row["name"] == "雨" and row["metadata"]["tags"] == ["主角", "雨"]
    changed = update_metadata(client, row).json()
    assert changed["revision"] == 1 and changed["metadata"]["description"] == "更正简介"
    assert changed["metadata"]["usage_scope"]["kind"] == "PROJECT"
    assert changed["creation_source"] == row["creation_source"]
    assert rename(client, row).status_code == 409
    assert update_metadata(client, row).status_code == 409
    assert client.get(path(row["project_id"])).json() == [changed]
    assert client.get(path(row["project_id"]) + "/" + row["id"]).json() == changed


@pytest.mark.parametrize(
    "metadata",
    [
        {"tags": ["a", " a "]},
        {"tags": [" "]},
        {"tags": ["x"] * 21},
        {"tags": ["x" * 81]},
        {"description": "x" * 4001},
        {"appearance": {"freeform": "forbidden"}},
        {"unknown": True},
        {"appearance": {"age_range": "x" * 161}},
        {"usage_scope": {"kind": "EPISODES", "episode_ids": []}},
        {"usage_scope": {"kind": "PROJECT", "episode_ids": [str(uuid4())]}},
        {"usage_scope": {"kind": "ANYWHERE"}},
    ],
)
def test_invalid_metadata_has_no_side_effect(client, episode, metadata):
    row = create(client, episode["project_id"])
    assert update_metadata(client, row, metadata).status_code == 422
    assert client.get(path(row["project_id"]) + "/" + row["id"]).json() == row


def test_scope_validation_and_protected_creation_fields(client, episode):
    row = create(client, episode["project_id"])
    other = create_project(client)
    foreign = client.post(
        f"/api/v1/projects/{other['id']}/episodes", json={"episode_no": 1, "title": "他集"}
    ).json()
    for ids in ([str(uuid4())], [foreign["id"]], [episode["id"]] * 2):
        assert (
            update_metadata(
                client, row, {"usage_scope": {"kind": "EPISODES", "episode_ids": ids}}
            ).status_code
            == 422
        )
    for expected in (True, -1, "0", None):
        assert update_metadata(client, row, expected_revision=expected).status_code == 422
    for field in ("created_by", "creation_source", "asset_type"):
        assert update_metadata(client, row, **{field: "forged"}).status_code == 422
    assert (
        client.post(
            path(row["project_id"]),
            json={"name": "伪造", "asset_type": "PROP", "created_by": "other"},
        ).status_code
        == 422
    )
    assert client.get(path(row["project_id"]) + "/" + row["id"]).json() == row


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_metadata_permissions(client, role):
    register(client)
    project = create_project(client)
    row = create(client, project["id"])
    member = register(client, f"metadata-{role}@example.com")
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role=role
                )
            )
    assert (
        update_metadata(client, row).status_code
        == {"editor": 200, "viewer": 403, "outsider": 404}[role]
    )


def test_legacy_unknowns_and_media_preservation(client, episode):
    with client.app.state.session_factory.begin() as db:
        reference = make_asset(db, episode["project_id"], "CHARACTER")
        asset = db.get(VisualAsset, UUID(reference["asset_id"]))
        asset.current_version_id = UUID(reference["asset_version_id"])
        asset.lock_version = 4
        version = db.get(AssetVersion, asset.current_version_id)
        before = {
            column.name: getattr(version, column.name) for column in version.__table__.columns
        }
    row = client.get(path(episode["project_id"]) + "/" + reference["asset_id"]).json()
    assert row["created_by"] is None and row["creation_source"] is None
    updated = update_metadata(client, row).json()
    assert (
        updated["current_version_id"] == reference["asset_version_id"]
        and updated["lock_version"] == 4
    )
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(reference["asset_version_id"]))
        assert {
            column.name: getattr(version, column.name) for column in version.__table__.columns
        } == before
    archived = archive(client, updated).json()
    assert update_metadata(client, archived).status_code == 409
    with client.app.state.session_factory.begin() as db:
        db.get(VisualAsset, UUID(row["id"])).status = "LOCKED"
    assert update_metadata(client, updated).status_code == 409


def test_metadata_migration_refuses_data_loss(client, episode, migrated):
    row = create(client, episode["project_id"])
    with pytest.raises(RuntimeError, match="Asset metadata exists"):
        command.downgrade(migrated, "0012")
    assert client.get(path(row["project_id"]) + "/" + row["id"]).json() == row
    config = Config(migrated.config_file_name, output_buffer=StringIO())
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    with pytest.raises(RuntimeError, match="online preservation check"):
        command.downgrade(config, "0013:0012", sql=True)


def test_metadata_and_name_concurrent_cas(client, episode, settings):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL row locking is verified in the dedicated CI database")
    row = create(client, episode["project_id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [pool.submit(update_metadata, client, row), pool.submit(rename, client, row)]
        assert sorted(future.result().status_code for future in responses) == [200, 409]
