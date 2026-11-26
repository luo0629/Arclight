"""Export approval remains subject to live project scope and reviewer authority."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from render_admission_helpers import (
    assert_blocked,
    formal_export_command,
    record_export_decision,
    record_export_resume,
    start_formal_export,
)
from render_manifest_helpers import render_case, review_case, timeline_case
from test_collaboration import register

from arclight.application.render_admission import admit_render
from arclight.persistence.human_reviews import HumanReviewResume
from arclight.persistence.models import Episode, Project, ProjectMember, User

__all__ = ["render_case", "review_case", "timeline_case"]


def test_editor_can_request_but_owner_must_decide_and_resume(client, episode, render_case):
    member = register(client, "export-editor@example.com")
    editor_token = client.headers["Authorization"]
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(episode["project_id"]),
            user_id=UUID(member["user"]["id"]),
            role="editor",
        )
        db.add(membership)
        db.flush()
        membership_id = membership.id
    creation_key = str(uuid4())
    started = start_formal_export(client, render_case, creation_key=creation_key)
    assert started.status_code == 201, started.text
    value, row = started.json()["manifest"], started.json()["review"]
    sha = row["request_sha256"]
    assert row["requested_by"] == member["user"]["id"]
    assert_blocked(formal_export_command(client, value, "APPROVE", request_sha256=sha), "FORBIDDEN")
    client.headers["Authorization"] = "Bearer test-token"
    decision = record_export_decision(client, row)
    client.headers["Authorization"] = editor_token
    assert_blocked(
        formal_export_command(client, value, "CONTINUE", request_sha256=sha), "FORBIDDEN"
    )
    client.headers["Authorization"] = "Bearer test-token"
    receipt = record_export_resume(client, row, decision)
    client.headers["Authorization"] = editor_token
    continued = formal_export_command(client, value, "CONTINUE", request_sha256=sha)
    assert continued.status_code == 200, continued.text
    assert continued.json()["review"]["resume"] == receipt
    assert continued.json()["job"]["manifest_id"] == value["id"]
    with client.app.state.session_factory.begin() as db:
        db.get(ProjectMember, membership_id).role = "viewer"
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value
    assert_blocked(start_formal_export(client, render_case, creation_key=creation_key), "FORBIDDEN")
    for action in ("APPROVE", "REJECT", "CONTINUE"):
        assert_blocked(
            formal_export_command(client, value, action, request_sha256=sha), "FORBIDDEN"
        )
    register(client, "export-stranger@example.com")
    assert client.get(f"/api/v1/render-manifests/{value['id']}").status_code == 404
    assert client.get(f"/api/v1/human-reviews/{row['id']}").status_code == 404
    assert_blocked(start_formal_export(client, render_case, creation_key=creation_key), "NOT_FOUND")
    assert_blocked(
        formal_export_command(client, value, "CONTINUE", request_sha256=sha), "NOT_FOUND"
    )


@pytest.mark.parametrize("change", ["owner_changed", "disabled"])
def test_admission_rechecks_original_approver_even_after_durable_resume(
    client, episode, render_case, change
):
    owner = register(client, "export-owner@example.com")
    owner_id = owner["user"]["id"]
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).owner_id = owner_id
    started = start_formal_export(client, render_case)
    assert started.status_code == 201, started.text
    value, row = started.json()["manifest"], started.json()["review"]
    sha = row["request_sha256"]
    decision = record_export_decision(client, row)
    receipt = record_export_resume(client, row, decision)
    with client.app.state.session_factory() as db:
        admitted = admit_render(db, client.app.state.settings, UUID(value["id"]), owner_id)
        assert str(admitted.review_request_id) == row["id"]
        assert str(admitted.decision_id) == decision["id"]
    editor = register(client, "export-successor@example.com")
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(value["project_id"]),
                user_id=UUID(editor["user"]["id"]),
                role="editor",
            )
        )
        if change == "owner_changed":
            db.get(Project, UUID(value["project_id"])).owner_id = editor["user"]["id"]
        else:
            db.get(User, UUID(owner_id)).disabled = True
    assert_blocked(
        formal_export_command(client, value, "CONTINUE", request_sha256=sha),
        "NOT_FOUND" if change == "owner_changed" else "FORBIDDEN",
    )
    # Revocation does not alter the historical receipt or grant a replacement approval.
    with client.app.state.session_factory() as db:
        assert db.get(HumanReviewResume, UUID(receipt["id"])).result_json == receipt["result_json"]


@pytest.mark.parametrize(
    "change,code",
    [
        ("project", "PROJECT_ARCHIVED"),
        ("episode", "EPISODE_ARCHIVED"),
        ("deleted", "NOT_FOUND"),
    ],
)
def test_scope_lifecycle_blocks_export_after_approval_without_erasing_history(
    client, render_case, change, code
):
    creation_key = str(uuid4())
    started = start_formal_export(client, render_case, creation_key=creation_key)
    assert started.status_code == 201, started.text
    value, row = started.json()["manifest"], started.json()["review"]
    sha = row["request_sha256"]
    approved = formal_export_command(client, value, "APPROVE", request_sha256=sha)
    assert approved.status_code == 200, approved.text
    receipt = approved.json()["review"]["resume"]
    assert receipt["result_json"]["status"] == "APPROVED"
    with client.app.state.session_factory.begin() as db:
        project = db.get(Project, UUID(value["project_id"]))
        if change == "project":
            project.archived = True
        elif change == "deleted":
            project.deleted_at = datetime.now(UTC)
        else:
            db.get(Episode, UUID(value["snapshot"]["timeline"]["episode_id"])).status = "ARCHIVED"
    assert_blocked(start_formal_export(client, render_case, creation_key=creation_key), code)
    assert_blocked(formal_export_command(client, value, "CONTINUE", request_sha256=sha), code)
    detail = client.get(f"/api/v1/render-manifests/{value['id']}")
    if change == "deleted":
        assert detail.status_code == 404
    else:
        assert detail.json() == value
    with client.app.state.session_factory() as db:
        assert db.get(HumanReviewResume, UUID(receipt["id"])).result_json == receipt["result_json"]
