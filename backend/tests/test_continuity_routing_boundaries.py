"""Authority, delayed delivery and transactional recovery at the routing boundary."""

from datetime import UTC, timedelta
from uuid import uuid4

import pytest
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_reviewer import run_review
from test_continuity_routing import route, route_counts
from test_continuity_rules import rules_storage as rules_storage

from arclight.application import agent_limits
from arclight.application.production_event_handlers import HANDLERS, ReviewStageHandler
from arclight.application.production_events import consume_stage_event
from arclight.core.errors import DomainError
from arclight.domain.production_events import StageEvent
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.models import AuditEvent
from arclight.persistence.production_events import ProductionEvent, ProductionEventReceipt


def test_delayed_result_after_original_deadline_cannot_open_admission(
    client, settings, model_service, reviewer_case, monkeypatch
):
    install_reviewer_model(model_service, dynamic=False)
    assert run_review(client, settings, reviewer_case).status == "COMPLETED"
    with client.app.state.session_factory() as db:
        control = db.get(AgentRunLimit, reviewer_case["run_id"])
        deadline = control.deadline_at.replace(tzinfo=UTC)
    monkeypatch.setattr(agent_limits, "utcnow", lambda: deadline + timedelta(seconds=1))
    with pytest.raises(DomainError) as error:
        route(client, settings, reviewer_case)
    assert error.value.code == "AGENT_DEADLINE_EXCEEDED"
    assert route_counts(client) == dict(records=0, admissions=0, events=0, audit=0)


@pytest.mark.parametrize("field", ["candidate_id", "shot_id", "storyboard_id"])
def test_caller_cannot_retarget_a_completed_model_result(
    client, settings, model_service, reviewer_case, field
):
    install_reviewer_model(model_service, dynamic=False)
    assert run_review(client, settings, reviewer_case).status == "COMPLETED"
    case = reviewer_case | {"target": reviewer_case["target"] | {field: str(uuid4())}}
    with pytest.raises(DomainError) as error:
        route(client, settings, case)
    assert error.value.code == "REVIEW_LOOP_INVALID"
    assert route_counts(client) == dict(records=0, admissions=0, events=0, audit=0)


def test_review_event_without_server_route_is_blocked(client, episode):
    event = StageEvent(
        event_id=uuid4(),
        project_id=episode["project_id"],
        episode_id=episode["id"],
        source_kind="REVIEW",
        source_id=uuid4(),
        source_revision=1,
        actor_id="service-owner",
        input_sha256="f" * 64,
        script_version_id=None,
        storyboard_version_id=None,
    )
    factory = client.app.state.session_factory
    with factory.begin() as db:
        db.add(
            ProductionEvent(
                id=event.event_id,
                project_id=event.project_id,
                episode_id=event.episode_id,
                source_kind=event.source_kind,
                source_id=event.source_id,
                source_revision=event.source_revision,
                request_sha256=digest(event.model_dump(mode="json")),
                event_json=event.model_dump(mode="json"),
            )
        )
    receipt = consume_stage_event(factory, event.event_id)
    assert receipt.disposition == "BLOCKED" and receipt.reason == "REVIEW_ROUTE_REQUIRED"
    assert consume_stage_event(factory, event.event_id) == receipt
    assert route_counts(client)["admissions"] == 0


def test_event_consumer_interruption_replays_without_repeating_effect_or_model_call(
    client, settings, model_service, reviewer_case, monkeypatch
):
    sent, _, _ = install_reviewer_model(model_service, dynamic=False)
    outcome = run_review(client, settings, reviewer_case)
    routed, _ = route(client, settings, reviewer_case)

    class Interrupted(ReviewStageHandler):
        def advance(self, *args, **kwargs):
            super().advance(*args, **kwargs)
            raise RuntimeError("interrupted after domain write")

    handlers = HANDLERS | {"REVIEW": Interrupted()}
    factory = client.app.state.session_factory
    with pytest.raises(RuntimeError, match="after domain write"):
        consume_stage_event(factory, routed.event_id, handlers=handlers)
    with factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(ProductionEventReceipt)
                .where(ProductionEventReceipt.event_id == routed.event_id)
            )
            == 0
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
            == 0
        )
    receipt = consume_stage_event(factory, routed.event_id)
    assert receipt.disposition == "APPLIED" and receipt.action == "READY_FOR_REVIEW"
    assert consume_stage_event(factory, routed.event_id) == receipt
    assert len(sent) == outcome.model_turns
