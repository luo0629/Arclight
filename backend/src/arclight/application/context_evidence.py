"""Append bounded retrieval rounds to immutable startup manifests."""

from collections.abc import Callable
from copy import deepcopy
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_episode, require_project
from arclight.application.content_dependencies import reference_ids
from arclight.application.context_builder import resolve_assets
from arclight.application.context_snapshots import manifest_references, read_contexts
from arclight.application.context_sources import (
    asset_sources,
    source_snapshot,
    validate_sources,
    verify_images,
)
from arclight.application.source_objects import source_text
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_memory import MAX_MEMORY_SOURCES, MEMORY_VERSION
from arclight.domain.context import TASK_CONTRACTS, ContextLimits, check_size
from arclight.domain.context_trace import MAX_EVIDENCE_ROUNDS, TRACE_VERSION, EvidenceRequest
from arclight.domain.versions import digest
from arclight.persistence.models import DependencyManifest, StoryEntry, StorySource, WorkflowRun


def root_manifest(db: Session, run: WorkflowRun, stage: str, actor: str) -> DependencyManifest:
    episode = require_episode(db, run.episode_id, actor)
    read_contexts(db, episode.project_id, run)
    references = manifest_references(db, episode.project_id, run.id)
    if not references or stage not in references:
        raise DomainError("CONTEXT_TRACE_UNAVAILABLE", "本运行未保存该阶段的启动上下文", 409)
    root = db.get(DependencyManifest, UUID(references[stage]))
    assert root is not None
    return root


def round_key(root_id: UUID, number: int) -> UUID:
    return uuid5(root_id, "context-evidence:" + str(number))


def evidence_rounds(db: Session, root: DependencyManifest) -> list[DependencyManifest]:
    keys = [round_key(root.id, number) for number in range(1, MAX_EVIDENCE_ROUNDS + 1)]
    rows = db.scalars(
        select(DependencyManifest).where(
            DependencyManifest.project_id == root.project_id,
            DependencyManifest.creation_key.in_(keys),
        )
    ).all()
    found = {row.creation_key: row for row in rows}
    result = []
    parent = root.id
    for number, key in enumerate(keys[: len(rows)], 1):
        row = found.get(key)
        if row is None or digest(row.snapshot_json) != row.request_fingerprint:
            raise DomainError("CONTEXT_REFERENCE_INVALID", "检索证据链缺失或校验失败", 409)
        value = row.snapshot_json.get("context", {})
        if (
            value.get("origin") != "context_evidence"
            or value.get("startup_manifest_id") != str(root.id)
            or value.get("parent_manifest_id") != str(parent)
            or value.get("round") != number
            or value.get("trace_version") != TRACE_VERSION
            or value.get("run_id") != root.snapshot_json["context"]["run_id"]
            or value.get("stage") != root.snapshot_json["context"]["task_context"]["task"]
            or row.id != key
        ):
            raise DomainError("CONTEXT_REFERENCE_INVALID", "检索证据链范围不匹配", 409)
        validate_sources(db, root.project_id, value["sources"])
        result.append(row)
        parent = row.id
    return result


def bounded_trace(
    root: DependencyManifest, rounds: list[DependencyManifest], receipt: dict | None = None
) -> dict:
    initial = root.snapshot_json["context"]
    context = initial["task_context"]
    result = {
        "run_id": initial["run_id"],
        "stage": context["task"],
        "startup_manifest_id": str(root.id),
        "trace_available": "trace" in initial,
        "task_context": deepcopy(context),
        "startup": deepcopy(initial.get("trace")),
        "evidence": [
            {"manifest_id": str(row.id), **deepcopy(row.snapshot_json["context"])} for row in rounds
        ],
        "consumption": deepcopy(receipt),
    }
    limits = ContextLimits.model_validate(context["limits"])
    check_size(result, limits)
    all_sources = [*(initial.get("trace") or {}).get("sources", [])]
    for row in rounds:
        all_sources.extend(row.snapshot_json["context"]["sources"])
    if receipt and receipt.get("resolved_input"):
        all_sources.extend(receipt["resolved_input"]["sources"])
    if (initial.get("trace") or {}).get("memory_version") == MEMORY_VERSION and len(
        all_sources
    ) > MAX_MEMORY_SOURCES:
        raise DomainError("AGENT_MEMORY_LIMIT", "必需记忆来源超过数量限额，不能裁剪", 422)
    for kind, field, maximum in (
        ("ASSET_VERSION", "asset_version_id", limits.max_assets),
        ("STORY_ENTRY", "id", limits.max_story_entries),
        ("STORY_SOURCE", "source_id", 16),
    ):
        if len({item[field] for item in all_sources if item["kind"] == kind}) > maximum:
            raise DomainError("CONTEXT_TOO_LARGE", "累计精确证据超过上下文数量限制", 422)
    return result


def read_trace(
    db: Session, run: WorkflowRun, stage: str, actor: str, through_round: int | None = None
) -> dict:
    root = root_manifest(db, run, stage, actor)
    rounds = evidence_rounds(db, root)
    if through_round is not None:
        if through_round > len(rounds):
            raise DomainError("CONTEXT_REFERENCE_INVALID", "指定检索轮次不存在", 409)
        rounds = rounds[:through_round]
    from arclight.application.context_consumption import consumption

    return bounded_trace(root, rounds, consumption(db, root))


def append_evidence(
    db: Session,
    run: WorkflowRun,
    stage: str,
    actor: str,
    request: EvidenceRequest,
    settings: Settings,
) -> tuple[DependencyManifest, bool]:
    def resolve(root, rounds, limits):
        return _resolve_evidence(db, root, stage, request, settings, limits)

    return append_resolved_evidence(
        db,
        run,
        stage,
        actor,
        creation_key=request.creation_key,
        expected_round=request.expected_round,
        request_sha256=digest(request.model_dump(mode="json")),
        resolve=resolve,
    )


