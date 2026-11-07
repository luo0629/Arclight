"""Exercise actual multimodal runtime, art investigation and authoritative comparison."""

import base64
import json
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from art_direction_support import (
    install_direction_model,
    prepare_direction,
    reference_set,
    seed_from_payload,
)
from pydantic import ValidationError
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_agent_runtime import tool_feedback
from test_asset_versions import image_bytes
from test_contexts import visual_setup

from arclight.application.art_direction import propose_art
from arclight.domain.art_direction import ArtDirectionDraft, ArtDirectionTask
from arclight.persistence.art_proposals import ArtApprovedVersion, ArtProposalVersion
from arclight.persistence.models import AssetVersion, Review, Storyboard, VisualAsset, WorkflowRun


def facts(client):
    with client.app.state.session_factory() as db:
        return {
            model.__name__: db.scalar(select(func.count()).select_from(model))
            for model in (ArtProposalVersion, ArtApprovedVersion, Review, Storyboard, AssetVersion)
        } | {
            "directories": [
                (
                    str(asset.id),
                    asset.name,
                    asset.revision,
                    asset.lock_version,
                    str(asset.current_version_id),
                    asset.status,
                    asset.metadata_json,
                    asset.character_profile,
                )
                for asset in db.scalars(select(VisualAsset).order_by(VisualAsset.id))
            ]
        }


