"""DIRECT-03 runs the actual gateway, tool, journals, domain services and original reviews."""

import copy
import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_agent_model import call_response
from test_agent_model import model_service as model_service
from test_agent_runtime import final, tool_feedback
from test_director_creation import fragment_value, progress, start
from test_director_creation import install as install_creation
from test_screenwriter_creation import approve, run_once

from arclight.domain.director_revision import CHECK_TOOL, WORKING_VERSION
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoopAction
from arclight.persistence.models import Review, Script, Storyboard, WorkflowRun


def install_revision(
    model_service,
    monkeypatch,
    settings,
    *,
    mutate=None,
    make_patch=None,
    invalid_first=None,
    mutate_report=None,
    on_http=None,
    human_unresolved=False,
    escalate=False,
    revised=False,
    skip_check=False,
    accept_invalid_after_answer=False,
):
    base = []
    _, base_errors = install_creation(base.append, monkeypatch, settings, revised=revised)
    sent, errors, originals = [], [], {}
    invalid_sent = []

    def respond(request):
        payload = json.loads(request.content)
        seed = json.loads(payload["messages"][1]["content"]).get("data", {})
        if seed.get("phase") not in {"DIRECT_WORKING_SCENE", "CHECK_DIRECTOR"}:
            return base[0](request)
        try:
            sent.append(payload)
            if on_http:
                on_http(payload)
            if seed["phase"] == "DIRECT_WORKING_SCENE":
                result = fragment_value(seed) | {"schema_version": WORKING_VERSION}
                originals[result["scene_id"]] = copy.deepcopy(result["shots"])
                if mutate:
                    mutate(result, seed)
                value = final(result=result)
            else:
                feedback = tool_feedback(payload)
                answered = any(
                    "human_decision" in str(item.get("content")) for item in payload["messages"]
                )
                result = {
                    "schema_version": "director-revision/1",
                    "source_sha256": seed["source_sha256"],
                    "check_sha256": "0" * 64,
                    "decision": "ACCEPT",
                    "reason": "真实检查无未解决问题",
                }
                if human_unresolved and answered:
                    value = final("HUMAN_REVIEW")
                elif skip_check:
                    value = final(result=result)
                elif answered and accept_invalid_after_answer and feedback:
                    result["check_sha256"] = feedback[-1]["result"]["check_sha256"]
                    value = final(result=result)
                elif not feedback:
                    value = call_response(
                        (
                            "check-source",
                            CHECK_TOOL,
                            {
                                "operation_key": seed["operation_key"],
                                "source_sha256": seed["source_sha256"],
                            },
                        )
                    )
                else:
                    latest = feedback[-1]
                    checked = latest.get("result") or {}
                    changed = checked.get("content_sha256") not in {None, seed["source_sha256"]}
                    if (
                        latest["status"] == "SUCCEEDED"
                        and checked["accepted"]
                        and (changed or checked["valid"] or escalate)
                    ):
                        result |= {
                            "check_sha256": checked["check_sha256"],
                            "decision": "REVISE"
                            if changed
                            else "ESCALATE"
                            if escalate and not answered
                            else "ACCEPT",
                        }
                        value = final(result=result)
                    else:
                        issues = seed["checks"]["issues"]
                        target = next(
                            (item["scene_id"] for item in issues if item["scene_id"]),
                            seed["source"]["scenes"][0]["scene_id"],
                        )
                        scene = next(
                            item for item in seed["source"]["scenes"] if item["scene_id"] == target
                        )
                        patch = {
                            "scene_id": target,
                            "start": 0,
                            "end": len(scene["shots"]),
                            "shots": copy.deepcopy(originals[target]),
                        }
                        if make_patch:
                            patch = make_patch(seed, patch)
                        arguments = {
                            "operation_key": seed["operation_key"],
                            "source_sha256": seed["source_sha256"],
                            "patch": patch,
                        }
                        if invalid_first and not invalid_sent:
                            invalid_sent.append(True)
                            invalid_first(arguments, seed)
                        value = call_response((f"patch-{len(feedback)}", CHECK_TOOL, arguments))
                if mutate_report and value["choices"][0]["message"].get("content"):
                    message = value["choices"][0]["message"]
                    decision = json.loads(message["content"])
                    mutate_report(decision, seed)
                    message["content"] = json.dumps(decision)
            value["usage"] = {"prompt_tokens": 20, "completion_tokens": 10, "actual_cost": "0"}
            return httpx.Response(200, json=value)
        except Exception as exc:
            errors.append(repr(exc))
            raise

    model_service(respond)
    return sent, errors, base_errors


def checks(client, run_id):
    return [op for op in progress(client, run_id)["operations"] if op["kind"] == "director_check"]


def answer_body(operation, *, decision="ANSWER"):
    return {
        "question_id": operation["question_id"],
        "input_sha256": operation["input_sha256"],
        "decision": decision,
        "answer": "保留原剧本和限额，继续核验" if decision == "ANSWER" else None,
    }


