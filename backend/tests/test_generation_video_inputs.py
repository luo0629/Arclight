"""EXT-01: exact approved first frames traverse the normal native generation path."""

import base64
import copy
import hashlib
import json
import os
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from art_concept_support import complete_concept_job, install_concept_model, start_concepts
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from storage_helpers import clear_test_bucket
from test_agent_model import model_service as model_service
from test_art_review_workflow import decide
from test_art_workflow import drive, progress, proposal
from test_asset_selection import choose
from test_asset_versions import image_bytes
from test_asset_versions import save as save_image
from test_asset_versions import state as image_state
from test_assets import create as create_asset
from test_generation_accounting import invoice, ledger, usage
from test_generation_budget import set_policy
from test_generation_delivery import counts, deliver, event_for
from test_generation_jobs import base, capture, capture_ok
from test_generation_reconciliation import query
from test_model_services import account
from test_native_media_integration import attempt_for, body_for, mp4_bytes, project, provider_for
from test_text_contract_v2 import make_asset

from arclight.application.agent_external_waits import queue_ready_external
from arclight.application.asset_objects import object_path, read_object
from arclight.application.generation_artifacts import import_attempt as import_media
from arclight.core.errors import DomainError
from arclight.domain.service_models import ServiceModelPolicy
from arclight.persistence.generation import GenerationDispatch
from arclight.persistence.generation_accounting import GenerationBudgetSettlement
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    DependencyManifest,
    GenerationAttempt,
    GenerationJob,
    StoredFile,
    VisualAsset,
)
from arclight.storage.objects import ObjectStorage, initialize

MODEL = "doubao-seedance-1-0-pro-250528"
PROMPT = "雨指向门口，林后退半步并转头看向她；保持两人的互动和动作接续。"
KEY = "ext01-fixture-credential"


