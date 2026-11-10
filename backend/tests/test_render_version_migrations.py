"""Render version migration and publication contracts hold in both databases."""

from contextlib import contextmanager
from copy import deepcopy
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest
from alembic import command
from alembic.config import Config
from render_job_helpers import job_case, read, render_case, review_case, run, timeline_case
from render_manifest_helpers import result_case
from sqlalchemy import event, func, inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from test_concurrency import require_postgres
from test_render_inspection_migrations import measured_result, output_file

from arclight.application import render_versions
from arclight.application.render_leases import claim
from arclight.application.render_manifests import checked_snapshot
from arclight.domain.render_inspections import RenderInspectionResult
from arclight.persistence.models import (
    AssetVersion,
    RenderArtifact,
    RenderAttempt,
    RenderExecution,
    RenderInspection,
    RenderJob,
    RenderManifest,
    StoredFile,
)

__all__ = [
    "job_case",
    "measured_result",
    "render_case",
    "result_case",
    "review_case",
    "timeline_case",
]


@pytest.fixture
def pending_registration(client, job_case, measured_result):
    """Persist a real measured receipt; do not publish or fabricate an output object."""
    manifest, value, _ = job_case
    factory = client.app.state.session_factory
    token = claim(factory, UUID(value["id"]))
    assert token is not None
    with factory.begin() as db:
        db.get(RenderAttempt, token).input_sha256 = "7" * 64
        db.add(
            RenderInspection(
                attempt_id=token,
                manifest_id=UUID(manifest["id"]),
                manifest_sha256=manifest["content_sha256"],
                input_sha256="7" * 64,
                output_sha256=measured_result["output_sha256"],
                byte_size=measured_result["byte_size"],
                passed=True,
                result_json=deepcopy(measured_result),
            )
        )
    return manifest, value, token, RenderInspectionResult.model_validate(measured_result)


@contextmanager
def altered_insert(model, change):
    def mutate(mapper, connection, target):
        change(target)

    event.listen(model, "before_insert", mutate)
    try:
        yield
    finally:
        event.remove(model, "before_insert", mutate)


def register_candidate(client, pending):
    manifest, value, token, result = pending
    with client.app.state.session_factory.begin() as db:
        return render_versions.register_render_version(
            db,
            client.app.state.settings,
            db.get(RenderExecution, UUID(value["id"])),
            db.get(RenderJob, UUID(value["id"])),
            db.get(RenderAttempt, token),
            checked_snapshot(db.get(RenderManifest, UUID(manifest["id"]))),
            result,
        ).id


@pytest.fixture
def registered_film(client, job_case):
    manifest, value, _ = job_case
    assert run(client, value) == "SUCCEEDED"
    return manifest, read(client, value)


def test_empty_version_migration_roundtrip_preserves_manifest_and_job(client, job_case, migrated):
    manifest, value, _ = job_case
    before = read(client, value)
    command.downgrade(migrated, "0059")
    names = {
        column["name"]
        for column in inspect(client.app.state.engine).get_columns("render_artifacts")
    }
    assert "job_id" not in names and "asset_version_id" not in names
    command.upgrade(migrated, "head")
    assert read(client, value) == before
    assert client.get(f"/api/v1/render-manifests/{manifest['id']}").json() == manifest
    command.check(migrated)


def test_upgrade_and_downgrade_preserve_legacy_manual_receipt(client, result_case, migrated):
    manifest, artifact, _ = result_case
    endpoint = f"/api/v1/render-manifests/{manifest['id']}/results"
    before = client.get(endpoint).json()
    command.downgrade(migrated, "0059")
    with client.app.state.session_factory() as db:
        legacy = dict(db.execute(text("SELECT * FROM render_artifacts")).mappings().one())
    assert "job_id" not in legacy and "asset_version_id" not in legacy
    command.upgrade(migrated, "head")
    assert client.get(endpoint).json() == before
    with client.app.state.session_factory() as db:
        row = db.get(RenderArtifact, artifact.id)
        assert row.job_id is None and row.asset_version_id is None
        assert row.file_id == artifact.file_id and row.checksum == artifact.sha256
    command.check(migrated)


@pytest.mark.parametrize("target", ["0059", "0056"])
def test_registered_history_refuses_downgrade_atomically(client, registered_film, migrated, target):
    manifest, value = registered_film
    tables = set(inspect(client.app.state.engine).get_table_names())
    factory = client.app.state.session_factory
    with factory() as db:
        head = db.scalar(text("SELECT version_num FROM alembic_version"))
        receipt = dict(db.execute(text("SELECT * FROM render_artifacts")).mappings().one())
    with pytest.raises(RuntimeError, match="Cannot discard render version history"):
        command.downgrade(migrated, target)
    assert set(inspect(client.app.state.engine).get_table_names()) == tables
    with factory() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == head
        assert dict(db.execute(text("SELECT * FROM render_artifacts")).mappings().one()) == receipt
    assert read(client, value) == value
    assert client.get(f"/api/v1/render-manifests/{manifest['id']}").json() == manifest
    command.check(migrated)


@pytest.mark.parametrize(
    "problem",
    [
        "job_id",
        "manifest_id",
        "asset_version_id",
        "file_id",
        "object_key",
        "checksum",
        "byte_size",
        "duration_ms",
    ],
)
def test_database_rejects_mismatched_registered_receipt(client, pending_registration, problem):
    def altered(artifact):
        if problem in {"job_id", "manifest_id", "asset_version_id", "file_id"}:
            value = uuid4()
        elif problem == "object_key":
            value = "render/unrelated-object.mp4"
        elif problem == "checksum":
            value = "0" * 64
        else:
            value = getattr(artifact, problem) + 1
        setattr(artifact, problem, value)

    with (
        altered_insert(RenderArtifact, altered),
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
    ):
        register_candidate(client, pending_registration)
    version_id = uuid5(UUID(pending_registration[1]["id"]), "render-version")
    with client.app.state.session_factory() as db:
        assert db.scalar(select(RenderArtifact.id)) is None
        assert db.get(AssetVersion, version_id) is None
        assert (
            db.scalar(select(StoredFile.id).where(StoredFile.asset_version_id == version_id))
            is None
        )


