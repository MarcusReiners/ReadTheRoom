import json
import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


def load_app_settings(path: str, default_system_prompt: str, default_voice_id: str) -> dict:
    """Reads the saved system prompt / named ElevenLabs voice library
    (editable from the web app's settings tab). Missing/corrupt file just
    means "not customized yet" - not an error, since a fresh install has no
    settings file at all.

    Returns {"system_prompt": str, "voices": [{"id": str, "name": str,
    "voice_id": str}, ...], "active_voice_id": str} - "id" is this app's own
    stable key for the saved entry (a person can rename a voice or reuse the
    same ElevenLabs voice_id under two names), "voice_id" is what's actually
    sent to ElevenLabs, and "active_voice_id" refers to that same "id" field.
    """
    try:
        data = json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError, OSError):
        data = {}

    voices = data.get("voices")
    if not voices:
        # Fresh install, or migrating from the single-voice_id format this
        # used before multi-voice support - either way, seed one entry so
        # there's always at least a "Default" voice to fall back to.
        legacy_voice_id = data.get("voice_id") or default_voice_id
        voices = [{"id": str(uuid.uuid4()), "name": "Default", "voice_id": legacy_voice_id}]

    active_voice_id = data.get("active_voice_id")
    if not active_voice_id or not any(v["id"] == active_voice_id for v in voices):
        active_voice_id = voices[0]["id"]

    return {
        "system_prompt": data.get("system_prompt") or default_system_prompt,
        "voices": voices,
        "active_voice_id": active_voice_id,
    }


def save_app_settings(path: str, system_prompt: str, voices: list, active_voice_id: str) -> None:
    Path(path).write_text(json.dumps({
        "system_prompt": system_prompt,
        "voices": voices,
        "active_voice_id": active_voice_id,
    }, indent=2))
    logger.info("[Settings] System-Prompt/Voice-Bibliothek gespeichert (%s).", path)
