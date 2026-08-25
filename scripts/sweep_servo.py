import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup

HOLD_SECONDS = 2.0


def main() -> None:
    logging_setup.configure_logging(config)

    if not config.USE_SERVO:
        print("USE_SERVO ist False - kein Servo zum Testen.")
        return

    from adapters.factory import build_turntable

    turntable = build_turntable(config)

    # Read off the turntable rather than config: the safe range is editable
    # from the web app, and commanding the config value would sweep a
    # narrower arc than the head is actually allowed to travel.
    lo, hi = turntable.safe_min_angle, turntable.safe_max_angle
    print(f"Voller Schwenk zwischen {lo:.0f} und {hi:.0f} Grad.")
    print("Strg+C zum Beenden.")

    try:
        while True:
            print(f"-> {lo:.0f} Grad")
            turntable.set_angle_immediate(lo)
            time.sleep(HOLD_SECONDS)

            print(f"-> {hi:.0f} Grad")
            turntable.set_angle_immediate(hi)
            time.sleep(HOLD_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        turntable.stop()
        print("\nServo freigegeben. Ciao!")


if __name__ == "__main__":
    main()
