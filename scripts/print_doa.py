import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup

POLL_INTERVAL_S = 0.2


def main() -> None:
    logging_setup.configure_logging(config)

    from adapters.hardware.doa_respeaker import RespeakerDOAAdapter

    doa = RespeakerDOAAdapter(front_reference_degrees=config.DOA_FRONT_REFERENCE_DEGREES)
    print("DOA output - no servo, no sensor noise filter. Ctrl+C to quit.")
    print("VOICE lines mean the array's own VAD fired; quiet means it did not.\n")

    polls = 0
    voiced = 0
    seen = []
    started = time.monotonic()
    try:
        while True:
            polls += 1
            active = doa.get_voice_active()
            if active:
                voiced += 1
                angle = doa.get_direction_degrees()
                raw = (angle - 90.0 + config.DOA_FRONT_REFERENCE_DEGREES) % 360.0
                seen.append(angle)
                print(f"VOICE   raw={raw:6.1f}   target={angle:7.1f}   "
                      f"({voiced}/{polls} polls, {100.0 * voiced / polls:.0f}%)")
            elif polls % 25 == 0:
                print(f"  quiet  {time.monotonic() - started:5.0f}s elapsed, "
                      f"{voiced}/{polls} polls had voice ({100.0 * voiced / polls:.0f}%)")
            time.sleep(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        pass
    finally:
        doa.close()
        if polls:
            print(f"\n{voiced}/{polls} polls reported voice "
                  f"({100.0 * voiced / polls:.0f}%) over {time.monotonic() - started:.0f}s")
        if seen:
            from adapters.hardware.doa_respeaker import median_angle
            med = median_angle(seen)
            spread = sorted(abs(((a - med + 180.0) % 360.0) - 180.0) for a in seen)
            p90 = spread[int(0.9 * (len(spread) - 1))]
            raw_med = (med - 90.0 + config.DOA_FRONT_REFERENCE_DEGREES) % 360.0
            print(f"median target={med:.1f} deg  (raw={raw_med:.1f})  "
                  f"90% of readings within {p90:.1f} deg of it  n={len(seen)}")
            print(f"current DOA_FRONT_REFERENCE_DEGREES={config.DOA_FRONT_REFERENCE_DEGREES:.1f}")
            print("If the source was straight ahead, set that env var to the raw median above.")
        print("Bye!")


if __name__ == "__main__":
    main()
