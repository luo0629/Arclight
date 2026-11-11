"""Machine acceptance provenance survives migrations and rejects direct SQL forgery."""

from copy import deepcopy
from hashlib import sha256
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from render_admission_helpers import ready_manifest, request_export
from render_job_helpers import submit
from render_manifest_helpers import (
    render_case,
    report_case,
    result_case,
    review_case,
    timeline_case,
)
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from test_concurrency import require_postgres
from test_continuity_selection import human_request_fixture
from test_human_reviews import decide, resume
from test_render_inspection_migrations import measured_result

from arclight.application.continuity_selection import require_shot_review
from arclight.application.human_reviews import ReviewHandler, submit_decision
from arclight.application.render_admission import admission_facts
from arclight.application.render_leases import claim, finish
from arclight.application.render_manifests import checked_snapshot
from arclight.application.render_reports import (
    canonical_report_bytes,
    report_envelope,
    report_warning,
    report_warnings,
)
from arclight.application.render_versions import register_render_version
from arclight.domain.continuity_selection import HUMAN_REVIEW_HANDLER
from arclight.domain.human_reviews import HumanDecision
from arclight.domain.render_inspections import RenderInspectionResult
from arclight.domain.render_manifests import RenderReportEnvelope
from arclight.domain.render_reports import RenderMachineReport, RenderReportChecks
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewRequest
from arclight.persistence.models import (
    AssetVersion,
    RenderAttempt,
    RenderExecution,
    RenderInspection,
    RenderJob,
    RenderManifest,
    RenderReportLink,
    StoredFile,
    Timeline,
)

__all__ = [
    "measured_result",
    "render_case",
    "report_case",
    "result_case",
    "review_case",
    "timeline_case",
]


@pytest.fixture
def formal_report_case(client, render_case, measured_result):
    """Prepare genuine exact DB receipts; this fixture does not publish a movie or report."""
    manifest, _ = ready_manifest(client, render_case, profile="FORMAL")
    request = request_export(client, manifest)
    decision = decide(client, request)
    assert decision.status_code == 200, decision.text
    assert resume(client, request, decision.json()).status_code == 200
    value, _ = submit(client, manifest)
    factory = client.app.state.session_factory
    token = claim(factory, UUID(value["id"]))
    assert token is not None
    with factory.begin() as db:
        row = db.get(RenderManifest, UUID(manifest["id"]))
        facts = admission_facts(db, row)
        snapshot = checked_snapshot(row)
        job = db.get(RenderJob, UUID(value["id"]))
        run = db.get(RenderExecution, job.id)
        attempt = db.get(RenderAttempt, token)
        attempt.input_sha256 = facts.input_sha256
        inspection = RenderInspection(
            attempt_id=token,
            manifest_id=row.id,
            manifest_sha256=row.content_sha256,
            input_sha256=facts.input_sha256,
            output_sha256=measured_result["output_sha256"],
            byte_size=measured_result["byte_size"],
            passed=True,
            result_json=deepcopy(measured_result),
        )
        db.add(inspection)
        db.flush()
        result = RenderInspectionResult.model_validate(measured_result)
        artifact = register_render_version(
            db, client.app.state.settings, run, job, attempt, snapshot, result
        )
        machine = RenderMachineReport(
            job_id=job.id,
            attempt_id=attempt.id,
            artifact_id=artifact.id,
            asset_version_id=artifact.asset_version_id,
            manifest_id=row.id,
            manifest_sha256=row.content_sha256,
            timeline_id=row.timeline_id,
            timeline_sha256=snapshot.timeline_sha256,
            timeline_version=db.get(Timeline, row.timeline_id).version_no,
            inspection_id=inspection.id,
            input_sha256=facts.input_sha256,
            output_file_id=artifact.file_id,
            output_sha256=artifact.checksum,
            byte_size=artifact.byte_size,
            duration_ms=artifact.duration_ms,
            sources=snapshot.sources,
            font=snapshot.font,
            software=snapshot.software,
            output=snapshot.output,
            line_audio_decisions=snapshot.line_audio_decisions,
            checks=RenderReportChecks(
                video_decodable=True,
                audio_stream_present=True,
                subtitle_range_valid=True,
                missing_required_shots=0,
                missing_audio_lines=0,
                stale_dependencies=0,
                technical=result,
            ),
            warnings=report_warnings(db, snapshot),
        )
        envelope = report_envelope(machine)
        raw = canonical_report_bytes(envelope)
        file = StoredFile(
            project_id=row.project_id,
            storage_backend="LOCAL",
            object_key=f"fixture/reports/{uuid4()}.json",
            checksum=sha256(raw).hexdigest(),
            byte_size=len(raw),
            media_type="application/json",
            original_name="migration-report.json",
            source_note="Migration fixture; no report object is published",
            rights_note="Generated deterministic test metadata",
            rights_confirmed=True,
            created_by="service-owner",
        )
        db.add(file)
        db.flush()
        fields = {
            "artifact_id": artifact.id,
            "file_id": file.id,
            "object_key": file.object_key,
            "checksum": file.checksum,
            "byte_size": file.byte_size,
            "inspection_id": inspection.id,
            "input_sha256": facts.input_sha256,
            "machine_report_json": envelope.model_dump(mode="json"),
        }
    return job.id, token, fields


