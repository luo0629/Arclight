"""DIRECT-04 exercises the real Runtime, art comparison, receipts and original review graph."""

import json
from uuid import uuid4

import httpx
import pytest
from art_direction_support import install_direction_model, reference_set
from sqlalchemy import func, select
from test_agent_model import call_response
from test_agent_model import model_service as model_service
from test_agent_runtime import final, tool_feedback
from test_art_direction import facts
from test_director_creation import progress, start
from test_director_revision import answer_body, install_revision
from test_screenwriter_creation import approve, run_once

from arclight.application.agent_limits import delegation_count
from arclight.domain.director_collaboration import OBSERVE_OPINION, REQUEST_OPINION
from arclight.persistence.agent_limits import AgentAction, AgentRunLimit


def install_collaboration(
    model_service,
    monkeypatch,
    settings,
    *,
    art_human=False,
    decision="ADOPT",
    mutate_request=None,
    mutate_choice=None,
    on_art=None,
    on_director=None,
    art_options=None,
    repeat_request=None,
    skip_observe=False,
    consult=True,
    revised=False,
):
    director, art = [], []
    sent, errors, base_errors = install_revision(
        director.append, monkeypatch, settings, revised=revised
    )
    art_sent, art_errors, _ = install_direction_model(
        art.append, human=art_human, on_http=on_art, **(art_options or {})
    )

    def respond(request):
        payload = json.loads(request.content)
        seed = json.loads(payload["messages"][1]["content"]).get("data", {})
        if "task" in seed and "visual_goal" in seed["task"]:
            return art[0](request)
        if on_director:
            on_director(payload)
        if seed.get("phase") != "CHECK_DIRECTOR" or seed["round"] != 0 or not consult:
            return director[0](request)
        feedback = tool_feedback(payload)
        requested = [item for item in feedback if item["tool_name"] == REQUEST_OPINION]
        observed = [item for item in feedback if item["tool_name"] == OBSERVE_OPINION]
        if not requested:
            arguments = {
                "operation_key": seed["operation_key"],
                "source_sha256": seed["source_sha256"],
                "task": {"visual_goal": "比较雨夜角色构图，提供导演工作稿视觉意见"},
            }
            if mutate_request:
                mutate_request(arguments, seed)
            return httpx.Response(200, json=call_response(("ask-art", REQUEST_OPINION, arguments)))
        if requested[-1]["status"] != "SUCCEEDED":
            return httpx.Response(200, json=final("HUMAN_REVIEW"))
        if repeat_request and len(requested) == 1:
            arguments = {
                key: requested[0]["result"][key]
                for key in ("operation_key", "source_sha256", "task")
            }
            if repeat_request == "changed":
                arguments["task"] = arguments["task"] | {"visual_goal": "第二份不同的意见"}
            return httpx.Response(
                200, json=call_response(("ask-again", REQUEST_OPINION, arguments))
            )
        if not observed and not skip_observe:
            value = requested[-1]["result"]
            arguments = {
                key: value[key] for key in ("delegation_id", "operation_key", "source_sha256")
            }
            return httpx.Response(200, json=call_response(("read-art", OBSERVE_OPINION, arguments)))
        # Reuse the existing director model's real checks and local-patch behavior.
        filtered = [
            message
            for message in payload["messages"]
            if not (
                message["role"] == "tool"
                and json.loads(message["content"])["tool_name"]
                in {REQUEST_OPINION, OBSERVE_OPINION}
            )
        ]
        forwarded = httpx.Request("POST", request.url, json=payload | {"messages": filtered})
        response = director[0](forwarded)
        value = response.json()
        message = value["choices"][0]["message"]
        if message.get("content"):
            report = json.loads(message["content"])
            if observed and report.get("result") is not None:
                result = observed[-1]["result"]
                choice = {
                    "delegation_id": result["delegation_id"],
                    "observation_sha256": result["observation_sha256"],
                    "decision": decision,
                    "reason": "依据真实多图比较采纳构图意见"
                    if decision == "ADOPT"
                    else "保留原调度方案",
                }
                if mutate_choice:
                    mutate_choice(choice)
                report["result"]["opinion_decision"] = choice
            message["content"] = json.dumps(report)
        return httpx.Response(200, json=value)

    model_service(respond)
    return sent, art_sent, errors, base_errors, art_errors


def begin(client, episode, **options):
    return start(
        client, episode, director_revision_rounds=2, director_art_collaboration=True, **options
    )


def opinion_operations(client, run_id):
    return [
        item for item in progress(client, run_id)["operations"] if item["kind"] == "art_opinion"
    ]


