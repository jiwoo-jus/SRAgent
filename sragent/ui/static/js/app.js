const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const trunc = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
let S = null, M = null, tab = "steps", showSnoozed = false, lastJobId = null;
const answers = {};            // todo id -> {option, text, extra}
let STAGES = [];               // [key, label] of every stage, in order: the server's registry (sragent/stagespec.py)
const VIEWS = ["steps","stage","needs","studies","settings","activity","configs"];
const TITLES = {steps:["Overview","Where the review stands and the next thing to do"], stage:["Stage",""], needs:["Needs you","Everything waiting for a person, by stage"],
  settings:["Settings","Models per stage, the spending cap, evaluations"], todo:["To-do","Decisions and checks waiting for you"],
  studies:["Studies","Every record, its status and the evidence behind it"], pdfs:["Full texts","PDFs that would help the agent, and how to add them"],
  run:["Run","Start the agent, choose a model and see the cost first"], activity:["Activity","Jobs, proposals, comments and the full change history"],
  configs:["Configs","Every run file: open one, edit it, or set up a new review"]};

/* ---------------- icons ---------------- */
const IC = {
  check:'<path d="M20 6 9 17l-5-5"/>', x:'<path d="M18 6 6 18M6 6l12 12"/>', arrowR:'<path d="M5 12h14M13 6l6 6-6 6"/>', arrowL:'<path d="M19 12H5M11 18l-6-6 6-6"/>',
  chevR:'<path d="m9 18 6-6-6-6"/>', search:'<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  copy:'<rect width="13" height="13" x="9" y="9" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  download:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
  refresh:'<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>', play:'<path d="m7 4 13 8-13 8z"/>',
  stop:'<rect x="6" y="6" width="12" height="12" rx="1.5"/>', ext:'<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  todo:'<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8M13 12h8M13 18h8"/>',
  file:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M12 18v-6M9 15l3 3 3-3"/>',
  report:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8M16 17H8M10 9H8"/>',
  warn:'<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3z"/><path d="M12 9v4M12 17h.01"/>',
  info:'<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>', folder:'<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  msg:'<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>', bot:'<rect width="18" height="10" x="3" y="11" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4M8 16h.01M16 16h.01"/>',
  edit:'<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>', flag:'<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><path d="M4 22v-7"/>',
  sun:'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  moon:'<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z"/>', auto:'<circle cx="12" cy="12" r="9"/><path d="M12 3v18" /><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/>',
  clock:'<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
};
const ic = (n, cls) => `<svg class="i${cls ? " " + cls : ""}" viewBox="0 0 24 24">${IC[n] || ""}</svg>`;

