import logging
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup
from domain.conversation import ConversationState
from domain.events import ListeningStateChanged
from service_layer.bus import EventBus
from service_layer.handlers import register_handlers

from adapters.factory import build_stt, build_tts
from adapters.llm import LLMGatewayAdapter
from adapters.hardware.radar_ld2450 import DummyRadarAdapter
from adapters.hardware.face_display import DummyFaceDisplayAdapter
from adapters.hardware.status_display import DummyStatusDisplayAdapter
from adapters.hardware.turntable import DummyTurntableAdapter
from adapters.hardware.buttons import DummyButtonsAdapter

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


def main() -> None:
    logging_setup.configure_logging(config)

    bus = EventBus()
    conversation = ConversationState()

    stt = build_stt(config)
    tts = build_tts(config, bus)
    llm = LLMGatewayAdapter(
        model=config.LLM_MODEL,
        api_base=config.LLM_API_BASE,
        fallback_model=config.LLM_FALLBACK_MODEL,
        fallback_api_base=config.LLM_FALLBACK_API_BASE,
    )

    radar = DummyRadarAdapter(bus=bus)
    buttons = DummyButtonsAdapter(bus=bus)

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

        matrix = LedMatrix(rows=48, cols=96, chain=2)
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

    radar.start()
    buttons.start()

    print("\nENTER startet die Aufnahme, ENTER stoppt sie. "
          "'t' + Enter = Antwort auf Display umleiten.\n")

    while True:
        try:
            input()

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
            conversation.add_user_message(user_text)

            face.set_eye_direction(angle_degrees=90.0)
            turntable.rotate_towards(target_angle_degrees=90.0)

            ki_antwort = tts.speak_stream(llm.ask_stream(user_text))
            conversation.add_assistant_message(ki_antwort)

        except KeyboardInterrupt:
            print("\nCiao!")
            stt.stop()
            if hasattr(turntable, "stop"):
                turntable.stop()
            break


if __name__ == "__main__":
    main()
