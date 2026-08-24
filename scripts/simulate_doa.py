import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup

ANGLES = [180.0, 110.0, 250.0]
HOLD_SECONDS = 3.0


def main() -> None:
    logging_setup.configure_logging(config)
    # DEBUG just for the turntable, so its track_relative_angle() internals
    # (current/target/raw_target/window/result) print without switching the
    # whole app's log level.
    logging.getLogger("adapters.hardware.turntable").setLevel(logging.DEBUG)

    if not config.USE_SERVO:
        print("USE_SERVO ist False - kein Servo zum Simulieren.")
        return

    from adapters.factory import build_turntable

    turntable = build_turntable(config)
    print(
        "DOA-Simulation - speist feste Zielwinkel direkt in rotate_towards() ein "
        "(exakt derselbe Pfad wie beim echten Tracking), ganz ohne Sensor - "
        "damit lässt sich reine Code-Logik von Sensor-Rauschen trennen.\n"
        f"Start: current_heading_degrees={turntable.current_heading_degrees:.1f} "
        f"(sicherer Bereich {turntable._min_angle:.1f}-{turntable._max_angle:.1f})\n"
    )

    try:
        for angle in ANGLES:
            before = turntable.current_heading_degrees
            print(f"-> Simuliere DOA-Zielwinkel {angle:.0f} Grad (aktuell: {before:.1f})")
            turntable.rotate_towards(target_angle_degrees=angle)
            after = turntable.current_heading_degrees
            print(f"   Ergebnis: current_heading_degrees={after:.1f} (Delta {after - before:+.1f})\n")
            time.sleep(HOLD_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        turntable.stop()
        print("\nServo freigegeben. Ciao!")


if __name__ == "__main__":
    main()
