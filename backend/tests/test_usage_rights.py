import hashlib
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_asset_versions import draft, image_bytes
from test_collaboration import create_project, register
from test_file_transfers import transfer_setup, upload_payload, upload_to_s3
from test_media_validation import font_bytes, wave_bytes
from test_story_sources import source_payload, sources_path
from test_upload_completion import DECLARATION

from arclight.application.story_sources import capture_inline
from arclight.persistence.models import AssetVersion, Episode, StoredFile, StorySource

__all__ = ["transfer_setup"]


@pytest.mark.parametrize(
    "purpose,name,media_type",
    [
        ("STORY", "story.txt", "text/plain"),
        ("REFERENCE_IMAGE", "image.png", "image/png"),
        ("AUDIO", "voice.wav", "audio/wav"),
        ("MUSIC", "music.wav", "audio/wav"),
        ("FONT", "font.ttf", "font/ttf"),
    ],
)
def test_all_categories_keep_exact_declaration_and_purpose(
    client, transfer_setup, purpose, name, media_type
):
    _, root = transfer_setup
    data = (
        b"Original story"
        if purpose == "STORY"
        else image_bytes()
        if purpose == "REFERENCE_IMAGE"
        else font_bytes()
        if purpose == "FONT"
        else wave_bytes()
    )
    ticket = client.post(
        root + "/uploads",
        json=upload_payload(
            purpose=purpose,
            original_name=name,
            media_type=media_type,
            byte_size=len(data),
            checksum=hashlib.sha256(data).hexdigest(),
        ),
    ).json()
    assert upload_to_s3(ticket, data=data).status_code == 204
    path = root + f"/uploads/{ticket['upload_id']}/complete"
    declaration = {
        **DECLARATION,
        "source_note": "Author / https://example.org/license",
        "rights_note": "Project use permitted; attribution required",
    }
    result = client.post(path, json=declaration)
    assert result.status_code == 200, result.text
    receipt = result.json()
    assert client.post(path, json=declaration).json() == receipt
    assert client.post(path, json={**declaration, "rights_note": "changed"}).status_code == 409
    response = client.get(root + "/rights", params={"category": purpose})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    [record] = response.json()
    assert record["id"] == receipt["id"]
    assert record["category"] == purpose and record["rights_status"] == "CONFIRMED"
    assert record["source_note"] == declaration["source_note"]
    assert record["rights_note"] == declaration["rights_note"]
    assert record["checksum"] == hashlib.sha256(data).hexdigest()
    assert record["recorded_by"] == "service-owner" and record["recorded_at"].endswith("Z")
    assert record["asset_version_id"] is None and record["episode_id"] is None
    assert not {"bucket", "object_key", "url", "text", "raw_bytes"}.intersection(record)
    assert client.get(root + "/rights?rights_status=UNDECLARED").json() == []
    with client.app.state.session_factory() as db:
        saved = db.get(StoredFile, UUID(receipt["id"]))
        assert saved.source_note == declaration["source_note"]
        assert len(list(db.scalars(select(StoredFile)))) == 1


def test_story_revisions_and_undeclared_inline_keep_separate_evidence(client, episode):
    root = f"/api/v1/projects/{episode['project_id']}"
    path = sources_path(episode)
    first = client.post(path, json=source_payload()).json()
    second = client.post(
        path,
        json=source_payload(
            b"Revised story",
            base_source_id=first["id"],
            change_note="licensed revision",
            source_note="Another source",
            rights_note="Another license",
        ),
    )
    assert second.status_code == 201, second.text
    with client.app.state.session_factory.begin() as db:
        manual = capture_inline(
            db,
            db.get(Episode, UUID(episode["id"])),
            "service-owner",
            "Manual story",
            settings=client.app.state.settings,
        )
        manual_id = str(manual.id)
    records = client.get(root + "/rights").json()
    assert len(records) == 3
    by_id = {r["id"]: r for r in records}
    assert by_id[first["id"]]["source_note"] == first["source_note"]
    assert by_id[second.json()["id"]]["rights_note"] == "Another license"
    assert by_id[manual_id]["rights_status"] == "UNDECLARED"
    assert all(r["episode_id"] == episode["id"] for r in records)
    assert client.get(root + "/rights?rights_status=UNDECLARED").json() == [by_id[manual_id]]
    paged = [client.get(root + f"/rights?limit=1&offset={i}").json()[0] for i in range(3)]
    assert paged == records and len({r["id"] for r in paged}) == 3
    assert client.get(root + "/rights?offset=3").json() == []


def test_direct_image_and_restore_keep_exact_version_rights(client, episode, tmp_path):
    client.app.state.settings.asset_storage_path = tmp_path / "images"
    root = f"/api/v1/projects/{episode['project_id']}"
    asset = client.post(root + "/assets", json={"asset_type": "SCENE", "name": "Image"}).json()
    path = root + f"/assets/{asset['id']}/versions"
    payload = draft(asset)
    first = client.post(path, json=payload).json()
    second = client.post(
        path + f"/{first['id']}/restore-as-new",
        json={"creation_key": str(uuid4()), "expected_revision": 1, "comment": "restore"},
    )
    assert second.status_code == 201, second.text
    records = client.get(root + "/rights?category=REFERENCE_IMAGE").json()
    assert {r["asset_version_id"] for r in records} == {first["id"], second.json()["id"]}
    assert all(r["source_note"] == payload["source_note"] for r in records)
    assert all(r["rights_status"] == "CONFIRMED" for r in records)


