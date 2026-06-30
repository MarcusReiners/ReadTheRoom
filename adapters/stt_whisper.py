"""
Whisper STT Adapter (Wyoming-Protokoll).

Uebernimmt die bereits funktionierende, getestete Logik aus dem
urspruenglichen assistant.py 1:1 - nur als Adapter-Klasse gekapselt,
damit sie ueber den Event-Bus angesprochen werden kann.
"""

import socket
import wave
import os

from wyoming.event import Event as WyomingEvent, write_event, read_event
from wyoming.audio import AudioChunk, AudioStart, AudioStop


class WhisperSTTAdapter:
    def __init__(self, host: str = "127.0.0.1", port: int = 10300,
                 samples_per_chunk: int = 1024) -> None:
        self.host = host
        self.port = port
        self.samples_per_chunk = samples_per_chunk

    def transcribe(self, audio_file: str) -> str:
        print("✨ Verarbeite Sprache mit Whisper...")
        try:
            if not os.path.exists(audio_file):
                print(f"  Fehler: Audiodatei '{audio_file}' nicht gefunden.")
                return ""

            with wave.open(audio_file, "rb") as wav_file:
                rate = wav_file.getframerate()
                width = wav_file.getsampwidth()
                channels = wav_file.getnchannels()
                pcm_data = wav_file.readframes(wav_file.getnframes())

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.connect((self.host, self.port))
                sock.settimeout(30)
                fp_write = sock.makefile("wb")
                fp_read = sock.makefile("rb")

                start = AudioStart(rate=rate, width=width, channels=channels)
                write_event(start.event(), fp_write)

                chunk_size_bytes = self.samples_per_chunk * width * channels
                offset = 0
                while offset < len(pcm_data):
                    chunk_bytes = pcm_data[offset: offset + chunk_size_bytes]
                    chunk = AudioChunk(rate=rate, width=width, channels=channels, audio=chunk_bytes)
                    write_event(chunk.event(), fp_write)
                    offset += chunk_size_bytes

                stop = AudioStop()
                write_event(stop.event(), fp_write)
                fp_write.flush()
                # Kein sock.shutdown() - Whisper braucht die Verbindung
                # noch fuer die Antwort (siehe urspruengliches Debugging).

                while True:
                    event = read_event(fp_read)
                    if event is None:
                        break
                    if event.type == "transcript":
                        return event.data.get("text", "")
                    if event.type == "error":
                        print(f"  Whisper-Fehler: {event.data}")
                        return ""

        except ConnectionRefusedError:
            print(f"  Fehler: Verbindung zu Whisper ({self.host}:{self.port}) "
                  f"verweigert. Laeuft der Docker-Container?")
        except Exception as e:
            print(f"  STT Fehler: {type(e).__name__}: {e}")
        return ""
