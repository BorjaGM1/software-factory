#!/usr/bin/env node
// Record the dashboard sharply, for the video: a headed Chromium alone in its own Xvfb screen, grabbed by ffmpeg
// at a steady 30 fps (Playwright's recordVideo is low-bitrate VP8 and blurs the text).
//
//   node demo/rec_dash.js URL OUT_DIR [--seconds N] [--scale 2] [--no-mp4]
//
// URL is the dashboard, normally with ?present=1 so the display is paced (see dashboard/README.md). The page is
// 960x1080 CSS pixels; --scale 2 records it at 1920x2160 (same layout, twice the pixels, for zooms in the edit).
// It records until --seconds have passed or it gets SIGINT/SIGTERM, then finishes the files in OUT_DIR:
//
//   dash.mkv         the master: lossless H.264 (qp 0, 4:4:4), constant 30 fps
//   dash.mp4         for the editor: H.264 4:2:0, crf 12, faststart (skipped with --no-mp4)
//   start_epoch.txt  wall-clock time of the first frame, in epoch seconds (frame n is at start + n/30)
//   display.jsonl    when the page showed each log line, in epoch ms (window.__shown): the display is paced, so
//                    the log's own ts doesn't tell where a moment is in the footage; this does
//   layout.json      where the blocks to point at are, in video pixels; the layout never moves, so one map holds
//                    for the whole recording
//
// Needs Xvfb, ffmpeg and Playwright's Chromium (Playwright is found in ~/demo-rec/node_modules when it isn't
// installed next to this script, or set PLAYWRIGHT_DIR).
"use strict";
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const argv = process.argv.slice(2);
const flag = (name) => argv.includes(name);
const opt = (name, dflt) => (argv.includes(name) ? argv[argv.indexOf(name) + 1] : dflt);
const [url, outDir] = argv.filter((a, i) => !a.startsWith("--") && !["--seconds", "--scale"].includes(argv[i - 1]));
if (!url || !outDir) {
  console.error("usage: node demo/rec_dash.js URL OUT_DIR [--seconds N] [--scale 2] [--no-mp4]");
  process.exit(2);
}
const SECONDS = Number(opt("--seconds", 0));
const SCALE = Number(opt("--scale", 1));
const FPS = 30, CSS_W = 960, CSS_H = 1080;
const W = CSS_W * SCALE, H = CSS_H * SCALE;
const out = path.resolve(outDir);
fs.mkdirSync(out, { recursive: true });

function loadPlaywright() {
  const tries = [process.env.PLAYWRIGHT_DIR, "playwright", path.join(os.homedir(), "demo-rec/node_modules/playwright")];
  for (const t of tries.filter(Boolean)) { try { return require(t); } catch {} }
  throw new Error("Playwright not found: npm i playwright next to this script, or set PLAYWRIGHT_DIR");
}
const { chromium } = loadPlaywright();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(`[rec ${new Date().toISOString().slice(11, 19)}]`, ...a);

// The blocks an edit points at (dashboard/README.md lists them). Some only exist at times (#ask-q, #ask-a).
const TARGETS = ["#counters", "#count-running", "#count-success", "#count-flawed", "#count-blocked", "#count-manual",
  "#count-rejected", "#money", "#money-spent", "#money-per-success", "#money-failed-attempts", "#focus", "#run-meta",
  "#run-cost", "#run-title", "#run-state", "#track", "#step-intake", "#step-triage", "#step-plan", "#step-build",
  "#step-verify", "#step-review", "#step-learn", "#step-outcome", "#now", "#ask-q", "#ask-a", "#feed", "#gate",
  "#gate-title", "#checks", "#runs-panel", "#runs", "#rules-panel", "#rules"];

let xvfb, ffmpeg, context, stopping = false;
const children = [];
const cleanup = () => { for (const c of children) { try { process.kill(-c.pid, "SIGTERM"); } catch {} } };
process.on("exit", cleanup);