@pytest.mark.parametrize("decision", ["ADOPT", "DECLINE"])
def test_real_art_advice_precedes_final_storyboard_and_records_exact_choice(
    client, episode, settings, model_service, monkeypatch, decision
):
    reference_set(client, episode)
    sent, art_sent, *errors = install_collaboration(
        model_service, monkeypatch, settings, decision=decision
    )
    run_id, source = begin(client, episode)
    before = facts(client)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (
        run.get("error_code"),
        progress(client, run_id),
        errors,
    )
    assert not any(errors), errors
    assert run["workflow_version"] == "director/3.0"
    data = progress(client, run_id)
    request = data["art_opinion_request"]
    assert request["storyboard_status"] == "WORK_IN_PROGRESS"
    assert request["script_version_id"] == source["id"]
    assert request["working_storyboard"]["scenes"]
    assert len(opinion_operations(client, run_id)) == 1
    checks = [item for item in data["operations"] if item["kind"] == "director_check"]
    choice = checks[0]["result"]["opinion_decision"]
    assert choice["decision"] == decision and choice["reason"]
    assert choice["delegation_id"] == request["delegation_id"]
    observed = next(
        item["feedback"]["result"]
        for item in checks[0]["observations"]
        if item["tool"] == OBSERVE_OPINION
    )
    assert choice["observation_sha256"] == observed["observation_sha256"]
    assert observed["status"] == "ADVICE" and len(art_sent) == 4
    assert all(
        {tool["function"]["name"] for tool in payload["tools"]}
        == {"investigate_art", "compare_art_options"}
        for payload in art_sent
    )
    envelope = json.loads(art_sent[0]["messages"][2]["content"])["data"]["director_opinion"]
    assert envelope == request
    after = facts(client)
    assert after["ArtProposalVersion"] == before["ArtProposalVersion"]
    assert after["ArtApprovedVersion"] == before["ArtApprovedVersion"]
    original_directories = {row[0]: row for row in before["directories"]}
    current_directories = {row[0]: row for row in after["directories"]}
    assert {key: current_directories[key] for key in original_directories} == original_directories
    assert all(
        row[5] == "DRAFT" and row[6].get("content_registry")
        for key, row in current_directories.items()
        if key not in original_directories
    )
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 1
        assert delegation_count(db, run_id) == 1
        assert {row.run_id for row in db.scalars(select(AgentAction))} == {run_id}
        assert {row.root_run_id for row in db.scalars(select(AgentAction))} == {run_id}
    approve(client, run)
    assert run_once(client, settings, run_id)["status"] == "SUCCEEDED"
    calls = len(sent), len(art_sent)
    assert run_once(client, settings, run_id)["status"] == "SUCCEEDED"
    assert (len(sent), len(art_sent)) == calls


def test_art_clarification_resumes_original_run_and_opinion_without_publishing_assets(
    client, episode, settings, model_service, monkeypatch
):
    reference_set(client, episode)
    _, art_sent, *errors = install_collaboration(
        model_service, monkeypatch, settings, art_human=True
    )
    run_id, _ = begin(client, episode)
    before = facts(client)
    run = run_once(client, settings, run_id)
    assert run["status"] == "RUNNING", (run, errors)
    operation = opinion_operations(client, run_id)[0]
    assert operation["status"] == "WAITING_HUMAN" and progress(client, run_id)["can_cancel"]
    assert facts(client) == before
    original_loop, original_calls = operation["loop_id"], len(art_sent)
    assert run_once(client, settings, run_id)["status"] == "RUNNING"
    assert len(art_sent) == original_calls
    response = client.post(
        f"/api/v1/runs/{run_id}/director/{operation['loop_id']}/answers",
        json=answer_body(operation) | {"answer": operation["human_request"]["options"][0]},
    )
    assert response.status_code == 202, response.text
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (
        run.get("error_code"),
        progress(client, run_id),
        errors,
    )
    assert not any(errors), errors
    operations = opinion_operations(client, run_id)
    assert len(operations) == 1 and operations[0]["loop_id"] == original_loop
    assert operations[0]["status"] == "COMPLETED"


@pytest.mark.parametrize(
    "limits,code",
    [
        ({"max_delegations": 0}, "AGENT_DELEGATION_LIMIT"),
        ({"max_delegation_depth": 0}, "AGENT_DELEGATION_DEPTH"),
    ],
)
def test_delegation_limits_block_before_art_model_io(
    client, episode, settings, model_service, monkeypatch, limits, code
):
    reference_set(client, episode)
    _, art_sent, *errors = install_collaboration(model_service, monkeypatch, settings)
    run_id, _ = begin(client, episode, agent_limits=limits)
    before = facts(client)
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == code, (run, errors)
    assert art_sent == [] and facts(client) == before


@pytest.mark.parametrize("mode", ["skip", "forged"])
def test_missing_or_forged_advice_receipt_cannot_publish(
    client, episode, settings, model_service, monkeypatch, mode
):
    reference_set(client, episode)
    install_collaboration(
        model_service,
        monkeypatch,
        settings,
        skip_observe=mode == "skip",
        mutate_choice=(lambda value: value.update(observation_sha256="a" * 64))
        if mode == "forged"
        else None,
    )
    run_id, _ = begin(client, episode)
    before = facts(client)
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "ART_OPINION_UNVERIFIED", run
    assert facts(client) == before


