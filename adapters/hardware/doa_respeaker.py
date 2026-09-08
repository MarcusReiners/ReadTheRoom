import statistics
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


def raw_to_target_degrees(raw: float, front_reference_degrees: float,
                          offaxis_gain: float = 1.0) -> float:
    """Converts a raw array reading into a DOA-convention angle (90 = straight
    ahead, matching ServoTurntableAdapter's rotate_towards()/track_relative_angle()
    - track_relative_angle() derives its actual servo delta from
    target_angle_degrees - 90). Module-level and shared with
    scripts/simulate_doa.py so the two can't drift out of sync with each
    other the way an inline-duplicated copy would.

    The raw-front_reference_degrees difference is wrapped to the SIGNED
    range (-180, 180], not a plain % 360 into [0, 360) - confirmed on the
    bench this matters: since the servo moves linearly rather than wrapping
    around (0 and 360 are NOT the same position to it), a reading just past
    the front reference on the "high" side used to produce a target near
    360, which set_angle_immediate()'s linear clamp then read as a huge
    delta in the wrong direction, instead of the small, correct one on the
    other side of 90. Reducing to the shortest signed difference first means
    target - 90 is always the smallest real turn that reaches the intended
    direction, which is the only sensible reading for hardware with no wraparound."""
    diff = (raw - front_reference_degrees + 180.0) % 360.0 - 180.0
    # The array under-reports how far off-axis a source is: measured on this
    # mount, a true 45 read as 58.6 and a true 135 as 123.0, while 90 was
    # exact - a linear compression toward the front with gain 0.716
    # (R2 = 0.9998 over those three points). Dividing by that gain undoes it.
    # offaxis_gain=1.0 leaves the reading untouched.
    if offaxis_gain and offaxis_gain != 1.0:
        diff /= offaxis_gain
    return 90.0 + diff


def median_angle(angles: list[float]) -> float:
    """Median of several DOA-convention readings of the same source.

    A plain median can't be taken on raw angles - they live on a circle, so
    170 and -170 are 20 degrees apart but average to 0, pointing the exact
    wrong way. Each sample is first unwrapped to the branch nearest the first
    one, making them ordinary colinear numbers, then re-expressed on the same
    branch afterward.

    Median rather than mean deliberately: the array occasionally emits a
    single wildly-off reading (a reflection off a wall or monitor, a chair
    scrape landing mid-window), and one such outlier drags a mean of five
    samples by tens of degrees while leaving the median untouched.
    """
    if not angles:
        raise ValueError("median_angle() braucht mindestens einen Wert.")
    base = angles[0]
    unwrapped = [base + ((a - base + 180.0) % 360.0 - 180.0) for a in angles]
    return statistics.median(unwrapped)


class RespeakerDOAAdapter:
    """Reads onboard direction-of-arrival and voice activity from a
    ReSpeaker USB Mic Array v2.0.

    Uses the same vendor USB control-transfer protocol as Seeed's
    usb_4_mic_array/tuning.py, independent of the audio stream.
    """

    def __init__(self, front_reference_degrees: float = 0.0,
                 offaxis_gain: float = 1.0) -> None:
        """front_reference_degrees is whatever raw DOA value the array reports
        when a speaker is actually standing straight ahead of the physical
        mount - the array's own 0 has no relation to how it happens to be
        oriented once installed, same idea as the servo's home_offset_degrees.
        Tune by watching logged raw angles ("[DOA] Stimme erkannt bei X Grad")
        while standing dead ahead and setting DOA_FRONT_REFERENCE_DEGREES to
        that value; default 0 is just an unconfigured starting guess."""
        self._front_reference_degrees = front_reference_degrees
        self._offaxis_gain = offaxis_gain
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
        raw = float(self._read_param(*_DOAANGLE_PARAM))
        return raw_to_target_degrees(raw, self._front_reference_degrees, self._offaxis_gain)

    def get_voice_active(self) -> bool:
        """Onboard VAD flag - use this to gate on "loud enough"/speech-like sound
        instead of reacting to every DOA reading, most of which are ambient noise."""
        return bool(self._read_param(*_VOICEACTIVITY_PARAM))

    def close(self) -> None:
        usb.util.dispose_resources(self._dev)
