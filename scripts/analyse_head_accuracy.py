import argparse
import csv
import glob
import json
import math
import os
import re
from collections import defaultdict

FACTORS = ["angle_true", "distance_m", "noise_condition"]
NOISE_ORDER = ["none", "ambient", "offaxis"]


def level_sort_key(label):
    try:
        return (0, float(label), "")
    except ValueError:
        pass
    if label in NOISE_ORDER:
        return (1, NOISE_ORDER.index(label), "")
    return (2, 0.0, label)


def chi2_sf(x, k):
    if x <= 0:
        return 1.0
    a = k / 2.0
    x2 = x / 2.0
    if x2 < a + 1.0:
        term = 1.0 / a
        total = term
        n = a
        for _ in range(500):
            n += 1.0
            term *= x2 / n
            total += term
            if abs(term) < abs(total) * 1e-14:
                break
        return 1.0 - total * math.exp(-x2 + a * math.log(x2) - math.lgamma(a))
    tiny = 1e-300
    b = x2 + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h * math.exp(-x2 + a * math.log(x2) - math.lgamma(a))


def rank_with_ties(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    tie_groups = []
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        if j > i:
            tie_groups.append(j - i + 1)
        i = j + 1
    return ranks, tie_groups


def kruskal_wallis(groups):
    groups = [g for g in groups if len(g) > 0]
    k = len(groups)
    if k < 2:
        return None
    flat = [v for g in groups for v in g]
    n = len(flat)
    if n < k + 1:
        return None
    ranks, tie_groups = rank_with_ties(flat)
    h = 0.0
    idx = 0
    for g in groups:
        rsum = sum(ranks[idx:idx + len(g)])
        h += rsum * rsum / len(g)
        idx += len(g)
    h = 12.0 / (n * (n + 1)) * h - 3.0 * (n + 1)
    correction = 1.0 - sum(t ** 3 - t for t in tie_groups) / float(n ** 3 - n) if n > 1 else 1.0
    if correction > 0:
        h /= correction
    df = k - 1
    eps2 = (h - k + 1) / (n - k) if n > k else float("nan")
    return {"H": h, "df": df, "p": chi2_sf(h, df), "n": n, "k": k, "eps2": eps2}


def median(vals):
    if not vals:
        return float("nan")
    s = sorted(vals)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def quantile(vals, q):
    if not vals:
        return float("nan")
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def mean(vals):
    return sum(vals) / len(vals) if vals else float("nan")


def rmse(vals):
    return math.sqrt(sum(v * v for v in vals) / len(vals)) if vals else float("nan")


def sd(vals):
    if len(vals) < 2:
        return float("nan")
    m = mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def fnum(row, key):
    raw = (row.get(key) or "").strip()
    if not raw:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def load(paths):
    rows = []
    for path in paths:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                row["_source"] = os.path.basename(path)
                rows.append(row)
    return rows


def enrich(rows):
    out = []
    for row in rows:
        angle_true = fnum(row, "angle_true")
        if angle_true is None:
            continue
        rec = dict(row)
        rec["angle_true"] = angle_true
        rec["distance_m"] = fnum(row, "distance_m")
        rec["miss"] = (row.get("miss") or "0").strip() in ("1", "true", "True")
        rec["simulated"] = (row.get("simulated") or "0").strip() in ("1", "true", "True")
        rec["excluded"] = (row.get("excluded") or "0").strip() in ("1", "true", "True")
        rec["exclude_reason"] = (row.get("exclude_reason") or "").strip()
        rec["cfg"] = tuple((row.get(k) or "").strip() for k in
                           ("cfg_onset_skip_s", "cfg_sample_interval_s",
                            "cfg_min_samples", "cfg_post_move_quiet_s"))
        rec["settle_time_s"] = fnum(row, "settle_time_s")
        rec["settle_from_voice_s"] = fnum(row, "settle_from_voice_s")
        rec["playback_mode"] = (row.get("playback_mode") or "").strip()

        implied = fnum(row, "doa_implied_bearing")
        target = fnum(row, "servo_target_angle")
        physical = fnum(row, "physical_angle_measured")

        rec["doa_error"] = None if implied is None else implied - angle_true
        rec["mech_error"] = None if (physical is None or target is None) else physical - target
        rec["map_error"] = None if (target is None or implied is None) else target - implied
        if physical is not None:
            rec["total_error"] = physical - angle_true
            rec["total_source"] = "measured"
        elif target is not None:
            rec["total_error"] = target - angle_true
            rec["total_source"] = "commanded"
        else:
            rec["total_error"] = None
            rec["total_source"] = "none"
        out.append(rec)
    return out


def describe(errors, label, indent=""):
    if not errors:
        print(f"{indent}{label:22} n=0")
        return
    absv = [abs(e) for e in errors]
    print(f"{indent}{label:22} n={len(errors):<4} "
          f"bias={mean(errors):+6.2f}  MAE={mean(absv):5.2f}  "
          f"median|e|={median(absv):5.2f}  IQR=[{quantile(absv, .25):.2f},{quantile(absv, .75):.2f}]  "
          f"RMSE={rmse(errors):5.2f}  max={max(absv):5.2f}")


def linear_fit(points):
    n = len(points)
    if n < 2:
        return None
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in points) / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for _, y in points)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in points)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return {"a": a, "b": b, "r2": r2, "n": n}


