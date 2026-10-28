from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import func, inspect, select
from test_collaboration import create_episode, create_project, register

from arclight.persistence.models import AuditEvent, Episode, Project, ProjectMember, WorkflowRun

PREFIX = "/api/v1"


def archive(client, project):
    response = client.patch(f"{PREFIX}/projects/{project['id']}", json={"archived": True})
    assert response.status_code == 200, response.text


def delete(client, project):
    return client.request(
        "DELETE", f"{PREFIX}/projects/{project['id']}", json={"confirm_name": project["name"]}
    )


def restore(client, project, deletion_id):
    return client.post(
        f"{PREFIX}/projects/{project['id']}/restore", json={"deletion_id": deletion_id}
    )


def test_delete_requires_archive_confirmation_and_owner(client):
    owner = register(client)
    project = create_project(client)
    assert delete(client, project).json()["error"]["code"] == "ARCHIVE_REQUIRED"
    archive(client, project)
    mismatch = client.request(
        "DELETE", f"{PREFIX}/projects/{project['id']}", json={"confirm_name": "wrong"}
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "CONFIRMATION_MISMATCH"
    member = register(client, "member@example.com")
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role="editor"
            )
        )
    assert delete(client, project).status_code == 403
    assert client.get(PREFIX + "/project-trash").json() == []
    register(client, "outsider@example.com")
    assert delete(client, project).status_code == 404
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert delete(client, project).status_code == 200
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert delete(client, project).status_code == 404
    assert client.get(f"{PREFIX}/projects/{project['id']}").status_code == 404


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "WAITING_REVIEW"])
def test_delete_protects_active_runs_in_every_episode(client, status):
    project = create_project(client)
    episode = create_episode(client, project)
    archive(client, project)
    with client.app.state.session_factory.begin() as db:
        db.add(
            WorkflowRun(
                episode_id=UUID(episode["id"]),
                workflow_type="episode",
                trace_id="deletion-test",
                status=status,
                idempotency_key="active",
                request_fingerprint="active",
                input_json={},
            )
        )
    response = delete(client, project)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ACTIVE_WORKFLOW"
    assert client.get(PREFIX + "/project-trash").json() == []


def test_logical_delete_is_idempotent_hidden_and_restorable_without_data_loss(client):
    project = create_project(client)
    episode = create_episode(client, project)
    archive(client, project)
    first = delete(client, project)
    assert first.status_code == 200, first.text
    receipt = first.json()
    assert receipt["can_restore"] is True
    from datetime import datetime

    assert datetime.fromisoformat(receipt["restore_until"]) - datetime.fromisoformat(
        receipt["deleted_at"]
    ) == timedelta(days=30)
    assert delete(client, project).json() == receipt
    assert client.get(PREFIX + "/projects").json() == []
    assert client.get(PREFIX + "/project-trash").json() == [receipt]
    assert client.get(PREFIX + "/project-trash?offset=1&limit=1").json() == []
    for path in [
        f"/projects/{project['id']}",
        f"/projects/{project['id']}/access",
        f"/projects/{project['id']}/episodes",
        f"/projects/{project['id']}/members",
        f"/projects/{project['id']}/audit-events",
        f"/episodes/{episode['id']}",
        f"/episodes/{episode['id']}/scripts",
        f"/episodes/{episode['id']}/storyboards",
        f"/episodes/{episode['id']}/canvas",
        f"/episodes/{episode['id']}/runs",
    ]:
        response = client.get(PREFIX + path)
        assert response.status_code == 404, (path, response.text)
    assert (
        client.patch(f"{PREFIX}/projects/{project['id']}", json={"archived": False}).status_code
        == 404
    )
    with client.app.state.session_factory() as db:
        assert db.get(Episode, UUID(episode["id"])) is not None
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "project.deleted")
            )
            == 1
        )
    assert restore(client, project, str(uuid4())).status_code == 409
    restored = restore(client, project, receipt["deletion_id"])
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived"] is True
    assert client.get(PREFIX + "/project-trash").json() == []
    assert len(client.get(PREFIX + "/projects").json()) == 1
    assert client.get(f"{PREFIX}/episodes/{episode['id']}").status_code == 200
    assert restore(client, project, receipt["deletion_id"]).status_code == 409
    second = delete(client, project).json()
    assert second["deletion_id"] != receipt["deletion_id"]
    assert restore(client, project, receipt["deletion_id"]).status_code == 409
    assert restore(client, project, second["deletion_id"]).status_code == 200
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "project.restored")
            )
            == 2
        )


