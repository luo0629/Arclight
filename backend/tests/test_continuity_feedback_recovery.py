"""Crashes, global allowances and human reentry preserve feedback authority."""

from copy import deepcopy
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_agent_loop_recovery import answer
from test_agent_model import model_service as model_service
from test_asset_versions import state
from test_continuity_feedback import feedback_case as feedback_case
from test_continuity_feedback import (
    feedback_rows,
    finish_revision,
    install_feedback_model,
    run_feedback,
    target_rows,
)
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_revisions import execution_counts, request_revision
from test_continuity_revisions import revision_case as revision_case
from test_continuity_rules import rules_storage as rules_storage
from test_generation_budget import priced, set_policy
from test_human_reviews import decide, resume

from arclight.application import continuity_feedback as feedback
from arclight.application.continuity_feedback_store import validate_feedback_record
from arclight.application.continuity_revision_store import revision_record
from arclight.domain.agent_limits import AgentRunLimits
from arclight.main import create_app
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop


@pytest.mark.parametrize("point", ["BEFORE_LOOP", "BEFORE_RECEIPT", "AFTER_REVISION"])
def test_crash_repair_keeps_selection_closed_and_no_duplicate_model_or_media(
    client, settings, model_service, feedback_case, monkeypatch, point
):
    case = feedback_case
    action = "REVISE" if point == "AFTER_REVISION" else "READY_FOR_REVIEW"
    sent, _, reviewed, errors, _ = install_feedback_model(
        model_service, [action], difference=("PROP", "MEDIUM") if action == "REVISE" else None
    )
    original_counts = execution_counts(client)
    original = {
        "BEFORE_LOOP": feedback.run_persistent_agent_loop,
        "BEFORE_RECEIPT": feedback.seal_feedback,
        "AFTER_REVISION": feedback.audit,
    }[point]
    name = {
        "BEFORE_LOOP": "run_persistent_agent_loop",
        "BEFORE_RECEIPT": "seal_feedback",
        "AFTER_REVISION": "audit",
    }[point]

    def crash(*args, **kwargs):
        raise RuntimeError("interrupted feedback handoff")

    monkeypatch.setattr(feedback, name, crash)
    with pytest.raises(RuntimeError, match="interrupted feedback"):
        run_feedback(client, settings, case)
    assert not feedback_rows(client, case)
    asset, version = target_rows(client, case)
    blocked = state(client, asset, version, "READY_FOR_REVIEW")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "CONTINUITY_FEEDBACK_PENDING"
    counts = execution_counts(client)
    assert counts["generation_jobs"] == original_counts["generation_jobs"]
    assert (
        counts["generation_budget_reservations"]
        == original_counts["generation_budget_reservations"]
    )
    calls = (len(sent), len(reviewed))
    monkeypatch.setattr(feedback, name, original)
    with TestClient(create_app(settings)) as restarted:
        result = run_feedback(restarted, settings, case)
        assert result.status == ("REVISION_QUEUED" if action == "REVISE" else "READY_FOR_REVIEW")
        assert run_feedback(restarted, settings, case) == result
    assert not errors
    assert (len(sent), len(reviewed)) == (1 if point == "BEFORE_LOOP" else calls[0], calls[1])
    counts = execution_counts(client)
    assert counts["generation_jobs"] == original_counts["generation_jobs"] + (action == "REVISE")


@pytest.mark.parametrize("reviewer_case", [AgentRunLimits(max_model_calls=10)], indirect=True)
def test_feedback_exhausts_original_global_model_allowance_without_reset(
    client, settings, model_service, feedback_case
):
    case = feedback_case
    with client.app.state.session_factory() as db:
        control = db.get(AgentRunLimit, case["run_id"])
        original = (control.deadline_at, deepcopy(control.limits_json), control.root_run_id)
    sent, _, reviewed, errors, _ = install_feedback_model(model_service, ["READY_FOR_REVIEW"])
    result = run_feedback(client, settings, case)
    assert result.status == "HUMAN_REVIEW", result.model_dump_json()
    assert result.record.source == "LIMIT"
    assert result.record.stop_code == "AGENT_MODEL_CALL_LIMIT"
    assert not sent and len(reviewed) == 5 and not errors
    assert run_feedback(client, settings, case) == result
    with client.app.state.session_factory() as db:
        validate_feedback_record(db, result.record)
        control = db.get(AgentRunLimit, case["run_id"])
        assert (control.deadline_at, control.limits_json, control.root_run_id) == original
    request = client.get(f"/api/v1/human-reviews/{result.record.human_review_request_id}")
    assert request.status_code == 200, request.text
    decision = decide(client, request.json())
    assert decision.status_code == 200, decision.text
    assert resume(client, request.json(), decision.json()).status_code == 200
    assert not sent


