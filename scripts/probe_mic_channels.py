import argparse
import array
import math
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

# ArrayUAC10 lays its six channels out as: 0 processed (beamformed) audio,
# 1-4 the four raw microphones, 5 the playback reference. Only 1-4 say
# anything about the capsules themselves.
MIC_CHANNELS = (1, 2, 3, 4)


def record(device, rate, channels, seconds):
    proc = subprocess.run(
        ["arecord", "-q", "-D", device, "-t", "raw", "-r", str(rate), "-f", "S16_LE",
         "-c", str(channels), "-d", str(int(math.ceil(seconds)))],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip() or "arecord failed")
    samples = array.array("h")
    samples.frombytes(proc.stdout[: len(proc.stdout) // 2 * 2])
    return samples


def per_channel(samples, channels):
    sums = [0.0] * channels
    peaks = [0] * channels
    counts = [0] * channels
    for i, value in enumerate(samples):
        c = i % channels
        sums[c] += float(value) * value
        counts[c] += 1
        if abs(value) > peaks[c]:
            peaks[c] = abs(value)
    return [(math.sqrt(s / n) if n else 0.0, p) for s, n, p in zip(sums, counts, peaks)]


def dbfs(rms):
    return 20.0 * math.log10(rms / 32768.0) if rms > 0 else -120.0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare the four raw microphones of the ReSpeaker array against each other. "
                    "A capsule that is dead, blocked by the shell or covered reads far quieter "
                    "than its neighbours, and the array's direction finding degenerates with it.")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--device", default=config.MIC_DEVICE)
    parser.add_argument("--rate", type=int, default=config.MIC_RATE)
    parser.add_argument("--channels", type=int, default=config.MIC_CHANNELS)
    parser.add_argument("--quiet-first", action="store_true",
                        help="also record the empty room first and report the noise floor per capsule")
    args = parser.parse_args()

    if args.quiet_first:
        input(f"Silence please - ENTER to record {args.seconds:.0f} s of the empty room > ")
        floors = per_channel(record(args.device, args.rate, args.channels, args.seconds), args.channels)
        print("  noise floor:  " + "  ".join(
            f"mic {c}: {dbfs(floors[c][0]):6.1f} dBFS" for c in MIC_CHANNELS))
        print()

    input(f"Now talk steadily, close to the head - ENTER to record {args.seconds:.0f} s > ")
    print("  recording...")
    levels = per_channel(record(args.device, args.rate, args.channels, args.seconds), args.channels)
    print()

    mics = [(c, levels[c][0], levels[c][1]) for c in MIC_CHANNELS if c < args.channels]
    if not mics:
        print(f"Only {args.channels} channel(s) recorded - this probe needs the 6-channel raw device.")
        return
    loudest = max(rms for _, rms, _ in mics)
    print("  channel | level      | peak  | against the loudest")
    for c, rms, peak in mics:
        down = dbfs(rms) - dbfs(loudest) if loudest else 0.0
        bar = "#" * max(0, int(round(24 * (rms / loudest)))) if loudest else ""
        print(f"   mic {c}   | {dbfs(rms):6.1f} dBFS | {peak:5} | {down:+6.1f} dB {bar}")

    quiet = [(c, dbfs(rms) - dbfs(loudest)) for c, rms, _ in mics if loudest and rms / loudest < 0.5]
    print()
    if not quiet:
        print("  All four capsules are within 6 dB of each other, so none of them is blocked or dead.")
        print("  A direction ambiguity is then not down to a missing microphone.")
    else:
        for c, down in quiet:
            print(f"  Microphone {c} is {abs(down):.0f} dB below the loudest one.")
        print("  That is a blocked, covered or dead capsule. With one capsule out of four the array")
        print("  has only a triangle left to work with, which leaves its direction estimate")
        print("  ambiguous in steps of about 120 degrees - check the shell opening in front of it,")
        print("  any glue or foam over it, and that the board is seated properly.")


if __name__ == "__main__":
    main()
