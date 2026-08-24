import logging
import os
import queue
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup
from domain.conversation import ConversationState
from domain.events import (
    AssistantDeltaReceived,
    AssistantMessageCompleted,
    ListeningStateChanged,
    SpeechPlaybackEnded,
    SpeechPlaybackStarted,
    SpeechTranscribed,
)
from service_layer.bus import EventBus
from service_layer.handlers import register_handlers, register_tie_led_handlers, start_doa_tracking

from adapters.factory import build_stt, build_tts, build_radar, build_turntable
from adapters.chat_bridge import ChatBridgeAdapter
from adapters.conversation_store import ConversationStore
from adapters.llm import LLMGatewayAdapter
from adapters.hardware.face_display import DummyFaceDisplayAdapter

logger = logging.getLogger(__name__)


def _spawn_recorder(output_file: str):
    """Starts the platform recorder subprocess in the background (does not
    block). Returns (proc, raw_file) - raw_file is None on macOS since sox
    writes directly to output_file there; on ALSA it's the raw-PCM file that
    still needs converting once recording stops, in _finish_recording().
    """
    if sys.platform == "darwin":
        proc = subprocess.Popen(
            ["sox", "-d", "-r", str(config.MIC_RATE), "-c", "1", "-b", "16", output_file],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        return proc, None

    raw_file = "temp_in_raw.pcm"
    proc = subprocess.Popen(
        ["arecord", "-D", config.MIC_DEVICE, "-t", "raw",
         "-r", str(config.MIC_RATE), "-f", "S16_LE", "-c", str(config.MIC_CHANNELS), raw_file],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    return proc, raw_file


def _finish_recording(proc, raw_file, output_file: str) -> bool:
    """Stops the recorder subprocess and (on ALSA) converts raw PCM to the
    final mono wav. Shared by both the manual (ENTER) and automatic
    (VAD-triggered) capture paths - only how "stop now" gets decided differs
    between them, not what happens once it's decided."""
    proc.terminate()
    proc.wait()

    if raw_file is None:
        return os.path.exists(output_file) and os.path.getsize(output_file) > 0

    try:
        if not os.path.exists(raw_file) or os.path.getsize(raw_file) == 0:
            stderr = proc.stderr.read().decode().strip() if proc.stderr else ""
            logger.error("arecord Fehler: %s", stderr or "keine Audiodaten aufgenommen")
            return False

        result = subprocess.run(
            ["sox", "-t", "raw", "-r", str(config.MIC_RATE), "-e", "signed", "-b", "16",
             "-c", str(config.MIC_CHANNELS), raw_file, "-c", "1", output_file, "remix", "1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            logger.error("sox Fehler: %s", result.stderr.decode().strip())
            return False

        return os.path.exists(output_file)
    finally:
        if os.path.exists(raw_file):
            os.remove(raw_file)


def record_audio(output_file: str) -> bool:
    """Manual capture: starts recording immediately, stops on the next ENTER
    (or after MAX_RECORD_SECONDS, whichever comes first)."""
    proc, raw_file = _spawn_recorder(output_file)
    watchdog = threading.Timer(config.MAX_RECORD_SECONDS, proc.terminate)
    watchdog.start()
    try:
        input()
    finally:
        watchdog.cancel()
    return _finish_recording(proc, raw_file, output_file)


def _transcribe_and_enqueue(stt, turn_queue: "queue.Queue[str]", temp_in: str, log_prefix: str) -> None:
    audio_s = max(0, os.path.getsize(temp_in) - 44) / (config.MIC_RATE * 2)
    t_stt = time.monotonic()
    user_text = stt.transcribe(temp_in)
    logger.info("[timing] STT (%s): %.2fs fuer %.1fs Audio", log_prefix, time.monotonic() - t_stt, audio_s)
    if os.path.exists(temp_in):
        os.remove(temp_in)

    if not user_text or len(user_text.strip()) < 2:
        logger.warning("%s: nichts erkannt.", log_prefix)
        return

    logger.info("[%s] Du hast gesagt: '%s'", log_prefix, user_text)
    turn_queue.put(user_text)


def console_input_loop(
    bus: EventBus, turn_queue: "queue.Queue[str]", stt, mic_lock: threading.Lock,
) -> None:
    """Reads stdin on its own thread: bare ENTER starts/stops a voice recording,
    whose result is pushed onto turn_queue like any other turn. Still works
    as a manual override alongside automatic VAD-triggered recording."""
    print("\nENTER startet die Aufnahme, ENTER stoppt sie.\n")
    while True:
        try:
            line = input()
        except EOFError:
            break

        temp_in = "temp_in.wav"
        print("Aufnahme läuft... sprechen und mit ENTER beenden.")
        with mic_lock:
            bus.publish(ListeningStateChanged(listening=True))
            success = record_audio(temp_in)
            bus.publish(ListeningStateChanged(listening=False))

        if not success:
            logger.error("Aufnahme fehlgeschlagen.")
            continue

        _transcribe_and_enqueue(stt, turn_queue, temp_in, "manuell")


def vad_input_loop(
    bus: EventBus, turn_queue: "queue.Queue[str]", stt, doa, doa_lock: threading.Lock,
    mic_lock: threading.Lock, assistant_speaking: threading.Event,
    poll_interval_s: float = 0.2, trailing_silence_s: float = 0.8,
) -> None:
    """Hands-free alternative to console_input_loop: watches the ReSpeaker's
    onboard VAD and starts recording automatically once it detects speech,
    stopping after trailing_silence_s of continued silence (or
    MAX_RECORD_SECONDS, whichever comes first) instead of waiting for ENTER.

    doa_lock serializes USB control-transfer access to the ReSpeaker with
    start_doa_tracking()'s own polling loop, since both read the same device
    concurrently from different threads. mic_lock defers to a manual
    (console_input_loop) recording already in progress instead of racing it
    for the same audio device. assistant_speaking skips triggering entirely
    while TTS is playing - the mic picks up the assistant's own voice same as
    any other sound, and without this guard that reliably self-triggers a
    "recording" of the assistant talking to itself.
    """
    while True:
        if assistant_speaking.is_set():
            time.sleep(poll_interval_s)
            continue

        with doa_lock:
            active = doa.get_voice_active()
        if not active:
            time.sleep(poll_interval_s)
            continue

        if not mic_lock.acquire(blocking=False):
            # A manual recording is already running - don't fight it for the
            # mic, just wait for the next VAD trigger after it's done.
            time.sleep(poll_interval_s)
            continue

        try:
            temp_in = "temp_in_vad.wav"
            bus.publish(ListeningStateChanged(listening=True))
            proc, raw_file = _spawn_recorder(temp_in)

            start_time = time.monotonic()
            last_active_time = start_time
            while True:
                time.sleep(poll_interval_s)
                with doa_lock:
                    still_active = doa.get_voice_active()
                if still_active:
                    last_active_time = time.monotonic()
                if time.monotonic() - last_active_time >= trailing_silence_s:
                    break
                if time.monotonic() - start_time >= config.MAX_RECORD_SECONDS:
                    break

            bus.publish(ListeningStateChanged(listening=False))
            success = _finish_recording(proc, raw_file, temp_in)
        finally:
            mic_lock.release()

        if not success:
            logger.error("VAD-Aufnahme fehlgeschlagen.")
            continue

        _transcribe_and_enqueue(stt, turn_queue, temp_in, "VAD")


def handle_turn(
    text: str, bus: EventBus, conversation: ConversationState, store: ConversationStore,
    llm, tts, face, turntable,
) -> None:
    conversation_id = store.get_active_id()
    history = store.get_history(conversation_id)

    store.add_user_message(conversation_id, text)
    bus.publish(SpeechTranscribed(text=text, conversation_id=conversation_id))

    face.set_eye_direction(angle_degrees=90.0)
    turntable.rotate_towards(target_angle_degrees=90.0)

    def mirrored_deltas():
        for delta in llm.ask_stream(text, history):
            bus.publish(AssistantDeltaReceived(delta=delta, conversation_id=conversation_id))
            yield delta

    # Speak aloud only while alone (or voice hasn't been muted from the chat
    # app) - otherwise just stream the answer as text into the chat.
    should_speak = conversation.voice_enabled and conversation.modality == "voice"
    if should_speak:
        full_text = tts.speak_stream(mirrored_deltas())
    else:
        full_text = "".join(mirrored_deltas())

    store.add_assistant_message(conversation_id, full_text)
    bus.publish(AssistantMessageCompleted(text=full_text, conversation_id=conversation_id))


def main() -> None:
    logging_setup.configure_logging(config)

    bus = EventBus()
    conversation = ConversationState()
    store = ConversationStore(config.CONVERSATIONS_DB_PATH)
    if store.get_active_id() is None:
        store.set_active_id(store.create_conversation())
    turn_queue: "queue.Queue[str]" = queue.Queue()

    stt = build_stt(config)
    tts = build_tts(config, bus)
    llm = LLMGatewayAdapter(
        model=config.LLM_MODEL,
        api_base=config.LLM_API_BASE,
        fallback_model=config.LLM_FALLBACK_MODEL,
        fallback_api_base=config.LLM_FALLBACK_API_BASE,
    )

    radar = build_radar(config, bus)
    turntable = build_turntable(config)

    if config.USE_LED_MATRIX:
        from adapters.hardware.led_matrix import LedMatrix
        from adapters.hardware.led_eyes import LedEyesAdapter

        matrix = LedMatrix(
            rows=48, cols=96, chain=1,
            gpio_slowdown=config.GPIO_SLOWDOWN,
            brightness=config.LED_MATRIX_BRIGHTNESS,
            pwm_bits=config.LED_MATRIX_PWM_BITS,
        )
        face = LedEyesAdapter(bus=bus, x_offset=0, width=96, height=48)
        matrix.add_renderer(face)
        matrix.start()
    else:
        face = DummyFaceDisplayAdapter(bus=bus)

    register_handlers(bus, conversation, tts)

    chat_bridge = ChatBridgeAdapter(
        bus, conversation, store, turn_queue,
        radar=radar,
        turntable=turntable,
        servo_calibration_path=config.SERVO_CALIBRATION_PATH,
        host=config.CHAT_BRIDGE_HOST, port=config.CHAT_BRIDGE_PORT,
    )
    chat_bridge.start()

    radar.start()
    register_tie_led_handlers(bus, radar)

    mic_lock = threading.Lock()

    if config.USE_SERVO:
        from adapters.hardware.doa_respeaker import RespeakerDOAAdapter

        doa = RespeakerDOAAdapter(front_reference_degrees=config.DOA_FRONT_REFERENCE_DEGREES)
        doa_lock = threading.Lock()
        start_doa_tracking(doa, turntable, face, lock=doa_lock)

        assistant_speaking = threading.Event()
        bus.subscribe(SpeechPlaybackStarted, lambda e: assistant_speaking.set())
        bus.subscribe(SpeechPlaybackEnded, lambda e: assistant_speaking.clear())
        threading.Thread(
            target=vad_input_loop,
            args=(bus, turn_queue, stt, doa, doa_lock, mic_lock, assistant_speaking),
            daemon=True,
        ).start()

    threading.Thread(target=console_input_loop, args=(bus, turn_queue, stt, mic_lock), daemon=True).start()

    print(f"\nChat: http://<Pi-Adresse>:{config.CHAT_BRIDGE_PORT}\n")

    while True:
        try:
            text = turn_queue.get()
            handle_turn(text, bus, conversation, store, llm, tts, face, turntable)
        except KeyboardInterrupt:
            print("\nCiao!")
            stt.stop()
            if hasattr(turntable, "stop"):
                turntable.stop()
            break


if __name__ == "__main__":
    main()
