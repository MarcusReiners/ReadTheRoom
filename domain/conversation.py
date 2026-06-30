"""
Konversationszustand und System-Prompt (ADR-003 Abschnitt 13).

Die Office-Assistent-Identitaet wird hier als System-Prompt definiert
und bei jeder LLM-Anfrage mitgesendet - kein Training, kein RAG noetig
fuer reine Persona-/Verhaltensregeln (siehe ADR-003).
"""

SYSTEM_PROMPT = """Du bist ein Office-Assistent, der im Buero auf einem \
Schreibtisch steht. Du hilfst bei Terminen, Notizen, kurzen Recherchen \
und allgemeinen Fragen. Antworte kurz und praegnant in maximal zwei \
Saetzen auf Deutsch. Du achtest auf Vertraulichkeit: Wenn waehrend des \
Gespraechs eine weitere Person den Raum betritt, kann das Gespraech \
automatisch unterbrochen und auf eine textbasierte Anzeige umgeschaltet \
werden."""


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
