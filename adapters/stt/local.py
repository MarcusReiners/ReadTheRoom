import logging
import os

from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)


class LocalSTTAdapter:
    def __init__(
        self,
        model_path: str = "/home/marcus/voice-pipeline/whisper-data/whisper-tiny-german-1224-ct2",
        language: str = "de",
        beam_size: int = 1,
        compute_type: str = "int8",
        cpu_threads: int = 3,
        vad: bool = True,
    ) -> None:
        self.language = language
        self.beam_size = beam_size
        self.vad = vad

        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Whisper-Modell nicht gefunden: {model_path}\n"
                "Bitte sicherstellen dass das CT2-Modell vorhanden ist."
            )

        logger.info("Lade Whisper-Modell...")
        self._model = WhisperModel(model_path, device="cpu", compute_type=compute_type,
                                   cpu_threads=cpu_threads, num_workers=1)
        logger.info("Whisper ready.")

    def transcribe(self, audio_file: str) -> str:
        if not os.path.exists(audio_file):
            logger.error("Audio file '%s' not found.", audio_file)
            return ""
        try:
            segments, info = self._model.transcribe(
                audio_file,
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=self.vad,
                vad_parameters={"speech_pad_ms": 800, "min_silence_duration_ms": 1000},
            )
            text = " ".join(s.text.strip() for s in segments).strip()

            for noise in ["[BLANK_AUDIO]", "(Stille)", "(Musik)", "(music)", "(silence)"]:
                text = text.replace(noise, "")

            return text.strip()

        except Exception as e:
            logger.exception("STT error: %s: %s", type(e).__name__, e)
            return ""

    def stop(self) -> None:
        pass
