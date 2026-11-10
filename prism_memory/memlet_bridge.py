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

import contextlib
import copy
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from prism_memory import memlet_dream as md
from prism_memory import memlet_wiki as mw

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
_SECTIONS = ("rules", "safety", "staleRules", "dream", "memory")
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
        r = subprocess.run(["git", "check-ignore", "-q", "--no-index", ".memlet/dream/APPROVAL-QUEUE.json"],
                           cwd=str(root), capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return None
        if r.returncode == 1:
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


def _load_queue(root: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """(graph or None, reason). Refuses a symlinked queue (raises ValueError through `memlet_dream._load_queue`)."""
    q = root / QUEUE_REL
    if not q.exists() and not q.is_symlink():
        return None, "no Dream queue (.memlet/dream/APPROVAL-QUEUE.json); run `learn` first"
    _q, graph = md._load_queue(q)
    return graph, ""


def _load_snapshot(root: Path) -> Tuple[Optional[Dict[str, List[str]]], str]:
    """The bridge's fixed in-repo snapshot (`.memlet/dream/snapshot/`), never a path read from the queue."""
    snap = root / SNAPSHOT_REL
    if _symlinked(root, (SNAPSHOT_REL,)):
        return None, "refusing symlinked snapshot directory"
    if not snap.is_dir():
        return None, "no transcript snapshot (.memlet/dream/snapshot/)"
    try:
        return md.load_snapshot(snap), ""
    except (OSError, ValueError) as exc:
        return None, f"snapshot unreadable: {exc}"


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
        lines = md.redact_secret_blocks(f.read_text(encoding="utf-8", errors="replace")).splitlines()
        stats = {"overlongLines": sum(1 for ln in lines if len(ln) > TRANSCRIPT_MAX_TEXT_CHARS)}
        if stats["overlongLines"]:
            lines = ["" if len(ln) > TRANSCRIPT_MAX_TEXT_CHARS else ln for ln in lines]
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
        if o is not None and md.node_approval_digest(o) == md.node_approval_digest(n) and _intact(o):
            out_nodes.append(o)
        else:
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
    work, over_work = 0, 0
    for f in files:
        if work >= TRANSCRIPT_MAX_WORK_CHARS:
            over_work += 1
            continue
        lines, st = _transcript_lines(f)
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
    with tempfile.TemporaryDirectory(prefix="memlet-learn-") as tmp:
        cpath = Path(tmp) / "candidates.json"
        cpath.write_text(json.dumps(cands, ensure_ascii=False), encoding="utf-8")
        receipt = md.run_dream_build(str(snap_dir), candidates_path=str(cpath),
                                     out_json=str(Path(tmp) / "queue.json"), vendor=vendor)
        built = json.loads((Path(tmp) / "queue.json").read_text(encoding="utf-8"))
    merged, info = _merge(old, built, snap, snap_dir)
    ver = md.verify_ontology_contract(merged, snap)
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
    }
    lines = ["## Memlet learn (PENDING: nothing is approved)", "",
             f"- Queue: `{QUEUE_REL}` · {len(info['added'])} new item(s) · {res['pendingCount']} pending",
             f"- Transcripts read: {len(files)} · visible messages: {reader['messages']}",
             "- Approve with `learn --approve <id>` after reading each cited line; rejected or pending items are "
             "never recalled."]
    lines += [f"- NOTICE: {n}" for n in notices]
    res["formatted"] = "\n".join(lines) + "\n"
    return res


def learn_pending(repo_path=".") -> Dict[str, Any]:
    """Nodes and edges of `.memlet/dream/APPROVAL-QUEUE.json` with no valid human decision (stale approvals included)."""
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
    for item in res["pendingNodes"]:
        node = by_id.get(str(item.get("id")))
        item["pinsVerify"] = bool(snap is not None and node is not None and _node_edges_verify(node, snap) is None)
    lines = [f"## Pending Dream items ({res['counts']['pendingNodes']} node(s), {res['counts']['pendingEdges']} edge(s))", ""]
    for item in res["pendingNodes"]:
        flag = "" if item["pinsVerify"] else " — citation does NOT verify: " + str(item.get("citationReason") or "snapshot unavailable")
        lines.append(f"- `{item['id']}` [{item['categoryLabel']}] {item['quote']} (cites `{SNAPSHOT_REL}/{item['span']}`){flag}")
        if item.get("citationReason") and item.get("currentQuote"):
            lines.append("  Current citation for fresh review: " + item["currentQuote"])
            lines.append("  After review: `memlet learn --approve " + str(item["id"]) + " --by NAME --refresh-citations`")
    if not res["pendingNodes"]:
        lines.append("- (none); add candidates with `memlet learn --transcript PATH`")
    return {"ok": True, "exitCode": 0, "command": "learn-pending", "pendingNodes": res["pendingNodes"],
            "pendingEdges": res["pendingEdges"], "counts": res["counts"], "approvalBoundary": res["approvalBoundary"],
            "notices": [], "formatted": "\n".join(lines) + "\n"}


def _decide(repo_path, ids, approver, decision: str, reason: str = "", *, reconsider=False, refresh_citations=False) -> Dict[str, Any]:
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
            if decision == md.APPROVED:
                snap = root / SNAPSHOT_REL
                if not snap.is_dir():
                    return _fail("no transcript snapshot (.memlet/dream/snapshot/) to verify pins against", 1)
                res = md.approve_queue_items(q, wanted, who, snapshot_path=str(snap), db_path=str(root / DB_REL),
                                             reconsider=reconsider, refresh_citations=refresh_citations)
            else:
                res = md.reject_queue_items(q, wanted, who, reason=reason, db_path=str(root / DB_REL))
            promotion = promote_approved(root)
    except (OSError, ValueError, sqlite3.Error) as exc:
        return _fail(f"{decision} failed: {exc}", 1)
    out = {"ok": bool(res.get("ok")) and bool(promotion.get("ok")), "exitCode": 0, "command": f"learn-{decision}",
           "decision": decision, "approver": who, "applied": res.get("applied", []),
           "unchanged": res.get("unchanged", []), "refused": res.get("refused", []),
           "cascadedEdges": res.get("cascadedEdges", []), "approvalBoundary": res.get("approvalBoundary"),
           "promotion": {k: promotion.get(k) for k in ("ok", "page", "promoted", "removed", "op", "error")}}
    errors = []
    if res.get("refused"):
        errors.append("refused: " + ", ".join(f"{r['id']} ({r['reason']})" for r in res["refused"]))
    if not promotion.get("ok"):
        errors.append(f"decision recorded, but promotion failed: {promotion.get('error')}")
    if errors:
        out.update(ok=False, exitCode=1, error="; ".join(errors))
    lines = [f"## Memlet learn — {decision} by {who}", ""]
    lines += [f"- {decision}: `{i}`" for i in out["applied"]]
    lines += [f"- unchanged: `{i}`" for i in out["unchanged"]]
    lines += [f"- REFUSED `{r['id']}`: {r['reason']}" for r in out["refused"]]
    if promotion.get("ok"):
        lines.append(f"- Promoted page `{PROMOTED_PAGE}`: {len(promotion.get('promoted') or [])} rule(s), "
                     f"{len(promotion.get('removed') or [])} removed ({promotion.get('op')})")
    else:
        lines.append(f"- Promotion NOT done: {promotion.get('error')}")
    out["formatted"] = "\n".join(lines) + "\n"
    return out


def learn_approve(repo_path=".", ids=(), *, approver="", reconsider=False, refresh_citations=False) -> Dict[str, Any]:
    """The only path out of PENDING: `memlet_dream.approve_queue_items` with the snapshot and DB passed explicitly
    (pins re-verified, approval bound to a content digest), then `promote_approved`."""
    return _decide(repo_path, ids, approver, md.APPROVED, reconsider=reconsider, refresh_citations=refresh_citations)


def learn_reject(repo_path=".", ids=(), *, approver="", reason="") -> Dict[str, Any]:
    """`memlet_dream.reject_queue_items` (rejected items are never recalled), then `promote_approved`."""
    return _decide(repo_path, ids, approver, md.REJECTED_DECISION, reason=reason)


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


def _find_by_basename(root: Path, name: str) -> List[str]:
    """Repo-relative files named `name` (bounded, pruned walk; never into .memlet, excluded or nested-repo dirs)."""
    out: List[str] = []
    seen = 0
    for cur, dirs, files in os.walk(root):
        rel_parts = Path(cur).relative_to(root).parts
        if rel_parts and (Path(cur) / ".git").exists():
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if d not in mw.EXCLUDED_DIRS and d != mw.MEMLET_DIR and d not in mw._SCRATCH_DIRS)
        seen += len(files)
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
    text = f"{node.get('title') or ''}\n{pp.get('quoteText') or ''}"
    for m in mw.iter_explicit_anchors(text):
        if "::" in m.group(0):
            path, sym = mw._clean_ref_path(m.group("path")), m.group("symbol").rstrip(".-")
            if mw._current_fingerprint(root, path, sym).get("resolution") == "exact":
                return path, sym
    scope = str(node.get("scopeTag") or "")
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
    view, _stats = md.approved_view(graph)
    raw_by_id = {str(n.get("id")): n for n in graph.get("nodes") or [] if isinstance(n, dict)}
    out: List[Dict[str, Any]] = []
    for n in view.get("nodes") or []:
        if not isinstance(n, dict) or n.get("verdict") not in ("SUPPORTED",) or n.get("statusKind") == "superseded_rule":
            continue
        live_edges = [e for e in n.get("edges") or [] if isinstance(e, dict) and e.get("edgeVerdict") == "VALID"]
        if not live_edges:
            continue
        raw = raw_by_id.get(str(n.get("id")))
        if raw is None or md.approval_state(raw, "node") != "approved":
            continue
        err = md._verify_item_pins(raw, "node", snap)
        for e in live_edges:
            err = err or md._verify_item_pins(e, "edge", snap)
        if err:
            notes.append(f"{n.get('id')}: {err}")
            continue
        out.append(raw)
    return out, notes


