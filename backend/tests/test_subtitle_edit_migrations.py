"""Edited subtitle receipts retain their parent and database history guards."""

from copy import deepcopy
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import DBAPIError
from test_subtitle_production import (
    generate,
    render_case,
    review_case,
    subtitle_case,
    subtitle_source,
    timeline_case,
)

from arclight.application.subtitles import edit_subtitles
from arclight.domain.subtitles import SubtitleEdit
from arclight.persistence.models import AssetVersion, VisualAsset
from arclight.persistence.subtitles import SubtitleProduction

__all__ = ["render_case", "review_case", "timeline_case", "subtitle_source", "subtitle_case"]


def insert_guard(engine):
    with engine.connect() as db:
        if engine.dialect.name == "sqlite":
            return db.scalar(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='immutable_subtitle_productions_insert'"
                )
            )
        return db.scalar(
            text(
                "SELECT pg_get_functiondef("
                "'immutable_subtitle_productions_insert_guard()'::regprocedure)"
            )
        )


def edit_track(client, case):
    track = case["track"]
    response = client.get(
        f"/api/v1/projects/{case['episode']['project_id']}/assets/{track['asset_id']}"
    )
    assert response.status_code == 200, response.text
    response = client.post(
        f"/api/v1/subtitle-tracks/{track['id']}/edits",
        json={
            "creation_key": str(uuid4()),
            "expected_revision": response.json()["revision"],
            "changes": [{"cue_no": 1, "text": "新的显示字幕。"}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_subtitle_edit_empty_guard_roundtrip_retains_schema(client, migrated):
    engine = client.app.state.session_factory.kw["bind"]
    tables = sorted(inspect(engine).get_table_names())
    command.check(migrated)
    assert "subtitle-track/2" in insert_guard(engine)
    command.downgrade(migrated, "0066")
    assert "subtitle-track/2" not in insert_guard(engine)
    assert sorted(inspect(engine).get_table_names()) == tables
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert "subtitle-track/2" in insert_guard(engine)
    assert sorted(inspect(engine).get_table_names()) == tables


def test_subtitle_edit_guard_roundtrip_preserves_generated_history(client, subtitle_case, migrated):
    engine = client.app.state.session_factory.kw["bind"]
    track = subtitle_case["track"]
    url = f"/api/v1/subtitle-tracks/{track['id']}"
    original_srt = client.get(url + "/srt").content
    command.downgrade(migrated, "0066")
    assert "subtitle-track/2" not in insert_guard(engine)
    assert client.get(url).json() == track
    assert client.get(url + "/srt").content == original_srt
    command.upgrade(migrated, "head")
    edited = edit_track(client, subtitle_case)
    assert edited["id"] != track["id"]
    assert client.get(url + "/srt").content == original_srt


def test_subtitle_edit_history_blocks_downgrade_before_ddl(client, subtitle_case, migrated):
    edited = edit_track(client, subtitle_case)
    engine = client.app.state.session_factory.kw["bind"]
    guard = insert_guard(engine)
    statements = []

    def record_ddl(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {"ALTER", "CREATE", "DROP"}:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_ddl)
    try:
        with pytest.raises(RuntimeError, match="Cannot discard subtitle edit history"):
            command.downgrade(migrated, "0066")
    finally:
        event.remove(engine, "before_cursor_execute", record_ddl)
    assert statements == []
    assert insert_guard(engine) == guard
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0067"
        assert db.scalar(text("SELECT count(*) FROM subtitle_productions")) == 2
    assert client.get(f"/api/v1/subtitle-tracks/{edited['id']}").status_code == 200


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE"])
def test_subtitle_edit_receipts_keep_existing_immutable_guards(client, subtitle_case, operation):
    edit_track(client, subtitle_case)
    engine = client.app.state.session_factory.kw["bind"]
    if operation == "UPDATE":
        sql = "UPDATE subtitle_productions SET request_fingerprint='changed'"
    elif operation == "DELETE":
        sql = "DELETE FROM subtitle_productions"
    elif engine.dialect.name == "sqlite":
        sql = "INSERT OR REPLACE INTO subtitle_productions SELECT * FROM subtitle_productions"
    else:
        sql = "TRUNCATE subtitle_productions"
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
        db.execute(text(sql))
    with engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM subtitle_productions")) == 2


@pytest.mark.parametrize(
    "corruption",
    [
        "parent_checksum",
        "parent_document",
        "parent_self",
        "parent_other_asset",
        "original_cues",
        "display_order",
        "display_overlap",
        "display_out_of_bounds",
        "missing_display",
        "empty_text",
        "fractional_time",
        "unknown_schema",
    ],
)
def test_subtitle_edit_insert_guard_rejects_corrupted_receipt(client, subtitle_case, corruption):
    # Mutate only the pending receipt after application validation. Its version,
    # file, manifest and identity remain fresh, so duplicate keys cannot explain
    # the database rejection being exercised here.
    factory = client.app.state.session_factory
    track = subtitle_case["track"]
    other_track = None
    if corruption == "parent_other_asset":
        response = generate(client, subtitle_case, creation_key=str(uuid4()))
        assert response.status_code == 201, response.text
        other_track = response.json()
    with factory() as db:
        source = db.get(AssetVersion, UUID(track["id"]))
        asset = db.get(VisualAsset, source.asset_id)
        actor, revision = source.created_by, asset.revision
        other_version = (
            db.get(AssetVersion, UUID(other_track["id"])) if other_track is not None else None
        )
        other_checksum = other_version.checksum if other_version is not None else None
        other_document = (
            other_version.metadata_json["subtitle_document_sha256"]
            if other_version is not None
            else None
        )

    def corrupt(mapper, connection, row):
        document = deepcopy(row.snapshot_json)
        if corruption == "parent_checksum":
            document["edit_source_checksum"] = "0" * 64
        elif corruption == "parent_document":
            document["edit_source_document_sha256"] = "0" * 64
        elif corruption == "parent_self":
            document["edit_source_version_id"] = str(row.asset_version_id)
        elif corruption == "parent_other_asset":
            document["edit_source_version_id"] = other_track["id"]
            document["edit_source_checksum"] = other_checksum
            document["edit_source_document_sha256"] = other_document
        elif corruption == "original_cues":
            document["cues"][0]["text"] = "偷偷修改来源台词"
        elif corruption == "display_order":
            document["display_cues"].reverse()
        elif corruption == "display_overlap":
            document["display_cues"][1]["start_ms"] = document["display_cues"][0]["start_ms"]
        elif corruption == "display_out_of_bounds":
            document["display_cues"][-1]["end_ms"] = 3_600_001
        elif corruption == "missing_display":
            del document["display_cues"]
        elif corruption == "empty_text":
            document["display_cues"][0]["text"] = ""
        elif corruption == "fractional_time":
            document["display_cues"][0]["start_ms"] = 125.5
        else:
            document["schema_version"] = "subtitle-track/3"
        row.snapshot_json = document

    event.listen(SubtitleProduction, "before_insert", corrupt)
    try:
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory() as db:
            edit_subtitles(
                db,
                client.app.state.settings,
                UUID(track["id"]),
                SubtitleEdit(
                    creation_key=uuid4(),
                    expected_revision=revision,
                    changes=[{"cue_no": 1, "text": "合法的显示字幕修改。"}],
                ),
                actor,
            )
    finally:
        event.remove(SubtitleProduction, "before_insert", corrupt)
    with factory() as db:
        expected = 2 if other_track is not None else 1
        assert db.scalar(text("SELECT count(*) FROM subtitle_productions")) == expected


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_subtitle_edit_offline_sql_extends_only_the_insert_guard(dialect):
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = (
        "sqlite:///unused.db"
        if dialect == "sqlite"
        else "postgresql+psycopg://unused:unused@localhost/unused"
    )
    command.upgrade(config, "0066:0067", sql=True)
    sql = output.getvalue()
    for contract in (
        "immutable_subtitle_productions_insert",
        "subtitle-track/1",
        "subtitle-track/2",
        "display_cues",
        "edit_source_version_id",
        "edit_source_checksum",
        "edit_source_document_sha256",
        "pv.asset_id=v.asset_id",
        "pv.version_no<v.version_no",
        "m.target_asset_id=a.id",
        "f.rights_confirmed",
        "dependency_outputs",
        "subtitle_production_id",
    ):
        assert contract in sql
    assert "CREATE TABLE" not in sql
    assert "DROP TABLE" not in sql
    assert "immutable_subtitle_productions_write" not in sql
    assert "immutable_subtitle_productions_truncate" not in sql
    with pytest.raises(RuntimeError, match="online history check"):
        command.downgrade(config, "0067:0066", sql=True)