@pytest.mark.parametrize(
    "field",
    [
        "render_job_id",
        "render_manifest_id",
        "render_manifest_sha256",
        "timeline_id",
        "timeline_sha256",
        "inspection_id",
        "profile",
        "episode_id",
        "media.media_type",
        "media.byte_size",
        "media.byte_size.string",
        "media.byte_size.fractional",
        "media.byte_size.suffix",
        "media.duration_ms",
        "media.duration_ms.string",
        "media.duration_ms.fractional",
        "media.duration_ms.suffix",
        "storage_backend",
        "storage_bucket",
    ],
)
def test_database_rejects_forged_version_trace_metadata(client, pending_registration, field):
    def altered(version):
        metadata = deepcopy(version.metadata_json)
        if field == "media.media_type":
            metadata["media"]["media_type"] = "video/webm"
        elif field.startswith("media."):
            name = field.split(".")[1]
            value = metadata["media"][name]
            if field.endswith(".string"):
                metadata["media"][name] = str(value)
            elif field.endswith(".fractional"):
                metadata["media"][name] = value + 0.5
            elif field.endswith(".suffix"):
                metadata["media"][name] = f"{value}junk"
            else:
                metadata["media"][name] = value + 1
        elif field.endswith("sha256"):
            metadata[field] = "0" * 64
        elif field.endswith("_id"):
            metadata[field] = str(uuid4())
        elif field == "profile":
            metadata[field] = "formal" if metadata[field] == "preview" else "preview"
        elif field == "storage_backend":
            metadata[field] = "S3" if metadata[field] == "LOCAL" else "LOCAL"
        else:
            metadata[field] = "unrelated-private-bucket"
        version.metadata_json = metadata

    with (
        altered_insert(AssetVersion, altered),
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
    ):
        register_candidate(client, pending_registration)
    with client.app.state.session_factory() as db:
        assert db.scalar(select(RenderArtifact.id)) is None


@pytest.mark.parametrize("state", ["cancelled", "expired", "wrong_attempt"])
def test_database_registration_requires_current_live_uncancelled_attempt(
    client, pending_registration, state
):
    from datetime import timedelta

    from arclight.application.generation_leases import clock

    _, value, _, _ = pending_registration
    with client.app.state.session_factory.begin() as db:
        job = db.get(RenderJob, UUID(value["id"]))
        if state == "cancelled":
            job.cancel_requested_at = clock(db)
        elif state == "expired":
            job.lease_expires_at = clock(db) - timedelta(seconds=1)
        else:
            job.attempt_count += 1
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"):
        register_candidate(client, pending_registration)


def test_database_cannot_publish_passed_inspection_without_registered_version(
    client, pending_registration
):
    manifest, value, token, report = pending_registration
    with (
        pytest.raises(DBAPIError, match="registered render version required"),
        client.app.state.session_factory.begin() as db,
    ):
        file = output_file(db, manifest, token, report.model_dump(mode="json"))
        assert db.scalar(select(RenderArtifact.id)) is None
        # The execution ledger requires output and terminal status in one update.
        # Avoid finish() autoflushing an intermediate RUNNING row with an output.
        db.execute(
            update(RenderExecution)
            .where(RenderExecution.id == UUID(value["id"]))
            .values(
                output_file_id=file.id,
                status="SUCCEEDED",
                error_code=None,
                finished_at=func.current_timestamp(),
            )
        )
    with client.app.state.session_factory() as db:
        assert db.get(RenderExecution, UUID(value["id"])).status == "RUNNING"
        assert db.scalar(select(RenderArtifact.id)) is None


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_registered_receipt_cannot_be_updated_or_deleted(client, registered_film, operation):
    _, value = registered_film
    statement = (
        "UPDATE render_artifacts SET updated_at=updated_at"
        if operation == "UPDATE"
        else "DELETE FROM render_artifacts"
    )
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(text(statement))
    assert read(client, value) == value


def test_sqlite_replace_cannot_replace_registered_history(client, settings, request):
    if not settings.database_url.startswith("sqlite"):
        pytest.skip("SQLite replacement syntax")
    _, value = request.getfixturevalue("registered_film")
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(text("INSERT OR REPLACE INTO render_artifacts SELECT * FROM render_artifacts"))
    assert read(client, value) == value


def test_postgres_truncate_cannot_remove_registered_history(client, settings, request):
    require_postgres(settings)
    _, value = request.getfixturevalue("registered_film")
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(text("TRUNCATE render_artifacts CASCADE"))
    assert read(client, value) == value


def test_offline_postgres_version_guards_and_online_only_downgrade():
    output = StringIO()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"), output_buffer=output)
    config.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://offline:offline@localhost/offline"
    )
    command.upgrade(config, "0059:0060", sql=True)
    sql = output.getvalue()
    for fragment in (
        "ADD COLUMN job_id UUID",
        "ADD COLUMN asset_version_id UUID",
        "WHERE job_id IS NULL",
        "j.lease_expires_at>clock_timestamp()",
        "j.cancel_requested_at IS NULL",
        "registered render version required",
        "i.output_sha256=NEW.checksum",
        "f.asset_version_id=v.id",
        "d.target_asset_id=s.id",
    ):
        assert fragment in sql
    with pytest.raises(RuntimeError, match="requires an online history check"):
        command.downgrade(config, "0060:0059", sql=True)
