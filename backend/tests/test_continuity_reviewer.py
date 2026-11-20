import base64
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import test_continuity_rules as rules_module
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import func, select
from test_agent_loop_recovery import answer
from test_agent_model import model_service as model_service
from test_art_direction import facts
from test_continuity_rules import ANNOTATIONS
from test_continuity_rules import rules_case as fixed_rules_case
from test_continuity_rules import rules_storage as rules_storage

from arclight.application import agent_loop_recovery as recovery
from arclight.application import continuity_reviewer
from arclight.application.agent_limits import configure_agent_run
from arclight.application.continuity_reviewer import resume_reviewer, review_candidate
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.continuity_reviews import ReviewTarget
from arclight.domain.continuity_selection import ADMISSION_RESOURCE
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import AgentToolGrant, AssetVersion, VersionBinding


@pytest.fixture
def reviewer_case(client, episode, settings, model_service, rules_storage, request, monkeypatch):
    settings.llm_agent_capabilities = settings.llm_agent_capabilities.model_copy(
        update={"max_input_images": 8}
    )
    parameter = getattr(request, "param", "match")
    limits = parameter if isinstance(parameter, AgentRunLimits) else AgentRunLimits()
    prepare = rules_module.prepare

    def prepare_with_limits(*args, **kwargs):
        run_id, actor, source = prepare(*args, **kwargs)
        with client.app.state.session_factory.begin() as db:
            configure_agent_run(db, run_id, actor, limits)
        return run_id, actor, source

    monkeypatch.setattr(rules_module, "prepare", prepare_with_limits)
    fixture_request = SimpleNamespace(
        param="match" if isinstance(parameter, AgentRunLimits) else parameter
    )
    return fixed_rules_case.__wrapped__(client, episode, settings, rules_storage, fixture_request)


def run_review(client, settings, case, **kwargs):
    return review_candidate(
        client.app.state.session_factory,
        settings,
        case["run_id"],
        case["actor"],
        ReviewTarget.model_validate(case["target"]),
        **kwargs,
    )


@pytest.mark.parametrize("rules_storage", ["local", "s3"], indirect=True)
def test_dynamic_review_consumes_exact_images_and_keeps_existing_state(
    client,
    settings,
    model_service,
    reviewer_case,
):
    before = facts(client)
    sent, errors, _ = install_reviewer_model(model_service)
    result = run_review(client, settings, reviewer_case)
    assert not errors, errors
    assert result.status == "COMPLETED", result.model_dump_json()
    assert result.result.conclusion == "PASS"
    assert result.result.confidence.kind == "ORDINAL_UNCALIBRATED"
    assert len(sent) == result.model_turns == 7
    assert all(
        "Use API-native tool_calls" in payload["messages"][0]["content"]
        and "without Markdown fences" in payload["messages"][0]["content"]
        for payload in sent
    )
    hashes = {
        hashlib.sha256(base64.b64decode(part["image_url"]["url"].split(",", 1)[1])).hexdigest()
        for payload in sent
        for message in payload["messages"]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part["type"] == "image_url"
    }
    assert ANNOTATIONS["sha256"]["match.png"] in hashes
    assert hashes == {
        ANNOTATIONS["sha256"][name]
        for name in (
            "match.png",
            "character.png",
            "costume-reference.png",
            "scene-reference.png",
            "prop-reference.png",
            "neighbor.png",
        )
    }
    assert facts(client) == before
    assert run_review(client, settings, reviewer_case) == result
    assert (
        resume_reviewer(
            client.app.state.session_factory, settings, result.loop_id, reviewer_case["actor"]
        )
        == result
    )
    assert len(sent) == 7
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == ADMISSION_RESOURCE)
            )
            == 0
        )
        loop = db.get(AgentLoop, result.loop_id)
        assert "base64," not in json.dumps(loop.seed_json)
        assert "base64," not in json.dumps(loop.outcome_json)


