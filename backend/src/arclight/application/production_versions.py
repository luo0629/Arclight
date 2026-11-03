"""One append-only registry and compatibility gate for all production families."""

from copy import deepcopy
from uuid import UUID, uuid5

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContent, StoryboardContent
from arclight.agents.script_rewrite import RewriteResult
from arclight.agents.story_analysis import StoryAnalysisContent
from arclight.agents.story_outline import StoryOutlineContent
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.generation_prompts import (
    PROMPT_COMPOSER_DEFINITION,
    PROMPT_COMPOSER_VERSION,
)
from arclight.domain.generation_requests import GenerationResult, GenerationSpec
from arclight.domain.versions import VersionRegistration, digest
from arclight.persistence.models import (
    DependencyManifest,
    ProductionVersion,
    VersionBinding,
    WorkflowRun,
)
from arclight.providers.prompts import TEXT_MAX_TOKENS, TEXT_PROMPTS

TEXT_STAGES = {
    "script_storyboard": ("script", "storyboard"),
    "storyboard": ("storyboard",),
    "script_rewrite": ("rewrite",),
    "story_analysis": ("analyze",),
    "story_outline": ("outline",),
}
SCHEMAS: dict[str, type[BaseModel]] = {
    "script": ScriptContent,
    "storyboard": StoryboardContent,
    "rewrite": RewriteResult,
    "analyze": StoryAnalysisContent,
    "outline": StoryOutlineContent,
}


def register(
    db: Session, project_id: UUID, specification: VersionRegistration
) -> ProductionVersion:
    specification = VersionRegistration.model_validate(specification.model_dump(mode="json"))
    value = specification.model_dump(mode="json")
    checksum = digest(value["definition"])
    identity = uuid5(project_id, "production-version:" + digest(value))
    row = db.get(ProductionVersion, identity)
    if row is None:
        row = ProductionVersion(
            id=identity,
            project_id=project_id,
            family=specification.family,
            kind=specification.kind,
            name=specification.name,
            version=specification.version,
            digest=checksum,
            definition_json=deepcopy(specification.definition),
        )
        db.add(row)
        db.flush()
    return row


def find_binding(db: Session, project_id: UUID, kind: str, identity: UUID) -> VersionBinding | None:
    return db.scalar(
        select(VersionBinding).where(
            VersionBinding.project_id == project_id,
            VersionBinding.resource_type == kind,
            VersionBinding.resource_id == identity,
        )
    )


def bind(
    db: Session, project_id: UUID, kind: str, identity: UUID, snapshot: dict
) -> VersionBinding:
    row = find_binding(db, project_id, kind, identity)
    if row is not None:
        if row.snapshot_json != snapshot:
            raise DomainError("VERSION_BINDING_CONFLICT", "历史版本绑定不能覆盖", 409)
        return row
    row = VersionBinding(
        project_id=project_id,
        resource_type=kind,
        resource_id=identity,
        snapshot_json=deepcopy(snapshot),
    )
    db.add(row)
    db.flush()
    return row


def component(
    db: Session, project_id: UUID, family: str, kind: str, name: str, version: str, definition: dict
) -> dict:
    entry = register(
        db,
        project_id,
        VersionRegistration.model_validate(
            {
                "family": family,
                "kind": kind,
                "name": name,
                "version": version,
                "definition": definition,
            }
        ),
    )
    return {
        "id": str(entry.id),
        "family": family,
        "kind": kind,
        "name": name,
        "version": version,
        "digest": entry.digest,
    }


def text_definitions(stage: str, workflow_version: str) -> list[tuple[str, str, dict]]:
    schema_field = SCHEMAS[stage].model_fields.get("schema_version")
    schema_version = str(schema_field.default) if schema_field is not None else "1"
    return [
        ("AGENT", workflow_version, {"operation": stage, "workflow_version": workflow_version}),
        ("PROMPT", "1", {"system_template": TEXT_PROMPTS[stage]}),
        ("SCHEMA", schema_version, SCHEMAS[stage].model_json_schema()),
    ]


