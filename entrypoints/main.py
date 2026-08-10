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
    DisplayTakeoverRequested,
    ListeningStateChanged,
    SpeechTranscribed,
)
from service_layer.bus import EventBus
from service_layer.handlers import register_handlers

from adapters.factory import build_stt, build_tts, build_radar
from adapters.chat_bridge import ChatBridgeAdapter
from adapters.llm import LLMGatewayAdapter
from adapters.hardware.face_display import DummyFaceDisplayAdapter
from adapters.hardware.status_display import DummyStatusDisplayAdapter
from adapters.hardware.turntable import DummyTurntableAdapter

logger = logging.getLogger(__name__)


def record_audio(output_file: str) -> bool:
    if sys.platform == "darwin":
        return _record_audio_mac(output_file)
    return _record_audio_alsa(output_file)


def _record_audio_mac(output_file: str) -> bool:
    proc = subprocess.Popen(
        ["sox", "-d", "-r", str(config.MIC_RATE), "-c", "1", "-b", "16", output_file],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    watchdog = threading.Timer(config.MAX_RECORD_SECONDS, proc.terminate)
    watchdog.start()
    try:
        input()
    finally:
        watchdog.cancel()
        proc.terminate()
        proc.wait()

    return os.path.exists(output_file) and os.path.getsize(output_file) > 0


def _record_audio_alsa(output_file: str) -> bool:
    raw_file = "temp_in_raw.pcm"
    try:
        proc = subprocess.Popen(
            ["arecord", "-D", config.MIC_DEVICE, "-t", "raw",
             "-r", str(config.MIC_RATE), "-f", "S16_LE", "-c", str(config.MIC_CHANNELS), raw_file],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )

        watchdog = threading.Timer(config.MAX_RECORD_SECONDS, proc.terminate)
        watchdog.start()
        try:
            input()
        finally:
            watchdog.cancel()
            proc.terminate()
            proc.wait()

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


def console_input_loop(bus: EventBus, turn_queue: "queue.Queue[str]", stt) -> None:
    """Reads stdin on its own thread: bare ENTER starts/stops a voice
    recording (result is pushed onto turn_queue like any other turn), 't'
    requests a display/chat takeover of the current answer. This is the
    single stdin reader for the process - it also covers what used to be
    DummyButtonsAdapter's separate 't'-listening thread, which raced with
    the old inline ENTER loop over the same stdin.
    """
    print("\nENTER startet die Aufnahme, ENTER stoppt sie. "
          "'t' + Enter = Antwort auf Display/Chat umleiten.\n")
    while True:
        try:
            line = input()
        except EOFError:
            break

        if line.strip().lower() == "t":
            bus.publish(DisplayTakeoverRequested())
            continue

        temp_in = "temp_in.wav"
        print("Aufnahme läuft... sprechen und mit ENTER beenden.")
        bus.publish(ListeningStateChanged(listening=True))

        success = record_audio(temp_in)
        bus.publish(ListeningStateChanged(listening=False))
        if not success:
            logger.error("Aufnahme fehlgeschlagen.")
            continue

        audio_s = max(0, os.path.getsize(temp_in) - 44) / (config.MIC_RATE * 2)
        t_stt = time.monotonic()
        user_text = stt.transcribe(temp_in)
        logger.info("[timing] STT: %.2fs fuer %.1fs Audio", time.monotonic() - t_stt, audio_s)
        if os.path.exists(temp_in):
            os.remove(temp_in)

        if not user_text or len(user_text.strip()) < 2:
            logger.warning("Whisper hat im Audio nichts erkannt.")
            continue

        logger.info("Du hast gesagt: '%s'", user_text)
        turn_queue.put(user_text)


def handle_turn(text: str, bus: EventBus, conversation: ConversationState, llm, tts, face, turntable) -> None:
    conversation.add_user_message(text)
    bus.publish(SpeechTranscribed(text=text))

    face.set_eye_direction(angle_degrees=90.0)
    turntable.rotate_towards(target_angle_degrees=90.0)

    def mirrored_deltas():
        for delta in llm.ask_stream(text):
            bus.publish(AssistantDeltaReceived(delta=delta))
            yield delta

    # Speak aloud only while alone (or voice hasn't been muted from the chat
    # app) - otherwise just stream the answer as text into the chat.
    should_speak = conversation.voice_enabled and conversation.modality == "voice"
    if should_speak:
        full_text = tts.speak_stream(mirrored_deltas())
    else:
        full_text = "".join(mirrored_deltas())

    conversation.add_assistant_message(full_text)
    bus.publish(AssistantMessageCompleted(text=full_text))


def main() -> None:
    logging_setup.configure_logging(config)

    bus = EventBus()
    conversation = ConversationState()
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

    if config.USE_SERVO:
        from adapters.hardware.turntable import ServoTurntableAdapter

        turntable = ServoTurntableAdapter(
            pin=config.SERVO_GPIO_PIN,
            min_angle=config.SERVO_MIN_ANGLE,
            max_angle=config.SERVO_MAX_ANGLE,
        )
    else:
        turntable = DummyTurntableAdapter()

    if config.USE_LED_MATRIX:
        from adapters.hardware.led_matrix import LedMatrix
        from adapters.hardware.status_display import StatusDisplayAdapter
        from adapters.hardware.led_eyes import LedEyesAdapter

        matrix = LedMatrix(rows=48, cols=96, chain=2, gpio_slowdown=config.GPIO_SLOWDOWN)
        status_offset, eyes_offset = (96, 0) if config.SWAP_LED_PANELS else (0, 96)
        status = StatusDisplayAdapter(bus=bus, x_offset=status_offset, width=96)
        face = LedEyesAdapter(bus=bus, x_offset=eyes_offset, width=96, height=48)
        matrix.add_renderer(status)
        matrix.add_renderer(face)
        matrix.start()
    else:
        status = DummyStatusDisplayAdapter(bus=bus)
        face = DummyFaceDisplayAdapter(bus=bus)

    register_handlers(bus, conversation, tts, status)

    chat_bridge = ChatBridgeAdapter(
        bus, conversation, turn_queue,
        radar_url=config.RADAR_URL,
        host=config.CHAT_BRIDGE_HOST, port=config.CHAT_BRIDGE_PORT,
    )
    chat_bridge.start()

    radar.start()

    threading.Thread(target=console_input_loop, args=(bus, turn_queue, stt), daemon=True).start()

    print(f"\nChat: http://<Pi-Adresse>:{config.CHAT_BRIDGE_PORT}\n")

    while True:
        try:
            text = turn_queue.get()
            handle_turn(text, bus, conversation, llm, tts, face, turntable)
        except KeyboardInterrupt:
            print("\nCiao!")
            stt.stop()
            if hasattr(turntable, "stop"):
                turntable.stop()
            break


if __name__ == "__main__":
    main()
