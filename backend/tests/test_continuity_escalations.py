"""QA-06 exercises actual Reviewer results and the production human-review API."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_asset_selection import choose, current
from test_asset_versions import state
from test_concurrency import require_postgres
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_reviewer import run_review
from test_continuity_routing import route
from test_continuity_rules import rules_storage as rules_storage
from test_continuity_selection import approve, target_rows
from test_human_reviews import decide, decision_payload, resume

from arclight.application.continuity_escalations import request_escalation
from arclight.application.continuity_review_store import REVIEW_RESOURCE
from arclight.application.human_reviews import resume_review, submit_decision
from arclight.application.production_events import consume_stage_event
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.continuity_escalations import ESCALATION_RESOURCE, ContinuityEscalation
from arclight.domain.continuity_reviews import ReviewTarget
from arclight.domain.continuity_selection import ADMISSION_RESOURCE
from arclight.domain.human_reviews import HumanDecision, ReviewResume
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    Storyboard,
    VersionBinding,
    WorkflowRun,
)


def count(client, model):
    with client.app.state.session_factory() as db:
        statement = select(func.count()).select_from(model)
        if model in {HumanReviewDecision, HumanReviewResume}:
            statement = statement.join(
                HumanReviewRequest, model.request_id == HumanReviewRequest.id
            )
        if model in {HumanReviewRequest, HumanReviewDecision, HumanReviewResume}:
            statement = statement.where(
                HumanReviewRequest.handler.in_(["continuity_shot", "continuity_incomplete"])
            )
        return db.scalar(statement)


def pending(client, case):
    with client.app.state.session_factory() as db:
        row = db.scalar(
            select(HumanReviewRequest).where(HumanReviewRequest.run_id == case["run_id"])
        )
        assert row is not None
        identity = row.id
    response = client.get(f"/api/v1/human-reviews/{identity}")
    assert response.status_code == 200, response.text
    return response.json()


def human_case(client, settings, model_service, case, **options):
    sent, errors, _ = install_reviewer_model(
        model_service, dynamic=False, **(options or {"difference": ("STATE", "HIGH")})
    )
    outcome = run_review(client, settings, case)
    assert not errors, errors
    assert outcome.status == "COMPLETED", outcome.model_dump_json()
    routed, record = route(client, settings, case)
    assert routed.conclusion == "HUMAN_REVIEW"
    receipt = consume_stage_event(client.app.state.session_factory, routed.event_id)
    assert receipt.disposition == "APPLIED", receipt
    return pending(client, case), outcome, routed, record, sent


@pytest.mark.parametrize("rules_storage", ["local", "s3"], indirect=True)
@pytest.mark.parametrize(
    "reason",
    ["HIGH", "CRITICAL", "OCCLUDED", "MISSING_REFERENCE", "LOW_VISIBILITY", "CONTRADICTION"],
)
def test_actual_human_results_persist_exact_reasons_and_idempotent_event(
    client, settings, model_service, reviewer_case, reason
):
    options = (
        {"difference": ("STATE", reason)}
        if reason in {"HIGH", "CRITICAL"}
        else {"uncertainty": {"reason": reason, "detail": "Cannot establish this detail."}}
    )
    row, outcome, routed, record, sent = human_case(
        client, settings, model_service, reviewer_case, **options
    )
    with client.app.state.session_factory() as db:
        binding = db.scalar(
            select(VersionBinding).where(VersionBinding.resource_type == ESCALATION_RESOURCE)
        )
        pause = ContinuityEscalation.model_validate(binding.snapshot_json["continuity_escalation"])
        assert pause.loop_id == outcome.loop_id
        assert pause.thread_id == f"agent-loop:{outcome.loop_id}"
        assert pause.review_id == outcome.result.result_id
        assert pause.target == outcome.result.target
        assert ("RISK_" + reason if reason in {"HIGH", "CRITICAL"} else reason) in pause.reasons
        if reason not in {"HIGH", "CRITICAL"}:
            assert "LOW_CONFIDENCE" in pause.reasons
        original = deepcopy(binding.snapshot_json)
    assert row["status"] == "WAITING_REVIEW"
    assert row["snapshot"]["object_id"] == str(routed.review_id)
    assert row["snapshot"]["thread_id"] == str(reviewer_case["run_id"])
    assert row["snapshot"]["version_id"] == reviewer_case["target"]["asset_version_id"]
    assert (
        consume_stage_event(client.app.state.session_factory, routed.event_id).disposition
        == "APPLIED"
    )
    with client.app.state.session_factory.begin() as db:
        assert request_escalation(
            db,
            reviewer_case["run_id"],
            reviewer_case["actor"],
            ReviewTarget.model_validate(reviewer_case["target"]),
        ).id == UUID(row["id"])
        assert db.get(VersionBinding, binding.id).snapshot_json == original
    assert count(client, HumanReviewRequest) == 1
    assert len(sent) == outcome.model_turns


@pytest.mark.parametrize("answer", ["APPROVE", "REJECT"])
def test_owner_decision_and_resume_preserve_review_and_explicit_asset_controls(
    client, settings, model_service, reviewer_case, answer
):
    row, outcome, routed, record, sent = human_case(client, settings, model_service, reviewer_case)
    calls = len(sent)
    asset, version = target_rows(client, reviewer_case)
    before = deepcopy(version)
    blank = client.post(
        f"/api/v1/human-reviews/{row['id']}/decisions", json=decision_payload(row, answer, "  ")
    )
    assert blank.status_code == 422, blank.text
    saved = decide(client, row, answer)
    assert saved.status_code == 200, saved.text
    assert decide(client, row, answer).json()["id"] == saved.json()["id"]
    assert decide(client, row, "REJECT" if answer == "APPROVE" else "APPROVE").status_code == 409
    handoff = resume(client, row, saved.json())
    assert handoff.status_code == 200, handoff.text
    assert handoff.json()["result_json"]["status"] == (
        "APPROVED" if answer == "APPROVE" else "REJECTED"
    )
    assert resume(client, row, saved.json()).json() == handoff.json()
    assert len(sent) == calls
    asset, version = target_rows(client, reviewer_case)
    assert version["status"] == before["status"]
    assert asset["current_version_id"] is None and asset["status"] != "LOCKED"
    assert choose(client, asset, version).status_code == 409
    if answer == "APPROVE":
        version = approve(client, reviewer_case)
        selected = choose(client, current(client, asset), version)
        assert selected.status_code == 200, selected.text
    else:
        blocked = state(client, asset, version, "READY_FOR_REVIEW")
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"]["code"] == "HUMAN_DECISION_REQUIRED"
    with client.app.state.session_factory() as db:
        loop = db.get(AgentLoop, outcome.loop_id)
        assert loop.status == "COMPLETED"
        assert loop.outcome_json["result"]["conclusion"] == "HUMAN_REVIEW"
        assert count(client, HumanReviewDecision) == count(client, HumanReviewResume) == 1


@pytest.mark.parametrize("reviewer_case", [AgentRunLimits(max_model_calls=1)], indirect=True)
def test_real_exhaustion_is_durable_cannot_be_approved_and_never_resets_limits(
    client, settings, model_service, reviewer_case
):
    sent, errors, _ = install_reviewer_model(model_service)
    outcome = run_review(client, settings, reviewer_case)
    assert outcome.status == "STOPPED" and outcome.stop_code == "AGENT_MODEL_CALL_LIMIT"
    assert not errors and len(sent) == 1
    row = pending(client, reviewer_case)
    with client.app.state.session_factory() as db:
        assert db.get(AgentLoop, outcome.loop_id).status == "STOPPED"
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type.in_([REVIEW_RESOURCE, ADMISSION_RESOURCE]))
            )
            == 0
        )
    denied = decide(client, row)
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "REVIEW_NOT_COMPLETED"
    saved = decide(client, row, "REJECT")
    assert saved.status_code == 200, saved.text
    assert resume(client, row, saved.json()).status_code == 200
    assert run_review(client, settings, reviewer_case) == outcome
    assert pending(client, reviewer_case)["status"] == "RESUMED"
    assert len(sent) == 1 and count(client, HumanReviewRequest) == 1
    with pytest.raises(DomainError) as changed:
        run_review(client, settings, reviewer_case, limits=AgentRunLimits(max_model_calls=100))
    assert changed.value.code == "AGENT_LIMITS_CONFLICT"
    asset, version = target_rows(client, reviewer_case)
    blocked = state(client, asset, version, "READY_FOR_REVIEW")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    assert choose(client, asset, version).status_code == 409


def test_real_budget_refusal_persists_human_wait_without_sending_or_minting_review(
    client, episode, settings, model_service, reviewer_case
):
    from test_director_planning import activate
    from test_generation_budget import set_policy

    from arclight.application.agent_limits import configure_agent_run
    from arclight.application.agent_tools import build_agent_tool_registry, grant_agent_tools
    from arclight.domain.agent_tools import AgentToolGrantRequest
    from arclight.domain.model_gateway import GatewayPolicy
    from arclight.persistence.agent_limits import AgentRunLimit

    set_policy(client, reviewer_case["project_id"], "100")
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, reviewer_case["run_id"]).status = "SUCCEEDED"
    # Pricing is frozen at run creation; changing current settings must not alter it.
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost="1", pricing_version="qa06-budget:1"
    )
    created = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboard-runs",
        headers={"Idempotency-Key": uuid4().hex},
        json={"script_version_id": reviewer_case["source"]["id"]},
    )
    assert created.status_code == 202, created.text
    run_id = UUID(created.json()["run_id"])
    actor = activate(client, episode, settings, run_id)
    case = reviewer_case | {"run_id": run_id, "actor": actor}
    with client.app.state.session_factory.begin() as db:
        configure_agent_run(db, run_id, actor, AgentRunLimits())
        grant_agent_tools(
            db,
            AgentToolGrantRequest(
                run_id=run_id,
                agent_name="continuity_reviewer",
                allowed_tools=["investigate_continuity", "check_continuity_rules"],
            ),
            actor,
            build_agent_tool_registry(settings=settings),
        )
        control = db.get(AgentRunLimit, run_id)
        original = (deepcopy(control.limits_json), control.deadline_at)
    sent, errors, _ = install_reviewer_model(model_service)
    outcome = run_review(client, settings, case)
    assert outcome.status == "STOPPED" and outcome.stop_code == "AGENT_BUDGET_EXCEEDED"
    assert sent == [] and not errors
    row = pending(client, case)
    assert decide(client, row).json()["error"]["code"] == "REVIEW_NOT_COMPLETED"
    saved = decide(client, row, "REJECT")
    assert saved.status_code == 200, saved.text
    assert resume(client, row, saved.json()).status_code == 200
    with client.app.state.session_factory() as db:
        control = db.get(AgentRunLimit, run_id)
        assert (control.limits_json, control.deadline_at) == original
        assert db.get(AgentLoop, outcome.loop_id).status == "STOPPED"
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type.in_([REVIEW_RESOURCE, ADMISSION_RESOURCE]))
            )
            == 0
        )
    assert sent == []


@pytest.mark.parametrize("field", ["version_id", "request_sha256"])
def test_decision_cannot_change_exact_version_or_request_digest(
    client, settings, model_service, reviewer_case, field
):
    row, *_ = human_case(client, settings, model_service, reviewer_case)
    payload = decision_payload(row)
    payload[field] = str(uuid4()) if field == "version_id" else "0" * 64
    response = client.post(f"/api/v1/human-reviews/{row['id']}/decisions", json=payload)
    assert response.status_code == 409, response.text
    assert count(client, HumanReviewDecision) == 0


@pytest.mark.parametrize("phase", ["DECIDE", "RESUME"])
@pytest.mark.parametrize("change", ["candidate", "reference", "board", "cancel", "archive"])
def test_current_inputs_and_lifecycle_are_rechecked_before_human_handling(
    client, settings, model_service, reviewer_case, phase, change
):
    row, *_ = human_case(client, settings, model_service, reviewer_case)
    saved = decide(client, row).json() if phase == "RESUME" else None
    with client.app.state.session_factory.begin() as db:
        if change in {"candidate", "reference"}:
            identity = (
                reviewer_case["target"]["asset_version_id"]
                if change == "candidate"
                else reviewer_case["refs"]["PROP"]["asset_version_id"]
            )
            db.get(AssetVersion, UUID(identity)).stale = True
        elif change == "board":
            db.get(Storyboard, UUID(reviewer_case["target"]["storyboard_id"])).status = "REJECTED"
        elif change == "cancel":
            db.get(WorkflowRun, reviewer_case["run_id"]).status = "CANCELLED"
        else:
            from arclight.persistence.models import Project

            db.get(Project, UUID(reviewer_case["project_id"])).archived = True
    response = decide(client, row) if phase == "DECIDE" else resume(client, row, saved)
    if change == "reference":
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "ASSET_NOT_USABLE"
    else:
        assert response.status_code in {404, 409}, response.text
    assert count(client, HumanReviewResume) == 0
    assert count(client, HumanReviewDecision) == (1 if phase == "RESUME" else 0)


@pytest.mark.parametrize("operation", ["REQUEST", "DECISION", "RESUME"])
def test_postgres_parallel_human_handling_has_one_durable_effect(
    client, settings, model_service, reviewer_case, operation
):
    require_postgres(settings)
    row, *_ = human_case(client, settings, model_service, reviewer_case)
    saved = decide(client, row).json() if operation == "RESUME" else None
    factory = client.app.state.session_factory

    def invoke(_):
        with factory.begin() as db:
            if operation == "REQUEST":
                return request_escalation(
                    db,
                    reviewer_case["run_id"],
                    reviewer_case["actor"],
                    ReviewTarget.model_validate(reviewer_case["target"]),
                ).id
            if operation == "DECISION":
                return submit_decision(
                    db,
                    UUID(row["id"]),
                    reviewer_case["actor"],
                    HumanDecision.model_validate(decision_payload(row)),
                ).id
            return resume_review(
                db,
                UUID(row["id"]),
                reviewer_case["actor"],
                ReviewResume(decision_id=UUID(saved["id"]), request_sha256=row["request_sha256"]),
            ).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(invoke, range(2)))
    assert first == second
    assert count(client, HumanReviewRequest) == 1
    assert count(client, HumanReviewDecision) == (0 if operation == "REQUEST" else 1)
    assert count(client, HumanReviewResume) == (1 if operation == "RESUME" else 0)
    with factory() as db:
        assert db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "continuity_review.human_handled")
        ) == (1 if operation == "RESUME" else 0)
