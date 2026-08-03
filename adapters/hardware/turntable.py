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

    def rotate_towards(self, target_angle_degrees: float) -> None:
        delta = target_angle_degrees - self.current_heading_degrees
        if abs(delta) > ROTATION_THRESHOLD_DEGREES:
            self.current_heading_degrees = target_angle_degrees

    def home(self) -> None:
        self.current_heading_degrees = 90.0


class ServoTurntableAdapter:
    def __init__(
        self,
        pin: int,
        min_angle: float,
        max_angle: float,
        min_pulse_width: float = 0.0005,
        max_pulse_width: float = 0.0025,
    ) -> None:
        from gpiozero import AngularServo

        self._min_angle = min_angle
        self._max_angle = max_angle
        self.current_heading_degrees = (min_angle + max_angle) / 2
        self._smoothed_target = self.current_heading_degrees
        self._large_change_streak = 0
        self._last_move_time = time.monotonic()
        self._servo = AngularServo(
            pin,
            initial_angle=self.current_heading_degrees,
            min_angle=min_angle,
            max_angle=max_angle,
            min_pulse_width=min_pulse_width,
            max_pulse_width=max_pulse_width,
        )
        logger.info("[Servo] GPIO%s bereit, Bereich %.0f-%.0f Grad.", pin, min_angle, max_angle)

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

    def stop(self) -> None:
        self._servo.detach()
