/* One page per stage (docs/refactor/04-refactor-plan.md §5.1): the same template for all ten, from GET /api/stage.
   Header (status, models, spent) · the five-button toolbar · decisions waiting for you · inputs and dependents ·
   the items table (every output of the stage, with what a person can do to it) · this stage's jobs.
   Every change goes through runOp() (ops.js): previewed first when it reaches other outputs. */
const STG = {key: null, d: null, filter: "all", q: "", loading: false};

async function openStage(key, quiet){
  STG.key = key;
  if (!quiet) { STG.filter = "all"; STG.q = ""; STG.d = null; renderStage(); }
  STG.loading = true;
  const d = await api("/api/stage?key=" + encodeURIComponent(key));
  STG.loading = false;
  if (STG.key !== key) return;
  STG.d = d;
  renderStage();
}

const STAGE_KINDS = {   // the To-do cards that belong to each stage page (their forms are reused here)
  protocol: ["protocol"], search: ["search"], screen: [], retrieve: ["fulltexts"], eligibility: ["eligibility"],
  snowball: [], extract: ["fields"], rob: ["rob"], synthesis: ["sync"], report: []};

function stageDecisions(key){
  if (!S || !S.todos) return "";
  const T = S.todos.filter(t => !t.snoozed);
  const k = kind => T.filter(t => t.kind === kind);
  const parts = [];
  for (const kind of STAGE_KINDS[key] || []) {
    const items = k(kind);
    if (!items.length) continue;
    if (kind === "protocol") parts.push(protocolCard(items[0]));
    else if (kind === "search") parts.push(items[0].ran ? searchRanCard(items[0]) : searchCard(items[0]));
    else if (kind === "fulltexts") parts.push(pdfPanel(items[0], true));
    else if (kind === "eligibility") parts.push(eligTable(items));
    else if (kind === "fields") parts.push(`<details data-k="fields"><summary class="small mut" style="cursor:pointer;margin:4px 0 10px">Data items and quality domains · ${items[0].fields.length} items, ${items[0].domains.length} domains · open to change them</summary>${fieldsCard(items[0])}</details>`);
    else if (kind === "rob") { parts.push(robTable(items.filter(t => !t.optional))); parts.push(robTable(items.filter(t => t.optional), true)); }
    else if (kind === "sync") parts.push(`<div class="card"><div class="card-b"><div class="q" style="font-weight:600">${esc(items[0].title)}</div>
        <ul class="ctx small">${items[0].context.map(c => `<li>${esc(c)}</li>`).join("")}</ul></div>
        <div class="card-f"><button class="btn primary" onclick="runOp('update_all', {}, 'Update every out-of-date output')">${ic("refresh","sm")} Update now</button></div></div>`);
  }
  // flags, edits and answers to feedback whose output belongs to this stage
  T.filter(t => ["flag", "manual_edit"].includes(t.kind) && t.step === key).forEach(t => parts.push(card(t)));
  if (key === "screen" && (S.ruled || []).length) parts.push(ruledList());
  return parts.join("");
}

function stageToolbar(d){
  return `<div class="row" style="gap:8px;flex-wrap:wrap">${d.toolbar.map(b => {
    const dis = !b.enabled ? `disabled title="${esc(b.reason || "not available")}"` : (b.reason ? `title="${esc(b.reason)}"` : "");
    const cls = b.key === "run" ? "btn primary" : b.key === "stop" ? "btn danger" : "btn";
    const icon = {run: "play", rerun_all: "refresh", clear: "x", verify: "check", stop: "stop"}[b.key];
    const act = b.key === "stop" ? "stopRun()" : `stageButton('${b.key}')`;
    return `<button class="${cls}" ${dis} onclick="${act}">${ic(icon, "sm")} ${esc(b.label)}</button>`;
  }).join("")}<span class="grow"></span>
    ${d.pending && d.pending.items ? `<span class="small mut">${d.pending.items} pending${d.pending.est_usd ? ` · about ${usd(d.pending.est_usd)}` : ""}</span>` : ""}</div>`;
}

