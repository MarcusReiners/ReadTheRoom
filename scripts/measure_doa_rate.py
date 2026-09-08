import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup

POLL_S = 0.01
DURATION_S = 20.0


def main() -> None:
    logging_setup.configure_logging(config)
    from adapters.hardware.doa_respeaker import RespeakerDOAAdapter

    doa = RespeakerDOAAdapter(front_reference_degrees=config.DOA_FRONT_REFERENCE_DEGREES)
    print(f"Polling the array as fast as possible for {DURATION_S:.0f}s.")
    print("Play a continuous sound from a fixed position. Ctrl+C to stop early.\n")

    last = None
    changes = []
    samples = 0
    started = time.monotonic()
    try:
        while time.monotonic() - started < DURATION_S:
            if doa.get_voice_active():
                angle = doa.get_direction_degrees()
                samples += 1
                now = time.monotonic()
                if last is not None and angle != last[1]:
                    changes.append(now - last[0])
                    last = (now, angle)
                elif last is None:
                    last = (now, angle)
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        pass
    finally:
        doa.close()

    print(f"\n{samples} voice-active reads in {time.monotonic() - started:.1f}s")
    if len(changes) < 3:
        print("Too few value changes to estimate the update period.")
        print("Either the source was too quiet, or the value never moved at all.")
        return
    changes.sort()
    med = changes[len(changes) // 2]
    print(f"{len(changes)} value changes; median gap between changes = {med * 1000:.0f}ms")
    print(f"-> the array updates roughly every {med * 1000:.0f}ms ({1.0 / med:.1f} Hz)")
    print(f"-> space study samples at least {med * 1000:.0f}ms apart "
          f"(--doa-sample-interval {max(0.05, med):.2f})")
