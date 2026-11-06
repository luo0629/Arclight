"""DIRECT-01 executes the persistent Runtime; only provider HTTP is replaced."""

import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_agent_loop_recovery import answer
from test_agent_model import call_response
from test_agent_model import model_service as model_service
from test_agent_runtime import final, tool_feedback
from test_director_workflow import script

from arclight.application import director_planning as planning
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import build_agent_tool_registry, grant_agent_tools
from arclight.application.gateway_text import execution_actor
from arclight.domain.agent_state import transition_agent_step
from arclight.domain.agent_tools import AgentToolGrantRequest
from arclight.domain.model_gateway import GatewayPolicy
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import Review, Script, Storyboard, WorkflowRun


def prepare(client, episode, settings, source=None):
    settings.model_gateway_policy = GatewayPolicy(text_maximum_cost="0", pricing_version="plan:1")
    source = source or script(client, f"/api/v1/episodes/{episode['id']}")
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboard-runs",
        headers={"Idempotency-Key": uuid4().hex},
        json={"script_version_id": source["id"]},
    )
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    actor = activate(client, episode, settings, run_id)
    return run_id, actor, source


def activate(client, episode, settings, run_id):
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        state = run.agent_state_json
        run.agent_state_json = transition_agent_step(
            state,
            scope_for_run(run, UUID(episode["project_id"])),
            expected_revision=state["revision"],
            step_id="storyboard",
            target="RUNNING",
        ).model_dump(mode="json")
        run.status = "RUNNING"
        actor = execution_actor(db, run)
        grant_agent_tools(
            db,
            AgentToolGrantRequest(
                run_id=run.id,
                agent_name="director",
                allowed_tools=["investigate_production"],
            ),
            actor,
            build_agent_tool_registry(settings=settings),
        )
    return actor


def plan_value(payload):
    task = json.loads(payload["messages"][1]["content"])["data"]["constraints"]
    evidence = [
        {key: value for key, value in material.items() if key != "content"}
        for feedback in tool_feedback(payload)
        if feedback["status"] == "SUCCEEDED"
        for material in feedback["result"]["materials"]
    ]
    count = len(task["scene_ids"])
    duration, remainder = divmod(task["target_duration_ms"], count)
    return {
        "schema_version": "director-plan/1",
        "objective": "让场景目标可见并保持已批准内容",
        "script_version_id": task["script_version_id"],
        "script_sha256": task["script_sha256"],
        "target_duration_ms": task["target_duration_ms"],
        "estimated_duration_ms": task["target_duration_ms"],
        "scenes": [
            {
                "scene_id": identity,
                "dramatic_goal": "发现矛盾后做出选择",
                "emotion": "期待转为坚定",
                "staging": ["人物从画面左侧走向河边，停下观察"],
                "rhythm": ["先留出观察，再加速行动"],
                "estimated_duration_ms": duration + (remainder if index == 0 else 0),
            }
            for index, identity in enumerate(task["scene_ids"])
        ],
        "evidence": evidence,
        "open_questions": [],
    }


def install_model(model_service, *, extra=None, patch=None, human=False, on_http=None):
    sent, errors = [], []

    def respond(request):
        try:
            payload = json.loads(request.content)
            sent.append(payload)
            if on_http:
                on_http(payload)
            assert {tool["function"]["name"] for tool in payload.get("tools", [])} <= {
                "investigate_production"
            }
            feedback = tool_feedback(payload)
            answered = any(
                message["role"] == "user" and "human_decision" in message.get("content", "")
                for message in payload["messages"]
            )
            if not feedback:
                result = call_response(
                    *[
                        (f"research-{index}", "investigate_production", {"query": query})
                        for index, query in enumerate(
                            [
                                {"path": "script"},
                                {"path": "visual"},
                                {"path": "capabilities", "provider": "mock", "model": "mock-image"},
                            ]
                        )
                    ]
                )
            elif extra and len(feedback) == 3:
                result = call_response(("branch", "investigate_production", {"query": extra}))
            elif human and not answered:
                result = final("HUMAN_REVIEW")
            else:
                value = plan_value(payload)
                if patch:
                    patch(value)
                result = final(result=value)
            return httpx.Response(200, json=result)
        except Exception as error:
            errors.append(repr(error))
            raise

    model_service(respond)
    return sent, errors


