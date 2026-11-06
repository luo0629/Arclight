"""Actual professional workflow and persistent Runtime; only HTTP is deterministic."""

import copy
import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_agent_model import call_response
from test_agent_model import model_service as model_service
from test_agent_runtime import final, tool_feedback
from test_screenwriter_planning import plan_value

from arclight.agents.contracts import ScriptContent
from arclight.application.runtime import execute_run
from arclight.domain.model_gateway import GatewayPolicy
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import Script, StoryOutline, WorkflowRun
from arclight.providers.text import MockTextProvider


def start(client, episode, *, season=False, key=None, **patch):
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/{'story-outline-runs' if season else 'script-runs'}",
        json={"story": "雨夜发现纸船，主角决定把信送回失主。", "professional": True, **patch},
        headers={"Idempotency-Key": key or uuid4().hex},
    )
    assert response.status_code == 202, response.text
    return UUID(response.json()["run_id"])


def run_once(client, settings, run_id):
    factory = client.app.state.session_factory
    with factory() as db:
        revision = db.get(WorkflowRun, run_id).revision
    execute_run(factory, settings, run_id, revision)
    response = client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def progress(client, run_id):
    response = client.get(f"/api/v1/runs/{run_id}/screenwriter")
    assert response.status_code == 200, response.text
    return response.json()["operations"]


def install(
    model_service, monkeypatch, settings, *, count=2, mutate=None, human=None, on_http=None
):
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost="0", pricing_version="creation-test:1"
    )
    sent, errors = [], []

    def respond(request):
        try:
            payload = json.loads(request.content)
            sent.append(payload)
            if on_http:
                on_http(payload)
            data = json.loads(payload["messages"][1]["content"])
            if "data" not in data:
                # The existing director uses its original text contract.
                data = data.get("task_input", data)
                script = ScriptContent.model_validate(data["script"])
                board = MockTextProvider().storyboard(script, data["target_duration_ms"])
                value = {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": board.model_dump_json()},
                        }
                    ]
                }
            else:
                seed = data["data"]
                answered = any(
                    m["role"] == "user" and "human_decision" in str(m.get("content"))
                    for m in payload["messages"]
                )
                if seed["phase"] == "PLANNING_ONLY":
                    if not tool_feedback(payload):
                        value = call_response(
                            ("research", "investigate_story", {"query": {"path": "startup"}})
                        )
                    elif human == "plan" and not answered:
                        value = final("HUMAN_REVIEW")
                    else:
                        result = plan_value(payload)
                        if result["plan"]["mode"] == "episode":
                            base = result["plan"]["scenes"][0]
                            duration = result["plan"]["estimated_duration_ms"]
                            result["plan"]["scenes"] = [
                                {
                                    **base,
                                    "scene_id": str(uuid4()),
                                    "title": f"河边{i}",
                                    "estimated_duration_ms": duration // count
                                    + (i <= duration % count),
                                }
                                for i in range(1, count + 1)
                            ]
                        if mutate:
                            mutate("plan", result, seed)
                        value = final(result=result)
                elif human == seed["scene_no"] and not answered:
                    value = final("HUMAN_REVIEW")
                else:
                    scene = seed["plan"]["plan"]["scenes"][seed["scene_no"] - 1]
                    result = {
                        "schema_version": "screenwriter-fragment/1",
                        "plan_loop_id": seed["plan_loop_id"],
                        "scene": {
                            "scene_id": scene["scene_id"],
                            "scene_no": seed["scene_no"],
                            "title": scene["title"],
                            "action": "主角沿河岸寻找纸船的失主。",
                            "dialogue": [
                                {"line_id": str(uuid4()), "speaker": "主角", "text": "把信送回去。"}
                            ],
                        },
                        "characters": ["主角"],
                        "character_assets": [],
                        "estimated_duration_ms": scene["estimated_duration_ms"],
                        "evidence": seed["plan"]["evidence"],
                    }
                    if mutate:
                        mutate("scene", result, seed)
                    value = final(result=result)
            value["usage"] = {"prompt_tokens": 20, "completion_tokens": 10, "actual_cost": "0"}
            return httpx.Response(200, json=value)
        except Exception as exc:
            errors.append(repr(exc))
            raise

    model_service(respond)
    monkeypatch.setattr(
        "arclight.providers.text.PublicHTTPTransport", lambda *a, **k: httpx.MockTransport(respond)
    )
    return sent, errors


def approve(client, run):
    response = client.post(
        f"/api/v1/runs/{run['id']}/resume",
        json={
            **run["pending_review"],
            "decision": "APPROVE",
            "comment": "核验完整候选",
            "expected_revision": run["revision"],
        },
    )
    assert response.status_code == 202, response.text