def lesson_text(node: Dict[str, Any]) -> str:
    """The reviewed pinned quote, capped at 8192 characters with an explicit ellipsis.

    Preserve code anchors/backticks. Escape HTML comment delimiters so transcript text cannot inject pin metadata.
    """
    text = (node.get("primaryPin") or {}).get("quoteText") or node.get("title") or ""
    text = md._LINE_PREFIX_RE.sub("", str(text), count=1).strip()
    text = md.safe_truncate_masked(text, md.LESSON_TEXT_MAX_CHARS)
    return text.replace("<!--", "&lt;!--").replace("-->", "--&gt;").replace("\n", " ")


def _render_rule(node: Dict[str, Any], anchor: Optional[Tuple[str, Dict[str, Any]]]) -> Tuple[str, List[str]]:
    pp = node.get("primaryPin") if isinstance(node.get("primaryPin"), dict) else {}
    text = lesson_text(node)
    meta = (f"dream {node.get('id')} · {md.category_label(node.get('category'))} · scope {_defuse(node.get('scopeTag'), 48)}"
            f" · cites {_defuse(SNAPSHOT_REL + '/' + str(pp.get('span') or ''), 120)}")
    body = f"{text} (`{anchor[0]}`) — {meta}" if anchor else f"{text} — {meta}"
    lines = [f"- {body}"]
    if anchor:
        lines.append(_pin_comment(anchor[0], anchor[1]))
    lines.append(f"  <!-- memlet:dream id={node.get('id')} digest={md.node_approval_digest(node)} text={_sha(body)[:16]} -->")
    return body, lines


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
    config_pins = mw._config_pins(root)
    lesson_lines: List[str] = []
    dead_lines: List[str] = []
    rules_out: List[Dict[str, Any]] = []
    for n in sorted(lessons, key=lambda x: str(x.get("id"))):
        nid = str(n.get("id"))
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


