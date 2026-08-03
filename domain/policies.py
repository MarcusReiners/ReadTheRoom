# "Large change" - the servo only moves once its smoothed target has drifted this
# far from its current position. Raised deliberately high so it holds still for
# anything but a real, sustained change in direction.
ROTATION_THRESHOLD_DEGREES = 35.0

# Exponential-smoothing factor for incoming DOA/turntable targets, applied before
# the rotation threshold above. Lower = smoother/slower to react, higher = snappier
# but more prone to visibly following sensor noise.
DOA_SMOOTHING_ALPHA = 0.1

# The smoothed target must stay past ROTATION_THRESHOLD_DEGREES for this many
# consecutive updates before the servo actually moves - rejects one-off spikes,
# only reacts to a change that actually persists.
MIN_CONSECUTIVE_LARGE_CHANGES = 5

# Minimum time between physical servo moves, regardless of how far off target it
# is - guarantees it rests for a while after moving instead of chasing every
# update, even if incoming readings are noisy enough to otherwise trigger often.
MOVE_COOLDOWN_SECONDS = 5.0
