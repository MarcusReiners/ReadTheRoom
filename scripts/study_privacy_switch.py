import argparse
import csv
import hashlib
import json
import logging
import math
import os
import random
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup
from adapters.hardware.radar_ld2450 import zone_signed_distance
from domain.conversation import ConversationState
from domain.events import (
    DoorCleared,
    ModalitySwitched,
    PersonAtDoor,
    PersonEnteredRoom,
    PersonLeftRoom,
    RadarTargetsUpdated,
    RoomClearChanged,
    SpeechPlaybackEnded,
    SpeechPlaybackStarted,
    SpeechTranscribed,
    VoiceDucked,
)
from adapters.tts.base import StreamingTTSAdapter
from service_layer.bus import EventBus
from service_layer.handlers import register_handlers

STUDY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "study")
REPLY_AUDIO = os.path.join(STUDY_DIR, "private_reply.wav")
REPLY_META = os.path.join(STUDY_DIR, "private_reply.json")
FN_TARGET = 0.05
STAY_S = 15.0
WALK_S = 0.0
CLEAR_HOLD_S = 2.0
SPEECH_START_TIMEOUT_S = 20.0
ENTRY_WINDOW_S = 20.0
PEEK_WINDOW_S = 15.0
PASS_WINDOW_S = 15.0
REVERSION_TIMEOUT_S = 60.0
STUCK_PROMPT_S = 8.0
SPEED_WINDOW_S = 0.6

# should: the answer must move to the chat. should_duck: the voice must get
# quieter without a switch (a peek), must not change (a passer-by), or is not
# judged (entries - they pass the door zone on the way in, so a brief dip
# before the switch is expected either way).
SCRIPTS = {
    "A": {"name": "direct entry, stay", "kind": "entry", "should": True, "should_duck": None,
          "tell": "walk in at NORMAL pace, stay inside ~{stay:.0f} s, then leave", "sim_speed": 1100.0},
    "B": {"name": "passer-by, near", "kind": "pass", "should": False, "should_duck": False,
          "tell": "walk past the open door, close to it, WITHOUT stepping into the doorway", "sim_dist": 150.0},
    "C": {"name": "passer-by, far", "kind": "pass", "should": False, "should_duck": False,
          "tell": "walk past along the FAR line, WITHOUT entering", "sim_dist": 700.0},
    "D": {"name": "peek at the door", "kind": "peek", "should": False, "should_duck": True,
          "tell": "stop IN THE DOORWAY for ~3 s as if looking in - do NOT step into the room - then leave"},
    "E": {"name": "slow entry", "kind": "entry", "should": True, "should_duck": None,
          "tell": "walk in SLOWLY, stay inside ~{stay:.0f} s, then leave", "sim_speed": 500.0},
    "F": {"name": "fast entry", "kind": "entry", "should": True, "should_duck": None,
          "tell": "walk in FAST or jog in, stay inside ~{stay:.0f} s, then leave", "sim_speed": 2300.0},
}
DEFAULT_REPS = {"A": 30, "B": 20, "D": 18, "F": 25}

PRIVATE_REPLY = [
    "You have three new messages. ",
    "The first is from your bank, about your current account balance and a payment that is overdue. ",
    "The second is from the clinic, confirming your appointment on Thursday and the results of your blood test. ",
    "The third is from your landlord, about the rent increase that was discussed last week. ",
    "Would you like me to read the first message in full? ",
    "It says that your balance is below the agreed limit, and that a fee will be charged unless a transfer is made by Friday. ",
    "The message also lists the last five transactions on the account, including two card payments and a standing order. ",
    "The clinic message says that most values are within the normal range, but one value should be discussed with the doctor. ",
    "The landlord writes that the new amount will apply from the first of next month, and asks you to confirm by email. ",
    "Shall I draft a reply to any of these messages, or would you like to hear them again? ",
]

CSV_COLUMNS = [
    "trial_id", "session", "configuration", "script_id", "mover_id", "timestamp", "simulated",
    "speech_start_ts", "entry_detected_ts", "entry_via", "modality_switch_ts", "takeover_ts",
    "exit_detected_ts", "reversion_ts", "threshold_crossing_ts", "exit_crossing_ts",
    "ground_truth_should_switch", "switched", "classification",
    "switch_latency_s", "switch_latency_source", "audible_latency_s", "switch_latency_est_s",
    "entry_to_switch_ms", "switch_to_takeover_ms",
    "reverted", "reversion_latency_s", "reversion_latency_source", "exit_detection_lag_s",
    "output_latency_ms", "detect_depth_mm", "mover_speed_mms", "max_in_zone",
    "min_outside_dist_mm", "n_radar_frames", "zone_valid", "zone_mode",
    "door_zone", "door_detected_ts", "duck_ts", "unduck_ts", "door_to_duck_ms", "duck_hold_s",
    "should_duck", "ducked", "duck_correct", "door_outcome", "notes",
]
VIDEO_COLUMNS = ["trial_id", "script_id", "classification",
                 "reply_onset_s", "crossing_in_s", "audible_stop_s", "crossing_out_s", "notes"]


def iso(t):
    return "" if t is None else datetime.fromtimestamp(t).isoformat(timespec="milliseconds")


class EventLog:
    def __init__(self):
        self._lock = threading.Lock()
        self._items = []

    def clear(self):
        with self._lock:
            self._items = []

    def add(self, t, kind, **data):
        with self._lock:
            self._items.append({"t": t, "kind": kind, **data})

    def snapshot(self):
        with self._lock:
            return sorted(self._items, key=lambda e: e["t"])


def attach_recorder(bus, log):
    bus.subscribe(PersonEnteredRoom,
                  lambda e: log.add(e.timestamp.timestamp(), "enter", id=e.person_id, via=e.via,
                                    x=e.x_mm, y=e.y_mm))
    bus.subscribe(PersonLeftRoom,
                  lambda e: log.add(e.timestamp.timestamp(), "leave", id=e.person_id))
    bus.subscribe(ModalitySwitched,
                  lambda e: log.add(e.timestamp.timestamp(), "modality", to=e.to_modality, reason=e.reason))
    bus.subscribe(SpeechTranscribed,
                  lambda e: log.add(e.timestamp.timestamp(), "turn", source=e.source))
    bus.subscribe(SpeechPlaybackStarted,
                  lambda e: log.add(e.timestamp.timestamp(), "speech_start"))
    bus.subscribe(SpeechPlaybackEnded,
                  lambda e: log.add(e.timestamp.timestamp(), "speech_end", completed=e.completed))
    bus.subscribe(PersonAtDoor,
                  lambda e: log.add(e.timestamp.timestamp(), "door", id=e.person_id, x=e.x_mm, y=e.y_mm))
    bus.subscribe(DoorCleared,
                  lambda e: log.add(e.timestamp.timestamp(), "door_cleared", id=e.person_id, outcome=e.outcome))
    bus.subscribe(VoiceDucked,
                  lambda e: log.add(e.timestamp.timestamp(), "duck", active=e.active, reason=e.reason))
    bus.subscribe(RoomClearChanged,
                  lambda e: log.add(e.timestamp.timestamp(), "clear", clear=e.clear))
    bus.subscribe(RadarTargetsUpdated,
                  lambda e: log.add(e.timestamp.timestamp(), "frame", targets=[
                      {"id": t.get("id"), "x": t.get("x_mm"), "y": t.get("y_mm"),
                       "v": t.get("speed_mms"), "z": bool(t.get("in_zone", True))}
                      for t in e.targets]))


