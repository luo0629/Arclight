"""VIDEO-01: real decoded clips use existing candidate, asset and source history."""

import io
from copy import deepcopy
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_collaboration import register
from test_contexts import approved_board
from test_generation_delivery import deliver
from test_generation_jobs import base, capture_ok
from test_generation_reconciliation import query
from test_generation_video_inputs import configure_video
from test_generation_video_inputs import video_case as video_case  # noqa: F401
from test_native_media_integration import attempt_for, provider_for
from test_text_contract_v2 import save_script

from arclight.application import generation_artifacts, video_candidates
from arclight.application.asset_objects import read_object
from arclight.application.generation_candidates import require_candidate_review
from arclight.core.errors import DomainError
from arclight.domain.candidate_checks import CandidateVideoCheck, CheckedVideo
from arclight.persistence.models import (
    AssetVersion,
    DependencyOutput,
    GenerationCandidate,
    GenerationMediaArtifact,
    Project,
    ProjectMember,
    StoredFile,
    User,
    VersionBinding,
    VisualAsset,
)


def video_bytes(*, seconds=6, width=1280, height=720, rate=24):
    import av

    result = io.BytesIO()
    with av.open(result, "w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=rate)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        frame = av.VideoFrame(width, height, "yuv420p")
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        for index in range(seconds * rate):
            frame.pts = index
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return result.getvalue()


@pytest.fixture
def formal_video_case(client, video_case):
    case = deepcopy(video_case)
    episode = {"id": case["episode_id"], "project_id": case["project_id"]}
    script = save_script(client, episode)
    board, shots = approved_board(client, episode, script)
    case["body"]["review_target"] = {"storyboard_id": board["id"], "shot_id": shots[0]["id"]}
    case["body"]["inputs"].append(
        {
            "source_asset_version_id": board["id"],
            "dependency_type": "CONTENT_DEPENDENCY",
            "selectors": ["shot:" + shots[0]["id"]],
        }
    )
    return case


def finish_video(client, settings, case, data):
    job = capture_ok(client, case["project_id"], case["body"])
    request_id = "video01-" + job["id"]

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": request_id})
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=data)
        return httpx.Response(
            200,
            json={
                "status": "succeeded",
                "content": {"video_url": "https://cdn.example/video"},
            },
        )

    provider = provider_for(client, settings, job, handler)
    assert deliver(client, job, provider) == "SUBMITTED"
    from arclight.application.generation_reconciliation import mark_result_unknown

    with client.app.state.session_factory.begin() as db:
        mark_result_unknown(db, UUID(job["id"]), attempt_for(client, job), request_id=request_id)
    latest = client.get(base(case["project_id"]) + "/" + job["id"]).json()
    response = query(client, latest, provider)
    assert response.status_code == 200 and response.json()["status"] == "SUCCEEDED", response.text
    return job, attempt_for(client, job), provider


def imported_rows(client):
    with client.app.state.session_factory() as db:
        return list(
            db.scalars(
                select(GenerationCandidate).where(
                    GenerationCandidate.media_check_json["schema_version"].as_string()
                    == "candidate-video-check/1"
                )
            )
        )


