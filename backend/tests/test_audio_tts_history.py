"""Database scope and immutable history for exact dialogue TTS requests."""

from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from test_audio_production import audio_source, prepare

from arclight.persistence.audio_tts import AudioTTSRequest
from arclight.persistence.audio_voices import AudioVoiceVersion
from arclight.persistence.generation import GenerationJob
from arclight.persistence.models import DependencyManifest, Project, VisualAsset

__all__ = ["audio_source"]
TABLES = (AudioVoiceVersion.__table__, AudioTTSRequest.__table__)


@pytest.fixture
def tts_ready(client, audio_source):
    _, target_id, manifest_id = prepare(client, audio_source)
    episode, _, snapshot, lines = audio_source
    project_id = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        character = VisualAsset(project_id=project_id, asset_type="CHARACTER", name="角色")
        foreign = Project(name="另一项目", owner_id="service:test")
        db.add_all([character, foreign])
        db.flush()
        voice = AudioVoiceVersion(
            project_id=project_id,
            speaker=lines[0]["speaker"],
            character_asset_id=character.id,
            version_no=1,
            creation_key=uuid4(),
            request_fingerprint="a" * 64,
            configuration_json={"voice_id": "test-voice"},
            service_snapshot_json={"model": "test-tts"},
            created_by="service:test",
        )
        generation = DependencyManifest(
            project_id=project_id,
            target_asset_id=UUID(target_id),
            creation_key=uuid4(),
            request_fingerprint="b" * 64,
            snapshot_json={
                "context": {"audio_line": {"dependency_manifest_id": manifest_id}},
                "inputs": [],
            },
            created_by="service:test",
        )
        db.add_all([voice, generation])
        db.flush()
        job = GenerationJob(
            project_id=project_id,
            episode_id=UUID(episode["id"]),
            target_type="AUDIO",
            target_asset_id=UUID(target_id),
            input_manifest_id=generation.id,
            task_type="AUDIO",
            provider="mock",
            model="test-tts",
            status="SUCCEEDED",
            idempotency_key=str(uuid4()),
            request_fingerprint="c" * 64,
            created_by="service:test",
        )
        db.add(job)
        db.flush()
        voice_values = dict(
            db.execute(select(TABLES[0]).where(TABLES[0].c.id == voice.id)).mappings().one()
        )
        request = {
            "id": uuid4(),
            "project_id": project_id,
            "snapshot_id": UUID(snapshot["id"]),
            "line_id": lines[0]["line_id"],
            "voice_version_id": voice.id,
            "target_asset_id": UUID(target_id),
            "dependency_manifest_id": UUID(manifest_id),
            "job_id": job.id,
            "creation_key": uuid4(),
            "request_fingerprint": "d" * 64,
            "created_by": "service:test",
        }
        return {
            "voice": voice_values,
            "request": request,
            "foreign_project_id": foreign.id,
            "generation_manifest_id": generation.id,
        }


@pytest.fixture
def tts_history(client, tts_ready):
    with client.app.state.session_factory.begin() as db:
        db.execute(TABLES[1].insert().values(**tts_ready["request"]))
        tts_ready["request"] = dict(db.execute(select(TABLES[1])).mappings().one())
    return tts_ready


def test_audio_tts_migration_matches_metadata_and_empty_roundtrip(client, migrated):
    engine = client.app.state.session_factory.kw["bind"]
    command.check(migrated)
    command.downgrade(migrated, "0062")
    assert not {table.name for table in TABLES} & set(inspect(engine).get_table_names())
    command.upgrade(migrated, "head")
    command.check(migrated)
    with engine.connect() as db:
        for table in TABLES:
            assert db.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.parametrize("table", TABLES, ids=lambda table: table.name)
@pytest.mark.parametrize("action", ["UPDATE", "DELETE"])
def test_audio_tts_history_refuses_sql_mutation(client, tts_history, table, action):
    engine = client.app.state.session_factory.kw["bind"]
    statement = (
        f"UPDATE {table.name} SET created_by='rewritten'"
        if action == "UPDATE"
        else f"DELETE FROM {table.name}"
    )
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
        db.execute(text(statement))
    with engine.connect() as db:
        assert db.scalar(select(func.count()).select_from(table)) == 1
        assert db.scalar(select(table.c.created_by)) == "service:test"


