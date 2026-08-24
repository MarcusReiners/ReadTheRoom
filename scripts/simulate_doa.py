import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup

# RAW simulated sensor readings (what the array itself would report), NOT
# pre-converted DOA-convention targets - get converted the same way
# RespeakerDOAAdapter.get_direction_degrees() converts a real reading, so
# this exercises the exact same pipeline live tracking uses end to end.
RAW_READINGS = [180.0, 110.0, 250.0]
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
    from adapters.hardware.doa_respeaker import raw_to_target_degrees

    turntable = build_turntable(config)
    front_reference = config.DOA_FRONT_REFERENCE_DEGREES
    print(
        "DOA-Simulation - speist simulierte RAW-Sensorwerte durch dieselbe "
        "Umrechnung wie RespeakerDOAAdapter.get_direction_degrees() (importiert, "
        "nicht dupliziert), dann in rotate_towards() (exakt derselbe Pfad wie "
        "beim echten Tracking) - ganz ohne Sensor, um reine Code-Logik von "
        "Sensor-Rauschen zu trennen.\n"
        f"front_reference={front_reference:.1f}\n"
        f"Start: current_heading_degrees={turntable.current_heading_degrees:.1f} "
        f"(sicherer Bereich {turntable._min_angle:.1f}-{turntable._max_angle:.1f})\n"
    )

    try:
        for raw in RAW_READINGS:
            target = raw_to_target_degrees(raw, front_reference)
            before = turntable.current_heading_degrees
            print(f"-> Simuliere RAW={raw:.0f} Grad -> target={target:.1f} Grad (aktuell: {before:.1f})")
            turntable.rotate_towards(target_angle_degrees=target)
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
