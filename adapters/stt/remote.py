import logging
import os

import requests

logger = logging.getLogger(__name__)


class RemoteSTTAdapter:
    def __init__(self, server_url: str, timeout: float = 30.0) -> None:
        self.server_url = server_url.rstrip("/")
        self.timeout = timeout

    def transcribe(self, audio_file: str) -> str:
        if not os.path.exists(audio_file):
            logger.error("Audiodatei '%s' nicht gefunden.", audio_file)
            return ""
        try:
            with open(audio_file, "rb") as f:
                response = requests.post(
                    f"{self.server_url}/transcribe",
                    files={"audio": f},
                    timeout=self.timeout,
                )
            response.raise_for_status()
            return response.json().get("text", "").strip()
        except requests.RequestException as e:
            logger.error("STT Fehler (Mac-Server): %s", e)
            return ""

    def stop(self) -> None:
        pass