/* ---------------- small helpers ---------------- */
/* the open run file: ?config=<path> in the page URL (none = the one `sragent ui --config` was started with) */
const CFG = new URLSearchParams(location.search).get("config") || "";
const U = path => CFG ? path + (path.includes("?") ? "&" : "?") + "config=" + encodeURIComponent(CFG) : path;
document.addEventListener("DOMContentLoaded", () => { const r = document.getElementById("reportlink"); if (r && CFG) r.href = U("/report"); });
async function api(path, body){
  const r = await fetch(U(path), body ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)} : {});
  try { return await r.json(); } catch(e) { return {ok:false, error:`HTTP ${r.status}`}; }
}
const badge = (text, tone, cls) => `<span class="badge ${tone||""} ${cls||""}">${esc(text)}</span>`;
function tone(s){
  s = String(s || "").toLowerCase();
  if (/^(done|accepted|included|include|met|validated|low|located|resolved|added|confirmed)/.test(s)) return "ok";
  if (/^(running|queued|ready to run)/.test(s)) return "info";
  if (/^(failed|high|not_met|not_found|bad|interrupted|stopped)/.test(s)) return "bad";
  if (/^(needs you|proposed|open|some_concerns|awaiting|unclear|forward|draft)/.test(s)) return "warn";
  return "";
}
function chipFor(status){ const s = String(status||""); return s.startsWith("included") ? "ok" : s.startsWith("excluded") ? "" : s ? "warn" : ""; }
const JCOL = {low:"ok", some_concerns:"warn", high:"bad", unclear:""};
const JLAB = {low:"Low", some_concerns:"Some concerns", high:"High", unclear:"Unclear"};
const human = s => { s = String(s||"").replace(/_/g, " "); return s.charAt(0).toUpperCase() + s.slice(1); };
function fmtT(ts){ if (!ts) return ""; const d = new Date(ts*1000); return d.toLocaleDateString(undefined,{month:"short",day:"numeric"})+" "+d.toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"}); }
function fmtDur(s){ s = Math.max(0, Math.round(s)); return s < 60 ? s + "s" : s < 3600 ? Math.floor(s/60) + "m " + (s%60) + "s" : Math.floor(s/3600) + "h " + Math.floor(s%3600/60) + "m"; }
const usd = (v, d) => v == null ? "" : "$" + Number(v).toFixed(d ?? 2);
function msg(el, r){ const m = typeof el === "string" ? document.getElementById(el) : el; if (m) m.innerHTML = r.ok ? `<span class="saved">${ic("check","sm")} ${esc(r.message||"saved")}</span>` : `<span class="err">${esc(r.message||r.error||"failed")}</span>`; }
function toast(text, bad){
  const t = document.createElement("div"); t.className = "toast" + (bad ? " err" : "");
  t.innerHTML = ic(bad ? "warn" : "check") + `<span>${esc(text)}</span>`;
  $("#toasts").appendChild(t); setTimeout(() => t.remove(), bad ? 6000 : 3500);
}
function copyText(t, b){
  const done = () => { if (!b) return; const o = b.innerHTML; b.innerHTML = ic("check","sm") + " Copied"; setTimeout(() => b.innerHTML = o, 1200); };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(t).then(done, () => fallbackCopy(t, done));
  else fallbackCopy(t, done);
}
function fallbackCopy(t, done){
  const a = document.createElement("textarea"); a.value = t; a.style.position = "fixed"; a.style.opacity = "0"; document.body.appendChild(a); a.select();
  try { document.execCommand("copy"); done(); } catch(e) { toast("Copy failed; select the text and copy it by hand", true); }
  a.remove();
}
function scrollToId(id){ const el = document.getElementById(id); if (!el) return; if (el.tagName === "DETAILS") el.open = true; el.scrollIntoView({behavior:"smooth", block:"start"}); }
const lastSig = {};
function unchanged(key, v){ const s = JSON.stringify(v); if (lastSig[key] === s) return true; lastSig[key] = s; return false; }

/* ---------------- theme + mobile menu ---------------- */
function getTheme(){ try { return localStorage.getItem("sragent-theme") || "auto"; } catch(e) { return "auto"; } }
function applyTheme(){
  const t = getTheme();
  if (t === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", t);
  $("#themebtn").innerHTML = ic(t === "dark" ? "moon" : t === "light" ? "sun" : "auto");
  $("#themebtn").title = "Theme: " + t + " (click to change)";
}
function cycleTheme(){ const n = {auto:"light", light:"dark", dark:"auto"}[getTheme()]; try { localStorage.setItem("sragent-theme", n); } catch(e) {} applyTheme(); }
function toggleSide(v){ const o = v ?? !$("#side").classList.contains("open"); $("#side").classList.toggle("open", o); $("#scrim").classList.toggle("open", o); }

/* ---------------- routing (#/view or #/studies/<rid>) ---------------- */
function nav(t, rid){ const h = "#/" + t + (rid ? "/" + rid : ""); if (location.hash !== h) history.pushState(null, "", h); }
function show(t){ nav(t); activate(t); }
function activate(t, noload){
  tab = t;
  document.querySelectorAll("#tabs a[data-t]").forEach(b => b.classList.toggle("on", b.dataset.t === t));
  VIEWS.forEach(v => $("#v-"+v).hidden = v !== t);
  $("#ptitle").textContent = TITLES[t][0]; $("#psub").textContent = TITLES[t][1];
  toggleSide(false);
  window.scrollTo(0, 0);
  if (noload) return;
  render();
  if (NOCFG) return;      // no run file open: only the Configs page has data
  if (t === "settings") loadModels();
  if (t === "studies") loadStudies();
  if (t === "activity") loadActivity();
}
function go(hash){ if (location.hash !== hash) history.pushState(null, "", hash); route(); }
function route(){
  const [t0, rid, arg] = location.hash.replace(/^#\/?/, "").split("/");
  if (CF.dirty && !(t0 === "configs" && rid === "edit" && decodeURIComponent(arg||"") === CF.path)) {
    if (!confirm("Discard your unsaved changes to " + CF.path + "?")) { history.pushState(null, "", "#/configs/edit/" + encodeURIComponent(CF.path)); return; }
    CF.dirty = false;
  }
  const moved = {todo: "#/needs", pdfs: "#/stage/retrieve", run: "#/settings"}[t0];
  if (moved) { history.replaceState(null, "", moved); return route(); }
  const t = VIEWS.includes(t0) ? t0 : NOCFG ? "configs" : "steps";
  if (t === "configs") {
    CF.sub = ["edit","new-topic","new-run"].includes(rid) ? rid : "list"; CF.arg = decodeURIComponent(arg || "");
    document.body.dataset.ready = "1"; CF.shown = null; activate("configs"); return;
  }
  if (t === "stage") {
    const key = decodeURIComponent((rid || "").split("?")[0]);
    document.body.dataset.ready = "1";
    if (!(tab === "stage" && STG.key === key && STG.d)) { activate("stage", true); openStage(key); }
    else renderStage();
    stageNav();
    return;
  }
  if (t === "studies" && rid) { if (!(tab === "studies" && ST.rid === rid && ST.d)) openStudy(decodeURIComponent(rid)); return; }
  if (t === "studies" && ST.rid) { ST.rid = null; ST.d = null; }
  else if (t === tab && document.body.dataset.ready) return;
  document.body.dataset.ready = "1";
  activate(t);
}
window.addEventListener("popstate", route);
window.addEventListener("hashchange", route);
document.querySelectorAll("#tabs a[data-t]").forEach(a => a.addEventListener("click", e => { e.preventDefault(); const t = a.dataset.t; if (t === "studies") { ST.rid = null; ST.d = null; } if (t === "configs") go("#/configs"); else show(t); }));

let NOCFG = null;      // {error, config} when no run file is open or it fails to load
async function refresh(force){
  let st;
  try { st = await api("/api/state"); } catch(e){ return; }
  if (st && (st.no_config || st.config_error)) {
    const first = !NOCFG; S = null; NOCFG = {error: st.config_error || "", config: st.config || CFG, changes: st.config_changed || null};
    renderNoRun(); if (first && tab !== "configs") go("#/configs"); return;
  }
  S = st; NOCFG = null;
  if (!S || !S.run) return;
  if (S.registry) STAGES = S.registry.map(x => [x.key, x.label]);
  renderChrome();
  const j = S.job.current;
  if (lastJobId !== (j ? j.id : null)) {
    const was = lastJobId; lastJobId = j ? j.id : null;
    ST.stale = true;
    if (was && tab === "studies") { if (ST.rid) openStudy(ST.rid, true); else loadStudies(); }
  }
  if (force) ST.stale = true;
  render(!force);
}

function renderNoRun(){
  document.title = "Configs · SRAgent";
  $("#runname").textContent = NOCFG.changes ? "Paused: settings changed" : NOCFG.error ? "Run file does not load" : "No run file open";
  $("#topic").textContent = NOCFG.error ? NOCFG.config : "Open a run file, or set up a new review.";
  $("#mode").innerHTML = NOCFG.changes ? badge("settings changed", "warn", "dot") : NOCFG.error ? badge("config error", "bad", "dot") : "";
  document.querySelectorAll("#tabs a[data-t]").forEach(a => a.classList.toggle("off", a.dataset.t !== "configs"));
  $("#reportlink").classList.add("off");
}
function renderChrome(){
  document.querySelectorAll("#tabs a.off").forEach(a => a.classList.remove("off"));
  $("#reportlink").href = U("/report");
  document.title = S.run.name + " · SRAgent";
  $("#runname").textContent = S.run.name;
  $("#runname").parentElement.title = S.run.run_dir;
  $("#topic").textContent = S.run.topic || "No review question yet.";
  $("#topic").title = S.run.topic || "";
  const ao = S.run.human_mode === "agent_only";
  $("#mode").innerHTML = `<span class="badge ${ao ? "info" : ""} dot" title="${ao ? "Agent-only: the agent makes every judgment; people may only supply the full texts it asks for." : "Human in the loop: you approve, correct and confirm the agent's work."}">${ao ? "Agent-only" : "Human in the loop"}</span>`;
  $("#spent").textContent = usd(S.run.spent_usd);
  $("#needs-n").textContent = S.todos.filter(t => !t.snoozed && !t.optional).length || "";
  const ft = S.todos.find(t => t.kind === "fulltexts");
  // (the Full texts count is on the retrieve stage in the stage list)
  const j = S.job.current;
  $("#act-n").textContent = ((j ? 1 : 0) + (S.job.queued || 0)) || "";
  let w = "", top = "";
  if (j) {
    const el = j.started ? fmtDur(Date.now()/1000 - j.started) : "";
    w = `<div class="jobw run"><div class="h"><span class="spin"></span>Running<span class="mut" style="font-weight:400">${el ? "· " + el : ""}</span></div>
      <div class="l" title="${esc(j.label||j.action)}">${esc(j.label||j.action)}</div>
      ${S.job.queued ? `<div class="xs mut" style="margin-top:2px">${S.job.queued} more queued</div>` : ""}
      <div class="a"><button class="btn sm" onclick="show('run')">View log</button><button class="btn sm danger" onclick="stopRun()">${ic("stop","sm")} Stop</button></div></div>`;
    top = `<button class="btn sm" onclick="show('run')" title="${esc(j.label||j.action)}"><span class="spin"></span><span class="hide-sm">Running</span> ${esc(trunc(j.label||j.action, 28))}</button>`;
  } else if (S.job.last) {
    const L = S.job.last;
    w = `<div class="jobw" style="cursor:pointer" onclick="show('activity')" title="Open Activity"><div class="h">${badge(L.status, tone(L.status), "dot")}<span class="mut xs" style="font-weight:400">last job${L.ended ? " · " + fmtT(L.ended) : ""}</span></div>
      <div class="l">${esc(L.label||L.action)}</div></div>`;
  }
  $("#jobw").innerHTML = w; $("#topjob").innerHTML = top;
  if (typeof stageNav === "function") stageNav();
}

function render(fromPoll){
  if (tab === "configs") { if (!fromPoll) renderConfigs(); return; }
  if (!S) return;
  if (tab === "steps") renderSteps(fromPoll);
  if (tab === "stage" && STG.key) {
    const ae = document.activeElement;
    const typing = ae && $("#v-stage").contains(ae) && ["INPUT","TEXTAREA","SELECT"].includes(ae.tagName);
    if (!fromPoll) renderStage();
    else if (!typing && !unchanged("stage", [S.todos, S.steps, S.job, S.stale])) openStage(STG.key, true);
  }
  if (tab === "needs") renderNeeds(fromPoll);
  if (tab === "settings" && !fromPoll) renderSettings();
  if (tab === "activity") renderJobsCard(fromPoll);
}

/* ---------------- next step banner ---------------- */
function nextBanner(){
  const n = S.next || {};
  const label = {todo:"Open", run:"Open the stage", pdfs:"Open Full texts", report:"Open report"}[n.go] || "Go";
  const icon = {todo:"todo", run:"play", pdfs:"file", report:"report"}[n.go] || "arrowR";
  const running = !!S.job.current && n.go === "run";
  return `<div class="next"><div class="ni">${running ? `<span class="spin" style="border-color:rgba(255,255,255,.35);border-top-color:#fff;width:16px;height:16px"></span>` : ic(icon)}</div>
     <div class="grow"><div class="eyebrow">Next step</div><div class="nx">${esc(n.text||"")}</div></div>
     <button class="btn primary lg" onclick="goNext()">${label} ${ic("arrowR")}</button></div>`;
}
function goNext(){
  const n = S.next || {};
  if (n.go === "todo") goTodoGroup(n.group);
  else if (n.go === "run") presetRun(n.until || "report");
  else if (n.go === "pdfs") go("#/stage/retrieve");
  else window.open(U("/report"), "_blank");
}
/* every decision lives on its stage page; what belongs to no stage (answers to your feedback) is on Needs you */
const GROUP_STAGE = {protocol:"protocol", search:"search", eligibility:"eligibility", sync:"synthesis", fulltexts:"retrieve",
                     rob:"rob", ruled:"screen", fields:"extract"};
function goTodoGroup(group){ const s = GROUP_STAGE[group]; go(s ? "#/stage/" + s : "#/needs"); }
function goTodoStep(step){ go(step && step !== "feedback" ? "#/stage/" + step : "#/needs"); }
const RUN_OP = {protocol:"protocol.draft", search:"search.run", screen:"screen.run", retrieve:"retrieve.run", eligibility:"eligibility.run",
                extract:"extract.run", rob:"rob.run", synthesis:"synthesis.write", report:"report.render"};
function runTo(stage){ const o = RUN_OP[stage]; if (o) runOp(o, {}, `Run up to ${stage}`); }

/* ---------------- overview ---------------- */
function renderSteps(fromPoll){
  if ((!ST.list || ST.stale) && !ST.loading) {
    ST.loading = true;
    api("/api/studies").then(L => { ST.list = Array.isArray(L) ? L : []; ST.stale = false; ST.loading = false; if (tab === "steps") renderSteps(); })
                      .catch(() => { ST.loading = false; });
  }
  const L = ST.list;
  const g = k => L ? L.filter(r => r.group === k).length : null;
  if (fromPoll && unchanged("steps", [S.steps, S.next, S.run.spent_usd, S.ruled.length, !!S.job.current, L && L.length, g("included"), g("pending")])) return;
  if (!fromPoll) unchanged("steps", [S.steps, S.next, S.run.spent_usd, S.ruled.length, !!S.job.current, L && L.length, g("included"), g("pending")]);
  const v = x => x == null ? "–" : x.toLocaleString();
  const kpi = (label, val, sub, group, color) => `<button class="kpi" ${group ? `onclick="ST.group='${group}';ST.status='';show('studies')"` : `onclick="ACT.sub='usage';show('activity')"`} title="${group ? "Show these studies" : "LLM calls by stage"}">
     <div class="l">${color ? `<i style="background:${color}"></i>` : ""}${label}</div><div class="v">${val}</div><div class="s">${sub}</div></button>`;
  const kpis = `<div class="kpis">
    ${kpi("Records", v(L && L.length), "after duplicate removal", "all")}
    ${kpi("Included", v(g("included")), "after full-text review", "included", "var(--ok)")}
    ${kpi("Pending", v(g("pending")), "undecided or waiting for a PDF", "pending", "var(--warn)")}
    ${kpi("Excluded", v(g("excluded")), `${S.ruled.length} by publication-type rules`, "excluded", "var(--mut-2)")}
    ${kpi("Spent", usd(S.run.spent_usd), "API cost, this run folder", null)}</div>`;
  const nDone = S.steps.filter(s => s.status === "done").length;
  const rows = S.steps.map((s, i) => {
    const st = s.status === "done" ? "done" : s.status === "needs you" ? "you" : s.status === "ready to run" ? "ready" : "todo";
    const label = s.label.replace(/^\d+\.\s*/, "");
    const link = s.key !== "feedback" ? `#/stage/${s.key}` : "#/needs";
    const facts = String(s.summary || "").split(" · ").map(x => `<span>${esc(x)}</span>`).join(`<span class="sep">·</span>`);
    return `<li class="stp ${st}">
      <div class="si">${st === "done" ? ic("check","sm") : st === "you" ? "!" : st === "ready" ? ic("play","sm") : i + 1}</div>
      <div style="min-width:0"><div class="sh"><a class="stl" href="${link}"><b>${esc(label)}</b></a>${st === "you" ? badge("needs you","warn") : st === "ready" ? badge("ready to run","info") : st === "todo" ? badge("to do") : ""}</div>
        <div class="ss">${facts}</div></div>
      <div class="sa">${s.todos ? `<button class="btn sm" onclick="goTodoStep('${s.key}')">Review ${s.todos} ${ic("arrowR","sm")}</button>` : ""}
        ${RUN_OP[s.key] ? `<button class="btn sm ghost" onclick="runTo('${s.key}')" title="Run the review up to this stage (finished work is kept)">${ic("play","sm")} Run to here</button>` : ""}</div></li>`;
  }).join("");
  const stale = S.stale || {n:0};
  const staleBox = stale.n ? `<div class="callout warn">${ic("refresh")}<div class="grow">${stale.n} output${stale.n>1?"s are":" is"} out of date: a dependency changed after ${stale.n>1?"they were":"it was"} computed (${Object.entries(stale.by_type||{}).map(([t,n]) => `${n} ${t}`).join(", ")}${stale.pinned ? `; ${stale.pinned} decided or edited by you, kept` : ""}).${stale.est_usd != null ? ` Updating costs about ${usd(stale.est_usd)}.` : ""}
      <button class="link" onclick="goTodoGroup('sync')">See the plan and update</button></div></div>` : "";
  const of = S.open_flags || {n:0};
  const flagBox = of.n && (S.run||{}).human_mode === "agent_only" ? `<div class="callout info">${ic("flag")}<div class="grow">${of.n} open flag${of.n>1?"s":""} need${of.n>1?"":"s"} a person (${Object.entries(of.by_source||{}).map(([k,n]) => `${n} ${k}`).join(", ")}). In agent-only mode nobody answers them during the run; they are listed in the report under "Open flags".</div></div>` : "";
  $("#v-steps").innerHTML = nextBanner() + staleBox + flagBox + kpis + `
    <div class="card"><div class="card-h"><div class="grow"><h2>Review pipeline</h2><div class="sub">${nDone} of ${S.steps.length} steps done</div></div>
      <div style="width:160px" class="bar" title="${nDone} of ${S.steps.length} done"><i style="width:${Math.round(100*nDone/S.steps.length)}%"></i></div></div>
      <ol class="stepper">${rows}</ol></div>
    ${S.ruled.length ? `<div class="callout plain">${ic("info")}<div>${S.ruled.length} records were excluded automatically by publication-type rules (conference abstracts, reviews, editorials, off-topic records without an abstract).
      <button class="link" onclick="goTodoGroup('ruled')">See and restore them</button></div></div>` : ""}`;
}
function presetRun(stage){ go("#/stage/" + (stage === "report" ? "synthesis" : stage)); }   // its toolbar runs it

/* ---------------- to-do ---------------- */
const elig = {};     // rid -> {option, criterion, text}
const robSel = {};   // rid -> bool
const robEdit = {};  // rid -> {domain, judgment, text, open}
function renderTodo(){   // the To-do forms redraw the page they are on (a stage page, or Needs you)
  if (tab === "stage") renderStage(); else if (tab === "needs") renderNeeds(); else render();
}
/* protocol: ask the agent for a change in plain words, or edit criteria in place; then approve */
const PC = {edit:null, ask:"", busy:""};
const PC_CHIPS = ["Widen the design criterion to any non-randomised intervention study, including case series and case reports",
  "Add an English-language criterion", "Move the outcome criterion to the title/abstract stage", "Merge overlapping exclusion criteria"];
function protocolCard(t){
  const a = answers[t.id] || (answers[t.id] = {option:null, text:"", extra:{}});
  const TT = {inclusion:"ok", exclusion:"bad"};
  const stg = s => (s || "title_abstract").replace(/_/g, " / ").replace("title / abstract", "title/abstract");
  const rows = (t.criteria || []).map((c, i) => {
    if (PC.edit) {
      const e = PC.edit[i];
      return `<tr style="${e.remove ? "opacity:.45" : ""}"><td class="mono" style="font-weight:600">${esc(c.id)}</td>
        <td><select onchange="PC.edit[${i}].type=this.value">${["inclusion","exclusion","practical"].map(x => `<option ${e.type===x?"selected":""}>${x}</option>`).join("")}</select></td>
        <td class="hide-xs"><select onchange="PC.edit[${i}].stage=this.value">${["title_abstract","full_text"].map(x => `<option value="${x}" ${e.stage===x?"selected":""}>${stg(x)}</option>`).join("")}</select></td>
        <td><textarea rows="2" style="width:100%" oninput="PC.edit[${i}].text=this.value">${esc(e.text)}</textarea>
          <div class="row" style="gap:8px;margin-top:4px"><label class="xs mut">any of group <input style="width:48px" value="${esc(e.any_of||"")}" oninput="PC.edit[${i}].any_of=this.value"></label>
          <button class="link small" onclick="PC.edit[${i}].remove=!PC.edit[${i}].remove;renderTodo()">${e.remove ? "keep" : "remove"}</button></div></td></tr>`;
    }
    return `<tr><td class="mono" style="font-weight:600">${esc(c.id)}</td><td>${badge(human(c.type), TT[c.type]||"", "sq")}</td><td class="small mut nowrap hide-xs">${esc(stg(c.stage))}</td>
      <td>${esc(c.text)}${c.any_of ? ` <span class="xs mut">[any one of group ${esc(c.any_of)} suffices]</span>` : ""}</td></tr>`;
  }).join("");
  const revs = (t.revisions || []).map(r => `<div class="small" style="margin-top:6px;padding:8px 10px;border:1px solid var(--line);border-radius:8px">
      ${r.instruction ? `<span class="mut">You:</span> ${esc(r.instruction)}<br>` : ""}<span class="mut">${r.actor === "agent" ? "Agent:" : "Edited:"}</span> ${esc(r.explanation || r.reason || "")}
      ${r.changed && (r.changed.changed||[]).concat(r.changed.added||[], r.changed.removed||[]).length ? `<span class="xs mut">(changed ${esc((r.changed.changed||[]).join(", "))}${(r.changed.added||[]).length ? "; added " + esc(r.changed.added.join(", ")) : ""}${(r.changed.removed||[]).length ? "; removed " + esc(r.changed.removed.join(", ")) : ""})</span>` : ""}</div>`).join("");
  const opts = t.options.map(o => `<button class="choice ${a.option===o.value?"on":""}" onclick="pick('${t.id}','${o.value}')"><span class="rd"></span>${esc(o.label)}</button>`).join("");
  const hint = (t.options.find(o => o.value === a.option) || {}).hint;
  return `<div class="card tcard" id="c-protocol" data-step="protocol">
    <div class="card-h"><h3>${esc(t.title)}</h3>${t.pending ? badge("queued until the job finishes","info") : ""}
      ${PC.edit ? "" : `<button class="btn sm ghost" onclick="pcEditStart()">${ic("edit","sm")} Edit criteria</button>`}</div>
    <div class="card-b"><div class="q">${esc(t.question)}</div>
      <div class="tw" style="border:1px solid var(--line);border-radius:10px;margin-bottom:14px"><table class="crit"><thead><tr><th>ID</th><th>Type</th><th class="hide-xs">Applied at</th><th>Criterion</th></tr></thead><tbody>${rows}</tbody></table></div>
      ${PC.edit ? `<div class="row" style="gap:8px;margin-bottom:14px"><button class="btn primary" onclick="pcEditSave()">Save criteria (no LLM)</button><button class="btn" onclick="pcEditAdd()">Add a criterion</button><button class="btn" onclick="PC.edit=null;renderTodo()">Cancel</button></div>` : ""}
      <div style="margin-top:4px"><div class="small" style="font-weight:600">Ask the agent to change it</div>
        <div class="row" style="flex-wrap:wrap;gap:6px;margin:6px 0">${PC_CHIPS.map(c => `<button class="btn sm ghost" onclick="pcAsk(${JSON.stringify(c).replace(/"/g, "&quot;")})">${esc(c)}</button>`).join("")}</div>
        <div class="field"><input placeholder="e.g. split I2 by strategy type; drop E6; require a comparator" value="${esc(PC.ask)}" oninput="PC.ask=this.value" onkeydown="if(event.key==='Enter')pcAsk(this.value)">
          <button class="btn" onclick="pcAsk(PC.ask)" ${PC.busy ? "disabled" : ""}>Ask</button></div>
        <div class="xs mut">One call to the protocol stage's model; the revised criteria appear here for your approval.${PC.busy ? " " + esc(PC.busy) : ""}</div>${revs}</div>
      ${t.approved ? "" : `<div class="choices" style="margin-top:14px">${opts}</div>${hint ? `<div class="hint" style="margin-top:8px">${esc(hint)}</div>` : ""}
      <textarea style="margin-top:12px" placeholder="${esc(t.free_text)}" oninput="answers['${t.id}'].text=this.value">${esc(a.text)}</textarea>`}</div>
    <div class="card-f">${t.approved ? `<span class="hint grow">Approved. A change here updates the decisions that depend on it (Out-of-date outputs) and asks for approval again.</span>`
      : `<button class="btn primary" ${a.option?"":"disabled"} onclick="submitTodo('${t.id}')">Submit</button>${a.option ? "" : `<span class="hint">Choose an answer first</span>`}`}<span id="m-protocol" class="msg"></span></div></div>`;
}
function provenanceMode(){ return (S && S.run && S.run.human_mode) || ""; }
/* the eligibility review after extraction: the extraction's claim, the verifier's verdict, the outcome */
function reviewBox(r){
  const OUT = {excluded:["Excluded: both agents say the study is not eligible","bad"],
    stays_included:["Stays included: the verifier finds it eligible (two readings against one)","ok"],
    asks_you:["Both agents say exclude; your inclusion stands until you answer (Needs you)","warn"],
    kept_human_decision:["Your decision is kept","info"]};
  const o = OUT[r.outcome] || [r.outcome, ""];
  return `<div style="margin-top:8px;padding:10px 12px;border:1px solid var(--line);border-radius:10px">
    <div class="xs" style="font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)">Eligibility review after extraction</div>
    <div class="small" style="margin-top:6px"><b>Extraction</b> (${esc(r.claim.criterion)}): exclude. ${esc(r.claim.why)}</div>
    <div class="small" style="margin-top:4px"><b>Verifier</b> (${esc(r.verifier.by||"")}): ${esc(r.verifier.verdict)}. ${esc(r.verifier.explanation)}${(r.verifier.evidence||[]).length ? ` <span class="xs mono mut">${esc(r.verifier.evidence.join(", "))}</span>` : ""}</div>
    ${r.verifier.quote ? `<div class="xs mut" style="margin-top:4px;white-space:pre-wrap">${esc(r.verifier.quote.slice(0, 500))}</div>` : ""}
    <div style="margin-top:6px">${badge(o[0], o[1], "dot")}</div></div>`;
}
/* the verifier's independent check of a screening decision */
function verifyBox(v){
  const OUT = {agreed:["Verifier agrees","ok"], decided_by_verifier:["The screener could not decide; the verifier did","ok"], still_undecided:["Neither agent could decide; your call","warn"],
    forwarded:["Verifier disagreed; forwarded to the full text","info"], pending_disagreement:["Screener and verifier disagree; your call","warn"],
    include_by_policy_disagreement:["Disagreement; included by the pipeline rule","warn"], exclude_by_policy_disagreement:["Disagreement; excluded by the pipeline rule","warn"]};
  const o = OUT[v.outcome] || [v.outcome, ""];
  return `<div style="margin-top:8px;padding:10px 12px;border:1px solid var(--line);border-radius:10px">
    <div class="xs" style="font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)">Second reading of the screening decision</div>
    <div class="small" style="margin-top:6px"><b>Verifier</b> (${esc(v.by||"")}): ${esc(v.decision)}. ${esc(v.explanation||"")}</div>
    ${(v.differs_on||[]).map(d => `<div class="xs mut" style="margin-top:3px">${esc(d.id)}: ${esc(d.assessment)}${d.reason ? " · " + esc(d.reason) : ""}${(d.evidence||[]).length ? ` <span class="mono">${esc(d.evidence.join(", "))}</span>` : ""}</div>`).join("")}
    <div style="margin-top:6px">${badge(o[0], o[1], "dot")}</div></div>`;
}
function pcEditStart(){ const t = S.todos.find(x => x.kind === "protocol"); PC.edit = (t.criteria || []).map(c => ({...c})); renderTodo(); }
function pcEditAdd(){
  const t = S.todos.find(x => x.kind === "protocol"); const n = PC.edit.filter(c => c.type === "inclusion").length + 1;
  let id = "I" + n; while (PC.edit.some(c => c.id === id)) id = id + "b";
  PC.edit.push({id, type:"inclusion", stage:"title_abstract", text:""}); renderTodo();
}
async function pcCall(body, busy){
  PC.busy = busy; renderTodo();
  const r = (await runOp(...(body.op === "revise" ? ["protocol.revise", {instruction: body.instruction}, "Ask the agent to change the protocol"]
                                                 : ["protocol.edit_criteria", {criteria: body.criteria, note: body.note || ""}, "Change the criteria"]))) || {ok: false, message: "cancelled"};
  PC.busy = "";
  const m = document.getElementById("m-protocol"); if (m) msg(m, r);
  if (!r.ok) toast(r.message || r.error || "Failed", true); else toast(r.message || "Done");
  await refresh(true); renderTodo();
  return r;
}
async function pcEditSave(){
  const crit = PC.edit.map(c => ({id:c.id, type:c.type, stage:c.stage, text:c.text, any_of:(c.any_of||"").trim(), remove:!!c.remove}));
  const r = await pcCall({op:"edit", criteria:crit}, "Saving…");
  if (r.ok) { PC.edit = null; delete answers["protocol"]; renderTodo(); }
}
async function pcAsk(text){
  if (!text || !text.trim()) return;
  PC.ask = "";
  const r = await pcCall({op:"revise", instruction:text.trim()}, "Asking the agent…");
  if (r.ok) { delete answers["protocol"]; renderTodo(); }
}

/* data items and quality domains: editable at any stage; saved into the topic file, accepted, then brought up to date */
const FC = {edit:null, busy:""};
function fieldsCard(t){
  const e = FC.edit;
  const frow = (f, i) => e ? `<tr style="${f.remove ? "opacity:.45" : ""}"><td><input class="mono" style="width:170px" value="${esc(f.name||"")}" oninput="FC.edit.fields[${i}].name=this.value" placeholder="item_name"></td>
      <td><select onchange="FC.edit.fields[${i}].kind=this.value"><option value="" ${!f.kind?"selected":""}>text</option><option value="drug" ${f.kind==="drug"?"selected":""}>drug (term check)</option><option value="disease" ${f.kind==="disease"?"selected":""}>disease (term check)</option></select></td>
      <td><textarea rows="2" style="width:100%" oninput="FC.edit.fields[${i}].description=this.value">${esc(f.description||"")}</textarea>
        <button class="link small" onclick="FC.edit.fields[${i}].remove=!FC.edit.fields[${i}].remove;renderTodo()">${f.remove ? "keep" : "remove"}</button></td></tr>`
    : `<tr><td class="mono" style="font-weight:600">${esc(f.name)}</td><td class="small mut">${esc(f.kind || "text")}</td><td>${esc(f.description)}</td></tr>`;
  const drow = (d, i) => e ? `<tr style="${d.remove ? "opacity:.45" : ""}"><td><input class="mono" style="width:170px" value="${esc(d.name||"")}" oninput="FC.edit.domains[${i}].name=this.value" placeholder="domain_name"></td>
      <td colspan="2"><textarea rows="2" style="width:100%" oninput="FC.edit.domains[${i}].description=this.value">${esc(d.description||"")}</textarea>
        <button class="link small" onclick="FC.edit.domains[${i}].remove=!FC.edit.domains[${i}].remove;renderTodo()">${d.remove ? "keep" : "remove"}</button></td></tr>`
    : `<tr><td class="mono" style="font-weight:600">${esc(d.name)}</td><td colspan="2">${esc(d.description)}</td></tr>`;
  const F = e ? e.fields : t.fields, D = e ? e.domains : t.domains;
  return `<div class="card"><div class="card-b"><div class="q">${esc(t.question)}</div>
    <div class="small" style="font-weight:600;margin-top:4px">Extraction items</div>
    <div class="tw" style="border:1px solid var(--line);border-radius:10px;margin:6px 0 12px"><table class="crit"><thead><tr><th>Name</th><th>Kind</th><th>What to extract</th></tr></thead><tbody>${F.map(frow).join("")}</tbody></table></div>
    ${e ? `<button class="btn sm" onclick="FC.edit.fields.push({name:'',description:''});renderTodo()">Add an item</button>` : ""}
    <div class="small" style="font-weight:600;margin-top:14px">Quality domains${t.tool ? ` <span class="mut" style="font-weight:400">· ${esc(t.tool)}</span>` : ""}</div>
    <div class="tw" style="border:1px solid var(--line);border-radius:10px;margin:6px 0 12px"><table class="crit"><thead><tr><th>Name</th><th colspan="2">What to judge</th></tr></thead><tbody>${D.map(drow).join("")}</tbody></table></div>
    ${e ? `<button class="btn sm" onclick="FC.edit.domains.push({name:'',description:''});renderTodo()">Add a domain</button>` : ""}
    <div class="xs mut" style="margin-top:12px">${t.n_extracted ? `${t.n_extracted} of ${t.n_included} included studies are extracted. A new or changed item is extracted for each of them on "Update now" (one extraction call per study; values you edited are kept); a removed item is dropped. A new domain is judged the same way; your confirmed judgments are kept.` : "Nothing is extracted yet: changes apply from the first extraction on."} The change is a review-defining setting: it is written to the topic file, accepted on your behalf and recorded.</div>
  </div>
  <div class="card-f">${e ? `<button class="btn primary" onclick="fcSave()" ${FC.busy ? "disabled" : ""}>${ic("check","sm")} Save (no LLM)</button><button class="btn" onclick="FC.edit=null;renderTodo()">Cancel</button>`
    : `<button class="btn" onclick="fcEdit()">${ic("edit","sm")} Edit items and domains</button>`}<span class="hint">${esc(FC.busy)}</span><span id="m-fields" class="msg"></span></div></div>`;
}
function fcEdit(){ const t = S.todos.find(x => x.kind === "fields"); FC.edit = {fields: t.fields.map(f => ({...f})), domains: t.domains.map(d => ({...d}))}; renderTodo(); }
async function fcSave(){
  const body = {op:"save", fields: FC.edit.fields.filter(f => !f.remove).map(f => ({name:f.name.trim(), kind:f.kind||"", description:f.description.trim()})),
                domains: FC.edit.domains.filter(d => !d.remove).map(d => ({name:d.name.trim(), description:d.description.trim()}))};
  FC.busy = "Saving…"; renderTodo();
  const r = await api("/api/data_items", body);
  FC.busy = "";
  const m = document.getElementById("m-fields"); if (m) msg(m, r);
  if (r.ok) { toast(r.message || "Saved"); FC.edit = null; } else toast(r.message || r.error || "Not saved", true);
  await refresh(true); renderTodo();
}

/* search strategy: the agent's query and alternatives with PubMed counts; ask for changes; confirm the query to run */
const SS = {pick:null, custom:"", ask:"", busy:""};
const SS_CHIPS = ["Exclude reviews and editorials", "Add a block for the management strategies the protocol names",
  "Humans only", "English only", "Published since 2010", "More precise: main concept in the title only"];
function searchCard(t){
  if (!t) return "";
  const vars = t.variants || [], conf = t.confirmed, fmt = n => n == null ? "–" : Number(n).toLocaleString();
  const selected = SS.custom.trim() || SS.pick || (conf && conf.query) || (vars[0] && vars[0].query) || "";
  const EX = {sensitive:"info", balanced:"", precise:"warn"};
  const rows = vars.map((v, i) => `<tr><td style="width:28px"><input type="radio" name="ssv" ${(!SS.custom.trim() && selected === v.query) ? "checked" : ""} onclick="ssPick(${i})"></td>
      <td><b style="font-weight:600">${esc(v.name)}</b>${v.expect ? ` ${badge(v.expect, EX[v.expect] || "", "sq")}` : ""}<div class="xs mut" style="margin-top:3px">${esc((v.rationale || "").length > 320 ? v.rationale.slice(0, 320) + "…" : (v.rationale || ""))}</div>
        <details data-k="ssq-${v.id}" style="margin-top:4px"><summary class="xs">query${(v.rationale || "").length > 320 ? " and full rationale" : ""}</summary><div class="xs mono" style="white-space:pre-wrap;word-break:break-word">${esc(v.query)}</div>${(v.rationale || "").length > 320 ? `<div class="xs mut" style="margin-top:6px">${esc(v.rationale)}</div>` : ""}</details></td>
      <td class="r nowrap"><b>${fmt(v.count)}</b><div class="xs mut">hits</div>${v.error ? `<div class="xs" style="color:var(--warn)">${esc(v.error)}</div>` : ""}</td></tr>`).join("");
  const chat = (t.chat || []).map(c => c.role === "user" ? `<div class="small" style="margin-top:8px"><span class="mut">You:</span> ${esc(c.text)}</div>`
      : `<div class="small" style="margin-top:4px;padding:8px 10px;border:1px solid var(--line);border-radius:8px"><span class="mut">Agent:</span> ${esc(c.text)} <b>${fmt(c.count)} hits</b>
         <button class="link small" onclick="ssUse(${JSON.stringify(c.query).replace(/"/g, "&quot;")})">use this query</button></div>`).join("");
  const confBox = conf ? `<div class="callout info" style="margin:0 0 12px">${ic("check")}<div class="grow"><b>Confirmed:</b> ${esc(conf.variant || "your query")} · ${fmt(conf.count)} hits · ${esc(conf.ts || "")}
      <div class="xs mono" style="white-space:pre-wrap;word-break:break-word;margin-top:4px">${esc(conf.query)}</div>
      <div class="xs mut" style="margin-top:4px">The search runs with this query (Run: up to stage 2 or later). <button class="link small" onclick="ssClear()">Change it</button></div></div></div>` : "";
  const need = t.required && !conf ? `<div class="callout warn" style="margin:0 0 12px">${ic("warn")}<div class="grow">The search waits for a confirmed query (<code>pipeline.confirm_search</code>).</div></div>` : "";
  return `<div class="grp" id="g-search" data-step="search">${grpHead("Search strategy", null, `The agent's PubMed query and alternatives, each with its hit count${t.maxdate ? ` (up to ${esc(t.maxdate)})` : ""}${t.retmax ? `; the run fetches at most ${fmt(t.retmax)} records` : ""}. Pick one, ask for a change in plain words, or write your own, then confirm.`)}
    <div class="card"><div class="card-b">${need}${confBox}
      ${vars.length ? `<div class="tw"><table><thead><tr><th></th><th>Query</th><th class="r">PubMed</th></tr></thead><tbody>${rows}</tbody></table></div>`
                    : `<div class="small mut">No alternatives yet. "Suggest alternatives" asks the agent for its query and five variants (one call to the search stage's model) and counts each on PubMed.</div>`}
      <div class="row" style="margin-top:10px;gap:8px"><button class="btn" onclick="ssSuggest()" ${SS.busy ? "disabled" : ""}>${ic("bot","sm")} ${vars.length ? "Suggest again" : "Suggest alternatives"}</button><span class="hint">${esc(SS.busy)}</span></div>
      <div style="margin-top:16px"><div class="small" style="font-weight:600">Ask for a change</div>
        <div class="row" style="flex-wrap:wrap;gap:6px;margin:6px 0">${SS_CHIPS.map(c => `<button class="btn sm ghost" onclick="ssAsk(${JSON.stringify(c).replace(/"/g, "&quot;")})">${esc(c)}</button>`).join("")}</div>
        <div class="field"><input placeholder="e.g. limit to children; add Spanish synonyms; drop the brand names" value="${esc(SS.ask)}" oninput="SS.ask=this.value" onkeydown="if(event.key==='Enter')ssAsk(this.value)">
          <button class="btn" onclick="ssAsk(SS.ask)" ${SS.busy ? "disabled" : ""}>Ask</button></div>
        <div class="xs mut">Applied to the selected query (${esc(selected ? selected.slice(0, 60) + (selected.length > 60 ? "…" : "") : "none")}). Each request is one call to the search stage's model plus a free PubMed count.</div>
        ${chat}</div>
      <div style="margin-top:16px"><div class="small" style="font-weight:600">Or write the query yourself</div>
        <textarea rows="4" class="mono" style="margin-top:6px" placeholder="A complete PubMed query" oninput="SS.custom=this.value">${esc(SS.custom)}</textarea>
        <div class="row" style="gap:8px;margin-top:6px"><button class="btn" onclick="ssCount()">Count on PubMed</button>${t.draft && t.draft.query === SS.custom.trim() ? `<span class="small"><b>${fmt(t.draft.count)}</b> hits</span>` : ""}</div></div>
    </div>
    <div class="card-f"><button class="btn primary" onclick="ssConfirm()" ${selected && !SS.busy ? "" : "disabled"}>${ic("check","sm")} Confirm this query</button>
      <span class="hint grow">${conf ? "Confirming again replaces the confirmed query." : "The search runs with the confirmed query; without one it runs the agent's."}</span><span id="m-search" class="msg"></span></div></div></div>`;
}
function searchRanCard(t){
  const fmt = n => n == null ? "–" : Number(n).toLocaleString();
  const r = t.ran, vars = t.variants || [];
  const rows = vars.map(v => `<tr><td><b style="font-weight:600">${esc(v.name)}</b>${r.query === v.query ? ` ${badge("ran", "ok", "sq")}` : ""}<div class="xs mut" style="margin-top:3px">${esc((v.rationale || "").slice(0, 320))}</div>
      <details data-k="ssq-${v.id}" style="margin-top:4px"><summary class="xs">query</summary><div class="xs mono" style="white-space:pre-wrap;word-break:break-word">${esc(v.query)}</div></details></td>
      <td class="r nowrap"><b>${fmt(v.count)}</b><div class="xs mut">hits</div></td></tr>`).join("");
  return `<div class="card"><div class="card-b">
    <div class="small"><b>${esc(r.database || "PubMed")}</b> · ${fmt(r.hits)} hits · ${fmt(r.retrieved)} fetched${t.retmax ? ` (cap ${fmt(t.retmax)})` : ""} · ${fmt(r.records)} records after removing ${fmt(r.duplicates)} duplicates · query by ${esc(r.source || "the agent")}${t.confirmed ? ` (${esc(t.confirmed.ts || "")})` : ""}</div>
    ${r.hits > r.retrieved ? `<div class="callout warn" style="margin:10px 0">${ic("warn")}<div class="grow">${fmt(r.hits - r.retrieved)} hits were never fetched (the run's record cap). To see them, raise <code>pubmed_retmax</code> in the topic file or narrow the query, in a new run folder.</div></div>` : ""}
    <div class="xs mono" style="white-space:pre-wrap;word-break:break-word;margin-top:8px;padding:8px 10px;border:1px solid var(--line);border-radius:8px">${esc(r.query || "")}</div>
    ${vars.length ? `<div class="small" style="font-weight:600;margin-top:14px">The alternatives that were considered</div><div class="tw"><table><tbody>${rows}</tbody></table></div>` : ""}
    <div class="xs mut" style="margin-top:10px">The search is the root of the review: a different query means a new search, so it is run in a new run folder (Configs → New run file), not changed in place.</div>
  </div></div>`;
}
function ssPick(i){ const t = S.todos.find(x => x.kind === "search"); SS.pick = t.variants[i].query; SS.custom = ""; renderTodo(); }
function ssUse(q){ SS.custom = q; SS.pick = q; renderTodo(); }
async function ssCall(body, busy){
  SS.busy = busy; renderTodo();
  const OPN = {suggest: "search.suggest_variants", refine: "search.refine", count: "search.count", confirm: "search.confirm_strategy", clear: "search.clear_strategy"};
  const args = {suggest: {}, refine: {query: body.query, instruction: body.instruction}, count: {query: body.query},
                confirm: {query: body.query, note: body.note || ""}, clear: {}}[body.op];
  const r = (await runOp(OPN[body.op], args, "Search strategy: " + body.op)) || {ok: false, message: "cancelled"};
  SS.busy = "";
  const m = document.getElementById("m-search"); if (m) msg(m, r);
  if (!r.ok) toast(r.message || r.error || "Failed", true);
  await refresh(true); renderTodo();
  return r;
}
async function ssSuggest(){ await ssCall({op:"suggest"}, "Asking the agent for alternatives and counting them on PubMed…"); }
async function ssAsk(text){
  const t = S.todos.find(x => x.kind === "search"); if (!t || !text || !text.trim()) return;
  const q = SS.custom.trim() || SS.pick || (t.confirmed && t.confirmed.query) || (t.variants[0] && t.variants[0].query);
  if (!q) { toast("Choose or write a query first", true); return; }
  SS.ask = "";
  const r = await ssCall({op:"refine", query:q, instruction:text.trim()}, "Revising the query…");
  if (r.ok && r.query) { SS.custom = r.query; SS.pick = r.query; renderTodo(); }
}
async function ssCount(){ if (SS.custom.trim()) await ssCall({op:"count", query:SS.custom.trim()}, "Counting…"); }
async function ssConfirm(){
  const t = S.todos.find(x => x.kind === "search");
  const q = SS.custom.trim() || SS.pick || (t.confirmed && t.confirmed.query) || (t.variants[0] && t.variants[0].query);
  if (!q) return;
  const r = await ssCall({op:"confirm", query:q}, "Confirming…");
  if (r.ok) { toast(r.message || "Confirmed"); SS.custom = ""; SS.pick = null; renderTodo(); }
}
async function ssClear(){ await ssCall({op:"clear"}, ""); }
function grpHead(title, n, sub){ return `<div class="gh"><h2>${title}</h2>${n != null ? `<span class="cnt">${n}</span>` : ""}</div>${sub ? `<p class="gsub">${sub}</p>` : `<div style="height:8px"></div>`}`; }

/* criteria lines "I1 (inclusion, title_abstract): text" -> a small table */
function ctxBlock(items){
  if (!items || !items.length) return "";
  const re = /^([A-Z]+\d+) \(([a-z_]+), ([a-z_]+)\): (.*)$/;
  if (items.every(c => re.test(c))) {
    const TT = {inclusion:"ok", exclusion:"bad"};
    return `<div class="tw" style="border:1px solid var(--line);border-radius:10px;margin-bottom:14px"><table class="crit"><thead><tr><th>ID</th><th>Type</th><th class="hide-xs">Applied at</th><th>Criterion</th></tr></thead><tbody>${items.map(c => {
      const [, id, ty, stage, text] = c.match(re);
      return `<tr><td class="mono" style="font-weight:600">${esc(id)}</td><td>${badge(human(ty), TT[ty]||"", "sq")}</td><td class="small mut nowrap hide-xs">${esc(stage.replace(/_/g, " / ").replace("title / abstract","title/abstract"))}</td><td>${esc(text)}</td></tr>`;
    }).join("")}</tbody></table></div>`;
  }
  return `<ul class="ctx">${items.map(c => `<li>${esc(c)}</li>`).join("")}</ul>`;
}

/* eligibility: one table, suggestions pre-filled, one Save */
function eligTable(items){
  if (!items.length) return "";
  items.forEach(t => { if (!elig[t.rid]) elig[t.rid] = {option: t.suggest.option, criterion: t.suggest.criterion || "", text: ""}; });
  const OPT = [["include","Include"],["exclude","Exclude"],["pdf","Need full text"]];
  const nChanged = items.filter(t => elig[t.rid].option !== t.suggest.option).length;
  const rows = items.map(t => {
    const e = elig[t.rid];
    const seg = `<div class="seg">${OPT.map(([v,l]) => `<button class="${e.option===v?"on":""} ${t.suggest.option===v?"sug":""}" onclick="elig['${t.rid}'].option='${v}';renderTodo()">${l}</button>`).join("")}</div>`;
    const reason = e.option === "exclude" ? `<select onchange="elig['${t.rid}'].criterion=this.value" style="min-width:200px"><option value="">Reason for exclusion…</option>
        ${t.criteria.concat(["other"]).map(c => `<option ${e.criterion===c?"selected":""}>${esc(c)}</option>`).join("")}</select>` : "";
    const sugL = (OPT.find(o => o[0] === t.suggest.option) || [0, t.suggest.option])[1];
    return `<div class="erow ${e.option !== t.suggest.option ? "chg" : ""}">
      <div style="min-width:0"><div class="t">${esc(t.title)}</div><div class="m">${esc(t.meta)}</div>
        <div class="lk">${t.pmid ? `<a target="_blank" href="https://pubmed.ncbi.nlm.nih.gov/${esc(t.pmid)}/">PubMed ${esc(t.pmid)} ${ic("ext","sm")}</a>` : ""}
          ${t.doi ? `<a target="_blank" href="https://doi.org/${esc(t.doi)}">DOI ${ic("ext","sm")}</a>` : ""}
          <button class="link small" onclick="openStudy('${t.rid}')">Study page</button></div>
        <div class="small" style="margin-top:6px"><span class="mut">Undecided:</span> ${esc(t.why)}</div>
        ${t.agent_reason ? `<div class="xs mut" style="margin-top:4px"><span style="font-weight:600;text-transform:uppercase;letter-spacing:.05em">Agent</span> ${esc(t.agent_reason)}</div>` : ""}
        ${t.review ? reviewBox(t.review) : ""}${t.verify ? verifyBox(t.verify) : ""}
        ${t.abstract ? `<details data-k="abs-${esc(t.rid)}" style="margin-top:6px"><summary>Abstract</summary><div class="abstract">${esc(t.abstract)}</div></details>` : `<div class="xs mut" style="margin-top:6px">No abstract</div>`}
      </div>
      <div class="dec">${seg}${reason}
        <div class="xs mut"><span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--acc);margin-right:5px;vertical-align:1px"></span>Suggested: <b style="color:var(--fg-2)">${esc(sugL)}</b> · ${esc(t.suggest.why)}</div></div></div>`;
  }).join("");
  return `<div class="grp" id="g-eligibility" data-step="eligibility">${grpHead("Eligibility", items.length, `The agent could not decide these. A suggestion is pre-selected for each; change any you disagree with, then save once. "Need full text" moves the study to the Full texts list.`)}
    <div class="card"><div class="card-b flush">${rows}</div>
    <div class="card-f"><button class="btn primary" onclick="saveElig()">Save ${items.length} decision${items.length>1?"s":""}</button>
      <span class="hint">${nChanged ? `${nChanged} changed from the suggestion` : "All as suggested"}</span><span id="eligmsg" class="msg"></span></div></div></div>`;
}
async function saveElig(){
  const rids = S.todos.filter(t => t.kind === "eligibility").map(t => t.rid);
  const items = rids.map(r => ({rid:r, ...elig[r]}));
  const bad = items.filter(i => i.option === "exclude" && !i.criterion);
  if (bad.length) { $("#eligmsg").innerHTML = `<span class="err">Choose a reason for each exclusion</span>`; return; }
  const r = await runOp("eligibility.decide_many", {items: items.map(i => ({rid: i.rid, decision: i.option, criterion: i.criterion, note: i.text || ""}))},
                        `Save ${items.length} eligibility decision(s)`);
  if (!r) return;
  $("#eligmsg").innerHTML = r.ok ? `<span class="saved">${ic("check","sm")} ${esc(r.message)}</span>` : `<span class="err">${esc(r.message||r.error)}</span>`;
  if (r.ok) toast(r.message || "Saved");
  rids.forEach(r => delete elig[r]);
  refresh(true);
}