def test_complete_segmented_candidate_and_original_two_reviews(
    client, episode, settings, model_service, monkeypatch
):
    sent, errors = install(model_service, monkeypatch, settings)
    run_id = start(client, episode)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors)
    assert run["pending_review"]["resource_type"] == "script" and len(sent) == 4
    operations = progress(client, run_id)
    assert [item["kind"] for item in operations] == ["plan", "scene", "scene"]
    assert all(item["status"] == "COMPLETED" for item in operations)
    factory = client.app.state.session_factory
    with factory() as db:
        script = db.scalar(select(Script).where(Script.run_id == run_id))
        content = ScriptContent.model_validate(script.content_json)
        assert [s.scene_id for s in content.scenes] == [
            s["scene_id"] for s in operations[0]["result"]["plan"]["scenes"]
        ]
        assert content.estimated_duration_ms == 30000
        assert script.status == "READY_FOR_REVIEW"
    approve(client, run)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors)
    assert run["pending_review"]["resource_type"] == "storyboard" and len(sent) == 5
    approve(client, run)
    run = run_once(client, settings, run_id)
    assert run["status"] == "SUCCEEDED", (run, errors)
    assert len(sent) == 5
    assert len(progress(client, run_id)) == 3


def test_season_uses_original_candidate_confirmation(
    client, episode, settings, model_service, monkeypatch
):
    sent, errors = install(model_service, monkeypatch, settings)
    run_id = start(client, episode, season=True, target_episode_count=3, episode_duration_ms=30000)
    run = run_once(client, settings, run_id)
    assert run["status"] == "SUCCEEDED", (run, errors)
    assert len(sent) == 2
    with client.app.state.session_factory() as db:
        candidate = db.scalar(select(StoryOutline).where(StoryOutline.run_id == run_id))
        candidate_id = str(candidate.id)
        assert len(candidate.content_json["episodes"]) == 3 and candidate.status != "APPROVED"
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/story-outlines/{candidate_id}/confirm"
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "APPROVED"


@pytest.mark.parametrize("human", ["plan", 2])
def test_human_answers_are_queued_and_reenter_saved_loop(
    client, episode, settings, model_service, monkeypatch, human
):
    sent, errors = install(model_service, monkeypatch, settings, human=human)
    run_id = start(client, episode)
    run = run_once(client, settings, run_id)
    assert run["status"] == "RUNNING", (run, errors)
    waiting = next(o for o in progress(client, run_id) if o["status"] == "WAITING_HUMAN")
    count = len(sent)
    body = {
        "question_id": waiting["question_id"],
        "input_sha256": waiting["input_sha256"],
        "decision": "ANSWER",
        "answer": "补充",
    }
    endpoint = f"/api/v1/runs/{run_id}/screenwriter/{waiting['loop_id']}/answers"
    response = client.post(endpoint, json=body)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "QUEUED" and len(sent) == count
    assert client.post(endpoint, json=body).status_code == 202
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors)
    assert len(progress(client, run_id)) == 3
    assert client.post(endpoint, json=body).status_code == 202


def test_failed_scene_retry_preserves_successful_fragments_and_limits(
    client, episode, settings, model_service, monkeypatch
):
    failed = [False]

    def mutate(kind, result, seed):
        if kind == "scene" and seed["scene_no"] == 2 and not failed[0]:
            failed[0] = True
            result["scene"]["scene_id"] = str(uuid4())

    sent, errors = install(model_service, monkeypatch, settings, mutate=mutate)
    run_id = start(client, episode)
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED", (run, errors)
    assert run["error_code"] == "SCREENWRITER_FRAGMENT_SCOPE"
    original = copy.deepcopy(progress(client, run_id))
    with client.app.state.session_factory() as db:
        deadline = db.get(AgentRunLimit, run_id).deadline_at
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run_id)) == 0
        )
    response = client.post(f"/api/v1/runs/{run_id}/retry")
    assert response.status_code == 202, response.text
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors)
    assert len(sent) == 5
    assert progress(client, run_id)[:2] == original[:2]
    with client.app.state.session_factory() as db:
        assert db.get(AgentRunLimit, run_id).deadline_at == deadline
        assert (
            db.scalar(select(func.count()).select_from(AgentLoop).where(AgentLoop.run_id == run_id))
            == 4
        )


