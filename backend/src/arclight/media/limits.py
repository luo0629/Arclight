"""Apply limits before importing native parsers or reading untrusted bytes."""

import sys

MEMORY_BYTES = 512 * 1024**2
CPU_SECONDS = 12


def restrict_process() -> None:
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024**2, 1024**2))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        return

    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("minimum_working_set", ctypes.c_size_t),
            ("maximum_working_set", ctypes.c_size_t),
            ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "read_operations",
                "write_operations",
                "other_operations",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits),
            ("io", IoCounters),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    # Per-process CPU, active-process count, per-process memory, kill on job close.
    limits.basic.flags = 0x2 | 0x8 | 0x100 | 0x2000
    limits.basic.process_time = CPU_SECONDS * 10_000_000
    limits.basic.active_processes = 1
    limits.process_memory = MEMORY_BYTES
    if (
        not job
        or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
        or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())
    ):
        raise OSError("Cannot apply media parser resource limits")
    # Keep this handle open until process exit, then Windows releases the job.
