"""Audio history reuses explicit approval, selection and locking without visual writes."""

import hashlib
import io
import wave
from uuid import UUID, uuid4

import av
import pytest
from render_manifest_helpers import stored_file
from sqlalchemy import select
from test_asset_versions import draft
from test_audio_production import output_audio, prepare, wave_bytes
from test_audio_regeneration import regeneration_case, registered_audio, tts_case
from test_audio_tts import declaration, finish_audio
from test_audio_tts_boundaries import approve_changed_line, boundary_state
from test_audio_tts_output import output_voice, wave_data
from test_media_validation import wave_bytes as silent_wave
from test_model_services import account

from arclight.application.dependencies import attach_output
from arclight.application.render_inputs import file_storage
from arclight.persistence.audio_production import AudioLineBinding
from arclight.persistence.models import AssetVersion, ProjectMember, StoredFile
from arclight.storage.objects import ObjectStorage

__all__ = ["tts_case"]


def audio_urls(case, asset_id, version_id):
    asset = f"/api/v1/projects/{case['pid']}/assets/{asset_id}"
    return asset, asset + f"/versions/{version_id}"


def read_ok(client, url):
    response = client.get(url)
    assert response.status_code == 200, response.text
    return response.json()


def selection_payload(asset):
    return {
        "expected_revision": asset["revision"],
        "expected_lock_version": asset["lock_version"],
    }


def approve_audio(client, url):
    for status in ("READY_FOR_REVIEW", "APPROVED"):
        current = read_ok(client, url)
        response = client.patch(
            url + "/state", json={"status": status, "expected_revision": current["revision"]}
        )
        assert response.status_code == 200, response.text
    return read_ok(client, url)


def select_audio(client, asset_url, version_url):
    response = client.post(
        version_url + "/select", json=selection_payload(read_ok(client, asset_url))
    )
    assert response.status_code == 200, response.text
    return response.json()


def legacy_candidate(client, case, *, data=None, bind=False, rights=True):
    """Insert an older producer's exact facts once, including deliberately invalid media."""
    source = case["episode"], case["script"], case["snapshot"], case["lines"]
    _, target, manifest = prepare(client, source)
    version_id = output_audio(client, source, target, manifest, data=data, rights=rights)
    if bind:
        with client.app.state.session_factory.begin() as db:
            file = db.scalar(
                select(StoredFile).where(StoredFile.asset_version_id == UUID(version_id))
            )
            db.add(
                AudioLineBinding(
                    snapshot_id=UUID(case["snapshot"]["id"]),
                    line_id=case["lines"][0]["line_id"],
                    asset_version_id=UUID(version_id),
                    dependency_manifest_id=UUID(manifest),
                    file_id=file.id,
                    duration_ms=100,
                    created_by=case["voice"]["created_by"],
                )
            )
    return audio_urls(case, target, version_id)


def test_large_bound_audio_uses_audio_limits_for_explicit_approval_selection_and_locking(
    client, settings, tts_case
):
    data = wave_bytes()
    with wave.open(io.BytesIO(data), "rb") as original:
        params = original.getparams()
        frames = original.readframes(original.getnframes())
    output = io.BytesIO()
    with wave.open(output, "wb") as expanded:
        expanded.setparams(params)
        expanded.writeframes(frames * 300)
    large_audio = output.getvalue()
    assert len(large_audio) > 1024 * 1024
    request, _, _ = finish_audio(client, settings, tts_case, data=large_audio)
    response = client.post(tts_case["url"] + f"/{request['job_id']}/result", json=declaration())
    assert response.status_code == 201, response.text
    receipt = response.json()
    asset_url, version_url = audio_urls(
        tts_case, request["target_asset_id"], receipt["binding"]["asset_version_id"]
    )
    asset = read_ok(client, asset_url)
    assert asset["asset_type"] == "AUDIO"
    assert asset["metadata"]["audio_snapshot_id"] == tts_case["snapshot"]["id"]
    assert asset["metadata"]["line_id"] == tts_case["lines"][0]["line_id"]
    assert asset["current_version_id"] is None
    version = approve_audio(client, version_url)
    assert version["status"] == "APPROVED"
    selected = select_audio(client, asset_url, version_url)
    assert selected["current_version_id"] == receipt["binding"]["asset_version_id"]
    locked = client.post(version_url + "/lock", json=selection_payload(selected))
    assert locked.status_code == 200, locked.text
    assert locked.json()["status"] == "LOCKED"
    assert read_ok(client, version_url)["status"] == "LOCKED"
    assert [item["id"] for item in read_ok(client, asset_url + "/versions")] == [
        receipt["binding"]["asset_version_id"]
    ]


