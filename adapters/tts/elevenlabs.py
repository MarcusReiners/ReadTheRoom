from typing import Iterable

from elevenlabs.client import ElevenLabs

from adapters.tts.base import StreamingTTSAdapter
from service_layer.bus import EventBus

_OUTPUT_FORMAT = "pcm_16000"
_SAMPLE_RATE = 16000


class ElevenLabsTTSAdapter(StreamingTTSAdapter):
    def __init__(
        self,
        bus: EventBus,
        api_key: str,
        voice_id: str,
        model_id: str = "eleven_flash_v2_5",
        speaker_device: str = "default",
    ) -> None:
        super().__init__(bus=bus, speaker_device=speaker_device)
        if not api_key:
            raise ValueError("ELEVENLABS_API_KEY ist nicht gesetzt.")
        self.voice_id = voice_id
        self.model_id = model_id
        self._client = ElevenLabs(api_key=api_key)

    @property
    def sample_rate(self) -> int:
        return _SAMPLE_RATE

    def _synthesize_chunks(self, sentence: str) -> Iterable[bytes]:
        stream = self._client.text_to_speech.stream(
            text=sentence,
            voice_id=self.voice_id,
            model_id=self.model_id,
            output_format=_OUTPUT_FORMAT,
        )
        for chunk in stream:
            if chunk:
                yield chunk
