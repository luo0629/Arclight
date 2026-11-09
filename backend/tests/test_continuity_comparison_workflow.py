"""Comparison and recovery over actual reviewer, revision and human receipts."""

import hashlib
import json
from uuid import UUID, uuid4

import pytest
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_asset_selection import choose
from test_asset_versions import state
from test_collaboration import register
from test_continuity_comparison import comparison, comparison_path
from test_continuity_escalations import human_case
from test_continuity_feedback import (
    feedback_case as feedback_case,
)
from test_continuity_feedback import (
    install_feedback_model,
    run_feedback,
)
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_reviewer import run_review
from test_continuity_revisions import execution_counts
from test_continuity_revisions import revision_case as revision_case
from test_continuity_rules import rules_storage as rules_storage
from test_human_reviews import decide, decision_payload, resume

from arclight.domain.agent_limits import AgentRunLimits
from arclight.persistence.models import AssetVersion, ProjectMember, VisualAsset


@pytest.mark.parametrize("rules_storage", ["local", "s3"], indirect=True)
def test_exact_images_lineage_evidence_and_feedback_are_read_only(
    client, settings, model_service, feedback_case
):
    case = feedback_case
    sent, _, reviewed, errors, _ = install_feedback_model(model_service, ["READY_FOR_REVIEW"])
    result = run_feedback(client, settings, case)
    assert not errors and result.status == "READY_FOR_REVIEW"
    calls = len(sent), len(reviewed)
    target = result.record.target.model_dump(mode="json")
    revised_case = {"project_id": case["project_id"], "target": target}
    before = execution_counts(client)
    original = comparison(client, case)
    revised = comparison(client, revised_case)
    assert execution_counts(client) == before and calls == (len(sent), len(reviewed))
    assert original["actions"]["blocker"]["code"] == "CONTINUITY_REVISION_REQUIRED"
    assert revised["actions"]["blocker"] is None and revised["actions"]["can_submit"]
    assert revised["version"]["status"] == "DRAFT" and not revised["actions"]["can_select"]
    for projection in (original, revised):
        assert {item["candidate_id"] for item in projection["images"]} == {
            case["target"]["candidate_id"],
            case["candidate"]["id"],
        }
        assert [item["round"] for item in projection["images"]] == [0, 1]
        assert projection["revisions"][0]["record"] == case["revision"]
        assert projection["revisions"][0]["feedback"] == [result.record.model_dump(mode="json")]
        for image in projection["images"]:
            response = client.get("/api/v1" + image["image_path"])
            assert response.status_code == 200, response.text
            assert hashlib.sha256(response.content).hexdigest() == image["version"]["checksum"]
    references = revised["reviews"][0]["references"]
    image_refs = [item for item in references if item["image_path"]]
    assert len(image_refs) >= 5
    for item in image_refs:
        response = client.get("/api/v1" + item["image_path"])
        assert response.status_code == 200, response.text
        assert hashlib.sha256(response.content).hexdigest() == item["reference"]["content_sha256"]
        if item["reference"]["role"] == "REFERENCE":
            assert item["reference"]["reference_id"] in item["image_path"]
            assert revised["latest_review_id"] in item["image_path"]
    path = "/api/v1" + image_refs[1]["image_path"]
    ref = image_refs[1]["reference"]
    for original_id in (
        revised["latest_review_id"],
        ref["reference_id"],
        target["asset_id"],
        target["asset_version_id"],
        case["project_id"],
    ):
        assert client.get(path.replace(original_id, str(uuid4()))).status_code == 404
    session = register(client, "qa08-reference-viewer@example.com")
    actor_id = UUID(session["user"]["id"])
    with client.app.state.session_factory.begin() as db:
        db.add(ProjectMember(project_id=UUID(case["project_id"]), user_id=actor_id, role="viewer"))
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    with client.app.state.session_factory.begin() as db:
        db.delete(db.scalar(select(ProjectMember).where(ProjectMember.user_id == actor_id)))
    assert client.get(path).status_code == 404
    client.headers["Authorization"] = "Bearer test-token"
    public = json.dumps(revised)
    assert all(
        secret not in public
        for secret in ("system_instruction", "base64,", "object_key", "Authorization", "seed_json")
    )
    # A later reference pointer must not silently replace a frozen historical image.
    item = next(item for item in image_refs if item["reference"]["role"] == "REFERENCE")
    with client.app.state.session_factory.begin() as db:
        db.get(VisualAsset, UUID(item["reference"]["asset_id"])).current_version_id = None
    refreshed = comparison(client, revised_case)
    assert (
        next(
            ref
            for ref in refreshed["reviews"][0]["references"]
            if ref["reference"]["reference_id"] == item["reference"]["reference_id"]
        )["image_path"]
        == item["image_path"]
    )


