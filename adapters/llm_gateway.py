import json
from typing import Iterator

import requests

from domain.conversation import SYSTEM_PROMPT


class LLMGatewayAdapter:
    def __init__(self, url: str, model: str) -> None:
        self.url = url.replace("/api/generate", "/api/chat")
        self.model = model

    def ask_stream(self, user_text: str) -> Iterator[str]:
        """Streamt die Antwort satzweise-vorbereitet als Text-Deltas, damit die
        TTS-Ausgabe schon starten kann, waehrend das LLM noch generiert."""
        print(f"LLM denkt nach über: '{user_text}'")
        try:
            res = requests.post(self.url, json={
                "model": self.model,
                "think": False,
                "stream": True,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_text},
                ],
                "options": {"num_predict": 50},
            }, timeout=60, stream=True)
            res.raise_for_status()
            for line in res.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                delta = data.get("message", {}).get("content", "")
                if delta:
                    yield delta
                if data.get("done"):
                    break
        except requests.exceptions.ConnectionError:
            yield ("Fehler: Kann das LLM-Backend nicht erreichen. "
                   "Läuft Ollama/LiteLLM und ist die URL korrekt?")
        except Exception as e:
            yield f"Fehler bei LLM: {e}"
