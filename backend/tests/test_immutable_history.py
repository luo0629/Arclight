"""ASSET-11: enforce history at the SQL boundary, independently of service code."""

import hashlib
from datetime import timedelta
from io import StringIO
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, UniqueConstraint, inspect, select, text
from sqlalchemy.exc import IntegrityError

from arclight.persistence.base import Base, utcnow

# Everything except these explicit lifecycle/control fields must be immutable.
# Deriving the protected columns from metadata catches future unguarded additions.
MUTABLE = {
    "scripts": {"status", "updated_at"},
    "storyboards": {"status", "updated_at"},
    "shots": {"status", "updated_at"},
    "asset_versions": {"status", "stale", "revision", "archived_from_status", "updated_at"},
    "visual_assets": {
        "name",
        "status",
        "current_version_id",
        "lock_version",
        "revision",
        "archived_from_status",
        "metadata_json",
        "character_profile",
        "scene_profile",
        "prop_profile",
        "updated_at",
    },
    "story_sources": set(),
    "story_source_revisions": set(),
    "story_source_bindings": set(),
    "story_analyses": set(),
    "story_outlines": {"status", "confirmed_by", "confirmed_at", "updated_at"},
    "visual_bible_versions": set(),
    "asset_dependencies": set(),
    "character_confirmations": set(),
    "workflow_runs": {
        "status",
        "pending_review",
        "resume_json",
        "error_code",
        "revision",
        "updated_at",
    },
    "reviews": set(),
}


def seed_history(engine, episode):
    """Create a valid linked history using INSERTs only, including nullable identities."""
    ids = {}
    legacy = "raw_bytes" in {c["name"] for c in inspect(engine).get_columns("story_sources")}
    with engine.begin() as db:

        def add(table, **values):
            row_id = uuid4()
            if table == "story_sources":
                if legacy:
                    reflected = Table(table, MetaData(), autoload_with=db)
                    values = {
                        k: (v.hex if isinstance(v, UUID) and engine.dialect.name == "sqlite" else v)
                        for k, v in {"id": row_id, **values}.items()
                    }
                    db.execute(
                        reflected.insert().values(
                            **values, created_at=utcnow(), updated_at=utcnow()
                        )
                    )
                    ids[table] = row_id
                    return row_id
                values.pop("raw_bytes")
                values.pop("text")
                values.update(
                    storage_backend="LOCAL",
                    bucket=None,
                    object_key=f"sources/{episode['project_id']}/{row_id}",
                )
            db.execute(Base.metadata.tables[table].insert().values(id=row_id, **values))
            ids[table] = row_id
            return row_id

        project_id, episode_id = UUID(episode["project_id"]), UUID(episode["id"])
        run = add(
            "workflow_runs",
            episode_id=episode_id,
            trace_id="trace",
            idempotency_key="key",
            request_fingerprint="a" * 64,
            input_json={"story": "original"},
        )
        script = add(
            "scripts",
            episode_id=episode_id,
            run_id=run,
            version_no=1,
            content_json={"script": "original"},
        )
        board = add(
            "storyboards",
            episode_id=episode_id,
            run_id=run,
            script_version_id=script,
            version_no=1,
            content_json={"shots": ["original"]},
        )
        add(
            "shots",
            storyboard_id=board,
            shot_no=1,
            duration_ms=30000,
            description="original",
            content_json={"description": "original"},
        )
        asset = add(
            "visual_assets",
            project_id=project_id,
            asset_type="SCENE",
            name="original",
            creation_source={"note": "original"},
            created_by="service-owner",
        )
        versions = []
        for number in (1, 2):
            versions.append(
                add(
                    "asset_versions",
                    asset_id=asset,
                    version_no=number,
                    object_key=f"original/{number}.png",
                    checksum="a" * 64,
                    metadata_json={"provenance": "original"},
                    created_by="service-owner",
                    creation_key=uuid4(),
                )
            )
        add(
            "asset_dependencies",
            source_asset_version_id=versions[0],
            target_asset_version_id=versions[1],
            dependency_type="reference",
        )
        sources = []
        for number in (1, 2):
            source = add(
                "story_sources",
                episode_id=episode_id,
                import_no=number,
                import_key=uuid4(),
                request_fingerprint="a" * 64,
                original_name="original.txt",
                source_type="TXT",
                media_type="text/plain",
                encoding="utf-8",
                byte_size=8,
                sha256=hashlib.sha256(b"original").hexdigest(),
                raw_bytes=b"original",
                text="original",
                source_note="own",
                rights_note="own",
                created_by="service-owner",
            )
            sources.append(source)
            add(
                "story_source_revisions",
                source_id=source,
                root_source_id=sources[0],
                parent_source_id=sources[0] if number == 2 else None,
                version_no=number,
                change_note="original",
                origin_kind="FILE",
                rights_status="CONFIRMED",
                storage_backend="database",
                object_key=f"original/{number}.txt",
            )
        add("story_source_bindings", source_id=sources[1], run_id=run)
        analysis = add(
            "story_analyses",
            episode_id=episode_id,
            run_id=run,
            version_no=1,
            source_id=sources[1],
            source_name="original.txt",
            source_sha256="a" * 64,
            story_sha256="a" * 64,
            created_by="service-owner",
            content_json={"summary": "old"},
        )
        add(
            "story_outlines",
            project_id=project_id,
            episode_id=episode_id,
            run_id=run,
            base_version_id=None,
            version_no=1,
            content_json={"episodes": []},
            source_snapshot={"original": True},
            created_by="service-owner",
            save_key="key",
            request_fingerprint="a" * 64,
        )
        add(
            "visual_bible_versions",
            project_id=project_id,
            version_no=1,
            art_style="old",
            line_style="old",
            render_style="old",
            palette="old",
            lighting="old",
            composition="old",
            prohibited="old",
            negative_prompt="old",
            locked_fields=[],
            legacy_snapshot={"original": True},
            change_kind="EDIT",
            created_by="service-owner",
            creation_key=uuid4(),
            request_fingerprint="a" * 64,
        )
        entry = add(
            "story_entries",
            project_id=project_id,
            kind="CHARACTER",
            name="original",
            created_by="service-owner",
        )
        for source in ({"script_id": script}, {"analysis_id": analysis}):
            add(
                "character_confirmations",
                project_id=project_id,
                **source,
                source_name="original",
                entry_id=entry,
                request_fingerprint="a" * 64,
                snapshot_json={"original": True},
                created_by="service-owner",
            )
        add(
            "reviews",
            run_id=run,
            resource_type="script",
            version_id=script,
            reviewer_id="service-owner",
            decision="APPROVE",
            comment="original",
        )
    return ids


