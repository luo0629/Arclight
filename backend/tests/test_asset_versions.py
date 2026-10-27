"""ASSET-05 real local bytes, immutable contents, lifecycle and history boundaries."""

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO, StringIO
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from PIL import Image
from sqlalchemy import func, select
from test_assets import archive, create, path, rename
from test_collaboration import create_project, register
from test_text_contract_v2 import board_content, make_asset, save_board, save_script

from arclight.persistence.models import AssetVersion, AuditEvent, ProjectMember, VisualAsset


@pytest.fixture(autouse=True)
def private_objects(settings, tmp_path):
    settings.asset_storage_path = tmp_path / "objects"
    return settings.asset_storage_path


def image_bytes(kind="PNG", size=(8, 12), color="blue"):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format=kind)
    return output.getvalue()


def versions(row):
    return path(row["project_id"]) + "/" + row["id"] + "/versions"


def draft(row, **values):
    return {
        "creation_key": str(uuid4()),
        "expected_revision": row["revision"],
        "original_name": "scene.png",
        "media_type": "image/png",
        "content_base64": base64.b64encode(image_bytes()).decode(),
        "source_note": "独立测试生成图片",
        "rights_note": "自行创建测试图片",
        "rights_confirmed": True,
        **values,
    }


def save(client, row, **values):
    response = client.post(versions(row), json=draft(row, **values))
    assert response.status_code == 201, response.text
    return response.json()


def state(client, row, version, status, comment="验收说明"):
    return client.patch(
        versions(row) + "/" + version["id"] + "/state",
        json={
            "expected_revision": version["revision"],
            "status": status,
            "comment": comment,
        },
    )


def test_real_image_versions_are_append_only_and_exact(client, episode, private_objects):
    row = create(client, episode["project_id"], "SCENE")
    payload = draft(row)
    first = client.post(versions(row), json=payload).json()
    assert first["version_no"] == 1 and first["status"] == "DRAFT"
    assert first["revision"] == 0 and not first["stale"]
    assert first["created_by"] == "service-owner"
    assert first["checksum"] == hashlib.sha256(image_bytes()).hexdigest()
    assert first["media"]["width"] == 8 and first["media"]["height"] == 12
    assert first["asset_snapshot"]["name"] == row["name"]
    assert "object_key" not in first and "request_sha256" not in str(first)
    assert client.post(versions(row), json=payload).json() == first
    assert (
        client.post(versions(row), json={**payload, "source_note": "different"}).status_code == 409
    )
    image = client.get(versions(row) + "/" + first["id"] + "/image")
    assert image.content == image_bytes() and image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "private, no-store"
    row = client.get(path(row["project_id"]) + "/" + row["id"]).json()
    assert row["revision"] == 1 and row["current_version_id"] is None
    row = rename(client, row, "第二版场景").json()
    second = save(client, row, content_base64=base64.b64encode(image_bytes(color="red")).decode())
    assert second["version_no"] == 2 and second["id"] != first["id"]
    assert second["asset_snapshot"]["name"] == "第二版场景"
    assert client.get(versions(row) + "/" + first["id"]).json() == first
    assert client.get(versions(row) + "?limit=1").json() == [first]
    assert client.get(versions(row) + "?limit=1&offset=1").json() == [second]
    assert (
        client.put(versions(row) + "/" + first["id"], json={"checksum": "changed"}).status_code
        == 405
    )
    assert client.delete(versions(row) + "/" + first["id"]).status_code == 405
    assert len(list(private_objects.rglob("*.image"))) == 2


