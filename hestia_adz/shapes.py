"""Pre-compiled stick & trigger deadzone geometries with 4096-entry curve LUTs."""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass
from typing import Any, Mapping

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


def _clamp01(v: float) -> float:
    if v <= 0.0:
        return 0.0
    if v >= 1.0:
        return 1.0
    return v


def build_curve_lut(curve: float) -> array | None:
    """Precompute a 4096-entry float32 power curve table when curve != 1.0."""
    if abs(curve - 1.0) < 1e-9:
        return None
    inv_max = 1.0 / LUT_MAX_IDX
    return array("f", ((i * inv_max) ** curve for i in range(LUT_SIZE)))


def eval_lut(lut: array | None, u: float, curve: float = 1.0) -> float:
    """Evaluate normalized u in [0.0, 1.0] using linear interpolation over lut."""
    if u <= 0.0:
        return 0.0
    if u >= 1.0:
        return 1.0
    if lut is None:
        return u
    pos = u * LUT_MAX_IDX
    idx = int(pos)
    if idx < 4:
        return u ** curve
    if idx >= LUT_MAX_IDX:
        return 1.0
    frac = pos - idx
    t0 = lut[idx]
    return t0 + (lut[idx + 1] - t0) * frac


@dataclass(frozen=True, slots=True)
class CompiledStick:
    shape: str
    deadzone: float
    outer_deadzone: float
    active_max: float
    inv_span: float
    inv_active_max: float
    anti_deadzone: float
    anti_span: float
    curve: float
    lut: array | None
    axial_deadzone: float
    inv_one_minus_slope: float
    sloped_dominance_curve: float
    outer_circle_clamp: bool
    is_unscaled: bool
    is_passthrough: bool

    def _map_1d(self, d: float) -> float:
        """Map non-negative deflection d through deadzone, outer_deadzone, anti_deadzone & LUT."""
        if d <= self.deadzone:
            return 0.0
        if self.is_unscaled:
            if d >= self.active_max:
                return 1.0
            scaled = d * self.inv_active_max
            return self.anti_deadzone if scaled < self.anti_deadzone else scaled
        if d >= self.active_max:
            return 1.0
        t = (d - self.deadzone) * self.inv_span
        return self.anti_deadzone + self.anti_span * eval_lut(self.lut, t, self.curve)

    def shape_xy(self, x: float, y: float) -> tuple[float, float]:
        """Shape normalized stick coordinates (x, y) in [-1.0, 1.0]."""
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
                ratio = ay / ax if ax > 0.0 else 0.0
                if ratio <= slope:
                    ox = -mag if x < 0.0 else mag
                    oy = 0.0
                else:
                    remapped = (ratio - slope) * self.inv_one_minus_slope
                    inv_norm = 1.0 / math.hypot(1.0, remapped)
                    ux = inv_norm
                    uy = remapped * inv_norm
                    ox = (-ux if x < 0.0 else ux) * mag
                    oy = (-uy if y < 0.0 else uy) * mag
            else:
                ratio = ax / ay if ay > 0.0 else 0.0
                if ratio <= slope:
                    ox = 0.0
                    oy = -mag if y < 0.0 else mag
                else:
                    remapped = (ratio - slope) * self.inv_one_minus_slope
                    inv_norm = 1.0 / math.hypot(1.0, remapped)
                    ux = remapped * inv_norm
                    uy = inv_norm
                    ox = (-ux if x < 0.0 else ux) * mag
                    oy = (-uy if y < 0.0 else uy) * mag

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

            if major <= 0.0:
                return 0.0, 0.0

            out_major = self.anti_deadzone + self.anti_span * _clamp01(major)
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

        raise ValueError(f"Unsupported deadzone shape: {shape}")


