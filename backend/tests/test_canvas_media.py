"""UI-01: exact canvas projections use real registrations and private media routes."""

import hashlib
from uuid import UUID, uuid4

import pytest
from render_job_helpers import job_case, render_case, review_case, timeline_case
from render_manifest_helpers import stored_file
from sqlalchemy import event, select
from test_audio_production import audio_source, output_audio, prepare, wave_bytes
from test_file_transfers import transfer_setup
from test_generation_video_inputs import video_case
from test_music_assets import create_music, import_music, music_source
from test_render_deliveries import delivery_case, history_url, viewer
from test_video_candidates import finish_video, formal_video_case, imported_rows, video_bytes

from arclight.application import generation_artifacts
from arclight.application.production_versions import bind
from arclight.application.short_drama_budget import MEDIA_SCOPE
from arclight.application.short_drama_production import set_progress
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    GenerationJob,
    StoredFile,
    Storyboard,
    WorkflowRun,
)

__all__ = [
    "audio_source",
    "delivery_case",
    "job_case",
    "render_case",
    "review_case",
    "timeline_case",
    "transfer_setup",
    "formal_video_case",
    "video_case",
]


def graph(client, episode):
    response = client.get(f"/api/v1/episodes/{episode['id']}/canvas")
    assert response.status_code == 200, response.text
    return response.json()


def exact(value, version_id):
    return next(node for node in value["nodes"] if node["id"] == f"asset-version:{version_id}")


def test_canvas_shot_image_uses_frozen_episode_when_job_episode_is_omitted(client, episode):
    from test_continuity_workflow import make_job
    from test_generation_candidates import candidates
    from test_shot_production import production
    from test_storyboard_table import board_fixture

    _, board, _ = board_fixture(client, episode)
    shot = production(client, episode, board)[1]["shots"][0]
    case = {"target": {"storyboard_id": board["id"], "shot_id": shot["id"]}}
    job, _ = make_job(client, episode | {"id": None}, case)
    candidate = candidates(client, episode["project_id"], job)[0]
    assert job["episode_id"] is None

    other_episode = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "Another episode"},
    )
    assert other_episode.status_code == 201, other_episode.text
    for view in (episode, other_episode.json()):
        node = exact(graph(client, view), candidate["asset_version_id"])
        assert node["preview_kind"] == "IMAGE"
        assert node["media_episode_id"] == episode["id"]


def test_canvas_automatic_scene_ignores_other_jobs_and_edits_of_the_same_storyboard(
    client, episode, render_case
):
    storyboard_id = render_case["snapshot"]["storyboard_id"]
    clip = render_case["snapshot"]["tracks"][0]["clips"][0]
    shot_id = clip["shot_id"]

    def projected_shot():
        return next(
            shot
            for node in graph(client, episode)["nodes"]
            if node["kind"] == "scene"
            for shot in node["shots"]
            if shot["id"] == f"shot:{shot_id}"
        )

    assert projected_shot()["media_node_id"] == f"asset-version:{clip['source_asset_version_id']}"

    with client.app.state.session_factory.begin() as db:
        board = db.get(Storyboard, UUID(storyboard_id))
        project_id = UUID(episode["project_id"])
        run = WorkflowRun(
            episode_id=board.episode_id,
            workflow_type="producer",
            workflow_version="producer/1",
            input_json={"short_drama": {"script_version_id": str(board.script_version_id)}},
            status="RUNNING",
            idempotency_key="canvas-auto-ownership",
            request_fingerprint="canvas-auto-ownership",
            trace_id="canvas-auto-ownership",
        )
        manifest = DependencyManifest(
            project_id=project_id,
            creation_key=uuid4(),
            request_fingerprint="a" * 64,
            created_by="service-owner",
            snapshot_json={
                "context": {
                    "continuity_review": {"storyboard_id": storyboard_id, "shot_id": shot_id}
                }
            },
        )
        db.add_all([run, manifest])
        db.flush()
        set_progress(db, run, "SHOTS", storyboard_id=storyboard_id)
        for key, status in [("own", "CREATED"), ("manual", "FAILED")]:
            job = GenerationJob(
                project_id=project_id,
                episode_id=board.episode_id,
                input_manifest_id=manifest.id,
                target_type="SHOT_IMAGE",
                task_type="IMAGE",
                provider="mock",
                model="mock",
                status=status,
                idempotency_key=f"canvas-job-{key}",
                request_fingerprint="b" * 64,
                created_by="service-owner",
            )
            db.add(job)
            db.flush()
            if key == "own":
                bind(db, project_id, MEDIA_SCOPE, job.id, {"run_id": str(run.id)})

    shot = projected_shot()
    assert shot["status"] == "CREATED"
    assert shot.get("cover_node_id") is None and shot.get("media_node_id") is None


