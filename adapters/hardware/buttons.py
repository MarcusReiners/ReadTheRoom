import threading

from domain.events import DisplayTakeoverRequested
from service_layer.bus import EventBus


class DummyButtonsAdapter:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._thread = threading.Thread(target=self._listen, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _listen(self) -> None:
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip().lower() == "t":
                self.bus.publish(DisplayTakeoverRequested())
