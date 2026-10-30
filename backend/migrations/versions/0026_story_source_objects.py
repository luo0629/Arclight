"""Copy and verify exact story originals before removing database content columns."""

import hashlib
from uuid import UUID

import sqlalchemy as sa
from alembic import context, op

from arclight.core.config import get_settings
from arclight.core.errors import DomainError
from arclight.storage.objects import ObjectStorage

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

SOURCE_FIELDS = (
    "episode_id import_no import_key request_fingerprint original_name source_type media_type "
    "encoding byte_size sha256 source_note rights_note created_by updated_at"
)
REVISION_FIELDS = (
    "source_id root_source_id parent_source_id version_no change_note origin_kind "
    "rights_status storage_backend object_key updated_at"
)


def reserve(connection) -> None:
    if connection.dialect.name == "sqlite":
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        connection.execute(
            sa.text("LOCK TABLE story_sources, story_source_revisions IN ACCESS EXCLUSIVE MODE")
        )


def rows(connection, *, content=False):
    cursor = None
    fields = (
        ", s.raw_bytes, s.text, s.byte_size, s.sha256, s.encoding, s.media_type" if content else ""
    )
    while True:
        where = "WHERE s.id > :cursor" if cursor is not None else ""
        page = (
            connection.execute(
                sa.text(
                    f"SELECT s.id, e.project_id{fields} FROM story_sources s "
                    f"JOIN episodes e ON e.id=s.episode_id {where} ORDER BY s.id LIMIT 100"
                ),
                {"cursor": cursor} if cursor is not None else {},
            )
            .mappings()
            .all()
        )
        if not page:
            return
        yield from page
        cursor = page[-1]["id"]


def object_key(row) -> str:
    return f"sources/{UUID(str(row['project_id']))}/{UUID(str(row['id']))}"


def copy_originals(connection, settings) -> None:
    store = ObjectStorage(settings)
    try:
        store.check()
    except DomainError:
        raise RuntimeError(
            "Story source storage unavailable; original database content retained"
        ) from None
    for row in rows(connection, content=True):
        data = bytes(row["raw_bytes"])
        try:
            if (
                not 0 < len(data) <= 262144
                or len(data) != row["byte_size"]
                or hashlib.sha256(data).hexdigest() != row["sha256"]
                or data.decode(row["encoding"]) != row["text"]
            ):
                raise ValueError("Legacy source integrity mismatch")
            key = object_key(row)
            try:
                store.put(key, data, row["media_type"])
            except DomainError as exc:
                if exc.code != "OBJECT_EXISTS":
                    raise
                # A previous interrupted migration may have copied this exact object.
            if store.read(key, 262144, row["sha256"]) != data:
                raise ValueError("Copied source differs")
        except (DomainError, ValueError, UnicodeError, LookupError):
            raise RuntimeError(
                "Story source copy or integrity check failed; original database content retained"
            ) from None


def drop_guards(table) -> None:
    label = f"immutable_{table}"
    if op.get_bind().dialect.name == "sqlite":
        for suffix in ("update", "delete", "insert"):
            op.execute(f"DROP TRIGGER {label}_{suffix}")
    else:
        op.execute(f'DROP TRIGGER {label}_write ON "{table}"')
        op.execute(f'DROP TRIGGER {label}_truncate ON "{table}"')
        op.execute(f"DROP FUNCTION {label}_guard()")


def guards(table, fields, groups) -> None:
    label = f"immutable_{table}"
    names = ["id", "created_at", *fields.split()]
    if op.get_bind().dialect.name == "sqlite":
        changed = " OR ".join(f'OLD."{name}" IS NOT NEW."{name}"' for name in names)
        op.execute(f'''CREATE TRIGGER {label}_update BEFORE UPDATE ON "{table}"
            FOR EACH ROW WHEN {changed}
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table}'); END''')
        op.execute(f'''CREATE TRIGGER {label}_delete BEFORE DELETE ON "{table}"
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table}'); END''')
        conflicts = " OR ".join(
            "("
            + " AND ".join(
                f'NEW."{name}" IS NOT NULL AND existing."{name}"=NEW."{name}"' for name in group
            )
            + ")"
            for group in [("id",), *groups]
        )
        op.execute(f'''CREATE TRIGGER {label}_insert BEFORE INSERT ON "{table}"
            WHEN EXISTS (SELECT 1 FROM "{table}" existing WHERE {conflicts})
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table} replacement'); END''')
    else:
        changed = " OR ".join(f'OLD."{name}" IS DISTINCT FROM NEW."{name}"' for name in names)
        op.execute(f"""CREATE FUNCTION {label}_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
                    RAISE EXCEPTION 'IMMUTABLE_HISTORY: {table}' USING ERRCODE='23514';
                END IF;
                IF {changed} THEN
                    RAISE EXCEPTION 'IMMUTABLE_HISTORY: {table}' USING ERRCODE='23514';
                END IF;
                RETURN NEW;
            END; $$""")
        op.execute(f'''CREATE TRIGGER {label}_write BEFORE UPDATE OR DELETE ON "{table}"
            FOR EACH ROW EXECUTE FUNCTION {label}_guard()''')
        op.execute(f'''CREATE TRIGGER {label}_truncate BEFORE TRUNCATE ON "{table}"
            FOR EACH STATEMENT EXECUTE FUNCTION {label}_guard()''')


