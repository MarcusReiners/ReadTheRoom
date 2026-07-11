# ReadTheRoom

A local desk voice assistant that "reads the room": a radar sensor detects how many people are present, and when the conversation is confidential, output automatically switches from speech to a display. The whole pipeline (speech recognition, LLM, speech synthesis) runs locally or on your own network — no cloud.

## How it works

1. Recording is started and stopped with ENTER, using a microphone array (arecord + sox, channel downmix to mono)
2. Transcription with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (German tiny model, CTranslate2, CPU)
3. Response from a local LLM via [LiteLLM](https://github.com/BerriAI/litellm) (Ollama, streamed)
4. Speech synthesis with [Piper](https://github.com/rhasspy/piper) (voice "Thorsten"), streamed sentence by sentence while the LLM is still generating
5. An LED matrix shows the current state (listening, speaking) and can take over the answer as scrolling text

While the assistant is speaking, the answer can be redirected from speech to the display with `t` + ENTER — for example when a second person enters the room.

## Architecture

The project follows a ports-and-adapters structure with a central event bus:

```
entrypoints/     Entry point and main loop (main.py)
domain/          Events, conversation state, policies (no hardware code)
service_layer/   EventBus and event handlers (wires domain and adapters together)
adapters/        Hardware and model integrations
```

Adapters communicate through events (`PersonCountChanged`, `SpeechPlaybackStarted`, `ListeningStateChanged`, `DisplayTakeoverRequested`, …) instead of knowing each other directly. Every hardware component has a dummy adapter, so the system also runs without any hardware attached.

| Adapter | Purpose | Status |
|---|---|---|
| `stt_whisper` | Speech recognition (faster-whisper, German) | active |
| `tts_piper` | Speech synthesis with streaming and display takeover | active |
| `llm_gateway` | LLM access via LiteLLM/Ollama | active |
| `led_matrix` / `status_display` | RGB LED matrix with status animations and scrolling text | active (switchable) |
| `face_display` | Animated eyes via HDMI | optional |
| `radar_ld2450` | Person detection (LD2450 radar) | dummy |
| `turntable` | Turntable that rotates the assistant towards the person | dummy |
| `buttons` | Button input (currently: `t` for display takeover) | dummy |

## Target hardware

- Raspberry Pi (Linux, ALSA)
- ReSpeaker mic array (`ArrayUAC10`, 6 channels) as microphone and speaker
- RGB LED matrix 96x48 (rpi-rgb-led-matrix / `rgbmatrix`)
- HLK-LD2450 radar sensor (planned)
- Ollama server on the local network (e.g. `qwen3.5:9b`)

## Requirements

- Python 3.10+
- System tools: `arecord`, `aplay` (ALSA), `sox`
- Python packages: `faster-whisper`, `piper-tts`, `litellm`, optionally `rgbmatrix` and `pygame`
- A Whisper CT2 model and a Piper voice on local disk
- A reachable Ollama server

## Configuration

All settings live as constants at the top of [entrypoints/main.py](entrypoints/main.py):

| Constant | Meaning |
|---|---|
| `LLM_MODEL`, `LLM_API_BASE` | Ollama model and server address |
| `WHISPER_MODEL`, `WHISPER_THREADS`, `WHISPER_VAD` | Path to the CT2 model, CPU threads, VAD filter |
| `PIPER_MODEL` | Path to the Piper voice (.onnx) |
| `MIC_DEVICE`, `MIC_CHANNELS`, `MIC_RATE` | ALSA capture device |
| `SPEAKER_DEVICE` | ALSA playback device |
| `USE_LED_MATRIX`, `USE_HDMI_EYES` | Enable/disable hardware features |
| `MAX_RECORD_SECONDS` | Watchdog against endless recordings |

## Running

```bash
python entrypoints/main.py
```

Controls:

- **ENTER** — start recording, **ENTER** again — stop recording
- **t + ENTER** — redirect the current answer from the speaker to the display (or end display text mode)
- **Ctrl+C** — quit
