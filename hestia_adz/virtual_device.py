"""Persistent virtual Xbox 360 uinput receiver with D-pad hat mapping & EV_FF rumble passthrough."""

from __future__ import annotations

import inspect
import logging
import os
import struct
from dataclasses import dataclass

from evdev import AbsInfo, InputDevice, UInput, ecodes

LOG = logging.getLogger("adz.vdev")

INPUT_EVENT_STRUCT = struct.Struct("llHHi")
EVENT_SIZE = INPUT_EVENT_STRUCT.size

XBOX360_VENDOR = 0x045E
XBOX360_PRODUCT = 0x028E
XBOX360_VERSION = 0x0110
XBOX360_NAME = "Xbox Wireless Controller"

VIRTUAL_BUTTONS = (
    ecodes.BTN_SOUTH,
    ecodes.BTN_EAST,
    ecodes.BTN_NORTH,
    ecodes.BTN_WEST,
    ecodes.BTN_TL,
    ecodes.BTN_TR,
    ecodes.BTN_SELECT,
    ecodes.BTN_START,
    ecodes.BTN_MODE,
    ecodes.BTN_THUMBL,
    ecodes.BTN_THUMBR,
)

VIRTUAL_ABS = (
    (ecodes.ABS_X, AbsInfo(value=0, min=-32768, max=32767, fuzz=0, flat=0, resolution=0)),
    (ecodes.ABS_Y, AbsInfo(value=0, min=-32768, max=32767, fuzz=0, flat=0, resolution=0)),
    (ecodes.ABS_RX, AbsInfo(value=0, min=-32768, max=32767, fuzz=0, flat=0, resolution=0)),
    (ecodes.ABS_RY, AbsInfo(value=0, min=-32768, max=32767, fuzz=0, flat=0, resolution=0)),
    (ecodes.ABS_Z, AbsInfo(value=0, min=0, max=255, fuzz=0, flat=0, resolution=0)),
    (ecodes.ABS_RZ, AbsInfo(value=0, min=0, max=255, fuzz=0, flat=0, resolution=0)),
    (ecodes.ABS_HAT0X, AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)),
    (ecodes.ABS_HAT0Y, AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)),
)

VIRTUAL_FF = (
    ecodes.FF_RUMBLE,
    ecodes.FF_PERIODIC,
    ecodes.FF_SQUARE,
    ecodes.FF_TRIANGLE,
    ecodes.FF_SINE,
    ecodes.FF_GAIN,
)

DPAD_BUTTON_CODES = frozenset({
    ecodes.BTN_DPAD_UP,
    ecodes.BTN_DPAD_DOWN,
    ecodes.BTN_DPAD_LEFT,
    ecodes.BTN_DPAD_RIGHT,
})


@dataclass(slots=True)
class AxisRange:
    minimum: int
    maximum: int


