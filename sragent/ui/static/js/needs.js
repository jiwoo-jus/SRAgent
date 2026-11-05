/* Needs you (04 §5): everything waiting for a person, grouped by stage. A stage's items link to that stage page,
   where the decision is made (one place per decision); what belongs to no stage (the agent's answers to your
   feedback: proposals, questions, disagreements) is answered here. */
function renderNeeds(fromPoll){
  const el = $("#v-needs");
  if (!el || !S) return;
  const ae = document.activeElement;
  if (fromPoll && ae && el.contains(ae) && ["INPUT","TEXTAREA","SELECT"].includes(ae.tagName)) return;
  if (fromPoll && unchanged("needs", [S.todos, S.next])) return;
  if (!fromPoll) unchanged("needs", [S.todos, S.next]);
  const T = S.todos.filter(t => !t.snoozed && !t.optional);
  const label = Object.fromEntries((S.steps || []).map(s => [s.key, s.label.replace(/^\d+\.\s*/, "")]));
  const byStage = {};
  T.filter(t => t.step && t.step !== "feedback").forEach(t => (byStage[t.step] = byStage[t.step] || []).push(t));
  const order = (S.steps || []).map(s => s.key);
  const stageCards = order.filter(k => byStage[k]).map(k => {
    const items = byStage[k];
    return `<div class="card"><div class="card-h"><h2>${esc(label[k] || k)}</h2><span class="cnt warn">${items.length}</span><span class="grow"></span>
        <a class="btn sm" href="#/stage/${k}">Open the stage ${ic("arrowR","sm")}</a></div>
      <div class="card-b flush tw"><table><tbody>${items.map(t => `<tr><td style="white-space:normal">${esc(t.title)}<div class="small mut">${esc(trunc(t.question || "", 160))}</div></td>
        <td class="r"><a class="btn sm ghost" href="#/stage/${k}${t.rid ? "?rid=" + encodeURIComponent(t.rid) : ""}">Decide ${ic("arrowR","sm")}</a></td></tr>`).join("")}</tbody></table></div></div>`;
  }).join("");
  const fb = T.filter(t => ["changeset", "clarify", "disagreement"].includes(t.kind));
  el.innerHTML = nextBanner() + stageCards +
    (fb.length ? `<div class="grp">${grpHead("The agent's answers to your feedback", fb.length, "Accept or reject proposed revisions; where the agent disagreed with you, decide who is right.")}${fb.map(card).join("")}</div>` : "") +
    (stageCards || fb.length ? "" : `<div class="card"><div class="empty"><div class="ei">${ic("check")}</div><b>All caught up</b>Nothing is waiting for you right now.</div></div>`);
}