async function stageButton(key){
  const b = (STG.d.toolbar || []).find(x => x.key === key);
  if (!b || !b.op) return;
  const label = `${STG.d.label}: ${b.label.replace(/\s*\(\d+\)$/, "")}`;
  await runOp(b.op, b.args || {}, label);
}

async function rowAction(i, j){
  const row = STG.view[i], a = row.actions[j];
  const args = {...(a.args || {})};
  if (a.needs) {
    const v = prompt(`${a.label} (${row.rid || row.id}): ${a.needs}`);
    if (!v) return;
    args[a.needs] = v;
  }
  await runOp(a.op, args, `${a.label}: ${row.rid || row.id}`);
}

function stageRows(d){
  const f = STG.filter, q = STG.q.toLowerCase();
  const waiting = new Set((d.needs_you || []).map(x => x.rid).filter(Boolean));
  let rows = d.items.map((r, i) => ({...r, _i: i}));
  if (f === "you") rows = rows.filter(r => waiting.has(r.rid));
  else if (f === "stale") rows = rows.filter(r => r.stale.length);
  else if (f === "person") rows = rows.filter(r => r.by === "person");
  else if (f !== "all") rows = rows.filter(r => String(r.decision) === f);
  if (q) rows = rows.filter(r => (r.rid + " " + r.title + " " + r.decision).toLowerCase().includes(q));
  return rows;
}

