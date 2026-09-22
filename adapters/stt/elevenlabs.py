import logging
import os

from elevenlabs.client import ElevenLabs

logger = logging.getLogger(__name__)


class ElevenLabsSTTAdapter:
    def __init__(
        self,
        api_key: str,
        model_id: str = "scribe_v2",
        language_code: str = "eng",
    ) -> None:
        if not api_key:
            raise ValueError("ELEVENLABS_API_KEY ist nicht gesetzt.")
        self.model_id = model_id
        # Empty -> None, so an empty ELEVENLABS_LANGUAGE_CODE means "let
        # Scribe auto-detect" rather than sending a blank code the API
        # rejects. Auto-detect is the right setting for a multilingual user;
        # a fixed code is more accurate when the language really is fixed.
        self.language_code = language_code or None
        self._client = ElevenLabs(api_key=api_key)

    def transcribe(self, audio_file: str) -> str:
        if not os.path.exists(audio_file):
            logger.error("Audio file '%s' not found.", audio_file)
            return ""
        try:
            with open(audio_file, "rb") as f:
                result = self._client.speech_to_text.convert(
                    file=f,
                    model_id=self.model_id,
                    language_code=self.language_code,
                )
            return (result.text or "").strip()
        except Exception as e:
            logger.exception("STT error: %s: %s", type(e).__name__, e)
            return ""

    def stop(self) -> None:
        pass
