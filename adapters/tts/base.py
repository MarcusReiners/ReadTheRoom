import logging
import re
import subprocess
import sys
import threading
import time
from typing import Iterable

try:
    import numpy as np
except ImportError:  # volume scaling degrades to a no-op, TTS still works
    np = None

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
        # Editable live from the web app's settings tab - applied per PCM
        # chunk in _emit() below, so it takes effect immediately (including
        # mid-sentence) rather than only on the next speak_stream() call.
        self.volume = 1.0

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(2.0, volume))
        if np is None and self.volume != 1.0:
            logger.warning(
                "[TTS] Lautstaerke %.2f ignoriert - numpy ist nicht installiert (pip install numpy).",
                self.volume,
            )

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
                if self.volume != 1.0 and np is not None:
                    chunk = _scale_volume(chunk, self.volume)
                aplay_proc.stdin.write(chunk)
        except (BrokenPipeError, ValueError, OSError):
            if not self._takeover.is_set():
                raise

    def speak_stream(self, text_chunks: Iterable[str]) -> str:
        full_text = ""
        buffer = ""
        completed = False
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

            logger.info("Antworte: %s", full_text)
            return full_text

        except BrokenPipeError:
            return full_text
        except Exception as e:
            logger.exception("TTS Fehler: %s: %s", type(e).__name__, e)
            return full_text
        finally:
            self._streaming = False
            with self._lock:
                self._aplay_process = None
            # MUST pair with every SpeechPlaybackStarted, on every exit path
            # including an unexpected one. main.py holds an assistant_speaking
            # Event open between these two events, and both the DOA tracking
            # loop and the VAD recording loop refuse to do anything while it's
            # set - so an exception escaping here without publishing Ended
            # (an ElevenLabs network blip or quota error was enough) left that
            # Event set forever and the assistant permanently deaf, with
            # nothing in the logs pointing at the cause.
            if self._started_published:
                self.bus.publish(SpeechPlaybackEnded(completed=completed))


def _scale_volume(chunk: bytes, volume: float) -> bytes:
    """Scales signed 16-bit PCM samples by `volume`, clipping back into
    range - applied per emitted chunk rather than re-synthesizing, so a
    volume change takes effect immediately without a round-trip to the TTS
    API. Uses numpy (already a transitive dependency here) since Python's
    stdlib `audioop` was removed in 3.13 and a pure-Python per-sample loop
    would be slow enough to matter on the Pi for a several-second chunk.

    Nothing guarantees a chunk from the TTS API lands on a 2-byte sample
    boundary, and np.frombuffer() raises on a buffer that isn't a whole
    number of int16s - so a trailing odd byte is passed through unscaled
    rather than allowed to throw. One inaudible half-sample at the seam
    beats an exception here, which _emit() re-raises and which would take
    the whole playback down."""
    tail = b""
    if len(chunk) % 2:
        chunk, tail = chunk[:-1], chunk[-1:]
    samples = np.frombuffer(chunk, dtype=np.int16)
    scaled = np.clip(samples.astype(np.float32) * volume, -32768, 32767).astype(np.int16)
    return scaled.tobytes() + tail
