"""UI-07: database pagination of real ledgers and exact project read boundaries."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from render_job_helpers import job_case, read, render_case, review_case, run, timeline_case
from render_manifest_helpers import result_case
from sqlalchemy import event
from test_collaboration import register
from test_generation_schema import add_attempt, add_job
from test_generation_schema import generation_seed as generation_seed

from arclight.application.text_reviews import pause_for_content
from arclight.domain.human_reviews import ReviewSnapshot
from arclight.domain.versions import digest
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewRequest
from arclight.persistence.models import (
    AssetVersion,
    Episode,
    Project,
    RenderExecution,
    RenderJob,
    Script,
    Storyboard,
    StoryOutline,
    WorkflowRun,
)

__all__ = ["job_case", "render_case", "review_case", "timeline_case", "result_case"]


def endpoint(episode, resource):
    return f"/api/v1/projects/{episode['project_id']}/{resource}"


def task(db, episode, **changes):
    row = WorkflowRun(
        **{
            "episode_id": UUID(episode["id"]),
            "trace_id": "private-trace",
            "idempotency_key": str(uuid4()),
            "request_fingerprint": "a" * 64,
            "input_json": {"system_prompt": "secret-input"},
            **changes,
        }
    )
    db.add(row)
    db.flush()
    return row


def history_review(db, episode, **changes):
    identity = uuid4()
    snapshot = ReviewSnapshot(
        project_id=UUID(episode["project_id"]),
        episode_id=UUID(episode["id"]),
        run_id=None,
        thread_id=f"manual:{identity}",
        kind="SCRIPT",
        object_id=identity,
        version_id=identity,
        content_sha256="a" * 64,
        input_sha256="b" * 64,
    ).model_dump(mode="json")
    row = HumanReviewRequest(
        **{
            "project_id": UUID(episode["project_id"]),
            "episode_id": UUID(episode["id"]),
            "run_id": None,
            "creation_key": str(uuid4()),
            "handler": "UNKNOWN_HANDLER",
            "requested_by": "service-owner",
            "snapshot_json": snapshot,
            "request_sha256": digest(snapshot),
            **changes,
        }
    )
    db.add(row)
    db.flush()
    return row


def test_tasks_project_union_filters_use_latest_attempt_and_safe_diagnostics(
    client,
    episode,
    generation_seed,
):
    stamp = datetime(2026, 1, 2, tzinfo=UTC)
    with client.app.state.session_factory.begin() as db:
        text = task(
            db,
            episode,
            status="FAILED",
            error_code="private:key=secret",
            workflow_type="private-system-prompt",
            created_at=stamp,
        )
        text_id = str(text.id)
        media = add_job(db, generation_seed, status="SUCCEEDED", created_at=stamp)
        media_id = str(media.id)
        add_attempt(db, media, error_code="PROVIDER_FAILED", status="FAILED")
        add_attempt(db, media, attempt_no=2, error_code=None, status="SUCCEEDED")
        project_media = add_job(
            db,
            generation_seed,
            target_asset_id=None,
            episode_id=None,
            idempotency_key=str(uuid4()),
            status="FAILED",
            created_at=stamp,
        )
        add_attempt(db, project_media, error_code="provider-secret", status="FAILED")
    url = endpoint(episode, "tasks")
    result = client.get(url)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"
    value = result.json()
    assert value["total"] == 3
    assert {item["family"] for item in value["items"]} == {"WORKFLOW", "GENERATION"}
    assert all(item["created_at"].endswith("Z") for item in value["items"])
    texts = [item for item in value["items"] if item["id"] == text_id]
    assert texts[0]["error_code"] == "INTERNAL_ERROR"
    assert texts[0]["workflow_type"] == "unknown"
    assert all(
        secret not in result.text
        for secret in (
            "private",
            "secret",
            "input_json",
            "input_snapshot_json",
            "trace_id",
            "result_json",
        )
    )
    assert client.get(url, params={"error_code": "PROVIDER_FAILED"}).json()["total"] == 0
    assert client.get(url, params={"error_code": "INTERNAL_ERROR"}).json()["total"] == 2
    query = {
        "episode_id": episode["id"],
        "task_type": "IMAGE",
        "status": "SUCCEEDED",
        "created_from": stamp.isoformat(),
        "created_to": (stamp + timedelta(days=1)).isoformat(),
        "limit": 1,
    }
    filtered = client.get(url, params=query).json()
    assert filtered["total"] == 1 and filtered["items"][0]["id"] == media_id
    # A non-UTC input must select the same UTC instant on SQLite and PostgreSQL.
    query["created_from"] = "2026-01-02T08:00:00+08:00"
    query["created_to"] = "2026-01-02T08:00:01+08:00"
    assert client.get(url, params=query).json() == filtered
    assert client.get(url, params={"created_to": stamp.isoformat()}).json()["total"] == 0


def test_task_pagination_is_stable_and_database_bounded_without_payload_reads(client, episode):
    stamp = datetime(2026, 1, 2, tzinfo=UTC)
    with client.app.state.session_factory.begin() as db:
        identities = [str(task(db, episode, created_at=stamp).id) for _ in range(103)]
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(client.app.state.engine, "before_cursor_execute", capture)
    try:
        response = client.get(endpoint(episode, "tasks"), params={"offset": 100, "limit": 3})
    finally:
        event.remove(client.app.state.engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["total"] == 103
    assert [item["id"] for item in value["items"]] == sorted(identities)[100:]
    assert client.get(endpoint(episode, "tasks"), params={"offset": 999}).json()["items"] == []
    assert any("LIMIT" in sql.upper() and "OFFSET" in sql.upper() for sql in statements)
    assert not any(
        sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for sql in statements
    )
    assert not any(
        field in sql
        for sql in statements
        for field in (
            "input_json",
            "input_snapshot_json",
            "result_json",
            "agent_state_json",
            "trace_id",
        )
    )


def test_known_business_errors_are_public_categories_and_filter_in_sql(
    client, episode, generation_seed
):
    aliases = [
        ("ART_CONCEPT_BUDGET", "BUDGET_EXCEEDED"),
        ("BUDGET_UNPRICED_HISTORY", "BUDGET_EXCEEDED"),
        ("GATEWAY_QUOTA_EXHAUSTED", "PROVIDER_UNAVAILABLE"),
        ("GATEWAY_PROVIDER_RATE_LIMITED", "PROVIDER_UNAVAILABLE"),
        ("GATEWAY_PROVIDER_AUTHENTICATION_FAILED", "PROVIDER_UNAVAILABLE"),
        ("GATEWAY_PROVIDER_ACCESS_DENIED", "PROVIDER_UNAVAILABLE"),
        ("GATEWAY_RECONCILIATION_REQUIRED", "RESULT_UNKNOWN"),
        ("ART_CONCEPT_UNRESOLVED", "RESULT_UNKNOWN"),
        ("ART_CONCEPT_INPUT_CHANGED", "INPUT_INVALID"),
        ("ART_CONCEPT_REFERENCE_LOCKED", "INPUT_INVALID"),
        ("ART_CONCEPT_ROUND_LIMIT", "INPUT_INVALID"),
        ("WORKFLOW_EXECUTION_FAILED", "WORKFLOW_FAILED"),
        ("BUDGET_PRIVATE_SECRET", "INTERNAL_ERROR"),
    ]
    with client.app.state.session_factory.begin() as db:
        identities = {
            str(task(db, episode, status="FAILED", error_code=raw).id): category
            for raw, category in aliases
        }
        media = add_job(db, generation_seed, status="FAILED")
        add_attempt(db, media, error_code="PROVIDER_AUTHENTICATION_FAILED", status="FAILED")
        identities[str(media.id)] = "PROVIDER_UNAVAILABLE"
    url = endpoint(episode, "tasks")
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert {row["id"]: row["error_code"] for row in response.json()["items"]} == identities
    assert all(raw not in response.text for raw, _ in aliases)
    for category in set(identities.values()):
        page = client.get(url, params={"error_code": category, "limit": 1}).json()
        assert page["total"] == sum(value == category for value in identities.values())
        assert len(page["items"]) == 1 and page["items"][0]["error_code"] == category


def test_renderer_error_aliases_keep_real_failures_in_safe_filter_categories(
    client, episode, job_case
):
    _, job, _ = job_case
    url = endpoint(episode, "tasks")
    aliases = (
        ("RENDER_INPUT_CHANGED", "INPUT_INVALID"),
        ("RENDER_STORAGE_CHANGED", "STORAGE_FAILED"),
        ("RENDER_MEDIA_TIME_LIMIT", "RENDER_TIMEOUT"),
        ("RENDER_INSPECTION_INVALID_MEDIA", "INSPECTION_FAILED"),
        ("RENDER_OUTPUT_DURATION", "RENDER_OUTPUT_INVALID"),
        ("RENDER_WORKER_INTERRUPTED", "RENDER_INTERRUPTED"),
        ("RENDER_OUTPUT_PRIVATE_SECRET", "INTERNAL_ERROR"),
    )
    identities = {}
    with client.app.state.session_factory.begin() as db:
        original = db.get(RenderExecution, UUID(job["id"]))
        original_job = db.get(RenderJob, original.id)
        for raw, category in aliases:
            identity = uuid4()
            # Independent terminal fixtures preserve the immutable original history.
            db.add(
                RenderExecution(
                    id=identity,
                    project_id=original.project_id,
                    input_file_id=original.input_file_id,
                    created_by=original.created_by,
                    storage_identity=original.storage_identity,
                    intermediate_key=f"rendered/{original.project_id}/{identity}",
                    status="FAILED",
                    error_code=raw,
                    finished_at=datetime.now(UTC),
                )
            )
            db.flush()
            db.add(
                RenderJob(
                    id=identity,
                    manifest_id=original_job.manifest_id,
                    manifest_sha256=original_job.manifest_sha256,
                    project_id=original.project_id,
                    creation_key=uuid4(),
                    request_sha256="a" * 64,
                    phase="FAILED",
                )
            )
            identities[category] = str(identity)
    for raw, category in aliases:
        response = client.get(url, params={"task_type": "RENDER", "error_code": category})
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1
        summary = response.json()["items"][0]
        assert summary["id"] == identities[category] and summary["error_code"] == category
        assert raw not in response.text


@pytest.mark.parametrize(
    "query",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"task_type": "unknown"},
        {"status": "private status"},
        {"error_code": "private diagnostic"},
        {"created_from": "2026-01-02T00:00:00"},
        {"created_from": "2026-01-03T00:00:00Z", "created_to": "2026-01-02T00:00:00Z"},
    ],
)
def test_task_query_rejects_invalid_filters(client, episode, query):
    assert client.get(endpoint(episode, "tasks"), params=query).status_code == 422


def test_pending_queue_covers_legacy_manual_workflow_outline_and_exact_journal_once(
    client, episode
):
    eid = UUID(episode["id"])
    with client.app.state.session_factory.begin() as db:
        manual = Script(episode_id=eid, version_no=1, content_json={}, status="READY_FOR_REVIEW")
        logged = Script(episode_id=eid, version_no=2, content_json={}, status="READY_FOR_REVIEW")
        db.add_all([manual, logged])
        db.flush()
        manual_id, logged_id = str(manual.id), str(logged.id)
        paused = pause_for_content(db, logged, "service-owner")
        paused_id = str(paused.id)
        db.add(
            Storyboard(
                episode_id=eid,
                script_version_id=manual.id,
                version_no=1,
                content_json={},
                status="READY_FOR_REVIEW",
            )
        )
        db.add(
            StoryOutline(
                project_id=UUID(episode["project_id"]),
                episode_id=eid,
                version_no=1,
                content_json={},
                source_snapshot={},
                created_by="service-owner",
            )
        )
        workflow = task(
            db,
            episode,
            status="WAITING_REVIEW",
            pending_review={"resource_type": "script", "version_id": str(uuid4())},
        )
        workflow_id = str(workflow.id)
        # Historical decisions precede the actual outstanding journal row. They
        # must be removed by SQL before LIMIT, rather than after fetching a page.
        for _ in range(24):
            old = history_review(db, episode)
            db.add(
                HumanReviewDecision(
                    request_id=old.id,
                    actor_id="service-owner",
                    decision_json={"private": "receipt-secret"},
                )
            )
    response = client.get(endpoint(episode, "pending-reviews"))
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 5
    assert {item["source"] for item in page["items"]} == {
        "SCRIPT",
        "STORYBOARD",
        "OUTLINE",
        "WORKFLOW",
        "HUMAN_REVIEW",
    }
    assert {item["id"] for item in page["items"]} >= {manual_id, workflow_id, paused_id}
    assert sum(item["version_id"] == logged_id for item in page["items"]) == 1
    assert all(item["status"] == "WAITING_REVIEW" for item in page["items"])
    assert client.get(endpoint(episode, "pending-reviews"), params={"limit": 1}).json()["items"]
    assert all(
        secret not in response.text
        for secret in ("content_json", "snapshot_json", "receipt-secret")
    )


def test_journal_dedup_retains_different_exact_versions_on_same_run_and_reports_stale(
    client, episode
):
    with client.app.state.session_factory.begin() as db:
        workflow = task(db, episode, status="WAITING_REVIEW")
        script = Script(
            episode_id=UUID(episode["id"]),
            run_id=workflow.id,
            version_no=1,
            content_json={},
            status="READY_FOR_REVIEW",
        )
        db.add(script)
        db.flush()
        workflow.pending_review = {"resource_type": "script", "version_id": str(script.id)}
        journal = pause_for_content(db, script, "service-owner")
        journal_id, script_id, workflow_id = str(journal.id), str(script.id), str(workflow.id)
    url = endpoint(episode, "pending-reviews")
    value = client.get(url).json()
    assert value["total"] == 1 and value["items"][0]["source"] == "HUMAN_REVIEW"
    assert f"&run={workflow_id}" in value["items"][0]["detail_url"]
    with client.app.state.session_factory.begin() as db:
        db.get(WorkflowRun, UUID(workflow_id)).pending_review = {
            "resource_type": "storyboard",
            "version_id": str(uuid4()),
        }
    response = client.get(url)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["total"] == 2
    old = next(item for item in value["items"] if item["id"] == journal_id)
    assert old["version_id"] == script_id
    assert old["status"] == "BLOCKED" and old["blocking_code"] == "REVIEW_INPUT_CHANGED"


def test_pending_queue_reports_safe_unavailable_handler_and_archived_history(client, episode):
    with client.app.state.session_factory.begin() as db:
        row = history_review(db, episode)
        row_id = str(row.id)
    url = endpoint(episode, "pending-reviews")
    item = client.get(url).json()["items"][0]
    assert item["id"] == row_id
    assert item["status"] == "BLOCKED"
    assert item["blocking_code"] == "REVIEW_HANDLER_UNAVAILABLE"
    with client.app.state.session_factory.begin() as db:
        db.get(Episode, UUID(episode["id"])).status = "ARCHIVED"
    assert client.get(url).json()["items"][0]["blocking_code"] == "EPISODE_ARCHIVED"
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).archived = True
    assert client.get(url).json()["items"][0]["blocking_code"] == "PROJECT_ARCHIVED"


def test_exceptional_review_exposes_only_exact_project_asset_identity(
    client, episode, generation_seed
):
    with client.app.state.session_factory.begin() as db:
        version = AssetVersion(
            asset_id=generation_seed["target_asset_id"],
            version_no=1,
            object_key="private/internal-path",
            checksum="a" * 64,
            created_by="service-owner",
            metadata_json={"prompt": "private-system-prompt"},
        )
        db.add(version)
        db.flush()
        snapshot = ReviewSnapshot(
            project_id=UUID(episode["project_id"]),
            episode_id=UUID(episode["id"]),
            run_id=None,
            thread_id="manual:review",
            kind="EXCEPTIONAL_SHOT",
            object_id=uuid4(),
            version_id=version.id,
            content_sha256="a" * 64,
            input_sha256="b" * 64,
        ).model_dump(mode="json")
        request = history_review(
            db, episode, snapshot_json=snapshot, request_sha256=digest(snapshot)
        )
        request_id, version_id = str(request.id), str(version.id)
    response = client.get(endpoint(episode, "pending-reviews"))
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["asset_id"] == str(generation_seed["target_asset_id"])
    assert item["version_id"] == version_id and item["id"] == request_id
    assert item["object_id"] == snapshot["object_id"]
    assert item["object_id"] != item["asset_id"]
    assert f"asset_review={version_id}&review={request_id}" in item["detail_url"]
    assert "private" not in response.text and "metadata_json" not in response.text


def test_pending_legacy_versions_page_stably_in_database_without_reading_story(client, episode):
    stamp = datetime(2026, 1, 2, tzinfo=UTC)
    with client.app.state.session_factory.begin() as db:
        rows = [
            Script(
                episode_id=UUID(episode["id"]),
                version_no=index,
                content_json={"story": "private"},
                status="READY_FOR_REVIEW",
                created_at=stamp,
            )
            for index in range(1, 104)
        ]
        db.add_all(rows)
        db.flush()
        identities = sorted(str(row.id) for row in rows)
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(client.app.state.engine, "before_cursor_execute", capture)
    try:
        response = client.get(
            endpoint(episode, "pending-reviews"), params={"limit": 3, "offset": 100}
        )
    finally:
        event.remove(client.app.state.engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["total"] == 103
    assert [item["id"] for item in value["items"]] == identities[100:]
    assert not any("content_json" in sql or "input_json" in sql for sql in statements)
    assert any("LIMIT" in sql.upper() and "OFFSET" in sql.upper() for sql in statements)


@pytest.mark.parametrize("resource", ["tasks", "pending-reviews", "deliveries"])
def test_project_indexes_scope_episode_filter_and_membership(client, episode, resource):
    other = client.post("/api/v1/projects", json={"name": "Other project"}).json()
    foreign_episode = client.post(
        f"/api/v1/projects/{other['id']}/episodes",
        json={"episode_no": 1, "title": "Foreign episode"},
    ).json()
    url = endpoint(episode, resource)
    assert client.get(url, params={"episode_id": foreign_episode["id"]}).status_code == 404
    assert client.get(url, params={"episode_id": str(uuid4())}).status_code == 404
    assert client.get(url, params={"episode_id": "not-uuid"}).status_code == 422
    stranger = register(client, f"production-{resource}@example.com")
    client.headers["Authorization"] = "Bearer " + stranger["access_token"]
    assert client.get(url).status_code == 404


def test_viewer_can_read_all_indexes_without_write_authority(client, episode):
    owner = client.headers["Authorization"]
    viewer = register(client, "production-viewer@example.com")
    client.headers["Authorization"] = owner
    member = client.post(
        f"/api/v1/projects/{episode['project_id']}/members",
        json={"email": viewer["user"]["email"], "role": "viewer"},
    )
    assert member.status_code == 201, member.text
    client.headers["Authorization"] = "Bearer " + viewer["access_token"]
    for resource in ("tasks", "pending-reviews", "deliveries"):
        response = client.get(endpoint(episode, resource))
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 0


def test_real_renderer_task_and_cross_episode_delivery_index_preserve_existing_acceptance(
    client,
    episode,
    job_case,
):
    _, job, _ = job_case
    task_page = client.get(endpoint(episode, "tasks"), params={"task_type": "RENDER"})
    assert task_page.status_code == 200, task_page.text
    assert task_page.json()["total"] == 1
    summary = task_page.json()["items"][0]
    assert summary["id"] == job["id"] and summary["family"] == "RENDER"
    assert summary["episode_id"] == episode["id"]
    assert run(client, job) == "SUCCEEDED"
    finished = read(client, job)
    existing = client.get(f"/api/v1/episodes/{episode['id']}/deliveries").json()[0]
    response = client.get(endpoint(episode, "deliveries"))
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 1
    delivery = page["items"][0]
    assert delivery["id"] == finished["artifact_id"]
    assert all(delivery[field] == value for field, value in existing.items())
    assert delivery["episode_no"] == episode["episode_no"]
    assert delivery["episode_title"] == episode["title"]
    assert delivery["is_delivery"] is False
    assert f"delivery={delivery['id']}" in delivery["detail_url"]
    second = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "Second"},
    ).json()
    assert (
        client.get(endpoint(episode, "deliveries"), params={"episode_id": second["id"]}).json()[
            "total"
        ]
        == 0
    )
    assert client.get(endpoint(episode, "deliveries"), params={"offset": 1}).json()["items"] == []
    with client.app.state.session_factory.begin() as db:
        db.get(Episode, UUID(episode["id"])).status = "ARCHIVED"
    assert client.get(endpoint(episode, "deliveries")).json()["total"] == 1