def configure_video(client, settings, *, model=MODEL, revision=0, key=KEY):
    if not settings.service_encryption_key.get_secret_value():
        settings.service_encryption_key = SecretStr(Fernet.generate_key().decode())
    settings.service_model_policies = [
        ServiceModelPolicy(
            kind="video",
            supplier="custom",
            protocol="seedance",
            model=model,
            base_url="https://native.example/v1",
            maximum_cost_per_output=Decimal("1"),
            pricing_version="ext01-fixture:1",
        )
    ]
    response = client.put(
        "/api/v1/me/model-services/video",
        json={
            "mode": "custom",
            "supplier": "custom",
            "model": model,
            "protocol": "seedance",
            "api_key": key,
            "base_url": "https://native.example/v1",
            "expected_revision": revision,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def first_frame(
    client,
    project_id,
    *,
    asset=None,
    kind="PNG",
    size=(320, 480),
    color="#1b4671",
    approve=True,
    original_name=None,
):
    asset = asset or create_asset(client, project_id, "SCENE", "首帧")
    data = image_bytes(kind, size=size, color=color)
    version = save_image(
        client,
        asset,
        original_name=original_name or ("first-frame.png" if kind == "PNG" else "first-frame.jpg"),
        media_type="image/png" if kind == "PNG" else "image/jpeg",
        content_base64=base64.b64encode(data).decode("ascii"),
    )
    if approve:
        for status in ("READY_FOR_REVIEW", "APPROVED"):
            response = image_state(client, asset, version, status)
            assert response.status_code == 200, response.text
            version = response.json()
    return asset, version, data


def with_frame(body, version_id):
    value = copy.deepcopy(body)
    value["inputs"] = [
        {"source_asset_version_id": str(version_id), "dependency_type": "REFERENCE_DEPENDENCY"}
    ]
    return value


@pytest.fixture
def video_case(client, settings):
    account(client)
    configure_video(client, settings)
    project_id = project(client)
    response = client.post(
        f"/api/v1/projects/{project_id}/episodes",
        json={"episode_no": 1, "title": "门口的冲突", "target_duration_ms": 30000},
    )
    assert response.status_code == 201, response.text
    episode = response.json()
    set_policy(client, project_id, "20")
    asset, version, data = first_frame(client, project_id)
    body = body_for(client, project_id, "video")
    body["episode_id"] = episode["id"]
    body["spec"]["prompt"] = PROMPT
    return {
        "project_id": project_id,
        "episode_id": episode["id"],
        "asset": asset,
        "version": version,
        "data": data,
        "body": with_frame(body, version["id"]),
    }


def frozen_history(client, job):
    with client.app.state.session_factory() as db:
        manifest = db.get(DependencyManifest, UUID(job["input_manifest_id"]))
        attempts = list(
            db.scalars(select(GenerationAttempt).where(GenerationAttempt.job_id == UUID(job["id"])))
        )
        audit = list(
            db.scalars(select(AuditEvent).where(AuditEvent.project_id == UUID(job["project_id"])))
        )
        dispatches = list(
            db.scalars(
                select(GenerationDispatch).where(GenerationDispatch.job_id == UUID(job["id"]))
            )
        )
        return copy.deepcopy(manifest.snapshot_json), json.dumps(
            [
                manifest.snapshot_json,
                [row.input_snapshot_json for row in attempts],
                [row.details_json for row in audit],
                [row.receipt_json for row in dispatches],
            ],
            ensure_ascii=False,
        )


def assert_no_transport_media(value, data):
    for forbidden in (
        base64.b64encode(data).decode("ascii"),
        "data:image/",
        KEY,
        "signature=ephemeral",
        "object_key",
        "content_base64",
    ):
        assert forbidden not in value


@pytest.mark.parametrize("kind", ["PNG", "JPEG"])
def test_native_first_frame_payload_uses_exact_approved_bytes_and_no_persisted_transport_media(
    client, settings, video_case, caplog, kind
):
    case = video_case
    version, data = case["version"], case["data"]
    if kind == "JPEG":
        _, version, data = first_frame(client, case["project_id"], kind=kind)
    body = with_frame(case["body"], version["id"])
    job = capture_ok(client, case["project_id"], body)
    manifest, persisted = frozen_history(client, job)
    reference = manifest["context"]["video_images"][0]
    assert manifest["context"]["budget_quote"]["pricing_version"] == "ext01-fixture:1"
    configure_video(client, settings, revision=1, key="ext01-rotated-credential")
    assert reference["kind"] == "CONTROLLED_IMAGE" and reference["role"] == "first_frame"
    assert reference["asset_version_id"] == version["id"]
    assert reference["checksum"] == hashlib.sha256(data).hexdigest()
    assert reference["byte_size"] == len(data)
    assert reference["media_type"] == ("image/png" if kind == "PNG" else "image/jpeg")
    requests = []

    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/v1/contents/generations/tasks"
        assert request.headers["authorization"] == "Bearer " + KEY
        payload = json.loads(request.content)
        assert payload["model"] == MODEL
        assert payload["duration"] == 6
        text = [item["text"] for item in payload["content"] if item["type"] == "text"]
        images = [item for item in payload["content"] if item["type"] == "image_url"]
        assert text == [PROMPT]
        assert len(images) == 1 and images[0]["role"] == "first_frame"
        uri = images[0]["image_url"]["url"]
        assert uri.startswith("data:" + reference["media_type"] + ";base64,")
        assert base64.b64decode(uri.split(",", 1)[1], validate=True) == data
        requests.append(payload)
        return httpx.Response(200, json={"id": "task_1"})

    provider = provider_for(client, settings, job, handler)
    assert deliver(client, job, provider) == "SUBMITTED"
    assert deliver(client, job, provider) == "SUBMITTED"
    assert len(requests) == 1
    after, persisted = frozen_history(client, job)
    assert after == manifest
    assert_no_transport_media(persisted + caplog.text, data)
    with client.app.state.session_factory() as db:
        attempt = db.get(GenerationAttempt, attempt_for(client, job))
        assert attempt.input_snapshot_json == manifest


def test_unknown_recovery_queries_original_request_and_private_import_and_invoice_are_idempotent(
    client, settings, video_case, caplog
):
    case = video_case
    key = "ext01-original-command"
    created = capture(client, case["project_id"], case["body"], key)
    assert created.status_code == 201, created.text
    job = created.json()
    replay = capture(client, case["project_id"], case["body"], key)
    assert replay.status_code == 201 and replay.json()["id"] == job["id"]
    requests = []
    video = mp4_bytes()

    def handler(request):
        requests.append((request.method, request.url.host, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={"id": "task_1"})
        if request.url.host == "cdn.example":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=video)
        assert request.url.path == "/v1/contents/generations/tasks/task_1"
        assert request.content == b""
        return httpx.Response(
            200,
            json={
                "status": "succeeded",
                "content": {"video_url": "https://cdn.example/video?signature=ephemeral"},
            },
        )

    provider = provider_for(client, settings, job, handler)
    assert deliver(client, job, provider) == "SUBMITTED"
    # Corrupted source bytes after acceptance cannot force an upload on query/recovery.
    object_path(
        settings.asset_storage_path, UUID(case["project_id"]), UUID(case["version"]["id"])
    ).write_bytes(b"unavailable after accepted request")
    with client.app.state.session_factory.begin() as db:
        from arclight.application.generation_reconciliation import mark_result_unknown

        row = db.get(GenerationJob, UUID(job["id"]))
        attempt_id = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == row.id)
        )
        mark_result_unknown(db, row.id, attempt_id, request_id="task_1")
    latest = client.get(base(case["project_id"]) + "/" + job["id"]).json()
    assert latest["status"] == "RESULT_UNKNOWN"
    restored = provider_for(client, settings, job, handler)
    response = query(client, latest, restored)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "SUCCEEDED"
    assert deliver(client, job, restored) == "SUCCEEDED"
    attempt_id = attempt_for(client, job)
    assert import_media(client.app.state.session_factory, settings, attempt_id, restored) == 1
    assert import_media(client.app.state.session_factory, settings, attempt_id, restored) == 0
    artifact_path = base(case["project_id"]) + f"/{job['id']}/artifacts"
    response = client.get(artifact_path)
    assert response.status_code == 200, response.text
    artifact = response.json()[0]
    assert artifact["file_id"] and artifact["error_code"] is None
    download_path = artifact_path + f"/{artifact['id']}/content"
    download = client.get(download_path)
    assert download.status_code == 200 and download.content == video
    assert download.headers["cache-control"] == "private, no-store"
    assert usage(client, case["project_id"]) == (Decimal("0"), Decimal("1"))
    reservation = ledger(client, case["project_id"])[0]
    assert reservation["status"] == "HELD"
    settled = invoice(client, case["project_id"], reservation, "0.750000")
    assert settled.status_code == 200, settled.text
    assert invoice(client, case["project_id"], reservation, "0.750000").json() == settled.json()
    assert usage(client, case["project_id"]) == (Decimal("0.75"), Decimal("0"))
    assert sum(method == "POST" for method, _, _ in requests) == 1
    assert sum(host == "cdn.example" for _, host, _ in requests) == 1
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationBudgetSettlement)) == 1
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 1
    _, persisted = frozen_history(client, job)
    assert_no_transport_media(persisted + response.text + settled.text + caplog.text, case["data"])
    account(client)
    assert client.get(download_path).status_code == 404


