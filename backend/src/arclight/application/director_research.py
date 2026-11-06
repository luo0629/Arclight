"""Read-only production investigation on the original immutable Context evidence chain."""

import json
from copy import deepcopy
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.content_dependencies import reference_ids
from arclight.application.context_builder import resolve_assets
from arclight.application.context_consumption import consume_context
from arclight.application.context_evidence import append_resolved_evidence, read_trace, round_key
from arclight.application.context_sources import asset_sources, verify_images
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.context import ContextLimits
from arclight.domain.director_planning import (
    AssetQuery,
    CapabilityQuery,
    CatalogQuery,
    InvestigateProductionParameters,
    InvestigateProductionResult,
    ScriptQuery,
    VisualQuery,
)
from arclight.domain.versions import digest
from arclight.persistence.models import AssetVersion, Episode, Script, VisualAsset, WorkflowRun
from arclight.providers.media_capabilities import resolve_capabilities


def approved_input(
    db: Session, run: WorkflowRun, actor: str, settings: Settings
) -> tuple[Script, dict]:
    """Resolve the selected script, including a deferred output of the original dual-review run."""
    if run.workflow_type not in {"storyboard", "script_storyboard"}:
        raise DomainError("DIRECTOR_TASK_INVALID", "此任务没有导演规划阶段", 422)
    episode = db.get(Episode, run.episode_id)
    assert episode is not None
    consume_context(db, episode.project_id, run, "storyboard", settings)
    trace = read_trace(db, run, "storyboard", actor)
    receipt = trace["consumption"]["resolved_input"]
    script = db.get(Script, UUID(receipt["script_version_id"]), populate_existing=True)
    if (
        script is None
        or script.episode_id != run.episode_id
        or script.status != "APPROVED"
        or digest(script.content_json) != receipt["sha256"]
    ):
        raise DomainError("DIRECTOR_SCRIPT_INVALID", "导演输入须为原精确批准剧本", 409)
    root = trace["task_context"]
    versions = {str(script.id)} | reference_ids(script.content_json)
    versions.update(item["asset_version_id"] for item in root["required"].get("assets", []))
    resolve_assets(
        db, UUID(root["project_id"]), versions, ContextLimits.model_validate(root["limits"])
    )
    return script, trace


