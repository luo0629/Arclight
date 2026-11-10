"""Durable render commands retain permission, idempotency and cancellation contracts."""

from uuid import UUID, uuid4

import pytest
from render_admission_helpers import assert_blocked, ready_manifest, render_path, request_export
from render_job_helpers import (
    cancel,
    job_case,
    read,
    render_case,
    review_case,
    run,
    submit,
    timeline_case,
)
from test_collaboration import register
from test_human_reviews import decide, resume

from arclight.application import render_execution
from arclight.persistence.human_reviews import HumanReviewResume
from arclight.persistence.models import Project, ProjectMember, User

__all__ = ["job_case", "render_case", "review_case", "timeline_case"]


def test_queue_cancel_uses_revision_and_replay_does_not_restart(client, job_case):
    manifest, value, body = job_case
    path = f"/api/v1/render-jobs/{value['id']}/cancel"
    for revision in (0, True, "1"):
        assert client.post(path, json={"expected_revision": revision}).status_code == 422
    assert_blocked(
        client.post(path, json={"expected_revision": value["revision"] + 1}), "REVISION_CONFLICT"
    )
    stopped = cancel(client, value)
    assert stopped.status_code == 200, stopped.text
    final = stopped.json()
    assert final["status"] == "CANCELLED" and final["attempt_count"] == 0
    assert final["finished_at"] and final["cancel_requested_at"]
    assert_blocked(
        client.post(path, json={"expected_revision": value["revision"]}), "REVISION_CONFLICT"
    )
    assert cancel(client, value).json() == final
    assert run(client, value) == "CANCELLED"
    assert client.post(render_path(manifest, "jobs"), json=body).json() == final
    assert client.get(f"/api/v1/render-jobs/{value['id']}/attempts").json() == []


def test_retry_is_a_new_execution_and_creation_key_binds_inputs(client, job_case):
    manifest, value, body = job_case
    path = render_path(manifest, "jobs")
    assert_blocked(
        client.post(path, json={**body, "retry_of": str(uuid4())}), "IDEMPOTENCY_CONFLICT"
    )
    assert_blocked(
        client.post(path, json={"creation_key": str(uuid4()), "retry_of": value["id"]}),
        "RENDER_RETRY_CONFLICT",
    )
    assert_blocked(
        client.post(path, json={"creation_key": str(uuid4()), "retry_of": str(uuid4())}),
        "NOT_FOUND",
    )
    assert client.post(path, json={**body, "command": "arbitrary"}).status_code == 422
    assert cancel(client, value).status_code == 200
    retry, retry_body = submit(client, manifest, retry_of=value["id"])
    assert retry["id"] != value["id"] and retry["retry_of_id"] == value["id"]
    assert read(client, value)["status"] == "CANCELLED"
    assert client.post(path, json=retry_body).json() == retry


def test_history_pages_have_no_gaps_and_reject_foreign_cursors(client, job_case, render_case):
    manifest, value, _ = job_case
    for _ in range(3):
        submit(client, manifest)
    path = render_path(manifest, "jobs")
    complete = client.get(path).json()
    first = client.get(path, params={"limit": 2}).json()
    second = client.get(path, params={"limit": 2, "before_id": first[-1]["id"]}).json()
    assert first + second == complete and len({row["id"] for row in complete}) == 4
    assert value["id"] in {row["id"] for row in complete}
    assert client.get(path, params={"before_id": complete[-1]["id"]}).json() == []
    other, _ = ready_manifest(client, render_case)
    foreign, _ = submit(client, other)
    assert_blocked(client.get(path, params={"before_id": foreign["id"]}), "NOT_FOUND")
    assert_blocked(
        client.post(
            render_path(other, "jobs"),
            json={"creation_key": str(uuid4()), "retry_of": value["id"]},
        ),
        "NOT_FOUND",
    )


