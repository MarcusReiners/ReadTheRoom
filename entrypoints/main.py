import os
import subprocess
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domain.conversation import ConversationState
from domain.events import ListeningStateChanged
from domain.pause_resume import PauseResumeController
from service_layer.bus import EventBus
from service_layer.handlers import register_handlers

from adapters.stt_whisper import WhisperCppAdapter as WhisperSTTAdapter
from adapters.tts_piper import PiperTTSAdapter
from adapters.llm_gateway import LLMGatewayAdapter
from adapters.radar_ld2450 import DummyRadarAdapter
from adapters.face_display import DummyFaceDisplayAdapter
from adapters.status_display import DummyStatusDisplayAdapter
from adapters.turntable import DummyTurntableAdapter
from adapters.buttons import DummyButtonsAdapter

USE_LED_MATRIX = True
USE_HDMI_EYES = False
LLM_URL = "http://192.168.178.37:11434/api/generate"
LLM_MODEL = "qwen3.5:9b"
WHISPER_HOST, WHISPER_PORT = "127.0.0.1", 8081
WHISPER_BINARY = "/home/marcus/whisper.cpp/build/bin/whisper-server"
WHISPER_MODEL = "/home/marcus/whisper.cpp/models/ggml-tiny-german-q5_0.bin"
PIPER_MODEL = "/home/marcus/piper-voices/de_DE-thorsten-low.onnx"
MIC_DEVICE = "hw:ArrayUAC10,0"
MIC_CHANNELS = 6
MIC_RATE = 16000
SPEAKER_DEVICE = "plughw:CARD=ArrayUAC10,DEV=0"
MAX_RECORD_SECONDS = 30


def record_audio(output_file: str) -> bool:
    """Push-to-talk: Aufnahme laeuft, bis Enter gedrueckt wird (max.
    MAX_RECORD_SECONDS). 6-Kanal-Aufnahme als Raw-PCM, Kanal 0
    (beamformt) per sox extrahieren."""
    raw_file = "temp_in_raw.pcm"
    try:
        proc = subprocess.Popen(
            ["arecord", "-D", MIC_DEVICE, "-t", "raw",
             "-r", str(MIC_RATE), "-f", "S16_LE", "-c", str(MIC_CHANNELS), raw_file],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )

        watchdog = threading.Timer(MAX_RECORD_SECONDS, proc.terminate)
        watchdog.start()
        try:
            input()
        finally:
            watchdog.cancel()
            proc.terminate()
            proc.wait()

        if not os.path.exists(raw_file) or os.path.getsize(raw_file) == 0:
            stderr = proc.stderr.read().decode().strip() if proc.stderr else ""
            print(f"  arecord Fehler: {stderr or 'keine Audiodaten aufgenommen'}")
            return False

        result = subprocess.run(
            ["sox", "-t", "raw", "-r", str(MIC_RATE), "-e", "signed", "-b", "16",
             "-c", str(MIC_CHANNELS), raw_file, "-c", "1", output_file, "remix", "1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            print(f"  sox Fehler: {result.stderr.decode().strip()}")
            return False

        return os.path.exists(output_file)
    finally:
        if os.path.exists(raw_file):
            os.remove(raw_file)


def main() -> None:
    print("\nOffice Assistant — Phase 1 (Code-Skelett, Dummy-Hardware)\n")

    bus = EventBus()
    conversation = ConversationState()
    pause_controller = PauseResumeController()

    stt = WhisperSTTAdapter(
        server_binary=WHISPER_BINARY, model_path=WHISPER_MODEL,
        host=WHISPER_HOST, port=WHISPER_PORT,
    )
    tts = PiperTTSAdapter(bus=bus, model_path=PIPER_MODEL, speaker_device=SPEAKER_DEVICE)
    llm = LLMGatewayAdapter(url=LLM_URL, model=LLM_MODEL)

    radar = DummyRadarAdapter(bus=bus)
    turntable = DummyTurntableAdapter()
    buttons = DummyButtonsAdapter(bus=bus)

    if USE_LED_MATRIX:
        from adapters.led_matrix import LedMatrix
        from adapters.status_display import StatusDisplayAdapter

        matrix = LedMatrix(rows=48, cols=96, chain=1)
        status = StatusDisplayAdapter(bus=bus)
        matrix.add_renderer(status)
        matrix.start()
    else:
        status = DummyStatusDisplayAdapter(bus=bus)

    if USE_HDMI_EYES:
        from adapters.face_display import HDMIFaceDisplayAdapter

        face = HDMIFaceDisplayAdapter(bus=bus)
        face.start()
    else:
        face = DummyFaceDisplayAdapter(bus=bus)

    register_handlers(bus, conversation, pause_controller, tts, status)

    radar.start()
    buttons.start()

    print("\nENTER startet die Aufnahme, ENTER stoppt sie. "
          "'s' + Enter = Not-Stopp, 't' + Enter = Antwort auf Display umleiten.\n")

    while True:
        try:
            if pause_controller.is_paused:
                print("System pausiert. Drücke 's' erneut für Fortsetzen/Verwerfen-Abfrage.")
                input()
                continue

            input()

            temp_in = "temp_in.wav"
            print("Aufnahme läuft... sprechen und mit ENTER beenden.")
            bus.publish(ListeningStateChanged(listening=True))

            success = record_audio(temp_in)
            bus.publish(ListeningStateChanged(listening=False))
            print("Aufnahme beendet." if success else "Aufnahme fehlgeschlagen.")
            if not success:
                continue

            user_text = stt.transcribe(temp_in)
            if os.path.exists(temp_in):
                os.remove(temp_in)

            if not user_text or len(user_text.strip()) < 2:
                print("Whisper hat im Audio nichts erkannt.")
                continue

            print(f"Du hast gesagt: '{user_text}'")
            conversation.add_user_message(user_text)

            face.set_eye_direction(angle_degrees=180.0)
            turntable.rotate_towards(target_angle_degrees=180.0)

            ki_antwort = tts.speak_stream(llm.ask_stream(user_text))
            conversation.add_assistant_message(ki_antwort)

        except KeyboardInterrupt:
            print("\nCiao!")
            stt.stop()
            break


if __name__ == "__main__":
    main()
