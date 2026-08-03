# ReadTheRoom

A desk voice assistant that "reads the room": a radar sensor detects how many people are present, and when the conversation is confidential, output automatically switches from speech to a display. Speech recognition, the LLM, and speech synthesis all run through pluggable APIs — cloud by default, with a local Mac fallback for when there's no internet.

## How it works

1. Recording is started and stopped with ENTER, using a microphone array (arecord + sox, channel downmix to mono; `sox -d` on macOS)
2. Transcription via a swappable STT provider (default: ElevenLabs Scribe; local `faster-whisper` fallback)
3. Response from a swappable LLM provider via [LiteLLM](https://github.com/BerriAI/litellm) (default: OpenAI; any LiteLLM-supported provider works by changing one config value, incl. local Ollama)
4. Speech synthesis via a swappable TTS provider (default: ElevenLabs; local Piper fallback), streamed sentence by sentence while the LLM is still generating
5. Two daisy-chained LED matrix panels show the current state and eyes: panel 1 (status animations, can take over the answer as scrolling text), panel 2 (eyes that blink and track the last known speaker direction)
6. An MG996R servo pans the head towards that same direction, for gesturing or turning to face incoming speech

While the assistant is speaking, the answer can be redirected from speech to the display with `t` + ENTER — for example when a second person enters the room.

## Architecture

The project follows a ports-and-adapters structure with a central event bus:

```
entrypoints/     Entry points: main.py (Pi/Mac loop), mac_server.py (local fallback server)
domain/          Events, conversation state, policies (no hardware code)
service_layer/   EventBus and event handlers (wires domain and adapters together)
adapters/
  stt/           Speech-to-text adapters (elevenlabs.py, local.py, remote.py)
  tts/           Text-to-speech adapters (base.py, elevenlabs.py, local.py, remote.py)
  hardware/      Physical I/O: LED matrix, eyes, servo turntable, radar, buttons
  llm.py         LLM access via LiteLLM
  factory.py     Builds the configured STT/TTS adapter from config.py
config.py        All settings, read from environment variables
```

Adapters communicate through events (`PersonCountChanged`, `SpeechPlaybackStarted`, `ListeningStateChanged`, `DisplayTakeoverRequested`, …) instead of knowing each other directly. Every hardware component has a dummy adapter, so the system also runs without any hardware attached.

STT and TTS each have three interchangeable adapters, selected via `STT_PROVIDER`/`TTS_PROVIDER` in config (`elevenlabs | local | remote`) — swapping in a better model later means adding a new file next to the existing ones and flipping the config value, not changing any calling code:

| Stage | Provider value | Adapter | Where it runs |
|---|---|---|---|
| STT | `elevenlabs` (default) | `adapters/stt/elevenlabs.py` | ElevenLabs Scribe API |
| STT | `remote` | `adapters/stt/remote.py` | Mac pipeline server (LAN fallback) |
| STT | `local` | `adapters/stt/local.py` | faster-whisper, in-process |
| TTS | `elevenlabs` (default) | `adapters/tts/elevenlabs.py` | ElevenLabs API |
| TTS | `remote` | `adapters/tts/remote.py` | Mac pipeline server (LAN fallback) |
| TTS | `local` | `adapters/tts/local.py` | Piper, in-process |

All TTS adapters share sentence-buffering, `aplay` piping, and takeover handling via `adapters/tts/base.py::StreamingTTSAdapter` — a new TTS provider only has to implement `_synthesize_chunks()` and `sample_rate`.

The LLM stays on [LiteLLM](https://github.com/BerriAI/litellm): change `LLM_MODEL` (e.g. `openai/gpt-4o-mini`, `anthropic/claude-sonnet-4-5`, `ollama_chat/qwen3.5:9b`) to swap providers. `LLM_FALLBACK_MODEL`/`LLM_FALLBACK_API_BASE` let it fail over automatically to a local Ollama server if the cloud API is unreachable.

| Adapter | Purpose | Status |
|---|---|---|
| `adapters/stt/*` | Speech recognition | active |
| `adapters/tts/*` | Speech synthesis with streaming and display takeover | active |
| `adapters/llm.py` | LLM access via LiteLLM, with local-Ollama fallback | active |
| `adapters/hardware/led_matrix.py` / `status_display.py` | Daisy-chained RGB LED matrix, panel 1: status animations and scrolling text | active (switchable) |
| `adapters/hardware/led_eyes.py` | Panel 2 of the same matrix: blinking eyes that track the last known direction | active (switchable) |
| `adapters/hardware/face_display.py` | No-hardware fallback for the eyes (`DummyFaceDisplayAdapter`) | dummy |
| `adapters/hardware/turntable.py` | MG996R servo (GPIO19, hardware PWM) panning the head towards the person/audio | active (switchable) |
| `adapters/hardware/radar_ld2450.py` | Person detection (LD2450 radar) | dummy |
| `adapters/hardware/buttons.py` | Button input (currently: `t` for display takeover) | dummy |

## Local fallback: running the pipeline on your Mac

If the Pi has no internet access but is on the same LAN as your Mac, run the STT/TTS models on the Mac and have the Pi call them over HTTP instead of the cloud APIs. The Pi still does all mic/speaker/hardware I/O — only the model calls move.

On the Mac:

```bash
pip install fastapi uvicorn python-multipart
python entrypoints/mac_server.py
```

This starts a small FastAPI server (default port 8000) that exposes `/transcribe` (wraps `faster-whisper`) and `/speak` (wraps Piper), reusing the existing local adapters unchanged.

On the Pi, point the config at it:

```bash
export STT_PROVIDER=remote
export TTS_PROVIDER=remote
export MAC_SERVER_URL=http://<mac-ip>:8000
python entrypoints/main.py
```

The LLM already supports the same pattern today: point `LLM_MODEL` at `ollama_chat/<model>` and `LLM_API_BASE` at an Ollama server running on the Mac's LAN address.

You can also run the whole pipeline directly on the Mac (e.g. for development without a Pi at all) — `entrypoints/main.py` detects macOS and uses `sox -d`/`play` for audio I/O instead of ALSA's `arecord`/`aplay`.

## Target hardware

- Raspberry Pi (Linux, ALSA) or macOS (for local/dev runs)
- ReSpeaker USB Mic Array v2.0 (`ArrayUAC10`, 6 channels) as microphone and speaker (Pi) — also provides onboard direction-of-arrival (DOA) over USB HID, independent of the audio stream
- 2x Waveshare RGB LED matrix 96x48, daisy-chained (rpi-rgb-led-matrix / `rgbmatrix`) — panel 1 status, panel 2 eyes
- MG996R servo on GPIO19 (hardware PWM) for head panning — GPIO18 is taken by the LED matrix's OE- signal
- HLK-LD2450 radar sensor (planned)

## Requirements

- Python 3.10+
- System tools: `arecord`, `aplay` (ALSA, Pi) or `sox` (macOS)
- Python packages: `litellm`, `requests`, `elevenlabs` (for the default cloud providers); optionally `faster-whisper`, `piper-tts` (for local fallback), `fastapi`, `uvicorn`, `python-multipart` (for the Mac server), `python-dotenv`, `rgbmatrix`, `gpiozero` (servo; `pigpio` is an optional smoother PWM backend for it), `pyusb` (ReSpeaker DOA)
- A reachable LLM API (OpenAI/Anthropic/etc., or a local/LAN Ollama server)

## Configuration

All settings live in [config.py](config.py) and are read from environment variables (a `.env` file is loaded automatically if `python-dotenv` is installed).

| Variable | Meaning |
|---|---|
| `STT_PROVIDER`, `TTS_PROVIDER` | `elevenlabs` \| `local` \| `remote` |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_TTS_MODEL`, `ELEVENLABS_STT_MODEL`, `ELEVENLABS_LANGUAGE_CODE` | ElevenLabs settings |
| `OPENAI_API_KEY` | Read by LiteLLM when `LLM_MODEL` starts with `openai/` |
| `LLM_MODEL`, `LLM_API_BASE` | Primary LLM (LiteLLM model string + optional server address) |
| `LLM_FALLBACK_MODEL`, `LLM_FALLBACK_API_BASE` | Local/LAN Ollama fallback if the primary LLM is unreachable |
| `MAC_SERVER_URL`, `MAC_SERVER_TTS_SAMPLE_RATE` | Address of the Mac pipeline server, and its Piper voice's sample rate |
| `WHISPER_MODEL`, `WHISPER_THREADS`, `WHISPER_VAD` | Local STT fallback: path to the CT2 model, CPU threads, VAD filter |
| `PIPER_MODEL` | Local TTS fallback: path to the Piper voice (.onnx) |
| `MIC_DEVICE`, `MIC_CHANNELS`, `MIC_RATE` | ALSA capture device (Pi only) |
| `SPEAKER_DEVICE` | ALSA playback device (Pi only) |
| `USE_LED_MATRIX` | Enable/disable the LED matrix (status panel + eyes panel) |
| `USE_SERVO`, `SERVO_GPIO_PIN`, `SERVO_MIN_ANGLE`, `SERVO_MAX_ANGLE` | Enable/disable the servo turntable, its GPIO pin, and its clamped safe sweep range |
| `MAX_RECORD_SECONDS` | Watchdog against endless recordings |
| `LOG_DIR`, `LOG_FILE`, `LOG_LEVEL`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT` | Logging (see below) |

## Logging

Every domain event (`PersonCountChanged`, `SpeechPlaybackStarted`, …) and all adapter activity (STT/LLM/TTS timings, transcribed text, errors, model load status) go through the stdlib `logging` module, set up once in [logging_setup.py](logging_setup.py). Output goes to both the console and a local rotating log file — `logs/readtheroom.log` by default, capped at `LOG_MAX_BYTES` (5 MB) × `LOG_BACKUP_COUNT` (5) so it can't fill the SD card. Logs stay on the device by design — nothing is uploaded, since conversations can be confidential.

## Running

```bash
python entrypoints/main.py
```

Controls:

- **ENTER** — start recording, **ENTER** again — stop recording
- **t + ENTER** — redirect the current answer from the speaker to the display (or end display text mode)
- **Ctrl+C** — quit

## Testing hardware

[scripts/test_hardware.py](scripts/test_hardware.py) exercises the LED matrix and servo without needing any STT/LLM/TTS API keys configured:

```bash
python scripts/test_hardware.py         # canned demo: display states, scrolling text, servo sweep
python scripts/test_hardware.py --doa   # live: servo + eyes follow the ReSpeaker's direction-of-arrival
```

The `--doa` mode needs `pyusb` and, once per Pi, a udev rule so the ReSpeaker's USB HID interface is readable without root:

```bash
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="2886", ATTRS{idProduct}=="0018", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/99-respeaker.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Note the ReSpeaker reports a full 0–359° angle, but the servo is physically clamped to `SERVO_MIN_ANGLE`–`SERVO_MAX_ANGLE` — directions behind/beside the assistant just pin it at its nearest limit, which is expected. 90° is "straight ahead" for both the servo and the eyes.

Servo motion is deliberately not 1:1 with raw DOA readings — `--doa` only reacts while the mic's onboard VAD (`RespeakerDOAAdapter.get_voice_active()`) says something voice-like is happening, and `ServoTurntableAdapter.rotate_towards()` layers three stabilizers on top (all in `domain/policies.py`) before it ever moves: `DOA_SMOOTHING_ALPHA` low-pass-filters the target, `ROTATION_THRESHOLD_DEGREES`/`MIN_CONSECUTIVE_LARGE_CHANGES` require a large deviation to persist across several readings, and `MOVE_COOLDOWN_SECONDS` enforces a minimum rest period between physical moves regardless of how noisy the input is. Tune those four constants if it feels too sluggish or still too twitchy.

If the LED matrix flickers, `LedMatrix`'s `pwm_bits` (default 6, down from the library's default 11) trades unneeded color depth for a higher refresh rate — lower it further if it still flickers, or raise it if you want smoother color gradients and can spare the refresh rate. The library's own startup hint about adding `isolcpus=3` to `/boot/cmdline.txt` (dedicating a CPU core to the matrix refresh thread, reboot required) is a further, bigger lever if `pwm_bits` alone isn't enough — worth it now that STT/LLM/TTS run in the cloud rather than competing for CPU on the Pi.

If the chained (2nd) panel shows ghosting or stray static pixels while otherwise rendering correctly, that's a signal-timing symptom, not a dead panel/cable — try raising `GPIO_SLOWDOWN` (default 4) a step or two at a time; it trades max refresh rate for cleaner signal integrity across the chain.