@pytest.mark.parametrize(
    "risk,expected",
    [("MEDIUM", "REVISION_REQUIRED"), ("HIGH", "HUMAN_REVIEW"), ("CRITICAL", "HUMAN_REVIEW")],
)
def test_actual_differences_keep_risk_exact_evidence_and_bounded_suggestions(
    client,
    settings,
    model_service,
    reviewer_case,
    risk,
    expected,
):
    sent, errors, _ = install_reviewer_model(
        model_service, dynamic=False, difference=("STATE", risk)
    )
    outcome = run_review(client, settings, reviewer_case)
    assert not errors, errors
    assert outcome.status == "COMPLETED", outcome.model_dump_json()
    assert outcome.result.conclusion == expected and outcome.result.risk == risk
    assert outcome.result.issues[0].evidence_ids
    assert len(sent) == 5


@pytest.mark.parametrize(
    "reason", ["OCCLUDED", "MISSING_REFERENCE", "CONTRADICTION", "LOW_VISIBILITY"]
)
def test_uncertainty_is_explicit_and_never_becomes_a_probability_or_pass(
    client,
    settings,
    model_service,
    reviewer_case,
    reason,
):
    uncertainty = {
        "reason": reason,
        "detail": "The garment detail cannot be established from these exact images.",
    }
    _, errors, _ = install_reviewer_model(model_service, dynamic=False, uncertainty=uncertainty)
    outcome = run_review(client, settings, reviewer_case)
    assert not errors, errors
    assert outcome.status == "COMPLETED", outcome.model_dump_json()
    assert outcome.result.conclusion == "HUMAN_REVIEW"
    assert outcome.result.confidence.uncertainty[0].model_dump() == uncertainty


@pytest.mark.parametrize("rule_id", ["PROP", "HOLDER", "POSITION", "STATE", "ADJACENT"])
def test_occluded_prop_comparisons_preserve_unknowns_without_missing_item_issues(
    client,
    settings,
    model_service,
    reviewer_case,
    rule_id,
):
    uncertainty = {
        "reason": "OCCLUDED",
        "detail": "The expected item and hand region is hidden; absence cannot be established.",
    }
    sent, errors, _ = install_reviewer_model(
        model_service,
        dynamic=False,
        uncertainty=uncertainty,
        uncertainty_rule=rule_id,
    )
    outcome = run_review(client, settings, reviewer_case)
    assert not errors, errors
    assert outcome.status == "COMPLETED", outcome.model_dump_json()
    assert outcome.result.conclusion == "HUMAN_REVIEW"
    assert outcome.result.risk == "NONE" and outcome.result.issues == []
    assert [u.model_dump() for u in outcome.result.confidence.uncertainty] == [uncertainty]
    checked = [
        item
        for payload in sent
        for tool in payload.get("messages", [])
        if tool["role"] == "tool"
        for item in json.loads(tool["content"]).get("result", {}).get("checks", [])
        if item["definition"]["rule_id"] == rule_id and item["comparison"]
    ]
    assert checked and checked[-1]["comparison"]["status"] == "UNDETERMINED"


