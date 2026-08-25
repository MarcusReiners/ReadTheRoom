import contextlib
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


def start_doa_tracking(
    doa, turntable, face, poll_interval_s: float = 0.3, lock=None, assistant_speaking=None,
    settle_base_s: float = 0.15, settle_deg_per_s: float = 200.0, eye_lead_s: float = 0.15,
    silence_timeout_s: float = 5.0,
) -> None:
    """Continuously polls the ReSpeaker's onboard DOA/VAD on a background
    thread for the lifetime of the process, turning the head/eyes toward
    detected speech - same logic as scripts/test_hardware.py's
    live_doa_tracking().

    lock serializes USB control-transfer access to the ReSpeaker with any
    other thread reading the same device concurrently (see main.py's
    vad_input_loop) - pass None if this is the only consumer.

    assistant_speaking (a threading.Event, set for the duration of
    SpeechPlaybackStarted..SpeechPlaybackEnded) skips reacting entirely while
    TTS is playing - confirmed on the bench this isn't just cosmetic: without
    it, the head swings wildly tracking its own voice mid-sentence, and the
    resulting servo motor noise is itself loud enough to trip the VAD and
    trigger a bogus recording, which is picked up by main.py's
    vad_input_loop as if a person had spoken - a real feedback loop, not a
    hypothetical one. Pass None to disable the guard (not recommended for
    normal use).

    settle_base_s/settle_deg_per_s: after actually commanding a move, DOA is
    ignored entirely (not even polled) for settle_base_s + moved_degrees /
    settle_deg_per_s - the servo has no position feedback, so a poll taken
    while it's still physically mid-turn would compute the next target from a
    current_heading_degrees that doesn't yet match reality, and the motor's
    own noise while moving risks tripping the VAD the same way speech
    playback does. settle_deg_per_s is a conservative estimate of this
    servo's turning speed, not a measured spec - tune down if it still reacts
    before finishing a large turn, or up if it feels sluggish on small ones.

    eye_lead_s: on a detected direction, the eyes dart to it immediately and
    the head follows eye_lead_s later (a person's eyes move before their head
    does), then drift back to center over the servo's own settle window via
    face.animate_eye_direction() - by the time the head physically arrives,
    eyes and head are aligned again instead of the eyes staying cocked to the
    side.

    silence_timeout_s: if no voice activity has been picked up for this long,
    the head drifts back to home on its own - it shouldn't stay cocked toward
    the last speaker indefinitely once the room's gone quiet. The clock
    resets on every detected voice activity AND on assistant_speaking
    clearing (a reply just ended) - not while assistant_speaking is set,
    since that time is spent replying, not sitting in silence. Set to None or
    <=0 to disable.
    """
    lock = lock or contextlib.nullcontext()
    moving_until = 0.0

    def _tracking_loop() -> None:
        nonlocal moving_until
        logger.info("[DOA] Tracking gestartet.")
        last_active_time = time.monotonic()
        was_speaking = False
        at_home = True
        while True:
            try:
                speaking = assistant_speaking is not None and assistant_speaking.is_set()
                settling = time.monotonic() < moving_until
                if was_speaking and not speaking:
                    # A reply just finished - start the silence clock fresh
                    # from here rather than from before the reply, so a long
                    # reply doesn't count against the user as silence.
                    last_active_time = time.monotonic()
                was_speaking = speaking

                if not speaking and not settling:
                    with lock:
                        active = doa.get_voice_active()
                        angle = doa.get_direction_degrees() if active else None
                    logger.debug("[DOA] voice_active=%s", active)
                    if active:
                        last_active_time = time.monotonic()
                        at_home = False
                        logger.info("[DOA] Stimme erkannt bei %.0f Grad.", angle)
                        # Eyes dart to the sound first, human-like, before the
                        # head starts physically catching up.
                        face.set_eye_direction(angle_degrees=angle)
                        time.sleep(eye_lead_s)

                        moved = turntable.predict_relative_move(angle)
                        if moved > 0.5:
                            settle_s = settle_base_s + moved / settle_deg_per_s
                            moving_until = time.monotonic() + settle_s
                            # Eyes drift back to center over the same span the
                            # head takes to physically arrive, so both land
                            # aligned together instead of the eyes staying
                            # cocked to the side after the head catches up.
                            face.animate_eye_direction(90.0, duration_s=settle_s)
                            # Glides there over settle_s instead of jumping in
                            # one PWM update - an instant snap looks abrupt,
                            # this reads as natural head motion. Blocks this
                            # thread for the ramp's duration, which is fine:
                            # nothing else would happen during settling anyway.
                            turntable.rotate_towards(target_angle_degrees=angle, ramp_duration_s=settle_s)
                        else:
                            turntable.rotate_towards(target_angle_degrees=angle)
                            face.set_eye_direction(angle_degrees=90.0)
                    elif (
                        not at_home
                        and silence_timeout_s
                        and silence_timeout_s > 0
                        and time.monotonic() - last_active_time >= silence_timeout_s
                    ):
                        logger.info("[DOA] %.0fs Stille - Kopf kehrt zur Home-Position zurueck.", silence_timeout_s)
                        settle_s = settle_base_s + turntable.predict_home_move() / settle_deg_per_s
                        moving_until = time.monotonic() + settle_s
                        face.animate_eye_direction(90.0, duration_s=settle_s)
                        turntable.home(ramp_duration_s=settle_s)
                        at_home = True
            except Exception:
                logger.exception("[DOA] Fehler beim Lesen/Ansteuern - Tracking-Thread beendet sich NICHT.")
            time.sleep(poll_interval_s)

    threading.Thread(target=_tracking_loop, daemon=True).start()
