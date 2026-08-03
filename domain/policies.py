# "Large change" - the servo only moves once its smoothed target has drifted this
# far from its current position. These used to be much higher while we thought
# the servo's buzzing was our target-selection logic - it turned out to be PWM
# signal jitter (fixed by switching gpiozero to the pigpio pin factory), so this
# only needs to filter genuine DOA sensor noise now, not overcompensate.
ROTATION_THRESHOLD_DEGREES = 15.0

# Exponential-smoothing factor for incoming DOA/turntable targets, applied before
# the rotation threshold above. Lower = smoother/slower to react, higher = snappier
# but more prone to visibly following sensor noise.
DOA_SMOOTHING_ALPHA = 0.25

# The smoothed target must stay past ROTATION_THRESHOLD_DEGREES for this many
# consecutive updates before the servo actually moves - rejects one-off spikes,
# only reacts to a change that actually persists.
MIN_CONSECUTIVE_LARGE_CHANGES = 2

# Minimum time between physical servo moves, regardless of how far off target it
# is - guarantees it rests for a bit after moving instead of chasing every
# update, even if incoming readings are noisy enough to otherwise trigger often.
MOVE_COOLDOWN_SECONDS = 1.5
