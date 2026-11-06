"""Planning-specific recovery through the real journal, authorization and database."""

import json
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from test_agent_loop_recovery import answer
from test_agent_model import call_response
from test_agent_model import model_service as model_service
from test_agent_runtime import final, tool_feedback
from test_agent_tools import create_entry
from test_collaboration import create_episode, create_project, register
from test_concurrency import require_postgres
from test_context_trace import trace
from test_screenwriter_planning import install_model, plan_value, prepare

from arclight.application import screenwriter_planning as planning
from arclight.application.agent_loop_recovery import run_persistent_agent_loop
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.screenwriter_planning import ScreenwriterPlan
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision
from arclight.persistence.base import utcnow
from arclight.persistence.models import (
    AgentToolGrant,
    AgentToolInvocation,
    Project,
    ProjectMember,
    StoryEntry,
    User,
    WorkflowRun,
)


def test_missing_investigation_escalates_and_requires_explicit_refresh_after_human_answer(
    client, episode, settings, model_service
):
    run_id, actor = prepare(client, episode, settings)
    sent = []
    query = {"path": "entries", "name": "测试"}

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        feedback = tool_feedback(payload)
        answered = any(
            m["role"] == "user" and "human_decision" in (m.get("content") or "")
            for m in payload["messages"]
        )
        if not feedback or (answered and len(feedback) < 3):
            if feedback:
                assert feedback[-1]["result"]["missing"]
                assert not feedback[-1]["result"]["materials"]
            value = call_response(
                (
                    f"lookup-{len(feedback)}",
                    "investigate_story",
                    {"query": query, "refresh": len(feedback) == 2},
                )
            )
        elif not answered:
            assert feedback[-1]["result"]["missing"][0]["reason"] == "NOT_FOUND"
            value = final("HUMAN_REVIEW")
        else:
            assert feedback[-1]["result"]["materials"][0]["content"]["fact"] == "雨后返家"
            value = final(result=plan_value(payload))
        return httpx.Response(200, json=value)

    model_service(respond)
    factory = client.app.state.session_factory
    waiting = planning.plan_screenwriter(factory, settings, run_id, actor)
    assert waiting.status == "HUMAN_REVIEW"
    assert waiting.human_request.reason == "MISSING_EVIDENCE"
    entry = create_entry(client, episode["project_id"], data={"fact": "雨后返家"})
    completed = planning.resume_screenwriter_plan(
        factory, settings, waiting.loop_id, actor, decision=answer(waiting)
    )
    assert completed.status == "COMPLETED" and completed.model_turns == 5
    assert len(sent) == 5 and completed.result.evidence[0].source_id == entry
    history = trace(client, run_id)["evidence"]
    assert len(history) == 3
    assert not history[0]["investigation"]["materials"]
    assert not history[1]["investigation"]["materials"]
    assert history[2]["investigation"]["materials"][0]["source_id"] == str(entry)


def test_planning_keeps_bound_source_after_update_during_human_pause(
    client, episode, settings, model_service
):
    run_id, actor = prepare(client, episode, settings)
    entry = create_entry(client, episode["project_id"], data={"fact": "原设定"})
    sent = []

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        feedback = tool_feedback(payload)
        answered = any("human_decision" in (m.get("content") or "") for m in payload["messages"])
        if not feedback or (answered and len(feedback) == 1):
            value = call_response(
                (
                    f"bound-{len(feedback)}",
                    "investigate_story",
                    {
                        "query": {
                            "path": "exact",
                            "source_kind": "STORY_ENTRY",
                            "source_id": str(entry),
                        }
                    },
                )
            )
        elif not answered:
            value = final("HUMAN_REVIEW")
        else:
            assert feedback[-1]["result"]["materials"][0]["content"]["fact"] == "原设定"
            value = final(result=plan_value(payload))
        return httpx.Response(200, json=value)

    model_service(respond)
    factory = client.app.state.session_factory
    waiting = planning.plan_screenwriter(factory, settings, run_id, actor)
    assert waiting.status == "HUMAN_REVIEW"
    with factory.begin() as db:
        row = db.get(StoryEntry, entry)
        row.data_json = {"fact": "新设定"}
        row.revision += 1
    completed = planning.resume_screenwriter_plan(
        factory, settings, waiting.loop_id, actor, decision=answer(waiting)
    )
    assert completed.status == "COMPLETED" and len(sent) == 4
    first, second = completed.result.evidence
    assert first.bound_manifest_id == second.bound_manifest_id
    assert first.content_sha256 == second.content_sha256


