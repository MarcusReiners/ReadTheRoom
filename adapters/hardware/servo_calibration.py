import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_home_offset(path: str) -> float:
    """Reads the saved home-offset (degrees) for a 360-degree servo's mounted
    orientation. Missing/corrupt file just means "not calibrated yet" - not
    an error, since a fresh install has no calibration file at all."""
    try:
        data = json.loads(Path(path).read_text())
        return float(data.get("home_offset_degrees", 0.0))
    except (FileNotFoundError, ValueError, OSError):
        return 0.0


def save_home_offset(path: str, offset_degrees: float) -> None:
    Path(path).write_text(json.dumps({"home_offset_degrees": offset_degrees}))
    logger.info("[Servo] Home-Offset %.1f Grad gespeichert (%s).", offset_degrees, path)
