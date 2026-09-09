import argparse
import csv
import glob
import math
import os
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
    args = parser.parse_args()

    study_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "study")
    paths = args.csv or sorted(
        p for p in glob.glob(os.path.join(study_dir, "head_accuracy_*.csv"))
        if not p.endswith("_trials.csv"))
    if not paths:
        print("No CSV files found. Pass paths explicitly or run the study first.")
        return

    rows = enrich(load(paths))
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

    if args.plots:
        make_plots(rows, analysed, paths, args.include_misses)

    print("\n" + "=" * 78)


def run_kruskal(analysed):
    for factor in FACTORS:
        if factor == "angle_true" and len({r["angle_true"] for r in analysed}) < 2:
            continue
        buckets = defaultdict(list)
        for r in analysed:
            if r["total_error"] is not None:
                buckets[factor_label(r, factor)].append(abs(r["total_error"]))
        levels = sorted(buckets, key=level_sort_key)
        res = kruskal_wallis([buckets[l] for l in levels])
        print(f"\n{factor}")
        for l in levels:
            v = buckets[l]
            print(f"   {l:>10}  n={len(v):<3} median|e|={median(v):5.2f}  "
                  f"IQR=[{quantile(v, .25):.2f},{quantile(v, .75):.2f}]")
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
