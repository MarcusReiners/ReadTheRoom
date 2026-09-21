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

    def set_eyes_closed(self, closed: bool, duration_s: float = 0.4) -> None:
        pass

    def set_happy(self, happy: bool) -> None:
        pass

    def animate_eye_direction(self, to_angle_degrees: float, duration_s: float) -> None:
        pass

    def _on_speech_started(self, event: SpeechPlaybackStarted) -> None:
        pass

    def _on_speech_ended(self, event: SpeechPlaybackEnded) -> None:
        pass

    def _on_listening_changed(self, event: ListeningStateChanged) -> None:
        pass
