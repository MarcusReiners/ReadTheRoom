# Study 1 — Head-Orientation Accuracy

Technical feasibility study: how accurately does the head turn towards a speaker?

Design (supervisor's simplified version): **3 angles × 3 distances × 2 noise × 5 reps = 90 trials**.

| Factor | Levels |
|---|---|
| Angle (true bearing) | 45°, 90°, 135° |
| Distance | 0.5 m, 1.5 m, 3.0 m |
| Noise | none, ambient |
| Reps | 5 per cell |

90° = straight ahead. Angles are room bearings, measured from the head's home position.

## Before the session

- [ ] Pi running, `pigpiod` active (`sudo pigpiod`) — without it the servo jitters and timings are unusable
- [ ] `USE_SERVO=true`, ReSpeaker connected, main app **not** running (it would fight for the mic)
- [ ] Tape marks on the floor at 45/90/135° × 0.5/1.5/3.0 m (9 speaker positions)
- [ ] Protractor / angle gauge fixed under the head, zeroed at home
- [ ] Ambient noise source ready at a fixed, repeatable level (note the dB in the run log)
- [ ] Rehearse once with `--simulate` so the prompts are familiar

## Running

Print the run order (grouped by distance, so the speaker moves as rarely as possible):

```bash
python3 scripts/study_head_accuracy.py --plan
```

Then one command per condition, e.g.:

```bash
python3 scripts/study_head_accuracy.py --angle 45 --distance 0.5 --noise none --reps 5 --session 20260907
```

Per trial the script: homes the head, waits for you to speak the prompt phrase, runs the **production** `start_doa_tracking` path, detects settling, then asks for the protractor reading.

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
python3 scripts/analyse_head_accuracy.py study/head_accuracy_20260907.csv
python3 scripts/analyse_head_accuracy.py --plots               # needs matplotlib
python3 scripts/analyse_head_accuracy.py --include-misses      # keep non-reacting trials
```

Reports: error decomposition, per-condition table, Kruskal–Wallis per factor with ε² effect size and Bonferroni-corrected pairwise tests. No third-party dependencies.

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
