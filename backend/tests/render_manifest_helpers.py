"""Real local media receipts shared by immutable render manifest tests."""

import hashlib
import json
from copy import deepcopy
from io import BytesIO
from uuid import UUID, uuid4

import av
import pytest
from PIL import Image
from sqlalchemy import select
from test_continuity_reviews import review_case
from test_media_validation import font_bytes, wave_bytes
from test_timeline_saves import saved_timeline
from test_timelines import created_timeline, timeline_case

from arclight.application.render_inputs import file_storage
from arclight.application.render_results import bind_render_artifact, bind_render_report
from arclight.persistence.models import (
    AssetVersion,
    Project,
    Script,
    StoredFile,
    Storyboard,
    VisualAsset,
)
from arclight.storage.objects import ObjectStorage

__all__ = [
    "review_case",
    "timeline_case",
    "render_case",
    "audio_case",
    "result_case",
    "report_case",
]


def stored_file(
    client,
    project_id,
    data,
    media_type,
    *,
    kind=None,
    rights=True,
    version_metadata=None,
    **overrides,
):
    settings = client.app.state.settings
    key = f"render-tests/{project_id}/{uuid4()}"
    checksum = hashlib.sha256(data).hexdigest()
    ObjectStorage(settings).put(key, data, media_type)
    with client.app.state.session_factory.begin() as db:
        version_id = None
        if kind:
            asset = VisualAsset(project_id=UUID(str(project_id)), asset_type=kind, name=kind)
            db.add(asset)
            db.flush()
            version = AssetVersion(
                asset_id=asset.id,
                version_no=1,
                object_key=key,
                checksum=checksum,
                status="APPROVED",
                created_by="service-owner",
                metadata_json=version_metadata or {},
            )
            db.add(version)
            db.flush()
            version_id = version.id
        row = StoredFile(
            **{
                "project_id": UUID(str(project_id)),
                "asset_version_id": version_id,
                "storage_backend": settings.object_storage_backend.upper(),
                "bucket": settings.s3_bucket if settings.object_storage_backend == "s3" else None,
                "object_key": key,
                "checksum": checksum,
                "byte_size": len(data),
                "media_type": media_type,
                "original_name": "render-fixture",
                "source_note": "Original offline test fixture",
                "rights_note": "Created for this project and licensed for rendering",
                "rights_confirmed": rights,
                "created_by": "service-owner",
                **overrides,
            }
        )
        db.add(row)
        db.flush()
        return row.id, version_id, checksum


def manifest_url(timeline):
    return f"/api/v1/timelines/{timeline['id']}/render-manifests"


def manifest(client, timeline, **overrides):
    body = {"creation_key": str(uuid4()), **overrides}
    response = client.post(manifest_url(timeline), json=body)
    assert response.status_code == 201, response.text
    return response.json(), body


@pytest.fixture
def render_case(client, episode, timeline_case):
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).output_profile = {
            "width": 256,
            "height": 256,
            "fps": 24,
        }
    body = {**timeline_case[0], "default_duration_ms": 500}
    return created_timeline(client, episode, body)