def report_gain(analysed):
    """Fits reported bearing against true bearing.

    A slope below 1 means the array under-reports how far off-axis a source
    is, pulling every estimate toward the front.
    """
    def bearing(r):
        for key in ("doa_implied_bearing", "servo_target_angle"):
            v = fnum(r, key)
            if v is not None:
                return v
        return None

    print("\n" + "-" * 78)
    print("OFF-AXIS GAIN (reported bearing vs true bearing)")
    print("-" * 78)
    print("slope < 1 = the array pulls sources toward the front (angular compression)")
    print(f"\n{'distance':>9} {'noise':>9} {'n':>4} {'slope k':>9} {'1/k':>7} "
          f"{'intercept':>10} {'R2':>8}   per-angle means")

    cells = defaultdict(list)
    for r in analysed:
        b = bearing(r)
        if b is not None:
            cells[(r["distance_m"], r["noise_condition"])].append((r["angle_true"], b))

    for key in sorted(cells):
        pts = cells[key]
        fit = linear_fit(pts)
        by_angle = defaultdict(list)
        for t, b in pts:
            by_angle[t].append(b)
        summary = "  ".join(f"{a:g}->{mean(v):.1f}" for a, v in sorted(by_angle.items()))
        n_angles = len({t for t, _ in pts})
        thin_fit = "  <-- only %d angles" % n_angles if n_angles < 3 else ""
        if fit is None or n_angles < 2:
            print(f"{key[0]:>9g} {key[1]:>9} {len(pts):>4} {'-':>9} {'-':>7} "
                  f"{'-':>10} {'-':>8}   {summary}")
            continue
        print(f"{key[0]:>9g} {key[1]:>9} {fit['n']:>4} {fit['b']:>9.3f} "
              f"{1.0 / fit['b'] if fit['b'] else float('nan'):>7.3f} "
              f"{fit['a']:>10.2f} {fit['r2']:>8.4f}   {summary}{thin_fit}")

    quiet = [p for (d, n), pts in cells.items() if n == "none" for p in pts]
    if quiet and len({t for t, _ in quiet}) >= 2:
        fit = linear_fit(quiet)
        print(f"\nPooled over all quiet trials: k = {fit['b']:.3f} "
              f"(correction 1/k = {1.0 / fit['b']:.3f}), R2 = {fit['r2']:.4f}, n = {fit['n']}")
        print("Compare k across distances above - if it varies, a single correction")
        print("constant is wrong and the model needs to be distance-dependent.")


def median_angle_as_deployed(angles):
    base = angles[0]
    return median([base + ((a - base + 180.0) % 360.0 - 180.0) for a in angles])


def servo_heading(heading, target):
    return min(180.0, max(0.0, heading + target - 90.0))


def load_trial_details(paths, analysed):
    wanted = {(r.get("session", ""), str(r.get("trial_id", ""))): r for r in analysed}
    details = []
    for path in paths:
        jpath = os.path.splitext(path)[0] + "_trials.jsonl"
        if not os.path.exists(jpath):
            print(f"  {os.path.basename(jpath)} not found - {os.path.basename(path)} skipped")
            continue
        found = {}
        with open(jpath) as f:
            for line in f:
                if line.strip():
                    d = json.loads(line)
                    key = (d.get("session", ""), str(d.get("trial_id")))
                    if key in wanted:
                        found[key] = d
        print(f"  {os.path.basename(jpath)}: {len(found)} trial record(s) matched")
        details.extend((wanted[key], d) for key, d in found.items())
    return details


def rebuild_moves(d):
    skip = float(d["config"].get("onset_skip_s") or 0)
    k = int(d["config"].get("doa_samples") or 5)
    readings = d["doa_samples"]
    accounted = set()
    prev = float("-inf")
    moves = []
    for m in (m for m in d["moves"] if m["kind"] == "track"):
        group = [i for i, s in enumerate(readings) if prev < s["t"] <= m["t"]]
        chosen = (group[1:] if skip > 0 else group)[-k:]
        accounted.update(chosen)
        if skip > 0 and group:
            accounted.add(group[0])
        moves.append((m, [readings[i]["doa_angle"] for i in chosen]))
        prev = m["t"]
    runs, run = [], []
    for i, s in enumerate(readings):
        if i in accounted:
            if run:
                runs.append(run)
            run = []
        else:
            run.append(s["doa_angle"])
    if run:
        runs.append(run)
    return moves, runs


