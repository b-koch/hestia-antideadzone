"""Unified adz CLI with lazy imports for sub-millisecond control commands."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import (
    PID_FILE_PATH,
    atomic_write_json,
    load_raw_config,
    resolve_config_path,
    strip_comments,
    validate_config_dict,
)

BASH_COMPLETION = """_adz_complete() {
    local cur prev words cword
    _init_completion || return
    local cmds="daemon status switch pause resume toggle list devices latency monitor validate edit completions"
    if [[ ${cword} -eq 1 ]]; then
        COMPREPLY=($(compgen -W "${cmds}" -- "${cur}"))
        return
    fi
    case "${words[1]}" in
        switch)
            local profiles
            profiles=$(adz list --names 2>/dev/null)
            COMPREPLY=($(compgen -W "${profiles} --keep-paused" -- "${cur}"))
            ;;
        completions)
            COMPREPLY=($(compgen -W "bash zsh fish" -- "${cur}"))
            ;;
        status)
            COMPREPLY=($(compgen -W "--short --active" -- "${cur}"))
            ;;
        daemon)
            COMPREPLY=($(compgen -W "--paused --verbose --latency --rt" -- "${cur}"))
            ;;
        latency)
            COMPREPLY=($(compgen -W "--duration --samples --with-uinput --toggle-daemon" -- "${cur}"))
            ;;
    esac
}
complete -F _adz_complete adz"""

ZSH_COMPLETION = """#compdef adz
_adz() {
    local -a subcmds profiles
    subcmds=(
        'daemon:Run the input shaping daemon'
        'status:Show concise daemon and profile status'
        'switch:Activate a profile'
        'pause:Pause shaping and remove virtual receiver'
        'resume:Resume shaping and persistent virtual receiver'
        'toggle:Toggle between paused and active states'
        'list:List available profiles'
        'devices:List connected evdev input devices'
        'latency:Measure input pipeline latency'
        'monitor:Interactive terminal stick and trigger visualizer'
        'validate:Validate config.json schema and ranges'
        'edit:Safely edit config.json in $EDITOR with validation'
        'completions:Generate shell completion scripts'
    )
    if (( CURRENT == 2 )); then
        _describe 'command' subcmds
        return
    fi
    case "$words[2]" in
        switch)
            profiles=(${(f)"$(adz list --names 2>/dev/null)"})
            _describe 'profile' profiles
            ;;
        completions)
            _values 'shell' bash zsh fish
            ;;
    esac
}
_adz "$@" """

FISH_COMPLETION = """complete -c adz -f
complete -c adz -n "__fish_use_subcommand" -a "daemon status switch pause resume toggle list devices latency monitor validate edit completions"
complete -c adz -n "__fish_seen_subcommand_from switch" -a "(adz list --names 2>/dev/null)"
complete -c adz -n "__fish_seen_subcommand_from switch" -l keep-paused
complete -c adz -n "__fish_seen_subcommand_from completions" -a "bash zsh fish"
complete -c adz -n "__fish_seen_subcommand_from status" -l short
complete -c adz -n "__fish_seen_subcommand_from status" -l active
complete -c adz -n "__fish_seen_subcommand_from daemon" -l paused -l verbose -l latency -l rt"""


def _read_daemon_pid() -> int | None:
    if not PID_FILE_PATH.exists():
        return None
    try:
        pid = int(PID_FILE_PATH.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        return None


def _signal_daemon_reload() -> bool:
    pid = _read_daemon_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGHUP)
        return True
    except OSError:
        return False


def cmd_list(args: argparse.Namespace) -> int:
    cfg_path = resolve_config_path(args.config)
    raw = load_raw_config(cfg_path)
    clean = strip_comments(raw)
    profiles = list(clean.get("profiles", {}).keys())
    if "off" not in profiles:
        profiles.append("off")

    if getattr(args, "names", False):
        for name in profiles:
            print(name)
        return 0

    pid = _read_daemon_pid()
    paused = bool(clean.get("paused", False))
    active = str(clean.get("active_profile", "default"))
    daemon_str = f"running (PID {pid})" if pid else "stopped"
    state_str = "PAUSED" if paused else "ACTIVE"

    print(f"Daemon:  {daemon_str}")
    print(f"State:   {state_str}")
    print(f"Active:  {active}")
    print(f"Config:  {cfg_path}")
    print("Profiles:")
    for name in profiles:
        marker = "*" if name == active else " "
        print(f"  {marker} {name}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg_path = resolve_config_path(args.config)
    clean = strip_comments(load_raw_config(cfg_path))
    paused = bool(clean.get("paused", False))
    active = str(clean.get("active_profile", "default"))
    state = "paused" if paused else "active"

    if args.active:
        print(active)
        return 0
    if args.short:
        print(f"{state}:{active}")
        return 0

    pid = _read_daemon_pid()
    pid_info = f"daemon PID {pid}" if pid else "daemon stopped"
    print(f"{state}: {active} [{pid_info}]")
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    cfg_path = resolve_config_path(args.config)
    raw = load_raw_config(cfg_path)
    clean = strip_comments(raw)
    target = args.profile
    available = set(clean.get("profiles", {}).keys()) | {"off"}
    if target not in available:
        print(f"Error: Unknown profile {target!r}. Available: {', '.join(sorted(available))}", file=sys.stderr)
        return 1

    raw["active_profile"] = target
    if not args.keep_paused:
        raw["paused"] = False
    atomic_write_json(cfg_path, raw)
    _signal_daemon_reload()

    state = "paused" if raw.get("paused") else "active"
    print(f"Switched to profile {target!r} ({state})")
    return 0


def _set_paused(paused: bool, config_arg: str | None) -> int:
    cfg_path = resolve_config_path(config_arg)
    raw = load_raw_config(cfg_path)
    raw["paused"] = paused
    atomic_write_json(cfg_path, raw)
    _signal_daemon_reload()
    print("Paused (virtual receiver removed, physical gamepad released)" if paused else "Resumed (virtual receiver active)")
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    return _set_paused(True, args.config)


def cmd_resume(args: argparse.Namespace) -> int:
    return _set_paused(False, args.config)


def cmd_toggle(args: argparse.Namespace) -> int:
    cfg_path = resolve_config_path(args.config)
    raw = load_raw_config(cfg_path)
    new_paused = not bool(raw.get("paused", False))
    return _set_paused(new_paused, args.config)


def cmd_devices(args: argparse.Namespace) -> int:
    from evdev import InputDevice, list_devices

    from .daemon import is_physical_gamepad

    cfg_path = resolve_config_path(args.config)
    clean = strip_comments(load_raw_config(cfg_path))
    match_sub = str(clean.get("device_name_match", "")).lower()

    print(f"{'PATH':<18} {'MATCH':<20} {'VID:PID':<11} NAME")
    print("-" * 78)
    for path in sorted(list_devices()):
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        is_pad = is_physical_gamepad(dev)
        if is_pad and (not match_sub or match_sub in dev.name.lower()):
            match_col = "YES <-- gamepad"
        elif is_pad:
            match_col = "gamepad (filtered)"
        else:
            match_col = "no"
        vid_pid = f"{dev.info.vendor:04x}:{dev.info.product:04x}"
        print(f"{dev.path:<18} {match_col:<20} {vid_pid:<11} {dev.name!r}")
        dev.close()
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    cfg_path = resolve_config_path(args.config)
    if not cfg_path.exists():
        print(f"Error: Config file not found at {cfg_path}", file=sys.stderr)
        return 1
    try:
        raw = load_raw_config(cfg_path)
    except Exception as exc:
        print(f"JSON syntax error in {cfg_path}: {exc}", file=sys.stderr)
        return 1

    report = validate_config_dict(raw)
    for w in report.warnings:
        print(f"WARNING: {w}")
    for e in report.errors:
        print(f"ERROR:   {e}", file=sys.stderr)

    if report.is_valid:
        print(f"OK: {cfg_path} is valid ({len(report.warnings)} warning(s)).")
        return 0
    return 1


def cmd_edit(args: argparse.Namespace) -> int:
    cfg_path = resolve_config_path(args.config)
    if not cfg_path.exists():
        from .config import DEFAULT_CONFIG

        atomic_write_json(cfg_path, DEFAULT_CONFIG)

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or shutil.which("nano") or shutil.which("vi") or "vi"
    original_text = cfg_path.read_text(encoding="utf-8")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="adz-edit-", delete=False, encoding="utf-8") as tmp:
        tmp.write(original_text)
        tmp_path = Path(tmp.name)

    try:
        while True:
            rc = subprocess.call([editor, str(tmp_path)])
            if rc != 0:
                print("Editor exited with non-zero status; discarding changes.", file=sys.stderr)
                return rc
            try:
                parsed = json.loads(tmp_path.read_text(encoding="utf-8"))
                report = validate_config_dict(parsed)
            except Exception as exc:
                print(f"Invalid JSON: {exc}", file=sys.stderr)
                report = None

            if report is not None and report.is_valid:
                for w in report.warnings:
                    print(f"WARNING: {w}")
                atomic_write_json(cfg_path, parsed)
                _signal_daemon_reload()
                print(f"Saved validated configuration to {cfg_path}.")
                return 0

            if report is not None:
                for e in report.errors:
                    print(f"ERROR: {e}", file=sys.stderr)

            choice = input("Validation failed. [r]e-edit or [d]iscard changes? [r/D]: ").strip().lower()
            if choice != "r":
                print("Discarded changes; live config untouched.")
                return 1
    finally:
        tmp_path.unlink(missing_ok=True)


def cmd_completions(args: argparse.Namespace) -> int:
    if args.shell == "bash":
        print(BASH_COMPLETION)
    elif args.shell == "zsh":
        print(ZSH_COMPLETION)
    elif args.shell == "fish":
        print(FISH_COMPLETION)
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    import logging

    from .daemon import AntiDeadzoneDaemon

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    daemon = AntiDeadzoneDaemon(
        config_path=resolve_config_path(args.config) if args.config else None,
        start_paused=args.paused,
        live_latency=args.latency,
        realtime=args.rt,
    )
    return daemon.run()


def cmd_latency(args: argparse.Namespace) -> int:
    from .telemetry import run_latency_benchmark, toggle_daemon_telemetry

    if args.toggle_daemon:
        return toggle_daemon_telemetry(PID_FILE_PATH)
    return run_latency_benchmark(
        duration=args.duration,
        max_samples=args.samples,
        with_uinput=args.with_uinput,
        config_path=resolve_config_path(args.config) if args.config else None,
    )


def cmd_monitor(args: argparse.Namespace) -> int:
    from .monitor import run_monitor

    return run_monitor(
        fps=args.rate,
        device_match=args.device,
        config_path=resolve_config_path(args.config) if args.config else None,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adz",
        description="Hestia kernel-level Linux analog stick & trigger anti-deadzone daemon and CLI.",
    )
    parser.add_argument("-c", "--config", default=None, help="Path to config.json")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="Show daemon state and available profiles")
    p_list.add_argument("--names", action="store_true", help="Print only profile names (one per line)")
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="Concise status output for status bars or scripts")
    p_status.add_argument("--short", action="store_true", help="Output state:profile")
    p_status.add_argument("--active", action="store_true", help="Output active profile name only")
    p_status.set_defaults(func=cmd_status)

    p_switch = sub.add_parser("switch", help="Switch active profile")
    p_switch.add_argument("profile", help="Profile name (or 'off')")
    p_switch.add_argument("--keep-paused", action="store_true", help="Do not unpause if currently paused")
    p_switch.set_defaults(func=cmd_switch)

    p_pause = sub.add_parser("pause", help="Disable virtual receiver and release physical controller grab")
    p_pause.set_defaults(func=cmd_pause)

    p_resume = sub.add_parser("resume", help="Enable persistent virtual receiver and stick shaping")
    p_resume.set_defaults(func=cmd_resume)

    p_toggle = sub.add_parser("toggle", help="Toggle between paused and active states")
    p_toggle.set_defaults(func=cmd_toggle)

    p_devices = sub.add_parser("devices", help="List connected evdev input devices")
    p_devices.set_defaults(func=cmd_devices)

    p_val = sub.add_parser("validate", help="Validate config.json syntax, keys, and numeric bounds")
    p_val.set_defaults(func=cmd_validate)

    p_edit = sub.add_parser("edit", help="Edit config.json safely in $EDITOR with schema validation")
    p_edit.set_defaults(func=cmd_edit)

    p_comp = sub.add_parser("completions", help="Output shell completion script")
    p_comp.add_argument("shell", choices=["bash", "zsh", "fish"], help="Target shell")
    p_comp.set_defaults(func=cmd_completions)

    p_daemon = sub.add_parser("daemon", help="Run the background input shaping daemon")
    p_daemon.add_argument("--paused", action="store_true", help="Start in paused mode")
    p_daemon.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    p_daemon.add_argument("--latency", action="store_true", help="Enable live latency telemetry logs")
    p_daemon.add_argument("--rt", action="store_true", help="Request real-time / low-nice priority")
    p_daemon.set_defaults(func=cmd_daemon)

    p_lat = sub.add_parser("latency", help="Measure real-time controller and shaping latency")
    p_lat.add_argument("-d", "--duration", type=float, default=5.0, help="Benchmark duration in seconds")
    p_lat.add_argument("-n", "--samples", type=int, default=None, help="Stop after N frames")
    p_lat.add_argument("--with-uinput", action="store_true", help="Include virtual uinput write overhead")
    p_lat.add_argument("--toggle-daemon", action="store_true", help="Send SIGUSR2 to running daemon")
    p_lat.set_defaults(func=cmd_latency)

    p_mon = sub.add_parser("monitor", help="Interactive ANSI terminal stick & trigger visualizer")
    p_mon.add_argument("--rate", type=float, default=30.0, help="Refresh rate in Hz (default: 30)")
    p_mon.add_argument("--device", default="", help="Optional substring to match physical gamepad")
    p_mon.set_defaults(func=cmd_monitor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        return cmd_list(args)
    return args.func(args)