@dataclass(frozen=True, slots=True)
class CompiledTrigger:
    deadzone: float
    outer_deadzone: float
    active_max: float
    inv_span: float
    anti_deadzone: float
    anti_span: float
    curve: float
    lut: array | None
    is_passthrough: bool

    def shape_value(self, raw: int, phys_min: int, phys_max: int) -> int:
        """Normalize raw trigger axis [phys_min, phys_max] -> [0, 255] with shaping."""
        span = phys_max - phys_min
        if span <= 0:
            return 0
        u = (raw - phys_min) / span
        if u <= 0.0:
            return 0
        if u >= 1.0:
            u = 1.0

        if self.is_passthrough:
            return int(u * 255.0 + 0.5)

        if u <= self.deadzone:
            return 0
        if u >= self.active_max:
            out = 1.0
        else:
            t = (u - self.deadzone) * self.inv_span
            out = self.anti_deadzone + self.anti_span * eval_lut(self.lut, t, self.curve)
        val = int(out * 255.0 + 0.5)
        if val < 0:
            return 0
        if val > 255:
            return 255
        return val


def compile_stick(cfg: Mapping[str, Any] | None, *, default_deadzone: float = 0.05) -> CompiledStick:
    cfg = cfg or {}
    shape = str(cfg.get("deadzone_shape", "radial")).strip().lower()
    if shape not in VALID_SHAPES:
        raise ValueError(f"Unknown deadzone_shape {shape!r}")

    dz = float(cfg.get("deadzone", default_deadzone))
    odz = float(cfg.get("outer_deadzone", 0.0))
    adz = float(cfg.get("anti_deadzone", 0.25))
    curve = float(cfg.get("curve", 1.0))
    dom_curve = float(cfg.get("sloped_dominance_curve", 1.0))

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
        axial_dz = float(raw_axial)

    default_clamp = shape == "hybrid"
    outer_clamp = bool(cfg.get("outer_circle_clamp", default_clamp))

    active_max = 1.0 - odz
    if active_max <= dz:
        raise ValueError(f"deadzone ({dz}) + outer_deadzone ({odz}) must be < 1.0")

    inv_span = 1.0 / (active_max - dz)
    inv_active_max = 1.0 / active_max
    anti_span = 1.0 - adz
    inv_one_minus_slope = 1.0 / max(1e-6, 1.0 - axial_dz)
    is_unscaled = shape in _UNSCALED_SHAPES
    lut = None if is_unscaled else build_curve_lut(curve)

    is_passthrough = (
        shape in ("radial", "axial", "square", "squircle", "smooth_axial")
        and dz == 0.0
        and odz == 0.0
        and adz == 0.0
        and lut is None
        and not outer_clamp
    )

    return CompiledStick(
        shape=shape,
        deadzone=dz,
        outer_deadzone=odz,
        active_max=active_max,
        inv_span=inv_span,
        inv_active_max=inv_active_max,
        anti_deadzone=adz,
        anti_span=anti_span,
        curve=curve,
        lut=lut,
        axial_deadzone=axial_dz,
        inv_one_minus_slope=inv_one_minus_slope,
        sloped_dominance_curve=dom_curve,
        outer_circle_clamp=outer_clamp,
        is_unscaled=is_unscaled,
        is_passthrough=is_passthrough,
    )


def compile_trigger(cfg: Mapping[str, Any] | None) -> CompiledTrigger:
    cfg = cfg or {}
    dz = float(cfg.get("deadzone", 0.0))
    if "outer_deadzone" in cfg and cfg["outer_deadzone"] is not None:
        odz = float(cfg["outer_deadzone"])
    elif "max" in cfg and cfg["max"] is not None:
        odz = 1.0 - float(cfg["max"])
    else:
        odz = 0.0

    adz = float(cfg.get("anti_deadzone", 0.0))
    curve = float(cfg.get("curve", 1.0))

    active_max = 1.0 - odz
    if active_max <= dz:
        raise ValueError(f"Trigger deadzone ({dz}) + outer_deadzone ({odz}) must be < 1.0")

    lut = build_curve_lut(curve)
    is_passthrough = dz == 0.0 and odz == 0.0 and adz == 0.0 and lut is None

    return CompiledTrigger(
        deadzone=dz,
        outer_deadzone=odz,
        active_max=active_max,
        inv_span=1.0 / (active_max - dz),
        anti_deadzone=adz,
        anti_span=1.0 - adz,
        curve=curve,
        lut=lut,
        is_passthrough=is_passthrough,
    )
