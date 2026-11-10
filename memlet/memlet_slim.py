"""`memlet slim-plan`: propose a slimmer always-loaded instruction file by MOVING (never rewriting) old content.

    memlet slim-plan <project> --out <new-empty-dir> [--keep-recent 2] [--split-rules]

Reads `<project>/AGENTS.md` only. Writes nothing into the project: the proposal (new AGENTS.md, a directives log, and
with --split-rules a rules file) plus PROOF.json and ROLLBACK.md go to --out, which must be a new or empty directory
that is neither inside nor above the project.

Layout recognised: a preamble of dated directive blocks (consecutive non-blank lines, newest first) before the first
Markdown heading, followed by the standing rules. The newest `keep_recent` blocks and (unless --split-rules) the
standing rules stay in AGENTS.md; every other block moves, in order, to the log; AGENTS.md gains an index line per
moved block plus a read-before-acting note. Which old directive is still in force is left to the owner.

Preservation proof (provenance, not a multiset): every output line is either a copy of one source line, recorded by
its index, or a generated line. The proof checks that every source line index - blank and whitespace-only lines
included - is used exactly once, that indices increase within each file, that each copy is byte-identical, and that
the serialized files equal the recorded mapping. Generated lines are listed in PROOF.json.
"""

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOG_REL = "management/agent-directives-log.md"
RULES_REL = "management/agent-rules.md"
# First line of each moved-content file. It is the ownership proof Memlet's instruction discovery requires before it
# treats `management/agent-*.md` as agent instructions (memlet_wiki.slim_output_files); a foreign file is never followed.
LOG_HEADER = "# Agent directives log (moved verbatim from AGENTS.md by memlet slim-plan)"
RULES_HEADER = "# Standing rules (moved verbatim from AGENTS.md by memlet slim-plan)"
SLIM_OUTPUTS = {LOG_REL: LOG_HEADER, RULES_REL: RULES_HEADER}
_HEADING_RE = re.compile(r"^#{1,6}\s")
_TITLE_RE = re.compile(r"^\s*>?\s*\*\*(?P<t>.+?)\*\*")
TITLE_MAX = 120

GUIDE = [
    "> **Memlet slim layout** — 오래된 지시는 아래 색인의 파일로 원문 그대로 옮겨졌어요. 색인 주제가 작업과 관련되면 "
    "행동하기 전에 해당 줄을 반드시 읽고, 지시가 서로 다르면 더 최근 지시를 따르세요. 색인에 없다고 규칙이 없는 것은 "
    "아니에요. (Older directives were moved verbatim to the indexed file; read the cited lines before acting on a "
    "listed topic; newer directives win.)",
    "",
]

Slot = Tuple[str, Any]  # ("src", source_index) or ("gen", text)


def _mask(text: str) -> str:
    from memlet.memlet_brief import _mask as m
    return m(text)


def _clean_title(raw: str) -> str:
    # mask the WHOLE original text first; only then normalise and shorten (shortening first can cut a secret below
    # the recogniser's minimum length and leak its prefix)
    t = _mask(raw)
    t = re.sub(r"[`<>]|\s+", " ", t).strip()
    return t if len(t) <= TITLE_MAX else t[:TITLE_MAX - 1].rstrip() + "…"


def _segments(lines: List[str]) -> Tuple[List[Tuple[int, int]], int, int]:
    """Directive segments before the first heading as [start, end) spans, each = one block plus the blank lines that
    follow it; the leading-blank span length; and the first heading index."""
    first_heading = next((i for i, l in enumerate(lines) if _HEADING_RE.match(l)), len(lines))
    i = 0
    while i < first_heading and not lines[i].strip():
        i += 1
    lead = i
    segs = []
    while i < first_heading:
        j = i
        while j < first_heading and lines[j].strip():
            j += 1
        k = j
        while k < first_heading and not lines[k].strip():
            k += 1
        segs.append((i, k))
        i = k
    return segs, lead, first_heading


def _sections(lines: List[str], start: int) -> List[Tuple[int, int]]:
    heads = [i for i in range(start, len(lines)) if _HEADING_RE.match(lines[i])]
    if heads and heads[0] > start:
        heads = [start] + heads
    elif not heads and start < len(lines):
        heads = [start]
    return [(h, heads[k + 1] if k + 1 < len(heads) else len(lines)) for k, h in enumerate(heads)]


