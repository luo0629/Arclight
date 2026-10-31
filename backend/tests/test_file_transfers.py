import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from sqlalchemy import select
from test_asset_versions import draft, image_bytes
from test_collaboration import create_project, register

from arclight.core.security import decode_token
from arclight.persistence.models import AuditEvent, AuthSession, Project, UploadIntent, User
from arclight.storage.objects import ObjectStorage, initialize


@pytest.fixture
def transfer_setup(client, episode, gateway_server):
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
    gateway_server(settings)
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
    headers = dict(ticket["headers"])
    if "Content-Type" in changed:
        headers["Content-Type"] = changed.pop("Content-Type")
    return httpx.post(
        ticket["url"],
        params=changed,
        headers=headers,
        content=image_bytes() if data is None else data,
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
    assert ticket["headers"] == {"Content-Type": "image/png"}
    assert ticket["body"] == "raw"
    assert "X-Amz" not in json.dumps(ticket) and "fields" not in ticket
    assert intent.object_key not in json.dumps(ticket)
    assert upload_to_s3(ticket).status_code == 204
    assert ObjectStorage(settings).read(intent.object_key, 1024, intent.checksum) == image_bytes()
    assert (
        httpx.get(
            f"{settings.s3_endpoint_url}/{settings.s3_bucket}/{intent.object_key}", trust_env=False
        ).status_code
        == 403
    )
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
    assert httpx.get(url + "?expires_in=300", trust_env=False).status_code == 403
    # SigV4 timestamps have whole-second precision; allow an initial valid GET.
    time.sleep(4)
    assert httpx.get(url, trust_env=False).status_code == 403


def test_upload_policy_expires(client, transfer_setup):
    _, root = transfer_setup
    ticket = client.post(root + "/uploads", json=upload_payload(expires_in=1)).json()
    time.sleep(2)
    assert upload_to_s3(ticket).status_code == 403


def test_internal_storage_capabilities_are_never_issued_to_clients(client, transfer_setup):
    settings, root = transfer_setup
    settings.s3_public_endpoint_url = "http://ignored-public-endpoint.invalid:9000"
    ticket = client.post(root + "/uploads", json=upload_payload()).json()
    assert ticket["url"].startswith(settings.file_transfer_base_url)
    assert "ignored-public" not in json.dumps(ticket)
    assert settings.s3_endpoint_url not in json.dumps(ticket)
    assert upload_to_s3(ticket).status_code == 204


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


def member_tickets(client):
    owner = register(client)
    project = create_project(client)
    root = f"/api/v1/projects/{project['id']}"
    receipt = mint_image(client, root)
    member = register(client, "editor@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.post(
            root + "/members", json={"email": "editor@example.com", "role": "editor"}
        ).status_code
        == 201
    )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    upload = client.post(root + "/uploads", json=upload_payload()).json()
    download = client.post(root + f"/files/{receipt['id']}/download-url", json={}).json()
    return owner, member, root, receipt, upload, download


