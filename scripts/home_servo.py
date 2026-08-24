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
        print("USE_SERVO ist False - kein Servo zum Homen.")
        return

    from adapters.factory import build_turntable

    turntable = build_turntable(config)
    if args.angle is not None:
        turntable.set_doa_angle_immediate(args.angle)
        print(f"Servo haelt bei DOA-Winkel {args.angle:.0f} Grad (90=Home).")
    else:
        turntable.home()
        print("Servo in Home-Position.")
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
