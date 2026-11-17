"""Serialized story-ledger writes and exact immutable query receipts."""

import copy
import hashlib
import json
from datetime import UTC
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContent
from arclight.api.collaboration import require_idle
from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.production_events import begin_serialized
from arclight.core.errors import DomainError
from arclight.domain.story_ledger import LedgerQuery, StoryLedgerContent, query_ledger
from arclight.domain.versions import digest
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewRequest
from arclight.persistence.models import (
    AssetVersion,
    Episode,
    Script,
    StoryEntry,
    StoryLedgerApproval,
    StoryLedgerHead,
    StoryLedgerVersion,
    VisualAsset,
)


def timestamp(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def head_data(head: StoryLedgerHead | None) -> dict:
    return {
        "revision": head.revision if head else 0,
        "latest_version_id": str(head.latest_version_id)
        if head and head.latest_version_id
        else None,
        "approved_version_id": (
            str(head.approved_version_id) if head and head.approved_version_id else None
        ),
        "approval_id": str(head.approval_id) if head and head.approval_id else None,
    }


def get_head(db: Session, project_id: UUID) -> StoryLedgerHead | None:
    return db.scalar(
        select(StoryLedgerHead)
        .where(StoryLedgerHead.project_id == project_id)
        .execution_options(populate_existing=True)
    )


def approval_data(row: StoryLedgerApproval | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": str(row.id),
        "version_id": str(row.version_id),
        "content_sha256": row.content_sha256,
        "source_manifest_sha256": row.source_manifest_sha256,
        "approved_by": row.approved_by,
        "approved_at": timestamp(row.created_at),
    }


def version_approval(db: Session, row: StoryLedgerVersion) -> StoryLedgerApproval | None:
    approval = db.scalar(
        select(StoryLedgerApproval).where(StoryLedgerApproval.version_id == row.id)
    )
    if approval is not None and (
        approval.project_id != row.project_id
        or approval.content_sha256 != row.content_sha256
        or approval.source_manifest_sha256 != row.source_manifest_sha256
    ):
        raise DomainError("STORY_LEDGER_HISTORY_INVALID", "账本批准凭证不匹配", 409)
    return approval


def version_data(row: StoryLedgerVersion, approval: StoryLedgerApproval | None = None) -> dict:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "version_no": row.version_no,
        "base_version_id": str(row.base_version_id) if row.base_version_id else None,
        "content": copy.deepcopy(row.content_json),
        "content_sha256": row.content_sha256,
        "source_manifest": copy.deepcopy(row.source_manifest_json),
        "source_manifest_sha256": row.source_manifest_sha256,
        "created_by": row.created_by,
        "created_at": timestamp(row.created_at),
        "approval": approval_data(approval),
    }


def version_summary(row: StoryLedgerVersion, approval: StoryLedgerApproval | None = None) -> dict:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "version_no": row.version_no,
        "base_version_id": str(row.base_version_id) if row.base_version_id else None,
        "content_sha256": row.content_sha256,
        "source_manifest_sha256": row.source_manifest_sha256,
        "created_by": row.created_by,
        "created_at": timestamp(row.created_at),
        "approval": approval_data(approval),
    }


def require_version(db: Session, project_id: UUID, version_id: UUID) -> StoryLedgerVersion:
    row = db.get(StoryLedgerVersion, version_id)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "故事账本版本不存在", 404)
    if (
        digest(row.content_json) != row.content_sha256
        or digest(row.source_manifest_json) != row.source_manifest_sha256
    ):
        raise DomainError("STORY_LEDGER_HISTORY_INVALID", "故事账本历史摘要不匹配", 409)
    return row


def source_error():
    return DomainError("STORY_LEDGER_SOURCE_INVALID", "精确来源未批准、已失效或身份不匹配", 409)


