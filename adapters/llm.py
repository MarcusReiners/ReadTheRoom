import logging
import queue
import sys
import threading
import time
import traceback
from typing import Iterator, Optional

import litellm

from domain.conversation import SYSTEM_PROMPT

litellm.drop_params = True

logger = logging.getLogger(__name__)

_MAX_STACK_DUMPS = 3
_stack_dumps = 0


def _log_thread_stacks(reason: str) -> None:
    """Logs where every thread currently is, to find what an LLM request waits on."""
    global _stack_dumps
    if _stack_dumps >= _MAX_STACK_DUMPS:
        return
    _stack_dumps += 1
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = [f"[LLM] {reason} - where every thread is right now:"]
    for ident, frame in sys._current_frames().items():
        parts.append(f"--- {names.get(ident, ident)}")
        parts.append("".join(traceback.format_stack(frame, limit=14)).rstrip())
    logger.warning("\n".join(parts))

# Spoken aloud, so: short, plain, no punctuation salad, and English to match
# the assistant's locked reply language. Anything diagnostic belongs in the
# log - see the except blocks in ask_stream().
_SPOKEN_ERROR = "Sorry, something went wrong reaching the language model. The details are in the log."
_SPOKEN_UNREACHABLE = "Sorry, I can't reach the language model right now."
_SPOKEN_BUSY = "The language model is very busy right now. Please try again in a moment."
_SPOKEN_SLOW = "The language model is taking too long to answer right now. Please try again in a moment."

# Replies that are apologies for a failure, not answers. main.py checks this
# before writing a turn to the conversation store: persisting them poisons the
# history, since every later turn then ships the failure back to the model as
# if the assistant had really said it.
ERROR_REPLIES = frozenset({_SPOKEN_ERROR, _SPOKEN_UNREACHABLE, _SPOKEN_BUSY, _SPOKEN_SLOW})

_BUSY_STATUS = (429, 503, 529)
_BUSY_MARKERS = ("high demand", "overloaded", "resource exhausted", "resource_exhausted", "rate limit")


