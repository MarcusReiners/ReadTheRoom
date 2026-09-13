import argparse
import glob
import importlib.util
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyse_privacy_switch as analysis
from domain.conversation import ConversationState
from domain.events import ModalitySwitched, PersonEnteredRoom, PersonLeftRoom, RoomClearChanged, SpeechTranscribed
from service_layer.bus import EventBus
from service_layer.handlers import register_handlers


class SilentTTS:
    def request_takeover(self):
        return True

    def set_duck(self, active):
        pass


def load_radar(path):
    if not path:
        from adapters.hardware import radar_ld2450
        return radar_ld2450
    spec = importlib.util.spec_from_file_location("radar_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def replay(radar_module, trial, door, args):
    bus = EventBus()
    conversation = ConversationState()
    register_handlers(bus, conversation, SilentTTS(), departure_grace_s=60.0, door_clear_hold_s=1.5)
    clock = {"t": trial["t_arm"]}
    events = []
    bus.subscribe(PersonEnteredRoom, lambda e: events.append((clock["t"], "enter", e.via)))
    bus.subscribe(PersonLeftRoom, lambda e: events.append((clock["t"], "leave", None)))
    bus.subscribe(ModalitySwitched, lambda e: events.append((clock["t"], "modality", e.to_modality)))
    bus.subscribe(RoomClearChanged, lambda e: events.append((clock["t"], "clear", e.clear)))
    radar = radar_module.RadarLD2450Adapter(bus=bus, serial_port="/dev/null",
                                            entry_margin_mm=args.entry_margin, exit_margin_mm=args.exit_margin,
                                            door_near_mm=args.door_near)
    radar.set_door_zone(trial.get("door_zone") or door)
    bus.publish(SpeechTranscribed(text="(replay)", conversation_id="replay", source="voice"))
    for f in trial["events"]:
        if f["kind"] != "frame":
            continue
        clock["t"] = f["t"]
        targets = [{"id": t["id"], "x_mm": t["x"], "y_mm": t["y"], "speed_mms": t["v"], "in_zone": t["z"]}
                   for t in f["targets"]]
        radar._update_crossings(targets, trial["zone"], f["t"])
    return events


def outcome(script, events, live_switch, stay_s):
    switch = next((t for t, k, v in events if k == "modality" and v == "web"), None)
    if script in ("A", "E", "F"):
        cls = "TP" if switch is not None else "FN"
    else:
        cls = "FP" if switch is not None else "TN"
    ret, voice_t = None, None
    if cls == "TP" and live_switch is not None:
        cue = live_switch + stay_s
        voice_t = next((t for t, k, v in events if k == "clear" and v and t > switch), None)
        ret = "missed" if voice_t is None else ("premature" if voice_t < cue else "correct")
    return cls, ret, switch, voice_t


def main():
    parser = argparse.ArgumentParser(description="Replay a Study 2 session's radar frames through the decision logic.")
    parser.add_argument("csv", nargs="?", help="session CSV (default: the newest study/privacy_switch_*.csv)")
    parser.add_argument("--settings", help="JSON with the door_zone used (default: <csv>_settings.json)")
    parser.add_argument("--radar-module", help="replay with this copy of radar_ld2450.py instead of the current one, "
                                               "e.g. from: git show <rev>:adapters/hardware/radar_ld2450.py")
    parser.add_argument("--entry-margin", type=float, default=100.0)
    parser.add_argument("--exit-margin", type=float, default=100.0)
    parser.add_argument("--door-near", type=float, default=800.0)
    args = parser.parse_args()
    study = os.path.join(ROOT, "study")
    path = args.csv or max((p for p in glob.glob(os.path.join(study, "privacy_switch_*.csv"))
                            if not p.endswith("_video.csv")), key=os.path.getmtime)
    rows, logs, door = analysis.load(path, args.settings)
    radar_module = load_radar(args.radar_module)
    print(f"\n{os.path.basename(path)}: replaying {len(rows)} trials through "
          f"{args.radar_module or 'adapters/hardware/radar_ld2450.py'}")

    live_cls, replay_cls, live_ret, replay_ret, changed, shifts = Counter(), Counter(), Counter(), Counter(), [], []
    for r in rows:
        trial = logs.get(r["trial_id"])
        if trial is None:
            continue
        live = analysis.analyse_trial(r, trial, door)
        live_switch = live["switch"]["t"] if live.get("switch") else None
        cls, ret, _, voice_t = outcome(r["script_id"], replay(radar_module, trial, door, args), live_switch,
                                     (trial.get("session_info") or {}).get("stay_s", analysis.STAY_S))
        live_cls[live["cls"]] += 1
        replay_cls[cls] += 1
        if live.get("reversion"):
            live_ret[live["reversion"]] += 1
        if ret:
            replay_ret[ret] += 1
        if cls != live["cls"] or ret != live.get("reversion"):
            changed.append((r["trial_id"], r["script_id"], live["cls"], live.get("reversion"), cls, ret))
        elif ret == "correct" and live.get("voice"):
            shifts.append(voice_t - live["voice"]["t"])

    print("\n                 live      replay")
    for c in ("TP", "FN", "FP", "TN"):
        print(f"  {c:14} {live_cls[c]:>5}  {replay_cls[c]:>9}")
    for c in ("correct", "premature", "missed"):
        print(f"  return {c:9} {live_ret[c]:>3}  {replay_ret[c]:>9}")
    same = len(rows) - len(changed)
    print(f"\n  same outcome as live: {same}/{len(rows)} trials")
    for tid, s, lc, lr, rc, rr in changed:
        print(f"    trial {tid:>3} {s}: live {lc} {lr or ''} -> replay {rc} {rr or ''}")
    if shifts:
        analysis.dist("return to speech, replay - live", shifts)
    print()


if __name__ == "__main__":
    main()
