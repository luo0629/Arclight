"""Real child-process bounds, cleanup, fixed commands and existing media integration."""

import base64
import json
import logging
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path

import av
import pytest
from PIL import Image

from arclight.application.rendering import render_phase
from arclight.core.errors import DomainError
from arclight.domain.story_import import decode_source
from arclight.media import execution
from arclight.media.execution import MediaExecutionError, execute_media
from arclight.media.validation import probe


@pytest.fixture
def recorded_children(monkeypatch, tmp_path):
    original_process = subprocess.Popen
    original_directory = execution.tempfile.TemporaryDirectory
    children, directories, invocations = [], [], []

    def directory(**kwargs):
        result = original_directory(dir=tmp_path, **kwargs)
        directories.append(Path(result.name))
        return result

    def capture(script=None):
        def start(command, **kwargs):
            invocations.append((command, kwargs))
            if script is not None:
                command = [sys.executable, "-I", "-B", "-c", script]
            process = original_process(command, **kwargs)
            children.append(process)
            return process

        monkeypatch.setattr(execution.subprocess, "Popen", start)

    monkeypatch.setattr(execution.tempfile, "TemporaryDirectory", directory)
    capture()
    yield capture, children, directories, invocations
    assert all(child.poll() is not None for child in children)
    assert all(not directory.exists() for directory in directories)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "operation,argument,data",
    [
        ("shell", "encode", b"data"),
        ("preview", "encode;whoami", b"data"),
        ("preview", "http://127.0.0.1/private", b"data"),
        ("preview", "../../output.mp4", b"data"),
        ("preview", "movie=/private/secret", b"data"),
        ("probe", "text/html", b"data"),
        ("probe", "image/png -filter_complex movie=secret", b"data"),
        ("story", "utf-7", b"data"),
        ("probe", "image/png", b""),
        ("probe", "image/png", bytes(1024**2 + 1)),
        ("preview", "encode", "not bytes"),
    ],
    ids=[
        "unknown-operation",
        "shell-suffix",
        "external-url",
        "output-path",
        "filter-path",
        "unsupported-format",
        "injected-format",
        "unsupported-encoding",
        "empty-input",
        "oversized-input",
        "non-bytes-input",
    ],
)
def test_invalid_command_paths_filters_and_input_never_start_a_child(
    monkeypatch, operation, argument, data
):
    def refuse(*_args, **_kwargs):
        pytest.fail("Rejected input must not create a process or workspace")

    monkeypatch.setattr(execution.subprocess, "Popen", refuse)
    monkeypatch.setattr(execution.tempfile, "TemporaryDirectory", refuse)
    with pytest.raises(MediaExecutionError):
        execute_media(operation, argument, data)


def test_parser_receives_only_fixed_arguments_clean_environment_and_pipes(
    recorded_children, monkeypatch, caplog
):
    capture, children, directories, invocations = recorded_children
    monkeypatch.setenv("ARCLIGHT_LLM_API_KEY", "PRIVATE_KEY_MARKER")
    monkeypatch.setenv("ARCLIGHT_DATABASE_URL", "PRIVATE_DATABASE_MARKER")
    caplog.set_level(logging.INFO, logger=execution.__name__)
    capture(
        "import json,os,sys; sys.stdin.buffer.read(); "
        "print(json.dumps({'environment': dict(os.environ), 'cwd_files': os.listdir('.')}))"
    )
    result = json.loads(execute_media("probe", "text/plain", b"PRIVATE_INPUT_MARKER"))
    assert result["cwd_files"] == []
    assert set(result["environment"]) <= {
        "SYSTEMROOT",
        "WINDIR",
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "LC_CTYPE",
    }
    assert all("PRIVATE" not in value for value in result["environment"].values())
    command, options = invocations[0]
    assert command == [
        sys.executable,
        "-I",
        "-B",
        "-m",
        "arclight.media.worker",
        "probe",
        "text/plain",
    ]
    assert options["close_fds"] is True
    assert not options.get("shell")
    assert all(options[name] == subprocess.PIPE for name in ("stdin", "stdout", "stderr"))
    assert children[0].poll() == 0 and not directories[0].exists()
    assert "outcome=SUCCESS" in caplog.text and "cleanup=complete" in caplog.text
    assert "PRIVATE" not in caplog.text and str(directories[0]) not in caplog.text


