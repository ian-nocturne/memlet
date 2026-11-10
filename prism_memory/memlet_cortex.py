"""Memory Cortex tab for the Nocturne Inspector (`memlet ui`).

Shows what the next model will actually receive: sources -> anchored memories -> AST gate -> signed handover
capsule, with a quarantine-orbit hero banner on top. One instance is rendered per workspace (live / demo).

Safety contract (same as the rest of the inspector):
- every string that reaches HTML markup goes through `html.escape`;
- data for the client is embedded as JSON with `<`, `>` and `&` escaped as `\\u003c` / `\\u003e` / `\\u0026`, so no
  markup or `</script>` sequence can appear inside the JSON block; the client inserts it with `textContent` only;
- no network, no external assets; motion respects `prefers-reduced-motion`.
"""

from __future__ import annotations

import html
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_CARDS = 24
MAX_DEAD_ENDS = 6
_ORBIT_CAPS = {"active": 18, "compatible-unverified": 12, "stale": 10}
_STATES = ("active", "compatible-unverified", "stale")


def _e(value: Any) -> str:
    return html.escape("—" if value is None or value == "" else str(value), quote=True)


def _json_for_html(obj: Any) -> str:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _status(value: Any) -> str:
    return value if value in _STATES else "compatible-unverified"


def _load_dream_lineage(ws: Dict[str, Any]) -> Any:
    if not isinstance(ws, dict):
        return None
    p = ws.get("path") or ws.get("root") or ws.get("workspacePath")
    if not p or str(p) == "<repo>":
        return None
    try:
        root_p = Path(str(p))
        q_file = root_p / ".memlet" / "dream" / "APPROVAL-QUEUE.json"
        for chk_p in (root_p, root_p / ".memlet", root_p / ".memlet" / "dream", q_file):
            if chk_p.is_symlink():
                return None
        if q_file.is_file() and q_file.stat().st_size <= 2_000_000:
            from prism_memory.memlet_dream import build_cortex_lineage_payload, recall_subgraph
            g = json.loads(q_file.read_text(encoding="utf-8"))
            if not isinstance(g, dict):
                return None
            return build_cortex_lineage_payload(g, recall_subgraph(g, query_text="", budget_tokens=1500))
    except Exception:
        return None
    return None


def _cortex_data(ws: Dict[str, Any]) -> Dict[str, Any]:
    """Select and order what the client needs; never invents values."""
    anchors = [dict(a, status=_status(a.get("status"))) for a in ws.get("anchors") or []]
    rank = {"stale": 0, "compatible-unverified": 1, "active": 2}
    ordered = sorted(enumerate(anchors), key=lambda p: (rank[p[1]["status"]], p[0]))
    shown = [a for _i, a in ordered[:MAX_CARDS]]
    h = ws.get("handover") or None
    handover: Optional[Dict[str, Any]] = None
    rejected: List[Dict[str, Any]] = []
    if h:
        rejected = list(h.get("rejectedApproaches") or [])
        handover = {k: h.get(k) for k in ("schema", "repoHead", "activeConstraints", "reverifyBeforeUse",
                                          "quarantinedStaleAnchors", "omitted", "capsuleSignature",
                                          "tokenEstimate", "tokenBudget", "authorityBoundary")}
        handover["rejectedApproaches"] = rejected[:MAX_DEAD_ENDS]
    led = ws.get("ledger") or {}
    events = led.get("wikiEvents") or []
    return {
        "workspace": ws.get("id"),
        "anchors": [{k: a.get(k) for k in ("anchor", "status", "reason", "pinned", "currentAstHash",
                                           "currentSigHash", "currentReqSigHash", "sources")} for a in shown],
        "anchorsOmitted": max(0, len(anchors) - len(shown)),
        "deadEndsOmitted": max(0, len(rejected) - MAX_DEAD_ENDS),
        "scenarios": ws.get("scenarios") or [],
        "handover": handover,
        "ledgerEvents": len(events),
        "latestEvent": ({"eventType": events[0].get("eventType"), "recordedAt": events[0].get("recordedAt")}
                        if events else None),
        "ontologyLineage": ws.get("ontologyLineage") or _load_dream_lineage(ws),
    }


# ---------------------------------------------------------------------------
# Hero: quarantine orbit (deterministic SVG, rendered server-side)


def _ellipse_point(cx: float, cy: float, rx: float, ry: float, deg: float) -> tuple:
    rad = math.radians(deg)
    return cx + rx * math.cos(rad), cy + ry * math.sin(rad)