@pytest.mark.parametrize(
    "revocation", ["remove", "downgrade", "logout", "disabled", "expired", "delete"]
)
def test_old_upload_and_download_tickets_are_revoked(client, transfer_setup, revocation):
    settings, _ = transfer_setup
    owner, member, root, receipt, upload, download = member_tickets(client)
    assert upload_to_s3(upload).status_code == 204
    assert httpx.get(download["url"], trust_env=False).content == image_bytes()
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    if revocation == "remove":
        assert client.delete(root + f"/members/{member['user']['id']}").status_code == 204
    elif revocation == "downgrade":
        assert (
            client.patch(
                root + f"/members/{member['user']['id']}", json={"role": "viewer"}
            ).status_code
            == 200
        )
    elif revocation == "logout":
        client.cookies.clear()
        client.headers["Authorization"] = "Bearer " + member["access_token"]
        assert client.post("/api/v1/auth/logout").status_code == 204
    else:
        with client.app.state.session_factory.begin() as db:
            if revocation == "disabled":
                db.get(User, UUID(member["user"]["id"])).disabled = True
            elif revocation == "expired":
                sid = decode_token(member["access_token"], settings, "access")["sid"]
                db.get(AuthSession, UUID(sid)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
            else:
                project = db.get(Project, UUID(root.split("/")[-1]))
                project.deleted_at = datetime.now(UTC)
                project.deletion_id = uuid4()
    # No Authorization header is supplied here: these are the exact previously issued URLs.
    assert upload_to_s3(upload).status_code == 403
    denied = httpx.get(download["url"], trust_env=False)
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "private, no-store"
    assert "X-Amz" not in denied.text and image_bytes() not in denied.content
    if revocation in {"remove", "downgrade"}:
        if revocation == "remove":
            assert (
                client.post(
                    root + "/members", json={"email": "editor@example.com", "role": "editor"}
                ).status_code
                == 201
            )
        else:
            assert (
                client.patch(
                    root + f"/members/{member['user']['id']}", json={"role": "editor"}
                ).status_code
                == 200
            )
        assert upload_to_s3(upload).status_code == 403
        assert httpx.get(download["url"], trust_env=False).status_code == 403
        client.headers["Authorization"] = "Bearer " + member["access_token"]
        fresh = client.post(root + f"/files/{receipt['id']}/download-url", json={}).json()
        assert httpx.get(fresh["url"], trust_env=False).content == image_bytes()


def test_action_tampering_service_rotation_and_archive(client, transfer_setup):
    settings, root = transfer_setup
    receipt = mint_image(client, root)
    upload = client.post(root + "/uploads", json=upload_payload()).json()
    download = client.post(root + f"/files/{receipt['id']}/download-url", json={}).json()
    assert httpx.get(upload["url"], trust_env=False).status_code == 403
    assert httpx.post(download["url"], content=b"x", trust_env=False).status_code == 403
    parts = download["url"].rsplit(".", 1)
    bad = parts[0] + "." + ("A" if parts[1][0] != "A" else "B") + parts[1][1:]
    assert httpx.get(bad, trust_env=False).status_code == 403
    assert client.patch(root, json={"archived": True}).status_code == 200
    assert upload_to_s3(upload).status_code == 403
    assert httpx.get(download["url"], trust_env=False).status_code == 200
    settings.api_token = type(settings.api_token)("new-development-token")
    assert httpx.get(download["url"], trust_env=False).status_code == 403


def test_large_raw_transfer_bypasses_api_body_limit(client, transfer_setup):
    import io
    import wave

    settings, root = transfer_setup
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * (3 * 1024 * 1024 // 2))
    data = output.getvalue()
    assert len(data) > settings.max_request_bytes
    upload = client.post(
        root + "/uploads",
        json=upload_payload(
            purpose="AUDIO",
            original_name="stream.wav",
            media_type="audio/wav",
            byte_size=len(data),
            checksum=hashlib.sha256(data).hexdigest(),
        ),
    ).json()
    assert upload_to_s3(upload, data=data).status_code == 204
    key = f"temporary/{root.split('/')[-1]}/{upload['upload_id']}"
    assert ObjectStorage(settings).read(key, len(data), hashlib.sha256(data).hexdigest()) == data


def test_private_authorization_cannot_be_called_by_a_user(client, transfer_setup):
    settings, root = transfer_setup
    upload = client.post(root + "/uploads", json=upload_payload()).json()
    token = upload["url"].rsplit("/", 1)[1]
    body = {"ticket": token, "method": "POST"}
    endpoint = "/internal/file-transfers/authorize"
    assert client.post(endpoint, json=body).status_code == 401
    response = client.post(
        endpoint,
        json=body,
        headers={"X-File-Gateway-Key": settings.file_gateway_secret.get_secret_value()},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["url"].startswith(settings.s3_endpoint_url)
