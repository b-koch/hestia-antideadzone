# hestia-antideadzone (`adz`)

A lightweight, system-wide Linux daemon and CLI (`adz`) that removes forced in-game analog stick and trigger deadzones and shapes response curves at the kernel input level—without relying on Steam Input.

When active, the daemon reads your physical controller via `evdev`, applies per-stick and per-trigger deadzone, anti-deadzone, and response curve shaping, and re-emits a persistent virtual Xbox 360-style gamepad (`Xbox Wireless Controller`, `045e:028e`) through `uinput`. Buttons, D-pad inputs (normalized to `ABS_HAT0X`/`ABS_HAT0Y`), and force-feedback (`EV_FF`) rumble effects pass through seamlessly.

When paused (`adz pause`), the daemon releases its exclusive grab (`EVIOCGRAB`) on your physical controller and tears down the virtual device completely so games see your untouched native hardware.

> **AI Attribution:** This project was built collaboratively with an AI assistant (Google Gemini). See [Development & AI Attribution](#development--ai-attribution) for details.

## Features

* **Unified `adz` CLI + Background Daemon:** One executable handles background input shaping (`adz daemon`), profile switching (`adz switch <profile>`), pause/resume toggling (`adz pause`, `adz resume`, `adz toggle`), controller inspection (`adz devices`), safe config editing (`adz edit`, `adz validate`), live terminal visualization (`adz monitor`), and latency profiling (`adz latency`).
* **True Bypass / Pause Mode:** `adz pause` ungrabs your physical pad and removes the virtual Xbox 360 device immediately, letting you keep the systemd user service running permanently while opting in only for games that need it.
* **Instant Updates + `inotify` Hot-Reload:** CLI commands write atomically to `config.json` and signal the running daemon (`SIGHUP`) to apply changes immediately, while manual edits are picked up instantaneously via Linux `inotify` (with automatic fallback polling via `reload_interval`).
* **Event-Driven Hotplugging & Single-Instance Lock:** Uses non-blocking `pyudev` netlink monitoring to detect controller connections/disconnections without busy-polling, protected by an exclusive `flock` lock so duplicate daemon instances cannot spawn extra virtual controllers.
* **Stick & Trigger Shaping with 4096-Entry Curve LUTs:** Supports `radial`, `radial_unscaled`, `bowtie`, `axial`, `axial_unscaled`, `sloped_axial`, `sloped_axial_unscaled`, `hybrid`, `square`, `square_unscaled`, `squircle`, and `smooth_axial` stick geometries with inner/outer deadzones, plus independent left/right trigger deadzone, outer deadzone, anti-deadzone, and curve shaping.
* **Rumble Passthrough & D-Pad Normalization:** Forwards `EV_FF` rumble/periodic effects and gain from games back to the physical controller and normalizes `BTN_DPAD_*` buttons into Xbox 360 hat axes.
* **Optional Polling Rate Cap & Low-Jitter Runtime:** Coalesces high-polling-rate stick events (e.g., 500 Hz or 1000 Hz controllers) down to `max_output_hz` while sending buttons and triggers immediately, alongside `CLOCK_MONOTONIC` timestamps, cyclic GC suppression during active streaming, and optional real-time scheduling (`--rt`).

## Repository Structure

```text
hestia-antideadzone/
├── __main__.py                                      # Direct execution entry point
├── build.py                                         # Zero-dependency zipapp builder
├── Makefile                                         # Build & user-space install targets
├── config.example.json                              # Default configuration template
├── dist/
│   └── adz                                          # Pre-built executable Python zipapp
├── hestia_adz/
│   ├── __init__.py                                  # Package version metadata
│   ├── __main__.py                                  # Zipapp / python -m entry point
│   ├── cli.py                                       # Subcommands & shell completions
│   ├── config.py                                    # Schema validation, atomic JSON & compilation
│   ├── daemon.py                                    # Main epoll/selector loop, flock & hotplug
│   ├── monitor.py                                   # Live ANSI polar stick & trigger visualizer
│   ├── shapes.py                                    # 8 stick geometries, triggers & 4096-entry LUTs
│   ├── sys_runtime.py                               # inotify, sd_notify, CLOCK_MONOTONIC, RT & GC
│   ├── telemetry.py                                 # Microsecond latency recorder & benchmark
│   └── virtual_device.py                            # Persistent uinput Xbox 360 receiver & EV_FF
└── packaging/
    ├── Containerfile.snippet                        # Custom Bazzite / ublue-os image integration
    ├── systemd/
    │   ├── antideadzone.service                     # User service targeting ~/.local/bin/adz
    │   └── antideadzone.system-bin.service          # User service targeting /usr/bin/adz
    └── udev/
        └── 99-uinput.rules                          # Grants `input` group access to /dev/uinput
```

## Requirements

* **OS:** Linux with `evdev` and `uinput` kernel modules loaded
* **Python:** Python 3.10+
* **Python Packages** *(required only for `adz daemon`, `adz devices`, `adz monitor`, and `adz latency`; profile/pause/validation commands use only the Python standard library)*:
  * `evdev`
  * `pyudev`

Install dependencies via your distribution's package manager or `pip`:

```bash
# Fedora / Bazzite (layered or image build)
sudo dnf install python3-evdev python3-pyudev

# Arch Linux
sudo pacman -S python-evdev python-pyudev

# Debian / Ubuntu
sudo apt install python3-evdev python3-pyudev

# Or via pip
pip install evdev pyudev
```

## Permissions Setup

To run the daemon as a regular user (without `root`), your user needs read/write access to `/dev/input/event*` and `/dev/uinput`.

1. **Add your user to the `input` group:**
   ```bash
   sudo usermod -aG input "$USER"
   ```

2. **Grant `input` group access to `/dev/uinput` via udev:**

   Copy `packaging/udev/99-uinput.rules` to `/etc/udev/rules.d/99-uinput.rules`:
   ```udev
   KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"
   ```

3. **Reload udev rules and re-login** (or reboot) for group membership to take effect:
   ```bash
   sudo udevadm control --reload-rules
   sudo udevadm trigger
   ```

## Building from Source

The repository includes a pre-built zipapp at `dist/adz`. Whenever you modify any file in `hestia_adz/`, rebuild the single-file executable with:

```bash
python3 build.py
# Or via Make:
make build
```

This packages `hestia_adz/` and `__main__.py` into a compressed, executable Python zipapp at `dist/adz` (`#!/usr/bin/env python3`). You can also run directly from source without building via `python3 -m hestia_adz <command>`.

## Installation

### Option A: User-Space Installation (`~/.local/bin/adz`)

Ideal for testing changes immediately on immutable distributions like Bazzite without rebuilding the OS image:

```bash
make install-user
systemctl --user enable --now antideadzone.service
```

Or manually:

```bash
mkdir -p ~/.local/bin ~/.config/hestia/anti-deadzone ~/.config/systemd/user
cp dist/adz ~/.local/bin/adz
chmod +x ~/.local/bin/adz
cp config.example.json ~/.config/hestia/anti-deadzone/config.json
cp packaging/systemd/antideadzone.service ~/.config/systemd/user/antideadzone.service
systemctl --user daemon-reload
systemctl --user enable --now antideadzone.service
```

### Option B: System-Wide / Custom Bazzite (`ublue-os`) Image Installation

To bake `adz` into a custom immutable image (see `packaging/Containerfile.snippet`):

```dockerfile
COPY dist/adz /usr/lib/hestia/anti-deadzone/adz
COPY config.example.json /etc/hestia/anti-deadzone/config.json
COPY packaging/udev/99-uinput.rules /etc/udev/rules.d/99-uinput.rules
RUN chmod 0755 /usr/lib/hestia/anti-deadzone/adz && \
    chmod 0644 /etc/hestia/anti-deadzone/config.json /etc/udev/rules.d/99-uinput.rules && \
    ln -sf /usr/lib/hestia/anti-deadzone/adz /usr/bin/adz
```

Then install `packaging/systemd/antideadzone.system-bin.service` as `~/.config/systemd/user/antideadzone.service` and enable it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now antideadzone.service
```

> **Note:** If you previously tested a user-local binary in `~/.local/bin/adz` with a systemd drop-in override, remove `~/.local/bin/adz` and `~/.config/systemd/user/antideadzone.service.d/override.conf` after rebasing onto your updated OS image so `/usr/bin/adz` is used cleanly.

### Shell Completions (Optional)

```bash
# Bash
adz completions bash > ~/.local/share/bash-completion/completions/adz

# Zsh
adz completions zsh > ~/.zfunc/_adz

# Fish
adz completions fish > ~/.config/fish/completions/adz.fish
```

### Service Logs

```bash
journalctl --user -u antideadzone.service -f
```

## CLI Usage

Pass `-c <path>` / `--config <path>` (or set `ADZ_CONFIG`) before any subcommand to target an alternate configuration file. Resolution order: `-c/--config` -> `$ADZ_CONFIG` -> `~/.config/hestia/anti-deadzone/config.json` -> `/etc/hestia/anti-deadzone/config.json`.

### Profile & Daemon Control

```bash
# Show daemon state, paused/active status, active profile, and profile list
adz
adz list
adz list --names         # Print only profile names (one per line, for scripts/completions)

# Concise status output (useful for status bars or scripts)
adz status               # e.g. "active: rdr2 [daemon PID 1234]"
adz status --short       # e.g. "active:rdr2" or "paused:rdr2"
adz status --active      # e.g. "rdr2"

# Switch to a profile (automatically resumes if paused)
adz switch rdr2

# Switch profile without unpausing
adz switch rdr2 --keep-paused

# Disable the virtual device & release physical controller grab completely
adz pause

# Re-enable the virtual device & stick/trigger shaping
adz resume

# Flip between paused and active states
adz toggle

# Switch to built-in 0.0 passthrough while keeping the virtual device connected
adz switch off

# List connected evdev input devices
adz devices
```

### Configuration & Diagnostics

```bash
# Validate config.json syntax, keys, and numeric bounds
adz validate

# Edit config.json in $VISUAL / $EDITOR with automatic schema validation before saving
adz edit

# Live ANSI terminal stick & trigger polar visualizer (simulates profile when daemon is paused)
adz monitor
adz monitor --rate 60 --device "Generic X-Box pad"

# Measure kernel-to-userspace and shaping latency in microseconds
adz latency
adz latency -d 10 --with-uinput
adz latency --toggle-daemon    # Send SIGUSR2 to running daemon to toggle live telemetry logs

# Generate shell completions
adz completions bash           # Also supports: zsh, fish

# Run the daemon in the foreground
adz daemon
adz daemon --paused
adz daemon --verbose
adz daemon --latency
adz daemon --rt
```

### Per-Game Steam / Lutris Launch Options

If you leave the daemon paused by default (`adz pause`), you can activate a game's profile automatically on launch and pause again when the game exits:

* **Steam Launch Options:**
  ```bash
  adz switch rdr2 && %command%; adz pause
  ```
* **Lutris / Heroic Wrapper Script:**
  ```bash
  #!/usr/bin/env bash
  adz switch "$1"
  shift
  "$@"
  adz pause
  ```

> **Note on Double Input:** When active, `adz daemon` calls `grab()` (`EVIOCGRAB`) on the physical `evdev` device so games only see the virtual controller. Titles using SDL2/Steam/Proton that open `/dev/hidraw*` directly may still see the raw HID node unless Steam Input is disabled for that game.

## Configuration Reference

Configuration is stored in JSON. Any key starting with `_` (such as `_comment` or `_profile_help`) is ignored by `adz`.

### Global Options

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `paused` | `boolean` | `false` | When `true`, the daemon releases its grab on the physical controller and removes the virtual `uinput` device. Managed automatically by `adz pause` / `adz resume`. |
| `device_name_match` | `string` | `""` | Case-insensitive substring matched against the `evdev` device name. Empty matches the first physical gamepad found. |
| `active_profile` | `string` | `"default"` | Name of the active entry in `profiles`. `"off"` is always available as a built-in raw passthrough profile. |
| `profiles` | `object` | `{...}` | Dictionary of named profiles, each containing `"left"` and `"right"` stick objects, optional `"left_trigger"` / `"right_trigger"` objects, and an optional profile-level `"max_output_hz"`. |
| `max_output_hz` | `number` | `0` | Maximum stick output rate in Hz (`0` = unlimited). Can also be overridden inside an individual profile. |
| `reload_interval` | `number` | `1.0` | Fallback polling interval (in seconds) if Linux `inotify` is unavailable. |
| `rescan_interval` | `number` | `1.0` | Fallback polling interval (in seconds) if `pyudev` netlink monitoring is unavailable. |
| `debug_sample_rate` | `integer` | `0` | Log every $N$-th shaped stick event at `DEBUG` level (`0` disables). |
| `realtime_priority` | `boolean` | `false` | Attempt `SCHED_RR(10)` (or `nice(-10)`) scheduling priority on daemon startup for lower jitter. |

### Per-Stick Options (`left` / `right`)

Each profile configures `"left"` and `"right"` sticks independently:

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `deadzone` | `float` | `0.05` / `0.01` | Inner center gate ($0.0$–$1.0$) to filter out physical stick rest drift and noise. |
| `outer_deadzone` | `float` | `0.0` | Outer margin ($0.0$–$0.95$) subtracted from full deflection so worn sticks reach $100\%$ output before the physical rim. |
| `anti_deadzone` | `float` | `0.25` | Minimum output magnitude ($0.0$–$1.0$) applied once the stick leaves the deadzone, countering a game's built-in deadzone. |
| `curve` | `float` | `1.0` | Power response curve exponent applied after rescaling. `$1.0$` is linear; `$> 1.0$` gives finer precision near center; `$< 1.0$` is faster near center. Ignored by `_unscaled` shapes. |
| `deadzone_shape` | `string` | `"radial"` | Deadzone and anti-deadzone geometry (see table below). |
| `axial_deadzone` | `float` | `null` | Shape-specific secondary width/gain ($0.0$–$1.0$). Sets the cardinal/bowtie cone slope on `bowtie`/`sloped_axial`/`hybrid` (auto-derives from `anti_deadzone * 0.45` when `deadzone < 0.05`), diagonal squareness blend on `squircle` (default `0.55`), and cardinal axis ramp width on `smooth_axial` (default `0.06`). |
| `sloped_dominance_curve` | `float` | `1.0` | Controls how smoothly `anti_deadzone` ramps onto the minor axis in `sloped_axial` and `hybrid` (`0.0` = full boost immediately outside the bowtie; `1.0` = smooth Hermite ramp; `> 1.0` = stronger cardinal bias). |
| `outer_circle_clamp` | `boolean` | `false` (`true` on `hybrid`) | Clamps diagonal output vector magnitude to $\le 1.0$ so diagonals do not exceed cardinal turn speeds. |

### Per-Trigger Options (`left_trigger` / `right_trigger`)

Optional per-trigger shaping blocks normalized to Xbox 360 trigger output (`0`–`255`):

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `deadzone` | `float` | `0.0` | Inner pull threshold ($0.0$–$1.0$) below which trigger output stays `0`. |
| `outer_deadzone` | `float` | `0.0` | Outer margin ($0.0$–$0.95$) so full output (`255`) is reached before physical bottom-out. |
| `max` | `float` | `null` | Alternative shorthand (`0.05`–$1.0$) for `1.0 - outer_deadzone` (ignored if `outer_deadzone` is also set). |
| `anti_deadzone` | `float` | `0.0` | Minimum trigger output ($0.0$–$1.0$) applied once `deadzone` is crossed. |
| `curve` | `float` | `1.0` | Power response curve exponent applied across the active trigger travel. |

### Deadzone Shapes Explained

| Shape | Behavior & Best Use Case |
| :--- | :--- |
| **`radial`** | Circular center deadzone with vector-normalized anti-deadzone and rescaling. Preserves exact stick angles with zero diagonal distortion. Best general-purpose choice. |
| **`radial_unscaled`** | Circular deadzone that jumps straight to raw stick magnitude at the boundary instead of rescaling from $0$. Useful when a game already rescales its own deadzone. |
| **`bowtie`** | Locks pure horizontal/vertical movement inside an angular cone around the cardinal axes, then transitions continuously ($C^0$) into angle-preserving radial anti-deadzone outside the cone. Ideal for camera sticks where cardinal panning feels wobbly. |
| **`axial`** | Independent per-axis flat deadzone and anti-deadzone. Matches games with strict square/cross internal deadzones, though shallow diagonals near the boundary bend toward $45^\circ$. |
| **`axial_unscaled`** | Independent per-axis flat deadzone without rescaling from $0$. |
| **`sloped_axial`** | Wedge-shaped per-axis deadzone combined with a smooth Hermite dominance ramp on the minor axis. Defeats square in-game deadzones while avoiding sudden cardinal-to-diagonal snapping. |
| **`sloped_axial_unscaled`** | Unscaled variant of `sloped_axial`. |
| **`hybrid`** | Two-stage shaping: circular center deadzone + single radial response curve first, followed by bowtie per-axis shaping with symmetric diagonal anti-deadzone and outer-circle normalization. |
| **`square`** | Chebyshev ($L_\infty$) square deadzone and square outer boundary with rescaling from $0$. |
| **`square_unscaled`** | Unscaled variant of `square` without rescaling from $0$ after leaving the square deadzone. |
| **`squircle`** | Circular inner deadzone that smoothly blends toward a square outer boundary along diagonals (`axial_deadzone` controls squareness, default `0.55`). |
| **`smooth_axial`** | Circular inner deadzone followed by independent per-axis $C^1$ Hermite anti-deadzone ramps (`axial_deadzone` controls ramp width, default `0.06`), eliminating sudden axis snapping. |

## Development & AI Attribution

This repository was developed through human–AI collaboration rather than written solo from scratch:

* **Project Direction & Verification:** Problem definition, system requirements, deadzone geometry specifications, Linux/Bazzite integration, live hardware testing, and bug reproduction were driven by the repository maintainer.
* **Code & Documentation Drafting:** Implementation of the Python modules, mathematical shaping LUTs, `uinput`/`evdev` event loop, CLI tooling, and documentation were co-authored with **Google Gemini** and iteratively reviewed and tested on real controller hardware.
