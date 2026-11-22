"""Configuration loading, atomic persistence, compilation, and schema validation."""

from __future__ import annotations

import copy
import difflib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .shapes import VALID_SHAPES, CompiledStick, CompiledTrigger, compile_stick, compile_trigger

USER_CONFIG_PATH = Path.home() / ".config" / "hestia" / "anti-deadzone" / "config.json"
SYSTEM_CONFIG_PATH = Path("/etc/hestia/anti-deadzone/config.json")
PID_FILE_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "hestia-antideadzone.pid"

GLOBAL_ALLOWED_KEYS = frozenset({
    "paused",
    "device_name_match",
    "active_profile",
    "profiles",
    "max_output_hz",
    "reload_interval",
    "rescan_interval",
    "debug_sample_rate",
    "realtime_priority",
})

PROFILE_ALLOWED_KEYS = frozenset({
    "left",
    "right",
    "left_trigger",
    "right_trigger",
    "max_output_hz",
})

STICK_ALLOWED_KEYS = frozenset({
    "deadzone",
    "outer_deadzone",
    "anti_deadzone",
    "curve",
    "deadzone_shape",
    "axial_deadzone",
    "sloped_dominance_curve",
    "outer_circle_clamp",
})

TRIGGER_ALLOWED_KEYS = frozenset({
    "deadzone",
    "outer_deadzone",
    "max",
    "anti_deadzone",
    "curve",
})

DEFAULT_CONFIG: dict[str, Any] = {
    "paused": False,
    "device_name_match": "",
    "active_profile": "default",
    "max_output_hz": 0,
    "reload_interval": 1.0,
    "rescan_interval": 1.0,
    "debug_sample_rate": 0,
    "realtime_priority": False,
    "profiles": {
        "default": {
            "left": {
                "deadzone_shape": "radial",
                "deadzone": 0.05,
                "outer_deadzone": 0.0,
                "anti_deadzone": 0.15,
                "curve": 1.0,
            },
            "right": {
                "deadzone_shape": "bowtie",
                "deadzone": 0.02,
                "outer_deadzone": 0.0,
                "axial_deadzone": 0.12,
                "anti_deadzone": 0.20,
                "curve": 1.25,
            },
            "left_trigger": {
                "deadzone": 0.0,
                "outer_deadzone": 0.0,
            },
            "right_trigger": {
                "deadzone": 0.0,
                "outer_deadzone": 0.0,
            },
        }
    },
}

BUILTIN_OFF_PROFILE: dict[str, Any] = {
    "left": {
        "deadzone_shape": "radial",
        "deadzone": 0.0,
        "outer_deadzone": 0.0,
        "anti_deadzone": 0.0,
        "curve": 1.0,
        "outer_circle_clamp": False,
    },
    "right": {
        "deadzone_shape": "radial",
        "deadzone": 0.0,
        "outer_deadzone": 0.0,
        "anti_deadzone": 0.0,
        "curve": 1.0,
        "outer_circle_clamp": False,
    },
    "left_trigger": {"deadzone": 0.0, "outer_deadzone": 0.0},
    "right_trigger": {"deadzone": 0.0, "outer_deadzone": 0.0},
}


@dataclass(frozen=True, slots=True)
class CompiledProfile:
    name: str
    left: CompiledStick
    right: CompiledStick
    left_trigger: CompiledTrigger
    right_trigger: CompiledTrigger
    max_output_hz: float


@dataclass(frozen=True, slots=True)
class ValidationReport:
    errors: list[str]
    warnings: list[str]

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


