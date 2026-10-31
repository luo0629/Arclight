import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_collaboration import create_project, register
from test_file_transfers import upload_payload, upload_to_s3
from test_upload_completion import DECLARATION

from arclight.application import storage_cleanup as cleanup
from arclight.core.errors import DomainError
from arclight.persistence.models import (
    AssetSelectionLock,
    AssetVersion,
    StoredFile,
    UploadIntent,
    WorkflowRun,
)
from arclight.storage.cleanup import snapshot
from arclight.storage.objects import ObjectStorage, initialize


@pytest.fixture(params=["local", "s3"])
def gc_setup(request, client, episode, tmp_path, monkeypatch):
    settings = client.app.state.settings
    settings.asset_storage_path = tmp_path / "objects"
    settings.object_storage_backend = request.param
    if request.param == "s3":
        endpoint = os.getenv("ARCLIGHT_TEST_S3_ENDPOINT")
        if not endpoint:
            pytest.skip("Requires isolated S3 test service")
        settings.s3_endpoint_url = endpoint
        settings.s3_access_key = type(settings.s3_access_key)("arclight-test")
        settings.s3_secret_key = type(settings.s3_secret_key)("arclight-test-secret")
        settings.s3_bucket = "arclight-test-" + uuid4().hex
    initialize(settings)
    clock = [datetime.now(UTC)]
    monkeypatch.setattr(cleanup, "utcnow", lambda: clock[0])
    root = f"/api/v1/projects/{episode['project_id']}"
    yield settings, ObjectStorage(settings), root, clock
    if request.param == "s3":
        store = ObjectStorage(settings)
        if store.client.get_bucket_versioning(Bucket=settings.s3_bucket).get("Status"):
            for page in store.client.get_paginator("list_object_versions").paginate(
                Bucket=settings.s3_bucket
            ):
                for row in page.get("Versions", []) + page.get("DeleteMarkers", []):
                    store.client.delete_object(
                        Bucket=settings.s3_bucket, Key=row["Key"], VersionId=row["VersionId"]
                    )
        for page in store.client.get_paginator("list_objects_v2").paginate(
            Bucket=settings.s3_bucket
        ):
            for row in page.get("Contents", []):
                store.delete(row["Key"])
        store.client.delete_bucket(Bucket=settings.s3_bucket)


def make_key(settings, project_id, namespace="VALIDATED"):
    suffix = f"{uuid4()}.image" if namespace == "ASSET" else str(uuid4())
    return cleanup.prefix(settings, UUID(project_id), namespace) + suffix


def plan(client, root, namespace="VALIDATED", **values):
    response = client.post(root + "/storage-cleanups", json={"namespace": namespace, **values})
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def run_item(client, root, planned, item=None):
    item = item or planned["items"][0]
    return client.post(root + f"/storage-cleanups/{planned['id']}/items/{item['id']}/execute")


def receipt(client, project_id, key, *, asset_version_id=None):
    with client.app.state.session_factory.begin() as db:
        db.add(
            StoredFile(
                project_id=UUID(project_id),
                asset_version_id=asset_version_id,
                storage_backend="S3",
                bucket="test",
                object_key=key,
                checksum="a" * 64,
                byte_size=1,
                media_type="image/png",
                original_name="original.png",
                source_note="Original",
                rights_note="Original",
                rights_confirmed=True,
                created_by="service-owner",
            )
        )


def test_orphan_retention_observation_delete_and_idempotent_retry(client, gc_setup):
    settings, store, root, clock = gc_setup
    key = make_key(settings, root.split("/")[-1])
    store.put(key, b"orphan", "application/octet-stream")
    assert plan(client, root)["items"] == []
    clock[0] += timedelta(days=8)
    planned = plan(client, root)
    assert len(planned["items"]) == 1 and planned["items"][0]["reason"] == "ORPHAN"
    assert not {"object_key", "bucket", "next_cursor", "storage_identity"}.intersection(planned)
    assert "object_key" not in planned["items"][0]
    assert run_item(client, root, planned).status_code == 409
    assert snapshot(store, key) is not None
    clock[0] += timedelta(hours=1, seconds=1)
    response = run_item(client, root, planned)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "DELETED" and response.json()["attempt_count"] == 1
    assert snapshot(store, key) is None
    assert run_item(client, root, planned).json() == response.json()
    assert (
        client.get(root + f"/storage-cleanups/{planned['id']}").json()["items"][0]["status"]
        == "DELETED"
    )


