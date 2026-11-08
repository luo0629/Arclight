import asyncio
import time
from decimal import Decimal
from pathlib import Path

import pytest
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import select
from test_agent_loop_recovery import answer
from test_agent_model import model_service as model_service
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_reviewer import run_review
from test_continuity_rules import rules_storage as rules_storage

from arclight.application.continuity_reviewer import resume_reviewer
from arclight.application.gateway_accounting import latest_receipt, reconcile
from arclight.domain.model_gateway import GatewayUsage
from arclight.evaluation.benchmark_v1 import build_benchmark_v1
from arclight.evaluation.continuity_reviewer_v1 import (
    REVIEW_CASE_IDS,
    REVIEW_MODES,
    build_continuity_reviewer_benchmark_v1,
)
from arclight.evaluation.contracts import (
    AgentRole,
    CaseStatus,
    EvaluationObservation,
    ExecutionDecision,
    ExecutionMetadata,
    FailureCode,
    UncertaintyLevel,
    UsageObservation,
)
from arclight.evaluation.runner import EvaluationRunner
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import WorkflowRun


@pytest.mark.parametrize(
    "reviewer_case,mode",
    [
        ("match", "match"),
        ("occluded", "occluded"),
        ("match", "contradiction"),
        ("missing_bindings", "missing"),
        ("match", "recovery"),
    ],
    indirect=["reviewer_case"],
)
def test_offline_reviewer_subset_executes_actual_evidence_comparison_and_recovery(
    client,
    settings,
    model_service,
    reviewer_case,
    mode,
):
    dataset = build_continuity_reviewer_benchmark_v1()
    assert dataset.cases[:5] == build_benchmark_v1().cases
    identity = REVIEW_CASE_IDS[REVIEW_MODES.index(mode)]
    uncertainty = (
        {"reason": "OCCLUDED", "detail": "The candidate garment is partly occluded."}
        if mode == "occluded"
        else {
            "reason": "MISSING_REFERENCE",
            "detail": "The fixed view does not establish the garment detail.",
        }
        if mode == "missing"
        else None
    )
    sent, errors, _ = install_reviewer_model(
        model_service,
        dynamic=True,
        human=mode == "recovery",
        difference=("STATE", "HIGH") if mode == "contradiction" else None,
        uncertainty=uncertainty,
    )
    factory = client.app.state.session_factory

    class Adapter:
        agent_role = AgentRole.CONTINUITY_REVIEWER

        async def evaluate_case(self, case):
            started = time.monotonic()
            outcome = run_review(client, settings, reviewer_case)
            if mode == "recovery":
                assert outcome.status == "HUMAN_REVIEW", outcome.model_dump_json()
                outcome = resume_reviewer(
                    factory,
                    settings,
                    outcome.loop_id,
                    reviewer_case["actor"],
                    decision=answer(outcome),
                )
            assert not errors, errors
            assert outcome.status == "COMPLETED", outcome.model_dump_json()
            with factory.begin() as db:
                run = db.get(WorkflowRun, outcome.run_id)
                loop = db.get(AgentLoop, outcome.loop_id)
                actions = list(
                    db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop.id))
                )
                tools = [a for a in actions if a.kind == "TOOL"]
                succeeded = [a for a in tools if a.result_json["status"] == "SUCCEEDED"]
                investigated = [
                    m
                    for a in succeeded
                    if a.request_json["tool_name"] == "investigate_continuity"
                    for m in a.result_json["result"]["materials"]
                ]
                assert any(m["source_kind"] == "CANDIDATE" for m in investigated)
                assert any(
                    m["source_kind"] == "ASSET" and m["content"]["image"] for m in investigated
                )
                assert any(r.role == "CANDIDATE" for r in outcome.result.references)
                assert any(
                    r.role == "REFERENCE"
                    and r.asset_type in {"IMAGE", "CHARACTER", "SCENE", "PROP"}
                    for r in outcome.result.references
                )
                receipts = []
                for call in db.scalars(
                    select(GatewayCall).where(GatewayCall.workflow_id == run.id)
                ):
                    receipt = latest_receipt(db, call.id)
                    assert receipt is not None
                    receipts.append(
                        reconcile(
                            db,
                            call,
                            reviewer_case["actor"],
                            "SUCCEEDED",
                            GatewayUsage.model_validate(receipt.usage_json).model_copy(
                                update={"actual_cost": Decimal("0")}
                            ),
                            "offline-arev02: synthetic PNG and mocked HTTP; no paid calls",
                        )
                    )
                assert len(receipts) == len(sent) == outcome.model_turns
                aliases = {
                    "investigate_continuity": "review.investigate",
                    "check_continuity_rules": "review.compare",
                }
                human = outcome.result.conclusion == "HUMAN_REVIEW"
                return EvaluationObservation(
                    case_id=case.case_id,
                    sample_id=case.sample_id,
                    agent_role=self.agent_role,
                    execution=ExecutionMetadata(
                        run_id=run.id,
                        trace_id=run.trace_id,
                        model_id=settings.llm_model,
                        model_version=settings.llm_agent_capabilities.version,
                        prompt_version=loop.seed_json["prompt_version"],
                        workflow_version=run.workflow_version,
                        usage=UsageObservation(
                            input_tokens=sum(r.usage_json["input_tokens"] for r in receipts),
                            output_tokens=sum(r.usage_json["output_tokens"] for r in receipts),
                            cost_usd=sum((r.actual_cost for r in receipts), Decimal("0")),
                        ),
                    ),
                    schema_valid=True,
                    required_fields_present=bool(outcome.result.evidence),
                    selected_tool_ids=tuple(aliases[a.request_json["tool_name"]] for a in tools),
                    executed_tool_ids=tuple(
                        aliases[a.request_json["tool_name"]] for a in succeeded
                    ),
                    citation_ids=("review:candidate.v1", "review:reference.v1"),
                    evidence_ids=("review:candidate.v1", "review:reference.v1"),
                    asset_reference_ids=("review:reference.v1",),
                    uncertainty=UncertaintyLevel.HIGH if human else UncertaintyLevel.LOW,
                    decision=ExecutionDecision.HUMAN_REVIEW
                    if human
                    else ExecutionDecision.COMPLETE,
                    latency_ms=max(1, int((time.monotonic() - started) * 1000)),
                )

    report = asyncio.run(
        EvaluationRunner().run(
            dataset, {AgentRole.CONTINUITY_REVIEWER: Adapter()}, case_ids=(identity,)
        )
    )
    database = "postgres" if settings.database_url.startswith("postgresql") else "sqlite"
    path = Path("test-results") / f"continuity-reviewer-{database}-{mode}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    assert len(report.cases) == 1, report.model_dump_json()
    case = report.cases[0]
    assert case.status == CaseStatus.PENDING_HUMAN_REVIEW, report.model_dump_json()
    assert case.failure_codes == (FailureCode.HUMAN_REVIEW_REQUIRED,), report.model_dump_json()
    assert all(score.passed is not False for score in case.scores), report.model_dump_json()
    assert report.summary.total_cost_usd == Decimal("0")
