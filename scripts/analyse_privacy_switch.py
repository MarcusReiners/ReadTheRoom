import argparse
import csv
import glob
import json
import math
import os
from collections import Counter

STAY_S = 15.0
FN_TARGET = 0.05
MAX_JUMP_MM = 800.0
TRACK_GAP_S = 1.0
SPEED_WINDOW_S = 0.6
EXIT_SPEED_MMS = 500.0
SCRIPT_NAMES = {"A": "direct entry", "B": "passer-by", "D": "peek", "F": "fast entry",
                "C": "passer-by, far", "E": "slow entry"}


def zone_distance(x, y, zone):
    if not zone or not zone.get("valid", True) or x is None or y is None:
        return None
    x0, x1 = sorted((zone["min_x_mm"], zone["max_x_mm"]))
    y0, y1 = sorted((zone["min_y_mm"], zone["max_y_mm"]))
    if x0 <= x <= x1 and y0 <= y <= y1:
        return -min(x - x0, x1 - x, y - y0, y1 - y)
    return math.hypot(max(x0 - x, 0.0, x - x1), max(y0 - y, 0.0, y - y1))


def clopper_pearson(k, n, alpha=0.05):
    if n == 0:
        return float("nan"), float("nan")

    def cdf(x, p):
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(x + 1))

    def solve(target, x):
        lo, hi = 0.0, 1.0
        for _ in range(80):
            mid = (lo + hi) / 2
            if cdf(x, mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    return (0.0 if k == 0 else solve(1 - alpha / 2, k - 1)), (1.0 if k == n else solve(alpha / 2, k))


def zero_failure_n(target, alpha=0.05):
    n = 1
    while clopper_pearson(0, n, alpha)[1] >= target:
        n += 1
    return n


def quantile(vals, q):
    s = sorted(vals)
    if not s:
        return float("nan")
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def rate(label, k, n, indent="  "):
    if n == 0:
        print(f"{indent}{label:34} no trials")
        return
    lo, hi = clopper_pearson(k, n)
    print(f"{indent}{label:34} {k:>2}/{n:<2} = {100 * k / n:5.1f}%   95% CI [{100 * lo:5.1f}, {100 * hi:5.1f}]%")


def dist(label, vals, unit="s", scale=1.0, digits=3, indent="  "):
    vals = [v * scale for v in vals if v is not None]
    if not vals:
        print(f"{indent}{label:34} no data")
        return
    print(f"{indent}{label:34} n={len(vals):<3} median {quantile(vals, .5):8.{digits}f}  "
          f"IQR [{quantile(vals, .25):.{digits}f}, {quantile(vals, .75):.{digits}f}]  "
          f"range [{min(vals):.{digits}f}, {max(vals):.{digits}f}] {unit}")


def load(csv_path, settings_path):
    with open(csv_path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("simulated") or "0") != "1"]
    logs = {}
    jsonl = csv_path[:-4] + "_trials.jsonl"
    with open(jsonl) as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                logs[str(d["trial_id"])] = d
    door = None
    settings_path = settings_path or csv_path[:-4] + "_settings.json"
    if os.path.exists(settings_path):
        with open(settings_path) as f:
            door = json.load(f).get("door_zone")
    return rows, logs, door


def build_tracks(frames):
    live, hist, next_id = {}, {}, 0
    for f in frames:
        pts = [p for p in f["targets"] if p.get("x") is not None and p.get("y") is not None]
        cand = {k: v for k, v in live.items() if f["t"] - v[0] <= TRACK_GAP_S}
        pairs = sorted((math.hypot(p["x"] - v[1], p["y"] - v[2]), i, k)
                       for i, p in enumerate(pts) for k, v in cand.items())
        ids, used = [None] * len(pts), set()
        for d, i, k in pairs:
            if d > MAX_JUMP_MM:
                break
            if ids[i] is None and k not in used:
                ids[i] = k
                used.add(k)
        for i, p in enumerate(pts):
            if ids[i] is None:
                next_id += 1
                ids[i] = next_id
            live[ids[i]] = (f["t"], p["x"], p["y"])
            hist.setdefault(ids[i], []).append((f["t"], p["x"], p["y"], p.get("v") or 0))
    return hist


