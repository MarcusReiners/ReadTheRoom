import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup
from adapters.app_settings import load_app_settings, update_app_setting
from adapters.radar_alignment import clean_pose, pose_from_marks, target_bearing
from domain.events import RadarTargetsUpdated
from service_layer.bus import EventBus

DEFAULT_MARKS = ["20:1.0", "90:1.0", "160:1.0", "50:1.8", "130:1.8"]
COLLECT_S = 4.0


def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        return "q"


class Targets:
    """The radar's latest frame, kept with the time it arrived."""

    def __init__(self, bus):
        self.latest = []
        self.at = 0.0
        bus.subscribe(RadarTargetsUpdated, self._on_update)

    def _on_update(self, event):
        self.latest = list(event.targets)
        self.at = time.monotonic()

    def fresh(self, max_age_s=1.5):
        if time.monotonic() - self.at > max_age_s:
            return []
        return [t for t in self.latest
                if t.get("x_mm") is not None and t.get("y_mm") is not None]


def collect_point(targets, seconds):
    """The median position of whatever the radar sees, over `seconds`. With
    several targets the nearest one is taken, since the person standing on
    the mark is the closest thing to the sensor's view of it."""
    xs, ys, frames, seen = [], [], 0, 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        current = targets.fresh()
        frames += 1
        if current:
            seen += 1
            nearest = min(current, key=lambda t: (float(t["x_mm"]) ** 2 + float(t["y_mm"]) ** 2))
            xs.append(float(nearest["x_mm"]))
            ys.append(float(nearest["y_mm"]))
        time.sleep(0.1)
    if not xs:
        return None, 0.0
    return (statistics.median(xs), statistics.median(ys)), seen / max(1, frames)


def run_check(targets, alignment):
    print("\nWalk around the head. Every radar target is shown as the head sees it.")
    print("Ctrl+C to stop.\n")
    try:
        while True:
            current = targets.fresh()
            if not current:
                print("  nothing on the radar", end="\r")
            else:
                parts = []
                for t in current:
                    bearing, distance = target_bearing(float(t["x_mm"]), float(t["y_mm"]), alignment)
                    parts.append(f"bearing {bearing:6.1f} deg at {distance / 1000.0:4.2f} m")
                print("  " + " | ".join(parts) + " " * 20, end="\r")
            time.sleep(0.2)
    except KeyboardInterrupt:
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Work out where the radar unit sits relative to the head, so a radar target can "
                    "be turned into a bearing the servo understands. The head points at each mark "
                    "in turn, so the directions come from the servo rather than from a tape measure "
                    "on the floor.")
    parser.add_argument("--marks", nargs="+", default=DEFAULT_MARKS,
                        help="bearing:metres pairs to stand on, e.g. 20:1.0 90:1.0 160:1.8 "
                             "(90 is straight ahead of the head's home position)")
    parser.add_argument("--seconds", type=float, default=COLLECT_S,
                        help="how long to watch the radar at each mark")
    parser.add_argument("--check", action="store_true",
                        help="skip the marks and just show live radar targets as bearings")
    parser.add_argument("--path", default=config.APP_SETTINGS_PATH,
                        help="the settings file the web app shares (head_pose is stored in it)")
    args = parser.parse_args()

    logging_setup.configure_logging(config)

    if config.RADAR_PROVIDER != "ld2450":
        print(f"RADAR_PROVIDER is {config.RADAR_PROVIDER!r} - no radar to align.")
        return

    from adapters.factory import build_radar, build_turntable

    bus = EventBus()
    targets = Targets(bus)
    radar = build_radar(config, bus)
    radar.start()
    turntable = build_turntable(config) if config.USE_SERVO else None

    print("\nWaiting for the sensor unit...")
    for _ in range(100):
        if targets.at:
            break
        time.sleep(0.1)
    if not targets.at:
        print("No data from the bridge. Is the sensor unit powered and in range?")
        return
    print("Sensor unit is sending.")

    if args.check:
        pose = clean_pose(load_app_settings(args.path, defaults={}).get("head_pose"))
        if pose is None:
            print(f"The assistant has not been placed yet ({args.path}). Drag it onto the radar view "
                  "in the web app, or run this without --check.")
            return
        print(f"Using {args.path}: assistant at {pose['x_mm']:.0f}/{pose['y_mm']:.0f} mm, "
              f"facing {pose['facing_degrees']:.0f} deg")
        run_check(targets, pose)
        return

    if turntable is None:
        print("USE_SERVO is False - the head cannot point out the marks.")
        return

    pairs = []
    for mark in args.marks:
        try:
            bearing_s, distance_s = mark.split(":")
            bearing, distance_mm = float(bearing_s), float(distance_s) * 1000.0
        except ValueError:
            print(f"Skipping {mark!r}: expected bearing:metres, e.g. 20:1.0")
            continue
        lo, hi = turntable.safe_min_angle, turntable.safe_max_angle
        if not lo <= bearing <= hi:
            print(f"Skipping {bearing:g}: outside the servo's window {lo:.0f}-{hi:.0f}")
            continue

        turntable.set_angle_immediate(bearing)
        time.sleep(0.8)
        print(f"\n--- mark {bearing:g} deg, {distance_mm / 1000.0:.2f} m")
        print("  Stand where the head is pointing now, at that distance.")
        print("  Keep shifting your weight a little - the radar loses people who stand perfectly still.")
        if ask("  ENTER when you are there (s to skip, q to stop) > ").strip().lower() in ("q", "quit"):
            break
        point, share = collect_point(targets, args.seconds)
        if point is None:
            print("  The radar never saw you there - skipped. Is the mark inside its field of view?")
            continue
        print(f"  radar read ({point[0]:.0f}, {point[1]:.0f}) mm, seen in {share * 100:.0f}% of frames")
        pairs.append((point, (bearing, distance_mm)))

    turntable.home(ramp_duration_s=0.6)
    if hasattr(radar, "stop"):
        radar.stop()

    if len(pairs) < 3:
        print(f"\nOnly {len(pairs)} usable mark(s) - three are needed. Nothing saved.")
        return

    pose = pose_from_marks(pairs)
    print("\n" + "=" * 70)
    print(f"  The assistant stands at {pose['x_mm'] / 1000.0:+.2f} / {pose['y_mm'] / 1000.0:+.2f} m "
          f"in the radar's own frame,")
    print(f"  facing {pose['facing_degrees']:+.0f} degrees from the sensor's axis.")
    print("  Error at each mark (bearing, distance): " +
          ", ".join(f"{b:.0f} deg / {d:.0f} mm" for b, d in pose["mark_errors"]))
    worst = max(b for b, _ in pose["mark_errors"])
    if worst > 10.0:
        print(f"  The worst mark is {worst:.0f} degrees out, which is a lot for a fit. Likely causes:")
        print("  standing off the line the head pointed along, a mistyped distance, or the radar")
        print("  having tracked something other than you.")
    update_app_setting(args.path, "head_pose",
                       {k: pose[k] for k in ("x_mm", "y_mm", "facing_degrees", "placed")})
    print("\n  Saved. The web app shows it on the radar view, where it can be dragged.")
    print(f"  Check it by walking around: {sys.argv[0]} --check")


if __name__ == "__main__":
    main()
