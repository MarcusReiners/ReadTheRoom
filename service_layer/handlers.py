import contextlib
import logging
import threading
import time

from domain.aim import IdleGaze, fuse_bearing, glance_bearing
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


def handle_head_tap(bus: EventBus, conversation, buzz, addons=None) -> str:
    """One tap on the head. A local add-on gets it first (see
    LocalAddons.on_tap). After a visit - answers still going to the chat - it
    hands speech back, like the web app's Resume speaking button. Otherwise it
    switches private mode. The motor says which: one long pulse for resuming,
    one short pulse for private mode on, two for off, a short one for an
    add-on. Returns what the tap did, for the log."""
    if addons is not None and addons.on_tap():
        buzz(0.15)
        done = "handled by an add-on"
    elif conversation.modality == "web":
        bus.publish(ResumeSpeechRequested(source="touch"))
        buzz(0.4)
        done = "speech resumed"
    else:
        enabled = not conversation.confidential
        conversation.set_private_mode(enabled)
        bus.publish(PrivateModeChanged(enabled=enabled))
        if enabled:
            buzz(0.15)
        else:
            buzz(0.12, 0.12)
        done = f"private mode {'on' if enabled else 'off'}"
    logger.info("[Touch] Tap on the head - %s.", done)
    return done


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
    post_move_quiet_s: float = 1.5, servo_recentering=None, min_doa_samples: int = 2,
    accept_range=None, paused=None, sample_window_s: float = 3.0,
    doa_sample_interval_s=0.25, doa_onset_skip_s: float = 0.75, turn_in_progress=None,
    doa_quiet_give_up_s: float = 0.75, doa_agreement_degrees: float = 25.0,
    doa_astern_band_degrees: float = 5.0, radar_targets=None, radar_agree_weight: float = 0.25,
    radar_agree_degrees: float = 10.0, radar_gate_degrees: float = 45.0,
    radar_near_mm: float = 2500.0, aim_bias=None, idle_gaze=None,
    idle_gaze_glide_s: float = 0.45, glance_hold_s: float = 1.5,
    glance_glide_s: float = 0.25, recenter_request=None,
    require_speech_flag: bool = True, speech_share: float = 0.3,
) -> None:
    """Continuously polls the ReSpeaker's onboard DOA/VAD on a background
    thread for the lifetime of the process, turning the head/eyes toward
    detected speech - same logic as scripts/test_hardware.py's
    live_doa_tracking().

    The sampling defaults follow Study 1's settle-first configuration: ignore
    what the array says at the very onset of speech, then take readings 250ms
    apart (at least three, up to doa_samples), and stay deaf for 1.5s after a
    move. The head waits for the array's estimate to settle and turns once,
    instead of turning on the onset readings and again while the person is
    still talking - fewer moves mid-utterance means less servo noise in the
    recording. The earlier defaults sampled from the onset (37.5ms apart within
    0.6s, no skip); on the bench that made a coarse first move and, once,
    turned the head away from the talker.

    doa_onset_skip_s is 0.75 rather than the 1.5 the study ran with. The
    recorded samples of 14 September 2026 put the lock-on time at well under
    that: a single reading taken in the first 0.25s of speech is off by 11
    degrees at the median and by 45 at the ninth decile, while from 0.75s on
    the median is 2 degrees and the ninth decile 8 - no better at 1.5s. The
    whole second of waiting bought nothing and cost every utterance shorter
    than about two seconds, which was dropped for want of readings.

    doa_quiet_give_up_s ends a sample window early once the array has
    reported no voice for that long. Without it the window ran to
    sample_window_s whatever happened, and the samples in it did not have to
    belong to the sound that opened it: on 22 September a 0.8s blip opened a
    window at 18:42:00.9, the real question that followed at 18:42:07.4 went
    entirely unheard because the loop was still inside that window, and the
    bearing it finally acted on was built from a book closing at 18:42:16.

    doa_agreement_degrees is how far a single reading may sit from the median
    before it is treated as a reflection rather than the talker - see
    settled_angle() in the DOA adapter.

    radar_targets: a callable returning the people the radar can currently
    see, as (bearing, distance_mm) in the head's own frame - see
    adapters/radar_alignment.py. Given one, the sound's bearing is fused with
    it rather than used raw: domain/aim.py has the rule and the reasoning,
    the short version being that the sound leads while the two agree to
    radar_agree_degrees and the radar takes over as they diverge. None falls
    back to the microphone array alone, which is also what happens whenever
    the radar sees nobody - a motionless person at the desk is invisible to
    it.

    aim_bias: a callable returning the operator's own correction in degrees,
    added to every bearing. Set from the web app's Aim card, for the case the
    head lands off the talker the same way every time.

    doa_astern_band_degrees covers only the knife edge at dead astern, where
    a degree of jitter reverses the turn and the servo has no basis to choose
    a side - a property of a head that cannot spin, not of the array. It is
    deliberately NOT wide: with the servo defining the angles, a talker
    directly behind the head was placed to within 0 degrees over 12 seconds
    on 25 October 2026, so the rear as such is usable. The same run also
    showed the failure a band cannot catch - at 120 degrees round, the array
    reported 63 degrees the other way, stably and well inside the sweep,
    after being correct for the first second.

    min_doa_samples is two, not three. A question of ordinary length leaves
    very little behind the onset skip: "Hey, how are you feeling?" was 1.9s
    of recording on 25 October but only 1.1s of reported voice activity, so
    at 250ms spacing it produced exactly two settled readings and a minimum
    of three threw the whole utterance away. Two is still real evidence
    rather than a single blip - settled_angle() requires both of them to
    agree, a transient shorter than the onset skip produces none at all, and
    a reading taken this late is off by 2 degrees at the median.

    accept_range: (min, max) bearings to accept, or a callable returning
    that pair. Estimates outside it are ignored. The deployed assistant
    passes None: a talker off to the side, or behind the head, should still
    get the head turning as far that way as the sweep allows, which is what
    the servo's linear clamp does on its own. Study 1's harness sets it, so
    that a trial the head cannot face is a recorded non-reaction rather than
    a bearing pinned to an end stop.

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
    from adapters.hardware.doa_respeaker import DOAUnavailable, median_angle, settled_angle

    lock = lock or contextlib.nullcontext()
    moving_until = 0.0
    glance_until = 0.0
    if aim_bias is None:
        aim_bias = lambda: 0.0

    def _tracking_loop() -> None:
        nonlocal moving_until, glance_until
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
                if (recenter_request is not None and recenter_request.is_set()
                        and not speaking and not calibrating):
                    # Something that turned the head has since been judged not
                    # to be speech - by the recogniser, which is the only
                    # thing here that can actually tell. It says so a second
                    # or two late, far too late to gate the turn, but not too
                    # late to take it back.
                    recenter_request.clear()
                    if not at_home:
                        logger.info("[DOA] It was not speech after all - head returning to home.")
                        settle_s = settle_base_s + turntable.predict_home_move() / settle_deg_per_s
                        moving_until = time.monotonic() + settle_s + post_move_quiet_s
                        if servo_recentering is not None:
                            servo_recentering.set()
                        face.animate_eye_direction(90.0, duration_s=settle_s)
                        turntable.home(ramp_duration_s=settle_s)
                        at_home = True
                        last_active_time = time.monotonic()

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

                        # The single reading taken the instant VAD trips is the
                        # worst of the whole utterance: it lands on the speech
                        # onset, before the array has locked on, and is
                        # routinely impossible (a rear bearing for a source in
                        # front). A median over several later ones averages
                        # down per-sample noise and survives a wall reflection.
                        # Spacing is deliberately NOT tied to eye_lead_s (an
                        # animation timing) - samples closer together than the
                        # array's own update period are the same estimate read
                        # twice, and a median over duplicates averages nothing
                        # away. A dropout mid-utterance is normal, since the
                        # VAD flutters between phonemes, so quiet polls are
                        # skipped rather than ending the window - but only up
                        # to doa_quiet_give_up_s of them, or the window stops
                        # belonging to the sound that opened it.
                        step = (doa_sample_interval_s if doa_sample_interval_s
                                else eye_lead_s / max(1, doa_samples - 1))
                        # Every reading is kept with its age, so the ones from
                        # before the array had locked on can be left out of
                        # the median without a blind sleep over them - a sleep
                        # cannot tell a talker who stopped from one who is
                        # still going.
                        started_sampling = time.monotonic()
                        readings = [(0.0, angle)]
                        sample_deadline = started_sampling + doa_onset_skip_s + sample_window_s
                        quiet_since = None
                        interrupted = ""
                        speech_polls = 0
                        while time.monotonic() < sample_deadline:
                            settled = [a for age, a in readings if age >= doa_onset_skip_s]
                            if len(settled) >= doa_samples:
                                break
                            time.sleep(step)
                            if ((paused is not None and paused.is_set())
                                    or (assistant_speaking is not None and assistant_speaking.is_set())
                                    or (calibration_mode is not None and calibration_mode.is_set())):
                                interrupted = "tracking was paused while it was sampled"
                                break
                            with lock:
                                if doa.get_voice_active():
                                    heard = doa.get_direction_degrees()
                                    speechy = (doa.get_speech_detected() if require_speech_flag
                                               else True)
                                else:
                                    heard, speechy = None, False
                            now = time.monotonic()
                            if heard is None:
                                if quiet_since is None:
                                    quiet_since = now
                                elif now - quiet_since >= doa_quiet_give_up_s:
                                    break
                                continue
                            quiet_since = None
                            last_active_time = now
                            readings.append((now - started_sampling, heard))
                            speech_polls += int(speechy)
                        settled = [a for age, a in readings if age >= doa_onset_skip_s]
                        if interrupted:
                            logger.info("[DOA] Estimate dropped - %s.", interrupted)
                            face.set_eye_direction(angle_degrees=90.0)
                            time.sleep(poll_interval_s)
                            continue
                        # Over the sampled polls, not the readings: the one
                        # that opened the window was taken before the speech
                        # flag was ever read.
                        sampled = max(1, len(readings) - 1)
                        was_speech = (not require_speech_flag
                                      or speech_polls >= max(1, round(speech_share * sampled)))
                        if len(settled) < min_doa_samples or not was_speech:
                            # Two ways a sound does not earn a head turn. Too
                            # short to place: a knock, a chair, the servo's own
                            # settling, where the readings are far too poor to
                            # turn on. Or the array's speech flag stayed quiet
                            # through it, which on 25 October 2026 measured 0%
                            # of polls for footsteps, 4% for clapping and 6%
                            # for objects, against 65-73% for speech - the
                            # flag everything else is gated on reports 24-59%
                            # for those same noises, which is why the head used
                            # to follow them. Either way the eyes still glance:
                            # the readings are good enough for that.
                            if not was_speech:
                                logger.info(
                                    "[DOA] Not speech - the speech flag was up in %d of %d poll(s), "
                                    "so the head stays put.", speech_polls, sampled,
                                )
                            else:
                                logger.info(
                                    "[DOA] Only %d reading(s) once the array had settled (%.1f s of "
                                    "voice) - too short to place, ignored.",
                                    len(settled), readings[-1][0],
                                )
                            rough = [a for _, a in readings
                                     if abs((a - 90.0 + 180.0) % 360.0 - 180.0)
                                     < 180.0 - doa_astern_band_degrees]
                            if glance_hold_s > 0 and rough:
                                heading = getattr(turntable, "current_heading_degrees", 90.0)
                                nearby = ([(bearing - heading + 90.0, distance)
                                           for bearing, distance in radar_targets()]
                                          if radar_targets is not None else [])
                                look, why = glance_bearing(median_angle(rough), nearby,
                                                           near_mm=radar_near_mm)
                                logger.info("[Aim] Glanced at %s.", why)
                                face.animate_eye_direction(look, duration_s=glance_glide_s)
                                if idle_gaze is not None:
                                    idle_gaze.note_eyes(look)
                                glance_until = time.monotonic() + glance_hold_s
                            else:
                                face.set_eye_direction(angle_degrees=90.0)
                            time.sleep(poll_interval_s)
                            continue
                        angle, kept, no_bearing = settled_angle(
                            settled, doa_agreement_degrees, doa_astern_band_degrees)
                        if angle is None:
                            logger.info(
                                "[DOA] Readings ignored - %s (%s).",
                                no_bearing, " ".join(f"{s:.0f}" for s in settled),
                            )
                            face.set_eye_direction(angle_degrees=90.0)
                            time.sleep(poll_interval_s)
                            continue
                        if len(kept) < len(settled):
                            logger.info(
                                "[DOA] %d of %d reading(s) discarded as reflections, no-lock or behind the head (%s).",
                                len(settled) - len(kept), len(settled),
                                " ".join(f"{s:.0f}" for s in settled),
                            )
                        samples = kept
                        if radar_targets is not None:
                            # The sound says who is talking, the radar says
                            # where the people are - see domain/aim.py for why
                            # the radar is not simply averaged in.
                            #
                            # The two arrive in different frames and have to be
                            # put in the same one first. A radar bearing is
                            # absolute: it is measured from the head's home
                            # direction and does not change when the head
                            # turns. The array's bearing is relative to
                            # wherever the nose points right now. Fusing them
                            # raw made the head drift further every round -
                            # once it had turned to 135, a talker it was
                            # already facing read as 90 from the array and
                            # still 135 from the radar, and the "disagreement"
                            # moved it another 34 degrees.
                            heading = getattr(turntable, "current_heading_degrees", 90.0)
                            relative = [(90.0 + (bearing - heading), distance)
                                        for bearing, distance in radar_targets()]
                            fused, why = fuse_bearing(
                                angle, relative,
                                agree_weight=radar_agree_weight,
                                agree_degrees=radar_agree_degrees,
                                gate_degrees=radar_gate_degrees,
                                near_mm=radar_near_mm, bias_degrees=aim_bias(),
                            )
                            logger.info("[Aim] %s.", why)
                            if fused is None:
                                face.set_eye_direction(angle_degrees=90.0)
                                time.sleep(poll_interval_s)
                                continue
                            angle = fused
                        elif aim_bias() != 0.0:
                            angle += aim_bias()
                        bounds = accept_range() if callable(accept_range) else accept_range
                        if bounds is not None and not (bounds[0] <= angle <= bounds[1]):
                            # Physically impossible for this mount: the head
                            # cannot turn there, so the target would only clamp
                            # to an end stop and park the head off-axis.
                            logger.info(
                                "[DOA] %.0f deg is outside %s - ignored.",
                                angle, bounds,
                            )
                            time.sleep(poll_interval_s)
                            continue
                        logger.info(
                            "[DOA] Voice detected at %.0f deg (median of %d readings: %s; head at %.0f deg).",
                            angle, len(samples), " ".join(f"{s:.0f}" for s in samples),
                            getattr(turntable, "current_heading_degrees", float("nan")),
                        )
                        if (
                            (paused is not None and paused.is_set())
                            or (assistant_speaking is not None and assistant_speaking.is_set())
                            or (calibration_mode is not None and calibration_mode.is_set())
                        ):
                            logger.info("[DOA] Estimate dropped - tracking was paused while it was sampled.")
                            face.set_eye_direction(angle_degrees=90.0)
                            time.sleep(poll_interval_s)
                            continue

                        # Whatever the sweep cannot deliver stays with the
                        # eyes: a talker further round than the head can turn
                        # leaves the head at its end stop, and the eyes hold
                        # the leftover angle instead of drifting back to
                        # centre as if the head had arrived. 90 means centred,
                        # so an exactly reached target leaves them centred.
                        delivered = turntable.predict_relative_turn(angle)
                        moved = abs(delivered)
                        residual = angle - delivered
                        if moved > 0.5:
                            ramp_s = settle_base_s + moved / settle_deg_per_s
                            # Eyes travel to where the head will not reach
                            # over the same span the head takes to physically
                            # arrive, so both land together instead of the
                            # eyes snapping somewhere before it moves.
                            face.animate_eye_direction(residual, duration_s=ramp_s)
                            # Glides there over ramp_s instead of jumping in
                            # one PWM update - an instant snap looks abrupt,
                            # this reads as natural head motion. BLOCKS this
                            # thread until the motion is done, which is why
                            # the quiet window below is started afterwards.
                            turntable.rotate_towards(target_angle_degrees=angle, ramp_duration_s=ramp_s)
                        else:
                            turntable.rotate_towards(target_angle_degrees=angle)
                            face.set_eye_direction(angle_degrees=residual)
                        moving_until = time.monotonic() + post_move_quiet_s
                    if (not active and idle_gaze is not None and radar_targets is not None
                            and time.monotonic() >= glance_until):
                        # Between utterances the eyes follow whoever the radar
                        # can see. Eyes only: a servo that tracks a body
                        # around the room is restless, noisy in the recording,
                        # and keeps moving the microphone array for nothing.
                        heading = getattr(turntable, "current_heading_degrees", 90.0)
                        look = idle_gaze.update([(90.0 + (bearing - heading), distance)
                                                 for bearing, distance in radar_targets()])
                        if look is not None:
                            face.animate_eye_direction(look, duration_s=idle_gaze_glide_s)

                    if (
                        not at_home
                        and silence_timeout_s
                        and silence_timeout_s > 0
                        and time.monotonic() - last_active_time >= silence_timeout_s
                        and not active
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
