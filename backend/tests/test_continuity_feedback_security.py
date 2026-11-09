"""New feedback and exceptions never bypass exact review or current authority."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_asset_selection import choose
from test_asset_versions import state
from test_collaboration import register
from test_concurrency import require_postgres
from test_continuity_feedback import feedback_case as feedback_case
from test_continuity_feedback import (
    feedback_rows,
    install_feedback_model,
    run_feedback,
    target_rows,
)
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_revisions import execution_counts, request_revision
from test_continuity_revisions import revision_case as revision_case
from test_continuity_rules import rules_storage as rules_storage
from test_human_reviews import decide, resume

from arclight.application import continuity_feedback as feedback
from arclight.application.continuity_feedback_store import validate_feedback_record
from arclight.core.errors import DomainError
from arclight.domain.continuity_reviews import ReviewRerun
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import AssetVersion, Project, ProjectMember


def human_request(client, result):
    response = client.get(f"/api/v1/human-reviews/{result.record.human_review_request_id}")
    assert response.status_code == 200, response.text
    return response.json()


def test_new_review_uncertainty_reuses_qa06_without_calling_feedback_model(
    client, settings, model_service, feedback_case
):
    case = feedback_case
    sent, _, _, errors, deliveries = install_feedback_model(
        model_service,
        ["READY_FOR_REVIEW"],
        uncertainty={"reason": "OCCLUDED", "detail": "The new garment is not visible."},
    )
    result = run_feedback(client, settings, case)
    assert result.status == "HUMAN_REVIEW", result.model_dump_json()
    assert result.record.source == "REVIEWER" and not sent and not errors
    assert deliveries[0]["conclusion"] == "HUMAN_REVIEW"
    row = human_request(client, result)
    from arclight.persistence.human_reviews import HumanReviewRequest

    with client.app.state.session_factory() as db:
        original = db.get(HumanReviewRequest, result.record.human_review_request_id)
        assert original.handler == "continuity_shot"
        assert original.snapshot_json["object_id"] == str(result.record.review_id)
    assert run_feedback(client, settings, case) == result
    decision = decide(client, row)
    assert decision.status_code == 200, decision.text
    assert resume(client, row, decision.json()).status_code == 200
    asset, version = target_rows(client, case)
    ready = state(client, asset, version, "READY_FOR_REVIEW")
    assert ready.status_code == 200, ready.text


def test_new_feedback_human_request_cannot_borrow_preceding_exception(
    client, settings, model_service, feedback_case
):
    case = feedback_case
    install_feedback_model(model_service, ["HUMAN_REVIEW"])
    first = run_feedback(client, settings, case)
    row = human_request(client, first)
    decision = decide(client, row)
    assert decision.status_code == 200
    original_resume = resume(client, row, decision.json())
    assert original_resume.status_code == 200
    asset, version = target_rows(client, case)
    ready = state(client, asset, version, "READY_FOR_REVIEW")
    assert ready.status_code == 200, ready.text
    approved = state(client, asset, ready.json(), "APPROVED")
    assert approved.status_code == 200, approved.text
    install_feedback_model(model_service, ["HUMAN_REVIEW"])
    second = run_feedback(
        client,
        settings,
        case,
        rerun=ReviewRerun(
            previous_review_id=first.record.review_id,
            previous_sha256=first.record.review_sha256,
            reason="Read the exact approved image and its reference evidence again.",
        ),
    )
    assert second.status == "HUMAN_REVIEW", second.model_dump_json()
    assert second.record.human_review_request_id != first.record.human_review_request_id
    assert second.record.review_id != first.record.review_id
    assert len(feedback_rows(client, case)) == 2
    asset, version = target_rows(client, case)
    blocked = choose(client, asset, version)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "HUMAN_DECISION_REQUIRED"
    # The shared service may replay a completed historical receipt. That
    # immutable receipt still grants nothing to the new pending request.
    replay = resume(client, row, decision.json())
    assert replay.status_code == 200 and replay.json() == original_resume.json()
    blocked_again = choose(client, asset, version)
    assert blocked_again.status_code == 409
    assert blocked_again.json()["error"]["code"] == "HUMAN_DECISION_REQUIRED"


@pytest.mark.parametrize("phase", ["DECIDE", "RESUME", "CACHED_FEEDBACK"])
def test_revoked_original_actor_cannot_handle_or_replay_feedback(
    client, settings, model_service, feedback_case, phase
):
    case = feedback_case
    sent, _, _, _, _ = install_feedback_model(model_service, ["HUMAN_REVIEW"])
    result = run_feedback(client, settings, case)
    row = human_request(client, result)
    decision = decide(client, row).json() if phase == "RESUME" else None
    new_owner = register(client, uuid4().hex + "@example.com")["user"]["id"]
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(case["project_id"])).owner_id = new_owner
    if phase == "CACHED_FEEDBACK":
        with pytest.raises(DomainError) as caught:
            run_feedback(client, settings, case)
        assert caught.value.status_code in {403, 404}
    else:
        response = decide(client, row) if phase == "DECIDE" else resume(client, row, decision)
        assert response.status_code in {403, 404}, response.text
    assert len(sent) == 1


def test_feedback_history_is_project_scoped_and_readable_by_viewer(
    client, settings, model_service, feedback_case
):
    case = feedback_case
    install_feedback_model(model_service, ["READY_FOR_REVIEW"])
    result = run_feedback(client, settings, case)
    root = (
        f"/api/v1/projects/{case['project_id']}/continuity-revisions/"
        f"{case['revision']['revision_id']}/feedback"
    )
    viewer = register(client, uuid4().hex + "@example.com")
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(case["project_id"]),
                user_id=UUID(viewer["user"]["id"]),
                role="viewer",
            )
        )
    client.headers["Authorization"] = "Bearer " + viewer["access_token"]
    response = client.get(root)
    assert response.status_code == 200, response.text
    assert response.json() == [result.record.model_dump(mode="json")]
    foreign = root.replace(case["project_id"], str(uuid4()))
    assert client.get(foreign).status_code == 404
    assert request_revision(client, case).status_code == 403
    outsider = register(client, uuid4().hex + "@example.com")
    client.headers["Authorization"] = "Bearer " + outsider["access_token"]
    assert client.get(root).status_code == 404


def test_modified_checkpoint_cannot_replace_original_model_delivery_or_human_basis(
    client, settings, model_service, feedback_case
):
    case = feedback_case
    install_feedback_model(model_service, ["HUMAN_REVIEW"])
    result = run_feedback(client, settings, case)
    row = human_request(client, result)
    with client.app.state.session_factory.begin() as db:
        loop = db.get(AgentLoop, result.record.decision_loop_id)
        changed = deepcopy(loop.outcome_json)
        changed["result"]["reason"] = "A fabricated replacement reason."
        loop.outcome_json = changed
    assert decide(client, row).status_code == 409
    with client.app.state.session_factory() as db:
        with pytest.raises(DomainError) as caught:
            validate_feedback_record(db, result.record)
        assert caught.value.code == "FEEDBACK_DELIVERY_INVALID"
    assert feedback_rows(client, case)[0] == result.record


@pytest.mark.parametrize("entry", ["READY", "SELECT"])
def test_feedback_model_in_flight_blocks_both_existing_formal_entry_paths(
    client, settings, model_service, feedback_case, entry
):
    case = feedback_case
    observed = []

    def during_model(seed):
        asset, version = target_rows(client, case)
        if entry == "SELECT":
            with client.app.state.session_factory.begin() as db:
                db.get(AssetVersion, UUID(version["id"])).status = "APPROVED"
            asset, version = target_rows(client, case)
            blocked = choose(client, asset, version)
        else:
            blocked = state(client, asset, version, "READY_FOR_REVIEW")
        observed.append(blocked)

    install_feedback_model(model_service, ["READY_FOR_REVIEW"], on_feedback=during_model)
    result = run_feedback(client, settings, case)
    assert result.status == "READY_FOR_REVIEW", result.model_dump_json()
    assert len(observed) == 1 and observed[0].status_code == 409
    assert observed[0].json()["error"]["code"] == "CONTINUITY_FEEDBACK_PENDING"


@pytest.mark.parametrize("matching", [True, False])
def test_already_admitted_manual_revision_is_reused_or_escalated_without_second_job(
    client, settings, model_service, feedback_case, matching
):
    case = feedback_case
    admitted = []

    def during_model(seed):
        request = {
            "review_id": seed["review"]["review"]["result_id"],
            "issue_ids": [seed["review"]["review"]["issues"][0]["issue_id"]],
            "action": "REGENERATE" if matching else "REVISE_PROMPT",
            "amendments": []
            if matching
            else [
                {"block": "continuity_context", "instruction": "Keep the exact red case closed."}
            ],
        }
        response = request_revision(client, case, key="manual-existing-revision", command=request)
        assert response.status_code == 201, response.text
        admitted.append(response.json())

    sent, _, _, errors, _ = install_feedback_model(
        model_service, ["REVISE"], difference=("PROP", "MEDIUM"), on_feedback=during_model
    )
    before = execution_counts(client)
    result = run_feedback(client, settings, case)
    assert result.status == ("REVISION_QUEUED" if matching else "HUMAN_REVIEW")
    assert result.record.child_revision_id == UUID(admitted[0]["revision_id"])
    if not matching:
        assert result.record.stop_code == "REVISION_ALREADY_REQUESTED"
    assert run_feedback(client, settings, case) == result
    assert execution_counts(client)["generation_jobs"] == before["generation_jobs"] + 1
    assert len(sent) == 1 and not errors


def test_current_reference_withdrawal_during_feedback_cannot_execute_old_revision(
    client, settings, model_service, feedback_case
):
    from arclight.persistence.models import Storyboard

    case = feedback_case

    def withdraw(seed):
        with client.app.state.session_factory.begin() as db:
            db.get(Storyboard, UUID(case["target"]["storyboard_id"])).status = "REJECTED"

    sent, _, _, _, _ = install_feedback_model(
        model_service, ["REVISE"], difference=("PROP", "MEDIUM"), on_feedback=withdraw
    )
    before = execution_counts(client)
    result = run_feedback(client, settings, case)
    assert result.status == "STOPPED", result.model_dump_json()
    assert not feedback_rows(client, case)
    assert execution_counts(client)["generation_jobs"] == before["generation_jobs"]
    assert len(sent) == 1


def test_postgres_parallel_repair_seals_one_feedback_revision_and_reservation(
    client, settings, model_service, feedback_case, monkeypatch
):
    require_postgres(settings)
    case = feedback_case
    sent, _, _, errors, _ = install_feedback_model(
        model_service, ["REVISE"], difference=("PROP", "MEDIUM")
    )
    original = feedback.seal_feedback

    def crash(*args, **kwargs):
        raise RuntimeError("receipt not sealed")

    monkeypatch.setattr(feedback, "seal_feedback", crash)
    with pytest.raises(RuntimeError, match="receipt not sealed"):
        run_feedback(client, settings, case)
    monkeypatch.setattr(feedback, "seal_feedback", original)
    before = execution_counts(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_feedback(client, settings, case), range(2)))
    assert results[0] == results[1]
    assert results[0].status == "REVISION_QUEUED"
    assert len(sent) == 1 and not errors
    assert len(feedback_rows(client, case)) == 1
    after = execution_counts(client)
    assert after["generation_jobs"] == before["generation_jobs"] + 1
    assert after["generation_budget_reservations"] == before["generation_budget_reservations"] + 1
    with client.app.state.session_factory() as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(AgentLoop).where(
                            AgentLoop.run_id == case["run_id"],
                            AgentLoop.seed_json["operation_key"]
                            .as_string()
                            .like("continuity-feedback/1:%"),
                        )
                    )
                )
            )
            == 1
        )
