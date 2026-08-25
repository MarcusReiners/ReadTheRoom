import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_opt(name: str) -> str | None:
    return os.environ.get(name) or None


# --- Provider selection -----------------------------------------------
# STT_PROVIDER / TTS_PROVIDER: elevenlabs | local | remote
#   elevenlabs -> cloud API (adapters/stt|tts/elevenlabs.py)
#   local      -> in-process model, e.g. faster-whisper/Piper (adapters/stt|tts/local.py)
#   remote     -> HTTP call to the Mac pipeline server (adapters/stt|tts/remote.py)
STT_PROVIDER = _env("STT_PROVIDER", "elevenlabs")
TTS_PROVIDER = _env("TTS_PROVIDER", "elevenlabs")

# --- API keys (never hardcode these) -----------------------------------
ELEVENLABS_API_KEY = _env_opt("ELEVENLABS_API_KEY")
OPENAI_API_KEY = _env_opt("OPENAI_API_KEY")
GEMINI_API_KEY = _env_opt("GEMINI_API_KEY")

# --- ElevenLabs settings ------------------------------------------------
ELEVENLABS_VOICE_ID = _env("ELEVENLABS_VOICE_ID", "wDsJlOXPqcvIUKdLXjDs")
ELEVENLABS_TTS_MODEL = _env("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")
ELEVENLABS_STT_MODEL = _env("ELEVENLABS_STT_MODEL", "scribe_v1")
ELEVENLABS_LANGUAGE_CODE = _env("ELEVENLABS_LANGUAGE_CODE", "deu")
# ISO 639-1 (two-letter), unlike ELEVENLABS_LANGUAGE_CODE above which is
# STT's ISO 639-3 - only eleven_flash_v2_5/eleven_turbo_v2_5-class models
# actually enforce this for TTS.
ELEVENLABS_TTS_LANGUAGE_CODE = _env("ELEVENLABS_TTS_LANGUAGE_CODE", "en")

# --- LLM (via LiteLLM, provider swap = change the model string) --------
LLM_MODEL = _env("LLM_MODEL", "openai/gpt-4o-mini")
LLM_API_BASE = _env_opt("LLM_API_BASE")
LLM_FALLBACK_MODEL = _env_opt("LLM_FALLBACK_MODEL") or "ollama_chat/qwen3.5:9b"
LLM_FALLBACK_API_BASE = _env_opt("LLM_FALLBACK_API_BASE") or "http://192.168.178.37:11434"

# --- Local model fallback paths -----------------------------------------
WHISPER_MODEL = _env(
    "WHISPER_MODEL",
    "/home/marcus/voice-pipeline/whisper-data/whisper-tiny-german-1224-ct2",
)
WHISPER_THREADS = int(_env("WHISPER_THREADS", "4"))
WHISPER_VAD = _env_bool("WHISPER_VAD", False)
PIPER_MODEL = _env("PIPER_MODEL", "/home/marcus/piper-voices/de_DE-thorsten-low.onnx")

# --- Mac pipeline server (local network fallback) -----------------------
MAC_SERVER_URL = _env("MAC_SERVER_URL", "http://127.0.0.1:8000")
# Must match the sample rate of the Piper voice mac_server.py loads (PIPER_MODEL).
MAC_SERVER_TTS_SAMPLE_RATE = int(_env("MAC_SERVER_TTS_SAMPLE_RATE", "16000"))

# --- Audio I/O ------------------------------------------------------------
MIC_DEVICE = _env("MIC_DEVICE", "hw:ArrayUAC10,0")
MIC_CHANNELS = int(_env("MIC_CHANNELS", "6"))
MIC_RATE = int(_env("MIC_RATE", "16000"))
SPEAKER_DEVICE = _env("SPEAKER_DEVICE", "plughw:CARD=ArrayUAC10,DEV=0")
MAX_RECORD_SECONDS = int(_env("MAX_RECORD_SECONDS", "30"))

# --- Hardware features ----------------------------------------------------
USE_LED_MATRIX = _env_bool("USE_LED_MATRIX", True)
# Raise if the panel shows ghosting/stray static pixels - gives the Pi more
# settling time per GPIO toggle, at the cost of max refresh rate.
GPIO_SLOWDOWN = int(_env("GPIO_SLOWDOWN", "5"))
LED_MATRIX_BRIGHTNESS = int(_env("LED_MATRIX_BRIGHTNESS", "90"))
# Lower trades color depth for a higher hardware refresh rate (less visible
# flicker) - 5 bits is still 32 levels/channel, plenty for solid dots/text.
LED_MATRIX_PWM_BITS = int(_env("LED_MATRIX_PWM_BITS", "5"))

