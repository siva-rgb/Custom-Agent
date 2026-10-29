"""FR-151 (M41): who owns a workspace, and whether that owner is provably gone.

Every container and volume a Runner creates carries `agentsdk.owner`, `hostname:pid:start`: the
host, the process id and that process's start time, so a pid the system has since reused names a
different owner (DECISION-825b81a6). A Runner reaps only what `is_dead` says is dead, and that is
said only when it is certain: the label names this host, and its process is gone, has exited, or
started at another time. Anything else -- another host, a label this module cannot read, a
process whose start time it may not read -- counts as alive, and is never touched.

The start time is read with the standard library alone (NFR-40): `GetProcessTimes` through ctypes
on Windows, `/proc/<pid>/stat` on Linux, and `ps` elsewhere.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys

_GONE = object()  # the process does not exist, or has exited
_UNKNOWN = object()  # whether it runs, or when it started, cannot be read


def _windows_start(pid: int) -> object:
    import ctypes
    import ctypes.wintypes as wt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wt.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        # ERROR_INVALID_PARAMETER: no such process. Anything else, access denied above all, says
        # nothing about whether it runs (KNOWLEDGE-87741d74).
        return _GONE if ctypes.get_last_error() == 87 else _UNKNOWN
    try:
        code = wt.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return _UNKNOWN
        if code.value != 259:  # STILL_ACTIVE: an exited process opens while a handle on it is held
            return _GONE
        created, exited, kernel, user = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel),
                                        ctypes.byref(user)):
            return _UNKNOWN
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


def _linux_start(pid: int) -> object:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as stat:
            fields = stat.read().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return _GONE
    except OSError:
        return _UNKNOWN
    # After the command's closing parenthesis: state is field 3 and starttime field 22.
    if len(fields) < 20:
        return _UNKNOWN
    return _GONE if fields[0] in ("Z", "X") else fields[19]


def _ps_start(pid: int) -> object:
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return _UNKNOWN
    text = done.stdout.strip()
    if done.returncode == 1 and not text:
        return _GONE
    return "-".join(text.split()) if done.returncode == 0 and text else _UNKNOWN


def _start(pid: int) -> object:
    """The process's start time as text, `_GONE` or `_UNKNOWN`."""
    if pid <= 0:
        return _UNKNOWN
    try:
        if sys.platform == "win32":
            return _windows_start(pid)
        if sys.platform.startswith("linux"):
            return _linux_start(pid)
        return _ps_start(pid)
    except Exception:  # noqa: BLE001 - whatever reading it raised, nothing is known
        return _UNKNOWN


_OWNER: str | None = None


def this_owner() -> str:
    """This process's owner label. A start time that cannot be read is written `?`, which no
    reading ever matches, so such a process is reaped only once its pid is gone."""
    global _OWNER
    if _OWNER is None:
        start = _start(os.getpid())
        _OWNER = f"{socket.gethostname()}:{os.getpid()}:{start if isinstance(start, str) else '?'}"
    return _OWNER


def is_dead(label: str) -> bool:
    """True only when `label` names this host and its process is provably not the one running:
    gone, exited, or started at another time."""
    try:
        host, pid_text, start = str(label).rsplit(":", 2)
    except ValueError:
        return False
    # ASCII digits only: str.isdigit() admits '²', which int() refuses (M41 pre-review).
    if host != socket.gethostname() or not (pid_text.isascii() and pid_text.isdigit()) or not start:
        return False
    found = _start(int(pid_text))
    if found is _GONE:
        return True
    if found is _UNKNOWN or start == "?":
        return False
    return found != start
