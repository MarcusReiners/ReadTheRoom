import subprocess
import time

import requests


class WhisperCppAdapter:
    def __init__(
        self,
        server_binary: str = "/home/marcus/whisper.cpp/build/bin/whisper-server",
        model_path: str = "/home/marcus/whisper.cpp/models/ggml-base.bin",
        host: str = "127.0.0.1",
        port: int = 8081,
        language: str = "de",
        threads: int = 3,
        startup_timeout_s: float = 30.0,
    ) -> None:
        self.language = language
        self.url = f"http://{host}:{port}/inference"

        self._proc = subprocess.Popen(
            [server_binary, "--model", model_path, "--host", host, "--port", str(port),
             "--language", language, "--threads", str(threads)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._wait_until_ready(host, port, startup_timeout_s)
        print("  Whisper bereit.")

    def _wait_until_ready(self, host: str, port: int, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        base_url = f"http://{host}:{port}/"
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError("whisper-server ist beim Start abgestürzt.")
            try:
                requests.get(base_url, timeout=1)
                return
            except requests.exceptions.ConnectionError:
                time.sleep(0.5)
        raise RuntimeError("whisper-server ist nicht rechtzeitig gestartet.")

    def transcribe(self, audio_file: str) -> str:
        try:
            with open(audio_file, "rb") as f:
                res = requests.post(
                    self.url,
                    files={"file": (audio_file, f, "audio/wav")},
                    data={"language": self.language, "response_format": "json"},
                    timeout=30,
                )
            res.raise_for_status()
            text = res.json().get("text", "").strip()

            for noise in ["[BLANK_AUDIO]", "(Stille)", "(Musik)", "(music)", "(silence)"]:
                text = text.replace(noise, "")

            return text.strip()

        except Exception as e:
            print(f"  STT Fehler: {type(e).__name__}: {e}")
            return ""

    def stop(self) -> None:
        self._proc.terminate()
        self._proc.wait(timeout=5)
