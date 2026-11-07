"""Generated proposals use the existing immutable versions and Owner review path."""

import json
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from art_direction_support import install_direction_model, prepare_direction, reference_set
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_art_direction import facts
from test_art_proposals import proposal_payload
from test_art_protection import acknowledgement
from test_art_review_workflow import decide, decision, effects, path
from test_contexts import visual_setup

from arclight.application.art_generation import generate_art_proposal, publish_art_direction
from arclight.domain.art_generation import DELIVERY_KEY, ArtDeliveryInput
from arclight.domain.art_protection import ArtComparison
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.art_proposals import ArtProposalVersion


@pytest.mark.parametrize("kind", ["CHARACTER", "SCENE", "PROP", "STYLE", "VISUAL_BIBLE"])
@pytest.mark.parametrize("verdict", ["APPROVE", "REJECT"])
def test_multimodal_generation_freezes_provenance_and_uses_existing_exact_review(
    client, episode, settings, model_service, kind, verdict
):
    if kind == "VISUAL_BIBLE":
        visual_setup(client, episode)
    _, references = reference_set(client, episode)
    run_id, actor, script, task = prepare_direction(client, episode, settings)

    def choose(draft):
        for option in draft["proposal"]["options"]:
            design = option["designs"][0]
            if kind != "VISUAL_BIBLE":
                template = proposal_payload(kind)["options"][0]["designs"][0]
                template["design_id"] = design["design_id"]
                option["designs"] = [template]
        draft["proposal"]["preferred_option_id"] = draft["proposal"]["options"][1]["option_id"]

    sent, errors, _ = install_direction_model(
        model_service, bible=kind == "VISUAL_BIBLE", mutate_draft=choose
    )
    factory = client.app.state.session_factory
    before = facts(client)
    bible_endpoint = f"/api/v1/projects/{episode['project_id']}/visual-bible"
    before_bible = client.get(bible_endpoint).json()
    result = generate_art_proposal(factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.direction.status == "COMPLETED", result.model_dump_json()
    delivery = result.delivery
    assert delivery is not None and len(sent) == 4
    expected = deepcopy(before)
    expected["ArtProposalVersion"] += 1
    assert facts(client) == expected
    assert client.get(bible_endpoint).json() == before_bible
    proposal = result.direction.result.draft.proposal
    assert delivery.option_id == proposal.options[1].option_id
    endpoint = path(episode["project_id"]) + f"/{delivery.proposal_id}"
    response = client.get(endpoint)
    assert response.status_code == 200, response.text
    waiting = response.json()
    original = deepcopy(waiting["version"])
    assert original["proposal_json"] == proposal.model_dump(mode="json")
    assert waiting["review"]["id"] == str(delivery.review_request_id)
    assert waiting["review"]["status"] == "WAITING_REVIEW"
    assert waiting["review"]["snapshot"]["version_id"] == str(delivery.version_id)
    assert waiting["outputs"] == []
    with factory() as db:
        loop = db.get(AgentLoop, delivery.loop_id)
        action = db.scalar(
            select(AgentLoopAction).where(AgentLoopAction.action_key == DELIVERY_KEY)
        )
        provenance = ArtDeliveryInput.model_validate(action.request_json)
        assert action.result_json == delivery.model_dump(mode="json")
        assert action.request_sha256 == digest(action.request_json)
        assert provenance.seed.script_version_id == UUID(script["id"])
        assert provenance.seed.task == task
        assert provenance.input_sha256 == loop.input_sha256
        assert provenance.agent_request_sha256 == loop.request_sha256
        assert provenance.direction_sha256 == digest(
            result.direction.result.model_dump(mode="json")
        )
        assert provenance.evidence == result.direction.result.draft.evidence
        assert provenance.assessments == result.direction.result.draft.assessments
        assert {str(ref.asset.asset_version_id) for ref in proposal.references} == {
            ref["id"] for ref in references
        }
        assert "content_base64" not in json.dumps(action.request_json)
    assert publish_art_direction(factory, settings, delivery.loop_id, actor) == delivery
    body = decision(waiting, verdict)
    if verdict == "APPROVE":
        body["confirmation"] = acknowledgement(
            ArtComparison.model_validate(original["comparison_json"]), 1
        ).model_dump(mode="json")
    decided = decide(client, waiting, verdict, body=body)
    assert decided.status_code == 200, decided.text
    decided = decided.json()
    assert decided["review"]["resume"]["result_json"]["status"] == (
        "APPROVED" if verdict == "APPROVE" else "REJECTED"
    )
    assert len(decided["outputs"]) == (1 if verdict == "APPROVE" else 0)
    if verdict == "APPROVE":
        assert decided["outputs"][0]["target_type"] == kind
        if kind == "VISUAL_BIBLE":
            assert any(
                change["protection"] == "LOCKED"
                for option in original["comparison_json"]["options"]
                for change in option["changes"]
            )
            assert client.get(bible_endpoint).json()["id"] != before_bible["id"]
    else:
        assert facts(client) == expected
        assert client.get(bible_endpoint).json() == before_bible
    after = effects(client)
    assert publish_art_direction(factory, settings, delivery.loop_id, actor) == delivery
    assert generate_art_proposal(factory, settings, run_id, actor, task) == result
    assert decide(client, waiting, verdict, body=body).json() == decided
    assert len(sent) == 4 and effects(client) == after
    assert client.get(endpoint + f"/versions/{delivery.version_id}").json()["version"] == original


def test_generated_history_survives_manual_revision_and_archive(
    client, episode, settings, model_service
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    sent, errors, _ = install_direction_model(model_service)
    factory = client.app.state.session_factory
    generated = generate_art_proposal(factory, settings, run_id, actor, task)
    assert not errors
    delivery = generated.delivery
    endpoint = path(episode["project_id"]) + f"/{delivery.proposal_id}"
    waiting = client.get(endpoint).json()
    assert decide(client, waiting, "REJECT").status_code == 200
    current = client.get(endpoint).json()
    manual = proposal_payload(project_id=episode["project_id"])
    body = {
        "creation_key": str(uuid4()),
        "expected_revision": current["revision"],
        "proposal": manual,
    }
    response = client.put(endpoint, json=body)
    assert response.status_code == 201, response.text
    revised = response.json()
    assert revised["version"]["version_no"] == 2
    assert client.delete(endpoint + f"?expected_revision={revised['revision']}").status_code == 200
    assert publish_art_direction(factory, settings, delivery.loop_id, actor) == delivery
    assert generate_art_proposal(factory, settings, run_id, actor, task) == generated
    assert len(sent) == 4
    old = client.get(endpoint + f"/versions/{delivery.version_id}")
    assert old.status_code == 200 and old.json()["version"] == waiting["version"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ArtProposalVersion)) == 2
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentLoopAction)
                .where(AgentLoopAction.action_key == DELIVERY_KEY)
            )
            == 1
        )
