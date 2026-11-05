from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from arclight.application.runtime import execute_run
from arclight.main import create_app
from arclight.persistence.models import OutboxEvent, Review, Script, Shot, Storyboard, WorkflowRun


def start(client, episode, key="first", story="雨夜，主角在街角发现一封信。"):
    return client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        headers={"Idempotency-Key": key},
        json={"story": story},
    )


def advance(client, settings, run_id):
    state = client.get(f"/api/v1/runs/{run_id}").json()
    execute_run(client.app.state.session_factory, settings, UUID(run_id), state["revision"])
    return client.get(f"/api/v1/runs/{run_id}").json()


def review(client, state, decision="APPROVE"):
    return client.post(
        f"/api/v1/runs/{state['id']}/resume",
        json={
            **state["pending_review"],
            "decision": decision,
            "expected_revision": state["revision"],
        },
    )


def test_health_auth_errors_and_validation(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    denied = client.get("/api/v1/projects", headers={"Authorization": "Bearer invalid"})
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "UNAUTHORIZED"
    response = client.post("/api/v1/projects", json={"name": "  "})
    assert response.status_code == 422
    assert "input" not in str(response.json())
    assert response.headers["x-request-id"]
    assert client.get("/api/v1/projects?limit=101").status_code == 422


def test_idempotency_and_duplicate_episode(client, episode):
    first = start(client, episode)
    assert first.status_code == 202, first.text
    second = start(client, episode)
    assert first.json()["run_id"] == second.json()["run_id"]
    assert start(client, episode, story="另一个故事").status_code == 409
    assert start(client, episode, key="second").status_code == 409
    duplicate = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={
            "episode_no": 1,
            "title": "重复",
        },
    )
    assert duplicate.status_code == 409
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1


