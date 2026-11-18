"""Browser-only synthetic jobs retain production dispatch, media and review gates."""

import hashlib
import importlib.util
import tempfile
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_generation_jobs import capture_ok
from test_generation_video_inputs import video_case as video_case  # noqa: F401
from test_video_candidates import formal_video_case as formal_video_case  # noqa: F401

from arclight.core.config import Settings
from arclight.media.video import inspect_video
from arclight.persistence.generation import GenerationReconciliation
from arclight.persistence.models import (
    AssetVersion,
    GenerationCandidate,
    GenerationJob,
    VersionBinding,
    VisualAsset,
)

spec = importlib.util.spec_from_file_location(
    "video01_e2e_script", Path(__file__).resolve().parents[1] / "scripts" / "e2e_video.py"
)
e2e_video = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e_video)


@pytest.fixture
def settings():
    # This runner deliberately uses its own SQLite database even in the PG suite.
    with tempfile.TemporaryDirectory(prefix="arclight-e2e-") as folder:
        path = Path(folder)
        yield Settings(
            _env_file=None,
            environment="test",
            provider="mock",
            api_token="test-token",
            database_url="sqlite:///" + (path / "business.db").as_posix(),
            checkpoint_url="sqlite:///" + (path / "checkpoints.db").as_posix(),
            asset_storage_path=path / "objects",
        )


def test_browser_video_runner_registers_real_h264_candidate_without_review_or_selection(
    client,
    settings,
    formal_video_case,
):
    case = formal_video_case
    case["body"]["spec"]["context"] = {"e2e_synthetic_video": "video01"}
    job = capture_ok(client, case["project_id"], case["body"])
    factory = client.app.state.session_factory
    assert e2e_video.isolated_browser_database(factory, settings)
    assert e2e_video.advance_videos(factory, settings) == 1
    assert e2e_video.advance_videos(factory, settings) == 0
    with factory() as db:
        saved = db.get(GenerationJob, UUID(job["id"]))
        assert saved.status == "SUCCEEDED"
        candidate = db.scalar(
            select(GenerationCandidate).where(GenerationCandidate.job_id == saved.id)
        )
        version = db.get(AssetVersion, candidate.asset_version_id)
        asset = db.get(VisualAsset, version.asset_id)
        media = candidate.media_check_json["video"]
        assert media["codec"] == "h264" and media["decoded_frames"] == 144
        assert media["duration_ms"] == 6000 and media["frame_rate"] == 24
        assert (
            media["full_decode_verified"]
            and media["checksum"] == hashlib.sha256(e2e_video.synthetic_video()).hexdigest()
        )
        assert version.status == "DRAFT" and asset.current_version_id is None
        assert db.scalar(select(func.count()).select_from(GenerationReconciliation)) == 1
        assert not list(
            db.scalars(
                select(VersionBinding).where(
                    VersionBinding.resource_type.in_(
                        ["VIDEO_TEMPORAL_REVIEW", "VIDEO_TEMPORAL_ADMISSION"]
                    )
                )
            )
        )


@pytest.mark.parametrize("marker", [None, "video02", True])
def test_unmarked_native_jobs_are_not_advanced(client, settings, formal_video_case, marker):
    case = formal_video_case
    case["body"]["spec"]["context"] = {} if marker is None else {"e2e_synthetic_video": marker}
    job = capture_ok(client, case["project_id"], case["body"])
    assert e2e_video.advance_videos(client.app.state.session_factory, settings) == 0
    with client.app.state.session_factory() as db:
        assert db.get(GenerationJob, UUID(job["id"])).status == "CREATED"
        assert db.scalar(select(func.count()).select_from(GenerationCandidate)) == 0


@pytest.mark.parametrize("problem", ["environment", "path", "engine"])
def test_browser_video_runner_refuses_nonisolated_settings(client, settings, problem):
    factory = client.app.state.session_factory
    changed = settings.model_copy()
    if problem == "environment":
        changed.environment = "production"
    elif problem == "path":
        changed.database_url = "sqlite:///business.db"
    else:
        changed.database_url = settings.database_url.replace("arclight-e2e-", "arclight-e2e-other-")
    assert not e2e_video.isolated_browser_database(factory, changed)
    assert e2e_video.advance_videos(factory, changed) == 0


def test_browser_video_failure_is_diagnosed_without_killing_worker(
    client,
    settings,
    formal_video_case,
    monkeypatch,
    caplog,
):
    case = formal_video_case
    case["body"]["spec"]["context"] = {"e2e_synthetic_video": "video01"}
    job = capture_ok(client, case["project_id"], case["body"])

    def fail(*args):
        raise RuntimeError("private fixture details must not enter the log")

    monkeypatch.setattr(e2e_video, "_advance", fail)
    assert e2e_video.advance_videos(client.app.state.session_factory, settings) == 0
    assert job["id"] in caplog.text and "RuntimeError" in caplog.text
    assert "private fixture details" not in caplog.text
    assert "video01-fixture-key" not in caplog.text


def test_synthetic_clip_is_fully_decodable_h264_for_browser_playback():
    media = inspect_video(e2e_video.synthetic_video())
    assert media["codec"] == "h264" and media["width"] == 1280 and media["height"] == 720
    assert media["duration_ms"] == 6000 and media["decoded_frames"] == 144
