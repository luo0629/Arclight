"""Historical real renders retain exact provenance and require current download rights."""

import hashlib
import json
from uuid import UUID, uuid4

import httpx
import pytest
from render_admission_helpers import ready_manifest, request_export
from render_job_helpers import job_case, read, render_case, review_case, run, submit, timeline_case
from render_manifest_helpers import result_case
from sqlalchemy import select
from test_collaboration import create_episode, create_project, register
from test_file_transfers import transfer_setup
from test_human_reviews import decide, resume

from arclight.persistence.models import AuditEvent, StoredFile
from arclight.storage.objects import ObjectStorage

__all__ = [
    "job_case",
    "render_case",
    "result_case",
    "review_case",
    "timeline_case",
    "transfer_setup",
]


def history_url(episode):
    return f"/api/v1/episodes/{episode['id']}/deliveries"


@pytest.fixture
def delivery_case(client, episode, job_case):
    manifest, value, _ = job_case
    assert run(client, value) == "SUCCEEDED"
    final = read(client, value)
    response = client.get(history_url(episode) + f"/{final['artifact_id']}")
    assert response.status_code == 200, response.text
    return manifest, final, response.json()


def movie_bytes(client, delivery):
    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(delivery["file_id"]))
        return ObjectStorage(client.app.state.settings).read(
            file.object_key, file.byte_size, file.checksum
        )


def viewer(client, episode):
    owner = client.headers["Authorization"]
    member = register(client, "render-viewer@example.com")
    client.headers["Authorization"] = owner
    response = client.post(
        f"/api/v1/projects/{episode['project_id']}/members",
        json={"email": member["user"]["email"], "role": "viewer"},
    )
    assert response.status_code == 201, response.text
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    return owner, member


def test_rerender_history_pages_and_details_preserve_exact_old_movie(
    client, episode, delivery_case
):
    manifest, first_job, first = delivery_case
    root = history_url(episode)
    original = movie_bytes(client, first)
    second_job, _ = submit(client, manifest)
    assert run(client, second_job) == "SUCCEEDED"
    second_job = read(client, second_job)
    response = client.get(root)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    newest, oldest = response.json()
    assert [newest["version_no"], oldest["version_no"]] == [2, 1]
    assert newest["asset_id"] == oldest["asset_id"] == first["asset_id"]
    for field in ("id", "job_id", "asset_version_id", "file_id", "dependency_manifest_id"):
        assert newest[field] != oldest[field]
    assert oldest["id"] == first_job["artifact_id"]
    assert newest["id"] == second_job["artifact_id"]
    assert oldest["manifest_id"] == newest["manifest_id"] == manifest["id"]
    page = client.get(root, params={"limit": 1})
    assert page.json() == [newest]
    assert client.get(root, params={"limit": 1}).json() == [newest]
    assert client.get(root, params={"limit": 1, "before_id": newest["id"]}).json() == [oldest]
    assert client.get(root, params={"before_id": oldest["id"]}).json() == []

    detail = client.get(root + f"/{first['id']}")
    assert detail.status_code == 200 and detail.json() == first
    assert detail.headers["cache-control"] == "no-store"
    snapshot = manifest["snapshot"]
    assert first["sources"] == snapshot["sources"]
    assert first["timeline_id"] == snapshot["timeline_id"]
    assert first["timeline_sha256"] == snapshot["timeline_sha256"]
    assert first["timeline_dependency_manifest_id"] == snapshot["dependency_manifest_id"]
    assert first["timeline_dependency_manifest_sha256"] == snapshot["dependency_manifest_sha256"]
    for field in ("font", "software", "output"):
        assert first[field] == snapshot[field]
    assert first["profile"] == "PREVIEW" and first["is_delivery"] is False
    assert first["duration_ms"] == 2500
    assert first["manifest_sha256"] == manifest["content_sha256"]
    for field in ("dependency_manifest_id", "inspection_id", "asset_version_id"):
        UUID(first[field])
    for value in (response.json(), first):
        serialized = json.dumps(value)
        assert '"object_key"' not in serialized and '"bucket"' not in serialized
    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(first["file_id"]))
        assert file.object_key not in detail.text
    downloaded = client.get(root + f"/{first['id']}/content")
    assert downloaded.status_code == 200 and downloaded.content == original
    assert hashlib.sha256(downloaded.content).hexdigest() == first["sha256"]
    assert len(downloaded.content) == first["byte_size"]
    assert client.get(f"/api/v1/render-jobs/{first_job['id']}").json() == first_job


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"before_id": "invalid"}])
def test_history_rejects_invalid_pagination(client, episode, params):
    assert client.get(history_url(episode), params=params).status_code == 422