def motion_times(d):
    readings = d["doa_samples"]
    track = [m for m in d["moves"] if m["kind"] == "track"]
    out = {"first": None, "at_rest": None, "moved_later": False, "decisions": len(track)}
    if not readings or not track:
        return out
    t0 = readings[0]["t"]
    out["first"] = track[0]["t"] - t0
    last_angle = readings[0]["head_heading"]
    for p in d["pwm_updates"]:
        if abs(p["angle"] - last_angle) > 0.005:
            out["at_rest"] = p["t"] - t0
            if p["t"] > track[0]["t"]:
                out["moved_later"] = True
        last_angle = p["angle"]
    return out


def table_from_config(d):
    m = re.search(r"\+table:(.+)\((\d+) points\)$", str(d["config"].get("offaxis_gain", "")))
    if not m:
        return None
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), m.group(1))
    by_true = defaultdict(list)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            t, r = fnum(row, "true_angle"), fnum(row, "reported_angle")
            if t is not None and r is not None:
                by_true[t].append(r)
    table = sorted((median(v), t) for t, v in by_true.items())
    if len(table) != int(m.group(2)):
        raise SystemExit(f"{path} now has {len(table)} points, the run loaded {m.group(2)} - not the same table")
    return table


def uncorrect(value, table):
    pts = [(t, r) for r, t in table]
    if value <= pts[0][0]:
        (t1, r1), (t2, r2) = pts[0], pts[1]
    elif value >= pts[-1][0]:
        (t1, r1), (t2, r2) = pts[-2], pts[-1]
    else:
        (t1, r1), (t2, r2) = next((a, b) for a, b in zip(pts, pts[1:]) if a[0] <= value <= b[0])
    return r1 if t2 == t1 else r1 + (r2 - r1) * (value - t1) / (t2 - t1)


def print_tables(details):
    seen = set()
    for _, d in details:
        m = re.search(r"\+table:(.+)\((\d+) points\)$", str(d["config"].get("offaxis_gain", "")))
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        table_from_config(d)
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), m.group(1))
        by_true = defaultdict(list)
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                t, r = fnum(row, "true_angle"), fnum(row, "reported_angle")
                if t is not None and r is not None:
                    by_true[t].append((r, fnum(row, "distance_m")))
        print(f"\nCalibration table in use: {m.group(1)} ({m.group(2)} points)")
        print(f"  {'true':>6} {'reps':>5} {'median reported':>16} {'reported per rep':>20}  distance")
        for t in sorted(by_true):
            reps = by_true[t]
            print(f"  {t:>6g} {len(reps):>5} {median([r for r, _ in reps]):>16g} "
                  f"{' '.join(f'{r:g}' for r, _ in reps):>20}  {sorted({dist for _, dist in reps})}")


def report_implied_bearings(details):
    print("\nWhere the last estimate puts the source, from uncorrected readings")
    print("  implied = heading before the last move + (uncorrected estimate - 90). The last estimate is taken")
    print("  with the head already turned, so the source is close to its axis. For a run with a table the")
    print("  readings are un-mapped through the same table; they should come out as whole degrees.")
    by_cell = defaultdict(list)
    for row, d in details:
        by_cell[(row["_source"], row["angle_true"], row["distance_m"], row["noise_condition"])].append((row, d))
    for key in sorted(by_cell):
        items = by_cell[key]
        source = f"{key[0]} {key[1]:g} deg {key[2]:g} m {key[3]}"
        implied = {"first": [], "last": []}
        parts = {"first": [], "last": []}
        off_whole = 0.0
        for row, d in sorted(items, key=lambda rd: int(rd[1]["trial_id"])):
            moves, _ = rebuild_moves(d)
            if not moves:
                continue
            table = table_from_config(d)
            for which, (m, used) in (("first", moves[0]), ("last", moves[-1])):
                raw = [uncorrect(v, table) for v in used] if table else used
                off_whole = max([off_whole] + [abs(v - round(v)) for v in raw])
                bearing = m["heading_before"] + median_angle_as_deployed(raw) - 90.0
                implied[which].append(bearing)
                parts[which].append(f"{d['trial_id']}: {bearing:g}")
        print(f"  {source}  (largest distance of an uncorrected reading from a whole degree {off_whole:.3f})")
        for which, label in (("first", "first estimate, head at home"), ("last", "last estimate")):
            vals = implied[which]
            print(f"    {label:29} median {median(vals):.2f} [{quantile(vals, .25):.2f}, {quantile(vals, .75):.2f}]"
                  f"   per trial {', '.join(parts[which])}")


