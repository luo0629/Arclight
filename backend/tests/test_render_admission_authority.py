"""Export approval remains subject to live project scope and reviewer authority."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from render_admission_helpers import assert_blocked, ready_manifest, render_path, request_export
from render_manifest_helpers import render_case, review_case, timeline_case
from test_collaboration import register
from test_human_reviews import decide, resume

from arclight.persistence.human_reviews import HumanReviewResume
from arclight.persistence.models import Episode, Project, ProjectMember, User

__all__ = ["render_case", "review_case", "timeline_case"]


def test_editor_can_request_but_owner_must_decide_and_resume(client, render_case):
    value, _ = ready_manifest(client, render_case, profile="FORMAL")
    member = register(client, "export-editor@example.com")
    editor_token = client.headers["Authorization"]
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(value["project_id"]),
            user_id=UUID(member["user"]["id"]),
            role="editor",
        )
        db.add(membership)
        db.flush()
        membership_id = membership.id
    row = request_export(client, value)
    assert row["requested_by"] == member["user"]["id"]
    assert_blocked(decide(client, row), "FORBIDDEN")
    assert_blocked(resume(client, row, {"id": str(uuid4())}), "FORBIDDEN")
    client.headers["Authorization"] = "Bearer test-token"
    decision = decide(client, row)
    assert decision.status_code == 200, decision.text
    assert resume(client, row, decision.json()).status_code == 200
    client.headers["Authorization"] = editor_token
    assert client.post(render_path(value, "admission")).status_code == 200
    with client.app.state.session_factory.begin() as db:
        db.get(ProjectMember, membership_id).role = "viewer"
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value
    for operation in ("export-review", "admission"):
        assert_blocked(client.post(render_path(value, operation)), "FORBIDDEN")
    assert_blocked(decide(client, row), "FORBIDDEN")
    assert_blocked(resume(client, row, decision.json()), "FORBIDDEN")
    register(client, "export-stranger@example.com")
    assert client.get(f"/api/v1/render-manifests/{value['id']}").status_code == 404
    assert client.get(f"/api/v1/human-reviews/{row['id']}").status_code == 404
    for operation in ("export-review", "admission"):
        assert_blocked(client.post(render_path(value, operation)), "NOT_FOUND")


@pytest.mark.parametrize("change", ["owner_changed", "disabled"])
def test_admission_rechecks_original_approver_even_after_durable_resume(
    client, render_case, change
):
    value, _ = ready_manifest(client, render_case, profile="FORMAL")
    owner = register(client, "export-owner@example.com")
    owner_id = owner["user"]["id"]
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(value["project_id"])).owner_id = owner_id
    row = request_export(client, value)
    decision = decide(client, row)
    assert decision.status_code == 200, decision.text
    receipt = resume(client, row, decision.json())
    assert receipt.status_code == 200, receipt.text
    assert client.post(render_path(value, "admission")).status_code == 200
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
        client.post(render_path(value, "admission")),
        "NOT_FOUND" if change == "owner_changed" else "FORBIDDEN",
    )
    # Revocation does not alter the historical receipt or grant a replacement approval.
    with client.app.state.session_factory() as db:
        assert (
            db.get(HumanReviewResume, UUID(receipt.json()["id"])).result_json
            == (receipt.json()["result_json"])
        )


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
    value, _ = ready_manifest(client, render_case, profile="FORMAL")
    row = request_export(client, value)
    decision = decide(client, row)
    assert decision.status_code == 200, decision.text
    assert resume(client, row, decision.json()).status_code == 200
    with client.app.state.session_factory.begin() as db:
        project = db.get(Project, UUID(value["project_id"]))
        if change == "project":
            project.archived = True
        elif change == "deleted":
            project.deleted_at = datetime.now(UTC)
        else:
            db.get(Episode, UUID(value["snapshot"]["timeline"]["episode_id"])).status = "ARCHIVED"
    for operation in ("export-review", "admission"):
        assert_blocked(client.post(render_path(value, operation)), code)
    detail = client.get(f"/api/v1/render-manifests/{value['id']}")
    if change == "deleted":
        assert detail.status_code == 404
    else:
        assert detail.json() == value
