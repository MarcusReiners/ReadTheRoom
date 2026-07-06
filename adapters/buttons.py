import threading

from domain.events import EmergencyStopPressed, DisplayTakeoverRequested
from service_layer.bus import EventBus


class DummyButtonsAdapter:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._thread = threading.Thread(target=self._listen, daemon=True)

    def start(self) -> None:
        print("  [DummyButtons] Simuliert per Tastatur: 's' + Enter = Not-Stopp, "
              "'t' + Enter = Antwort auf Display umleiten.")
        self._thread.start()

    def _listen(self) -> None:
        while True:
            try:
                line = input()
            except EOFError:
                break
            key = line.strip().lower()
            if key == "s":
                print("  [DummyButtons] Not-Stopp ausgelöst!")
                self.bus.publish(EmergencyStopPressed())
            elif key == "t":
                self.bus.publish(DisplayTakeoverRequested())