def wrap_takeover(tts, log):
    original = tts.request_takeover

    def recording_takeover():
        t = time.time()
        effective = original()
        log.add(t, "takeover", effective=bool(effective))
        return effective

    tts.request_takeover = recording_takeover
    return original


def first(events, kind, after, pred=None):
    for e in events:
        if e["kind"] == kind and e["t"] >= after and (pred is None or pred(e)):
            return e
    return None


def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        return "q"


def wait_for(log, kind, after, pred=None, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        e = first(log.snapshot(), kind, after, pred)
        if e is not None:
            return e
        time.sleep(0.05)
    return first(log.snapshot(), kind, after, pred)


def countdown(seconds, label):
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            break
        sys.stdout.write(f"\r  {label}: {left:4.1f} s ")
        sys.stdout.flush()
        time.sleep(0.1)
    sys.stdout.write("\r" + " " * 44 + "\r")
    sys.stdout.flush()


def wait_clear(guard, conversation, hold_s=CLEAR_HOLD_S, timeout_s=180.0):
    """No visitor inside, nobody at the door, normal volume and speech mode,
    for hold_s without interruption - so walking out to the start mark cannot
    leak a switch or a lowered voice into the trial. Speech no longer comes
    back on its own, so once the room counts as clear this resumes it the way
    the user would."""
    deadline = time.monotonic() + timeout_s
    since = None
    shown = None
    stuck_since = time.monotonic()
    while time.monotonic() < deadline:
        visitors, mode = guard.visitors, conversation.modality
        at_door, ducked = guard.people_at_door, guard.ducked
        if visitors == 0 and mode == "web":
            guard.resume_speech("resumed_study")
            continue
        if visitors > 0 and time.monotonic() - stuck_since >= STUCK_PROMPT_S:
            sys.stdout.write("\r" + " " * 72 + "\r")
            ask(f"  Still {visitors} visitor(s) counted - an exit was missed (it stays recorded in the last "
                "trial).\n  If you are outside and the room is empty, press ENTER to reset > ")
            guard.reset_room()
            stuck_since = time.monotonic()
            shown = None
            continue
        if visitors == 0:
            stuck_since = time.monotonic()
        if visitors == 0 and mode == "voice" and not at_door and not ducked:
            since = since or time.monotonic()
            if time.monotonic() - since >= hold_s:
                if shown:
                    sys.stdout.write("\r" + " " * 72 + "\r")
                    sys.stdout.flush()
                return True
        else:
            since = None
        line = (f"  waiting until nobody counts as a visitor (now {visitors}, {mode}"
                f"{', someone at the door' if at_door else ''}{', voice lowered' if ducked else ''})")
        if line != shown:
            sys.stdout.write("\r" + line.ljust(71))
            sys.stdout.flush()
            shown = line
        time.sleep(0.1)
    print("\n  never cleared - was an exit missed? Walk fully out of the zone and back, or restart.")
    return False


def mover_at_entry(frames, enter, zone):
    """Depth into the zone and ground speed of the entering track at the moment
    it was detected - depth over speed estimates how long ago it crossed."""
    upto = [f for f in frames if f["t"] <= enter["t"] + 0.05]
    track = []
    for f in reversed(upto):
        if enter["t"] - f["t"] > SPEED_WINDOW_S:
            break
        same = [t for t in f["targets"] if t["id"] == enter["id"]]
        if same:
            track.append((f["t"], same[0]["x"], same[0]["y"]))
    if not track:
        return None, None, None
    _, x, y = track[0]
    d = zone_signed_distance(x, y, zone)
    depth = -d if d is not None and d < 0 else (0.0 if d is not None else None)
    speed = None
    if len(track) >= 3:
        n = len(track)
        mt = sum(p[0] for p in track) / n
        stt = sum((p[0] - mt) ** 2 for p in track)
        if stt > 0:
            vx = sum((p[0] - mt) * p[1] for p in track) / stt
            vy = sum((p[0] - mt) * p[2] for p in track) / stt
            speed = math.hypot(vx, vy)
    est_s = depth / speed if depth is not None and speed and speed > 100 else None
    return depth, speed, est_s


def reversion(log, t_from, timeout_s):
    """When the room was judged clear again after t_from - the moment speech
    used to come back, and now the moment the user is offered to resume it."""
    voice = wait_for(log, "clear", t_from, lambda e: e["clear"], timeout_s)
    leave = None
    if voice is not None:
        leaves = [e for e in log.snapshot() if e["kind"] == "leave" and t_from <= e["t"] <= voice["t"]]
        leave = leaves[-1] if leaves else None
    return {
        "exit_detected_ts": iso(leave["t"]) if leave else "",
        "reversion_ts": iso(voice["t"]) if voice else "",
        "reverted": "Y" if voice else "N",
    }


def analyse(log, zone, t_arm):
    events = log.snapshot()
    frames = [e for e in events if e["kind"] == "frame" and e["t"] >= t_arm]
    enter = first(events, "enter", t_arm)
    switch = first(events, "modality", t_arm, lambda e: e["to"] == "web")
    take = first(events, "takeover", switch["t"] if switch else t_arm, lambda e: e["effective"])
    depth, speed, est_s = mover_at_entry(frames, enter, zone) if enter else (None, None, None)
    est_latency = round(switch["t"] - (enter["t"] - est_s), 3) if switch and enter and est_s is not None else ""
    door = first(events, "door", t_arm)
    duck = first(events, "duck", t_arm, lambda e: e["active"])
    unduck = first(events, "duck", duck["t"], lambda e: not e["active"]) if duck else None
    cleared = first(events, "door_cleared", door["t"], lambda e: e["id"] == door["id"]) if door else None
    return {
        "enter": enter, "switch": switch, "duck": duck,
        "door_detected_ts": iso(door["t"]) if door else "",
        "duck_ts": iso(duck["t"]) if duck else "",
        "unduck_ts": iso(unduck["t"]) if unduck else "",
        "door_to_duck_ms": "" if not (door and duck) else round((duck["t"] - door["t"]) * 1000, 1),
        "duck_hold_s": "" if not (duck and unduck) else round(unduck["t"] - duck["t"], 3),
        "ducked": "Y" if duck else "N",
        "door_outcome": cleared["outcome"] if cleared else ("still at door" if door else ""),
        "entry_detected_ts": iso(enter["t"]) if enter else "",
        "entry_via": enter["via"] if enter else "",
        "modality_switch_ts": iso(switch["t"]) if switch else "",
        "takeover_ts": iso(take["t"]) if take else "",
        "entry_to_switch_ms": "" if not (enter and switch) else round((switch["t"] - enter["t"]) * 1000, 1),
        "switch_to_takeover_ms": "" if not (switch and take) else round((take["t"] - switch["t"]) * 1000, 1),
        "switch_latency_est_s": est_latency,
        "detect_depth_mm": "" if depth is None else round(depth, 1),
        "mover_speed_mms": "" if speed is None else round(speed, 1),
        "max_in_zone": max([sum(1 for t in f["targets"] if t["z"]) for f in frames] + [0]),
        "n_radar_frames": len(frames),
    }


def run_trial(ctx, script_id):
    sc = SCRIPTS[script_id]
    log, zone, sim, stay, bus = ctx["log"], ctx["zone"], ctx["sim"], ctx["stay_s"], ctx["bus"]
    print(f"  You: {sc['tell'].format(stay=stay)}.")
    if ctx["walk_s"] > 0:
        prompt = "  ENTER, then go to the start mark. The reply starts once the zone is clear (q to quit) > "
    else:
        prompt = "  At the start mark? ENTER to start the reply (q to quit) > "
    if ask(prompt).strip().lower() == "q":
        raise KeyboardInterrupt
    if not sim and ctx["walk_s"] > 0:
        countdown(ctx["walk_s"], "walk to the start mark")
    if not wait_clear(ctx["guard"], ctx["conversation"]):
        return None
    log.clear()
    t_arm = time.time()
    bus.publish(SpeechTranscribed(text="(study: simulated conversation turn)",
                                  conversation_id="study", source="voice"))
    speaker = threading.Thread(target=ctx["tts"].speak_stream, args=(iter(PRIVATE_REPLY),), daemon=True)
    speaker.start()
    start = wait_for(log, "speech_start", t_arm, timeout_s=SPEECH_START_TIMEOUT_S)
    if start is None:
        print("  the assistant never started speaking - check the TTS provider.")
        ctx["stop_speech"]()
        return None
    print("  SPEAKING - GO.")

    rev = {}
    if sc["kind"] == "entry":
        if sim:
            sim.enter(sc.get("sim_speed", 1100.0))
        switch = wait_for(log, "modality", t_arm, lambda e: e["to"] == "web", ENTRY_WINDOW_S)
        if switch is not None:
            print("  SWITCHED - stay inside.")
            countdown(stay, "stay inside")
            early = first(log.snapshot(), "clear", switch["t"], lambda e: e["clear"])
            print("  >>> LEAVE NOW <<<")
            if sim:
                sim.leave()
            if early is not None:
                rev = {"reversion_ts": iso(early["t"]), "reverted": "early"}
            else:
                rev = reversion(log, time.time(), REVERSION_TIMEOUT_S)
        else:
            print(f"  no switch within {ENTRY_WINDOW_S:.0f} s - leave the zone.")
    elif sc["kind"] == "peek":
        if sim:
            sim.peek()
        switch = wait_for(log, "modality", t_arm, lambda e: e["to"] == "web", PEEK_WINDOW_S)
        if switch is not None:
            print("  SWITCHED - step back out if you have not already.")
            rev = reversion(log, switch["t"], REVERSION_TIMEOUT_S)
        else:
            duck = first(log.snapshot(), "duck", t_arm, lambda e: e["active"])
            if duck is not None:
                wait_for(log, "duck", duck["t"], lambda e: not e["active"], REVERSION_TIMEOUT_S)
    else:
        if sim:
            sim.passby(sc.get("sim_dist", 150.0))
        countdown(PASS_WINDOW_S, "watching the pass")
        switch = first(log.snapshot(), "modality", t_arm, lambda e: e["to"] == "web")
        if switch is not None:
            rev = reversion(log, switch["t"], REVERSION_TIMEOUT_S)
    ctx["stop_speech"]()
    speaker.join(timeout=3.0)

    a = analyse(log, zone, t_arm)
    switched = a["switch"] is not None
    should = sc["should"]
    should_duck = sc["should_duck"]
    if sc["kind"] == "peek":
        stepped = ask("  Did you step past the doorway into the room after all? [y/N] > ").strip().lower()
        if stepped in ("y", "yes"):
            should, should_duck = True, None
    row = {"script_id": script_id, "speech_start_ts": iso(start["t"]),
           "door_zone": "Y" if ctx["door_zone"].get("valid") else "N",
           "should_duck": "" if should_duck is None else ("Y" if should_duck else "N")}
    if should_duck is not None:
        row["duck_correct"] = "Y" if (a["duck"] is not None) == should_duck else "N"
    if sc["kind"] == "pass":
        frames = [e for e in log.snapshot() if e["kind"] == "frame" and e["t"] >= t_arm]
        outside = [zone_signed_distance(t["x"], t["y"], zone) for f in frames for t in f["targets"]]
        outside = [d for d in outside if d is not None and d > 0]
        row["min_outside_dist_mm"] = round(min(outside), 1) if outside else ""

    cls = ("TP" if switched else "FN") if should else ("FP" if switched else "TN")
    row.update({k: v for k, v in a.items() if k not in ("enter", "switch", "duck")})
    row.update(rev)
    row.update({"ground_truth_should_switch": "Y" if should else "N",
                "switched": "Y" if switched else "N", "classification": cls})
    if cls == "TP" and row.get("switch_latency_est_s", "") != "":
        row["switch_latency_s"] = row["switch_latency_est_s"]
        row["switch_latency_source"] = "radar_estimate"

    print(f"  {cls}", end="")
    if cls == "TP":
        print(f"  entry ({row['entry_via']}) -> switch {row['entry_to_switch_ms']} ms, "
              f"switch -> audio killed {row['switch_to_takeover_ms']} ms, "
              f"est. crossing -> switch {row['switch_latency_est_s']} s")
        if row.get("reverted") == "Y":
            print("      room judged clear as soon as you were tracked leaving")
        elif row.get("reverted") == "early":
            print("      room judged clear while you were still inside - a premature exit (logged)")
        elif row.get("reverted") == "N":
            print("      your exit was not seen - the room still counts a visitor.")
    elif cls == "FN":
        print("  - MISSED: you entered but the assistant kept talking.")
    elif cls == "FP":
        print("  - SPURIOUS: it switched although you did not enter.")
    elif sc["kind"] == "peek":
        if a["duck"] is not None:
            print(f"  - correct: no switch, voice quieter {row['door_to_duck_ms']} ms after you were seen "
                  f"at the door, normal again {row['duck_hold_s'] or '?'} s later.")
        elif row.get("door_detected_ts"):
            print("  - no switch, but the voice did NOT get quieter although you were seen at the door.")
        else:
            print("  - no switch, but the radar never saw you at the door - is the door zone over the doorway?")
    else:
        print(f"  - correctly stayed in voice. Closest approach: {row.get('min_outside_dist_mm') or '?'} mm"
              + ("   (but the voice got quieter)" if a["duck"] is not None else ""))
    return row, {"t_arm": t_arm, "events": log.snapshot()}


def epoch(ts):
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def fnum(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def load_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def real(rows):
    return [r for r in rows if (r.get("simulated") or "0") != "1" or os.environ.get("STUDY_INCLUDE_SIM")]


def write_rows(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_COLUMNS})
    os.replace(tmp, path)


def write_video_template(csv_path, video_path):
    rows = real(load_rows(csv_path))
    if not rows:
        print(f"No trials in {csv_path}")
        return
    existing = {r["trial_id"]: r for r in load_rows(video_path)}
    with open(video_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=VIDEO_COLUMNS)
        w.writeheader()
        for r in rows:
            keep = existing.get(r["trial_id"], {})
            w.writerow({"trial_id": r["trial_id"], "script_id": r["script_id"],
                        "classification": r["classification"],
                        **{k: keep.get(k, "") for k in VIDEO_COLUMNS[3:]}})
    print(f"Wrote {len(rows)} trials to {video_path}")
    print("Fill in, per trial, times read off the continuous video (any clock, in seconds):")
    print("  reply_onset_s   first audible word of the private reply  (the sync marker)")
    print("  crossing_in_s   foot crosses the zone-edge tape inward   (entries, peeks)")
    print("  audible_stop_s  the voice stops                          (true positives)")
    print("  crossing_out_s  foot crosses the tape outward            (entries, peeks)")
    print("Then run with --import-video.")


def import_video(csv_path, video_path):
    rows = load_rows(csv_path)
    video = {r["trial_id"]: r for r in load_rows(video_path)}
    lat = []
    for r in rows:
        v = video.get(r["trial_id"])
        if not v:
            continue
        onset, stop = fnum(v.get("reply_onset_s")), fnum(v.get("audible_stop_s"))
        start, take = epoch(r.get("speech_start_ts")), epoch(r.get("takeover_ts"))
        if None not in (onset, stop, start, take):
            lat.append((take - start) - (stop - onset))
    output_latency = sorted(lat)[len(lat) // 2] if lat else 0.0
    updated = 0
    for r in rows:
        v = video.get(r["trial_id"])
        onset = fnum(v.get("reply_onset_s")) if v else None
        start = epoch(r.get("speech_start_ts"))
        if onset is None or start is None:
            continue

        def pi_time(video_s):
            return None if video_s is None else start + output_latency + (video_s - onset)

        cin, cout, stop = (fnum(v.get(k)) for k in ("crossing_in_s", "crossing_out_s", "audible_stop_s"))
        t_in, t_out = pi_time(cin), pi_time(cout)
        switch, voice, left = (epoch(r.get(k)) for k in ("modality_switch_ts", "reversion_ts", "exit_detected_ts"))
        r["output_latency_ms"] = round(output_latency * 1000, 1)
        if t_in is not None:
            r["threshold_crossing_ts"] = iso(t_in)
            if switch is not None and r.get("classification") == "TP":
                r["switch_latency_s"] = round(switch - t_in, 3)
                r["switch_latency_source"] = "video"
            if stop is not None and cin is not None:
                r["audible_latency_s"] = round(stop - cin, 3)
        if t_out is not None:
            r["exit_crossing_ts"] = iso(t_out)
            if voice is not None:
                r["reversion_latency_s"] = round(voice - t_out, 3)
                r["reversion_latency_source"] = "video"
            if left is not None:
                r["exit_detection_lag_s"] = round(left - t_out, 3)
        updated += 1
    write_rows(csv_path, rows)
    print(f"Merged video timings into {updated} trial(s).")
    print(f"Output latency (reply logged -> audible), median over {len(lat)} trials: "
          f"{1000 * output_latency:.0f} ms - applied when mapping video times onto the Pi clock.")


def list_trials(csv_path):
    rows = load_rows(csv_path)
    if not rows:
        print(f"No trials in {csv_path}")
        return
    print(f"\n{csv_path}")
    for r in rows:
        print(f"  #{r.get('trial_id'):>3}  {r.get('configuration', ''):10} {r.get('script_id')} "
              f"{SCRIPTS.get(r.get('script_id'), {}).get('name', ''):20} {r.get('classification', ''):3} "
              f"back to voice: {r.get('reverted') or '-':2} quieter: {r.get('ducked') or '-':2} "
              f"{repr(r.get('notes'))[:40] if r.get('notes') else ''}")
    print()


def drop_trials(csv_path, jsonl_path, spec):
    ids = {s.strip() for s in spec.split(",") if s.strip()}
    rows = load_rows(csv_path)
    keep = [r for r in rows if r.get("trial_id") not in ids]
    gone = [r.get("trial_id") for r in rows if r.get("trial_id") in ids]
    if not gone:
        print(f"No trial with id {', '.join(sorted(ids))} in {csv_path}")
        return
    write_rows(csv_path, keep)
    if os.path.exists(jsonl_path):
        with open(jsonl_path) as f:
            lines = [ln for ln in f if ln.strip() and str(json.loads(ln).get("trial_id")) not in ids]
        with open(jsonl_path, "w") as f:
            f.writelines(lines)
    print(f"Removed trial(s) {', '.join(gone)} - {len(keep)} left in {csv_path}")


def parse_reps(spec):
    if not spec:
        return dict(DEFAULT_REPS)
    out = {}
    for part in spec.split(","):
        k, _, v = part.partition("=")
        k = k.strip().upper()
        if k not in SCRIPTS or not v.strip().isdigit():
            raise SystemExit(f"bad --reps entry {part!r}; use e.g. A=30,B=20,D=18,F=25")
        out[k] = int(v)
    return out


def done_counts(rows, configuration):
    counts = {}
    for r in real(rows):
        if r.get("configuration") == configuration:
            counts[r["script_id"]] = counts.get(r["script_id"], 0) + 1
    return counts


def next_script(reps, counts, rng, fixed_order=False):
    if fixed_order:
        return next((k for k, n in reps.items() if counts.get(k, 0) < n), None)
    bag = [k for k, n in reps.items() for _ in range(max(0, n - counts.get(k, 0)))]
    return rng.choice(bag) if bag else None


def next_trial_id(rows):
    ids = [int(r["trial_id"]) for r in rows if (r.get("trial_id") or "").isdigit()]
    return max(ids) + 1 if ids else 1


def append(path, row):
    """A sheet written before the door-zone columns existed is rewritten with
    the current columns first; appending blindly would shift every value
    after the old last column."""
    new = not os.path.exists(path)
    if not new:
        with open(path, newline="") as f:
            header = next(csv.reader(f), [])
        if header != CSV_COLUMNS:
            write_rows(path, load_rows(path) + [row])
            return
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_COLUMNS})


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

    lower = 0.0 if k == 0 else solve(1 - alpha / 2, k - 1)
    upper = 1.0 if k == n else solve(alpha / 2, k)
    return lower, upper


