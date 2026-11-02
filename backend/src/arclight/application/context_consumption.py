"""Pin the exact evidence prefix before a text stage's first provider attempt."""

from copy import deepcopy
from uuid import UUID, uuid5

from sqlalchemy.orm import Session

from arclight.application.context_evidence import evidence_rounds
from arclight.application.context_sources import invalid, verify_images
from arclight.application.dependencies import lock_dependency_graph
from arclight.core.config import Settings
from arclight.domain.versions import digest
from arclight.persistence.models import DependencyManifest, WorkflowRun


def consumption(db: Session, root: DependencyManifest) -> dict | None:
    row = db.get(DependencyManifest, uuid5(root.id, "context-consumption"))
    if row is None:
        return None
    value = row.snapshot_json.get("context", {})
    initial = root.snapshot_json["context"]
    if (
        row.project_id != root.project_id
        or digest(row.snapshot_json) != row.request_fingerprint
        or value.get("origin") != "context_consumption"
        or value.get("startup_manifest_id") != str(root.id)
        or value.get("run_id") != initial["run_id"]
        or value.get("stage") != initial["task_context"]["task"]
    ):
        invalid()
    rounds = evidence_rounds(db, root)
    refs = value["evidence_manifest_ids"]
    if refs != [str(item.id) for item in rounds[: len(refs)]] or len(refs) > len(rounds):
        invalid()
    return {"consumption_manifest_id": str(row.id), **deepcopy(value)}


def consume_context(
    db: Session, project_id: UUID, run: WorkflowRun, stage: str, settings: Settings
) -> dict[str, dict]:
    # Callers commit this receipt before invoking a checkpointed graph. Single-shot
    # analysis/outline writers commit it with their output under the same project gate.
    lock_dependency_graph(db, project_id)
    from arclight.application.context_snapshots import read_contexts

    contexts = read_contexts(db, project_id, run)
    context = contexts.get(stage, {})
    if "trace_ref" not in context:
        return contexts  # Older runs never acquire invented startup provenance.
    root = db.get(DependencyManifest, UUID(context["trace_ref"]["startup_manifest_id"]))
    assert root is not None
    receipt = consumption(db, root)
    if receipt is None:
        value = {
            "schema_version": 1,
            "target": deepcopy(root.snapshot_json["target"]),
            "inputs": [],
            "context": {
                "origin": "context_consumption",
                "run_id": str(run.id),
                "stage": stage,
                **deepcopy(context["trace_ref"]),
            },
        }
        db.add(
            DependencyManifest(
                id=uuid5(root.id, "context-consumption"),
                project_id=project_id,
                creation_key=uuid5(root.id, "context-consumption"),
                target_asset_id=None,
                created_by="workflow:" + str(run.id),
                snapshot_json=value,
                request_fingerprint=digest(value),
            )
        )
        db.flush()
        receipt = consumption(db, root)
        assert receipt is not None
    refs = receipt["evidence_manifest_ids"]
    context["retrieval_evidence"] = context["retrieval_evidence"][: len(refs)]
    context["trace_ref"]["evidence_manifest_ids"] = refs
    sources = list(root.snapshot_json["context"]["trace"]["sources"])
    for evidence in context["retrieval_evidence"]:
        sources.extend(evidence["sources"])
    verify_images(db, settings, project_id, sources)
    return contexts
