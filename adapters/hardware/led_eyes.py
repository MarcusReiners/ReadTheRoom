import math
import time

from service_layer.bus import EventBus

_WHITE = (255, 255, 255)


class LedEyesAdapter:
    """LED-matrix renderer drawing two plain white round eyes side by side.

    Same role as the old HDMI face display, but as a matrix renderer
    (`render(canvas, t)`) registered on `LedMatrix` instead of owning its
    own pygame window/thread.

    `angle_degrees` follows the same DOA convention as `ServoTurntableAdapter`:
    90 degrees means straight ahead.
    """

    BLINK_INTERVAL = 4.5
    BLINK_DURATION = 0.15

    # Normal in-window tracking uses 60% of the pupil's available travel,
    # leaving a visible reserve. When the DOA target overshoots the servo's
    # safe window (the head physically can't turn any further), that reserve
    # is spent proportionally to how far past the edge the target is - the
    # eyes "strain" further in that direction on top of the head's own
    # maxed-out contribution, instead of jumping straight to a fixed pinned
    # position regardless of overshoot size.
    _BASE_OFFSET_FRACTION = 0.6
    _MAX_OFFSET_FRACTION = 0.95
    _COMPENSATION_RANGE_DEGREES = 45.0

    def __init__(
        self,
        bus: EventBus,
        x_offset: int = 0,
        width: int = 96,
        height: int = 48,
        min_angle_degrees: float = 0.0,
        max_angle_degrees: float = 180.0,
        calibration_mode=None,
    ) -> None:
        self.x_offset = x_offset
        self.width = width
        self.height = height
        # Set for as long as the web app's settings tab (servo home
        # calibration) is open - see start_doa_tracking()'s calibration_mode
        # param, which pauses on the same Event. Checked directly here (in
        # render(), driving the openness animation below) rather than
        # through set_eye_direction()/DOA angle plumbing, since "closed eyes"
        # replaces the normal open-eye rendering entirely.
        self._calibration_mode = calibration_mode
        # DOA readings can report angles well outside the servo's safe
        # window (the head can't physically turn that far) - clamping here
        # pins the eyes at the matrix edge in that direction instead of the
        # raw sin() wrapping back through center past +-90 degrees off
        # straight-ahead, which would show the eyes snapping back to
        # dead-center while the sound is actually further off to one side.
        self._min_angle_degrees = min_angle_degrees
        self._max_angle_degrees = max_angle_degrees
        self._eye_angle = 90.0

        # animate_eye_direction()'s state - a separate wall-clock-driven
        # transition, independent of render(t)'s own blink clock, since it's
        # triggered from outside the render loop (the DOA tracking thread).
        self._anim_start_angle = 90.0
        self._anim_target_angle = 90.0
        self._anim_start_time = 0.0
        self._anim_duration_s = 0.0

        # Openness animation for calibration_mode: 1.0 = normal open eyes,
        # 0.0 = fully closed (a flat line) - same wall-clock-driven pattern
        # as _current_angle() above, just for a different property. Detected
        # and (re)started from render() itself by diffing calibration_mode
        # against _was_calibrating each frame, since that's the only place
        # polling the Event anyway.
        self._openness = 1.0
        self._openness_anim_start = 1.0
        self._openness_anim_target = 1.0
        self._openness_anim_start_time = 0.0
        self._openness_anim_duration_s = 0.0
        self._was_calibrating = False

        self._eye_r = max(4, min(self.height // 6, self.width // 10))
        self._dot_r = max(2, self._eye_r // 2)
        self._cy = self.height // 2
        self._left_cx = self.x_offset + int(self.width * 0.375)
        self._right_cx = self.x_offset + int(self.width * 0.625)

    def set_eyes_closed(self, closed: bool, duration_s: float = 0.4) -> None:
        """Closes or reopens the eyes, for the redirect signal.

        Closed eyes say the assistant has withdrawn its attention, which is
        the opposite of turning towards someone: the point of the signal is
        that the visitor is not being addressed. The blink clock in render()
        keeps running underneath, so reopening returns to normal blinking.
        """
        self._start_openness_animation(0.0 if closed else 1.0, duration_s)

    def set_eye_direction(self, angle_degrees: float) -> None:
        """Instant snap - cancels any in-progress animate_eye_direction()."""
        self._eye_angle = angle_degrees
        self._anim_duration_s = 0.0

    def animate_eye_direction(self, to_angle_degrees: float, duration_s: float) -> None:
        """Smoothly transitions from the current (possibly still-animating)
        eye angle to to_angle_degrees over duration_s, computed each frame in
        render() from elapsed wall time - e.g. eyes drifting back to center
        over the same span the head takes to physically catch up, instead of
        snapping back the instant the head starts moving."""
        self._anim_start_angle = self._current_angle()
        self._anim_target_angle = to_angle_degrees
        self._anim_start_time = time.monotonic()
        self._anim_duration_s = max(duration_s, 0.001)

    def _current_angle(self) -> float:
        if self._anim_duration_s <= 0:
            return self._eye_angle
        elapsed = time.monotonic() - self._anim_start_time
        if elapsed >= self._anim_duration_s:
            self._eye_angle = self._anim_target_angle
            self._anim_duration_s = 0.0
            return self._eye_angle
        progress = elapsed / self._anim_duration_s
        return self._anim_start_angle + (self._anim_target_angle - self._anim_start_angle) * progress

    _CLOSE_ANIM_DURATION_S = 0.35

    def _start_openness_animation(self, target: float, duration_s: float) -> None:
        self._openness_anim_start = self._current_openness()
        self._openness_anim_target = target
        self._openness_anim_start_time = time.monotonic()
        self._openness_anim_duration_s = max(duration_s, 0.001)

    def _current_openness(self) -> float:
        if self._openness_anim_duration_s <= 0:
            return self._openness
        elapsed = time.monotonic() - self._openness_anim_start_time
        if elapsed >= self._openness_anim_duration_s:
            self._openness = self._openness_anim_target
            self._openness_anim_duration_s = 0.0
            return self._openness
        progress = elapsed / self._openness_anim_duration_s
        return self._openness_anim_start + (self._openness_anim_target - self._openness_anim_start) * progress

    def render(self, canvas, t: float) -> None:
        calibrating = self._calibration_mode is not None and self._calibration_mode.is_set()
        if calibrating != self._was_calibrating:
            self._start_openness_animation(0.0 if calibrating else 1.0, self._CLOSE_ANIM_DURATION_S)
            self._was_calibrating = calibrating

        openness = self._current_openness()
        if openness < 0.999:
            self._draw_closing_eyes(canvas, openness)
            return

        blinking = (t % self.BLINK_INTERVAL) < self.BLINK_DURATION

        current_angle = self._current_angle()
        clamped_angle = max(self._min_angle_degrees, min(self._max_angle_degrees, current_angle))
        overshoot = max(0.0, current_angle - self._max_angle_degrees, self._min_angle_degrees - current_angle)
        overshoot_fraction = min(overshoot / self._COMPENSATION_RANGE_DEGREES, 1.0)
        offset_fraction = self._BASE_OFFSET_FRACTION + overshoot_fraction * (
            self._MAX_OFFSET_FRACTION - self._BASE_OFFSET_FRACTION
        )

        rad = math.radians(clamped_angle - 90.0)
        px = int((self._eye_r - self._dot_r) * offset_fraction * math.sin(rad))

        for cx in (self._left_cx, self._right_cx):
            if blinking:
                for dx in range(-self._dot_r, self._dot_r + 1):
                    canvas.SetPixel(cx + dx, self._cy, *_WHITE)
                continue
            _fill_circle(canvas, cx + px, self._cy, self._dot_r, _WHITE)

    def _draw_closing_eyes(self, canvas, openness: float) -> None:
        """Both eyes shown mid-blink-close (or opening back up), driven by
        the openness animation rather than the normal periodic blink timer -
        used while calibration_mode is set, and during the brief transition
        in/out of it. At openness=1 this would be identical to a normal open
        pupil (_dot_r circle); at openness=0 it's a flat line spanning each
        eye's full width (_eye_r) - width grows and height shrinks together
        as it closes, so the pupil visibly morphs into a shut eyelid instead
        of just vanishing."""
        width_r = max(1, round(self._dot_r + (self._eye_r - self._dot_r) * (1 - openness)))
        height_r = max(1, round(self._dot_r * openness))
        for cx in (self._left_cx, self._right_cx):
            _fill_ellipse(canvas, cx, self._cy, width_r, height_r, _WHITE)


def _fill_circle(canvas, cx: int, cy: int, r: int, color: tuple) -> None:
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                canvas.SetPixel(cx + dx, cy + dy, *color)


def _fill_ellipse(canvas, cx: int, cy: int, rx: int, ry: int, color: tuple) -> None:
    for dy in range(-ry, ry + 1):
        for dx in range(-rx, rx + 1):
            if (dx * dx) / (rx * rx) + (dy * dy) / (ry * ry) <= 1.0:
                canvas.SetPixel(cx + dx, cy + dy, *color)
