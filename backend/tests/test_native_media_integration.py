"""Account-bound native requests traverse pricing, dispatch, private import and access checks."""

import base64
import io
import wave
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from alembic import command
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_asset_versions import image_bytes
from test_generation_delivery import deliver
from test_generation_jobs import base, capture, capture_ok
from test_generation_scheduling import execute, schedule
from test_model_services import account

from arclight.application.generation_artifacts import import_attempt as import_media
from arclight.application.generation_candidates import import_attempt as import_image
from arclight.application.media_services import connection_for_job
from arclight.core.errors import DomainError
from arclight.domain.service_models import ServiceModelPolicy
from arclight.persistence.model_gateway import GatewayReceipt
from arclight.persistence.models import (
    GatewayCall,
    GenerationAttempt,
    GenerationJob,
    GenerationServiceBinding,
    ProjectMember,
    StoredFile,
    User,
)
from arclight.providers.native_media import NativeMediaProvider


def configure(
    client,
    settings,
    kind="image",
    protocol="cogview",
    revision=0,
    key="first-key",
):
    if not settings.service_encryption_key.get_secret_value():
        settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    model = "wan2.7-t2v" if protocol == "wan" else "native-model"
    settings.service_model_policies = [
        ServiceModelPolicy(
            kind=kind,
            supplier="custom",
            protocol=protocol,
            model=model,
            base_url="https://native.example/v1",
            maximum_cost_per_output=Decimal("1"),
            pricing_version="test-native:1",
        )
    ]
    response = client.put(
        f"/api/v1/me/model-services/{kind}",
        json={
            "supplier": "custom",
            "model": model,
            "protocol": protocol,
            "api_key": key,
            "base_url": "https://native.example/v1",
            "expected_revision": revision,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def catalog_version(client, project_id):
    response = client.get(f"/api/v1/projects/{project_id}/audio-voice-catalog")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "READY", result
    return result["configuration_version"]


def project(client):
    response = client.post("/api/v1/projects", json={"name": "原生服务联通"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def body_for(client, project_id, kind="image"):
    config = client.get(base(project_id) + f"/services/{kind}/configuration").json()
    task = {"image": "IMAGE", "video": "VIDEO", "speech": "AUDIO"}[kind]
    return {
        "target_type": "SHOT_IMAGE" if task == "IMAGE" else task,
        "task_type": task,
        "spec": {
            "provider": config["provider"],
            "model": config["model"],
            "model_config_version": config["configuration_version"],
            "prompt": "synthetic media",
            "prompt_version": "test:1",
            "parameters": {"voice": "test-voice"} if task == "AUDIO" else {},
            "output_spec": {"format": "wav"}
            if task == "AUDIO"
            else {
                "width": 1024 if task == "IMAGE" else 1280,
                "height": 1024 if task == "IMAGE" else 720,
                "format": "png" if task == "IMAGE" else "mp4",
                **({"duration_seconds": 6} if task == "VIDEO" else {}),
            },
        },
    }


def provider_for(client, settings, job, handler, *, output=False):
    with client.app.state.session_factory() as db:
        connection = connection_for_job(
            db, db.get(GenerationJob, UUID(job["id"])), settings, for_output=output
        )
    return NativeMediaProvider(settings, connection, transport=httpx.MockTransport(handler))


def attempt_for(client, job):
    with client.app.state.session_factory() as db:
        return db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )


def test_frozen_key_dispatch_and_image_candidates_keep_existing_review_gate(client, settings):
    uid = account(client)
    configure(client, settings)
    pid = project(client)
    job = capture_ok(client, pid, body_for(client, pid))
    configure(client, settings, revision=1, key="rotated-key")
    data = image_bytes("PNG", (1024, 1024))
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(data).decode()}]})

    provider = provider_for(client, settings, job, handler)
    assert deliver(client, job, provider) == "SUCCEEDED"
    assert seen == ["Bearer first-key"]
    attempt = attempt_for(client, job)
    assert import_image(client.app.state.session_factory, settings, attempt, provider) == 1
    assert import_image(client.app.state.session_factory, settings, attempt, provider) == 0
    candidates = client.get(base(pid) + f"/{job['id']}/candidates").json()
    assert len(candidates) == 1 and candidates[0]["required_review"] == "CONTINUITY"
    assert candidates[0]["error_code"] is None
    with client.app.state.session_factory() as db:
        binding = db.scalar(
            select(GenerationServiceBinding).where(
                GenerationServiceBinding.job_id == UUID(job["id"])
            )
        )
        assert binding.created_by == uid and binding.configuration_revision == 1
        call = db.scalar(select(GatewayCall).where(GatewayCall.generation_attempt_id == attempt))
        assert call.route_json["credential_source"] == "ACCOUNT_SNAPSHOT"
        file = db.scalar(select(StoredFile))
        assert not file.rights_confirmed
    public = client.get(base(pid) + f"/{job['id']}/attempts").text
    assert "first-key" not in public and "credential_id" not in public


