"""Tiered rule context built from the verified rule index (`memlet index-build`). No model call, fully deterministic.

    memlet rule-context .                       # T0 safety core (verbatim quotes) + T1 topic index, for session start
    memlet rule-recall . "<task text>" --k 3    # T2: the k index spans most relevant to the task, quoted verbatim

Every quoted line comes from the instruction file itself (masked), and only from entries whose pin still matches the
current source (`load_verified_index`). Changed entries are reported, never quoted.
"""

import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

CORE_MAX_LINES = 4
MAX_ENTRIES = 150        # same bound as index-build
MAX_K = 20
MAX_QUERY_TOKENS = 2000
SAFETY_EXTRA = 4
_GUIDE = ("Read the cited lines before acting on any of these topics; rules not shown here may still exist "
          "(see the rule index or the instructions).")
_ASCII_RE = re.compile(r"[a-z0-9][a-z0-9_./\-]*")
_HANGUL_RE = re.compile(r"[가-힣]+")
_STOP = {"the", "a", "an", "of", "to", "in", "for", "on", "with", "and", "or", "is", "are", "be", "by", "at", "as", "it",
         "what", "which", "how", "when", "where", "who", "whom", "why", "does", "do", "did", "this", "that", "these",
         "those", "any", "must", "should", "can", "may", "from", "into", "its", "their", "there", "than", "then"}
_K1, _B = 1.5, 0.75


def _tokens(text: str) -> List[str]:
    text = (text or "").lower()
    out: List[str] = []
    for w in _ASCII_RE.findall(text):
        parts = [w] + [p for p in re.split(r"[./_\-]+", w) if p and p != w]
        out += [p for p in parts if len(p) >= 2 and p not in _STOP]
    for h in _HANGUL_RE.findall(text):
        out += [h[i:i + 2] for i in range(len(h) - 1)] or [h]  # character bigrams: robust to Korean endings
    return out


