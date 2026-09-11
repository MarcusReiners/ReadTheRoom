# ReadTheRoom

A desk voice assistant that "reads the room": a radar sensor detects how many people are present, and when the conversation is confidential, output automatically switches from speech to a display. Speech recognition, the LLM, and speech synthesis all run through pluggable APIs — cloud by default, with a local Mac fallback for when there's no internet.

## How it works

1. Recording starts automatically when the mic array's onboard VAD hears speech, and stops after a short trailing silence (`vad_input_loop`). Pressing ENTER still works as a manual override. Audio via arecord + sox with a channel downmix to mono; `sox -d` on macOS
2. Transcription via a swappable STT provider (default: ElevenLabs Scribe; local `faster-whisper` fallback)
3. Response from a swappable LLM provider via [LiteLLM](https://github.com/BerriAI/litellm) (default: OpenAI; any LiteLLM-supported provider works by changing one config value, incl. local Ollama)
4. Speech synthesis via a swappable TTS provider (default: ElevenLabs; local Piper fallback), streamed sentence by sentence while the LLM is still generating
5. A single LED matrix panel shows eyes that blink and track the last known speaker direction. Status (idle/listening/speaking) is shown separately on a 7-LED "tie" strip driven by the bridge ESP32
6. A DS3225 digital servo pans the head towards that same direction, for gesturing or turning to face incoming speech
7. A [Seeed XIAO ESP32S3](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/) running the LD2450 firmware (`firmware/mmWave/`) reads the sensor over UART and sends target data over **ESP-NOW** (no WiFi AP/router involved — sidesteps the association/isolation issues plain WiFi ran into). A second XIAO ESP32S3 running the bridge firmware (`firmware/mmWaveBridge/`) receives those packets and relays them to the Pi as newline-delimited JSON over USB serial. The Pi reads that (`adapters/hardware/radar_ld2450.py::RadarLD2450Adapter`) and publishes `PersonCountChanged`/`RadarTargetsUpdated`.
8. A small web app (`adapters/chat_bridge.py`, served at `http://<pi-address>:8765`) mirrors the conversation as text, accepts typed messages, and has a settings tab for the radar zone and enforcement mode, servo calibration and turning range, private mode, the voice library and volume, the system prompt, the LLM model and its thinking level, and the speech-recognition language. When someone walks into the radar zone during a conversation, the current/next answer is automatically redirected from speech to that chat — reverting to speech once they have walked out again. The switch is decided by who *crosses the zone edge*, not by a headcount: the LD2450 loses still, seated people, so the user's own presence is taken from the fact that they are talking, and only someone tracked moving in from outside counts as a newcomer (`service_layer/handlers.py`, `PrivacyGuard`). This needs the zone's *Enforced by* set to **Software** — sensor-side enforcement hides everyone outside the zone, so no entry can be seen. A voice on/off toggle overrides this manually regardless of who's in the room.

   Optionally, a second rectangle — the **door zone** — is drawn over the doorway in the same settings view. With it, entries and exits are decided only there: someone who comes through the door zone into the room is an entry, someone who goes from the room into the door zone and disappears has left, and movement anywhere else (someone getting up, a reflection near a wall) can no longer trigger anything. Someone who only stands in the doorway during a conversation makes the voice quieter within about 0.3 s instead of cutting the answer off; it returns to normal shortly after the doorway is clear. The door zone is stored on the Pi only (`app_settings.json`); the sensor keeps just the room zone.

## Architecture

The project follows a ports-and-adapters structure with a central event bus:

```
entrypoints/     Entry points: main.py (Pi/Mac loop), mac_server.py (local fallback server)
domain/          Events and conversation state (no hardware code)
service_layer/   EventBus and event handlers (wires domain and adapters together)
adapters/
  stt/           Speech-to-text adapters (elevenlabs.py, local.py, remote.py)
  tts/           Text-to-speech adapters (base.py, elevenlabs.py, local.py, remote.py)
  hardware/      Physical I/O: LED matrix eyes, servo turntable, radar
  llm.py         LLM access via LiteLLM
  chat_bridge.py Web app (FastAPI/WebSocket): mirrors the conversation, accepts typed turns, serves the settings tab
  conversation_store.py  SQLite store for chat history
  app_settings.py        Settings editable from the web app, persisted to app_settings.json
  factory.py     Builds the configured STT/TTS/radar/turntable adapter from config.py
web/static/      Chat app frontend (single static index.html, no build step)
config.py        All settings, read from environment variables
```

Adapters communicate through events (`PersonCountChanged`, `SpeechPlaybackStarted`, `ListeningStateChanged`, …) instead of knowing each other directly. Every hardware component has a dummy adapter, so the system also runs without any hardware attached.

STT and TTS each have three interchangeable adapters, selected via `STT_PROVIDER`/`TTS_PROVIDER` in config (`elevenlabs | local | remote`) — swapping in a better model later means adding a new file next to the existing ones and flipping the config value, not changing any calling code:

| Stage | Provider value | Adapter | Where it runs |
|---|---|---|---|
| STT | `elevenlabs` (default) | `adapters/stt/elevenlabs.py` | ElevenLabs Scribe API |
| STT | `remote` | `adapters/stt/remote.py` | Mac pipeline server (LAN fallback) |
| STT | `local` | `adapters/stt/local.py` | faster-whisper, in-process |
| TTS | `elevenlabs` (default) | `adapters/tts/elevenlabs.py` | ElevenLabs API |
| TTS | `remote` | `adapters/tts/remote.py` | Mac pipeline server (LAN fallback) |
| TTS | `local` | `adapters/tts/local.py` | Piper, in-process |

All TTS adapters share sentence-buffering, `aplay` piping, and takeover handling (muting mid-stream when someone enters the room) via `adapters/tts/base.py::StreamingTTSAdapter` — a new TTS provider only has to implement `_synthesize_chunks()` and `sample_rate`.

The LLM stays on [LiteLLM](https://github.com/BerriAI/litellm): change `LLM_MODEL` (e.g. `openai/gpt-4o-mini`, `anthropic/claude-sonnet-4-5`, `ollama_chat/qwen3.5:9b`) to swap providers. `LLM_FALLBACK_MODEL`/`LLM_FALLBACK_API_BASE` let it fail over automatically to a local Ollama server if the cloud API is unreachable.

| Adapter | Purpose | Status |
|---|---|---|
| `adapters/stt/*` | Speech recognition | active |
| `adapters/tts/*` | Speech synthesis with streaming, muted on takeover | active |
| `adapters/llm.py` | LLM access via LiteLLM, with local-Ollama fallback | active |
| `adapters/hardware/led_matrix.py` | Single RGB LED matrix, eyes only | active (switchable) |
| `adapters/hardware/led_eyes.py` | Blinking eyes rendered on the matrix, tracking the last known direction | active (switchable) |
| `adapters/hardware/face_display.py` | No-hardware fallback for the eyes (`DummyFaceDisplayAdapter`) | dummy |
| `adapters/hardware/turntable.py` | DS3225 servo (GPIO19, hardware PWM) panning the head towards the person/audio | active (switchable) |
| `adapters/hardware/radar_ld2450.py` | Person detection: reads XIAO ESP32S3/LD2450 data relayed over ESP-NOW + USB serial (see `firmware/mmWave/`, `firmware/mmWaveBridge/`) | active (switchable via `RADAR_PROVIDER`) |
| `adapters/chat_bridge.py` | Web chat app: mirrors the conversation, accepts typed turns, and is the destination when a spoken answer switches modality on someone entering | active |

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
- 1x Waveshare RGB LED matrix 96x48 (rpi-rgb-led-matrix / `rgbmatrix`) — eyes only; status is shown on a separate tie LED strip driven by the bridge ESP32
- DS3225 digital servo on GPIO19 (hardware PWM) for head panning — GPIO18 is taken by the LED matrix's OE- signal. Note the DS3225 ships in 180° and 270° variants using the same 500–2500µs pulse range, so `SERVO_HARDWARE_MIN_ANGLE`/`SERVO_HARDWARE_MAX_ANGLE` must match the sweep *your* unit actually performs (see below)
- HLK-LD2450 radar sensor on a Seeed XIAO ESP32S3 (`firmware/mmWave/`), relayed to the Pi via ESP-NOW + a second XIAO ESP32S3 acting as a USB bridge (`firmware/mmWaveBridge/`). Both are PlatformIO projects: open the folder in VS Code with the PlatformIO extension and upload. The sensor keeps its room zone in flash, so reflashing does not reset it

## Requirements

- Python 3.10+
- System tools: `arecord`, `aplay` (ALSA, Pi) or `sox` (macOS)
- Python packages: `litellm`, `requests`, `elevenlabs` (for the default cloud providers); `fastapi`, `uvicorn` (chat bridge, always required now; `python-multipart` additionally for the Mac server); `pyserial` (radar bridge link); optionally `faster-whisper`, `piper-tts` (for local fallback), `python-dotenv`, `numpy` (TTS volume scaling — without it the volume slider is a no-op), `rgbmatrix`, `gpiozero` (servo; `pigpio` strongly recommended as the PWM backend, see below), `pyusb` (ReSpeaker DOA)
- A reachable LLM API (OpenAI/Anthropic/etc., or a local/LAN Ollama server)

## Configuration

All settings live in [config.py](config.py) and are read from environment variables (a `.env` file is loaded automatically if `python-dotenv` is installed).

| Variable | Meaning |
|---|---|
| `STT_PROVIDER`, `TTS_PROVIDER` | `elevenlabs` \| `local` \| `remote` |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_TTS_MODEL`, `ELEVENLABS_STT_MODEL` | ElevenLabs credentials and model selection |
| `ELEVENLABS_LANGUAGE_CODE` | STT input language, ISO 639-3 (`eng`, `deu`). Empty = let Scribe auto-detect, which is what you want if more than one language gets spoken. A *wrong* fixed code returns mangled transcripts rather than an error |
| `ELEVENLABS_TTS_LANGUAGE_CODE` | TTS output language, ISO 639-1 (`en`, `de`) — keep it consistent with what the system prompt tells the model to reply in, or the voice mispronounces its own output |
| `OPENAI_API_KEY`, `GEMINI_API_KEY` | Read by LiteLLM when `LLM_MODEL` starts with `openai/` / `gemini/` |
| `LLM_MODEL`, `LLM_API_BASE` | Primary LLM (LiteLLM model string + optional server address) |
| `OLLAMA_API_BASE` | Address of the Ollama server used whenever the model is an `ollama_chat/...` one, e.g. `http://<mac-ip>:11434` - lets the web app switch between a cloud model and the local one without a restart |
| `LLM_FALLBACK_MODEL`, `LLM_FALLBACK_API_BASE` | Optional local/LAN Ollama fallback if the primary LLM is unreachable (off unless `LLM_FALLBACK_MODEL` is set, e.g. `ollama_chat/qwen3.5:9b`) |
| `LLM_REASONING_EFFORT` | How much the model may "think" before answering: `minimal`/`none`/`disable`/`low`/`medium`/`high`, or empty for the provider's default. Thinking is pure dead air here — nothing can be spoken until the first real token. Note Gemini 3.x cannot disable it fully; `disable` lands on its lowest level. Also editable from the web app |
| `LLM_MAX_TOKENS` | Ceiling on reply length (default 500). Thinking tokens count against it |
| `MAC_SERVER_URL`, `MAC_SERVER_TTS_SAMPLE_RATE` | Address of the Mac pipeline server, and its Piper voice's sample rate |
| `WHISPER_MODEL`, `WHISPER_THREADS`, `WHISPER_VAD` | Local STT fallback: path to the CT2 model, CPU threads, VAD filter |
| `PIPER_MODEL` | Local TTS fallback: path to the Piper voice (.onnx) |
| `MIC_DEVICE`, `MIC_CHANNELS`, `MIC_RATE` | ALSA capture device (Pi only) |
| `SPEAKER_DEVICE` | ALSA playback device (Pi only) |
| `USE_LED_MATRIX`, `GPIO_SLOWDOWN`, `LED_MATRIX_BRIGHTNESS`, `LED_MATRIX_PWM_BITS` | Enable/disable the LED matrix (eyes) and its signal/refresh tuning |
| `USE_SERVO`, `SERVO_GPIO_PIN` | Enable/disable the servo turntable and its GPIO pin |
| `SERVO_MIN_ANGLE`, `SERVO_MAX_ANGLE` | The safe sweep window every commanded angle is clamped to, so the head can't wind its own cabling. Keep it centered inside the hardware range below. Also editable live from the web app's settings tab |
| `SERVO_HARDWARE_MIN_ANGLE`, `SERVO_HARDWARE_MAX_ANGLE` | The servo's **real** physical sweep. These are labels on a linear map onto the pulse widths below, not limits — if they don't match what the servo actually does, every commanded "degree" silently stops being a degree (see *Testing hardware*) |
| `SERVO_MIN_PULSE_WIDTH`, `SERVO_MAX_PULSE_WIDTH` | Pulse widths (seconds) the two hardware angles map to. `0.0005`–`0.0025` is the DS3225's documented range |
| `SERVO_USE_PIGPIO` | Use pigpio's DMA-timed PWM instead of gpiozero's software-timed default. Strongly recommended — software PWM visibly jitters while holding position |
| `SERVO_CALIBRATION_PATH` | Where the calibrated home offset is persisted (default `servo_calibration.json`) |
| `DOA_FRONT_REFERENCE_DEGREES` | The raw angle the ReSpeaker reports for a speaker standing straight ahead of the mount — calibrate with `scripts/print_doa.py` |
| `RADAR_PROVIDER`, `RADAR_SERIAL_PORT`, `RADAR_SERIAL_BAUD` | `ld2450` (read radar data relayed by the bridge ESP32 over USB serial) \| `dummy`; the bridge's serial device (default `/dev/ttyUSB0`); baud rate (default `115200`) |
| `RADAR_ENTRY_MARGIN_MM`, `RADAR_EXIT_MARGIN_MM` | How far inside / outside the zone edge a track must be to count as in / out (default `100` each). The band between absorbs radar jitter, so someone standing at the edge cannot fire entries and exits back and forth |
| `PRIVACY_DEPARTURE_GRACE_S` | After someone leaves while no visitor is present, how long without further conversation before the user is assumed to have left (default `60`). Until then, anyone entering is still treated as a visitor |
| `RADAR_DOOR_NEAR_MM` | With a door zone: someone first picked up inside the room within this distance of it still counts as having come through the door (default `800`) |
| `TTS_DUCK_GAIN`, `PRIVACY_DOOR_CLEAR_HOLD_S` | How far the voice drops while someone stands in the door zone (default `0.35`, about −9 dB), and how long after the doorway is clear it returns to normal (default `1.5` s) |
| `CHAT_BRIDGE_HOST`, `CHAT_BRIDGE_PORT` | Address the web app binds to (default `0.0.0.0:8765`) — open `http://<pi-address>:8765` in a browser |
| `CONVERSATIONS_DB_PATH` | SQLite file holding chat history (default `conversations.db`) |
| `APP_SETTINGS_PATH` | Settings written by the web app's settings tab — system prompt, LLM model, reasoning effort, STT language, voice library, volume, servo range (default `app_settings.json`). **Values here take precedence over `.env` and the `config.py` defaults**, so a setting saved from the web app wins until it is changed there or removed from the file |
| `MAX_RECORD_SECONDS` | Watchdog against endless recordings |
| `LOG_DIR`, `LOG_FILE`, `LOG_LEVEL`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT` | Logging (see below) |

## Logging

Every domain event (`PersonCountChanged`, `SpeechPlaybackStarted`, …) and all adapter activity (STT/LLM/TTS timings, transcribed text, errors, model load status) go through the stdlib `logging` module, set up once in [logging_setup.py](logging_setup.py). Output goes to both the console and a local rotating log file — `logs/readtheroom.log` by default, capped at `LOG_MAX_BYTES` (5 MB) × `LOG_BACKUP_COUNT` (5) so it can't fill the SD card. Logs stay on the device by design — nothing is uploaded, since conversations can be confidential.

## Running

```bash
python entrypoints/main.py
```

Speaking is hands-free: the mic array's onboard VAD starts and stops recording on its own. The console controls are a manual override:

- **ENTER** — start recording, **ENTER** again — stop recording
- **Ctrl+C** — quit

Typed messages from the web app take priority over speech: sending one interrupts an in-progress spoken reply rather than queueing behind it. Voice input pauses while the chat box is focused, entirely while the settings tab is open, and while the head is recentering (that move is the one servo noise loud enough to trip the mic, and it only happens when nobody is talking).

A recording runs until `trailing_silence_s` (0.8s) of quiet, then is kept only if at least `min_voice_polls` polls across the whole recording reported voice. That count is **cumulative, not consecutive** — pauses inside a sentence cost nothing, while a knock never reaches the total. Raise `min_voice_polls` if noise gets transcribed; raise `trailing_silence_s` if you are being cut off mid-sentence.

A failed LLM call is spoken as a short apology and deliberately **not** written to the conversation history — persisting it fed the failure back as context on every later turn.

## Testing hardware

[scripts/test_hardware.py](scripts/test_hardware.py) exercises the LED matrix and servo without needing any STT/LLM/TTS API keys configured:

```bash
python scripts/test_hardware.py         # canned demo: eye states + servo sweep
python scripts/test_hardware.py --doa   # live: servo + eyes follow the ReSpeaker's direction-of-arrival
```

Other scripts, roughly in the order you'd reach for them:

| Script | What it's for |
|---|---|
| `track_audio.py` | Live DOA tracking on the servo alone, using the same production code path as `main.py` but without STT/LLM/TTS/radar/matrix |
| `print_doa.py` | Prints only the tracked DOA angle — used to find `DOA_FRONT_REFERENCE_DEGREES` by standing dead ahead |
| `sweep_servo.py` | Sweeps the full safe window end to end; the quickest way to check the range is what you think it is |
| `probe_servo_range.py` | Finds the servo's **real** mechanical limits one small step at a time, waiting for you between each. Stops at the first sign of resistance |
| `calibrate_servo_home.py` | Nudge-then-save flow for the home offset, unclamped by the safe window |
| `home_servo.py` | Drives to home (or `--angle N`) and holds there — handy while assembling the head |
| `print_radar.py` | Radar/ESP-NOW link only: prints incoming targets and person counts |
| `check_stale_targets.py` | Reports how long radar targets sit frozen, to check whether the firmware's ghost filter is erasing real, motionless people |
| `raw_serial_monitor.py` | Raw bytes off the bridge's serial port, bypassing JSON parsing — for diagnosing a link that "connects" but yields nothing usable |
| `bridge_check.py` | Radar link check without opening the case: counts packets, blinks the tie LED (proves the bridge reads USB), restarts the bridge over USB and listens again |
| `find_zone_edge.py` | Beeps on zone entry/exit to tape the floor at the real trigger point; `--trace` prints every frame with its distance to the zone edge |
| `../study/radar_replay.html` | Open in Safari or Chrome on the Mac: rebuilds the web app's radar view from a study trial log (`privacy_switch_*_trials.jsonl`), puts it next to a camera video synced on the assistant's first word, and exports both as one clip |
| `../study/radar_export/` | The same radar view rendered to MP4, radar only, for compositing in a video editor. `npm install` once in that folder, then `python3 pick_trials.py <csv> <trials.jsonl> <settings.json>` picks the cleanest trial per case and `node export_clips.js <trials.jsonl> <out dir> <ids> "<door zone x,x,y,y>"` writes one clip per trial plus a `.txt` with the Pi-clock time of the first frame and every event |
| `test_llm.py` / `probe_gemini.sh` | LLM latency: the app's exact request with per-chunk timing, and a plain-curl IPv4 vs IPv6 comparison for Gemini |
| `test_tie_led.py` | Exercises the 7-LED tie strip on the bridge ESP32 |
| `simulate_doa.py` | Replays DOA angles through the same conversion the live adapter uses, no hardware needed |
| `study_head_accuracy.py` | Runs Study 1 (head-orientation accuracy), logging every trial — see [study/README.md](study/README.md) |
| `analyse_head_accuracy.py` | Analyses the Study 1 CSVs: error decomposition, Kruskal-Wallis, optional plots |
| `study_privacy_switch.py` | Runs Study 2 (privacy switch and door-zone ducking) with the live radar and TTS; `--report` analyses a session, `--list`/`--drop` review or remove trials, `--simulate` rehearses without hardware. `--recording` plays `study/private_reply.wav` instead of live TTS — WAVs are gitignored, so put one there first |

[scripts/home_servo.py](scripts/home_servo.py) moves the servo to its front-facing center position and holds it there (doesn't detach) until you Ctrl+C — handy while physically assembling the head, so you can attach the horn/mount at a known reference angle instead of wherever it happened to power on at:

```bash
venv/bin/python scripts/home_servo.py
```

The `--doa` mode needs `pyusb` and, once per Pi, a udev rule so the ReSpeaker's USB HID interface is readable without root:

```bash
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="2886", ATTRS{idProduct}=="0018", MODE="0666"' \
  | sudo tee /etc/udev/rules.d/99-respeaker.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Note the ReSpeaker reports a full 0–359° angle, but the servo is physically clamped to `SERVO_MIN_ANGLE`–`SERVO_MAX_ANGLE` — directions behind/beside the assistant just pin it at its nearest limit, which is expected. 90° is "straight ahead" for both the servo and the eyes.

Servo motion is deliberately not 1:1 with raw DOA readings — `--doa` only reacts while the mic's onboard VAD (`RespeakerDOAAdapter.get_voice_active()`) says something voice-like is happening. On top of that, `service_layer/handlers.py`'s `start_doa_tracking()` takes a median of several DOA samples (`doa_samples`, default 5) spread across the eye-lead window rather than reacting to a single reading, and `ServoTurntableAdapter._ramp_to()` glides to the target over small intermediate PWM steps (`steps_per_s`, default 20) instead of jumping in one update. Tune those if it feels too sluggish or still too twitchy — but if the servo is buzzing/jittering while *holding* a constant angle (no new movement in the log either), that's PWM signal jitter, not these constants. Set `SERVO_USE_PIGPIO=true` and install/enable the daemon so it survives a reboot:

```bash
sudo apt install pigpio
sudo systemctl enable --now pigpiod
```

A one-off `sudo pigpiod` also works but doesn't persist across a reboot, so prefer the `systemctl` form. `ServoTurntableAdapter` logs which PWM backend actually ended up active at startup (`[Servo] PWM-Backend: ...`) — check that line if twitching shows up again: `PiGPIOFactory` is the jitter-free DMA-timed one, anything else means `SERVO_USE_PIGPIO` isn't taking effect and it's still on software-timed PWM.

### Servo angles are a scale, not just a limit

`SERVO_HARDWARE_MIN_ANGLE`/`SERVO_HARDWARE_MAX_ANGLE` map linearly onto `SERVO_MIN_PULSE_WIDTH`/`SERVO_MAX_PULSE_WIDTH`. They are **not** a safety clamp (that's `SERVO_MIN_ANGLE`/`SERVO_MAX_ANGLE`) — they define what a "degree" means. If they claim a wider sweep than the servo really performs, every commanded degree moves proportionally less, silently and with no error:

> This bit us: `SERVO_HARDWARE_MAX_ANGLE` was `360` on a servo that sweeps 180°, so every commanded degree moved *half* a real degree and the head only ever had 90° of usable travel. Tracking looked inaccurate because each move under-turned by 2×.

To verify: run `scripts/sweep_servo.py` and measure the actual sweep. If commanding the full window doesn't produce that many physical degrees, set `SERVO_HARDWARE_MAX_ANGLE` to what you measured. Use `scripts/probe_servo_range.py` to find the true mechanical limits first if you don't know them.

### LED matrix

If the matrix flickers, `LedMatrix`'s `pwm_bits` (default 5, down from the library's default 11) trades unneeded color depth for a higher refresh rate, and `limit_refresh_rate_hz` (default 0 = uncapped) no longer artificially caps how fast it can refresh — lower `pwm_bits` further if it still flickers, or raise it for smoother gradients if you can spare the refresh rate. The library's own startup hint about adding `isolcpus=3` to `/boot/cmdline.txt` (dedicating a CPU core to the matrix refresh thread, reboot required) is a bigger lever if `pwm_bits` alone isn't enough — worth it now that STT/LLM/TTS run in the cloud rather than competing for CPU on the Pi.

If the panel shows ghosting or stray static pixels while otherwise rendering correctly, that's a signal-timing symptom, not a dead panel/cable — try raising `GPIO_SLOWDOWN` (default 5) a step or two at a time; it trades max refresh rate for cleaner signal integrity.
