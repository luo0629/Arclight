"""Production feedback drives investigation; source and retry gates precede I/O."""

import json
from copy import deepcopy
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_agent_model import call_response
from test_agent_model import model_service as model_service
from test_agent_runtime import final, tool_feedback
from test_api_workflow import review
from test_collaboration import register
from test_director_planning import activate, install_model, plan_value, prepare
from test_screenwriter_creation import install as install_writer
from test_screenwriter_creation import run_once
from test_screenwriter_creation import start as start_writer
from test_text_contract_v2 import make_asset, save_script

from arclight.application import director_planning as planning
from arclight.core.errors import DomainError
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import AssetVersion, GenerationJob, Script
from arclight.providers.media_capabilities import MOCK_CAPABILITIES
from arclight.providers.text import MockTextProvider


@pytest.mark.parametrize("supports_seed", [True, False])
def test_capability_feedback_selects_exact_asset_or_missing_reference_and_human(
    client, episode, settings, model_service, monkeypatch, supports_seed
):
    with client.app.state.session_factory.begin() as db:
        ref = make_asset(db, episode["project_id"], "SCENE")
    profile = MOCK_CAPABILITIES.model_copy(update={"supports_seed": supports_seed})
    monkeypatch.setattr(
        "arclight.application.director_research.resolve_capabilities", lambda *args: profile
    )
    run_id, actor, _ = prepare(client, episode, settings)
    branches = []

    def respond(request):
        payload = json.loads(request.content)
        feedback = tool_feedback(payload)
        if not feedback:
            queries = [
                {"path": "script"},
                {"path": "visual"},
                {"path": "capabilities", "provider": "mock", "model": "mock-image"},
            ]
        elif len(feedback) == 3:
            capability = feedback[-1]["result"]["materials"][0]["content"]["declaration"]
            assert capability["supports_seed"] == supports_seed
            query = (
                {"path": "assets", "version_ids": [ref["asset_version_id"]]}
                if capability["supports_seed"]
                else {"path": "catalog", "asset_type": "SCENE", "name": "不存在的固定参考"}
            )
            branches.append(query["path"])
            queries = [query]
        else:
            result = (
                final(result=plan_value(payload))
                if feedback[-1]["result"]["materials"]
                else final("HUMAN_REVIEW")
            )
            return httpx.Response(200, json=result)
        return httpx.Response(
            200,
            json=call_response(
                *[
                    (f"query-{len(feedback)}-{index}", "investigate_production", {"query": query})
                    for index, query in enumerate(queries)
                ]
            ),
        )

    model_service(respond)
    result = planning.plan_director(client.app.state.session_factory, settings, run_id, actor)
    assert result.status == ("COMPLETED" if supports_seed else "HUMAN_REVIEW"), (
        result.model_dump_json()
    )
    assert branches == (["assets"] if supports_seed else ["catalog"])
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0


@pytest.mark.parametrize("change", ["unapproved", "stale"])
def test_unusable_script_is_rejected_before_new_plan_or_model_io(
    client, episode, settings, model_service, change
):
    run_id, actor, source = prepare(client, episode, settings)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        if change == "unapproved":
            db.get(Script, UUID(source["id"])).status = "REJECTED"
        else:
            db.get(AssetVersion, UUID(source["id"])).stale = True
    sent, _ = install_model(model_service)
    with pytest.raises(DomainError) as raised:
        planning.plan_director(factory, settings, run_id, actor)
    assert raised.value.code == (
        "CONTEXT_REFERENCE_INVALID" if change == "unapproved" else "CONTEXT_INPUT_NOT_USABLE"
    )
    assert sent == []
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentLoop)) == 0


@pytest.mark.parametrize("all_pages", [False, True])
def test_all_original_scenes_require_actual_script_evidence_pages(
    client, episode, settings, model_service, all_pages
):
    content = MockTextProvider().script("两个场景", {}).model_dump(mode="json")
    second = deepcopy(content["scenes"][0])
    second.update(scene_id=str(uuid4()), scene_no=2, title="远处河岸", dialogue=[])
    content["scenes"].append(second)
    source = save_script(client, episode, content)
    run_id, actor, _ = prepare(client, episode, settings, source=source)
    ids = [scene["scene_id"] for scene in source["content_json"]["scenes"]]

    def respond(request):
        payload = json.loads(request.content)
        feedback = tool_feedback(payload)
        if not feedback:
            queries = [
                {"path": "script", "scene_ids": ids[:1]},
                {"path": "visual"},
                {"path": "capabilities", "provider": "mock", "model": "mock-image"},
            ]
        elif all_pages and len(feedback) == 3:
            queries = [{"path": "script", "scene_ids": ids[1:]}]
        else:
            return httpx.Response(200, json=final(result=plan_value(payload)))
        return httpx.Response(
            200,
            json=call_response(
                *[
                    (f"page-{len(feedback)}-{index}", "investigate_production", {"query": query})
                    for index, query in enumerate(queries)
                ]
            ),
        )

    model_service(respond)
    result = planning.plan_director(client.app.state.session_factory, settings, run_id, actor)
    assert result.status == ("COMPLETED" if all_pages else "STOPPED"), result.model_dump_json()
    if not all_pages:
        assert result.stop_code == "DIRECTOR_PLAN_EVIDENCE"
    else:
        assert [scene.scene_id for scene in result.result.scenes] == ids


def test_original_dual_review_run_uses_its_approved_deferred_script(
    client, episode, settings, model_service, monkeypatch
):
    _, writer_errors = install_writer(model_service, monkeypatch, settings)
    run_id = start_writer(client, episode)
    state = run_once(client, settings, run_id)
    assert state["status"] == "WAITING_REVIEW", (state, writer_errors)
    source_id = state["pending_review"]["version_id"]
    assert review(client, state).status_code == 202

    class PendingDirector(MockTextProvider):
        def storyboard(self, *args, **kwargs):
            raise RuntimeError("director fixture paused before legacy generation")

    monkeypatch.setattr(
        "arclight.application.screenwriter_execution.build_provider", lambda _: PendingDirector()
    )
    assert run_once(client, settings, run_id)["status"] == "FAILED"
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    actor = activate(client, episode, settings, run_id)
    sent, errors = install_model(model_service)
    outcome = planning.plan_director(client.app.state.session_factory, settings, run_id, actor)
    assert outcome.status == "COMPLETED", (outcome.model_dump_json(), errors)
    assert str(outcome.result.script_version_id) == source_id
    assert len(sent) == 2


def test_completed_plan_blocks_new_attempt_and_foreign_read_or_resume_before_io(
    client, episode, settings, model_service
):
    run_id, actor, _ = prepare(client, episode, settings)
    sent, errors = install_model(model_service)
    factory = client.app.state.session_factory
    result = planning.plan_director(factory, settings, run_id, actor)
    assert result.status == "COMPLETED", (result.model_dump_json(), errors)
    with pytest.raises(DomainError) as conflict:
        planning.plan_director(factory, settings, run_id, actor, attempt=1)
    assert conflict.value.code == "DIRECTOR_PLAN_CONFLICT"
    outsider = register(client, "director-outsider@example.com")["user"]["id"]
    with factory() as db, pytest.raises(DomainError) as forbidden:
        planning.read_director_plan(db, result.loop_id, outsider)
    assert forbidden.value.status_code == 404
    with pytest.raises(DomainError) as forbidden:
        planning.resume_director_plan(factory, settings, result.loop_id, outsider)
    assert forbidden.value.status_code == 404
    assert len(sent) == 2
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentLoop)) == 1
