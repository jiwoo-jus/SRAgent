"""Figures for a planned report (sragent/reportplan.py), drawn from plain data as SVG text. No plotting library.

Each function takes the data it shows and returns one <svg>. Nothing here knows a review's topic: what goes into a
figure (which molecules, which groups, which colours) comes from the report plan.

  evidence_map     how many studies per row x column (bubbles): where the evidence is, and where it is thin
  direction_plot   an effect direction plot: per item and column, one mark per study by direction of effect
  mechanism_map    a schematic: inputs -> intervention -> panels of items with direction and number of studies -> outputs
  dose_plot        each result at its concentration (log axis), by lane and direction, with a reference band
  rob_summary      share of studies per judgment, per risk-of-bias domain (stacked bars)
The forest plot of a pooled group is sragent.meta.forest_svg.
"""
from __future__ import annotations

import math

FONT = "font-family='Helvetica, Arial, sans-serif'"
DIR_COLOR = {"increase": "#c0392b", "decrease": "#1f6fb2", "no change": "#8a8a8a", "mixed": "#8e5bb5", "not stated": "#c9c9c9"}
DIR_MARK = {"increase": "▲", "decrease": "▼", "no change": "●", "mixed": "◆", "not stated": "○"}
ROB_COLOR = {"low": "#3a9d5d", "some_concerns": "#e0b23a", "high": "#cf4b3f", "unclear": "#b5b5b5"}


