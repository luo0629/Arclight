"""Concept limits, uncertainty, STORE evidence, exact approval and real PG races."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from art_concept_support import complete_concept_job, install_concept_model, start_concepts
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from test_agent_model import model_service as model_service
from test_art_review_workflow import decide
from test_art_workflow import drive, progress, proposal
from test_concurrency import require_postgres
from test_generation_budget import priced, set_policy
from test_generation_candidates import ingest
from test_generation_delivery import deliver
from test_generation_jobs import capture, payload

from arclight.application.agent_external_waits import queue_ready_external
from arclight.application.agent_limits import action_usage
from arclight.application.agent_tools import execute_agent_tool
from arclight.application.art_concept_jobs import concept_jobs, concept_origin
from arclight.application.art_direction import art_direction_registry
from arclight.application.art_research_assets import read_reference
from arclight.application.asset_objects import object_path
from arclight.application.generation_accounting import costs
from arclight.application.generation_attempts import finish_attempt, start_attempt
from arclight.application.generation_candidates import require_candidate_review
from arclight.application.generation_leases import utc
from arclight.core.errors import DomainError
from arclight.domain.agent_tools import AgentToolCall
from arclight.domain.context import ContextLimits
from arclight.domain.generation import GenerationStatus
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.art_proposals import ArtApprovedVersion
from arclight.persistence.base import utcnow
from arclight.persistence.generation import GenerationAttempt, GenerationJob
from arclight.persistence.generation_accounting import GenerationBudgetReservation
from arclight.persistence.generation_candidates import GenerationCandidate
from arclight.persistence.generation_scheduling import GenerationSchedule
from arclight.persistence.models import (
    AgentToolGrant,
    AssetVersion,
    OutboxEvent,
    VisualAsset,
    WorkflowRun,
)
from arclight.providers.media_outputs import MediaOutput, MockMediaOutputProvider
from arclight.providers.media_submission import MockMediaSubmissionProvider, SubmissionUncertain


def waiting(client, episode, settings, model_service, **options):
    sent, errors, _ = install_concept_model(model_service, rounds=options.get("rounds", 2))
    run_id, body = start_concepts(client, episode, settings, **options)
    drive(client, settings, run_id)
    detail = progress(client, run_id)
    assert not errors, [item["error"] for item in errors]
    assert detail["operations"][0]["status"] == "WAITING_EXTERNAL", detail
    return run_id, body, detail["concept_rounds"][0], sent, errors


def tool(client, settings, run_id, name, arguments, *, key=None):
    with client.app.state.session_factory.begin() as db:
        return execute_agent_tool(
            db,
            art_direction_registry(settings),
            "art_director",
            AgentToolCall(
                run_id=UUID(run_id),
                tool_name=name,
                arguments=arguments,
                idempotency_key=key or uuid4().hex,
            ),
        )


def delivered_one(client, episode, settings, model_service):
    run_id, _, row, sent, errors = waiting(client, episode, settings, model_service, rounds=1)
    complete_concept_job(client, row["job_id"])
    assert queue_ready_external(client.app.state.session_factory, settings) == 1
    assert drive(client, settings, run_id)["status"] == "WAITING_REVIEW", errors
    return run_id, proposal(client, episode, progress(client, run_id)["delivery"]), sent


@pytest.mark.parametrize(
    "mutation", ["target", "batch", "quantity", "rounds", "context", "parameter"]
)
def test_frozen_plan_rejects_uncontrolled_capabilities_and_limits(
    client, episode, settings, mutation
):
    _, body = start_concepts(client, episode, settings)
    changed = deepcopy(body)
    plan = changed["concept_plan"]
    if mutation == "target":
        plan["target_type"] = "SHOT"
    elif mutation == "batch":
        plan["spec"]["batch_size"] = 5
    elif mutation == "quantity":
        plan["maximum_candidates"] = 1
    elif mutation == "rounds":
        plan["maximum_rounds"] = 5
    elif mutation == "context":
        plan["spec"]["context"] = {"art_concept": {"run_id": str(uuid4())}}
    else:
        plan["spec"]["parameters"] = {"n": 64}
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/art-runs",
        json=changed,
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 422, response.text
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0


def test_round_replay_uses_original_job_quote_and_reservation_and_rejects_changed_args(
    client, episode, settings, model_service
):
    run_id, _, original, _, _ = waiting(client, episode, settings, model_service)
    arguments = {
        "round_no": 1,
        "prompt": "第1轮降低背景饱和度，保留银发轮廓",
        "rationale": "探索清晰轮廓",
    }
    replay = tool(client, settings, run_id, "request_art_concepts", arguments)
    assert replay.status == "SUCCEEDED", replay
    assert str(replay.result["job_id"]) == original["job_id"]
    with client.app.state.session_factory() as db:
        schedule = db.scalar(
            select(GenerationSchedule).where(GenerationSchedule.job_id == UUID(original["job_id"]))
        )
        assert utc(schedule.deadline_at) == utc(db.get(AgentRunLimit, UUID(run_id)).deadline_at)
    conflict = tool(
        client, settings, run_id, "request_art_concepts", arguments | {"prompt": "不同方向"}
    )
    assert conflict.error.code == "IDEMPOTENCY_CONFLICT"
    unknown = tool(
        client, settings, run_id, "request_art_concepts", arguments | {"maximum_cost": "999"}
    )
    assert unknown.error.code == "INVALID_ARGUMENTS"
    skipped = tool(client, settings, run_id, "request_art_concepts", arguments | {"round_no": 3})
    assert skipped.error.code == "ART_CONCEPT_ROUND_LIMIT"
    with client.app.state.session_factory() as db:
        assert len(concept_jobs(db, run_id=UUID(run_id))) == 1
        assert db.scalar(select(func.count()).select_from(GenerationBudgetReservation)) == 1


def test_revision_requires_real_comparison_and_cross_run_observation_is_forbidden(
    client, episode, settings, model_service
):
    run_id, _, original, _, _ = waiting(client, episode, settings, model_service)
    complete_concept_job(client, original["job_id"])
    second = tool(
        client,
        settings,
        run_id,
        "request_art_concepts",
        {
            "round_no": 2,
            "prompt": "尚未比较就重做",
            "rationale": "伪造依据",
            "previous_comparison_sha256": "a" * 64,
        },
    )
    assert second.error.code == "ART_CONCEPT_COMPARISON_REQUIRED"
    other_episode = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "另一个范围"},
    ).json()
    other, _, _, _, _ = waiting(client, other_episode, settings, model_service)
    observed = tool(client, settings, other, "observe_art_concepts", {"job_id": original["job_id"]})
    assert observed.status == "FAILED" and observed.error.code == "NOT_FOUND"


def test_root_budget_counts_generation_reservation_before_next_model_and_retry(
    client, episode, settings, model_service, monkeypatch
):
    priced(monkeypatch, "1")
    set_policy(client, episode["project_id"], limit="20")
    sent, errors, _ = install_concept_model(model_service)
    run_id, _ = start_concepts(client, episode, settings, budget="10", limits={"maximum_cost": "1"})
    drive(client, settings, run_id)
    assert not errors and len(sent) == 2
    assert progress(client, run_id)["operations"][0]["stop_code"] == "AGENT_BUDGET_EXCEEDED"
    with client.app.state.session_factory() as db:
        assert not concept_jobs(db, run_id=UUID(run_id))


def test_original_generation_retry_cannot_reset_concept_budget(
    client, episode, settings, model_service, monkeypatch
):
    priced(monkeypatch, "1")
    set_policy(client, episode["project_id"], limit="20")
    run_id, _, original, _, _ = waiting(client, episode, settings, model_service, budget="2")
    job_id = UUID(original["job_id"])
    with client.app.state.session_factory.begin() as db:
        job = db.get(GenerationJob, job_id)
        attempt = start_attempt(db, job.id, job.revision)
        finish_attempt(
            db,
            job.id,
            attempt.id,
            GenerationStatus.FAILED,
            error_code="PROVIDER_FAILED",
            cost=Decimal("1"),
        )
        assert action_usage(db, UUID(run_id))["actual_cost"] == Decimal("1")
        revision = job.revision
    retry = client.post(
        f"/api/v1/projects/{episode['project_id']}/generation-jobs/{job_id}/retry",
        json={"expected_revision": revision},
    )
    assert retry.status_code == 409 and retry.json()["error"]["code"] == "ART_CONCEPT_BUDGET", (
        retry.text
    )
    with client.app.state.session_factory() as db:
        assert db.get(GenerationJob, job_id).attempt_count == 1
        assert costs(db, UUID(episode["project_id"]), job_id=job_id) == (Decimal("1"), Decimal("0"))


def test_cancelling_external_wait_before_submission_releases_only_original_unspent_reservation(
    client, episode, settings, model_service, monkeypatch
):
    priced(monkeypatch, "1")
    set_policy(client, episode["project_id"], limit="20")
    run_id, _, original, sent, _ = waiting(client, episode, settings, model_service)
    response = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert response.status_code == 200, response.text
    assert queue_ready_external(client.app.state.session_factory, settings) == 0
    assert len(sent) == 3
    with client.app.state.session_factory() as db:
        job = db.get(GenerationJob, UUID(original["job_id"]))
        assert job.status == "CANCELLED" and job.attempt_count == 0
        assert costs(db, job.project_id, job_id=job.id) == (Decimal("0"), Decimal("0"))
        assert db.scalar(select(func.count()).select_from(GenerationCandidate)) == 0
        assert db.scalar(select(func.count()).select_from(ArtApprovedVersion)) == 0


def test_unknown_result_is_observed_once_never_retried_or_shown_as_zero_and_cancel_keeps_hold(
    client, episode, settings, model_service, monkeypatch
):
    priced(monkeypatch, "1")
    set_policy(client, episode["project_id"], limit="20")
    run_id, _, original, sent, _ = waiting(client, episode, settings, model_service)

    class Unknown(MockMediaSubmissionProvider):
        calls = 0

        def submit(self, request):
            self.calls += 1
            raise SubmissionUncertain("Synthetic transport outcome unknown")

    provider = Unknown()
    assert deliver(client, {"id": original["job_id"]}, provider) == "RESULT_UNKNOWN"
    assert deliver(client, {"id": original["job_id"]}, provider) == "RESULT_UNKNOWN"
    assert provider.calls == 1
    detail = progress(client, run_id)["concept_rounds"][0]
    assert detail["actual_cost"] is None and Decimal(detail["reserved_cost"]) == 2
    assert queue_ready_external(client.app.state.session_factory, settings) == 1
    drive(client, settings, run_id)
    assert progress(client, run_id)["operations"][0]["status"] == "WAITING_HUMAN"
    assert len(sent) == 4
    job_path = f"/api/v1/projects/{episode['project_id']}/generation-jobs/{original['job_id']}"
    current = client.get(job_path).json()
    assert (
        client.post(
            job_path + "/retry", json={"expected_revision": current["revision"]}
        ).status_code
        == 409
    )
    assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
    with client.app.state.session_factory() as db:
        job = db.get(GenerationJob, UUID(original["job_id"]))
        assert job.status == "RESULT_UNKNOWN" and job.attempt_count == 1
        assert job.cancel_requested_at is not None
        assert costs(db, job.project_id, job_id=job.id) == (Decimal("0"), Decimal("2"))
        assert not progress(client, run_id)["delivery"]


def test_partial_invalid_media_is_recorded_without_duplicate_assets_or_early_resume(
    client, episode, settings, model_service
):
    run_id, _, original, _, errors = waiting(
        client, episode, settings, model_service, rounds=1, minimum=1
    )
    assert deliver(client, {"id": original["job_id"]}, MockMediaSubmissionProvider()) == "SUCCEEDED"
    assert queue_ready_external(client.app.state.session_factory, settings) == 0
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(original["job_id"]))
        )

    class Partial(MockMediaOutputProvider):
        def download(self, request, handle, max_bytes):
            return (
                MediaOutput(b"broken", "image/png")
                if handle.endswith("/1")
                else super().download(request, handle, max_bytes)
            )

    assert ingest(client, attempt, Partial()) == 2
    assert ingest(client, attempt, Partial()) == 0
    assert queue_ready_external(client.app.state.session_factory, settings) == 1
    assert drive(client, settings, run_id)["status"] == "WAITING_REVIEW", errors
    candidates = progress(client, run_id)["concept_rounds"][0]["candidates"]
    assert sum(item["error_code"] == "INVALID_IMAGE" for item in candidates) == 1
    assert sum(item["asset_version_id"] is not None for item in candidates) == 1


@pytest.mark.parametrize("decision", ["REJECT", "APPROVE"])
def test_reference_approval_is_explicit_and_rejection_keeps_original_media_history(
    client, episode, settings, model_service, decision
):
    run_id, saved, _ = delivered_one(client, episode, settings, model_service)
    refs = saved["version"]["proposal_json"]["references"]
    with client.app.state.session_factory() as db:
        before = {
            ref["asset"]["asset_version_id"]: deepcopy(
                db.get(AssetVersion, UUID(ref["asset"]["asset_version_id"])).metadata_json
            )
            for ref in refs
        }
    result = decide(client, saved, decision)
    assert result.status_code == 200, result.text
    assert decide(client, saved, decision).json() == result.json()
    with client.app.state.session_factory() as db:
        for ref in refs:
            version = db.get(AssetVersion, UUID(ref["asset"]["asset_version_id"]))
            assert version.status == ("APPROVED" if decision == "APPROVE" else "DRAFT")
            assert version.metadata_json == before[str(version.id)]
            assert db.get(VisualAsset, version.asset_id).current_version_id is None
            if decision == "APPROVE":
                read = read_reference(
                    db,
                    settings,
                    UUID(episode["project_id"]),
                    UUID(episode["id"]),
                    version.id,
                    ContextLimits(),
                    include_images=True,
                )
                assert read.reason is None and len(read.image_file_ids) == 1
        assert db.scalar(select(func.count()).select_from(ArtApprovedVersion)) == (
            3 if decision == "APPROVE" else 0
        )
    assert progress(client, run_id)["delivery"]["version_id"] == saved["version"]["id"]


def test_lock_changed_while_waiting_for_approval_prevents_any_reference_or_design_write(
    client, episode, settings, model_service
):
    _, saved, _ = delivered_one(client, episode, settings, model_service)
    ref = saved["version"]["proposal_json"]["references"][0]
    with client.app.state.session_factory.begin() as db:
        asset = db.get(VisualAsset, UUID(ref["asset"]["asset_id"]))
        asset.status = "LOCKED"
        asset.lock_version += 1
    response = decide(client, saved)
    assert response.status_code == 409, response.text
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ArtApprovedVersion)) == 0
        assert all(
            db.get(AssetVersion, UUID(item["asset"]["asset_version_id"])).status == "DRAFT"
            for item in saved["version"]["proposal_json"]["references"]
        )


def test_pending_waits_do_not_starve_later_ready_runs_with_a_bounded_scan(
    client, episode, settings, model_service
):
    first_id, _, _, _, _ = waiting(client, episode, settings, model_service, rounds=1)
    other_episode = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "后续概念图"},
    ).json()
    ready_id, _, ready, _, _ = waiting(client, other_episode, settings, model_service, rounds=1)
    complete_concept_job(client, ready["job_id"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        first = db.scalar(select(AgentLoop).where(AgentLoop.run_id == UUID(first_id)))
        first.updated_at = utcnow() - timedelta(days=1)
    assert queue_ready_external(factory, settings, limit=1) == 0
    assert queue_ready_external(factory, settings, limit=1) == 1
    with factory() as db:
        assert db.get(WorkflowRun, UUID(first_id)).status == "RUNNING"
        assert db.get(WorkflowRun, UUID(ready_id)).status == "QUEUED"


@pytest.mark.parametrize("target_type", ["SHOT_IMAGE", "CHARACTER"])
def test_forged_spec_context_cannot_change_the_original_continuity_gate(
    client, episode, settings, model_service, target_type
):
    run_id, _, original, _, _ = waiting(client, episode, settings, model_service, rounds=1)
    with client.app.state.session_factory() as db:
        origin = deepcopy(concept_origin(db, db.get(GenerationJob, UUID(original["job_id"]))))
    origin["round_no"] = 2
    body = payload(target_type=target_type, episode_id=episode["id"])
    body["spec"]["context"] = {
        "art_concept": origin,
        "required_review": "ART_PROPOSAL",
    }
    body["spec"]["output_spec"] = {"width": 8, "height": 12, "format": "png"}
    response = capture(client, episode["project_id"], body, key=f"art-concept:{run_id}:2")
    assert response.status_code == 201, response.text
    job = response.json()
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    assert ingest(client, attempt) == 1
    with client.app.state.session_factory() as db:
        candidate = db.scalar(
            select(GenerationCandidate).where(GenerationCandidate.job_id == UUID(job["id"]))
        )
        version = db.get(AssetVersion, candidate.asset_version_id)
        assert concept_origin(db, db.get(GenerationJob, UUID(job["id"]))) is None
        assert version.metadata_json["required_review"] == "CONTINUITY"
        with pytest.raises(DomainError) as error:
            require_candidate_review(version, db)
        assert error.value.code == "CONTINUITY_REVIEW_REQUIRED"


def test_frozen_concept_inputs_and_parent_deadline_reject_historical_rewrites(
    client, episode, settings, model_service
):
    run_id, _, original, _, _ = waiting(client, episode, settings, model_service)
    factory = client.app.state.session_factory
    with factory() as db:
        saved_input = deepcopy(db.get(WorkflowRun, UUID(run_id)).input_json)
        deadline = utc(db.get(AgentRunLimit, UUID(run_id)).deadline_at)
    with pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        changed = deepcopy(saved_input)
        changed["art_direction"]["concept"]["plan"]["maximum_cost"] = "999"
        run.input_json = changed
    with pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
        schedule = db.scalar(
            select(GenerationSchedule).where(GenerationSchedule.job_id == UUID(original["job_id"]))
        )
        schedule.deadline_at = deadline + timedelta(days=1)
    with factory() as db:
        assert db.get(WorkflowRun, UUID(run_id)).input_json == saved_input
        schedule = db.scalar(
            select(GenerationSchedule).where(GenerationSchedule.job_id == UUID(original["job_id"]))
        )
        assert utc(schedule.deadline_at) == deadline


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("grant", "USER_NOT_AUTHORIZED"),
        ("deadline", "AGENT_DEADLINE_EXCEEDED"),
        ("stale", "RUN_STATE_INVALID"),
    ],
)
def test_external_wait_and_media_dispatch_recheck_original_authority_without_new_calls(
    client, episode, settings, model_service, monkeypatch, mutation, code
):
    run_id, _, original, sent, _ = waiting(client, episode, settings, model_service)
    with client.app.state.session_factory.begin() as db:
        if mutation == "grant":
            db.scalar(
                select(AgentToolGrant).where(AgentToolGrant.run_id == UUID(run_id))
            ).allowed_tools_json = []
        elif mutation == "stale":
            run = db.get(WorkflowRun, UUID(run_id))
            run.agent_state_json = run.agent_state_json | {"input_sha256": "f" * 64}
        else:
            deadline = db.get(AgentRunLimit, UUID(run_id)).deadline_at
    if mutation == "deadline":
        monkeypatch.setattr(
            "arclight.application.agent_limits.utcnow",
            lambda: deadline.replace(tzinfo=utcnow().tzinfo) + timedelta(seconds=1),
        )

    class NeverSubmit(MockMediaSubmissionProvider):
        calls = 0

        def submit(self, request):
            self.calls += 1
            return super().submit(request)

    provider = NeverSubmit()
    assert deliver(client, {"id": original["job_id"]}, provider) == "FAILED"
    assert provider.calls == 0
    assert queue_ready_external(client.app.state.session_factory, settings) == 1
    assert queue_ready_external(client.app.state.session_factory, settings) == 0
    state = drive(client, settings, run_id)
    assert state["status"] == "FAILED" and state["error_code"] == code, state
    assert len(sent) == 3
    assert progress(client, run_id)["delivery"] is None


def test_changed_capability_is_rejected_before_first_generation_request(
    client, episode, settings, model_service, monkeypatch
):
    from arclight.application import art_concepts
    from arclight.providers.media_capabilities import MOCK_CAPABILITIES

    run_id, _ = start_concepts(client, episode, settings)
    sent, errors, _ = install_concept_model(model_service)
    monkeypatch.setattr(
        art_concepts,
        "resolve_capabilities",
        lambda *args: MOCK_CAPABILITIES.model_copy(update={"version": "changed-after-freeze"}),
    )
    drive(client, settings, run_id)
    assert any(item["error"]["code"] == "ART_CONCEPT_INPUT_CHANGED" for item in errors), errors
    assert len(sent) == 3 and not progress(client, run_id)["concept_rounds"]


def test_store_checksum_is_rechecked_before_model_receives_concept_images(
    client, episode, settings, model_service
):
    run_id, _, original, sent, errors = waiting(client, episode, settings, model_service, rounds=1)
    complete_concept_job(client, original["job_id"])
    with client.app.state.session_factory() as db:
        key = db.scalar(
            select(GenerationCandidate.asset_version_id).where(
                GenerationCandidate.job_id == UUID(original["job_id"])
            )
        )
        version = db.get(AssetVersion, key)
        path = object_path(settings.asset_storage_path, UUID(episode["project_id"]), version.id)
    path.write_bytes(b"synthetic corrupted media")
    assert queue_ready_external(client.app.state.session_factory, settings) == 1
    drive(client, settings, run_id)
    assert errors and errors[0]["error"]["code"] == "ASSET_OBJECT_UNAVAILABLE", errors
    assert progress(client, run_id)["delivery"] is None
    assert len(sent) == 4


@pytest.mark.parametrize("phase", ["request", "wakeup", "approve"])
def test_postgres_competing_concept_transactions_preserve_one_effect(
    client, episode, settings, model_service, phase
):
    require_postgres(settings)
    run_id, _, original, _, errors = waiting(client, episode, settings, model_service, rounds=1)
    barrier = Barrier(2)
    saved = None
    if phase in {"wakeup", "approve"}:
        complete_concept_job(client, original["job_id"])
    if phase == "approve":
        assert queue_ready_external(client.app.state.session_factory, settings) == 1
        drive(client, settings, run_id)
        saved = proposal(client, episode, progress(client, run_id)["delivery"])

    def compete(_):
        barrier.wait(timeout=15)
        if phase == "request":
            return tool(
                client,
                settings,
                run_id,
                "request_art_concepts",
                {
                    "round_no": 1,
                    "prompt": "第1轮降低背景饱和度，保留银发轮廓",
                    "rationale": "探索清晰轮廓",
                },
            )
        if phase == "wakeup":
            return queue_ready_external(client.app.state.session_factory, settings)
        return decide(client, saved)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, range(2)))
    assert not errors
    if phase == "request":
        assert all(
            item.status == "SUCCEEDED" and str(item.result["job_id"]) == original["job_id"]
            for item in results
        )
    elif phase == "wakeup":
        assert sorted(results) == [0, 1]
        assert drive(client, settings, run_id)["status"] == "WAITING_REVIEW"
    else:
        assert all(item.status_code == 200 for item in results), [item.text for item in results]
        assert results[0].json() == results[1].json()
    with client.app.state.session_factory() as db:
        assert len(concept_jobs(db, run_id=UUID(run_id))) == 1
        assert db.scalar(select(func.count()).select_from(AgentLoop)) == 1
        assert db.scalar(select(func.count()).select_from(GenerationBudgetReservation)) == 1
        assert db.scalar(select(func.count()).select_from(ArtApprovedVersion)) == (
            3 if phase == "approve" else 0
        )
        assert db.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.run_id == UUID(run_id))
        ) == (1 if phase == "request" else 3 if phase == "approve" else 2)
