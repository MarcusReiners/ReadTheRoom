SYSTEM_PROMPT = """You are a desk office assistant. Reply in English, in two to three sentences, only going longer if explicitly asked for more detail. Start every reply with a very short one-to-two-word opener (e.g. "Sure.", "One moment.", "Got it."), followed by the actual content."""
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

    def switch_modality(self, modality: str) -> None:
        self.modality = modality
