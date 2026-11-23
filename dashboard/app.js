// Follows runs/events.jsonl through /events (one raw line per message) and draws it.
// Anything unexpected in a line is ignored or shown as plain text; it never stops the page.
// The page is patched in place: only elements whose content changed are touched, and only those animate.

const STEPS = ["intake", "triage", "plan", "build", "verify", "review", "learn", "outcome"];
const STATES = ["success", "flawed", "blocked", "manual", "rejected"];
const ACTORS = ["agent", "check", "human", "intake"];
const STATUS = { ok: "ok", pass: "ok", passed: "ok", success: "ok", true: "ok",
  failed: "failed", fail: "failed", error: "failed", false: "failed",
  started: "started", running: "started", info: "info", skipped: "skipped", skip: "skipped" };
// Focus: stay on a run until its outcome, then hold it this long before the next run (shorter when runs queue up).
const HOLD = 4500, HOLD_BUSY = 2500, HOLD_RUSH = 1300;
// Presentation mode (?present=1), for recording: the display is paced, the log is not. Each new line waits until
// the last visible change has been on screen this long (ms), by what changed; a finished run is held HOLD_PRESENT.
const PRESENT = /^(1|true|yes|on)?$/i.test(new URLSearchParams(location.search).get("present") ?? "x");
const PACE = { step: 2000, check: 700, fail: 2200, rule: 2500, review: 2500, state: 2000, act: 900, feed: 1500, focus: 1500 };
const HOLD_PRESENT = 5000;
// When the display is more than LAG_MAX behind the log, it catches up at the next change of run (never mid-run):
// everything older than LAG_TARGET is shown at once. A run in focus that says nothing for STALL lets the next one in.
const LAG_MAX = 30000, LAG_TARGET = 4000, STALL = 45000;

const $ = (id) => document.getElementById(id);
const str = (v) => (typeof v === "string" ? v : v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));
const obj = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : {});
const esc = (s) => str(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const norm = (s) => STATUS[str(s).toLowerCase()] || "info";
const stateVar = (s) => (STATES.includes(s) ? `var(--${s})` : "var(--dim)");
const actorVar = (a) => (ACTORS.includes(a) ? `var(--${a})` : "var(--dim)");
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
// "new-tests-fail-on-base" -> "new tests fail on base"; text that isn't a slug is left alone.
const slug = (s) => (/^[\w.+]+(?:[-_][\w.+]+)+$/.test(s) ? s.replace(/[-_]+/g, " ") : s);
const short = (id) => str(id).replace(/^run-\d{8}-/, "run-");
const num = (v) => (typeof v === "number" && Number.isFinite(v) && v > 0 ? v : 0);
const usd = (v) => (!v ? "$0" : v >= 1 ? `$${v.toFixed(2)}` : v >= 0.01 ? `$${v.toFixed(3)}` : v >= 0.0001 ? `$${v.toFixed(4)}` : "<$0.0001");
const tok = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n));
// data.usage, as the contract will have it: tokens, cost, turns. Missing or wrong fields count as 0.
const usageOf = (u) => ({ cost: num(u.cost_usd), turns: num(u.turns), input: num(u.input_tokens), output: num(u.output_tokens),
  cacheRead: num(u.cache_read_tokens), cacheWrite: num(u.cache_creation_tokens) });
const addUsage = (a, b) => { for (const k in b) a[k] += b[k]; return a; };
const tokens = (u) => u.input + u.output + u.cacheRead + u.cacheWrite;
// What a run cost: the outcome's summed usage when it has one, else what its lines reported (older logs carry
// only data.cost_usd), never less than an outcome's own cost_usd.
function spend(run) {
  if (run.final) return run.final;
  return { ...run.used, cost: Math.max(run.used.cost, run.finalCost) };
}

let S, fx, live = false, pinned = null, queued = false, focus, seen, shown;
function reset() {
  S = { runs: new Map(), order: [], rules: new Map(), synthetic: false, bad: 0, seq: 0 };
  fx = new Set(); focus = { id: null, until: 0 }; seen = new Set(); shown = undefined;
}
reset();
let marks = []; // what the line being read changed, to pace the next one
const mark = (k) => { if (live) { fx.add(k); marks.push(k); } };

