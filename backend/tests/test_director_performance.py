"""Performance contracts run on original tools, persistence and human recovery."""

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_director_creation import progress, start
from test_screenwriter_creation import run_once
from test_series_performance_contracts import performance_scene
from test_series_planning import planned_outline as planned_outline
from test_series_screenwriter_runtime import manual_series_source
from test_writing_context import writing_case as writing_case

from arclight.agents.contracts import ScriptContent, StoryboardContentV3
from arclight.application.director_performance import (
    check_performance_storyboard,
    performance_text_definitions,
)
from arclight.application.production_versions import text_definitions
from arclight.domain.art_direction import DIRECTION_PROMPT
from arclight.domain.director_collaboration import OPINION_PROMPT
from arclight.domain.director_performance import PerformanceRevisionSeed
from arclight.domain.model_gateway import GatewayPolicy
from arclight.domain.versions import VersionRegistration, digest
from arclight.persistence.agent_loops import (
    AgentLoop,
    AgentLoopAction,
    AgentLoopDecision,
    AgentLoopQuestion,
)
from arclight.persistence.models import Storyboard, WorkflowRun
from arclight.providers.media_capabilities import MOCK_CAPABILITIES

# Exported from git archive 5248ab5a71ea87e13084f60891b51e3e22de39ee, before SERIES-04.
LEGACY_DEFINITIONS = {
    "director/1.0": "3bc87c11a0b713ca225bdca799783be4fd667c0fd51aeacf90efb090640014c4",
    "director/2.0": "47aa46d21628f0ea205394bfedc03dd7319d1d2e9cbfb663bb5a1235d886ac63",
    "director/3.0": "50e440dd9795790819cd65a9d9a21a89c787c292647d2af3cd2317c61794a43c",
    "production/1.0": "cecb2a9b6a406dd8597ab23f99f39e038ef16733440b4f378a8287c7cf07f7f1",
    "production/2.0": "c12208fc6abd45e3f6b8d60ef60c210395db94246d1e5664b7b69bae0e384fe8",
    "production/3.0": "49bf68eadd8db0e15b70bd6fa46dbe31762dba528e467087b4eac2f5b9424049",
    "production/4.0": "fc88f6d8ae899b4b74619cb064ea53c78a05777df240a37ed9b4f6d40cf73fba",
    "production/5.0": "1fbea4a38dc2f2edd71d7e1f2b52b4828aea02e47a67d3a73e131ae5e181a088",
    "production/6.0": "afcd48ae2a62d1cee8be75fa2149503169574cabde9173ea0e273104460e28f1",
}


@pytest.mark.parametrize("version,expected", LEGACY_DEFINITIONS.items())
def test_archived_definitions_preserve_contracts_and_use_current_art_instruction(version, expected):
    definitions = text_definitions("storyboard", version)
    for kind, _, definition in definitions:
        if kind != "PROMPT" or "art_opinion" not in definition:
            continue
        assert definition["art_opinion"] == DIRECTION_PROMPT + OPINION_PROMPT
        # Prompt updates apply directly; all other historical fields remain exact.
        archived_prompt = (
            Path(__file__).parent / "fixtures/archived_art_direction_prompt.txt"
        ).read_text(encoding="utf-8")
        definition["art_opinion"] = archived_prompt + OPINION_PROMPT
    assert digest(definitions) == expected


