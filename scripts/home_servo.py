import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup


def main() -> None:
    parser = argparse.ArgumentParser(description="Servo auf eine feste Position fahren und dort halten.")
    parser.add_argument(
        "--angle", type=float, default=None,
        help="Zielwinkel in Grad (Standard: Mitte von SERVO_MIN_ANGLE/SERVO_MAX_ANGLE).",
    )
    args = parser.parse_args()

    logging_setup.configure_logging(config)

    if not config.USE_SERVO:
        print("USE_SERVO ist False - kein Servo zum Homen.")
        return

    from adapters.hardware.turntable import ServoTurntableAdapter

    turntable = ServoTurntableAdapter(
        pin=config.SERVO_GPIO_PIN,
        min_angle=config.SERVO_MIN_ANGLE,
        max_angle=config.SERVO_MAX_ANGLE,
        hardware_min_angle=config.SERVO_HARDWARE_MIN_ANGLE,
        hardware_max_angle=config.SERVO_HARDWARE_MAX_ANGLE,
    )
    center = (config.SERVO_MIN_ANGLE + config.SERVO_MAX_ANGLE) / 2
    target = args.angle if args.angle is not None else center
    turntable.set_angle_immediate(target)
    print(f"Servo haelt bei {target:.0f} Grad.")
    print("Strg+C zum Beenden (Servo wird danach freigegeben).")

    try:
        while True:
            input()
    except KeyboardInterrupt:
        pass
    finally:
        turntable.stop()
        print("\nServo freigegeben. Ciao!")


if __name__ == "__main__":
    main()
