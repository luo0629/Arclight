import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from test_asset_versions import image_bytes
from test_file_transfers import transfer_setup, upload_payload, upload_to_s3

from arclight.persistence.models import (
    AssetVersion,
    FileRegistration,
    StoredFile,
    UploadCompletion,
    UploadIntent,
)
from arclight.storage.objects import ObjectStorage

# Imported fixtures are shared intentionally with real S3 transfer tests.
__all__ = ["transfer_setup"]
DECLARATION = {
    "source_note": "test-created image",
    "rights_note": "owned test bytes",
    "rights_confirmed": True,
}


def ticket(client, root, data=None, **values):
    raw = image_bytes() if data is None else data
    payload = upload_payload(byte_size=len(raw), checksum=hashlib.sha256(raw).hexdigest(), **values)
    response = client.post(root + "/uploads", json=payload)
    assert response.status_code == 201, response.text
    result = response.json()
    assert upload_to_s3(result, data=raw).status_code == 204
    return result


def complete(client, root, upload):
    return client.post(root + f"/uploads/{upload['upload_id']}/complete", json=DECLARATION)


def test_temporary_validated_and_registered_exact_stages(client, transfer_setup):
    settings, root = transfer_setup
    upload = ticket(client, root)
    path = root + f"/uploads/{upload['upload_id']}"
    assert client.get(path).json()["stage"] == "TEMPORARY"
    assert client.get(root + "/files").json() == []
    result = complete(client, root, upload)
    assert result.status_code == 200, result.text
    candidate = result.json()
    assert candidate["stage"] == "CANDIDATE" and candidate["asset_version_id"] is None
    assert complete(client, root, upload).json()["id"] == candidate["id"]
    assert client.get(path).json()["stage"] == "VALIDATED"
    assert client.get(path).json()["file_id"] == candidate["id"]
    assert (
        client.post(path + "/complete", json={**DECLARATION, "source_note": "changed"}).status_code
        == 409
    )
    asset = client.post(root + "/assets", json={"asset_type": "SCENE", "name": "registered"}).json()
    payload = {
        "asset_id": asset["id"],
        "creation_key": str(uuid4()),
        "expected_revision": asset["revision"],
    }
    endpoint = root + f"/files/{candidate['id']}/asset-versions"
    registered = client.post(endpoint, json=payload)
    assert registered.status_code == 201, registered.text
    assert client.post(endpoint, json=payload).json()["id"] == registered.json()["id"]
    assert registered.json()["status"] == "DRAFT"
    receipts = client.get(root + "/files").json()
    assert {r["stage"] for r in receipts} == {"REGISTERED", "CANDIDATE"}
    with client.app.state.session_factory() as db:
        rows = list(db.scalars(select(StoredFile)))
        assert len({r.object_key for r in rows}) == 2
        assert all(r.object_key != upload["fields"]["key"] for r in rows)
        link = db.scalar(select(FileRegistration))
        assert str(link.file_id) == candidate["id"]
        assert str(link.asset_version_id) == registered.json()["id"]
    # The still-valid upload policy cannot replace either validated or asset bytes.
    assert upload_to_s3(upload, data=b"x" * len(image_bytes())).status_code == 204
    for receipt in receipts:
        with client.app.state.session_factory() as db:
            saved = db.get(StoredFile, UUID(receipt["id"]))
            assert (
                ObjectStorage(settings).read(saved.object_key, 1024, saved.checksum)
                == image_bytes()
            )


@pytest.mark.parametrize("data", [b"not an image", image_bytes()[:-16]])
def test_invalid_bytes_never_register_file_or_asset(client, transfer_setup, data):
    _, root = transfer_setup
    upload = ticket(client, root, data)
    assert complete(client, root, upload).status_code == 422
    assert client.get(root + "/files").json() == []
    with client.app.state.session_factory() as db:
        assert db.scalar(select(StoredFile)) is None
        assert db.scalar(select(AssetVersion)) is None
        assert db.scalar(select(UploadCompletion)) is None


def test_missing_hash_mismatch_expired_and_cross_project_are_rejected(client, transfer_setup):
    _, root = transfer_setup
    upload = client.post(root + "/uploads", json=upload_payload()).json()
    assert complete(client, root, upload).status_code == 409
    assert upload_to_s3(upload, data=b"x" * len(image_bytes())).status_code == 204
    assert complete(client, root, upload).status_code == 409
    other = client.post("/api/v1/projects", json={"name": "other"}).json()
    other_root = f"/api/v1/projects/{other['id']}"
    assert complete(client, other_root, upload).status_code == 404
    assert client.get(other_root + f"/uploads/{upload['upload_id']}").status_code == 404
    with client.app.state.session_factory.begin() as db:
        db.get(UploadIntent, UUID(upload["upload_id"])).expires_at = datetime.now(UTC) - timedelta(
            days=2
        )
    assert client.get(root + f"/uploads/{upload['upload_id']}").json()["stage"] == "EXPIRED"
    assert complete(client, root, upload).status_code == 409
    assert client.get(root + "/files").json() == []


