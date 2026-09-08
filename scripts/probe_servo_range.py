import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup

# How far to move per step. Small on purpose - the whole point is to give
# you time to notice resistance/grinding before it becomes a stall, not to
# sweep quickly and find out the hard way. Override with --step if 5 feels
# too coarse near a limit.
DEFAULT_STEP_DEGREES = 5.0


def probe_direction(turntable, start_angle: float, sign: int, step_degrees: float, label: str) -> float:
    """Steps the servo outward from start_angle one step at a time, waiting
    for the operator between every single step - never moves unattended.
    Returns the last angle confirmed safe (NOT wherever it stopped, in case
    the operator's last step was the one that hit resistance)."""
    print(f"\nRichtung {label}: Enter = naechster Schritt, 's' = Grenze hier, 'q' = sofort abbrechen.")
    angle = start_angle
    last_safe = start_angle
    while True:
        line = input(f"  [Position {angle:.1f} Grad] > ").strip().lower()
        if line in ("q", "quit"):
            print("Cancelled - no limit saved for this direction.")
            return start_angle
        if line in ("s", "stop"):
            print(f"Limit {label}: {last_safe:.1f} deg.")
            return last_safe

        # Any other input (including empty Enter) advances one step - this
        # deliberately does NOT accept a custom step size here: the whole
        # safety property of this script is "next step is always small and
        # always waits", which a free-form delta prompt would let you break.
        last_safe = angle
        angle = turntable.set_raw_angle(angle + sign * step_degrees)
        clamped_by_config = abs(angle - last_safe) < abs(step_degrees) - 0.01
        if clamped_by_config:
            print(
                f"  Servo an SERVO_HARDWARE_{'MIN' if sign < 0 else 'MAX'}_ANGLE "
                f"({angle:.1f} Grad) in config.py geklemmt - das ist die konfigurierte, "
                "nicht zwingend die echte mechanische Grenze. Falls hier schon Widerstand "
                "spuerbar ist, ist die echte Grenze noch enger; falls nicht, weiten Sie "
                "SERVO_HARDWARE_MIN/MAX_ANGLE in .env vorlaeufig, um weiter zu testen."
            )
            return angle


def main() -> None:
    logging_setup.configure_logging(config)

    if not config.USE_SERVO:
        print("USE_SERVO is False - no servo to test.")
        return

    step_degrees = DEFAULT_STEP_DEGREES
    for arg in sys.argv[1:]:
        if arg.startswith("--step="):
            step_degrees = float(arg.split("=", 1)[1])

    from adapters.factory import build_turntable

    # move_to_home_on_start=False: start exactly wherever set_raw_angle first
    # places it (the configured hardware midpoint, below) rather than
    # whatever the currently saved home offset is - this probes the servo's
    # true mechanical limits, which have nothing to do with where "forward"
    # happens to be calibrated to right now.
    turntable = build_turntable(config, move_to_home_on_start=False)

    mid = (config.SERVO_HARDWARE_MIN_ANGLE + config.SERVO_HARDWARE_MAX_ANGLE) / 2
    start_angle = turntable.set_raw_angle(mid)

    print(
        "Servo-Bereich testen - findet die ECHTE mechanische Grenze, nicht die in "
        "config.py konfigurierte.\n\n"
        f"Start bei der Mitte des konfigurierten Bereichs ({start_angle:.1f} Grad).\n"
        f"Schrittweite: {step_degrees:.1f} Grad pro Enter.\n\n"
        "WICHTIG: nach JEDEM Schritt anhalten und pruefen, bevor Sie weitermachen -\n"
        "hoeren (Brummen/Knirschen) UND, falls sicher erreichbar, leicht mit der Hand\n"
        "gegenhalten. Bei geringstem Widerstand SOFORT 's' eingeben, nicht weiterfahren -\n"
        "ein Servo, das gegen seinen Endanschlag anlaeuft, kann Zahnraeder beschaedigen\n"
        "oder durchbrennen, auch bei kurzer Belastung.\n"
    )

    try:
        input("Enter zum Start Richtung MIN (Strg+C jederzeit zum Abbrechen ohne Speichern) > ")
        min_limit = probe_direction(turntable, start_angle, sign=-1, step_degrees=step_degrees, label="MIN")

        print(f"\nBack to the start position ({start_angle:.1f} deg)...")
        turntable.set_raw_angle(start_angle)

        input("Enter zum Start Richtung MAX > ")
        max_limit = probe_direction(turntable, start_angle, sign=1, step_degrees=step_degrees, label="MAX")

        print(f"\nBack to the start position ({start_angle:.1f} deg)...")
        turntable.set_raw_angle(start_angle)

        margin = max(step_degrees, 5.0)
        safe_min = min_limit + margin
        safe_max = max_limit - margin
        print(
            "\n--- Ergebnis ---\n"
            f"Gemessene Grenzen: {min_limit:.1f} - {max_limit:.1f} Grad "
            f"({max_limit - min_limit:.1f} Grad Gesamtbereich).\n"
            f"Empfohlen mit {margin:.0f} Grad Sicherheitsabstand pro Seite:\n\n"
            f"  SERVO_HARDWARE_MIN_ANGLE={safe_min:.0f}\n"
            f"  SERVO_HARDWARE_MAX_ANGLE={safe_max:.0f}\n\n"
            "In .env eintragen. Pruefen Sie danach, dass SERVO_MIN_ANGLE/SERVO_MAX_ANGLE "
            "(das sichere Kabel-Fenster, separat davon) noch innerhalb dieses neuen "
            "Bereichs liegen - andernfalls calibrate_servo_home.py erneut ausfuehren."
        )
    except KeyboardInterrupt:
        print("\nCancelled - nothing changed.")
    finally:
        turntable.stop()
        print("Servo released.")


if __name__ == "__main__":
    main()
