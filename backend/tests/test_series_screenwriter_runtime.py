"""Series workflow runs real tools, persistence, patch checks and review boundaries."""

import copy
import importlib.util
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_agent_loop_recovery import answer
from test_agent_model import call_response
from test_agent_model import model_service as model_service
from test_agent_runtime import final, tool_feedback
from test_agent_tools import grant
from test_context_trace import trace
from test_screenwriter_creation import progress, run_once, start
from test_script_rewrite import body as rewrite_body
from test_script_rewrite import source as rewrite_source
from test_script_rewrite import start as start_rewrite
from test_series_planning import planned_outline as planned_outline
from test_story_ledger import approve_ledger, approved_script, ledger_content, save_ledger
from test_writing_context import extended_plan, ledger_baseline, preview
from test_writing_context import writing_case as writing_case

from arclight.agents.contracts import ScriptContent, ScriptContentV3, read_script_content
from arclight.application import screenwriter_planning as planning
from arclight.application.agent_memory_sources import capture_memory_sources
from arclight.application.agent_runtime_state import initialize_run_state, scope_for_run
from arclight.application.agent_tools import build_agent_tool_registry
from arclight.application.context_consumption import consume_context
from arclight.application.context_sources import validate_sources
from arclight.application.gateway_text import execution_actor
from arclight.application.screenwriter_creation import scene_narrative_projections
from arclight.application.screenwriter_research import investigate_series_story
from arclight.core.errors import DomainError
from arclight.domain.agent_state import transition_agent_step
from arclight.domain.model_gateway import GatewayPolicy
from arclight.domain.screenwriter_planning import SeriesScreenwriterPlan
from arclight.domain.screenwriter_research import (
    SERIES_RESEARCH_TOOL,
    InvestigateSeriesStoryParameters,
)
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.models import DependencyManifest, Script, WorkflowRun
from arclight.persistence.story_ledger import StoryLedgerVersion


@pytest.fixture
def series_model_service(writing_case, request):
    """Build the approved baseline before selecting the model HTTP test transport."""
    return request.getfixturevalue("model_service")


def install_series(model_service, settings, *, author_limit=None, transform=None):
    path = Path(__file__).resolve().parents[1] / "scripts" / "e2e_screenwriter.py"
    spec = importlib.util.spec_from_file_location("series_screenwriter_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost="0", pricing_version="series-runtime-test:1"
    )
    sent, errors = [], []

    def respond(request):
        try:
            sent.append(json.loads(request.content))
            response = module._respond(request)
            if author_limit is not None:
                value = response.json()
                for call in value["choices"][0]["message"].get("tool_calls", []):
                    function = call["function"]
                    if function["name"] == "investigate_series_story":
                        arguments = json.loads(function["arguments"])
                        if not arguments.get("character_id"):
                            arguments["limit"] = author_limit
                            function["arguments"] = json.dumps(arguments)
                response = httpx.Response(200, json=value)
            if transform is not None:
                response = httpx.Response(200, json=transform(sent[-1], response.json()))
            return response
        except Exception as exc:
            errors.append(repr(exc))
            raise

    model_service(respond)
    return sent, errors


def seeds(sent):
    return [json.loads(payload["messages"][1]["content"])["data"] for payload in sent]