def wilson(k, n, z=1.96):
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def rate_line(label, k, n):
    if n == 0:
        print(f"  {label:22} no trials")
        return None
    cl, cu = clopper_pearson(k, n)
    wl, wu = wilson(k, n)
    print(f"  {label:22} {k}/{n} = {100 * k / n:5.1f}%   "
          f"Clopper-Pearson [{100 * cl:4.1f}, {100 * cu:4.1f}]%   Wilson [{100 * wl:4.1f}, {100 * wu:4.1f}]%")
    return max(cu, wu)


def nums(rows, key):
    out = []
    for r in rows:
        v = fnum(r.get(key))
        if v is not None:
            out.append(v)
    return out


def pct(vals, q):
    s = sorted(vals)
    if not s:
        return float("nan")
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def dist_line(label, vals, unit):
    if not vals:
        print(f"  {label:38} no data")
        return
    print(f"  {label:38} n={len(vals):<3} median {pct(vals, .5):7.3f}  p95 {pct(vals, .95):7.3f}  "
          f"max {max(vals):7.3f} {unit}")


def print_report(path):
    rows = real(load_rows(path))
    if not rows:
        print(f"No data at {path}")
        return
    print(f"\n{path}")
    for conf in sorted({r.get("configuration", "") for r in rows}):
        rs = [r for r in rows if r.get("configuration", "") == conf]
        cls = {c: sum(1 for r in rs if r.get("classification") == c) for c in ("TP", "FN", "FP", "TN")}
        print(f"\n=== configuration: {conf or '(unnamed)'}  ({len(rs)} trials) ===")
        print(f"  TP {cls['TP']}   FN {cls['FN']}   FP {cls['FP']}   TN {cls['TN']}")
        fn_upper = rate_line("false-negative rate", cls["FN"], cls["TP"] + cls["FN"])
        rate_line("false-positive rate", cls["FP"], cls["FP"] + cls["TN"])
        if fn_upper is not None:
            if fn_upper < FN_TARGET:
                print(f"  -> FN target <{100 * FN_TARGET:.0f}% MET: both 95% upper bounds are below it.")
            else:
                print(f"  -> FN target <{100 * FN_TARGET:.0f}% NOT DEMONSTRATED: upper bound "
                      f"{100 * fn_upper:.1f}% with {cls['TP'] + cls['FN']} entry trials "
                      f"(zero misses in 73 entries are needed).")

        tp = [r for r in rs if r.get("classification") == "TP"]
        print("\n  switch latency, true positives only")
        vid = [r for r in tp if r.get("switch_latency_source") == "video"]
        if vid:
            dist_line("crossing -> switch (video, plan 2.8)", nums(vid, "switch_latency_s"), "s")
            dist_line("crossing -> audible stop (video)", nums(vid, "audible_latency_s"), "s")
        else:
            print("  (no video timings imported yet - figures below are radar estimates)")
        dist_line("crossing -> switch (radar estimate)", nums(tp, "switch_latency_est_s"), "s")
        dist_line("entry detected -> switch (software)", [v / 1000 for v in nums(tp, "entry_to_switch_ms")], "s")
        dist_line("switch -> audio killed (software)", [v / 1000 for v in nums(tp, "switch_to_takeover_ms")], "s")
        vias = {}
        for r in tp:
            vias[r.get("entry_via") or "?"] = vias.get(r.get("entry_via") or "?", 0) + 1
        if vias:
            print("  entries detected by: " + ", ".join(f"{k} {n}" for k, n in sorted(vias.items())))

        rev = [r for r in tp if r.get("reverted") in ("Y", "N", "early")]
        print("\n  exit after leaving (room judged clear; Study 2 sessions: speech came back)")
        if rev:
            for label, value in (("after the mover left", "Y"), ("before the mover left", "early"),
                                 ("not at all", "N")):
                print(f"  {label:38} {sum(1 for r in rev if r['reverted'] == value)}/{len(rev)}")
            print("  (sessions recorded before the 'early' mark count premature ones as not at all - "
                  "analyse_privacy_switch.py reads the event log instead)")
            vrev = [r for r in rev if r.get("reversion_latency_source") == "video"]
            if vrev:
                dist_line("exit crossing -> voice (video)", nums(vrev, "reversion_latency_s"), "s")
                dist_line("  exit crossing -> exit detected", nums(vrev, "exit_detection_lag_s"), "s")
            else:
                print("  (no video timings imported yet - exit crossing times come from the footage)")
        else:
            print("  no reversion data")

        judged = [r for r in rs if r.get("should_duck") in ("Y", "N")]
        if judged or any(r.get("ducked") == "Y" for r in rs):
            print("\n  quieter voice (door zone)")
            peeks = [r for r in judged if r["should_duck"] == "Y"]
            passes = [r for r in judged if r["should_duck"] == "N"]
            rate_line("peeks: quieter", sum(1 for r in peeks if r.get("ducked") == "Y"), len(peeks))
            rate_line("peeks: switched", sum(1 for r in peeks if r.get("switched") == "Y"), len(peeks))
            rate_line("passers-by: quieter", sum(1 for r in passes if r.get("ducked") == "Y"), len(passes))
            dist_line("seen at door -> quieter (software)",
                      [v / 1000 for v in nums(rs, "door_to_duck_ms")], "s")
            dist_line("quieter -> normal again (peeks)", nums(peeks, "duck_hold_s"), "s")
            door_first = sum(1 for r in tp if r.get("entry_via") == "door")
            if tp:
                print(f"  {'entries first seen at the door':38} {door_first}/{len(tp)}")

        print("\n  per script")
        for sid in sorted({r["script_id"] for r in rs}):
            s_rows = [r for r in rs if r["script_id"] == sid]
            c = {k: sum(1 for r in s_rows if r.get("classification") == k) for k in ("TP", "FN", "FP", "TN")}
            ducks = sum(1 for r in s_rows if r.get("ducked") == "Y")
            print(f"    {sid} {SCRIPTS[sid]['name']:20} n={len(s_rows):<3} "
                  f"TP {c['TP']:>2}  FN {c['FN']:>2}  FP {c['FP']:>2}  TN {c['TN']:>2}  quieter {ducks:>2}")
    print()