@pytest.mark.parametrize("table", TABLES, ids=lambda table: table.name)
def test_postgres_audio_tts_truncate_keeps_history(client, tts_history, table):
    engine = client.app.state.session_factory.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL TRUNCATE boundary")
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
        db.execute(text(f"TRUNCATE {table.name} CASCADE"))
    with engine.connect() as db:
        assert db.scalar(select(func.count()).select_from(table)) == 1


@pytest.mark.parametrize(
    ("table_index", "conflict"),
    [
        (0, "id"),
        (0, "speaker_version"),
        (0, "creation_key"),
        (1, "id"),
        (1, "job_id"),
        (1, "creation_key"),
    ],
)
def test_sqlite_audio_tts_replace_cannot_discard_unique_history(
    client, tts_history, table_index, conflict
):
    engine = client.app.state.session_factory.kw["bind"]
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite INSERT OR REPLACE boundary")
    table = TABLES[table_index]
    original = tts_history["voice" if table_index == 0 else "request"]
    values = {**original, "id": uuid4(), "creation_key": uuid4(), "created_by": "replacement"}
    if table_index == 0:
        values["version_no"] = 2
    elif conflict != "job_id":
        with engine.begin() as db:
            job_table = GenerationJob.__table__
            job_values = dict(
                db.execute(select(job_table).where(job_table.c.id == values["job_id"]))
                .mappings()
                .one()
            )
            job_values.update(id=uuid4(), idempotency_key=str(uuid4()))
            db.execute(job_table.insert().values(**job_values))
            values["job_id"] = job_values["id"]
    if conflict == "id":
        values["id"] = original["id"]
    elif conflict == "creation_key":
        values["creation_key"] = original["creation_key"]
    elif conflict == "speaker_version":
        values["version_no"] = original["version_no"]
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
        db.execute(table.insert().prefix_with("OR REPLACE").values(**values))
    with engine.connect() as db:
        assert dict(db.execute(select(table)).mappings().one()) == original


@pytest.mark.parametrize("with_request", [False, True], ids=["voice_only", "voice_and_request"])
def test_audio_tts_downgrade_refuses_history_before_any_ddl(
    client, tts_ready, migrated, with_request
):
    engine = client.app.state.session_factory.kw["bind"]
    if with_request:
        with engine.begin() as db:
            db.execute(TABLES[1].insert().values(**tts_ready["request"]))
    before = set(inspect(engine).get_table_names())
    with pytest.raises(RuntimeError, match="audio TTS history"):
        command.downgrade(migrated, "0062")
    assert set(inspect(engine).get_table_names()) == before
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0063"
        assert db.scalar(select(func.count()).select_from(TABLES[0])) == 1
        assert db.scalar(select(func.count()).select_from(TABLES[1])) == int(with_request)


def test_audio_tts_offline_sql_installs_guards_and_rejects_unchecked_downgrade():
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command.upgrade(config, "0062:0063", sql=True)
    sql = output.getvalue()
    for table in TABLES:
        assert f"CREATE TABLE {table.name}" in sql
        assert f"BEFORE TRUNCATE ON {table.name}" in sql
        assert f"CREATE FUNCTION immutable_{table.name}_insert_guard" in sql
    assert "uq_audio_voice_speaker_version" in sql
    assert "uq_audio_voice_creation" in sql
    assert "uq_audio_tts_creation" in sql
    assert "uq_audio_tts_requests_job_id" in sql
    assert "jsonb_array_elements" in sql
    assert "dependency_manifest_id'" in sql
    with pytest.raises(RuntimeError, match="online history check"):
        command.downgrade(config, "0063:0062", sql=True)


@pytest.mark.parametrize("violation", ["foreign_character", "wrong_asset_type"])
def test_audio_voice_insert_rejects_incorrect_character_scope(client, tts_ready, violation):
    with client.app.state.session_factory.begin() as db:
        asset = VisualAsset(
            project_id=(
                tts_ready["foreign_project_id"]
                if violation == "foreign_character"
                else tts_ready["voice"]["project_id"]
            ),
            asset_type="CHARACTER" if violation == "foreign_character" else "AUDIO",
            name="错误角色来源",
        )
        db.add(asset)
        db.flush()
        asset_id = asset.id
    values = {
        **tts_ready["voice"],
        "id": uuid4(),
        "version_no": 2,
        "creation_key": uuid4(),
        "character_asset_id": asset_id,
    }
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(TABLES[0].insert().values(**values))


