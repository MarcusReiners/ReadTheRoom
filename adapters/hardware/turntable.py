import logging

from domain.policies import ROTATION_THRESHOLD_DEGREES

logger = logging.getLogger(__name__)


class DummyTurntableAdapter:
    def __init__(self) -> None:
        self.current_heading_degrees = 0.0

    def rotate_towards(self, target_angle_degrees: float) -> None:
        delta = target_angle_degrees - self.current_heading_degrees
        if abs(delta) > ROTATION_THRESHOLD_DEGREES:
            self.current_heading_degrees = target_angle_degrees


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
        self._servo = AngularServo(
            pin,
            min_angle=min_angle,
            max_angle=max_angle,
            min_pulse_width=min_pulse_width,
            max_pulse_width=max_pulse_width,
        )
        self.current_heading_degrees = (min_angle + max_angle) / 2
        self._servo.angle = self.current_heading_degrees
        logger.info("[Servo] GPIO%s bereit, Bereich %.0f-%.0f Grad.", pin, min_angle, max_angle)

    def rotate_towards(self, target_angle_degrees: float) -> None:
        target = max(self._min_angle, min(self._max_angle, target_angle_degrees))
        if abs(target - self.current_heading_degrees) > ROTATION_THRESHOLD_DEGREES:
            self.current_heading_degrees = target
            self._servo.angle = target

    def stop(self) -> None:
        self._servo.detach()