function ingest(raw) {
  let e;
  try { e = JSON.parse(raw); } catch { S.bad++; return; }
  if (!e || typeof e !== "object" || Array.isArray(e)) { S.bad++; return; }
  const data = obj(e.data);
  if (data.synthetic === true) S.synthetic = true;
  const id = str(e.run_id) || str(e.event_id);
  if (!id) return; // a line about no run (like the fixture header)

  let run = S.runs.get(id);
  if (!run) {
    run = { id, title: "", source: "", kind: "", steps: new Map(), checks: new Map(), feed: [],
      outcome: null, review: null, seq: 0, acts: [], used: usageOf({}), final: null, finalCost: 0 };
    S.runs.set(id, run); S.order.push(id);
  }
  // The engine logs an agent's tool calls and usage under step "agent": they belong to that agent's own step.
  const AGENT_STEP = { builder: "build", reviewer: "review", reader: "intake", checker: "learn" };
  const step = str(e.step) === "agent" ? AGENT_STEP[str(data.agent)] || "build" : str(e.step) || "?";
  const status = norm(e.status);
  const actor = str(e.actor).toLowerCase();
  const detail = str(e.detail);
  const seq = ++S.seq;
  // One line per tool call an agent makes: actor agent with data.tool (data.agent says which agent).
  const isAction = actor === "agent" && typeof data.tool === "string" && data.tool !== "";
  const u = data.usage && typeof data.usage === "object" && !Array.isArray(data.usage) ? usageOf(data.usage) : null;
  if (step === "outcome") { if (u) run.final = u; else run.finalCost = Math.max(run.finalCost, num(data.cost_usd)); }
  else if (u) addUsage(run.used, u);
  else run.used.cost += num(data.cost_usd);
  if (isAction) {
    run.acts.push({ k: seq, tool: data.tool, agent: str(data.agent), detail, step });
    run.acts = run.acts.slice(-6);
    mark(`act:${id}:${seq}`);
  }

  if (data.title) run.title = str(data.title);
  else if (!run.title && step === "intake" && detail) run.title = detail;
  if (data.source) run.source = str(data.source);
  if (data.kind) run.kind = str(data.kind);

  // Checks. One per line: data.check, or data.step on a verify line (the contract's shape), with data.ok or
  // the line's status. Several at once: data.checks or data.steps, a list or a {name: status} map.
  const nameOf = (o) => [o.check, o.name, o.step, o.id].find((n) => typeof n === "string" && n) || "";
  const okOf = (o, dflt) => (typeof o.ok === "boolean" ? (o.ok ? "ok" : "failed")
    : o.status != null ? norm(o.status) : o.result != null ? norm(o.result) : dflt);
  const lineCheck = typeof data.check === "string" || (step === "verify" && typeof data.step === "string") ? nameOf(data) : "";
  const setCheck = (name, st, d, label) => {
    if (!name) return;
    const c = run.checks.get(name);
    if (!c || c.status !== st) mark(`check:${id}:${name}`);
    run.checks.set(name, { status: st, detail: str(d) || c?.detail || "", label: str(label) || c?.label || "", step });
  };
  const lineStatus = lineCheck ? okOf(data, status) : status;
  if (lineCheck) setCheck(lineCheck, lineStatus, detail, data.label);
  const many = Array.isArray(data.checks) || (data.checks && typeof data.checks === "object") ? data.checks
    : Array.isArray(data.steps) ? data.steps : null;
  if (Array.isArray(many)) {
    for (const c of many) {
      if (typeof c === "string") setCheck(c, status, "");
      else if (c && typeof c === "object") setCheck(nameOf(c), okOf(c, status), c.detail, c.label);
    }
  } else if (many) {
    for (const [k, v] of Object.entries(many)) setCheck(k, v && typeof v === "object" ? okOf(v, status) : norm(v), obj(v).detail);
  }

  // Review: a human, or Claude playing one (data.played_by), approves, asks for changes or answers a question.
  const by = actor === "human" ? str(data.played_by) : ""; // who plays the human; other actors aren't played
  let stepStatus = lineStatus;
  if (step === "review") {
    const decision = str(data.decision);
    if (/^(approve|yes)\b/i.test(decision)) stepStatus = "ok";
    else if (/^(no|request|reject)/i.test(decision)) stepStatus = "failed";
    if (decision || data.text || detail) {
      run.review = { by, decision, reason: str(data.reason || data.text), detail, status: stepStatus,
        question: str(data.question), of: str(data.run_id) };
      mark(`review:${id}`);
    }
  }

  const prev = run.steps.get(step);
  // A passing check doesn't finish its step: it stays running until the step's own summary line.
  if (lineCheck) stepStatus = lineStatus === "failed" || prev?.status === "failed" ? "failed" : "started";
  const agentLine = str(e.step) === "agent"; // a tool call or an agent's usage: neither ends nor re-owns the step
  if (isAction || agentLine) stepStatus = prev ? prev.status : "started";
  run.steps.set(step, { actor: (agentLine && prev?.actor) || actor || prev?.actor || "", by: by || prev?.by || "",
    status: stepStatus });
  if (!prev || prev.status !== stepStatus || prev.actor !== actor) mark(`step:${id}:${step}`);

  // Learned rules: data.rule as an object, or as the rule's id with text/status/born_from beside it (the
  // contract's shape); or data.rules, a list. A later line with the same id updates the rule.
  const rules = Array.isArray(data.rules) ? data.rules
    : data.rule && typeof data.rule === "object" ? [data.rule]
    : typeof data.rule === "string" ? [{ id: data.rule, text: data.text, status: data.status, born_from: data.born_from }] : [];
  for (const r of rules) {
    const o = typeof r === "string" ? { id: r } : obj(r);
    const rid = str(o.id) || str(o.text);
    if (!rid) continue;
    const old = S.rules.get(rid);
    const rule = { id: str(o.id), text: str(o.text) || old?.text || "", from: str(o.born_from) || old?.from || id,
      status: str(o.status) || old?.status || "proposed", seq: old?.seq ?? seq };
    if (!old || old.status !== rule.status) mark(`rule:${rid}`);
    S.rules.set(rid, rule);
  }

  if (step === "outcome") {
    run.outcome = { state: str(data.state).toLowerCase() || "unknown", reason: str(data.reason), detail,
      ask: str(data.ask || data.question || data.why), need: str(data.needs || data.need) };
    mark(`state:${id}`); mark(`count:${run.outcome.state}`);
  }
  if (detail && !isAction && (!lineCheck || lineStatus === "failed")) {
    run.feed.push({ k: seq, step, actor, by, status: stepStatus, detail });
    run.feed = run.feed.slice(-4);
    mark(`feed:${id}:${seq}`);
  }
  run.seq = seq; run.last = step;

  // A line for the run in focus that finishes it (or follows its outcome) starts or extends the hold.
  if (live && id === focus.id && run.outcome) focus.until = Math.max(focus.until, Date.now() + holdFor());
}

