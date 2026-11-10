"""Database guards protect render inputs and receipts independently of HTTP writers."""

from uuid import UUID, uuid4

import pytest
from render_manifest_helpers import (
    manifest,
    render_case,
    report_case,
    result_case,
    review_case,
    stored_file,
    timeline_case,
)
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_collaboration import create_project

from arclight.persistence.models import DependencyManifest, StoredFile
from arclight.persistence.render_manifests import RenderArtifact, RenderManifest, RenderReportLink

__all__ = ["render_case", "report_case", "result_case", "review_case", "timeline_case"]
TABLES = ("render_manifests", "render_artifacts", "render_report_links")


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_database_rejects_updates_and_deletes_of_all_render_history(
    client, report_case, table, operation
):
    value, artifact, report = report_case
    statement = (
        f"UPDATE {table} SET updated_at=updated_at"
        if operation == "UPDATE"
        else f"DELETE FROM {table}"
    )
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(text(statement))
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value
    results = client.get(f"/api/v1/render-manifests/{value['id']}/results").json()
    assert results["artifact"] == artifact.model_dump(mode="json")
    assert results["report"] == report.model_dump(mode="json")


def test_sqlite_replace_cannot_bypass_append_only_history(client, report_case, settings):
    if not settings.database_url.startswith("sqlite"):
        pytest.skip("SQLite INSERT OR REPLACE syntax")
    value, _, _ = report_case
    for table in TABLES:
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(text(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"))
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value


def test_postgres_truncate_cannot_remove_render_history(client, settings, request):
    from test_concurrency import require_postgres

    require_postgres(settings)
    value, _, _ = request.getfixturevalue("report_case")
    for table in TABLES:
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(text(f"TRUNCATE {table} CASCADE"))
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value


def test_manifest_insert_guard_requires_exact_timeline_dependency_scope_and_licensed_font(
    client, episode, render_case
):
    value, _ = manifest(client, render_case)
    foreign = create_project(client)["id"]
    with client.app.state.session_factory() as db:
        original = db.get(RenderManifest, UUID(value["id"]))
        columns = {
            column.name: getattr(original, column.name) for column in original.__table__.columns
        }
        unrelated = db.scalar(
            select(DependencyManifest.id).where(
                DependencyManifest.id != original.dependency_manifest_id
            )
        )
    assert unrelated is not None
    foreign_font, _, _ = stored_file(client, foreign, b"font", "font/ttf")
    unlicensed_font, _, _ = stored_file(
        client, episode["project_id"], b"font", "font/ttf", rights=False
    )
    wrong_type, _, _ = stored_file(client, episode["project_id"], b"font", "text/plain")
    for changes in (
        {"project_id": UUID(foreign)},
        {"dependency_manifest_id": unrelated},
        {"font_file_id": foreign_font},
        {"font_file_id": unlicensed_font},
        {"font_file_id": wrong_type},
    ):
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.add(RenderManifest(**{**columns, "id": uuid4(), "creation_key": uuid4(), **changes}))
            db.flush()


@pytest.mark.parametrize("target", ["artifact", "report"])
def test_result_insert_guard_requires_exact_file_scope_type_key_hash_and_size(
    client, episode, render_case, result_case, target
):
    original, artifact, _ = result_case
    blank, _ = manifest(client, render_case)
    model = RenderArtifact if target == "artifact" else RenderReportLink
    parent = (
        {"manifest_id": UUID(blank["id"]), "duration_ms": 2500}
        if target == "artifact"
        else {"artifact_id": artifact.id}
    )
    media_type = "video/mp4" if target == "artifact" else "application/json"
    foreign = create_project(client)["id"]
    for problem in ("scope", "type", "object_key", "checksum", "byte_size"):
        file_id, _, _ = stored_file(
            client,
            foreign if problem == "scope" else episode["project_id"],
            b"new unbound receipt",
            "text/plain" if problem == "type" else media_type,
        )
        with client.app.state.session_factory() as db:
            file = db.get(StoredFile, file_id)
            values = {
                field: getattr(file, field) for field in ("object_key", "checksum", "byte_size")
            }
        if problem in values:
            values[problem] = {"object_key": "wrong/key", "checksum": "0" * 64, "byte_size": 1}[
                problem
            ]
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.add(model(**parent, file_id=file_id, **values))
            db.flush()
    assert client.get(f"/api/v1/render-manifests/{original['id']}").json() == original
