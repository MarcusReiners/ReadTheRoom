import logging
import math
import threading

from domain.events import SpeechPlaybackStarted, SpeechPlaybackEnded, ListeningStateChanged
from service_layer.bus import EventBus

logger = logging.getLogger(__name__)


class DummyStatusDisplayAdapter:
    def __init__(self, bus: EventBus) -> None:
        self.text_active = False

    def set_text(self, text: str) -> None:
        self.text_active = True
        logger.info("[DummyStatusDisplay] Text: %s", text)

    def stop_text(self) -> None:
        self.text_active = False
        logger.info("[DummyStatusDisplay] Textmodus beendet.")


class StatusDisplayAdapter:
    def __init__(
        self,
        bus: EventBus,
        x_offset: int = 0,
        width: int = 96,
        height: int = 48,
        font_path: str = "/home/marcus/rpi-rgb-led-matrix/fonts/6x10.bdf",
        scroll_px_per_s: float = 4.0,
        scroll_hold_s: float = 2.5,
    ) -> None:
        from rgbmatrix import graphics

        self._graphics = graphics
        self._font = graphics.Font()
        self._font.LoadFont(font_path)
        self._text_color = graphics.Color(255, 255, 255)

        self.x_offset = x_offset
        self.width = width
        self.height = height

        self.state = "idle"
        self.text_active = False
        self._text = ""
        self._scroll_px_per_s = scroll_px_per_s
        self._scroll_hold_s = scroll_hold_s
        self._text_started_at: float | None = None
        self._lock = threading.Lock()
        self._border = _border_points(self.x_offset, self.width, self.height)

        bus.subscribe(ListeningStateChanged, self._on_listening)
        bus.subscribe(SpeechPlaybackStarted, self._on_speech_started)
        bus.subscribe(SpeechPlaybackEnded, self._on_speech_ended)

    def _on_listening(self, event: ListeningStateChanged) -> None:
        self.state = "listening" if event.listening else "idle"

    def _on_speech_started(self, event: SpeechPlaybackStarted) -> None:
        self.state = "speaking"

    def _on_speech_ended(self, event: SpeechPlaybackEnded) -> None:
        self.state = "idle"

    def set_text(self, text: str) -> None:
        with self._lock:
            if not self.text_active:
                self._text_started_at = None
            self._text = text
            self.text_active = True

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

        lines = self._wrap(text)
        line_height = self._font.height + 1
        total_height = len(lines) * line_height
        max_scroll = max(0, total_height - self.height)
        offset = min(max_scroll,
                     max(0.0, (elapsed - self._scroll_hold_s) * self._scroll_px_per_s))

        for i, line in enumerate(lines):
            y = self._font.baseline + i * line_height - int(offset)
            if y < 0 or y - self._font.baseline > self.height:
                continue
            self._graphics.DrawText(canvas, self._font, self.x_offset + 1, y,
                                    self._text_color, line)

    def _wrap(self, text: str) -> list[str]:
        lines: list[str] = []
        current = ""
        for word in text.split():
            candidate = word if not current else current + " " + word
            if self._text_width(candidate) <= self.width - 2:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    def _text_width(self, text: str) -> int:
        return sum(self._font.CharacterWidth(ord(c)) for c in text)

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
        n = len(self._border)
        speed_px_per_s = 24.0
        tail_len = max(6, n // 10)
        head = int(t * speed_px_per_s) % n

        for i in range(tail_len):
            x, y = self._border[(head - i) % n]
            fade = 1.0 - i / tail_len
            brightness = int(20 + 160 * fade)
            canvas.SetPixel(x, y, brightness, brightness, brightness)


def _fill_circle(canvas, cx: int, cy: int, r: int, color: tuple) -> None:
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                canvas.SetPixel(cx + dx, cy + dy, *color)


def _border_points(x_offset: int, width: int, height: int) -> list[tuple[int, int]]:
    x0, x1 = x_offset, x_offset + width - 1
    y0, y1 = 0, height - 1
    points: list[tuple[int, int]] = []
    points += [(x, y0) for x in range(x0, x1 + 1)]
    points += [(x1, y) for y in range(y0 + 1, y1 + 1)]
    points += [(x, y1) for x in range(x1 - 1, x0 - 1, -1)]
    points += [(x0, y) for y in range(y1 - 1, y0, -1)]
    return points
