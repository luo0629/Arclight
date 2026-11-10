"""Real MP4 and JSON receipts, exact associations, replay and integrity boundaries."""

from uuid import UUID, uuid4

import pytest
from render_manifest_helpers import (
    manifest,
    render_case,
    report_bytes,
    report_case,
    result_case,
    review_case,
    stored_file,
    timeline_case,
)
from sqlalchemy import func, select
from test_collaboration import create_project

from arclight.application import render_results
from arclight.application.render_results import bind_render_artifact, bind_render_report
from arclight.core.errors import DomainError
from arclight.media.versions import renderer_software
from arclight.persistence.models import AuditEvent, StoredFile
from arclight.persistence.render_manifests import RenderArtifact, RenderReportLink
from arclight.storage.objects import ObjectStorage

__all__ = ["render_case", "report_case", "result_case", "review_case", "timeline_case"]


def test_real_output_and_report_are_append_only_receipts_with_exact_private_identity(
    client, report_case
):
    value, artifact, report = report_case
    settings = client.app.state.settings
    with client.app.state.session_factory.begin() as db:
        assert (
            bind_render_artifact(
                db,
                settings,
                UUID(value["id"]),
                artifact.file_id,
                artifact.duration_ms,
                "service-owner",
            )
            == artifact
        )
        assert (
            bind_render_report(db, settings, UUID(value["id"]), report.file_id, "service-owner")
            == report
        )
        for model, receipt in ((RenderArtifact, artifact), (RenderReportLink, report)):
            row = db.get(model, receipt.id)
            file = db.get(StoredFile, receipt.file_id)
            assert (row.object_key, row.checksum, row.byte_size) == (
                file.object_key,
                file.checksum,
                file.byte_size,
            )
            assert ObjectStorage(settings).stat(row.object_key).size == row.byte_size
        assert db.get(StoredFile, artifact.file_id).rights_confirmed is False
        for action in ("render.artifact_bound", "render.report_bound"):
            assert (
                db.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.action == action)) == 1
            )
    response = client.get(f"/api/v1/render-manifests/{value['id']}/results")
    assert response.json() == {
        "manifest_id": value["id"],
        "artifact": artifact.model_dump(mode="json"),
        "report": report.model_dump(mode="json"),
    }
    assert "object_key" not in response.text
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value
    assert (
        client.get(f"/api/v1/timelines/{value['snapshot']['timeline_id']}").json()["snapshot"]
        == value["snapshot"]["timeline"]
    )


def test_rerender_uses_new_files_and_cannot_replace_or_borrow_old_results(
    client, episode, render_case, report_case
):
    original, artifact, report = report_case
    newer, _ = manifest(client, render_case)
    settings = client.app.state.settings
    factory = client.app.state.session_factory
    with pytest.raises(DomainError, match="已绑定其他清单"), factory.begin() as db:
        bind_render_artifact(
            db, settings, UUID(newer["id"]), artifact.file_id, artifact.duration_ms, "service-owner"
        )
    with factory() as db:
        source = db.get(StoredFile, artifact.file_id)
        data = ObjectStorage(settings).read(source.object_key, source.byte_size, source.checksum)
    new_file, _, _ = stored_file(client, episode["project_id"], data, "video/mp4")
    with factory.begin() as db:
        new_artifact = bind_render_artifact(
            db, settings, UUID(newer["id"]), new_file, artifact.duration_ms, "service-owner"
        )
    assert new_artifact.id != artifact.id and new_artifact.sha256 == artifact.sha256
    with pytest.raises(DomainError, match="不能替换"), factory.begin() as db:
        bind_render_artifact(
            db, settings, UUID(original["id"]), new_file, artifact.duration_ms, "service-owner"
        )
    with pytest.raises(DomainError, match="已绑定其他产物"), factory.begin() as db:
        bind_render_report(db, settings, UUID(newer["id"]), report.file_id, "service-owner")
    new_report, _, _ = stored_file(
        client, episode["project_id"], report_bytes(newer, new_artifact), "application/json"
    )
    with factory.begin() as db:
        bind_render_report(db, settings, UUID(newer["id"]), new_report, "service-owner")
    with pytest.raises(DomainError, match="不能替换"), factory.begin() as db:
        bind_render_report(db, settings, UUID(original["id"]), new_report, "service-owner")
    assert client.get(f"/api/v1/render-manifests/{original['id']}/results").json()[
        "artifact"
    ] == artifact.model_dump(mode="json")
    assert client.get(f"/api/v1/render-manifests/{original['id']}").json() == original


def test_pending_results_and_report_without_output_do_not_claim_completion(client, render_case):
    value, _ = manifest(client, render_case)
    assert client.get(f"/api/v1/render-manifests/{value['id']}/results").json() == {
        "manifest_id": value["id"],
        "artifact": None,
        "report": None,
    }
    with (
        pytest.raises(DomainError, match="精确产物"),
        client.app.state.session_factory.begin() as db,
    ):
        bind_render_report(
            db, client.app.state.settings, UUID(value["id"]), uuid4(), "service-owner"
        )


