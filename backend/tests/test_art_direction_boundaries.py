"""Evidence admission, selected locks and deadline boundaries of the art adapter."""

from datetime import timedelta
from uuid import UUID

import pytest
from art_direction_support import install_direction_model, prepare_direction, reference_set
from test_agent_model import model_service as model_service
from test_agent_runtime import tool_feedback
from test_art_direction import facts
from test_asset_locks import lock, selected
from test_assets import create as create_asset
from test_collaboration import create_project

from arclight.application.art_direction import propose_art
from arclight.persistence.base import utcnow


def test_metadata_only_reference_reads_do_not_count_as_actual_image_observation(
    client, episode, settings, model_service
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    sent, errors, _ = install_direction_model(model_service, include_images=False)
    before = facts(client)
    result = propose_art(client.app.state.session_factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.status == "HUMAN_REVIEW", result.model_dump_json()
    assert tool_feedback(sent[-1])[-1]["error"]["code"] == "ART_EVIDENCE_REQUIRED"
    assert not any(
        isinstance(message.get("content"), list)
        for payload in sent
        for message in payload["messages"]
    )
    assert facts(client) == before


@pytest.mark.parametrize("kind", ["VISUAL", "MODEL"])
def test_delivery_cannot_omit_required_investigation_even_when_startup_data_exists(
    client, episode, settings, model_service, kind
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)

    def omit(draft):
        removed = {item["manifest_id"] for item in draft["evidence"] if item["source_kind"] == kind}
        draft["evidence"] = [item for item in draft["evidence"] if item["source_kind"] != kind]
        for assessment in draft["assessments"]:
            assessment["evidence_manifest_ids"] = [
                value for value in assessment["evidence_manifest_ids"] if value not in removed
            ]

    sent, errors, _ = install_direction_model(model_service, mutate_draft=omit)
    result = propose_art(client.app.state.session_factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.status == "HUMAN_REVIEW"
    assert tool_feedback(sent[-1])[-1]["error"]["code"] == "ART_EVIDENCE_REQUIRED"


def test_selected_locked_asset_changes_are_compared_without_unlock_or_formal_write(
    client, episode, settings, model_service
):
    asset, version = selected(client, episode["project_id"])
    locked = lock(client, asset, version)
    assert locked.status_code == 200, locked.text
    run_id, actor, _, task = prepare_direction(
        client, episode, settings, target_ids=[asset["id"]], minimum=1
    )
    sent, errors, _ = install_direction_model(model_service, existing=True)
    before = facts(client)
    result = propose_art(client.app.state.session_factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.status == "COMPLETED", result.model_dump_json()
    report = tool_feedback(sent[-1])[-1]["result"]["comparison"]
    assert all(
        any(change["protection"] == "LOCKED" for change in option["changes"])
        for option in report["options"]
    )
    assert facts(client) == before


def test_art_loop_deadline_is_rechecked_after_model_io(
    client, episode, settings, model_service, monkeypatch
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    future = utcnow() + timedelta(seconds=3600)

    def advance(payload):
        monkeypatch.setattr("arclight.application.agent_limits.utcnow", lambda: future)

    sent, errors, _ = install_direction_model(model_service, on_http=advance)
    before = facts(client)
    result = propose_art(client.app.state.session_factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.status == "STOPPED" and result.stop_code == "AGENT_DEADLINE_EXCEEDED", (
        result.model_dump_json()
    )
    assert len(sent) == 1 and facts(client) == before


def test_foreign_target_is_rejected_and_never_exposed_to_the_model(
    client, episode, settings, model_service
):
    foreign = create_asset(client, create_project(client)["id"], name="外部私有角色")
    unknown = UUID(foreign["id"])
    run_id, actor, _, task = prepare_direction(client, episode, settings, target_ids=[str(unknown)])
    sent, errors, _ = install_direction_model(model_service)
    result = propose_art(client.app.state.session_factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.status == "HUMAN_REVIEW"
    denied = tool_feedback(sent[-1])[-1]
    assert denied["tool_name"] == "investigate_art"
    assert denied["status"] == "FAILED"
    assert denied["error"]["code"] == "REFERENCE_SCOPE_INVALID"
    assert denied["result"] is None
    assert str(unknown) not in str(denied)
    assert foreign["name"] not in str(sent)
    assert task.target_asset_ids == [unknown]


@pytest.mark.parametrize("kind", ["SCENE", "PROP", "STYLE"])
def test_art_delivery_reuses_all_existing_concept_types_and_exact_reference_bindings(
    client, episode, settings, model_service, kind
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)

    def concept_type(draft):
        for option in draft["proposal"]["options"]:
            design = option["designs"][0]
            design["kind"] = kind
            design.pop("profile", None)

    _, errors, _ = install_direction_model(model_service, mutate_draft=concept_type)
    before = facts(client)
    result = propose_art(client.app.state.session_factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.status == "COMPLETED", result.model_dump_json()
    assert {
        design.kind for option in result.result.draft.proposal.options for design in option.designs
    } == {kind}
    assert facts(client) == before