@pytest.mark.parametrize("endpoint", ["story", "image", "upload"])
@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_explicit_true_is_required_at_every_declaration_boundary(client, episode, endpoint, value):
    root = f"/api/v1/projects/{episode['project_id']}"
    if endpoint == "story":
        path, payload = sources_path(episode), source_payload(rights_confirmed=value)
    elif endpoint == "image":
        asset = client.post(root + "/assets", json={"asset_type": "SCENE", "name": "Image"}).json()
        path, payload = (
            root + f"/assets/{asset['id']}/versions",
            {**draft(asset), "rights_confirmed": value},
        )
    else:
        path, payload = (
            root + f"/uploads/{uuid4()}/complete",
            {**DECLARATION, "rights_confirmed": value},
        )
    assert client.post(path, json=payload).status_code == 422


def test_inventory_permissions_archive_revoke_and_project_isolation(client):
    owner = register(client)
    project = create_project(client)
    root = f"/api/v1/projects/{project['id']}"
    member = register(client, "viewer@example.com")
    assert client.get(root + "/rights").status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.post(
            root + "/members", json={"email": "viewer@example.com", "role": "viewer"}
        ).status_code
        == 201
    )
    assert client.patch(root, json={"archived": True}).status_code == 200
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert client.get(root + "/rights").json() == []
    assert client.get(root + "/rights?category=UNKNOWN").status_code == 422
    assert client.get(root + "/rights?limit=101").status_code == 422
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert client.patch(root, json={"archived": False}).status_code == 200
    assert client.delete(root + f"/members/{member['user']['id']}").status_code == 204
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert client.get(root + "/rights").status_code == 404
    client.headers.pop("Authorization")
    assert client.get(root + "/rights").status_code == 401


@pytest.mark.parametrize("field", ["source_note", "rights_note"])
def test_blank_declarations_rejected_before_storage(client, episode, field):
    path = sources_path(episode)
    assert client.post(path, json=source_payload(**{field: "  \t "})).status_code == 422
    root = f"/api/v1/projects/{episode['project_id']}"
    assert client.get(root + "/rights").json() == []


def test_mixed_inventory_pagination_does_not_skip_file_or_story(client, episode, tmp_path):
    client.app.state.settings.asset_storage_path = tmp_path / "images"
    root = f"/api/v1/projects/{episode['project_id']}"
    story = client.post(sources_path(episode), json=source_payload()).json()
    asset = client.post(root + "/assets", json={"asset_type": "SCENE", "name": "Image"}).json()
    assert (
        client.post(root + f"/assets/{asset['id']}/versions", json=draft(asset)).status_code == 201
    )
    records = client.get(root + "/rights").json()
    assert len(records) == 2 and {r["record_type"] for r in records} == {"FILE", "STORY_SOURCE"}
    assert story["id"] in {r["id"] for r in records}
    assert (
        client.get(root + "/rights?limit=1").json()
        + client.get(root + "/rights?limit=1&offset=1").json()
        == records
    )
    other = client.post("/api/v1/projects", json={"name": "Other"}).json()
    assert client.get(f"/api/v1/projects/{other['id']}/rights").json() == []


def test_legacy_missing_confirmation_is_not_inferred_from_notes(client, episode):
    with client.app.state.session_factory.begin() as db:
        db.add(
            StorySource(
                episode_id=UUID(episode["id"]),
                import_no=1,
                import_key=uuid4(),
                request_fingerprint="a" * 64,
                original_name="legacy.txt",
                source_type="TXT",
                media_type="text/plain",
                encoding="utf-8",
                byte_size=6,
                sha256="b" * 64,
                storage_backend="LOCAL",
                bucket=None,
                object_key=f"sources/{episode['project_id']}/{uuid4()}",
                source_note="Legacy source",
                rights_note="Legacy note",
                created_by="old-importer",
            )
        )
        db.add(
            StoredFile(
                project_id=UUID(episode["project_id"]),
                storage_backend="LOCAL",
                bucket=None,
                object_key=f"{episode['project_id']}/{uuid4()}.image",
                checksum="c" * 64,
                byte_size=1,
                media_type="image/png",
                original_name="legacy.png",
                source_note="Legacy source",
                rights_note="Legacy note",
                rights_confirmed=False,
                created_by="old-importer",
            )
        )
    root = f"/api/v1/projects/{episode['project_id']}/rights"
    records = client.get(root + "?rights_status=UNDECLARED").json()
    assert len(records) == 2 and all(r["recorded_by"] == "old-importer" for r in records)
    assert client.get(root + "?rights_status=CONFIRMED").json() == []


@pytest.mark.parametrize("confirmation", [True, False, None])
def test_pre_file_ledger_images_remain_visible_without_duplicate_receipts(
    client, episode, confirmation
):
    root = f"/api/v1/projects/{episode['project_id']}"
    asset = client.post(root + "/assets", json={"asset_type": "SCENE", "name": "Legacy"}).json()
    media = {
        "media_type": "image/png",
        "original_name": "legacy.png",
        "source_note": "Original source",
        "rights_note": "Original note",
    }
    if confirmation is not None:
        media["rights_confirmed"] = confirmation
    with client.app.state.session_factory.begin() as db:
        version = AssetVersion(
            asset_id=UUID(asset["id"]),
            version_no=1,
            object_key="legacy/private.image",
            checksum="d" * 64,
            metadata_json={"media": media},
            created_by="old-importer",
        )
        db.add(version)
        db.flush()
        version_id = str(version.id)
    records = client.get(root + "/rights").json()
    assert len(records) == 1
    assert records[0]["id"] == records[0]["asset_version_id"] == version_id
    assert records[0]["record_type"] == "ASSET_VERSION"
    assert records[0]["source_note"] == "Original source"
    assert records[0]["rights_status"] == ("CONFIRMED" if confirmation else "UNDECLARED")
    assert "private.image" not in str(records)
