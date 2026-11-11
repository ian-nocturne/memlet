"""Memlet 9.3 tidy report: lessons a person might merge, look at again, retire or promote. Strictly read-only.

Nothing here writes: no queue, ledger, page, cache or table is created or changed, and no model is called. Tiers come
from the one reader every other reader uses (`memlet_bridge.dream_items`); merge state comes only from
`memlet_bridge.merge_view` / `merge_blockers` through `merge_checks`, the single adapter, which says so when this
build has no merge support. Sightings ("said again") are computed at read time from the stored snapshots under
`.memlet/dream/snapshot/`: every file is opened at most once per call, and the work is counted, not timed.
"""

from __future__ import annotations

import datetime
import os
import re
import shlex
import stat
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import memlet_bridge as mb
from . import memlet_dream as md
from . import memlet_wiki as mw
from . import vocabulary as v
from .vocabulary import APPROVED, HINT, PENDING, REJECTED

MAX_BYTES = 64 * 1024 * 1024          # snapshot bytes read per call
MAX_FILES = md.SNAPSHOT_MAX_FILES      # snapshot files read per call
MAX_LINE_BYTES = 64 * 1024            # a longer physical line is skipped (a cited line is at most 16,384 characters)
MAX_WHERE = 8
MIN_SIMILAR_WORDS = 5
SIMILAR_JACCARD = 0.8
MAX_SIMILAR_WORK = 2_000_000          # posting-list visits for the similar-wording comparison

_USER_LINE_RE = re.compile(r"^\s*(?:\d{4}-\d{2}-\d{2}[T ][^\s]+\s*)?\[user\]")
# `<stem>-<10 hex>.log` (a transcript's snapshot) and `<stem>-<10 hex>-<10 hex>.log` (the same transcript rewritten).
_SESSION_SUFFIX_RE = re.compile(r"(?:-[0-9a-f]{10}){1,2}\.log$")
_WORD_RE = re.compile(r"\w+")


# ---------------------------------------------------------------------------------------------------------------
# small helpers