def test_late_receipt_and_all_historical_locked_versions_survive(client, gc_setup):
    settings, store, root, clock = gc_setup
    project_id = root.split("/")[-1]
    late = make_key(settings, project_id)
    candidate = make_key(settings, project_id)
    for key in (late, candidate):
        store.put(key, b"retained", "image/png")
    receipt(client, project_id, candidate)
    asset = client.post(root + "/assets", json={"asset_type": "SCENE", "name": "history"}).json()
    with client.app.state.session_factory.begin() as db:
        version_id = uuid4()
        asset_key = cleanup.prefix(settings, UUID(project_id), "ASSET") + f"{version_id}.image"
        store.put(asset_key, b"locked", "image/png")
        db.add(
            AssetVersion(
                id=version_id,
                asset_id=UUID(asset["id"]),
                version_no=1,
                object_key=asset_key,
                checksum="b" * 64,
                metadata_json={},
                created_by="service-owner",
                status="LOCKED",
            )
        )
        db.flush()
        db.add(
            AssetSelectionLock(
                asset_id=UUID(asset["id"]),
                version_id=version_id,
                previous_status="APPROVED",
                created_by="service-owner",
            )
        )
    clock[0] += timedelta(days=8)
    assert plan(client, root, "ASSET")["items"] == []
    planned = plan(client, root)
    assert len(planned["items"]) == 1
    receipt(client, project_id, late)
    clock[0] += timedelta(hours=2)
    assert run_item(client, root, planned).json()["status"] == "RETAINED"
    assert all(snapshot(store, key) is not None for key in (late, candidate, asset_key))


def test_expired_upload_waits_for_completion_grace_and_recent_object(client, gc_setup):
    settings, store, root, clock = gc_setup
    project_id = UUID(root.split("/")[-1])
    upload_id = uuid4()
    key = f"temporary/{project_id}/{upload_id}"
    store.put(key, b"failed-upload", "image/png")
    with client.app.state.session_factory.begin() as db:
        db.add(
            UploadIntent(
                id=upload_id,
                project_id=project_id,
                bucket=settings.s3_bucket,
                object_key=key,
                purpose="REFERENCE_IMAGE",
                original_name="bad.png",
                declared_media_type="image/png",
                byte_size=13,
                checksum="a" * 64,
                expires_at=clock[0] + timedelta(minutes=5),
                created_by="service-owner",
            )
        )
    clock[0] += timedelta(days=1)
    assert plan(client, root, "TEMPORARY")["items"] == []
    clock[0] += timedelta(minutes=6)
    planned = plan(client, root, "TEMPORARY")
    assert planned["items"][0]["reason"] == "EXPIRED_UPLOAD"
    clock[0] += timedelta(hours=2)
    assert run_item(client, root, planned).json()["status"] == "DELETED"
    with client.app.state.session_factory() as db:
        assert db.get(UploadIntent, upload_id) is not None


def test_failure_diagnostic_retry_absence_and_changed_object(client, gc_setup, monkeypatch):
    settings, store, root, clock = gc_setup
    key = make_key(settings, root.split("/")[-1])
    store.put(key, b"old", "image/png")
    clock[0] += timedelta(days=8)
    planned = plan(client, root)
    clock[0] += timedelta(hours=2)
    original = cleanup.delete_snapshot

    def fail(*_args):
        raise DomainError("STORAGE_UNAVAILABLE", "temporary secret details", 503)

    monkeypatch.setattr(cleanup, "delete_snapshot", fail)
    result = run_item(client, root, planned).json()
    assert result["status"] == "FAILED" and result["error_code"] == "STORAGE_UNAVAILABLE"
    assert "secret" not in str(result)
    monkeypatch.setattr(cleanup, "delete_snapshot", original)
    store.delete(key)
    store.put(key, b"changed bytes", "image/png")
    changed = run_item(client, root, planned).json()
    assert changed["status"] == "FAILED" and changed["error_code"] == "CLEANUP_OBJECT_CHANGED"
    assert snapshot(store, key) is not None
    store.delete(key)
    result = run_item(client, root, planned).json()
    assert result["status"] == "ABSENT" and result["attempt_count"] == 3


def test_namespace_boundary_bounded_pages_plan_expiry_and_storage_change(
    client, gc_setup, tmp_path
):
    settings, store, root, clock = gc_setup
    project_id = root.split("/")[-1]
    keys = [make_key(settings, project_id) for _ in range(3)]
    foreign = make_key(settings, str(uuid4()))
    invalid = f"validated/{project_id}/not-a-server-uuid"
    for key in [*keys, foreign, invalid]:
        store.put(key, b"orphan", "image/png")
    clock[0] += timedelta(days=8)
    pages = [plan(client, root, limit=1)]
    while pages[-1]["has_more"]:
        pages.append(plan(client, root, limit=1, previous_plan_id=pages[-1]["id"]))
    items = [(p, item) for p in pages for item in p["items"]]
    assert len(items) == 3 and sum(p["scanned_count"] for p in pages) == 4
    bad = client.post(
        root + "/storage-cleanups", json={"namespace": "ASSET", "previous_plan_id": pages[0]["id"]}
    )
    assert bad.status_code == 409
    clock[0] += timedelta(hours=2)
    before = settings.s3_bucket if store.client else settings.asset_storage_path
    if store.client:
        settings.s3_bucket = "different-bucket"
    else:
        settings.asset_storage_path = tmp_path / "different"
    assert run_item(client, root, *items[0]).status_code == 409
    if store.client:
        settings.s3_bucket = before
    else:
        settings.asset_storage_path = before
    clock[0] += timedelta(days=1)
    assert run_item(client, root, *items[0]).status_code == 409
    assert snapshot(store, foreign) is not None and snapshot(store, invalid) is not None


