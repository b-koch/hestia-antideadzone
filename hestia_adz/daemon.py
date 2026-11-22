"""Core event-driven input shaping daemon with zero-allocation struct loop & persistent uinput."""

from __future__ import annotations

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

        self.inotify: ConfigInotifyWatcher | None = None
        self.udev_monitor = None
        self._lock_fd: int | None = None
        self._wakeup_r: int = -1
        self._wakeup_w: int = -1

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
            LOG.info("Event-driven pyudev hotplug monitor active")
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
        match_sub = self.device_name_match.lower()
        for path in sorted(list_devices()):
            try:
                dev = InputDevice(path)
            except OSError:
                continue
            if not is_physical_gamepad(dev):
                dev.close()
                continue
            if match_sub and match_sub not in dev.name.lower():
                dev.close()
                continue

            try:
                dev.grab()
            except OSError as exc:
                LOG.warning("Could not grab %s (%s): %s", dev.path, dev.name, exc)
                dev.close()
                continue

            set_monotonic_clock(dev.fd)
            os.set_blocking(dev.fd, False)

            caps = dev.capabilities()
            self._axis_ranges.clear()
            for code, info in caps.get(ecodes.EV_ABS, []):
                self._axis_ranges[code] = AxisRange(minimum=int(info.min), maximum=int(info.max))

            self.phys_dev = dev
            self.selector.register(dev.fd, selectors.EVENT_READ, data="phys")
            self.vdev.reset_neutral()
            self.vdev.attach_physical_ff(dev)
            self.gc_guard.enter_streaming()
            LOG.info("Grabbed physical gamepad %r (%s)", dev.name, dev.path)
            self._notify_status()
            return

    def _normalize_stick_axis(self, code: int, raw_val: int) -> float:
        rng = self._axis_ranges.get(code)
        if rng is None:
            return max(-1.0, min(1.0, raw_val / 32768.0))
        mid = 0.5 * (rng.minimum + rng.maximum)
        half = 0.5 * (rng.maximum - rng.minimum)
        if half <= 0.0:
            return 0.0
        norm = (raw_val - mid) / half
        if norm < -1.0:
            return -1.0
        if norm > 1.0:
            return 1.0
        return norm

    def _emit_shaped_sticks(self, now_ns: int) -> None:
        prof = self.profile
        if prof is None:
            return

        lx_norm = self._normalize_stick_axis(ecodes.ABS_X, self._raw_lx)
        ly_norm = self._normalize_stick_axis(ecodes.ABS_Y, self._raw_ly)
        rx_norm = self._normalize_stick_axis(ecodes.ABS_RX, self._raw_rx)
        ry_norm = self._normalize_stick_axis(ecodes.ABS_RY, self._raw_ry)

        olx, oly = prof.left.shape_xy(lx_norm, ly_norm)
        orx, ory = prof.right.shape_xy(rx_norm, ry_norm)

        ilx = max(-32768, min(32767, int(round(olx * 32767.0))))
        ily = max(-32768, min(32767, int(round(oly * 32767.0))))
        irx = max(-32768, min(32767, int(round(orx * 32767.0))))
        iry = max(-32768, min(32767, int(round(ory * 32767.0))))

        self.vdev.queue_event(ecodes.EV_ABS, ecodes.ABS_X, ilx)
        self.vdev.queue_event(ecodes.EV_ABS, ecodes.ABS_Y, ily)
        self.vdev.queue_event(ecodes.EV_ABS, ecodes.ABS_RX, irx)
        self.vdev.queue_event(ecodes.EV_ABS, ecodes.ABS_RY, iry)

        self._sticks_dirty = False
        self._last_stick_emit_ns = now_ns

        if self.debug_sample_rate > 0:
            self._sample_counter += 1
            if self._sample_counter >= self.debug_sample_rate:
                self._sample_counter = 0
                LOG.debug(
                    "L=(%.3f,%.3f)->(%d,%d) R=(%.3f,%.3f)->(%d,%d)",
                    lx_norm, ly_norm, ilx, ily, rx_norm, ry_norm, irx, iry,
                )

    def _process_physical_events(self) -> None:
        dev = self.phys_dev
        prof = self.profile
        if dev is None or prof is None:
            return

        read_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC) if self._live_latency else 0
        try:
            buf = os.read(dev.fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return
        except OSError as exc:
            LOG.info("Physical controller disconnected (%s)", exc)
            self._detach_physical_device()
            self._notify_status()
            return

        if not buf:
            self._detach_physical_device()
            self._notify_status()
            return

        frame_ev_ns: int | None = None
        max_hz = prof.max_output_hz
        min_interval_ns = int(1_000_000_000 / max_hz) if max_hz > 0.0 else 0

        for offset in range(0, len(buf) - EVENT_SIZE + 1, EVENT_SIZE):
            sec, usec, ev_type, ev_code, ev_value = INPUT_EVENT_STRUCT.unpack_from(buf, offset)
            if self._live_latency and frame_ev_ns is None and ev_type != ecodes.EV_SYN:
                frame_ev_ns = sec * 1_000_000_000 + usec * 1_000

            if ev_type == ecodes.EV_ABS:
                if ev_code == ecodes.ABS_X:
                    self._raw_lx = ev_value
                    self._sticks_dirty = True
                elif ev_code == ecodes.ABS_Y:
                    self._raw_ly = ev_value
                    self._sticks_dirty = True
                elif ev_code == ecodes.ABS_RX:
                    self._raw_rx = ev_value
                    self._sticks_dirty = True
                elif ev_code == ecodes.ABS_RY:
                    self._raw_ry = ev_value
                    self._sticks_dirty = True
                elif ev_code == ecodes.ABS_Z:
                    rng = self._axis_ranges.get(ecodes.ABS_Z)
                    pmin, pmax = (rng.minimum, rng.maximum) if rng else (0, 255)
                    shaped_lt = prof.left_trigger.shape_value(ev_value, pmin, pmax)
                    self.vdev.queue_event(ecodes.EV_ABS, ecodes.ABS_Z, shaped_lt)
                elif ev_code == ecodes.ABS_RZ:
                    rng = self._axis_ranges.get(ecodes.ABS_RZ)
                    pmin, pmax = (rng.minimum, rng.maximum) if rng else (0, 255)
                    shaped_rt = prof.right_trigger.shape_value(ev_value, pmin, pmax)
                    self.vdev.queue_event(ecodes.EV_ABS, ecodes.ABS_RZ, shaped_rt)
                elif ev_code in (ecodes.ABS_HAT0X, ecodes.ABS_HAT0Y):
                    self.vdev.queue_hat_axis(ev_code, ev_value)

            elif ev_type == ecodes.EV_KEY:
                if ev_code in DPAD_BUTTON_CODES:
                    self.vdev.queue_dpad_button(ev_code, ev_value)
                elif ev_code in PASSTHROUGH_BUTTONS:
                    self.vdev.queue_button(ev_code, ev_value)

            elif ev_type == ecodes.EV_SYN and ev_code == ecodes.SYN_REPORT:
                now_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC) if (min_interval_ns or self._live_latency) else 0
                proc_start_ns = now_ns

                if self._sticks_dirty:
                    if min_interval_ns == 0 or (now_ns - self._last_stick_emit_ns) >= min_interval_ns:
                        self._emit_shaped_sticks(now_ns)

                self.vdev.flush()

                if self._live_latency and frame_ev_ns is not None:
                    end_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
                    self.latency_recorder.record(
                        max(0.0, (read_ns - frame_ev_ns) / 1000.0),
                        max(0.0, (end_ns - proc_start_ns) / 1000.0),
                        max(0.0, (end_ns - frame_ev_ns) / 1000.0),
                    )
                    frame_ev_ns = None

    def run(self) -> int:
        if not self._acquire_instance_lock():
            return 1
        PID_FILE_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
        self._setup_signals()
        self._setup_watchers()
        self.load_configuration(initial=True)

        try:
            while self.running:
                if self._reload_requested:
                    self._reload_requested = False
                    if self.inotify is not None:
                        self.inotify.rewatch()
                    self.load_configuration(initial=False)

                timeout = 1.0
                if not self.paused and self._sticks_dirty and self.profile and self.profile.max_output_hz > 0:
                    interval_s = 1.0 / self.profile.max_output_hz
                    elapsed_s = (time.clock_gettime_ns(time.CLOCK_MONOTONIC) - self._last_stick_emit_ns) / 1e9
                    timeout = max(0.0002, interval_s - elapsed_s)
                elif not self.paused and self.phys_dev is None and self.udev_monitor is None:
                    timeout = min(timeout, self.rescan_interval)
                if self.inotify is None:
                    timeout = min(timeout, self.reload_interval)

                events = self.selector.select(timeout=timeout)
                for key, _mask in events:
                    tag = key.data
                    if tag == "signal":
                        try:
                            os.read(self._wakeup_r, 64)
                        except OSError:
                            pass
                    elif tag == "phys":
                        self._process_physical_events()
                    elif tag == "uinput":
                        self.vdev.handle_uinput_events(self.phys_dev)
                    elif tag == "inotify":
                        if self.inotify is not None and self.inotify.drain_changed():
                            self.load_configuration(initial=False)
                    elif tag == "udev":
                        if self.udev_monitor is not None:
                            for dev in iter(functools.partial(self.udev_monitor.poll, 0), None):
                                if dev.action in ("add", "change", "bind") and not self.paused and self.phys_dev is None:
                                    self._scan_and_attach_physical()

                if not self.paused and self._sticks_dirty and self.profile and self.profile.max_output_hz > 0:
                    now_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
                    min_ns = int(1_000_000_000 / self.profile.max_output_hz)
                    if (now_ns - self._last_stick_emit_ns) >= min_ns:
                        self._emit_shaped_sticks(now_ns)
                        self.vdev.flush()

                if self.inotify is None and self.config_path.exists():
                    mtime = self.config_path.stat().st_mtime
                    if mtime != self._last_config_mtime:
                        self.load_configuration(initial=False)
                if not self.paused and self.phys_dev is None and self.udev_monitor is None:
                    self._scan_and_attach_physical()

                if self._live_latency and (time.monotonic() - self._last_latency_flush) >= 2.0:
                    if self.latency_recorder.count > 0:
                        LOG.info("\n%s", self.latency_recorder.format_report("Live Telemetry"))
                        self.latency_recorder.clear()
                    self._last_latency_flush = time.monotonic()
        finally:
            sd_notify("STOPPING=1")
            self._detach_physical_device()
            if self.vdev.is_open:
                try:
                    self.selector.unregister(self.vdev.fd)
                except KeyError:
                    pass
                self.vdev.close()
            if self._wakeup_w >= 0:
                try:
                    signal.set_wakeup_fd(-1)
                except (ValueError, OSError):
                    pass
                try:
                    self.selector.unregister(self._wakeup_r)
                except (KeyError, ValueError, OSError):
                    pass
                for fd in (self._wakeup_r, self._wakeup_w):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                self._wakeup_r = self._wakeup_w = -1
            if self.inotify is not None:
                self.inotify.close()
            self.selector.close()
            PID_FILE_PATH.unlink(missing_ok=True)
            if self._lock_fd is not None:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                    os.close(self._lock_fd)
                except OSError:
                    pass
                self._lock_fd = None
            LOG.info("Daemon stopped cleanly")
        return 0
