"""GEN-08: capabilities constrain captured input, actual adapters and recovery."""

import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from itertools import count
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from test_asset_selection import approved
from test_asset_versions import private_objects  # noqa: F401
from test_assets import create
from test_collaboration import register
from test_generation_delivery import Provider, deliver
from test_generation_jobs import base, capture, capture_ok, payload
from test_generation_leases import expire
from test_generation_reconciliation import QueryFake, query
from test_generation_scheduling import configure, fresh

from arclight.application import generation_jobs
from arclight.application import generation_leases as leases
from arclight.application.generation_attempts import finish_attempt, start_attempt
from arclight.application.generation_delivery import consume_dispatch
from arclight.application.generation_recovery import recover_job
from arclight.application.generation_state import request_cancel
from arclight.core.errors import DomainError
from arclight.domain.generation import GenerationStatus as Status
from arclight.domain.generation_requests import GenerationCreate, GenerationResult, GenerationSpec
from arclight.domain.media_capabilities import MediaCapabilities, validate_capabilities
from arclight.persistence.generation import GenerationAttempt, GenerationJob
from arclight.persistence.models import DependencyManifest
from arclight.providers.media_cancel import CancellationReceipt
from arclight.providers.media_capabilities import MOCK_CAPABILITIES, resolve_parameters
from arclight.providers.media_query import QueryReceipt
from arclight.providers.media_submission import MockMediaSubmissionProvider, SubmissionReceipt


def profile(**changes):
    return MediaCapabilities.model_validate(MOCK_CAPABILITIES.model_dump(mode="json") | changes)


def use_profile(monkeypatch, **changes):
    capabilities = profile(**changes)
    monkeypatch.setattr(generation_jobs, "resolve_capabilities", lambda *_: capabilities)
    return capabilities


def manifest(client, job):
    with client.app.state.session_factory() as db:
        return deepcopy(db.get(DependencyManifest, UUID(job["input_manifest_id"])).snapshot_json)


def records(client):
    with client.app.state.session_factory() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (GenerationJob, GenerationAttempt, DependencyManifest)
        )


def test_only_installed_capabilities_are_public_and_permission_scoped(client, episode):
    url = base(episode["project_id"]) + "/providers/mock/capabilities"
    response = client.get(url)
    assert response.status_code == 200
    values = response.json()
    assert values["version"] == "mock:1" and values["max_batch_size"] == 8
    assert values["max_reference_images"] == 64 and values["supports_seed"]
    assert values["execution_mode"] == "SYNC"
    assert values["supports_query"] is values["supports_cancel"] is False
    assert "secret" not in response.text
    assert client.get(url, params={"model": "x" * 161}).status_code == 422
    assert client.get(url.replace("/mock/", "/uninstalled/")).status_code == 422
    register(client, "capability-outsider@example.com")
    assert client.get(url).status_code == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"max_batch_size": 0},
        {"max_reference_images": -1},
        {"supports_seed": "yes"},
        {"min_width": 2048, "max_width": 1024},
        {"task_types": []},
        {"sizes": [{"width": 9000, "height": 16}]},
    ],
)
def test_invalid_capability_declarations_are_rejected(changes):
    with pytest.raises(ValidationError):
        profile(**changes)


@pytest.mark.parametrize(
    "model,instructions_supported",
    [
        ("tts-1", False),
        ("tts-1-hd", False),
        ("gpt-4o-mini-tts", True),
        ("tts-1-compatible", True),
    ],
)
def test_native_tts_gateway_parameter_declaration_obeys_known_model_support(
    model, instructions_supported
):
    parameters = resolve_parameters("openai_tts", model)
    assert {"voice", "speed"} <= parameters
    assert ("instructions" in parameters) is instructions_supported


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"supports_seed": False}, "PROVIDER_SEED_UNSUPPORTED"),
        ({"max_seed": 16}, "PROVIDER_SEED_UNSUPPORTED"),
        ({"max_width": 512}, "PROVIDER_SIZE_UNSUPPORTED"),
        ({"max_pixels": 500000}, "PROVIDER_SIZE_UNSUPPORTED"),
        ({"dimension_multiple": 64}, "PROVIDER_SIZE_UNSUPPORTED"),
        ({"aspect_ratios": [{"width": 1, "height": 1}]}, "PROVIDER_ASPECT_UNSUPPORTED"),
        ({"sizes": [{"width": 512, "height": 512}]}, "PROVIDER_SIZE_UNSUPPORTED"),
        ({"task_types": ["AUDIO"]}, "PROVIDER_TASK_UNSUPPORTED"),
    ],
)
def test_create_rejects_unsupported_requests_without_any_records(
    client, episode, monkeypatch, changes, code
):
    use_profile(monkeypatch, **changes)
    before = records(client)
    response = capture(client, episode["project_id"])
    assert response.status_code == 422 and response.json()["error"]["code"] == code
    assert records(client) == before