// A private X screen and Chromium in kiosk mode on it: no window frame, no bars, the page fills the screen.
// (On WSL /tmp/.X11-unix belongs to WSLg, so Xvfb may only get its abstract socket; that's enough. Its lock file
// says it is up.) The screen is pad pixels larger than the video when kiosk comes out short of it.
async function launch(pad) {
  const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
  let disp = 90;
  for (;; disp++) {
    const lock = `/tmp/.X${disp}-lock`;
    if (!fs.existsSync(lock)) break;
    const pid = Number(fs.readFileSync(lock, "utf8").trim());
    if (pid && !alive(pid)) { try { fs.unlinkSync(lock); break; } catch {} } // stale: its server is gone
  }
  xvfb = spawn("Xvfb", [`:${disp}`, "-screen", "0", `${W + pad[0]}x${H + pad[1]}x24`, "-nolisten", "tcp", "-nocursor"],
    { detached: true, stdio: "ignore" });
  children.push(xvfb);
  for (let i = 0; i < 50 && !fs.existsSync(`/tmp/.X${disp}-lock`); i++) await sleep(100);
  await sleep(300);
  if (!fs.existsSync(`/tmp/.X${disp}-lock`) || xvfb.exitCode !== null) throw new Error("Xvfb did not start");
  const DISPLAY = `:${disp}`;
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "rec-dash-"));
  context = await chromium.launchPersistentContext(profile, {
    headless: false, viewport: null, colorScheme: "dark", env: { ...process.env, DISPLAY },
    ignoreDefaultArgs: ["--enable-automation"],
    args: ["--kiosk", "--window-position=0,0", `--window-size=${W + pad[0]},${H + pad[1]}`, `--force-device-scale-factor=${SCALE}`,
      "--hide-scrollbars", "--no-first-run", "--noerrdialogs", "--disable-infobars", "--disable-features=Translate",
      "--force-color-profile=srgb"],
  });
  const page = context.pages()[0] || (await context.newPage());
  await page.goto("about:blank");
  await sleep(500);
  const [iw, ih] = await page.evaluate(() => [innerWidth, innerHeight]);
  return { DISPLAY, profile, page, short: [CSS_W - iw, CSS_H - ih] };
}

