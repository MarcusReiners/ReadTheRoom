import logging
import math
import time

logger = logging.getLogger(__name__)


def _wrap180(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def clean_pose(pose) -> dict | None:
    """Where the assistant stands, in the radar's own coordinates - what the
    web app's radar view lets a person drag.

    x_mm and y_mm are the head's position in the same frame the target dots
    are drawn in, and facing_degrees is the direction its nose points when at
    home: 0 straight out from the sensor, growing towards +x, which is the
    convention the radar view already draws its field of view in.
    """
    if not isinstance(pose, dict):
        return None
    try:
        cleaned = {
            "x_mm": float(pose["x_mm"]),
            "y_mm": float(pose["y_mm"]),
            "facing_degrees": _wrap180(float(pose.get("facing_degrees", 0.0))),
            "mirrored": bool(pose.get("mirrored", True)),
            "placed": True,
        }
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in cleaned.values() if isinstance(v, float)):
        return None
    return cleaned


def target_bearing(x_mm: float, y_mm: float, pose: dict) -> tuple[float, float]:
    """A radar target as (bearing, distance_mm) seen from the head, the
    bearing in the convention the DOA and the servo share: 90 is straight
    ahead of the head's home position.

    pose["mirrored"] decides which way round left and right are. Nothing can
    derive it: the sensor's lateral axis has whatever sign the module and its
    mounting give it, the radar view draws +x to the right either way, and a
    person placing the assistant on that view places it in the drawn frame.
    If the drawn frame is the mirror image of the room then every bearing
    computed in it is reflected about straight ahead, which shows up as the
    head turning right for a talker on the left while a talker straight
    ahead stays correct. Measured mirrored on this installation on 25
    October 2026, hence the default; the radar view has a switch for it, and
    probe_radar_alignment.py fits both and keeps whichever matches.
    """
    dx = x_mm - pose["x_mm"]
    dy = y_mm - pose["y_mm"]
    towards = math.degrees(math.atan2(dx, dy))
    sign = -1.0 if pose.get("mirrored", True) else 1.0
    return 90.0 + sign * _wrap180(towards - pose["facing_degrees"]), math.hypot(dx, dy)


def pose_from_marks(pairs: list[tuple[tuple[float, float], tuple[float, float]]]) -> dict:
    """The same pose fitted from marks instead of dragged.

    pairs are ((radar_x_mm, radar_y_mm), (bearing_degrees, distance_mm)) for
    places a person stood that the head itself pointed out, which is what
    scripts/probe_radar_alignment.py collects. Rotation and translation only,
    by the usual centred cross and dot products: both frames are in
    millimetres and neither is scaled.
    """
    if len(pairs) < 3:
        raise ValueError("at least three marks are needed")
    best = None
    for mirrored in (False, True):
        pose = _fit_one(pairs, mirrored)
        worst = max(b for b, _ in pose["mark_errors"])
        if best is None or worst < best[0]:
            best = (worst, pose)
    return best[1]


def _fit_one(pairs, mirrored: bool) -> dict:
    heads = []
    for _, (bearing, distance) in pairs:
        offset = math.radians(bearing - 90.0)
        hx = distance * math.sin(offset)
        heads.append((-hx if mirrored else hx, distance * math.cos(offset)))
    radars = [p[0] for p in pairs]

    rx = sum(p[0] for p in radars) / len(radars)
    ry = sum(p[1] for p in radars) / len(radars)
    hx = sum(p[0] for p in heads) / len(heads)
    hy = sum(p[1] for p in heads) / len(heads)
    cross = sum((h[0] - hx) * (r[1] - ry) - (h[1] - hy) * (r[0] - rx) for r, h in zip(radars, heads))
    dot = sum((h[0] - hx) * (r[0] - rx) + (h[1] - hy) * (r[1] - ry) for r, h in zip(radars, heads))
    if cross == 0.0 and dot == 0.0:
        raise ValueError("the marks are all in the same place - spread them out")

    # theta rotates head coordinates into radar ones. A bearing grows the
    # other way round from a mathematical angle - the head's +x axis sits at
    # bearing 180, a quarter turn clockwise of its nose - so the facing is
    # the negative of it, and getting that sign wrong leaves the position
    # exactly right while every bearing comes out mirrored.
    theta = math.atan2(cross, dot)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    pose = {
        "x_mm": rx - (cos_t * hx - sin_t * hy),
        "y_mm": ry - (sin_t * hx + cos_t * hy),
        "facing_degrees": _wrap180(-math.degrees(theta)),
        "mirrored": mirrored,
        "placed": True,
    }
    errors = []
    for (radar_xy, (bearing, distance)) in pairs:
        got_bearing, got_distance = target_bearing(radar_xy[0], radar_xy[1], pose)
        errors.append((round(abs(_wrap180(got_bearing - bearing)), 1),
                       round(abs(got_distance - distance))))
    pose["mark_errors"] = errors
    return pose


class RadarBearings:
    """The radar's current targets, as bearings in the head's frame.

    Subscribes to RadarTargetsUpdated and keeps only the latest frame, so a
    caller always gets what the sensor sees now rather than a history. A
    frame older than max_age_s counts as nothing seen: the sensor unit runs
    on a power bank and goes quiet when it is out of range or flat, and a
    stale position would otherwise keep steering the head towards where
    somebody used to be.

    The pose comes from a callable rather than a value, so dragging the
    assistant to a new spot in the web app takes effect on the next utterance
    instead of at the next restart.
    """

    def __init__(self, bus, pose, max_age_s: float = 1.5) -> None:
        from domain.events import RadarTargetsUpdated

        self._pose = pose if callable(pose) else (lambda: pose)
        self.max_age_s = max_age_s
        self._targets: list = []
        self._at = 0.0
        bus.subscribe(RadarTargetsUpdated, self._on_update)

    def _on_update(self, event) -> None:
        self._targets = list(event.targets)
        self._at = time.monotonic()

    def __call__(self) -> list[tuple[float, float]]:
        pose = self._pose()
        if not pose or not pose.get("placed"):
            return []
        if not self._at or time.monotonic() - self._at > self.max_age_s:
            return []
        out = []
        for target in self._targets:
            x, y = target.get("x_mm"), target.get("y_mm")
            if x is None or y is None:
                continue
            try:
                out.append(target_bearing(float(x), float(y), pose))
            except (TypeError, ValueError, KeyError):
                continue
        return out
