"""
Piper TTS Adapter (ersetzt Wyoming-Piper Docker).

Nutzt das piper-tts Python-Package direkt statt ueber Wyoming-Protokoll.
Vorteile: kein Docker, kein TCP-Overhead, direktes Streaming moeglich.

VORAUSSETZUNGEN (einmalig):
    pip install piper-tts
    mkdir -p ~/piper-voices
    wget .../de_DE-thorsten-low.onnx -P ~/piper-voices/
    wget .../de_DE-thorsten-low.onnx.json -P ~/piper-voices/
"""

import os
import signal
import subprocess
import threading

from piper.voice import PiperVoice

from domain.events import SpeechPlaybackStarted, SpeechPlaybackEnded
from service_layer.bus import EventBus


class PiperTTSAdapter:
    def __init__(
        self,
        bus: EventBus,
        model_path: str = "/home/marcus/piper-voices/de_DE-thorsten-low.onnx",
        speaker_device: str = "hw:3,0",
    ) -> None:
        self.bus = bus
        self.speaker_device = speaker_device
        self._aplay_process: subprocess.Popen | None = None
        self._lock = threading.Lock()

        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Piper-Modell nicht gefunden: {model_path}\n"
                "Bitte laden: wget .../de_DE-thorsten-low.onnx -P ~/piper-voices/"
            )
        print("  Lade Piper-Stimme...")
        self._voice = PiperVoice.load(model_path)
        print("  Piper bereit.")

    def speak(self, text: str) -> None:
        print(f"Antworte: {text}")
        try:
            sample_rate = self._voice.config.sample_rate
            aplay_proc = subprocess.Popen(
                [
                    "aplay",
                    "-D", self.speaker_device,
                    "-r", str(sample_rate),
                    "-f", "S16_LE",
                    "-c", "1",
                    "-t", "raw",
                    "-",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            with self._lock:
                self._aplay_process = aplay_proc

            self.bus.publish(SpeechPlaybackStarted(text=text))

            with aplay_proc.stdin as pipe:
                for chunk in self._voice.synthesize(text):
                    audio_bytes = chunk.audio_int16_bytes
                    pipe.write(audio_bytes)

            aplay_proc.wait()
            completed = aplay_proc.returncode == 0

            with self._lock:
                self._aplay_process = None

            self.bus.publish(SpeechPlaybackEnded(completed=completed))

        except BrokenPipeError:
            self.bus.publish(SpeechPlaybackEnded(completed=False))
        except Exception as e:
            print(f"  TTS Fehler: {type(e).__name__}: {e}")

    def pause(self) -> None:
        """SIGSTOP – Wiedergabe friert ein, Prozess bleibt erhalten (ADR-002, 11)."""
        with self._lock:
            if self._aplay_process is not None:
                self._aplay_process.send_signal(signal.SIGSTOP)
                print("  Wiedergabe pausiert (SIGSTOP).")

    def resume(self) -> None:
        with self._lock:
            if self._aplay_process is not None:
                self._aplay_process.send_signal(signal.SIGCONT)
                print("  Wiedergabe fortgesetzt (SIGCONT).")

    def discard(self) -> None:
        """Beendet die pausierte Wiedergabe endgueltig (Nutzerentscheidung: verwerfen)."""
        with self._lock:
            if self._aplay_process is not None:
                self._aplay_process.send_signal(signal.SIGCONT)
                self._aplay_process.terminate()
                self._aplay_process = None
                print("  Antwort verworfen.")
