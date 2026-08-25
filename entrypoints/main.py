import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logging_setup
from domain.conversation import SYSTEM_PROMPT, ConversationState
from domain.events import (
    AssistantDeltaReceived,
    AssistantMessageCompleted,
    AssistantTurnCancelled,
    ListeningStateChanged,
    SpeechPlaybackEnded,
    SpeechPlaybackStarted,
    SpeechTranscribed,
)
from service_layer.bus import EventBus
from service_layer.handlers import register_handlers, register_tie_led_handlers, start_doa_tracking

from adapters.factory import build_stt, build_tts, build_radar, build_turntable
from adapters.app_settings import load_app_settings
from adapters.chat_bridge import ChatBridgeAdapter
from adapters.conversation_store import ConversationStore
from adapters.llm import LLMGatewayAdapter
from adapters.hardware.face_display import DummyFaceDisplayAdapter

logger = logging.getLogger(__name__)


def _spawn_recorder(output_file: str):
    """Starts the platform recorder subprocess in the background (does not
    block). Returns (proc, raw_file) - raw_file is None on macOS since sox
    writes directly to output_file there; on ALSA it's the raw-PCM file that
    still needs converting once recording stops, in _finish_recording().
    """
    if sys.platform == "darwin":
        proc = subprocess.Popen(
            ["sox", "-d", "-r", str(config.MIC_RATE), "-c", "1", "-b", "16", output_file],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        return proc, None

    raw_file = "temp_in_raw.pcm"
    proc = subprocess.Popen(
        ["arecord", "-D", config.MIC_DEVICE, "-t", "raw",
         "-r", str(config.MIC_RATE), "-f", "S16_LE", "-c", str(config.MIC_CHANNELS), raw_file],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    return proc, raw_file


def _finish_recording(proc, raw_file, output_file: str) -> bool:
    """Stops the recorder subprocess and (on ALSA) converts raw PCM to the
    final mono wav. Shared by both the manual (ENTER) and automatic
    (VAD-triggered) capture paths - only how "stop now" gets decided differs
    between them, not what happens once it's decided."""
    proc.terminate()
    proc.wait()

    if raw_file is None:
        return os.path.exists(output_file) and os.path.getsize(output_file) > 0

    try:
        if not os.path.exists(raw_file) or os.path.getsize(raw_file) == 0:
            stderr = proc.stderr.read().decode().strip() if proc.stderr else ""
            logger.error("arecord Fehler: %s", stderr or "keine Audiodaten aufgenommen")
            return False

        result = subprocess.run(
            ["sox", "-t", "raw", "-r", str(config.MIC_RATE), "-e", "signed", "-b", "16",
             "-c", str(config.MIC_CHANNELS), raw_file, "-c", "1", output_file, "remix", "1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            logger.error("sox Fehler: %s", result.stderr.decode().strip())
            return False

        return os.path.exists(output_file)
    finally:
        if os.path.exists(raw_file):
            os.remove(raw_file)


def record_audio(output_file: str) -> bool:
    """Manual capture: starts recording immediately, stops on the next ENTER
    (or after MAX_RECORD_SECONDS, whichever comes first)."""
    proc, raw_file = _spawn_recorder(output_file)
    watchdog = threading.Timer(config.MAX_RECORD_SECONDS, proc.terminate)
    watchdog.start()
    try:
        input()
    finally:
        watchdog.cancel()
    return _finish_recording(proc, raw_file, output_file)


# ElevenLabs Scribe (and Whisper-family STT generally) transcribes non-speech
# sounds it can still identify - coughs, throat-clears, drumming - as bracketed
# annotations like "[hustet]" or "[Trommel]" instead of refusing outright.
# That's it correctly telling us "this wasn't speech", so strip these out
# wherever they appear (not just when the whole transcript is one) - a
# transcript like "Ja [hustet] genau" should become "Ja genau", not be kept
# or discarded wholesale.
_NON_SPEECH_ANNOTATION_RE = re.compile(r"[\[(][^\])]*[\])]")

# turn_queue is a PriorityQueue of (priority, monotonic_ns, text) - lower
# priority number is served first. A typed chat message should always jump
# ahead of anything still waiting from voice/console input (see
# ChatBridgeAdapter's user_text handler, which also interrupts an
# already-in-progress voice-triggered reply rather than just queuing behind
# it - see handle_turn()'s cancel_event). The monotonic_ns timestamp is only
# a tie-breaker preserving arrival order within the same priority; it's never
# compared for its own sake.
PRIORITY_CHAT = 0
PRIORITY_VOICE = 1


def _enqueue_turn(turn_queue: "queue.PriorityQueue", priority: int, text: str) -> None:
    turn_queue.put((priority, time.monotonic_ns(), text))


def _strip_non_speech_annotations(text: str) -> str:
    stripped = _NON_SPEECH_ANNOTATION_RE.sub("", text)
    return re.sub(r"\s+", " ", stripped).strip()


def _transcribe_and_enqueue(stt, turn_queue: "queue.PriorityQueue", temp_in: str, log_prefix: str) -> None:
    audio_s = max(0, os.path.getsize(temp_in) - 44) / (config.MIC_RATE * 2)
    t_stt = time.monotonic()
    user_text = stt.transcribe(temp_in)
    logger.info("[timing] STT (%s): %.2fs fuer %.1fs Audio", log_prefix, time.monotonic() - t_stt, audio_s)
    if os.path.exists(temp_in):
        os.remove(temp_in)

    if user_text:
        cleaned = _strip_non_speech_annotations(user_text)
        if cleaned != user_text.strip():
            logger.info("[%s] Nicht-Sprache-Anmerkungen entfernt: '%s' -> '%s'", log_prefix, user_text, cleaned)
        user_text = cleaned

    if not user_text or len(user_text) < 2:
        logger.warning("%s: nichts erkannt.", log_prefix)
        return

    logger.info("[%s] Du hast gesagt: '%s'", log_prefix, user_text)
    _enqueue_turn(turn_queue, PRIORITY_VOICE, user_text)


def console_input_loop(
    bus: EventBus, turn_queue: "queue.PriorityQueue", stt, mic_lock: threading.Lock,
) -> None:
    """Reads stdin on its own thread: bare ENTER starts/stops a voice recording,
    whose result is pushed onto turn_queue like any other turn. Still works
    as a manual override alongside automatic VAD-triggered recording."""
    print("\nENTER startet die Aufnahme, ENTER stoppt sie.\n")
    while True:
        try:
            line = input()
        except EOFError:
            break

        temp_in = "temp_in.wav"
        print("Aufnahme läuft... sprechen und mit ENTER beenden.")
        with mic_lock:
            bus.publish(ListeningStateChanged(listening=True))
            success = record_audio(temp_in)
            bus.publish(ListeningStateChanged(listening=False))

        if not success:
            logger.error("Aufnahme fehlgeschlagen.")
            continue

        _transcribe_and_enqueue(stt, turn_queue, temp_in, "manuell")


def vad_input_loop(
    bus: EventBus, turn_queue: "queue.PriorityQueue", stt, doa, doa_lock: threading.Lock,
    mic_lock: threading.Lock, assistant_speaking: threading.Event,
    poll_interval_s: float = 0.2, trailing_silence_s: float = 0.8, trigger_confirm_polls: int = 2,
    calibration_mode: threading.Event | None = None, composing_mode: threading.Event | None = None,
) -> None:
    """Hands-free alternative to console_input_loop: watches the ReSpeaker's
    onboard VAD and starts recording automatically once it detects speech,
    stopping after trailing_silence_s of continued silence (or
    MAX_RECORD_SECONDS, whichever comes first) instead of waiting for ENTER.

    doa_lock serializes USB control-transfer access to the ReSpeaker with
    start_doa_tracking()'s own polling loop, since both read the same device
    concurrently from different threads. mic_lock defers to a manual
    (console_input_loop) recording already in progress instead of racing it
    for the same audio device. assistant_speaking skips triggering entirely
    while TTS is playing - the mic picks up the assistant's own voice same as
    any other sound, and without this guard that reliably self-triggers a
    "recording" of the assistant talking to itself.

    calibration_mode: same Event start_doa_tracking() pauses on (set while
    the web app's settings tab is open) - skipped here too, so the assistant
    doesn't pick up and later answer whatever gets said while someone's
    fiddling with servo calibration.

    composing_mode: set while a client has the chat text box focused (see
    ChatBridgeAdapter's set_composing) - skipped here too, so typing a
    message (possibly while talking, to someone else in the room or reading
    the draft aloud) doesn't also get picked up and queued as a second,
    separate voice turn.

    trigger_confirm_polls: after voice_active first flips true, recording
    starts immediately (so no audio is lost) but is discarded unless
    voice_active stays true for this many more consecutive polls - rejects a
    single brief noise blip (a knock, click, a moment of servo/motor noise)
    without needing to delay the start of a real utterance to find out.
    trailing_silence_s deliberately isn't too short either: cutting off after
    a shorter gap clips natural mid-sentence pauses (thinking, a breath).
    """
    while True:
        if (
            assistant_speaking.is_set()
            or (calibration_mode is not None and calibration_mode.is_set())
            or (composing_mode is not None and composing_mode.is_set())
        ):
            time.sleep(poll_interval_s)
            continue

        with doa_lock:
            active = doa.get_voice_active()
        if not active:
            time.sleep(poll_interval_s)
            continue

        if not mic_lock.acquire(blocking=False):
            # A manual recording is already running - don't fight it for the
            # mic, just wait for the next VAD trigger after it's done.
            time.sleep(poll_interval_s)
            continue

        abort_reason = None
        try:
            temp_in = "temp_in_vad.wav"
            bus.publish(ListeningStateChanged(listening=True))
            proc, raw_file = _spawn_recorder(temp_in)

            # Confirmation phase: recording's already running (so no audio is
            # lost), but require voice_active to stay true a few more polls
            # before treating this as real speech rather than a brief blip.
            confirmed_polls = 0
            while confirmed_polls < trigger_confirm_polls:
                time.sleep(poll_interval_s)
                if assistant_speaking.is_set():
                    abort_reason = "Assistent hat zu sprechen begonnen"
                    break
                if calibration_mode is not None and calibration_mode.is_set():
                    abort_reason = "Kalibrierungsmodus aktiv"
                    break
                if composing_mode is not None and composing_mode.is_set():
                    abort_reason = "Nutzer tippt im Chat"
                    break
                with doa_lock:
                    still_active = doa.get_voice_active()
                if still_active:
                    confirmed_polls += 1
                else:
                    abort_reason = "nur kurzer Ausschlag, keine echte Sprache"
                    break

            if abort_reason is None:
                start_time = time.monotonic()
                last_active_time = start_time
                while True:
                    time.sleep(poll_interval_s)
                    if assistant_speaking.is_set():
                        # TTS started mid-recording - almost certainly means this
                        # recording has picked up (or is about to pick up) the
                        # assistant's own voice. Discard rather than transcribe it.
                        abort_reason = "Assistent hat zu sprechen begonnen"
                        break
                    if calibration_mode is not None and calibration_mode.is_set():
                        abort_reason = "Kalibrierungsmodus aktiv"
                        break
                    if composing_mode is not None and composing_mode.is_set():
                        abort_reason = "Nutzer tippt im Chat"
                        break
                    with doa_lock:
                        still_active = doa.get_voice_active()
                    if still_active:
                        last_active_time = time.monotonic()
                    if time.monotonic() - last_active_time >= trailing_silence_s:
                        break
                    if time.monotonic() - start_time >= config.MAX_RECORD_SECONDS:
                        break

            bus.publish(ListeningStateChanged(listening=False))
            success = _finish_recording(proc, raw_file, temp_in)
        finally:
            mic_lock.release()

        if abort_reason is not None:
            if os.path.exists(temp_in):
                os.remove(temp_in)
            logger.info("[VAD] Aufnahme verworfen (%s).", abort_reason)
            continue

        if not success:
            logger.error("VAD-Aufnahme fehlgeschlagen.")
            continue

        _transcribe_and_enqueue(stt, turn_queue, temp_in, "VAD")


def handle_turn(
    text: str, bus: EventBus, conversation: ConversationState, store: ConversationStore,
    llm, tts, cancel_event: threading.Event | None = None,
) -> None:
    """cancel_event: set by ChatBridgeAdapter the instant a chat message is
    typed and sent, so a still-in-progress voice-triggered reply gets cut off
    rather than finishing first - chat always has priority (see
    PRIORITY_CHAT/PRIORITY_VOICE above for the queued-but-not-yet-started
    half of that same rule). Checked once per streamed LLM delta below;
    cleared by main()'s turn loop just before each handle_turn() call, so a
    stale set from an idle moment never affects the next turn."""
    conversation_id = store.get_active_id()
    history = store.get_history(conversation_id)

    replaced = store.replace_or_add_user_message(conversation_id, text)
    bus.publish(SpeechTranscribed(text=text, conversation_id=conversation_id, replaced=replaced))

    # No recentering here on purpose: the head should stay exactly where DOA
    # tracking last left it (facing whoever just spoke) all the way through
    # the reply, not snap back to home first. start_doa_tracking()'s own
    # assistant_speaking guard already stops it from reacting to anything
    # further while the reply plays, so it just holds this position until
    # tracking resumes afterward.

    def mirrored_deltas():
        for delta in llm.ask_stream(text, history):
            if cancel_event is not None and cancel_event.is_set():
                break
            bus.publish(AssistantDeltaReceived(delta=delta, conversation_id=conversation_id))
            yield delta

    # Speak aloud only while alone (or voice hasn't been muted from the chat
    # app) - otherwise just stream the answer as text into the chat.
    should_speak = conversation.voice_enabled and conversation.modality == "voice"
    if should_speak:
        full_text = tts.speak_stream(mirrored_deltas())
    else:
        full_text = "".join(mirrored_deltas())

    if cancel_event is not None and cancel_event.is_set():
        # Cut short by a higher-priority chat message - don't persist a
        # truncated reply to a user message that's about to be overwritten
        # anyway, and tell the frontend to drop whatever partial bubble it
        # has instead of finalizing it as a real answer.
        bus.publish(AssistantTurnCancelled(conversation_id=conversation_id))
        return

    store.add_assistant_message(conversation_id, full_text)
    bus.publish(AssistantMessageCompleted(text=full_text, conversation_id=conversation_id))


def main() -> None:
    logging_setup.configure_logging(config)

    bus = EventBus()
    conversation = ConversationState()
    store = ConversationStore(config.CONVERSATIONS_DB_PATH)
    if store.get_active_id() is None:
        store.set_active_id(store.create_conversation())
    turn_queue: "queue.PriorityQueue" = queue.PriorityQueue()

    # Everything the web app's settings tab can edit lives in one file, with
    # config.py / the domain layer supplying the fallbacks for a fresh
    # install that has no settings file yet.
    app_settings = load_app_settings(config.APP_SETTINGS_PATH, defaults={
        "system_prompt": SYSTEM_PROMPT,
        "llm_model": config.LLM_MODEL,
        "voice_id": config.ELEVENLABS_VOICE_ID,
        "volume": 1.0,
        "servo_min_angle": config.SERVO_MIN_ANGLE,
        "servo_max_angle": config.SERVO_MAX_ANGLE,
    })
    active_voice = next(
        (v for v in app_settings["voices"] if v["id"] == app_settings["active_voice_id"]),
        app_settings["voices"][0],
    )

    stt = build_stt(config)
    tts = build_tts(config, bus)
    if hasattr(tts, "voice_id"):
        tts.voice_id = active_voice["voice_id"]
    if hasattr(tts, "set_volume"):
        tts.set_volume(app_settings["volume"])
    llm = LLMGatewayAdapter(
        model=app_settings["llm_model"],
        api_base=config.LLM_API_BASE,
        fallback_model=config.LLM_FALLBACK_MODEL,
        fallback_api_base=config.LLM_FALLBACK_API_BASE,
        system_prompt=app_settings["system_prompt"],
    )

    radar = build_radar(config, bus)
    # build_turntable() already applies the saved safe range (see
    # adapters/factory.py) so the hardware scripts and the assistant agree.
    turntable = build_turntable(config)

    # Set for as long as the web app's settings tab (servo home calibration)
    # is open - see start_doa_tracking()'s calibration_mode param and
    # LedEyesAdapter's wrench-instead-of-eyes rendering, both of which check
    # this same Event. Created unconditionally (even with USE_SERVO/
    # USE_LED_MATRIX off) so ChatBridgeAdapter always has one to set/clear.
    calibration_mode = threading.Event()

    # Set for as long as a client has the chat text box focused - vad_input_loop
    # pauses on this too (see its composing_mode param), so typing a message
    # doesn't also get picked up as a separate spoken turn. Created unconditionally
    # for the same reason as calibration_mode above.
    composing_mode = threading.Event()

    # Set by ChatBridgeAdapter the instant a chat message is typed and sent -
    # see handle_turn()'s cancel_event param. Cleared by the turn loop below
    # just before each handle_turn() call.
    cancel_current_turn = threading.Event()

    if config.USE_LED_MATRIX:
        from adapters.hardware.led_matrix import LedMatrix
        from adapters.hardware.led_eyes import LedEyesAdapter

        matrix = LedMatrix(
            rows=48, cols=96, chain=1,
            gpio_slowdown=config.GPIO_SLOWDOWN,
            brightness=config.LED_MATRIX_BRIGHTNESS,
            pwm_bits=config.LED_MATRIX_PWM_BITS,
        )
        face = LedEyesAdapter(
            bus=bus, x_offset=0, width=96, height=48,
            min_angle_degrees=turntable.safe_min_angle,
            max_angle_degrees=turntable.safe_max_angle,
            calibration_mode=calibration_mode,
        )
        matrix.add_renderer(face)
        matrix.start()
    else:
        face = DummyFaceDisplayAdapter(bus=bus)

    register_handlers(bus, conversation, tts)

    chat_bridge = ChatBridgeAdapter(
        bus, conversation, store, turn_queue,
        radar=radar,
        turntable=turntable,
        servo_calibration_path=config.SERVO_CALIBRATION_PATH,
        llm=llm,
        tts=tts,
        app_settings_path=config.APP_SETTINGS_PATH,
        voices=app_settings["voices"],
        active_voice_id=app_settings["active_voice_id"],
        calibration_mode=calibration_mode,
        composing_mode=composing_mode,
        cancel_current_turn=cancel_current_turn,
        host=config.CHAT_BRIDGE_HOST, port=config.CHAT_BRIDGE_PORT,
    )
    chat_bridge.start()

    radar.start()
    register_tie_led_handlers(bus, radar)

    mic_lock = threading.Lock()

    if config.USE_SERVO:
        from adapters.hardware.doa_respeaker import RespeakerDOAAdapter

        doa = RespeakerDOAAdapter(front_reference_degrees=config.DOA_FRONT_REFERENCE_DEGREES)
        doa_lock = threading.Lock()

        assistant_speaking = threading.Event()
        bus.subscribe(SpeechPlaybackStarted, lambda e: assistant_speaking.set())
        bus.subscribe(SpeechPlaybackEnded, lambda e: assistant_speaking.clear())

        start_doa_tracking(
            doa, turntable, face, lock=doa_lock,
            assistant_speaking=assistant_speaking, calibration_mode=calibration_mode,
        )
        threading.Thread(
            target=vad_input_loop,
            args=(bus, turn_queue, stt, doa, doa_lock, mic_lock, assistant_speaking),
            kwargs={"calibration_mode": calibration_mode, "composing_mode": composing_mode},
            daemon=True,
        ).start()

    threading.Thread(target=console_input_loop, args=(bus, turn_queue, stt, mic_lock), daemon=True).start()

    print(f"\nChat: http://<Pi-Adresse>:{config.CHAT_BRIDGE_PORT}\n")

    while True:
        try:
            _priority, _seq, text = turn_queue.get()
            # Holds a queued turn (typed while on the settings tab, or a
            # console/VAD turn that slipped in right as calibration started)
            # until calibration_mode clears, rather than answering mid-
            # calibration or dropping it silently.
            while calibration_mode.is_set():
                time.sleep(0.2)
            cancel_current_turn.clear()
            try:
                handle_turn(text, bus, conversation, store, llm, tts, cancel_current_turn)
            except Exception:
                # One failed turn must not end the loop. Everything else
                # (radar, DOA tracking, the web app) runs on daemon threads,
                # so losing this thread used to take the whole process down
                # with it - or, worse, leave the unit visibly alive and
                # tracking while silently never answering again.
                logger.exception("Turn fehlgeschlagen - Assistent laeuft weiter.")
        except KeyboardInterrupt:
            print("\nCiao!")
            stt.stop()
            if hasattr(turntable, "stop"):
                turntable.stop()
            break


if __name__ == "__main__":
    main()