def test_canvas_dialogue_uses_exact_bound_file_and_original_episode(client, audio_source):
    episode = audio_source[0]
    url, asset_id, manifest_id = prepare(client, audio_source)
    version_id = output_audio(client, audio_source, asset_id, manifest_id)
    bound = client.post(url + "/bindings", json={"asset_version_id": version_id})
    assert bound.status_code == 201, bound.text
    other = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "Same-project media retains its source episode"},
    ).json()
    node = exact(graph(client, other), version_id)
    assert node["preview_kind"] == "AUDIO" and node["asset_id"] == asset_id
    assert node["media_episode_id"] == episode["id"]
    assert node["duration_ms"] == 100
    assert node["media_file_id"] == bound.json()["file_id"]
    assert node["dependency_manifest_id"] == manifest_id
    assert node["status"] == "DRAFT" and not node["current"]
    file_url = f"/api/v1/projects/{episode['project_id']}/files/{node['media_file_id']}"
    metadata = client.get(file_url).json()
    assert metadata["asset_version_id"] == version_id
    assert metadata["checksum"] == node["checksum"]
    content = client.get(file_url + "/content")
    assert content.status_code == 200 and content.content == wave_bytes()
    assert hashlib.sha256(content.content).hexdigest() == node["checksum"]


@pytest.mark.parametrize("storage", ["LOCAL", "S3"])
@pytest.mark.parametrize("role", ["BACKGROUND", "AMBIENCE", "TRANSITION"])
def test_canvas_music_effects_read_registered_output_not_original_upload(
    client, episode, request, role, storage
):
    if storage == "S3":
        request.getfixturevalue("transfer_setup")
    asset = create_music(client, episode)
    source_id = music_source(client, episode)
    version = import_music(client, asset, source_id, role)
    node = exact(graph(client, episode), version["id"])
    assert node["preview_kind"] == "AUDIO" and node["asset_type"] == "MUSIC"
    assert node["media_file_id"] != source_id
    file_url = f"/api/v1/projects/{episode['project_id']}/files/{node['media_file_id']}"
    metadata = client.get(file_url).json()
    assert metadata["asset_version_id"] == version["id"]
    assert metadata["checksum"] == node["checksum"] == version["checksum"]
    content = client.get(file_url + "/content")
    assert content.status_code == 200 and content.content == wave_bytes()


