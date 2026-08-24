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
    print("DOA-Ausgabe - kein Servo, kein Sensor-Rauschfilter. Strg+C zum Beenden.\n")

    try:
        while True:
            if doa.get_voice_active():
                print(f"{doa.get_direction_degrees():.1f}")
            time.sleep(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        pass
    finally:
        doa.close()
        print("\nCiao!")


if __name__ == "__main__":
    main()
