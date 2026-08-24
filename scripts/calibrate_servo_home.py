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

    # move_to_home_on_start=False: the servo stays exactly wherever it already
    # physically is - constructing the adapter (or anything below, before the
    # first nudge) must not command any movement of its own.
    turntable = build_turntable(config, move_to_home_on_start=False)

    # Label only, not applied to hardware - servos have no position feedback,
    # so this is just a reference point for computing relative deltas. It
    # will very likely NOT match reality (the servo may have been powered
    # off, bumped, etc. since it was last commanded) - the first nudge you
    # type is what actually starts moving it, from wherever it really is.
    raw_angle = turntable.raw_angle_for_offset(turntable.home_offset_degrees)

    print(
        "Servo-Home-Kalibrierung - der Servo bewegt sich NICHT beim Start, "
        "erst durch deine Eingaben.\n"
        f"Referenzwert: {raw_angle:.1f} Grad (aus gespeichertem Offset "
        f"{turntable.home_offset_degrees:.1f}) - entspricht evtl. nicht der "
        "tatsaechlichen Position, da der Servo keine Positionsrueckmeldung hat.\n"
        "Zahl + Enter (z.B. 5 oder -3) bewegt den Kopf um diese Gradzahl, "
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
                print(f"Gespeichert: Offset {offset:.1f} Grad (Position {raw_angle:.1f}).")
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
        print("\nServo freigegeben. Nicht gespeicherte Aenderungen wurden verworfen. Ciao!")


if __name__ == "__main__":
    main()
