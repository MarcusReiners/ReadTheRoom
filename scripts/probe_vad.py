import argparse
import csv
import os
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup

STUDY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "study")
POLL_INTERVAL_S = 0.05
SEGMENTS = [
    ("quiet", "Stay silent, nothing moving."),
    ("speech_desk", "Speak normally from the desk, with natural pauses, for about 20 s."),
    ("speech_far", "Speak normally from the far side of the room, for about 20 s."),
    ("knocking", "Knock on the desk a few times, for about 20 s."),
    ("typing", "Type on a keyboard next to the device, for about 20 s."),
    ("objects", "Put a cup down, close a drawer, move a chair - about 20 s."),
    ("footsteps", "Walk around the room, for about 20 s."),
    ("clapping", "Clap a few times, for about 20 s."),
    ("media", "Play music or a video with speech, for about 20 s (s to skip)."),
]


def onsets(values):
    return sum(1 for a, b in zip([0] + values, values) if b and not a)


def main():
    parser = argparse.ArgumentParser(description="Compare the ReSpeaker's VOICEACTIVITY and SPEECHDETECTED "
                                                 "flags on speech and on everyday non-speech sounds.")
    parser.add_argument("--session", default=datetime.now().strftime("%Y%m%d_%H%M"))
    args = parser.parse_args()
    logging_setup.configure_logging(config)

    from adapters.hardware.doa_respeaker import RespeakerDOAAdapter
    doa = RespeakerDOAAdapter(front_reference_degrees=config.DOA_FRONT_REFERENCE_DEGREES)

    state = {"segment": None, "stop": False}
    polls = []

    def poll():
        while not state["stop"]:
            label = state["segment"]
            if label:
                va = int(doa.get_voice_active())
                sd = int(doa.get_speech_detected())
                polls.append((time.time(), label, va, sd))
            time.sleep(POLL_INTERVAL_S)

    threading.Thread(target=poll, daemon=True).start()
    print("Stop the main app first - it reads the same flags. Each step: ENTER starts it, ENTER ends it, "
          "s + ENTER skips it.")
    started = {}
    try:
        for label, text in SEGMENTS:
            if input(f"\n{label}: {text}\n  ENTER to start > ").strip().lower() == "s":
                continue
            started[label] = time.time()
            state["segment"] = label
            input("  recording ... ENTER when done > ")
            state["segment"] = None
    except KeyboardInterrupt:
        print()
    finally:
        state["stop"] = True
        time.sleep(POLL_INTERVAL_S * 2)

    path = os.path.join(STUDY_DIR, f"vad_probe_{args.session}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "segment", "voice_activity", "speech_detected"])
        w.writerows(polls)

    print(f"\nRaw polls: {path}")
    print(f"{'segment':14} {'polls':>6} {'s':>6}   {'VOICEACTIVITY':>22}   {'SPEECHDETECTED':>22}")
    print(f"{'':14} {'':>6} {'':>6}   {'active %':>10} {'onsets':>6} {'1st s':>5}   "
          f"{'active %':>10} {'onsets':>6} {'1st s':>5}")
    for label, _ in SEGMENTS:
        rows = [p for p in polls if p[1] == label]
        if not rows:
            continue
        cols = []
        for i in (2, 3):
            vals = [p[i] for p in rows]
            first = next((p[0] - started[label] for p in rows if p[i]), None)
            cols.append(f"{100 * sum(vals) / len(vals):>9.1f}% {onsets(vals):>6} "
                        f"{'-' if first is None else f'{first:.2f}':>5}")
        print(f"{label:14} {len(rows):>6} {rows[-1][0] - rows[0][0]:>6.1f}   {cols[0]}   {cols[1]}")


if __name__ == "__main__":
    main()
