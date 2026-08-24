import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup

HOLD_SECONDS = 2.0


def main() -> None:
    logging_setup.configure_logging(config)

    if not config.USE_SERVO:
        print("USE_SERVO ist False - kein Servo zum Testen.")
        return

    from adapters.hardware.turntable import ServoTurntableAdapter

    turntable = ServoTurntableAdapter(
        pin=config.SERVO_GPIO_PIN,
        min_angle=config.SERVO_MIN_ANGLE,
        max_angle=config.SERVO_MAX_ANGLE,
        hardware_min_angle=config.SERVO_HARDWARE_MIN_ANGLE,
        hardware_max_angle=config.SERVO_HARDWARE_MAX_ANGLE,
    )

    print(f"Voller Schwenk zwischen {config.SERVO_MIN_ANGLE:.0f} und {config.SERVO_MAX_ANGLE:.0f} Grad.")
    print("Strg+C zum Beenden.")

    try:
        while True:
            print(f"-> {config.SERVO_MIN_ANGLE:.0f} Grad")
            turntable.set_angle_immediate(config.SERVO_MIN_ANGLE)
            time.sleep(HOLD_SECONDS)

            print(f"-> {config.SERVO_MAX_ANGLE:.0f} Grad")
            turntable.set_angle_immediate(config.SERVO_MAX_ANGLE)
            time.sleep(HOLD_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        turntable.stop()
        print("\nServo freigegeben. Ciao!")


if __name__ == "__main__":
    main()
