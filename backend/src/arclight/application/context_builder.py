"""Resolve only a task's bounded, authorized domain context; never trim constraints."""

from copy import deepcopy
from dataclasses import asdict
from typing import get_args
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContent
from arclight.application.commands import require_project
from arclight.application.content_dependencies import reference_ids
from arclight.application.dependencies import exact_version
from arclight.application.generation_budget import usage
from arclight.application.rewrite_content import provider_context, selected_scenes
from arclight.application.story_context import build_story_context
from arclight.application.visual_bible import snapshot as visual_snapshot
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.context import (
    BUILDER_VERSION,
    TASK_CONTRACTS,
    ContextLimits,
    ContextRequest,
    check_size,
)
from arclight.domain.generation_requests import MediaErrorCode
from arclight.domain.project_settings import ProjectStorySettings
from arclight.persistence.models import (
    Episode,
    GenerationAttempt,
    GenerationJob,
    Review,
    Script,
    Shot,
    Storyboard,
    StoryEntry,
    WorkflowRun,
)
from arclight.providers.prompts import TEXT_MAX_TOKENS


def missing(message: str) -> None:
    raise DomainError("CONTEXT_INPUT_MISSING", message, 422)


def load_local(db: Session, project_id: UUID, request: ContextRequest) -> tuple[dict, set[str]]:
    """A shot's neighbors always come from the same exact storyboard, never current pointers."""
    content: dict = {}
    versions = set(map(str, request.asset_version_ids))
    script_id = request.script_version_id
    if request.shot_id is not None:
        shot = db.get(Shot, request.shot_id)
        board = db.get(Storyboard, shot.storyboard_id) if shot else None
        if board is None or board.episode_id != request.episode_id:
            raise DomainError("NOT_FOUND", "上下文镜头不存在", 404)
        assert shot is not None
        if script_id is not None and script_id != board.script_version_id:
            raise DomainError("CONTEXT_SCOPE_MISMATCH", "镜头和剧本必须属于同一精确分镜", 422)
        script_id = board.script_version_id
        if board.status != "APPROVED" or shot.status != "APPROVED":
            raise DomainError("CONTEXT_INPUT_NOT_USABLE", "上下文镜头必须已批准且有效", 409)
        assert shot is not None
        radius = request.limits.neighbor_radius if request.limits else 1
        neighbors = db.scalars(
            select(Shot)
            .where(
                Shot.storyboard_id == board.id,
                Shot.shot_no.between(shot.shot_no - radius, shot.shot_no + radius),
            )
            .order_by(Shot.shot_no)
            .limit(radius * 2 + 1)
        ).all()
        content["shot"] = {"id": str(shot.id), **deepcopy(shot.content_json)}
        content["storyboard_id"] = str(board.id)
        versions.add(str(board.id))
        content["neighbors"] = [
            {"id": str(row.id), **deepcopy(row.content_json)}
            for row in neighbors
            if row.id != shot.id
        ]
        versions.update(reference_ids(content))
    if script_id is not None:
        script = db.get(Script, script_id)
        episode = db.get(Episode, script.episode_id) if script else None
        if episode is None or episode.project_id != project_id or episode.id != request.episode_id:
            raise DomainError("NOT_FOUND", "上下文剧本不存在", 404)
        assert script is not None
        allowed = (
            {"APPROVED", "READY_FOR_REVIEW", "REJECTED"}
            if request.task == "rewrite"
            else {"APPROVED"}
        )
        if script.status not in allowed:
            raise DomainError("CONTEXT_INPUT_NOT_USABLE", "上下文剧本未批准或已失效", 409)
        assert script is not None
        content["script_version_id"] = str(script.id)
        if request.task == "rewrite":
            assert request.rewrite_target is not None
            parsed = ScriptContent.model_validate(script.content_json)
            selection = selected_scenes(parsed, request.rewrite_target)
            content["selection"] = provider_context(parsed, request.rewrite_target)
            versions.update(reference_ids([scene.model_dump(mode="json") for scene in selection]))
            versions.update(
                reference_ids([row.model_dump(mode="json") for row in parsed.character_assets])
            )
        elif request.shot_id is None:
            content["script"] = deepcopy(script.content_json)
            versions.update(reference_ids(script.content_json))
        else:
            # Local visual tasks get relevant scenes, not the whole episode screenplay.
            scene_numbers = {content["shot"]["scene_no"]} | {
                row["scene_no"] for row in content["neighbors"]
            }
            content["scenes"] = [
                deepcopy(scene)
                for scene in script.content_json["scenes"]
                if scene["scene_no"] in scene_numbers
            ]
            versions.update(reference_ids(content["scenes"]))
        # Exact text references are already part of the common asset version registry.
        versions.add(str(script.id))
    return content, versions