async function main() {
  // Without a window manager kiosk can come out a pixel short of the screen: then try again on a larger screen.
  let L = await launch([0, 0]);
  if (L.short[0] > 0 || L.short[1] > 0) {
    log(`page came out ${L.short.join("x")} px short; padding the screen`);
    await context.close().catch(() => {});
    try { process.kill(-xvfb.pid, "SIGTERM"); } catch {}
    fs.rmSync(L.profile, { recursive: true, force: true });
    await sleep(500);
    L = await launch([L.short[0] * SCALE, L.short[1] * SCALE]);
  }
  const { DISPLAY, profile, page } = L;
  await page.goto(url);
  await page.waitForSelector("#conn.live", { timeout: 20000 }).catch(() => log("warning: the page never connected to /events"));
  await page.evaluate(() => document.fonts.ready);
  await sleep(500);
  const vp = await page.evaluate(() => [innerWidth, innerHeight, devicePixelRatio]);
  if (vp[0] !== CSS_W || vp[1] !== CSS_H || vp[2] !== SCALE) log(`warning: page is ${vp.join("x")}, expected ${CSS_W}x${CSS_H}@${SCALE}`);

  const layout = async () => page.evaluate(([sel, s]) => Object.fromEntries(sel.map((q) => {
    const el = document.querySelector(q);
    if (!el || !el.getClientRects().length) return [q, null];
    const r = el.getBoundingClientRect();
    return [q, { x: Math.round(r.x * s), y: Math.round(r.y * s), w: Math.round(r.width * s), h: Math.round(r.height * s) }];
  })), [TARGETS, SCALE]);
  const firstLayout = await layout();

  // 3. ffmpeg grabs the screen at a constant 30 fps into a lossless master. x11grab stamps frames with the wall
  // clock, so the input's "start:" is the first frame's epoch time.
  const mkv = path.join(out, "dash.mkv");
  ffmpeg = spawn("ffmpeg", ["-hide_banner", "-y", "-f", "x11grab", "-draw_mouse", "0", "-framerate", String(FPS),
    "-video_size", `${W}x${H}`, "-i", `${DISPLAY}.0+0,0`, "-fps_mode", "cfr", "-r", String(FPS),
    "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-pix_fmt", "yuv444p", mkv],
    { detached: true, stdio: ["pipe", "ignore", "pipe"] });
  children.push(ffmpeg);
  let errTail = "", start = null, lastFrame = 0;
  ffmpeg.stderr.on("data", (d) => {
    const s = d.toString();
    errTail = (errTail + s).slice(-4000);
    const m = !start && errTail.match(/start: (\d{9,}\.\d+)/);
    if (m) { start = m[1]; fs.writeFileSync(path.join(out, "start_epoch.txt"), start + "\n"); log("first frame at", start); }
    const f = s.match(/frame=\s*(\d+)/g);
    if (f) lastFrame = Number(f[f.length - 1].replace(/\D/g, ""));
  });
  const ffDone = new Promise((r) => ffmpeg.on("exit", r));

  // 4. While it records, collect what the page showed when.
  const shownFile = path.join(out, "display.jsonl");
  fs.writeFileSync(shownFile, "");
  let shownN = 0;
  const pull = async () => {
    const rows = await page.evaluate((n) => (window.__shown || []).slice(n), shownN).catch(() => []);
    if (rows.length) { fs.appendFileSync(shownFile, rows.map((r) => JSON.stringify(r)).join("\n") + "\n"); shownN += rows.length; }
  };
  const poller = setInterval(pull, 1000);
  log(`recording ${url} at ${W}x${H} ${FPS} fps into ${out}${SECONDS ? ` for ${SECONDS}s` : " (Ctrl-C to stop)"}`);

  const stop = async (why) => {
    if (stopping) return;
    stopping = true;
    log("stopping:", why);
    clearInterval(poller);
    try { ffmpeg.stdin.write("q"); ffmpeg.stdin.end(); } catch {}
    const code = await Promise.race([ffDone, sleep(15000).then(() => "timeout")]);
    if (code === "timeout") { try { process.kill(-ffmpeg.pid, "SIGINT"); } catch {} await ffDone; }
    await pull();
    const lastLayout = await layout().catch(() => null);
    const moved = lastLayout ? Object.keys(firstLayout).filter((k) => !k.startsWith("#money-") && firstLayout[k] && lastLayout[k] &&
      JSON.stringify(firstLayout[k]) !== JSON.stringify(lastLayout[k])) : [];
    fs.writeFileSync(path.join(out, "layout.json"), JSON.stringify({ video: { width: W, height: H, fps: FPS, scale: SCALE },
      note: "boxes in video pixels; #ask-q and #ask-a are the halves of #now when the question and the answer show; " +
        "the #money-* figures are as wide as their text, so they shift a little as the amounts grow",
      boxes: { ...firstLayout, ...Object.fromEntries(Object.entries(lastLayout || {}).filter(([k, v]) => !firstLayout[k] && v)) },
      moved }, null, 2) + "\n");
    if (moved.length) log("warning: these blocks moved during the recording:", moved.join(" "));
    await context.close().catch(() => {});
    try { process.kill(-xvfb.pid, "SIGTERM"); } catch {}
    fs.rmSync(profile, { recursive: true, force: true });
    if (!start) log("warning: no start time in ffmpeg's output:\n" + errTail);
    const probe = spawnSync("ffprobe", ["-v", "error", "-count_packets", "-select_streams", "v:0", "-show_entries",
      "stream=nb_read_packets:format=duration", "-of", "csv=p=0", mkv]).stdout?.toString().trim().replace(/\n/g, " ");
    log(`master: ${mkv} (${probe || lastFrame + " frames"})`);
    if (!flag("--no-mp4")) {
      const mp4 = path.join(out, "dash.mp4");
      log("encoding", mp4);
      const r = spawnSync("ffmpeg", ["-hide_banner", "-loglevel", "error", "-y", "-i", mkv, "-c:v", "libx264", "-preset", "slow",
        "-crf", "12", "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4], { stdio: "inherit" });
      log(r.status === 0 ? `done: ${mp4}` : "mp4 encoding failed (the master is fine)");
    }
    process.exit(0);
  };
  process.on("SIGINT", () => stop("SIGINT"));
  process.on("SIGTERM", () => stop("SIGTERM"));
  ffmpeg.on("exit", () => { if (!stopping) stop("ffmpeg exited:\n" + errTail.slice(-800)); });
  // by the clock, not one long timer (those fire late on WSL)
  const until = Date.now() + SECONDS * 1000;
  if (SECONDS) setInterval(() => { if (Date.now() >= until) stop(`${SECONDS}s`); }, 200);
}

main().catch((err) => { console.error(err); cleanup(); process.exit(1); });
