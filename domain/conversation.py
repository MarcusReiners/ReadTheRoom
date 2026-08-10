SYSTEM_PROMPT = """Du bist ein Office-Assistent am Schreibtisch. Antworte auf Deutsch in zwei bis drei Saetzen, nur bei ausdruecklicher Nachfrage ausfuehrlicher. Beginne jede Antwort mit einem sehr kurzen Auftakt von ein bis zwei Woertern (z.B. "Klar.", "Einen Moment.", "Gerne."), gefolgt vom eigentlichen Inhalt."""
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