@pytest.fixture
def history(client, episode):
    return seed_history(client.app.state.engine, episode)


def changed(value, column):
    kind = column.type.python_type
    if kind is UUID:
        return uuid4()
    if kind is str:
        return "tampered"
    if kind is int:
        return (value or 0) + 1
    if kind is bytes:
        return b"tampered"
    if kind is dict:
        return ["tampered"] if isinstance(value, list) else {"tampered": True}
    return (value or utcnow()) + timedelta(seconds=1)


@pytest.mark.parametrize("name", MUTABLE)
def test_sql_cannot_change_or_delete_any_immutable_field(client, history, name):
    engine = client.app.state.engine
    table = Base.metadata.tables[name]
    with engine.connect() as db:
        original = dict(
            db.execute(select(table).where(table.c.id == history[name])).mappings().one()
        )
    for column in table.columns:
        if column.name in MUTABLE[name]:
            continue
        candidates = [changed(original[column.name], column)]
        if column.nullable and original[column.name] is not None:
            candidates.append(None)
        for value in candidates:
            # Suppress SQLAlchemy's updated_at default so another protected field cannot
            # accidentally mask a missing guard on the column under test.
            patch = {"updated_at": original["updated_at"], column.name: value}
            with (
                pytest.raises(IntegrityError, match=f"IMMUTABLE_HISTORY: {name}"),
                engine.begin() as db,
            ):
                db.execute(table.update().where(table.c.id == history[name]).values(**patch))
    with (
        pytest.raises(IntegrityError, match=f"IMMUTABLE_HISTORY: {name}"),
        engine.begin() as db,
    ):
        db.execute(table.delete().where(table.c.id == history[name]))
    with engine.begin() as db:
        # Rewriting identical values is allowed and preserves the exact snapshot.
        db.execute(table.update().where(table.c.id == history[name]).values(**original))
        assert (
            dict(db.execute(select(table).where(table.c.id == history[name])).mappings().one())
            == original
        )


