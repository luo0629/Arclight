"""QA-06 crash, revocation and stale-review boundaries use the original journals."""

from uuid import UUID, uuid4

import pytest
from continuity_reviewer_support import install_reviewer_model
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_asset_selection import choose, current
from test_collaboration import register
from test_continuity_escalations import count, human_case, pending
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_reviewer import run_review
from test_continuity_routing import new_review_run, route
from test_continuity_rules import rules_storage as rules_storage
from test_continuity_selection import approve, target_rows
from test_human_reviews import decide, resume

import arclight.application.continuity_escalations as escalations
import arclight.application.human_reviews as human_reviews
from arclight.application.agent_limits import configure_agent_run
from arclight.application.continuity_review_store import require_review_record
from arclight.application.continuity_selection import require_shot_review
from arclight.application.production_events import consume_stage_event
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.continuity_escalations import ESCALATION_RESOURCE
from arclight.domain.continuity_reviews import ReviewRerun
from arclight.domain.versions import digest
from arclight.main import create_app
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    Project,
    ProjectMember,
    User,
    VersionBinding,
    WorkflowRun,
)


@pytest.mark.parametrize("phase", ["WAITING", "DECIDED"])
def test_new_application_and_engine_resume_same_version_once_without_model_io(
    client, settings, model_service, reviewer_case, phase
):
    row, outcome, _, _, sent = human_case(client, settings, model_service, reviewer_case)
    saved = decide(client, row).json() if phase == "DECIDED" else None
    calls = len(sent)
    with TestClient(create_app(settings)) as restarted:
        restarted.headers["Authorization"] = "Bearer test-token"
        assert restarted.app.state.engine is not client.app.state.engine
        recovered = pending(restarted, reviewer_case)
        assert recovered["id"] == row["id"]
        if saved is None:
            saved = decide(restarted, row).json()
        first = resume(restarted, row, saved)
        assert first.status_code == 200, first.text
        assert first.json()["result_json"]["thread_id"] == str(reviewer_case["run_id"])
        assert first.json()["result_json"]["version_id"] == row["snapshot"]["version_id"]
        assert resume(restarted, row, saved).json() == first.json()
        assert count(restarted, HumanReviewResume) == 1
        with restarted.app.state.session_factory() as db:
            assert db.get(AgentLoop, outcome.loop_id).outcome_json == outcome.model_dump(
                mode="json"
            )
    assert len(sent) == calls


@pytest.mark.parametrize("phase", ["DECIDE", "RESUME"])
@pytest.mark.parametrize("role", ["stranger", "viewer", "editor", "new_owner"])
def test_owner_role_is_rechecked_for_exact_saved_exception(
    client, settings, model_service, reviewer_case, phase, role
):
    row, *_ = human_case(client, settings, model_service, reviewer_case)
    saved = decide(client, row).json() if phase == "RESUME" else None
    user = register(client, uuid4().hex + "@example.com")["user"]
    with client.app.state.session_factory.begin() as db:
        if role in {"viewer", "editor"}:
            db.add(
                ProjectMember(
                    project_id=UUID(reviewer_case["project_id"]),
                    user_id=UUID(user["id"]),
                    role=role,
                )
            )
        elif role == "new_owner":
            db.get(Project, UUID(reviewer_case["project_id"])).owner_id = user["id"]
    response = decide(client, row) if phase == "DECIDE" else resume(client, row, saved)
    assert response.status_code in {403, 404}, response.text
    assert count(client, HumanReviewResume) == 0
    assert count(client, HumanReviewDecision) == (1 if phase == "RESUME" else 0)


@pytest.mark.parametrize("phase", ["DECIDE", "RESUME"])
@pytest.mark.parametrize("revocation", ["disabled", "membership"])
def test_disabled_original_initiator_cannot_be_replaced_by_current_owner(
    client, episode, settings, model_service, reviewer_case, phase, revocation
):
    # Both the run's immutable author and its Reviewer loop use a real user.
    user = register(client, uuid4().hex + "@example.com")["user"]
    actor = user["id"]
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(reviewer_case["project_id"]), user_id=UUID(actor), role="editor"
            )
        )
    case = new_review_run(client, episode, settings, reviewer_case)
    assert case["actor"] == actor
    with client.app.state.session_factory.begin() as db:
        configure_agent_run(db, case["run_id"], actor, AgentRunLimits())
    row, *_ = human_case(client, settings, model_service, case)
    client.headers["Authorization"] = "Bearer test-token"
    saved = decide(client, row).json() if phase == "RESUME" else None
    with client.app.state.session_factory.begin() as db:
        if revocation == "disabled":
            db.get(User, UUID(actor)).disabled = True
        else:
            db.execute(ProjectMember.__table__.delete().where(ProjectMember.user_id == UUID(actor)))
    response = decide(client, row) if phase == "DECIDE" else resume(client, row, saved)
    assert response.status_code == (403 if revocation == "disabled" else 404), response.text
    assert count(client, HumanReviewResume) == 0


