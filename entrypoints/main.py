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
from adapters.llm import ERROR_REPLIES, LLMGatewayAdapter
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
            logger.error("arecord error: %s", stderr or "no audio recorded")
            return False

        result = subprocess.run(
            ["sox", "-t", "raw", "-r", str(config.MIC_RATE), "-e", "signed", "-b", "16",
             "-c", str(config.MIC_CHANNELS), raw_file, "-c", "1", output_file, "remix", "1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            logger.error("sox error: %s", result.stderr.decode().strip())
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
#
# Square brackets ONLY. This used to also strip anything in parentheses,
# which over-filters: Scribe marks audio events with square brackets, while
# parentheses appear in ordinary transcribed speech ("der Preis (netto) ist
# ...") and were being silently deleted from real sentences.
_NON_SPEECH_ANNOTATION_RE = re.compile(r"\[[^\]]*\]")

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


def _transcribe_and_enqueue(stt, turn_queue: "queue.PriorityQueue", temp_in: str, log_prefix: str,
                            turn_in_progress: threading.Event | None = None) -> None:
    # Set before transcription, not after: STT plus the LLM call is several
    # seconds of silence, and the head must not recenter during it.
    if turn_in_progress is not None:
        turn_in_progress.set()
    audio_s = max(0, os.path.getsize(temp_in) - 44) / (config.MIC_RATE * 2)
    t_stt = time.monotonic()
    user_text = stt.transcribe(temp_in)
    logger.info("[timing] STT (%s): %.2fs fuer %.1fs Audio", log_prefix, time.monotonic() - t_stt, audio_s)
    if os.path.exists(temp_in):
        os.remove(temp_in)

    if user_text:
        cleaned = _strip_non_speech_annotations(user_text)
        if cleaned != user_text.strip():
            logger.info("[%s] Removed non-speech annotations: '%s' -> '%s'", log_prefix, user_text, cleaned)
        user_text = cleaned

    if not user_text or len(user_text) < 2:
        logger.warning("%s: nothing recognised.", log_prefix)
        # Nothing was queued, so nothing will clear this later.
        if turn_in_progress is not None:
            turn_in_progress.clear()
        return

    logger.info("[%s] You said: '%s'", log_prefix, user_text)
    _enqueue_turn(turn_queue, PRIORITY_VOICE, user_text)


def console_input_loop(
    bus: EventBus, turn_queue: "queue.PriorityQueue", stt, mic_lock: threading.Lock,
) -> None:
    """Reads stdin on its own thread: bare ENTER starts/stops a voice recording,
    whose result is pushed onto turn_queue like any other turn. Still works
    as a manual override alongside automatic VAD-triggered recording."""
    print("\nENTER starts recording, ENTER stops it.\n")
    while True:
        try:
            line = input()
        except EOFError:
            break

        temp_in = "temp_in.wav"
        print("Recording... speak, then press ENTER to stop.")
        with mic_lock:
            bus.publish(ListeningStateChanged(listening=True))
            success = record_audio(temp_in)
            bus.publish(ListeningStateChanged(listening=False))

        if not success:
            logger.error("Recording failed.")
            continue

        _transcribe_and_enqueue(stt, turn_queue, temp_in, "manuell", turn_in_progress)


def _restart_on_error(loop, name: str):
    def run(*args, **kwargs):
        while True:
            try:
                return loop(*args, **kwargs)
            except Exception:
                logger.exception("%s crashed - restarting it.", name)
                time.sleep(1.0)
    return run


def vad_input_loop(
    bus: EventBus, turn_queue: "queue.PriorityQueue", stt, doa, doa_lock: threading.Lock,
    mic_lock: threading.Lock, assistant_speaking: threading.Event,
    poll_interval_s: float = 0.2, trailing_silence_s: float = 0.8, min_voice_polls: int = 2,
    calibration_mode: threading.Event | None = None, composing_mode: threading.Event | None = None,
    turn_in_progress: threading.Event | None = None,
    servo_recentering: threading.Event | None = None,
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

    servo_recentering: set by start_doa_tracking() only while the head is
    driving back to home after a silence timeout - never for tracking moves.
    A recenter happens when nobody has spoken for seconds, so VAD activity
    during one is the motor, not a person, and starting a recording on it is
    always wrong.

    Checked ONLY before opening a recording, never to abort one already in
    progress. Aborting was tried and was badly wrong: the head turns toward
    a speaker because they are speaking, so mid-recording servo checks threw
    away the recording of the very utterance that caused the move. If a
    recording is already open, some motor noise in the audio is a far
    smaller problem than losing the speech entirely.

    min_voice_polls: how many polls across the WHOLE recording must have
    reported voice for the result to be worth transcribing. Counted
    cumulatively, so pauses inside a sentence cost nothing, while a knock or
    a click never reaches the total. The recording always runs to its natural
    end first - this only decides whether to spend an STT call on it.
    trailing_silence_s deliberately isn't too short either: cutting off after
    a shorter gap clips natural mid-sentence pauses (thinking, a breath).
    """
    from adapters.hardware.doa_respeaker import DOAUnavailable

    while True:
        if (
            assistant_speaking.is_set()
            or (calibration_mode is not None and calibration_mode.is_set())
            or (composing_mode is not None and composing_mode.is_set())
            or (servo_recentering is not None and servo_recentering.is_set())
        ):
            time.sleep(poll_interval_s)
            continue

        try:
            with doa_lock:
                active = doa.get_voice_active()
        except DOAUnavailable:
            time.sleep(1.0)
            continue
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

            # One loop, and the keep-or-discard decision is made AFTER the
            # recording ends rather than during it. The previous design
            # aborted mid-utterance whenever voice_active read false for a
            # few consecutive polls - but ordinary sentences pause ("Hello,
            # who are you?"), and a pause past the tolerance threw the whole
            # recording away half-spoken. Nothing gets truncated now: a pause
            # shorter than trailing_silence_s keeps recording, a longer one
            # ends the turn exactly as it always did.
            start_time = time.monotonic()
            last_active_time = start_time
            voice_polls = 1  # the trigger poll that opened this recording
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
                try:
                    with doa_lock:
                        still_active = doa.get_voice_active()
                except DOAUnavailable:
                    abort_reason = "mic array stopped answering"
                    break
                if still_active:
                    voice_polls += 1
                    last_active_time = time.monotonic()
                if time.monotonic() - last_active_time >= trailing_silence_s:
                    break
                if time.monotonic() - start_time >= config.MAX_RECORD_SECONDS:
                    break

            # Cumulative, not consecutive: a real utterance accumulates voice
            # across its own pauses, while a single knock or click never
            # reaches the total. This only avoids a pointless STT call -
            # anything slipping through is caught downstream, where an empty
            # or annotation-only transcript is discarded anyway.
            if abort_reason is None and voice_polls < min_voice_polls:
                abort_reason = f"zu wenig Sprachaktivitaet ({voice_polls} Messungen)"

            bus.publish(ListeningStateChanged(listening=False))
            success = _finish_recording(proc, raw_file, temp_in)
        finally:
            mic_lock.release()

        if abort_reason is not None:
            if os.path.exists(temp_in):
                os.remove(temp_in)
            logger.info("[VAD] Recording discarded (%s).", abort_reason)
            continue

        if not success:
            logger.error("VAD-Recording failed.")
            continue

        _transcribe_and_enqueue(stt, turn_queue, temp_in, "VAD", turn_in_progress)


def handle_turn(
    text: str, bus: EventBus, conversation: ConversationState, store: ConversationStore,
    llm, tts, cancel_event: threading.Event | None = None, source: str = "voice",
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
    bus.publish(SpeechTranscribed(text=text, conversation_id=conversation_id, replaced=replaced,
                                  source=source))

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

    if full_text in ERROR_REPLIES:
        # An apology for a failed call, not something the assistant said.
        # Storing it fed the failure back as context on every later turn -
        # and left the question that triggered it sitting in the history
        # unanswered, so the model would keep trying to answer it alongside
        # whatever was actually asked next.
        logger.warning("Error reply not stored in the conversation history: %s", full_text)
    else:
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
        "reasoning_effort": config.LLM_REASONING_EFFORT,
        "stt_language_code": config.ELEVENLABS_LANGUAGE_CODE,
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
    if hasattr(stt, "language_code"):
        stt.language_code = app_settings["stt_language_code"] or None
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
        reasoning_effort=app_settings["reasoning_effort"],
        max_tokens=config.LLM_MAX_TOKENS,
    )
    llm.warm_up()

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

    # Set by start_doa_tracking() only while the head recenters after a
    # silence timeout - vad_input_loop() skips opening a recording then,
    # since VAD activity during a recenter is the motor, not a person.
    servo_recentering = threading.Event()

    # Set by ChatBridgeAdapter the instant a chat message is typed and sent -
    # Set from the moment transcription starts until the reply finishes, so
    # start_doa_tracking() does not recenter the head during the several
    # seconds of silence that STT and the LLM call occupy - turning away from
    # the person exactly as it is about to answer them.
    turn_in_progress = threading.Event()

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

    register_handlers(bus, conversation, tts, departure_grace_s=config.PRIVACY_DEPARTURE_GRACE_S)

    chat_bridge = ChatBridgeAdapter(
        bus, conversation, store, turn_queue,
        radar=radar,
        turntable=turntable,
        servo_calibration_path=config.SERVO_CALIBRATION_PATH,
        llm=llm,
        tts=tts,
        stt=stt,
        app_settings_path=config.APP_SETTINGS_PATH,
        voices=app_settings["voices"],
        active_voice_id=app_settings["active_voice_id"],
        calibration_mode=calibration_mode,
        study_dir=config.STUDY_DIR,
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

        doa = RespeakerDOAAdapter(
            front_reference_degrees=config.DOA_FRONT_REFERENCE_DEGREES,
            offaxis_gain=config.DOA_OFFAXIS_GAIN,
            calibration_path=config.DOA_CALIBRATION_PATH,
        )
        doa_lock = threading.Lock()

        assistant_speaking = threading.Event()
        bus.subscribe(SpeechPlaybackStarted, lambda e: assistant_speaking.set())
        bus.subscribe(SpeechPlaybackEnded, lambda e: assistant_speaking.clear())

        # The bridge is built before the array exists, so the guided
        # calibration gets its handles here.
        chat_bridge.doa = doa
        chat_bridge.assistant_speaking = assistant_speaking

        start_doa_tracking(
            doa, turntable, face, lock=doa_lock,
            assistant_speaking=assistant_speaking, calibration_mode=calibration_mode,
            servo_recentering=servo_recentering, turn_in_progress=turn_in_progress,
        )
        threading.Thread(
            target=_restart_on_error(vad_input_loop, "[VAD] input loop"),
            args=(bus, turn_queue, stt, doa, doa_lock, mic_lock, assistant_speaking),
            kwargs={
                "calibration_mode": calibration_mode, "composing_mode": composing_mode,
                "servo_recentering": servo_recentering, "turn_in_progress": turn_in_progress,
            },
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
            turn_in_progress.set()
            try:
                handle_turn(text, bus, conversation, store, llm, tts, cancel_current_turn,
                            source="chat" if _priority == PRIORITY_CHAT else "voice")
            except Exception:
                # One failed turn must not end the loop. Everything else
                # (radar, DOA tracking, the web app) runs on daemon threads,
                # so losing this thread used to take the whole process down
                # with it - or, worse, leave the unit visibly alive and
                # tracking while silently never answering again.
                logger.exception("Turn failed - the assistant keeps running.")
            finally:
                turn_in_progress.clear()
        except KeyboardInterrupt:
            print("\nBye!")
            stt.stop()
            if hasattr(turntable, "stop"):
                turntable.stop()
            break


if __name__ == "__main__":
    main()