@pytest.mark.parametrize("answer", ["APPROVE", "REJECT"])
def test_exact_owner_decision_survives_reload_and_separate_resume(
    client, settings, model_service, reviewer_case, answer
):
    case = reviewer_case
    row, _, _, _, sent = human_case(client, settings, model_service, case)
    initial = comparison(client, case)
    assert initial["human_reviews"][0]["id"] == row["id"]
    assert initial["human_reviews"][0]["status"] == "WAITING_REVIEW"
    assert initial["actions"]["blocker"]["code"] == "HUMAN_DECISION_REQUIRED"
    before = len(sent)
    wrong = decision_payload(row, answer)
    wrong["request_sha256"] = "0" * 64
    assert (
        client.post(f"/api/v1/human-reviews/{row['id']}/decisions", json=wrong).status_code == 409
    )
    blank = decision_payload(row, answer, "  ")
    assert (
        client.post(f"/api/v1/human-reviews/{row['id']}/decisions", json=blank).status_code == 422
    )
    decision = decide(client, row, answer)
    assert decision.status_code == 200, decision.text
    reloaded = comparison(client, case)
    decided = reloaded["human_reviews"][0]
    assert decided["status"] == "DECIDED" and decided["resume"] is None
    assert decided["decision"]["id"] == decision.json()["id"]
    assert decided["snapshot"] == row["snapshot"]
    response = resume(client, decided, decided["decision"])
    assert response.status_code == 200, response.text
    done = comparison(client, case)
    assert done["human_reviews"][0]["status"] == "RESUMED"
    assert done["human_reviews"][0]["resume"]["decision_id"] == decision.json()["id"]
    assert done["reviews"][0]["record"] == initial["reviews"][0]["record"]
    assert len(sent) == before
    assert done["version"]["status"] == "DRAFT" and done["asset"]["current_version_id"] is None
    if answer == "APPROVE":
        assert done["actions"]["can_submit"] and not done["actions"]["can_select"]
        ready = state(client, done["asset"], done["version"], "READY_FOR_REVIEW")
        assert ready.status_code == 200, ready.text
        approved = state(client, done["asset"], ready.json(), "APPROVED")
        assert approved.status_code == 200, approved.text
        final = comparison(client, case)
        assert final["actions"]["can_select"]
        assert choose(client, final["asset"], final["version"]).status_code == 200
    else:
        assert done["actions"]["blocker"]["code"] == "HUMAN_DECISION_REQUIRED"
        assert choose(client, done["asset"], done["version"]).status_code == 409


def test_feedback_human_request_is_visible_on_the_revised_exact_version(
    client, settings, model_service, feedback_case
):
    sent, _, _, errors, _ = install_feedback_model(model_service, ["HUMAN_REVIEW"])
    result = run_feedback(client, settings, feedback_case)
    assert not errors and result.status == "HUMAN_REVIEW"
    case = {
        "project_id": feedback_case["project_id"],
        "target": result.record.target.model_dump(mode="json"),
    }
    result_read = comparison(client, case)
    request = result_read["human_reviews"][0]
    assert request["id"] == str(result.record.human_review_request_id)
    assert request["snapshot"]["version_id"] == case["target"]["asset_version_id"]
    assert request["snapshot"]["object_id"] == str(result.record.feedback_id)
    assert comparison(client, feedback_case)["human_reviews"] == []
    answer = decide(client, request)
    assert answer.status_code == 200, answer.text
    read_decision = comparison(client, case)["human_reviews"][0]
    assert read_decision["status"] == "DECIDED"
    assert resume(client, read_decision, read_decision["decision"]).status_code == 200
    assert comparison(client, case)["actions"]["blocker"] is None
    assert len(sent) == 1
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(case["target"]["asset_version_id"]))
        assert version.status == "DRAFT"
        assert db.get(VisualAsset, version.asset_id).current_version_id is None


def test_changed_version_keeps_receipt_but_closes_actions(
    client, settings, model_service, reviewer_case
):
    case = reviewer_case
    human_case(client, settings, model_service, case)
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(case["target"]["asset_version_id"])).stale = True
    result = comparison(client, case)
    assert result["human_reviews"][0]["status"] == "BLOCKED"
    assert result["human_reviews"][0]["blocking_code"] is not None
    assert not result["actions"]["can_select"] and not result["actions"]["can_submit"]
    assert client.get(comparison_path(case)).status_code == 200


@pytest.mark.parametrize("reviewer_case", [AgentRunLimits(max_model_calls=1)], indirect=True)
def test_incomplete_review_has_no_approval_capability_but_can_be_rejected_and_resumed(
    client, settings, model_service, reviewer_case
):
    sent, errors, _ = install_reviewer_model(model_service)
    outcome = run_review(client, settings, reviewer_case)
    assert not errors and outcome.status == "STOPPED" and len(sent) == 1
    result = comparison(client, reviewer_case)
    assert result["reviews"] == [] and result["latest_review_id"] is None
    request = result["human_reviews"][0]
    assert request["status"] == "WAITING_REVIEW" and not request["can_approve"]
    denied = decide(client, request)
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "REVIEW_NOT_COMPLETED"
    decision = decide(client, request, "REJECT")
    assert decision.status_code == 200, decision.text
    assert resume(client, request, decision.json()).status_code == 200
    final = comparison(client, reviewer_case)
    assert final["human_reviews"][0]["status"] == "RESUMED"
    assert not final["actions"]["can_select"] and not final["actions"]["can_submit"]
    assert len(sent) == 1
