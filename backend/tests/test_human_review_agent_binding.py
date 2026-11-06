"""The review foundation binds ARUN-05 identities and never replaces its recovery engine."""

import httpx
import pytest
from test_agent_model import FinalResult, model_request
from test_agent_model import model_service as model_service
from test_agent_runtime import final, setup_loop
from test_human_reviews import contract_registry as contract_registry
from test_human_reviews import contract_review, count, decide, resume

from arclight.application.agent_loop_recovery import run_persistent_agent_loop
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision
from arclight.persistence.human_reviews import HumanReviewResume
from arclight.persistence.models import WorkflowRun


@pytest.mark.parametrize("cancel", [False, True])
def test_review_retains_original_agent_loop_and_reuses_recovery_guards(
    client, episode, settings, contract_registry, model_service, cancel
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    calls = []
    model_service(
        lambda request: calls.append(request) or httpx.Response(200, json=final("HUMAN_REVIEW"))
    )
    waiting = run_persistent_agent_loop(
        client.app.state.session_factory, settings, model_request(run_id), registry, FinalResult
    )
    assert waiting.status == "HUMAN_REVIEW"
    row = contract_review(client, episode, actor=actor, run_id=run_id, loop_id=waiting.loop_id)
    assert row["snapshot"]["thread_id"] == f"agent-loop:{waiting.loop_id}"
    saved = decide(client, row)
    assert saved.status_code == 200, saved.text
    if cancel:
        # As in ARUN-05, seed the authoritative server cancellation fact.
        # Legacy text HTTP cancellation intentionally excludes running Agent loops.
        with client.app.state.session_factory.begin() as db:
            db.get(WorkflowRun, run_id).status = "CANCELLED"
    response = resume(client, row, saved.json())
    assert response.status_code == (409 if cancel else 200), response.text
    if cancel:
        assert response.json()["error"]["code"] == "AGENT_CANCELLED"
    assert len(calls) == 1 and count(client, AgentLoopDecision) == 0
    with client.app.state.session_factory() as db:
        loop = db.get(AgentLoop, waiting.loop_id)
        assert loop.thread_id == row["snapshot"]["thread_id"]
        assert loop.status == "WAITING_HUMAN"
    # Domain approval does not answer the Agent's distinct pending question.
    assert count(client, HumanReviewResume) == (0 if cancel else 1)
