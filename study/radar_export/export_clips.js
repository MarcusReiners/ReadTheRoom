const fs = require("fs"), path = require("path"), { spawn } = require("child_process");
const { createCanvas } = require("@napi-rs/canvas");
const ffmpeg = require("ffmpeg-static");

const [,, logPath, outDir, spec, doorSpec, themeName = "dark", fpsArg = "30", heightArg = "1080", viewArg = "radar"] = process.argv;
if (!logPath || !outDir || !spec) {
  console.log('usage: node export_clips.js <trials.jsonl> <out dir> <trial ids, e.g. 30,84 or 41@28.5 to end 28.5 s after the first word> ["min_x,max_x,min_y,max_y" | -] [dark|light] [fps] [height] [radar|captions]');
  process.exit(1);
}
const html = fs.readFileSync(path.join(__dirname, "..", "radar_replay.html"), "utf8");
const m = { exports: {} };
new Function("module", html.match(/<script id="core">([\s\S]*?)<\/script>/)[1])(m);
const R = m.exports;
const trials = R.parseLog(fs.readFileSync(logPath, "utf8"));
const wanted = spec.split(",").map((s) => s.trim());
const fps = parseInt(fpsArg, 10), H = parseInt(heightArg, 10), captions = viewArg === "captions";
const door = doorSpec && doorSpec !== "-" ? (() => {
  const [a, b, c, d] = doorSpec.split(",").map(Number);
  return { valid: true, min_x_mm: a, max_x_mm: b, min_y_mm: c, max_y_mm: d };
})() : null;
fs.mkdirSync(outDir, { recursive: true });

const iso = (t) => { const d = new Date(t * 1000); const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`; };
const NAMES = { A: "direct_entry", B: "passer_by", D: "peek", F: "fast_entry", C: "passer_by_far", E: "slow_entry" };

(async () => {
  for (const entry of wanted) {
    const [id, endArg] = entry.split("@");
    const tr = trials.find((t) => String(t.id) === id);
    if (!tr) { console.log(`trial ${id} not in the log`); continue; }
    if (!tr.door && door) tr.door = door;
    const lay = R.layout(H, 0, captions);
    const t0 = tr.tStart, t1 = endArg ? tr.t0 + parseFloat(endArg) : tr.tEnd + 0.5;
    const lastFrame = tr.steps.length ? tr.steps[tr.steps.length - 1].t : t0;
    const n = Math.ceil((t1 - t0) * fps);
    const base = `trial${tr.id}_${tr.script}_${NAMES[tr.script] || "case"}`;
    const out = path.join(outDir, `${base}.mp4`);
    const ff = spawn(ffmpeg, ["-y", "-hide_banner", "-loglevel", "error", "-f", "image2pipe", "-c:v", "png", "-framerate", String(fps), "-i", "-", "-c:v", "libx264", "-preset", "slow", "-crf", "16",
      "-pix_fmt", "yuv420p", "-movflags", "+faststart", out], { stdio: ["pipe", "inherit", "inherit"] });
    const canvas = createCanvas(lay.W, lay.H), ctx = canvas.getContext("2d");
    for (let i = 0; i < n; i++) {
      R.drawComposite(ctx, tr, t0 + i / fps, R.THEMES[themeName], lay, null, captions);
      const buf = canvas.toBuffer("image/png");
      if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once("drain", r));
    }
    ff.stdin.end();
    await new Promise((r) => ff.on("close", r));
    const rel = (t) => `${(t - t0).toFixed(2).padStart(6)} s`;
    const lines = [
      `Trial ${tr.id}  script ${tr.script} (${NAMES[tr.script]})  ${tr.row ? tr.row.classification : ""}  ${R.outcomeText(tr.row, tr)}`,
      `Video: ${lay.W}x${lay.H}, ${fps} fps, ${(n / fps).toFixed(2)} s, ${themeName} theme, ${captions ? "radar with status and event captions" : "radar only"}`,
      `First frame = Pi clock ${iso(t0)}`,
      `First word of the reply at ${rel(tr.t0)} in this video; the on-screen clock counts from there`,
      ...(t1 > lastFrame + 0.5 ? [`The log ends at ${rel(lastFrame)}; the radar view holds its last frame after that`] : []),
      ...(t1 < tr.tEnd ? [`Cut at ${rel(t1)}; the log runs to ${rel(tr.tEnd)}`] : []),
      "",
      "Events (time in this video, then Pi clock):",
      ...tr.events.filter((e) => R.eventLabel(e) && e.t <= t1).map((e) => `  ${rel(e.t)}   ${iso(e.t)}   ${R.eventLabel(e)}`),
    ];
    fs.writeFileSync(path.join(outDir, `${base}.txt`), lines.join("\n") + "\n");
    console.log(`${base}.mp4  ${(n / fps).toFixed(1)} s, ${n} frames, ${(fs.statSync(out).size / 1e6).toFixed(1)} MB`);
  }
})();
