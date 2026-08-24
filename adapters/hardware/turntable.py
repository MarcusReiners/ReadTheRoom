import logging
import time

from domain.policies import (
    DOA_SMOOTHING_ALPHA,
    MIN_CONSECUTIVE_LARGE_CHANGES,
    MOVE_COOLDOWN_SECONDS,
    ROTATION_THRESHOLD_DEGREES,
)

logger = logging.getLogger(__name__)


class DummyTurntableAdapter:
    def __init__(self) -> None:
        self.current_heading_degrees = 90.0  # 90 degrees = front-facing home position
        self.home_offset_degrees = 0.0

    def rotate_towards(self, target_angle_degrees: float) -> None:
        delta = target_angle_degrees - self.current_heading_degrees
        if abs(delta) > ROTATION_THRESHOLD_DEGREES:
            self.current_heading_degrees = target_angle_degrees

    def home(self) -> None:
        self.current_heading_degrees = 90.0

    def set_angle_immediate(self, angle_degrees: float) -> None:
        self.current_heading_degrees = angle_degrees

    def set_home_offset(self, offset_degrees: float) -> None:
        self.home_offset_degrees = offset_degrees


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
        self._smoothed_target = self.current_heading_degrees
        self._large_change_streak = 0
        self._last_move_time = time.monotonic()
        self._servo = AngularServo(
            pin,
            initial_angle=self.current_heading_degrees,
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
        width = self._base_max_angle - self._base_min_angle
        lo = self._base_min_angle + offset_degrees
        hi = lo + width

        # Shift the whole window back inside hardware bounds rather than clipping
        # each edge independently - clipping edges separately would shrink the
        # window's width instead of just repositioning it, silently narrowing the
        # cable-safety range instead of preserving it.
        shifted = False
        if lo < self._hardware_min_angle:
            shift = self._hardware_min_angle - lo
            lo += shift
            hi += shift
            shifted = True
        if hi > self._hardware_max_angle:
            shift = hi - self._hardware_max_angle
            lo -= shift
            hi -= shift
            shifted = True

        if shifted:
            logger.warning(
                "[Servo] Home-Offset %.1f wuerde den sicheren Bereich ausserhalb des "
                "Hardware-Bereichs (%.0f-%.0f) schieben - Fenster auf %.1f-%.1f verschoben "
                "(Breite %.0f Grad bleibt erhalten).",
                offset_degrees, self._hardware_min_angle, self._hardware_max_angle, lo, hi, width,
            )
        return lo, hi

    def set_home_offset(self, offset_degrees: float) -> None:
        """Live-adjusts the home offset (e.g. from a calibration script/web
        endpoint's nudge-then-save flow). Does not move the servo itself -
        call home() afterward to actually drive there."""
        self.home_offset_degrees = offset_degrees
        self._min_angle, self._max_angle = self._windowed_range(offset_degrees)

    def rotate_towards(self, target_angle_degrees: float) -> None:
        # target_angle_degrees is DOA-style (90 degrees = straight ahead), independent
        # of how min/max_angle happen to be configured; re-center onto the servo's own range.
        center = (self._min_angle + self._max_angle) / 2
        raw_target = center + (target_angle_degrees - 90.0)
        raw_target = max(self._min_angle, min(self._max_angle, raw_target))

        # Low-pass filter so a single noisy reading can't jerk the servo - it only
        # ever chases a smoothed estimate.
        self._smoothed_target += DOA_SMOOTHING_ALPHA * (raw_target - self._smoothed_target)

        # Physical movement only happens once that estimate has drifted past the
        # deadband threshold AND stayed there for several updates in a row - a
        # one-off large reading doesn't move the servo, only a sustained one does.
        if abs(self._smoothed_target - self.current_heading_degrees) > ROTATION_THRESHOLD_DEGREES:
            self._large_change_streak += 1
        else:
            self._large_change_streak = 0

        cooled_down = (time.monotonic() - self._last_move_time) >= MOVE_COOLDOWN_SECONDS
        if self._large_change_streak >= MIN_CONSECUTIVE_LARGE_CHANGES and cooled_down:
            self.current_heading_degrees = self._smoothed_target
            self._servo.angle = self.current_heading_degrees
            self._large_change_streak = 0
            self._last_move_time = time.monotonic()

    def home(self) -> None:
        """Return to the front-facing center position and reset tracking state,
        bypassing the deadband/streak/cooldown gating - this is a deliberate
        command, not a noisy DOA reading to be filtered."""
        center = (self._min_angle + self._max_angle) / 2
        self.current_heading_degrees = center
        self._smoothed_target = center
        self._large_change_streak = 0
        self._last_move_time = time.monotonic()
        self._servo.angle = center
        logger.info("[Servo] Home-Position (%.0f Grad).", center)

    def set_angle_immediate(self, angle_degrees: float) -> None:
        """Directly drives the servo to an exact angle, bypassing the DOA
        smoothing/deadband/cooldown gating - for deliberate test/calibration
        movements only, same idea as home()."""
        angle_degrees = max(self._min_angle, min(self._max_angle, angle_degrees))
        self.current_heading_degrees = angle_degrees
        self._smoothed_target = angle_degrees
        self._servo.angle = angle_degrees

    def stop(self) -> None:
        self._servo.detach()
