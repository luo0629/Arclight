"""Commit an exact reviewed screenplay and its state changes in one caller transaction."""

import copy
from dataclasses import dataclass
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContentV3, read_script_content
from arclight.application.audit import audit
from arclight.application.commands import require_episode, require_project
from arclight.application.invalidation import invalidate
from arclight.application.narrative_dependencies import NarrativeReplacement
from arclight.application.story_ledger import (
    capture_sources,
    check_revision,
    get_head,
    head_data,
    require_version,
    script_source,
    write_head,
)
from arclight.core.errors import DomainError
from arclight.domain.story_ledger import (
    StoryLedgerContent,
    StoryLedgerContentV2,
    read_ledger_content,
)
from arclight.domain.versions import digest
from arclight.persistence.models import (
    Episode,
    Script,
    StoryEntry,
    StoryLedgerApproval,
    StoryLedgerHead,
    StoryLedgerVersion,
)


def _conflict(message: str) -> DomainError:
    return DomainError("STORY_STATE_CONFLICT", message, 409)


def validate_narrative_script(
    db: Session, episode: Episode, actor: str, content: ScriptContentV3
) -> None:
    from arclight.application.writing_context import validate_narrative_context

    validate_narrative_context(db, episode, actor, content.narrative_context)
    if content.narrative_context.episode_no != episode.episode_no:
        raise _conflict("目标剧集的播放坐标已变化，请重新冻结后另存")
    names = {}
    for identity in content.narrative_context.character_ids:
        row = db.get(StoryEntry, identity, populate_existing=True)
        if (
            row is None
            or row.project_id != episode.project_id
            or row.kind != "CHARACTER"
            or row.archived
        ):
            raise DomainError("NARRATIVE_CHARACTER_INVALID", "叙事人物来源不属于当前可用人物", 422)
        names[identity] = row.name
    for scene in content.scenes:
        objectives = {item.character_id for item in scene.dramatic_beat.character_objectives}
        speakers = {line.speaker for line in scene.dialogue if line.speaker != "旁白"}
        if not speakers <= {names[identity] for identity in objectives}:
            raise DomainError("NARRATIVE_CHARACTER_INVALID", "对白人物缺少精确目的与情绪交付", 422)
        lines = {UUID(line.line_id): line for line in scene.dialogue}
        for interaction in scene.dramatic_beat.dialogue_interactions:
            if any(
                lines[identity].speaker != "旁白"
                and lines[identity].speaker
                not in {names[person] for person in interaction.character_ids}
                for identity in interaction.line_ids
            ):
                raise DomainError(
                    "NARRATIVE_CHARACTER_INVALID", "对白互动未关联该台词的真实人物", 422
                )