def test_real_multimodal_loop_delivers_compared_art_proposal_and_preserves_formal_state(
    client, episode, settings, model_service
):
    asset, versions = reference_set(client, episode)
    run_id, actor, source, task = prepare_direction(
        client, episode, settings, target_ids=[asset["id"]]
    )
    before = facts(client)
    sent, errors, _ = install_direction_model(model_service)
    result = propose_art(client.app.state.session_factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.status == "COMPLETED", result.model_dump_json()
    assert len(sent) == result.model_turns == 4
    delivery = result.result
    assert delivery.draft.proposal.origin == "AGENT"
    assert len(delivery.draft.proposal.options) == 2
    assert len(delivery.draft.assessments) == 2
    assert {str(ref.asset.asset_version_id) for ref in delivery.draft.proposal.references} == {
        version["id"] for version in versions
    }
    assert delivery.draft.proposal.sources[0].resource_id == UUID(source["id"])
    images = [
        part["image_url"]["url"]
        for message in sent[2]["messages"]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part["type"] == "image_url"
    ]
    assert images == [
        "data:image/png;base64," + base64.b64encode(image_bytes(color=color)).decode()
        for color in ("blue", "red")
    ]
    assert facts(client) == before
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.status == "RUNNING" and run.pending_review is None
        assert "content_base64" not in json.dumps(run.agent_state_json)


@pytest.mark.parametrize("initial", ["雨夜角色", "空目录"])
def test_model_changes_investigation_after_actual_directory_feedback(
    client, episode, settings, model_service, initial
):
    reference_set(client, episode)
    reference_set(client, episode, count=0, name="空目录")
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    sent, errors, _ = install_direction_model(model_service, search=initial, fallback="雨夜角色")
    result = propose_art(client.app.state.session_factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.status == "COMPLETED", result.model_dump_json()
    calls = [
        call["function"]["arguments"]
        for payload in sent
        for call in payload.get("messages", [])
        if call.get("role") == "assistant"
        for call in call.get("tool_calls", [])
    ]
    # The extra catalog query is selected only after the actual empty-directory result.
    assert result.model_turns == (4 if initial == "雨夜角色" else 5)
    assert any("version_ids" in value for value in calls)


def test_locked_bible_changes_remain_proposals_and_comparison_reports_real_locks(
    client, episode, settings, model_service
):
    visual_setup(client, episode)
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    sent, errors, _ = install_direction_model(model_service, bible=True)
    before = client.get(f"/api/v1/projects/{episode['project_id']}/visual-bible").json()
    result = propose_art(client.app.state.session_factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.status == "COMPLETED", result.model_dump_json()
    comparison = tool_feedback(sent[-1])[-1]["result"]["comparison"]
    assert all(
        any(change["protection"] == "LOCKED" for change in option["changes"])
        for option in comparison["options"]
    )
    assert client.get(f"/api/v1/projects/{episode['project_id']}/visual-bible").json() == before
    assert result.result.draft.proposal.requires_human_review
    source = seed_from_payload(sent[0])["visual_bible_source"]
    assert source["resource_id"] == before["id"]
    assert source["resource_type"] == "VISUAL_BIBLE"
    proposal = result.result.draft.proposal.model_dump(mode="json")
    assert source in proposal["sources"]
    assert all(
        design["content_sha256"] == source["content_sha256"]
        for option in proposal["options"]
        for design in option["designs"]
    )


@pytest.mark.parametrize("count", [0, 1])
def test_missing_required_images_escalate_without_formal_writes(
    client, episode, settings, model_service, count
):
    reference_set(client, episode, count=count)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    before = facts(client)
    sent, errors, _ = install_direction_model(model_service)
    result = propose_art(client.app.state.session_factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.status == "HUMAN_REVIEW", result.model_dump_json()
    assert result.human_request.reason == "MISSING_EVIDENCE" and result.question_id
    assert facts(client) == before
    if count:
        assert tool_feedback(sent[-1])[-1]["error"]["code"] == "ART_EVIDENCE_REQUIRED"


@pytest.mark.parametrize("fault", ["source", "evidence", "goal", "constraints", "scope"])
def test_unobserved_or_retargeted_drafts_cannot_pass_comparison(
    client, episode, settings, model_service, fault
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)

    def mutate(draft):
        if fault == "source":
            draft["proposal"]["sources"][0]["resource_id"] = str(uuid4())
        elif fault == "evidence":
            draft["evidence"][0]["manifest_id"] = str(uuid4())
            for assessment in draft["assessments"]:
                assessment["evidence_manifest_ids"][0] = draft["evidence"][0]["manifest_id"]
        elif fault == "goal":
            draft["proposal"]["visual_goal"] = "改成另一个任务"
        elif fault == "constraints":
            draft["proposal"]["constraints"] = []
        else:
            draft["proposal"]["project_id"] = str(uuid4())

    sent, errors, _ = install_direction_model(model_service, mutate_draft=mutate)
    before = facts(client)
    result = propose_art(client.app.state.session_factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.status == "HUMAN_REVIEW", result.model_dump_json()
    feedback = tool_feedback(sent[-1])[-1]
    assert feedback["error"]["code"] in {"ART_EVIDENCE_INVALID", "ART_DIRECTION_SCOPE"}
    assert facts(client) == before


@pytest.mark.parametrize("fault", ["skip", "hash", "prompt", "assessment"])
def test_final_delivery_requires_exact_unmodified_real_comparison(
    client, episode, settings, model_service, fault
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)

    def mutate(result):
        if fault == "hash":
            result["comparison_sha256"] = "f" * 64
        elif fault == "prompt":
            result["draft"]["proposal"]["options"][0]["prompts"][0]["text"] += "悄悄改稿"
        elif fault == "assessment":
            result["draft"]["assessments"][0]["tradeoffs"] = "改成另一个比较结论"

    _, errors, _ = install_direction_model(
        model_service, bypass_compare=fault == "skip", mutate_final=mutate
    )
    before = facts(client)
    result = propose_art(client.app.state.session_factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.status == "STOPPED" and result.stop_code == "ART_COMPARISON_REQUIRED"
    assert facts(client) == before


def test_art_domain_rejects_unbounded_targets_and_privileged_fields():
    with pytest.raises(ValidationError):
        ArtDirectionTask(visual_goal="目标", target_asset_ids=[uuid4()] * 2)
    with pytest.raises(ValidationError):
        ArtDirectionTask(visual_goal="目标", minimum_reference_images=True)
    with pytest.raises(ValidationError):
        ArtDirectionTask.model_validate({"visual_goal": "目标", "approve": True})


def test_assessments_cannot_omit_options_or_hide_unread_reference_bindings(
    client, episode, settings, model_service
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    _, errors, drafts = install_direction_model(model_service)
    assert (
        propose_art(client.app.state.session_factory, settings, run_id, actor, task).status
        == "COMPLETED"
    )
    assert not errors, errors
    value = deepcopy(drafts[-1])
    value["assessments"].pop()
    with pytest.raises(ValidationError):
        ArtDirectionDraft.model_validate(value)
    value = deepcopy(drafts[-1])
    for option in value["proposal"]["options"]:
        remaining = option["reference_ids"][:-1]
        option["reference_ids"] = list(remaining)
        option["prompts"][0]["reference_ids"] = list(remaining)
    for assessment in value["assessments"]:
        assessment["reference_ids"] = list(remaining)
    with pytest.raises(ValidationError):
        ArtDirectionDraft.model_validate(value)
