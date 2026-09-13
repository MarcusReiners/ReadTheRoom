# Study 2 held-out session — setup notes

Session `study2_holdout`, 13 September 2026, 15:47–16:14, one continuous run (seed 980545), code `1787ab9`.

## Reported by the experimenter (Marcus)

- A different room from Study 2, with different furniture.
- Door frame about 80 cm wide; sensor to door about 430 cm (tape measure, rounded as given).
- Room zone: the left and near edges were kept from the previous room after checking in the web app that the sensor placement and the mover's dot matched the new room; only the right and far edges were redrawn. The identical coordinates in both sessions come from that reuse, not from a new calibration.
- One helper sat still at the desk throughout ("seat spot": the stationary dot in front of the sensor). Mover M1 (Marcus).
- No floor mark for where the mover stood during the stays; the position below is what the radar reported.
- One pass (B) was redone with the reason "nothing tracked" (logged in `privacy_switch_study2_holdout_discarded.jsonl`).

## From the recorded data

`python3 scripts/analyse_privacy_switch.py study/privacy_switch_study2_holdout.csv` (sections Geometry, Seated person)

- Room zone x −1630..1646 mm, y 107..4311 mm; door zone x −1630..−214 mm, y 4320..4979 mm (1416 × 659 mm). The door zone's left edge equals the room zone's left edge, the web app's default placement for a new door zone.
- Door zone inner edge 4320 mm from the sensor, consistent with the measured ~430 cm.
- Room-edge crossing of detected entries: x median −807 mm [IQR −949, −609], range −1013..−361 mm (652 mm wide) — within the ~80 cm frame; the door zone is about 0.6 m wider than the frame.
- Seated person (30 s baseline, 300/300 frames): x 51.5, y 653.5 mm, 656 mm from the sensor; reports within 476 mm of that.
- Mover during the stays: x median −442 mm [−663, −221], y 2612 mm [2441, 2792]; 1708 mm [1528, 1879] past the door zone's inner edge; 2037 mm [1894, 2222] from the seated person.
- Reply: pre-rendered recording, voice Jarvis, `eleven_flash_v2_5`, 64.923 s, identical sha256 in all 50 trials. Exit rule 500 mm/s, stay 15 s.

Not measured: sensor height, room and corridor dimensions, reflective surfaces.
