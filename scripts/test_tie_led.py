import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup
from service_layer.bus import EventBus

HOLD_SECONDS = 4.0
WHITE = (255, 255, 255)

# (name, mode, period_ms) - white only, states differ by animation speed/mode.
STEPS = [
    ("Aus", "off", None),
    ("Solid (Speaking-Zustand)", "solid", None),
    ("Pulse langsam (Idle-Zustand, 3000ms)", "pulse", 3000),
    ("Pulse schnell (Listening-Zustand, 900ms)", "pulse", 900),
]


def send_led(radar, mode: str, period_ms: int | None) -> None:
    r, g, b = WHITE
    cmd = {"cmd": "set_led", "mode": mode, "r": r, "g": g, "b": b}
    if period_ms is not None:
        cmd["period_ms"] = period_ms
    radar.send_command(cmd)


def main() -> None:
    logging_setup.configure_logging(config)

    if config.RADAR_PROVIDER != "ld2450":
        print(
            f"RADAR_PROVIDER ist {config.RADAR_PROVIDER!r}, nicht 'ld2450' - "
            "die Tie-LED-Strip haengt am Bridge-ESP32, der nur ueber den "
            "echten Radar-Adapter erreichbar ist. Kein LED-Test moeglich."
        )
        return

    from adapters.factory import build_radar

    bus = EventBus()
    radar = build_radar(config, bus)
    radar.start()
    # Give the serial connection to the bridge a moment to come up before sending.
    time.sleep(1.0)

    print("Tie-LED-Test - jede Stufe haelt "
          f"{HOLD_SECONDS:.1f}s. Strg+C zum Abbrechen (LEDs werden dann ausgeschaltet).\n")

    try:
        while True:
            for name, mode, period_ms in STEPS:
                print(f"-> {name} ({mode}, period_ms={period_ms})")
                send_led(radar, mode, period_ms)
                time.sleep(HOLD_SECONDS)
            print("Cycle complete, Ctrl+C to quit or keep looping...\n")
    except KeyboardInterrupt:
        pass
    finally:
        send_led(radar, "off", None)
        if hasattr(radar, "stop"):
            radar.stop()
        print("\nLEDs off.")


if __name__ == "__main__":
    main()