def test_performance_definitions_register_complete_shared_schemas():
    definitions = performance_text_definitions("director/4.0")
    for kind, version, definition in definitions:
        VersionRegistration.model_validate(
            {
                "family": "TEXT",
                "kind": kind,
                "name": "storyboard",
                "version": version,
                "definition": definition,
            }
        )
    schema = next(value for kind, _, value in definitions if kind == "SCHEMA")
    assert set(schema) == {
        "$defs",
        "planning",
        "planning_seed",
        "fragment",
        "fragment_seed",
        "work",
        "revision",
        "revision_seed",
        "check_parameters",
        "check_result",
        "research_parameters",
        "research_result",
        "candidate",
    }

    def resolve(reference):
        assert reference.startswith("#/")
        target = schema
        for part in reference[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return target

    def check_refs(value):
        if isinstance(value, dict):
            if "$ref" in value:
                assert resolve(value["$ref"])
            for nested in value.values():
                check_refs(nested)
        elif isinstance(value, list):
            for nested in value:
                check_refs(nested)

    check_refs(schema)
    revision_seed = resolve(schema["revision_seed"]["$ref"])
    script = resolve(revision_seed["properties"]["script"]["$ref"])
    scene = resolve(script["properties"]["scenes"]["items"]["$ref"])
    assert {"dramatic_beat", "state_changes"} <= scene["properties"].keys()
    candidate = resolve(schema["candidate"]["$ref"])
    shot = resolve(candidate["properties"]["shots"]["items"]["$ref"])
    performance = resolve(shot["properties"]["performance"]["$ref"])
    assert {"characters", "interactions", "props", "dialogue"} <= performance["properties"].keys()


def test_video_declaration_and_local_route_cannot_certify_motion_or_lipsync(monkeypatch):
    script, value = performance_scene()
    value["generation_routes"] = [{"provider": "installed", "model": "declared-video"}]
    monkeypatch.setattr(
        "arclight.application.director_performance.resolve_capabilities",
        lambda *_: MOCK_CAPABILITIES.model_copy(update={"provider": "installed"}),
    )
    board = StoryboardContentV3.model_validate(value)
    issues = check_performance_storyboard(None, board, script)
    codes = {issue.code for issue in issues}
    assert "PERFORMANCE_CAPABILITY_UNSUPPORTED" not in codes
    assert {"PERFORMANCE_MOTION_UNVERIFIED", "PERFORMANCE_LIPSYNC_UNVERIFIED"} <= codes
    assert "PERFORMANCE_LIPSYNC_LIMIT" not in codes
    assert board.shots[0].performance.motion_requirement == "dynamic_required"
    value["shots"][0]["duration_ms"] = 15001
    oversized = StoryboardContentV3.model_validate(value)
    issues = check_performance_storyboard(None, oversized, script)
    assert any(issue.code == "PERFORMANCE_LIPSYNC_LIMIT" and issue.slot == 0 for issue in issues)


def test_mock_capability_is_visible_and_not_treated_as_real():
    script, value = performance_scene()
    value["generation_routes"] = [{"provider": "mock", "model": "mock-image"}]
    board = StoryboardContentV3.model_validate(value)
    codes = {issue.code for issue in check_performance_storyboard(None, board, script)}
    assert {"PERFORMANCE_MOCK_NOT_PRODUCTION", "PERFORMANCE_CAPABILITY_UNSUPPORTED"} <= codes


def test_second_scene_issues_use_scene_local_slots():
    script, value = performance_scene()
    source = script.model_dump(mode="json")
    second = copy.deepcopy(source["scenes"][0])
    second["scene_no"], second["scene_id"] = 2, str(uuid4())
    identities = {line["line_id"]: str(uuid4()) for line in second["dialogue"]}
    for line in second["dialogue"]:
        line["line_id"] = identities[line["line_id"]]
    source["scenes"].append(second)
    copied = copy.deepcopy(value["shots"])
    for shot in copied:
        shot["shot_no"] += 3
        shot["scene_no"], shot["scene_id"] = 2, second["scene_id"]
        shot["dialogue_line_ids"] = [identities[line] for line in shot["dialogue_line_ids"]]
        for line in shot["performance"]["dialogue"]:
            line["line_id"] = identities[line["line_id"]]
        if shot["performance"]["continuity_from_shot"] is not None:
            shot["performance"]["continuity_from_shot"] += 3
    value["shots"].extend(copied)
    board = StoryboardContentV3.model_validate(value)
    issues = check_performance_storyboard(None, board, ScriptContent.model_validate(source))
    locations = [issue.slot for issue in issues if issue.scene_id == second["scene_id"]]
    assert locations and min(locations) == 0 and max(locations) <= 2


@pytest.fixture
def performance_service(writing_case, request):
    """Construct the approved planning baseline before switching to model HTTP transport."""
    return request.getfixturevalue("model_service")


def test_actual_creation_waits_for_capability_decision_and_replays_exact_v3(
    client, writing_case, performance_service, monkeypatch, settings
):
    source = manual_series_source(client, writing_case)
    response = client.post(
        f"/api/v1/episodes/{writing_case['episode']['id']}/scripts/{source['id']}/review",
        json={"decision": "APPROVE"},
    )
    assert response.status_code == 200, response.text
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "performance_e2e_screenwriter", scripts / "e2e_screenwriter.py"
    )
    assert spec and spec.loader
    responder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(responder)
    performance_service(responder._respond)
    # Recovery may serialize the task type set in a different order than the saved receipt.
    declaration = MOCK_CAPABILITIES.model_dump(mode="json")
    declaration["task_types"].reverse()
    monkeypatch.setattr(
        "arclight.application.director_performance.resolve_capabilities",
        lambda *_: SimpleNamespace(model_dump=lambda **_: declaration),
    )
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost="0", pricing_version="series04:1"
    )
    run_id, _ = start(
        client, writing_case["episode"], source, performance=True, director_revision_rounds=1
    )
    run = run_once(client, settings, run_id)
    assert run["workflow_version"] == "director/4.0"
    # Native human questions pause the inner loop while the owning workflow stays RUNNING.
    assert run["status"] == "RUNNING", run
    before = progress(client, run_id)
    assert before["can_cancel"] is True
    checks = [item for item in before["operations"] if item["kind"] == "director_check"]
    assert checks[0]["result"]["decision"] == "REVISE"
    assert checks[0]["director_revision"]["patches"][0]["selected"] is True
    check = checks[-1]
    assert check["status"] == "WAITING_HUMAN" and check["question_id"]
    codes = {issue["code"] for issue in check["director_revision"]["initial_check"]["issues"]}
    assert {"PERFORMANCE_MOTION_UNVERIFIED", "PERFORMANCE_MOCK_NOT_PRODUCTION"} <= codes
    assert "PERFORMANCE_CAPABILITY_UNVERIFIED" not in codes
    factory = client.app.state.session_factory
    with factory() as db:
        loop = db.get(AgentLoop, UUID(check["loop_id"]))
        question = db.get(AgentLoopQuestion, UUID(check["question_id"]))
        assert question is not None and question.loop_id == loop.id
        assert question.input_sha256 == loop.input_sha256 == check["input_sha256"]
        assert question.question_json == check["human_request"]
        assert not db.scalar(
            select(AgentLoopDecision.id).where(AgentLoopDecision.question_id == question.id)
        )
        question_snapshot = (question.id, copy.deepcopy(question.question_json))
        seed = PerformanceRevisionSeed.model_validate(loop.seed_json["messages"][0]["data"])
        assert seed.script.model_dump(mode="json") == source["content_json"]
        assert all(
            scene.dramatic_beat.conflict and scene.state_changes for scene in seed.script.scenes
        )
        assert seed.source.scenes[0].shots[0].notes
        assert seed.source.scenes[0].shots[0].performance.motion_requirement == "dynamic_required"
        snapshot = (loop.id, loop.input_sha256, copy.deepcopy(loop.seed_json))
        evidence_count = db.scalar(select(func.count()).select_from(AgentLoopAction))
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
    assert run_once(client, settings, run_id)["status"] == "RUNNING"
    after = progress(client, run_id)
    assert after["can_cancel"] is True
    assert [(item["loop_id"], item["question_id"]) for item in after["operations"]] == [
        (item["loop_id"], item["question_id"]) for item in before["operations"]
    ]
    with factory() as db:
        loop = db.get(AgentLoop, snapshot[0])
        assert (loop.id, loop.input_sha256, loop.seed_json) == snapshot
        assert db.scalar(select(func.count()).select_from(AgentLoopAction)) == evidence_count
        question = db.get(AgentLoopQuestion, question_snapshot[0])
        assert question.question_json == question_snapshot[1]
        assert not db.scalar(
            select(AgentLoopDecision.id).where(AgentLoopDecision.question_id == question.id)
        )
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
        assert db.get(WorkflowRun, run_id).input_json["director"]["performance"] is True
