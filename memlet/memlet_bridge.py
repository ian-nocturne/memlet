"""Memlet storage bridge: one recall over the three stores, Dream -> wiki promotion, per-anchor accept, changed-file check.

Memlet keeps three stores: the `.memlet/` wiki + ledger (anchored rules), the bi-temporal memory store
(`StandaloneStore`) and the Dream approval queue under `.memlet/dream/`. This module connects them without a model
call and without weakening any existing gate:

* `unified_recall` returns rules (BM25 over the verified rule index, `memlet_recall.recall`), approved and
  pin-verified Dream lessons (`memlet_dream.run_dream_recall` semantics: never PENDING, rejected or unverified) and,
  when a store and task are given, task memory, in one budgeted response. A missing store is reported in `sources`,
  never raised.
* `learn_from_transcript` / `learn_pending` / `learn_approve` / `learn_reject` wrap the Dream library with the fixed
  in-repo location `.memlet/dream/` (queue `APPROVAL-QUEUE.json`, DB `ontology.sqlite3`, line-addressable transcript
  text under `snapshot/`). The snapshot holds only what the transcript reader extracted (user/assistant visible
  text for `.jsonl`; `.log` files are taken as already-extracted lines), secret-masked. Approval always re-verifies
  pins against that snapshot, passed explicitly: no snapshot or DB path is ever taken from the queue JSON.
* `promote_approved` writes approved, pin-verified lessons as ordinary wiki rules into `.memlet/wiki/dream-approved.md`
  so `handover` reads them like any other wiki page; `memlet_recall` re-verifies them on every recall. A human edit to
  that page is detected through the ledger and never overwritten.
* `accept_anchor` re-pins exactly the named anchors; `changed_check` checks only rules that touch the changed files.

Every function returns a JSON-serialisable dict with `ok`; failures carry `error` (and `exitCode` where it matters)
and `formatted` holds a Markdown rendering.
"""

from __future__ import annotations

from memlet.memlet_dream import call_foreign as _call_foreign, _foreign_context

from .vocabulary import (APPROVED as TIER_APPROVED, HINT as TIER_HINT, PENDING as TIER_PENDING, REJECTED as TIER_REJECTED)
from .vocabulary import (suggested_label, HINT_LABEL, HINT_HELP, MODEL_AGREEMENT, VENDOR_AGREEMENT,
                         VETO_PREFIX, VETO_TEMPLATE, VERDICT_HELP, TIERS, SAME_VENDOR_REVIEW,
                         APPROVAL_PATHS_LABEL, INACTIVE_APPROVAL_PATHS, APPROVAL_PATHS_SUFFIX,
                         LESSON_VIA_SCOPE, LESSON_VIA_PATH)

import contextlib
import contextvars
import copy
import hashlib
import json
import os
import re
import selectors
import time
import unicodedata
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from memlet import memlet_dream as md
from memlet import memlet_wiki as mw

DREAM_DIR = ".memlet/dream"
QUEUE_REL = ".memlet/dream/APPROVAL-QUEUE.json"
DB_REL = ".memlet/dream/ontology.sqlite3"
SNAPSHOT_REL = ".memlet/dream/snapshot"
PROMOTED_PAGE = mw.DREAM_PROMOTED_PAGE  # ".memlet/wiki/dream-approved.md"
PROMOTED_SCHEMA = "memlet-dream-promoted/v1"
_PAGE_META = "<!-- memlet:dream-page "
_RULE_MARK_RE = re.compile(
    r"^\s*<!-- memlet:dream id=(?P<id>[A-Za-z0-9_.:-]{1,64}) digest=(?P<digest>sha256:[0-9a-f]{64}) "
    r"text=(?P<text>[0-9a-f]{16}) -->\s*$")
_BULLET_LINE_RE = re.compile(r"^- (?P<body>\S.*)$")
_MEMLET_PAGE_EVENTS = ("dream_promote", "anchor_accept", "pin_migration")
_LESSON_HEADING = "## Approved lessons"
_DEAD_END_HEADING = "## Dead-ends (approved Dream pitfalls with no code anchor; do not retry)"
_TRANSCRIPT_SUFFIXES = (".jsonl", ".log")
# Input bounds of `learn_from_transcript`, so every caller (CLI, hooks, MCP) does bounded work on any input. Anything
# over a bound is skipped whole and counted in the result's `skipped`; nothing is ever stored partially.
TRANSCRIPT_MAX_FILE_BYTES = 16 * 1024 * 1024   # a larger file is not read at all
TRANSCRIPT_MAX_TOTAL_BYTES = 64 * 1024 * 1024  # all files of one call together; later files are skipped
TRANSCRIPT_MAX_LINE_CHARS = 4 * 1024 * 1024    # one physical transcript line (a `.jsonl` record): never parsed
TRANSCRIPT_MAX_TEXT_CHARS = 16 * 1024          # one visible message / `.log` line: becomes an empty line
TRANSCRIPT_MAX_WORK_CHARS = 8 * 1024 * 1024    # visible text kept from all files of one call; later files skipped
_MAX_TRANSCRIPT_FILES = 200
_MAX_BASENAME_WALK = 20000
_SPLITLINES_CHARS_RE = re.compile("[\r\n\x0b\x0c\x1c\x1d\x1e\x85  ]")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_SECTIONS = ("rules", "safety", "staleRules", "dream", "memory", "hints")
_STATUS_ORDER = ("stale", "compatible-unverified", "unpinned", "path-exists", "active")


# ---------------------------------------------------------------------------------------------------------------
# small helpers


def _fail(error: str, exit_code: int = 1, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "error": error, "exitCode": exit_code}
    out.update(extra)
    out.setdefault("formatted", f"- ERROR: {error}\n")
    return out


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_DIR_RELS = (mw.MEMLET_DIR, mw.WIKI_DIR, DREAM_DIR, SNAPSHOT_REL)
_DB_FILES = (DB_REL, DB_REL + "-journal", DB_REL + "-wal", DB_REL + "-shm")


def _symlinked(root: Path, rels: Iterable[str]) -> Optional[str]:
    """Why the first of `rels` is not a safe Memlet location (a symlinked component, a non-regular file or a
    non-directory where a directory is expected, or a location outside the repository), else None. The same check
    (`memlet_wiki.managed_path_problem`) that every Memlet-managed write applies."""
    for rel in rels:
        problem = mw.managed_path_problem(root, rel, leaf="dir" if rel in _DIR_RELS else "file")
        if problem:
            return problem
    return None


def _refusing(problem: str) -> str:
    return problem if problem.startswith("refusing") else f"refusing {problem}"


def _ids(ids: Any) -> List[str]:
    if isinstance(ids, str):
        ids = [ids]
    out: List[str] = []
    for raw in ids or ():
        s = str(raw).strip()
        if s and s not in out:
            out.append(s)
    return out


def _gitignore_notice(root: Path) -> Optional[str]:
    """Guidance (never an edit) when `.memlet/` is not git-ignored in a git repository."""
    if not (root / ".git").exists():
        return None
    try:
        rc, _, _ = md._run_owned_git(["git", "check-ignore", "-q", "--no-index", ".memlet/dream/APPROVAL-QUEUE.json"],
                                    cwd=str(root), capture_output=True, text=True, timeout=10, _run=subprocess.run)
        if rc == 0:
            return None
        if rc == 1:
            return ("`.memlet/` is not git-ignored: the Dream queue and transcript snapshot hold conversation text. "
                    "Add `.memlet/` to `.gitignore` (Memlet does not edit it for you).")
    except (OSError, subprocess.SubprocessError):
        pass
    text = mw._read_text(root / ".gitignore") if (root / ".gitignore").is_file() else ""
    covered = {".memlet", ".memlet/", "/.memlet", "/.memlet/", ".memlet/*", "/.memlet/*", ".memlet/**", "/.memlet/**"}
    if any(line.strip() in covered for line in text.splitlines()):
        return None
    return ("`.memlet/` does not appear in `.gitignore`: the Dream queue and transcript snapshot hold conversation "
            "text. Add `.memlet/` to `.gitignore` (Memlet does not edit it for you).")


def _snapshot_digest(snap_files: Optional[Dict[str, List[str]]]) -> Optional[str]:
    if snap_files is None:
        return None
    h = hashlib.sha256()
    for k in sorted(snap_files):
        h.update(k.encode("utf-8", "surrogateescape") + b"\0")
        for ln in snap_files[k]:
            h.update(ln.encode("utf-8", "surrogateescape") + b"\n")
        h.update(b"\0")
    return h.hexdigest()


