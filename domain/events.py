"""
Domain Events fuer den Office Assistant.

Jedes Event ist ein einfaches, unveraenderliches Datenobjekt. Adapter
erzeugen Events, der Event-Bus verteilt sie an registrierte Handler.
Adapter wissen NICHTS voneinander - sie kommunizieren ausschliesslich
ueber Events (siehe ADR-001 Abschnitt 2).
"""

from dataclasses import dataclass, field
from datetime import datetime


def _now() -> datetime:
    return datetime.now()


@dataclass(frozen=True)
class Event:
    """Basisklasse aller Events. timestamp wird automatisch gesetzt."""
    timestamp: datetime = field(default_factory=_now, init=False)


# ---------------------------------------------------------------------
# Sensor-Events (ADR-001): LD2450 Radar
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class PersonEnteredRoom(Event):
    person_id: int
    x_mm: float
    y_mm: float


@dataclass(frozen=True)
class PersonLeftRoom(Event):
    person_id: int


@dataclass(frozen=True)
class PersonCountChanged(Event):
    count: int


@dataclass(frozen=True)
class PersonPositionUpdated(Event):
    """Kontinuierliches Tracking-Update, Grundlage fuer ApproachScore."""
    person_id: int
    x_mm: float
    y_mm: float
    speed_mm_s: float


# ---------------------------------------------------------------------
# Sprach-Events (ADR-001): Whisper STT
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class SpeechTranscribed(Event):
    text: str


@dataclass(frozen=True)
class ConfidentialIntentDetected(Event):
    """Wird vom LLM-Klassifikations-Handler ausgeloest, nicht vom STT-Adapter."""
    source_text: str
    confidence: float


# ---------------------------------------------------------------------
# Fusions-Events (ADR-001 Abschnitt 3)
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class FocusShiftRequested(Event):
    person_id: int
    score: float


# ---------------------------------------------------------------------
# TTS-Wiedergabe-Events (ADR-002 Abschnitt 9.2) - steuern Mundanimation
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class SpeechPlaybackStarted(Event):
    text: str


@dataclass(frozen=True)
class SpeechPlaybackEnded(Event):
    completed: bool  # False, wenn durch EmergencyStop unterbrochen


@dataclass(frozen=True)
class ListeningStateChanged(Event):
    listening: bool


# ---------------------------------------------------------------------
# Not-Stopp / Pause-Resume (ADR-002 Abschnitt 11)
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class EmergencyStopPressed(Event):
    pass


@dataclass(frozen=True)
class ResumeDecisionRequested(Event):
    pass


@dataclass(frozen=True)
class ResponseResumed(Event):
    pass


@dataclass(frozen=True)
class ResponseDiscarded(Event):
    pass


# ---------------------------------------------------------------------
# Modalitaet (ADR-001 Abschnitt 4)
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class ModalitySwitched(Event):
    to_modality: str  # "voice" | "web"
    reason: str