const waiting = () => S.order.filter((id) => !seen.has(id));
const holdFor = () => { if (PRESENT) return HOLD_PRESENT; const n = waiting().length; return n === 0 ? HOLD : n < 3 ? HOLD_BUSY : HOLD_RUSH; };

function setFocus(id) {
  focus.id = id; seen.add(id); focusLineAt = Date.now();
  focus.until = S.runs.get(id)?.outcome ? Date.now() + holdFor() : 0;
}

// Never leave a run mid-way; once it has finished and its hold is over, go to the oldest run not shown yet.
function pickFocus() {
  if (!live) return;
  const cur = S.runs.get(focus.id);
  if (cur && (!cur.outcome || Date.now() < focus.until)) return;
  const next = waiting()[0];
  if (next) setFocus(next);
}

// When the backlog has been read: show the oldest run still going, else the latest one; the rest count as seen.
function onReady() {
  const runs = S.order.map((id) => S.runs.get(id));
  const target = runs.find((r) => !r.outcome) || runs[runs.length - 1];
  for (const id of S.order) seen.add(id);
  if (target) setFocus(target.id);
}

/* ---------- drawing: patch in place ---------- */

const setText = (el, t) => { if (el.textContent !== t) el.textContent = t; };
const setHTML = (el, h) => { if (el._h !== h) { el._h = h; el.innerHTML = h; return true; } return false; };
function flash(el) {
  if (!el) return;
  el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); el._f = true;
  clearTimeout(el._ft); el._ft = setTimeout(() => { el.classList.remove("flash"); el._f = false; }, 2600);
}
// items: [{k, id, cls, style, html, title, flash}], reusing elements by key so untouched ones keep their state.
function patch(parent, items, tag = "li") {
  const old = new Map();
  for (const c of [...parent.children]) { if (c.dataset.k != null) old.set(c.dataset.k, c); else c.remove(); }
  items.forEach((it, i) => {
    let el = old.get(it.k);
    if (el) old.delete(it.k); else { el = document.createElement(tag); el.dataset.k = it.k; if (it.id) el.id = it.id; }
    if (el._c !== it.cls) { el._c = it.cls; el.className = it.cls + (el._f ? " flash" : ""); }
    const st = it.style || "";
    if (el._s !== st) { el._s = st; el.style.cssText = st; }
    setHTML(el, it.html);
    if (it.title != null && el.title !== it.title) el.title = it.title;
    if (parent.children[i] !== el) parent.insertBefore(el, parent.children[i] || null);
    if (it.flash) flash(el);
  });
  for (const el of old.values()) el.remove();
}