def _load_queue(root: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """(graph or None, reason). Refuses a symlinked queue (raises ValueError through `memlet_dream._load_queue`)."""
    rkey = Path(root).resolve()
    val = md._read_cache("loaded_queues").get(rkey, md._CACHE_MISS)
    if val is not md._CACHE_MISS:
        if isinstance(val, Exception):
            raise val
        cached_graph, cached_why = val
        return (md._shallow_graph_for_approval(cached_graph) if cached_graph is not None else None, cached_why)
    q = root / QUEUE_REL
    if not q.exists() and not q.is_symlink():
        out = (None, "no Dream queue (.memlet/dream/APPROVAL-QUEUE.json); run `learn` first")
        md._read_cache("loaded_queues")[rkey] = out
        md._read_cache("queue_digests")[rkey] = "absent"
        return out
    try:
        _q, graph = md._load_queue(q)
    except (OSError, ValueError) as exc:
        md._read_cache("loaded_queues")[rkey] = exc
        raise
    if md._active_read_state() is not None:
        md._read_cache("loaded_queues")[rkey] = (graph, "")
        md._read_cache("queue_digests")[rkey] = md.graph_sha256(graph)
        return (md._shallow_graph_for_approval(graph), "")
    return (graph, "")


def _load_snapshot(root: Path) -> Tuple[Optional[Dict[str, List[str]]], str]:
    """The bridge's fixed in-repo snapshot (`.memlet/dream/snapshot/`), never a path read from the queue."""
    rkey = Path(root).resolve()
    cached = md._read_cache("loaded_snapshots").get(rkey, md._CACHE_MISS)
    if cached is not md._CACHE_MISS:
        cached_snap, cached_why = cached
        if cached_snap is None:
            return None, cached_why
        return {k: list(v) for k, v in cached_snap.items()}, cached_why
    snap = root / SNAPSHOT_REL
    if _symlinked(root, (SNAPSHOT_REL,)):
        out = (None, "refusing symlinked snapshot directory")
    elif not snap.is_dir():
        out = (None, "no transcript snapshot (.memlet/dream/snapshot/)")
    else:
        try:
            out = (md.load_snapshot(snap), "")
        except (OSError, ValueError) as exc:
            out = (None, f"snapshot unreadable: {exc}")
    if md._active_read_state() is not None:
        md._read_cache("loaded_snapshots")[rkey] = out
        md._read_cache("snapshot_digests")[rkey] = _snapshot_digest(out[0]) or ("err:" + out[1])
        if out[0] is not None:
            return {k: list(v) for k, v in out[0].items()}, out[1]
    return out


def _node_edges_verify(node: Dict[str, Any], snap: Dict[str, List[str]]) -> Optional[str]:
    err = md._verify_item_pins(node, "node", snap)
    if err:
        return err
    for e in node.get("edges") or []:
        if isinstance(e, dict):
            err = md._verify_item_pins(e, "edge", snap)
            if err:
                return err
    return None


# ---------------------------------------------------------------------------------------------------------------
# learn: transcripts -> PENDING queue


def _transcript_files(src: Path) -> Tuple[List[Path], Dict[str, int]]:
    skipped = {"symlinks": 0, "unsupported": 0, "outsideDirectory": 0, "overLimit": 0}
    if src.is_symlink():
        skipped["symlinks"] += 1
        return [], skipped
    if src.is_file():
        if src.suffix in _TRANSCRIPT_SUFFIXES:
            return [src.resolve()], skipped
        skipped["unsupported"] += 1
        return [], skipped
    base = src.resolve()
    found: List[Path] = []
    for p in sorted(set(base.rglob("*.jsonl")) | set(base.rglob("*.log"))):
        if p.is_symlink():
            skipped["symlinks"] += 1
            continue
        if not p.is_file():
            continue
        try:
            p.resolve().relative_to(base)
        except ValueError:
            skipped["outsideDirectory"] += 1
            continue
        if len(found) >= _MAX_TRANSCRIPT_FILES:
            skipped["overLimit"] += 1
            continue
        found.append(p.resolve())
    return found, skipped


def _transcript_lines(f: Path) -> Tuple[List[str], Dict[str, int]]:
    """Line N of the result is line N of the transcript (`.jsonl`: visible user/assistant text only, via
    `memlet_dream.read_jsonl_transcript`). Characters `str.splitlines` would split on are folded to spaces so the
    stored snapshot reads back with the same numbering; every line is secret-masked before it is stored. Lines and
    messages over the bounds above become empty lines (counted), so no part of them is stored."""
    if f.suffix == ".jsonl":
        lines, stats = md.read_jsonl_transcript(f, max_line_chars=TRANSCRIPT_MAX_LINE_CHARS,
                                                max_text_chars=TRANSCRIPT_MAX_TEXT_CHARS)
    else:
        # whole-text redaction BEFORE splitting: a key body between its markers is on lines with no marker of its own
        raw = mw.read_regular_text(f, TRANSCRIPT_MAX_FILE_BYTES, errors="replace")
        if raw is None:
            raise ValueError("transcript is not a bounded regular file")
        lines = md.redact_secret_blocks(raw).splitlines()
        stats = {"overlongLines": sum(1 for ln in lines if len(ln) > TRANSCRIPT_MAX_TEXT_CHARS)}
        if stats["overlongLines"]:
            lines = ["" if len(ln) > TRANSCRIPT_MAX_TEXT_CHARS else ln for ln in lines]
    if f.suffix != ".jsonl":
        for i, line in enumerate(lines):
            if md.memlet_echo(line):
                lines[i] = ""
                stats["skippedMemletEcho"] = stats.get("skippedMemletEcho", 0) + 1
    return [md.mask_secrets(_SPLITLINES_CHARS_RE.sub(" ", ln)) for ln in lines], stats


def _bounded_files(files: List[Path], skipped: Dict[str, Any]) -> List[Path]:
    """Drop files over the per-file size bound, and every file once the total bound is reached (counted)."""
    kept: List[Path] = []
    total = 0
    skipped.setdefault("overSizeFiles", 0)
    skipped.setdefault("overTotalSizeFiles", 0)
    for f in files:
        try:
            size = f.stat().st_size
        except OSError:
            skipped["unsupported"] = skipped.get("unsupported", 0) + 1
            continue
        if size > TRANSCRIPT_MAX_FILE_BYTES:
            skipped["overSizeFiles"] += 1
            continue
        if total + size > TRANSCRIPT_MAX_TOTAL_BYTES:
            skipped["overTotalSizeFiles"] += 1
            continue
        total += size
        kept.append(f)
    return kept


def _write_text_atomic(target: Path, text: str) -> None:
    """Every bridge write below `.memlet/` (snapshot files, promoted page, accepted config and pages): the shared
    checked helper (`memlet_wiki.write_managed_path`): refused when any component is a symlink or not a regular
    file / real directory, temp file in the same directory, atomic replace, a new file is private (0600)."""
    mw.write_managed_path(target, text, new_mode=0o600)


def _store_snapshot(snap_dir: Path, f: Path, lines: List[str]) -> Tuple[str, str]:
    """Write the transcript text under the snapshot dir; returns (file id, op). The same transcript always maps to the
    same file; an appended transcript extends it (existing line numbers, hence pins, are unchanged). A transcript
    whose earlier lines changed gets a separate content-addressed file, so pins into the old text stay verifiable."""
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", f.name)[:60].strip(".") or "transcript"
    base = f"{stem}-{_sha(str(f))[:10]}"
    data = "\n".join(lines) + ("\n" if lines else "")
    primary = snap_dir / f"{base}.log"
    if not primary.exists() and not primary.is_symlink():
        _write_text_atomic(primary, data)
        return primary.name, "create"
    old_text = mw.read_regular_text(primary, md.SNAPSHOT_MAX_FILE_BYTES, errors="replace")
    if old_text is None:
        raise ValueError(f"refusing snapshot file {primary.name}: not a regular file within the snapshot bound")
    old = old_text.splitlines()
    if old == lines:
        return primary.name, "unchanged"
    if lines[:len(old)] == old:
        _write_text_atomic(primary, data)
        return primary.name, "extend"
    variant = snap_dir / f"{base}-{_sha(data)[:10]}.log"
    if (variant.is_file() and not variant.is_symlink()
            and (mw.read_regular_text(variant, md.SNAPSHOT_MAX_FILE_BYTES, errors="replace") or "").splitlines() == lines):
        return variant.name, "unchanged"
    _write_text_atomic(variant, data)
    return variant.name, "create-variant"


def _stable_candidates(snap_files: Dict[str, List[str]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Deterministic extractor per snapshot file, with content-derived ids (`DRM-<12 hex>`), so re-learning the same
    transcript yields the same ids and adding another transcript never renumbers existing items."""
    out: List[Dict[str, Any]] = []
    rep = {"truncatedFiles": 0, "omittedCandidates": 0}
    for fid in sorted(snap_files):
        ext = md.extract_candidates_with_report({fid: snap_files[fid]})
        if ext["truncated"]:
            rep["truncatedFiles"] += 1
            rep["omittedCandidates"] += int(ext["omittedCount"])
        mapping: Dict[str, str] = {}
        for c in ext["candidates"]:
            key = "\n".join([fid, str(c.get("category")), "|".join(c.get("src_spans") or []),
                             str(c.get("superseded_by_src") or "")])
            mapping[str(c.get("lesson_id"))] = "DRM-" + _sha(key)[:12]
        for c in ext["candidates"]:
            c = dict(c)
            c["lesson_id"] = mapping[str(c.get("lesson_id"))]
            if c.get("superseded_by_id"):
                c["superseded_by_id"] = mapping.get(str(c["superseded_by_id"]), c["superseded_by_id"])
            out.append(c)
    return out, rep


def _edges_match_graph(node: Dict[str, Any], graph_edges: Dict[str, Dict[str, Any]]) -> bool:
    return all(isinstance(e, dict) and graph_edges.get(str(e.get("edgeId"))) == e for e in node.get("edges") or [])


def _merge(old: Optional[Dict[str, Any]], built: Dict[str, Any], snap: Dict[str, List[str]],
           snap_dir: Path) -> Tuple[Dict[str, Any], Dict[str, List[str]]]:
    """Existing nodes (with their approval records) are kept unchanged when the rebuild produces the identical item;
    new items are added PENDING. An existing node whose pins no longer verify is dropped (it can never be recalled)."""
    old_nodes = [n for n in (old or {}).get("nodes") or [] if isinstance(n, dict)]
    old_edges = {str(e.get("edgeId")): e for e in (old or {}).get("edges") or [] if isinstance(e, dict)}
    old_by_id: Dict[str, Dict[str, Any]] = {}
    for n in old_nodes:
        old_by_id.setdefault(str(n.get("id")), n)

    def _intact(n: Dict[str, Any]) -> bool:
        return _node_edges_verify(n, snap) is None and _edges_match_graph(n, old_edges)

    info: Dict[str, List[str]] = {"added": [], "replacedChanged": [], "droppedUnverifiable": []}
    out_nodes: List[Dict[str, Any]] = []
    new_ids = set()
    for n in built.get("nodes") or []:
        nid = str(n.get("id"))
        new_ids.add(nid)
        o = old_by_id.get(nid)
        candidate = dict(n, approvedPaths=o["approvedPaths"]) if o is not None and "approvedPaths" in o else n
        if o is not None and md.node_approval_digest(o) == md.node_approval_digest(candidate) and _intact(o):
            out_nodes.append(o)
        else:
            if o is not None and "merge" in o:
                n = dict(n, merge=copy.deepcopy(o["merge"]))  # changed content lapses; it does not erase the decision
            out_nodes.append(n)
            info["added" if o is None else "replacedChanged"].append(nid)
    for o in old_nodes:
        oid = str(o.get("id"))
        if oid in new_ids:
            continue
        if _intact(o):
            out_nodes.append(o)
        else:
            info["droppedUnverifiable"].append(oid)
    out_edges = [copy.deepcopy(e) for n in out_nodes for e in (n.get("edges") or [])]
    merged = {
        "schema": md.DREAM_ONTOLOGY_SCHEMA,
        "nodes": out_nodes,
        "edges": out_edges,
        "dropped": built.get("dropped") or [],
        "stats": {"pinnedNodeCount": len(out_nodes), "pinnedEdgeCount": len(out_edges),
                  "droppedCount": len(built.get("dropped") or [])},
        "queueMeta": {"snapshotPath": str(snap_dir), "dbPath": Path(DB_REL).name, "approvalRequired": True},
    }
    return merged, info


AGENT_TRANSCRIPT_DIRS = (".codex/sessions", ".claude/projects", ".muse/sessions")


def agent_transcript_roots() -> Tuple[Path, ...]:
    """Folders where coding agents keep session transcripts on this machine.

    `MEMLET_TRANSCRIPT_DIRS` (os.pathsep-separated) replace the defaults. The
    defaults are the usual per-user folders plus `$CLAUDE_CONFIG_DIR/projects`, because a relocated Claude Code
    config directory (managed profiles, dotfile setups) keeps its transcripts there, often as a symlink back to
    `~/.claude/projects`.
    """
    custom = os.environ.get("MEMLET_TRANSCRIPT_DIRS") or ""
    if custom.strip():
        return tuple(Path(p).expanduser() for p in custom.split(os.pathsep) if p.strip())
    roots = [Path.home() / rel for rel in AGENT_TRANSCRIPT_DIRS]
    config_dir = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if config_dir:
        roots.append(Path(config_dir).expanduser() / "projects")
    return tuple(roots)


@md.fresh_read
def transcript_symlink_problem(path: Any) -> Optional[str]:
    """Why a transcript path must be refused because of symbolic links, or None.

    The file itself may never be a symlink. A symlinked folder on the way is accepted only when the real file
    lives inside one of `agent_transcript_roots()`: that is how a relocated agent config directory looks, while a
    link that leads anywhere else is refused, so a path cannot be used to pull in an arbitrary file.
    """
    p = Path(path)
    try:
        if p.is_symlink():
            return "the transcript file is a symbolic link"
        if not any(q.is_symlink() for q in p.parents):
            return None
        real = Path(os.path.realpath(str(p)))
        for root in agent_transcript_roots():
            try:
                real.relative_to(Path(os.path.realpath(str(root))))
                return None
            except ValueError:
                continue
    except OSError:
        return "the transcript path could not be inspected"
    return "a symlink in the transcript path leads outside the known agent transcript folders"


@md.fresh_read
def learn_from_transcript(repo_path=".", transcript_path=None, *, vendor="autonomous-deterministic") -> Dict[str, Any]:
    """Build or extend the PENDING queue `.memlet/dream/APPROVAL-QUEUE.json` (+ `ontology.sqlite3`) from a `.jsonl`
    / `.log` transcript or a directory of them. Nothing is approved; existing approvals are kept untouched."""
    try:
        root = mw._resolve_root(repo_path)
    except ValueError as exc:
        return _fail(str(exc), 2)
    if not transcript_path:
        return _fail("a transcript path (.jsonl / .log file or a directory) is required", 2)
    bad = _symlinked(root, (QUEUE_REL, SNAPSHOT_REL) + _DB_FILES + (PROMOTED_PAGE,) + mw._LEDGER_FILES)
    if bad:
        return _fail(_refusing(bad), 2)
    src = Path(str(transcript_path)).expanduser()
    if not src.exists() and not src.is_symlink():
        return _fail(f"transcript not found: {src}", 2)
    try:
        src_resolved = src.resolve()
        src_resolved.relative_to((root / ".memlet").resolve())
        return _fail("refusing to learn from Memlet's own .memlet/ directory", 2)
    except ValueError:
        pass
    files, skipped = _transcript_files(src)
    if not files:
        return _fail("no .jsonl or .log transcript found (symlinked files are skipped)", 2, skipped=skipped)
    files = _bounded_files(files, skipped)
    if not files:
        return _fail(f"every transcript is over the size bound ({TRANSCRIPT_MAX_FILE_BYTES} bytes per file, "
                     f"{TRANSCRIPT_MAX_TOTAL_BYTES} in total); nothing was read", 2, skipped=skipped)
    q_path = root / QUEUE_REL
    notices: List[str] = []
    try:
        with md.queue_lock(q_path):  # load -> merge -> commit -> promote is one critical section
            res = _learn_locked(root, files, skipped, vendor, notices)
    except (OSError, ValueError, sqlite3.Error) as exc:
        return _fail(f"learn failed: {exc}", 1)
    return res


def _learn_locked(root: Path, files: List[Path], skipped: Dict[str, Any], vendor: str,
                  notices: List[str]) -> Dict[str, Any]:
    snap_dir = root / SNAPSHOT_REL
    q_path = root / QUEUE_REL
    db_path = root / DB_REL
    old, _reason = _load_queue(root)
    if old is not None:
        meta = old.get("queueMeta") if isinstance(old.get("queueMeta"), dict) else {}
        if meta.get("snapshotPath") != str(snap_dir):
            return _fail("the existing .memlet/dream/APPROVAL-QUEUE.json was not built by `learn` (it cites "
                         "another snapshot); move it aside before learning into this repository", 1)
    snap_dir.mkdir(parents=True, exist_ok=True)
    stored, reader = [], {"messages": 0, "malformedLines": 0, "nonMessageLines": 0, "overlongLines": 0,
                          "overlongMessages": 0}
    work, over_work, echo_count = 0, 0, 0
    for f in files:
        if work >= TRANSCRIPT_MAX_WORK_CHARS:
            over_work += 1
            continue
        lines, st = _transcript_lines(f)
        echo_count += st.get("skippedMemletEcho", 0)
        size = sum(len(ln) for ln in lines)
        if work + size > TRANSCRIPT_MAX_WORK_CHARS:
            over_work += 1  # never stored partially
            continue
        work += size
        for k in reader:
            reader[k] += int(st.get(k, 0))
        fid, op = _store_snapshot(snap_dir, f, lines)
        stored.append({"transcript": str(f), "snapshotFile": f"{SNAPSHOT_REL}/{fid}", "op": op})
    snap = md.load_snapshot(snap_dir)
    cands, cap = _stable_candidates(snap)
    if old:
        config, _ = _load_config(root)
        trust = dream_tiers(old, snapshot=snap, config=config)
        approved_quotes = {md._LINE_PREFIX_RE.sub("", line["quote"], count=1).strip()
                           for node in old.get("nodes", []) if trust.get(node["id"], {}).get("tier") == TIER_APPROVED
                           for line in md.lesson_lines(node)}
        old_ids = {n["id"] for n in old.get("nodes", [])}
        kept = []
        for candidate in cands:
            spans = candidate.get("src_spans", [])
            pins = [md._resolve_pin(snap, span) for span in spans]
            quote = "\n".join(md._LINE_PREFIX_RE.sub("", p["quoteText"], count=1).strip() for p in pins if p)
            if candidate["lesson_id"] not in old_ids and quote in approved_quotes:
                echo_count += 1
            else:
                kept.append(candidate)
        cands = kept
    with tempfile.TemporaryDirectory(prefix="memlet-learn-") as tmp:
        cpath = Path(tmp) / "candidates.json"
        cpath.write_text(json.dumps(cands, ensure_ascii=False), encoding="utf-8")
        receipt = md.run_dream_build(str(snap_dir), candidates_path=str(cpath),
                                     out_json=str(Path(tmp) / "queue.json"), vendor=vendor)
        built = json.loads((Path(tmp) / "queue.json").read_text(encoding="utf-8"))
    for node in built.get("nodes", []):
        node["proposers"] = [{"vendor": "keyword", "origin": "untrusted", "isolation": "none"}]
    merged, info = _merge(old, built, snap, snap_dir)
    ver = md._verify_ontology_contract(merged, snap)
    if not ver["ok"]:
        return _fail("merged Dream queue failed the ontology contract; nothing was written",
                     1, contractVerification=ver)
    unchanged = old is not None and all(old.get(k) == merged[k] for k in ("nodes", "edges", "queueMeta"))
    if not unchanged:
        prior_sha = md.graph_sha256(old) if old is not None else None
        md._commit_queue_and_db(q_path, json.dumps(merged, ensure_ascii=False, indent=2) + "\n", db_path, merged,
                                replaces_sha=prior_sha)
    pending = md.list_pending(merged)
    note = _gitignore_notice(root)
    if note:
        notices.append(note)
    if info["droppedUnverifiable"]:
        notices.append(f"{len(info['droppedUnverifiable'])} existing queue item(s) no longer verify against the "
                       "snapshot and were removed: " + ", ".join(info["droppedUnverifiable"][:5]))
    if (root / PROMOTED_PAGE).is_file():  # keep the promoted page in step with what still verifies
        prom = promote_approved(root)
        if not prom.get("ok"):
            notices.append(f"promoted page not refreshed: {prom.get('error')}")
        elif prom.get("removed"):
            notices.append("removed from the promoted page (no longer verifies): " + ", ".join(prom["removed"][:5]))
    skipped.update({"overlongLines": reader["overlongLines"], "overlongMessages": reader["overlongMessages"],
                    "overWorkCapFiles": over_work,
                    "transcriptMalformedLines": reader["malformedLines"],
                    "transcriptNonMessageLines": reader["nonMessageLines"],
                    "droppedCandidates": len(receipt.get("dropped") or []),
                    "truncatedFiles": cap["truncatedFiles"], "omittedCandidates": cap["omittedCandidates"],
                    "replacedChanged": info["replacedChanged"], "droppedUnverifiable": info["droppedUnverifiable"]})
    if echo_count:
        from .vocabulary import MEMLET_ECHO_NOTICE
        skipped["skippedMemletEcho"] = echo_count
        notices.append(MEMLET_ECHO_NOTICE % echo_count)
    bounded = {k: skipped.get(k, 0) for k in ("overSizeFiles", "overTotalSizeFiles", "overWorkCapFiles",
                                              "overlongLines", "overlongMessages") if skipped.get(k)}
    if bounded:
        notices.append("input over the transcript bounds was skipped whole (never stored in part): "
                       + ", ".join(f"{k}={v}" for k, v in bounded.items()))
    res = {
        "ok": True, "exitCode": 0, "command": "learn", "repoPath": str(root), "queuePath": str(q_path),
        "dbPath": str(db_path), "snapshotDir": str(snap_dir), "snapshotFiles": stored, "written": not unchanged,
        "addedNodeIds": info["added"], "pendingCount": pending["counts"]["pendingNodes"],
        "pendingEdgeCount": pending["counts"]["pendingEdges"], "transcriptMessages": reader["messages"],
        "skipped": skipped, "notices": notices, "approvalRequired": True,
        **({"skippedMemletEcho": echo_count} if echo_count else {}),
    }
    lines = ["## Memlet learn (PENDING: nothing is approved)", "",
             f"- Queue: `{QUEUE_REL}` · {len(info['added'])} new item(s) · {res['pendingCount']} pending",
             f"- Transcripts read: {len(files)} · visible messages: {reader['messages']}",
             "- Approve with `learn --approve <id>` after reading each cited line; rejected or pending items are "
             "excluded from the approved lessons group."]
    lines += [f"- NOTICE: {n}" for n in notices]
    res["formatted"] = "\n".join(lines) + "\n"
    return res


# A projection shares this context through existing reader APIs without global
# caches or monkeypatching. Nested/concurrent projections restore their own state.
from ._read_context import TRUST_PROJECTIONS as _ACTIVE_TRUST_PROJ_SINK


class _LessonPathContext(dict):
    def __init__(self, root, work=None):
        super().__init__(root=root, loaded=False, trust={}, is_git=False, tracked=set(),
                         initialized=False, directories=work.directories if work else {},
                         valid={}, literal={}, paths={}, work=work)

    def update(self, *args, **kwargs):
        values = dict(*args, **kwargs)
        if "snapshot" in values and not self["loaded"] and self["work"]:
            self["work"].count("snapshotReads")
            self["work"].count("tierPasses")
        super().update(values)


def _valid_path_context(context: Any) -> Optional[_LessonPathContext]:
    return context if isinstance(context, _LessonPathContext) else None


def _active_lesson_path_context(root: Path) -> Optional[_LessonPathContext]:
    return md._read_cache("lesson_path_contexts").get(root)


@contextlib.contextmanager
def _lesson_path_projection(root, work):
    """A call-local output accumulator, passed explicitly; never a read memo."""
    with md._enter_read_scope():
        yield _LessonPathContext(mw._resolve_root(root), work)


def _lesson_path_available(context):
    return not context.get("work") or context["work"].available()


def _lesson_path_exhausted(context):
    work = context.get("work")
    return bool(work and work.overdue())


@md.fresh_read
def valid_lesson_path(root, path, *, context=None):
    """Exact, contained regular file; an attachment names a location, not content identity.

    Recreating a removed file at that location reactivates it, just like a file anchor.
    No normalization is allowed for a human approval. Caches live for one read only.
    """
    root = Path(root)
    if context is None:
        active = _active_lesson_path_context(root)
        context = active if active is not None else _LessonPathContext(root)
    if _lesson_path_exhausted(context):
        return False
    if not isinstance(path, str):
        return False
    if path in context["valid"]:
        return context["valid"][path]
    result = _valid_lesson_path(root, path, context)
    context["valid"][path] = result
    return result


def _valid_lesson_path(root, path, context):
    if (not path or len(path) > 240
            or any(c in path for c in "\\*?[]`<>") or unsafe_contributed_string(path) or path.startswith("/")
            or any(p in ("", ".", "..", ".git", ".memlet") for p in path.split("/"))
            or mw._is_secret_path(path) or mw.contained_path_problem(root, path)):
        return False
    cursor = root
    directories = context["directories"]
    try:
        for part in path.split("/"):
            if _lesson_path_exhausted(context):
                return False
            if cursor not in directories:
                work = context.get("work")
                if len(directories) < 8 and getattr(work, "_caller_clock", True) and not _lesson_path_available(context):
                    return False
                work = context.get("work")
                if work and work.counters is not None:
                    key = cursor.relative_to(root).as_posix()
                    counts = work.counters["directoryListings"]
                    counts[key] = counts.get(key, 0) + 1
                directories[cursor] = set()  # also cache an unreadable directory
                directories[cursor] = {p.name for p in cursor.iterdir()}
            if part not in directories[cursor]:
                return False
            cursor = cursor / part
            if cursor != root / path and (cursor / ".git").exists():
                return False
        return cursor.is_file()
    except OSError:
        return False


LITERAL_FILES_MAX_BYTES = 64 * 1024 * 1024
LITERAL_FILES_TIMEOUT = 5.0


def attachment_metadata(context):
    if context.get("attachmentIncomplete"):
        from .vocabulary import ATTACHMENT_INCOMPLETE
        return {"attachmentIncomplete": True, "attachmentReason": ATTACHMENT_INCOMPLETE}
    return {}


def _attachment_result(result, context):
    metadata = attachment_metadata(context)
    if metadata:
        result.update(metadata)
        notice = metadata["attachmentReason"]
        notices = result.setdefault("notices", [])
        if notice not in notices:
            notices.append(notice)
    return result


def _attachment_reader(fn):
    import functools
    import inspect
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def read(*args, **kwargs):
        try:
            bound = signature.bind(*args, **kwargs)
        except TypeError:
            return fn(*args, **kwargs)
        repo_path = bound.arguments.get("repo_path", ".")
        state = md._active_read_state()
        try:
            root = mw._resolve_root(repo_path)
        except ValueError:
            return fn(*args, **kwargs)
        if "_read_state" in signature.parameters:
            kwargs = dict(kwargs, _read_state=state)
        valid_ctx = _valid_path_context(kwargs.get("_path_context"))
        if valid_ctx is not None:
            return fn(*args, **dict(kwargs, _path_context=valid_ctx))
        kwargs = dict(kwargs, _path_context=None)
        with _lesson_path_projection(root, None) as context:
            result = fn(*args, **dict(kwargs, _path_context=context))
            if context.get("attachmentIncomplete"):
                _attachment_result(result, context)
                if "formatted" in result and "tokenBudget" not in result:
                    result["formatted"] += "\n" + result["attachmentReason"] + "\n"
            return result
    return read


@md.fresh_read
def lesson_path_context(repo_path=".", *, remaining=None, _read_state=None, context=None):
    """One bounded tracked-file lookup and one set of evidence reads per projection.

    A held context is the caller's snapshot, even after the read or a later write.
    An invalid/unreadable git work tree fails closed for automatic attachments.
    """
    root = mw._resolve_root(repo_path)
    context = _valid_path_context(context)
    if context is None:
        context = _active_lesson_path_context(root)
        if context is None:
            context = _LessonPathContext(root)
            md._read_cache("lesson_path_contexts")[root] = context
    if context["initialized"]:
        return context
    context["initialized"] = True
    is_git = any((p / ".git").exists() for p in [root] + list(root.parents))
    context["is_git"] = is_git
    complete = not is_git
    if is_git and _lesson_path_available(context):
        work = context.get("work")
        if work:
            work.count("gitCalls")
        allowance = LITERAL_FILES_TIMEOUT if remaining is None else min(LITERAL_FILES_TIMEOUT, max(0, remaining))
        if work and work.bounded:
            allowance = min(allowance, max(0, work.deadline - getattr(work, "last_now", 0.0)))
        complete, raw_tracked = md._read_owned_git_stream(
            ["git", "ls-files", "-z", "--cached", "--"],
            cwd=str(root),
            allowance=allowance,
            max_bytes=LITERAL_FILES_MAX_BYTES,
            _popen=subprocess.Popen,
            _read=os.read,
        )
        if complete:
            context["tracked"] = set(raw_tracked.decode("utf-8", "surrogateescape").split("\0"))
    if not complete:
        context["attachmentIncomplete"] = True
        if context.get("work"):
            from .vocabulary import GRAPH_FILES_NOTICE
            context["work"].skip(GRAPH_FILES_NOTICE)
    return context


_lesson_token = re.compile(r"(?<![\w/.-])(?:\./)?[\w.-]+(?:/[\w.-]+)*")
_lesson_punctuation = re.compile(r"[.,;:!?)\]\"'`]+(?=\s|$)")


def _lesson_path_tokens(text):
    raw, named = [], set()
    for match in _lesson_token.finditer(text):
        path = match.group().rstrip(".")
        raw.append(path)
        end = match.start() + len(path)
        before = text[match.start()-1:match.start()] if match.start() else ""
        after = text[end:end+1]
        leading = not before or before.isspace() or before in "\"'`([{<"
        boundary = leading and (end == len(text) or text[end].isspace() or _lesson_punctuation.match(text, end))
        if boundary and not any(unicodedata.category(c) in ("Cc", "Cf", "Mn", "Me") for c in before + after):
            named.add(path.removeprefix("./"))
    return raw, named


def _lesson_path_candidates(text):
    """Shared lexical prefilter; no file or tier decision is made here."""
    return _lesson_path_tokens(text)[1]


@md.fresh_read
def literal_lesson_paths(root, scope, text, *, context=None):
    """Extract whole path tokens from text; Git requires tracked files.

    ``scope`` is retained for caller compatibility but never attaches a path: it
    is only a display label and recall filter term, regardless of proposer/tier.
    """
    context = lesson_path_context(root, _read_state=md._active_read_state()) if context is None else context
    root = context["root"]
    if _lesson_path_exhausted(context):
        return []
    if text in context["literal"]:
        return list(context["literal"][text])
    # Preserve the legacy ordering (explicit references first), but scan token
    # boundaries once. Long lines with repeated words cannot cause nested scans.
    raw = [p for p, _ in mw._line_refs(text)]
    tokens, named = _lesson_path_tokens(text)
    raw.extend(tokens)
    out = []
    for path in dict.fromkeys(raw):
        if _lesson_path_exhausted(context):
            break
        path = path.replace("\\", "/").removeprefix("./")
        if (path in named and len(path) <= 80 and (not context["is_git"] or path in context["tracked"])
                and valid_lesson_path(root, path, context=context) and path not in out):
            out.append(path)
            if len(out) == 8:
                break
    context["literal"][text] = tuple(out)
    return out


def _lesson_literal_paths(node, root, context):
    """Match original hashed lines, never a sanitised display projection.

    Own citations were approved with the node; only approved-valid edges may add
    further evidence. Each line is matched separately so joins cannot invent tokens.
    """
    scope = node.get("scopeTag") or ""
    # Ordinary lessons need only citation identities, not sanitised display
    # strings. Supersessions still use the authoritative role computation.
    fields = (md.lesson_display_fields(node) if md.superseded_lesson(node) or node.get("_lineReplacements")
              else {"cites": md.lesson_lines(node)})
    roles = fields.get("lineRoles", ["current"] * len(fields.get("cites", [])))
    paths = []
    evidence = list(zip(fields.get("cites", []), roles))
    replaced = {c["span"] for c, role in evidence if role == "replaced"}
    for edge in node.get("edges", []):
        if _lesson_path_exhausted(context):
            return []
        if md.approval_state(edge, "edge") == TIER_APPROVED and edge.get("edgeVerdict") == "VALID":
            for pin in md._item_pins(edge, "edge") + [edge.get("citedTargetPin")]:
                if not isinstance(pin, dict) or md._verify_pin_dict(pin, context["snapshot"] or {}, "path"):
                    continue
                parsed = md.parse_span(pin.get("span", ""))
                if parsed:
                    for number in range(parsed[1], parsed[2] + 1):
                        span = "%s:%d" % (parsed[0], number)
                        if span not in replaced:
                            evidence.append((md._resolve_pin(context["snapshot"], span), "current"))
    seen = set()
    for cite, role in evidence:
        if _lesson_path_exhausted(context):
            break
        identity = (cite["span"], cite.get("sha256"))
        if role != "current" or identity in seen:
            continue
        seen.add(identity)
        if len(paths) >= 8:
            break
        pin = md._resolve_pin(context["snapshot"] or {}, cite["span"])
        if not pin or pin.get("sha256") != cite.get("sha256"):
            continue
        for line in str(pin.get("quoteText", "")).split("\n"):
            for path in literal_lesson_paths(root, scope, line, context=context):
                if path not in paths:
                    paths.append(path)
    return paths[:8]


@md.fresh_read
def lesson_paths(node, repo_path=".", *, context=None):
    """Human-attached locations: approved tier, exact existing file; tracking is optional."""
    if not node.get("approvedPaths") or md.approval_state(node, "node") != TIER_APPROVED:
        return []
    context = lesson_path_context(repo_path, _read_state=md._active_read_state()) if context is None else context
    if not context["loaded"]:
        snapshot, _ = _load_snapshot(context["root"])
        config, _ = _load_config(context["root"])
        graph, _ = _load_queue(context["root"])
        if context.get("work"):
            context["work"].count("queueReads")
        context.update(loaded=True, snapshot=snapshot, config=tier_config(graph or {}, config),
                       trust=dream_tiers(graph or {}, snapshot=snapshot, config=config, _read_state=md._active_read_state()))
    state = context["trust"].get(node["id"])
    if state is None:
        state = {"tier": TIER_PENDING}
        context["trust"][node["id"]] = state
    if state["tier"] != TIER_APPROVED:
        return []
    return [p for p in node.get("approvedPaths", []) if valid_lesson_path(context["root"], p, context=context)]


def _decision_event_write(root, events):
    unique = {}
    for event in events:
        unique.setdefault((event[1], event[2]), event)
    events = list(unique.values())
    # Advisory history only: integrity, not authentication; no tier reads this table.
    conn = mw.connect_managed_sqlite(Path(root), ".memlet/ledger.sqlite3")
    try:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS dream_decision_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, item_id TEXT, kind TEXT, decision TEXT, by TEXT, digest TEXT, paths TEXT)")
            conn.executemany("INSERT INTO dream_decision_events (at,item_id,kind,decision,by,digest,paths) VALUES (?,?,?,?,?,?,?)", events)
    finally:
        conn.close()
    md._drop_read_caches()


class DecisionEvents(list):
    """List-compatible advisory rows with read diagnostics."""
    def __init__(self, rows=(), *, skipped=0, truncated=False):
        super().__init__(rows)
        self.skipped = skipped
        self.truncated = truncated


@md.fresh_read
def decision_events(repo_path=".", item_id=None):
    """Bounded advisory history in stable sequence order; absent/unreadable means unknown."""
    try:
        root = mw._resolve_root(repo_path)
        conn = mw.connect_managed_sqlite(root, ".memlet/ledger.sqlite3", readonly=True)
        try:
            query = "SELECT seq,at,item_id,kind,decision,by,digest,paths FROM dream_decision_events"
            rows = conn.execute(query + (" WHERE item_id=?" if item_id is not None else "") + " ORDER BY seq DESC LIMIT 4097", (item_id,) if item_id is not None else ()).fetchall()
            result = DecisionEvents(truncated=len(rows) > 4096)
            for r in reversed(rows[:4096]):
                try:
                    paths = json.loads(r[7])
                    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
                        raise ValueError("invalid paths")
                    result.append(dict(zip(("seq","at","item_id","kind","decision","by","digest","paths"), (*r[:7], paths))))
                except (ValueError, TypeError):
                    result.skipped += 1
            return result
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error):
        return []


def _learn_repo_option(root):
    import shlex
    try:
        return "" if Path(root).resolve() == Path.cwd().resolve() else " --repo " + shlex.quote(str(root))
    except FileNotFoundError:
        return " --repo " + shlex.quote(str(root))  # caller may have replaced its cwd


def _learn_commands_repo(text, root):
    suffix = _learn_repo_option(root)
    return re.sub(r"`(memlet learn [^`]+)`", lambda m: "`" + m[1] + suffix + "`", text) if suffix else text


@md.fresh_read
def learn_pending(repo_path=".", *, _record_review=True, _counters: Optional[Dict[str, int]] = None,
                  _read_state: Optional[Any] = None) -> Dict[str, Any]:
    """Nodes and edges of `.memlet/dream/APPROVAL-QUEUE.json` with no valid human decision (stale approvals included)."""
    with md.work_counters(_counters), md._enter_read_scope(_read_state):
        return _learn_pending_scoped(repo_path, _record_review=_record_review)


def _learn_pending_scoped(repo_path=".", *, _record_review=True) -> Dict[str, Any]:
    try:
        root = mw._resolve_root(repo_path)
    except ValueError as exc:
        return _fail(str(exc), 2)
    bad = _symlinked(root, (QUEUE_REL, SNAPSHOT_REL))
    if bad:
        return _fail(_refusing(bad), 2)
    empty_counts = {"approvedNodes": 0, "rejectedNodes": 0, "approvedEdges": 0, "rejectedEdges": 0,
                    "pendingNodes": 0, "pendingEdges": 0}
    try:
        graph, reason = _load_queue(root)
    except (OSError, ValueError) as exc:
        return _fail(f"cannot read the Dream queue: {exc}", 1)
    if graph is None:
        return {"ok": True, "exitCode": 0, "command": "learn-pending", "pendingNodes": [], "pendingEdges": [],
                "counts": empty_counts, "notices": [reason], "formatted": f"- {reason}; run `memlet learn --transcript PATH`\n"}
    snap, _why = _load_snapshot(root)
    res = md.list_pending(graph, snapshot_files=snap or {})
    by_id = {str(n.get("id")): n for n in graph.get("nodes") or [] if isinstance(n, dict)}
    config, _ = _load_config(root)
    trust = dream_tiers(graph, snapshot=snap, config=config)
    res["pendingNodes"] = [i for i in res["pendingNodes"] if trust.get(i["id"], {}).get("tier") != TIER_APPROVED]
    pending_ids = {i["id"] for i in res["pendingNodes"]}
    for node in graph.get("nodes", []):
        if (node["id"] not in pending_ids and md.approval_state(node, "node") == TIER_APPROVED
                and trust[node["id"]]["tier"] != TIER_APPROVED):
            pp = node.get("primaryPin") or {}
            res["pendingNodes"].append({"id": node["id"], "category": node["category"],
                "categoryLabel": md.category_label(node["category"]), "quote": lesson_text(node), "span": pp.get("span"),
                "staleApproval": True, "citationReason": "approval has no live approved-valid edge, a rejected edge, or stale citations"})
    merges = merge_view(root, context=dict(graph=graph, snapshot=snap, config=config, trust=trust))
    hidden = {i for i, st in merges["states"].items() if st["state"] == "merged"}
    member_reviews = {i["id"]: i for i in res["pendingNodes"] if i["id"] in hidden}
    res["pendingNodes"] = [i for i in res["pendingNodes"] if i["id"] not in hidden]
    res["pendingEdges"] = [e for e in res["pendingEdges"] if e.get("fromId") not in hidden and e.get("toId") not in hidden]
    res["counts"]["pendingEdges"] = len(res["pendingEdges"])
    res["counts"]["pendingNodes"] = len(res["pendingNodes"])
    res["counts"]["approvedNodes"] = sum(t["tier"] == TIER_APPROVED for i, t in trust.items() if i not in hidden)
    if any("merge" in n for n in by_id.values()):
        res["counts"]["hiddenMergedNodes"] = len(hidden)
        res["counts"]["approvedEdges"] = sum(md.approval_state(e, "edge") == TIER_APPROVED
            for e in graph.get("edges", []) if e.get("fromId") not in hidden and e.get("toId") not in hidden)
    for item in res["pendingNodes"]:
        node = by_id.get(str(item.get("id")))
        item.update(trust[str(item["id"])])
        item.update(proposers=_display_records((node or {}).get("proposers", [])),
                    verification=_display_records((node or {}).get("verification", [])))
        item.update(_lesson_fields(node))
        if (node or {}).get("approvedPaths"):
            item["approvalPaths"] = node["approvedPaths"]
        if item.get("quoteLines"):
            item["quote"] = lesson_text(node)
        item["pinsVerify"] = bool(snap is not None and node is not None and _node_edges_verify(node, snap) is None)
    for item in res["pendingNodes"] + res["pendingEdges"]:
        for key in ("quote", "currentQuote", "title", "scopeTag", "targetLabel"):
            if isinstance(item.get(key), str):
                item[key] = safe_lesson_display(item[key])
    from .vocabulary import LESSON_SEPARATOR
    for item in res["pendingNodes"]:
        if item.get("quoteLines"):
            item["quote"] = LESSON_SEPARATOR.join(item["quoteLines"])
    lines = [f"## Pending Dream items ({res['counts']['pendingNodes']} node(s), {res['counts']['pendingEdges']} edge(s))", ""]
    for item in res["pendingNodes"]:
        flag = (" — " + str(item["citationReason"]) if item.get("citationReason") else
                "" if item["pinsVerify"] else " — citation does NOT verify: snapshot unavailable")
        if item.get("quoteLines"):
            lines.append(f"- `{item['id']}` [{item['categoryLabel']}]{flag}\n" + (_pending_quotes(item)))
        else:
            lines.append(f"- `{item['id']}` [{item['categoryLabel']}] {item['quote']} (cites `{SNAPSHOT_REL}/{item['span']}`){flag}")
            if item.get("approvalPaths"):
                lines.append("  " + APPROVAL_PATHS_LABEL + ", ".join(item["approvalPaths"]))
        lines.append("  tier=" + item["tier"] + "; " + item["agreementLabel"] + "; proposers=" + ", ".join(str(r.get("vendor")) + "/" + str(r.get("isolation", "none")) for r in item["proposers"] if isinstance(r, dict))
                     + "; verification=" + (", ".join(str(r.get("vendor")) + ":" + str(r.get("verdict")) for r in item["verification"] if isinstance(r, dict)) or "none"))
        if item.get("citationReason") and item.get("currentQuote"):

            lines.append("  Current citations for fresh review (all node and edge evidence):")
            lines += ["  > [" + ("changed" if c["changed"] else "unchanged") + "] " + c["quote"]
                      + " (`" + SNAPSHOT_REL + "/" + c["span"] + "`)" for c in item["currentCitations"]]
            lines.append("  After review: `memlet learn --approve " + str(item["id"]) + " --by NAME --refresh-citations`")
    if not res["pendingNodes"]:
        lines.append("- (none); add candidates with `memlet learn --transcript PATH`")
    for edge in res["pendingEdges"]:
        if any(c["changed"] for c in edge.get("currentCitations", [])):
            lines.append("- Edge `" + edge["edgeId"] + "` (" + edge["edgeType"] + "): current citations for fresh review")
            lines += ["  > [" + ("changed" if c["changed"] else "unchanged") + "] " + c["quote"]
                      + " (`" + SNAPSHOT_REL + "/" + c["span"] + "`)" for c in edge["currentCitations"]]
            lines.append("  After review: `memlet learn --approve " + edge["edgeId"] + " --by NAME --refresh-citations`")
    extra = {}
    if any("merge" in n for n in by_id.values()):
        from .vocabulary import MERGE_REASONS, MERGE_HEADING, MERGE_LAPSED_HEADING, MERGE_MEMBER_LABEL
        def detail(ident):
            n = by_id[ident]
            review = member_reviews.get(ident, {})
            return dict(id=ident, tier=trust[ident]["tier"], quote=safe_lesson_display(lesson_text(n)),
                        pinsVerify=bool(snap is not None and _node_edges_verify(n, snap) is None),
                        **{k: review[k] for k in ("citationReason", "currentCitations", "currentQuote") if k in review},
                        **_lesson_fields(n))
        groups = [dict(detail(keep), mergedFrom=members, members=[detail(i) for i in members])
                  for keep, members in sorted(merges["members"].items())]
        lapsed = [dict(detail(i), into=st["into"], reason=st["reason"], message=MERGE_REASONS[st["reason"]])
                  for i, st in merges["states"].items() if st["state"] == "lapsed"]
        extra.update(mergedLessons=groups, lapsedMerges=lapsed)
        for item in res["pendingNodes"]:
            if merges["members"].get(item["id"]):
                item["mergedFrom"] = merges["members"][item["id"]]
                item["members"] = [detail(i) for i in item["mergedFrom"]]
        if groups:
            lines += ["", MERGE_HEADING]
            for group in groups:
                lines += ["- `" + group["id"] + "` (" + group["tier"] + ")", _pending_quotes(group)]
                for member in group["members"]:
                    flag = (" — " + member["citationReason"] if member.get("citationReason") else
                            "" if member["pinsVerify"] else " — citation does NOT verify: snapshot unavailable")
                    lines += ["  - `" + member["id"] + "` [" + member["tier"] + "] " + MERGE_MEMBER_LABEL + " `" + group["id"] + "`" + flag,
                              "\n".join("  " + line for line in _pending_quotes(member).splitlines())]
        if lapsed:
            lines += ["", MERGE_LAPSED_HEADING]
            from .vocabulary import MERGE_INVALID_LINE
            lines += ["- `" + i["id"] + "` [" + i["tier"] + "]" + (
                " → `" + i["into"] + "`: " + i["reason"] + " — " + i["message"] if i["into"] else
                ": " + MERGE_INVALID_LINE) for i in lapsed]
    if _record_review:
        md.record_citation_reviews(root / QUEUE_REL, dict(res, pendingNodes=res["pendingNodes"] + list(member_reviews.values())))
    return {"ok": True, "exitCode": 0, "command": "learn-pending", "pendingNodes": res["pendingNodes"],
            "pendingEdges": res["pendingEdges"], "counts": res["counts"], "approvalBoundary": res["approvalBoundary"],
            "notices": [], **extra, "formatted": (_learn_commands_repo("\n".join(lines) + "\n", root) if extra else "\n".join(lines) + "\n")}


def _decide(repo_path, ids, approver, decision: str, reason: str = "", *, reconsider=False, refresh_citations=False, paths=None) -> Dict[str, Any]:
    try:
        root = mw._resolve_root(repo_path)
    except ValueError as exc:
        return _fail(str(exc), 2)
    who = str(approver or "").strip()
    if not who:  # an explicit human name is part of the review contract; nothing is ever filled in
        return _fail("an approver name is required (the person who read the cited lines); nothing was changed", 2)
    wanted = _ids(ids)
    if not wanted:
        return _fail("at least one Dream item id is required", 2)
    bad = _symlinked(root, (QUEUE_REL, SNAPSHOT_REL) + _DB_FILES + (PROMOTED_PAGE,) + mw._LEDGER_FILES)
    if bad:
        return _fail(_refusing(bad), 2)
    q = root / QUEUE_REL
    if not q.is_file():
        return _fail("no Dream queue (.memlet/dream/APPROVAL-QUEUE.json); run `learn` first", 1)
    try:
        # the decision and the promotion that follows form one critical section: no committed decision is lost to a
        # concurrent writer, and the promoted page never lags the queue it was rendered from
        with md.queue_lock(q):
            if decision == md.APPROVED and paths is not None:
                _, path_context = _merge_context(root)
                refused = _member_path_refusal(root, wanted, paths, who, path_context)
                if refused:
                    return refused
            if decision == md.APPROVED:
                snap = root / SNAPSHOT_REL
                if not snap.is_dir():
                    return _fail("no transcript snapshot (.memlet/dream/snapshot/) to verify pins against", 1)
                res = md.approve_queue_items(q, wanted, who, snapshot_path=str(snap), db_path=str(root / DB_REL),
                                             reconsider=reconsider, refresh_citations=refresh_citations, paths=paths)
            else:
                res = md.reject_queue_items(q, wanted, who, reason=reason, db_path=str(root / DB_REL))
            promotion = promote_approved(root)
            lost_files, path_commands, path_notices = {}, {}, []
            if decision == md.APPROVED and paths is not None:
                _, context = _merge_context(root)
                view = merge_view(root, context=context)
                for keep in wanted:
                    members = view["members"].get(keep, [])
                    if not members:
                        continue
                    checked = merge_blockers(root, keep, members, context=context)
                    if checked["lostFiles"]:
                        lost_files.update(checked["lostFiles"])
                        command, note = merge_path_repair(root, keep, checked["lostFiles"], context, who)
                        if command:
                            path_commands[keep] = command
                        if note:
                            path_notices.append(note)
    except (OSError, ValueError, sqlite3.Error) as exc:
        return _fail(f"{decision} failed: {exc}", 1)
    out = {"ok": bool(res.get("ok")) and bool(promotion.get("ok")), "exitCode": 0, "command": f"learn-{decision}",
           "decision": decision, "approver": who, "applied": res.get("applied", []),
           "unchanged": res.get("unchanged", []), "refused": res.get("refused", []),
           "cascadedEdges": res.get("cascadedEdges", []), "approvalBoundary": res.get("approvalBoundary"),
           "promotion": {k: promotion.get(k) for k in ("ok", "page", "promoted", "removed", "op", "error")}}
    for key in ("resealedEdges", "untouchedEdges"):
        if key in res:
            out[key] = res[key]
    errors = list(res.get("eventErrors", []))
    if paths is not None:
        out["paths"] = list(paths)
    if res.get("refused"):
        errors.append("refused: " + ", ".join(f"{r['id']} ({r['reason']})" for r in res["refused"]))
    if not promotion.get("ok"):
        errors.append(f"decision recorded, but promotion failed: {promotion.get('error')}")
    if errors:
        out.update(ok=False, exitCode=1, error="; ".join(errors))
    lines = [f"## Memlet learn — {decision} by {who}", ""]
    lines += [f"- {decision}: `{i}`" for i in out["applied"]]
    lines += [f"- unchanged: `{i}`" for i in out["unchanged"]]
    if "resealedEdges" in out or "untouchedEdges" in out:
        from .vocabulary import RESEALED_RELATIONS, UNTOUCHED_RELATIONS
        lines.append("- " + RESEALED_RELATIONS + (", ".join(out.get("resealedEdges", [])) or "none"))
        lines.append("- " + UNTOUCHED_RELATIONS + (", ".join(
            e["id"] + " (" + e["state"] + ")" for e in out.get("untouchedEdges", [])) or "none"))
    lines += [f"- REFUSED `{r['id']}`: {r['reason']}" for r in out["refused"]]
    if promotion.get("ok"):
        lines.append(f"- Promoted page `{PROMOTED_PAGE}`: {len(promotion.get('promoted') or [])} rule(s), "
                     f"{len(promotion.get('removed') or [])} removed ({promotion.get('op')})")
    else:
        lines.append(f"- Promotion NOT done: {promotion.get('error')}")
    if paths is not None:
        lines.append("- The same paths apply to each requested lesson: " + ", ".join(paths))
    if lost_files:
        from .vocabulary import MERGE_LOST_FILES, MERGE_ATTACH_LABEL
        out.update(lostFiles=lost_files, pathCommands=path_commands, pathNotices=path_notices)
        lines += ["- `" + i + "`: " + MERGE_LOST_FILES + ", ".join(ps) for i, ps in lost_files.items()]
        lines += ["- " + MERGE_ATTACH_LABEL + "`" + c + "`" for c in path_commands.values()]
        lines += ["- " + n for n in path_notices]
    out["formatted"] = "\n".join(lines) + "\n"
    return out


@md.fresh_read
def learn_approve(repo_path=".", ids=(), *, approver="", reconsider=False, refresh_citations=False, paths=None) -> Dict[str, Any]:
    """The only path out of PENDING: `memlet_dream.approve_queue_items` with the snapshot and DB passed explicitly
    (pins re-verified, approval bound to a content digest), then `promote_approved`."""
    return _decide(repo_path, ids, approver, md.APPROVED, reconsider=reconsider, refresh_citations=refresh_citations, paths=paths)


@md.fresh_read
def learn_paths(repo_path=".", ids=(), *, approver="", paths=None):
    """Replace attachments by a fresh approval; only a currently approved lesson qualifies."""
    try:
        root = mw._resolve_root(repo_path)
        if _symlinked(root, (QUEUE_REL, SNAPSHOT_REL)):
            return _fail("refusing symlinked Dream data", 2)
        with md.queue_lock(root / QUEUE_REL):
            graph, _ = _load_queue(root)
            if graph and any("merge" in n for n in graph.get("nodes", [])):
                _, context = _merge_context(root, {"graph": graph})
                refusal = _member_path_refusal(root, _ids(ids), paths or [], approver, context)
                if refusal:
                    return refusal
                approved = {i for i, state in context["trust"].items() if state["tier"] == TIER_APPROVED}
            else:
                approved = {i["id"] for i in dream_items(root, _graph=graph)["items"]}
            if not paths or not _ids(ids) or any(i not in approved for i in _ids(ids)):
                return _fail("--paths requires an already approved lesson and --path FILE", 2)
            selected = [n for n in graph["nodes"] if n["id"] in _ids(ids)]
            changed = any(n.get("approvedPaths", []) != list(paths) for n in selected)
            return learn_approve(root, ids, approver=approver, reconsider=changed, paths=paths)
    except (OSError, ValueError) as exc:
        return _fail(str(exc), 2)


@md.fresh_read
def learn_reject(repo_path=".", ids=(), *, approver="", reason="") -> Dict[str, Any]:
    """`memlet_dream.reject_queue_items` (rejected items are never recalled), then `promote_approved`."""
    return _decide(repo_path, ids, approver, md.REJECTED_DECISION, reason=reason)


@md.fresh_read
def merge_path_repair(root, keep, lost, context, who="NAME", extra=(), *, approve=False):
    """One attachment union and the existing eight-file/approved-tier limits."""
    import shlex
    from .vocabulary import MERGE_PATH_APPROVAL, MERGE_PATH_LIMIT
    node = next(n for n in context["graph"]["nodes"] if n["id"] == keep)
    paths = list(dict.fromkeys(node.get("approvedPaths", []) + list(extra) +
                              sorted({p for missing in lost.values() for p in missing})))
    if context["trust"][keep]["tier"] != TIER_APPROVED and not approve:
        return None, MERGE_PATH_APPROVAL
    if len(paths) > 8:
        return None, MERGE_PATH_LIMIT
    mode = "--approve" if approve and context["trust"][keep]["tier"] != TIER_APPROVED else "--paths"
    return ("memlet learn " + mode + " " + shlex.quote(keep) + " --by " + shlex.quote(who) +
            "".join(" --path " + shlex.quote(p) for p in paths) + _learn_repo_option(root)), None


def _member_path_refusal(root, wanted, paths, who, context):
    from .vocabulary import MERGE_MEMBER_PATH_REFUSED, MERGE_ATTACH_LABEL
    view = merge_view(root, context=context)
    for ident in wanted:
        state = view["states"].get(ident, {})
        if state.get("state") == "merged":
            keep = state["into"]
            checks = merge_blockers(root, keep, view["members"][keep], context=context)
            command, note = merge_path_repair(root, keep, checks["lostFiles"], context, who, paths, approve=True)
            message = MERGE_MEMBER_PATH_REFUSED % (ident, keep)
            result = _fail(message, 2, reason="merged-member-paths", keep=keep,
                           pathCommands={keep: command} if command else {}, pathNotice=note)
            result["formatted"] += "- " + (MERGE_ATTACH_LABEL + "`" + command + "`" if command else note) + "\n"
            return result
    return None


def _merge_decide(repo_path, ids, approver, *, keep=None):
    from .vocabulary import MERGE_REASONS, MERGE_DONE, MERGE_LOST_FILES, MERGE_ATTACH_LABEL
    who = md.safe_truncate_masked(str(approver or "").strip(), 64)
    wanted = _ids(ids)
    if not who:
        return _fail(MERGE_REASONS["by-required"], 2, reason="by-required")
    if not wanted:
        reason = "ids-required" if keep is None else "same-required"
        return _fail(MERGE_REASONS[reason], 2, reason=reason)
    if keep is not None and not str(keep).strip():
        return _fail(MERGE_REASONS["keep-required"], 2, reason="keep-required")
    try:
        root = mw._resolve_root(repo_path)
        bad = _symlinked(root, (QUEUE_REL, SNAPSHOT_REL, PROMOTED_PAGE) + _DB_FILES + mw._LEDGER_FILES)
        if bad:
            return _fail(_refusing(bad), 2)
        with md.queue_lock(root / QUEUE_REL):
            root, context = _merge_context(root)
            graph = context["graph"]
            nodes = {n["id"]: n for n in graph["nodes"]}
            if keep is None:
                checks = dict(ok=all(i in nodes for i in wanted), keep=None,
                              blockers={i: ["unknown-id"] for i in wanted if i not in nodes}, lostFiles={})
            else:
                keep = str(keep).strip()
                checks = merge_blockers(root, keep, wanted, context=context)
            if not checks["ok"] or checks["blockers"]:
                refused = [{"id": keep if keep is not None and keep not in nodes and r == "unknown-id" else i, "reason": r, "message": MERGE_REASONS[r]}
                           for i, reasons in checks["blockers"].items() for r in reasons]
                said = []  # one sentence per (id, message): an unknown kept id is not repeated once per member
                for r in refused:
                    line = r["id"] + ": " + r["message"]
                    if line not in said:
                        said.append(line)
                message = checks.get("error") or "; ".join(said)
                return _fail(message, 2, **{k: v for k, v in checks.items() if k not in ("ok", "error")},
                             applied=[], refused=refused)
            path_commands, path_notice = {}, None
            if checks["lostFiles"]:
                command, path_notice = merge_path_repair(root, keep, checks["lostFiles"], context, who)
                if command:
                    path_commands[keep] = command
            res = md.write_merge_decisions(root / QUEUE_REL, graph, wanted, who, keep=keep, db_path=root / DB_REL)
            promotion = promote_approved(root) if res["applied"] else {"ok": True}
        decision = "unmerged" if keep is None else "merged"
        lines = [MERGE_DONE % (decision, who), ""]
        lines += ["- " + decision + ": `" + i + "`" + (" → `" + keep + "`" if keep else "") for i in res["applied"]]
        lines += ["- unchanged: `" + i + "`" for i in res["unchanged"]]
        for ident, missing in checks["lostFiles"].items():
            lines += ["- `" + ident + "`: " + MERGE_LOST_FILES + ", ".join(missing)]
        lines += ["- " + MERGE_ATTACH_LABEL + "`" + command + "`" for command in path_commands.values()]
        if path_notice:
            lines.append("- " + path_notice)
        if res.get("repaired"):
            from .vocabulary import MERGE_HISTORY_REPAIRED
            lines += ["- " + MERGE_HISTORY_REPAIRED + "`" + i + "`" for i in res["repaired"]]
        errors = res["eventErrors"] + ([promotion["error"]] if not promotion["ok"] else [])
        return dict(ok=not errors, exitCode=1 if errors else 0, command="learn-unmerge" if keep is None else "learn-merge",
                    keep=keep, approver=who, applied=res["applied"], unchanged=res["unchanged"],
                    lostFiles=checks["lostFiles"], pathCommands=path_commands, pathNotice=path_notice, repaired=res.get("repaired", []), refused=[],
                    **({"error": "; ".join(errors)} if errors else {}), formatted="\n".join(lines) + "\n")
    except (OSError, ValueError, sqlite3.Error) as exc:
        return _fail(str(exc), 1)


@md.fresh_read
def learn_merge(repo_path=".", keep="", same=(), *, approver=""):
    """A person records that members say the same thing as the kept lesson."""
    return _merge_decide(repo_path, same, approver, keep=keep)


@md.fresh_read
def learn_unmerge(repo_path=".", ids=(), *, approver=""):
    """Remove merge records, including lapsed ones, in one atomic batch."""
    return _merge_decide(repo_path, ids, approver)


# ---------------------------------------------------------------------------------------------------------------
# promote: approved, verified lessons -> one Memlet-owned wiki page


def _defuse(text: str, limit: int = 160) -> str:
    """One line of plain text that the wiki parser cannot read as markup or as a code reference: comment delimiters
    removed, backticks neutralised, `a.py::x` / `a.py#x` broken apart. The only anchor on a promoted rule is the one
    Memlet verified."""
    s = re.sub(r"<!--|-->", " ", str(text or ""))
    s = s.replace("`", "'").replace("::", ": :")
    s = re.sub(r"(?<=[A-Za-z0-9_])#(?=[A-Za-z_])", "# ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def _build_basename_candidates(root: Path) -> Dict[str, List[str]]:
    raw_by_name: Dict[str, List[str]] = {}
    seen = 0
    for cur, dirs, files in os.walk(root):
        rel_parts = Path(cur).relative_to(root).parts
        if rel_parts and (Path(cur) / ".git").exists():
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if d not in mw.EXCLUDED_DIRS and d != mw.MEMLET_DIR and d not in mw._SCRATCH_DIRS)
        seen += len(files)
        md._count_work("basenameWalkFiles", len(files))
        for fname in files:
            raw_by_name.setdefault(fname, []).append(cur)
        if seen > _MAX_BASENAME_WALK:
            break
    return raw_by_name


def _find_by_basename(root: Path, name: str) -> List[str]:
    """Repo-relative files named `name` (bounded, pruned walk; never into .memlet, excluded or nested-repo dirs)."""
    if md._active_read_state() is not None:
        rkey = Path(root).resolve()
        entry = md._read_cache("basename_index").get(rkey)
        if entry is None:
            raw_cands = _build_basename_candidates(root)
            entry = {"raw": raw_cands, "resolved": {}}
            md._read_cache("basename_index")[rkey] = entry
        if name in entry["resolved"]:
            return list(entry["resolved"][name])
        out_cached: List[str] = []
        for cur in entry["raw"].get(name, ()):
            p = Path(cur) / name
            if p.is_file() and not p.is_symlink():
                out_cached.append(mw._rel(root, p))
                if len(out_cached) > 1:
                    break
        entry["resolved"][name] = out_cached
        return list(out_cached)
    out: List[str] = []
    seen = 0
    for cur, dirs, files in os.walk(root):
        rel_parts = Path(cur).relative_to(root).parts
        if rel_parts and (Path(cur) / ".git").exists():
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if d not in mw.EXCLUDED_DIRS and d != mw.MEMLET_DIR and d not in mw._SCRATCH_DIRS)
        seen += len(files)
        md._count_work("basenameWalkFiles", len(files))
        if name in files:
            p = Path(cur) / name
            if p.is_file() and not p.is_symlink():
                out.append(mw._rel(root, p))
                if len(out) > 1:
                    break
        if seen > _MAX_BASENAME_WALK:
            break
    return out


def _resolve_lesson_anchor(root: Path, node: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """A real `path::symbol` for a lesson whose scope names code, only when that symbol exists right now."""
    pp = node.get("primaryPin") if isinstance(node.get("primaryPin"), dict) else {}
    text = f"{node.get('title') or ''}\n{node.get('scopeTag') or ''}\n{lesson_text(node)}"
    for m in mw.iter_explicit_anchors(text):
        if "::" in m.group(0):
            path, sym = mw._clean_ref_path(m.group("path")), m.group("symbol").rstrip(".-")
            if mw._current_fingerprint(root, path, sym).get("resolution") == "exact":
                return path, sym
    scope = safe_lesson_display(node.get("scopeTag") or "")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9]+", scope) or mw._unsafe_rel(scope):
        return None
    if (root / scope).is_file() and not (root / scope).is_symlink():
        candidates = [scope]
    else:
        candidates = _find_by_basename(root, scope.rsplit("/", 1)[-1])
    if len(candidates) != 1:
        return None  # missing or ambiguous file: an unanchored rule
    path = candidates[0]
    symbols = (mw.extract_file_symbol_signatures(root / path).get("symbols") or {})
    for ident in _IDENT_RE.findall(text):
        parts = ident.split(".")
        for cand in [ident] + [".".join(parts[i:i + 2]) for i in range(len(parts) - 1)] + parts:
            if len(cand) >= 3 and cand in symbols and mw._current_fingerprint(root, path, cand).get("resolution") == "exact":
                return path, cand
    return None


def _pin_comment(key: str, pin: Dict[str, Any]) -> str:
    sig = f" sig={pin['sig']}" if pin.get("sig") else ""
    req = f" req={pin['req']}" if pin.get("req") else ""
    return f"  <!-- memlet:anchor {key} ast={pin['ast']}{sig}{req} basis={pin.get('basis') or 'legacy'} -->"


def _parse_promoted(text: str) -> List[Dict[str, Any]]:
    """Rules on a promoted page: bullet line, optional pin comment(s), then the `memlet:dream` marker."""
    rules: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for no, line in enumerate(text.splitlines(), start=1):
        b = _BULLET_LINE_RE.match(line)
        if b:
            cur = {"line": no, "body": b.group("body"), "pins": []}
            continue
        if cur is None:
            continue
        pm = mw.PIN_RE.search(line)
        if pm and line.strip().startswith("<!--"):
            cur["pins"].append({"key": f"{pm.group('path')}::{pm.group('symbol')}",
                                "pin": {"ast": pm.group("ast"), "sig": pm.group("sig"), "req": pm.group("req"),
                                        "basis": pm.group("basis")}})
            continue
        mk = _RULE_MARK_RE.match(line)
        if mk:
            rules.append(dict(cur, id=mk.group("id"), digest=mk.group("digest"), textSha=mk.group("text")))
        cur = None
    return rules


def _page_edit_problem(root: Path, text: str, *, for_reader: bool = False) -> Optional[str]:
    """None when the page is exactly what Memlet last wrote (per the ledger), else why it must not be overwritten.

    `for_reader=True` is the rule readers (handover, recall, session start, changed-file check) apply before they
    present anything on the page as verified: the latest ledger event for the page must be a Memlet write
    (promotion, anchor accept, pin migration) of exactly this body. An earlier hand edit that a later explicit
    `accept` re-pinned no longer blocks readers; promotion keeps its stricter rule (any recorded hand edit after the
    last promotion blocks an overwrite, so hand notes are never destroyed)."""
    if _PAGE_META not in text:
        return f"{PROMOTED_PAGE} exists but was not written by Memlet; move it aside to let Memlet regenerate it"
    ledger = root / mw.LEDGER_PATH
    if _symlinked(root, mw._LEDGER_FILES) or not ledger.is_file():
        return f"{PROMOTED_PAGE} exists but the ledger has no record of Memlet writing it; move the page aside"
    try:
        with contextlib.closing(mw.connect_managed_sqlite(root, mw.LEDGER_PATH, readonly=True)) as conn:
            rows = conn.execute("SELECT id, content_sha256, event_type FROM wiki_events WHERE page=? ORDER BY id",
                                (PROMOTED_PAGE,)).fetchall()
    except (sqlite3.Error, OSError, ValueError) as exc:
        return f"cannot read the ledger: {exc}"
    promotes = [r[0] for r in rows if r[2] == "dream_promote"]
    if not promotes:
        return f"{PROMOTED_PAGE} exists but the ledger has no record of Memlet writing it; move the page aside"
    if not for_reader and any(r[2] == "user_edit" and r[0] > promotes[-1] for r in rows):
        return f"{PROMOTED_PAGE} was edited by hand (ledger user_edit); move your notes to another page first"
    last = rows[-1]
    if last[2] not in _MEMLET_PAGE_EVENTS or last[1] != mw._page_body_sha256(text):
        return f"{PROMOTED_PAGE} was edited by hand since Memlet last wrote it; move your notes to another page first"
    return None


def _record_pages(root: Path, events: Sequence[Tuple[str, str, str]], snapshots: Optional[List[Dict[str, Any]]] = None) -> None:
    now = mw._utc_now()
    with contextlib.closing(mw.connect_managed_sqlite(root, mw.LEDGER_PATH)) as conn:
        with conn:
            mw._ledger_ready(conn)
            for page, sha, event in events:
                mw._record_page(conn, page, sha, event, now)
            if snapshots:
                mw._sync_snapshots(conn, snapshots, now)


def _approved_lessons(root: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(approved nodes whose own pins and surviving edges verify against the snapshot, notes)."""
    notes: List[str] = []
    try:
        graph, why = _load_queue(root)
    except (OSError, ValueError) as exc:
        return [], [f"Dream queue unreadable: {exc}"]
    if graph is None:
        return [], [why]
    snap, why = _load_snapshot(root)
    if snap is None:
        return [], [why]
    # One tier gate for every consumer; do not reimplement edge approval here.
    result = dream_items(root)
    permitted = {it["id"] for it in result["items"]}
    return [_approved_projection(n, snap) for n in graph.get("nodes", []) if n.get("id") in permitted], result["notices"]


def lesson_text(node: Dict[str, Any]) -> str:
    return md.lesson_text(node)


def _lesson_fields(node):
    fields = md.lesson_display_fields(node)
    if fields:
        fields["cites"] = [dict(c, file=SNAPSHOT_REL + "/" + c["file"]) for c in fields["cites"]]
    return fields


def _pending_quotes(item):
    from .vocabulary import REPLACED_MARKER, CURRENT_MARKER
    if not any(r in ("replaced", "current") for r in item.get("lineRoles", [])):
        text = _lesson_quote_block(item)
        return text + ("\n  " + APPROVAL_PATHS_LABEL + ", ".join(item["approvalPaths"]) if item.get("approvalPaths") else "")
    lines = []
    for i, (quote, cite) in enumerate(zip(item.get("quoteLines", []), item.get("cites", []))):
        role = (item.get("lineRoles") or [None] * len(item["quoteLines"]))[i]
        marker = REPLACED_MARKER if role == "replaced" else CURRENT_MARKER if role == "current" else ">"
        lines.append("  " + marker + " " + quote + " (`{file}:{start}-{end}`)".format(**cite))
    if item.get("approvalPaths"):
        lines.append("  " + APPROVAL_PATHS_LABEL + ", ".join(item["approvalPaths"]))
    return "\n".join(lines)


def _lesson_quote_block(item):
    """Multi-line readers: one quoted line per citation, with relation source labels."""
    lines = []
    for index, (quote, cite) in enumerate(zip(item.get("quoteLines", []), item.get("cites", []))):
        tags = []
        for edge in item.get("edgeSources", []):
            parsed = md.parse_span(edge["span"])
            if parsed and cite["file"].endswith(parsed[0]) and parsed[1] <= cite["start"] <= parsed[2]:
                tags.append(edge["edgeType"] + " source")
        role = (item.get("lineRoles") or [None] * len(item.get("quoteLines", [])))[index]
        label = ""
        prefix = "  > "
        pin = " [" + cite.get("span", "{file}:{start}".format(**cite)) + " " + cite.get("sha256", "")[:8] + "]: "
        if role == "replaced":
            prefix = "  old_pin [" + item.get("oldLabel", "SUPERSEDED — DO NOT APPLY AS CURRENT") + "]" + pin
            label = "replaced — do not apply as current: "
            replacing = item.get("lineReplacements", {}).get(cite.get("span"))
            if replacing:
                prefix += " [replaced by " + ", ".join(replacing) + "]"
        elif role == "current":
            prefix = "  replacement_pin [" + item.get("replacementLabel", "ACTIVE — APPLY THIS") + "]" + pin
            label = "current instruction: "
        lines.append(prefix + label + quote + " (`{file}:{start}-{end}`".format(**cite) + ("; " + ", ".join(tags) if tags else "") + ")")
    return "\n".join(lines)


def render_capsule_lesson(item, hint=False):
    """Compact display only; citation and quote evidence remain untouched."""
    quotes = [safe_lesson_display(q, limit=md.LESSON_TEXT_MAX_CHARS)
              for q in item.get("quoteLines", [item.get("text", "")])]
    cites = [Path(c["file"]).name + ":" + str(c["start"]) for c in item.get("cites", [])]
    kinds = item.get("edgeTypes") or [e["edgeType"] for e in item.get("edgeSources", [])]
    relation = next((k for k in ("SUPERSEDES", "BLOCKS", "RESOLVES", "GOVERNS") if k in kinds), "LESSON")
    if item.get("quarantined"):
        parts = sorted(zip(item.get("lineRoles", []), quotes, cites), key=lambda p: p[0] != "replaced")
        replacements = {Path(c["file"]).name + ":" + str(c["start"]): ids
                        for c in item.get("cites", [])
                        for ids in [item.get("lineReplacements", {}).get(c.get("span"))] if ids}
        quotes = [("[REPLACED — do not apply as current" +
                   ("; replaced by " + ", ".join(replacements[cite]) if cite in replacements else "") + "] "
                   if role == "replaced" else "[" + item.get("replacementLabel", "CURRENT") + "] ") + quote
                  for role, quote, cite in parts]
        cites = [cite for role, quote, cite in parts]
        prefix = ""
    else:
        prefix = "[" + relation + "] "
    if hint:
        prefix += "[hint; " + hint_item_label(item) + "] "
    return "- " + prefix + " ⟶ ".join(quotes) + " (" + ", ".join(cites) + ")"


def render_recall_lesson(item):
    if not item.get("quarantined"):
        return render_approved_lesson(item)
    lines = ["- [" + item["id"] + "] Superseded Quarantine (`SUPERSEDES`)"]
    for role, quote, cite in zip(item.get("lineRoles", []), item.get("quoteLines", []), item.get("cites", [])):
        label = "REPLACED — do not apply as current" if role == "replaced" else item.get("replacementLabel", "CURRENT")
        replacing = item.get("lineReplacements", {}).get(cite.get("span"))
        if replacing:
            label += "; replaced by " + ", ".join(replacing)
        lines.append("  [" + label + "] " + quote + " (`{file}:{start}-{end}`)".format(**cite))
    return "\n".join(lines)


def render_approved_lesson(item):
    if not item.get("quoteLines"):
        item = dict(item, quoteLines=[safe_lesson_display(md._LINE_PREFIX_RE.sub("", item.get("text", ""), count=1))])
    if item.get("quarantined"):
        return md.render_superseded_quarantine(item)
    return ("- [" + item["id"] + "] [" + ", ".join(item.get("edgeTypes", [])) + "]\n"
            + _lesson_quote_block(item))


@md.fresh_read
def enrich_promoted_rules(root, rules, *, _read_state=None):
    if not any(r["source"] == PROMOTED_PAGE for r in rules):
        return
    entries = {e["source"]: e for e in promoted_rule_entries(root, _read_state=_read_state)[0]}
    for r in rules:
        e = entries.get("%s:%s" % (r["source"], r["line"]))
        if e and e.get("quoteLines"):
            r.update({k: e[k] for k in ("quoteLines", "cites", "edgeSources")})
            r["text"] = e["quote"]




def _render_rule(node: Dict[str, Any], anchor: Optional[Tuple[str, Dict[str, Any]]]) -> Tuple[str, List[str]]:
    pp = node.get("primaryPin") if isinstance(node.get("primaryPin"), dict) else {}
    text = lesson_text(node)
    meta = (f"dream {node.get('id')} · {md.category_label(node.get('category'))} · scope {_defuse(node.get('scopeTag'), 48)}"
            f" · cites {_defuse(SNAPSHOT_REL + '/' + str(pp.get('span') or ''), 120)}")
    if node.get("approvedPaths"):
        meta += APPROVAL_PATHS_SUFFIX + ", ".join(node["approvedPaths"])
    body = f"{text} (`{anchor[0]}`) — {meta}" if anchor else f"{text} — {meta}"
    lines = [f"- {body}"]
    if anchor:
        lines.append(_pin_comment(anchor[0], anchor[1]))
    lines.append(f"  <!-- memlet:dream id={node.get('id')} digest={md.node_approval_digest(node)} text={_sha(body)[:16]} -->")
    return body, lines


@md.fresh_read
def promote_approved(repo_path=".") -> Dict[str, Any]:
    """Write approved, pin-verified Dream lessons as rules into `.memlet/wiki/dream-approved.md`. Idempotent; removes
    rules whose approval or citation no longer verifies. Runs under
    the queue lock (`memlet_dream.queue_lock`). Anchors are sticky: once a rule is anchored, later promotions
    keep that anchor and its recorded pin (so drift shows up as stale instead of the anchor silently disappearing)."""
    try:
        root = mw._resolve_root(repo_path)
    except ValueError as exc:
        return _fail(str(exc), 2)
    if _symlinked(root, (QUEUE_REL,)) or not (root / DREAM_DIR).is_dir():
        return _promote_locked(root)  # no queue to serialise against (symlinks are refused inside)
    try:
        with md.queue_lock(root / QUEUE_REL):  # the page is rendered from the queue generation it reads
            return _promote_locked(root)
    except (OSError, ValueError) as exc:
        return _fail(f"cannot promote: {exc}", 1, page=PROMOTED_PAGE, promoted=[], removed=[])


def _promote_locked(root: Path) -> Dict[str, Any]:
    bad = _symlinked(root, (PROMOTED_PAGE, mw.LEDGER_PATH, QUEUE_REL, SNAPSHOT_REL))
    if bad:
        return _fail(_refusing(bad), 2, page=PROMOTED_PAGE, promoted=[], removed=[])
    page = root / PROMOTED_PAGE
    old_text = None
    if page.is_file():
        old_text = mw.read_regular_text(page, mw.MAX_WIKI_PAGE_BYTES, errors="replace")
        if old_text is None:
            return _fail(f"{PROMOTED_PAGE} is larger than {mw.MAX_WIKI_PAGE_BYTES} bytes or unreadable; move it aside",
                         1, page=PROMOTED_PAGE, promoted=[], removed=[])
    prev: Dict[str, Dict[str, Any]] = {}
    if old_text is not None:
        problem = _page_edit_problem(root, old_text)
        if problem:
            return _fail(problem, 1, page=PROMOTED_PAGE, promoted=[], removed=[])
        prev = {r["id"]: r for r in _parse_promoted(old_text)}
    lessons, notes = _approved_lessons(root)
    projected = {i["id"]: i for i in dream_items(root)["items"]}
    config_pins = mw._config_pins(root)
    lesson_lines: List[str] = []
    dead_lines: List[str] = []
    replaced_lines: List[str] = []
    rules_out: List[Dict[str, Any]] = []
    for n in sorted(lessons, key=lambda x: str(x.get("id"))):
        nid = str(n.get("id"))
        if projected[nid].get("quarantined"):
            body = "Replaced instruction " + nid + " (do not apply as current)"
            replaced_lines += ["- " + body,
                f"  <!-- memlet:dream id={nid} digest={md.node_approval_digest(n)} text={_sha(body)[:16]} -->",
                md.render_superseded_quarantine(projected[nid]) + (APPROVAL_PATHS_SUFFIX + ", ".join(n["approvedPaths"]) if n.get("approvedPaths") else "")]
            rules_out.append({"id": nid, "category": n.get("category"), "scope": n.get("scopeTag"),
                              "anchor": None, "section": "replaced", "text": body})
            continue
        anchor: Optional[Tuple[str, Dict[str, Any]]] = None
        old = prev.get(nid)
        if old and old.get("pins"):
            anchor = (old["pins"][0]["key"], old["pins"][0]["pin"])  # sticky: keep the recorded baseline
        else:
            found = _resolve_lesson_anchor(root, n)
            if found:
                key = f"{found[0]}::{found[1]}"
                pin = config_pins.get(key)
                if not pin:
                    fp = mw._current_fingerprint(root, found[0], found[1])
                    pin = {"ast": fp["ast"], "sig": fp.get("sig"), "req": fp.get("req"), "basis": fp.get("basis"), "types": fp.get("types"), "shape": fp.get("shape")}
                anchor = (key, pin)
        body, lines = _render_rule(n, anchor)
        dead = n.get("category") == "repeated_pitfall" and anchor is None
        (dead_lines if dead else lesson_lines).extend(lines)
        rules_out.append({"id": nid, "category": n.get("category"), "scope": n.get("scopeTag"),
                          "anchor": anchor[0] if anchor else None, "section": "dead-ends" if dead else "lessons",
                          "citation": f"{SNAPSHOT_REL}/{(n.get('primaryPin') or {}).get('span')}", "text": body})
    promoted = [r["id"] for r in rules_out]
    removed = sorted(set(prev) - set(promoted))
    if old_text is None and not promoted:
        return {"ok": True, "exitCode": 0, "page": PROMOTED_PAGE, "promoted": [], "removed": [], "rules": [],
                "op": "none", "notes": notes, "formatted": "- No approved, verified Dream lessons to promote.\n"}
    meta = {"schema": PROMOTED_SCHEMA, "queue": QUEUE_REL, "snapshot": SNAPSHOT_REL}
    out = ["# Dream-approved lessons", "",
           "<!-- Generated by Memlet from human-approved Dream items whose transcript citations still verify. Memlet "
           "rewrites this page on every approve/reject and refuses to overwrite a hand edit; keep notes elsewhere. -->",
           f"{_PAGE_META}{json.dumps(meta, sort_keys=True)} -->", "", _LESSON_HEADING, ""]
    out += lesson_lines or ["(none)"]
    if dead_lines:
        out += ["", _DEAD_END_HEADING, ""] + dead_lines
    if replaced_lines:
        out += ["", "## Replaced instructions (do not apply as current)", ""] + replaced_lines
    text = "\n".join(out) + "\n"
    if old_text is not None and mw._PAGE_STATUS_RE.sub("", old_text) == text:
        op = "unchanged"
    else:
        try:
            _write_text_atomic(page, text)
            _record_pages(root, [(PROMOTED_PAGE, mw._page_body_sha256(text), "dream_promote")])
        except (OSError, ValueError, sqlite3.Error) as exc:
            return _fail(f"cannot write {PROMOTED_PAGE}: {exc}", 1, page=PROMOTED_PAGE, promoted=[], removed=[])
        op = "update" if old_text is not None else "create"
    lines = [f"## Dream lessons promoted to `{PROMOTED_PAGE}` ({op})", ""]
    lines += [f"- `{r['id']}` {r['text']}" for r in rules_out] or ["- (none)"]
    lines += [f"- removed `{i}` (approval or citation no longer verifies)" for i in removed]
    return {"ok": True, "exitCode": 0, "page": PROMOTED_PAGE, "promoted": promoted, "removed": removed,
            "rules": rules_out, "op": op, "notes": notes, "formatted": "\n".join(lines) + "\n"}


@md.fresh_read
def promoted_rule_entries(root: Path, *, _counters: Optional[Dict[str, int]] = None,
                          _read_state: Optional[Any] = None, _path_context=None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(fresh, stale) rule-index-shaped entries for `memlet_recall`: a promoted rule is fresh only while its line is
    exactly the text Memlet renders from the approved Dream item, that item is still approved for the same content
    (approval digest), and its citations verify against the snapshot. Re-checked on every call; no model call."""
    state = md._active_read_state()
    _path_context = _valid_path_context(_path_context)
    rkey = Path(root).resolve()
    cached = md._read_cache("promoted_entries", state=state).get(rkey)
    if cached is not None:
        cached_fresh, cached_stale = cached
        return [dict(e) for e in cached_fresh], [dict(e) for e in cached_stale]
    fresh, stale = _promoted_rule_entries_scoped(root, _read_state=state, _path_context=_path_context)
    md._read_cache("promoted_entries", state=state)[rkey] = ([dict(e) for e in fresh], [dict(e) for e in stale])
    return fresh, stale


def _promoted_rule_entries_scoped(root: Path, *, _read_state: Optional[Any] = None, _path_context=None) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    page = root / PROMOTED_PAGE
    if _symlinked(root, (PROMOTED_PAGE,)) or not page.is_file():
        return [], []
    text = mw.read_regular_text(page, mw.MAX_WIKI_PAGE_BYTES, errors="replace")
    if text is None or _PAGE_META not in text:
        return [], []
    rules = _parse_promoted(text)
    if not rules:
        return [], []
    # The same ownership rule promotion applies before overwriting the page: unless the page is exactly what Memlet
    # last wrote (ledger hash), nothing on it is fresh -- a hand edit (e.g. a re-written anchor comment) is not
    # trusted until an explicit `accept` or promotion records it.
    if _page_edit_problem(root, text, for_reader=True):
        return [], [{"topic": "approved lesson · dream", "importance": 2, "source": f"{PROMOTED_PAGE}:{r['line']}",
                     "dreamId": r["id"]} for r in rules]
    try:
        graph, _why = _load_queue(root) if not _symlinked(root, (QUEUE_REL,)) else (None, "")
    except (OSError, ValueError):
        graph = None
    snap, _why = _load_snapshot(root)
    by_id = {str(n.get("id")): n for n in (graph or {}).get("nodes") or [] if isinstance(n, dict)}
    view_by_id = {str(n.get("id")): n for n in (md._approved_view(graph, _copy=False)[0].get("nodes") or [])} if graph else {}
    merges = merge_view(root, context=dict(graph=graph, snapshot=snap), _read_state=_read_state) if any("merge" in n for n in by_id.values()) else {"states": {}}
    hidden = {i for i, st in merges["states"].items() if st["state"] == "merged"}
    quarantined = {i["id"] for i in dream_items(root, _graph=graph, _read_state=_read_state, _path_context=_path_context)["items"] if i.get("quarantined")}
    fresh, stale = [], []
    for r in rules:
        node = by_id.get(r["id"])
        if r["id"] in hidden or r["id"] in quarantined or (node and md.superseded_lesson(node)):
            continue  # quarantine is never a wiki rule, even if explicitly code-anchored
        topic = f"approved lesson · {md.category_label((node or {}).get('category') or 'dream')}"
        imp = md._safe_imp((node or {}).get("importance"), 2)
        entry = {"topic": topic, "importance": max(1, min(3, imp)), "source": f"{PROMOTED_PAGE}:{r['line']}",
                 "dreamId": r["id"]}
        anchor = (r["pins"][0]["key"], r["pins"][0]["pin"]) if r["pins"] else None
        ok = (node is not None and snap is not None and _sha(r["body"])[:16] == r["textSha"]
              and md.approval_state(node, "node") == TIER_APPROVED and md.node_approval_digest(node) == r["digest"]
              and _render_rule(node, anchor)[0] == r["body"]  # the quoted text is exactly what was approved
              and (view_by_id.get(r["id"]) or {}).get("verdict") == "SUPPORTED"
              and _node_edges_verify(node, snap) is None)
        if ok:
            entry.update(quote=r["body"], lines=1, **_lesson_fields(node))
            fresh.append(entry)
        else:
            stale.append(entry)
    return fresh, stale


# ---------------------------------------------------------------------------------------------------------------
# unified recall


def _rules_items(root: Path, query: str, *, _read_state: Optional[Any] = None, _path_context=None) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Dict[str, Any]], List[str]]:
    """({"rules": query matches, "safety": always-on importance-1 rules}, their sources, notices).
    With a rule index: BM25 over its verified entries. Without one: the deterministic parsed fallback
    (`memlet_recall.parsed_rule_entries`), labelled as such and never presented as reviewed; it has no safety list."""
    from memlet import memlet_recall as mr
    from memlet.memlet_index import INDEX_PAGE
    mode = "index" if (root / INDEX_PAGE).is_file() else "parsed"
    # promoted Dream lessons are shown once, in the `dream` section, with their transcript citation
    promoted = [e["source"] for e in promoted_rule_entries(root, _read_state=_read_state, _path_context=_path_context)[0]]
    res, picked = mr.recall_items(root, query, k=mr.MAX_K, safety=(mode == "index"), exclude=promoted,
                                  fallback=True)
    notices = []
    if res.get("stale"):
        notices.append(f"{res['stale']} indexed rule(s) changed since the index was built and are not quoted")
    if (res.get("indexStale") or {}).get("stale"):
        notices.append("instruction files changed after the rule index was built; re-run `memlet admin index-build` (calls the `claude` CLI; a model call)")
    out: Dict[str, List[Dict[str, Any]]] = {"rules": [], "safety": [], "staleRules": []}
    for it in picked:
        head = f"- [{it['topic']}] `{it['source']}`" + (f" (sha256 {it['sha256'][:12]})" if it.get("sha256") else "")
        head += " " + suggested_label(it) + f"[{it['anchorStatus']}: {it['anchorReason']}]"
        text = "\n".join([head + ":"] + [f"  > {q}" for q in it["quote"].splitlines()])
        item = {"key": it["key"], "source": it["source"], "topic": it["topic"], "quote": it["quote"], "text": text,
                "anchorStatus": it["anchorStatus"], "anchorReason": it["anchorReason"], "anchors": it["anchors"]}
        for extra in ("dreamId", "sha256"):
            if it.get(extra):
                item[extra] = it[extra]
        out["staleRules" if it["anchorStatus"] == "stale" else "rules" if it.get("match") else "safety"].append(item)
    if mode == "parsed" and not res.get("spans") and not mr.parsed_rule_entries(root):
        rules_src = {"available": False, "mode": mode, "reason": "no rule index and no instruction files with rules"}
    else:
        rules_src = {"available": True, "mode": mode, "staleEntries": res.get("stale", 0), "staleAnchors": res.get("staleAnchors", 0),
                     "reason": "" if out["rules"] else "no rule matches the query"}
    if mode == "index":
        safety_src = {"available": True, "mode": mode, "reason": "importance-1 rules from the rule index, shown "
                      "regardless of the query (not query matches)"}
    else:
        safety_src = {"available": False, "mode": mode, "reason": "parsed rules are already being used; they carry no reviewed importance; "
                      "run `memlet admin index-build` (calls the `claude` CLI; a model call) for an always-on safety core"}
    return out, {"rules": rules_src, "safety": safety_src,
                 "staleRules": {"available": True, "reason": "anchored code changed"}}, notices


def _dream_items(root: Path, query: str, *, _read_state: Optional[Any] = None, _path_context=None) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[str]]:
    bad = _symlinked(root, (QUEUE_REL, SNAPSHOT_REL))
    if bad:
        return [], {"available": False, "reason": _refusing(bad)}, []
    q = root / QUEUE_REL
    if not q.is_file():
        return [], {"available": False, "reason": "no Dream queue (.memlet/dream/APPROVAL-QUEUE.json)"}, []
    snap = root / SNAPSHOT_REL
    if not snap.is_dir():
        return [], {"available": False, "reason": "no transcript snapshot to verify Dream pins; nothing returned"}, []
    try:
        graph, _qwhy = _load_queue(root)
        if graph is None:
            return [], {"available": False, "reason": "no Dream queue (.memlet/dream/APPROVAL-QUEUE.json)"}, []
        snap_files, snap_why = _load_snapshot(root)
        if snap_files is None:
            raise ValueError(snap_why)
        shared = dream_items(root, query=query, _read_state=_read_state, _path_context=_path_context)
        shared_ids = {i["id"] for i in shared["items"]}
        mixed = any(n.get("id") in shared_ids and any(md.approval_state(e, "edge") != TIER_APPROVED for e in n.get("edges", [])) for n in graph.get("nodes", []))
        if mixed:
            res = {"ok": True, "includedNodeIds": sorted(shared_ids), "pinVerification": "verified", "unverifiableItems": []}
        else:
            res = md.run_dream_recall(str(q), snapshot_path=str(snap), query_text=query, budget_tokens=100000,
                                      _read_state=_read_state, _graph=graph, _snapshot=snap_files)
    except (OSError, ValueError) as exc:
        return [], {"available": False, "reason": f"Dream snapshot or queue does not verify; nothing returned ({exc})"[:300]}, []
    if not res.get("ok"):
        return [], {"available": False, "reason": str(res.get("error") or "Dream recall failed")}, []
    read_graph = graph
    if mixed:
        read_graph = copy.deepcopy(graph)
        read_graph["nodes"] = [_approved_projection(n, snap_files) if n.get("id") in shared_ids else n for n in graph["nodes"]]
    view, stats = md._approved_view(md._citation_review_view(read_graph, snap_files), _copy=False)
    by_id = {md.sanitize_node_id(n.get("id"), ""): n for n in view.get("nodes") or [] if isinstance(n, dict)}
    items = []
    for nid in res.get("includedNodeIds") or []:
        n = by_id.get(nid)
        if n is None or n.get("verdict") not in md.ALLOWED_RECALL_VERDICTS:
            continue
        pp = n.get("primaryPin") if isinstance(n.get("primaryPin"), dict) else {}
        source = f"{SNAPSHOT_REL}/{pp.get('span')}"
        label = "SUPERSEDED — do not apply as current; " if n.get("statusKind") == "superseded_rule" else ""
        text = (f"- [{nid}] ({md.category_label(n.get('category'))}; {_defuse(n.get('scopeTag'), 48)}) {label}"
                f"{lesson_text(n)} — cites `{source}`")
        items.append({"id": nid, "category": n.get("category"), "statusKind": n.get("statusKind"),
                      "title": n.get("title"), "quote": lesson_text(n), "source": source, "text": text, **_lesson_fields(n)})
        if items[-1].get("quoteLines"):
            items[-1]["text"] = f"- [{nid}] ({md.category_label(n.get('category'))}) {label}\n" + _lesson_quote_block(items[-1])
    states = {it["id"]: it for it in dream_items(root, _read_state=_read_state, _path_context=_path_context)["items"]}
    present = {i["id"] for i in items}
    for nid, state in states.items():
        if nid not in present and any(md.approval_state(e, "edge") != TIER_APPROVED for e in by_id.get(nid, {}).get("edges", [])):
            cite = state["cites"][0]
            items.append(dict(state, source=SNAPSHOT_REL + "/" + cite["span"], quote=state["text"]))
    permitted = set(states)
    items = [it for it in items if it["id"] in permitted]
    for item in items:
        item.update(states[item["id"]])
        item["quote"] = item["text"]
        item["text"] = render_recall_lesson(item)
    promoted_by_id = {e["dreamId"]: e for e in promoted_rule_entries(root, _read_state=_read_state, _path_context=_path_context)[0]}
    entries = []
    for it in items:
        entry = promoted_by_id.get(it["id"])
        if entry is None:
            node = by_id[it["id"]]
            resolved = _resolve_lesson_anchor(root, node)
            quote = lesson_text(node)
            if resolved:
                quote += f" (`{resolved[0]}::{resolved[1]}`)"
            entry = {"quote": quote, "source": PROMOTED_PAGE + ":1"}
        entries.append(dict(entry))
    mw.classify_rule_quotes(root, entries, _read_state=_read_state)
    for item, entry in zip(items, entries):
        for key in ("anchorStatus", "anchorReason", "anchors"):
            item[key] = entry[key]
        item["text"] += f" [{entry['anchorStatus']}: {entry['anchorReason']}]"
    rules = mw._rules_from_files(root, mw.import_source_files(root, "auto"))
    mw._verify_rules(root, rules, mw._config_pins(root), pin_source="pinned", _read_state=_read_state)
    notices = replaced_rule_notices(root, rules, list(states.values()))
    failures = res.get("unverifiableItems", [])
    if failures:
        notices.append(md.citation_failure_notice(failures))
    return items, {"available": True, "reason": md.citation_failure_notice(failures),
                   "unapprovedCount": stats.get("pendingNodes", 0), "rejectedCount": stats.get("rejectedNodes", 0),
                   "pinVerification": res.get("pinVerification"), "unverifiableCount": len(failures),
                   "unverifiableNodeIds": [f["id"] for f in failures], "unverifiableItems": failures}, notices


@md.fresh_read
def replaced_rule_notices(root, rules, approved):
    from .handover_bridge import _rule_text
    texts = md.replaced_line_index(approved)[1]
    cache, notices = {}, []
    for rule in rules:
        if rule["source"] == PROMOTED_PAGE or not rule.get("anchors"):
            continue
        text = _rule_text(root, rule, cache)
        ids = texts.get(md.normalised_quote(text)) if text else None
        if ids:
            notices.append("Rule %s:%s is replaced by lesson %s; re-verify before use." %
                           (rule["source"], rule["line"], ", ".join(sorted(ids))))
    return notices


def _memory_items(store: Any, task: Any, query: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if store is None or not task:
        return [], {"available": False, "reason": "memory store recall needs both a store and a task"}
    try:
        if not _call_foreign(lambda: hasattr(store, "memory")):
            p = Path(str(store)).expanduser()
            from .compatibility import LEGACY_DATABASE_FILENAME
            dbs = ("memlet.sqlite3", LEGACY_DATABASE_FILENAME, "work.sqlite3", "work_tasks.sqlite3")
            if not p.is_dir() or not any((p / name).is_file() for name in dbs):  # never create a store by reading
                return [], {"available": False, "reason": f"memory store not found: {p}"}
            from memlet.standalone import StandaloneStore
            store = StandaloneStore(p)
        res = _call_foreign(lambda: store.memory(str(task), query or "", 6000))
    except Exception as exc:  # the store raises plain ValueError/sqlite errors with localized text
        return [], {"available": False, "reason": f"memory store recall failed: {type(exc).__name__}: {exc}"[:300]}
    items = []
    for it in ((res.get("retrieval") or {}).get("items") or []):
        if not isinstance(it, dict):
            continue
        iid = str(it.get("item_id") or it.get("id") or "")
        body = " ".join(str(it.get("excerpt") or it.get("content") or it.get("summary") or "").split())
        if not iid or not body:
            continue
        from memlet.memlet_brief import _mask
        body = _mask(body)
        body = body if len(body) <= 300 else body[:299] + "…"
        kind = str(it.get("kind") or "memory")
        items.append({"id": iid, "kind": kind, "source": f"store:{task}:{iid}", "text": f"- [{kind}] {body} (`{iid}`)",
                      "excerpt": body})
    return items, {"available": True, "reason": ""}


_HEAD = "## Memlet recall (deterministic; read the cited source before acting)"
_SECTION_HEADS = {
    "rules": "### Project rules matching the query (quoted verbatim from the instruction files; unchanged since indexed)",
    "rules-parsed": ("### Project rules matching the query (parsed from instruction files; not an indexed/reviewed "
                     "rule index — each cites file:lines and the SHA-256 of that text)"),
    "staleRules": "### anchored code changed — do not apply without re-reading the rule",
    "safety": "### Always-on safety rules (importance 1 in the rule index; shown regardless of the query)",
    "dream": "### Approved lessons (human-approved Dream items; transcript citations re-verified)",
    "memory": "### Task memory (bi-temporal store)",
    "hints": "### " + HINT_LABEL,
}


def _head_for(section: str, rules_mode: str) -> str:
    return _SECTION_HEADS["rules-parsed" if section == "rules" and rules_mode == "parsed" else section]


def _render_recall(chosen: Dict[str, List[Dict[str, Any]]], footer: Optional[str], rules_mode: str = "index", verification_notice: str = "") -> str:
    lines = [_HEAD]
    if verification_notice:
        lines.append("- WARNING: " + verification_notice)
    for s in _SECTIONS:
        if chosen.get(s):
            lines += ["", ("### " + hint_label(chosen[s]) + "\n" + HINT_HELP if s == "hints" else _head_for(s, rules_mode))] + [it["text"] for it in chosen[s]]
    if footer:
        lines += ["", footer]
    return "\n".join(lines) + "\n"


@md.fresh_read
@_attachment_reader
def unified_recall(repo_path=".", query="", *, budget=1200, store=None, task=None,
                   include=("rules", "dream", "memory"), files=None,
                   _counters: Optional[Dict[str, int]] = None,
                   _read_state: Optional[Any] = None, _path_context=None) -> Dict[str, Any]:
    """One budgeted response from three stores (no model call). The budget is split evenly across the sections that
    have candidates; whatever a section leaves unused flows to the others. `tokenEstimate <= tokenBudget` always."""
    with md.work_counters(_counters), md._enter_read_scope(_read_state) as state:
        return _unified_recall_scoped(repo_path, query, budget=budget, store=store, task=task,
                                      include=include, files=files, _read_state=state, _path_context=_path_context)


def _unified_recall_scoped(repo_path=".", query="", *, budget=1200, store=None, task=None,
                           include=("rules", "dream", "memory"), files=None,
                           _read_state: Optional[Any] = None, _path_context=None) -> Dict[str, Any]:
    try:
        root = mw._resolve_root(repo_path)
    except ValueError as exc:
        return _fail(str(exc), 2)
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        return _fail(f"budget must be an integer, got {budget!r}", 2)
    if isinstance(include, str):
        include = [s.strip() for s in include.split(",")]
    asked = set(include or ())
    if "rules" in asked:
        asked.update(("safety", "staleRules"))  # the always-on safety list belongs to the rules source
    if "dream" in asked:
        asked.add("hints")
    wanted = [s for s in _SECTIONS if s in asked]
    notices: List[str] = [f"unknown section ignored: {s}" for s in (include or ()) if s and s not in _SECTIONS]
    query = str(query or "")
    cands: Dict[str, List[Dict[str, Any]]] = {s: [] for s in _SECTIONS}
    sources: Dict[str, Dict[str, Any]] = {s: {"available": False, "reason": "not requested"} for s in _SECTIONS}
    rules_mode = "index"
    if "rules" in wanted:
        try:
            got, srcs, extra = _rules_items(root, query, _read_state=_read_state, _path_context=_path_context)
            cands["rules"], cands["safety"] = got["rules"], got["safety"]
            sources["rules"], sources["safety"] = srcs["rules"], srcs["safety"]
            cands["staleRules"], sources["staleRules"] = got["staleRules"], srcs["staleRules"]
            rules_mode = srcs["rules"]["mode"]
            if files is not None:
                wanted_files = set(_changed_files(root, None, files)[0])
                for section in ("rules", "safety", "staleRules"):
                    cands[section] = [i for i in cands[section] if any(
                        (a if isinstance(a, str) else a.get("anchor", "")).split("::", 1)[0] in wanted_files
                        for a in i.get("anchors", []))]
            notices += extra
        except (OSError, ValueError) as exc:
            sources["rules"] = {"available": False, "reason": f"rule recall failed: {exc}"}
            sources["safety"] = {"available": False, "reason": f"rule recall failed: {exc}"}
    if "dream" in wanted:
        cands["dream"], sources["dream"], extra = _dream_items(root, query, _read_state=_read_state, _path_context=_path_context)
        notices += extra
        if files is not None:
            selection = dream_items(root, files=files, _read_state=_read_state, _path_context=_path_context)
            notices.extend(n for n in selection["notices"] if n.startswith("outside repository"))
            selected = {it["id"] for it in selection["items"]}
            cands["dream"] = [it for it in cands["dream"] if it["id"] in selected]
        stale_dream = [it for it in cands["dream"] if it["anchorStatus"] == "stale"]
        cands["staleRules"].extend(stale_dream)
        cands["dream"] = [it for it in cands["dream"] if it["anchorStatus"] != "stale"]
        if stale_dream:
            sources["staleRules"] = {"available": True, "reason": "anchored code changed"}
    if "hints" in wanted:
        hints = dream_items(root, tiers=(TIER_HINT,), query=query, files=files, _read_state=_read_state, _path_context=_path_context)
        sources["hints"] = {"available": hints["ok"], "reason": "", "label": hint_label(hints["items"])}
        for hint in hints["items"]:
            citation = hint["cites"][0]
            src = "{file}:{start}-{end}".format(**citation)
            text = "- [hint; " + hint_item_label(hint) + "] " + hint["text"] + " — cites `" + src + "`"
            if hint.get("quoteLines"):
                text = "- [hint; " + hint_item_label(hint) + "]\n" + _lesson_quote_block(hint)
            cands["hints"].append(dict(hint, quote=hint["text"], text=text))
    if "memory" in wanted:
        cands["memory"], sources["memory"] = _memory_items(store, task, query)
    for s in _SECTIONS:
        sources[s]["candidates"] = len(cands[s])

    est = mw.estimate_tokens
    failures = sources["dream"].get("unverifiableItems", [])
    verification_notice = "\n- ".join([n for n in [md.citation_failure_notice(failures)] if n]
                                       + [n for n in notices if n.startswith("Rule ") and "is replaced by lesson" in n])
    attachment = attachment_metadata(_path_context or {})
    if attachment:
        verification_notice = "\n- ".join(filter(None, (verification_notice, attachment["attachmentReason"])))
    fixed_heading = _render_recall({}, None, rules_mode, verification_notice)
    unavailable = [f"{s} ({sources[s]['reason']})" for s in wanted if not sources[s]["available"]]
    footer = ("- Not consulted: " + "; ".join(unavailable)) if unavailable else None
    chosen: Dict[str, List[Dict[str, Any]]] = {s: [] for s in _SECTIONS}
    order: List[str] = []
    active = [s for s in _SECTIONS if cands[s] and s != "hints"]
    room = budget - est(fixed_heading)
    if active and room > 0:
        share = room // len(active)
        used = {s: 0 for s in _SECTIONS}
        nxt = {s: 0 for s in _SECTIONS}

        def _cost(s: str, item: Dict[str, Any]) -> int:
            return est(item["text"] + "\n") + (est("\n" + _head_for(s, rules_mode) + "\n") if not chosen[s] else 0)

        for s in active:  # pass 1: each section fills its own share
            while nxt[s] < len(cands[s]):
                c = _cost(s, cands[s][nxt[s]])
                if used[s] + c > share:
                    break
                chosen[s].append(cands[s][nxt[s]])
                order.append(s)
                used[s] += c
                nxt[s] += 1
        spare = room - sum(used.values())
        progress = True
        while progress:  # pass 2: unused budget flows to the sections that still have candidates
            progress = False
            for s in active:
                if nxt[s] < len(cands[s]):
                    c = _cost(s, cands[s][nxt[s]])
                    if c <= spare:
                        chosen[s].append(cands[s][nxt[s]])
                        order.append(s)
                        spare -= c
                        nxt[s] += 1
                        progress = True
    # Hints use only the remainder after every verified candidate has been served.
    if all(len(chosen[s]) == len(cands[s]) for s in _SECTIONS if s != "hints") and cands["hints"]:
        running_recall_tokens = est(_render_recall(chosen, None, rules_mode, verification_notice))
        hints_head_tokens = est("### " + hint_label(cands["hints"]) + "\n" + HINT_HELP)
        for hint in cands["hints"]:
            add_tokens = est(hint["text"]) + (hints_head_tokens if not chosen["hints"] else 0)
            if running_recall_tokens + add_tokens > budget:
                break
            running_recall_tokens += add_tokens
            chosen["hints"].append(hint)
            order.append("hints")
    text = _render_recall(chosen, footer, rules_mode, verification_notice)
    if est(text) > budget:
        text = _render_recall(chosen, None, rules_mode, verification_notice)
        footer = None
    while est(text) > budget and order:  # the estimator is not additive across joins: trim until it truly fits
        victim = "hints" if chosen["hints"] else order[-1]
        order.pop(len(order) - 1 - order[::-1].index(victim))
        chosen[victim].pop()
        text = _render_recall(chosen, None, rules_mode, verification_notice)
    budget_too_small = est(text) > budget
    if budget_too_small:
        text = ""
    omitted = {s: len(cands[s]) - len(chosen[s]) for s in _SECTIONS}
    for s in _SECTIONS:
        sources[s]["returned"] = len(chosen[s])
        sources[s]["omittedForBudget"] = omitted[s]
    sections = {s: [{k: v for k, v in it.items() if k != "text"} for it in chosen[s]] for s in _SECTIONS}
    unapproved, rejected = sources["dream"].get("unapprovedCount", 0), sources["dream"].get("rejectedCount", 0)
    if unapproved or rejected:
        notices.append(f"{unapproved} unapproved lesson(s), of which {len(chosen['hints'])} shown as unreviewed hint(s); "
                       f"{rejected} rejected item(s) excluded from the approved lessons group")
    res = {"ok": not budget_too_small, "exitCode": 2 if budget_too_small else 0, "command": "recall",
           "repoPath": str(root), "query": query, "sections": sections,
           "staleAnchors": len({a["anchor"] for it in cands["staleRules"] for a in it.get("anchors", []) if a["status"] == "stale"}), "sources": sources, "notices": notices,
           "unverifiableCount": len(failures), "unverifiableNodeIds": [f["id"] for f in failures],
           "unverifiableItems": failures,
           "tokenBudget": budget, "tokenEstimate": est(text), "formatted": text}
    if budget_too_small:
        res["error"] = f"budget {budget} is below the fixed recall heading and verification notice ({est(fixed_heading)} tokens)"
    return res


# ---------------------------------------------------------------------------------------------------------------
# accept_anchor / changed_check


def _wiki_pages(root: Path) -> Dict[str, str]:
    """Wiki pages through the shared bounded, contained iterator (`memlet_wiki.iter_wiki_pages`)."""
    return dict(mw.iter_wiki_pages(root))


def _load_config(root: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if md._active_read_state() is not None:
        rkey = Path(root).resolve()
        cached = md._read_cache("loaded_configs").get(rkey, md._CACHE_MISS)
        if cached is not md._CACHE_MISS:
            cfg, err = cached
            return (copy.deepcopy(cfg) if cfg is not None else None, err)
        cfg, err = mw._read_config(root)
        md._read_cache("loaded_configs")[rkey] = (cfg, err)
        md._read_cache("config_digests")[rkey] = (
            hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            if isinstance(cfg, dict) else f"err:{err}"
        )
        return (copy.deepcopy(cfg) if cfg is not None else None, err)
    return mw._read_config(root)


@md.fresh_read
def accept_anchor(repo_path=".", anchors=(), *, dry_run=False) -> Dict[str, Any]:
    """Re-pin exactly the named `path::symbol` anchors to their current hashes (config.json and every wiki page pin
    of that anchor); every other pin is left as it is. Unknown anchors are errors."""
    try:
        root = mw._resolve_root(repo_path)
    except ValueError as exc:
        return _fail(str(exc), 2)
    names = _ids(anchors)
    if not names:
        return _fail("at least one `path::symbol` anchor is required", 2, accepted=[], unknown=[], dryRun=bool(dry_run))
    bad = _symlinked(root, (mw.MEMLET_DIR, mw.CONFIG_PATH, mw.WIKI_DIR, PROMOTED_PAGE) + mw._LEDGER_FILES)
    if bad:
        return _fail(_refusing(bad), 2, accepted=[], unknown=[], dryRun=bool(dry_run))
    if dry_run:
        return _accept_locked(root, names, dry_run)
    try:
        # The whole read (config, pages) -> re-pin -> write (config, pages, promoted page) -> ledger runs under the
        # Memlet write lock that Dream promotion, approval and every wiki writer also hold: a page rendered from an
        # older generation can never replace a newer one (an approved lesson can no longer be lost to an accept).
        with mw.memlet_write_lock(root):
            return _accept_locked(root, names, dry_run)
    except (OSError, ValueError) as exc:
        return _fail(f"accept failed: {exc}", 1, accepted=[], unknown=[], dryRun=bool(dry_run))


def _accept_locked(root: Path, names: List[str], dry_run: bool) -> Dict[str, Any]:
    cfg, err = _load_config(root)
    if err:
        return _fail(err, 1, accepted=[], unknown=[], dryRun=bool(dry_run))
    pages = _wiki_pages(root)
    known = set((mw._validated_config_pins(cfg)[0] if cfg else {}).keys())
    for rel, src_text in mw._source_texts(root, mw.discover_instruction_files(root)):
        known.update(f"{a['path']}::{a['symbol']}" for a in mw._extract_anchors(src_text, rel))
    for rel, text in pages.items():
        known.update(f"{a['path']}::{a['symbol']}" for a in mw._extract_anchors(text, rel))
    unknown = [a for a in names if a not in known]
    accepted, refused, new_pins = [], [], {}
    for a in names:
        if a in unknown:
            continue
        path, symbol = a.rsplit("::", 1)
        fp = mw._current_fingerprint(root, path, symbol)
        if fp.get("resolution") != "exact" or not fp.get("ast"):
            refused.append({"anchor": a, "reason": fp.get("reason") or fp.get("resolution") or "unresolvable"})
            continue
        new_pins[a] = {"ast": fp["ast"], "sig": fp.get("sig"), "req": fp.get("req"), "basis": fp.get("basis"), "types": fp.get("types"), "shape": fp.get("shape")}
        accepted.append(a)
    changed_pages: Dict[str, Tuple[str, str]] = {}
    for rel, text in pages.items():
        def _sub(m: "re.Match[str]") -> str:
            key = f"{m.group('path')}::{m.group('symbol')}"
            if key not in new_pins:
                return m.group(0)
            return _pin_comment(key, new_pins[key]).strip()
        new = mw.PIN_RE.sub(_sub, text)
        page_keys = {f"{m.group('path')}::{m.group('symbol')}" for m in mw.PIN_RE.finditer(text)}
        if new != text or (rel == PROMOTED_PAGE and page_keys and page_keys <= set(new_pins)):
            # the promoted page is also re-recorded when every pin on it was just accepted, even if a hand edit had
            # already written those exact pins: the explicit accept is what makes its pins trusted again (A7)
            changed_pages[rel] = (text, new)
    res = {"ok": not unknown and not refused, "exitCode": 0 if not unknown and not refused else 1, "command": "accept",
           "accepted": accepted, "unknown": unknown, "refused": refused, "dryRun": bool(dry_run),
           "pagesUpdated": sorted(changed_pages), "pins": {a: new_pins[a] for a in accepted}}
    if accepted and not dry_run:
        try:
            if cfg is None:
                cfg = mw._base_config(mw._git_head(root), mw.discover_instruction_files(root))
            pins = mw._validated_config_pins(cfg)[0]
            for a in accepted:
                p = new_pins[a]
                pins[a] = {"astHash": p["ast"], "sigHash": p["sig"], "reqSigHash": p["req"], "hashBasis": p["basis"], "annotationHash": p.get("types"), "parameterShapeHash": p.get("shape")}
            cfg["pinnedAnchors"] = dict(sorted(pins.items()))
            cfg_path = root / mw.CONFIG_PATH
            _write_text_atomic(cfg_path, json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            last = mw._read_ledger_hashes(root / mw.LEDGER_PATH)
            events: List[Tuple[str, str, str]] = []
            for rel, (before, after) in sorted(changed_pages.items()):
                b_sha = mw._page_body_sha256(before)
                hand_edited = rel in last and last[rel] != b_sha
                if hand_edited:  # an unrecorded hand edit stays visible in the ledger
                    events.append((rel, b_sha, "user_edit"))
                if after != before:
                    _write_text_atomic(root / rel, after)
                keys = {f"{m.group('path')}::{m.group('symbol')}" for m in mw.PIN_RE.finditer(after)}
                # A hand-edited promoted page becomes Memlet's again only when every pin on it was explicitly
                # accepted now; otherwise another hand-edited pin would be trusted by readers (it stays a user edit).
                owned = not (rel == PROMOTED_PAGE and hand_edited and not keys <= set(accepted))
                events.append((rel, mw._page_body_sha256(after), "anchor_accept" if owned else "user_edit"))
            snaps = [mw.classify_anchor(root, {"path": a.rsplit("::", 1)[0], "symbol": a.rsplit("::", 1)[1],
                                               "pin": new_pins[a]}) for a in accepted]
            _record_pages(root, events, snaps)
        except (OSError, ValueError, sqlite3.Error) as exc:
            return _fail(f"accept failed: {exc}", 1, accepted=[], unknown=unknown, dryRun=bool(dry_run))
    errs = []
    if unknown:
        errs.append("unknown anchor(s): " + ", ".join(unknown))
    if refused:
        errs.append("cannot re-pin: " + ", ".join(f"{r['anchor']} ({r['reason']})" for r in refused))
    if errs:
        res["error"] = "; ".join(errs)
    lines = [f"## Memlet accept{' (dry run)' if dry_run else ''}", ""]
    lines += [f"- {'would re-pin' if dry_run else 're-pinned'} `{a}` ast={new_pins[a]['ast']}" for a in accepted]
    lines += [f"- UNKNOWN `{a}`" for a in unknown] + [f"- REFUSED `{r['anchor']}`: {r['reason']}" for r in refused]
    res["formatted"] = "\n".join(lines) + "\n"
    return res


def _git_z(root: Path, args: List[str]) -> Tuple[Optional[List[str]], str]:
    try:
        rc, stdout, stderr = md._run_owned_git(["git"] + args, cwd=str(root), capture_output=True, timeout=30, _run=subprocess.run)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git is unavailable ({exc}); pass the changed files explicitly"
    if rc != 0:
        err = (stderr.decode("utf-8", "replace").strip().splitlines() or [""])[0]
        return None, ("not a git repository or unknown base ref; pass the changed files explicitly: " + err)[:300]
    return [t for t in stdout.decode("utf-8", "surrogateescape").split("\0")], ""


@md.fresh_read
def normalise_lesson_files(root, files):
    """Exact spelling; resolve absolute ancestors without changing the leaf spelling."""
    root = Path(root).resolve()
    out, notices = [], []
    for raw in ([files] if isinstance(files, str) else files):
        text = str(raw).replace("\\", "/")
        p = Path(text)
        if p.is_absolute():
            try:
                text = (p.parent.resolve() / p.name).relative_to(root).as_posix()
            except ValueError:
                notices.append("outside repository file ignored: " + str(raw))
                continue
        text = text.removeprefix("./")
        if ".." in text.split("/"):
            notices.append("outside repository or traversal file ignored: " + str(raw))
            continue
        try:
            (root / text).parent.resolve().relative_to(root)
        except ValueError:
            notices.append("outside repository file ignored: " + str(raw))
            continue
        if text and text not in out:
            out.append(text)
    return out, notices


def _changed_files(root: Path, base: Optional[str], files: Optional[Iterable[str]]) -> Tuple[Optional[List[str]], Optional[str]]:
    """`files` as given (absolute paths inside the repo made relative), else `git diff --name-status -z <base or HEAD>`
    plus untracked files. NUL-delimited, so names are taken literally; both ends of a rename or copy and every deleted
    path count as changed (a rule citing the old path must be re-checked)."""
    if files is not None:
        if isinstance(files, str):
            files = [files]
        out: List[str] = []
        for raw in files:
            s = str(raw).strip()
            if not s:
                continue
            p = Path(s)
            if p.is_absolute():
                try:
                    s = p.resolve().relative_to(root).as_posix()
                except ValueError:
                    continue  # outside the repository: no rule here can reference it
            s = mw._clean_ref_path(s.replace("\\", "/"))
            if s and s not in out:
                out.append(s)
        return out, None
    ref = base or "HEAD"
    if ref.startswith("-"):
        return None, f"invalid base ref: {ref}"
    toks, err = _git_z(root, ["diff", "--name-status", "-z", "--relative", ref, "--"])
    if toks is None:
        return None, err
    out = []
    i = 0
    while i < len(toks):
        status = toks[i]
        if not status:
            i += 1
            continue
        n = 2 if status[:1] in ("R", "C") else 1
        for path in toks[i + 1:i + 1 + n]:
            if path and path not in out:
                out.append(path)
        i += 1 + n
    untracked, _err = _git_z(root, ["ls-files", "--others", "--exclude-standard", "-z"])
    for path in untracked or []:
        if path and path not in out:
            out.append(path)
    return out, None


def _memlet_state(root: Path, rel: str) -> bool:
    """Memlet's own generated state under `.memlet/` (config, ledger, Dream queue/DB/snapshot, generated pages and
    HTML, the promoted page). Hand-written `.memlet/wiki/**/*.md` pages are rule sources and are NOT ignored."""
    if rel != mw.MEMLET_DIR and not rel.startswith(mw.MEMLET_DIR + "/"):
        return False
    if not (rel.startswith(mw.WIKI_DIR + "/") and rel.endswith(".md")):
        return True
    if rel in mw._GENERATED_PAGES or rel == PROMOTED_PAGE:
        return True
    marker = mw._MARKED_GENERATED_PAGES.get(rel)
    if marker:
        p = root / rel
        return not p.is_file() or marker in mw._read_text(p)  # a deleted BRIEF/INDEX-RULES page is generated state
    return False


def _cheap_rule_sources(root: Path, cfg: Optional[Dict[str, Any]], changed: Iterable[str] = ()) -> List[str]:
    """Instruction files without walking the repository: fixed names, the instruction globs, slim outputs, the files
    `init` recorded in config.json and every changed path that discovery would accept (a new nested AGENTS.md
    included), each re-checked with `memlet_wiki.is_instruction_path`."""
    found = set()
    for rel in list(mw.TOP_INSTRUCTION_FILES) + mw.slim_output_files(root):
        if mw.is_instruction_path(root, rel):
            found.add(rel)
    for pat in mw.INSTRUCTION_GLOBS:
        for p in root.glob(pat):
            rel = mw._rel(root, p)
            if mw.is_instruction_path(root, rel):
                found.add(rel)
    for rel in list((cfg or {}).get("instructionFiles") or []) + list(changed):
        if isinstance(rel, str) and mw.is_instruction_path(root, rel):
            found.add(rel)
    return sorted(found)


@md.fresh_read
@_attachment_reader
def changed_check(repo_path=".", *, base=None, files=None, _read_state: Optional[Any] = None, _path_context=None) -> Dict[str, Any]:
    """Fast hook check: classify only the anchors / referenced paths of rules that touch the changed files (`files`,
    else `git diff --name-status <base or HEAD>` with both rename ends, plus untracked files). Memlet's own generated
    state is ignored (`ignoredFiles`); a deleted instruction file is listed in `removedInstructionFiles`.
    With no changed files, recorded pins are still checked for committed drift.
    exitCode 0 = nothing stale, 1 = stale."""
    try:
        root = mw._resolve_root(repo_path)
    except ValueError as exc:
        return _fail(str(exc), 2, changedFiles=[], affected=[], staleCount=0)
    listed, err = _changed_files(root, base, files)
    if err:
        return _fail(err, 2, changedFiles=[], affected=[], staleCount=0)
    ignored = [f for f in listed if _memlet_state(root, f)]
    changed = [f for f in listed if f not in ignored]
    removed = [f for f in changed if not (root / f).exists() and not (root / f).is_symlink()
               and (mw.instruction_path_shape(f) or (f.startswith(mw.WIKI_DIR + "/") and f.endswith(".md")))]
    res: Dict[str, Any] = {"ok": True, "exitCode": 0, "command": "check-changed", "repoPath": str(root),
                           "changedFiles": changed, "ignoredFiles": ignored, "removedInstructionFiles": removed,
                           "affected": [], "staleCount": 0, "formatted": ""}
    notes = [f"REMOVED instruction file `{f}`: its rules no longer apply; check nothing still relies on them\n"
             for f in removed]
    cfg, _err = _load_config(root)
    changed_set = set(changed)
    rules = mw._rules_from_files(root, _cheap_rule_sources(root, cfg, changed))
    page_pins: Dict[str, Dict[str, Any]] = {}
    for rel, text in mw.trusted_wiki_pages(root):  # same promoted-page ownership rule as handover
        for a in mw._extract_anchors(text, rel):
            if a["pin"]:
                page_pins.setdefault(f"{a['path']}::{a['symbol']}", a["pin"])
        if not mw._is_generated_page(rel, text):
            body = mw._STATUS_BLOCK_RE.sub(mw._blank_lines, mw._PAGE_STATUS_RE.sub(mw._blank_lines, text))
            page_rules = mw._extract_rules(body, rel)
            if rel == PROMOTED_PAGE:  # same rule as handover: only lessons that still verify are rules
                live = {int(e["source"].rsplit(":", 1)[1]) for e in promoted_rule_entries(root, _read_state=_read_state, _path_context=_path_context)[0]}
                page_rules = [r for r in page_rules if r["line"] in live]
            rules.extend(page_rules)
    mw._attach_sidecar(root, rules)  # wiki rules must acquire accepted anchors BEFORE path filtering
    touching = []
    for r in rules:
        paths = {mw._resolve_ref_path(root, r["source"], p) for p, _s in r["refs"]}
        if r["refs"] and (paths & changed_set or r["source"] in changed_set):
            touching.append(r)
    if touching:
        pins = mw._config_pins(root)
        pins.update(page_pins)
        anchors = mw._verify_rules(root, touching, pins, pin_source="pinned")
        by_key = {a["anchor"]: a for a in anchors}
        seen = set()
        for r in touching:
            keys = [k for k in r["anchors"] if by_key[k]["path"] in changed_set] or (
                r["anchors"] if r["source"] in changed_set else [])
            if not keys:
                continue
            worst = min(keys, key=lambda k: _STATUS_ORDER.index(by_key[k]["status"]))
            dedupe = (r["source"], r["line"], worst)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            a = by_key[worst]
            lesson_fields = {k: r[k] for k in ("quoteLines", "cites", "edgeSources") if k in r}
            from .vocabulary import LESSON_SEPARATOR
            rule_text = LESSON_SEPARATOR.join(mw._clip(q, 160) for q in r["quoteLines"]) if r.get("quoteLines") else mw._clip(r["text"], 160)
            res["affected"].append({"anchor": worst, "status": a["status"], "reason": a["reason"],
                                    "rule": rule_text, **lesson_fields, "source": f"{r['source']}:{r['line']}",
                                    **mw._suggested_fields(r, worst)})
    if not changed and (root / mw.CONFIG_PATH).is_file():
        res["pinnedAnchors"] = mw.pinned_anchor_report(root, _read_state=_read_state)
        shown = {a["anchor"] for a in res["affected"]}
        for a in res["pinnedAnchors"]["anchors"]:
            if not changed and a["pinned"] and a["status"] == "stale" and a["anchor"] not in shown:
                rows = a.get("rules") or [{"rule": "pinned anchor (including committed changes)",
                                           "source": ", ".join(a["sources"]) or mw.CONFIG_PATH}]
                res["affected"].extend(dict(row, anchor=a["anchor"], status=a["status"], reason=a["reason"]) for row in rows)
    # 9.1.1 scope matches are informational; they must never feed attachment,
    # export or denial decisions. Strict 9.2 path matches take precedence.
    from .memlet_dreamcycle import _canonical_path
    requested = {_canonical_path(f) for f in changed}
    attached_files = set(normalise_lesson_files(root, files if files is not None else changed)[0])
    path_context = lesson_path_context(root, _read_state=_read_state, context=_path_context)
    def matched_lessons(tier):
        rows = []
        for item in dream_items(root, _read_state=_read_state, tiers=(tier,), _path_context=path_context)["items"]:
            scope, text = item["scope"], item["text"]
            scope_path = _canonical_path(scope.split("::", 1)[0])
            scope_match = any(f == scope_path or (scope.endswith(("/", "\\")) and
                f.startswith(scope_path.rstrip("/") + "/")) or f + "::" in text or
                "`" + f + "`" in text for f in requested)
            path_match = bool(attached_files.intersection(path_context["paths"].get(item["id"], [])))
            if path_match or scope_match:
                rows.append(dict(item, via=LESSON_VIA_PATH if path_match else LESSON_VIA_SCOPE))
        return rows
    res["approvedLessons"] = matched_lessons(TIER_APPROVED)
    res["hints"] = matched_lessons(TIER_HINT)
    res["hintsLabel"] = hint_label(res["hints"])
    res["staleCount"] = sum(1 for a in res["affected"] if a["status"] == "stale")
    if res["staleCount"]:
        res.update(ok=False, exitCode=1, error=f"{res['staleCount']} rule(s) or recorded pins have stale anchors")
    res["formatted"] = "".join(f"{a['status'].upper()} `{a['anchor']}` ({a['reason']}) — {a['source']}: " +
                               (f"{suggested_label(a)}" + ("\n" + _lesson_quote_block(a) if a.get("quoteLines") else mw._clip(a['rule'], 100)) + "\n") for a in res["affected"]) + "".join(notes)
    if res["approvedLessons"]:
        res["formatted"] += "\n### Approved lessons\n" + "\n".join(render_approved_lesson(i) for i in res["approvedLessons"]) + "\n"
    if res["hints"]:
        res["formatted"] += "\n### " + res["hintsLabel"] + "\n" + HINT_HELP + "\n" + "\n".join("- [" + hint_item_label(h) + "]" + ("\n" + _lesson_quote_block(h) if h.get("quoteLines") else " " + h["text"]) for h in res["hints"]) + "\n"
    return res


# A phrase list, not a proof about semantics. Normalization closes spelling bypasses;
# unreviewed hints can still contain permission language not represented in this list.
_PERMISSION_RE = re.compile(
    r"\b(?:may|can\s+(?:skip|bypass|ignore|omit|drop|disable|force[-\s]?push)|"
    r"(?:it'?s|is|are)\s+(?:fine|ok|okay|acceptable|safe|allowed|permitted)|"
    r"(?:do\s+not|don't|doesn't|does\s+not)\s+(?:need|have)\s+to|"
    r"need\s+not|no\s+need\s+(?:to|for)|not\s+(?:required|necessary|mandatory|needed)|"
    r"unnecessary|can\s+be\s+(?:skipped|ignored)|feel\s+free|permitted|optional(?:ly)?|"
    r"(?:aren't|isn't)\s+required|needn't|(?:you're|you\s+are)\s+free\s+to|don't\s+bother|skippable|"
    r"use\s+--no-verify|skip\s+(?:the\s+)?(?:tests|ci)|push\s+directly\s+to\s+main|bypass\s+review|"
    r"allowed\s+to|(?:allow|permit)(?:s|ed|ing)?|go\s+ahead|ignoring\s+the\s+lint\s+errors\s+works)\b|"
    r"(?:해도|해\s*도|하지\s*않아도|안\s*해도|생략해도|건너뛰어도|꺼도)\s*(?:된다|됩니다|됨|돼|괜찮|좋|상관없)|"
    r"않아도\s*(?:돼|된다|됨|괜찮|무방)|안\s*\S+도\s*(?:돼|된다|됨|괜찮)|"
    r"무방|필수(?:가|는)?\s*(?:아니|아님)|강제하지\s*않|(?:스킵|생략|무시|패스)\s*(?:가능|해도|ok)|"
    r"(?:필요(?:가|는|도)?|안\s*해도)\s*(?:없|없는)|불필요|선택\s*사항|선택적|허용|"
    r"강제\s*푸시\s*가능|검토\s*없이\s*배포|생략할\s*것|건너뛸\s*수|할\s*수\s*있|무시하세요|건너뛰세요", re.I)
_UNSAFE_STRING = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|.)")


def unsafe_contributed_string(value):
    return bool(_UNSAFE_STRING.search(value)) or (not value.isascii() and any(_hidden_character(c) for c in value))


def _tag_character(c):
    return 0xE0000 <= ord(c) <= 0xE007F


def _hidden_character(c):
    return _format_character(c) or _tag_character(c)


def _format_character(c):
    # Python 3.9's Unicode table predates these Cf additions. Keep the screen
    # and display policy identical on both supported interpreters.
    return (unicodedata.category(c) == "Cf" or ord(c) in (0x0890, 0x0891)
            or 0x13439 <= ord(c) <= 0x1343F)


def hidden_hint_blocked(text):
    if text.isascii():
        return False
    return any(_tag_character(c) for c in text) or sum(_format_character(c) for c in text) > 3


def safe_lesson_display(text, limit=None):
    # Render only: never rewrite the stored quote or its hash.
    text = str(text)
    if not text.isascii():
        text = text.replace("⟶", "→")
        hidden = any(_hidden_character(c) for c in text)
        if hidden:
            text = "".join(c for c in text if not _hidden_character(c))
    else:
        hidden = False
    text = _ANSI.sub(" ", text)
    text = _UNSAFE_STRING.sub(" ", text)
    text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    if limit is not None:
        text = md.safe_truncate_masked(text, limit)
    return text + (" [contains hidden characters]" if hidden else "")


def _display_records(value):
    """Display copies of receipt evidence; stored receipts and hashes are untouched."""
    if isinstance(value, dict):
        return {k: _display_records(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_display_records(v) for v in value]
    return safe_lesson_display(value) if isinstance(value, str) else value


def permission_phrase(text):
    normalized = unicodedata.normalize("NFKC", str(text)).casefold()
    normalized = "".join(chr(ord(ch) - 0xE0000) if _tag_character(ch) else ch
                         for ch in normalized)
    normalized = "".join(ch for ch in normalized if not _format_character(ch)).casefold()
    for match in _PERMISSION_RE.finditer(normalized):
        prefix = normalized[max(0, match.start()-24):match.start()]
        suffix = normalized[match.end():match.end()+16]
        if (re.search(r"(?:never|do not|don't|must not|not(?:\s+be)?)\s*$", prefix)
                or re.match(r"\s+(?:not|never)\b", suffix)
                or re.match(r"\s*(?:하지\s*(?:않|마)|하지마)", suffix)):
            continue  # plain prohibitions such as 'Never allow unverified certificates'
        return True
    return False


def dream_settings(config):
    cfg = config.get("dream", {}) if isinstance(config, dict) else {}
    if not isinstance(cfg, dict):
        return False, "vendor", ["invalid dream settings: hints off; agreement defaults to vendor"]
    hints, agreement, notices = cfg.get("hints", True), cfg.get("agreement", "vendor"), []
    if type(hints) is not bool:
        hints = False
        notices.append("invalid dream.hints: expected boolean; hints off")
    if not isinstance(agreement, str) or agreement not in ("vendor", "model"):
        agreement = "vendor"
        notices.append("invalid dream.agreement: expected vendor or model; using vendor")
    return hints, agreement, notices





def _approved_node(node, snapshot, *, _preverified_proj=None):
    raw_edges = [dict(e) for e in (node.get("edges") or []) if isinstance(e, dict)]
    view, _ = md._approved_view({"nodes": [node], "edges": raw_edges}, _copy=False)
    n = view["nodes"][0]
    edges = n.get("edges", [])
    if n.get("verdict") not in ("SUPPORTED", "ALREADY_SUPERSEDED"):
        return False
    if any(e.get("edgeVerdict") == md.REJECTED for e in edges):
        return False
    if md.superseded_lesson(n) and not any(e.get("edgeType") == "SUPERSEDES" and e.get("edgeVerdict") == "VALID" for e in edges):
        return False
    if _preverified_proj is not None:
        valid_eids = {e.get("edgeId") for e in edges if e.get("edgeVerdict") == "VALID"}
        pre_edges = [e for e in _preverified_proj.get("edges", ()) if e.get("edgeId") in valid_eids]
        if pre_edges and len(pre_edges) == len(valid_eids):
            sink = _ACTIVE_TRUST_PROJ_SINK.get()
            if sink is not None:
                sink.append((_preverified_proj, pre_edges, valid_eids))
            return True
        proj_n = _approved_projection(n, snapshot)
        if not proj_n["edges"]:
            return False
        sink = _ACTIVE_TRUST_PROJ_SINK.get()
        if sink is not None:
            sink.append((_preverified_proj, proj_n["edges"], valid_eids))
        return True
    if not _approved_projection(n, snapshot)["edges"]:
        return False
    return not _node_edges_verify(_approved_projection(node, snapshot), snapshot)


def _compute_tier(node, *, snapshot, config):
    """Computed on read. Receipts are integrity checks, never signatures or approvals."""
    from memlet.memlet_dreamcycle import evidence_pins
    if not isinstance(node, dict):
        return TIER_PENDING
    decision = md.approval_state(node, "node")
    if decision == TIER_REJECTED or node.get("verdict") == md.REJECTED:
        return TIER_REJECTED
    try:
        if not isinstance(snapshot, dict):
            return TIER_PENDING
        if decision == TIER_APPROVED:
            proj_approved = _approved_projection(node, snapshot)
            if md._verify_item_pins(proj_approved, "node", snapshot):
                return TIER_PENDING
        else:
            proj_approved = None
            if _node_edges_verify(node, snapshot):
                return TIER_PENDING
        edges = node.get("edges", [])
        if any(md.approval_state(e, "edge") == TIER_REJECTED or e.get("edgeVerdict") == md.REJECTED for e in edges):
            return TIER_PENDING
        if decision == TIER_APPROVED:
            return TIER_APPROVED if _approved_node(node, snapshot, _preverified_proj=proj_approved) else TIER_PENDING
        if not dream_settings(config)[0]:
            return TIER_PENDING
        if md.lesson_quote_digest(node) in config.get("_dreamRejectedQuoteHashes", ()):
            return TIER_PENDING
        if not edges or not {e.get("edgeType") for e in edges} <= {"BLOCKS", "RESOLVES"}:
            return TIER_PENDING
        pins = evidence_pins(node)
        if any(md._verify_pin_dict(pin, snapshot, "evidence") for pin in pins):
            return TIER_PENDING
        texts = [p["quoteText"] for p in pins]
        quote_lines = {(p["fileId"], p["startLine"] + i): line for p in pins
                       for i, line in enumerate(p["quoteLines"])}
        if hidden_hint_blocked("\n".join(quote_lines.values())):
            return TIER_PENDING
        texts += [node.get("title", ""), node.get("scopeTag", "")] + [e.get("targetLabel", "") for e in edges]
        if any(permission_phrase(t) for t in texts):
            return TIER_PENDING
        if _agreement_basis(node, config):
            return TIER_HINT
    except (TypeError, ValueError, KeyError, AttributeError):
        pass
    return TIER_PENDING


def tier_config(graph, config):
    config = dict(config or {})
    config["_dreamRejectedQuoteHashes"] = {md.lesson_quote_digest(n)
        for n in (graph or {}).get("nodes", []) if isinstance(n, dict)
        and (md.approval_state(n, "node") == TIER_REJECTED or n.get("verdict") == md.REJECTED
             or any(md.approval_state(e, "edge") == TIER_REJECTED or e.get("edgeVerdict") == md.REJECTED
                    for e in n.get("edges", [])))}
    return config


@md.fresh_read
def item_trust(node, *, snapshot, config):
    """The sole tier/basis/label computation; callers share a graph-bound config."""
    return _item_trust(node, snapshot=snapshot, config=config)


def _item_trust(node, *, snapshot, config):
    from memlet.memlet_dreamcycle import trusted_record
    tier = _compute_tier(node, snapshot=snapshot, config=config)
    basis = _agreement_basis(node or {}, config)
    vetoes = [VETO_TEMPLATE % (r["vendor"], r["model"], r.get("verdict")) + ". " + VERDICT_HELP.get(r.get("verdict"), "")
              for r in (node or {}).get("verification", [])
              if trusted_record(r, node) and r.get("verdict") != "SUPPORTED"]
    label = "; ".join(vetoes) if vetoes else (hint_item_label({"agreementBasis": basis}) if basis else "no trusted agreement")
    if tier == TIER_PENDING and not vetoes and dream_settings(config)[1] == "vendor":
        from memlet.vendor_cli import same_model
        proposers = [r for r in (node or {}).get("proposers", []) if trusted_record(r, node)]
        if any(r.get("verdict") == "SUPPORTED" and trusted_record(r, node)
               and any(p["vendor"] == r["vendor"] and not same_model(p["vendor"], p["model"], r["vendor"], r["model"])
                       for p in proposers) for r in (node or {}).get("verification", [])):
            label = SAME_VENDOR_REVIEW
    return {"tier": tier, "agreementBasis": basis, "agreementLabel": label}


@md.fresh_read
def item_tier(node, *, snapshot, config):
    return item_trust(node, snapshot=snapshot, config=config)["tier"]


def _approved_projection(node, snapshot):
    return dict(node, edges=[e for e in node.get("edges", [])
                if md.approval_state(e, "edge") == TIER_APPROVED and e.get("edgeVerdict") == "VALID"
                and md._verify_item_pins(e, "edge", snapshot or {}) is None])


@md.fresh_read
def dream_tiers(graph, *, snapshot, config, _counters: Optional[Dict[str, int]] = None,
                _read_state: Optional[Any] = None, _out_projections: Optional[Dict[str, Dict[str, Any]]] = None):
    with md.work_counters(_counters), md._enter_read_scope(_read_state):
        config = tier_config(graph, config)
        approved_graph = md._approved_view(graph or {}, _copy=False)[0]
        sup_index = md._supersession_index(approved_graph)
        result = {}
        seen_ids = set()
        dup_ids = set()
        for node in (graph or {}).get("nodes", []):
            if not isinstance(node, dict):
                continue
            nid = str(node["id"])
            if nid in seen_ids:
                dup_ids.add(nid)
                if _out_projections is not None:
                    _out_projections.pop(nid, None)
            else:
                seen_ids.add(nid)
            proj_box: List[Any] = []
            tok_sink = _ACTIVE_TRUST_PROJ_SINK.set(proj_box)
            try:
                state = item_trust(node, snapshot=snapshot, config=config)
            finally:
                _ACTIVE_TRUST_PROJ_SINK.reset(tok_sink)
            proj = proj_box[0][0] if proj_box else _approved_projection(node, snapshot)
            if _out_projections is not None and nid not in dup_ids and proj_box:
                _out_projections[nid] = proj_box[0]
            display_node = md._supersession_pair(proj, approved_graph, _index=sup_index)
            if state["tier"] == TIER_APPROVED and display_node is not proj and _node_edges_verify(display_node, snapshot or {}):
                state["tier"] = TIER_PENDING
            result[nid] = state
        return result


def _merge_nodes(graph, trust, snapshot=None):
    """Call-local verified tiers and relations; never persisted or digested."""
    nodes, duplicates = {}, set()
    replacements = {n["id"] for n in (graph or {}).get("nodes", []) if md.superseded_lesson(n)}
    for edge in list((graph or {}).get("edges", [])) + [e for n in (graph or {}).get("nodes", []) for e in n.get("edges", [])]:
        if edge.get("edgeType") == "SUPERSEDES":
            replacements.update((edge.get("fromId"), edge.get("toId")))
    for n in (graph or {}).get("nodes", []):
        ident = str(n["id"])
        if ident in nodes:
            duplicates.add(ident)
            if "merge" in nodes[ident]:
                continue  # retain a record to explain why all copies remain visible
        nodes[ident] = dict(n, **trust.get(ident, {}))
        if snapshot is None:
            nodes[ident].pop("tier", None)
        nodes[ident]["_mergeRelations"] = {e["edgeType"] for e in _approved_projection(n, snapshot).get("edges", [])}
        nodes[ident]["_mergeReplacement"] = ident in replacements
    for ident in duplicates:
        nodes[ident]["_mergeDuplicate"] = True
    return nodes


def merge_state(node, nodes_by_id):
    """Pure holds predicate, requiring the shared reader's verified tiers.

    Cycle participants lapse; upstream records are judged on their own merits.
    Missing verification context fails open, never using stored approval alone.
    """
    from .vocabulary import MERGE_SCHEMA
    none = {"state": "none", "into": None, "reason": None}
    chain, visited = [], {}
    current = node
    while True:
        ident = str(current.get("id"))
        if ident in visited:
            cycle_start = visited[ident]
            if cycle_start == 0:
                return {"state": "lapsed", "into": node["merge"].get("into"), "reason": "merge-cycle"}
            chain = chain[:cycle_start]
            result = {"state": "lapsed", "into": None, "reason": "merge-cycle"}
            break
        rec = current.get("merge")
        if rec is None:
            result = none
            break
        into = rec.get("into") if isinstance(rec, dict) else None
        reason = None
        if (not isinstance(rec, dict) or rec.get("schema") != MERGE_SCHEMA
                or not isinstance(into, str) or not into.strip()
                or not isinstance(rec.get("by"), str) or not rec["by"].strip()
                or not md._valid_utc_stamp(rec.get("at"))):
            reason = "invalid-record"
        elif current.get("_mergeDuplicate") or nodes_by_id.get(into, {}).get("_mergeDuplicate"):
            reason = "duplicate-id"
        elif md.merge_approval_digest(current) != rec.get("memberDigest"):
            reason = "member-changed"
        elif into not in nodes_by_id:
            reason = "keep-missing"
        else:
            kept = nodes_by_id[into]
            if "tier" not in current or "tier" not in kept:
                reason = "unverified-read"
            elif md.merge_approval_digest(kept) != rec.get("keepDigest"):
                reason = "keep-changed"
            elif current.get("tier") == TIER_REJECTED or current.get("verdict") == md.REJECTED:
                reason = "member-rejected"
            elif kept.get("tier") == TIER_REJECTED or kept.get("verdict") == md.REJECTED:
                reason = "keep-rejected"
            elif (current.get("_mergeReplacement") or kept.get("_mergeReplacement")
                  or md.superseded_lesson(current) or md.superseded_lesson(kept)):
                reason = "supersedes"
            elif not current.get("_mergeRelations", set()) <= kept.get("_mergeRelations", set()):
                reason = "relation-types"
            elif current["tier"] == TIER_APPROVED and kept["tier"] != TIER_APPROVED:
                reason = "keep-not-approved"
        if reason:
            result = {"state": "lapsed", "into": into if isinstance(into, str) and into.strip() else None, "reason": reason}
            break
        visited[ident] = len(chain)
        chain.append(into)
        current = nodes_by_id[into]
    for into in reversed(chain):
        result = ({"state": "lapsed", "into": into, "reason": "keep-is-member"}
                  if result["state"] == "merged" else {"state": "merged", "into": into, "reason": None})
    return result


def _merge_context(repo_path, context=None):
    context = context if context is not None else {}
    root = mw._resolve_root(repo_path)
    bad = _symlinked(root, (QUEUE_REL, SNAPSHOT_REL, mw.CONFIG_PATH))
    if bad:
        raise ValueError(_refusing(bad))
    if "graph" not in context:
        context["graph"] = _load_queue(root)[0] or {"nodes": [], "edges": []}
    if "snapshot" not in context:
        context["snapshot"] = _load_snapshot(root)[0]
    if "config" not in context:
        context["config"] = _load_config(root)[0]
    if "trust" not in context or context.get("loaded") is False:
        context["trust"] = dream_tiers(context["graph"], snapshot=context["snapshot"], config=context["config"], _read_state=md._active_read_state())
    context["loaded"] = True
    return root, context


@md.fresh_read
def merge_view(repo_path=".", *, context=None, _read_state=None):
    """Read-only merge states using the same current tiers as dream_items."""
    try:
        _root, context = _merge_context(repo_path, context)
        if "mergeView" in context:
            return context["mergeView"]
        nodes = _merge_nodes(context["graph"], context["trust"], context["snapshot"])
        context["mergeNodes"] = nodes
        states = {ident: merge_state(node, nodes) for ident, node in nodes.items()}
        members = {}
        for ident, state in states.items():
            if state["state"] == "merged":
                members.setdefault(state["into"], []).append(ident)
        context["mergeView"] = {"ok": True, "states": states, "members": {k: sorted(v) for k, v in members.items()}}
        return context["mergeView"]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"ok": False, "states": {}, "members": {}, "error": str(exc)}


@md.fresh_read
def merge_blockers(repo_path=".", keep="", same=(), *, context=None, _read_state=None):
    """Read-only, whole-batch eligibility and lost reach; never changes a tier."""
    wanted = _ids(same)
    keep = str(keep or "").strip()
    try:
        root, context = _merge_context(repo_path, context)
        graph, trust, snapshot = context["graph"], context["trust"], context["snapshot"]
        view = merge_view(root, context=context, _read_state=md._active_read_state())
        nodes = context["mergeNodes"]
        if not view["ok"]:
            raise ValueError(view["error"])
        kept = nodes.get(keep)
        def relations(n):
            return (n or {}).get("_mergeRelations", set())
        path_context = _valid_path_context(context if isinstance(context, _LessonPathContext) else context.get("pathContext"))
        if path_context is None:
            path_context = lesson_path_context(root, _read_state=md._active_read_state())
            context["pathContext"] = path_context
        path_context.update(loaded=True, snapshot=snapshot, config=context["config"], trust=trust)
        def reach(n):
            if not n or n["tier"] not in (TIER_APPROVED, TIER_HINT):
                return set()
            display = _approved_projection(n, snapshot) if n["tier"] == TIER_APPROVED else n
            return set(lesson_paths(n, root, context=path_context) + _lesson_literal_paths(display, root, path_context))
        keep_paths, blockers, lost = reach(kept), {}, {}
        for ident in wanted:
            node, reasons = nodes.get(ident), []
            if not kept or not node:
                reasons.append("unknown-id")
            if (kept or {}).get("_mergeDuplicate") or (node or {}).get("_mergeDuplicate"):
                reasons.append("duplicate-id")
            if ident == keep:
                reasons.append("same-as-keep")
            if kept and kept["tier"] == TIER_REJECTED:
                reasons.append("keep-rejected")
            if node and node["tier"] == TIER_REJECTED:
                reasons.append("member-rejected")
            if node and kept and node["tier"] == TIER_APPROVED and kept["tier"] != TIER_APPROVED:
                reasons.append("keep-not-approved")
            if view["states"].get(keep, {}).get("state") == "merged":
                reasons.append("keep-is-member")
            if view["members"].get(ident):
                reasons.append("member-has-members")
            if (kept or {}).get("_mergeReplacement") or (node or {}).get("_mergeReplacement"):
                reasons.append("supersedes")
            if node and kept and not relations(node) <= relations(kept):
                reasons.append("relation-types")
            if reasons:
                blockers[ident] = reasons
            missing = sorted(reach(node) - keep_paths)
            if missing:
                lost[ident] = missing
        if not keep or not wanted:
            blockers[keep] = ["keep-required" if not keep else "same-required"]
        return {"ok": True, "keep": keep, "blockers": blockers, "lostFiles": lost}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"ok": False, "keep": keep, "blockers": {}, "lostFiles": {}, "error": str(exc)}


def _agreement_basis(node, config):
    from memlet.memlet_dreamcycle import trusted_record
    from memlet.vendor_cli import same_model
    records = [r for r in node.get("proposers", []) if trusted_record(r, node)]
    verdicts = [r for r in node.get("verification", []) if trusted_record(r, node)]
    if any(r.get("verdict") != "SUPPORTED" for r in verdicts):
        return None  # every trusted disagreement on this exact content vetoes a hint
    support = records + verdicts
    vendor_agreement = any(a["vendor"] != b["vendor"] for a in records for b in support)
    if dream_settings(config)[1] == "model":
        if any(not same_model(a["vendor"], a["model"], b["vendor"], b["model"]) for a in records for b in support):
            return "vendor" if vendor_agreement else "model"
        return None
    return "vendor" if vendor_agreement else None


def hint_label(items):
    return HINT_LABEL


def hint_item_label(item):
    return item.get("label") or item.get("agreementLabel") or (MODEL_AGREEMENT if item.get("agreementBasis") == "model" else VENDOR_AGREEMENT)


@md.fresh_read
def dream_items(repo_path=".", *, tiers=(TIER_APPROVED,), query="", files=None, _graph=None, include_merged=False,
                _counters: Optional[Dict[str, int]] = None, _read_state: Optional[Any] = None, _path_context=None):
    """Fixed in-repo snapshot, per-item rehash, disjoint tiers; rejected items never leave.

    File-filtered hints use current literal citations only. Their optional
    ``paths`` field is display-only, never approval or guard authority. Calls
    without ``files`` retain the legacy hint output without a paths field.
    """
    with md.work_counters(_counters), md._enter_read_scope(_read_state) as state:
        return _dream_items_reader(repo_path, tiers=tiers, query=query, files=files, _graph=_graph,
                                   include_merged=include_merged, _read_state=state, _path_context=_path_context)


def _dream_items_reader(repo_path=".", *, tiers=(TIER_APPROVED,), query="", files=None, _graph=None,
                        include_merged=False, _read_state: Optional[Any] = None, _path_context=None):
    _path_context = _valid_path_context(_path_context)
    read_state = _read_state if _read_state is not None else md._active_read_state()
    if (read_state is not None and _graph is None and files is None and query == ""
            and type(tiers) in (list, tuple, set, frozenset)
            and all(type(tier) is str for tier in tiers)):
        try:
            rkey = mw._resolve_root(repo_path)
        except Exception:
            rkey = None
        if rkey is not None and _path_context is None:
            q_dig = md._read_cache("queue_digests", state=read_state).get(rkey)
            s_dig = md._read_cache("snapshot_digests", state=read_state).get(rkey)
            c_dig = md._read_cache("config_digests", state=read_state).get(rkey)
            if q_dig is not None and s_dig is not None and c_dig is not None:
                ckey = (rkey, q_dig, s_dig, c_dig, tuple(tiers), bool(include_merged))
                cached_res = md._read_cache("dream_items_cache", state=read_state).get(ckey)
                if cached_res is not None:
                    return copy.deepcopy(cached_res)
            res_computed = _dream_items_reader_uncached(
                repo_path, tiers=tiers, query=query, files=files, _graph=_graph, include_merged=include_merged, _path_context=_path_context
            )
            q_dig = md._read_cache("queue_digests", state=read_state).get(rkey)
            s_dig = md._read_cache("snapshot_digests", state=read_state).get(rkey)
            c_dig = md._read_cache("config_digests", state=read_state).get(rkey)
            if q_dig is not None and s_dig is not None and c_dig is not None:
                md._read_cache("dream_items_cache", state=read_state)[
                    (rkey, q_dig, s_dig, c_dig, tuple(tiers), bool(include_merged))
                ] = res_computed
            return copy.deepcopy(res_computed)
    res_computed = _dream_items_reader_uncached(
        repo_path, tiers=tiers, query=query, files=files, _graph=_graph, include_merged=include_merged, _path_context=_path_context
    )
    return res_computed


def _dream_items_reader_uncached(repo_path=".", *, tiers=(TIER_APPROVED,), query="", files=None, _graph=None, include_merged=False, _path_context=None):
    from memlet.memlet_dreamcycle import evidence_pins, _canonical_path
    counts = {TIER_APPROVED: 0, TIER_HINT: 0, TIER_PENDING: 0}
    try:
        root = mw._resolve_root(repo_path)
        bad = _symlinked(root, (QUEUE_REL, SNAPSHOT_REL, mw.CONFIG_PATH))
        if bad:
            raise ValueError(bad)
        graph, why = (_load_queue(root) if _graph is None else (_graph, ""))
        snapshot, snap_why = _load_snapshot(root)
        config, cfg_why = _load_config(root)
        if graph is None:
            return {"ok": True, "items": [], "counts": counts, "notices": [why] + dream_settings(config)[2]}
        config = tier_config(graph, config)
        verified_projs: Dict[str, Dict[str, Any]] = {}
        trust = dream_tiers(graph, snapshot=snapshot, config=config, _read_state=md._active_read_state(), _out_projections=verified_projs)
        merges = (merge_view(root, context=dict(graph=graph, snapshot=snapshot, config=config, trust=trust), _read_state=md._active_read_state())
                  if any("merge" in n for n in graph.get("nodes", [])) else {"states": {}, "members": {}})
        hidden = {i for i, st in merges["states"].items() if st["state"] == "merged"}
        approved_graph = md._approved_view(graph, _copy=False)[0]
        approved_graph = dict(approved_graph, nodes=[n for n in approved_graph.get("nodes", [])
                              if trust.get(str(n.get("id")), {}).get("tier") == TIER_APPROVED and (include_merged or n.get("id") not in hidden)])
        live_ids = {n["id"] for n in approved_graph["nodes"]}
        approved_graph["edges"] = [e for e in approved_graph.get("edges", [])
                                   if e.get("fromId") in live_ids or e.get("toId") in live_ids]
        def _reuse_node_proj(n_view):
            entry = verified_projs.get(str(n_view.get("id")))
            if entry is not None:
                _raw_p, v_edges, single_valid_eids = entry
                full_valid_eids = {e.get("edgeId") for e in n_view.get("edges", ()) if md.approval_state(e, "edge") == TIER_APPROVED and e.get("edgeVerdict") == "VALID"}
                if full_valid_eids == single_valid_eids:
                    v_by_eid = {e.get("edgeId"): e for e in v_edges}
                    return dict(n_view, edges=[e for e in n_view.get("edges", ()) if e.get("edgeId") in v_by_eid])
            return _approved_projection(n_view, snapshot)
        approved_graph["nodes"] = [_reuse_node_proj(n) for n in approved_graph["nodes"]]
        sup_index = md._supersession_index(approved_graph)
        projection = md._supersession_projection(approved_graph, _index=sup_index)
        path_context = lesson_path_context(root, _read_state=md._active_read_state(), context=_path_context)
        path_context.update(loaded=True, snapshot=snapshot, config=config, trust=trust)
        items, notices = [], dream_settings(config)[2]
        query_terms = md._query_terms(str(query))
        requested = None
        if files is not None:
            requested, file_notices = normalise_lesson_files(root, files)
            notices.extend(file_notices)
        for node in graph.get("nodes", []):
            if not isinstance(node, dict):
                continue
            item_state = trust[str(node["id"])]
            tier = item_state["tier"]
            if tier == TIER_REJECTED or (node["id"] in hidden and not include_merged):
                continue
            if tier == TIER_PENDING and md.approval_state(node, "node") == TIER_APPROVED:
                notices.append(node["id"] + ": pending; approval has no live approved-valid edge, a rejected edge, or stale citations")
            if tier == TIER_PENDING and md.lesson_quote_digest(node) in config["_dreamRejectedQuoteHashes"]:
                notices.append(node["id"] + ": pending; " + ("primary quote matches" if len(md.lesson_lines(node)) == 1 else "full cited quote set matches") + " a rejected item; explicit human approval is required")
            if tier in tiers and item_state["agreementLabel"].startswith(VETO_PREFIX):
                notices.append(node["id"] + ": " + item_state["agreementLabel"])
            counts[tier] += 1
            if tier not in tiers:
                continue
            verified_entry = verified_projs.get(str(node["id"])) if tier == TIER_APPROVED else None
            verified_proj = verified_entry[0] if verified_entry is not None else None
            display_node = md._supersession_display_node((verified_proj if verified_proj is not None else _approved_projection(node, snapshot)) if tier == TIER_APPROVED else node, approved_graph, projection, _index=sup_index)
            text = lesson_text(display_node)
            scope = safe_lesson_display(node.get("scopeTag") or "")
            if query_terms and not query_terms & md._query_terms(text + " " + scope):
                continue
            attached = lesson_paths(node, root, context=path_context) if tier == TIER_APPROVED else []
            literal = _lesson_literal_paths(display_node, root, path_context) if tier in (TIER_APPROVED, TIER_HINT) else []
            selected_paths = list(dict.fromkeys(attached + literal))
            path_context["paths"][node["id"]] = selected_paths
            if tier == TIER_HINT:
                path_context.setdefault("hintPaths", {})[node["id"]] = literal
            # Preserve no-files hint bytes while sharing the same selection
            # with recall and graph; human attachments remain approved-only.
            paths = selected_paths if node.get("approvedPaths") or requested is not None else []
            inactive = [p for p in node.get("approvedPaths", []) if p not in attached]
            if inactive and tier == TIER_APPROVED:
                notices.append(node["id"] + INACTIVE_APPROVAL_PATHS + ", ".join(inactive))
            if requested is not None and not any(f in selected_paths for f in requested):
                continue
            cites, seen = [], set()
            for pin in evidence_pins(display_node):
                if not isinstance(pin, dict) or pin.get("span") in seen:
                    continue
                parsed = md.parse_span(str(pin.get("span", "")))
                if not parsed:
                    continue
                seen.add(pin["span"])
                f, a, b = parsed
                cites.append({"file": SNAPSHOT_REL + "/" + f, "start": a, "end": b,
                              "sha256": pin.get("sha256"), "verified": snapshot is not None and md._verify_pin_dict(pin, snapshot, "cite") is None})
            if not cites:
                continue
            it = {"id": node["id"], "tier": tier, "category": node["category"],
                  "statusKind": node.get("statusKind"),
                  "edgeTypes": sorted({e["edgeType"] for e in display_node.get("edges", [])}), "text": text,
                  "cites": cites, "scope": scope, "proposers": _display_records(node.get("proposers", [])),
                  "verification": _display_records(node.get("verification", []))}
            if paths:
                it["paths"] = paths
            if node.get("approvedPaths"):
                it["approvalPaths"] = node["approvedPaths"]
            fields = _lesson_fields(display_node)
            if fields:
                # Display hashes are per-line projections of stored pins, never fresh trust baselines.
                pins_verify = True if (verified_proj is not None and display_node is verified_proj) else (snapshot is not None and _node_edges_verify(display_node, snapshot) is None)
                fields["cites"] = [dict(c, verified=bool(pins_verify and
                    (md._resolve_pin(snapshot, c["span"]) or {}).get("sha256") == c["sha256"])) for c in fields["cites"]]
                it.update(fields)
            it.update(item_state)
            if merges["members"].get(node["id"]):
                it["mergedFrom"] = merges["members"][node["id"]]
            if include_merged and node["id"] in hidden:
                it["mergedInto"] = merges["states"][node["id"]]["into"]
            items.append(it)
        if snap_why:
            notices.append(snap_why)
        if cfg_why:
            notices.append(cfg_why)
        return _attachment_result({"ok": True, "items": items, "counts": counts, "notices": notices}, path_context)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"ok": False, "items": [], "counts": counts, "notices": [str(exc)]}