def test_canvas_sealed_timeline_and_candidates_keep_exact_history_and_dependencies(
    client, episode, render_case
):
    value = graph(client, episode)
    timeline = exact(value, render_case["id"])
    assert timeline["preview_kind"] == "TIMELINE"
    assert timeline["media_episode_id"] == episode["id"]
    assert timeline["duration_ms"] == render_case["snapshot"]["duration_ms"]
    assert timeline["checksum"] == render_case["content_sha256"]
    assert timeline["dependency_manifest_id"] == render_case["dependency_manifest_id"]
    scene_shots = {
        shot["id"]: shot
        for item in value["nodes"]
        if item["kind"] == "scene"
        for shot in item["shots"]
    }
    for clip in render_case["snapshot"]["tracks"][0]["clips"]:
        scene_shot = scene_shots[f"shot:{clip['shot_id']}"]
        assert scene_shot["status"] == "READY"
        assert scene_shot["media_node_id"] == f"asset-version:{clip['source_asset_version_id']}"
    visuals = [
        n
        for n in value["nodes"]
        if n.get("asset_type") == "SHOT_IMAGE" and n["kind"] == "asset_version"
    ]
    assert len(visuals) >= 5
    for node in visuals:
        assert node["preview_kind"] == "IMAGE"
        assert node["preview_path"].endswith(f"/{node['resource_id']}/image")
        response = client.get("/api/v1" + node["preview_path"])
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == node["checksum"]
    assert any(
        edge["target"] == timeline["id"] and edge.get("relation") == "dependency"
        for edge in value["edges"]
    )
    positions = {timeline["id"]: {"x": 135, "y": 240}}
    saved = client.put(
        f"/api/v1/episodes/{episode['id']}/canvas/layout",
        json={
            "expected_revision": 0,
            "positions": positions,
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        },
    )
    assert saved.status_code == 200, saved.text
    after = graph(client, episode)
    assert after["nodes"] == value["nodes"] and after["edges"] == value["edges"]
    assert after["layout"]["positions"] == positions
    assert (
        client.put(
            f"/api/v1/episodes/{episode['id']}/canvas/layout",
            json={
                "expected_revision": 0,
                "positions": positions,
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
        ).status_code
        == 409
    )


def test_canvas_successful_mp4_is_a_precise_preview_not_a_formal_delivery(
    client, episode, delivery_case
):
    _, _, delivery = delivery_case
    value = graph(client, episode)
    node = exact(value, delivery["asset_version_id"])
    assert node["preview_kind"] == "DELIVERY"
    assert node["delivery_id"] == delivery["id"]
    assert node["checksum"] == delivery["sha256"]
    assert node["media_episode_id"] == episode["id"]
    assert delivery["profile"] == "PREVIEW" and not delivery["is_delivery"]
    assert "is_delivery" not in node
    response = client.get(history_url(episode) + f"/{delivery['id']}/content")
    assert response.status_code == 200 and response.headers["content-type"] == "video/mp4"
    assert hashlib.sha256(response.content).hexdigest() == node["checksum"]
    assert not {"object_key", "bucket", "signed_url", "content_base64"}.intersection(node)


@pytest.mark.parametrize("invalid", ["rights", "checksum", "object", "unsupported"])
def test_canvas_inconsistent_file_never_becomes_a_playable_node(client, episode, invalid):
    overrides = {
        "rights": {"rights": False},
        "checksum": {"checksum": "0" * 64},
        "object": {"object_key": "unrelated-object"},
        "unsupported": {},
    }[invalid]
    _, version_id, _ = stored_file(
        client,
        episode["project_id"],
        wave_bytes(),
        "application/octet-stream" if invalid == "unsupported" else "audio/wav",
        kind="AUDIO",
        **overrides,
    )
    node = exact(graph(client, episode), version_id)
    assert "preview_kind" not in node and "media_file_id" not in node
    assert node["resource_id"] == str(version_id)


def test_canvas_metadata_cannot_publish_an_unregistered_render(client, episode):
    _, version_id, _ = stored_file(
        client,
        episode["project_id"],
        b"not-an-executed-render",
        "video/mp4",
        kind="RENDER",
        version_metadata={"delivery_id": str(uuid4()), "is_delivery": True},
    )
    node = exact(graph(client, episode), version_id)
    assert node["status"] == "APPROVED"
    assert "preview_kind" not in node and "delivery_id" not in node


def test_canvas_viewer_reads_media_but_revocation_clears_all_private_routes(
    client, episode, delivery_case
):
    _, _, delivery = delivery_case
    node = exact(graph(client, episode), delivery["asset_version_id"])
    owner, member = viewer(client, episode)
    assert exact(graph(client, episode), delivery["asset_version_id"]) == node
    layout = f"/api/v1/episodes/{episode['id']}/canvas/layout"
    assert (
        client.put(
            layout,
            json={"expected_revision": 0, "positions": {}, "viewport": {"x": 0, "y": 0, "zoom": 1}},
        ).status_code
        == 403
    )
    client.headers["Authorization"] = owner
    assert (
        client.delete(
            f"/api/v1/projects/{episode['project_id']}/members/{member['user']['id']}"
        ).status_code
        == 204
    )
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    for path in [
        f"/api/v1/episodes/{episode['id']}/canvas",
        history_url(episode) + f"/{delivery['id']}",
        history_url(episode) + f"/{delivery['id']}/content",
        f"/api/v1/timelines/{delivery['timeline_id']}",
    ]:
        assert client.get(path).status_code == 404


def test_canvas_media_never_leaks_from_a_foreign_project(client, episode):
    foreign = client.post("/api/v1/projects", json={"name": "Unrelated media"}).json()
    _, version_id, _ = stored_file(client, foreign["id"], wave_bytes(), "audio/wav", kind="AUDIO")
    value = graph(client, episode)
    assert all(n.get("resource_id") != str(version_id) for n in value["nodes"])
    with client.app.state.session_factory() as db:
        assert db.get(AssetVersion, version_id) is not None
        file = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version_id))
        assert (
            client.get(
                f"/api/v1/projects/{episode['project_id']}/files/{file.id}/content"
            ).status_code
            == 404
        )