@pytest.mark.parametrize(
    "option,code",
    [
        ("require_query", "PROVIDER_QUERY_UNSUPPORTED"),
        ("require_cancel", "PROVIDER_CANCEL_UNSUPPORTED"),
    ],
)
def test_required_control_capability_is_explicit_and_fails_before_queueing(
    client, episode, option, code
):
    body = payload()
    body["spec"][option] = True
    before = records(client)
    response = capture(client, episode["project_id"], body)
    assert response.status_code == 422 and response.json()["error"]["code"] == code
    assert records(client) == before


def test_reference_count_uses_exact_unique_image_versions_and_rechecks_at_execution(
    client, episode, monkeypatch
):
    source = create(client, episode["project_id"])
    version = approved(client, source)
    body = payload(
        inputs=[
            {"source_asset_version_id": version["id"], "dependency_type": kind}
            for kind in ("REFERENCE_DEPENDENCY", "STYLE_DEPENDENCY")
        ]
    )
    use_profile(monkeypatch, max_reference_images=0)
    before = records(client)
    rejected = capture(client, episode["project_id"], body)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "PROVIDER_REFERENCES_UNSUPPORTED"
    assert records(client) == before
    use_profile(monkeypatch, max_reference_images=1)
    job = capture_ok(client, episode["project_id"], body)
    snapshot = manifest(client, job)
    assert len(snapshot["inputs"]) == 2
    provider = Provider()
    provider.capabilities = profile(max_reference_images=0)
    assert deliver(client, job, provider) == "FAILED" and not provider.requests
    assert fresh(client, job)["attempt_count"] == 0
    assert manifest(client, job) == snapshot


@pytest.mark.parametrize(
    "width,height", [(0, 1), (960.0, 540), (True, 540), (8193, 2), (8192, 8192)]
)
def test_size_types_bounds_and_pixel_ceiling_are_enforced(client, episode, width, height):
    body = payload()
    body["spec"]["output_spec"].update(width=width, height=height)
    assert capture(client, episode["project_id"], body).status_code == 422


def test_declared_size_aspect_ratio_and_video_duration_boundaries():
    caps = profile(
        dimension_multiple=8,
        aspect_ratios=[{"width": 16, "height": 9}],
        sizes=[],
    )
    body = payload()["spec"]
    body["output_spec"].update(width=1280, height=720)
    validate_capabilities(caps, GenerationSpec.model_validate(body), "IMAGE", 0)
    exact = profile(sizes=[{"width": 960, "height": 540}])
    validate_capabilities(exact, GenerationSpec.model_validate(payload()["spec"]), "IMAGE", 0)
    body["output_spec"]["duration_seconds"] = 120
    validate_capabilities(caps, GenerationSpec.model_validate(body), "VIDEO", 0)
    body["output_spec"]["duration_seconds"] = 120.001
    with pytest.raises(DomainError, match="duration"):
        validate_capabilities(caps, GenerationSpec.model_validate(body), "VIDEO", 0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("parameters", {"num_images": 9}),
        ("parameters", {"width": 42}),
        ("parameters", {"options": [{"Reference_Images": ["untracked"]}]}),
        ("parameters", {"options": {"Seed": 9}}),
        ("output_spec", {"size": "unvalidated"}),
        ("batch_size", True),
        ("batch_size", 0),
        ("require_query", "yes"),
    ],
)
def test_alternate_parameter_paths_cannot_bypass_capability_validation(
    client, episode, field, value
):
    body = payload()
    body["spec"][field] = value
    assert capture(client, episode["project_id"], body).status_code == 422