/* full texts: copyable PMID list + file names */
function pdfPanel(ft, compact){
  const pm = ft.pmids || [], dois = ft.dois || [];
  const q = encodeURIComponent(pm.map(p => p + "[pmid]").join(" OR "));
  const nNew = (S.fulltext.new_files||[]).length;
  const added = ft.files.filter(x => x.present).length, total = ft.files.length;
  const rows = ft.files.map(x => `<tr><td>${x.present ? badge("added","ok","dot") : badge("missing","","dot")}</td>
      <td class="nowrap"><code style="word-break:normal">${esc(x.filename)}</code></td>
      <td><div>${esc(x.title)}</div><div class="xs mut" style="margin-top:2px">${esc(x.why)}${x.open_access_url?` · <a target="_blank" href="${esc(x.open_access_url)}">open-access copy ${ic("ext","sm")}</a> (the server could not download it; your browser usually can)`:""}</div></td>
      <td class="nowrap r">${x.priority===1 && !x.present ? `<button class="btn sm" onclick="pdfUnwant('${x.rid}')">Decide without it</button>` : ""}
        ${!x.present ? `<button class="btn sm" onclick="pdfUnavailable('${x.rid}')">Not obtainable</button>` : ""}</td></tr>`).join("");
  return `<div class="card">
    <div class="card-h"><div class="grow"><h3>${added} of ${total} added</h3><div class="sub">Name each file <code>&lt;PMID&gt;.pdf</code>; the agent picks them up when you process them.</div></div>
      <div style="width:160px" class="bar"><i style="width:${total ? Math.round(100*added/total) : 0}%"></i></div></div>
    <div class="ftsteps">
      <div class="fts"><div class="n">1</div><div style="min-width:0"><b>Find the papers</b>
        <div class="field"><input id="pmidbox" readonly value="${esc(pm.join(","))}" aria-label="PMIDs">
          <button class="btn" data-v="${esc(pm.join(","))}" onclick="copyText(this.dataset.v, this)">${ic("copy","sm")} Copy ${pm.length} PMIDs</button>
          ${pm.length ? `<a class="btn" target="_blank" href="https://pubmed.ncbi.nlm.nih.gov/?term=${q}">Open in PubMed ${ic("ext","sm")}</a>` : ""}</div>
        ${dois.length ? `<div class="field" style="margin-top:8px"><input readonly value="${esc(dois.join(", "))}" aria-label="DOIs"><button class="btn" data-v="${esc(dois.join(", "))}" onclick="copyText(this.dataset.v, this)">${ic("copy","sm")} Copy ${dois.length} DOIs</button></div>
          <div class="hint" style="margin-top:4px">DOIs of records without a PMID.</div>` : ""}</div></div>
      <div class="fts"><div class="n">2</div><div style="min-width:0"><b>Save each PDF in this folder</b>
        <div class="field"><input readonly value="${esc(S.fulltext.dir)}" aria-label="Folder"><button class="btn" data-v="${esc(S.fulltext.dir)}" onclick="copyText(this.dataset.v, this)">${ic("copy","sm")} Copy path</button></div>
        <div class="hint" style="margin-top:4px">For example <code>${esc((ft.files[0]||{}).filename||"30157389.pdf")}</code>. The file list below has the name for every study.</div></div></div>
      <div class="fts"><div class="n">3</div><div><b>Process the added files</b>
        <div class="row"><button class="btn primary" onclick="processPdfs(this)" ${nNew?"":"disabled"}>${ic("play","sm")} Process added PDFs${nNew ? ` (${nNew})` : ""}</button>
          <span class="hint">${nNew ? `${nNew} new file${nNew>1?"s":""} found in the folder.` : "No new files in the folder yet."}</span><span class="msg pdfmsg"></span></div></div></div>
    </div>
    <div class="card-f" style="display:block">
      <details data-k="ftlist" ${compact?"":"open"}><summary>File list (${total})</summary>
        <div class="tw" style="margin-top:10px;background:var(--surface);border:1px solid var(--line);border-radius:10px"><table><thead><tr><th>Status</th><th>File name</th><th>Study</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
        <div class="row" style="margin-top:10px"><a class="btn sm" href="${U('/needed_pdfs.csv')}">${ic("download","sm")} Download list (CSV)</a></div></details></div></div>`;
}
async function pdfUnwant(rid){ await runOp("retrieve.decide_without", {rids:[rid]}, `Decide ${rid} without another full text`); }
async function pdfUnavailable(rid){ await runOp("retrieve.declare_unobtainable", {rids:[rid]}, `${rid}: not obtainable`); }

/* risk of bias: one table, bulk confirm */
function robWhy(t){
  const v = t.verify, J = j => esc(JLAB[j]||j);
  const why = (t.reasons||[]).length ? `<div class="xs" style="margin-top:6px;color:var(--warn)"><b>Why a second look:</b> ${t.reasons.map(esc).join("; ")}</div>` : "";
  if (!v) return why;
  const OUT = {agreed:["The verifier agreed with the draft","ok"], decided_by_verifier:["The verifier settled the flagged points","ok"], still_undecided:["Neither agent could settle it; your call","warn"]};
  const [lab, cls] = OUT[v.outcome] || [v.outcome, ""];
  const ch = (v.changed||[]).map(c => `${esc(human(c.domain))}: agent ${J(c.agent)}, verifier ${J(c.verifier)}`).join("; ");
  const own = v.outcome === "still_undecided" && (v.domains||[]).length ? `<div class="xs mut">Verifier's leaning: ${v.domains.map(d => `${esc(human(d.domain))} ${J(d.judgment)}`).join("; ")}</div>` : "";
  return `${why}<div style="margin-top:6px;padding:6px 10px;border:1px solid var(--line);border-radius:8px">
    <div class="xs">${badge(lab, cls, "dot")} <span class="mut">${esc(v.by||"verifier")}</span>${ch ? ` · ${ch}` : ""}</div>
    ${v.explanation ? `<div class="xs mut" style="margin-top:2px">${esc(v.explanation)}</div>` : ""}${own}</div>`;
}
function robTable(items, opt){
  if (!items.length) return "";
  const O = opt ? "o" : "";
  const rows = items.map(t => {
    const ed = robEdit[t.rid];
    const doms = t.domains.map(d => `<span class="rob-chip ${JCOL[d.judgment]||""}" title="${esc(JLAB[d.judgment]||d.judgment)}: ${esc(d.rationale)}"><i></i>${esc(human(d.domain))} <b>${esc(JLAB[d.judgment]||d.judgment)}</b></span>`).join("");
    const edit = ed ? `<div class="robedit"><div class="tw"><table>${t.domains.map((d,i) => `<tr><td style="width:190px"><b>${esc(human(d.domain))}</b></td>
          <td style="width:170px"><select onchange="robEdit['${t.rid}'].j[${i}]=this.value">${["low","some_concerns","high","unclear"].map(j=>`<option value="${j}" ${ed.j[i]===j?"selected":""}>${JLAB[j]}</option>`).join("")}</select>
            ${ed.j[i]!==d.judgment?`<div class="xs mut" style="margin-top:3px">agent: ${esc(JLAB[d.judgment]||d.judgment)}</div>`:""}</td>
          <td class="small mut">${esc(d.rationale)}</td></tr>`).join("")}</table></div>
        <textarea style="margin-top:10px" placeholder="Why (optional; kept in the history, and sent to the agent if you ask it to re-assess)" oninput="robEdit['${t.rid}'].text=this.value">${esc(ed.text||"")}</textarea>
        <div class="row" style="margin-top:10px"><button class="btn primary" onclick="robChange('${t.rid}')">Save changes and confirm</button>
          ${judging() ? `<button class="btn" onclick="robAgent('${t.rid}')">${ic("bot","sm")} Ask the agent to re-assess instead</button>` : ""}
          <button class="btn ghost" onclick="delete robEdit['${t.rid}'];renderTodo()">Cancel</button><span id="robm-${t.rid}" class="msg"></span></div></div>` : "";
    return `<tr><td style="width:36px"><input type="checkbox" aria-label="Select" ${robSel[t.rid]?"checked":""} onchange="robSel['${t.rid}']=this.checked"></td>
      <td style="min-width:170px;width:220px"><b>${esc(t.title.split(":")[0].replace(/\s*\[\d+\]$/, ""))}</b><div class="xs mut mono">${esc(t.rid)}</div>
        <div class="small" style="margin-top:4px;display:flex;gap:10px"><a target="_blank" href="${U(t.link)}">Report</a><button class="link small" onclick="openStudy('${t.rid}')">Study page</button></div></td>
      <td>${doms}${robWhy(t)}${edit}</td>
      <td class="nowrap r">${ed ? "" : `<button class="btn sm" onclick="robOpen('${t.rid}')">${ic("edit","sm")} Change</button>`}</td></tr>`;
  }).join("");
  const table = `<div class="card"><div class="card-h"><div class="row grow"><button class="btn sm" onclick="robAll(true,${!!opt})">Select all</button><button class="btn sm ghost" onclick="robAll(false,${!!opt})">Clear</button>
      <button class="btn sm primary" onclick="robConfirm(${!!opt})">${ic("check","sm")} Confirm selected</button><span id="robmsg${O}" class="msg"></span></div>
      <div class="legend">${["low","some_concerns","high","unclear"].map(j => `<span><i style="background:${{low:"var(--ok)",some_concerns:"var(--warn)",high:"var(--bad)",unclear:"var(--mut-2)"}[j]}"></i>${JLAB[j]}</span>`).join("")}</div></div>
    <div class="card-b flush tw"><table><tbody>${rows}</tbody></table></div></div>`;
  if (opt) return `<div class="grp" id="g-rob-opt" data-step="rob"><details data-k="rob-optional"><summary class="small mut" style="cursor:pointer;margin:4px 0 10px">Risk-of-bias drafts with nothing to settle · ${items.length} studies · optional: open to read, confirm or change them (the report marks them as drafts)</summary>${table}</details></div>`;
  return `<div class="grp" id="g-rob" data-step="rob">${grpHead("Risk of bias: points neither agent could settle", items.length, `Each study shows why it was flagged and what the verifier said. Hover a domain for the rationale. Confirm the draft, or use "Change" to set any number of domains at once.`)}${table}</div>`;
}
function robOpen(rid){ const t = S.todos.find(x => x.kind==="rob" && x.rid===rid); robEdit[rid] = {j: t.domains.map(d => d.judgment), text:""}; renderTodo(); }
function robAll(v, opt){ S.todos.filter(t => t.kind==="rob" && !!t.optional === !!opt).forEach(t => robSel[t.rid] = v); renderTodo(); }
async function robConfirm(opt){
  const ids = S.todos.filter(t => t.kind==="rob" && !!t.optional === !!opt && robSel[t.rid]).map(t => t.id);
  const M = $("#robmsg" + (opt ? "o" : ""));
  if (!ids.length) { M.innerHTML = `<span class="err">Tick at least one study</span>`; return; }
  const r = await runOp("rob.confirm", {rids: ids.map(i => i.slice(4))}, `Confirm ${ids.length} risk-of-bias assessment(s)`);
  if (!r) return;
  ids.forEach(i => delete robSel[i.slice(4)]);
  M.innerHTML = `<span class="saved">${ic("check","sm")} ${esc(r.message)}</span>`; toast(r.message || "Confirmed"); refresh(true);
}
async function robChange(rid){
  const t = S.todos.find(x => x.kind==="rob" && x.rid===rid), e = robEdit[rid];
  const changes = t.domains.map((d,i) => ({domain:d.domain, judgment:e.j[i]})).filter((c,i) => c.judgment !== t.domains[i].judgment);
  const r = await runOp("rob.edit_domains", {rid, changes, note: e.text || ""}, `Risk of bias of ${rid}`);
  if (!r) return;
  msg("robm-"+rid, r);
  if (r.ok) { delete robEdit[rid]; setTimeout(() => refresh(true), 500); }
}
async function robAgent(rid){
  const e = robEdit[rid];
  const r = await runOp("rob.recompute", {rid, note: e.text || ""}, `Ask the agent to re-assess ${rid}`);
  if (!r) return;
  msg("robm-"+rid, r);
  if (r.ok) { delete robEdit[rid]; setTimeout(() => refresh(true), 800); }
}

