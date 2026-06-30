"""
Dummy-Adapter fuer den Not-Stopp-Taster (GPIO, ADR-002 Abschnitt 11).

Solange kein echter Taster angeschlossen ist, wird der Stopp ueber
eine Tastatureingabe ('s' + Enter) in einem separaten Thread simuliert,
damit das Pause/Resume-Verhalten bereits jetzt manuell getestet werden
kann, ohne den Hauptthread zu blockieren.

ECHTE IMPLEMENTIERUNG (spaeter): gpiozero.Button mit Interrupt-
Callback, siehe ADR-002 Abschnitt 11 (eigener Interrupt-Handler mit
hoechster Prioritaet, unabhaengig von der Auslastung der uebrigen
Event-Verarbeitung).
"""

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
                print("  [DummyEmergencyStop] 🛑 Taster ausgelöst!")
                self.bus.publish(EmergencyStopPressed())
