"""Non-dialogue versions keep existing approval, exact selection, and lock semantics."""

from copy import deepcopy
from uuid import UUID, uuid4

from test_asset_versions import draft
from test_music_assets import (
    approve_music,
    create_music,
    import_music,
    music_source,
    music_url,
    output_file,
)

from arclight.persistence.models import AssetVersion
from arclight.storage.objects import ObjectStorage


def selection(asset):
    return {"expected_revision": asset["revision"], "expected_lock_version": asset["lock_version"]}


def test_music_unapproved_or_stale_versions_cannot_be_selected(client, episode):
    asset = create_music(client, episode)
    version = import_music(client, asset, music_source(client, episode))
    asset = client.get(music_url(asset)).json()
    url = music_url(asset, f"/versions/{version['id']}")
    assert client.post(url + "/select", json=selection(asset)).status_code == 409
    version = approve_music(client, asset, version)
    stale_revision = client.patch(
        url + "/state", json={"status": "STALE", "expected_revision": 0, "comment": "过期状态"}
    )
    assert stale_revision.status_code == 409
    stale = client.patch(
        url + "/state",
        json={"status": "STALE", "expected_revision": version["revision"], "comment": "素材已失效"},
    )
    assert stale.status_code == 200, stale.text
    assert client.post(url + "/select", json=selection(asset)).status_code == 409
    assert client.get(url).json()["stale"] is True


def test_music_selection_lock_cas_and_unlock_keep_history_when_object_is_missing(client, episode):
    asset = create_music(client, episode)
    version = approve_music(
        client, asset, import_music(client, asset, music_source(client, episode))
    )
    asset = client.get(music_url(asset)).json()
    url = music_url(asset, f"/versions/{version['id']}")
    selected = client.post(url + "/select", json=selection(asset))
    assert selected.status_code == 200, selected.text
    assert client.post(url + "/lock", json=selection(asset)).status_code == 409
    asset = selected.json()
    locked = client.post(url + "/lock", json=selection(asset))
    assert locked.status_code == 200, locked.text
    file = output_file(client, version)
    ObjectStorage(client.app.state.settings).delete(file.object_key)
    unlocked = client.post(url + "/unlock", json=selection(locked.json()))
    assert unlocked.status_code == 200, unlocked.text
    assert client.get(url).json()["status"] == "APPROVED"
    assert client.post(url + "/lock", json=selection(unlocked.json())).status_code == 409


def test_music_forged_asset_metadata_without_original_registration_cannot_be_reviewed(
    client, episode
):
    asset = create_music(client, episode)
    real = import_music(client, asset, music_source(client, episode))
    with client.app.state.session_factory.begin() as db:
        source = db.get(AssetVersion, UUID(real["id"]))
        forged = AssetVersion(
            id=uuid4(),
            asset_id=source.asset_id,
            version_no=2,
            object_key="forged-object",
            checksum=source.checksum,
            created_by=source.created_by,
            creation_key=uuid4(),
            status="DRAFT",
            metadata_json=deepcopy(source.metadata_json),
        )
        db.add(forged)
        db.flush()
        identity = forged.id
    response = client.patch(
        music_url(asset, f"/versions/{identity}/state"),
        json={"status": "READY_FOR_REVIEW", "expected_revision": 0},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "MUSIC_SOURCE_INVALID"
    assert client.get(music_url(asset, f"/versions/{identity}")).json()["status"] == "DRAFT"


def test_music_visual_version_creation_does_not_bypass_registered_sources(client, episode):
    asset = create_music(client, episode)
    response = client.post(music_url(asset, "/versions"), json=draft(asset))
    assert response.status_code == 404, response.text
    assert client.get(music_url(asset, "/versions")).json() == []


def test_music_current_selected_version_does_not_silently_change_on_new_import(client, episode):
    asset = create_music(client, episode)
    first = approve_music(client, asset, import_music(client, asset, music_source(client, episode)))
    asset = client.get(music_url(asset)).json()
    selected = client.post(
        music_url(asset, f"/versions/{first['id']}/select"), json=selection(asset)
    )
    assert selected.status_code == 200, selected.text
    asset = selected.json()
    second = import_music(client, asset, music_source(client, episode), "TRANSITION")
    assert second["status"] == "DRAFT"
    assert client.get(music_url(asset)).json()["current_version_id"] == first["id"]
    assert client.get(music_url(asset, f"/versions/{first['id']}")).json() == first