@pytest.mark.parametrize(
    "combined,revised,version",
    [
        (False, False, "director/2.0"),
        (True, False, "production/3.0"),
        (True, True, "production/4.0"),
    ],
)
def test_checked_candidate_uses_original_reviews_and_shared_limits(
    client, episode, settings, model_service, monkeypatch, combined, revised, version
):
    sent, errors, base_errors = install_revision(
        model_service, monkeypatch, settings, revised=revised
    )
    prefix = f"/api/v1/episodes/{episode['id']}"
    if combined:
        response = client.post(
            prefix + "/script-runs",
            headers={"Idempotency-Key": uuid4().hex},
            json={
                "story": "雨夜寻找纸船失主",
                "professional": True,
                "professional_director": True,
                "director_revision_rounds": 3,
                **({"revision_rounds": 1} if revised else {}),
            },
        )
        assert response.status_code == 202, response.text
        run_id = UUID(response.json()["run_id"])
        run = run_once(client, settings, run_id)
        assert run["status"] == "WAITING_REVIEW", (run, errors, base_errors)
        assert not progress(client, run_id)["operations"]
        approve(client, run)
    else:
        run_id, _ = start(client, episode, director_revision_rounds=3)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors, base_errors)
    assert run["workflow_version"] == version
    reports = checks(client, run_id)
    assert [op["result"]["decision"] for op in reports] == ["ACCEPT"]
    assert reports[0]["observations"][-1]["feedback"]["result"]["valid"] is True
    assert reports[0]["director_revision"]["initial_check"]["issue_count"] == 0
    approve(client, run)
    assert run_once(client, settings, run_id)["status"] == "SUCCEEDED"
    count = len(sent)
    assert run_once(client, settings, run_id)["status"] == "SUCCEEDED" and len(sent) == count
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 1
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 1
        assert db.scalar(select(Script.status)) == "APPROVED"
        assert db.get(WorkflowRun, run_id).input_json["director"]["revision_rounds"] == 3


@pytest.mark.parametrize(
    "fault,code",
    [
        ("missing", "DIRECTOR_DIALOGUE_MISSING"),
        ("duplicate", "DIRECTOR_DIALOGUE_DUPLICATE"),
        ("line", "DIRECTOR_DIALOGUE_REFERENCE"),
        ("number", "DIRECTOR_SHOT_ORDER"),
        ("scene", "DIRECTOR_SHOT_SCENE"),
        ("duration", "DIRECTOR_SHOT_DURATION"),
        ("total", "STORYBOARD_DURATION_OUT_OF_RANGE"),
        ("speaker", "DIRECTOR_SHOT_REFERENCE"),
        ("asset", "INVALID_ASSET_REFERENCE"),
    ],
)
def test_actual_feedback_selects_patch_and_rechecks_before_publication(
    client, episode, settings, model_service, monkeypatch, fault, code
):
    def mutate(result, seed):
        if seed["scene"]["scene_no"] != 2:
            return
        shot = result["shots"][0]
        if fault == "missing":
            shot["dialogue_line_ids"] = []
        elif fault == "duplicate":
            shot["dialogue_line_ids"] *= 2
        elif fault == "line":
            shot["dialogue_line_ids"] = [str(uuid4())]
        elif fault == "number":
            shot["shot_no"] = -1
        elif fault == "scene":
            shot["scene_id"] = str(uuid4())
        elif fault == "duration":
            shot["duration_ms"] = 1
        elif fault == "total":
            for item in result["shots"]:
                item["duration_ms"] = 30000
        elif fault == "speaker":
            shot["characters"] = []
        else:
            shot["props"] = [{"asset_id": str(uuid4()), "asset_version_id": str(uuid4())}]

    sent, errors, base_errors = install_revision(
        model_service, monkeypatch, settings, mutate=mutate
    )
    run_id, _ = start(client, episode, director_revision_rounds=2)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors, base_errors)
    reports = checks(client, run_id)
    assert [op["result"]["decision"] for op in reports] == ["REVISE", "ACCEPT"]
    feedback = reports[0]["observations"][0]["feedback"]["result"]
    assert code in {issue["code"] for issue in feedback["issues"]}
    assert any(code in str(item["messages"]) for item in sent)
    assert reports[0]["director_revision"]["patches"][-1]["selected"] is True
    assert reports[1]["director_revision"]["remaining_patches"] == 1
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Review)) == 0
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentLoopAction)
                .where(AgentLoopAction.kind == "TOOL")
            )
            >= 6
        )


def test_multiround_partial_patches_protect_other_scenes(
    client, episode, settings, model_service, monkeypatch
):
    def mutate(result, seed):
        result["shots"][0]["dialogue_line_ids"] = []

    _, errors, base_errors = install_revision(model_service, monkeypatch, settings, mutate=mutate)
    run_id, _ = start(client, episode, director_revision_rounds=3)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors, base_errors)
    reports = checks(client, run_id)
    assert [op["result"]["decision"] for op in reports] == ["REVISE"] * 3 + ["ACCEPT"]
    for before, after in zip(reports, reports[1:], strict=False):
        patch = before["director_revision"]["patches"][-1]
        assert patch["selected"] and patch["check"]["accepted"]
        for scene_before, scene_after in zip(
            before["director_revision"]["working_storyboard"]["scenes"],
            after["director_revision"]["working_storyboard"]["scenes"],
            strict=True,
        ):
            if scene_before["scene_id"] != patch["patch"]["scene_id"]:
                assert scene_before == scene_after
    assert reports[-1]["director_revision"]["initial_check"]["valid"]
