"""DIRECT-02 exercises actual Runtime/graphs/database; only provider HTTP is replaced."""

import copy
import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_agent_model import call_response
from test_agent_model import model_service as model_service
from test_agent_runtime import final, tool_feedback
from test_director_planning import plan_value
from test_screenwriter_creation import approve, run_once
from test_screenwriter_creation import install as install_writer

from arclight.agents.contracts import ScriptContent
from arclight.domain.model_gateway import GatewayPolicy
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.models import Review, Script, Storyboard, WorkflowRun
from arclight.providers.text import MockTextProvider


def approved_script(client, episode, *, count=3, configure=None):
    prefix = f"/api/v1/episodes/{episode['id']}"
    content = MockTextProvider().script("雨夜主角寻找纸船的失主", {}).model_dump(mode="json")
    scene = content["scenes"][0]
    scene["dialogue"] = [{"line_id": str(uuid4()), "speaker": "主角", "text": "把信送回去。"}]
    content["scenes"] = [
        {
            **copy.deepcopy(scene),
            "scene_id": str(uuid4()),
            "scene_no": n,
            "title": f"场景{n}",
            "dialogue": [{**line, "line_id": str(uuid4())} for line in scene["dialogue"]],
        }
        for n in range(1, count + 1)
    ]
    if configure:
        configure(content)
    response = client.post(prefix + "/scripts", json={"expected_version_no": 0, "content": content})
    assert response.status_code == 201, response.text
    source = response.json()
    assert (
        client.post(
            prefix + f"/scripts/{source['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 200
    )
    return source


def start(client, episode, source=None, **options):
    source = source or approved_script(client, episode)
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboard-runs",
        headers={"Idempotency-Key": uuid4().hex},
        json={"script_version_id": source["id"], "professional": True, **options},
    )
    assert response.status_code == 202, response.text
    return UUID(response.json()["run_id"]), source


def progress(client, run_id):
    response = client.get(f"/api/v1/runs/{run_id}/director")
    assert response.status_code == 200, response.text
    return response.json()


def fragment_value(seed):
    scene = seed["scene"]
    duration = seed["plan"]["scenes"][scene["scene_no"] - 1]["estimated_duration_ms"]
    source = ScriptContent(
        title="当前场景",
        summary="精确输入",
        characters=seed["characters"],
        character_assets=seed["character_assets"],
        estimated_duration_ms=duration,
        scenes=[{**scene, "scene_no": 1}],
    )
    shots = MockTextProvider().storyboard(source, duration).model_dump(mode="json")["shots"]
    for shot in shots:
        shot["scene_no"] = scene["scene_no"]
    return {
        "schema_version": "director-fragment/1",
        "plan_loop_id": seed["plan_loop_id"],
        "scene_id": scene["scene_id"],
        "scene_no": scene["scene_no"],
        "shots": shots,
        "evidence": seed["plan"]["evidence"],
    }


