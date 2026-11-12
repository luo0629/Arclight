"""Dialogue TTS admission, recovery and explicit result registration boundaries."""

from copy import deepcopy
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_audio_production import snapshot, wave_bytes
from test_generation_budget import set_policy
from test_generation_delivery import deliver
from test_generation_jobs import base, capture_ok
from test_media_validation import wave_bytes as silent_wave
from test_model_services import account
from test_native_media_integration import body_for, configure, provider_for
from test_text_contract_v2 import save_script

from arclight.application.generation_artifacts import import_attempt
from arclight.application.generation_attempts import finish_attempt, start_attempt
from arclight.application.generation_scheduling import diagnose
from arclight.application.render_inputs import file_storage
from arclight.core.errors import DomainError
from arclight.domain.generation import GenerationStatus
from arclight.persistence.audio_production import AudioLineBinding
from arclight.persistence.audio_tts import AudioTTSRequest
from arclight.persistence.generation_accounting import (
    GenerationBudgetReservation,
    GenerationBudgetSettlement,
)
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    DependencyOutput,
    FileRegistration,
    GenerationAttempt,
    GenerationJob,
    GenerationMediaArtifact,
    ProjectMember,
    StoredFile,
    VisualAsset,
)


def voice_version(client, project_id, speaker, **changes):
    response = client.post(
        f"/api/v1/projects/{project_id}/audio-voices",
        json={
            "creation_key": str(uuid4()),
            "speaker": speaker,
            "expected_version_no": 0,
            "parameters": {"voice": "alloy", "speed": 1.0},
            "output_spec": {"format": "wav"},
            **changes,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def boundary_case(client, settings):
    owner = account(client)
    configure(client, settings, "speech", "openai_tts")
    response = client.post("/api/v1/projects", json={"name": "逐句配音边界"})
    assert response.status_code == 201, response.text
    project_id = response.json()["id"]
    response = client.post(
        f"/api/v1/projects/{project_id}/episodes",
        json={"episode_no": 1, "title": "雨夜", "target_duration_ms": 30000},
    )
    assert response.status_code == 201, response.text
    episode = response.json()
    script = save_script(client, episode)
    response = snapshot(client, episode, script)
    assert response.status_code == 201, response.text
    frozen = response.json()
    lines = client.get(f"/api/v1/audio-snapshots/{frozen['id']}/lines").json()
    voice = voice_version(client, project_id, lines[0]["speaker"])
    set_policy(client, project_id, "20")
    return {
        "owner": owner,
        "project_id": project_id,
        "episode": episode,
        "script": script,
        "snapshot": frozen,
        "lines": lines,
        "voice": voice,
        "url": f"/api/v1/audio-snapshots/{frozen['id']}/lines/{lines[0]['line_id']}/tts",
        "payload": {"creation_key": str(uuid4()), "voice_version_id": voice["id"]},
    }


def boundary_state(client):
    models = (
        AudioTTSRequest,
        GenerationJob,
        GenerationAttempt,
        GenerationMediaArtifact,
        DependencyManifest,
        DependencyOutput,
        VisualAsset,
        AssetVersion,
        StoredFile,
        FileRegistration,
        AudioLineBinding,
    )
    with client.app.state.session_factory() as db:
        return {
            "counts": tuple(db.scalar(select(func.count()).select_from(model)) for model in models),
            "reserved": tuple(
                db.execute(
                    select(
                        GenerationBudgetReservation.id, GenerationBudgetReservation.amount
                    ).order_by(GenerationBudgetReservation.id)
                )
            ),
            "settled": tuple(
                db.execute(
                    select(GenerationBudgetSettlement.id, GenerationBudgetSettlement.cost).order_by(
                        GenerationBudgetSettlement.id
                    )
                )
            ),
        }


def queued(client, case, **changes):
    response = client.post(case["url"], json={**case["payload"], **changes})
    assert response.status_code == 202, response.text
    return response.json()


def declaration(**changes):
    return {
        "expected_revision": 0,
        "source_note": "Native TTS generation for the frozen dialogue",
        "rights_note": "Synthetic test result with explicit project usage permission",
        "rights_confirmed": True,
        **changes,
    }


def archived(client, settings, case, *, data=None):
    row = queued(client, case)
    job = client.get(base(case["project_id"]) + f"/{row['job_id']}").json()
    raw = wave_bytes() if data is None else data

    def handler(_request):
        return httpx.Response(200, content=raw, headers={"content-type": "audio/wav"})

    provider = provider_for(client, settings, job, handler)
    assert deliver(client, job, provider) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt_id = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    assert import_attempt(client.app.state.session_factory, settings, attempt_id, provider) == 1
    artifacts = client.get(base(case["project_id"]) + f"/{job['id']}/artifacts").json()
    assert len(artifacts) == 1
    return row, artifacts[0], raw


def approve_changed_line(client, case):
    content = deepcopy(case["script"]["content_json"])
    content["scenes"][0]["dialogue"][0]["text"] = "这句已经正式修改。"
    prefix = f"/api/v1/episodes/{case['episode']['id']}/scripts"
    response = client.post(
        prefix,
        json={
            "expected_version_no": 1,
            "base_version_id": case["script"]["id"],
            "content": content,
        },
    )
    assert response.status_code == 201, response.text
    response = client.post(
        prefix + f"/{response.json()['id']}/review", json={"decision": "APPROVE"}
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "parameters,code",
    [
        ({"voice": "another-voice"}, "AUDIO_PERFORMANCE_UNSUPPORTED"),
        ({"pitch": 1.2}, "AUDIO_PERFORMANCE_UNSUPPORTED"),
        ({"emotion": "angry"}, "AUDIO_PERFORMANCE_UNSUPPORTED"),
        ({"speed": 0.1}, "PROVIDER_PARAMETERS_UNSUPPORTED"),
        ({"instructions": True}, "PROVIDER_PARAMETERS_UNSUPPORTED"),
    ],
)
def test_tts_unsupported_performance_has_no_task_manifest_or_budget_side_effects(
    client, boundary_case, parameters, code
):
    before = boundary_state(client)
    response = client.post(
        boundary_case["url"],
        json={**boundary_case["payload"], "performance_parameters": parameters},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code
    assert boundary_state(client) == before


@pytest.mark.parametrize("source", ["other-speaker", "other-project", "missing"])
def test_tts_voice_scope_and_speaker_reject_before_budget_admission(client, boundary_case, source):
    case = boundary_case
    if source == "other-speaker":
        voice = voice_version(client, case["project_id"], case["lines"][1]["speaker"])
        status, code = 422, "AUDIO_VOICE_SPEAKER_MISMATCH"
    elif source == "other-project":
        project = client.post("/api/v1/projects", json={"name": "另一个项目"}).json()
        voice = voice_version(client, project["id"], case["lines"][0]["speaker"])
        status, code = 404, "NOT_FOUND"
    else:
        voice = {"id": str(uuid4())}
        status, code = 404, "NOT_FOUND"
    before = boundary_state(client)
    response = client.post(case["url"], json={**case["payload"], "voice_version_id": voice["id"]})
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert boundary_state(client) == before


@pytest.mark.parametrize("role,status", [(None, 404), ("viewer", 403)])
def test_tts_creation_revalidates_project_write_permission(client, boundary_case, role, status):
    member = account(client)
    if role:
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(boundary_case["project_id"]), user_id=UUID(member), role=role
                )
            )
    before = boundary_state(client)
    response = client.post(boundary_case["url"], json=boundary_case["payload"])
    assert response.status_code == status, response.text
    assert boundary_state(client) == before


def test_tts_replay_ignores_only_cas_and_rejects_changed_line_voice_or_performance(
    client, boundary_case
):
    case = boundary_case
    original = queued(client, case)
    response = client.post(case["url"], json={**case["payload"], "expected_revision": 999})
    assert response.status_code == 200, response.text
    assert response.json() == original
    second_voice = voice_version(
        client,
        case["project_id"],
        case["lines"][0]["speaker"],
        expected_version_no=1,
        parameters={"voice": "nova", "speed": 1.0},
    )
    before = boundary_state(client)
    other_line = case["url"].replace(case["lines"][0]["line_id"], case["lines"][1]["line_id"])
    for url, changes in [
        (case["url"], {"performance_parameters": {"speed": 0.8}}),
        (case["url"], {"voice_version_id": second_voice["id"]}),
        (other_line, {}),
    ]:
        response = client.post(url, json={**case["payload"], **changes})
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert boundary_state(client) == before


def test_tts_old_line_cannot_create_a_new_job_or_budget_reservation(client, boundary_case):
    approve_changed_line(client, boundary_case)
    before = boundary_state(client)
    response = client.post(boundary_case["url"], json=boundary_case["payload"])
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "AUDIO_LINE_NOT_USABLE"
    assert boundary_state(client) == before


def test_tts_queued_line_change_blocks_attempt_without_consuming_another_reservation(
    client, boundary_case
):
    row = queued(client, boundary_case)
    approve_changed_line(client, boundary_case)
    before = boundary_state(client)
    with pytest.raises(DomainError) as caught, client.app.state.session_factory.begin() as db:
        start_attempt(db, UUID(row["job_id"]), row["revision"])
    assert caught.value.code == "AUDIO_LINE_NOT_USABLE"
    assert caught.value.status_code == 409
    assert boundary_state(client) == before
    current = client.get(base(boundary_case["project_id"]) + f"/{row['job_id']}").json()
    assert current["status"] == "CREATED" and current["attempt_count"] == 0
    assert current["revision"] == row["revision"]


def test_tts_diagnosed_replay_requires_line_entry_but_generic_audio_remains_compatible(
    client, boundary_case
):
    case = boundary_case
    row = queued(client, case)
    source = client.get(base(case["project_id"]) + f"/{row['job_id']}").json()
    generic = capture_ok(client, case["project_id"], body_for(client, case["project_id"], "speech"))
    for job in (source, generic):
        with client.app.state.session_factory.begin() as db:
            attempt = start_attempt(db, UUID(job["id"]), job["revision"])
            finish_attempt(
                db,
                UUID(job["id"]),
                attempt.id,
                GenerationStatus.FAILED,
                error_code="INPUT_INVALID",
                not_accepted=True,
            )
            diagnose(db, db.get(GenerationJob, UUID(job["id"])), "NOT_RETRYABLE", "INPUT_INVALID")
    before = boundary_state(client)
    for job, expected in ((source, 409), (generic, 201)):
        current = client.get(base(case["project_id"]) + f"/{job['id']}").json()
        response = client.post(
            base(case["project_id"]) + f"/{job['id']}/replay",
            json={"expected_revision": current["revision"], "reason": "Reviewed definite failure"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == expected, response.text
        if expected == 409:
            assert response.json()["error"]["code"] == "AUDIO_REPLAY_REQUIRES_LINE"
            assert boundary_state(client) == before
        else:
            assert response.json()["task_type"] == "AUDIO"
            assert response.json()["id"] != generic["id"]


def test_tts_late_result_cannot_bind_changed_line_and_retains_raw_file(
    client, settings, boundary_case
):
    row, artifact, raw = archived(client, settings, boundary_case)
    approve_changed_line(client, boundary_case)
    before = boundary_state(client)
    response = client.post(boundary_case["url"] + f"/{row['job_id']}/result", json=declaration())
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "AUDIO_LINE_NOT_USABLE"
    assert boundary_state(client) == before
    with client.app.state.session_factory() as db:
        source = db.get(StoredFile, UUID(artifact["file_id"]))
        assert source.asset_version_id is None and not source.rights_confirmed
        assert (
            file_storage(settings, source).read(
                source.object_key, source.byte_size, source.checksum
            )
            == raw
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"rights_confirmed": False},
        {"rights_confirmed": 1},
        {"rights_confirmed": "true"},
        {"rights_note": "   "},
        {"source_note": "   "},
        {"source_file_id": str(uuid4())},
    ],
)
def test_tts_result_requires_explicit_nonblank_rights_and_server_owned_source(
    client, settings, boundary_case, changes
):
    row, _, _ = archived(client, settings, boundary_case)
    before = boundary_state(client)
    response = client.post(
        boundary_case["url"] + f"/{row['job_id']}/result", json=declaration(**changes)
    )
    assert response.status_code == 422, response.text
    assert boundary_state(client) == before


def test_tts_result_is_scoped_to_its_frozen_line_and_task(client, settings, boundary_case):
    row, _, _ = archived(client, settings, boundary_case)
    before = boundary_state(client)
    other_line = boundary_case["url"].replace(
        boundary_case["lines"][0]["line_id"], boundary_case["lines"][1]["line_id"]
    )
    for url in (
        other_line + f"/{row['job_id']}/result",
        boundary_case["url"] + f"/{uuid4()}/result",
    ):
        response = client.post(url, json=declaration())
        assert response.status_code == 404, response.text
    assert boundary_state(client) == before


@pytest.mark.parametrize("kind", ["silent", "invalid"])
def test_tts_result_media_failure_never_creates_a_bound_version(
    client, settings, boundary_case, kind
):
    data = silent_wave() if kind == "silent" else b"RIFF\x00\x00\x00\x00WAVEtruncated"
    row, artifact, _ = archived(client, settings, boundary_case, data=data)
    before = boundary_state(client)
    response = client.post(boundary_case["url"] + f"/{row['job_id']}/result", json=declaration())
    assert response.status_code == (422 if kind == "silent" else 409), response.text
    assert response.json()["error"]["code"] == (
        "AUDIO_SILENT" if kind == "silent" else "AUDIO_RESULT_NOT_READY"
    )
    assert boundary_state(client) == before
    assert (artifact["file_id"] is not None) == (kind == "silent")
    assert artifact["error_code"] == (None if kind == "silent" else "INVALID_MEDIA_OUTPUT")


def test_tts_failed_registration_cleans_only_new_object_and_can_retry(
    client, settings, boundary_case, monkeypatch
):
    import arclight.application.audio_tts as tts

    row, artifact, raw = archived(client, settings, boundary_case)
    before = boundary_state(client)
    original_objects = {path for path in settings.asset_storage_path.rglob("*") if path.is_file()}

    def fail_binding(*_args, **_kwargs):
        raise DomainError("INJECTED_TTS_FAILURE", "Interrupted after output creation", 409)

    url = boundary_case["url"] + f"/{row['job_id']}/result"
    with monkeypatch.context() as patched:
        patched.setattr(tts, "bind_audio", fail_binding)
        response = client.post(url, json=declaration())
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "INJECTED_TTS_FAILURE"
    assert boundary_state(client) == before
    assert {
        path for path in settings.asset_storage_path.rglob("*") if path.is_file()
    } == original_objects
    with client.app.state.session_factory() as db:
        source = db.get(StoredFile, UUID(artifact["file_id"]))
        assert (
            file_storage(settings, source).read(
                source.object_key, source.byte_size, source.checksum
            )
            == raw
        )
    response = client.post(url, json=declaration())
    assert response.status_code == 201, response.text
    assert response.json()["source_file_id"] == artifact["file_id"]
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AudioLineBinding)) == 1
        assert db.scalar(select(func.count()).select_from(FileRegistration)) == 1