def flac_audio(data):
    output = io.BytesIO()
    with (
        av.open(io.BytesIO(data)) as source,
        av.open(output, "w", format="flac") as target,
    ):
        rate = source.streams.audio[0].sample_rate
        stream = target.add_stream("flac", rate=rate)
        stream.layout = "mono"
        for frame in source.decode(audio=0):
            for packet in stream.encode(frame):
                target.mux(packet)
        for packet in stream.encode(None):
            target.mux(packet)
    return output.getvalue()


def registered_flac(client, settings, case):
    output_voice(client, settings, case, {"format": "flac", "channels": 1, "sample_rate": 24000})
    data = flac_audio(wave_data())
    row, _, _ = finish_audio(client, settings, case, data=data)
    response = client.post(case["url"] + f"/{row['job_id']}/result", json=declaration())
    assert response.status_code == 201, response.text
    receipt = response.json()
    validation = receipt["validation"]
    assert validation["media_type"] == "audio/flac" and validation["codec"] == "flac"
    assert validation["sample_rate"] == 24000 and validation["channels"] == 1
    assert validation["silence"]["silent"] is False
    return row, receipt


def test_real_generated_flac_can_be_reviewed_selected_and_locked(client, settings, tts_case):
    row, receipt = registered_flac(client, settings, tts_case)
    asset_url, version_url = audio_urls(
        tts_case, row["target_asset_id"], receipt["binding"]["asset_version_id"]
    )
    assert read_ok(client, asset_url)["current_version_id"] is None
    assert approve_audio(client, version_url)["status"] == "APPROVED"
    selected = select_audio(client, asset_url, version_url)
    assert selected["current_version_id"] == receipt["binding"]["asset_version_id"]
    response = client.post(version_url + "/lock", json=selection_payload(selected))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "LOCKED"


