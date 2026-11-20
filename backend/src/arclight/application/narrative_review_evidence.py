"""Freeze bounded, explicit narrative sources; never infer a latest screenplay."""

import hashlib
import json
from typing import NoReturn
from uuid import UUID, uuid5

from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContentV3, read_script_content
from arclight.application.commands import require_episode, require_project
from arclight.application.dependencies import exact_version
from arclight.application.writing_context import capture_narrative_context, replay_projection
from arclight.core.errors import DomainError
from arclight.domain.narrative_review import (
    NarrativeAssessment,
    NarrativeEvidence,
    NarrativeLocation,
    NarrativeReviewCreate,
    NarrativeReviewWork,
)
from arclight.domain.script_narrative import NarrativeContext, NarrativeRequest
from arclight.domain.series_planning import SeriesOutlineContent
from arclight.domain.versions import digest
from arclight.persistence.models import Script, StoryOutline

MAX_REVIEW_BYTES = 384 * 1024


def invalid(message: str) -> NoReturn:
    raise DomainError("NARRATIVE_REVIEW_INPUT_INVALID", message, 409)


def evidence(
    project_id: UUID, kind: str, location: NarrativeLocation | None, value: dict
) -> NarrativeEvidence:
    data = {
        "kind": kind,
        "location": location.model_dump(mode="json") if location else None,
        "value": value,
    }
    return NarrativeEvidence.model_validate({"id": uuid5(project_id, digest(data)), **data})


def plan_excerpt(plan: SeriesOutlineContent, numbers: set[int], characters: set[UUID]) -> dict:
    """Selected progression and bounded adjacent milestones, not a whole-season dump."""
    progressions = [
        item for item in plan.series_plan.episode_progressions if item.episode_no in numbers
    ]
    thread_ids = {beat.thread_id for item in progressions for beat in item.thread_beats}
    clues = [
        item
        for item in plan.series_plan.foreshadowing
        if (item.thread_id in thread_ids or item.expected_payoff_episode_no <= max(numbers))
        and item.setup_episode_no <= max(numbers)
    ]
    thread_ids.update(item.thread_id for item in clues)

    def arc(value) -> dict:
        result = value.model_dump(mode="json")
        result["milestones"].sort(key=lambda item: item["episode_no"])
        previous = [item for item in result["milestones"] if item["episode_no"] < min(numbers)]
        selected = [item for item in result["milestones"] if item["episode_no"] in numbers]
        following = [item for item in result["milestones"] if item["episode_no"] > max(numbers)]
        result["milestones"] = [*previous[-1:], *selected, *following[:1]]
        result["milestone_coverage"] = "PREVIOUS_SELECTED_NEXT"
        return result

    return {
        "kind": "PLANNED",
        "target_episode_count": plan.target_episode_count,
        "selected_episode_nos": sorted(numbers),
        "coverage": "EXPLICIT_RELEVANT_PLAN_SUBSET",
        "episodes": [
            item.model_dump(mode="json") for item in plan.episodes if item.episode_no in numbers
        ],
        "progressions": [item.model_dump(mode="json") for item in progressions],
        "threads": [
            item.model_dump(mode="json")
            for item in plan.series_plan.threads
            if item.id in thread_ids
        ],
        "character_arcs": [
            arc(item) for item in plan.series_plan.character_arcs if item.character_id in characters
        ],
        "relationship_arcs": [
            arc(item)
            for item in plan.series_plan.relationship_arcs
            if {item.source_character_id, item.target_character_id} & characters
        ],
        "foreshadowing": [
            item.model_dump(mode="json")
            | {"payoff_due": item.expected_payoff_episode_no <= max(numbers)}
            for item in clues
        ],
    }


