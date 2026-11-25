"""Authorized project queries shared by Agent tools and the MCP transport."""

from dataclasses import dataclass
from typing import get_args
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.agents.contracts import read_script_content, read_shot_content
from arclight.application.agent_limits import action_usage, delegation_count
from arclight.application.commands import require_episode, require_project, require_run
from arclight.application.context_evidence import read_trace
from arclight.application.continuity_review_store import require_review_record
from arclight.application.dependencies import exact_version
from arclight.application.invalidation import impact_preview
from arclight.application.narrative_review import read_report, read_work, report_identity
from arclight.application.producer_plan_history import find_task_delegation
from arclight.application.producer_planning import read_producer_plan
from arclight.application.producer_series import has_series_scope, task_context
from arclight.application.production_versions import checked_snapshot, find_binding
from arclight.application.video_review_evidence import approved_shot_context
from arclight.application.video_review_routing import temporal_report_id
from arclight.application.writing_context import query_frozen_writing
from arclight.core.errors import DomainError
from arclight.domain.art_proposals import AssetKind
from arclight.domain.continuity_reviewer import review_operation
from arclight.domain.continuity_routing import ReviewRoute
from arclight.domain.continuity_selection import ADMISSION_RESOURCE
from arclight.domain.continuity_workflow import (
    REVIEW_WORK_RESOURCE,
    CandidateReviewWork,
)
from arclight.domain.narrative_review import NARRATIVE_REPORT_RESOURCE
from arclight.domain.producer_planning import DelegationRequest, ProducerPlan
from arclight.domain.producer_series import SeriesProducerPlan
from arclight.domain.script_narrative import FrozenWritingQuery
from arclight.domain.versions import digest
from arclight.domain.video_review import VIDEO_REVIEW_RESOURCE, VideoReviewRecord
from arclight.domain.video_temporal_reviewer import (
    TEMPORAL_WORK_RESOURCE,
    TemporalWork,
)
from arclight.domain.visual_bible import FIELD_NAMES, VisualFields
from arclight.persistence.models import (
    AssetVersion,
    Script,
    Shot,
    Storyboard,
    VisualAsset,
    VisualBibleVersion,
    WorkflowRun,
)
from arclight.persistence.versions import VersionBinding

_MAX_REFERENCES = 64
_MAX_IMPACTS = 50
_MAX_IMPACT_LOCATIONS = 20
_MAX_REVIEW_REPORTS = 16
_USABLE = ("APPROVED", "LOCKED")
_SOURCE_FIELDS = {
    "kind",
    "id",
    "asset_id",
    "asset_version_id",
    "checksum",
    "created_from_source_id",
    "dependency_manifest_id",
    "episode_id",
    "parent_source_id",
    "result_sha256",
    "revision",
    "revision_id",
    "root_source_id",
    "sha256",
    "source_id",
    "version_no",
    "version_id",
}


def _source_refs(sources: list[dict]) -> list[dict]:
    return [
        {key: value for key, value in source.items() if key in _SOURCE_FIELDS} for source in sources
    ]


def _trace_projection(trace: dict) -> dict:
    return {
        "startup_manifest_id": trace["startup_manifest_id"],
        "trace_available": trace["trace_available"],
        "startup_sources": _source_refs((trace["startup"] or {}).get("sources", [])),
        "evidence": [
            {
                "manifest_id": round_["manifest_id"],
                "parent_manifest_id": round_["parent_manifest_id"],
                "round": round_["round"],
                "request_sha256": round_["request_sha256"],
                "sources": _source_refs(round_["sources"]),
            }
            for round_ in trace["evidence"]
        ],
    }


