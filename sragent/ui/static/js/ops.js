/* Every change to the review is an operation (sragent/ops): runOp() is the page's one way to make one.
   An operation that reaches other outputs is previewed first (what changes, what becomes out of date, what your
   decisions keep, the cost); a model operation runs as a job. */
let OPS = null;
async function loadOps(){
  if (!OPS) { const r = await api("/api/ops"); OPS = Object.fromEntries(((r && r.ops) || []).map(o => [o.name, o])); }
  return OPS;
}
function closeModal(){ const m = $("#modal"); if (m) m.remove(); }
/* the change, stage by stage: where each stage it reaches stands now, what applying changes, and what then changes
   when the outputs are brought up to date. Resolves false (cancelled), "apply", or "apply+update". */
function impactTable(p){
  const rows = p.stages || [];
  if (!rows.length) return `<p class="small mut">No stage changes.</p>`;
  const upd = rows.some(r => r.update.length);
  const li = xs => xs.length ? `<ul class="plain">${xs.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : `<span class="mut">no change</span>`;
  return `<div class="tw" style="border:1px solid var(--line);border-radius:10px"><table class="impact"><thead><tr><th>Stage</th><th>Now</th>
      <th>${p.runs_as_job && !upd ? "When it runs" : "When you apply"}</th>${upd ? `<th>Then, when you update outputs</th>` : ""}</tr></thead><tbody>
    ${rows.map(r => `<tr><td class="nowrap"><b>${esc(r.label)}</b></td><td class="mut nowrap">${esc(r.now)}</td><td>${li(r.apply)}</td>
      ${upd ? `<td>${li(r.update)}${r.usd ? `<div class="xs mut">about ${usd(r.usd)}</div>` : ""}</td>` : ""}</tr>`).join("")}
    </tbody></table></div><p class="xs mut" style="margin:6px 0 0">Stages not listed are not changed.</p>`;
}
function previewDialog(name, args, label){
  return new Promise(async resolve => {
    closeModal();
    const box = document.createElement("div");
    box.id = "modal"; box.className = "modal";
    box.innerHTML = `<div class="modal-c card" style="max-width:860px"><div class="card-h"><h2>${esc(label || name)}</h2></div><div class="card-b"><span class="mut">Working out what this changes…</span></div></div>`;
    document.body.appendChild(box);
    const p = await api("/api/preview", {name, args});
    const later = (p.stages || []).some(r => r.update.length) && name !== "update_all";
    const cost = (p.cost || {}).total_usd;
    const body = !p.ok && p.message ? `<div class="callout warn">${esc(p.message)}</div>` : `
      ${!(p.stages || []).length && p.message ? `<p class="small">${esc(p.message)}</p>` : ""}
      ${impactTable(p)}
      ${(p.kept || []).length ? `<p class="small" style="margin-top:10px">Kept as they are, because you decided or edited them: ${esc(p.kept.join(", "))}.</p>` : ""}
      <p class="small" style="margin-top:10px">${p.runs_as_job ? "Runs as a job with model calls. " : ""}${cost ? `Model calls: about ${usd(cost)}.` : "No model calls."}
        ${later ? "Applying changes only the “When you apply” column; the rest waits until you update outputs (or choose “Apply and update outputs”)." : ""}</p>`;
    box.querySelector(".card-b").innerHTML = body;
    const f = document.createElement("div"); f.className = "card-f";
    const dis = p.ok === false ? "disabled" : "";
    f.innerHTML = `<button class="btn" data-a="no">Cancel</button><span class="grow"></span>
      ${later && !p.runs_as_job ? `<button class="btn" data-a="apply+update" ${dis}>Apply and update outputs</button>` : ""}
      <button class="btn primary" data-a="apply" ${dis}>${p.runs_as_job ? "Start" : "Apply"}</button>`;
    box.querySelector(".modal-c").appendChild(f);
    f.addEventListener("click", e => { const a = e.target.closest("button")?.dataset.a; if (!a) return; closeModal(); resolve(a === "no" ? false : a); });
    box.addEventListener("click", e => { if (e.target === box) { closeModal(); resolve(false); } });
  });
}
async function runOp(name, args, label, extra){   // extra: {agent} a model picked for this one job
  await loadOps();
  const o = OPS[name] || {};
  args = args || {};
  // feedback sent with a comment or by "fix" on a flag: what the agent's revision could reach
  const pv = name === "comment" && args.send ? ["feedback", {text: args.text, target: args.target}]
           : name === "flag.answer" && args.option === "fix" ? ["feedback", {text: args.text || "", target: args.node}]
           : name === "flag.answer" && args.option === "rescreen" ? ["eligibility.recompute", {rid: args.node.split(":")[1]}]
           : o.reach ? [name, args] : null;
  let choice = "apply";
  if (pv) { choice = await previewDialog(pv[0], pv[1], label); if (!choice) return null; }
  else if (o.kind === "llm" && !confirm(`${label || name}: runs as a job with model calls. Start it?`)) return null;
  const r = await api("/api/op", {name, args, ...(extra || {})});
  toast(r.message || r.error || (r.ok ? "Done" : "Failed"), !r.ok);
  if (r.ok && choice === "apply+update") {         // the update was previewed with the change: no second question
    const u = await api("/api/op", {name: "update_all", args: {}});
    toast(u.message || u.error || "Update queued", !u.ok);
  }
  await refresh(true);
  if (tab === "stage" && STG.key) openStage(STG.key, true);
  return r;
}
