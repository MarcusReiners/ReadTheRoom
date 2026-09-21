import argparse
import csv
import glob
import os
import statistics
from collections import defaultdict


def turn(angle):
    return (angle - 90.0 + 180.0) % 360.0 - 180.0


def side(angle):
    t = turn(angle)
    if abs(t) < 1.0 or abs(t) > 179.0:
        return 0
    return 1 if t > 0 else -1


def servo_side(estimate):
    t = estimate - 90.0
    if abs(t) < 1.0:
        return 0
    return 1 if t > 0 else -1


def median_angle(angles):
    base = angles[0]
    unwrapped = [base + ((a - base + 180.0) % 360.0 - 180.0) for a in angles]
    return 90.0 + ((statistics.median(unwrapped) - 90.0 + 180.0) % 360.0 - 180.0)


def settle_first(polls, onset):
    """The current default: skip 1.5 s after onset, then voiced readings at
    least 0.25 s apart, up to five, at least three."""
    t0 = polls[onset]["t"] + 1.5
    picked, next_t = [], t0
    for p in polls[onset:]:
        if p["t"] >= next_t and p["voice"]:
            picked.append(p)
            next_t = p["t"] + 0.25
        if len(picked) == 5:
            break
    return picked if len(picked) >= 3 else None


def onset_raw(polls, onset):
    """The earlier default: the onset reading and voiced readings within
    0.6 s after it, up to five."""
    t0 = polls[onset]["t"]
    picked = [p for p in polls[onset:] if p["voice"] and p["t"] - t0 <= 0.6][:5]
    return picked if len(picked) >= 2 else None


def onset_fresh(polls, onset, window_s=0.8, change_deg=5.0, take=3):
    """The first readings after onset that differ from the value the array
    still showed before the voice started - the array's first estimate of
    the new sound, before reflections of sustained speech weigh in. If the
    value never changes, the talker is where the last sound came from."""
    t0 = polls[onset]["t"]
    baseline = polls[onset - 1]["target"] if onset > 0 else None
    window = [p for p in polls[onset:] if p["voice"] and p["t"] - t0 <= window_s]
    if baseline is None:
        fresh = window
    else:
        first = next((i for i, p in enumerate(window)
                      if abs(turn(p["target"] - baseline + 90.0)) > change_deg), None)
        fresh = window[first:] if first is not None else [p for p in window if p["t"] - t0 >= 0.2]
    picked = fresh[:take]
    return picked if len(picked) >= 2 else None


ESTIMATORS = [("settle-first (now)", settle_first), ("onset raw (before)", onset_raw),
              ("onset fresh", onset_fresh)]


def load(paths):
    reps = defaultdict(list)
    for path in paths:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                reps[(r["session"], float(r["true_angle"]), int(r["rep"]))].append({
                    "t": float(r["t"]), "raw": float(r["raw"]), "target": float(r["target"]),
                    "voice": int(r["voice"]), "speech": int(r["speech"]),
                })
    return reps


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare direction estimators on probe_doa_circle.py recordings.")
    parser.add_argument("polls", nargs="*", help="study/doa_circle_*_polls.csv (default: all)")
    parser.add_argument("--reps", action="store_true", help="also list every recording")
    args = parser.parse_args()
    study = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "study")
    paths = args.polls or sorted(glob.glob(os.path.join(study, "doa_circle_*_polls.csv")))
    if not paths:
        print("No doa_circle_*_polls.csv found - run scripts/probe_doa_circle.py first.")
        return
    reps = load(paths)
    print(f"{len(reps)} recordings from {', '.join(os.path.basename(p) for p in paths)}")
    print("right way = the head would turn towards the source; behind = the estimate points more than 90 deg")
    print("off the nose, i.e. past the side of the head; error only for sources in front of the head.\n")

    results = defaultdict(lambda: defaultdict(list))
    for (session, angle, rep), polls in sorted(reps.items()):
        polls.sort(key=lambda p: p["t"])
        onset = next((i for i, p in enumerate(polls) if p["voice"]), None)
        line = []
        for name, estimator in ESTIMATORS:
            picked = estimator(polls, onset) if onset is not None else None
            if picked is None:
                results[name][angle].append(None)
                line.append(f"{name}: -")
                continue
            est = median_angle([p["target"] for p in picked])
            decided = picked[-1]["t"] - polls[onset]["t"]
            results[name][angle].append((est, decided))
            readings = " ".join("%.0f" % p["target"] for p in picked)
            line.append(f"{name}: {est:.0f} ({readings}) at {decided:.2f}s")
        if args.reps:
            print(f"  {angle:>5g} rep {rep}: " + " | ".join(line))

    angles = sorted({a for per in results.values() for a in per})
    for name, _ in ESTIMATORS:
        print(f"\n{name}")
        print(f"  {'true':>6} {'n':>3} {'none':>5} {'right way':>10} {'behind':>7} {'median |error|':>15} {'decided after':>14}")
        totals = defaultdict(int)
        for angle in angles:
            got = results[name][angle]
            done = [g for g in got if g is not None]
            s = side(angle)
            right = sum(1 for est, _ in done if s and servo_side(est) == s)
            behind = sum(1 for est, _ in done if abs(turn(est)) > 90.0)
            errors = [abs(turn(est - angle + 90.0)) for est, _ in done] if abs(turn(angle)) <= 90 else []
            times = [d for _, d in done]
            totals["n"] += len(got)
            totals["none"] += len(got) - len(done)
            totals["right"] += right
            totals["sided"] += len(done) if s else 0
            totals["behind"] += behind
            print(f"  {angle:>6g} {len(got):>3} {len(got) - len(done):>5} "
                  f"{f'{right}/{len(done)}' if s else '-':>10} {behind:>7} "
                  f"{f'{statistics.median(errors):.1f}' if errors else '-':>15} "
                  f"{f'{statistics.median(times):.2f}s' if times else '-':>14}")
        print(f"  all: {totals['n']} recordings, {totals['none']} without an estimate, right way "
              f"{totals['right']}/{totals['sided']}, behind {totals['behind']}")


if __name__ == "__main__":
    main()
