import json
import time
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
        t_start = time.monotonic()
        t_first_token = None
        saw_thinking = False
        try:
            res = requests.post(self.url, json={
                "model": self.model,
                "think": False,
                "stream": True,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_text},
                ],
                "options": {"num_predict": 200},
            }, timeout=60, stream=True)
            res.raise_for_status()
            for line in res.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                message = data.get("message", {})
                if message.get("thinking"):
                    saw_thinking = True
                delta = message.get("content", "")
                if delta:
                    if t_first_token is None:
                        t_first_token = time.monotonic()
                        print(f"  [debug] Zeit bis 1. Token: {t_first_token - t_start:.2f}s")
                    yield delta
                if data.get("done"):
                    t_done = time.monotonic()
                    print(f"  [debug] Gesamtzeit: {t_done - t_start:.2f}s, "
                          f"thinking-Feld gesehen: {saw_thinking}")
                    for key in ("load_duration", "prompt_eval_duration", "eval_duration", "eval_count"):
                        if key in data:
                            value = data[key]
                            unit = "ns" if "duration" in key else ""
                            shown = f"{value / 1e9:.2f}s" if "duration" in key else value
                            print(f"  [debug] {key}: {shown}")
                    break
        except requests.exceptions.ConnectionError:
            yield ("Fehler: Kann das LLM-Backend nicht erreichen. "
                   "Läuft Ollama/LiteLLM und ist die URL korrekt?")
        except Exception as e:
            yield f"Fehler bei LLM: {e}"
