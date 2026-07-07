import os
import re
import subprocess
import threading
import time
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
        self.takeover_sink = None
        self._aplay_process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._takeover = threading.Event()
        self._streaming = False
        self._full_text = ""
        self._started_published = False

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

    def request_takeover(self) -> bool:
        if not self._streaming:
            return False
        self._takeover.set()
        with self._lock:
            if self._aplay_process is not None:
                self._aplay_process.terminate()
                self._aplay_process = None
        if self.takeover_sink is not None:
            self.takeover_sink(self._full_text)
        return True

    def _notify_started(self) -> None:
        if not self._started_published:
            self._started_published = True
            print(f"  [timing] erster Ton: {time.monotonic() - self._t_stream_start:.2f}s nach LLM-Start")
            self.bus.publish(SpeechPlaybackStarted(text=self._full_text))

    def _emit(self, aplay_proc: subprocess.Popen, sentence: str, on_first_chunk=None) -> None:
        if self._takeover.is_set():
            return
        try:
            for i, chunk in enumerate(self._voice.synthesize(sentence)):
                if i == 0 and on_first_chunk is not None:
                    on_first_chunk()
                aplay_proc.stdin.write(chunk.audio_int16_bytes)
        except (BrokenPipeError, ValueError, OSError):
            if not self._takeover.is_set():
                raise

    def speak_stream(self, text_chunks: Iterable[str]) -> str:
        """Verarbeitet Text-Deltas (z.B. von LLMGatewayAdapter.ask_stream):
        sobald ein Satz im Puffer vollstaendig ist, wird er synthetisiert und
        in die noch laufende aplay-Pipe geschrieben, waehrend das LLM den
        naechsten Satz generiert. Bei Takeover ('t') wird die komplette
        bisherige Antwort an takeover_sink uebergeben und dort fortgesetzt.
        Gibt die vollstaendige Antwort zurueck."""
        full_text = ""
        buffer = ""
        self._full_text = ""
        self._started_published = False
        self._t_stream_start = time.monotonic()
        self._takeover.clear()
        self._streaming = True
        aplay_proc = self._spawn_aplay()

        with self._lock:
            self._aplay_process = aplay_proc

        try:
            for delta in text_chunks:
                full_text += delta
                self._full_text = full_text
                if self._takeover.is_set():
                    if self.takeover_sink is not None:
                        self.takeover_sink(full_text)
                    continue
                buffer += delta
                sentences, buffer = _split_sentences(buffer)
                for sentence in sentences:
                    self._emit(aplay_proc, sentence, on_first_chunk=self._notify_started)

            tail = buffer.strip()
            if tail and not self._takeover.is_set():
                self._emit(aplay_proc, tail, on_first_chunk=self._notify_started)

            if self._takeover.is_set():
                completed = False
            else:
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
        finally:
            self._streaming = False
