import collections
import logging
import subprocess
import threading
import time

logger = logging.getLogger(__name__)


class MicStream:
    """Keeps the microphone open while listening is allowed and holds the
    last preroll_s of audio in memory, so a recording starts with the first
    syllable of the sentence. Starting a recorder only once voice activity
    is seen loses 0.2-0.4 s - the poll interval, the array's own detection
    delay and the recorder's start-up - which is enough to turn "play" into
    "slay". The buffer is overwritten continuously and written nowhere until
    a recording begins.

    stop_when: conditions under which the microphone is closed entirely and
    the buffer dropped (a visit, the settings tab, typing in the chat, a
    local add-on holding the mic), so nothing is captured at all.
    mute_when: conditions under which audio is captured but kept out of the
    buffer (the assistant speaking, the head recentering), plus mute_tail_s
    afterwards, so a pre-roll never carries the assistant's own voice or
    motor noise."""

    def __init__(self, device: str, rate: int, channels: int, preroll_s: float = 0.6,
                 stop_when=(), mute_when=(), mute_tail_s: float = 0.3, chunk_s: float = 0.02):
        self.cmd = ["arecord", "-q", "-D", device, "-t", "raw", "-r", str(rate), "-f", "S16_LE",
                    "-c", str(channels)]
        self.chunk_bytes = max(1, int(rate * chunk_s)) * channels * 2
        self.ring: "collections.deque[bytes]" = collections.deque(maxlen=max(1, round(preroll_s / chunk_s)))
        self.stop_when, self.mute_when, self.mute_tail_s = stop_when, mute_when, mute_tail_s
        self._lock = threading.Lock()
        self._sink = None
        self._proc = None
        self._muted_until = 0.0
        self._last_error_log = 0.0

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="mic-stream").start()

    def begin(self, raw_path: str) -> None:
        """Starts writing raw audio to raw_path, beginning with the pre-roll."""
        with self._lock:
            self._sink = open(raw_path, "wb")
            self._sink.write(b"".join(self.ring))

    def end(self) -> None:
        with self._lock:
            if self._sink is not None:
                self._sink.close()
                self._sink = None

    def _any(self, conditions) -> bool:
        return any(c is not None and c.is_set() for c in conditions)

    def _close(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        self.ring.clear()

    def _log_error(self, message: str, *args) -> None:
        if time.monotonic() - self._last_error_log > 60:
            self._last_error_log = time.monotonic()
            logger.warning(message, *args)

    def _run(self) -> None:
        while True:
            try:
                if self._any(self.stop_when):
                    self._close()
                    time.sleep(0.05)
                    continue
                if self._proc is None:
                    self._proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                data = self._proc.stdout.read(self.chunk_bytes)
                if len(data) < self.chunk_bytes:
                    stderr = self._proc.stderr.read().decode(errors="replace").strip() if self._proc.stderr else ""
                    self._log_error("[Mic] Recorder stopped (%s) - reopening.", stderr or "no data")
                    self._close()
                    time.sleep(1.0)
                    continue
                now = time.monotonic()
                with self._lock:
                    if self._sink is not None:
                        self._sink.write(data)
                    if self._any(self.mute_when):
                        self._muted_until = now + self.mute_tail_s
                        self.ring.clear()
                    elif now >= self._muted_until:
                        self.ring.append(data)
            except Exception as e:
                self._log_error("[Mic] Stream failed: %s - retrying.", e)
                self._close()
                time.sleep(1.0)


class StreamRecording:
    """Stands in for a recorder process where the recording comes from a
    MicStream: terminate() ends it."""

    stderr = None

    def __init__(self, stream: MicStream):
        self.stream = stream

    def terminate(self) -> None:
        self.stream.end()

    def wait(self, timeout=None) -> int:
        return 0
