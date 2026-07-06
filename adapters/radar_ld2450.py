"""
Dummy-Adapter fuer den LD2450-Radar (ADR-001).

Solange die echte Hardware nicht angeschlossen ist, simuliert dieser
Adapter eine einzelne, ruhig stehende Person im Raum und veraendert
nichts von selbst. Er implementiert dieselbe Schnittstelle, die der
echte radar_ld2450.py spaeter haben wird, damit der Rest des Systems
(Fusionslogik, Event-Handler) unveraendert bleibt, wenn die Hardware
angeschlossen wird (Phase 2, siehe ADR-Dokument).

ECHTE IMPLEMENTIERUNG (spaeter): liest UART-Frames vom LD2450,
parst Multi-Target X/Y-Koordinaten, publiziert PersonEnteredRoom /
PersonLeftRoom / PersonPositionUpdated ueber den Bus.
"""

from domain.events import PersonCountChanged
from service_layer.bus import EventBus


class DummyRadarAdapter:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._person_count = 1

    def start(self) -> None:
        print("  [DummyRadar] Simuliert: 1 Person im Raum (Hauptnutzer).")
        self.bus.publish(PersonCountChanged(count=self._person_count))

    @property
    def person_count(self) -> int:
        """Wird von der Pause/Resume-Logik abgefragt (ADR-002, 11.2)."""
        return self._person_count

    def simulate_second_person_enters(self) -> None:
        """Hilfsfunktion fuer manuelle Tests ohne echte Hardware."""
        self._person_count = 2
        self.bus.publish(PersonCountChanged(count=self._person_count))

    def simulate_second_person_leaves(self) -> None:
        self._person_count = 1
        self.bus.publish(PersonCountChanged(count=self._person_count))
