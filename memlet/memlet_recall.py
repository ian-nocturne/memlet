"""Deterministic rule recall: reads the pinned index without making model calls.
Index creation (`memlet admin index-build`) separately invokes the `claude` CLI and makes a model call.

    memlet rule-context .                       # T0 safety core (verbatim quotes) + T1 topic index, for session start
    memlet rule-recall . "<task text>" --k 3    # T2: the k index spans most relevant to the task, quoted verbatim

Every quoted line comes from the instruction file itself (masked), and only from entries whose pin still matches the
current source (`load_verified_index`). Changed entries are reported, never quoted.
"""

from .vocabulary import suggested_label

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


MAX_PROMOTED = 100  # approved Dream lessons promoted to `.memlet/wiki/dream-approved.md` (memlet_bridge)


MAX_PARSED = 600        # parsed-fallback rule spans considered per call
MAX_PARSED_SPAN = 12    # same span bound as index-build


def _rule_spans(lines: List[str]) -> List[Tuple[int, int, str]]:
    """Complete rule spans of one Markdown file as (first, last, section), 1-based inclusive: a list item with its
    continuation lines, or a paragraph; headings, code fences, tables, HTML comments and front matter are not rules.
    Deterministic; a span is cut at MAX_PARSED_SPAN lines."""
    from memlet.memlet_wiki import _BULLET_RE, _FENCE_RE, _HEADING_RE
    spans: List[Tuple[int, int, str]] = []
    section, in_fence, cur = "", False, None
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, min(len(lines), 60)):
            if lines[i].strip() == "---":
                start = i + 1
                break

    def close():
        if cur:
            spans.append((cur[0], cur[1], cur[2]))

    for idx in range(start, len(lines)):
        no, line = idx + 1, lines[idx]
        stripped = line.strip()
        if _FENCE_RE.match(line):
            close()
            cur, in_fence = None, not in_fence
            continue
        if in_fence:
            continue
        h = _HEADING_RE.match(line)
        if h or not stripped or stripped.startswith(("<!--", "|")):
            close()
            cur = None
            if h:
                section = h.group("title").strip()
            continue
        if _BULLET_RE.match(line) or cur is None or cur[1] - cur[0] + 1 >= MAX_PARSED_SPAN:
            close()
            cur = [no, no, section]
        else:
            cur[1] = no  # continuation of the item or paragraph
    close()
    return spans


def parsed_rule_entries(root: Path) -> List[Dict[str, Any]]:
    """Deterministic fallback when no rule index exists (never a model call): every discovered instruction file and
    hand-written wiki page split into complete rule spans, each cited `file:first-last` with the SHA-256 of its
    current text, quoted from the secret-masked file. Not reviewed or ranked by importance: `importance` is 2 for all."""
    from memlet.memlet_brief import _mask, _sha, _source_lines
    from memlet import memlet_wiki as mw
    files = dict(_source_lines(root))  # discovered instruction files: one containment rule, bounded reads
    for rel, text in mw.iter_wiki_pages(root):  # contained, size-capped, at most MAX_WIKI_PAGES pages
        if mw._is_generated_page(rel, text) or rel == mw.DREAM_PROMOTED_PAGE or rel in mw._MARKED_GENERATED_PAGES:
            continue
        body = mw._STATUS_BLOCK_RE.sub(mw._blank_lines, mw._PAGE_STATUS_RE.sub(mw._blank_lines, text))
        files[rel] = body.splitlines()
    out: List[Dict[str, Any]] = []
    for rel in sorted(files):
        lines = files[rel]
        masked = _mask("\n".join(lines)).split("\n")
        for a, b, section in _rule_spans(lines):
            raw = "\n".join(lines[a - 1:b])
            out.append({"topic": section or rel.rsplit("/", 1)[-1], "importance": 2, "source": f"{rel}:{a}-{b}",
                        "quote": "\n".join(masked[a - 1:b]), "lines": b - a + 1, "sha256": _sha(raw), "parsed": True})
            if len(out) >= MAX_PARSED:
                return out
    return out


