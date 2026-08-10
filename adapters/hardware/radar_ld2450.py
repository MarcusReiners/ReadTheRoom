import logging
import threading
import time

import requests

from domain.events import PersonCountChanged, RadarTargetsUpdated
from service_layer.bus import EventBus

logger = logging.getLogger(__name__)


class DummyRadarAdapter:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._person_count = 1

    def start(self) -> None:
        self.bus.publish(PersonCountChanged(count=self._person_count))

    @property
    def person_count(self) -> int:
        return self._person_count

    def simulate_second_person_enters(self) -> None:
        self._person_count = 2
        self.bus.publish(PersonCountChanged(count=self._person_count))

    def simulate_second_person_leaves(self) -> None:
        self._person_count = 1
        self.bus.publish(PersonCountChanged(count=self._person_count))


class RadarLD2450Adapter:
    """Polls a Seeed XIAO ESP32S3 running the LD2450 firmware's HTTP JSON
    endpoint (GET {url}/targets -> {"targets": [...]})and republishes the
    active target count as PersonCountChanged, on change only.
    """

    def __init__(
        self,
        bus: EventBus,
        url: str,
        poll_interval_s: float = 0.3,
        request_timeout_s: float = 1.0,
    ) -> None:
        self.bus = bus
        self._targets_url = url.rstrip("/") + "/targets"
        self._poll_interval_s = poll_interval_s
        self._request_timeout_s = request_timeout_s
        self._person_count = 1
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def person_count(self) -> int:
        return self._person_count

    def start(self) -> None:
        self.bus.publish(PersonCountChanged(count=self._person_count))
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                response = requests.get(self._targets_url, timeout=self._request_timeout_s)
                response.raise_for_status()
                targets = response.json().get("targets", [])
                self.bus.publish(RadarTargetsUpdated(targets=targets))

                count = len(targets)
                if count != self._person_count:
                    self._person_count = count
                    self.bus.publish(PersonCountChanged(count=count))
            except requests.RequestException as e:
                logger.warning("[Radar] %s nicht erreichbar: %s", self._targets_url, e)

            time.sleep(self._poll_interval_s)
