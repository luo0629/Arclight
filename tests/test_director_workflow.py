from uuid import UUID, uuid4

import pytest
from arclight.application.runtime import execute_run
from arclight.persistence.models import OutboxEvent, Script, Storyboard, WorkflowRun
from arclight.providers.text import MockTextProvider
from sqlalchemy import func, select


def script(client, prefix, version=0, approve=True):
    content = MockTextProvider().script("人工撰写的星星故事", {}).model_dump()
    created = client.post(
        prefix + "/scripts", json={"expected_version_no": version, "content": content}
    )
    assert created.status_code == 201, created.text
    result = created.json()
    if approve:
        assert (
            client.post(
                prefix + f"/scripts/{result['id']}/review", json={"decision": "APPROVE"}
            ).status_code
            == 200
        )
    return result


def advance(client, settings, run_id):
    state = client.get(f"/api/v1/runs/{run_id}").json()
    execute_run(client.app.state.session_factory, settings, UUID(run_id), state["revision"])
    return client.get(f"/api/v1/runs/{run_id}").json()


@pytest.mark.parametrize("decision,expected", [("APPROVE", "SUCCEEDED"), ("REJECT", "REJECTED")])
def test_director_uses_exact_script_without_screenwriter(
    client, episode, settings, monkeypatch, decision, expected
):
    prefix = f"/api/v1/episodes/{episode['id']}"
    first = script(client, prefix)
    script(client, prefix, version=1)

    def no_screenwriter(*args, **kwargs):
        raise AssertionError("单独导演任务不得调用编剧")

    monkeypatch.setattr(MockTextProvider, "script", no_screenwriter)
    headers = {"Idempotency-Key": "director"}
    body = {"script_version_id": first["id"]}
    created = client.post(prefix + "/storyboard-runs", headers=headers, json=body)
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    assert (
        client.post(prefix + "/storyboard-runs", headers=headers, json=body).json()["run_id"]
        == run_id
    )
    assert (
        client.post(prefix + "/script-runs", headers=headers, json={"story": "冲突"}).status_code
        == 409
    )
    assert (
        client.post(
            prefix + "/storyboard-runs", headers={"Idempotency-Key": "busy"}, json=body
        ).status_code
        == 409
    )
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        assert run.workflow_type == "storyboard" and run.workflow_version == "director-1.0"
        assert run.input_json["script"] == first["content_json"]
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1
    state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW", state
    assert state["pending_review"]["resource_type"] == "storyboard"
    boards = client.get(prefix + "/storyboards").json()
    assert len(boards) == 1 and boards[0]["script_version_id"] == first["id"]
    assert (
        client.post(
            prefix + "/scripts", json={"expected_version_no": 2, "content": first["content_json"]}
        ).status_code
        == 409
    )
    reviewed = client.post(
        f"/api/v1/runs/{run_id}/resume",
        json={
            **state["pending_review"],
            "decision": decision,
            "expected_revision": state["revision"],
        },
    )
    assert reviewed.status_code == 202, reviewed.text
    assert advance(client, settings, run_id)["status"] == expected
    assert advance(client, settings, run_id)["status"] == expected
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Script)) == 2
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 1
    assert (
        client.post(prefix + "/storyboard-runs", headers=headers, json=body).json()["run_id"]
        == run_id
    )


def test_director_requires_approved_same_episode_script(client, episode):
    prefix = f"/api/v1/episodes/{episode['id']}"
    draft = script(client, prefix, approve=False)

    def create(script_id, key="validation"):
        return client.post(
            prefix + "/storyboard-runs",
            headers={"Idempotency-Key": key},
            json={"script_version_id": script_id},
        )

    assert create(draft["id"]).status_code == 409
    assert create(str(uuid4())).status_code == 404
    other = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "另一个剧集"},
    ).json()
    foreign = script(client, f"/api/v1/episodes/{other['id']}")
    assert create(foreign["id"]).status_code == 404
    assert (
        client.post(
            prefix + "/storyboard-runs", json={"script_version_id": draft["id"]}
        ).status_code
        == 422
    )
    assert (
        client.post(
            prefix + "/storyboard-runs",
            headers={"Idempotency-Key": "bad"},
            json={"script_version_id": "not-a-uuid"},
        ).status_code
        == 422
    )
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 0


def test_director_permissions_archiving_and_cross_kind_keys(client):
    from test_collaboration import create_episode, create_project, register

    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    prefix = f"/api/v1/episodes/{episode['id']}"
    approved = script(client, prefix)
    body = {"script_version_id": approved["id"]}
    headers = {"Idempotency-Key": "role-check"}

    def create():
        return client.post(prefix + "/storyboard-runs", headers=headers, json=body)

    member = register(client, "director-member@example.com")
    assert create().status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    members = f"/api/v1/projects/{project['id']}/members"
    assert (
        client.post(members, json={"email": member["user"]["email"], "role": "viewer"}).status_code
        == 201
    )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert create().status_code == 403
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    for url in [prefix, f"/api/v1/projects/{project['id']}"]:
        assert client.patch(url, json={"archived": True}).status_code == 200
        assert create().status_code == 409
        assert client.patch(url, json={"archived": False}).status_code == 200
    assert (
        client.patch(members + f"/{member['user']['id']}", json={"role": "editor"}).status_code
        == 200
    )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    created = create()
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
    # 反方向也必须拒绝跨类型复用幂等键。
    assert (
        client.post(
            prefix + "/script-runs",
            headers={"Idempotency-Key": "story-first"},
            json={"story": "原故事"},
        ).status_code
        == 202
    )
    assert (
        client.post(
            prefix + "/storyboard-runs", headers={"Idempotency-Key": "story-first"}, json=body
        ).status_code
        == 409
    )


def test_director_failure_retry_preserves_snapshot(client, episode, settings, monkeypatch):
    from arclight.application import runtime

    prefix = f"/api/v1/episodes/{episode['id']}"
    approved = script(client, prefix)

    class BrokenDirector(MockTextProvider):
        def storyboard(self, *args, **kwargs):
            raise RuntimeError("private provider error")

    monkeypatch.setattr(runtime, "build_provider", lambda _: BrokenDirector())
    created = client.post(
        prefix + "/storyboard-runs",
        headers={"Idempotency-Key": "retry-director"},
        json={"script_version_id": approved["id"]},
    )
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    state = advance(client, settings, run_id)
    assert state["status"] == "FAILED"
    assert "private" not in str(state)
    # 失败期间允许新人工版本，恢复仍固定旧输入。
    script(client, prefix, version=1)
    monkeypatch.setattr(runtime, "build_provider", lambda _: MockTextProvider())
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    state = advance(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW", state
    boards = client.get(prefix + "/storyboards").json()
    assert len(boards) == 1 and boards[0]["script_version_id"] == approved["id"]
