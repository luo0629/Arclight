"""UI-03: exact shot state, shared command gates and successful-work preservation."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_collaboration import create_episode, create_project, register
from test_concurrency import race, require_postgres
from test_continuity_reviews import review_case, saved
from test_continuity_selection import admission_fixture, approve
from test_continuity_workflow import make_job
from test_generation_attempts import fail, start
from test_generation_delivery import deliver
from test_generation_jobs import base
from test_storyboard_table import board_fixture

from arclight.application.generation_state import transition
from arclight.domain.generation import GenerationStatus
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    Episode,
    GenerationJob,
    VisualAsset,
)
from arclight.providers.media_submission import MockMediaSubmissionProvider

__all__ = ["review_case"]


def production(client, episode, board):
    path = f"/api/v1/episodes/{episode['id']}/storyboards/{board['id']}/production"
    response = client.get(path)
    assert response.status_code == 200, response.text
    return path, response.json()


def create_shot(client, project_id, shot, *, key=None):
    return client.post(
        base(project_id),
        json=shot["request"],
        headers={"Idempotency-Key": key or str(uuid4())},
    )


def test_production_reads_precise_paged_source_and_never_creates_work(client, episode):
    _, board, _ = board_fixture(client, episode)
    with client.app.state.session_factory() as db:
        original_manifests = set(db.scalars(select(DependencyManifest.id)))
    path, page = production(client, episode, board)
    assert page["writable"] and page["owner"]
    assert page["total"] == len(board["content_json"]["shots"])
    assert [row["shot_no"] for row in page["shots"]] == list(range(1, page["total"] + 1))
    for shot in page["shots"]:
        request = shot["request"]
        assert shot["job"] is None and shot["candidates"] == [] and shot["blocker"] is None
        assert request["review_target"] == {"storyboard_id": board["id"], "shot_id": shot["id"]}
        assert request["inputs"] == [
            {
                "source_asset_version_id": board["id"],
                "dependency_type": "CONTENT_DEPENDENCY",
                "selectors": ["shot:" + shot["id"]],
            }
        ]
        assert (
            request["scheduling_mode"] == "BATCH"
            and shot["description"] in request["spec"]["prompt"]
        )
    first = client.get(path, params={"limit": 1}).json()
    second = client.get(path, params={"limit": 1, "offset": 1}).json()
    assert first["next_offset"] == 1 and second["shots"][0] == page["shots"][1]
    assert client.get(path, params={"offset": 1000}).json()["shots"] == []
    assert client.get(path, params={"limit": 101}).status_code == 422
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0
        assert set(db.scalars(select(DependencyManifest.id))) == original_manifests


def test_independent_shots_partial_failure_and_same_key_replay_keep_success(client, episode):
    _, board, _ = board_fixture(client, episode)
    _, page = production(client, episode, board)
    first, second = page["shots"][:2]
    key = str(uuid4())
    accepted = create_shot(client, episode["project_id"], first, key=key)
    assert accepted.status_code == 201, accepted.text
    original = accepted.json()
    invalid = deepcopy(second)
    invalid["request"]["review_target"]["shot_id"] = str(uuid4())
    assert create_shot(client, episode["project_id"], invalid).status_code == 409
    busy = create_shot(client, episode["project_id"], first)
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "GENERATION_SHOT_BUSY"
    replay = create_shot(client, episode["project_id"], first, key=key)
    assert replay.status_code == 201 and replay.json()["id"] == original["id"]
    changed = deepcopy(first)
    changed["request"]["spec"]["prompt"] += "修改"
    assert create_shot(client, episode["project_id"], changed, key=key).status_code == 409
    assert create_shot(client, episode["project_id"], second).status_code == 201
    _, latest = production(client, episode, board)
    assert latest["shots"][0]["job"]["id"] == original["id"]
    assert latest["shots"][0]["job"]["maximum_cost"] == "0.000000"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 2


@pytest.mark.parametrize("gate", ["episode", "source_stale", "source_archived"])
def test_captured_generation_rechecks_source_and_archive_before_queuing(client, episode, gate):
    _, board, _ = board_fixture(client, episode)
    _, page = production(client, episode, board)
    with client.app.state.session_factory.begin() as db:
        if gate == "episode":
            db.get(Episode, UUID(episode["id"])).status = "ARCHIVED"
        else:
            version = db.get(AssetVersion, UUID(board["id"]))
            if gate == "source_stale":
                version.stale = True
            else:
                db.get(VisualAsset, version.asset_id).status = "ARCHIVED"
    response = create_shot(client, episode["project_id"], page["shots"][0])
    assert response.status_code == 409, response.text
    _, current = production(client, episode, board)
    assert current["shots"][0]["request"] is None and current["shots"][0]["blocker"]
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 0


def test_only_failed_retry_retains_success_and_original_exact_sources(client, episode):
    _, board, _ = board_fixture(client, episode)
    _, page = production(client, episode, board)
    jobs = [create_shot(client, episode["project_id"], shot).json() for shot in page["shots"][:2]]
    assert deliver(client, jobs[0], MockMediaSubmissionProvider()) == "SUCCEEDED"
    attempt = start(client, jobs[1])
    fail(client, jobs[1], attempt, error="PROVIDER_FAILED")
    _, current = production(client, episode, board)
    success, failed = current["shots"][:2]
    assert success["job"]["status"] == "SUCCEEDED" and failed["job"]["status"] == "FAILED"
    success_snapshot = client.get(base(episode["project_id"]) + "/" + jobs[0]["id"]).json()
    retry_path = base(episode["project_id"]) + "/" + jobs[1]["id"] + "/retry"
    result = client.post(retry_path, json={"expected_revision": failed["job"]["revision"]})
    assert result.status_code == 200 and result.json()["status"] == "RETRY_WAIT", result.text
    assert result.json()["input_manifest_id"] == jobs[1]["input_manifest_id"]
    assert (
        client.post(retry_path, json={"expected_revision": failed["job"]["revision"]}).status_code
        == 409
    )
    assert client.get(base(episode["project_id"]) + "/" + jobs[0]["id"]).json() == success_snapshot
    assert len(client.get(base(episode["project_id"])).json()) == 2


def test_unknown_outcome_blocks_new_shot_work_and_blind_retry(client, episode):
    _, board, _ = board_fixture(client, episode)
    _, page = production(client, episode, board)
    job = create_shot(client, episode["project_id"], page["shots"][0]).json()
    start(client, job)
    with client.app.state.session_factory.begin() as db:
        row = db.get(GenerationJob, UUID(job["id"]))
        transition(db, row.id, GenerationStatus.RESULT_UNKNOWN, row.revision)
    current = client.get(base(episode["project_id"]) + "/" + job["id"]).json()
    retry = client.post(
        base(episode["project_id"]) + "/" + job["id"] + "/retry",
        json={"expected_revision": current["revision"]},
    )
    assert retry.status_code == 409 and retry.json()["error"]["code"] == "GENERATION_NOT_RETRYABLE"
    assert (
        create_shot(client, episode["project_id"], page["shots"][0]).json()["error"]["code"]
        == "GENERATION_SHOT_BUSY"
    )


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_read_and_each_command_obey_current_membership_and_revocation(client, role):
    owner = register(client, "production-owner@example.com")
    project = create_project(client)
    episode = create_episode(client, project)
    _, board, _ = board_fixture(client, episode)
    path, page = production(client, episode, board)
    member = register(client, "production-member@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.post(
            f"/api/v1/projects/{project['id']}/members",
            json={"email": member["user"]["email"], "role": role},
        ).status_code
        == 201
    )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    viewed = client.get(path)
    assert viewed.status_code == 200 and not viewed.json()["owner"]
    assert viewed.json()["writable"] == (role == "editor")
    result = create_shot(client, project["id"], page["shots"][0])
    assert result.status_code == (201 if role == "editor" else 403)
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert (
        client.delete(
            f"/api/v1/projects/{project['id']}/members/{member['user']['id']}"
        ).status_code
        == 204
    )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    assert client.get(path).status_code == 404
    assert create_shot(client, project["id"], page["shots"][0]).status_code == 404


def test_read_rejects_cross_episode_board_and_keeps_unapproved_history_readable(client, episode):
    prefix, board, body = board_fixture(client, episode)
    second = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "另一集", "target_duration_ms": 30000},
    ).json()
    assert (
        client.get(
            f"/api/v1/episodes/{second['id']}/storyboards/{board['id']}/production"
        ).status_code
        == 404
    )
    body["content"]["shots"][0]["notes"] = "待审新版本"
    pending = client.post(prefix + "/storyboards", json=body).json()
    _, page = production(client, episode, pending)
    assert page["shots"][0]["request"] is None
    assert page["shots"][0]["blocker"]["code"] == "REVIEW_TARGET_INVALID"


def test_candidates_require_real_review_and_report_original_precise_versions(
    client, episode, review_case
):
    job, _ = make_job(client, episode, review_case, count=2)
    board = {"id": review_case["target"]["storyboard_id"]}
    _, page = production(client, episode, board)
    row = next(shot for shot in page["shots"] if shot["id"] == review_case["target"]["shot_id"])
    assert row["job"]["id"] == job["id"] and len(row["candidates"]) == 2
    for candidate in row["candidates"]:
        assert not candidate["can_submit"] and not candidate["can_approve"]
        assert candidate["blocker"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
        path = (
            f"/api/v1/projects/{episode['project_id']}/assets/{candidate['asset_id']}"
            f"/versions/{candidate['version_id']}/state"
        )
        denied = client.patch(
            path, json={"status": "READY_FOR_REVIEW", "expected_revision": candidate["revision"]}
        )
        assert (
            denied.status_code == 409
            and denied.json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
        )
        assert client.get("/api/v1" + candidate["image_path"]).status_code == 200


@pytest.mark.parametrize("retry", [False, True])
def test_selected_locked_shot_cannot_be_bypassed_with_an_unregistered_target(
    client, episode, review_case, retry
):
    record, _ = saved(client, review_case)
    admission_fixture(client, record)
    approve(client, review_case)
    prefix = f"/api/v1/episodes/{episode['id']}"
    original = client.get(prefix + "/storyboards").json()[0]
    content = deepcopy(original["content_json"])
    content["shots"][0]["selected_visual_asset"] = {
        name: review_case["target"][name] for name in ("asset_id", "asset_version_id")
    }
    response = client.post(
        prefix + "/storyboards",
        json={
            "expected_version_no": original["version_no"],
            "base_version_id": original["id"],
            "script_version_id": original["script_version_id"],
            "content": content,
        },
    )
    assert response.status_code == 201, response.text
    board = response.json()
    assert (
        client.post(
            prefix + f"/storyboards/{board['id']}/review", json={"decision": "APPROVE"}
        ).status_code
        == 200
    )
    _, page = production(client, episode, board)
    captured = page["shots"][0]
    assert captured["job"] is None
    assert [candidate["version_id"] for candidate in captured["candidates"]] == [
        review_case["target"]["asset_version_id"]
    ]
    captured["request"]["target_asset_id"] = None
    captured["request"]["expected_asset"] = None
    job = None
    if retry:
        job = create_shot(client, episode["project_id"], captured).json()
        fail(client, job, start(client, job))
    with client.app.state.session_factory.begin() as db:
        db.get(VisualAsset, UUID(review_case["target"]["asset_id"])).status = "LOCKED"
    if retry:
        current = client.get(base(episode["project_id"]) + "/" + job["id"]).json()
        result = client.post(
            base(episode["project_id"]) + "/" + job["id"] + "/retry",
            json={"expected_revision": current["revision"]},
        )
    else:
        result = create_shot(client, episode["project_id"], captured)
    assert result.status_code == 409 and result.json()["error"]["code"] == "ASSET_READ_ONLY", (
        result.text
    )
    _, current = production(client, episode, board)
    assert current["shots"][0]["request"] is None


def test_postgres_competing_unregistered_shot_targets_have_one_winner(client, episode, settings):
    require_postgres(settings)
    _, board, _ = board_fixture(client, episode)
    _, page = production(client, episode, board)
    # Distinct keys prove shot exclusion, rather than merely replaying the same command.
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from fastapi.testclient import TestClient

    from arclight.main import create_app

    barrier = Barrier(2)

    def submit(_):
        with TestClient(create_app(settings)) as connection:
            barrier.wait(timeout=15)
            return connection.post(
                base(episode["project_id"]),
                json=page["shots"][0]["request"],
                headers={"Authorization": "Bearer test-token", "Idempotency-Key": str(uuid4())},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, range(2)))
    assert sorted(row.status_code for row in responses) == [201, 409]
    assert (
        next(row for row in responses if row.status_code == 409).json()["error"]["code"]
        == "GENERATION_SHOT_BUSY"
    )
    assert len(client.get(base(episode["project_id"])).json()) == 1


def test_postgres_same_shot_same_key_shares_one_job_and_manifest(client, episode, settings):
    require_postgres(settings)
    _, board, _ = board_fixture(client, episode)
    _, page = production(client, episode, board)
    responses = race(
        client.app,
        base(episode["project_id"]),
        page["shots"][0]["request"],
        {"Authorization": "Bearer test-token", "Idempotency-Key": str(uuid4())},
    )
    assert all(row.status_code == 201 for row in responses)
    assert responses[0].json()["id"] == responses[1].json()["id"]
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == 1
        assert db.scalar(select(func.count()).select_from(DependencyManifest)) == 2


def test_generation_reuses_exact_scene_prop_references_and_rechecks_stale_inputs(client, episode):
    from test_asset_versions import save, state
    from test_assets import create

    prefix, original, body = board_fixture(client, episode)
    references = []
    for kind in ("SCENE", "PROP"):
        asset = create(client, episode["project_id"], kind=kind, name=kind)
        version = save(client, asset)
        ready = state(client, asset, version, "READY_FOR_REVIEW")
        assert ready.status_code == 200, ready.text
        approved = state(client, asset, ready.json(), "APPROVED")
        assert approved.status_code == 200, approved.text
        references.append({"asset_id": asset["id"], "asset_version_id": version["id"]})
    body["content"] = deepcopy(original["content_json"])
    body["content"]["shots"][0].update(
        scene_asset=references[0], location_asset=references[0], props=[references[1]]
    )
    result = client.post(prefix + "/storyboards", json=body)
    assert result.status_code == 201, result.text
    board = result.json()
    approved = client.post(
        prefix + f"/storyboards/{board['id']}/review", json={"decision": "APPROVE"}
    )
    assert approved.status_code == 200, approved.text
    _, page = production(client, episode, board)
    request = page["shots"][0]["request"]
    assert [item["source_asset_version_id"] for item in request["inputs"]] == [
        board["id"],
        references[0]["asset_version_id"],
        references[1]["asset_version_id"],
    ]
    job = create_shot(client, episode["project_id"], page["shots"][0])
    assert job.status_code == 201, job.text
    with client.app.state.session_factory.begin() as db:
        manifest = db.get(DependencyManifest, UUID(job.json()["input_manifest_id"]))
        assert [item["source_asset_version_id"] for item in manifest.snapshot_json["inputs"]] == [
            item["source_asset_version_id"] for item in request["inputs"]
        ]
        db.get(AssetVersion, UUID(references[1]["asset_version_id"])).stale = True
    _, current = production(client, episode, board)
    assert current["shots"][0]["request"] is None
    assert current["shots"][0]["blocker"]["code"] == "ASSET_NOT_USABLE"


def test_bound_candidates_share_original_submit_owner_review_and_cas_gates(
    client, episode, review_case
):
    from arclight.domain.versions import digest

    job, _ = make_job(client, episode, review_case, count=2)
    candidates = client.get(base(episode["project_id"]) + f"/{job['id']}/candidates").json()
    candidate = candidates[0]
    value = deepcopy(review_case)
    value["result_id"] = str(uuid4())
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(candidate["asset_version_id"]))
        value["target"].update(
            candidate_id=candidate["id"],
            asset_id=str(version.asset_id),
            asset_version_id=str(version.id),
            input_manifest_id=candidate["input_manifest_id"],
            media_check_sha256=digest(candidate["media_check"]),
        )
        value["references"][0].update(resource_id=candidate["id"], content_sha256=version.checksum)
    record, _ = saved(client, value)
    admission_fixture(client, record)
    board = {"id": value["target"]["storyboard_id"]}
    _, page = production(client, episode, board)
    shot = next(row for row in page["shots"] if row["id"] == value["target"]["shot_id"])
    exact = next(row for row in shot["candidates"] if row["id"] == candidate["id"])
    assert exact["can_submit"] and not exact["can_approve"]
    path = (
        f"/api/v1/projects/{episode['project_id']}/assets/{exact['asset_id']}"
        f"/versions/{exact['version_id']}/state"
    )
    ready = client.patch(
        path, json={"status": "READY_FOR_REVIEW", "expected_revision": exact["revision"]}
    )
    assert ready.status_code == 200, ready.text
    approved = client.patch(
        path,
        json={
            "status": "APPROVED",
            "expected_revision": ready.json()["revision"],
            "comment": "逐镜头人工批准",
        },
    )
    assert approved.status_code == 200, approved.text
    assert (
        client.patch(
            path,
            json={
                "status": "APPROVED",
                "expected_revision": ready.json()["revision"],
                "comment": "不得重复批准",
            },
        ).status_code
        == 409
    )
    _, current = production(client, episode, board)
    shot = next(row for row in current["shots"] if row["id"] == value["target"]["shot_id"])
    assert (
        next(row for row in shot["candidates"] if row["id"] == candidate["id"])["status"]
        == "APPROVED"
    )
    other = next(row for row in shot["candidates"] if row["id"] != candidate["id"])
    assert other["status"] == "DRAFT" and not other["can_submit"]
