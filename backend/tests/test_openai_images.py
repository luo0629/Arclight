"""GEN-09: actual adapter HTTP contracts, durable candidates and safe uncertainty."""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from test_asset_selection import approved, current
from test_asset_versions import image_bytes, private_objects  # noqa: F401
from test_assets import create
from test_concurrency import require_postgres
from test_generation_budget import set_policy
from test_generation_candidates import candidates
from test_generation_delivery import deliver, event_for
from test_generation_jobs import base, capture, capture_ok, payload
from test_generation_queues import messages
from test_generation_reconciliation import QueryFake, query

from arclight.application.generation_candidates import import_attempt
from arclight.application.generation_jobs import create_job
from arclight.core.errors import DomainError
from arclight.core.image_service import OPENAI_IMAGE_MODEL, OpenAIImageSettings
from arclight.domain.generation_requests import GenerationCreate, GenerationSpec
from arclight.domain.versions import digest
from arclight.persistence.model_gateway import GatewayCall, GatewayReceipt
from arclight.persistence.models import DependencyManifest, GenerationAttempt, GenerationJob
from arclight.providers.media_outputs import get_media_output_provider
from arclight.providers.media_query import QueryReceipt
from arclight.providers.media_submission import MediaSubmission, get_media_submission_provider
from arclight.providers.openai_images import OpenAIImageProvider, result_object_key

CANARY = "image-secret-canary-value"


@pytest.fixture
def configured(settings):
    settings.image_service = OpenAIImageSettings(
        enabled=True,
        api_key=SecretStr(CANARY),
        maximum_cost_per_image=Decimal("0.5"),
        pricing_version="verified-test-price:1",
    )
    settings.media_execution_policy = settings.media_execution_policy.model_copy(
        update={"automatic_retry_errors": frozenset()}
    )
    return settings


def image_request(settings, target=None, count=1):
    body = payload(target)
    body["spec"].update(
        provider="openai",
        model=settings.image_service.model,
        model_config_version=settings.image_service.public_configuration()["configuration_version"],
        parameters={"quality": "low"},
        seed=None,
        batch_size=count,
        output_spec={"width": 1024, "height": 1024, "format": "jpeg"},
    )
    return body


def response(body, status=200):
    raw = json.dumps(body).encode()
    return httpx.Response(
        status,
        headers={"content-type": "application/json", "x-request-id": "req_image_contract"},
        stream=httpx.ByteStream(raw),
    )


def success(count=1, **extra):
    return {
        "data": [
            {"b64_json": base64.b64encode(image_bytes("JPEG", (1024, 1024))).decode()}
            for _ in range(count)
        ],
        "usage": {"input_tokens": 12, "output_tokens": 30, "total_tokens": 42},
        **extra,
    }


def attempt_for(client, job):
    with client.app.state.session_factory() as db:
        return db.scalar(
            select(GenerationAttempt).where(GenerationAttempt.job_id == UUID(job["id"]))
        )