def _snapshot(content: ScriptContentV3, script: Script, baseline: StoryLedgerContent) -> dict:
    value = copy.deepcopy(baseline.model_dump(mode="json"))
    value["schema_version"] = "2.0"
    entries = {item["id"]: item for item in value["entries"]}
    context = content.narrative_context
    # A replacement is explicit. Old immutable ledger versions remain untouched.
    for scene in content.scenes:
        mapping_id = str(uuid5(script.id, "story-scene:" + scene.scene_id))
        value["scene_times"].append(
            {
                "id": mapping_id,
                "episode_id": str(script.episode_id),
                "episode_no": context.episode_no,
                "script_version_id": str(script.id),
                "scene_id": scene.scene_id,
                "scene_no": scene.scene_no,
                "segment_no": 1,
                "story_time": scene.dramatic_beat.story_time,
            }
        )
        for proposal in scene.state_changes:
            existing = entries.get(str(proposal.id))
            semantic = proposal.model_dump(mode="json", exclude={"supersedes_entry_id"})
            if (
                existing is not None
                and proposal.supersedes_entry_id is None
                and existing["status"] == "CONFIRMED"
                and existing["source_scene_id"] == scene.scene_id
                and {key: existing[key] for key in semantic} == semantic
            ):
                # An unchanged scene carried through a local rewrite does not
                # submit its already confirmed proposal a second time.
                continue
            if str(proposal.id) in entries and proposal.supersedes_entry_id != proposal.id:
                raise _conflict("已有状态身份只能通过显式精确替代提案保留")
            if proposal.story_start != scene.dramatic_beat.story_time:
                raise _conflict("状态提案生效时间必须属于对应场景的故事时点")
            if proposal.supersedes_entry_id is not None:
                previous = entries.get(str(proposal.supersedes_entry_id))
                if (
                    previous is None
                    or previous["status"] != "CONFIRMED"
                    or previous["kind"] != proposal.kind
                    or previous["subject_id"]
                    != (str(proposal.subject_id) if proposal.subject_id else None)
                    or previous["target_id"]
                    != (str(proposal.target_id) if proposal.target_id else None)
                    or previous["state_key"] != proposal.state_key
                    or previous["story_start"] > proposal.story_start
                    or (
                        previous["story_start"] == proposal.story_start
                        and proposal.id != proposal.supersedes_entry_id
                    )
                    or (
                        previous["story_start"] < proposal.story_start
                        and proposal.id == proposal.supersedes_entry_id
                    )
                    or (
                        previous["story_end"] is not None
                        and previous["story_end"] <= proposal.story_start
                    )
                ):
                    raise _conflict("替代提案不匹配当前有效的精确状态")
                if previous["story_start"] < proposal.story_start:
                    previous["story_end"] = proposal.story_start
            entry = proposal.model_dump(mode="json", exclude={"supersedes_entry_id"}) | {
                "status": "CONFIRMED",
                "source_script_version_id": str(script.id),
                "source_scene_id": scene.scene_id,
                "disclosure_start": mapping_id,
                "disclosure_end": None,
            }
            entries[entry["id"]] = entry
    value["entries"] = list(entries.values())
    try:
        return read_ledger_content(value).model_dump(mode="json")
    except ValueError as error:
        raise _conflict("状态提案存在因果、时序或状态冲突，剧本与事实均未批准") from error


@dataclass(frozen=True)
class _ScriptState:
    head: StoryLedgerHead | None
    base: StoryLedgerVersion | None
    next_id: UUID
    next_no: int
    snapshot: dict


def _prepare_state(
    db: Session, episode: Episode, script: Script, content: ScriptContentV3
) -> _ScriptState:
    context = content.narrative_context
    head = get_head(db, episode.project_id)
    check_revision(head, context.expected_ledger_head_revision)
    if (head.approved_version_id if head else None) != context.ledger_version_id or (
        head and head.latest_version_id != head.approved_version_id
    ):
        raise _conflict("账本基准已有新版本，请重新冻结后另存剧本")
    base = (
        require_version(db, episode.project_id, context.ledger_version_id)
        if context.ledger_version_id
        else None
    )
    baseline = read_ledger_content(base.content_json) if base else StoryLedgerContentV2()
    return _ScriptState(
        head,
        base,
        uuid5(script.id, "approved-story-state"),
        base.version_no + 1 if base else 1,
        _snapshot(content, script, baseline),
    )


def preview_script_state(
    db: Session, episode: Episode, script: Script
) -> NarrativeReplacement | None:
    if script.content_json.get("schema_version") != "3.0":
        return None
    content = ScriptContentV3.model_validate(script.content_json)
    state = _prepare_state(db, episode, script, content)
    # Projection only uses these receipts to associate visible facts with their source episodes.
    # The real approval still captures and validates the new script's human decision and hashes.
    scripts = state.base.source_manifest_json["scripts"] if state.base else []
    row = StoryLedgerVersion(
        id=state.next_id,
        version_no=state.next_no,
        project_id=episode.project_id,
        content_json=state.snapshot,
        source_manifest_json={
            "scripts": [
                *scripts,
                {
                    "script_version_id": str(script.id),
                    "episode_no": episode.episode_no,
                },
            ]
        },
    )
    return NarrativeReplacement(row, episode.episode_no)