def test_active_intermediates_retained_then_terminal_retention(client, gc_setup, episode):
    settings, store, root, clock = gc_setup
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        json={"story": "An original story"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    key = f"intermediate/{episode['project_id']}/{run_id}/{uuid4()}"
    store.put(key, b"render-intermediate", "application/octet-stream")
    clock[0] += timedelta(days=8)
    assert plan(client, root, "INTERMEDIATE")["items"] == []
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, UUID(run_id)).status = "FAILED"
    planned = plan(client, root, "INTERMEDIATE")
    assert planned["items"][0]["reason"] == "FINISHED_INTERMEDIATE"
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, UUID(run_id)).status = "QUEUED"
    clock[0] += timedelta(hours=2)
    assert run_item(client, root, planned).json()["status"] == "RETAINED"
    assert snapshot(store, key) is not None


def test_cleanup_target_history_immutable_and_migration_protected(client, gc_setup, migrated):
    settings, store, root, clock = gc_setup
    store.put(make_key(settings, root.split("/")[-1]), b"old", "image/png")
    clock[0] += timedelta(days=8)
    planned = plan(client, root)
    for sql in (
        "UPDATE cleanup_items SET object_key='other'",
        "DELETE FROM cleanup_items",
        "DELETE FROM cleanup_plans",
        "UPDATE cleanup_plans SET namespace='ASSET'",
    ):
        with pytest.raises(IntegrityError), client.app.state.session_factory.begin() as db:
            db.execute(text(sql))
    if client.app.state.engine.dialect.name == "postgresql":
        with (
            pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(text("TRUNCATE cleanup_plans CASCADE"))
    with pytest.raises(RuntimeError, match="Cannot remove saved cleanup"):
        command.downgrade(migrated, "0024")
    assert client.get(root + f"/storage-cleanups/{planned['id']}").status_code == 200


def test_concurrent_execution_claims_once(client, gc_setup):
    settings, store, root, clock = gc_setup
    key = make_key(settings, root.split("/")[-1])
    store.put(key, b"orphan", "image/png")
    clock[0] += timedelta(days=8)
    planned = plan(client, root)
    clock[0] += timedelta(hours=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_item(client, root, planned), range(2)))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert all(r.json()["status"] == "DELETED" and r.json()["attempt_count"] == 1 for r in results)
    assert snapshot(store, key) is None


def test_s3_replacement_before_deletion_is_retained(client, gc_setup, monkeypatch):
    settings, store, root, clock = gc_setup
    if store.client is None:
        pytest.skip("S3 conditional-delete integration")
    key = make_key(settings, root.split("/")[-1])
    store.put(key, b"old", "image/png")
    clock[0] += timedelta(days=8)
    planned = plan(client, root)
    clock[0] += timedelta(hours=2)
    original = cleanup.delete_snapshot

    def replace_before_deletion(current_store, expected):
        store.client.put_object(Bucket=settings.s3_bucket, Key=key, Body=b"new concurrent bytes")
        return original(current_store, expected)

    monkeypatch.setattr(cleanup, "delete_snapshot", replace_before_deletion)
    result = run_item(client, root, planned).json()
    assert result["status"] == "FAILED" and result["error_code"] == "CLEANUP_OBJECT_CHANGED"
    assert store.read(key, 1024) == b"new concurrent bytes"


