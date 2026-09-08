import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup
from domain.events import PersonCountChanged, RadarTargetsUpdated
from service_layer.bus import EventBus


def main() -> None:
    logging_setup.configure_logging(config)

    if config.RADAR_PROVIDER != "ld2450":
        print(f"RADAR_PROVIDER is {config.RADAR_PROVIDER!r}, not 'ld2450' - nothing to test.")
        return

    from adapters.factory import build_radar

    bus = EventBus()
    bus.subscribe(RadarTargetsUpdated, lambda e: print(f"[Targets] {e.targets}"))
    bus.subscribe(PersonCountChanged, lambda e: print(f"[PersonCount] {e.count}"))

    radar = build_radar(config, bus)
    print(
        "Radar/ESP-NOW-Test - nur die Bridge-Verbindung und ankommende Sensordaten, "
        "kein Servo/Matrix/STT/LLM/TTS. Ctrl+C to quit.\n"
        f"Port: {config.RADAR_SERIAL_PORT}\n"
    )
    radar.start()

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        if hasattr(radar, "stop"):
            radar.stop()
        print("\nBye!")


if __name__ == "__main__":
    main()