@pytest.mark.parametrize("version_no", [0, -1])
def test_audio_voice_insert_requires_positive_version(client, tts_ready, version_no):
    values = {
        **tts_ready["voice"],
        "id": uuid4(),
        "version_no": version_no,
        "creation_key": uuid4(),
    }
    with (
        pytest.raises(DBAPIError, match="audio_voice_number"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(TABLES[0].insert().values(**values))


@pytest.mark.parametrize(
    "violation",
    [
        "foreign_project",
        "missing_line",
        "wrong_speaker",
        "foreign_voice",
        "foreign_target",
        "wrong_target_type",
        "wrong_manifest_target",
        "foreign_manifest",
        "foreign_job",
        "wrong_job_task",
        "wrong_job_target",
        "wrong_job_episode",
        "wrong_origin_manifest",
    ],
)
def test_audio_tts_insert_rejects_mismatched_exact_scope(client, tts_ready, violation):
    values = dict(tts_ready["request"])
    with client.app.state.session_factory.begin() as db:
        if violation == "foreign_project":
            values["project_id"] = tts_ready["foreign_project_id"]
        elif violation == "missing_line":
            values["line_id"] = "missing-line"
        elif violation in {"wrong_speaker", "foreign_voice"}:
            voice_values = {
                **tts_ready["voice"],
                "id": uuid4(),
                "creation_key": uuid4(),
                "character_asset_id": None,
                "speaker": "另一说话者"
                if violation == "wrong_speaker"
                else tts_ready["voice"]["speaker"],
                "project_id": (
                    tts_ready["foreign_project_id"]
                    if violation == "foreign_voice"
                    else values["project_id"]
                ),
            }
            db.execute(TABLES[0].insert().values(**voice_values))
            values["voice_version_id"] = voice_values["id"]
        elif violation in {"foreign_target", "wrong_target_type"}:
            asset = VisualAsset(
                project_id=(
                    tts_ready["foreign_project_id"]
                    if violation == "foreign_target"
                    else values["project_id"]
                ),
                asset_type="AUDIO" if violation == "foreign_target" else "CHARACTER",
                name="错误输出范围",
            )
            db.add(asset)
            db.flush()
            values["target_asset_id"] = asset.id
        elif violation in {"wrong_manifest_target", "foreign_manifest"}:
            manifest = DependencyManifest(
                project_id=(
                    tts_ready["foreign_project_id"]
                    if violation == "foreign_manifest"
                    else values["project_id"]
                ),
                target_asset_id=None,
                creation_key=uuid4(),
                request_fingerprint="e" * 64,
                snapshot_json={},
                created_by="service:test",
            )
            db.add(manifest)
            db.flush()
            values["dependency_manifest_id"] = manifest.id
        else:
            job_table = GenerationJob.__table__
            job = dict(
                db.execute(select(job_table).where(job_table.c.id == values["job_id"]))
                .mappings()
                .one()
            )
            job.update(id=uuid4(), idempotency_key=str(uuid4()))
            if violation == "foreign_job":
                job["project_id"] = tts_ready["foreign_project_id"]
            elif violation == "wrong_job_task":
                job["task_type"] = "IMAGE"
            elif violation == "wrong_job_target":
                job["target_asset_id"] = None
            elif violation == "wrong_job_episode":
                job["episode_id"] = None
            else:
                manifest = DependencyManifest(
                    project_id=values["project_id"],
                    target_asset_id=values["target_asset_id"],
                    creation_key=uuid4(),
                    request_fingerprint="f" * 64,
                    snapshot_json={
                        "context": {"audio_line": {"dependency_manifest_id": str(uuid4())}}
                    },
                    created_by="service:test",
                )
                db.add(manifest)
                db.flush()
                job["input_manifest_id"] = manifest.id
            db.execute(job_table.insert().values(**job))
            values["job_id"] = job["id"]
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(TABLES[1].insert().values(**values))
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(TABLES[1])) == 0
