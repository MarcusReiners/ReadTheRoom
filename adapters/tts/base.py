import logging
import queue
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

try:
    import fcntl
except ImportError:
    fcntl = None

from domain.events import SpeechPlaybackStarted, SpeechPlaybackEnded
from service_layer.bus import EventBus

logger = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")

# Volume is applied as late as possible so a change (the privacy duck above
# all) is heard within ~0.3 s: 32 ms blocks, the smallest pipe Linux allows
# (~0.13 s of 16 kHz audio) and a 150 ms ALSA buffer. Before, a 64 KB pipe
# plus aplay's default buffer held ~2.5 s of already-scaled audio, so a volume
# change arrived seconds late. The look-ahead that bridges gaps between
# sentences now lives in a Python queue in front of the writer instead.
_BLOCK_BYTES = 1024
_PIPE_BYTES = 4096
_ALSA_BUFFER_US = 150000


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
        # Editable live from the web app's settings tab - applied per block in
        # _write_block() below, so it takes effect mid-sentence rather than
        # only on the next speak_stream() call.
        self.volume = 1.0
        # Fraction of the volume used while someone stands at the door (see
        # PrivacyGuard); set_duck() toggles it.
        self.duck_gain = 0.35
        self._ducked = False
        self._gain = 1.0

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(2.0, volume))
        if np is None and self.volume != 1.0:
            logger.warning(
                "[TTS] Lautstaerke %.2f ignoriert - numpy ist nicht installiert (pip install numpy).",
                self.volume,
            )

    def set_duck(self, active: bool) -> None:
        self._ducked = bool(active)
        if np is None and active:
            logger.warning("[TTS] cannot lower the voice - numpy is not installed (pip install numpy).")

    def _target_gain(self) -> float:
        return self.volume * (self.duck_gain if self._ducked else 1.0)

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
                   "-f", "S16_LE", "-c", "1", "-t", "raw", f"--buffer-time={_ALSA_BUFFER_US}", "-"]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        if fcntl is not None and hasattr(fcntl, "F_SETPIPE_SZ"):
            try:
                fcntl.fcntl(proc.stdin.fileno(), fcntl.F_SETPIPE_SZ, _PIPE_BYTES)
            except OSError:
                pass
        return proc

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

    def _emit(self, audio: queue.Queue, sentence: str, on_first_chunk=None) -> None:
        if self._takeover.is_set():
            return
        for i, chunk in enumerate(self._synthesize_chunks(sentence)):
            if self._takeover.is_set():
                return
            if i == 0 and on_first_chunk is not None:
                on_first_chunk()
            audio.put(chunk)

    def _writer(self, proc: subprocess.Popen, audio: queue.Queue, abort: threading.Event) -> None:
        carry = b""
        try:
            while True:
                item = audio.get()
                if item is None or abort.is_set() or self._takeover.is_set():
                    return
                data = carry + item
                usable = len(data) - len(data) % 2
                data, carry = data[:usable], data[usable:]
                for i in range(0, len(data), _BLOCK_BYTES):
                    if abort.is_set() or self._takeover.is_set():
                        return
                    self._write_block(proc, data[i:i + _BLOCK_BYTES])
        except (BrokenPipeError, ValueError, OSError):
            return

    def _write_block(self, proc: subprocess.Popen, block: bytes) -> None:
        """Applies the current gain as the block goes out, ramping linearly
        from the previous gain across the block so a change never clicks."""
        start, target = self._gain, self._target_gain()
        self._gain = target
        if np is not None and (start != 1.0 or target != 1.0):
            samples = np.frombuffer(block, dtype=np.int16).astype(np.float32)
            if start != target:
                samples *= np.linspace(start, target, len(samples), dtype=np.float32)
            else:
                samples *= target
            block = np.clip(samples, -32768, 32767).astype(np.int16).tobytes()
        proc.stdin.write(block)

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
        audio: queue.Queue = queue.Queue()
        abort = threading.Event()
        self._gain = self._target_gain()
        writer = threading.Thread(target=self._writer, args=(aplay_proc, audio, abort),
                                  daemon=True, name="tts-writer")
        writer.start()

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
                    self._emit(audio, sentence, on_first_chunk=self._notify_started)

            tail = buffer.strip()
            if tail and not self._takeover.is_set():
                self._emit(audio, tail, on_first_chunk=self._notify_started)

            audio.put(None)
            writer.join()
            if self._takeover.is_set():
                completed = False
            else:
                aplay_proc.stdin.close()
                aplay_proc.wait()
                completed = aplay_proc.returncode == 0

            logger.info("Replying: %s", full_text)
            return full_text

        except BrokenPipeError:
            return full_text
        except Exception as e:
            logger.exception("TTS error: %s: %s", type(e).__name__, e)
            return full_text
        finally:
            abort.set()
            audio.put(None)
            try:
                aplay_proc.stdin.close()
            except OSError:
                pass
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
