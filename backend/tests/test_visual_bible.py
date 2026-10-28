"""ASSET-10 explicit fields, append-only snapshots, locks, CAS and migration."""

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from test_collaboration import create_project, register

from arclight.persistence.models import (
    AuditEvent,
    Project,
    ProjectMember,
    VisualBibleVersion,
    WorkflowRun,
)


def path(project):
    return f"/api/v1/projects/{project['id']}/visual-bible"


def edit(client, project, expected=0, **fields):
    return client.post(
        path(project) + "/versions",
        json={
            "expected_version_no": expected,
            "creation_key": str(uuid4()),
            "fields": fields,
        },
    )


def lock(client, project, expected, fields):
    return client.post(
        path(project) + "/locks",
        json={
            "expected_version_no": expected,
            "creation_key": str(uuid4()),
            "locked_fields": fields,
        },
    )


def test_fields_versions_locks_partial_edits_and_exact_history(client, episode):
    project = {"id": episode["project_id"]}
    assert client.get(path(project)).json()["version_no"] == 0
    first = edit(client, project, art_style=" 水彩\n", palette="蓝灰", prohibited="血腥")
    assert first.status_code == 201, first.text
    first = first.json()
    assert first["fields"]["art_style"] == " 水彩\n"
    locked = lock(client, project, 1, ["art_style"]).json()
    assert locked["version_no"] == 2
    assert edit(client, project, 2, art_style="覆盖").status_code == 409
    other = edit(client, project, 2, lighting="月光")
    assert other.status_code == 201
    assert other.json()["fields"]["art_style"] == " 水彩\n"
    assert edit(client, project, 1, composition="旧输入").status_code == 409
    assert lock(client, project, 3, []).status_code == 201
    assert edit(client, project, 4, art_style="新画风").status_code == 201
    assert client.get(path(project) + "/versions/" + first["id"]).json() == first
    history = client.get(path(project) + "/versions").json()
    assert [row["version_no"] for row in history] == [1, 2, 3, 4, 5]
    assert client.get(path(project) + "/versions?limit=2&offset=2").json() == history[2:4]
    assert client.get(path(project) + "/versions/" + str(uuid4())).status_code == 404


def test_legacy_preservation_patch_bypass_and_provider_snapshot(client, episode):
    project = {"id": episode["project_id"]}
    legacy = {"description": "旧水彩", "palette": ["旧色"], "unknown": {"locked": True}}
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(project["id"])).style_guide = legacy
    initial = client.get(path(project)).json()
    assert initial["fields"]["art_style"] == "旧水彩"
    assert initial["legacy_snapshot"] == legacy
    endpoint = f"/api/v1/projects/{project['id']}"
    assert (
        client.patch(
            endpoint,
            json={
                "style_guide": {**legacy, "unknown": "overwrite"},
                "expected_visual_version_no": 0,
            },
        ).status_code
        == 422
    )
    assert (
        client.patch(endpoint, json={"style_guide": {**legacy, "description": "无CAS"}}).status_code
        == 422
    )
    response = client.patch(
        endpoint,
        json={"style_guide": {**legacy, "description": "新版"}, "expected_visual_version_no": 0},
    )
    assert response.status_code == 200, response.text
    assert response.json()["style_guide"] == {**legacy, "description": "新版"}
    lock(client, project, 1, ["art_style"])
    assert (
        client.patch(
            endpoint,
            json={
                "style_guide": {**legacy, "description": "绕过锁"},
                "expected_visual_version_no": 2,
            },
        ).status_code
        == 409
    )
    assert client.patch(endpoint, json={"output_profile": {"width": 100}}).status_code == 200
    version = client.get(path(project)).json()
    assert version["legacy_snapshot"] == legacy
    started = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        headers={"Idempotency-Key": str(uuid4())},
        json={"story": "测试视觉规范输入"},
    )
    assert started.status_code == 202, started.text
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, UUID(started.json()["run_id"]))
        frozen = run.input_json["visual_bible"]
        assert frozen["version_id"] == version["id"]
        assert frozen["fields"]["art_style"] == "新版"
        assert frozen["locked_fields"] == ["art_style"]
    # New snapshots can be maintained during a run; existing inputs remain fixed.
    assert edit(client, project, 2, line_style="细线").status_code == 201
    with client.app.state.session_factory() as db:
        assert (
            db.get(WorkflowRun, UUID(started.json()["run_id"])).input_json["visual_bible"] == frozen
        )


