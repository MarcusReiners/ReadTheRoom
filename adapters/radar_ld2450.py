from domain.events import PersonCountChanged
from service_layer.bus import EventBus


class DummyRadarAdapter:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._person_count = 1

    def start(self) -> None:
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
