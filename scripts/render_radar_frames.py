"""Renders recorded radar frames of one Study 2 trial as a row of TikZ panels,
for the figure that pairs photographs of an entry with what the radar saw.

Output is LaTeX on stdout: one small panel per chosen moment, each showing the
room and door zones, the targets reported in that frame, and the track so far.
It uses the same pgfplots style as the other figures in the thesis, so nothing
has to be rendered to an image.

    # four moments, picked automatically between first detection and the switch
    python3 scripts/render_radar_frames.py --trial 41 --auto

    # explicit moments, in seconds after the first frame of the trial
    python3 scripts/render_radar_frames.py --csv study/privacy_switch_study2_holdout.csv \
        --trial 12 --times 0.0 1.4 2.1 3.0 > ../thesis/figures/entry-radar.tex

Paste the output into the figure, or \\input it.
"""

import argparse
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyse_privacy_switch as analysis

M = 1000.0  # mm -> m


def frames_of(trial):
    return [f for f in trial["events"] if f["kind"] == "frame"]


def first_events(trial):
    """Time of the first event of each kind; a trial also logs the exit and the
    return to normal speech, and those must not be mistaken for the entry."""
    ev = {}
    for e in trial["events"]:
        ev.setdefault(e["kind"], e["t"])
    return ev


def pick_times(trial, door, n):
    """The moments the trial turns on: first sighting, door zone, crossing, after.

    Anchoring the panels to the logged events rather than to an even spacing
    keeps them comparable with photographs of the same entry, and makes the
    figure show the decision rather than four arbitrary instants. If a trial
    has fewer events than panels asked for, the remaining moments are spread
    evenly over what is left.
    """
    frames = frames_of(trial)
    seen = [f["t"] for f in frames if f["targets"]]
    if not seen:
        raise SystemExit("this trial has no target reports")
    ev = first_events(trial)
    switch = ev.get("modality")
    end = (switch + 1.5) if switch else seen[-1]
    wanted = [seen[0], ev.get("door"), ev.get("enter") or switch, end]
    times = []
    for t in wanted:
        if t is not None and all(abs(t - u) > 0.35 for u in times):
            times.append(t)
    times.sort()
    while len(times) > n:
        gaps = [(times[i + 1] - times[i - 1], i) for i in range(1, len(times) - 1)]
        times.pop(min(gaps)[1] if gaps else -1)
    while len(times) < n:
        gaps = [(times[i + 1] - times[i], i) for i in range(len(times) - 1)]
        if not gaps:
            break
        widest, i = max(gaps)
        times.insert(i + 1, times[i] + widest / 2)
    return times


def frame_at(trial, t):
    return min(frames_of(trial), key=lambda f: abs(f["t"] - t))


def zone_rect(zone, style):
    if not zone:
        return ""
    x0, x1 = sorted((zone["min_x_mm"] / M, zone["max_x_mm"] / M))
    y0, y1 = sorted((zone["min_y_mm"] / M, zone["max_y_mm"] / M))
    return f"      \\draw[{style}] (axis cs:{x0:.2f},{y0:.2f}) rectangle (axis cs:{x1:.2f},{y1:.2f});\n"


def panel(trial, door, t, t0, xlim, ylim, label, width):
    room = trial["zone"]
    scale = max(1.0, (width / 0.24) ** 0.5)  # marks grow with the panel, but slower
    frames = frames_of(trial)
    f = frame_at(trial, t)
    trail = [(p["x"] / M, p["y"] / M) for g in frames if g["t"] <= f["t"] for p in g["targets"]]
    out = [
        "  \\begin{tikzpicture}",
        "    \\begin{axis}[",
        f"      axis equal image, width={width:.3f}\\textwidth,",
        f"      xmin={xlim[0]:.2f}, xmax={xlim[1]:.2f}, ymin={ylim[0]:.2f}, ymax={ylim[1]:.2f},",
        "      xtick=\\empty, ytick=\\empty, axis line style={draw=black!45},",
        f"      title={{\\footnotesize {label}}}, title style={{yshift=-2pt}},",
        "    ]",
    ]
    for line in (zone_rect(room, "black!40, fill=black!4"), zone_rect(door, "black!40, dashed, fill=black!10")):
        if line:
            out.append(line.rstrip("\n"))
    if trail:
        pts = " ".join(f"({x:.2f},{y:.2f})" for x, y in trail)
        out.append(f"      \\addplot[only marks, mark=*, mark size={0.5 * scale:.1f}pt, black!25] coordinates {{{pts}}};")
    now = " ".join(f"({p['x'] / M:.2f},{p['y'] / M:.2f})" for p in f["targets"])
    if now:
        out.append(f"      \\addplot[only marks, mark=*, mark size={2.4 * scale:.1f}pt, radarc] coordinates {{{now}}};")
    if ylim[0] <= 0.0 <= ylim[1] and xlim[0] <= 0.0 <= xlim[1]:
        out.append(f"      \\addplot[only marks, mark=triangle*, mark size={2.0 * scale:.1f}pt, black!70] coordinates {{(0,0)}};")
    out.append("    \\end{axis}")
    out.append("  \\end{tikzpicture}")
    return "\n".join(out)


