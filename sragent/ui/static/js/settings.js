/* Settings (04 §5): the model of each stage and role (the run file's models:; a stage's models are fixed once it
   has started), the spending cap of the next jobs, and, for benchmark topics, evaluations.
   What to run is not here: each stage page's toolbar runs that stage. */
function renderSettings(){
  const el = $("#v-settings");
  if (!el) return;
  if (!M || !runOpts.agent) { el.innerHTML = `<div class="card"><div class="empty"><span class="spin" style="display:inline-block;margin-bottom:8px"></span><br>Loading models…</div></div>`; return; }
  const a = runOpts;
  const cfg = (S && S.run && S.run.config) || "";
  el.innerHTML = `${stageCard()}
    <div class="card"><div class="card-h"><h2>Spending cap</h2></div><div class="card-b"><div class="form">
      <label>Per job</label><div><div class="field" style="max-width:180px"><span class="mut">$</span><input type="number" min="0" step="0.5" value="${esc(a.extra_budget)}" oninput="runOpts.extra_budget=this.value"></div>
        <div class="hint">Every job (a stage run, a re-run, an update, feedback) stops cleanly when it has spent this much; finished work is kept.</div></div>
      <label></label><div class="row"><button class="btn" onclick="testModel()">${ic("bot","sm")} Test the agent model</button><span id="testres" class="small mut">One tiny call, well under $0.001.</span></div>
    </div></div><div class="card-f"><button class="btn primary" onclick="saveBudget()">${ic("check","sm")} Save</button><span id="budmsg" class="msg"></span></div></div>
    ${M.benchmark ? evalCard() : ""}
    <div class="card"><div class="card-h"><h2>The run file</h2></div><div class="card-b small">Gates (protocol approval, a confirmed search query, waiting for requested PDFs), update policies and the review's own settings are in the run file${cfg ? ` <code>${esc(cfg)}</code>` : ""}: open it in <a href="#/configs">Configs</a>.</div></div>`;
  if (M.benchmark) estimate();
}
async function saveBudget(){
  const r = await api("/api/settings", {extra_budget: runOpts.extra_budget});
  msg("budmsg", r);
}
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
