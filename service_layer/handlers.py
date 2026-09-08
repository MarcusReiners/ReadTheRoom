import contextlib
import logging
import threading
import time

from domain.conversation import ConversationState
from domain.events import (
    ListeningStateChanged,
    ModalitySwitched,
    PersonCountChanged,
    SpeechPlaybackEnded,
    SpeechPlaybackStarted,
)
from service_layer.bus import EventBus
from adapters.tts.base import StreamingTTSAdapter

logger = logging.getLogger(__name__)

# Tie LED strip - white only, states are distinguished by animation speed
# instead of color: idle breathes slowly, listening breathes faster, a
# spoken answer is a steady solid white.
TIE_LED_WHITE = (255, 255, 255)
TIE_LED_IDLE_PERIOD_MS = 3000
TIE_LED_LISTENING_PERIOD_MS = 900


def register_handlers(
    bus: EventBus,
    conversation: ConversationState,
    tts: StreamingTTSAdapter,
) -> None:
    def on_person_count_changed(event: PersonCountChanged) -> None:
        if event.count > 1 and conversation.confidential and conversation.modality == "voice":
            conversation.switch_modality("web")
            bus.publish(ModalitySwitched(to_modality="web", reason="person_entered"))
            logger.info("Modality switched to 'web' (PersonCount=%s)", event.count)
            if tts.request_takeover():
                logger.info("Speech interrupted (a person entered the room).")
        elif event.count <= 1 and conversation.modality == "web":
            conversation.switch_modality("voice")
            bus.publish(ModalitySwitched(to_modality="voice", reason="alone_again"))
            logger.info("Modalitätswechsel zurück zu 'voice' (wieder allein)")

    bus.subscribe(PersonCountChanged, on_person_count_changed)


def _tie_led_command(mode: str, period_ms: int | None = None) -> dict:
    r, g, b = TIE_LED_WHITE
    cmd = {"cmd": "set_led", "mode": mode, "r": r, "g": g, "b": b}
    if period_ms is not None:
        cmd["period_ms"] = period_ms
    return cmd


def register_tie_led_handlers(bus: EventBus, radar) -> None:
    """Drives the 7-LED tie strip via the same serial command channel the
    radar adapter already has open to the bridge ESP32 (see
    mmWaveBridge/src/main.cpp's set_led command) - always white, states
    are distinguished by animation speed/mode rather than color.
    """

    def on_listening_changed(event: ListeningStateChanged) -> None:
        if event.listening:
            radar.send_command(_tie_led_command("pulse", TIE_LED_LISTENING_PERIOD_MS))
        else:
            radar.send_command(_tie_led_command("pulse", TIE_LED_IDLE_PERIOD_MS))

    def on_speech_started(event: SpeechPlaybackStarted) -> None:
        radar.send_command(_tie_led_command("solid"))

    def on_speech_ended(event: SpeechPlaybackEnded) -> None:
        radar.send_command(_tie_led_command("pulse", TIE_LED_IDLE_PERIOD_MS))

    bus.subscribe(ListeningStateChanged, on_listening_changed)
    bus.subscribe(SpeechPlaybackStarted, on_speech_started)
    bus.subscribe(SpeechPlaybackEnded, on_speech_ended)

    radar.send_command(_tie_led_command("pulse", TIE_LED_IDLE_PERIOD_MS))


