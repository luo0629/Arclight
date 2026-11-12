"""Dialogue requests traverse the real native adapter, budget, artifacts and binding."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_audio_production import snapshot, wave_bytes
from test_generation_budget import set_policy
from test_generation_delivery import deliver
from test_model_services import account
from test_native_media_integration import attempt_for, configure, project, provider_for
from test_text_contract_v2 import save_script

from arclight.application.generation_artifacts import import_attempt
from arclight.persistence.audio_production import AudioLineBinding
from arclight.persistence.audio_tts import AudioTTSRequest
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    FileRegistration,
    GenerationJob,
    GenerationServiceBinding,
    StoredFile,
    VisualAsset,
)

__all__ = ["tts_case"]


@pytest.fixture
def tts_case(client, settings):
    account(client)
    configure(client, settings, "speech", "openai_tts")
    pid = project(client)
    result = client.post(
        f"/api/v1/projects/{pid}/episodes",
        json={"episode_no": 1, "title": "配音", "target_duration_ms": 30000},
    )
    assert result.status_code == 201, result.text
    episode = result.json()
    script = save_script(client, episode)
    result = snapshot(client, episode, script)
    assert result.status_code == 201, result.text
    snap = result.json()
    lines = client.get(f"/api/v1/audio-snapshots/{snap['id']}/lines").json()
    set_policy(client, pid, "20")
    voice_payload = {
        "creation_key": str(uuid4()),
        "speaker": lines[0]["speaker"],
        "expected_version_no": 0,
        "parameters": {"voice": "test-voice", "speed": 1.0},
        "output_spec": {"format": "wav"},
    }
    response = client.post(f"/api/v1/projects/{pid}/audio-voices", json=voice_payload)
    assert response.status_code == 201, response.text
    voice = response.json()
    url = f"/api/v1/audio-snapshots/{snap['id']}/lines/{lines[0]['line_id']}/tts"
    payload = {"creation_key": str(uuid4()), "voice_version_id": voice["id"]}
    return {
        "pid": pid,
        "episode": episode,
        "script": script,
        "snapshot": snap,
        "lines": lines,
        "voice": voice,
        "voice_payload": voice_payload,
        "url": url,
        "payload": payload,
    }


def create_ok(client, case, **changes):
    response = client.post(case["url"], json={**case["payload"], **changes})
    assert response.status_code == 202, response.text
    row = response.json()
    job = client.get(f"/api/v1/projects/{case['pid']}/generation-jobs/{row['job_id']}")
    assert job.status_code == 200, job.text
    return row, job.json()


def finish_audio(client, settings, case, *, data=None, handler=None):
    row, job = create_ok(client, case)
    audio = wave_bytes() if data is None else data

    def respond(request):
        body = json.loads(request.content)
        assert body["input"] == case["lines"][0]["text"]
        assert body["voice"] == "test-voice"
        return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})

    provider = provider_for(client, settings, job, handler or respond)
    assert deliver(client, job, provider) == "SUCCEEDED"
    attempt_id = attempt_for(client, job)
    assert import_attempt(client.app.state.session_factory, settings, attempt_id, provider) == 1
    assert import_attempt(client.app.state.session_factory, settings, attempt_id, provider) == 0
    return row, job, audio


def declaration(**changes):
    return {
        "expected_revision": 0,
        "source_note": "原创测试台词的供应商生成结果",
        "rights_note": "测试者已确认本次生成结果的使用授权",
        "rights_confirmed": True,
        **changes,
    }


def test_dialogue_tts_registers_exact_unapproved_audio_and_preserves_raw_receipt(
    client, settings, tts_case
):
    row, job, audio = finish_audio(client, settings, tts_case)
    endpoint = tts_case["url"] + f"/{row['job_id']}"
    assert client.get(endpoint + "/result").status_code == 404
    response = client.post(endpoint + "/result", json=declaration())
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["voice_version_id"] == tts_case["voice"]["id"]
    assert receipt["binding"]["snapshot_id"] == tts_case["snapshot"]["id"]
    assert receipt["binding"]["line_id"] == tts_case["lines"][0]["line_id"]
    assert receipt["validation"]["duration_ms"] == 100
    assert receipt["validation"]["silence"]["silent"] is False
    assert receipt["asset_revision"] == 1
    assert client.get(endpoint + "/result").json() == receipt
    replay = client.post(endpoint + "/result", json=declaration(expected_revision=0))
    assert replay.status_code == 200 and replay.json() == receipt
    changed = client.post(endpoint + "/result", json=declaration(rights_note="changed"))
    assert changed.status_code == 409
    with client.app.state.session_factory() as db:
        request = db.scalar(
            select(AudioTTSRequest).where(AudioTTSRequest.job_id == UUID(job["id"]))
        )
        generation = db.get(DependencyManifest, UUID(row["generation_manifest_id"]))
        line_manifest = db.get(DependencyManifest, request.dependency_manifest_id)
        assert generation.id != line_manifest.id
        assert generation.snapshot_json["context"]["audio_line"]["dependency_manifest_id"] == str(
            line_manifest.id
        )
        assert len(line_manifest.snapshot_json["inputs"]) == 1
        binding = db.get(AudioLineBinding, UUID(receipt["binding"]["id"]))
        version = db.get(AssetVersion, binding.asset_version_id)
        asset = db.get(VisualAsset, version.asset_id)
        raw = db.get(StoredFile, UUID(receipt["source_file_id"]))
        exact = db.get(StoredFile, binding.file_id)
        assert raw.rights_confirmed is False and raw.asset_version_id is None
        assert exact.rights_confirmed is True and exact.asset_version_id == version.id
        assert raw.object_key != exact.object_key
        assert version.status == "DRAFT" and not version.stale
        assert asset.current_version_id is None
        assert version.checksum == raw.checksum == hashlib.sha256(audio).hexdigest()
        assert (
            db.scalar(
                select(func.count())
                .select_from(FileRegistration)
                .where(FileRegistration.file_id == raw.id)
            )
            == 1
        )
    assert client.get(endpoint).json()["asset_version_id"] == str(version.id)


def test_tts_creation_replay_keeps_budget_and_frozen_voice_after_new_defaults(
    client, settings, tts_case
):
    row, job = create_ok(client, tts_case)
    configure(client, settings, "speech", "openai_tts", revision=1, key="replacement-key")
    response = client.post(
        f"/api/v1/projects/{tts_case['pid']}/audio-voices",
        json={
            **tts_case["voice_payload"],
            "creation_key": str(uuid4()),
            "expected_version_no": 1,
            "parameters": {"voice": "another-voice"},
        },
    )
    assert response.status_code == 201, response.text
    replay = client.post(tts_case["url"], json=tts_case["payload"])
    assert replay.status_code == 200 and replay.json() == row
    with client.app.state.session_factory() as db:
        manifest = db.get(DependencyManifest, UUID(row["generation_manifest_id"]))
        spec = manifest.snapshot_json["context"]["generation"]
        assert spec["parameters"]["voice"] == "test-voice"
        assert spec["model_config_version"] == tts_case["voice"]["model_config_version"]
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 1
        binding = db.scalar(
            select(GenerationServiceBinding).where(
                GenerationServiceBinding.job_id == UUID(job["id"])
            )
        )
        assert binding.configuration_json["config_revision"] == 1
    assert "credential_id" not in json.dumps(tts_case["voice"])


def test_tts_freezes_deployment_execution_policy_and_replay_preserves_it(
    client, settings, tts_case
):
    from arclight.domain.generation_scheduling import ExecutionPolicy
    from arclight.persistence.generation_scheduling import GenerationSchedule

    policy = ExecutionPolicy(
        max_attempts=1,
        retry_errors=frozenset(),
        automatic_retry_errors=frozenset(),
        call_timeout_seconds=45,
        max_elapsed_seconds=90,
    )
    settings.media_execution_policy = policy
    expected_policy = policy.model_dump(mode="json") | {
        "project_limits": {tts_case["pid"]: policy.project_concurrency}
    }
    row, _ = create_ok(client, tts_case)
    with client.app.state.session_factory() as db:
        schedule = db.scalar(
            select(GenerationSchedule).where(GenerationSchedule.job_id == UUID(row["job_id"]))
        )
        assert schedule.policy_json == expected_policy
    settings.media_execution_policy = ExecutionPolicy(max_attempts=5)
    replay = client.post(tts_case["url"], json=tts_case["payload"])
    assert replay.status_code == 200 and replay.json() == row
    with client.app.state.session_factory() as db:
        schedule = db.scalar(
            select(GenerationSchedule).where(GenerationSchedule.job_id == UUID(row["job_id"]))
        )
        assert schedule.policy_json == expected_policy
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 1


def test_voice_defaults_are_explicit_and_paginated_without_changing_history(client, tts_case):
    endpoint = f"/api/v1/projects/{tts_case['pid']}/audio-voices"
    first = tts_case["voice"]
    response = client.post(
        endpoint,
        json={
            **tts_case["voice_payload"],
            "creation_key": str(uuid4()),
            "expected_version_no": 1,
            "parameters": {"voice": "new-default"},
        },
    )
    assert response.status_code == 201, response.text
    second = response.json()
    defaults = client.get(endpoint, params={"speaker": first["speaker"], "latest_only": True})
    assert defaults.status_code == 200 and defaults.json() == [second]
    history = client.get(endpoint, params={"speaker": first["speaker"]})
    assert history.json() == [second, first]
    assert client.get(endpoint, params={"limit": 1, "offset": 1}).json() == [first]
    assert client.get(endpoint + f"/{first['id']}").json() == first


@pytest.mark.parametrize("protocol", ["qwen_tts", "siliconflow_tts"])
def test_native_dialogue_flow_reuses_each_existing_tts_protocol(
    client, settings, tts_case, protocol
):
    configure(client, settings, "speech", protocol, revision=1)
    response = client.post(
        f"/api/v1/projects/{tts_case['pid']}/audio-voices",
        json={
            **tts_case["voice_payload"],
            "creation_key": str(uuid4()),
            "expected_version_no": 1,
            "parameters": {"voice": "test-voice"},
        },
    )
    assert response.status_code == 201, response.text
    tts_case["payload"]["voice_version_id"] = response.json()["id"]
    audio = wave_bytes()
    submitted = []

    def respond(request):
        if request.method == "GET":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=audio)
        body = json.loads(request.content)
        submitted.append(body)
        if protocol == "qwen_tts":
            assert body["input"] == {"text": tts_case["lines"][0]["text"], "voice": "test-voice"}
            return httpx.Response(
                200, json={"output": {"audio": {"url": "https://cdn.example/speech"}}}
            )
        assert body["input"] == tts_case["lines"][0]["text"]
        return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})

    row, _, _ = finish_audio(client, settings, tts_case, data=audio, handler=respond)
    response = client.post(tts_case["url"] + f"/{row['job_id']}/result", json=declaration())
    assert response.status_code == 201, response.text
    assert len(submitted) == 1


@pytest.mark.parametrize("operation", ["create", "register"])
def test_postgres_duplicate_tts_operations_have_one_job_or_exact_version(
    client, settings, tts_case, operation
):
    from arclight.application.audio_tts import create_tts, register_result, require_tts
    from arclight.domain.audio_tts import AudioTTSCreate, AudioTTSRegister

    factory = client.app.state.session_factory
    with factory() as db:
        if db.bind.dialect.name != "postgresql":
            pytest.skip("PostgreSQL dialogue TTS row locking required")
    actor = tts_case["voice"]["created_by"]
    snapshot_id = UUID(tts_case["snapshot"]["id"])
    line_id = tts_case["lines"][0]["line_id"]
    row = finish_audio(client, settings, tts_case)[0] if operation == "register" else None

    def execute(_):
        with factory.begin() as db:
            if operation == "create":
                receipt, created = create_tts(
                    db, snapshot_id, line_id, actor, AudioTTSCreate(**tts_case["payload"])
                )
                return str(receipt.job_id), created
            request = require_tts(db, snapshot_id, line_id, UUID(row["job_id"]), actor, write=True)
            receipt, created = register_result(
                db, settings, request, actor, AudioTTSRegister(**declaration())
            )
            return str(receipt.binding.asset_version_id), created

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(execute, range(2)))
    assert len({item[0] for item in receipts}) == 1
    assert sorted(item[1] for item in receipts) == [False, True]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AudioTTSRequest)) == 1
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 1
        if operation == "register":
            assert db.scalar(select(func.count()).select_from(AudioLineBinding)) == 1
            assert db.scalar(select(func.count()).select_from(FileRegistration)) == 1
