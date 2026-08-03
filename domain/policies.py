ROTATION_THRESHOLD_DEGREES = 8.0

# Exponential-smoothing factor for incoming DOA/turntable targets, applied before
# the rotation threshold above. Lower = smoother/slower to react, higher = snappier
# but more prone to visibly following sensor noise.
DOA_SMOOTHING_ALPHA = 0.3
