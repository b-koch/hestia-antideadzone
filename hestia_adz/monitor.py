"""Interactive ANSI terminal stick & trigger visualizer (adz monitor)."""

from __future__ import annotations

import math
import os
import selectors
from pathlib import Path

from .config import compile_active_profile, load_raw_config, resolve_config_path


def _render_polar_grid(
    title: str,
    raw_x: float,
    raw_y: float,
    out_x: float,
    out_y: float,
    dz: float,
    slope: float,
    radius: int = 7,
) -> list[str]:
    width = radius * 4 + 1
    height = radius * 2 + 1
    grid = [[" " for _ in range(width)] for _ in range(height)]
    cx = radius * 2
    cy = radius

    for gy in range(height):
        ny = (gy - cy) / radius
        for gx in range(width):
            nx = (gx - cx) / (radius * 2)
            r = math.hypot(nx, ny)
            if abs(r - 1.0) < 0.08:
                grid[gy][gx] = "·"
            elif dz > 0.02 and abs(r - dz) < 0.07:
                grid[gy][gx] = "°"
            elif gx == cx and gy == cy:
                grid[gy][gx] = "+"
            elif gy == cy:
                grid[gy][gx] = "─"
            elif gx == cx:
                grid[gy][gx] = "│"
            elif slope > 0.01 and r <= 1.0:
                ax, ay = abs(nx), abs(ny)
                if (ax > ay and abs(ay - slope * ax) < 0.06) or (ay > ax and abs(ax - slope * ay) < 0.06):
                    grid[gy][gx] = "·"

    def _plot(px: float, py: float, ch: str) -> None:
        gx = max(0, min(width - 1, int(round(cx + px * radius * 2))))
        gy = max(0, min(height - 1, int(round(cy + py * radius))))
        grid[gy][gx] = ch

    _plot(raw_x, raw_y, "○")
    _plot(out_x, out_y, "●")

    raw_mag = math.hypot(raw_x, raw_y)
    out_mag = math.hypot(out_x, out_y)
    angle = math.degrees(math.atan2(-out_y, out_x)) if out_mag > 1e-4 else 0.0
    locked = (abs(out_x) < 1e-5) ^ (abs(out_y) < 1e-5)

    lines = [f" {title:<31}"]
    lines.append("┌" + "─" * width + "┐")
    for row in grid:
        lines.append("│" + "".join(row) + "│")
    lines.append("└" + "─" * width + "┘")
    lines.append(f" Raw: ({raw_x:+.3f}, {raw_y:+.3f}) r={raw_mag:.3f}")
    lines.append(f" Out: ({out_x:+.3f}, {out_y:+.3f}) r={out_mag:.3f}")
    lock_tag = " [CARDINAL LOCK]" if locked else ""
    lines.append(f" Ang: {angle:+6.1f}°{lock_tag:<16}")
    return lines


def _bar(label: str, val: int, width: int = 22) -> str:
    filled = int(round((max(0, min(255, val)) / 255.0) * width))
    return f"{label}: [{'█' * filled}{'░' * (width - filled)}] {val:3d}/255"