def test_cleared_account_media_key_blocks_new_jobs_without_changing_frozen_binding(
    client, settings
):
    account(client)
    saved = configure(client, settings)
    pid = project(client)
    body = body_for(client, pid)
    job = capture_ok(client, pid, body)
    cleared = client.put(
        "/api/v1/me/model-services/image",
        json={
            "supplier": saved["supplier"],
            "model": saved["model"],
            "protocol": saved["protocol"],
            "base_url": saved["base_url"],
            "expected_revision": saved["revision"],
            "clear_key": True,
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert not cleared.json()["has_key"]
    config = client.get(base(pid) + "/services/image/configuration")
    assert config.status_code == 200 and not config.json()["configured"]
    rejected = capture(client, pid, body)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "SERVICE_INCOMPLETE"
    with client.app.state.session_factory() as db:
        frozen = connection_for_job(db, db.get(GenerationJob, UUID(job["id"])), settings)
        assert frozen is not None and frozen.api_key == "first-key"
        assert frozen.configuration["configuration_version"] == body["spec"]["model_config_version"]


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(bytes(4800))
    return stream.getvalue()


def mp4_bytes():
    import av

    stream = io.BytesIO()
    with av.open(stream, "w", format="mp4") as output:
        video = output.add_stream("mpeg4", rate=24)
        video.width, video.height, video.pix_fmt = 1280, 720, "yuv420p"
        for _ in range(3):
            frame = av.VideoFrame(1280, 720, "yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in video.encode(frame):
                output.mux(packet)
        for packet in video.encode():
            output.mux(packet)
    return stream.getvalue()


@pytest.mark.parametrize("kind,protocol", [("speech", "openai_tts"), ("video", "gemini_video")])
def test_native_audio_video_validated_durable_registered_download_is_authorized(
    client, settings, kind, protocol
):
    account(client)
    configure(client, settings, kind, protocol)
    pid = project(client)
    job = capture_ok(client, pid, body_for(client, pid, kind))
    data = wav_bytes() if kind == "speech" else mp4_bytes()

    def handler(request):
        if request.method == "GET":
            assert (
                "authorization" not in request.headers and "x-goog-api-key" not in request.headers
            )
            return httpx.Response(200, content=data)
        if kind == "speech":
            return httpx.Response(200, content=data, headers={"content-type": "audio/wav"})
        return httpx.Response(
            200,
            json={
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {"type": "video", "uri": "https://cdn.example/video?signature=secret"}
                        ],
                    }
                ]
            },
        )

    provider = provider_for(client, settings, job, handler)
    assert deliver(client, job, provider) == "SUCCEEDED"
    attempt = attempt_for(client, job)
    assert import_media(client.app.state.session_factory, settings, attempt, provider) == 1
    assert import_media(client.app.state.session_factory, settings, attempt, provider) == 0
    endpoint = base(pid) + f"/{job['id']}/artifacts"
    response = client.get(endpoint)
    assert response.status_code == 200, response.text
    artifact = response.json()[0]
    assert artifact["file_id"] and artifact["error_code"] is None
    assert artifact["media_json"]["duration_ms"] > 0
    download = endpoint + f"/{artifact['id']}/content"
    content = client.get(download)
    assert content.status_code == 200 and content.content == data
    assert content.headers["cache-control"] == "private, no-store"
    assert "signature" not in response.text
    assert_history_guard(client.app.state.session_factory, "generation_media_artifacts", job["id"])
    account(client)
    assert client.get(download).status_code == 404


def test_price_missing_or_changed_and_revoked_credential_owner_fail_closed(client, settings):
    uid = account(client)
    configure(client, settings)
    pid = project(client)
    body = body_for(client, pid)
    policies = settings.service_model_policies
    settings.service_model_policies = []
    rejected = capture(client, pid, body)
    assert rejected.status_code in {409, 422}
    settings.service_model_policies = policies
    job = capture_ok(client, pid, body)
    with client.app.state.session_factory.begin() as db:
        db.get(User, UUID(uid)).disabled = True
    with pytest.raises(DomainError, match="停用"):
        provider_for(client, settings, job, lambda req: httpx.Response(200))
    # Reading an already durable result does not depend on today's API key.
    with client.app.state.session_factory() as db:
        assert (
            connection_for_job(
                db, db.get(GenerationJob, UUID(job["id"])), settings, for_output=True
            ).api_key
            == ""
        )