@pytest.mark.parametrize("phase", ["DECIDE", "RESUME"])
def test_revoked_original_initiator_cannot_handle_old_exception(
    client, settings, model_service, reviewer_case, phase
):
    row, *_ = human_case(client, settings, model_service, reviewer_case)
    saved = decide(client, row).json() if phase == "RESUME" else None
    new_owner = register(client, uuid4().hex + "@example.com")["user"]["id"]
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(reviewer_case["project_id"])).owner_id = new_owner
    response = decide(client, row) if phase == "DECIDE" else resume(client, row, saved)
    assert response.status_code == 404, response.text
    assert count(client, HumanReviewResume) == 0


def test_parent_success_allows_human_handoff_without_reopening_model_execution(
    client, settings, model_service, reviewer_case
):
    row, outcome, _, _, sent = human_case(client, settings, model_service, reviewer_case)
    calls = len(sent)
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, reviewer_case["run_id"]).status = "SUCCEEDED"
    saved = decide(client, row)
    assert saved.status_code == 200, saved.text
    assert resume(client, row, saved.json()).status_code == 200
    assert len(sent) == calls
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, reviewer_case["run_id"]).status == "SUCCEEDED"
        assert db.get(AgentLoop, outcome.loop_id).status == "COMPLETED"


@pytest.mark.parametrize("failure", ["REQUEST", "RESUME"])
def test_human_transaction_failure_rolls_back_effect_and_recovers_exactly_once(
    client, settings, model_service, reviewer_case, monkeypatch, failure
):
    if failure == "REQUEST":
        install_reviewer_model(model_service, dynamic=False, difference=("STATE", "HIGH"))
        assert run_review(client, settings, reviewer_case).status == "COMPLETED"
        routed, _ = route(client, settings, reviewer_case)
        original = escalations.request_review

        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("human request interruption")

        with monkeypatch.context() as patch:
            patch.setattr(escalations, "request_review", crash)
            with pytest.raises(RuntimeError, match="request interruption"):
                consume_stage_event(client.app.state.session_factory, routed.event_id)
        assert count(client, HumanReviewRequest) == 0
        with client.app.state.session_factory() as db:
            assert (
                db.scalar(
                    select(VersionBinding).where(
                        VersionBinding.resource_type == ESCALATION_RESOURCE
                    )
                )
                is None
            )
        assert (
            consume_stage_event(client.app.state.session_factory, routed.event_id).disposition
            == "APPLIED"
        )
        assert count(client, HumanReviewRequest) == 1
    else:
        row, *_ = human_case(client, settings, model_service, reviewer_case)
        saved = decide(client, row).json()
        original = human_reviews.record_resume

        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("human resume interruption")

        with monkeypatch.context() as patch:
            patch.setattr(human_reviews, "record_resume", crash)
            with pytest.raises(RuntimeError, match="resume interruption"):
                resume(client, row, saved)
        assert count(client, HumanReviewResume) == 0
        with client.app.state.session_factory() as db:
            assert (
                db.scalar(
                    select(AuditEvent).where(AuditEvent.action == "continuity_review.human_handled")
                )
                is None
            )
        assert resume(client, row, saved).status_code == 200
        assert count(client, HumanReviewResume) == 1


