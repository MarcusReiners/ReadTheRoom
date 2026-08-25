import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serial

import config

# Answers one question: does the LD2450 ever freeze a target's coordinates at
# exactly the same value for seconds at a time, and if so, is that a real
# person holding still or a coasted ghost of someone who left?
#
# The XIAO firmware's ghost filter (STALE_NOISE_THRESHOLD_MM /
# STALE_TIMEOUT_MS in mmWave/src/main.cpp) assumes only ghosts freeze, and
# drops any target that does. This prints the freeze durations that filter
# would act on, so the assumption can be checked against the actual sensor
# instead of taken on faith.
#
# Run it twice and compare:
#   1. Sit still in the zone for a minute.   Freezes here = the filter is
#      erasing real, present people.
#   2. Walk out of the zone and stay out.    Freezes here = real coasted
#      ghosts, and the filter is earning its keep.

FREEZE_THRESHOLD_MM = 2      # matches STALE_NOISE_THRESHOLD_MM
REPORT_AFTER_S = 1.0         # only mention freezes worth noticing
FILTER_TIMEOUT_S = 4.0       # matches STALE_TIMEOUT_MS


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else config.RADAR_SERIAL_PORT
    ser = serial.Serial(port, config.RADAR_SERIAL_BAUD, timeout=1)

    print(f"Lausche auf {port}. Strg+C zum Beenden.\n")
    print("Sitz erst still im Zonenbereich, dann verlasse den Raum - und "
          "vergleiche, wo Freezes auftreten.\n")

    last_pos: dict[int, tuple[int, int]] = {}
    frozen_since: dict[int, float] = {}
    reported: set[int] = set()

    while True:
        line = ser.readline()
        if not line:
            continue
        try:
            data = json.loads(line.decode("utf-8", errors="ignore").strip())
        except ValueError:
            continue

        now = time.monotonic()
        seen = set()
        for target in data.get("targets", []):
            tid = target.get("id")
            seen.add(tid)
            pos = (target.get("x_mm", 0), target.get("y_mm", 0))
            previous = last_pos.get(tid)

            moved = previous is None or (
                abs(pos[0] - previous[0]) > FREEZE_THRESHOLD_MM
                or abs(pos[1] - previous[1]) > FREEZE_THRESHOLD_MM
            )
            if moved:
                held = now - frozen_since.get(tid, now)
                if tid in reported:
                    print(f"  -> Target {tid} bewegt sich wieder nach {held:.1f}s "
                          f"({'WAERE SCHON GEFILTERT' if held >= FILTER_TIMEOUT_S else 'noch nicht gefiltert'})")
                last_pos[tid] = pos
                frozen_since[tid] = now
                reported.discard(tid)
            else:
                held = now - frozen_since.get(tid, now)
                if held >= REPORT_AFTER_S and tid not in reported and held >= FILTER_TIMEOUT_S:
                    print(f"[{time.strftime('%H:%M:%S')}] Target {tid} eingefroren seit {held:.1f}s "
                          f"bei x={pos[0]} y={pos[1]} in_zone={target.get('in_zone')} "
                          f"speed={target.get('speed_mms')} -> Firmware-Filter wuerde ihn JETZT verwerfen")
                    reported.add(tid)

        for tid in list(last_pos):
            if tid not in seen:
                last_pos.pop(tid, None)
                frozen_since.pop(tid, None)
                reported.discard(tid)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCiao!")
