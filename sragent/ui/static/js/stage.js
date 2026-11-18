/* One page per stage (docs/refactor/04-refactor-plan.md §5.1): the same template for all ten, from GET /api/stage.
   Header (status, models, spent) · the five-button toolbar · decisions waiting for you · inputs and dependents ·
   the items table (every output of the stage, with what a person can do to it) · this stage's jobs.
   Every change goes through runOp() (ops.js): previewed first when it reaches other outputs. */
const STG = {key: null, d: null, filter: "all", q: "", loading: false, sel: new Set()};

async function openStage(key, quiet){
  STG.key = key;
  if (!quiet) { STG.filter = "all"; STG.q = ""; STG.d = null; STG.sel = new Set(); renderStage(); }
  STG.loading = true;
  const d = await api("/api/stage?key=" + encodeURIComponent(key));
  STG.loading = false;
  if (STG.key !== key) return;
  STG.d = d;
  STG.sel = new Set([...STG.sel].filter(i => d.items[i] && bulkActs(d.items[i]).length));
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
  else if (f !== "all") rows = rows.filter(r => String(r.decision) === f);
  if (q) rows = rows.filter(r => (r.rid + " " + r.title + " " + r.decision).toLowerCase().includes(q));
  return rows;
}

/* rows whose operations take a list of studies can be applied to many rows at once (select, then one button) */
function bulkActs(r){ return (r.actions || []).filter(a => Array.isArray((a.args || {}).rids)); }

function selectRow(i, on){ on ? STG.sel.add(i) : STG.sel.delete(i); renderStage(); }

function selectAll(on){
  const ids = stageRows(STG.d).filter(r => bulkActs(r).length).map(r => r._i);
  ids.forEach(i => on ? STG.sel.add(i) : STG.sel.delete(i));
  renderStage();
}

function bulkBar(d){
  if (!STG.sel.size) return "";
  const by = {};
  for (const i of STG.sel) for (const a of bulkActs(d.items[i])) (by[a.op] = by[a.op] || {label: a.label, rids: []}).rids.push(...a.args.rids);
  return `<div class="row" style="gap:8px;flex-wrap:wrap;margin-bottom:8px;align-items:center"><b class="small">${STG.sel.size} selected</b>
    ${Object.entries(by).map(([op, x]) => `<button class="btn sm primary" onclick="bulkRun('${esc(op)}')">${esc(x.label)} (${x.rids.length})</button>`).join("")}
    <button class="btn sm ghost" onclick="STG.sel=new Set();renderStage()">Clear selection</button></div>`;
}

async function bulkRun(op){
  const rows = [...STG.sel].map(i => STG.d.items[i]);
  const a = rows.flatMap(r => bulkActs(r).filter(x => x.op === op));
  const rids = [...new Set(a.flatMap(x => x.args.rids))];
  if (!rids.length) return;
  STG.sel = new Set();
  await runOp(op, {rids}, `${a[0].label}: ${rids.length} studies`);
}

