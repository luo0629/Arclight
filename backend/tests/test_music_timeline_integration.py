"""Public music import flows into exact timeline and manifest contracts."""

from copy import deepcopy
from uuid import uuid4

import pytest
from render_manifest_helpers import manifest, manifest_url
from sqlalchemy import func, select
from test_continuity_reviews import review_case
from test_music_assets import (
    approve_music,
    create_music,
    import_music,
    music_source,
    music_url,
    output_file,
)
from test_timeline_saves import save_counts, save_url, saved_case, saved_timeline
from test_timelines import timeline_case

from arclight.persistence.render_manifests import RenderManifest
from arclight.storage.objects import ObjectStorage

__all__ = ["review_case", "saved_case", "timeline_case"]


def consumption_counts(client):
    with client.app.state.session_factory() as db:
        manifests = db.scalar(select(func.count()).select_from(RenderManifest))
    return (*save_counts(client), manifests)


def selected_music_case(client, episode, saved_case, role="BACKGROUND", *, selected=True):
    asset = create_music(client, episode)
    source_id = music_source(client, episode)
    version = approve_music(client, asset, import_music(client, asset, source_id, role))
    asset = client.get(music_url(asset)).json()
    if selected:
        response = client.post(
            music_url(asset, f"/versions/{version['id']}/select"),
            json={
                "expected_revision": asset["revision"],
                "expected_lock_version": asset["lock_version"],
            },
        )
        assert response.status_code == 200, response.text
        asset = response.json()
    original, saved_body, _ = saved_case
    body = deepcopy(saved_body)
    kind = "MUSIC" if role == "BACKGROUND" else "SFX"
    track = next(track for track in body["snapshot"]["tracks"] if track["kind"] == kind)
    track["clips"] = [
        {
            "id": str(uuid4()),
            "ordinal": 0,
            "source_asset_version_id": version["id"],
            "source_sha256": version["checksum"],
            "start_ms": 0,
            "duration_ms": 100,
            "audio": {
                "purpose_role": role,
                "gain_milli": 750,
                "fade_in_ms": 20,
                "fade_out_ms": 30,
                "envelope": [
                    {"offset_ms": 0, "gain_milli": 1000},
                    {"offset_ms": 40, "gain_milli": 500},
                    {"offset_ms": 100, "gain_milli": 1000},
                ],
            },
        }
    ]
    return original, body, asset, version, source_id, track["clips"][0]


@pytest.mark.parametrize("role", ["BACKGROUND", "AMBIENCE", "TRANSITION"])
def test_public_music_source_and_automation_are_frozen_in_manifest(
    client, episode, saved_case, role
):
    original, body, _, version, source_id, clip = selected_music_case(
        client, episode, saved_case, role
    )
    timeline = saved_timeline(client, original, body)
    frozen, _ = manifest(client, timeline)
    source = next(
        source
        for source in frozen["snapshot"]["sources"]
        if source["asset_version_id"] == version["id"]
    )
    output = output_file(client, version)
    assert str(output.id) != source_id
    assert source["asset_type"] == "MUSIC"
    assert source["file"]["sha256"] == version["checksum"]
    assert source["file"]["source_note"] == version["media"]["source_note"]
    assert source["file"]["rights_note"] == version["media"]["rights_note"]
    assert source["file"]["rights_confirmed"] is True
    frozen_clip = next(
        value
        for track in frozen["snapshot"]["timeline"]["tracks"]
        for value in track["clips"]
        if value["source_asset_version_id"] == version["id"]
    )
    assert frozen_clip["audio"] == clip["audio"]
    assert frozen["snapshot"]["timeline"] == timeline["snapshot"]
    edited = deepcopy(timeline["snapshot"])
    next(
        value
        for track in edited["tracks"]
        for value in track["clips"]
        if value["source_asset_version_id"] == version["id"]
    )["audio"]["gain_milli"] = 250
    saved_timeline(
        client,
        timeline,
        {
            "creation_key": str(uuid4()),
            "expected_version_no": timeline["version_no"],
            "snapshot": edited,
        },
    )
    assert client.get(f"/api/v1/render-manifests/{frozen['id']}").json() == frozen
    assert client.get(save_url(timeline)).json() == timeline


def test_approved_music_requires_explicit_selection_before_timeline_save(
    client, episode, saved_case
):
    original, body, _, _, _, _ = selected_music_case(client, episode, saved_case, selected=False)
    before = consumption_counts(client)
    response = client.patch(save_url(original), json=body)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ASSET_NOT_SELECTED"
    assert consumption_counts(client) == before


