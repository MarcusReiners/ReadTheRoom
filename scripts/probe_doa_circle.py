import argparse
import csv
import os
import statistics
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup

STUDY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "study")
DEFAULT_ANGLES = [90, 60, 30, 0, -30, -60, 270, 240, 210, 180, 150, 120]
POLL_S = 0.05
RECORD_S = 6.0
SETTLE_S = 1.5
PROD_WINDOW_S = 0.6
PROD_SAMPLES = 5
PROD_MIN_SAMPLES = 2


def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        return "q"


def turn(angle):
    return (angle - 90.0 + 180.0) % 360.0 - 180.0


def side(angle):
    t = turn(angle)
    if abs(t) < 1.0 or abs(t) > 179.0:
        return 0
    return 1 if t > 0 else -1


def servo_side(estimate):
    """The servo adds estimate - 90 to its heading and clamps linearly, so an
    estimate of 300 turns it the +210 way, not the -150 way."""
    t = estimate - 90.0
    if abs(t) < 1.0:
        return 0
    return 1 if t > 0 else -1


def pointing(angle):
    """Where the head has to point to show this direction, and whether the
    source goes in front of it or behind it on that line."""
    a = (angle + 90.0) % 360.0 - 90.0
    if 0.0 <= a <= 180.0:
        return a, "in front of the head, where it points"
    return (a - 180.0 if a > 180.0 else a + 180.0), "BEHIND the head, on the line opposite to where it points"


def production_estimate(polls, median_angle):
    """start_doa_tracking()'s production sampling, approximated on the polls:
    the reading at voice onset, then further voiced readings within the
    sample window, up to doa_samples."""
    onset = next((i for i, p in enumerate(polls) if p["voice"]), None)
    if onset is None:
        return None, None, []
    t0 = polls[onset]["t"]
    samples = [p["target"] for p in polls[onset:] if p["voice"] and p["t"] - t0 <= PROD_WINDOW_S][:PROD_SAMPLES]
    if len(samples) < PROD_MIN_SAMPLES:
        return None, None, samples
    return median_angle(samples), statistics.median(samples), samples