def test_batch_upper_bound_and_actual_mock_output_count(client, episode):
    body = payload()
    body["spec"]["batch_size"] = 9
    response = capture(client, episode["project_id"], body)
    assert (
        response.status_code == 422
        and response.json()["error"]["code"] == "PROVIDER_BATCH_UNSUPPORTED"
    )
    body["spec"]["batch_size"] = 8
    job = capture_ok(client, episode["project_id"], body)
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    rows = client.get(base(job["project_id"]) + f"/{job['id']}/attempts").json()
    assert len(set(rows[0]["result_json"]["output_handles"])) == 8
    assert rows[0]["input_snapshot_json"]["context"]["provider_capabilities"]["max_batch_size"] == 8


def test_inconsistent_batch_receipt_is_unknown_and_never_resubmitted(client, episode):
    body = payload()
    body["spec"]["batch_size"] = 2
    job = capture_ok(client, episode["project_id"], body)
    provider = Provider()
    assert deliver(client, job, provider) == "RESULT_UNKNOWN"
    assert deliver(client, job, provider) == "RESULT_UNKNOWN" and len(provider.requests) == 1


def test_query_batch_mismatch_routes_to_manual_review(client, episode):
    body = payload()
    body["spec"]["batch_size"] = 2
    job = capture_ok(client, episode["project_id"], body)
    assert (
        deliver(
            client,
            job,
            Provider(
                receipt=SubmissionReceipt(
                    status="RESULT_UNKNOWN", error_code="RESULT_UNKNOWN", request_id="batch-query"
                )
            ),
        )
        == "RESULT_UNKNOWN"
    )
    response = query(
        client,
        fresh(client, job),
        QueryFake(
            QueryReceipt(status="SUCCEEDED", result=GenerationResult(output_handles=["only-one"]))
        ),
    )
    assert response.status_code == 200 and response.json()["status"] == "MANUAL_REVIEW"
    history = client.get(base(job["project_id"]) + f"/{job['id']}/reconciliations").json()
    assert history[0]["receipt_json"]["status"] == "INVALID_RESPONSE"


def test_live_capability_shrink_blocks_call_but_does_not_change_snapshot(client, episode):
    job = capture_ok(client, episode["project_id"])
    snapshot = manifest(client, job)
    provider = Provider()
    provider.capabilities = profile(version="mock:2", supports_seed=False)
    assert deliver(client, job, provider) == "FAILED" and not provider.requests
    assert manifest(client, job) == snapshot
    assert fresh(client, job)["attempt_count"] == 0