function render() {
  queued = false;
  try { pickFocus(); draw(); } catch (err) { console.error(err); }
  fx.clear();
}
const schedule = () => { if (!queued) { queued = true; setTimeout(render, 16); } }; // not rAF: it stops in background tabs
setInterval(() => { const before = focus.id; pickFocus(); if (focus.id !== before) schedule(); }, 250);

function draw() {
  $("synthetic").hidden = !S.synthetic;
  setText($("footer"), S.bad ? `${S.bad} unreadable line${S.bad > 1 ? "s" : ""} skipped` : "");

  const run = S.runs.get(pinned) || S.runs.get(focus.id);
  const switched = (run?.id ?? null) !== shown;
  if (switched && shown !== undefined && run) { flash($("focus")); flash($("gate")); }
  shown = run?.id ?? null;
  const f = (k) => !switched && fx.has(k); // per-element motion only within the same run

  // counters
  const n = Object.fromEntries(STATES.map((s) => [s, 0]));
  let running = 0, other = 0;
  for (const r of S.runs.values()) {
    if (!r.outcome) running++;
    else if (r.outcome.state in n) n[r.outcome.state]++;
    else other++;
  }
  const cells = [["running", running, "var(--running)"], ...STATES.map((s) => [s, n[s], stateVar(s)])];
  if (other) cells.push(["other", other, "var(--dim)"]);
  patch($("counters"), cells.map(([l, v, c]) => ({ k: l, id: `count-${l}`, cls: `count${v ? "" : " zero"}`, style: `--c:${c}`,
    html: `<span class="n">${v}</span><span class="l">${l}</span>`, flash: fx.has("count:" + l) })), "div");

  // money: everything spent, what each success cost on average, and what flawed attempts burned
  let spent = 0, failed = 0;
  for (const r of S.runs.values()) {
    const c = spend(r).cost;
    spent += c;
    if (r.outcome?.state === "flawed") failed += c;
  }
  $("money").classList.toggle("none", !spent); // its place stays reserved
  patch($("money"), [["spent", usd(spent)], ["per success", n.success ? usd(spent / n.success) : "—"],
    ["failed attempts", usd(failed)]].map(([l, v]) => ({ k: l, id: `money-${l.replace(/ /g, "-")}`, cls: "m",
    html: `<span class="v">${v}</span><span class="l">${l}</span>` })), "div");

  drawFocus(run, f);
  drawGate(run, f);

  // runs: newest first by when they started, so the list doesn't reshuffle on every line
  const recent = S.order.slice(-4).reverse().map((id) => S.runs.get(id));
  patch($("runs"), recent.length ? recent.map((r) => {
    const st = r.outcome?.state;
    const now = st || r.last || "";
    return { k: r.id, cls: `${r.outcome ? "" : "live"}${r === run ? " focus" : ""}`, title: r.title || r.id,
      style: `--c:${st ? stateVar(st) : "var(--dim)"}`, flash: fx.has(`state:${r.id}`),
      html: `<span class="d"></span><span class="t">${esc(r.title || r.id)}</span>${spend(r).cost ? `<span class="c">${usd(spend(r).cost)}</span>` : ""}<span class="s">${esc(now)}</span>` };
  }) : [{ k: "empty", cls: "empty", html: "None yet" }]);

  const rules = [...S.rules.entries()].sort((a, b) => b[1].seq - a[1].seq).slice(0, 3);
  patch($("rules"), rules.length ? rules.map(([k, r]) => ({ k, cls: "", title: r.id, flash: fx.has("rule:" + k),
    html: `<div class="top"><span class="st ${r.status === "active" ? "active" : ""}">${esc(r.status)}</span>
      <span class="from">from ${esc(short(r.from))}</span></div><div class="txt">${esc(ruleText(r))}</div>` }))
    : [{ k: "empty", cls: "empty", html: "None yet" }]);
}