def print_moves(details):
    print("\nPer trial: seconds from the array's first voice report; each move is logged when the head")
    print("has reached it. Errors are commanded heading minus true bearing.")
    for row, d in sorted(details, key=lambda rd: (rd[0]["_source"], rd[0]["angle_true"], int(rd[1]["trial_id"]))):
        times = motion_times(d)
        moves, _ = rebuild_moves(d)
        if not moves:
            continue
        t0 = d["doa_samples"][0]["t"]
        true = row["angle_true"]
        first, final = moves[0][0]["resulting_heading"], moves[-1][0]["resulting_heading"]
        rest = "-" if times["at_rest"] is None else f"{times['at_rest']:.3f}"
        print(f"  {row['_source']} trial {d['trial_id']:>3} true {true:g}: first {first:g} ({first - true:+g}), "
              f"final {final:g} ({final - true:+g}), {times['decisions']} decision(s), "
              f"first move {times['first']:.3f}, at rest {rest}, settle {row.get('settle_from_voice_s')}")
        for m, used in moves:
            print(f"      {m['t'] - t0:6.3f}  readings {' '.join(f'{v:g}' for v in used):<28} "
                  f"estimate {m['target']:g}  heading {m['heading_before']:g} -> {m['resulting_heading']:g}")


def print_trajectories(details, until_s):
    print(f"\nHeading error over time (pgfplots coordinates): commanded heading minus true bearing at every")
    print(f"PWM update, from the array's first voice report, held at its last value until {until_s:g} s.")
    for row, d in sorted(details, key=lambda rd: (rd[0]["_source"], rd[0]["angle_true"], int(rd[1]["trial_id"]))):
        if not d["doa_samples"]:
            continue
        t0 = d["doa_samples"][0]["t"]
        true = row["angle_true"]
        heading = d["doa_samples"][0]["head_heading"]
        points = [(0.0, heading - true)]
        for p in d["pwm_updates"]:
            t = p["t"] - t0
            if t > until_s:
                break
            if abs(p["angle"] - heading) > 0.005:
                points.append((t, heading - true))
                points.append((t, p["angle"] - true))
                heading = p["angle"]
        points.append((until_s, heading - true))
        coords = " ".join(f"({t:.3f},{e:.2f})" for t, e in points)
        print(f"% {row['_source']} trial {d['trial_id']} true {true:g}")
        print(f"coordinates {{{coords}}};")


def fmt_med_iqr(vals):
    if not vals:
        return "-"
    return f"{median(vals):.3f} [{quantile(vals, .25):.3f}, {quantile(vals, .75):.3f}]"


def report_trials(paths, analysed, show_moves=False, trajectories_until_s=None):
    print("\n" + "-" * 78)
    print("TRIAL RECORDS (rebuilt from the _trials.jsonl readings)")
    print("-" * 78)
    details = load_trial_details(paths, analysed)
    if not details:
        return

    print("\nEstimator: each head move rebuilt from the readings that fed it")
    print("(onset read dropped when the onset skip is on, then the last doa_samples readings).")
    by_file = defaultdict(list)
    for row, d in details:
        by_file[row["_source"]].append((row, d))
    for source, items in by_file.items():
        n_moves = reproduced = 0
        differs, not_rebuilt, wide_runs = [], [], []
        for row, d in items:
            moves, runs = rebuild_moves(d)
            first_diff = True
            for idx, (m, used) in enumerate(moves, 1):
                n_moves += 1
                if not used or abs(median_angle_as_deployed(used) - m["target"]) > 0.02:
                    not_rebuilt.append((d["trial_id"], idx, m["target"], used))
                    continue
                reproduced += 1
                plain = median(used)
                if abs(plain - m["target"]) > 0.02:
                    differs.append((row, d, idx, m, used, plain, first_diff))
                    first_diff = False
            for run in runs:
                if len(run) > 1 and max(run) - min(run) >= 180.0:
                    wide_runs.append((d["trial_id"], run))
        print(f"\n  {source}: {reproduced}/{n_moves} moves reproduce the recorded target; "
              f"plain median differs on {len(differs)}")
        for tid, idx, target, used in not_rebuilt:
            print(f"    NOT REBUILT trial {tid} move {idx}: target {target}, readings {used}")
        for row, d, idx, m, used, plain, first in differs:
            before = m["heading_before"]
            print(f"    trial {d['trial_id']} (true {row['angle_true']:g}) move {idx}"
                  f"{'' if first else ' (after a differing move - not a valid counterfactual)'}: "
                  f"readings {' '.join(f'{v:g}' for v in used)}")
            print(f"      deployed {m['target']:g} -> heading {servo_heading(before, m['target']):g}   "
                  f"plain {plain:g} -> heading {servo_heading(before, plain):g}   (head was at {before:g})")
        unused = sum(len(r) for r in [run for _, d in items for run in rebuild_moves(d)[1]])
        print(f"    readings that fed no move: {unused}; runs spanning 180 deg or more "
              f"(where the estimators could disagree): {len(wide_runs)}")
        for tid, run in wide_runs:
            print(f"      trial {tid}: {' '.join(f'{v:g}' for v in run)}")

    print("\nLatency split, seconds from the array's first voice report")
    print("  decisions = track moves issued (incl. ones that left the head where it was)")
    print("  first     = first move complete (logged once the head reached it)")
    print("  first|e|  = median absolute error of the heading after that first move, degrees")
    print("  moved     = trials in which the head moved at all; later = moved again after the first move")
    print("  at rest   = last PWM update that changed the angle (trials where the head moved)")
    print("  settle    = settle_from_voice_s as recorded (last move or PWM event of any kind)")
    print(f"\n{'angle':>6} {'dist':>5} {'noise':>8} {'n':>3} {'decisions':>9}  "
          f"{'first median [IQR]':>24} {'first|e|':>8}  {'moved':>5} {'later':>5} "
          f"{'at rest median [IQR]':>24}  {'settle median [IQR]':>24}")
    cells = defaultdict(list)
    for row, d in details:
        cells[(row["angle_true"], row["distance_m"], row["noise_condition"])].append((row, d))
    for key in sorted(cells):
        firsts, first_errs, rests, settles, decisions, later = [], [], [], [], [], 0
        for row, d in cells[key]:
            times = motion_times(d)
            track = [m for m in d["moves"] if m["kind"] == "track"]
            if times["first"] is not None:
                firsts.append(times["first"])
                first_errs.append(abs(track[0]["resulting_heading"] - row["angle_true"]))
            if times["at_rest"] is not None:
                rests.append(times["at_rest"])
            later += times["moved_later"]
            if row.get("settle_from_voice_s") is not None:
                settles.append(row["settle_from_voice_s"])
            decisions.append(times["decisions"])
        print(f"{key[0]:>6g} {key[1]:>5g} {key[2]:>8} {len(cells[key]):>3} {median(decisions):>9g}  "
              f"{fmt_med_iqr(firsts):>24} {median(first_errs):>8.2f}  {len(rests):>5} {later:>5} "
              f"{fmt_med_iqr(rests):>24}  {fmt_med_iqr(settles):>24}")

    print_sampling_split(details)
    print_tables(details)
    report_implied_bearings(details)
    if show_moves:
        print_moves(details)
    if trajectories_until_s:
        print_trajectories(details, trajectories_until_s)


