import struct
import time

import usb.core
import usb.util

_VENDOR_ID = 0x2886
_PRODUCT_ID = 0x0018
_TIMEOUT_MS = 100000
_RETRY_ATTEMPTS = 3
_RETRY_DELAY_S = 0.05

# (param_id, cmd_base) pairs, from Seeed's usb_4_mic_array/tuning.py PARAMETERS table.
_DOAANGLE_PARAM = (21, 0x00)
_VOICEACTIVITY_PARAM = (19, 0x20)


class RespeakerDOAAdapter:
    """Reads onboard direction-of-arrival and voice activity from a
    ReSpeaker USB Mic Array v2.0.

    Uses the same vendor USB control-transfer protocol as Seeed's
    usb_4_mic_array/tuning.py, independent of the audio stream.
    """

    def __init__(self, front_reference_degrees: float = 0.0) -> None:
        """front_reference_degrees is whatever raw DOA value the array reports
        when a speaker is actually standing straight ahead of the physical
        mount - the array's own 0 has no relation to how it happens to be
        oriented once installed, same idea as the servo's home_offset_degrees.
        Tune by watching logged raw angles ("[DOA] Stimme erkannt bei X Grad")
        while standing dead ahead and setting DOA_FRONT_REFERENCE_DEGREES to
        that value; default 0 is just an unconfigured starting guess."""
        self._front_reference_degrees = front_reference_degrees
        self._dev = usb.core.find(idVendor=_VENDOR_ID, idProduct=_PRODUCT_ID)
        if self._dev is None:
            raise RuntimeError(
                "ReSpeaker Mic Array (USB 2886:0018) nicht gefunden. "
                "USB-Verbindung und udev-Regel pruefen."
            )

    def _read_param(self, param_id: int, cmd_base: int) -> int:
        cmd = 0x80 | cmd_base | 0x40  # read + int type, per Seeed protocol
        # Back-to-back control transfers from two polling loops (DOA tracking
        # and VAD-triggered recording) occasionally stall this endpoint with a
        # transient USBError (Pipe error) - usually clears itself on the very
        # next attempt, so a short retry is cheaper than treating it as fatal.
        last_error: usb.core.USBError | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = self._dev.ctrl_transfer(
                    usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
                    0, cmd, param_id, 8, _TIMEOUT_MS,
                )
                break
            except usb.core.USBError as e:
                last_error = e
                if attempt < _RETRY_ATTEMPTS - 1:
                    time.sleep(_RETRY_DELAY_S)
        else:
            raise last_error

        value, _ = struct.unpack("ii", response.tobytes())
        return value

    def get_direction_degrees(self) -> float:
        """Returns a DOA-convention angle (90 = straight ahead, matching
        ServoTurntableAdapter's rotate_towards()), as a plain circular shift
        of the array's raw 0-360 reading by front_reference_degrees - modulo
        360, deliberately with NO attempt to resolve it to a "shortest path"
        signed value here. That resolution has to happen against the servo's
        actual reachable window (see ServoTurntableAdapter._nearest_reachable_angle),
        not against an arbitrary +/-180 cut from the front reference - doing
        it here caused two nearly-identical real-world directions, just
        either side of "directly behind", to snap to opposite ends of the
        safe window instead of both landing on whichever edge is truly closer."""
        raw = float(self._read_param(*_DOAANGLE_PARAM))
        return (90.0 + (raw - self._front_reference_degrees)) % 360.0

    def get_voice_active(self) -> bool:
        """Onboard VAD flag - use this to gate on "loud enough"/speech-like sound
        instead of reacting to every DOA reading, most of which are ambient noise."""
        return bool(self._read_param(*_VOICEACTIVITY_PARAM))

    def close(self) -> None:
        usb.util.dispose_resources(self._dev)