def install_guards(*, legacy=False) -> None:
    fields = SOURCE_FIELDS + (" raw_bytes text" if legacy else " storage_backend bucket object_key")
    groups = [("episode_id", "import_no"), ("episode_id", "import_key")]
    if not legacy:
        groups.append(("storage_backend", "object_key"))
    guards("story_sources", fields, groups)
    guards(
        "story_source_revisions",
        REVISION_FIELDS,
        [("source_id",), ("root_source_id", "version_no"), ("parent_source_id",), ("object_key",)],
    )


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Story object migration requires online verified object copies")
    connection = op.get_bind()
    reserve(connection)
    settings = None
    if connection.scalar(sa.text("SELECT 1 FROM story_sources LIMIT 1")):
        settings = context.config.attributes.get("storage_settings") or get_settings()
        # Before any schema or metadata mutation; originals survive any failed copy.
        copy_originals(connection, settings)
    drop_guards("story_sources")
    drop_guards("story_source_revisions")
    op.add_column("story_sources", sa.Column("storage_backend", sa.String(16)))
    op.add_column("story_sources", sa.Column("bucket", sa.String(63)))
    op.add_column("story_sources", sa.Column("object_key", sa.String(512)))
    if settings is not None:
        for row in rows(connection):
            connection.execute(
                sa.text(
                    "UPDATE story_sources SET storage_backend=:backend, bucket=:bucket, "
                    "object_key=:key WHERE id=:id"
                ),
                {
                    "backend": settings.object_storage_backend.upper(),
                    "bucket": settings.s3_bucket
                    if settings.object_storage_backend == "s3"
                    else None,
                    "key": object_key(row),
                    "id": row["id"],
                },
            )
    with op.batch_alter_table("story_sources") as batch:
        batch.drop_column("raw_bytes")
        batch.drop_column("text")
        batch.alter_column("storage_backend", existing_type=sa.String(16), nullable=False)
        batch.alter_column("object_key", existing_type=sa.String(512), nullable=False)
        batch.create_unique_constraint("uq_story_source_object", ["storage_backend", "object_key"])
        batch.create_check_constraint(
            op.f("ck_story_sources_source_object_storage"),
            "(storage_backend='LOCAL' AND bucket IS NULL) OR "
            "(storage_backend='S3' AND bucket IS NOT NULL)",
        )
    with op.batch_alter_table("story_source_revisions") as batch:
        batch.drop_constraint(
            op.f("ck_story_source_revisions_source_storage_backend"), type_="check"
        )
        batch.create_check_constraint(
            op.f("ck_story_source_revisions_source_storage_backend"),
            "storage_backend IN ('database','LOCAL','S3')",
        )
    install_guards()


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Story object downgrade requires online preservation check")
    connection = op.get_bind()
    reserve(connection)
    if connection.scalar(sa.text("SELECT 1 FROM story_sources LIMIT 1")):
        raise RuntimeError(
            "Cannot downgrade object-backed story sources; original history must survive"
        )
    drop_guards("story_sources")
    drop_guards("story_source_revisions")
    with op.batch_alter_table("story_sources") as batch:
        batch.drop_constraint("uq_story_source_object", type_="unique")
        batch.drop_constraint(op.f("ck_story_sources_source_object_storage"), type_="check")
        for name in ("storage_backend", "bucket", "object_key"):
            batch.drop_column(name)
        batch.add_column(sa.Column("raw_bytes", sa.LargeBinary(), nullable=False))
        batch.add_column(sa.Column("text", sa.Text(), nullable=False))
    with op.batch_alter_table("story_source_revisions") as batch:
        batch.drop_constraint(
            op.f("ck_story_source_revisions_source_storage_backend"), type_="check"
        )
        batch.create_check_constraint(
            op.f("ck_story_source_revisions_source_storage_backend"), "storage_backend='database'"
        )
    install_guards(legacy=True)