def story_entries(
    db: Session,
    project_id: UUID,
    request: ContextRequest,
    local: dict,
    limits: ContextLimits,
    frozen: dict,
) -> dict:
    if request.task == "script" and not request.story_entry_ids:
        result = frozen.get("story_context") or build_story_context(db, project_id)
    else:
        names = set(local.get("script", local.get("selection", {})).get("characters", []))
        for shot in [local.get("shot", {}), *local.get("neighbors", [])]:
            names.update(shot.get("characters", []))
        query = select(StoryEntry).where(StoryEntry.project_id == project_id, ~StoryEntry.archived)
        ids = set(request.story_entry_ids)
        if ids:
            found = set(
                db.scalars(query.where(StoryEntry.id.in_(ids)).with_only_columns(StoryEntry.id))
            )
            if found != ids:
                raise DomainError("NOT_FOUND", "上下文故事设定不存在", 404)
        rows = db.scalars(
            query.where(
                or_(StoryEntry.kind == "WORLD", StoryEntry.name.in_(names), StoryEntry.id.in_(ids))
            )
            .order_by(StoryEntry.id)
            .limit(limits.max_story_entries + 1)
        ).all()
        result = {
            "schema_version": 1,
            "project_id": str(project_id),
            "entries": [
                {
                    "id": str(row.id),
                    "revision": row.revision,
                    "kind": row.kind,
                    "name": row.name,
                    **deepcopy(row.data_json),
                }
                for row in rows
            ],
        }
    if len(result["entries"]) > limits.max_story_entries:
        raise DomainError("CONTEXT_TOO_LARGE", "必需故事设定超过上下文条数限制", 422)
    return result


def resolve_assets(
    db: Session,
    project_id: UUID,
    version_ids: set[str],
    limits: ContextLimits,
    editable_script: UUID | None = None,
    candidate: UUID | None = None,
) -> list[dict]:
    if len(version_ids) > limits.max_assets:
        raise DomainError("CONTEXT_TOO_LARGE", "精确资产引用超过上下文数量限制", 422)
    result = []
    pending = set(version_ids)
    visited: set[str] = set()
    while pending:
        if len(pending | visited) > limits.max_assets:
            raise DomainError("CONTEXT_TOO_LARGE", "关联参考超过上下文数量限制", 422)
        identity = min(pending)
        pending.remove(identity)
        visited.add(identity)
        version, asset = exact_version(db, project_id, UUID(identity))
        if (
            version.stale
            or (
                version.status not in {"APPROVED", "LOCKED"}
                and not (
                    version.id == editable_script
                    and version.status in {"READY_FOR_REVIEW", "REJECTED"}
                )
                and not (
                    version.id == candidate and version.status in {"DRAFT", "READY_FOR_REVIEW"}
                )
            )
            or asset.status in {"ARCHIVED", "REJECTED", "STALE"}
        ):
            raise DomainError("CONTEXT_INPUT_NOT_USABLE", "上下文引用未批准或已失效", 409)
        if version.id == candidate and asset.asset_type not in {"IMAGE", "SHOT_IMAGE"}:
            raise DomainError("CONTEXT_SCOPE_MISMATCH", "审查对象必须是精确图片版本", 422)
        profile = deepcopy(
            asset.character_profile or asset.scene_profile or asset.prop_profile or {}
        )
        images = profile.pop("images", [])
        standard = next(
            (row for row in images if row["id"] == profile.get("standard_image_id")), None
        )
        if standard is not None:
            profile["standard_image"] = standard
        pending.update(reference_ids(profile) - visited)
        result.append(
            {
                "asset_id": str(asset.id),
                "asset_version_id": str(version.id),
                "version_no": version.version_no,
                "checksum": version.checksum,
                "asset_type": asset.asset_type,
                "name": asset.name,
                "directory_revision": asset.revision,
                "locked": asset.status == "LOCKED" or version.status == "LOCKED",
                "purpose": "REVIEW_CANDIDATE" if version.id == candidate else "REFERENCE",
                "profile": profile,
            }
        )
    return result


