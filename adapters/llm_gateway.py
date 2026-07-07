import time
from typing import Iterator, Optional

import litellm

from domain.conversation import SYSTEM_PROMPT

litellm.drop_params = True


class LLMGatewayAdapter:
    def __init__(self, model: str, api_base: Optional[str] = None) -> None:
        self.model = model
        self.api_base = api_base

    def ask_stream(self, user_text: str) -> Iterator[str]:
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
                keep_alive="24h",
                extra_body={"think": False},
            )
            t_start = time.monotonic()
            t_first = None
            for chunk in response:
                delta = chunk.choices[0].delta.content
                if delta:
                    if t_first is None:
                        t_first = time.monotonic()
                        print(f"  [timing] LLM erstes Token: {t_first - t_start:.2f}s")
                    yield delta
            if t_first is not None:
                print(f"  [timing] LLM gesamt: {time.monotonic() - t_start:.2f}s")
        except litellm.exceptions.APIConnectionError:
            yield ("Fehler: Kann das LLM-Backend nicht erreichen. "
                   "Läuft der Server und ist die URL korrekt?")
        except Exception as e:
            yield f"Fehler bei LLM: {e}"
