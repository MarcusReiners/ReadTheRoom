import math

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

        self._eye_r = max(4, min(self.height // 6, self.width // 10))
        self._dot_r = max(2, self._eye_r // 2)
        self._cy = self.height // 2
        self._left_cx = self.x_offset + int(self.width * 0.375)
        self._right_cx = self.x_offset + int(self.width * 0.625)

    def set_eye_direction(self, angle_degrees: float) -> None:
        self._eye_angle = angle_degrees

    def render(self, canvas, t: float) -> None:
        blinking = (t % self.BLINK_INTERVAL) < self.BLINK_DURATION

        rad = math.radians(self._eye_angle - 90.0)
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
