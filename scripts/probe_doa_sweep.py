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

STUDY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "study")
DEFAULT_HEADINGS = [0, 20, 40, 60, 80, 100, 120, 140, 160, 180]
POLL_S = 0.05
MOVE_SETTLE_S = 1.2
RECORD_S = 2.5


def wrap180(degrees):
    return (degrees + 180.0) % 360.0 - 180.0


def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        return "q"


def circular_offset(pairs):
    """The single rotation that best explains reported-against-true, as a
    circular mean - so a set straddling the 180 seam is not averaged through
    zero."""
    xs = sum(math.cos(math.radians(wrap180(r - t))) for r, t in pairs)
    ys = sum(math.sin(math.radians(wrap180(r - t))) for r, t in pairs)
    return math.degrees(math.atan2(ys, xs))


def fit(pairs, mirrored):
    """How well reported turns follow true turns, either directly or
    mirrored. Returns (offset, median |residual|, worst |residual|)."""
    signed = [(r, -t if mirrored else t) for r, t in pairs]
    offset = circular_offset(signed)
    residuals = [abs(wrap180(r - t - offset)) for r, t in signed]
    return offset, statistics.median(residuals), max(residuals)


def collect(doa, convert, seconds):
    """Voiced readings over `seconds`, as (age, raw, converted) triples. The
    age matters: this array's bearing is a tracker that re-converges after
    every change in the scene - including the head's own movement - so a
    reading is only as good as the time the array has had to settle."""
    out = []
    started = time.monotonic()
    deadline = started + seconds
    while time.monotonic() < deadline:
        if doa.get_voice_active():
            raw = doa.get_raw_direction_degrees()
            out.append((time.monotonic() - started, raw, convert(raw)))
        time.sleep(POLL_S)
    return out