def x(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _svg(w: float, h: float, body: list[str]) -> str:
    return (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {w:.0f} {h:.0f}' width='{w:.0f}' height='{h:.0f}' {FONT} font-size='12'>"
            f"<rect width='{w:.0f}' height='{h:.0f}' fill='white'/>" + "".join(body) + "</svg>")


def wrap(text: str, width: int) -> list[str]:
    out, cur = [], ""
    for word in str(text).split():
        if cur and len(cur) + 1 + len(word) > width:
            out.append(cur)
            cur = word
        else:
            cur = (cur + " " + word).strip()
    return out + ([cur] if cur else [])


def study_direction(dirs: set) -> str:
    """One direction for a study that reports several results on an item."""
    d = set(dirs) - {None, "not stated"}
    if "increase" in d and "decrease" in d:
        return "mixed"
    return "increase" if "increase" in d else "decrease" if "decrease" in d else "no change" if d else "not stated"


def legend(xp: float, yp: float, keys=("increase", "decrease", "mixed", "no change"), suffix: str = "") -> str:
    out, cx = [], xp
    for k in keys:
        out.append(f"<text x='{cx}' y='{yp}' fill='{DIR_COLOR[k]}' font-size='13'>{DIR_MARK[k]}</text>"
                   f"<text x='{cx + 14}' y='{yp}' fill='#444' font-size='11'>{x(k + suffix)}</text>")
        cx += 30 + 6.2 * len(k + suffix)
    return "".join(out)


def evidence_map(rows: list[str], cols: list[str], counts: dict, groups: dict | None = None, unit: str = "studies") -> str:
    """counts[(row, col)] = number of studies. groups: {row: group label} draws a group heading above its first row."""
    left, top, cw, rh = 250, 54, 118, 24
    heads = []
    ys, yy, last = {}, top, None
    for r in rows:
        g = (groups or {}).get(r)
        if g and g != last:
            heads.append((g, yy + 14))
            yy += 22
            last = g
        ys[r] = yy
        yy += rh
    w, h = left + cw * len(cols) + 30, yy + 34
    big = max(counts.values() or [1])
    b = [f"<text x='{left + cw * i + cw / 2}' y='{top - 22}' text-anchor='middle' font-weight='bold'>{x(c)}</text>" for i, c in enumerate(cols)]
    b += [f"<text x='10' y='{y}' font-weight='bold' fill='#333'>{x(g)}</text>" for g, y in heads]
    for r in rows:
        y = ys[r]
        b.append(f"<line x1='{left}' y1='{y + rh / 2}' x2='{left + cw * len(cols)}' y2='{y + rh / 2}' stroke='#eee'/>"
                 f"<text x='{left - 12}' y='{y + rh / 2 + 4}' text-anchor='end'>{x(r)}</text>")
        for i, c in enumerate(cols):
            n = counts.get((r, c), 0)
            if n:
                rad = 3 + 8 * math.sqrt(n / big)
                b.append(f"<circle cx='{left + cw * i + cw / 2}' cy='{y + rh / 2}' r='{rad:.1f}' fill='#2e6da4' fill-opacity='0.75'/>"
                         f"<text x='{left + cw * i + cw / 2 + rad + 4}' y='{y + rh / 2 + 4}' font-size='11' fill='#333'>{n}</text>")
    b.append(f"<text x='{left}' y='{h - 10}' font-size='11' fill='#666'>Bubble area and number: {x(unit)} reporting the item in that model.</text>")
    return _svg(w, h, b)


def direction_plot(items: list[dict], cols: list[str], groups: dict | None = None) -> str:
    """items: [{label, by: {col: [direction per study]}}]. One mark per study, in the order increase, decrease, mixed,
    no change, so a row reads as a small bar of directions."""
    left, top, rh = 250, 58, 22
    order = ["increase", "decrease", "mixed", "no change", "not stated"]
    widest = {c: max([len(i["by"].get(c) or []) for i in items] + [4]) for c in cols}
    cw = {c: max(26 + 11 * widest[c], 18 + 7 * len(c)) for c in cols}      # a column is at least as wide as its heading
    xs, cx = {}, left
    for c in cols:
        xs[c] = cx
        cx += cw[c]
    b, yy, last = [], top, None
    for c in cols:
        b.append(f"<text x='{xs[c] + 8}' y='{top - 24}' font-weight='bold'>{x(c)}</text>")
    for it in items:
        g = (groups or {}).get(it["label"])
        if g and g != last:
            b.append(f"<text x='10' y='{yy + 14}' font-weight='bold' fill='#333'>{x(g)}</text>")
            yy += 22
            last = g
        b.append(f"<line x1='{left}' y1='{yy + rh / 2}' x2='{cx}' y2='{yy + rh / 2}' stroke='#f0f0f0'/>"
                 f"<text x='{left - 12}' y='{yy + rh / 2 + 4}' text-anchor='end'>{x(it['label'])}</text>")
        for c in cols:
            ds = sorted(it["by"].get(c) or [], key=order.index)
            for k, d in enumerate(ds):
                b.append(f"<text x='{xs[c] + 8 + 11 * k}' y='{yy + rh / 2 + 5}' fill='{DIR_COLOR[d]}' font-size='12'>{DIR_MARK[d]}</text>")
        yy += rh
    b.append(legend(10, yy + 22, keys=("increase", "decrease", "mixed", "no change")) + f"<text x='10' y='{yy + 42}' font-size='11' fill='#666'>"
             "One mark per study; a study with results in both directions is marked mixed. Direction as reported, not effect size.</text>")
    return _svg(max(cx + 20, 560), yy + 54, b)


def mechanism_map(panels: list[dict], inputs: list[str], intervention: str, outputs: list[str], loop: str = "") -> str:
    """panels: [{title, color, items: [{label, dir, n, note}]}], drawn in two columns between the inputs (left) and the
    outputs (right); `loop` labels the arrow that leads from the outputs back to the inputs."""
    pw, gap, side = 300, 18, 190
    inw = [wrap(t, 24) for t in inputs]
    outw = [wrap(t, 22) for t in outputs]
    x0, top = side + 60, 70
    cols = [panels[0::2], panels[1::2]]
    heights = [[46 + 19 * len(p["items"]) for p in col] for col in cols]
    body_h = max(sum(hs) + gap * (len(hs) - 1) for hs in heights)
    w, h = x0 + 2 * pw + gap + 40 + side + 20, top + body_h + 78
    mid = top + body_h / 2
    b = ["<defs><marker id='ah' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'>"
         "<path d='M0,0 L10,5 L0,10 z' fill='#555'/></marker></defs>"]
    ih = 15 * sum(len(t) for t in inw) + 14 * len(inw) + 32
    cxi, yy = 12 + side / 2, mid - ih / 2 + 38
    b.append(f"<rect x='12' y='{mid - ih / 2}' width='{side}' height='{ih}' rx='8' fill='#f4f4f4' stroke='#999'/>"
             f"<text x='{cxi}' y='{mid - ih / 2 + 20}' text-anchor='middle' font-weight='bold'>Placental stress</text>")
    for lines in inw:
        for line in lines:
            b.append(f"<text x='{cxi}' y='{yy}' text-anchor='middle' font-size='11'>{x(line)}</text>")
            yy += 15
        yy += 8
    b.append(f"<line x1='{12 + side + 2}' y1='{mid}' x2='{x0 - 8}' y2='{mid}' stroke='#555' stroke-width='1.6' marker-end='url(#ah)'/>")
    b.append(f"<rect x='{x0 - 2}' y='{top - 44}' width='{2 * pw + gap + 4}' height='26' rx='13' fill='#1f4e79'/>"
             f"<text x='{x0 + pw + gap / 2}' y='{top - 26}' text-anchor='middle' fill='white' font-weight='bold'>{x(intervention)}</text>")
    for ci, col in enumerate(cols):
        yy = top
        for p in col:
            ph = 46 + 19 * len(p["items"])
            px = x0 + ci * (pw + gap)
            b.append(f"<rect x='{px}' y='{yy}' width='{pw}' height='{ph}' rx='8' fill='{p.get('color', '#eef3f8')}' stroke='#9fb3c8'/>"
                     f"<text x='{px + 12}' y='{yy + 20}' font-weight='bold'>{x(p['title'])}</text>")
            for k, it in enumerate(p["items"]):
                d = it.get("dir", "no change")
                ty = yy + 42 + 19 * k
                b.append(f"<text x='{px + 14}' y='{ty}' fill='{DIR_COLOR.get(d, '#888')}' font-size='13'>{DIR_MARK.get(d, '●')}</text>"
                         f"<text x='{px + 32}' y='{ty}'>{x(it['label'])}</text>"
                         + (f"<text x='{px + pw - 10}' y='{ty}' text-anchor='end' font-size='11' fill='#555'>{x(it['note'])}</text>" if it.get("note") else ""))
            yy += ph + gap
    ox = x0 + 2 * pw + gap + 40
    oh = 15 * sum(len(t) for t in outw) + 12 * len(outw) + 32
    cxo, yy = ox + side / 2, mid - oh / 2 + 38
    b.append(f"<line x1='{x0 + 2 * pw + gap + 4}' y1='{mid}' x2='{ox - 8}' y2='{mid}' stroke='#555' stroke-width='1.6' marker-end='url(#ah)'/>"
             f"<rect x='{ox}' y='{mid - oh / 2}' width='{side}' height='{oh}' rx='8' fill='#f4f4f4' stroke='#999'/>"
             f"<text x='{cxo}' y='{mid - oh / 2 + 20}' text-anchor='middle' font-weight='bold'>Placental function</text>")
    for lines in outw:
        for line in lines:
            b.append(f"<text x='{cxo}' y='{yy}' text-anchor='middle' font-size='11'>{x(line)}</text>")
            yy += 15
        yy += 6
    if loop:
        ly = top + body_h + 30
        b.append(f"<path d='M{cxo},{mid + oh / 2 + 2} L{cxo},{ly} L{cxi},{ly} L{cxi},{mid + ih / 2 + 8}' fill='none' stroke='#555' stroke-width='1.4' "
                 f"stroke-dasharray='5 4' marker-end='url(#ah)'/><text x='{(cxi + cxo) / 2}' y='{ly - 7}' text-anchor='middle' font-size='11' fill='#444'>{x(loop)}</text>")
    b.append(legend(x0, h - 12, keys=("increase", "decrease", "mixed", "no change"), suffix=" with " + (intervention.split("/")[0].strip().lower() or "treatment")))
    return _svg(w, h, b)


def dose_plot(points: list[dict], lanes: list[str], band: tuple | None = None, band_label: str = "", unit: str = "µM",
              lo: float = 0.01, hi: float = 100000.0) -> str:
    """points: [{x: concentration, lane, dir}] on a log axis; band: (from, to) shaded with its label."""
    left, top, lh, pw = 230, 46, 34, 620
    h = top + lh * len(lanes) + 78
    lx = lambda v: left + (math.log10(min(max(v, lo), hi)) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)) * pw
    b = []
    if band:
        b.append(f"<rect x='{lx(band[0]):.1f}' y='{top - 8}' width='{lx(band[1]) - lx(band[0]):.1f}' height='{lh * len(lanes) + 8}' fill='#f6e7b0' fill-opacity='0.7'/>"
                 f"<text x='{(lx(band[0]) + lx(band[1])) / 2:.1f}' y='{top - 14}' text-anchor='middle' font-size='11' fill='#7a5d00'>{x(band_label)}</text>")
    t = lo
    while t <= hi * 1.001:
        b.append(f"<line x1='{lx(t):.1f}' y1='{top - 8}' x2='{lx(t):.1f}' y2='{top + lh * len(lanes)}' stroke='#e6e6e6'/>"
                 f"<text x='{lx(t):.1f}' y='{top + lh * len(lanes) + 16}' text-anchor='middle' font-size='11'>{t:g}</text>")
        t *= 10
    slot: dict = {}
    for i, ln in enumerate(lanes):
        y = top + lh * i
        b.append(f"<text x='{left - 12}' y='{y + lh / 2 + 4}' text-anchor='end'>{x(ln)}</text>"
                 f"<line x1='{left}' y1='{y + lh}' x2='{left + pw}' y2='{y + lh}' stroke='#ddd'/>")
    for p in sorted(points, key=lambda p: p["x"]):
        if p["lane"] not in lanes or not p["x"] or p["x"] <= 0:
            continue
        i = lanes.index(p["lane"])
        key = (i, round(lx(p["x"]) / 7))
        k = slot[key] = slot.get(key, 0) + 1
        y = top + lh * i + 8 + (k - 1) % 4 * 7
        b.append(f"<circle cx='{lx(p['x']):.1f}' cy='{y:.1f}' r='3.2' fill='{DIR_COLOR.get(p['dir'], '#bbb')}' fill-opacity='0.8'/>")
    yb = top + lh * len(lanes) + 38
    b.append(f"<text x='{left + pw / 2}' y='{yb}' text-anchor='middle' font-size='12'>Concentration applied ({x(unit)}, log scale)</text>")
    cx = left
    for k in ("increase", "decrease", "no change"):
        b.append(f"<circle cx='{cx + 4}' cy='{yb + 20}' r='3.5' fill='{DIR_COLOR[k]}'/><text x='{cx + 13}' y='{yb + 24}' font-size='11' fill='#444'>{x(k)} with aspirin</text>")
        cx += 150
    return _svg(left + pw + 30, h, b)


