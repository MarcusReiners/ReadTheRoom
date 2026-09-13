# ReadTheRoom — Handoff for the Pre-Submission Sprint

Written 13 September 2026 at the end of a thesis review session. Read all of it before doing anything.

## Context

This repository is the prototype and study tooling for Marcus Reiners' master's thesis (LMU Munich, MCI), **due 29 September 2026**. The thesis itself is a separate LaTeX project at:

```
/Users/marcus/Desktop/ReadTheRoom/69f21301775b4ed8e68ee708
  content.tex               all chapters
  appendix-quotations.tex   German originals of focus-group quotations
  main.tex, bibliography.bib
```

The thesis argument: two focus groups (RQ1) produced design requirements; the prototype implements them; Study 1 measures head-orientation accuracy (RQ2); Study 2 measures the privacy switch — redirecting a spoken answer to the chat when someone enters, and returning to speech when they leave (RQ3).

A full review found the thesis text had drifted from what the code and data actually show. Most of that is now fixed in the thesis. **What remains depends on the code and on four new measurement runs**, which is this session's job.

## Uncommitted code changes to review, test on hardware, then commit

All of these compile and were tested headless with fake radar/face/TTS objects. **None has been run on the device.** Nothing is committed.

| File | Change | Why |
|---|---|---|
| `service_layer/handlers.py` | New `VisitDiscretion` class: `is_set()` is true while `PrivacyGuard.visitors > 0` and modality is `"web"`. | While a visitor is present, the head must not turn to whoever speaks (that makes the visitor the addressee) and the visitor's speech must not be recorded or answered. |
| `service_layer/handlers.py` | `register_tie_led_handlers(bus, radar, face=None)` now subscribes to `ModalitySwitched`. On redirect to web: tie strip `off`, eyes closed. On return to voice: idle pulse, eyes open. Listening/speaking/speech-ended handlers no longer overwrite the redirected state. | The thesis describes a signalled switch (§4.4.2). It was not implemented before. |
| `entrypoints/main.py` | Keeps the `PrivacyGuard` returned by `register_handlers`; builds `VisitDiscretion`; passes it as `paused=` to `start_doa_tracking` and as `visit_discretion=` to `vad_input_loop` (gates opening a recording, and aborts one in progress with reason "Besuch im Raum"); passes `face` to `register_tie_led_handlers`. | Wires the above. |
| `adapters/hardware/led_eyes.py` | New public `set_eyes_closed(closed, duration_s=0.4)`. | Used by the redirect signal. |
| `adapters/hardware/face_display.py` | No-op `set_eyes_closed` on `DummyFaceDisplayAdapter`. | Keeps the no-hardware path working. |
| `domain/conversation.py` | `SYSTEM_PROMPT` gains a paragraph: the assistant talks to one person and must never address, greet, answer for or comment on anyone else in the room. | The thesis's requirements table claimed the prompt did this. It didn't. |
| `scripts/study_head_accuracy.py` | New `--production` flag: reads `start_doa_tracking`'s defaults via `inspect.signature` and builds the DOA adapter exactly as `main.py` does, including `DOA_OFFAXIS_GAIN` and `DOA_CALIBRATION_PATH`; records the configuration in the `cfg_*` columns. | Study 1's main session did not run production settings (see below). |

## Facts about the code the thesis now depends on

Do not change these without telling Marcus, because the thesis describes them.

- **Study 1 did not use production settings.** Production (`start_doa_tracking` defaults): `doa_onset_skip_s=0.0`, `sample_window_s=0.6`, `min_doa_samples=2`, `post_move_quiet_s=0.6`, `doa_samples=5`. Study 1's main and validation sessions: 1.5 s onset skip, 0.25 s sample interval, 3 minimum samples, 1.5 s post-move quiet. The main session ran raw bearings (no off-axis gain); the validation session ran `offaxis_gain=0.869`.
- **The switch reacts to arrivals, not presence.** `PrivacyGuard` decides entries and exits at the door zone only. A spoken turn makes the conversation partner present; only people who come in through the door count as visitors. Anyone already in the room when a conversation starts is treated as part of it.
- **The exit-speed condition (`EXIT_SPEED_MMS = 500` in `adapters/hardware/radar_ld2450.py`) was added after Study 2.** Replaying Study 2's radar frames with `scripts/replay_privacy_switch.py` reproduces every observed trial to within 1 ms with the old logic; with the condition, all 8 premature returns disappear at the cost of 3 missed returns instead of 1.
- **The capacitive touch sensor does not exist yet.** Marcus plans to fit one on top of the head, with vibration feedback, toggling private mode. The thesis currently says it is planned and not built.
- **`ConfidentialIntentDetected`** in `domain/events.py` is defined but never raised or handled. The thesis says the system never classifies what is said; an examiner reading the code could ask about it.

## Tasks, in priority order

### Task 0 — Verify the new behaviour on the device (do this first)

Run `entrypoints/main.py` on the Pi. Hold a spoken conversation, then have someone walk in through the door zone while the assistant is speaking. Expected:

1. The voice lowers while they are in the doorway.
2. On entry the reply moves to the chat, **the tie strip goes dark and the eyes close**.
3. While they are inside, **the head does not turn when either person speaks**, and no recording is opened.
4. Switching back to voice by hand in the web app restores head tracking, listening, the idle tie pulse and open eyes.
5. When the visitor leaves (without the manual switch), the same restore happens.

Report which of these held and paste the relevant log lines. If any fails, fix it, and tell Marcus so the thesis's `\todo` in §4.4.2 stays open.

### Task 1 — Study 1 production session (~1 hour)