def test_default_options_preserve_historical_fingerprint_and_idempotent_replay(
    client, episode, monkeypatch
):
    body = payload()
    expected = GenerationCreate.model_validate(body).model_dump(mode="json")
    # GEN-13 adds an optional batch association; historical requests had no field.
    del expected["batch_id"]
    # GEN-16's default classification was also absent from the historical request.
    del expected["scheduling_mode"]
    # Exact review bindings were absent from historical generic generation requests.
    del expected["review_target"]
    for field in ("batch_size", "require_query", "require_cancel"):
        del expected["spec"][field]
    old_hash = hashlib.sha256(
        json.dumps(
            expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()
    assert old_hash == "f7c73161431ee88ce0a5d1ecbb9b2b5382d0e12c60bf292b7eef5f353c31e80b"
    assert generation_jobs.request_fingerprint(GenerationCreate.model_validate(body)) == old_hash
    first = capture(client, episode["project_id"], body, "stable-capture").json()
    use_profile(monkeypatch, supports_seed=False, version="mock:2")
    assert capture(client, episode["project_id"], body, "stable-capture").json() == first
    explicit_default = body | {"review_target": None}
    assert (
        capture(client, episode["project_id"], explicit_default, "stable-capture").json() == first
    )
    assert manifest(client, first)["context"]["provider_capabilities"]["version"] == "mock:1"
    changed = deepcopy(body)
    changed["spec"]["batch_size"] = 2
    assert capture(client, episode["project_id"], changed, "stable-capture").status_code == 409


def test_unsupported_query_and_cancel_are_never_called(client, episode):
    job = capture_ok(client, episode["project_id"])
    assert (
        deliver(
            client,
            job,
            Provider(receipt=SubmissionReceipt(status="RUNNING", request_id="unsupported-control")),
        )
        == "RUNNING"
    )
    with client.app.state.session_factory.begin() as db:
        row = db.get(GenerationJob, UUID(job["id"]))
        request_cancel(db, row.id, row.revision, "service-owner")
    expire(client.app.state.session_factory, job)

    class Unsupported:
        capabilities = MOCK_CAPABILITIES

        def cancel(self, _):
            pytest.fail("Unsupported cancellation must not call provider")

        def query(self, _):
            pytest.fail("Unsupported query must not call provider")

    assert (
        recover_job(
            client.app.state.session_factory,
            UUID(job["id"]),
            Unsupported(),
            cancel_provider=Unsupported(),
        )
        == "MANUAL_REVIEW"
    )
    assert fresh(client, job)["cancel_requested_at"] is not None
    history = client.get(base(job["project_id"]) + f"/{job['id']}/reconciliations").json()
    assert history[0]["receipt_json"]["status"] == "UNSUPPORTED"


def test_capability_snapshot_is_protected_by_existing_immutable_manifest_guard(client, episode):
    job = capture_ok(client, episode["project_id"])
    changed = manifest(client, job)
    changed["context"]["provider_capabilities"]["max_batch_size"] = 99
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(
            update(DependencyManifest)
            .where(DependencyManifest.id == UUID(job["input_manifest_id"]))
            .values(snapshot_json=changed)
        )


def test_direct_attempt_result_cannot_bypass_batch_contract(client, episode):
    body = payload()
    body["spec"]["batch_size"] = 2
    job = capture_ok(client, episode["project_id"], body)
    with client.app.state.session_factory.begin() as db:
        attempt = start_attempt(db, UUID(job["id"]), 0)
    with pytest.raises(DomainError, match="batch"), client.app.state.session_factory.begin() as db:
        finish_attempt(
            db,
            UUID(job["id"]),
            attempt.id,
            Status.SUCCEEDED,
            result=GenerationResult(output_handles=["one"]),
        )
    assert fresh(client, job)["status"] == "SUBMITTING"


@pytest.mark.parametrize(
    "confirmation,expected", [("CONFIRMED", "CANCELLED"), ("REQUESTED", "MANUAL_REVIEW")]
)
def test_cancellation_capability_does_not_require_query_capability(
    client, episode, confirmation, expected
):
    job = capture_ok(client, episode["project_id"])
    assert (
        deliver(
            client,
            job,
            Provider(receipt=SubmissionReceipt(status="RUNNING", request_id="cancel-only")),
        )
        == "RUNNING"
    )
    with client.app.state.session_factory.begin() as db:
        row = db.get(GenerationJob, UUID(job["id"]))
        request_cancel(db, row.id, row.revision, "service-owner")
    expire(client.app.state.session_factory, job)
    query_provider = QueryFake()
    query_provider.capabilities = profile(supports_query=False)

    class CancelOnly:
        capabilities = profile(supports_cancel=True, supports_query=False)
        calls = 0

        def cancel(self, _):
            self.calls += 1
            return CancellationReceipt(status=confirmation)

    canceller = CancelOnly()
    assert (
        recover_job(
            client.app.state.session_factory,
            UUID(job["id"]),
            query_provider,
            cancel_provider=canceller,
        )
        == expected
    )
    assert canceller.calls == 1 and not query_provider.calls


def test_required_query_cancel_and_seedless_request_accept_capable_adapter(
    client, episode, monkeypatch
):
    caps = use_profile(
        monkeypatch,
        supports_query=True,
        supports_cancel=True,
        supports_seed=False,
        execution_mode="BOTH",
    )
    body = payload()
    body["spec"].update(seed=None, require_query=True, require_cancel=True)
    job = capture_ok(client, episode["project_id"], body)
    provider = Provider()
    provider.capabilities = caps
    assert deliver(client, job, provider) == "SUCCEEDED"


def test_idempotency_and_execution_mode_declarations_are_enforced(client, episode):
    conflict = capture_ok(client, episode["project_id"])
    provider = Provider(supports=False)
    provider.capabilities = profile(supports_idempotency=True)
    assert deliver(client, conflict, provider) == "FAILED" and not provider.requests
    job = capture_ok(client, episode["project_id"])
    provider = Provider(receipt=SubmissionReceipt(status="RUNNING", request_id="sync-mismatch"))
    provider.capabilities = MOCK_CAPABILITIES
    assert deliver(client, job, provider) == "RESULT_UNKNOWN" and len(provider.requests) == 1


def test_legacy_job_is_validated_without_rewriting_old_capability_metadata(
    client, episode, monkeypatch
):
    from arclight.application.generation_events import enqueue_dispatch

    current = capture_ok(client, episode["project_id"])
    legacy = manifest(client, current)
    del legacy["context"]["provider_capabilities"]
    for field in ("batch_size", "require_query", "require_cancel"):
        del legacy["context"]["generation"][field]
    with client.app.state.session_factory.begin() as db:
        original = db.get(GenerationJob, UUID(current["id"]))
        frozen = DependencyManifest(
            project_id=original.project_id,
            target_asset_id=None,
            creation_key=UUID(current["id"]),
            request_fingerprint=original.request_fingerprint,
            created_by=original.created_by,
            snapshot_json=legacy,
        )
        db.add(frozen)
        db.flush()
        job = GenerationJob(
            project_id=original.project_id,
            target_type=original.target_type,
            task_type=original.task_type,
            input_manifest_id=frozen.id,
            provider=original.provider,
            model=original.model,
            idempotency_key="legacy-capability-job",
            request_fingerprint=original.request_fingerprint,
            created_by=original.created_by,
        )
        db.add(job)
        db.flush()
        event = enqueue_dispatch(db, job)
        identity, event_id, manifest_id = job.id, event.id, frozen.id
    with client.app.state.session_factory() as db:
        stamp = leases.clock(db)
    ticks = count()
    # PostgreSQL advances between reads; SQLite's second precision can hide this ordering bug.
    monkeypatch.setattr(leases, "clock", lambda _: stamp + timedelta(microseconds=next(ticks)))
    assert (
        consume_dispatch(client.app.state.session_factory, identity, event_id, Provider())
        == "SUCCEEDED"
    )
    with client.app.state.session_factory() as db:
        assert db.get(DependencyManifest, manifest_id).snapshot_json == legacy
        attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == identity))
        assert attempt.input_snapshot_json == legacy