def run_monitor(
    *,
    fps: float = 30.0,
    device_match: str = "",
    config_path: Path | None = None,
) -> int:
    from evdev import InputDevice, ecodes, list_devices

    cfg_path = resolve_config_path(config_path)
    raw_cfg = load_raw_config(cfg_path)
    profile = compile_active_profile(raw_cfg)
    sub = (device_match or str(raw_cfg.get("device_name_match", ""))).lower()

    phys_dev: InputDevice | None = None
    virt_dev: InputDevice | None = None
    for path in sorted(list_devices()):
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        caps = dev.capabilities()
        abs_codes = {c for c, _ in caps.get(ecodes.EV_ABS, [])}
        if ecodes.ABS_X not in abs_codes or ecodes.ABS_Y not in abs_codes:
            dev.close()
            continue
        if dev.name == "Xbox Wireless Controller" and dev.info.vendor == 0x045E:
            virt_dev = dev
        elif not sub or sub in dev.name.lower():
            if phys_dev is None:
                phys_dev = dev
            else:
                dev.close()
        else:
            dev.close()

    daemon_active = not bool(raw_cfg.get("paused", False))
    if daemon_active and virt_dev is not None:
        if phys_dev is not None:
            phys_dev.close()
        target = virt_dev
        observing_virtual = True
    elif phys_dev is not None:
        if virt_dev is not None:
            virt_dev.close()
        target = phys_dev
        observing_virtual = False
    elif virt_dev is not None:
        target = virt_dev
        observing_virtual = True
    else:
        print("Error: No gamepad found to monitor.")
        return 1

    os.set_blocking(target.fd, False)
    caps = target.capabilities()
    ranges = {c: (int(i.min), int(i.max)) for c, i in caps.get(ecodes.EV_ABS, [])}

    def _norm(code: int, val: int) -> float:
        lo, hi = ranges.get(code, (-32768, 32767))
        mid = 0.5 * (lo + hi)
        half = 0.5 * (hi - lo)
        return max(-1.0, min(1.0, (val - mid) / half)) if half > 0 else 0.0

    lx = ly = rx = ry = 0
    lt = rt = 0
    sel = selectors.DefaultSelector()
    sel.register(target.fd, selectors.EVENT_READ)
    frame_dt = 1.0 / max(1.0, fps)

    print("\033[?25l\033[2J", end="", flush=True)
    try:
        while True:
            sel.select(timeout=frame_dt)
            while True:
                try:
                    for ev in target.read():
                        if ev.type == ecodes.EV_ABS:
                            if ev.code == ecodes.ABS_X:
                                lx = ev.value
                            elif ev.code == ecodes.ABS_Y:
                                ly = ev.value
                            elif ev.code == ecodes.ABS_RX:
                                rx = ev.value
                            elif ev.code == ecodes.ABS_RY:
                                ry = ev.value
                            elif ev.code == ecodes.ABS_Z:
                                lt = ev.value
                            elif ev.code == ecodes.ABS_RZ:
                                rt = ev.value
                except BlockingIOError:
                    break

            nlx, nly = _norm(ecodes.ABS_X, lx), _norm(ecodes.ABS_Y, ly)
            nrx, nry = _norm(ecodes.ABS_RX, rx), _norm(ecodes.ABS_RY, ry)

            if observing_virtual:
                olx, oly, orx, ory = nlx, nly, nrx, nry
                slt, srt = lt, rt
                mode_note = "observing live virtual receiver output (pause daemon to see raw vs shaped)"
            else:
                olx, oly = profile.left.shape_xy(nlx, nly)
                orx, ory = profile.right.shape_xy(nrx, nry)
                lt_lo, lt_hi = ranges.get(ecodes.ABS_Z, (0, 255))
                rt_lo, rt_hi = ranges.get(ecodes.ABS_RZ, (0, 255))
                slt = profile.left_trigger.shape_value(lt, lt_lo, lt_hi)
                srt = profile.right_trigger.shape_value(rt, rt_lo, rt_hi)
                mode_note = "simulating profile live on raw physical input (○ raw, ● shaped)"

            left_box = _render_polar_grid(
                f"LEFT STICK [{profile.left.shape}]",
                nlx, nly, olx, oly,
                profile.left.deadzone, profile.left.axial_deadzone,
            )
            right_box = _render_polar_grid(
                f"RIGHT STICK [{profile.right.shape}]",
                nrx, nry, orx, ory,
                profile.right.deadzone, profile.right.axial_deadzone,
            )

            out_lines = [
                f"\033[Hhestia-antideadzone monitor | profile: {profile.name} | device: {target.name}",
                f"Mode: {mode_note} (Ctrl+C to exit)",
                "",
            ]
            for l_line, r_line in zip(left_box, right_box):
                out_lines.append(f"{l_line:<33}   {r_line:<33}")
            out_lines.append("")
            out_lines.append(f" {_bar('LT', slt)}     {_bar('RT', srt)}")
            print("\n".join(out_lines), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sel.close()
        target.close()
        print("\033[?25h", end="", flush=True)
    return 0