def print_sampling_split(details):
    """Where the latency goes per noise condition (figure s1-latency in the thesis):
    median time of each bearing sample that fed the first move, gap between samples,
    and the servo ramp from its first PWM update to the head's settle time. The
    "typical trial" is the one closest to its condition's medians of settle time,
    servo ramp and sampling phase; the thesis figure draws those two trials."""
    print("\nSampling split, seconds from the array's first voice report (first move only)")
    print("  sample k = median time of the k-th reading after the onset skip that fed the first move")
    print("  gap      = time between consecutive samples; a poll without voice yields none")
    print("  servo    = settle_from_voice_s minus the first PWM update")
    by_noise = defaultdict(lambda: {"k": defaultdict(list), "gap": [], "servo": [], "settle": [],
                                    "trials": []})
    for row, d in details:
        track = [m for m in d["moves"] if m["kind"] == "track"]
        readings = d["doa_samples"]
        if not track or not readings or row.get("settle_from_voice_s") is None:
            continue
        t0 = readings[0]["t"]
        k = int(d["config"].get("doa_samples") or 5)
        fed = [s["t"] - t0 for s in readings[1:] if s["t"] <= track[0]["t"]][-k:]
        cell = by_noise[row["noise_condition"]]
        for i, t in enumerate(fed):
            cell["k"][i].append(t)
        cell["gap"].extend(b - a for a, b in zip(fed, fed[1:]))
        if d["pwm_updates"]:
            cell["servo"].append(row["settle_from_voice_s"] - (d["pwm_updates"][0]["t"] - t0))
        cell["settle"].append(row["settle_from_voice_s"])
        cell["trials"].append((d["trial_id"], fed, row["settle_from_voice_s"]))
    for noise in sorted(by_noise):
        cell = by_noise[noise]
        samples = " ".join(f"{median(cell['k'][i]):.3f}" for i in sorted(cell["k"]))
        print(f"  {noise:>8} n={len(cell['settle']):<3} samples {samples}  gap {median(cell['gap']):.3f}  "
              f"servo {fmt_med_iqr(cell['servo'])}  settle {fmt_med_iqr(cell['settle'])}")
        skip = 1.5
        med = (median(cell["settle"]), median([st - f[-1] for _, f, st in cell["trials"]]),
               median([f[-1] - skip for _, f, _ in cell["trials"]]))
        tid, fed, settle = min(cell["trials"], key=lambda t: abs(t[2] - med[0])
                               + abs(t[2] - t[1][-1] - med[1]) + abs(t[1][-1] - skip - med[2]))
        print(f"  {'':>8} typical trial {tid}: samples {' '.join(f'{t:.3f}' for t in fed)}  "
              f"settle {settle:.3f}")


def factor_label(rec, factor):
    if factor == "noise_condition":
        return rec.get("noise_condition", "?")
    val = rec.get(factor)
    return f"{val:g}" if isinstance(val, float) else str(val)


