/* Every change to the review is an operation (sragent/ops): runOp() is the page's one way to make one.
   An operation that reaches other outputs is previewed first (what changes, what becomes out of date, what your
   decisions keep, the cost); a model operation runs as a job. */
let OPS = null;
async function loadOps(){
  if (!OPS) { const r = await api("/api/ops"); OPS = Object.fromEntries(((r && r.ops) || []).map(o => [o.name, o])); }
  return OPS;
}
function closeModal(){ const m = $("#modal"); if (m) m.remove(); }
function previewDialog(name, args, label){
  return new Promise(async resolve => {
    closeModal();
    const box = document.createElement("div");
    box.id = "modal"; box.className = "modal";
    box.innerHTML = `<div class="modal-c card"><div class="card-h"><h2>${esc(label || name)}</h2></div><div class="card-b"><span class="mut">Working out what this reaches…</span></div></div>`;
    document.body.appendChild(box);
    const p = await api("/api/preview", {name, args});
    const reach = Object.entries(p.reach || {}).map(([t, ids]) => `<li><b>${ids.length} ${esc(t)}</b> <span class="mut small">${esc(ids.slice(0, 8).join(", "))}${ids.length > 8 ? " …" : ""}</span></li>`).join("");
    const cost = (p.cost || {}).total_usd;
    const body = !p.ok && p.message ? `<div class="callout warn">${esc(p.message)}</div>` : `
      ${p.runs_as_job ? `<p class="small">Runs as a job with model calls; the result is shown for you to accept or reject where it changes something you decided.</p>` : ""}
      <p class="small">${p.changes && p.changes.length ? `Changes <b>${p.changes.length}</b> output(s): <span class="mut">${esc(p.changes.slice(0, 10).join(", "))}${p.changes.length > 10 ? " …" : ""}</span>` : "Changes nothing stored yet."}</p>
      ${reach ? `<p class="small" style="margin-bottom:4px">Then out of date, to bring up to date:</p><ul class="small" style="margin-top:0">${reach}</ul>` : `<p class="small mut">Nothing else becomes out of date.</p>`}
      ${(p.kept || []).length ? `<p class="small">Kept because you decided or edited them: ${esc(p.kept.join(", "))}</p>` : ""}
      ${cost ? `<p class="small">Bringing them up to date costs about ${usd(cost)}.</p>` : ""}`;
    box.querySelector(".card-b").innerHTML = body;
    const f = document.createElement("div"); f.className = "card-f";
    f.innerHTML = `<button class="btn" data-a="no">Cancel</button><span class="grow"></span><button class="btn primary" data-a="yes" ${p.ok === false ? "disabled" : ""}>${p.runs_as_job ? "Start" : "Apply"}</button>`;
    box.querySelector(".modal-c").appendChild(f);
    f.addEventListener("click", e => { const a = e.target.closest("button")?.dataset.a; if (!a) return; closeModal(); resolve(a === "yes"); });
    box.addEventListener("click", e => { if (e.target === box) { closeModal(); resolve(false); } });
  });
}
async function runOp(name, args, label, extra){   // extra: {agent} a model picked for this one job
  await loadOps();
  const o = OPS[name] || {};
  args = args || {};
  if (o.reach) { if (!(await previewDialog(name, args, label))) return null; }
  else if (o.kind === "llm" && !confirm(`${label || name}: runs as a job with model calls. Start it?`)) return null;
  const r = await api("/api/op", {name, args, ...(extra || {})});
  toast(r.message || r.error || (r.ok ? "Done" : "Failed"), !r.ok);
  await refresh(true);
  if (tab === "stage" && STG.key) openStage(STG.key, true);
  return r;
}