def panel_turned(trial, door, t, depth, across, height_cm, label):
    """One panel with the view turned a quarter clockwise: depth from the sensor
    runs left to right (the doorway on the right) and the sensor's left is up.
    That is a rotation, not a mirror, and it matches a camera looking along the
    wall at the doorway, as in the entry video. The axis is sized by height so
    the panel lines up with a photograph of the same height; the width follows
    from keeping one metre the same length in both directions."""
    room = trial["zone"]
    frames = frames_of(trial)
    f = frame_at(trial, t)
    turn = lambda x, y: (y, -x)
    def rect(zone, style):
        if not zone:
            return None
        x0, x1 = sorted((zone["min_x_mm"] / M, zone["max_x_mm"] / M))
        y0, y1 = sorted((zone["min_y_mm"] / M, zone["max_y_mm"] / M))
        return (f"      \\draw[{style}] (axis cs:{y0:.2f},{-x1:.2f}) rectangle (axis cs:{y1:.2f},{-x0:.2f});")
    width_cm = height_cm * (depth[1] - depth[0]) / (across[1] - across[0])
    trail = [turn(p["x"] / M, p["y"] / M) for g in frames if g["t"] <= f["t"] for p in g["targets"]]
    now = [turn(p["x"] / M, p["y"] / M) for p in f["targets"]]
    out = [
        "  \\begin{tikzpicture}",
        "    \\begin{axis}[",
        f"      scale only axis, width={width_cm:.2f}cm, height={height_cm:.2f}cm,",
        f"      xmin={depth[0]:.2f}, xmax={depth[1]:.2f}, ymin={across[0]:.2f}, ymax={across[1]:.2f},",
        "      xtick=\\empty, ytick=\\empty, axis line style={draw=black!45}, clip=true,",
        "    ]",
    ]
    for line in (rect(room, "black!40, line width=0.6pt, fill=black!4"),
                 rect(door, "black!55, line width=0.6pt, dashed, fill=black!10")):
        if line:
            out.append(line)
    if trail:
        pts = " ".join(f"({a:.2f},{b:.2f})" for a, b in trail)
        out.append(f"      \\addplot[only marks, mark=*, mark size=1.1pt, black!30] coordinates {{{pts}}};")
    if now:
        pts = " ".join(f"({a:.2f},{b:.2f})" for a, b in now)
        out.append(f"      \\addplot[only marks, mark=*, mark size=3.4pt, radarc] coordinates {{{pts}}};")
    out.append(f"      \\node[anchor=north west, font=\\footnotesize, fill=white, fill opacity=0.85, text opacity=1,"
               f" inner sep=2pt] at (rel axis cs:0.015,0.97) {{{label}}};")
    out.append("    \\end{axis}")
    out.append("  \\end{tikzpicture}")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", help="session CSV (default: the newest study/privacy_switch_*.csv)")
    parser.add_argument("--settings", help="JSON with the door_zone (default: <csv>_settings.json)")
    parser.add_argument("--trial", required=True, help="trial id to render")
    parser.add_argument("--times", type=float, nargs="+", help="moments in seconds after the trial's first frame")
    parser.add_argument("--auto", action="store_true", help="pick moments automatically")
    parser.add_argument("--panels", type=int, default=4, help="number of panels for --auto (default: 4)")
    parser.add_argument("--cols", type=int, default=4, help="panels per row (default: 4; use 2 for a larger 2x2 block)")
    parser.add_argument("--focus", action="store_true",
                        help="crop the view to the band the targets move through, instead of showing the "
                             "whole field down to the sensor; makes the panels wider than they are tall")
    parser.add_argument("--turned", action="store_true",
                        help="depth runs left to right with the doorway on the right, one panel per moment, "
                             "each at a fixed height so it can sit beside a video frame")
    parser.add_argument("--depth", type=float, nargs=2, metavar=("NEAR", "FAR"),
                        help="with --turned: distance range from the sensor to show, in metres")
    parser.add_argument("--across", type=float, nargs=2, metavar=("LOW", "HIGH"),
                        help="with --turned: range across the view, in metres, positive to the sensor's left")
    parser.add_argument("--height", type=float, default=4.8, help="with --turned: panel height in cm (default 4.8)")
    parser.add_argument("--split", metavar="PREFIX",
                        help="with --turned: write panel i to PREFIX-i.tex instead of printing all panels")
    args = parser.parse_args()

    study = os.path.join(ROOT, "study")
    path = args.csv or max((p for p in glob.glob(os.path.join(study, "privacy_switch_*.csv"))
                            if not p.endswith("_video.csv")), key=os.path.getmtime)
    rows, logs, door = analysis.load(path, args.settings)
    trial = logs.get(str(args.trial))
    if trial is None:
        raise SystemExit(f"trial {args.trial} not found in {os.path.basename(path)}")
    door = trial.get("door_zone") or door
    frames = frames_of(trial)
    t0 = frames[0]["t"]
    times = [t0 + dt for dt in args.times] if args.times else pick_times(trial, door, args.panels)

    tx = [p["x"] / M for f in frames for p in f["targets"]] or [0.0]
    ty = [p["y"] / M for f in frames for p in f["targets"]] or [0.0]
    zones = [z for z in (trial["zone"], door) if z]
    zx = [z[k] / M for z in zones for k in ("min_x_mm", "max_x_mm")]
    zy = [z[k] / M for z in zones for k in ("min_y_mm", "max_y_mm")]
    xlim = (min(tx + zx) - 0.3, max(tx + zx) + 0.3)
    if args.focus:
        # the near half of the room is empty in every trial: the mover comes
        # through the doorway and stops well short of the sensor. Cropping it
        # away doubles the scale the panels can be drawn at.
        ylim = (min(ty) - 0.6, max(ty + zy) + 0.3)
    else:
        ylim = (min(ty + zy + [0.0]) - 0.3, max(ty + zy) + 0.3)

    if args.turned:
        marks = first_events(trial)
        sighted = next((f["t"] for f in frames if f["targets"]), None)
        depth = args.depth or (min(ty) - 0.3, max(ty + zy) + 0.3)
        across = args.across or (min(-x for x in tx + zx) - 0.3, max(-x for x in tx + zx) + 0.3)
        for i, t in enumerate(times, 1):
            note = next((n for k, n in (("enter", "entering"), ("modality", "switch"), ("door", "at the door"))
                         if k in marks and abs(marks[k] - t) < 0.35), "")
            if not note and sighted is not None and abs(sighted - t) < 0.35:
                note = "first seen"
            label = f"{t - t0:.1f}\\,s" + (f", {note}" if note else "")
            body = "\n".join([
                f"% radar view of trial {args.trial} in {os.path.basename(path)}, moment {i} ({t - t0:.1f} s)",
                "% generated by scripts/render_radar_frames.py --turned -- regenerate rather than edit",
                "\\definecolor{radarc}{HTML}{2A78D6}%",
                panel_turned(trial, door, t, depth, across, args.height, label) + "%",
            ])
            if args.split:
                with open(f"{args.split}-{i}.tex", "w") as fh:
                    fh.write(body + "\n")
                print(f"wrote {args.split}-{i}.tex  ({label.replace(chr(92) + ',', ' ')})")
            else:
                print(body)
        return

    print(f"% radar view of trial {args.trial} in {os.path.basename(path)}")
    print("% generated by scripts/render_radar_frames.py -- regenerate rather than edit")
    print("  \\definecolor{radarc}{HTML}{2A78D6}")
    marks = first_events(trial)
    sighted = next((f["t"] for f in frames if f["targets"]), None)
    width = (0.96 - 0.02 * (args.cols - 1)) / args.cols
    if args.focus and ylim[0] > 0.0:
        print(f"% view cropped: the sensor stands {ylim[0]:.1f} m below the bottom edge, on the centre line")
    for i, t in enumerate(times):
        note = next((n for k, n in (("enter", "entering"), ("modality", "switch"), ("door", "at the door"))
                     if k in marks and abs(marks[k] - t) < 0.35), "")
        if not note and sighted is not None and abs(sighted - t) < 0.35:
            note = "first seen"
        label = f"{t - t0:.1f}\\,s" + (f", {note}" if note else "")
        print(panel(trial, door, t, t0, xlim, ylim, label, width))
        if i < len(times) - 1:
            print("  \\hfill" if (i + 1) % args.cols else "  \\par\\medskip")


if __name__ == "__main__":
    main()
