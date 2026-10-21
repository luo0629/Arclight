from uuid import UUID, uuid4

from sqlalchemy import func, select

from arclight.application.runtime import execute_run
from arclight.main import create_app
from arclight.persistence.models import OutboxEvent, Review, Script, Shot, Storyboard


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
    assert advance(client, settings, run_id)["status"] == "REJECTED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0


def test_cancel_queued_run_and_unknown_resource(client, episode, settings):
    run_id = start(client, episode).json()["run_id"]
    assert client.post(f"/api/v1/runs/{run_id}/cancel").json()["status"] == "CANCELLED"
    assert advance(client, settings, run_id)["status"] == "CANCELLED"
    assert client.get(f"/api/v1/runs/{uuid4()}").status_code == 404
    assert client.get(f"/api/v1/projects/{uuid4()}").status_code == 404


def test_failure_is_sanitized_and_explicit_retry_recovers(client, episode, settings, monkeypatch):
    from arclight.application import runtime
    from arclight.providers.text import MockTextProvider

    class BrokenProvider(MockTextProvider):
        def script(self, story, style):
            raise RuntimeError("secret-provider-key-must-not-leak")

    run_id = start(client, episode).json()["run_id"]
    monkeypatch.setattr(runtime, "build_provider", lambda _: BrokenProvider())
    failed = advance(client, settings, run_id)
    assert failed["status"] == "FAILED"
    assert "secret-provider-key" not in str(failed)
    assert failed["error_code"] == "WORKFLOW_EXECUTION_FAILED"
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    monkeypatch.setattr(runtime, "build_provider", lambda _: MockTextProvider())
    assert advance(client, settings, run_id)["status"] == "WAITING_REVIEW"
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 409


def test_outbox_redelivers_until_consumed(client, episode, settings, monkeypatch):
    from arclight.workers import tasks

    run_id = start(client, episode).json()["run_id"]
    deliveries = []
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks.execute_workflow, "apply_async", lambda **kw: deliveries.append(kw))
    tasks.dispatch_outbox.run()
    tasks.dispatch_outbox.run()
    assert len(deliveries) == 2
    assert deliveries[0]["args"] == [run_id, 0]
    advance(client, settings, run_id)
    tasks.dispatch_outbox.run()
    assert len(deliveries) == 2


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
    recovered = advance(client, settings, run_id)
    assert recovered["pending_review"] == state["pending_review"]
    assert recovered["status"] == "WAITING_REVIEW"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Script)) == 1


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
        def script(self, story, style):
            nonlocal count
            with lock:
                count += 1
            return super().script(story, style)

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
