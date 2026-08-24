import json
import logging
import threading
import time

import serial

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

    def get_zone(self) -> dict:
        return {"valid": False}

    def get_calibration_status(self) -> dict:
        return {"calibrating": False}

    def send_command(self, command: dict) -> None:
        pass


class RadarLD2450Adapter:
    """Reads newline-delimited JSON status lines from the ESP-NOW bridge
    ESP32 (see mmWaveBridge/ - it relays packets from the XIAO's LD2450
    sensor over ESP-NOW), connected to the Pi over USB serial, and
    republishes the in-zone target count as PersonCountChanged, on change
    only. The full raw target list (including out-of-zone ones, each
    tagged "in_zone") is still broadcast via RadarTargetsUpdated so the
    settings page can show everything the sensor is tracking, not just
    what counts for presence.
    """

    def __init__(
        self,
        bus: EventBus,
        serial_port: str,
        baud_rate: int = 115200,
    ) -> None:
        self.bus = bus
        self._serial_port = serial_port
        self._baud_rate = baud_rate
        self._person_count = 1
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._serial: serial.Serial | None = None
        self._pending_commands: list[dict] = []

        self._latest_zone: dict = {"valid": False}
        self._latest_calibration: dict = {"calibrating": False}

    @property
    def person_count(self) -> int:
        return self._person_count

    def start(self) -> None:
        self.bus.publish(PersonCountChanged(count=self._person_count))
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get_zone(self) -> dict:
        return dict(self._latest_zone)

    def get_calibration_status(self) -> dict:
        return dict(self._latest_calibration)

    def send_command(self, command: dict) -> None:
        with self._write_lock:
            if self._serial is not None and self._serial.is_open:
                self._write(command)
            else:
                # Connection isn't up yet (e.g. commands sent right after start()) -
                # queue it rather than silently dropping, flushed once connected.
                self._pending_commands.append(command)

    def _write(self, command: dict) -> None:
        line = json.dumps(command) + "\n"
        try:
            self._serial.write(line.encode("utf-8"))
        except serial.SerialException as e:
            logger.warning("[Radar] Befehl konnte nicht gesendet werden: %s", e)

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._serial = serial.Serial(self._serial_port, self._baud_rate, timeout=1)
                logger.info("[Radar] Verbunden mit Bridge auf %s", self._serial_port)
                with self._write_lock:
                    for command in self._pending_commands:
                        self._write(command)
                    self._pending_commands.clear()
                self._consume(self._serial)
            except serial.SerialException as e:
                logger.warning("[Radar] Bridge auf %s nicht erreichbar: %s", self._serial_port, e)
                time.sleep(1.0)
            finally:
                with self._write_lock:
                    if self._serial is not None:
                        try:
                            self._serial.close()
                        except Exception:
                            pass
                        self._serial = None

    def _consume(self, ser: "serial.Serial") -> None:
        while not self._stop.is_set():
            line = ser.readline()
            if not line:
                continue
            try:
                data = json.loads(line.decode("utf-8", errors="ignore").strip())
            except ValueError:
                continue
            self._handle_status(data)

    def _handle_status(self, data: dict) -> None:
        targets = data.get("targets", [])
        self.bus.publish(RadarTargetsUpdated(targets=targets))

        count = sum(1 for t in targets if t.get("in_zone", True))
        if count != self._person_count:
            self._person_count = count
            self.bus.publish(PersonCountChanged(count=count))

        zone = data.get("zone")
        if zone is not None:
            self._latest_zone = zone

        calibrating = bool(data.get("calibrating", False))
        calibration = {"calibrating": calibrating}
        if calibrating:
            calibration["remaining_ms"] = data.get("remaining_ms", 0)
        self._latest_calibration = calibration
