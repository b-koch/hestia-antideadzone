"""Deadzone geometry shaping, anti-deadzone mapping, and curve LUTs."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

LUT_SIZE = 4096
LUT_MAX_IDX = LUT_SIZE - 1
_SQRT2_MINUS_1 = math.sqrt(2.0) - 1.0

VALID_SHAPES = frozenset({
    "radial",
    "radial_unscaled",
    "bowtie",
    "axial",
    "axial_unscaled",
    "sloped_axial",
    "sloped_axial_unscaled",
    "hybrid",
    "square",
    "square_unscaled",
    "squircle",
    "smooth_axial",
})

_UNSCALED_SHAPES = frozenset({
    "radial_unscaled",
    "axial_unscaled",
    "sloped_axial_unscaled",
    "square_unscaled",
})


def _clamp01(value: float) -> float:
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return value


def build_curve_lut(curve: float, size: int = LUT_SIZE) -> tuple[float, ...]:
    """Precompute normalized [0, 1] -> [0, 1] power curve values."""
    if abs(curve - 1.0) < 1e-9:
        return tuple(i / (size - 1) for i in range(size))
    denom = float(size - 1)
    return tuple((i / denom) ** curve for i in range(size))


def eval_lut(lut: tuple[float, ...], u: float, curve: float) -> float:
    """Linearly interpolate a 4096-entry curve LUT with near-zero exactness."""
    if u <= 0.0:
        return 0.0
    if u >= 1.0:
        return 1.0
    if abs(curve - 1.0) < 1e-9:
        return u
    pos = u * LUT_MAX_IDX
    idx = int(pos)
    if idx < 4:
        return u ** curve
    if idx >= LUT_MAX_IDX:
        return 1.0
    frac = pos - idx
    y0 = lut[idx]
    return y0 + (lut[idx + 1] - y0) * frac


@dataclass(slots=True)
class CompiledStick:
    """Precomputed shaping parameters and hot-loop evaluator for one analog stick."""

    shape: str
    deadzone: float
    outer_deadzone: float
    active_max: float
    inv_span: float
    inv_active_max: float
    anti_deadzone: float
    anti_span: float
    curve: float
    axial_deadzone: float
    sloped_dominance_curve: float
    outer_circle_clamp: bool
    is_unscaled: bool
    is_passthrough: bool
    lut: tuple[float, ...] | None

    def _map_1d(self, mag: float) -> float:
        """Map an input magnitude in [deadzone, active_max] to [anti_deadzone, 1.0]."""
        if mag <= self.deadzone:
            return 0.0
        if self.is_unscaled:
            if mag >= self.active_max:
                return 1.0
            scaled = mag * self.inv_active_max
            return self.anti_deadzone if scaled < self.anti_deadzone else scaled
        if mag >= self.active_max:
            return 1.0
        t = (mag - self.deadzone) * self.inv_span
        curved = eval_lut(self.lut, t, self.curve)
        return self.anti_deadzone + self.anti_span * curved

    def shape_xy(self, x: float, y: float) -> tuple[float, float]:
        """Transform normalized [-1.0, 1.0] stick coordinates."""
        if self.is_passthrough:
            return x, y

        shape = self.shape
        dz = self.deadzone

        if shape == "radial" or shape == "radial_unscaled":
            r = math.hypot(x, y)
            if r <= dz or r == 0.0:
                return 0.0, 0.0
            mag = self._map_1d(r)
            scale = mag / r
            ox = x * scale
            oy = y * scale
            if self.outer_circle_clamp:
                out_r = math.hypot(ox, oy)
                if out_r > 1.0:
                    inv_r = 1.0 / out_r
                    ox *= inv_r
                    oy *= inv_r
            return ox, oy

        if shape == "square" or shape == "square_unscaled":
            ax = -x if x < 0.0 else x
            ay = -y if y < 0.0 else y
            m = ax if ax >= ay else ay
            if m <= dz or m == 0.0:
                return 0.0, 0.0
            mag = self._map_1d(m)
            scale = mag / m
            ox = x * scale
            oy = y * scale
            if self.outer_circle_clamp:
                out_r = math.hypot(ox, oy)
                if out_r > 1.0:
                    inv_r = 1.0 / out_r
                    ox *= inv_r
                    oy *= inv_r
            return ox, oy

        if shape == "squircle":
            r = math.hypot(x, y)
            if r <= dz or r == 0.0:
                return 0.0, 0.0
            mag = self._map_1d(r)
            r2 = r * r
            sin2_2theta = 4.0 * (x * x * y * y) / (r2 * r2)
            diag_gain = 1.0 + self.axial_deadzone * _SQRT2_MINUS_1 * sin2_2theta
            scale = (mag * diag_gain) / r
            ox = x * scale
            oy = y * scale
            if self.outer_circle_clamp:
                out_r = math.hypot(ox, oy)
                if out_r > 1.0:
                    inv_r = 1.0 / out_r
                    ox *= inv_r
                    oy *= inv_r
            return ox, oy

        if shape == "smooth_axial":
            r = math.hypot(x, y)
            if r <= dz or r == 0.0:
                return 0.0, 0.0
            if r >= self.active_max:
                u = 1.0
            else:
                t = (r - dz) * self.inv_span
                u = eval_lut(self.lut, t, self.curve)
            inv_r = u / r
            vx = (-x if x < 0.0 else x) * inv_r
            vy = (-y if y < 0.0 else y) * inv_r

            ramp = self.axial_deadzone
            adz = self.anti_deadzone
            anti_span = self.anti_span

            if vx <= 0.0:
                mx = 0.0
            else:
                if ramp <= 0.0 or vx >= ramp:
                    wx = 1.0
                else:
                    qx = vx / ramp
                    wx = qx * qx * (3.0 - 2.0 * qx)
                mx = adz * wx + anti_span * vx

            if vy <= 0.0:
                my = 0.0
            else:
                if ramp <= 0.0 or vy >= ramp:
                    wy = 1.0
                else:
                    qy = vy / ramp
                    wy = qy * qy * (3.0 - 2.0 * qy)
                my = adz * wy + anti_span * vy

            ox = -mx if x < 0.0 else mx
            oy = -my if y < 0.0 else my
            if self.outer_circle_clamp:
                out_r = math.hypot(ox, oy)
                if out_r > 1.0:
                    inv_out = 1.0 / out_r
                    ox *= inv_out
                    oy *= inv_out
            return ox, oy

        if shape == "axial" or shape == "axial_unscaled":
            ax = -x if x < 0.0 else x
            ay = -y if y < 0.0 else y
            mx = self._map_1d(ax)
            my = self._map_1d(ay)
            ox = -mx if x < 0.0 else mx
            oy = -my if y < 0.0 else my
            if self.outer_circle_clamp:
                out_r = math.hypot(ox, oy)
                if out_r > 1.0:
                    inv_r = 1.0 / out_r
                    ox *= inv_r
                    oy *= inv_r
            return ox, oy

        if shape == "bowtie":
            r = math.hypot(x, y)
            if r <= dz or r == 0.0:
                return 0.0, 0.0
            mag = self._map_1d(r)
            ax = -x if x < 0.0 else x
            ay = -y if y < 0.0 else y
            slope = self.axial_deadzone
            if ax >= ay:
                x_dz = slope * ax
                if ay <= x_dz:
                    ox = -mag if x < 0.0 else mag
                    return ox, 0.0
                span_y = ax - x_dz
                ny = 1.0 if span_y <= 0.0 else ((ay - x_dz) / span_y) * ax
                norm = math.hypot(ax, ny)
                scale = mag / norm
                ox = (-ax if x < 0.0 else ax) * scale
                oy = (-ny if y < 0.0 else ny) * scale
            else:
                y_dz = slope * ay
                if ax <= y_dz:
                    oy = -mag if y < 0.0 else mag
                    return 0.0, oy
                span_x = ay - y_dz
                nx = 1.0 if span_x <= 0.0 else ((ax - y_dz) / span_x) * ay
                norm = math.hypot(nx, ay)
                scale = mag / norm
                ox = (-nx if x < 0.0 else nx) * scale
                oy = (-ay if y < 0.0 else ay) * scale
            if self.outer_circle_clamp:
                out_r = math.hypot(ox, oy)
                if out_r > 1.0:
                    inv_r = 1.0 / out_r
                    ox *= inv_r
                    oy *= inv_r
            return ox, oy

        if shape == "sloped_axial" or shape == "sloped_axial_unscaled":
            ax = -x if x < 0.0 else x
            ay = -y if y < 0.0 else y
            x_is_major = ax >= ay
            major = ax if x_is_major else ay
            minor = ay if x_is_major else ax

            if major <= dz:
                return 0.0, 0.0

            out_major = self._map_1d(major)
            minor_dz = dz + self.axial_deadzone * (major - dz)
            if minor_dz >= major:
                minor_dz = major - 1e-6

            if minor <= minor_dz:
                out_minor = 0.0
            else:
                span_minor = major - minor_dz
                ang_t = 1.0 if span_minor <= 0.0 else _clamp01((minor - minor_dz) / span_minor)
                dom_exp = self.sloped_dominance_curve
                if dom_exp <= 0.0:
                    dom = 1.0
                else:
                    hermite = ang_t * ang_t * (3.0 - 2.0 * ang_t)
                    dom = hermite if abs(dom_exp - 1.0) < 1e-9 else (hermite ** dom_exp)
                eff_adz = self.anti_deadzone * dom
                if eff_adz > out_major:
                    eff_adz = out_major
                if self.is_unscaled:
                    scaled = out_major * ang_t
                    out_minor = eff_adz if scaled < eff_adz else scaled
                else:
                    out_minor = eff_adz + (out_major - eff_adz) * eval_lut(self.lut, ang_t, self.curve)

            if x_is_major:
                ox = -out_major if x < 0.0 else out_major
                oy = -out_minor if y < 0.0 else out_minor
            else:
                ox = -out_minor if x < 0.0 else out_minor
                oy = -out_major if y < 0.0 else out_major

            if self.outer_circle_clamp:
                out_r = math.hypot(ox, oy)
                if out_r > 1.0:
                    inv_r = 1.0 / out_r
                    ox *= inv_r
                    oy *= inv_r
            return ox, oy

        if shape == "hybrid":
            # Stage 1: Circular center deadzone + radial response curve
            r = math.hypot(x, y)
            if r <= dz or r == 0.0:
                return 0.0, 0.0
            if r >= self.active_max:
                r_curved = 1.0
            else:
                t_rad = (r - dz) * self.inv_span
                r_curved = eval_lut(self.lut, t_rad, self.curve)
            rad_scale = r_curved / r
            hx = x * rad_scale
            hy = y * rad_scale

            # Stage 2: Bowtie per-axis shaping + symmetric diagonal anti-deadzone
            ax = -hx if hx < 0.0 else hx
            ay = -hy if hy < 0.0 else hy
            x_is_major = ax >= ay
            major = ax if x_is_major else ay
            minor = ay if x_is_major else ax

            out_major = self.anti_deadzone + self.anti_span * major
            minor_dz = self.axial_deadzone * major
            if minor <= minor_dz:
                out_minor = 0.0
            else:
                span_minor = major - minor_dz
                ang_t = 1.0 if span_minor <= 0.0 else _clamp01((minor - minor_dz) / span_minor)
                dom_exp = self.sloped_dominance_curve
                if dom_exp <= 0.0:
                    dom = 1.0
                else:
                    hermite = ang_t * ang_t * (3.0 - 2.0 * ang_t)
                    dom = hermite if abs(dom_exp - 1.0) < 1e-9 else (hermite ** dom_exp)
                eff_adz = self.anti_deadzone * dom
                if eff_adz > out_major:
                    eff_adz = out_major
                out_minor = eff_adz + (out_major - eff_adz) * ang_t

            if x_is_major:
                ox = -out_major if hx < 0.0 else out_major
                oy = -out_minor if hy < 0.0 else out_minor
            else:
                ox = -out_minor if hx < 0.0 else out_minor
                oy = -out_major if hy < 0.0 else out_major

            if self.outer_circle_clamp:
                out_r = math.hypot(ox, oy)
                if out_r > 1.0:
                    inv_r = 1.0 / out_r
                    ox *= inv_r
                    oy *= inv_r
            return ox, oy

        # Fallback: radial
        r = math.hypot(x, y)
        if r <= dz or r == 0.0:
            return 0.0, 0.0
        mag = self._map_1d(r)
        scale = mag / r
        return x * scale, y * scale


@dataclass(slots=True)
class CompiledTrigger:
    """Precomputed shaping parameters and hot-loop evaluator for one analog trigger."""

    deadzone: float
    outer_deadzone: float
    active_max: float
    inv_span: float
    anti_deadzone: float
    anti_span: float
    curve: float
    is_passthrough: bool
    lut: tuple[float, ...] | None

    def shape_value(self, raw: float) -> float:
        """Transform normalized [0.0, 1.0] trigger input."""
        if self.is_passthrough:
            return raw
        if raw <= self.deadzone:
            return 0.0
        if raw >= self.active_max:
            return 1.0
        t = (raw - self.deadzone) * self.inv_span
        curved = eval_lut(self.lut, t, self.curve)
        return self.anti_deadzone + self.anti_span * curved


def compile_stick(cfg: dict[str, Any]) -> CompiledStick:
    """Compile a validated stick configuration dictionary into a fast runtime object."""
    shape = str(cfg.get("deadzone_shape", "radial"))
    if shape not in VALID_SHAPES:
        shape = "radial"

    dz = _clamp01(float(cfg.get("deadzone", 0.0)))
    odz = max(0.0, min(0.95, float(cfg.get("outer_deadzone", 0.0))))
    active_max = max(dz + 1e-4, 1.0 - odz)
    span = max(1e-6, active_max - dz)
    adz = _clamp01(float(cfg.get("anti_deadzone", 0.0)))
    curve = max(0.05, float(cfg.get("curve", 1.0)))

    raw_axial = cfg.get("axial_deadzone")
    if raw_axial is None:
        if shape == "squircle":
            axial_dz = 0.55
        elif shape == "smooth_axial":
            axial_dz = 0.06
        elif shape in ("bowtie", "sloped_axial", "sloped_axial_unscaled", "hybrid") and dz < 0.05:
            axial_dz = min(0.95, adz * 0.45)
        else:
            axial_dz = 0.0
    else:
        axial_dz = max(0.0, min(1.0, float(raw_axial)))

    dom_curve = max(0.0, float(cfg.get("sloped_dominance_curve", 1.0)))
    raw_clamp = cfg.get("outer_circle_clamp")
    outer_clamp = (shape == "hybrid") if raw_clamp is None else bool(raw_clamp)
    is_unscaled = shape in _UNSCALED_SHAPES
    lut = None if is_unscaled else build_curve_lut(curve)

    is_passthrough = (
        shape in ("radial", "axial", "square", "squircle", "smooth_axial")
        and dz == 0.0
        and odz == 0.0
        and adz == 0.0
        and abs(curve - 1.0) < 1e-9
        and not outer_clamp
    )

    return CompiledStick(
        shape=shape,
        deadzone=dz,
        outer_deadzone=odz,
        active_max=active_max,
        inv_span=1.0 / span,
        inv_active_max=1.0 / active_max,
        anti_deadzone=adz,
        anti_span=1.0 - adz,
        curve=curve,
        axial_deadzone=axial_dz,
        sloped_dominance_curve=dom_curve,
        outer_circle_clamp=outer_clamp,
        is_unscaled=is_unscaled,
        is_passthrough=is_passthrough,
        lut=lut,
    )


def compile_trigger(cfg: dict[str, Any] | None) -> CompiledTrigger:
    """Compile a validated trigger configuration dictionary into a fast runtime object."""
    if not cfg:
        return CompiledTrigger(
            deadzone=0.0,
            outer_deadzone=0.0,
            active_max=1.0,
            inv_span=1.0,
            anti_deadzone=0.0,
            anti_span=1.0,
            curve=1.0,
            is_passthrough=True,
            lut=None,
        )

    dz = _clamp01(float(cfg.get("deadzone", 0.0)))
    odz = max(0.0, min(0.95, float(cfg.get("outer_deadzone", 0.0))))
    active_max = max(dz + 1e-4, 1.0 - odz)
    span = max(1e-6, active_max - dz)
    adz = _clamp01(float(cfg.get("anti_deadzone", 0.0)))
    curve = max(0.05, float(cfg.get("curve", 1.0)))

    is_passthrough = (
        dz == 0.0
        and odz == 0.0
        and adz == 0.0
        and abs(curve - 1.0) < 1e-9
    )
    lut = None if is_passthrough else build_curve_lut(curve)

    return CompiledTrigger(
        deadzone=dz,
        outer_deadzone=odz,
        active_max=active_max,
        inv_span=1.0 / span,
        anti_deadzone=adz,
        anti_span=1.0 - adz,
        curve=curve,
        is_passthrough=is_passthrough,
        lut=lut,
    )