def test_http_adapter_through_gateway_ledger_and_real_candidate_import(configured, client, episode):
    project_id = episode["project_id"]
    set_policy(client, project_id, "20")
    target = create(client, project_id)
    body = image_request(configured, target, count=2)
    key = str(uuid4())
    first = capture(client, project_id, body, key)
    assert first.status_code == 201, first.text
    job = first.json()
    assert capture(client, project_id, body, key).json() == job
    requests = []

    def handle(request):
        requests.append(request)
        assert str(request.url) == "https://api.openai.com/v1/images/generations"
        assert request.headers["authorization"] == "Bearer " + CANARY
        assert "idempotency-key" not in request.headers
        sent = json.loads(request.content)
        assert sent["model"] == OPENAI_IMAGE_MODEL and sent["n"] == 2
        assert sent["size"] == "1024x1024" and sent["output_format"] == "jpeg"
        assert sent["output_compression"] == 85 and sent["quality"] == "low"
        assert "Avoid these elements" in sent["prompt"]
        return response(success(2, private_response=CANARY))

    provider = OpenAIImageProvider(configured, transport=httpx.MockTransport(handle))
    dispatch = event_for(client, job)
    assert deliver(client, job, provider, dispatch) == "SUCCEEDED"
    assert deliver(client, job, provider, dispatch) == "SUCCEEDED"
    assert len(requests) == 1
    attempt = attempt_for(client, job)
    assert attempt.provider_request_id == "req_image_contract"
    assert attempt.cost is None and attempt.result_json["usage"]["images"] == 2
    assert "b64_json" not in str(attempt.result_json) and CANARY not in str(attempt.result_json)
    with client.app.state.session_factory() as db:
        call = db.scalar(select(GatewayCall))
        assert call.route_json["credential_source"] == "SERVER"
        assert db.scalar(select(func.count()).select_from(GatewayCall)) == 1
        receipt = db.scalar(select(GatewayReceipt))
        assert receipt.actual_cost is None and receipt.usage_json["images"] == 2
        manifest = db.get(DependencyManifest, UUID(job["input_manifest_id"]))
        assert manifest.snapshot_json["context"]["provider_configuration"] == (
            configured.image_service.public_configuration()
        )
        assert CANARY not in str(manifest.snapshot_json)
    costs = client.get(f"/api/v1/projects/{project_id}/costs").json()["summary"]
    assert costs["entry_count"] == 1 and costs["pending_estimate"] == "1.000000"
    assert costs["actual_cost"] is None and costs["cost_is_estimate"]
    factory = client.app.state.session_factory
    assert import_attempt(factory, configured, attempt.id, provider) == 2
    assert import_attempt(factory, configured, attempt.id, provider) == 0
    rows = candidates(client, project_id, job)
    assert len(rows) == 2 and all(row["error_code"] is None for row in rows)
    assert all(row["required_review"] == "CONTINUITY" for row in rows)
    assert current(client, target)["current_version_id"] is None
    for row in rows:
        image = client.get(f"/api/v1/projects/{project_id}/generation-candidates/{row['id']}/image")
        assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
        assert image.content == image_bytes("JPEG", (1024, 1024))
    assert isinstance(get_media_submission_provider("openai", configured), OpenAIImageProvider)
    assert isinstance(get_media_output_provider("openai", configured), OpenAIImageProvider)


@pytest.mark.parametrize(
    "changes",
    [
        {"seed": 17},
        {"require_query": True},
        {"require_cancel": True},
        {"batch_size": 5},
        {"parameters": {"steps": 20}},
        {"parameters": {"quality": "auto"}},
        {"parameters": {"quality": ["low"]}},
        {"parameters": {"quality": None}},
        {"output_spec": {"width": 8, "height": 12}},
        {"output_spec": {"width": 1024, "height": 1024, "format": "webp"}},
        {"output_spec": {"width": 1024, "height": 1024, "format": {"name": "png"}}},
        {"model": "unregistered-image"},
        {"model_config_version": "untrusted:1"},
    ],
)
def test_unsupported_capabilities_and_configuration_are_rejected_before_work(
    configured, client, episode, changes
):
    set_policy(client, episode["project_id"], "20")
    body = image_request(configured)
    body["spec"].update(changes)
    assert capture(client, episode["project_id"], body).status_code in {409, 422}
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
        assert db.scalar(select(func.count()).select_from(GatewayCall)) == 0


def test_disabled_missing_quote_or_secret_never_create_billable_work(configured, client, episode):
    for changes in (
        {"enabled": False},
        {"maximum_cost_per_image": None},
        {"api_key": SecretStr("")},
        {"api_key": SecretStr(""), "api_key_file": Path("does-not-exist-secret")},
    ):
        original = configured.image_service
        configured.image_service = original.model_copy(update=changes)
        assert capture(client, episode["project_id"], image_request(configured)).status_code in {
            409,
            422,
        }
        configured.image_service = original
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"pricing_version": "next:2"},
        {"base_url": "https://other-provider.example/v1"},
        {"model": "gpt-image-2.5-sunburst"},
    ],
)
def test_server_config_change_fails_before_attempt_and_public_config_is_secret_free(
    configured, client, episode, changes
):
    set_policy(client, episode["project_id"], "20")
    job = capture_ok(client, episode["project_id"], image_request(configured))
    before = configured.image_service.public_configuration()
    configured.image_service = configured.image_service.model_copy(update=changes)
    assert (
        configured.image_service.public_configuration()["configuration_version"]
        != before["configuration_version"]
    )
    requests = []
    provider = OpenAIImageProvider(
        configured, transport=httpx.MockTransport(lambda req: requests.append(req))
    )
    assert deliver(client, job, provider) == "FAILED"
    assert not requests and attempt_for(client, job) is None
    public = client.get(base(episode["project_id"]) + "/providers/openai/configuration")
    assert public.status_code == 200 and public.json()["enabled"]
    assert CANARY not in public.text and "api_key" not in public.text


