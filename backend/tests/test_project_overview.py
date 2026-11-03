from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from test_api_workflow import advance, review, start
from test_collaboration import register
from test_story_analysis import execute as execute_analysis
from test_story_analysis import start as start_analysis
from test_story_outline import confirm, generate, save

from arclight.persistence.models import (
    AuditEvent,
    Episode,
    Project,
    ProjectMember,
    Script,
    Storyboard,
    StoryOutline,
    WorkflowRun,
)


def overview(client, episode, query=""):
    result = client.get(f"/api/v1/projects/{episode['project_id']}/overview{query}")
    assert result.status_code == 200, result.text
    return result.json()


def test_empty_overview_reports_coverage_without_invented_zeros(client, episode):
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).budget_limit = Decimal("123.45")
    result = overview(client, episode)
    assert result["stage_counts"] == {"NOT_STARTED": 1}
    assert result["task_total"] == result["pending_reviews"]["total"] == 0
    assert result["cost"]["status"] == "AVAILABLE"
    assert result["cost"]["budget_limit"] == "123.45"
    assert result["cost"]["actual_cost"] is None
    assert result["cost"]["estimated_cost"] is None
    assert result["cost"]["entry_count"] == 0
    assert result["cost"]["currency"] == "USD"
    assert result["deliveries"] == {"status": "UNAVAILABLE", "version_count": None}
    assert result["recent_tasks"] == []
    assert result["generated_at"].endswith("Z")


def test_generated_reviews_count_once_and_follow_task_transitions(client, episode, settings):
    run_id = start(client, episode).json()["run_id"]
    assert overview(client, episode)["task_counts"] == {"QUEUED": 1}
    state = advance(client, settings, run_id)
    result = overview(client, episode)
    assert result["stage_counts"] == {"SCRIPT_REVIEW": 1}
    assert result["pending_reviews"] == {
        "workflows": 1,
        "manual_scripts": 0,
        "manual_storyboards": 0,
        "outline_confirmations": 0,
        "total": 1,
    }
    assert review(client, state).status_code == 202
    state = advance(client, settings, run_id)
    assert overview(client, episode)["stage_counts"] == {"STORYBOARD_REVIEW": 1}
    assert review(client, state).status_code == 202
    advance(client, settings, run_id)
    result = overview(client, episode)
    assert result["stage_counts"] == {"STORYBOARD_APPROVED": 1}
    assert result["pending_reviews"]["total"] == 0
    assert result["task_counts"] == {"SUCCEEDED": 1}
    assert result["deliveries"]["version_count"] is None


def test_latest_script_and_exact_storyboard_source_determine_stage(client, episode):
    eid = UUID(episode["id"])
    with client.app.state.session_factory.begin() as db:
        old = Script(episode_id=eid, version_no=1, status="APPROVED", content_json={})
        current = Script(episode_id=eid, version_no=2, status="APPROVED", content_json={})
        db.add_all([old, current])
        db.flush()
        current_id = current.id
        db.add(
            Storyboard(
                episode_id=eid,
                script_version_id=old.id,
                version_no=1,
                status="APPROVED",
                content_json={},
            )
        )
    assert overview(client, episode)["stage_counts"] == {"SCRIPT_APPROVED": 1}
    for status, stage in [
        ("READY_FOR_REVIEW", "STORYBOARD_REVIEW"),
        ("REJECTED", "STORYBOARD_REVISION"),
        ("APPROVED", "STORYBOARD_APPROVED"),
    ]:
        with client.app.state.session_factory.begin() as db:
            board = db.scalar(
                select(Storyboard).where(Storyboard.episode_id == eid, Storyboard.version_no == 2)
            )
            if board is None:
                db.add(
                    Storyboard(
                        episode_id=eid,
                        script_version_id=current_id,
                        version_no=2,
                        status=status,
                        content_json={},
                    )
                )
            else:
                board.status = status
        assert overview(client, episode)["stage_counts"] == {stage: 1}
    with client.app.state.session_factory.begin() as db:
        db.add(Script(episode_id=eid, version_no=3, status="READY_FOR_REVIEW", content_json={}))
    result = overview(client, episode)
    assert result["stage_counts"] == {"SCRIPT_REVIEW": 1}
    assert result["pending_reviews"]["manual_scripts"] == 1
    with client.app.state.session_factory.begin() as db:
        db.scalar(
            select(Script).where(Script.episode_id == eid, Script.version_no == 3)
        ).status = "REJECTED"
    assert overview(client, episode)["stage_counts"] == {"SCRIPT_REVISION": 1}


def test_analysis_and_latest_outline_stages_are_scoped_to_origin_episode(client, episode, settings):
    other = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "Another episode"},
    )
    assert other.status_code == 201
    accepted = start_analysis(client, episode)
    assert accepted.status_code == 202
    execute_analysis(client, settings, accepted.json()["run_id"])
    assert overview(client, episode)["stage_counts"] == {"ANALYSIS_READY": 1, "NOT_STARTED": 1}
    outline = generate(client, episode, settings)
    assert overview(client, episode)["stage_counts"] == {"OUTLINE_DRAFT": 1, "NOT_STARTED": 1}
    assert confirm(client, episode, outline).status_code == 200
    result = overview(client, episode)
    assert result["stage_counts"] == {"OUTLINE_APPROVED": 1, "NOT_STARTED": 1}
    assert result["pending_reviews"]["total"] == 0
    assert save(client, episode, outline).status_code == 201
    result = overview(client, episode)
    assert result["stage_counts"] == {"OUTLINE_DRAFT": 1, "NOT_STARTED": 1}
    assert result["pending_reviews"]["outline_confirmations"] == 1
    assert result["episodes"][1]["id"] == other.json()["id"]
    assert result["episodes"][1]["stage"] == "NOT_STARTED"


