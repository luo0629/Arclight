"""Owner selection finishes production reviews without authorizing other candidates."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import select, text
from test_agent_model import model_service as model_service
from test_asset_selection import choose, current
from test_concurrency import require_postgres
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_rules import rules_storage as rules_storage
from test_continuity_selection import approve, target_rows
from test_continuity_workflow import make_job, work_for
from test_continuity_workflow_recovery import run_work
from test_generation_candidates import candidates

from arclight.application.commands import require_project
from arclight.application.continuity_neighbors import selected_neighbor
from arclight.application.continuity_workflow import queue_pending_reviews
from arclight.persistence.models import AssetVersion, VisualAsset, WorkflowEvent, WorkflowRun


def test_postgres_selection_locks_run_before_project_and_finishes_once(
    client, episode, settings, reviewer_case, model_service
):
    require_postgres(settings)
    _, identity, case = ready_candidate(client, episode, settings, reviewer_case, model_service)
    asset, _ = target_rows(client, case)
    version = approve(client, case)
    asset = current(client, asset)
    factory = client.app.state.session_factory
    with ThreadPoolExecutor(max_workers=2) as pool:
        with factory.begin() as db:
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            db.get(WorkflowRun, identity, with_for_update=True)
            results = [pool.submit(choose, client, asset, version) for _ in range(2)]
            for future in results:
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.2)
            # Selection must wait on the run without holding the project gate.
            require_project(db, UUID(episode["project_id"]), "service-owner", write=True)
        assert sorted(future.result(timeout=30).status_code for future in results) == [200, 409]
    with factory() as db:
        assert db.get(WorkflowRun, identity).status == "SUCCEEDED"
        assert (
            len(
                list(
                    db.scalars(
                        select(WorkflowEvent).where(
                            WorkflowEvent.run_id == identity,
                            WorkflowEvent.event_type == "workflow.finished",
                        )
                    )
                )
            )
            == 1
        )


def ready_candidate(client, episode, settings, case, model_service):
    _, errors, deliveries = install_reviewer_model(model_service, dynamic=False)
    job, _ = make_job(client, episode, case)
    candidate = candidates(client, episode["project_id"], job)[0]
    assert queue_pending_reviews(client.app.state.session_factory, settings) == 1
    work = work_for(client, episode["project_id"], candidate)
    identity = run_work(client, settings, work)
    assert not errors and deliveries[0]["conclusion"] == "PASS"
    return (
        candidate,
        identity,
        {"project_id": episode["project_id"], "target": work["work"]["target"]},
    )


def test_only_owner_selection_completes_once_and_exposes_exact_neighbor(
    client,
    episode,
    settings,
    reviewer_case,
    model_service,
):
    candidate, identity, case = ready_candidate(
        client, episode, settings, reviewer_case, model_service
    )
    factory = client.app.state.session_factory
    project_id, shot_id = UUID(episode["project_id"]), UUID(case["target"]["shot_id"])
    with factory() as db:
        original = deepcopy(db.get(WorkflowRun, identity).input_json)
        assert selected_neighbor(db, project_id, shot_id) is None
    asset, version = target_rows(client, case)
    assert choose(client, asset, version).status_code == 409
    version = approve(client, case)
    assert work_for(client, episode["project_id"], candidate)["status"] == "WAITING_REVIEW"
    asset = current(client, asset)
    chosen = choose(client, asset, version)
    assert chosen.status_code == 200, chosen.text
    assert choose(client, asset, version).status_code == 409
    assert work_for(client, episode["project_id"], candidate)["status"] == "SUCCEEDED"
    with factory() as db:
        run = db.get(WorkflowRun, identity)
        assert run.input_json == original and run.pending_review is None
        assert run.agent_state_json["status"] == "SUCCEEDED"
        events = list(
            db.scalars(
                select(WorkflowEvent).where(
                    WorkflowEvent.run_id == identity,
                    WorkflowEvent.event_type == "workflow.finished",
                )
            )
        )
        assert len(events) == 1
        assert events[0].payload_json["asset_version_id"] == version["id"]
        assert selected_neighbor(db, project_id, shot_id) == {
            "asset_id": asset["id"],
            "asset_version_id": version["id"],
        }
        assert selected_neighbor(db, uuid4(), shot_id) is None
        assert selected_neighbor(db, project_id, uuid4()) is None


@pytest.mark.parametrize("problem", ["stale", "archived", "unselected", "ambiguous"])
def test_neighbor_resolution_never_guesses_invalid_or_ambiguous_selections(
    client,
    episode,
    settings,
    reviewer_case,
    model_service,
    problem,
):
    _, _, case = ready_candidate(client, episode, settings, reviewer_case, model_service)
    asset, _ = target_rows(client, case)
    version = approve(client, case)
    assert choose(client, current(client, asset), version).status_code == 200
    if problem == "ambiguous":
        _, _, other = ready_candidate(client, episode, settings, reviewer_case, model_service)
        other_asset, _ = target_rows(client, other)
        other_version = approve(client, other)
        assert choose(client, current(client, other_asset), other_version).status_code == 200
    else:
        # Fault injection only in this isolated guard test, never in the slice.
        with client.app.state.session_factory.begin() as db:
            if problem == "stale":
                db.get(AssetVersion, UUID(version["id"])).stale = True
            else:
                row = db.get(VisualAsset, UUID(asset["id"]))
                if problem == "archived":
                    row.status = "ARCHIVED"
                else:
                    row.current_version_id = None
    with client.app.state.session_factory() as db:
        assert (
            selected_neighbor(db, UUID(episode["project_id"]), UUID(case["target"]["shot_id"]))
            is None
        )
