import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup

STUDY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "study")
SETTLE_QUIET_S = 1.0
MISS_TIMEOUT_S = 5.0
ARM_TIMEOUT_S = 30.0
POST_MOVE_QUIET_S = 1.5
MIN_DOA_SAMPLES = 3
DOA_SAMPLE_INTERVAL_S = 0.2
RESUME_LATENCY_S = 0.35
HOME_SETTLE_S = 1.5
HOME_TOLERANCE_DEG = 2.0
HOME_ATTEMPTS = 3
MEASUREMENT_SANITY_DEG = 20.0


def playback_command(path):
    if sys.platform == "darwin":
        return ["afplay", path]
    return ["aplay", "-q", path]


def check_player():
    exe = playback_command("x")[0]
    if shutil.which(exe) is None:
        raise SystemExit(f"'{exe}' not found - install it or drop --play to time playback by hand.")
    return exe

CSV_COLUMNS = [
    "trial_id", "session", "timestamp", "angle_true", "distance_m", "noise_condition", "rep",
    "playback_start_ts", "settle_ts", "settle_time_s", "settle_from_voice_s", "playback_mode",
    "head_heading_at_start", "started_at_home",
    "doa_logged_angle", "head_heading_at_doa", "doa_implied_bearing",
    "servo_target_angle", "servo_first_target_angle", "n_track_moves", "physical_angle_measured",
    "moved", "miss", "simulated", "n_doa_samples", "n_servo_commands", "notes",
]


class TrialRecorder:
    def __init__(self):
        self._lock = threading.Lock()
        self.doa_samples = []
        self.moves = []
        self.pwm_updates = []

    def clear(self):
        with self._lock:
            self.doa_samples = []
            self.moves = []
            self.pwm_updates = []

    def add_doa(self, angle, head_heading):
        with self._lock:
            self.doa_samples.append({
                "t": time.monotonic(), "ts": _now_iso(),
                "doa_angle": round(angle, 2), "head_heading": round(head_heading, 2),
            })

    def add_move(self, kind, target, resulting_heading, heading_before=None):
        with self._lock:
            self.moves.append({
                "t": time.monotonic(), "ts": _now_iso(), "kind": kind,
                "target": None if target is None else round(target, 2),
                "resulting_heading": round(resulting_heading, 2),
                "heading_before": None if heading_before is None else round(heading_before, 2),
            })

    def add_pwm(self, angle):
        with self._lock:
            self.pwm_updates.append({"t": time.monotonic(), "angle": round(angle, 2)})

    def snapshot(self):
        with self._lock:
            return list(self.doa_samples), list(self.moves), list(self.pwm_updates)

    def last_activity_time(self):
        with self._lock:
            times = [e[-1]["t"] for e in (self.pwm_updates, self.moves) if e]
            return max(times) if times else None

    def first_doa_time(self):
        with self._lock:
            return self.doa_samples[0]["t"] if self.doa_samples else None

    def has_track_move(self):
        with self._lock:
            return any(m["kind"] == "track" for m in self.moves)


class RecordingDOA:
    def __init__(self, doa, turntable, recorder):
        self._doa = doa
        self._turntable = turntable
        self._recorder = recorder

    def get_voice_active(self):
        return self._doa.get_voice_active()

    def get_direction_degrees(self):
        angle = self._doa.get_direction_degrees()
        self._recorder.add_doa(angle, self._turntable.current_heading_degrees)
        return angle

    def __getattr__(self, name):
        return getattr(self._doa, name)


class RecordingTurntable:
    def __init__(self, turntable, recorder):
        self._turntable = turntable
        self._recorder = recorder

    def rotate_towards(self, target_angle_degrees, ramp_duration_s=None):
        heading_before = self._turntable.current_heading_degrees
        self._turntable.rotate_towards(target_angle_degrees, ramp_duration_s=ramp_duration_s)
        self._recorder.add_move("track", target_angle_degrees,
                                self._turntable.current_heading_degrees, heading_before)

    def home(self, ramp_duration_s=None):
        heading_before = self._turntable.current_heading_degrees
        self._turntable.home(ramp_duration_s=ramp_duration_s)
        self._recorder.add_move("home", None, self._turntable.current_heading_degrees,
                                heading_before)

    def __getattr__(self, name):
        return getattr(self._turntable, name)