@pytest.mark.parametrize(
    "case,code",
    [
        ("scene_id", "SCREENWRITER_FRAGMENT_SCOPE"),
        ("scene_no", "SCREENWRITER_FRAGMENT_SCOPE"),
        ("plan_id", "SCREENWRITER_FRAGMENT_SCOPE"),
        ("duration", "SCREENWRITER_FRAGMENT_SCOPE"),
        ("evidence", "SCREENWRITER_FRAGMENT_EVIDENCE"),
        ("asset", "INVALID_ASSET_REFERENCE"),
        ("duplicate_line", "SCREENWRITER_ASSEMBLY_INVALID"),
        ("speaker", "GATEWAY_INVALID_RESPONSE"),
        ("missing_scene_id", "SCREENWRITER_FRAGMENT_SCOPE"),
    ],
)
def test_invalid_fragments_never_publish_partial_candidate(
    client, episode, settings, model_service, monkeypatch, case, code
):
    line_id = str(uuid4())

    def mutate(kind, result, seed):
        if kind != "scene":
            return
        if case == "duplicate_line":
            result["scene"]["dialogue"][0]["line_id"] = line_id
        elif seed["scene_no"] == 2:
            if case == "scene_id":
                result["scene"]["scene_id"] = str(uuid4())
            elif case == "scene_no":
                result["scene"]["scene_no"] = 1
            elif case == "plan_id":
                result["plan_loop_id"] = str(uuid4())
            elif case == "duration":
                result["estimated_duration_ms"] += 1
            elif case == "evidence":
                result["evidence"][0]["manifest_id"] = str(uuid4())
            elif case == "asset":
                result["scene"]["props"] = [
                    {"asset_id": str(uuid4()), "asset_version_id": str(uuid4())}
                ]
            elif case == "speaker":
                result["scene"]["dialogue"][0]["speaker"] = "未定义角色"
            elif case == "missing_scene_id":
                result["scene"].pop("scene_id")

    _, errors = install(model_service, monkeypatch, settings, mutate=mutate)
    run_id = start(client, episode)
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED", (run["error_code"], errors)
    assert run["error_code"] == code, run["error_code"]
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run_id)) == 0
        )
    assert progress(client, run_id)[1]["status"] == "COMPLETED"


@pytest.mark.parametrize(
    "season,field",
    [
        (True, "target_episode_count"),
        (True, "episode_duration_ms"),
        (False, "estimated_duration_ms"),
    ],
)
def test_plan_constraint_change_is_rejected_before_creating(
    client, episode, settings, model_service, monkeypatch, season, field
):
    def mutate(kind, result, seed):
        if kind == "plan":
            target = result["plan"].get("outline", result["plan"])
            target[field] += 1

    sent, _ = install(model_service, monkeypatch, settings, mutate=mutate)
    run_id = start(client, episode, season=season)
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and len(sent) == 2
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run_id)) == 0
        )
        assert (
            db.scalar(
                select(func.count()).select_from(StoryOutline).where(StoryOutline.run_id == run_id)
            )
            == 0
        )


def test_original_model_limit_applies_across_all_scene_loops_and_retry(
    client, episode, settings, model_service, monkeypatch
):
    sent, _ = install(model_service, monkeypatch, settings)
    run_id = start(client, episode, agent_limits={"max_model_calls": 3})
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "AGENT_MODEL_CALL_LIMIT"
    assert len(sent) == 3 and progress(client, run_id)[1]["status"] == "COMPLETED"
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    run = run_once(client, settings, run_id)
    assert run["error_code"] == "AGENT_MODEL_CALL_LIMIT" and len(sent) == 3


def test_director_after_approval_shares_original_call_limit_and_keeps_candidate(
    client, episode, settings, model_service, monkeypatch
):
    sent, errors = install(model_service, monkeypatch, settings)
    run_id = start(client, episode, agent_limits={"max_model_calls": 4})
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW" and len(sent) == 4, errors
    candidate_id = UUID(run["pending_review"]["version_id"])
    with client.app.state.session_factory() as db:
        content = copy.deepcopy(db.get(Script, candidate_id).content_json)
        deadline = db.get(AgentRunLimit, run_id).deadline_at
    approve(client, run)
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "AGENT_MODEL_CALL_LIMIT"
    assert len(sent) == 4
    with client.app.state.session_factory() as db:
        saved = db.get(WorkflowRun, run_id)
        assert saved.agent_state_json["status"] == "FAILED"
        assert (
            next(
                step for step in saved.agent_state_json["steps"] if step["step_id"] == "storyboard"
            )["status"]
            == "FAILED"
        )
        candidate = db.get(Script, candidate_id)
        assert candidate.content_json == content and candidate.status == "APPROVED"
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "AGENT_MODEL_CALL_LIMIT"
    assert len(sent) == 4
    with client.app.state.session_factory() as db:
        assert db.get(AgentRunLimit, run_id).deadline_at == deadline
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run_id)) == 1
        )


