import struct

import usb.core
import usb.util

_VENDOR_ID = 0x2886
_PRODUCT_ID = 0x0018
_TIMEOUT_MS = 100000

# (param_id, cmd_base) pairs, from Seeed's usb_4_mic_array/tuning.py PARAMETERS table.
_DOAANGLE_PARAM = (21, 0x00)
_VOICEACTIVITY_PARAM = (19, 0x20)


class RespeakerDOAAdapter:
    """Reads onboard direction-of-arrival and voice activity from a
    ReSpeaker USB Mic Array v2.0.

    Uses the same vendor USB control-transfer protocol as Seeed's
    usb_4_mic_array/tuning.py, independent of the audio stream.
    """

    def __init__(self) -> None:
        self._dev = usb.core.find(idVendor=_VENDOR_ID, idProduct=_PRODUCT_ID)
        if self._dev is None:
            raise RuntimeError(
                "ReSpeaker Mic Array (USB 2886:0018) nicht gefunden. "
                "USB-Verbindung und udev-Regel pruefen."
            )

    def _read_param(self, param_id: int, cmd_base: int) -> int:
        cmd = 0x80 | cmd_base | 0x40  # read + int type, per Seeed protocol
        response = self._dev.ctrl_transfer(
            usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
            0, cmd, param_id, 8, _TIMEOUT_MS,
        )
        value, _ = struct.unpack("ii", response.tobytes())
        return value

    def get_direction_degrees(self) -> float:
        return float(self._read_param(*_DOAANGLE_PARAM))

    def get_voice_active(self) -> bool:
        """Onboard VAD flag - use this to gate on "loud enough"/speech-like sound
        instead of reacting to every DOA reading, most of which are ambient noise."""
        return bool(self._read_param(*_VOICEACTIVITY_PARAM))

    def close(self) -> None:
        usb.util.dispose_resources(self._dev)