def test_three_media_rounds_stop_via_qa05_without_a_fourth_job(
    client, episode, settings, model_service, feedback_case
):
    case = feedback_case
    sent, _, _, errors, _ = install_feedback_model(
        model_service, ["REVISE"], difference=("PROP", "MEDIUM")
    )
    original = execution_counts(client)
    for round_no in (1, 2, 3):
        result = run_feedback(client, settings, case)
        with client.app.state.session_factory() as db:
            child = revision_record(db, UUID(case["project_id"]), result.record.child_revision_id)
        assert child.round == round_no + 1
        if round_no < 3:
            assert result.status == "REVISION_QUEUED"
            candidate = finish_revision(client, episode, case, child.model_dump(mode="json"))
            case = case | {"revision": child.model_dump(mode="json"), "candidate": candidate}
        else:
            assert child.status == "NEEDS_REVIEW" and child.job_id is None
            assert result.status == "HUMAN_REVIEW"
            assert result.record.stop_code == "REVISION_ROUND_LIMIT"
    assert len(sent) == 3 and not errors
    assert execution_counts(client)["generation_jobs"] == original["generation_jobs"] + 2
    assert run_feedback(client, settings, case) == result
    exhausted = case | {"revision": child.model_dump(mode="json")}
    stopped = run_feedback(client, settings, exhausted)
    assert stopped.status == "HUMAN_REVIEW" and stopped.record.source == "LIMIT"
    assert run_feedback(client, settings, exhausted) == stopped
    assert len(sent) == 3


def test_media_budget_refusal_is_atomic_and_persists_owner_handling(
    client, episode, settings, model_service, feedback_case, monkeypatch
):
    case = feedback_case
    priced(monkeypatch)
    set_policy(client, episode["project_id"], "1")
    sent, _, _, errors, _ = install_feedback_model(
        model_service, ["REVISE"], difference=("PROP", "MEDIUM")
    )
    before = execution_counts(client)
    result = run_feedback(client, settings, case)
    assert result.status == "HUMAN_REVIEW", result.model_dump_json()
    assert result.record.stop_code == "BUDGET_EXCEEDED"
    assert result.record.child_revision_id is None
    assert not errors and len(sent) == 1
    after = execution_counts(client)
    for key in ("generation_jobs", "generation_attempts", "generation_budget_reservations"):
        assert after[key] == before[key]
    assert run_feedback(client, settings, case) == result


@pytest.mark.parametrize("reviewer_case", [AgentRunLimits(max_model_calls=11)], indirect=True)
def test_new_exhausted_recheck_keeps_old_pass_unselectable_and_qa06_wait_durable(
    client, settings, model_service, feedback_case
):
    from arclight.persistence.human_reviews import HumanReviewRequest

    case = feedback_case
    sent, _, reviewed, errors, _ = install_feedback_model(model_service, ["INVESTIGATE"])
    result = run_feedback(client, settings, case)
    assert result.status == "STOPPED", result.model_dump_json()
    assert len(sent) == 1 and len(reviewed) == 5 and not errors
    history = feedback_rows(client, case)
    assert len(history) == 1 and history[0].action == "INVESTIGATE"
    asset, version = target_rows(client, case)
    blocked = state(client, asset, version, "READY_FOR_REVIEW")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "REVIEW_NOT_COMPLETED"
    with client.app.state.session_factory() as db:
        request = db.scalar(
            select(HumanReviewRequest).where(
                HumanReviewRequest.run_id == case["run_id"],
                HumanReviewRequest.handler == "continuity_incomplete",
            )
        )
        assert request is not None
        request_id = request.id
        assert request.snapshot_json["version_id"] == case["candidate"]["asset_version_id"]
    row = client.get(f"/api/v1/human-reviews/{request_id}").json()
    rejected = decide(client, row)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "REVIEW_NOT_COMPLETED"
    assert run_feedback(client, settings, case) == result
    assert len(sent) == 1 and len(reviewed) == 5