def test_history_cursor_and_detail_are_scoped_to_exact_episode(client, episode, delivery_case):
    _, _, delivery = delivery_case
    root = history_url(episode)
    assert client.get(root, params={"before_id": str(uuid4())}).status_code == 404
    assert client.get(root + f"/{uuid4()}").status_code == 404
    response = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "第二集"},
    )
    assert response.status_code == 201, response.text
    other_project_episode = create_episode(client, create_project(client))
    for other in (response.json(), other_project_episode):
        other_root = history_url(other)
        assert client.get(other_root).json() == []
        assert client.get(other_root, params={"before_id": delivery["id"]}).status_code == 404
        assert client.get(other_root + f"/{delivery['id']}").status_code == 404
        assert client.get(other_root + f"/{delivery['id']}/content").status_code == 404


def test_unregistered_manual_receipt_is_not_published_as_a_delivery(client, episode, result_case):
    _, artifact, _ = result_case
    root = history_url(episode)
    assert client.get(root).json() == []
    assert client.get(root + f"/{artifact.id}").status_code == 404
    assert client.get(root + f"/{artifact.id}/content").status_code == 404


def test_formal_render_history_keeps_profile_without_claiming_final_delivery(
    client, episode, render_case
):
    manifest, _ = ready_manifest(client, render_case, profile="FORMAL")
    review = request_export(client, manifest)
    decision = decide(client, review)
    assert decision.status_code == 200, decision.text
    assert resume(client, review, decision.json()).status_code == 200
    value, _ = submit(client, manifest)
    assert run(client, value) == "SUCCEEDED"
    final = read(client, value)
    root = history_url(episode)
    response = client.get(root + f"/{final['artifact_id']}")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["profile"] == detail["output"]["name"] == "FORMAL"
    assert detail["is_delivery"] is False
    assert client.get(root).json()[0]["is_delivery"] is False
    assert client.get(root + f"/{detail['id']}/content").status_code == 200


def test_local_download_uses_exact_receipt_headers_and_audit(
    client, episode, delivery_case, monkeypatch
):
    _, _, delivery = delivery_case
    original_read = ObjectStorage.read
    limits = []

    def read_exact(storage, key, max_bytes, checksum=None):
        limits.append(max_bytes)
        return original_read(storage, key, max_bytes, checksum)

    monkeypatch.setattr(ObjectStorage, "read", read_exact)
    response = client.get(history_url(episode) + f"/{delivery['id']}/content")
    assert response.status_code == 200, response.text
    assert limits == [None]
    assert response.content == movie_bytes(client, delivery)
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="render-{delivery["id"]}.mp4"'
    )
    with client.app.state.session_factory() as db:
        events = list(
            db.scalars(select(AuditEvent).where(AuditEvent.action == "render.downloaded"))
        )
        assert len(events) == 1
        event = events[0]
        assert str(event.project_id) == episode["project_id"]
        assert str(event.resource_id) == delivery["id"]
        assert event.actor_id == "service-owner"
        assert event.details_json == {"file_id": delivery["file_id"]}


@pytest.mark.parametrize("damage", ["missing", "replaced", "over_limit"])
def test_local_download_rechecks_storage_integrity_and_bounds(
    client, episode, delivery_case, damage
):
    _, _, delivery = delivery_case
    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(delivery["file_id"]))
        path = ObjectStorage(client.app.state.settings).path(file.object_key)
    if damage == "missing":
        path.unlink()
    elif damage == "replaced":
        path.write_bytes(b"x" * delivery["byte_size"])
    else:
        path.write_bytes(path.read_bytes() + b"x")
    root = history_url(episode)
    response = client.get(root + f"/{delivery['id']}/content")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "OBJECT_UNAVAILABLE"
    assert str(path) not in response.text
    assert client.get(root + f"/{delivery['id']}").json() == delivery
    with client.app.state.session_factory() as db:
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "render.downloaded")) is None


