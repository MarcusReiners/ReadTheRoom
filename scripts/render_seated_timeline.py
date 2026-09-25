"""Renders, for the second installation of Study 2, when the radar reported the
person sitting between the sensor and the doorway. Output is a TikZ figure body
on stdout, in the style of the thesis's other plots:

  top     one bar for all trials: the share of frames in each class
  below   a timeline for each trial with any gap, grouped by the kind of gap,
          time in the trial along the axis; the remaining trials are counted
          but not drawn, since they were reported throughout

Each frame falls into one of three classes, as in seated_dropouts() of
analyse_privacy_switch.py:
  reported    a target within the seat radius of the baseline position
  shifted     not at the seat, but another target within 1 m of it: most
              likely the same person reported a little off their usual place
  lost        not at the seat and nobody within 1 m of it

    python3 scripts/render_seated_timeline.py > <thesis>/figures/seated-timeline.tex
"""

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyse_privacy_switch as analysis

CSV = os.path.join(ROOT, "study", "privacy_switch_study2_holdout.csv")
XMAX = 30.0          # seconds shown; later frames in the long trials are all "reported"
NEAR_M = 1.0         # a gap with another target this close to the seat counts as shifted
FILL = {"reported": "seatc!35", "shifted": "black!35", "lost": "lossc"}
X0, XW = 2.6, 10.6   # left edge and width (cm) of the time axis, which spans XMAX seconds
ROW = 0.52           # pitch of the trial rows (cm)
BAR = 0.34           # height of a bar (cm)


def classes(x, seat):
    """Frame times and classes for one trial."""
    frames = x["frames"]
    seen = [any(analysis.near_seat(t, seat) for t in f["targets"]) for f in frames]
    out = ["reported"] * len(frames)
    i = 0
    while i < len(frames):
        if seen[i]:
            i += 1
            continue
        j = i
        while j < len(frames) and not seen[j]:
            j += 1
        near = [math.hypot(t["x"] - seat["median_x_mm"], t["y"] - seat["median_y_mm"]) / 1000
                for f in frames[i:j] for t in f["targets"]]
        cls = "shifted" if near and min(near) <= NEAR_M else "lost"
        out[i:j] = [cls] * (j - i)
        i = j
    t0 = frames[0]["t"]
    return [f["t"] - t0 for f in frames], out


def runs(times, cls):
    """(start, end, class) runs; a frame lasts until the next one, the last for 0.1 s."""
    out, start = [], 0
    for k in range(1, len(times) + 1):
        if k == len(times) or cls[k] != cls[start]:
            end = times[k] if k < len(times) else times[-1] + 0.1
            out.append((times[start], end, cls[start]))
            start = k
    return out


def x_of(t):
    return X0 + XW * min(t, XMAX) / XMAX


def bar(y, a, b, fill):
    return (f"  \\fill[{fill}] ({x_of(a):.3f},{y - BAR / 2:.3f}) rectangle "
            f"({x_of(b):.3f},{y + BAR / 2:.3f});")


