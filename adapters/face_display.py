import math
import threading
import time

from domain.events import SpeechPlaybackStarted, SpeechPlaybackEnded, ListeningStateChanged
from service_layer.bus import EventBus


class DummyFaceDisplayAdapter:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        bus.subscribe(SpeechPlaybackStarted, self._on_speech_started)
        bus.subscribe(SpeechPlaybackEnded, self._on_speech_ended)
        bus.subscribe(ListeningStateChanged, self._on_listening_changed)

    def set_eye_direction(self, angle_degrees: float) -> None:
        pass

    def _on_speech_started(self, event: SpeechPlaybackStarted) -> None:
        pass

    def _on_speech_ended(self, event: SpeechPlaybackEnded) -> None:
        pass

    def _on_listening_changed(self, event: ListeningStateChanged) -> None:
        pass


class HDMIFaceDisplayAdapter:
    BLINK_INTERVAL = 4.5
    BLINK_DURATION = 0.15

    def __init__(self, bus: EventBus, fps: int = 30) -> None:
        self.fps = fps
        self.state = "idle"
        self._eye_angle = 180.0
        self._running = False
        self._thread = threading.Thread(target=self._loop, daemon=True)

        bus.subscribe(SpeechPlaybackStarted, self._on_speech_started)
        bus.subscribe(SpeechPlaybackEnded, self._on_speech_ended)
        bus.subscribe(ListeningStateChanged, self._on_listening_changed)

    def start(self) -> None:
        self._running = True
        self._thread.start()
        print("  [HDMIFace] Augen-Display gestartet.")

    def stop(self) -> None:
        self._running = False

    def set_eye_direction(self, angle_degrees: float) -> None:
        self._eye_angle = angle_degrees

    def _on_speech_started(self, event: SpeechPlaybackStarted) -> None:
        self.state = "speaking"

    def _on_speech_ended(self, event: SpeechPlaybackEnded) -> None:
        self.state = "idle"

    def _on_listening_changed(self, event: ListeningStateChanged) -> None:
        self.state = "listening" if event.listening else "idle"

    def _loop(self) -> None:
        import pygame

        pygame.init()
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        pygame.mouse.set_visible(False)
        clock = pygame.time.Clock()
        width, height = screen.get_size()

        eye_r = height // 5
        pupil_r_base = eye_r // 3
        cy = height // 2
        left_cx = width // 3
        right_cx = 2 * width // 3

        bg = (10, 10, 15)
        sclera = (230, 230, 230)
        iris = (70, 160, 255)

        t0 = time.monotonic()
        while self._running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self._running = False

            t = time.monotonic() - t0
            blinking = (t % self.BLINK_INTERVAL) < self.BLINK_DURATION
            pupil_r = pupil_r_base + (6 if self.state == "listening" else 0)
            if self.state == "speaking":
                pupil_r += int(4 * abs(math.sin(t * 8)))

            rad = math.radians(self._eye_angle - 180.0)
            px = int((eye_r - pupil_r) * 0.6 * math.sin(rad))

            screen.fill(bg)
            for cx in (left_cx, right_cx):
                if blinking:
                    pygame.draw.line(screen, sclera, (cx - eye_r, cy), (cx + eye_r, cy), 8)
                    continue
                pygame.draw.circle(screen, sclera, (cx, cy), eye_r)
                pygame.draw.circle(screen, iris, (cx + px, cy), pupil_r)
                pygame.draw.circle(screen, bg, (cx + px, cy), pupil_r // 3)

            pygame.display.flip()
            clock.tick(self.fps)

        pygame.quit()