def print_progress(path, configuration, reps):
    counts = done_counts(load_rows(path), configuration)
    print(f"\n{path}  (configuration: {configuration})")
    for k, n in reps.items():
        d = counts.get(k, 0)
        print(f"  {k} {SCRIPTS[k]['name']:20} {d:>3}/{n:<3} {'complete' if d >= n else f'{n - d} to go'}")
    got = sum(min(counts.get(k, 0), n) for k, n in reps.items())
    entries = sum(counts.get(k, 0) for k in reps if SCRIPTS[k]["should"])
    print(f"  {got}/{sum(reps.values())} trials, {entries} entry trials so far\n")


class SimScene:
    """Rehearsal only: a single walker whose frames go through the REAL
    adapter's crossing detection, starting outside the zone."""

    ZONE = {"valid": True, "min_x_mm": -800, "max_x_mm": 800,
            "min_y_mm": 500, "max_y_mm": 2500, "mode": 0}
    DOOR = {"min_x_mm": -1300, "max_x_mm": -800, "min_y_mm": 1200, "max_y_mm": 1800}

    def __init__(self, radar):
        self.radar = radar
        self.pos = None
        self.vel = (0.0, 0.0)
        self.goal = None
        self.dwell_until = None
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def enter(self, speed):
        with self._lock:
            self.pos, self.vel, self.goal, self.dwell_until = [-1400.0, 1500.0], (speed, 0.0), ("x", -300.0), None

    def peek(self):
        with self._lock:
            self.pos, self.vel, self.goal = [-1400.0, 1500.0], (1100.0, 0.0), ("x", -1000.0)
            self.dwell_until = "pending"

    def leave(self):
        with self._lock:
            self.vel, self.goal, self.dwell_until = (-1100.0, 0.0), ("x", -2600.0), None

    def passby(self, dist_mm):
        with self._lock:
            self.pos, self.vel, self.goal = [-1300.0 - dist_mm, 0.0], (0.0, 1200.0), ("y", 3200.0)
            self.dwell_until = None

    def _run(self):
        dt = 0.1
        while True:
            with self._lock:
                targets = []
                if self.pos is not None:
                    if isinstance(self.dwell_until, float) and time.monotonic() >= self.dwell_until:
                        self.vel, self.goal, self.dwell_until = (-1100.0, 0.0), ("x", -2600.0), None
                    self.pos[0] += self.vel[0] * dt
                    self.pos[1] += self.vel[1] * dt
                    axis, limit = self.goal
                    i = 0 if axis == "x" else 1
                    v = self.vel[i]
                    if (v > 0 and self.pos[i] >= limit) or (v < 0 and self.pos[i] <= limit):
                        self.pos[i] = limit
                        self.vel = (0.0, 0.0)
                        if self.dwell_until == "pending":
                            self.dwell_until = time.monotonic() + 3.0
                        if limit in (-2600.0, 3200.0):
                            self.pos = None
                    if self.pos is not None:
                        x = self.pos[0] + random.gauss(0, 40)
                        y = self.pos[1] + random.gauss(0, 40)
                        r = math.hypot(*self.pos) or 1.0
                        radial = (self.pos[0] * self.vel[0] + self.pos[1] * self.vel[1]) / r
                        targets.append({"id": 2, "x_mm": x, "y_mm": y, "speed_mms": int(radial),
                                        "in_zone": zone_signed_distance(x, y, self.ZONE) <= 0})
            self.radar._handle_status({"targets": targets, "zone": self.ZONE})
            time.sleep(dt)


