import argparse
import csv
import glob
import math
import os
import statistics

STUDY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "study")


def load(paths):
    by_true = {}
    for path in paths:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                raw = (row.get("reported_angle") or "").strip()
                if not raw:
                    continue
                try:
                    by_true.setdefault(float(row["true_angle"]), []).append(float(raw))
                except (KeyError, ValueError):
                    continue
    return sorted((t - 90.0, statistics.median(v) - 90.0) for t, v in by_true.items())


def lstsq(rows, ys):
    n = len(rows[0])
    a = [[sum(r[i] * r[j] for r in rows) for j in range(n)] + [sum(r[i] * y for r, y in zip(rows, ys))]
         for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            return None
        a[col], a[piv] = a[piv], a[col]
        for r in range(n):
            if r == col:
                continue
            f = a[r][col] / a[col][col]
            for c in range(col, n + 1):
                a[r][c] -= f * a[col][c]
    return [a[i][n] / a[i][i] for i in range(n)]


def basis_linear(t):
    return [t]


def basis_cubic(t):
    return [t, t ** 3]


def basis_quintic(t):
    return [t, t ** 3, t ** 5]


MODELS = {
    "gain  r = k*t": (basis_linear, 1),
    "cubic r = a*t + c*t^3": (basis_cubic, 2),
    "quintic r = a*t + c*t^3 + e*t^5": (basis_quintic, 3),
}


def fit(points, basis):
    rows = [basis(t) for t, _ in points]
    ys = [r for _, r in points]
    return lstsq(rows, ys)


def predict(coef, basis, t):
    return sum(c * b for c, b in zip(coef, basis(t)))


def fit_arcsin(points):
    """r = asin(g * sin(t)) - the form a delay-based estimator produces when
    it under-reads the inter-microphone delay."""
    best = None
    g = 0.30
    while g <= 1.30:
        ss = 0.0
        ok = True
        for t, r in points:
            v = g * math.sin(math.radians(t))
            if abs(v) > 1.0:
                ok = False
                break
            ss += (math.degrees(math.asin(v)) - r) ** 2
        if ok and (best is None or ss < best[1]):
            best = (g, ss)
        g += 0.001
    return best


def r2(points, pred):
    ys = [r for _, r in points]
    mean = sum(ys) / len(ys)
    tot = sum((y - mean) ** 2 for y in ys)
    res = sum((y - pred(t)) ** 2 for t, y in points)
    return 1.0 - res / tot if tot > 0 else float("nan")


def loo_rmse(points, fitter, predictor, n_params):
    """Leave-one-out error: the guard against a model that only looks good
    because it has enough parameters to pass through every point."""
    if len(points) <= n_params + 1:
        return None
    errs = []
    for i in range(len(points)):
        train = points[:i] + points[i + 1:]
        model = fitter(train)
        if model is None:
            return None
        t, y = points[i]
        errs.append((predictor(model, t) - y) ** 2)
    return math.sqrt(sum(errs) / len(errs))


def main():
    parser = argparse.ArgumentParser(
        description="Fit candidate response curves to a DOA angle calibration.")
    parser.add_argument("csv", nargs="*", help="default: newest doa_angle_response_*.csv")
    args = parser.parse_args()

    paths = args.csv or sorted(glob.glob(os.path.join(STUDY_DIR, "doa_angle_response_*.csv")),
                               key=os.path.getmtime)[-1:]
    if not paths:
        print("No calibration files found.")
        return
    points = load(paths)
    if len(points) < 3:
        print(f"Only {len(points)} calibrated angle(s) - need at least 3 to compare models.")
        return

    print(f"# {', '.join(os.path.basename(p) for p in paths)}")
    print(f"\n{len(points)} angles, as offsets from straight ahead:")
    print(f"  {'true':>6} {'reported':>9} {'error':>7} {'local gain':>11}")
    for t, r in points:
        gain = f"{r/t:.3f}" if t else "-"
        print(f"  {t:>+6.0f} {r:>+9.1f} {r-t:>+7.1f} {gain:>11}")

    print(f"\n{'model':>34} {'R2':>8} {'params':>7} {'LOO rmse':>9}  coefficients")
    rows = []
    for name, (basis, n) in MODELS.items():
        coef = fit(points, basis)
        if coef is None:
            continue
        rr = r2(points, lambda t, c=coef, b=basis: predict(c, b, t))
        loo = loo_rmse(points,
                       lambda pts, b=basis: fit(pts, b),
                       lambda m, t, b=basis: predict(m, b, t), n)
        rows.append((name, rr, n, loo))
        coefs = "  ".join(f"{c:+.4g}" for c in coef)
        print(f"{name:>34} {rr:>8.4f} {n:>7} "
              f"{('%9.2f' % loo) if loo is not None else '        -'}  {coefs}")

    best = fit_arcsin(points)
    if best:
        g, _ = best
        rr = r2(points, lambda t, g=g: math.degrees(math.asin(max(-1.0, min(1.0, g * math.sin(math.radians(t)))))))
        loo = loo_rmse(points, lambda pts: fit_arcsin(pts)[0],
                       lambda m, t: math.degrees(math.asin(max(-1.0, min(1.0, m * math.sin(math.radians(t)))))), 1)
        rows.append(("arcsin r = asin(g*sin t)", rr, 1, loo))
        print(f"{'arcsin r = asin(g*sin t)':>34} {rr:>8.4f} {1:>7} "
              f"{('%9.2f' % loo) if loo is not None else '        -'}  g = {g:.4f}")

    usable = [r for r in rows if r[3] is not None]
    if usable:
        win = min(usable, key=lambda r: r[3])
        print(f"\nBest by leave-one-out error: {win[0].split(' ')[0]} "
              f"(LOO rmse {win[3]:.2f} deg, {win[2]} parameter(s))")
        print("Judge by LOO, not R2 - R2 always improves with more parameters,")
        print("which is exactly how the 3-point linear fit misled us before.")
    else:
        print("\nToo few angles for leave-one-out validation. Measure more.")
    print("\nIf no closed form wins clearly, use the measured lookup table:")
    print("  DOA_CALIBRATION_PATH=<this csv>")


if __name__ == "__main__":
    main()
