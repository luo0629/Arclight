"""SERIES-06 exercises real intake, Reviewer protocol, reports and human gates."""

import copy
import json
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_agent_model import model_service as model_service  # noqa: F401
from test_agent_runtime import final
from test_script_state_commit import capture, save, screenplay
from test_series_planning import planned_outline as planned_outline  # noqa: F401
from test_story_ledger import approve_ledger, approved_script, ledger_content, save_ledger
from test_writing_context import writing_case as writing_case  # noqa: F401

from arclight.application import agent_limits
from arclight.application.agent_project_reads import ProjectReads
from arclight.application.narrative_review import read_work
from arclight.application.narrative_review_evidence import validate_assessment
from arclight.application.runtime import execute_run
from arclight.core.errors import DomainError
from arclight.domain.model_gateway import GatewayPolicy
from arclight.domain.narrative_review import CATEGORIES, NarrativeAssessment
from arclight.main import create_app
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.models import AssetVersion, Episode, OutboxEvent, Project, WorkflowRun


def prepare(client, case):
    content = screenplay(case, capture(client, case))
    saved = save(client, case, content)
    return saved, content


def payload(case, script, content, **changes):
    return {
        "creation_key": str(uuid4()),
        "scope": "SCENE",
        "baseline": {
            key: case["request"][key]
            for key in (
                "series_plan_version_id",
                "ledger_version_id",
                "expected_ledger_head_revision",
            )
        },
        "targets": [
            {"script_version_id": script["id"], "scene_ids": [content["scenes"][0]["scene_id"]]}
        ],
        **changes,
    }


def endpoint(case):
    return f"/api/v1/projects/{case['episode']['project_id']}/narrative-reviews"


def start(client, case, body):
    result = client.post(endpoint(case), json=body)
    assert result.status_code == 202, result.text
    return result.json()["run_id"]


def progress(client, case, run_id):
    result = client.get(endpoint(case) + "/" + run_id)
    assert result.status_code == 200, result.text
    return result.json()


def execute(client, settings, run_id):
    with client.app.state.session_factory() as db:
        revision = db.get(WorkflowRun, UUID(run_id)).revision
    execute_run(client.app.state.session_factory, settings, UUID(run_id), revision)


def assessment(work, *, blocker=False):
    source = next(item for item in work["evidence"] if item["kind"] == "SCENE")
    checks = [
        {
            "category": category,
            "status": "CLEAR",
            "summary": "按本次精确证据核对，无已识别矛盾。",
            "evidence_ids": [source["id"]],
            "issues": [],
        }
        for category in sorted(CATEGORIES)
    ]
    if blocker:
        checks[0].update(
            status="ISSUE",
            summary="人物行动与前集因果冲突。",
            issues=[
                {
                    "severity": "BLOCKER",
                    "summary": "此处声称信已经烧毁，与精确来源相矛盾。",
                    "location": source["location"],
                    "evidence_ids": [source["id"]],
                }
            ],
        )
    return {"summary": "叙事审查仅作为建议，仍须连续观片。", "checks": checks}


def human_decision(value, *, approve=True):
    report, human = value["report"], value["human_review"]
    checks = report["assessment"]["checks"] if report["assessment"] else []
    result = {
        "version_id": human["snapshot"]["version_id"],
        "request_sha256": human["request_sha256"],
        "decision": "APPROVE" if approve else "REJECT",
        "comment": "逐集连续观片并核对精确稿件，说明各项限制的处理。",
    }
    if approve:
        result["narrative_content"] = {
            "report_id": report["report_id"],
            "input_sha256": report["input_sha256"],
            "continuous_viewing_confirmed": True,
            "episode_ids": list(
                dict.fromkeys(
                    item["location"]["episode_id"]
                    for item in value["work"]["evidence"]
                    if item["kind"] == "SCENE"
                )
            ),
            "limitation_resolutions": [
                "已在对应原始素材中逐项人工核对。" for _ in report["limitations"]
            ],
            "warning_resolutions": [
                "已核实为允许的叙事安排，记录接受理由。"
                for check in checks
                for issue in check["issues"]
                if issue["severity"] == "WARNING"
            ],
        }
    return result


