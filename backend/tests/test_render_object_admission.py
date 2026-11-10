"""Admission reads real media again, including after a durable export approval."""

from copy import deepcopy
from uuid import UUID

import pytest
from render_admission_helpers import (
    assert_blocked,
    edited,
    ready_manifest,
    render_path,
    request_export,
)
from render_manifest_helpers import audio_case, render_case, review_case, stored_file, timeline_case
from test_human_reviews import decide, resume
from test_media_validation import wave_bytes
from test_storage_cleanup import gc_setup

from arclight.application.render_inputs import file_storage
from arclight.persistence.models import Shot, StoredFile
from arclight.providers.text import MockTextProvider

__all__ = ["audio_case", "gc_setup", "render_case", "review_case", "timeline_case"]


@pytest.fixture
def object_audio_case(gc_setup, request):
    # Configure the isolated local/S3 backend before creating any source media.
    return request.getfixturevalue("audio_case")


@pytest.mark.parametrize("kind", ["VOICE", "SFX", "MUSIC"])
def test_matching_file_receipt_does_not_admit_a_truncated_audio_object(
    client, episode, object_audio_case, kind
):
    timeline, font, _ = object_audio_case
    _, identity, checksum = stored_file(
        client, episode["project_id"], wave_bytes()[:-1], "audio/wav", kind="AUDIO"
    )
    snapshot = deepcopy(timeline["snapshot"])
    clip = next(track for track in snapshot["tracks"] if track["kind"] == kind)["clips"][0]
    clip.update(source_asset_version_id=str(identity), source_sha256=checksum)
    if kind == "VOICE":
        snapshot["tracks"][4]["clips"][0]["audio_version_id"] = str(identity)
    timeline = edited(client, timeline, snapshot)
    value, _ = ready_manifest(client, timeline, font_file_id=str(font))
    # The real hash and size match the immutable receipt; only decoding exposes damage.
    for operation in ("admission", "export-review"):
        assert_blocked(client.post(render_path(value, operation)), "INVALID_FILE")


@pytest.mark.parametrize("target", ["audio", "font"])
@pytest.mark.parametrize("problem", ["missing", "replaced"])
def test_approved_export_rechecks_audio_and_font_objects(
    client, object_audio_case, target, problem
):
    timeline, font, (audio, _) = object_audio_case
    value, _ = ready_manifest(client, timeline, profile="FORMAL", font_file_id=str(font))
    review = request_export(client, value)
    decision = decide(client, review)
    assert decision.status_code == 200, decision.text
    receipt = resume(client, review, decision.json())
    assert receipt.status_code == 200, receipt.text
    assert client.post(render_path(value, "admission")).status_code == 200
    with client.app.state.session_factory() as db:
        row = db.get(StoredFile, audio if target == "audio" else font)
        store = file_storage(client.app.state.settings, row)
        if problem == "missing":
            store.delete(row.object_key)
        else:
            replacement = bytes(row.byte_size)
            if store.client is None:
                store.path(row.object_key).write_bytes(replacement)
            else:
                # Simulate external replacement without altering the stored receipt.
                store.client.put_object(
                    Bucket=store.settings.s3_bucket,
                    Key=row.object_key,
                    Body=replacement,
                    ContentType=row.media_type,
                )
    assert_blocked(client.post(render_path(value, "admission")), "OBJECT_UNAVAILABLE")
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value
    current = client.get(f"/api/v1/human-reviews/{review['id']}").json()
    assert current["resume"]["result_json"] == receipt.json()["result_json"]


@pytest.fixture
def configured_sound_storyboard(monkeypatch):
    original = MockTextProvider.storyboard

    def storyboard(self, script, duration_ms):
        content = original(self, script, duration_ms)
        first = content.shots[0].model_copy(update={"sound_effects": ["脚步声", "雨声"]})
        return content.model_copy(update={"shots": [first, *content.shots[1:]]})

    monkeypatch.setattr(MockTextProvider, "storyboard", storyboard)


@pytest.fixture
def sound_case(configured_sound_storyboard, request):
    # Only the offline provider's initial response changes; all persistence,
    # storyboard approvals, image reviews and selection use production code.
    return request.getfixturevalue("audio_case")


@pytest.mark.parametrize("sound_shot", [None, 1, 0], ids=["unbound", "wrong-shot", "exact-shot"])
def test_persisted_required_sound_effects_use_the_exact_shot(client, sound_case, sound_shot):
    timeline, font, _ = sound_case
    snapshot = deepcopy(timeline["snapshot"])
    required_shot = snapshot["tracks"][0]["clips"][0]["shot_id"]
    with client.app.state.session_factory() as db:
        assert db.get(Shot, UUID(required_shot)).content_json["sound_effects"] == [
            "脚步声",
            "雨声",
        ]
    if sound_shot is not None:
        snapshot["tracks"][2]["clips"][0]["shot_id"] = snapshot["tracks"][0]["clips"][sound_shot][
            "shot_id"
        ]
        timeline = edited(client, timeline, snapshot)
    value, _ = ready_manifest(client, timeline, font_file_id=str(font))
    response = client.post(render_path(value, "admission"))
    if sound_shot == 0:
        assert response.status_code == 200, response.text
    else:
        assert_blocked(response, "RENDER_SFX_INCOMPLETE")
