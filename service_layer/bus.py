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
        for handler in self._handlers.get(event_type, []):
            handler(event)

    @property
    def history(self) -> list[Event]:
        return list(self._log)