def patch_pwm_recording(turntable, recorder):
    original = turntable.set_angle_immediate

    def recording_set_angle(angle_degrees):
        original(angle_degrees)
        recorder.add_pwm(turntable.current_heading_degrees)

    turntable.set_angle_immediate = recording_set_angle


class SilentFace:
    def set_eye_direction(self, **kwargs):
        pass

    def animate_eye_direction(self, *args, **kwargs):
        pass


class SimulatedDOA:
    def __init__(self, bearing_provider, error_degrees=6.0):
        self._bearing = bearing_provider
        self._error = error_degrees
        self._active_until = 0.0

    def arm(self, duration_s=2.0):
        self._active_until = time.monotonic() + duration_s

    def disarm(self):
        self._active_until = 0.0

    def get_voice_active(self):
        return time.monotonic() < self._active_until

    def get_direction_degrees(self):
        import random
        return self._bearing() + random.uniform(-self._error, self._error)

    def close(self):
        pass


def _ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        print()
        return "q"


def _now_iso():
    return datetime.now().isoformat(timespec="milliseconds")


def build_conditions(angles, distances, noises):
    conditions = []
    for distance in distances:
        for angle in angles:
            for noise in noises:
                conditions.append({"angle_true": angle, "distance_m": distance, "noise_condition": noise})
    return conditions


def print_plan(angles, distances, noises, reps):
    conditions = build_conditions(angles, distances, noises)
    print(f"\nRun plan: {len(conditions)} conditions x {reps} reps = {len(conditions) * reps} trials")
    print("Ordered by distance first, so the speaker is repositioned as rarely as possible.\n")
    print(f"{'#':>3}  {'distance':>8}  {'angle':>5}  {'noise':<10}  command")
    for i, c in enumerate(conditions, 1):
        cmd = (f"--angle {c['angle_true']} --distance {c['distance_m']} "
               f"--noise {c['noise_condition']} --reps {reps}")
        print(f"{i:>3}  {c['distance_m']:>8}  {c['angle_true']:>5}  {c['noise_condition']:<10}  {cmd}")
    print()


def wait_until_settled(recorder, deadline_s, quiet_s, arm_timeout_s=None, progress=False):
    """Waits for the head to react and come to rest.

    deadline_s only starts counting once the array first reports voice, so an
    operator starting playback on another machine is under no time pressure.
    arm_timeout_s bounds that waiting-for-voice phase.
    """
    start = time.monotonic()
    arm_deadline = start + (arm_timeout_s if arm_timeout_s is not None else deadline_s)
    shown = None
    while True:
        now = time.monotonic()
        last = recorder.last_activity_time()
        if recorder.has_track_move() and last is not None and now - last >= quiet_s:
            if progress:
                sys.stdout.write("\r" + " " * 60 + "\r")
                sys.stdout.flush()
            return True
        voice_at = recorder.first_doa_time()
        if progress:
            if voice_at is None:
                label = f"  waiting for playback... {arm_deadline - now:.0f}s left"
            else:
                label = f"  voice detected, tracking... {now - voice_at:.1f}s"
            if label != shown:
                sys.stdout.write("\r" + label.ljust(58))
                sys.stdout.flush()
                shown = label
        if voice_at is None:
            if now >= arm_deadline:
                break
        elif now - voice_at >= deadline_s:
            break
        time.sleep(0.05)
    if progress:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()
    return False


