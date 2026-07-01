"""
Konversationszustand und System-Prompt (ADR-003 Abschnitt 13).

Die Office-Assistent-Identitaet wird hier als System-Prompt definiert
und bei jeder LLM-Anfrage mitgesendet - kein Training, kein RAG noetig
fuer reine Persona-/Verhaltensregeln (siehe ADR-003).
"""

SYSTEM_PROMPT = """Du bist ein Office-Assistent am Schreibtisch. Antworte immer in maximal einem kurzen Satz auf Deutsch"""
class ConversationState:
    """
    Haelt den aktuellen Modus (voice/web) und ob das laufende Gespraech
    als vertraulich eingestuft ist (ADR-001 Abschnitt 4).
    """

    def __init__(self) -> None:
        self.modality: str = "voice"
        self.confidential: bool = False
        self.history: list[dict] = []  # [{"role": "user"/"assistant", "content": str}]

    def add_user_message(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})

    def add_assistant_message(self, text: str) -> None:
        self.history.append({"role": "assistant", "content": text})

    def mark_confidential(self) -> None:
        self.confidential = True

    def switch_modality(self, modality: str) -> None:
        self.modality = modality