def capture_review(
    db: Session, project_id: UUID, run_id: UUID, actor: str, request: NarrativeReviewCreate
) -> NarrativeReviewWork:
    require_project(db, project_id, actor, write=True)
    plan_row = db.get(StoryOutline, request.baseline.series_plan_version_id, populate_existing=True)
    if plan_row is None or plan_row.project_id != project_id or plan_row.status != "APPROVED":
        invalid("审查需要同项目已批准的精确全剧规划")
    assert plan_row is not None
    if plan_row.content_json.get("schema_version") != "2.0":
        raise DomainError("NARRATIVE_REVIEW_SCHEMA_REQUIRED", "叙事审查需要 2.0 全剧规划", 422)
    plan = SeriesOutlineContent.model_validate(plan_row.content_json)
    items: list[NarrativeEvidence] = []
    byte_count = 0
    character_ids: set[UUID] = set()

    def append(item: NarrativeEvidence) -> None:
        nonlocal byte_count
        byte_count += len(item.model_dump_json().encode())
        if byte_count > MAX_REVIEW_BYTES or len(items) >= 4096:
            raise DomainError(
                "CONTEXT_TOO_LARGE", "审查证据超过有界上限，请缩小范围；未完成全剧审查", 422
            )
        items.append(item)

    limitations = []
    if request.baseline.ledger_version_id is None:
        limitations.append("未提供批准的剧情事实账本；规划和送审稿不能代替已确认事实。")
    episode_nos = []
    for target in request.targets:
        script = db.get(Script, target.script_version_id, populate_existing=True)
        if script is None:
            invalid("精确剧本不存在")
        assert script is not None
        episode = require_episode(db, script.episode_id, actor, write=True)
        if episode.project_id != project_id or script.status not in {
            "DRAFT",
            "READY_FOR_REVIEW",
            "APPROVED",
            "LOCKED",
        }:
            invalid("剧本不属于审查项目或已经失效")
        version, asset = exact_version(db, project_id, script.id)
        if (
            version.stale
            or asset.asset_type != "SCRIPT"
            or version.metadata_json.get("content_registry") is not True
            or version.checksum
            != hashlib.sha256(json.dumps(script.content_json, sort_keys=True).encode()).hexdigest()
        ):
            invalid("剧本的精确内容登记已经失效")
        content = read_script_content(script.content_json)
        if not isinstance(content, ScriptContentV3):
            raise DomainError(
                "NARRATIVE_REVIEW_SCHEMA_REQUIRED",
                "叙事审查需要带故事时点的 3.0 剧本；旧稿须先显式补齐时点",
                422,
            )
        selected = [
            scene
            for scene in content.scenes
            if not target.scene_ids or UUID(scene.scene_id) in target.scene_ids
        ]
        if len(selected) != (len(target.scene_ids) if target.scene_ids else len(content.scenes)):
            invalid("送审场景不属于精确剧本")
        episode_nos.append(episode.episode_no)
        locations = {}
        for scene in selected:
            character_ids.update(
                item.character_id for item in scene.dramatic_beat.character_objectives
            )
            location = NarrativeLocation(
                episode_id=episode.id,
                episode_no=episode.episode_no,
                script_version_id=script.id,
                scene_id=UUID(scene.scene_id),
            )
            locations[scene.scene_no] = location
            append(
                evidence(
                    project_id,
                    "SCENE",
                    location,
                    {
                        "scene": scene.model_dump(mode="json"),
                        "script_sha256": digest(script.content_json),
                        "script_status": script.status,
                        "registry_checksum": version.checksum,
                        "registry_revision": version.revision,
                    },
                )
            )
            context = NarrativeContext.model_validate(
                capture_narrative_context(
                    db,
                    episode,
                    actor,
                    NarrativeRequest(
                        **request.baseline.model_dump(),
                        target_story_time=scene.dramatic_beat.story_time,
                        target_scene_no=scene.scene_no,
                        character_ids=[
                            item.character_id for item in scene.dramatic_beat.character_objectives
                        ],
                    ),
                )
            )
            for character_id in [None, *context.character_ids]:
                projection = replay_projection(
                    db,
                    context,
                    {
                        "story_time": context.target_story_time,
                        "scene_no": scene.scene_no,
                        "character_id": str(character_id) if character_id else None,
                        "causal_ids": [],
                        "limit": 100,
                        "offset": 0,
                    },
                    [],
                )
                append(
                    evidence(
                        project_id,
                        "CHARACTER_STATE" if character_id else "AUTHOR_STATE",
                        location,
                        {"context": context.model_dump(mode="json"), "projection": projection},
                    )
                )
                if (
                    projection["truncated"]
                    or projection["index_truncated"]
                    or projection["related_truncated"]
                    or (projection["plan"] and projection["plan"]["truncated"])
                ):
                    limitations.append(
                        f"第{episode.episode_no}集场景{scene.scene_no}的时点查询存在有界覆盖限制。"
                    )
                if projection["related_missing"]:
                    limitations.append(
                        f"第{episode.episode_no}集场景{scene.scene_no}未提供全部因果前集的批准全文；不能推定其内容。"
                    )
        from arclight.application.narrative_review_media import capture_media

        media, gaps = capture_media(db, project_id, actor, target, locations)
        for item in media:
            append(item)
        limitations.extend(gaps)
    if len(episode_nos) != len(set(episode_nos)):
        invalid("一次审查每集只能选择一个剧本版本")
    numbers = sorted(episode_nos)
    if request.scope == "SERIES" and numbers != list(range(1, plan.target_episode_count + 1)):
        invalid("全剧审查必须覆盖批准规划的全部剧集，不能以部分集代替")
    if request.scope == "ADJACENT_EPISODES" and numbers[1] != numbers[0] + 1:
        invalid("相邻集审查必须选择连续两集")
    # Plans are author-facing, explicitly not confirmed events or a character's knowledge.
    append(
        evidence(
            project_id,
            "PLAN",
            None,
            {
                "version_id": str(plan_row.id),
                "classification": "PLANNED",
                "sha256": digest(plan_row.content_json),
                "source_sha256": digest(plan_row.source_snapshot),
                "content": plan_excerpt(plan, set(episode_nos), character_ids),
            },
        ),
    )
    items = [items[-1], *items[:-1]]
    if len(set(limitations)) > 499:
        raise DomainError(
            "CONTEXT_TOO_LARGE", "审查来源数量超过上限，请缩小范围；未完成全剧审查", 422
        )
    work = NarrativeReviewWork(
        project_id=project_id,
        run_id=run_id,
        request=request,
        evidence=items,
        limitations=list(dict.fromkeys(limitations)),
    )
    if len(work.model_dump_json().encode()) > MAX_REVIEW_BYTES:
        raise DomainError(
            "CONTEXT_TOO_LARGE", "审查证据超过 384 KiB，请分场景／相邻集审查；未完成全剧审查", 422
        )
    return work


