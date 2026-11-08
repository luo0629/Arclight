"""Reviewer evidence on the existing authorized visual-stage Context chain."""

import json
from copy import deepcopy
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from arclight.application.art_research_assets import allowed_for_episode
from arclight.application.content_dependencies import reference_ids
from arclight.application.context_builder import load_local, resolve_assets
from arclight.application.context_evidence import append_resolved_evidence, read_trace, round_key
from arclight.application.context_sources import asset_sources, validate_sources, verify_images
from arclight.application.continuity_reviews import require_review_target, shot_sha256
from arclight.application.dependencies import exact_version, require_manifest
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.context import ContextLimits, ContextRequest
from arclight.domain.continuity_research import (
    InvestigateContinuityParameters,
    InvestigateContinuityResult,
)
from arclight.domain.versions import digest
from arclight.persistence.models import Shot, VisualAsset, WorkflowRun


def require_evidence_candidate(
    db: Session, run: WorkflowRun, project_id: UUID, actor: str, evidence: dict
) -> UUID:
    """Only a server-collected, checked candidate may extend the image reader's scope."""
    result = InvestigateContinuityResult.model_validate(evidence["result"])
    target = result.target
    version, board, shot = require_review_target(db, project_id, run.episode_id, target, actor)
    target_material = next(
        item
        for item in result.materials
        if item.source_kind == "SHOT" and item.source_id == shot.id
    )
    if target_material.content["shot_sha256"] != shot_sha256(shot):
        raise DomainError("REVIEW_REFERENCE_CHANGED", "取证镜头与原精确内容不一致", 409)
    resolve_assets(
        db,
        project_id,
        {str(board.id), str(board.script_version_id)},
        ContextLimits(),
        follow_profile_references=False,
    )
    if shot.status != "APPROVED":
        raise DomainError("CONTEXT_INPUT_NOT_USABLE", "取证镜头已失效", 409)
    return version.id