def test_mock_capability_gap_always_requests_human_and_does_not_approve_sources(
    client, writing_case, settings
):
    saved, content = prepare(client, writing_case)
    run_id = start(client, writing_case, payload(writing_case, saved, content))
    execute(client, settings, run_id)
    value = progress(client, writing_case, run_id)
    assert value["status"] == "WAITING_REVIEW", value
    assert value["report"]["conclusion"] == "HUMAN_REVIEW"
    assert value["report"]["assessment"] is None
    assert value["report"]["human_content_approval_required"] is True
    assert value["human_review"]["snapshot"]["kind"] == "NARRATIVE"
    assert value["human_review"]["status"] == "WAITING_REVIEW"
    with client.app.state.session_factory() as db:
        assert db.get(AssetVersion, UUID(saved["id"])).status == saved["status"]


def test_idempotent_intake_freezes_limits_and_dispatch_does_not_duplicate_reports(
    client, writing_case, settings
):
    saved, content = prepare(client, writing_case)
    body = payload(writing_case, saved, content)
    run_id = start(client, writing_case, body)
    assert start(client, writing_case, body) == run_id
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.run_id == UUID(run_id))
            )
            == 1
        )
    conflict = client.post(
        endpoint(writing_case), json=body | {"agent_limits": {"max_model_calls": 1}}
    )
    assert conflict.status_code == 409
    execute(client, settings, run_id)
    first = progress(client, writing_case, run_id)
    execute(client, settings, run_id)
    assert progress(client, writing_case, run_id) == first


@pytest.mark.parametrize("kind", ["SERIES", "ADJACENT_EPISODES"])
def test_partial_series_and_nonadjacent_episodes_cannot_claim_full_coverage(
    client, writing_case, kind
):
    saved, content = prepare(client, writing_case)
    body = payload(
        writing_case, saved, content, scope=kind, targets=[{"script_version_id": saved["id"]}]
    )
    if kind == "ADJACENT_EPISODES":
        other = client.post(
            f"/api/v1/projects/{writing_case['episode']['project_id']}/episodes",
            json={"episode_no": 3, "title": "尾集", "target_duration_ms": 30000},
        )
        assert other.status_code == 201, other.text
        case = copy.deepcopy(writing_case)
        case["episode"] = other.json()
        later, _ = prepare(client, case)
        body["targets"].append({"script_version_id": later["id"]})
    response = client.post(endpoint(writing_case), json=body)
    assert response.status_code == 409, response.text