def validate_assessment(work: NarrativeReviewWork, assessment: NarrativeAssessment) -> None:
    """A model may only cite obtained evidence and actually observed sample locations."""
    by_id = {item.id: item for item in work.evidence}
    scenes = {item.location for item in work.evidence if item.kind == "SCENE"}
    for check in assessment.checks:
        if not set(check.evidence_ids) <= by_id.keys():
            raise DomainError("NARRATIVE_REVIEW_CITATION_INVALID", "模型引用了未取得的证据", 409)
        for issue in check.issues:
            location = issue.location
            base = location.model_copy(
                update={"line_id": None, "shot_id": None, "candidate_id": None, "time_ms": None}
            )
            if base not in scenes or not set(issue.evidence_ids) <= by_id.keys():
                raise DomainError(
                    "NARRATIVE_REVIEW_CITATION_INVALID", "问题引用的场景或事实不存在", 409
                )
            cited = [by_id[identity] for identity in issue.evidence_ids]
            local = [
                item
                for item in cited
                if item.location is not None
                and item.location.model_copy(
                    update={"line_id": None, "shot_id": None, "candidate_id": None, "time_ms": None}
                )
                == base
            ]
            if not local:
                raise DomainError(
                    "NARRATIVE_REVIEW_CITATION_INVALID", "问题必须引用其定位场景的实际证据", 409
                )
            source = next(
                item for item in work.evidence if item.kind == "SCENE" and item.location == base
            )
            if location.line_id is not None and str(location.line_id) not in {
                line["line_id"] for line in source.value["scene"]["dialogue"]
            }:
                raise DomainError(
                    "NARRATIVE_REVIEW_CITATION_INVALID", "问题台词不属于当前精确场景", 409
                )
            if location.shot_id is not None and not any(
                item.location and item.location.shot_id == location.shot_id for item in local
            ):
                raise DomainError(
                    "NARRATIVE_REVIEW_CITATION_INVALID", "问题镜头没有本次取得的证据", 409
                )
            if location.candidate_id is not None and not any(
                item.kind == "VIDEO"
                and item.location
                and item.location.candidate_id == location.candidate_id
                and item.location.shot_id == location.shot_id
                and (
                    location.time_ms is None
                    or any(
                        sample["time_ms"] == location.time_ms
                        for sample in item.value["report"]["samples"]
                    )
                )
                for item in local
            ):
                raise DomainError(
                    "NARRATIVE_REVIEW_CITATION_INVALID", "问题时间码不是实际取得的候选样本", 409
                )
