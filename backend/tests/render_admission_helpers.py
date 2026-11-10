"""Exact creative decisions and API helpers for render preflight integration tests."""

from uuid import UUID, uuid4

from render_manifest_helpers import manifest
from test_timeline_saves import saved_timeline

from arclight.persistence.models import Script, Storyboard


def edited(client, timeline, snapshot):
    return saved_timeline(
        client,
        timeline,
        {
            "creation_key": str(uuid4()),
            "expected_version_no": timeline["version_no"],
            "snapshot": snapshot,
        },
    )


def line_decisions(client, timeline):
    voiced = {
        (clip["script_version_id"], clip["line_id"])
        for track in timeline["snapshot"]["tracks"]
        if track["kind"] == "VOICE"
        for clip in track["clips"]
    }
    with client.app.state.session_factory() as db:
        board = db.get(Storyboard, UUID(timeline["snapshot"]["storyboard_id"]))
        script = db.get(Script, board.script_version_id)
        return [
            {
                "script_version_id": str(script.id),
                "line_id": line["line_id"],
                "mode": "SILENT",
                "reason": "本次静态预演由创作者明确选择静默呈现",
            }
            for scene in script.content_json["scenes"]
            for line in scene["dialogue"]
            if (str(script.id), line["line_id"]) not in voiced
        ]


def ready_manifest(client, timeline, **overrides):
    return manifest(
        client, timeline, line_audio_decisions=line_decisions(client, timeline), **overrides
    )


def render_path(value, suffix):
    return f"/api/v1/render-manifests/{value['id']}/{suffix}"


def request_export(client, value):
    response = client.post(render_path(value, "export-review"))
    assert response.status_code == 201, response.text
    return response.json()


def assert_blocked(response, code):
    assert response.status_code in {403, 404, 409, 422}, response.text
    assert response.json()["error"]["code"] == code, response.text
