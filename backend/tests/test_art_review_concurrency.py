"""Real PostgreSQL races use the existing project transaction gate."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_art_review_workflow import decision, existing_payload, save, submit

from arclight.application.art_proposal_store import save_proposal
from arclight.application.art_reviews import decide_art
from arclight.core.errors import DomainError
from arclight.domain.art_review import ArtDecision, ArtSave
from arclight.persistence.art_proposals import (
    ArtApprovedVersion,
    ArtProposalRecord,
    ArtProposalVersion,
)
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewResume


def parallel(factory, calls):
    barrier = Barrier(len(calls))

    def run(call):
        barrier.wait(timeout=10)
        try:
            with factory.begin() as db:
                return call(db)
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


@pytest.fixture(autouse=True)
def postgres_only(settings):
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("Real PostgreSQL transaction concurrency")


def test_postgres_duplicate_creation_mints_one_immutable_proposal_version(client, episode):
    from uuid import uuid4

    from test_art_proposals import proposal_payload

    body = ArtSave(
        creation_key=uuid4(), proposal=proposal_payload(project_id=episode["project_id"])
    )
    factory = client.app.state.session_factory

    def create(db):
        row, version, created = save_proposal(
            db, UUID(episode["project_id"]), "service-owner", body
        )
        return row.id, version.id, created

    results = parallel(factory, [create, create])
    assert results[0][:2] == results[1][:2] and sorted(result[2] for result in results) == [
        False,
        True,
    ]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ArtProposalRecord)) == 1
        assert db.scalar(select(func.count()).select_from(ArtProposalVersion)) == 1


def test_postgres_duplicate_approval_has_one_decision_receipt_and_output(client, episode):
    saved, _ = save(client, episode)
    waiting = submit(client, saved).json()
    body = ArtDecision.model_validate(decision(waiting))
    factory = client.app.state.session_factory

    def approve(db):
        return decide_art(
            db, UUID(saved["project_id"]), UUID(saved["id"]), "service-owner", body
        ).id

    results = parallel(factory, [approve, approve])
    assert results[0] == results[1] and not isinstance(results[0], str)
    with factory() as db:
        for model in (ArtApprovedVersion, HumanReviewDecision, HumanReviewResume):
            assert db.scalar(select(func.count()).select_from(model)) == 1


def test_postgres_competing_proposals_cannot_overwrite_same_asset_baseline(client, episode):
    payload, _ = existing_payload(client, episode)
    first, _ = save(client, episode, payload=payload)
    payload["options"][0]["designs"][0]["name"] = "另一个方案"
    second, _ = save(client, episode, payload=payload)
    waiting = [submit(client, row).json() for row in (first, second)]
    factory = client.app.state.session_factory
    calls = []
    for row in waiting:

        def approve(db, row=row):
            return decide_art(
                db,
                UUID(row["project_id"]),
                UUID(row["id"]),
                "service-owner",
                ArtDecision.model_validate(decision(row)),
            ).id

        calls.append(approve)
    results = parallel(factory, calls)
    assert len([row for row in results if not isinstance(row, str)]) == 1
    assert "ART_PROPOSAL_BASE_STALE" in results
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ArtApprovedVersion)) == 1
        assert db.scalar(select(func.count()).select_from(HumanReviewResume)) == 1