def relevant_history(
    db: Session, project_id: UUID, episode_id: UUID, local: dict, assets: list[dict], limit: int
) -> list[dict]:
    """Only exact-input reviews and related asset failures, without provider payloads."""
    if limit == 0:
        return []
    rows: list[tuple] = []
    script_id = local.get("script_version_id")
    if script_id:
        reviews = db.scalars(
            select(Review)
            .join(WorkflowRun, Review.run_id == WorkflowRun.id)
            .join(Episode, WorkflowRun.episode_id == Episode.id)
            .where(
                Episode.project_id == project_id,
                Episode.id == episode_id,
                Review.version_id == UUID(script_id),
                Review.resource_type == "script",
            )
            .order_by(Review.created_at.desc(), Review.id.desc())
            .limit(limit)
        )
        rows.extend(
            (
                row.created_at,
                str(row.id),
                {
                    "kind": "REVIEW",
                    "review_id": str(row.id),
                    "decision": row.decision,
                    "comment": row.comment,
                    "version_id": script_id,
                },
            )
            for row in reviews
        )
    identities = [UUID(item["asset_id"]) for item in assets]
    if identities:
        attempts = db.scalars(
            select(GenerationAttempt)
            .join(GenerationJob, GenerationAttempt.job_id == GenerationJob.id)
            .where(
                GenerationJob.project_id == project_id,
                or_(GenerationJob.episode_id == episode_id, GenerationJob.episode_id.is_(None)),
                GenerationJob.target_asset_id.in_(identities),
                GenerationAttempt.status == "FAILED",
            )
            .order_by(GenerationAttempt.created_at.desc(), GenerationAttempt.id.desc())
            .limit(limit)
        )
        rows.extend(
            (
                row.created_at,
                str(row.id),
                {
                    "kind": "FAILED_ATTEMPT",
                    "attempt_id": str(row.id),
                    "job_id": str(row.job_id),
                    "attempt_no": row.attempt_no,
                    "input_manifest_id": str(row.input_manifest_id),
                    "error_code": row.error_code
                    if row.error_code in get_args(MediaErrorCode)
                    else "PROVIDER_FAILED",
                },
            )
            for row in attempts
        )
    return [
        value
        for _, _, value in sorted(rows, key=lambda item: (item[0], item[1]), reverse=True)[:limit]
    ]


