"""Conservative retention roots, project serialization and retryable cleanup receipts."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from arclight.application.commands import require_project
from arclight.application.uploads import COMPLETION_GRACE
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.persistence.base import utcnow
from arclight.persistence.models import (
    AssetVersion,
    CleanupItem,
    CleanupPlan,
    Project,
    RenderAttempt,
    RenderExecution,
    StoredFile,
    StorySource,
    UploadIntent,
    WorkflowRun,
)
from arclight.storage.cleanup import (
    Snapshot,
    delete_snapshot,
    identity,
    list_page,
    recover_local_snapshot,
)
from arclight.storage.objects import ObjectStorage

DAY = timedelta(days=1)
ORPHAN_RETENTION = timedelta(days=7)
OBSERVATION = timedelta(hours=1)
TERMINAL = {"SUCCEEDED", "REJECTED", "FAILED", "CANCELLED"}


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def serialize_project(db: Session, project_id: UUID, actor: str) -> None:
    # PostgreSQL callers already hold FOR UPDATE. SQLite needs an actual write
    # before checking references, so other project writers cannot commit in the gap.
    if db.get_bind().dialect.name == "sqlite":
        db.execute(update(Project).where(Project.id == project_id).values(name=Project.name))
    # Re-read authorization/archive/delete state after taking SQLite's write gate.
    require_project(db, project_id, actor, write=True, owner=True)


def prefix(settings: Settings, project_id: UUID, namespace: str) -> str:
    if namespace == "SOURCE":
        return f"sources/{project_id}/"
    if namespace == "ASSET":
        base = f"s3/{settings.s3_bucket}/" if settings.object_storage_backend == "s3" else ""
        return f"{base}{project_id}/"
    return f"{namespace.lower()}/{project_id}/"


def managed_target(settings: Settings, project_id: UUID, namespace: str, key: str) -> bool:
    start = prefix(settings, project_id, namespace)
    if not key.startswith(start):
        return False
    parts = key[len(start) :].split("/")
    if len(parts) != (2 if namespace == "INTERMEDIATE" else 1):
        return False
    if namespace == "ASSET":
        if not parts[0].endswith(".image"):
            return False
        parts[0] = parts[0][:-6]
    try:
        return all(str(UUID(part)) == part for part in parts)
    except ValueError:
        return False


def reason(
    db: Session, settings: Settings, project_id: UUID, namespace: str, item: Snapshot, now: datetime
) -> str | None:
    # S3 HEAD timestamps have second precision; retain the whole boundary second.
    modified = utc(item.modified) + timedelta(seconds=1)
    if not managed_target(settings, project_id, namespace, item.key):
        return None
    # Every historical receipt/version is a retention root, regardless of selection,
    # approval, archive, lock, deletion or current dependency usage.
    if (
        db.scalar(select(StoredFile.id).where(StoredFile.object_key == item.key).limit(1))
        or db.scalar(select(AssetVersion.id).where(AssetVersion.object_key == item.key).limit(1))
        or db.scalar(select(StorySource.id).where(StorySource.object_key == item.key).limit(1))
    ):
        return None
    if namespace == "TEMPORARY":
        upload = db.scalar(select(UploadIntent).where(UploadIntent.object_key == item.key))
        if upload is not None:
            if upload.project_id != project_id or upload.bucket != settings.s3_bucket:
                return None
            if now >= max(utc(upload.expires_at) + COMPLETION_GRACE, modified + DAY):
                return "EXPIRED_UPLOAD"
            return None
    if namespace == "INTERMEDIATE":
        run_id = UUID(item.key.split("/")[-2])
        render = db.get(RenderExecution, run_id)
        if render is not None:
            if (
                render.project_id != project_id
                or render.storage_identity != identity(ObjectStorage(settings))
                or render.status not in TERMINAL
                or render.finished_at is None
                or now < max(utc(render.finished_at), modified) + DAY
            ):
                return None
            return "FINISHED_INTERMEDIATE"
        run = db.get(WorkflowRun, run_id)
        if run is not None:
            from arclight.persistence.models import Episode

            episode = db.get(Episode, run.episode_id)
            if (
                episode is None
                or episode.project_id != project_id
                or run.status not in TERMINAL
                or now < max(utc(run.updated_at), modified) + DAY
            ):
                return None
            return "FINISHED_INTERMEDIATE"
    if namespace == "RENDERED":
        attempt = db.scalar(select(RenderAttempt).where(RenderAttempt.output_key == item.key))
        if attempt is not None:
            render = db.get(RenderExecution, attempt.run_id)
            if (
                render is None
                or render.project_id != project_id
                or render.storage_identity != identity(ObjectStorage(settings))
                or render.status not in TERMINAL
                or attempt.status in {"RUNNING", "SUCCEEDED"}
                or attempt.finished_at is None
            ):
                return None
            modified = max(modified, utc(attempt.finished_at))
    return "ORPHAN" if now >= modified + ORPHAN_RETENTION else None


def scan(
    db: Session,
    settings: Settings,
    project_id: UUID,
    actor: str,
    namespace: str,
    limit: int,
    previous: CleanupPlan | None = None,
) -> CleanupPlan:
    serialize_project(db, project_id, actor)
    store = ObjectStorage(settings)
    if previous is not None and (
        previous.project_id != project_id
        or previous.namespace != namespace
        or previous.storage_identity != identity(store)
        or previous.next_cursor is None
    ):
        raise DomainError("CLEANUP_CURSOR_CONFLICT", "分页计划不属于当前存储或已无下一页", 409)
    now = utcnow()
    objects, cursor = list_page(
        store,
        prefix(settings, project_id, namespace),
        previous.next_cursor if previous else None,
        limit,
    )
    candidates = [(row, reason(db, settings, project_id, namespace, row, now)) for row in objects]
    plan = CleanupPlan(
        project_id=project_id,
        namespace=namespace,
        storage_identity=identity(store),
        scanned_at=now,
        execute_after=now + OBSERVATION,
        expires_at=now + DAY,
        next_cursor=cursor,
        scanned_count=len(objects),
        retained_count=sum(why is None for _, why in candidates),
        created_by=actor,
    )
    db.add(plan)
    db.flush()
    for row, why in candidates:
        if why:
            db.add(
                CleanupItem(
                    plan_id=plan.id,
                    object_key=row.key,
                    byte_size=row.size,
                    modified_at=row.modified,
                    object_token=row.token,
                    reason=why,
                )
            )
    db.flush()
    return plan


def execute(
    db: Session, settings: Settings, plan: CleanupPlan, item: CleanupItem, actor: str
) -> CleanupItem:
    serialize_project(db, plan.project_id, actor)
    db.refresh(item, with_for_update=True)
    if item.status in {"DELETED", "ABSENT", "RETAINED"}:
        return item
    now = utcnow()
    if now < utc(plan.execute_after):
        raise DomainError("CLEANUP_OBSERVING", "清理计划仍在一小时观察期内", 409)
    if now >= utc(plan.expires_at):
        raise DomainError("CLEANUP_PLAN_EXPIRED", "清理计划已过期，请重新扫描", 409)
    store = ObjectStorage(settings)
    if plan.storage_identity != identity(store):
        raise DomainError("CLEANUP_STORAGE_CHANGED", "存储配置已改变，请重新扫描", 409)
    expected = Snapshot(item.object_key, item.byte_size, utc(item.modified_at), item.object_token)
    item.attempt_count += 1
    item.error_code = None
    item.status = "RUNNING"
    db.flush()  # Claim stays in this transaction; crash rolls back and retry rechecks.
    try:
        recover_local_snapshot(store, expected)
        if reason(db, settings, plan.project_id, plan.namespace, expected, now) is None:
            item.status = "RETAINED"
        else:
            deleted = delete_snapshot(store, expected)
            item.status = "DELETED" if deleted else "ABSENT"
        item.completed_at = now
    except DomainError as exc:
        item.status = "FAILED"
        item.error_code = exc.code
    db.flush()
    return item
