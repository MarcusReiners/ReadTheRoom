import logging
from typing import Iterable

import requests

from adapters.tts.base import StreamingTTSAdapter
from service_layer.bus import EventBus

logger = logging.getLogger(__name__)


class RemoteTTSAdapter(StreamingTTSAdapter):
    def __init__(
        self,
        bus: EventBus,
        server_url: str,
        sample_rate: int = 22050,
        speaker_device: str = "default",
        timeout: float = 30.0,
    ) -> None:
        super().__init__(bus=bus, speaker_device=speaker_device)
        self.server_url = server_url.rstrip("/")
        self._sample_rate = sample_rate
        self.timeout = timeout

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def _synthesize_chunks(self, sentence: str) -> Iterable[bytes]:
        try:
            response = requests.post(
                f"{self.server_url}/speak",
                json={"text": sentence},
                timeout=self.timeout,
                stream=True,
            )
            response.raise_for_status()
            yield from response.iter_content(chunk_size=4096)
        except requests.RequestException as e:
            logger.error("TTS Fehler (Mac-Server): %s", e)