class PersistentXboxReceiver:
    """Manages a persistent virtual Xbox 360 uinput device and forwards EV_FF rumble to hardware."""

    def __init__(self) -> None:
        self.ui: UInput | None = None
        self._batch = bytearray()
        self._held_buttons: set[int] = set()
        self._dpad_up = False
        self._dpad_down = False
        self._dpad_left = False
        self._dpad_right = False
        self._hat_x = 0
        self._hat_y = 0

        self._stored_effects: dict[int, object] = {}
        self._phys_effect_ids: dict[int, int] = {}
        self._last_gain: int | None = None

    @property
    def is_open(self) -> bool:
        return self.ui is not None

    @property
    def fd(self) -> int:
        return -1 if self.ui is None else self.ui.fd

    def open(self) -> None:
        if self.ui is not None:
            return
        events = {
            ecodes.EV_KEY: list(VIRTUAL_BUTTONS),
            ecodes.EV_ABS: list(VIRTUAL_ABS),
            ecodes.EV_FF: list(VIRTUAL_FF),
        }
        kwargs = {
            "events": events,
            "name": XBOX360_NAME,
            "vendor": XBOX360_VENDOR,
            "product": XBOX360_PRODUCT,
            "version": XBOX360_VERSION,
            "bustype": ecodes.BUS_USB,
        }
        sig = inspect.signature(UInput.__init__)
        if "max_effects" in sig.parameters:
            kwargs["max_effects"] = 16
        self.ui = UInput(**kwargs)
        os.set_blocking(self.ui.fd, False)
        LOG.info("Opened persistent virtual receiver %r (fd=%d)", XBOX360_NAME, self.ui.fd)

    def close(self) -> None:
        if self.ui is None:
            return
        try:
            self.ui.close()
        except OSError:
            pass
        self.ui = None
        self._batch.clear()
        self._held_buttons.clear()
        self._dpad_up = self._dpad_down = self._dpad_left = self._dpad_right = False
        self._hat_x = self._hat_y = 0
        self._stored_effects.clear()
        self._phys_effect_ids.clear()
        self._last_gain = None
        LOG.info("Closed virtual receiver")

    def queue_event(self, ev_type: int, ev_code: int, ev_value: int) -> None:
        self._batch.extend(INPUT_EVENT_STRUCT.pack(0, 0, ev_type, ev_code, ev_value))

    def flush(self) -> None:
        if not self._batch or self.ui is None:
            return
        self._batch.extend(INPUT_EVENT_STRUCT.pack(0, 0, ecodes.EV_SYN, ecodes.SYN_REPORT, 0))
        os.write(self.ui.fd, self._batch)
        self._batch.clear()

    def queue_button(self, code: int, value: int) -> None:
        pressed = 1 if value else 0
        if pressed:
            self._held_buttons.add(code)
        else:
            self._held_buttons.discard(code)
        self.queue_event(ecodes.EV_KEY, code, pressed)

    def queue_dpad_button(self, code: int, value: int) -> None:
        pressed = bool(value)
        if code == ecodes.BTN_DPAD_UP:
            self._dpad_up = pressed
        elif code == ecodes.BTN_DPAD_DOWN:
            self._dpad_down = pressed
        elif code == ecodes.BTN_DPAD_LEFT:
            self._dpad_left = pressed
        elif code == ecodes.BTN_DPAD_RIGHT:
            self._dpad_right = pressed

        new_x = (-1 if self._dpad_left else 0) + (1 if self._dpad_right else 0)
        new_y = (-1 if self._dpad_up else 0) + (1 if self._dpad_down else 0)
        if new_x != self._hat_x:
            self._hat_x = new_x
            self.queue_event(ecodes.EV_ABS, ecodes.ABS_HAT0X, new_x)
        if new_y != self._hat_y:
            self._hat_y = new_y
            self.queue_event(ecodes.EV_ABS, ecodes.ABS_HAT0Y, new_y)

    def queue_hat_axis(self, code: int, value: int) -> None:
        clamped = -1 if value < 0 else (1 if value > 0 else 0)
        if code == ecodes.ABS_HAT0X and clamped != self._hat_x:
            self._hat_x = clamped
            self.queue_event(ecodes.EV_ABS, ecodes.ABS_HAT0X, clamped)
        elif code == ecodes.ABS_HAT0Y and clamped != self._hat_y:
            self._hat_y = clamped
            self.queue_event(ecodes.EV_ABS, ecodes.ABS_HAT0Y, clamped)

    def reset_neutral(self) -> None:
        """Neutralize all axes and release any held buttons so state never sticks across hot-swaps."""
        if self.ui is None:
            return
        for btn in tuple(self._held_buttons):
            self.queue_event(ecodes.EV_KEY, btn, 0)
        self._held_buttons.clear()
        self._dpad_up = self._dpad_down = self._dpad_left = self._dpad_right = False
        self._hat_x = 0
        self._hat_y = 0

        for code in (
            ecodes.ABS_X,
            ecodes.ABS_Y,
            ecodes.ABS_RX,
            ecodes.ABS_RY,
            ecodes.ABS_Z,
            ecodes.ABS_RZ,
            ecodes.ABS_HAT0X,
            ecodes.ABS_HAT0Y,
        ):
            self.queue_event(ecodes.EV_ABS, code, 0)
        self.flush()

    def attach_physical_ff(self, phys_dev: InputDevice | None) -> None:
        """Re-upload any active virtual FF effects onto a newly connected physical controller."""
        self._phys_effect_ids.clear()
        if phys_dev is None or not self._stored_effects:
            return
        caps = phys_dev.capabilities()
        if ecodes.EV_FF not in caps:
            return
        for virt_id, effect in list(self._stored_effects.items()):
            try:
                effect.id = -1
                phys_id = phys_dev.upload_effect(effect)
                self._phys_effect_ids[virt_id] = phys_id
            except OSError as exc:
                LOG.debug("Failed to re-upload FF effect %d to physical device: %s", virt_id, exc)
        if self._last_gain is not None:
            try:
                phys_dev.write(ecodes.EV_FF, ecodes.FF_GAIN, self._last_gain)
            except OSError:
                pass

    def detach_physical_ff(self, phys_dev: InputDevice | None) -> None:
        """Stop and erase uploaded FF effects when a physical device is detached."""
        if phys_dev is not None and self._phys_effect_ids:
            for phys_id in list(self._phys_effect_ids.values()):
                try:
                    phys_dev.write(ecodes.EV_FF, phys_id, 0)
                    phys_dev.erase_effect(phys_id)
                except OSError:
                    pass
        self._phys_effect_ids.clear()

    def handle_uinput_events(self, phys_dev: InputDevice | None) -> None:
        """Drain EV_UINPUT and EV_FF events from uinput fd and forward rumble to phys_dev."""
        if self.ui is None:
            return
        while True:
            try:
                buf = os.read(self.ui.fd, EVENT_SIZE * 32)
            except BlockingIOError:
                break
            except OSError:
                break
            if not buf:
                break

            for offset in range(0, len(buf) - EVENT_SIZE + 1, EVENT_SIZE):
                _sec, _usec, ev_type, ev_code, ev_value = INPUT_EVENT_STRUCT.unpack_from(buf, offset)
                if ev_type == ecodes.EV_UINPUT:
                    if ev_code == ecodes.UI_FF_UPLOAD:
                        upload = self.ui.begin_upload(ev_value)
                        effect = upload.effect
                        virt_id = int(effect.id)
                        self._stored_effects[virt_id] = effect
                        if phys_dev is not None:
                            try:
                                caps = phys_dev.capabilities()
                                if ecodes.EV_FF in caps:
                                    effect.id = self._phys_effect_ids.get(virt_id, -1)
                                    phys_id = phys_dev.upload_effect(effect)
                                    self._phys_effect_ids[virt_id] = phys_id
                            except OSError as exc:
                                LOG.debug("Physical FF upload failed: %s", exc)
                        upload.retval = 0
                        self.ui.end_upload(upload)

                    elif ev_code == ecodes.UI_FF_ERASE:
                        erase = self.ui.begin_erase(ev_value)
                        virt_id = int(erase.effect_id)
                        self._stored_effects.pop(virt_id, None)
                        phys_id = self._phys_effect_ids.pop(virt_id, None)
                        if phys_dev is not None and phys_id is not None:
                            try:
                                phys_dev.erase_effect(phys_id)
                            except OSError:
                                pass
                        erase.retval = 0
                        self.ui.end_erase(erase)

                elif ev_type == ecodes.EV_FF:
                    if ev_code == ecodes.FF_GAIN:
                        self._last_gain = ev_value
                        if phys_dev is not None:
                            try:
                                phys_dev.write(ecodes.EV_FF, ecodes.FF_GAIN, ev_value)
                            except OSError:
                                pass
                    else:
                        phys_id = self._phys_effect_ids.get(ev_code)
                        if phys_dev is not None and phys_id is not None:
                            try:
                                phys_dev.write(ecodes.EV_FF, phys_id, ev_value)
                            except OSError:
                                pass
