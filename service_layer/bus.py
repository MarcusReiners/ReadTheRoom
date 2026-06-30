"""
Einfacher, synchroner Event-Bus.

Bewusst simpel gehalten (kein asyncio, kein Threading) fuer Phase 1.
Handler werden synchron in Registrierungsreihenfolge aufgerufen. Das
reicht fuer den aktuellen Funktionsumfang aus; sollte spaeter echte
Parallelitaet noetig werden (z.B. Radar-Polling parallel zu TTS-
Wiedergabe), wird dieser Bus durch eine Queue-basierte Variante ersetzt
- der Rest der Architektur aendert sich dadurch nicht, weil Handler nur
ueber den Bus kommunizieren.
"""

from collections import defaultdict
from typing import Callable, Type, TypeVar

from domain.events import Event

E = TypeVar("E", bound=Event)
Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[Type[Event], list[Handler]] = defaultdict(list)
        self._log: list[Event] = []

    def subscribe(self, event_type: Type[E], handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: Event) -> None:
        self._log.append(event)
        event_type = type(event)
        print(f"[BUS] {event_type.__name__}: {event}")
        for handler in self._handlers.get(event_type, []):
            handler(event)

    @property
    def history(self) -> list[Event]:
        """Fuer Debugging/Tests: alle bisher publizierten Events."""
        return list(self._log)