@pytest.mark.parametrize("pipe", ["stdout", "stderr"])
def test_output_flood_is_bounded_killed_and_cleaned(recorded_children, caplog, pipe):
    capture, children, directories, _ = recorded_children
    caplog.set_level(logging.INFO, logger=execution.__name__)
    capture(
        "import os,sys; sys.stdin.buffer.read(); "
        f"stream=sys.{pipe}.buffer; "
        "block=b'PRIVATE_NATIVE_DIAGNOSTIC'*4096\n"
        "while True: stream.write(block); stream.flush()"
    )
    started = time.monotonic()
    with pytest.raises(MediaExecutionError, match="OUTPUT_LIMIT"):
        execute_media("probe", "text/plain", b"data", timeout=10)
    assert time.monotonic() - started < 10
    assert children[0].poll() is not None and not directories[0].exists()
    assert "outcome=OUTPUT_LIMIT" in caplog.text and "cleanup=complete" in caplog.text
    assert "PRIVATE_NATIVE_DIAGNOSTIC" not in caplog.text


def test_timeout_reaps_child_and_cleans_before_next_success(recorded_children, caplog):
    capture, children, directories, _ = recorded_children
    caplog.set_level(logging.INFO, logger=execution.__name__)
    capture("import time; time.sleep(30)")
    with pytest.raises(MediaExecutionError, match="TIME_LIMIT"):
        execute_media("probe", "text/plain", b"data", timeout=0.05)
    assert children[0].poll() is not None and not directories[0].exists()
    assert "outcome=TIME_LIMIT" in caplog.text and "cleanup=complete" in caplog.text
    capture()
    assert probe(b"ordinary story", "text/plain")["characters"] == 14


def test_timeout_unblocks_a_full_input_pipe_and_joins_io_threads(recorded_children):
    capture, children, directories, _ = recorded_children
    capture("import time; time.sleep(30)")
    started = time.monotonic()
    with pytest.raises(MediaExecutionError, match="TIME_LIMIT"):
        execute_media("preview", "encode", bytes(1024**2), timeout=0.1)
    assert time.monotonic() - started < 5
    assert children[0].poll() is not None and not directories[0].exists()


def test_spawn_failure_is_cleaned_redacted_and_releases_admission(
    recorded_children, monkeypatch, caplog
):
    capture, _, directories, _ = recorded_children
    caplog.set_level(logging.INFO, logger=execution.__name__)

    def unavailable(*_args, **_kwargs):
        raise OSError("PRIVATE_NATIVE_ERROR /private/path")

    monkeypatch.setattr(execution.subprocess, "Popen", unavailable)
    with pytest.raises(MediaExecutionError, match="UNAVAILABLE"):
        execute_media("probe", "text/plain", b"data")
    assert not directories[0].exists()
    assert "outcome=UNAVAILABLE" in caplog.text and "cleanup=complete" in caplog.text
    assert "PRIVATE" not in caplog.text and "/private/path" not in caplog.text
    capture()
    assert probe(b"data", "text/plain")["characters"] == 4


def test_partial_io_thread_start_failure_reaps_child_and_releases_all_pipes(
    recorded_children, monkeypatch
):
    capture, children, directories, _ = recorded_children
    capture("import time; time.sleep(30)")
    original_start = execution.Thread.start
    started = []

    def start(thread):
        if started:
            raise RuntimeError("Cannot start I/O thread")
        original_start(thread)
        started.append(thread)

    monkeypatch.setattr(execution.Thread, "start", start)
    with pytest.raises(MediaExecutionError, match="UNAVAILABLE"):
        execute_media("probe", "text/plain", b"data")
    child = children[0]
    assert child.poll() is not None and not directories[0].exists()
    assert all(pipe.closed for pipe in (child.stdin, child.stdout, child.stderr))
    assert all(not thread.is_alive() for thread in started)
    monkeypatch.setattr(execution.Thread, "start", original_start)
    capture()
    assert probe(b"data", "text/plain")["characters"] == 4


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX process group cleanup")
def test_exited_leaders_descendants_cannot_keep_output_pipes_open(recorded_children):
    capture, _, _, _ = recorded_children
    capture(
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(30)']); "
        "print('DONE',flush=True)"
    )
    started = time.monotonic()
    assert execute_media("probe", "text/plain", b"data", timeout=2).strip() == b"DONE"
    assert time.monotonic() - started < 5


