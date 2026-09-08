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
        help="Zielwinkel in DOA-Konvention (90=Home, Standard: Home-Position).",
    )
    args = parser.parse_args()

    logging_setup.configure_logging(config)

    if not config.USE_SERVO:
        print("USE_SERVO is False - no servo to home.")
        return

    from adapters.factory import build_turntable

    turntable = build_turntable(config)
    if args.angle is not None:
        turntable.set_doa_angle_immediate(args.angle)
        print(f"Servo holding at DOA angle {args.angle:.0f} deg (90 = home).")
    else:
        turntable.home()
        print("Servo at home position.")
    print("Ctrl+C to quit (the servo is released afterwards).")

    try:
        while True:
            input()
    except KeyboardInterrupt:
        pass
    finally:
        turntable.stop()
        print("\nServo released.")


if __name__ == "__main__":
    main()
