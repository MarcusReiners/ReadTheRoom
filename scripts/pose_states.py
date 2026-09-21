"""Holds the assistant in each of its visible states, one at a time, so each
one can be photographed for the thesis figure.

The states and the commands that produce them are the ones
register_tie_led_handlers() in service_layer/handlers.py sends in response to
real events; the timing constants and the LED command builder are imported
from there rather than copied, so this script cannot drift from the deployed
behaviour. Nothing is spoken and no audio is played: the script sets the
display and the strip directly, which is what a photograph needs.

By default it waits for Enter between states, so the camera can be set up
without a clock running. --hold runs it on a timer instead, for a tripod and
a self-timer.

    # on the Pi, with the prototype assembled
    python3 scripts/pose_states.py

    # timed run, five seconds per state
    python3 scripts/pose_states.py --hold 5

    # redo a single shot
    python3 scripts/pose_states.py --only redirected

One caveat the camera cannot get around: idle, listening and speaking differ
only in the strip's animation (a 3 s pulse, a 0.9 s pulse, and steady). A
still photograph of the three shows the same white strip, so the figure has
to name the difference in its caption rather than show it.
"""

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config
import logging_setup
from service_layer.bus import EventBus
from service_layer.handlers import (
    TIE_LED_IDLE_PERIOD_MS,
    TIE_LED_LISTENING_PERIOD_MS,
    TIE_LED_ROOM_CLEAR,
    _tie_led_command,
)

EYES_FRONT_DEGREES = 90.0  # same DOA convention as the servo: 90 is straight ahead


# (key, caption label, led command, eyes closed, what the photo should show)
STATES = [
    ("idle", "idle",
     _tie_led_command("pulse", TIE_LED_IDLE_PERIOD_MS), False,
     "eyes open and centred, strip pulsing slowly in white (3 s cycle)"),
    ("listening", "listening",
     _tie_led_command("pulse", TIE_LED_LISTENING_PERIOD_MS), False,
     "eyes open, strip pulsing quickly in white (0.9 s cycle)"),
    ("speaking", "speaking",
     _tie_led_command("solid"), False,
     "eyes open, strip steady white"),
    ("redirected", "output redirected",
     _tie_led_command("off"), True,
     "eyes closed, strip dark -- the state that must not look like a crash"),
    ("room-clear", "room judged clear",
     _tie_led_command("solid", color=TIE_LED_ROOM_CLEAR), True,
     "eyes closed, strip glowing faintly: waiting to be told it may speak"),
]


def build_display(bus: EventBus):
    """The eyes, on the LED matrix. Mirrors scripts/test_hardware.py."""
    if not config.USE_LED_MATRIX:
        print("USE_LED_MATRIX is False -- no eyes to pose.")
        return None, None

    from adapters.hardware.led_matrix import LedMatrix
    from adapters.hardware.led_eyes import LedEyesAdapter
    from adapters.app_settings import load_servo_range

    servo_min, servo_max = load_servo_range(
        config.APP_SETTINGS_PATH, config.SERVO_MIN_ANGLE, config.SERVO_MAX_ANGLE,
    )
    matrix = LedMatrix(
        rows=48, cols=96, chain=1,
        gpio_slowdown=config.GPIO_SLOWDOWN,
        brightness=config.LED_MATRIX_BRIGHTNESS,
        pwm_bits=config.LED_MATRIX_PWM_BITS,
    )
    face = LedEyesAdapter(
        bus=bus, x_offset=0, width=96, height=48,
        min_angle_degrees=servo_min, max_angle_degrees=servo_max,
    )
    matrix.add_renderer(face)
    matrix.start()
    return matrix, face


def build_strip(bus: EventBus):
    """The tie strip hangs off the bridge ESP32, reachable only through the
    radar adapter's serial link (the same channel the deployed code uses)."""
    if config.RADAR_PROVIDER != "ld2450":
        print(f"RADAR_PROVIDER is {config.RADAR_PROVIDER!r}, not 'ld2450' -- "
              "the strip cannot be driven, so only the eyes will change.")
        return None

    from adapters.factory import build_radar

    radar = build_radar(config, bus)
    radar.start()
    time.sleep(1.0)  # let the serial link to the bridge come up before sending
    return radar


def centre_head():
    """Puts the head at its home bearing so all five frames line up."""
    if not config.USE_SERVO:
        return None
    from adapters.factory import build_turntable

    turntable = build_turntable(config)
    lo, hi = turntable.safe_min_angle, turntable.safe_max_angle
    turntable.set_doa_angle_immediate(target_angle_degrees=(lo + hi) / 2)
    time.sleep(1.0)
    return turntable


def apply(state, radar, face) -> None:
    _, _, command, eyes_closed, _ = state
    if face is not None:
        face.set_eye_direction(angle_degrees=EYES_FRONT_DEGREES)
        face.set_eyes_closed(eyes_closed)
    if radar is not None:
        radar.send_command(command)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hold", type=float,
                        help="seconds to hold each state instead of waiting for Enter")
    parser.add_argument("--only", help="comma-separated subset: "
                                       + ", ".join(s[0] for s in STATES))
    parser.add_argument("--no-servo", action="store_true",
                        help="leave the head where it is instead of centring it")
    args = parser.parse_args()

    wanted = [s.strip() for s in args.only.split(",")] if args.only else None
    states = [s for s in STATES if wanted is None or s[0] in wanted]
    if not states:
        raise SystemExit(f"no state matched {args.only!r}")

    logging_setup.configure_logging(config)
    bus = EventBus()
    matrix, face = build_display(bus)
    radar = build_strip(bus)
    turntable = None if args.no_servo else centre_head()

    print(f"\nPosing {len(states)} state(s). The eyes close for the last two.\n"
          "Shoot all of them from the same seat without moving the camera, so the\n"
          "row reads as one device changing rather than five different devices.\n")
    try:
        for i, state in enumerate(states, 1):
            key, label, _, _, expect = state
            apply(state, radar, face)
            print(f"[{i}/{len(states)}] {label}  ({key})")
            print(f"        camera should see: {expect}")
            if args.hold:
                time.sleep(args.hold)
            else:
                input("        Enter for the next state... ")
            print()
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        # Leave the device in idle rather than in whichever state came last.
        if face is not None:
            face.set_eyes_closed(False)
            face.set_eye_direction(angle_degrees=EYES_FRONT_DEGREES)
        if radar is not None:
            radar.send_command(_tie_led_command("pulse", TIE_LED_IDLE_PERIOD_MS))
            time.sleep(0.3)
            if hasattr(radar, "stop"):
                radar.stop()
        if matrix is not None and hasattr(matrix, "stop"):
            matrix.stop()
        if turntable is not None and hasattr(turntable, "stop"):
            turntable.stop()
        print("back to idle.")


if __name__ == "__main__":
    main()
