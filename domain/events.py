from dataclasses import dataclass, field
from datetime import datetime


def _now() -> datetime:
    return datetime.now()


@dataclass(frozen=True)
class Event:
    timestamp: datetime = field(default_factory=_now, init=False)


@dataclass(frozen=True)
class PersonEnteredRoom(Event):
    person_id: int
    x_mm: float
    y_mm: float
    # "crossing": tracked outside, then inside. "appeared": first seen inside,
    # already moving and close to the edge - someone who came in faster than
    # the radar picked them up outside.
    via: str = "crossing"


@dataclass(frozen=True)
class PersonLeftRoom(Event):
    person_id: int


@dataclass(frozen=True)
class PersonAtDoor(Event):
    """Someone from outside stepped into the door zone - could be about to
    come in, or just looking in."""
    person_id: int
    x_mm: float
    y_mm: float


@dataclass(frozen=True)
class DoorCleared(Event):
    """Someone who was at the door is no longer there. outcome: "entered"
    (walked on into the room) or "gone" (turned back or vanished - a peek)."""
    person_id: int
    outcome: str


@dataclass(frozen=True)
class VoiceDucked(Event):
    active: bool
    reason: str


@dataclass(frozen=True)
class PersonCountChanged(Event):
    count: int


@dataclass(frozen=True)
class SpeechTranscribed(Event):
    text: str
    conversation_id: str
    # True when this overwrote the conversation's last (still-unanswered)
    # user message in place rather than adding a new one - see
    # ConversationStore.replace_or_add_user_message().
    replaced: bool = False
    # "voice" or "chat". Only a spoken turn proves that someone is physically
    # in the room talking to the assistant.
    source: str = "voice"


@dataclass(frozen=True)
class ConfidentialIntentDetected(Event):
    source_text: str
    confidence: float


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
class ModalitySwitched(Event):
    to_modality: str
    reason: str


@dataclass(frozen=True)
class AssistantDeltaReceived(Event):
    delta: str
    conversation_id: str


@dataclass(frozen=True)
class AssistantMessageCompleted(Event):
    text: str
    conversation_id: str


@dataclass(frozen=True)
class AssistantTurnCancelled(Event):
    """Published instead of AssistantMessageCompleted when a reply was cut
    short mid-generation - a chat-typed message taking priority over a
    still-in-progress voice-triggered reply (see main.py's handle_turn())."""
    conversation_id: str


@dataclass(frozen=True)
class RadarTargetsUpdated(Event):
    targets: list
