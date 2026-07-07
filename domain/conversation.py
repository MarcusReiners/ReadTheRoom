SYSTEM_PROMPT = """Du bist ein Office-Assistent am Schreibtisch. Antworte auf Deutsch, so ausfuehrlich wie die Frage es erfordert. Beginne jede Antwort mit einem sehr kurzen Auftakt von ein bis zwei Woertern (z.B. "Klar.", "Einen Moment.", "Gerne."), gefolgt vom eigentlichen, vollstaendigen Inhalt."""
class ConversationState:
    """
    Haelt den aktuellen Modus (voice/web) und ob das laufende Gespraech
    als vertraulich eingestuft ist (ADR-001 Abschnitt 4).
    """

    def __init__(self) -> None:
        self.modality: str = "voice"
        self.confidential: bool = False
        self.history: list[dict] = []

    def add_user_message(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})

    def add_assistant_message(self, text: str) -> None:
        self.history.append({"role": "assistant", "content": text})

    def mark_confidential(self) -> None:
        self.confidential = True

    def switch_modality(self, modality: str) -> None:
        self.modality = modality