def test_generated_flac_still_requires_current_rights_before_review(client, settings, tts_case):
    source = tts_case["episode"], tts_case["script"], tts_case["snapshot"], tts_case["lines"]
    _, target, manifest = prepare(client, source)
    data = flac_audio(wave_data())
    checksum = hashlib.sha256(data).hexdigest()
    key = f"audio-history-tests/{tts_case['pid']}/{uuid4()}.flac"
    ObjectStorage(settings).put(key, data, "audio/flac")
    # Older producer facts are inserted once; immutable file rights are never rewritten.
    with client.app.state.session_factory.begin() as db:
        version = AssetVersion(
            asset_id=UUID(target),
            version_no=1,
            object_key=key,
            checksum=checksum,
            status="DRAFT",
            created_by="service-owner",
            metadata_json={"dependency_manifest_id": manifest},
        )
        db.add(version)
        db.flush()
        attach_output(db, UUID(tts_case["pid"]), UUID(manifest), version)
        file = StoredFile(
            project_id=UUID(tts_case["pid"]),
            asset_version_id=version.id,
            storage_backend=settings.object_storage_backend.upper(),
            bucket=settings.s3_bucket if settings.object_storage_backend == "s3" else None,
            object_key=key,
            checksum=checksum,
            byte_size=len(data),
            media_type="audio/flac",
            original_name="unlicensed.flac",
            source_note="Original offline FLAC fixture",
            rights_note="Usage rights have not been confirmed",
            rights_confirmed=False,
            created_by="service-owner",
        )
        db.add(file)
        db.flush()
        db.add(
            AudioLineBinding(
                snapshot_id=UUID(tts_case["snapshot"]["id"]),
                line_id=tts_case["lines"][0]["line_id"],
                asset_version_id=version.id,
                dependency_manifest_id=UUID(manifest),
                file_id=file.id,
                duration_ms=100,
                created_by="service-owner",
            )
        )
        asset_url, version_url = audio_urls(tts_case, target, version.id)
    before = boundary_state(client)
    response = client.patch(
        version_url + "/state", json={"status": "READY_FOR_REVIEW", "expected_revision": 0}
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "AUDIO_FILE_INVALID"
    assert read_ok(client, version_url)["status"] == "DRAFT"
    assert read_ok(client, asset_url)["current_version_id"] is None
    assert boundary_state(client) == before


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_real_flac_owner_review_still_rejects_project_members(client, settings, tts_case, role):
    row, receipt = registered_flac(client, settings, tts_case)
    asset_url, version_url = audio_urls(
        tts_case, row["target_asset_id"], receipt["binding"]["asset_version_id"]
    )
    ready = client.patch(
        version_url + "/state", json={"status": "READY_FOR_REVIEW", "expected_revision": 0}
    )
    assert ready.status_code == 200, ready.text
    asset = read_ok(client, asset_url)
    member = account(client)
    with client.app.state.session_factory.begin() as db:
        db.add(ProjectMember(project_id=UUID(tts_case["pid"]), user_id=UUID(member), role=role))
    before = boundary_state(client)
    response = client.patch(
        version_url + "/state",
        json={"status": "APPROVED", "expected_revision": ready.json()["revision"]},
    )
    assert response.status_code == 403, response.text
    locked = client.post(version_url + "/lock", json=selection_payload(asset))
    assert locked.status_code == 403, locked.text
    assert read_ok(client, version_url)["status"] == "READY_FOR_REVIEW"
    assert read_ok(client, asset_url)["current_version_id"] is None
    assert boundary_state(client) == before


def test_generated_silent_flac_cannot_register_a_reviewable_version(client, settings, tts_case):
    output_voice(
        client, settings, tts_case, {"format": "flac", "channels": 1, "sample_rate": 24000}
    )
    output = io.BytesIO()
    with wave.open(output, "wb") as silent:
        silent.setnchannels(1)
        silent.setsampwidth(2)
        silent.setframerate(24000)
        silent.writeframes(bytes(4800))
    row, _, _ = finish_audio(client, settings, tts_case, data=flac_audio(output.getvalue()))
    before = boundary_state(client)
    response = client.post(tts_case["url"] + f"/{row['job_id']}/result", json=declaration())
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "AUDIO_SILENT"
    assert boundary_state(client) == before
    asset_url = f"/api/v1/projects/{tts_case['pid']}/assets/{row['target_asset_id']}"
    assert read_ok(client, asset_url)["current_version_id"] is None
    assert read_ok(client, asset_url + "/versions") == []


def test_new_audio_candidate_requires_a_new_owner_decision_before_selected_version_changes(
    client, settings, tts_case
):
    first, row, _, _ = registered_audio(client, settings, tts_case)
    asset_url, version_url = audio_urls(
        tts_case, row["target_asset_id"], first["binding"]["asset_version_id"]
    )
    approved = approve_audio(client, version_url)
    selected = select_audio(client, asset_url, version_url)
    second, _, _, _ = registered_audio(
        client,
        settings,
        regeneration_case(
            tts_case, first, expected_revision=read_ok(client, asset_url)["revision"]
        ),
    )
    replacement_url = asset_url + f"/versions/{second['binding']['asset_version_id']}"
    current = read_ok(client, asset_url)
    assert current["current_version_id"] == selected["current_version_id"]
    assert current["lock_version"] == selected["lock_version"]
    assert read_ok(client, version_url) == approved
    assert read_ok(client, replacement_url)["status"] == "DRAFT"
    refused = client.post(replacement_url + "/select", json=selection_payload(current))
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "ASSET_NOT_USABLE"
    approve_audio(client, replacement_url)
    replacement = select_audio(client, asset_url, replacement_url)
    assert replacement["current_version_id"] == second["binding"]["asset_version_id"]
    assert read_ok(client, version_url)["status"] == "APPROVED"


@pytest.mark.parametrize(
    "role,read_status,write_status", [(None, 404, 404), ("viewer", 200, 403), ("editor", 200, 403)]
)
def test_audio_version_reading_and_owner_review_remain_permission_scoped(
    client, settings, tts_case, role, read_status, write_status
):
    source, row, _, _ = registered_audio(client, settings, tts_case)
    asset_url, version_url = audio_urls(
        tts_case, row["target_asset_id"], source["binding"]["asset_version_id"]
    )
    ready = client.patch(
        version_url + "/state", json={"status": "READY_FOR_REVIEW", "expected_revision": 0}
    )
    assert ready.status_code == 200, ready.text
    current = read_ok(client, asset_url)
    member = account(client)
    if role:
        with client.app.state.session_factory.begin() as db:
            db.add(ProjectMember(project_id=UUID(tts_case["pid"]), user_id=UUID(member), role=role))
    for url in (asset_url, asset_url + "/versions", version_url):
        response = client.get(url)
        assert response.status_code == read_status, response.text
    approved = client.patch(
        version_url + "/state",
        json={"status": "APPROVED", "expected_revision": ready.json()["revision"]},
    )
    assert approved.status_code == write_status, approved.text
    locked = client.post(version_url + "/lock", json=selection_payload(current))
    assert locked.status_code == write_status, locked.text
    if role != "editor":
        selected = client.post(version_url + "/select", json=selection_payload(current))
        assert selected.status_code == write_status, selected.text


def test_audio_selection_and_locking_keep_cas_and_unlock_without_reading_a_missing_file(
    client, settings, tts_case
):
    source, row, _, _ = registered_audio(client, settings, tts_case)
    asset_url, version_url = audio_urls(
        tts_case, row["target_asset_id"], source["binding"]["asset_version_id"]
    )
    stale_version = read_ok(client, version_url)
    approve_audio(client, version_url)
    conflict = client.patch(
        version_url + "/state",
        json={"status": "ARCHIVED", "expected_revision": stale_version["revision"]},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"
    before = read_ok(client, asset_url)
    selected = select_audio(client, asset_url, version_url)
    conflict = client.post(version_url + "/lock", json=selection_payload(before))
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"
    locked = client.post(version_url + "/lock", json=selection_payload(selected))
    assert locked.status_code == 200, locked.text
    case = regeneration_case(tts_case, source, expected_revision=locked.json()["revision"])
    denied = client.post(case["url"], json=case["payload"])
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "ASSET_READ_ONLY"
    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(source["binding"]["file_id"]))
        file_storage(settings, file).delete(file.object_key)
    unlocked = client.post(version_url + "/unlock", json=selection_payload(locked.json()))
    assert unlocked.status_code == 200, unlocked.text
    assert unlocked.json()["status"] != "LOCKED"
    assert read_ok(client, version_url)["status"] == "APPROVED"
    rejected = client.post(version_url + "/lock", json=selection_payload(unlocked.json()))
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "OBJECT_UNAVAILABLE"


@pytest.mark.parametrize("damage", ["missing", "changed_bytes"])
def test_positive_audio_review_rechecks_actual_private_bytes_but_history_stays_readable(
    client, settings, tts_case, damage
):
    source, row, _, _ = registered_audio(client, settings, tts_case)
    asset_url, version_url = audio_urls(
        tts_case, row["target_asset_id"], source["binding"]["asset_version_id"]
    )
    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(source["binding"]["file_id"]))
        storage = file_storage(settings, file)
        if damage == "missing":
            storage.delete(file.object_key)
        else:
            storage.path(file.object_key).write_bytes(b"corrupt")
    before = boundary_state(client)
    denied = client.patch(
        version_url + "/state", json={"status": "READY_FOR_REVIEW", "expected_revision": 0}
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "OBJECT_UNAVAILABLE"
    assert read_ok(client, version_url)["status"] == "DRAFT"
    assert len(read_ok(client, asset_url + "/versions")) == 1
    assert boundary_state(client) == before


@pytest.mark.parametrize(
    "data,rights,status,code",
    [
        (b"not a waveform", True, 422, "INVALID_FILE"),
        (silent_wave(), True, 422, "AUDIO_SILENT"),
        (None, False, 409, "AUDIO_FILE_INVALID"),
    ],
    ids=["invalid-wave", "silent-wave", "missing-rights"],
)
def test_bound_older_audio_requires_real_decoding_voice_and_rights_before_review(
    client, tts_case, data, rights, status, code
):
    asset_url, version_url = legacy_candidate(client, tts_case, data=data, bind=True, rights=rights)
    before = boundary_state(client)
    response = client.patch(
        version_url + "/state", json={"status": "READY_FOR_REVIEW", "expected_revision": 0}
    )
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert read_ok(client, version_url)["status"] == "DRAFT"
    assert len(read_ok(client, asset_url + "/versions")) == 1
    assert boundary_state(client) == before


def test_unbound_audio_history_cannot_be_approved_and_legacy_approved_audio_cannot_be_selected(
    client, tts_case
):
    asset_url, version_url = legacy_candidate(client, tts_case)
    denied = client.patch(
        version_url + "/state", json={"status": "READY_FOR_REVIEW", "expected_revision": 0}
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "AUDIO_BINDING_REQUIRED"
    assert len(read_ok(client, asset_url + "/versions")) == 1
    _, legacy_id, _ = stored_file(client, tts_case["pid"], wave_bytes(), "audio/wav", kind="AUDIO")
    with client.app.state.session_factory() as db:
        legacy_asset_id = str(db.get(AssetVersion, legacy_id).asset_id)
    legacy_asset_url, legacy_version_url = audio_urls(tts_case, legacy_asset_id, str(legacy_id))
    assert read_ok(client, legacy_version_url)["status"] == "APPROVED"
    denied = client.post(
        legacy_version_url + "/select",
        json=selection_payload(read_ok(client, legacy_asset_url)),
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "AUDIO_BINDING_REQUIRED"


def test_expired_dialogue_and_wrong_asset_scope_cannot_launder_audio_into_approval(
    client, settings, tts_case
):
    source, row, _, _ = registered_audio(client, settings, tts_case)
    asset_url, version_url = audio_urls(
        tts_case, row["target_asset_id"], source["binding"]["asset_version_id"]
    )
    wrong_asset = version_url.replace(row["target_asset_id"], str(uuid4()))
    assert client.get(wrong_asset).status_code == 404
    wrong_project = version_url.replace(tts_case["pid"], str(uuid4()))
    assert client.get(wrong_project).status_code == 404
    approve_changed_line(client, tts_case)
    expired = read_ok(client, version_url)
    assert expired["stale"] is True
    before = boundary_state(client)
    denied = client.patch(
        version_url + "/state",
        json={"status": "READY_FOR_REVIEW", "expected_revision": expired["revision"]},
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["code"] == "ASSET_VERSION_STATE_CONFLICT"
    assert read_ok(client, asset_url)["current_version_id"] is None
    assert boundary_state(client) == before


def test_audio_lifecycle_does_not_open_image_creation_restoration_rebuild_or_visual_comparison(
    client, settings, tts_case
):
    source, row, _, _ = registered_audio(client, settings, tts_case)
    asset_url, version_url = audio_urls(
        tts_case, row["target_asset_id"], source["binding"]["asset_version_id"]
    )
    asset = read_ok(client, asset_url)
    before = boundary_state(client)
    for suffix in ("/image", "/rebuild-inputs", "/continuity"):
        response = client.get(version_url + suffix)
        assert response.status_code == 404, response.text
    requests = [
        (asset_url + "/versions", draft(asset)),
        (
            version_url + "/restore-as-new",
            {
                "creation_key": str(uuid4()),
                "expected_revision": asset["revision"],
                "comment": "视觉恢复接口不能用于音频",
            },
        ),
        (
            version_url + "/rebuild-manifest",
            {
                "creation_key": str(uuid4()),
                "expected_revision": asset["revision"],
                "replacements": {},
            },
        ),
    ]
    for url, payload in requests:
        response = client.post(url, json=payload)
        assert response.status_code == 404, response.text
    assert boundary_state(client) == before
