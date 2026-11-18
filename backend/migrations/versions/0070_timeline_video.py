"""Admit exact shot videos in sealed timelines and their formal review reports."""

from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic import context, op
from alembic.util import load_python_file

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def replace_guard_term(condition: str, previous: str, replacement: str) -> str:
    # Extend the fixed SQL of published revisions without editing or reapplying them.
    if condition.count(previous) != 1:
        raise RuntimeError(f"Expected one prior guard term: {previous}")
    return condition.replace(previous, replacement, 1)


def add_video_picture(condition: str, reports: ModuleType) -> str:
    still = "a.asset_type IN ('IMAGE','SHOT_IMAGE')"

    def exact_source(field: str, column: str) -> str:
        value = reports.json_text("v.metadata_json", f"video_source.{field}")
        return reports.uuid_matches(column, value)

    video = (
        "a.asset_type='VIDEO' "
        f"AND {exact_source('storyboard_id', 'b.id')} "
        f"AND {exact_source('shot_id', 'NEW.shot_id')} "
        "AND a.current_version_id=v.id AND NOT v.stale AND v.status IN ('APPROVED','LOCKED') "
        "AND EXISTS (SELECT 1 FROM generation_candidates c JOIN generation_jobs j "
        "ON j.id=c.job_id WHERE c.asset_version_id=v.id AND c.project_id=e.project_id "
        "AND j.project_id=e.project_id AND j.episode_id=e.id AND j.task_type='VIDEO') "
        "AND EXISTS (SELECT 1 FROM stored_files f WHERE f.asset_version_id=v.id "
        "AND f.project_id=e.project_id AND f.object_key=v.object_key "
        "AND f.checksum=v.checksum AND f.media_type='video/mp4')"
    )
    return replace_guard_term(condition, still, f"({still} OR ({video}))")


def add_video_warning(condition: str, reports: ModuleType, *, sqlite: bool) -> str:
    warning = "w.value" if sqlite else "w"
    source = "s.value" if sqlite else "s"
    sources = reports.json_value("m.snapshot_json", "sources")
    each = "json_each" if sqlite else "jsonb_array_elements"
    source_type = reports.json_text(source, "asset_type")
    version = reports.json_text(source, "asset_version_id")
    warned_version = reports.json_text(warning, "source_version_id")
    still = f"{source_type} IN ('IMAGE','SHOT_IMAGE')"
    still_handler = "q.handler='continuity_shot'"

    def source_matches(allowed: str) -> str:
        return (
            f"AND EXISTS (SELECT 1 FROM {each}({sources}) s WHERE {allowed} "
            f"AND {version}={warned_version})"
        )

    allowed = (
        f"(({still} AND {still_handler}) OR "
        f"({source_type}='VIDEO' AND q.handler='video_temporal_review'))"
    )
    # Evaluate source type inside the request check so it stays paired with its handler.
    condition = replace_guard_term(condition, f" {source_matches(still)}", "")
    return replace_guard_term(condition, f"AND {still_handler} ", f"{source_matches(allowed)} ")


def install_guards(*, video: bool) -> None:
    versions = str(Path(__file__).parent)
    audio = load_python_file(versions, "0068_timeline_audio.py")
    reports = load_python_file(versions, "0061_render_machine_reports.py")
    sqlite = op.get_bind().dialect.name == "sqlite"
    if video:
        condition = add_video_picture(audio.clip_guard(sqlite, music=True), reports)
        if sqlite:
            op.execute("DROP TRIGGER immutable_timeline_clips_insert")
            op.execute(
                "CREATE TRIGGER immutable_timeline_clips_insert BEFORE INSERT ON timeline_clips "
                f"WHEN {condition} "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: timeline scope'); END"
            )
        else:
            op.execute(
                "CREATE OR REPLACE FUNCTION immutable_timeline_clips_insert_guard() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                f"IF {condition} THEN RAISE EXCEPTION 'IMMUTABLE_HISTORY: timeline'; "
                "END IF; RETURN NEW; END; $$"
            )
    else:
        audio.install_clip_guard(music=True)
    name = "render_report_machine_insert"
    condition = reports.machine_condition()
    if video:
        condition = add_video_warning(condition, reports, sqlite=sqlite)
    if sqlite:
        op.execute(f"DROP TRIGGER {name}")
        reports.create_guard(
            name, "render_report_links", condition, "exact machine render report required"
        )
    else:
        op.execute(
            f"CREATE OR REPLACE FUNCTION {name}_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN IF {condition} THEN RAISE EXCEPTION "
            "'IMMUTABLE_HISTORY: exact machine render report required'; "
            "END IF; RETURN NEW; END; $$"
        )


def upgrade():
    install_guards(video=True)


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Video timeline downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        driver = db.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            db.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        op.execute("LOCK TABLE timeline_clips IN ACCESS EXCLUSIVE MODE")
    if db.scalar(
        sa.text(
            "SELECT 1 FROM timeline_clips c "
            "JOIN asset_versions v ON v.id=c.source_asset_version_id "
            "JOIN visual_assets a ON a.id=v.asset_id WHERE a.asset_type='VIDEO' LIMIT 1"
        )
    ):
        raise RuntimeError("Cannot downgrade with video timeline history")
    install_guards(video=False)