def test_collaboration_entry_requires_explicit_checked_director_and_exact_idempotency(
    client, episode
):
    from test_director_creation import approved_script

    source = approved_script(client, episode)
    path = f"/api/v1/episodes/{episode['id']}/storyboard-runs"
    key = {"Idempotency-Key": uuid4().hex}
    invalid = {"script_version_id": source["id"], "director_art_collaboration": True}
    assert client.post(path, headers=key, json=invalid).status_code == 422
    body = invalid | {"professional": True, "director_revision_rounds": 2}
    response = client.post(path, headers=key, json=body)
    assert response.status_code == 202, response.text
    assert client.post(path, headers=key, json=body).json()["run_id"] == response.json()["run_id"]
    conflict = client.post(path, headers=key, json=body | {"director_art_collaboration": False})
    assert (
        conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )


@pytest.mark.parametrize("repeat", ["same", "changed"])
def test_repeated_request_reuses_one_delegation_and_rejects_changed_task(
    client, episode, settings, model_service, monkeypatch, repeat
):
    reference_set(client, episode)
    _, art_sent, *errors = install_collaboration(
        model_service, monkeypatch, settings, repeat_request=repeat
    )
    run_id, _ = begin(client, episode)
    run = run_once(client, settings, run_id)
    data = progress(client, run_id)
    check = next(item for item in data["operations"] if item["kind"] == "director_check")
    requests = [
        item["feedback"] for item in check["observations"] if item["tool"] == REQUEST_OPINION
    ]
    assert len(requests) == 2 and requests[0]["status"] == "SUCCEEDED"
    if repeat == "same":
        assert run["status"] == "WAITING_REVIEW", (run, errors)
        assert requests[0]["result"] == requests[1]["result"] and len(art_sent) == 4
    else:
        assert requests[1]["error"]["code"] == "ART_OPINION_CONFLICT"
        assert run["status"] == "RUNNING" and art_sent == []
        assert not opinion_operations(client, run_id)
    with client.app.state.session_factory() as db:
        assert delegation_count(db, run_id) == 1


@pytest.mark.parametrize(
    "field,value", [("source_sha256", "b" * 64), ("operation_key", "foreign-operation")]
)
def test_forged_working_source_cannot_delegate(
    client, episode, settings, model_service, monkeypatch, field, value
):
    reference_set(client, episode)
    _, art_sent, *_ = install_collaboration(
        model_service,
        monkeypatch,
        settings,
        mutate_request=lambda args, _: args.update({field: value}),
    )
    run_id, _ = begin(client, episode)
    before = facts(client)
    run_once(client, settings, run_id)
    data = progress(client, run_id)
    check = next(item for item in data["operations"] if item["kind"] == "director_check")
    observation = next(
        item["feedback"] for item in check["observations"] if item["tool"] == REQUEST_OPINION
    )
    assert observation["status"] == "FAILED"
    assert observation["error"]["code"] in {"ART_OPINION_STALE", "DIRECTOR_REVISION_SCOPE"}
    assert data["art_opinion_request"] is None and not art_sent
    assert facts(client) == before


def test_art_cost_reservations_share_original_root_budget_without_reset(
    client, episode, settings, model_service, monkeypatch
):
    from decimal import Decimal

    from test_generation_budget import set_policy

    from arclight.application.agent_limits import action_usage
    from arclight.domain.model_gateway import GatewayPolicy

    reference_set(client, episode)
    set_policy(client, episode["project_id"], "100")
    _, art_sent, *_ = install_collaboration(model_service, monkeypatch, settings)
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost="0.2", pricing_version="direct04-budget:1"
    )
    run_id, _ = begin(client, episode, agent_limits={"maximum_cost": "1.9"})
    before = facts(client)
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "AGENT_BUDGET_EXCEEDED", run
    calls = len(art_sent)
    assert 0 < calls < 4 and facts(client) == before
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 1
        usage = action_usage(db, run_id)
        models = [action for action in usage["actions"] if action.kind == "MODEL"]
        assert {action.agent_name for action in models} == {"director", "art_director"}
        assert len([action for action in models if action.agent_name == "art_director"]) == calls
        assert usage["actual_cost"] == 0
        assert usage["pending_estimate"] == Decimal("0.2") * len(models)
        assert usage["actual_cost"] + usage["pending_estimate"] <= Decimal("1.9")
        assert usage["actual_cost"] + usage["pending_estimate"] + Decimal("0.2") > Decimal("1.9")
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    assert run_once(client, settings, run_id)["error_code"] == "AGENT_BUDGET_EXCEEDED"
    assert len(art_sent) == calls
