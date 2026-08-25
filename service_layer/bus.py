import logging
from collections import defaultdict, deque
from dataclasses import asdict, is_dataclass
from typing import Callable, Type, TypeVar

from domain.events import Event, RadarTargetsUpdated

E = TypeVar("E", bound=Event)
Handler = Callable[[Event], None]

logger = logging.getLogger("readtheroom.events")

# The in-memory event log is a debugging aid, not a record anything depends
# on - so it's bounded. It used to be an unbounded list, which on a desk
# unit meant to run for weeks was a slow memory leak: RadarTargetsUpdated
# alone lands at roughly 10Hz, i.e. ~860k retained events per day, none of
# them ever read back.
_HISTORY_LIMIT = 500

# Events published at sensor rate rather than at conversation rate. Logging
# these at INFO drowns the log file (and the rotation budget) in radar
# coordinates, burying the handful of lines that actually explain what the
# assistant did - and costs a dataclass->dict conversion per event on the Pi
# whether or not anything ends up written.
_HIGH_FREQUENCY_EVENTS = (RadarTargetsUpdated,)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[Type[Event], list[Handler]] = defaultdict(list)
        self._log: deque[Event] = deque(maxlen=_HISTORY_LIMIT)

    def subscribe(self, event_type: Type[E], handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: Event) -> None:
        self._log.append(event)
        event_type = type(event)
        level = logging.DEBUG if isinstance(event, _HIGH_FREQUENCY_EVENTS) else logging.INFO
        if logger.isEnabledFor(level):
            logger.log(level, "%s: %s", event_type.__name__,
                       asdict(event) if is_dataclass(event) else event)
        for handler in self._handlers.get(event_type, []):
            # One misbehaving subscriber must not take down the thread that
            # published - that thread is usually a sensor read loop, and
            # losing it silently stops radar or DOA input for good.
            try:
                handler(event)
            except Exception:
                logger.exception("Handler fuer %s ist fehlgeschlagen.", event_type.__name__)

    @property
    def history(self) -> list[Event]:
        return list(self._log)