def plan(lines: List[str], keep_recent: int = 2, split_rules: bool = False) -> Dict[str, Any]:
    if keep_recent < 0:
        return {"ok": False, "why": "--keep-recent must be >= 0"}
    segs, lead, first_heading = _segments(lines)
    if len(segs) <= keep_recent and not split_rules:
        return {"ok": False, "why": "nothing to move (no dated directive blocks beyond the kept ones)"}
    keep, move = segs[:keep_recent], segs[keep_recent:]
    log: List[Slot] = [("gen", LOG_HEADER), ("gen", "")]
    index: List[Slot] = []
    for s, e in move:
        a = len(log) + 1
        log += [("src", i) for i in range(s, e)]
        last_nonblank = max(i for i in range(s, e) if lines[i].strip())
        b = a + (last_nonblank - s)
        m = _TITLE_RE.match(lines[s])
        index.append(("gen", f"- {_clean_title(m.group('t') if m else lines[s].lstrip('> '))} → `{LOG_REL}:{a}-{b}`"))
    agents: List[Slot] = [("src", i) for i in range(0, lead)] + [("gen", g) for g in GUIDE]
    for s, e in keep:
        agents += [("src", i) for i in range(s, e)]
    agents += [("gen", "## Directive index (older directives, verbatim in the log)")] + index + [("gen", "")]
    files: Dict[str, List[Slot]] = {"AGENTS.md": agents, LOG_REL: log}
    if split_rules and first_heading < len(lines):
        rules: List[Slot] = [("gen", RULES_HEADER), ("gen", "")]
        rindex: List[Slot] = []
        for s, e in _sections(lines, first_heading):
            a = len(rules) + 1
            rules += [("src", i) for i in range(s, e)]
            nonblank = [i for i in range(s, e) if lines[i].strip()]
            b = a + ((nonblank[-1] if nonblank else s) - s)
            # hoisted out of the f-string: a backslash inside an f-string expression is a SyntaxError before 3.12
            heading_text = re.sub(r"^#+\s*", "", lines[s])
            rindex.append(("gen", f"- {_clean_title(heading_text)} → `{RULES_REL}:{a}-{b}`"))
        agents += [("gen", "## Standing rules index (verbatim in the rules file)")] + rindex + [("gen", "")]
        files[RULES_REL] = rules
    else:
        agents += [("src", i) for i in range(first_heading, len(lines))]
    return {"ok": True, "files": files, "keepRecent": keep_recent, "splitRules": split_rules,
            "moved": len(move), "kept": len(keep)}


def render(lines: List[str], slots: List[Slot]) -> List[str]:
    return [lines[v] if kind == "src" else v for kind, v in slots]


def proof(lines: List[str], p: Dict[str, Any], written: Optional[Dict[str, List[str]]] = None,
          written_bytes: Optional[Dict[str, bytes]] = None, trailing: bool = True) -> Dict[str, Any]:
    used: Dict[int, str] = {}
    duplicates, order_ok, byte_ok = [], True, True
    for rel, slots in p["files"].items():
        last = -1
        for kind, v in slots:
            if kind != "src":
                continue
            if v in used:
                duplicates.append(v)
            used[v] = rel
            if v <= last:
                order_ok = False
            last = v
    missing = [i for i in range(len(lines)) if i not in used]
    serialized_ok = True
    if written is not None:
        for rel, slots in p["files"].items():
            if written.get(rel) != render(lines, slots):
                serialized_ok = False
    if written_bytes is not None:  # the whole file, byte for byte: no extra suffix, no lost final newline
        for rel, slots in p["files"].items():
            expect = ("\n".join(render(lines, slots)) + ("\n" if trailing else "")).encode("utf-8")
            if written_bytes.get(rel) != expect:
                serialized_ok = False
    generated = [v for slots in p["files"].values() for kind, v in slots if kind == "gen"]
    ok = not missing and not duplicates and order_ok and byte_ok and serialized_ok
    return {"ok": ok, "sourceLines": len(lines), "sourceLinesMissing": len(missing), "sourceLinesDuplicated": len(duplicates),
            "orderPreserved": order_ok, "serializedMatchesMapping": serialized_ok, "generatedLines": generated,
            "map": {rel: _ranges([v for k, v in slots if k == "src"]) for rel, slots in p["files"].items()}}


