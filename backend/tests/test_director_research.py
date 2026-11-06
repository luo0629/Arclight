"""Actual tool authorization, frozen production sources and bounded feedback."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from test_collaboration import create_project, register
from test_contexts import visual_setup
from test_director_planning import prepare
from test_text_contract_v2 import make_asset, save_script
from test_visual_bible import edit

from arclight.application.agent_tools import build_agent_tool_registry, execute_agent_tool
from arclight.application.context_evidence import read_trace
from arclight.domain.agent_tools import AgentToolCall
from arclight.domain.director_planning import InvestigateProductionParameters
from arclight.persistence.models import AssetVersion, DependencyManifest, VisualAsset, WorkflowRun
from arclight.providers.media_capabilities import MOCK_CAPABILITIES


def query(client, settings, run_id, value, *, refresh=False, key=None):
    with client.app.state.session_factory.begin() as db:
        return execute_agent_tool(
            db,
            build_agent_tool_registry(settings=settings),
            "director",
            AgentToolCall(
                run_id=run_id,
                tool_name="investigate_production",
                idempotency_key=key or uuid4().hex,
                arguments={"query": value, "refresh": refresh},
            ),
        )


def materials(result):
    return [{k: v for k, v in item.items() if k != "manifest_id"} for item in result["materials"]]


@pytest.mark.parametrize(
    "value",
    [
        {"path": "script", "version_id": str(uuid4())},
        {"path": "catalog", "asset_type": "SCENE", "name": "河边", "limit": True},
        {"path": "assets", "version_ids": [str(uuid4())] * 9},
        {
            "path": "capabilities",
            "provider": "mock",
            "model": "mock-image",
            "url": "https://example.com",
        },
        {"path": "shell", "command": "whoami"},
    ],
)
def test_investigation_contract_cannot_invent_scope_or_execution(value):
    with pytest.raises(ValidationError):
        InvestigateProductionParameters.model_validate({"query": value})


def test_visual_constraints_and_capabilities_freeze_and_explicit_refresh_preserves_history(
    client, episode, settings, monkeypatch
):
    project = visual_setup(client, episode)
    run_id, actor, _ = prepare(client, episode, settings)
    visual = query(client, settings, run_id, {"path": "visual"})
    assert visual.status == "SUCCEEDED", visual.model_dump_json()
    content = visual.result["materials"][0]["content"]
    assert content["constraints"]["locked_visual_fields"] == {
        "art_style": "水彩",
        "prohibited": "禁止血腥",
    }
    capability_query = {"path": "capabilities", "provider": "mock", "model": "mock-image"}
    old = query(client, settings, run_id, capability_query)
    assert old.status == "SUCCEEDED"
    assert edit(client, project, expected=2, palette="暖红").status_code == 201
    changed = MOCK_CAPABILITIES.model_copy(
        update={"version": "mock:changed", "supports_seed": False}
    )
    monkeypatch.setattr(
        "arclight.application.director_research.resolve_capabilities", lambda *args: changed
    )
    reused = query(client, settings, run_id, capability_query)
    fresh = query(client, settings, run_id, capability_query, refresh=True)
    assert materials(reused.result) == materials(old.result)
    assert reused.result["reused_binding"]
    assert fresh.result["materials"][0]["content"]["declaration"]["version"] == "mock:changed"
    assert (
        query(client, settings, run_id, {"path": "visual"}, refresh=True).result["materials"][0][
            "content"
        ]
        == content
    )
    with client.app.state.session_factory() as db:
        trace = read_trace(db, db.get(WorkflowRun, run_id), "storyboard", actor)
        saved = next(
            row for row in trace["evidence"] if row["manifest_id"] == old.result["manifest_id"]
        )
        assert saved["director_investigation"]["result"] == old.result


def test_asset_exact_catalog_freezing_and_current_stale_rejection(client, episode, settings):
    with client.app.state.session_factory.begin() as db:
        ref = make_asset(db, episode["project_id"], "SCENE")
        asset = db.get(VisualAsset, UUID(ref["asset_id"]))
        asset.current_version_id = UUID(ref["asset_version_id"])
        asset.name = "河%_边"
    run_id, actor, _ = prepare(client, episode, settings)
    catalog = {"path": "catalog", "asset_type": "SCENE", "name": "%_"}
    first = query(client, settings, run_id, catalog)
    assert first.status == "SUCCEEDED", first.model_dump_json()
    assert first.result["materials"][0]["source_id"] == ref["asset_version_id"]
    with client.app.state.session_factory.begin() as db:
        db.get(VisualAsset, UUID(ref["asset_id"])).name = "新的目录名称"
    exact = query(
        client, settings, run_id, {"path": "assets", "version_ids": [ref["asset_version_id"]]}
    )
    assert exact.result["materials"][0]["content"]["name"] == "河%_边"
    assert materials(query(client, settings, run_id, catalog).result) == materials(first.result)
    fresh = query(client, settings, run_id, catalog, refresh=True)
    assert fresh.result["missing"][0]["reason"] == "NOT_FOUND"
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(ref["asset_version_id"])).stale = True
    stale = query(
        client,
        settings,
        run_id,
        {"path": "assets", "version_ids": [ref["asset_version_id"]]},
        refresh=True,
    )
    assert (
        stale.result["materials"] == []
        and stale.result["missing"][0]["reason"] == "INPUT_NOT_USABLE"
    )
    with client.app.state.session_factory() as db:
        saved = db.get(DependencyManifest, UUID(first.result["manifest_id"]))
        assert saved.snapshot_json["context"]["director_investigation"]["result"] == first.result


def test_foreign_assets_are_denied_and_unknown_provider_is_explicit_missing(
    client, episode, settings
):
    original_authorization = client.headers["Authorization"]
    register(client, "foreign-director@example.com")
    foreign = create_project(client)
    with client.app.state.session_factory.begin() as db:
        ref = make_asset(db, foreign["id"], "CHARACTER")
    # Original fixture uses the explicit service-owner principal.
    client.headers["Authorization"] = original_authorization
    run_id, _, _ = prepare(client, episode, settings)
    denied = query(
        client, settings, run_id, {"path": "assets", "version_ids": [ref["asset_version_id"]]}
    )
    assert denied.status == "FAILED" and denied.error.code == "REFERENCE_SCOPE_INVALID"
    unknown = query(
        client,
        settings,
        run_id,
        {"path": "capabilities", "provider": "uninstalled", "model": "unknown"},
    )
    assert unknown.status == "SUCCEEDED" and unknown.result["materials"] == []
    assert unknown.result["missing"][0]["reason"] == "PROVIDER_NOT_INSTALLED"


def test_exact_script_pages_and_tool_replay_are_immutable(client, episode, settings):
    source = save_script(client, episode)
    run_id, _, _ = prepare(client, episode, settings, source=source)
    scene_id = source["content_json"]["scenes"][0]["scene_id"]
    value = {"path": "script", "scene_ids": [scene_id]}
    key = uuid4().hex
    first = query(client, settings, run_id, value, key=key)
    assert first.status == "SUCCEEDED", first.model_dump_json()
    with client.app.state.session_factory() as db:
        before = db.scalar(select(func.count()).select_from(DependencyManifest))
    assert query(client, settings, run_id, value, key=key) == first
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(DependencyManifest)) == before
    assert first.result["materials"][0]["content"]["selection"] == [scene_id]
    bad = deepcopy(value)
    bad["scene_ids"] = [str(uuid4())]
    denied = query(client, settings, run_id, bad)
    assert denied.status == "FAILED" and denied.error.code == "DIRECTOR_PLAN_SCOPE"