def main():
    parser = argparse.ArgumentParser(description="Analyse Study 1 head-orientation accuracy data.")
    parser.add_argument("csv", nargs="*", help="session CSVs (default: study/head_accuracy_*.csv)")
    parser.add_argument("--include-misses", action="store_true",
                        help="keep trials where the head never moved in the error statistics")
    parser.add_argument("--include-simulated", action="store_true",
                        help="keep rehearsal trials recorded with --simulate")
    parser.add_argument("--plots", action="store_true", help="write PNG plots next to the CSVs")
    parser.add_argument("--trials", action="store_true",
                        help="read the matching _trials.jsonl: rebuild each head move, compare the deployed "
                             "circular median with a plain median, and split latency")
    parser.add_argument("--moves", action="store_true",
                        help="with --trials, also list every trial's moves and the readings behind them")
    parser.add_argument("--pgf-until", type=float, metavar="S",
                        help="with --trials, print each trial's heading error over time as pgfplots "
                             "coordinates up to S seconds after the first voice report")
    parser.add_argument("--only", nargs="+", metavar="ANGLE:DIST:NOISE",
                        help="restrict the analysis to these cells, e.g. 45:1.25:none")
    parser.add_argument("--drop", nargs="+", metavar="SESSION:TRIAL",
                        help="leave these trials out, for a sensitivity check, e.g. production:11")
    args = parser.parse_args()

    study_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "study")
    paths = args.csv or sorted(
        p for p in glob.glob(os.path.join(study_dir, "head_accuracy_*.csv"))
        if not p.endswith("_trials.csv"))
    if not paths:
        print("No CSV files found. Pass paths explicitly or run the study first.")
        return

    rows = enrich(load(paths))
    if args.only:
        cells = set()
        for spec in args.only:
            angle, dist, noise = spec.split(":")
            cells.add((float(angle), float(dist), noise))
        rows = [r for r in rows if (r["angle_true"], r["distance_m"], r["noise_condition"]) in cells]
        print(f"Restricted to {', '.join(args.only)}")
    if args.drop:
        drop = {tuple(spec.split(":", 1)) for spec in args.drop}
        before = len(rows)
        rows = [r for r in rows if (r.get("session", ""), str(r.get("trial_id", ""))) not in drop]
        print(f"Sensitivity check: left out {before - len(rows)} trial(s) ({', '.join(args.drop)})")
    if not rows:
        print("No usable trials found.")
        return

    dropped = [r for r in rows if r["excluded"]]
    if dropped:
        rows = [r for r in rows if not r["excluded"]]
        reasons = {}
        for r in dropped:
            reasons.setdefault(r["exclude_reason"] or "(no reason given)", []).append(r)
        print(f"Excluded {len(dropped)} trial(s) marked by the experimenter:")
        for reason, rs in reasons.items():
            ids = ", ".join(str(r.get("trial_id", "?")) for r in rs)
            print(f"  {len(rs)} trial(s) - {reason}")
            print(f"      ids: {ids}")
        print("  State this exclusion and its reason in the write-up.\n")

    simulated = [r for r in rows if r["simulated"]]
    if simulated and not args.include_simulated:
        rows = [r for r in rows if not r["simulated"]]
        print(f"!! Dropped {len(simulated)} SIMULATED trial(s) - rehearsal data, not real "
              "measurements.\n   Delete those rows, or pass --include-simulated to keep them.\n")
    if not rows:
        print("Nothing left after dropping simulated trials.")
        return

    misses = [r for r in rows if r["miss"]]
    analysed = rows if args.include_misses else [r for r in rows if not r["miss"]]
    measured = [r for r in analysed if r["total_source"] == "measured"]

    print("=" * 78)
    print("Study 1 - Head-Orientation Accuracy")
    print("=" * 78)
    print(f"Files          : {', '.join(os.path.basename(p) for p in paths)}")
    print(f"Trials loaded  : {len(rows)}")
    print(f"Misses         : {len(misses)} ({100.0 * len(misses) / len(rows):.1f}%)"
          f"{' - included' if args.include_misses else ' - excluded from error stats'}")
    if not measured:
        print("Measurement    : COMMANDED ANGLES ONLY - no protractor readings.")
        print("                 'total' below is servo_target - angle_true, i.e. perception +")
        print("                 mapping. It assumes the servo executes its command exactly, so")
        print("                 it CANNOT detect a scaling or home-offset fault. State this as a")
        print("                 limitation, or characterise the servo once and cite the offset.")
    elif len(measured) < len(analysed):
        print(f"Protractor     : {len(measured)}/{len(analysed)} trials have a physical measurement")
        print("                 the rest fall back to the commanded servo angle, which cannot")
        print("                 show mechanical error - report this limitation.")
    else:
        print(f"Protractor     : all {len(measured)} trials physically measured")

    cfgs = {}
    for r in analysed:
        cfgs.setdefault(r["cfg"], []).append(r)
    if len(cfgs) > 1:
        print("\n" + "!" * 78)
        print("WARNING: trials were collected under DIFFERENT tracker settings.")
        print("Angle/distance/noise effects are confounded with these changes -")
        print("a difference between conditions may just be a difference in setup.")
        for cfg, rs in sorted(cfgs.items(), key=lambda kv: -len(kv[1])):
            label = ("onset_skip={0}s interval={1}s min_samples={2} post_move_quiet={3}s"
                     .format(*(c or "?" for c in cfg)))
            angles = sorted({r["angle_true"] for r in rs})
            print(f"  {len(rs):>3} trials  {label}")
            print(f"       angles: {', '.join(f'{a:g}' for a in angles)}")
        print("Either re-run the odd group, or report them as separate datasets.")
        print("!" * 78)

    print("\n" + "-" * 78)
    print("ERROR DECOMPOSITION (degrees, signed bias / unsigned magnitudes)")
    print("-" * 78)
    total_label = "total (cmd vs true)" if not measured else "total (head vs true)"
    describe([r["total_error"] for r in analysed if r["total_error"] is not None],
             total_label)
    describe([r["doa_error"] for r in analysed if r["doa_error"] is not None],
             "  perception (DoA)")
    describe([r["map_error"] for r in analysed if r["map_error"] is not None],
             "  mapping (cmd-DoA)")
    describe([r["mech_error"] for r in analysed if r["mech_error"] is not None],
             "  actuation (mech)")

    for key, label in (("settle_time_s", "Settle from onset"),
                       ("settle_from_voice_s", "Settle from 1st voice")):
        vals = [r[key] for r in analysed if r.get(key) is not None]
        if vals:
            print(f"\n{label:22} n={len(vals):<4} median={median(vals):.3f}s  "
                  f"IQR=[{quantile(vals, .25):.3f},{quantile(vals, .75):.3f}]  "
                  f"mean={mean(vals):.3f}s  max={max(vals):.3f}s")

    modes = {r["playback_mode"] for r in analysed if r.get("playback_mode")}
    if "manual" in modes:
        print("\nNOTE: some trials used manual playback timing, so 'settle from onset'")
        print("      carries your reaction time. Report 'settle from 1st voice' instead,")
        print("      or re-run those conditions with --play.")

    report_gain(analysed)

    print("\n" + "-" * 78)
    print("BY CONDITION")
    print("-" * 78)
    print(f"{'angle':>6} {'dist':>6} {'noise':>9} {'n':>5} {'bias':>7} {'MAE':>6} "
          f"{'med|e|':>7} {'SD':>6} {'miss':>6} {'lat_s':>7}")
    print("n = trials contributing to the error stats (misses excluded)")
    print("lat_s = median settle_from_voice_s, machine-timed on the Pi")
    cells = defaultdict(list)
    for r in rows:
        cells[(r["angle_true"], r["distance_m"], r["noise_condition"])].append(r)
    for key in sorted(cells, key=lambda k: (k[0], k[1], k[2])):
        group = cells[key]
        used = group if args.include_misses else [g for g in group if not g["miss"]]
        errs = [g["total_error"] for g in used if g["total_error"] is not None]
        st = [g["settle_from_voice_s"] for g in used
              if g.get("settle_from_voice_s") is not None]
        nmiss = sum(1 for g in group if g["miss"])
        thin = " <-- too few" if len(errs) < 3 else ""
        print(f"{key[0]:>6g} {key[1]:>6g} {key[2]:>9} {len(errs):>5} "
              f"{mean(errs) if errs else float('nan'):>+7.2f} "
              f"{mean([abs(e) for e in errs]) if errs else float('nan'):>6.2f} "
              f"{median([abs(e) for e in errs]) if errs else float('nan'):>7.2f} "
              f"{sd(errs):>6.2f} "
              f"{nmiss:>2}/{len(group):<3} "
              f"{median(st) if st else float('nan'):>7.3f}{thin}")

    for scope, subset in (("ALL ANGLES", analysed),
                          ("OFF-AXIS ONLY (45 and 135)",
                           [r for r in analysed if r["angle_true"] != 90.0])):
        print("\n" + "-" * 78)
        print(f"KRUSKAL-WALLIS on unsigned error |total| - {scope}")
        print("-" * 78)
        if scope.startswith("OFF-AXIS"):
            print("At 90 deg the compression predicts zero error by construction, so")
            print("including it dilutes any distance or noise effect. This subset is")
            print("where those factors can actually show.")
        run_kruskal(subset)

    lat = [r for r in analysed if r.get("settle_from_voice_s") is not None]
    if lat:
        print("\n" + "-" * 78)
        print("KRUSKAL-WALLIS on detection latency (settle_from_voice_s, seconds)")
        print("-" * 78)
        print("Machine-timed on the Pi: array's first voice report -> head at rest.")
        run_kruskal(lat, value=lambda r: r.get("settle_from_voice_s"))

    if args.trials or args.moves or args.pgf_until:
        report_trials(paths, analysed, show_moves=args.moves, trajectories_until_s=args.pgf_until)

    if args.plots:
        make_plots(rows, analysed, paths, args.include_misses)

    print("\n" + "=" * 78)