def track_at(hist, t, x, y):
    best = None
    for k, pts in hist.items():
        for tt, xx, yy, _ in pts:
            if abs(tt - t) <= 0.3:
                d = math.hypot(xx - x, yy - y) + abs(tt - t) * 1000
                if best is None or d < best[0]:
                    best = (d, k)
    return best[1] if best and best[0] < 400 else None


def crossing(pts, zone, inward):
    for (t0, x0, y0, _), (t1, x1, y1, _) in reversed(list(zip(pts, pts[1:]))):
        d0, d1 = zone_distance(x0, y0, zone), zone_distance(x1, y1, zone)
        if d0 is None or d1 is None:
            continue
        if (inward and d0 > 0 >= d1) or (not inward and d0 <= 0 < d1):
            return t0 + (t1 - t0) * d0 / (d0 - d1)
    return None


def ground_speed(pts, t):
    win = [p for p in pts if t - SPEED_WINDOW_S <= p[0] <= t + 0.05]
    if len(win) < 3:
        return None
    mt = sum(p[0] for p in win) / len(win)
    stt = sum((p[0] - mt) ** 2 for p in win)
    if stt == 0:
        return None
    vx = sum((p[0] - mt) * p[1] for p in win) / stt
    vy = sum((p[0] - mt) * p[2] for p in win) / stt
    return math.hypot(vx, vy)


def first(events, kind, after=-1e18, pred=None):
    for e in events:
        if e["kind"] == kind and e["t"] >= after and (pred is None or pred(e)):
            return e
    return None


def last_exit_crossing(hist, room, t_from, t_to):
    best = None
    for k, pts in hist.items():
        t = crossing([p for p in pts if t_from <= p[0] <= t_to], room, inward=False)
        if t is not None and (best is None or t > best[0]):
            best = (t, k)
    return best


def speed_near(pts, t, before=0.5, after=0.5):
    return max((p[3] for p in pts if t - before <= p[0] <= t + after), default=None)


def range_rate(pts):
    if len(pts) < 3:
        return None
    ts = [p[0] for p in pts]
    rs = [math.hypot(p[1], p[2]) for p in pts]
    mt, mr = sum(ts) / len(ts), sum(rs) / len(rs)
    stt = sum((t - mt) ** 2 for t in ts)
    return sum((t - mt) * (r - mr) for t, r in zip(ts, rs)) / stt if stt else None


def phantom(hist, k, room, t_switch, t_until):
    pts = [p for p in hist.get(k, []) if t_switch <= p[0] <= t_until]
    t_out = crossing(pts, room, inward=False)
    if t_out is None:
        return None
    after = [p for p in pts if p[0] >= t_out]
    return {"t_out": t_out, "after_switch": t_out - t_switch, "end": after[-1][0] if after else t_out,
            "v_abs_median": quantile([abs(p[3]) for p in after], .5) if after else None,
            "moved_mm": math.hypot(after[-1][1] - after[0][1], after[-1][2] - after[0][2]) if after else 0.0}


