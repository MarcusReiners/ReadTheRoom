import threading

from domain.events import EmergencyStopPressed
from service_layer.bus import EventBus


class DummyEmergencyStopAdapter:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._thread = threading.Thread(target=self._listen, daemon=True)

    def start(self) -> None:
        print("  [DummyEmergencyStop] Simuliert per Tastatur: 's' + Enter "
              "zum Ausloesen (in separatem Thread, blockiert die Pipeline nicht).")
        self._thread.start()

    def _listen(self) -> None:
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip().lower() == "s":
                print("  [DummyEmergencyStop] Taster ausgelöst!")
                self.bus.publish(EmergencyStopPressed())