def insert_report(client, fields):
    with client.app.state.session_factory.begin() as db:
        row = RenderReportLink(**fields)
        db.add(row)
        db.flush()
        return row.id


def complete_formal(client, job_id, output_file_id):
    with client.app.state.session_factory.begin() as db:
        run = db.get(RenderExecution, job_id)
        job = db.get(RenderJob, job_id)
        # The worker retains its claimed attempt before assigning terminal output.
        # Keep the same identity-map entry here so finish does not query/autoflush
        # an output_file_id while the execution is still RUNNING.
        attempt = db.get(RenderAttempt, job.lease_token)
        assert attempt is not None
        run.output_file_id = output_file_id
        attempt.progress, job.progress = 100, 100
        attempt.cleanup_status = "RETAINED"
        finish(db, run, job, "SUCCEEDED", None)


def report_columns(engine):
    """Compare every reflected column property while normalizing SQL type objects."""
    return [
        column | {"type": str(column["type"])}
        for column in inspect(engine).get_columns("render_report_links")
    ]


def store_changed_report_fixture(client, fields):
    """Store immutable metadata for the changed canonical bytes, without publishing an object."""
    raw = canonical_report_bytes(RenderReportEnvelope.model_validate(fields["machine_report_json"]))
    with client.app.state.session_factory.begin() as db:
        original = db.get(StoredFile, fields["file_id"])
        file = StoredFile(
            project_id=original.project_id,
            storage_backend="LOCAL",
            object_key=f"fixture/reports/{uuid4()}.json",
            checksum=sha256(raw).hexdigest(),
            byte_size=len(raw),
            media_type="application/json",
            original_name="migration-report.json",
            source_note="Migration fixture; no report object is published",
            rights_note="Generated deterministic test metadata",
            rights_confirmed=True,
            created_by="service-owner",
        )
        db.add(file)
        db.flush()
        fields.update(
            file_id=file.id,
            object_key=file.object_key,
            checksum=file.checksum,
            byte_size=file.byte_size,
        )