def analyse_trial(r, d, door):
    ev = d["events"]
    room = d["zone"]
    door = d.get("door_zone") or door
    frames = [e for e in ev if e["kind"] == "frame"]
    hist = build_tracks(frames)
    out = {"id": r["trial_id"], "script": r["script_id"], "cls": r["classification"],
           "frames": frames, "hist": hist}
    seen = [f for f in frames if f["targets"]]
    out["fps"] = (len(frames) - 1) / (frames[-1]["t"] - frames[0]["t"]) if len(frames) > 1 else None
    out["n_seen"] = len(seen)
    out["n_multi"] = sum(1 for f in seen if len(f["targets"]) > 1)
    door_ev = first(ev, "door")
    out["door"] = door_ev
    duck = first(ev, "duck", pred=lambda e: e["active"])
    unduck = first(ev, "duck", duck["t"], lambda e: not e["active"]) if duck else None
    out["duck"], out["unduck"] = duck, unduck
    out["door_to_duck"] = duck["t"] - door_ev["t"] if duck and door_ev else None
    out["duck_len"] = unduck["t"] - duck["t"] if duck and unduck else None
    enter = first(ev, "enter")
    sw = first(ev, "modality", pred=lambda e: e["to"] == "web")
    out["enter"], out["switch"] = enter, sw
    if enter and sw:
        take = first(ev, "takeover", sw["t"])
        end = first(ev, "speech_end", sw["t"])
        out["enter_to_switch"] = sw["t"] - enter["t"]
        out["switch_to_takeover"] = take["t"] - sw["t"] if take else None
        out["switch_to_speech_end"] = end["t"] - sw["t"] if end else None
        k = track_at(hist, enter["t"], enter["x"], enter["y"])
        pts = [p for p in hist.get(k, []) if p[0] <= enter["t"] + 0.01]
        out["entry_track"] = k
        out["first_seen"] = pts[0] if pts else None
        out["cross_in"] = crossing(pts, room, inward=True)
        out["cross_to_switch"] = sw["t"] - out["cross_in"] if out["cross_in"] else None
        out["speed_in"] = ground_speed(pts, enter["t"])
        ent_door = first(ev, "door", pred=lambda e: e["id"] == enter["id"])
        out["door_to_enter"] = enter["t"] - ent_door["t"] if ent_door else None
        out["ducked_before_switch"] = sw["t"] - duck["t"] if duck and duck["t"] <= sw["t"] else None
        out["via"] = enter.get("via")
    if r["classification"] == "TP" and sw:
        t_leave_now = sw["t"] + STAY_S
        voice = first(ev, "modality", sw["t"], lambda e: e["to"] == "voice")
        out["t_leave_now"] = t_leave_now
        out["voice"] = voice
        if voice is None or voice.get("reason") == "reset":
            out["reversion"] = "missed"
        elif voice["t"] < t_leave_now:
            out["reversion"] = "premature"
        else:
            out["reversion"] = "correct"
        ph = phantom(hist, out.get("entry_track"), room, sw["t"], t_leave_now - 1.0)
        out["phantom"] = ph
        if voice is not None and out["reversion"] != "missed":
            leave = [e for e in ev if e["kind"] == "leave" and e["t"] <= voice["t"] + 0.01]
            leave = leave[-1] if leave else None
            out["leave"] = leave
            out["exit_track_same"] = bool(leave and leave["id"] == enter["id"])
            if out["reversion"] == "correct":
                found = last_exit_crossing(hist, room, t_leave_now - 1.0, voice["t"])
            else:
                found = (ph["t_out"], out.get("entry_track")) if ph else None
            t_out = found[0] if found else None
            out["cross_out"] = t_out
            out["exit_speed_max"] = speed_near(hist[found[1]], t_out) if found else None
            if found:
                t_to = t_out + 0.5 if out["reversion"] == "correct" else min(ph["end"], t_out + 1.5)
                seg = [p for p in hist[found[1]] if t_out - 0.5 <= p[0] <= t_to]
                out["exit_range_rate"] = range_rate(seg)
                out["exit_doppler"] = quantile([p[3] for p in seg], .5) if seg else None
            others = [p for f in frames if abs(f["t"] - voice["t"]) <= 0.15 for p in f["targets"]
                      if (zone_distance(p["x"], p["y"], room) or 1) <= 0
                      and (zone_distance(p["x"], p["y"], door) or 1) > 0]
            out["someone_inside_at_voice"] = bool(others)
            out["voice_after_leave_now"] = voice["t"] - t_leave_now
            out["exit_to_voice"] = voice["t"] - t_out if t_out is not None else None
            out["leave_to_voice"] = voice["t"] - leave["t"] if leave else None
    cleared = first(ev, "door_cleared", door_ev["t"], lambda e: e["id"] == door_ev["id"]) if door_ev else None
    out["door_outcome"] = cleared["outcome"] if cleared else None
    out["door_dwell"] = cleared["t"] - door_ev["t"] if cleared and door_ev else None
    out["door_depth"] = None
    if door_ev and door:
        k = track_at(hist, door_ev["t"], door_ev["x"], door_ev["y"])
        t_end = cleared["t"] if cleared else door_ev["t"] + 30
        ys = [p[2] for p in hist.get(k, []) if door_ev["t"] - 0.01 <= p[0] <= t_end
              and (zone_distance(p[1], p[2], door) or 1) <= 0]
        out["door_depth"] = door["max_y_mm"] - min(ys) if ys else None
    gaps = [zone_distance(p["x"], p["y"], door) for f in frames for p in f["targets"]] if door else []
    out["closest_to_door"] = min((g for g in gaps if g is not None), default=None)
    start = first(ev, "speech_start")
    out["tts_start"] = start["t"] - d["t_arm"] if start else None
    return out