@pytest.mark.parametrize(
    "mode,code",
    [
        ("unread", "REVIEW_IMAGE_NOT_OBSERVED"),
        ("bypass", "REVIEW_COMPARISON_REQUIRED"),
        ("invented", "INVALID_ASSET_REFERENCE"),
        ("wrong_version", "REVIEW_SCOPE_INVALID"),
        ("omit_difference", "REVIEW_CONCLUSION_INVALID"),
        ("omit_text_basis", "REVIEW_EVIDENCE_MISSING"),
        ("rewrite_uncertainty", "REVIEW_UNCERTAINTY_REQUIRED"),
        ("omit_issue_basis", "REVIEW_ISSUE_MISSING"),
        ("lower_issue_risk", "REVIEW_ISSUE_MISSING"),
        ("adjacent_envelope_hash", "REVIEW_REFERENCE_CHANGED"),
    ],
)
def test_unobserved_forged_or_suppressed_deliveries_stop_without_admission(
    client,
    settings,
    model_service,
    reviewer_case,
    mode,
    code,
):
    def tamper(result):
        if mode == "invented":
            ref = next(r for r in result["references"] if r["role"] == "REFERENCE")
            ref["resource_id"] = str(uuid4())
        elif mode == "wrong_version":
            result["versions"][0]["version"] = "imaginary/9"
        elif mode == "omit_difference":
            result.update(conclusion="PASS", risk="NONE", issues=[], suggested_action="NONE")
        elif mode in {"omit_text_basis", "omit_issue_basis"}:
            ref = next(r for r in result["references"] if r["role"] == "TARGET_SHOT")
            missing = {
                e["evidence_id"]
                for e in result["evidence"]
                if e["reference_id"] == ref["reference_id"]
            }
            if mode == "omit_text_basis":
                result["evidence"] = [
                    e for e in result["evidence"] if e["evidence_id"] not in missing
                ]
            else:
                for issue in result["issues"]:
                    issue["evidence_ids"] = [
                        identity for identity in issue["evidence_ids"] if identity not in missing
                    ]
        elif mode == "rewrite_uncertainty":
            result["confidence"]["uncertainty"][0]["detail"] = (
                "A paraphrase loses the exact comparison uncertainty."
            )
        elif mode == "lower_issue_risk":
            result["issues"][0]["risk"] = result["risk"] = "LOW"
        elif mode == "adjacent_envelope_hash":
            ref = next(r for r in result["references"] if r["role"] == "ADJACENT_SHOT")
            material = next(
                m
                for m in reviewer_case["evidence"]["materials"]
                if m["source_kind"] == "SHOT" and m["source_id"] == ref["resource_id"]
            )
            assert ref["content_sha256"] != material["content_sha256"]
            ref["content_sha256"] = material["content_sha256"]

    difference = (
        ("STATE", "MEDIUM")
        if mode in {"omit_difference", "omit_issue_basis", "lower_issue_risk"}
        else None
    )
    uncertainty = (
        {"reason": "OCCLUDED", "detail": "The garment is hidden behind a foreground object."}
        if mode == "rewrite_uncertainty"
        else None
    )

    _, errors, _ = install_reviewer_model(
        model_service,
        dynamic=False,
        no_images=mode == "unread",
        bypass=mode == "bypass",
        mutate_final=tamper,
        difference=difference,
        uncertainty=uncertainty,
    )
    outcome = run_review(client, settings, reviewer_case)
    assert not errors, errors
    assert outcome.status == "STOPPED" and outcome.stop_code == code, outcome.model_dump_json()
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == ADMISSION_RESOURCE)
            )
            == 0
        )


def test_human_pause_and_duplicate_resume_keep_original_limits_and_thread(
    client,
    settings,
    model_service,
    reviewer_case,
    monkeypatch,
):
    factory = client.app.state.session_factory
    sent, errors, _ = install_reviewer_model(model_service, human=True, dynamic=False)
    waiting = run_review(client, settings, reviewer_case)
    assert waiting.status == "HUMAN_REVIEW", waiting.model_dump_json()
    with factory() as db:
        control = db.get(AgentRunLimit, reviewer_case["run_id"])
        original = (control.deadline_at, control.limits_json, control.root_run_id)
        thread = db.get(AgentLoop, waiting.loop_id).thread_id
        frozen = db.get(AgentLoop, waiting.loop_id).seed_json
    monkeypatch.setattr(continuity_reviewer, "REVIEWER_PROMPT", "A later review instruction.")
    assert run_review(client, settings, reviewer_case) == waiting
    decision = answer(waiting)
    complete = resume_reviewer(
        factory, settings, waiting.loop_id, reviewer_case["actor"], decision=decision
    )
    assert not errors, errors
    assert complete.status == "COMPLETED", complete.model_dump_json()
    assert complete.loop_id == waiting.loop_id
    assert all(
        payload["messages"][0]["content"].startswith(frozen["system_instruction"])
        for payload in sent
    )
    calls = len(sent)
    assert (
        resume_reviewer(
            factory, settings, waiting.loop_id, reviewer_case["actor"], decision=decision
        )
        == complete
    )
    assert len(sent) == calls
    with factory() as db:
        control = db.get(AgentRunLimit, reviewer_case["run_id"])
        assert (control.deadline_at, control.limits_json, control.root_run_id) == original
        assert db.get(AgentLoop, waiting.loop_id).thread_id == thread
        assert db.get(AgentLoop, waiting.loop_id).seed_json == frozen