def test_authorized_replay_retains_exact_reference_selectors_and_batch(client, episode):
    configure(client, max_attempts=1)
    source = create(client, episode["project_id"])
    version = approved(client, source)
    body = payload(
        inputs=[
            {
                "source_asset_version_id": version["id"],
                "dependency_type": "REFERENCE_DEPENDENCY",
                "selectors": ["face.front"],
            }
        ]
    )
    body["spec"]["batch_size"] = 2
    job = capture_ok(client, episode["project_id"], body)
    assert (
        deliver(
            client,
            job,
            Provider(receipt=SubmissionReceipt(status="FAILED", error_code="PROVIDER_FAILED")),
        )
        == "FAILED"
    )
    changed_style = client.post(
        f"/api/v1/projects/{job['project_id']}/visual-bible/versions",
        json={
            "expected_version_no": 0,
            "creation_key": str(uuid4()),
            "fields": {
                "art_style": "new style after failure",
                "negative_prompt": "new prohibition",
            },
        },
    )
    assert changed_style.status_code == 201, changed_style.text
    response = client.post(
        base(job["project_id"]) + f"/{job['id']}/replay",
        json={"expected_revision": fresh(client, job)["revision"], "reason": "bounded replay"},
        headers={"Idempotency-Key": "exact-reference-replay"},
    )
    assert response.status_code == 201, response.text
    replay = response.json()
    assert manifest(client, replay)["inputs"] == manifest(client, job)["inputs"]
    assert manifest(client, replay)["context"]["generation"]["batch_size"] == 2
    assert (
        manifest(client, replay)["context"]["generation"]
        == manifest(client, job)["context"]["generation"]
    )
    assert (
        manifest(client, replay)["context"]["prompt_composition"]
        == manifest(client, job)["context"]["prompt_composition"]
    )
