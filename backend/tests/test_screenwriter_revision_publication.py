"""Final candidate publication, registered versions and immutable preexisting history."""

import copy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_screenwriter_creation import approve, progress, run_once, start
from test_screenwriter_revision import install_revision
from test_script_rewrite import body as rewrite_body
from test_script_rewrite import start as start_rewrite

from arclight.persistence.models import Review, Script, Storyboard


def test_crash_after_saved_repair_rebuilds_from_receipt_before_publishing_once(
    client, episode, settings, model_service, monkeypatch
):
    from arclight.application import screenwriter_revision as revision

    sent, errors, ordinary_errors = install_revision(model_service, monkeypatch, settings)
    original = revision._round

    class WorkerDied(BaseException):
        pass

    def die(*args, **kwargs):
        if args[5] == 1:
            raise WorkerDied()
        return original(*args, **kwargs)

    monkeypatch.setattr(revision, "_round", die)
    run_id = start(client, episode, revision_rounds=1)
    with pytest.raises(WorkerDied):
        run_once(client, settings, run_id)
    assert len(sent) == 6
    saved = copy.deepcopy(progress(client, run_id))
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run_id)) == 0
        )
    monkeypatch.setattr(revision, "_round", original)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW" and len(sent) == 7, (run, errors, ordinary_errors)
    assert progress(client, run_id)[:-1] == saved
    assert run_once(client, settings, run_id)["status"] == "WAITING_REVIEW" and len(sent) == 7
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run_id)) == 1
        )


def test_registered_revision_change_blocks_continuation_with_approved_history_preserved(
    client, episode, settings, model_service, monkeypatch
):
    from arclight.application import production_versions as versions

    sent, errors, ordinary_errors = install_revision(model_service, monkeypatch, settings)
    run_id = start(client, episode, revision_rounds=1)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors, ordinary_errors)
    candidate_id = UUID(run["pending_review"]["version_id"])
    approve(client, run)
    monkeypatch.setattr(versions, "REVISION_PROMPT", versions.REVISION_PROMPT + " changed")
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "PRODUCTION_VERSION_INCOMPATIBLE"
    assert len(sent) == 7
    with client.app.state.session_factory() as db:
        assert db.get(Script, candidate_id).status == "APPROVED"
        assert (
            db.scalar(select(func.count()).select_from(Review).where(Review.run_id == run_id)) == 1
        )


def test_professional_rewrite_preserves_approved_source_old_reviews_and_existing_storyboard(
    client, episode, settings, model_service, monkeypatch
):
    settings.provider = "mock"
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        json={"story": "纸船归途"},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 202
    old_id = UUID(response.json()["run_id"])
    run = run_once(client, settings, old_id)
    for _ in range(2):
        assert run["status"] == "WAITING_REVIEW"
        approve(client, run)
        run = run_once(client, settings, old_id)
    assert run["status"] == "SUCCEEDED"
    base = client.get(f"/api/v1/episodes/{episode['id']}/scripts").json()[0]
    with client.app.state.session_factory() as db:
        board = db.scalar(select(Storyboard).where(Storyboard.run_id == old_id))
        old_board_id, old_board = board.id, copy.deepcopy(board.content_json)
        old_reviews = [
            (row.id, row.version_id, row.decision, row.comment)
            for row in db.scalars(select(Review).where(Review.run_id == old_id).order_by(Review.id))
        ]
    settings.provider = "openai_compatible"
    sent, errors, ordinary_errors = install_revision(model_service, monkeypatch, settings)
    target = {"scope": "scene", "scene_id": base["content_json"]["scenes"][0]["scene_id"]}
    response = start_rewrite(client, episode, rewrite_body(base, target) | {"professional": True})
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors, ordinary_errors)
    count = len(sent)
    approve(client, run)
    assert run_once(client, settings, run_id)["status"] == "SUCCEEDED" and len(sent) == count
    with client.app.state.session_factory() as db:
        assert db.get(Script, UUID(base["id"])).content_json == base["content_json"]
        assert db.get(Storyboard, old_board_id).content_json == old_board
        assert [
            (row.id, row.version_id, row.decision, row.comment)
            for row in db.scalars(select(Review).where(Review.run_id == old_id).order_by(Review.id))
        ] == old_reviews
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 1