def test_formal_video_records_real_bytes_sources_and_unselected_history(
    client,
    settings,
    formal_video_case,
):
    case, data = formal_video_case, video_bytes()
    job, attempt, provider = finish_video(client, settings, case, data)
    assert (
        generation_artifacts.import_attempt(
            client.app.state.session_factory, settings, attempt, provider
        )
        == 1
    )
    rows = imported_rows(client)
    assert len(rows) == 1 and rows[0].error_code is None
    check = CandidateVideoCheck.model_validate(rows[0].media_check_json)
    assert check.status == "PASSED" and check.video.full_decode_verified
    assert check.video.codec == "mpeg4" and check.video.decoded_frames == 144
    assert check.video.duration_ms == 6000 and check.video.frame_rate == 24
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, rows[0].asset_version_id)
        asset = db.get(VisualAsset, version.asset_id)
        artifact = db.scalar(select(GenerationMediaArtifact))
        assert asset.asset_type == "VIDEO" and asset.current_version_id is None
        assert version.status == "DRAFT" and not version.stale
        assert version.metadata_json["required_review"] == "VIDEO_TEMPORAL"
        origin = version.metadata_json["video_source"]
        assert origin["artifact_id"] == str(artifact.id)
        assert origin["source_file_id"] == str(artifact.file_id)
        assert origin["storyboard_id"] == case["body"]["review_target"]["storyboard_id"]
        assert origin["video_images"][0]["asset_version_id"] == case["version"]["id"]
        assert (
            read_object(
                settings, asset.project_id, version.id, version.object_key, version.checksum
            )
            == data
        )
        assert (
            db.scalar(
                select(DependencyOutput).where(DependencyOutput.asset_version_id == version.id)
            )
            is not None
        )
        assert (
            db.scalar(
                select(VersionBinding).where(
                    VersionBinding.resource_type == "ASSET_VERSION",
                    VersionBinding.resource_id == version.id,
                )
            )
            is not None
        )
        assert (
            video_candidates.require_video_candidate_media_check(db, asset.project_id, rows[0].id)
            == check
        )
        with pytest.raises(DomainError) as error:
            require_candidate_review(version, db)
        assert error.value.code == "VIDEO_REVIEW_REQUIRED"
        original_metadata = deepcopy(version.metadata_json)
    assert generation_artifacts.pending_attempts(client.app.state.session_factory) == []
    assert (
        generation_artifacts.import_attempt(
            client.app.state.session_factory, settings, attempt, provider
        )
        == 0
    )
    assert len(imported_rows(client)) == 1
    with client.app.state.session_factory() as db:
        assert db.get(AssetVersion, rows[0].asset_version_id).metadata_json == original_metadata


def test_generic_video_keeps_artifact_without_formal_candidate(client, settings, video_case):
    _, attempt, provider = finish_video(client, settings, video_case, video_bytes())
    assert (
        generation_artifacts.import_attempt(
            client.app.state.session_factory, settings, attempt, provider
        )
        == 1
    )
    assert imported_rows(client) == []
    assert generation_artifacts.pending_attempts(client.app.state.session_factory) == []


@pytest.mark.parametrize(
    "fault", ["damaged", "wrong_geometry", "wrong_duration", "too_long", "too_fast"]
)
def test_invalid_video_keeps_failure_and_never_becomes_a_version(
    client,
    settings,
    formal_video_case,
    fault,
):
    data = (
        b"damaged mp4"
        if fault == "damaged"
        else video_bytes(
            seconds=2 if fault == "wrong_duration" else 31 if fault == "too_long" else 6,
            width=640 if fault == "wrong_geometry" else 1280,
            rate=121 if fault == "too_fast" else 24,
        )
    )
    _, attempt, provider = finish_video(client, settings, formal_video_case, data)
    assert (
        generation_artifacts.import_attempt(
            client.app.state.session_factory, settings, attempt, provider
        )
        == 1
    )
    rows = imported_rows(client)
    assert len(rows) == 1 and rows[0].asset_version_id is None
    assert rows[0].error_code == (
        "INVALID_MEDIA_OUTPUT"
        if fault in {"damaged", "too_long", "too_fast"}
        else "VIDEO_OUTPUT_MISMATCH"
    )
    assert (
        generation_artifacts.import_attempt(
            client.app.state.session_factory, settings, attempt, provider
        )
        == 0
    )