/* rule exclusions: visible and reversible */
function ruledList(){
  const R = S.ruled || [];
  if (!R.length) return "";
  const groups = {}; R.forEach(r => (groups[r.rule] = groups[r.rule] || []).push(r));
  const body = Object.entries(groups).map(([rule, rs]) => `<details data-k="rule-${esc(rule)}" style="display:block;padding:12px 18px;border-top:1px solid var(--line)">
     <summary style="color:var(--fg);font-size:13.5px;display:flex"><b>${esc(human(rule))}</b><span class="cnt" style="margin-left:8px">${rs.length}</span>
       <span class="small mut hide-sm" style="margin-left:10px;font-weight:400">${esc(rs[0].reason.split("(")[0])}</span></summary>
     <div class="tw" style="margin-top:10px"><table><tbody>${rs.map(r => `<tr><td>${esc(r.title)}<div class="xs mut">${esc(r.reason)}</div></td>
       <td class="nowrap">${r.pmid?`<a class="small" target="_blank" href="https://pubmed.ncbi.nlm.nih.gov/${esc(r.pmid)}/">PubMed ${ic("ext","sm")}</a>`:""}</td>
       <td class="r"><button class="btn sm ghost" onclick="restoreRule('${r.rid}')">Restore</button></td></tr>`).join("")}</tbody></table></div></details>`).join("");
  return `<div class="grp" id="g-ruled">${grpHead("Excluded automatically", R.length, `No action needed. These were excluded by publication-type rules before any LLM call. "Restore" sends a record back to your eligibility table.`)}
    <div class="card" style="overflow:hidden"><div style="margin-top:-1px">${body}</div></div></div>`;
}
async function restoreRule(rid){ await runOp("screen.restore_rule_exclusion", {rids:[rid]}, `Restore ${rid} (undo the rule exclusion)`); }

