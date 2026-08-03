import logging
import os
from typing import Iterable

from piper.voice import PiperVoice

from adapters.tts.base import StreamingTTSAdapter
from service_layer.bus import EventBus

logging.getLogger("piper").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)


class LocalTTSAdapter(StreamingTTSAdapter):
    def __init__(
        self,
        bus: EventBus,
        model_path: str = "/home/marcus/piper-voices/de_DE-thorsten-low.onnx",
        speaker_device: str = "hw:3,0",
    ) -> None:
        super().__init__(bus=bus, speaker_device=speaker_device)

        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Piper-Modell nicht gefunden: {model_path}\n"
                "Bitte laden: wget .../de_DE-thorsten-low.onnx -P ~/piper-voices/"
            )
        logger.info("Lade Piper-Stimme...")
        self._voice = PiperVoice.load(model_path)
        logger.info("Piper bereit.")

    @property
    def sample_rate(self) -> int:
        return self._voice.config.sample_rate

    def _synthesize_chunks(self, sentence: str) -> Iterable[bytes]:
        for chunk in self._voice.synthesize(sentence):
            yield chunk.audio_int16_bytes