def investigate_continuity(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    actor: str,
    parameters: InvestigateContinuityParameters,
) -> dict:
    trace = read_trace(db, run, "storyboard", actor)
    target = parameters.target
    target_json = target.model_dump(mode="json")

    def resolve(root, rounds, limits):
        version, board, shot = require_review_target(
            db, root.project_id, run.episode_id, target, actor
        )
        local, exact_ids = load_local(
            db,
            root.project_id,
            ContextRequest(
                task="review",
                episode_id=run.episode_id,
                shot_id=shot.id,
                candidate_version_id=version.id,
                limits=limits,
            ),
        )
        frozen_script = root.snapshot_json["context"]["task_context"]["scope"].get(
            "script_version_id"
        )
        consumed = trace.get("consumption") or {}
        frozen_script = (consumed.get("resolved_input") or {}).get(
            "script_version_id", frozen_script
        )
        if frozen_script and str(board.script_version_id) != frozen_script:
            raise DomainError("REVIEW_TARGET_INVALID", "取证分镜与原运行的精确剧本不匹配", 409)
        manifest = require_manifest(db, root.project_id, target.input_manifest_id)
        exact_ids.update(
            item["source_asset_version_id"] for item in manifest.snapshot_json["inputs"]
        )
        for item in manifest.snapshot_json["inputs"]:
            if item["dependency_type"] != "CONTENT_DEPENDENCY":
                continue
            source, source_asset = exact_version(
                db, root.project_id, UUID(item["source_asset_version_id"])
            )
            expected = {"STORYBOARD": board.id, "SCRIPT": board.script_version_id}
            if (
                source_asset.asset_type in expected
                and source.id != expected[source_asset.asset_type]
            ):
                raise DomainError(
                    "REVIEW_TARGET_INVALID", "候选生成时的精确文本版本与目标不匹配", 409
                )
        required = root.snapshot_json["context"]["task_context"]["required"]
        exact_ids.update(reference_ids(required["global"]))
        exact_ids -= {str(version.id), str(board.id), str(board.script_version_id)}
        identity = round_key(root.id, len(rounds) + 1)
        bindings, source_bindings = {}, {}
        missing_bindings: dict[str, str] = {}
        previous = None
        if not parameters.refresh:
            for row in rounds:
                saved = row.snapshot_json["context"].get("continuity_investigation")
                if saved is None or saved["result"]["target"] != target_json:
                    continue
                previous = saved["result"]
                for item in previous["materials"]:
                    bindings[item["source_kind"], item["source_id"]] = item
                    if item["source_kind"] == "ASSET":
                        missing_bindings.pop(item["source_id"], None)
                for item in row.snapshot_json["context"]["sources"]:
                    if item["kind"] == "ASSET_VERSION":
                        source_bindings[item["asset_version_id"]] = item
                for item in previous["missing"]:
                    if item["reason"] in {"NOT_FOUND", "INPUT_NOT_USABLE", "OUT_OF_SCOPE"} and item[
                        "selector"
                    ].get("asset_version_id"):
                        missing_bindings[item["selector"]["asset_version_id"]] = item["reason"]
        if previous is not None:
            exact_ids = set(previous["reference_version_ids"])
        if not set(map(str, parameters.reference_version_ids)) <= exact_ids:
            raise DomainError(
                "REVIEW_REFERENCE_INVALID", "只能选择镜头或生成清单绑定的精确参考版本", 422
            )
        materials, missing, assets, sources = [], [], [], []
        reused = False

        def material(kind, source_id, content, bound=None):
            nonlocal reused
            saved = bindings.get((kind, str(source_id)))
            if saved is not None:
                reused = True
                item = deepcopy(saved) | {"manifest_id": str(identity)}
                if kind == "SHOT" and item["content"]["shot_sha256"] != content["shot_sha256"]:
                    raise DomainError("REVIEW_REFERENCE_CHANGED", "已取证镜头内容发生变化", 409)
            else:
                item = {
                    "manifest_id": str(identity),
                    "source_kind": kind,
                    "source_id": str(source_id),
                    "bound_manifest_id": str(bound or identity),
                    "content_sha256": digest(content),
                    "content": deepcopy(content),
                }
            materials.append(item)
            return item["content"]

        def absent(selector, reason):
            missing.append({"selector": selector, "reason": reason})

        def asset_material(version_id, *, candidate=False, core=False):
            nonlocal reused
            selector = {"asset_version_id": str(version_id)}
            if str(version_id) in missing_bindings and not core:
                absent(selector, missing_bindings[str(version_id)])
                reused = True
                return
            try:
                resolved = resolve_assets(
                    db,
                    root.project_id,
                    {str(version_id)},
                    limits,
                    candidate=version.id if candidate else None,
                    follow_profile_references=False,
                )[0]
            except DomainError as error:
                if core or error.code not in {"NOT_FOUND", "CONTEXT_INPUT_NOT_USABLE"}:
                    raise
                absent(selector, "NOT_FOUND" if error.code == "NOT_FOUND" else "INPUT_NOT_USABLE")
                return
            asset = db.get(VisualAsset, UUID(resolved["asset_id"]), populate_existing=True)
            assert asset is not None
            if not allowed_for_episode(asset, run.episode_id):
                if core:
                    raise DomainError("CONTEXT_SCOPE_NOT_ALLOWED", "候选不在当前剧集使用范围", 403)
                absent(selector, "OUT_OF_SCOPE")
                return
            kind = "CANDIDATE" if candidate else "ASSET"
            content = material(
                kind, target.candidate_id if candidate else version_id, {"asset": resolved}
            )
            resolved = content["asset"]
            source = (
                source_bindings.get(str(version_id))
                or asset_sources(db, root.project_id, [resolved])[0]
            )
            assets.append(deepcopy(resolved))
            sources.append(deepcopy(source))
            if source["object_ref"] is None and not core:
                absent(selector, "NO_IMAGE")
            if asset.asset_type in {"CHARACTER", "SCENE", "PROP", "STYLE"}:
                exact_ids.update(reference_ids(resolved["profile"]))

        asset_material(version.id, candidate=True, core=True)
        # Registered text versions give the shared source validator exact board/script hashes.
        for version_id in (board.id, board.script_version_id):
            resolved = resolve_assets(
                db, root.project_id, {str(version_id)}, limits, follow_profile_references=False
            )[0]
            assets.append(resolved)
            sources.extend(asset_sources(db, root.project_id, [resolved]))
        material(
            "STORYBOARD",
            board.id,
            {
                "script_version_id": str(board.script_version_id),
                "content_sha256": digest(board.content_json),
                "version_no": board.version_no,
            },
        )
        material(
            "SCRIPT",
            board.script_version_id,
            {
                "scenes": local["scenes"],
                "character_assets": local["character_assets"],
            },
        )
        details = {item["character"]: item for item in local["shot"].get("character_details", [])}
        characters = {item["character"] for item in local["character_assets"]}
        for name in local["shot"].get("characters", []):
            detail = details.get(name, {})
            for role, available in (
                ("CHARACTER", detail.get("asset") or name in characters),
                ("COSTUME", detail.get("costume")),
            ):
                if not available:
                    absent(
                        {"role": role, "character": name, "shot_id": str(shot.id)},
                        "NO_EXACT_REFERENCE",
                    )
        scene: dict = next(
            (row for row in local["scenes"] if row["scene_no"] == local["shot"]["scene_no"]), {}
        )
        if not any(
            value.get(field)
            for value in (local["shot"], scene)
            for field in ("scene_asset", "location_asset")
        ):
            absent({"role": "SCENE", "shot_id": str(shot.id)}, "NO_EXACT_REFERENCE")
        for value in [local["shot"], *local["neighbors"]]:
            current = db.get(Shot, UUID(value["id"]), populate_existing=True)
            assert current is not None
            if current.status != "APPROVED":
                raise DomainError("CONTEXT_INPUT_NOT_USABLE", "相邻镜头已失效", 409)
            if current.id != shot.id and not value.get("selected_visual_asset"):
                absent({"role": "ADJACENT_IMAGE", "shot_id": str(current.id)}, "NO_EXACT_REFERENCE")
            material(
                "SHOT",
                current.id,
                {
                    "storyboard_id": str(board.id),
                    "shot_no": current.shot_no,
                    "shot_sha256": shot_sha256(current),
                    "shot": value,
                },
            )
        for offset in (-1, 1):
            if not any(
                item["content"]["shot_no"] == shot.shot_no + offset
                for item in materials
                if item["source_kind"] == "SHOT"
            ):
                absent(
                    {"storyboard_id": str(board.id), "shot_no": shot.shot_no + offset},
                    "NO_ADJACENT_SHOT",
                )
        style = required["global"].get("style", {})
        material(
            "VISUAL",
            style.get("version_id") or root.id,
            {
                "global": required["global"],
                "constraints": required["constraints"],
            },
            root.id,
        )
        sources.extend(
            deepcopy(item)
            for item in root.snapshot_json["context"]["trace"]["sources"]
            if item["kind"] == "VISUAL_BIBLE"
        )
        if not style.get("version_id"):
            absent({"role": "VISUAL_BIBLE"}, "NO_VISUAL_BIBLE")
        if not exact_ids:
            absent({"role": "SHOT_REFERENCES"}, "NO_EXACT_REFERENCE")
        for version_id in parameters.reference_version_ids:
            asset_material(version_id)
        selected = set(map(str, parameters.reference_version_ids))
        for reference_id in sorted(exact_ids - selected):
            absent({"asset_version_id": reference_id}, "NOT_REQUESTED")
        if len(exact_ids) > limits.max_assets:
            raise DomainError("CONTEXT_TOO_LARGE", "审查精确参考索引超过限额", 422)
        validate_sources(db, root.project_id, sources)
        verify_images(db, settings, root.project_id, sources)
        result_value = {
            "manifest_id": identity,
            "startup_manifest_id": root.id,
            "evidence_round": len(rounds) + 1,
            "target": target_json,
            "reference_version_ids": sorted(exact_ids),
            "materials": materials,
            "missing": missing,
            "image_file_ids": [
                item["object_ref"]["file_id"]
                for item in sources
                if parameters.include_images and item.get("object_ref")
            ],
            "reused_binding": reused,
        }
        if len(json.dumps(result_value, ensure_ascii=False, default=str).encode()) > 65536:
            raise DomainError("CONTEXT_TOO_LARGE", "审查取证反馈超限，请缩小所选参考范围", 422)
        if len(result_value["image_file_ids"]) > 8:
            raise DomainError("GATEWAY_CAPABILITY_REJECTED", "受控图片超过8张，请减少本轮参考", 422)
        result = InvestigateContinuityResult.model_validate(result_value).model_dump(mode="json")
        return {
            "sources": sources,
            "assets": assets,
            "continuity_investigation": {"result": result},
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
    return deepcopy(row.snapshot_json["context"]["continuity_investigation"]["result"])