@pytest.mark.parametrize("node", ["decide", "tools"])
def test_restart_reuses_committed_model_and_tool_actions(
    client,
    settings,
    model_service,
    reviewer_case,
    monkeypatch,
    node,
):
    sent, errors, _ = install_reviewer_model(model_service, dynamic=False)
    original = getattr(recovery._PersistentRuntime, node)
    crashed = []

    def interrupt_once(*args, **kwargs):
        change = original(*args, **kwargs)
        if not crashed:
            crashed.append(True)
            raise RuntimeError("reviewer interruption")
        return change

    monkeypatch.setattr(recovery._PersistentRuntime, node, interrupt_once)
    with pytest.raises(RuntimeError, match="reviewer interruption"):
        run_review(client, settings, reviewer_case)
    outcome = run_review(client, settings, reviewer_case)
    assert not errors, errors
    assert outcome.status == "COMPLETED" and len(sent) == 5, outcome.model_dump_json()


@pytest.mark.parametrize("reviewer_case", [AgentRunLimits(max_model_calls=1)], indirect=True)
def test_exhaustion_is_bounded_and_reentry_does_not_reset_budget(
    client,
    settings,
    model_service,
    reviewer_case,
):
    sent, errors, _ = install_reviewer_model(model_service)
    limits = AgentRunLimits(max_model_calls=1)
    outcome = run_review(client, settings, reviewer_case, limits=limits)
    assert not errors, errors
    assert outcome.status == "STOPPED" and outcome.stop_code == "AGENT_MODEL_CALL_LIMIT", (
        outcome.model_dump_json()
    )
    assert len(sent) == 1
    assert run_review(client, settings, reviewer_case) == outcome
    assert len(sent) == 1


@pytest.mark.parametrize("mutation", ["grant", "stale", "archived"])
def test_final_revalidates_current_authority_and_candidate_after_model_io(
    client,
    settings,
    model_service,
    reviewer_case,
    mutation,
):
    factory = client.app.state.session_factory

    def change(payload):
        feedback = [m for m in payload["messages"] if m["role"] == "tool"]
        if (
            not feedback
            or json.loads(feedback[-1]["content"])["tool_name"] != "check_continuity_rules"
        ):
            return
        result = json.loads(feedback[-1]["content"])["result"]
        if result["suggested_conclusion"] is None:
            return
        with factory.begin() as db:
            if mutation == "grant":
                grant = db.scalar(
                    select(AgentToolGrant).where(
                        AgentToolGrant.run_id == reviewer_case["run_id"],
                        AgentToolGrant.agent_name == "continuity_reviewer",
                    )
                )
                grant.allowed_tools_json = []
            elif mutation == "stale":
                db.get(AssetVersion, UUID(reviewer_case["target"]["asset_version_id"])).stale = True
            else:
                from arclight.persistence.models import Project

                db.get(Project, UUID(reviewer_case["project_id"])).archived = True

    _, errors, _ = install_reviewer_model(model_service, dynamic=False, on_http=change)
    outcome = run_review(client, settings, reviewer_case)
    assert not errors, errors
    assert outcome.status == "STOPPED", outcome.model_dump_json()
