from domain.policies import ROTATION_THRESHOLD_DEGREES


class DummyTurntableAdapter:
    def __init__(self) -> None:
        self.current_heading_degrees = 0.0

    def rotate_towards(self, target_angle_degrees: float) -> None:
        delta = target_angle_degrees - self.current_heading_degrees
        if abs(delta) > ROTATION_THRESHOLD_DEGREES:
            self.current_heading_degrees = target_angle_degrees