def test_idempotency_and_audit_once(client, episode):
    project = {"id": episode["project_id"]}
    body = {"expected_version_no": 0, "creation_key": str(uuid4()), "fields": {"art_style": "test"}}
    first = client.post(path(project) + "/versions", json=body)
    assert first.status_code == 201
    assert client.post(path(project) + "/versions", json=body).json() == first.json()
    assert (
        client.post(
            path(project) + "/versions", json={**body, "fields": {"palette": "other"}}
        ).status_code
        == 409
    )
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(VisualBibleVersion)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "visual_bible.saved")
            )
            == 1
        )


@pytest.mark.parametrize(
    "fields",
    [{}, {"unknown": "x"}, {"art_style": None}, {"palette": ["red"]}, {"lighting": "x" * 2001}],
)
def test_typed_bounded_patch_rejected_without_versions(client, episode, fields):
    project = {"id": episode["project_id"]}
    response = client.post(
        path(project) + "/versions",
        json={"expected_version_no": 0, "creation_key": str(uuid4()), "fields": fields},
    )
    assert response.status_code == 422
    assert client.get(path(project) + "/versions").json() == []


@pytest.mark.parametrize("fields", [["unknown"], ["art_style", "art_style"]])
def test_invalid_locks(client, episode, fields):
    assert lock(client, {"id": episode["project_id"]}, 0, fields).status_code == 422


@pytest.mark.parametrize("role", ["editor", "viewer", "outsider"])
def test_permissions_and_project_scope(client, role):
    register(client)
    project = create_project(client)
    saved = edit(client, project, art_style="owner").json()
    member = register(client, f"visual-{role}@example.com")
    if role != "outsider":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]), user_id=UUID(member["user"]["id"]), role=role
                )
            )
    assert client.get(path(project)).status_code == (404 if role == "outsider" else 200)
    assert (
        edit(client, project, 1, line_style="editor").status_code
        == {"editor": 201, "viewer": 403, "outsider": 404}[role]
    )
    assert lock(client, project, 2 if role == "editor" else 1, ["art_style"]).status_code == (
        404 if role == "outsider" else 403
    )
    other = create_project(client)
    assert client.get(path(other) + "/versions/" + saved["id"]).status_code == 404


@pytest.mark.legacy_schema
def test_archived_deleted_project_and_empty_migration_roundtrip(client, episode, migrated):
    project = {"id": episode["project_id"]}
    command.downgrade(migrated, "0017")
    command.upgrade(migrated, "0018")
    assert edit(client, project, art_style="safe").status_code == 201
    with pytest.raises(RuntimeError, match="Visual Bible versions exist"):
        command.downgrade(migrated, "0017")
    config = Config(migrated.config_file_name, output_buffer=StringIO())
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    with pytest.raises(RuntimeError, match="online preservation check"):
        command.downgrade(config, "0018:0017", sql=True)
    endpoint = f"/api/v1/projects/{project['id']}"
    client.patch(endpoint, json={"archived": True})
    assert client.get(path(project)).status_code == 200
    assert edit(client, project, 1, lighting="x").status_code == 409
    assert lock(client, project, 1, ["art_style"]).status_code == 409
    client.request("DELETE", endpoint, json={"confirm_name": "测试漫剧"})
    assert client.get(path(project)).status_code == 404
    # Verify the current schema after the historical preservation assertions.
    command.upgrade(migrated, "head")
    command.check(migrated)


def test_concurrent_edit_and_lock_same_base(client, episode, settings):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL project lock verified in dedicated CI")
    project = {"id": episode["project_id"]}
    edit(client, project, art_style="base")
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(edit, client, project, 1, art_style="new"),
            pool.submit(lock, client, project, 1, ["art_style"]),
        ]
        assert sorted(call.result().status_code for call in calls) == [201, 409]
    assert len(client.get(path(project) + "/versions").json()) == 2
