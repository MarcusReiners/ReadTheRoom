import logging

from domain.conversation import ConversationState
from domain.events import PersonCountChanged, DisplayTakeoverRequested, ModalitySwitched
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
        if event.count > 1 and conversation.confidential and conversation.modality == "voice":
            conversation.switch_modality("web")
            bus.publish(ModalitySwitched(to_modality="web", reason="person_entered"))
            logger.info("Modalitätswechsel zu 'web' (PersonCount=%s)", event.count)
            if tts.request_takeover():
                logger.info("Sprachausgabe unterbrochen (Person betrat den Raum).")
        elif event.count <= 1 and conversation.modality == "web":
            conversation.switch_modality("voice")
            bus.publish(ModalitySwitched(to_modality="voice", reason="alone_again"))
            logger.info("Modalitätswechsel zurück zu 'voice' (wieder allein)")

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
