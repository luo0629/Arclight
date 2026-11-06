"""Actual critique, patch tool, journal, final candidate and original review graphs."""

import copy
import json
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from test_agent_model import call_response
from test_agent_model import model_service as model_service
from test_agent_runtime import final, tool_feedback
from test_screenwriter_creation import approve, install, progress, run_once, start
from test_script_rewrite import body as rewrite_body
from test_script_rewrite import source as rewrite_source
from test_script_rewrite import start as start_rewrite

from arclight.agents.script_rewrite import RewriteRequest
from arclight.application.rewrite_content import content_hash
from arclight.domain.screenwriter_revision import CHECKS, PATCH_TOOL, ScriptCritique
from arclight.main import create_app
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction, AgentLoopQuestion
from arclight.persistence.models import Review, Script, Storyboard


def install_revision(
    model_service,
    monkeypatch,
    settings,
    *,
    patches=1,
    invalid_first=None,
    category="MOTIVATION",
    mutate_report=None,
    escalate=False,
    human_unresolved=False,
    on_http=None,
):
    sent, errors = [], []

    def register(ordinary):
        def respond(request):
            payload = json.loads(request.content)
            sent.append(payload)
            if on_http:
                on_http(payload)
            first = json.loads(payload["messages"][1]["content"])
            seed = first.get("data", {})
            if seed.get("phase") != "CRITIQUE_SCRIPT":
                return ordinary(request)
            try:
                feedback = tool_feedback(payload)
                answered = any(
                    "human_decision" in str(item.get("content")) for item in payload["messages"]
                )
                source = seed["source_script"]
                target = seed["permitted_target"]
                selected_id = target.get("scene_id") if target else None
                scene = next(
                    (item for item in source["scenes"] if item["scene_id"] == selected_id),
                    source["scenes"][0],
                )
                issue = {
                    "category": category,
                    "scene_id": scene["scene_id"],
                    "line_id": None,
                    "problem": "送信的动机不够明确",
                    "evidence": scene["action"],
                    "suggestion": "保留动作并补充寻找失主的动机",
                }
                result = {
                    "schema_version": "screenwriter-revision/1",
                    "source_sha256": seed["source_sha256"],
                    "checks": sorted(CHECKS),
                    "issues": [],
                    "decision": "ACCEPT",
                    "patch_sha256": None,
                }
                if human_unresolved and answered:
                    value = final("HUMAN_REVIEW")
                elif answered and (escalate or seed["remaining_patches"] == 0):
                    value = final(result=result)
                elif (
                    escalate or seed["remaining_patches"] == 0 and seed["round"] < patches
                ) and not answered:
                    result |= {"decision": "ESCALATE", "issues": [issue]}
                    value = final(result=result)
                elif seed["round"] < patches or seed["initial_rewrite_required"]:
                    if (
                        not feedback
                        or feedback[-1]["status"] != "SUCCEEDED"
                        or not feedback[-1]["result"]["accepted"]
                    ):
                        target = seed["permitted_target"] or {
                            "scope": "scene",
                            "scene_id": scene["scene_id"],
                        }
                        if target["scope"] == "paragraph":
                            patch_scenes = []
                            replacement = (
                                "因为答应过失主🙂，他决定把信亲手送回。"
                                + "再确认。" * seed["round"]
                            )
                        else:
                            selected = target.get("scene_ids", [target.get("scene_id")])
                            patch_scenes = [
                                {
                                    key: copy.deepcopy(item[key])
                                    for key in ("scene_id", "title", "action", "dialogue")
                                }
                                for item in source["scenes"]
                                if item["scene_id"] in selected
                            ]
                            for item in patch_scenes:
                                item["action"] += "他记得失主的嘱托，因而决定亲自送信。"
                            replacement = None
                        arguments = {
                            "operation_key": seed["operation_key"],
                            "source_sha256": seed["source_sha256"],
                            "target": target,
                            "instructions": seed["instructions"] or {"instruction": "明确动机"},
                            "patch": {
                                "estimated_duration_ms": source["estimated_duration_ms"],
                                "scenes": patch_scenes,
                                "text": replacement,
                            },
                        }
                        if invalid_first and not feedback:
                            invalid_first(arguments, seed)
                        value = call_response((f"patch-{len(feedback)}", PATCH_TOOL, arguments))
                    else:
                        result |= {
                            "decision": "REVISE",
                            "issues": [issue],
                            "patch_sha256": feedback[-1]["result"]["patch_sha256"],
                        }
                        value = final(result=result)
                else:
                    value = final(result=result)
                if mutate_report and "choices" in value:
                    message = value["choices"][0]["message"]
                    if message.get("content"):
                        decision = json.loads(message["content"])
                        mutate_report(decision, seed)
                        message["content"] = json.dumps(decision)
                value["usage"] = {"prompt_tokens": 20, "completion_tokens": 10, "actual_cost": "0"}
                return httpx.Response(200, json=value)
            except Exception as exc:
                errors.append(repr(exc))
                raise

        model_service(respond)

    _, ordinary_errors = install(register, monkeypatch, settings)
    return sent, errors, ordinary_errors


