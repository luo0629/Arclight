"""RENDER-06 exact immutable inputs, profiles and licensed font capture."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import av
import pytest
from render_manifest_helpers import (
    audio_case,
    manifest,
    manifest_url,
    render_case,
    review_case,
    stored_file,
    timeline_case,
)
from sqlalchemy import func, select
from test_collaboration import create_project
from test_media_validation import font_bytes
from test_timeline_saves import saved_case, saved_timeline
from test_timeline_visuals import configured_body

from arclight.application.render_manifests import output_profile
from arclight.core.errors import DomainError
from arclight.domain.timeline_visuals import ClipVisual
from arclight.domain.versions import digest
from arclight.media.versions import renderer_software
from arclight.persistence.models import (
    AssetVersion,
    AuditEvent,
    DependencyManifest,
    Project,
    StoredFile,
)
from arclight.persistence.render_manifests import RenderManifest
from arclight.storage.objects import ObjectStorage

__all__ = ["audio_case", "render_case", "review_case", "saved_case", "timeline_case"]


def test_manifest_freezes_exact_timeline_sources_defaults_software_and_dependencies(
    client, episode, render_case
):
    started = datetime.now(UTC)
    value, _ = manifest(client, render_case)
    snapshot = value["snapshot"]
    assert snapshot["timeline"] == render_case["snapshot"]
    assert snapshot["timeline_sha256"] == render_case["content_sha256"]
    assert snapshot["timeline_sha256"] == digest(snapshot["timeline"])
    assert value["content_sha256"] == digest(snapshot)
    assert snapshot["dependency_manifest_id"] == render_case["dependency_manifest_id"]
    assert snapshot["software"] == renderer_software().model_dump(mode="json")
    assert snapshot["software"]["pyav_version"] == av.__version__
    assert started <= datetime.fromisoformat(snapshot["created_at"]) <= datetime.now(UTC)
    clips = snapshot["timeline"]["tracks"][0]["clips"]
    assert snapshot["visuals"] == [
        {"clip_id": clip["id"], "settings": ClipVisual().model_dump(mode="json")} for clip in clips
    ]
    assert len(snapshot["sources"]) == 7
    assert {source["asset_type"] for source in snapshot["sources"]} == {
        "SCRIPT",
        "STORYBOARD",
        "SHOT_IMAGE",
    }
    with client.app.state.session_factory() as db:
        dependency = db.get(DependencyManifest, UUID(snapshot["dependency_manifest_id"]))
        assert snapshot["dependency_manifest_sha256"] == digest(dependency.snapshot_json)
        for source in snapshot["sources"]:
            version = db.get(AssetVersion, UUID(source["asset_version_id"]))
            assert source["sha256"] == version.checksum
            if source["file"]:
                file = db.get(StoredFile, UUID(source["file"]["file_id"]))
                assert file.asset_version_id == version.id
                assert source["file"]["sha256"] == file.checksum == version.checksum
                assert source["file"]["rights_confirmed"] is file.rights_confirmed
                assert source["file"]["source_note"] == file.source_note
                assert source["file"]["rights_note"] == file.rights_note
        # Generated candidates keep their original, unconfirmed usage evidence.
        assert any(
            source["file"] and source["file"]["rights_confirmed"] is False
            for source in snapshot["sources"]
        )
    assert "object_key" not in str(value)
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value


def test_schema2_order_crop_motion_fades_and_history_remain_exact(client, saved_case):
    original, body, _ = saved_case
    timeline = saved_timeline(client, original, configured_body(body))
    value, _ = manifest(client, timeline)
    assert value["snapshot"]["timeline"] == timeline["snapshot"]
    clips = timeline["snapshot"]["tracks"][0]["clips"]
    assert [item["settings"] for item in value["snapshot"]["visuals"]] == [
        clip["visual"] for clip in clips
    ]
    assert client.get(f"/api/v1/timelines/{original['id']}").json() == original


def test_all_five_tracks_and_font_capture_exact_audio_subtitle_and_stable_line(client, audio_case):
    timeline, font_id, _ = audio_case
    missing = client.post(manifest_url(timeline), json={"creation_key": str(uuid4())})
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "RENDER_FONT_REQUIRED"
    value, _ = manifest(
        client,
        timeline,
        font_file_id=str(font_id),
        subtitle_style={
            "font_size": 32,
            "color": "#FFFF00",
            "outline_width": 3,
            "bold": True,
        },
    )
    snapshot = value["snapshot"]
    assert snapshot["timeline"] == timeline["snapshot"]
    assert all(track["clips"] for track in snapshot["timeline"]["tracks"])
    assert snapshot["font"]["file_id"] == str(font_id)
    assert snapshot["font"]["media_type"] == "font/ttf"
    assert len(snapshot["sources"]) == 9
    assert {source["asset_type"] for source in snapshot["sources"]} >= {"AUDIO", "SUBTITLE"}
    subtitle = snapshot["timeline"]["tracks"][4]["clips"][0]
    assert subtitle["audio_version_id"] in {
        source["asset_version_id"] for source in snapshot["sources"]
    }
    assert subtitle["script_version_id"] in {
        source["asset_version_id"] for source in snapshot["sources"]
    }
    assert subtitle["line_id"] and snapshot["subtitle_style"]["bold"] is True


def test_distinct_profiles_replay_pagination_and_project_profile_changes(client, render_case):
    first, body = manifest(client, render_case)
    assert client.post(manifest_url(render_case), json=body).json() == first
    conflict = client.post(manifest_url(render_case), json={**body, "profile": "FORMAL"})
    assert conflict.status_code == 409
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(first["project_id"])).output_profile = {
            "width": 1920,
            "height": 1080,
            "fps": 60,
        }
    formal, _ = manifest(client, render_case, profile="FORMAL")
    assert formal["id"] != first["id"]
    assert formal["snapshot"]["timeline_sha256"] == first["snapshot"]["timeline_sha256"]
    for value, video_rate, audio_rate in [(first, 2_000_000, 96_000), (formal, 8_000_000, 192_000)]:
        output = value["snapshot"]["output"]
        assert (output["width"], output["height"], output["fps"]) == (256, 256, 24)
        assert (output["video_bitrate"], output["audio_bitrate"]) == (video_rate, audio_rate)
        assert (output["container"], output["video_encoder"], output["audio_codec"]) == (
            "mp4",
            "libx264",
            "aac",
        )
    assert client.get(manifest_url(render_case), params={"limit": 1}).json() == [formal]
    assert client.get(manifest_url(render_case), params={"before_id": formal["id"]}).json() == [
        first
    ]
    assert client.get(manifest_url(render_case), params={"limit": 101}).status_code == 422
    assert (
        client.get(manifest_url(render_case), params={"before_id": str(uuid4())}).status_code == 404
    )
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(RenderManifest.id))) == 2
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == "render.manifest_created"
                )
            )
            == 2
        )


@pytest.mark.parametrize(
    "geometry,expected",
    [
        ({"width": 1080, "height": 1920, "fps": 30}, (360, 640, 24)),
        ({"width": 1920, "height": 1080, "fps": 60}, (640, 360, 24)),
        ({"width": 128, "height": 128, "fps": 12}, (128, 128, 12)),
    ],
)
def test_preview_preserves_exact_aspect_ratio_even_geometry_and_never_upscales(geometry, expected):
    output = output_profile(geometry, "PREVIEW")
    assert (output.width, output.height, output.fps) == expected


@pytest.mark.parametrize(
    "geometry",
    [
        {"width": 255, "height": 256, "fps": 24},
        {"width": 8192, "height": 8192, "fps": 24},
        {"width": True, "height": 256, "fps": 24},
        {"width": 256, "height": 256, "fps": 0},
        {"width": 256, "height": 256, "fps": 24, "command": "custom"},
        {"width": 4094, "height": 4092, "fps": 24},
    ],
)
def test_invalid_encoder_geometry_is_rejected(geometry):
    with pytest.raises(DomainError, match="输出规格"):
        output_profile(geometry, "FORMAL")


@pytest.mark.parametrize(
    "extra",
    [
        {"command": "ffmpeg"},
        {"output_path": "output.mp4"},
        {"filters": "movie=x"},
        {"profile": "custom"},
        {"subtitle_style": {"font_size": True}},
        {"subtitle_style": {"font_size": 145}},
        {"subtitle_style": {"bold": 1}},
        {"subtitle_style": {"color": "red"}},
        {"subtitle_style": {"font_path": "x.ttf"}},
    ],
)
def test_public_contract_rejects_arbitrary_commands_paths_and_unbounded_style(client, extra):
    response = client.post(
        f"/api/v1/timelines/{uuid4()}/render-manifests",
        json={
            "creation_key": str(uuid4()),
            **extra,
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "problem", ["foreign", "wrong_type", "unlicensed", "corrupt", "missing", "otf"]
)
def test_font_requires_exact_authorized_project_file_and_actual_decoding(
    client, episode, render_case, problem
):
    project = create_project(client)["id"] if problem == "foreign" else episode["project_id"]
    media_type = (
        "text/plain" if problem == "wrong_type" else "font/otf" if problem == "otf" else "font/ttf"
    )
    data = b"fake-font" if problem == "corrupt" else font_bytes(problem != "otf")
    file_id, _, _ = stored_file(client, project, data, media_type, rights=problem != "unlicensed")
    if problem == "missing":
        with client.app.state.session_factory() as db:
            row = db.get(StoredFile, file_id)
            ObjectStorage(client.app.state.settings).delete(row.object_key)
    response = client.post(
        manifest_url(render_case),
        json={
            "creation_key": str(uuid4()),
            "font_file_id": str(file_id),
        },
    )
    if problem == "otf":
        assert response.status_code == 201, response.text
        assert response.json()["snapshot"]["font"]["media_type"] == "font/otf"
    else:
        assert response.status_code in {404, 409, 422}, response.text
        with client.app.state.session_factory() as db:
            assert db.scalar(select(func.count(RenderManifest.id))) == 0


def test_new_manifest_checks_actual_source_bytes_but_history_is_stable(client, render_case):
    value, body = manifest(client, render_case)
    source = next(source for source in value["snapshot"]["sources"] if source["file"])
    with client.app.state.session_factory() as db:
        row = db.get(StoredFile, UUID(source["file"]["file_id"]))
        ObjectStorage(client.app.state.settings).path(row.object_key).write_bytes(b"corrupt")
    response = client.post(manifest_url(render_case), json={"creation_key": str(uuid4())})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OBJECT_UNAVAILABLE"
    assert client.post(manifest_url(render_case), json=body).json() == value
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value
