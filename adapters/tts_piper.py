"""
Piper TTS Adapter.

Erweitert die urspruengliche Piper-Logik um:
- SpeechPlaybackStarted / SpeechPlaybackEnded Events (ADR-002, 9.2),
  damit der Face-Display-Adapter die Mundanimation steuern kann.
- pause()/resume() via SIGSTOP/SIGCONT statt SIGKILL (ADR-002, 11),
  damit eine durch den Not-Stopp-Taster unterbrochene Wiedergabe
  spaeter fortgesetzt werden kann.
"""

import os
import signal
import socket
import subprocess

from wyoming.event import Event as WyomingEvent, write_event, read_event
from wyoming.audio import AudioChunk, AudioStart, AudioStop

from domain.events import SpeechPlaybackStarted, SpeechPlaybackEnded
from service_layer.bus import EventBus


class PiperTTSAdapter:
    def __init__(self, bus: EventBus, host: str = "127.0.0.1", port: int = 10200,
                 speaker_device: str = "hw:0,0") -> None:
        self.bus = bus
        self.host = host
        self.port = port
        self.speaker_device = speaker_device
        self._aplay_process: subprocess.Popen | None = None

    def speak(self, text: str) -> None:
        print(f"🤖 Antworte: {text}")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.connect((self.host, self.port))
                sock.settimeout(30)
                fp_write = sock.makefile("wb")
                fp_read = sock.makefile("rb")

                text_event = WyomingEvent(type="synthesize", data={"text": text})
                write_event(text_event, fp_write)
                fp_write.flush()

                all_audio = bytearray()
                audio_rate, audio_width, audio_channels = 22050, 2, 1

                while True:
                    event = read_event(fp_read)
                    if event is None:
                        break
                    if AudioStart.is_type(event.type):
                        start = AudioStart.from_event(event)
                        audio_rate, audio_width, audio_channels = start.rate, start.width, start.channels
                    elif AudioChunk.is_type(event.type):
                        chunk = AudioChunk.from_event(event)
                        all_audio.extend(chunk.audio)
                    elif AudioStop.is_type(event.type):
                        break

                if not all_audio:
                    print("  Warnung: Piper hat keine Audiodaten zurueckgegeben.")
                    return

                temp_raw = "temp_out.raw"
                with open(temp_raw, "wb") as f:
                    f.write(bytes(all_audio))

                self.bus.publish(SpeechPlaybackStarted(text=text))
                self._aplay_process = subprocess.Popen(
                    ["aplay", "-D", self.speaker_device,
                     "-r", str(audio_rate), "-f", f"S{audio_width * 8}_LE",
                     "-c", str(audio_channels), temp_raw],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self._aplay_process.wait()
                completed = self._aplay_process.returncode == 0
                self._aplay_process = None
                self.bus.publish(SpeechPlaybackEnded(completed=completed))

                if os.path.exists(temp_raw):
                    os.remove(temp_raw)

        except ConnectionRefusedError:
            print(f"  Fehler: Verbindung zu Piper ({self.host}:{self.port}) verweigert.")
        except Exception as e:
            print(f"  TTS Fehler: {type(e).__name__}: {e}")

    def pause(self) -> None:
        """SIGSTOP statt SIGKILL - Wiedergabe friert ein, Prozess bleibt erhalten (ADR-002, 11)."""
        if self._aplay_process is not None:
            self._aplay_process.send_signal(signal.SIGSTOP)
            print("  ⏸  Wiedergabe pausiert (SIGSTOP).")

    def resume(self) -> None:
        if self._aplay_process is not None:
            self._aplay_process.send_signal(signal.SIGCONT)
            print("  ▶  Wiedergabe fortgesetzt (SIGCONT).")

    def discard(self) -> None:
        """Beendet die pausierte Wiedergabe endgueltig (Nutzerentscheidung: verwerfen)."""
        if self._aplay_process is not None:
            self._aplay_process.send_signal(signal.SIGCONT)
            self._aplay_process.terminate()
            self._aplay_process = None
            print("  🗑  Antwort verworfen.")