def _ranges(idx: List[int]) -> List[List[int]]:
    out: List[List[int]] = []
    for i in idx:
        if out and out[-1][1] == i:
            out[-1][1] = i + 1
        else:
            out.append([i, i + 1])
    return [[a + 1, b] for a, b in out]  # 1-based inclusive source line ranges


def _safe_out(project: Path, out: Path) -> Optional[str]:
    if out.is_symlink():
        return "--out must not be a symlink"
    proj, dst = project.resolve(), out.resolve()
    if dst == proj or proj in dst.parents or dst in proj.parents:
        return "--out must be neither inside nor above the project (the project is never written)"
    if dst.exists() and (not dst.is_dir() or any(dst.iterdir())):
        return "--out must be a new or empty directory"
    return None


def _write_new(base: Path, rel: str, data: str) -> None:
    """Create rel under base exclusively, never following symlinks (base was verified new/empty and is ours)."""
    parts = Path(rel).parts
    d = base
    for part in parts[:-1]:
        d = d / part
        if d.is_symlink():
            raise OSError(f"refusing symlinked directory {d}")
        d.mkdir(exist_ok=True)
    fd = os.open(d / parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)


def slim_plan(project, out, *, keep_recent: int = 2, split_rules: bool = False) -> Dict[str, Any]:
    from memlet.memlet_wiki import estimate_tokens
    project, out = Path(project), Path(out)
    src = project / "AGENTS.md"
    if not src.is_file() or src.is_symlink():
        return {"ok": False, "command": "slim-plan", "why": "AGENTS.md not found (or a symlink)"}
    bad = _safe_out(project, out)
    if bad:
        return {"ok": False, "command": "slim-plan", "why": bad}
    raw = src.read_bytes()
    text = raw.decode("utf-8")
    lines = text.split("\n")
    trailing = lines[-1] == ""
    if trailing:
        lines = lines[:-1]
    p = plan(lines, keep_recent=keep_recent, split_rules=split_rules)
    if not p["ok"]:
        return {"ok": False, "command": "slim-plan", "why": p["why"]}
    rendered = {rel: render(lines, slots) for rel, slots in p["files"].items()}
    pr = proof(lines, p, written=rendered)
    collisions = [rel for rel in p["files"] if rel != "AGENTS.md" and os.path.lexists(project / rel)]  # dangling links too
    out.mkdir(parents=True, exist_ok=True)
    for rel, flines in rendered.items():
        _write_new(out, rel, "\n".join(flines) + ("\n" if trailing else ""))
    reread = {rel: (out / rel).read_bytes() for rel in rendered}
    pr = proof(lines, p, written_bytes=reread, trailing=trailing)
    pr["originalSha256"] = hashlib.sha256(raw).hexdigest()
    tokens = {rel: estimate_tokens("\n".join(fl)) for rel, fl in rendered.items()}
    report = {"ok": pr["ok"] and not collisions, "command": "slim-plan", "project": str(project.resolve()),
              "out": str(out.resolve()), "keepRecent": keep_recent, "splitRules": split_rules,
              "movedBlocks": p["moved"], "keptBlocks": p["kept"], "proof": pr,
              "applyCollisions": collisions, "tokens": {"original": estimate_tokens(text), **tokens},
              "alwaysLoadedRatio": round(tokens["AGENTS.md"] / max(1, estimate_tokens(text)), 3)}
    if collisions:
        report["why"] = "the project already has " + ", ".join(collisions) + "; applying would overwrite it"
    new_files = [r for r in rendered if r != "AGENTS.md"]
    _write_new(out, "PROOF.json", json.dumps(report, ensure_ascii=False, indent=1))
    _write_new(out, "ROLLBACK.md",
               "# Rollback\n\nBefore applying, keep a byte-exact copy of the current file:\n\n"
               f"    cp AGENTS.md AGENTS.md.pre-slim-{pr['originalSha256'][:12]}\n\n"
               "To roll back: restore that copy over AGENTS.md and delete only the files this proposal introduced "
               f"({', '.join(new_files)}; none of them existed before — applying is refused if one does). Verify with "
               f"`shasum -a 256 AGENTS.md` = {pr['originalSha256']}.\n")
    return report
