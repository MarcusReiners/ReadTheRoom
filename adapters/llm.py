import logging
import queue
import threading
import time
from typing import Iterator, Optional

import litellm

from domain.conversation import SYSTEM_PROMPT

litellm.drop_params = True

logger = logging.getLogger(__name__)

# Spoken aloud, so: short, plain, no punctuation salad, and English to match
# the assistant's locked reply language. Anything diagnostic belongs in the
# log - see the except blocks in ask_stream().
_SPOKEN_ERROR = "Sorry, something went wrong reaching the language model. The details are in the log."
_SPOKEN_UNREACHABLE = "Sorry, I can't reach the language model right now."

# Replies that are apologies for a failure, not answers. main.py checks this
# before writing a turn to the conversation store: persisting them poisons the
# history, since every later turn then ships the failure back to the model as
# if the assistant had really said it.
ERROR_REPLIES = frozenset({_SPOKEN_ERROR, _SPOKEN_UNREACHABLE})


class _StreamStalled(Exception):
    """The provider accepted the request but produced no token in time.

    litellm's own `timeout` covers establishing the request, not an idle gap
    on an already-open stream - a Gemini call was seen taking 105s to its
    first token with timeout=15 set and nothing tripping. Enforced here
    instead, and treated exactly like litellm.exceptions.Timeout.
    """


class LLMGatewayAdapter:
    def __init__(
        self,
        model: str,
        api_base: Optional[str] = None,
        fallback_model: Optional[str] = None,
        fallback_api_base: Optional[str] = None,
        system_prompt: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        max_tokens: int = 500,
        first_token_timeout_s: float = 20.0,
        stall_timeout_s: float = 30.0,
    ) -> None:
        self.model = model
        self.api_base = api_base
        self.fallback_model = fallback_model
        self.fallback_api_base = fallback_api_base
        # Instance attribute (not the module-level SYSTEM_PROMPT) so the web
        # app's settings tab can edit it live - see ChatBridgeAdapter's
        # set_system_prompt handler.
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.reasoning_effort = reasoning_effort or None
        self.max_tokens = max_tokens
        # How long to wait for the FIRST token, and for each subsequent one.
        # The first is the one that matters for perceived responsiveness -
        # nothing has been spoken yet, so the turn is simply dead air. Later
        # gaps get a longer allowance since a long answer legitimately
        # streams over many seconds.
        self.first_token_timeout_s = first_token_timeout_s
        self.stall_timeout_s = stall_timeout_s

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
        except (litellm.exceptions.APIConnectionError, litellm.exceptions.Timeout, _StreamStalled):
            if emitted_any:
                logger.warning("'%s' aborted mid-stream - no fallback, the reply stays incomplete.",
                                self.model)
                return
            if self.fallback_model:
                logger.warning("'%s' unreachable or too slow, falling back to '%s'.",
                                self.model, self.fallback_model)
                try:
                    yield from self._ask_stream(self.fallback_model, self.fallback_api_base, user_text, history)
                    return
                except (litellm.exceptions.APIConnectionError, litellm.exceptions.Timeout, _StreamStalled):
                    pass
            yield _SPOKEN_UNREACHABLE
        except Exception as e:
            if emitted_any:
                logger.exception("LLM stream aborted after a partial reply: %s", e)
                return
            # The detail goes to the log, NOT to the speaker. Yielding the raw
            # exception meant a provider error was read out loud verbatim -
            # a model-retired 404 came through as several seconds of spoken
            # JSON, punctuation and all.
            logger.exception("LLM error: %s", e)
            yield _SPOKEN_ERROR

    def _stream_with_deadline(self, completion_kwargs: dict) -> Iterator[str]:
        """Yields content deltas, raising _StreamStalled if the provider goes
        quiet for too long.

        The request runs on a worker thread feeding a queue, because the only
        way to put a deadline on a blocking iterator is to not be the one
        blocking on it. A stalled call can't be cancelled - litellm exposes no
        handle for that - so the thread is abandoned and left to finish on its
        own; it's a daemon and holds nothing but its own HTTP connection.
        """
        chunks: "queue.Queue[tuple[str, object]]" = queue.Queue()

        def worker() -> None:
            try:
                for chunk in litellm.completion(**completion_kwargs):
                    chunks.put(("chunk", chunk))
                chunks.put(("done", None))
            except Exception as e:  # re-raised on the consumer side below
                chunks.put(("error", e))

        threading.Thread(target=worker, daemon=True).start()

        model = completion_kwargs.get("model")
        # The first-token limit is an ABSOLUTE deadline, not a per-chunk one.
        # A per-chunk timeout is restarted by every arriving chunk - including
        # content-free keepalives - so a provider that streams empty chunks
        # while producing nothing would hold the turn open indefinitely and
        # never trip it. Only real content resets the clock.
        first_deadline = time.monotonic() + self.first_token_timeout_s
        got_first = False
        while True:
            if got_first:
                wait = self.stall_timeout_s
            else:
                wait = max(0.05, first_deadline - time.monotonic())
            try:
                kind, payload = chunks.get(timeout=wait)
            except queue.Empty:
                raise _StreamStalled(f"Kein Token von {model} - Stream abgebrochen.")
            if kind == "done":
                return
            if kind == "error":
                raise payload
            delta = payload.choices[0].delta.content
            if delta:
                got_first = True
                yield delta
            elif not got_first and time.monotonic() >= first_deadline:
                raise _StreamStalled(
                    f"Kein Token von {model} binnen {self.first_token_timeout_s:.0f}s "
                    "(nur leere Chunks) - Stream abgebrochen."
                )

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
            max_tokens=self.max_tokens,
            # No timeout previously meant a slow/hung API response (seen once
            # taking 46s+) blocked the whole turn with nothing to make it
            # fail fast - this raises Timeout instead, which now also
            # triggers the fallback model the same way a connection error does.
            timeout=15,
        )
        if self.reasoning_effort:
            # Only sent when explicitly configured - omitting it leaves the
            # provider default alone rather than silently picking one.
            completion_kwargs["reasoning_effort"] = self.reasoning_effort
        if model.startswith("ollama"):
            # Ollama-specific: disables qwen3's "think" preamble via its
            # native REST field. Other providers reject unknown fields
            # (Gemini hard-errors on this), so it must not be sent to them.
            completion_kwargs["extra_body"] = {"think": False}
            completion_kwargs["keep_alive"] = "24h"

        t_start = time.monotonic()
        t_first = None
        for delta in self._stream_with_deadline(completion_kwargs):
            if delta:
                if t_first is None:
                    t_first = time.monotonic()
                    logger.info("[timing] LLM erstes Token: %.2fs", t_first - t_start)
                yield delta
        if t_first is not None:
            logger.info("[timing] LLM gesamt: %.2fs", time.monotonic() - t_start)
