"""Screenwriter investigation on the existing authorized, immutable evidence chain."""

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.content_dependencies import reference_ids
from arclight.application.context_builder import resolve_assets
from arclight.application.context_evidence import append_resolved_evidence, read_trace, round_key
from arclight.application.context_sources import asset_sources, source_snapshot, verify_images
from arclight.application.source_objects import source_text
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.context import ContextLimits
from arclight.domain.screenwriter_research import (
    CatalogQuery,
    EntrySearchQuery,
    ExactQuery,
    InvestigateStoryParameters,
    InvestigateStoryResult,
    NeighborQuery,
    ResearchMaterial,
    StartupQuery,
)
from arclight.domain.versions import digest
from arclight.persistence.models import (
    DependencyManifest,
    Episode,
    Script,
    StoryAnalysis,
    StoryEntry,
    StoryOutline,
    StorySource,
    WorkflowRun,
)

type ResearchRow = StoryEntry | StorySource | StoryOutline | StoryAnalysis | Script
_MODELS: dict[str, type[ResearchRow]] = {
    "STORY_ENTRY": StoryEntry,
    "STORY_SOURCE": StorySource,
    "OUTLINE": StoryOutline,
    "ANALYSIS": StoryAnalysis,
    "SCRIPT": Script,
}
_CATALOG_MODELS: dict[str, type[StorySource] | type[StoryOutline] | type[StoryAnalysis]] = {
    "STORY_SOURCE": StorySource,
    "OUTLINE": StoryOutline,
    "ANALYSIS": StoryAnalysis,
}


def _material(kind: str, identity: UUID, manifest_id: UUID, source: dict, content: dict) -> dict:
    return ResearchMaterial.model_validate(
        {
            "source_kind": kind,
            "source_id": identity,
            "bound_manifest_id": manifest_id,
            "source": source,
            "content": deepcopy(content),
            "content_sha256": digest(content),
        }
    ).model_dump(mode="json")


def _same_slice(content: dict, selection: dict | None) -> bool:
    saved = content.get("selection")
    if selection is None:
        return saved is None
    return (
        saved is not None
        and saved["field"] == selection["field"]
        and saved["start"] == selection["start"]
        and saved["end"] == min(selection["start"] + selection["length"], saved["total"])
    )


def _bindings(root: DependencyManifest, rounds: list[DependencyManifest]) -> list[dict]:
    """Include original CTX sources, so another retrieval path cannot silently refresh them."""
    initial = root.snapshot_json["context"]
    containers = [
        (
            root.id,
            initial.get("trace", {}),
            initial["task_context"]["required"].get("story", {}).get("entries", []),
        ),
        *[(row.id, row.snapshot_json["context"], None) for row in rounds],
    ]
    result = []
    for identity, container, entries in containers:
        sources = {
            source["id"]: source
            for source in container.get("sources", [])
            if source["kind"] == "STORY_ENTRY"
        }
        for entry in entries if entries is not None else container.get("story_entries", []):
            if entry["id"] in sources:
                result.append(
                    _material(
                        "STORY_ENTRY", UUID(entry["id"]), identity, sources[entry["id"]], entry
                    )
                )
        result.extend(container.get("investigation", {}).get("materials", []))
    return result


