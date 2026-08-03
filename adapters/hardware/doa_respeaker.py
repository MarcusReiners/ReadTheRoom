import struct

import usb.core
import usb.util

_VENDOR_ID = 0x2886
_PRODUCT_ID = 0x0018
_TIMEOUT_MS = 100000
_DOAANGLE_PARAM_ID = 21  # from Seeed's usb_4_mic_array/tuning.py PARAMETERS table


class RespeakerDOAAdapter:
    """Reads onboard direction-of-arrival from a ReSpeaker USB Mic Array v2.0.

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

    def get_direction_degrees(self) -> float:
        cmd = 0x80 | 0x40  # read + int type, per Seeed protocol
        response = self._dev.ctrl_transfer(
            usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
            0, cmd, _DOAANGLE_PARAM_ID, 8, _TIMEOUT_MS,
        )
        value, _ = struct.unpack("ii", response.tobytes())
        return float(value)

    def close(self) -> None:
        usb.util.dispose_resources(self._dev)