Same room, geometry and procedure as the main session of 9 September (home the head, place the loudspeaker on the head's axis, verify with the protractor, return home before presenting).

```
python3 scripts/study_head_accuracy.py --production --session production --distance 1.25 --noise none --reps 5 --angle 45
python3 scripts/study_head_accuracy.py --production --session production --distance 1.25 --noise none --reps 5 --angle 90
python3 scripts/study_head_accuracy.py --production --session production --distance 1.25 --noise none --reps 5 --angle 135
```

Check the `cfg_*` columns in `study/head_accuracy_production.csv` show the production values listed above, and note what `cfg_offaxis_gain` recorded.

**The thesis needs**, per bearing: median absolute error and signed mean ± SD; latency from voice onset to rest (median [IQR]); detection failures. Compare against the main session's 1.25 m quiet cells in `study/head_accuracy_20260909.csv` (signed means +6.4°, 0.0°, −5.4°; quiet latency median 3.413 s over all distances). The question it answers: is the study configuration representative of the deployed device, or does production trade accuracy for speed? Thesis placeholder: `\subsection{Production Configuration}`, label `sec:results-s1-production`.

### Task 2 — Calibration table at a held-out bearing (~1 hour)

The existing sweep `study/doa_angle_response_20260909_1550.csv` is at 0.8 m. Build one at the study distance, **excluding 60°**, then test 60° with and without it:

```
python3 scripts/calibrate_doa_angles.py --distance 1.25 --angles 0 30 45 90 135 150 180 --reps 3 --session table125
DOA_CALIBRATION_PATH=study/doa_angle_response_table125.csv \
  python3 scripts/study_head_accuracy.py --production --session production_cal   --distance 1.25 --noise none --reps 5 --angle 60
DOA_CALIBRATION_PATH= \
  python3 scripts/study_head_accuracy.py --production --session production_nocal --distance 1.25 --noise none --reps 5 --angle 60
```

Confirm from `cfg_offaxis_gain` that the table was actually active in the first run and absent in the second — the Pi's `.env` or `app_settings.json` may override the environment variable. `load_calibration` rejects non-monotonic tables; if it does, report that rather than working around it.

**The thesis needs**: absolute error at 60° with and without the table, set against the single-gain correction's +6.87° residual at 60° in the validation session. The question: does interpolation generalise where a single constant did not?

### Task 3 — Study 2 video timings (~1–2 hours at the desk)

```
python3 scripts/study_privacy_switch.py --session study2 --video-template
# fill study/privacy_switch_study2_video.csv, one row per trial:
#   reply_onset_s   first audible word of the reply (the sync marker)
#   crossing_in_s   foot crosses the zone-edge tape inward
#   audible_stop_s  the voice stops (true positives)
#   crossing_out_s  foot crosses the tape outward
python3 scripts/study_privacy_switch.py --session study2 --import-video
python3 scripts/study_privacy_switch.py --session study2 --report
```

`analyse_privacy_switch.py` does not read the video columns. **Extend it** to report, per script and overall, for the 53 detected entries: physical crossing → switch; physical crossing → audible stop; and the radar's own detection delay (radar-estimated crossing minus video crossing). State the output latency the import applied when mapping video time onto the Pi clock.

**Why it matters**: the thesis claims to measure end-to-end behavioural latency, from a physical crossing to an audible change. Today it only has latency against the radar's own estimate of the crossing, with the sensor's delay unobserved. These numbers make the claim true. Thesis placeholder: a `\todo` immediately before `\subsection{Lowered Voice at the Door}`.

### Task 4 — Study 2 held-out session in a different room (~1 hour + analysis)

A second person moves; a different room; the exit condition active. This validates the post-hoc exit condition on trials it was not derived from.

1. Pull the latest code onto the Pi and confirm `EXIT_SPEED_MMS = 500` is present.
2. In the web app's settings tab, draw the room zone and door zone for the new doorway. Freeze them before the first trial, and record the coordinates.
3. Run:
   ```
   python3 scripts/study_privacy_switch.py --session study2_holdout --mover M2 --reps A=15,F=15,B=10,D=10
   python3 scripts/analyse_privacy_switch.py study/privacy_switch_study2_holdout.csv
   ```

**The thesis needs**, in the same form as Study 2's tables: entries detected and false switches by script; returns to speech classified correct / premature / missed; all with exact Clopper–Pearson 95 % intervals; plus the date, room, zone coordinates and mover ID. The session changes the mover and the room together, so keep "does the exit condition work on new data" apart from "how much transfers between rooms" when summarising. Thesis placeholders: labels `sec:feas-s2-holdout` (method) and `sec:results-s2-holdout` (results).

### Task 5 — Optional, if time allows

- **Touch control.** Capacitive sensor on top of the head plus vibration feedback, toggling private mode. Decide which board it's wired to (the bridge XIAO already carries a serial command channel to the Pi; a `gpiozero` input on the Pi is the alternative), then implement and test. If it works before submission, tell Marcus so the thesis can describe a physical override again.
- **Remove `ConfidentialIntentDetected`** if nothing is meant to use it.

## Working rules

- **Never invent, estimate or round away a number.** Everything reported must come from a script run on the raw CSV/JSONL, and you must show the command used. If a figure can't be computed, say so.
- Extend the analysis scripts rather than computing by hand, so the numbers stay reproducible.
- **Don't edit the thesis LaTeX unless Marcus asks.** Hand over results ready to paste, in the thesis's conventions: `7.0\textdegree{}`, `1.25\,m`, `3.4\,s`, `5\,\%`; no siunitx; proportions with exact Clopper–Pearson 95 % CIs; durations as median [IQR].
- If anything in the code or data contradicts what the thesis says above, stop and flag it rather than quietly working around it.
- Commit only when Marcus asks, and keep the existing code style and comment density.