def commit_script_state(db: Session, script: Script, actor: str) -> dict | None:
    """Only a persisted, exact human approval can mint the ledger approval below.

    Caller has serialized the existing review and registry and owns commit/rollback.
    Public ledger operations retain their project-idle requirement.
    """
    content = read_script_content(script.content_json)
    if not isinstance(content, ScriptContentV3):
        return None
    episode = require_episode(db, script.episode_id, actor, write=True, owner=True)
    require_project(db, episode.project_id, actor, write=True, owner=True)
    context = content.narrative_context
    from arclight.application.writing_context import validate_narrative_context

    validate_narrative_context(db, episode, actor, context, check_head=False)
    validate_narrative_script(db, episode, actor, content)
    # This validates registry checksum, human request/hash, decision/hash and actor.
    source, _ = script_source(db, episode.project_id, script.id)
    key = "script-state:" + str(script.id)
    fingerprint = digest({"source": source, "context": context.model_dump(mode="json")})
    replay = db.scalar(
        select(StoryLedgerVersion).where(
            StoryLedgerVersion.project_id == episode.project_id,
            StoryLedgerVersion.creation_key == key,
        )
    )
    if replay is not None:
        if replay.request_fingerprint != fingerprint:
            raise _conflict("同一剧本的状态提交来源发生变化")
        return copy.deepcopy(replay.receipt_json)
    validate_narrative_context(db, episode, actor, context, check_head=True)
    state = _prepare_state(db, episode, script, content)
    head, snapshot = state.head, state.snapshot
    manifest = capture_sources(
        db, episode.project_id, read_ledger_content(snapshot), approved_base=state.base
    )
    version_id, approval_id = state.next_id, uuid4()
    receipt = head_data(head) | {
        "revision": context.expected_ledger_head_revision + 1,
        "latest_version_id": str(version_id),
        "approved_version_id": str(version_id),
        "approval_id": str(approval_id),
        "script_version_id": str(script.id),
        "script_content_sha256": digest(script.content_json),
        "proposal_sha256": digest(
            [
                {
                    "scene_id": scene.scene_id,
                    "changes": [p.model_dump(mode="json") for p in scene.state_changes],
                }
                for scene in content.scenes
            ]
        ),
        "approval_decision_id": source["approval_decision_id"],
    }
    row = StoryLedgerVersion(
        id=version_id,
        project_id=episode.project_id,
        version_no=state.next_no,
        base_version_id=context.ledger_version_id,
        creation_key=key,
        request_fingerprint=fingerprint,
        content_json=snapshot,
        content_sha256=digest(snapshot),
        source_manifest_json=manifest,
        source_manifest_sha256=digest(manifest),
        receipt_json=receipt,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    db.add(
        StoryLedgerApproval(
            id=approval_id,
            project_id=episode.project_id,
            version_id=version_id,
            creation_key=key,
            request_fingerprint=fingerprint,
            content_sha256=row.content_sha256,
            source_manifest_sha256=row.source_manifest_sha256,
            receipt_json=receipt,
            approved_by=actor,
        )
    )
    db.flush()
    write_head(
        db,
        episode.project_id,
        head,
        context.expected_ledger_head_revision,
        {
            "latest_version_id": version_id,
            "approved_version_id": version_id,
            "approval_id": approval_id,
        },
    )
    event, _ = invalidate(
        db,
        episode.project_id,
        actor,
        uuid5(row.id, "story-ledger-approved"),
        [],
        "前集剧本批准并提交剧情事实",
        narrative=NarrativeReplacement(row, episode.episode_no),
    )
    audit(
        db,
        episode.project_id,
        actor,
        "script.state_committed",
        script.id,
        receipt | {"dependency_invalidation_id": str(event.id)},
    )
    return receipt