@pytest.mark.parametrize("status", [401, 403, 404, 422, 429])
def test_definite_http_rejection_releases_reservation_and_never_retries_inline(
    configured, client, episode, status
):
    set_policy(client, episode["project_id"], "20")
    job = capture_ok(client, episode["project_id"], image_request(configured))
    requests = []

    def reject(request):
        requests.append(request)
        return response({"error": {"message": CANARY}}, status)

    provider = OpenAIImageProvider(configured, transport=httpx.MockTransport(reject))
    assert deliver(client, job, provider) == "FAILED"
    assert deliver(client, job, provider) == "FAILED"
    assert len(requests) == 1
    attempt = attempt_for(client, job)
    assert attempt.cost == 0 and CANARY not in str(attempt.result_json)
    costs = client.get(f"/api/v1/projects/{episode['project_id']}/costs").json()["summary"]
    assert costs["actual_cost"] == "0.000000" and costs["pending_estimate"] == "0.000000"


@pytest.mark.parametrize("failure", ["timeout", "server", "invalid", "batch", "encoding", "large"])
def test_uncertain_response_holds_budget_and_goes_to_manual_without_resubmit(
    configured, client, episode, failure
):
    project_id = episode["project_id"]
    set_policy(client, project_id, "20")
    job = capture_ok(client, project_id, image_request(configured))
    requests = []

    def fail(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(CANARY, request=request)
        if failure == "server":
            return response({"error": {"message": CANARY}}, 503)
        if failure == "invalid":
            return response({"data": [{"url": "https://private.invalid/" + CANARY}]})
        if failure == "batch":
            return response(success(2))
        if failure == "encoding":
            return response({"data": [{"b64_json": "%%%"}]})
        return response({"data": [{"b64_json": "A" * 2_000_000}]})

    provider = OpenAIImageProvider(configured, transport=httpx.MockTransport(fail))
    event_id = event_for(client, job)
    assert deliver(client, job, provider, event_id) == "RESULT_UNKNOWN"
    assert deliver(client, job, provider, event_id) == "RESULT_UNKNOWN"
    assert len(requests) == 1
    attempt = attempt_for(client, job)
    assert attempt.cost is None and attempt.result_json is None
    costs = client.get(f"/api/v1/projects/{project_id}/costs").json()["summary"]
    assert costs["pending_estimate"] == "0.500000" and costs["actual_cost"] is None
    current_job = client.get(base(project_id) + f"/{job['id']}").json()
    outcome = query(client, current_job, QueryFake(QueryReceipt(status="UNSUPPORTED")))
    assert outcome.status_code == 200 and outcome.json()["status"] == "MANUAL_REVIEW"
    with client.app.state.session_factory() as db:
        receipts = list(db.scalars(select(GatewayReceipt)))
        assert len(receipts) == 1 and CANARY not in str(receipts[0].usage_json)


def test_key_file_and_output_handles_are_private_and_bound_to_the_attempt(
    configured, client, episode, tmp_path
):
    secret_file = tmp_path / "image-key"
    secret_file.write_text(CANARY, encoding="utf-8")
    configured.image_service = configured.image_service.model_copy(
        update={"api_key": SecretStr(""), "api_key_file": secret_file}
    )
    set_policy(client, episode["project_id"], "20")
    job = capture_ok(client, episode["project_id"], image_request(configured))
    provider = OpenAIImageProvider(
        configured, transport=httpx.MockTransport(lambda req: response(success()))
    )
    assert deliver(client, job, provider) == "SUCCEEDED"
    attempt = attempt_for(client, job)
    request = MediaSubmission(
        UUID(job["id"]), attempt.id, attempt.input_manifest_id, attempt.input_snapshot_json, None
    )
    handle = attempt.result_json["output_handles"][0]
    assert provider.download(request, handle, 1024 * 1024).data
    foreign = MediaSubmission(
        uuid4(), attempt.id, attempt.input_manifest_id, attempt.input_snapshot_json, None
    )
    with pytest.raises(DomainError, match="当前尝试"):
        provider.download(foreign, handle, 1024 * 1024)
    with pytest.raises(DomainError):
        provider.download(request, "https://api.openai.com/private", 1024 * 1024)
    with pytest.raises(DomainError):
        provider.download(request, handle, 1)


def test_exact_legacy_mock_input_schema_remains_executable(client, episode, monkeypatch):
    schema = json.loads(
        (Path(__file__).parent / "fixtures/generation-input-v1.json").read_text(encoding="utf-8")
    )
    assert digest(schema) == "5ce490e456d24b3f3f8cc7897706f1636c09de322dacc28edf05f42631702c4b"
    with monkeypatch.context() as legacy:
        legacy.setattr(GenerationSpec, "model_json_schema", lambda **kwargs: schema)
        job = capture_ok(client, episode["project_id"])
    assert deliver(client, job) == "SUCCEEDED"


@pytest.mark.parametrize(
    "width,height,kind,quality",
    [(1024, 1024, "png", "low"), (1536, 1024, "jpeg", "medium"), (1024, 1536, "jpg", "high")],
)
def test_supported_outputs_use_worker_selection_and_candidate_validation(
    configured, client, episode, monkeypatch, width, height, kind, quality
):
    from arclight.workers import tasks

    set_policy(client, episode["project_id"], "20")
    body = image_request(configured)
    body["spec"].update(
        output_spec={"width": width, "height": height, "format": kind},
        parameters={"quality": quality},
    )
    job = capture_ok(client, episode["project_id"], body)
    image_kind = "PNG" if kind == "png" else "JPEG"
    data = image_bytes(image_kind, (width, height))
    requests = []

    def handle(request):
        sent = json.loads(request.content)
        requests.append(sent)
        assert sent["size"] == f"{width}x{height}" and sent["quality"] == quality
        assert ("output_compression" in sent) == (kind != "png")
        return response({"data": [{"b64_json": base64.b64encode(data).decode()}]})

    provider = OpenAIImageProvider(configured, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(tasks, "get_settings", lambda: configured)
    real_factory = tasks.get_media_submission_provider

    def selected(name, settings):
        assert isinstance(real_factory(name, settings), OpenAIImageProvider)
        return provider

    monkeypatch.setattr(tasks, "get_media_submission_provider", selected)
    message = messages(client)[0]
    assert (
        tasks.execute_generation.run(str(message[0]), str(message[1]), str(message[2]), message[3])
        == "SUCCEEDED"
    )
    attempt = attempt_for(client, job)
    assert tasks.import_generation_candidates.run(str(attempt.id)) == 1
    assert tasks.import_generation_candidates.run(str(attempt.id)) == 0
    rows = candidates(client, episode["project_id"], job)
    assert len(requests) == 1 and rows[0]["error_code"] is None
    assert rows[0]["required_review"] == "CONTINUITY"
    assert len(result_object_key(attempt.result_json["output_handles"][0])) < 90


def test_reference_images_are_rejected_before_a_billable_attempt(configured, client, episode):
    set_policy(client, episode["project_id"], "20")
    source = approved(client, create(client, episode["project_id"]))
    body = image_request(configured)
    body["inputs"] = [
        {"source_asset_version_id": source["id"], "dependency_type": "REFERENCE_DEPENDENCY"}
    ]
    rejected = capture(client, episode["project_id"], body)
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "PROVIDER_REFERENCES_UNSUPPORTED"
    assert attempt_for(client, {"id": str(uuid4())}) is None


@pytest.mark.parametrize("phase", ["before", "after"])
def test_storage_unavailable_releases_only_before_acceptance(
    configured, client, episode, monkeypatch, phase
):
    set_policy(client, episode["project_id"], "20")
    job = capture_ok(client, episode["project_id"], image_request(configured))
    requests = []

    def handle(request):
        requests.append(request)
        return response(success())

    provider = OpenAIImageProvider(configured, transport=httpx.MockTransport(handle))

    def unavailable(*args):
        raise DomainError("STORAGE_UNAVAILABLE", CANARY, 503)

    monkeypatch.setattr(provider.store, "check" if phase == "before" else "put", unavailable)
    expected = "FAILED" if phase == "before" else "RESULT_UNKNOWN"
    assert deliver(client, job, provider) == expected
    assert len(requests) == (0 if phase == "before" else 1)
    attempt = attempt_for(client, job)
    assert attempt.cost == (0 if phase == "before" else None)
    assert CANARY not in str(attempt.result_json)


def test_corrupt_private_result_is_rejected_on_download(configured, client, episode):
    set_policy(client, episode["project_id"], "20")
    job = capture_ok(client, episode["project_id"], image_request(configured))
    provider = OpenAIImageProvider(
        configured, transport=httpx.MockTransport(lambda req: response(success()))
    )
    assert deliver(client, job, provider) == "SUCCEEDED"
    attempt = attempt_for(client, job)
    handle = attempt.result_json["output_handles"][0]
    provider.store.path(result_object_key(handle)).write_bytes(b"corrupted")
    request = MediaSubmission(
        UUID(job["id"]), attempt.id, attempt.input_manifest_id, attempt.input_snapshot_json, None
    )
    with pytest.raises(DomainError, match="完整性"):
        provider.download(request, handle, 1024 * 1024)
    assert import_attempt(client.app.state.session_factory, configured, attempt.id, provider) == 1
    assert candidates(client, episode["project_id"], job)[0]["error_code"] is not None


@pytest.mark.parametrize("failure", ["redirect", "dns"])
def test_image_network_fails_closed_without_following_or_retrying(
    configured, client, episode, monkeypatch, failure
):
    from arclight.providers import network

    set_policy(client, episode["project_id"], "20")
    job = capture_ok(client, episode["project_id"], image_request(configured))
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "http://127.0.0.1/" + CANARY})

    def blocked(*args):
        raise DomainError("SERVICE_ADDRESS_BLOCKED", CANARY, 422)

    if failure == "dns":
        monkeypatch.setattr(network, "resolve_public", blocked)
    provider = OpenAIImageProvider(
        configured, transport=httpx.MockTransport(redirect) if failure == "redirect" else None
    )
    assert deliver(client, job, provider) == "RESULT_UNKNOWN"
    assert len(requests) == (1 if failure == "redirect" else 0)
    with client.app.state.session_factory() as db:
        assert db.scalar(select(GatewayReceipt)).actual_cost is None


