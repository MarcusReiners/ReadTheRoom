"""
Whisper.cpp STT Adapter (ersetzt Wyoming-Whisper Docker).

Ruft whisper-cli direkt als Subprocess auf statt ueber das Wyoming-Protokoll.
Vorteile: kein Docker, native ARM-NEON, keine haengenden TCP-Verbindungen.

VORAUSSETZUNGEN (einmalig):
    cd ~/whisper.cpp
    cmake -B build && cmake --build build -j4
    ./models/download-ggml-model.sh base
"""

import os
import subprocess


class WhisperCppAdapter:
    def __init__(
        self,
        whisper_cli: str = "/home/marcus/whisper.cpp/build/bin/whisper-cli",
        model: str = "/home/marcus/whisper.cpp/models/ggml-tiny-q5_1.bin",
        language: str = "de",
        threads: int = 4,
    ) -> None:
        self.whisper_cli = whisper_cli
        self.model = model
        self.language = language
        self.threads = threads

        if not os.path.exists(self.whisper_cli):
            raise FileNotFoundError(
                f"whisper-cli nicht gefunden: {self.whisper_cli}\n"
                "Bitte erst bauen: cd ~/whisper.cpp && cmake -B build && cmake --build build -j4"
            )
        if not os.path.exists(self.model):
            raise FileNotFoundError(
                f"Whisper-Modell nicht gefunden: {self.model}\n"
                "Bitte laden: cd ~/whisper.cpp && ./models/download-ggml-model.sh base"
            )

    def transcribe(self, audio_file: str) -> str:
        print("✨ Verarbeite Sprache mit whisper.cpp...")
        if not os.path.exists(audio_file):
            print(f"  Fehler: Audiodatei '{audio_file}' nicht gefunden.")
            return ""
        try:
            result = subprocess.run(
                [
                    self.whisper_cli,
                    "-m", self.model,
                    "-f", audio_file,
                    "-l", self.language,
                    "-t", str(self.threads),
                    "--no-timestamps",
                    "--no-prints",
                    "--suppress-nst",
                    "--beam-size", "1",
                    "--best-of", "1",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            text = result.stdout.strip()
            for noise in ["[BLANK_AUDIO]", "(Stille)", "(Musik)", "(music)", "(silence)"]:
                text = text.replace(noise, "")
            text = text.strip()
            if result.returncode != 0 and not text:
                print(f"  whisper-cli Fehler: {result.stderr[:200]}")
                return ""
            return text
        except subprocess.TimeoutExpired:
            print("  Fehler: whisper-cli Timeout nach 60s.")
            return ""
        except Exception as e:
            print(f"  STT Fehler: {type(e).__name__}: {e}")
            return ""
