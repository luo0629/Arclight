"""Bind narrative reads to screenplay outputs and compare approved replacements."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContentV3
from arclight.application.dependencies import invalidation_sequence
from arclight.application.writing_context import replay_projection
from arclight.core.errors import DomainError
from arclight.domain.script_drafts import REWRITE_WORKFLOWS
from arclight.domain.script_narrative import NarrativeContext
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoopAction
from arclight.persistence.models import (
    AssetVersion,
    DependencyInvalidation,
    DependencyManifest,
    DependencyOutput,
    Episode,
    Script,
    StoryLedgerVersion,
    StoryOutline,
    VisualAsset,
    WorkflowRun,
)


@dataclass(frozen=True)
class NarrativeReplacement:
    version: StoryOutline | StoryLedgerVersion
    origin_episode_no: int | None = None
    replaces_plans: frozenset[UUID] = frozenset()

    @classmethod
    def plan(cls, db: Session, version: StoryOutline) -> "NarrativeReplacement":
        ancestors: set[UUID] = set()
        previous = version
        while previous.base_version_id is not None:
            base = db.get(StoryOutline, previous.base_version_id)
            if (
                base is None
                or base.project_id != version.project_id
                or base.version_no >= previous.version_no
            ):
                raise DomainError("NARRATIVE_CONTEXT_INVALID", "全剧规划版本链不匹配", 409)
            ancestors.add(base.id)
            previous = base
        return cls(version, replaces_plans=frozenset(ancestors))

    def snapshot(self) -> dict:
        return {
            "kind": "SERIES_PLAN" if isinstance(self.version, StoryOutline) else "STORY_LEDGER",
            "version_id": str(self.version.id),
            "version_no": self.version.version_no,
            "content_sha256": digest(self.version.content_json),
            "origin_episode_no": self.origin_episode_no,
        }

    def scope(self, context: NarrativeContext) -> dict | None:
        if self.origin_episode_no is not None and context.episode_no <= self.origin_episode_no:
            return None
        if isinstance(self.version, StoryOutline):
            if context.series_plan_version_id not in self.replaces_plans:
                return None
            source = context.plan_source
        else:
            source = context.ledger_source or {"id": None, "version_no": 0}
        return source if source["version_no"] < self.version.version_no else None


def _projection_receipt(proof: dict, frozen: NarrativeContext) -> dict:
    context = NarrativeContext.model_validate(proof["context"])
    anchor = {
        "target_scene_no": context.target_scene_no,
        "target_story_time": context.target_story_time,
    }
    if frozen.model_copy(update=anchor) != context:
        raise DomainError("NARRATIVE_CONTEXT_INVALID", "叙事读取与剧本的冻结基准不匹配", 409)
    return {
        "anchor": anchor,
        "query": proof["query"],
        "script_sources": proof["script_sources"],
        "result_sha256": proof["result_sha256"],
    }


def _run_reads(db: Session, run: WorkflowRun, frozen: NarrativeContext) -> tuple[list[dict], int]:
    from arclight.application.agent_workflows import creation_loops
    from arclight.application.context_consumption import consumption
    from arclight.application.context_evidence import evidence_rounds
    from arclight.application.context_snapshots import manifest_references

    stage = "rewrite" if run.workflow_type in REWRITE_WORKFLOWS else "script"
    references = manifest_references(db, frozen.project_id, run.id) or {}
    root = db.get(DependencyManifest, UUID(references[stage])) if stage in references else None
    if root is None:
        return [], 0
    if (
        root.project_id != frozen.project_id
        or root.snapshot_json["context"]["run_id"] != str(run.id)
        or digest(root.snapshot_json) != root.request_fingerprint
    ):
        raise DomainError("NARRATIVE_CONTEXT_INVALID", "写作启动清单不匹配", 409)
    consumed = consumption(db, root)
    evidence_ids: set[str] = set(consumed["evidence_manifest_ids"]) if consumed else set()
    proofs: list[dict] = []
    completed = []
    for loop in creation_loops(db, run.id):
        if loop.status != "COMPLETED" or loop.project_id != frozen.project_id:
            continue
        seed = loop.seed_json
        if seed.get("stage") != stage:
            continue
        if digest(seed) != loop.request_sha256:
            raise DomainError("NARRATIVE_CONTEXT_INVALID", "创作调用的叙事来源校验失败", 409)
        completed.append(loop.id)
        for item in seed["messages"][0]["data"].get("narrative_projections", []):
            for view in item.get("views", [item]):
                proofs.extend(
                    source for source in view["sources"] if source["kind"] == "NARRATIVE_QUERY"
                )
    if completed:
        for action in db.scalars(
            select(AgentLoopAction).where(
                AgentLoopAction.loop_id.in_(completed),
                AgentLoopAction.kind == "TOOL",
                AgentLoopAction.request_json["tool_name"].as_string() == "investigate_series_story",
            )
        ):
            result = (action.result_json or {}).get("result", {})
            if result.get("startup_manifest_id") == str(root.id):
                evidence_ids.add(result["manifest_id"])
    proofs.extend(
        item
        for evidence in evidence_rounds(db, root)
        if str(evidence.id) in evidence_ids
        for item in evidence.snapshot_json["context"]["sources"]
        if item["kind"] == "NARRATIVE_QUERY"
    )
    receipts = {}
    for proof in proofs:
        receipt = _projection_receipt(proof, frozen)
        receipts[digest(receipt)] = receipt
    return list(receipts.values()), root.snapshot_json.get("invalidation_seq", 0)


def _manual_reads(db: Session, content: ScriptContentV3) -> list[dict]:
    """Manual versions declare their use through scene time and character objectives."""
    result = []
    for scene in content.scenes:
        anchor = {
            "target_scene_no": scene.scene_no,
            "target_story_time": scene.dramatic_beat.story_time,
        }
        context = content.narrative_context.model_copy(update=anchor)
        for character in [
            None,
            *[str(item.character_id) for item in scene.dramatic_beat.character_objectives],
        ]:
            query: dict[str, Any] = {
                "story_time": context.target_story_time,
                "scene_no": context.target_scene_no,
                "character_id": character,
                "causal_ids": [],
                "limit": 20,
                "offset": 0,
            }
            result.append(
                {
                    "anchor": anchor,
                    "query": query,
                    "script_sources": [],
                    "result_sha256": digest(replay_projection(db, context, query, [])),
                }
            )
    return result


def capture_narrative_reads(
    db: Session, script: Script, *, historical: bool = False
) -> dict | None:
    if script.content_json.get("schema_version") != "3.0":
        return None
    content = ScriptContentV3.model_validate(script.content_json)
    frozen = content.narrative_context
    episode = db.get(Episode, script.episode_id)
    if episode is None or (frozen.episode_id, frozen.project_id) != (
        episode.id,
        episode.project_id,
    ):
        # Episode copies require a fresh baseline when their content is next saved.
        return None
    run = db.get(WorkflowRun, script.run_id) if script.run_id else None
    if run is not None and run.input_json.get("narrative_context") == frozen.model_dump(
        mode="json"
    ):
        reads, seq = _run_reads(db, run, frozen)
    else:
        reads = []
        seq = 0 if historical else invalidation_sequence(db, frozen.project_id)
    return {
        "context": frozen.model_dump(mode="json"),
        "invalidation_seq": seq,
        "origin": "WORKFLOW_READS" if reads else "DECLARED_SCENES",
        "projections": reads or _manual_reads(db, content),
    }


_PROJECTION_SECTIONS = {
    "plan": "全剧规划",
    "character_name": "角色来源",
    "facts": "事实、关系或知情范围",
    "planned": "作者计划",
    "proposals": "状态提议",
    "episode_index": "分集索引",
    "causal_summaries": "因果摘要",
    "related_missing": "因果来源范围",
}


def _changed_read(
    db: Session, reads: dict, change: NarrativeReplacement
) -> tuple[dict, str] | None:
    frozen = NarrativeContext.model_validate(reads["context"])
    source = change.scope(frozen)
    if frozen.project_id != change.version.project_id or source is None:
        return None
    for receipt in reads["projections"]:
        query = receipt["query"]
        context = frozen.model_copy(update=receipt["anchor"])
        original = replay_projection(db, context, query, receipt["script_sources"])
        if digest(original) != receipt["result_sha256"]:
            raise DomainError("NARRATIVE_CONTEXT_INVALID", "保存的叙事读取未通过校验", 409)
        perspective = "角色视角" if query["character_id"] else "作者视角"
        label = f"第 {query['scene_no']} 场 · {perspective}"
        try:
            revised = replay_projection(
                db, context, query, receipt["script_sources"], replacement=change.version
            )
        except DomainError as error:
            if error.code not in {"NARRATIVE_CONTEXT_INVALID", "CONTEXT_TOO_LARGE"}:
                raise
            return source, f"{label}的原查询在新版本中不可用"
        for view in (original, revised):
            if view["plan"]:
                view["plan"].pop("version_id")
                view["plan"].pop("sha256")
        modified = [
            label for name, label in _PROJECTION_SECTIONS.items() if original[name] != revised[name]
        ]
        if modified:
            return source, f"{label}读取的{'、'.join(modified)}有变化"
    return None


def narrative_consumers(
    db: Session, project_id: UUID, change: NarrativeReplacement
) -> list[tuple[AssetVersion, VisualAsset, dict]]:
    if isinstance(change.version, StoryOutline) and not change.replaces_plans:
        return []
    rows = db.execute(
        select(Script, AssetVersion, VisualAsset, DependencyManifest)
        .join(Episode, Episode.id == Script.episode_id)
        .join(AssetVersion, AssetVersion.id == Script.id)
        .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
        .outerjoin(DependencyOutput, DependencyOutput.asset_version_id == Script.id)
        .outerjoin(
            DependencyManifest, DependencyManifest.id == DependencyOutput.dependency_manifest_id
        )
        .where(
            Episode.project_id == project_id,
            Episode.status != "ARCHIVED",
            VisualAsset.project_id == project_id,
            VisualAsset.asset_type == "SCRIPT",
            AssetVersion.stale.is_(False),
            AssetVersion.status.in_(("DRAFT", "READY_FOR_REVIEW", "APPROVED", "LOCKED")),
            Script.content_json["schema_version"].as_string() == "3.0",
        )
        .order_by(Script.id)
    )
    affected = []
    snapshot = change.snapshot()
    for script, version, asset, manifest in rows:
        context = NarrativeContext.model_validate(script.content_json["narrative_context"])
        if change.scope(context) is None:
            continue
        reads = manifest.snapshot_json.get("narrative") if manifest else None
        if reads is None:
            reads = capture_narrative_reads(db, script, historical=True)
        if reads is not None and (matched := _changed_read(db, reads, change)) is not None:
            source, reason = matched
            affected.append(
                (
                    version,
                    asset,
                    {
                        "source_asset_version_id": source["id"] or str(change.version.id),
                        "dependency_type": "CONTENT_DEPENDENCY",
                        "source_kind": snapshot["kind"],
                        "source_version_id": source["id"],
                        "replacement_version_id": str(change.version.id),
                        "reason": reason,
                    },
                )
            )
    return affected


def expired_narrative_event(db: Session, project_id: UUID, reads: dict) -> UUID | None:
    events = db.scalars(
        select(DependencyInvalidation)
        .where(
            DependencyInvalidation.project_id == project_id,
            DependencyInvalidation.snapshot_json["narrative"]["kind"].as_string().is_not(None),
        )
        .order_by(DependencyInvalidation.event_no)
    )
    for event in events:
        snapshot = event.snapshot_json["narrative"]
        version_id = UUID(snapshot["version_id"])
        version: StoryOutline | StoryLedgerVersion | None = (
            db.get(StoryOutline, version_id)
            if snapshot["kind"] == "SERIES_PLAN"
            else db.get(StoryLedgerVersion, version_id)
        )
        if version is None or version.project_id != project_id:
            raise DomainError("NARRATIVE_CONTEXT_INVALID", "叙事变更版本不存在", 409)
        change = (
            NarrativeReplacement.plan(db, version)
            if isinstance(version, StoryOutline)
            else NarrativeReplacement(version, snapshot["origin_episode_no"])
        )
        if change.snapshot() != snapshot:
            raise DomainError("NARRATIVE_CONTEXT_INVALID", "叙事变更版本校验失败", 409)
        if _changed_read(db, reads, change):
            return event.id
    return None
