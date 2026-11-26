"""Core event-driven input shaping daemon with zero-allocation struct loop & persistent uinput."""

from __future__ import annotations

import errno
import fcntl
import functools
import logging
import os
import selectors
import signal
import time
from pathlib import Path
from typing import Any

from evdev import InputDevice, ecodes, list_devices

from .config import (
    PID_FILE_PATH,
    CompiledProfile,
    compile_active_profile,
    load_raw_config,
    resolve_config_path,
    validate_config_dict,
)
from .sys_runtime import (
    ConfigInotifyWatcher,
    StreamingGCGuard,
    apply_realtime_priority,
    sd_notify,
    set_monotonic_clock,
)
from .telemetry import LatencyRecorder
from .virtual_device import (
    DPAD_BUTTON_CODES,
    EVENT_SIZE,
    INPUT_EVENT_STRUCT,
    VIRTUAL_BUTTONS,
    XBOX360_NAME,
    XBOX360_VENDOR,
    AxisRange,
    PersistentXboxReceiver,
)

LOG = logging.getLogger("adz.daemon")

PASSTHROUGH_BUTTONS = frozenset(VIRTUAL_BUTTONS)
STARTUP_RETRY_INTERVAL = 0.25
STARTUP_RETRY_WINDOW_S = 10.0


def _sysfs_input_name(event_path: str) -> str:
    """Read /sys/class/input/eventX/device/name without needing /dev/input/eventX ACLs."""
    try:
        ev_name = os.path.basename(event_path)
        sys_name = Path("/sys/class/input") / ev_name / "device" / "name"
        return sys_name.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def is_physical_gamepad(dev: InputDevice) -> bool:
    if dev.name == XBOX360_NAME and dev.info.vendor == XBOX360_VENDOR:
        return False
    caps = dev.capabilities()
    abs_entries = caps.get(ecodes.EV_ABS, [])
    abs_codes = {code for code, _ in abs_entries}
    key_codes = set(caps.get(ecodes.EV_KEY, []))
    has_sticks = ecodes.ABS_X in abs_codes and ecodes.ABS_Y in abs_codes
    has_gamepad_btn = bool(key_codes & {ecodes.BTN_SOUTH, ecodes.BTN_GAMEPAD, ecodes.BTN_START})
    return has_sticks and has_gamepad_btn