def critique_operations(client, run_id):
    return [item for item in progress(client, run_id) if item["kind"] == "critique"]


def test_creation_critiques_patches_rechecks_and_enters_original_two_reviews(
    client, episode, settings, model_service, monkeypatch
):
    sent, errors, ordinary_errors = install_revision(model_service, monkeypatch, settings)
    run_id = start(client, episode, revision_rounds=2)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run["error_code"], errors, ordinary_errors)
    assert run["workflow_version"] == "screenwriter/2.0"
    reports = critique_operations(client, run_id)
    assert [item["result"]["decision"] for item in reports] == ["REVISE", "ACCEPT"]
    with client.app.state.session_factory() as db:
        candidate = db.scalar(select(Script).where(Script.run_id == run_id))
        assert "失主的嘱托" in candidate.content_json["scenes"][0]["action"]
        assert "失主的嘱托" not in candidate.content_json["scenes"][1]["action"]
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run_id)) == 1
        )
        assert (
            db.scalar(select(func.count()).select_from(Review).where(Review.run_id == run_id)) == 0
        )
        original = copy.deepcopy(candidate.content_json)
    approve(client, run)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run["error_code"], errors, ordinary_errors)
    assert run["pending_review"]["resource_type"] == "storyboard", (run, errors)
    approve(client, run)
    assert run_once(client, settings, run_id)["status"] == "SUCCEEDED"
    count = len(sent)
    assert run_once(client, settings, run_id)["status"] == "SUCCEEDED" and len(sent) == count
    with client.app.state.session_factory() as db:
        assert db.scalar(select(Script).where(Script.run_id == run_id)).content_json == original


def test_invalid_patch_feedback_is_observed_then_repaired_before_candidate(
    client, episode, settings, model_service, monkeypatch
):
    def invalid(arguments, seed):
        arguments["patch"]["estimated_duration_ms"] = 1

    sent, errors, ordinary_errors = install_revision(
        model_service, monkeypatch, settings, invalid_first=invalid
    )
    run_id = start(client, episode, revision_rounds=2)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors, ordinary_errors)
    with client.app.state.session_factory() as db:
        actions = list(
            db.scalars(
                select(AgentLoopAction)
                .join(AgentLoop)
                .where(AgentLoop.run_id == run_id, AgentLoopAction.kind == "TOOL")
            )
        )
        checks = [
            row.result_json["result"]
            for row in actions
            if row.request_json["tool_name"] == PATCH_TOOL
        ]
        assert [row["accepted"] for row in checks] == [False, True]
        assert checks[0]["issues"][0]["code"] == "SCRIPT_DURATION_OUT_OF_RANGE"
    assert any("SCRIPT_DURATION_OUT_OF_RANGE" in str(item["messages"]) for item in sent)