@pytest.mark.parametrize(
    "change",
    [
        "valid",
        "request",
        "decision",
        "hash",
        "reason",
        "request_version",
        "request_kind",
        "rejected",
    ],
)
def test_media_warning_binds_real_exact_human_request_and_decision(
    client, formal_report_case, change
):
    _, _, fields = formal_report_case
    machine = fields["machine_report_json"]["details"]
    source = next(
        item for item in machine["sources"] if item["asset_type"] in {"IMAGE", "SHOT_IMAGE"}
    )
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(source["asset_version_id"]))
        record = require_shot_review(db, version)
    reason = " " + "例" * 1998 + " "
    changes = (
        {"version_id": uuid4()}
        if change == "request_version"
        else {"kind": "EXPORT"}
        if change == "request_kind"
        else None
    )
    request_id = human_request_fixture(
        client,
        record,
        comment=reason,
        decision=None
        if change == "request_version"
        else "REJECT"
        if change == "rejected"
        else "APPROVE",
        changes=changes,
    )
    if change == "request_version":
        # A coherent approval for another version cannot authorize this report source.
        with client.app.state.session_factory.begin() as db:
            request = db.get(HumanReviewRequest, request_id)
            submit_decision(
                db,
                request.id,
                "service-owner",
                HumanDecision(
                    version_id=UUID(request.snapshot_json["version_id"]),
                    request_sha256=request.request_sha256,
                    decision="APPROVE",
                    comment=reason,
                ),
                registry={
                    HUMAN_REVIEW_HANDLER: ReviewHandler(
                        read=lambda _db, value: value, apply=lambda *_: {}
                    )
                },
            )
    with client.app.state.session_factory() as db:
        request = db.get(HumanReviewRequest, request_id)
        decision = db.scalar(
            select(HumanReviewDecision).where(HumanReviewDecision.request_id == request.id)
        )
        warning = report_warning(
            "MEDIA_EXCEPTION",
            version.id,
            None,
            reason,
            human_review_request_id=request.id,
            human_review_decision_id=decision.id,
            human_review_request_sha256=request.request_sha256,
        ).model_dump(mode="json")
    if change in {"request", "decision"}:
        warning[f"human_review_{change}_id"] = str(uuid4())
    elif change == "hash":
        warning["human_review_request_sha256"] = "0" * 64
    elif change == "reason":
        warning["reason"] = "Changed human decision"
    machine["warnings"].append(warning)
    store_changed_report_fixture(client, fields)
    if change == "valid":
        report_id = insert_report(client, fields)
        with client.app.state.session_factory() as db:
            assert (
                db.get(RenderReportLink, report_id).machine_report_json
                == fields["machine_report_json"]
            )
    else:
        with pytest.raises(DBAPIError, match="exact machine render report required"):
            insert_report(client, fields)


def test_empty_report_migration_roundtrip_preserves_legacy_files_and_guards(
    client, report_case, migrated
):
    manifest, _, report = report_case
    endpoint = f"/api/v1/render-manifests/{manifest['id']}/results"
    before = client.get(endpoint).json()
    with client.app.state.session_factory() as db:
        link = db.get(RenderReportLink, report.id)
        assert link.inspection_id is link.input_sha256 is link.machine_report_json is None
    command.downgrade(migrated, "0060")
    columns = {
        item["name"] for item in inspect(client.app.state.engine).get_columns("render_report_links")
    }
    assert not {"inspection_id", "input_sha256", "machine_report_json"} & columns
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert client.get(endpoint).json() == before
    for operation in (
        "UPDATE render_report_links SET updated_at=updated_at",
        "DELETE FROM render_report_links",
    ):
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(text(operation))


def test_valid_machine_report_retains_exact_receipt_and_allows_formal_success(
    client, formal_report_case
):
    job_id, _, fields = formal_report_case
    report_id = insert_report(client, fields)
    output = UUID(fields["machine_report_json"]["output_file_id"])
    complete_formal(client, job_id, output)
    with client.app.state.session_factory() as db:
        assert db.get(RenderExecution, job_id).status == "SUCCEEDED"
        report = db.get(RenderReportLink, report_id)
        assert report.machine_report_json == fields["machine_report_json"]
        assert report.inspection_id == fields["inspection_id"]