class FileTTS(StreamingTTSAdapter):
    """Plays the pre-rendered private message (study/private_reply.wav) through
    the assistant's normal audio path - same buffering, volume, quieter voice
    and takeover as the live voice. Identical in every trial, and it spends no
    TTS credits: 93 trials of the full message would need ~90,000 characters."""

    def __init__(self, bus, path, speaker_device):
        super().__init__(bus=bus, speaker_device=speaker_device)
        with wave.open(path, "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise SystemExit(f"{path} must be 16-bit mono PCM")
            self._rate = w.getframerate()
            self._pcm = w.readframes(w.getnframes())
        self.duration_s = len(self._pcm) / 2 / self._rate
        self._played = False

    @property
    def sample_rate(self):
        return self._rate

    def speak_stream(self, text_chunks):
        self._played = False
        return super().speak_stream(text_chunks)

    def _synthesize_chunks(self, sentence):
        if self._played:
            return
        self._played = True
        for i in range(0, len(self._pcm), 8192):
            yield self._pcm[i:i + 8192]


class SimTTS:
    def __init__(self, bus):
        self.bus = bus
        self._streaming = False
        self._takeover = threading.Event()

    def speak_stream(self, chunks):
        self._streaming = True
        self._takeover.clear()
        self.bus.publish(SpeechPlaybackStarted(text="(simulated)"))
        completed = True
        for sentence in chunks:
            if self._takeover.wait(len(sentence) / 14.0):
                completed = False
                break
        self._streaming = False
        self.bus.publish(SpeechPlaybackEnded(completed=completed))
        return ""

    def request_takeover(self):
        if not self._streaming:
            return False
        self._takeover.set()
        return True

    def set_duck(self, active):
        pass


def quiet_console():
    """Keeps the terminal to the prompts; the log file still gets everything."""
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(logging.WARNING)


def load_voice_settings():
    from adapters.app_settings import load_app_settings
    return load_app_settings(config.APP_SETTINGS_PATH, defaults={
        "voices": [{"id": "default", "voice_id": config.ELEVENLABS_VOICE_ID}],
        "active_voice_id": "default", "volume": 1.0})


def active_voice(settings):
    return next((v for v in settings["voices"] if v["id"] == settings["active_voice_id"]), settings["voices"][0])


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def code_version():
    root = os.path.dirname(STUDY_DIR)
    git = ["git", "-c", "safe.directory=*", "-C", root]
    try:
        commit = subprocess.run(git + ["rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                timeout=5).stdout.strip()
        status = subprocess.run(git + ["status", "--porcelain", "--untracked-files=no"], capture_output=True,
                                text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return {"commit": commit, "uncommitted": [line[3:] for line in status.splitlines()]} if commit else {}


def render_recording(path):
    """Speaks PRIVATE_REPLY once, sentence by sentence, through the same TTS
    adapter and active voice a live session uses, and saves it for
    --recording, with the voice and model next to it in REPLY_META."""
    from adapters.factory import build_tts
    voice = active_voice(load_voice_settings())
    tts = build_tts(config, EventBus())
    if hasattr(tts, "voice_id"):
        tts.voice_id = voice["voice_id"]
    print(f"Rendering {len(PRIVATE_REPLY)} sentences with {config.TTS_PROVIDER}, "
          f"voice {voice.get('name') or voice['voice_id']} ...")
    pcm = b"".join(chunk for sentence in PRIVATE_REPLY for chunk in tts._synthesize_chunks(sentence.strip()))
    pcm = pcm[:len(pcm) - len(pcm) % 2]
    tmp = path + ".part"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(tts.sample_rate)
        w.writeframes(pcm)
    os.replace(tmp, path)
    meta = {"provider": config.TTS_PROVIDER, "voice_name": voice.get("name", ""), "voice_id": voice["voice_id"],
            "model_id": getattr(tts, "model_id", ""), "language_code": getattr(tts, "language_code", None),
            "sample_rate": tts.sample_rate, "duration_s": round(len(pcm) / 2 / tts.sample_rate, 3),
            "sha256": file_sha256(path), "rendered_at": iso(time.time()), "text": "".join(PRIVATE_REPLY).strip()}
    with open(REPLY_META, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"Wrote {path} ({meta['duration_s']} s) and {REPLY_META}")


def main():
    parser = argparse.ArgumentParser(description="Study 2: privacy-switch reliability.")
    parser.add_argument("--configuration", default="baseline", help="placement label, e.g. baseline or doorway")
    parser.add_argument("--mover", default="M1", help="mover id for the data sheet")
    parser.add_argument("--reps", default="", help="trials per script, e.g. A=30,B=20,D=18,F=25")
    parser.add_argument("--session", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--seed", type=int, default=None, help="randomisation seed (logged per trial)")
    parser.add_argument("--stay", type=float, default=STAY_S, help="seconds you stay inside (A/E/F)")
    parser.add_argument("--walk", type=float, default=WALK_S, help="seconds to reach the start mark")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--list", action="store_true", help="list the trials saved in this session")
    parser.add_argument("--drop", default="", help="remove trials by id from the sheet and log, e.g. 2 or 2,5")
    parser.add_argument("--video-template", action="store_true",
                        help="write the sheet for video timings, prefilled with the session's trials")
    parser.add_argument("--import-video", action="store_true", help="merge the filled video sheet")
    parser.add_argument("--simulate", action="store_true", help="no radar or speaker, for rehearsal")
    parser.add_argument("--fixed-order", action="store_true",
                        help="run the scripts in the order given in --reps instead of randomly (demo takes)")
    parser.add_argument("--recording", action="store_true",
                        help="play study/private_reply.wav instead of speaking the message with the live TTS "
                             "provider - identical in every trial, no TTS credits; keep one choice per session")
    parser.add_argument("--render-recording", action="store_true",
                        help="synthesise the message once with the live TTS provider and the active voice into "
                             "study/private_reply.wav (plus private_reply.json), for --recording")
    parser.add_argument("--verbose", action="store_true", help="show the full event log in the terminal too")
    args = parser.parse_args()

    reps = parse_reps(args.reps)
    os.makedirs(STUDY_DIR, exist_ok=True)
    csv_path = os.path.join(STUDY_DIR, f"privacy_switch_{args.session}.csv")
    jsonl_path = os.path.join(STUDY_DIR, f"privacy_switch_{args.session}_trials.jsonl")
    video_path = os.path.join(STUDY_DIR, f"privacy_switch_{args.session}_video.csv")
    discarded_path = os.path.join(STUDY_DIR, f"privacy_switch_{args.session}_discarded.jsonl")
    if args.render_recording:
        render_recording(REPLY_AUDIO)
        return
    if args.list:
        list_trials(csv_path)
        return
    if args.drop:
        drop_trials(csv_path, jsonl_path, args.drop)
        return
    if args.progress:
        print_progress(csv_path, args.configuration, reps)
        return
    if args.report:
        print_report(csv_path)
        return
    if args.video_template:
        write_video_template(csv_path, video_path)
        return
    if args.import_video:
        import_video(csv_path, video_path)
        return

    logging_setup.configure_logging(config)
    if not args.verbose:
        quiet_console()
    bus = EventBus()
    log = EventLog()
    attach_recorder(bus, log)
    conversation = ConversationState()

    from adapters.hardware import radar_ld2450
    from adapters.hardware.radar_ld2450 import RadarLD2450Adapter
    sim = None
    if args.simulate:
        radar = RadarLD2450Adapter(bus=bus, serial_port="/dev/null",
                                   entry_margin_mm=config.RADAR_ENTRY_MARGIN_MM,
                                   exit_margin_mm=config.RADAR_EXIT_MARGIN_MM,
                                   door_near_mm=config.RADAR_DOOR_NEAR_MM)
        radar.set_door_zone(SimScene.DOOR)
        tts = SimTTS(bus)
        reply = {"source": "simulated"}
    else:
        if config.RADAR_PROVIDER != "ld2450":
            raise SystemExit(f"RADAR_PROVIDER is {config.RADAR_PROVIDER!r} - this study needs the LD2450.")
        from adapters.factory import build_radar, build_tts
        settings = load_voice_settings()
        if args.recording:
            if not os.path.exists(REPLY_AUDIO):
                raise SystemExit(f"--recording needs {REPLY_AUDIO} - create it with --render-recording")
            tts = FileTTS(bus, REPLY_AUDIO, config.SPEAKER_DEVICE)
            reply = {"source": "recording", "sha256": file_sha256(REPLY_AUDIO), "duration_s": round(tts.duration_s, 3)}
            if os.path.exists(REPLY_META):
                with open(REPLY_META) as f:
                    meta = json.load(f)
                reply.update({k: meta.get(k) for k in ("voice_name", "voice_id", "model_id", "rendered_at")})
                if meta.get("sha256") != reply["sha256"]:
                    print(f"Warning: {REPLY_AUDIO} is not the file described in {REPLY_META}.")
        else:
            tts = build_tts(config, bus)
            voice = active_voice(settings)
            if hasattr(tts, "voice_id"):
                tts.voice_id = voice["voice_id"]
            reply = {"source": "live", "provider": config.TTS_PROVIDER, "voice_id": getattr(tts, "voice_id", "")}
        if hasattr(tts, "set_volume"):
            tts.set_volume(settings.get("volume", 1.0))
        if hasattr(tts, "duck_gain"):
            tts.duck_gain = config.TTS_DUCK_GAIN
        radar = build_radar(config, bus)

    original_takeover = wrap_takeover(tts, log)
    guard = register_handlers(bus, conversation, tts, departure_grace_s=config.PRIVACY_DEPARTURE_GRACE_S,
                              door_clear_hold_s=config.PRIVACY_DOOR_CLEAR_HOLD_S)
    if args.simulate:
        sim = SimScene(radar)
    else:
        radar.start()

    print("Waiting for radar frames...")
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and not any(e["kind"] == "frame" for e in log.snapshot()):
        time.sleep(0.1)
    if not any(e["kind"] == "frame" for e in log.snapshot()):
        raise SystemExit("No radar frames arrived - is the bridge plugged in and the main app stopped?")
    zone = radar.get_zone()
    if not zone.get("valid"):
        raise SystemExit("No zone is configured - draw it in the web app first.")
    if zone.get("mode", 0) not in (0, None):
        raise SystemExit("The zone is enforced by the sensor, so people outside it are invisible and no "
                         "entry can ever be seen. Set 'Enforced by' to Software in the web app.")

    seed = args.seed if args.seed is not None else random.randrange(1_000_000)
    rng = random.Random(seed)
    door_zone = radar.get_door_zone()
    ctx = {"log": log, "radar": radar, "conversation": conversation, "tts": tts, "zone": zone, "bus": bus,
           "sim": sim, "guard": guard, "stop_speech": original_takeover, "stay_s": args.stay, "walk_s": args.walk,
           "door_zone": door_zone}
    session_info = {"reply": reply, "exit_speed_mms": getattr(radar_ld2450, "EXIT_SPEED_MMS", None),
                    "stay_s": args.stay, "code": code_version()}
    counts = done_counts(load_rows(csv_path), args.configuration)
    entries_planned = sum(n for k, n in reps.items() if SCRIPTS[k]["should"])
    _, best_upper = clopper_pearson(0, entries_planned)

    print(f"\nSession {args.session}  configuration={args.configuration}  mover={args.mover}  seed={seed}")
    print(f"Data sheet : {csv_path}")
    print(f"Trial log  : {jsonl_path}")
    print("Scripts    : " + ", ".join(f"{k}x{n}" for k, n in reps.items()) + "  (order randomised)")
    print(f"Entries    : {entries_planned} planned - with zero misses the FN upper bound would be "
          f"{100 * best_upper:.1f}%")
    print(f"Zone       : x {zone['min_x_mm']}..{zone['max_x_mm']} mm, y {zone['min_y_mm']}..{zone['max_y_mm']} mm")
    print(f"Edge band  : in at {config.RADAR_ENTRY_MARGIN_MM:.0f} mm inside, out at "
          f"{config.RADAR_EXIT_MARGIN_MM:.0f} mm outside the zone edge")
    if isinstance(tts, FileTTS):
        print(f"Voice      : recording {os.path.relpath(REPLY_AUDIO)} ({tts.duration_s:.0f} s, "
              f"{reply.get('voice_name') or 'voice not recorded'}), identical in every trial")
    elif not args.simulate:
        print(f"Voice      : live {config.TTS_PROVIDER} TTS")
    exit_speed = session_info["exit_speed_mms"]
    print("Exit rule  : " + (f"an exit needs {exit_speed:.0f} mm/s away from the sensor" if exit_speed
                             else "no speed condition (radar code older than the Study 2 fix)"))
    code = session_info["code"]
    print("Code       : " + (f"commit {code['commit']}" + (f", uncommitted changes in {', '.join(code['uncommitted'])}"
                                                           if code["uncommitted"] else "")
                             if code else "git version unknown"))
    if door_zone.get("valid"):
        print(f"Door zone  : x {door_zone['min_x_mm']}..{door_zone['max_x_mm']} mm, "
              f"y {door_zone['min_y_mm']}..{door_zone['max_y_mm']} mm - entries decided there, "
              f"a peek makes the voice quieter")
    else:
        print("Door zone  : none - entries are decided at the room-zone edge, peeks cannot be told apart")

    kept = 0
    try:
        while True:
            script_id = next_script(reps, counts, rng, args.fixed_order)
            if script_id is None:
                print("\nAll planned trials for this configuration are done.")
                break
            print(f"\n--- trial {sum(counts.values()) + 1}/{sum(reps.values())}: script {script_id} "
                  f"({SCRIPTS[script_id]['name']}) ---")
            result = run_trial(ctx, script_id)
            if result is None:
                continue
            row, detail = result
            choice = ask("[ENTER] keep  r = redo  q = quit > ").strip().lower()
            if choice == "q":
                break
            if choice == "r":
                reason = ask("Why is it redone? (logged) > ").strip()
                with open(discarded_path, "a") as f:
                    f.write(json.dumps({"discarded_at": iso(time.time()), "reason": reason, "seed": seed,
                                        "row": row, "zone": zone, "door_zone": door_zone,
                                        "session_info": session_info, **detail}) + "\n")
                print("  discarded and logged - that script stays in the pool.")
                continue
            note = ask("Optional note > ").strip()
            trial_id = next_trial_id(load_rows(csv_path))
            row.update({"trial_id": trial_id, "session": args.session,
                        "configuration": args.configuration, "mover_id": args.mover,
                        "timestamp": iso(time.time()), "simulated": int(args.simulate),
                        "zone_valid": int(bool(zone.get("valid"))), "zone_mode": zone.get("mode", ""),
                        "notes": note})
            append(csv_path, row)
            with open(jsonl_path, "a") as f:
                f.write(json.dumps({"trial_id": trial_id, "seed": seed, "row": row, "zone": zone,
                                    "door_zone": door_zone, "session_info": session_info, **detail}) + "\n")
            counts[script_id] = counts.get(script_id, 0) + 1
            kept += 1
    except KeyboardInterrupt:
        print("\nInterrupted - rerun the same command to continue where you left off.")
    finally:
        original_takeover()
        print(f"\nSaved {kept} trial(s) to {csv_path}" if kept else "\nNo trials kept - nothing written.")


if __name__ == "__main__":
    main()
