"""Local uploads issue the same validated private receipt without S3 capabilities."""

import base64
import hashlib
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_collaboration import register
from test_native_media_integration import mp4_bytes

from arclight.application import local_video_uploads
from arclight.core.errors import DomainError
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    Project,
    ProjectMember,
    StoredFile,
    UploadIntent,
    User,
)
from arclight.storage.objects import ObjectStorage


@pytest.fixture(scope="module")
def video_data():
    return mp4_bytes()


def upload_body(data, **changes):
    return {
        "purpose": "VIDEO",
        "original_name": "manual.mp4",
        "media_type": "video/mp4",
        "byte_size": len(data),
        "checksum": hashlib.sha256(data).hexdigest(),
        "content_base64": base64.b64encode(data).decode(),
        "source_note": " Synthetic MP4 generated for tests ",
        "rights_note": " Authorized test media ",
        "rights_confirmed": True,
        **changes,
    }


def assert_no_upload(client, settings):
    with client.app.state.session_factory() as db:
        assert db.scalar(select(StoredFile)) is None
        assert db.scalar(select(UploadIntent)) is None
        assert db.scalar(select(AssetVersion)) is None
    assert not [path for path in settings.asset_storage_path.rglob("*") if path.is_file()]


def test_local_upload_has_exact_private_receipt_and_rights(client, episode, settings, video_data):
    project_id = episode["project_id"]
    root = f"/api/v1/projects/{project_id}"
    body = upload_body(video_data)
    ticket_body = {
        key: body[key]
        for key in ("purpose", "original_name", "media_type", "byte_size", "checksum")
    }
    unavailable = client.post(root + "/uploads", json=ticket_body)
    assert unavailable.status_code == 503, unavailable.text
    assert unavailable.json()["error"]["code"] == "PRESIGNED_UNAVAILABLE"
    response = client.post(root + "/uploads/local", json={**body, "expires_in": 120})
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    receipt = response.json()
    assert UUID(receipt["id"])
    assert receipt["project_id"] == project_id
    assert receipt["stage"] == "CANDIDATE" and receipt["asset_version_id"] is None
    for field in ("original_name", "media_type", "byte_size", "checksum"):
        assert receipt[field] == body[field]
    assert receipt["source_note"] == body["source_note"].strip()
    assert receipt["rights_note"] == body["rights_note"].strip()
    assert receipt["rights_confirmed"] is True
    assert receipt["created_by"] == "service-owner"
    for private in ("object_key", "storage_backend", "bucket", "content_base64"):
        assert private not in receipt
    content = client.get(root + f"/files/{receipt['id']}/content")
    assert content.status_code == 200 and content.content == video_data
    assert content.headers["content-type"] == "video/mp4"
    assert content.headers["cache-control"] == "private, no-store"
    assert client.get(root + f"/files/{receipt['id']}").json() == receipt
    assert client.get(root + "/files").json() == [receipt]
    rights = client.get(root + "/rights?category=VIDEO").json()
    assert len(rights) == 1 and rights[0]["id"] == receipt["id"]
    assert rights[0]["rights_status"] == "CONFIRMED"
    with client.app.state.session_factory() as db:
        row = db.get(StoredFile, UUID(receipt["id"]))
        assert row.storage_backend == "LOCAL" and row.bucket is None
        assert row.object_key == f"validated/{project_id}/{receipt['id']}"
        assert ObjectStorage(settings).read(row.object_key, None, row.checksum) == video_data
        assert db.scalar(select(UploadIntent)) is None
        assert db.scalar(select(AssetVersion)) is None
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "file.upload_validated"))
        assert event.resource_id == row.id and event.details_json == {"source": "LOCAL"}
    other = client.post("/api/v1/projects", json={"name": "other"}).json()["id"]
    assert client.get(f"/api/v1/projects/{other}/files/{receipt['id']}/content").status_code == 404
    register(client, "local-video-outsider@example.com")
    assert client.get(root + f"/files/{receipt['id']}/content").status_code == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"content_base64": "%%%"},
        {"content_base64": "AAAA\nAAAA"},
        {"byte_size": 1},
        {"checksum": "0" * 64},
        {"original_name": "../manual.mp4"},
        {"purpose": "GENERATED_VIDEO"},
        {"purpose": "REFERENCE_IMAGE", "media_type": "image/png", "original_name": "x.png"},
        {"source_note": " "},
        {"rights_note": " "},
        {"rights_confirmed": False},
        {"rights_confirmed": 1},
    ],
)
def test_invalid_local_declarations_never_register(client, episode, settings, video_data, changes):
    response = client.post(
        f"/api/v1/projects/{episode['project_id']}/uploads/local",
        json=upload_body(video_data, **changes),
    )
    assert response.status_code == 422, response.text
    assert_no_upload(client, settings)