def test_native_error_is_redacted_and_only_fixed_diagnostic_tokens_survive(
    recorded_children, caplog
):
    capture, _, _, _ = recorded_children
    caplog.set_level(logging.INFO, logger=execution.__name__)
    capture(
        "import sys; sys.stdin.buffer.read(); "
        "sys.stderr.write('/private/path API_KEY=PRIVATE_KEY_MARKER\\n'); sys.exit(1)"
    )
    with pytest.raises(MediaExecutionError, match="FAILED"):
        execute_media("probe", "text/plain", b"PRIVATE_INPUT_MARKER")
    assert "PRIVATE" not in caplog.text and "/private/path" not in caplog.text
    capture(
        "import sys; sys.stdin.buffer.read(); "
        "sys.stderr.write('ARCLIGHT_MEDIA:MEMORY_LIMIT\\n'); sys.exit(1)"
    )
    with pytest.raises(MediaExecutionError, match="MEMORY_LIMIT"):
        execute_media("probe", "text/plain", b"data")
    assert "outcome=MEMORY_LIMIT" in caplog.text


def test_admission_is_bounded_and_releases_after_busy():
    assert execution._slots.acquire(blocking=False)
    assert execution._slots.acquire(blocking=False)
    try:
        with pytest.raises(MediaExecutionError, match="BUSY"):
            execute_media("probe", "text/plain", b"data")
    finally:
        execution._slots.release()
        execution._slots.release()
    assert probe(b"data", "text/plain")["characters"] == 4


@pytest.mark.parametrize(
    "reason,code,status",
    [
        ("SOURCE_ENCODING", "SOURCE_ENCODING", 422),
        ("SOURCE_NOT_TEXT", "SOURCE_NOT_TEXT", 422),
        ("SOURCE_EMPTY", "SOURCE_EMPTY", 422),
        ("BUSY", "VALIDATION_BUSY", 503),
        ("TIME_LIMIT", "SOURCE_PARSE_LIMIT", 422),
        ("SANDBOX_UNAVAILABLE", "SOURCE_PARSE_LIMIT", 422),
    ],
)
def test_story_child_failure_preserves_public_error_contract(monkeypatch, reason, code, status):
    from arclight.domain import story_import

    def failed(*_args, **_kwargs):
        raise MediaExecutionError(reason)

    monkeypatch.setattr(story_import, "execute_media", failed)
    with pytest.raises(DomainError) as exc:
        decode_source("story.txt", "text/plain", "utf-8", base64.b64encode(b"data").decode())
    assert (exc.value.code, exc.value.status_code) == (code, status)


def test_story_invalid_child_output_is_a_stable_safe_failure(monkeypatch):
    from arclight.domain import story_import

    monkeypatch.setattr(story_import, "execute_media", lambda *_args: b"\xff")
    with pytest.raises(DomainError) as exc:
        decode_source("story.txt", "text/plain", "utf-8", base64.b64encode(b"data").decode())
    assert exc.value.code == "SOURCE_PARSE_LIMIT"


def test_actual_ffmpeg_roundtrip_preserves_preview_and_refuses_non_mp4_and_external_input():
    image = BytesIO()
    Image.new("RGB", (16, 12), "red").save(image, format="PNG")
    clip = render_phase(image.getvalue(), "encode")
    result = render_phase(clip, "finalize")
    with av.open(BytesIO(result), format="mov") as movie:
        frames = list(movie.decode(video=0))
        assert len(frames) == 24
        assert (frames[0].width, frames[0].height) == (256, 256)
    for data in (
        b"#EXTM3U\nhttp://127.0.0.1/private\n",
        b"ffconcat version 1.0\nfile '/private/secret'\n",
        clip[: len(clip) // 2],
    ):
        with pytest.raises(DomainError) as exc:
            render_phase(data, "finalize")
        assert exc.value.code == "RENDER_FAILED"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows development Job Object enforcement")
def test_windows_job_refuses_a_second_process():
    script = (
        "import subprocess,sys; from arclight.media.limits import restrict_process; "
        "restrict_process()\n"
        "try: subprocess.Popen([sys.executable,'-I','-c','pass'])\n"
        "except OSError: print('PROCESS_DENIED')\n"
        "else: raise AssertionError('Job must deny an additional process')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script], capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == b"PROCESS_DENIED"
