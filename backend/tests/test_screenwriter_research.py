"""WRITE-01 uses real registry, evidence, authorization and persistence services."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, select
from test_agent_tools import activate_run, create_entry, create_run, execute, grant
from test_collaboration import create_episode, create_project, register
from test_concurrency import require_postgres
from test_context_trace import evidence, trace
from test_story_analysis import execute as analyze
from test_story_analysis import start as start_analysis
from test_story_outline import confirm, generate
from test_story_outline import save as save_outline
from test_story_source_revisions import revise, save_text, text_payload
from test_text_contract_v2 import legacy_script, make_asset, save_script

from arclight.application.agent_memory import read_agent_memory
from arclight.application.agent_tools import build_agent_tool_registry, execute_agent_tool
from arclight.domain.agent_tools import AgentToolCall
from arclight.domain.screenwriter_research import InvestigateStoryParameters
from arclight.domain.versions import digest
from arclight.main import create_app
from arclight.persistence.models import (
    AgentToolInvocation,
    AssetVersion,
    DependencyManifest,
    Episode,
    Project,
    StoryEntry,
    StorySource,
    User,
    VisualAsset,
    WorkflowRun,
)
from arclight.storage.objects import ObjectStorage


def ready(client, episode, settings):
    run_id = create_run(client, episode, key=uuid4().hex)
    actor = activate_run(client, run_id, episode["id"])
    registry = build_agent_tool_registry(settings=settings)
    grant(client, run_id, actor, registry, allowed=("investigate_story",))
    return run_id, actor, registry


def request_for(run_id, query, *, refresh=False, call_id=None):
    return AgentToolCall(
        run_id=run_id,
        tool_name="investigate_story",
        idempotency_key=call_id or uuid4().hex,
        arguments={"query": query, "refresh": refresh},
    )


def research(client, run_id, registry, query, **kwargs):
    feedback = execute(client, registry, request_for(run_id, query, **kwargs))
    assert feedback.status == "SUCCEEDED", feedback.model_dump_json()
    assert feedback.trust == "UNTRUSTED"
    return feedback.result


def exact(kind, identity, **patch):
    return {"path": "exact", "source_kind": kind, "source_id": str(identity), **patch}


def add_episode(client, project_id, number):
    response = client.post(
        f"/api/v1/projects/{project_id}/episodes",
        json={"episode_no": number, "title": f"第{number}集"},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": {"path": "sql", "query": "select * from users"}},
        {"query": {"path": "startup", "project_id": str(uuid4())}},
        {"query": {"path": "entries", "name": "角色", "limit": 9}},
        {"query": {"path": "entries", "name": "角色", "limit": True}},
        {"query": {"path": "neighbors", "offsets": [0]}},
        {"query": {"path": "neighbors", "offsets": [1, 1]}},
        {"query": {"path": "neighbors", "offsets": [3]}},
        {"query": exact("OUTLINE", uuid4(), revision=0)},
        {"query": exact("STORY_SOURCE", uuid4(), selection={"field": "text", "length": 12001})},
        {"query": exact("SCRIPT", uuid4(), selection={"field": "scenes", "length": 9})},
        {"query": exact("STORY_ENTRY", uuid4(), selection={"field": "text", "length": 4})},
        {"query": {"path": "startup"}, "refresh": "true"},
    ],
)
def test_queries_are_bounded_and_cannot_invent_scope(arguments):
    with pytest.raises(ValidationError):
        InvestigateStoryParameters.model_validate(arguments)


def test_startup_story_settings_and_origin_remain_fixed(client, episode, settings):
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).story_settings = {"target_episode_count": 3}
    run_id, actor, registry = ready(client, episode, settings)
    frozen = trace(client, run_id)
    first = research(client, run_id, registry, {"path": "startup"})
    material = first["materials"][0]
    assert material["content"]["input"]["story"] == "可供 Agent 工具测试的故事"
    assert material["content"]["input"]["story_settings"]["target_episode_count"] == 3
    assert material["bound_manifest_id"] == frozen["startup_manifest_id"]
    assert material["source"]["id"] == frozen["startup_manifest_id"]
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).story_settings = {"target_episode_count": 9}
    second = research(client, run_id, registry, {"path": "startup"}, refresh=True)
    assert second["materials"] == first["materials"]
    assert trace(client, run_id)["startup"] == frozen["startup"]
    with client.app.state.session_factory() as db:
        memory = read_agent_memory(db, db.get(WorkflowRun, run_id), "script", actor)
    assert memory["memory"]["evidence"]  # Original story is evidence, not confirmed facts.


def test_entry_startup_binding_survives_update_across_paths_and_explicit_refresh(
    client, episode, settings
):
    entry_id = create_entry(client, episode["project_id"], data={"rules": ["旧规则"]})
    run_id, _, registry = ready(client, episode, settings)
    original = trace(client, run_id)
    with client.app.state.session_factory.begin() as db:
        row = db.get(StoryEntry, entry_id)
        row.data_json = {"rules": ["新规则"]}
        row.revision += 1
    query = exact("STORY_ENTRY", entry_id)
    first = research(client, run_id, registry, query)
    assert first["materials"][0]["content"]["rules"] == ["旧规则"]
    assert first["materials"][0]["source"]["revision"] == 0
    search = research(client, run_id, registry, {"path": "entries", "name": "测试"})
    assert search["materials"] == first["materials"]
    fresh = research(client, run_id, registry, query, refresh=True)
    assert fresh["materials"][0]["content"]["rules"] == ["新规则"]
    assert fresh["materials"][0]["source"]["revision"] == 1
    historical = research(client, run_id, registry, {**query, "revision": 0}, refresh=True)
    assert historical["materials"] == first["materials"]
    unavailable = research(client, run_id, registry, {**query, "revision": 99})
    assert unavailable["materials"] == []
    assert unavailable["missing"][0]["reason"] == "REVISION_UNAVAILABLE"
    final = trace(client, run_id)
    assert final["startup"] == original["startup"]
    assert final["evidence"][0]["investigation"]["materials"] == first["materials"]
    assert (
        evidence(client, run_id, expected_round=5, story_entry_ids=[str(entry_id)]).status_code
        == 201
    )
    assert trace(client, run_id)["evidence"][-1]["story_entries"][0]["rules"] == ["新规则"]


def test_new_queries_reuse_generic_evidence_entry_revision(client, episode, settings):
    run_id, _, registry = ready(client, episode, settings)
    entry_id = create_entry(client, episode["project_id"], data={"description": "第一版"})
    assert evidence(client, run_id, story_entry_ids=[str(entry_id)]).status_code == 201
    with client.app.state.session_factory.begin() as db:
        row = db.get(StoryEntry, entry_id)
        row.data_json = {"description": "第二版"}
        row.revision += 1
    result = research(client, run_id, registry, exact("STORY_ENTRY", entry_id))
    assert result["materials"][0]["content"]["description"] == "第一版"
    assert (
        result["materials"][0]["bound_manifest_id"]
        == trace(client, run_id)["evidence"][0]["manifest_id"]
    )


def test_missing_search_requires_explicit_refresh_to_bind_new_material(client, episode, settings):
    run_id, _, registry = ready(client, episode, settings)
    query = {"path": "entries", "name": "测试"}
    first = research(client, run_id, registry, query)
    assert not first["materials"] and first["missing"][0]["reason"] == "NOT_FOUND"
    entry_id = create_entry(client, episode["project_id"])
    unchanged = research(client, run_id, registry, query)
    assert not unchanged["materials"] and unchanged["missing"] == first["missing"]
    refreshed = research(client, run_id, registry, query, refresh=True)
    assert refreshed["materials"][0]["source_id"] == str(entry_id)
    assert trace(client, run_id)["evidence"][0]["investigation"]["materials"] == []


def test_startup_source_catalog_binding_is_fixed_until_explicit_refresh(client, episode, settings):
    source = save_text(client, episode, text_payload("原精确输入"))
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        headers={"Idempotency-Key": "source-bound-research"},
        json={"source_id": source["id"]},
    )
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    actor = activate_run(client, run_id, episode["id"])
    registry = build_agent_tool_registry(settings=settings)
    grant(client, run_id, actor, registry, allowed=("investigate_story",))
    newer = save_text(client, episode, revise(client, episode, source, text="新精确输入"))
    query = {"path": "catalog", "source_kind": "STORY_SOURCE"}
    first = research(client, run_id, registry, query)
    assert first["materials"][0]["source_id"] == source["id"]
    assert first["materials"][0]["content"]["text"] == "原精确输入"
    second = research(client, run_id, registry, query, refresh=True)
    assert second["materials"][0]["source_id"] == newer["id"]


def test_entry_asset_dependencies_are_exact_and_rechecked_without_changing_history(
    client, episode, settings
):
    run_id, _, registry = ready(client, episode, settings)
    with client.app.state.session_factory.begin() as db:
        asset = make_asset(db, episode["project_id"], "CHARACTER")
    entry_id = create_entry(client, episode["project_id"], data={"asset": asset})
    query = exact("STORY_ENTRY", entry_id)
    first = research(client, run_id, registry, query)
    old_trace = trace(client, run_id)
    assert any(
        source.get("asset_version_id") == asset["asset_version_id"]
        for source in old_trace["evidence"][0]["sources"]
    )
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(asset["asset_version_id"])).stale = True
    missing = research(client, run_id, registry, query)
    assert missing["materials"] == [] and missing["missing"][0]["reason"] == "INPUT_NOT_USABLE"
    assert trace(client, run_id)["evidence"][0] == old_trace["evidence"][0]
    assert first["materials"][0]["content"]["asset"] == asset


def test_name_search_is_literal_bounded_and_explicitly_refreshes_match_set(
    client, episode, settings
):
    run_id, _, registry = ready(client, episode, settings)
    ids = [create_entry(client, episode["project_id"]) for _ in range(3)]
    with client.app.state.session_factory.begin() as db:
        for index, identity in enumerate(ids):
            entry = db.get(StoryEntry, identity)
            entry.name = f"船%_{index}"
            entry.kind = "CHARACTER"
    query = {"path": "entries", "name": "%_", "entry_kind": "CHARACTER", "limit": 2}
    first = research(client, run_id, registry, query)
    assert len(first["materials"]) == 2 and first["has_more"]
    assert all(item["content"]["kind"] == "CHARACTER" for item in first["materials"])
    with client.app.state.session_factory.begin() as db:
        db.get(StoryEntry, ids[0]).name = "改名后"
    bound = research(client, run_id, registry, query)
    assert bound["materials"] == first["materials"] and bound["reused_binding"]
    fresh = research(client, run_id, registry, query, refresh=True)
    assert len(fresh["materials"]) == 2 and not fresh["has_more"]
    assert {m["source_id"] for m in fresh["materials"]} == {str(ids[1]), str(ids[2])}


def test_source_exact_versions_rights_and_private_object_loss(client, episode, settings):
    first_source = save_text(client, episode, text_payload("纸船原稿"))
    run_id, _, registry = ready(client, episode, settings)
    result = research(client, run_id, registry, exact("STORY_SOURCE", first_source["id"]))
    material = result["materials"][0]
    assert material["content"] == {"text": "纸船原稿"}
    assert material["source"]["version_no"] == 1
    assert material["source"]["rights_status"] == "CONFIRMED"
    new_source = save_text(
        client, episode, revise(client, episode, first_source, text="第二版原稿")
    )
    same = research(client, run_id, registry, exact("STORY_SOURCE", first_source["id"]))
    assert same["materials"] == result["materials"]
    fresh = research(client, run_id, registry, exact("STORY_SOURCE", new_source["id"]))
    assert fresh["materials"][0]["source"]["version_no"] == 2
    assert fresh["materials"][0]["source"]["root_source_id"] == first_source["id"]
    before_loss = trace(client, run_id)
    with client.app.state.session_factory() as db:
        source = db.get(StorySource, UUID(first_source["id"]))
        ObjectStorage(settings).delete(source.object_key)
    lost = research(client, run_id, registry, exact("STORY_SOURCE", first_source["id"]))
    assert not lost["materials"] and lost["missing"][0]["reason"] == "SOURCE_UNAVAILABLE"
    assert trace(client, run_id)["evidence"][:3] == before_loss["evidence"]
    serialized = json.dumps(result)
    for private in ("object_key", "bucket", "_text_service", "raw_bytes"):
        assert private not in serialized


def test_large_source_has_explicit_pages_with_one_exact_origin_and_no_silent_trimming(
    client, episode, settings
):
    original = "上" * 12000 + "下" * 12000
    source = save_text(client, episode, text_payload(original))
    run_id, _, registry = ready(client, episode, settings)
    full = execute(client, registry, request_for(run_id, exact("STORY_SOURCE", source["id"])))
    assert full.error.code == "CONTEXT_TOO_LARGE"
    assert trace(client, run_id)["evidence"] == []
    pages = []
    for start in (0, 12000, 0):
        page = research(
            client,
            run_id,
            registry,
            exact(
                "STORY_SOURCE",
                source["id"],
                selection={
                    "field": "text",
                    "start": start,
                    "length": 12000,
                },
            ),
        )["materials"][0]
        assert page["source"]["sha256"] == source["sha256"]
        assert page["source_id"] == source["id"]
        assert page["content"]["selection"] == {
            "field": "text",
            "start": start,
            "end": start + 12000,
            "total": 24000,
        }
        pages.append(page)
    assert pages[0]["content"]["text"] + pages[1]["content"]["text"] == original
    assert pages[0] == pages[2]
    missing = research(
        client,
        run_id,
        registry,
        exact(
            "STORY_SOURCE",
            source["id"],
            selection={
                "field": "text",
                "start": 24000,
                "length": 1,
            },
        ),
    )
    assert missing["materials"] == [] and missing["missing"][0]["reason"] == "NOT_FOUND"


def test_structured_script_sections_keep_exact_full_source_provenance(client, episode, settings):
    second = add_episode(client, episode["project_id"], 2)
    content = legacy_script()
    content["scenes"].append(
        {**copy.deepcopy(content["scenes"][0]), "scene_no": 2, "title": "灯塔"}
    )
    script = save_script(client, second, content)
    run_id, _, registry = ready(client, episode, settings)
    first = research(
        client,
        run_id,
        registry,
        exact(
            "SCRIPT",
            script["id"],
            selection={
                "field": "scenes",
                "start": 0,
                "length": 1,
            },
        ),
    )["materials"][0]
    other = research(
        client,
        run_id,
        registry,
        exact(
            "SCRIPT",
            script["id"],
            selection={
                "field": "scenes",
                "start": 1,
                "length": 1,
            },
        ),
    )["materials"][0]
    assert first["source"] == other["source"]
    assert first["content"]["scenes"][0]["title"] == "街道"
    assert other["content"]["scenes"][0]["title"] == "灯塔"
    assert first["content"]["selection"]["total"] == 2
    full = research(client, run_id, registry, exact("SCRIPT", script["id"]))["materials"][0]
    assert len(full["content"]["scenes"]) == 2 and "selection" not in full["content"]


def test_actual_analysis_outline_catalog_and_unconfirmed_outline(client, episode, settings):
    source = save_text(client, episode)
    response = start_analysis(client, episode, {"source_id": source["id"]})
    assert response.status_code == 202, response.text
    analyze(client, settings, response.json()["run_id"])
    analysis = client.get(f"/api/v1/episodes/{episode['id']}/story-analyses").json()[0]
    outline = generate(client, episode, settings, {"analysis_id": analysis["id"]})
    draft_response = save_outline(client, episode, outline)
    assert draft_response.status_code == 201, draft_response.text
    draft = draft_response.json()
    assert confirm(client, episode, outline).status_code == 200
    run_id, actor, registry = ready(client, episode, settings)
    unconfirmed = research(client, run_id, registry, exact("OUTLINE", draft["id"]))
    assert unconfirmed["missing"][0]["reason"] == "NOT_CONFIRMED"
    for kind, row in (("OUTLINE", outline), ("ANALYSIS", analysis)):
        result = research(client, run_id, registry, {"path": "catalog", "source_kind": kind})
        assert result["materials"][0]["source_id"] == row["id"]
        assert result["materials"][0]["source"]["version_no"] == row["version_no"]
        assert result["materials"][0]["content_sha256"] == digest(result["materials"][0]["content"])
    catalog = research(
        client,
        run_id,
        registry,
        {"path": "catalog", "source_kind": "STORY_SOURCE", "limit": 8},
        refresh=True,
    )
    assert catalog["materials"] and catalog["materials"][0]["source_kind"] == "STORY_SOURCE"
    assert any(item["reason"] == "RIGHTS_UNCONFIRMED" for item in catalog["missing"])
    with client.app.state.session_factory() as db:
        memory = read_agent_memory(db, db.get(WorkflowRun, run_id), "script", actor)["memory"]
    assert any(item["confirmation_rule"] == "CONFIRMED_OUTLINE" for item in memory["domain_facts"])
    assert any(item["source_id"] == analysis["id"] for item in memory["evidence"])


def test_neighbors_use_exact_approved_scripts_and_keep_bound_version(client, episode, settings):
    second = add_episode(client, episode["project_id"], 2)
    old = save_script(client, second)
    run_id, _, registry = ready(client, episode, settings)
    query = {"path": "neighbors", "offsets": [-1, 1, 2]}
    first = research(client, run_id, registry, query)
    assert first["materials"][0]["source_id"] == old["id"]
    assert first["materials"][0]["source"]["kind"] == "ASSET_VERSION"
    assert len(first["missing"]) == 2 and all(
        item["reason"] == "NOT_FOUND" for item in first["missing"]
    )
    path = f"/api/v1/episodes/{second['id']}"
    saved = client.post(
        path + "/scripts",
        json={
            "expected_version_no": 1,
            "content": {**first["materials"][0]["content"], "title": "新版"},
        },
    )
    assert saved.status_code == 201, saved.text
    new = saved.json()
    reviewed = client.post(path + f"/scripts/{new['id']}/review", json={"decision": "APPROVE"})
    assert reviewed.status_code == 200, reviewed.text
    same = research(client, run_id, registry, query)
    assert same["materials"] == first["materials"]
    narrowed = research(client, run_id, registry, {"path": "neighbors", "offsets": [1]})
    assert narrowed["materials"] == first["materials"]
    fresh = research(client, run_id, registry, query, refresh=True)
    assert fresh["materials"][0]["source_id"] == new["id"]
    assert trace(client, run_id)["evidence"][0]["investigation"]["materials"] == first["materials"]


@pytest.mark.parametrize("failure", ["unapproved", "archived_episode", "stale", "archived_asset"])
def test_unusable_neighbors_are_missing_and_do_not_leak_content(client, episode, settings, failure):
    second = add_episode(client, episode["project_id"], 2)
    script = save_script(client, second, approve=failure != "unapproved")
    run_id, _, registry = ready(client, episode, settings)
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, UUID(script["id"]))
        if failure == "archived_episode":
            db.get(Episode, UUID(second["id"])).status = "ARCHIVED"
        elif failure == "stale":
            version.stale = True
        elif failure == "archived_asset":
            db.get(VisualAsset, version.asset_id).status = "ARCHIVED"
    result = research(client, run_id, registry, {"path": "neighbors", "offsets": [1]})
    assert result["materials"] == [] and len(result["missing"]) == 1
    assert (
        result["missing"][0]["reason"]
        == {
            "unapproved": "NOT_CONFIRMED",
            "archived_episode": "ARCHIVED",
            "stale": "INPUT_NOT_USABLE",
            "archived_asset": "INPUT_NOT_USABLE",
        }[failure]
    )
    assert "雨夜" not in json.dumps(result, ensure_ascii=False)


def test_foreign_and_unknown_id_have_same_missing_result_and_search_is_project_scoped(
    client, episode, settings
):
    foreign_project = create_project(client)
    foreign_episode = create_episode(client, foreign_project)
    foreign_entry = create_entry(client, foreign_project["id"], data={"description": "不得泄漏"})
    foreign_source = save_text(client, foreign_episode, text_payload("不得泄漏原稿"))
    foreign_script = save_script(client, foreign_episode)
    foreign_outline = generate(client, foreign_episode, settings)
    run_id, _, registry = ready(client, episode, settings)
    for kind, identity in (
        ("STORY_ENTRY", foreign_entry),
        ("STORY_SOURCE", foreign_source["id"]),
        ("SCRIPT", foreign_script["id"]),
        ("OUTLINE", foreign_outline["id"]),
    ):
        result = research(client, run_id, registry, exact(kind, identity))
        assert result["materials"] == [] and result["missing"][0]["reason"] == "NOT_FOUND"
        unknown = research(client, run_id, registry, exact(kind, uuid4()))
        assert unknown["materials"] == [] and unknown["missing"][0]["reason"] == "NOT_FOUND"
    result = research(client, run_id, registry, {"path": "entries", "name": "测试"})
    assert result["materials"] == []
    assert "不得泄漏" not in json.dumps(trace(client, run_id), ensure_ascii=False)


def test_archived_entry_missing_and_tool_idempotency_survive_restart(client, episode, settings):
    run_id, _, registry = ready(client, episode, settings)
    entry_id = create_entry(client, episode["project_id"])
    request = request_for(run_id, exact("STORY_ENTRY", entry_id), call_id="one-research")
    first = execute(client, registry, request)
    assert first.status == "SUCCEEDED"
    frozen = trace(client, run_id)
    with client.app.state.session_factory.begin() as db:
        db.get(StoryEntry, entry_id).archived = True
    with TestClient(create_app(settings)) as restarted:
        restarted.headers["Authorization"] = "Bearer test-token"
        replay = execute(restarted, build_agent_tool_registry(settings=settings), request)
        assert replay == first
        assert trace(restarted, run_id) == frozen
        missing = research(restarted, run_id, registry, exact("STORY_ENTRY", entry_id))
        assert missing["materials"] == [] and missing["missing"][0]["reason"] == "ARCHIVED"
    assert trace(client, run_id)["evidence"][0] == frozen["evidence"][0]
    conflict = execute(
        client, registry, request.model_copy(update={"arguments": {"query": {"path": "startup"}}})
    )
    assert conflict.error.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("revoke", ["grant", "project", "disabled"])
def test_current_authority_is_required_even_for_tool_replay(client, episode, settings, revoke):
    if revoke == "disabled":
        user = register(client)
        project = create_project(client)
        episode = create_episode(client, project)
    run_id, actor, registry = ready(client, episode, settings)
    request = request_for(run_id, {"path": "startup"})
    assert execute(client, registry, request).status == "SUCCEEDED"
    before = trace(client, run_id)
    if revoke == "grant":
        grant(client, run_id, actor, registry, allowed=())
    else:
        with client.app.state.session_factory.begin() as db:
            if revoke == "disabled":
                db.get(User, UUID(user["user"]["id"])).disabled = True
            else:
                db.get(Project, UUID(episode["project_id"])).owner_id = str(uuid4())
    result = execute(client, registry, request)
    assert result.status == "FAILED"
    with client.app.state.session_factory() as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(AgentToolInvocation).where(AgentToolInvocation.run_id == run_id)
                    )
                )
            )
            == 1
        )
    if revoke == "grant":
        assert trace(client, run_id) == before


@pytest.mark.parametrize("failure", ["large", "invalid_result", "sensitive"])
def test_failed_investigation_rolls_back_round_and_bindings(client, episode, settings, failure):
    run_id, _, registry = ready(client, episode, settings)
    entry_id = create_entry(client, episode["project_id"], data={"description": "初版"})
    before = trace(client, run_id)
    if failure == "invalid_result":
        original = registry.get("investigate_story")

        def invalid(context, parameters):
            result = original.handler(context, parameters)
            return {**result, "extra": True}

        registry._tools["investigate_story"] = replace(original, handler=invalid)
    else:
        with client.app.state.session_factory.begin() as db:
            db.get(StoryEntry, entry_id).data_json = (
                {"description": "长" * 30000}
                if failure == "large"
                else {"credentials": "forbidden"}
            )
    result = execute(client, registry, request_for(run_id, exact("STORY_ENTRY", entry_id)))
    assert result.status == "FAILED"
    assert (
        result.error.code
        == {
            "large": "CONTEXT_TOO_LARGE",
            "invalid_result": "INVALID_TOOL_RESULT",
            "sensitive": "CONTEXT_INPUT_UNSAFE",
        }[failure]
    )
    assert trace(client, run_id) == before
    with client.app.state.session_factory.begin() as db:
        db.get(StoryEntry, entry_id).data_json = {"description": "修正后"}
    good = research(
        client, run_id, build_agent_tool_registry(settings=settings), exact("STORY_ENTRY", entry_id)
    )
    assert (
        good["evidence_round"] == 1 and good["materials"][0]["content"]["description"] == "修正后"
    )


def test_professional_tool_is_not_available_to_other_agents_or_legacy_runs(
    client, episode, settings
):
    run_id, _, registry = ready(client, episode, settings)
    request = request_for(run_id, {"path": "startup"})
    other = execute(client, registry, request, agent="director")
    assert other.status == "FAILED" and other.error.code == "USER_NOT_AUTHORIZED"
    assert trace(client, run_id)["evidence"] == []
    tool = registry.get("investigate_story")
    assert tool.allowed_steps == frozenset({"outline", "script", "rewrite"})
    assert build_agent_tool_registry().get("investigate_story") is None


def test_legacy_run_cannot_fabricate_startup_or_research_history(client, episode, settings):
    def legacy(_mapper, _connection, row):
        if row.snapshot_json["context"].get("origin") == "context_builder":
            value = copy.deepcopy(row.snapshot_json)
            value["context"].pop("trace")
            row.snapshot_json = value
            row.request_fingerprint = digest(value)

    event.listen(DependencyManifest, "before_insert", legacy)
    try:
        run_id, _, registry = ready(client, episode, settings)
    finally:
        event.remove(DependencyManifest, "before_insert", legacy)
    before = trace(client, run_id)
    result = execute(client, registry, request_for(run_id, {"path": "startup"}))
    assert result.error.code == "CONTEXT_TRACE_UNAVAILABLE"
    assert trace(client, run_id) == before and not before["trace_available"]


def test_postgres_duplicate_investigations_append_one_round(client, episode, settings):
    require_postgres(settings)
    run_id, _, registry = ready(client, episode, settings)
    entry_id = create_entry(client, episode["project_id"])
    request = request_for(run_id, exact("STORY_ENTRY", entry_id), call_id="concurrent")
    barrier = Barrier(2)

    def worker():
        with client.app.state.session_factory.begin() as db:
            barrier.wait(timeout=10)
            return execute_agent_tool(db, registry, "screenwriter", request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert results[0] == results[1] and results[0].status == "SUCCEEDED"
    assert len(trace(client, run_id)["evidence"]) == 1
