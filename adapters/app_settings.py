import json
import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Keys persisted to the settings file, and how to fall back when one is
# missing (a fresh install, or a file written before that key existed).
# Defaults come from config.py / the domain layer rather than being repeated
# here, so this module stays a storage concern only.
_SCALAR_KEYS = (
    "system_prompt", "llm_model", "reasoning_effort", "stt_language_code",
    "volume", "servo_min_angle", "servo_max_angle", "door_zone",
)


def load_app_settings(path: str, defaults: dict) -> dict:
    """Reads the settings editable from the web app's settings tab. A
    missing or corrupt file just means "not customized yet" - not an error,
    since a fresh install has no settings file at all.

    Returns every key in `defaults`, plus "voices" (a list of
    {"id", "name", "voice_id"}) and "active_voice_id". "id" is this app's own
    stable key for a saved voice - a person can rename one, or reuse the same
    ElevenLabs voice_id under two names - while "voice_id" is what actually
    gets sent to ElevenLabs.
    """
    try:
        data = json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError, OSError):
        data = {}

    settings = {key: data.get(key, defaults.get(key)) for key in _SCALAR_KEYS}
    for key, value in settings.items():
        if value is None:
            settings[key] = defaults.get(key)

    voices = data.get("voices")
    if not voices:
        # Fresh install, or a file written before multi-voice support, which
        # stored a single flat "voice_id" - either way seed one entry so
        # there is always at least one voice to fall back to.
        legacy_voice_id = data.get("voice_id") or defaults.get("voice_id") or ""
        voices = [{"id": str(uuid.uuid4()), "name": "Default", "voice_id": legacy_voice_id}]

    active_voice_id = data.get("active_voice_id")
    if not active_voice_id or not any(v["id"] == active_voice_id for v in voices):
        active_voice_id = voices[0]["id"]

    settings["voices"] = voices
    settings["active_voice_id"] = active_voice_id
    return settings


def load_servo_range(path: str, default_min: float, default_max: float) -> tuple[float, float]:
    """Just the servo's safe turning window, without needing defaults for
    every unrelated setting. Used by adapters/factory.py so that every
    consumer of build_turntable() - main.py and all the hardware scripts
    alike - honours the range set in the web app, rather than the scripts
    quietly falling back to the config.py value and moving through a
    different window than the assistant does."""
    try:
        data = json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return default_min, default_max

    min_angle = data.get("servo_min_angle")
    max_angle = data.get("servo_max_angle")
    if min_angle is None or max_angle is None or max_angle <= min_angle:
        return default_min, default_max
    return float(min_angle), float(max_angle)


def load_door_zone(path: str) -> dict | None:
    """The door zone drawn in the web app, for every consumer of build_radar()
    - the main app and the study/zone scripts alike."""
    try:
        data = json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return None
    zone = data.get("door_zone")
    return zone if isinstance(zone, dict) else None


def save_app_settings(path: str, settings: dict) -> None:
    """Takes the whole settings dict rather than one parameter per field.
    The positional form this replaced meant every caller had to re-supply
    every unrelated value just to change one of them, and silently wrote a
    stale value for anything it got wrong."""
    Path(path).write_text(json.dumps(settings, indent=2))
    logger.info("[Settings] Saved (%s).", path)
