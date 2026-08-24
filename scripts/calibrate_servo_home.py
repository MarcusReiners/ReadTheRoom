import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup


def main() -> None:
    logging_setup.configure_logging(config)

    if not config.USE_SERVO:
        print("USE_SERVO ist False - kein Servo zum Kalibrieren.")
        return

    from adapters.factory import build_turntable
    from adapters.hardware.servo_calibration import save_home_offset

    turntable = build_turntable(config)

    print(
        "Servo-Home-Kalibrierung.\n"
        f"Aktueller Offset: {turntable.home_offset_degrees:.1f} Grad "
        f"(aus {config.SERVO_CALIBRATION_PATH}, 0 falls noch nie gespeichert).\n"
        "Zahl + Enter (z.B. 5 oder -3) bewegt den Kopf um diese Gradzahl.\n"
        "'s' + Enter speichert den aktuellen Offset als neue Home-Position.\n"
        "'q' + Enter oder Strg+C beendet (ungespeicherte Aenderungen gehen verloren).\n"
    )

    try:
        while True:
            line = input(f"[Offset {turntable.home_offset_degrees:.1f}] > ").strip().lower()
            if not line:
                continue
            if line in ("q", "quit"):
                break
            if line in ("s", "save"):
                save_home_offset(config.SERVO_CALIBRATION_PATH, turntable.home_offset_degrees)
                print(f"Gespeichert: {turntable.home_offset_degrees:.1f} Grad.")
                continue
            try:
                delta = float(line)
            except ValueError:
                print("Unbekannte Eingabe - Zahl, 's' zum Speichern, oder 'q' zum Beenden.")
                continue

            new_offset = turntable.home_offset_degrees + delta
            turntable.set_home_offset(new_offset)
            turntable.home()
    except KeyboardInterrupt:
        pass
    finally:
        turntable.stop()
        print("\nServo freigegeben. Nicht gespeicherte Aenderungen wurden verworfen. Ciao!")


if __name__ == "__main__":
    main()