def run_kruskal(analysed, value=None, label="unsigned error |total|"):
    if value is None:
        def value(r):
            return abs(r["total_error"]) if r["total_error"] is not None else None
    for factor in FACTORS:
        if factor == "angle_true" and len({r["angle_true"] for r in analysed}) < 2:
            continue
        buckets = defaultdict(list)
        for r in analysed:
            v = value(r)
            if v is not None:
                buckets[factor_label(r, factor)].append(v)
        levels = sorted(buckets, key=level_sort_key)
        res = kruskal_wallis([buckets[l] for l in levels])
        print(f"\n{factor}")
        for l in levels:
            v = buckets[l]
            print(f"   {l:>10}  n={len(v):<3} median={median(v):6.3f}  "
                  f"IQR=[{quantile(v, .25):.3f},{quantile(v, .75):.3f}]")
        if res is None:
            print("   not enough data for a test")
            continue
        stars = "***" if res["p"] < .001 else "**" if res["p"] < .01 else "*" if res["p"] < .05 else "n.s."
        print(f"   H({res['df']}) = {res['H']:.3f}, p = {res['p']:.4f} {stars}, "
              f"eps^2 = {res['eps2']:.3f}, N = {res['n']}")
        if res["p"] < .05 and len(levels) > 2:
            print("   pairwise (Bonferroni-corrected):")
            pairs = [(a, b) for i, a in enumerate(levels) for b in levels[i + 1:]]
            for a, b in pairs:
                pr = kruskal_wallis([buckets[a], buckets[b]])
                if pr is None:
                    continue
                padj = min(1.0, pr["p"] * len(pairs))
                mark = "*" if padj < .05 else "n.s."
                print(f"      {a:>8} vs {b:<8} p_adj = {padj:.4f} {mark}")


