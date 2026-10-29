import base64
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from sqlalchemy import select
from test_asset_versions import draft, image_bytes
from test_collaboration import create_project, register

from arclight.persistence.models import AuditEvent, Project, UploadIntent
from arclight.storage.objects import ObjectStorage, initialize


@pytest.fixture
def transfer_setup(client, episode):
    endpoint = os.getenv("ARCLIGHT_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("Requires isolated S3 test service")
    settings = client.app.state.settings
    settings.object_storage_backend = "s3"
    settings.s3_endpoint_url = endpoint
    settings.s3_public_endpoint_url = endpoint
    settings.s3_access_key = type(settings.s3_access_key)("arclight-test")
    settings.s3_secret_key = type(settings.s3_secret_key)("arclight-test-secret")
    settings.s3_bucket = "arclight-test-" + uuid4().hex
    initialize(settings)
    root = f"/api/v1/projects/{episode['project_id']}"
    yield settings, root
    store = ObjectStorage(settings)
    for page in store.client.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket):
        for entry in page.get("Contents", []):
            store.delete(entry["Key"])
    store.client.delete_bucket(Bucket=settings.s3_bucket)


def upload_payload(**values):
    return {
        "purpose": "REFERENCE_IMAGE",
        "original_name": "scene.png",
        "media_type": "image/png",
        "byte_size": len(image_bytes()),
        "checksum": hashlib.sha256(image_bytes()).hexdigest(),
        **values,
    }


def upload_to_s3(ticket, data=None, **changed):
    return httpx.post(
        ticket["url"],
        data={**ticket["fields"], **changed},
        files={"file": ("untrusted-name.png", image_bytes() if data is None else data)},
        timeout=10,
        trust_env=False,
    )


def mint_image(client, root):
    asset = client.post(root + "/assets", json={"asset_type": "SCENE", "name": "download"}).json()
    assert (
        client.post(root + f"/assets/{asset['id']}/versions", json=draft(asset)).status_code == 201
    )
    return client.get(root + "/files").json()[0]


def test_exact_private_upload_policy_and_no_registration(client, transfer_setup):
    settings, root = transfer_setup
    response = client.post(root + "/uploads", json=upload_payload())
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    ticket = response.json()
    with client.app.state.session_factory() as db:
        intent = db.get(UploadIntent, UUID(ticket["upload_id"]))
        assert intent.object_key == f"temporary/{root.split('/')[-1]}/{intent.id}"
        assert intent.created_by == "service-owner" and intent.bucket == settings.s3_bucket
        assert intent.original_name == "scene.png"
        assert intent.expires_at.replace(tzinfo=UTC) > datetime.now(UTC)
        events = list(db.scalars(select(AuditEvent)))
        assert any(e.action == "file.upload_url_issued" for e in events)
        assert not any("X-Amz-Signature" in str(e.details_json) for e in events)
    policy = json.loads(base64.b64decode(ticket["fields"]["policy"]))
    assert {"key": intent.object_key} in policy["conditions"]
    assert ["content-length-range", len(image_bytes()), len(image_bytes())] in policy["conditions"]
    assert upload_to_s3(ticket).status_code == 204
    assert ObjectStorage(settings).read(intent.object_key, 1024, intent.checksum) == image_bytes()
    assert httpx.get(ticket["url"] + "/" + intent.object_key, trust_env=False).status_code == 403
    assert client.get(root + "/files").json() == []


def test_storage_rejects_changed_key_type_metadata_and_size(client, transfer_setup):
    settings, root = transfer_setup
    ticket = client.post(root + "/uploads", json=upload_payload()).json()
    for changed in (
        {"key": "temporary/another-project/another-file"},
        {"Content-Type": "text/html"},
        {"x-amz-meta-sha256": "0" * 64},
        {"acl": "public-read"},
    ):
        assert upload_to_s3(ticket, **changed).status_code in {400, 403}
    for data in (image_bytes() + b"extra", b"x"):
        assert upload_to_s3(ticket, data=data).status_code in {400, 403}
    assert (
        ObjectStorage(settings).client.list_objects_v2(Bucket=settings.s3_bucket)["KeyCount"] == 0
    )