def test_series_creation_consumes_views_patches_and_saves_complete_v3_without_facts(
    client, settings, series_model_service, writing_case
):
    sent, errors = install_series(series_model_service, settings, author_limit=1)
    run_id = start(client, writing_case["episode"], narrative=writing_case["request"])
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors)
    assert not errors
    assert run["workflow_version"] == "screenwriter-series/1.0"
    operations = progress(client, run_id)
    assert [item["kind"] for item in operations] == [
        "plan",
        "scene",
        "scene",
        "critique",
        "critique",
    ]
    assert all(item["status"] == "COMPLETED" for item in operations)
    assert [item["result"]["decision"] for item in operations[-2:]] == ["REVISE", "ACCEPT"]
    assert operations[-2]["revision"]["patch_checks"][0]["accepted"]
    with client.app.state.session_factory() as db:
        stored_run = db.get(WorkflowRun, run_id)
        assert stored_run.input_json["screenwriter"]["revision_rounds"] == 3
        candidate = db.scalar(select(Script).where(Script.run_id == run_id))
        assert candidate is not None and candidate.status == "READY_FOR_REVIEW"
        content = read_script_content(candidate.content_json)
        assert isinstance(content, ScriptContentV3)
        assert content.model_dump(mode="json") == candidate.content_json
        assert (
            content.narrative_context.model_dump(mode="json")
            == stored_run.input_json["narrative_context"]
        )
        assert len(content.scenes) == 2
        assert all(scene.state_changes for scene in content.scenes)
        assert "失主的嘱托" in content.scenes[0].action
        assert run["pending_review"]["version_id"] == str(candidate.id)
        assert db.scalar(select(func.count()).select_from(StoryLedgerVersion)) == 0
        actions = db.scalars(
            select(AgentLoopAction)
            .join(AgentLoop, AgentLoop.id == AgentLoopAction.loop_id)
            .where(AgentLoop.run_id == run_id, AgentLoopAction.kind == "TOOL")
        ).all()
        research = [
            action.result_json["result"]
            for action in actions
            if action.request_json.get("tool_name") == "investigate_series_story"
        ]
        author = next(
            value
            for value in research
            if value["materials"][0]["content"]["perspective"] == "AUTHOR"
        )
        projection = author["materials"][0]["content"]
        assert not projection["truncated"] and projection["index_truncated"]
        assert author["has_more"]  # Index pagination is independent from the fact page.
        manifest = db.get(DependencyManifest, UUID(author["manifest_id"]))
        assert (
            manifest.snapshot_json["context"]["investigation"]["materials"][0]["content_sha256"]
            == author["materials"][0]["content_sha256"]
        )
    scene_seeds = [seed for seed in seeds(sent) if seed.get("scene_no")]
    assert [seed["scene_no"] for seed in scene_seeds] == [1, 2]
    assert scene_seeds[1]["previous"][0]["scene"]["dramatic_beat"]
    assert scene_seeds[1]["previous"][0]["scene"]["state_changes"]
    for seed in scene_seeds:
        views = seed["narrative_projections"]
        assert views[0]["perspective"] == "AUTHOR"
        assert views[0]["target"]["scene_no"] == seed["scene_no"]
        assert {view["character_id"] for view in views[1:]} == set(
            writing_case["request"]["character_ids"]
        )
        assert all(view["plan"] is None and not view["causal_summaries"] for view in views[1:])
        assert all(view["target"]["scene_no"] == seed["scene_no"] for view in views[1:])
    before = len(sent)
    assert run_once(client, settings, run_id)["status"] == "WAITING_REVIEW"
    assert len(sent) == before  # Re-entry cannot replace successful fragment or patch receipts.


def manual_series_source(client, case):
    base = rewrite_source(client, case["episode"])
    response = preview(client, case)
    assert response.status_code == 200, response.text
    content = copy.deepcopy(base["content_json"])
    content["schema_version"] = "3.0"
    content["narrative_context"] = response.json()["context"]
    identities = case["request"]["character_ids"]
    for scene in content["scenes"]:
        scene["dramatic_beat"] = {
            "story_time": 0,
            "character_objectives": [
                {
                    "character_id": identity,
                    "purpose": "确认来信的主人",
                    "emotion_start": "疑惑",
                    "emotion_end": "坚定",
                }
                for identity in identities
            ],
            "conflict": "双方对送信方案存在分歧",
            "dialogue_interactions": [
                {
                    "line_ids": [line["line_id"] for line in scene["dialogue"]],
                    "character_ids": identities,
                    "action": "提问后等待回应",
                }
            ],
        }
        scene["state_changes"] = [
            {
                "id": str(uuid4()),
                "kind": "EVENT",
                "state_key": f"scene_{scene['scene_no']}",
                "value": "决定查明信件来源",
                "story_start": 0,
            }
        ]
    response = client.post(
        f"/api/v1/episodes/{case['episode']['id']}/scripts",
        json={"expected_version_no": base["version_no"], "content": content},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("scope", ["scene", "paragraph"])
def test_series_independent_rewrite_keeps_unselected_narrative_and_exact_source(
    client, settings, series_model_service, writing_case, scope
):
    base = manual_series_source(client, writing_case)
    sent, errors = install_series(series_model_service, settings)
    scenes = base["content_json"]["scenes"]
    target = {"scope": scope, "scene_id": scenes[1]["scene_id"]}
    if scope == "paragraph":
        target |= {"field": "action", "start": 2, "end": 8}
    payload = rewrite_body(base, target) | {
        "professional": True,
        "narrative": writing_case["request"] | {"target_scene_no": 2},
    }
    response = start_rewrite(client, writing_case["episode"], payload)
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    assert start_rewrite(client, writing_case["episode"], payload).json()["run_id"] == str(run_id)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors)
    assert not errors and run["workflow_version"] == "screenwriter-series-rewrite/1.0"
    operations = progress(client, run_id)
    assert [item["result"]["decision"] for item in operations if item["kind"] == "critique"] == [
        "REVISE",
        "ACCEPT",
    ]
    with client.app.state.session_factory() as db:
        candidate = db.scalar(select(Script).where(Script.run_id == run_id))
        content = read_script_content(candidate.content_json)
        assert isinstance(content, ScriptContentV3)
        assert content.model_dump(mode="json") == candidate.content_json
        assert candidate.content_json["scenes"][0] == scenes[0]
        assert candidate.content_json["scenes"][2] == scenes[2]
        assert db.get(Script, UUID(base["id"])).content_json == base["content_json"]
        changed = candidate.content_json["scenes"][1]
        assert changed["dramatic_beat"] == scenes[1]["dramatic_beat"]
        assert changed["state_changes"] == scenes[1]["state_changes"]
        if scope == "paragraph":
            assert changed["action"].startswith(scenes[1]["action"][:2])
            assert changed["action"].endswith(scenes[1]["action"][8:])
            assert changed["dialogue"] == scenes[1]["dialogue"]
        assert content.narrative_context.target_scene_no == 2
        assert db.scalar(select(func.count()).select_from(StoryLedgerVersion)) == 0
    critique_seeds = [seed for seed in seeds(sent) if seed["phase"] == "CRITIQUE_SCRIPT"]
    assert all(seed["source_script"]["schema_version"] == "3.0" for seed in critique_seeds)
    assert all(seed["source_script"]["scenes"][2]["state_changes"] for seed in critique_seeds)