def rob_summary(domains: list[tuple[str, dict]], total_label: str = "") -> str:
    """domains: [(label, {judgment: number of studies})], one stacked bar each."""
    left, top, rh, pw = 250, 26, 26, 520
    order = ["low", "some_concerns", "high", "unclear"]
    b = []
    for i, (lab, c) in enumerate(domains):
        y, tot, cx = top + rh * i, sum(c.get(k, 0) for k in order) or 1, left
        b.append(f"<text x='{left - 12}' y='{y + 16}' text-anchor='end'>{x(lab)}</text>")
        for k in order:
            wv = pw * c.get(k, 0) / tot
            if wv > 0:
                b.append(f"<rect x='{cx:.1f}' y='{y + 3}' width='{wv:.1f}' height='18' fill='{ROB_COLOR[k]}'/>"
                         + (f"<text x='{cx + wv / 2:.1f}' y='{y + 16}' text-anchor='middle' font-size='11' fill='white'>{c[k]}</text>" if wv > 22 else ""))
            cx += wv
    y = top + rh * len(domains) + 22
    cx = left
    for k, lab in zip(order, ("Low", "Some concerns", "High", "Unclear")):
        b.append(f"<rect x='{cx}' y='{y - 10}' width='12' height='12' fill='{ROB_COLOR[k]}'/><text x='{cx + 17}' y='{y}' font-size='11'>{lab}</text>")
        cx += 120
    if total_label:
        b.append(f"<text x='{left + pw}' y='{y}' text-anchor='end' font-size='11' fill='#666'>{x(total_label)}</text>")
    return _svg(left + pw + 30, y + 16, b)