def assert_history_guard(factory, table, job_id):
    with factory() as db:
        postgres = db.bind.dialect.name == "postgresql"
    statements = [
        f"UPDATE {table} SET updated_at=created_at WHERE job_id=:id",
        f"DELETE FROM {table} WHERE job_id=:id",
        f"TRUNCATE TABLE {table}"
        if postgres
        else f"INSERT OR REPLACE INTO {table} SELECT * FROM {table} WHERE job_id=:id",
    ]
    for statement in statements:
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
            db.execute(
                text(statement),
                {
                    "id": UUID(job_id) if postgres else UUID(job_id).hex,
                },
            )


@pytest.mark.usefixtures("legacy_execution_deadlines")
def test_native_history_is_immutable_and_schema_matches_metadata(client, settings, migrated):
    account(client)
    configure(client, settings)
    pid = project(client)
    job = capture_ok(client, pid, body_for(client, pid))
    command.check(migrated)
    assert_history_guard(client.app.state.session_factory, "generation_service_bindings", job["id"])
    with pytest.raises(RuntimeError, match="history exists"):
        command.downgrade(migrated, "0051")


def test_two_accounts_and_owner_replay_keep_the_original_credential_owner(client, settings):
    owner = account(client)
    owner_auth = client.headers["Authorization"]
    configure(client, settings, key="owner-key")
    pid = project(client)
    editor = account(client)
    configure(client, settings, key="editor-key")
    with client.app.state.session_factory.begin() as db:
        db.add(ProjectMember(project_id=UUID(pid), user_id=UUID(editor), role="editor"))
    job = capture_ok(client, pid, body_for(client, pid))
    provider = provider_for(client, settings, job, lambda req: httpx.Response(401))
    assert provider.connection.api_key == "editor-key"
    assert deliver(client, job, provider) == "FAILED"
    client.headers["Authorization"] = owner_auth
    current = client.get(base(pid) + f"/{job['id']}").json()
    replayed = client.post(
        base(pid) + f"/{job['id']}/replay",
        json={
            "expected_revision": current["revision"],
            "reason": "Explicit owner replay after key access restored",
        },
        headers={"Idempotency-Key": "native-replay"},
    )
    assert replayed.status_code == 201, replayed.text
    replay = replayed.json()
    assert replay["created_by"] == owner
    selected = provider_for(client, settings, replay, lambda req: httpx.Response(200))
    assert selected.connection.api_key == "editor-key"
    with client.app.state.session_factory.begin() as db:
        member = db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == UUID(pid), ProjectMember.user_id == UUID(editor)
            )
        )
        db.delete(member)
    with pytest.raises(DomainError):
        provider_for(client, settings, replay, lambda req: httpx.Response(200))


@pytest.mark.parametrize("protocol", ["cogview", "openai_image"])
def test_completed_download_failure_keeps_unknown_cost_in_both_ledgers(client, settings, protocol):
    account(client)
    configure(client, settings, protocol=protocol)
    pid = project(client)
    job = capture_ok(client, pid, body_for(client, pid))

    def handler(req):
        if req.method == "GET":
            assert "authorization" not in req.headers
            return httpx.Response(401)
        return httpx.Response(200, json={"data": [{"url": "https://cdn.example/image"}]})

    assert deliver(client, job, provider_for(client, settings, job, handler)) == "FAILED"
    with client.app.state.session_factory() as db:
        attempt = db.get(GenerationAttempt, attempt_for(client, job))
        assert attempt.cost is None and attempt.error_code == "PROVIDER_REJECTED"
        call = db.scalar(select(GatewayCall).where(GatewayCall.generation_attempt_id == attempt.id))
        receipt = db.scalar(select(GatewayReceipt).where(GatewayReceipt.call_id == call.id))
        assert receipt.status == "FAILED" and receipt.actual_cost is None
        assert receipt.usage_json["images"] == 1


def test_sdk_timeout_keeps_original_attempt_without_resubmission(client, settings):
    account(client)
    configure(client, settings, protocol="openai_image")
    pid = project(client)
    job = capture_ok(client, pid, body_for(client, pid))
    requests = []

    def timeout(request):
        requests.append(request)
        raise httpx.ReadTimeout("synthetic response loss", request=request)

    assert deliver(client, job, provider_for(client, settings, job, timeout)) == "RESULT_UNKNOWN"
    original_attempt = attempt_for(client, job)
    assert deliver(client, job, provider_for(client, settings, job, timeout)) == "RESULT_UNKNOWN"
    assert len(requests) == 1
    assert attempt_for(client, job) == original_attempt
    with client.app.state.session_factory() as db:
        call = db.scalar(
            select(GatewayCall).where(GatewayCall.generation_attempt_id == original_attempt)
        )
        receipt = db.scalar(select(GatewayReceipt).where(GatewayReceipt.call_id == call.id))
        assert receipt.status == "RESULT_UNKNOWN" and receipt.actual_cost is None