def test_reviewer_uses_exact_evidence_and_blocker_cannot_be_approved(
    client, writing_case, settings, request
):
    saved, content = prepare(client, writing_case)
    model = request.getfixturevalue("model_service")
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost=Decimal("0"), pricing_version="narrative-test:1"
    )
    run_id = start(client, writing_case, payload(writing_case, saved, content))
    value = progress(client, writing_case, run_id)
    calls = []

    def respond(req):
        calls.append(req)
        assert saved["id"] in req.content.decode()
        return httpx.Response(200, json=final(result=assessment(value["work"], blocker=True)))

    model(respond)
    execute(client, settings, run_id)
    value = progress(client, writing_case, run_id)
    assert value["status"] == "WAITING_REVIEW", value
    assert len(calls) == 1
    assert value["report"]["assessment"]["checks"][0]["issues"][0]["severity"] == "BLOCKER"
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, UUID(run_id))
        episode = db.get(Episode, run.episode_id)
        actor = db.get(Project, episode.project_id).owner_id
        evidence = ProjectReads(db, actor).arclight_read_review_evidence(run.id, "review")
    (report,) = evidence["review_reports"]
    assert report["kind"] == "NARRATIVE" and report["report"] == value["report"]
    assert report["cited_locations"]
    human_id = value["human_review"]["id"]
    response = client.post(
        f"/api/v1/human-reviews/{human_id}/decisions", json=human_decision(value)
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "NARRATIVE_REVISION_REQUIRED"


def test_human_decision_and_resume_are_separate_and_exact(client, writing_case, settings):
    saved, content = prepare(client, writing_case)
    run_id = start(client, writing_case, payload(writing_case, saved, content))
    execute(client, settings, run_id)
    value = progress(client, writing_case, run_id)
    human_id = value["human_review"]["id"]
    body = human_decision(value)
    no_viewing = dict(body)
    no_viewing.pop("narrative_content")
    assert (
        client.post(f"/api/v1/human-reviews/{human_id}/decisions", json=no_viewing).status_code
        == 422
    )
    decided = client.post(f"/api/v1/human-reviews/{human_id}/decisions", json=body)
    assert decided.status_code == 200, decided.text
    assert progress(client, writing_case, run_id)["status"] == "WAITING_REVIEW"
    resumed = client.post(
        f"/api/v1/human-reviews/{human_id}/resume",
        json={"decision_id": decided.json()["id"], "request_sha256": body["request_sha256"]},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["result_json"]["status"] == "APPROVED"
    assert progress(client, writing_case, run_id)["status"] == "SUCCEEDED"


@pytest.mark.parametrize("field", ["line_id", "scene_id", "evidence", "time_ms"])
def test_fabricated_line_scene_evidence_or_unsampled_time_is_rejected(client, writing_case, field):
    saved, content = prepare(client, writing_case)
    run_id = start(client, writing_case, payload(writing_case, saved, content))
    value = progress(client, writing_case, run_id)
    result = assessment(value["work"], blocker=True)
    issue = result["checks"][0]["issues"][0]
    if field == "evidence":
        issue["evidence_ids"] = [str(uuid4())]
    elif field == "time_ms":
        issue["location"].update(time_ms=100, shot_id=str(uuid4()), candidate_id=str(uuid4()))
    else:
        issue["location"][field] = str(uuid4())
    with client.app.state.session_factory() as db:
        work = read_work(db, UUID(writing_case["episode"]["project_id"]), UUID(run_id))
        with pytest.raises(DomainError) as raised:
            validate_assessment(work, NarrativeAssessment.model_validate(result))
        assert raised.value.code == "NARRATIVE_REVIEW_CITATION_INVALID"


def test_stale_nonanchor_episode_blocks_publication_and_human_approval(
    client, writing_case, settings
):
    saved, content = prepare(client, writing_case)
    other = client.post(
        f"/api/v1/projects/{writing_case['episode']['project_id']}/episodes",
        json={"episode_no": 2, "title": "后集", "target_duration_ms": 30000},
    )
    assert other.status_code == 201
    case = copy.deepcopy(writing_case)
    case["episode"] = other.json()
    later, _ = prepare(client, case)
    body = payload(
        writing_case,
        saved,
        content,
        scope="ADJACENT_EPISODES",
        targets=[{"script_version_id": saved["id"]}, {"script_version_id": later["id"]}],
    )
    run_id = start(client, writing_case, body)
    execute(client, settings, run_id)
    value = progress(client, writing_case, run_id)
    with client.app.state.session_factory.begin() as db:
        row = db.get(Episode, UUID(case["episode"]["id"]))
        row.status = "ARCHIVED"
    decision = client.post(
        f"/api/v1/human-reviews/{value['human_review']['id']}/decisions", json=human_decision(value)
    )
    assert decision.status_code == 409, decision.text


def test_rereview_keeps_prior_history_and_inherits_shared_limits(client, writing_case, settings):
    saved, content = prepare(client, writing_case)
    run_id = start(client, writing_case, payload(writing_case, saved, content))
    execute(client, settings, run_id)
    first = progress(client, writing_case, run_id)
    rejected = client.post(
        f"/api/v1/human-reviews/{first['human_review']['id']}/decisions",
        json=human_decision(first, approve=False),
    )
    assert rejected.status_code == 200, rejected.text
    finished = client.post(
        f"/api/v1/human-reviews/{first['human_review']['id']}/resume",
        json={
            "decision_id": rejected.json()["id"],
            "request_sha256": first["human_review"]["request_sha256"],
        },
    )
    assert finished.status_code == 200, finished.text
    newer = copy.deepcopy(content)
    newer["scenes"][0]["dialogue"][0]["text"] = "等过了河，我就向你解释。"
    revision = save(client, writing_case, newer, expected=1, base=saved["id"])
    next_run = start(
        client,
        writing_case,
        payload(writing_case, revision, newer, previous_report_id=first["report"]["report_id"]),
    )
    execute(client, settings, next_run)
    second = progress(client, writing_case, next_run)
    assert second["report"]["previous_report_id"] == first["report"]["report_id"]
    assert progress(client, writing_case, run_id)["report"] == first["report"]
    with client.app.state.session_factory() as db:
        root = db.get(AgentRunLimit, UUID(run_id))
        child = db.get(AgentRunLimit, UUID(next_run))
        assert child.root_run_id == root.root_run_id
        assert child.parent_run_id == root.run_id
        assert child.deadline_at == root.deadline_at
        from arclight.application.narrative_review_human import completed_review_parent

        parent_run = db.get(WorkflowRun, UUID(run_id))
        child_run = db.get(WorkflowRun, UUID(next_run))
        assert completed_review_parent(db, parent_run, child_run)
        unrelated = WorkflowRun(
            id=child_run.id,
            workflow_type="continuity_review",
            workflow_version=child_run.workflow_version,
            input_json={"narrative_review": {"previous_report_id": str(uuid4())}},
        )
        assert not completed_review_parent(db, parent_run, unrelated)
        unrelated.input_json = child_run.input_json
        unrelated.workflow_version = "continuity-review/1"
        assert not completed_review_parent(db, parent_run, unrelated)
        with pytest.raises(DomainError) as finished_parent:
            agent_limits.validate_agent_execution(db, parent_run.id, "storyboard")
        assert finished_parent.value.code == "AGENT_NOT_ACTIVE"
    assert len(client.get(endpoint(writing_case)).json()) == 2


def test_whole_series_requires_and_freezes_every_planned_episode(client, writing_case, settings):
    targets = []
    for number in (1, 2, 3):
        case = copy.deepcopy(writing_case)
        if number != 1:
            response = client.post(
                f"/api/v1/projects/{case['episode']['project_id']}/episodes",
                json={"episode_no": number, "title": f"第{number}集", "target_duration_ms": 30000},
            )
            assert response.status_code == 201, response.text
            case["episode"] = response.json()
        saved, content = prepare(client, case)
        targets.append({"script_version_id": saved["id"]})
    run_id = start(
        client, writing_case, payload(writing_case, saved, content, scope="SERIES", targets=targets)
    )
    execute(client, settings, run_id)
    value = progress(client, writing_case, run_id)
    assert value["status"] == "WAITING_REVIEW", value
    assert {
        item["location"]["episode_no"]
        for item in value["work"]["evidence"]
        if item["kind"] == "SCENE"
    } == {1, 2, 3}
    incomplete = human_decision(value)
    incomplete["narrative_content"]["episode_ids"].pop()
    response = client.post(
        f"/api/v1/human-reviews/{value['human_review']['id']}/decisions", json=incomplete
    )
    assert response.status_code == 422, response.text


def test_story_time_and_character_knowledge_keep_costume_injury_and_flashback_distinct(
    client, writing_case
):
    case = writing_case
    original = approved_script(client, case["episode"])
    content = ledger_content(case["episode"], original, case["characters"][0]["id"])
    base = content["entries"][0]
    content["entries"].extend(
        [
            {
                **copy.deepcopy(base),
                "id": str(uuid4()),
                "kind": "COSTUME",
                "state_key": "costume",
                "value": "红色外衣",
                "story_start": 0,
                "story_end": 5,
            },
            {
                **copy.deepcopy(base),
                "id": str(uuid4()),
                "kind": "COSTUME",
                "state_key": "costume",
                "value": "批准换穿蓝色外衣",
                "story_start": 5,
            },
            {
                **copy.deepcopy(base),
                "id": str(uuid4()),
                "kind": "INJURY",
                "state_key": "injury",
                "value": "批准的右手伤势",
                "story_start": 5,
            },
        ]
    )
    created = save_ledger(client, case["episode"], content)
    approved = approve_ledger(client, case["episode"], created)
    case["request"].update(
        ledger_version_id=created["version"]["id"],
        expected_ledger_head_revision=approved["head"]["revision"],
    )
    context = capture(client, case, target_story_time=10)
    script_content = screenplay(case, context)
    flashback = copy.deepcopy(script_content["scenes"][0])
    flashback.update(scene_id=str(uuid4()), scene_no=2, title="过去的回忆")
    flashback["dramatic_beat"]["story_time"] = -5
    flashback["dialogue"][0]["line_id"] = str(uuid4())
    flashback["dramatic_beat"]["dialogue_interactions"][0]["line_ids"] = [
        flashback["dialogue"][0]["line_id"]
    ]
    flashback["state_changes"] = []
    script_content["scenes"].append(flashback)
    saved = save(client, case, script_content, expected=1, base=original["id"])
    run_id = start(client, case, payload(case, saved, script_content))
    current = progress(client, case, run_id)["work"]
    author = next(item for item in current["evidence"] if item["kind"] == "AUTHOR_STATE")["value"][
        "projection"
    ]
    assert {item["value"] for item in author["facts"]} == {
        "已经收到红信",
        "批准换穿蓝色外衣",
        "批准的右手伤势",
    }
    assert len(author["planned"]) == len(author["proposals"]) == 1
    characters = [
        item["value"]["projection"]
        for item in current["evidence"]
        if item["kind"] == "CHARACTER_STATE"
    ]
    assert characters[1]["facts"] == []
    assert all(not item["planned"] and not item["proposals"] for item in characters)
    body = payload(case, saved, script_content)
    body["targets"][0]["scene_ids"] = [flashback["scene_id"]]
    before_id = start(client, case, body)
    before = progress(client, case, before_id)["work"]
    assert all(
        item["value"]["projection"]["facts"] == []
        for item in before["evidence"]
        if item["kind"] in {"AUTHOR_STATE", "CHARACTER_STATE"}
    )


def test_no_model_budget_escalates_without_network_call(client, writing_case, settings, request):
    saved, content = prepare(client, writing_case)
    model = request.getfixturevalue("model_service")
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost=Decimal("0"), pricing_version="narrative-test:1"
    )
    calls = []
    model(lambda req: calls.append(req) or httpx.Response(500))
    run_id = start(
        client,
        writing_case,
        payload(writing_case, saved, content, agent_limits={"max_model_calls": 0}),
    )
    execute(client, settings, run_id)
    value = progress(client, writing_case, run_id)
    assert not calls
    assert value["status"] == "WAITING_REVIEW", value
    assert "AGENT_MODEL_CALL_LIMIT" in str(value["report"]["limitations"])


def test_input_archived_during_model_io_never_publishes_report(
    client, writing_case, settings, request
):
    saved, content = prepare(client, writing_case)
    model = request.getfixturevalue("model_service")
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost=Decimal("0"), pricing_version="narrative-test:1"
    )
    run_id = start(client, writing_case, payload(writing_case, saved, content))
    frozen = progress(client, writing_case, run_id)["work"]

    def respond(_req):
        with client.app.state.session_factory.begin() as db:
            db.get(Episode, UUID(writing_case["episode"]["id"])).status = "ARCHIVED"
        return httpx.Response(200, json=final(result=assessment(frozen)))

    model(respond)
    execute(client, settings, run_id)
    value = progress(client, writing_case, run_id)
    assert value["status"] == "FAILED", value
    assert value["report"] is None
    assert value["human_review"] is None


