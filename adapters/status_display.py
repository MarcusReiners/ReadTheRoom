import math
import threading

from domain.events import SpeechPlaybackStarted, SpeechPlaybackEnded, ListeningStateChanged
from service_layer.bus import EventBus


class DummyStatusDisplayAdapter:
    def __init__(self, bus: EventBus) -> None:
        self.text_active = False

    def append_text(self, text: str) -> None:
        self.text_active = True
        print(f"  [DummyStatusDisplay] Laufschrift: {text}")

    def stop_text(self) -> None:
        self.text_active = False
        print("  [DummyStatusDisplay] Textmodus beendet.")


class StatusDisplayAdapter:
    def __init__(
        self,
        bus: EventBus,
        x_offset: int = 0,
        width: int = 96,
        height: int = 48,
        font_path: str = "/home/marcus/rpi-rgb-led-matrix/fonts/8x13.bdf",
    ) -> None:
        from rgbmatrix import graphics

        self._graphics = graphics
        self._font = graphics.Font()
        self._font.LoadFont(font_path)
        self._text_color = graphics.Color(255, 180, 0)

        self.x_offset = x_offset
        self.width = width
        self.height = height

        self.state = "idle"
        self.text_active = False
        self._text = ""
        self._text_started_at: float | None = None
        self._lock = threading.Lock()

        bus.subscribe(ListeningStateChanged, self._on_listening)
        bus.subscribe(SpeechPlaybackStarted, self._on_speech_started)
        bus.subscribe(SpeechPlaybackEnded, self._on_speech_ended)

    def _on_listening(self, event: ListeningStateChanged) -> None:
        self.state = "listening" if event.listening else "idle"

    def _on_speech_started(self, event: SpeechPlaybackStarted) -> None:
        self.state = "speaking"

    def _on_speech_ended(self, event: SpeechPlaybackEnded) -> None:
        self.state = "idle"

    def append_text(self, text: str) -> None:
        with self._lock:
            self._text += (" " if self._text else "") + text
            if not self.text_active:
                self.text_active = True
                self._text_started_at = None

    def stop_text(self) -> None:
        with self._lock:
            self.text_active = False
            self._text = ""

    def render(self, canvas, t: float) -> None:
        if self.text_active:
            self._render_text(canvas, t)
        elif self.state == "listening":
            self._render_listening(canvas, t)
        elif self.state == "speaking":
            self._render_speaking(canvas, t)
        else:
            self._render_idle(canvas, t)

    def _render_text(self, canvas, t: float) -> None:
        with self._lock:
            text = self._text
            if self._text_started_at is None:
                self._text_started_at = t
            elapsed = t - self._text_started_at
        x = canvas.width - int(elapsed * 30)
        y = self.height // 2 + 4
        width = self._graphics.DrawText(canvas, self._font, x, y, self._text_color, text)
        if x + width < 0:
            with self._lock:
                self._text_started_at = t

    def _render_listening(self, canvas, t: float) -> None:
        cx = self.x_offset + self.width // 2
        cy = self.height // 2
        radius = 4 + int(3 * (1 + math.sin(t * 6)) / 2)
        _fill_circle(canvas, cx, cy, radius, (200, 30, 30))

    def _render_speaking(self, canvas, t: float) -> None:
        bars = 7
        spacing = self.width // (bars + 1)
        for i in range(bars):
            x = self.x_offset + spacing * (i + 1)
            h = int((self.height // 2 - 4) * (0.3 + 0.7 * abs(math.sin(t * 8 + i * 1.3))))
            for dy in range(-h, h + 1):
                canvas.SetPixel(x, self.height // 2 + dy, 30, 160, 220)
                canvas.SetPixel(x + 1, self.height // 2 + dy, 30, 160, 220)

    def _render_idle(self, canvas, t: float) -> None:
        cx = self.x_offset + self.width // 2
        cy = self.height // 2
        brightness = int(40 + 30 * (1 + math.sin(t * 1.5)) / 2)
        _fill_circle(canvas, cx, cy, 2, (brightness, brightness, brightness))


def _fill_circle(canvas, cx: int, cy: int, r: int, color: tuple) -> None:
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                canvas.SetPixel(cx + dx, cy + dy, *color)
