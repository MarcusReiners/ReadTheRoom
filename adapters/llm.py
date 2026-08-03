import logging
import time
from typing import Iterator, Optional

import litellm

from domain.conversation import SYSTEM_PROMPT

litellm.drop_params = True

logger = logging.getLogger(__name__)


class LLMGatewayAdapter:
    def __init__(
        self,
        model: str,
        api_base: Optional[str] = None,
        fallback_model: Optional[str] = None,
        fallback_api_base: Optional[str] = None,
    ) -> None:
        self.model = model
        self.api_base = api_base
        self.fallback_model = fallback_model
        self.fallback_api_base = fallback_api_base

    def ask_stream(self, user_text: str) -> Iterator[str]:
        try:
            yield from self._ask_stream(self.model, self.api_base, user_text)
        except litellm.exceptions.APIConnectionError:
            if self.fallback_model:
                logger.warning("'%s' nicht erreichbar, weiche aus auf Fallback '%s'.",
                                self.model, self.fallback_model)
                try:
                    yield from self._ask_stream(self.fallback_model, self.fallback_api_base, user_text)
                    return
                except litellm.exceptions.APIConnectionError:
                    pass
            yield ("Fehler: Kann das LLM-Backend nicht erreichen. "
                   "Läuft der Server und ist die URL korrekt?")
        except Exception as e:
            yield f"Fehler bei LLM: {e}"

    def _ask_stream(self, model: str, api_base: Optional[str], user_text: str) -> Iterator[str]:
        response = litellm.completion(
            model=model,
            api_base=api_base,
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
                    logger.info("[timing] LLM erstes Token: %.2fs", t_first - t_start)
                yield delta
        if t_first is not None:
            logger.info("[timing] LLM gesamt: %.2fs", time.monotonic() - t_start)
