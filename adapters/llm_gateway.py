"""
LLM-Gateway-Adapter (ADR-003 Abschnitt 14).

Spricht das Ollama-API-Format (POST /api/generate). Funktioniert sowohl
gegen einen direkten Ollama-Server als auch gegen einen LiteLLM-Proxy.

think=False deaktiviert den Thinking-Mode bei Qwen3.5, was die
Antwortzeit von ~86s auf ~2s reduziert.
"""

import requests

from domain.conversation import SYSTEM_PROMPT


class LLMGatewayAdapter:
    def __init__(self, url: str, model: str) -> None:
        self.url = url
        self.model = model

    def ask(self, user_text: str) -> str:
        print(f"🧠 LLM denkt nach über: '{user_text}'")
        try:
            prompt = f"{SYSTEM_PROMPT}\n\nNutzer: {user_text}"
            res = requests.post(self.url, json={
                "model": self.model,
                "prompt": prompt,
                "think": False,
                "stream": False,
            }, timeout=60)
            res.raise_for_status()
            return res.json()["response"]
        except requests.exceptions.ConnectionError:
            return ("Fehler: Kann das LLM-Backend nicht erreichen. "
                    "Läuft Ollama/LiteLLM und ist die URL korrekt?")
        except Exception as e:
            return f"Fehler bei LLM: {e}"