def script_source(db: Session, project_id: UUID, script_id: UUID) -> tuple[dict, ScriptContent]:
    script = db.get(Script, script_id, populate_existing=True)
    episode = db.get(Episode, script.episode_id, populate_existing=True) if script else None
    registry = db.get(AssetVersion, script_id, populate_existing=True)
    asset = db.get(VisualAsset, registry.asset_id, populate_existing=True) if registry else None
    if (
        script is None
        or episode is None
        or episode.project_id != project_id
        or episode.status == "ARCHIVED"
        or script.status != "APPROVED"
        or registry is None
        or asset is None
        or asset.project_id != project_id
        or asset.asset_type != "SCRIPT"
        or asset.status in {"ARCHIVED", "STALE"}
        or registry.status != "APPROVED"
        or registry.stale
        or registry.version_no != script.version_no
        or registry.metadata_json.get("episode_id") != str(episode.id)
        or registry.metadata_json.get("content_registry") is not True
        or asset.metadata_json.get("episode_id") != str(episode.id)
        or asset.metadata_json.get("content_registry") is not True
    ):
        raise source_error()
    checksum = hashlib.sha256(json.dumps(script.content_json, sort_keys=True).encode()).hexdigest()
    if registry.checksum != checksum:
        raise source_error()
    request = db.scalar(
        select(HumanReviewRequest).where(
            HumanReviewRequest.project_id == project_id,
            HumanReviewRequest.creation_key == "text:" + str(script.id),
            HumanReviewRequest.handler == "TEXT_SCRIPT",
        )
    )
    decision = (
        db.scalar(select(HumanReviewDecision).where(HumanReviewDecision.request_id == request.id))
        if request
        else None
    )
    if (
        request is None
        or decision is None
        or request.snapshot_json.get("version_id") != str(script.id)
        or request.snapshot_json.get("content_sha256") != digest(script.content_json)
        or request.request_sha256 != digest(request.snapshot_json)
        or decision.decision_json.get("decision") != "APPROVE"
        or decision.decision_json.get("version_id") != str(script.id)
        or decision.decision_json.get("request_sha256") != request.request_sha256
    ):
        raise source_error()
    content = ScriptContent.model_validate(script.content_json)
    return {
        "script_version_id": str(script.id),
        "episode_id": str(episode.id),
        "episode_no": episode.episode_no,
        "version_no": script.version_no,
        "content_sha256": digest(script.content_json),
        "registry_version_id": str(registry.id),
        "registry_checksum": registry.checksum,
        "registry_revision": registry.revision,
        "approval_request_id": str(request.id),
        "approval_request_sha256": request.request_sha256,
        "approval_decision_id": str(decision.id),
        "approval_decision_sha256": digest(decision.decision_json),
        "approved_by": decision.actor_id,
    }, content


def capture_sources(db: Session, project_id: UUID, content: StoryLedgerContent) -> dict:
    scripts: dict[UUID, tuple[dict, ScriptContent]] = {}
    for mapping in content.scene_times:
        if mapping.script_version_id not in scripts:
            scripts[mapping.script_version_id] = script_source(
                db, project_id, mapping.script_version_id
            )
        receipt, script = scripts[mapping.script_version_id]
        scene = next(
            (item for item in script.scenes if str(item.scene_id) == str(mapping.scene_id)), None
        )
        if (
            receipt["episode_id"] != str(mapping.episode_id)
            or receipt["episode_no"] != mapping.episode_no
            or scene is None
            or scene.scene_no != mapping.scene_no
        ):
            raise source_error()
    kinds: dict[UUID, set[str] | None] = {}

    def reference(identity: UUID | None, allowed: set[str] | None = None):
        if identity is None:
            return
        if identity not in kinds:
            kinds[identity] = allowed
        elif allowed is not None:
            previous = kinds[identity]
            kinds[identity] = allowed if previous is None else previous & allowed

    for entry in content.entries:
        expected = (
            {"PROP"}
            if entry.kind == "PROP_OWNERSHIP"
            else {"CHARACTER"}
            if entry.kind != "EVENT"
            else None
        )
        reference(entry.subject_id, expected)
        reference(entry.target_id, {"CHARACTER"} if entry.kind != "EVENT" else None)
        for knowledge in entry.known_by:
            reference(knowledge.character_id, {"CHARACTER"})
    entities = []
    for identity in sorted(kinds, key=str):
        row = db.get(StoryEntry, identity, populate_existing=True)
        allowed = kinds[identity]
        if (
            row is None
            or row.project_id != project_id
            or row.archived
            or (allowed is not None and row.kind not in allowed)
        ):
            raise source_error()
        entities.append(
            {
                "id": str(row.id),
                "kind": row.kind,
                "name": row.name,
                "revision": row.revision,
                "content_sha256": digest(
                    {"kind": row.kind, "name": row.name, "data": row.data_json}
                ),
            }
        )
    return {
        "schema_version": "1.0",
        "scripts": [scripts[identity][0] for identity in sorted(scripts, key=str)],
        "story_entries": entities,
    }


def edit_gate(db: Session, project_id: UUID, actor: str, *, owner: bool = False):
    begin_serialized(db)
    require_project(db, project_id, actor, write=True, owner=owner)


def check_revision(head: StoryLedgerHead | None, expected: int):
    if (head.revision if head else 0) != expected:
        raise DomainError("VERSION_CONFLICT", "故事账本已变化，请刷新后另建版本", 409)


