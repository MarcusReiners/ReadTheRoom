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
        system_prompt: Optional[str] = None,
    ) -> None:
        self.model = model
        self.api_base = api_base
        self.fallback_model = fallback_model
        self.fallback_api_base = fallback_api_base
        # Instance attribute (not the module-level SYSTEM_PROMPT) so the web
        # app's settings tab can edit it live - see ChatBridgeAdapter's
        # set_system_prompt handler.
        self.system_prompt = system_prompt or SYSTEM_PROMPT

    def ask_stream(self, user_text: str, history: Optional[list[dict]] = None) -> Iterator[str]:
        # Tracks whether the primary model already emitted anything before
        # failing. A timeout can fire mid-stream, not just while connecting,
        # and restarting from the fallback model then replays a whole fresh
        # answer on top of the half-sentence already streamed into the chat
        # and spoken aloud ("Sure. The weather- Got it, it's sunny today").
        # Once the primary has committed text to the turn, its failure has to
        # end the turn rather than silently double it.
        emitted_any = False
        try:
            for delta in self._ask_stream(self.model, self.api_base, user_text, history):
                emitted_any = True
                yield delta
        except (litellm.exceptions.APIConnectionError, litellm.exceptions.Timeout):
            if emitted_any:
                logger.warning("'%s' brach mitten im Stream ab - kein Fallback, Antwort bleibt unvollstaendig.",
                                self.model)
                return
            if self.fallback_model:
                logger.warning("'%s' nicht erreichbar/zu langsam, weiche aus auf Fallback '%s'.",
                                self.model, self.fallback_model)
                try:
                    yield from self._ask_stream(self.fallback_model, self.fallback_api_base, user_text, history)
                    return
                except (litellm.exceptions.APIConnectionError, litellm.exceptions.Timeout):
                    pass
            yield ("Fehler: Kann das LLM-Backend nicht erreichen. "
                   "Läuft der Server und ist die URL korrekt?")
        except Exception as e:
            if emitted_any:
                logger.exception("LLM-Stream nach Teilantwort abgebrochen: %s", e)
                return
            yield f"Fehler bei LLM: {e}"

    def _ask_stream(
        self, model: str, api_base: Optional[str], user_text: str, history: Optional[list[dict]],
    ) -> Iterator[str]:
        messages = [{"role": "system", "content": self.system_prompt}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_text})

        completion_kwargs = dict(
            model=model,
            api_base=api_base,
            messages=messages,
            stream=True,
            # High enough that a "thinking" model (e.g. Gemini's flash
            # models reason internally before answering) doesn't get cut
            # off before it ever reaches the actual reply text - a plain
            # non-reasoning model just stops early via finish_reason=stop
            # well under this ceiling, so it costs those nothing.
            max_tokens=500,
            # No timeout previously meant a slow/hung API response (seen once
            # taking 46s+) blocked the whole turn with nothing to make it
            # fail fast - this raises Timeout instead, which now also
            # triggers the fallback model the same way a connection error does.
            timeout=15,
        )
        if model.startswith("ollama"):
            # Ollama-specific: disables qwen3's "think" preamble via its
            # native REST field. Other providers reject unknown fields
            # (Gemini hard-errors on this), so it must not be sent to them.
            completion_kwargs["extra_body"] = {"think": False}
            completion_kwargs["keep_alive"] = "24h"

        response = litellm.completion(**completion_kwargs)
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
