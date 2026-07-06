import os
import re
import signal
import subprocess
import threading
from typing import Iterable

from piper.voice import PiperVoice

from domain.events import SpeechPlaybackStarted, SpeechPlaybackEnded
from service_layer.bus import EventBus

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(buffer: str) -> tuple[list[str], str]:
    """Trennt vollstaendige Saetze vom Puffer ab; der Rest (moeglicherweise
    unvollstaendiger letzter Satz) wird als neuer Puffer zurueckgegeben."""
    parts = _SENTENCE_END.split(buffer)
    complete, rest = parts[:-1], parts[-1]
    return [p for p in complete if p.strip()], rest


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

    def _spawn_aplay(self) -> subprocess.Popen:
        sample_rate = self._voice.config.sample_rate
        return subprocess.Popen(
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

    def speak_stream(self, text_chunks: Iterable[str]) -> str:
        """Verarbeitet Text-Deltas (z.B. von LLMGatewayAdapter.ask_stream):
        sobald ein Satz im Puffer vollstaendig ist, wird er synthetisiert und
        in die noch laufende aplay-Pipe geschrieben, waehrend das LLM den
        naechsten Satz generiert. Gibt die vollstaendig zusammengesetzte
        Antwort zurueck (fuer die Conversation History)."""
        full_text = ""
        buffer = ""
        started = False
        aplay_proc = self._spawn_aplay()

        with self._lock:
            self._aplay_process = aplay_proc

        try:
            for delta in text_chunks:
                full_text += delta
                buffer += delta
                sentences, buffer = _split_sentences(buffer)
                for sentence in sentences:
                    if not started:
                        self.bus.publish(SpeechPlaybackStarted(text=full_text))
                        started = True
                    for chunk in self._voice.synthesize(sentence):
                        aplay_proc.stdin.write(chunk.audio_int16_bytes)

            tail = buffer.strip()
            if tail:
                if not started:
                    self.bus.publish(SpeechPlaybackStarted(text=full_text))
                    started = True
                for chunk in self._voice.synthesize(tail):
                    aplay_proc.stdin.write(chunk.audio_int16_bytes)

            aplay_proc.stdin.close()
            aplay_proc.wait()
            completed = aplay_proc.returncode == 0

            with self._lock:
                self._aplay_process = None

            self.bus.publish(SpeechPlaybackEnded(completed=completed))
            print(f"Antworte: {full_text}")
            return full_text

        except BrokenPipeError:
            with self._lock:
                self._aplay_process = None
            self.bus.publish(SpeechPlaybackEnded(completed=False))
            return full_text
        except Exception as e:
            print(f"  TTS Fehler: {type(e).__name__}: {e}")
            with self._lock:
                self._aplay_process = None
            return full_text

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