@pytest.mark.parametrize("fault", ["unsupported_model", "multiple_frames"])
def test_reference_capability_refusals_leave_no_generation_records(
    client, settings, video_case, fault
):
    case = video_case
    body = copy.deepcopy(case["body"])
    if fault == "unsupported_model":
        configure_video(client, settings, model="doubao-seedance-1-0-pro-unlisted", revision=1)
        body["spec"] = body_for(client, case["project_id"], "video")["spec"]
    else:
        _, version, _ = first_frame(client, case["project_id"])
        body["inputs"].append(
            {"source_asset_version_id": version["id"], "dependency_type": "REFERENCE_DEPENDENCY"}
        )
    before = counts(client)
    response = capture(client, case["project_id"], body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PROVIDER_REFERENCES_UNSUPPORTED"
    assert counts(client) == before


@pytest.mark.parametrize(
    "fault,status,code",
    [
        ("draft", 409, "ASSET_NOT_USABLE"),
        ("stale", 409, "ASSET_NOT_USABLE"),
        ("foreign", 404, "NOT_FOUND"),
        ("profile_without_image", 422, "VIDEO_INPUT_INVALID"),
        ("scope", 403, "CONTEXT_SCOPE_NOT_ALLOWED"),
        ("rights", 409, "VIDEO_INPUT_RIGHTS_UNCONFIRMED"),
        ("corrupt", 422, "VIDEO_INPUT_INVALID"),
        ("checksum", 422, "VIDEO_INPUT_INVALID"),
        ("too_large", 422, "VIDEO_INPUT_INVALID"),
    ],
)
def test_unusable_or_unauthorized_first_frames_are_refused_before_queueing(
    client, settings, video_case, fault, status, code
):
    case = video_case
    body = copy.deepcopy(case["body"])
    if fault == "draft":
        _, version, _ = first_frame(client, case["project_id"], approve=False)
        body = with_frame(body, version["id"])
    elif fault == "foreign":
        _, version, _ = first_frame(client, project(client))
        body = with_frame(body, version["id"])
    elif fault == "profile_without_image":
        with client.app.state.session_factory.begin() as db:
            reference = make_asset(db, case["project_id"], "CHARACTER")
        body = with_frame(body, reference["asset_version_id"])
    elif fault in {"rights", "checksum", "too_large"}:
        field, value = {
            "rights": ("rights_confirmed", False),
            "checksum": ("checksum", "0" * 64),
            "too_large": ("byte_size", 30 * 1024 * 1024),
        }[fault]
        import_name = f"first-frame-{fault}-{uuid4().hex}.png"
        inserted = []

        def import_inconsistent_receipt(_mapper, _connection, stored):
            if stored.original_name == import_name and stored.project_id == UUID(
                case["project_id"]
            ):
                setattr(stored, field, value)
                inserted.append(stored.id)

        # Represent an isolated bad historical import without rewriting protected history.
        event.listen(StoredFile, "before_insert", import_inconsistent_receipt)
        try:
            _, version, _ = first_frame(client, case["project_id"], original_name=import_name)
        finally:
            event.remove(StoredFile, "before_insert", import_inconsistent_receipt)
        assert len(inserted) == 1
        body = with_frame(body, version["id"])
    elif fault == "corrupt":
        object_path(
            settings.asset_storage_path, UUID(case["project_id"]), UUID(case["version"]["id"])
        ).write_bytes(b"not an image")
    else:
        with client.app.state.session_factory.begin() as db:
            version = db.get(AssetVersion, UUID(case["version"]["id"]))
            asset = db.get(VisualAsset, version.asset_id)
            if fault == "stale":
                version.stale = True
            elif fault == "scope":
                asset.metadata_json = {
                    "usage_scope": {"kind": "EPISODES", "episode_ids": [str(uuid4())]}
                }
    before = counts(client)
    response = capture(client, case["project_id"], body)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert counts(client) == before
    assert_no_transport_media(response.text, case["data"])


@pytest.mark.parametrize("size", [(299, 480), (320, 299), (300, 751), (751, 300)])
def test_first_frame_dimensions_and_ratio_are_enforced_before_queueing(client, video_case, size):
    case = video_case
    _, version, _ = first_frame(client, case["project_id"], size=size)
    before = counts(client)
    response = capture(client, case["project_id"], with_frame(case["body"], version["id"]))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VIDEO_INPUT_INVALID"
    assert counts(client) == before


def test_new_current_version_does_not_replace_the_frozen_first_frame(client, settings, video_case):
    case = video_case
    job = capture_ok(client, case["project_id"], case["body"])
    manifest, _ = frozen_history(client, job)
    path = f"/api/v1/projects/{case['project_id']}/assets/{case['asset']['id']}"
    current = client.get(path).json()
    _, newer, newer_data = first_frame(client, case["project_id"], asset=current, color="red")
    assert newer_data != case["data"]
    current = client.get(path).json()
    response = choose(client, current, newer)
    assert response.status_code == 200, response.text
    sent = []

    def handler(request):
        payload = json.loads(request.content)
        image = next(item for item in payload["content"] if item["type"] == "image_url")
        sent.append(base64.b64decode(image["image_url"]["url"].split(",", 1)[1], validate=True))
        return httpx.Response(200, json={"id": "task_1"})

    assert deliver(client, job, provider_for(client, settings, job, handler)) == "SUBMITTED"
    assert sent == [case["data"]]
    assert frozen_history(client, job)[0] == manifest


@pytest.mark.parametrize(
    "fault", ["bytes", "checksum", "rights", "scope", "archived_source", "missing_object"]
)
def test_frozen_first_frame_tampering_fails_before_an_attempt_or_external_call(
    client, settings, video_case, fault
):
    case = video_case
    job = capture_ok(client, case["project_id"], case["body"])
    original, history_before = frozen_history(client, job)
    path = object_path(
        settings.asset_storage_path, UUID(case["project_id"]), UUID(case["version"]["id"])
    )
    if fault in {"checksum", "rights"}:
        field, value = {
            "checksum": ("checksum", "f" * 64),
            "rights": ("rights_confirmed", False),
        }[fault]
        with client.app.state.session_factory() as db:
            stored = db.scalar(
                select(StoredFile).where(StoredFile.asset_version_id == UUID(case["version"]["id"]))
            )
            before = (stored.checksum, stored.rights_confirmed, stored.byte_size)
            setattr(stored, field, value)
            with pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"):
                db.flush()
            db.rollback()
            db.refresh(stored)
            assert (stored.checksum, stored.rights_confirmed, stored.byte_size) == before
            persisted_job = db.get(GenerationJob, UUID(job["id"]))
            assert persisted_job.status == job["status"]
            assert persisted_job.input_manifest_id == UUID(job["input_manifest_id"])
        assert frozen_history(client, job) == (original, history_before)
        # Database history stays intact; external object corruption is still refused.
        path.write_bytes(b"changed after immutable metadata update was refused")
    elif fault == "bytes":
        path.write_bytes(b"changed after freezing")
    elif fault == "missing_object":
        path.unlink()
    else:
        with client.app.state.session_factory.begin() as db:
            version = db.get(AssetVersion, UUID(case["version"]["id"]))
            asset = db.get(VisualAsset, version.asset_id)
            if fault == "archived_source":
                asset.status = "ARCHIVED"
            else:
                asset.metadata_json = {
                    "usage_scope": {"kind": "EPISODES", "episode_ids": [str(uuid4())]}
                }
    calls = []

    def handler(request):
        calls.append(request)
        raise AssertionError("invalid frozen image must fail before HTTP")

    assert deliver(client, job, provider_for(client, settings, job, handler)) == "FAILED"
    assert calls == []
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 0
        dispatch = db.get(GenerationDispatch, event_for(client, job))
        assert dispatch.status == "SKIPPED"
    assert frozen_history(client, job)[0] == original
    assert usage(client, case["project_id"]) == (Decimal("0"), Decimal("0"))


def test_provider_error_cannot_persist_inline_first_frame_or_credential(
    client, settings, video_case, caplog
):
    case = video_case
    job = capture_ok(client, case["project_id"], case["body"])

    def handler(request):
        payload = json.loads(request.content)
        image = next(item for item in payload["content"] if item["type"] == "image_url")
        return httpx.Response(
            400,
            json={"error": {"message": image["image_url"]["url"] + " Bearer " + KEY}},
        )

    assert deliver(client, job, provider_for(client, settings, job, handler)) == "RETRY_WAIT"
    response = client.get(base(case["project_id"]) + "/" + job["id"])
    assert response.status_code == 200
    _, persisted = frozen_history(client, job)
    assert_no_transport_media(persisted + response.text + caplog.text, case["data"])


def test_approved_concept_reference_reuses_its_actual_rights_approval(
    client, settings, video_case, model_service
):
    case = video_case
    episode = {"id": case["episode_id"], "project_id": case["project_id"]}
    _, errors, drafts = install_concept_model(model_service, rounds=1)
    run_id, _ = start_concepts(
        client,
        episode,
        settings,
        rounds=1,
        spec_patch={"output_spec": {"width": 320, "height": 480, "format": "png"}},
    )
    waiting = drive(client, settings, run_id)
    assert waiting["status"] == "RUNNING", waiting
    detail = progress(client, run_id)
    assert complete_concept_job(client, detail["concept_rounds"][-1]["job_id"]) == 2
    assert queue_ready_external(client.app.state.session_factory, settings) == 1
    waiting = drive(client, settings, run_id)
    assert waiting["status"] == "WAITING_REVIEW", (waiting, errors)
    assert errors == []
    detail = progress(client, run_id)
    saved = proposal(client, episode, detail["delivery"])
    approved = decide(client, saved)
    assert approved.status_code == 200, approved.text
    reference = drafts[-1]["proposal"]["references"][0]["asset"]
    version_id = reference["asset_version_id"]
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(version_id))
        stored = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version.id))
        assert version.status == "APPROVED"
        assert not stored.rights_confirmed
        data = read_object(
            settings, UUID(case["project_id"]), version.id, version.object_key, version.checksum
        )
    job = capture_ok(client, case["project_id"], with_frame(case["body"], version_id))
    sent = []

    def handler(request):
        payload = json.loads(request.content)
        image = next(item for item in payload["content"] if item["type"] == "image_url")
        sent.append(base64.b64decode(image["image_url"]["url"].split(",", 1)[1], validate=True))
        return httpx.Response(200, json={"id": "task_1"})

    assert deliver(client, job, provider_for(client, settings, job, handler)) == "SUBMITTED"
    assert sent == [data]


