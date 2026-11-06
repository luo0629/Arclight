"""ART-03 actual PostgreSQL transaction gates and stale cached-snapshot rejection."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_art_proposal_references import bound_payload
from test_art_protection import acknowledgement, bible_payload, compare
from test_visual_bible import edit

from arclight.api.assets import change_asset
from arclight.application.art_protection import compare_art_proposal, require_art_confirmation
from arclight.application.commands import require_project
from arclight.application.visual_bible import append_version
from arclight.core.errors import DomainError
from arclight.domain.art_proposals import ArtProposal
from arclight.domain.art_protection import ArtComparison
from arclight.persistence.models import Project, VisualAsset


@pytest.mark.parametrize("phase", ["compare", "confirm"])
def test_postgres_art_gate_is_held_until_the_callers_transaction_ends(
    client, episode, settings, phase
):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("Real PostgreSQL row lock test")
    payload, _, _ = bound_payload(client, episode)
    proposal = ArtProposal.model_validate(payload)
    report = ArtComparison.model_validate(compare(client, payload).json())
    factory = client.app.state.session_factory

    def competing_writer():
        try:
            with factory.begin() as other:
                other.execute(text("SET LOCAL lock_timeout = '200ms'"))
                require_project(other, proposal.project_id, "service-owner", write=True)
        except DBAPIError as error:
            return getattr(error.orig, "sqlstate", None)
        return "unexpectedly_acquired"

    with factory.begin() as db:
        if phase == "compare":
            compare_art_proposal(db, proposal, "service-owner")
        else:
            require_art_confirmation(db, proposal, "service-owner", acknowledgement(report))
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(competing_writer).result(timeout=10) == "55P03"
        assert not db.dirty and not db.new and not db.deleted
    with factory.begin() as db:
        assert require_project(db, proposal.project_id, "service-owner", write=True)


@pytest.mark.parametrize("target", ["asset", "bible"])
def test_postgres_two_confirmations_cannot_silently_overwrite_the_same_base(
    client, episode, settings, target
):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("Real PostgreSQL approval transaction race")
    if target == "asset":
        payload, asset, _ = bound_payload(client, episode)
        design = payload["options"][0]["designs"][0]
        design.update(metadata=None, profile=None, name="已确认的新版名称")
    else:
        payload = bible_payload(client, episode, lighting="暖灯")
    proposal = ArtProposal.model_validate(payload)
    report = ArtComparison.model_validate(compare(client, payload).json())
    proof = acknowledgement(report)
    factory = client.app.state.session_factory
    barrier = Barrier(2)

    def confirm_and_use_existing_writer(_):
        barrier.wait(timeout=10)
        try:
            with factory.begin() as db:
                require_art_confirmation(db, proposal, "service-owner", proof)
                if target == "asset":
                    row = db.get(VisualAsset, UUID(asset["asset_id"]))
                    change_asset(db, row, design["target"]["revision"], name=design["name"])
                else:
                    project = require_project(db, proposal.project_id, "service-owner", write=True)
                    append_version(
                        db, project, "service-owner", 0, uuid4(), patch={"lighting": "暖灯"}
                    )
            return "applied_in_caller_transaction"
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm_and_use_existing_writer, range(2)))
    assert sorted(results) == sorted(["applied_in_caller_transaction", "ART_PROPOSAL_BASE_STALE"])
    if target == "asset":
        with factory.begin() as db:
            row = db.get(VisualAsset, UUID(asset["asset_id"]))
            assert row.revision == design["target"]["revision"] + 1 and row.name == design["name"]
    else:
        response = client.get(f"/api/v1/projects/{episode['project_id']}/visual-bible")
        assert (
            response.json()["version_no"] == 1 and response.json()["fields"]["lighting"] == "暖灯"
        )


@pytest.mark.parametrize("change", ["asset", "archive", "bible"])
def test_cached_orm_objects_do_not_hide_new_baselines_or_project_archive(client, episode, change):
    payload, asset, _ = bound_payload(client, episode)
    if change == "bible":
        payload = bible_payload(client, episode, lighting="暖灯")
    factory = client.app.state.session_factory
    with factory() as stale:
        cached_project = stale.get(Project, UUID(episode["project_id"]))
        cached_asset = stale.get(VisualAsset, UUID(asset["asset_id"]))
        stale.commit()  # expire_on_commit=False deliberately retains the old ORM objects.
        if change == "bible":
            assert (
                edit(client, {"id": episode["project_id"]}, lighting="已保存的新值").status_code
                == 201
            )
        else:
            with factory.begin() as writer:
                if change == "asset":
                    row = writer.get(VisualAsset, UUID(asset["asset_id"]))
                    row.name = "另一个事务的名称"
                    row.revision += 1
                else:
                    writer.get(Project, UUID(episode["project_id"])).archived = True
        assert cached_project is not None and cached_asset is not None
        with pytest.raises(DomainError) as error:
            compare_art_proposal(stale, ArtProposal.model_validate(payload), "service-owner")
        assert error.value.code == (
            "PROJECT_ARCHIVED" if change == "archive" else "ART_PROPOSAL_BASE_STALE"
        )
        stale.rollback()