def main():
    parser = argparse.ArgumentParser(description="Log the ReSpeaker's direction readings from all around the head.")
    parser.add_argument("--angles", type=float, nargs="+", default=DEFAULT_ANGLES,
                        help="true directions in the 90 = straight ahead convention, -90..270")
    parser.add_argument("--distance", type=float, required=True)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--session", default=datetime.now().strftime("%Y%m%d_%H%M"))
    parser.add_argument("--source", default="voice", help="what makes the sound, for the data sheet")
    parser.add_argument("--no-servo", action="store_true", help="place the source by floor marks instead")
    args = parser.parse_args()
    logging_setup.configure_logging(config)

    from adapters.hardware.doa_respeaker import RespeakerDOAAdapter, median_angle, raw_to_target_degrees
    doa = RespeakerDOAAdapter(front_reference_degrees=config.DOA_FRONT_REFERENCE_DEGREES)
    turntable = None
    if not args.no_servo:
        if not config.USE_SERVO:
            raise SystemExit("USE_SERVO is False - run with --no-servo and floor marks.")
        from adapters.factory import build_turntable
        turntable = build_turntable(config)
        turntable.home(ramp_duration_s=0.6)

    os.makedirs(STUDY_DIR, exist_ok=True)
    reps_path = os.path.join(STUDY_DIR, f"doa_circle_{args.session}.csv")
    polls_path = os.path.join(STUDY_DIR, f"doa_circle_{args.session}_polls.csv")
    rep_cols = ["timestamp", "session", "true_angle", "distance_m", "source", "rep", "front_reference",
                "voiced_polls", "settled_median", "settled_error", "wrong_side_share", "onset_target",
                "prod_samples", "prod_estimate", "prod_turn_correct", "plain_estimate", "plain_turn_correct"]
    poll_cols = ["session", "true_angle", "rep", "t", "raw", "target", "voice", "speech"]
    for path, cols in ((reps_path, rep_cols), (polls_path, poll_cols)):
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=cols).writeheader()

    print(f"\nDirection logging - {args.distance} m, {args.reps} rep(s) per direction, source: {args.source}")
    print("Main app stopped? The head stays at home while recording; it only points to show where to stand.")
    print(f"Writing {reps_path}\n    and {polls_path}\n")
    results = {}
    try:
        for angle in args.angles:
            point, where = pointing(angle)
            rep = 1
            while rep <= args.reps:
                print(f"--- direction {angle:g} (turn {turn(angle):+.0f}), rep {rep}/{args.reps} ---")
                if turntable is not None:
                    turntable.set_doa_angle_immediate(point)
                    time.sleep(0.8)
                    msg = f"Stand {where}, {args.distance} m away"
                else:
                    msg = f"Stand at the floor mark for {angle:g}, {args.distance} m away"
                if ask(msg + ", then ENTER (q to quit) > ").strip().lower() == "q":
                    raise KeyboardInterrupt
                if turntable is not None:
                    turntable.home(ramp_duration_s=0.6)
                    time.sleep(1.2)
                ask("Head is home. ENTER, then speak for about 5 s > ")
                polls = []
                start = time.monotonic()
                while time.monotonic() - start < RECORD_S:
                    raw = doa.get_raw_direction_degrees()
                    polls.append({"t": time.monotonic() - start, "raw": raw,
                                  "target": raw_to_target_degrees(raw, config.DOA_FRONT_REFERENCE_DEGREES),
                                  "voice": int(doa.get_voice_active()), "speech": int(doa.get_speech_detected())})
                    time.sleep(POLL_S)
                voiced = [p for p in polls if p["voice"]]
                if len(voiced) < 3:
                    print(f"  only {len(voiced)} voiced reading(s) - repeating this rep.\n")
                    continue
                onset_t = voiced[0]["t"]
                settled = [p["target"] for p in voiced if p["t"] - onset_t >= SETTLE_S]
                settled_median = median_angle(settled) if settled else None
                s = side(angle)
                wrong = (sum(1 for p in voiced if side(p["target"]) == -s) / len(voiced)) if s else None
                prod, plain, samples = production_estimate(polls, median_angle)
                row = {
                    "timestamp": datetime.now().isoformat(timespec="milliseconds"), "session": args.session,
                    "true_angle": angle, "distance_m": args.distance, "source": args.source, "rep": rep,
                    "front_reference": config.DOA_FRONT_REFERENCE_DEGREES, "voiced_polls": len(voiced),
                    "settled_median": "" if settled_median is None else round(settled_median, 1),
                    "settled_error": "" if settled_median is None else round(turn(settled_median - angle + 90.0), 1),
                    "wrong_side_share": "" if wrong is None else round(wrong, 3),
                    "onset_target": round(voiced[0]["target"], 1),
                    "prod_samples": " ".join(f"{v:.0f}" for v in samples),
                    "prod_estimate": "" if prod is None else round(prod, 1),
                    "prod_turn_correct": "" if prod is None or not s else int(servo_side(prod) == s),
                    "plain_estimate": "" if plain is None else round(plain, 1),
                    "plain_turn_correct": "" if plain is None or not s else int(servo_side(plain) == s),
                }
                with open(reps_path, "a", newline="") as f:
                    csv.DictWriter(f, fieldnames=rep_cols).writerow(row)
                with open(polls_path, "a", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=poll_cols)
                    for p in polls:
                        w.writerow({"session": args.session, "true_angle": angle, "rep": rep,
                                    "t": round(p["t"], 3), "raw": p["raw"], "target": p["target"],
                                    "voice": p["voice"], "speech": p["speech"]})
                print(f"  settled {row['settled_median']}  error {row['settled_error']}  "
                      f"wrong side {row['wrong_side_share']}  onset {row['onset_target']}  "
                      f"production {row['prod_estimate']} ({' '.join(f'{v:.0f}' for v in samples) or '-'})  "
                      f"plain median {row['plain_estimate']}\n")
                results.setdefault(angle, []).append(row)
                rep += 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if turntable is not None:
            turntable.home(ramp_duration_s=0.5)
            time.sleep(0.4)
            if hasattr(turntable, "stop"):
                turntable.stop()

    if results:
        print("\n" + "=" * 96)
        print(f"{'true':>6} {'turn':>6} {'settled':>8} {'error':>7} {'wrong side':>11} "
              f"{'production right way':>21} {'plain median right way':>23}")
        for angle, rows in results.items():
            settled = [float(r["settled_median"]) for r in rows if r["settled_median"] != ""]
            wrong = [float(r["wrong_side_share"]) for r in rows if r["wrong_side_share"] != ""]
            prod = [int(r["prod_turn_correct"]) for r in rows if r["prod_turn_correct"] != ""]
            plain = [int(r["plain_turn_correct"]) for r in rows if r["plain_turn_correct"] != ""]
            med = statistics.median(settled) if settled else None
            print(f"{angle:>6g} {turn(angle):>+6.0f} {'-' if med is None else f'{med:.1f}':>8} "
                  f"{'-' if med is None else f'{turn(med - angle + 90.0):+.1f}':>7} "
                  f"{'-' if not wrong else f'{100 * statistics.median(wrong):.0f}%':>11} "
                  f"{f'{sum(prod)}/{len(prod)}' if prod else '-':>21} {f'{sum(plain)}/{len(plain)}' if plain else '-':>23}")
        print("\n'wrong side' = share of voiced readings on the other side of the head's nose; "
              "'right way' = the estimate would turn the head towards the source.")
    print(f"\nSaved to {reps_path}")


if __name__ == "__main__":
    main()
