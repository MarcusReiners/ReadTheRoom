import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serial

import config

PORT = config.RADAR_SERIAL_PORT
BAUD = config.RADAR_SERIAL_BAUD


def listen(seconds: float) -> int:
    status = 0
    with serial.Serial(PORT, BAUD, timeout=0.5) as ser:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            line = ser.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                json.loads(line)
                status += 1
            except ValueError:
                print(f"    bridge says: {line}")
    return status


def set_led(ser: serial.Serial, r: int, g: int, b: int, mode: str = "solid") -> None:
    ser.write((json.dumps({"cmd": "set_led", "mode": mode, "r": r, "g": g, "b": b}) + "\n").encode())


def led_test() -> None:
    with serial.Serial(PORT, BAUD, timeout=0.5) as ser:
        set_led(ser, 255, 0, 0)
        time.sleep(2.0)
        set_led(ser, 0, 0, 255)
        time.sleep(2.0)
        set_led(ser, 0, 0, 0, mode="off")


def hard_reset() -> bool:
    with serial.Serial(PORT, BAUD) as ser:
        ser.dtr = False
        ser.rts = True
        time.sleep(0.2)
        ser.rts = False
        time.sleep(0.2)
    time.sleep(1.5)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if os.path.exists(PORT):
            time.sleep(0.5)
            return True
        time.sleep(0.2)
    return False


def main() -> None:
    if not os.path.exists(PORT):
        raise SystemExit(f"{PORT} does not exist - the bridge is not on USB at all.")
    print(f"Bridge port {PORT}. Stop the main app and other radar scripts first.\n")

    print("1) Listening 4 s for radar packets as they are now...")
    before = listen(4.0)
    print(f"   {before} packets\n")

    print("2) LED test - watch the tie LED: red for 2 s, then blue for 2 s, then off.")
    led_test()
    print("   done\n")

    print("3) Restarting the bridge through USB (same as unplugging it)...")
    if not hard_reset():
        raise SystemExit(f"   {PORT} did not come back within 10 s - check `dmesg | tail`.")
    print("   back. Listening 8 s...")
    after = listen(8.0)
    print(f"   {after} packets\n")

    if before:
        print("Packets were arriving before the restart - the radar link was working.")
    elif after:
        print("The bridge had hung; the restart fixed it. Radar data is flowing again.")
    else:
        print("A freshly restarted bridge receives nothing, so the problem is on the sensor side:")
        print("  - LED reacted in step 2: the bridge itself is fine; the sensor unit is not sending")
        print("    (no power, or hung) - check its power supply and power-cycle it.")
        print("  - LED did not react: the bridge firmware is not running properly either.")


if __name__ == "__main__":
    main()