def test_expired_delete_retains_data_and_never_purges(client, monkeypatch):
    project = create_project(client)
    archive(client, project)
    receipt = delete(client, project).json()
    from datetime import datetime

    from arclight.api import project_deletion

    deadline = datetime.fromisoformat(receipt["restore_until"])
    monkeypatch.setattr(project_deletion, "utcnow", lambda: deadline)
    response = restore(client, project, receipt["deletion_id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RESTORE_EXPIRED"
    assert client.get(PREFIX + "/project-trash").json()[0]["can_restore"] is False
    with client.app.state.session_factory() as db:
        assert db.get(Project, UUID(project["id"])).deleted_at is not None


@pytest.mark.legacy_schema
def test_migration_refuses_to_resurrect_deleted_projects(client, migrated):
    project = create_project(client)
    create_episode(client, project)
    archive(client, project)
    receipt = delete(client, project).json()
    with pytest.raises(RuntimeError, match="Deleted projects exist"):
        command.downgrade(migrated, "0005")
    with client.app.state.session_factory() as db:
        assert "deleted_at" in {
            column["name"] for column in inspect(db.bind).get_columns("projects")
        }
    assert restore(client, project, receipt["deletion_id"]).status_code == 200
    command.downgrade(migrated, "0005")
    command.upgrade(migrated, "0018")
    assert len(client.get(f"{PREFIX}/projects/{project['id']}/episodes").json()) == 1


def test_deleted_project_hides_preserved_assets_members_and_run_history(client):
    from arclight.persistence.models import VisualAsset

    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    member = register(client, "viewer@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role="viewer"
            )
        )
        asset = VisualAsset(project_id=UUID(project["id"]), name="retained", asset_type="CHARACTER")
        db.add(asset)
        run = WorkflowRun(
            episode_id=UUID(episode["id"]),
            status="SUCCEEDED",
            trace_id="retained",
            idempotency_key="retained",
            request_fingerprint="retained",
            input_json={},
        )
        db.add(run)
        db.flush()
        asset_id, run_id = asset.id, run.id
    archive(client, project)
    receipt = delete(client, project).json()
    for suffix in [
        f"/runs/{run_id}",
        f"/runs/{run_id}/events",
        f"/projects/{project['id']}/story-entries",
    ]:
        assert client.get(PREFIX + suffix).status_code == 404
    assert restore(client, project, receipt["deletion_id"]).status_code == 200
    with client.app.state.session_factory() as db:
        assert db.get(VisualAsset, asset_id).name == "retained"
        assert db.get(WorkflowRun, run_id).status == "SUCCEEDED"
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert client.get(f"{PREFIX}/projects/{project['id']}").status_code == 200
    assert client.get(f"{PREFIX}/projects/{project['id']}/access").json()["role"] == "viewer"


def test_delete_and_restore_validate_payloads_without_changing_state(client):
    project = create_project(client)
    archive(client, project)
    path = f"{PREFIX}/projects/{project['id']}"
    for body in [{}, {"confirm_name": " "}, {"confirm_name": project["name"], "purge": True}]:
        assert client.request("DELETE", path, json=body).status_code == 422
    assert client.post(path + "/restore", json={"deletion_id": "not-a-uuid"}).status_code == 422
    assert client.get(path).status_code == 200


def test_deletion_preserves_versions_reviews_asset_dependencies_and_canvas(client):
    from test_episode_copy import source_content

    from arclight.persistence.models import (
        AssetDependency,
        AssetVersion,
        CanvasLayout,
        Review,
        Script,
        Storyboard,
    )

    project = create_project(client)
    episode = create_episode(client, project)
    script, board, references = source_content(client, episode, with_assets=True)
    with client.app.state.session_factory.begin() as db:
        dependency = AssetDependency(
            source_asset_version_id=UUID(references["CHARACTER"]["asset_version_id"]),
            target_asset_version_id=UUID(references["COSTUME"]["asset_version_id"]),
            dependency_type="costume",
        )
        db.add(dependency)
        layout = CanvasLayout(
            episode_id=UUID(episode["id"]),
            revision=1,
            positions={},
            viewport={"x": 10, "y": 20, "zoom": 1},
        )
        db.add(layout)
        db.flush()
        dependency_id, layout_id = dependency.id, layout.id
        reviews = db.scalar(select(func.count()).select_from(Review))
    archive(client, project)
    receipt = delete(client, project).json()
    for resource, version in [("scripts", script), ("storyboards", board)]:
        root = f"{PREFIX}/episodes/{episode['id']}/{resource}/{version['id']}"
        assert client.get(root + "/comparison").status_code == 404
        assert client.post(root + "/review", json={"decision": "APPROVE"}).status_code == 404
    assert (
        client.get(f"{PREFIX}/episodes/{episode['id']}/scripts/{script['id']}/lines").status_code
        == 404
    )
    assert (
        client.post(
            f"{PREFIX}/episodes/{episode['id']}/copy", json={"title": "denied", "episode_no": 2}
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"{PREFIX}/episodes/{episode['id']}/canvas/layout",
            json={"expected_revision": 1, "positions": {}, "viewport": {"x": 0, "y": 0, "zoom": 1}},
        ).status_code
        == 404
    )
    assert restore(client, project, receipt["deletion_id"]).status_code == 200
    with client.app.state.session_factory() as db:
        assert db.get(Script, UUID(script["id"])).content_json == script["content_json"]
        assert db.get(Storyboard, UUID(board["id"])).status == "APPROVED"
        assert db.get(AssetDependency, dependency_id) is not None
        assert db.get(CanvasLayout, layout_id).viewport["x"] == 10
        assert db.scalar(select(func.count()).select_from(Review)) == reviews
        for reference in references.values():
            assert db.get(AssetVersion, UUID(reference["asset_version_id"])) is not None


def test_offline_downgrade_cannot_skip_deletion_safety_check():
    from io import StringIO
    from pathlib import Path

    from alembic.config import Config

    config = Config(
        str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=StringIO()
    )
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    with pytest.raises(RuntimeError, match="online tombstone check"):
        command.downgrade(config, "0006:0005", sql=True)
