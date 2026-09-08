import logging
import time

logger = logging.getLogger(__name__)


class DummyTurntableAdapter:
    def __init__(self) -> None:
        self.current_heading_degrees = 90.0  # 90 degrees = front-facing home position
        self.home_offset_degrees = 0.0

    def rotate_towards(self, target_angle_degrees: float, ramp_duration_s: float | None = None) -> None:
        self.current_heading_degrees = target_angle_degrees

    def home(self, ramp_duration_s: float | None = None) -> None:
        self.current_heading_degrees = 90.0

    def set_angle_immediate(self, angle_degrees: float) -> None:
        self.current_heading_degrees = angle_degrees

    def set_doa_angle_immediate(self, target_angle_degrees: float) -> None:
        self.set_angle_immediate(target_angle_degrees)

    def track_relative_angle(self, target_angle_degrees: float, ramp_duration_s: float | None = None) -> None:
        self.current_heading_degrees = self.current_heading_degrees + (target_angle_degrees - 90.0)

    def predict_relative_move(self, target_angle_degrees: float) -> float:
        return abs(target_angle_degrees - 90.0)

    def predict_home_move(self) -> float:
        return abs(self.current_heading_degrees - 90.0)

    @property
    def safe_min_angle(self) -> float:
        return 0.0

    @property
    def safe_max_angle(self) -> float:
        return 180.0

    def set_safe_range(self, min_angle: float, max_angle: float) -> None:
        pass

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

            try:
                Device.pin_factory = PiGPIOFactory()
            except OSError as e:
                # PiGPIOFactory() connects to the pigpiod daemon's socket at
                # construction time - if it's not running (a plain
                # `sudo pigpiod` doesn't survive a reboot; needs
                # `sudo systemctl enable --now pigpiod` to persist), this
                # raises here immediately rather than silently falling back
                # to software PWM. Re-raising with the fix inline rather than
                # gpiozero's raw connection-refused error, which doesn't say
                # what to do about it.
                raise RuntimeError(
                    "SERVO_USE_PIGPIO ist gesetzt, aber pigpiod ist nicht erreichbar. "
                    "Starten mit: sudo systemctl enable --now pigpiod (persistiert "
                    "einen Reboot, anders als ein einmaliges `sudo pigpiod`)."
                ) from e

        from gpiozero import AngularServo, Device

        # Logged unconditionally (not just when use_pigpio is set) so a
        # twitching/chattering report can be checked against the actual PWM
        # backend from the log, rather than assuming SERVO_USE_PIGPIO is
        # doing what it's supposed to. "PiGPIOFactory" is DMA-timed and
        # jitter-free; anything else (e.g. "LGPIOFactory", "RPiGPIOFactory")
        # is software-timed PWM and will chatter while holding a fixed angle
        # under load - see the SERVO_USE_PIGPIO comment in config.py.
        logger.info(
            "[Servo] PWM-Backend: %s (SERVO_USE_PIGPIO=%s)",
            type(Device.pin_factory).__name__, use_pigpio,
        )

        self._hardware_min_angle = hardware_min_angle
        self._hardware_max_angle = hardware_max_angle
        # Clamp the requested safe window into what the servo can actually
        # reach. The window can arrive from a saved app_settings.json written
        # before SERVO_HARDWARE_MIN/MAX_ANGLE was corrected (a 0-360 window
        # persisted from when the hardware range was wrongly believed to be
        # 360), which bypasses set_safe_range()'s validation entirely by
        # coming in through the constructor. Without this, safe_min_angle/
        # safe_max_angle keep reporting the impossible saved values - the web
        # app and the startup log both showed "0-360" on 0-180 hardware.
        self._base_min_angle = max(min_angle, hardware_min_angle)
        self._base_max_angle = min(max_angle, hardware_max_angle)
        if (self._base_min_angle, self._base_max_angle) != (min_angle, max_angle):
            logger.warning(
                "[Servo] Sicherer Bereich %.0f-%.0f liegt ausserhalb des Hardware-Bereichs "
                "%.0f-%.0f - auf %.0f-%.0f begrenzt. In der Web-App neu setzen, um die "
                "gespeicherte Einstellung zu korrigieren.",
                min_angle, max_angle, hardware_min_angle, hardware_max_angle,
                self._base_min_angle, self._base_max_angle,
            )
        self.home_offset_degrees = home_offset_degrees
        self._min_angle, self._max_angle = self._windowed_range(home_offset_degrees)

        self.current_heading_degrees = (self._min_angle + self._max_angle) / 2
        self._servo = AngularServo(
            pin,
            initial_angle=self.current_heading_degrees if move_to_home_on_start else None,
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
        """Despite being labeled a "360-degree servo", this unit does NOT
        move circularly - confirmed on the bench: commanded to go from 30 to
        -10 (=350 by wraparound), it swept the entire long way around through
        40, 50, 60... instead of the short way. Its angle-to-pulse-width
        mapping is a straight line, not a circle: there is no "the other way
        around" for it, ever. So the window is kept inside hardware bounds by
        shifting it (preserving width) rather than by wrapping - the earlier
        wraparound-based version of this method was solving a problem that
        doesn't physically exist on this hardware.
        """
        width = self._base_max_angle - self._base_min_angle
        hardware_width = self._hardware_max_angle - self._hardware_min_angle

        # A window at least as wide as the servo's whole travel can't be
        # shifted anywhere - it simply IS the full range, and there's no room
        # left for an offset to move it into. Handled explicitly because the
        # shift-to-fit below can't converge in that case: it used to push the
        # window down to land hi on hardware_max without re-checking lo, and
        # silently returned an out-of-bounds lower edge (a 360-wide window on
        # 0-180 hardware came out as -180..180). gpiozero then rejected every
        # negative angle and tracking died on each move.
        if width >= hardware_width:
            return self._hardware_min_angle, self._hardware_max_angle

        lo = self._base_min_angle + offset_degrees
        hi = lo + width

        if lo < self._hardware_min_angle:
            shift = self._hardware_min_angle - lo
            lo += shift
            hi += shift
        if hi > self._hardware_max_angle:
            shift = hi - self._hardware_max_angle
            lo -= shift
            hi -= shift

        return lo, hi

    @property
    def safe_min_angle(self) -> float:
        return self._base_min_angle

    @property
    def safe_max_angle(self) -> float:
        return self._base_max_angle

    def set_safe_range(self, min_angle: float, max_angle: float) -> None:
        """Live-adjusts the cable-safety window the head is allowed to turn
        within (from the web app's settings tab). Widening it lets the head
        follow speakers further off to the sides; the ceiling is how far the
        cabling running into the head can take being wound before something
        gets pulled, which is a physical property of this build and not
        something the software can discover - so this trusts the caller and
        only enforces that the window is non-empty and fits the servo's
        actual mechanical range.

        current_heading_degrees is re-clamped into the new window, otherwise
        a narrowed range would leave the tracker believing the head sits
        somewhere it is no longer allowed to be, and every subsequent
        relative move would be computed from that impossible position."""
        if max_angle <= min_angle:
            raise ValueError("max_angle muss groesser als min_angle sein.")
        span = max_angle - min_angle
        if span > self._hardware_max_angle - self._hardware_min_angle:
            raise ValueError("Bereich ist breiter als der mechanische Bereich des Servos.")

        self._base_min_angle = min_angle
        self._base_max_angle = max_angle
        self._min_angle, self._max_angle = self._windowed_range(self.home_offset_degrees)
        self.current_heading_degrees = max(
            self._min_angle, min(self._max_angle, self.current_heading_degrees)
        )
        logger.info(
            "[Servo] Sicherer Bereich jetzt %.0f-%.0f Grad (Fenster %.0f-%.0f).",
            min_angle, max_angle, self._min_angle, self._max_angle,
        )

    def set_home_offset(self, offset_degrees: float) -> None:
        """Live-adjusts the home offset (e.g. from a calibration script/web
        endpoint's nudge-then-save flow). Does not move the servo itself -
        call home() afterward to actually drive there."""
        self.home_offset_degrees = offset_degrees
        self._min_angle, self._max_angle = self._windowed_range(offset_degrees)

    def set_raw_angle(self, angle_degrees: float) -> float:
        """Drives the servo to an exact angle, clamped to its true hardware
        range - NOT clamped to the cable-safety window. For interactive home
        calibration only, while a person is watching and nudging by hand: the
        safety window exists to bound autonomous conversation-driven
        movement, not a supervised calibration session, so it shouldn't fight
        you while you're trying to find where "forward" actually is. Use
        offset_for_raw_angle() afterward to convert the angle you land on
        into a home offset, then set_home_offset() to actually apply the
        (still cable-safe) window for normal operation - this method never
        touches that window itself.

        This is a plain linear clamp, not a wrap - the servo's angle-to-pulse-width
        mapping is a straight line (confirmed on the bench: commanded to go
        the "short way" across the 0/360 seam, it swept the long way around
        instead), so 0 and 360 are NOT the same position to it, and wrapping
        past either end would command a huge, wrong physical move rather than
        a small one.

        Returns the actually-applied (clamped) angle - callers MUST track
        this return value rather than their own running total, or their
        tracked position silently drifts away from where the servo really is
        once a requested angle goes outside [hardware_min_angle,
        hardware_max_angle]."""
        angle_degrees = self.clamp_raw_angle(angle_degrees)
        self._servo.angle = angle_degrees
        return angle_degrees

    def clamp_raw_angle(self, angle_degrees: float) -> float:
        """Same clamp set_raw_angle() applies, without moving the servo - for
        bookkeeping that needs to match what a real call would resolve to."""
        return max(self._hardware_min_angle, min(self._hardware_max_angle, angle_degrees))

    def offset_for_raw_angle(self, angle_degrees: float) -> float:
        """The home_offset_degrees that makes angle_degrees the center of the
        (still 140-degree-wide) safe window, i.e. what set_raw_angle() found
        during calibration becomes the new "straight ahead" for normal use."""
        return angle_degrees - self._default_center_angle

    def raw_angle_for_offset(self, offset_degrees: float) -> float:
        """The real hardware angle a given home offset currently resolves to
        - e.g. to resume calibration from wherever the last saved offset
        actually sits. _windowed_range() already keeps lo/hi inside
        [0, 360], so their midpoint needs no further normalization."""
        lo, hi = self._windowed_range(offset_degrees)
        return (lo + hi) / 2

    @property
    def _default_center_angle(self) -> float:
        return (self._base_min_angle + self._base_max_angle) / 2

    def rotate_towards(self, target_angle_degrees: float, ramp_duration_s: float | None = None) -> None:
        """Moves to target_angle_degrees, every call - no deadband/cooldown.
        Noise rejection is the caller's job: start_doa_tracking() only calls
        this while the ReSpeaker's onboard VAD reports actual voice activity,
        so a still room simply never calls this rather than being filtered
        out here after the fact.

        Uses track_relative_angle(), not set_doa_angle_immediate() - the mic
        array is mounted on the moving head, so every live reading has to be
        interpreted relative to the head's current position, not home.

        ramp_duration_s: see track_relative_angle()."""
        self.track_relative_angle(target_angle_degrees, ramp_duration_s=ramp_duration_s)

    def home(self, ramp_duration_s: float | None = None) -> None:
        """Return to the front-facing center position - a deliberate command,
        not a DOA reading."""
        center = (self._min_angle + self._max_angle) / 2
        if ramp_duration_s and ramp_duration_s > 0:
            self._ramp_to(center, ramp_duration_s)
        else:
            self.current_heading_degrees = center
            self._servo.angle = center
        logger.info("[Servo] Home position (%.0f deg).", center)

    def set_angle_immediate(self, angle_degrees: float) -> None:
        """Directly drives the servo to an exact angle already expressed in
        the safe window's own coordinates - NOT the DOA 90-degrees-is-home
        convention - for deliberate test/calibration movements, same idea as
        home(). Plain linear clamp to [_min_angle, _max_angle]: this
        hardware's angle-to-pulse-width mapping is a straight line, not a
        circle (confirmed on the bench - it takes the long way around rather
        than wrapping), and _windowed_range() already keeps _min_angle/_max_angle
        inside [0, 360] by shifting rather than wrapping, so no extra
        normalization is needed here."""
        angle_degrees = max(self._min_angle, min(self._max_angle, angle_degrees))
        self.current_heading_degrees = angle_degrees
        self._servo.angle = angle_degrees

    def set_doa_angle_immediate(self, target_angle_degrees: float) -> None:
        """Like set_angle_immediate(), but target_angle_degrees is in the DOA
        convention (90 = home/straight ahead) instead of the safe window's own
        coordinates, applied relative to HOME - for one-off test/calibration
        moves (e.g. home_servo.py --angle), not live tracking. See
        track_relative_angle() for why live DOA tracking can't use this."""
        center = (self._min_angle + self._max_angle) / 2
        raw_target = center + (target_angle_degrees - 90.0)
        self.set_angle_immediate(raw_target)

    def track_relative_angle(self, target_angle_degrees: float, ramp_duration_s: float | None = None) -> None:
        """Like set_doa_angle_immediate(), but applied relative to wherever
        the head is CURRENTLY pointing instead of home. The mic array is
        mounted rigidly on the moving head, so its own DOA reading (90 =
        aligned with the array's own nose) is always relative to the head's
        current orientation, not a fixed room direction - after the head has
        already turned once, "home" and "wherever the nose is now" are two
        different things, and anchoring to home would compound errors on
        every subsequent reading. This is what live DOA tracking must use;
        set_doa_angle_immediate() is for one-off moves where "relative to
        home" is what's actually wanted (e.g. explicit angle tests).

        raw_target is clamped LINEARLY in set_angle_immediate() - not the
        "nearest edge by circular distance" this used to do. That logic
        assumed the servo takes the short way around when a target is out of
        window range, which was wrong: this hardware's angle-to-pulse-width
        mapping is a straight line, not a circle, so out-of-window targets
        just clamp to whichever literal edge value (min or max) is closer on
        that line - normal linear clamping, nothing circular about it.

        ramp_duration_s: if given (not None), glides there over that many
        seconds via small intermediate PWM updates instead of jumping in one
        - confirmed on the bench that a single instant update looks abrupt
        (the servo moves at its own max speed), where a short eased glide
        looks like natural head motion instead."""
        raw_target = self.current_heading_degrees + (target_angle_degrees - 90.0)
        logger.debug(
            "[Servo] track_relative_angle: current=%.1f target=%.1f raw_target=%.1f (window %.1f-%.1f)",
            self.current_heading_degrees, target_angle_degrees, raw_target,
            self._min_angle, self._max_angle,
        )
        if ramp_duration_s and ramp_duration_s > 0:
            self._ramp_to(raw_target, ramp_duration_s)
        else:
            self.set_angle_immediate(raw_target)

    def predict_relative_move(self, target_angle_degrees: float) -> float:
        """How far track_relative_angle(target_angle_degrees) would actually
        move the servo (after clamping), without moving it - lets a caller
        size a ramp/settle duration before committing to the move."""
        raw_target = self.current_heading_degrees + (target_angle_degrees - 90.0)
        clamped = max(self._min_angle, min(self._max_angle, raw_target))
        return abs(clamped - self.current_heading_degrees)

    def predict_home_move(self) -> float:
        """How far home() would actually move the servo, without moving it -
        lets a caller size a ramp/settle duration before committing, same
        idea as predict_relative_move()."""
        center = (self._min_angle + self._max_angle) / 2
        return abs(center - self.current_heading_degrees)

    def _ramp_to(self, target_angle_degrees: float, duration_s: float, steps_per_s: float = 20.0) -> None:
        # 50 steps/s (previous default) sent PWM updates faster than this
        # servo/PWM setup settles between them, producing visible jitter
        # instead of a smooth glide - 20/s is still fluid to the eye for head
        # motion while giving each step room to actually land.
        start_angle = self.current_heading_degrees
        n_steps = max(1, int(duration_s * steps_per_s))
        interval = duration_s / n_steps
        for i in range(1, n_steps + 1):
            angle = start_angle + (target_angle_degrees - start_angle) * (i / n_steps)
            self.set_angle_immediate(angle)
            if i < n_steps:
                time.sleep(interval)

    def stop(self) -> None:
        self._servo.detach()