@dataclass(frozen=True, slots=True)
class ProjectReads:
    db: Session
    actor: str

    def arclight_read_content(
        self, episode_id: UUID, version_id: UUID, scene_no: int | None = None
    ) -> dict:
        require_episode(self.db, episode_id, self.actor)
        script = self.db.get(Script, version_id)
        if script is None or script.episode_id != episode_id:
            raise DomainError("NOT_FOUND", "剧本版本不存在", 404)
        if script.status not in _USABLE:
            raise DomainError("CONTENT_NOT_APPROVED", "该剧本版本当前未获批准", 409)
        content = read_script_content(script.content_json).model_dump(mode="json")
        if scene_no is not None:
            scenes = [scene for scene in content["scenes"] if scene["scene_no"] == scene_no]
            if not scenes:
                raise DomainError("NOT_FOUND", "剧本场次不存在", 404)
            content["scenes"] = scenes
        return {
            "episode_id": str(episode_id),
            "version_id": str(script.id),
            "version_no": script.version_no,
            "status": script.status,
            "content_sha256": digest(script.content_json),
            "selected_scene_no": scene_no,
            "content": content,
        }

    def arclight_query_story_facts(self, episode_id: UUID, query: FrozenWritingQuery) -> dict:
        result = query_frozen_writing(self.db, episode_id, self.actor, query)
        result["sources"] = _source_refs(result["sources"])
        return result

    def arclight_read_shot_context(
        self, episode_id: UUID, storyboard_id: UUID, shot_id: UUID
    ) -> dict:
        require_episode(self.db, episode_id, self.actor)
        storyboard = self.db.get(Storyboard, storyboard_id)
        if storyboard is None or storyboard.episode_id != episode_id:
            raise DomainError("NOT_FOUND", "分镜版本不存在", 404)
        shot = self.db.get(Shot, shot_id)
        if shot is None or shot.storyboard_id != storyboard_id:
            raise DomainError("NOT_FOUND", "镜头不存在", 404)
        if storyboard.status not in _USABLE:
            raise DomainError("CONTENT_NOT_APPROVED", "该分镜版本当前未获批准", 409)
        result = {
            **approved_shot_context(self.db, shot),
            "episode_id": str(episode_id),
            "script_version_id": str(storyboard.script_version_id),
            "storyboard_version_no": storyboard.version_no,
            "storyboard_sha256": digest(storyboard.content_json),
            "storyboard_status": storyboard.status,
        }
        for item in result["shot_plan"]:
            item["content_sha256"] = digest(item["content"])
            item["content"] = read_shot_content(item["content"]).model_dump(mode="json")
        return result

    def arclight_read_visual_references(self, project_id: UUID, version_id: UUID) -> dict:
        require_project(self.db, project_id, self.actor)
        bible = self.db.get(VisualBibleVersion, version_id)
        if bible is None or bible.project_id != project_id:
            raise DomainError("NOT_FOUND", "视觉规范版本不存在", 404)
        rows = self.db.execute(
            select(VisualAsset, AssetVersion)
            .join(
                AssetVersion,
                (AssetVersion.id == VisualAsset.current_version_id)
                & (AssetVersion.asset_id == VisualAsset.id),
            )
            .where(
                VisualAsset.project_id == project_id,
                VisualAsset.asset_type.in_(get_args(AssetKind)),
                VisualAsset.status.not_in(("ARCHIVED", "REJECTED", "STALE")),
                func.coalesce(
                    VisualAsset.metadata_json["usage_scope"]["kind"].as_string(), "PROJECT"
                )
                == "PROJECT",
                AssetVersion.status.in_(_USABLE),
                AssetVersion.stale.is_(False),
            )
            .order_by(VisualAsset.asset_type, VisualAsset.name, VisualAsset.id)
            .limit(_MAX_REFERENCES + 1)
        ).all()
        visual = {
            "version_id": str(bible.id),
            "version_no": bible.version_no,
            "fields": VisualFields(
                **{name: getattr(bible, name) for name in FIELD_NAMES}
            ).model_dump(),
            "locked_fields": bible.locked_fields,
        }
        return {
            "project_id": str(project_id),
            "visual_bible": {**visual, "content_sha256": digest(visual)},
            # The approved directory can change independently of the visual bible.
            "current_project_references": [
                {
                    "asset_id": str(asset.id),
                    "asset_type": asset.asset_type,
                    "name": asset.name,
                    "asset_version_id": str(version.id),
                    "version_no": version.version_no,
                    "status": version.status,
                    "checksum": version.checksum,
                }
                for asset, version in rows[:_MAX_REFERENCES]
            ],
            "references_truncated": len(rows) > _MAX_REFERENCES,
        }

    def arclight_read_production_status(self, run_id: UUID) -> dict:
        run = require_run(self.db, run_id, self.actor)
        if run.workflow_type != "producer":
            raise DomainError("NOT_FOUND", "生产协调运行不存在", 404)
        episode = require_episode(self.db, run.episode_id, self.actor)
        binding = self.db.scalar(
            select(VersionBinding)
            .where(
                VersionBinding.project_id == episode.project_id,
                VersionBinding.resource_type == "PRODUCER_PLAN",
                VersionBinding.snapshot_json["run_id"].as_string() == str(run_id),
            )
            .order_by(VersionBinding.snapshot_json["version_no"].as_integer().desc())
            .limit(1)
        )
        usage = action_usage(self.db, run.id)
        latest = self._production_plan(run, binding) if binding is not None else None
        result = {
            "run_id": str(run.id),
            "episode_id": str(episode.id),
            "project_id": str(episode.project_id),
            "status": run.status,
            "revision": run.revision,
            "latest_plan": latest,
            "budget": {
                "actual_cost": str(usage["actual_cost"]),
                "pending_estimate": str(usage["pending_estimate"]),
                "unknown_cost": usage["unknown_cost"],
                "model_calls": sum(action.kind == "MODEL" for action in usage["actions"]),
                "tool_calls": sum(action.kind == "TOOL" for action in usage["actions"]),
                "delegations": delegation_count(self.db, run.id),
            },
        }
        return result

    def _production_plan(self, run: WorkflowRun, binding: VersionBinding) -> dict:
        try:
            reference = read_producer_plan(self.db, binding.resource_id, self.actor)
            snapshot = binding.snapshot_json
            plan_contract = SeriesProducerPlan if has_series_scope(run) else ProducerPlan
            plan = plan_contract.model_validate(snapshot["plan"])
            checksum = digest(plan.model_dump(mode="json"))
            version_no = snapshot["version_no"]
            if (
                reference.plan_sha256 != checksum
                or plan.goal_sha256 != snapshot["goal_sha256"]
                or snapshot["goal_sha256"] != run.input_json["goal_sha256"]
                or type(version_no) is not int
                or version_no < 1
            ):
                raise ValueError("Production plan differs from its exact scope")
            tasks = []
            for task in plan.tasks:
                origin = snapshot.get("carried_tasks", {}).get(task.task_key)
                request = (
                    DelegationRequest.model_validate(origin)
                    if origin is not None
                    else DelegationRequest(
                        plan_id=binding.resource_id, plan_sha256=checksum, task_key=task.task_key
                    )
                )
                saved = find_task_delegation(self.db, run.id, request)
                child = require_run(self.db, saved.child_run_id, self.actor) if saved else None
                selected = task_context(run, plan, task.task_key)
                episode_id = selected.episode_id if selected else run.episode_id
                if child is not None and child.episode_id != episode_id:
                    raise ValueError("Delegation child differs from its exact task scope")
                tasks.append(
                    {
                        "task_key": task.task_key,
                        "kind": task.task.kind,
                        "episode_id": str(episode_id),
                        "depends_on": task.depends_on,
                        "child_run_id": str(child.id) if child else None,
                        "status": child.status if child else "NOT_STARTED",
                    }
                )
            return {
                "plan_id": str(binding.resource_id),
                "plan_sha256": checksum,
                "version_no": version_no,
                "summary": plan.summary,
                "tasks": tasks,
            }
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError("PRODUCER_PLAN_INVALID", "生产计划与精确范围不匹配", 409) from error

    def arclight_read_review_evidence(
        self, run_id: UUID, stage: str, through_round: int | None = None
    ) -> dict:
        run = require_run(self.db, run_id, self.actor)
        episode = require_episode(self.db, run.episode_id, self.actor)
        is_review = run.workflow_type == "continuity_review" and stage in {"review", "storyboard"}
        reports: list[dict] = []
        reports_truncated = False
        if is_review and through_round is None:
            reports, reports_truncated = self._review_reports(run, episode.project_id)
        trace = None
        try:
            saved = read_trace(self.db, run, stage, self.actor, through_round)
            trace = _trace_projection(saved)
        except DomainError as error:
            if error.code != "CONTEXT_TRACE_UNAVAILABLE" or not reports:
                raise
        result = {
            "run_id": str(run.id),
            "stage": stage,
            "trace": trace,
            "review_reports": reports,
            "reports_truncated": reports_truncated,
        }
        return result

    def _review_reports(self, run: WorkflowRun, project_id: UUID) -> tuple[list[dict], bool]:
        if "narrative_review" in run.input_json:
            identity = report_identity(run.id)
            if find_binding(self.db, project_id, NARRATIVE_REPORT_RESOURCE, identity) is None:
                return [], False
            report = read_report(self.db, project_id, identity)
            frozen = read_work(self.db, project_id, run.id)
            if report.run_id != run.id or report.input_sha256 != digest(
                frozen.model_dump(mode="json")
            ):
                raise DomainError("REVIEW_SCOPE_INVALID", "叙事报告与精确运行输入不匹配", 409)
            checks = report.assessment.checks if report.assessment else []
            cited = {identity for check in checks for identity in check.evidence_ids}
            cited.update(
                identity
                for check in checks
                for issue in check.issues
                for identity in issue.evidence_ids
            )
            return [
                {
                    "kind": "NARRATIVE",
                    "report": report.model_dump(mode="json"),
                    "cited_locations": [
                        item.model_dump(mode="json", exclude={"value"})
                        for item in frozen.evidence
                        if item.id in cited
                    ],
                }
            ], False
        if "continuity_review" in run.input_json:
            return self._candidate_reports(run, project_id, REVIEW_WORK_RESOURCE, "continuity_work")
        if "video_temporal_review" in run.input_json:
            return self._candidate_reports(run, project_id, TEMPORAL_WORK_RESOURCE, "temporal_work")
        return [], False

    def _candidate_reports(
        self, run: WorkflowRun, project_id: UUID, resource: str, snapshot_key: str
    ) -> tuple[list[dict], bool]:
        work_rows = self.db.scalars(
            select(VersionBinding)
            .where(
                VersionBinding.project_id == project_id,
                VersionBinding.resource_type == resource,
                VersionBinding.snapshot_json[snapshot_key]["run_id"].as_string() == str(run.id),
            )
            .order_by(VersionBinding.created_at, VersionBinding.resource_id)
            .limit(_MAX_REVIEW_REPORTS + 1)
        ).all()
        try:
            reports = []
            for row in work_rows[:_MAX_REVIEW_REPORTS]:
                work = checked_snapshot(self.db, row)[snapshot_key]
                if resource == REVIEW_WORK_RESOURCE:
                    report = self._image_report(
                        run, project_id, CandidateReviewWork.model_validate(work)
                    )
                else:
                    report = self._video_report(run, project_id, TemporalWork.model_validate(work))
                if report is not None:
                    reports.append(report)
            return reports, len(work_rows) > _MAX_REVIEW_REPORTS
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise DomainError(
                "REVIEW_SCOPE_INVALID", "审查报告与精确运行输入不匹配", 409
            ) from error

    def _image_report(
        self, run: WorkflowRun, project_id: UUID, work: CandidateReviewWork
    ) -> dict | None:
        identity = uuid5(run.id, review_operation(work.target, work.rerun))
        binding = find_binding(self.db, project_id, ADMISSION_RESOURCE, identity)
        if binding is None:
            return None
        route = ReviewRoute.model_validate(checked_snapshot(self.db, binding)["review_route"])
        record = require_review_record(self.db, project_id, identity, self.actor)
        if (
            route.run_id != run.id
            or route.review_id != identity
            or route.record_sha256 != digest(record.model_dump(mode="json"))
            or record.review.episode_id != run.episode_id
            or record.review.target != work.target
        ):
            raise ValueError("Image report differs from its exact review scope")
        return {
            "kind": "IMAGE_CONTINUITY",
            "report": record.review.model_dump(mode="json"),
            "report_sha256": record.review_sha256,
        }

    def _video_report(self, run: WorkflowRun, project_id: UUID, work: TemporalWork) -> dict | None:
        identity = temporal_report_id(run, work.target.candidate_id)
        binding = find_binding(self.db, project_id, VIDEO_REVIEW_RESOURCE, identity)
        if binding is None:
            return None
        snapshot = checked_snapshot(self.db, binding)
        record = VideoReviewRecord.model_validate(snapshot["video_review"])
        report = record.report
        if (
            report.report_id != identity
            or report.project_id != project_id
            or report.episode_id != run.episode_id
            or report.target != work.target
            or snapshot["components"] != [part.model_dump(mode="json") for part in report.versions]
        ):
            raise ValueError("Video report differs from its exact review scope")
        return {
            "kind": "TEMPORAL_VIDEO",
            "report": report.model_dump(mode="json"),
            "report_sha256": record.report_sha256,
        }

    def arclight_preview_change_impact(self, project_id: UUID, version_id: UUID) -> dict:
        require_project(self.db, project_id, self.actor)
        version, _ = exact_version(self.db, project_id, version_id)
        preview = impact_preview(
            self.db,
            project_id,
            [
                {
                    "source_asset_version_id": str(version.id),
                    "expected_revision": version.revision,
                    "change_kind": "CONTENT",
                    "selectors": [],
                }
            ],
        )
        affected = preview["affected"]
        items = [dict(item) for item in affected[:_MAX_IMPACTS]]
        for item in items:
            for key in ("shots", "reviews"):
                if key in item:
                    item[f"{key}_total"] = len(item[key])
                    item[key] = item[key][:_MAX_IMPACT_LOCATIONS]
        result = {
            "project_id": str(project_id),
            "source_asset_version_id": str(version.id),
            "expected_revision": version.revision,
            "affected": items,
            "affected_total": len(affected),
            "digest": preview["digest"],
            "summary": preview["summary"],
        }
        return result
