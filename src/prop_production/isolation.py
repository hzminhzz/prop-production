"""Fail-closed Linux Landlock + seccomp restrictions for credential-free workers.

Systemd identities remain the deployment boundary. This additional unprivileged
sandbox is tested separately and never substitutes for protected host ownership.
"""
from __future__ import annotations

import ctypes
import errno
import os
import platform
from pathlib import Path

READ_FILE = 1 << 2
READ_DIR = 1 << 3


class Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class Filter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class Program(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(Filter))]


def restrict_planner(writable_directories: list[Path], readable_paths: list[Path]) -> None:
    """Irreversible restriction of the current worker, before handling any input."""
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("BLOCKED_OS_ISOLATION")
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 3:
        raise RuntimeError("LANDLOCK_ABI_UNAVAILABLE")
    handled = (1 << 15) - 1
    ruleset = Ruleset(handled)
    fd = libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "LANDLOCK_RULESET")
    try:
        for path, writable in [(p, True) for p in writable_directories] + [(p, False) for p in readable_paths]:
            if path.is_symlink() or (writable and (not path.is_dir() or path.stat().st_mode & 0o077)):
                raise ValueError("UNSAFE_SANDBOX_PATH")
            directory = path.is_dir()
            allowed = handled if writable else (READ_FILE | (READ_DIR if directory else 0))
            path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(allowed, path_fd)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0) != 0:
                    raise OSError(ctypes.get_errno(), "LANDLOCK_PATH")
            finally:
                os.close(path_fd)
        if libc.prctl(38, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "NO_NEW_PRIVILEGES")
        if libc.syscall(446, fd, 0) != 0:
            raise OSError(ctypes.get_errno(), "LANDLOCK_RESTRICT")
    finally:
        os.close(fd)
    # seccomp_data: nr at 0, arch at 4, args[0] at 16. Restrict to native ABI.
    instructions = [
        Filter(0x20, 0, 0, 4), Filter(0x15, 1, 0, 0xC000003E),
        Filter(0x06, 0, 0, 0x80000000), Filter(0x20, 0, 0, 0),
        # x32 syscall ABI must not bypass native syscall checks.
        Filter(0x45, 0, 1, 0x40000000), Filter(0x06, 0, 0, 0x80000000),
    ]
    # No exec/ptrace/cross-process fd or memory access; no child to escape worker policy.
    for number in (59, 322, 101, 310, 311, 438, 56, 57, 58, 435, 321):
        instructions.extend([Filter(0x15, 0, 1, number), Filter(0x06, 0, 0, 0x00050000 | errno.EPERM)])
    instructions.extend([
        Filter(0x15, 0, 3, 41), Filter(0x20, 0, 0, 16),
        Filter(0x15, 1, 0, 1), Filter(0x06, 0, 0, 0x00050000 | errno.EPERM),
        Filter(0x06, 0, 0, 0x7FFF0000),
    ])
    filters = (Filter * len(instructions))(*instructions)
    program = Program(len(instructions), filters)
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "SECCOMP_RESTRICT")