def test_canvas_formal_video_is_private_exact_and_unselected_without_temporal_review(
    client, settings, formal_video_case
):
    case, data = formal_video_case, video_bytes()
    _, attempt, provider = finish_video(client, settings, case, data)
    assert (
        generation_artifacts.import_attempt(
            client.app.state.session_factory, settings, attempt, provider
        )
        == 1
    )
    candidate = imported_rows(client)[0]
    episode = {"id": case["episode_id"], "project_id": case["project_id"]}
    node = exact(graph(client, episode), str(candidate.asset_version_id))
    assert node["preview_kind"] == "VIDEO"
    assert node["candidate_id"] == str(candidate.id)
    assert node["media_episode_id"] == case["episode_id"]
    assert node["asset_type"] == "VIDEO"
    assert node["status"] == "DRAFT" and not node["current"]
    assert node["duration_ms"] == 6000
    assert node["checksum"] == hashlib.sha256(data).hexdigest()
    assert node["preview_path"] == (
        f"/projects/{case['project_id']}/generation-candidates/{candidate.id}/content"
    )
    response = client.get("/api/v1" + node["preview_path"])
    assert response.status_code == 200 and response.content == data
    assert response.headers["content-type"] == "video/mp4"
    assert not {"object_key", "bucket", "signed_url", "content_base64"}.intersection(node)
    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(node["media_file_id"]))
        assert file.asset_version_id == candidate.asset_version_id
        assert not file.rights_confirmed
    detail = client.get(
        f"/api/v1/projects/{case['project_id']}/generation-candidates/{candidate.id}"
    ).json()
    assert detail["admission"]["status"] == "BLOCKED"
    assert detail["actions"]["can_select"] is False
    foreign = client.post("/api/v1/projects", json={"name": "Unrelated video"}).json()
    foreign_episode = client.post(
        f"/api/v1/projects/{foreign['id']}/episodes",
        json={"episode_no": 1, "title": "No foreign footage"},
    ).json()
    assert all(
        item.get("candidate_id") != str(candidate.id)
        for item in graph(client, foreign_episode)["nodes"]
    )
    assert (
        client.get(
            f"/api/v1/projects/{foreign['id']}/generation-candidates/{candidate.id}/content"
        ).status_code
        == 404
    )


@pytest.mark.parametrize("checksum_matches", [True, False])
def test_canvas_video_file_registration_must_match_the_formal_candidate(
    client, settings, formal_video_case, checksum_matches
):
    case = formal_video_case
    _, attempt, provider = finish_video(client, settings, case, video_bytes())

    def register_checksum(_mapper, _connection, file):
        # Inject a bad initial receipt without rewriting immutable history.
        if not checksum_matches and file.asset_version_id and file.media_type == "video/mp4":
            file.checksum = "f" * 64

    event.listen(StoredFile, "before_insert", register_checksum)
    try:
        assert (
            generation_artifacts.import_attempt(
                client.app.state.session_factory, settings, attempt, provider
            )
            == 1
        )
    finally:
        event.remove(StoredFile, "before_insert", register_checksum)
    candidate = imported_rows(client)[0]
    episode = {"id": case["episode_id"], "project_id": case["project_id"]}
    with client.app.state.session_factory() as db:
        file = db.scalar(
            select(StoredFile).where(StoredFile.asset_version_id == candidate.asset_version_id)
        )
        version = db.get(AssetVersion, candidate.asset_version_id)
        assert file is not None and version is not None
        assert (file.checksum == version.checksum) is checksum_matches
    node = exact(graph(client, episode), str(candidate.asset_version_id))
    if checksum_matches:
        assert node["preview_kind"] == "VIDEO"
        assert node["candidate_id"] == str(candidate.id)
        assert node["preview_path"].endswith(f"/{candidate.id}/content")
    else:
        assert node.get("preview_kind") is None and node.get("candidate_id") is None
        assert node.get("preview_path") is None


def test_canvas_video_metadata_cannot_publish_an_unregistered_candidate(client, episode):
    _, version_id, _ = stored_file(
        client,
        episode["project_id"],
        b"not-a-generated-video",
        "video/mp4",
        kind="VIDEO",
        version_metadata={
            "candidate_id": str(uuid4()),
            "video_check": {"status": "PASSED", "full_decode_verified": True},
            "video_source": {"storyboard_id": str(uuid4())},
        },
    )
    node = exact(graph(client, episode), version_id)
    assert node.get("preview_kind") is None and node.get("candidate_id") is None
    assert node.get("preview_path") is None