def test_viewer_reads_editor_writes_and_stranger_cannot_discover_jobs(client, job_case):
    manifest, value, body = job_case
    member = register(client, "render-controls-member@example.com")
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(manifest["project_id"]),
            user_id=UUID(member["user"]["id"]),
            role="viewer",
        )
        db.add(membership)
        db.flush()
        membership_id = membership.id
    assert read(client, value) == value
    assert client.get(render_path(manifest, "jobs")).json() == [value]
    assert_blocked(cancel(client, value), "FORBIDDEN")
    assert_blocked(client.post(render_path(manifest, "jobs"), json=body), "FORBIDDEN")
    with client.app.state.session_factory.begin() as db:
        db.get(ProjectMember, membership_id).role = "editor"
    assert_blocked(client.post(render_path(manifest, "jobs"), json=body), "IDEMPOTENCY_CONFLICT")
    editor_job, _ = submit(client, manifest)
    assert cancel(client, editor_job).status_code == 200
    register(client, "render-controls-stranger@example.com")
    for path in (
        f"/api/v1/render-jobs/{value['id']}",
        f"/api/v1/render-jobs/{value['id']}/attempts",
        render_path(manifest, "jobs"),
    ):
        assert client.get(path).status_code == 404
    assert_blocked(
        client.post(f"/api/v1/render-jobs/{value['id']}/cancel", json={"expected_revision": 1}),
        "NOT_FOUND",
    )


def test_archival_blocks_new_work_and_worker_rechecks_scope(client, job_case):
    manifest, value, _ = job_case
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(manifest["project_id"])).archived = True
    assert_blocked(
        client.post(render_path(manifest, "jobs"), json={"creation_key": str(uuid4())}),
        "PROJECT_ARCHIVED",
    )
    assert run(client, value) == "FAILED"
    final = read(client, value)
    assert final["error_code"] == "PROJECT_ARCHIVED"
    assert final["output_file_id"] is None and final["cleanup_status"] == "CLEANED"


def test_formal_job_needs_exact_approval_and_independent_resume(client, render_case):
    manifest, _ = ready_manifest(client, render_case, profile="FORMAL")
    path = render_path(manifest, "jobs")
    body = {"creation_key": str(uuid4())}
    response = client.post(path, json=body)
    assert response.status_code == 409
    request = request_export(client, manifest)
    decision = decide(client, request)
    assert decision.status_code == 200
    assert client.post(path, json=body).status_code == 409
    assert resume(client, request, decision.json()).status_code == 200
    response = client.post(path, json=body)
    assert response.status_code == 202 and response.json()["profile"] == "FORMAL"
    assert not response.json()["is_delivery"]
    value = response.json()
    assert run(client, value) == "SUCCEEDED"
    final = read(client, value)
    assert final["profile"] == "FORMAL" and final["output_file_id"]
    assert not final["is_delivery"]


@pytest.mark.parametrize("revoke_at", ["queued", "encoding"])
@pytest.mark.parametrize("change", ["owner_changed", "disabled"])
def test_formal_worker_rechecks_original_approver_without_changing_review_history(
    client, render_case, monkeypatch, revoke_at, change
):
    manifest, _ = ready_manifest(client, render_case, profile="FORMAL")
    owner = register(client, "render-formal-owner@example.com")
    owner_id = owner["user"]["id"]
    factory = client.app.state.session_factory
    with factory.begin() as db:
        db.get(Project, UUID(manifest["project_id"])).owner_id = owner_id
    request = request_export(client, manifest)
    decision = decide(client, request)
    assert decision.status_code == 200, decision.text
    receipt = resume(client, request, decision.json())
    assert receipt.status_code == 200, receipt.text
    editor = register(client, "render-formal-successor@example.com")
    with factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(manifest["project_id"]),
                user_id=UUID(editor["user"]["id"]),
                role="editor",
            )
        )
    value, _ = submit(client, manifest)

    def revoke():
        with factory.begin() as db:
            if change == "owner_changed":
                db.get(Project, UUID(manifest["project_id"])).owner_id = editor["user"]["id"]
            else:
                db.get(User, UUID(owner_id)).disabled = True

    encodes = []

    def encode(*args, **kwargs):
        encodes.append(True)
        revoke()
        return b"candidate-never-persisted"

    if revoke_at == "queued":
        revoke()
    monkeypatch.setattr(render_execution, "execute_media", encode)
    assert run(client, value) == "FAILED"
    final = read(client, value)
    assert final["error_code"] == ("NOT_FOUND" if change == "owner_changed" else "FORBIDDEN")
    assert final["output_file_id"] is None and final["cleanup_status"] == "CLEANED"
    assert bool(encodes) == (revoke_at == "encoding")
    with factory() as db:
        assert (
            db.get(HumanReviewResume, UUID(receipt.json()["id"])).result_json
            == receipt.json()["result_json"]
        )
