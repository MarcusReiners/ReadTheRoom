import logging
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup
from adapters.hardware.radar_ld2450 import zone_signed_distance
from domain.events import PersonEnteredRoom, PersonLeftRoom, RadarTargetsUpdated
from service_layer.bus import EventBus


def make_tone(path, freq_hz, dur_s=0.25, rate=16000):
    n = int(rate * dur_s)
    fade = int(rate * 0.01)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            env = min(1.0, i / fade, (n - i) / fade)
            frames += struct.pack("<h", int(14000 * env * math.sin(2 * math.pi * freq_hz * i / rate)))
        w.writeframes(bytes(frames))


def player():
    if sys.platform == "darwin":
        return lambda path: subprocess.Popen(["afplay", path])
    if shutil.which("aplay") is None:
        return lambda path: None
    return lambda path: subprocess.Popen(["aplay", "-q", "-D", config.SPEAKER_DEVICE, path],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def quiet_console():
    """Keeps the terminal to the prompts; the log file still gets everything."""
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(logging.WARNING)


def main():
    logging_setup.configure_logging(config)
    quiet_console()
    if config.RADAR_PROVIDER != "ld2450":
        raise SystemExit(f"RADAR_PROVIDER is {config.RADAR_PROVIDER!r} - this needs the LD2450.")
    tmp = tempfile.mkdtemp(prefix="zone_edge_")
    high, low = os.path.join(tmp, "in.wav"), os.path.join(tmp, "out.wav")
    make_tone(high, 1320)
    make_tone(low, 440)
    play = player()

    from adapters.factory import build_radar
    bus = EventBus()
    radar = build_radar(config, bus)
    state = {"frames": 0}

    def on_enter(e):
        d = zone_signed_distance(e.x_mm, e.y_mm, radar.get_zone())
        play(high)
        print(f"  IN   ({e.via:8})  x={e.x_mm:6.0f}  y={e.y_mm:6.0f} mm   "
              f"{-d if d is not None else float('nan'):5.0f} mm inside the drawn edge")

    def on_leave(e):
        play(low)
        print("  OUT")

    trace = "--trace" in sys.argv
    start = time.monotonic()
    last_seen, last_print = {}, {}

    def on_frame(e):
        state["frames"] += 1
        if not trace or not state.get("ready"):
            return
        now = time.monotonic()
        zone = radar.get_zone()
        for t in e.targets:
            d = zone_signed_distance(t.get("x_mm"), t.get("y_mm"), zone)
            if d is None:
                continue
            slot = t.get("id")
            first = now - last_seen.get(slot, -99.0) > 1.0
            last_seen[slot] = now
            if not first and now - last_print.get(slot, -99.0) < 0.25:
                continue
            last_print[slot] = now
            side = ("in" if d <= -config.RADAR_ENTRY_MARGIN_MM
                    else "out" if d >= config.RADAR_EXIT_MARGIN_MM else "edge")
            print(f"  {now - start:6.1f}s {'FIRST' if first else '     '} id {slot}  x={t.get('x_mm'):6.0f}  "
                  f"y={t.get('y_mm'):6.0f}  speed={t.get('speed_mms') or 0:5.0f}  "
                  f"edge distance={d:6.0f} mm  {side}")

    bus.subscribe(PersonEnteredRoom, on_enter)
    bus.subscribe(PersonLeftRoom, on_leave)
    bus.subscribe(RadarTargetsUpdated, on_frame)
    radar.start()

    deadline = time.monotonic() + 15.0
    while state["frames"] == 0 and time.monotonic() < deadline:
        time.sleep(0.1)
    if state["frames"] == 0:
        raise SystemExit("No radar frames - is the bridge plugged in and the main app stopped?")
    zone = radar.get_zone()
    if not zone.get("valid"):
        raise SystemExit("No zone configured - draw it in the web app first.")
    if zone.get("mode", 0) not in (0, None):
        raise SystemExit("Zone is enforced by the sensor, so nobody outside it is visible. "
                         "Set 'Enforced by' to Software in the web app.")

    print(f"\nZone x {zone['min_x_mm']}..{zone['max_x_mm']} mm, y {zone['min_y_mm']}..{zone['max_y_mm']} mm")
    print(f"An entry fires {config.RADAR_ENTRY_MARGIN_MM:.0f} mm inside the drawn edge, "
          f"an exit {config.RADAR_EXIT_MARGIN_MM:.0f} mm outside it.")
    print("\nWalk slowly through the doorway: HIGH beep = entry, LOW beep = exit.")
    print("Tape the floor where the high beep sounds. Repeat a few times. Ctrl+C to quit.\n")
    state["ready"] = True
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        try:
            print("\nBye!")
        except BrokenPipeError:
            pass


if __name__ == "__main__":
    main()