def write_head(
    db: Session, project_id: UUID, head: StoryLedgerHead | None, expected: int, values: dict
) -> StoryLedgerHead:
    if head is None:
        head = StoryLedgerHead(project_id=project_id, revision=1, **values)
        db.add(head)
        db.flush()
    else:
        changed = db.execute(
            update(StoryLedgerHead)
            .where(StoryLedgerHead.id == head.id, StoryLedgerHead.revision == expected)
            .values(revision=expected + 1, **values)
            .returning(StoryLedgerHead.id)
        ).scalar_one_or_none()
        if changed is None:
            raise DomainError("VERSION_CONFLICT", "故事账本已变化，请刷新后另建版本", 409)
        db.refresh(head)
    return head


def create_version(
    db: Session,
    project_id: UUID,
    actor: str,
    expected_revision: int,
    base_version_id: UUID | None,
    content: StoryLedgerContent,
    creation_key: str,
) -> dict:
    edit_gate(db, project_id, actor)
    value = content.model_dump(mode="json")
    fingerprint = digest(
        {
            "actor": actor,
            "expected_revision": expected_revision,
            "base_version_id": str(base_version_id) if base_version_id else None,
            "content": value,
        }
    )
    replay = db.scalar(
        select(StoryLedgerVersion).where(
            StoryLedgerVersion.project_id == project_id,
            StoryLedgerVersion.creation_key == creation_key,
        )
    )
    if replay is not None:
        if replay.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "幂等键已用于不同请求", 409)
        return {"head": copy.deepcopy(replay.receipt_json), "version": version_data(replay)}
    require_idle(db, select(Episode.id).where(Episode.project_id == project_id))
    head = get_head(db, project_id)
    check_revision(head, expected_revision)
    if base_version_id != (head.latest_version_id if head else None):
        raise DomainError("VERSION_CONFLICT", "必须从当前精确账本版本创建替代版本", 409)
    manifest = capture_sources(db, project_id, content)
    identity = uuid4()
    next_no = (
        db.scalar(
            select(func.max(StoryLedgerVersion.version_no)).where(
                StoryLedgerVersion.project_id == project_id
            )
        )
        or 0
    ) + 1
    receipt = head_data(head) | {
        "revision": expected_revision + 1,
        "latest_version_id": str(identity),
    }
    row = StoryLedgerVersion(
        id=identity,
        project_id=project_id,
        version_no=next_no,
        base_version_id=base_version_id,
        creation_key=creation_key,
        request_fingerprint=fingerprint,
        content_json=value,
        content_sha256=digest(value),
        source_manifest_json=manifest,
        source_manifest_sha256=digest(manifest),
        receipt_json=receipt,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    write_head(db, project_id, head, expected_revision, {"latest_version_id": identity})
    audit(
        db,
        project_id,
        actor,
        "story_ledger.version_created",
        identity,
        {
            "version_no": next_no,
            "content_sha256": row.content_sha256,
            "source_manifest_sha256": row.source_manifest_sha256,
            "revision": receipt["revision"],
        },
    )
    return {"head": receipt, "version": version_data(row)}


def approve_version(
    db: Session,
    project_id: UUID,
    version_id: UUID,
    actor: str,
    expected_revision: int,
    creation_key: str,
) -> dict:
    edit_gate(db, project_id, actor, owner=True)
    fingerprint = digest(
        {
            "actor": actor,
            "version_id": str(version_id),
            "expected_revision": expected_revision,
        }
    )
    replay = db.scalar(
        select(StoryLedgerApproval).where(
            StoryLedgerApproval.project_id == project_id,
            StoryLedgerApproval.creation_key == creation_key,
        )
    )
    if replay is not None:
        if replay.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "幂等键已用于不同请求", 409)
        return {"head": copy.deepcopy(replay.receipt_json), "approval": approval_data(replay)}
    require_idle(db, select(Episode.id).where(Episode.project_id == project_id))
    head = get_head(db, project_id)
    check_revision(head, expected_revision)
    row = require_version(db, project_id, version_id)
    if head is None or head.latest_version_id != row.id:
        raise DomainError("VERSION_CONFLICT", "只能批准当前精确账本版本", 409)
    if version_approval(db, row) is not None:
        raise DomainError("ALREADY_APPROVED", "该精确账本版本已经批准", 409)
    current = capture_sources(db, project_id, StoryLedgerContent.model_validate(row.content_json))
    if current != row.source_manifest_json:
        raise source_error()
    identity = uuid4()
    receipt = head_data(head) | {
        "revision": expected_revision + 1,
        "approved_version_id": str(row.id),
        "approval_id": str(identity),
    }
    approval = StoryLedgerApproval(
        id=identity,
        project_id=project_id,
        version_id=row.id,
        creation_key=creation_key,
        request_fingerprint=fingerprint,
        content_sha256=row.content_sha256,
        source_manifest_sha256=row.source_manifest_sha256,
        receipt_json=receipt,
        approved_by=actor,
    )
    db.add(approval)
    db.flush()
    write_head(
        db,
        project_id,
        head,
        expected_revision,
        {
            "approved_version_id": row.id,
            "approval_id": identity,
        },
    )
    audit(
        db,
        project_id,
        actor,
        "story_ledger.approved",
        row.id,
        {
            "approval_id": str(identity),
            "revision": receipt["revision"],
            "content_sha256": row.content_sha256,
            "source_manifest_sha256": row.source_manifest_sha256,
        },
    )
    return {"head": receipt, "approval": approval_data(approval)}