def make_plots(rows, analysed, paths, include_misses):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\nmatplotlib is not installed - skipping plots "
              "(pip install matplotlib, or drop --plots).")
        return

    outdir = os.path.dirname(paths[0]) or "."
    errs_by_angle = defaultdict(list)
    for r in analysed:
        if r["total_error"] is not None:
            errs_by_angle[r["angle_true"]].append(r["total_error"])

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    angles = sorted(errs_by_angle)
    axes[0].boxplot([errs_by_angle[a] for a in angles], labels=[f"{a:g}" for a in angles])
    axes[0].axhline(0, color="grey", lw=.8, ls="--")
    axes[0].set_xlabel("true bearing (deg)")
    axes[0].set_ylabel("signed error (deg)")
    axes[0].set_title("Error by angle")

    by_dist = defaultdict(list)
    for r in analysed:
        if r["total_error"] is not None:
            by_dist[r["distance_m"]].append(abs(r["total_error"]))
    dists = sorted(by_dist)
    axes[1].boxplot([by_dist[d] for d in dists], labels=[f"{d:g}" for d in dists])
    axes[1].set_xlabel("distance (m)")
    axes[1].set_ylabel("absolute error (deg)")
    axes[1].set_title("Error by distance")

    by_noise = defaultdict(list)
    for r in analysed:
        if r["total_error"] is not None:
            by_noise[r["noise_condition"]].append(abs(r["total_error"]))
    noises = sorted(by_noise)
    axes[2].boxplot([by_noise[n] for n in noises], labels=noises)
    axes[2].set_xlabel("noise condition")
    axes[2].set_ylabel("absolute error (deg)")
    axes[2].set_title("Error by noise")

    fig.tight_layout()
    p1 = os.path.join(outdir, "head_accuracy_error.png")
    fig.savefig(p1, dpi=150)

    fig2, ax = plt.subplots(figsize=(6, 6))
    for noise in sorted({r["noise_condition"] for r in analysed}):
        xs = [r["angle_true"] for r in analysed
              if r["noise_condition"] == noise and r["total_error"] is not None]
        ys = [r["angle_true"] + r["total_error"] for r in analysed
              if r["noise_condition"] == noise and r["total_error"] is not None]
        ax.scatter(xs, ys, label=noise, alpha=.7)
    lo = min([r["angle_true"] for r in analysed] or [0]) - 15
    hi = max([r["angle_true"] for r in analysed] or [180]) + 15
    ax.plot([lo, hi], [lo, hi], color="grey", ls="--", lw=.8, label="perfect")
    ax.set_xlabel("true bearing (deg)")
    ax.set_ylabel("achieved head angle (deg)")
    ax.set_title("Achieved vs true bearing")
    ax.legend()
    fig2.tight_layout()
    p2 = os.path.join(outdir, "head_accuracy_scatter.png")
    fig2.savefig(p2, dpi=150)
    print(f"\nPlots written: {p1}\n               {p2}")


if __name__ == "__main__":
    main()