def section(title):
    print(f"\n{title}\n{'-' * len(title)}")


def main():
    parser = argparse.ArgumentParser(description="Analyse Study 2 privacy-switch data.")
    parser.add_argument("csv", nargs="?", help="session CSV (default: the newest study/privacy_switch_*.csv)")
    parser.add_argument("--settings", help="JSON with the door_zone used (default: <csv>_settings.json)")
    parser.add_argument("--coords", action="store_true", help="also print pgfplots coordinates for the figures")
    parser.add_argument("--track", help="also print the reconstructed tracks of this trial id as pgfplots coordinates")
    args = parser.parse_args()
    study = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "study")
    path = args.csv or max((p for p in glob.glob(os.path.join(study, "privacy_switch_*.csv"))
                            if not p.endswith("_video.csv")), key=os.path.getmtime)
    rows, logs, door = load(path, args.settings)
    res = [analyse_trial(r, logs[r["trial_id"]], door) for r in rows if r["trial_id"] in logs]
    room = next(iter(logs.values()))["zone"]
    door = next((d["door_zone"] for d in logs.values() if d.get("door_zone")), door)
    stamps = sorted(r["timestamp"] for r in rows)

    section(f"{os.path.basename(path)}: {len(res)} trials, {stamps[0]} to {stamps[-1]}")
    print(f"  room zone  x {room['min_x_mm']}..{room['max_x_mm']}  y {room['min_y_mm']}..{room['max_y_mm']} mm")
    if door:
        print(f"  door zone  x {door['min_x_mm']}..{door['max_x_mm']}  y {door['min_y_mm']}..{door['max_y_mm']} mm")
    print("  trials per script: " + ", ".join(f"{s} {SCRIPT_NAMES.get(s, s)} {n}"
                                              for s, n in sorted(Counter(x["script"] for x in res).items())))
    dist("TTS playback start after arming", [x["tts_start"] for x in res])

    section("Switch decisions")
    cls = Counter(x["cls"] for x in res)
    print(f"  TP {cls['TP']}  FN {cls['FN']}  FP {cls['FP']}  TN {cls['TN']}")
    rate("entries detected (sensitivity)", cls["TP"], cls["TP"] + cls["FN"])
    rate("missed entries (FN rate)", cls["FN"], cls["TP"] + cls["FN"])
    rate("non-entries not switched (spec.)", cls["TN"], cls["TN"] + cls["FP"])
    rate("false switches (FP rate)", cls["FP"], cls["TN"] + cls["FP"])
    fn_hi = clopper_pearson(cls["FN"], cls["TP"] + cls["FN"])[1]
    print(f"  FN target <{100 * FN_TARGET:.0f}%: upper bound {100 * fn_hi:.1f}% -> "
          f"{'met' if fn_hi < FN_TARGET else 'not demonstrated'}; zero misses in "
          f"{zero_failure_n(FN_TARGET)} entries would demonstrate it")
    for s in sorted({x["script"] for x in res}):
        xs = [x for x in res if x["script"] == s]
        c = Counter(x["cls"] for x in xs)
        if c["TP"] + c["FN"]:
            rate(f"{s} {SCRIPT_NAMES.get(s, s)}: detected", c["TP"], c["TP"] + c["FN"])
        else:
            rate(f"{s} {SCRIPT_NAMES.get(s, s)}: switched", c["FP"], c["TN"] + c["FP"])

    tp = [x for x in res if x["cls"] == "TP"]
    section("Entry timeline, true positives")
    print("  entries decided via: " + ", ".join(f"{k} {n}" for k, n in Counter(x["via"] for x in tp).items()))
    for s in sorted({x["script"] for x in tp}):
        xs = [x for x in tp if x["script"] == s]
        print(f"  {s} {SCRIPT_NAMES.get(s, s)}")
        dist("ground speed at entry", [x["speed_in"] for x in xs], "m/s", 0.001, 2, "    ")
        dist("first seen -> switch", [x["switch"]["t"] - x["first_seen"][0] for x in xs if x["first_seen"]],
             indent="    ")
        dist("at door -> entry (voice lowered)", [x["door_to_enter"] for x in xs], indent="    ")
        dist("edge crossing -> switch (radar)", [x["cross_to_switch"] for x in xs], indent="    ")
    dist("edge crossing -> switch (radar), all", [x["cross_to_switch"] for x in tp])
    dist("entry event -> switch (software)", [x["enter_to_switch"] for x in tp], "ms", 1000, 2)
    dist("switch -> playback killed", [x["switch_to_takeover"] for x in tp], "ms", 1000, 2)
    dist("switch -> playback loop ended", [x["switch_to_speech_end"] for x in tp], "ms", 1000, 2)
    ys = [x["first_seen"][2] for x in tp if x["first_seen"]]
    inside_door = sum(1 for x in tp if x["first_seen"] and (zone_distance(x["first_seen"][1], x["first_seen"][2], door) or 1) <= 0)
    beyond = sum(1 for y in ys if door and y > door["max_y_mm"])
    print(f"  entrant first seen: beyond the door zone {beyond}, inside it {inside_door}, "
          f"elsewhere {len(ys) - beyond - inside_door}")
    dist("first seen at range y", ys, "m", 0.001, 2)
    print(f"  lowered before the switch: {sum(1 for x in tp if x['ducked_before_switch'] is not None)}/{len(tp)}")
    dist("voice lowered before switch", [x["ducked_before_switch"] for x in tp])

    section("Missed entries")
    for x in [x for x in res if x["cls"] == "FN"]:
        fs = next((f for f in x["frames"] if f["targets"]), None)
        pos = f"{fs['targets'][0]['x']},{fs['targets'][0]['y']} v{fs['targets'][0]['v']}" if fs else "-"
        print(f"  trial {x['id']} {x['script']}: at door {'yes' if x['door'] else 'no'}, ducked "
              f"{'yes' if x['duck'] else 'no'}, door outcome {x['door_outcome']}, first target {pos}")

    section("Return to voice after the visitor left, true positives")
    cats = Counter(x["reversion"] for x in tp)
    for c in ("correct", "premature", "missed"):
        rate(c, cats[c], len(tp))
    ok = [x for x in tp if x["reversion"] == "correct"]
    pre = [x for x in tp if x["reversion"] == "premature"]
    dist("correct: LEAVE NOW -> voice", [x["voice_after_leave_now"] for x in ok])
    dist("correct: exit crossing -> voice", [x["exit_to_voice"] for x in ok])
    dist("correct: exit event -> voice", [x["leave_to_voice"] for x in ok], "ms", 1000, 2)
    dist("correct: max radial speed at exit", [x["exit_speed_max"] for x in ok], "mm/s", 1, 0)
    dist("premature: switch -> voice", [x["voice"]["t"] - x["switch"]["t"] for x in pre])
    dist("premature: voice before LEAVE NOW", [-x["voice_after_leave_now"] for x in pre])
    dist("premature: max radial speed at exit", [x["exit_speed_max"] for x in pre], "mm/s", 1, 0)
    print(f"  premature: exit on the entrant's own track {sum(1 for x in pre if x['exit_track_same'])}/{len(pre)}, "
          f"someone still tracked inside at the switch back {sum(1 for x in pre if x['someone_inside_at_voice'])}"
          f"/{len(pre)}")
    for label, xs in (("correct", ok), ("premature", pre)):
        dist(f"{label}: range rate from positions", [x.get("exit_range_rate") for x in xs], "mm/s", 1, 0)
        dist(f"{label}: reported radial speed", [x.get("exit_doppler") for x in xs], "mm/s", 1, 0)
    sep_ok = sum(1 for x in ok if (x["exit_speed_max"] or 0) >= EXIT_SPEED_MMS)
    sep_pre = sum(1 for x in pre if (x["exit_speed_max"] or 0) < EXIT_SPEED_MMS)
    print(f"  exit radial speed >= {EXIT_SPEED_MMS:.0f} mm/s: correct {sep_ok}/{len(ok)}, "
          f"premature below it {sep_pre}/{len(pre)}")
    ph = [x for x in tp if x.get("phantom")]
    print(f"  entrant's track back across the room edge before LEAVE NOW: {len(ph)}/{len(tp)} "
          f"(premature {sum(1 for x in ph if x['reversion'] == 'premature')}, "
          f"correct {sum(1 for x in ph if x['reversion'] == 'correct')}, "
          f"missed {sum(1 for x in ph if x['reversion'] == 'missed')})")
    dist("  ... after the switch", [x["phantom"]["after_switch"] for x in ph])
    dist("  ... its median |radial speed|", [x["phantom"]["v_abs_median"] for x in ph], "mm/s", 1, 0)
    dist("  ... displacement while outside", [x["phantom"]["moved_mm"] for x in ph], "m", 0.001, 2)
    for x in pre + [x for x in tp if x["reversion"] == "missed"]:
        extra = (f"voice {x['voice']['t'] - x['switch']['t']:.1f} s after the switch, max exit speed "
                 f"{x['exit_speed_max']} mm/s, inside at switch back {x['someone_inside_at_voice']}"
                 ) if x["reversion"] == "premature" else "no return to voice"
        print(f"    trial {x['id']} {x['script']} {x['reversion']}: {extra}")

    section("Quieter voice at the door")
    for s, label in (("D", "peeks"), ("B", "passers-by")):
        xs = [x for x in res if x["script"] == s]
        if not xs:
            continue
        rate(f"{label}: lowered", sum(1 for x in xs if x["duck"]), len(xs))
        rate(f"{label}: switched", sum(1 for x in xs if x["switch"]), len(xs))
        dist(f"{label}: lowered for", [x["duck_len"] for x in xs])
        dist(f"{label}: at the door for", [x["door_dwell"] for x in xs])
        dist(f"{label}: seen at the door at y", [x["door"]["y"] for x in xs if x["door"]], "m", 0.001, 2)
        dist(f"{label}: ... and x", [x["door"]["x"] for x in xs if x["door"]], "m", 0.001, 2)
        dist(f"{label}: deepest point past outer edge", [x["door_depth"] for x in xs], "m", 0.001, 2)
        rest = [x for x in xs if not x["door"]]
        if rest:
            print(f"  {label} not seen at the door: {len(rest)}, of which never seen at all "
                  f"{sum(1 for x in rest if not x['n_seen'])}")
            dist(f"{label}: ... closest approach to it", [x["closest_to_door"] for x in rest], "m", 0.001, 2)
    dist("door event -> gain change (software)", [x["door_to_duck"] for x in res], "ms", 1000, 2)

    section("Radar data")
    dist("frame rate", [x["fps"] for x in res], "Hz", 1, 1)
    seen = sum(x["n_seen"] for x in res)
    multi = sum(x["n_multi"] for x in res)
    print(f"  frames with a target: {seen}; with two or more targets: {multi} ({100 * multi / seen:.1f}%)")
    print(f"  trials with at least one multi-target frame: {sum(1 for x in res if x['n_multi'])}/{len(res)}")

    if args.coords:
        section("pgfplots coordinates: depth past the door zone's outer edge (m)")
        rows_out = (("pass, voice unchanged", [-x["closest_to_door"] for x in res
                                                if x["script"] == "B" and not x["door"]]),
                    ("pass, voice lowered", [x["door_depth"] for x in res if x["script"] == "B" and x["door"]]),
                    ("peek", [x["door_depth"] for x in res if x["script"] == "D" and x["door_depth"] is not None]))
        for row, (label, vals) in enumerate(rows_out, start=1):
            print(f"  % {label} ({len(vals)})")
            print("  " + " ".join(f"({v / 1000:.3f},{row})" for v in vals))
    if args.track:
        x = next((x for x in res if x["id"] == args.track), None)
        if x is None:
            print(f"\nNo trial {args.track}")
            return
        t0 = logs[args.track]["t_arm"]
        section(f"pgfplots coordinates: tracks of trial {args.track} (x, y in m)")
        for k, pts in x["hist"].items():
            if len(pts) >= 5:
                print(f"  % track {k}: {pts[0][0] - t0:.1f} to {pts[-1][0] - t0:.1f} s")
                print("  " + " ".join(f"({p[1] / 1000:.2f},{p[2] / 1000:.2f})" for p in pts))
    print()


if __name__ == "__main__":
    main()
