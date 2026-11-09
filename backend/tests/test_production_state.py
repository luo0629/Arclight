"""FLOW-01: database-authoritative reads, existing flows and producer boundaries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from test_api_workflow import advance, review, start
from test_asset_selection import approved
from test_assets import create
from test_collaboration import create_episode, create_project, register
from test_content_duration import approve, save_script, script_content
from test_generation_candidates import generate, ingest
from test_generation_jobs import capture_ok, payload

from arclight.application import production_state
from arclight.application.dependencies import lock_dependency_graph
from arclight.application.generation_state import transition
from arclight.domain.generation import GenerationStatus
from arclight.main import create_app
from arclight.persistence.models import (
    AssetVersion,
    Episode,
    GenerationCandidate,
    Project,
    ProjectMember,
    Script,
    Shot,
    WorkflowRun,
)


def path(episode):
    return f"/api/v1/episodes/{episode['id']}/production-state"


def read(client, episode):
    result = client.get(path(episode))
    assert result.status_code == 200, result.text
    return result.json()


def finish_text(client, episode, settings):
    run_id = start(client, episode).json()["run_id"]
    for _ in range(2):
        pending = advance(client, settings, run_id)
        assert review(client, pending).status_code == 202
    completed = advance(client, settings, run_id)
    assert completed["status"] == "SUCCEEDED"
    return completed


def test_existing_text_reviews_resume_and_complete_without_inventing_media(
    client, episode, settings
):
    assert read(client, episode)["phase"] == "DRAFT"
    run_id = start(client, episode).json()["run_id"]
    queued = read(client, episode)
    assert queued["phase"] == "SCRIPTING"
    assert queued["task_counts"] == {"QUEUED": 1}
    pending = advance(client, settings, run_id)
    state = read(client, episode)
    assert state["phase"] == "WAITING_SCRIPT_REVIEW"
    assert state["script_version_id"] == pending["pending_review"]["version_id"]
    with TestClient(create_app(settings)) as restarted:
        restarted.headers["Authorization"] = "Bearer test-token"
        assert read(restarted, episode) == state
        assert review(restarted, pending).status_code == 202
    assert read(client, episode)["phase"] == "STORYBOARDING"
    board_pending = advance(client, settings, run_id)
    assert read(client, episode)["phase"] == "WAITING_STORYBOARD_REVIEW"
    assert review(client, board_pending).status_code == 202
    completed = advance(client, settings, run_id)
    state = read(client, episode)
    assert state["phase"] == "GENERATING_MEDIA"
    assert state["approved_shot_count"] == 0
    assert state["shot_count"] > 0
    assert state["shot_counts"] == {"READY": state["shot_count"]}
    assert state["task_counts"] == {"SUCCEEDED": 1}
    assert state["unavailable_producers"] == ["MEDIA_QA", "RENDER", "DELIVERY_QA"]
    # Duplicate completion and polling cannot create a second execution or approval.
    advance(client, settings, run_id)
    assert read(client, episode) == state
    with TestClient(create_app(settings)) as restarted:
        restarted.headers["Authorization"] = "Bearer test-token"
        assert read(restarted, episode) == state
    overview = client.get(f"/api/v1/projects/{episode['project_id']}/overview").json()
    assert overview["episodes"][0]["stage"] == "STORYBOARD_APPROVED"
    assert overview["episodes"][0]["production_state_url"] == path(episode)
    assert completed["status"] == "SUCCEEDED"


def test_reads_do_not_write_audit_status_or_content_and_hide_private_inputs(
    client, episode, settings
):
    finish_text(client, episode, settings)
    statements = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql.lstrip().upper())

    event.listen(client.app.state.engine, "before_cursor_execute", capture)
    try:
        first = read(client, episode)
        assert read(client, episode) == first
    finally:
        event.remove(client.app.state.engine, "before_cursor_execute", capture)
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
    assert not any(
        key in str(first)
        for key in ("input_json", "content_json", "object_key", "雨夜，主角", "provider_request_id")
    )
    assert len(first["evidence_sha256"]) == 64


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_production_state_is_read_only_and_episode_cannot_set_arbitrary_status(
    client, episode, method
):
    assert client.request(method, path(episode), json={"phase": "COMPLETED"}).status_code == 405
    rejected = client.patch(f"/api/v1/episodes/{episode['id']}", json={"status": "COMPLETED"})
    assert rejected.status_code == 422
    assert read(client, episode)["phase"] == "DRAFT"


@pytest.mark.parametrize("terminal", ["FAILED", "CANCELLED"])
def test_actual_text_terminal_states_do_not_invent_downstream_artifacts(client, episode, terminal):
    run_id = start(client, episode).json()["run_id"]
    if terminal == "CANCELLED":
        response = client.post(f"/api/v1/runs/{run_id}/cancel")
        assert response.status_code == 200, response.text
    else:
        with client.app.state.session_factory.begin() as db:
            db.get(WorkflowRun, UUID(run_id)).status = terminal
    state = read(client, episode)
    assert state["phase"] == terminal
    assert state["blocked_phase"] == "SCRIPTING"
    assert state["script_version_id"] is None and state["shot_count"] == 0


@pytest.mark.parametrize("stage", ["script", "storyboard"])
def test_cancelling_a_pending_review_keeps_history_but_reports_terminal_execution(
    client, episode, settings, stage
):
    run_id = start(client, episode).json()["run_id"]
    pending = advance(client, settings, run_id)
    if stage == "storyboard":
        assert review(client, pending).status_code == 202
        advance(client, settings, run_id)
    before = read(client, episode)
    expected = "WAITING_SCRIPT_REVIEW" if stage == "script" else "WAITING_STORYBOARD_REVIEW"
    assert before["phase"] == expected
    assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
    state = read(client, episode)
    assert state["phase"] == "CANCELLED" and state["blocked_phase"] == expected
    assert state["script_version_id"] == before["script_version_id"]
    assert state["storyboard_version_id"] == before["storyboard_version_id"]
    assert state["task_counts"] == {"CANCELLED": 1}
    assert "EXECUTION_CANCELLED" in state["issues"]


def test_latest_version_and_stale_registry_override_old_success(client, episode, settings):
    finish_text(client, episode, settings)
    before = read(client, episode)
    current = save_script(client, episode, expected=1)
    assert current.status_code == 201, current.text
    state = read(client, episode)
    assert state["phase"] == "WAITING_SCRIPT_REVIEW"
    assert state["script_version_id"] == current.json()["id"]
    assert state["evidence_sha256"] != before["evidence_sha256"]
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(current.json()["id"])).stale = True
    state = read(client, episode)
    assert "STALE_INPUT" in state["issues"] and state["approved_shot_count"] == 0


def test_old_script_director_failure_cannot_block_current_script(client, episode):
    first = save_script(client, episode).json()
    second = save_script(client, episode, expected=1).json()
    assert approve(client, episode, "scripts", second["id"]).status_code == 200
    with client.app.state.session_factory.begin() as db:
        db.add(
            WorkflowRun(
                episode_id=UUID(episode["id"]),
                workflow_type="storyboard",
                status="FAILED",
                trace_id="private-director-trace",
                idempotency_key="old-script-director",
                request_fingerprint="a" * 64,
                input_json={"script_version_id": first["id"]},
            )
        )
    state = read(client, episode)
    assert state["phase"] == "STORYBOARDING" and state["task_counts"] == {}
    with client.app.state.session_factory.begin() as db:
        db.add(
            WorkflowRun(
                episode_id=UUID(episode["id"]),
                workflow_type="storyboard",
                status="FAILED",
                trace_id="private-current-trace",
                idempotency_key="current-script-director",
                request_fingerprint="b" * 64,
                input_json={"script_version_id": second["id"]},
            )
        )
    state = read(client, episode)
    assert state["phase"] == "FAILED" and state["blocked_phase"] == "STORYBOARDING"
    assert state["task_counts"] == {"FAILED": 1}


@pytest.mark.parametrize("mode", ["STALE", "ARCHIVED"])
def test_existing_reference_asset_gate_blocks_production(client, episode, mode):
    asset = create(client, episode["project_id"], "CHARACTER")
    version = approved(client, asset)
    content = script_content()
    content["character_assets"] = [
        {
            "character": content["characters"][0],
            "asset": {"asset_id": asset["id"], "asset_version_id": version["id"]},
        }
    ]
    script = save_script(client, episode, content).json()
    assert approve(client, episode, "scripts", script["id"]).status_code == 200
    with client.app.state.session_factory.begin() as db:
        row = db.get(AssetVersion, UUID(version["id"]))
        if mode == "STALE":
            row.stale = True
        else:
            row.status = "ARCHIVED"
    state = read(client, episode)
    assert state["phase"] == "ASSET_PREPARATION"
    assert state["issues"] == ["STALE_INPUT" if mode == "STALE" else "ASSET_NOT_APPROVED"]


def test_legacy_missing_registry_and_invalid_content_block_instead_of_succeeding(client, episode):
    with client.app.state.session_factory.begin() as db:
        db.add(
            Script(episode_id=UUID(episode["id"]), version_no=1, status="APPROVED", content_json={})
        )
    state = read(client, episode)
    assert state["phase"] == "ASSET_PREPARATION"
    assert set(state["issues"]) == {"MISSING_VERSION_REGISTRY", "INVALID_CONTENT"}


def test_rejected_version_registry_cannot_be_overridden_by_text_approval(client, episode):
    script = save_script(client, episode).json()
    assert approve(client, episode, "scripts", script["id"]).status_code == 200
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, UUID(script["id"])).status = "REJECTED"
    state = read(client, episode)
    assert state["phase"] == "SCRIPTING"
    assert not next(
        condition
        for condition in state["conditions"]
        if condition["condition"] == "SCRIPT_APPROVED"
    )["satisfied"]


def test_generic_media_history_is_counted_without_truncation_or_forged_shot_binding(
    client, episode, settings, monkeypatch
):
    finish_text(client, episode, settings)
    initial = read(client, episode)
    shot_id = initial["shots"][0]["shot_id"]
    active = initial["shot_count"] + 1
    for status in ("FAILED", "CANCELLED", *(["CREATED"] * active)):
        body = payload(episode_id=episode["id"])
        body["spec"]["context"]["shot_id"] = shot_id
        job = capture_ok(client, episode["project_id"], body)
        if status != "CREATED":
            with client.app.state.session_factory.begin() as db:
                transition(db, UUID(job["id"]), GenerationStatus(status), 0)
    # The evidence cap is for detailed required shots, not all historical task rows.
    monkeypatch.setattr(production_state, "MAX_PRODUCTION_ITEMS", initial["shot_count"])
    state = read(client, episode)
    assert state["unbound_task_counts"] == {"FAILED": 1, "CANCELLED": 1, "CREATED": active}
    assert state["task_counts"] == state["unbound_task_counts"] | {"SUCCEEDED": 1}
    assert set(state["task_issues"]) == {"EXECUTION_FAILED", "EXECUTION_CANCELLED"}
    assert state["has_partial_failure"]
    assert state["phase"] == "GENERATING_MEDIA"
    assert set(state["shot_counts"]) == {"READY"}


def test_detailed_shot_limit_rejects_without_silently_losing_required_shots(
    client, episode, settings, monkeypatch
):
    finish_text(client, episode, settings)
    monkeypatch.setattr(production_state, "MAX_PRODUCTION_ITEMS", 1)
    result = client.get(path(episode))
    assert result.status_code == 413
    assert result.json()["error"]["code"] == "PRODUCTION_STATE_LIMIT"


def test_existing_candidate_import_proves_file_check_without_forging_continuity(client, episode):
    job, attempt = generate(client, episode["project_id"])
    assert ingest(client, attempt) == 1
    with client.app.state.session_factory() as db:
        candidate = db.scalar(
            select(GenerationCandidate).where(GenerationCandidate.job_id == UUID(job["id"]))
        )
        version = db.get(AssetVersion, candidate.asset_version_id)
        owner_episode = db.get(Episode, UUID(episode["id"]))
        # A controlled future shot-binding fixture over a real, immutable GEN-12 import.
        # There is currently no actual producer that binds this candidate to a shot.
        fixture = Shot(
            id=uuid4(),
            storyboard_id=uuid4(),
            shot_no=1,
            status="APPROVED",
            content_json={
                "shot_no": 1,
                "scene_no": 1,
                "duration_ms": 30000,
                "visual_description": "受控生产器合同夹具",
                "selected_visual_asset": {
                    "asset_id": str(version.asset_id),
                    "asset_version_id": str(version.id),
                },
            },
        )
        facts = production_state.shot_facts(db, owner_episode, fixture)
        assert facts.file_check is not None and facts.file_check.decision == "PASS"
        assert facts.file_check.review_id == candidate.id
        assert facts.continuity is None and facts.selection is None
        assert facts.media.status == "DRAFT"


def test_permissions_revocation_archived_and_deleted_project(client):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    viewer = register(client, "production-viewer@example.com")
    assert client.get(path(episode)).status_code == 404
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(project["id"]), user_id=UUID(viewer["user"]["id"]), role="viewer"
        )
        db.add(membership)
        db.flush()
        membership_id = membership.id
        db.get(Episode, UUID(episode["id"])).status = "ARCHIVED"
        db.get(Project, UUID(project["id"])).archived = True
    assert read(client, episode)["archived"] is True
    with client.app.state.session_factory.begin() as db:
        db.delete(db.get(ProjectMember, membership_id))
    assert client.get(path(episode)).status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert read(client, episode)["archived"] is True
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(project["id"])).deleted_at = datetime.now(UTC)
    assert client.get(path(episode)).status_code == 404
    assert client.get(f"/api/v1/episodes/{uuid4()}/production-state").status_code == 404


def test_snapshot_does_not_mix_before_and_after_a_concurrent_commit(
    client, episode, settings, monkeypatch
):
    finish_text(client, episode, settings)
    before = read(client, episode)
    attempted, committed = Event(), Event()
    original = production_state.shot_facts
    pending = []

    def write():
        attempted.set()
        with client.app.state.session_factory.begin() as db:
            lock_dependency_graph(db, UUID(episode["project_id"]))
            db.get(AssetVersion, UUID(before["script_version_id"])).stale = True
        committed.set()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def during_read(db, *args, **kwargs):
            if not pending:
                pending.append(pool.submit(write))
                assert attempted.wait(10)
                assert not committed.wait(0.3)
            return original(db, *args, **kwargs)

        monkeypatch.setattr(production_state, "shot_facts", during_read)
        assert read(client, episode) == before
        pending[0].result(timeout=10)
    monkeypatch.setattr(production_state, "shot_facts", original)
    after = read(client, episode)
    assert after["evidence_sha256"] != before["evidence_sha256"]
    assert "STALE_INPUT" in after["issues"] and after["approved_shot_count"] == 0
