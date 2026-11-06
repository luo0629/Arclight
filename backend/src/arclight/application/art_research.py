"""Art investigation appends observations to the original authorized evidence chain."""

import json
from copy import deepcopy
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.art_research_assets import (
    DIRECTORY_KINDS,
    directory_snapshot,
    read_reference,
    scoped_directory,
    usable_directory,
)
from arclight.application.context_evidence import append_resolved_evidence, round_key
from arclight.application.context_sources import verify_images
from arclight.application.director_research import approved_input
from arclight.application.model_services import run_settings
from arclight.application.production_versions import text_settings
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.art_research import (
    ArtAuthorizationQuery,
    ArtCapabilityQuery,
    ArtCatalogQuery,
    ArtDirectoryQuery,
    ArtModelQuery,
    ArtReferenceQuery,
    ArtVisualQuery,
    InvestigateArtParameters,
    InvestigateArtResult,
)
from arclight.domain.versions import digest
from arclight.persistence.models import VisualAsset, WorkflowRun
from arclight.providers.media_capabilities import resolve_capabilities


def _revalidate(db, settings, run, project_id, materials, limits):
    for item in materials:
        identity = UUID(item["source_id"])
        if item["source_kind"] == "DIRECTORY":
            asset = scoped_directory(db, project_id, identity)
            if asset is None or not usable_directory(asset):
                raise DomainError("CONTEXT_INPUT_NOT_USABLE", "已取证目录已归档或不可用", 409)
        elif item["source_kind"] in {"ASSET", "AUTHORIZATION"}:
            value = read_reference(
                db,
                settings,
                project_id,
                run.episode_id,
                identity,
                limits,
                authorization_only=item["source_kind"] == "AUTHORIZATION",
            )
            if value.reason and (
                item["source_kind"] == "ASSET"
                or value.reason in {"NOT_FOUND", "INPUT_NOT_USABLE", "OUT_OF_SCOPE"}
            ):
                raise DomainError("CONTEXT_INPUT_NOT_USABLE", "已取证参考已失效或不可用", 409)