def test_completion_failure_rolls_back_receipt_and_frozen_object(
    client, transfer_setup, monkeypatch
):
    settings, root = transfer_setup
    upload = ticket(client, root)
    from arclight.api import upload_completion
    from arclight.core.errors import DomainError

    def fail(*_args):
        raise DomainError("TEST_FAILURE", "failure after freeze", 503)

    monkeypatch.setattr(upload_completion, "audit", fail)
    assert complete(client, root, upload).status_code == 503
    assert client.get(root + "/files").json() == []
    contents = ObjectStorage(settings).client.list_objects_v2(Bucket=settings.s3_bucket)["Contents"]
    assert [c["Key"] for c in contents] == [upload["fields"]["key"]]


def test_completion_receipts_immutable_and_downgrade_guarded(client, transfer_setup, migrated):
    _, root = transfer_setup
    assert complete(client, root, ticket(client, root)).status_code == 200
    for sql in ("DELETE FROM upload_completions", "UPDATE upload_completions SET purpose='STORY'"):
        with pytest.raises(IntegrityError), client.app.state.session_factory.begin() as db:
            db.execute(text(sql))
    with pytest.raises(RuntimeError, match="Cannot remove saved upload validation"):
        command.downgrade(migrated, "0023")


@pytest.mark.parametrize("confirmed", [False, 1, "true"])
def test_completion_requires_explicit_rights(client, transfer_setup, confirmed):
    _, root = transfer_setup
    upload = ticket(client, root)
    response = client.post(
        root + f"/uploads/{upload['upload_id']}/complete",
        json={**DECLARATION, "rights_confirmed": confirmed},
    )
    assert response.status_code == 422


def test_registration_failure_keeps_candidate_and_rolls_back_asset(
    client, transfer_setup, monkeypatch
):
    settings, root = transfer_setup
    candidate = complete(client, root, ticket(client, root)).json()
    asset = client.post(root + "/assets", json={"asset_type": "SCENE", "name": "rollback"}).json()
    from arclight.api import upload_completion
    from arclight.core.errors import DomainError

    def fail(*_args):
        raise DomainError("TEST_FAILURE", "registration audit failure", 503)

    monkeypatch.setattr(upload_completion, "audit", fail)
    response = client.post(
        root + f"/files/{candidate['id']}/asset-versions",
        json={
            "asset_id": asset["id"],
            "creation_key": str(uuid4()),
            "expected_revision": asset["revision"],
        },
    )
    assert response.status_code == 503
    assert [r["id"] for r in client.get(root + "/files").json()] == [candidate["id"]]
    with client.app.state.session_factory() as db:
        assert db.scalar(select(AssetVersion)) is None
        assert db.scalar(select(FileRegistration)) is None
    keys = [
        c["Key"]
        for c in ObjectStorage(settings).client.list_objects_v2(Bucket=settings.s3_bucket)[
            "Contents"
        ]
    ]
    assert len(keys) == 2 and any(k.startswith("validated/") for k in keys)


def test_completion_and_registration_obey_viewer_and_archive(client, transfer_setup):
    _, _ = transfer_setup
    from test_collaboration import create_project, register

    owner = register(client)
    project = create_project(client)
    root = f"/api/v1/projects/{project['id']}"
    upload = ticket(client, root)
    candidate = complete(client, root, upload).json()
    asset = client.post(
        root + "/assets", json={"asset_type": "SCENE", "name": "permissions"}
    ).json()
    payload = {
        "asset_id": asset["id"],
        "creation_key": str(uuid4()),
        "expected_revision": asset["revision"],
    }
    member = register(client, "reader@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.post(
            root + "/members", json={"email": "reader@example.com", "role": "viewer"}
        ).status_code
        == 201
    )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert complete(client, root, upload).status_code == 403
    assert (
        client.post(root + f"/files/{candidate['id']}/asset-versions", json=payload).status_code
        == 403
    )
    assert client.get(root + f"/uploads/{upload['upload_id']}").status_code == 200
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert client.patch(root, json={"archived": True}).status_code == 200
    assert complete(client, root, upload).status_code == 409
    assert (
        client.post(root + f"/files/{candidate['id']}/asset-versions", json=payload).status_code
        == 409
    )
