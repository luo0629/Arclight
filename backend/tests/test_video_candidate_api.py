"""Actual candidate APIs retain private bytes, exact history and video admission."""

import hashlib
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_collaboration import register
from test_file_transfers import transfer_setup as transfer_setup  # noqa: F401
from test_file_transfers import upload_payload, upload_to_s3
from test_generation_video_inputs import video_case as video_case  # noqa: F401
from test_upload_completion import DECLARATION
from test_video_candidates import finish_video, video_bytes
from test_video_candidates import formal_video_case as formal_video_case  # noqa: F401

from arclight.application import generation_artifacts
from arclight.domain.file_policy import check_file_policy
from arclight.persistence.models import GenerationCandidate, StoredFile
from arclight.storage.objects import ObjectStorage


@pytest.fixture
def api_video(client, settings, formal_video_case):
    data = video_bytes()
    job, attempt, provider = finish_video(client, settings, formal_video_case, data)
    assert (
        generation_artifacts.import_attempt(
            client.app.state.session_factory, settings, attempt, provider
        )
        == 1
    )
    root = f"/api/v1/projects/{formal_video_case['project_id']}"
    response = client.get(root + f"/generation-jobs/{job['id']}/candidates")
    assert response.status_code == 200, response.text
    [candidate] = response.json()
    return root, candidate, data, formal_video_case


def manual_file(client, settings, project_id, data, *, confirmed=True):
    identity = uuid4()
    checksum = hashlib.sha256(data).hexdigest()
    key = f"files/{project_id}/{identity}"
    ObjectStorage(settings).put(key, data, "video/mp4")
    with client.app.state.session_factory() as db:
        db.add(
            StoredFile(
                id=identity,
                project_id=UUID(project_id),
                original_name="replacement.mp4",
                media_type="video/mp4",
                byte_size=len(data),
                checksum=checksum,
                storage_backend="LOCAL",
                bucket=None,
                object_key=key,
                source_note="Synthetic test video",
                rights_note="Test generated media",
                rights_confirmed=confirmed,
                created_by="service-owner",
            )
        )
        db.commit()
    return str(identity)


def test_video_detail_production_and_private_content_are_exact(client, api_video):
    root, candidate, data, case = api_video
    path = root + f"/generation-candidates/{candidate['id']}"
    response = client.get(path)
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail == candidate
    assert detail["media_kind"] == "VIDEO"
    assert detail["project_id"] == case["project_id"]
    assert detail["required_review"] == "VIDEO_TEMPORAL"
    assert detail["production"]["model"] == case["body"]["spec"]["model"]
    assert detail["production"]["prompt"] == case["body"]["spec"]["prompt"]
    assert detail["video_source"]["storyboard_id"] == case["body"]["review_target"]["storyboard_id"]
    assert detail["video_source"]["video_images"][0]["asset_version_id"] == case["version"]["id"]
    public = json.dumps(detail)
    for forbidden in (
        "object_key",
        "credential",
        "api_key",
        "cdn.example",
        "provider_configuration",
    ):
        assert forbidden not in public
    assert detail["admission"]["status"] == "BLOCKED"
    for action in ("can_submit", "can_approve", "can_select", "can_lock"):
        assert detail["actions"][action] is False
    assert detail["actions"]["can_replace"] is True
    content = client.get("/api/v1" + detail["content_path"])
    assert content.status_code == 200 and content.content == data
    assert content.headers["content-type"] == "video/mp4"
    assert content.headers["cache-control"] == "private, no-store"
    assert content.headers["x-content-type-options"] == "nosniff"
    assert client.get(path + "/image").status_code == 422
    board = case["body"]["review_target"]["storyboard_id"]
    production = client.get(f"/api/v1/episodes/{case['episode_id']}/storyboards/{board}/production")
    assert production.status_code == 200, production.text
    shot = next(
        row
        for row in production.json()["shots"]
        if row["id"] == case["body"]["review_target"]["shot_id"]
    )
    assert shot["candidates"] == []
    assert shot["video_candidates"][0]["id"] == detail["id"]
    assert not shot["video_candidates_truncated"]


def test_video_content_and_replacements_reject_cross_project_or_removed_access(client, api_video):
    root, candidate, _, _ = api_video
    other = client.post("/api/v1/projects", json={"name": "other"}).json()["id"]
    for suffix in ("", "/content", "/image"):
        assert (
            client.get(
                f"/api/v1/projects/{other}/generation-candidates/{candidate['id']}" + suffix
            ).status_code
            == 404
        )
    register(client, "video-outsider@example.com")
    path = root + f"/generation-candidates/{candidate['id']}"
    assert client.get(path).status_code == 404
    assert client.get(path + "/content").status_code == 404
    assert (
        client.post(
            path + "/replacements",
            json={
                "file_id": str(uuid4()),
                "creation_key": str(uuid4()),
                "expected_revision": 0,
            },
        ).status_code
        == 404
    )


def test_video_replacement_is_idempotent_preserves_history_and_checks_cas(
    client, settings, api_video
):
    root, source, data, case = api_video
    file_id = manual_file(client, settings, case["project_id"], data)
    path = root + f"/generation-candidates/{source['id']}/replacements"
    payload = {
        "file_id": file_id,
        "creation_key": str(uuid4()),
        "expected_revision": source["asset_revision"],
    }
    response = client.post(path, json=payload)
    assert response.status_code == 201, response.text
    replacement = response.json()
    assert replacement["id"] != source["id"]
    assert replacement["source"] == "REPLACEMENT"
    assert replacement["replaces_candidate_id"] == source["id"]
    assert replacement["version"]["status"] == "DRAFT"
    assert replacement["media_check"]["status"] == "PASSED"
    assert replacement["admission"]["status"] == "BLOCKED"
    assert client.post(path, json=payload).json() == replacement
    assert client.post(path, json={**payload, "creation_key": str(uuid4())}).status_code == 409
    assert client.post(path, json={**payload, "file_id": str(uuid4())}).status_code == 409
    original = client.get(root + f"/generation-candidates/{source['id']}").json()
    assert (
        original["version"] == source["version"]
        and original["media_check"] == source["media_check"]
    )
    assert client.get("/api/v1" + source["content_path"]).content == data
    with client.app.state.session_factory() as db:
        assert len(list(db.scalars(select(GenerationCandidate)))) == 2