def investigate_production(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    actor: str,
    parameters: InvestigateProductionParameters,
) -> dict:
    script, trace = approved_input(db, run, actor, settings)
    query = parameters.query
    query_json = query.model_dump(mode="json")
    initial_assets = trace["task_context"]["required"].get("assets", [])

    def resolve(root, rounds, limits):
        identity = round_key(root.id, len(rounds) + 1)
        materials, missing, assets = [], [], []
        has_more, reused = False, False
        previous = next(
            (
                row.snapshot_json["context"]["director_investigation"]
                for row in reversed(rounds)
                if row.snapshot_json["context"].get("director_investigation", {}).get("query")
                == query_json
            ),
            None,
        )

        def material(kind, source_id, content, bound=None):
            return {
                "manifest_id": str(identity),
                "source_kind": kind,
                "source_id": str(source_id),
                "bound_manifest_id": str(bound or identity),
                "content_sha256": digest(content),
                "content": deepcopy(content),
            }

        if previous and not parameters.refresh:
            saved = previous["result"]
            materials = [item | {"manifest_id": str(identity)} for item in saved["materials"]]
            missing, has_more, reused = deepcopy(saved["missing"]), saved["has_more"], True
            assets = deepcopy(previous["assets"])
            # A saved query is not permission to consume a now-stale asset.
            resolve_assets(db, root.project_id, {a["asset_version_id"] for a in assets}, limits)
        elif isinstance(query, ScriptQuery):
            content = deepcopy(script.content_json)
            if query.scene_ids is not None:
                selected_scenes = [
                    scene for scene in content["scenes"] if scene["scene_id"] in query.scene_ids
                ]
                if {scene["scene_id"] for scene in selected_scenes} != set(query.scene_ids):
                    raise DomainError("DIRECTOR_PLAN_SCOPE", "所选场景不在精确剧本范围", 422)
                content = {
                    "script_version_id": str(script.id),
                    "script_sha256": digest(script.content_json),
                    "scenes": selected_scenes,
                    "selection": query.scene_ids,
                }
            materials.append(material("SCRIPT", script.id, content, root.id))
            assets = resolve_assets(db, root.project_id, {str(script.id)}, limits)
        elif isinstance(query, VisualQuery):
            required = root.snapshot_json["context"]["task_context"]["required"]
            materials.append(
                material(
                    "VISUAL",
                    root.id,
                    {"global": required["global"], "constraints": required["constraints"]},
                    root.id,
                )
            )
        elif isinstance(query, CapabilityQuery):
            try:
                profile = resolve_capabilities(query.provider, query.model).model_dump(mode="json")
            except DomainError as error:
                if error.code != "PROVIDER_NOT_INSTALLED":
                    raise
                missing.append({"selector": query_json, "reason": error.code})
            else:
                content = {"model": query.model, "declaration": profile}
                materials.append(material("CAPABILITY", uuid5(root.id, digest(content)), content))
        else:
            if isinstance(query, AssetQuery):
                selected = query.version_ids
            else:
                assert isinstance(query, CatalogQuery)
                rows = db.scalars(
                    select(VisualAsset)
                    .where(
                        VisualAsset.project_id == root.project_id,
                        VisualAsset.asset_type == query.asset_type,
                        VisualAsset.status.not_in(["ARCHIVED", "REJECTED", "STALE"]),
                        VisualAsset.name.icontains(query.name, autoescape=True),
                        VisualAsset.current_version_id.is_not(None),
                    )
                    .order_by(VisualAsset.name, VisualAsset.id)
                    .limit(query.limit + 1)
                ).all()
                has_more = len(rows) > query.limit
                selected = [
                    row.current_version_id
                    for row in rows[: query.limit]
                    if row.current_version_id is not None
                ]
                if not selected:
                    missing.append({"selector": query_json, "reason": "NOT_FOUND"})
            frozen = {item["asset_version_id"]: (root.id, item) for item in initial_assets}
            for row in rounds:
                frozen.update(
                    {
                        item["asset_version_id"]: (row.id, item)
                        for item in row.snapshot_json["context"].get("assets", [])
                        if item["asset_version_id"] not in frozen
                    }
                )
            for version_id in selected:
                version = db.get(AssetVersion, version_id)
                owner = db.get(VisualAsset, version.asset_id) if version else None
                if owner is not None and owner.project_id != root.project_id:
                    raise DomainError("REFERENCE_SCOPE_INVALID", "资产不在当前项目范围", 404)
                selector = {"asset_version_id": str(version_id)}
                if version is None:
                    missing.append({"selector": selector, "reason": "NOT_FOUND"})
                    continue
                try:
                    resolved = resolve_assets(db, root.project_id, {str(version_id)}, limits)
                except DomainError as error:
                    if error.code != "CONTEXT_INPUT_NOT_USABLE":
                        raise
                    missing.append({"selector": selector, "reason": "INPUT_NOT_USABLE"})
                    continue
                for asset in resolved:
                    bound, content = frozen.get(asset["asset_version_id"], (identity, asset))
                    assets.append(deepcopy(content))
                    if asset["asset_version_id"] == str(version_id):
                        materials.append(material("ASSET", version_id, content, bound))
                        reused |= bound != identity
        sources = asset_sources(db, root.project_id, assets)
        verify_images(db, settings, root.project_id, sources)
        result = InvestigateProductionResult(
            manifest_id=identity,
            startup_manifest_id=root.id,
            evidence_round=len(rounds) + 1,
            materials=materials,
            missing=missing,
            has_more=has_more,
            reused_binding=reused,
        ).model_dump(mode="json")
        if len(json.dumps(result, ensure_ascii=False).encode()) > 65536:
            raise DomainError(
                "CONTEXT_TOO_LARGE", "调查反馈超过限额，请显式缩小场景或资产范围", 422
            )
        return {
            "sources": sources,
            "assets": assets,
            "director_investigation": {"query": query_json, "result": result, "assets": assets},
        }

    row, _ = append_resolved_evidence(
        db,
        run,
        "storyboard",
        actor,
        creation_key=uuid4(),
        expected_round=len(trace["evidence"]),
        request_sha256=digest(parameters.model_dump(mode="json")),
        resolve=resolve,
    )
    return deepcopy(row.snapshot_json["context"]["director_investigation"]["result"])