def main():
    rows, logs, door = analysis.load(CSV, None)
    baselines = analysis.load_baselines(CSV)
    res = [analysis.analyse_trial(r, logs[r["trial_id"]], door) for r in rows if r["trial_id"] in logs]
    res.sort(key=lambda x: int(x["id"]))

    frames = {"reported": 0, "shifted": 0, "lost": 0}
    trials, gappy = 0, []
    for x in res:
        seat = analysis.seat_of(logs[x["id"]], baselines)
        if not seat:
            continue
        trials += 1
        times, cls = classes(x, seat)
        for c in cls:
            frames[c] += 1
        spans = runs(times, cls)
        if any(c != "reported" for _, _, c in spans):
            gappy.append((int(x["id"]), times[-1] + 0.1, spans))
    total = sum(frames.values())
    share = {c: 100.0 * n / total for c, n in frames.items()}
    lost = [g for g in gappy if any(c == "lost" for _, _, c in g[2])]
    shifted = [g for g in gappy if g not in lost]

    out = []
    # the share of all frames, as one bar
    left = 0.0
    for c in ("reported", "shifted", "lost"):
        width = XW * share[c] / 100
        out.append(f"  \\fill[{FILL[c]}] ({X0 + left:.3f},{-BAR / 2:.3f}) rectangle "
                   f"({X0 + left + width:.3f},{BAR / 2:.3f});")
        left += width
    out.append(f"  \\node[anchor=east, font=\\footnotesize\\bfseries] at ({X0 - 0.15},0) "
               f"{{All {trials} trials}};")
    out.append(f"  \\node[anchor=south west, inner sep=1pt] at ({X0},{BAR / 2 + 0.05:.2f}) "
               f"{{\\textbf{{{share['reported']:.1f}\\,\\%}} reported at the seat}};")
    out.append(f"  \\node[anchor=south east, inner sep=1pt] at ({X0 + XW},{BAR / 2 + 0.05:.2f}) "
               f"{{\\textcolor{{black!60}}{{\\textbf{{{share['shifted']:.1f}\\,\\%}} about 0.5\\,m off}}"
               f"\\quad\\textcolor{{lossc!85!black}}{{\\textbf{{{share['lost']:.1f}\\,\\%}} not reported}}}};")

    # one row per trial with a gap, grouped by the kind of gap
    y = -1.05
    grid = []
    for title, colour, group in (("Not reported, nobody near", "lossc!85!black", lost),
                                 ("Reported about 0.5\\,m from the seat", "black!60", shifted)):
        out.append(f"  \\node[anchor=west, font=\\footnotesize\\bfseries, text={colour}] "
                   f"at (0,{y:.3f}) {{{title}}};")
        y -= ROW
        first = y
        for tid, length, spans in group:
            out.append(f"  \\node[anchor=east] at ({X0 - 0.15},{y:.3f}) {{Trial {tid}}};")
            out.append(bar(y, 0, length, FILL["reported"]))
            for a, b, c in spans:
                if c == "reported" or a >= XMAX:
                    continue
                out.append(bar(y, a, b, FILL[c]))
                if c == "lost":
                    whole = a < 0.2 and b >= length - 0.2
                    label = f"{b - a:.1f}\\,s" + (", the whole trial" if whole else "")
                    out.append(f"  \\node[text=white, font=\\scriptsize\\bfseries] "
                               f"at ({(x_of(a) + x_of(b)) / 2:.3f},{y:.3f}) {{{label}}};")
            if length > XMAX:
                out.append(f"  \\node[anchor=west, text=black!55, font=\\scriptsize] "
                           f"at ({x_of(XMAX) + 0.05:.3f},{y:.3f}) {{$\\rightarrow$ {length:.0f}\\,s}};")
            y -= ROW
        grid += [f"  \\draw[black!12, line width=0.3pt] ({x_of(t):.3f},{first + ROW / 2:.3f}) -- ({x_of(t):.3f},{y + ROW / 2:.3f});"
                 for t in range(0, int(XMAX) + 1, 5)]
        y -= 0.15
    out.append(f"  \\node[anchor=west, text=black!60] at (0,{y + 0.1:.3f}) "
               f"{{The other {trials - len(gappy)} trials: reported at the seat throughout.}};")
    axis = y - 0.35
    # grid, axis and ticks
    out.append(f"  \\draw[black!45, line width=0.4pt] ({X0},{axis:.3f}) -- ({X0 + XW},{axis:.3f});")
    for t in range(0, int(XMAX) + 1, 5):
        out.append(f"  \\draw[black!45, line width=0.4pt] ({x_of(t):.3f},{axis:.3f}) -- ++(0,-0.08) "
                   f"node[below, font=\\scriptsize, text=black] {{{t}}};")
    out.append(f"  \\node[font=\\footnotesize] at ({X0 + XW / 2:.3f},{axis - 0.7:.3f}) {{Time in trial (s)}};")

    print("% when the radar reported the seated person, second installation of Study 2")
    print("% generated by scripts/render_seated_timeline.py -- regenerate rather than edit")
    print("\\definecolor{seatc}{HTML}{2A78D6}%")
    print("\\definecolor{lossc}{HTML}{EB6834}%")
    print("\\begin{tikzpicture}[font=\\footnotesize]")
    print("\n".join(grid))
    print("\n".join(out))
    print("\\end{tikzpicture}%")


if __name__ == "__main__":
    main()
