from typing import Iterator, Optional

import litellm

from domain.conversation import SYSTEM_PROMPT

litellm.drop_params = True


class LLMGatewayAdapter:
    def __init__(self, model: str, api_base: Optional[str] = None) -> None:
        self.model = model
        self.api_base = api_base

    def ask_stream(self, user_text: str) -> Iterator[str]:
        """Streamt die Antwort satzweise-vorbereitet als Text-Deltas, damit die
        TTS-Ausgabe schon starten kann, waehrend das LLM noch generiert.
        Ueber LiteLLM austauschbar zwischen Ollama und Cloud-Providern
        (z.B. model="gpt-4o-mini" statt "ollama_chat/qwen3.5:9b")."""
        try:
            response = litellm.completion(
                model=self.model,
                api_base=self.api_base,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_text},
                ],
                stream=True,
                max_tokens=200,
                think=False,
                keep_alive="24h",
            )
            for chunk in response:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        except litellm.exceptions.APIConnectionError:
            yield ("Fehler: Kann das LLM-Backend nicht erreichen. "
                   "Läuft der Server und ist die URL korrekt?")
        except Exception as e:
            yield f"Fehler bei LLM: {e}"
