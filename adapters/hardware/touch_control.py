import logging
import queue
import threading
import time

logger = logging.getLogger(__name__)


class TouchControl:
    """A capacitive touch pad and a vibration motor on the Pi's GPIO, driven
    through the pigpio daemon the servo already uses.

    A tap calls on_tap once, on its own thread. A touch has to stay steady for
    min_touch_s before it counts (pigpio's glitch filter), and taps closer
    together than min_gap_s count as one, so a brush of the hand or
    electrical noise does nothing. buzz() plays a pattern of pulses on the
    motor without blocking the caller."""

    def __init__(self, touch_pin: int, motor_pin: int, on_tap, min_touch_s: float = 0.05,
                 min_gap_s: float = 0.8, pulse_gap_s: float = 0.12):
        import pigpio
        self._pigpio = pigpio
        self.pi = pigpio.pi()
        if not self.pi.connected:
            raise RuntimeError("the pigpio daemon is not running (sudo systemctl enable --now pigpiod)")
        self.touch_pin, self.motor_pin, self.on_tap = touch_pin, motor_pin, on_tap
        self.min_gap_s, self.pulse_gap_s = min_gap_s, pulse_gap_s
        self._last_tap = 0.0
        self._patterns: "queue.Queue[tuple[float, ...]]" = queue.Queue()
        self.pi.set_mode(motor_pin, pigpio.OUTPUT)
        self.pi.write(motor_pin, 0)
        self.pi.set_mode(touch_pin, pigpio.INPUT)
        self.pi.set_pull_up_down(touch_pin, pigpio.PUD_DOWN)
        self.pi.set_glitch_filter(touch_pin, int(min_touch_s * 1_000_000))
        self._callback = self.pi.callback(touch_pin, pigpio.RISING_EDGE, self._edge)
        threading.Thread(target=self._motor_loop, daemon=True, name="vibration").start()

    def buzz(self, *pulses_s: float) -> None:
        self._patterns.put(pulses_s or (0.15,))

    def close(self) -> None:
        self._callback.cancel()
        self.pi.write(self.motor_pin, 0)
        self.pi.stop()

    def _edge(self, gpio, level, tick) -> None:
        now = time.monotonic()
        if now - self._last_tap < self.min_gap_s:
            return
        self._last_tap = now
        threading.Thread(target=self._tap, daemon=True, name="touch-tap").start()

    def _tap(self) -> None:
        try:
            self.on_tap()
        except Exception:
            logger.exception("[Touch] Handling a tap failed.")

    def _motor_loop(self) -> None:
        while True:
            pulses = self._patterns.get()
            try:
                for i, pulse_s in enumerate(pulses):
                    if i:
                        time.sleep(self.pulse_gap_s)
                    self.pi.write(self.motor_pin, 1)
                    time.sleep(max(0.0, min(pulse_s, 2.0)))
                    self.pi.write(self.motor_pin, 0)
            except Exception:
                logger.exception("[Touch] Vibration motor failed.")
                try:
                    self.pi.write(self.motor_pin, 0)
                except Exception:
                    pass