def install(
    model_service, monkeypatch, settings, *, human=None, mutate=None, on_http=None, revised=False
):
    # Capture the existing writer's deterministic HTTP implementation for dual runs.
    writer = []
    if revised:
        from test_screenwriter_revision import install_revision

        install_revision(writer.append, monkeypatch, settings)
    else:
        install_writer(writer.append, monkeypatch, settings)
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost="0", pricing_version="direct02:1"
    )
    sent, errors = [], []

    def respond(request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        seed = data.get("data", {})
        director_plan = "script_version_id" in seed.get("constraints", {})
        if not director_plan and seed.get("phase") != "DIRECT_SCENE_ONLY":
            return writer[0](request)
        try:
            sent.append(payload)
            if on_http:
                on_http(payload)
            answered = any(
                m["role"] == "user" and "human_decision" in str(m.get("content"))
                for m in payload["messages"]
            )
            if director_plan and not tool_feedback(payload):
                value = call_response(
                    *[
                        (f"research-{i}", "investigate_production", {"query": query})
                        for i, query in enumerate(
                            [
                                {"path": "script"},
                                {"path": "visual"},
                                {"path": "capabilities", "provider": "mock", "model": "mock-image"},
                            ]
                        )
                    ]
                )
            elif (
                human == "plan" if director_plan else human == seed["scene"]["scene_no"]
            ) and not answered:
                value = final("HUMAN_REVIEW")
            else:
                result = plan_value(payload) if director_plan else fragment_value(seed)
                if mutate:
                    mutate("plan" if director_plan else "scene", result, seed)
                value = final(result=result)
            value["usage"] = {"prompt_tokens": 20, "completion_tokens": 10, "actual_cost": "0"}
            return httpx.Response(200, json=value)
        except Exception as exc:
            errors.append(repr(exc))
            raise

    model_service(respond)
    return sent, errors


@pytest.mark.parametrize("combined", [False, True])
def test_multiscene_candidate_original_reviews_and_role_isolation(
    client, episode, settings, model_service, monkeypatch, combined
):
    sent, errors = install(model_service, monkeypatch, settings)
    prefix = f"/api/v1/episodes/{episode['id']}"
    if combined:
        response = client.post(
            prefix + "/script-runs",
            headers={"Idempotency-Key": uuid4().hex},
            json={"story": "雨夜主角寻找失主", "professional": True, "professional_director": True},
        )
        assert response.status_code == 202, response.text
        run_id = UUID(response.json()["run_id"])
        run = run_once(client, settings, run_id)
        assert run["status"] == "WAITING_REVIEW", (run, errors)
        assert run["pending_review"]["resource_type"] == "script"
        assert not progress(client, run_id)["operations"]
        approve(client, run)
        scene_count = 2
    else:
        run_id, _ = start(client, episode)
        scene_count = 3
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors)
    assert run["pending_review"]["resource_type"] == "storyboard"
    operations = progress(client, run_id)["operations"]
    assert [x["kind"] for x in operations] == ["plan"] + ["scene"] * scene_count
    assert all(x["status"] == "COMPLETED" for x in operations)
    assert len(operations[0]["observations"]) == 3
    if combined:
        writer_progress = client.get(f"/api/v1/runs/{run_id}/screenwriter").json()
        assert len(writer_progress["operations"]) == 3
        assert not {x["loop_id"] for x in writer_progress["operations"]} & {
            x["loop_id"] for x in operations
        }
    else:
        assert client.get(f"/api/v1/runs/{run_id}/screenwriter").status_code == 404
    boards = client.get(prefix + "/storyboards").json()
    assert len(boards) == 1
    board = boards[0]
    assert {x["scene_no"] for x in board["content_json"]["shots"]} == set(range(1, scene_count + 1))
    assert [x["shot_no"] for x in board["content_json"]["shots"]] == list(
        range(1, 5 * scene_count + 1)
    )
    original = digest(board["content_json"])
    approve(client, run)
    assert run_once(client, settings, run_id)["status"] == "SUCCEEDED"
    assert run_once(client, settings, run_id)["status"] == "SUCCEEDED"
    assert len(sent) == scene_count + 2 and not errors
    with client.app.state.session_factory() as db:
        saved = db.get(Storyboard, UUID(board["id"]))
        assert digest(saved.content_json) == original and saved.status == "APPROVED"
        state = db.get(WorkflowRun, run_id).agent_state_json
        assert state["extensions"]["director.planning"]["loop_id"] == operations[0]["loop_id"]
        if combined:
            assert state["extensions"]["screenwriter.planning"]
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentRunLimit)
                .where(AgentRunLimit.run_id == run_id)
            )
            == 1
        )
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 1


@pytest.mark.parametrize(
    "fault", ["line", "scene", "number", "speaker", "plan", "evidence", "duration", "asset"]
)
def test_invalid_fragment_never_publishes_partial_candidate(
    client, episode, settings, model_service, monkeypatch, fault
):
    def mutate(phase, result, seed):
        if phase != "scene" or seed["scene"]["scene_no"] != 2:
            return
        shot = result["shots"][0]
        if fault == "line":
            shot["dialogue_line_ids"] = [str(uuid4())]
        elif fault == "scene":
            shot["scene_id"] = str(uuid4())
        elif fault == "number":
            shot["shot_no"] = 2
        elif fault == "speaker":
            shot["characters"] = []
        elif fault == "plan":
            result["plan_loop_id"] = str(uuid4())
        elif fault == "evidence":
            result["evidence"][0]["content_sha256"] = "0" * 64
        elif fault == "duration":
            shot["duration_ms"] += 1000
        else:
            shot["props"] = [{"asset_id": str(uuid4()), "asset_version_id": str(uuid4())}]

    sent, errors = install(model_service, monkeypatch, settings, mutate=mutate)
    run_id, _ = start(client, episode)
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED", (run, errors)
    assert len(sent) == 4 and not errors
    assert progress(client, run_id)["operations"][1]["status"] == "COMPLETED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
        assert (
            db.scalar(select(func.count()).select_from(Review).where(Review.run_id == run_id)) == 0
        )
        assert db.scalar(select(Script.status)) == "APPROVED"