def test_explicit_review_cas_and_archive_restore(client, episode):
    row = create(client, episode["project_id"])
    first = save(client, row)
    assert state(client, row, first, "APPROVED").status_code == 409
    ready = state(client, row, first, "READY_FOR_REVIEW").json()
    assert ready["status"] == "READY_FOR_REVIEW" and ready["revision"] == 1
    assert state(client, row, first, "READY_FOR_REVIEW").status_code == 409
    approved = state(client, row, ready, "APPROVED").json()
    with client.app.state.session_factory() as db:
        saved_row = db.get(AssetVersion, UUID(first["id"]))
        immutable_before = {
            key: getattr(saved_row, key)
            for key in (
                "id",
                "asset_id",
                "version_no",
                "object_key",
                "checksum",
                "metadata_json",
                "created_by",
                "created_at",
                "creation_key",
            )
        }
    archived = state(client, row, approved, "ARCHIVED").json()
    assert archived["status"] == "ARCHIVED" and archived["archived_from_status"] == "APPROVED"
    assert client.get(versions(row) + "/" + first["id"] + "/image").status_code == 200
    restored = state(client, row, archived, "RESTORE").json()
    assert restored["status"] == "APPROVED" and restored["archived_from_status"] is None
    assert state(client, row, restored, "STALE", "").status_code == 422
    stale = state(client, row, restored, "STALE").json()
    assert stale["status"] == "STALE" and stale["stale"]
    again = state(client, row, stale, "ARCHIVED").json()
    assert state(client, row, again, "RESTORE").json()["status"] == "STALE"
    assert (
        client.get(path(row["project_id"]) + "/" + row["id"]).json()["current_version_id"] is None
    )
    with client.app.state.session_factory() as db:
        persisted = db.get(AssetVersion, UUID(first["id"]))
        assert persisted.checksum == first["checksum"]
        assert {key: getattr(persisted, key) for key in immutable_before} == immutable_before
        events = db.scalars(select(AuditEvent).where(AuditEvent.resource_id == persisted.id)).all()
        assert {event.action for event in events} == {
            "asset.version_created",
            "asset.version_state_changed",
        }
        assert events[-1].details_json["comment"] == "验收说明"


@pytest.mark.parametrize(
    "values",
    [
        {"content_base64": "invalid!"},
        {"content_base64": "YQ=="},
        {"media_type": "image/jpeg"},
        {"media_type": "image/svg+xml"},
        {"rights_confirmed": False},
        {"rights_confirmed": 1},
        {"rights_note": " "},
        {"original_name": "../secret.png"},
        {"expected_revision": True},
        {"expected_revision": "0"},
        {"status": "APPROVED"},
        {"object_key": "arbitrary"},
        {"created_by": "other"},
        {"current_version_id": str(uuid4())},
    ],
)
def test_invalid_import_has_no_version_or_objects(client, episode, private_objects, values):
    row = create(client, episode["project_id"])
    assert client.post(versions(row), json=draft(row, **values)).status_code == 422
    assert client.get(versions(row)).json() == []
    assert not list(private_objects.rglob("*.image"))
    assert client.get(path(row["project_id"]) + "/" + row["id"]).json()["revision"] == 0


def test_image_bounds_and_jpeg(client, episode):
    row = create(client, episode["project_id"])
    animated = BytesIO()
    Image.new("RGB", (8, 12), "blue").save(
        animated, format="PNG", save_all=True, append_images=[Image.new("RGB", (8, 12), "red")]
    )
    for data in (
        image_bytes(size=(4097, 1)),
        image_bytes(size=(2001, 2000)),
        image_bytes()[:-12],
        b"x" * (1024 * 1024 + 1),
        animated.getvalue(),
    ):
        assert (
            client.post(
                versions(row), json=draft(row, content_base64=base64.b64encode(data).decode())
            ).status_code
            == 422
        )
    version = save(
        client,
        row,
        media_type="image/jpeg",
        original_name="scene.jpg",
        content_base64=base64.b64encode(image_bytes("JPEG")).decode(),
    )
    assert version["media"]["media_type"] == "image/jpeg"


@pytest.mark.parametrize(
    "values",
    [
        {"expected_revision": True},
        {"expected_revision": "0"},
        {"expected_revision": -1},
        {"status": "LOCKED"},
        {"status": "DRAFT"},
        {"status": "APPROVED", "object_key": "overwrite"},
    ],
)
def test_state_input_cannot_overwrite_content_or_lock(client, episode, values):
    row = create(client, episode["project_id"])
    version = save(client, row)
    response = client.patch(
        versions(row) + "/" + version["id"] + "/state",
        json={"expected_revision": 0, "status": "READY_FOR_REVIEW", **values},
    )
    assert response.status_code == 422
    assert client.get(versions(row) + "/" + version["id"]).json() == version


