"""Versioned offline media exercising the existing immutable rendering path."""

import base64
import hashlib
from array import array
from copy import deepcopy
from io import BytesIO
from uuid import UUID, uuid4

import av
import pytest
from PIL import Image
from render_admission_helpers import ready_manifest, request_export
from render_job_helpers import read, run, submit
from render_manifest_helpers import audio_case, render_case, review_case, stored_file, timeline_case
from render_report_helpers import report_bytes, report_read
from test_human_reviews import decide, resume
from test_render_captions import caption_font
from test_render_mixing import tone
from test_timeline_saves import saved_timeline

from arclight.persistence.models import AssetVersion, StoredFile
from arclight.storage.objects import ObjectStorage

__all__ = ["audio_case", "render_case", "review_case", "timeline_case"]
DATASET = "render-offline/1"
SRT = b"1\n00:00:00,000 --> 00:00:01,000\nA\n"


def object_bytes(client, file_id):
    with client.app.state.session_factory() as db:
        row = db.get(StoredFile, UUID(str(file_id)))
        assert row is not None
        return ObjectStorage(client.app.state.settings).read(
            row.object_key, row.byte_size, row.checksum
        )


def input_state(client, manifest):
    """Include actual object hashes and immutable asset facts, not just database hashes."""
    result = {}
    sources = manifest["snapshot"]["sources"]
    references = [source["file"] for source in sources if source.get("file")]
    references.append(manifest["snapshot"]["font"])
    with client.app.state.session_factory() as db:
        for reference in references:
            row = db.get(StoredFile, UUID(reference["file_id"]))
            data = object_bytes(client, row.id)
            result[str(row.id)] = (
                row.object_key,
                row.checksum,
                row.byte_size,
                row.media_type,
                hashlib.sha256(data).hexdigest(),
            )
        for source in sources:
            version = db.get(AssetVersion, UUID(source["asset_version_id"]))
            result[str(version.id)] = (
                str(version.asset_id),
                version.version_no,
                version.object_key,
                version.checksum,
                version.status,
                deepcopy(version.metadata_json),
            )
    return result


@pytest.fixture
def offline_case(client, episode, audio_case):
    timeline, _, _ = audio_case
    snapshot = deepcopy(timeline["snapshot"])
    voice_id = None
    for kind, frequency in (("VOICE", 440), ("SFX", 660), ("MUSIC", 880)):
        source = base64.b64decode(tone(frequency=frequency, amplitude=0.05).data)
        _, version, checksum = stored_file(
            client, episode["project_id"], source, "audio/wav", kind="AUDIO"
        )
        clip = next(track for track in snapshot["tracks"] if track["kind"] == kind)["clips"][0]
        clip.update(source_asset_version_id=str(version), source_sha256=checksum)
        if kind == "VOICE":
            voice_id = version
    _, subtitle, checksum = stored_file(
        client, episode["project_id"], SRT, "text/plain", kind="SUBTITLE"
    )
    clip = next(track for track in snapshot["tracks"] if track["kind"] == "SUBTITLE")["clips"][0]
    clip.update(
        source_asset_version_id=str(subtitle),
        source_sha256=checksum,
        audio_version_id=str(voice_id),
    )
    timeline = saved_timeline(
        client,
        timeline,
        {
            "creation_key": str(uuid4()),
            "expected_version_no": timeline["version_no"],
            "snapshot": snapshot,
        },
    )
    font, _, _ = stored_file(client, episode["project_id"], caption_font("A"), "font/ttf")
    return timeline, str(font)


@pytest.fixture
def rendered_offline(client, episode, offline_case):
    timeline, font = offline_case
    manifest, _ = ready_manifest(client, timeline, profile="FORMAL", font_file_id=font)
    inputs = input_state(client, manifest)
    requested = request_export(client, manifest)
    decision = decide(client, requested)
    assert decision.status_code == 200, decision.text
    assert resume(client, requested, decision.json()).status_code == 200
    results = []
    for _ in range(2):
        job, _ = submit(client, manifest)
        assert run(client, job) == "SUCCEEDED", read(client, job)
        final = read(client, job)
        report = report_read(client, episode, final["artifact_id"])
        results.append(
            (
                final,
                object_bytes(client, final["output_file_id"]),
                report,
                report_bytes(client, report),
            )
        )
    return timeline, manifest, inputs, results


def sample_movie(data, snapshot, sources):
    """Decode frames across every shot and audio in both the voiced and silent windows."""
    with av.open(BytesIO(data)) as container:
        frames = list(container.decode(video=0))
    assert len(frames) == 60
    video = next(track for track in snapshot["tracks"] if track["kind"] == "VIDEO")
    for clip in video["clips"]:
        index = (clip["start_ms"] + clip["duration_ms"] // 2) * 24 // 1000
        image = frames[index].to_image()
        with Image.open(BytesIO(sources[clip["source_asset_version_id"]])) as source:
            expected = source.convert("RGB").getpixel((source.width // 2, source.height // 2))
        observed = image.getpixel((image.width // 2, image.height // 4))
        assert max(abs(a - b) for a, b in zip(observed, expected, strict=True)) <= 8
    caption_counts = [
        sum(
            min(image.getpixel((x, y))) > 220
            for y in range(image.height // 2, image.height)
            for x in range(image.width)
        )
        for image in (frames[index].to_image() for index in (6, 18, 36, 54))
    ]
    assert all(count > 5 for count in caption_counts[:2])
    assert caption_counts[2:] == [0, 0]
    windows = [[], []]
    with av.open(BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            target = windows[0 if float(frame.pts * frame.time_base) < 0.8 else 1]
            timestamp = float(frame.pts * frame.time_base)
            if timestamp < 0.8 or timestamp > 1.25:
                assert frame.format.name == "fltp"
                for plane in frame.planes:
                    values = array("f")
                    values.frombytes(bytes(plane)[: frame.samples * 4])
                    target.extend(abs(value) for value in values)
    assert max(windows[0]) > 0.05 and max(windows[1]) < 0.001