/* the study an item is about: its rid, else the study of the output it names (screen:/elig:/ft:/ext:/rob:<rid>) */
function studyOf(t){
  if (t.rid) return t.rid;
  const m = String(t.target || t.id || "").match(/(?:^|:)(?:screen|elig|ft|ext|rob|rec):([^:]+)/);
  return m ? m[1] : null;
}
/* generic card for the few remaining items (protocol, proposals, flags, out-of-date outputs) */
/* who said what about an item (an eligibility doubt: you, data extraction, the verifier), with their reasons */
function overviewBlock(rows){
  if (!rows || !rows.length) return "";
  const B = {include:"ok", exclude:"bad"};
  return `<div class="tw" style="border:1px solid var(--line);border-radius:10px;margin:10px 0 14px"><table class="who"><tbody>${rows.map(r => `<tr>
    <td class="nowrap" style="width:1%"><b>${esc(r.who)}</b></td><td class="nowrap" style="width:1%">${badge(r.says || "?", B[r.says] || "")}</td>
    <td>${esc(r.why || "")}${r.quote ? `<div class="wq">“${esc(r.quote)}”</div>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
}
function card(t){
  const a = answers[t.id] || (answers[t.id] = {option:null, text:"", extra:{}});
  const wide = t.options.some(o => o.hint);   // each answer says what it does: one per line
  const opts = t.options.map(o => `<button class="choice ${wide ? "wide" : ""} ${a.option===o.value?"on":""}" onclick="pick('${t.id}','${o.value}')"><span class="rd"></span>
    <span>${esc(o.label)}${o.hint ? `<span class="ch-h">${esc(o.hint)}</span>` : ""}</span></button>`).join("");
  const free = t.free_text ? `<textarea style="margin-top:12px" placeholder="${esc(t.free_text)}" oninput="answers['${t.id}'].text=this.value">${esc(a.text)}</textarea>` : "";
  return `<div class="card tcard" id="c-${esc(t.id)}" data-step="${esc(t.step||"")}">
    <div class="card-h"><h3>${esc(t.title)}</h3>${t.pending ? badge("queued until the job finishes","info") : ""}
      ${studyOf(t) ? `<a class="btn sm" href="#/studies/${encodeURIComponent(studyOf(t))}">Open study ${ic("arrowR","sm")}</a>` : ""}
      ${t.link ? `<a class="btn sm ghost" href="${U(t.link)}" target="_blank">Open in report ${ic("ext","sm")}</a>` : ""}</div>
    <div class="card-b"><div class="q">${esc(t.question)}</div>${overviewBlock(t.overview)}${ctxBlock(t.context)}
      <div class="choices ${wide ? "col" : ""}">${opts}</div>${free}</div>
    <div class="card-f"><button class="btn primary" ${a.option?"":"disabled"} onclick="submitTodo('${t.id}')">Submit</button>
      ${a.option ? "" : `<span class="hint">Choose an answer first</span>`}<span id="m-${esc(t.id)}" class="msg"></span></div></div>`;
}
/* a To-do answer is an operation: the server says which (/api/resolve), runOp makes it */
async function answerOp(id, option, text, extra){
  const o = await api("/api/resolve", {todo_id:id, option, text: text || "", extra: extra || {}});
  if (!o.ok) { toast(o.message || "Unknown answer", true); return o; }
  return (await runOp(o.name, o.args, (S.todos.find(t => t.id === id) || {}).title || o.name)) || {ok: false, message: "cancelled"};
}
async function answerNow(id, opt){
  const r = await answerOp(id, opt);
  const m = document.getElementById("m-"+id); if (m) msg(m, r);
}
function pick(id, v){ answers[id].option = v; renderTodo(); }
async function submitTodo(id){
  const a = answers[id];
  const r = await answerOp(id, a.option, a.text, a.extra);
  const m = document.getElementById("m-"+id);
  if (m) msg(m, r);
  if (r.ok) delete answers[id];
}

/* ---------------- run ---------------- */
const runOpts = {action:"run", until:"report", suite:"all", n_cases:"", max_records:"", extra_budget:2, agent:null, judge:null, effort:"low"};
async function loadModels(){
  const m = await api("/api/models");
  if (!m || m.error || !m.current) return;
  M = m;
  const st = M.settings || {};
  if (!runOpts.agent) runOpts.agent = st.agent || {provider:M.current.agent.provider, model:M.current.agent.model, effort:M.current.agent.reasoning_effort||"low"};
  if (!runOpts.judge) runOpts.judge = st.judge || {provider:M.current.judge.provider, model:M.current.judge.model, effort:"low"};
  if (st.extra_budget) runOpts.extra_budget = st.extra_budget;
  if (st.max_records && M.benchmark) runOpts.max_records = st.max_records;
  SM.edit = Object.fromEntries((M.stages || []).map(r => [r.stage, Object.keys(r.set).length ? {...r.set} : null])); SM.dirty = false;
  if (tab === "settings") renderSettings();
}
function modelOptions(sel, role){
  const api = M.api.filter(m => !m.roles || m.roles.includes(role) || role==="agent");
  const o = api.map(m => {
    const v = JSON.stringify({provider:m.provider, model:m.id});
    const on = sel && sel.provider===m.provider && sel.model===m.id;
    return `<option value='${esc(v)}' ${on?"selected":""} ${m.key_available?"":"disabled"}>${esc(m.id)} · $${m.input_per_m} in / $${m.output_per_m} out per 1M${m.key_available?"":" (no API key)"}</option>`;
  }).join("");
  const lo = M.local.models.map(id => {
    const v = JSON.stringify({provider:"local", model:id});
    const on = sel && sel.provider==="local" && sel.model===id;
    return `<option value='${esc(v)}' ${on?"selected":""}>${esc(id==="auto" ? "served model (auto)" : id)} · free (local)</option>`;
  }).join("");
  return `<optgroup label="API models (list price)">${o}</optgroup><optgroup label="Local models (vLLM endpoints)">${lo}</optgroup>`;
}
function noteFor(sel){
  if (!sel) return "";
  if (sel.provider === "local") {
    const L = M.local;
    return L.error ? `No valid endpoint list at ${L.file} (${L.error}). Register running servers with sragent local-endpoints (GUIDE section 4).`
                   : `${L.endpoints} endpoint(s) in ${L.file}. Check them with sragent local-endpoints --probe.`;
  }
  const m = M.api.find(x => x.id === sel.model); return m ? m.note || "" : "";
}
function runWarn(){
  const el = $("#runwarn"); if (!el) return;
  const a = runOpts;
  el.innerHTML = (a.action==="run" && a.until!=="protocol" && S && S.steps[0].status==="needs you") ? `<div class="callout warn">${ic("warn")}<div>The protocol is not approved yet. You can still run, but screening follows the current criteria.
     <button class="link" onclick="goTodoGroup('protocol')">Review the protocol</button></div></div>` : "";
}
/* models per stage (stage_models in the run file) */
const STAGE_JOB = act => ["run","pdfs","sync"].includes(act);
const SM = {edit:null, dirty:false};
function smEff(r){   // what a row resolves to with the unsaved edits
  const e = SM.edit[r.stage] || {}, ag = M.current.agent;
  return {provider: e.model ? (e.provider || ag.provider) : ag.provider, model: e.model || ag.model,
          effort: "reasoning_effort" in e ? e.reasoning_effort : ag.reasoning_effort};
}
function stageCard(){
  if (!M.stages) return "";
  const ag = M.current.agent;
  const apiOpts = (sel) => M.api.map(m => `<option value='${esc(JSON.stringify({provider:m.provider, model:m.id}))}' ${sel && sel.model===m.id && (sel.provider||ag.provider)===m.provider ? "selected" : ""} ${m.key_available ? "" : "disabled"}>${esc(m.id)} · $${m.input_per_m} / $${m.output_per_m}${m.key_available ? "" : " (no API key)"}</option>`).join("")
    + M.local.models.map(id => `<option value='${esc(JSON.stringify({provider:"local", model:id}))}' ${sel && sel.provider==="local" && sel.model===id ? "selected" : ""}>${esc(id === "auto" ? "local: served model" : "local: " + id)} · free</option>`).join("");
  const rows = M.stages.map(r => {
    const e = SM.edit[r.stage] || {}, eff = smEff(r), lock = r.started;
    const effs = (M.efforts[eff.provider] || ["low","medium","high"]);
    const effSel = "reasoning_effort" in e ? (e.reasoning_effort === null ? "default" : e.reasoning_effort) : "";
    return `<tr${lock ? ' style="opacity:.75"' : ""}${r.role ? ' style="border-top:2px solid var(--line)"' : ""}><td><b style="font-weight:600">${esc(r.label)}</b>${lock ? `<div class="xs" style="color:var(--warn)" title="This stage has started, so its model is fixed (accept a change on the Configs page)">fixed: stage started</div>` : ""}${r.note ? `<div class="xs mut">${esc(r.note)}</div>` : ""}</td>
      <td><select style="width:100%;min-width:200px" ${lock ? "disabled" : ""} onchange="smSet('${r.stage}','model',this.value)"><option value="" title="the agent in llm_defaults">${esc(ag.provider === "local" ? "local: " + (ag.model === "auto" ? "served model" : ag.model) + " · free" : ag.model)} (default)</option>${apiOpts(e.model ? e : null)}</select></td>
      <td><select ${lock ? "disabled" : ""} onchange="smSet('${r.stage}','effort',this.value)"><option value="" title="the agent in llm_defaults">${esc(ag.reasoning_effort || "model default")} (default)</option>
        ${effs.map(x => `<option ${effSel===x ? "selected" : ""}>${x}</option>`).join("")}<option value="default" ${effSel==="default" ? "selected" : ""}>model default</option></select></td></tr>`;
  }).join("");
  return `<div class="card"><div class="card-h"><div class="grow"><h2>Models per stage</h2><div class="sub">Saved in the run file (<code>stage_models</code>; the checks row in <code>roles.verifier</code>). A stage's model is fixed once that stage has started; the others can change.</div></div></div>
    <div class="card-b flush tw"><table id="smtable"><thead><tr><th>Stage</th><th>Model</th><th>Effort</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="card-f"><button class="btn primary" id="smsave" onclick="saveStageModels()" ${SM.dirty ? "" : "disabled"}>${ic("check","sm")} Save to run file</button>
      <button class="btn" onclick="testStageModels()">${ic("bot","sm")} Test models</button><span id="smmsg" class="msg grow"></span></div></div>`;
}
function smSet(stage, what, v){
  const e = {...(SM.edit[stage] || {})};
  if (what === "model") { if (v) { const o = JSON.parse(v); e.model = o.model; e.provider = o.provider; } else { delete e.model; delete e.provider; } }
  else { if (v === "") delete e.reasoning_effort; else e.reasoning_effort = v === "default" ? null : v; }
  SM.edit[stage] = Object.keys(e).length ? e : null; SM.dirty = true;
  if (tab === "settings") renderSettings();
}
async function saveStageModels(){
  const r = await api("/api/stage_models", {stages: SM.edit});
  msg("smmsg", r);
  if (!r.ok) { toast(r.message || r.error || "Not saved", true); return false; }
  toast(r.message || "Saved"); M = null; await loadModels(); return true;
}
async function testStageModels(){
  const el = $("#smmsg"); const seen = {}, out = [];
  M.stages.forEach(r => { const e = smEff(r); seen[e.provider + ":" + e.model + ":" + e.effort] = e; });
  el.innerHTML = `<span class="spin" style="display:inline-block;vertical-align:-2px"></span> testing ${Object.keys(seen).length} model(s)…`;
  for (const e of Object.values(seen)) {
    const r = await api("/api/test_model", {provider:e.provider, model:e.model, effort: e.effort || "default"});
    out.push(r.ok ? `<span class="saved">${ic("check","sm")} ${esc(e.model)} (${esc(e.effort || "default")}) ${r.seconds}s</span>` : `<span class="err">${esc(e.model)}: ${esc(r.error || "failed")}</span>`);
  }
  el.innerHTML = out.join(" · ");
}
let estTimer = null;
function estimate(){
  clearTimeout(estTimer);
  estTimer = setTimeout(async () => {
    const e = await api("/api/estimate", {...runOpts, max_records: runOpts.max_records ? Number(runOpts.max_records) : null,
                                          ...(STAGE_JOB(runOpts.action) && SM.edit ? {stage_models: SM.edit} : {})});
    const el = $("#est"); if (!el) return;
    if (e.error) { el.innerHTML = `<span class="err">${esc(e.error)}</span>`; return; }
    const rows = (e.rows||[]).map(r => `<tr title="${r.in_per_call.toLocaleString()} in / ${r.out_per_call.toLocaleString()} out tokens per call · ${esc(r.source)}"><td>${esc(r.stage)}${r.model ? `<div class="xs mut">${esc(r.model)} · ${esc(r.effort)}</div>` : ""}</td><td class="r num mut">${r.calls}×</td><td class="r num">${usd(r.usd,3)}</td></tr>`).join("");
    const spent = e.spent_so_far !== undefined ? `<div class="hint" style="margin-top:12px">Already spent in this run folder: ${usd(e.spent_so_far)}. Cached calls are free and are not counted again.</div>` : "";
    if (!rows && !e.note) { el.innerHTML = `<div class="est-big">$0</div><p class="small" style="margin:8px 0 0">Nothing left to run for this choice. It is already done, or waiting for your decisions in the To-do list.</p>${spent}`; return; }
    el.innerHTML = `<div class="est-big">${usd(e.total_usd)}</div><div class="small mut" style="margin-top:2px">likely $${e.range_usd[0]} to $${e.range_usd[1]}</div>
      ${rows ? `<table class="est-t" style="margin-top:12px"><thead><tr><th>Stage</th><th class="r">Calls</th><th class="r">Cost</th></tr></thead><tbody>${rows}</tbody></table>` : ""}
      ${e.note ? `<div class="hint" style="margin-top:10px">${esc(e.note)}</div>` : ""}${spent}`;
  }, 250);
}
async function testModel(){
  $("#testres").innerHTML = `<span class="spin" style="display:inline-block;vertical-align:-2px"></span> testing…`;
  const r = await api("/api/test_model", runOpts.agent);
  $("#testres").innerHTML = r.ok ? `<span class="saved">${ic("check","sm")} ${esc(r.model)} answered in ${r.seconds}s</span>` : `<span class="err">${esc(r.error||"failed")}</span>`;
}
async function startRun(){
  if (STAGE_JOB(runOpts.action) && SM.dirty && !(await saveStageModels())) return;
  const body = {...runOpts, max_records: runOpts.max_records ? Number(runOpts.max_records) : null,
                n_cases: runOpts.n_cases ? Number(runOpts.n_cases) : null};
  if (body.action !== "eval") delete body.judge;
  const r = await api("/api/run", body);
  $("#runmsg").innerHTML = r.id ? `<span class="saved">${ic("check","sm")} Started: ${esc(r.label || r.action)}</span>` : `<span class="err">${esc(r.error)}</span>`;
  if (r.id) toast("Job started: " + (r.label || r.action));
  setTimeout(() => refresh(true), 800);
}
async function stopRun(){ await api("/api/stop", {}); toast("Stopping the job; finished work is kept"); setTimeout(() => refresh(true), 800); }
function renderJob(){
  const box = $("#jobbox"); if (!box || !S) return;
  const sb = $("#stopbtn"); if (sb) sb.disabled = !S.job.current;
  const j = S.job.current || S.job.last;
  if (!j) { box.innerHTML = `<div class="empty" style="padding:20px">No job has run from this interface yet.</div>`; $("#jobhead").innerHTML = ""; return; }
  const st = S.job.current ? "running" : j.status;
  $("#jobhead").innerHTML = `${S.job.current ? `<span class="spin"></span>` : ""}${badge(st, tone(st), "dot")}${j.usd!=null?`<span class="small mut">cost ${usd(j.usd,4)}</span>`:""}`;
  const old = box.querySelector("pre");
  const atEnd = !old || old.scrollTop + old.clientHeight >= old.scrollHeight - 30;
  const prev = old ? old.scrollTop : 0;
  box.innerHTML = `<div class="small" style="margin-bottom:8px"><b>${esc(j.label || j.action)}</b> <code>sragent ${esc(j.cmd||j.action)}</code></div><pre class="log">${esc(j.log||"")}</pre>`;
  const pre = box.querySelector("pre"); if (pre) pre.scrollTop = atEnd ? pre.scrollHeight : prev;
}

/* ---------------- full texts ---------------- */
async function redraftRob(btn){
  const r = await runOp("rob.redraft_all", {}, "Redraft every risk-of-bias assessment (those you confirmed are kept)");
  const m = r && btn.parentElement.querySelector(".robrd");
  if (m) m.innerHTML = r.ok ? `<span class="saved">${ic("check","sm")} Started; progress is in Activity</span>` : `<span class="err">${esc(r.message)}</span>`;
}
async function processPdfs(btn){
  const r = await runOp("retrieve.process_added_files", {}, "Process the files added to the full-text folder");
  const m = r && btn && btn.parentElement.querySelector(".pdfmsg");
  if (m) m.innerHTML = r.ok ? `<span class="saved">${ic("check","sm")} Started; progress is in Activity</span>` : `<span class="err">${esc(r.message)}</span>`;
}

/* ---------------- shared: model picker for agent jobs ---------------- */
async function ensureModels(){ if (!M) { const m = await api("/api/models"); if (m && m.current) M = m; } return M; }
function defaultAgent(){
  const st = (M && M.settings) || {};
  return runOpts.agent || st.agent || (M ? {provider:M.current.agent.provider, model:M.current.agent.model, effort:"low"} : null);
}
function modelPicker(holder, key){
  // holder[key] = {provider, model, effort}
  if (!M) return `<span class="small mut">Loading models…</span>`;
  if (!holder[key]) holder[key] = {...defaultAgent()};
  const path = holder === ST.rr ? "ST.rr" : holder === ACT ? "ACT" : "ST";
  return `<select style="min-width:260px;max-width:100%" onchange="${path}.${key}={...JSON.parse(this.value),effort:'low'};studyEst()">${modelOptions(holder[key],"agent")}</select>`;
}

/* ---------------- studies ---------------- */
const ST = {list:null, group:"all", status:"", q:"", rid:null, d:null, edit:null, rr:{}, rcomment:"", agent:null};
async function loadStudies(){
  ensureModels();
  if (ST.rid) return openStudy(ST.rid, true);
  if (!ST.list) $("#v-studies").innerHTML = `<div class="card"><div class="empty"><span class="spin" style="display:inline-block"></span></div></div>`;
  ST.list = await api("/api/studies"); ST.stale = false; renderStudies();
}
function filteredStudies(){
  const q = ST.q.toLowerCase();
  return (ST.list||[]).filter(r => (ST.group==="all" || r.group===ST.group) && (!ST.status || r.status===ST.status) &&
    (!q || (r.rid+" "+r.title+" "+r.first_author+" "+r.year+" "+r.reason+" "+r.pmid).toLowerCase().includes(q)));
}
function renderStudies(){
  if (ST.rid) return renderStudy();
  if (tab !== "studies") return;
  const hadFocus = document.activeElement && document.activeElement.id === "studyq";
  const L = ST.list || [];
  const cnt = g => L.filter(r => g==="all" || r.group===g).length;
  const groups = [["all","All"],["included","Included"],["pending","Pending"],["excluded","Excluded"]]
    .map(([g,l]) => `<button class="${ST.group===g?"on":""}" onclick="ST.group='${g}';ST.status='';renderStudies()">${l}<span class="cnt">${cnt(g)}</span></button>`).join("");
  const statuses = [...new Set(L.filter(r => ST.group==="all" || r.group===ST.group).map(r => r.status))].sort();
  const F = filteredStudies();
  const pm = F.map(r => r.pmid).filter(Boolean);
  const rows = F.slice(0, 400).map(r => `<tr tabindex="0" onclick="openStudy('${r.rid}')" onkeydown="if(event.key==='Enter')openStudy('${r.rid}')">
      <td style="min-width:240px"><div class="st-title clamp2"><span class="au">${esc(r.first_author)} ${esc(r.year)}</span> · ${esc(r.title)}</div><div class="st-sub">${esc(r.rid)}<span class="st-src"> · ${esc(r.source)}</span>${r.journal ? `<span class="st-src"> · ${esc(trunc(r.journal, 48))}</span>` : ""}</div></td>
      <td style="min-width:150px;max-width:260px">${badge(r.status, chipFor(r.status), "dot")}${r.reason ? `<div class="reason clamp2">${esc(r.reason)}</div>` : ""}</td>
      <td class="nowrap">${r.rob_overall ? `<span class="rob-chip ${JCOL[r.rob_overall]||""}" style="margin:0"><i></i>${esc(JLAB[r.rob_overall]||r.rob_overall)}${r.rob_confirmed ? " " + ic("check","sm") : ""}</span>` : ""}</td>
      <td class="nowrap">${r.flags_open ? badge(r.flags_open + " open", "bad") : r.flags_total ? `<span class="small mut">${r.flags_total} closed</span>` : ""}</td>
      <td class="small hide-sm" style="min-width:80px">${esc(r.cited_in)}</td><td class="small hide-sm r">${r.comments ? `<span class="mut">${ic("msg","sm")} ${r.comments}</span>` : ""}</td></tr>`).join("");
  $("#v-studies").innerHTML = `
   <div class="card"><div class="card-b"><div class="toolbar">
     <div class="seg">${groups}</div>
     <select onchange="ST.status=this.value;renderStudies()" aria-label="Status" style="max-width:230px"><option value="">Any status</option>${statuses.map(s=>`<option ${ST.status===s?"selected":""}>${esc(s)}</option>`).join("")}</select>
     <div class="search">${ic("search")}<input placeholder="Search title, author, PMID, reason" title="Press / to search" value="${esc(ST.q)}" oninput="ST.q=this.value;clearTimeout(window._sq);window._sq=setTimeout(renderStudies,200)" id="studyq"></div></div></div>
     <div class="card-f"><span class="small"><b>${F.length.toLocaleString()}</b> <span class="mut">shown</span></span><span class="grow"></span>
       <button class="btn sm" data-v="${pm.join(",")}" onclick="copyText(this.dataset.v, this)">${ic("copy","sm")} Copy ${pm.length} PMIDs</button>
       <a class="btn sm" href="${U("/export/studies.csv" + (ST.group!=="all" ? "?group="+ST.group : ""))}">${ic("download","sm")} Table (CSV)</a>
       <a class="btn sm" href="${U(`/export/review.json`)}">${ic("download","sm")} Everything (JSON)</a>
       <button class="btn sm ghost" onclick="loadStudies()" title="Refresh">${ic("refresh","sm")}</button></div></div>
   <details class="card" data-k="revc" ${ST.revOpen ? "open" : ""} ontoggle="ST.revOpen=this.open">
     <summary class="dsum">${ic("msg")}<b>Comment on the whole review</b><span class="small mut hide-sm">${judging() ? "Send it to the agent or keep it as a note" : "Saved as a note"}</span></summary>
     <div style="padding:0 18px 16px">
     <textarea id="revc" oninput="ST.revc=this.value" placeholder="e.g. 'Re-check every population statement for scope' or a note for your co-reviewer">${esc(ST.revc||"")}</textarea>
     ${judging() ? `<div class="row" style="margin-top:10px"><span class="small mut">Agent model</span>${modelPicker(ST, "agent")}
       <button class="btn primary" onclick="reviewComment(true)">${ic("bot","sm")} Send to the agent</button><button class="btn" onclick="reviewComment(false)">Save as a comment only</button><span id="revcm" class="msg"></span></div>
     <div class="hint" style="margin-top:8px">Sent comments go through the same check as feedback: the agent finds the output it is about, checks it against the source and proposes a change set that you approve in the To-do list.</div>`
     : `<div class="row" style="margin-top:10px"><button class="btn" onclick="reviewComment(false)">Save as a comment</button><span id="revcm" class="msg"></span></div><div style="margin-top:8px">${AGENT_ONLY_NOTE}</div>`}</div></details>
   <div class="card"><div class="card-b flush tw"><table class="hover"><thead><tr><th>Study</th><th>Status</th><th>RoB</th><th>Flags</th><th class="hide-sm">Cited in</th><th class="hide-sm"></th></tr></thead><tbody>${rows}</tbody></table>
     ${F.length ? "" : `<div class="empty">No studies match this filter.</div>`}
     ${F.length>400?`<p class="small mut" style="padding:10px 18px;margin:0;border-top:1px solid var(--line)">Showing 400 of ${F.length}; narrow the filter.</p>`:""}</div></div>`;
  if (hadFocus) { const qi = $("#studyq"); qi.focus(); qi.setSelectionRange(qi.value.length, qi.value.length); }
}
async function reviewComment(send){
  const t = $("#revc").value.trim(); if (!t) return;
  const r = await runOp("comment", {target:"review", text:t, send}, send ? "Send to the agent" : "Save the comment", send && ST.agent ? {agent: ST.agent} : null);
  if (!r) return;
  msg("revcm", r); if (r.ok) { $("#revc").value = ""; ST.revc = ""; }
}
async function openStudy(rid, keepEdit){
  nav("studies", rid);
  if (tab !== "studies") activate("studies", true);
  await ensureModels();
  if (ST.rid !== rid) { ST.edit = null; ST.rr = {step:"", note:"", propagate:true, agent:null}; }
  if (!keepEdit) ST.edit = null;
  const first = ST.rid !== rid || !ST.d;
  ST.rid = rid;
  if (first) $("#v-studies").innerHTML = `<div class="card"><div class="empty"><span class="spin" style="display:inline-block"></span></div></div>`;
  try { ST.d = await api("/api/study?rid="+encodeURIComponent(rid)); } catch(e){ ST.d = {error:String(e)}; }
  if (ST.rid !== rid) return;
  renderStudy(); if (!keepEdit) window.scrollTo(0,0);
}
function backToList(){ ST.rid = null; ST.d = null; nav("studies"); loadStudies(); }
function evList(ev, n){
  ev = ev || []; n = n ?? 2;
  if (!ev.length) return `<span class="xs mut">–</span>`;
  const one = e => `<div class="ev"><div class="eh"><span class="id">${esc(e.sid)}</span><span class="sc" title="${esc(e.section)}">${esc(trunc(e.section, 48))}</span></div><div class="eq">“${esc(e.text)}”</div></div>`;
  return ev.slice(0, n).map(one).join("") + (ev.length > n ? `<details class="more"><summary>${ev.length - n} more sentence${ev.length - n > 1 ? "s" : ""}</summary><div style="margin-top:6px">${ev.slice(n).map(one).join("")}</div></details>` : "");
}
// Agent-only mode: people may only supply requested full texts, so judgment controls are not shown
// (the server refuses them anyway). Comments can still be saved as annotations.
function judging(){ return !(S && S.run.human_mode === "agent_only"); }
const AGENT_ONLY_NOTE = `<div class="callout info" style="margin:0">${IC.info ? `<svg class="i" viewBox="0 0 24 24">${IC.info}</svg>` : ""}<div>Agent-only mode: people may only supply the full texts the agent requested, so re-runs, edits and comments sent to the agent are not available. Comments can be saved as notes.</div></div>`;
function sec(title, body, right, id){ return `<section class="card"${id ? ` id="${id}"` : ""}><div class="card-h"><h3>${title}</h3>${right ? `<div class="card-act">${right}</div>` : ""}</div><div class="card-b">${body}</div></section>`; }
function renderStudy(){
  const d = ST.d; if (!d) return;
  const crumbs = `<div class="crumbs"><button class="link" onclick="backToList()">${ic("arrowL","sm")} Studies</button><span class="sep">/</span><span class="mono">${esc(d.rid || ST.rid)}</span></div>`;
  if (d.error) { $("#v-studies").innerHTML = crumbs + `<div class="callout warn">${ic("warn")}<div>${esc(d.error)}</div></div>`; return; }
  const r = d.record, E = ST.edit || {};
  const links = Object.entries(d.links).map(([k,v]) => `<a class="btn sm" target="_blank" href="${esc(v)}">${esc(k)} ${ic("ext","sm")}</a>`).join("");
  const out = [];
  out.push(`<div class="row" style="margin-bottom:14px">${crumbs.replace('class="crumbs"', 'class="crumbs" style="margin:0"')}<span class="grow"></span>
     <button class="btn sm" onclick="copyStudy(this)">${ic("copy","sm")} Copy as Markdown</button>
     <a class="btn sm" href="${U(`/export/study/${d.rid}.md`)}">${ic("download","sm")} .md</a>
     <a class="btn sm" href="${U(`/export/study/${d.rid}.json`)}">${ic("download","sm")} .json</a>
     <button class="btn sm ghost" onclick="openStudy('${d.rid}')" title="Refresh">${ic("refresh","sm")}</button></div>`);
  out.push(`<div class="card shead"><div class="pt">${esc((r.pub_types||[]).join(" · "))}</div>
     <h2>${esc(r.title)}</h2><div class="au">${esc(r.authors)}</div><div class="small mut"><i>${esc(r.journal)}</i> ${esc(r.year)}</div>
     <div class="row" style="margin-top:12px">${badge(d.status, chipFor(d.status), "dot")}${d.reason ? `<span class="small mut">${esc(d.reason)}</span>` : ""}
       ${d.pdf_wanted ? badge("waiting for your PDF","warn") : ""}${d.pdf_unavailable ? badge("marked not obtainable") : ""}</div>
     ${d.source ? `<div class="src">Source used by the agent: <b style="color:var(--fg-2)">${esc(d.source.kind)}</b> · ${esc(d.source.origin)} · ${Number(d.source.chars).toLocaleString()} characters
       ${d.source.local_file ? ` · <a target="_blank" href="/files/fulltext/${encodeURIComponent(d.source.local_file)}?config=${encodeURIComponent(CFG)}">your file ${ic("ext","sm")}</a>` : ""}
       ${d.source.auto_pdf ? ` · <a target="_blank" href="/files/fulltext/${encodeURIComponent(d.source.auto_pdf)}?config=${encodeURIComponent(CFG)}">PDF the agent fetched ${ic("ext","sm")}</a>` : ""}
       ${d.source.auto_text ? ` · <a target="_blank" href="/files/fulltext/${encodeURIComponent(d.source.auto_text)}?config=${encodeURIComponent(CFG)}">text ${ic("ext","sm")}</a>` : ""}
       ${!d.source.auto_pdf && d.source.pmcid ? ` · <a target="_blank" href="https://europepmc.org/articles/${esc(d.source.pmcid)}?pdf=render">PDF at Europe PMC ${ic("ext","sm")}</a> <span class="xs mut">(your browser can; the server was refused)</span>` : ""}</div>` : ""}
     <div class="lk">${links}</div>
     ${r.abstract ? `<details style="margin-top:12px"><summary>Abstract</summary><div class="abstract">${esc(r.abstract)}</div></details>` : ""}</div>`);

  // in-page navigation
  const navItems = [["s-ask","Ask the agent", judging()], ["s-elig","Eligibility", !!d.screening], ["s-ext","Extracted data", !!d.extraction], ["s-rob","Risk of bias", !!d.rob],
    ["s-syn",`Synthesis <span class="cnt">${d.synthesis.length}</span>`, d.synthesis.length], ["s-prop",`Proposals <span class="cnt">${d.proposals.length}</span>`, d.proposals.length],
    ["s-flags",`Flags <span class="cnt ${d.flags.some(f=>f.status==="open")?"warn":""}">${d.flags.length}</span>`, true], ["s-com",`Comments <span class="cnt">${d.comments.length}</span>`, true],
    ["s-hist",`History <span class="cnt">${d.history.length}</span>`, true]].filter(x => x[2]);
  out.push(`<div class="subnav">${navItems.map(([id,l]) => `<button onclick="scrollToId('${id}')">${l}</button>`).join("")}</div>`);

  // agent actions
  const steps = [["screen","Screening (eligibility)"]].concat(d.group==="included" ? [["extract","Data extraction"]] : []).concat(d.extraction && d.group==="included" ? [["rob","Risk of bias"]] : []);
  const rr = ST.rr;
  if (judging()) out.push(`<details class="card" id="s-ask" ${ST.askOpen || rr.step ? "open" : ""} ontoggle="ST.askOpen=this.open"><summary class="dsum">${ic("bot")}<b>Ask the agent</b><span class="small mut hide-sm" style="font-weight:400">Re-run screening, extraction or risk of bias for this study</span></summary><div class="card-b" style="border-top:1px solid var(--line)"><div class="form">
    <label>Re-run this step</label><div><div class="seg">${steps.map(([v,l]) => `<button class="${rr.step===v?"on":""}" onclick="ST.rr.step='${v}';renderStudy();studyEst()">${l}</button>`).join("")}</div></div>
    <label>Model</label><div>${modelPicker(ST.rr, "agent")}</div>
    <label>Note for the agent</label><div><textarea placeholder="Optional, e.g. 'the table on page 5 has the steady-state troughs'" oninput="ST.rr.note=this.value">${esc(rr.note||"")}</textarea>
      <label class="small" style="display:flex;gap:6px;align-items:center;margin-top:8px"><input type="checkbox" ${rr.propagate?"checked":""} onchange="ST.rr.propagate=this.checked;studyEst()"> Also propose updates to the outputs that depend on it (recommended)</label></div>
    <label></label><div class="row"><button class="btn primary" ${rr.step?"":"disabled"} onclick="doRerun()">${ic("play","sm")} Re-run</button><span id="rrest" class="small mut"></span><span id="rrmsg" class="msg"></span></div>
    </div><div class="hint" style="margin-top:14px">The new result is not applied directly. It appears as a proposal (with the dependent changes) in the To-do list and below; accepting it updates everything at once. Comments at the bottom of this page can also be sent to the agent.</div></div></details>`);

  const editBar = (label) => `${editNote()}${impactLine()}<div class="row" style="margin-top:12px"><button class="btn primary" onclick="saveEdit()">${label}</button><button class="btn ghost" onclick="ST.edit=null;renderStudy()">Cancel</button><span id="edmsg" class="msg"></span></div>`;
  // screening
  if (d.screening) {
    const s = d.screening;
    const AS = {met:"ok", not_met:"bad"};
    const crit = s.criteria.map(c => `<tr><td class="nowrap"><b class="mono">${esc(c.id)}</b><div class="xs mut">${esc(c.type)}</div></td><td class="small" style="min-width:180px">${esc(c.text)}</td>
        <td>${badge(human(c.assessment), AS[c.assessment] || "warn", "sq")}${c.settled ? `<div class="xs mut" style="margin-top:4px">agent said "${esc(c.model_assessment)}": ${esc(c.settled)}</div>` : ""}${c.reason ? `<div class="xs mut" style="margin-top:4px;max-width:260px">${esc(c.reason)}</div>` : ""}</td><td style="min-width:300px">${evList(c.evidence)}</td></tr>`).join("");
    const crits = s.criteria.map(c => c.id);
    const ed = E.kind === "screen" ? `<div class="robedit"><div class="form">
        <label>Decision</label><div><div class="seg">${["include","exclude","awaiting"].map(v => `<button class="${E.decision===v?"on":""}" onclick="ST.edit.decision='${v}';renderStudy()">${human(v)}</button>`).join("")}</div></div>
        ${E.decision==="exclude"?`<label>Reason</label><div><select onchange="ST.edit.criterion=this.value"><option value="">Criterion…</option>${crits.concat(["other"]).map(c=>`<option ${E.criterion===c?"selected":""}>${esc(c)}</option>`).join("")}</select></div>`:""}
        </div>${editBar("Save (no LLM)")}</div>` : "";
    out.push(sec("Eligibility", `<div class="row">${badge(s.decision, tone(s.decision) || "", "dot")}
        <span class="small mut">${esc(human(s.stage))} · because ${esc((s.because||[]).join(", ")||"–")}${s.decided_by ? ` · decided by ${esc(s.decided_by.replace("+", " + "))}` : ""} · v${s.version}</span></div>
        ${s.eligibility_review ? reviewBox(s.eligibility_review) : ""}${s.screen_verify ? verifyBox(s.screen_verify) : ""}
        ${s.rule?`<div class="small" style="margin-top:8px">Rule <b>${esc(s.rule)}</b>: ${esc(s.rule_reason)}</div>`:""}
        ${s.human_decision?`<div class="small" style="margin-top:8px">Your decision: <b>${esc(s.human_decision.decision)}</b> ${esc(s.human_decision.reason||"")} ${esc(s.human_decision.note||"")}</div>`:""}
        ${s.llm_reason?`<div class="quote"><span class="xs mut" style="display:block;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Agent</span>${esc(s.llm_reason)}</div>`:""}
        ${s.title_abstract?`<div class="xs mut" style="margin-top:8px">Title/abstract stage: ${esc(s.title_abstract.decision)}</div>`:""}
        ${ed}
        ${crit?`<div class="tw" style="margin-top:14px;border:1px solid var(--line);border-radius:10px"><table><thead><tr><th>Criterion</th><th></th><th>Assessment</th><th>Evidence</th></tr></thead><tbody>${crit}</tbody></table></div>`:""}`,
      E.kind==="screen" || !judging() ? "" : `<button class="btn sm" onclick="ST.edit={kind:'screen',decision:'${s.decision}',criterion:'',note:''};renderStudy()">${ic("edit","sm")} Edit</button>`, "s-elig"));
  }
  // extraction
  if (d.extraction) {
    const x = d.extraction, editing = E.kind === "ext";
    const rows = x.fields.map(f => `<tr><td style="width:150px"><b>${esc(human(f.name))}</b></td>
        <td style="min-width:240px">${editing ? `<textarea oninput="ST.edit.fields['${f.name}']=this.value">${esc(E.fields[f.name])}</textarea>` : esc(f.value)}
          ${f.edited?`<div class="xs mut" style="margin-top:4px">${ic("edit","sm")} edited by you; was “${esc(String(f.edited.before).slice(0,120))}”</div>`:""}
          ${(f.terms||[]).map(t => `<div class="small" style="margin-top:4px">${badge(human(t.status), t.status==="validated"?"ok":t.status==="not_found"?"bad":"warn","sq")} ${esc(t.term)} <span class="xs mut mono">${esc(Object.entries(t.ids||{}).map(([k,v]) => k+":"+v).join(" "))}</span></div>`).join("")}</td>
        <td style="min-width:280px">${evList(f.evidence)}${f.check && f.check!=="located"?`<div style="margin-top:4px">${badge(f.check==="none" ? "no source sentence" : f.check,"warn")}</div>`:""}${f.note ? `<div class="xs mut" style="margin-top:4px"><span style="font-weight:600;text-transform:uppercase;letter-spacing:.05em">Agent</span> ${esc(f.note)}</div>` : ""}${(f.uncovered||[]).length ? `<div class="xs mut" style="margin-top:4px" title="A sum, a rounding or a different spelling in the paper is the usual reason; nothing is required of you">not found as written in the paper: ${esc(f.uncovered.join(", "))}</div>` : ""}</td></tr>`).join("");
    out.push(sec(`Extracted data <span class="small mut" style="font-weight:400">v${x.version} · from ${esc(x.source_kind)}${x.status!=="active"?" · "+esc(x.status):""}</span>`,
      `<div class="tw" style="margin:-16px -18px"><table><thead><tr><th>Item</th><th>Value</th><th>Evidence</th></tr></thead><tbody>${rows}</tbody></table></div>
       ${editing?`<div style="margin-top:28px">${editBar("Save (no LLM)")}</div>
       <div class="hint" style="margin-top:8px">The cited sentences stay as the agent's evidence; your value is marked as edited, and the outputs that use it are listed in the To-do list for an optional update.</div>`:""}`,
      editing || !judging() ? "" : `<button class="btn sm" onclick="editExt()">${ic("edit","sm")} Edit values</button>`, "s-ext"));
  }
  // rob
  if (d.rob) {
    const b = d.rob, editing = E.kind === "rob";
    const rows = b.domains.map((x,i) => `<tr><td style="width:180px"><b>${esc(human(x.domain))}</b></td>
        <td style="width:160px">${editing ? `<select onchange="ST.edit.changes[${i}].judgment=this.value">${["low","some_concerns","high","unclear"].map(j=>`<option value="${j}" ${E.changes[i].judgment===j?"selected":""}>${JLAB[j]}</option>`).join("")}</select>`
          : `<span class="rob-chip ${JCOL[x.judgment]||""}" style="cursor:default"><i></i>${esc(JLAB[x.judgment]||x.judgment)}</span>`}
          ${x.judgment_agent && x.judgment_agent!==x.judgment?`<div class="xs mut">agent: ${esc(JLAB[x.judgment_agent]||x.judgment_agent)}${x.decided_by==="verifier"?"; set by the verifier":""}</div>`:""}</td>
        <td class="small" style="min-width:200px">${editing ? `<textarea oninput="ST.edit.changes[${i}].rationale=this.value">${esc(E.changes[i].rationale)}</textarea>` : esc(x.rationale)}</td>
        <td style="min-width:260px">${evList(x.evidence, 1)}</td></tr>`).join("");
    out.push(sec(`Risk of bias <span class="small mut" style="font-weight:400">overall <b style="color:var(--fg-2)">${esc(JLAB[b.overall]||b.overall)}</b> · ${b.confirmed?"confirmed":(b.verify && b.verify.outcome!=="still_undecided"?"draft, checked by the verifier":"draft")} · v${b.version}</span>`,
      `<div class="tw" style="margin:-16px -18px"><table><thead><tr><th>Domain</th><th>Judgment</th><th>Rationale</th><th>Evidence</th></tr></thead><tbody>${rows}</tbody></table></div>
       ${(b.reasons||[]).length || b.verify ? `<div style="margin-top:26px">${robWhy({reasons: b.reasons, verify: b.verify})}</div>` : ""}
       ${editing?`<div style="margin-top:28px">${editBar("Save and confirm (no LLM)")}</div>`:""}`,
      editing || !judging() ? "" : `<button class="btn sm" onclick="editRob()">${ic("edit","sm")} Edit domains</button>`, "s-rob"));
  }
  // synthesis
  if (d.synthesis.length) {
    out.push(sec("Cited in the synthesis", d.synthesis.map(s => {
      const editing = E.kind === "syn" && E.node === s.id;
      return `<div class="item"><div class="ih"><span class="badge info sq mono">${esc(s.id.replace("syn:",""))}</span>
          <span class="meta">cites ${esc(s.cites.join(", "))} · self-check ${esc(s.support||"–")}</span><span class="grow"></span>
          ${!editing && judging() ? `<button class="btn sm ghost" onclick="editSyn('${s.id}')">${ic("edit","sm")} Edit</button>` : ""}</div>
        ${editing ? `<textarea style="min-height:90px;margin-top:8px" oninput="ST.edit.text=this.value">${esc(E.text)}</textarea>
           <div class="field" style="margin-top:8px"><span class="small mut nowrap">Cites</span><input value="${esc(E.cites)}" oninput="ST.edit.cites=this.value"></div>${editBar("Save (no LLM)")}`
        : `<div class="body">${esc(s.text)}</div>`}</div>`; }).join(""), "", "s-syn"));
  }
  // proposals
  if (d.proposals.length) {
    out.push(sec("Change proposals for this study", d.proposals.map(p => `<div class="item">
      <div class="ih">${badge(p.status, tone(p.status), "dot")} <b class="mono">${esc(p.id)}</b> <span class="meta">${esc(p.kind)} · ${fmtT(p.created)}${p.model?" · "+esc(p.model):""}${p.cost_usd!=null?" · $"+p.cost_usd:""}</span></div>
      <div class="body">${esc(p.request)}</div>${p.explanation?`<div class="small mut" style="margin-top:4px">${esc(p.explanation)}</div>`:""}
      <ul>${p.changes.map(c => `<li><b>${esc(c.node)}</b> (${esc(c.role)}): ${esc(c.what.join("; "))}</li>`).join("")}</ul>
      ${p.status==="proposed"?`<div class="row" style="margin-top:10px"><button class="btn primary sm" onclick="decideCs('${p.id}','accept',this)">${ic("check","sm")} Accept</button><button class="btn sm" onclick="decideCs('${p.id}','reject',this)">Reject</button></div>`:""}</div>`).join(""), "", "s-prop"));
  }
  // flags
  out.push(sec(`Flags <span class="cnt">${d.flags.length}</span>`, d.flags.length ? `<div class="tw" style="margin:-16px -18px"><table><tbody>${d.flags.map(f => `<tr><td style="width:90px">${badge(f.status, f.status==="open"?"bad":"", "dot")}</td>
     <td class="small" style="width:220px"><b class="mono">${esc(f.node)}</b><div class="xs mut">${esc(f.source||"")} · ${esc(f.actor||"")} · ${fmtT(f.ts)}</div></td><td class="small">${esc(f.text)}${f.resolution?`<div class="mut" style="margin-top:2px">→ ${esc(f.resolution)}</div>`:""}</td></tr>`).join("")}</tbody></table></div>` : `<div class="small mut">No flags.</div>`, "", "s-flags"));
  // comments
  const targets = [["study:"+d.rid,"this study"]].concat(d.nodes.filter(n => !n.startsWith("rec:") && !n.startsWith("ft:")).map(n => [n, n]));
  out.push(sec(`Comments <span class="cnt">${d.comments.length}</span>`, `${d.comments.map(c => `<div class="item">
      <div class="ih">${badge(c.status, c.status==="open"?"warn":"", "dot")} <span class="meta">${esc(c.id)} on <b>${esc(c.target)}</b> · ${fmtT(c.ts)}${c.send_to_agent?" · sent to agent"+(c.job_id?" (job "+esc(c.job_id)+")":""):""}</span>
        <span class="grow"></span>${c.status==="open"?`<button class="btn sm ghost" onclick="resolveComment('${c.id}')">${ic("check","sm")} Mark resolved</button>`:""}</div>
      <div class="body">${esc(c.text)}</div>${c.status!=="open" && c.resolution ? `<div class="small mut" style="margin-top:2px">→ ${esc(c.resolution)}</div>` : ""}</div>`).join("")}
    <div style="${d.comments.length ? "margin-top:16px;padding-top:16px;border-top:1px solid var(--line)" : ""}">
    <div class="row"><span class="small mut">About</span><select id="ctarget" onchange="ST.ctarget=this.value">${targets.map(([v,l]) => `<option value="${v}" ${ST.ctarget===v?"selected":""}>${esc(l)}</option>`).join("")}</select></div>
    <textarea id="ctext" style="margin-top:8px" placeholder="Your comment" oninput="ST.rcomment=this.value">${esc(ST.rcomment)}</textarea>
    ${judging() ? `<div class="row" style="margin-top:10px"><button class="btn primary" onclick="studyComment(true)">${ic("bot","sm")} Send to the agent</button><button class="btn" onclick="studyComment(false)">Save as a comment only</button><span id="cmsg" class="msg"></span></div>
    <div class="hint" style="margin-top:8px">"Send to the agent" uses the model chosen under "Ask the agent" above. The agent checks your comment against the source text; if it agrees it proposes a revision (shown above and in the To-do list), if it disagrees it explains why.</div>`
    : `<div class="row" style="margin-top:10px"><button class="btn" onclick="studyComment(false)">Save as a comment</button><span id="cmsg" class="msg"></span></div><div style="margin-top:10px">${AGENT_ONLY_NOTE}</div>`}</div>`, "", "s-com"));
  // history
  out.push(sec(`History <span class="cnt">${d.history.length}</span>`, `<div class="tw" style="margin:-16px -18px"><table><thead><tr><th>When</th><th>Output</th><th>What</th><th>Who</th><th>Why</th></tr></thead><tbody>
     ${d.history.slice().reverse().map(h => `<tr${h.seq != null ? ` id="h-${h.seq}"` : ""}><td class="small nowrap">${fmtT(h.ts)}${h.seq != null ? `<div class="mut mono">#${h.seq}</div>` : ""}</td><td class="small mono">${esc(h.node)}</td>
       <td class="small">${esc(h.op)}: ${esc(h.changes.join("; "))}${h.operation ? `<div class="mut">by ${esc(h.operation)}</div>` : ""}</td><td class="small">${badge(h.actor, h.actor==="user"?"info":"")}</td><td class="small mut">${esc(h.reason)}${histBecause(h)}</td></tr>`).join("")}</tbody></table></div>`, "", "s-hist"));
  $("#v-studies").innerHTML = out.join("");
  studyEst();
}
/* the change that set this one off (the cause chain): a link to its row when it is in this table */
function histBecause(h){
  if (!(h.because || []).length) return "";
  return `<div class="small" style="margin-top:3px">because ${h.because.map(b => `<a href="javascript:void 0" onclick="const r=document.getElementById('h-${b.seq}'); if(r){r.scrollIntoView({block:'center'}); r.style.background='var(--warn-bg,#fff6db)'}">#${b.seq}</a> ${esc(b.text)}`).join("; ")}</div>`;
}
function impactNode(){ const E = ST.edit || {}; if (!ST.d) return null; return E.kind === "syn" ? E.node : E.kind ? `${E.kind}:${ST.d.rid}` : null; }
function impactLine(){
  const node = impactNode(); if (!node) return "";
  if (!ST.impact || ST.impact.node !== node) {
    if (!ST.impactLoading) { ST.impactLoading = node; api("/api/impact?node=" + encodeURIComponent(node)).then(r => { ST.impactLoading = null; ST.impact = r; if (impactNode() === node) renderStudy(); }); }
    return `<div class="hint" style="margin-top:8px"><span class="spin" style="display:inline-block;vertical-align:-2px"></span> Checking what this change would reach…</div>`;
  }
  const I = ST.impact, reach = Object.entries(I.reach || {});
  if (!reach.length) return `<div class="hint" style="margin-top:8px">${ic("info","sm")} No other output depends on this one.</div>`;
  return `<div class="callout info" style="margin:10px 0 0;font-size:12.5px">${ic("info")}<div>This change would reach ${reach.map(([t, ids]) => `<b>${ids.length}</b> ${esc(t)}${ids.length>1?"s":""} (${ids.slice(0,4).map(esc).join(", ")}${ids.length>4?", …":""})`).join(", ")}${I.cost && I.cost.total_usd != null ? `; bringing them up to date costs about ${usd(I.cost.total_usd)}` : ""}. After saving, the To-do list offers the update.</div></div>`;
}
function editNote(){ return `<div class="form" style="margin-top:12px"><label>Note (kept in the history)</label><input placeholder="Optional" value="${esc((ST.edit||{}).note||"")}" oninput="ST.edit.note=this.value"></div>`; }
function editSyn(id){ const s = ST.d.synthesis.find(x => x.id === id); ST.edit = {kind:"syn", node:id, text:s.text, cites:s.cites.join(", "), note:""}; renderStudy(); }
function editExt(){ const f = {}; ST.d.extraction.fields.forEach(x => f[x.name] = x.value); ST.edit = {kind:"ext", fields:f, orig:{...f}, note:""}; renderStudy(); }
function editRob(){ ST.edit = {kind:"rob", changes: ST.d.rob.domains.map(x => ({domain:x.domain, judgment:x.judgment, rationale:x.rationale||""})), note:""}; renderStudy(); }
async function saveEdit(){
  const E = ST.edit, d = ST.d; let body;
  // the same operations as the stage pages (sragent/ops), previewed when they reach other outputs
  if (E.kind === "screen") { if (E.decision==="exclude" && !E.criterion) { msg("edmsg",{ok:false,message:"Choose a reason"}); return; }
    body = ["eligibility.decide", {rid:d.rid, decision:E.decision, criterion:E.criterion, note:E.note}, `Eligibility of ${d.rid}: ${E.decision}`]; }
  if (E.kind === "ext") { const ch = {}; Object.keys(E.fields).forEach(k => { if (E.fields[k] !== E.orig[k]) ch[k] = E.fields[k]; });
    body = ["extract.edit_values", {rid:d.rid, fields:ch, note:E.note}, `Extracted values of ${d.rid}`]; }
  if (E.kind === "rob") body = (E.changes || []).length ? ["rob.edit_domains", {rid:d.rid, changes:E.changes, note:E.note}, `Risk of bias of ${d.rid}`]
                                                       : ["rob.confirm", {rids:[d.rid], note:E.note}, `Confirm the risk of bias of ${d.rid}`];
  if (E.kind === "syn") body = ["synthesis.edit_statement", {node:E.node, text:E.text, cites:E.cites, note:E.note}, `Statement ${E.node}`];
  const r = await runOp(...body);
  if (!r) return;                      // cancelled in the preview
  msg("edmsg", r);
  if (r.ok) { ST.edit = null; setTimeout(() => { openStudy(d.rid, true); }, 300); }
}
async function studyEst(){
  const el = $("#rrest"); if (!el || !ST.rr.step) { if (el) el.textContent = ""; return; }
  const e = await api("/api/estimate", {action:"rerun", step:ST.rr.step, propagate:ST.rr.propagate, agent:ST.rr.agent});
  if ($("#rrest")) $("#rrest").textContent = e.total_usd!=null ? `about $${e.total_usd.toFixed(3)}` : "";
}
async function doRerun(){
  const name = {screen: "eligibility.recompute", extract: "extract.recompute", rob: "rob.recompute"}[ST.rr.step];
  const r = await runOp(name, {rid:ST.d.rid, note:ST.rr.note||"", propagate:ST.rr.propagate}, `Re-run ${ST.rr.step} for ${ST.d.rid}`,
                        ST.rr.agent ? {agent: ST.rr.agent} : null);
  if (r) msg("rrmsg", r);
}
async function studyComment(send){
  const t = $("#ctext").value.trim(); if (!t) return;
  const ag = send ? (ST.rr.agent || defaultAgent()) : null;
  const r = await runOp("comment", {target:$("#ctarget").value, text:t, send}, send ? "Send to the agent" : "Save the comment", ag ? {agent: ag} : null);
  if (!r) return;
  msg("cmsg", r); if (r.ok) { ST.rcomment = ""; setTimeout(() => openStudy(ST.d.rid, true), 300); }
}
async function resolveComment(id){ const r = await runOp("comment.resolve", {id}, "Resolve the comment"); if (r && tab==="studies" && ST.rid) openStudy(ST.rid, true); else if (r) loadActivity(); }
async function decideCs(id, opt, b){
  b.disabled = true;
  const r = await runOp(opt === "accept" ? "proposal.accept" : "proposal.reject", {changeset: id}, (opt === "accept" ? "Accept" : "Reject") + ` proposal ${id}`);
  if (!r) { b.disabled = false; return; }
  setTimeout(() => { if (tab==="studies" && ST.rid) openStudy(ST.rid, true); else loadActivity(); }, 300);
}
async function copyStudy(b){ const t = await (await fetch(U(`/export/study/${ST.d.rid}.md`))).text(); copyText(t, b); }

/* ---------------- activity (global history) ---------------- */
const ACT = {sub:"jobs", actor:"", q:"", H:null, J:null, open:{}};
async function loadActivity(){
  if (!ACT.H) $("#v-activity").innerHTML = `<div class="card"><div class="empty"><span class="spin" style="display:inline-block"></span></div></div>`;
  [ACT.J, ACT.H] = await Promise.all([api("/api/jobs"), api("/api/history?actor="+encodeURIComponent(ACT.actor)+"&q="+encodeURIComponent(ACT.q))]);
  renderActivity();
}
function jobRow(j, kind){
  const st = j.status, dur = j.started ? Math.round(((j.ended||Date.now()/1000) - j.started)) : null;
  return `<tr><td class="small nowrap">${fmtT(j.created)}</td>
    <td><b>${esc(j.label||j.action)}</b>${j.rid?` <button class="link small" onclick="openStudy('${esc(j.rid)}')">study ${esc(j.rid)}</button>`:""}${j.note||j.text?`<div class="xs mut">${esc((j.note||j.text||"").slice(0,160))}</div>`:""}</td>
    <td class="small mono">${esc(j.model||"")}</td>
    <td>${st === "running" ? `<span class="badge info"><span class="spin" style="width:9px;height:9px;border-width:1.5px"></span>running</span>` : badge(st, tone(st), "dot")}</td>
    <td class="small r num">${j.usd!=null?"$"+j.usd:""}</td><td class="small r num">${dur!=null?fmtDur(dur):""}</td>
    <td class="small nowrap r">${kind==="queued"?`<button class="btn sm ghost danger" onclick="cancelJob('${j.id}')">Cancel</button>`:""}
      ${kind!=="queued"?`<button class="btn sm ghost" onclick="toggleLog('${j.id}')">${ACT.open[j.id]?"Hide log":"Log"}</button>`:""}</td></tr>
    ${ACT.open[j.id]?`<tr><td colspan="7" style="background:var(--surface-2)"><pre class="log" id="log-${j.id}">${esc(ACT.open[j.id])}</pre></td></tr>`:""}`;
}
function renderJobsCard(fromPoll){
  const box = $("#jobs-card"); if (!box) return;
  if (fromPoll) { api("/api/jobs").then(J => { if (unchanged("jobs", J)) return; ACT.J = J; const b = $("#jobs-card"); if (b) b.innerHTML = jobsHtml(); }); return; }
  box.innerHTML = jobsHtml();
}
function jobsHtml(){
  const J = ACT.J || {running:[],queued:[],done:[]};
  const rows = J.running.map(j => jobRow(j,"running")).join("") + J.queued.map(j => jobRow(j,"queued")).join("") + J.done.map(j => jobRow(j,"done")).join("");
  return `<div class="card-h"><div class="row grow"><b>${J.running.length} running · ${J.queued.length} queued · ${J.done.length} finished</b>
     ${J.pending_answers?badge(`${J.pending_answers} of your answers wait for the running job`,"warn"):""}</div>
     <a class="btn sm" href="${U(`/export/jobs.csv`)}">${ic("download","sm")} CSV</a></div>
     ${rows?`<div class="card-b flush tw"><table><thead><tr><th>Queued</th><th>Task</th><th>Model</th><th>Status</th><th class="r">Cost</th><th class="r">Time</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`:`<div class="empty">No LLM jobs yet.</div>`}`;
}
async function toggleLog(id){
  if (ACT.open[id]) { delete ACT.open[id]; }
  else { ACT.open[id] = await (await fetch(U("/api/job_log?id="+id))).text(); }
  $("#jobs-card").innerHTML = jobsHtml();
}
async function cancelJob(id){ await api("/api/cancel", {id}); toast("Job cancelled"); loadActivity(); refresh(); }
function renderActivity(){
  const H = ACT.H || {proposals:[],decisions:[],changelog:[],comments:[],llm_by_tag:[]};
  const subs = [["jobs","LLM jobs",null],["proposals","Agent proposals",H.proposals.length],["comments","Comments",H.comments.length],
                ["decisions","Your decisions",H.decisions.length],["changes","Change history",H.changelog_total||0],["usage","LLM calls by stage",null]];
  const tabs = `<div class="seg">${subs.map(([v,l,n]) => `<button class="${ACT.sub===v?"on":""}" onclick="ACT.sub='${v}';renderActivity()">${l}${n!=null?`<span class="cnt">${n}</span>`:""}</button>`).join("")}</div>`;
  const none = t => `<div class="empty">${t}</div>`;
  let body = "";
  if (ACT.sub === "jobs") body = `<div class="card" id="jobs-card">${jobsHtml()}</div><p class="hint">Every agent task started from this interface (runs, re-runs, comments sent to the agent, updates). The list refreshes every few seconds.</p>`;
  if (ACT.sub === "proposals") body = `<div class="card"><div class="card-b">${H.proposals.length ? H.proposals.map(p => `<div class="item">
      <div class="ih">${badge(p.status, tone(p.status), "dot")} <b class="mono">${esc(p.id)}</b> <span class="meta">${esc(p.kind)} · target ${esc(p.target||"–")} · ${fmtT(p.created)}${p.cost_usd!=null?" · $"+p.cost_usd:""}</span></div>
      <div class="body">${esc(p.request)}</div>${p.explanation?`<div class="small mut" style="margin-top:4px">${esc(p.verdict||"")}: ${esc(p.explanation.slice(0,400))}</div>`:""}
      ${p.changes.length?`<ul>${p.changes.map(c => `<li><b>${esc(c.node)}</b> (${esc(c.role)}): ${esc(c.what.join("; ").slice(0,400))}</li>`).join("")}</ul>`:""}
      ${p.status==="proposed"?`<div class="row" style="margin-top:10px"><button class="btn primary sm" onclick="decideCs('${p.id}','accept',this)">${ic("check","sm")} Accept</button><button class="btn sm" onclick="decideCs('${p.id}','reject',this)">Reject</button></div>`:""}</div>`).join("") : none("No proposals yet.")}</div></div>`;
  if (ACT.sub === "comments") body = `<div class="card"><div class="card-b">${H.comments.length ? H.comments.map(c => `<div class="item">
      <div class="ih">${badge(c.status, c.status==="open"?"warn":"", "dot")} <span class="meta">${esc(c.id)} on ${c.target.includes(":")?`<button class="link small" onclick="openStudy('${esc(c.target.split(":")[1])}')">${esc(c.target)}</button>`:esc(c.target)} · ${fmtT(c.ts)}${c.send_to_agent?" · sent to agent":""}</span>
        <span class="grow"></span>${c.status==="open"?`<button class="btn sm ghost" onclick="resolveComment('${c.id}')">${ic("check","sm")} Mark resolved</button>`:""}</div>
      <div class="body">${esc(c.text)}</div></div>`).join("") : none("No comments yet. Add them on a study page or in the report.")}</div></div>`;
  if (ACT.sub === "decisions") body = `<div class="card"><div class="card-b flush tw">${H.decisions.length ? `<table><thead><tr><th>When</th><th>Item</th><th>Answer</th><th>Note</th><th>Status</th></tr></thead><tbody>${H.decisions.map(x => `<tr>
      <td class="small nowrap">${fmtT(x.ts)}</td><td class="small mono">${esc(x.todo_id||"")}</td><td class="small">${esc(x.option||"")}${x.extra&&Object.keys(x.extra).length?" "+esc(JSON.stringify(x.extra).slice(0,160)):""}</td>
      <td class="small">${esc(x.text||"")}</td><td class="small">${x.status ? badge(x.status, tone(x.status)) : ""}</td></tr>`).join("")}</tbody></table>` : none("No decisions yet.")}</div></div>`;
  if (ACT.sub === "changes") body = `<div class="card"><div class="card-h"><div class="toolbar grow"><div class="seg">${[["","Everyone"],["agent","Agent"],["user","You"],["rule","Rules"]].map(([v,l]) => `<button class="${ACT.actor===v?"on":""}" onclick="ACT.actor='${v}';loadActivity()">${l}</button>`).join("")}</div>
      <div class="search">${ic("search")}<input placeholder="Filter by output id or reason (e.g. ext:30157389), then Enter" value="${esc(ACT.q)}" onchange="ACT.q=this.value;loadActivity()"></div>
      <a class="btn sm" href="${U(`/export/history.csv`)}">${ic("download","sm")} All (CSV)</a></div></div>
      <div class="card-b flush tw"><table><thead><tr><th>When</th><th>Output</th><th>What</th><th>Who</th><th>Why</th></tr></thead><tbody>${H.changelog.map(h => `<tr${h.seq != null ? ` id="h-${h.seq}"` : ""}><td class="small nowrap">${fmtT(h.ts)}${h.seq != null ? `<div class="mut mono">#${h.seq}</div>` : ""}</td>
      <td class="small mono nowrap">${(h.node.split(":")[1]||"").match(/^[0-9d]/) ? `<button class="link small mono" onclick="openStudy('${esc(h.node.split(":")[1])}')">${esc(h.node)}</button>` : esc(h.node)}</td>
      <td class="small">${esc(h.op)}: ${esc(h.changes.join("; ").slice(0,300))}${h.operation ? `<div class="mut">by ${esc(h.operation)}</div>` : ""}</td><td class="small">${badge(h.actor, h.actor==="user"?"info":"")}</td><td class="small mut">${esc(h.reason.slice(0,200))}${histBecause(h)}</td></tr>`).join("")}</tbody></table>
      ${H.changelog.length ? "" : none("No changes match.")}
      ${H.changelog_total>H.changelog.length?`<p class="small mut" style="padding:10px 18px;margin:0;border-top:1px solid var(--line)">Showing the latest ${H.changelog.length} of ${H.changelog_total}; download the CSV for all.</p>`:""}</div></div>`;
  if (ACT.sub === "usage") { const tot = H.llm_by_tag.reduce((a,u) => a + u.usd, 0);
    body = `<div class="card"><div class="card-b flush tw"><table><thead><tr><th>Stage (tag)</th><th class="r">Calls</th><th class="r">From cache</th><th class="r">Cost</th><th>Last</th></tr></thead><tbody>${H.llm_by_tag.map(u => `<tr>
      <td class="small mono">${esc(u.tag)}</td><td class="r num">${u.calls.toLocaleString()}</td><td class="r num mut">${u.cached.toLocaleString()}</td><td class="r num">${usd(u.usd,3)}</td><td class="small mut">${fmtT(u.last)}</td></tr>`).join("")}
      ${H.llm_by_tag.length ? `<tr><td><b>Total</b></td><td class="r num"><b>${H.llm_by_tag.reduce((a,u) => a + u.calls, 0).toLocaleString()}</b></td><td class="r num mut">${H.llm_by_tag.reduce((a,u) => a + u.cached, 0).toLocaleString()}</td><td class="r num"><b>${usd(tot,3)}</b></td><td></td></tr>` : ""}</tbody></table>
      ${H.llm_by_tag.length ? "" : none("No LLM calls yet.")}</div></div>`; }
  $("#v-activity").innerHTML = `<div class="toolbar" style="margin-bottom:16px">${tabs}<span class="grow"></span><button class="btn sm ghost" onclick="loadActivity()">${ic("refresh","sm")} Refresh</button></div>${body}`;
}

/* ---------------- configs (#/configs, #/configs/edit/<path>, #/configs/new-topic, #/configs/new-run/<topic path>) ---------------- */
const CF = {sub:"list", arg:"", shown:null, L:null, q:"", file:null, text:"", path:"", dirty:false, chk:null, created:null, nt:null, nr:null};
const KIND_LABEL = {run:"Run file", topic:"Topic", catalog:"Model catalog", other:"YAML"};
const KIND_SUB = {run:"A run = the folder's topic + the models it chooses + run settings. Its outputs go to the same path under runs/.",
  topic:"What to review: question, eligibility, records, extraction fields and risk-of-bias domains. Shared by every run file in this folder.",
  catalog:"Facts about each model (provider, price), written once. Run files choose models from it by name.", other:"A YAML file under configs/."};
const CF_HELP = {
  run:`<p style="margin-top:0"><code>include:</code> loads this folder's <code>topic.yaml</code>; keys written here override it, and nested sections merge key by key. Anything not written takes its default. Models are chosen by name (<code>llm_defaults.model</code>, <code>roles.judge.model</code>, <code>stage_models</code>); their provider and price come from <code>configs/models.yaml</code>.</p>
    <table class="sumt keys"><tbody>
    <tr><td><code>budget_usd</code></td><td>hard cost cap for the run folder (5.0)</td></tr>
    <tr><td><code>contact_email</code></td><td>sent to NCBI, Unpaywall, OpenAlex; Unpaywall needs it</td></tr>
    <tr><td><code>pipeline.human_mode</code></td><td><code>human_in_the_loop</code> or <code>agent_only</code></td></tr>
    <tr><td><code>pipeline.fulltext_unclear</code></td><td><code>await</code> · <code>include</code> · <code>exclude</code> (agent_only: not await)</td></tr>
    <tr><td><code>pipeline.max_records</code></td><td>benchmark only: screen a sample</td></tr>
    <tr><td><code>pipeline.citation_chasing</code></td><td>follow references and citations (true)</td></tr>
    <tr><td><code>feedback.auto_accept</code></td><td>apply proposals without confirmation (false)</td></tr>
    <tr><td><code>eval.budget_usd</code></td><td>benchmark only, per evaluation (8.0)</td></tr></tbody></table>
    <p class="hint" style="margin-bottom:0">Every key is listed in <code>configs/README.md</code>. API keys never go here; they live in <code>api_env.yaml</code>.</p>`,
  topic:`<table class="sumt keys"><tbody>
    <tr><td><code>key</code></td><td>short id</td></tr>
    <tr><td><code>question</code></td><td>the review question</td></tr>
    <tr><td><code>seed_eligibility</code></td><td>eligibility in plain words; the agent turns it into atomic criteria you approve</td></tr>
    <tr><td><code>screening_rules</code></td><td>exclusions before any LLM call: empty_record, conference_abstract, erratum, editorial, review, off_topic_no_abstract</td></tr>
    <tr><td><code>records.source</code></td><td><code>pubmed</code> (with <code>pubmed_retmax</code>, <code>maxdate</code>), <code>file</code> (a JSONL path) or <code>synergy</code> (benchmark)</td></tr>
    <tr><td><code>extraction_fields</code></td><td><code>{name, description, kind}</code>; kind <code>drug</code> or <code>disease</code> turns on term checks</td></tr>
    <tr><td><code>rob_tool_name</code>, <code>rob_domains</code></td><td>quality tool and its domains <code>{name, description}</code></td></tr></tbody></table>`,
  catalog:`<p style="margin-top:0">One entry per model under <code>models:</code>. Only facts about the model go here; choices (effort, <code>max_tokens</code>, <code>concurrency</code>, <code>api_env</code>) go in the run file.</p>
    <table class="sumt keys"><tbody>
    <tr><td><code>provider</code></td><td><code>openai</code> · <code>gemini</code> · <code>anthropic</code> · <code>local</code></td></tr>
    <tr><td><code>pricing</code></td><td><code>{input_per_m, output_per_m}</code> USD per 1M tokens; optional <code>long_context: {threshold_tokens, input_per_m, output_per_m}</code>. Used for the budget guard and every estimate</td></tr>
    <tr><td><code>base_url</code></td><td>optional API endpoint override</td></tr>
    <tr><td><code>note</code></td><td>free text</td></tr></tbody></table>
    <p class="hint" style="margin-bottom:0">The Run page and the New review form offer every model listed here. Removing a model that a run file still uses is refused.</p>`,
  other:`<p style="margin:0">This file is not a run, topic or catalog file, so only its YAML syntax is checked.</p>`};
const editHash = p => "#/configs/edit/" + encodeURIComponent(p);
function openConfig(p){
  if (CF.dirty && !confirm("Discard your unsaved changes to " + CF.path + "?")) return;
  CF.dirty = false;
  if (S && S.run.config_rel === p && CFG === p) return show("steps");
  location.href = "/?config=" + encodeURIComponent(p) + "#/steps";
}
const stTone = s => /done/.test(s||"") ? "ok" : /progress/.test(s||"") ? "info" : "";
const today = () => { const d = new Date(); return `${d.getFullYear()}/${String(d.getMonth()+1).padStart(2,"0")}/${String(d.getDate()).padStart(2,"0")}`; };
const loadingCard = t => `<div class="card"><div class="empty"><span class="spin" style="display:inline-block;margin-bottom:8px"></span><br>${t}</div></div>`;

function renderConfigs(){
  const key = CF.sub + "|" + CF.arg;
  if (CF.shown === key) return;
  CF.shown = key;
  const T = {edit:["Edit config", CF.arg], "new-topic":["New review","A topic folder with its topic.yaml and a first run file"], "new-run":["New run file","Another way to run an existing topic: other models, people mode or budget"]}[CF.sub];
  if (T) { $("#ptitle").textContent = T[0]; $("#psub").textContent = T[1]; }
  if (CF.sub === "edit") return openEditor(CF.arg);
  if (CF.sub === "new-topic" || CF.sub === "new-run") return renderNewForm();
  loadConfigs();
}
async function loadConfigs(){
  if (!CF.L) $("#v-configs").innerHTML = loadingCard("Reading configs…");
  CF.L = await api("/api/configs");
  if (tab === "configs" && CF.sub === "list") renderConfigList();
}
function renderConfigList(){
  const L = CF.L;
  if (!L || L.error) { $("#v-configs").innerHTML = `<div class="callout warn">${ic("warn")}<div>${esc((L && L.error) || "Could not read the configs.")}</div></div>`; return; }
  const q = CF.q.toLowerCase();
  const hit = (...xs) => !q || xs.join(" ").toLowerCase().includes(q);
  const runs = L.runs.filter(r => hit(r.path, r.question, r.agent, r.human_mode, r.status));
  const grp = r => r.outside ? "other" : r.template ? "template" : r.kind;
  const groups = [["other","Opened from outside configs/"],["review","Reviews"],["benchmark","Benchmarks (known answers)"],["template","Templates"]];
  const runRow = r => {
    const people = r.human_mode === "agent_only" ? badge("Agent-only","info") : r.human_mode ? badge("Human in the loop") : "";
    const status = r.error ? `<span title="${esc(r.error)}">${badge("does not load","bad","dot")}</span>` : r.status ? badge(r.status, stTone(r.status), "dot") : "";
    return `<tr class="${r.current ? "cur" : ""}" data-p="${esc(r.path)}" onclick="${r.error || r.template ? "go(editHash(this.dataset.p))" : "openConfig(this.dataset.p)"}" title="${r.error ? "Fix this run file" : r.template ? "A template: copy it with New review" : "Open this review"}">
      <td><div class="st-title">${r.topic ? `<span class="mut" style="font-weight:500">${esc(r.topic)} /</span> ` : ""}${esc(r.name)}
        ${r.current ? badge("open","info") : ""}${r.job ? " " + badge(r.job, "info", "dot") : ""}
        ${r.config_changed && r.config_changed.length ? `<span title="Changed after the start: ${esc(r.config_changed.join(", "))}">${badge("settings changed","warn","dot")}</span>` : ""}</div><div class="cfg-path">${esc(r.path)}</div>
        ${r.question ? `<div class="reason clamp2" style="max-width:80ch" title="${esc(r.question)}">${esc(r.question)}</div>` : ""}
        ${r.error ? `<div class="reason err">${esc(trunc(r.error, 200))}</div>` : ""}</td>
      <td class="hide-xs mono nowrap" title="${esc(r.agent||"")}">${esc((r.agent||"").split(":").slice(1).join(":") || r.agent || "")}</td><td class="hide-xs">${people}</td>
      <td class="nowrap">${status}${r.spent_usd ? `<div class="xs mut" style="margin-top:3px">${usd(r.spent_usd)} spent</div>` : ""}</td>
      <td class="r nowrap" onclick="event.stopPropagation()">
        ${r.error || r.template ? "" : `<button class="btn sm ${r.current ? "" : "primary"}" onclick="openConfig(this.closest('tr').dataset.p)">${r.current ? "Back" : "Open"}</button>`}
        ${r.outside ? "" : `<button class="btn sm" onclick="go(editHash(this.closest('tr').dataset.p))">${ic("edit","sm")} Edit</button>
        <button class="btn sm ghost" onclick="dupConfig(this.closest('tr').dataset.p)" title="Copy this run file in the same folder">${ic("copy","sm")}</button>`}</td></tr>`;
  };
  const body = groups.map(([g, label]) => { const rs = runs.filter(r => grp(r) === g); return rs.length ? `<tr class="tg"><td colspan="5">${label} · ${rs.length}</td></tr>` + rs.map(runRow).join("") : ""; }).join("");
  const topics = L.topics.filter(t => hit(t.path, t.question, t.source));
  const models = L.models.filter(m => hit(m.name, m.provider));
  const other = L.other.filter(o => hit(o.path));
  const editBtn = p => `<button class="btn sm" data-p="${esc(p)}" onclick="go(editHash(this.dataset.p))">${ic("edit","sm")} Edit</button>`;
  const C = CF.created;
  $("#v-configs").innerHTML = `
    ${NOCFG && NOCFG.changes ? changedPanel() : NOCFG && NOCFG.error ? `<div class="callout warn">${ic("warn")}<div class="grow"><b>${esc(NOCFG.config)}</b> does not load: ${esc(NOCFG.error)}
       ${L.runs.some(r => r.path === NOCFG.config) || /^configs\//.test(NOCFG.config) ? `<div style="margin-top:6px"><button class="btn sm" onclick="go(editHash(NOCFG.config))">${ic("edit","sm")} Fix it</button></div>` : ""}</div></div>` : ""}
    ${NOCFG && !NOCFG.error ? `<div class="callout info">${ic("info")}<div>No run file is open. Open one below, or set up a new review.</div></div>` : ""}
    ${C ? `<div class="callout info">${ic("check")}<div class="grow">Created ${C.files.map(f => `<code>${esc(f)}</code>`).join(" and ")}.
       ${C.check && !C.check.ok ? `<div class="err" style="margin-top:4px">Still to fix: ${esc(C.check.errors.join("; "))}</div>` : C.check && C.check.warnings.length ? `<div class="small" style="margin-top:4px;color:var(--warn)">${esc(C.check.warnings.join("; "))}</div>` : ""}
       <div class="row" style="margin-top:8px">${C.check && C.check.ok ? `<button class="btn sm primary" onclick="openConfig(CF.created.path)">${ic("play","sm")} Open the review</button>` : ""}
       ${C.files.map(f => `<button class="btn sm" data-p="${esc(f)}" onclick="go(editHash(this.dataset.p))">${ic("edit","sm")} Edit ${esc(f.split("/").pop())}</button>`).join("")}
       <button class="btn sm ghost" onclick="CF.created=null;renderConfigList()">Dismiss</button></div></div></div>` : ""}
    <div class="toolbar" style="margin-bottom:16px"><div class="search">${ic("search")}<input id="cfgq" placeholder="Filter by path, question, model or status" value="${esc(CF.q)}" oninput="CF.q=this.value;renderConfigList();const i=$('#cfgq');i.focus();i.setSelectionRange(i.value.length,i.value.length)"></div>
      <button class="btn" onclick="go('#/configs/new-run')">New run file</button>
      <button class="btn primary" onclick="go('#/configs/new-topic')">${ic("edit","sm")} New review</button></div>
    <div class="card"><div class="card-h"><div class="grow"><h2>Run files</h2><div class="sub">Click a row to open that review here. <code>configs/&lt;review|benchmark&gt;/&lt;topic&gt;/&lt;run&gt;.yaml</code> writes to <code>runs/</code> under the same path.</div></div></div>
      <div class="card-b flush tw"><table class="hover"><thead><tr><th style="width:44%">Run file</th><th class="hide-xs">Agent</th><th class="hide-xs">People</th><th>Status</th><th></th></tr></thead>
      <tbody>${body || `<tr><td colspan="5" class="empty">${q ? "No run file matches." : "No run files yet. Start with New review."}</td></tr>`}</tbody></table></div></div>
    <div class="card"><div class="card-h"><div class="grow"><h2>Topics</h2><div class="sub">One folder per topic; its <code>topic.yaml</code> is shared by the run files next to it.</div></div></div>
      <div class="card-b flush tw"><table><thead><tr><th>Topic</th><th class="hide-sm">Question</th><th>Records</th><th></th></tr></thead><tbody>
      ${topics.map(t => `<tr><td><div class="st-title">${esc(t.topic)} ${t.template ? badge("template") : ""}</div><div class="cfg-path">${esc(t.path)}</div></td>
        <td class="hide-sm"><div class="clamp2 small" style="max-width:56ch">${esc(t.question)}</div></td><td>${t.source ? badge(t.source, t.source === "synergy" ? "info" : "") : ""}</td>
        <td class="r nowrap">${editBtn(t.path)}${t.template ? "" : ` <button class="btn sm ghost" data-p="${esc(t.path)}" onclick="go('#/configs/new-run/'+encodeURIComponent(this.dataset.p))">+ Run file</button>`}</td></tr>`).join("")
        || `<tr><td colspan="4" class="empty">No topics match.</td></tr>`}</tbody></table></div></div>
    <div class="card"><div class="card-h"><div class="grow"><h2>Model catalog</h2><div class="sub"><code>${esc(L.catalog)}</code>: the provider and price of each model, written once. Run files choose models from it by name.</div></div>${editBtn(L.catalog)}</div>
      ${L.catalog_error ? `<div class="card-b"><span class="err">${esc(L.catalog_error)}</span></div>` : `<div class="card-b flush tw"><table><thead><tr><th>Model</th><th>Provider</th><th class="r">Input / 1M</th><th class="r">Output / 1M</th></tr></thead><tbody>
      ${models.map(m => `<tr><td class="mono" style="font-weight:600">${esc(m.name)}</td><td>${esc(m.provider)}</td><td class="r num">${usd(m.input_per_m)}</td>
        <td class="r num">${usd(m.output_per_m)}${m.long_context ? `<div class="xs mut">more above the long-context threshold</div>` : ""}</td></tr>`).join("") || `<tr><td colspan="4" class="empty">No models match.</td></tr>`}</tbody></table></div>`}</div>
    ${other.length ? `<div class="card"><div class="card-h"><h2>Other YAML files</h2></div><div class="card-b flush tw"><table><tbody>
      ${other.map(o => `<tr><td class="mono">${esc(o.path)}</td><td class="r">${editBtn(o.path)}</td></tr>`).join("")}</tbody></table></div></div>` : ""}`;
}
async function dupConfig(p){
  const base = p.split("/").pop().replace(/\.ya?ml$/, "");
  const name = prompt("Name of the copy (same folder, without .yaml):", base + "_copy");
  if (!name) return;
  const r = await api("/api/config_duplicate", {path:p, name});
  if (!r.ok) return toast(r.message || r.error || "Failed", true);
  toast("Created " + r.path); CF.L = null; go(editHash(r.path));
}

/* editor */
async function openEditor(path){
  CF.path = path; CF.dirty = false; CF.chk = null; CF.file = null;
  $("#v-configs").innerHTML = loadingCard("Opening " + esc(path) + "…");
  const f = await api("/api/config_file?path=" + encodeURIComponent(path));
  if (CF.path !== path) return;
  if (!f || f.error || f.text == null) {
    $("#v-configs").innerHTML = `<div class="crumbs"><a href="#/configs" onclick="event.preventDefault();go('#/configs')">Configs</a></div>
      <div class="callout warn">${ic("warn")}<div>${esc((f && (f.error || f.message)) || "Could not open the file.")}</div></div>`; return;
  }
  CF.file = f; CF.text = f.text;
  renderEditor(); checkConfig();
}
function renderEditor(){
  const f = CF.file, isRun = f.kind === "run";
  const isCur = isRun && S && S.run.config_rel === f.path;
  $("#v-configs").innerHTML = `
    <div class="crumbs"><a href="#/configs" onclick="event.preventDefault();go('#/configs')">Configs</a><span class="sep">/</span><span class="mono">${esc(f.path)}</span></div>
    <div class="edgrid"><div>
      <div class="card"><div class="card-h"><div class="grow"><h2 style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">${esc(f.path.split("/").pop())} ${badge(KIND_LABEL[f.kind] || "YAML", f.kind === "run" ? "info" : "")}<span id="ed-dirty" class="row" style="gap:5px"></span></h2>
        <div class="sub">${KIND_SUB[f.kind] || ""}</div></div></div>
        <textarea class="code" id="ed" spellcheck="false" autocapitalize="off" autocomplete="off" aria-label="YAML of ${esc(f.path)}"></textarea>
        <div class="card-f"><button class="btn primary" id="ed-save" onclick="saveConfig()">${ic("check","sm")} Save</button>
          <button class="btn" onclick="checkConfig()">Check</button><button class="btn ghost" onclick="revertConfig()">Revert</button>
          ${isRun ? `<button class="btn" onclick="openConfig(CF.path)">${ic("play","sm")} ${isCur ? "Back to this review" : "Open this review"}</button>` : ""}
          <span class="hint grow nowrap hide-sm">Ctrl+S saves · Tab indents</span><span id="ed-msg" class="msg"></span></div></div>
    </div><aside>
      <div class="card"><div class="card-h"><h2>Check</h2><span id="chk-badge"></span></div><div class="card-b" id="chk"><span class="mut small">Checking…</span></div></div>
      <div class="card"><div class="card-h"><h2>Keys</h2></div><div class="card-b small">${CF_HELP[f.kind] || CF_HELP.other}</div></div>
    </aside></div>`;
  const ta = $("#ed"); ta.value = CF.text;
  ta.addEventListener("input", () => { CF.text = ta.value; setDirty(CF.text !== CF.file.text); scheduleCheck(); });
  ta.addEventListener("keydown", edKey);
}
function setDirty(v){
  CF.dirty = v;
  const el = $("#ed-dirty"); if (el) el.innerHTML = v ? `<span class="dirty"></span><span class="small" style="font-weight:500;color:var(--warn)">unsaved</span>` : "";
}
function insertText(ta, t){
  ta.focus();
  if (!document.execCommand || !document.execCommand("insertText", false, t)) { ta.setRangeText(t, ta.selectionStart, ta.selectionEnd, "end"); ta.dispatchEvent(new Event("input")); }
}
function edKey(e){
  const ta = e.target;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); return saveConfig(); }
  if (e.key === "Tab" && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); insertText(ta, "  "); }
  if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {   // keep the indentation (+2 after "key:")
    const before = ta.value.slice(0, ta.selectionStart), line = before.slice(before.lastIndexOf("\n") + 1);
    let ind = (line.match(/^\s*/) || [""])[0];
    if (/:\s*$/.test(line.split(" #")[0])) ind += "  ";
    if (/^\s*- /.test(line) && !/:\s*$/.test(line)) ind = ind + "";
    e.preventDefault(); insertText(ta, "\n" + ind);
  }
}
let chkTimer = null;
function scheduleCheck(){ clearTimeout(chkTimer); chkTimer = setTimeout(checkConfig, 700); }
async function checkConfig(){
  clearTimeout(chkTimer);
  const path = CF.path, text = CF.text;
  const r = await api("/api/config_check", {path, text});
  if (CF.path !== path || CF.text !== text) return;
  CF.chk = r; renderCheck();
}
function gotoLine(n){
  const ta = $("#ed"); if (!ta) return;
  const lines = ta.value.split("\n"); let pos = 0;
  for (let i = 0; i < n - 1 && i < lines.length; i++) pos += lines[i].length + 1;
  ta.focus(); ta.setSelectionRange(pos, pos + (lines[n-1] || "").length);
  ta.scrollTop = Math.max(0, (n - 5) * 20);
}
function renderCheck(){
  const r = CF.chk, el = $("#chk"); if (!el || !r) return;
  if (r.error) { el.innerHTML = `<span class="err">${esc(r.error)}</span>`; return; }
  const nl = (r.locked || []).length;
  $("#chk-badge").innerHTML = !r.ok ? badge(r.errors.length + " problem" + (r.errors.length > 1 ? "s" : ""), "bad", "dot")
    : nl ? badge("pauses " + nl + " started run" + (nl > 1 ? "s" : ""), "warn", "dot")
    : r.warnings.length ? badge("loads, " + r.warnings.length + " note" + (r.warnings.length > 1 ? "s" : ""), "warn", "dot") : badge("loads", "ok", "dot");
  const sm = r.summary;
  el.innerHTML = `
    ${r.errors.length ? `<ul class="issues e">${r.errors.map(e => `<li>${esc(e)}</li>`).join("")}</ul>${r.line ? `<button class="btn sm" style="margin-top:8px" onclick="gotoLine(${r.line})">Go to line ${r.line}</button>` : ""}` : ""}
    ${r.warnings.length ? `<ul class="issues w" ${r.errors.length ? 'style="margin-top:10px"' : ""}>${r.warnings.map(e => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
    ${lockedHtml(r.locked)}
    ${!r.errors.length && !r.warnings.length && !(r.locked||[]).length ? `<div class="saved">${ic("check","sm")} ${r.kind === "run" ? "Loads with its includes, as sragent run would." : "Looks right."}</div>` : ""}
    ${sm ? `<table class="sumt" style="margin-top:12px"><tbody>
      <tr><td>Outputs</td><td class="mono">${esc(sm.run_dir)}</td></tr>
      <tr><td>Status</td><td>${badge(sm.status, stTone(sm.status), "dot")}</td></tr>
      <tr><td>Agent</td><td class="mono">${esc(sm.agent)}</td></tr>
      ${sm.benchmark ? `<tr><td>Judge</td><td class="mono">${esc(sm.judge)}</td></tr>` : ""}
      <tr><td>People</td><td>${esc(human(sm.human_mode))}</td></tr>
      <tr><td>Records</td><td>${esc(sm.records)}${sm.benchmark ? " (benchmark)" : ""}${sm.max_records ? ` · sample of ${sm.max_records}` : ""}</td></tr>
      <tr><td>Budget</td><td>${usd(sm.budget_usd)}</td></tr>
      <tr><td>Question</td><td class="small">${esc(trunc(sm.question, 220))}</td></tr></tbody></table>` : ""}
    ${r.resolved ? `<details style="margin-top:12px"><summary>Resolved configuration (with includes and defaults)</summary><pre class="log" style="margin-top:8px;max-height:420px">${esc(r.resolved)}</pre></details>` : ""}`;
}
function changesTable(ch){
  return `<div class="tw" style="border:1px solid var(--line);border-radius:10px;margin:8px 0;background:var(--surface)"><table><thead><tr><th>Setting</th><th>Started with</th><th>Config now</th></tr></thead><tbody>
    ${ch.map(c => `<tr><td class="mono nowrap">${esc(c.key)}</td><td class="small">${esc(trunc(typeof c.old === "string" ? c.old : JSON.stringify(c.old), 300))}</td>
      <td class="small">${esc(trunc(typeof c.new === "string" ? c.new : JSON.stringify(c.new), 300))}</td></tr>`).join("")}</tbody></table></div>`;
}
function changesList(ch){   // narrow places: one block per setting
  const v = x => esc(trunc(typeof x === "string" ? x : JSON.stringify(x), 260));
  return `<div style="margin-top:8px">${ch.map(c => `<div style="border-top:1px solid var(--warn-line);padding:7px 0">
    <div class="mono" style="font-weight:600">${esc(c.key)}</div><div class="small"><span class="mut">was</span> ${v(c.old)}</div><div class="small"><span class="mut">now</span> ${v(c.new)}</div></div>`).join("")}</div>`;
}
function lockedHtml(L){
  if (!L || !L.length) return "";
  return L.map(x => `<div class="callout warn" style="margin:10px 0 0">${ic("warn")}<div class="grow" style="min-width:0"><b>${esc(x.run.split("/").pop())}</b> has started.
    Saving this changes settings that are fixed for it, so that review pauses until you accept the change (it is recorded and shown in the report) or set them back.${changesList(x.changes)}</div></div>`).join("");
}
function changedPanel(){
  const N = NOCFG, inConfigs = /^configs\//.test(N.config || "");
  const topicFile = N.config.replace(/[^/]+$/, "topic.yaml");
  const files = [...(N.changes.some(c => c.key.startsWith("topic.")) ? [topicFile] : []), ...(N.changes.some(c => !c.key.startsWith("topic.")) ? [N.config] : [])];
  return `<div class="callout warn" style="display:block">
    <div class="row" style="gap:8px">${ic("warn")}<b>${esc(N.config)}</b> is paused: settings that define this review were changed after it started.</div>
    <p class="small" style="margin:8px 0 0">Nothing runs and nothing is spent until you choose. Outputs so far followed the settings it started with.</p>
    ${changesTable(N.changes)}
    <div class="optcards" style="margin-top:12px">
      <div class="optcard" style="cursor:default;flex-direction:column"><b>Set them back</b><span class="d">Edit the file back to the "started with" values; the review continues as it was.</span>
        ${inConfigs ? `<div class="row" style="margin-top:8px;gap:6px">${files.map(f => `<button class="btn sm" data-p="${esc(f)}" onclick="go(editHash(this.dataset.p))">${ic("edit","sm")} Edit ${esc(f.split("/").pop())}</button>`).join("")}</div>` : ""}</div>
      <div class="optcard" style="cursor:default;flex-direction:column"><b>Start a new run file</b><span class="d">A copy with the new settings starts a fresh review in its own runs/ folder. Set this file back to continue the original too.</span>
        ${inConfigs ? `<button class="btn sm" style="margin-top:8px" onclick="dupConfig(NOCFG.config)">${ic("copy","sm")} Copy as a new run file</button>` : ""}</div>
      <div class="optcard" style="cursor:default;flex-direction:column"><b>Accept the change</b><span class="d">Continue this review with the new settings. The change and your reason are recorded in provenance.jsonl, the report and every evaluation of this run.</span>
        <textarea id="acc-reason" rows="2" style="margin-top:8px" placeholder="Why the settings changed (required)"></textarea>
        <div class="row" style="margin-top:6px"><button class="btn sm primary" onclick="acceptConfig(this)">Accept and continue</button><span id="acc-msg" class="msg"></span></div></div>
    </div></div>`;
}
async function acceptConfig(btn){
  const reason = ($("#acc-reason").value || "").trim();
  if (!reason) return msg("acc-msg", {ok:false, message:"Write a reason first"});
  btn.disabled = true;
  const r = await fetch("/api/config_accept?config=" + encodeURIComponent(NOCFG.config), {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({reason})})
    .then(x => x.json()).catch(e => ({ok:false, error:String(e)}));
  btn.disabled = false;
  if (!r.ok) return msg("acc-msg", r);
  toast(r.message || "Accepted");
  location.href = "/?config=" + encodeURIComponent(NOCFG.config) + "#/steps";
}
async function saveConfig(force){
  if (!CF.file) return;
  const text = CF.text, btn = $("#ed-save"); if (btn) btn.disabled = true;
  const r = await api("/api/config_save", {path:CF.path, text, mtime: force ? null : CF.file.mtime});
  if (btn) btn.disabled = false;
  const m = $("#ed-msg");
  if (r.conflict) {
    if (m) m.innerHTML = `<span class="err">${esc(r.message)}</span> <button class="btn sm danger" onclick="saveConfig(true)">Overwrite</button> <button class="btn sm" onclick="CF.dirty=false;openEditor(CF.path)">Reload</button>`;
    return;
  }
  if (!r.ok) { if (m) msg(m, r); if (r.errors) { CF.chk = r; renderCheck(); } return; }
  CF.file.text = text; CF.file.mtime = r.mtime; setDirty(CF.text !== text);
  CF.chk = r; renderCheck(); CF.L = null;
  const lk = (r.locked || [])[0];
  if (m) m.innerHTML = lk ? `<span class="err">Saved. ${esc(lk.run.split("/").pop())} is paused until you accept or revert the change</span>
      <button class="btn sm" data-p="${esc(lk.run)}" onclick="openConfig(this.dataset.p)">Accept or revert…</button>`
    : r.errors && r.errors.length ? `<span class="err">Saved, with problems to fix</span>` : `<span class="saved">${ic("check","sm")} Saved</span>`;
  toast("Saved " + CF.path + (S ? "; the open review uses it from now on" : ""));
  if (S) refresh(true);
}
function revertConfig(){
  if (!CF.file) return;
  CF.text = CF.file.text; const ta = $("#ed"); if (ta) ta.value = CF.text;
  setDirty(false); checkConfig();
}

/* new review / new run file */
const NT_FIELDS = `study_design: Design, setting, sample size, comparator and follow-up.
population: Who was studied (age, condition, severity).
intervention [drug]: Drug or intervention, dose and regimen.
comparator [drug]: Comparator or control.
outcomes: Main results with numbers, units, timepoints and uncertainty.`;
const NT_ROB = `randomization: Random sequence generation and allocation concealment.
deviations: Deviations from the intended interventions.
missing_data: Missing outcome data.
measurement: Measurement of the outcome.
selective_reporting: Selection of the reported result.`;
function catDefault(){ const m = ((CF.L && CF.L.catalog_models) || []).map(x => x.name); return m.includes("gemini-3.8-flash") ? "gemini-3.8-flash" : m[0] || ""; }
function catJudge(agent){   // a judge from another provider when there is one
  const C = (CF.L && CF.L.catalog_models) || [], a = C.find(x => x.name === agent);
  return (C.find(x => a && x.provider !== a.provider && x.input_per_m >= 1) || C.find(x => x.name !== agent && x.input_per_m >= 1) || C[0] || {}).name || "";
}
function catOptions(sel, withLocal){
  return ((CF.L && CF.L.catalog_models) || []).map(m => `<option value="${esc(m.name)}" ${sel===m.name?"selected":""}>${esc(m.name)} · ${esc(m.provider)} · $${m.input_per_m} / $${m.output_per_m}</option>`).join("")
    + (withLocal ? `<option value="local" ${sel==="local"?"selected":""}>your vLLM servers (local) · free</option>` : "");
}
async function renderNewForm(){
  if (!CF.L) { $("#v-configs").innerHTML = loadingCard("Reading configs…"); CF.L = await api("/api/configs"); }
  if (tab !== "configs" || !["new-topic","new-run"].includes(CF.sub)) return;
  if (CF.sub === "new-topic") {
    if (!CF.nt) CF.nt = {key:"", question:"", seed_eligibility:"", source:"pubmed", maxdate:today(), pubmed_retmax:1000, records_file:"",
      extraction_fields:NT_FIELDS, rob_tool_name:"RoB 2", rob_domains:NT_ROB, agent_model:catDefault(), agent_effort:"low", human_mode:"human_in_the_loop",
      fulltext_unclear:"include", budget_usd:10, contact_email:"", run_name:"run"};
  } else {
    const topics = CF.L.topics.filter(t => !t.template);
    if (!CF.nr || (CF.arg && CF.nr.topic_path !== CF.arg)) CF.nr = {topic_path: CF.arg || (topics[0] || {}).path || "", name:"", named:false, agent_model:catDefault(), agent_effort:"low", judge_model:catJudge(catDefault()),
      human_mode:"human_in_the_loop", fulltext_unclear:"include", budget_usd:10, max_records:"", contact_email:""};
  }
  drawNewForm();
}
function nf(){ return CF.sub === "new-topic" ? CF.nt : CF.nr; }
function setNf(k, v, redraw){ nf()[k] = v; if (CF.sub === "new-run" && k !== "name" && !CF.nr.named) CF.nr.name = ""; if (redraw) drawNewForm(); else nfPreview(); }
function nrName(){
  const r = CF.nr; if (r.named && r.name) return r.name;
  return (r.agent_model || "model").replace(/[^A-Za-z0-9_.-]+/g, "_") + "_" + (r.human_mode === "agent_only" ? "agentonly" : "humanloop") + (r.max_records ? "_n" + r.max_records : "");
}
function nfPreview(){
  const el = $("#nf-path"); if (!el) return;
  if (CF.sub === "new-topic") { const k = CF.nt.key || "<topic>"; el.innerHTML = `Writes <code>configs/review/${esc(k)}/topic.yaml</code> and <code>configs/review/${esc(k)}/${esc(CF.nt.run_name || "run")}.yaml</code>; outputs go to <code>runs/review/${esc(k)}/${esc(CF.nt.run_name || "run")}/</code>.`; }
  else { const folder = CF.nr.topic_path.replace(/\/topic\.yaml$/, ""); const n = nrName(); el.innerHTML = folder ? `Writes <code>${esc(folder)}/${esc(n)}.yaml</code>; outputs go to <code>${esc(folder.replace(/^configs\//, "runs/"))}/${esc(n)}/</code>.` : ""; const ni = $("#nr-name"); if (ni && !CF.nr.named) ni.placeholder = n; }
}
function peopleFields(o){
  const ao = o.human_mode === "agent_only";
  return `<label>People</label><div><div class="seg">${[["human_in_the_loop","Human in the loop"],["agent_only","Agent-only"]].map(([v,l]) => `<button class="${o.human_mode===v?"on":""}" onclick="setNf('human_mode','${v}',true)">${l}</button>`).join("")}</div>
      <div class="hint">${ao ? "The agent makes every judgment; people may only supply the full texts it asks for." : "You approve the protocol, decide unclear records, and confirm or correct the agent's work."}</div></div>
    ${ao ? `<label>Still unclear after the full text</label><div><div class="seg">${[["include","Include (flagged)"],["exclude","Exclude"]].map(([v,l]) => `<button class="${o.fulltext_unclear===v?"on":""}" onclick="setNf('fulltext_unclear','${v}',true)">${l}</button>`).join("")}</div>
      <div class="hint">No person decides in agent-only mode, so these records need a rule.</div></div>` : ""}
    <label>Agent model</label><div><div class="row"><select onchange="setNf('agent_model',this.value,true)">${catOptions(o.agent_model, true)}</select>
      ${o.agent_model === "local" ? "" : `<select onchange="setNf('agent_effort',this.value)">${["low","medium","high"].concat(((CF.L.catalog_models||[]).find(m => m.name === o.agent_model) || {}).provider === "anthropic" ? ["xhigh","max"] : []).map(x => `<option ${o.agent_effort===x?"selected":""}>${x}</option>`).join("")}<option value="null" ${o.agent_effort==="null"?"selected":""}>model default</option></select>`}</div>
      <div class="hint">From the model catalog (<code>${esc(CF.L.catalog)}</code>). Give single stages another model or effort later on the Run page (Models per stage).</div></div>
    <label>Budget</label><div><div class="field" style="max-width:180px"><span class="mut">$</span><input type="number" min="0" step="1" style="flex:0 1 120px" value="${esc(o.budget_usd)}" oninput="setNf('budget_usd',this.value)"></div>
      <div class="hint">Hard cap for the whole run folder; the run stops cleanly when it is reached.</div></div>
    <label>Contact email</label><div><input type="email" style="width:100%;max-width:360px" placeholder="you@example.org" value="${esc(o.contact_email)}" oninput="setNf('contact_email',this.value)">
      <div class="hint">Sent to NCBI, Unpaywall and OpenAlex. Unpaywall needs it to find open-access full texts.</div></div>`;
}
function drawNewForm(){
  const back = `<div class="crumbs"><a href="#/configs" onclick="event.preventDefault();go('#/configs')">Configs</a><span class="sep">/</span><span>${CF.sub === "new-topic" ? "New review" : "New run file"}</span></div>`;
  let body;
  if (CF.sub === "new-topic") {
    const o = CF.nt;
    body = `<div class="card"><div class="card-h"><div class="grow"><h2>What to review</h2><div class="sub">You can change all of this later in the topic file; the protocol the agent drafts from it is approved by you before screening.</div></div></div><div class="card-b"><div class="form">
      <label>Topic folder</label><div><input style="width:100%;max-width:360px" placeholder="e.g. statins_dementia" value="${esc(o.key)}" oninput="this.value=this.value.replace(/[^A-Za-z0-9_-]/g,'_');setNf('key',this.value)">
        <div class="hint">Short id: letters, digits and _ only.</div></div>
      <label>Review question</label><div><textarea rows="3" placeholder="What is the effect of X on Y in Z?" oninput="setNf('question',this.value)">${esc(o.question)}</textarea></div>
      <label>Eligibility criteria</label><div><textarea rows="6" placeholder="Include … Exclude …" oninput="setNf('seed_eligibility',this.value)">${esc(o.seed_eligibility)}</textarea>
        <div class="hint">Plain words. The agent turns this into atomic inclusion and exclusion criteria.</div></div>
      <label>Records</label><div><div class="seg">${[["pubmed","PubMed search"],["file","My own file (JSONL)"]].map(([v,l]) => `<button class="${o.source===v?"on":""}" onclick="setNf('source','${v}',true)">${l}</button>`).join("")}</div>
        <div class="row" style="margin-top:10px">${o.source === "pubmed"
          ? `<label class="small mut">Search end date <input style="width:130px" value="${esc(o.maxdate)}" oninput="setNf('maxdate',this.value)" placeholder="YYYY/MM/DD"></label>
             <label class="small mut">Most records <input type="number" min="1" style="width:110px" value="${esc(o.pubmed_retmax)}" oninput="setNf('pubmed_retmax',this.value)"></label>`
          : `<input style="flex:1;min-width:240px" placeholder="data/my_records.jsonl (from the project root)" value="${esc(o.records_file)}" oninput="setNf('records_file',this.value)">`}</div>
        <div class="hint">${o.source === "pubmed" ? "The agent writes the PubMed query from the question and criteria." : "One JSON record per line (pmid, title, abstract, …)."}</div></div>
    </div></div></div>
    <div class="card"><div class="card-h"><div class="grow"><h2>What to extract and assess</h2><div class="sub">One per line, <code>name: description</code>. Add <code>[drug]</code> or <code>[disease]</code> after a name to check its terms in RxNorm, PubChem, ChEMBL or MONDO.</div></div></div><div class="card-b"><div class="form">
      <label>Extraction fields</label><div><textarea class="mono" rows="6" oninput="setNf('extraction_fields',this.value)">${esc(o.extraction_fields)}</textarea></div>
      <label>Risk-of-bias tool</label><div><input style="width:100%;max-width:360px" value="${esc(o.rob_tool_name)}" oninput="setNf('rob_tool_name',this.value)"></div>
      <label>Its domains</label><div><textarea class="mono" rows="5" oninput="setNf('rob_domains',this.value)">${esc(o.rob_domains)}</textarea></div>
    </div></div></div>
    <div class="card"><div class="card-h"><div class="grow"><h2>How to run it</h2><div class="sub">The first run file of this topic. Add more later (other models or people mode) with New run file.</div></div></div><div class="card-b"><div class="form">
      ${peopleFields(o)}
      <label>Run file name</label><div><input style="width:200px" value="${esc(o.run_name)}" oninput="this.value=this.value.replace(/[^A-Za-z0-9_.-]/g,'_');setNf('run_name',this.value)"></div>
    </div></div>
    <div class="card-f"><button class="btn primary lg" onclick="createConfig(this)">${ic("check","sm")} Create review</button><span id="nf-path" class="hint grow"></span><span id="nf-msg" class="msg"></span></div></div>`;
  } else {
    const o = CF.nr, topics = CF.L.topics.filter(t => !t.template);
    const t = topics.find(x => x.path === o.topic_path), bench = t && t.kind === "benchmark";
    body = `<div class="card"><div class="card-h"><div class="grow"><h2>Run file</h2><div class="sub">Same topic, another way to run it. Each run file has its own outputs, cost and history.</div></div></div><div class="card-b"><div class="form">
      <label>Topic</label><div><select style="width:100%;max-width:520px" onchange="setNf('topic_path',this.value,true)">${topics.map(x => `<option value="${esc(x.path)}" ${x.path===o.topic_path?"selected":""}>${esc(x.kind)} / ${esc(x.topic)}</option>`).join("")}</select>
        ${t ? `<div class="hint">${esc(trunc(t.question, 240))}</div>` : `<div class="hint">No topics yet: start with New review.</div>`}</div>
      ${peopleFields(o)}
      ${bench ? `<label>Judge model</label><div><select onchange="setNf('judge_model',this.value)">${catOptions(o.judge_model, false)}</select>
        <div class="hint">For <code>sragent eval</code>. Prefer a model from another family than the agent.</div></div>` : ""}
      ${bench ? `<label>Records to screen</label><div><input type="number" min="1" placeholder="all" style="width:120px" value="${esc(o.max_records)}" oninput="setNf('max_records',this.value)">
        <div class="hint">Benchmark only: a sample that always keeps every known answer. Blank = all.</div></div>` : ""}
      <label>Name</label><div><input id="nr-name" style="width:100%;max-width:420px" value="${esc(o.named ? o.name : "")}" placeholder="${esc(nrName())}" oninput="this.value=this.value.replace(/[^A-Za-z0-9_.-]/g,'_');CF.nr.named=!!this.value;setNf('name',this.value)">
        <div class="hint">${bench ? "Benchmark runs are named <code>&lt;models&gt;_&lt;characteristic&gt;</code>." : "Leave blank for the suggested name."}</div></div>
    </div></div>
    <div class="card-f"><button class="btn primary lg" onclick="createConfig(this)" ${t ? "" : "disabled"}>${ic("check","sm")} Create run file</button><span id="nf-path" class="hint grow"></span><span id="nf-msg" class="msg"></span></div></div>`;
  }
  $("#v-configs").innerHTML = back + body;
  nfPreview();
}
async function createConfig(btn){
  const what = CF.sub === "new-topic" ? "topic" : "run";
  const body = what === "topic" ? {what, ...CF.nt} : {what, ...CF.nr, name: nrName()};
  btn.disabled = true;
  const r = await api("/api/config_new", body);
  btn.disabled = false;
  if (!r.ok) { msg("nf-msg", r); return; }
  toast("Created " + r.files.join(", "));
  CF.created = r; CF.L = null;
  if (what === "topic") CF.nt = null; else CF.nr = null;
  go("#/configs");
}
window.addEventListener("beforeunload", e => { if (CF.dirty) { e.preventDefault(); e.returnValue = ""; } });

/* ---------------- start ---------------- */
document.addEventListener("keydown", e => {
  if (e.key === "/" && tab === "studies" && !ST.rid && !["INPUT","TEXTAREA","SELECT"].includes((document.activeElement||{}).tagName)) { e.preventDefault(); const q = $("#studyq"); if (q) q.focus(); }
  if (e.key === "Escape") toggleSide(false);
});
// start once every module (ops, stage, needs, settings) is loaded: a reload on a stage page needs stage.js
document.addEventListener("DOMContentLoaded", () => {
  applyTheme();
  route();
  refresh(true);
  setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 3000);
});
