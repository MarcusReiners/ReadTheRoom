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
        # param, which pauses on the same Event. Checked directly here rather
        # than through set_eye_direction()/DOA angle plumbing, since a wrench
        # replaces the eyes entirely rather than being a pose of them.
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

        self._eye_r = max(4, min(self.height // 6, self.width // 10))
        self._dot_r = max(2, self._eye_r // 2)
        self._cy = self.height // 2
        self._left_cx = self.x_offset + int(self.width * 0.375)
        self._right_cx = self.x_offset + int(self.width * 0.625)

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

    def render(self, canvas, t: float) -> None:
        if self._calibration_mode is not None and self._calibration_mode.is_set():
            self._draw_wrench(canvas)
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

    def _draw_wrench(self, canvas) -> None:
        """A simple open-end-wrench glyph shown in place of the eyes while
        calibration_mode is set - a handle (thick diagonal bar) leading up to
        an open jaw (ring) at one end, small grip knob at the other. Built
        from the same _fill_circle/_draw_ring primitives as the eyes, stamped
        along a line rather than hand-authored pixel-by-pixel."""
        cx = self.x_offset + self.width // 2
        cy = self.height // 2
        span = min(self.width, self.height * 2) * 0.32
        bar_r = max(2, self._dot_r - 1)

        grip_x, grip_y = cx - span, cy + span * 0.55
        jaw_x, jaw_y = cx + span, cy - span * 0.55

        _stamp_line(canvas, grip_x, grip_y, jaw_x, jaw_y, bar_r, _WHITE)
        _fill_circle(canvas, int(round(grip_x)), int(round(grip_y)), bar_r + 1, _WHITE)
        _draw_ring(canvas, int(round(jaw_x)), int(round(jaw_y)), self._eye_r + 2, bar_r, _WHITE)


def _fill_circle(canvas, cx: int, cy: int, r: int, color: tuple) -> None:
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy <= r * r:
                canvas.SetPixel(cx + dx, cy + dy, *color)


def _draw_ring(canvas, cx: int, cy: int, outer_r: int, inner_r: int, color: tuple) -> None:
    for dy in range(-outer_r, outer_r + 1):
        for dx in range(-outer_r, outer_r + 1):
            dist_sq = dx * dx + dy * dy
            if inner_r * inner_r <= dist_sq <= outer_r * outer_r:
                canvas.SetPixel(cx + dx, cy + dy, *color)


def _stamp_line(canvas, x0: float, y0: float, x1: float, y1: float, r: int, color: tuple) -> None:
    """Thick line as filled circles stamped along the segment - gives
    naturally rounded ends without separate cap geometry."""
    length = math.hypot(x1 - x0, y1 - y0)
    steps = max(1, int(length))
    for i in range(steps + 1):
        frac = i / steps
        x = x0 + (x1 - x0) * frac
        y = y0 + (y1 - y0) * frac
        _fill_circle(canvas, int(round(x)), int(round(y)), r, color)
