import logging

from domain.conversation import ConversationState
from domain.events import PersonCountChanged, DisplayTakeoverRequested
from service_layer.bus import EventBus
from adapters.tts.base import StreamingTTSAdapter

logger = logging.getLogger(__name__)


def register_handlers(
    bus: EventBus,
    conversation: ConversationState,
    tts: StreamingTTSAdapter,
    status_display,
) -> None:
    tts.takeover_sink = status_display.set_text

    def on_person_count_changed(event: PersonCountChanged) -> None:
        if event.count > 1 and conversation.confidential:
            conversation.switch_modality("web")
            logger.info("Modalitätswechsel zu 'web' (PersonCount=%s, vertraulich=%s)",
                        event.count, conversation.confidential)

    def on_display_takeover(event: DisplayTakeoverRequested) -> None:
        if status_display.text_active:
            status_display.stop_text()
            logger.info("Display-Textmodus beendet.")
        elif tts.request_takeover():
            logger.info("Sprachausgabe unterbrochen, Antwort läuft auf dem Display weiter.")
        else:
            logger.info("Keine laufende Antwort zum Umleiten.")

    bus.subscribe(PersonCountChanged, on_person_count_changed)
    bus.subscribe(DisplayTakeoverRequested, on_display_takeover)