def test_viewer_download_revalidates_membership_and_logout(client, episode, delivery_case):
    _, _, delivery = delivery_case
    root = history_url(episode)
    paths = (root, root + f"/{delivery['id']}", root + f"/{delivery['id']}/content")
    original_authorization = client.headers["Authorization"]
    outsider = register(client, "render-outsider@example.com")
    for path in paths:
        assert client.get(path).status_code == 404
    client.headers["Authorization"] = original_authorization
    owner, member = viewer(client, episode)
    for path in paths:
        assert client.get(path).status_code == 200
    client.headers["Authorization"] = owner
    member_url = f"/api/v1/projects/{episode['project_id']}/members/{member['user']['id']}"
    assert client.delete(member_url).status_code == 204
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    for path in paths:
        assert client.get(path).status_code == 404
    client.cookies.clear()
    assert client.post("/api/v1/auth/logout").status_code == 204
    for path in paths:
        assert client.get(path).status_code == 401
    client.headers["Authorization"] = "Bearer " + outsider["access_token"]
    assert client.get(root).status_code == 404


def test_archive_keeps_historical_download_but_project_deletion_revokes_it(
    client, episode, delivery_case
):
    _, _, delivery = delivery_case
    root = history_url(episode)
    paths = (root, root + f"/{delivery['id']}", root + f"/{delivery['id']}/content")
    project_url = f"/api/v1/projects/{episode['project_id']}"
    episode_url = f"/api/v1/episodes/{episode['id']}"
    assert client.patch(episode_url, json={"archived": True}).status_code == 200
    for path in paths:
        assert client.get(path).status_code == 200
    assert client.patch(episode_url, json={"archived": False}).status_code == 200
    archived = client.patch(project_url, json={"archived": True})
    assert archived.status_code == 200, archived.text
    for path in paths:
        assert client.get(path).status_code == 200
    deleted = client.request("DELETE", project_url, json={"confirm_name": archived.json()["name"]})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["can_restore"] is True
    for path in paths:
        assert client.get(path).status_code == 404


def test_delivery_public_routes_cannot_overwrite_or_remove_history(client, episode, delivery_case):
    _, _, delivery = delivery_case
    root = history_url(episode)
    detail_url = root + f"/{delivery['id']}"
    original = movie_bytes(client, delivery)
    assert client.post(root, json=delivery).status_code == 405
    for method in ("patch", "put", "delete"):
        assert client.request(method, detail_url, json={"file_id": str(uuid4())}).status_code == 405
    assert client.get(detail_url).json() == delivery
    assert client.get(detail_url + "/content").content == original


@pytest.fixture
def s3_delivery_case(client, transfer_setup, request):
    # Configure S3 before creating inputs. Without isolated S3, skip this fixture
    # before starting the gateway.
    manifest, final, detail = request.getfixturevalue("delivery_case")
    return transfer_setup, manifest, final, detail


@pytest.mark.parametrize("revocation", ["remove_and_rejoin", "logout", "delete_project"])
def test_s3_real_render_download_uses_private_revocable_exact_file_ticket(
    client, episode, s3_delivery_case, revocation
):
    (settings, project_url), _, _, delivery = s3_delivery_case
    root = history_url(episode)
    assert delivery["storage_backend"] == "S3"
    direct = client.get(root + f"/{delivery['id']}/content")
    assert direct.status_code == 409 and direct.json()["error"]["code"] == "PRESIGNED_REQUIRED"
    original = movie_bytes(client, delivery)
    owner, member = viewer(client, episode)
    ticket_url = project_url + f"/files/{delivery['file_id']}/download-url"
    minted = client.post(ticket_url, json={"expires_in": 60})
    assert minted.status_code == 200, minted.text
    assert minted.headers["cache-control"] == "no-store"
    ticket = minted.json()
    downloaded = httpx.get(ticket["url"], trust_env=False, timeout=10)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == original
    assert hashlib.sha256(downloaded.content).hexdigest() == delivery["sha256"]
    assert len(downloaded.content) == delivery["byte_size"]
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(delivery["file_id"]))
        public_url = settings.s3_endpoint_url.rstrip("/") + f"/{file.bucket}/{file.object_key}"
        assert file.object_key not in minted.text and file.bucket not in minted.text
    assert httpx.get(public_url, trust_env=False, timeout=10).status_code == 403
    if revocation == "logout":
        client.cookies.clear()
        assert client.post("/api/v1/auth/logout").status_code == 204
        denied_status = 401
    else:
        client.headers["Authorization"] = owner
        if revocation == "remove_and_rejoin":
            assert (
                client.delete(project_url + f"/members/{member['user']['id']}").status_code == 204
            )
        else:
            archived = client.patch(project_url, json={"archived": True})
            assert archived.status_code == 200, archived.text
            deleted = client.request(
                "DELETE", project_url, json={"confirm_name": archived.json()["name"]}
            )
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()["can_restore"] is True
        client.headers["Authorization"] = "Bearer " + member["access_token"]
        denied_status = 404
    assert client.post(ticket_url, json={}).status_code == denied_status
    denied = httpx.get(ticket["url"], trust_env=False, timeout=10)
    assert denied.status_code == 403 and denied.headers["cache-control"] == "private, no-store"
    assert original not in denied.content and "X-Amz" not in denied.text
    if revocation == "remove_and_rejoin":
        client.headers["Authorization"] = owner
        restored = client.post(
            project_url + "/members",
            json={"email": member["user"]["email"], "role": "viewer"},
        )
        assert restored.status_code == 201, restored.text
        assert httpx.get(ticket["url"], trust_env=False, timeout=10).status_code == 403
        client.headers["Authorization"] = "Bearer " + member["access_token"]
        fresh = client.post(ticket_url, json={})
        assert fresh.status_code == 200, fresh.text
        assert httpx.get(fresh.json()["url"], trust_env=False, timeout=10).content == original


