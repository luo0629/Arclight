import hashlib
import os
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from test_asset_versions import draft, image_bytes

from arclight.persistence.models import StoredFile
from arclight.storage.objects import ObjectStorage, initialize


@pytest.fixture(params=["local", "s3"])
def files_setup(request, client, episode, tmp_path):
    settings = client.app.state.settings
    settings.asset_storage_path = tmp_path / "images"
    if request.param == "s3":
        endpoint = os.getenv("ARCLIGHT_TEST_S3_ENDPOINT")
        if not endpoint:
            pytest.skip("Requires isolated S3 test service")
        settings.object_storage_backend = "s3"
        settings.s3_endpoint_url = endpoint
        settings.s3_access_key = type(settings.s3_access_key)("arclight-test")
        settings.s3_secret_key = type(settings.s3_secret_key)("arclight-test-secret")
        settings.s3_bucket = "arclight-test-" + uuid4().hex
        initialize(settings)
    project_id = episode["project_id"]
    asset = client.post(
        f"/api/v1/projects/{project_id}/assets",
        json={"name": "file test", "asset_type": "SCENE"},
    )
    assert asset.status_code == 201, asset.text
    row = asset.json()
    url = f"/api/v1/projects/{project_id}/assets/{row['id']}/versions"
    yield settings, row, url, f"/api/v1/projects/{project_id}/files"
    if settings.object_storage_backend == "s3":
        store = ObjectStorage(settings)
        for entry in store.client.list_objects_v2(Bucket=settings.s3_bucket).get("Contents", []):
            store.delete(entry["Key"])
        store.client.delete_bucket(Bucket=settings.s3_bucket)


def test_file_receipt_exact_bytes_version_and_idempotency(client, files_setup):
    settings, asset, url, files_url = files_setup
    payload = draft(asset)
    response = client.post(url, json=payload)
    assert response.status_code == 201, response.text
    version = response.json()
    assert client.post(url, json=payload).json()["id"] == version["id"]
    rows = client.get(files_url).json()
    assert len(rows) == 1
    receipt = rows[0]
    assert receipt["asset_version_id"] == version["id"]
    assert receipt["checksum"] == hashlib.sha256(image_bytes()).hexdigest()
    assert receipt["byte_size"] == len(image_bytes())
    assert receipt["media_type"] == "image/png"
    assert receipt["source_note"] == payload["source_note"]
    assert receipt["rights_note"] == payload["rights_note"]
    assert receipt["rights_confirmed"] is True
    assert receipt["created_by"] == "service-owner"
    assert not {"object_key", "bucket", "storage_backend"}.intersection(receipt)
    assert client.get(files_url + "/" + receipt["id"]).json() == receipt
    assert client.get(url + "/" + version["id"] + "/image").content == image_bytes()
    assert client.get(files_url + "?limit=1&offset=1").json() == []
    other = client.post("/api/v1/projects", json={"name": "other"}).json()
    assert client.get(f"/api/v1/projects/{other['id']}/files/{receipt['id']}").status_code == 404
    with client.app.state.session_factory() as db:
        row = db.get(StoredFile, UUID(receipt["id"]))
        assert row.storage_backend == settings.object_storage_backend.upper()
        assert row.bucket == (settings.s3_bucket if row.storage_backend == "S3" else None)
        assert row.object_key.endswith(version["id"] + ".image")
    client.headers.pop("Authorization")
    assert client.get(files_url).status_code == 401


def test_restore_creates_new_file_keeps_original(client, files_setup):
    _, asset, url, files_url = files_setup
    first = client.post(url, json=draft(asset)).json()
    restored = client.post(
        url + "/" + first["id"] + "/restore-as-new",
        json={"creation_key": str(uuid4()), "expected_revision": 1, "comment": "new candidate"},
    )
    assert restored.status_code == 201, restored.text
    receipts = client.get(files_url).json()
    assert len(receipts) == 2
    assert {x["asset_version_id"] for x in receipts} == {first["id"], restored.json()["id"]}
    assert len({x["id"] for x in receipts}) == 2
    assert client.get(url + "/" + first["id"] + "/image").content == image_bytes()


def test_file_history_is_immutable_and_migration_refuses_loss(client, files_setup, migrated):
    _, asset, url, files_url = files_setup
    assert client.post(url, json=draft(asset)).status_code == 201
    for sql in ("UPDATE stored_files SET byte_size=1", "DELETE FROM stored_files"):
        with pytest.raises(IntegrityError), client.app.state.session_factory.begin() as db:
            db.execute(text(sql))
    if client.app.state.engine.dialect.name == "postgresql":
        with (
            pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(text("TRUNCATE stored_files CASCADE"))
    with pytest.raises(RuntimeError, match="Cannot remove saved"):
        command.downgrade(migrated, "0021")
    assert len(client.get(files_url).json()) == 1


def test_file_metadata_failure_rolls_back_version_and_object(client, files_setup, monkeypatch):
    from arclight.api import asset_versions

    settings, asset, url, files_url = files_setup

    def fail(*_args):
        from arclight.core.errors import DomainError

        raise DomainError("STORAGE_UNAVAILABLE", "文件登记失败", 503)

    monkeypatch.setattr(asset_versions, "record_asset_file", fail)
    assert client.post(url, json=draft(asset)).status_code == 503
    assert client.get(url).json() == []
    assert client.get(files_url).json() == []
    with client.app.state.session_factory() as db:
        assert db.scalar(select(StoredFile)) is None
    if settings.object_storage_backend == "s3":
        assert (
            ObjectStorage(settings).client.list_objects_v2(Bucket=settings.s3_bucket)["KeyCount"]
            == 0
        )
    else:
        assert list(settings.asset_storage_path.rglob("*.image")) == []


def test_switch_to_s3_keeps_local_exact_history_readable(client, files_setup):
    settings, asset, url, files_url = files_setup
    if settings.object_storage_backend != "s3":
        return
    settings.object_storage_backend = "local"
    first = client.post(url, json=draft(asset)).json()
    settings.object_storage_backend = "s3"
    assert client.get(url + "/" + first["id"] + "/image").content == image_bytes()
    restored = client.post(
        url + "/" + first["id"] + "/restore-as-new",
        json={"creation_key": str(uuid4()), "expected_revision": 1, "comment": "copy to S3"},
    )
    assert restored.status_code == 201, restored.text
    assert client.get(url + "/" + restored.json()["id"] + "/image").content == image_bytes()
    with client.app.state.session_factory() as db:
        rows = list(db.scalars(select(StoredFile)))
        assert {x.storage_backend for x in rows} == {"LOCAL", "S3"}
    assert len(client.get(files_url).json()) == 2