function renderStage(){
  const el = $("#v-stage");
  if (!el) return;
  const d = STG.d;
  if (!d) { el.innerHTML = `<div class="card"><div class="card-b"><span class="mut">Loading…</span></div></div>`; return; }
  if (d.ok === false) { el.innerHTML = `<div class="callout warn">${esc(d.message)}</div>`; return; }
  $("#ptitle").textContent = `${d.order} · ${d.label}`;
  $("#psub").textContent = `${d.status}${d.counts.out_of_date ? ` · ${d.counts.out_of_date} out of date` : ""}${d.counts.needs_you ? ` · ${d.counts.needs_you} need you` : ""}`;
  const m = d.models || {};
  const models = [m.writer ? `writer: <b>${esc(m.writer.model || "?")}</b>${m.writer.effort ? ` (${esc(m.writer.effort)})` : ""}${m.writer.fixed ? " · fixed" : ""}` : "",
                  m.verifier ? `verifier: <b>${esc(m.verifier.model || "?")}</b>` : "", `spent ${usd(d.spent_usd || 0)}`].filter(Boolean).join(" · ");
  const values = [...new Set(d.items.map(r => String(r.decision)))].filter(v => v && v !== "undefined");
  const chips = [["all", "All", d.items.length], ["you", "Needs you", d.counts.needs_you], ["stale", "Out of date", d.counts.out_of_date],
                 ["person", "Decided by you", d.items.filter(r => r.by === "person").length], ...values.map(v => [v, v, d.items.filter(r => String(r.decision) === v).length])]
                .filter(c => c[2] || c[0] === "all");
  STG.view = d.items;
  const rows = stageRows(d);
  const table = d.items.length ? `<div class="tbl"><table><thead><tr><th>Item</th><th>Title</th><th>Decision</th><th>By</th><th>Out of date</th><th>Notes</th><th></th></tr></thead><tbody>${
    rows.slice(0, 400).map(r => `<tr id="row-${esc(r.rid || r.id)}">
      <td class="mono">${r.rid ? `<a href="#/studies/${encodeURIComponent(r.rid)}">${esc(r.rid)}</a>` : esc(r.id)}</td>
      <td style="white-space:normal;max-width:420px">${esc(trunc(r.title, 160))}${r.detail ? `<div class="small mut mono">${esc(trunc(r.detail, 160))}</div>` : ""}</td>
      <td>${badge(r.decision ?? "", tone(r.decision))}</td><td class="small">${esc(r.by || "")}</td>
      <td>${r.stale.length ? `<span title="${esc(r.stale.join("\n"))}">${badge("out of date", "warn")}</span>` : ""}</td>
      <td class="small">${r.notes.length ? `<span title="${esc(r.notes.join("\n\n"))}">${badge(r.notes.length + " note" + (r.notes.length > 1 ? "s" : ""), "info")}</span>` : ""}</td>
      <td style="white-space:nowrap">${(r.actions || []).map((a, j) => `<button class="btn sm" onclick="rowAction(${r._i},${j})">${esc(a.label)}</button>`).join(" ")}</td></tr>`).join("")
  }</tbody></table></div>${rows.length > 400 ? `<p class="small mut">Showing 400 of ${rows.length}; filter to see the rest.</p>` : ""}` : `<div class="empty"><b>Nothing here yet</b>${d.status === "not started" ? "This stage has not run." : ""}</div>`;
  const io = `<div class="grid2">
    <div class="card"><div class="card-h"><h2>Inputs</h2></div><div class="card-b small">${d.inputs.length ? d.inputs.map(x => `<div>${esc(x.input)} <span class="mut">· used by ${x.outputs}</span>${x.out_of_date ? ` · ${badge(x.out_of_date + " out of date", "warn")}` : ""}</div>`).join("") : `<span class="mut">none yet</span>`}</div></div>
    <div class="card"><div class="card-h"><h2>Used by</h2></div><div class="card-b small">${Object.keys(d.dependents).length ? Object.entries(d.dependents).map(([t, n]) => `<div>${esc(t)} <span class="mut">· ${n}</span></div>`).join("") : `<span class="mut">nothing yet</span>`}</div></div></div>`;
  const decisions = stageDecisions(d.key);
  const jobs = (d.jobs || []).length ? `<div class="card"><div class="card-h"><h2>Jobs of this stage</h2></div><div class="card-b small">${d.jobs.map(j => `<div>${esc(j.label || j.action)} · ${badge(j.status, tone(j.status))}${j.usd ? ` · ${usd(j.usd)}` : ""}</div>`).join("")}</div></div>` : "";
  el.innerHTML = `<div class="card"><div class="card-b"><div class="small mut" style="margin-bottom:10px">${models}</div>${stageToolbar(d)}</div></div>
    ${decisions ? `<div class="grp">${grpHead("Needs you", d.counts.needs_you || null, "The decisions of this stage that wait for a person.")}${decisions}</div>` : ""}
    ${io}
    <div class="card"><div class="card-h"><h2>Items</h2><span class="grow"></span><input class="inp sm" placeholder="Filter…" value="${esc(STG.q)}" oninput="STG.q=this.value;renderStage()" style="max-width:220px"></div>
      <div class="card-b"><div class="row" style="gap:6px;flex-wrap:wrap;margin-bottom:8px">${chips.map(([v, l, n]) => `<button class="btn sm ${STG.filter === v ? "primary" : "ghost"}" onclick="STG.filter='${esc(v)}';renderStage()">${esc(l)} <span class="cnt">${n}</span></button>`).join("")}</div>${table}</div></div>
    ${jobs}`;
  const want = new URLSearchParams(location.hash.split("?")[1] || "").get("rid");
  if (want) { const r = document.getElementById("row-" + want); if (r) { r.classList.add("hl"); r.scrollIntoView({block: "center"}); } }
}

function stageNav(){
  const box = $("#stagenav");
  if (!box || !S || !S.steps) return;
  box.innerHTML = S.steps.filter(s => s.key !== "feedback").map(s => {
    const dot = s.status === "done" ? "ok" : s.status === "needs you" ? "warn" : s.status === "ready to run" ? "info" : "";
    return `<a href="#/stage/${s.key}" data-stage="${s.key}" class="${tab === "stage" && STG.key === s.key ? "on" : ""}"><span class="dot ${dot}"></span>${esc(s.label.replace(/^\d+\.\s*/, ""))}${s.todos ? `<span class="cnt warn">${s.todos}</span>` : ""}</a>`;
  }).join("");
}