@pytest.mark.parametrize("legacy_link", [False, True])
def test_formal_success_requires_machine_provenance_even_with_legacy_report(
    client, formal_report_case, legacy_link
):
    job_id, _, fields = formal_report_case
    if legacy_link:
        legacy = {
            name: value
            for name, value in fields.items()
            if name not in {"inspection_id", "input_sha256", "machine_report_json"}
        }
        insert_report(client, legacy)
    with pytest.raises(DBAPIError, match="machine formal render report required"):
        complete_formal(client, job_id, UUID(fields["machine_report_json"]["output_file_id"]))
    with client.app.state.session_factory() as db:
        assert db.get(RenderExecution, job_id).status == "RUNNING"


@pytest.mark.parametrize("field", ["inspection_id", "input_sha256", "machine_report_json"])
def test_machine_provenance_cannot_be_partially_missing(client, formal_report_case, field):
    _, _, fields = formal_report_case
    fields[field] = None
    with pytest.raises(DBAPIError):
        insert_report(client, fields)


@pytest.mark.parametrize(
    "path,replacement",
    [
        ("schema_version", "1"),
        ("manifest_id", "00000000-0000-4000-8000-000000000001"),
        ("output_sha256", "0" * 64),
        ("details.checker", "caller-supplied"),
        ("details.render_status", "failed"),
        ("details.render_status", None),
        ("details.profile", "PREVIEW"),
        ("details.job_id", "00000000-0000-4000-8000-000000000001"),
        ("details.attempt_id", "00000000-0000-4000-8000-000000000001"),
        ("details.artifact_id", "00000000-0000-4000-8000-000000000001"),
        ("details.asset_version_id", "00000000-0000-4000-8000-000000000001"),
        ("details.manifest_sha256", "0" * 64),
        ("details.timeline_id", "00000000-0000-4000-8000-000000000001"),
        ("details.timeline_sha256", "0" * 64),
        ("details.inspection_id", "00000000-0000-4000-8000-000000000001"),
        ("details.input_sha256", "0" * 64),
        ("details.output_file_id", "00000000-0000-4000-8000-000000000001"),
        ("details.byte_size", 1),
        ("details.duration_ms", 1),
        ("details.timeline_version", True),
        ("details.sources", []),
        ("details.software", {}),
        ("details.line_audio_decisions", []),
        ("details.checks.video_decodable", 1),
        ("details.checks.audio_stream_present", False),
        ("details.checks.missing_required_shots", 1),
        ("details.checks.missing_audio_lines", "0"),
        ("details.checks.stale_dependencies", -1),
        ("details.checks.technical", {}),
        ("details.warnings", {}),
        ("details.warnings", []),
    ],
)
def test_direct_insert_cannot_forge_machine_report_bindings_or_checks(
    client, formal_report_case, path, replacement
):
    _, _, fields = formal_report_case
    value = fields["machine_report_json"]
    names = path.split(".")
    for name in names[:-1]:
        value = value[name]
    value[names[-1]] = replacement
    with pytest.raises(DBAPIError):
        insert_report(client, fields)


def test_machine_report_cannot_omit_required_nullable_font(client, formal_report_case):
    _, _, fields = formal_report_case
    del fields["machine_report_json"]["details"]["font"]
    with pytest.raises(DBAPIError, match="exact machine render report required"):
        insert_report(client, fields)


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("id", "not-a-sha256"),
        ("code", "MEDIA_EXCEPTION"),
        ("source_version_id", "00000000-0000-4000-8000-000000000001"),
        ("line_id", None),
        ("reason", "Changed approved creative exception"),
        ("human_review_request_id", "00000000-0000-4000-8000-000000000001"),
        ("human_review_decision_id", "00000000-0000-4000-8000-000000000001"),
        ("human_review_request_sha256", "0" * 64),
    ],
)
def test_line_warnings_require_exact_manifest_decision_without_media_provenance(
    client, formal_report_case, field, replacement
):
    _, _, fields = formal_report_case
    warnings = fields["machine_report_json"]["details"]["warnings"]
    assert warnings and all(item["code"] == "EXPLICIT_SILENCE" for item in warnings)
    warnings[0][field] = replacement
    with pytest.raises(DBAPIError, match="exact machine render report required"):
        insert_report(client, fields)


