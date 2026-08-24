import math
import time

from service_layer.bus import EventBus

_WHITE = (255, 255, 255)


class LedEyesAdapter:
    """LED-matrix renderer drawing two plain white round eyes side by side.

    Same role as the old HDMI face display, but as a matrix renderer
    (`render(canvas, t)`) registered on `LedMatrix` instead of owning its
    own pygame window/thread.

    `angle_degrees` follows the same DOA convention as `ServoTurntableAdapter`:
    90 degrees means straight ahead.
    """

    BLINK_INTERVAL = 4.5
    BLINK_DURATION = 0.15

    def __init__(
        self,
        bus: EventBus,
        x_offset: int = 0,
        width: int = 96,
        height: int = 48,
    ) -> None:
        self.x_offset = x_offset
        self.width = width
        self.height = height
        self._eye_angle = 90.0

        # animate_eye_direction()'s state - a separate wall-clock-driven
        # transition, independent of render(t)'s own blink clock, since it's
        # triggered from outside the render loop (the DOA tracking thread).
        self._anim_start_angle = 90.0
        self._anim_target_angle = 90.0
        self._anim_start_time = 0.0
        self._anim_duration_s = 0.0

        self._eye_r = max(4, min(self.height // 6, self.width // 10))
        self._dot_r = max(2, self._eye_r // 2)
        self._cy = self.height // 2
        self._left_cx = self.x_offset + int(self.width * 0.375)
        self._right_cx = self.x_offset + int(self.width * 0.625)

    def set_eye_direction(self, angle_degrees: float) -> None:
        """Instant snap - cancels any in-progress animate_eye_direction()."""
        self._eye_angle = angle_degrees
        self._anim_duration_s = 0.0

    def animate_eye_direction(self, to_angle_degrees: float, duration_s: float) -> None:
        """Smoothly transitions from the current (possibly still-animating)
        eye angle to to_angle_degrees over duration_s, computed each frame in
        render() from elapsed wall time - e.g. eyes drifting back to center
        over the same span the head takes to physically catch up, instead of
        snapping back the instant the head starts moving."""
        self._anim_start_angle = self._current_angle()
        self._anim_target_angle = to_angle_degrees
        self._anim_start_time = time.monotonic()
        self._anim_duration_s = max(duration_s, 0.001)

    def _current_angle(self) -> float:
        if self._anim_duration_s <= 0:
            return self._eye_angle
        elapsed = time.monotonic() - self._anim_start_time
        if elapsed >= self._anim_duration_s:
            self._eye_angle = self._anim_target_angle
            self._anim_duration_s = 0.0
            return self._eye_angle
        progress = elapsed / self._anim_duration_s
        return self._anim_start_angle + (self._anim_target_angle - self._anim_start_angle) * progress

    def render(self, canvas, t: float) -> None:
        blinking = (t % self.BLINK_INTERVAL) < self.BLINK_DURATION

        rad = math.radians(self._current_angle() - 90.0)
        px = int((self._eye_r - self._dot_r) * 0.6 * math.sin(rad))

        for cx in (self._left_cx, self._right_cx):
            if blinking:
                for dx in range(-self._dot_r, self._dot_r + 1):
                    canvas.SetPixel(cx + dx, self._cy, *_WHITE)
                continue
            _fill_circle(canvas, cx + px, self._cy, self._dot_r, _WHITE)


def _fill_circle(canvas, cx: int, cy: int, r: int, color: tuple) -> None:
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                canvas.SetPixel(cx + dx, cy + dy, *color)