def _resolve_evidence(
    db: Session,
    root: DependencyManifest,
    stage: str,
    request: EvidenceRequest,
    settings: Settings,
    limits: ContextLimits,
) -> dict:
    contract = TASK_CONTRACTS[stage]
    if (
        (request.asset_version_ids and not contract.assets)
        or (request.story_entry_ids and not contract.entries)
        or (request.source_ids and stage not in {"analyze", "outline", "script", "rewrite"})
    ):
        raise DomainError("CONTEXT_SCOPE_NOT_ALLOWED", "当前任务不允许此检索范围", 422)
    sources = []
    entries = []
    for identity in request.story_entry_ids:
        story_entry = db.get(StoryEntry, identity)
        if story_entry is None or story_entry.project_id != root.project_id or story_entry.archived:
            raise DomainError("NOT_FOUND", "检索故事设定不存在", 404)
        entry = {
            "id": str(story_entry.id),
            "revision": story_entry.revision,
            "kind": story_entry.kind,
            "name": story_entry.name,
            **deepcopy(story_entry.data_json),
        }
        entries.append(entry)
        sources.append(
            {
                "kind": "STORY_ENTRY",
                "id": str(story_entry.id),
                "revision": story_entry.revision,
                "sha256": digest(entry),
            }
        )
    assets = resolve_assets(
        db,
        root.project_id,
        set(map(str, request.asset_version_ids)) | reference_ids(entries),
        limits,
    )
    sources.extend(asset_sources(db, root.project_id, assets))
    verify_images(db, settings, root.project_id, sources)
    stories = []
    for identity in request.source_ids:
        source = source_snapshot(db, root.project_id, identity)
        story_source = db.get(StorySource, identity)
        assert story_source is not None
        stories.append(
            {"source_id": str(identity), "text": source_text(db, settings, story_source)}
        )
        sources.append(source)
    return {"sources": sources, "assets": assets, "story_entries": entries, "stories": stories}


def append_resolved_evidence(
    db: Session,
    run: WorkflowRun,
    stage: str,
    actor: str,
    *,
    creation_key: UUID,
    expected_round: int,
    request_sha256: str,
    resolve: Callable[[DependencyManifest, list[DependencyManifest], ContextLimits], dict],
) -> tuple[DependencyManifest, bool]:
    """Append server-collected materials through the original lock, chain and memory gates.

    The resolver runs under the project write gate, after idempotency validation.
    It is application code, never a client-supplied source or confirmation payload.
    """
    episode = require_episode(db, run.episode_id, actor, write=True)
    # Existing writers use the same project gate, serializing round allocation.
    require_project(db, episode.project_id, actor, write=True)
    root = root_manifest(db, run, stage, actor)
    if "trace" not in root.snapshot_json["context"]:
        raise DomainError("CONTEXT_TRACE_UNAVAILABLE", "旧运行不能补造启动来源", 409)
    rounds = evidence_rounds(db, root)
    for previous in rounds:
        value = previous.snapshot_json["context"]
        if value["creation_key"] == str(creation_key):
            if value["request_sha256"] != request_sha256:
                raise DomainError("IDEMPOTENCY_CONFLICT", "同一检索键不能用于不同输入", 409)
            return previous, False
    if len(rounds) != expected_round:
        raise DomainError("CONTEXT_ROUND_CONFLICT", "检索轮次已变化，请刷新后追加", 409)
    context = root.snapshot_json["context"]["task_context"]
    limits = ContextLimits.model_validate(context["limits"])
    number = len(rounds) + 1
    if number > MAX_EVIDENCE_ROUNDS:
        raise DomainError("CONTEXT_TOO_LARGE", "检索轮次达到上限", 422)
    collected = resolve(root, rounds, limits)
    sources = collected["sources"]
    validate_sources(db, root.project_id, sources)
    value = {
        "schema_version": 1,
        "target": deepcopy(root.snapshot_json["target"]),
        "inputs": [
            {
                "source_asset_version_id": item["asset_version_id"],
                "asset_id": item["asset_id"],
                "checksum": item["checksum"],
                "dependency_type": "REFERENCE_DEPENDENCY",
                "selectors": [],
            }
            for item in collected.get("assets", [])
        ],
        "context": {
            "origin": "context_evidence",
            "trace_version": TRACE_VERSION,
            "run_id": str(run.id),
            "stage": stage,
            "startup_manifest_id": str(root.id),
            "parent_manifest_id": str(rounds[-1].id if rounds else root.id),
            "round": number,
            "creation_key": str(creation_key),
            "request_sha256": request_sha256,
            **collected,
        },
    }
    from arclight.application.agent_memory_sources import capture_memory_sources

    value["context"].update(capture_memory_sources(db, root.project_id, sources))
    row = DependencyManifest(
        id=round_key(root.id, number),
        project_id=root.project_id,
        target_asset_id=None,
        creation_key=round_key(root.id, number),
        created_by=actor,
        request_fingerprint=digest(value),
        snapshot_json=value,
    )
    from arclight.application.agent_memory import project_agent_memory
    from arclight.application.context_consumption import consumption

    project_agent_memory(run, bounded_trace(root, [*rounds, row], consumption(db, root)))
    db.add(row)
    db.flush()
    audit(
        db,
        root.project_id,
        actor,
        "context.evidence_appended",
        run.id,
        {
            "manifest_id": str(row.id),
            "startup_manifest_id": str(root.id),
            "round": number,
            "stage": stage,
        },
    )
    return row, True
