import json
import logging
import math
import threading
import time

import serial

from domain.events import (
    DoorCleared,
    PersonAtDoor,
    PersonCountChanged,
    PersonEnteredRoom,
    PersonLeftRoom,
    RadarTargetsUpdated,
)
from service_layer.bus import EventBus

logger = logging.getLogger(__name__)

TRACK_GAP_S = 1.0
MAX_TRACK_JUMP_MM = 800.0
APPEAR_EDGE_MM = 600.0
APPEAR_SPEED_MMS = 150.0
APPEAR_INWARD_MM = 80.0
APPEAR_WINDOW_S = 0.6
DOOR_OUTSIDE_S = 1.0
SILENCE_WARN_S = 3.0
MODULE_SILENT_MS = 2000
DIAG_WINDOW_S = 10.0
SEND_FAIL_WARN = 10


def zone_signed_distance(x: float, y: float, zone: dict) -> float | None:
    """Distance to the zone rectangle's edge: positive outside, negative inside."""
    if not zone or not zone.get("valid") or x is None or y is None:
        return None
    try:
        x0, x1 = sorted((zone["min_x_mm"], zone["max_x_mm"]))
        y0, y1 = sorted((zone["min_y_mm"], zone["max_y_mm"]))
    except (KeyError, TypeError):
        return None
    if x0 <= x <= x1 and y0 <= y <= y1:
        return -min(x - x0, x1 - x, y - y0, y1 - y)
    return math.hypot(max(x0 - x, 0.0, x - x1), max(y0 - y, 0.0, y - y1))