def input_summary(inputs: dict) -> dict:
    """Reference identities plus content digest; credentials never enter the registry."""
    value = {key: item for key, item in inputs.items() if not key.startswith("_")}
    references: list[dict] = []

    def walk(item):
        if isinstance(item, dict):
            found = {
                key: val
                for key, val in item.items()
                if (key.endswith("_id") or key.endswith("_sha256") or key == "version_no")
                and isinstance(val, (str, int))
            }
            if found:
                references.append(found)
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return {"sha256": digest(value), "references": sorted(references, key=digest)}


def capture_text(
    db: Session, project_id: UUID, run: WorkflowRun, settings: Settings, *, actor: str | None = None
) -> None:
    from arclight.application.context_snapshots import capture_contexts

    contexts = capture_contexts(db, project_id, run, settings, actor)
    service = run.input_json.get("_text_service")
    model = {
        "provider": "openai_compatible" if service else settings.provider,
        "model": service["model"] if service else settings.llm_model,
        "base_url": service["base_url"] if service else settings.llm_base_url,
    }
    parameters = {
        "max_tokens": TEXT_MAX_TOKENS,
        "timeout_seconds": settings.llm_timeout_seconds,
        "duration_tolerance_percent": settings.text_duration_tolerance_percent,
    }
    entries = [
        component(db, project_id, "TEXT", kind, stage, version, definition)
        for stage in TEXT_STAGES[run.workflow_type]
        for kind, version, definition in text_definitions(stage, run.workflow_version)
    ]
    entries.append(component(db, project_id, "TEXT", "MODEL", "text-model", digest(model), model))
    bind(
        db,
        project_id,
        "WORKFLOW",
        run.id,
        {
            "schema_version": 1,
            "compatibility": "EXACT_DEFINITIONS",
            "workflow_type": run.workflow_type,
            "workflow_version": run.workflow_version,
            "components": entries,
            "model": model,
            "parameters": parameters,
            "parameter_sha256": digest(parameters),
            "inputs": input_summary(run.input_json),
            "context_manifest_ids": contexts,
        },
    )


def checked_snapshot(db: Session, binding: VersionBinding) -> dict:
    value = binding.snapshot_json
    if value.get("schema_version") != 1 or value.get("compatibility") != "EXACT_DEFINITIONS":
        raise DomainError("PRODUCTION_VERSION_INCOMPATIBLE", "版本合同不支持恢复", 409)
    for entry in value["components"]:
        row = db.get(ProductionVersion, UUID(entry["id"]))
        if row is None or row.project_id != binding.project_id or row.digest != entry["digest"]:
            raise DomainError("PRODUCTION_VERSION_INCOMPATIBLE", "版本登记缺失或不匹配", 409)
    if value["parameter_sha256"] != digest(value["parameters"]):
        raise DomainError("PRODUCTION_VERSION_INCOMPATIBLE", "参数版本摘要不匹配", 409)
    return value


def text_settings(db: Session, project_id: UUID, run: WorkflowRun, settings: Settings) -> Settings:
    binding = find_binding(db, project_id, "WORKFLOW", run.id)
    if binding is None:
        # Historical records retain their existing workflow-version compatibility policy.
        return settings
    snapshot = checked_snapshot(db, binding)
    expected = {
        (stage, kind): digest(definition)
        for stage in TEXT_STAGES.get(run.workflow_type, ())
        for kind, _, definition in text_definitions(stage, run.workflow_version)
    }
    if (
        snapshot["workflow_type"] != run.workflow_type
        or snapshot["parameters"]["max_tokens"] != TEXT_MAX_TOKENS
        or snapshot["workflow_version"] != run.workflow_version
        or snapshot["inputs"] != input_summary(run.input_json)
        or {
            (entry["name"], entry["kind"]): entry["digest"]
            for entry in snapshot["components"]
            if entry["kind"] != "MODEL"
        }
        != expected
    ):
        raise DomainError("PRODUCTION_VERSION_INCOMPATIBLE", "提示词、Agent或Schema不兼容恢复", 409)
    model, parameters = snapshot["model"], snapshot["parameters"]
    return settings.model_copy(
        update={
            "provider": model["provider"],
            "llm_model": model["model"],
            "llm_base_url": model["base_url"],
            "llm_timeout_seconds": parameters["timeout_seconds"],
            "text_duration_tolerance_percent": parameters["duration_tolerance_percent"],
        }
    )


