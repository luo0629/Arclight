"""Music/effects use public asset APIs and actual immutable private audio objects."""

import hashlib
import io
import struct
import wave
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_audio_import import completed_source
from test_audio_production import wave_bytes
from test_file_transfers import transfer_setup
from test_upload_completion import complete, ticket

from arclight.application.music_assets import music_resource_facts
from arclight.persistence.models import AssetVersion, FileRegistration, StoredFile, VisualAsset
from arclight.storage.objects import ObjectStorage

__all__ = ["transfer_setup"]


def create_music(client, episode):
    response = client.post(
        f"/api/v1/projects/{episode['project_id']}/assets",
        json={"asset_type": "MUSIC", "name": "原创音乐和音效"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def music_source(client, episode, *, data=None, purpose="MUSIC", **changes):
    return completed_source(client, (episode,), data=data, purpose=purpose, **changes)


def music_url(asset, suffix=""):
    return f"/api/v1/projects/{asset['project_id']}/assets/{asset['id']}" + suffix


def music_payload(asset, role="BACKGROUND", **changes):
    return {
        "asset_id": asset["id"],
        "creation_key": str(uuid4()),
        "expected_revision": asset["revision"],
        "purpose_role": role,
        **changes,
    }


def import_music(client, asset, file_id, role="BACKGROUND", **changes):
    response = client.post(
        f"/api/v1/projects/{asset['project_id']}/files/{file_id}/asset-versions",
        json=music_payload(asset, role, **changes),
    )
    assert response.status_code == 201, response.text
    return response.json()


def approve_music(client, asset, version):
    for status in ("READY_FOR_REVIEW", "APPROVED"):
        response = client.patch(
            music_url(asset, f"/versions/{version['id']}/state"),
            json={"status": status, "expected_revision": version["revision"]},
        )
        assert response.status_code == 200, response.text
        version = response.json()
    return version


def output_file(client, version):
    with client.app.state.session_factory() as db:
        return db.scalar(
            select(StoredFile).where(StoredFile.asset_version_id == UUID(version["id"]))
        )


@pytest.mark.parametrize("role", ["BACKGROUND", "AMBIENCE", "TRANSITION"])
def test_music_public_import_review_selection_lock_and_private_content(client, episode, role):
    asset = create_music(client, episode)
    data = wave_bytes()
    source_id = music_source(client, episode, data=data)
    version = import_music(client, asset, source_id, role)
    assert version["status"] == "DRAFT" and not version["stale"]
    assert version["audio_resource"]["purpose_role"] == role
    assert version["audio_resource"]["source_file_id"] == source_id
    assert version["audio_resource"]["validation"]["duration_ms"] == 100
    assert not version["audio_resource"]["validation"]["silence"]["silent"]
    assert client.get(music_url(asset)).json()["current_version_id"] is None
    assert client.get(music_url(asset, "/versions")).json() == [version]
    with client.app.state.session_factory() as db:
        row = db.get(AssetVersion, UUID(version["id"]))
        music = db.get(VisualAsset, UUID(asset["id"]))
        facts = music_resource_facts(db, music.project_id, music, row)
        assert facts.purpose_role == role
        link = db.scalar(
            select(FileRegistration).where(FileRegistration.asset_version_id == row.id)
        )
        assert str(link.file_id) == source_id
        original = db.get(StoredFile, UUID(source_id))
        assert original.asset_version_id is None
        assert row.metadata_json["media"]["rights_note"] == original.rights_note
    version = approve_music(client, asset, version)
    asset = client.get(music_url(asset)).json()
    selected = client.post(
        music_url(asset, f"/versions/{version['id']}/select"),
        json={
            "expected_revision": asset["revision"],
            "expected_lock_version": asset["lock_version"],
        },
    )
    assert selected.status_code == 200, selected.text
    asset = selected.json()
    locked = client.post(
        music_url(asset, f"/versions/{version['id']}/lock"),
        json={
            "expected_revision": asset["revision"],
            "expected_lock_version": asset["lock_version"],
        },
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["status"] == "LOCKED"
    output = output_file(client, version)
    for file_id in (source_id, str(output.id)):
        content = client.get(f"/api/v1/projects/{asset['project_id']}/files/{file_id}/content")
        assert content.status_code == 200 and content.content == data
        assert content.headers["cache-control"] == "private, no-store"
        assert content.headers["x-content-type-options"] == "nosniff"
        assert content.headers["content-type"].startswith("audio/wav")
        assert "asset-media" in content.headers["content-disposition"]
    rights = client.get(f"/api/v1/projects/{asset['project_id']}/rights?category=MUSIC").json()
    assert {row["id"] for row in rights} == {source_id, str(output.id)}
    assert all(row["rights_status"] == "CONFIRMED" for row in rights)
    assert all("object_key" not in row and "bucket" not in row for row in rights)


def test_music_directory_filter_management_and_visual_api_boundaries(client, episode):
    asset = create_music(client, episode)
    base = f"/api/v1/projects/{episode['project_id']}/assets"
    assert client.get(base).json() == []
    assert client.get(base + "?asset_type=MUSIC").json() == [asset]
    renamed = client.put(music_url(asset), json={"name": "片尾", "expected_revision": 0})
    assert renamed.status_code == 200, renamed.text
    asset = renamed.json()
    archived = client.patch(music_url(asset), json={"archived": True, "expected_revision": 1})
    assert archived.status_code == 200, archived.text
    restored = client.patch(music_url(asset), json={"archived": False, "expected_revision": 2})
    assert restored.status_code == 200, restored.text
    asset = restored.json()
    source_id = music_source(client, episode)
    version = import_music(client, asset, source_id)
    version_url = music_url(asset, f"/versions/{version['id']}")
    for suffix in ("/image", "/rebuild-inputs", "/continuity"):
        assert client.get(version_url + suffix).status_code == 404
    assert (
        client.post(
            version_url + "/restore-as-new",
            json={
                "creation_key": str(uuid4()),
                "expected_revision": 4,
                "comment": "音乐不走图片恢复",
            },
        ).status_code
        == 404
    )


def test_music_role_changes_create_new_immutable_versions_with_original_rights(client, episode):
    asset = create_music(client, episode)
    source_id = music_source(client, episode)
    first = import_music(client, asset, source_id)
    original = deepcopy(first)
    asset = client.get(music_url(asset)).json()
    second_source = music_source(client, episode, rights_note="仅此新版本的独立原创授权")
    second = import_music(client, asset, second_source, "AMBIENCE")
    assert second["id"] != first["id"] and second["version_no"] == 2
    assert second["audio_resource"]["purpose_role"] == "AMBIENCE"
    assert second["media"]["rights_note"] != first["media"]["rights_note"]
    assert client.get(music_url(asset, f"/versions/{first['id']}")).json() == original
    files = client.get(f"/api/v1/projects/{asset['project_id']}/rights?category=MUSIC").json()
    assert len(files) == 4
    assert len({row["rights_note"] for row in files}) == 2


def test_music_import_accepts_real_audio_larger_than_one_mebibyte(client, episode):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(struct.pack("<hhhh", 0, 1000, 0, -1000) * 150000)
    data = stream.getvalue()
    assert len(data) > 1024**2
    asset = create_music(client, episode)
    source_id = music_source(client, episode, data=data)
    version = import_music(client, asset, source_id)
    assert version["checksum"] == hashlib.sha256(data).hexdigest()
    assert version["media"]["byte_size"] == len(data)
    assert version["audio_resource"]["validation"]["duration_ms"] == 25000
    version = approve_music(client, asset, version)
    file = output_file(client, version)
    assert (
        client.get(f"/api/v1/projects/{asset['project_id']}/files/{file.id}/content").content
        == data
    )


@pytest.mark.parametrize("role", ["BACKGROUND", "AMBIENCE", "TRANSITION"])
def test_music_public_completed_s3_upload_freezes_original_and_output(client, transfer_setup, role):
    settings, root = transfer_setup
    episode = {"project_id": root.split("/")[-1]}
    asset = create_music(client, episode)
    data = wave_bytes()
    upload = ticket(
        client,
        root,
        data=data,
        purpose="MUSIC",
        original_name="original.wav",
        media_type="audio/wav",
    )
    completed = complete(client, root, upload)
    assert completed.status_code == 200, completed.text
    source_id = completed.json()["id"]
    version = import_music(client, asset, source_id, role)
    version = approve_music(client, asset, version)
    file = output_file(client, version)
    assert file.storage_backend == "S3" and file.bucket == settings.s3_bucket
    assert ObjectStorage(settings).read(file.object_key, None, file.checksum) == data
    content = client.get(root + f"/files/{file.id}/content")
    assert content.status_code == 200 and content.content == data