def test_versioned_bucket_is_refused_before_scan_and_at_delete(client, gc_setup):
    settings, store, root, clock = gc_setup
    if store.client is None:
        pytest.skip("S3 versioning capability check")
    key = make_key(settings, root.split("/")[-1])
    store.put(key, b"old", "image/png")
    clock[0] += timedelta(days=8)
    planned = plan(client, root)
    clock[0] += timedelta(hours=2)
    store.client.put_bucket_versioning(
        Bucket=settings.s3_bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    assert (
        client.post(root + "/storage-cleanups", json={"namespace": "VALIDATED"}).status_code == 409
    )
    result = run_item(client, root, planned).json()
    assert result["status"] == "FAILED" and result["error_code"] == "CLEANUP_VERSIONED_BUCKET"
    assert snapshot(store, key) is not None


def test_owner_only_archive_revoke_cross_project_and_arbitrary_keys(client, tmp_path):
    settings = client.app.state.settings
    settings.asset_storage_path = tmp_path / "objects"
    initialize(settings)
    owner = register(client)
    project = create_project(client)
    root = f"/api/v1/projects/{project['id']}"
    planned = plan(client, root)
    outsider = register(client, "outsider@example.com")
    assert client.get(root + "/storage-cleanups").status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.post(
            root + "/members", json={"email": "outsider@example.com", "role": "editor"}
        ).status_code
        == 201
    )
    client.headers["Authorization"] = "Bearer " + outsider["access_token"]
    assert client.post(root + "/storage-cleanups", json={"namespace": "ASSET"}).status_code == 403
    assert client.get(root + f"/storage-cleanups/{planned['id']}").status_code == 403
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.post(
            root + "/storage-cleanups",
            json={"namespace": "ASSET", "object_key": "../../production"},
        ).status_code
        == 422
    )
    other = create_project(client)
    assert (
        client.get(f"/api/v1/projects/{other['id']}/storage-cleanups/{planned['id']}").status_code
        == 404
    )
    assert client.patch(root, json={"archived": True}).status_code == 200
    assert client.get(root + "/storage-cleanups").status_code == 200
    assert client.post(root + "/storage-cleanups", json={"namespace": "ASSET"}).status_code == 409
    assert client.patch(root, json={"archived": False}).status_code == 200
    assert client.delete(root + f"/members/{outsider['user']['id']}").status_code == 204
    client.headers["Authorization"] = "Bearer " + outsider["access_token"]
    assert client.get(root + "/storage-cleanups").status_code == 404
    client.headers.pop("Authorization")
    assert client.get(root + "/storage-cleanups").status_code == 401


def test_local_junction_escape_is_never_followed(client, gc_setup, tmp_path):
    settings, store, root, clock = gc_setup
    if store.client is not None:
        pytest.skip("Local path isolation")
    project_id = root.split("/")[-1]
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / str(uuid4())
    sentinel.write_bytes(b"must survive")
    parent = settings.asset_storage_path / "validated"
    parent.mkdir()
    link = parent / project_id
    if os.name == "nt":
        import subprocess

        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True
        )
        assert result.returncode == 0
    else:
        link.symlink_to(outside, target_is_directory=True)
    clock[0] += timedelta(days=8)
    assert (
        client.post(root + "/storage-cleanups", json={"namespace": "VALIDATED"}).status_code == 422
    )
    assert sentinel.read_bytes() == b"must survive"


@pytest.mark.parametrize("valid", [False, True])
def test_real_upload_cleanup_keeps_candidate_and_completion_receipt(
    client, gc_setup, valid, gateway_server
):
    settings, store, root, clock = gc_setup
    if store.client is None:
        pytest.skip("Actual S3 upload lifecycle")
    gateway_server(settings)
    data = None if valid else b"x" * upload_payload()["byte_size"]
    payload = upload_payload()
    if data is not None:
        payload["checksum"] = hashlib.sha256(data).hexdigest()
    ticket = client.post(root + "/uploads", json=payload).json()
    assert upload_to_s3(ticket, data=data).status_code == 204
    completion = root + f"/uploads/{ticket['upload_id']}/complete"
    response = client.post(completion, json=DECLARATION)
    assert response.status_code == (200 if valid else 422), response.text
    clock[0] += timedelta(days=2)
    planned = plan(client, root, "TEMPORARY")
    assert len(planned["items"]) == 1
    clock[0] += timedelta(hours=2)
    assert run_item(client, root, planned).json()["status"] == "DELETED"
    assert snapshot(store, f"temporary/{root.split('/')[-1]}/{ticket['upload_id']}") is None
    if valid:
        assert client.post(completion, json=DECLARATION).json() == response.json()
        clock[0] += timedelta(days=8)
        assert plan(client, root, "VALIDATED")["items"] == []
        assert client.get(root + f"/files/{response.json()['id']}").status_code == 200


def test_local_scan_is_bounded_and_never_deletes_during_scan(client, gc_setup):
    settings, store, root, clock = gc_setup
    if store.client is not None:
        pytest.skip("Local directory scan bound")
    folder = settings.asset_storage_path / "validated" / root.split("/")[-1]
    folder.mkdir(parents=True)
    for index in range(2001):
        (folder / str(index)).touch()
    clock[0] += timedelta(days=8)
    response = client.post(root + "/storage-cleanups", json={"namespace": "VALIDATED"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "CLEANUP_SCAN_LIMIT"
    assert len(list(folder.iterdir())) == 2001
