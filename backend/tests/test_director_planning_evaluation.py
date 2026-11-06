"""Offline observations derive from actual saved director plans, tools and gateway receipts."""

import asyncio
import time
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from test_agent_loop_recovery import answer
from test_agent_model import model_service as model_service
from test_director_planning import install_model, prepare
from test_screenwriter_research import add_episode
from test_text_contract_v2 import make_asset, save_script

from arclight.application.director_planning import (
    plan_director,
    read_director_plan,
    resume_director_plan,
)
from arclight.application.gateway_accounting import latest_receipt, reconcile
from arclight.domain.model_gateway import GatewayUsage
from arclight.evaluation.benchmark_v1 import build_benchmark_v1
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
from arclight.evaluation.director_planning_v1 import (
    PLANNING_CASE_IDS,
    SOURCE_ALIASES,
    build_director_planning_benchmark_v1,
)
from arclight.evaluation.runner import EvaluationRunner
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import WorkflowRun
from arclight.providers.text import MockTextProvider


def test_offline_director_subset_observes_real_investigation_plan_escalation_and_recovery(
    client, episode, settings, model_service
):
    dataset = build_director_planning_benchmark_v1()
    baseline = build_benchmark_v1()
    assert dataset.cases[: len(baseline.cases)] == baseline.cases
    observations = []

    class Adapter:
        agent_role = AgentRole.DIRECTOR

        async def evaluate_case(self, case):
            current = add_episode(client, episode["project_id"], len(observations) + 2)
            mode = case.input_payload["mode"]
            content = (
                MockTextProvider().script(case.input_payload["story"], {}).model_dump(mode="json")
            )
            content["scenes"][0]["notes"] = case.input_payload["untrusted_note"]
            source = save_script(client, current, content=content)
            run_id, actor, _ = prepare(client, current, settings, source=source)
            extra = None
            if mode == "assets":
                with client.app.state.session_factory.begin() as db:
                    reference = make_asset(db, current["project_id"], "SCENE")
                extra = {"path": "assets", "version_ids": [reference["asset_version_id"]]}
            elif mode == "missing":
                extra = {
                    "path": "capabilities",
                    "provider": "not-installed",
                    "model": "unavailable",
                }
            sent, errors = install_model(
                model_service, extra=extra, human=mode in {"missing", "recovery"}
            )
            factory = client.app.state.session_factory
            started = time.monotonic()
            outcome = plan_director(factory, settings, run_id, actor)
            if mode == "recovery":
                assert outcome.status == "HUMAN_REVIEW", (outcome.model_dump_json(), errors)
                outcome = resume_director_plan(
                    factory, settings, outcome.loop_id, actor, decision=answer(outcome)
                )
            assert outcome.status == ("HUMAN_REVIEW" if mode == "missing" else "COMPLETED"), (
                outcome.model_dump_json(),
                errors,
            )
            with factory.begin() as db:
                assert read_director_plan(db, outcome.loop_id, actor) == outcome
                run = db.get(WorkflowRun, run_id)
                loop = db.get(AgentLoop, outcome.loop_id)
                actions = list(
                    db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop.id))
                )
                tools = [row for row in actions if row.kind == "TOOL"]
                succeeded = [row for row in tools if row.result_json["status"] == "SUCCEEDED"]
                materials = [
                    item for row in succeeded for item in row.result_json["result"]["materials"]
                ]
                available = {item["source_id"] for item in materials}
                assert (
                    outcome.result is None
                    or {str(item.source_id) for item in outcome.result.evidence} <= available
                )
                ids = tuple(sorted({SOURCE_ALIASES[item["source_kind"]] for item in materials}))
                cited = (
                    tuple(
                        sorted(
                            {SOURCE_ALIASES[item.source_kind] for item in outcome.result.evidence}
                        )
                    )
                    if outcome.result
                    else ()
                )
                receipts = []
                for call in db.scalars(
                    select(GatewayCall).where(GatewayCall.workflow_id == run_id)
                ):
                    receipt = latest_receipt(db, call.id)
                    assert receipt is not None
                    receipts.append(
                        reconcile(
                            db,
                            call,
                            actor,
                            "SUCCEEDED",
                            GatewayUsage.model_validate(receipt.usage_json).model_copy(
                                update={"actual_cost": Decimal("0")}
                            ),
                            "offline-direct01: local HTTP MockTransport; no external model calls",
                        )
                    )
                assert len(receipts) == outcome.model_turns == len(sent)
                assert {row.request_json["tool_name"] for row in tools} == {
                    "investigate_production"
                }
                observation = EvaluationObservation(
                    case_id=case.case_id,
                    sample_id=case.sample_id,
                    agent_role=self.agent_role,
                    execution=ExecutionMetadata(
                        run_id=run_id,
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
                    required_fields_present=True,
                    selected_tool_ids=tuple("production.investigate" for _ in tools),
                    executed_tool_ids=tuple("production.investigate" for _ in succeeded),
                    citation_ids=cited,
                    evidence_ids=ids,
                    asset_reference_ids=(SOURCE_ALIASES["ASSET"],) if mode == "assets" else (),
                    decision=ExecutionDecision.HUMAN_REVIEW
                    if outcome.human_request
                    else ExecutionDecision.COMPLETE,
                    uncertainty=UncertaintyLevel.HIGH
                    if outcome.human_request
                    else UncertaintyLevel.LOW,
                    output_duration_ms=outcome.result.estimated_duration_ms
                    if outcome.result
                    else None,
                    latency_ms=max(1, int((time.monotonic() - started) * 1000)),
                )
                observations.append(observation)
            return observation

    report = asyncio.run(
        EvaluationRunner().run(dataset, {AgentRole.DIRECTOR: Adapter()}, case_ids=PLANNING_CASE_IDS)
    )
    database = "postgres" if settings.database_url.startswith("postgresql") else "sqlite"
    path = Path("test-results") / f"director-planning-{database}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    assert len(report.cases) == len(observations) == 4, report.model_dump_json()
    for case in report.cases:
        assert case.status == CaseStatus.PENDING_HUMAN_REVIEW, report.model_dump_json()
        assert case.failure_codes == (FailureCode.HUMAN_REVIEW_REQUIRED,)
        assert all(score.passed is not False for score in case.scores)
    assert report.summary.total_cost_usd == Decimal("0")