def test_exhausted_revision_forces_saved_question_without_extra_model_call(
    client, episode, settings, model_service, monkeypatch
):
    sent, errors, ordinary_errors = install_revision(
        model_service, monkeypatch, settings, patches=2
    )
    run_id = start(client, episode, revision_rounds=1)
    run = run_once(client, settings, run_id)
    assert run["status"] == "RUNNING", (run, errors, ordinary_errors)
    operations = progress(client, run_id)
    waiting = operations[-1]
    assert (
        waiting["status"] == "WAITING_HUMAN"
        and "送信的动机" in waiting["human_request"]["question"]
    )
    assert len(sent) == 7
    with client.app.state.session_factory() as db:
        deadline = db.get(AgentRunLimit, run_id).deadline_at
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run_id)) == 0
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentLoopQuestion)
                .join(AgentLoop)
                .where(AgentLoop.run_id == run_id)
            )
            == 1
        )
    before = copy.deepcopy(operations)
    run_once(client, settings, run_id)
    assert len(sent) == 7 and progress(client, run_id) == before
    assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
    assert run_once(client, settings, run_id)["status"] == "CANCELLED" and len(sent) == 7
    with client.app.state.session_factory() as db:
        assert db.get(AgentRunLimit, run_id).deadline_at == deadline


@pytest.mark.parametrize("scope", ["scene", "chapter", "action", "dialogue"])
def test_independent_rewrite_keeps_original_scope_across_length_changes_and_one_review(
    client, episode, settings, model_service, monkeypatch, scope
):
    sent, errors, ordinary_errors = install_revision(
        model_service, monkeypatch, settings, patches=2
    )
    base = rewrite_source(client, episode)
    scenes = base["content_json"]["scenes"]
    targets = {
        "scene": {"scope": "scene", "scene_id": scenes[1]["scene_id"]},
        "chapter": {"scope": "chapter", "scene_ids": [s["scene_id"] for s in scenes[:2]]},
        "action": {
            "scope": "paragraph",
            "scene_id": scenes[1]["scene_id"],
            "field": "action",
            "start": 2,
            "end": 8,
        },
        "dialogue": {
            "scope": "paragraph",
            "scene_id": scenes[1]["scene_id"],
            "field": "dialogue",
            "line_id": scenes[1]["dialogue"][0]["line_id"],
            "start": 1,
            "end": 4,
        },
    }
    target = targets[scope]
    payload = rewrite_body(base, target) | {"professional": True, "revision_rounds": 2}
    response = start_rewrite(client, episode, payload)
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    assert start_rewrite(client, episode, payload).json()["run_id"] == str(run_id)
    assert start_rewrite(client, episode, payload | {"revision_rounds": 1}).status_code == 409
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors, ordinary_errors)
    assert run["workflow_version"] == "screenwriter-rewrite/1.0"
    reports = critique_operations(client, run_id)
    assert [item["result"]["decision"] for item in reports] == ["REVISE", "REVISE", "ACCEPT"]
    assert [item["revision"]["remaining_patches"] for item in reports] == [3, 2, 1]
    with client.app.state.session_factory() as db:
        candidate = db.scalar(select(Script).where(Script.run_id == run_id))
        after = candidate.content_json
        assert after["scenes"][2] == scenes[2]
        assert db.get(Script, UUID(base["id"])).content_json == base["content_json"]
        assert db.scalar(select(func.count()).select_from(Review)) == 0
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
        candidate_id = str(candidate.id)
        assert content_hash(after) != content_hash(base["content_json"])
        if scope != "chapter":
            assert after["scenes"][0] == scenes[0]
        if scope in {"action", "dialogue"}:
            old = scenes[1]["action"] if scope == "action" else scenes[1]["dialogue"][0]["text"]
            new = (
                after["scenes"][1]["action"]
                if scope == "action"
                else after["scenes"][1]["dialogue"][0]["text"]
            )
            replacement = "因为答应过失主🙂，他决定把信亲手送回。再确认。"
            assert new == old[: target["start"]] + replacement + old[target["end"] :]
            assert reports[1]["revision"]["working_script"]["scenes"][1] != after["scenes"][1]
        assert run["pending_review"]["version_id"] == str(candidate.id)
    history = client.get(
        f"/api/v1/episodes/{episode['id']}/scripts/{candidate_id}/comparison"
    ).json()["target"]
    assert history["base_version"]["id"] == base["id"]
    assert history["rewrite"]["target"] == RewriteRequest.model_validate(
        rewrite_body(base, target)
    ).target.model_dump(mode="json")
    count = len(sent)
    approve(client, run)
    assert run_once(client, settings, run_id)["status"] == "SUCCEEDED"
    assert len(sent) == count  # Independent rewrites never automatically start a director.
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Review)) == 1
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
        assert db.get(Script, UUID(base["id"])).content_json == base["content_json"]


