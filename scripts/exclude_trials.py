import argparse
import csv
import os
import shutil

STUDY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "study")
EXTRA_COLUMNS = ["excluded", "exclude_reason"]


def session_path(session):
    return os.path.join(STUDY_DIR, f"head_accuracy_{session}.csv")


def load(path):
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), list(reader.fieldnames or [])


def save(path, rows, fieldnames):
    backup = path + ".bak"
    shutil.copy2(path, backup)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    return backup


def is_excluded(row):
    return (row.get("excluded") or "0").strip() in ("1", "true", "True")


def cell_of(row):
    return (row.get("angle_true", "?"), row.get("distance_m", "?"),
            row.get("noise_condition", "?"))


def cmd_list(rows, args):
    counts = {}
    for r in rows:
        if not is_excluded(r):
            counts[cell_of(r)] = counts.get(cell_of(r), 0) + 1

    over = {c: n for c, n in counts.items() if n > args.reps}
    if over:
        print(f"Cells with MORE than {args.reps} counted trials:")
        for c, n in sorted(over.items()):
            print(f"  angle={c[0]} distance={c[1]} noise={c[2]}  -> {n} trials "
                  f"({n - args.reps} extra)")
        print()
    else:
        print(f"No cell has more than {args.reps} counted trials.\n")

    shown = rows
    if args.noise:
        shown = [r for r in shown if r.get("noise_condition") == args.noise]
    if args.angle is not None:
        shown = [r for r in shown if r.get("angle_true") == f"{args.angle:g}"]
    if args.distance is not None:
        shown = [r for r in shown if r.get("distance_m") == f"{args.distance:g}"]

    print(f"{'id':>4} {'timestamp':<24} {'ang':>4} {'dist':>5} {'noise':>8} "
          f"{'rep':>3} {'target':>7} {'miss':>4}  flag")
    print("-" * 78)
    for r in shown:
        flag = ""
        if is_excluded(r):
            flag = f"EXCLUDED: {r.get('exclude_reason', '')}"
        print(f"{r.get('trial_id', ''):>4} {r.get('timestamp', ''):<24} "
              f"{r.get('angle_true', ''):>4} {r.get('distance_m', ''):>5} "
              f"{r.get('noise_condition', ''):>8} {r.get('rep', ''):>3} "
              f"{r.get('servo_target_angle', ''):>7} {r.get('miss', ''):>4}  {flag}")
    print()


def cmd_exclude(rows, fieldnames, path, args):
    ids = {str(i) for i in args.exclude}
    found = [r for r in rows if str(r.get("trial_id")) in ids]
    missing = ids - {str(r.get("trial_id")) for r in found}
    if missing:
        raise SystemExit(f"no trial with id {', '.join(sorted(missing))} in {path}")

    for col in EXTRA_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
    for r in rows:
        r.setdefault("excluded", "")
        r.setdefault("exclude_reason", "")
    for r in found:
        r["excluded"] = "1"
        r["exclude_reason"] = args.reason

    print(f"Marking {len(found)} trial(s) excluded, reason: {args.reason}")
    for r in found:
        print(f"  trial {r['trial_id']}  angle={r.get('angle_true')} "
              f"distance={r.get('distance_m')} noise={r.get('noise_condition')} "
              f"rep={r.get('rep')}")
    backup = save(path, rows, fieldnames)
    print(f"\nWritten. Original backed up to {os.path.basename(backup)}")
    print("The rows stay in the file - the analyser skips them and reports the count.")


def cmd_delete(rows, fieldnames, path, args):
    ids = {str(i) for i in args.delete}
    doomed = [r for r in rows if str(r.get("trial_id")) in ids]
    missing = ids - {str(r.get("trial_id")) for r in doomed}
    if missing:
        raise SystemExit(f"no trial with id {', '.join(sorted(missing))} in {path}")

    print(f"Deleting {len(doomed)} trial(s):")
    for r in doomed:
        print(f"  trial {r['trial_id']}  {r.get('timestamp', '')}  "
              f"angle={r.get('angle_true')} distance={r.get('distance_m')} "
              f"noise={r.get('noise_condition')} rep={r.get('rep')} "
              f"target={r.get('servo_target_angle')}")

    kept = [r for r in rows if str(r.get("trial_id")) not in ids]
    backup = save(path, kept, fieldnames)
    print(f"\n{len(kept)} trial(s) remain. CSV backed up to {os.path.basename(backup)}")

    jsonl = path.replace(".csv", "_trials.jsonl")
    if os.path.exists(jsonl):
        import json
        shutil.copy2(jsonl, jsonl + ".bak")
        keep_lines = []
        removed = 0
        with open(jsonl) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    keep_lines.append(line)
                    continue
                if str(rec.get("trial_id")) in ids:
                    removed += 1
                else:
                    keep_lines.append(line)
        with open(jsonl, "w") as f:
            f.writelines(keep_lines)
        print(f"Removed {removed} matching record(s) from {os.path.basename(jsonl)} "
              f"(backed up too)")


def cmd_restore(rows, fieldnames, path, args):
    ids = {str(i) for i in args.restore}
    n = 0
    for r in rows:
        if str(r.get("trial_id")) in ids and is_excluded(r):
            r["excluded"] = "0"
            r["exclude_reason"] = ""
            n += 1
    if not n:
        print("Nothing to restore.")
        return
    save(path, rows, fieldnames)
    print(f"Restored {n} trial(s).")


def main():
    parser = argparse.ArgumentParser(
        description="List trials and mark ones that should not count toward the study.")
    parser.add_argument("--session", required=True)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--noise", help="only show this noise condition")
    parser.add_argument("--angle", type=float, help="only show this angle")
    parser.add_argument("--distance", type=float, help="only show this distance")
    parser.add_argument("--exclude", type=int, nargs="+", metavar="ID",
                        help="trial ids to mark as excluded")
    parser.add_argument("--delete", type=int, nargs="+", metavar="ID",
                        help="permanently remove these trials from the CSV and the trial log")
    parser.add_argument("--restore", type=int, nargs="+", metavar="ID",
                        help="trial ids to un-exclude")
    parser.add_argument("--reason", default="", help="why they are excluded (required with --exclude)")
    args = parser.parse_args()

    path = session_path(args.session)
    if not os.path.exists(path):
        raise SystemExit(f"no such session file: {path}")
    rows, fieldnames = load(path)

    if args.delete:
        cmd_delete(rows, fieldnames, path, args)
    elif args.exclude:
        if not args.reason:
            raise SystemExit("--exclude needs --reason, so the write-up can state why.")
        cmd_exclude(rows, fieldnames, path, args)
    elif args.restore:
        cmd_restore(rows, fieldnames, path, args)
    else:
        cmd_list(rows, args)


if __name__ == "__main__":
    main()
