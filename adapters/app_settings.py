import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_app_settings(path: str, default_system_prompt: str, default_voice_id: str) -> dict:
    """Reads the saved system prompt / ElevenLabs voice ID (editable from the
    web app's settings tab). Missing/corrupt file just means "not customized
    yet" - not an error, since a fresh install has no settings file at all."""
    try:
        data = json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    return {
        "system_prompt": data.get("system_prompt") or default_system_prompt,
        "voice_id": data.get("voice_id") or default_voice_id,
    }


def save_app_settings(path: str, system_prompt: str, voice_id: str) -> None:
    Path(path).write_text(json.dumps({"system_prompt": system_prompt, "voice_id": voice_id}, indent=2))
    logger.info("[Settings] System-Prompt/Voice-ID gespeichert (%s).", path)
