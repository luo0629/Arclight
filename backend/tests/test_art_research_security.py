"""Live grants, project/scope isolation, shared limits and genuine database concurrency."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from test_agent_tools import activate_run, create_run
from test_art_research import add_image, contents, prepare_art, query, trace
from test_art_review_workflow import decide, save, submit
from test_asset_locks import lock, selected
from test_assets import create as create_asset
from test_collaboration import create_episode, create_project, register
from test_concurrency import require_postgres
from test_director_workflow import script
from test_text_contract_v2 import make_asset

from arclight.application.agent_limits import configure_agent_run
from arclight.application.agent_memory import read_agent_memory
from arclight.application.agent_tools import build_agent_tool_registry, grant_agent_tools
from arclight.application.art_proposal_store import asset_content
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_tools import AgentToolGrantRequest
from arclight.domain.versions import digest
from arclight.persistence.art_proposals import ArtApprovedVersion, ArtProposalVersion
from arclight.persistence.models import (
    AgentToolGrant,
    AgentToolInvocation,
    AssetSelectionLock,
    AssetVersion,
    DependencyManifest,
    ProjectMember,
    StoredFile,
    User,
    VisualAsset,
    VisualBibleVersion,
    WorkflowRun,
)


@pytest.mark.parametrize("path", ["directories", "references", "authorization"])
def test_foreign_reference_is_atomic_and_never_leaks_material(client, episode, settings, path):
    own = create_asset(client, episode["project_id"])
    other = create_project(client)
    foreign = create_asset(client, other["id"])
    if path == "directories":
        selector = {"path": path, "asset_ids": [own["id"], foreign["id"]]}
    else:
        a, b = add_image(client, own), add_image(client, foreign)
        selector = {"path": path, "version_ids": [a["id"], b["id"]]}
    run_id, actor, _ = prepare_art(client, episode, settings)
    before = deepcopy(trace(client, run_id, actor))
    denied = query(client, settings, run_id, selector)
    assert denied.status == "FAILED" and denied.error.code == "REFERENCE_SCOPE_INVALID"
    assert denied.result is None
    assert foreign["id"] not in denied.model_dump_json()
    assert trace(client, run_id, actor) == before


@pytest.mark.parametrize("path", ["authorization", "references"])
@pytest.mark.parametrize("defect", ["project", "checksum"])
def test_mismatched_file_receipt_cannot_expose_rights_or_sources(
    client, episode, settings, path, defect
):
    other = create_project(client)
    with client.app.state.session_factory.begin() as db:
        reference = make_asset(
            db,
            episode["project_id"],
            "PROP",
            metadata_json={"media": {"rights_confirmed": True}},
        )
        db.add(
            StoredFile(
                project_id=UUID(other["id"] if defect == "project" else episode["project_id"]),
                asset_version_id=UUID(reference["asset_version_id"]),
                storage_backend="LOCAL",
                object_key="malformed-receipt/" + uuid4().hex,
                checksum=("b" if defect == "checksum" else "a") * 64,
                byte_size=1,
                media_type="image/png",
                original_name="receipt.png",
                source_note="private-receipt-source",
                rights_note="private-receipt-rights",
                rights_confirmed=True,
                created_by="service-owner",
            )
        )
    run_id, actor, _ = prepare_art(client, episode, settings)
    before = deepcopy(trace(client, run_id, actor))
    denied = query(
        client, settings, run_id, {"path": path, "version_ids": [reference["asset_version_id"]]}
    )
    assert denied.status == "FAILED" and denied.error.code == "CONTEXT_REFERENCE_INVALID"
    assert denied.result is None
    assert "private-receipt" not in denied.model_dump_json()
    assert trace(client, run_id, actor) == before


@pytest.mark.parametrize("change", ["revoke", "downgrade", "disabled"])
def test_revocation_is_rechecked_before_replaying_cached_tool_result(client, settings, change):
    register(client, "art-owner@example.test")
    project = create_project(client)
    episode = create_episode(client, project)
    source = script(client, f"/api/v1/episodes/{episode['id']}")
    editor = register(client, "art-editor@example.test")["user"]["id"]
    with client.app.state.session_factory.begin() as db:
        db.add(ProjectMember(project_id=UUID(project["id"]), user_id=UUID(editor), role="editor"))
    run_id, actor, _ = prepare_art(client, episode, settings, source=source)
    first = query(client, settings, run_id, {"path": "visual"}, key="cached-art")
    assert len(contents(first)) == 1
    with client.app.state.session_factory.begin() as db:
        member = db.scalar(select(ProjectMember).where(ProjectMember.user_id == UUID(editor)))
        if change == "revoke":
            db.delete(member)
        elif change == "downgrade":
            member.role = "viewer"
        else:
            db.get(User, UUID(editor)).disabled = True
    cached = query(client, settings, run_id, {"path": "visual"}, key="cached-art")
    assert cached.status == "FAILED" and cached.error.code == "USER_NOT_AUTHORIZED"
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AgentToolInvocation)
                .where(AgentToolInvocation.run_id == run_id)
            )
            == 1
        )
        saved = db.get(DependencyManifest, UUID(first.result["manifest_id"]))
        assert saved.snapshot_json["context"]["art_investigation"]["result"] == first.result


@pytest.mark.parametrize(
    "change,code",
    [
        ("agent", "AGENT_NOT_ALLOWED"),
        ("grant", "USER_NOT_AUTHORIZED"),
        ("cancel", "WORKFLOW_NOT_ACTIVE"),
        ("stage", "AGENT_NOT_ALLOWED"),
    ],
)
def test_only_granted_art_agent_in_active_production_step_can_investigate(
    client, episode, settings, change, code
):
    if change == "stage":
        run_id = create_run(client, episode)
        actor = activate_run(client, run_id, episode["id"])
        with client.app.state.session_factory.begin() as db:
            grant_agent_tools(
                db,
                AgentToolGrantRequest(
                    run_id=run_id, agent_name="art_director", allowed_tools=["investigate_art"]
                ),
                actor,
                build_agent_tool_registry(settings=settings),
            )
    else:
        run_id, _, _ = prepare_art(client, episode, settings)
        with client.app.state.session_factory.begin() as db:
            if change == "grant":
                row = db.scalar(
                    select(AgentToolGrant).where(
                        AgentToolGrant.run_id == run_id, AgentToolGrant.agent_name == "art_director"
                    )
                )
                row.allowed_tools_json = []
            elif change == "cancel":
                db.get(WorkflowRun, run_id).status = "CANCELLED"
    denied = query(
        client,
        settings,
        run_id,
        {"path": "visual"},
        agent="director" if change == "agent" else "art_director",
    )
    assert denied.status == "FAILED" and denied.error.code == code


def test_episode_usage_scope_is_checked_for_images_and_visible_in_directory_feedback(
    client, episode, settings
):
    project_id = episode["project_id"]
    other = client.post(
        f"/api/v1/projects/{project_id}/episodes", json={"episode_no": 2, "title": "第二集"}
    ).json()
    asset = create_asset(client, project_id)
    version = add_image(client, asset)
    url = f"/api/v1/projects/{project_id}/assets/{asset['id']}"
    asset = client.get(url).json()
    response = client.put(
        url + "/metadata",
        json={
            "expected_revision": asset["revision"],
            "metadata": {"usage_scope": {"kind": "EPISODES", "episode_ids": [other["id"]]}},
        },
    )
    assert response.status_code == 200, response.text
    run_id, _, _ = prepare_art(client, episode, settings)
    directory = query(client, settings, run_id, {"path": "directories", "asset_ids": [asset["id"]]})
    assert contents(directory)[0]["allowed_for_episode"] is False
    assert "OUT_OF_SCOPE" in {m["reason"] for m in directory.result["missing"]}
    for path in ("authorization", "references"):
        feedback = query(client, settings, run_id, {"path": path, "version_ids": [version["id"]]})
        assert (
            contents(feedback) == [] and feedback.result["missing"][0]["reason"] == "OUT_OF_SCOPE"
        )
        assert feedback.result["image_file_ids"] == []


def test_investigation_uses_original_tool_limit_and_has_no_formal_write_authority(
    client, episode, settings
):
    asset = create_asset(client, episode["project_id"])
    add_image(client, asset)
    run_id, actor, _ = prepare_art(client, episode, settings)
    models = (
        VisualAsset,
        AssetVersion,
        AssetSelectionLock,
        VisualBibleVersion,
        ArtApprovedVersion,
        ArtProposalVersion,
    )
    with client.app.state.session_factory.begin() as db:
        before = {
            model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in models
        }
        configure_agent_run(db, run_id, actor, AgentRunLimits(max_tool_calls=0))
    denied = query(client, settings, run_id, {"path": "visual"})
    assert denied.status == "FAILED" and denied.error.code == "AGENT_TOOL_CALL_LIMIT"
    assert trace(client, run_id, actor)["evidence"] == []
    with client.app.state.session_factory() as db:
        assert before == {
            model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in models
        }


def test_art_evidence_cannot_be_overwritten_in_database(client, episode, settings):
    run_id, actor, _ = prepare_art(client, episode, settings)
    first = query(client, settings, run_id, {"path": "visual"})
    assert len(contents(first)) == 1
    with pytest.raises(DBAPIError), client.app.state.session_factory.begin() as db:
        db.execute(
            update(DependencyManifest)
            .where(DependencyManifest.id == UUID(first.result["manifest_id"]))
            .values(snapshot_json={"forged": True})
        )
    assert (
        trace(client, run_id, actor)["evidence"][0]["art_investigation"]["result"] == first.result
    )


def test_successful_research_preserves_selected_locks_formal_history_and_fact_boundaries(
    client, episode, settings
):
    asset, version = selected(client, episode["project_id"])
    locked = lock(client, asset, version).json()
    assert locked["status"] == "LOCKED"
    saved, _ = save(client, episode, "PROP")
    formal = decide(client, submit(client, saved).json()).json()["outputs"][0]
    run_id, actor, _ = prepare_art(client, episode, settings)

    def state():
        with client.app.state.session_factory() as db:
            return digest(
                {
                    "directories": [
                        asset_content(row)
                        for row in db.scalars(select(VisualAsset).order_by(VisualAsset.id))
                    ],
                    "versions": [
                        {
                            "id": str(row.id),
                            "status": row.status,
                            "revision": row.revision,
                            "stale": row.stale,
                            "checksum": row.checksum,
                            "metadata": row.metadata_json,
                        }
                        for row in db.scalars(select(AssetVersion).order_by(AssetVersion.id))
                    ],
                    "locks": [
                        {"asset_id": str(row.asset_id), "version_id": str(row.version_id)}
                        for row in db.scalars(
                            select(AssetSelectionLock).order_by(AssetSelectionLock.asset_id)
                        )
                    ],
                    "formal": [
                        {"id": str(row.id), "hash": row.content_sha256}
                        for row in db.scalars(
                            select(ArtApprovedVersion).order_by(ArtApprovedVersion.id)
                        )
                    ],
                    "proposals": [
                        str(row.id)
                        for row in db.scalars(
                            select(ArtProposalVersion).order_by(ArtProposalVersion.id)
                        )
                    ],
                }
            )

    before = state()
    dirs = query(
        client,
        settings,
        run_id,
        {"path": "directories", "asset_ids": [asset["id"], formal["asset_id"]]},
    )
    assert contents(dirs)[0]["baseline"]["lock_version"] == locked["lock_version"]
    refs = query(client, settings, run_id, {"path": "references", "version_ids": [version["id"]]})
    assert contents(refs)[0]["asset"]["locked"]
    assert len(contents(query(client, settings, run_id, {"path": "visual"}))) == 1
    assert state() == before
    with client.app.state.session_factory() as db:
        memory = read_agent_memory(db, db.get(WorkflowRun, run_id), "storyboard", actor)["memory"]
        facts = {row["source_id"] for row in memory["domain_facts"]}
        assert version["id"] in facts
        assert formal["asset_id"] not in facts
        assert db.get(WorkflowRun, run_id).pending_review is None


@pytest.mark.parametrize("duplicate", [False, True])
def test_postgres_concurrent_art_queries_keep_one_ordered_chain_and_idempotency(
    client, episode, settings, duplicate
):
    require_postgres(settings)
    run_id, actor, _ = prepare_art(client, episode, settings)
    barrier = Barrier(2)

    def execute(index):
        barrier.wait(timeout=20)
        return query(
            client,
            settings,
            run_id,
            {"path": "visual"},
            key="same-art" if duplicate else f"art-{index}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        feedback = list(pool.map(execute, range(2)))
    assert all(item.status == "SUCCEEDED" for item in feedback), [
        f.model_dump_json() for f in feedback
    ]
    assert len(trace(client, run_id, actor)["evidence"]) == (1 if duplicate else 2)
    if duplicate:
        assert feedback[0] == feedback[1]
    else:
        assert {item.result["evidence_round"] for item in feedback} == {1, 2}