def start_doa_tracking(
    doa, turntable, face, poll_interval_s: float = 0.3, lock=None, assistant_speaking=None,
    settle_base_s: float = 0.15, settle_deg_per_s: float = 200.0, eye_lead_s: float = 0.15,
    silence_timeout_s: float = 5.0, calibration_mode=None, doa_samples: int = 5,
    post_move_quiet_s: float = 0.6, servo_recentering=None, min_doa_samples: int = 2,
    accept_range=None,
) -> None:
    """Continuously polls the ReSpeaker's onboard DOA/VAD on a background
    thread for the lifetime of the process, turning the head/eyes toward
    detected speech - same logic as scripts/test_hardware.py's
    live_doa_tracking().

    lock serializes USB control-transfer access to the ReSpeaker with any
    other thread reading the same device concurrently (see main.py's
    vad_input_loop) - pass None if this is the only consumer.

    assistant_speaking (a threading.Event, set for the duration of
    SpeechPlaybackStarted..SpeechPlaybackEnded) skips reacting entirely while
    TTS is playing - confirmed on the bench this isn't just cosmetic: without
    it, the head swings wildly tracking its own voice mid-sentence, and the
    resulting servo motor noise is itself loud enough to trip the VAD and
    trigger a bogus recording, which is picked up by main.py's
    vad_input_loop as if a person had spoken - a real feedback loop, not a
    hypothetical one. Pass None to disable the guard (not recommended for
    normal use).

    settle_base_s/settle_deg_per_s: size the RAMP - how long the glide to the
    target is spread over, as settle_base_s + moved_degrees /
    settle_deg_per_s. settle_deg_per_s is a conservative estimate of this
    servo's turning speed, not a measured spec - tune down if motion looks
    rushed on large turns, or up if small ones feel sluggish.

    post_move_quiet_s: how long DOA is ignored entirely (not even polled)
    AFTER a move has physically finished. This is separate from the ramp on
    purpose. rotate_towards()/home() block this thread for the whole ramp, so
    a deadline set before calling them expires the instant the motion ends -
    which is exactly when the servo is still audibly settling. The result was
    a feedback loop on the silence recenter: the head returns home, its own
    motor noise trips the VAD, the head turns toward its own noise, goes
    quiet, recenters again, forever. Starting the window after the move gives
    the motor noise time to decay before the mic is trusted again.

    servo_recentering: a threading.Event set only while the head is driving
    back to home (and through its quiet window), so main.py's vad_input_loop
    can ignore the motor noise that move makes.

    Deliberately NOT set for tracking moves, which is the whole subtlety
    here. The head turns toward a speaker BECAUSE they are speaking, so a
    flag covering every move is raised exactly when real speech is happening
    - an earlier version gated the recorder on that and killed the recording
    of every utterance that made the head turn ("Aufnahme verworfen (Servo
    bewegt sich)"), or stopped one from ever opening. A recenter is the
    opposite case: it fires after silence_timeout_s of nobody talking, so
    anything the mic hears during it really is the servo.

    eye_lead_s: on a detected direction, the eyes dart to it immediately and
    the head follows eye_lead_s later (a person's eyes move before their head
    does), then drift back to center over the servo's own settle window via
    face.animate_eye_direction() - by the time the head physically arrives,
    eyes and head are aligned again instead of the eyes staying cocked to the
    side.

    silence_timeout_s: if no voice activity has been picked up for this long,
    the head drifts back to home on its own - it shouldn't stay cocked toward
    the last speaker indefinitely once the room's gone quiet. The clock
    resets on every detected voice activity AND on assistant_speaking
    clearing (a reply just ended) - not while assistant_speaking is set,
    since that time is spent replying, not sitting in silence. Set to None or
    <=0 to disable.

    calibration_mode: a threading.Event, set for as long as the web app's
    settings tab is open (see ChatBridgeAdapter). The settings tab lets a
    person nudge the servo's raw angle directly for home calibration - this
    loop kept fighting those nudges with its own live DOA-driven moves
    (confirmed: calibrating from the web app "didn't really work" because of
    exactly this), so tracking fully pauses for as long as it's set. On
    entering AND leaving, the head is driven home once - entering, so it's
    out of the way and in a known position for calibration; leaving, because
    set_raw_angle() (used by the calibration nudges) doesn't update
    current_heading_degrees, so it would otherwise be stale once tracking
    resumes. LedEyesAdapter checks the same Event directly to show a wrench
    instead of the eyes - this loop doesn't need to touch the face for that.
    """
    # Imported here rather than at module scope: doa_respeaker pulls in
    # usb.core, and handlers.py is imported unconditionally by main.py even
    # when USE_SERVO is off (or on a dev machine with no pyusb at all).
    from adapters.hardware.doa_respeaker import median_angle

    lock = lock or contextlib.nullcontext()
    moving_until = 0.0

    def _tracking_loop() -> None:
        nonlocal moving_until
        logger.info("[DOA] Tracking started.")
        last_active_time = time.monotonic()
        was_speaking = False
        was_calibrating = False
        at_home = True
        while True:
            try:
                speaking = assistant_speaking is not None and assistant_speaking.is_set()
                calibrating = calibration_mode is not None and calibration_mode.is_set()

                if calibrating != was_calibrating:
                    logger.info(
                        "[DOA] Calibration mode %s - head moving to home.",
                        "active" if calibrating else "ended",
                    )
                    settle_s = settle_base_s + turntable.predict_home_move() / settle_deg_per_s
                    moving_until = time.monotonic() + settle_s
                    if servo_recentering is not None:
                        servo_recentering.set()
                    turntable.home(ramp_duration_s=settle_s)
                    at_home = True
                    last_active_time = time.monotonic()
                was_calibrating = calibrating
                settling = time.monotonic() < moving_until
                # Mirror the settle gate onto the shared flag so the recorder
                # thread ignores exactly the window this loop already does.
                if servo_recentering is not None and not settling:
                    servo_recentering.clear()

                if was_speaking and not speaking:
                    # A reply just finished - start the silence clock fresh
                    # from here rather than from before the reply, so a long
                    # reply doesn't count against the user as silence.
                    last_active_time = time.monotonic()
                was_speaking = speaking

                if not speaking and not settling and not calibrating:
                    with lock:
                        active = doa.get_voice_active()
                        angle = doa.get_direction_degrees() if active else None
                    logger.debug("[DOA] voice_active=%s", active)
                    if active:
                        last_active_time = time.monotonic()
                        at_home = False
                        logger.debug("[DOA] First reading %.0f deg.", angle)
                        # Eyes dart to the sound first, human-like, before the
                        # head starts physically catching up.
                        face.set_eye_direction(angle_degrees=angle)

                        # That eye-lead pause used to be a plain sleep. It's
                        # now spent collecting more DOA samples instead, which
                        # costs no extra latency and is a large accuracy win:
                        # the single reading taken the instant VAD trips is
                        # the worst one of the whole utterance - it lands on
                        # the speech onset (a plosive/breath, often a poor
                        # bearing estimate) and gets no chance to be checked
                        # against a second opinion. A median over the window
                        # both averages down per-sample noise and discards the
                        # occasional wall-reflection outlier.
                        samples = [angle]
                        for _ in range(max(0, doa_samples - 1)):
                            time.sleep(eye_lead_s / max(1, doa_samples - 1))
                            with lock:
                                if not doa.get_voice_active():
                                    break
                                samples.append(doa.get_direction_degrees())
                        if len(samples) < min_doa_samples:
                            # VAD dropped again almost immediately: a knock, a
                            # chair, the servo's own settling - not speech. The
                            # single reading taken on such a blip is meaningless
                            # (the array reports a stale/defaulted bearing) and
                            # used to be enough to swing the head.
                            logger.info(
                                "[DOA] Only %d reading(s) - too short to be speech, ignored.",
                                len(samples),
                            )
                            time.sleep(poll_interval_s)
                            continue
                        angle = median_angle(samples)
                        if accept_range is not None and not (accept_range[0] <= angle <= accept_range[1]):
                            # Physically impossible for this mount: the head
                            # cannot turn there, so the target would only clamp
                            # to an end stop and park the head off-axis.
                            logger.info(
                                "[DOA] %.0f deg is outside %s - ignored.",
                                angle, accept_range,
                            )
                            time.sleep(poll_interval_s)
                            continue
                        logger.info(
                            "[DOA] Voice detected at %.0f deg (median of %d readings).",
                            angle, len(samples),
                        )

                        moved = turntable.predict_relative_move(angle)
                        if moved > 0.5:
                            ramp_s = settle_base_s + moved / settle_deg_per_s
                            # Eyes drift back to center over the same span the
                            # head takes to physically arrive, so both land
                            # aligned together instead of the eyes staying
                            # cocked to the side after the head catches up.
                            face.animate_eye_direction(90.0, duration_s=ramp_s)
                            # Glides there over ramp_s instead of jumping in
                            # one PWM update - an instant snap looks abrupt,
                            # this reads as natural head motion. BLOCKS this
                            # thread until the motion is done, which is why
                            # the quiet window below is started afterwards.
                            turntable.rotate_towards(target_angle_degrees=angle, ramp_duration_s=ramp_s)
                        else:
                            turntable.rotate_towards(target_angle_degrees=angle)
                            face.set_eye_direction(angle_degrees=90.0)
                        moving_until = time.monotonic() + post_move_quiet_s
                    elif (
                        not at_home
                        and silence_timeout_s
                        and silence_timeout_s > 0
                        and time.monotonic() - last_active_time >= silence_timeout_s
                    ):
                        logger.info("[DOA] %.0fs of silence - head returning to home.", silence_timeout_s)
                        ramp_s = settle_base_s + turntable.predict_home_move() / settle_deg_per_s
                        if servo_recentering is not None:
                            servo_recentering.set()
                        face.animate_eye_direction(90.0, duration_s=ramp_s)
                        turntable.home(ramp_duration_s=ramp_s)
                        at_home = True
                        moving_until = time.monotonic() + post_move_quiet_s
                        # Not counted as voice activity: otherwise the servo's
                        # own noise during this recenter would keep resetting
                        # the silence clock and the head would never settle.
                        last_active_time = time.monotonic()
            except Exception:
                logger.exception("[DOA] Error while reading or driving - the tracking thread does NOT exit.")
            time.sleep(poll_interval_s)

    threading.Thread(target=_tracking_loop, daemon=True).start()
