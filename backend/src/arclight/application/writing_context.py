"""Exact, bounded narrative inputs for writing scenes which do not exist yet."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, NoReturn
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.series_planning import validate_planning_source
from arclight.application.story_ledger import get_head, script_source, timestamp, version_approval
from arclight.core.errors import DomainError
from arclight.domain.script_narrative import NarrativeContext, NarrativeRequest
from arclight.domain.series_planning import SeriesOutlineContent
from arclight.domain.story_ledger import entry_visible, read_ledger_content
from arclight.domain.versions import digest
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewRequest
from arclight.persistence.models import (
    AssetVersion,
    Episode,
    Script,
    StoryEntry,
    StoryLedgerVersion,
    StoryOutline,
    VisualAsset,
)

NARRATIVE_SOURCE_KINDS = frozenset(
    {"SERIES_PLAN", "STORY_LEDGER", "WRITING_TARGET", "NARRATIVE_QUERY"}
)
MAX_PROJECTION_BYTES = 65536


def _invalid(message: str = "叙事上下文的精确来源不匹配") -> NoReturn:
    raise DomainError("NARRATIVE_CONTEXT_INVALID", message, 409)


def _plan_proof(row: StoryOutline) -> dict:
    return {
        "kind": "SERIES_PLAN",
        "id": str(row.id),
        "version_no": row.version_no,
        "sha256": digest(row.content_json),
        "source_snapshot_sha256": digest(row.source_snapshot),
        "confirmed_by": row.confirmed_by,
        "confirmed_at": timestamp(row.confirmed_at).isoformat() if row.confirmed_at else None,
        "classification": "PLANNED",
    }


def _ledger_proof(db: Session, row: StoryLedgerVersion) -> dict:
    approval = version_approval(db, row)
    if approval is None:
        _invalid("请选择已批准的精确剧情账本")
    assert approval is not None
    if (
        digest(row.content_json) != row.content_sha256
        or digest(row.source_manifest_json) != row.source_manifest_sha256
    ):
        _invalid()
    return {
        "kind": "STORY_LEDGER",
        "id": str(row.id),
        "version_no": row.version_no,
        "sha256": row.content_sha256,
        "source_manifest_sha256": row.source_manifest_sha256,
        "approval_id": str(approval.id),
        "approved_by": approval.approved_by,
    }


def _head_matches(db: Session, project_id: UUID, request: NarrativeRequest) -> None:
    head = get_head(db, project_id)
    revision = head.revision if head else 0
    approved = head.approved_version_id if head else None
    if revision != request.expected_ledger_head_revision or approved != request.ledger_version_id:
        raise DomainError("VERSION_CONFLICT", "剧情账本基线已变化，请重新预览并确认", 409)


def _exact_plan(db: Session, project_id: UUID, identity: UUID) -> StoryOutline:
    row = db.get(StoryOutline, identity, populate_existing=True)
    if row is None or row.project_id != project_id or row.status != "APPROVED":
        _invalid("请选择同项目已批准的全剧规划")
    assert row is not None
    if row.content_json.get("schema_version") != "2.0":
        _invalid("写作上下文需要 2.0 全剧规划")
    SeriesOutlineContent.model_validate(row.content_json)
    if row.confirmed_by is None or row.confirmed_at is None:
        _invalid()
    return row


def capture_narrative_context(
    db: Session, episode: Episode, actor: str, request: NarrativeRequest
) -> dict:
    """Capture only explicitly selected approved versions; no current pointer is inferred."""
    from arclight.application.commands import require_episode

    episode = require_episode(db, episode.id, actor)
    request = NarrativeRequest.model_validate(request)
    _head_matches(db, episode.project_id, request)
    plan = _exact_plan(db, episode.project_id, request.series_plan_version_id)
    content = SeriesOutlineContent.model_validate(plan.content_json)
    validate_planning_source(db, episode.project_id, content, plan.source_snapshot)
    if episode.episode_no > content.target_episode_count:
        _invalid("目标剧集不在批准的全剧规划范围内")
    if not request.character_ids:
        identities = [arc.character_id for arc in content.series_plan.character_arcs]
        if len(identities) > 50:
            raise DomainError(
                "NARRATIVE_CHARACTERS_REQUIRED", "请为本集明确选择最多 50 名人物", 422
            )
        request = request.model_copy(update={"character_ids": identities})
    for identity in request.character_ids:
        character = db.get(StoryEntry, identity, populate_existing=True)
        if (
            character is None
            or character.project_id != episode.project_id
            or character.kind != "CHARACTER"
            or character.archived
            or identity not in {arc.character_id for arc in content.series_plan.character_arcs}
        ):
            _invalid("知情角色必须是本规划中同项目未归档的人物")
    ledger_source = None
    if request.ledger_version_id is not None:
        ledger = db.get(StoryLedgerVersion, request.ledger_version_id, populate_existing=True)
        if ledger is None or ledger.project_id != episode.project_id:
            _invalid("剧情账本不属于当前项目")
        assert ledger is not None
        ledger_source = _ledger_proof(db, ledger)
    return NarrativeContext(
        **request.model_dump(),
        project_id=episode.project_id,
        episode_id=episode.id,
        episode_no=episode.episode_no,
        plan_sha256=digest(plan.content_json),
        plan_source=_plan_proof(plan),
        ledger_content_sha256=ledger_source["sha256"] if ledger_source else None,
        ledger_source_manifest_sha256=ledger_source["source_manifest_sha256"]
        if ledger_source
        else None,
        ledger_approval_id=UUID(ledger_source["approval_id"]) if ledger_source else None,
        ledger_source=ledger_source,
    ).model_dump(mode="json")


def _validate_frozen_context(
    db: Session, episode: Episode, frozen: NarrativeContext
) -> SeriesOutlineContent:
    if frozen.project_id != episode.project_id or frozen.episode_id != episode.id:
        _invalid("叙事上下文不属于当前剧集")
    plan = _exact_plan(db, episode.project_id, frozen.series_plan_version_id)
    if _plan_proof(plan) != frozen.plan_source or digest(plan.content_json) != frozen.plan_sha256:
        _invalid()
    content = SeriesOutlineContent.model_validate(plan.content_json)
    if not 1 <= frozen.episode_no <= content.target_episode_count:
        _invalid()
    characters = {arc.character_id for arc in content.series_plan.character_arcs}
    if not set(frozen.character_ids) <= characters:
        _invalid()
    if frozen.ledger_version_id is not None:
        ledger = db.get(StoryLedgerVersion, frozen.ledger_version_id)
        if ledger is None or ledger.project_id != episode.project_id:
            _invalid()
        assert ledger is not None
        proof = _ledger_proof(db, ledger)
        if (
            proof != frozen.ledger_source
            or proof["sha256"] != frozen.ledger_content_sha256
            or proof["source_manifest_sha256"] != frozen.ledger_source_manifest_sha256
            or proof["approval_id"] != str(frozen.ledger_approval_id)
        ):
            _invalid()
    return content


def validate_narrative_context(
    db: Session,
    episode: Episode,
    actor: str,
    context: NarrativeContext | dict,
    *,
    check_head: bool = False,
) -> None:
    """Revalidate frozen identities; only a new write/approval checks the moving head."""
    from arclight.application.commands import require_episode

    episode = require_episode(db, episode.id, actor)
    frozen = NarrativeContext.model_validate(context)
    content = _validate_frozen_context(db, episode, frozen)
    if check_head:
        if episode.episode_no != frozen.episode_no:
            _invalid("目标剧集的播放坐标已变化")
        _head_matches(db, episode.project_id, frozen)
        plan = _exact_plan(db, episode.project_id, frozen.series_plan_version_id)
        validate_planning_source(db, episode.project_id, content, plan.source_snapshot)


def narrative_sources(context: NarrativeContext | dict) -> list[dict]:
    frozen = NarrativeContext.model_validate(context).model_dump(mode="json")
    target = {
        "kind": "WRITING_TARGET",
        "id": str(uuid5(UUID(frozen["project_id"]), digest(frozen))),
        "sha256": digest(frozen),
        "context": frozen,
    }
    return [
        copy.deepcopy(frozen["plan_source"]),
        *([copy.deepcopy(frozen["ledger_source"])] if frozen["ledger_source"] else []),
        target,
    ]


@dataclass(frozen=True)
class _WritingTarget:
    """A writing coordinate has no pretend Script or source-scene identity."""

    episode_no: int
    scene_no: int
    story_time: int

    @property
    def presentation(self) -> tuple[int, int, int]:
        return self.episode_no, self.scene_no, 1


def _bounded(value, *, maximum: int = 6, text_max: int = 800):
    if isinstance(value, str):
        return value[:text_max]
    if isinstance(value, list):
        return [_bounded(item, maximum=maximum, text_max=text_max) for item in value[:maximum]]
    if isinstance(value, dict):
        return {
            key: _bounded(item, maximum=maximum, text_max=text_max) for key, item in value.items()
        }
    return value


def _frozen_script(db: Session, project_id: UUID, proof: dict) -> Script:
    """Verify immutable script approval evidence without following mutable current pointers."""
    row = db.get(Script, UUID(proof["script_version_id"]))
    registry = db.get(AssetVersion, UUID(proof["registry_version_id"]))
    asset = db.get(VisualAsset, registry.asset_id) if registry else None
    episode = db.get(Episode, row.episode_id) if row else None
    request = db.get(HumanReviewRequest, UUID(proof["approval_request_id"]))
    decision = db.get(HumanReviewDecision, UUID(proof["approval_decision_id"]))
    if (
        row is None
        or episode is None
        or episode.project_id != project_id
        or str(row.episode_id) != proof["episode_id"]
        or row.version_no != proof["version_no"]
        or digest(row.content_json) != proof["content_sha256"]
        or registry is None
        or registry.id != row.id
        or registry.version_no != row.version_no
        or registry.metadata_json.get("episode_id") != str(row.episode_id)
        or registry.metadata_json.get("content_registry") is not True
        or registry.checksum != proof["registry_checksum"]
        or asset is None
        or asset.project_id != project_id
        or asset.asset_type != "SCRIPT"
        or asset.metadata_json.get("episode_id") != str(row.episode_id)
        or asset.metadata_json.get("content_registry") is not True
        or request is None
        or request.project_id != project_id
        or request.episode_id != row.episode_id
        or request.creation_key != "text:" + str(row.id)
        or request.handler != "TEXT_SCRIPT"
        or request.snapshot_json.get("kind") != "SCRIPT"
        or request.snapshot_json.get("object_id") != str(row.id)
        or request.snapshot_json.get("episode_id") != str(row.episode_id)
        or request.snapshot_json.get("version_id") != str(row.id)
        or request.snapshot_json.get("content_sha256") != proof["content_sha256"]
        or digest(request.snapshot_json) != proof["approval_request_sha256"]
        or request.request_sha256 != proof["approval_request_sha256"]
        or decision is None
        or decision.request_id != request.id
        or decision.actor_id != proof["approved_by"]
        or digest(decision.decision_json) != proof["approval_decision_sha256"]
        or decision.decision_json.get("decision") != "APPROVE"
        or decision.decision_json.get("version_id") != str(row.id)
        or decision.decision_json.get("request_sha256") != request.request_sha256
    ):
        _invalid("远集摘要的精确批准来源不匹配")
    assert row is not None
    checksum = hashlib.sha256(json.dumps(row.content_json, sort_keys=True).encode()).hexdigest()
    if checksum != proof["registry_checksum"]:
        _invalid()
    return row


def _projection(
    db: Session,
    context: NarrativeContext,
    *,
    story_time: int,
    scene_no: int,
    character_id: UUID | None,
    causal_ids: list[UUID],
    limit: int,
    offset: int,
    script_proofs: list[dict] | None = None,
) -> tuple[dict, list[dict]]:
    if (
        type(story_time) is not int
        or not -1_000_000_000 <= story_time <= 1_000_000_000
        or type(scene_no) is not int
        or not 1 <= scene_no <= 50
        or type(limit) is not int
        or not 1 <= limit <= 100
        or type(offset) is not int
        or not 0 <= offset <= 1000
        or not isinstance(causal_ids, list)
        or len(causal_ids) > 50
        or any(not isinstance(identity, UUID) for identity in causal_ids)
    ):
        _invalid("写作目标或分页范围无效")
    plan_row = _exact_plan(db, context.project_id, context.series_plan_version_id)
    plan = SeriesOutlineContent.model_validate(plan_row.content_json)
    target = _WritingTarget(context.episode_no, scene_no, story_time)
    character_ids = {arc.character_id for arc in plan.series_plan.character_arcs}
    character_name: str | None = None
    if character_id is not None:
        if character_id not in character_ids or character_id not in context.character_ids:
            _invalid("角色不属于冻结的写作人物范围")
        if (story_time, scene_no) != (context.target_story_time, context.target_scene_no):
            _invalid("角色查询必须使用冻结的写作时点与场景")
        if script_proofs:
            _invalid("角色查询不能携带作者的远集来源")
        references = plan_row.source_snapshot.get("series_plan_references", {}).get(
            "characters", []
        )
        character_name = next(
            (item.get("name") for item in references if item.get("id") == str(character_id)),
            None,
        )
        if not isinstance(character_name, str) or not character_name.strip():
            _invalid("冻结的规划人物缺少姓名来源")
    groups: dict[str, list] = {"facts": [], "planned": [], "proposals": []}
    ledger = None
    if context.ledger_version_id is not None:
        ledger = db.get(StoryLedgerVersion, context.ledger_version_id)
        assert ledger is not None
        content = read_ledger_content(ledger.content_json)
        mappings = {item.id: item for item in content.scene_times}
        for entry in sorted(content.entries, key=lambda item: (item.story_start, str(item.id))):
            if entry.status == "AUTHOR_PLANNED":
                if character_id is None:
                    groups["planned"].append(entry)
                continue
            if not entry_visible(entry, target, mappings):
                continue
            if character_id is not None and (
                entry.status != "CONFIRMED"
                or not any(
                    item.character_id == character_id
                    and item.story_start <= story_time
                    and (item.story_end is None or story_time < item.story_end)
                    for item in entry.known_by
                )
            ):
                continue
            groups["facts" if entry.status == "CONFIRMED" else "proposals"].append(entry)
    all_visible = {entry.id: entry for values in groups.values() for entry in values}
    if causal_ids:
        if not set(causal_ids) <= all_visible.keys():
            _invalid("因果选择不是当前视角可见的事件")
        relevant = set(causal_ids)
        pending = list(causal_ids)
        while pending:
            item = all_visible[pending.pop()]
            for identity in item.causes:
                if identity in all_visible and identity not in relevant:
                    relevant.add(identity)
                    pending.append(identity)
        groups = {
            key: [entry for entry in values if entry.id in relevant]
            for key, values in groups.items()
        }
    records = [(key, entry) for key, values in groups.items() for entry in values]
    total = len(records)
    selected = records[offset : offset + limit]
    progression = plan.series_plan.episode_progressions[context.episode_no - 1]
    related_nos = set(progression.caused_by_episode_nos) if character_id is None else set()
    if ledger is not None and character_id is None:
        by_script = {
            item["script_version_id"]: item for item in ledger.source_manifest_json["scripts"]
        }
        for _, entry in records:
            proof = by_script.get(str(entry.source_script_version_id))
            if proof and proof["episode_no"] < context.episode_no:
                related_nos.add(proof["episode_no"])
    index_episodes = plan.episodes[offset : offset + limit] if character_id is None else []
    related_page = sorted(related_nos)[offset : offset + limit]
    wanted_nos = {item.episode_no for item in index_episodes} | set(related_page)
    if script_proofs is None:
        script_proofs = []
        if character_id is None:
            episodes = db.scalars(
                select(Episode).where(
                    Episode.project_id == context.project_id,
                    Episode.episode_no.in_(wanted_nos),
                    Episode.status != "ARCHIVED",
                )
            ).all()
            for episode in sorted(episodes, key=lambda item: item.episode_no):
                script = db.scalar(
                    select(Script)
                    .where(Script.episode_id == episode.id, Script.status == "APPROVED")
                    .order_by(Script.version_no.desc())
                    .limit(1)
                )
                if script is None:
                    continue
                try:
                    proof, _ = script_source(db, context.project_id, script.id)
                except DomainError as error:
                    if error.code != "STORY_LEDGER_SOURCE_INVALID":
                        raise
                    continue
                script_proofs.append(proof)
    scripts = {}
    for proof in script_proofs:
        row = _frozen_script(db, context.project_id, proof)
        scripts[proof["episode_no"]] = (row, proof)
    planned: dict[str, Any] | None = None
    if character_id is None:
        relevant_threads = {beat.thread_id for beat in progression.thread_beats}
        planned = {
            "kind": "PLANNED",
            "version_id": str(plan_row.id),
            "sha256": context.plan_sha256,
            "episode": plan.episodes[context.episode_no - 1].model_dump(mode="json"),
            "progression": progression.model_dump(mode="json"),
            "threads": [
                item.model_dump(mode="json")
                for item in plan.series_plan.threads
                if item.id in relevant_threads
            ],
            "character_arcs": [
                item.model_dump(mode="json")
                for item in plan.series_plan.character_arcs
                if not context.character_ids or item.character_id in context.character_ids
            ],
            "relationship_arcs": [
                item.model_dump(mode="json") for item in plan.series_plan.relationship_arcs
            ],
            "foreshadowing": [
                item.model_dump(mode="json")
                for item in plan.series_plan.foreshadowing
                if item.thread_id in relevant_threads
            ],
        }
        for arc in [*planned["character_arcs"], *planned["relationship_arcs"]]:
            milestones = arc["milestones"]
            prior = [item for item in milestones if item["episode_no"] < context.episode_no]
            current = [item for item in milestones if item["episode_no"] == context.episode_no]
            future = [item for item in milestones if item["episode_no"] > context.episode_no]
            arc["milestones"] = [*prior[-1:], *current, *future[:1]]
            arc["milestones_truncated"] = arc["milestones"] != milestones
        bounded = _bounded(planned)
        bounded["truncated"] = bounded != planned or any(
            arc["milestones_truncated"]
            for arc in [*planned["character_arcs"], *planned["relationship_arcs"]]
        )
        bounded["limits"] = {"max_items": 6, "max_text_chars": 800, "milestones_per_arc": 3}
        planned = bounded
    result: dict[str, Any] = {
        "schema_version": "writing-projection/1",
        "target": {
            "label": "WRITING_TARGET",
            "episode_id": str(context.episode_id),
            "episode_no": context.episode_no,
            "scene_no": scene_no,
            "segment_no": 1,
            "story_time": story_time,
        },
        "perspective": "CHARACTER" if character_id else "AUTHOR",
        "character_id": str(character_id) if character_id else None,
        "character_name": character_name,
        "plan": planned,
        "facts": [],
        "planned": [],
        "proposals": [],
        "episode_index": [
            {
                "episode_no": item.episode_no,
                "title": item.title,
                "plan_kind": "PLANNED",
                "approved_script_version_id": str(scripts[item.episode_no][0].id)
                if item.episode_no in scripts
                else None,
            }
            for item in index_episodes
        ],
        "index_total": len(plan.episodes) if character_id is None else 0,
        "index_next_offset": offset + len(index_episodes)
        if character_id is None and offset + len(index_episodes) < len(plan.episodes)
        else None,
        "index_truncated": character_id is None
        and offset + len(index_episodes) < len(plan.episodes),
        "related_total": len(related_nos),
        "related_next_offset": offset + len(related_page)
        if offset + len(related_page) < len(related_nos)
        else None,
        "related_truncated": offset + len(related_page) < len(related_nos),
        "related_missing": [
            {"episode_no": number, "reason": "NO_APPROVED_SCRIPT"}
            for number in related_page
            if number not in scripts
        ],
        "causal_summaries": [
            {
                "episode_no": number,
                "script_version_id": str(scripts[number][0].id),
                "summary": scripts[number][0].content_json["summary"][:2000],
                "truncated": len(scripts[number][0].content_json["summary"]) > 2000,
                "source": copy.deepcopy(scripts[number][1]),
            }
            for number in related_page
            if number in scripts
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "returned_count": len(selected),
        "next_offset": offset + len(selected) if offset + len(selected) < total else None,
        "truncated": offset + len(selected) < total,
    }
    source_budget = (
        len(
            json.dumps(
                {
                    "sources": narrative_sources(context),
                    "context": context.model_dump(mode="json"),
                    "script_sources": script_proofs,
                },
                ensure_ascii=False,
            ).encode()
        )
        + 2500
    )
    while True:
        visible_ids = {entry.id for _, entry in selected}
        for group in groups:
            result[group] = []
        for group, entry in selected:
            value = entry.model_dump(mode="json", exclude={"known_by"})
            value["causes"] = [
                str(identity) for identity in entry.causes if identity in visible_ids
            ]
            result[group].append(value)
        if (
            len(json.dumps(result, ensure_ascii=False).encode())
            <= MAX_PROJECTION_BYTES - source_budget
        ):
            break
        if result["plan"] and result["plan"]["limits"]["max_items"] > 1:
            new_maximum = max(1, result["plan"]["limits"]["max_items"] // 2)
            result["plan"] = _bounded(result["plan"], maximum=new_maximum, text_max=400)
            result["plan"]["limits"] = {
                "max_items": new_maximum,
                "max_text_chars": 400,
                "milestones_per_arc": min(3, new_maximum),
            }
            result["plan"]["truncated"] = True
        elif selected:
            selected.pop()
            result["returned_count"] = len(selected)
            result["next_offset"] = offset + len(selected)
        elif result["causal_summaries"]:
            removed = result["causal_summaries"].pop()
            result["related_next_offset"] = sorted(related_nos).index(removed["episode_no"])
            result["related_truncated"] = True
        elif result["episode_index"]:
            result["episode_index"].pop()
            result["index_next_offset"] = offset + len(result["episode_index"])
            result["index_truncated"] = True
        else:
            raise DomainError("CONTEXT_TOO_LARGE", "相关规划超过写作上下文上限", 422)
        result["truncated"] = True
    return result, script_proofs


def load_narrative_projection(
    db: Session,
    project_id: UUID,
    actor: str,
    frozen_context: NarrativeContext | dict,
    *,
    story_time: int | None = None,
    scene_no: int | None = None,
    character_id: UUID | None = None,
    causal_ids: list[UUID] | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    context = NarrativeContext.model_validate(frozen_context)
    episode = db.get(Episode, context.episode_id)
    if episode is None or context.project_id != project_id:
        _invalid()
    assert episode is not None
    validate_narrative_context(db, episode, actor, context)
    story_time = context.target_story_time if story_time is None else story_time
    scene_no = context.target_scene_no if scene_no is None else scene_no
    query = {
        "story_time": story_time,
        "scene_no": scene_no,
        "character_id": str(character_id) if character_id else None,
        "causal_ids": [str(identity) for identity in (causal_ids or [])],
        "limit": limit,
        "offset": offset,
    }
    result, scripts = _projection(
        db,
        context,
        story_time=story_time,
        scene_no=scene_no,
        character_id=character_id,
        causal_ids=causal_ids or [],
        limit=limit,
        offset=offset,
    )
    result_hash = digest(result)
    proof = {
        "kind": "NARRATIVE_QUERY",
        "id": str(uuid5(project_id, digest({"context": context.model_dump(mode="json"), **query}))),
        "context": context.model_dump(mode="json"),
        "query": query,
        "script_sources": scripts,
        "result_sha256": result_hash,
        "sha256": result_hash,
    }
    result["result_sha256"] = result_hash
    result["sources"] = [*narrative_sources(context), proof]
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_PROJECTION_BYTES:
        raise DomainError("CONTEXT_TOO_LARGE", "含精确来源的写作上下文超过上限，请缩小范围", 422)
    return result


def validate_narrative_source(db: Session, project_id: UUID, source: dict) -> None:
    kind = source["kind"]
    if kind == "SERIES_PLAN":
        row = _exact_plan(db, project_id, UUID(source["id"]))
        if _plan_proof(row) != source:
            _invalid()
    elif kind == "STORY_LEDGER":
        ledger = db.get(StoryLedgerVersion, UUID(source["id"]))
        if ledger is None or ledger.project_id != project_id:
            _invalid()
        assert ledger is not None
        if _ledger_proof(db, ledger) != source:
            _invalid()
    elif kind in {"WRITING_TARGET", "NARRATIVE_QUERY"}:
        context = NarrativeContext.model_validate(source["context"])
        episode = db.get(Episode, context.episode_id)
        if episode is None or context.project_id != project_id or episode.project_id != project_id:
            _invalid()
        _validate_frozen_context(db, episode, context)
        if kind == "WRITING_TARGET":
            if narrative_sources(context)[-1] != source:
                _invalid()
        else:
            query = source["query"]
            result, _ = _projection(
                db,
                context,
                story_time=query["story_time"],
                scene_no=query["scene_no"],
                character_id=UUID(query["character_id"]) if query["character_id"] else None,
                causal_ids=[UUID(value) for value in query["causal_ids"]],
                limit=query["limit"],
                offset=query["offset"],
                script_proofs=source["script_sources"],
            )
            identity = str(
                uuid5(project_id, digest({"context": context.model_dump(mode="json"), **query}))
            )
            if (
                digest(result) != source["result_sha256"]
                or source["sha256"] != source["result_sha256"]
                or source["id"] != identity
            ):
                _invalid()
    else:
        _invalid()
