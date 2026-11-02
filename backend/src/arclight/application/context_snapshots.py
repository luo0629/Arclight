"""Freeze task contexts in the existing immutable manifest, referenced by each run."""

from copy import deepcopy
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.context_builder import build_context
from arclight.application.dependencies import lock_dependency_graph
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.context import (
    BUILDER_VERSION,
    ContextLimits,
    ContextRequest,
    check_call,
    check_size,
)
from arclight.domain.versions import digest
from arclight.persistence.models import (
    DependencyManifest,
    Episode,
    Project,
    VersionBinding,
    WorkflowRun,
)

STAGES = {
    "script_storyboard": ("script", "storyboard"),
    "storyboard": ("storyboard",),
    "script_rewrite": ("rewrite",),
    "story_analysis": ("analyze",),
    "story_outline": ("outline",),
}


def startup_input(run: WorkflowRun, stage: str) -> dict:
    inputs = run.input_json
    if stage == "analyze":
        return {"story": inputs["story"]}
    if stage == "outline":
        return inputs["source_snapshot"]
    if stage == "rewrite":
        return inputs["rewrite_input"]["context"]
    if stage == "script":
        return {
            name: deepcopy(inputs[name])
            for name in (
                "story",
                "style_guide",
                "story_settings",
                "story_context",
                "outline_context",
                "target_duration_ms",
            )
            if name in inputs
        }
    return {
        name: deepcopy(inputs[name])
        for name in ("script", "script_version_id", "target_duration_ms")
        if name in inputs
    }


def capture_contexts(
    db: Session, project_id: UUID, run: WorkflowRun, settings: Settings, actor: str | None = None
) -> dict[str, str]:
    previous = manifest_references(db, project_id, run.id)
    if previous is not None:
        read_contexts(db, project_id, run)
        return previous
    lock_dependency_graph(db, project_id)
    project = db.get(Project, project_id)
    assert project is not None
    actor = actor or project.owner_id
    script_id = run.input_json.get("script_version_id")
    if run.workflow_type == "script_rewrite":
        script_id = run.input_json["rewrite_input"]["request"]["base_version_id"]
    contexts = {}
    for stage in STAGES[run.workflow_type]:
        request = ContextRequest.model_validate(
            {
                "task": stage,
                "episode_id": run.episode_id,
                "script_version_id": script_id if stage in {"storyboard", "rewrite"} else None,
                "rewrite_target": run.input_json["rewrite_input"]["request"]["target"]
                if stage == "rewrite"
                else None,
            }
        )
        value = build_context(
            db,
            project_id,
            actor,
            request,
            settings,
            frozen_input=run.input_json,
            deferred_script_run=run.id
            if run.workflow_type == "script_storyboard" and stage == "storyboard"
            else None,
        )
        check_call(value, startup_input(run, stage))
        identity = uuid5(run.id, "task-context:" + stage)
        row = db.scalar(
            select(DependencyManifest).where(
                DependencyManifest.project_id == project_id,
                DependencyManifest.creation_key == identity,
            )
        )
        snapshot = {
            "schema_version": 1,
            "target": {
                "asset_id": None,
                "asset_type": "TASK_CONTEXT",
                "episode_id": str(run.episode_id),
            },
            "inputs": [
                {
                    "source_asset_version_id": item["asset_version_id"],
                    "asset_id": item["asset_id"],
                    "checksum": item["checksum"],
                    "dependency_type": "REFERENCE_DEPENDENCY",
                    "selectors": [],
                }
                for item in value["required"].get("assets", [])
            ],
            "context": {"origin": "context_builder", "run_id": str(run.id), "task_context": value},
        }
        if row is None:
            row = DependencyManifest(
                project_id=project_id,
                target_asset_id=None,
                creation_key=identity,
                created_by=actor,
                request_fingerprint=digest(snapshot),
                snapshot_json=snapshot,
            )
            db.add(row)
            db.flush()
        elif row.snapshot_json != snapshot:
            raise DomainError("CONTEXT_SNAPSHOT_CONFLICT", "启动上下文不能覆盖", 409)
        contexts[stage] = str(row.id)
    return contexts


def manifest_references(db: Session, project_id: UUID, run_id: UUID) -> dict[str, str] | None:
    binding = db.scalar(
        select(VersionBinding).where(
            VersionBinding.project_id == project_id,
            VersionBinding.resource_type == "WORKFLOW",
            VersionBinding.resource_id == run_id,
        )
    )
    return binding.snapshot_json.get("context_manifest_ids") if binding else None


def read_contexts(db: Session, project_id: UUID, run: WorkflowRun) -> dict[str, dict]:
    episode = db.get(Episode, run.episode_id)
    if episode is None or episode.project_id != project_id:
        raise DomainError("NOT_FOUND", "上下文运行不存在", 404)
    references = manifest_references(db, project_id, run.id)
    if references is None:
        return {}  # Explicit legacy behavior: never reconstruct from current data.
    if set(references) != set(STAGES.get(run.workflow_type, ())):
        raise DomainError("CONTEXT_SNAPSHOT_INVALID", "启动上下文清单不完整", 409)
    values = {}
    for task, identity in references.items():
        row = db.get(DependencyManifest, UUID(identity))
        if (
            row is None
            or row.project_id != project_id
            or row.creation_key != uuid5(run.id, "task-context:" + task)
            or digest(row.snapshot_json) != row.request_fingerprint
        ):
            raise DomainError("CONTEXT_SNAPSHOT_INVALID", "启动上下文引用缺失或不匹配", 409)
        value = row.snapshot_json["context"]["task_context"]
        if (
            value["builder_version"] != BUILDER_VERSION
            or value["task"] != task
            or value["project_id"] != str(project_id)
            or value["scope"]["episode_id"] != str(run.episode_id)
        ):
            raise DomainError("CONTEXT_SNAPSHOT_INVALID", "启动上下文范围不匹配", 409)
        check_size(value, ContextLimits.model_validate(value["limits"]))
        values[task] = deepcopy(value)
    return values
