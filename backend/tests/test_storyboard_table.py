"""UI-02: immutable batch edits, sequence-only impact and reviewed image reuse."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_collaboration import register
from test_concurrency import race, require_postgres
from test_continuity_reviews import review_case, saved
from test_continuity_selection import admission_fixture, approve
from test_dependency_invalidation import produce
from test_timelines import created_timeline, timeline_case  # noqa: F401

from arclight.agents.contracts import ScriptContent
from arclight.persistence.models import AssetVersion, GenerationJob, Shot, Storyboard, VisualAsset
from arclight.providers.text import MockTextProvider

__all__ = ["review_case", "timeline_case"]


def board_fixture(client, episode):
    prefix = f"/api/v1/episodes/{episode['id']}"
    content = MockTextProvider().script("分镜表格的验收故事", {}).model_dump(mode="json")
    script = client.post(prefix + "/scripts", json={"expected_version_no": 0, "content": content})
    assert script.status_code == 201, script.text
    script = script.json()
    approved = client.post(prefix + f"/scripts/{script['id']}/review", json={"decision": "APPROVE"})
    assert approved.status_code == 200, approved.text
    board = MockTextProvider().storyboard(ScriptContent.model_validate(content), 30000)
    body = {
        "expected_version_no": 0,
        "script_version_id": script["id"],
        "content": board.model_dump(mode="json"),
    }
    saved = client.post(prefix + "/storyboards", json=body)
    assert saved.status_code == 201, saved.text
    saved = saved.json()
    approved = client.post(
        prefix + f"/storyboards/{saved['id']}/review", json={"decision": "APPROVE"}
    )
    assert approved.status_code == 200, approved.text
    return prefix, saved, {**body, "expected_version_no": 1, "base_version_id": saved["id"]}


def test_batch_edit_saves_one_version_and_keeps_unselected_rows_and_original(client, episode):
    prefix, original, body = board_fixture(client, episode)
    before = deepcopy(original["content_json"])
    body["content"] = deepcopy(before)
    for index in (0, 2):
        body["content"]["shots"][index].update(shot_size="close_up", notes="批量核对")
    result = client.post(prefix + "/storyboards", json=body)
    assert result.status_code == 201, result.text
    saved = result.json()
    assert saved["status"] == "READY_FOR_REVIEW" and saved["version_no"] == 2
    assert saved["script_version_id"] == original["script_version_id"]
    for index, shot in enumerate(saved["content_json"]["shots"]):
        expected = (
            {**before["shots"][index], "shot_size": "close_up", "notes": "批量核对"}
            if index in (0, 2)
            else before["shots"][index]
        )
        assert shot == expected
    versions = client.get(prefix + "/storyboards").json()
    assert len(versions) == 2 and versions[1]["content_json"] == before
    assert client.post(prefix + "/storyboards", json=body).status_code == 409
    assert len(client.get(prefix + "/storyboards").json()) == 2


@pytest.mark.parametrize("invalid", ["duration", "description", "scene", "number", "reference"])
def test_one_invalid_batch_row_rejects_every_row_atomically(client, episode, invalid):
    prefix, original, body = board_fixture(client, episode)
    body["content"] = deepcopy(original["content_json"])
    body["content"]["shots"][0]["notes"] = "此修改不可部分落库"
    last = body["content"]["shots"][-1]
    if invalid == "duration":
        last["duration_ms"] = 499
    elif invalid == "description":
        last["visual_description"] = ""
    elif invalid == "scene":
        last["scene_no"] = 999
    elif invalid == "number":
        last["shot_no"] = 1
    else:
        last["props"] = [{"asset_id": str(uuid4()), "asset_version_id": str(uuid4())}]
    response = client.post(prefix + "/storyboards", json=body)
    assert response.status_code == 422, response.text
    versions = client.get(prefix + "/storyboards").json()
    assert len(versions) == 1 and versions[0]["content_json"] == original["content_json"]
    shots = client.get(prefix + "/shots", params={"storyboard_id": original["id"]}).json()
    assert len(shots) == len(original["content_json"]["shots"])


def test_reorder_approval_invalidates_only_sequence_outputs_and_preserves_history(client, episode):
    prefix, original, body = board_fixture(client, episode)
    pid = UUID(episode["project_id"])
    with client.app.state.session_factory.begin() as db:
        source = db.get(AssetVersion, UUID(original["id"]))
        source_pair = db.get(VisualAsset, source.asset_id), source
        image = produce(db, pid, "SHOT_IMAGE", source_pair)
        timeline = produce(db, pid, "TIMELINE", source_pair, "SEQUENCE_DEPENDENCY")
        render = produce(db, pid, "RENDER", timeline, "RENDER_DEPENDENCY")
        image_id, timeline_id, render_id = image[1].id, timeline[1].id, render[1].id
    body["content"] = deepcopy(original["content_json"])
    body["content"]["shots"].reverse()
    for index, shot in enumerate(body["content"]["shots"]):
        shot["shot_no"] = index + 1
    response = client.post(prefix + "/storyboards", json=body)
    assert response.status_code == 201, response.text
    new = response.json()
    with client.app.state.session_factory() as db:
        assert not any(
            db.get(AssetVersion, item).stale for item in (image_id, timeline_id, render_id)
        )
    url = prefix + f"/storyboards/{new['id']}"
    impact = client.get(url + "/dependency-impact")
    assert impact.status_code == 200, impact.text
    impact = impact.json()
    assert {item["asset_version_id"] for item in impact["affected"]} == {
        str(timeline_id),
        str(render_id),
    }
    approved = client.post(
        url + "/review", json={"decision": "APPROVE", "impact_digest": impact["digest"]}
    )
    assert approved.status_code == 200, approved.text
    with client.app.state.session_factory() as db:
        assert db.get(AssetVersion, timeline_id).stale and db.get(AssetVersion, render_id).stale
        assert not db.get(AssetVersion, image_id).stale
        assert db.get(Storyboard, UUID(original["id"])).content_json == original["content_json"]
    nodes = client.get(prefix + "/canvas").json()["nodes"]
    assert len([node for node in nodes if node["kind"] == "shot"]) == 2 * len(
        body["content"]["shots"]
    )


def test_reordered_reviewed_images_create_new_timeline_without_regeneration(
    client, episode, timeline_case
):
    request, reviews = timeline_case
    prefix = f"/api/v1/episodes/{episode['id']}"
    original = next(
        row
        for row in client.get(prefix + "/storyboards").json()
        if row["id"] == request["storyboard_id"]
    )
    content = deepcopy(original["content_json"])
    for shot, review in zip(content["shots"], reviews, strict=True):
        shot["selected_visual_asset"] = {
            key: review["target"][key] for key in ("asset_id", "asset_version_id")
        }
        shot["quality_review"] = {
            "review_id": review["result_id"],
            "asset_version_id": review["target"]["asset_version_id"],
        }
    annotated = client.post(
        prefix + "/storyboards",
        json={
            "expected_version_no": original["version_no"],
            "base_version_id": original["id"],
            "script_version_id": original["script_version_id"],
            "content": content,
        },
    )
    assert annotated.status_code == 201, annotated.text
    annotated = annotated.json()
    approved = client.post(
        prefix + f"/storyboards/{annotated['id']}/review", json={"decision": "APPROVE"}
    )
    assert approved.status_code == 200, approved.text
    old_shots = client.get(prefix + "/shots", params={"storyboard_id": annotated["id"]}).json()
    old = created_timeline(
        client,
        episode,
        {
            "creation_key": str(uuid4()),
            "storyboard_id": annotated["id"],
            "selected_shots": [
                {
                    "shot_id": shot["id"],
                    "asset_version_id": shot["content_json"]["selected_visual_asset"][
                        "asset_version_id"
                    ],
                }
                for shot in old_shots
            ],
        },
    )
    with client.app.state.session_factory() as db:
        job_count = db.scalar(select(func.count()).select_from(GenerationJob))
    ordered = deepcopy(annotated["content_json"])
    ordered["shots"].reverse()
    for index, shot in enumerate(ordered["shots"]):
        shot["shot_no"] = index + 1
    result = client.post(
        prefix + "/storyboards",
        json={
            "expected_version_no": annotated["version_no"],
            "base_version_id": annotated["id"],
            "script_version_id": annotated["script_version_id"],
            "content": ordered,
        },
    )
    assert result.status_code == 201, result.text
    result = result.json()
    url = prefix + f"/storyboards/{result['id']}"
    impact = client.get(url + "/dependency-impact").json()
    assert {item["asset_version_id"] for item in impact["affected"]} == {old["id"]}
    approved = client.post(
        url + "/review", json={"decision": "APPROVE", "impact_digest": impact["digest"]}
    )
    assert approved.status_code == 200, approved.text
    new_shots = client.get(prefix + "/shots", params={"storyboard_id": result["id"]}).json()
    fresh = created_timeline(
        client,
        episode,
        {
            "creation_key": str(uuid4()),
            "storyboard_id": result["id"],
            "selected_shots": [
                {
                    "shot_id": shot["id"],
                    "asset_version_id": shot["content_json"]["selected_visual_asset"][
                        "asset_version_id"
                    ],
                }
                for shot in new_shots
            ],
        },
    )
    clips = fresh["snapshot"]["tracks"][0]["clips"]
    assert [clip["source_asset_version_id"] for clip in clips] == [
        review["target"]["asset_version_id"] for review in reversed(reviews)
    ]
    assert [clip["review_shot_id"] for clip in clips] == [
        review["target"]["shot_id"] for review in reversed(reviews)
    ]
    assert [clip["review_id"] for clip in clips] == [
        review["result_id"] for review in reversed(reviews)
    ]
    assert [clip["shot_id"] for clip in clips] == [shot["id"] for shot in new_shots]
    assert [clip["start_ms"] for clip in clips] == [0, 3000, 6000, 9000, 12000]
    historic = client.get(f"/api/v1/timelines/{old['id']}").json()
    assert historic["stale"] and historic["snapshot"] == old["snapshot"]
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(GenerationJob)) == job_count
        assert all(
            not db.get(AssetVersion, UUID(review["target"]["asset_version_id"])).stale
            for review in reviews
        )


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "WAITING_REVIEW"])
def test_batch_save_rechecks_active_workflow_and_writes_no_partial_shots(client, episode, status):
    from arclight.persistence.models import WorkflowRun

    prefix, original, body = board_fixture(client, episode)
    with client.app.state.session_factory.begin() as db:
        db.add(
            WorkflowRun(
                episode_id=UUID(episode["id"]),
                status=status,
                input_json={},
                trace_id=uuid4().hex,
                idempotency_key=str(uuid4()),
                request_fingerprint="a" * 64,
            )
        )
    response = client.post(prefix + "/storyboards", json=body)
    assert response.status_code == 409 and response.json()["error"]["code"] == "ACTIVE_WORKFLOW"
    assert len(client.get(prefix + "/storyboards").json()) == 1
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Shot)) == len(
            original["content_json"]["shots"]
        )


def test_postgres_concurrent_batch_saves_have_one_winner_and_exact_history(client, episode):
    require_postgres(client.app.state.settings)
    prefix, original, body = board_fixture(client, episode)
    body["content"]["shots"].reverse()
    for index, shot in enumerate(body["content"]["shots"]):
        shot["shot_no"] = index + 1
    responses = race(client.app, prefix + "/storyboards", body, headers=dict(client.headers))
    assert sorted(response.status_code for response in responses) == [201, 409]
    versions = client.get(prefix + "/storyboards").json()
    assert len(versions) == 2 and versions[1]["content_json"] == original["content_json"]


@pytest.mark.parametrize(
    "changed",
    [
        "visual_description",
        "composition",
        "camera_movement",
        "subtitle",
        "duration_ms",
        "quality_review",
    ],
)
def test_reorder_never_launders_changed_intent_or_wrong_review(
    client, episode, review_case, changed
):
    record, _ = saved(client, review_case)
    admission_fixture(client, record)
    approve(client, review_case)
    prefix = f"/api/v1/episodes/{episode['id']}"
    original = client.get(prefix + "/storyboards").json()[0]
    content = deepcopy(original["content_json"])
    shot = content["shots"][0]
    shot["selected_visual_asset"] = {
        key: review_case["target"][key] for key in ("asset_id", "asset_version_id")
    }
    shot["quality_review"] = {
        "review_id": review_case["result_id"],
        "asset_version_id": review_case["target"]["asset_version_id"],
    }
    if changed == "quality_review":
        shot["quality_review"]["review_id"] = str(uuid4())
    elif changed == "duration_ms":
        shot[changed] += 1
    elif changed == "camera_movement":
        shot[changed] = "push_in" if shot[changed] != "push_in" else "static"
    else:
        shot[changed] = "重排不能掩盖的新内容"
    content["shots"].reverse()
    for index, item in enumerate(content["shots"]):
        item["shot_no"] = index + 1
    response = client.post(
        prefix + "/storyboards",
        json={
            "expected_version_no": original["version_no"],
            "script_version_id": original["script_version_id"],
            "base_version_id": original["id"],
            "content": content,
        },
    )
    assert response.status_code == (422 if changed == "quality_review" else 409), response.text
    assert response.json()["error"]["code"] == (
        "INVALID_QUALITY_REFERENCE" if changed == "quality_review" else "REVIEW_TARGET_INVALID"
    )
    assert len(client.get(prefix + "/storyboards").json()) == 1


def test_batch_save_denies_viewer_and_revoked_member(client):
    from test_collaboration import create_episode, create_project

    owner = register(client, "table-owner@example.com")
    project = create_project(client)
    episode = create_episode(client, project)
    prefix, original, body = board_fixture(client, episode)
    viewer = register(client, "table-viewer@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    added = client.post(
        f"/api/v1/projects/{project['id']}/members",
        json={"email": viewer["user"]["email"], "role": "viewer"},
    )
    assert added.status_code == 201, added.text
    client.headers["Authorization"] = "Bearer " + viewer["access_token"]
    assert client.post(prefix + "/storyboards", json=body).status_code == 403
    assert client.get(prefix + "/storyboards").json()[0]["content_json"] == original["content_json"]
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    removed = client.delete(f"/api/v1/projects/{project['id']}/members/{viewer['user']['id']}")
    assert removed.status_code == 204, removed.text
    client.headers["Authorization"] = "Bearer " + viewer["access_token"]
    assert client.post(prefix + "/storyboards", json=body).status_code == 404
