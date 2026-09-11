import csv
import json
import statistics
import sys

if len(sys.argv) < 4:
    sys.exit("usage: python3 pick_trials.py <study.csv> <trials.jsonl> <settings.json with door_zone>")
csv_path, jsonl_path, settings_path = sys.argv[1:4]
rows = {r["trial_id"]: r for r in csv.DictReader(open(csv_path, newline=""))}
logs = {}
for line in open(jsonl_path):
    if line.strip():
        d = json.loads(line)
        logs[str(d["trial_id"])] = d
door = json.load(open(settings_path)).get("door_zone") or {}


def inside(x, y, z):
    return z.get("valid") and z["min_x_mm"] <= x <= z["max_x_mm"] and z["min_y_mm"] <= y <= z["max_y_mm"]


def planned(r, ev):
    s = r["script_id"]
    if (r.get("notes") or "").strip():
        return False
    if any(e["kind"] == "modality" and e.get("reason") == "reset" for e in ev):
        return False
    if s in ("A", "F"):
        return (r["classification"] == "TP" and r["entry_via"] == "door" and r["reverted"] == "Y"
                and r["door_outcome"] == "entered")
    if s == "B":
        return r["classification"] == "TN" and r["ducked"] == "N"
    if s == "D":
        return (r["classification"] == "TN" and r["ducked"] == "Y" and r["door_outcome"] == "gone"
                and bool(r["unduck_ts"]))
    return False


def quality(ev):
    frames = [e for e in ev if e["kind"] == "frame"]
    if not frames:
        return None
    ghosts = sum(1 for f in frames if len(f["targets"]) > 1) / len(frames)
    seen = [len(f["targets"]) > 0 for f in frames]
    first = seen.index(True) if True in seen else len(seen)
    last = len(seen) - 1 - seen[::-1].index(True) if True in seen else -1
    dropouts = sum(1 for a, b in zip(seen[first:last], seen[first + 1:last + 1]) if a and not b)
    return {"ghosts": ghosts, "dropouts": dropouts, "duration": frames[-1]["t"] - frames[0]["t"]}


def fits_door(ev):
    doors = [e for e in ev if e["kind"] == "door"]
    return all(inside(e["x"], e["y"], door) for e in doors)


by_case = {}
for tid, r in rows.items():
    d = logs.get(tid)
    if not d or r.get("configuration") != "door" or (r.get("simulated") or "0") == "1":
        continue
    ev = d["events"]
    if not planned(r, ev) or not fits_door(ev):
        continue
    q = quality(ev)
    if q:
        by_case.setdefault(r["script_id"], []).append((tid, q, r))

print(f"door zone from settings: {door}")
picks = []
for case in ("A", "F", "B", "D"):
    cands = by_case.get(case, [])
    if not cands:
        print(f"{case}: no trial went fully to plan")
        continue
    med = statistics.median(q["duration"] for _, q, _ in cands)
    cands.sort(key=lambda c: (c[1]["ghosts"] * 10 + c[1]["dropouts"], abs(c[1]["duration"] - med)))
    tid, q, r = cands[0]
    picks.append(tid)
    print(f"{case}: trial {tid}  (of {len(cands)} clean candidates)  ghosts {q['ghosts']:.0%}  "
          f"dropouts {q['dropouts']}  {q['duration']:.1f} s  speech start {r['speech_start_ts']}")
print("PICK=" + ",".join(picks))