def _entries_meta(root: Path, fallback: bool = False
                  ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Verified index entries plus promoted Dream lessons (each re-verified on every call by `memlet_bridge`:
    unedited line, still approved for the same content, transcript citation unchanged), and the index meta.
    `fallback=True` and no index page: the parsed instruction spans (`parsed_rule_entries`) stand in for the index."""
    from memlet.memlet_brief import _mask
    from memlet.memlet_index import INDEX_PAGE, load_verified_index
    fresh, stale, meta = load_verified_index(root)
    fresh, stale = fresh[:MAX_ENTRIES], stale[:MAX_ENTRIES]
    if fallback and not (root / INDEX_PAGE).is_file():
        fresh = parsed_rule_entries(root)
    from memlet.memlet_bridge import promoted_rule_entries
    p_fresh, p_stale = promoted_rule_entries(root)
    fresh, stale = fresh + p_fresh[:MAX_PROMOTED], stale + p_stale[:MAX_PROMOTED]
    import hashlib
    for e in fresh + stale:  # topics and paths come from the index page / file names: mask what is displayed
        e["id"] = e["source"]  # verified citation: private identity for membership, exclusion and de-duplication
        e["key"] = hashlib.sha256(e["id"].encode("utf-8")).hexdigest()[:16]  # opaque, collision-safe public handle
        e["topic"] = _mask(e["topic"])
        e["source"] = _mask(e["source"])  # display only
        if "quote" in e and e.get("dreamId"):
            e["quote"] = _mask(e["quote"])
    return fresh, stale, meta


def _entries(root: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    fresh, stale, _ = _entries_meta(root)
    return fresh, stale


def _stale_line(stale: List[Dict[str, Any]]) -> str:
    return (f"- WARNING: {len(stale)} indexed rule(s) changed since the index was built; they are not quoted — re-read: "
            + "; ".join(f"`{e['source']}`" for e in stale[:3]))


def _index_stale_line(meta: Optional[Dict[str, Any]]) -> Optional[str]:
    """Warning when instruction files changed after the index was built (new rules there are not indexed yet)."""
    st = (meta or {}).get("staleness") or {}
    if not st.get("stale"):
        return None
    from memlet.memlet_brief import _mask
    parts = []
    for key, label in (("newFiles", "new"), ("changedFiles", "changed"), ("removedFiles", "removed")):
        if st.get(key):
            parts.append(f"{label}: " + ", ".join(f"`{_mask(f)}`" for f in st[key][:3])
                         + (f" (+{len(st[key]) - 3})" if len(st[key]) > 3 else ""))
    detail = "; ".join(parts) or "instruction text differs from what was indexed"
    return (f"- WARNING: instruction files changed after the rule index was built ({detail}); rules added since are "
            "not indexed — re-run `memlet admin index-build` (calls the `claude` CLI; a model call)")


def _quote_block(e: Dict[str, Any]) -> str:
    status = " " + suggested_label(e) + f"[{e['anchorStatus']}: {e['anchorReason']}]" if "anchorStatus" in e else ""
    return "\n".join([f"- [{e['topic']}] `{e['source']}`{status}:"] + [f"  > {q}" for q in e["quote"].splitlines()])


AUTO_FULL_THRESHOLD = 8000  # spec-009/010: below this, the whole instructions beat any tiering on accuracy and cost


def _full_instructions(root: Path) -> Tuple[str, int]:
    from memlet.memlet_brief import _mask, _source_lines
    from memlet.memlet_wiki import estimate_tokens
    files = _source_lines(root)  # discovered instruction files, backup/run-output copies excluded
    text = "\n".join(f"===== {_mask(rel)} =====\n" + _mask("\n".join(lines)) for rel, lines in files.items())
    return text, estimate_tokens(text)


def build_context(repo_path=".", *, core_budget: int = 600, index_budget: int = 600, auto: bool = False,
                  threshold: int = AUTO_FULL_THRESHOLD) -> Dict[str, Any]:
    """auto=True: if the (backup-excluded) instructions fit in `threshold` tokens, return them whole (masked) plus
    the index's change warnings; otherwise the tiered T0+T1 context below."""
    if auto:
        from memlet.memlet_wiki import _resolve_root, estimate_tokens
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
    from memlet.memlet_wiki import _resolve_root, estimate_tokens
    root = _resolve_root(repo_path)
    fresh, stale, meta = _entries_meta(root)
    index_warning = _index_stale_line(meta)
    core_head = "## Project rules — safety core (quoted verbatim; source unchanged since the index was built)"
    index_head = ("## Rule index — BEFORE acting on any of these topics, read the cited lines "
                  "(not listing a rule here does not mean it does not exist)")
    total = max(0, int(core_budget)) + max(0, int(index_budget))
    fixed = [core_head, index_head] + ([_stale_line(stale)] if stale else []) + ([index_warning] if index_warning else [])
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
        if index_warning:
            lines.append(index_warning)
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
    return _recall(repo_path, query, k=k, exclude=exclude, safety=safety)


def recall_items(repo_path=".", query: str = "", *, k: int = 3, exclude: Iterable[str] = (),
                 safety: bool = False, fallback: bool = False) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """`recall` plus the picked entries as items (key, source, topic, quote, importance, match[, dreamId, sha256,
    parsed]) for callers that lay out their own budget (`memlet_bridge.unified_recall`). `match` is True for a query
    match and False for an always-on importance-1 rule added by `safety`. `fallback=True`: without a rule index, rank
    the parsed instruction spans instead (`parsed_rule_entries`). The `recall` result itself is unchanged."""
    return _recall(repo_path, query, k=k, exclude=exclude, safety=safety, with_items=True, fallback=fallback)


def _recall(repo_path=".", query: str = "", *, k: int = 3, exclude: Iterable[str] = (), safety: bool = False,
            with_items: bool = False, fallback: bool = False):
    from memlet.memlet_wiki import _resolve_root, estimate_tokens
    root = _resolve_root(repo_path)
    fresh, stale, meta = _entries_meta(root, fallback=fallback)
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
    matched = {e["id"] for e in picked}
    if safety:  # every importance-1 rule stays reachable: those not already shown are added regardless of score
        shown = {e["id"] for e in picked}
        forced = [e for e in fresh if e["importance"] == 1 and not out_of(e) and e["id"] not in shown]
        picked = picked + forced[:SAFETY_EXTRA]
    from memlet.memlet_wiki import classify_rule_quotes
    classify_rule_quotes(root, picked)
    all_picked = picked
    picked = [e for e in picked if e["anchorStatus"] != "stale"]
    lines = ["## Rules relevant to this task (quoted verbatim from the instructions)", _GUIDE]
    lines += [_quote_block(e) for e in picked] or ["- (no indexed rule matched; consult the rule index)"]
    stale_anchors = [e for e in all_picked if e["anchorStatus"] == "stale"]
    if stale_anchors:
        lines.append("- WARNING: anchored code changed — do not apply without re-reading the rule: " +
                     "; ".join(e["source"] + " (" + e["anchorReason"] + ")" for e in stale_anchors))
    if stale:
        lines.append(_stale_line(stale))
    index_warning = _index_stale_line(meta)
    if index_warning:
        lines.append(index_warning)
    text = "\n".join(lines) + "\n"
    res = {"ok": True, "command": "recall", "repoPath": str(root), "k": k, "stale": len(stale),
           "spans": [e["source"] for e in picked], "spanKeys": [e["key"] for e in picked],
           "scores": [round(-s, 4) for s, _, e in scored[:k] if e in picked], "tokenEstimate": estimate_tokens(text),
           "formatted": text}
    if index_warning:  # additive, only when the index is outdated: the CLI's golden key set stays unchanged otherwise
        res["indexStale"] = (meta or {}).get("staleness")
    if with_items:
        res["staleAnchors"] = len({a["anchor"] for e in all_picked for a in e["anchors"] if a["status"] == "stale"})
        items = []
        for e in all_picked:
            item = {"key": e["key"], "source": e["source"], "topic": e["topic"], "quote": e["quote"],
                    "importance": e["importance"], "match": e["id"] in matched,
                    "anchorStatus": e["anchorStatus"], "anchorReason": e["anchorReason"], "anchors": e["anchors"]}
            for extra in ("dreamId", "sha256", "parsed"):
                if e.get(extra):
                    item[extra] = e[extra]
            items.append(item)
        return res, items
    return res