def test_overdue_foreshadow_warning_retains_exact_plan_and_requires_human_resolution(
    client, writing_case, settings, request
):
    case = copy.deepcopy(writing_case)
    other = client.post(
        f"/api/v1/projects/{case['episode']['project_id']}/episodes",
        json={"episode_no": 3, "title": "回收集", "target_duration_ms": 30000},
    )
    assert other.status_code == 201
    case["episode"] = other.json()
    saved, content = prepare(client, case)
    model = request.getfixturevalue("model_service")
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost=Decimal("0"), pricing_version="narrative-test:1"
    )
    run_id = start(client, case, payload(case, saved, content))
    work = progress(client, case, run_id)["work"]
    result = assessment(work)
    check = next(item for item in result["checks"] if item["category"] == "FORESHADOWING")
    plan = next(item for item in work["evidence"] if item["kind"] == "PLAN")
    source = next(item for item in work["evidence"] if item["kind"] == "SCENE")
    check.update(
        status="ISSUE",
        summary="红印伏笔已到预定回收集，但送审场景未见回收证据。",
        issues=[
            {
                "severity": "WARNING",
                "summary": "提示红印伏笔未按计划回收；不能把预定回收当作已发生事实。",
                "location": source["location"],
                "evidence_ids": [source["id"], plan["id"]],
            }
        ],
    )
    model(lambda _req: httpx.Response(200, json=final(result=result)))
    execute(client, settings, run_id)
    value = progress(client, case, run_id)
    assert value["status"] == "WAITING_REVIEW", value
    assert value["report"]["assessment"]["checks"] == result["checks"]
    body = human_decision(value)
    body["narrative_content"]["warning_resolutions"] = []
    response = client.post(
        f"/api/v1/human-reviews/{value['human_review']['id']}/decisions", json=body
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("stop", [False, True])
def test_native_question_survives_restart_and_answer_does_not_approve_content(
    client, writing_case, settings, model_service, stop
):
    saved, content = prepare(client, writing_case)
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost=Decimal("0"), pricing_version="narrative-test:1"
    )
    run_id = start(client, writing_case, payload(writing_case, saved, content))
    work = progress(client, writing_case, run_id)["work"]
    calls = []

    def respond(req):
        data = json.loads(req.content)
        calls.append(data)
        answered = any("human_decision" in row.get("content", "") for row in data["messages"])
        return httpx.Response(
            200, json=final(result=assessment(work)) if answered else final("HUMAN_REVIEW")
        )

    model_service(respond)
    execute(client, settings, run_id)
    waiting = progress(client, writing_case, run_id)
    question = waiting["question"]
    assert question and waiting["report"] is None and len(calls) == 1
    assert waiting["status"] == "RUNNING"
    with TestClient(create_app(settings)) as restarted:
        restarted.headers.update(client.headers)
        execute(restarted, settings, run_id)
        assert progress(restarted, writing_case, run_id)["question"] == question
        assert len(calls) == 1
        with restarted.app.state.session_factory() as db:
            assert (
                db.get(WorkflowRun, UUID(run_id)).pending_review["destination"] == "HUMAN_QUESTION"
            )
        response = restarted.post(
            f"/api/v1/runs/{run_id}/continuity/{question['loop_id']}/answers",
            json={
                "question_id": question["question_id"],
                "input_sha256": question["input_sha256"],
                "decision": "STOP" if stop else "ANSWER",
                **({} if stop else {"answer": "补充"}),
            },
        )
        assert response.status_code == 202, response.text
        execute(restarted, settings, run_id)
        value = progress(restarted, writing_case, run_id)
        if stop:
            assert value["status"] == "CANCELLED", value
            assert value["report"] is None and len(calls) == 1
        else:
            assert value["status"] == "WAITING_REVIEW", value
            assert value["human_review"]["decision"] is None
            assert value["question"] is None and len(calls) == 2
            execute(restarted, settings, run_id)
            assert len(calls) == 2