class AntiDeadzoneDaemon:
    def __init__(
        self,
        *,
        config_path: Path | None = None,
        start_paused: bool = False,
        live_latency: bool = False,
        realtime: bool = False,
    ) -> None:
        self.config_path = resolve_config_path(config_path)
        self._force_start_paused = start_paused
        self._live_latency = live_latency
        self._cli_realtime = realtime

        self.running = True
        self.paused = False
        self.device_name_match = ""
        self.reload_interval = 1.0
        self.rescan_interval = 1.0
        self.debug_sample_rate = 0
        self.profile: CompiledProfile | None = None

        self.selector = selectors.DefaultSelector()
        self.vdev = PersistentXboxReceiver()
        self.gc_guard = StreamingGCGuard()
        self.latency_recorder = LatencyRecorder(capacity=2000)

        self.phys_dev: InputDevice | None = None
        self._axis_ranges: dict[int, AxisRange] = {}
        self._raw_lx = 0
        self._raw_ly = 0
        self._raw_rx = 0
        self._raw_ry = 0
        self._raw_lt = 0
        self._raw_rt = 0
        self._sticks_dirty = False
        self._last_stick_emit_ns = 0
        self._sample_counter = 0

        self._reload_requested = False
        self._last_config_mtime = 0.0
        self._last_latency_flush = time.monotonic()
        self._last_rescan_ts = 0.0
        self._attach_burst_until = 0.0
        self._last_blocked_log_ts = 0.0

        self.inotify: ConfigInotifyWatcher | None = None
        self.udev_monitor = None
        self._lock_fd: int | None = None
        self._wakeup_r: int = -1
        self._wakeup_w: int = -1

    def _effective_rescan_interval(self, now: float) -> float:
        if now < self._attach_burst_until:
            return min(self.rescan_interval, STARTUP_RETRY_INTERVAL)
        return self.rescan_interval

    def _acquire_instance_lock(self) -> bool:
        lock_path = PID_FILE_PATH.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                existing = os.read(fd, 64).decode("utf-8", errors="replace").strip()
            except OSError:
                existing = "unknown"
            os.close(fd)
            LOG.error("Another adz daemon is already running (PID %s); exiting.", existing or "unknown")
            return False
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        self._lock_fd = fd
        return True

    def _setup_signals(self) -> None:
        r_fd, w_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self._wakeup_r = r_fd
        self._wakeup_w = w_fd
        signal.set_wakeup_fd(w_fd, warn_on_full_buffer=False)
        self.selector.register(r_fd, selectors.EVENT_READ, data="signal")
        signal.signal(signal.SIGINT, self._on_stop_signal)
        signal.signal(signal.SIGTERM, self._on_stop_signal)
        signal.signal(signal.SIGHUP, self._on_sighup)
        signal.signal(signal.SIGUSR2, self._on_sigusr2)

    def _on_stop_signal(self, _signum: int, _frame: Any) -> None:
        self.running = False

    def _on_sighup(self, _signum: int, _frame: Any) -> None:
        self._reload_requested = True

    def _on_sigusr2(self, _signum: int, _frame: Any) -> None:
        self._live_latency = not self._live_latency
        self.latency_recorder.clear()
        self._last_latency_flush = time.monotonic()
        LOG.info("Live latency telemetry %s", "ENABLED" if self._live_latency else "DISABLED")

    def _setup_watchers(self) -> None:
        try:
            self.inotify = ConfigInotifyWatcher(self.config_path)
            self.selector.register(self.inotify.fd, selectors.EVENT_READ, data="inotify")
            LOG.info("Watching %s via inotify", self.config_path)
        except OSError as exc:
            LOG.warning("inotify unavailable (%s); falling back to %.1fs polling", exc, self.reload_interval)

        try:
            import pyudev

            ctx = pyudev.Context()
            mon = pyudev.Monitor.from_netlink(ctx)
            mon.filter_by(subsystem="input")
            mon.start()
            self.udev_monitor = mon
            self.selector.register(mon.fileno(), selectors.EVENT_READ, data="udev")
            LOG.info("Event-driven pyudev hotplug monitor active (with %.1fs fallback rescan)", self.rescan_interval)
        except Exception as exc:
            LOG.warning("pyudev unavailable (%s); falling back to %.1fs device rescan", exc, self.rescan_interval)

    def _notify_status(self) -> None:
        if self.paused:
            sd_notify("READY=1\nSTATUS=paused (bypass active)")
        else:
            pname = self.profile.name if self.profile else "unknown"
            dev_state = self.phys_dev.name if self.phys_dev else "waiting for controller"
            sd_notify(f"READY=1\nSTATUS=active: {pname} ({dev_state})")

    def load_configuration(self, initial: bool = False) -> None:
        sd_notify("RELOADING=1")
        try:
            raw = load_raw_config(self.config_path)
            report = validate_config_dict(raw)
            for warn in report.warnings:
                LOG.warning("Config warning: %s", warn)
            if not report.is_valid:
                for err in report.errors:
                    LOG.error("Config error: %s", err)
                if initial:
                    raise ValueError("Invalid initial configuration")
                self._notify_status()
                return

            new_profile = compile_active_profile(raw)
            new_paused = True if (initial and self._force_start_paused) else bool(raw.get("paused", False))
            new_match = str(raw.get("device_name_match", "")).strip()
            self.reload_interval = float(raw.get("reload_interval", 1.0))
            self.rescan_interval = float(raw.get("rescan_interval", 1.0))
            self.debug_sample_rate = int(raw.get("debug_sample_rate", 0))

            if initial:
                sched = apply_realtime_priority(self._cli_realtime or bool(raw.get("realtime_priority", False)))
                LOG.info("Process scheduling mode: %s", sched)

            match_changed = new_match.lower() != self.device_name_match.lower()
            self.device_name_match = new_match
            self.profile = new_profile

            if self.config_path.exists():
                self._last_config_mtime = self.config_path.stat().st_mtime

            if new_paused != self.paused or initial:
                self.paused = new_paused
                if self.paused:
                    self._enter_paused_mode()
                else:
                    self._enter_active_mode()
            elif not self.paused and match_changed:
                self._detach_physical_device()
                self._attach_burst_until = time.monotonic() + STARTUP_RETRY_WINDOW_S
                self._scan_and_attach_physical()

            LOG.info(
                "Loaded config [state=%s, profile=%s, max_hz=%g]",
                "paused" if self.paused else "active",
                self.profile.name,
                self.profile.max_output_hz,
            )
        except Exception as exc:
            LOG.error("Failed to reload configuration: %s", exc)
            if initial:
                raise
        finally:
            self._notify_status()

    def _enter_paused_mode(self) -> None:
        self._detach_physical_device()
        if self.vdev.is_open:
            try:
                self.selector.unregister(self.vdev.fd)
            except KeyError:
                pass
            self.vdev.close()
        self.gc_guard.leave_streaming()
        LOG.info("Entered PAUSED mode (physical controller released, virtual device removed)")

    def _enter_active_mode(self) -> None:
        if not self.vdev.is_open:
            self.vdev.open()
            self.selector.register(self.vdev.fd, selectors.EVENT_READ, data="uinput")
        if self.phys_dev is None:
            self._attach_burst_until = time.monotonic() + STARTUP_RETRY_WINDOW_S
            self._scan_and_attach_physical()

    def _detach_physical_device(self) -> None:
        if self.phys_dev is None:
            return
        dev = self.phys_dev
        self.phys_dev = None
        try:
            self.selector.unregister(dev.fd)
        except (KeyError, ValueError, OSError):
            pass
        self.vdev.detach_physical_ff(dev)
        try:
            dev.ungrab()
        except OSError:
            pass
        try:
            dev.close()
        except OSError:
            pass
        self._axis_ranges.clear()
        self._raw_lx = self._raw_ly = self._raw_rx = self._raw_ry = 0
        self._raw_lt = self._raw_rt = 0
        self._sticks_dirty = False
        if self.vdev.is_open:
            self.vdev.reset_neutral()
        self.gc_guard.leave_streaming()
        LOG.info("Detached physical controller; virtual receiver remains active")

    def _scan_and_attach_physical(self) -> None:
        if self.paused or self.phys_dev is not None:
            return
        now = time.monotonic()
        self._last_rescan_ts = now
        match_sub = self.device_name_match.lower()
        blocked_candidates: list[str] = []

        for path in sorted(list_devices()):
            try:
                dev = InputDevice(path)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EPERM):
                    sys_name = _sysfs_input_name(path)
                    if (
                        sys_name
                        and sys_name != XBOX360_NAME
                        and (not match