def test_lifecycle_control_remains_mutable_and_new_versions_append(client, history):
    engine = client.app.state.engine
    changes = {
        "scripts": {"status": "APPROVED"},
        "storyboards": {"status": "REJECTED"},
        "shots": {"status": "APPROVED"},
        "asset_versions": {
            "status": "ARCHIVED",
            "stale": True,
            "revision": 1,
            "archived_from_status": "APPROVED",
        },
        "visual_assets": {
            "name": "edited",
            "current_version_id": history["asset_versions"],
            "status": "LOCKED",
            "revision": 1,
            "lock_version": 1,
            "metadata_json": {"description": "edited"},
        },
        "story_outlines": {
            "status": "APPROVED",
            "confirmed_by": "service-owner",
            "confirmed_at": utcnow(),
        },
        "workflow_runs": {
            "status": "FAILED",
            "revision": 1,
            "error_code": "TEST",
            "pending_review": {"stage": "script"},
            "resume_json": {"ok": True},
        },
    }
    with engine.begin() as db:
        for name, patch in changes.items():
            table = Base.metadata.tables[name]
            db.execute(table.update().where(table.c.id == history[name]).values(**patch))
            row = db.execute(select(table).where(table.c.id == history[name])).mappings().one()
            for key, value in patch.items():
                assert row[key] == value or (
                    key == "confirmed_at" and row[key].replace(tzinfo=value.tzinfo) == value
                )
        versions = Base.metadata.tables["asset_versions"]
        new_id = uuid4()
        db.execute(
            versions.insert().values(
                id=new_id,
                asset_id=history["visual_assets"],
                version_no=3,
                object_key="new.png",
                checksum="b" * 64,
                metadata_json={},
                created_by="service-owner",
                creation_key=uuid4(),
            )
        )
        assert len(db.execute(select(versions.c.id)).all()) == 3


def test_sqlite_replace_cannot_bypass_primary_or_any_unique_identity(client, history):
    engine = client.app.state.engine
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite REPLACE bypass regression")
    with engine.connect() as db:
        db.exec_driver_sql("PRAGMA recursive_triggers = OFF")
        assert db.exec_driver_sql("PRAGMA recursive_triggers").scalar() == 0
        for name in MUTABLE:
            table = Base.metadata.tables[name]
            originals = [dict(row) for row in db.execute(select(table)).mappings()]
            groups = [{"id"}] + [
                {column.name for column in constraint.columns}
                for constraint in table.constraints
                if isinstance(constraint, UniqueConstraint)
            ]
            for group in groups:
                original = next(
                    row for row in originals if all(row[key] is not None for key in group)
                )
                replacement = dict(original)
                unique_columns = set().union(*groups)
                for key in unique_columns - group:
                    replacement[key] = changed(original[key], table.c[key])
                with pytest.raises(IntegrityError, match=f"IMMUTABLE_HISTORY: {name} replacement"):
                    db.execute(table.insert().prefix_with("OR REPLACE").values(**replacement))
                db.rollback()
            assert [dict(row) for row in db.execute(select(table)).mappings()] == originals


def test_postgres_truncate_cannot_discard_history(client, history):
    engine = client.app.state.engine
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL TRUNCATE statement trigger")
    for name in MUTABLE:
        with pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
            db.exec_driver_sql(f'TRUNCATE TABLE "{name}" CASCADE')
    with engine.connect() as db:
        for name, row_id in history.items():
            table = Base.metadata.tables[name]
            assert db.scalar(select(table.c.id).where(table.c.id == row_id)) == row_id


@pytest.mark.legacy_schema
def test_upgrade_preserves_legacy_history_and_downgrade_keeps_guards(
    client,
    episode,
    migrated,
):
    engine = client.app.state.engine
    ids = seed_history(engine, episode)
    # Compare the actual historical columns; later nullable schema additions must
    # not be queried before their migration exists or synthesized into old rows.
    from sqlalchemy import MetaData, Table

    historical = {name: Table(name, MetaData(), autoload_with=engine) for name in MUTABLE}
    with engine.connect() as db:
        before = {
            name: [dict(row) for row in db.execute(select(historical[name])).mappings()]
            for name in MUTABLE
        }
    # Exercise 0019 on its own schema before 0026 deliberately externalizes bytes.
    command.upgrade(migrated, "0019")
    with pytest.raises(RuntimeError, match="Immutable version history exists"):
        command.downgrade(migrated, "0018")
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0019"
        for name, rows in before.items():
            assert [dict(row) for row in db.execute(select(historical[name])).mappings()] == rows
    for name in MUTABLE:
        table = Base.metadata.tables[name]
        with pytest.raises(IntegrityError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
            db.execute(table.delete().where(table.c.id == ids[name]))
    command.upgrade(migrated, "head")
    command.check(migrated)
    with pytest.raises(RuntimeError, match="Cannot downgrade object-backed story sources"):
        command.downgrade(migrated, "0025")


def test_offline_postgres_has_guards_and_refuses_unchecked_downgrade(migrated):
    output = StringIO()
    config = Config(migrated.config_file_name, output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command.upgrade(config, "0018:0019", sql=True)
    sql = output.getvalue()
    for name in MUTABLE:
        assert f"CREATE FUNCTION immutable_{name}_guard()" in sql
        assert f'BEFORE UPDATE OR DELETE ON "{name}"' in sql
        assert f'BEFORE TRUNCATE ON "{name}"' in sql
    assert "IS DISTINCT FROM" in sql
    with pytest.raises(RuntimeError, match="online preservation check"):
        command.downgrade(config, "0019:0018", sql=True)
