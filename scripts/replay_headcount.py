"""Replays a Study 2 session through a head-count policy instead of the
doorway logic, so the design choice can be reported as a measurement rather
than as a development observation.

The deployed logic decides arrivals and departures at the door zone only
(adapters/hardware/radar_ld2450.py). The obvious alternative is to count the
people the radar reports inside the room and react whenever that count rises:
no zones at the doorway, no tracking through them, just "somebody more is in
here than when the reply started".

This script runs that alternative over the recorded frames of a session and
reports what it would have done, against what the deployed logic actually did.
A target counts as "in the room" when it lies inside the recorded room zone;
the baseline is the count during the first --baseline-s of the trial, so a
session with somebody already seated (the second installation) starts at one.
--min-frames requires the raised count to persist, which is the cheapest
defence a head-count implementation would have against a one-frame ghost.

    python3 scripts/replay_headcount.py study/privacy_switch_study2.csv
    python3 scripts/replay_headcount.py study/privacy_switch_study2_holdout.csv --min-frames 3
"""

import argparse
import glob
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyse_privacy_switch as analysis

ENTRY_SCRIPTS = ("A", "E", "F")


def in_room(target, zone):
    """True if the target lies inside the recorded room zone."""
    d = analysis.zone_distance(target["x"], target["y"], zone)
    return d is not None and d <= 0


def headcount_trial(trial, baseline_s, min_frames):
    """Returns (t_switch, t_back, baseline, peak) for the head-count policy.

    t_switch is the first moment the count stays above the baseline for
    min_frames consecutive frames; t_back the first moment it is back at the
    baseline for as long, which is where such a policy would resume speech.
    """
    zone = trial["zone"]
    frames = [f for f in trial["events"] if f["kind"] == "frame"]
    if not frames:
        return None, None, 0, 0
    t0 = frames[0]["t"]
    counts = [(f["t"], sum(1 for t in f["targets"] if in_room(t, zone))) for f in frames]
    baseline_window = [c for t, c in counts if t - t0 <= baseline_s]
    baseline = min(baseline_window) if baseline_window else 0

    t_switch = t_back = None
    run_up = run_down = 0
    for t, c in counts:
        if c > baseline:
            run_up += 1
            run_down = 0
            if t_switch is None and run_up >= min_frames:
                t_switch = t
        else:
            run_down += 1
            run_up = 0
            if t_switch is not None and t_back is None and run_down >= min_frames:
                t_back = t
    return t_switch, t_back, baseline, max(c for _, c in counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", nargs="?", help="session CSV (default: the newest study/privacy_switch_*.csv)")
    parser.add_argument("--settings", help="JSON with the door_zone used (default: <csv>_settings.json)")
    parser.add_argument("--baseline-s", type=float, default=1.0,
                        help="seconds at the start of a trial that define the baseline count (default: 1.0)")
    parser.add_argument("--min-frames", type=int, default=1,
                        help="consecutive frames the changed count must persist (default: 1)")
    args = parser.parse_args()

    study = os.path.join(ROOT, "study")
    path = args.csv or max((p for p in glob.glob(os.path.join(study, "privacy_switch_*.csv"))
                            if not p.endswith("_video.csv")), key=os.path.getmtime)
    rows, logs, door = analysis.load(path, args.settings)
    print(f"\n{os.path.basename(path)}: {len(rows)} trials through a head-count policy "
          f"(baseline over {args.baseline_s:g}s, {args.min_frames} frame(s) persistence)")

    live, head = Counter(), Counter()
    live_ret, head_ret = Counter(), Counter()
    false_switch_trials, missed_trials = [], []
    for r in rows:
        trial = logs.get(r["trial_id"])
        if trial is None:
            continue
        script = r["script_id"]
        entry = script in ENTRY_SCRIPTS
        t_switch, t_back, baseline, peak = headcount_trial(trial, args.baseline_s, args.min_frames)
        live_cls = analysis.analyse_trial(r, trial, door)["cls"]
        live[live_cls] += 1
        cls = ("TP" if t_switch is not None else "FN") if entry else ("FP" if t_switch is not None else "TN")
        head[cls] += 1
        if cls == "TP":
            live_trial = analysis.analyse_trial(r, trial, door)
            live_switch = live_trial["switch"]["t"] if live_trial.get("switch") else None
            stay_s = (trial.get("session_info") or {}).get("stay_s", analysis.STAY_S)
            if live_switch is not None:
                cue = live_switch + stay_s
                ret = "missed" if t_back is None else ("premature" if t_back < cue else "correct")
                head_ret[ret] += 1
            if live_trial.get("reversion"):
                live_ret[live_trial["reversion"]] += 1
        if cls == "FP":
            false_switch_trials.append((r["trial_id"], script, baseline, peak))
        if cls == "FN":
            missed_trials.append((r["trial_id"], script, baseline, peak))

    entries = head["TP"] + head["FN"]
    others = head["FP"] + head["TN"]
    print("\n                    doorway logic   head count")
    for c in ("TP", "FN", "FP", "TN"):
        print(f"  {c:18} {live[c]:>8}  {head[c]:>11}")
    if entries:
        print(f"\n  entries detected    {live['TP']:>8}/{entries}  {head['TP']:>11}/{entries}")
    if others:
        print(f"  false switches      {live['FP']:>8}/{others}  {head['FP']:>11}/{others}")
    if head_ret or live_ret:
        print("\n  after a detected entry, speech comes back:")
        for c in ("correct", "premature", "missed"):
            print(f"    {c:16} {live_ret[c]:>8}  {head_ret[c]:>11}")
    if false_switch_trials:
        print("\n  head count switched with nobody entering:")
        for tid, script, base, peak in false_switch_trials:
            print(f"    trial {tid:>3} {script}: baseline {base}, peak {peak} target(s) in the room")
    if missed_trials:
        print("\n  head count missed an entry:")
        for tid, script, base, peak in missed_trials:
            print(f"    trial {tid:>3} {script}: baseline {base}, peak {peak} target(s) in the room")
    print()


if __name__ == "__main__":
    main()
