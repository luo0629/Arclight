"""QA-04 routes actual evidence-driven reviews through the existing production Outbox."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_asset_selection import choose, current
from test_concurrency import require_postgres
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_reviewer import run_review
from test_continuity_rules import rules_storage as rules_storage
from test_continuity_selection import approve, target_rows
from test_director_planning import prepare

from arclight.application.agent_tools import build_agent_tool_registry, grant_agent_tools
from arclight.application.continuity_review_store import REVIEW_RESOURCE, require_review_record
from arclight.application.continuity_routing import read_routed_review, route_reviewer_result
from arclight.application.continuity_selection import require_shot_review
from arclight.application.production_events import consume_stage_event, publish_stage_events
from arclight.core.errors import DomainError
from arclight.domain.agent_tools import AgentToolGrantRequest
from arclight.domain.continuity_reviews import ReviewRerun, ReviewTarget
from arclight.domain.continuity_selection import ADMISSION_RESOURCE
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import (
    AgentToolGrant,
    AssetVersion,
    AuditEvent,
    OutboxEvent,
    Storyboard,
    VersionBinding,
    VisualAsset,
    WorkflowRun,
)
from arclight.persistence.production_events import ProductionEvent, ProductionEventReceipt


def route(client, settings, case):
    with client.app.state.session_factory.begin() as db:
        return route_reviewer_result(
            db, settings, case["run_id"], case["actor"], ReviewTarget.model_validate(case["target"])
        )


def route_counts(client):
    with client.app.state.session_factory() as db:
        return {
            "records": db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == REVIEW_RESOURCE)
            ),
            "admissions": db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == ADMISSION_RESOURCE)
            ),
            "events": db.scalar(
                select(func.count())
                .select_from(ProductionEvent)
                .where(ProductionEvent.source_kind == "REVIEW")
            ),
            "audit": db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "continuity_review.routed")
            ),
        }


@pytest.mark.parametrize(
    "difference,conclusion,destination",
    [
        (None, "PASS", "READY_FOR_REVIEW"),
        (("PROP", "MEDIUM"), "REVISION_REQUIRED", "REVISION_REQUIRED"),
        (("STATE", "HIGH"), "HUMAN_REVIEW", "HUMAN_REVIEW"),
    ],
)
@pytest.mark.parametrize("rules_storage", ["local", "s3"], indirect=True)
def test_real_review_routes_all_conclusions_without_selecting(
    client, settings, model_service, reviewer_case, difference, conclusion, destination
):
    sent, errors, _ = install_reviewer_model(model_service, dynamic=False, difference=difference)
    outcome = run_review(client, settings, reviewer_case)
    assert not errors, errors
    assert outcome.status == "COMPLETED", outcome.model_dump_json()
    assert outcome.result.conclusion == conclusion
    assert route_counts(client) == dict(records=0, admissions=0, events=0, audit=0)
    before = target_rows(client, reviewer_case)
    routed, created = route(client, settings, reviewer_case)
    assert created and routed.conclusion == conclusion and routed.destination == destination
    assert routed.loop_id == outcome.loop_id
    assert target_rows(client, reviewer_case) == before
    assert route(client, settings, reviewer_case) == (routed, False)
    assert route_counts(client) == dict(records=1, admissions=1, events=1, audit=1)
    factory = client.app.state.session_factory
    with factory() as db:
        record, proof = read_routed_review(db, routed.review_id)
        assert proof == routed and record.review == outcome.result
        assert record.state == "CONTRACT_VALIDATED"
        event = db.get(ProductionEvent, routed.event_id)
        assert event.source_kind == "REVIEW" and event.source_id == routed.review_id
        messages = list(
            db.scalars(select(OutboxEvent).where(OutboxEvent.production_event_id == event.id))
        )
        assert len(messages) == 1 and messages[0].event_type == "production.coordinate"
        asset = db.get(VisualAsset, UUID(reviewer_case["target"]["asset_id"]))
        assert asset.current_version_id is None
    published = []
    publish_stage_events(factory, published.append)
    publish_stage_events(factory, published.append)
    assert published.count(routed.event_id) == 2  # Lost deliveries remain replayable.
    receipt = consume_stage_event(factory, routed.event_id)
    assert receipt.disposition == "APPLIED", receipt.model_dump_json()
    assert receipt.action == destination
    assert consume_stage_event(factory, routed.event_id) == receipt
    assert target_rows(client, reviewer_case) == before
    with factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(ProductionEventReceipt)
                .where(ProductionEventReceipt.event_id == routed.event_id)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == "production.review_routed",
                    AuditEvent.resource_id == routed.review_id,
                )
            )
            == 1
        )
    assert len(sent) == outcome.model_turns


def test_routed_pass_requires_original_owner_approval_and_explicit_selection(
    client, settings, model_service, reviewer_case
):
    install_reviewer_model(model_service, dynamic=False)
    assert run_review(client, settings, reviewer_case).status == "COMPLETED"
    routed, _ = route(client, settings, reviewer_case)
    asset, version = target_rows(client, reviewer_case)
    assert choose(client, asset, version).status_code == 409
    version = approve(client, reviewer_case)
    assert current(client, asset)["current_version_id"] is None
    selected = choose(client, current(client, asset), version)
    assert selected.status_code == 200, selected.text
    assert selected.json()["current_version_id"] == version["id"]
    assert routed.destination == "READY_FOR_REVIEW"


@pytest.mark.parametrize(
    "difference,code",
    [
        (("PROP", "MEDIUM"), "CONTINUITY_REVISION_REQUIRED"),
        (("STATE", "HIGH"), "HUMAN_DECISION_REQUIRED"),
    ],
)
def test_revision_and_human_routes_keep_original_selection_gate(
    client, settings, model_service, reviewer_case, difference, code
):
    install_reviewer_model(model_service, dynamic=False, difference=difference)
    assert run_review(client, settings, reviewer_case).status == "COMPLETED"
    route(client, settings, reviewer_case)
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(reviewer_case["target"]["asset_version_id"]))
        with pytest.raises(DomainError) as error:
            require_shot_review(db, version)
        assert error.value.code == code
    asset, version = target_rows(client, reviewer_case)
    assert choose(client, asset, version).status_code == 409


@pytest.mark.parametrize("human", [False, True])
def test_missing_stopped_or_paused_loop_cannot_be_routed(
    client, settings, model_service, reviewer_case, human
):
    with pytest.raises(DomainError) as error:
        route(client, settings, reviewer_case)
    assert error.value.code == "REVIEW_LOOP_INVALID"
    install_reviewer_model(model_service, dynamic=False, human=human, bypass=not human)
    outcome = run_review(client, settings, reviewer_case)
    assert outcome.status == ("HUMAN_REVIEW" if human else "STOPPED")
    with pytest.raises(DomainError) as error:
        route(client, settings, reviewer_case)
    assert error.value.code == "REVIEW_NOT_COMPLETED"
    assert route_counts(client) == dict(records=0, admissions=0, events=0, audit=0)


@pytest.mark.parametrize("change", ["summary", "result_id", "loop_id", "conclusion", "versions"])
def test_forged_persisted_outcome_cannot_replace_final_model_receipt(
    client, settings, model_service, reviewer_case, change
):
    install_reviewer_model(model_service, dynamic=False)
    outcome = run_review(client, settings, reviewer_case)
    assert outcome.status == "COMPLETED"
    with client.app.state.session_factory.begin() as db:
        loop = db.get(AgentLoop, outcome.loop_id)
        value = deepcopy(loop.outcome_json)
        if change == "summary":
            value["result"]["summary"] = "Forged replacement"
        elif change == "result_id":
            value["result"]["result_id"] = str(uuid4())
        elif change == "loop_id":
            value["loop_id"] = str(uuid4())
        elif change == "conclusion":
            value["result"].update(conclusion="HUMAN_REVIEW", suggested_action="HUMAN_REVIEW")
        else:
            value["result"]["versions"][0]["digest"] = "f" * 64
        loop.outcome_json = value
    with pytest.raises(DomainError) as error:
        route(client, settings, reviewer_case)
    assert error.value.code == "REVIEW_DELIVERY_INVALID"
    assert route_counts(client) == dict(records=0, admissions=0, events=0, audit=0)


@pytest.mark.parametrize("change", ["stale", "board", "grant", "reference", "actor"])
def test_route_revalidates_current_candidate_reference_board_and_authority(
    client, settings, model_service, reviewer_case, change
):
    install_reviewer_model(model_service, dynamic=False)
    assert run_review(client, settings, reviewer_case).status == "COMPLETED"
    case = deepcopy(reviewer_case)
    with client.app.state.session_factory.begin() as db:
        if change == "stale":
            db.get(AssetVersion, UUID(case["target"]["asset_version_id"])).stale = True
        elif change == "board":
            db.get(Storyboard, UUID(case["target"]["storyboard_id"])).status = "REJECTED"
        elif change == "grant":
            db.scalar(
                select(AgentToolGrant).where(
                    AgentToolGrant.run_id == case["run_id"],
                    AgentToolGrant.agent_name == "continuity_reviewer",
                )
            ).allowed_tools_json = []
        elif change == "reference":
            db.get(AssetVersion, UUID(case["refs"]["PROP"]["asset_version_id"])).stale = True
        else:
            case["actor"] = str(uuid4())
    with pytest.raises(DomainError):
        route(client, settings, case)
    assert route_counts(client) == dict(records=0, admissions=0, events=0, audit=0)


def test_outbox_failure_rolls_back_review_admission_and_audit(
    client, settings, model_service, reviewer_case, monkeypatch
):
    from arclight.application import production_events

    sent, _, _ = install_reviewer_model(model_service, dynamic=False)
    outcome = run_review(client, settings, reviewer_case)
    original = production_events.enqueue_source

    def fail(*args, **kwargs):
        raise RuntimeError("simulated transaction interruption")

    monkeypatch.setattr(production_events, "enqueue_source", fail)
    with pytest.raises(RuntimeError, match="interruption"):
        route(client, settings, reviewer_case)
    assert route_counts(client) == dict(records=0, admissions=0, events=0, audit=0)
    monkeypatch.setattr(production_events, "enqueue_source", original)
    routed, created = route(client, settings, reviewer_case)
    assert created and routed.loop_id == outcome.loop_id
    assert len(sent) == outcome.model_turns


def new_review_run(client, episode, settings, case):
    # The fixture activates an internal storyboard step without executing its parent graph.
    # Complete that parent fixture before starting the independent rerun through the API.
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, case["run_id"]).status = "SUCCEEDED"
    run_id, actor, _ = prepare(client, episode, settings, source=case["source"])
    with client.app.state.session_factory.begin() as db:
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
    return case | {"run_id": run_id, "actor": actor}


def test_latest_rerun_invalidates_old_pass_and_pending_event_without_rewriting_history(
    client, episode, settings, model_service, reviewer_case
):
    install_reviewer_model(model_service, dynamic=False)
    first = run_review(client, settings, reviewer_case)
    original, _ = route(client, settings, reviewer_case)
    with client.app.state.session_factory() as db:
        record = require_review_record(
            db, first.project_id, original.review_id, reviewer_case["actor"]
        )
    case = new_review_run(client, episode, settings, reviewer_case)
    install_reviewer_model(model_service, dynamic=False, difference=("PROP", "MEDIUM"))
    rerun = ReviewRerun(
        previous_review_id=original.review_id,
        previous_sha256=digest(record.model_dump(mode="json")),
        reason="Recheck a newly identified prop difference.",
    )
    second = run_review(client, settings, case, rerun=rerun)
    assert second.status == "COMPLETED", second.model_dump_json()
    current_route, _ = route(client, settings, case)
    assert current_route.conclusion == "REVISION_REQUIRED"
    with pytest.raises(DomainError) as error, client.app.state.session_factory() as db:
        read_routed_review(db, original.review_id)
    assert error.value.code == "REVIEW_RESULT_STALE"
    receipt = consume_stage_event(client.app.state.session_factory, original.event_id)
    assert receipt.disposition == "STALE", receipt.model_dump_json()
    with client.app.state.session_factory() as db:
        assert (
            require_review_record(db, first.project_id, original.review_id, case["actor"]) == record
        )
        version = db.get(AssetVersion, UUID(case["target"]["asset_version_id"]))
        with pytest.raises(DomainError) as error:
            require_shot_review(db, version)
        assert error.value.code == "CONTINUITY_REVISION_REQUIRED"


@pytest.mark.parametrize("change", ["stale", "grant", "board"])
def test_queued_event_rechecks_inputs_instead_of_promoting_a_stale_pass(
    client, settings, model_service, reviewer_case, change
):
    install_reviewer_model(model_service, dynamic=False)
    run_review(client, settings, reviewer_case)
    routed, _ = route(client, settings, reviewer_case)
    with client.app.state.session_factory.begin() as db:
        if change == "stale":
            db.get(AssetVersion, UUID(reviewer_case["target"]["asset_version_id"])).stale = True
        elif change == "board":
            db.get(Storyboard, UUID(reviewer_case["target"]["storyboard_id"])).status = "REJECTED"
        else:
            # Tool authorization controls producing new decisions; a completed immutable
            # receipt remains a fact. Project authority is still checked by the consumer.
            db.scalar(
                select(AgentToolGrant).where(
                    AgentToolGrant.run_id == reviewer_case["run_id"],
                    AgentToolGrant.agent_name == "continuity_reviewer",
                )
            ).allowed_tools_json = []
    receipt = consume_stage_event(client.app.state.session_factory, routed.event_id)
    assert receipt.disposition == ("APPLIED" if change == "grant" else "BLOCKED")
    assert target_rows(client, reviewer_case)[0]["current_version_id"] is None


def test_postgres_duplicate_routes_and_events_commit_one_receipt(
    client, settings, model_service, reviewer_case
):
    require_postgres(settings)
    install_reviewer_model(model_service, dynamic=False)
    run_review(client, settings, reviewer_case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: route(client, settings, reviewer_case), range(2)))
    assert sorted(created for _, created in results) == [False, True]
    assert results[0][0] == results[1][0]
    assert route_counts(client) == dict(records=1, admissions=1, events=1, audit=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(
                lambda _: consume_stage_event(
                    client.app.state.session_factory, results[0][0].event_id
                ),
                range(2),
            )
        )
    assert receipts[0] == receipts[1] and receipts[0].disposition == "APPLIED"