/* pooled results of a review with a meta-analysis plan (sragent/meta.py): the table and its forest plots */
const META = {open: null, all: false};
function metaCard(m){
  if (!m) return "";
  if (m.error) return `<div class="callout warn">${esc(m.error)}</div>`;
  const f2 = x => Number(x).toFixed(2), est = x => !x ? "" : x.ratio == null ? `k = ${x.k}` : `${f2(x.ratio)} (${f2(x.ratio_ci[0])}–${f2(x.ratio_ci[1])}), k = ${x.k}`;
  const pooled = m.groups.filter(g => g.ratio != null), rest = m.groups.filter(g => g.ratio == null);
  const rows = pooled.map((g, i) => `<tr style="cursor:pointer" onclick="META.open=META.open===${i}?null:${i};renderStage()" title="Show the forest plot">
      <td>${esc(g.group)}</td><td class="small mut">${esc(g.measure)}</td><td class="r">${g.k}</td>
      <td class="nowrap"><b>${f2(g.ratio)}</b> (${f2(g.ci[0])}–${f2(g.ci[1])})${g.ci[0] > 1 || g.ci[1] < 1 ? " " + badge("excludes 1", "ok") : ""}</td>
      <td class="r">${Math.round(g.i2)}%</td><td class="small nowrap">${g.prediction ? `${f2(g.prediction[0])}–${f2(g.prediction[1])}` : ""}</td>
      <td class="small nowrap">${est((g.sensitivity || {})["reported numbers only"])}${g.estimated ? ` <span class="mut">(${g.estimated} est.)</span>` : ""}</td>
      <td class="small nowrap">${est((g.sensitivity || {})["without high risk of bias"])}</td></tr>
      ${META.open === i ? `<tr><td colspan="8" style="background:var(--bg-2, #fafafa)"><img src="${U(g.plot)}" alt="forest plot" style="max-width:100%"></td></tr>` : ""}`).join("");
  const why = Object.entries(m.not_used || {}).map(([k, v]) => `${esc(k)}: ${v}`).join(" · ");
  return `<div class="card"><div class="card-h"><h2>Pooled results</h2><span class="grow"></span>
      <button class="btn sm" onclick="runOp('synthesis.meta_analysis', {}, 'Compute the pooled results again')">${ic("refresh", "sm")} Compute again (no model)</button></div>
    <div class="card-b"><div class="small mut" style="margin-bottom:8px">${m.rows_used} of ${m.rows} extracted rows gave an effect size; ${pooled.length} groups have at least ${m.min_studies} studies and are pooled (random effects, Hartung-Knapp interval). A ratio below 1 means lower with treatment. Computed from the current rows each time this page opens; click a row for its forest plot.</div>
    ${pooled.length ? `<div class="tbl"><table><thead><tr><th>Group</th><th>Measure</th><th>Studies</th><th>Ratio (95% CI)</th><th>I²</th><th>Prediction interval</th><th>Reported numbers only</th><th>Without high risk of bias</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="empty"><b>No group has enough studies to pool yet</b></div>`}
    ${rest.length ? `<details style="margin-top:10px" ${META.all ? "open" : ""} ontoggle="META.all=this.open"><summary class="small mut" style="cursor:pointer">${rest.length} groups not pooled (fewer than ${m.min_studies} studies)</summary><div class="small" style="margin-top:6px">${rest.map(g => `${esc(g.group)} (${g.k})`).join(" · ")}</div></details>` : ""}
    ${why ? `<div class="xs mut" style="margin-top:10px">Rows not used: ${why}</div>` : ""}
    ${(m.possible_duplicates || []).length ? `<div class="callout warn" style="margin-top:10px">Possible duplicates (the same effect in one group): ${m.possible_duplicates.map(x => esc(x.studies.join(" and ") + " in " + Object.values(x.group).join(" · "))).join("; ")}. If they report one trial, list them under <code>meta_analysis.same_study</code> in the topic file.</div>` : ""}
    </div></div>`;
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
  const models = [...Object.entries(m).map(([role, c]) => `${role}: <b>${esc(c.model || "?")}</b>${c.reasoning_effort ? ` (${esc(c.reasoning_effort)})` : ""} ${usd(c.spent_usd || 0)}`),
                  Object.values(m).some(c => c.fixed) ? "fixed" : "",
                  `spent ${usd(d.spent_usd || 0)}${d.budget ? (d.budget.limit_usd != null ? ` of ${usd(d.budget.limit_usd)}` : " (no stage limit)") : ""}`
                  + (d.budget ? ` · <a href="javascript:void(0)" onclick="editBudget()">Edit</a>` : "")].filter(Boolean).join(" · ");
  const values = [...new Set(d.items.map(r => String(r.decision)))].filter(v => v && v !== "undefined");
  const chips = [["all", "All", d.items.length], ["you", "Needs you", d.counts.needs_you], ["stale", "Out of date", d.counts.out_of_date],
                 ...values.map(v => [v, v, d.items.filter(r => String(r.decision) === v).length])]
                .filter(c => c[2] || c[0] === "all");
  STG.view = d.items;
  const rows = stageRows(d);
  const selectable = rows.filter(r => bulkActs(r).length).map(r => r._i);
  const table = d.items.length ? `<div class="tbl"><table><thead><tr>${selectable.length ? `<th><input type="checkbox" title="Select all ${selectable.length} rows in this tab" ${selectable.every(i => STG.sel.has(i)) ? "checked" : ""} onchange="selectAll(this.checked)"></th>` : ""}<th>Item</th><th>Title</th><th>Decision</th><th>By</th><th>Out of date</th><th>Notes</th><th></th></tr></thead><tbody>${
    rows.slice(0, 400).map(r => `<tr id="row-${esc(r.rid || r.id)}">
      ${selectable.length ? `<td>${bulkActs(r).length ? `<input type="checkbox" ${STG.sel.has(r._i) ? "checked" : ""} onchange="selectRow(${r._i}, this.checked)">` : ""}</td>` : ""}
      <td class="mono">${r.rid ? `<a href="#/studies/${encodeURIComponent(r.rid)}">${esc(r.rid)}</a>` : esc(r.id)}</td>
      <td style="white-space:normal;max-width:420px">${esc(trunc(r.title, 160))}${r.detail ? `<div class="small mut mono">${esc(trunc(r.detail, 160))}</div>` : ""}</td>
      <td>${badge(r.decision ?? "", tone(r.decision))}</td><td class="small">${r.by === "person" ? badge("by you", "info") : esc(r.by || "")}</td>
      <td>${r.stale.length ? `<span title="${esc(r.stale.join("\n"))}">${badge("out of date", "warn")}</span>` : ""}</td>
      <td class="small">${r.notes.length ? `<span title="${esc(r.notes.join("\n\n"))}">${badge(r.notes.length + " note" + (r.notes.length > 1 ? "s" : ""), "info")}</span>` : ""}</td>
      <td style="white-space:nowrap">${(r.actions || []).map((a, j) => `<button class="btn sm" onclick="rowAction(${r._i},${j})">${esc(a.label)}</button>`).join(" ")}</td></tr>`).join("")
  }</tbody></table></div>${rows.length > 400 ? `<p class="small mut">Showing 400 of ${rows.length}; filter to see the rest.</p>` : ""}` : `<div class="empty"><b>Nothing here yet</b>${d.status === "not started" ? "This stage has not run." : ""}</div>`;
  const io = `<div class="grid2">
    <div class="card"><div class="card-h"><h2>Inputs</h2></div><div class="card-b small">${d.inputs.length ? d.inputs.map(x => `<div>${esc(x.input)} <span class="mut">· used by ${x.outputs}</span>${x.out_of_date ? ` · ${badge(x.out_of_date + " out of date", "warn")}` : ""}</div>`).join("") : `<span class="mut">none yet</span>`}</div></div>
    <div class="card"><div class="card-h"><h2>Used by</h2></div><div class="card-b small">${Object.keys(d.dependents).length ? Object.entries(d.dependents).map(([t, n]) => `<div>${esc(t)} <span class="mut">· ${n}</span></div>`).join("") : `<span class="mut">nothing yet</span>`}</div></div></div>`;
  const decisions = stageDecisions(d.key);
  const jobs = (d.jobs || []).length ? `<div class="card"><div class="card-h"><h2>Jobs of this stage</h2></div><div class="card-b small">${d.jobs.map(j => `<div>${esc(j.label || j.action)} · ${badge(j.status, tone(j.status))}${j.usd ? ` · ${usd(j.usd)}` : ""}</div>`).join("")}</div></div>` : "";
  const dl = (d.downloads || []).map(x => `<a class="btn sm" href="${U(x.href)}" title="${esc(x.hint || "")}">${ic("download", "sm")} ${esc(x.label)}</a>`).join(" ");
  const fg = (d.extras || {}).figures;
  const figs = fg && (fg.rows || fg.no_pdf) ? `<div class="row" style="gap:8px;margin-top:10px;align-items:center;flex-wrap:wrap">
      <button class="btn sm" ${fg.rows ? "" : "disabled"} onclick="runOp('extract.read_figures', {}, 'Read the values of figure-only results')">${ic("play", "sm")} Read figures (${fg.rows} rows)</button>
      <span class="small mut">${fg.rows ? `${fg.rows} results in ${fg.studies} studies are shown only in a figure (${fg.figures} figures); their values are estimated from the page image and marked as estimates.` : "No figure-only results left to read."}
      ${fg.no_pdf ? ` ${fg.no_pdf} more studies have such results but no PDF (text from PubMed Central): add <code>&lt;id&gt;.pdf</code> to the full-text folder <span title="${esc((fg.no_pdf_ids || []).join(", "))}" style="text-decoration:underline dotted;cursor:help">which</span>.` : ""}</span></div>` : "";
  el.innerHTML = `<div class="card"><div class="card-b"><div class="small mut" style="margin-bottom:10px">${models}</div>${stageToolbar(d)}${dl ? `<div class="row" style="gap:8px;margin-top:10px">${dl}</div>` : ""}${figs}</div></div>
    ${metaCard((d.extras || {}).meta)}
    ${decisions ? `<div class="grp">${grpHead("Needs you", d.counts.needs_you || null, "The decisions of this stage that wait for a person.")}${decisions}</div>` : ""}
    ${io}
    <div class="card"><div class="card-h"><h2>Items</h2><span class="grow"></span><input class="inp sm" placeholder="Filter…" value="${esc(STG.q)}" oninput="STG.q=this.value;STG.sel=new Set();renderStage()" style="max-width:220px"></div>
      <div class="card-b"><div class="row" style="gap:6px;flex-wrap:wrap;margin-bottom:8px">${chips.map(([v, l, n]) => `<button class="btn sm ${STG.filter === v ? "primary" : "ghost"}" onclick="STG.filter='${esc(v)}';STG.sel=new Set();renderStage()">${esc(l)} <span class="cnt">${n}</span></button>`).join("")}</div>${bulkBar(d)}${table}</div></div>
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
