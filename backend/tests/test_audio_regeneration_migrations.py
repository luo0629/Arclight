"""Regeneration lineage survives upgrades and cannot be silently discarded."""

from copy import deepcopy
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from test_audio_tts import create_ok, declaration, finish_audio, tts_case

from arclight.application.audio_tts import origin_for, validate_tts_dispatch
from arclight.persistence.audio_tts import AudioTTSRequest
from arclight.persistence.audio_voices import AudioVoiceVersion
from arclight.persistence.generation import GenerationJob
from arclight.persistence.models import AssetVersion, DependencyManifest

__all__ = ["tts_case"]


@pytest.fixture
def regeneration_request(client, settings, tts_case):
    original, _, _ = finish_audio(client, settings, tts_case)
    response = client.post(tts_case["url"] + f"/{original['job_id']}/result", json=declaration())
    assert response.status_code == 201, response.text
    receipt = response.json()
    response = client.post(
        tts_case["url"] + "/regenerate",
        json={
            "creation_key": str(uuid4()),
            "source_audio_version_id": receipt["binding"]["asset_version_id"],
            "voice_version_id": tts_case["voice"]["id"],
            "expected_revision": receipt["asset_revision"],
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_regeneration_empty_schema_roundtrip_matches_metadata(client, migrated):
    engine = client.app.state.session_factory.kw["bind"]
    command.check(migrated)
    command.downgrade(migrated, "0063")
    assert "source_audio_version_id" not in {
        column["name"] for column in inspect(engine).get_columns("audio_tts_requests")
    }
    command.upgrade(migrated, "head")
    command.check(migrated)
    column = next(
        column
        for column in inspect(engine).get_columns("audio_tts_requests")
        if column["name"] == "source_audio_version_id"
    )
    assert column["nullable"]


def test_regeneration_upgrade_preserves_legacy_request_and_dispatch(client, tts_case, migrated):
    created, _ = create_ok(client, tts_case)
    factory = client.app.state.session_factory
    with factory() as db:
        request = db.scalar(select(AudioTTSRequest))
        fingerprint = request.request_fingerprint
        origin = origin_for(request)
        assert "source_audio_version_id" not in origin
        manifest = db.get(DependencyManifest, UUID(created["generation_manifest_id"]))
        frozen = deepcopy(manifest.snapshot_json)
    command.downgrade(migrated, "0063")
    command.upgrade(migrated, "head")
    command.check(migrated)
    with factory() as db:
        request = db.scalar(select(AudioTTSRequest))
        assert request.source_audio_version_id is None
        assert request.request_fingerprint == fingerprint
        assert origin_for(request) == origin
        manifest = db.get(DependencyManifest, UUID(created["generation_manifest_id"]))
        assert manifest.snapshot_json == frozen
        validate_tts_dispatch(db, db.get(GenerationJob, UUID(created["job_id"])))


def test_regeneration_history_blocks_downgrade_before_ddl(client, regeneration_request, migrated):
    engine = client.app.state.session_factory.kw["bind"]
    before = [
        (column["name"], str(column["type"]), column["nullable"])
        for column in inspect(engine).get_columns("audio_tts_requests")
    ]
    with engine.connect() as db:
        before_revision = db.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="Cannot discard audio regeneration history"):
        command.downgrade(migrated, "0063")
    assert [
        (column["name"], str(column["type"]), column["nullable"])
        for column in inspect(engine).get_columns("audio_tts_requests")
    ] == before
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == before_revision
        assert (
            db.scalar(
                select(func.count())
                .select_from(AudioTTSRequest)
                .where(AudioTTSRequest.source_audio_version_id.is_not(None))
            )
            == 1
        )


@pytest.mark.parametrize("replacement", [None, "different"], ids=["erase", "replace"])
def test_regeneration_source_is_immutable(client, regeneration_request, replacement):
    factory = client.app.state.session_factory
    source = UUID(regeneration_request["source_audio_version_id"])
    request_id = UUID(regeneration_request["job_id"])
    table = AudioTTSRequest.__table__
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
        db.execute(
            table.update()
            .where(table.c.job_id == request_id)
            .values(source_audio_version_id=uuid4() if replacement else None)
        )
    with factory() as db:
        request = db.scalar(select(AudioTTSRequest).where(AudioTTSRequest.job_id == request_id))
        assert request.source_audio_version_id == source


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_regeneration_offline_sql_installs_scope_guard_and_blocks_unchecked_downgrade(dialect):
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = (
        "sqlite:///unused.db"
        if dialect == "sqlite"
        else "postgresql+psycopg://unused:unused@localhost/unused"
    )
    command.upgrade(config, "0063:0064", sql=True)
    sql = output.getvalue()
    assert "source_audio_version_id" in sql
    assert "REFERENCES asset_versions" in sql
    assert "immutable_audio_tts_regeneration_insert" in sql
    assert "audio_line_bindings" in sql
    assert "b.snapshot_id=NEW.snapshot_id AND b.line_id=NEW.line_id" in sql
    assert "v.asset_id=NEW.target_asset_id" in sql
    assert "context" in sql
    with pytest.raises(RuntimeError, match="online history check"):
        command.downgrade(config, "0064:0063", sql=True)


@pytest.mark.parametrize(
    "violation", ["valid", "erase", "missing", "unbound", "wrong_line", "origin"]
)
def test_regeneration_insert_enforces_source_and_frozen_origin(
    client, regeneration_request, tts_case, violation
):
    factory = client.app.state.session_factory
    requests = AudioTTSRequest.__table__
    jobs = GenerationJob.__table__
    manifests = DependencyManifest.__table__
    with factory.begin() as db:
        values = dict(
            db.execute(
                select(requests).where(requests.c.job_id == UUID(regeneration_request["job_id"]))
            )
            .mappings()
            .one()
        )
        job = dict(db.execute(select(jobs).where(jobs.c.id == values["job_id"])).mappings().one())
        job.update(id=uuid4(), idempotency_key=str(uuid4()), status="FAILED")
        if violation in {"missing", "unbound"}:
            changed_source = uuid4()
            if violation == "unbound":
                versions = AssetVersion.__table__
                version = dict(
                    db.execute(
                        select(versions).where(versions.c.id == values["source_audio_version_id"])
                    )
                    .mappings()
                    .one()
                )
                version.update(
                    id=changed_source,
                    version_no=2,
                    creation_key=uuid4(),
                    object_key=f"{version['object_key']}/unbound-{changed_source}",
                )
                db.execute(versions.insert().values(**version))
            values["source_audio_version_id"] = changed_source
        if violation == "wrong_line":
            voices = AudioVoiceVersion.__table__
            voice = dict(
                db.execute(select(voices).where(voices.c.id == values["voice_version_id"]))
                .mappings()
                .one()
            )
            voice.update(
                id=uuid4(),
                creation_key=uuid4(),
                version_no=2,
                character_asset_id=None,
                speaker=tts_case["lines"][1]["speaker"],
            )
            db.execute(voices.insert().values(**voice))
            values["voice_version_id"] = voice["id"]
            values["line_id"] = tts_case["lines"][1]["line_id"]
        if violation in {"origin", "missing", "unbound"}:
            manifest = dict(
                db.execute(select(manifests).where(manifests.c.id == job["input_manifest_id"]))
                .mappings()
                .one()
            )
            frozen = deepcopy(manifest["snapshot_json"])
            if violation == "origin":
                del frozen["context"]["audio_line"]["source_audio_version_id"]
            else:
                frozen["context"]["audio_line"]["source_audio_version_id"] = str(changed_source)
            manifest.update(id=uuid4(), creation_key=uuid4(), snapshot_json=frozen)
            db.execute(manifests.insert().values(**manifest))
            job["input_manifest_id"] = manifest["id"]
        db.execute(jobs.insert().values(**job))
        values.update(id=uuid4(), creation_key=uuid4(), job_id=job["id"])
        if violation == "erase":
            values["source_audio_version_id"] = None
    if violation == "valid":
        with factory.begin() as db:
            db.execute(requests.insert().values(**values))
    else:
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
            db.execute(requests.insert().values(**values))
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(requests)) == (
            3 if violation == "valid" else 2
        )
