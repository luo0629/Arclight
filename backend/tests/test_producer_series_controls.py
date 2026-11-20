"""Public scope/control contracts and explicit performance-director delegation."""

import copy
import importlib.util
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_agent_model import model_service as model_service  # noqa: F401
from test_producer_delegation import delegation_call, dispatched
from test_producer_series import begin, completed
from test_producer_series import series_case as series_case  # noqa: F401
from test_producer_series import series_transport as series_transport  # noqa: F401
from test_screenwriter_creation import run_once
from test_script_state_commit import capture, review, save, screenplay
from test_series_planning import planned_outline as planned_outline  # noqa: F401
from test_writing_context import writing_case as writing_case  # noqa: F401

from arclight.application.producer_series_delivery import control_series
from arclight.core.errors import DomainError
from arclight.domain.producer_series import SeriesControl, SeriesProducerScope
from arclight.persistence.models import Episode, StoryLedgerHead, WorkflowRun


@pytest.mark.parametrize("change", ["duplicate", "forward", "cycle"])
def test_series_schedule_is_explicit_ordered_dag_not_story_causality(change):
    first, second = str(uuid4()), str(uuid4())
    episodes = [{"episode_id": first}, {"episode_id": second, "requires_episode_ids": [first]}]
    if change == "duplicate":
        episodes[1]["episode_id"] = first
    elif change == "forward":
        episodes[0]["requires_episode_ids"] = [second]
    else:
        episodes[1]["requires_episode_ids"] = [second]
    with pytest.raises(ValidationError):
        SeriesProducerScope.model_validate(
            {
                "series_plan_version_id": str(uuid4()),
                "ledger_version_id": None,
                "expected_ledger_head_revision": 0,
                "episodes": episodes,
                "confirmation": "明确制作顺序",
            }
        )


def test_public_create_is_idempotent_and_pause_cannot_discard_pending_plan(
    client, settings, series_case
):
    url = f"/api/v1/projects/{series_case['episode']['project_id']}/producer-runs"
    payload = {"goal": "多集制作", "scope": series_case["scope"]}
    headers = {"Idempotency-Key": "public-series"}
    result = client.post(url, json=payload, headers=headers)
    assert result.status_code == 202, result.text
    run_id = result.json()["run_id"]
    assert client.post(url, json=payload, headers=headers).json() == result.json()
    progress = client.get(f"/api/v1/runs/{run_id}/producer")
    assert progress.status_code == 200, progress.text
    value = progress.json()
    assert len(value["series"]["scope"]["episodes"]) == 2
    assert not value["series"]["whole_series_complete"]
    with client.app.state.session_factory() as db:
        revision = db.get(WorkflowRun, UUID(run_id)).revision
    pause = client.post(
        f"/api/v1/runs/{run_id}/producer/series-controls",
        headers={"Idempotency-Key": "pause-pending"},
        json={"action": "PAUSE", "expected_revision": revision, "reason": "不能丢弃排队命令"},
    )
    assert pause.status_code == 409 and pause.json()["error"]["code"] == "PRODUCER_COMMAND_PENDING"
    assert "_text_service" not in progress.text and "api_key" not in progress.text


def test_cross_project_target_and_unknown_plan_are_rejected_before_root_creation(
    client, settings, series_case
):
    other_project = client.post("/api/v1/projects", json={"name": "不属于制作范围的项目"})
    assert other_project.status_code == 201
    other_episode = client.post(
        f"/api/v1/projects/{other_project.json()['id']}/episodes",
        json={"episode_no": 1, "title": "另一个项目", "target_duration_ms": 30000},
    )
    assert other_episode.status_code == 201
    changed = copy.deepcopy(series_case["scope"])
    changed["episodes"][1]["episode_id"] = other_episode.json()["id"]
    with pytest.raises(DomainError) as failure:
        begin(client, settings, series_case, scope=changed)
    assert failure.value.code == "NOT_FOUND"
    changed = copy.deepcopy(series_case["scope"])
    changed["series_plan_version_id"] = str(uuid4())
    with pytest.raises(DomainError) as failure:
        begin(client, settings, series_case, scope=changed)
    assert failure.value.code == "NARRATIVE_CONTEXT_INVALID"


@pytest.mark.parametrize("state", ["archived", "cancelled"])
def test_resume_never_bypasses_archive_or_cancellation(client, settings, series_case, state):
    run_id, actor = begin(client, settings, series_case)
    with client.app.state.session_factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        control_series(
            db,
            run_id,
            actor,
            SeriesControl(action="PAUSE", expected_revision=run.revision, reason="等待核对"),
            "pause",
        )
        if state == "archived":
            db.get(Episode, run.episode_id).status = "ARCHIVED"
        else:
            run.status = "CANCELLED"
    with client.app.state.session_factory.begin() as db, pytest.raises(DomainError) as error:
        run = db.get(WorkflowRun, run_id)
        control_series(
            db,
            run_id,
            actor,
            SeriesControl(action="RESUME", expected_revision=run.revision, reason="恢复仍须复验"),
            "resume",
        )
    assert error.value.code == ("EPISODE_ARCHIVED" if state == "archived" else "REVISION_CONFLICT")


def test_delegated_director_executes_performance_mode_on_exact_series_script(
    client, settings, series_case, series_transport, monkeypatch
):
    case = series_case
    script = save(client, case, screenplay(case, capture(client, case)))
    approved = review(client, case, script)
    assert approved.status_code == 200, approved.text
    with client.app.state.session_factory() as db:
        head = db.scalar(
            select(StoryLedgerHead).where(
                StoryLedgerHead.project_id == UUID(case["episode"]["project_id"])
            )
        )
        case["scope"].update(
            ledger_version_id=str(head.approved_version_id),
            expected_ledger_head_revision=head.revision,
        )
    tasks = [
        {
            "task_key": "direct",
            "depends_on": [],
            "reason": "将本集批准稿落实为人物动作与反应",
            "task": {"kind": "director", "script_version_id": script["id"]},
        }
    ]
    inputs = [{"task_key": "direct", "episode_id": case["episode"]["id"]}]
    run_id, _, reference, _ = completed(
        client, settings, case, series_transport, tasks=tasks, inputs=inputs
    )
    receipt = dispatched(client, settings, delegation_call(run_id, reference, task_key="direct"))
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "series_producer_performance_fixture", scripts / "e2e_screenwriter.py"
    )
    assert spec and spec.loader
    responder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(responder)
    series_transport(responder._respond)
    child_id = UUID(receipt["child_run_id"])
    result = run_once(client, settings, child_id)
    assert result["workflow_version"] == "director/4.0" and result["status"] == "RUNNING", result
    progress = client.get(f"/api/v1/runs/{child_id}/director").json()
    checks = [item for item in progress["operations"] if item["kind"] == "director_check"]
    assert checks and checks[-1]["status"] == "WAITING_HUMAN"
    with client.app.state.session_factory() as db:
        child = db.get(WorkflowRun, child_id)
        assert child.input_json["director"]["performance"] is True
        assert child.input_json["script_version_id"] == script["id"]