def _is_busy(error: Exception) -> bool:
    """The provider is up but turning requests away for now - Gemini's "high
    demand" 503, a 429 rate limit, an "overloaded" 529 - as opposed to being
    unreachable or rejecting the request itself. Follows wrapped exceptions:
    litellm reports a 503 that arrives on an open stream as a
    MidStreamFallbackError around the ServiceUnavailableError."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (litellm.exceptions.ServiceUnavailableError, litellm.exceptions.RateLimitError)):
            return True
        if getattr(error, "status_code", None) in _BUSY_STATUS:
            return True
        if any(marker in str(error).lower() for marker in _BUSY_MARKERS):
            return True
        error = getattr(error, "original_exception", None) or error.__cause__
    return False


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
        ollama_api_base: Optional[str] = None,
    ) -> None:
        self.model = model
        self.api_base = api_base
        self.ollama_api_base = ollama_api_base
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

    def _provider_kwargs(self, model: str, api_base: Optional[str] = None) -> dict:
        """The server address and the settings that differ per provider.

        Ollama models get their own address, so the web app can switch between
        a cloud model and the local one without a restart and without cloud
        requests being sent to the Ollama server. Ollama also gets qwen's
        "think" preamble switched off through its native field, which other
        providers reject (Gemini hard-errors on it); reasoning_effort is left
        out there so the two cannot conflict.
        """
        if model.startswith("ollama"):
            return {"api_base": api_base or self.ollama_api_base or self.api_base,
                    "extra_body": {"think": False}, "keep_alive": "24h"}
        kwargs = {"api_base": api_base or self.api_base}
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        return kwargs

    def warm_up(self) -> None:
        """Sends one tiny request in the background at startup, so litellm's
        one-off first-call setup (and loading a local model) is not paid inside
        the first turn's deadline."""
        def run() -> None:
            start = time.monotonic()
            kwargs = dict(model=self.model, stream=True, messages=[{"role": "user", "content": "Hi"}],
                          max_tokens=5, timeout=60, **self._provider_kwargs(self.model))
            watchdog = threading.Timer(10.0, _log_thread_stacks,
                                       args=("warm-up request still running after 10s",))
            watchdog.daemon = True
            watchdog.start()
            try:
                for _ in litellm.completion(**kwargs):
                    pass
                logger.info("[LLM] warm-up request done after %.1fs.", time.monotonic() - start)
            except Exception as e:
                logger.warning("[LLM] warm-up request failed after %.1fs: %s", time.monotonic() - start, e)
            finally:
                watchdog.cancel()

        threading.Thread(target=run, daemon=True, name="llm-warm-up").start()

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
            for delta in self._ask_stream(self.model, None, user_text, history):
                emitted_any = True
                yield delta
        except Exception as e:
            slow = isinstance(e, (litellm.exceptions.Timeout, _StreamStalled))
            unreachable = slow or isinstance(e, litellm.exceptions.APIConnectionError)
            if emitted_any:
                if unreachable:
                    logger.warning("'%s' aborted mid-stream (%s) - no fallback, the reply stays incomplete.",
                                   self.model, e)
                else:
                    logger.exception("LLM stream aborted after a partial reply: %s", e)
                return
            busy = not unreachable and _is_busy(e)
            if not (unreachable or busy):
                # The detail goes to the log, NOT to the speaker. Yielding the raw
                # exception meant a provider error was read out loud verbatim -
                # a model-retired 404 came through as several seconds of spoken
                # JSON, punctuation and all.
                logger.exception("LLM error: %s", e)
                yield _SPOKEN_ERROR
                return
            reason = "is too busy" if busy else "is too slow" if slow else "is unreachable"
            if self.fallback_model:
                logger.warning("'%s' %s (%s), falling back to '%s'.", self.model, reason, e, self.fallback_model)
                fallback_emitted = False
                try:
                    for delta in self._ask_stream(self.fallback_model, self.fallback_api_base, user_text, history):
                        fallback_emitted = True
                        yield delta
                    return
                except Exception as fallback_error:
                    logger.warning("Fallback '%s' at %s failed too (%s).",
                                   self.fallback_model, self.fallback_api_base, fallback_error)
                    if fallback_emitted:
                        return
            else:
                logger.warning("'%s' %s (%s) - no fallback model configured.", self.model, reason, e)
            yield _SPOKEN_BUSY if busy else _SPOKEN_SLOW if slow else _SPOKEN_UNREACHABLE

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
                stream = litellm.completion(**completion_kwargs)
                chunks.put(("opened", time.monotonic()))
                for chunk in stream:
                    chunks.put(("chunk", chunk))
                chunks.put(("done", None))
            except Exception as e:  # re-raised on the consumer side below
                chunks.put(("error", e))

        started = time.monotonic()
        threading.Thread(target=worker, daemon=True, name="llm-stream").start()

        model = completion_kwargs.get("model")
        opened_after: float | None = None
        empty_chunks = 0

        def stalled() -> _StreamStalled:
            _log_thread_stacks(f"no text from {model} yet")
            elapsed = time.monotonic() - started
            if got_first:
                return _StreamStalled(f"{model} went quiet for {self.stall_timeout_s:.0f}s mid-reply")
            if opened_after is None:
                return _StreamStalled(f"{model} had not even answered the request after {elapsed:.0f}s")
            return _StreamStalled(
                f"{model} sent no text within {elapsed:.0f}s (litellm set up the stream after "
                f"{opened_after:.1f}s, {empty_chunks} empty chunks)"
            )

        # The first-token limit is an ABSOLUTE deadline, not a per-chunk one.
        # A per-chunk timeout is restarted by every arriving chunk - including
        # content-free keepalives - so a provider that streams empty chunks
        # while producing nothing would hold the turn open indefinitely and
        # never trip it. Only real content resets the clock.
        first_deadline = started + self.first_token_timeout_s
        got_first = False
        while True:
            if got_first:
                wait = self.stall_timeout_s
            else:
                wait = max(0.05, first_deadline - time.monotonic())
            try:
                kind, payload = chunks.get(timeout=wait)
            except queue.Empty:
                raise stalled()
            if kind == "opened":
                opened_after = payload - started
                continue
            if kind == "done":
                return
            if kind == "error":
                raise payload
            delta = payload.choices[0].delta.content
            if delta:
                got_first = True
                yield delta
            else:
                empty_chunks += 1
                if not got_first and time.monotonic() >= first_deadline:
                    raise stalled()

    def _ask_stream(
        self, model: str, api_base: Optional[str], user_text: str, history: Optional[list[dict]],
    ) -> Iterator[str]:
        messages = [{"role": "system", "content": self.system_prompt}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_text})

        completion_kwargs = dict(
            model=model,
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
            **self._provider_kwargs(model, api_base),
        )

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
