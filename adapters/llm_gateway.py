"""
LLM-Gateway-Adapter (ADR-003 Abschnitt 14).

Nutzt /api/chat statt /api/generate, da nur dieser Endpoint
"think": false korrekt unterstuetzt (Qwen3.5 Thinking-Mode deaktivieren).
Dadurch sinkt die Antwortzeit von ~86s auf ~2.4s.
"""

import requests

from domain.conversation import SYSTEM_PROMPT


class LLMGatewayAdapter:
    def __init__(self, url: str, model: str) -> None:
        self.url = url.replace("/api/generate", "/api/chat")
        self.model = model

    def ask(self, user_text: str) -> str:
        print(f"LLM denkt nach über: '{user_text}'")
        try:
            res = requests.post(self.url, json={
                "model": self.model,
                "think": False,
                "stream": False,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_text},
                ],
                "options": {"num_predict": 50},
            }, timeout=60)
            res.raise_for_status()
            return res.json()["message"]["content"]
        except requests.exceptions.ConnectionError:
            return ("Fehler: Kann das LLM-Backend nicht erreichen. "
                    "Läuft Ollama/LiteLLM und ist die URL korrekt?")
        except Exception as e:
            return f"Fehler bei LLM: {e}"