def build_context(
    db: Session,
    project_id: UUID,
    actor: str,
    request: ContextRequest,
    settings: Settings,
    *,
    frozen_input: dict | None = None,
    deferred_script_run: UUID | None = None,
) -> dict:
    project = require_project(db, project_id, actor)
    episode = db.get(Episode, request.episode_id)
    if episode is None or episode.project_id != project_id:
        raise DomainError("NOT_FOUND", "上下文剧集不存在", 404)
    if episode.status == "ARCHIVED":
        raise DomainError("CONTEXT_INPUT_NOT_USABLE", "归档剧集不能组装新上下文", 409)
    contract = TASK_CONTRACTS[request.task]
    if (
        (request.asset_version_ids and not contract.assets)
        or (request.story_entry_ids and not contract.entries)
        or (request.script_version_id and not contract.script)
        or (request.shot_id and not contract.shot)
        or (request.rewrite_target and request.task != "rewrite")
        or (request.candidate_version_id and request.task != "review")
    ):
        raise DomainError("CONTEXT_SCOPE_NOT_ALLOWED", "此任务不能读取所选上下文范围", 422)
    if contract.shot and request.shot_id is None:
        missing("此任务必须指定精确镜头")
    if request.task == "review" and request.candidate_version_id is None:
        missing("审查任务必须指定精确候选图片版本")
    if request.task == "rewrite" and request.rewrite_target is None:
        missing("局部改写必须指定允许修改的精确范围")
    if (
        contract.script
        and request.script_version_id is None
        and request.shot_id is None
        and deferred_script_run is None
    ):
        missing("此任务必须指定精确剧本")
    limits = settings.context_limits.restrict(request.limits)
    request = request.model_copy(update={"limits": limits})
    frozen = frozen_input or {}
    local, version_ids = load_local(db, project_id, request)
    if request.candidate_version_id is not None:
        version_ids.add(str(request.candidate_version_id))
        local["candidate_version_id"] = str(request.candidate_version_id)
    brief = ProjectStorySettings.model_validate(project.story_settings).model_dump()
    required: dict = {
        "global": {
            "content_restrictions": brief["content_restrictions"],
            "output_profile": deepcopy(project.output_profile),
        },
        "episode": {
            "id": str(episode.id),
            "episode_no": episode.episode_no,
            "title": episode.title,
            "status": episode.status,
            "target_duration_ms": episode.target_duration_ms,
        },
        "local": local,
    }
    visual = visual_snapshot(db, project) if contract.style else None
    if visual:
        required["global"]["style"] = visual
    if request.task in {"script", "outline", "rewrite"}:
        required["brief"] = deepcopy(frozen.get("story_settings", brief))
    if contract.entries:
        required["story"] = story_entries(db, project_id, request, local, limits, frozen)
        version_ids.update(reference_ids(required["story"]))
    if deferred_script_run is not None:
        required["local"]["deferred_script"] = {
            "run_id": str(deferred_script_run),
            "policy": "EXACT_APPROVED_OUTPUT_OF_THIS_RUN",
        }
    if contract.assets:
        required["assets"] = resolve_assets(
            db,
            project_id,
            version_ids,
            limits,
            request.script_version_id if request.task == "rewrite" else None,
            request.candidate_version_id,
        )
    budget = usage(db, project_id)
    required["constraints"] = {
        "allowed_fields": list(contract.editable),
        "locked_visual_fields": {name: visual["fields"][name] for name in visual["locked_fields"]}
        if visual
        else {},
        "locked_assets": [
            row["asset_version_id"] for row in required.get("assets", []) if row["locked"]
        ],
        "budget": {
            name: str(value) if value is not None else None
            for name, value in budget.items()
            if name
            in {
                "project_limit",
                "task_limit",
                "episode_limit",
                "projected_cost",
                "policy_revision",
                "currency",
            }
        },
        "max_output_tokens": TEXT_MAX_TOKENS,
    }
    if request.rewrite_target is not None:
        required["constraints"]["rewrite_target"] = request.rewrite_target.model_dump(mode="json")
    history = relevant_history(
        db, project_id, episode.id, local, required.get("assets", []), limits.max_history
    )
    result = {
        "schema_version": 1,
        "builder_version": BUILDER_VERSION,
        "task": request.task,
        "project_id": str(project_id),
        "scope": request.model_dump(mode="json", exclude={"limits"}),
        "limits": limits.model_dump(),
        "required": required,
        "history": history,
        "contract": asdict(contract),
    }
    check_size(result, limits)
    return result
