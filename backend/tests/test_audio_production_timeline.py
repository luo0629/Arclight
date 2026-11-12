"""New audio provenance joins the existing timeline save path without replacing it."""

from copy import deepcopy
from uuid import UUID, uuid4

from render_manifest_helpers import render_case, review_case, timeline_case
from test_audio_production import output_audio, prepare, snapshot

from arclight.persistence.models import AssetVersion, Script, Storyboard

__all__ = ["render_case", "review_case", "timeline_case"]


def test_timeline_requires_new_audio_binding_and_rejects_cross_line_reuse(
    client, episode, render_case
):
    with client.app.state.session_factory() as db:
        board = db.get(Storyboard, UUID(render_case["snapshot"]["storyboard_id"]))
        script = db.get(Script, board.script_version_id)
        script_data = {"id": str(script.id)}
    response = snapshot(client, episode, script_data)
    assert response.status_code == 201, response.text
    row = response.json()
    lines = client.get(f"/api/v1/audio-snapshots/{row['id']}/lines").json()
    assert len(lines) >= 2
    source = episode, script_data, row, lines
    url, asset, manifest = prepare(client, source)
    identity = output_audio(client, source, asset, manifest)
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, UUID(identity))
        version.status = "APPROVED"
        checksum = version.checksum
    timeline = deepcopy(render_case["snapshot"])
    voice = next(track for track in timeline["tracks"] if track["kind"] == "VOICE")
    voice["clips"] = [
        {
            "id": str(uuid4()),
            "ordinal": 0,
            "start_ms": 0,
            "duration_ms": 100,
            "source_asset_version_id": identity,
            "source_sha256": checksum,
            "script_version_id": script_data["id"],
            "line_id": lines[0]["line_id"],
        }
    ]
    body = {
        "creation_key": str(uuid4()),
        "expected_version_no": render_case["version_no"],
        "snapshot": timeline,
    }
    save_url = f"/api/v1/timelines/{render_case['id']}"
    response = client.patch(save_url, json=body)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "AUDIO_BINDING_REQUIRED"
    assert client.post(url + "/bindings", json={"asset_version_id": identity}).status_code == 201
    voice["clips"][0]["line_id"] = lines[1]["line_id"]
    assert client.patch(save_url, json=body).status_code == 409
    voice["clips"][0]["line_id"] = lines[0]["line_id"]
    response = client.patch(save_url, json=body)
    assert response.status_code == 201, response.text
    assert client.get(f"/api/v1/timelines/{render_case['id']}").json() == render_case