KIND_STYLE = {"drug": ("#1f4e79", "#ffffff", "#1f4e79"), "upstream": ("#e8eef6", "#16304d", "#8ba8c6"),
              "mediator": ("#ffffff", "#1c1c1c", "#9aa6b2"), "effector": ("#fdf3e3", "#6b4a06", "#d9b566"),
              "function": ("#eef7ee", "#1e4620", "#8fc096"), "stress": ("#f4f4f4", "#333333", "#9a9a9a")}


def layer_of(nodes: list[dict], edges: list[dict]) -> dict:
    """Each node's column: one past its deepest parent. Nodes are placed in waves of those whose parents are all
    placed; a cycle (a feedback edge) is broken at the node declared first, whose remaining parents become feedback."""
    order = {n["id"]: i for i, n in enumerate(nodes)}
    parents: dict = {n["id"]: [] for n in nodes}
    for e in edges:
        if e["to"] in parents and e["from"] in parents and e["to"] != e["from"]:
            parents[e["to"]].append(e["from"])
    layer: dict = {}
    left = list(order)
    while left:
        ready = [nid for nid in left if all(p in layer for p in parents[nid])]
        if not ready:                                     # only cycles are left: break at the earliest declared node
            ready = [min(left, key=lambda nid: (sum(p not in layer for p in parents[nid]), order[nid]))]
        for nid in ready:
            layer[nid] = max([layer[p] + 1 for p in parents[nid] if p in layer] or [0])
        left = [nid for nid in left if nid not in layer]
    for n in nodes:                                       # a node the plan pins to a column stays there
        if n.get("layer") is not None:
            layer[n["id"]] = int(n["layer"])
    return layer


