import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup
from service_layer.bus import EventBus
from domain.events import ListeningStateChanged, SpeechPlaybackStarted, SpeechPlaybackEnded


def build_display(bus: EventBus):
    if not config.USE_LED_MATRIX:
        print("USE_LED_MATRIX ist False - kein Display zum Testen.")
        return None, None, None

    from adapters.hardware.led_matrix import LedMatrix
    from adapters.hardware.status_display import StatusDisplayAdapter
    from adapters.hardware.led_eyes import LedEyesAdapter

    matrix = LedMatrix(rows=48, cols=96, chain=2)
    status = StatusDisplayAdapter(bus=bus, x_offset=0, width=96)
    face = LedEyesAdapter(bus=bus, x_offset=96, width=96, height=48)
    matrix.add_renderer(status)
    matrix.add_renderer(face)
    matrix.start()
    return matrix, status, face


def build_turntable():
    if not config.USE_SERVO:
        print("USE_SERVO ist False - kein Servo zum Testen.")
        return None

    from adapters.hardware.turntable import ServoTurntableAdapter

    return ServoTurntableAdapter(
        pin=config.SERVO_GPIO_PIN,
        min_angle=config.SERVO_MIN_ANGLE,
        max_angle=config.SERVO_MAX_ANGLE,
    )


def demo_display(bus: EventBus, status, face) -> None:
    if status is None:
        return

    print("Display: idle...")
    time.sleep(2)

    print("Display: listening...")
    bus.publish(ListeningStateChanged(listening=True))
    time.sleep(2)
    bus.publish(ListeningStateChanged(listening=False))

    print("Display: speaking...")
    bus.publish(SpeechPlaybackStarted(text="Testantwort"))
    time.sleep(3)
    bus.publish(SpeechPlaybackEnded(completed=True))

    print("Display: scrolling text takeover...")
    status.set_text("Hallo, ich bin ReadTheRoom! Das ist ein Test des Displays.")
    time.sleep(6)
    status.stop_text()

    if face is not None:
        print("Display: eyes tracking sweep...")
        for angle in (90.0, 45.0, 90.0, 135.0, 90.0):
            face.set_eye_direction(angle_degrees=angle)
            time.sleep(1)


def demo_servo(turntable) -> None:
    if turntable is None:
        return

    # Note: rotate_towards() re-centers its input on 90 degrees = straight ahead, so
    # this only sweeps the servo's true min/center/max as written while SERVO_MIN_ANGLE
    # and SERVO_MAX_ANGLE straddle 90 degrees symmetrically (true for the defaults).
    print(f"Servo: sweeping {config.SERVO_MIN_ANGLE}-{config.SERVO_MAX_ANGLE} Grad...")
    for angle in (
        config.SERVO_MIN_ANGLE,
        config.SERVO_MAX_ANGLE,
        (config.SERVO_MIN_ANGLE + config.SERVO_MAX_ANGLE) / 2,
    ):
        print(f"  -> {angle} Grad")
        turntable.rotate_towards(target_angle_degrees=angle)
        time.sleep(2)


def live_doa_tracking(turntable, face, poll_interval_s: float = 0.3) -> None:
    if turntable is None:
        print("USE_SERVO ist False - Servo kann DOA nicht folgen.")
        return

    from adapters.hardware.doa_respeaker import RespeakerDOAAdapter

    doa = RespeakerDOAAdapter()
    print(
        f"Live-DOA-Tracking gestartet (Servo-Bereich {config.SERVO_MIN_ANGLE}-"
        f"{config.SERVO_MAX_ANGLE} Grad geklemmt). Strg+C zum Beenden.\n"
        "Aus der Nase des Mikrofonarrays sprechen und beobachten, ob Servo/Augen folgen.\n"
        "Bewegung erfolgt nur bei erkannter Sprachaktivitaet (Mikrofon-VAD) und geglaettet -"
        " kurze/leise Stoergeraeusche bewegen den Motor nicht."
    )
    try:
        while True:
            if doa.get_voice_active():
                angle = doa.get_direction_degrees()
                print(f"DOA: {angle:.0f} Grad (Stimme aktiv)")
                turntable.rotate_towards(target_angle_degrees=angle)
                if face is not None:
                    face.set_eye_direction(angle_degrees=angle)
            else:
                print("... (still)")
            time.sleep(poll_interval_s)
    finally:
        doa.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Hardware-Tests: LED-Matrix, Servo, DOA-Tracking.")
    parser.add_argument(
        "--doa", action="store_true",
        help="Live-Tracking: Servo/Augen folgen der Richtung vom ReSpeaker Mic Array.",
    )
    args = parser.parse_args()

    logging_setup.configure_logging(config)

    bus = EventBus()
    matrix, status, face = build_display(bus)
    turntable = build_turntable()

    if status is None and turntable is None:
        print("Weder Display noch Servo aktiviert (USE_LED_MATRIX/USE_SERVO) - nichts zu testen.")
        return

    try:
        if args.doa:
            live_doa_tracking(turntable, face)
        else:
            demo_display(bus, status, face)
            demo_servo(turntable)
            print("Fertig. Strg+C zum Beenden, oder Enter fuer eine weitere Runde.")
            while True:
                input()
                demo_display(bus, status, face)
                demo_servo(turntable)
    except KeyboardInterrupt:
        print("\nCiao!")
    finally:
        if turntable is not None and hasattr(turntable, "stop"):
            turntable.stop()


if __name__ == "__main__":
    main()
