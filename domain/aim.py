def _wrap180(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def radar_share(gap_degrees: float, agree_degrees: float, gate_degrees: float,
                agree_weight: float) -> float:
    """How much of the radar's bearing to use, given how far it sits from the
    sound's.

    Ramped rather than switched. A step at the gate would mean a degree of
    jitter either side of it swinging the head by tens of degrees, which is
    the same class of bug as the branch cut that used to send it to the wrong
    end stop.
    """
    if gap_degrees <= agree_degrees:
        return agree_weight
    if gap_degrees >= gate_degrees:
        return 1.0
    span = gate_degrees - agree_degrees
    return agree_weight + (1.0 - agree_weight) * (gap_degrees - agree_degrees) / span


def fuse_bearing(doa_bearing: float, radar_targets: list[tuple[float, float]],
                 agree_weight: float = 0.25, agree_degrees: float = 10.0,
                 gate_degrees: float = 45.0, near_mm: float = 2500.0,
                 bias_degrees: float = 0.0) -> tuple[float | None, str]:
    """Where to aim the head, from the microphone array and the radar together.

    radar_targets are (bearing, distance_mm) pairs in the head's own frame -
    see adapters/radar_alignment.py.

    Which instrument leads depends on whether they agree.

    When they do, the sound leads: agree_weight of the move goes to the
    radar and the rest to the array. The array is then measuring the mouth,
    which is what the head should face, while the radar reports the middle of
    a body - at 1.2 m a person standing side-on puts those about 10 degrees
    apart. The array is also the only evidence of who is talking at all.

    When they disagree the radar takes over, because the array's failure is
    not noise but confidence: measured on 25 October 2026, a talker 1.25 m
    away on one side was reported 178 degrees out, stably, for twelve
    seconds, and a reflection has no body for the radar to see. Between
    agreement and the gate the weight ramps across, so nothing jumps.

    Only people within near_mm are candidates. Someone across the room is
    visible to the radar while the person actually talking - sitting still at
    the desk - is not, since the LD2450 loses a motionless body. Without that
    limit a visitor by the door would pull the head off the user mid-sentence.

    bias_degrees is the operator's own correction from the web app, added to
    the sound's bearing before anything else.

    Returns (bearing, why), with bearing None when the head should not move.
    """
    doa = doa_bearing + bias_degrees
    near = [(b, d) for b, d in radar_targets if d <= near_mm]
    if not near:
        if radar_targets:
            return doa, (f"the radar only sees {len(radar_targets)} person(s) beyond "
                         f"{near_mm / 1000.0:.1f} m, going on the sound alone")
        return doa, "the radar sees nobody, going on the sound alone"

    nearest, distance = min(near, key=lambda t: abs(_wrap180(t[0] - doa)))
    gap = _wrap180(nearest - doa)

    if abs(gap) >= gate_degrees:
        if len(near) == 1:
            return nearest, (
                f"sound {doa:.0f} is {abs(gap):.0f} deg off the one person close by, at "
                f"{nearest:.0f} ({distance / 1000.0:.1f} m) - the sound looks like a reflection, "
                f"going with the radar")
        return None, (
            f"sound {doa:.0f} matches none of the {len(near)} people close by (nearest "
            f"{nearest:.0f}, {abs(gap):.0f} deg off) - not moving")

    share = radar_share(abs(gap), agree_degrees, gate_degrees, agree_weight)
    leader = "sound" if share < 0.5 else "radar"
    return doa + share * gap, (
        f"sound {doa:.0f} and radar {nearest:.0f} ({distance / 1000.0:.1f} m) agree to "
        f"{abs(gap):.0f} deg - {leader} leads, {share:.0%} to the radar")


class IdleGaze:
    """Where the eyes rest while nobody is talking.

    The radar sees people whether or not they speak, so between utterances
    the eyes can follow whoever is nearest instead of staring ahead. Only the
    eyes: the head stays put, because a servo that follows a body around is
    noisy, visibly restless, and moves the microphone array for no reason.

    Not every frame, either. The radar jumps - it swaps target ids, loses a
    motionless body for a frame and finds it again, and briefly reports
    ghosts - so the recent frames are kept and their median used as a gate
    rather than as the answer: the newest reading is followed unless it
    disagrees with that median by more than jump_degrees, in which case it is
    dropped as a ghost. Taking the median itself would be steadier and
    useless, since this is polled a few times a second and the median of a
    window that long trails a walking person by over a second.

    A move smaller than deadband_degrees is dropped as well, which is what
    stops the eyes shivering around somebody standing still.
    """

    def __init__(self, window: int = 8, deadband_degrees: float = 4.0,
                 jump_degrees: float = 25.0, min_share: float = 0.4) -> None:
        self.window = max(3, window)
        self.deadband_degrees = deadband_degrees
        self.jump_degrees = jump_degrees
        self.min_share = min_share
        self._seen: list[float | None] = []
        self._commanded = 90.0

    def update(self, bearings: list[tuple[float, float]]) -> float | None:
        """Takes the radar's people as (bearing, distance_mm) relative to the
        nose, and returns an eye angle to command, or None to leave the eyes
        where they are."""
        nearest = min(bearings, key=lambda t: t[1])[0] if bearings else None
        self._seen.append(nearest)
        del self._seen[:-self.window]

        present = [b for b in self._seen if b is not None]
        if len(present) < max(2, round(self.min_share * self.window)):
            # Nobody there, or too intermittent to believe: rest the eyes
            # ahead rather than holding the last place somebody was seen.
            return self._settle(90.0)
        if nearest is None:
            return None
        ordered = sorted(present)
        middle = len(ordered) // 2
        median = (ordered[middle] if len(ordered) % 2
                  else (ordered[middle - 1] + ordered[middle]) / 2.0)
        if abs(_wrap180(nearest - median)) > self.jump_degrees:
            return None
        return self._settle(nearest)

    def note_eyes(self, angle: float) -> None:
        """Tells the gaze where the eyes were just sent by someone else - a
        glance at a noise, say. Without it the deadband would measure the
        next move from a position the eyes are no longer in, and swallow it."""
        self._commanded = angle

    def _settle(self, target: float) -> float | None:
        if abs(_wrap180(target - self._commanded)) < self.deadband_degrees:
            return None
        self._commanded = target
        return target


def glance_bearing(rough_bearing: float, radar_targets: list[tuple[float, float]],
                   near_mm: float = 2500.0, gate_degrees: float = 60.0) -> tuple[float, str]:
    """Where to look at a noise that was not speech - a knock, a chair, keys
    on the desk, somebody closing a book.

    A rough direction is enough here, which is the point: the readings behind
    it are the ones the head refuses to turn on, taken at the onset of a
    sound too short to place, off by 11 degrees at the median and 45 at the
    ninth decile. Eyes can be wrong by that much and simply look again; a
    servo cannot.

    If the radar has somebody standing near that direction, they almost
    certainly made the noise, and their bearing is the better one by an order
    of magnitude - so the glance goes to the person rather than to the
    estimate. The gate is wide for the same reason the estimate is rough.
    """
    near = [t for t in radar_targets if t[1] <= near_mm]
    if near:
        bearing, distance = min(near, key=lambda t: abs(_wrap180(t[0] - rough_bearing)))
        if abs(_wrap180(bearing - rough_bearing)) <= gate_degrees:
            return bearing, (f"a noise at about {rough_bearing:.0f} - the person at {bearing:.0f} "
                             f"({distance / 1000.0:.1f} m) probably made it")
    return rough_bearing, f"a noise at about {rough_bearing:.0f}, nobody visible there"
