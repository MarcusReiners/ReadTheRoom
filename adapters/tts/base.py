import logging
import re
import subprocess
import sys
import threading
import time
from typing import Iterable

from domain.events import SpeechPlaybackStarted, SpeechPlaybackEnded
from service_layer.bus import EventBus

logger = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")


def _split_sentences(buffer: str, first: bool = False) -> tuple[list[str], str]:
    parts = _SENTENCE_END.split(buffer)
    if first and len(parts) == 1:
        parts = _CLAUSE_END.split(buffer, maxsplit=1)
    complete, rest = parts[:-1], parts[-1]
    return [p for p in complete if p.strip()], rest


class StreamingTTSAdapter:
    """Base class for sentence-by-sentence streaming TTS adapters.

    Subclasses implement `sample_rate` and `_synthesize_chunks(sentence)`
    (raw PCM16 mono bytes); everything else (sentence buffering, aplay
    piping, takeover handling, playback events) is shared.
    """

    def __init__(self, bus: EventBus, speaker_device: str = "default") -> None:
        self.bus = bus
        self.speaker_device = speaker_device
        self._aplay_process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._takeover = threading.Event()
        self._streaming = False
        self._full_text = ""
        self._started_published = False

    @property
    def sample_rate(self) -> int:
        raise NotImplementedError

    def _synthesize_chunks(self, sentence: str) -> Iterable[bytes]:
        raise NotImplementedError

    def _spawn_aplay(self) -> subprocess.Popen:
        if sys.platform == "darwin":
            cmd = ["play", "-q", "-t", "raw", "-r", str(self.sample_rate),
                   "-e", "signed", "-b", "16", "-c", "1", "-"]
        else:
            cmd = ["aplay", "-D", self.speaker_device, "-r", str(self.sample_rate),
                   "-f", "S16_LE", "-c", "1", "-t", "raw", "-"]
        return subprocess.Popen(
            cmd,
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
        return True

    def _notify_started(self) -> None:
        if not self._started_published:
            self._started_published = True
            logger.info("[timing] erster Ton: %.2fs nach LLM-Start", time.monotonic() - self._t_stream_start)
            self.bus.publish(SpeechPlaybackStarted(text=self._full_text))

    def _emit(self, aplay_proc: subprocess.Popen, sentence: str, on_first_chunk=None) -> None:
        if self._takeover.is_set():
            return
        try:
            for i, chunk in enumerate(self._synthesize_chunks(sentence)):
                if i == 0 and on_first_chunk is not None:
                    on_first_chunk()
                aplay_proc.stdin.write(chunk)
        except (BrokenPipeError, ValueError, OSError):
            if not self._takeover.is_set():
                raise

    def speak_stream(self, text_chunks: Iterable[str]) -> str:
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
                    continue
                buffer += delta
                sentences, buffer = _split_sentences(buffer, first=not self._started_published)
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
            logger.info("Antworte: %s", full_text)
            return full_text

        except BrokenPipeError:
            with self._lock:
                self._aplay_process = None
            self.bus.publish(SpeechPlaybackEnded(completed=False))
            return full_text
        except Exception as e:
            logger.exception("TTS Fehler: %s: %s", type(e).__name__, e)
            with self._lock:
                self._aplay_process = None
            return full_text
        finally:
            self._streaming = False
