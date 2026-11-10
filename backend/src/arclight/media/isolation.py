"""Linux kernel boundary: read-only runtime, no user files, sockets or new processes.

Windows development uses the existing Job Object plus the byte-only parser
contract. It does not provide Linux Landlock/seccomp's kernel access controls.
"""

import ctypes
import errno
import os
import platform
import sys
import sysconfig
from pathlib import Path


class IsolationUnavailable(OSError):
    pass


def seal_process() -> None:
    if sys.platform == "win32":
        return
    if sys.platform != "linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise IsolationUnavailable("Unsupported media sandbox platform")
    try:
        _restrict_files()
        _restrict_syscalls()
    except OSError as exc:
        raise IsolationUnavailable("Cannot enforce media sandbox") from exc


def _restrict_files() -> None:
    """Landlock ABI >= 3 is mandatory; missing enforcement fails closed."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(444, None, 0, 1)
    if abi < 3:
        raise IsolationUnavailable("Landlock ABI 3 is required")
    handled = ctypes.c_uint64((1 << 15) - 1)
    ruleset = libc.syscall(444, ctypes.byref(handled), ctypes.sizeof(handled), 0)
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "Cannot create filesystem policy")

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    # Do not grant the include/scripts/data prefixes (which can include /usr or a project).
    runtime = {
        Path(sysconfig.get_path(name)).resolve()
        for name in ("stdlib", "platstdlib", "purelib", "platlib")
    }
    runtime.add(Path(__file__).resolve().parent)
    runtime.update(Path(p).resolve() for p in ("/lib", "/usr/lib"))
    runtime.update(Path(p) for p in ("/etc/ld.so.cache", "/etc/localtime", "/dev/urandom"))
    try:
        for path in sorted(runtime):
            if not path.exists():
                continue
            descriptor = os.open(path, 0x200000 | 0x80000)  # Linux O_PATH | O_CLOEXEC
            try:
                rule = PathRule(4 | (8 if path.is_dir() else 0), descriptor)
                if libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0):
                    raise OSError(ctypes.get_errno(), "Cannot add runtime rule")
            finally:
                os.close(descriptor)
        if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, ruleset, 0):
            raise OSError(ctypes.get_errno(), "Cannot enforce filesystem policy")
    finally:
        os.close(ruleset)


def _restrict_syscalls() -> None:
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    library.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]
    library.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise OSError("Cannot create syscall policy")
    try:
        deny = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO
        blocked = (
            "socket",
            "socketpair",
            "connect",
            "bind",
            "listen",
            "accept",
            "accept4",
            "fork",
            "vfork",
            "execve",
            "execveat",
            "ptrace",
            "process_vm_readv",
            "process_vm_writev",
            "pidfd_open",
            "pidfd_getfd",
            "pidfd_send_signal",
            "kill",
            "tkill",
            "unshare",
            "setns",
            "mount",
            "umount2",
            "pivot_root",
            "open_by_handle_at",
            "name_to_handle_at",
            "bpf",
            "userfaultfd",
            "io_uring_setup",
            "io_uring_enter",
            "io_uring_register",
        )
        for name in blocked:
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number >= 0 and library.seccomp_rule_add(context, deny, number, 0):
                raise OSError("Cannot add denied syscall")
        # pthread falls back to clone when clone3 is unavailable. Only same-process threads survive.
        number = library.seccomp_syscall_resolve_name(b"clone3")
        if number >= 0 and library.seccomp_rule_add(context, 0x00050000 | errno.ENOSYS, number, 0):
            raise OSError("Cannot restrict clone3")

        class Argument(ctypes.Structure):
            _fields_ = [
                ("arg", ctypes.c_uint),
                ("op", ctypes.c_int),
                ("mask", ctypes.c_uint64),
                ("value", ctypes.c_uint64),
            ]

        argument = Argument(0, 7, 0x10000, 0)  # SCMP_CMP_MASKED_EQ: no CLONE_THREAD
        number = library.seccomp_syscall_resolve_name(b"clone")
        if number >= 0 and library.seccomp_rule_add_array(
            context, deny, number, 1, ctypes.byref(argument)
        ):
            raise OSError("Cannot restrict process creation")
        if library.seccomp_attr_set(context, 4, 1) or library.seccomp_load(context):
            raise OSError("Cannot enforce synchronized syscall policy")
    finally:
        library.seccomp_release(context)
