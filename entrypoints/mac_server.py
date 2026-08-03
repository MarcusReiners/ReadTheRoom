import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config
import logging_setup
from adapters.stt.local import LocalSTTAdapter
from piper.voice import PiperVoice

logging_setup.configure_logging(config)

app = FastAPI(title="ReadTheRoom Mac Pipeline Server")

_stt = LocalSTTAdapter(
    model_path=config.WHISPER_MODEL,
    cpu_threads=config.WHISPER_THREADS,
    vad=config.WHISPER_VAD,
)
_voice = PiperVoice.load(config.PIPER_MODEL)


class SpeakRequest(BaseModel):
    text: str


@app.post("/transcribe")
async def transcribe(audio: UploadFile):
    temp_path = "mac_server_in.wav"
    with open(temp_path, "wb") as f:
        f.write(await audio.read())
    try:
        text = _stt.transcribe(temp_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return {"text": text}


@app.post("/speak")
def speak(req: SpeakRequest):
    def _pcm_stream():
        for chunk in _voice.synthesize(req.text):
            yield chunk.audio_int16_bytes

    return StreamingResponse(_pcm_stream(), media_type="application/octet-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