def investigate_art(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    actor: str,
    parameters: InvestigateArtParameters,
) -> dict:
    # Existing production scope already has an exact approved screenplay and
    # frozen style. Art consultation must not wait for final storyboard approval.
    _, trace = approved_input(db, run, actor, settings)
    query = parameters.query
    query_json = query.model_dump(mode="json")

    def resolve(root, rounds, limits):
        identity = round_key(root.id, len(rounds) + 1)
        previous = None
        bindings = {}
        for row in rounds:
            investigation = row.snapshot_json["context"].get("art_investigation")
            if investigation:
                for item in investigation["result"]["materials"]:
                    bindings[item["source_kind"], item["source_id"]] = item
                if investigation["query"] == query_json:
                    previous = row.snapshot_json["context"]
        materials, missing, assets, sources, images = [], [], [], [], []
        has_more, reused = False, False
        content: dict

        def material(kind, source_id, content, bound=None):
            nonlocal reused
            saved = bindings.get((kind, str(source_id))) if not parameters.refresh else None
            if saved:
                reused = True
                return deepcopy(saved) | {"manifest_id": str(identity)}
            return {
                "manifest_id": str(identity),
                "source_kind": kind,
                "source_id": str(source_id),
                "bound_manifest_id": str(bound or identity),
                "content_sha256": digest(content),
                "content": deepcopy(content),
            }

        def absent(selector, reason):
            missing.append({"selector": selector, "reason": reason})

        if previous and not parameters.refresh:
            saved = previous["art_investigation"]["result"]
            materials = [
                deepcopy(item) | {"manifest_id": str(identity)} for item in saved["materials"]
            ]
            missing, has_more, reused = deepcopy(saved["missing"]), saved["has_more"], True
            assets, sources = deepcopy(previous["assets"]), deepcopy(previous["sources"])
            images = saved["image_file_ids"]
            _revalidate(db, settings, run, root.project_id, materials, limits)
            verify_images(db, settings, root.project_id, sources)
        elif isinstance(query, ArtVisualQuery):
            required = root.snapshot_json["context"]["task_context"]["required"]
            materials.append(
                material(
                    "VISUAL",
                    root.id,
                    {"global": required["global"], "constraints": required["constraints"]},
                    root.id,
                )
            )
        elif isinstance(query, ArtCapabilityQuery):
            try:
                declaration = resolve_capabilities(query.provider, query.model).model_dump(
                    mode="json"
                )
            except DomainError as error:
                if error.code != "PROVIDER_NOT_INSTALLED":
                    raise
                absent(query_json, error.code)
            else:
                declaration["task_types"] = sorted(declaration["task_types"])
                content = {"model": query.model, "declaration": declaration}
                materials.append(material("CAPABILITY", uuid5(root.id, digest(content)), content))
        elif isinstance(query, ArtModelQuery):
            model_settings = run_settings(
                text_settings(db, root.project_id, run, settings),
                run.input_json.get("_text_service"),
                db,
            )
            capabilities = model_settings.llm_agent_capabilities
            if capabilities is None or (
                capabilities.supplier != model_settings.llm_supplier
                or capabilities.model != model_settings.llm_model
            ):
                absent(query_json, "MODEL_CAPABILITY_UNDECLARED")
            else:
                content = {
                    "declaration": capabilities.model_dump(mode="json"),
                    "response_format": model_settings.llm_response_format,
                    "usable_for_art": capabilities.tools
                    and capabilities.max_input_images >= 2
                    and model_settings.llm_response_format in capabilities.response_formats,
                }
                materials.append(material("MODEL", uuid5(root.id, digest(content)), content))
                if not content["usable_for_art"]:
                    absent(query_json, "MODEL_CAPABILITY_INSUFFICIENT")
        elif isinstance(query, ArtReferenceQuery | ArtAuthorizationQuery):
            for version_id in query.version_ids:
                value = read_reference(
                    db,
                    settings,
                    root.project_id,
                    run.episode_id,
                    version_id,
                    limits,
                    authorization_only=isinstance(query, ArtAuthorizationQuery),
                    include_images=isinstance(query, ArtReferenceQuery) and query.include_images,
                )
                if value.content is not None:
                    kind = "ASSET" if value.assets else "AUTHORIZATION"
                    item = material(kind, version_id, value.content)
                    materials.append(item)
                    if kind == "ASSET":
                        # Directory data in a version binding must stay frozen, too.
                        assets.append(deepcopy(item["content"]["asset"]))
                    sources.extend(value.sources)
                    images.extend(value.image_file_ids)
                if value.reason:
                    absent({"asset_version_id": str(version_id)}, value.reason)
        else:
            if isinstance(query, ArtCatalogQuery):
                rows = db.scalars(
                    select(VisualAsset)
                    .where(
                        VisualAsset.project_id == root.project_id,
                        VisualAsset.asset_type == query.asset_type,
                        VisualAsset.status.not_in(["ARCHIVED", "REJECTED", "STALE"]),
                        VisualAsset.name.icontains(query.name, autoescape=True),
                    )
                    .order_by(VisualAsset.name, VisualAsset.id)
                    .offset(query.offset)
                    .limit(query.limit + 1)
                    .execution_options(populate_existing=True)
                ).all()
                has_more = len(rows) > query.limit
                selected = [row.id for row in rows[: query.limit]]
                if not selected:
                    absent(query_json, "NOT_FOUND")
            else:
                assert isinstance(query, ArtDirectoryQuery)
                selected = query.asset_ids
            for asset_id in selected:
                selector = {"asset_id": str(asset_id)}
                asset = scoped_directory(db, root.project_id, asset_id)
                if asset is None:
                    absent(selector, "NOT_FOUND")
                    continue
                if asset.asset_type not in DIRECTORY_KINDS:
                    raise DomainError(
                        "ART_REFERENCE_INVALID", "目录查询须为角色、场景、道具或风格", 422
                    )
                if not usable_directory(asset):
                    absent(selector, "INPUT_NOT_USABLE")
                    continue
                item = material(
                    "DIRECTORY", asset.id, directory_snapshot(db, asset, run.episode_id)
                )
                materials.append(item)
                content = item["content"]
                if not content["allowed_for_episode"]:
                    absent(selector, "OUT_OF_SCOPE")
                if content["directory"]["current_version_id"] is None:
                    absent(selector, "NO_SELECTED_VERSION")
                if not content["image_candidates"]:
                    absent(selector, "NO_IMAGE")
        result_value = {
            "manifest_id": identity,
            "startup_manifest_id": root.id,
            "evidence_round": len(rounds) + 1,
            "materials": materials,
            "missing": missing,
            "image_file_ids": images,
            "has_more": has_more,
            "reused_binding": reused,
        }
        if len(json.dumps(result_value, ensure_ascii=False, default=str).encode()) > 65536:
            raise DomainError("CONTEXT_TOO_LARGE", "美术调查反馈超限，请显式缩小查询范围", 422)
        result = InvestigateArtResult.model_validate(result_value).model_dump(mode="json")
        return {
            "sources": sources,
            "assets": assets,
            "art_investigation": {"query": query_json, "result": result},
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
    return deepcopy(row.snapshot_json["context"]["art_investigation"]["result"])