def test_current_and_asset_lock_guards_and_cross_project(client, episode):
    row = create(client, episode["project_id"])
    version = save(client, row)
    other_project = create_project(client)
    foreign = {**row, "project_id": other_project["id"]}
    assert client.get(versions(foreign)).status_code == 404
    assert client.get(versions(foreign) + "/" + version["id"] + "/image").status_code == 404
    assert client.post(versions(foreign), json=draft(row)).status_code == 404
    assert state(client, foreign, version, "READY_FOR_REVIEW").status_code == 404
    with client.app.state.session_factory.begin() as db:
        asset = db.get(VisualAsset, UUID(row["id"]))
        asset.current_version_id = UUID(version["id"])
    assert state(client, row, version, "ARCHIVED").status_code == 409
    row = client.get(path(row["project_id"]) + "/" + row["id"]).json()
    with client.app.state.session_factory.begin() as db:
        db.get(VisualAsset, UUID(row["id"])).status = "LOCKED"
    assert client.post(versions(row), json=draft(row)).status_code == 409
    assert state(client, row, version, "READY_FOR_REVIEW").status_code == 409
    assert client.get(versions(row) + "/" + version["id"] + "/image").status_code == 200


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_member_read_create_and_owner_review_gates(client, role):
    register(client)
    project = create_project(client)
    row = create(client, project["id"])
    version = save(client, row)
    ready = state(client, row, version, "READY_FOR_REVIEW").json()
    row = client.get(path(project["id"]) + "/" + row["id"]).json()
    member = register(client, role + "@example.com")
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role=role
                )
            )
    expected_read = 404 if role == "outsider" else 200
    for suffix in ("", "/" + version["id"], "/" + version["id"] + "/image"):
        assert client.get(versions(row) + suffix).status_code == expected_read
    assert state(client, row, ready, "APPROVED").status_code == (404 if role == "outsider" else 403)
    assert (
        client.post(versions(row), json=draft(row)).status_code
        == {"editor": 201, "viewer": 403, "outsider": 404}[role]
    )


def test_missing_bytes_storage_and_rollback_are_honest(
    client, episode, settings, private_objects, monkeypatch
):
    row = create(client, episode["project_id"])
    settings.asset_storage_path = None
    assert client.post(versions(row), json=draft(row)).status_code == 503
    assert client.get(versions(row)).json() == []
    settings.asset_storage_path = private_objects
    from arclight.api import asset_versions

    real_audit = asset_versions.audit

    def fail_audit(*args, **kwargs):
        from arclight.core.errors import DomainError

        raise DomainError("TEST_ROLLBACK", "rollback", 409)

    monkeypatch.setattr(asset_versions, "audit", fail_audit)
    assert client.post(versions(row), json=draft(row)).status_code == 409
    assert not list(private_objects.rglob("*.image"))
    monkeypatch.setattr(asset_versions, "audit", real_audit)
    version = save(client, row)
    file = next(private_objects.rglob("*.image"))
    file.write_bytes(b"tampered")
    assert client.get(versions(row) + "/" + version["id"] + "/image").status_code == 409
    assert state(client, row, version, "READY_FOR_REVIEW").status_code == 409
    assert client.get(versions(row) + "/" + version["id"]).json()["status"] == "DRAFT"