def capture_media(
    db: Session,
    project_id: UUID,
    job_id: UUID,
    manifest_id: UUID,
    task_type: str,
    spec: GenerationSpec,
    inputs: list[dict],
    *,
    prompt_composition: dict | None = None,
) -> None:
    entries = [
        component(
            db,
            project_id,
            "MEDIA",
            "AGENT",
            "generation-service",
            "1",
            {"service": "generation-service", "version": 1},
        ),
        component(
            db,
            project_id,
            "MEDIA",
            "PROMPT",
            "request-template",
            spec.prompt_version,
            {"origin": "REQUEST_DECLARED", "template_version": spec.prompt_version},
        ),
        component(
            db,
            project_id,
            "MEDIA",
            "SCHEMA",
            "generation-input",
            "1",
            GenerationSpec.model_json_schema(),
        ),
        component(
            db,
            project_id,
            "MEDIA",
            "SCHEMA",
            "generation-result",
            "1",
            GenerationResult.model_json_schema(),
        ),
        component(
            db,
            project_id,
            "MEDIA",
            "MODEL",
            "media-model",
            spec.model_config_version,
            {
                "provider": spec.provider,
                "model": spec.model,
                "configuration_version": spec.model_config_version,
            },
        ),
    ]
    if prompt_composition is not None:
        entries.append(
            component(
                db,
                project_id,
                "MEDIA",
                "PROMPT",
                "media-prompt-composer",
                PROMPT_COMPOSER_VERSION,
                PROMPT_COMPOSER_DEFINITION,
            )
        )
    parameters = {
        "parameters": spec.parameters,
        "seed": spec.seed,
        "variation": spec.variation,
        "output_spec": spec.output_spec,
        "batch_size": spec.batch_size,
    }
    bind(
        db,
        project_id,
        "GENERATION",
        job_id,
        {
            "schema_version": 1,
            "compatibility": "EXACT_DEFINITIONS",
            "components": entries,
            "task_type": task_type,
            "input_manifest_id": str(manifest_id),
            "inputs": inputs,
            "parameter_sha256": digest(parameters),
            "parameters": parameters,
            "prompt_sha256": digest(
                {"prompt": spec.prompt, "negative_prompt": spec.negative_prompt}
            ),
            **(
                {"prompt_composition_sha256": digest(prompt_composition)}
                if prompt_composition is not None
                else {}
            ),
        },
    )


def check_media(db: Session, project_id: UUID, job_id: UUID, manifest_id: UUID) -> None:
    binding = find_binding(db, project_id, "GENERATION", job_id)
    if binding is None:
        return
    value = checked_snapshot(db, binding)
    schemas = {
        entry["name"]: entry["digest"] for entry in value["components"] if entry["kind"] == "SCHEMA"
    }
    manifest = db.get(DependencyManifest, manifest_id)
    composition = (
        (manifest.snapshot_json.get("context") or {}).get("prompt_composition")
        if manifest
        else None
    )
    composition_digest = value.get("prompt_composition_sha256")
    composer_entries = [
        entry
        for entry in value["components"]
        if entry["kind"] == "PROMPT" and entry["name"] == "media-prompt-composer"
    ]
    composition_compatible = (
        composition is None and composition_digest is None and not composer_entries
    ) or (
        isinstance(composition, dict)
        and composition.get("composer_version") == PROMPT_COMPOSER_VERSION
        and composition_digest == digest(composition)
        and len(composer_entries) == 1
        and composer_entries[0]["version"] == PROMPT_COMPOSER_VERSION
        and composer_entries[0]["digest"] == digest(PROMPT_COMPOSER_DEFINITION)
    )
    if (
        value["input_manifest_id"] != str(manifest_id)
        or not composition_compatible
        or schemas
        != {
            "generation-input": digest(GenerationSpec.model_json_schema()),
            "generation-result": digest(GenerationResult.model_json_schema()),
        }
    ):
        raise DomainError("PRODUCTION_VERSION_INCOMPATIBLE", "媒体结果Schema不兼容恢复", 409)


def bind_output(
    db: Session,
    project_id: UUID,
    source_type: str,
    source_id: UUID,
    output_type: str,
    output_id: UUID,
) -> None:
    original = find_binding(db, project_id, source_type, source_id)
    if original is not None:
        bind(
            db,
            project_id,
            output_type,
            output_id,
            {**original.snapshot_json, "source_type": source_type, "source_id": str(source_id)},
        )
