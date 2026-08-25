import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup
from service_layer.bus import EventBus
from service_layer.handlers import start_doa_tracking


def main() -> None:
    logging_setup.configure_logging(config)

    if not config.USE_SERVO:
        print("USE_SERVO ist False - kein Servo zum Tracken.")
        return

    from adapters.factory import build_turntable
    from adapters.hardware.doa_respeaker import RespeakerDOAAdapter
    from adapters.hardware.face_display import DummyFaceDisplayAdapter

    bus = EventBus()
    turntable = build_turntable(config)
    doa = RespeakerDOAAdapter(front_reference_degrees=config.DOA_FRONT_REFERENCE_DEGREES)
    # No LED matrix here - only the servo needs to move, so the "face" is a
    # no-op sink rather than pulling in matrix hardware/permissions.
    face = DummyFaceDisplayAdapter(bus=bus)

    # Same production code path main.py uses (service_layer/handlers.py) -
    # not a reimplementation, so this behaves identically to the real
    # assistant's tracking, just standalone without STT/LLM/TTS/radar/matrix.
    start_doa_tracking(doa, turntable, face)

    # Read off the turntable, not config - the safe range is editable from
    # the web app, so the configured default isn't necessarily what's active.
    print(
        "Audio-Tracking laeuft - der Servo folgt jeder erkannten Stimme "
        f"(sicherer Bereich {turntable.safe_min_angle:.0f}-{turntable.safe_max_angle:.0f} Grad).\n"
        "Strg+C zum Beenden."
    )

    try:
        while True:
            input()
    except KeyboardInterrupt:
        pass
    finally:
        doa.close()
        turntable.stop()
        print("\nServo freigegeben. Ciao!")


if __name__ == "__main__":
    main()