@pytest.mark.parametrize("kind", ["fake", "truncated"])
def test_local_upload_decodes_the_actual_mp4(client, episode, settings, video_data, kind):
    data = b"not an MP4" if kind == "fake" else video_data[: len(video_data) // 2]
    response = client.post(
        f"/api/v1/projects/{episode['project_id']}/uploads/local", json=upload_body(data)
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_FILE"
    assert_no_upload(client, settings)


@pytest.mark.parametrize("authority", ["outsider", "viewer", "archived", "s3"])
def test_local_upload_enforces_project_and_backend(
    client, episode, settings, video_data, authority
):
    project_id = UUID(episode["project_id"])
    expected = {"outsider": 404, "viewer": 403, "archived": 409, "s3": 409}[authority]
    if authority in {"outsider", "viewer"}:
        user = register(client, "local-video-member@example.com")
        if authority == "viewer":
            with client.app.state.session_factory.begin() as db:
                db.add(
                    ProjectMember(
                        project_id=project_id, user_id=UUID(user["user"]["id"]), role="viewer"
                    )
                )
    elif authority == "archived":
        with client.app.state.session_factory.begin() as db:
            db.get(Project, project_id).archived = True
    else:
        settings.object_storage_backend = "s3"
    response = client.post(
        f"/api/v1/projects/{project_id}/uploads/local", json=upload_body(video_data)
    )
    assert response.status_code == expected, response.text
    if authority == "s3":
        assert response.json()["error"]["code"] == "LOCAL_UPLOAD_UNAVAILABLE"
    assert_no_upload(client, settings)


@pytest.mark.parametrize("authority", ["revoked", "viewer", "disabled"])
def test_local_upload_rechecks_authority_after_decode(
    client, episode, settings, video_data, monkeypatch, authority
):
    project_id = UUID(episode["project_id"])
    user = register(client, "local-video-editor@example.com")
    actor = UUID(user["user"]["id"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        db.add(ProjectMember(project_id=project_id, user_id=actor, role="editor"))
    real_validate = local_video_uploads.validate_file

    def revoke_after_decode(*args):
        result = real_validate(*args)
        with factory.begin() as db:
            member = db.scalar(select(ProjectMember).where(ProjectMember.user_id == actor))
            if authority == "revoked":
                db.delete(member)
            elif authority == "viewer":
                member.role = "viewer"
            else:
                db.get(User, actor).disabled = True
        return result

    monkeypatch.setattr(local_video_uploads, "validate_file", revoke_after_decode)
    response = client.post(
        f"/api/v1/projects/{project_id}/uploads/local", json=upload_body(video_data)
    )
    assert response.status_code == (404 if authority == "revoked" else 403), response.text
    assert_no_upload(client, settings)


@pytest.mark.parametrize("failure", ["registration", "partial_write", "existing"])
def test_failed_local_upload_cleans_only_its_own_bytes(
    client, episode, settings, video_data, monkeypatch, failure
):
    project_id = UUID(episode["project_id"])
    identity = uuid4()
    key = f"validated/{project_id}/{identity}"
    store = ObjectStorage(settings)
    if failure == "registration":

        def failed_audit(*args):
            raise DomainError("AUDIT_UNAVAILABLE", "try again", 503)

        monkeypatch.setattr(local_video_uploads, "audit", failed_audit)
    elif failure == "partial_write":
        real_put = ObjectStorage.put

        def partial_put(self, key, data, media_type):
            real_put(self, key, data[:4], media_type)
            raise DomainError("STORAGE_UNAVAILABLE", "try again", 503)

        monkeypatch.setattr(ObjectStorage, "put", partial_put)
    else:
        store.put(key, b"earlier immutable object", "video/mp4")
        monkeypatch.setattr(local_video_uploads, "uuid4", lambda: identity)
    response = client.post(
        f"/api/v1/projects/{project_id}/uploads/local", json=upload_body(video_data)
    )
    assert response.status_code == (409 if failure == "existing" else 503), response.text
    if failure == "existing":
        assert response.json()["error"]["code"] == "OBJECT_EXISTS"
        assert store.read(key, None) == b"earlier immutable object"
        with client.app.state.session_factory() as db:
            assert db.scalar(select(StoredFile)) is None
    else:
        assert_no_upload(client, settings)


def test_local_upload_keeps_the_configured_request_limit(client, episode, settings):
    response = client.post(
        f"/api/v1/projects/{episode['project_id']}/uploads/local",
        json=upload_body(b"small", content_base64="A" * settings.max_request_bytes),
    )
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
    assert_no_upload(client, settings)


def test_later_transaction_rollback_cannot_delete_a_committed_local_video(
    client, episode, settings, video_data
):
    body = upload_body(video_data)
    with client.app.state.session_factory() as db:
        receipt = local_video_uploads.freeze_local_video(
            db,
            settings,
            UUID(episode["project_id"]),
            "service-owner",
            original_name=body["original_name"],
            byte_size=body["byte_size"],
            checksum=body["checksum"],
            content_base64=body["content_base64"],
            declaration={
                "source_note": body["source_note"].strip(),
                "rights_note": body["rights_note"].strip(),
                "rights_confirmed": True,
            },
        )
        identity, key = receipt.id, receipt.object_key
        db.commit()
        # Reusing this Session starts a separate transaction whose rollback must
        # not run the earlier upload's cleanup against committed immutable bytes.
        assert db.scalar(select(StoredFile).where(StoredFile.id == identity)) is not None
        db.rollback()
        assert db.get(StoredFile, identity) is not None
        assert ObjectStorage(settings).read(key, None, body["checksum"]) == video_data
