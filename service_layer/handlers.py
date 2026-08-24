import logging
import threading
import time

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

# Tie LED strip - white only, states are distinguished by animation speed
# instead of color: idle breathes slowly, listening breathes faster, a
# spoken answer is a steady solid white.
TIE_LED_WHITE = (255, 255, 255)
TIE_LED_IDLE_PERIOD_MS = 3000
TIE_LED_LISTENING_PERIOD_MS = 900


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


def _tie_led_command(mode: str, period_ms: int | None = None) -> dict:
    r, g, b = TIE_LED_WHITE
    cmd = {"cmd": "set_led", "mode": mode, "r": r, "g": g, "b": b}
    if period_ms is not None:
        cmd["period_ms"] = period_ms
    return cmd


def register_tie_led_handlers(bus: EventBus, radar) -> None:
    """Drives the 7-LED tie strip via the same serial command channel the
    radar adapter already has open to the bridge ESP32 (see
    mmWaveBridge/src/main.cpp's set_led command) - always white, states
    are distinguished by animation speed/mode rather than color.
    """

    def on_listening_changed(event: ListeningStateChanged) -> None:
        if event.listening:
            radar.send_command(_tie_led_command("pulse", TIE_LED_LISTENING_PERIOD_MS))
        else:
            radar.send_command(_tie_led_command("pulse", TIE_LED_IDLE_PERIOD_MS))

    def on_speech_started(event: SpeechPlaybackStarted) -> None:
        radar.send_command(_tie_led_command("solid"))

    def on_speech_ended(event: SpeechPlaybackEnded) -> None:
        radar.send_command(_tie_led_command("pulse", TIE_LED_IDLE_PERIOD_MS))

    bus.subscribe(ListeningStateChanged, on_listening_changed)
    bus.subscribe(SpeechPlaybackStarted, on_speech_started)
    bus.subscribe(SpeechPlaybackEnded, on_speech_ended)

    radar.send_command(_tie_led_command("pulse", TIE_LED_IDLE_PERIOD_MS))


def start_doa_tracking(doa, turntable, face, poll_interval_s: float = 0.3) -> None:
    """Continuously polls the ReSpeaker's onboard DOA/VAD on a background
    thread for the lifetime of the process, turning the head/eyes toward
    detected speech - same logic as scripts/test_hardware.py's
    live_doa_tracking(). Runs unconditionally rather than being gated to the
    recording window, so note it will also react to the assistant's own
    voice being picked back up by the mic during TTS playback.
    """

    def _tracking_loop() -> None:
        logger.info("[DOA] Tracking gestartet.")
        while True:
            try:
                active = doa.get_voice_active()
                logger.debug("[DOA] voice_active=%s", active)
                if active:
                    angle = doa.get_direction_degrees()
                    logger.info("[DOA] Stimme erkannt bei %.0f Grad.", angle)
                    turntable.rotate_towards(target_angle_degrees=angle)
                    face.set_eye_direction(angle_degrees=angle)
            except Exception:
                logger.exception("[DOA] Fehler beim Lesen/Ansteuern - Tracking-Thread beendet sich NICHT.")
            time.sleep(poll_interval_s)

    threading.Thread(target=_tracking_loop, daemon=True).start()