def test_uniform_run_contract_freezes_inputs_reports_versions_usage_and_results(
    client, episode, settings
):
    story = "私密运行输入-8fca0b44"
    run_id = start(client, episode, key="contract-fixture", story=story).json()["run_id"]
    initial = client.get(f"/api/v1/runs/{run_id}").json()
    contract = initial["contract"]
    assert contract["schema_version"] == 1
    assert contract["run_id"] == run_id
    assert contract["project_id"] == episode["project_id"]
    assert contract["episode_id"] == episode["id"]
    assert contract["requested_by"]
    assert contract["task_type"] == "script_storyboard"
    created_at = datetime.fromisoformat(initial["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    assert datetime.fromisoformat(contract["requested_at"]) == created_at
    assert contract["started_at"] is None and contract["finished_at"] is None
    assert contract["retry_count"] == 0
    assert contract["input_snapshot"]["sha256"]
    assert contract["versions"]["state"] == "BOUND"
    assert contract["versions"]["model"]["provider"] == "mock"
    assert contract["versions"]["parameter_sha256"]
    assert {item["family"] for item in contract["versions"]["components"]} == {"TEXT"}
    assert contract["budget"]["scope"] == "project"
    assert contract["usage"]["calls"] == []
    assert contract["result"] is None
    assert story not in str(initial)
    assert all(
        value not in str(initial)
        for value in ("api_key", "encrypted_key", "input_json", "resume_json")
    )

    from arclight.persistence.models import Project, WorkflowRun

    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        frozen_agent_input = run.agent_state_json["input_sha256"]
        project = db.get(Project, UUID(episode["project_id"]))
        project.style_guide = {"changed_after_start": True}
    unchanged = client.get(f"/api/v1/runs/{run_id}").json()
    assert unchanged["contract"]["input_snapshot"] == contract["input_snapshot"]
    with client.app.state.session_factory() as db:
        saved_agent_input = db.get(WorkflowRun, UUID(run_id)).agent_state_json["input_sha256"]
        assert saved_agent_input == frozen_agent_input

    pending = advance(client, settings, run_id)
    contract = pending["contract"]
    result = contract["result"]
    assert contract["started_at"] is not None and contract["finished_at"] is None
    assert pending["status"] == "WAITING_REVIEW"
    assert result["result_id"]
    assert result["status"] == "WAITING_REVIEW"
    assert result["requires_human_review"] is True
    assert result["artifact_ids"] == [pending["pending_review"]["version_id"]]
    assert contract["usage"]["calls"]
    assert {call["status"] for call in contract["usage"]["calls"]} == {"SUCCEEDED"}
    assert "actual_cost" in contract["usage"]["calls"][0]["usage"]
    assert story not in str(pending)

    assert review(client, pending).status_code == 202
    storyboard_review = advance(client, settings, run_id)
    assert storyboard_review["status"] == "WAITING_REVIEW"
    assert review(client, storyboard_review).status_code == 202
    completed = advance(client, settings, run_id)
    contract = completed["contract"]
    assert completed["status"] == "SUCCEEDED"
    assert contract["finished_at"] is not None
    assert contract["result"]["status"] == "SUCCEEDED"
    assert contract["result"]["requires_human_review"] is False
    assert len(contract["result"]["artifact_ids"]) == 2
    listed = client.get(f"/api/v1/episodes/{episode['id']}/runs").json()
    assert listed[0]["contract"] == contract


def test_agent_state_persists_with_runtime_and_remains_private(client, episode, settings):
    from arclight.persistence.models import WorkflowRun

    story = "agent-state-secret-input"
    run_id = start(client, episode, key="agent-state", story=story).json()["run_id"]
    initial = client.get(f"/api/v1/runs/{run_id}").json()
    assert "agent_state" not in initial and "agent_state_json" not in initial

    pending = advance(client, settings, run_id)
    assert pending["status"] == "WAITING_REVIEW"
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        state = run.agent_state_json
        original_inputs = dict(run.input_json)
    assert state["schema_version"] == 1
    assert state["status"] == "WAITING_REVIEW"
    assert state["steps"][0]["status"] == "SUCCEEDED"
    assert state["steps"][1]["status"] == "WAITING_REVIEW"
    assert state["working_drafts"][0]["artifact_id"] == pending["pending_review"]["version_id"]
    assert story not in str(state)

    from fastapi.testclient import TestClient

    from arclight.main import create_app

    with TestClient(create_app(settings)) as restarted:
        restarted.headers["Authorization"] = "Bearer test-token"
        resumed = review(restarted, pending)
        assert resumed.status_code == 202, resumed.text
        advanced = advance(restarted, settings, run_id)
        assert advanced["status"] == "WAITING_REVIEW"

    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        assert run.agent_state_json["steps"][1]["status"] == "SUCCEEDED"
        assert run.agent_state_json["steps"][2]["status"] == "SUCCEEDED"
        assert run.agent_state_json["steps"][3]["status"] == "WAITING_REVIEW"
        assert run.input_json == original_inputs


def test_agent_state_migration_preserves_legacy_run_and_is_protected_on_downgrade(
    settings, migrated, episode
):
    from alembic import command
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, inspect, text

    from arclight.application.workflow_contracts import serialize_run

    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer test-token"
        run_id = start(client, episode, key="legacy-state-migration").json()["run_id"]
        with client.app.state.session_factory.begin() as db:
            run = db.get(WorkflowRun, UUID(run_id))
            run.agent_state_json = None
        with client.app.state.session_factory() as db:
            run = db.get(WorkflowRun, UUID(run_id))
            assert serialize_run(db, run)["status"] == "QUEUED"

        command.downgrade(migrated, "0042")
        engine = create_engine(settings.database_url)
        try:
            assert "agent_state_json" not in {
                item["name"] for item in inspect(engine).get_columns("workflow_runs")
            }
        finally:
            engine.dispose()
        engine = create_engine(settings.database_url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM workflow_runs")) == 1
        finally:
            engine.dispose()
        command.upgrade(migrated, "head")
        engine = create_engine(settings.database_url)
        try:
            assert "agent_state_json" in {
                item["name"] for item in inspect(engine).get_columns("workflow_runs")
            }
        finally:
            engine.dispose()
        response = client.get(f"/api/v1/runs/{run_id}")
        assert response.status_code == 200
        assert response.json()["status"] == "QUEUED"
        with client.app.state.session_factory() as db:
            assert db.get(WorkflowRun, UUID(run_id)).agent_state_json is None
        with client.app.state.session_factory.begin() as db:
            run = db.get(WorkflowRun, UUID(run_id))
            run.agent_state_json = {
                "schema_version": 1,
                "run_id": str(run.id),
                "project_id": episode["project_id"],
                "workflow_type": run.workflow_type,
                "workflow_version": run.workflow_version,
                "input_sha256": "a" * 64,
                "revision": 0,
                "status": "RUNNING",
                "plan": [{"step_id": "script", "operation": "DRAFT_SCRIPT"}],
                "steps": [{"step_id": "script", "status": "PENDING"}],
                "tool_call_refs": [],
                "observation_refs": [],
                "working_drafts": [],
                "termination_reason": None,
                "extensions": {},
            }

    with pytest.raises(RuntimeError, match="Agent runtime state exists"):
        command.downgrade(migrated, "0042")


def test_checkpoint_cannot_replace_frozen_input_before_resume(client, episode, settings):
    from arclight.providers.text import MockTextProvider
    from arclight.workflows.checkpoints import open_checkpointer
    from arclight.workflows.production import Artifacts, build_graph

    run_id = start(client, episode).json()["run_id"]
    pending = advance(client, settings, run_id)
    with open_checkpointer(settings) as saver:
        graph = build_graph(MockTextProvider(), Artifacts(), saver)
        graph.update_state({"configurable": {"thread_id": run_id}}, {"story": "changed input"})
    assert review(client, pending).status_code == 202
    assert advance(client, settings, run_id)["status"] == "FAILED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
        assert db.get(WorkflowRun, UUID(run_id)).input_json["story"] != "changed input"


def test_legacy_text_checkpoint_migrates_and_resumes_in_actual_runtime(client, episode, settings):
    from arclight.application.artifacts import ArtifactStore
    from arclight.application.context_sources import public_startup
    from arclight.providers.text import MockTextProvider
    from arclight.workflows.checkpoints import open_checkpointer
    from arclight.workflows.production import build_graph

    run_id = start(client, episode).json()["run_id"]
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        run.agent_state_json = None
        inputs = public_startup(run.input_json)
    store = ArtifactStore(
        client.app.state.session_factory,
        UUID(run_id),
        UUID(episode["id"]),
        inputs["target_duration_ms"],
        settings.text_duration_tolerance_percent,
    )
    with open_checkpointer(settings) as saver:
        legacy_graph = build_graph(MockTextProvider(), store, saver)
        config = {"configurable": {"thread_id": run_id}}
        legacy_graph.invoke({"run_id": run_id, **inputs}, config, durability="sync")
        assert "agent_state" not in legacy_graph.get_state(config).values
    pending = advance(client, settings, run_id)
    assert pending["status"] == "WAITING_REVIEW"
    with client.app.state.session_factory() as db:
        state = db.get(WorkflowRun, UUID(run_id)).agent_state_json
        assert state["schema_version"] == 1
        assert state["steps"][0]["status"] == "SUCCEEDED"
        assert len(state["working_drafts"]) == 1
    assert review(client, pending).status_code == 202
    assert advance(client, settings, run_id)["pending_review"]["resource_type"] == "storyboard"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Script)) == 1


def test_run_and_checkpoint_state_from_another_project_is_rejected(client, episode, settings):
    from arclight.application.production_versions import input_summary
    from arclight.domain.agent_state import AgentRunScope, initial_agent_state
    from arclight.domain.versions import digest
    from arclight.persistence.models import Episode

    project = client.post("/api/v1/projects", json={"name": "隔离项目"})
    other_episode = client.post(
        f"/api/v1/projects/{project.json()['id']}/episodes",
        json={"episode_no": 1, "title": "隔离剧集", "target_duration_ms": 30000},
    )
    assert other_episode.status_code == 201
    first_run_id = start(client, episode, key="first-isolation").json()["run_id"]
    second_run_id = start(client, other_episode.json(), key="second-isolation").json()["run_id"]
    with client.app.state.session_factory.begin() as db:
        first = db.get(WorkflowRun, UUID(first_run_id))
        second = db.get(WorkflowRun, UUID(second_run_id))
        first_state = initial_agent_state(
            AgentRunScope(
                run_id=first.id,
                project_id=db.get(Episode, first.episode_id).project_id,
                workflow_type=first.workflow_type,
                workflow_version=first.workflow_version,
                input_sha256=digest(input_summary(first.input_json)),
            )
        )
        second.agent_state_json = first_state.model_dump(mode="json")
    response = client.get(f"/api/v1/runs/{second_run_id}")
    assert response.status_code == 200
    execute_run(client.app.state.session_factory, settings, UUID(second_run_id), 0)
    with client.app.state.session_factory() as db:
        second = db.get(WorkflowRun, UUID(second_run_id))
        assert second.status == "FAILED"
        assert second.agent_state_json == first_state.model_dump(mode="json")


def test_legacy_run_without_version_binding_remains_readable(client, episode):
    from arclight.persistence.models import WorkflowRun

    with client.app.state.session_factory.begin() as db:
        run = WorkflowRun(
            episode_id=UUID(episode["id"]),
            workflow_type="story_analysis",
            workflow_version="legacy-1",
            status="SUCCEEDED",
            trace_id="legacy-trace",
            idempotency_key="legacy-unbound",
            request_fingerprint="0" * 64,
            input_json={
                "story": "legacy private input",
                "untrusted_asset_id": "private-inline-reference",
                "_text_service": {"encrypted_key": "private-encrypted-fixture"},
            },
        )
        db.add(run)
        db.flush()
        run_id = run.id

    response = client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["status"] == "SUCCEEDED"
    assert value["contract"]["versions"] == {
        "state": "LEGACY_UNBOUND",
        "workflow_version": "legacy-1",
        "components": [],
        "model": None,
        "parameter_sha256": None,
    }
    assert value["contract"]["result"] is None
    assert value["contract"]["requested_by"] is None
    assert value["contract"]["started_at"] is None
    assert value["contract"]["finished_at"] is None
    assert value["contract"]["budget"]["origin"] == "UNKNOWN"
    assert value["contract"]["budget"]["cost_estimate"] is None
    assert value["contract"]["input_snapshot"]["references"] == []
    assert "legacy private input" not in response.text
    assert "private-inline-reference" not in response.text
    assert "private-encrypted-fixture" not in response.text


def test_invalid_legacy_contract_returns_a_sanitized_error(client, episode):
    from arclight.persistence.models import WorkflowRun

    with client.app.state.session_factory.begin() as db:
        run = WorkflowRun(
            episode_id=UUID(episode["id"]),
            workflow_type="story_analysis",
            workflow_version="legacy-1",
            status="SUCCEEDED",
            trace_id="Bearer private-legacy-fixture",
            idempotency_key="legacy-invalid",
            request_fingerprint="0" * 64,
            input_json={"story": "private input from legacy record"},
        )
        db.add(run)
        db.flush()
        run_id = run.id
    response = client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "RUN_CONTRACT_INVALID"
    assert "private-legacy-fixture" not in response.text
    assert "private input" not in response.text


def test_persistent_workflow_and_version_bound_reviews(client, episode, settings):
    created = start(client, episode)
    run_id = created.json()["run_id"]
    state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW", state
    assert state["pending_review"]["resource_type"] == "script"
    # 每次 advance 都重新打开持久化 checkpointer；重启 API 不会丢失审核节点。
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as restarted:
        restarted.headers["Authorization"] = "Bearer test-token"
        assert (
            restarted.get(f"/api/v1/runs/{run_id}").json()["pending_review"]
            == state["pending_review"]
        )
        assert restarted.get(f"/api/v1/runs/{run_id}").json()["contract"] == state["contract"]
    stale = {
        **state["pending_review"],
        "version_id": str(uuid4()),
        "decision": "APPROVE",
        "expected_revision": state["revision"],
    }
    assert client.post(f"/api/v1/runs/{run_id}/resume", json=stale).status_code == 409
    approved = review(client, state)
    assert approved.status_code == 202, approved.text
    assert review(client, state).status_code == 409
    state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW", state
    assert state["pending_review"]["resource_type"] == "storyboard"
    board_id = state["pending_review"]["version_id"]
    shots = client.get(f"/api/v1/episodes/{episode['id']}/shots?storyboard_id={board_id}")
    assert len(shots.json()) == 5
    assert review(client, state).status_code == 202
    state = advance(client, settings, run_id)
    assert state["status"] == "SUCCEEDED", state
    # 重复、过期消息不创建重复产物，也不会重新调用 Provider。
    execute_run(client.app.state.session_factory, settings, UUID(run_id), 0)
    with client.app.state.session_factory() as db:
        for model, expected in [(Script, 1), (Storyboard, 1), (Shot, 5), (Review, 2)]:
            assert db.scalar(select(func.count()).select_from(model)) == expected
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    assert any(e["event_type"] == "workflow.finished" for e in events)


def test_rejection_stops_before_storyboard(client, episode, settings):
    run_id = start(client, episode).json()["run_id"]
    state = advance(client, settings, run_id)
    assert review(client, state, "REJECT").status_code == 202
    rejected = advance(client, settings, run_id)
    assert rejected["status"] == "REJECTED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
        agent_state = db.get(WorkflowRun, UUID(run_id)).agent_state_json
        assert agent_state["status"] == "REJECTED"
        assert agent_state["termination_reason"] == "REVIEW_REJECTED"


def test_cancel_queued_run_and_unknown_resource(client, episode, settings):
    run_id = start(client, episode).json()["run_id"]
    cancelled = client.post(f"/api/v1/runs/{run_id}/cancel").json()
    assert cancelled["status"] == "CANCELLED"
    with client.app.state.session_factory() as db:
        state = db.get(WorkflowRun, UUID(run_id)).agent_state_json
        assert state["status"] == "CANCELLED"
        assert state["termination_reason"] == "CANCELLED"
    assert cancelled["contract"]["finished_at"] is not None
    assert cancelled["contract"]["started_at"] is None
    assert advance(client, settings, run_id)["status"] == "CANCELLED"
    assert client.get(f"/api/v1/runs/{uuid4()}").status_code == 404
    assert client.get(f"/api/v1/projects/{uuid4()}").status_code == 404


def test_failure_is_sanitized_and_explicit_retry_recovers(client, episode, settings, monkeypatch):
    from arclight.application import runtime
    from arclight.providers.text import MockTextProvider

    class BrokenProvider(MockTextProvider):
        def script(
            self, story, style, duration_ms=30000, *, story_settings=None, story_context=None
        ):
            raise RuntimeError("secret-provider-key-must-not-leak")

    run_id = start(client, episode).json()["run_id"]
    monkeypatch.setattr(runtime, "build_provider", lambda _: BrokenProvider())
    failed = advance(client, settings, run_id)
    assert failed["status"] == "FAILED"
    assert "secret-provider-key" not in str(failed)
    assert failed["error_code"] == "WORKFLOW_EXECUTION_FAILED"
    assert failed["contract"]["finished_at"] is not None
    assert failed["contract"]["retry_count"] == 0
    with client.app.state.session_factory() as db:
        failed_state = db.get(WorkflowRun, UUID(run_id)).agent_state_json
        assert failed_state["status"] == "FAILED"
        assert failed_state["termination_reason"] == "EXECUTION_FAILED"
    response = client.post(f"/api/v1/runs/{run_id}/retry")
    assert response.status_code == 202
    assert response.json()["contract"]["retry_count"] == 1
    assert response.json()["contract"]["finished_at"] is None
    with client.app.state.session_factory() as db:
        retry_state = db.get(WorkflowRun, UUID(run_id)).agent_state_json
        assert retry_state["status"] == "RUNNING"
        assert retry_state["termination_reason"] is None
    monkeypatch.setattr(runtime, "build_provider", lambda _: MockTextProvider())
    recovered = advance(client, settings, run_id)
    assert recovered["status"] == "WAITING_REVIEW"
    assert recovered["contract"]["retry_count"] == 1
    assert recovered["contract"]["result"]["warnings"] == []
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 409


def test_outbox_redelivers_until_consumed(client, episode, settings, monkeypatch):
    from arclight.workers import tasks

    run_id = start(client, episode).json()["run_id"]
    deliveries = []
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks.execute_workflow, "apply_async", lambda **kw: deliveries.append(kw))
    stage_deliveries = []
    monkeypatch.setattr(
        tasks.coordinate_production, "apply_async", lambda **kw: stage_deliveries.append(kw)
    )
    tasks.dispatch_outbox.run()
    tasks.dispatch_outbox.run()
    assert len(deliveries) == 2
    assert stage_deliveries == []
    assert deliveries[0]["args"] == [run_id, 0]
    advance(client, settings, run_id)
    tasks.dispatch_outbox.run()
    assert len(deliveries) == 2
    assert len(stage_deliveries) == 1