def test_postgres_real_provider_quotes_compete_for_one_budget(configured, client, episode):
    require_postgres(configured)
    project_id = UUID(episode["project_id"])
    set_policy(client, project_id, "0.500000")
    barrier = Barrier(2)

    def create_one(_):
        barrier.wait(timeout=15)
        try:
            with client.app.state.session_factory.begin() as db:
                create_job(
                    db,
                    project_id,
                    "service-owner",
                    str(uuid4()),
                    GenerationCreate.model_validate(image_request(configured)),
                )
            return "CREATED"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create_one, range(2))) == ["BUDGET_EXCEEDED", "CREATED"]
    costs = client.get(f"/api/v1/projects/{project_id}/costs").json()["summary"]
    assert costs["pending_estimate"] == "0.500000" and costs["entry_count"] == 1


def test_live_acceptance_harness_with_mock_http_only(
    settings, client, episode, monkeypatch, tmp_path
):
    from test_openai_images_live import test_one_authorized_synthetic_image

    from arclight.workers import tasks

    if not settings.database_url.startswith("sqlite:///"):
        pytest.skip("Live harness deliberately requires an isolated SQLite fixture")
    key_file = tmp_path / "mock-live-key"
    key_file.write_text(CANARY, encoding="utf-8")
    monkeypatch.setenv("ARCLIGHT_LIVE_IMAGE_KEY_FILE", str(key_file))
    monkeypatch.setenv("ARCLIGHT_LIVE_IMAGE_MAX_USD", "0.50")
    monkeypatch.setenv("ARCLIGHT_LIVE_IMAGE_PRICING_VERSION", "mock-live-price:1")
    monkeypatch.setenv("ARCLIGHT_LIVE_IMAGE_BASE_URL", "https://image-provider.example")
    monkeypatch.setenv("ARCLIGHT_LIVE_IMAGE_MODEL", "gpt-image-2.5-sunburst")
    calls = []

    def handle(request):
        calls.append(request)
        assert str(request.url) == "https://image-provider.example/v1/images/generations"
        assert request.headers["authorization"] == "Bearer " + CANARY
        body = json.loads(request.content)
        assert body["n"] == 1 and body["model"] == "gpt-image-2.5-sunburst"
        return response(success())

    def selected(name, server):
        assert name == "openai" and server is settings
        return OpenAIImageProvider(server, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(tasks, "get_media_submission_provider", selected)
    test_one_authorized_synthetic_image(settings, client, episode, monkeypatch, tmp_path)
    assert len(calls) == 1
    evidence = (tmp_path / "live-image-evidence.json").read_text(encoding="utf-8")
    assert CANARY not in evidence and "image_sha256" in evidence
    assert "gpt-image-2.5-sunburst" in evidence


@pytest.mark.parametrize(
    "source,expected",
    [
        ("https://image-provider.example", "https://image-provider.example/v1"),
        ("https://image-provider.example/", "https://image-provider.example/v1"),
        ("https://IMAGE-provider.example:443/v1/", "https://image-provider.example/v1"),
        ("https://image-provider.example/proxy/v1/", "https://image-provider.example/proxy/v1"),
    ],
)
def test_custom_endpoint_normalization_has_a_stable_configuration_version(source, expected):
    service = OpenAIImageSettings(base_url=source, model="gpt-image-2.5-sunburst")
    canonical = OpenAIImageSettings(base_url=expected, model="gpt-image-2.5-sunburst")
    assert service.base_url == expected
    assert service.public_configuration() == canonical.public_configuration()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://image-provider.example",
        "https://127.0.0.1/v1",
        "https://service.local/v1",
        "https://image-provider.example:8443/v1",
        "https://user:secret@image-provider.example/v1",
        "https://image-provider.example/v1?key=" + CANARY,
        "https://image-provider.example/v1#" + CANARY,
        "https://image-provider.example/\n" + CANARY,
    ],
)
def test_image_endpoint_rejects_private_unsafe_or_secret_bearing_urls(endpoint):
    with pytest.raises(ValidationError) as caught:
        OpenAIImageSettings(base_url=endpoint)
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize(
    "kind,size,error",
    [
        ("PNG", (1024, 1024), None),
        ("PNG", (1254, 1254), None),
        ("JPEG", (1254, 1254), None),
        ("PNG", (2001, 2000), "INVALID_IMAGE"),
        ("PNG", (4097, 1), "INVALID_IMAGE"),
        ("INVALID", (1, 1), "INVALID_IMAGE"),
    ],
)
def test_provider_output_variants_keep_actual_metadata_and_cost_evidence(
    configured, client, episode, kind, size, error
):
    project_id = episode["project_id"]
    set_policy(client, project_id, "20")
    configured.image_service = configured.image_service.model_copy(
        update={"base_url": "https://image-provider.example/v1", "model": "gpt-image-2.5-sunburst"}
    )
    target = create(client, project_id)
    job = capture_ok(client, project_id, image_request(configured, target))
    requests = []

    def handle(request):
        requests.append(request)
        assert json.loads(request.content)["size"] == "1024x1024"
        assert json.loads(request.content)["output_format"] == "jpeg"
        data = b"not an image" if kind == "INVALID" else image_bytes(kind, size)
        return response(success(data=[{"b64_json": base64.b64encode(data).decode()}]))

    provider = OpenAIImageProvider(configured, transport=httpx.MockTransport(handle))
    event = event_for(client, job)
    assert deliver(client, job, provider, event) == "SUCCEEDED"
    assert deliver(client, job, provider, event) == "SUCCEEDED"
    attempt = attempt_for(client, job)
    factory = client.app.state.session_factory
    assert import_attempt(factory, configured, attempt.id, provider) == 1
    assert import_attempt(factory, configured, attempt.id, provider) == 0
    rows = candidates(client, project_id, job)
    assert len(requests) == 1 and len(rows) == 1
    assert rows[0]["error_code"] == error
    if error is None:
        media = rows[0]["version"]["media"]
        expected_media_type = "image/png" if kind == "PNG" else "image/jpeg"
        assert rows[0]["asset_version_id"] is not None
        assert media["media_type"] == expected_media_type
        assert (media["width"], media["height"]) == size
    else:
        assert rows[0]["asset_version_id"] is None and rows[0]["version"] is None
    assert current(client, target)["current_version_id"] is None
    costs = client.get(f"/api/v1/projects/{project_id}/costs").json()["summary"]
    assert costs["entry_count"] == 1 and costs["pending_estimate"] == "0.500000"
    assert costs["actual_cost"] is None and costs["cost_is_estimate"]