def _entries(root: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    from prism_memory.memlet_brief import _mask
    from prism_memory.memlet_index import load_verified_index
    fresh, stale, _ = load_verified_index(root)
    import hashlib
    for e in fresh + stale:  # topics and paths come from the index page / file names: mask what is displayed
        e["id"] = e["source"]  # verified citation: private identity for membership, exclusion and de-duplication
        e["key"] = hashlib.sha256(e["id"].encode("utf-8")).hexdigest()[:16]  # opaque, collision-safe public handle
        e["topic"] = _mask(e["topic"])
        e["source"] = _mask(e["source"])  # display only
    return fresh[:MAX_ENTRIES], stale[:MAX_ENTRIES]


def _stale_line(stale: List[Dict[str, Any]]) -> str:
    return (f"- WARNING: {len(stale)} indexed rule(s) changed since the index was built; they are not quoted — re-read: "
            + "; ".join(f"`{e['source']}`" for e in stale[:3]))


def _quote_block(e: Dict[str, Any]) -> str:
    return "\n".join([f"- [{e['topic']}] `{e['source']}`:"] + [f"  > {q}" for q in e["quote"].splitlines()])


AUTO_FULL_THRESHOLD = 8000  # spec-009/010: below this, the whole instructions beat any tiering on accuracy and cost


def _full_instructions(root: Path) -> Tuple[str, int]:
    from prism_memory.memlet_brief import _mask, _source_lines
    from prism_memory.memlet_wiki import estimate_tokens
    files = _source_lines(root)  # discovered instruction files, backup/run-output copies excluded
    text = "\n".join(f"===== {_mask(rel)} =====\n" + _mask("\n".join(lines)) for rel, lines in files.items())
    return text, estimate_tokens(text)


def build_context(repo_path=".", *, core_budget: int = 600, index_budget: int = 600, auto: bool = False,
                  threshold: int = AUTO_FULL_THRESHOLD) -> Dict[str, Any]:
    """auto=True: if the (backup-excluded) instructions fit in `threshold` tokens, return them whole (masked) plus
    the index's change warnings; otherwise the tiered T0+T1 context below."""
    if auto:
        from prism_memory.memlet_wiki import _resolve_root, estimate_tokens
        root = _resolve_root(repo_path)
        full, tokens = _full_instructions(root)
        if tokens <= threshold:
            _, stale = _entries(root)
            text = ("## Project instructions (complete; secret-looking values masked)\n" + full + "\n"
                    + (_stale_line(stale) + "\n" if stale else ""))
            return {"ok": True, "command": "context", "mode": "full", "repoPath": str(root), "instructionTokens": tokens,
                    "threshold": threshold, "stale": len(stale), "tokenEstimate": estimate_tokens(text), "formatted": text}
        res = build_context(repo_path, core_budget=core_budget, index_budget=index_budget)
        res.update({"mode": "tiered", "instructionTokens": tokens, "threshold": threshold})
        return res
    """T0: importance-1 rules quoted verbatim (<= CORE_MAX_LINES lines each). T1: other fresh entries as
    `topic -> file:lines`. The whole rendered text, headings and warnings included, stays within
    core_budget + index_budget tokens; if even the fixed text does not fit, the call fails instead of overrunning."""
    from prism_memory.memlet_wiki import _resolve_root, estimate_tokens
    root = _resolve_root(repo_path)
    fresh, stale = _entries(root)
    core_head = "## Project rules — safety core (quoted verbatim; source unchanged since the index was built)"
    index_head = ("## Rule index — BEFORE acting on any of these topics, read the cited lines "
                  "(not listing a rule here does not mean it does not exist)")
    total = max(0, int(core_budget)) + max(0, int(index_budget))
    fixed = [core_head, index_head] + ([_stale_line(stale)] if stale else [])
    omit_reserve = "- 999 more indexed rule(s) not listed for the token budget; read the instructions if unsure"
    reserve = estimate_tokens("\n".join(fixed + [omit_reserve, "- (none)", "- (none)", ""]))
    if reserve > total:
        return {"ok": False, "command": "context", "repoPath": str(root), "why": f"budget {total} is below the fixed "
                f"text ({reserve} tokens)", "core": [], "index": [], "omitted": len(fresh), "stale": len(stale),
                "tokenEstimate": 0, "formatted": ""}
    core_room = max(0, int(core_budget) - estimate_tokens(core_head))
    core, used = [], 0
    for e in fresh:
        if e["importance"] == 1 and e["lines"] <= CORE_MAX_LINES:
            cost = estimate_tokens(_quote_block(e))
            if used + cost <= core_room:
                core.append(e)
                used += cost
    core_src = {e["id"] for e in core}
    index_room = total - reserve - used
    index, iused, omitted = [], 0, 0
    for e in fresh:
        if e["id"] in core_src:
            continue
        cost = estimate_tokens(f"- {e['topic']} → `{e['source']}`")
        if iused + cost <= index_room:
            index.append(e)
            iused += cost
        else:
            omitted += 1

    def render():
        lines = [core_head] + ([_quote_block(e) for e in core] or ["- (none)"])
        lines += ["", index_head] + ([f"- {e['topic']} → `{e['source']}`" for e in index] or ["- (none)"])
        if omitted:
            lines.append(f"- {omitted} more indexed rule(s) not listed for the token budget; read the instructions if unsure")
        if stale:
            lines.append(_stale_line(stale))
        return "\n".join(lines) + "\n"
    text = render()
    while estimate_tokens(text) > total and index:  # estimator is not additive across joins: trim until it truly fits
        index.pop()
        omitted += 1
        text = render()
    while estimate_tokens(text) > total and core:
        core.pop()
        omitted += 1
        text = render()
    return {"ok": estimate_tokens(text) <= total, "command": "context", "repoPath": str(root),
            "core": [e["source"] for e in core], "index": [e["source"] for e in index], "omitted": omitted,
            "coreKeys": [e["key"] for e in core], "indexKeys": [e["key"] for e in index],
            "stale": len(stale), "tokenEstimate": estimate_tokens(text), "tokenBudget": total, "formatted": text}


def recall(repo_path=".", query: str = "", *, k: int = 3, exclude: Iterable[str] = (), safety: bool = False) -> Dict[str, Any]:
    """T2: BM25 over (topic + cited source text) of fresh entries; top-k spans quoted verbatim. Deterministic."""
    from prism_memory.memlet_wiki import _resolve_root, estimate_tokens
    root = _resolve_root(repo_path)
    fresh, stale = _entries(root)
    k = max(0, min(int(k), MAX_K))
    skip = set(exclude)  # verified citations or opaque keys (never masked display strings, which can collide)
    out_of = lambda e: e["id"] in skip or e["key"] in skip
    docs = [(e, _tokens(e["topic"] + " " + e["quote"])) for e in fresh if not out_of(e)]
    vocab = {t for _, toks in docs for t in toks}
    q = [t for t in dict.fromkeys(_tokens(query)[:MAX_QUERY_TOKENS]) if t in vocab]
    n = len(docs)
    avg = sum(len(t) for _, t in docs) / n if n else 0.0
    df = Counter(t for _, toks in docs for t in set(toks))
    scored = []
    for rank, (e, toks) in enumerate(docs):
        tf = Counter(toks)
        s = 0.0
        for t in q:
            if not tf[t]:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * tf[t] * (_K1 + 1) / (tf[t] + _K1 * (1 - _B + _B * len(toks) / (avg or 1)))
        if s > 0:
            scored.append((-round(s, 9), rank, e))
    scored.sort(key=lambda x: (x[0], x[1]))
    picked = [e for _, _, e in scored[:k]]
    if safety:  # every importance-1 rule stays reachable: those not already shown are added regardless of score
        shown = {e["id"] for e in picked}
        forced = [e for e in fresh if e["importance"] == 1 and not out_of(e) and e["id"] not in shown]
        picked = picked + forced[:SAFETY_EXTRA]
    lines = ["## Rules relevant to this task (quoted verbatim from the instructions)", _GUIDE]
    lines += [_quote_block(e) for e in picked] or ["- (no indexed rule matched; consult the rule index)"]
    if stale:
        lines.append(_stale_line(stale))
    text = "\n".join(lines) + "\n"
    return {"ok": True, "command": "recall", "repoPath": str(root), "k": k, "stale": len(stale),
            "spans": [e["source"] for e in picked], "spanKeys": [e["key"] for e in picked],
            "scores": [round(-s, 4) for s, _, _ in scored[:k]], "tokenEstimate": estimate_tokens(text),
            "formatted": text}
