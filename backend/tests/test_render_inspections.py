"""Real FFmpeg decoding, exact profile checks and bounded safe failure evidence."""

import hashlib
import json
import subprocess
import sys
import textwrap

import pytest
from render_inspection_helpers import movie
from test_render_encoder import profile

from arclight.application.render_inspections import inspect_candidate
from arclight.media.execution import MediaExecutionError, command_for, execute_media
from arclight.media.render_plan import MAX_OUTPUT_BYTES


def inspect(data, duration_ms=500, **changes):
    return inspect_candidate(data, profile(**changes), duration_ms, observer=lambda _: None)


def test_real_output_inspection_records_exact_bytes_full_decoding_and_profile():
    data = movie()
    result = inspect(data)
    assert result.passed and result.error_code is None, result
    assert result.output_sha256 == hashlib.sha256(data).hexdigest()
    assert result.byte_size == len(data)
    assert result.measurement.video.frames == 12
    assert result.measurement.audio.samples >= 24000
    assert result.measurement.video.duration_us == 500_000
    assert all(item.passed for item in result.checks)


def test_cold_worker_inspects_without_loading_application_modules_after_sealing():
    script = textwrap.dedent(
        """
        import importlib.abc
        import sys
        from arclight.media import isolation, worker

        class SealedApplicationImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if fullname.startswith('arclight.domain.'):
                    raise PermissionError('Application source is outside the sealed runtime')

        def seal():
            isolation.seal_process()
            sys.meta_path.insert(0, SealedApplicationImports())

        worker.seal_process = seal
        sys.argv = ['worker', 'inspect', 'render']
        worker.main()
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        input=movie(),
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    measurement = json.loads(result.stdout)
    assert measurement["decodable"] is True
    assert measurement["video"]["frames"] == 12
    assert measurement["audio"]["samples"] >= 24000


@pytest.mark.parametrize("fps,duration_ms", [(1, 1), (24, 501), (120, 250)])
def test_inspection_accepts_only_one_frame_rounding_and_aac_padding(fps, duration_ms):
    frames = (duration_ms * fps + 999) // 1000
    result = inspect(movie(fps=fps, frames=frames), duration_ms, fps=fps)
    assert result.passed, result


def test_render_check_accepts_long_output_without_loosening_upload_limit():
    data = movie(fps=1, frames=31)
    assert inspect(data, 31_000, fps=1).passed
    with pytest.raises(MediaExecutionError) as failure:
        execute_media("probe", "video/mp4", data)
    assert failure.value.reason == "INVALID_MEDIA"


@pytest.mark.parametrize(
    "changes,failed",
    [
        ({"width": 128}, "resolution"),
        ({"height": 128}, "resolution"),
        ({"fps": 12}, "frame_rate"),
        ({"video_codec": "mpeg4"}, "video_codec"),
        ({"pixel_format": "yuv444p"}, "pixel_format"),
        ({"sample_rate": 44100}, "audio_format"),
        ({"layout": "mono"}, "audio_format"),
        ({"audio": False}, "tracks"),
        ({"video": False}, "tracks"),
        ({"audio_tracks": 2}, "tracks"),
        ({"frames": 11}, "duration"),
        ({"frames": 13}, "duration"),
        ({"audio_frames": 6}, "duration"),
        ({"video_offset": 2}, "frame_rate"),
        ({"audio_offset": 2048}, "duration"),
    ],
)
def test_real_media_must_match_every_frozen_output_requirement(changes, failed):
    result = inspect(movie(**changes))
    assert not result.passed and result.error_code
    assert not next(item.passed for item in result.checks if item.name == failed)


@pytest.mark.parametrize("damage", ["garbage", "truncate", "corrupt_payload", "non_aac"])
def test_invalid_encoding_and_broken_packets_fail_safely(damage):
    data = movie()
    if damage == "garbage":
        data = b"PRIVATE_NATIVE_ERROR:/private/source?signature=secret"
    elif damage == "truncate":
        data = data[:-40]
    elif damage == "corrupt_payload":
        start = data.index(b"mdat") + 4
        end = data.index(b"moov") - 4
        data = data[:start] + bytes(end - start) + data[end:]
    else:
        data = movie(audio_codec="alac")
    result = inspect(data)
    assert not result.passed and result.error_code == "RENDER_INSPECTION_INVALID_MEDIA"
    assert result.measurement is None and all(not c.passed for c in result.checks)
    assert "PRIVATE_NATIVE_ERROR" not in result.model_dump_json()


def test_inspection_uses_fixed_byte_limits_and_no_client_arguments():
    command = command_for("inspect", "render")
    assert command.input_limit == MAX_OUTPUT_BYTES and command.output_limit == 4096
    for argument in ("../../private.mp4", "ffprobe -i private.mp4", "http://localhost/input"):
        with pytest.raises(MediaExecutionError, match="INVALID_COMMAND"):
            execute_media("inspect", argument, b"x")


@pytest.mark.parametrize(
    "reason",
    [
        "TIME_LIMIT",
        "MEMORY_LIMIT",
        "RESOURCE_LIMIT",
        "SANDBOX_UNAVAILABLE",
        "PRIVATE_NATIVE:/private",
    ],
)
def test_inspection_preserves_only_allowlisted_child_failures(monkeypatch, reason):
    def fail(*args, **kwargs):
        raise MediaExecutionError(reason)

    monkeypatch.setattr("arclight.application.render_inspections.execute_media", fail)
    result = inspect(b"x")
    assert not result.passed and result.measurement is None
    assert (
        result.error_code
        == f"RENDER_INSPECTION_{reason if reason != 'PRIVATE_NATIVE:/private' else 'FAILED'}"
    )
    assert "PRIVATE_NATIVE" not in result.model_dump_json()