def promoted_rule_entries(root: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(fresh, stale) rule-index-shaped entries for `memlet_recall`: a promoted rule is fresh only while its line is
    exactly the text Memlet renders from the approved Dream item, that item is still approved for the same content
    (approval digest), and its citations verify against the snapshot. Re-checked on every call; no model call."""
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
    view_by_id = {str(n.get("id")): n for n in (md.approved_view(graph)[0].get("nodes") or [])} if graph else {}
    fresh, stale = [], []
    for r in rules:
        node = by_id.get(r["id"])
        topic = f"approved lesson · {md.category_label((node or {}).get('category') or 'dream')}"
        imp = md._safe_imp((node or {}).get("importance"), 2)
        entry = {"topic": topic, "importance": max(1, min(3, imp)), "source": f"{PROMOTED_PAGE}:{r['line']}",
                 "dreamId": r["id"]}
        anchor = (r["pins"][0]["key"], r["pins"][0]["pin"]) if r["pins"] else None
        ok = (node is not None and snap is not None and _sha(r["body"])[:16] == r["textSha"]
              and md.approval_state(node, "node") == "approved" and md.node_approval_digest(node) == r["digest"]
              and _render_rule(node, anchor)[0] == r["body"]  # the quoted text is exactly what was approved
              and (view_by_id.get(r["id"]) or {}).get("verdict") == "SUPPORTED"
              and _node_edges_verify(node, snap) is None)
        if ok:
            entry.update(quote=r["body"], lines=1)
            fresh.append(entry)
        else:
            stale.append(entry)
    return fresh, stale


# ---------------------------------------------------------------------------------------------------------------
# unified recall


def _rules_items(root: Path, query: str) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Dict[str, Any]], List[str]]:
    """({"rules": query matches, "safety": always-on importance-1 rules}, their sources, notices).
    With a rule index: BM25 over its verified entries. Without one: the deterministic parsed fallback
    (`memlet_recall.parsed_rule_entries`), labelled as such and never presented as reviewed; it has no safety list."""
    from prism_memory import memlet_recall as mr
    from prism_memory.memlet_index import INDEX_PAGE
    mode = "index" if (root / INDEX_PAGE).is_file() else "parsed"
    # promoted Dream lessons are shown once, in the `dream` section, with their transcript citation
    promoted = [e["source"] for e in promoted_rule_entries(root)[0]]
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
        head += f" [{it['anchorStatus']}: {it['anchorReason']}]"
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


def _dream_items(root: Path, query: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[str]]:
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
        _q, graph = md._load_queue(q)
        res = md.run_dream_recall(str(q), snapshot_path=str(snap), query_text=query, budget_tokens=100000)
    except (OSError, ValueError) as exc:
        return [], {"available": False, "reason": f"Dream snapshot or queue does not verify; nothing returned ({exc})"[:300]}, []
    if not res.get("ok"):
        return [], {"available": False, "reason": str(res.get("error") or "Dream recall failed")}, []
    view, stats = md.approved_view(md.citation_review_view(graph, md.load_snapshot(snap)))
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
                      "title": n.get("title"), "quote": lesson_text(n), "source": source, "text": text})
    promoted_by_id = {e["dreamId"]: e for e in promoted_rule_entries(root)[0]}
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
    mw.classify_rule_quotes(root, entries)
    for item, entry in zip(items, entries):
        for key in ("anchorStatus", "anchorReason", "anchors"):
            item[key] = entry[key]
        item["text"] += f" [{entry['anchorStatus']}: {entry['anchorReason']}]"
    notices = []
    held = stats.get("pendingNodes", 0) + stats.get("rejectedNodes", 0)
    if held:
        notices.append(f"{stats.get('pendingNodes', 0)} pending and {stats.get('rejectedNodes', 0)} rejected Dream "
                       "item(s) are never recalled")
    failures = res.get("unverifiableItems", [])
    if failures:
        notices.append(md.citation_failure_notice(failures))
    return items, {"available": True, "reason": md.citation_failure_notice(failures),
                   "pinVerification": res.get("pinVerification"), "unverifiableCount": len(failures),
                   "unverifiableNodeIds": [f["id"] for f in failures], "unverifiableItems": failures}, notices


def _memory_items(store: Any, task: Any, query: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if store is None or not task:
        return [], {"available": False, "reason": "memory store recall needs both a store and a task"}
    try:
        if not hasattr(store, "memory"):
            p = Path(str(store)).expanduser()
            dbs = ("prism_memory.sqlite3", "work.sqlite3", "work_tasks.sqlite3")
            if not p.is_dir() or not any((p / name).is_file() for name in dbs):  # never create a store by reading
                return [], {"available": False, "reason": f"memory store not found: {p}"}
            from prism_memory.standalone import StandaloneStore
            store = StandaloneStore(p)
        res = store.memory(str(task), query or "", 6000)
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
        from prism_memory.memlet_brief import _mask
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
}


def _head_for(section: str, rules_mode: str) -> str:
    return _SECTION_HEADS["rules-parsed" if section == "rules" and rules_mode == "parsed" else section]


def _render_recall(chosen: Dict[str, List[Dict[str, Any]]], footer: Optional[str], rules_mode: str = "index", verification_notice: str = "") -> str:
    lines = [_HEAD]
    if verification_notice:
        lines.append("- WARNING: " + verification_notice)
    for s in _SECTIONS:
        if chosen.get(s):
            lines += ["", _head_for(s, rules_mode)] + [it["text"] for it in chosen[s]]
    if footer:
        lines += ["", footer]
    return "\n".join(lines) + "\n"


def unified_recall(repo_path=".", query="", *, budget=1200, store=None, task=None,
                   include=("rules", "dream", "memory")) -> Dict[str, Any]:
    """One budgeted response from three stores (no model call). The budget is split evenly across the sections that
    have candidates; whatever a section leaves unused flows to the others. `tokenEstimate <= tokenBudget` always."""
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
    wanted = [s for s in _SECTIONS if s in asked]
    notices: List[str] = [f"unknown section ignored: {s}" for s in (include or ()) if s and s not in _SECTIONS]
    query = str(query or "")
    cands: Dict[str, List[Dict[str, Any]]] = {s: [] for s in _SECTIONS}
    sources: Dict[str, Dict[str, Any]] = {s: {"available": False, "reason": "not requested"} for s in _SECTIONS}
    rules_mode = "index"
    if "rules" in wanted:
        try:
            got, srcs, extra = _rules_items(root, query)
            cands["rules"], cands["safety"] = got["rules"], got["safety"]
            sources["rules"], sources["safety"] = srcs["rules"], srcs["safety"]
            cands["staleRules"], sources["staleRules"] = got["staleRules"], srcs["staleRules"]
            rules_mode = srcs["rules"]["mode"]
            notices += extra
        except (OSError, ValueError) as exc:
            sources["rules"] = {"available": False, "reason": f"rule recall failed: {exc}"}
            sources["safety"] = {"available": False, "reason": f"rule recall failed: {exc}"}
    if "dream" in wanted:
        cands["dream"], sources["dream"], extra = _dream_items(root, query)
        notices += extra
        stale_dream = [it for it in cands["dream"] if it["anchorStatus"] == "stale"]
        cands["staleRules"].extend(stale_dream)
        cands["dream"] = [it for it in cands["dream"] if it["anchorStatus"] != "stale"]
        if stale_dream:
            sources["staleRules"] = {"available": True, "reason": "anchored code changed"}
    if "memory" in wanted:
        cands["memory"], sources["memory"] = _memory_items(store, task, query)
    for s in _SECTIONS:
        sources[s]["candidates"] = len(cands[s])

    est = mw.estimate_tokens
    failures = sources["dream"].get("unverifiableItems", [])
    verification_notice = md.citation_failure_notice(failures)
    fixed_heading = _render_recall({}, None, rules_mode, verification_notice)
    unavailable = [f"{s} ({sources[s]['reason']})" for s in wanted if not sources[s]["available"]]
    footer = ("- Not consulted: " + "; ".join(unavailable)) if unavailable else None
    chosen: Dict[str, List[Dict[str, Any]]] = {s: [] for s in _SECTIONS}
    order: List[str] = []
    active = [s for s in _SECTIONS if cands[s]]
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
    text = _render_recall(chosen, footer, rules_mode, verification_notice)
    if est(text) > budget:
        text = _render_recall(chosen, None, rules_mode, verification_notice)
        footer = None
    while est(text) > budget and order:  # the estimator is not additive across joins: trim until it truly fits
        chosen[order.pop()].pop()
        text = _render_recall(chosen, None, rules_mode, verification_notice)
    budget_too_small = est(text) > budget
    if budget_too_small:
        text = ""
    omitted = {s: len(cands[s]) - len(chosen[s]) for s in _SECTIONS}
    for s in _SECTIONS:
        sources[s]["returned"] = len(chosen[s])
        sources[s]["omittedForBudget"] = omitted[s]
    sections = {s: [{k: v for k, v in it.items() if k != "text"} for it in chosen[s]] for s in _SECTIONS}
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
    return mw.read_config(root)


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
        r = subprocess.run(["git"] + args, cwd=str(root), capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git is unavailable ({exc}); pass the changed files explicitly"
    if r.returncode != 0:
        err = (r.stderr.decode("utf-8", "replace").strip().splitlines() or [""])[0]
        return None, ("not a git repository or unknown base ref; pass the changed files explicitly: " + err)[:300]
    return [t for t in r.stdout.decode("utf-8", "surrogateescape").split("\0")], ""


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


def changed_check(repo_path=".", *, base=None, files=None) -> Dict[str, Any]:
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
                live = {int(e["source"].rsplit(":", 1)[1]) for e in promoted_rule_entries(root)[0]}
                page_rules = [r for r in page_rules if r["line"] in live]
            rules.extend(page_rules)
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
            res["affected"].append({"anchor": worst, "status": a["status"], "reason": a["reason"],
                                    "rule": mw._clip(r["text"], 160), "source": f"{r['source']}:{r['line']}"})
    if not changed and (root / mw.CONFIG_PATH).is_file():
        res["pinnedAnchors"] = mw.pinned_anchor_report(root)
        shown = {a["anchor"] for a in res["affected"]}
        for a in res["pinnedAnchors"]["anchors"]:
            if not changed and a["pinned"] and a["status"] == "stale" and a["anchor"] not in shown:
                res["affected"].append({"anchor": a["anchor"], "status": a["status"], "reason": a["reason"],
                                        "rule": "pinned anchor (including committed changes)",
                                        "source": ", ".join(a["sources"]) or mw.CONFIG_PATH})
    res["staleCount"] = sum(1 for a in res["affected"] if a["status"] == "stale")
    if res["staleCount"]:
        res.update(ok=False, exitCode=1, error=f"{res['staleCount']} rule(s) or recorded pins have stale anchors")
    res["formatted"] = "".join(f"{a['status'].upper()} `{a['anchor']}` ({a['reason']}) — {a['source']}: "
                               f"{mw._clip(a['rule'], 100)}\n" for a in res["affected"]) + "".join(notes)
    return res
