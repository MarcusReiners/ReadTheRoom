import argparse
import array
import glob
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import wave

STIMULUS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "study", "stimulus")
SR = 16000

HARVARD = [
    "The birch canoe slid on the smooth planks.",
    "Glue the sheet to the dark blue background.",
    "It's easy to tell the depth of a well.",
    "These days a chicken leg is a rare dish.",
    "Rice is often served in round bowls.",
    "The juice of lemons makes fine punch.",
]

BABBLE_VOICES = ["Samantha", "Daniel", "Karen", "Moira", "Tessa", "Rishi", "Fred", "Tara"]
BABBLE_TEXTS = [
    "The quarterly figures came in higher than anyone expected, so the meeting ran long and nobody minded very much at all.",
    "I told him the train leaves at half past six but he never listens to a word I say about scheduling anything.",
    "We should probably order lunch before the queue gets ridiculous again like it did on Tuesday afternoon.",
    "The new office layout is fine except the printer is now impossibly far from the people who actually use it.",
    "She spent the whole weekend rewriting that chapter and still is not happy with how the introduction reads.",
    "If the weather holds we could walk along the river instead of sitting inside for another three hours.",
    "There is a box of documents in the corridor that somebody needs to deal with before the inspection.",
    "He kept insisting the recipe needed more salt but honestly it tasted perfectly reasonable to me already.",
]


def require_macos_tools() -> None:
    missing = [t for t in ("say", "afconvert") if shutil.which(t) is None]
    if missing:
        raise SystemExit(f"needs macOS {' and '.join(missing)} - run this on the Mac, not the Pi.")


def synth(text: str, voice: str, rate: int, out_wav: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp:
        aiff = tmp.name
    try:
        subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", aiff, text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{SR}", "-c", "1", aiff, out_wav],
                       check=True)
    finally:
        os.unlink(aiff)


def read_pcm(path: str) -> array.array:
    with wave.open(path) as w:
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
        return a


def write_pcm(path: str, samples) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(array.array("h", samples).tobytes())


def describe(path: str) -> None:
    a = read_pcm(path)
    peak = max(abs(v) for v in a) if a else 0
    thr = peak * 0.02
    lead = next((i for i, v in enumerate(a) if abs(v) > thr), 0)
    print(f"  {os.path.basename(path):24} {len(a) / SR:6.2f}s  peak={peak:5d}  "
          f"leading silence={lead / SR * 1000:.0f}ms")


def make_speech(voice: str, rate: int) -> str:
    out = os.path.join(STIMULUS_DIR, "harvard_long.wav")
    synth("  ".join(HARVARD), voice, rate, out)
    return out


def make_babble(duration_s: float, seed: int) -> str:
    random.seed(seed)
    tmpdir = tempfile.mkdtemp(prefix="babble_")
    try:
        for i, voice in enumerate(BABBLE_VOICES):
            text = " ".join(BABBLE_TEXTS[(i + k) % len(BABBLE_TEXTS)] for k in (0, 3, 5, 1))
            synth(text, voice, 158 + i * 8, os.path.join(tmpdir, f"t{i}.wav"))
        n = int(SR * duration_s)
        mix = [0.0] * n
        for f in sorted(glob.glob(os.path.join(tmpdir, "t*.wav"))):
            a = read_pcm(f)
            off = random.randrange(n)
            for i in range(n):
                mix[i] += a[(i + off) % len(a)]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    rms = math.sqrt(sum(v * v for v in mix) / n)
    gain = 4000.0 / rms
    peak = max(abs(v) for v in mix)
    if peak * gain > 30000:
        gain = 30000.0 / peak
    out_samples = [int(max(-32768, min(32767, v * gain))) for v in mix]

    cross = int(SR * 0.2)
    for i in range(cross):
        t = i / cross
        out_samples[i] = int(out_samples[i] * math.sqrt(t)
                             + out_samples[n - cross + i] * math.sqrt(1 - t))
    out_samples = out_samples[:n - cross]

    out = os.path.join(STIMULUS_DIR, "babble_8talker.wav")
    write_pcm(out, out_samples)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Regenerate the Study 1 audio stimuli (macOS only).")
    parser.add_argument("--speech-only", action="store_true")
    parser.add_argument("--babble-only", action="store_true")
    parser.add_argument("--voice", default="Samantha", help="voice for the speech stimulus")
    parser.add_argument("--rate", type=int, default=168, help="words per minute")
    parser.add_argument("--babble-seconds", type=float, default=60.0)
    parser.add_argument("--seed", type=int, default=11, help="babble mix offsets, for reproducibility")
    args = parser.parse_args()

    require_macos_tools()
    os.makedirs(STIMULUS_DIR, exist_ok=True)
    made = []
    if not args.babble_only:
        made.append(make_speech(args.voice, args.rate))
    if not args.speech_only:
        made.append(make_babble(args.babble_seconds, args.seed))

    print(f"\nWritten to {STIMULUS_DIR}:")
    for path in made:
        describe(path)
    print("\nSpeech: 6 Harvard sentences (IEEE Rec. Practice for Speech Quality Measurements, 1969).")
    print("Babble: 8 overlapping synthetic talkers, 200ms crossfade so it loops seamlessly.")
    print("The babble is NOT citable - use NOISEX-92 babble for data you report.")


if __name__ == "__main__":
    main()
