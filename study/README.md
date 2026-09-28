# Studies

Data, harnesses and analysis of the two technical studies in the thesis. The thesis reports these sessions:

| Study | Session | Files |
|---|---|---|
| Study 1, head-orientation accuracy | Main session, 9 September 2026 | `head_accuracy_20260909*` |
| Study 1 | Validation session | `head_accuracy_validation*` |
| Study 2, privacy switch | Main session, 11 September 2026 | `privacy_switch_study2*` (without `_holdout`) |
| Study 2 | Second installation, 13 September 2026 | `privacy_switch_study2_holdout*` |

The other files in this folder are recordings made while developing the prototype.

# Study 1 — Head-Orientation Accuracy

Technical feasibility study: how accurately does the head turn towards a speaker?

Design: **3 bearings × 3 distances × 2 noise conditions × 5 repetitions = 90 trials**.

| Factor | Levels |
|---|---|
| Angle (true bearing) | 45°, 90°, 135° |
| Distance | 0.5 m, 1.25 m, 2.5 m |
| Noise | `none` (quiet), `ambient` (babble of eight voices, 50 dB(A) at the array) |
| Reps | 5 per cell |

The stimulus is six Harvard sentences in one 13.6 s file, played from a loudspeaker at 60 dB(A) at 1 m; the babble plays continuously from a fixed position behind the head. The audio files (`study/stimulus/*.wav`) are not part of the repository.

90° = straight ahead. Angles are room bearings, measured from the head's home position.

## Before the session

- [ ] Pi running, `pigpiod` active (`sudo pigpiod`) — without it the servo jitters and timings are unusable
- [ ] `USE_SERVO=true`, ReSpeaker connected, main app **not** running (it would fight for the mic)
- [ ] Tape marks at 45/90/135° × 0.5/1.25/2.5 m (9 loudspeaker positions)
- [ ] Protractor / angle gauge fixed under the head, zeroed at home
- [ ] Babble source behind the head, set to 50 dB(A) at the array
- [ ] Rehearse once with `--simulate` so the prompts are familiar

## Running

Print the run order (grouped by distance, so the speaker moves as rarely as possible):

```bash
python3 scripts/study_head_accuracy.py --plan
```

Then one command per condition, e.g.:

```bash
python3 scripts/study_head_accuracy.py --angle 45 --distance 0.5 --noise none --reps 5 --session 20260909
```

Per trial the script: homes the head, waits for the stimulus (started by hand on another machine, or played by the script with `--play <wav>`), runs the **production** `start_doa_tracking` path, detects settling, then asks for the protractor reading.

After each trial: enter the measured angle → `ENTER` keeps it, `r` redoes the rep, `q` quits. Notes are optional and free text.

Keep `--session` identical for the whole day; every condition appends to the same pair of files.

## What gets logged

Two files per session in `study/`:

- `head_accuracy_<session>.csv` — one row per trial, the analysis input
- `head_accuracy_<session>_trials.jsonl` — full detail per trial: every DoA sample with the head heading at that instant, every servo command, every PWM update

Key CSV columns:

| Column | Meaning |
|---|---|
| `angle_true` / `distance_m` / `noise_condition` / `rep` | the condition |
| `settle_time_s` | playback start → head stopped moving |
| `head_heading_at_start`, `started_at_home` | sanity check that the trial began at home |
| `doa_logged_angle` | raw DoA reading (relative to the head's *current* nose) |
| `doa_implied_bearing` | that reading converted to a room bearing |
| `servo_target_angle` | angle the servo was commanded to |
| `physical_angle_measured` | **your protractor reading** — the primary DV |
| `moved`, `miss` | whether the head reacted at all |
| `n_doa_samples`, `n_servo_commands`, `notes` | diagnostics |

`started_at_home = 0` means something moved the head after homing — the bearings for that trial are off, so redo it.

## Analysis

```bash
python3 scripts/analyse_head_accuracy.py                       # all sessions in study/
python3 scripts/analyse_head_accuracy.py study/head_accuracy_20260909.csv
python3 scripts/analyse_head_accuracy.py --plots               # needs matplotlib
python3 scripts/analyse_head_accuracy.py --include-misses      # keep non-reacting trials
```

Reports: error decomposition, per-condition table, Kruskal–Wallis per factor with η²_H effect size and Bonferroni-corrected pairwise tests. No third-party dependencies.

The **error decomposition** is the part worth reporting — it separates where the error comes from:

| Term | Computed as | Blames |
|---|---|---|
| perception | `doa_implied_bearing − angle_true` | the mic array / DoA estimate |
| mapping | `servo_target_angle − doa_implied_bearing` | the DoA→servo transform |
| actuation | `physical_angle_measured − servo_target_angle` | servo, backlash, mounting |
| **total** | `physical_angle_measured − angle_true` | end-to-end |

Non-parametric tests are used because per-cell n = 5 and the errors are not expected to be normal.

## Gotchas

- Trials with a blank protractor reading fall back to the **commanded** servo angle. That silently hides all actuation error, so measure every trial — the analyser reports how many are missing.
- Misses (head never moved) are excluded from the error stats by default and reported separately as a miss rate. Do not delete them; a miss is a result.
- The mic moves with the head, so raw DoA is relative to the head's current nose. Use `doa_implied_bearing`, never `doa_logged_angle`, for accuracy claims.
- Silence-recentering is disabled during the study (`silence_timeout_s=0`), so the head holds its position while you measure.

# Study 2 — Privacy-Switch Reliability

Technical feasibility study: does the assistant move its spoken answer to the chat when someone enters, lower its voice for someone who only stops in the doorway, and ignore people passing by?

The harness is `scripts/study_privacy_switch.py` (options in the main README). One person, the mover, walks a scripted path per trial while the assistant reads out a reply; the harness logs every radar frame and every event of the decision logic.

In the data, the movement scripts carry the IDs used during development. They correspond to the thesis as follows:

| `script_id` | ID in the thesis | Behaviour | Required reaction |
|---|---|---|---|
| A | A | Direct entry at normal walking pace | switch to chat |
| F | B | Fast entry at jogging pace | switch to chat |
| B | C | Pass close to the open door | none |
| D | D | Peek: stop in the doorway for about 3 s | lower the voice only |

## What gets logged

Per session in `study/`:

- `privacy_switch_<session>.csv` — one row per trial, the analysis input
- `privacy_switch_<session>_trials.jsonl` — full detail per trial: every radar frame and every event, timestamped on the Pi
- `privacy_switch_<session>_settings.json` — the door zone in use (the room zone is logged with every trial)
- `privacy_switch_<session>_discarded.jsonl` — trials that were redone, with the reason (only written if a trial was redone)

The second installation adds `_baseline.jsonl` (30 s of radar frames with only the seated person in view) and `_notes.md` (setup notes). `private_reply.json` describes the pre-rendered reply played in the second installation: voice, model, duration, file hash and text.

## Analysis

```bash
python3 scripts/analyse_privacy_switch.py study/privacy_switch_study2.csv          # switch rates, timing, returns to speech
python3 scripts/analyse_privacy_switch.py study/privacy_switch_study2_holdout.csv
venv/bin/python scripts/replay_privacy_switch.py study/privacy_switch_study2.csv   # replays the recorded radar frames through the current decision logic
```

`analyse_privacy_switch.py` has no third-party dependencies; `replay_privacy_switch.py` imports the assistant's own modules and needs its environment.