def test_actual_plan_persists_exact_input_and_replays_without_domain_publication(
    client, episode, settings, model_service
):
    run_id, actor, source = prepare(client, episode, settings)
    sent, errors = install_model(model_service)
    factory = client.app.state.session_factory
    with factory() as db:
        before = [
            db.scalar(select(func.count()).select_from(model))
            for model in (Script, Storyboard, Review)
        ]
        original = digest(db.get(WorkflowRun, run_id).input_json)
    result = planning.plan_director(factory, settings, run_id, actor)
    assert result.status == "COMPLETED", (result.model_dump_json(), errors)
    assert result.result.script_version_id == UUID(source["id"])
    assert planning.plan_director(factory, settings, run_id, actor) == result
    assert planning.resume_director_plan(factory, settings, result.loop_id, actor) == result
    assert len(sent) == 2
    with factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert digest(run.input_json) == original
        assert run.status == "RUNNING" and run.pending_review is None
        assert run.agent_state_json["extensions"]["director.planning"]["loop_id"] == str(
            result.loop_id
        )
        assert planning.read_director_plan(db, result.loop_id, actor) == result
        assert before == [
            db.scalar(select(func.count()).select_from(model))
            for model in (Script, Storyboard, Review)
        ]
        assert db.get(AgentLoop, result.loop_id).thread_id == f"agent-loop:{result.loop_id}"
    assert settings.llm_api_key.get_secret_value() not in json.dumps(sent)


@pytest.mark.parametrize(
    "field", ["script", "hash", "scene", "target", "duration", "evidence", "blocking"]
)
def test_plan_rejects_scope_timing_forged_evidence_and_unresolved_questions(
    client, episode, settings, model_service, field
):
    run_id, actor, _ = prepare(client, episode, settings)

    def patch(value):
        if field == "script":
            value["script_version_id"] = str(uuid4())
        elif field == "hash":
            value["script_sha256"] = "0" * 64
        elif field == "scene":
            value["scenes"][0]["scene_id"] = str(uuid4())
        elif field == "target":
            value["target_duration_ms"] += 1000
        elif field == "duration":
            value["estimated_duration_ms"] *= 2
            value["scenes"][0]["estimated_duration_ms"] += value["target_duration_ms"]
        elif field == "evidence":
            value["evidence"][0]["content_sha256"] = "0" * 64
        else:
            value["open_questions"] = [
                {"question": "缺少参考", "reason": "MISSING_EVIDENCE", "blocking": True}
            ]

    _, errors = install_model(model_service, patch=patch)
    result = planning.plan_director(client.app.state.session_factory, settings, run_id, actor)
    assert not errors
    assert result.status == "STOPPED"
    assert result.stop_code in {
        "DIRECTOR_PLAN_SCOPE",
        "DIRECTOR_PLAN_DURATION",
        "DIRECTOR_PLAN_EVIDENCE",
        "DIRECTOR_CLARIFICATION_REQUIRED",
    }


def test_missing_material_branch_escalates_and_answer_resumes_same_loop(
    client, episode, settings, model_service
):
    run_id, actor, _ = prepare(client, episode, settings)
    sent, errors = install_model(
        model_service,
        extra={"path": "catalog", "name": "缺失河边", "asset_type": "SCENE"},
        human=True,
    )
    factory = client.app.state.session_factory
    waiting = planning.plan_director(factory, settings, run_id, actor)
    assert waiting.status == "HUMAN_REVIEW", (waiting.model_dump_json(), errors)
    missing = tool_feedback(sent[-1])[-1]["result"]["missing"]
    assert missing[0]["reason"] == "NOT_FOUND"
    resumed = planning.resume_director_plan(
        factory, settings, waiting.loop_id, actor, decision=answer(waiting)
    )
    assert resumed.status == "COMPLETED", (resumed.model_dump_json(), errors)
    assert resumed.loop_id == waiting.loop_id
    assert (
        planning.resume_director_plan(
            factory, settings, waiting.loop_id, actor, decision=answer(waiting)
        )
        == resumed
    )