@pytest.mark.parametrize("category", sorted(CHECKS))
def test_all_five_issue_categories_are_located_and_observable(
    client, episode, settings, model_service, monkeypatch, category
):
    _, errors, ordinary_errors = install_revision(
        model_service, monkeypatch, settings, category=category
    )
    run_id = start(client, episode, revision_rounds=1)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors, ordinary_errors)
    report = critique_operations(client, run_id)[0]
    issue = report["result"]["issues"][0]
    assert issue["category"] == category and issue["evidence"] and issue["scene_id"]
    assert set(report["result"]["checks"]) == CHECKS


@pytest.mark.parametrize(
    "case,code",
    [
        ("source", "SCREENWRITER_CRITIQUE_SCOPE"),
        ("scene", "SCREENWRITER_CRITIQUE_SCOPE"),
        ("line", "SCREENWRITER_CRITIQUE_SCOPE"),
        ("receipt", "SCREENWRITER_PATCH_UNCHECKED"),
        ("checks", "GATEWAY_INVALID_RESPONSE"),
    ],
)
def test_forged_critique_never_publishes_a_candidate(
    client, episode, settings, model_service, monkeypatch, case, code
):
    def mutate(decision, seed):
        result = decision.get("result")
        if not result or result["decision"] != "REVISE":
            return
        if case == "source":
            result["source_sha256"] = "a" * 64
        elif case in {"scene", "line"}:
            result["issues"][0][f"{case}_id"] = str(uuid4())
        elif case == "receipt":
            result["patch_sha256"] = "b" * 64
        else:
            result["checks"] = ["DIALOGUE"] * 5

    _, errors, ordinary_errors = install_revision(
        model_service, monkeypatch, settings, mutate_report=mutate
    )
    run_id = start(client, episode, revision_rounds=1)
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == code, (run, errors, ordinary_errors)
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run_id)) == 0
        )
        assert db.scalar(select(func.count()).select_from(Review)) == 0


def answer_body(waiting, *, decision="ANSWER"):
    return {
        "question_id": waiting["question_id"],
        "input_sha256": waiting["input_sha256"],
        "decision": decision,
        **({"answer": "失主的嘱托已在前集明确，保留当前文本。"} if decision == "ANSWER" else {}),
    }


@pytest.mark.parametrize("decision", ["ANSWER", "STOP"])
def test_exhausted_question_restarts_and_exact_decision_is_idempotent(
    client, episode, settings, model_service, monkeypatch, decision
):
    sent, errors, ordinary_errors = install_revision(
        model_service, monkeypatch, settings, patches=2
    )
    run_id = start(client, episode, revision_rounds=1)
    run_once(client, settings, run_id)
    waiting = progress(client, run_id)[-1]
    original_reports = copy.deepcopy(progress(client, run_id)[:-1])
    with client.app.state.session_factory() as db:
        deadline = db.get(AgentRunLimit, run_id).deadline_at
    endpoint = f"/api/v1/runs/{run_id}/screenwriter/{waiting['loop_id']}/answers"
    body = answer_body(waiting, decision=decision)
    with TestClient(create_app(settings)) as restarted:
        restarted.headers.update(client.headers)
        assert run_once(restarted, settings, run_id)["status"] == "RUNNING" and len(sent) == 7
        assert restarted.post(endpoint, json=body).status_code == 202
        assert restarted.post(endpoint, json=body).status_code == 202
        conflict = answer_body(waiting, decision="STOP" if decision == "ANSWER" else "ANSWER")
        assert restarted.post(endpoint, json=conflict).status_code == 409
        run = run_once(restarted, settings, run_id)
        assert run["status"] == ("WAITING_REVIEW" if decision == "ANSWER" else "CANCELLED"), (
            run,
            errors,
            ordinary_errors,
        )
        assert restarted.post(endpoint, json=body).status_code == 202
        assert progress(restarted, run_id)[:-1] == original_reports
    assert len(sent) == (8 if decision == "ANSWER" else 7)
    with client.app.state.session_factory() as db:
        assert db.get(AgentRunLimit, run_id).deadline_at == deadline