def query_version(
    db: Session,
    project_id: UUID,
    actor: str,
    version_id: UUID,
    query: LedgerQuery,
    limit: int,
    offset: int,
) -> dict:
    require_project(db, project_id, actor)
    row = require_version(db, project_id, version_id)
    content = StoryLedgerContent.model_validate(row.content_json)
    episode = db.get(Episode, query.episode_id)
    script = db.get(Script, query.script_version_id)
    if (
        episode is None
        or episode.project_id != project_id
        or script is None
        or script.episode_id != episode.id
        or not any(
            str(item.scene_id) == str(query.scene_id)
            for item in ScriptContent.model_validate(script.content_json).scenes
        )
    ):
        raise DomainError("STORY_LEDGER_TARGET_INVALID", "目标场景不属于当前项目", 422)
    if query.character_id is not None:
        frozen = next(
            (
                item
                for item in row.source_manifest_json["story_entries"]
                if item["id"] == str(query.character_id)
            ),
            None,
        )
        if frozen is not None:
            valid_character = frozen["kind"] == "CHARACTER"
        else:
            character = db.get(StoryEntry, query.character_id)
            valid_character = (
                character is not None
                and character.project_id == project_id
                and character.kind == "CHARACTER"
            )
        if not valid_character:
            raise DomainError("STORY_LEDGER_CHARACTER_INVALID", "角色不属于当前项目", 422)
    approval = version_approval(db, row)
    bounded_query = query.model_copy(update={"limit": limit, "offset": offset})
    result = query_ledger(content, bounded_query, approved_root=approval is not None)
    approval_id = str(approval.id) if approval else None
    proof = {
        "version_id": str(row.id),
        "content_sha256": row.content_sha256,
        "source_manifest_sha256": row.source_manifest_sha256,
        "approval_id": approval_id,
        "query": bounded_query.model_dump(mode="json"),
        "result_sha256": digest(result),
        "returned_count": result["returned_count"],
        "next_offset": result["next_offset"],
    }
    return result | {
        "version_id": str(row.id),
        "version_no": row.version_no,
        "content_sha256": row.content_sha256,
        "source_manifest_sha256": row.source_manifest_sha256,
        "approval_id": approval_id,
        "query_sha256": digest(proof),
        "result_sha256": digest(result),
    }


def list_sources(
    db: Session,
    project_id: UUID,
    actor: str,
    limit: int,
    offset: int,
    episode_id: UUID | None = None,
) -> dict:
    require_project(db, project_id, actor)
    statement = (
        select(Script, Episode)
        .join(Episode, Episode.id == Script.episode_id)
        .where(
            Episode.project_id == project_id,
            Script.status == "APPROVED",
        )
        .order_by(Episode.episode_no, Script.version_no.desc(), Script.id)
    )
    if episode_id is not None:
        episode = db.get(Episode, episode_id)
        if episode is None or episode.project_id != project_id:
            raise DomainError("NOT_FOUND", "目标分集不存在", 404)
        statement = statement.where(Episode.id == episode_id)
    valid = []
    for script, episode in db.execute(statement):
        try:
            receipt, content = script_source(db, project_id, script.id)
        except DomainError:
            continue
        valid.append(
            {
                "script_version_id": str(script.id),
                "version_no": script.version_no,
                "episode_id": str(episode.id),
                "episode_no": episode.episode_no,
                "episode_title": episode.title,
                "content_sha256": receipt["content_sha256"],
                "registry_version_id": receipt["registry_version_id"],
                "scenes": [
                    {
                        "scene_id": str(scene.scene_id),
                        "scene_no": scene.scene_no,
                        "title": scene.title,
                        "dialogue_count": len(scene.dialogue),
                    }
                    for scene in content.scenes
                ],
            }
        )
    return {
        "items": valid[offset : offset + limit],
        "total": len(valid),
        "limit": limit,
        "offset": offset,
    }
