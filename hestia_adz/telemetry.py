"""Microsecond latency telemetry collector and standalone adz latency benchmark."""

from __future__ import annotations

import os
import selectors
import signal
import time
from array import array
from pathlib import Path

from .config import compile_active_profile, load_raw_config, resolve_config_path
from .sys_runtime import set_monotonic_clock
from .virtual_device import EVENT_SIZE, INPUT_EVENT_STRUCT, PersistentXboxReceiver


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


class LatencyRecorder:
    """Collects per-frame kernel wakeup, shaping, and end-to-end latencies in microseconds."""

    def __init__(self, capacity: int = 8192) -> None:
        self.capacity = capacity
        self.wakeup_us = array("f")
        self.proc_us = array("f")
        self.total_us = array("f")

    def record(self, wakeup_us: float, proc_us: float, total_us: float) -> None:
        if len(self.total_us) >= self.capacity:
            return
        self.wakeup_us.append(wakeup_us)
        self.proc_us.append(proc_us)
        self.total_us.append(total_us)

    def clear(self) -> None:
        del self.wakeup_us[:]
        del self.proc_us[:]
        del self.total_us[:]

    @property
    def count(self) -> int:
        return len(self.total_us)

    def format_report(self, title: str = "Latency Telemetry") -> str:
        n = len(self.total_us)
        if n == 0:
            return f"{title}: 0 samples collected (move sticks or press buttons during test)"

        def _row(label: str, arr: array) -> str:
            s = sorted(arr)
            avg = sum(s) / n
            p50 = _percentile(s, 50.0)
            p95 = _percentile(s, 95.0)
            p99 = _percentile(s, 99.0)
            mx = s[-1]
            return (
                f"  {label:<24} avg={avg:7.1f} us  p50={p50:7.1f} us  "
                f"p95={p95:7.1f} us  p99={p99:7.1f} us  max={mx:7.1f} us"
            )

        lines = [
            f"{title} ({n} frames):",
            _row("Kernel -> Userspace:", self.wakeup_us),
            _row("Shaping & Framing:", self.proc_us),
            _row("End-to-End Total:", self.total_us),
        ]
        return "\n".join(lines)


def run_latency_benchmark(
    *,
    duration: float = 5.0,
    max_samples: int | None = None,
    with_uinput: bool = False,
    config_path: Path | None = None,
) -> int:
    from evdev import InputDevice, ecodes, list_devices

    cfg_path = resolve_config_path(config_path)
    raw_cfg = load_raw_config(cfg_path)
    profile = compile_active_profile(raw_cfg)
    match_sub = str(raw_cfg.get("device_name_match", "")).lower()

    target_dev: InputDevice | None = None
    for path in sorted(list_devices()):
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if dev.name == "Xbox Wireless Controller" and dev.info.vendor == 0x045E:
            dev.close()
            continue
        caps = dev.capabilities()
        abs_codes = {c for c, _ in caps.get(ecodes.EV_ABS, [])}
        if ecodes.ABS_X in abs_codes and ecodes.ABS_Y in abs_codes:
            if not match_sub or match_sub in dev.name.lower():
                target_dev = dev
                break
        dev.close()

    if target_dev is None:
        print("Error: No matching physical gamepad found for latency test.")
        return 1

    set_monotonic_clock(target_dev.fd)
    os.set_blocking(target_dev.fd, False)

    vdev: PersistentXboxReceiver | None = None
    if with_uinput:
        vdev = PersistentXboxReceiver()
        vdev.open()

    recorder = LatencyRecorder(capacity=max_samples or 65536)
    sel = selectors.DefaultSelector()
    sel.register(target_dev.fd, selectors.EVENT_READ)

    deadline = time.monotonic() + duration if max_samples is None else float("inf")
    lx = ly = rx = ry = 0

    print(
        f"Measuring input latency on {target_dev.name!r} ({target_dev.path}) "
        f"[profile={profile.name}, with_uinput={with_uinput}]..."
    )
    print("Move sticks or press buttons now...")

    try:
        while time.monotonic() < deadline:
            if max_samples is not None and recorder.count >= max_samples:
                break
            timeout = 0.05 if max_samples is not None else max(0.0, min(0.05, deadline - time.monotonic()))
            events = sel.select(timeout=timeout)
            if not events:
                continue

            read_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
            try:
                buf = os.read(target_dev.fd, EVENT_SIZE * 64)
            except BlockingIOError:
                continue

            frame_event_ns: int | None = None
            for offset in range(0, len(buf) - EVENT_SIZE + 1, EVENT_SIZE):
                sec, usec, ev_type, ev_code, ev_value = INPUT_EVENT_STRUCT.unpack_from(buf, offset)
                if ev_type == ecodes.EV_SYN and ev_code == ecodes.SYN_REPORT:
                    proc_start_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
                    profile.left.shape_xy(lx / 32768.0, ly / 32768.0)
                    profile.right.shape_xy(rx / 32768.0, ry / 32768.0)
                    if vdev is not None:
                        vdev.queue_event(ecodes.EV_ABS, ecodes.ABS_X, lx)
                        vdev.flush()
                    end_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)

                    ev_ns = frame_event_ns if frame_event_ns is not None else (sec * 1_000_000_000 + usec * 1_000)
                    wakeup_us = max(0.0, (read_ns - ev_ns) / 1000.0)
                    proc_us = max(0.0, (end_ns - proc_start_ns) / 1000.0)
                    total_us = max(0.0, (end_ns - ev_ns) / 1000.0)
                    recorder.record(wakeup_us, proc_us, total_us)
                    frame_event_ns = None
                    if max_samples is not None and recorder.count >= max_samples:
                        break
                else:
                    if frame_event_ns is None:
                        frame_event_ns = sec * 1_000_000_000 + usec * 1_000
                    if ev_type == ecodes.EV_ABS:
                        if ev_code == ecodes.ABS_X:
                            lx = ev_value
                        elif ev_code == ecodes.ABS_Y:
                            ly = ev_value
                        elif ev_code == ecodes.ABS_RX:
                            rx = ev_value
                        elif ev_code == ecodes.ABS_RY:
                            ry = ev_value
    finally:
        sel.close()
        target_dev.close()
        if vdev is not None:
            vdev.close()

    print()
    print(recorder.format_report("Benchmark Results"))
    return 0


def toggle_daemon_telemetry(pid_file: Path) -> int:
    if not pid_file.exists():
        print("Daemon is not running (PID file not found).")
        return 1
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGUSR2)
        print(f"Sent SIGUSR2 to daemon (PID {pid}) to toggle live latency telemetry.")
        return 0
    except (ValueError, ProcessLookupError, PermissionError) as exc:
        print(f"Failed to signal daemon: {exc}")
        return 1
