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

    # Start from wherever the last saved home offset actually points to in
    # real hardware degrees, not the offset number itself - nudges from here
    # move the servo directly on its true 0-360 range, unclamped by the
    # cable-safety window (that window is for autonomous conversation-driven
    # movement, not a supervised calibration session watching every move).
    raw_angle = turntable.raw_angle_for_offset(turntable.home_offset_degrees)
    turntable.set_raw_angle(raw_angle)

    print(
        "Servo-Home-Kalibrierung (unbeschraenkt - bewegt sich frei auf dem vollen "
        "Hardware-Bereich, nicht nur im sicheren 140-Grad-Fenster).\n"
        f"Aktuelle Position: {raw_angle:.1f} Grad "
        f"(entspricht gespeichertem Offset {turntable.home_offset_degrees:.1f}).\n"
        "Zahl + Enter (z.B. 5 oder -3) bewegt den Kopf um diese Gradzahl.\n"
        "'s' + Enter speichert die aktuelle Position als neue Home-Position.\n"
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
                print(f"Gespeichert: Offset {offset:.1f} Grad (Position {raw_angle:.1f}).")
                continue
            try:
                delta = float(line)
            except ValueError:
                print("Unbekannte Eingabe - Zahl, 's' zum Speichern, oder 'q' zum Beenden.")
                continue

            raw_angle += delta
            turntable.set_raw_angle(raw_angle)
    except KeyboardInterrupt:
        pass
    finally:
        turntable.stop()
        print("\nServo freigegeben. Nicht gespeicherte Aenderungen wurden verworfen. Ciao!")


if __name__ == "__main__":
    main()
