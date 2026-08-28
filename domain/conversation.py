SYSTEM_PROMPT = """You are a desk assistant. Everything you say is spoken aloud, so write for the ear, not the page.

Answer in two or three sentences. Lead with the answer itself, then add detail only if it genuinely helps. Go longer only when explicitly asked to.

Start with a short one- or two-word acknowledgement ("Sure." / "Got it." / "One moment."), then the answer. Speech begins as soon as that first sentence lands, so it keeps the reply from feeling delayed.

Use plain spoken text only: no markdown, asterisks, bullet points, headings, code blocks, or emoji. Write numbers, dates and units the way you would say them out loud.

Your input comes from speech recognition and is sometimes wrong. If a request is garbled or could mean two quite different things, say what you think you heard and ask, instead of guessing. If you do not know something, say so in one sentence.

Always reply in English, even if you are addressed in another language."""


class ConversationState:
    """Device-level session state - shared across whichever chat is
    currently active. There's one mic and one speaker, so modality/voice
    settings apply to the device, not to any single conversation's content
    (that lives in adapters/conversation_store.py::ConversationStore).
    """

    def __init__(self) -> None:
        self.modality: str = "voice"
        # Personal desk assistant: every conversation is private by default,
        # not just ones explicitly flagged - a second person entering the
        # room is reason enough to switch away from speaking it aloud.
        self.confidential: bool = True
        # Manual override from the chat UI, independent of who's in the room.
        self.voice_enabled: bool = True

    def mark_confidential(self) -> None:
        self.confidential = True

    def set_private_mode(self, enabled: bool) -> None:
        """User-facing toggle (web app settings) for the same `confidential`
        flag handlers.py's on_person_count_changed() already gates the
        auto-switch-to-text-on-second-person behavior on - off means the
        assistant keeps talking aloud regardless of how many people are in
        the room."""
        self.confidential = enabled

    def switch_modality(self, modality: str) -> None:
        self.modality = modality