def run_trial(recorder, turntable, session, trial_id, condition, rep, settle_quiet_s, miss_timeout_s,
              on_playback_start=None, play_file=None, play_latency_s=0.0, arm_timeout_s=None,
              simulated=False, measure=True, paused=None):
    print(f"\n--- trial {trial_id}  angle={condition['angle_true']}  "
          f"distance={condition['distance_m']}m  noise={condition['noise_condition']}  rep={rep} ---")
    if paused is not None:
        paused.set()
    print("Homing...")
    home_angle = (turntable.safe_min_angle + turntable.safe_max_angle) / 2.0
    for attempt in range(HOME_ATTEMPTS):
        turntable.home(ramp_duration_s=0.4)
        time.sleep(HOME_SETTLE_S)
        if abs(turntable.current_heading_degrees - home_angle) <= HOME_TOLERANCE_DEG:
            break
        print(f"  head drifted to {turntable.current_heading_degrees:.1f} during homing "
              f"(a tracking reaction was still in flight), retrying")
    player = None
    if play_file:
        print("Playing stimulus...")
        player = subprocess.Popen(playback_command(play_file),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        playback_start_mono = time.monotonic() + play_latency_s
        playback_start_ts = _now_iso()
        playback_mode = "auto"
    else:
        if _ask("ENTER to arm, then start playback on the Mac > ").strip().lower() == "q":
            raise KeyboardInterrupt
        playback_start_mono = time.monotonic()
        playback_start_ts = _now_iso()
        playback_mode = "manual"
    recorder.clear()
    if paused is not None:
        paused.clear()
        time.sleep(RESUME_LATENCY_S)
    start_heading = turntable.current_heading_degrees
    started_at_home = abs(start_heading - home_angle) <= HOME_TOLERANCE_DEG
    if not started_at_home:
        print(f"WARNING head is at {start_heading:.1f}, expected {home_angle:.1f}. "
              "Bearings for this trial will be off - redo it once the room is quiet.")
    if on_playback_start is not None:
        on_playback_start()

    settled = wait_until_settled(recorder, miss_timeout_s, settle_quiet_s, arm_timeout_s,
                                 progress=True)
    doa_samples, moves, pwm_updates = recorder.snapshot()
    if paused is not None:
        paused.set()
    track_moves = [m for m in moves if m["kind"] == "track"]

    if track_moves:
        settle_mono = max([e[-1]["t"] for e in (pwm_updates, moves) if e])
        settle_ts = _now_iso()
        settle_time_s = round(settle_mono - playback_start_mono, 3)
        settle_from_voice_s = (round(settle_mono - doa_samples[0]["t"], 3)
                               if doa_samples else "")
        servo_target = track_moves[-1]["resulting_heading"]
        servo_first_target = track_moves[0]["resulting_heading"]
    else:
        settle_ts = ""
        settle_time_s = ""
        settle_from_voice_s = ""
        servo_target = ""
        servo_first_target = ""

    if player is not None and player.poll() is None:
        player.wait()

    if track_moves:
        # The reading that actually drove the head, not doa_samples[0] - the
        # first recorded sample is often one the tracker itself rejected
        # (out of range, or too short a burst), and reporting that as the
        # perceived bearing contradicts the servo target on the same row.
        accepted = track_moves[0]
        doa_angle = accepted["target"]
        head_at_doa = accepted["heading_before"]
        implied = (round(head_at_doa + (doa_angle - 90.0), 2)
                   if head_at_doa is not None and doa_angle is not None else "")
    elif doa_samples:
        first = doa_samples[0]
        doa_angle = first["doa_angle"]
        head_at_doa = first["head_heading"]
        implied = round(head_at_doa + (doa_angle - 90.0), 2)
    else:
        doa_angle = head_at_doa = implied = ""

    miss = not track_moves
    row = {
        "trial_id": trial_id, "session": session, "timestamp": _now_iso(),
        "angle_true": condition["angle_true"], "distance_m": condition["distance_m"],
        "noise_condition": condition["noise_condition"], "rep": rep,
        "playback_start_ts": playback_start_ts, "settle_ts": settle_ts,
        "settle_time_s": settle_time_s, "settle_from_voice_s": settle_from_voice_s,
        "playback_mode": playback_mode,
        "head_heading_at_start": round(start_heading, 2), "started_at_home": int(started_at_home),
        "doa_logged_angle": doa_angle, "head_heading_at_doa": head_at_doa,
        "doa_implied_bearing": implied, "servo_target_angle": servo_target,
        "servo_first_target_angle": servo_first_target, "n_track_moves": len(track_moves),
        "physical_angle_measured": "",
        "moved": int(bool(track_moves)), "miss": int(miss), "simulated": int(bool(simulated)),
        "n_doa_samples": len(doa_samples), "n_servo_commands": len(pwm_updates),
        "notes": "",
    }
    detail = {
        "trial_id": trial_id, "session": session, "condition": condition, "rep": rep,
        "playback_start_ts": playback_start_ts, "settled": settled, "miss": miss,
        "playback_mode": playback_mode, "play_file": play_file or "",
        "simulated": bool(simulated),
        "doa_samples": doa_samples, "moves": moves, "pwm_updates": pwm_updates,
    }

    if miss:
        if not doa_samples:
            print(f"MISS - the array never reported voice within {arm_timeout_s or miss_timeout_s:.0f}s. "
                  "Did playback actually start?")
        else:
            print(f"MISS - voice detected but no servo movement within {miss_timeout_s}s")
    else:
        print(f"settle_time={settle_time_s}s  (from voice {settle_from_voice_s}s)  "
              f"doa={doa_angle}  implied_bearing={implied}  servo_target={servo_target}")
        if len(track_moves) > 1:
            print(f"  NOTE {len(track_moves)} moves this trial (first {servo_first_target}, "
                  f"final {servo_target}). Check the JSONL: retriggered by the servo's own "
                  "noise, or by the stimulus still playing?")
    if measure:
        print("Take the overhead photo now.")
    return row, detail


def append_row(csv_path, row):
    is_new = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def append_detail(jsonl_path, detail):
    with open(jsonl_path, "a") as f:
        f.write(json.dumps(detail) + "\n")


def next_trial_id(csv_path):
    if not os.path.exists(csv_path):
        return 1
    with open(csv_path, newline="") as f:
        return sum(1 for _ in csv.DictReader(f)) + 1


def main():
    parser = argparse.ArgumentParser(description="Study 1: head-orientation accuracy, one condition per run.")
    parser.add_argument("--angle", type=float, help="true bearing in degrees, 90 = straight ahead")
    parser.add_argument("--distance", type=float, help="speaker distance in metres")
    parser.add_argument("--noise", choices=["none", "ambient", "offaxis"], help="background noise condition")
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--session", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--settle-quiet", type=float, default=SETTLE_QUIET_S)
    parser.add_argument("--miss-timeout", type=float, default=MISS_TIMEOUT_S)
    parser.add_argument("--plan", action="store_true", help="print the full run plan and exit")
    parser.add_argument("--simulate", action="store_true", help="no hardware, for rehearsing the procedure")
    parser.add_argument("--play", metavar="WAV",
                        help="stimulus file the script plays itself, so onset is machine-timed")
    parser.add_argument("--doa-sample-interval", type=float, default=DOA_SAMPLE_INTERVAL_S,
                        help="seconds between DOA samples; must exceed the array's update period")
    parser.add_argument("--min-doa-samples", type=int, default=MIN_DOA_SAMPLES,
                        help="readings required before the head is allowed to move")
    parser.add_argument("--post-move-quiet", type=float, default=POST_MOVE_QUIET_S,
                        help="seconds the tracker ignores DOA after a move, to reject servo noise")
    parser.add_argument("--no-measure", action="store_true",
                        help="skip the protractor prompt; error is then commanded-only")
    parser.add_argument("--arm-timeout", type=float, default=ARM_TIMEOUT_S,
                        help="seconds to wait for playback to begin after arming a manual trial")
    parser.add_argument("--play-latency-ms", type=float, default=0.0,
                        help="constant audio-device startup latency to add to the onset timestamp")
    args = parser.parse_args()

    if args.play:
        if not os.path.isfile(args.play):
            raise SystemExit(f"stimulus file not found: {args.play}")
        check_player()

    if args.plan:
        print_plan([45, 90, 135], [0.5, 1.5, 3.0], ["none", "ambient"], args.reps)
        return

    if args.angle is None or args.distance is None or args.noise is None:
        parser.error("--angle, --distance and --noise are required (or use --plan)")

    logging_setup.configure_logging(config)
    os.makedirs(STUDY_DIR, exist_ok=True)
    csv_path = os.path.join(STUDY_DIR, f"head_accuracy_{args.session}.csv")
    jsonl_path = os.path.join(STUDY_DIR, f"head_accuracy_{args.session}_trials.jsonl")

    recorder = TrialRecorder()

    if args.simulate:
        from adapters.hardware.turntable import DummyTurntableAdapter
        base_turntable = DummyTurntableAdapter()
        base_doa = SimulatedDOA(lambda: args.angle)
    else:
        if not config.USE_SERVO:
            print("USE_SERVO is False - no servo to measure.")
            return
        from adapters.factory import build_turntable
        from adapters.hardware.doa_respeaker import RespeakerDOAAdapter
        base_turntable = build_turntable(config)
        base_doa = RespeakerDOAAdapter(front_reference_degrees=config.DOA_FRONT_REFERENCE_DEGREES)

    patch_pwm_recording(base_turntable, recorder)
    turntable = RecordingTurntable(base_turntable, recorder)
    doa = RecordingDOA(base_doa, base_turntable, recorder)

    from service_layer.handlers import start_doa_tracking
    tracking_paused = threading.Event()
    start_doa_tracking(
        doa, turntable, SilentFace(),
        lock=threading.Lock(),
        silence_timeout_s=0,
        post_move_quiet_s=args.post_move_quiet,
        min_doa_samples=args.min_doa_samples,
        doa_sample_interval_s=args.doa_sample_interval,
        sample_window_s=max(1.5, args.doa_sample_interval * 6),
        accept_range=(base_turntable.safe_min_angle, base_turntable.safe_max_angle),
        paused=tracking_paused,
    )

    print(f"\nSession {args.session}")
    print(f"Data sheet : {csv_path}")
    print(f"Trial log  : {jsonl_path}")
    print(f"Servo range: {base_turntable.safe_min_angle:.0f}-{base_turntable.safe_max_angle:.0f} deg")
    print(f"Post-move deaf period: {args.post_move_quiet:.1f}s")
    print(f"Min DOA samples to move: {args.min_doa_samples}")
    print(f"DOA sample spacing: {args.doa_sample_interval * 1000:.0f}ms "
          f"(5 samples span {args.doa_sample_interval * 4:.2f}s)")
    print(f"Accepted bearings: {base_turntable.safe_min_angle:.0f}-"
          f"{base_turntable.safe_max_angle:.0f} deg (others ignored as impossible)")
    print(f"Condition  : angle={args.angle} distance={args.distance}m noise={args.noise}, {args.reps} reps")
    if args.play:
        print(f"Stimulus   : {args.play} (auto, +{args.play_latency_ms:.0f}ms latency offset)")
    else:
        print(f"Stimulus   : manual - arm, then play on the Mac (up to {args.arm_timeout:.0f}s)")
        print("             report settle_from_voice_s, not settle_time_s")
    if args.no_measure:
        print("\nNo physical measurement: error is commanded-only (perception + mapping).")
        print("After each trial: ENTER = keep, r = redo this rep, q = quit.\n")
    else:
        print("\nAfter each trial: measure the head with the protractor, then")
        print("ENTER = keep and continue, r = redo this rep, q = quit.\n")

    condition = {"angle_true": args.angle, "distance_m": args.distance, "noise_condition": args.noise}
    rep = 1
    kept = 0
    try:
        while rep <= args.reps:
            trial_id = next_trial_id(csv_path)
            if args.simulate:
                base_doa.disarm()
            on_start = (lambda: base_doa.arm(1.5)) if args.simulate else None
            row, detail = run_trial(
                recorder, turntable, args.session, trial_id, condition, rep,
                args.settle_quiet, args.miss_timeout, on_playback_start=on_start,
                play_file=args.play, play_latency_s=args.play_latency_ms / 1000.0,
                arm_timeout_s=args.arm_timeout, simulated=args.simulate,
                measure=not args.no_measure, paused=tracking_paused,
            )
            measured = "" if args.no_measure else _ask("Protractor reading in deg (ENTER to skip) > ").strip()
            if measured:
                try:
                    value = float(measured.replace(",", "."))
                except ValueError:
                    print(f"'{measured}' is not a number, left blank.")
                else:
                    row["physical_angle_measured"] = value
                    detail["physical_angle_measured"] = value
                    target = row.get("servo_target_angle")
                    if isinstance(target, float) and abs(value - target) > MEASUREMENT_SANITY_DEG:
                        print(f"  CHECK: {value:.1f} is {abs(value - target):.1f} deg off the "
                              f"commanded {target:.1f}. Typo, or did the head really do that? "
                              "Use r to redo if it was a typo.")

            choice = _ask("[ENTER] keep  r = redo  q = quit > ").strip().lower()
            if choice == "q":
                break
            if choice == "r":
                print("Discarded, repeating this rep.")
                continue
            note = _ask("Optional note for this trial > ").strip()
            row["notes"] = note
            detail["notes"] = note
            append_row(csv_path, row)
            append_detail(jsonl_path, detail)
            kept += 1
            rep += 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        base_turntable.home(ramp_duration_s=0.4)
        time.sleep(0.5)
        if hasattr(base_turntable, "stop"):
            base_turntable.stop()
        if hasattr(base_doa, "close"):
            base_doa.close()
        if kept:
            print(f"\nSaved {kept} trial(s) to {csv_path}")
        else:
            print("\nNo trials kept - nothing written.")


if __name__ == "__main__":
    main()