def test_existing_artifact_recovery_reprobes_old_bytes_without_fabricating_old_proof(
    client,
    settings,
    formal_video_case,
    monkeypatch,
):
    _, attempt, provider = finish_video(client, settings, formal_video_case, video_bytes())
    real_probe = generation_artifacts.probe
    proof_keys = {"codec", "decoded_frames", "has_audio", "full_decode_verified"}
    with monkeypatch.context() as patch:
        patch.setattr(
            generation_artifacts,
            "probe",
            lambda data, media_type: {
                key: value
                for key, value in real_probe(data, media_type).items()
                if key not in proof_keys
            },
        )
        patch.setattr(video_candidates, "import_video_candidates", lambda *_: 0)
        assert (
            generation_artifacts.import_attempt(
                client.app.state.session_factory, settings, attempt, provider
            )
            == 1
        )
    assert generation_artifacts.pending_attempts(client.app.state.session_factory) == [attempt]
    assert (
        generation_artifacts.import_attempt(
            client.app.state.session_factory, settings, attempt, provider
        )
        == 1
    )
    row = imported_rows(client)[0]
    assert CandidateVideoCheck.model_validate(row.media_check_json).video.full_decode_verified
    with client.app.state.session_factory() as db:
        assert not proof_keys & db.scalar(select(GenerationMediaArtifact)).media_json.keys()


def test_private_video_file_tampering_cannot_pass_frozen_check(client, settings, formal_video_case):
    _, attempt, provider = finish_video(client, settings, formal_video_case, video_bytes())
    generation_artifacts.import_attempt(
        client.app.state.session_factory, settings, attempt, provider
    )
    row = imported_rows(client)[0]
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, row.asset_version_id)
        stored = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version.id))
        storage = video_candidates.file_storage(settings, stored)
        storage_path = storage.path(stored.object_key)
        storage_path.write_bytes(b"different bytes")
        asset = db.get(VisualAsset, version.asset_id)
        with pytest.raises(DomainError):
            video_candidates.require_video_version(
                db, settings, asset.project_id, asset, version, asset.created_by
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"width": 4097},
        {"width": 4096, "height": 4096},
        {"duration_ms": 30001},
        {"frame_rate": 121},
        {"decoded_frames": 3601},
        {"full_decode_verified": False},
        {"codec": "vp9"},
        {"frame_rate": float("nan")},
    ],
)
def test_video_evidence_rejects_untrusted_or_over_limit_claims(changes):
    value = {
        "checksum": "a" * 64,
        "media_type": "video/mp4",
        "byte_size": 100,
        "width": 1280,
        "height": 720,
        "duration_ms": 6000,
        "frame_rate": 24,
        "codec": "mpeg4",
        "decoded_frames": 144,
        "has_audio": False,
        "full_decode_verified": True,
    }
    with pytest.raises(ValidationError):
        CheckedVideo.model_validate(value | changes)


@pytest.mark.parametrize("authority", ["revoked", "viewer", "disabled"])
def test_video_import_rechecks_original_creator_after_decode(
    client, settings, formal_video_case, monkeypatch, authority
):
    case = formal_video_case
    editor = register(client, "video-import-editor@example.com")
    actor = UUID(editor["user"]["id"])
    configure_video(client, settings)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        db.add(ProjectMember(project_id=UUID(case["project_id"]), user_id=actor, role="editor"))
    _, attempt, provider = finish_video(client, settings, case, video_bytes())
    real_validate = video_candidates.validated_video

    def revoke_after_decode(*args):
        result = real_validate(*args)
        with factory.begin() as db:
            membership = db.scalar(select(ProjectMember).where(ProjectMember.user_id == actor))
            if authority == "revoked":
                db.delete(membership)
            elif authority == "viewer":
                membership.role = "viewer"
            else:
                db.get(User, actor).disabled = True
        return result

    monkeypatch.setattr(video_candidates, "validated_video", revoke_after_decode)
    assert generation_artifacts.import_attempt(factory, settings, attempt, provider) == 1
    rows = imported_rows(client)
    assert len(rows) == 1 and rows[0].asset_version_id is None
    assert rows[0].error_code == ("NOT_FOUND" if authority == "revoked" else "FORBIDDEN")
    assert rows[0].media_check_json["status"] == "FAILED"
    with factory() as db:
        artifact = db.scalar(select(GenerationMediaArtifact))
        assert artifact.file_id is not None and artifact.error_code is None
        assert db.get(StoredFile, artifact.file_id) is not None
        assert db.scalar(select(VisualAsset).where(VisualAsset.asset_type == "VIDEO")) is None
    assert generation_artifacts.import_attempt(factory, settings, attempt, provider) == 0