class LostImageBody(httpx.SyncByteStream):
    def __iter__(self):
        yield b'{"data":['
        raise httpx.ReadError("synthetic response loss")


@pytest.mark.parametrize(
    "failure,not_sent,request_id",
    [
        ("connect", True, None),
        ("connect_timeout", True, None),
        ("pool_timeout", True, None),
        ("write_timeout", False, None),
        ("remote_protocol", False, None),
        ("response_body", False, "img-confirmed-header"),
        ("credential_header", False, None),
    ],
)
def test_images_submit_failure_keeps_or_releases_capacity_by_evidence(
    client, settings, failure, not_sent, request_id
):
    account(client)
    configure(client, settings, protocol="openai_image")
    settings.media_execution_policy = settings.media_execution_policy.model_copy(
        update={"project_concurrency": 1, "automatic_retry_errors": frozenset()}
    )
    pid = project(client)
    first = capture_ok(client, pid, body_for(client, pid))
    second = capture_ok(client, pid, body_for(client, pid))
    requests = []

    def fail(request):
        requests.append(request)
        if failure in {"response_body", "credential_header"}:
            return httpx.Response(
                200,
                headers={"x-request-id": request_id or "echo-first-key"},
                stream=LostImageBody(),
            )
        error = {
            "connect": httpx.ConnectError,
            "connect_timeout": httpx.ConnectTimeout,
            "pool_timeout": httpx.PoolTimeout,
            "write_timeout": httpx.WriteTimeout,
            "remote_protocol": httpx.RemoteProtocolError,
        }[failure]
        raise error("synthetic transport failure", request=request)

    provider = provider_for(client, settings, first, fail)
    outcome = "FAILED" if not_sent else "RESULT_UNKNOWN"
    assert execute(client, first, provider) == outcome
    assert execute(client, first, provider) == outcome
    assert len(requests) == 1
    original_attempt = attempt_for(client, first)
    with client.app.state.session_factory() as db:
        attempt = db.get(GenerationAttempt, original_attempt)
        assert attempt.provider_request_id == request_id and attempt.result_json is None
        call = db.scalar(
            select(GatewayCall).where(GatewayCall.generation_attempt_id == original_attempt)
        )
        receipt = db.scalar(select(GatewayReceipt).where(GatewayReceipt.call_id == call.id))
        assert receipt.status == ("NOT_ACCEPTED" if not_sent else "RESULT_UNKNOWN")
        assert receipt.actual_cost == (Decimal("0") if not_sent else None)
    calls = client.get(base(pid) + f"/{first['id']}/calls").json()
    assert len(calls) == 1 and (calls[0]["released_at"] is not None) is not_sent

    next_requests = []

    def succeed(request):
        next_requests.append(request)
        data = base64.b64encode(image_bytes("PNG", (1024, 1024))).decode()
        return httpx.Response(
            200,
            headers={"x-request-id": "img-next-shot"},
            json={"data": [{"b64_json": data}]},
        )

    next_provider = provider_for(client, settings, second, succeed)
    assert execute(client, second, next_provider) == ("SUCCEEDED" if not_sent else "CREATED")
    if not_sent:
        assert len(next_requests) == 1
        next_attempt = attempt_for(client, second)
        with client.app.state.session_factory() as db:
            assert db.get(GenerationAttempt, next_attempt).provider_request_id == "img-next-shot"
    else:
        assert (
            not next_requests and schedule(client, second)["blocked_code"] == "PROJECT_CONCURRENCY"
        )


def test_generated_flac_and_mp4_are_fully_decoded_and_truncation_is_rejected():
    import av

    from arclight.media.validation import probe

    stream = io.BytesIO()
    with av.open(stream, "w", format="flac") as output, av.open(io.BytesIO(wav_bytes())) as source:
        audio = output.add_stream("flac", rate=24000)
        audio.layout = "mono"
        for frame in source.decode(audio=0):
            for packet in audio.encode(frame):
                output.mux(packet)
        for packet in audio.encode():
            output.mux(packet)
    flac = stream.getvalue()
    assert probe(flac, "audio/flac")["duration_ms"] == 100
    video = mp4_bytes()
    actual = probe(video, "video/mp4")
    assert actual["width"] == 1280 and actual["height"] == 720
    assert actual["duration_ms"] == 125
    assert actual["codec"] == "mpeg4"
    assert actual["decoded_frames"] == 3
    assert actual["has_audio"] is False
    assert actual["full_decode_verified"] is True
    for data, media in ((flac, "audio/flac"), (video, "video/mp4")):
        with pytest.raises(DomainError):
            probe(data[: len(data) // 2], media)