def _now(clock: Optional[Callable[[], Any]]) -> datetime.datetime:
    value = clock() if clock is not None else datetime.datetime.now(datetime.timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _iso(value: Optional[datetime.datetime]) -> Optional[str]:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value is not None else None


def _line_time(line: str, now: datetime.datetime) -> Optional[datetime.datetime]:
    """Timestamp at the start of a transcript line; a future one (after `now`) is not believed."""
    ts = md.parse_iso_dt(md.extract_line_timestamp(line))
    return ts if ts is not None and ts <= now else None


def session_key(file_id: str) -> str:
    """One session per transcript: a snapshot base name without its trailing `-<10 hex>.log` (and the second hash a
    rewritten transcript adds), so a rewritten transcript is never counted twice. Undercounts rather than overstates."""
    head, _, name = file_id.rpartition("/")
    base = _SESSION_SUFFIX_RE.sub("", name)
    if not base or base == name:
        return file_id
    return (head + "/" if head else "") + base


def _q(value: Any) -> str:
    return shlex.quote(str(value))


def command(*argv: Any, repo: Optional[str] = None) -> str:
    """A POSIX-shell command line that runs only `memlet`, every argument quoted as one word."""
    parts = ["memlet"] + [_q(a) for a in argv]
    if repo is not None:
        parts += ["--repo", _q(repo)]
    return " ".join(parts)


def _current_lines(node: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(span, raw cited line) of the lesson as stored now, without replaced lines of a supersession."""
    display = dict(node, allPins=list(node.get("allPins") or [node.get("primaryPin")]))
    if md.superseded_lesson(node):
        display["allPins"] += [node.get("supersededByPin")]
        display["allPins"] += [e.get("srcPin") for e in node.get("edges", [])
                               if isinstance(e, dict) and e.get("edgeType") == "SUPERSEDES"]
    raw = {p["span"]: p["quote"] for p in md.lesson_lines(display)}
    fields = md.lesson_display_fields(node)
    roles = fields.get("lineRoles", ["current"] * len(fields.get("cites", [])))
    return [(c["span"], raw[c["span"]]) for c, role in zip(fields.get("cites", []), roles)
            if role != "replaced" and c["span"] in raw]


def _node_time(node: Dict[str, Any]) -> Optional[datetime.datetime]:
    times = [md.parse_iso_dt(md.extract_line_timestamp(p["quote"])) for p in md.lesson_lines(node)]
    times = [t for t in times if t is not None]
    return min(times) if times else None


def _decision_time(node: Dict[str, Any]) -> Optional[datetime.datetime]:
    record = node.get("approval") if isinstance(node.get("approval"), dict) else {}
    return md.parse_iso_dt(record.get("at")) if md._valid_utc_stamp(record.get("at")) else None


# ---------------------------------------------------------------------------------------------------------------
# merge awareness: the one adapter


def merge_checks(repo_path=".") -> Dict[str, Any]:
    """The single place this module learns about merges. Uses `memlet_bridge.merge_view` and
    `memlet_bridge.merge_blockers` when this build has both; otherwise reports that merge checks are not available.

    Returns {available, states: {id: {state, into, reason}}, members: {kept id: [member ids]}, blockers: callable or
    None, notice: str or None}. Nothing here decides whether a merge holds."""
    view_fn = getattr(mb, "merge_view", None)
    blockers_fn = getattr(mb, "merge_blockers", None)
    none = {"available": False, "states": {}, "members": {}, "blockers": None}
    if not callable(view_fn) or not callable(blockers_fn):
        return dict(none, notice=v.TIDY_MERGE_UNAVAILABLE)
    try:
        view = view_fn(repo_path)
    except (OSError, ValueError, TypeError, KeyError):
        view = None
    if (not isinstance(view, dict) or not view.get("ok") or not isinstance(view.get("states"), dict)
            or not isinstance(view.get("members"), dict)):
        return dict(none, notice=v.TIDY_MERGE_UNREADABLE)

    def blockers(keep, same):
        return blockers_fn(repo_path, keep, tuple(same))

    return {"available": True, "states": view["states"], "members": view["members"], "blockers": blockers,
            "notice": None}


def _holding_members(merge: Dict[str, Any]) -> Dict[str, str]:
    """{member id: kept id} for merges that hold, as `merge_view` reports them."""
    out = {}
    for keep, members in merge["members"].items():
        for member in members or ():
            out[str(member)] = str(keep)
    return out


# ---------------------------------------------------------------------------------------------------------------
# sightings


class _Work(dict):
    def __init__(self):
        super().__init__(filesListed=0, filesOpened=0, bytesRead=0, linesRead=0, linesCompared=0)


def _snapshot_files(root: Path, work: _Work, skipped: Dict[str, int]) -> List[Tuple[str, Path]]:
    """(file id, path) of every `.log` file under the snapshot directory, sorted; symlinks never followed."""
    base = root / mb.SNAPSHOT_REL
    out: List[Tuple[str, Path]] = []
    stack = [("", base)]
    while stack:
        prefix, folder = stack.pop()
        try:
            with os.scandir(str(folder)) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            skipped["unreadable"] += 1
            continue
        for entry in entries:
            rel = prefix + entry.name
            try:
                if entry.is_symlink():
                    skipped["symlinks"] += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append((rel + "/", Path(entry.path)))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    skipped["unsupported"] += 1
                    continue
            except OSError:
                skipped["unreadable"] += 1
                continue
            if not entry.name.endswith(".log") or not md._is_safe_file_id(rel):
                skipped["unsupported"] += 1
                continue
            work["filesListed"] += 1
            out.append((rel, Path(entry.path)))
    out.sort()
    return out


def _read_file(path: Path, remaining: int, work: _Work, skipped: Dict[str, int]) -> Tuple[Optional[bytes], bool]:
    """(bytes or None, over budget). One open per file; a symlink, FIFO or device is never followed or read."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        skipped["unreadable"] += 1
        return None, False
    work["filesOpened"] += 1
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            skipped["unsupported"] += 1
            return None, False
        if st.st_size > remaining:
            skipped["overBudget"] += 1
            return None, True
        chunks, total = [], 0
        while total <= remaining:
            chunk = os.read(fd, min(1 << 20, remaining + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        work["bytesRead"] += total
        if total > remaining:
            skipped["overBudget"] += 1
            return None, True
        return b"".join(chunks), False
    except OSError:
        skipped["unreadable"] += 1
        return None, False
    finally:
        os.close(fd)


def _scan(root: Path, wanted: Dict[str, List[str]], *, max_bytes: int, now: datetime.datetime
          ) -> Dict[str, Any]:
    """Records of complete sightings per lesson: [{session, file, line, early, late}], one per snapshot file."""
    work = _Work()
    skipped = {"symlinks": 0, "unreadable": 0, "unsupported": 0, "hugeLines": 0, "invalidUtf8Lines": 0,
               "overBudget": 0, "overFileCount": 0}
    records: Dict[str, List[Dict[str, Any]]] = {i: [] for i in wanted}
    complete, notices = True, []
    scanned = {"files": 0, "bytes": 0}
    need: Dict[str, List[Tuple[str, int]]] = {}
    for lesson_id, lines in wanted.items():
        for index, text in enumerate(lines):
            need.setdefault(text, []).append((lesson_id, index))
    problem = mb._symlinked(root, (mb.SNAPSHOT_REL,))
    if problem or not (root / mb.SNAPSHOT_REL).is_dir():
        notices.append(v.TIDY_SNAPSHOT_UNAVAILABLE)
        return {"records": records, "complete": not (root / mb.SNAPSHOT_REL).exists(), "work": work,
                "skipped": skipped, "scanned": scanned, "notices": notices}
    files = _snapshot_files(root, work, skipped)
    if len(files) > MAX_FILES:
        skipped["overFileCount"] = len(files) - MAX_FILES
        files, complete = files[:MAX_FILES], False
    budget = max(0, int(max_bytes))
    for file_id, path in files:
        data, over = _read_file(path, budget - work["bytesRead"], work, skipped)
        if data is None:
            complete = False
            continue
        scanned["files"] += 1
        scanned["bytes"] += len(data)
        if not need:
            continue
        found: Dict[str, Dict[int, List[Tuple[int, Optional[datetime.datetime]]]]] = {}
        raw_lines = data.split(b"\n")
        if raw_lines and raw_lines[-1] == b"":
            raw_lines.pop()
        for number, raw in enumerate(raw_lines, start=1):
            work["linesRead"] += 1
            if len(raw) > MAX_LINE_BYTES:
                skipped["hugeLines"] += 1
                continue
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped["invalidUtf8Lines"] += 1
                continue
            if not _USER_LINE_RE.match(line):
                continue
            work["linesCompared"] += 1
            hits = need.get(md.normalised_quote(line))
            if not hits:
                continue
            when = _line_time(line, now)
            for lesson_id, index in hits:
                found.setdefault(lesson_id, {}).setdefault(index, []).append((number, when))
        for lesson_id, by_index in found.items():
            if len(by_index) != len(wanted[lesson_id]):
                continue  # every cited line must be said in this one session
            first_line = min(n for n, _ in by_index[0])
            per_line = [[t for _, t in by_index[i] if t is not None] for i in range(len(wanted[lesson_id]))]
            early = late = None
            if all(per_line):
                early = max(min(ts) for ts in per_line)
                late = min(max(ts) for ts in per_line)
            records[lesson_id].append({"session": session_key(file_id), "file": file_id, "line": first_line,
                                       "early": early, "late": late})
    return {"records": records, "complete": complete, "work": work, "skipped": skipped, "scanned": scanned,
            "notices": notices}


def _summary(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    early = [r["early"] for r in records if r["early"] is not None]
    late = [max(r["early"], r["late"]) for r in records if r["early"] is not None]
    where = sorted(records, key=lambda r: (r["file"], r["line"]))[:MAX_WHERE]
    return {"sessions": len({r["session"] for r in records}), "first": _iso(min(early)) if early else None,
            "last": _iso(max(late)) if late else None,
            "where": [{"file": r["file"], "line": r["line"]} for r in where]}


def _sessions_after(records: List[Dict[str, Any]], when: Optional[datetime.datetime]) -> int:
    if when is None:
        return 0
    return len({r["session"] for r in records if r["late"] is not None and r["late"] > when})


def _sightings(root: Path, graph: Dict[str, Any], merge: Dict[str, Any], *, max_bytes: int,
               now: datetime.datetime) -> Dict[str, Any]:
    nodes = [n for n in (graph or {}).get("nodes", []) if isinstance(n, dict) and isinstance(n.get("id"), str)]
    wanted = {}
    for node in nodes:
        lines = [md.normalised_quote(raw) for _, raw in _current_lines(node)]
        if lines and all(lines):
            wanted[node["id"]] = lines
    scan = _scan(root, wanted, max_bytes=max_bytes, now=now)
    records = {n["id"]: list(scan["records"].get(n["id"], [])) for n in nodes}
    # A holding merge: the kept lesson counts distinct sessions across the whole group.
    for member, keep in _holding_members(merge).items():
        if keep in records:
            records[keep] = records[keep] + records.get(member, [])
    scan["grouped"] = records
    return scan


def lesson_sightings(repo_path=".", *, ids=None, max_bytes=MAX_BYTES, clock=None) -> Dict[str, Any]:
    """{ok, complete, scanned: {files, bytes}, sightings: {id: {sessions, first, last, where}}} plus `work` (counters:
    files listed and opened, bytes read, lines read and compared) and `skipped`. Read-only."""
    try:
        root = mw._resolve_root(repo_path)
        graph, why = mb._load_queue(root)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "complete": False, "scanned": {"files": 0, "bytes": 0},
                "sightings": {}}
    now = _now(clock)
    merge = merge_checks(root)
    scan = _sightings(root, graph or {}, merge, max_bytes=max_bytes, now=now)
    keep = None if ids is None else set(mb._ids(ids))
    sightings = {i: _summary(r) for i, r in scan["grouped"].items() if keep is None or i in keep}
    notices = ([why] if why else []) + scan["notices"] + ([merge["notice"]] if merge["notice"] else [])
    return {"ok": True, "complete": scan["complete"], "scanned": scan["scanned"], "sightings": sightings,
            "work": dict(scan["work"]), "skipped": scan["skipped"], "notices": notices}


# ---------------------------------------------------------------------------------------------------------------
# the report


def _cites(fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"file": c["file"], "start": c["start"], "end": c["end"]} for c in fields.get("cites", [])]


def _lesson(item: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": item["id"], "tier": item["tier"], "edgeTypes": list(item.get("edgeTypes", [])),
            "quoteLines": list(item.get("quoteLines") or [item.get("text", "")]), "cites": _cites(item)}


def _count(value: int, complete: bool) -> Any:
    return value if complete else v.TIDY_AT_LEAST % value


def _instruction_rules(root: Path) -> Dict[str, List[str]]:
    """{normalised rule text: [source:line]} from the instruction-file parser (never the promoted Dream page)."""
    out: Dict[str, List[str]] = {}
    for rel, text in mw._source_texts(root, mw.discover_instruction_files(root)):
        for rule in mw._extract_rules(text, rel):
            key = md.normalised_quote(rule["text"])
            if key:
                out.setdefault(key, []).append("%s:%d" % (rule["source"], rule["line"]))
    return out


def _promoted_anchors(root: Path) -> Tuple[Dict[str, Tuple[str, Dict[str, Any]]], bool]:
    """({lesson id: (path::symbol, pin)} from the promoted page, page trusted). Same rule as promotion's sticky anchors:
    only a page that is exactly what Memlet last wrote."""
    page = root / mb.PROMOTED_PAGE
    if mb._symlinked(root, (mb.PROMOTED_PAGE,)) or not page.is_file():
        return {}, True
    text = mw.read_regular_text(page, mw.MAX_WIKI_PAGE_BYTES, errors="replace")
    if text is None or mb._page_edit_problem(root, text, for_reader=True):
        return {}, False
    return {r["id"]: (r["pins"][0]["key"], r["pins"][0]["pin"]) for r in mb._parse_promoted(text) if r["pins"]}, True


def _anchor_stale(root: Path, key: str, pin: Dict[str, Any]) -> Optional[str]:
    path, _, symbol = key.partition("::")
    if not symbol:
        return None
    entry = mw.classify_anchor(root, {"path": path, "symbol": symbol, "pin": pin})
    return entry["reason"] if entry["status"] == "stale" else None


def _similar_groups(texts: Dict[str, List[str]]) -> Tuple[List[List[str]], bool]:
    """Components of distinct normalised quotes whose word sets have Jaccard >= 0.8 (both >= 5 words)."""
    keys = sorted(texts)
    words = {k: frozenset(_WORD_RE.findall(k)) for k in keys}
    eligible = [k for k in keys if len(words[k]) >= MIN_SIMILAR_WORDS]
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    postings: Dict[str, List[int]] = {}
    for i, k in enumerate(eligible):
        for w in words[k]:
            postings.setdefault(w, []).append(i)
    work, complete = 0, True
    for i, k in enumerate(eligible):
        overlap: Dict[int, int] = {}
        for w in words[k]:
            for j in postings[w]:
                work += 1
                if j > i:
                    overlap[j] = overlap.get(j, 0) + 1
            if work > MAX_SIMILAR_WORK:
                complete = False
                break
        for j, inter in overlap.items():
            other = eligible[j]
            if inter / (len(words[k]) + len(words[other]) - inter) >= SIMILAR_JACCARD:
                parent[find(other)] = find(k)
        if not complete:
            break
    groups: Dict[str, List[str]] = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    return list(groups.values()), complete


def _capped(entries: List[Dict[str, Any]], omitted: Dict[str, int], section: str) -> List[Dict[str, Any]]:
    omitted[section] = max(0, len(entries) - v.TIDY_SECTION_CAP)
    return entries[:v.TIDY_SECTION_CAP]


def tidy_report(repo_path=".", *, stale_days=180, min_seen=3, clock=None, max_bytes=MAX_BYTES) -> Dict[str, Any]:
    """{ok, duplicates, saidAgain, retire, unattached, promote, lapsedMerges, omitted, complete, notices, formatted}
    plus `scanned`, `work` and `skipped` of the snapshot scan. Writes nothing; proposes only."""
    if (type(stale_days) is not int or stale_days < 0 or type(min_seen) is not int or min_seen < 1):
        return {"ok": False, "exitCode": 2, "error": v.TIDY_INVALID_ARGUMENT,
                "formatted": "- ERROR: " + v.TIDY_INVALID_ARGUMENT + "\n"}
    repo_arg = None if str(repo_path) == "." else str(repo_path)
    try:
        root = mw._resolve_root(repo_path)
        problem = mb._symlinked(root, (mb.QUEUE_REL, mb.SNAPSHOT_REL, mw.CONFIG_PATH))
        if problem:
            raise ValueError(mb._refusing(problem))
        graph, why = mb._load_queue(root)
    except (OSError, ValueError) as exc:
        return {"ok": False, "exitCode": 2, "error": str(exc), "formatted": "- ERROR: " + str(exc) + "\n"}
    now = _now(clock)
    notices: List[str] = [why] if why else []
    graph = graph or {"nodes": [], "edges": []}
    merge = merge_checks(root)
    if merge["notice"]:
        notices.append(merge["notice"])
    with mb._lesson_path_projection(root, None) as context:
        read = mb.dream_items(root, tiers=(APPROVED, HINT, PENDING), _graph=graph) if graph["nodes"] else \
            {"ok": True, "items": [], "notices": []}
        reached = dict(context["paths"])
        attachment_complete = not context.get("attachmentIncomplete")
    if not read["ok"]:
        error = "; ".join(read.get("notices", [])) or v.GRAPH_EVIDENCE_ERROR
        return {"ok": False, "exitCode": 1, "error": error, "formatted": "- ERROR: " + error + "\n"}
    notices.extend(n for n in read.get("notices", []) if n not in notices)
    items = {i["id"]: i for i in read["items"]}
    nodes = {n["id"]: n for n in graph["nodes"] if isinstance(n, dict) and isinstance(n.get("id"), str)}
    rejected = {i: n for i, n in nodes.items() if i not in items
                and mb.item_tier(n, snapshot=None, config={}) == REJECTED
                and md.approval_state(n, "node") == REJECTED}
    holding = _holding_members(merge)
    scan = _sightings(root, graph, merge, max_bytes=max_bytes, now=now)
    notices.extend(scan["notices"])
    complete = scan["complete"]
    if not complete:
        notices.append(v.TIDY_INCOMPLETE)
    records = scan["grouped"]
    sight = {i: _summary(r) for i, r in records.items()}
    visible = {i: it for i, it in items.items() if i not in holding}
    omitted: Dict[str, int] = {}

    # duplicates ------------------------------------------------------------------------------------------------
    by_text: Dict[str, List[str]] = {}
    for lesson_id in visible:
        text = "\n".join(md.normalised_quote(p["quote"]) for p in md.lesson_lines(nodes[lesson_id]))
        if text.strip():
            by_text.setdefault(text, []).append(lesson_id)
    components, similar_complete = _similar_groups(by_text)
    if not similar_complete:
        notices.append(v.TIDY_SIMILAR_BOUND)
        complete = False
    duplicates, blockers_failed = [], False
    for keys in components:
        ids = sorted(i for k in keys for i in by_text[k])
        if len(ids) < 2:
            continue
        kind = v.TIDY_SAME_TEXT if len(keys) == 1 else v.TIDY_SIMILAR_TEXT
        far = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
        ids.sort(key=lambda i: (visible[i]["tier"] != APPROVED, _node_time(nodes[i]) or far, i))
        keep, same = ids[0], ids[1:]
        entry = {"kind": kind, "label": v.TIDY_DUPLICATE_LABELS[kind], "keep": keep, "same": same,
                 "lessons": [_lesson(visible[i]) for i in ids],
                 "command": command("learn", "--merge", keep, "--same", *same, "--by", "NAME", repo=repo_arg)}
        if merge["available"]:
            try:
                checked = merge["blockers"](keep, same)
                if not isinstance(checked, dict) or not checked.get("ok", True):
                    raise ValueError("merge blockers unavailable")
                entry["blockers"] = {str(k): list(c) for k, c in (checked.get("blockers") or {}).items()}
                entry["lostFiles"] = {str(k): list(p) for k, p in (checked.get("lostFiles") or {}).items()}
                lost = list(dict.fromkeys(p for paths in entry["lostFiles"].values() for p in paths))
                current = list(visible[keep].get("approvalPaths") or [])
                wanted = list(dict.fromkeys(current + lost))
                if lost and visible[keep]["tier"] == APPROVED and len(wanted) <= 8:
                    args = ["learn", "--paths", keep, "--by", "NAME"]
                    for p in wanted:
                        args += ["--path", p]
                    entry["pathsCommand"] = command(*args, repo=repo_arg)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                blockers_failed = True
        duplicates.append(entry)
    if blockers_failed:
        notices.append(v.TIDY_BLOCKERS_FAILED)
    duplicates.sort(key=lambda g: (-len(g["lessons"]), g["keep"]))

    # said again ------------------------------------------------------------------------------------------------
    said = []
    for lesson_id, item in visible.items():
        if item["tier"] in (PENDING, HINT) and sight[lesson_id]["sessions"] >= 2:
            n = sight[lesson_id]["sessions"]
            said.append(dict(_lesson(item), **sight[lesson_id], sessionsLabel=_count(n, complete),
                             label=v.TIDY_SAID_AGAIN_PENDING % _count(n, complete),
                             command=command("learn", "--approve", lesson_id, "--by", "NAME", repo=repo_arg),
                             commands=[command("learn", "--approve", lesson_id, "--by", "NAME", repo=repo_arg),
                                       command("learn", "--reject", lesson_id, "--by", "NAME", "--reason",
                                               "REASON", repo=repo_arg)]))
    said.sort(key=lambda e: (-e["sessions"], e["id"]))
    again = []
    for lesson_id, node in rejected.items():
        when = _decision_time(node)
        after = _sessions_after(records.get(lesson_id, []), when)
        if after:
            fields = mb._lesson_fields(node)
            reconsider = command("learn", "--approve", lesson_id, "--by", "NAME", "--reconsider", repo=repo_arg)
            again.append(dict({"id": lesson_id, "tier": REJECTED,
                               "edgeTypes": sorted({e.get("edgeType") for e in node.get("edges", [])
                                                    if isinstance(e, dict) and e.get("edgeType")}),
                               "quoteLines": list(fields.get("quoteLines") or [mb.lesson_text(node)]),
                               "cites": _cites(fields)}, **sight[lesson_id], rejectedAt=_iso(when),
                              sessionsAfter=after, sessionsLabel=_count(after, complete),
                              label=v.TIDY_SAID_AGAIN_REJECTED % (_count(after, complete), _iso(when)),
                              command=reconsider, commands=[reconsider]))
    again.sort(key=lambda e: (-e["sessionsAfter"], e["id"]))
    said_again = said + again

    # retire, unattached, promote -------------------------------------------------------------------------------
    approved = {i: it for i, it in visible.items() if it["tier"] == APPROVED}
    rules = _instruction_rules(root) if approved else {}
    anchors, page_trusted = _promoted_anchors(root) if approved else ({}, True)
    if not page_trusted:
        notices.append(v.TIDY_PAGE_UNTRUSTED)
    retire, unattached, promote = [], [], []
    for lesson_id in sorted(approved):
        item, node = approved[lesson_id], nodes[lesson_id]
        reasons, details = [], {}
        inactive = [p for p in item.get("approvalPaths") or [] if p not in reached.get(lesson_id, [])]
        if inactive:
            reasons.append("attachment-inactive")
            details["inactivePaths"] = inactive
        if lesson_id in anchors:
            stale = _anchor_stale(root, *anchors[lesson_id])
            if stale:
                reasons.append("anchored-code-changed")
                details["anchor"] = {"anchor": anchors[lesson_id][0], "reason": stale}
        matched = sorted({src for _, raw in _current_lines(node) for src in rules.get(md.normalised_quote(raw), [])})
        if matched:
            reasons.append("now-a-rule")
            details["rules"] = matched
        approved_at = _decision_time(node)
        if (complete and approved_at is not None and now - approved_at >= datetime.timedelta(days=stale_days)
                and not _sessions_after(records.get(lesson_id, []), approved_at)):
            reasons.append("old-unseen")
            details["approvedAt"] = _iso(approved_at)
            details["lastSaid"] = sight[lesson_id]["last"]
        if reasons:
            retire.append(dict(_lesson(item), reasons=reasons, labels=[v.TIDY_RETIRE_LABELS[r] for r in reasons],
                               details=details,
                               command=command("learn", "--reject", lesson_id, "--by", "NAME", "--reason",
                                               "tidy: " + ", ".join(reasons), repo=repo_arg)))
        files = list(reached.get(lesson_id, []))
        if not files and attachment_complete:
            unattached.append(dict(_lesson(item), label=v.TIDY_UNATTACHED_LABEL,
                                   command=command("learn", "--paths", lesson_id, "--by", "NAME", "--path", "FILE",
                                                   repo=repo_arg)))
        sessions = sight[lesson_id]["sessions"]
        if files and sessions >= min_seen and {"BLOCKS", "GOVERNS"} & set(item.get("edgeTypes", [])):
            promote.append(dict(_lesson(item), paths=files, sessions=sessions,
                                sessionsLabel=_count(sessions, complete),
                                label=v.TIDY_PROMOTE_LABEL % _count(sessions, complete)))
    if not attachment_complete:
        notices.append(v.TIDY_ATTACHMENT_UNKNOWN)
    promote.sort(key=lambda e: (-e["sessions"], e["id"]))
    promote = _capped(promote, omitted, "promote")
    instruction_files = mw.discover_instruction_files(root) if promote else []
    for entry in promote:
        entry.update(_promote_block(root, nodes[entry["id"]], approved[entry["id"]], anchors, instruction_files))
        entry["command"] = command("check", repo=repo_arg)

    # lapsed merges ---------------------------------------------------------------------------------------------
    lapsed = []
    for lesson_id in sorted(merge["states"]):
        state = merge["states"][lesson_id] or {}
        if state.get("state") != "lapsed" or lesson_id not in nodes:
            continue
        node = nodes[lesson_id]
        base = _lesson(items[lesson_id]) if lesson_id in items else {
            "id": lesson_id, "tier": REJECTED if lesson_id in rejected else PENDING, "edgeTypes": [],
            "quoteLines": list(mb._lesson_fields(node).get("quoteLines") or [mb.lesson_text(node)]),
            "cites": _cites(mb._lesson_fields(node))}
        into = state.get("into")
        unmerge = command("learn", "--unmerge", lesson_id, "--by", "NAME", repo=repo_arg)
        commands = [unmerge] + ([command("learn", "--merge", into, "--same", lesson_id, "--by", "NAME", repo=repo_arg)]
                                if into else [])
        lapsed.append(dict(base, into=into, reason=state.get("reason"),
                           label=v.TIDY_LAPSED_LABEL % (into, state.get("reason")), command=unmerge,
                           commands=commands))

    result = {"ok": True, "exitCode": 0,
              "duplicates": _capped(duplicates, omitted, "duplicates"),
              "saidAgain": _capped(said_again, omitted, "saidAgain"),
              "retire": _capped(retire, omitted, "retire"),
              "unattached": _capped(unattached, omitted, "unattached"),
              "promote": promote,
              "lapsedMerges": _capped(lapsed, omitted, "lapsedMerges"),
              "omitted": {s: omitted.get(s, 0) for s in v.TIDY_SECTIONS},
              "complete": complete, "notices": notices, "mergeChecks": merge["available"],
              "scanned": scan["scanned"], "work": dict(scan["work"]), "skipped": scan["skipped"]}
    result["formatted"] = render_markdown(result)
    return result


def _promote_block(root: Path, node: Dict[str, Any], item: Dict[str, Any],
                   page_anchors: Dict[str, Tuple[str, Dict[str, Any]]], instruction_files: List[str]) -> Dict[str, Any]:
    """The cited lines verbatim (current lines only) and the anchor promotion would give: the promoted page's sticky
    anchor, else `memlet_bridge._resolve_lesson_anchor`. Nothing is written."""
    roles = item.get("lineRoles") or ["current"] * len(item.get("quoteLines", []))
    lines = [q for q, role in zip(item.get("quoteLines") or [item.get("text", "")], roles) if role != "replaced"]
    if node["id"] in page_anchors:
        anchor = page_anchors[node["id"]][0]
    else:
        found = mb._resolve_lesson_anchor(root, node)
        anchor = "%s::%s" % found if found else None
    block = ["- " + lines[0]] + ["  " + line for line in lines[1:]]
    if anchor:
        block[-1] += " (`" + anchor + "`)"
    return {"block": "\n".join(block) + "\n", "suggestedAnchor": anchor, "instructionFiles": list(instruction_files),
            "suggestion": v.TIDY_PROMOTE_SUGGESTION}


# ---------------------------------------------------------------------------------------------------------------
# Markdown


def _fence(text: str) -> str:
    longest = max([len(m.group()) for m in re.finditer(r"`+", text)] + [2])
    return "`" * (longest + 1)


def _code(text: str) -> str:
    """Inline code span that a backtick inside cannot close early."""
    ticks = "`" * (max([len(m.group()) for m in re.finditer(r"`+", text)] + [0]) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return ticks + pad + text + pad + ticks


def _quotes(entry: Dict[str, Any]) -> List[str]:
    out = []
    for i, quote in enumerate(entry.get("quoteLines", [])):
        cite = entry["cites"][i] if i < len(entry.get("cites", [])) else None
        out.append("  > " + quote + (" (" + _code("{file}:{start}-{end}".format(**cite)) + ")" if cite else ""))
    return out


def _commands(entry: Dict[str, Any]) -> List[str]:
    out = ["  " + v.TIDY_COMMAND_LABEL + _code(entry["command"])]
    out += ["  " + v.TIDY_ALSO_LABEL + _code(c) for c in entry.get("commands", []) if c != entry["command"]]
    return out


def render_markdown(result: Dict[str, Any]) -> str:
    lines = [v.TIDY_HEAD, "", v.TIDY_NOTHING_CHANGED, "",
             "- " + v.TIDY_SCAN_LINE % (result["scanned"]["files"], result["scanned"]["bytes"])]
    lines += ["- " + n for n in result["notices"]]
    for section in v.TIDY_SECTIONS:
        entries = result[section]
        lines += ["", "### " + v.TIDY_SECTION_HEADINGS[section], ""]
        if not entries:
            lines.append("- " + v.TIDY_NONE)
        for e in entries:
            if section == "duplicates":
                lines.append("- [%s] %s %s (%s); same: %s" % (
                    e["label"], v.TIDY_KEEP_LABEL, _code(e["keep"]), e["lessons"][0]["tier"],
                    ", ".join(_code(x["id"]) + " (" + x["tier"] + ")" for x in e["lessons"][1:])))
                for lesson in e["lessons"]:
                    lines.append("  " + _code(lesson["id"]) + ":")
                    lines += ["  " + q for q in _quotes(lesson)]
                if e.get("blockers"):
                    lines.append("  " + v.TIDY_BLOCKERS_LABEL + "; ".join(
                        _code(k) + ": " + ", ".join(c) for k, c in sorted(e["blockers"].items()) if c))
                if any(e.get("lostFiles", {}).values()):
                    lines.append("  " + v.TIDY_LOST_FILES_LABEL + "; ".join(
                        _code(k) + ": " + ", ".join(_code(p) for p in ps) for k, ps in sorted(e["lostFiles"].items()) if ps))
                lines += _commands(e)
                if e.get("pathsCommand"):
                    lines.append("  " + v.TIDY_ALSO_LABEL + _code(e["pathsCommand"]))
                continue
            head = "- " + _code(e["id"]) + " (" + e["tier"] + ")"
            if section == "retire":
                head += ": " + "; ".join("%s — %s" % (r, label) for r, label in zip(e["reasons"], e["labels"]))
            elif "label" in e:
                head += ": " + e["label"]
            lines.append(head)
            lines += _quotes(e)
            if section == "retire":
                d = e["details"]
                if d.get("inactivePaths"):
                    lines.append("  inactive: " + ", ".join(_code(p) for p in d["inactivePaths"]))
                if d.get("anchor"):
                    lines.append("  anchor: " + _code(d["anchor"]["anchor"]) + " (" + d["anchor"]["reason"] + ")")
                if d.get("rules"):
                    lines.append("  rule: " + ", ".join(_code(s) for s in d["rules"]))
                if d.get("approvedAt"):
                    lines.append("  approved: " + d["approvedAt"] + "; last said: " + str(d.get("lastSaid")))
            if section == "promote":
                fence = _fence(e["block"])
                lines.append("  " + e["suggestion"])
                lines += ["  " + fence + "markdown"] + ["  " + b for b in e["block"].splitlines()] + ["  " + fence]
                lines.append("  " + (v.TIDY_ANCHOR_SUGGESTION + _code(e["suggestedAnchor"]) if e["suggestedAnchor"]
                                     else v.TIDY_NO_ANCHOR))
                lines.append("  " + (v.TIDY_INSTRUCTION_FILES + ", ".join(_code(f) for f in e["instructionFiles"])
                                     if e["instructionFiles"] else v.TIDY_NO_INSTRUCTION_FILES))
                lines.append("  " + v.TIDY_PROMOTE_COMMAND_NOTE + ": " + _code(e["command"]))
                continue
            lines += _commands(e)
        if result["omitted"].get(section):
            lines.append("- " + v.TIDY_OMITTED % result["omitted"][section])
    return "\n".join(lines) + "\n"
