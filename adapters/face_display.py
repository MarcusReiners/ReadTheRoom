from domain.events import SpeechPlaybackStarted, SpeechPlaybackEnded, ListeningStateChanged
from service_layer.bus import EventBus


class DummyFaceDisplayAdapter:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        bus.subscribe(SpeechPlaybackStarted, self._on_speech_started)
        bus.subscribe(SpeechPlaybackEnded, self._on_speech_ended)
        bus.subscribe(ListeningStateChanged, self._on_listening_changed)

    def set_eye_direction(self, angle_degrees: float) -> None:
        print(f"  [DummyFace] Augen schauen Richtung {angle_degrees:.0f}°")

    def _on_speech_started(self, event: SpeechPlaybackStarted) -> None:
        print("  [DummyFace] Mundanimation: START (spricht)")

    def _on_speech_ended(self, event: SpeechPlaybackEnded) -> None:
        print("  [DummyFace] Mundanimation: ENDE (Ruhezustand)")

    def _on_listening_changed(self, event: ListeningStateChanged) -> None:
        status = "höre zu" if event.listening else "Ruhezustand"
        print(f"  [DummyFace] Status: {status}")
