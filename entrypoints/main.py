import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domain.conversation import ConversationState
from domain.events import ListeningStateChanged
from service_layer.bus import EventBus
from service_layer.handlers import register_handlers

from adapters.stt_whisper import WhisperSTTAdapter
from adapters.tts_piper import PiperTTSAdapter
from adapters.llm_gateway import LLMGatewayAdapter
from adapters.radar_ld2450 import DummyRadarAdapter
from adapters.face_display import DummyFaceDisplayAdapter
from adapters.status_display import DummyStatusDisplayAdapter
from adapters.turntable import DummyTurntableAdapter
from adapters.buttons import DummyButtonsAdapter

USE_LED_MATRIX = True
USE_HDMI_EYES = False
LLM_MODEL = "ollama_chat/qwen3.5:9b"
LLM_API_BASE = "http://192.168.178.37:11434"
WHISPER_MODEL = "/home/marcus/voice-pipeline/whisper-data/whisper-tiny-german-1224-ct2"
WHISPER_THREADS = 4
WHISPER_VAD = False
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
    bus = EventBus()
    conversation = ConversationState()

    stt = WhisperSTTAdapter(model_path=WHISPER_MODEL, cpu_threads=WHISPER_THREADS, vad=WHISPER_VAD)
    tts = PiperTTSAdapter(bus=bus, model_path=PIPER_MODEL, speaker_device=SPEAKER_DEVICE)
    llm = LLMGatewayAdapter(model=LLM_MODEL, api_base=LLM_API_BASE)

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
                print("Aufnahme fehlgeschlagen.")
                continue

            audio_s = max(0, os.path.getsize(temp_in) - 44) / (MIC_RATE * 2)
            t_stt = time.monotonic()
            user_text = stt.transcribe(temp_in)
            print(f"  [timing] STT: {time.monotonic() - t_stt:.2f}s fuer {audio_s:.1f}s Audio")
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