def _hero(ws: Dict[str, Any], wid: str) -> str:
    s = ws.get("anchorSummary") or {}
    counts = {st: int(s.get(st) or 0) for st in _STATES}
    h = ws.get("handover") or {}
    by_status: Dict[str, List[Dict[str, Any]]] = {st: [] for st in _STATES}
    for a in ws.get("anchors") or []:
        by_status[_status(a.get("status"))].append(a)
    cx, cy = 500.0, 132.0
    parts: List[str] = []
    orbit_id = f"cx-orbit-{_e(wid)}"
    parts.append(f'<ellipse cx="{cx}" cy="{cy}" rx="455" ry="112" class="cx-orbit"/>')
    # lower arc, drawn left -> right so the caption reads upright
    parts.append(f'<path id="{orbit_id}" d="M {cx - 455} {cy} A 455 112 0 0 0 {cx + 455} {cy}" fill="none" stroke="none"/>')
    parts.append(f'<ellipse cx="{cx}" cy="{cy}" rx="285" ry="86" class="cx-ring warn"/>')
    parts.append(f'<ellipse cx="{cx}" cy="{cy}" rx="160" ry="52" class="cx-ring ok"/>')
    parts.append(f'<text class="cx-orbit-label"><textPath href="#{orbit_id}" startOffset="50%" text-anchor="middle" dy="-6">'
                 'QUARANTINE ORBIT · NEVER INJECTED INTO THE NEXT MODEL</textPath></text>')
    ring = {"active": (160, 52, 200), "compatible-unverified": (285, 86, 20)}
    for st in ("active", "compatible-unverified"):
        items = by_status[st][:_ORBIT_CAPS[st]]
        rx, ry, start = ring[st]
        for i, a in enumerate(items):
            x, y = _ellipse_point(cx, cy, rx, ry, start + 360.0 * i / max(1, len(items)))
            cls = "ok" if st == "active" else "warn"
            parts.append(f'<g class="cx-hnode {cls}"><title>{_e(a.get("anchor"))} · {_e(st)}</title>'
                         f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{6 if st == "active" else 6.5}"/></g>')
    stale = by_status["stale"][:_ORBIT_CAPS["stale"]]
    for i, a in enumerate(stale):
        # upper-right arc: clear of the headline block on the left and of the caption on the lower arc
        deg = 238 + 92.0 * (i + 0.5) / max(1, len(stale))
        x, y = _ellipse_point(cx, cy, 455, 112, deg)
        dx, dy = (cx - x) * 0.62, (cy - y) * 0.62
        sym = str(a.get("anchor") or "").split("::")[-1]
        anchor_side = "end" if x < cx else "start"
        lx = x - 14 if x < cx else x + 14
        parts.append(
            f'<g class="cx-hnode bad cx-drift" style="--dx:{dx:.1f}px;--dy:{dy:.1f}px;--delay:{0.35 + i * 0.22:.2f}s">'
            f'<title>{_e(a.get("anchor"))} · stale · {_e(a.get("reason"))}</title>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9"/>'
            f'<path d="M{x - 4:.1f} {y - 4:.1f} L{x + 4:.1f} {y + 4:.1f} M{x + 4:.1f} {y - 4:.1f} L{x - 4:.1f} {y + 4:.1f}"/>'
            f'<text x="{lx:.1f}" y="{y + 4:.1f}" text-anchor="{anchor_side}">{_e(sym)}</text></g>')
    tok = h.get("tokenEstimate")
    budget = h.get("tokenBudget")
    core = (f'<g class="cx-core"><circle cx="{cx}" cy="{cy}" r="44"/>'
            f'<text x="{cx}" y="{cy - 6}" class="cx-core-k">CAPSULE</text>'
            f'<text x="{cx}" y="{cy + 16}" class="cx-core-v">{_e(tok if tok is not None else "—")}</text>'
            f'<text x="{cx}" y="{cy + 31}" class="cx-core-k">/ {_e(budget if budget is not None else "—")} TOK</text></g>')
    n = counts["stale"]
    total = sum(counts.values())
    if n:
        headline, sub = (f"{n} rule{'s' if n != 1 else ''} stopped steering your agent",
                         "quarantined · never injected into the next model")
    elif total:
        headline, sub = "Every anchored rule still matches the code", "nothing quarantined in this workspace"
    else:
        headline, sub = "No anchored rules yet", "run `memlet init` to anchor rules to file::Symbol"
    more = sum(max(0, len(by_status[st]) - _ORBIT_CAPS[st]) for st in _STATES)
    more_note = f'<span class="cx-mut"> · {more} more anchors not drawn</span>' if more else ""
    return (
        f'<div class="cx-hero" data-cx="hero">'
        f'<svg viewBox="0 0 1000 264" role="img" aria-label="{_e(headline)}" preserveAspectRatio="xMidYMid meet">'
        + "".join(parts) + core + '</svg>'
        f'<div class="cx-hero-copy"><div class="cx-hero-h">{_e(headline)}</div><div class="cx-hero-s">{_e(sub)}{more_note}</div>'
        f'<div class="cx-hero-c"><span class="ok">● {counts["active"]} active</span>'
        f'<span class="warn">◌ {counts["compatible-unverified"]} re-verify</span>'
        f'<span class="bad">✕ {counts["stale"]} quarantined</span></div>'
        + (f'<button type="button" class="btn ghost cx-mini" data-cx="redrift">↻ Replay quarantine</button>' if n else "")
        + '</div></div>')


# ---------------------------------------------------------------------------
# Tab


def tab_cortex(payload: Dict[str, Any]) -> str:
    parts = []
    for wid, ws in payload["workspaces"].items():
        data = _cortex_data(ws)
        has_anchors = bool(data["anchors"])
        empty = ("" if has_anchors else
                 '<p class="notice">No <code>file::Symbol</code> anchors in this workspace yet. Run '
                 '<code>memlet init</code>, or switch to the Interactive Showcase.</p>')
        sim = '<span class="pill acc">[SIMULATED] showcase data</span>' if wid == "demo" else \
            '<span class="pill">live · read-only</span>'
        parts.append(
            f'<div data-ws="{_e(wid)}" class="cx" data-cx-root>'
            f'<script type="application/json" data-cx="data">{_json_for_html(data)}</script>'
            f'{_hero(ws, wid)}{empty}'
            f'<div class="cx-bar"><div><h3 class="cx-title">What your next model will actually remember</h3>'
            f'<p class="sub" style="margin:0">Every rule passes the AST gate before it may steer an agent. '
            f'The status filter above applies here too.</p></div>'
            f'<div class="cx-btns">{sim}'
            f'<label class="cx-tog"><input type="checkbox" data-cx="sigfirst" checked> signature-break first</label>'
            f'<button type="button" class="btn" data-cx="replay">▶ Assemble capsule</button></div></div>'
            f'<div class="cx-main"><section class="cx-stage" aria-label="memory flow into the handover capsule">'
            f'<div class="cx-flow" data-cx="flow"><svg class="cx-wires" data-cx="wires" aria-hidden="true"></svg>'
            f'<div><div class="cx-colh">Sources</div><div class="cx-srcs" data-cx="srcs"></div></div>'
            f'<div><div class="cx-colh"><span>Memories</span><span data-cx="memcount"></span></div>'
            f'<div class="cx-mems" data-cx="mems"></div><p class="cx-more" data-cx="more" hidden></p></div>'
            f'<div><div class="cx-colh">Gate</div><div class="cx-gates" data-cx="gates">'
            f'<div class="cx-diff" data-cx="diff"></div>'
            f'<div class="cx-gatebox"><b>AST · signature</b><small>file::Symbol must still match</small></div>'
            f'<div class="cx-caption" data-cx="caption">ready</div>'
            f'<div class="cx-out ok" data-out="active"><span>PASS</span><span data-cx="o-active"></span></div>'
            f'<div class="cx-out warn" data-out="compatible-unverified"><span>RE-VERIFY</span><span data-cx="o-compatible-unverified"></span></div>'
            f'<div class="cx-out bad" data-out="stale"><span>QUARANTINE</span><span data-cx="o-stale"></span></div>'
            f'<div class="cx-out dead" data-out="dead"><span>DEAD-END</span><span data-cx="o-dead"></span></div>'
            f'</div></div>'
            f'<div><div class="cx-colh">Capsule → next model</div><div class="cx-capsule" data-cx="capsule">'
            f'<div class="cx-seal" data-cx="seal"><span>SIGNED<br>&lt;{_e((ws.get("handover") or {}).get("tokenBudget") or 500)}<br>TOKENS</span></div>'
            f'<h4 class="cx-caph">Handover capsule</h4><div class="cx-meta" data-cx="meta"></div>'
            f'<div class="cx-gauge"><i data-cx="gauge"></i></div>'
            f'<div class="cx-gline"><span data-cx="gl"></span><span data-cx="gr"></span></div>'
            f'<div class="cx-sec ok"><h5><span>Active constraints</span><span data-cx="c-active"></span></h5><div data-cx="s-active"></div></div>'
            f'<div class="cx-sec warn"><h5><span>Re-verify before use</span><span data-cx="c-warn"></span></h5><div data-cx="s-warn"></div></div>'
            f'<div class="cx-sec bad"><h5><span>Quarantined · do not follow</span><span data-cx="c-bad"></span></h5><div data-cx="s-bad"></div></div>'
            f'<div class="cx-sec dead"><h5><span>Rejected approaches</span><span data-cx="c-dead"></span></h5><div data-cx="s-dead"></div></div>'
            f'<div class="cx-sig" data-cx="sig"></div></div></div>'
            f'</div></section>'
            f'<aside class="card cx-detail" data-cx="detail" aria-live="polite"><h3 data-cx="d-title">Select a memory</h3>'
            f'<p class="sub" data-cx="d-sub">Hover to trace its path. Click to see the verdict.</p>'
            f'<dl class="cx-kv" data-cx="d-kv"></dl><div data-cx="d-verdict"></div><div data-cx="d-rule"></div>'
            f'<div class="cx-legend"><div><i class="cx-g ok"></i>active · travels</div><div><i class="cx-g warn"></i>re-verify first</div>'
            f'<div><i class="cx-g bad"></i>stale · blocked</div><div><i class="cx-g dead"></i>dead-end · carried as warning</div></div>'
            f'<p class="sub" style="margin:10px 0 0">Token count and signature come from the capsule. Per-item token shares are not recorded, so none are shown.</p>'
            f'</aside></div>'
            f'<section class="card cx-lineage" data-cx="lineage" hidden>'
            f'<div class="cx-bar"><div><h3 class="cx-title">Dream Ontology Lineage · 4 Typed Edges (GOVERNS · SUPERSEDES · RESOLVES · BLOCKS)</h3>'
            f'<p class="sub" data-cx="lin-sub">Every edge carries a masked session transcript span and SHA-256 quote hash (verify with --snapshot).</p></div>'
            f'<div class="cx-btns" data-cx="lin-counts"></div></div>'
            f'<div class="cx-lin-grid"><div class="cx-lin-nodes" data-cx="lin-nodes"></div>'
            f'<div class="cx-lin-edges" data-cx="lin-edges"></div></div>'
            f'</section></div>')
    return "".join(parts)


CSS = """
.cx{display:grid;gap:14px}.cx-mut{color:var(--dim)}
.cx-lineage{border:1px solid var(--line);border-radius:14px;padding:14px 16px;background:var(--card)}
.cx-lin-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:10px}
.cx-lin-nodes,.cx-lin-edges{display:grid;gap:8px;align-content:start}
.cx-lnode,.cx-ledge{border:1px solid var(--line2);border-radius:10px;padding:8px 10px;background:var(--card2);font:12px var(--mono)}
.cx-lnode b,.cx-ledge b{color:var(--fg)}.cx-lquote{margin-top:4px;color:var(--mut);font-size:11px;white-space:pre-wrap;word-break:break-word}
.cx-badge{display:inline-block;padding:1px 6px;border-radius:6px;font:600 10px var(--mono);margin-right:6px;border:1px solid currentColor}
.cx-badge.GOVERNS{color:#10B981}.cx-badge.SUPERSEDES{color:#F43F5E}.cx-badge.RESOLVES{color:#38BDF8}.cx-badge.BLOCKS{color:#F59E0B}
.cx-hero{position:relative;border:1px solid var(--line);border-radius:16px;overflow:hidden;
background:radial-gradient(700px 220px at 50% 50%,rgba(99,102,241,.16),transparent 70%),radial-gradient(500px 200px at 12% 60%,rgba(244,63,94,.10),transparent 70%),var(--card)}
.cx-hero svg{display:block;width:100%;height:auto;max-height:280px}
.cx-orbit{fill:none;stroke:rgba(244,63,94,.38);stroke-width:1.4;stroke-dasharray:2 6;stroke-linecap:round}
.cx-ring{fill:none;stroke-width:1}.cx-ring.ok{stroke:rgba(16,185,129,.28)}.cx-ring.warn{stroke:rgba(245,158,11,.25);stroke-dasharray:4 5}
.cx-orbit-label{fill:rgba(253,164,175,.75);font:600 10px var(--mono);letter-spacing:.18em}
.cx-hnode.ok circle{fill:#0B3B2C;stroke:#10B981;stroke-width:2;filter:drop-shadow(0 0 5px rgba(16,185,129,.7))}
.cx-hnode.warn circle{fill:#0D0F17;stroke:#F59E0B;stroke-width:2;stroke-dasharray:3 2.4}
.cx-hnode.bad circle{fill:#1B0F15;stroke:#F43F5E;stroke-width:2;filter:drop-shadow(0 0 6px rgba(244,63,94,.6))}
.cx-hnode.bad path{stroke:#F43F5E;stroke-width:1.8;stroke-linecap:round}
.cx-hnode.bad text{fill:#FDA4AF;font:11.5px var(--mono)}
.cx-drift{animation:cx-drift 1.1s cubic-bezier(.2,.8,.2,1) both;animation-delay:var(--delay)}
@keyframes cx-drift{from{transform:translate(var(--dx),var(--dy));opacity:.2}60%{opacity:1}to{transform:none;opacity:1}}
.cx-core circle{fill:#11132A;stroke:#A5B4FC;stroke-width:2;filter:drop-shadow(0 0 14px rgba(99,102,241,.6))}
.cx-core text{text-anchor:middle;fill:#E6E8EF}.cx-core-k{font:600 8.5px var(--mono);letter-spacing:.14em;fill:#A5B4FC!important}
.cx-core-v{font:700 22px var(--sans)}
.cx-hero-copy{position:absolute;left:18px;top:14px;max-width:340px}
.cx-hero-h{font-size:20px;font-weight:650;letter-spacing:-.02em;line-height:1.2}
.cx-hero-s{color:#FDA4AF;font:12px var(--mono);margin-top:4px}
.cx-hero-c{display:flex;flex-wrap:wrap;gap:12px;margin-top:10px;font:12.5px var(--mono)}
.cx-hero-c .ok{color:var(--ok)}.cx-hero-c .warn{color:var(--warn)}.cx-hero-c .bad{color:var(--bad)}
.btn.cx-mini{margin-top:10px;padding:5px 10px;font-size:12px}
.cx-bar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between}
.cx-title{margin:0 0 2px;font-size:15px;letter-spacing:-.01em}
.cx-btns{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.cx-tog{display:inline-flex;gap:6px;align-items:center;color:var(--mut);font-size:12.5px;cursor:pointer}.cx-tog input{accent-color:var(--bad)}
.cx-main{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px}
.cx-stage{position:relative;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px 16px;overflow:hidden}
.cx-flow{position:relative;display:grid;grid-template-columns:110px minmax(200px,1fr) 140px minmax(230px,1.1fr);column-gap:40px;align-items:start}
.cx-colh{font:10.5px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--dim);margin:0 0 10px;display:flex;justify-content:space-between}
.cx-wires{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible}
.cx-wire{fill:none;stroke-width:1.3;opacity:.3;transition:opacity .25s,stroke-width .25s}
.cx-wire.ok{stroke:var(--ok)}.cx-wire.warn{stroke:var(--warn);stroke-dasharray:4 4}.cx-wire.bad{stroke:var(--bad);stroke-dasharray:1 5;stroke-linecap:round}
.cx-wire.dead{stroke:#A78BFA;stroke-dasharray:6 3}.cx-wire.src{stroke:#8B90A0;opacity:.16}
.cx-wire[data-highlighted=true]{opacity:1;stroke-width:2.2}
.cx-hovering .cx-wire:not([data-highlighted=true]){opacity:.06}
.cx-srcs{display:grid;gap:10px}
.cx-src{border:1px solid var(--line2);border-radius:10px;padding:8px 10px;background:var(--card2);font:12px var(--mono);transition:opacity .2s,box-shadow .2s;overflow-wrap:anywhere}
.cx-src small{display:block;color:var(--dim);font-size:10.5px;margin-top:2px}
.cx-mems{display:grid;gap:5px}
.cx-mem{all:unset;box-sizing:border-box;position:relative;display:grid;grid-template-columns:14px minmax(0,1fr) auto;gap:8px;align-items:center;cursor:pointer;
padding:6px 10px;border-radius:9px;background:var(--card2);border:1px solid var(--line);color:var(--fg);transition:opacity .25s,box-shadow .25s}
.cx-mem:focus-visible,.cx-slot:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.cx-sym{font:12px var(--mono);white-space:nowrap;display:flex;min-width:0}
.cx-sym i{font-style:normal;color:#9CA1B3;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:0 1000 auto}.cx-sym b{font-weight:500;flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}
.cx-sym.txt{display:block;overflow:hidden;text-overflow:ellipsis}
.cx-st{font:10.5px var(--mono);white-space:nowrap}
.cx-g{display:inline-block;width:12px;height:12px;border-radius:50%;border:2px solid currentColor;position:relative;flex:0 0 auto}
.cx-g.ok,.cx-mem.ok{color:var(--ok)}.cx-g.ok{background:currentColor;box-shadow:0 0 8px currentColor}
.cx-g.warn,.cx-mem.warn{color:var(--warn)}.cx-g.warn{border-style:dashed}
.cx-g.bad,.cx-mem.bad{color:var(--bad)}.cx-g.bad{border:0;border-radius:2px}
.cx-g.bad:before,.cx-g.bad:after{content:"";position:absolute;left:5px;top:-1px;width:2px;height:14px;background:currentColor;transform:rotate(45deg)}.cx-g.bad:after{transform:rotate(-45deg)}
.cx-g.dead,.cx-mem.dead{color:#A78BFA}.cx-g.dead{border-radius:3px;transform:rotate(45deg);width:10px;height:10px}
.cx-mem .cx-sym{color:var(--fg)}
.cx-mem.ok{border-color:rgba(16,185,129,.28)}.cx-mem.warn{border-color:rgba(245,158,11,.3);border-style:dashed}
.cx-mem.bad{border-color:rgba(244,63,94,.32);background:linear-gradient(90deg,rgba(244,63,94,.07),var(--card2))}.cx-mem.dead{border-color:rgba(167,139,250,.3)}
.cx-mem.selected{box-shadow:0 0 0 2px var(--acc),0 8px 30px rgba(99,102,241,.25)}
.cx [data-highlighted=true]:not(.cx-wire){box-shadow:0 0 0 1px rgba(255,255,255,.35),0 0 22px rgba(165,180,252,.25)}
.cx-hovering .cx-mem:not([data-highlighted=true]),.cx-hovering .cx-src:not([data-highlighted=true]),.cx-hovering .cx-slot:not([data-highlighted=true]){opacity:.28}
.cx-mem.dim,.cx-slot.dim{opacity:.18}
.cx-mem.rejecting{animation:cx-shake .5s}
@keyframes cx-shake{0%,100%{transform:none}20%{transform:translateX(-5px)}40%{transform:translateX(5px)}60%{transform:translateX(-3px)}80%{transform:translateX(2px)}}
.cx-more{font:11px var(--mono);color:var(--dim);margin:8px 0 0}
.cx-gates{display:grid;gap:12px;position:relative}
.cx-gatebox{border:1px solid var(--line2);border-radius:14px;padding:10px;background:linear-gradient(180deg,rgba(99,102,241,.12),rgba(99,102,241,.02));text-align:center}
.cx-gatebox b{display:block;font:600 11px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:#C7C9FF}
.cx-gatebox small{display:block;color:var(--mut);font-size:11px;margin-top:3px}
.cx-caption{font:10.5px var(--mono);color:var(--mut);text-align:center;min-height:28px;line-height:1.35}
.cx-out{border-radius:10px;padding:8px 10px;border:1px solid;font:11px var(--mono);display:flex;justify-content:space-between;gap:6px;transition:box-shadow .25s,transform .25s}
.cx-out.ok{color:var(--ok);border-color:rgba(16,185,129,.35);background:rgba(16,185,129,.06)}
.cx-out.warn{color:var(--warn);border-color:rgba(245,158,11,.35);background:rgba(245,158,11,.06);border-style:dashed}
.cx-out.bad{color:var(--bad);border-color:rgba(244,63,94,.4);background:rgba(244,63,94,.07)}
.cx-out.dead{color:#A78BFA;border-color:rgba(167,139,250,.35);background:rgba(167,139,250,.06)}
.cx-out.flash{transform:scale(1.06)}
.cx-diff{position:absolute;left:-30px;right:-30px;top:-50px;padding:7px 9px;border-radius:9px;background:#1B0F15;border:1px solid rgba(244,63,94,.55);
font:11px var(--mono);box-shadow:0 10px 30px rgba(244,63,94,.25);opacity:0;transform:translateY(6px);transition:opacity .3s,transform .3s;z-index:3;pointer-events:none}
.cx-diff.on{opacity:1;transform:none}.cx-diff s{color:var(--bad)}.cx-diff ins{text-decoration:none;color:#6EE7B7}.cx-diff div{color:var(--mut);margin-top:3px}
.cx-capsule{position:relative;border-radius:16px;padding:14px;background:linear-gradient(160deg,#171A2A,#10121A 55%);border:1px solid rgba(165,180,252,.35);
box-shadow:0 20px 60px rgba(99,102,241,.18)}
.cx-caph{margin:0;font-size:14px;padding-right:70px}
.cx-meta{font:11px var(--mono);color:var(--mut);margin:3px 0 10px;padding-right:70px;min-height:44px;overflow-wrap:anywhere}
.cx-gauge{height:8px;border-radius:99px;background:rgba(255,255,255,.06);overflow:hidden;position:relative}
.cx-gauge i{position:absolute;inset:0 auto 0 0;width:0;border-radius:99px;background:linear-gradient(90deg,#10B981,#6EE7B7 70%,#F59E0B);transition:width .45s cubic-bezier(.2,.8,.2,1)}
.cx-gauge i.progress{background:linear-gradient(90deg,#6366F1,#A5B4FC)}
.cx-gline{display:flex;justify-content:space-between;gap:8px;font:11px var(--mono);color:var(--mut);margin:6px 0 8px}
.cx-sec{margin:7px 0 0}.cx-sec h5{margin:0 0 5px;font:600 10.5px var(--mono);letter-spacing:.1em;text-transform:uppercase;display:flex;justify-content:space-between}
.cx-sec.ok h5{color:var(--ok)}.cx-sec.warn h5{color:var(--warn)}.cx-sec.bad h5{color:var(--bad)}.cx-sec.dead h5{color:#A78BFA}
.cx-sec>div{display:grid;gap:4px}
.cx-slot{all:unset;box-sizing:border-box;display:block;width:100%;font:11px var(--mono);color:var(--fg);padding:3px 8px;border-radius:7px;background:rgba(255,255,255,.03);
border:1px solid var(--line);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer;transition:opacity .35s,transform .35s,box-shadow .25s}
.cx-sec.bad .cx-slot{text-decoration:line-through;text-decoration-color:rgba(244,63,94,.7);color:#FDA4AF}
.cx-slot.trim{border-style:dashed;color:var(--warn);text-decoration:none;white-space:normal;cursor:default}
.cx-sec.bad .cx-slot.trim{text-decoration:none;color:var(--warn)}
.cx-empty{font:11px var(--mono);color:var(--dim)}
.cx-assembling .cx-slot{opacity:0;transform:translateX(10px)}.cx-assembling .cx-slot.in{opacity:1;transform:none}
.cx-seal{position:absolute;right:10px;top:10px;width:58px;height:58px;border-radius:50%;display:grid;place-items:center;text-align:center;
border:1.5px solid rgba(110,231,183,.7);color:#6EE7B7;font:600 8px/1.15 var(--mono);letter-spacing:.08em;transform:rotate(-12deg);
background:radial-gradient(circle,rgba(16,185,129,.16),transparent 70%)}
.cx-seal.pending{opacity:.15;filter:grayscale(1)}.cx-seal.stamp{animation:cx-stamp .55s cubic-bezier(.2,1.6,.4,1)}
@keyframes cx-stamp{from{transform:rotate(-30deg) scale(1.9);opacity:0}to{transform:rotate(-12deg) scale(1);opacity:1}}
.cx-sig{font:10.5px var(--mono);color:#A5B4FC;word-break:break-all;margin-top:10px;padding-top:8px;border-top:1px dashed var(--line2)}
.cx-tok{position:absolute;width:10px;height:10px;margin:-5px 0 0 -5px;border-radius:50%;pointer-events:none;z-index:4;box-shadow:0 0 14px currentColor,0 0 4px #fff;background:currentColor}
.cx-detail{align-self:start}
.cx-kv{display:grid;grid-template-columns:78px minmax(0,1fr);gap:6px 10px;font-size:12px;margin:0}
.cx-kv dt{color:var(--dim);font:11px var(--mono);text-transform:uppercase;letter-spacing:.05em}.cx-kv dd{margin:0;font-family:var(--mono);overflow-wrap:anywhere}
.cx-verdict{margin:12px 0 0;padding:10px 12px;border-radius:10px;font-weight:600;font-size:13px}
.cx-verdict.ok{background:rgba(16,185,129,.1);color:#6EE7B7;border:1px solid rgba(16,185,129,.35)}
.cx-verdict.warn{background:rgba(245,158,11,.1);color:#FCD34D;border:1px solid rgba(245,158,11,.35)}
.cx-verdict.bad{background:rgba(244,63,94,.1);color:#FDA4AF;border:1px solid rgba(244,63,94,.4)}
.cx-verdict.dead{background:rgba(167,139,250,.1);color:#DDD6FE;border:1px solid rgba(167,139,250,.35)}
.cx-rule{margin-top:10px;padding:9px 11px;border-left:2px solid var(--acc);background:rgba(255,255,255,.02);font-size:12.5px;border-radius:0 8px 8px 0}
.cx-legend{display:grid;gap:7px;font-size:12px;color:var(--mut);margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
.cx-legend div{display:flex;gap:9px;align-items:center}
@media (max-width:1180px){.cx-main{grid-template-columns:minmax(0,1fr)}}
@media (max-width:900px){.cx-flow{grid-template-columns:minmax(0,1fr);row-gap:18px}.cx-wires{display:none}
.cx-gates{grid-template-columns:repeat(2,minmax(0,1fr))}.cx-gatebox,.cx-caption{grid-column:1/-1}.cx-diff{left:0;right:0}
.cx-srcs{grid-template-columns:repeat(2,minmax(0,1fr))}.cx-hero-copy{position:static;padding:14px 16px 0;max-width:none}}
@media (prefers-reduced-motion:reduce){.cx *{animation:none!important;transition:none!important}}
"""

JS = r"""
(function(){
  'use strict';
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var NS='http://www.w3.org/2000/svg';
  var CLS={'active':'ok','compatible-unverified':'warn','stale':'bad','dead':'dead'};
  var COLOR={'active':'#10B981','compatible-unverified':'#F59E0B','stale':'#F43F5E','dead':'#A78BFA'};
  var REVERIFY={
    'unpinned':'Re-verify first — anchor is unpinned, so no baseline hash exists to compare',
    'optional_args_added_signature_compatible':'Re-verify first — an optional argument was added; required signature unchanged',
    'body_changed_signature_compatible':'Re-verify first — function body changed; signature still compatible'
  };
  function el(tag, cls, text){ var e=document.createElement(tag); if(cls) e.className=cls; if(text!=null) e.textContent=String(text); return e; }
  function all(sel, root){ return Array.prototype.slice.call((root||document).querySelectorAll(sel)); }
  function clear(n){ while(n.firstChild) n.removeChild(n.firstChild); }
  function wait(ms){ return new Promise(function(r){ setTimeout(r, reduce?0:ms); }); }
  var instances=[];

  function Cortex(root){
    var q=function(k){ return root.querySelector('[data-cx="'+k+'"]'); };
    var D=JSON.parse(q('data').textContent), H=D.handover;
    var stage=root.querySelector('.cx-stage'), flow=q('flow'), svg=q('wires');
    var ws=root.getAttribute('data-ws');
    var CAP_SHOW=12, SRC_SHOW=8, SRC_WIRES=3;
    var omitted=(H&&H.omitted)||{};

    // ---- model: verification status (from anchors) and capsule disposition (from the capsule) are kept apart
    var scBy={}; (D.scenarios||[]).forEach(function(s){ scBy[s.anchor]=s; });
    var items=[], byKey={};
    D.anchors.forEach(function(a,i){ var p=String(a.anchor).split('::');
      var it={id:ws+'-m'+i,kind:'anchor',status:a.status,anchor:a.anchor,file:p[0],symbol:p.slice(1).join('::'),reason:a.reason,
        refs:(a.sources&&a.sources.length)?a.sources.map(String):['unknown'],a:a};
      it.srcs=it.refs.map(function(s){ return s.replace(/:\d+$/,''); }).filter(function(s,i,arr){ return arr.indexOf(s)===i; });
      items.push(it); byKey[a.anchor]=it; });
    ((H&&H.rejectedApproaches)||[]).forEach(function(r,i){ var s=String(r.source||'').split(':')[0];
      var it={id:ws+'-d'+i,kind:'dead',status:'dead',approach:r.approach,srcs:[s.split('/').pop()||'wiki'],srcFull:r.source};
      items.push(it); byKey['dead:'+i]=it; });
    var rulesBy={}, disp={};
    if(H){
      (H.activeConstraints||[]).forEach(function(c){ (c.anchors||[]).forEach(function(a){ (rulesBy[a]=rulesBy[a]||[]).push(c); disp[a]='active'; }); });
      (H.reverifyBeforeUse||[]).forEach(function(x){ if(!disp[x.anchor]) disp[x.anchor]='reverify'; });
      (H.quarantinedStaleAnchors||[]).forEach(function(x){ disp[x.anchor]='quarantined'; });
    }
    var srcCount={}; items.forEach(function(it){ it.srcs.forEach(function(s){ srcCount[s]=(srcCount[s]||0)+1; }); });
    var sources=Object.keys(srcCount).sort(function(a,b){ return srcCount[b]-srcCount[a]||(a<b?-1:a>b?1:0); });
    var shownSrc=sources.slice(0,SRC_SHOW);

    // ---- sources
    var srcEls={}, memEls={}, outEls={}, slotsBy={}, trimSlots={}, slotSeq=0;
    shownSrc.forEach(function(s){ var d=el('div','cx-src',s); var n=srcCount[s];
      d.appendChild(el('small',null,n+(n===1?' linked memory':' linked memories'))); q('srcs').appendChild(d); srcEls[s]=d; });
    if(sources.length>SRC_SHOW) q('srcs').appendChild(el('div','cx-empty','+'+(sources.length-SRC_SHOW)+' more sources · listed per memory'));

    // ---- memories
    items.forEach(function(it){
      var b=el('button','cx-mem '+CLS[it.status]); b.type='button';
      b.setAttribute('data-node-id',it.id); b.setAttribute('data-status',it.status);
      b.appendChild(el('span','cx-g '+CLS[it.status]));
      var sym=el('span','cx-sym');
      if(it.kind==='dead'){ sym.textContent=it.approach; sym.classList.add('txt'); }
      else { sym.appendChild(el('i',null,it.file+'::')); sym.appendChild(el('b',null,it.symbol)); }
      b.appendChild(sym);
      b.appendChild(el('span','cx-st',it.kind==='dead'?'dead-end':(it.status==='compatible-unverified'?'re-verify':it.status)));
      b.setAttribute('aria-label',it.kind==='dead'?'dead-end: '+it.approach:it.anchor+' '+it.status);
      q('mems').appendChild(b); memEls[it.id]=b;
    });
    q('memcount').textContent=items.length;
    if(D.anchorsOmitted||D.deadEndsOmitted){ var mo=q('more'); mo.hidden=false;
      mo.textContent='+'+D.anchorsOmitted+' anchors'+(D.deadEndsOmitted?' and '+D.deadEndsOmitted+' dead-ends':'')+' not drawn here (display limit) — see the Anchors table (tab 2) and Handover tab'; }
    all('[data-out]',root).forEach(function(o){ outEls[o.getAttribute('data-out')]=o; });
    ['active','compatible-unverified','stale','dead'].forEach(function(k){ q('o-'+k).textContent=items.filter(function(it){return it.status===k;}).length; });

    // ---- capsule: rendered from the capsule's own lists, linked to drawn memories by anchor identity
    function addSlot(key,text,keys,extra){
      var linked=(keys||[]).map(function(k){ return byKey[k]; }).filter(Boolean);
      var s=el(linked.length?'button':'div','cx-slot'+(extra?' '+extra:''),text);
      s._items=linked;
      if(linked.length){ s.type='button'; s.setAttribute('data-node-id','cap-'+ws+'-'+(++slotSeq)); s.setAttribute('aria-label','capsule item: '+text);
        linked.forEach(function(it){ (slotsBy[it.id]=slotsBy[it.id]||[]).push(s); }); }
      q(key).appendChild(s); return s;
    }
    function section(key,list,textOf,keysOf,budgetOmitted,noun,displayOmitted){
      list.slice(0,CAP_SHOW).forEach(function(x,i){ addSlot(key,textOf(x),keysOf(x,i)); });
      var hidden=Math.max(0,list.length-CAP_SHOW)+(displayOmitted||0);
      if(hidden) q(key).appendChild(el('div','cx-empty','+'+hidden+' more in the capsule, not listed here (display limit) — see the Handover tab'));
      if(budgetOmitted) trimSlots[key]=addSlot(key,budgetOmitted+' '+noun+' trimmed to fit '+H.tokenBudget+' tokens — not in the capsule',[],'trim');
      if(!list.length&&!budgetOmitted&&!hidden) q(key).appendChild(el('div','cx-empty','none'));
      return list.length+(displayOmitted||0);
    }
    var nIn={active:0,warn:0,bad:0,dead:0};
    if(H){
      nIn.active=section('s-active',H.activeConstraints||[],function(c){ return (c.anchors||[]).join(', ')||String(c.rule||''); },
        function(c){ return c.anchors||[]; },omitted.activeConstraints||0,'active constraints');
      nIn.warn=section('s-warn',H.reverifyBeforeUse||[],function(x){ return x.anchor; },function(x){ return [x.anchor]; },
        omitted.reverifyBeforeUse||0,'re-verify items');
      nIn.bad=section('s-bad',H.quarantinedStaleAnchors||[],function(x){ return x.anchor; },function(x){ return [x.anchor]; },
        omitted.quarantinedStaleAnchors||0,'quarantined anchors');
      nIn.dead=section('s-dead',H.rejectedApproaches||[],function(r){ return r.approach; },function(r,i){ return ['dead:'+i]; },
        omitted.rejectedApproaches||0,'rejected approaches',D.deadEndsOmitted||0);
      [['c-active','active','activeConstraints'],['c-warn','warn','reverifyBeforeUse'],['c-bad','bad','quarantinedStaleAnchors'],['c-dead','dead','rejectedApproaches']]
        .forEach(function(r){ q(r[0]).textContent=nIn[r[1]]+(omitted[r[2]]?' (+'+omitted[r[2]]+' trimmed)':''); });
      q('meta').textContent='head '+String(H.repoHead||'').slice(0,12)+' · '+String(H.authorityBoundary||'');
      q('sig').textContent='signature '+String(H.capsuleSignature||'—');
    } else {
      ['s-active','s-warn','s-bad','s-dead'].forEach(function(k){ q(k).appendChild(el('div','cx-empty','no capsule')); });
      q('meta').textContent='No handover capsule for this workspace — nothing travels to the next model yet.';
      q('seal').classList.add('pending');
    }
    var TRIM_KEY={'active':'s-active','compatible-unverified':'s-warn','stale':'s-bad','dead':'s-dead'};
    function slotsFor(it){ return slotsBy[it.id]||(trimSlots[TRIM_KEY[it.status]]?[trimSlots[TRIM_KEY[it.status]]]:[]); }
    function outFor(it){ return outEls[it.status]||outEls.dead; }
    function dispText(it){
      if(!H) return 'no capsule';
      var d=disp[it.anchor];
      if(d==='active') return 'active constraints';
      if(d==='reverify') return 're-verify list';
      if(d==='quarantined') return 'quarantined list (name only, never followed)';
      return 'not in capsule';
    }

    function setGauge(frac,done,n,of){
      var g=q('gauge'); if(!H){ g.style.width='0'; q('gl').textContent='no capsule'; q('gr').textContent=''; return; }
      g.classList.toggle('progress',!done);
      g.style.width=(done?H.tokenEstimate/H.tokenBudget*100:Math.max(0,Math.min(1,frac))*100).toFixed(1)+'%';
      if(done){ q('gl').textContent=H.tokenEstimate+' / '+H.tokenBudget+' tokens'; q('gr').textContent=(H.tokenBudget-H.tokenEstimate)+' to spare'; }
      else { q('gl').textContent='assembly progress · '+(n||0)+' / '+(of||0)+' memories'; q('gr').textContent='tokens pending'; }
    }

    // ---- wires
    var wires=[];
    function rel(e){ var r=e.getBoundingClientRect(), f=flow.getBoundingClientRect(); return {l:r.left-f.left,r:r.right-f.left,cy:r.top-f.top+r.height/2,h:r.height}; }
    function curve(x1,y1,x2,y2){ var dx=Math.max(24,(x2-x1)*.5); return 'M'+x1+' '+y1+' C'+(x1+dx)+' '+y1+','+(x2-dx)+' '+y2+','+x2+' '+y2; }
    function layout(){
      clear(svg); wires=[];
      if(!root.offsetParent||getComputedStyle(svg).display==='none') return;
      items.forEach(function(it){
        var m=rel(memEls[it.id]); if(!m.h) return;
        var o=rel(outFor(it)), c=CLS[it.status];
        it.srcs.filter(function(s){ return srcEls[s]; }).slice(0,SRC_WIRES).forEach(function(s){ var sr=rel(srcEls[s]); add('src',curve(sr.r,sr.cy,m.l,m.cy),it.id,'src'); });
        add(c,curve(m.r,m.cy,o.l,o.cy),it.id,'out');
        var sl=slotsFor(it)[0]; if(sl){ var t=rel(sl); if(t.h) add(c,curve(o.r,o.cy,t.l,t.cy),it.id,'cap'); }
      });
      function add(c,d,id,part){ var p=document.createElementNS(NS,'path'); p.setAttribute('d',d); p.setAttribute('class','cx-wire '+c);
        p.setAttribute('data-for',id); p.setAttribute('data-part',part); svg.appendChild(p); wires.push(p); }
      applyFilter();
    }

    // ---- highlight
    function clearHi(){ stage.classList.remove('cx-hovering'); all('[data-highlighted]',root).forEach(function(e){ e.removeAttribute('data-highlighted'); }); }
    function hi(it){ memEls[it.id].setAttribute('data-highlighted','true');
      it.srcs.forEach(function(s){ if(srcEls[s]) srcEls[s].setAttribute('data-highlighted','true'); });
      outFor(it).setAttribute('data-highlighted','true'); slotsFor(it).forEach(function(s){ s.setAttribute('data-highlighted','true'); });
      wires.forEach(function(w){ if(w.getAttribute('data-for')===it.id) w.setAttribute('data-highlighted','true'); }); }
    function hover(list){ clearHi(); if(!list.length) return; stage.classList.add('cx-hovering'); list.forEach(hi); }
    function bind(n,list){ n.addEventListener('mouseenter',function(){ hover(list); }); n.addEventListener('focus',function(){ hover(list); });
      n.addEventListener('mouseleave',clearHi); n.addEventListener('blur',clearHi); n.addEventListener('click',function(){ select(list[0]); }); }
    items.forEach(function(it){ bind(memEls[it.id],[it]); });
    all('button.cx-slot',root).forEach(function(s){ bind(s,s._items); });
    shownSrc.forEach(function(s){ srcEls[s].addEventListener('mouseenter',function(){ hover(items.filter(function(it){ return it.srcs.indexOf(s)>=0; })); });
      srcEls[s].addEventListener('mouseleave',clearHi); });
    Object.keys(outEls).forEach(function(k){ outEls[k].addEventListener('mouseenter',function(){ hover(items.filter(function(it){return it.status===k;})); });
      outEls[k].addEventListener('mouseleave',clearHi); });

    // ---- detail
    function select(it){
      all('.cx-mem.selected',root).forEach(function(e){ e.classList.remove('selected'); }); memEls[it.id].classList.add('selected');
      var kv=q('d-kv'); clear(kv);
      function row(k,v){ kv.appendChild(el('dt',null,k)); kv.appendChild(el('dd',null,(v==null||v==='')?'—':v)); }
      var verdict, vcls=CLS[it.status], rules=[];
      if(it.kind==='dead'){
        q('d-title').textContent='Rejected approach'; q('d-sub').textContent=it.srcFull||it.srcs[0]; row('kind','dead-end'); row('source',it.srcFull);
        verdict='Carried as a warning so the next model does not repeat it'; rules=[it.approach];
      } else {
        q('d-title').textContent=it.symbol; q('d-sub').textContent=it.anchor;
        row('status',it.status); row('reason',it.reason); row('sources',it.refs.join(', ')); row('ast',it.a.currentAstHash);
        row('sig',it.a.currentSigHash); row('req sig',it.a.currentReqSigHash); row('pinned',it.a.pinned?'yes':'no');
        var sc=scBy[it.anchor]; if(sc) row('scenario',sc.id+' · '+sc.title);
        row('capsule',dispText(it));
        var inCap=H&&disp[it.anchor];
        if(it.status==='active'){
          if(!H){ verdict='Active in code · no handover capsule exists, so nothing travels yet'; vcls='warn'; }
          else if(inCap==='active') verdict='Travels to the next model as an active constraint';
          else { verdict='Active in code, but not in the capsule'+(omitted.activeConstraints?' — '+omitted.activeConstraints+' active constraints were trimmed for the '+H.tokenBudget+'-token budget':''); vcls='warn'; }
        } else if(it.status==='stale'){
          verdict='Blocked — quarantined, never injected · '+it.reason;
        } else {
          verdict=(REVERIFY[it.reason]||'Re-verify first · '+it.reason)+(H&&inCap!=='reverify'?' · not in the capsule':'');
        }
        rules=(rulesBy[it.anchor]||[]).map(function(c){ return c.rule+'  — '+c.source; });
        if(!rules.length) rules=[it.status==='stale'?'Source rule text is not carried for quarantined anchors. This stale rule is never injected.'
          :'Rule text is not in the capsule. Re-verify the anchor before relying on it.'];
      }
      var vd=q('d-verdict'); clear(vd); vd.appendChild(el('div','cx-verdict '+vcls,verdict));
      var rd=q('d-rule'); clear(rd); rules.forEach(function(t){ rd.appendChild(el('div','cx-rule',t)); });
    }

    // ---- filter (driven by the inspector's global status filter)
    function applyFilter(){
      var f=document.body.getAttribute('data-filter')||'all';
      function on(it){ return f==='all'||it.status===f; }
      items.forEach(function(it){ memEls[it.id].classList.toggle('dim',!on(it)); });
      all('button.cx-slot',root).forEach(function(s){ s.classList.toggle('dim',!s._items.some(on)); });
      Object.keys(trimSlots).forEach(function(k){ var st=Object.keys(TRIM_KEY).filter(function(x){ return TRIM_KEY[x]===k; })[0];
        trimSlots[k].classList.toggle('dim',!(f==='all'||f===st)); });
      wires.forEach(function(w){ var it=items.filter(function(x){ return x.id===w.getAttribute('data-for'); })[0];
        w.style.opacity=(it&&on(it))?'':'.03'; });
    }

    // ---- replay: assemble the capsule from empty
    var runId=0;
    function fly(it){
      if(reduce||getComputedStyle(svg).display==='none') return Promise.resolve();
      var segs=wires.filter(function(w){ return w.getAttribute('data-for')===it.id&&w.getAttribute('data-part')!=='src'; });
      if(!segs.length) return Promise.resolve();
      var tok=el('div','cx-tok'); tok.style.color=COLOR[it.status]; flow.appendChild(tok);
      var lens=segs.map(function(s){ return s.getTotalLength(); }), total=lens.reduce(function(a,b){return a+b;},0), t0=performance.now();
      return new Promise(function(res){ (function step(now){
        var u=Math.min(1,(now-t0)/620), e=u<.5?2*u*u:1-Math.pow(-2*u+2,2)/2, d=e*total, i=0;
        if(it.status==='stale') d=Math.min(d,lens[0]);
        while(i<segs.length-1&&d>lens[i]){ d-=lens[i]; i++; }
        var pt=segs[i].getPointAtLength(Math.min(d,lens[i])); tok.style.left=pt.x+'px'; tok.style.top=pt.y+'px';
        if(u<1) requestAnimationFrame(step); else { tok.parentNode.removeChild(tok); res(); } })(t0); });
    }
    function outcome(it){
      if(it.kind==='dead') return 'warn';
      if(it.status==='stale') return 'blocked';
      if(!H) return 'no capsule';
      var d=disp[it.anchor];
      return d==='active'?'travels':d==='reverify'?'re-verify':d==='quarantined'?'blocked':'not in capsule';
    }
    async function replay(){
      if(!items.length) return;
      var my=++runId; clearHi(); layout();
      var cap=q('capsule'); cap.classList.add('cx-assembling'); all('.cx-slot',root).forEach(function(s){ s.classList.remove('in'); });
      if(H){ q('seal').classList.add('pending'); q('seal').classList.remove('stamp'); }
      var order=items.slice(), sigfirst=q('sigfirst').checked;
      var sb=(D.scenarios||[]).filter(function(s){ return s.id==='signature-break'; })[0];
      if(sigfirst&&sb) order.sort(function(a,b){ return (b.anchor===sb.anchor)-(a.anchor===sb.anchor); });
      var lead=sigfirst&&sb&&order[0].anchor===sb.anchor;
      setGauge(0,false,0,order.length);
      for(var k=0;k<order.length;k++){
        if(my!==runId) return;
        var it=order[k], m=memEls[it.id], o=outFor(it);
        hover([it]);
        q('caption').textContent=(k+1)+'/'+order.length+' · '+(it.kind==='dead'?'dead-end':it.symbol)+' → '+outcome(it);
        if(k===0&&lead){ var d=q('diff'); clear(d); var p=String(sb.title).split('→');
          d.appendChild(el('s',null,(p[0]||'').trim())); d.appendChild(document.createTextNode(' → ')); d.appendChild(el('ins',null,(p[1]||'').trim()));
          d.appendChild(el('div',null,ws==='demo'?'replaying the recorded refactor · [SIMULATED]':'recorded refactor scenario'));
          d.classList.add('on'); m.classList.add('rejecting'); await wait(900); }
        await fly(it);
        o.classList.add('flash'); setTimeout(function(x){ return function(){ x.classList.remove('flash'); }; }(o),260);
        if(it.status==='stale'){ m.classList.remove('rejecting'); void m.offsetWidth; m.classList.add('rejecting'); }
        slotsFor(it).forEach(function(s){ s.classList.add('in'); });
        setGauge((k+1)/order.length,false,k+1,order.length);
        await wait(k===0&&lead?700:170);
        if(k===0) q('diff').classList.remove('on');
      }
      if(my!==runId) return;
      clearHi(); all('.cx-slot',root).forEach(function(s){ s.classList.add('in'); });
      setGauge(1,true); cap.classList.remove('cx-assembling');
      if(H){ q('caption').textContent='capsule sealed'; var seal=q('seal'); seal.classList.remove('pending'); void seal.offsetWidth; seal.classList.add('stamp'); }
      else q('caption').textContent='no handover capsule to seal';
    }
    q('replay').addEventListener('click',function(){ replay(); });
    var rd=q('redrift'); if(rd) rd.addEventListener('click',function(){
      all('.cx-drift',root).forEach(function(g){ g.classList.remove('cx-drift'); void g.getBoundingClientRect(); g.classList.add('cx-drift'); }); });

    var linSec=q("lineage");
    if(linSec && D.ontologyLineage && (D.ontologyLineage.nodes||[]).length){
      linSec.hidden=false;
      var lc=q("lin-counts"), ln=q("lin-nodes"), le=q("lin-edges");
      clear(lc); clear(ln); clear(le);
      var capInfo=D.ontologyLineage.subgraphCapsule||{};
      var cts=capInfo.counts||{};
      ["BLOCKS:"+String(cts.blocks||0),"RESOLVES:"+String(cts.resolves||0),"GOVERNS:"+String(cts.governs||0),"SUPERSEDED:"+String(cts.supersedesQuarantined||0),"TOKENS:"+String(capInfo.tokenEstimate||0)+"/"+String(capInfo.tokenBudget||1500)].forEach(function(t){
        lc.appendChild(el("span","cx-badge",t));
      });
      (D.ontologyLineage.nodes||[]).forEach(function(n){
        var card=el("div","cx-lnode");
        var head=el("div",null);
        head.appendChild(el("span","cx-badge "+(n.status==="superseded"?"SUPERSEDES":"GOVERNS"),String(n.status||"active")));
        head.appendChild(el("span","cx-badge RESOLVES",String(n.verdict||"UNVERIFIED")));
        if(n.inRecallCapsule) head.appendChild(el("span","cx-badge GOVERNS","IN CAPSULE"));
        head.appendChild(el("b",null,String(n.id||"")+" · "+String(n.title||"")));
        card.appendChild(head);
        card.appendChild(el("div","cx-mut","scope: "+String(n.scopeTag||"")+" · span: "+String(n.srcSpan||"")+" · sha256: "+String(n.sha256Pin||"").slice(0,12)+" · verified: "+String(n.verifiedBy||"—")));
        var qtxt=(n.quoteLines||[]).join(" / ");
        if(qtxt) card.appendChild(el("div","cx-lquote",qtxt));
        if(n.supersededBySpan) card.appendChild(el("div","cx-lquote","SUPERSEDED BY: "+String(n.supersededBySpan)+" (sha256:"+String(n.supersededBySha256||"").slice(0,12)+")"));
        ln.appendChild(card);
      });
      (D.ontologyLineage.edges||[]).forEach(function(e){
        var ec=el("div","cx-ledge");
        var eh=el("div",null);
        var et=["GOVERNS","SUPERSEDES","RESOLVES","BLOCKS"].indexOf(String(e.edgeType||""))>=0?String(e.edgeType):"GOVERNS"; eh.appendChild(el("span","cx-badge "+et,et));
        eh.appendChild(el("b",null,String(e.fromId||"")+" → "+String(e.toId||"")));
        ec.appendChild(eh);
        var meta="src: "+String(e.srcSpan||"")+" ("+String(e.srcSha256||"").slice(0,10)+")";
        if(e.targetSpan) meta+=" · target: "+String(e.targetSpan)+" ("+String(e.targetSha256||"").slice(0,10)+")";
        ec.appendChild(el("div","cx-mut",meta));
        le.appendChild(ec);
      });
    }
    setGauge(1,true);
    return {layout:layout, applyFilter:applyFilter};
  }

  all('[data-cx-root]').forEach(function(r){ instances.push(Cortex(r)); });
  function relayout(){ requestAnimationFrame(function(){ instances.forEach(function(i){ i.layout(); }); }); }
  // Re-layout whenever the visible tab or workspace changes, however it changed (click, arrow keys, script).
  if(window.MutationObserver){
    var mo=new MutationObserver(relayout);
    mo.observe(document.body,{attributes:true,attributeFilter:['data-ws']});
    all('[role=tabpanel]').forEach(function(p){ mo.observe(p,{attributes:true,attributeFilter:['hidden']}); });
    new MutationObserver(function(){ instances.forEach(function(i){ i.applyFilter(); }); })
      .observe(document.body,{attributes:true,attributeFilter:['data-filter']});
  }
  var rt=null; window.addEventListener('resize',function(){ clearTimeout(rt); rt=setTimeout(relayout,120); });
  relayout();
})();
"""