@dataclass
class _ResearchReader:
    db: Session
    settings: Settings
    run: WorkflowRun
    root: DependencyManifest
    rounds: list[DependencyManifest]
    limits: ContextLimits
    parameters: InvestigateStoryParameters

    @property
    def manifest_id(self) -> UUID:
        return round_key(self.root.id, len(self.rounds) + 1)

    def select(self) -> tuple[list[dict], list[dict], bool, bool]:
        query = self.parameters.query
        query_json = query.model_dump(mode="json")
        if not self.parameters.refresh:
            for row in reversed(self.rounds):
                previous = row.snapshot_json["context"].get("investigation", {})
                if previous.get("query") == query_json:
                    return (
                        deepcopy(previous["selected"]),
                        deepcopy(previous["unresolved"]),
                        previous["has_more"],
                        True,
                    )
        if isinstance(query, StartupQuery):
            return [], [], False, False
        if isinstance(query, ExactQuery):
            return [query_json], [], False, False
        if isinstance(query, EntrySearchQuery):
            statement = select(StoryEntry).where(
                StoryEntry.project_id == self.root.project_id,
                StoryEntry.archived.is_(False),
                StoryEntry.name.icontains(query.name, autoescape=True),
            )
            if query.entry_kind is not None:
                statement = statement.where(StoryEntry.kind == query.entry_kind)
            rows = self.db.scalars(
                statement.order_by(StoryEntry.name, StoryEntry.id).limit(query.limit + 1)
            ).all()
            selected = [
                {"path": "exact", "source_kind": "STORY_ENTRY", "source_id": str(row.id)}
                for row in rows[: query.limit]
            ]
            return (
                selected,
                [] if rows else [self.missing(query_json, "NOT_FOUND")],
                len(rows) > query.limit,
                False,
            )
        if isinstance(query, CatalogQuery):
            if not self.parameters.refresh:
                containers = [
                    self.root.snapshot_json["context"].get("trace", {}),
                    *[row.snapshot_json["context"] for row in self.rounds],
                ]
                bound_ids = list(
                    dict.fromkeys(
                        source.get("source_id") or source["id"]
                        for container in reversed(containers)
                        for source in container.get("sources", [])
                        if source["kind"] == query.source_kind
                    )
                )
                if bound_ids:
                    return (
                        [
                            {
                                "path": "exact",
                                "source_kind": query.source_kind,
                                "source_id": identity,
                            }
                            for identity in bound_ids[: query.limit]
                        ],
                        [],
                        len(bound_ids) > query.limit,
                        True,
                    )
            model = _CATALOG_MODELS[query.source_kind]
            statement = select(model)
            if model is StoryOutline:
                statement = statement.where(
                    StoryOutline.project_id == self.root.project_id,
                    StoryOutline.status == "APPROVED",
                )
            else:
                statement = statement.where(model.episode_id == self.run.episode_id)
            order = {
                "STORY_SOURCE": StorySource.import_no,
                "OUTLINE": StoryOutline.version_no,
                "ANALYSIS": StoryAnalysis.version_no,
            }[query.source_kind]
            rows = self.db.scalars(statement.order_by(order.desc()).limit(query.limit + 1)).all()
            selected = [
                {"path": "exact", "source_kind": query.source_kind, "source_id": str(row.id)}
                for row in rows[: query.limit]
            ]
            return (
                selected,
                [] if rows else [self.missing(query_json, "NOT_FOUND")],
                len(rows) > query.limit,
                False,
            )
        assert isinstance(query, NeighborQuery)
        current = self.db.get(Episode, self.run.episode_id)
        assert current is not None
        bound_scripts = {}
        if not self.parameters.refresh:
            for material in _bindings(self.root, self.rounds):
                if material["source_kind"] == "SCRIPT":
                    bound = self.db.get(Script, UUID(material["source_id"]))
                    if bound is not None:
                        bound_scripts[bound.episode_id] = bound
        selected, missing = [], []
        for offset in query.offsets:
            selector = {"path": "neighbors", "offset": offset}
            episode = self.db.scalar(
                select(Episode).where(
                    Episode.project_id == self.root.project_id,
                    Episode.episode_no == current.episode_no + offset,
                )
            )
            if episode is None or episode.status == "ARCHIVED":
                missing.append(
                    self.missing(selector, "NOT_FOUND" if episode is None else "ARCHIVED")
                )
                continue
            script = bound_scripts.get(episode.id) or self.db.scalar(
                select(Script)
                .where(Script.episode_id == episode.id, Script.status == "APPROVED")
                .order_by(Script.version_no.desc())
                .limit(1)
            )
            if script is None:
                missing.append(self.missing(selector, "NOT_CONFIRMED"))
            else:
                selected.append(
                    {"path": "exact", "source_kind": "SCRIPT", "source_id": str(script.id)}
                )
        return selected, missing, False, False

    @staticmethod
    def missing(selector: dict, reason: str) -> dict:
        return {"selector": selector, "reason": reason}

    def read(
        self, selector: dict, bindings: list[dict]
    ) -> tuple[dict | None, list[dict], str | None]:
        kind = selector["source_kind"]
        identity = UUID(selector["source_id"])
        row = cast(ResearchRow | None, self.db.get(_MODELS[kind], identity, populate_existing=True))
        if row is None:
            return None, [], "NOT_FOUND"
        if isinstance(row, (StoryEntry, StoryOutline)):
            if row.project_id != self.root.project_id:
                return None, [], "NOT_FOUND"
        else:
            episode = self.db.get(Episode, row.episode_id, populate_existing=True)
            if episode is None or episode.project_id != self.root.project_id:
                return None, [], "NOT_FOUND"
        if isinstance(row, StoryEntry):
            if row.archived:
                return None, [], "ARCHIVED"
        else:
            episode = self.db.get(Episode, row.episode_id, populate_existing=True)
            if episode is None or episode.status == "ARCHIVED":
                return None, [], "ARCHIVED"
        if isinstance(row, (StoryOutline, Script)) and row.status != "APPROVED":
            return None, [], "NOT_CONFIRMED"
        revision = selector.get("revision")
        bound = next(
            (
                item
                for item in reversed(bindings)
                if item["source_kind"] == kind
                and item["source_id"] == str(identity)
                and (revision is None or item["source"].get("revision") == revision)
                and _same_slice(item["content"], selector.get("selection"))
            ),
            None,
        )
        if self.parameters.refresh and revision is None:
            bound = None
        if (
            isinstance(row, StoryEntry)
            and revision is not None
            and bound is None
            and row.revision != revision
        ):
            return None, [], "REVISION_UNAVAILABLE"
        proof: dict
        content: dict
        if isinstance(row, StorySource):
            proof = source_snapshot(self.db, self.root.project_id, identity)
            if proof["rights_status"] != "CONFIRMED":
                return None, [], "RIGHTS_UNCONFIRMED"
            # Historical evidence remains readable if an object is later lost. A new
            # tool call must still check the current private object before returning it.
            try:
                text = source_text(self.db, self.settings, row)
            except DomainError as error:
                if error.code != "SOURCE_OBJECT_UNAVAILABLE":
                    raise
                return None, [], "SOURCE_UNAVAILABLE"
            content = {"text": text}
        elif isinstance(row, StoryEntry):
            content = {
                **deepcopy(row.data_json),
                "id": str(row.id),
                "revision": row.revision,
                "kind": row.kind,
                "name": row.name,
            }
            proof = {
                "kind": kind,
                "id": str(identity),
                "revision": row.revision,
                "sha256": digest(content),
            }
        else:
            content = deepcopy(row.content_json)
            proof = {
                "kind": kind,
                "id": str(identity),
                "version_no": row.version_no,
                "sha256": digest(content),
            }
        selection = selector.get("selection")
        if selection is not None:
            field = selection["field"]
            value = content.get(field)
            if not isinstance(value, str if field == "text" else list):
                return None, [], "NOT_FOUND"
            start, length = selection["start"], selection["length"]
            if start >= len(value):
                return None, [], "NOT_FOUND"
            end = min(start + length, len(value))
            content = {
                field: deepcopy(value[start:end]),
                "selection": {"field": field, "start": start, "end": end, "total": len(value)},
            }
            # A different page of an immutable source is a distinct observation.
            if bound is not None and bound["content_sha256"] != digest(content):
                bound = None
        elif bound is not None and "selection" in bound["content"]:
            bound = None
        assets = []
        if bound is not None:
            content = deepcopy(bound["content"])
            proof = deepcopy(bound["source"])
        identities = reference_ids(content)
        if isinstance(row, Script):
            identities.add(str(identity))
        try:
            assets = resolve_assets(self.db, self.root.project_id, identities, self.limits)
            if isinstance(row, Script):
                from arclight.persistence.models import AssetVersion

                version = self.db.get(AssetVersion, identity)
                assert version is not None
                checksum = hashlib.sha256(
                    json.dumps(row.content_json, sort_keys=True).encode()
                ).hexdigest()
                if version.checksum != checksum:
                    raise DomainError("CONTEXT_REFERENCE_INVALID", "剧本内容与精确资产不匹配", 409)
                proof = next(
                    source
                    for source in asset_sources(self.db, self.root.project_id, assets)
                    if source["asset_version_id"] == str(identity)
                )
            verify_images(
                self.db,
                self.settings,
                self.root.project_id,
                asset_sources(self.db, self.root.project_id, assets),
            )
        except DomainError as error:
            if error.code in {"CONTEXT_TOO_LARGE", "AGENT_MEMORY_LIMIT"}:
                raise
            return None, [], "INPUT_NOT_USABLE"
        material = bound or _material(kind, identity, self.manifest_id, proof, content)
        return deepcopy(material), assets, None

    def collect(self) -> dict:
        selected, unresolved, has_more, reused = self.select()
        bindings = _bindings(self.root, self.rounds)
        materials, missing = [], deepcopy(unresolved)
        assets: dict[str, dict] = {}
        if isinstance(self.parameters.query, StartupQuery):
            initial = self.root.snapshot_json["context"]
            content = {
                "input": initial["trace"]["startup_input"],
                "global": initial["task_context"]["required"]["global"],
            }
            materials.append(
                _material(
                    "STARTUP",
                    self.root.id,
                    self.root.id,
                    {
                        "kind": "STARTUP_MANIFEST",
                        "id": str(self.root.id),
                        "sha256": self.root.request_fingerprint,
                    },
                    content,
                )
            )
            if not content["input"].get("story") and not content["input"].get("source_snapshot"):
                missing.append(self.missing({"path": "startup"}, "STORY_NOT_PROVIDED"))
        for selector in selected:
            material, references, reason = self.read(selector, bindings)
            if reason is not None:
                missing.append(self.missing(selector, reason))
                continue
            assert material is not None
            materials.append(material)
            reused |= material["bound_manifest_id"] != str(self.manifest_id)
            assets.update((asset["asset_version_id"], asset) for asset in references)
        result = InvestigateStoryResult.model_validate(
            {
                "manifest_id": self.manifest_id,
                "startup_manifest_id": self.root.id,
                "evidence_round": len(self.rounds) + 1,
                "materials": materials,
                "missing": missing,
                "has_more": has_more,
                "reused_binding": reused,
            }
        ).model_dump(mode="json")
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > 65536:
            raise DomainError("CONTEXT_TOO_LARGE", "调查正文超过工具反馈上限，请缩小检索范围", 422)
        sources = {
            digest(item["source"]): item["source"]
            for item in materials
            if item["source_kind"] != "STARTUP"
        }
        sources.update(
            (digest(source), source)
            for source in asset_sources(self.db, self.root.project_id, list(assets.values()))
        )
        return {
            "sources": list(sources.values()),
            "assets": list(assets.values()),
            "investigation": {
                "schema_version": "screenwriter-research/1",
                "query": self.parameters.query.model_dump(mode="json"),
                "refresh": self.parameters.refresh,
                "selected": selected,
                "unresolved": unresolved,
                **{
                    name: result[name]
                    for name in ("materials", "missing", "has_more", "reused_binding")
                },
            },
        }


def investigate_story(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    stage: str,
    actor: str,
    parameters: InvestigateStoryParameters,
) -> dict:
    """Called only by the registered executor, inside its invocation transaction."""
    trace = read_trace(db, run, stage, actor)

    def resolve(root, rounds, limits):
        return _ResearchReader(db, settings, run, root, rounds, limits, parameters).collect()

    row, _ = append_resolved_evidence(
        db,
        run,
        stage,
        actor,
        creation_key=uuid4(),
        expected_round=len(trace["evidence"]),
        request_sha256=digest(parameters.model_dump(mode="json")),
        resolve=resolve,
    )
    value = row.snapshot_json["context"]["investigation"]
    return {
        "manifest_id": str(row.id),
        "startup_manifest_id": trace["startup_manifest_id"],
        "evidence_round": row.snapshot_json["context"]["round"],
        **{name: value[name] for name in ("materials", "missing", "has_more", "reused_binding")},
    }