def test_video_import_archived_during_decode_remains_pending_until_unarchived(
    client, settings, formal_video_case, monkeypatch
):
    case = formal_video_case
    factory = client.app.state.session_factory
    _, attempt, provider = finish_video(client, settings, case, video_bytes())
    real_validate = video_candidates.validated_video

    def archive_after_decode(*args):
        result = real_validate(*args)
        with factory.begin() as db:
            db.get(Project, UUID(case["project_id"])).archived = True
        return result

    with monkeypatch.context() as patch:
        patch.setattr(video_candidates, "validated_video", archive_after_decode)
        assert generation_artifacts.import_attempt(factory, settings, attempt, provider) == 1
    assert imported_rows(client) == []
    assert generation_artifacts.pending_attempts(factory) == [attempt]
    with factory.begin() as db:
        artifact = db.scalar(select(GenerationMediaArtifact))
        artifact_id, original_media = artifact.id, deepcopy(artifact.media_json)
        assert artifact.file_id is not None and artifact.error_code is None
        db.get(Project, UUID(case["project_id"])).archived = False
    assert generation_artifacts.import_attempt(factory, settings, attempt, provider) == 1
    rows = imported_rows(client)
    assert len(rows) == 1 and rows[0].asset_version_id is not None
    assert rows[0].error_code is None
    assert generation_artifacts.pending_attempts(factory) == []
    with factory() as db:
        assert db.get(GenerationMediaArtifact, artifact_id).media_json == original_media


@pytest.mark.parametrize("status", ["LOCKED", "ARCHIVED"])
def test_late_video_import_preserves_readonly_target_as_explicit_stale_history(
    client, settings, formal_video_case, status
):
    case = formal_video_case
    factory = client.app.state.session_factory
    _, first_attempt, first_provider = finish_video(client, settings, case, video_bytes())
    assert (
        generation_artifacts.import_attempt(factory, settings, first_attempt, first_provider) == 1
    )
    original = imported_rows(client)[0]
    with factory() as db:
        original_version = db.get(AssetVersion, original.asset_version_id)
        target = db.get(VisualAsset, original_version.asset_id)
        target_id, original_id = target.id, original_version.id
        metadata = deepcopy(original_version.metadata_json)
        targeted_case = deepcopy(case)
        targeted_case["body"].update(
            target_asset_id=str(target.id),
            expected_asset={
                "revision": target.revision,
                "lock_version": target.lock_version,
                "current_version_id": None,
            },
        )
    _, attempt, provider = finish_video(client, settings, targeted_case, video_bytes())
    # Simulate a target lifecycle change after the supplier has completed, before
    # its persisted artifact is delivered. The original content stays immutable.
    with factory.begin() as db:
        target = db.get(VisualAsset, target_id)
        target.status = status
        target.lock_version += 1
        target.revision += 1
        revision, lock_version = target.revision, target.lock_version
    assert generation_artifacts.import_attempt(factory, settings, attempt, provider) == 1
    with factory() as db:
        candidate = db.scalar(
            select(GenerationCandidate).where(GenerationCandidate.attempt_id == attempt)
        )
        version = db.get(AssetVersion, candidate.asset_version_id)
        target = db.get(VisualAsset, target_id)
        assert version.id != original_id and version.stale and version.status == "DRAFT"
        assert version.metadata_json["target_changed_since_submission"] is True
        assert target.current_version_id is None and target.status == status
        assert target.lock_version == lock_version and target.revision == revision + 1
        assert db.get(AssetVersion, original_id).metadata_json == metadata