@pytest.fixture
def audio_case(client, episode, render_case):
    audio, audio_version, audio_hash = stored_file(
        client, episode["project_id"], wave_bytes(), "audio/wav", kind="AUDIO"
    )
    subtitle, subtitle_version, subtitle_hash = stored_file(
        client, episode["project_id"], b"A", "text/plain", kind="SUBTITLE"
    )
    font, _, _ = stored_file(client, episode["project_id"], font_bytes(), "font/ttf")
    with client.app.state.session_factory() as db:
        board = db.get(Storyboard, UUID(render_case["snapshot"]["storyboard_id"]))
        script = db.get(Script, board.script_version_id)
        script_id = str(script.id)
        line_id = script.content_json["scenes"][0]["dialogue"][0]["line_id"]
    snapshot = deepcopy(render_case["snapshot"])
    for track in snapshot["tracks"][1:]:
        is_subtitle = track["kind"] == "SUBTITLE"
        clip = {
            "id": str(uuid4()),
            "ordinal": 0,
            "start_ms": 0,
            "duration_ms": 1000,
            "source_asset_version_id": str(subtitle_version if is_subtitle else audio_version),
            "source_sha256": subtitle_hash if is_subtitle else audio_hash,
        }
        if track["kind"] in {"VOICE", "SUBTITLE"}:
            clip.update(script_version_id=script_id, line_id=line_id)
        if is_subtitle:
            clip["audio_version_id"] = str(audio_version)
        track["clips"] = [clip]
    timeline = saved_timeline(
        client,
        render_case,
        {
            "creation_key": str(uuid4()),
            "expected_version_no": render_case["version_no"],
            "snapshot": snapshot,
        },
    )
    return timeline, font, (audio, subtitle)


def actual_mp4(client, timeline):
    """Encode all five exact images; production async composition belongs to RENDER-07."""
    output = BytesIO()
    profile = timeline["snapshot"]["output_profile"]
    with av.open(output, "w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=profile["fps"])
        stream.width, stream.height = profile["width"], profile["height"]
        stream.pix_fmt = "yuv420p"
        index = 0
        with client.app.state.session_factory() as db:
            for clip in timeline["snapshot"]["tracks"][0]["clips"]:
                row = db.scalar(
                    select(StoredFile).where(
                        StoredFile.asset_version_id == UUID(clip["source_asset_version_id"])
                    )
                )
                data = file_storage(client.app.state.settings, row).read(
                    row.object_key, row.byte_size, row.checksum
                )
                with Image.open(BytesIO(data)) as image:
                    image = image.convert("RGB").resize((stream.width, stream.height))
                    for _ in range(clip["duration_ms"] * profile["fps"] // 1000):
                        frame = av.VideoFrame.from_image(image)
                        frame.pts = index
                        index += 1
                        for packet in stream.encode(frame):
                            container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)
    data = output.getvalue()
    with av.open(BytesIO(data)) as decoded:
        video = decoded.streams.video[0]
        assert (video.width, video.height, video.average_rate, video.codec_context.name) == (
            profile["width"],
            profile["height"],
            profile["fps"],
            "h264",
        )
        frames = list(decoded.decode(video))
        assert len(frames) == index == 60
        assert int(decoded.duration / 1000) == timeline["snapshot"]["duration_ms"]
    return data


def report_bytes(value, artifact, **overrides):
    return json.dumps(
        {
            "schema_version": 1,
            "manifest_id": value["id"],
            "manifest_sha256": value["content_sha256"],
            "timeline_id": value["snapshot"]["timeline_id"],
            "output_file_id": str(artifact.file_id),
            "output_sha256": artifact.sha256,
            "details": {"fixture_inspection": {"video_decodable": True}},
            **overrides,
        }
    ).encode()


@pytest.fixture
def result_case(client, episode, render_case):
    value, _ = manifest(client, render_case)
    data = actual_mp4(client, render_case)
    file_id, _, _ = stored_file(client, episode["project_id"], data, "video/mp4", rights=False)
    with client.app.state.session_factory.begin() as db:
        artifact = bind_render_artifact(
            db,
            client.app.state.settings,
            UUID(value["id"]),
            file_id,
            render_case["snapshot"]["duration_ms"],
            "service-owner",
        )
    return value, artifact, data


@pytest.fixture
def report_case(client, episode, result_case):
    value, artifact, _ = result_case
    file_id, _, _ = stored_file(
        client, episode["project_id"], report_bytes(value, artifact), "application/json"
    )
    with client.app.state.session_factory.begin() as db:
        report = bind_render_report(
            db, client.app.state.settings, UUID(value["id"]), file_id, "service-owner"
        )
    return value, artifact, report