def test_signed_download_exact_file_and_expiry(client, transfer_setup):
    _, root = transfer_setup
    receipt = mint_image(client, root)
    endpoint = root + f"/files/{receipt['id']}/download-url"
    response = client.post(endpoint, json={"expires_in": 3})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    url = response.json()["url"]
    downloaded = httpx.get(url, trust_env=False)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == image_bytes()
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert (
        httpx.get(url.replace("X-Amz-Expires=3", "X-Amz-Expires=300"), trust_env=False).status_code
        == 403
    )
    # SigV4 timestamps have whole-second precision; allow an initial valid GET.
    time.sleep(4)
    assert httpx.get(url, trust_env=False).status_code == 403


def test_upload_policy_expires(client, transfer_setup):
    _, root = transfer_setup
    ticket = client.post(root + "/uploads", json=upload_payload(expires_in=1)).json()
    time.sleep(2)
    assert upload_to_s3(ticket).status_code == 403


def test_public_endpoint_is_signed_without_internal_hostname(client, transfer_setup):
    settings, root = transfer_setup
    internal_endpoint = settings.s3_endpoint_url
    settings.s3_endpoint_url = "http://internal-only.invalid:9000"
    ticket = client.post(root + "/uploads", json=upload_payload()).json()
    assert ticket["url"].startswith(settings.s3_public_endpoint_url)
    assert "internal-only" not in ticket["url"]
    assert upload_to_s3(ticket).status_code == 204
    settings.s3_endpoint_url = internal_endpoint


def test_cross_project_and_invalid_capabilities_are_rejected(client, transfer_setup):
    _, root = transfer_setup
    receipt = mint_image(client, root)
    other = create_project(client)
    other_root = f"/api/v1/projects/{other['id']}"
    assert (
        client.post(other_root + f"/files/{receipt['id']}/download-url", json={}).status_code == 404
    )
    for extra in (
        {"key": "chosen/key"},
        {"object_key": "../escape"},
        {"bucket": "other"},
        {"expires_in": 301},
        {"expires_in": 0},
        {"expires_in": True},
        {"original_name": "../escape.png"},
        {"original_name": "bad\nname.png"},
        {"media_type": "text/html"},
        {"checksum": "invalid"},
        {"byte_size": 0},
    ):
        assert client.post(root + "/uploads", json=upload_payload(**extra)).status_code == 422
    assert (
        client.post(
            root + f"/files/{receipt['id']}/download-url", json={"object_key": "x"}
        ).status_code
        == 422
    )
    client.headers.pop("Authorization")
    assert client.post(root + "/uploads", json=upload_payload()).status_code == 401
    assert client.post(root + f"/files/{receipt['id']}/download-url", json={}).status_code == 401


def test_roles_archive_deletion_and_revocation_gate_minting(client, transfer_setup):
    _, _ = transfer_setup
    owner = register(client)
    project = create_project(client)
    root = f"/api/v1/projects/{project['id']}"
    receipt = mint_image(client, root)
    member = register(client, "viewer@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.post(
            root + "/members", json={"email": "viewer@example.com", "role": "viewer"}
        ).status_code
        == 201
    )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert client.post(root + "/uploads", json=upload_payload()).status_code == 403
    assert client.post(root + f"/files/{receipt['id']}/download-url", json={}).status_code == 200
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.patch(root + f"/members/{member['user']['id']}", json={"role": "editor"}).status_code
        == 200
    )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert client.post(root + "/uploads", json=upload_payload()).status_code == 201
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert client.delete(root + f"/members/{member['user']['id']}").status_code == 204
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert client.post(root + f"/files/{receipt['id']}/download-url", json={}).status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert client.patch(root, json={"archived": True}).status_code == 200
    assert client.post(root + "/uploads", json=upload_payload()).status_code == 409
    assert client.post(root + f"/files/{receipt['id']}/download-url", json={}).status_code == 200
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(project["id"])).deleted_at = datetime.now(UTC)
    assert client.post(root + "/uploads", json=upload_payload()).status_code == 404
    assert client.post(root + f"/files/{receipt['id']}/download-url", json={}).status_code == 404


def test_upload_intent_migration_protects_pending_work(client, transfer_setup, migrated):
    _, root = transfer_setup
    assert client.post(root + "/uploads", json=upload_payload()).status_code == 201
    with pytest.raises(RuntimeError, match="Cannot remove saved upload intents"):
        command.downgrade(migrated, "0022")


def test_local_mode_does_not_mint_fake_s3_urls(client, episode):
    root = f"/api/v1/projects/{episode['project_id']}"
    assert client.post(root + "/uploads", json=upload_payload()).status_code == 503