@pytest.mark.parametrize("revocation", ["membership", "disabled"])
def test_owner_cannot_resume_planning_with_revoked_original_initiator(
    client, settings, model_service, revocation
):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    editor = register(client, "planning-editor@example.test")
    editor_id = UUID(editor["user"]["id"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        db.add(ProjectMember(project_id=UUID(project["id"]), user_id=editor_id, role="editor"))
    run_id, actor = prepare(client, episode, settings)
    sent, errors = install_model(model_service, human_reason="USER_DECISION")
    waiting = planning.plan_screenwriter(factory, settings, run_id, actor)
    assert waiting.status == "HUMAN_REVIEW", errors
    with factory.begin() as db:
        if revocation == "disabled":
            db.get(User, editor_id).disabled = True
        else:
            db.execute(ProjectMember.__table__.delete().where(ProjectMember.user_id == editor_id))
    with pytest.raises(DomainError) as caught:
        planning.resume_screenwriter_plan(
            factory, settings, waiting.loop_id, owner["user"]["id"], decision=answer(waiting)
        )
    assert caught.value.code == ("FORBIDDEN" if revocation == "disabled" else "NOT_FOUND")
    assert len(sent) == 2


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("archive", "PROJECT_ARCHIVED"),
        ("cancel", "AGENT_CANCELLED"),
        ("state", "RUN_STATE_INVALID"),
        ("deadline", "AGENT_DEADLINE_EXCEEDED"),
    ],
)
def test_planning_resume_rechecks_scope_and_original_deadline_before_model_io(
    client, episode, settings, model_service, monkeypatch, mutation, expected
):
    run_id, actor = prepare(client, episode, settings)
    sent, errors = install_model(model_service, human_reason="USER_DECISION")
    factory = client.app.state.session_factory
    waiting = planning.plan_screenwriter(factory, settings, run_id, actor)
    assert waiting.status == "HUMAN_REVIEW", errors
    with factory.begin() as db:
        if mutation == "archive":
            db.get(Project, UUID(episode["project_id"])).archived = True
        elif mutation == "cancel":
            db.get(WorkflowRun, run_id).status = "CANCELLED"
        elif mutation == "state":
            run = db.get(WorkflowRun, run_id)
            run.agent_state_json = run.agent_state_json | {"input_sha256": "f" * 64}
        else:
            deadline = db.get(AgentRunLimit, run_id).deadline_at
            monkeypatch.setattr(
                "arclight.application.agent_limits.utcnow",
                lambda: deadline.replace(tzinfo=utcnow().tzinfo) + timedelta(seconds=1),
            )
    with pytest.raises(DomainError) as caught:
        planning.resume_screenwriter_plan(
            factory, settings, waiting.loop_id, actor, decision=answer(waiting)
        )
    assert caught.value.code == expected
    assert len(sent) == 2
    with factory() as db:
        assert db.scalar(select(AgentLoopDecision)) is None


def test_revoked_investigation_grant_blocks_further_research_without_appending_evidence(
    client, episode, settings, model_service
):
    run_id, actor = prepare(client, episode, settings)
    sent, errors = install_model(model_service, human_reason="USER_DECISION")
    factory = client.app.state.session_factory
    waiting = planning.plan_screenwriter(factory, settings, run_id, actor)
    assert waiting.status == "HUMAN_REVIEW", errors
    with factory.begin() as db:
        db.scalar(
            select(AgentToolGrant).where(AgentToolGrant.run_id == run_id)
        ).allowed_tools_json = []
    before = trace(client, run_id)
    model_service(
        lambda request: (
            sent.append(request)
            or httpx.Response(
                200,
                json=call_response(
                    ("revoked", "investigate_story", {"query": {"path": "startup"}})
                ),
            )
        )
    )
    result = planning.resume_screenwriter_plan(
        factory, settings, waiting.loop_id, actor, decision=answer(waiting)
    )
    # The revoked tool disappears from the model declaration, so the gateway
    # rejects the attempted call before the tool executor can admit it.
    assert result.status == "STOPPED" and result.stop_code == "GATEWAY_INVALID_RESPONSE"
    assert len(sent) == 3 and trace(client, run_id) == before
    with factory() as db:
        assert len(list(db.scalars(select(AgentToolInvocation)))) == 1