def test_full_project_counts_and_bounded_rows_are_read_only(client, episode):
    project_id = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        db.add_all(
            [
                Episode(
                    project_id=project_id,
                    episode_no=n,
                    title=f"Episode {n}",
                    status="ARCHIVED" if n == 105 else "DRAFT",
                )
                for n in range(2, 106)
            ]
        )
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(client.app.state.engine, "before_cursor_execute", capture)
    try:
        result = overview(client, episode, "?offset=100&limit=4")
    finally:
        event.remove(client.app.state.engine, "before_cursor_execute", capture)
    assert result["episode_count"] == 105
    assert result["active_episode_count"] == 104
    assert result["archived_episode_count"] == 1
    assert result["stage_counts"] == {"NOT_STARTED": 104}
    assert [row["episode_no"] for row in result["episodes"]] == [101, 102, 103, 104]
    assert len(statements) < 15
    assert not any(
        sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for sql in statements
    )
    assert not any("content_json" in sql or "input_json" in sql for sql in statements)
    assert overview(client, episode, "?offset=104")["episodes"][0]["stage"] == "ARCHIVED"
    assert overview(client, episode, "?offset=999")["episodes"] == []


def test_pending_manual_versions_outlines_and_archived_scope(client, episode):
    eid = UUID(episode["id"])
    project_id = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        script = Script(episode_id=eid, version_no=1, status="READY_FOR_REVIEW", content_json={})
        db.add(script)
        db.flush()
        db.add(
            Storyboard(
                episode_id=eid,
                script_version_id=script.id,
                version_no=1,
                status="READY_FOR_REVIEW",
                content_json={},
            )
        )
        db.add(
            StoryOutline(
                project_id=project_id,
                episode_id=eid,
                version_no=1,
                content_json={},
                source_snapshot={},
                created_by="service-owner",
            )
        )
    result = overview(client, episode)
    assert result["pending_reviews"]["total"] == 3
    assert result["pending_reviews"]["outline_confirmations"] == 1
    with client.app.state.session_factory.begin() as db:
        db.get(Episode, eid).status = "ARCHIVED"
    result = overview(client, episode)
    assert result["pending_reviews"]["total"] == 0
    assert result["active_episode_count"] == 0


def test_tasks_are_bounded_ordered_scoped_and_sanitized(client, episode):
    with client.app.state.session_factory.begin() as db:
        for index, status in enumerate(
            ["FAILED", "CANCELLED", "REJECTED", "SUCCEEDED", "RUNNING", "QUEUED"] * 2
        ):
            db.add(
                WorkflowRun(
                    episode_id=UUID(episode["id"]),
                    status=status,
                    trace_id="private-trace",
                    idempotency_key=str(index),
                    request_fingerprint="private-fingerprint",
                    input_json={"story": "private-material", "key": "secret"},
                )
            )
        other = Project(name="Other", owner_id="service-owner")
        db.add(other)
        db.flush()
        db.add(Episode(project_id=other.id, episode_no=1, title="Private episode"))
    result = overview(client, episode)
    assert result["task_total"] == 12
    assert result["task_counts"] == dict.fromkeys(
        ["FAILED", "CANCELLED", "REJECTED", "SUCCEEDED", "RUNNING", "QUEUED"], 2
    )
    assert len(result["recent_tasks"]) == 10
    assert [row["created_at"] for row in result["recent_tasks"]] == sorted(
        [row["created_at"] for row in result["recent_tasks"]], reverse=True
    )
    assert all(row["episode_id"] == episode["id"] for row in result["recent_tasks"])
    assert not any(
        value in str(result) for value in ("private-", "secret", "input_json", "trace_id")
    )


@pytest.mark.parametrize("query", ["?limit=0", "?limit=101", "?offset=-1"])
def test_pagination_validation(client, episode, query):
    assert (
        client.get(f"/api/v1/projects/{episode['project_id']}/overview{query}").status_code == 422
    )


def test_permissions_members_archive_and_deletion(client):
    owner = register(client)
    project = client.post("/api/v1/projects", json={"name": "Owner project"}).json()
    url = f"/api/v1/projects/{project['id']}/overview"
    viewer = register(client, "viewer@example.com")
    assert client.get(url).status_code == 404
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(project["id"]), user_id=UUID(viewer["user"]["id"]), role="viewer"
            )
        )
        db.get(Project, UUID(project["id"])).archived = True
    assert client.get(url).json()["archived"] is True
    with client.app.state.session_factory.begin() as db:
        membership = db.scalar(select(ProjectMember))
        membership.role = "editor"
    assert client.get(url).status_code == 200
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(project["id"])).deleted_at = datetime.now(UTC)
    assert client.get(url).status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert client.get(url).status_code == 404
    assert client.get(f"/api/v1/projects/{uuid4()}/overview").status_code == 404
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent)) == 0
