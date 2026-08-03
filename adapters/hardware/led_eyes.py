import math

from domain.events import SpeechPlaybackStarted, SpeechPlaybackEnded, ListeningStateChanged
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
        self.state = "idle"
        self._eye_angle = 90.0

        self._eye_r = min(self.height // 2 - 2, self.width // 4 - 4)
        self._dot_r_base = max(2, self._eye_r // 2)
        self._cy = self.height // 2
        self._left_cx = self.x_offset + self.width // 4
        self._right_cx = self.x_offset + 3 * self.width // 4

        bus.subscribe(SpeechPlaybackStarted, self._on_speech_started)
        bus.subscribe(SpeechPlaybackEnded, self._on_speech_ended)
        bus.subscribe(ListeningStateChanged, self._on_listening_changed)

    def set_eye_direction(self, angle_degrees: float) -> None:
        self._eye_angle = angle_degrees

    def _on_speech_started(self, event: SpeechPlaybackStarted) -> None:
        self.state = "speaking"

    def _on_speech_ended(self, event: SpeechPlaybackEnded) -> None:
        self.state = "idle"

    def _on_listening_changed(self, event: ListeningStateChanged) -> None:
        self.state = "listening" if event.listening else "idle"

    def render(self, canvas, t: float) -> None:
        blinking = (t % self.BLINK_INTERVAL) < self.BLINK_DURATION
        dot_r = self._dot_r_base + (4 if self.state == "listening" else 0)
        if self.state == "speaking":
            dot_r += int(3 * abs(math.sin(t * 8)))
        dot_r = min(dot_r, self._eye_r)

        rad = math.radians(self._eye_angle - 90.0)
        px = int((self._eye_r - dot_r) * 0.6 * math.sin(rad))

        for cx in (self._left_cx, self._right_cx):
            if blinking:
                for dx in range(-dot_r, dot_r + 1):
                    canvas.SetPixel(cx + dx, self._cy, *_WHITE)
                continue
            _fill_circle(canvas, cx + px, self._cy, dot_r, _WHITE)


def _fill_circle(canvas, cx: int, cy: int, r: int, color: tuple) -> None:
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                canvas.SetPixel(cx + dx, cy + dy, *color)
