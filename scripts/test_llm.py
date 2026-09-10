import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import litellm

import adapters.llm
import config
from adapters.app_settings import load_app_settings
from adapters.conversation_store import ConversationStore
from domain.conversation import SYSTEM_PROMPT

LIMIT_S = 45.0


def run(label, kwargs):
    print(f"\n[{label}] reasoning_effort={kwargs.get('reasoning_effort') or '(provider default)'}, "
          f"{len(kwargs['messages'])} messages")
    events = queue.Queue()

    def worker():
        try:
            for chunk in litellm.completion(**kwargs):
                events.put(("chunk", chunk))
            events.put(("done", None))
        except Exception as e:
            events.put(("error", e))

    threading.Thread(target=worker, daemon=True).start()
    start = time.monotonic()
    first_text = None
    chunks = empty = 0
    text = ""
    while True:
        try:
            kind, payload = events.get(timeout=max(0.05, LIMIT_S - (time.monotonic() - start)))
        except queue.Empty:
            print(f"  nothing more after {LIMIT_S:.0f}s - the stream hangs")
            break
        t = time.monotonic() - start
        if kind == "error":
            print(f"  +{t:5.1f}s ERROR {type(payload).__name__}: {payload}")
            break
        if kind == "done":
            print(f"  +{t:5.1f}s stream finished")
            break
        chunks += 1
        choice = payload.choices[0] if payload.choices else None
        delta = choice.delta if choice else None
        content = (delta.content if delta else None) or ""
        thinking = (getattr(delta, "reasoning_content", None) if delta else None) or ""
        finish = choice.finish_reason if choice else None
        if content:
            text += content
            if first_text is None:
                first_text = t
        else:
            empty += 1
        if chunks <= 6 or finish:
            print(f"  +{t:5.1f}s chunk {chunks}: text={len(content)} chars, "
                  f"thinking={len(thinking)} chars, finish={finish}")
    first = "never" if first_text is None else f"{first_text:.1f}s"
    print(f"  first text: {first}, {chunks} chunks ({empty} without text), reply: {text[:80]!r}")


def main():
    settings = load_app_settings(config.APP_SETTINGS_PATH, defaults={
        "system_prompt": SYSTEM_PROMPT,
        "llm_model": config.LLM_MODEL,
        "reasoning_effort": config.LLM_REASONING_EFFORT,
    })
    model = settings["llm_model"]
    effort = settings["reasoning_effort"] or None
    system = settings["system_prompt"] or SYSTEM_PROMPT
    store = ConversationStore(config.CONVERSATIONS_DB_PATH)
    active = store.get_active_id()
    history = store.get_history(active) if active else []
    print(f"Model {model} (from {'app settings' if model != config.LLM_MODEL else '.env'}), "
          f"max_tokens {config.LLM_MAX_TOKENS}, system prompt {len(system)} chars, "
          f"history {len(history)} messages")

    base = dict(model=model, api_base=config.LLM_API_BASE, stream=True,
                max_tokens=config.LLM_MAX_TOKENS, timeout=LIMIT_S)
    if effort:
        base["reasoning_effort"] = effort
    system_msg = [{"role": "system", "content": system}]
    question = [{"role": "user", "content": "How is it going?"}]

    run("1 system prompt only", dict(base, messages=system_msg + question))
    run("2 system prompt + history (what the app sends)", dict(base, messages=system_msg + history + question))
    if effort != "minimal":
        run("3 same as 2, thinking minimal", dict(base, messages=system_msg + history + question,
                                                  reasoning_effort="minimal"))


if __name__ == "__main__":
    main()
