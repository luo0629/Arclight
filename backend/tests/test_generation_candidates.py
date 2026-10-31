"""GEN-12: real output bytes, per-image history, durable retries and fail-closed review."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from test_asset_selection import approved, choose, current
from test_asset_versions import image_bytes, private_objects, state, versions  # noqa: F401
from test_assets import create, rename
from test_collaboration import create_project, register
from test_concurrency import require_postgres
from test_file_transfers import transfer_setup  # noqa: F401
from test_generation_delivery import deliver
from test_generation_jobs import base, capture_ok, payload
from test_generation_reconciliation import QueryFake, query, unknown
from test_upload_completion import complete, ticket

__all__ = ["transfer_setup"]

from arclight.application.generation_candidates import import_attempt, pending_attempts
from arclight.core.errors import DomainError
from arclight.domain.generation_requests import GenerationResult
from arclight.persistence.models import (
    AssetVersion,
    DependencyOutput,
    GenerationAttempt,
    ProjectMember,
    StoredFile,
    VersionBinding,
    VisualAsset,
)
from arclight.providers.media_outputs import MediaOutput, MockMediaOutputProvider
from arclight.providers.media_query import QueryReceipt
from arclight.providers.media_submission import MockMediaSubmissionProvider


def generate(client, project_id, target=None, count=1):
    body = payload(target)
    body["spec"]["batch_size"] = count
    body["spec"]["output_spec"] = {"width": 8, "height": 12, "format": "png"}
    job = capture_ok(client, project_id, body)
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    return job, attempt


def ingest(client, attempt, provider=None):
    return import_attempt(
        client.app.state.session_factory,
        client.app.state.settings,
        attempt,
        provider or MockMediaOutputProvider(),
    )


def candidates(client, project_id, job):
    response = client.get(base(project_id) + f"/{job['id']}/candidates")
    assert response.status_code == 200, response.text
    return response.json()


def test_batch_import_real_bytes_history_versions_bindings_and_no_selection(client, episode):
    project_id = episode["project_id"]
    asset = create(client, project_id)
    job, attempt = generate(client, project_id, asset, 3)
    assert pending_attempts(client.app.state.session_factory) == [attempt]
    assert ingest(client, attempt) == 3
    rows = candidates(client, project_id, job)
    assert len(rows) == 3 and {r["ordinal"] for r in rows} == {0, 1, 2}
    assert all(r["error_code"] is None and r["required_review"] == "CONTINUITY" for r in rows)
    assert len({r["asset_version_id"] for r in rows}) == 3
    assert pending_attempts(client.app.state.session_factory) == []
    assert ingest(client, attempt) == 0
    assert candidates(client, project_id, job) == rows
    assert current(client, asset)["current_version_id"] is None
    for row in rows:
        response = client.get(
            f"/api/v1/projects/{project_id}/generation-candidates/{row['id']}/image"
        )
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "private, no-store"
    with client.app.state.session_factory() as db:
        saved = list(db.scalars(select(AssetVersion).order_by(AssetVersion.version_no)))
        assert [v.version_no for v in saved] == [1, 2, 3]
        assert all(v.status == "DRAFT" and not v.stale for v in saved)
        assert db.scalar(select(func.count()).select_from(StoredFile)) == 3
        assert db.scalar(select(func.count()).select_from(DependencyOutput)) == 3
        bindings = list(
            db.scalars(
                select(VersionBinding).where(VersionBinding.resource_type == "ASSET_VERSION")
            )
        )
        assert {v.resource_id for v in bindings} == {v.id for v in saved}
        assert all(str(attempt) == v.metadata_json["generation_attempt_id"] for v in saved)
    page = client.get(base(project_id) + f"/{job['id']}/candidates?limit=1&offset=1")
    assert page.json() == rows[1:2]


def test_untargeted_shot_creates_only_candidate_asset_at_import(client, episode):
    job, attempt = generate(client, episode["project_id"], count=2)
    with client.app.state.session_factory() as db:
        assert db.scalar(select(VisualAsset)) is None
    assert ingest(client, attempt) == 2
    with client.app.state.session_factory() as db:
        asset = db.scalar(select(VisualAsset))
        assert asset.asset_type == "SHOT_IMAGE" and asset.current_version_id is None
        assert db.scalar(select(func.count()).select_from(VisualAsset)) == 1
    assert len(candidates(client, episode["project_id"], job)) == 2


class OutputFake:
    name = "mock"

    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = 0

    def download(self, request, handle, max_bytes):
        self.calls += 1
        output = self.outputs[int(handle.rsplit("/", 1)[1])]
        if isinstance(output, Exception):
            raise output
        return output


@pytest.mark.parametrize(
    "bad,code",
    [
        (MediaOutput(b"broken", "image/png"), "INVALID_IMAGE"),
        (MediaOutput(image_bytes()[:-16], "image/png"), "INVALID_IMAGE"),
        (MediaOutput(image_bytes(size=(9, 12)), "image/png"), "GENERATION_OUTPUT_MISMATCH"),
        (MediaOutput(image_bytes(), "image/jpeg"), "INVALID_IMAGE"),
        (MediaOutput(b"x" * (1024 * 1024 + 1), "image/png"), "INVALID_IMAGE"),
    ],
)
def test_one_invalid_output_retains_valid_siblings_and_immutable_failure(
    client, episode, bad, code
):
    job, attempt = generate(client, episode["project_id"], count=2)
    provider = OutputFake([MediaOutput(image_bytes(), "image/png"), bad])
    assert ingest(client, attempt, provider) == 2
    rows = candidates(client, episode["project_id"], job)
    assert len([r for r in rows if r["asset_version_id"]]) == 1
    assert [r["error_code"] for r in rows if not r["asset_version_id"]] == [code]
    assert ingest(client, attempt, provider) == 0 and provider.calls == 2
    assert pending_attempts(client.app.state.session_factory) == []


def test_temporary_download_retries_without_new_generation_or_duplicate_versions(client, episode):
    job, attempt = generate(client, episode["project_id"], count=2)
    provider = OutputFake([TimeoutError(), MediaOutput(image_bytes(), "image/png")])
    assert ingest(client, attempt, provider) == 1
    assert pending_attempts(client.app.state.session_factory) == [attempt]
    assert ingest(client, attempt) == 1
    assert pending_attempts(client.app.state.session_factory) == []
    assert len(candidates(client, episode["project_id"], job)) == 2
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationAttempt)) == 1


def test_storage_rollback_keeps_import_pending_and_cleans_objects(client, episode, monkeypatch):
    from arclight.application import generation_candidates

    job, attempt = generate(client, episode["project_id"])
    original = generation_candidates.record_asset_file

    def fail(*args):
        raise DomainError("ASSET_STORAGE_UNAVAILABLE", "test rollback", 503)

    monkeypatch.setattr(generation_candidates, "record_asset_file", fail)
    with pytest.raises(DomainError):
        ingest(client, attempt)
    assert candidates(client, episode["project_id"], job) == []
    assert list(client.app.state.settings.asset_storage_path.rglob("*.image")) == []
    monkeypatch.setattr(generation_candidates, "record_asset_file", original)
    assert ingest(client, attempt) == 1


def test_review_gate_prevents_ready_approval_and_restore_laundering(client, episode):
    asset = create(client, episode["project_id"])
    job, attempt = generate(client, episode["project_id"], asset)
    assert ingest(client, attempt) == 1
    version = client.get(versions(asset)).json()[0]
    assert (
        state(client, asset, version, "READY_FOR_REVIEW").json()["error"]["code"]
        == "CONTINUITY_REVIEW_REQUIRED"
    )
    assert current(client, asset)["current_version_id"] is None
    # A restore requires a new confirmed input manifest, and retains the gate.
    from arclight.application.generation_candidates import require_candidate_review

    with client.app.state.session_factory() as db:
        row = db.get(AssetVersion, UUID(version["id"]))
        with pytest.raises(DomainError, match="连续性"):
            require_candidate_review(row)
    response = client.post(
        versions(asset) + f"/{version['id']}/restore-as-new",
        json={
            "creation_key": str(uuid4()),
            "expected_revision": current(client, asset)["revision"],
            "comment": "restore",
        },
    )
    assert response.status_code == 409
    assert len(candidates(client, episode["project_id"], job)) == 1


def test_candidate_api_project_isolation(client, episode):
    job, attempt = generate(client, episode["project_id"])
    assert ingest(client, attempt) == 1
    row = candidates(client, episode["project_id"], job)[0]
    other = client.post("/api/v1/projects", json={"name": "other"}).json()["id"]
    assert client.get(base(other) + f"/{job['id']}/candidates").status_code == 404
    assert (
        client.get(f"/api/v1/projects/{other}/generation-candidates/{row['id']}/image").status_code
        == 404
    )


@pytest.mark.parametrize("role", ["viewer", "outsider"])
def test_candidate_read_and_replacement_permissions(client, role):
    owner = register(client)
    project = create_project(client)
    job, attempt = generate(client, project["id"])
    assert ingest(client, attempt) == 1
    row = candidates(client, project["id"], job)[0]
    member = register(client, role + "@example.com")
    if role == "viewer":
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(project["id"]),
                    user_id=UUID(member["user"]["id"]),
                    role="viewer",
                )
            )
    root = f"/api/v1/projects/{project['id']}/generation-candidates/{row['id']}"
    assert client.get(root + "/image").status_code == (200 if role == "viewer" else 404)
    assert client.post(
        root + "/replacements",
        json={"file_id": str(uuid4()), "creation_key": str(uuid4()), "expected_revision": 1},
    ).status_code == (403 if role == "viewer" else 404)
    assert owner["user"]["id"] != member["user"]["id"]


def test_late_results_preserve_existing_selection_and_mark_target_change(client, episode):
    asset = create(client, episode["project_id"])
    previous = approved(client, asset)
    asset = choose(client, current(client, asset), previous).json()
    job, attempt = generate(client, episode["project_id"], asset, 2)
    assert rename(client, current(client, asset), "changed after generation").status_code == 200
    assert ingest(client, attempt) == 2
    now = current(client, asset)
    assert now["current_version_id"] == previous["id"] and now["status"] == asset["status"]
    assert all(r["version"]["stale"] for r in candidates(client, episode["project_id"], job))


def test_restore_new_copy_retains_review_gate_and_cannot_select(client, episode):
    project_id = episode["project_id"]
    reference = create(client, project_id)
    ref_version = approved(client, reference)
    asset = create(client, project_id)
    body = payload(
        asset,
        inputs=[
            {
                "source_asset_version_id": ref_version["id"],
                "dependency_type": "REFERENCE_DEPENDENCY",
            }
        ],
    )
    body["spec"]["output_spec"] = {"width": 8, "height": 12, "format": "png"}
    job = capture_ok(client, project_id, body)
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(select(GenerationAttempt.id))
    assert ingest(client, attempt) == 1
    row = candidates(client, project_id, job)[0]["version"]
    root = versions(asset) + f"/{row['id']}"
    rebuilt = client.post(
        root + "/rebuild-manifest",
        json={
            "creation_key": str(uuid4()),
            "expected_revision": row["revision"],
            "replacements": {},
        },
    )
    assert rebuilt.status_code == 201, rebuilt.text
    restored = client.post(
        root + "/restore-as-new",
        json={
            "creation_key": str(uuid4()),
            "expected_revision": current(client, asset)["revision"],
            "comment": "copy candidate",
            "dependency_manifest_id": rebuilt.json()["id"],
        },
    )
    assert restored.status_code == 201, restored.text
    copy = restored.json()
    assert copy["required_review"] == "CONTINUITY"
    assert state(client, asset, copy, "READY_FOR_REVIEW").status_code == 409
    assert (
        choose(client, current(client, asset), copy).json()["error"]["code"]
        == "CONTINUITY_REVIEW_REQUIRED"
    )


def test_real_worker_outbox_imports_without_resubmitting(client, episode, settings, monkeypatch):
    from arclight.workers import tasks

    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    job, attempt = generate(client, episode["project_id"])
    messages = []
    monkeypatch.setattr(
        tasks.import_generation_candidates, "apply_async", lambda *, args: messages.append(args)
    )
    tasks.dispatch_outbox()
    tasks.dispatch_outbox()
    assert messages == [[str(attempt)], [str(attempt)]]
    assert tasks.import_generation_candidates(*messages[0]) == 1
    assert tasks.import_generation_candidates(*messages[1]) == 0
    tasks.dispatch_outbox()
    assert len(messages) == 2
    assert len(candidates(client, episode["project_id"], job)) == 1


def test_reconciled_success_uses_the_same_durable_candidate_import(client, episode):
    job, attempt = unknown(client, episode["project_id"])
    assert pending_attempts(client.app.state.session_factory) == []
    receipt = QueryReceipt(
        status="SUCCEEDED", result=GenerationResult(output_handles=[f"mock/image/{attempt.id}/0"])
    )
    response = query(client, job, QueryFake(receipt))
    assert response.status_code == 200, response.text
    assert pending_attempts(client.app.state.session_factory) == [attempt.id]
    assert ingest(client, attempt.id) == 1
    assert len(candidates(client, episode["project_id"], job)) == 1


def test_output_fetch_runs_outside_business_locks(client, episode):
    require_postgres(client.app.state.settings)
    job, attempt = generate(client, episode["project_id"])

    class UnlockedOutput(MockMediaOutputProvider):
        def download(self, request, handle, max_bytes):
            with client.app.state.session_factory.begin() as db:
                from arclight.persistence.models import GenerationJob, Project

                assert (
                    db.scalar(
                        select(Project)
                        .where(Project.id == UUID(episode["project_id"]))
                        .with_for_update(nowait=True)
                    )
                    is not None
                )
                assert (
                    db.scalar(
                        select(GenerationJob)
                        .where(GenerationJob.id == UUID(job["id"]))
                        .with_for_update(nowait=True)
                    )
                    is not None
                )
            return super().download(request, handle, max_bytes)

    assert ingest(client, attempt, UnlockedOutput()) == 1


@pytest.mark.parametrize("timezone", ["Asia/Shanghai", "America/New_York"])
def test_non_utc_database_candidate_and_job_responses_are_stable(client, episode, timezone):
    require_postgres(client.app.state.settings)
    engine = client.app.state.engine

    def set_timezone(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('TimeZone', %s, false)", (timezone,))

    engine.dispose()
    event.listen(engine, "connect", set_timezone)
    try:
        asset = create(client, episode["project_id"])
        job, attempt = generate(client, episode["project_id"], asset)
        assert ingest(client, attempt) == 1
        response = candidates(client, episode["project_id"], job)[0]
        assert response["created_at"].endswith("Z")
        assert response["version"]["created_at"].endswith("Z")
        assert response["version"]["updated_at"].endswith("Z")
        current_job = client.get(base(episode["project_id"]) + f"/{job['id']}").json()
        assert job["created_at"] == current_job["created_at"]
        assert current_job["created_at"].endswith("Z")
        dispatch = client.get(base(episode["project_id"]) + f"/{job['id']}/dispatches").json()[0]
        assert dispatch["finished_at"].endswith("Z")
    finally:
        event.remove(engine, "connect", set_timezone)
        engine.dispose()


def test_upload_replacement_is_explicit_immutable_and_requires_new_review(client, transfer_setup):
    settings, root = transfer_setup
    project_id = root.rsplit("/", 1)[1]
    asset = create(client, project_id)
    job, attempt = generate(client, project_id, asset)
    assert ingest(client, attempt) == 1
    source = candidates(client, project_id, job)[0]
    upload = ticket(client, root)
    file = complete(client, root, upload).json()
    body = {
        "file_id": file["id"],
        "creation_key": str(uuid4()),
        "expected_revision": current(client, asset)["revision"],
    }
    endpoint = root + f"/generation-candidates/{source['id']}/replacements"
    response = client.post(endpoint, json=body)
    assert response.status_code == 201, response.text
    replacement = response.json()
    assert replacement["source"] == "REPLACEMENT"
    assert replacement["replaces_candidate_id"] == source["id"]
    assert replacement["asset_version_id"] != source["asset_version_id"]
    assert client.post(endpoint, json=body).json() == replacement
    assert client.post(endpoint, json={**body, "file_id": str(uuid4())}).status_code == 409
    assert client.post(endpoint, json={**body, "creation_key": str(uuid4())}).status_code == 409
    rows = client.get(versions(asset)).json()
    assert len(rows) == 2 and all(r["status"] == "DRAFT" for r in rows)
    assert state(client, asset, rows[1], "READY_FOR_REVIEW").status_code == 409
    assert current(client, asset)["current_version_id"] is None
    assert client.get(root + f"/generation-candidates/{source['id']}/image").status_code == 200
    assert (
        client.get(root + f"/generation-candidates/{replacement['id']}/image").content
        == image_bytes()
    )


def test_history_database_guards_and_downgrade_refusal(client, episode, migrated):
    job, attempt = generate(client, episode["project_id"])
    assert ingest(client, attempt) == 1
    statements = [
        "UPDATE generation_candidates SET source='REPLACEMENT'",
        "DELETE FROM generation_candidates",
        "UPDATE generation_image_imports SET output_count=1",
        "DELETE FROM generation_image_imports",
    ]
    if client.app.state.engine.dialect.name == "postgresql":
        statements += ["TRUNCATE generation_candidates", "TRUNCATE generation_image_imports"]
    else:
        statements += [
            "INSERT OR REPLACE INTO generation_candidates SELECT * FROM generation_candidates",
            "INSERT OR REPLACE INTO generation_image_imports "
            "SELECT * FROM generation_image_imports",
        ]
    for statement in statements:
        with pytest.raises(DBAPIError), client.app.state.engine.begin() as db:
            db.execute(text(statement))
    with pytest.raises(RuntimeError, match="Generation history"):
        command.downgrade(migrated, "0035")
    assert len(candidates(client, episode["project_id"], job)) == 1


@pytest.mark.parametrize("mode", ["LOCKED", "ARCHIVED"])
def test_results_preserved_after_target_becomes_read_only(client, episode, mode):
    asset = create(client, episode["project_id"])
    previous = approved(client, asset)
    asset = choose(client, current(client, asset), previous).json()
    job, attempt = generate(client, episode["project_id"], asset)
    if mode == "LOCKED":
        response = client.post(
            versions(asset) + f"/{previous['id']}/lock",
            json={
                "expected_revision": asset["revision"],
                "expected_lock_version": asset["lock_version"],
            },
        )
        assert response.status_code == 200, response.text
    else:
        from test_assets import archive

        assert archive(client, asset).status_code == 200
    assert ingest(client, attempt) == 1
    now = current(client, asset)
    assert now["status"] == mode and now["current_version_id"] == previous["id"]
    assert len(candidates(client, episode["project_id"], job)) == 1


@pytest.mark.parametrize("duplicate", [True, False])
def test_real_database_replacement_replay_or_cas(client, transfer_setup, duplicate):
    require_postgres(client.app.state.settings)
    _, root = transfer_setup
    project_id = root.rsplit("/", 1)[1]
    asset = create(client, project_id)
    job, attempt = generate(client, project_id, asset)
    assert ingest(client, attempt) == 1
    source = candidates(client, project_id, job)[0]
    file = complete(client, root, ticket(client, root)).json()
    payloads = [
        {
            "file_id": file["id"],
            "creation_key": str(uuid4()),
            "expected_revision": current(client, asset)["revision"],
        }
    ]
    payloads.append(
        {**payloads[0], "creation_key": payloads[0]["creation_key"] if duplicate else str(uuid4())}
    )
    barrier = Barrier(2)

    def replace(body):
        barrier.wait(timeout=10)
        return client.post(root + f"/generation-candidates/{source['id']}/replacements", json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(replace, payloads))
    assert sorted(r.status_code for r in responses) == ([201, 201] if duplicate else [201, 409])
    if duplicate:
        assert responses[0].json()["id"] == responses[1].json()["id"]
    assert len(candidates(client, project_id, job)) == 2
    assert current(client, asset)["current_version_id"] is None


def test_real_database_duplicate_imports_have_one_outcome_per_image(client, episode):
    require_postgres(client.app.state.settings)
    job, attempt = generate(client, episode["project_id"], count=2)
    barrier = Barrier(2)

    class ParallelOutput(MockMediaOutputProvider):
        def download(self, request, handle, max_bytes):
            if handle.endswith("/0"):
                barrier.wait(timeout=15)
            return super().download(request, handle, max_bytes)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ingest, client, attempt, ParallelOutput()) for _ in range(2)]
        assert sum(f.result(timeout=45) for f in futures) == 2
    assert len(candidates(client, episode["project_id"], job)) == 2
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AssetVersion)) == 2