@pytest.mark.parametrize("duration", [0, -1, True, 2500.0, "2500", 3_600_001])
def test_invalid_measured_duration_never_creates_a_receipt(client, render_case, duration):
    value, _ = manifest(client, render_case)
    with (
        pytest.raises(DomainError, match="实测时长"),
        client.app.state.session_factory.begin() as db,
    ):
        bind_render_artifact(
            db, client.app.state.settings, UUID(value["id"]), uuid4(), duration, "service-owner"
        )


@pytest.mark.parametrize(
    "problem,code",
    [
        ("foreign", "NOT_FOUND"),
        ("unknown", "NOT_FOUND"),
        ("wrong_type", "RENDER_RESULT_INVALID"),
        ("hash", "OBJECT_UNAVAILABLE"),
        ("size", "OBJECT_UNAVAILABLE"),
        ("oversize", "OBJECT_UNAVAILABLE"),
        ("corrupt", "OBJECT_UNAVAILABLE"),
        ("missing", "OBJECT_UNAVAILABLE"),
        ("provenance", "RENDER_RIGHTS_REQUIRED"),
    ],
)
def test_output_requires_exact_project_receipt_and_actual_bounded_bytes(
    client, episode, render_case, problem, code
):
    value, _ = manifest(client, render_case)
    project = create_project(client)["id"] if problem == "foreign" else episode["project_id"]
    overrides = {}
    if problem == "hash":
        overrides["checksum"] = "0" * 64
    elif problem in {"size", "oversize"}:
        overrides["byte_size"] = 1 if problem == "size" else render_results.MAX_RENDER_BYTES + 1
    elif problem == "provenance":
        overrides["source_note"] = " "
    file_id, _, _ = stored_file(
        client,
        project,
        b"output-bytes",
        "text/plain" if problem == "wrong_type" else "video/mp4",
        **overrides,
    )
    if problem in {"corrupt", "missing"}:
        with client.app.state.session_factory() as db:
            row = db.get(StoredFile, file_id)
            storage = ObjectStorage(client.app.state.settings)
            if problem == "corrupt":
                storage.path(row.object_key).write_bytes(b"changed")
            else:
                storage.delete(row.object_key)
    with pytest.raises(DomainError) as error, client.app.state.session_factory.begin() as db:
        bind_render_artifact(
            db,
            client.app.state.settings,
            UUID(value["id"]),
            uuid4() if problem == "unknown" else file_id,
            2500,
            "service-owner",
        )
    assert error.value.code == code
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(RenderArtifact.id))) == 0


@pytest.mark.parametrize(
    "field", ["manifest_id", "manifest_sha256", "timeline_id", "output_file_id", "output_sha256"]
)
def test_report_cannot_bind_checks_for_another_input_or_output(client, episode, result_case, field):
    value, artifact, _ = result_case
    wrong = "0" * 64 if field.endswith("sha256") else str(uuid4())
    file_id, _, _ = stored_file(
        client,
        episode["project_id"],
        report_bytes(value, artifact, **{field: wrong}),
        "application/json",
    )
    with pytest.raises(DomainError, match="不匹配"), client.app.state.session_factory.begin() as db:
        bind_render_report(
            db, client.app.state.settings, UUID(value["id"]), file_id, "service-owner"
        )
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(RenderReportLink.id))) == 0


@pytest.mark.parametrize("problem", ["foreign", "type", "json", "oversize", "hash", "size"])
def test_report_rejects_wrong_scope_type_schema_and_inconsistent_bytes(
    client, episode, result_case, problem
):
    value, artifact, _ = result_case
    project = create_project(client)["id"] if problem == "foreign" else episode["project_id"]
    data = report_bytes(value, artifact)
    overrides = {}
    if problem == "json":
        data = b"{}"
    elif problem == "oversize":
        data += b" " * render_results.MAX_REPORT_BYTES
    elif problem == "hash":
        overrides["checksum"] = "0" * 64
    elif problem == "size":
        overrides["byte_size"] = len(data) + 1
    file_id, _, _ = stored_file(
        client,
        project,
        data,
        "text/plain" if problem == "type" else "application/json",
        **overrides,
    )
    with pytest.raises(DomainError), client.app.state.session_factory.begin() as db:
        bind_render_report(
            db, client.app.state.settings, UUID(value["id"]), file_id, "service-owner"
        )


def test_changed_encoder_requires_new_manifest_but_does_not_rewrite_history(
    client, episode, render_case, monkeypatch
):
    value, _ = manifest(client, render_case)
    file_id, _, _ = stored_file(client, episode["project_id"], b"output", "video/mp4")
    changed = renderer_software().model_copy(update={"renderer_version": "arclight-pyav/next"})
    monkeypatch.setattr(render_results, "renderer_software", lambda: changed)
    with (
        pytest.raises(DomainError, match="软件已变更"),
        client.app.state.session_factory.begin() as db,
    ):
        bind_render_artifact(
            db, client.app.state.settings, UUID(value["id"]), file_id, 2500, "service-owner"
        )
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value
