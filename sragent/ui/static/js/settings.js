/* Settings (04 §5): the model of each stage and role (the run file's models:; a stage's models are fixed once it
   has started), the budget (the run file's budget:: the total and each stage's limit), and, for benchmark topics,
   evaluations.
   What to run is not here: each stage page's toolbar runs that stage. */
function renderSettings(){
  const el = $("#v-settings");
  if (!el) return;
  if (!M || !runOpts.agent) { el.innerHTML = `<div class="card"><div class="empty"><span class="spin" style="display:inline-block;margin-bottom:8px"></span><br>Loading models…</div></div>`; return; }
  const a = runOpts;
  const cfg = (S && S.run && S.run.config) || "";
  el.innerHTML = `${stageCard()}${budgetCard()}
    <div class="card"><div class="card-h"><h2>Connection</h2></div><div class="card-b"><div class="row"><button class="btn" onclick="testModel()">${ic("bot","sm")} Test the agent model</button><span id="testres" class="small mut">One tiny call, well under $0.001.</span></div></div></div>
    ${M.benchmark ? evalCard() : ""}
    <div class="card"><div class="card-h"><h2>The run file</h2></div><div class="card-b small">Gates (protocol approval, a confirmed search query, waiting for requested PDFs), update policies and the review's own settings are in the run file${cfg ? ` <code>${esc(cfg)}</code>` : ""}: open it in <a href="#/configs">Configs</a>.</div></div>`;
  if (M.benchmark) estimate();
}
/* the budget: the total (a number) and each stage's limit (empty: unlimited); BG holds the edits until saved */
let BG = null;
function budgetCard(){
  const b = S && S.budget;
  if (!b || b.error) return `<div class="card" id="budget"><div class="card-h"><h2>Budget</h2></div><div class="card-b small err">${esc((b && b.error) || "No budget: section in the run file.")}</div></div>`;
  if (!BG) BG = {total: b.total_usd, stages: Object.fromEntries(b.stages.map(x => [x.key, x.limit_usd == null ? "" : x.limit_usd]))};
  const pct = (s, l) => l ? Math.min(100, Math.round(100 * s / l)) : 0;
  const rows = b.stages.map(x => {
    const v = BG.stages[x.key], lim = v === "" ? null : Number(v);
    return `<tr><td>${esc(x.label)}</td><td class="nowrap">${usd(x.spent_usd)}</td>
      <td><div class="field" style="max-width:150px"><span class="mut">$</span><input type="number" min="0" step="0.5" placeholder="unlimited" value="${esc(v)}" oninput="BG.stages['${x.key}']=this.value;BG.dirty=true"></div></td>
      <td style="width:30%">${lim != null ? `<div class="bar" title="${usd(x.spent_usd)} of ${usd(lim)}"><i style="width:${pct(x.spent_usd, lim)}%;${x.spent_usd >= lim ? "background:var(--bad)" : ""}"></i></div>` : `<span class="xs mut">no limit of its own</span>`}</td></tr>`;
  }).join("");
  return `<div class="card" id="budget"><div class="card-h"><div class="grow"><h2>Budget</h2><div class="sub">US dollars this run folder may spend. A model call runs only when the total and its stage both have room. A job that reaches a limit stops cleanly and keeps what it finished; raise the limit and press <b>Resume</b>. Saved in the run file (<code>budget:</code>); it can change at any time.</div></div></div>
    <div class="card-b"><div class="form">
      <label>Total</label><div><div class="row" style="gap:10px"><div class="field" style="max-width:150px"><span class="mut">$</span><input type="number" min="0" step="1" value="${esc(BG.total)}" oninput="BG.total=this.value;BG.dirty=true"></div>
        <span class="small">spent ${usd(b.spent_usd)}</span><div class="bar" style="flex:1;max-width:240px"><i style="width:${pct(b.spent_usd, Number(BG.total))}%"></i></div></div>
        <div class="hint">One hard ceiling for every call of this review. Evaluations have their own (<code>eval.budget_usd</code>).</div></div></div>
      <div class="tw" style="margin-top:12px"><table><thead><tr><th>Stage</th><th>Spent</th><th>Limit</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
      <div class="hint" style="margin-top:6px">Leave a stage empty for no limit of its own (the total still applies).</div></div>
    <div class="card-f"><button class="btn primary" onclick="saveBudget()">${ic("check","sm")} Save</button><span id="budmsg" class="msg"></span></div></div>`;
}
async function saveBudget(){
  const r = await api("/api/budget", {total_usd: BG.total, stages: BG.stages});
  msg("budmsg", r);
  if (r.ok) { BG = null; refresh(); }
}
function editBudget(){ go("#/settings"); setTimeout(() => { const e = document.getElementById("budget"); if (e) e.scrollIntoView({behavior: "smooth"}); }, 300); }
function evalCard(){
  const a = runOpts;
  a.action = "eval";
  const suites = ["all","e2e","screening","stagewise","grounding","synthesis","claim1","claim2","judge"];
  return `<div class="card"><div class="card-h"><div class="grow"><h2>Evaluation</h2><div class="sub">Benchmark topics only. Each evaluation is its own run in eval/runs/ (fresh model calls, its own cost and report).</div></div></div>
    <div class="card-b"><div class="form">
      <label>Suite</label><div><select onchange="runOpts.suite=this.value;estimate()">${suites.map(s => `<option ${a.suite===s?"selected":""}>${s}</option>`).join("")}</select></div>
      <label>Claim-2 cases per type</label><div><input type="number" min="1" placeholder="from config" value="${esc(a.n_cases)}" oninput="runOpts.n_cases=this.value" style="width:120px"></div>
      <label>Agent model</label><div><select style="width:100%" onchange="runOpts.agent={...JSON.parse(this.value),effort:runOpts.agent.effort};estimate()">${modelOptions(a.agent,"agent")}</select></div>
      <label>Judge model</label><div><select style="width:100%" onchange="runOpts.judge={...JSON.parse(this.value),effort:'low'};estimate()">${modelOptions(a.judge,"judge")}</select>
        <div class="hint">Prefer a judge from a different model family than the agent.</div></div>
    </div><div id="est" class="small" style="margin-top:10px"><span class="mut">Estimating…</span></div></div>
    <div class="card-f"><button class="btn primary" onclick="startEval()">${ic("play","sm")} Start the evaluation</button><span id="runmsg" class="msg"></span></div></div>`;
}
async function startEval(){
  const body = {...runOpts, action: "eval", n_cases: runOpts.n_cases ? Number(runOpts.n_cases) : null};
  if (!confirm("Start the evaluation? It makes fresh model calls (the estimate is shown above).")) return;
  const r = await api("/api/run", body);
  $("#runmsg").innerHTML = r.id ? `<span class="saved">${ic("check","sm")} Started: ${esc(r.label || "evaluation")}</span>` : `<span class="err">${esc(r.error || r.message)}</span>`;
  setTimeout(() => refresh(true), 800);
}
