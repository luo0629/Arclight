"""QA-01: real media, exact references, atomic outcomes and preserved history."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_asset_selection import approved, current
from test_asset_versions import image_bytes, state, versions
from test_assets import create, rename
from test_concurrency import require_postgres
from test_file_transfers import transfer_setup  # noqa: F401
from test_generation_candidates import OutputFake, candidates, generate, ingest
from test_generation_delivery import deliver
from test_generation_jobs import capture_ok, payload
from test_upload_completion import complete, ticket

from arclight.application.candidate_checks import (
    check_candidate_media,
    require_candidate_media_check,
)
from arclight.application.production_versions import digest
from arclight.core.errors import DomainError
from arclight.domain.asset_versions import decode_image
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    GenerationAttempt,
    GenerationCandidate,
    GenerationJob,
)
from arclight.providers.media_outputs import MediaOutput, MockMediaOutputProvider
from arclight.providers.media_submission import MockMediaSubmissionProvider

__all__ = ["transfer_setup"]


@pytest.mark.parametrize(
    "kind,media_type,size", [("PNG", "image/png", (8, 12)), ("JPEG", "image/jpeg", (32, 24))]
)
def test_real_checked_bytes_and_actual_dimensions_are_bound_without_granting_review(
    client, episode, kind, media_type, size
):
    project_id = episode["project_id"]
    asset = create(client, project_id)
    job, attempt = generate(client, project_id, asset)
    data = image_bytes(kind, size)
    assert ingest(client, attempt, OutputFake([MediaOutput(data, media_type)])) == 1
    row = candidates(client, project_id, job)[0]
    check = row["media_check"]
    assert check["status"] == "PASSED" and check["references_valid"] is True
    assert check["candidate_id"] == row["id"]
    assert check["input_manifest_id"] == row["input_manifest_id"]
    assert check["image"] == {
        "checksum": hashlib.sha256(data).hexdigest(),
        "media_type": media_type,
        "byte_size": len(data),
        "width": size[0],
        "height": size[1],
    }
    with client.app.state.session_factory() as db:
        manifest = db.get(DependencyManifest, UUID(row["input_manifest_id"]))
        assert check["manifest_sha256"] == digest(manifest.snapshot_json)
        assert (
            require_candidate_media_check(db, UUID(project_id), UUID(row["id"])).status == "PASSED"
        )
    assert (
        state(client, asset, row["version"], "READY_FOR_REVIEW").json()["error"]["code"]
        == "CONTINUITY_REVIEW_REQUIRED"
    )
    assert current(client, asset)["current_version_id"] is None


@pytest.mark.parametrize(
    "bad",
    [
        MediaOutput(b"broken", "image/png"),
        MediaOutput(image_bytes()[:-16], "image/png"),
        MediaOutput(image_bytes(), "image/jpeg"),
        MediaOutput(b"x" * (1024 * 1024 + 1), "image/png"),
        MediaOutput(image_bytes(size=(4097, 1)), "image/png"),
        MediaOutput(image_bytes(size=(2001, 2000)), "image/png"),
    ],
)
def test_invalid_siblings_have_immutable_failed_checks_and_cannot_enter_review(
    client, episode, bad
):
    project_id = episode["project_id"]
    job, attempt = generate(client, project_id, count=2)
    provider = OutputFake([MediaOutput(image_bytes(), "image/png"), bad])
    assert ingest(client, attempt, provider) == 2
    before = candidates(client, project_id, job)
    assert sorted(r["media_check"]["status"] for r in before) == ["FAILED", "PASSED"]
    failed = next(r for r in before if r["error_code"])
    assert failed["asset_version_id"] is None and failed["version"] is None
    assert failed["media_check"]["error_code"] == failed["error_code"] == "INVALID_IMAGE"
    with client.app.state.session_factory() as db:
        with pytest.raises(DomainError, match="媒体检查"):
            require_candidate_media_check(db, UUID(project_id), UUID(failed["id"]))
        assert len(list(db.scalars(select(AssetVersion)))) == 1
    assert ingest(client, attempt, provider) == 0 and provider.calls == 2
    assert candidates(client, project_id, job) == before


@pytest.mark.parametrize(
    "changed",
    [
        {"candidate_id": str(uuid4())},
        {"input_manifest_id": str(uuid4())},
        {"manifest_sha256": "0" * 64},
        {"image": None},
        {"status": "FAILED", "error_code": "INVALID_IMAGE"},
        {"validator_version": "untrusted/99"},
        {"internal_reasoning": "untrusted"},
    ],
)
def test_review_admission_rejects_misattributed_or_incomplete_evidence(client, episode, changed):
    project_id = episode["project_id"]
    job, attempt = generate(client, project_id)
    assert ingest(client, attempt) == 1
    before = candidates(client, project_id, job)
    candidate_id = UUID(before[0]["id"])
    with client.app.state.session_factory() as db:
        row = db.get(GenerationCandidate, candidate_id)
        # Simulate an untrusted report in memory; whole-row DB guards stay active.
        row.media_check_json.update(changed)
        assert row not in db.dirty
        with pytest.raises(DomainError, match="媒体检查"):
            require_candidate_media_check(db, UUID(project_id), candidate_id)
    assert candidates(client, project_id, job) == before


def test_storage_rollback_does_not_publish_a_check_or_candidate(client, episode, monkeypatch):
    from arclight.application import generation_candidates as imports

    project_id = episode["project_id"]
    job, attempt = generate(client, project_id)
    original = imports.record_asset_file

    def unavailable(*args):
        raise DomainError("ASSET_STORAGE_UNAVAILABLE", "测试存储故障", 503)

    monkeypatch.setattr(imports, "record_asset_file", unavailable)
    with pytest.raises(DomainError):
        ingest(client, attempt)
    assert candidates(client, project_id, job) == []
    with client.app.state.session_factory() as db:
        assert list(db.scalars(select(AssetVersion))) == []
    monkeypatch.setattr(imports, "record_asset_file", original)
    assert ingest(client, attempt) == 1
    assert candidates(client, project_id, job)[0]["media_check"]["status"] == "PASSED"


@pytest.mark.parametrize(
    "changed", ["checksum", "asset_id", "version_no", "source_asset_version_id"]
)
def test_reference_identity_is_checked_by_the_original_dependency_service(client, episode, changed):
    import base64

    project_id = episode["project_id"]
    source = create(client, project_id)
    version = approved(client, source)
    job = capture_ok(
        client,
        project_id,
        payload(
            inputs=[
                {
                    "source_asset_version_id": version["id"],
                    "dependency_type": "REFERENCE_DEPENDENCY",
                }
            ]
        ),
    )
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory.begin() as db:
        job_row = db.get(GenerationJob, UUID(job["id"]))
        attempt = db.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job_row.id))
        original = db.get(DependencyManifest, job_row.input_manifest_id)
        snapshot = deepcopy(original.snapshot_json)
        snapshot["inputs"][0][changed] = (
            999
            if changed == "version_no"
            else ("0" * 64 if changed == "checksum" else str(uuid4()))
        )
        invalid = DependencyManifest(
            project_id=UUID(project_id),
            target_asset_id=original.target_asset_id,
            creation_key=uuid4(),
            request_fingerprint=digest(snapshot),
            snapshot_json=snapshot,
            created_by="qa-fixture",
        )
        db.add(invalid)
        db.flush()
        # Detached input objects simulate a malformed producer without mutating real history.
        db.expunge(job_row)
        db.expunge(attempt)
        job_row.input_manifest_id = attempt.input_manifest_id = invalid.id
        image = decode_image(base64.b64encode(image_bytes()).decode(), "image/png")
        result = check_candidate_media(db, job_row, attempt, uuid4(), image)
        assert result.status == "FAILED" and result.references_valid is False
        assert result.error_code in {"NOT_FOUND", "INVALID_DEPENDENCY_INPUT"}
        assert original.snapshot_json != snapshot


def test_reference_updates_keep_original_check_and_exact_bound_version(client, episode):
    project_id = episode["project_id"]
    source = create(client, project_id)
    version = approved(client, source)
    job = capture_ok(
        client,
        project_id,
        payload(
            inputs=[
                {
                    "source_asset_version_id": version["id"],
                    "dependency_type": "REFERENCE_DEPENDENCY",
                }
            ]
        ),
    )
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    assert ingest(client, attempt) == 1
    before = candidates(client, project_id, job)
    assert rename(client, current(client, source), "更新名称").status_code == 200
    approved(client, current(client, source))
    assert candidates(client, project_id, job) == before
    with client.app.state.session_factory() as db:
        assert (
            require_candidate_media_check(db, UUID(project_id), UUID(before[0]["id"])).status
            == "PASSED"
        )


def test_upload_replacement_gets_a_new_exact_check_and_keeps_the_original(client, transfer_setup):
    _, root = transfer_setup
    project_id = root.rsplit("/", 1)[1]
    asset = create(client, project_id)
    job, attempt = generate(client, project_id, asset)
    assert ingest(client, attempt) == 1
    source = candidates(client, project_id, job)[0]
    data = image_bytes(size=(24, 16), color="red")
    file = complete(client, root, ticket(client, root, data=data)).json()
    body = {
        "file_id": file["id"],
        "creation_key": str(uuid4()),
        "expected_revision": current(client, asset)["revision"],
    }
    endpoint = root + f"/generation-candidates/{source['id']}/replacements"
    response = client.post(endpoint, json=body)
    assert response.status_code == 201, response.text
    replacement = response.json()
    assert replacement["media_check"]["candidate_id"] == replacement["id"] != source["id"]
    assert replacement["media_check"]["image"]["checksum"] == hashlib.sha256(data).hexdigest()
    assert replacement["media_check"]["image"]["width"] == 24
    assert client.post(endpoint, json=body).json() == replacement
    assert next(r for r in candidates(client, project_id, job) if r["id"] == source["id"]) == source
    assert len(client.get(versions(asset)).json()) == 2
    assert state(client, asset, replacement["version"], "READY_FOR_REVIEW").status_code == 409


def test_check_history_is_protected_and_downgrade_refuses_evidence_loss(client, episode, migrated):
    project_id = episode["project_id"]
    job, attempt = generate(client, project_id)
    assert ingest(client, attempt) == 1
    before = candidates(client, project_id, job)
    statements = [
        "UPDATE generation_candidates SET media_check_json=NULL",
        "DELETE FROM generation_candidates",
    ]
    if client.app.state.engine.dialect.name == "postgresql":
        statements.append("TRUNCATE generation_candidates")
    else:
        statements.append(
            "INSERT OR REPLACE INTO generation_candidates SELECT * FROM generation_candidates"
        )
    for statement in statements:
        with pytest.raises(DBAPIError), client.app.state.engine.begin() as db:
            db.execute(text(statement))
    with pytest.raises(RuntimeError, match="Generation history contains media checks"):
        command.downgrade(migrated, "0050")
    assert candidates(client, project_id, job) == before


def test_upgrade_keeps_legacy_evidence_missing_and_never_fabricates_a_pass(
    client, episode, migrated
):
    project_id = episode["project_id"]
    job, attempt = generate(client, project_id)
    command.downgrade(migrated, "0050")
    candidate_id = uuid4()
    with client.app.state.engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO generation_candidates "
                "(id,created_at,updated_at,project_id,job_id,attempt_id,input_manifest_id,ordinal,source,"
                "output_handle,creation_key,request_fingerprint,error_code,created_by) VALUES "
                "(:id,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,:project,:job,:attempt,:manifest,0,"
                "'GENERATED','legacy/output',:creation,:fingerprint,'INVALID_IMAGE','legacy-worker')"
            ),
            {
                "id": candidate_id.hex,
                "project": UUID(project_id).hex,
                "job": UUID(job["id"]).hex,
                "attempt": attempt.hex,
                "manifest": UUID(job["input_manifest_id"]).hex,
                "creation": uuid4().hex,
                "fingerprint": "0" * 64,
            },
        )
    command.upgrade(migrated, "head")
    row = candidates(client, project_id, job)[0]
    assert row["id"] == str(candidate_id) and row["media_check"] is None
    assert row["created_by"] == "legacy-worker" and row["error_code"] == "INVALID_IMAGE"
    with client.app.state.session_factory() as db, pytest.raises(DomainError, match="缺少有效"):
        require_candidate_media_check(db, UUID(project_id), candidate_id)


def test_concurrent_delivery_keeps_one_check_per_output(client, episode):
    require_postgres(client.app.state.settings)
    project_id = episode["project_id"]
    job, attempt = generate(client, project_id, count=2)
    barrier = Barrier(2)

    class ParallelOutput(MockMediaOutputProvider):
        def download(self, request, handle, max_bytes):
            if handle.endswith("/0"):
                barrier.wait(timeout=15)
            return super().download(request, handle, max_bytes)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ingest, client, attempt, ParallelOutput()) for _ in range(2)]
        assert sum(f.result(timeout=45) for f in futures) == 2
    rows = candidates(client, project_id, job)
    assert len(rows) == 2
    assert len({r["media_check"]["candidate_id"] for r in rows}) == 2
    with client.app.state.session_factory() as db:
        for row in rows:
            assert (
                require_candidate_media_check(db, UUID(project_id), UUID(row["id"])).status
                == "PASSED"
            )