def test_planning_resume_does_not_reset_original_model_allowance(
    client, episode, settings, model_service
):
    run_id, actor = prepare(client, episode, settings)
    sent, errors = install_model(model_service, human_reason="USER_DECISION")
    factory = client.app.state.session_factory
    waiting = planning.plan_screenwriter(
        factory, settings, run_id, actor, limits=AgentRunLimits(max_model_calls=2)
    )
    assert waiting.status == "HUMAN_REVIEW", errors
    result = planning.resume_screenwriter_plan(
        factory, settings, waiting.loop_id, actor, decision=answer(waiting)
    )
    assert result.status == "STOPPED" and result.stop_code == "AGENT_MODEL_CALL_LIMIT"
    assert len(sent) == 2


def test_planning_cannot_cite_successful_investigation_from_another_operation(
    client, episode, settings, model_service
):
    run_id, actor = prepare(client, episode, settings)
    factory = client.app.state.session_factory
    with factory() as db:
        seed = planning._seed(db.get(WorkflowRun, run_id), settings)
    sent, errors = install_model(model_service)
    other = run_persistent_agent_loop(
        factory,
        settings,
        seed.model_copy(update={"operation_key": "unrelated-investigation:1"}),
        planning._registry(settings),
        ScreenwriterPlan,
    )
    assert other.status == "COMPLETED", errors
    model_service(
        lambda request: (
            sent.append(request)
            or httpx.Response(200, json=final(result=other.result.model_dump(mode="json")))
        )
    )
    result = planning.plan_screenwriter(factory, settings, run_id, actor)
    assert result.status == "STOPPED" and result.stop_code == "SCREENWRITER_PLAN_EVIDENCE"
    assert len(sent) == 3


@pytest.mark.parametrize("duration", [27000, 33000])
def test_planning_accepts_integer_millisecond_closed_duration_bounds(
    client, episode, settings, model_service, duration
):
    run_id, actor = prepare(client, episode, settings)

    def at_bound(value):
        value["plan"]["estimated_duration_ms"] = duration
        value["plan"]["scenes"][0]["estimated_duration_ms"] = duration

    _, errors = install_model(model_service, patch=at_bound)
    result = planning.plan_screenwriter(client.app.state.session_factory, settings, run_id, actor)
    assert result.status == "COMPLETED", errors


def test_postgres_planning_resume_is_fenced_and_model_io_holds_no_business_lock(
    client, episode, settings, model_service
):
    require_postgres(settings)
    run_id, actor = prepare(client, episode, settings)
    _, errors = install_model(model_service, human_reason="USER_DECISION")
    factory = client.app.state.session_factory
    waiting = planning.plan_screenwriter(factory, settings, run_id, actor)
    assert waiting.status == "HUMAN_REVIEW", errors
    sent = []

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        with factory.begin() as db:
            db.execute(text("SET LOCAL lock_timeout = '1500ms'"))
            run = db.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update())
            run.revision += 1
        with pytest.raises(DomainError) as caught:
            planning.resume_screenwriter_plan(factory, settings, waiting.loop_id, actor)
        assert caught.value.code == "AGENT_LOOP_BUSY"
        return httpx.Response(200, json=final(result=plan_value(payload)))

    model_service(respond)
    result = planning.resume_screenwriter_plan(
        factory, settings, waiting.loop_id, actor, decision=answer(waiting)
    )
    assert result.status == "COMPLETED" and len(sent) == 1
    assert planning.resume_screenwriter_plan(factory, settings, waiting.loop_id, actor) == result
    with factory() as db:
        assert db.get(AgentLoop, result.loop_id).thread_id == f"agent-loop:{waiting.loop_id}"
        assert len(list(db.scalars(select(AgentLoopDecision)))) == 1
