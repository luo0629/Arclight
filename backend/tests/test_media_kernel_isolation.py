"""Exercise the Linux deployment's real kernel restrictions, also as uid 0."""

import json
import signal
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux Landlock/seccomp deployment")


@pytest.mark.parametrize(
    "operation",
    [
        "open(secret).read()",
        "os.open(secret, os.O_RDONLY)",
        "open('/proc/self/environ', 'rb').read()",
        "open('disk-fill', 'wb').write(bytes(1024))",
        "os.mkdir('new-directory')",
        "socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
        "socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)",
        "socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)",
        "os.fork()",
        "os.execv(sys.executable, [sys.executable, '-c', 'pass'])",
        "subprocess.Popen([sys.executable, '-c', 'pass'])",
    ],
)
def test_kernel_denies_private_files_disk_network_and_processes(tmp_path, operation):
    secret = tmp_path / "private-data"
    secret.write_text("PRIVATE_CREDENTIAL_MARKER", encoding="utf-8")
    script = textwrap.dedent(
        f"""
        import os,socket,subprocess,sys
        from arclight.media.limits import restrict_process
        from arclight.media.isolation import seal_process
        secret={str(secret)!r}
        restrict_process()
        seal_process()
        try:
            {operation}
        except PermissionError:
            print('KERNEL_DENIED')
        else:
            raise AssertionError('Kernel must refuse this operation')
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == b"KERNEL_DENIED"
    assert secret.read_text(encoding="utf-8") == "PRIVATE_CREDENTIAL_MARKER"
    assert {p.name for p in tmp_path.iterdir()} == {"private-data"}


def test_missing_kernel_policy_fails_closed_before_untrusted_work():
    script = textwrap.dedent(
        """
        from arclight.media import isolation
        isolation._restrict_files = lambda: (_ for _ in ()).throw(OSError('Unavailable'))
        try:
            isolation.seal_process()
        except isolation.IsolationUnavailable:
            print('REFUSED')
        else:
            raise AssertionError('No weak fallback is permitted')
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script], capture_output=True, timeout=10
    )
    assert result.returncode == 0 and result.stdout.strip() == b"REFUSED", result.stderr


def test_real_cpu_limit_terminates_infinite_worker():
    script = (
        "from arclight.media import limits; limits.CPU_SECONDS=1; limits.restrict_process(); "
        "from arclight.media.isolation import seal_process; seal_process()\n"
        "while True: pass"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script], capture_output=True, timeout=10
    )
    assert result.returncode == -signal.SIGKILL


def test_file_size_limit_bounds_even_a_preopened_regular_output(tmp_path):
    path = tmp_path / "bounded-output"
    script = (
        "import os; from arclight.media.limits import restrict_process; restrict_process(); "
        "os.write(1, bytes(2*1024**2)); os.write(1,b'excess')"
    )
    with path.open("wb") as output:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script],
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    assert result.returncode != 0
    assert path.stat().st_size <= 1024**2


def test_resource_settings_apply_in_the_real_child():
    script = (
        "import json,resource; from arclight.media.limits import restrict_process; "
        "restrict_process(); print(json.dumps([resource.getrlimit(x) for x in "
        "(resource.RLIMIT_AS,resource.RLIMIT_CPU,resource.RLIMIT_FSIZE,resource.RLIMIT_NPROC,"
        "resource.RLIMIT_NOFILE,resource.RLIMIT_CORE)]))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script], capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        [512 * 1024**2] * 2,
        [12] * 2,
        [1024**2] * 2,
        [64] * 2,
        [64] * 2,
        [0] * 2,
    ]
