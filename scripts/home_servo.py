import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup


def main() -> None:
    logging_setup.configure_logging(config)

    if not config.USE_SERVO:
        print("USE_SERVO ist False - kein Servo zum Homen.")
        return

    from adapters.hardware.turntable import ServoTurntableAdapter

    turntable = ServoTurntableAdapter(
        pin=config.SERVO_GPIO_PIN,
        min_angle=config.SERVO_MIN_ANGLE,
        max_angle=config.SERVO_MAX_ANGLE,
    )
    turntable.home()
    center = (config.SERVO_MIN_ANGLE + config.SERVO_MAX_ANGLE) / 2
    print(f"Servo in Home-Position ({center:.0f} Grad) - haelt die Position zum Montieren.")
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
