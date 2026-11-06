"""AART-01 uses actual tool grants, domain APIs and immutable Context manifests."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from test_art_review_workflow import decide, save, submit
from test_asset_versions import image_bytes
from test_asset_versions import save as save_image
from test_asset_versions import state as image_state
from test_assets import create as create_asset
from test_assets import path as asset_path
from test_assets import rename
from test_contexts import visual_setup
from test_director_planning import prepare
from test_visual_bible import edit

from arclight.application.agent_tools import (
    build_agent_tool_registry,
    execute_agent_tool,
    grant_agent_tools,
)
from arclight.application.context_evidence import read_trace
from arclight.domain.agent_model import AgentModelCapabilities
from arclight.domain.agent_tools import AgentToolCall, AgentToolGrantRequest
from arclight.domain.art_research import InvestigateArtParameters
from arclight.domain.versions import digest
from arclight.persistence.art_proposals import ArtApprovedVersion, ArtProposalVersion
from arclight.persistence.models import AssetVersion, DependencyManifest, WorkflowRun
from arclight.providers.media_capabilities import MOCK_CAPABILITIES, resolve_capabilities

UNKNOWN = "00000000-0000-4000-8000-000000000001"


def prepare_art(client, episode, settings, source=None):
    run_id, actor, source = prepare(client, episode, settings, source=source)
    with client.app.state.session_factory.begin() as db:
        grant_agent_tools(
            db,
            AgentToolGrantRequest(
                run_id=run_id, agent_name="art_director", allowed_tools=["investigate_art"]
            ),
            actor,
            build_agent_tool_registry(settings=settings),
        )
    return run_id, actor, source


def query(client, settings, run_id, value, *, refresh=False, key=None, agent="art_director"):
    with client.app.state.session_factory.begin() as db:
        return execute_agent_tool(
            db,
            build_agent_tool_registry(settings=settings),
            agent,
            AgentToolCall(
                run_id=run_id,
                tool_name="investigate_art",
                idempotency_key=key or uuid4().hex,
                arguments={"query": value, "refresh": refresh},
            ),
        )


def trace(client, run_id, actor):
    with client.app.state.session_factory() as db:
        return read_trace(db, db.get(WorkflowRun, run_id), "storyboard", actor)


def contents(feedback):
    assert feedback.status == "SUCCEEDED", feedback.model_dump_json()
    return [item["content"] for item in feedback.result["materials"]]


def add_image(client, asset, *, color="blue", approve=True):
    asset = client.get(asset_path(asset["project_id"]) + "/" + asset["id"]).json()
    import base64

    version = save_image(
        client, asset, content_base64=base64.b64encode(image_bytes(color=color)).decode()
    )
    if approve:
        ready = image_state(client, asset, version, "READY_FOR_REVIEW").json()
        response = image_state(client, asset, ready, "APPROVED")
        assert response.status_code == 200, response.text
        version = response.json()
    return version


@pytest.mark.parametrize(
    "value",
    [
        {"path": "directories", "asset_ids": [UNKNOWN, UNKNOWN]},
        {"path": "references", "version_ids": [UNKNOWN] * 9},
        {"path": "references", "version_ids": [UNKNOWN], "include_images": 1},
        {"path": "authorization", "version_ids": []},
        {"path": "catalog", "asset_type": "SCENE", "name": "河", "limit": True},
        {"path": "catalog", "asset_type": "SCENE", "name": "河", "offset": -1},
        {"path": "catalog", "asset_type": "SCRIPT", "name": "河"},
        {"path": "visual", "project_id": UNKNOWN},
        {"path": "model", "max_input_images": 8},
        {"path": "capabilities", "provider": "mock", "model": "x", "url": "https://example.com"},
        {"path": "shell", "command": "whoami"},
    ],
)
def test_art_queries_cannot_invent_authority_or_unbounded_selections(value):
    with pytest.raises(ValidationError):
        InvestigateArtParameters.model_validate({"query": value})


@pytest.mark.parametrize("kind", ["CHARACTER", "SCENE", "PROP", "STYLE"])
def test_formally_approved_setting_without_image_is_discoverable(client, episode, settings, kind):
    saved, _ = save(client, episode, kind)
    approved = decide(client, submit(client, saved).json()).json()
    output = approved["outputs"][0]
    run_id, actor, _ = prepare_art(client, episode, settings)
    feedback = query(
        client, settings, run_id, {"path": "directories", "asset_ids": [output["asset_id"]]}
    )
    content = contents(feedback)[0]
    assert content["directory"]["current_version_id"] is None
    assert content["directory"]["name"] == "雨夜角色"
    assert content["baseline"]["asset_id"] == output["asset_id"]
    assert content["approved_design"]["version_id"] == output["id"]
    assert content["approved_design"]["matches_directory"]
    assert {item["reason"] for item in feedback.result["missing"]} == {
        "NO_IMAGE",
        "NO_SELECTED_VERSION",
    }
    assert feedback.result["image_file_ids"] == []
    catalog = query(
        client, settings, run_id, {"path": "catalog", "asset_type": kind, "name": "雨夜"}
    )
    assert contents(catalog) == [content] and catalog.result["reused_binding"]
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AssetVersion)
                .where(AssetVersion.asset_id == UUID(output["asset_id"]))
            )
            == 0
        )
        assert db.scalar(select(func.count()).select_from(ArtApprovedVersion)) == 1
        assert db.scalar(select(func.count()).select_from(ArtProposalVersion)) == 1
    assert (
        trace(client, run_id, actor)["evidence"][-1]["art_investigation"]["result"]
        == catalog.result
    )


def test_full_multi_angle_index_keeps_fixed_features_and_explicit_standard(
    client, episode, settings
):
    asset = create_asset(client, episode["project_id"])
    versions = [add_image(client, asset, color=color) for color in ("blue", "red", "green")]
    images = [
        {
            "id": str(uuid4()),
            "label": label,
            "kind": "INITIAL" if index == 0 else "ANGLE",
            "angle": "" if index == 0 else label,
            "source": {"asset_id": asset["id"], "asset_version_id": version["id"]},
        }
        for index, (label, version) in enumerate(
            zip(["正面", "侧面", "背面"], versions, strict=True)
        )
    ]
    asset = client.get(asset_path(asset["project_id"]) + "/" + asset["id"]).json()
    profile = {
        "fixed_features": ["银发", "圆眼镜"],
        "variable_features": ["姿势"],
        "images": images,
        "standard_image_id": images[0]["id"],
    }
    response = client.put(
        asset_path(asset["project_id"]) + "/" + asset["id"] + "/character-profile",
        json={"expected_revision": asset["revision"], "profile": profile},
    )
    assert response.status_code == 200, response.text
    run_id, _, _ = prepare_art(client, episode, settings)
    result = query(client, settings, run_id, {"path": "directories", "asset_ids": [asset["id"]]})
    content = contents(result)[0]
    assert content["directory"]["character_profile"]["images"] == images
    assert content["directory"]["character_profile"]["fixed_features"] == ["银发", "圆眼镜"]
    assert [item["is_standard"] for item in content["image_candidates"]] == [True, False, False]
    assert result.result["image_file_ids"] == []
    refs = query(
        client, settings, run_id, {"path": "references", "version_ids": [v["id"] for v in versions]}
    )
    assert refs.status == "SUCCEEDED", refs.model_dump_json()
    assert len(refs.result["image_file_ids"]) == 3 and not refs.result["missing"]


def test_catalog_escapes_literal_search_and_exposes_pagination_without_selecting_images(
    client, episode, settings
):
    assets = [
        create_asset(client, episode["project_id"], "PROP", name=f"钥匙%_{i}") for i in range(3)
    ]
    create_asset(client, episode["project_id"], "PROP", name="钥匙XX")
    run_id, _, _ = prepare_art(client, episode, settings)
    first = query(
        client,
        settings,
        run_id,
        {"path": "catalog", "asset_type": "PROP", "name": "%_", "limit": 2},
    )
    second = query(
        client,
        settings,
        run_id,
        {"path": "catalog", "asset_type": "PROP", "name": "%_", "limit": 2, "offset": 2},
    )
    assert first.result["has_more"] and not second.result["has_more"]
    assert [item["directory"]["asset_id"] for item in contents(first) + contents(second)] == [
        a["id"] for a in assets
    ]


def test_directory_and_missing_bindings_freeze_across_queries_and_refresh_only_appends(
    client, episode, settings
):
    run_id, actor, _ = prepare_art(client, episode, settings)
    selection = {"path": "catalog", "asset_type": "SCENE", "name": "雨城"}
    missing = query(client, settings, run_id, selection)
    assert contents(missing) == [] and missing.result["missing"][0]["reason"] == "NOT_FOUND"
    asset = create_asset(client, episode["project_id"], "SCENE", name="雨城")
    assert query(client, settings, run_id, selection).result["missing"] == missing.result["missing"]
    fresh = query(client, settings, run_id, selection, refresh=True)
    original = contents(fresh)[0]
    assert rename(client, asset, "雨城新名").status_code == 200
    exact = query(client, settings, run_id, {"path": "directories", "asset_ids": [asset["id"]]})
    assert contents(exact) == [original] and exact.result["reused_binding"]
    latest = query(
        client, settings, run_id, {"path": "directories", "asset_ids": [asset["id"]]}, refresh=True
    )
    assert contents(latest)[0]["directory"]["name"] == "雨城新名"
    assert (
        trace(client, run_id, actor)["evidence"][0]["art_investigation"]["result"] == missing.result
    )
    with client.app.state.session_factory() as db:
        saved = db.get(DependencyManifest, UUID(fresh.result["manifest_id"]))
        assert saved.snapshot_json["context"]["art_investigation"]["result"] == fresh.result


def test_complete_frozen_bible_and_locked_constraints_survive_updates_and_refresh(
    client, episode, settings
):
    project = visual_setup(client, episode)
    run_id, actor, _ = prepare_art(client, episode, settings)
    initial = trace(client, run_id, actor)["task_context"]["required"]
    first = query(client, settings, run_id, {"path": "visual"})
    assert contents(first) == [{"global": initial["global"], "constraints": initial["constraints"]}]
    assert contents(first)[0]["constraints"]["locked_visual_fields"] == {
        "art_style": "水彩",
        "prohibited": "禁止血腥",
    }
    assert edit(client, project, expected=2, palette="暖红").status_code == 201
    settings.context_limits = settings.context_limits.model_copy(update={"max_bytes": 1024})
    assert contents(query(client, settings, run_id, {"path": "visual"}, refresh=True)) == contents(
        first
    )


@pytest.mark.parametrize(
    "images,tools,reason",
    [
        (0, True, "MODEL_CAPABILITY_INSUFFICIENT"),
        (1, True, "MODEL_CAPABILITY_INSUFFICIENT"),
        (2, False, "MODEL_CAPABILITY_INSUFFICIENT"),
        (4, True, None),
    ],
)
def test_exact_model_declaration_is_checked_and_frozen_with_run(
    client, episode, settings, images, tools, reason
):
    settings.llm_agent_capabilities = AgentModelCapabilities(
        version="art-model:1",
        supplier=settings.llm_supplier,
        model=settings.llm_model,
        tools=tools,
        response_formats=frozenset({"json_schema"}),
        max_input_images=images,
    )
    run_id, _, _ = prepare_art(client, episode, settings)
    before = query(client, settings, run_id, {"path": "model"})
    assert contents(before)[0]["declaration"]["version"] == "art-model:1"
    assert [item["reason"] for item in before.result["missing"]] == ([reason] if reason else [])
    settings.llm_agent_capabilities = settings.llm_agent_capabilities.model_copy(
        update={"version": "art-model:2", "max_input_images": 8}
    )
    assert contents(query(client, settings, run_id, {"path": "model"}, refresh=True)) == contents(
        before
    )


def test_undeclared_model_and_media_capability_changes_are_explicit_frozen_observations(
    client, episode, settings, monkeypatch
):
    run_id, actor, _ = prepare_art(client, episode, settings)
    absent = query(client, settings, run_id, {"path": "model"})
    assert (
        contents(absent) == []
        and absent.result["missing"][0]["reason"] == "MODEL_CAPABILITY_UNDECLARED"
    )
    selector = {"path": "capabilities", "provider": "mock", "model": "mock-image"}
    first = query(client, settings, run_id, selector)
    declaration = contents(first)[0]["declaration"]
    assert declaration["task_types"] == sorted(declaration["task_types"])
    for key in (
        "max_reference_images",
        "supports_seed",
        "sizes",
        "max_batch_size",
        "supports_query",
        "supports_cancel",
        "version",
    ):
        assert key in declaration
    changed = MOCK_CAPABILITIES.model_copy(
        update={"version": "mock:art2", "max_reference_images": 0}
    )
    monkeypatch.setattr(
        "arclight.application.art_research.resolve_capabilities",
        lambda provider, model: (
            changed if provider == "mock" else resolve_capabilities(provider, model)
        ),
    )
    assert contents(query(client, settings, run_id, selector)) == contents(first)
    assert (
        contents(query(client, settings, run_id, selector, refresh=True))[0]["declaration"][
            "version"
        ]
        == "mock:art2"
    )
    unknown = query(
        client, settings, run_id, {"path": "capabilities", "provider": "absent", "model": "x"}
    )
    assert (
        contents(unknown) == []
        and unknown.result["missing"][0]["reason"] == "PROVIDER_NOT_INSTALLED"
    )
    assert (
        trace(client, run_id, actor)["evidence"][1]["art_investigation"]["result"] == first.result
    )


def test_oversized_investigation_rolls_back_instead_of_trimming_required_profiles(
    client, episode, settings
):
    assets = []
    for index in range(8):
        asset = create_asset(client, episode["project_id"], "SCENE", name=f"大场景{index}")
        response = client.put(
            asset_path(asset["project_id"]) + "/" + asset["id"] + "/scene-profile",
            json={
                "expected_revision": asset["revision"],
                "profile": {
                    "layout": "室内" * 2000,
                    "lighting": "夜色" * 1000,
                    "environment": "雨夜" * 1000,
                },
            },
        )
        assert response.status_code == 200, response.text
        assets.append(asset)
    run_id, actor, _ = prepare_art(client, episode, settings)
    before = deepcopy(trace(client, run_id, actor))
    feedback = query(
        client, settings, run_id, {"path": "directories", "asset_ids": [a["id"] for a in assets]}
    )
    assert feedback.status == "FAILED" and feedback.error.code == "CONTEXT_TOO_LARGE"
    assert trace(client, run_id, actor) == before


def test_tool_idempotency_returns_original_manifest_and_rejects_changed_request(
    client, episode, settings
):
    run_id, actor, _ = prepare_art(client, episode, settings)
    with client.app.state.session_factory() as db:
        frozen_input = digest(db.get(WorkflowRun, run_id).input_json)
    first = query(client, settings, run_id, {"path": "visual"}, key="one-art-call")
    assert query(client, settings, run_id, {"path": "visual"}, key="one-art-call") == first
    changed = query(client, settings, run_id, {"path": "model"}, key="one-art-call")
    assert changed.status == "FAILED" and changed.error.code == "IDEMPOTENCY_CONFLICT"
    assert len(trace(client, run_id, actor)["evidence"]) == 1
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.pending_review is None and run.status == "RUNNING"
        assert digest(run.input_json) == frozen_input
