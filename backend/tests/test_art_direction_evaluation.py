"""Offline metrics derive from actual art deliveries, tool actions and model receipts."""

import asyncio
import time
from decimal import Decimal
from pathlib import Path

from art_direction_support import install_direction_model, prepare_direction, reference_set
from sqlalchemy import select
from test_agent_loop_recovery import answer
from test_agent_model import model_service as model_service
from test_art_direction import facts
from test_contexts import visual_setup
from test_screenwriter_research import add_episode
from test_text_contract_v2 import save_script

from arclight.application.art_direction import propose_art, resume_art_direction
from arclight.application.gateway_accounting import latest_receipt, reconcile
from arclight.domain.model_gateway import GatewayUsage
from arclight.evaluation.art_direction_v1 import (
    DIRECTION_CASE_IDS,
    SOURCE_ALIASES,
    build_art_direction_benchmark_v1,
)
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
from arclight.evaluation.runner import EvaluationRunner
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import WorkflowRun
from arclight.providers.text import MockTextProvider


def test_offline_art_subset_runs_actual_multimodal_comparison_missing_and_recovery(
    client, episode, settings, model_service
):
    dataset = build_art_direction_benchmark_v1()
    baseline = build_benchmark_v1()
    assert dataset.cases[: len(baseline.cases)] == baseline.cases
    observations = []

    class Adapter:
        agent_role = AgentRole.ART_DIRECTOR

        async def evaluate_case(self, case):
            current = add_episode(client, episode["project_id"], len(observations) + 10)
            mode = case.input_payload["mode"]
            content = (
                MockTextProvider().script(case.input_payload["story"], {}).model_dump(mode="json")
            )
            content["scenes"][0]["notes"] = case.input_payload["untrusted_note"]
            source = save_script(client, current, content=content)
            if mode == "locked":
                visual_setup(client, current)
            name = "美术" + mode
            reference_set(client, current, count=0 if mode == "missing" else 2, name=name)
            if mode == "branch":
                reference_set(client, current, count=0, name="空目录")
            run_id, actor, _, task = prepare_direction(client, current, settings, source=source)
            sent, errors, _ = install_direction_model(
                model_service,
                search="空目录" if mode == "branch" else name,
                fallback=name if mode == "branch" else None,
                human=mode == "recovery",
                bible=mode == "locked",
            )
            factory = client.app.state.session_factory
            before = facts(client)
            started = time.monotonic()
            outcome = propose_art(factory, settings, run_id, actor, task)
            if mode == "recovery":
                assert outcome.status == "HUMAN_REVIEW", (outcome.model_dump_json(), errors)
                outcome = resume_art_direction(
                    factory, settings, outcome.loop_id, actor, decision=answer(outcome)
                )
            assert not errors, errors
            assert outcome.status == ("HUMAN_REVIEW" if mode == "missing" else "COMPLETED"), (
                outcome.model_dump_json()
            )
            assert facts(client) == before
            with factory.begin() as db:
                run = db.get(WorkflowRun, run_id)
                loop = db.get(AgentLoop, outcome.loop_id)
                actions = list(
                    db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop.id))
                )
                tools = [action for action in actions if action.kind == "TOOL"]
                succeeded = [
                    action for action in tools if action.result_json["status"] == "SUCCEEDED"
                ]
                materials = [
                    item
                    for action in succeeded
                    if action.request_json["tool_name"] == "investigate_art"
                    for item in action.result_json["result"]["materials"]
                ]
                evidence_ids = tuple(
                    sorted({SOURCE_ALIASES[item["source_kind"]] for item in materials})
                )
                cited = (
                    tuple(
                        sorted(
                            {
                                SOURCE_ALIASES[item.source_kind]
                                for item in outcome.result.draft.evidence
                            }
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
                            "offline-aart02: synthetic images and local HTTP; no paid calls",
                        )
                    )
                assert len(receipts) == outcome.model_turns == len(sent)
                aliases = {
                    "investigate_art": "art.investigate",
                    "compare_art_options": "art.compare",
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
                    selected_tool_ids=tuple(
                        aliases[action.request_json["tool_name"]] for action in tools
                    ),
                    executed_tool_ids=tuple(
                        aliases[action.request_json["tool_name"]] for action in succeeded
                    ),
                    citation_ids=cited,
                    evidence_ids=evidence_ids,
                    asset_reference_ids=(SOURCE_ALIASES["ASSET"],) if outcome.result else (),
                    decision=ExecutionDecision.HUMAN_REVIEW
                    if outcome.human_request
                    else ExecutionDecision.COMPLETE,
                    uncertainty=UncertaintyLevel.HIGH
                    if outcome.human_request
                    else UncertaintyLevel.LOW,
                    latency_ms=max(1, int((time.monotonic() - started) * 1000)),
                )
            observations.append(observation)
            return observation

    report = asyncio.run(
        EvaluationRunner().run(
            dataset, {AgentRole.ART_DIRECTOR: Adapter()}, case_ids=DIRECTION_CASE_IDS
        )
    )
    database = "postgres" if settings.database_url.startswith("postgresql") else "sqlite"
    path = Path("test-results") / f"art-direction-{database}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    assert len(report.cases) == len(observations) == 5, report.model_dump_json()
    for case in report.cases:
        assert case.status == CaseStatus.PENDING_HUMAN_REVIEW, report.model_dump_json()
        assert case.failure_codes == (FailureCode.HUMAN_REVIEW_REQUIRED,)
        assert all(score.passed is not False for score in case.scores)
    assert report.summary.total_cost_usd == Decimal("0")
