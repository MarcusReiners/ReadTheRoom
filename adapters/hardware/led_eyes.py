import math

from domain.events import SpeechPlaybackStarted, SpeechPlaybackEnded, ListeningStateChanged
from service_layer.bus import EventBus

_BG = (10, 10, 15)
_SCLERA = (230, 230, 230)
_IRIS = (70, 160, 255)


class LedEyesAdapter:
    """LED-matrix renderer drawing two eyes side by side within its panel.

    Same role as the old HDMI face display, but as a matrix renderer
    (`render(canvas, t)`) registered on `LedMatrix` instead of owning its
    own pygame window/thread.
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
        self._eye_angle = 180.0

        self._eye_r = min(self.height // 2 - 2, self.width // 4 - 4)
        self._pupil_r_base = max(2, self._eye_r // 3)
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
        pupil_r = self._pupil_r_base + (6 if self.state == "listening" else 0)
        if self.state == "speaking":
            pupil_r += int(4 * abs(math.sin(t * 8)))
        pupil_r = min(pupil_r, self._eye_r - 1)

        rad = math.radians(self._eye_angle - 180.0)
        px = int((self._eye_r - pupil_r) * 0.6 * math.sin(rad))

        for cx in (self._left_cx, self._right_cx):
            if blinking:
                for dx in range(-self._eye_r, self._eye_r + 1):
                    canvas.SetPixel(cx + dx, self._cy, *_SCLERA)
                    canvas.SetPixel(cx + dx, self._cy + 1, *_SCLERA)
                continue
            _fill_circle(canvas, cx, self._cy, self._eye_r, _SCLERA)
            _fill_circle(canvas, cx + px, self._cy, pupil_r, _IRIS)
            _fill_circle(canvas, cx + px, self._cy, max(1, pupil_r // 3), _BG)


def _fill_circle(canvas, cx: int, cy: int, r: int, color: tuple) -> None:
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                canvas.SetPixel(cx + dx, cy + dy, *color)
