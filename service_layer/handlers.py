import contextlib
import logging
import threading
import time

from domain.conversation import ConversationState
from domain.events import (
    DoorCleared,
    ListeningStateChanged,
    ModalitySwitched,
    PersonAtDoor,
    PersonEnteredRoom,
    PersonLeftRoom,
    PrivateModeChanged,
    ResumeSpeechRequested,
    RoomClearChanged,
    SpeechPlaybackEnded,
    SpeechPlaybackStarted,
    SpeechTranscribed,
    VoiceDucked,
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
TIE_LED_ROOM_CLEAR = (64, 64, 64)


class PrivacyGuard:
    """Moves spoken answers to the chat when someone new walks in.

    Decided by who crosses the zone boundary, not by how many people the radar
    currently sees - the LD2450 loses still, seated people, so a headcount
    both misses the user and, worse, hides a newcomer behind them.

    - Someone talking to the assistant is taken as proof that a conversation
      partner is present, tracked or not. If nobody was seen entering first,
      they had been there all along, and an open conversation is their choice.
    - Anyone crossing IN while a partner is present is a visitor: speech stops.
    - Anyone crossing IN with no conversation going is harmless - they become
      the partner if they start talking.
    - Someone crossing OUT while visitors are present is taken to be a visitor.
      Once the last one has left the room counts as clear (RoomClearChanged),
      but the answer stays in the chat until the user resumes speech - so an
      exit the radar gets wrong costs nothing anyone can hear.
    - Someone crossing OUT with no visitors may be the partner. If no
      conversation follows within departure_grace_s, the partner is assumed
      gone; until then, newcomers are still treated as visitors.
    - With a door zone: someone standing in it while a conversation runs
      makes the voice quieter at once - they may only be looking in, so it
      is not worth cutting the answer off. It returns to normal
      door_clear_hold_s after the door zone is empty; walking on in is an
      entry like any other.
    - Resuming by hand (ResumeSpeechRequested - the web app's button, later
      the head) or turning private mode off brings speech back, visitor or
      not, and forgets the visitor count: the user has judged who may hear.
    """

    def __init__(self, bus: EventBus, conversation: ConversationState, tts: StreamingTTSAdapter,
                 departure_grace_s: float = 60.0, door_clear_hold_s: float = 1.5) -> None:
        self.bus = bus
        self.conversation = conversation
        self.tts = tts
        self.departure_grace_s = departure_grace_s
        self.door_clear_hold_s = door_clear_hold_s
        self.partner_present = False
        self.visitors = 0
        self.room_clear = False
        self._departure_since: float | None = None
        self._at_door: set = set()
        self._ducked = False
        self._unduck_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        bus.subscribe(SpeechTranscribed, self._on_turn)
        bus.subscribe(PersonEnteredRoom, self._on_entered)
        bus.subscribe(PersonLeftRoom, self._on_left)
        bus.subscribe(PersonAtDoor, self._on_at_door)
        bus.subscribe(DoorCleared, self._on_door_cleared)
        bus.subscribe(PrivateModeChanged, self._on_private_mode)
        bus.subscribe(ResumeSpeechRequested, lambda e: self.resume_speech(f"resumed_{e.source}"))

    @property
    def people_at_door(self) -> int:
        return len(self._at_door)

    def resume_speech(self, reason: str) -> bool:
        with self._lock:
            self.visitors = 0
            self.room_clear = False
            back = self.conversation.modality == "web"
            if back:
                self.conversation.switch_modality("voice")
        if back:
            self.bus.publish(ModalitySwitched(to_modality="voice", reason=reason))
            logger.info("Modality switched back to 'voice' (%s)", reason)
        return back

    def reset_room(self) -> None:
        """Forgets visitors and anyone at the door, back to speech at normal
        volume. For the study harness only, once the tester confirms the room
        is empty after an exit the radar missed - otherwise one missed exit
        keeps every later trial waiting for a visitor who already left."""
        with self._lock:
            self._at_door.clear()
            self._cancel_unduck()
        self._set_duck(False, "reset by the tester")
        self.resume_speech("reset")

    @property
    def ducked(self) -> bool:
        return self._ducked

    def _set_duck(self, active: bool, reason: str) -> None:
        with self._lock:
            if active == self._ducked:
                return
            self._ducked = active
        set_duck = getattr(self.tts, "set_duck", None)
        if set_duck is not None:
            set_duck(active)
        self.bus.publish(VoiceDucked(active=active, reason=reason))
        logger.info("[Privacy] voice %s (%s).", "quieter" if active else "back to normal volume", reason)

    def _cancel_unduck(self) -> None:
        if self._unduck_timer is not None:
            self._unduck_timer.cancel()
            self._unduck_timer = None

    def _on_at_door(self, event: PersonAtDoor) -> None:
        with self._lock:
            self._expire_departure(time.monotonic())
            self._at_door.add(event.person_id)
            self._cancel_unduck()
            protect = (self.partner_present and self.conversation.confidential
                       and self.conversation.modality == "voice")
        if protect:
            self._set_duck(True, "someone at the door")
        else:
            logger.info("[Privacy] someone at the door, no conversation running - nothing to protect.")

    def _on_door_cleared(self, event: DoorCleared) -> None:
        with self._lock:
            self._at_door.discard(event.person_id)
            if self._at_door or not self._ducked:
                return
            self._cancel_unduck()
            self._unduck_timer = threading.Timer(self.door_clear_hold_s, self._unduck_if_clear)
            self._unduck_timer.daemon = True
            self._unduck_timer.start()

    def _unduck_if_clear(self) -> None:
        with self._lock:
            self._unduck_timer = None
            clear = not self._at_door
        if clear:
            self._set_duck(False, "door clear")

    def _expire_departure(self, now: float) -> None:
        if self._departure_since is not None and now - self._departure_since >= self.departure_grace_s:
            self._departure_since = None
            self.partner_present = False
            logger.info("[Privacy] nobody spoke since someone left - assuming the user left.")

    def _on_turn(self, event: SpeechTranscribed) -> None:
        if getattr(event, "source", "voice") != "voice":
            return
        with self._lock:
            self._expire_departure(time.monotonic())
            if not self.partner_present:
                logger.info("[Privacy] conversation partner present (someone spoke to the assistant).")
            self.partner_present = True
            self._departure_since = None

    def _on_entered(self, event: PersonEnteredRoom) -> None:
        with self._lock:
            self._expire_departure(time.monotonic())
            if not self.partner_present:
                logger.info("[Privacy] someone entered (%s), no conversation running - nothing to protect.",
                            event.via)
                return
            self.visitors += 1
            switch = self.conversation.confidential and self.conversation.modality == "voice"
            occupied_again = self.room_clear
            self.room_clear = False
            visitors = self.visitors
        logger.info("[Privacy] visitor entered (%s) - %d visitor(s) present.", event.via, visitors)
        if occupied_again:
            self.bus.publish(RoomClearChanged(clear=False))
        if switch:
            self.conversation.switch_modality("web")
            self.bus.publish(ModalitySwitched(to_modality="web", reason="person_entered"))
            logger.info("Modality switched to 'web' (someone entered during a conversation)")
            if self.tts.request_takeover():
                logger.info("Speech interrupted (a person entered the room).")

    def _on_left(self, event: PersonLeftRoom) -> None:
        clear = False
        with self._lock:
            now = time.monotonic()
            self._expire_departure(now)
            if self.visitors > 0:
                self.visitors -= 1
                clear = self.visitors == 0 and self.conversation.modality == "web" and not self.room_clear
                self.room_clear = self.room_clear or clear
                logger.info("[Privacy] visitor left - %d still present.", self.visitors)
            elif self.partner_present:
                self._departure_since = now
                logger.info("[Privacy] someone left with no visitors present - the user, unless "
                            "the conversation continues within %.0fs.", self.departure_grace_s)
        if clear:
            self.bus.publish(RoomClearChanged(clear=True))
            logger.info("[Privacy] room clear again - answers stay in the chat until the user resumes speech.")

    def _on_private_mode(self, event: PrivateModeChanged) -> None:
        if event.enabled:
            return
        with self._lock:
            self._cancel_unduck()
        self._set_duck(False, "private mode off")
        self.resume_speech("private_mode_off")


class VisitDiscretion:
    """Reads as set while an answer has been moved to the chat and the user
    has not resumed speech - including after the radar judges the room clear
    again, since that judgement is exactly what Study 2 found can be wrong.
    start_doa_tracking() and vad_input_loop() only ever call is_set(), so
    this stands in for a threading.Event.

    While it is set the head stops following voices - turning to whoever
    speaks would make the visitor the addressee - and no new recording is
    opened, so what the visitor says is neither transcribed nor answered.
    Resuming speech by hand clears it: the user has decided who may hear,
    and the assistant behaves normally again."""

    def __init__(self, conversation: ConversationState) -> None:
        self._conversation = conversation

    def is_set(self) -> bool:
        return self._conversation.modality == "web"


def register_handlers(
    bus: EventBus,
    conversation: ConversationState,
    tts: StreamingTTSAdapter,
    departure_grace_s: float = 60.0,
    door_clear_hold_s: float = 1.5,
) -> PrivacyGuard:
    return PrivacyGuard(bus, conversation, tts, departure_grace_s=departure_grace_s,
                        door_clear_hold_s=door_clear_hold_s)


def _tie_led_command(mode: str, period_ms: int | None = None, color: tuple = TIE_LED_WHITE) -> dict:
    r, g, b = color
    cmd = {"cmd": "set_led", "mode": mode, "r": r, "g": g, "b": b}
    if period_ms is not None:
        cmd["period_ms"] = period_ms
    return cmd


def register_tie_led_handlers(bus: EventBus, radar, face=None) -> None:
    """Drives the 7-LED tie strip via the same serial command channel the
    radar adapter already has open to the bridge ESP32 (see
    mmWaveBridge/src/main.cpp's set_led command) - always white, states
    are distinguished by animation speed/mode rather than color.

    A fourth state joins idle/listening/speaking: while output is redirected
    to the chat the strip goes dark and the eyes close. Dark is the one state
    the other three cannot be mistaken for, and closed eyes on a still-lit
    face say the assistant has withdrawn rather than crashed - the signal
    has to be readable without announcing that something is being hidden.
    Once the radar judges the room clear again the strip glows faintly while
    the eyes stay closed: the assistant is waiting to be told it may speak.
    """
    redirected = False
    room_clear = False

    def redirected_command() -> dict:
        return _tie_led_command("solid", color=TIE_LED_ROOM_CLEAR) if room_clear else _tie_led_command("off")

    def on_listening_changed(event: ListeningStateChanged) -> None:
        if redirected:
            return
        if event.listening:
            radar.send_command(_tie_led_command("pulse", TIE_LED_LISTENING_PERIOD_MS))
        else:
            radar.send_command(_tie_led_command("pulse", TIE_LED_IDLE_PERIOD_MS))

    def on_speech_started(event: SpeechPlaybackStarted) -> None:
        if redirected:
            return
        radar.send_command(_tie_led_command("solid"))

    def on_speech_ended(event: SpeechPlaybackEnded) -> None:
        # The switch kills playback, so this arrives just after a redirect -
        # without the guard it would immediately overwrite the dark strip.
        radar.send_command(redirected_command() if redirected
                           else _tie_led_command("pulse", TIE_LED_IDLE_PERIOD_MS))

    def on_modality_switched(event: ModalitySwitched) -> None:
        nonlocal redirected, room_clear
        redirected = event.to_modality == "web"
        room_clear = False
        close_eyes = getattr(face, "set_eyes_closed", None)
        if close_eyes is not None:
            close_eyes(redirected)
        radar.send_command(redirected_command() if redirected
                           else _tie_led_command("pulse", TIE_LED_IDLE_PERIOD_MS))

    def on_room_clear(event: RoomClearChanged) -> None:
        nonlocal room_clear
        if not redirected:
            return
        room_clear = event.clear
        radar.send_command(redirected_command())

    bus.subscribe(ListeningStateChanged, on_listening_changed)
    bus.subscribe(SpeechPlaybackStarted, on_speech_started)
    bus.subscribe(SpeechPlaybackEnded, on_speech_ended)
    bus.subscribe(ModalitySwitched, on_modality_switched)
    bus.subscribe(RoomClearChanged, on_room_clear)

    radar.send_command(_tie_led_command("pulse", TIE_LED_IDLE_PERIOD_MS))


def start_doa_tracking(
    doa, turntable, face, poll_interval_s: float = 0.3, lock=None, assistant_speaking=None,
    settle_base_s: float = 0.15, settle_deg_per_s: float = 200.0, eye_lead_s: float = 0.15,
    silence_timeout_s: float = 5.0, calibration_mode=None, doa_samples: int = 5,
    post_move_quiet_s: float = 0.6, servo_recentering=None, min_doa_samples: int = 2,
    accept_range=None, paused=None, sample_window_s: float = 0.6,
    doa_sample_interval_s=None, doa_onset_skip_s: float = 0.0, turn_in_progress=None,
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

    turn_in_progress: set while a turn is being answered - from the moment
    transcription starts until the reply finishes. The silence clock keeps
    running through STT and the LLM call, which is dead air of several
    seconds, so without this the head recenters just before it speaks -
    turning away from the person exactly as it answers them. assistant_speaking
    alone is not enough: it only covers playback, not the thinking before it.

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
    from adapters.hardware.doa_respeaker import DOAUnavailable, median_angle

    lock = lock or contextlib.nullcontext()
    moving_until = 0.0

    def _tracking_loop() -> None:
        nonlocal moving_until
        logger.info("[DOA] Tracking started.")
        last_active_time = time.monotonic()
        was_speaking = False
        was_calibrating = False
        at_home = True
        last_error_log = 0.0
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

                is_paused = paused is not None and paused.is_set()
                if not speaking and not settling and not calibrating and not is_paused:
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
                        # A dropout mid-utterance is normal - the VAD flutters
                        # between phonemes, and harder in noise. Bailing out on
                        # the first inactive poll (as this used to) yields one or
                        # two samples in exactly the conditions where a stable
                        # median matters most. Skip the quiet polls instead and
                        # keep gathering until either enough samples or the
                        # window runs out.
                        # Spacing is deliberately NOT tied to eye_lead_s (an
                        # animation timing) - samples closer together than the
                        # array's own DOA update period are the same estimate
                        # read twice, and a median over duplicates averages
                        # nothing away.
                        # The bearings reported in the first fraction of a
                        # second after VAD trips are produced before the array
                        # has locked on, and are routinely impossible (rear
                        # bearings for a source in front). Including them drags
                        # the median toward a direction nothing was ever in.
                        step = (doa_sample_interval_s if doa_sample_interval_s
                                else eye_lead_s / max(1, doa_samples - 1))
                        if doa_onset_skip_s > 0:
                            samples = []
                            time.sleep(doa_onset_skip_s)
                        else:
                            samples = [angle]
                        sample_deadline = time.monotonic() + sample_window_s
                        while len(samples) < doa_samples and time.monotonic() < sample_deadline:
                            time.sleep(step)
                            with lock:
                                if doa.get_voice_active():
                                    samples.append(doa.get_direction_degrees())
                        if not samples or len(samples) < min_doa_samples:
                            # VAD dropped again almost immediately: a knock, a
                            # chair, the servo's own settling - not speech. The
                            # single reading taken on such a blip is meaningless
                            # (the array reports a stale/defaulted bearing) and
                            # used to be enough to swing the head.
                            logger.info(
                                "[DOA] Only %d reading(s) after the onset window - "
                                "too short to be speech, ignored.", len(samples),
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
                            "[DOA] Voice detected at %.0f deg (median of %d readings: %s; head at %.0f deg).",
                            angle, len(samples), " ".join(f"{s:.0f}" for s in samples),
                            getattr(turntable, "current_heading_degrees", float("nan")),
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
                        and not (turn_in_progress is not None and turn_in_progress.is_set())
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
            except DOAUnavailable:
                time.sleep(1.0)
            except Exception:
                now = time.monotonic()
                if now - last_error_log >= 10.0:
                    last_error_log = now
                    logger.exception("[DOA] Error while reading or driving - the tracking thread does NOT exit.")
            time.sleep(poll_interval_s)

    threading.Thread(target=_tracking_loop, daemon=True).start()
