from dataclasses import dataclass, field
from datetime import datetime


def _now() -> datetime:
    return datetime.now()


@dataclass(frozen=True)
class Event:
    """Basisklasse aller Events. timestamp wird automatisch gesetzt."""
    timestamp: datetime = field(default_factory=_now, init=False)


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


@dataclass(frozen=True)
class SpeechTranscribed(Event):
    text: str


@dataclass(frozen=True)
class ConfidentialIntentDetected(Event):
    """Wird vom LLM-Klassifikations-Handler ausgeloest, nicht vom STT-Adapter."""
    source_text: str
    confidence: float


@dataclass(frozen=True)
class FocusShiftRequested(Event):
    person_id: int
    score: float


@dataclass(frozen=True)
class SpeechPlaybackStarted(Event):
    text: str


@dataclass(frozen=True)
class SpeechPlaybackEnded(Event):
    completed: bool


@dataclass(frozen=True)
class ListeningStateChanged(Event):
    listening: bool


@dataclass(frozen=True)
class DisplayTakeoverRequested(Event):
    pass


@dataclass(frozen=True)
class ModalitySwitched(Event):
    to_modality: str
    reason: str
