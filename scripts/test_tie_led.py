import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup
from service_layer.bus import EventBus

HOLD_SECONDS = 2.5

# (name, mode, r, g, b)
STEPS = [
    ("Aus", "off", 0, 0, 0),
    ("Solid Rot", "solid", 200, 20, 20),
    ("Solid Gruen", "solid", 20, 180, 60),
    ("Solid Blau", "solid", 0, 60, 200),
    ("Solid Gold (Speaking-Farbe)", "solid", 200, 160, 90),
    ("Pulse Blau (Idle-Farbe)", "pulse", 0, 60, 160),
    ("Pulse Rot (Listening-Farbe)", "pulse", 160, 20, 20),
]


def send_led(radar, mode: str, r: int, g: int, b: int) -> None:
    radar.send_command({"cmd": "set_led", "mode": mode, "r": r, "g": g, "b": b})


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
            for name, mode, r, g, b in STEPS:
                print(f"-> {name} ({mode}, r={r} g={g} b={b})")
                send_led(radar, mode, r, g, b)
                time.sleep(HOLD_SECONDS)
            print("Durchlauf fertig, Strg+C zum Beenden oder weiter im Kreis...\n")
    except KeyboardInterrupt:
        pass
    finally:
        send_led(radar, "off", 0, 0, 0)
        if hasattr(radar, "stop"):
            radar.stop()
        print("\nLEDs aus. Ciao!")


if __name__ == "__main__":
    main()
