import logging

logger = logging.getLogger(__name__)


class DummyTurntableAdapter:
    def __init__(self) -> None:
        self.current_heading_degrees = 90.0  # 90 degrees = front-facing home position
        self.home_offset_degrees = 0.0

    def rotate_towards(self, target_angle_degrees: float) -> None:
        self.current_heading_degrees = target_angle_degrees

    def home(self) -> None:
        self.current_heading_degrees = 90.0

    def set_angle_immediate(self, angle_degrees: float) -> None:
        self.current_heading_degrees = angle_degrees

    def set_doa_angle_immediate(self, target_angle_degrees: float) -> None:
        self.set_angle_immediate(target_angle_degrees)

    def track_relative_angle(self, target_angle_degrees: float) -> None:
        self.current_heading_degrees = self.current_heading_degrees + (target_angle_degrees - 90.0)

    def set_home_offset(self, offset_degrees: float) -> None:
        self.home_offset_degrees = offset_degrees

    def set_raw_angle(self, angle_degrees: float) -> float:
        self.current_heading_degrees = angle_degrees
        return angle_degrees

    def clamp_raw_angle(self, angle_degrees: float) -> float:
        return angle_degrees

    def offset_for_raw_angle(self, angle_degrees: float) -> float:
        return angle_degrees - 90.0

    def raw_angle_for_offset(self, offset_degrees: float) -> float:
        return 90.0 + offset_degrees