class DummyRadarAdapter:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._person_count = 0

    def start(self) -> None:
        self.bus.publish(PersonCountChanged(count=self._person_count))

    @property
    def person_count(self) -> int:
        return self._person_count

    def simulate_person_enters(self) -> None:
        self._person_count += 1
        self.bus.publish(PersonCountChanged(count=self._person_count))
        self.bus.publish(PersonEnteredRoom(person_id=self._person_count, x_mm=0.0, y_mm=0.0))

    def simulate_person_leaves(self) -> None:
        if self._person_count > 0:
            self.bus.publish(PersonLeftRoom(person_id=self._person_count))
        self._person_count = max(0, self._person_count - 1)
        self.bus.publish(PersonCountChanged(count=self._person_count))

    def get_zone(self) -> dict:
        return {"valid": False, "mode": 0}

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
        drop_hold_s: float = 3.0,
        entry_margin_mm: float = 100.0,
        exit_margin_mm: float = 100.0,
        door_near_mm: float = 800.0,
    ) -> None:
        """drop_hold_s: how long a *decrease* in person count has to hold
        steady before it's believed. Increases are published immediately.

        This asymmetry is deliberate and load-bearing for privacy. The
        LD2450 routinely merges two nearby people into one target, or loses
        a stationary one for a frame or two - and every one of those glitches
        reads as "the second person left". Believed instantly, that flips the
        assistant straight back out of text-only mode and starts it speaking
        a confidential answer out loud with both people still standing there,
        which is the exact failure the privacy switch exists to prevent.
        Erring toward "someone might still be here" costs a few seconds of
        unnecessary discretion; erring the other way leaks the conversation.
        """
        self.bus = bus
        self._serial_port = serial_port
        self._baud_rate = baud_rate
        self._drop_hold_s = drop_hold_s
        self._person_count = 0
        self._entry_margin_mm = entry_margin_mm
        self._exit_margin_mm = exit_margin_mm
        self._door_near_mm = door_near_mm
        self._door_zone: dict = {"valid": False}
        self._tracks: dict = {}
        self._next_track_id = 0
        self._warned_zone_mode = None
        self._last_packet_error = 0.0
        self._last_data: float | None = None
        self._silent_warned = False
        self._module_silent_warned = False
        self._sensor: dict = {}
        self._bridge_dropped = 0
        self._diag_mark: tuple | None = None
        # Candidate lower count waiting out drop_hold_s before being believed.
        self._pending_lower_count: int | None = None
        self._pending_since = 0.0
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

    def get_diagnostics(self) -> dict:
        return dict(self._sensor, bridge_dropped=self._bridge_dropped)

    def get_door_zone(self) -> dict:
        return dict(self._door_zone)

    def set_door_zone(self, zone: dict | None) -> dict:
        """Accepts {"min_x_mm", "max_x_mm", "min_y_mm", "max_y_mm"} or None to
        remove it. Lives only on the Pi: the sensor keeps just the room zone."""
        cleaned = {"valid": False}
        if isinstance(zone, dict) and zone.get("valid", True):
            try:
                x0, x1 = sorted((float(zone["min_x_mm"]), float(zone["max_x_mm"])))
                y0, y1 = sorted((float(zone["min_y_mm"]), float(zone["max_y_mm"])))
            except (KeyError, TypeError, ValueError):
                x0 = x1 = y0 = y1 = 0.0
            if x1 - x0 >= 100 and y1 - y0 >= 100:
                cleaned = {"valid": True, "min_x_mm": round(x0), "max_x_mm": round(x1),
                           "min_y_mm": round(y0), "max_y_mm": round(y1)}
        self._door_zone = cleaned
        for track in self._tracks.values():
            track.pop("region", None)
        logger.info("[Radar] door zone %s.", "set" if cleaned["valid"] else "removed")
        return dict(cleaned)

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
            logger.warning("[Radar] Could not send command: %s", e)

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
                logger.warning("[Radar] Bridge on %s unreachable: %s", self._serial_port, e)
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
        """Reads in chunks and splits lines itself: pyserial's readline() reads
        one byte per call, which at radar rate cost a large share of a CPU
        core and let the Pi fall behind the bridge."""
        pending = b""
        started = time.monotonic()
        while not self._stop.is_set():
            chunk = ser.read(ser.in_waiting or 1)
            if chunk:
                pending += chunk
                *lines, pending = pending.split(b"\n")
                if len(pending) > 8192:
                    pending = b""
                for line in lines:
                    self._consume_line(line)
            self._check_silence(time.monotonic(), started)

    def _consume_line(self, line: bytes) -> None:
        try:
            data = json.loads(line.decode("utf-8", errors="ignore").strip())
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        if self._silent_warned:
            self._silent_warned = False
            logger.info("[Radar] data from the bridge is arriving again.")
        self._last_data = time.monotonic()
        try:
            self._handle_status(data)
        except Exception:
            # One odd packet must not end the read loop: _read_loop only
            # recovers from serial errors, so anything raised here used to
            # stop radar input for good, silently, until a restart.
            now = time.monotonic()
            if now - self._last_packet_error > 10.0:
                self._last_packet_error = now
                logger.exception("[Radar] could not process a packet, skipping it: %r", line[:200])

    def _check_silence(self, now: float, started: float) -> None:
        last = self._last_data if self._last_data is not None else started
        if not self._silent_warned and now - last >= SILENCE_WARN_S:
            self._silent_warned = True
            logger.warning(
                "[Radar] no data from the bridge for %.0f s - is the sensor unit's power bank on "
                "and the sensor in radio range? (It sends at least once a second.)", now - last,
            )

    def _check_sensor_health(self, sensor: dict, dropped, now: float) -> None:
        """Turns the diagnostics the sensor and bridge firmware append to each
        line into log warnings, so a failing link shows up in the log instead
        of only as unreliable tracking."""
        self._sensor = dict(sensor)
        if isinstance(dropped, int):
            self._bridge_dropped = dropped
        fps, age = sensor.get("fps"), sensor.get("frame_age_ms")
        if fps == 0 and isinstance(age, int) and age >= MODULE_SILENT_MS:
            if not self._module_silent_warned:
                self._module_silent_warned = True
                logger.warning(
                    "[Radar] the sensor unit runs but the LD2450 delivers no frames (last one %.0f s ago) "
                    "- check the wires between the LD2450 and the XIAO.", age / 1000,
                )
        elif isinstance(fps, int) and fps > 0 and self._module_silent_warned:
            self._module_silent_warned = False
            logger.info("[Radar] LD2450 frames are arriving again (%d/s).", fps)

        failures = sensor.get("send_failures")
        if not isinstance(failures, int):
            return
        if self._diag_mark is None:
            self._diag_mark = (now, failures, self._bridge_dropped)
            return
        t0, failures0, dropped0 = self._diag_mark
        if now - t0 < DIAG_WINDOW_S:
            return
        lost = (failures - failures0) % 65536
        dropped_lines = self._bridge_dropped - dropped0
        if lost >= SEND_FAIL_WARN:
            logger.warning(
                "[Radar] the radio link lost %d packets in the last %.0f s - move the sensor closer "
                "to the assistant or check the power bank.", lost, now - t0,
            )
        if dropped_lines > 0:
            logger.warning(
                "[Radar] the bridge dropped %d lines in the last %.0f s because the Pi read too slowly.",
                dropped_lines, now - t0,
            )
        self._diag_mark = (now, failures, self._bridge_dropped)

    def _update_person_count(self, count: int) -> None:
        if count == self._person_count:
            self._pending_lower_count = None
            return

        if count > self._person_count:
            # Someone arrived - act on it now, no waiting. A false positive
            # here only costs unnecessary discretion (see drop_hold_s).
            self._pending_lower_count = None
            self._person_count = count
            self.bus.publish(PersonCountChanged(count=count))
            return

        now = time.monotonic()
        if self._pending_lower_count != count:
            # A different lower count than we were already waiting on -
            # restart the clock rather than inheriting the old deadline.
            self._pending_lower_count = count
            self._pending_since = now
            logger.debug("[Radar] count dropped to %d, holding %.1fs before accepting it.", count, self._drop_hold_s)
            return

        if now - self._pending_since >= self._drop_hold_s:
            self._pending_lower_count = None
            self._person_count = count
            self.bus.publish(PersonCountChanged(count=count))

    def _check_zone_mode(self, zone: dict) -> None:
        mode = zone.get("mode", 0)
        if mode != self._warned_zone_mode and mode not in (0, None):
            logger.warning(
                "[Radar] zone enforced by the sensor (mode %s): people outside the zone are not "
                "reported, so nobody can be seen crossing into it. Set 'Enforced by' to Software.",
                mode,
            )
        self._warned_zone_mode = mode

    def _update_crossings(self, targets: list, zone: dict, now: float) -> None:
        """Publishes PersonEnteredRoom / PersonLeftRoom when a track crosses the
        zone edge. Only movement across the boundary counts: someone sitting
        inside - tracked, lost and re-acquired - never generates an event."""
        if not zone or not zone.get("valid"):
            return
        points = []
        for target in targets:
            x, y = target.get("x_mm"), target.get("y_mm")
            if x is None or y is None:
                continue
            d = zone_signed_distance(x, y, zone)
            if d is not None:
                points.append((target, x, y, d))
        track_ids = self._associate([(x, y) for _, x, y, _ in points], now)
        if self._door_zone.get("valid"):
            self._update_door_tracks(points, track_ids, now)
            return
        seen = set()
        for (target, x, y, d), tid in zip(points, track_ids):
            seen.add(tid)
            if d <= -self._entry_margin_mm:
                side = "in"
            elif d >= self._exit_margin_mm:
                side = "out"
            else:
                side = None
            prev = self._tracks.get(tid)
            continuous = prev is not None
            prev_side = prev.get("side") if continuous else None
            pending = prev.get("pending") if continuous else None
            new_side = side or prev_side
            depth = -d
            if new_side == "in" and prev_side != "in":
                if prev_side == "out":
                    self.bus.publish(PersonEnteredRoom(person_id=tid, x_mm=x, y_mm=y, via="crossing"))
                elif abs(target.get("speed_mms") or 0) >= APPEAR_SPEED_MMS and d >= -APPEAR_EDGE_MM:
                    # First seen already inside, moving, near the edge: either
                    # someone who came in faster than the radar caught them
                    # outside, or the seated user getting up to leave. Only the
                    # direction tells them apart, so wait to see it.
                    pending = {"depth": depth, "t": now}
            elif new_side == "out" and prev_side == "in":
                self.bus.publish(PersonLeftRoom(person_id=tid))
                pending = None
            if pending is not None and new_side == "in":
                if depth - pending["depth"] >= APPEAR_INWARD_MM:
                    self.bus.publish(PersonEnteredRoom(person_id=tid, x_mm=x, y_mm=y, via="appeared"))
                    pending = None
                elif pending["depth"] - depth >= APPEAR_INWARD_MM or now - pending["t"] > APPEAR_WINDOW_S:
                    pending = None
            self._tracks[tid] = {"t": now, "x": x, "y": y, "side": new_side, "pending": pending}
        for tid in [t for t, tr in self._tracks.items() if t not in seen and now - tr["t"] > TRACK_GAP_S]:
            del self._tracks[tid]

    def _region(self, x: float, y: float, d_room: float, prev_region: str | None) -> str:
        """"door", "room" (the room zone minus the door zone) or "outside",
        with the same hysteresis as the room edge so jitter cannot flip it."""
        d_door = zone_signed_distance(x, y, self._door_zone)
        if d_door is not None and d_door <= (self._exit_margin_mm if prev_region == "door" else 0.0):
            return "door"
        limit = self._exit_margin_mm if prev_region == "room" else -self._entry_margin_mm
        return "room" if d_room <= limit else "outside"

    def _update_door_tracks(self, points: list, track_ids: list, now: float) -> None:
        """Entries and exits decided at the door zone only.

        entered: first seen at the door (or coming to it from outside), then
                 into the room - or first picked up inside within
                 door_near_mm of the door zone, too fast for the radar.
        peek:    at the door, then gone again without coming in (DoorCleared
                 "gone"); PersonAtDoor/DoorCleared drive the quieter voice.
        left:    from the room into the door zone, then gone or outside.
        Movement anywhere else - someone getting up, a reflection near a wall -
        cannot produce an entry or an exit.
        """
        seen = set()
        for (target, x, y, d_room), tid in zip(points, track_ids):
            seen.add(tid)
            prev = self._tracks.get(tid)
            prev_region = prev.get("region") if prev else None
            region = self._region(x, y, d_room, prev_region)
            if prev is None or "region" not in prev:
                tr = {"at_door": False, "in_room": region == "room", "leaving": False, "outside_since": None}
                if region == "door":
                    self._announce_at_door(tid, tr, x, y)
                elif region == "room" and prev is None:
                    d_door = zone_signed_distance(x, y, self._door_zone)
                    if d_door is not None and d_door <= self._door_near_mm:
                        self.bus.publish(PersonEnteredRoom(person_id=tid, x_mm=x, y_mm=y, via="near-door"))
            else:
                tr = prev
                if region != prev_region:
                    self._door_transition(tid, tr, region, x, y)
            if region == "outside":
                tr["outside_since"] = tr.get("outside_since") or now
                if now - tr["outside_since"] >= DOOR_OUTSIDE_S:
                    self._finish_door_track(tid, tr)
            else:
                tr["outside_since"] = None
            tr.update(t=now, x=x, y=y, region=region)
            self._tracks[tid] = tr
        for tid in [t for t, tr in self._tracks.items() if t not in seen and now - tr["t"] > TRACK_GAP_S]:
            self._finish_door_track(tid, self._tracks.pop(tid))

    def _announce_at_door(self, tid: int, tr: dict, x: float, y: float) -> None:
        tr["at_door"] = True
        self.bus.publish(PersonAtDoor(person_id=tid, x_mm=x, y_mm=y))

    def _door_transition(self, tid: int, tr: dict, region: str, x: float, y: float) -> None:
        if region == "door":
            if tr.get("in_room"):
                tr["leaving"] = True
            elif not tr.get("at_door"):
                self._announce_at_door(tid, tr, x, y)
        elif region == "room":
            if tr.get("at_door"):
                tr["at_door"] = False
                self.bus.publish(PersonEnteredRoom(person_id=tid, x_mm=x, y_mm=y, via="door"))
                self.bus.publish(DoorCleared(person_id=tid, outcome="entered"))
            tr["leaving"] = False
            tr["in_room"] = True

    def _finish_door_track(self, tid: int, tr: dict) -> None:
        """The person is gone from view (or has been outside the zones for a
        while): a pending peek or a pending exit is final now."""
        if tr.get("at_door"):
            tr["at_door"] = False
            self.bus.publish(DoorCleared(person_id=tid, outcome="gone"))
        if tr.get("leaving"):
            tr["leaving"] = False
            tr["in_room"] = False
            self.bus.publish(PersonLeftRoom(person_id=tid))

    def _associate(self, points: list, now: float) -> list:
        """Gives each reported target the id of the nearest recent track.

        The LD2450's three slots are not identities: when people cross paths
        or one drops out, the module can report a person in a different slot.
        Keyed by slot, that read as one track jumping across the room and
        could fake an entry or exit with two people present. Matching is
        greedy, closest pairs first, within MAX_TRACK_JUMP_MM of a track seen
        in the last TRACK_GAP_S; anything unmatched starts a new track.
        """
        live = {tid: tr for tid, tr in self._tracks.items() if now - tr["t"] <= TRACK_GAP_S}
        pairs = sorted(
            (math.hypot(x - tr["x"], y - tr["y"]), i, tid)
            for i, (x, y) in enumerate(points)
            for tid, tr in live.items()
        )
        ids: list = [None] * len(points)
        used = set()
        for dist, i, tid in pairs:
            if dist > MAX_TRACK_JUMP_MM:
                break
            if ids[i] is None and tid not in used:
                ids[i] = tid
                used.add(tid)
        for i, tid in enumerate(ids):
            if tid is None:
                self._next_track_id += 1
                ids[i] = self._next_track_id
        return ids

    def _handle_status(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        sensor = data.get("sensor")
        if isinstance(sensor, dict):
            self._check_sensor_health(sensor, data.get("bridge_dropped"), time.monotonic())
        targets = [t for t in (data.get("targets") or []) if isinstance(t, dict)]
        self.bus.publish(RadarTargetsUpdated(targets=targets))

        self._update_person_count(sum(1 for t in targets if t.get("in_zone", True)))

        zone = data.get("zone")
        if zone is not None:
            self._latest_zone = zone
            self._check_zone_mode(zone)
        self._update_crossings(targets, self._latest_zone, time.monotonic())

        calibrating = bool(data.get("calibrating", False))
        calibration = {"calibrating": calibrating}
        if calibrating:
            calibration["remaining_ms"] = data.get("remaining_ms", 0)
        self._latest_calibration = calibration