def reject_and_finish(client, case, run_id):
    value = progress(client, case, run_id)
    human_id = value["human_review"]["id"]
    response = client.post(
        f"/api/v1/human-reviews/{human_id}/decisions", json=human_decision(value, approve=False)
    )
    assert response.status_code == 200, response.text
    result = client.post(
        f"/api/v1/human-reviews/{human_id}/resume",
        json={
            "decision_id": response.json()["id"],
            "request_sha256": value["human_review"]["request_sha256"],
        },
    )
    assert result.status_code == 200, result.text
    return value


def test_terminal_parent_without_actual_resume_cannot_authorize_rereview(
    client, writing_case, settings
):
    saved, content = prepare(client, writing_case)
    run_id = start(client, writing_case, payload(writing_case, saved, content))
    execute(client, settings, run_id)
    value = progress(client, writing_case, run_id)
    response = client.post(
        f"/api/v1/human-reviews/{value['human_review']['id']}/decisions",
        json=human_decision(value, approve=False),
    )
    assert response.status_code == 200, response.text
    # A terminal status alone is not a receipt for an authenticated human handoff.
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, UUID(run_id)).status = "REJECTED"
    rejected = client.post(
        endpoint(writing_case),
        json=payload(writing_case, saved, content, previous_report_id=value["report"]["report_id"]),
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "AGENT_NOT_ACTIVE"


def test_rereview_uses_original_deadline_and_cannot_reset_root_policy(
    client, writing_case, settings, monkeypatch
):
    saved, content = prepare(client, writing_case)
    run_id = start(client, writing_case, payload(writing_case, saved, content))
    execute(client, settings, run_id)
    value = reject_and_finish(client, writing_case, run_id)
    body = payload(writing_case, saved, content, previous_report_id=value["report"]["report_id"])
    changed = client.post(
        endpoint(writing_case), json=body | {"agent_limits": {"max_model_calls": 1}}
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["error"]["code"] == "AGENT_LIMITS_CONFLICT"
    with client.app.state.session_factory() as db:
        from arclight.application.model_gateway import utc

        deadline = utc(db.get(AgentRunLimit, UUID(run_id)).deadline_at)
    monkeypatch.setattr(agent_limits, "utcnow", lambda: deadline)
    expired = client.post(endpoint(writing_case), json=body)
    assert expired.status_code == 409, expired.text
    assert expired.json()["error"]["code"] == "AGENT_DEADLINE_EXCEEDED"


def test_rereview_cannot_spend_more_model_calls_than_shared_root(
    client, writing_case, settings, model_service
):
    saved, content = prepare(client, writing_case)
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost=Decimal("0"), pricing_version="narrative-test:1"
    )
    limits = {"max_model_calls": 1}
    run_id = start(client, writing_case, payload(writing_case, saved, content, agent_limits=limits))
    work = progress(client, writing_case, run_id)["work"]
    calls = []
    model_service(
        lambda req: calls.append(req) or httpx.Response(200, json=final(result=assessment(work)))
    )
    execute(client, settings, run_id)
    value = reject_and_finish(client, writing_case, run_id)
    child = start(
        client,
        writing_case,
        payload(
            writing_case,
            saved,
            content,
            agent_limits=limits,
            previous_report_id=value["report"]["report_id"],
        ),
    )
    execute(client, settings, child)
    second = progress(client, writing_case, child)
    assert len(calls) == 1
    assert second["status"] == "WAITING_REVIEW", second
    assert "AGENT_MODEL_CALL_LIMIT" in str(second["report"]["limitations"])


def test_video_citation_accepts_only_exact_obtained_sample(client, writing_case):
    from arclight.application.narrative_review_evidence import evidence

    saved, content = prepare(client, writing_case)
    run_id = start(client, writing_case, payload(writing_case, saved, content))
    with client.app.state.session_factory() as db:
        work = read_work(db, UUID(writing_case["episode"]["project_id"]), UUID(run_id))
    scene = next(item for item in work.evidence if item.kind == "SCENE")
    location = scene.location.model_copy(update={"shot_id": uuid4(), "candidate_id": uuid4()})
    video = evidence(
        work.project_id, "VIDEO", location, {"report": {"samples": [{"time_ms": 1450}]}}
    )
    work = work.model_copy(update={"evidence": [*work.evidence, video]})
    result = assessment(work.model_dump(mode="json"), blocker=True)
    issue = result["checks"][0]["issues"][0]
    issue.update(
        location=location.model_copy(update={"time_ms": 1450}).model_dump(mode="json"),
        evidence_ids=[str(video.id)],
    )
    validate_assessment(work, NarrativeAssessment.model_validate(result))
    issue["location"]["time_ms"] = 1451
    with pytest.raises(DomainError, match="实际取得"):
        validate_assessment(work, NarrativeAssessment.model_validate(result))


def test_relevant_plan_sorts_adjacent_milestones_and_keeps_overdue_inactive_thread(planned_outline):
    from arclight.application.narrative_review_evidence import plan_excerpt
    from arclight.domain.series_planning import SeriesOutlineContent

    _, characters, source = planned_outline
    value = copy.deepcopy(source)
    arc = value["series_plan"]["character_arcs"][0]
    arc["milestones"].reverse()
    value["series_plan"]["episode_progressions"][2]["thread_beats"] = [
        {"thread_id": "letter", "progression": "继续交信"}
    ]
    value["series_plan"]["foreshadowing"].append(
        {
            "id": "trust-late",
            "thread_id": "trust",
            "setup_episode_no": 1,
            "setup": "合作暗号",
            "expected_payoff_episode_no": 2,
            "planned_payoff": "解释暗号",
        }
    )
    plan = SeriesOutlineContent.model_validate(value)
    for number, expected in ((3, [2, 3]), (1, [1, 2])):
        selected = plan_excerpt(plan, {number}, {UUID(characters[0]["id"])})
        assert [
            item["episode_no"] for item in selected["character_arcs"][0]["milestones"]
        ] == expected
    assert selected["kind"] == "PLANNED"
    overdue = plan_excerpt(plan, {3}, {UUID(characters[0]["id"])})
    assert next(item for item in overdue["foreshadowing"] if item["id"] == "trust-late")[
        "payoff_due"
    ]
    assert {item["id"] for item in overdue["threads"]} == {"letter", "trust"}
    assert [
        item["episode_no"] for item in plan.series_plan.character_arcs[0].model_dump()["milestones"]
    ] == [3, 2, 1]


def test_adjacent_conflict_has_both_sources_and_stale_second_episode_blocks_native_answer(
    client, writing_case, settings, model_service
):
    saved, content = prepare(client, writing_case)
    other = client.post(
        f"/api/v1/projects/{writing_case['episode']['project_id']}/episodes",
        json={"episode_no": 2, "title": "矛盾后集", "target_duration_ms": 30000},
    )
    assert other.status_code == 201, other.text
    case = copy.deepcopy(writing_case)
    case["episode"] = other.json()
    later_content = screenplay(case, capture(client, case))
    later_content["scenes"][0]["dialogue"][0]["text"] = "那封信早已烧掉，不能再交给你。"
    later = save(client, case, later_content)
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost=Decimal("0"), pricing_version="narrative-test:1"
    )
    body = payload(
        writing_case,
        saved,
        content,
        scope="ADJACENT_EPISODES",
        targets=[{"script_version_id": saved["id"]}, {"script_version_id": later["id"]}],
    )
    run_id = start(client, writing_case, body)
    work = progress(client, writing_case, run_id)["work"]
    scenes = [item for item in work["evidence"] if item["kind"] == "SCENE"]
    result = assessment(work)
    check = next(item for item in result["checks"] if item["category"] == "CAUSALITY")
    check.update(
        status="ISSUE",
        summary="后集信件状态与前集行动不一致，需核对因果。",
        issues=[
            {
                "severity": "WARNING",
                "summary": "两集关于红信去向的叙述需人工核实。",
                "location": scenes[1]["location"]
                | {"line_id": later_content["scenes"][0]["dialogue"][0]["line_id"]},
                "evidence_ids": [item["id"] for item in scenes],
            }
        ],
    )
    calls = []

    def respond(req):
        calls.append(req)
        assert saved["id"] in req.content.decode() and later["id"] in req.content.decode()
        return httpx.Response(200, json=final(result=result))

    model_service(respond)
    execute(client, settings, run_id)
    value = progress(client, writing_case, run_id)
    assert value["report"]["assessment"]["checks"] == result["checks"]
    reject_and_finish(client, writing_case, run_id)
    child = start(
        client,
        writing_case,
        body | {"creation_key": str(uuid4()), "previous_report_id": value["report"]["report_id"]},
    )
    model_service(lambda req: calls.append(req) or httpx.Response(200, json=final("HUMAN_REVIEW")))
    execute(client, settings, child)
    question = progress(client, writing_case, child)["question"]
    assert question and len(calls) == 2
    with client.app.state.session_factory.begin() as db:
        db.get(Episode, UUID(other.json()["id"])).status = "ARCHIVED"
    response = client.post(
        f"/api/v1/runs/{child}/continuity/{question['loop_id']}/answers",
        json={
            "question_id": question["question_id"],
            "input_sha256": question["input_sha256"],
            "decision": "ANSWER",
            "answer": "无新增证据",
        },
    )
    assert response.status_code == 409, response.text
    assert len(calls) == 2
