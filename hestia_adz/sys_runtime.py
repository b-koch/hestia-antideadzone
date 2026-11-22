"""Linux runtime helpers: inotify config watcher, systemd sd_notify, monotonic clocks, RT priority."""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import gc
import os
import socket
import struct
from pathlib import Path

EVIOCSCLOCKID = 0x400445A0
CLOCK_MONOTONIC = 1

IN_NONBLOCK = 0o0004000
IN_CLOEXEC = 0o2000000
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_MOVED_FROM = 0x00000040
IN_CLOSE_WRITE = 0x00000008
IN_Q_OVERFLOW = 0x00004000

_INOTIFY_HEADER = struct.Struct("iIII")


def set_monotonic_clock(fd: int) -> bool:
    """Configure an evdev file descriptor to stamp input_events with CLOCK_MONOTONIC."""
    try:
        buf = struct.pack("i", CLOCK_MONOTONIC)
        fcntl.ioctl(fd, EVIOCSCLOCKID, buf)
        return True
    except OSError:
        return False


def sd_notify(state: str) -> bool:
    """Send a systemd readiness/status datagram via $NOTIFY_SOCKET (no-op if unset)."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(state.encode("utf-8"), addr)
        return True
    except OSError:
        return False


def apply_realtime_priority(enabled: bool, nice_value: int = -10) -> str:
    """Attempt low-jitter scheduling priority when enabled (best-effort, non-fatal)."""
    if not enabled:
        return "default"
    try:
        sched_rr = getattr(os, "SCHED_RR", 2)
        param = os.sched_param(10)
        os.sched_setscheduler(0, sched_rr, param)
        return "SCHED_RR(10)"
    except (AttributeError, OSError, PermissionError):
        pass
    try:
        os.nice(nice_value)
        return f"nice({nice_value})"
    except OSError:
        return "default"


class StreamingGCGuard:
    """Disable cyclic garbage collection while actively streaming frames to avoid micro-stutters."""

    def __init__(self) -> None:
        self._suppressed = False

    def enter_streaming(self) -> None:
        if not self._suppressed and gc.isenabled():
            gc.collect()
            gc.disable()
            self._suppressed = True

    def leave_streaming(self) -> None:
        if self._suppressed:
            gc.enable()
            gc.collect()
            self._suppressed = False


class ConfigInotifyWatcher:
    """Zero-polling Linux inotify watcher on the config file's parent directory."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.target_name = config_path.name
        self.fd: int = -1
        self._wd: int = -1
        self._libc = None

        libc_name = ctypes.util.find_library("c") or "libc.so.6"
        libc = ctypes.CDLL(libc_name, use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        libc.inotify_rm_watch.restype = ctypes.c_int
        self._libc = libc

        fd = libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno))
        self.fd = fd
        self.rewatch()

    def rewatch(self) -> bool:
        if self.fd < 0 or self._libc is None:
            return False
        parent = self.config_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if self._wd >= 0:
            self._libc.inotify_rm_watch(self.fd, self._wd)
            self._wd = -1
        mask = IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_MOVED_FROM
        wd = self._libc.inotify_add_watch(self.fd, os.fsencode(parent), mask)
        if wd < 0:
            return False
        self._wd = wd
        return True

    def drain_changed(self) -> bool:
        """Drain pending inotify events and return True if target config file was modified."""
        if self.fd < 0:
            return False
        changed = False
        while True:
            try:
                buf = os.read(self.fd, 4096)
            except BlockingIOError:
                break
            except OSError:
                break
            if not buf:
                break
            offset = 0
            n = len(buf)
            while offset + _INOTIFY_HEADER.size <= n:
                _wd, mask, _cookie, name_len = _INOTIFY_HEADER.unpack_from(buf, offset)
                offset += _INOTIFY_HEADER.size
                raw_name = buf[offset : offset + name_len].rstrip(b"\x00")
                offset += name_len
                if mask & IN_Q_OVERFLOW:
                    changed = True
                elif not raw_name:
                    changed = True
                else:
                    try:
                        fname = os.fsdecode(raw_name)
                    except UnicodeDecodeError:
                        fname = ""
                    if fname == self.target_name:
                        changed = True
        return changed

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
            self._wd = -1