def test_replacing_a_restored_video_retains_its_effective_manifest(client, settings, api_video):
    root, source, data, case = api_video
    asset_root = root + f"/assets/{source['version']['asset_id']}"
    version_root = asset_root + f"/versions/{source['version']['id']}"
    manifest = client.post(
        version_root + "/rebuild-manifest",
        json={
            "creation_key": str(uuid4()),
            "expected_revision": source["version"]["revision"],
        },
    )
    assert manifest.status_code == 201, manifest.text
    restored = client.post(
        version_root + "/restore-as-new",
        json={
            "creation_key": str(uuid4()),
            "expected_revision": source["asset_revision"],
            "comment": "Restore exact historical video",
            "dependency_manifest_id": manifest.json()["id"],
        },
    )
    assert restored.status_code == 201, restored.text
    with client.app.state.session_factory() as db:
        candidate = db.scalar(
            select(GenerationCandidate).where(
                GenerationCandidate.asset_version_id == UUID(restored.json()["id"])
            )
        )
        restored_id = str(candidate.id)
    detail = client.get(root + f"/generation-candidates/{restored_id}").json()
    file_id = manual_file(client, settings, case["project_id"], data)
    replaced = client.post(
        root + f"/generation-candidates/{restored_id}/replacements",
        json={
            "creation_key": str(uuid4()),
            "expected_revision": detail["asset_revision"],
            "file_id": file_id,
        },
    )
    assert replaced.status_code == 201, replaced.text
    result = replaced.json()
    assert result["input_manifest_id"] == manifest.json()["id"]
    assert result["media_check"]["input_manifest_id"] == manifest.json()["id"]
    assert result["version"]["dependency_manifest_id"] == manifest.json()["id"]
    assert result["admission"]["status"] == "BLOCKED"
    assert result["video_source"]["generation_manifest_id"] == source["input_manifest_id"]
    assert result["production"] == source["production"]
    assert detail["production"] == source["production"]
    board = case["body"]["review_target"]["storyboard_id"]
    production = client.get(f"/api/v1/episodes/{case['episode_id']}/storyboards/{board}/production")
    assert production.status_code == 200, production.text
    shot = next(
        row
        for row in production.json()["shots"]
        if row["id"] == case["body"]["review_target"]["shot_id"]
    )
    assert {row["id"] for row in shot["video_candidates"]} == {
        source["id"],
        restored_id,
        result["id"],
    }


@pytest.mark.parametrize("bad", ["rights", "broken", "other_project"])
def test_video_replacement_rejects_invalid_sources_without_new_history(
    client, settings, api_video, bad
):
    root, source, data, case = api_video
    project_id = case["project_id"]
    if bad == "other_project":
        project_id = client.post("/api/v1/projects", json={"name": "foreign"}).json()["id"]
    file_id = manual_file(
        client,
        settings,
        project_id,
        b"broken" if bad == "broken" else data,
        confirmed=bad != "rights",
    )
    result = client.post(
        root + f"/generation-candidates/{source['id']}/replacements",
        json={
            "file_id": file_id,
            "creation_key": str(uuid4()),
            "expected_revision": source["asset_revision"],
        },
    )
    assert result.status_code in {404, 422}, result.text
    with client.app.state.session_factory() as db:
        assert len(list(db.scalars(select(GenerationCandidate)))) == 1


def test_video_upload_policy_accepts_only_mp4_for_the_video_purpose():
    assert check_file_policy("VIDEO", "shot.mp4", "video/mp4", 1024).max_bytes is None
    from arclight.core.errors import DomainError

    for name, media in (
        ("shot.png", "image/png"),
        ("shot.wav", "audio/wav"),
        ("shot.exe", "video/mp4"),
    ):
        with pytest.raises(DomainError):
            check_file_policy("VIDEO", name, media, 1024)


@pytest.mark.parametrize("valid", [True, False])
def test_actual_mp4_upload_and_rights_inventory(client, transfer_setup, valid):
    _, root = transfer_setup
    data = video_bytes(seconds=1) if valid else b"invalid video bytes"
    response = client.post(
        root + "/uploads",
        json=upload_payload(
            purpose="VIDEO",
            original_name="shot.mp4",
            media_type="video/mp4",
            byte_size=len(data),
            checksum=hashlib.sha256(data).hexdigest(),
        ),
    )
    assert response.status_code == 201, response.text
    ticket = response.json()
    assert upload_to_s3(ticket, data=data).status_code == 204
    result = client.post(root + f"/uploads/{ticket['upload_id']}/complete", json=DECLARATION)
    if not valid:
        assert result.status_code == 422, result.text
        assert client.get(root + "/rights?category=VIDEO").json() == []
        return
    assert result.status_code == 200, result.text
    receipt = result.json()
    assert client.get(root + f"/files/{receipt['id']}/content").content == data
    rights = client.get(root + "/rights?category=VIDEO")
    assert rights.status_code == 200, rights.text
    [row] = rights.json()
    assert row["category"] == "VIDEO" and row["rights_status"] == "CONFIRMED"
    assert row["id"] == receipt["id"]