def settled_tail(readings, tail_s):
    """The readings from the last tail_s of a recording - what the array had
    converged on, as opposed to where it started."""
    if not readings:
        return []
    last = readings[-1][0]
    tail = [c for age, _, c in readings if age >= last - tail_s]
    return tail or [readings[-1][2]]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure the array's bearings all round the head using the servo as the protractor: "
                    "the talker stays in one place and the head sweeps, so every relative angle comes "
                    "from the servo rather than from where somebody judged they were standing.")
    parser.add_argument("--source", type=float, required=True,
                        help="where the talker stands, in the head's home frame: 90 straight ahead, "
                             "0 and 180 are the two sides, 240/300 behind. Standing at 0 or 180 "
                             "(square to the side of the head) is the easiest to place accurately "
                             "and sweeps the relative bearing from straight ahead to dead astern.")
    parser.add_argument("--headings", type=float, nargs="+", default=DEFAULT_HEADINGS,
                        help="servo positions to step through (default 0 to 180 in 20 degree steps)")
    parser.add_argument("--distance", type=float, required=True)
    parser.add_argument("--seconds", type=float, default=RECORD_S,
                        help="how long to listen at each heading")
    parser.add_argument("--pause", action="store_true",
                        help="wait for ENTER before each heading, so a talker can breathe between them "
                             "instead of holding one unbroken stretch")
    parser.add_argument("--tail", type=float, default=1.5,
                        help="how much of the end of each recording counts as the converged bearing")
    parser.add_argument("--session", default=datetime.now().strftime("%Y%m%d_%H%M"))
    args = parser.parse_args()

    logging_setup.configure_logging(config)

    if not config.USE_SERVO:
        print("USE_SERVO is False - this probe needs the servo to set the angles.")
        return

    from adapters.factory import build_turntable
    from adapters.hardware.doa_respeaker import (RespeakerDOAAdapter, apply_calibration,
                                                 load_calibration, median_angle,
                                                 raw_to_target_degrees)

    turntable = build_turntable(config)
    doa = RespeakerDOAAdapter(
        front_reference_degrees=config.DOA_FRONT_REFERENCE_DEGREES,
        offaxis_gain=config.DOA_OFFAXIS_GAIN,
        calibration_path=config.DOA_CALIBRATION_PATH,
    )
    table = None
    if config.DOA_CALIBRATION_PATH and os.path.exists(config.DOA_CALIBRATION_PATH):
        try:
            table = load_calibration(config.DOA_CALIBRATION_PATH)
        except ValueError:
            table = None

    def convert(raw):
        angle = raw_to_target_degrees(raw, config.DOA_FRONT_REFERENCE_DEGREES, config.DOA_OFFAXIS_GAIN)
        return apply_calibration(angle, table) if table else angle

    lo, hi = turntable.safe_min_angle, turntable.safe_max_angle
    headings = [h for h in args.headings if lo <= h <= hi]
    skipped = [h for h in args.headings if h not in headings]
    if skipped:
        print(f"Outside the servo's window {lo:.0f}-{hi:.0f}, skipped: "
              f"{' '.join(f'{h:g}' for h in skipped)}")
    if not headings:
        print("No usable headings.")
        return

    path = os.path.join(STUDY_DIR, f"doa_sweep_{args.session}.csv")
    os.makedirs(STUDY_DIR, exist_ok=True)
    columns = ["timestamp", "session", "source_bearing", "distance_m", "heading",
               "expected", "raw_median", "reported", "error", "n_readings", "raw", "reported_all",
               "ages"]
    with open(path, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=columns).writeheader()

    print(f"\nSweep probe - talker at {args.source:g} in the home frame, {args.distance} m away")
    print(f"Writing {path}")
    if lo <= args.source <= hi:
        # Same trick calibrate_doa_angles.py uses for the front half: the
        # servo defines the direction, so nobody has to work out where a
        # numbered bearing is. This is the whole point of the probe - the
        # earlier circle run had to describe the spot in words for angles the
        # head cannot point at, and a described spot is a spot that can be
        # misread.
        turntable.set_angle_immediate(args.source)
        print("\nThe head is now pointing AT your spot. Stand exactly where its nose points,")
        print(f"{args.distance} m away. The head returns to sweeping once you press ENTER.")
    else:
        print(f"\n{args.source:g} is outside the servo's window, so the head cannot point it out.")
        print("Prefer --source 0 or --source 180, which it can.")
    if args.pause:
        print(f"\n{len(headings)} recordings of {args.seconds:.0f} s, one per heading, with a pause between")
        print("each. Keep talking for the whole of each recording - read something aloud.")
    else:
        print("\nKeep talking without stopping until the sweep ends")
        print(f"(about {len(headings) * (args.seconds + MOVE_SETTLE_S + 0.5):.0f} s). Read something aloud.")
    if ask("ENTER to start, q to quit > ").strip().lower() == "q":
        return

    rows = []
    for heading in headings:
        if args.pause:
            if ask(f"\n  heading {heading:g}: ENTER, then start talking (q to stop) > ").strip().lower() == "q":
                break
        turntable.set_angle_immediate(heading)
        time.sleep(MOVE_SETTLE_S)
        if args.pause:
            print(f"      recording {args.seconds:.0f} s - keep talking")
        expected = 90.0 + (args.source - heading)
        readings = collect(doa, convert, args.seconds)
        if not readings:
            print(f"  heading {heading:6.1f} -> expected {wrap180(expected - 90) + 90:7.1f}   no voice heard")
            continue
        raws = [r for _, r, _ in readings]
        converted = [c for _, _, c in readings]
        idle = sum(1 for c in converted if c == 90.0)
        reported = median_angle(settled_tail(readings, args.tail))
        error = wrap180(reported - expected)
        buckets = []
        for second in range(int(args.seconds)):
            inside = [c for age, _, c in readings if second <= age < second + 1]
            buckets.append(f"{abs(wrap180(median_angle(inside) - expected)):4.0f}" if inside else "   -")
        note = f"  ALL IDLE ({idle}/{len(converted)} readings exactly 90)" if idle == len(converted) else ""
        print(f"  heading {heading:6.1f} -> expected {wrap180(expected - 90) + 90:7.1f}   "
              f"settled {reported:7.1f}   error {error:+7.1f}   n={len(readings)}{note}")
        print(f"      |error| by second: {' '.join(buckets)}")
        if idle == len(converted):
            continue
        rows.append((heading, expected, reported, error, len(readings)))
        with open(path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=columns).writerow({
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "session": args.session, "source_bearing": args.source,
                "distance_m": args.distance, "heading": heading,
                "expected": round(expected, 2), "raw_median": round(statistics.median(raws), 2),
                "reported": round(reported, 2), "error": round(error, 2),
                "n_readings": len(readings),
                "raw": " ".join(f"{r:.0f}" for r in raws),
                "reported_all": " ".join(f"{c:.0f}" for c in converted),
                "ages": " ".join(f"{age:.2f}" for age, _, _ in readings),
            })

    turntable.home(ramp_duration_s=0.6)
    doa.close()

    if len(rows) < 3:
        print("\nToo few headings with voice to say anything.")
        return

    pairs = [(reported - 90.0, expected - 90.0) for _, expected, reported, _, _ in rows]
    front = [(r, t) for r, t in pairs if abs(t) <= 90.0]
    rear = [(r, t) for r, t in pairs if abs(t) > 90.0]

    print("\n" + "=" * 78)
    print("  Converged bearings only (the last of each recording), servo defines the truth")
    print(f"  {'set':12} {'n':>3}  {'direct fit':>26}  {'mirrored fit':>26}")
    for label, subset in (("everything", pairs), ("in front", front), ("behind", rear)):
        if len(subset) < 2:
            print(f"  {label:12} {len(subset):>3}  too few points")
            continue
        o1, m1, w1 = fit(subset, mirrored=False)
        o2, m2, w2 = fit(subset, mirrored=True)
        print(f"  {label:12} {len(subset):>3}  "
              f"offset {o1:+6.1f} median |err| {m1:5.1f}  "
              f"offset {o2:+6.1f} median |err| {m2:5.1f}")

    o1, m1, _ = fit(pairs, mirrored=False)
    o2, m2, _ = fit(pairs, mirrored=True)
    print()
    if len(pairs) < 4 or min(m1, m2) > 30.0:
        # One straight line through readings that disagree by more than the
        # angles being measured says nothing, and a verdict printed anyway
        # gets believed. Report the refusal instead.
        print(f"  No fit: {len(pairs)} usable heading(s), best median error {min(m1, m2):.0f} deg.")
        print("  The readings are too inconsistent for a single offset to mean anything -")
        print("  read the per-second rows above instead, heading by heading.")
    elif m1 <= m2:
        print(f"  The direct reading fits best (median error {m1:.1f} deg at an offset of {o1:+.1f}).")
        if abs(o1) > 10.0:
            print(f"  That offset is not zero: DOA_FRONT_REFERENCE_DEGREES is out by about {o1:+.0f} deg,")
            print(f"  or the talker stood {o1:+.0f} deg from the mark.")
    else:
        print(f"  The MIRRORED reading fits best (median error {m2:.1f} deg at an offset of {o2:+.1f}),")
        print("  which means the array counts angles the other way round from raw_to_target_degrees().")
    if len(rear) >= 2 and len(front) >= 2:
        _, mf, _ = fit(front, mirrored=False)
        _, mr, _ = fit(rear, mirrored=False)
        print(f"  In front the direct fit is off by {mf:.1f} deg, behind by {mr:.1f} deg.")
        if mr > 3 * max(mf, 1.0):
            print("  Behind the head the bearings are much worse than in front, so the rear is the")
            print("  problem rather than the labelling.")
    print(f"\nSaved to {path}")


if __name__ == "__main__":
    main()