@pytest.mark.parametrize(
    "case", ["missing_configuration", "wrong_role", "source_start", "duration"]
)
def test_registered_music_rejects_wrong_purpose_and_out_of_source_ranges(
    client, episode, saved_case, case
):
    original, body, _, _, _, clip = selected_music_case(client, episode, saved_case, "AMBIENCE")
    expected = "TIMELINE_AUDIO_ROLE_INVALID"
    if case == "missing_configuration":
        del clip["audio"]
    elif case == "wrong_role":
        clip["audio"]["purpose_role"] = "TRANSITION"
    else:
        expected = "TIMELINE_SOURCE_RANGE_INVALID"
        if case == "source_start":
            clip["source_start_ms"] = 1
        else:
            clip["duration_ms"] = 101
            clip["audio"]["envelope"][-1]["offset_ms"] = 101
    before = consumption_counts(client)
    response = client.patch(save_url(original), json=body)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == expected
    assert consumption_counts(client) == before


def test_stale_music_cannot_enter_a_new_timeline(client, episode, saved_case):
    original, body, asset, version, _, _ = selected_music_case(client, episode, saved_case)
    changed = client.patch(
        music_url(asset, f"/versions/{version['id']}/state"),
        json={
            "status": "STALE",
            "expected_revision": version["revision"],
            "comment": "The score must be replaced",
        },
    )
    assert changed.status_code == 200, changed.text
    before = consumption_counts(client)
    response = client.patch(save_url(original), json=body)
    assert response.status_code == 409, response.text
    assert consumption_counts(client) == before


@pytest.mark.parametrize("entrypoint", ["timeline", "manifest"])
def test_damaged_music_object_blocks_new_consumption_and_keeps_frozen_history(
    client, episode, saved_case, entrypoint
):
    original, body, _, version, _, _ = selected_music_case(client, episode, saved_case)
    timeline = saved_timeline(client, original, body)
    frozen, _ = manifest(client, timeline)
    output = output_file(client, version)
    ObjectStorage(client.app.state.settings).path(output.object_key).write_bytes(b"damaged")
    before = consumption_counts(client)
    if entrypoint == "timeline":
        response = client.patch(
            save_url(timeline),
            json={
                "creation_key": str(uuid4()),
                "expected_version_no": timeline["version_no"],
                "snapshot": timeline["snapshot"],
            },
        )
    else:
        response = client.post(manifest_url(timeline), json={"creation_key": str(uuid4())})
    assert response.status_code == 409, response.text
    assert consumption_counts(client) == before
    assert client.get(f"/api/v1/render-manifests/{frozen['id']}").json() == frozen
    assert client.get(save_url(timeline)).json() == timeline


@pytest.mark.parametrize("change", ["stale", "selection-replaced"])
def test_changed_music_admission_blocks_new_manifest_and_preserves_frozen_history(
    client, episode, saved_case, change
):
    original, body, asset, version, _, _ = selected_music_case(client, episode, saved_case)
    timeline = saved_timeline(client, original, body)
    frozen, frozen_body = manifest(client, timeline)
    replacement = None
    if change == "selection-replaced":
        current = client.get(music_url(asset)).json()
        replacement = approve_music(
            client,
            current,
            import_music(client, current, music_source(client, episode)),
        )
    preview = client.post(
        f"/api/v1/projects/{asset['project_id']}/dependencies/impact",
        json={
            "changes": [
                {
                    "source_asset_version_id": version["id"],
                    "expected_revision": version["revision"],
                    "change_kind": "LIFECYCLE" if change == "stale" else "CONTENT",
                    "selectors": [],
                }
            ]
        },
    )
    assert preview.status_code == 200, preview.text
    digest = preview.json()["digest"]
    if replacement is None:
        changed = client.patch(
            music_url(asset, f"/versions/{version['id']}/state"),
            json={
                "status": "STALE",
                "expected_revision": version["revision"],
                "comment": "The approved score is no longer usable",
                "impact_digest": digest,
            },
        )
    else:
        current = client.get(music_url(asset)).json()
        changed = client.post(
            music_url(asset, f"/versions/{replacement['id']}/select"),
            json={
                "expected_revision": current["revision"],
                "expected_lock_version": current["lock_version"],
                "impact_digest": digest,
            },
        )
    assert changed.status_code == 200, changed.text
    before = consumption_counts(client)
    response = client.post(manifest_url(timeline), json={"creation_key": str(uuid4())})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == (
        "MUSIC_VERSION_NOT_USABLE" if change == "stale" else "ASSET_NOT_SELECTED"
    )
    assert consumption_counts(client) == before
    replayed, _ = manifest(client, timeline, **frozen_body)
    assert replayed == frozen
    assert consumption_counts(client) == before
    assert client.get(f"/api/v1/render-manifests/{frozen['id']}").json() == frozen
    historical = client.get(save_url(timeline))
    assert historical.status_code == 200, historical.text
    historical = historical.json()
    assert historical["snapshot"] == timeline["snapshot"]
    assert historical["content_sha256"] == timeline["content_sha256"]
    assert historical["stale"] is True