def test_legacy_script_schema_stays_separate_and_series_rewrite_needs_explicit_baseline(
    client, writing_case
):
    assert "narrative_context" not in ScriptContent.model_json_schema()["properties"]
    base = manual_series_source(client, writing_case)
    payload = rewrite_body(base) | {"professional": True}
    response = start_rewrite(client, writing_case["episode"], payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NARRATIVE_CONTEXT_REQUIRED"


def test_internal_scene_targets_replay_acquisition_forgetting_and_flashback_knowledge(
    client, settings, series_model_service, writing_case
):
    script = approved_script(client, writing_case["episode"])
    identity = writing_case["characters"][0]["id"]
    content = ledger_content(writing_case["episode"], script, identity)
    content["entries"][0]["known_by"] = [
        {"character_id": identity, "story_start": 5, "story_end": 15}
    ]
    created = save_ledger(client, writing_case["episode"], content)
    approved = approve_ledger(client, writing_case["episode"], created)
    writing_case["request"] |= {
        "ledger_version_id": created["version"]["id"],
        "expected_ledger_head_revision": approved["head"]["revision"],
    }
    install_series(series_model_service, settings)
    run_id = start(client, writing_case["episode"], narrative=writing_case["request"])
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        original = copy.deepcopy(run.input_json["narrative_context"])
        project_id = UUID(writing_case["episode"]["project_id"])
        all_views = [
            scene_narrative_projections(db, settings, run, story_time, scene_no, [UUID(identity)])
            for scene_no, story_time in enumerate((0, 10, 20, 0), 1)
        ]
        assert [len(views[1]["facts"]) for views in all_views] == [0, 1, 0, 0]
        assert all(len(views[0]["facts"]) == 1 for views in all_views)
        for scene_no, views in enumerate(all_views, 1):
            for view in views:
                assert view["target"]["scene_no"] == scene_no
                validate_sources(db, project_id, view["sources"])
                memory = capture_memory_sources(db, project_id, view["sources"])
                assert {item["category"] for item in memory["memory_sources"]} == {"EVIDENCE"}
                proof = next(
                    source for source in view["sources"] if source["kind"] == "NARRATIVE_QUERY"
                )
                assert proof["context"]["target_scene_no"] == scene_no
                assert proof["context"]["target_story_time"] == view["target"]["story_time"]
                assert proof["context"]["ledger_version_id"] == original["ledger_version_id"]
                assert proof["context"]["plan_sha256"] == original["plan_sha256"]
        assert run.input_json["narrative_context"] == original


def test_series_research_refresh_appends_binding_after_pause_without_changing_consumption(
    client, settings, series_model_service, writing_case
):
    case = writing_case
    extended_plan(client, case, 6)
    source_episode = case["episode"]
    old_script, _, _ = ledger_baseline(client, case)
    response = client.post(
        f"/api/v1/projects/{source_episode['project_id']}/episodes",
        json={"episode_no": 6, "title": "远集因果回响", "target_duration_ms": 30000},
    )
    assert response.status_code == 201, response.text
    case["episode"] = response.json()
    case["request"]["target_story_time"] = 50

    def respond(payload, original):
        seed = json.loads(payload["messages"][1]["content"])["data"]
        if seed["phase"] != "PLANNING_ONLY":
            return original
        feedback = tool_feedback(payload)
        answered = any(
            "human_decision" in (message.get("content") or "") for message in payload["messages"]
        )
        if not feedback:
            return call_response(("first", SERIES_RESEARCH_TOOL, {}))
        if not answered:
            return final("HUMAN_REVIEW")
        if len(feedback) < 4:
            parameters = {"refresh": True} if len(feedback) == 2 else {}
            if len(feedback) == 1:
                # Explicit default targets have the same normalized query binding.
                parameters = {"story_time": 50, "scene_no": 1}
            return call_response((f"resumed-{len(feedback)}", SERIES_RESEARCH_TOOL, parameters))
        return original

    sent, errors = install_series(series_model_service, settings, transform=respond)
    run_id = start(client, case["episode"], narrative=case["request"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        project_id = UUID(case["episode"]["project_id"])
        state = initialize_run_state(run, project_id)
        run.agent_state_json = transition_agent_step(
            state,
            scope_for_run(run, project_id),
            expected_revision=0,
            step_id="script",
            target="RUNNING",
        ).model_dump(mode="json")
        run.status = "RUNNING"
        actor = execution_actor(db, run)
        initial = investigate_series_story(
            db, settings, run, "script", actor, InvestigateSeriesStoryParameters()
        )
        consume_context(db, project_id, run, "script", settings)
        frozen = copy.deepcopy(
            db.get(DependencyManifest, UUID(initial["manifest_id"])).snapshot_json
        )
    registry = build_agent_tool_registry(settings=settings)
    grant(client, run_id, actor, registry, allowed=(SERIES_RESEARCH_TOOL,))
    receipt = trace(client, run_id)["consumption"]
    assert receipt["evidence_manifest_ids"] == [initial["manifest_id"]]

    waiting = planning.plan_screenwriter(factory, settings, run_id, actor)
    assert waiting.status == "HUMAN_REVIEW", (waiting, errors)
    before_resume = trace(client, run_id)
    assert before_resume["consumption"] == receipt
    newer = approved_script(client, source_episode, expected_version_no=old_script["version_no"])
    assert newer["id"] != old_script["id"]
    completed = planning.resume_screenwriter_plan(
        factory, settings, waiting.loop_id, actor, decision=answer(waiting)
    )
    assert completed.status == "COMPLETED", (completed, errors)
    assert not errors and len(sent) == 6

    with factory() as db:
        actions = db.scalars(
            select(AgentLoopAction).where(
                AgentLoopAction.loop_id == waiting.loop_id,
                AgentLoopAction.kind == "TOOL",
            )
        ).all()
        research = sorted(
            (action.result_json["result"] for action in actions),
            key=lambda item: item["evidence_round"],
        )
        first, repeated, refreshed, latest = research
        assert [item["evidence_round"] for item in research] == [2, 3, 4, 5]
        assert all(item["materials"] == initial["materials"] for item in (first, repeated))
        assert all(item["reused_binding"] for item in (first, repeated, latest))
        assert not refreshed["reused_binding"]
        assert refreshed["materials"] == latest["materials"]
        old_material = initial["materials"][0]
        fresh_material = refreshed["materials"][0]
        assert old_material["bound_manifest_id"] == initial["manifest_id"]
        assert fresh_material["bound_manifest_id"] == refreshed["manifest_id"]
        assert old_material["content_sha256"] != fresh_material["content_sha256"]
        assert (
            old_material["content"]["causal_summaries"][0]["script_version_id"] == old_script["id"]
        )
        assert fresh_material["content"]["causal_summaries"][0]["script_version_id"] == newer["id"]
        assert db.get(DependencyManifest, UUID(initial["manifest_id"])).snapshot_json == frozen
        refresh_row = db.get(DependencyManifest, UUID(refreshed["manifest_id"]))
        assert refresh_row.snapshot_json["context"]["investigation"]["refresh"] is True
        assert "refresh" not in refresh_row.snapshot_json["context"]["investigation"]["query"]
        assert completed.result.evidence[-1].bound_manifest_id == UUID(refreshed["manifest_id"])
    after_resume = trace(client, run_id)
    assert after_resume["consumption"] == receipt
    assert after_resume["evidence"][:2] == before_resume["evidence"]
    assert len(after_resume["evidence"]) == 5


def test_series_creation_validates_second_scene_anchor_while_first_scene_is_flashback(
    client, settings, series_model_service, writing_case
):
    writing_case["request"] |= {"target_scene_no": 2, "target_story_time": 10}

    def respond(payload, value):
        message = value["choices"][0]["message"]
        if not message.get("content"):
            return value
        envelope = json.loads(message["content"])
        result = envelope.get("result")
        if result and result.get("schema_version") == "screenwriter-plan/2":
            result["plan"]["scenes"][0]["story_time"] = -5
            message["content"] = json.dumps(envelope, ensure_ascii=False)
        return value

    _, errors = install_series(series_model_service, settings, transform=respond)
    run_id = start(client, writing_case["episode"], narrative=writing_case["request"])
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors)
    operations = progress(client, run_id)
    plan = SeriesScreenwriterPlan.model_validate(operations[0]["result"])
    assert [scene.story_time for scene in plan.plan.scenes] == [-5, 10]
    with client.app.state.session_factory() as db:
        candidate = db.scalar(select(Script).where(Script.run_id == run_id))
        content = read_script_content(candidate.content_json)
        assert [scene.dramatic_beat.story_time for scene in content.scenes] == [-5, 10]
        altered = plan.model_copy(deep=True)
        altered.plan.scenes[1].story_time = 11
        with pytest.raises(DomainError) as rejected:
            planning._validate(db, run_id, altered)
        assert rejected.value.code == "SCREENWRITER_PLAN_SCOPE"
        altered = plan.model_copy(deep=True)
        altered.plan.scenes.pop()
        with pytest.raises(DomainError) as missing:
            planning._validate(db, run_id, altered)
        assert missing.value.code == "SCREENWRITER_PLAN_SCOPE"


@pytest.mark.parametrize("anchor_in_scope", [True, False])
def test_series_rewrite_maps_anchor_through_source_scene_ids(
    client, settings, series_model_service, writing_case, anchor_in_scope
):
    base = manual_series_source(client, writing_case)
    scenes = base["content_json"]["scenes"]
    target = (
        {"scope": "chapter", "scene_ids": [scene["scene_id"] for scene in scenes[:2]]}
        if anchor_in_scope
        else {"scope": "scene", "scene_id": scenes[1]["scene_id"]}
    )

    def respond(payload, value):
        message = value["choices"][0]["message"]
        if not message.get("content"):
            return value
        envelope = json.loads(message["content"])
        result = envelope.get("result")
        if result and result.get("schema_version") == "screenwriter-plan/2":
            result["plan"]["scenes"][0]["story_time"] = -5
            message["content"] = json.dumps(envelope, ensure_ascii=False)
        return value

    _, errors = install_series(series_model_service, settings, transform=respond)
    body = rewrite_body(base, target) | {
        "professional": True,
        "narrative": writing_case["request"] | {"target_scene_no": 2 if anchor_in_scope else 1},
    }
    invalid = start_rewrite(
        client,
        writing_case["episode"],
        body | {"narrative": body["narrative"] | {"target_scene_no": len(scenes) + 1}},
    )
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["error"]["code"] == "NARRATIVE_TARGET_INVALID"
    if not anchor_in_scope:
        invalid = start_rewrite(
            client,
            writing_case["episode"],
            body | {"narrative": body["narrative"] | {"target_story_time": 1}},
        )
        assert invalid.status_code == 422, invalid.text
        assert invalid.json()["error"]["code"] == "NARRATIVE_TARGET_INVALID"
    response = start_rewrite(client, writing_case["episode"], body)
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors)
    operations = progress(client, run_id)
    plan = SeriesScreenwriterPlan.model_validate(operations[0]["result"])
    assert plan.plan.scenes[0].story_time == -5
    with client.app.state.session_factory() as db:
        candidate = db.scalar(select(Script).where(Script.run_id == run_id))
        assert candidate.content_json["scenes"][2] == scenes[2]
        anchor = 2 if anchor_in_scope else 1
        assert candidate.content_json["scenes"][anchor - 1]["dramatic_beat"]["story_time"] == 0
        if anchor_in_scope:
            assert [scene.scene_id for scene in plan.plan.scenes] == [
                scene["scene_id"] for scene in scenes[:2]
            ]
            altered = plan.model_copy(deep=True)
            altered.plan.scenes[1].story_time = 1
            with pytest.raises(DomainError) as rejected:
                planning._validate(db, run_id, altered)
            assert rejected.value.code == "SCREENWRITER_PLAN_SCOPE"
        else:
            assert candidate.content_json["scenes"][0] == scenes[0]
            planning._validate(db, run_id, plan)
