"""Art delivery rolls back atomically and reuses durable autonomous work."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from art_direction_support import install_direction_model, prepare_direction, reference_set
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from test_agent_loop_recovery import answer
from test_agent_model import model_service as model_service
from test_art_direction import facts
from test_art_review_workflow import effects
from test_concurrency import require_postgres

from arclight.application import art_generation as generation
from arclight.application.art_direction import propose_art, resume_art_direction
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.art_generation import DELIVERY_KEY
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.art_proposals import (
    ArtProposalRecord,
    ArtProposalReview,
    ArtProposalVersion,
)
from arclight.persistence.human_reviews import HumanReviewRequest


@pytest.mark.parametrize("mode", ["human", "missing-images", "limit", "model-error"])
def test_incomplete_direction_creates_no_candidate_or_review(
    client, episode, settings, model_service, mode
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    install_direction_model(
        model_service, human=mode == "human", include_images=mode != "missing-images"
    )
    if mode == "model-error":
        import httpx

        model_service(lambda request: httpx.Response(503, json={"error": "unavailable"}))
    before = facts(client)
    result = generation.generate_art_proposal(
        client.app.state.session_factory,
        settings,
        run_id,
        actor,
        task,
        limits=AgentRunLimits(max_model_calls=1) if mode == "limit" else None,
    )
    assert result.direction.status in {"HUMAN_REVIEW", "STOPPED"}, result.model_dump_json()
    assert result.delivery is None and facts(client) == before
    factory = client.app.state.session_factory
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ArtProposalRecord)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(HumanReviewRequest)
                .where(HumanReviewRequest.handler == "ART_PROPOSAL")
            )
            == 0
        )
    with pytest.raises(DomainError, match="美术决策尚未完整交付"):
        generation.publish_art_direction(factory, settings, result.direction.loop_id, actor)


def test_paused_direction_resumes_then_delivers_once_without_repeating_generation(
    client, episode, settings, model_service
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    sent, errors, _ = install_direction_model(model_service, human=True)
    factory = client.app.state.session_factory
    waiting = generation.generate_art_proposal(factory, settings, run_id, actor, task)
    assert waiting.direction.status == "HUMAN_REVIEW" and waiting.delivery is None
    finished = resume_art_direction(
        factory, settings, waiting.direction.loop_id, actor, decision=answer(waiting.direction)
    )
    assert not errors and finished.status == "COMPLETED", finished.model_dump_json()
    delivery = generation.publish_art_direction(factory, settings, finished.loop_id, actor)
    assert delivery.loop_id == waiting.direction.loop_id
    assert generation.publish_art_direction(factory, settings, finished.loop_id, actor) == delivery
    assert len(sent) == 5


@pytest.mark.parametrize("fault", ["save_proposal", "submit_art", "audit"])
def test_delivery_failure_rolls_back_candidate_review_receipt_and_audit_then_reenters(
    client, episode, settings, model_service, monkeypatch, fault
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    sent, errors, _ = install_direction_model(model_service)
    factory = client.app.state.session_factory
    completed = propose_art(factory, settings, run_id, actor, task)
    assert completed.status == "COMPLETED" and not errors
    before, before_effects = facts(client), effects(client)
    original = getattr(generation, fault)

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated delivery crash")

    with monkeypatch.context() as patch:
        patch.setattr(generation, fault, fail)
        with pytest.raises(RuntimeError, match="simulated delivery crash"):
            generation.publish_art_direction(factory, settings, completed.loop_id, actor)
    assert facts(client) == before and effects(client) == before_effects
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ArtProposalReview)) == 0
        assert (
            db.scalar(select(AgentLoopAction).where(AgentLoopAction.action_key == DELIVERY_KEY))
            is None
        )
        assert db.get(AgentLoop, completed.loop_id).status == "COMPLETED"
    delivery = generation.publish_art_direction(factory, settings, completed.loop_id, actor)
    assert generation.publish_art_direction(factory, settings, completed.loop_id, actor) == delivery
    assert len(sent) == 4


@pytest.mark.parametrize("record", ["candidate", "source", "receipt", "selection"])
@pytest.mark.parametrize("mutation", ["update", "delete"])
def test_generated_candidate_provenance_receipt_and_review_selection_are_database_immutable(
    client, episode, settings, model_service, record, mutation
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    install_direction_model(model_service)
    factory = client.app.state.session_factory
    generated = generation.generate_art_proposal(factory, settings, run_id, actor, task)
    delivery = generated.delivery
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
        if record == "candidate":
            row = db.get(ArtProposalVersion, delivery.version_id)
            field = "proposal_json"
        elif record == "selection":
            row = db.scalar(select(ArtProposalReview))
            field = "confirmation_json"
        else:
            row = db.scalar(
                select(AgentLoopAction).where(AgentLoopAction.action_key == DELIVERY_KEY)
            )
            field = "request_json" if record == "source" else "result_json"
        if mutation == "delete":
            db.delete(row)
        else:
            setattr(row, field, {"tampered": True})
        db.flush()
    assert generation.publish_art_direction(factory, settings, delivery.loop_id, actor) == delivery


@pytest.mark.parametrize("tamper", ["scope", "proposal", "comparison"])
def test_mutable_loop_outcome_cannot_forge_an_unobserved_delivery(
    client, episode, settings, model_service, tamper
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    install_direction_model(model_service)
    factory = client.app.state.session_factory
    completed = propose_art(factory, settings, run_id, actor, task)
    before = facts(client)
    with factory.begin() as db:
        loop = db.get(AgentLoop, completed.loop_id)
        changed = deepcopy(loop.outcome_json)
        if tamper == "scope":
            changed["agent_name"] = "director"
        elif tamper == "proposal":
            changed["result"]["draft"]["proposal"]["visual_goal"] = "unobserved goal"
        else:
            changed["result"]["comparison_sha256"] = "a" * 64
        loop.outcome_json = changed
    with pytest.raises(DomainError):
        generation.publish_art_direction(factory, settings, completed.loop_id, actor)
    assert facts(client) == before


def test_postgres_concurrent_publication_creates_one_candidate_review_and_receipt(
    client, episode, settings, model_service
):
    require_postgres(settings)
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    sent, errors, _ = install_direction_model(model_service)
    factory = client.app.state.session_factory
    completed = propose_art(factory, settings, run_id, actor, task)
    assert completed.status == "COMPLETED" and not errors
    barrier = Barrier(2)

    def deliver():
        barrier.wait(timeout=10)
        return generation.publish_art_direction(factory, settings, completed.loop_id, actor)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(deliver) for _ in range(2)]
        results = [item.result(timeout=30) for item in pending]
    assert results[0] == results[1] and len(sent) == 4
    with factory() as db:
        for table in (ArtProposalRecord, ArtProposalVersion, ArtProposalReview, HumanReviewRequest):
            assert db.scalar(select(func.count()).select_from(table)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentLoopAction)
                .where(AgentLoopAction.action_key == DELIVERY_KEY)
            )
            == 1
        )
