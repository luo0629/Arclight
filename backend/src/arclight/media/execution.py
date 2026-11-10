"""Run fixed, byte-only media operations with bounded pipes and deterministic cleanup."""

import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from threading import BoundedSemaphore, Event, Thread

from arclight.domain.file_policy import POLICIES

logger = logging.getLogger(__name__)
_slots = BoundedSemaphore(2)


@dataclass(frozen=True)
class MediaCommand:
    argument: str
    input_limit: int
    output_limit: int


FAILURES = frozenset(
    {
        "INVALID_MEDIA",
        "SOURCE_ENCODING",
        "SOURCE_NOT_TEXT",
        "SOURCE_EMPTY",
        "MEMORY_LIMIT",
        "SANDBOX_UNAVAILABLE",
    }
)


class MediaExecutionError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def command_for(operation: str, argument: str) -> MediaCommand:
    if operation == "probe" and argument in POLICIES:
        return MediaCommand(argument, POLICIES[argument].max_bytes, 4096)
    if operation == "story" and argument in {"utf-8", "gb18030"}:
        return MediaCommand(argument, 256 * 1024, 1024**2)
    if operation == "preview" and argument in {"encode", "finalize"}:
        return MediaCommand(argument, 1024**2, 1024**2)
    raise MediaExecutionError("INVALID_COMMAND")


def _stop(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        if process.poll() is None:
            process.kill()
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def _exchange(process: subprocess.Popen, data: bytes, limit: int, timeout: float) -> bytes:
    """Drain both pipes continuously; never accumulate unbounded stdout or stderr."""
    output, diagnostic = bytearray(), bytearray()
    exceeded = Event()
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    incoming = process.stdin

    def read(pipe, target, maximum):
        try:
            while chunk := pipe.read(4096):
                remaining = maximum - len(target)
                target.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    exceeded.set()
                    return
        finally:
            pipe.close()

    def write():
        try:
            incoming.write(data)
        except (BrokenPipeError, OSError):
            pass
        finally:
            with suppress(BrokenPipeError, OSError):
                incoming.close()

    threads = [
        Thread(target=read, args=(process.stdout, output, limit)),
        Thread(target=read, args=(process.stderr, diagnostic, 4096)),
        Thread(target=write),
    ]
    started_threads: list[Thread] = []
    deadline = time.monotonic() + timeout
    reason = None
    try:
        for thread in threads:
            try:
                thread.start()
            except RuntimeError:
                raise MediaExecutionError("UNAVAILABLE") from None
            started_threads.append(thread)
        while process.poll() is None:
            if exceeded.wait(min(0.01, max(0, deadline - time.monotonic()))):
                reason = "OUTPUT_LIMIT"
                break
            if time.monotonic() >= deadline:
                reason = "TIME_LIMIT"
                break
    finally:
        _stop(process)
        for thread in started_threads:
            thread.join()
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()
    if reason or exceeded.is_set():
        raise MediaExecutionError(reason or "OUTPUT_LIMIT")
    if process.returncode:
        # Only our fixed error tokens are retained. Native stderr is discarded.
        token = bytes(diagnostic).split(b"ARCLIGHT_MEDIA:")[-1].strip().decode("ascii", "ignore")
        reason = (
            token if token in FAILURES else "RESOURCE_LIMIT" if process.returncode < 0 else "FAILED"
        )
        raise MediaExecutionError(reason)
    if not output:
        raise MediaExecutionError("INVALID_OUTPUT")
    return bytes(output)


def execute_media(operation: str, argument: str, data: bytes, *, timeout: float = 20) -> bytes:
    """Only fixed operations and bytes cross the boundary; filenames and secrets never do."""
    command = command_for(operation, argument)
    if (
        not isinstance(data, bytes)
        or not 0 < len(data) <= command.input_limit
        or not 0 < timeout <= 600
    ):
        raise MediaExecutionError("INVALID_INPUT")
    if not _slots.acquire(timeout=3):
        raise MediaExecutionError("BUSY")
    started = time.monotonic()
    reason, cleaned = "SUCCESS", False
    try:
        environment = {
            key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ
        }
        environment.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"})
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW
        # Bytes travel through bounded pipes. No output file or writable media path is exposed.
        with tempfile.TemporaryDirectory(prefix="arclight-media-") as directory:
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-m",
                        "arclight.media.worker",
                        operation,
                        command.argument,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=directory,
                    env=environment,
                    close_fds=True,
                    start_new_session=sys.platform != "win32",
                    creationflags=creation_flags,
                )
                result = _exchange(process, data, command.output_limit, timeout)
            finally:
                if "process" in locals() and process.poll() is None:
                    _stop(process)
        cleaned = True
        return result
    except MediaExecutionError as exc:
        reason = exc.reason
        raise
    except OSError:
        reason = "UNAVAILABLE"
        raise MediaExecutionError(reason) from None
    finally:
        _slots.release()
        # TemporaryDirectory has already run, also on failure. Never log stderr or paths.
        if "directory" in locals():
            cleaned = not os.path.exists(directory)
        logger.info(
            "media_execution operation=%s outcome=%s elapsed_ms=%d cleanup=%s",
            operation,
            reason,
            int((time.monotonic() - started) * 1000),
            "complete" if cleaned else "failed",
        )
