import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup


def main() -> None:
    logging_setup.configure_logging(config)

    if not config.USE_SERVO:
        print("USE_SERVO is False - no servo to calibrate.")
        return

    from adapters.factory import build_turntable
    from adapters.hardware.servo_calibration import save_home_offset

    # move_to_home_on_start=False: we explicitly drive it to the hardware
    # midpoint ourselves right below, not to whatever the last saved (likely
    # stale - e.g. after remounting the horn) home offset would compute.
    turntable = build_turntable(config, move_to_home_on_start=False)

    # Always start at the servo's own hardware midpoint (180 by default) -
    # the middle of its LINEAR pulse-width range, as far as possible from
    # both physical ends. This hardware doesn't wrap around (confirmed on
    # the bench - commanded the "short way" across 0/360, it took the long
    # way instead), so keeping the safe window centered far from either end
    # is what actually avoids that problem, rather than any software fix.
    # Mount/adjust the horn here so this becomes "forward", then fine-tune
    # with nudges below if it's not perfectly aligned.
    mid = (config.SERVO_HARDWARE_MIN_ANGLE + config.SERVO_HARDWARE_MAX_ANGLE) / 2
    raw_angle = turntable.set_raw_angle(mid)

    print(
        "Servo-Home-Kalibrierung - der Servo faehrt auf seine Hardware-Mitte "
        f"({raw_angle:.0f} Grad), moeglichst weit von beiden physischen Enden "
        "entfernt.\n"
        "Falls noch nicht geschehen: jetzt den Kopf/Horn auf dieser Position "
        "montieren, sodass sie 'geradeaus' entspricht.\n"
        "Zahl + Enter (z.B. 5 oder -3) feinjustiert die Position, "
        "unbeschraenkt vom sicheren 140-Grad-Fenster.\n"
        "'s' + Enter speichert die aktuelle Position als neue Home-Position "
        "(danach im Normalbetrieb wieder auf +/-70 Grad ab hier begrenzt).\n"
        "'q' + Enter oder Strg+C beendet (ungespeicherte Aenderungen gehen verloren).\n"
    )

    try:
        while True:
            line = input(f"[Position {raw_angle:.1f}] > ").strip().lower()
            if not line:
                continue
            if line in ("q", "quit"):
                break
            if line in ("s", "save"):
                offset = turntable.offset_for_raw_angle(raw_angle)
                turntable.set_home_offset(offset)
                save_home_offset(config.SERVO_CALIBRATION_PATH, offset)
                print(f"Saved: offset {offset:.1f} deg (position {raw_angle:.1f}).")
                continue
            try:
                delta = float(line)
            except ValueError:
                print("Unbekannte Eingabe - Zahl, 's' zum Speichern, oder 'q' zum Beenden.")
                continue

            raw_angle = turntable.set_raw_angle(raw_angle + delta)
    except KeyboardInterrupt:
        pass
    finally:
        turntable.stop()
        print("\nServo released. Unsaved changes were discarded.")


if __name__ == "__main__":
    main()
