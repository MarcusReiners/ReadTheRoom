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
DEFAULT_ANGLES = [30, 45, 60, 75, 90, 105, 120, 135, 150]
ONSET_SKIP_S = 1.5
SAMPLE_INTERVAL_S = 0.25
SAMPLES = 7
GATHER_TIMEOUT_S = 12.0


def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        return "q"


def gather(doa, samples, interval_s, onset_skip_s, timeout_s):
    """Waits for voice, discards the array's unconverged onset, then samples."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if doa.get_voice_active():
            break
        time.sleep(0.05)
    else:
        return []
    time.sleep(onset_skip_s)
    out = []
    while len(out) < samples and time.monotonic() < deadline:
        if doa.get_voice_active():
            out.append(doa.get_direction_degrees())
        time.sleep(interval_s)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure the array's angular response using the servo to define each angle.")
    parser.add_argument("--angles", type=float, nargs="+", default=DEFAULT_ANGLES)
    parser.add_argument("--distance", type=float, required=True)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--session", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--samples", type=int, default=SAMPLES)
    parser.add_argument("--interval", type=float, default=SAMPLE_INTERVAL_S)
    parser.add_argument("--onset-skip", type=float, default=ONSET_SKIP_S)
    args = parser.parse_args()

    logging_setup.configure_logging(config)
    if not config.USE_SERVO:
        print("USE_SERVO is False - the servo defines the angles, so this needs it.")
        return

    from adapters.factory import build_turntable
    from adapters.hardware.doa_respeaker import RespeakerDOAAdapter, median_angle

    turntable = build_turntable(config)
    doa = RespeakerDOAAdapter(front_reference_degrees=config.DOA_FRONT_REFERENCE_DEGREES)
    home = (turntable.safe_min_angle + turntable.safe_max_angle) / 2.0

    os.makedirs(STUDY_DIR, exist_ok=True)
    path = os.path.join(STUDY_DIR, f"doa_angle_response_{args.session}.csv")
    columns = ["timestamp", "session", "true_angle", "distance_m", "rep",
               "reported_angle", "error", "local_gain", "n_samples", "samples"]
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=columns).writeheader()

    print(f"\nAngular response calibration - {args.distance} m, {args.reps} rep(s) per angle")
    print(f"Angles: {', '.join(f'{a:g}' for a in args.angles)}")
    print(f"Writing to {path}")
    print("\nThe servo defines each angle, so no floor marks are needed.")
    print("Per angle: the head points where the source must go, you place it there,")
    print("the head returns home, then you play the stimulus.\n")

    results = {}
    try:
        for angle in args.angles:
            for rep in range(1, args.reps + 1):
                print(f"--- {angle:g} deg, rep {rep}/{args.reps} ---")
                turntable.set_doa_angle_immediate(angle)
                time.sleep(0.8)
                if ask(f"Head is pointing at {angle:g} deg. Put the source exactly where it "
                       f"looks, {args.distance} m away, then ENTER (q to quit) > ").strip().lower() == "q":
                    raise KeyboardInterrupt
                turntable.home(ramp_duration_s=0.6)
                time.sleep(1.2)
                if ask("Head is back home. Start the stimulus, then ENTER > ").strip().lower() == "q":
                    raise KeyboardInterrupt

                got = gather(doa, args.samples, args.interval, args.onset_skip, GATHER_TIMEOUT_S)
                if len(got) < 3:
                    print(f"  only {len(got)} reading(s) - not enough, repeating this rep.\n")
                    continue
                reported = median_angle(got)
                err = reported - angle
                gain = ((reported - 90.0) / (angle - 90.0)) if angle != 90.0 else float("nan")
                print(f"  reported {reported:.1f}  error {err:+.1f}  "
                      f"local gain {gain:.3f}  (n={len(got)})\n")
                results.setdefault(angle, []).append(reported)
                with open(path, "a", newline="") as f:
                    csv.DictWriter(f, fieldnames=columns).writerow({
                        "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                        "session": args.session, "true_angle": angle,
                        "distance_m": args.distance, "rep": rep,
                        "reported_angle": round(reported, 2), "error": round(err, 2),
                        "local_gain": "" if angle == 90.0 else round(gain, 4),
                        "n_samples": len(got),
                        "samples": " ".join(f"{v:.0f}" for v in got),
                    })
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        turntable.home(ramp_duration_s=0.5)
        time.sleep(0.4)
        if hasattr(turntable, "stop"):
            turntable.stop()
        if hasattr(doa, "close"):
            doa.close()

    if results:
        print("\n" + "=" * 62)
        print("ANGULAR RESPONSE")
        print(f"{'true':>6} {'reported':>10} {'error':>8} {'local gain':>11} {'reps':>5}")
        for angle in sorted(results):
            vals = results[angle]
            m = statistics.median(vals)
            gain = "" if angle == 90.0 else f"{(m - 90.0) / (angle - 90.0):.3f}"
            print(f"{angle:>6g} {m:>10.1f} {m - angle:>+8.1f} {gain:>11} {len(vals):>5}")
        print("\nA correction lookup maps reported back to true. Interpolate between")
        print("these points rather than fitting one gain - the response is not linear.")
    print(f"\nSaved to {path}")


if __name__ == "__main__":
    main()
