"""Mixed line history and published exact inputs survive a new audio candidate."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from render_manifest_helpers import manifest, render_case, review_case, timeline_case
from sqlalchemy import select
from test_audio_import import completed_source, import_request
from test_audio_production import output_audio, prepare, snapshot, wave_bytes
from test_audio_regeneration import regeneration_case, registered_audio, tts_case
from test_audio_tts_boundaries import boundary_state, voice_version
from test_generation_budget import set_policy
from test_model_services import account
from test_native_media_integration import configure
from test_text_contract_v2 import save_script

from arclight.application.render_inputs import file_storage
from arclight.persistence.models import (
    AssetVersion,
    Project,
    ProjectMember,
    Script,
    StoredFile,
    Storyboard,
)

__all__ = ["tts_case", "render_case", "review_case", "timeline_case"]


def imported_audio(client, case, source_file, *, target=None, revision=0, index=0):
    source = case["episode"], case["script"], case["snapshot"], case["lines"]
    url, payload = import_request(
        source, target, source_file, index=index, expected_revision=revision
    )
    if target is None:
        del payload["target_asset_id"]
    response = client.post(url, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_line_history_combines_import_and_generated_versions_with_exact_lineage_and_pagination(
    client, settings, tts_case
):
    first, first_row, _, _ = registered_audio(client, settings, tts_case)
    source = tts_case["episode"], tts_case["script"], tts_case["snapshot"], tts_case["lines"]
    file_id = completed_source(client, source)
    imported = imported_audio(
        client, tts_case, file_id, target=first_row["target_asset_id"], revision=1
    )
    imported_source = {
        "binding": {"asset_version_id": imported["asset_version_id"]},
        "asset_revision": imported["asset_revision"],
    }
    third, third_row, _, _ = registered_audio(
        client, settings, regeneration_case(tts_case, imported_source)
    )
    root = tts_case["url"].removesuffix("/tts")
    response = client.get(root + "/history")
    assert response.status_code == 200, response.text
    history = response.json()
    versions = [
        first["binding"]["asset_version_id"],
        imported["asset_version_id"],
        third["binding"]["asset_version_id"],
    ]
    assert [item["version"]["id"] for item in history] == versions
    assert [item["version"]["version_no"] for item in history] == [1, 2, 3]
    assert [item["tts_job_id"] for item in history] == [
        first_row["job_id"],
        None,
        third_row["job_id"],
    ]
    assert [item["source_audio_version_id"] for item in history] == [
        None,
        None,
        imported["asset_version_id"],
    ]
    assert [item["binding"] for item in history] == client.get(root + "/bindings").json()
    version_root = (
        f"/api/v1/projects/{tts_case['pid']}/assets/{first_row['target_asset_id']}/versions"
    )
    listed = client.get(version_root)
    assert listed.status_code == 200, listed.text
    assert [item["version"] for item in history] == listed.json()
    assert all(item["version"]["media"]["rights_confirmed"] for item in history)
    assert all(item["version"]["status"] == "DRAFT" for item in history)
    assert "api_key" not in response.text and "first-key" not in response.text
    paginated = [
        client.get(root + "/history", params={"limit": 1, "offset": offset}).json()
        for offset in range(4)
    ]
    assert paginated == [[history[0]], [history[1]], [history[2]], []]
    assert client.get(root + "/history", params={"limit": 2, "offset": 1}).json() == history[1:]
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": 100001}):
        assert client.get(root + "/history", params=params).status_code == 422
    assert (
        client.get(
            root.replace(tts_case["lines"][0]["line_id"], "missing") + "/history"
        ).status_code
        == 404
    )


def test_regenerating_one_line_preserves_existing_other_line_and_other_episode_audio(
    client, settings, tts_case
):
    first, _, _, _ = registered_audio(client, settings, tts_case)
    source = tts_case["episode"], tts_case["script"], tts_case["snapshot"], tts_case["lines"]
    file_id = completed_source(client, source)
    imported_audio(client, tts_case, file_id, index=1)
    response = client.post(
        f"/api/v1/projects/{tts_case['pid']}/episodes",
        json={"episode_no": 2, "title": "后续剧集", "target_duration_ms": 30000},
    )
    assert response.status_code == 201, response.text
    episode = response.json()
    script = save_script(client, episode)
    response = snapshot(client, episode, script)
    assert response.status_code == 201, response.text
    frozen = response.json()
    lines = client.get(f"/api/v1/audio-snapshots/{frozen['id']}/lines").json()
    next_case = {
        **tts_case,
        "episode": episode,
        "script": script,
        "snapshot": frozen,
        "lines": lines,
    }
    imported_audio(client, next_case, file_id)
    other_line_url = (
        tts_case["url"]
        .removesuffix("/tts")
        .replace(tts_case["lines"][0]["line_id"], tts_case["lines"][1]["line_id"])
        + "/history"
    )
    other_episode_url = (
        f"/api/v1/audio-snapshots/{frozen['id']}/lines/{lines[0]['line_id']}/history"
    )
    before = [client.get(path).json() for path in (other_line_url, other_episode_url)]
    assert all(len(values) == 1 for values in before)
    registered_audio(client, settings, regeneration_case(tts_case, first))
    assert [client.get(path).json() for path in (other_line_url, other_episode_url)] == before


def test_audio_history_rechecks_membership_and_preserves_archived_but_hides_deleted_projects(
    client, settings, tts_case
):
    registered_audio(client, settings, tts_case)
    path = tts_case["url"].removesuffix("/tts") + "/history"
    original = client.get(path).json()
    owner_header = client.headers["Authorization"]
    member = account(client)
    assert client.get(path).status_code == 404
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(tts_case["pid"]), user_id=UUID(member), role="viewer"
        )
        db.add(membership)
        db.flush()
        member_id = membership.id
    assert client.get(path).json() == original
    with client.app.state.session_factory.begin() as db:
        db.delete(db.get(ProjectMember, member_id))
    assert client.get(path).status_code == 404
    client.headers["Authorization"] = owner_header
    project_url = f"/api/v1/projects/{tts_case['pid']}"
    project = client.get(project_url).json()
    archived = client.patch(project_url, json={"archived": True})
    assert archived.status_code == 200, archived.text
    assert client.get(path).json() == original
    deleted = client.request("DELETE", project_url, json={"confirm_name": project["name"]})
    assert deleted.status_code == 200, deleted.text
    assert client.get(path).status_code == 404


def test_history_reads_have_no_generation_budget_or_lifecycle_side_effects(
    client, settings, tts_case
):
    source, _, _, _ = registered_audio(client, settings, tts_case)
    path = tts_case["url"].removesuffix("/tts") + "/history"
    before = boundary_state(client)
    for _ in range(3):
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.json()[0]["version"]["id"] == source["binding"]["asset_version_id"]
    assert boundary_state(client) == before


@pytest.mark.parametrize("kind", ["list", "dict", "invalid", "other_job"])
def test_history_does_not_trust_legacy_metadata_as_a_tts_request(
    client, settings, tts_case, monkeypatch, kind
):
    _, original, _, _ = registered_audio(client, settings, tts_case)
    source = tts_case["episode"], tts_case["script"], tts_case["snapshot"], tts_case["lines"]
    url, target, manifest_id = prepare(client, source)
    job_id = {
        "list": [original["job_id"]],
        "dict": {"id": original["job_id"]},
        "invalid": "not-a-job-id",
        "other_job": original["job_id"],
    }[kind]

    def legacy_version(*args, **kwargs):
        # Construct an older producer's metadata before insertion; never rewrite history.
        kwargs["metadata_json"] = {
            **kwargs["metadata_json"],
            "audio_tts": {"job_id": job_id},
        }
        return AssetVersion(*args, **kwargs)

    monkeypatch.setattr("test_audio_production.AssetVersion", legacy_version)
    version_id = output_audio(client, source, target, manifest_id)
    bound = client.post(url + "/bindings", json={"asset_version_id": version_id})
    assert bound.status_code == 201, bound.text
    response = client.get(url + "/history")
    assert response.status_code == 200, response.text
    row = next(value for value in response.json() if value["version"]["id"] == version_id)
    assert row["tts_job_id"] is None
    assert row["source_audio_version_id"] is None
    assert "api_key" not in response.text and "first-key" not in response.text


def test_regeneration_preserves_approved_selected_audio_and_frozen_timeline_and_render_manifest(
    client, settings, episode, render_case
):
    actor = account(client)
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).owner_id = actor
        board = db.get(Storyboard, UUID(render_case["snapshot"]["storyboard_id"]))
        script = db.get(Script, board.script_version_id)
        script_data = {"id": str(script.id), "content_json": deepcopy(script.content_json)}
    configure(client, settings, "speech", "openai_tts")
    set_policy(client, episode["project_id"], "20")
    response = snapshot(client, episode, script_data)
    assert response.status_code == 201, response.text
    frozen = response.json()
    lines = client.get(f"/api/v1/audio-snapshots/{frozen['id']}/lines").json()
    voice = voice_version(
        client,
        episode["project_id"],
        lines[0]["speaker"],
        parameters={"voice": "test-voice", "speed": 1.0},
    )
    case = {
        "pid": episode["project_id"],
        "episode": episode,
        "script": script_data,
        "snapshot": frozen,
        "lines": lines,
        "voice": voice,
        "url": f"/api/v1/audio-snapshots/{frozen['id']}/lines/{lines[0]['line_id']}/tts",
        "payload": {"creation_key": str(uuid4()), "voice_version_id": voice["id"]},
    }
    original, row, _, audio = registered_audio(client, settings, case)
    asset_url = f"/api/v1/projects/{case['pid']}/assets/{row['target_asset_id']}"
    version_url = asset_url + f"/versions/{original['binding']['asset_version_id']}"
    for status in ("READY_FOR_REVIEW", "APPROVED"):
        current = client.get(version_url).json()
        response = client.patch(
            version_url + "/state",
            json={"status": status, "expected_revision": current["revision"]},
        )
        assert response.status_code == 200, response.text
    asset = client.get(asset_url).json()
    response = client.post(
        version_url + "/select",
        json={
            "expected_revision": asset["revision"],
            "expected_lock_version": asset["lock_version"],
        },
    )
    assert response.status_code == 200, response.text
    selected = response.json()
    approved_version = client.get(version_url).json()
    timeline_snapshot = deepcopy(render_case["snapshot"])
    voice_track = next(track for track in timeline_snapshot["tracks"] if track["kind"] == "VOICE")
    voice_track["clips"] = [
        {
            "id": str(uuid4()),
            "ordinal": 0,
            "start_ms": 0,
            "duration_ms": original["binding"]["duration_ms"],
            "source_asset_version_id": original["binding"]["asset_version_id"],
            "source_sha256": approved_version["checksum"],
            "script_version_id": script_data["id"],
            "line_id": lines[0]["line_id"],
        }
    ]
    response = client.patch(
        f"/api/v1/timelines/{render_case['id']}",
        json={
            "creation_key": str(uuid4()),
            "expected_version_no": render_case["version_no"],
            "snapshot": timeline_snapshot,
        },
    )
    assert response.status_code == 201, response.text
    timeline = response.json()
    render, _ = manifest(client, timeline)
    replacement, replacement_row, _, _ = registered_audio(
        client,
        settings,
        regeneration_case(case, original, expected_revision=selected["revision"]),
    )
    assert replacement_row["target_asset_id"] == row["target_asset_id"]
    assert client.get(version_url).json() == approved_version
    current_asset = client.get(asset_url).json()
    assert current_asset["current_version_id"] == original["binding"]["asset_version_id"]
    assert current_asset["lock_version"] == selected["lock_version"]
    assert client.get(f"/api/v1/timelines/{timeline['id']}").json() == timeline
    assert client.get(f"/api/v1/timelines/{render_case['id']}").json() == render_case
    assert client.get(f"/api/v1/render-manifests/{render['id']}").json() == render
    assert any(
        source["asset_version_id"] == original["binding"]["asset_version_id"]
        for source in render["snapshot"]["sources"]
    )
    assert all(
        source["asset_version_id"] != replacement["binding"]["asset_version_id"]
        for source in render["snapshot"]["sources"]
    )
    replacement_url = asset_url + f"/versions/{replacement['binding']['asset_version_id']}"
    assert client.get(replacement_url).json()["status"] == "DRAFT"
    denied = client.post(
        replacement_url + "/select",
        json={
            "expected_revision": current_asset["revision"],
            "expected_lock_version": current_asset["lock_version"],
        },
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "ASSET_NOT_USABLE"
    with client.app.state.session_factory() as db:
        for version_id in (
            original["binding"]["asset_version_id"],
            replacement["binding"]["asset_version_id"],
        ):
            version = db.get(AssetVersion, UUID(version_id))
            file = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version.id))
            assert (
                file_storage(settings, file).read(file.object_key, file.byte_size, file.checksum)
                == audio
            )
    assert audio == wave_bytes()