// A rule as a person reads it: its text, else its id without the "rule-" prefix.
const ruleText = (r) => (r.text && r.text !== r.id ? r.text : slug(str(r.id).replace(/^rule[-_:]/i, "")));

function actorHTML(actor, by) {
  return `${esc(actor)}${by ? `<small>(${esc(cap(by))})</small>` : ""}`;
}

function drawFocus(run, f) {
  // for the edit: which run is in focus and how it stands, on the panel itself
  const fp = $("focus"), st = run ? run.outcome?.state || "running" : "";
  if (fp.dataset.run !== (run?.id || "")) fp.dataset.run = run?.id || "";
  if (fp.dataset.state !== st) fp.dataset.state = st;
  if (!run) {
    setText($("run-meta"), "");
    setText($("run-title"), live ? "Waiting for the first event…" : "Connecting…");
    setHTML($("run-state"), ""); setText($("run-cost"), ""); patch($("track"), []); patch($("feed"), []); patch($("ask"), []);
    showNow("idle");
    return;
  }
  setHTML($("run-meta"), `${run.source ? `<span class="src">${esc(run.source)}</span>` : ""}<span class="id">${esc(short(run.id))}${run.kind ? " · " + esc(slug(run.kind)) : ""}</span>`);
  setText($("run-title"), run.title || run.id);
  $("run-title").title = run.title || run.id;

  const o = run.outcome;
  if (setHTML($("run-state"), o ? `<div class="badge" style="--c:${stateVar(o.state)}">${esc(o.state)}</div>`
    : `<div class="badge running">running</div>`) && o && f("state:" + run.id)) flash($("run-state").firstChild);

  const sp = spend(run), tk = tokens(sp);
  setText($("run-cost"), [sp.cost ? usd(sp.cost) : "", tk ? `${tok(tk)} tokens` : "", sp.turns ? `${sp.turns} turn${sp.turns === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · "));
  $("run-cost").title = tk ? `input ${sp.input}, output ${sp.output}, cache read ${sp.cacheRead}, cache write ${sp.cacheWrite}` : "";

  const steps = [...STEPS, ...[...run.steps.keys()].filter((s) => !STEPS.includes(s))];
  patch($("track"), steps.map((s) => {
    const st = run.steps.get(s);
    if (!st) return { k: s, id: `step-${s}`, cls: "node", style: "", html: `<span class="dot"></span><span class="name">${esc(s)}</span><span class="actor"></span>` };
    const end = s === "outcome" && o;
    return { k: s, id: `step-${s}`, cls: `node reached ${end ? "end" : o && st.status === "started" ? "info" : st.status}`,
      style: `--a:${actorVar(st.actor)};--c:${o ? stateVar(o.state) : "var(--dim)"};--g:${end ? stateVar(o.state) : "#fff"}`,
      html: `<span class="dot"></span><span class="name">${esc(s)}</span><span class="actor">${actorHTML(st.actor, st.by)}</span>`,
      flash: f(`step:${run.id}:${s}`) };
  }));

  // The box where the human is asked, and what the human (or Claude playing one) decided. The engine logs the
  // review (with data.question) just before a manual or blocked outcome, so the question can come from either line.
  const parts = [];
  const rv = run.review;
  const stopped = o && (o.state === "manual" || o.state === "blocked");
  const word = (s) => /^[\w-]{1,32}$/.test(s); // a need like "real-renfe-data" fits a heading; a sentence doesn't
  const q = rv?.question || (stopped ? o.ask || (!word(o.need) && o.need) || o.detail : "");
  if (q) {
    const head = o?.state === "blocked" ? (word(o.need) ? `Needs · ${slug(o.need)}` : "Needs") : "Asks the maintainer";
    parts.push({ k: "q", id: "ask-q", cls: "q", style: `--c:${o ? stateVar(o.state) : "var(--manual)"}`, flash: f("review:" + run.id) || f("state:" + run.id),
      html: `<b>${esc(head)}</b><p>${esc(q)}</p>` });
  }
  if (rv) {
    const who = rv.by ? `Human (${cap(rv.by)})` : "Human";
    const kind = q ? "answer" : rv.of ? `correction of ${short(rv.of)}` : "review";
    const what = rv.decision ? (word(rv.decision) ? cap(slug(rv.decision)) : rv.decision) : rv.reason || rv.detail;
    const why = rv.reason && rv.reason !== what ? rv.reason : "";
    parts.push({ k: "a", id: "ask-a", cls: `a ${rv.status}`, flash: f("review:" + run.id),
      html: `<b>${esc(who)} · ${esc(kind)}</b><p>${rv.decision ? `<span class="dec">${esc(what)}</span>` : esc(what)}${why ? ` <span class="why">${esc(why)}</span>` : ""}</p>` });
  }
  patch($("ask"), parts, "div");

  // What an agent is doing right now: its latest tool calls, newest on top, while its step is still going.
  const lastAct = run.acts[run.acts.length - 1];
  const working = !o && !!lastAct && run.steps.get(lastAct.step)?.status === "started";
  if (working) {
    const acts = run.acts.filter((a) => a.agent === lastAct.agent).slice(-3).reverse();
    setText($("act-who"), `${lastAct.agent || "agent"} · now`);
    patch($("acts"), acts.map((a, i) => ({ k: `${run.id}:${a.k}`, cls: `age${i}`, flash: f(`act:${run.id}:${a.k}`),
      html: `<span class="tool">${esc(a.tool)}</span><span class="txt">${esc(a.detail)}</span>` })));
  } else patch($("acts"), []);

  // How the run ended, when no question or answer says it already.
  const said = !parts.length && !working && o ? o.detail || o.reason : "";
  if (said) {
    setText($("said-head"), `${o.state}${o.reason && word(o.reason) ? ` · ${slug(o.reason)}` : ""}`);
    setText($("said-text"), said);
    if ($("said")._st !== o.state) { $("said")._st = o.state; $("said").style.setProperty("--c", stateVar(o.state)); }
    if (f("state:" + run.id)) flash($("said"));
  }
  showNow(parts.length ? "ask" : working ? "activity" : said ? "said" : "idle");

  // What the box already says (the review, the outcome) isn't repeated in the feed.
  const feed = run.feed.filter((x) => !((parts.length && x.step === "review") || ((q || said) && x.step === "outcome"))).slice(-2);
  patch($("feed"), feed.map((x, i) => ({ k: `${run.id}:${x.k}`, cls: `${i < feed.length - 2 ? "old " : ""}${x.status}`,
    style: `--a:${actorVar(x.actor)}`, flash: f(`feed:${run.id}:${x.k}`),
    html: `<span class="who">${actorHTML(x.actor, x.by)}</span><span class="txt">${esc(x.detail)}</span>` })));
}

// The "now" box shows one thing at a time; its size never changes.
function showNow(mode) {
  for (const m of ["ask", "activity", "said", "idle"]) if ($(m).hidden !== (m !== mode)) $(m).hidden = m !== mode;
  if ($("now").dataset.mode !== mode) $("now").dataset.mode = mode;
}

// A check as a person reads it: a rule check shows the rule's text; other names lose their dashes.
function checkLabel(name, c) {
  const m = name.match(/^rule\b[:\s]*(.*)$/i);
  if (!m) return { rule: false, text: c.label || slug(name) };
  const rule = S.rules.get(m[1].trim());
  if (rule && rule.text && rule.text !== rule.id) return { rule: true, text: rule.text };
  const d = (c.detail.startsWith(name) ? c.detail.slice(name.length) : c.detail).replace(/^[:\s]+/, "").replace(/[:\s]+(ok|passed|failed?)\s*$/i, "");
  return { rule: true, text: c.label || d || slug(m[1].replace(/^rule[-_:]/i, "")) || name };
}

function drawGate(run, f) {
  let checks = run ? [...run.checks.entries()] : [];
  // Show one step's checks: the gate's when it ran, else the intake's (rejected and early-stopped runs).
  const gate = checks.filter(([, c]) => c.step === "verify");
  if (gate.length) checks = gate;
  const label = gate.length || !checks.length ? "Gate" : `${cap(checks[0][1].step)} checks`;
  if (!checks.length) {
    setText($("gate-title"), label); $("checks").classList.remove("many");
    patch($("checks"), [{ k: "empty", cls: "empty", html: run ? (run.outcome ? "Not reached in this run" : "Not reached yet") : "—" }]);
    return;
  }
  const pass = checks.filter(([, c]) => c.status === "ok").length;
  const fail = checks.filter(([, c]) => c.status === "failed").length;
  setText($("gate-title"), `${label} · ${pass} passed${fail ? ` · ${fail} failed` : ""}`);
  $("checks").classList.toggle("many", checks.length > 14);
  const icon = { ok: "✓", failed: "✕", skipped: "–", started: "", info: "·" };
  patch($("checks"), checks.map(([name, c]) => {
    const l = checkLabel(name, c);
    return { k: name, cls: `check ${c.status}${l.rule ? " rule" : ""}`, title: c.detail || name,
      style: `--g:${c.status === "failed" ? "var(--flawed)" : "var(--success)"}`, flash: f(`check:${run.id}:${name}`),
      html: `<span class="m">${icon[c.status] ?? ""}</span><span class="t">${esc(l.text)}</span>` };
  }));
}

$("runs").addEventListener("click", (ev) => {
  const li = ev.target.closest("li[data-k]");
  if (!li || li.dataset.k === "empty") return;
  pinned = pinned === li.dataset.k ? null : li.dataset.k;
  schedule();
});
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { pinned = null; schedule(); } });

/* ---------- presentation mode: pace what is shown, never what is read ---------- */

// Lines that arrive once the backlog is read wait here and are shown one by one. window.__shown says when each was
// shown (epoch ms), for matching the footage to the log: the display runs behind the log by design.
let Q = [], nextAt = 0, focusLineAt = 0;
window.__shown = [];
const note = (o) => window.__shown.push({ at: Date.now(), ...o });
const runOf = (raw) => { try { const e = JSON.parse(raw); return str(e?.run_id) || str(e?.event_id); } catch { return ""; } };

function read(raw) {
  marks = [];
  try { ingest(raw); } catch (err) { S.bad++; console.error(err); }
  return marks;
}

// How long what this line changed should stay on screen before the next change.
function costOf(ks, id) {
  let c = 0;
  for (const k of ks) {
    const [kind, rid, name] = k.split(":");
    if (kind === "rule") c = Math.max(c, PACE.rule); // the rules panel is shown whatever run is in focus
    if (kind === "count" || (rid && rid !== id) || id !== focus.id) continue;
    if (kind === "check") c = Math.max(c, S.runs.get(id)?.checks.get(k.slice(`check:${id}:`.length))?.status === "failed" ? PACE.fail : PACE.check);
    else if (kind === "act") c = Math.max(c, Date.now() - (Q[0]?.t ?? Date.now()) > 8000 ? 300 : PACE.act);
    else if (PACE[kind]) c = Math.max(c, PACE[kind]);
  }
  return c;
}

function pace() {
  if (!Q.length || Date.now() < nextAt) return;
  const now = Date.now();
  const cur = S.runs.get(focus.id);
  const busy = cur && (!cur.outcome || now < focus.until);
  let i = 0, stalled = false;
  if (busy) {
    // While a run is in focus, only its lines move on, plus lines of runs already shown and finished; the next
    // runs wait their turn, so each one is seen from its first step.
    i = Q.findIndex((q) => !q.id || q.id === cur.id || (seen.has(q.id) && S.runs.get(q.id)?.outcome));
    if (i < 0) {
      if (cur.outcome || now - focusLineAt < STALL) return;
      i = 0; stalled = true; // the run in focus has gone quiet for long: let the next one in
      note({ stall: cur.id });
    }
  } else if (now - Q[0].t > LAG_MAX) {
    catchUp(now);
    return;
  }
  const q = Q.splice(i, 1)[0];
  const before = focus.id;
  const ks = read(q.raw);
  if (q.id && !seen.has(q.id) && (!busy || stalled)) setFocus(q.id);
  if (q.id === focus.id) focusLineAt = now;
  const e = (() => { try { return JSON.parse(q.raw); } catch { return {}; } })();
  note({ run: q.id, step: str(e.step), ts: str(e.ts), event: str(e.event_id), focus: focus.id, changed: ks.length });
  nextAt = now + (focus.id !== before ? Math.max(PACE.focus, costOf(ks, q.id)) : costOf(ks, q.id));
  schedule();
}

// Far behind at a change of run: show at once everything older than LAG_TARGET, then go on from there. Runs that
// end inside the jump count as shown; the focus goes to the oldest one still going, else the latest.
function catchUp(now) {
  const n = Q.findIndex((q) => now - q.t <= LAG_TARGET);
  const batch = Q.splice(0, n < 0 ? Q.length : n);
  const was = live; live = false; // no motion for what is skipped
  const touched = [];
  for (const q of batch) { read(q.raw); if (q.id && !touched.includes(q.id)) touched.push(q.id); }
  live = was;
  const runs = touched.map((id) => S.runs.get(id)).filter(Boolean);
  for (const r of runs) if (r.outcome) seen.add(r.id);
  const target = runs.find((r) => !r.outcome) || runs[runs.length - 1];
  if (target) { setFocus(target.id); focusLineAt = now; }
  note({ catchup: batch.length, focus: focus.id });
  nextAt = now + PACE.focus;
  schedule();
}
if (PRESENT) { document.documentElement.classList.add("present"); setInterval(pace, 50); }

// A window shorter than the 1080 px the layout is drawn for is scaled down to fit, so no block ever changes size.
function fit() {
  const z = innerWidth > 720 ? Math.min(1, innerHeight / 1080) : 1;
  document.documentElement.style.zoom = z === 1 ? "" : String(z);
  document.body.style.height = innerWidth > 720 ? `${Math.max(1080, innerHeight / z)}px` : "";
}
addEventListener("resize", fit);
fit();

function connect() {
  const es = new EventSource("/events");
  // A reset mid-stream (the log was truncated or replaced, as a looping replay does) keeps the page live.
  es.addEventListener("reset", () => { reset(); Q = []; nextAt = 0; pinned = null; note({ reset: true }); schedule(); });
  es.addEventListener("ready", () => { onReady(); live = true; $("conn").classList.add("live"); note({ ready: S.order.length }); schedule(); });
  es.onmessage = (m) => {
    if (PRESENT && live) { Q.push({ raw: m.data, id: runOf(m.data), t: Date.now() }); return; }
    read(m.data); schedule();
  };
  es.onerror = () => { $("conn").classList.remove("live"); };
}
connect();
render();
