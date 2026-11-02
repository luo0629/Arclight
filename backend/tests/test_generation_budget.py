"""GEN-13: budget admission, execution rechecks, estimate and adjustment contracts."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_collaboration import create_project, register
from test_concurrency import require_postgres
from test_generation_delivery import Provider, deliver
from test_generation_jobs import base, capture, capture_ok, payload

from arclight.application.generation_jobs import create_job
from arclight.core.errors import DomainError
from arclight.domain.generation_budget import CostQuote
from arclight.domain.generation_requests import GenerationCreate
from arclight.persistence.generation_budget import GenerationBudgetQuote
from arclight.persistence.models import AuditEvent, GenerationAttempt, GenerationJob, Project
from arclight.providers import media_pricing
from arclight.providers.media_submission import SubmissionReceipt


def budget(project_id):
    return f"/api/v1/projects/{project_id}/budget"


def set_policy(client, project_id, limit="10.00", **limits):
    revision = client.get(budget(project_id)).json()["policy_revision"]
    result = client.patch(
        budget(project_id),
        json={
            "expected_revision": revision,
            "project_limit": limit,
            **limits,
        },
    )
    assert result.status_code == 200, result.text
    return result.json()


def priced(monkeypatch, amount="2", version="test-price-v1"):
    monkeypatch.setattr(
        media_pricing,
        "quote",
        lambda spec, task: CostQuote(
            maximum_cost=Decimal(amount) * spec.batch_size,
            pricing_version=version,
        ),
    )


def test_estimate_warning_budget_stop_idempotent_replay_and_read_history(
    client, episode, monkeypatch
):
    project_id = episode["project_id"]
    set_policy(client, project_id, "4", warning_percent=50)
    priced(monkeypatch)
    estimate = client.post(budget(project_id) + "/estimate", json=payload()).json()
    assert estimate["allowed"] and estimate["warning"]
    assert Decimal(estimate["estimate"]["maximum_cost"]) == 2
    key = str(uuid4())
    first = capture(client, project_id, key=key)
    assert first.status_code == 201, first.text
    second = capture_ok(client, project_id)
    stopped = capture(client, project_id)
    assert stopped.status_code == 409 and stopped.json()["error"]["code"] == "BUDGET_EXCEEDED"
    assert capture(client, project_id, key=key).json() == first.json()
    assert client.get(base(project_id) + "/" + second["id"]).status_code == 200
    current = client.get(budget(project_id)).json()
    assert current["warning"] and Decimal(current["pending_estimate"]) == 4
    quote = client.get(budget(project_id) + "/jobs/" + first.json()["id"]).json()
    assert quote["estimate"]["pricing_version"] == "test-price-v1"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 2


def test_unknown_quote_is_explicit_and_cannot_create_paid_job(client, episode, monkeypatch):
    project_id = episode["project_id"]
    monkeypatch.setattr(media_pricing, "quote", lambda spec, task: None)
    estimate = client.post(budget(project_id) + "/estimate", json=payload()).json()
    assert estimate["estimate"] is None and not estimate["allowed"]
    assert estimate["reason"] == "BUDGET_QUOTE_UNAVAILABLE"
    assert capture(client, project_id).status_code == 422
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0


@pytest.mark.parametrize(
    "limit,code",
    [
        ({"task_limit": "1"}, "BUDGET_TASK_EXCEEDED"),
        ({"episode_limit": "1"}, "BUDGET_EPISODE_EXCEEDED"),
        ({"batch_limit": "1"}, "BUDGET_BATCH_EXCEEDED"),
    ],
)
def test_each_scope_blocks_independently(client, episode, monkeypatch, limit, code):
    project_id = episode["project_id"]
    set_policy(client, project_id, **limit)
    priced(monkeypatch)
    result = capture(client, project_id, payload(episode_id=episode["id"]))
    assert result.status_code == 409 and result.json()["error"]["code"] == code


def test_episode_limit_counts_pending_jobs(client, episode, monkeypatch):
    project_id = episode["project_id"]
    set_policy(client, project_id, episode_limit="3")
    priced(monkeypatch)
    capture_ok(client, project_id, payload(episode_id=episode["id"]))
    result = capture(client, project_id, payload(episode_id=episode["id"]))
    assert result.json()["error"]["code"] == "BUDGET_EPISODE_EXCEEDED"


def test_batch_accumulates_rejects_mismatch_and_is_idempotent(client, episode, monkeypatch):
    project_id = episode["project_id"]
    set_policy(client, project_id, batch_limit="5")
    priced(monkeypatch)
    body = {"creation_key": str(uuid4()), "budget_limit": "3", "episode_id": episode["id"]}
    response = client.post(budget(project_id) + "/batches", json=body)
    assert response.status_code == 201, response.text
    batch_id = response.json()["id"]
    assert client.post(budget(project_id) + "/batches", json=body).json() == response.json()
    conflict = client.post(budget(project_id) + "/batches", json=body | {"budget_limit": "4"})
    assert conflict.status_code == 409
    assert capture(client, project_id, payload(batch_id=batch_id)).status_code == 422
    request = payload(batch_id=batch_id, episode_id=episode["id"])
    capture_ok(client, project_id, request)
    assert capture(client, project_id, request).json()["error"]["code"] == "BUDGET_BATCH_EXCEEDED"


@pytest.mark.parametrize("change", ["limit", "quote", "missing"])
def test_worker_rechecks_before_external_call(client, episode, monkeypatch, change):
    project_id = episode["project_id"]
    set_policy(client, project_id)
    priced(monkeypatch)
    job = capture_ok(client, project_id)
    if change == "limit":
        set_policy(client, project_id, "1")
    elif change == "quote":
        priced(monkeypatch, "3", "new-price")
    else:
        monkeypatch.setattr(media_pricing, "quote", lambda spec, task: None)
    provider = Provider()
    assert deliver(client, job, provider) == "FAILED"
    assert provider.requests == []
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 0


def test_cas_budget_policy_and_legacy_patch_revision(client, episode):
    project_id = episode["project_id"]
    current = set_policy(client, project_id)
    conflict = client.patch(
        budget(project_id), json={"expected_revision": 0, "project_limit": "100"}
    )
    assert conflict.status_code == 409
    response = client.patch(f"/api/v1/projects/{project_id}", json={"budget_limit": "11"})
    assert response.status_code == 200
    assert (
        client.get(budget(project_id)).json()["policy_revision"] == current["policy_revision"] + 1
    )


def test_adjustment_request_decision_replay_conflict_audit_and_pagination(client, episode):
    project_id = episode["project_id"]
    body = {"creation_key": str(uuid4()), "requested_limit": "20", "reason": "需要更多候选"}
    created = client.post(budget(project_id) + "/adjustments", json=body)
    assert created.status_code == 201, created.text
    adjustment = created.json()
    assert client.post(budget(project_id) + "/adjustments", json=body).json() == adjustment
    conflict = client.post(budget(project_id) + "/adjustments", json=body | {"reason": "另一用途"})
    assert conflict.status_code == 409
    path = budget(project_id) + f"/adjustments/{adjustment['id']}/decision"
    decision = {"approved": True, "expected_revision": 0, "reason": "批准一次调整"}
    accepted = client.post(path, json=decision)
    assert accepted.status_code == 200, accepted.text
    assert client.post(path, json=decision).json() == accepted.json()
    rows = client.get(budget(project_id) + "/adjustments?limit=1&offset=0").json()
    assert rows[0]["approved"] is True
    assert client.get(budget(project_id) + "/adjustments?limit=1&offset=1").json() == []
    assert Decimal(client.get(budget(project_id)).json()["project_limit"]) == 20
    with client.app.state.session_factory() as db:
        actions = list(db.scalars(select(AuditEvent.action)))
        assert actions.count("budget.adjustment_requested") == 1
        assert actions.count("budget.adjustment_decided") == 1


def test_unknown_result_keeps_project_estimate_and_free_jobs_still_work(
    client, episode, monkeypatch
):
    project_id = episode["project_id"]
    set_policy(client, project_id, "2")
    original = media_pricing.quote
    priced(monkeypatch)
    job = capture_ok(client, project_id)
    provider = Provider(
        receipt=SubmissionReceipt(status="RESULT_UNKNOWN", error_code="RESULT_UNKNOWN")
    )
    assert deliver(client, job, provider) == "RESULT_UNKNOWN"
    assert capture(client, project_id).status_code == 409
    assert Decimal(client.get(budget(project_id)).json()["pending_estimate"]) == 2
    monkeypatch.setattr(media_pricing, "quote", original)
    assert capture(client, project_id).status_code == 201


def test_candidate_limit_includes_requested_candidates(client, episode):
    project_id = episode["project_id"]
    set_policy(client, project_id, max_candidates_per_target=1)
    body = payload()
    body["spec"]["batch_size"] = 2
    assert capture(client, project_id, body).json()["error"]["code"] == "BUDGET_CANDIDATE_LIMIT"


def test_postgres_parallel_creation_cannot_exceed_project_limit(
    client, episode, settings, monkeypatch
):
    require_postgres(settings)
    project_id = UUID(episode["project_id"])
    set_policy(client, project_id, "3")
    priced(monkeypatch)
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
                    GenerationCreate.model_validate(payload()),
                )
            return "CREATED"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create_one, range(2)))
    assert sorted(results) == ["BUDGET_EXCEEDED", "CREATED"]


def test_quote_history_and_protective_downgrade(client, episode, migrated):
    project_id = episode["project_id"]
    job = capture_ok(client, project_id)
    with client.app.state.session_factory() as db:
        row = db.scalar(
            select(GenerationBudgetQuote).where(GenerationBudgetQuote.job_id == UUID(job["id"]))
        )
        quote_id = row.id
    for sql in [
        "UPDATE generation_budget_quotes SET maximum_cost=100 WHERE id=:id",
        "DELETE FROM generation_budget_quotes WHERE id=:id",
    ]:
        with (
            client.app.state.session_factory.begin() as db,
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            db.begin_nested(),
        ):
            identity = quote_id.hex if db.get_bind().dialect.name == "sqlite" else quote_id
            db.execute(text(sql), {"id": identity})
    with pytest.raises(RuntimeError, match="Generation history exists"):
        command.downgrade(migrated, "0036")


def test_budget_permission_project_isolation_and_read_only_members(client):
    owner = register(client)
    project_id = create_project(client)["id"]
    owner_token = owner["access_token"]
    editor = register(client, "budget-editor@example.com")
    viewer = register(client, "budget-viewer@example.com")
    client.headers["Authorization"] = "Bearer " + owner_token
    for member, role in ((editor, "editor"), (viewer, "viewer")):
        response = client.post(
            f"/api/v1/projects/{project_id}/members",
            json={
                "email": member["user"]["email"],
                "role": role,
            },
        )
        assert response.status_code == 201, response.text
    client.headers["Authorization"] = "Bearer " + editor["access_token"]
    assert client.get(budget(project_id)).status_code == 200
    assert (
        client.patch(
            budget(project_id),
            json={
                "expected_revision": 0,
                "project_limit": "10",
            },
        ).status_code
        == 403
    )
    body = {"creation_key": str(uuid4()), "requested_limit": "10", "reason": "申请补充预算"}
    request = client.post(budget(project_id) + "/adjustments", json=body)
    assert request.status_code == 201
    decision = budget(project_id) + f"/adjustments/{request.json()['id']}/decision"
    assert (
        client.post(
            decision,
            json={
                "expected_revision": 0,
                "approved": True,
                "reason": "自我批准",
            },
        ).status_code
        == 403
    )
    client.headers["Authorization"] = "Bearer " + viewer["access_token"]
    assert client.get(budget(project_id)).status_code == 200
    assert client.post(budget(project_id) + "/adjustments", json=body).status_code == 403
    assert client.post(budget(project_id) + "/estimate", json=payload()).status_code == 403
    stranger = register(client, "budget-stranger@example.com")
    assert stranger["user"]["id"] != owner["user"]["id"]
    assert client.get(budget(project_id)).status_code == 404


def test_budget_archive_preserves_reading_and_rejects_adjustment(client, episode):
    project_id = episode["project_id"]
    job = capture_ok(client, project_id)
    assert (
        client.patch(f"/api/v1/projects/{project_id}", json={"archived": True}).status_code == 200
    )
    assert client.get(budget(project_id)).status_code == 200
    assert client.get(base(project_id) + f"/{job['id']}").status_code == 200
    body = {"creation_key": str(uuid4()), "requested_limit": "10", "reason": "申请预算"}
    assert client.post(budget(project_id) + "/adjustments", json=body).status_code == 409


def test_budget_all_immutable_facts_reject_overwrite_and_truncate(client, episode):
    project_id = episode["project_id"]
    capture_ok(client, project_id)
    batch = client.post(
        budget(project_id) + "/batches",
        json={
            "creation_key": str(uuid4()),
            "budget_limit": "1",
        },
    )
    assert batch.status_code == 201
    request = client.post(
        budget(project_id) + "/adjustments",
        json={
            "creation_key": str(uuid4()),
            "requested_limit": "5",
            "reason": "申请预算",
        },
    ).json()
    decision = client.post(
        budget(project_id) + f"/adjustments/{request['id']}/decision",
        json={
            "expected_revision": 0,
            "approved": False,
            "reason": "无需调整",
        },
    )
    assert decision.status_code == 200
    for table in (
        "generation_budget_quotes",
        "generation_budget_batches",
        "generation_budget_adjustments",
        "generation_budget_decisions",
    ):
        with client.app.state.session_factory.begin() as db:
            sql = (
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"
                if db.get_bind().dialect.name == "sqlite"
                else f"TRUNCATE {table} CASCADE"
            )
            with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), db.begin_nested():
                db.execute(text(sql))


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "0.0000001", "10000000000"])
def test_money_rejects_invalid_precision_ranges_and_nonfinite(client, episode, value):
    project_id = episode["project_id"]
    result = client.patch(
        budget(project_id),
        json={
            "expected_revision": 0,
            "project_limit": "10",
            "task_limit": value,
        },
    )
    assert result.status_code == 422


def test_batch_episode_must_belong_to_exact_project(client, episode):
    project_id = create_project(client)["id"]
    result = client.post(
        budget(project_id) + "/batches",
        json={
            "creation_key": str(uuid4()),
            "budget_limit": "1",
            "episode_id": episode["id"],
        },
    )
    assert result.status_code == 404


def test_recorded_cost_blocks_new_paid_jobs_but_not_prior_results(client, episode, monkeypatch):
    from arclight.application.generation_attempts import finish_attempt, start_attempt
    from arclight.domain.generation import GenerationStatus
    from arclight.domain.generation_requests import GenerationResult

    project_id = episode["project_id"]
    set_policy(client, project_id, "3")
    priced(monkeypatch)
    job = capture_ok(client, project_id)
    with client.app.state.session_factory.begin() as db:
        attempt = start_attempt(db, UUID(job["id"]), job["revision"])
        finish_attempt(
            db,
            UUID(job["id"]),
            attempt.id,
            GenerationStatus.SUCCEEDED,
            result=GenerationResult(output_handles=["fake/image/1"]),
            cost=Decimal("2"),
        )
    assert Decimal(client.get(budget(project_id)).json()["actual_cost"]) == 2
    assert capture(client, project_id).json()["error"]["code"] == "BUDGET_EXCEEDED"
    assert client.get(base(project_id) + f"/{job['id']}/attempts").status_code == 200


def test_admission_refreshes_previously_cached_project_limit(client, episode):
    from arclight.application.generation_budget import enforce_budget

    project_id = UUID(episode["project_id"])
    set_policy(client, project_id, "10")
    with client.app.state.session_factory.begin() as cached:
        cached_project = cached.get(Project, project_id)
        assert cached_project.budget_limit == 10
        with client.app.state.session_factory.begin() as other:
            other.get(Project, project_id).budget_limit = Decimal("1")
        with pytest.raises(DomainError, match="项目预算已达到上限"):
            enforce_budget(
                cached, project_id, GenerationCreate.model_validate(payload()), Decimal("2")
            )


def test_retry_checks_cumulative_task_cost_before_next_attempt(client, episode, monkeypatch):
    from arclight.application.generation_attempts import (
        finish_attempt,
        request_retry,
        start_attempt,
    )
    from arclight.domain.generation import GenerationStatus

    project_id = episode["project_id"]
    set_policy(client, project_id, task_limit="2")
    priced(monkeypatch)
    job = capture_ok(client, project_id)
    job_id = UUID(job["id"])
    with client.app.state.session_factory.begin() as db:
        attempt = start_attempt(db, job_id, job["revision"])
        finish_attempt(
            db,
            job_id,
            attempt.id,
            GenerationStatus.FAILED,
            error_code="PROVIDER_FAILED",
            cost=Decimal("1"),
        )
        row = db.get(GenerationJob, job_id)
        with pytest.raises(DomainError) as exc:
            request_retry(db, job_id, row.revision, "service-owner")
        assert exc.value.code == "BUDGET_TASK_EXCEEDED"
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 1
        assert row.status == "FAILED"