@pytest.mark.parametrize("original_conclusion", ["PASS", "HUMAN_REVIEW"])
def test_new_exhaustion_blocks_old_result_even_before_request_repair_and_valid_rerun_recovers(
    client, episode, settings, model_service, reviewer_case, monkeypatch, original_conclusion
):
    if original_conclusion == "HUMAN_REVIEW":
        row, _, first, _, _ = human_case(client, settings, model_service, reviewer_case)
        saved = decide(client, row).json()
    else:
        install_reviewer_model(model_service, dynamic=False)
        assert run_review(client, settings, reviewer_case).status == "COMPLETED"
        first, _ = route(client, settings, reviewer_case)
    with client.app.state.session_factory() as db:
        previous = require_review_record(
            db, UUID(reviewer_case["project_id"]), first.review_id, reviewer_case["actor"]
        )
    version = (
        approve(client, reviewer_case)
        if original_conclusion == "PASS"
        else target_rows(client, reviewer_case)[1]
    )
    next_case = new_review_run(client, episode, settings, reviewer_case)
    with client.app.state.session_factory.begin() as db:
        configure_agent_run(
            db, next_case["run_id"], next_case["actor"], AgentRunLimits(max_model_calls=1)
        )
    sent, errors, _ = install_reviewer_model(model_service)

    def crash(*args, **kwargs):
        raise RuntimeError("stop persisted before request")

    with monkeypatch.context() as patch:
        patch.setattr(escalations, "request_escalation", crash)
        with pytest.raises(RuntimeError, match="stop persisted"):
            run_review(client, settings, next_case)
    assert len(sent) == 1 and not errors
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(select(AgentLoop).where(AgentLoop.run_id == next_case["run_id"])).status
            == "STOPPED"
        )
        with pytest.raises(DomainError) as denied:
            require_shot_review(db, db.get(AssetVersion, UUID(version["id"])))
        assert denied.value.code == "REVIEW_NOT_COMPLETED"
    if original_conclusion == "HUMAN_REVIEW":
        assert resume(client, row, saved).status_code == 409
    # Reentry repairs the missing wait without another provider request.
    from datetime import timedelta

    from arclight.persistence.base import utcnow

    expired = utcnow() + timedelta(days=1)
    with monkeypatch.context() as patch:
        patch.setattr("arclight.application.agent_limits.utcnow", lambda: expired)
        assert run_review(client, settings, next_case).status == "STOPPED"
    assert pending(client, next_case)["status"] == "WAITING_REVIEW"
    assert len(sent) == 1
    last_case = new_review_run(client, episode, settings, next_case)
    with client.app.state.session_factory.begin() as db:
        configure_agent_run(db, last_case["run_id"], last_case["actor"], AgentRunLimits())
    install_reviewer_model(model_service, dynamic=False)
    assert (
        run_review(
            client,
            settings,
            last_case,
            rerun=ReviewRerun(
                previous_review_id=first.review_id,
                previous_sha256=digest(previous.model_dump(mode="json")),
                reason="Fresh complete evidence review",
            ),
        ).status
        == "COMPLETED"
    )
    route(client, settings, last_case)
    asset, _ = target_rows(client, reviewer_case)
    version = approve(client, reviewer_case) if original_conclusion == "HUMAN_REVIEW" else version
    selected = choose(client, current(client, asset), version)
    assert selected.status_code == 200, selected.text


def test_production_exception_needs_committed_handoff_before_formal_selection(
    client, settings, model_service, reviewer_case
):
    row, *_ = human_case(client, settings, model_service, reviewer_case)
    from test_asset_versions import state

    saved = decide(client, row)
    assert saved.status_code == 200, saved.text
    asset, version = target_rows(client, reviewer_case)
    blocked = state(client, asset, version, "READY_FOR_REVIEW")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "HUMAN_DECISION_REQUIRED"
    assert choose(client, asset, version).status_code == 409
    assert resume(client, row, saved.json()).status_code == 200
    version = approve(client, reviewer_case)
    assert choose(client, current(client, asset), version).status_code == 200


@pytest.mark.parametrize("phase", ["DECIDE", "RESUME"])
def test_new_complete_rerun_blocks_superseded_human_decision_and_resume(
    client, episode, settings, model_service, reviewer_case, phase
):
    row, _, first, _, _ = human_case(client, settings, model_service, reviewer_case)
    saved = decide(client, row).json() if phase == "RESUME" else None
    with client.app.state.session_factory() as db:
        previous = require_review_record(
            db, UUID(reviewer_case["project_id"]), first.review_id, reviewer_case["actor"]
        )
    case = new_review_run(client, episode, settings, reviewer_case)
    install_reviewer_model(model_service, dynamic=False)
    outcome = run_review(
        client,
        settings,
        case,
        rerun=ReviewRerun(
            previous_review_id=first.review_id,
            previous_sha256=digest(previous.model_dump(mode="json")),
            reason="Complete fresh review rather than using an older exception",
        ),
    )
    assert outcome.status == "COMPLETED", outcome.model_dump_json()
    assert route(client, settings, case)[0].conclusion == "PASS"
    response = decide(client, row) if phase == "DECIDE" else resume(client, row, saved)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "REVIEW_RESULT_STALE"
    assert count(client, HumanReviewResume) == 0


def test_started_rerun_blocks_previous_pass_during_model_io_until_new_admission(
    client, episode, settings, model_service, reviewer_case
):
    install_reviewer_model(model_service, dynamic=False)
    assert run_review(client, settings, reviewer_case).status == "COMPLETED"
    first, _ = route(client, settings, reviewer_case)
    version = approve(client, reviewer_case)
    asset, _ = target_rows(client, reviewer_case)
    with client.app.state.session_factory() as db:
        previous = require_review_record(
            db, UUID(reviewer_case["project_id"]), first.review_id, reviewer_case["actor"]
        )
    case = new_review_run(client, episode, settings, reviewer_case)
    blocked = []

    def during_io(_):
        response = choose(client, current(client, asset), version)
        blocked.append(response.status_code)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "REVIEW_NOT_COMPLETED"

    _, errors, _ = install_reviewer_model(model_service, dynamic=False, on_http=during_io)
    outcome = run_review(
        client,
        settings,
        case,
        rerun=ReviewRerun(
            previous_review_id=first.review_id,
            previous_sha256=digest(previous.model_dump(mode="json")),
            reason="Recheck current evidence",
        ),
    )
    assert not errors, errors
    assert outcome.status == "COMPLETED" and blocked
    route(client, settings, case)
    assert choose(client, current(client, asset), version).status_code == 200
