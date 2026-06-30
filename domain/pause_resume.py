"""
Pause/Resume-Zustandsautomat (ADR-002 Abschnitt 11).

NORMAL -> PAUSED -> AWAITING_RESUME_DECISION -> NORMAL

Der Not-Stopp-Taster pausiert (SIGSTOP-Aequivalent), beendet nicht.
Erst nach explizitem zweitem Tastendruck UND einer Nutzerentscheidung
(fortsetzen/verwerfen) kehrt das System in den Normalzustand zurueck.
"""

from enum import Enum, auto


class PauseState(Enum):
    NORMAL = auto()
    PAUSED = auto()
    AWAITING_RESUME_DECISION = auto()


class PauseResumeController:
    def __init__(self) -> None:
        self.state = PauseState.NORMAL

    def on_first_press(self) -> None:
        """EmergencyStopPressed im NORMAL-Zustand."""
        if self.state == PauseState.NORMAL:
            self.state = PauseState.PAUSED

    def on_second_press(self) -> None:
        """Zweiter Tastendruck im PAUSED-Zustand -> Rueckfrage ausloesen."""
        if self.state == PauseState.PAUSED:
            self.state = PauseState.AWAITING_RESUME_DECISION

    def on_resume_decided(self, resume: bool) -> None:
        """Nutzerentscheidung in AWAITING_RESUME_DECISION."""
        if self.state == PauseState.AWAITING_RESUME_DECISION:
            self.state = PauseState.NORMAL

    @property
    def is_paused(self) -> bool:
        return self.state != PauseState.NORMAL