def test_changed_registered_creation_prompt_stops_before_any_new_model_call(
    client, episode, settings, model_service, monkeypatch
):
    from arclight.application import production_versions as versions

    sent, errors = install(model_service, monkeypatch, settings)
    run_id = start(client, episode)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW" and len(sent) == 4, errors
    candidate_id = UUID(run["pending_review"]["version_id"])
    approve(client, run)
    monkeypatch.setattr(versions, "FRAGMENT_PROMPT", versions.FRAGMENT_PROMPT + " changed")
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "PRODUCTION_VERSION_INCOMPATIBLE"
    assert len(sent) == 4
    with client.app.state.session_factory() as db:
        assert db.get(Script, candidate_id).status == "APPROVED"


def test_explicit_planning_retry_preserves_failed_attempt(
    client, episode, settings, model_service, monkeypatch
):
    failed = [False]

    def mutate(kind, result, seed):
        if kind == "plan" and not failed[0]:
            failed[0] = True
            result["evidence"][0]["manifest_id"] = str(uuid4())

    sent, errors = install(model_service, monkeypatch, settings, mutate=mutate)
    run_id = start(client, episode)
    run = run_once(client, settings, run_id)
    assert run["error_code"] == "SCREENWRITER_PLAN_EVIDENCE"
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run["error_code"], errors)
    operations = progress(client, run_id)
    assert operations[0]["status"] == "STOPPED" and operations[1]["status"] == "COMPLETED"
    assert len(sent) == 6


def test_human_stop_never_approves_or_publishes(
    client, episode, settings, model_service, monkeypatch
):
    sent, errors = install(model_service, monkeypatch, settings, human=2)
    run_id = start(client, episode)
    run_once(client, settings, run_id)
    waiting = progress(client, run_id)[-1]
    response = client.post(
        f"/api/v1/runs/{run_id}/screenwriter/{waiting['loop_id']}/answers",
        json={
            "question_id": waiting["question_id"],
            "input_sha256": waiting["input_sha256"],
            "decision": "STOP",
        },
    )
    assert response.status_code == 202, response.text
    run = run_once(client, settings, run_id)
    assert run["status"] == "CANCELLED" and len(sent) == 4, errors
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run_id)) == 0
        )


def test_default_request_fingerprints_and_explicit_options(client, episode, settings):
    path = f"/api/v1/episodes/{episode['id']}/script-runs"
    headers = {"Idempotency-Key": "legacy-defaults"}
    legacy = client.post(path, json={"story": "原稿"}, headers=headers)
    assert legacy.status_code == 202
    explicit = client.post(path, json={"story": "原稿", "professional": False}, headers=headers)
    assert explicit.json()["run_id"] == legacy.json()["run_id"]
    changed = client.post(path, json={"story": "原稿", "professional": True}, headers=headers)
    assert changed.status_code == 409
    assert (
        client.post(path, json={"story": "原稿", "agent_limits": {}}, headers=headers).status_code
        == 422
    )


def test_completed_work_reenters_after_crash_before_candidate_save(
    client, episode, settings, model_service, monkeypatch
):
    import arclight.application.screenwriter_execution as execution

    sent, errors = install(model_service, monkeypatch, settings)
    run_id = start(client, episode)
    original = execution.assemble_script

    class WorkerDied(BaseException):
        pass

    def die(*args):
        raise WorkerDied()

    monkeypatch.setattr(execution, "assemble_script", die)
    with pytest.raises(WorkerDied):
        run_once(client, settings, run_id)
    saved = copy.deepcopy(progress(client, run_id))
    monkeypatch.setattr(execution, "assemble_script", original)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW" and len(sent) == 4, errors
    assert progress(client, run_id) == saved


def test_crash_after_candidate_publication_keeps_single_version(
    client, episode, settings, model_service, monkeypatch
):
    import arclight.application.screenwriter_execution as execution

    sent, errors = install(model_service, monkeypatch, settings)
    run_id = start(client, episode)
    original = execution._review_graph

    class WorkerDied(BaseException):
        pass

    def die(*args):
        raise WorkerDied()

    monkeypatch.setattr(execution, "_review_graph", die)
    with pytest.raises(WorkerDied):
        run_once(client, settings, run_id)
    monkeypatch.setattr(execution, "_review_graph", original)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW" and len(sent) == 4, errors
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run_id)) == 1
        )