@pytest.mark.parametrize("change", ["cancelled", "expired", "wrong_input", "wrong_inspection"])
def test_machine_report_requires_current_live_uncancelled_inspection(
    client, formal_report_case, change
):
    job_id, _, fields = formal_report_case
    if change in {"wrong_input", "wrong_inspection"}:
        fields["input_sha256" if change == "wrong_input" else "inspection_id"] = (
            "0" * 64 if change == "wrong_input" else uuid4()
        )
    else:
        with client.app.state.session_factory.begin() as db:
            from datetime import UTC, datetime, timedelta

            job = db.get(RenderJob, job_id)
            if change == "cancelled":
                job.cancel_requested_at = datetime.now(UTC)
            else:
                job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"):
        insert_report(client, fields)


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_machine_report_and_added_fields_are_append_only(client, formal_report_case, operation):
    _, _, fields = formal_report_case
    insert_report(client, fields)
    statement = (
        "UPDATE render_report_links SET machine_report_json=NULL,"
        "inspection_id=NULL,input_sha256=NULL"
        if operation == "UPDATE"
        else "DELETE FROM render_report_links"
    )
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(text(statement))


def test_sqlite_replace_cannot_rewrite_machine_report(client, formal_report_case):
    if client.app.state.engine.dialect.name != "sqlite":
        pytest.skip("SQLite conflict algorithm contract")
    _, _, fields = formal_report_case
    report_id = insert_report(client, fields)
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(
            text(
                "INSERT OR REPLACE INTO render_report_links "
                "SELECT * FROM render_report_links WHERE id=:id"
            ),
            {"id": report_id.hex},
        )


def test_postgres_truncate_cannot_remove_machine_report(client, formal_report_case):
    require_postgres(client.app.state.settings)
    _, _, fields = formal_report_case
    insert_report(client, fields)
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(text("TRUNCATE render_report_links"))


@pytest.mark.parametrize("target", ["0060", "0058"])
def test_machine_history_refuses_downgrade_without_changing_schema(
    client, formal_report_case, migrated, target
):
    _, _, fields = formal_report_case
    report_id = insert_report(client, fields)
    before = report_columns(client.app.state.engine)
    with client.app.state.session_factory() as db:
        head = db.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="Cannot discard machine render report history"):
        command.downgrade(migrated, target)
    assert report_columns(client.app.state.engine) == before
    with client.app.state.session_factory() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == head
        assert (
            db.get(RenderReportLink, report_id).machine_report_json == fields["machine_report_json"]
        )
    command.check(migrated)


def test_upgrade_retains_legacy_success_without_fabricating_machine_acceptance(
    client, formal_report_case, migrated
):
    job_id, _, fields = formal_report_case
    command.downgrade(migrated, "0060")
    complete_formal(client, job_id, UUID(fields["machine_report_json"]["output_file_id"]))
    command.upgrade(migrated, "head")
    with client.app.state.session_factory() as db:
        assert db.get(RenderExecution, job_id).status == "SUCCEEDED"
        assert db.scalar(select(RenderReportLink.id)) is None
    command.check(migrated)


def test_offline_postgres_report_guards_and_online_only_downgrade():
    output = StringIO()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"), output_buffer=output)
    config.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://offline:offline@localhost/offline"
    )
    command.upgrade(config, "0060:0061", sql=True)
    sql = output.getvalue()
    for fragment in (
        "ADD COLUMN inspection_id UUID",
        "ADD COLUMN input_sha256 VARCHAR(64)",
        "ADD COLUMN machine_report_json JSONB",
        "render_report_machine_insert",
        "(NEW.machine_report_json #>> '{schema_version}')='1'",
        "j.lease_expires_at>clock_timestamp()",
        "i.output_sha256=a.checksum",
        "t.input_sha256=NEW.input_sha256",
        "machine formal render report required",
    ):
        assert fragment in sql
    with pytest.raises(RuntimeError, match="requires an online history check"):
        command.downgrade(config, "0061:0060", sql=True)