def pathway_map(nodes: list[dict], edges: list[dict], title: str = "", note: str = "", flow: str = "right") -> str:
    """A signalling cascade: nodes in columns by depth, edges labelled with the step and drawn as an arrow
    (activates, leads to) or a bar (inhibits). A node may carry a second line (`sub`), a direction mark and a note:
    its evidence, as one string or a line per setting. Nodes are laid out so that an edge runs left to right; a
    feedback edge is drawn dashed."""
    lay = layer_of(nodes, edges)
    cols: dict = {}
    for n in nodes:
        cols.setdefault(lay[n["id"]], []).append(n)
    down = flow == "down"                                 # a deep chain reads better down the page than across it
    deepest = max(len(n["note"]) if isinstance(n.get("note"), list) else 1 for n in nodes)    # a note per setting
    nw, nh = (196, 56 + 11 * (deepest - 1)) if down else (182, 54 + 11 * (deepest - 1))
    gapx, gapy, top = (26, 62, 52) if down else (124, 24, 52)
    wide = max(len(v) for v in cols.values())
    deep = max(cols) + 1
    if down:
        w = 40 + wide * (nw + gapx) + 40
        h = top + deep * (nh + gapy) + 76
    else:
        h = top + wide * (nh + gapy) + 76
        w = 40 + deep * (nw + gapx)
    # order the nodes of a column near the average row of their parents, so that fewer edges cross
    parents: dict = {n["id"]: [] for n in nodes}
    for e in edges:
        if e["to"] in parents and e["from"] in parents and lay.get(e["from"], 0) < lay.get(e["to"], 0):
            parents[e["to"]].append(e["from"])
    row: dict = {}
    pos = {}
    for c in sorted(cols):
        members = cols[c]
        if c:
            members.sort(key=lambda n: (sum(row.get(p, 0) for p in parents[n["id"]]) / len(parents[n["id"]])
                                        if parents[n["id"]] else 99))
        block = len(members) * ((nw if down else nh) + (gapx if down else gapy)) - (gapx if down else gapy)
        start = ((w - 80 - block) / 2 + 40) if down else (top + (h - 128 - block) / 2)
        for i, n in enumerate(members):
            row[n["id"]] = i
            pos[n["id"]] = ((start + i * (nw + gapx), top + c * (nh + gapy)) if down
                            else (40 + c * (nw + gapx), start + i * (nh + gapy)))
    b = ["<defs>"
         "<marker id='pa' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='6.5' markerHeight='6.5' orient='auto-start-reverse'>"
         "<path d='M0,0 L10,5 L0,10 z' fill='#5a6472'/></marker>"
         "<marker id='pb' viewBox='0 0 10 10' refX='3' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'>"
         "<path d='M2,0 L2,10' stroke='#b03a2e' stroke-width='3'/></marker></defs>"]
    if title:
        b.append(f"<text x='40' y='30' font-size='15' font-weight='bold'>{x(title)}</text>")
    taken: dict = {}
    for e in edges:
        if e["from"] not in pos or e["to"] not in pos:
            continue
        (x1, y1), (x2, y2) = pos[e["from"]], pos[e["to"]]
        back = lay[e["to"]] <= lay[e["from"]]
        inhibit = e.get("effect") == "inhibits"
        colour = "#b03a2e" if inhibit else "#5a6472"
        mark = "pb" if inhibit else "pa"
        if back:                                           # feedback: around the side
            if down:
                xb = w - 26
                b.append(f"<path d='M{x1 + nw},{y1 + nh / 2} L{xb},{y1 + nh / 2} L{xb},{y2 + nh / 2} L{x2 + nw + 3},{y2 + nh / 2}' fill='none' "
                         f"stroke='{colour}' stroke-width='1.3' stroke-dasharray='5 4' marker-end='url(#{mark})'/>")
                if e.get("label"):
                    b.append(f"<text x='{xb - 6}' y='{(y1 + y2) / 2 + nh / 2}' text-anchor='end' font-size='10.5' fill='#555'>{x(e['label'])}</text>")
                continue
            yb = h - 86
            b.append(f"<path d='M{x1 + nw / 2},{y1 + nh} L{x1 + nw / 2},{yb} L{x2 + nw / 2},{yb} L{x2 + nw / 2},{y2 + nh + 3}' fill='none' "
                     f"stroke='{colour}' stroke-width='1.3' stroke-dasharray='5 4' marker-end='url(#{mark})'/>")
            if e.get("label"):
                b.append(f"<text x='{(x1 + x2) / 2 + nw / 2}' y='{yb - 6}' text-anchor='middle' font-size='10.5' fill='#555'>{x(e['label'])}</text>")
            continue
        if down:
            sx, sy, tx, ty = x1 + nw / 2, y1 + nh, x2 + nw / 2, y2
            my = (sy + ty) / 2
            b.append(f"<path d='M{sx},{sy} C{sx},{my} {tx},{my} {tx},{ty - 3}' fill='none' stroke='{colour}' stroke-width='1.4' marker-end='url(#{mark})'/>")
        else:
            sx, sy, tx, ty = x1 + nw, y1 + nh / 2, x2, y2 + nh / 2
            mx = (sx + tx) / 2
            b.append(f"<path d='M{sx},{sy} C{mx},{sy} {mx},{ty} {tx - 3},{ty}' fill='none' stroke='{colour}' stroke-width='1.4' marker-end='url(#{mark})'/>")
        if e.get("label"):
            lines = wrap(e["label"], 15)
            if down:                                      # in the band between the two rows, beside its own line
                gx = (sx + tx) / 2
                wide = 6.3 * max(len(t) for t in lines) + 14
                for dy in (0, 15, -15, 30, -30):          # a label that would sit on another moves within the band
                    ly = sy + gapy / 2 + 4 + dy
                    used = taken.setdefault(round(ly), [])
                    if not any(abs(gx - u) < wide for u in used):
                        break
                used = taken.setdefault(round(ly), [])
                used.append(gx)
            else:
                gx = sx + gapx / 2                        # in the gap after the source, never over a box
                used = taken.setdefault(round(gx), [])
                ly = (sy + ty) / 2 if abs(sx - tx) < nw + gapx + 2 else sy + (ty - sy) * (gapx / 2) / (tx - sx)
                step = 13 * len(lines) + 4
                while any(abs(ly - u) < step for u in used):   # another label already sits here
                    ly += step
                used.append(ly)
            wd = 6.1 * max(len(t) for t in lines) + 8
            b.append(f"<rect x='{gx - wd / 2:.1f}' y='{ly - 12 * len(lines) - 1}' width='{wd:.1f}' height='{12.5 * len(lines) + 4}' "
                     f"rx='3' fill='white' fill-opacity='0.92'/>")
            for i, line in enumerate(lines):
                b.append(f"<text x='{gx:.1f}' y='{ly - 12 * (len(lines) - 1 - i)}' text-anchor='middle' font-size='10.5' fill='#555'>{x(line)}</text>")
    for n in nodes:
        xp, yp = pos[n["id"]]
        fill, fg, stroke = KIND_STYLE.get(n.get("kind", "mediator"), KIND_STYLE["mediator"])
        b.append(f"<rect x='{xp}' y='{yp}' width='{nw}' height='{nh}' rx='7' fill='{fill}' stroke='{stroke}' stroke-width='1.2'/>")
        d = n.get("dir")
        lab = x(n["label"])
        mark = f"<tspan fill='{DIR_COLOR.get(d, '#888')}'>{DIR_MARK.get(d, '')} </tspan>" if d else ""
        b.append(f"<text x='{xp + nw / 2}' y='{yp + (22 if n.get('sub') or n.get('note') else 31)}' text-anchor='middle' font-size='12.5' "
                 f"font-weight='bold' fill='{fg}'>{mark}{lab}</text>")
        if n.get("sub"):
            b.append(f"<text x='{xp + nw / 2}' y='{yp + 36}' text-anchor='middle' font-size='10.5' fill='{fg}' opacity='0.85'>{x(n['sub'])}</text>")
        evidence = n["note"] if isinstance(n.get("note"), list) else [n["note"]] if n.get("note") else []
        for i, line in enumerate(evidence):
            b.append(f"<text x='{xp + nw / 2}' y='{yp + (49 if n.get('sub') else 38) + 11 * i}' text-anchor='middle' "
                     f"font-size='10' fill='#666'>{x(line)}</text>")
    yl = h - 46
    b.append(f"<text x='40' y='{yl}' font-size='11' fill='#444'>"
             f"<tspan fill='#5a6472'>&#8594;</tspan> activates or leads to"
             f"<tspan fill='#b03a2e' dx='16'>&#8867;</tspan> inhibits</text>")
    b.append(legend(260, yl, keys=("increase", "decrease", "mixed", "no change"), suffix=" with the drug"))
    if note:
        b.append(f"<text x='40' y='{h - 24}' font-size='11' fill='#666'>{x(note)}</text>")
    return _svg(w, h, b)