@pytest.mark.parametrize("forged", ["empty", "controlled_image"])
def test_client_cannot_supply_internal_video_image_context_or_reserve_budget(
    client, video_case, forged
):
    case = video_case
    body = copy.deepcopy(case["body"])
    images = []
    if forged == "controlled_image":
        with client.app.state.session_factory() as db:
            stored = db.scalar(
                select(StoredFile).where(StoredFile.asset_version_id == UUID(case["version"]["id"]))
            )
            images = [
                {
                    "kind": "CONTROLLED_IMAGE",
                    "role": "first_frame",
                    "file_id": str(stored.id),
                    "asset_version_id": case["version"]["id"],
                    "checksum": stored.checksum,
                    "byte_size": stored.byte_size,
                    "media_type": stored.media_type,
                }
            ]
    body["spec"]["context"] = {"video_images": images}
    before = counts(client)
    before_ledger = ledger(client, case["project_id"])
    before_usage = usage(client, case["project_id"])
    response = capture(client, case["project_id"], body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VIDEO_INPUT_INVALID"
    assert counts(client) == before
    assert ledger(client, case["project_id"]) == before_ledger
    assert usage(client, case["project_id"]) == before_usage
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 0
    assert_no_transport_media(response.text, case["data"])


def test_s3_first_frame_reads_its_historical_bucket_after_current_bucket_changes(
    client, settings, video_case
):
    endpoint = os.getenv("ARCLIGHT_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("Requires isolated S3 test service")
    case = video_case
    settings.object_storage_backend = "s3"
    settings.s3_endpoint_url = endpoint
    settings.s3_public_endpoint_url = endpoint
    settings.s3_access_key = SecretStr("arclight-test")
    settings.s3_secret_key = SecretStr("arclight-test-secret")
    settings.s3_bucket = "arclight-test-ext01-source-" + uuid4().hex
    stores = []
    try:
        source_configuration = settings.model_copy(deep=True)
        initialize(source_configuration)
        source_store = ObjectStorage(source_configuration)
        stores.append(source_store)
        _, version, data = first_frame(client, case["project_id"])
        with client.app.state.session_factory() as db:
            stored = db.scalar(
                select(StoredFile).where(StoredFile.asset_version_id == UUID(version["id"]))
            )
            assert stored.storage_backend == "S3"
            assert stored.bucket == source_configuration.s3_bucket
            stored_key, checksum = stored.object_key, stored.checksum
        assert source_store.read(stored_key, None, checksum) == data
        job = capture_ok(client, case["project_id"], with_frame(case["body"], version["id"]))
        original, _ = frozen_history(client, job)
        assert original["context"]["video_images"][0]["asset_version_id"] == version["id"]
        assert source_configuration.s3_bucket not in json.dumps(original)
        assert "object_key" not in json.dumps(original)
        settings.s3_bucket = "arclight-test-ext01-current-" + uuid4().hex
        current_configuration = settings.model_copy(deep=True)
        initialize(current_configuration)
        current_store = ObjectStorage(current_configuration)
        stores.append(current_store)
        assert current_configuration.s3_bucket != source_configuration.s3_bucket
        with pytest.raises(DomainError) as missing:
            current_store.read(stored_key, None, checksum)
        assert missing.value.code == "OBJECT_UNAVAILABLE"
        requests = []

        def handler(request):
            assert request.method == "POST"
            payload = json.loads(request.content)
            image = next(item for item in payload["content"] if item["type"] == "image_url")
            uri = image["image_url"]["url"]
            assert uri.startswith("data:image/png;base64,")
            assert base64.b64decode(uri.split(",", 1)[1], validate=True) == data
            requests.append(payload)
            return httpx.Response(200, json={"id": "task_1"})

        provider = provider_for(client, settings, job, handler)
        assert deliver(client, job, provider) == "SUBMITTED"
        assert len(requests) == 1
        assert frozen_history(client, job)[0] == original
        assert source_store.read(stored_key, None, checksum) == data
        assert_no_transport_media(frozen_history(client, job)[1], data)
    finally:
        for store in reversed(stores):
            clear_test_bucket(store)