def test_retry_after_saved_patch_replays_completed_round_without_resetting_repairs(
    client, episode, settings, model_service, monkeypatch
):
    failed = []

    def mutate(decision, seed):
        if seed["round"] == 1 and decision.get("result") and not failed:
            failed.append(True)
            decision["result"]["source_sha256"] = "a" * 64

    sent, errors, ordinary_errors = install_revision(
        model_service, monkeypatch, settings, mutate_report=mutate
    )
    run_id = start(client, episode, revision_rounds=1)
    run = run_once(client, settings, run_id)
    assert run["error_code"] == "SCREENWRITER_CRITIQUE_SCOPE", (run, errors, ordinary_errors)
    reports = critique_operations(client, run_id)
    assert reports[0]["status"] == "COMPLETED" and reports[1]["status"] == "STOPPED"
    with client.app.state.session_factory() as db:
        deadline = db.get(AgentRunLimit, run_id).deadline_at
    count = len(sent)
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors, ordinary_errors)
    assert len(sent) == count + 1
    current = critique_operations(client, run_id)
    assert current[:2] == reports
    assert current[2]["revision"]["remaining_patches"] == 0
    with client.app.state.session_factory() as db:
        assert db.get(AgentRunLimit, run_id).deadline_at == deadline


@pytest.mark.parametrize("calls", [4, 5, 6])
def test_root_call_limit_applies_to_critique_tools_rechecks_and_explicit_retry(
    client, episode, settings, model_service, monkeypatch, calls
):
    sent, errors, ordinary_errors = install_revision(model_service, monkeypatch, settings)
    run_id = start(client, episode, revision_rounds=2, agent_limits={"max_model_calls": calls})
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "AGENT_MODEL_CALL_LIMIT", (
        run,
        errors,
        ordinary_errors,
    )
    assert len(sent) == calls
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    assert run_once(client, settings, run_id)["error_code"] == "AGENT_MODEL_CALL_LIMIT"
    assert len(sent) == calls


@pytest.mark.parametrize(
    "case",
    ["missing_check", "unlocated", "accept_issues", "revision_no_receipt", "escalation_no_issues"],
)
def test_critique_contract_rejects_incomplete_or_inconsistent_decisions(case):
    issue = {
        "category": "MOTIVATION",
        "scene_id": str(uuid4()),
        "problem": "动机",
        "evidence": "文本",
        "suggestion": "明确",
    }
    value = {
        "source_sha256": "a" * 64,
        "checks": sorted(CHECKS),
        "issues": [],
        "decision": "ACCEPT",
    }
    if case == "missing_check":
        value["checks"] = ["DIALOGUE"] * 5
    elif case == "unlocated":
        value |= {"decision": "ESCALATE", "issues": [issue | {"scene_id": None}]}
    elif case == "accept_issues":
        value["issues"] = [issue]
    elif case == "revision_no_receipt":
        value |= {"decision": "REVISE", "issues": [issue]}
    else:
        value["decision"] = "ESCALATE"
    with pytest.raises(ValidationError):
        ScriptCritique.model_validate(value)