class ServoTurntableAdapter:
    def __init__(
        self,
        pin: int,
        min_angle: float,
        max_angle: float,
        hardware_min_angle: float = 0.0,
        hardware_max_angle: float = 360.0,
        min_pulse_width: float = 0.0005,
        max_pulse_width: float = 0.0025,
        use_pigpio: bool = False,
        home_offset_degrees: float = 0.0,
        move_to_home_on_start: bool = True,
    ) -> None:
        """min_angle/max_angle is the safe clamp every commanded move is restricted
        to (cable-safety window); hardware_min_angle/hardware_max_angle is the
        servo's true mechanical range, used only to calibrate pulse-width-to-angle.
        Conflating the two stretches the full pulse range across just the safe
        window, turning every in-window move into a near-full physical rotation.

        use_pigpio switches gpiozero's global pin factory to pigpio's DMA-timed
        PWM instead of its default software-timed PWM thread - fixes a digital
        servo chattering while holding a fixed position (still while tracking a
        moving target) by removing the OS-scheduling jitter it was chasing.
        Requires `sudo pigpiod` running and the `pigpio` package installed.

        home_offset_degrees shifts the whole safe window by a fixed amount to
        compensate for the servo horn's mounted orientation - a 360-degree servo
        has no inherent "forward" until it's bolted on, so whatever the mount
        ended up at needs to be dialed in in software. The window's width never
        changes, only where it sits on the servo's true 0-360 range - cable
        safety holds regardless of the offset. Adjustable later via
        set_home_offset() without remounting the horn.

        move_to_home_on_start=False skips driving the servo to that computed
        position at construction time - for calibrate_servo_home.py, which
        should sit exactly where it physically already is on launch instead of
        jumping to a stale computed home before the operator has even started
        adjusting it by hand. Normal/production use (main.py, test scripts)
        wants the default True - moving to a known position on startup.
        """
        if use_pigpio:
            from gpiozero import Device
            from gpiozero.pins.pigpio import PiGPIOFactory

            Device.pin_factory = PiGPIOFactory()

        from gpiozero import AngularServo

        self._base_min_angle = min_angle
        self._base_max_angle = max_angle
        self._hardware_min_angle = hardware_min_angle
        self._hardware_max_angle = hardware_max_angle
        self.home_offset_degrees = home_offset_degrees
        self._min_angle, self._max_angle = self._windowed_range(home_offset_degrees)

        self.current_heading_degrees = (self._min_angle + self._max_angle) / 2
        self._servo = AngularServo(
            pin,
            initial_angle=self.current_heading_degrees % 360.0 if move_to_home_on_start else None,
            min_angle=hardware_min_angle,
            max_angle=hardware_max_angle,
            min_pulse_width=min_pulse_width,
            max_pulse_width=max_pulse_width,
        )
        logger.info(
            "[Servo] GPIO%s bereit, sicherer Bereich %.0f-%.0f Grad (Offset %.1f, Hardware %.0f-%.0f Grad).",
            pin, self._min_angle, self._max_angle, home_offset_degrees, hardware_min_angle, hardware_max_angle,
        )

    def _windowed_range(self, offset_degrees: float) -> tuple[float, float]:
        """A 360-degree positional servo has no real edge to avoid - -25 and
        335 degrees are the same physical position. So unlike an earlier
        version of this method, there's no "shift the window to stay inside
        0-360" logic here: lo is just normalized into [0, 360) and hi is
        simply lo+width, deliberately left unwrapped (may exceed 360) so
        everything that uses this pair (center, set_angle_immediate's clamp)
        can keep doing plain linear arithmetic in this pair's own consistent
        frame. Only the single angle actually written to hardware gets
        wrapped back to [0, 360) with a final %, in set_angle_immediate()."""
        width = self._base_max_angle - self._base_min_angle
        lo = (self._base_min_angle + offset_degrees) % 360.0
        hi = lo + width
        return lo, hi

    def set_home_offset(self, offset_degrees: float) -> None:
        """Live-adjusts the home offset (e.g. from a calibration script/web
        endpoint's nudge-then-save flow). Does not move the servo itself -
        call home() afterward to actually drive there."""
        self.home_offset_degrees = offset_degrees
        self._min_angle, self._max_angle = self._windowed_range(offset_degrees)

    def set_raw_angle(self, angle_degrees: float) -> float:
        """Drives the servo to an exact angle, wrapped onto its true hardware
        range (a full circle - 0 and 360 are the same position) - NOT clamped
        to the cable-safety window. For interactive home calibration only,
        while a person is watching and nudging by hand: the safety window
        exists to bound autonomous conversation-driven movement, not a
        supervised calibration session, so it shouldn't fight you while
        you're trying to find where "forward" actually is. Use
        offset_for_raw_angle() afterward to convert the angle you land on
        into a home offset, then set_home_offset() to actually apply the
        (still cable-safe) window for normal operation - this method never
        touches that window itself.

        Returns the actually-applied (wrapped) angle - callers MUST track
        this return value rather than their own running total, or their
        tracked position silently drifts away from where the servo really is
        once a requested angle goes outside a single 0-360 turn."""
        angle_degrees = self.clamp_raw_angle(angle_degrees)
        self._servo.angle = angle_degrees
        return angle_degrees

    def clamp_raw_angle(self, angle_degrees: float) -> float:
        """Same wrap set_raw_angle() applies, without moving the servo - for
        bookkeeping that needs to match what a real call would resolve to."""
        span = self._hardware_max_angle - self._hardware_min_angle
        return self._hardware_min_angle + (angle_degrees - self._hardware_min_angle) % span

    def offset_for_raw_angle(self, angle_degrees: float) -> float:
        """The home_offset_degrees that makes angle_degrees the center of the
        (still 140-degree-wide) safe window, i.e. what set_raw_angle() found
        during calibration becomes the new "straight ahead" for normal use."""
        return angle_degrees - self._default_center_angle

    def raw_angle_for_offset(self, offset_degrees: float) -> float:
        """The real hardware angle (normalized to [0, 360)) a given home
        offset currently resolves to - e.g. to resume calibration from
        wherever the last saved offset actually sits."""
        lo, hi = self._windowed_range(offset_degrees)
        return (lo + hi) / 2 % 360.0

    @property
    def _default_center_angle(self) -> float:
        return (self._base_min_angle + self._base_max_angle) / 2

    def rotate_towards(self, target_angle_degrees: float) -> None:
        """Moves immediately to target_angle_degrees, every call - no
        smoothing/deadband/cooldown. Noise rejection is the caller's job:
        start_doa_tracking() only calls this while the ReSpeaker's onboard
        VAD reports actual voice activity, so a still room simply never
        calls this rather than being filtered out here after the fact.

        Uses track_relative_angle(), not set_doa_angle_immediate() - the mic
        array is mounted on the moving head, so every live reading has to be
        interpreted relative to the head's current position, not home."""
        self.track_relative_angle(target_angle_degrees)

    def home(self) -> None:
        """Return to the front-facing center position - a deliberate command,
        not a DOA reading."""
        center = (self._min_angle + self._max_angle) / 2
        self.current_heading_degrees = center
        self._servo.angle = center % 360.0
        logger.info("[Servo] Home-Position (%.0f Grad).", center % 360.0)

    def set_angle_immediate(self, angle_degrees: float) -> None:
        """Directly drives the servo to an exact angle already expressed in
        the safe window's own (possibly-unwrapped, e.g. up to 475 for a
        window that wraps past 360) coordinates - NOT the DOA
        90-degrees-is-home convention - for deliberate test/calibration
        movements, same idea as home(). Clamped to the window using plain
        linear comparison, which is correct here specifically because
        _min_angle/_max_angle are always in this same unwrapped frame
        (self._max_angle = self._min_angle + width, never independently
        wrapped) - only the final write to hardware gets wrapped back to
        [0, 360) with a %, since gpiozero itself only accepts angles in that
        range."""
        angle_degrees = max(self._min_angle, min(self._max_angle, angle_degrees))
        self.current_heading_degrees = angle_degrees
        self._servo.angle = angle_degrees % 360.0

    def set_doa_angle_immediate(self, target_angle_degrees: float) -> None:
        """Like set_angle_immediate(), but target_angle_degrees is in the DOA
        convention (90 = home/straight ahead) instead of the safe window's own
        coordinates, applied relative to HOME - for one-off test/calibration
        moves (e.g. home_servo.py --angle), not live tracking. See
        track_relative_angle() for why live DOA tracking can't use this."""
        center = (self._min_angle + self._max_angle) / 2
        raw_target = center + (target_angle_degrees - 90.0)
        self.set_angle_immediate(self._nearest_reachable_angle(raw_target))

    def track_relative_angle(self, target_angle_degrees: float) -> None:
        """Like set_doa_angle_immediate(), but applied relative to wherever
        the head is CURRENTLY pointing instead of home. The mic array is
        mounted rigidly on the moving head, so its own DOA reading (90 =
        aligned with the array's own nose) is always relative to the head's
        current orientation, not a fixed room direction - after the head has
        already turned once, "home" and "wherever the nose is now" are two
        different things, and anchoring to home would compound errors on
        every subsequent reading. This is what live DOA tracking must use;
        set_doa_angle_immediate() is for one-off moves where "relative to
        home" is what's actually wanted (e.g. explicit angle tests)."""
        raw_target = self.current_heading_degrees + (target_angle_degrees - 90.0)
        self.set_angle_immediate(self._nearest_reachable_angle(raw_target))

    def _nearest_reachable_angle(self, angle_degrees: float) -> float:
        """angle_degrees can be any real number, wrapped or not - normalizes
        via its circular offset from _min_angle rather than comparing raw
        numbers directly, so this works regardless of how _min_angle/_max_angle
        happen to sit relative to the 0/360 seam."""
        width = self._max_angle - self._min_angle
        offset_from_min = (angle_degrees - self._min_angle) % 360.0
        if offset_from_min <= width:
            return self._min_angle + offset_from_min
        dist_to_min = 360.0 - offset_from_min
        dist_to_max = offset_from_min - width
        return self._min_angle if dist_to_min <= dist_to_max else self._max_angle

    def stop(self) -> None:
        self._servo.detach()