def test_waiting_original_media_does_not_start_model_generation_or_retry(
    client, settings, model_service, revision_case
):
    case = revision_case
    response = request_revision(client, case)
    assert response.status_code == 201, response.text
    case = case | {"revision": response.json()}
    sent, _, reviewed, _, _ = install_feedback_model(model_service, ["READY_FOR_REVIEW"])
    before = execution_counts(client)
    result = run_feedback(client, settings, case)
    assert result.status == "WAITING_MEDIA"
    assert result.job_id == UUID(response.json()["job_id"])
    assert run_feedback(client, settings, case) == result
    assert not sent and not reviewed and execution_counts(client) == before


def test_generic_agent_question_resumes_same_feedback_thread_and_limits(
    client, settings, model_service, feedback_case
):
    case = feedback_case
    sent, _, _, errors, _ = install_feedback_model(model_service, ["ASK", "READY_FOR_REVIEW"])
    waiting = run_feedback(client, settings, case)
    assert waiting.status == "REVIEWING" and waiting.question_id is not None
    with client.app.state.session_factory() as db:
        control = db.get(AgentRunLimit, case["run_id"])
        original = (control.deadline_at, deepcopy(control.limits_json), control.root_run_id)
        loop = db.get(AgentLoop, waiting.loop_id)
        thread, input_sha256 = loop.thread_id, loop.input_sha256
    with TestClient(create_app(settings)) as restarted:
        from types import SimpleNamespace

        decision = answer(
            SimpleNamespace(
                question_id=waiting.question_id,
                input_sha256=input_sha256,
            )
        )
        result = feedback.resume_revision_feedback(
            restarted.app.state.session_factory,
            settings,
            waiting.loop_id,
            case["actor"],
            decision=decision,
        )
        assert result.status == "READY_FOR_REVIEW", result.model_dump_json()
        calls = len(sent)
        assert (
            feedback.resume_revision_feedback(
                restarted.app.state.session_factory,
                settings,
                waiting.loop_id,
                case["actor"],
                decision=decision,
            )
            == result
        )
        assert len(sent) == calls and not errors
    with client.app.state.session_factory() as db:
        control = db.get(AgentRunLimit, case["run_id"])
        assert (control.deadline_at, control.limits_json, control.root_run_id) == original
        assert db.get(AgentLoop, waiting.loop_id).thread_id == thread
        assert len(list(db.scalars(select(AgentRunLimit)))) == 1


def test_legacy_recheck_operation_reenters_existing_loop_without_model_io(
    client, episode, settings, model_service, revision_case, monkeypatch
):
    from continuity_reviewer_support import install_reviewer_model
    from test_continuity_reviewer import run_review
    from test_continuity_routing import new_review_run, route

    from arclight.application import continuity_reviewer as reviewer
    from arclight.application.continuity_routing import read_routed_review
    from arclight.domain.continuity_reviews import ReviewRerun
    from arclight.domain.versions import digest

    case = new_review_run(client, episode, settings, revision_case)
    with client.app.state.session_factory() as db:
        previous, _ = read_routed_review(db, revision_case["review"].result_id)
    rerun = ReviewRerun(
        previous_review_id=previous.review.result_id,
        previous_sha256=digest(previous.model_dump(mode="json")),
        reason="Resume a recheck persisted by the preceding reviewer version.",
    )
    operation = reviewer.review_operation
    monkeypatch.setattr(reviewer, "review_operation", lambda target, rerun=None: operation(target))
    sent, errors, _ = install_reviewer_model(model_service, dynamic=False)
    first = run_review(client, settings, case, rerun=rerun)
    assert first.status == "COMPLETED", first.model_dump_json()
    route(client, settings, case)
    monkeypatch.setattr(reviewer, "review_operation", operation)
    calls = len(sent)
    assert run_review(client, settings, case, rerun=rerun) == first
    assert len(sent) == calls and not errors
