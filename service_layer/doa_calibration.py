import csv
import logging
import os
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

DEFAULT_ANGLES = [90.0, 135.0, 180.0, 45.0, 0.0]
COLUMNS = ["timestamp", "session", "true_angle", "distance_m", "rep",
           "reported_angle", "error", "n_samples", "samples"]

READY_TIMEOUT_S = 45.0
SPEECH_TIMEOUT_S = 25.0
ONSET_SKIP_S = 1.5
SAMPLE_INTERVAL_S = 0.25
SAMPLES = 7
MIN_SAMPLES = 3
TTS_TAIL_S = 0.6


class DOACalibration:
    """Voice-guided measurement of the array's angular response.

    The servo defines each angle: the head points where the speaker should
    stand, so no floor marks or protractors are needed. The head then returns
    home BEFORE the person talks - the array rides on the head, so a source in
    front of the head's own nose is always on-axis and would read 90 whatever
    the angle.
    """

    def __init__(self, doa, turntable, tts, assistant_speaking, calibration_mode,
                 study_dir, distance_m=1.25, angles=None, on_progress=None):
        self.doa = doa
        self.turntable = turntable
        self.tts = tts
        self.assistant_speaking = assistant_speaking
        self.calibration_mode = calibration_mode
        self.study_dir = study_dir
        self.distance_m = distance_m
        self.angles = list(angles or DEFAULT_ANGLES)
        self.on_progress = on_progress
        self._thread = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._state = {"running": False, "step": "idle", "angle": None,
                       "done": 0, "total": len(self.angles), "results": [], "message": ""}

    def status(self):
        with self._lock:
            return dict(self._state, results=list(self._state["results"]))

    def cancel(self):
        self._cancel.set()

    def start(self, distance_m=None, angles=None):
        with self._lock:
            if self._state["running"]:
                return False
        if distance_m is not None:
            self.distance_m = float(distance_m)
        if angles:
            self.angles = [float(a) for a in angles]
        self._cancel.clear()
        self._set(running=True, step="starting", done=0, total=len(self.angles),
                  results=[], angle=None, message="")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)
            snapshot = dict(self._state, results=list(self._state["results"]))
        if self.on_progress:
            try:
                self.on_progress(snapshot)
            except Exception:
                logger.exception("[DOACal] progress callback failed")

    def _say(self, text):
        """Speaks and waits until the audio has actually finished.

        Sampling may not start while the assistant is talking - the array
        would otherwise localise the head's own speaker.
        """
        self._set(message=text)
        try:
            self.tts.speak_stream([text])
        except Exception:
            logger.exception("[DOACal] TTS failed, continuing without speech")
        deadline = time.monotonic() + 20.0
        while self.assistant_speaking.is_set() and time.monotonic() < deadline:
            if self._cancel.is_set():
                return
            time.sleep(0.05)
        time.sleep(TTS_TAIL_S)

    def _wait_for_voice(self, timeout_s):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._cancel.is_set():
                return False
            if not self.assistant_speaking.is_set() and self.doa.get_voice_active():
                return True
            time.sleep(0.05)
        return False

    def _gather(self):
        from adapters.hardware.doa_respeaker import median_angle
        if not self._wait_for_voice(SPEECH_TIMEOUT_S):
            return None, []
        time.sleep(ONSET_SKIP_S)
        got = []
        deadline = time.monotonic() + SPEECH_TIMEOUT_S
        while len(got) < SAMPLES and time.monotonic() < deadline:
            if self._cancel.is_set():
                return None, got
            if not self.assistant_speaking.is_set() and self.doa.get_voice_active():
                got.append(self.doa.get_direction_degrees())
            time.sleep(SAMPLE_INTERVAL_S)
        if len(got) < MIN_SAMPLES:
            return None, got
        return median_angle(got), got

    def _run(self):
        session = datetime.now().strftime("%Y%m%d_%H%M")
        os.makedirs(self.study_dir, exist_ok=True)
        path = os.path.join(self.study_dir, f"doa_angle_response_{session}.csv")
        with open(path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=COLUMNS).writeheader()

        was_calibrating = self.calibration_mode.is_set()
        self.calibration_mode.set()
        try:
            self._say(f"Starting calibration. I will point at {len(self.angles)} positions. "
                      f"Each time, stand where I am looking, about {self.distance_m:.2g} "
                      "metres away, and say ready.")
            for index, angle in enumerate(self.angles, 1):
                if self._cancel.is_set():
                    break
                self._set(step="pointing", angle=angle, done=index - 1)
                self.turntable.set_doa_angle_immediate(angle)
                time.sleep(0.9)

                self._say(f"Position {index} of {len(self.angles)}. "
                          "Please stand where I am looking now, and say ready.")
                self._set(step="waiting_for_person")
                if not self._wait_for_voice(READY_TIMEOUT_S):
                    self._say("I did not hear you. Skipping this position.")
                    self._record(path, session, angle, index, None, [])
                    continue

                self._say("Thank you. Stay exactly there. I will look away, "
                          "and then please say a full sentence.")
                self._set(step="homing")
                self.turntable.home(ramp_duration_s=0.6)
                time.sleep(1.0)

                self._say("Please speak now.")
                self._set(step="listening")
                reported, samples = self._gather()
                self._record(path, session, angle, index, reported, samples)
                if reported is None:
                    self._say("I could not hear enough. Moving on.")
                else:
                    self._say("Got it.")
            if not self._cancel.is_set():
                self._say("Calibration finished. Thank you.")
        except Exception:
            logger.exception("[DOACal] calibration aborted")
            self._set(message="Calibration failed - see the log.")
        finally:
            try:
                self.turntable.home(ramp_duration_s=0.5)
            except Exception:
                logger.exception("[DOACal] could not home the head")
            if not was_calibrating:
                self.calibration_mode.clear()
            self._set(running=False, step="done", angle=None, csv_path=path)
            logger.info("[DOACal] results written to %s", path)

    def _record(self, path, session, angle, index, reported, samples):
        row = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "session": session, "true_angle": angle, "distance_m": self.distance_m,
            "rep": 1,
            "reported_angle": "" if reported is None else round(reported, 2),
            "error": "" if reported is None else round(reported - angle, 2),
            "n_samples": len(samples),
            "samples": " ".join(f"{v:.0f}" for v in samples),
        }
        with open(path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=COLUMNS).writerow(row)
        with self._lock:
            self._state["results"].append(
                {"angle": angle, "reported": row["reported_angle"], "error": row["error"]})
        self._set(done=index)
