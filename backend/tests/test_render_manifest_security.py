"""Manifest/result history retains existing membership and lifecycle gates."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from render_manifest_helpers import (
    manifest,
    manifest_url,
    render_case,
    report_case,
    result_case,
    review_case,
    timeline_case,
)
from test_collaboration import register

from arclight.application.render_results import bind_render_artifact, bind_render_report
from arclight.core.errors import DomainError
from arclight.persistence.models import Episode, Project, ProjectMember

__all__ = ["render_case", "report_case", "result_case", "review_case", "timeline_case"]


def assert_result_write_denied(client, value, artifact, report, actor, code):
    for writer, arguments in (
        (bind_render_artifact, (artifact.file_id, artifact.duration_ms)),
        (bind_render_report, (report.file_id,)),
    ):
        with pytest.raises(DomainError) as error, client.app.state.session_factory.begin() as db:
            writer(db, client.app.state.settings, UUID(value["id"]), *arguments, actor)
        assert error.value.code == code


def test_membership_revocation_and_role_changes_gate_new_writes_and_replays(
    client, episode, render_case, report_case
):
    original, artifact, report = report_case
    owner_header = client.headers["Authorization"]
    value, owner_body = manifest(client, render_case)
    member = register(client, "render-editor@example.com")
    actor = member["user"]["id"]
    history = manifest_url(render_case)
    detail = f"/api/v1/render-manifests/{original['id']}"
    for path in (history, detail, detail + "/results"):
        assert client.get(path).status_code == 404
    assert client.post(history, json=owner_body).status_code == 404
    assert_result_write_denied(client, original, artifact, report, actor, "NOT_FOUND")
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(episode["project_id"]), user_id=UUID(actor), role="viewer"
        )
        db.add(membership)
        db.flush()
        identity = membership.id
    assert client.get(detail).json() == original
    assert client.get(detail + "/results").status_code == 200
    assert client.get(history).status_code == 200
    assert client.post(history, json=owner_body).status_code == 403
    assert_result_write_denied(client, original, artifact, report, actor, "FORBIDDEN")
    with client.app.state.session_factory.begin() as db:
        db.get(ProjectMember, identity).role = "editor"
    conflict = client.post(history, json=owner_body)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    own, own_body = manifest(client, render_case)
    assert own["created_by"] == actor
    assert client.post(history, json=own_body).json() == own
    with client.app.state.session_factory.begin() as db:
        db.get(ProjectMember, identity).role = "viewer"
    assert client.post(history, json=own_body).status_code == 403
    assert_result_write_denied(client, original, artifact, report, actor, "FORBIDDEN")
    with client.app.state.session_factory.begin() as db:
        db.delete(db.get(ProjectMember, identity))
    assert client.post(history, json=own_body).status_code == 404
    assert client.get(detail).status_code == 404
    assert_result_write_denied(client, original, artifact, report, actor, "NOT_FOUND")
    client.headers["Authorization"] = owner_header
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value


def test_archive_is_read_only_and_deleted_project_hides_input_and_results(
    client, episode, render_case, report_case
):
    value, artifact, report = report_case
    _, body = manifest(client, render_case)
    detail = f"/api/v1/render-manifests/{value['id']}"
    for model, identity, field, archived, restored, code in (
        (Episode, episode["id"], "status", "ARCHIVED", "DRAFT", "EPISODE_ARCHIVED"),
        (Project, episode["project_id"], "archived", True, False, "PROJECT_ARCHIVED"),
    ):
        with client.app.state.session_factory.begin() as db:
            setattr(db.get(model, UUID(identity)), field, archived)
        assert client.get(detail).json() == value
        assert client.get(detail + "/results").status_code == 200
        for request in (body, {"creation_key": str(uuid4())}):
            assert client.post(manifest_url(render_case), json=request).status_code == 409
        assert_result_write_denied(client, value, artifact, report, "service-owner", code)
        with client.app.state.session_factory.begin() as db:
            setattr(db.get(model, UUID(identity)), field, restored)
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).deleted_at = datetime.now(UTC)
    for path in (detail, detail + "/results", manifest_url(render_case)):
        assert client.get(path).status_code == 404
    assert client.post(manifest_url(render_case), json=body).status_code == 404
    assert_result_write_denied(client, value, artifact, report, "service-owner", "NOT_FOUND")


def test_public_routes_cannot_edit_history_or_write_trusted_results(client, render_case):
    value, _ = manifest(client, render_case)
    detail = f"/api/v1/render-manifests/{value['id']}"
    assert client.patch(detail, json={"profile": "FORMAL"}).status_code == 405
    assert client.delete(detail).status_code == 405
    assert client.post(detail + "/results", json={"file_id": str(uuid4())}).status_code == 405
    assert client.get(detail).json() == value