def resolve_config_path(explicit_path: str | Path | None = None) -> Path:
    if explicit_path is not None:
        return Path(explicit_path).expanduser()
    env_path = os.environ.get("ADZ_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    if USER_CONFIG_PATH.exists():
        return USER_CONFIG_PATH
    if SYSTEM_CONFIG_PATH.exists():
        return SYSTEM_CONFIG_PATH
    return USER_CONFIG_PATH


def strip_comments(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: strip_comments(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [strip_comments(item) for item in obj]
    return obj


def load_raw_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = resolve_config_path(path)
    if not cfg_path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    with cfg_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Root of {cfg_path} must be a JSON object")
    return data


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _check_unknown_keys(
    mapping: dict[str, Any],
    allowed: frozenset[str],
    context: str,
    warnings: list[str],
) -> None:
    for key in mapping:
        if key.startswith("_"):
            continue
        if key not in allowed:
            close = difflib.get_close_matches(key, sorted(allowed), n=1, cutoff=0.6)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            warnings.append(f"{context}: unknown key {key!r}{hint}")


def _validate_stick(
    stick: Any,
    context: str,
    errors: list[str],
    warnings: list[str],
) -> None:
    if not isinstance(stick, dict):
        errors.append(f"{context}: must be a JSON object")
        return
    _check_unknown_keys(stick, STICK_ALLOWED_KEYS, context, warnings)

    shape = stick.get("deadzone_shape", "radial")
    if not isinstance(shape, str) or shape.lower() not in VALID_SHAPES:
        errors.append(
            f"{context}.deadzone_shape: invalid shape {shape!r} "
            f"(expected one of {sorted(VALID_SHAPES)})"
        )

    dz = stick.get("deadzone", 0.05)
    odz = stick.get("outer_deadzone", 0.0)
    adz = stick.get("anti_deadzone", 0.25)
    curve = stick.get("curve", 1.0)
    axial = stick.get("axial_deadzone")
    dom = stick.get("sloped_dominance_curve", 1.0)
    clamp = stick.get("outer_circle_clamp")

    if not isinstance(dz, (int, float)) or isinstance(dz, bool) or not (0.0 <= dz < 1.0):
        errors.append(f"{context}.deadzone: must be a float in [0.0, 1.0)")
    if not isinstance(odz, (int, float)) or isinstance(odz, bool) or not (0.0 <= odz < 0.95):
        errors.append(f"{context}.outer_deadzone: must be a float in [0.0, 0.95)")
    if (
        isinstance(dz, (int, float))
        and not isinstance(dz, bool)
        and isinstance(odz, (int, float))
        and not isinstance(odz, bool)
        and (dz + odz) >= 1.0
    ):
        errors.append(f"{context}: deadzone ({dz}) + outer_deadzone ({odz}) must be < 1.0")

    if not isinstance(adz, (int, float)) or isinstance(adz, bool) or not (0.0 <= adz <= 1.0):
        errors.append(f"{context}.anti_deadzone: must be a float in [0.0, 1.0]")
    if not isinstance(curve, (int, float)) or isinstance(curve, bool) or curve <= 0.0:
        errors.append(f"{context}.curve: must be a positive number (> 0.0)")
    if axial is not None:
        if not isinstance(axial, (int, float)) or isinstance(axial, bool) or not (0.0 <= axial <= 1.0):
            errors.append(f"{context}.axial_deadzone: must be null or a float in [0.0, 1.0]")
    if not isinstance(dom, (int, float)) or isinstance(dom, bool) or dom < 0.0:
        errors.append(f"{context}.sloped_dominance_curve: must be a non-negative number (>= 0.0)")
    if clamp is not None and not isinstance(clamp, bool):
        errors.append(f"{context}.outer_circle_clamp: must be a boolean")


def _validate_trigger(
    trig: Any,
    context: str,
    errors: list[str],
    warnings: list[str],
) -> None:
    if not isinstance(trig, dict):
        errors.append(f"{context}: must be a JSON object")
        return
    _check_unknown_keys(trig, TRIGGER_ALLOWED_KEYS, context, warnings)

    dz = trig.get("deadzone", 0.0)
    odz = trig.get("outer_deadzone", 0.0)
    max_val = trig.get("max")
    adz = trig.get("anti_deadzone", 0.0)
    curve = trig.get("curve", 1.0)

    if not isinstance(dz, (int, float)) or isinstance(dz, bool) or not (0.0 <= dz < 1.0):
        errors.append(f"{context}.deadzone: must be a float in [0.0, 1.0)")
    if not isinstance(odz, (int, float)) or isinstance(odz, bool) or not (0.0 <= odz < 0.95):
        errors.append(f"{context}.outer_deadzone: must be a float in [0.0, 0.95)")
    if max_val is not None:
        if not isinstance(max_val, (int, float)) or isinstance(max_val, bool) or not (0.05 < max_val <= 1.0):
            errors.append(f"{context}.max: must be a float in (0.05, 1.0]")
        else:
            odz = 1.0 - float(max_val)
    if (
        isinstance(dz, (int, float))
        and not isinstance(dz, bool)
        and isinstance(odz, (int, float))
        and not isinstance(odz, bool)
        and (dz + odz) >= 1.0
    ):
        errors.append(f"{context}: deadzone ({dz}) + outer_deadzone ({odz}) must be < 1.0")
    if not isinstance(adz, (int, float)) or isinstance(adz, bool) or not (0.0 <= adz <= 1.0):
        errors.append(f"{context}.anti_deadzone: must be a float in [0.0, 1.0]")
    if not isinstance(curve, (int, float)) or isinstance(curve, bool) or curve <= 0.0:
        errors.append(f"{context}.curve: must be a positive number (> 0.0)")


def validate_config_dict(raw: Any) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(raw, dict):
        return ValidationReport(errors=["Root configuration must be a JSON object"], warnings=[])

    _check_unknown_keys(raw, GLOBAL_ALLOWED_KEYS, "root", warnings)

    if "paused" in raw and not isinstance(raw["paused"], bool):
        errors.append("paused: must be a boolean")
    if "realtime_priority" in raw and not isinstance(raw["realtime_priority"], bool):
        errors.append("realtime_priority: must be a boolean")
    if "device_name_match" in raw and not isinstance(raw["device_name_match"], str):
        errors.append("device_name_match: must be a string")
    if "max_output_hz" in raw:
        hz = raw["max_output_hz"]
        if not isinstance(hz, (int, float)) or isinstance(hz, bool) or hz < 0:
            errors.append("max_output_hz: must be a number >= 0")
    if "reload_interval" in raw:
        ri = raw["reload_interval"]
        if not isinstance(ri, (int, float)) or isinstance(ri, bool) or ri <= 0:
            errors.append("reload_interval: must be a number > 0")
    if "rescan_interval" in raw:
        si = raw["rescan_interval"]
        if not isinstance(si, (int, float)) or isinstance(si, bool) or si <= 0:
            errors.append("rescan_interval: must be a number > 0")
    if "debug_sample_rate" in raw:
        dsr = raw["debug_sample_rate"]
        if not isinstance(dsr, int) or isinstance(dsr, bool) or dsr < 0:
            errors.append("debug_sample_rate: must be an integer >= 0")

    profiles = raw.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        errors.append("profiles: must be a non-empty JSON object of named profiles")
        return ValidationReport(errors=errors, warnings=warnings)

    active = raw.get("active_profile", "default")
    if not isinstance(active, str):
        errors.append("active_profile: must be a string")
    elif active != "off" and active not in profiles:
        errors.append(f"active_profile: {active!r} does not exist in profiles")

    for pname, pdata in profiles.items():
        if str(pname).startswith("_"):
            continue
        pctx = f"profiles.{pname}"
        if not isinstance(pdata, dict):
            errors.append(f"{pctx}: must be a JSON object")
            continue
        _check_unknown_keys(pdata, PROFILE_ALLOWED_KEYS, pctx, warnings)
        if "max_output_hz" in pdata:
            phz = pdata["max_output_hz"]
            if not isinstance(phz, (int, float)) or isinstance(phz, bool) or phz < 0:
                errors.append(f"{pctx}.max_output_hz: must be a number >= 0")
        if "left" in pdata:
            _validate_stick(pdata["left"], f"{pctx}.left", errors, warnings)
        if "right" in pdata:
            _validate_stick(pdata["right"], f"{pctx}.right", errors, warnings)
        if "left_trigger" in pdata:
            _validate_trigger(pdata["left_trigger"], f"{pctx}.left_trigger", errors, warnings)
        if "right_trigger" in pdata:
            _validate_trigger(pdata["right_trigger"], f"{pctx}.right_trigger", errors, warnings)

    return ValidationReport(errors=errors, warnings=warnings)


def compile_active_profile(raw_cfg: dict[str, Any]) -> CompiledProfile:
    clean = strip_comments(raw_cfg)
    active_name = str(clean.get("active_profile", "default"))
    global_hz = float(clean.get("max_output_hz", 0.0))

    if active_name == "off":
        pdata = BUILTIN_OFF_PROFILE
    else:
        profiles = clean.get("profiles", {})
        if active_name not in profiles:
            raise KeyError(f"Profile {active_name!r} not found in config")
        pdata = profiles[active_name]

    prof_hz = float(pdata.get("max_output_hz", global_hz))
    left = compile_stick(pdata.get("left"), default_deadzone=0.05)
    right = compile_stick(pdata.get("right"), default_deadzone=0.01)
    left_trig = compile_trigger(pdata.get("left_trigger"))
    right_trig = compile_trigger(pdata.get("right_trigger"))

    return CompiledProfile(
        name=active_name,
        left=left,
        right=right,
        left_trigger=left_trig,
        right_trigger=right_trig,
        max_output_hz=prof_hz,
    )