def test_s3_same_length_replacement_cannot_replace_a_downloaded_render_history(
    client, episode, s3_delivery_case
):
    (settings, project_url), manifest, _, first = s3_delivery_case
    root = history_url(episode)
    first_bytes = movie_bytes(client, first)
    first_ticket_response = client.post(
        project_url + f"/files/{first['file_id']}/download-url", json={}
    )
    assert first_ticket_response.status_code == 200, first_ticket_response.text
    first_ticket = first_ticket_response.json()["url"]

    second_job, _ = submit(client, manifest)
    assert run(client, second_job) == "SUCCEEDED"
    second_job = read(client, second_job)
    second_detail = client.get(root + f"/{second_job['artifact_id']}")
    assert second_detail.status_code == 200, second_detail.text
    second = second_detail.json()
    assert second["file_id"] != first["file_id"]
    second_bytes = movie_bytes(client, second)
    second_ticket_url = project_url + f"/files/{second['file_id']}/download-url"
    second_ticket_response = client.post(second_ticket_url, json={})
    assert second_ticket_response.status_code == 200, second_ticket_response.text
    second_ticket = second_ticket_response.json()["url"]
    assert httpx.get(first_ticket, trust_env=False, timeout=10).content == first_bytes
    assert httpx.get(second_ticket, trust_env=False, timeout=10).content == second_bytes

    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(second["file_id"]))
        bucket, key, media_type = file.bucket, file.object_key, file.media_type
    store = ObjectStorage(settings)
    assert store.client is not None
    corrupted = second_bytes[:-1] + bytes([second_bytes[-1] ^ 0xFF])
    assert len(corrupted) == second["byte_size"]
    assert hashlib.sha256(corrupted).hexdigest() != second["sha256"]
    # Simulate an out-of-band replacement that lies about the original checksum.
    store.client.put_object(
        Bucket=bucket,
        Key=key,
        Body=corrupted,
        ContentType=media_type,
        Metadata={"sha256": second["sha256"]},
    )
    metadata = store.client.head_object(Bucket=bucket, Key=key)
    assert metadata["ContentLength"] == second["byte_size"]
    assert metadata["Metadata"]["sha256"] == second["sha256"]
    fresh_ticket_response = client.post(second_ticket_url, json={})
    assert fresh_ticket_response.status_code == 200, fresh_ticket_response.text
    for ticket in (second_ticket, fresh_ticket_response.json()["url"]):
        rejected = httpx.get(ticket, trust_env=False, timeout=10)
        assert rejected.status_code == 502, rejected.text
        assert rejected.json()["error"]["code"] == "TRANSFER_UNAVAILABLE"
        assert rejected.headers["cache-control"] == "private, no-store"
        assert corrupted not in rejected.content
        assert key not in rejected.text and "X-Amz" not in rejected.text

    old_download = httpx.get(first_ticket, trust_env=False, timeout=10)
    assert old_download.status_code == 200 and old_download.content == first_bytes
    assert client.get(root + f"/{first['id']}").json() == first
    assert client.get(root + f"/{second['id']}").json() == second
    assert [row["id"] for row in client.get(root).json()] == [second["id"], first["id"]]
    store.client.put_object(
        Bucket=bucket,
        Key=key,
        Body=second_bytes,
        ContentType=media_type,
        Metadata={"sha256": second["sha256"]},
    )
    restored = httpx.get(second_ticket, trust_env=False, timeout=10)
    assert restored.status_code == 200 and restored.content == second_bytes
