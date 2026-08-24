import logging

from domain.conversation import ConversationState
from domain.events import (
    ListeningStateChanged,
    ModalitySwitched,
    PersonCountChanged,
    SpeechPlaybackEnded,
    SpeechPlaybackStarted,
)
from service_layer.bus import EventBus
from adapters.tts.base import StreamingTTSAdapter

logger = logging.getLogger(__name__)

# Tie LED colors (r, g, b) - idle/listening pulse gently, speaking is solid.
TIE_LED_IDLE_COLOR = (0, 60, 160)
TIE_LED_LISTENING_COLOR = (160, 20, 20)
TIE_LED_SPEAKING_COLOR = (200, 160, 90)


def register_handlers(
    bus: EventBus,
    conversation: ConversationState,
    tts: StreamingTTSAdapter,
) -> None:
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

    bus.subscribe(PersonCountChanged, on_person_count_changed)


def _tie_led_command(mode: str, color: tuple[int, int, int]) -> dict:
    r, g, b = color
    return {"cmd": "set_led", "mode": mode, "r": r, "g": g, "b": b}


def register_tie_led_handlers(bus: EventBus, radar) -> None:
    """Drives the 7-LED tie strip via the same serial command channel the
    radar adapter already has open to the bridge ESP32 (see
    mmWaveBridge/src/main.cpp's set_led command) - idle/listening pulse
    gently, a spoken answer shows as a solid color.
    """

    def on_listening_changed(event: ListeningStateChanged) -> None:
        if event.listening:
            radar.send_command(_tie_led_command("pulse", TIE_LED_LISTENING_COLOR))
        else:
            radar.send_command(_tie_led_command("pulse", TIE_LED_IDLE_COLOR))

    def on_speech_started(event: SpeechPlaybackStarted) -> None:
        radar.send_command(_tie_led_command("solid", TIE_LED_SPEAKING_COLOR))

    def on_speech_ended(event: SpeechPlaybackEnded) -> None:
        radar.send_command(_tie_led_command("pulse", TIE_LED_IDLE_COLOR))

    bus.subscribe(ListeningStateChanged, on_listening_changed)
    bus.subscribe(SpeechPlaybackStarted, on_speech_started)
    bus.subscribe(SpeechPlaybackEnded, on_speech_ended)

    radar.send_command(_tie_led_command("pulse", TIE_LED_IDLE_COLOR))