# --- Servo turntable (DS3225 digital 25kg servo on hardware PWM) ----------
USE_SERVO = _env_bool("USE_SERVO", True)
SERVO_GPIO_PIN = int(_env("SERVO_GPIO_PIN", "19"))
# Safe operating clamp - every commanded angle is restricted to this window so the
# head can never wind the cables running into it. NOT the servo's physical range.
# Keep this CENTERED inside SERVO_HARDWARE_MIN/MAX_ANGLE below: the window's
# midpoint is what home() drives to, so an off-center window puts "straight
# ahead" off-center in the servo's real travel and gives more headroom to one
# side than the other.
SERVO_MIN_ANGLE = float(_env("SERVO_MIN_ANGLE", "0"))
SERVO_MAX_ANGLE = float(_env("SERVO_MAX_ANGLE", "180"))
# The servo's true mechanical range, used only to calibrate pulse-width-to-angle.
# Passing SERVO_MIN_ANGLE/MAX_ANGLE here instead would stretch the full pulse
# range across just the safe window, turning every commanded move within that
# window into a near-full physical rotation - confirmed on the bench.
#
# These are LABELS on a linear map, not limits: MIN maps to SERVO_MIN_PULSE_WIDTH
# and MAX to SERVO_MAX_PULSE_WIDTH, with everything between interpolated. So this
# must equal the servo's REAL physical sweep across that pulse range, or every
# commanded "degree" silently stops being a degree. This was 360 against a servo
# that physically sweeps 180 - so each commanded degree moved only half a real
# degree, and a "5 degree" probe step was really 2.5. That scaling error is a
# likely contributor to past head-tracking inaccuracy, not just a range problem.
#
# 180 here matches the observed behaviour (a full 0-360 command swept 180
# physical degrees), but the DS3225 ships in BOTH 180 and 270 degree variants
# using the identical 500-2500us pulse range, so the model name alone can't
# settle it. Worth re-measuring now that pigpio is actually active: every
# earlier measurement ran on software-timed PWM, whose pulse-width accuracy is
# poor near the extremes and can under-sweep the true range.
SERVO_HARDWARE_MIN_ANGLE = float(_env("SERVO_HARDWARE_MIN_ANGLE", "0"))
SERVO_HARDWARE_MAX_ANGLE = float(_env("SERVO_HARDWARE_MAX_ANGLE", "180"))
# Pulse widths (seconds) the two hardware angles above map to. 500-2500us is the
# DS3225's documented full range and what its rated sweep is specified against;
# widening further is how you'd chase more travel, but it drives the servo
# toward its internal end stops, so change these only with the horn unloaded
# and stop at the first sign of strain.
SERVO_MIN_PULSE_WIDTH = float(_env("SERVO_MIN_PULSE_WIDTH", "0.0005"))
SERVO_MAX_PULSE_WIDTH = float(_env("SERVO_MAX_PULSE_WIDTH", "0.0025"))
# Use pigpio's DMA-timed PWM instead of gpiozero's default software-timed thread -
# fixes chatter while holding a fixed position. Needs `sudo pigpiod` running.
SERVO_USE_PIGPIO = _env_bool("SERVO_USE_PIGPIO", False)
# Where the calibrated home-offset (see adapters/hardware/servo_calibration.py)
# is persisted - survives restarts, editable via scripts/calibrate_servo_home.py
# or the web app's settings page.
SERVO_CALIBRATION_PATH = _env("SERVO_CALIBRATION_PATH", "servo_calibration.json")
# Raw DOA degrees the ReSpeaker reports when someone is actually standing
# straight ahead of the mount - see RespeakerDOAAdapter.get_direction_degrees().
# Tune by watching "[DOA] Stimme erkannt bei X Grad" while standing dead ahead.
DOA_FRONT_REFERENCE_DEGREES = float(_env("DOA_FRONT_REFERENCE_DEGREES", "0"))

# --- Radar (HLK-LD2450 on a Seeed XIAO ESP32S3 -> ESP-NOW -> bridge ESP32 --
# --- -> USB serial into the Pi; see mmWave/ and mmWaveBridge/) -------------
# RADAR_PROVIDER: ld2450 | dummy
RADAR_PROVIDER = _env("RADAR_PROVIDER", "ld2450")
RADAR_SERIAL_PORT = _env("RADAR_SERIAL_PORT", "/dev/ttyUSB0")
RADAR_SERIAL_BAUD = int(_env("RADAR_SERIAL_BAUD", "115200"))

# --- Chat bridge (web chat app mirroring the conversation) -----------------
CHAT_BRIDGE_HOST = _env("CHAT_BRIDGE_HOST", "0.0.0.0")
CHAT_BRIDGE_PORT = int(_env("CHAT_BRIDGE_PORT", "8765"))
CONVERSATIONS_DB_PATH = _env("CONVERSATIONS_DB_PATH", "conversations.db")
# Where the system prompt / ElevenLabs voice ID edited from the web app's
# settings tab are persisted - survives restarts, same idea as
# SERVO_CALIBRATION_PATH.
APP_SETTINGS_PATH = _env("APP_SETTINGS_PATH", "app_settings.json")

# --- Logging (local only, never uploaded) ---------------------------------
LOG_DIR = _env("LOG_DIR", "logs")
LOG_FILE = _env("LOG_FILE", "readtheroom.log")
LOG_LEVEL = _env("LOG_LEVEL", "INFO")
LOG_MAX_BYTES = int(_env("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(_env("LOG_BACKUP_COUNT", "5"))
