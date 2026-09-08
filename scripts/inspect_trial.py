import argparse
import glob
import json
import os

STUDY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "study")


def load(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def show(rec):
    cond = rec["condition"]
    print("=" * 72)
    print(f"trial {rec['trial_id']}  angle_true={cond['angle_true']}  "
          f"distance={cond['distance_m']}m  noise={cond['noise_condition']}  rep={rec['rep']}")
    print(f"miss={rec['miss']}  settled={rec['settled']}  mode={rec.get('playback_mode', '?')}")
    events = []
    for s in rec["doa_samples"]:
        events.append((s["t"], "doa", s))
    for m in rec["moves"]:
        events.append((m["t"], "move", m))
    if not events:
        print("  no events recorded")
        return
    t0 = min(e[0] for e in events)
    events.sort(key=lambda e: e[0])
    print(f"\n  {'t+s':>7}  {'event':<6} {'reading':>9}  {'head':>7}  note")
    print("  " + "-" * 62)
    for t, kind, e in events:
        rel = t - t0
        if kind == "doa":
            implied = e["head_heading"] + (e["doa_angle"] - 90.0)
            flag = "" if 0.0 <= implied <= 180.0 else "  <-- outside reachable arc"
            print(f"  {rel:7.3f}  {'DOA':<6} {e['doa_angle']:9.1f}  "
                  f"{e['head_heading']:7.1f}  implies {implied:6.1f}{flag}")
        else:
            before = e.get("heading_before")
            arrow = f"{before:.1f} -> {e['resulting_heading']:.1f}" if before is not None else \
                    f"-> {e['resulting_heading']:.1f}"
            print(f"  {rel:7.3f}  {e['kind'].upper():<6} {str(e['target']):>9}  "
                  f"{e['resulting_heading']:7.1f}  {arrow}")
    doas = [s["doa_angle"] for s in rec["doa_samples"]]
    if doas:
        print(f"\n  {len(doas)} DOA samples, min={min(doas):.1f} max={max(doas):.1f} "
              f"spread={max(doas) - min(doas):.1f} deg")
    tracks = [m for m in rec["moves"] if m["kind"] == "track"]
    if len(tracks) > 1:
        print(f"  {len(tracks)} track moves - head moved more than once this trial")


def main():
    parser = argparse.ArgumentParser(description="Show the DOA samples and servo moves of a trial.")
    parser.add_argument("jsonl", nargs="?", help="trial log (default: newest in study/)")
    parser.add_argument("--trial", type=int, action="append", help="only this trial id (repeatable)")
    parser.add_argument("--misses", action="store_true", help="only trials recorded as a miss")
    parser.add_argument("--last", type=int, help="only the last N trials")
    args = parser.parse_args()

    path = args.jsonl
    if path is None:
        cands = sorted(glob.glob(os.path.join(STUDY_DIR, "*_trials.jsonl")),
                       key=os.path.getmtime)
        if not cands:
            print("No trial logs found in study/.")
            return
        path = cands[-1]
    print(f"# {path}\n")
    recs = load(path)
    if args.trial:
        recs = [r for r in recs if r["trial_id"] in args.trial]
    if args.misses:
        recs = [r for r in recs if r["miss"]]
    if args.last:
        recs = recs[-args.last:]
    if not recs:
        print("Nothing matched.")
        return
    for r in recs:
        show(r)


if __name__ == "__main__":
    main()