def test_recovers_checkpoint_after_business_transaction_loss(client, episode, settings):
    from arclight.persistence.models import WorkflowRun

    run_id = start(client, episode).json()["run_id"]
    state = advance(client, settings, run_id)
    # 模拟图 checkpoint/产物已提交，而运行事务因进程退出尚未提交的窗口。
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        run.status = "QUEUED"
        run.pending_review = None
        run.revision = 0
        run.agent_state_json = None
    recovered = advance(client, settings, run_id)
    assert recovered["pending_review"] == state["pending_review"]
    assert recovered["status"] == "WAITING_REVIEW"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Script)) == 1
        recovered_state = db.get(WorkflowRun, UUID(run_id)).agent_state_json
        assert recovered_state["status"] == "WAITING_REVIEW"
        assert recovered_state["steps"][0]["status"] == "SUCCEEDED"


def test_failed_run_cannot_retry_when_another_run_is_active(client, episode):
    from arclight.persistence.models import WorkflowRun

    run_id = start(client, episode).json()["run_id"]
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, UUID(run_id)).status = "FAILED"
    assert start(client, episode, key="new-run").status_code == 202
    conflict = client.post(f"/api/v1/runs/{run_id}/retry")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "EPISODE_BUSY"


def test_postgres_concurrent_duplicate_delivery(client, episode, settings, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock

    import pytest

    from arclight.application import runtime
    from arclight.providers.text import MockTextProvider

    if not settings.database_url.startswith("postgresql"):
        pytest.skip("数据库行锁并发保障需要真实 PostgreSQL")
    count = 0
    lock = Lock()

    class CountedProvider(MockTextProvider):
        def script(
            self, story, style, duration_ms=30000, *, story_settings=None, story_context=None
        ):
            nonlocal count
            with lock:
                count += 1
            return super().script(story, style, duration_ms)

    monkeypatch.setattr(runtime, "build_provider", lambda _: CountedProvider())
    run_id = UUID(start(client, episode).json()["run_id"])
    factory = client.app.state.session_factory
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute_run, factory, settings, run_id, 0) for _ in range(2)]
        for future in futures:
            future.result(timeout=30)
    assert count == 1
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "WAITING_REVIEW"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Script)) == 1
        state = db.get(WorkflowRun, run_id).agent_state_json
        assert state["revision"] == 2
        assert state["steps"][0]["status"] == "SUCCEEDED"
        assert len(state["working_drafts"]) == 1


def test_transaction_commits_before_success_response(client):
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError

    factory = client.app.state.session_factory

    def fail_commit(_session):
        raise IntegrityError("simulated commit failure", {}, Exception("secret-database-url"))

    event.listen(factory, "before_commit", fail_commit)
    try:
        response = client.post("/api/v1/projects", json={"name": "不能假成功的项目"})
    finally:
        event.remove(factory, "before_commit", fail_commit)
    assert response.status_code == 409
    assert "secret-database-url" not in response.text
    assert client.get("/api/v1/projects").json() == []