def test_archive_project_asset_version_and_history_survive(client, episode):
    row = create(client, episode["project_id"], "SCENE")
    version = save(client, row)
    ready = state(client, row, version, "READY_FOR_REVIEW").json()
    approved = state(client, row, ready, "APPROVED").json()
    from test_scene_prop_profiles import save_profile

    reference = {"asset_id": row["id"], "asset_version_id": version["id"]}
    row = client.get(path(row["project_id"]) + "/" + row["id"]).json()
    row = save_profile(client, row, {"reference": reference}).json()
    script = save_script(client, episode)
    content = board_content(script)
    first_shot = content["shots"][0]
    first_shot["duration_ms"] = 15000
    first_shot["scene_asset"] = reference
    second_shot = deepcopy(first_shot)
    second_shot["shot_no"] = 2
    content["shots"].append(second_shot)
    board = save_board(client, episode, script, content)
    assert board.status_code == 201, board.text
    archived = state(client, row, approved, "ARCHIVED").json()
    historical = client.get(f"/api/v1/episodes/{episode['id']}/storyboards").json()[0]
    assert [shot["scene_asset"] for shot in historical["content_json"]["shots"]] == [
        reference,
        reference,
    ]
    assert (
        client.get(path(row["project_id"]) + "/" + row["id"]).json()["scene_profile"]["reference"]
        == reference
    )
    assert save_profile(client, row, {"reference": reference}).status_code == 422
    row = archive(client, row).json()
    assert client.get(versions(row)).json() == [archived]
    assert state(client, row, archived, "RESTORE").status_code == 409
    assert client.post(versions(row), json=draft(row)).status_code == 409
    assert client.get(versions(row) + "/" + version["id"] + "/image").status_code == 200
    client.patch(f"/api/v1/projects/{row['project_id']}", json={"archived": True})
    assert client.get(versions(row)).status_code == 200
    assert state(client, row, archived, "RESTORE").status_code == 409
    client.request(
        "DELETE", f"/api/v1/projects/{row['project_id']}", json={"confirm_name": "测试漫剧"}
    )
    assert client.get(versions(row)).status_code == 404
    assert client.get(versions(row) + "/" + version["id"] + "/image").status_code == 404


def test_legacy_locked_rejected_and_exact_identity(client, episode):
    row = create(client, episode["project_id"])
    first = save(client, row)
    ready = state(client, row, first, "READY_FOR_REVIEW").json()
    rejected = state(client, row, ready, "REJECTED").json()
    assert rejected["status"] == "REJECTED"
    assert state(client, row, rejected, "APPROVED").status_code == 409
    other = create(client, episode["project_id"], "PROP")
    assert client.get(versions(other) + "/" + first["id"]).status_code == 404
    assert client.get(versions(other) + "/" + first["id"] + "/image").status_code == 404
    with client.app.state.session_factory.begin() as db:
        legacy = make_asset(db, episode["project_id"], "CHARACTER")
        db.get(AssetVersion, UUID(legacy["asset_version_id"])).status = "LOCKED"
    legacy_asset = client.get(path(episode["project_id"]) + "/" + legacy["asset_id"]).json()
    locked = client.get(versions(legacy_asset)).json()[0]
    assert locked["status"] == "LOCKED" and locked["media"] is None
    assert state(client, legacy_asset, locked, "ARCHIVED").status_code == 409
    assert client.get(versions(legacy_asset) + "/" + locked["id"] + "/image").status_code == 409
    for query in ("limit=0", "limit=101", "offset=-1"):
        assert client.get(versions(row) + "?" + query).status_code == 422


def test_version_migration_roundtrip_and_data_guard(client, episode, migrated):
    row = create(client, episode["project_id"])
    command.downgrade(migrated, "0015")
    command.upgrade(migrated, "head")
    command.check(migrated)
    save(client, row)
    with pytest.raises(RuntimeError, match="Asset version lifecycle data exist"):
        command.downgrade(migrated, "0015")
    assert len(client.get(versions(row)).json()) == 1
    config = Config(migrated.config_file_name, output_buffer=StringIO())
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    with pytest.raises(RuntimeError, match="online preservation check"):
        command.downgrade(config, "0016:0015", sql=True)


def test_parallel_creation_and_review_are_cas_serialized(
    client, episode, settings, private_objects
):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL locks verified in isolated CI database")
    row = create(client, episode["project_id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(client.post, versions(row), json=draft(row)) for _ in range(2)]
        assert sorted(call.result().status_code for call in calls) == [201, 409]
    first = client.get(versions(row)).json()[0]
    ready = state(client, row, first, "READY_FOR_REVIEW").json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(state, client, row, ready, status) for status in ("APPROVED", "REJECTED")
        ]
        assert sorted(call.result().status_code for call in calls) == [200, 409]
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AssetVersion)
                .where(AssetVersion.asset_id == UUID(row["id"]))
            )
            == 1
        )
        assert db.get(VisualAsset, UUID(row["id"])).current_version_id is None
    assert len(list(private_objects.rglob("*.image"))) == 1
