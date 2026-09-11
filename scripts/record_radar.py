import argparse
import csv
import math
import os
import statistics
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup
from domain.events import RadarTargetsUpdated
from service_layer.bus import EventBus

PHASES = [
    ("sit", "Sit at your desk as you normally do."),
    ("walk", "Walk slowly around the room, then come back."),
]


def quiet_console():
    import logging
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(logging.WARNING)


def bearing(x, y):
    return math.degrees(math.atan2(x, y))


def summarize(name, frames):
    if not frames:
        print(f"\n[{name}] no frames")
        return
    counts = [len(ts) for _, ts in frames]
    print(f"\n[{name}] {len(frames)} frames")
    for n in range(4):
        share = counts.count(n) / len(frames)
        if share:
            print(f"  {n} target(s): {share:5.0%} of frames")
    extra = []
    for _, ts in frames:
        if len(ts) < 2:
            continue
        near = min(ts, key=lambda t: math.hypot(t[1], t[2]))
        r0, b0 = math.hypot(near[1], near[2]), bearing(near[1], near[2])
        for t in ts:
            if t is near:
                continue
            r, b = math.hypot(t[1], t[2]), bearing(t[1], t[2])
            extra.append({
                "dist": math.hypot(t[1] - near[1], t[2] - near[2]),
                "dr": r - r0, "db": b - b0, "x": t[1], "y": t[2], "speed": t[3],
            })
    if not extra:
        print("  never more than one target")
        return
    split = sum(1 for e in extra if e["dist"] < 600)
    mirror = sum(1 for e in extra if e["dist"] >= 600 and abs(e["db"]) < 15 and e["dr"] > 600)
    other = len(extra) - split - mirror
    moving = sum(1 for e in extra if abs(e["speed"]) > 0)
    med = lambda k: statistics.median(e[k] for e in extra)
    print(f"  extra target, relative to the nearest one (presumably you):")
    print(f"    distance between them: median {med('dist'):.0f} mm")
    print(f"    further away by:       median {med('dr'):.0f} mm, bearing offset median {med('db'):+.0f} deg")
    print(f"    typical position:      x={med('x'):.0f} mm, y={med('y'):.0f} mm")
    print(f"    moving (speed != 0):   {moving / len(extra):.0%} of the time")
    print(f"  pattern: split body {split / len(extra):.0%} | mirror behind you {mirror / len(extra):.0%}"
          f" | elsewhere {other / len(extra):.0%}")


def main():
    parser = argparse.ArgumentParser(description="Records raw LD2450 targets to see where ghost targets come from.")
    parser.add_argument("--seconds", type=float, default=20.0, help="length of each phase")
    args = parser.parse_args()

    logging_setup.configure_logging(config)
    quiet_console()
    from adapters.factory import build_radar

    bus = EventBus()
    radar = build_radar(config, bus)
    frames = []
    bus.subscribe(RadarTargetsUpdated, lambda e: frames.append(
        (time.monotonic(), [(t.get("id"), t.get("x_mm"), t.get("y_mm"), t.get("speed_mms") or 0)
                            for t in e.targets if t.get("x_mm") is not None and t.get("y_mm") is not None])))
    radar.start()
    deadline = time.monotonic() + 10
    while not frames and time.monotonic() < deadline:
        time.sleep(0.1)
    if not frames:
        raise SystemExit("No radar frames - is the main app stopped and the sensor powered?")

    os.makedirs(config.STUDY_DIR, exist_ok=True)
    path = os.path.join(config.STUDY_DIR, f"radar_capture_{datetime.now():%Y%m%d_%H%M%S}.csv")
    by_phase = {}
    for name, instruction in PHASES:
        input(f"\nPhase '{name}' ({args.seconds:.0f} s): {instruction}\nPress ENTER to start...")
        start = len(frames)
        end_t = time.monotonic() + args.seconds
        while time.monotonic() < end_t:
            time.sleep(0.2)
        by_phase[name] = frames[start:]
        print("  done")
    radar.stop()

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["phase", "t", "slot", "x_mm", "y_mm", "speed_mms"])
        for name, fs in by_phase.items():
            for t, ts in fs:
                for slot, x, y, v in ts:
                    w.writerow([name, f"{t:.3f}", slot, x, y, v])
    for name, fs in by_phase.items():
        summarize(name, fs)
    print(f"\nRaw data: {path}")


if __name__ == "__main__":
    main()
