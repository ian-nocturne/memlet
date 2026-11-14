"""Memlet 9.3 tidy report: lessons a person might merge, look at again, retire or promote. Strictly read-only.

Nothing here writes: no queue, ledger, page, cache, table or SQLite side file is created or changed, and no model is
called. Tiers and line roles (current / replaced) come from the one reader every other reader uses
(`memlet_bridge.dream_items` and `memlet_dream.lesson_display_fields`); merge state comes only from
`memlet_bridge.merge_view` / `merge_blockers` through `merge_checks`, the single adapter, which says so when this
build has no merge support or when what it returns does not fit the lessons loaded.

Sightings ("said again") are computed at read time from the stored snapshots, which Memlet stores flat in
`.memlet/dream/snapshot/`. The scan opens that directory through no-follow directory descriptors, opens each file at
most once relative to it, and counts its work; anything it could not consider makes the result incomplete. The tier
read inside the shared reader is separate, has its own existing bounds, and is not part of the scan's counters.
"""

from __future__ import annotations

from memlet.memlet_dream import call_foreign as _call_foreign, _foreign_context

import contextlib
import datetime
import os
import re
import shlex
import sqlite3
import stat
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import memlet_bridge as mb
from . import memlet_dream as md
from . import memlet_wiki as mw
from . import vocabulary as v
from .vocabulary import APPROVED, HINT, PENDING, REJECTED

MAX_BYTES = 64 * 1024 * 1024          # snapshot bytes read per scan
MAX_FILES = md.SNAPSHOT_MAX_FILES      # snapshot directory entries listed (and files opened) per scan
MAX_LINE_BYTES = 64 * 1024            # a longer physical line is not considered (a cited line is at most 16,384 chars)
MAX_WHERE = 8
MAX_MATCH_WORK = 2_000_000            # (quote, lesson) hand-overs from per-session quote summaries to lessons
MIN_SIMILAR_WORDS = 5
SIMILAR_JACCARD = 0.8
MAX_SIMILAR_WORK = 2_000_000          # posting-list visits for the similar-wording comparison
MAX_BLOCK_CHARS = 12_000              # a longer promote block is omitted whole, never cut
MAX_STALE_DAYS = 999_999_999          # the largest day count `datetime.timedelta` can represent

_USER_LINE_RE = re.compile(r"^\s*(?:\d{4}-\d{2}-\d{2}[T ][^\s]+\s*)?\[user\]")
# `<stem>-<10 hex>.log` (a transcript's snapshot) and `<stem>-<10 hex>-<10 hex>.log` (the same transcript rewritten).
_SESSION_SUFFIX_RE = re.compile(r"(?:-[0-9a-f]{10}){1,2}\.log$")
_WORD_RE = re.compile(r"\w+")
_SQLITE_MAGIC = b"SQLite format 3\x00"
_FAR = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------------------------------------------
# small helpers


def _now(clock: Optional[Callable[[], Any]]) -> datetime.datetime:
    value = _call_foreign(lambda: clock()) if clock is not None else datetime.datetime.now(datetime.timezone.utc)
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
    """A session is identified by its stored file name: the snapshot name without its trailing `-<10 hex>.log` (and
    the second hash a rewritten transcript adds). So a rewritten transcript counts once; a copy stored under another
    name counts again, and different transcripts with the same file name count once."""
    base = _SESSION_SUFFIX_RE.sub("", file_id)
    return base if base and base != file_id else file_id


def command(words: Sequence[str], options: Sequence[Tuple[str, Any]] = (), *, repo: Optional[str] = None
            ) -> Optional[str]:
    """A POSIX-shell command line that runs only `memlet` and that the memlet argument parser binds as intended.

    Every option value is passed as `--option=value`, so a value starting with `-` is never read as an option. An
    option that takes several values (`--same A B`) cannot use that form; when one of them starts with `-`, or any
    value holds a NUL character, no command is returned (None)."""
    argv = ["memlet"] + list(words)
    for flag, value in options:
        if value is True:
            argv.append(flag)
        elif isinstance(value, (list, tuple)):
            values = [str(x) for x in value]
            if len(values) == 1:
                argv.append(flag + "=" + values[0])
            elif not values or any(x.startswith("-") for x in values):
                return None
            else:
                argv += [flag] + values
        else:
            argv.append(flag + "=" + str(value))
    if repo is not None:
        argv.append("--repo=" + repo)
    if any("\x00" in a for a in argv):
        return None
    return " ".join(shlex.quote(a) for a in argv)


def _with_command(entry: Dict[str, Any], cmd: Optional[str], note: str = v.TIDY_UNSAFE_ARGUMENT) -> Dict[str, Any]:
    entry["command"] = cmd
    if cmd is None:
        entry["commandNote"] = note
    return entry


def _decision_time(node: Dict[str, Any]) -> Optional[datetime.datetime]:
    record = node.get("approval") if isinstance(node.get("approval"), dict) else {}
    return md.parse_iso_dt(record.get("at")) if md._valid_utc_stamp(record.get("at")) else None


def _strip(raw: str) -> str:
    return md._LINE_PREFIX_RE.sub("", raw, count=1).strip()


# ---------------------------------------------------------------------------------------------------------------
# lessons through the shared reader


def _raw_index(graph: Dict[str, Any]) -> Dict[Tuple[str, str], str]:
    """{(span, sha256): raw cited line} from every stored pin, so a line the reader projects (also from another
    lesson's pin) can be matched in full. A lookup by hash only; it decides no role and no supersession."""
    out: Dict[Tuple[str, str], str] = {}

    def add(pin):
        if not isinstance(pin, dict):
            return
        parsed = md.parse_span(str(pin.get("span", "")))
        if not parsed:
            return
        quotes = pin.get("quoteLines") or str(pin.get("quoteText", "")).split("\n")
        for number, quote in zip(range(parsed[1], parsed[2] + 1), quotes):
            if isinstance(quote, str):
                out.setdefault(("%s:%d" % (parsed[0], number), md.sha256_hex(quote)), quote)

    def edge_pins(edge):
        if isinstance(edge, dict):
            for key in ("srcPin", "targetPin", "citedTargetPin"):
                add(edge.get(key))

    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            continue
        for pin in [node.get("primaryPin"), node.get("supersededByPin")] + list(node.get("allPins") or []):
            add(pin)
        for edge in node.get("edges", []) or []:
            edge_pins(edge)
    for edge in graph.get("edges", []) or []:
        edge_pins(edge)
    return out


def _line_set(fields: Dict[str, Any], raw: Dict[Tuple[str, str], str]) -> List[Dict[str, Any]]:
    """[{role, raw, quote, cite}] in the reader's order, with the reader's roles (`current` when it gives none)."""
    cites = fields.get("cites", [])
    roles = fields.get("lineRoles") or ["current"] * len(cites)
    quotes = fields.get("quoteLines") or []
    out = []
    for i, cite in enumerate(cites):
        out.append({"role": roles[i] if i < len(roles) else "current",
                    "raw": raw.get((cite.get("span"), cite.get("sha256"))),
                    "quote": quotes[i] if i < len(quotes) else "", "cite": cite})
    return out


def _current(lines: List[Dict[str, Any]]) -> Optional[List[str]]:
    """Raw current lines, or None when one of them cannot be read in full."""
    current = [ln for ln in lines if ln["role"] != "replaced"]
    if any(ln["raw"] is None for ln in current):
        return None
    return [ln["raw"] for ln in current]


def _load_lessons(root: Path, graph: Dict[str, Any], *, path_context=None) -> Dict[str, Any]:
    nodes = {n["id"]: n for n in graph.get("nodes", []) if isinstance(n, dict) and isinstance(n.get("id"), str)}
    cm = contextlib.nullcontext(path_context) if path_context is not None else mb._lesson_path_projection(root, None)
    with cm as context:
        read = (mb.dream_items(root, tiers=(APPROVED, HINT, PENDING), _graph=graph, _path_context=context) if nodes
                else {"ok": True, "items": [], "notices": []})
        reached = {k: list(p) for k, p in context["paths"].items()}
        attachment_complete = not context.get("attachmentIncomplete")
    raw = _raw_index(graph)
    lessons: Dict[str, Dict[str, Any]] = {}
    for item in read.get("items", []):
        lines = _line_set(item, raw)
        lessons[item["id"]] = {"id": item["id"], "tier": item["tier"], "item": item, "lines": lines,
                               "current": _current(lines), "node": nodes.get(item["id"], {})}
    for lesson_id, node in nodes.items():
        if lesson_id in lessons or mb.item_tier(node, snapshot=None, config={}) != REJECTED:
            continue
        if md.approval_state(node, "node") != REJECTED:
            continue  # a model's REJECTED verdict alone has no rejection time
        fields = mb._lesson_fields(node)
        lines = _line_set(fields, raw)
        item = {"id": lesson_id, "tier": REJECTED,
                "edgeTypes": sorted({e.get("edgeType") for e in node.get("edges", [])
                                     if isinstance(e, dict) and e.get("edgeType")}),
                **fields}
        lessons[lesson_id] = {"id": lesson_id, "tier": REJECTED, "item": item, "lines": lines,
                              "current": _current(lines), "node": node}
    return {"ok": read["ok"], "notices": list(read.get("notices", [])), "lessons": lessons, "nodes": nodes,
            "reached": reached, "attachmentComplete": attachment_complete}


def _lesson(lesson: Dict[str, Any]) -> Dict[str, Any]:
    item = lesson["item"]
    out = {"id": lesson["id"], "tier": lesson["tier"], "edgeTypes": list(item.get("edgeTypes", [])),
           "quoteLines": [ln["quote"] for ln in lesson["lines"]] or [item.get("text", "")],
           "cites": [{"file": ln["cite"]["file"], "start": ln["cite"]["start"], "end": ln["cite"]["end"]}
                     for ln in lesson["lines"]]}
    if any(ln["role"] != "current" for ln in lesson["lines"]) or item.get("quarantined"):
        out["lineRoles"] = [ln["role"] for ln in lesson["lines"]]
    return out


def _earliest(lesson: Dict[str, Any]) -> Optional[datetime.datetime]:
    times = [md.parse_iso_dt(md.extract_line_timestamp(raw)) for raw in lesson["current"] or []]
    times = [t for t in times if t is not None]
    return min(times) if times else None


# ---------------------------------------------------------------------------------------------------------------
# merge awareness: the one adapter


def _merge_none(notice: Optional[str]) -> Dict[str, Any]:
    return {"available": False, "states": {}, "members": {}, "blockers": None, "notice": notice}


def _valid_view(view: Any, ids: Set[str]) -> bool:
    """Shapes and ids only; whether a merge holds is `merge_view`'s answer, never decided here."""
    if not isinstance(view, dict) or view.get("ok") is not True:
        return False
    states, members = view.get("states"), view.get("members")
    if not isinstance(states, dict) or not isinstance(members, dict):
        return False
    for lesson_id, state in states.items():
        if (lesson_id not in ids or not isinstance(state, dict)
                or state.get("state") not in ("none", "merged", "lapsed")
                or not (state.get("into") is None or isinstance(state.get("into"), str))
                or not (state.get("reason") is None or isinstance(state.get("reason"), str))):
            return False
        if state["state"] == "merged" and (state.get("into") not in ids or state["into"] == lesson_id
                                           or lesson_id not in (members.get(state["into"]) or [])):
            return False
    seen: Set[str] = set()
    for keep, listed in members.items():
        if keep not in ids or not isinstance(listed, list):
            return False
        for member in listed:
            if not isinstance(member, str) or member not in ids or member == keep or member in seen:
                return False
            seen.add(member)
            state = states.get(member)
            if state is not None and (state.get("state") != "merged" or state.get("into") != keep):
                return False
    return not (seen & set(members))  # a kept lesson is never itself a holding member


def _merge_functions() -> Tuple[Any, Any]:
    """The two merge reads this module may call (`memlet_bridge.merge_view`, `merge_blockers`), or None for one a
    build does not have. The only lookup; tests replace this function, never the bridge's own functions, which the
    bridge itself calls."""
    return getattr(mb, "merge_view", None), getattr(mb, "merge_blockers", None)


@md.fresh_read
def merge_checks(repo_path=".", ids: Optional[Iterable[str]] = None, *, context=None) -> Dict[str, Any]:
    """The single place this module learns about merges: `memlet_bridge.merge_view` and `merge_blockers` when this
    build has both, else "merge checks not available in this build". What `merge_view` returns is checked against the
    lessons loaded (`ids`); on any inconsistency or exception, merge checks are skipped, nothing is hidden, and the
    notice says so. Returns {available, states, members, blockers: callable or None, notice}."""
    view_fn, blockers_fn = _merge_functions()
    if not callable(view_fn) or not callable(blockers_fn):
        return _merge_none(v.TIDY_MERGE_UNAVAILABLE)
    try:
        context = {} if context is None else context
        view = view_fn(repo_path, context=context)
    except Exception:  # noqa: BLE001 - any adapter failure is reported, never raised through the report
        return _merge_none(v.TIDY_MERGE_UNREADABLE)
    if not isinstance(view, dict) or view.get("ok") is not True:
        return _merge_none(v.TIDY_MERGE_UNREADABLE)
    if ids is not None and not _valid_view(view, set(ids)):
        return _merge_none(v.TIDY_MERGE_SKIPPED)

    def blockers(keep, same):
        return blockers_fn(repo_path, keep, tuple(same), context=context)

    return {"available": True, "states": view["states"], "members": view["members"], "blockers": blockers,
            "notice": None}


def _checked_blockers(merge: Dict[str, Any], keep: str, same: List[str]) -> Optional[Dict[str, Any]]:
    """`merge_blockers` output when it could be computed (ok true, shapes valid), else None. A merge that would be
    refused is non-empty `blockers` with ok true."""
    try:
        checked = merge["blockers"](keep, same)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(checked, dict) or checked.get("ok") is not True:
        return None
    out = {}
    for key in ("blockers", "lostFiles"):
        value = checked.get(key) or {}
        if (not isinstance(value, dict) or any(k not in same for k in value)
                or any(not isinstance(x, list) or any(not isinstance(y, str) for y in x) for x in value.values())):
            return None
        out[key] = {k: list(x) for k, x in value.items()}
    return out


def _holding_members(merge: Dict[str, Any]) -> Dict[str, str]:
    return {str(m): str(keep) for keep, members in merge["members"].items() for m in members or ()}


# ---------------------------------------------------------------------------------------------------------------
# the sightings scan


class _Work(dict):
    def __init__(self):
        super().__init__(entriesListed=0, filesOpened=0, bytesRead=0, linesRead=0, linesCompared=0, matchWork=0)


def _skipped() -> Dict[str, int]:
    return {"symlinks": 0, "directories": 0, "unsupported": 0, "unreadable": 0, "hugeLines": 0,
            "invalidUtf8Lines": 0, "overBudget": 0, "overFileCount": 0, "overMatchWork": 0}


def _dir_fd_supported() -> bool:
    return (os.open in os.supports_dir_fd and os.scandir in os.supports_fd
            and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"))


@contextlib.contextmanager
def _snapshot_dir(root: Path):
    """A descriptor of `.memlet/dream/snapshot`, each component opened with O_NOFOLLOW relative to its parent.
    Yields (fd or None, state) with state "ok", "missing" or "refused"."""
    fds: List[int] = []
    try:
        fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
        fds.append(fd)
        for part in mb.SNAPSHOT_REL.split("/"):
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            fds.append(fd)
        state = "ok"
    except FileNotFoundError:
        state = "missing"
    except OSError:
        state = "refused"
    try:
        yield (fds[-1] if state == "ok" else None), state
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _list(dir_fd: int, work: _Work, skipped: Dict[str, int]) -> Tuple[List[str], bool]:
    """Names of regular `.log` files, at most MAX_FILES entries listed; (names, every entry considered)."""
    names, complete = [], True
    with os.scandir(dir_fd) as it:
        for entry in it:
            if work["entriesListed"] >= MAX_FILES:
                skipped["overFileCount"] += 1
                return sorted(names), False
            work["entriesListed"] += 1
            try:
                if entry.is_symlink():
                    skipped["symlinks"] += 1
                    complete = False
                    continue
                if entry.is_dir(follow_symlinks=False):
                    skipped["directories"] += 1  # snapshots are stored flat: never descended
                    complete = False
                    continue
                regular = entry.is_file(follow_symlinks=False)
            except OSError:
                skipped["unreadable"] += 1
                complete = False
                continue
            if not regular or not entry.name.endswith(".log") or not md._is_safe_file_id(entry.name):
                skipped["unsupported"] += 1  # includes `.jsonl`, which this scan does not parse
                complete = False
                continue
            names.append(entry.name)
    return sorted(names), complete


def _read(dir_fd: int, name: str, remaining: int, work: _Work, skipped: Dict[str, int]) -> Optional[bytes]:
    """One open per file, relative to the snapshot directory, never following a symlink or blocking on a FIFO."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError:
        skipped["unreadable"] += 1
        return None
    work["filesOpened"] += 1
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            skipped["unsupported"] += 1
            return None
        if st.st_size > remaining:
            skipped["overBudget"] += 1
            return None
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
            return None
        return b"".join(chunks)
    except OSError:
        skipped["unreadable"] += 1
        return None
    finally:
        os.close(fd)


def _scan(root: Path, wanted: Dict[str, List[str]], *, max_bytes: int, now: datetime.datetime) -> Dict[str, Any]:
    """Per lesson, one record per snapshot file in which every wanted quote was said by the user:
    [{session, file, line, early, late}]. Matches are summarised per quote and file first (first line, earliest and
    latest time); only the summaries are handed to the lessons that want them (counted in `matchWork`)."""
    work, skipped = _Work(), _skipped()
    records: Dict[str, List[Dict[str, Any]]] = {i: [] for i in wanted}
    scanned = {"files": 0, "bytes": 0}
    need: Dict[str, List[str]] = {}
    required: Dict[str, Tuple[str, ...]] = {}
    for lesson_id, quotes in wanted.items():
        required[lesson_id] = tuple(dict.fromkeys(quotes))
        for quote in required[lesson_id]:
            need.setdefault(quote, []).append(lesson_id)
    out = {"records": records, "complete": True, "work": work, "skipped": skipped, "scanned": scanned, "notices": []}
    if not _dir_fd_supported():
        out.update(complete=False, notices=[v.TIDY_SCAN_UNSUPPORTED])
        return out
    with _snapshot_dir(root) as (dir_fd, state):
        if state != "ok":
            out["notices"].append(v.TIDY_SNAPSHOT_UNAVAILABLE)
            out["complete"] = state == "missing"
            return out
        names, complete = _list(dir_fd, work, skipped)
        budget = max(0, int(max_bytes))
        for name in names:
            data = _read(dir_fd, name, budget - work["bytesRead"], work, skipped)
            if data is None:
                complete = False
                continue
            scanned["files"] += 1
            scanned["bytes"] += len(data)
            summary: Dict[str, List[Any]] = {}  # quote -> [first line, earliest time, latest time]
            raw_lines = data.split(b"\n")
            if raw_lines and raw_lines[-1] == b"":
                raw_lines.pop()
            for number, raw in enumerate(raw_lines, start=1):
                work["linesRead"] += 1
                if len(raw) > MAX_LINE_BYTES:
                    skipped["hugeLines"] += 1
                    complete = False
                    continue
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError:
                    skipped["invalidUtf8Lines"] += 1
                    complete = False
                    continue
                if not need or not _USER_LINE_RE.match(line):
                    continue
                work["linesCompared"] += 1
                quote = md.normalised_quote(line)
                if quote not in need:
                    continue
                when = _line_time(line, now)
                seen = summary.get(quote)
                if seen is None:
                    summary[quote] = [number, when, when]
                elif when is not None:
                    seen[1] = when if seen[1] is None else min(seen[1], when)
                    seen[2] = when if seen[2] is None else max(seen[2], when)
            handed: Set[str] = set()
            for quote in summary:
                work["matchWork"] += len(need[quote])
                if work["matchWork"] > MAX_MATCH_WORK:
                    skipped["overMatchWork"] += 1
                    out["complete"] = False
                    return out
                for lesson_id in need[quote]:
                    if lesson_id in handed:
                        continue
                    handed.add(lesson_id)
                    parts = [summary.get(q) for q in required[lesson_id]]
                    if any(p is None for p in parts):
                        continue  # every cited line must be said in this one session
                    early = late = None
                    if all(p[1] is not None for p in parts):
                        early = max(p[1] for p in parts)
                        late = min(p[2] for p in parts)
                    records[lesson_id].append({"session": session_key(name), "file": name, "line": parts[0][0],
                                               "early": early, "late": late})
    out["complete"] = complete
    return out


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


def _sightings(root: Path, lessons: Dict[str, Dict[str, Any]], merge: Dict[str, Any], *, max_bytes: int,
               now: datetime.datetime, graph=None, path_context=None) -> Dict[str, Any]:
    reached = {}
    # Only this scan asks the shared reader for holding members. It supplies the same
    # verified current/replaced projection that delivery uses, without exposing members.
    if _holding_members(merge):
        raw = _raw_index(graph or {})
        cm = contextlib.nullcontext(path_context) if path_context is not None else mb._lesson_path_projection(root, None)
        with cm as ctx:
            read = mb.dream_items(root, tiers=(APPROVED, HINT, PENDING), _graph=graph, include_merged=True, _path_context=ctx)
            reached = {i: list(paths) for i, paths in ctx["paths"].items()}
        lessons = dict(lessons)
        for item in read.get("items", []):
            if item.get("mergedInto"):
                lines = _line_set(item, raw)
                lessons[item["id"]] = {"current": _current(lines)}
    wanted = {}
    for lesson_id, lesson in lessons.items():
        quotes = [md.normalised_quote(raw) for raw in lesson["current"] or []]
        if quotes and all(quotes):
            wanted[lesson_id] = quotes
    scan = _scan(root, wanted, max_bytes=max_bytes, now=now)
    records = {i: list(scan["records"].get(i, [])) for i in lessons}
    # A holding merge: the kept lesson counts distinct sessions across the whole group.
    for member, keep in _holding_members(merge).items():
        if keep in records:
            records[keep] = records[keep] + records.get(member, [])
    scan["grouped"] = records
    scan["reached"] = reached
    return scan


def _load(repo_path) -> Tuple[Path, Dict[str, Any], str]:
    root = mw._resolve_root(repo_path)
    problem = mb._symlinked(root, (mb.QUEUE_REL, mb.SNAPSHOT_REL, mw.CONFIG_PATH))
    if problem:
        raise ValueError(mb._refusing(problem))
    graph, why = mb._load_queue(root)
    return root, graph or {"nodes": [], "edges": []}, why


@md.fresh_read
def lesson_sightings(repo_path=".", *, ids=None, max_bytes=MAX_BYTES, clock=None) -> Dict[str, Any]:
    """{ok, complete, scanned: {files, bytes}, sightings: {id: {sessions, first, last, where}}} plus `work` (entries
    listed, files opened, bytes read, lines read and compared, match work), `skipped` and `notices`. Read-only."""
    try:
        root, graph, why = _load(repo_path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "complete": False, "scanned": {"files": 0, "bytes": 0},
                "sightings": {}}
    now = _now(clock)
    with mb._lesson_path_projection(root, None) as path_context:
        loaded = _load_lessons(root, graph, path_context=path_context)
        if not loaded["ok"]:
            error = "; ".join(loaded["notices"]) or v.GRAPH_EVIDENCE_ERROR
            return {"ok": False, "error": error, "complete": False, "scanned": {"files": 0, "bytes": 0}, "sightings": {}}
        merge = merge_checks(root, loaded["nodes"], context={"graph": graph, "pathContext": path_context})
        scan = _sightings(root, loaded["lessons"], merge, max_bytes=max_bytes, now=now, graph=graph, path_context=path_context)
    keep = None if ids is None else set(mb._ids(ids))
    sightings = {i: _summary(r) for i, r in scan["grouped"].items() if keep is None or i in keep}
    notices = ([why] if why else []) + scan["notices"] + ([merge["notice"]] if merge["notice"] else [])
    if not scan["complete"]:
        notices.append(v.TIDY_INCOMPLETE)
    return {"ok": True, "complete": scan["complete"], "scanned": scan["scanned"], "sightings": sightings,
            "work": dict(scan["work"]), "skipped": scan["skipped"], "notices": notices}


# ---------------------------------------------------------------------------------------------------------------
# instruction rules, the promoted page and its ledger


def _instruction_rules(root: Path) -> Dict[str, List[str]]:
    """{normalised rule text: [source:line]} from the instruction-file parser (never the promoted Dream page)."""
    out: Dict[str, List[str]] = {}
    for rel, text in mw._source_texts(root, mw.discover_instruction_files(root)):
        for rule in mw._extract_rules(text, rel):
            text = re.sub(r"\s+\(`[^`\n]+::[^`\n]+`\)\s*$", "", rule["text"])
            key = md.normalised_quote(text)
            if key:
                out.setdefault(key, []).append("%s:%d" % (rule["source"], rule["line"]))
    return out


def _ledger_rows(root: Path) -> Tuple[Optional[List[Tuple[Any, ...]]], str]:
    """The promoted page's ledger events, read without any side effect, else (None, why).

    SQLite may create or touch `-wal` / `-shm` files even for a read-only connection, so the ledger is opened only
    when its header says rollback-journal mode and no `-wal`, `-shm` or `-journal` file exists, and then with
    `mode=ro&immutable=1`. why: "ok", "absent" (no ledger: the page is not Memlet's) or "side-effects"."""
    if mb._symlinked(root, mw._LEDGER_FILES):
        return None, "absent"
    ledger = root / mw.LEDGER_PATH
    if any(os.path.lexists(str(ledger) + suffix) for suffix in ("-wal", "-shm", "-journal")):
        return None, "side-effects"
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(ledger), flags)
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "side-effects"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "side-effects"
        header = os.read(fd, 100)
    except OSError:
        return None, "side-effects"
    finally:
        os.close(fd)
    if len(header) < 100 or not header.startswith(_SQLITE_MAGIC) or header[18] != 1 or header[19] != 1:
        return None, "side-effects"  # WAL mode (2), another format, or not a database: never opened
    try:
        with contextlib.closing(sqlite3.connect(ledger.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
            rows = conn.execute("SELECT id, content_sha256, event_type FROM wiki_events WHERE page=? ORDER BY id",
                                (mb.PROMOTED_PAGE,)).fetchall()
    except sqlite3.Error:
        return None, "side-effects"
    return rows, "ok"


def _promoted_anchors(root: Path) -> Tuple[Dict[str, Tuple[str, Dict[str, Any]]], Dict[str, str], str]:
    """(trusted {id: (path::symbol, pin)}, {id: path::symbol} on the page whatever its trust, state).

    The page counts only when it is exactly what Memlet last wrote, per the ledger: the rule the shared readers apply
    (`memlet_bridge._page_edit_problem(for_reader=True)`), evaluated here on a side-effect-free ledger read.
    state: "trusted", "none" (no page), "untrusted" or "side-effects"."""
    page = root / mb.PROMOTED_PAGE
    if mb._symlinked(root, (mb.PROMOTED_PAGE,)) or not page.is_file():
        return {}, {}, "none"
    text = mw.read_regular_text(page, mw.MAX_WIKI_PAGE_BYTES, errors="replace")
    if text is None:
        return {}, {}, "untrusted"
    pinned = {r["id"]: (r["pins"][0]["key"], r["pins"][0]["pin"]) for r in mb._parse_promoted(text) if r["pins"]}
    listed = {k: a[0] for k, a in pinned.items()}
    if mb._PAGE_META not in text:
        return {}, listed, "untrusted"
    rows, why = _ledger_rows(root)
    if why == "side-effects":
        return {}, listed, "side-effects"
    if not rows or not any(r[2] == "dream_promote" for r in rows):
        return {}, listed, "untrusted"
    last = rows[-1]
    if last[2] not in mb._MEMLET_PAGE_EVENTS or last[1] != mw._page_body_sha256(text):
        return {}, listed, "untrusted"
    return pinned, listed, "trusted"


def _anchor_stale(root: Path, key: str, pin: Dict[str, Any]) -> Optional[str]:
    path, _, symbol = key.partition("::")
    if not symbol:
        return None
    entry = mw.classify_anchor(root, {"path": path, "symbol": symbol, "pin": pin})
    return entry["reason"] if entry["status"] == "stale" else None


# ---------------------------------------------------------------------------------------------------------------
# duplicates


def _similar_pairs(keys: List[str], work: Dict[str, int]) -> Tuple[Dict[str, Set[str]], bool]:
    """{quote: quotes whose word sets have Jaccard >= 0.8 with it (both >= 5 distinct words)}; (pairs, complete).
    Every posting visit is counted in `work["similarityWork"]` and checked against MAX_SIMILAR_WORK."""
    words = {k: frozenset(_WORD_RE.findall(k)) for k in keys}
    eligible = [k for k in keys if len(words[k]) >= MIN_SIMILAR_WORDS]
    postings: Dict[str, List[int]] = {}
    for i, k in enumerate(eligible):
        for w in words[k]:
            postings.setdefault(w, []).append(i)
    pairs: Dict[str, Set[str]] = {k: set() for k in keys}
    for i, k in enumerate(eligible):
        overlap: Dict[int, int] = {}
        for w in words[k]:
            for j in postings[w]:
                if work["similarityWork"] >= MAX_SIMILAR_WORK:
                    return pairs, False
                work["similarityWork"] += 1
                if j > i:
                    overlap[j] = overlap.get(j, 0) + 1
        for j, inter in overlap.items():
            other = eligible[j]
            if inter / (len(words[k]) + len(words[other]) - inter) >= SIMILAR_JACCARD:
                pairs[k].add(other)
                pairs[other].add(k)
    return pairs, True


def _similar_groups(texts: Dict[str, List[str]]) -> Tuple[List[List[str]], bool]:
    """Groups of quotes, each formed around its first quote (in `texts` order) with only quotes similar to that one;
    used by tests and by `_duplicates`, which passes quotes in kept-lesson order."""
    work = {"similarityWork": 0}
    pairs, complete = _similar_pairs(list(texts), work)
    groups, taken = [], set()
    for k in texts:
        if k in taken:
            continue
        group = [k] + [o for o in texts if o not in taken and o != k and o in pairs[k]]
        taken.update(group)
        groups.append(group)
    return groups, complete


def kept_lesson_order(lessons: Dict[str, Dict[str, Any]], ids: Iterable[str]) -> List[str]:
    """Kept-lesson order: approved first, then the earliest line time, then the smallest id."""
    return sorted(ids, key=lambda i: (lessons[i]["tier"] != APPROVED, _earliest(lessons[i]) or _FAR, i))


def _choose_kept_lesson(
    lessons: Dict[str, Dict[str, Any]],
    ordered: Sequence[str],
    merge: Dict[str, Any],
    *,
    merge_ok: bool = True,
) -> Tuple[str, List[str], str, Optional[Dict[str, Any]], bool]:
    """Try at most the first three candidates in `ordered`; return `(keep, same, reason, checked, blockers_failed)`."""
    first = ordered[0]
    first_reason = "approved" if lessons[first]["tier"] == APPROVED else "earliest"
    if not merge_ok or not merge.get("available"):
        return first, list(ordered[1:]), first_reason, None, False
    earlier_codes: List[str] = []
    first_checked: Optional[Dict[str, Any]] = None
    for idx, cand in enumerate(ordered[:3]):
        others = [x for x in ordered if x != cand]
        chk = _checked_blockers(merge, cand, others)
        if chk is None:
            return first, list(ordered[1:]), first_reason, None, True
        if idx == 0:
            first_checked = chk
        if not any(chk["blockers"].values()):
            reason = first_reason if idx == 0 else ("the earlier ones cannot be kept: " + ", ".join(earlier_codes))
            return cand, others, reason, chk, False
        for _, codes in sorted(chk["blockers"].items()):
            for code in codes:
                if code not in earlier_codes:
                    earlier_codes.append(code)
    return first, list(ordered[1:]), first_reason, first_checked, False


def _model_proposals_fn():
    from . import memlet_dreamtidy as mdtidy
    return mdtidy.tidy_report_section


def _duplicates(lessons: Dict[str, Dict[str, Any]], visible: List[str], work: Dict[str, int],
                _counters: Optional[Dict[str, int]] = None) -> Tuple[List[Tuple[str, str, List[str]]], bool]:
    """[(kind, keep, members)] where every member says the same as, or reaches Jaccard 0.8 against, its kept lesson.
    Kept lessons are chosen in the contract's order: approved first, then the earliest line time, then the id."""
    order = kept_lesson_order(lessons, visible)
    key = {i: "\n".join(md.normalised_quote(raw) for raw in lessons[i]["current"]) for i in order}
    order = [i for i in order if key[i].strip()]
    pairs, complete = _similar_pairs(list(dict.fromkeys(key[i] for i in order)), work)
    # Candidates for a kept lesson are looked up by wording, not found by scanning every lesson: the same members in
    # the same order as a scan, in work proportional to the lessons that actually match.
    position = {i: n for n, i in enumerate(order)}
    by_key: Dict[str, List[str]] = {}
    for i in order:
        by_key.setdefault(key[i], []).append(i)
    out, taken = [], set()
    for keep in order:
        if keep in taken:
            continue
        taken.add(keep)
        wanted = [key[keep]] + [k for k in pairs.get(key[keep], ()) if k != key[keep]]
        found = [i for k in dict.fromkeys(wanted) for i in by_key.get(k, ())]
        if _counters is not None:  # for tests that count work; never part of the report
            _counters["groupingWork"] = _counters.get("groupingWork", 0) + 1 + len(found)
        members = sorted((i for i in found if i not in taken), key=position.__getitem__)
        if not members:
            continue
        taken.update(members)
        kind = v.TIDY_SAME_TEXT if all(key[i] == key[keep] for i in members) else v.TIDY_SIMILAR_TEXT
        out.append((kind, keep, members))
    return out, complete


# ---------------------------------------------------------------------------------------------------------------
# the report


def _count(value: int, complete: bool) -> Any:
    return value if complete else v.TIDY_AT_LEAST % value


def _capped(entries: List[Dict[str, Any]], omitted: Dict[str, int], section: str) -> List[Dict[str, Any]]:
    omitted[section] = max(0, len(entries) - v.TIDY_SECTION_CAP)
    return entries[:v.TIDY_SECTION_CAP]


def _invalid(stale_days, min_seen) -> bool:
    return (type(stale_days) is not int or not 0 <= stale_days <= MAX_STALE_DAYS
            or type(min_seen) is not int or min_seen < 1)


@md.fresh_read
def tidy_report(repo_path=".", *, stale_days=180, min_seen=3, clock=None, max_bytes=MAX_BYTES) -> Dict[str, Any]:
    """{ok, duplicates, saidAgain, retire, unattached, promote, lapsedMerges, omitted, complete, notices, formatted}
    plus `mergeChecks`, `scanned`, `work` and `skipped`. Writes nothing; proposes only."""
    if _invalid(stale_days, min_seen):
        return {"ok": False, "exitCode": 2, "error": v.TIDY_INVALID_ARGUMENT,
                "formatted": "- ERROR: " + v.TIDY_INVALID_ARGUMENT + "\n"}
    repo_arg = None if str(repo_path) == "." else str(repo_path)
    try:
        root, graph, why = _load(repo_path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "exitCode": 2, "error": str(exc), "formatted": "- ERROR: " + str(exc) + "\n"}
    now = _now(clock)
    notices: List[str] = [why] if why else []
    with mb._lesson_path_projection(root, None) as path_context:
        loaded = _load_lessons(root, graph, path_context=path_context)
        if not loaded["ok"]:
            error = "; ".join(loaded["notices"]) or v.GRAPH_EVIDENCE_ERROR
            return {"ok": False, "exitCode": 1, "error": error, "formatted": "- ERROR: " + error + "\n"}
        notices.extend(n for n in loaded["notices"] if n not in notices)
        lessons, nodes, reached = loaded["lessons"], loaded["nodes"], loaded["reached"]
        merge_context = {"graph": graph, "pathContext": path_context}
        merge = merge_checks(root, nodes, context=merge_context)
        if merge["notice"]:
            notices.append(merge["notice"])
        merge_ok = merge["available"]
        holding = _holding_members(merge)
        scan = _sightings(root, lessons, merge, max_bytes=max_bytes, now=now, graph=graph, path_context=path_context)
    notices.extend(scan["notices"])
    scan_complete = scan["complete"]
    records = scan["grouped"]
    sight = {i: _summary(r) for i, r in records.items()}
    visible = [i for i, l in lessons.items() if l["tier"] != REJECTED and i not in holding]
    work = dict(scan["work"], similarityWork=0)
    omitted: Dict[str, int] = {}

    def cmd(words, options):
        return command(words, options, repo=repo_arg)

    # duplicates ------------------------------------------------------------------------------------------------
    groups, similar_complete = _duplicates(lessons, [i for i in visible if lessons[i]["current"]], work)
    complete = scan_complete and similar_complete
    if not scan_complete:
        notices.append(v.TIDY_INCOMPLETE)
    if not similar_complete:
        notices.append(v.TIDY_SIMILAR_BOUND)
    duplicates, blockers_failed = [], False
    groups.sort(key=lambda g: (-len(g[2]), g[1]))
    omitted["duplicates"] = max(0, len(groups) - v.TIDY_SECTION_CAP)
    for kind, keep0, same0 in groups[:v.TIDY_SECTION_CAP]:
        ordered_group = [keep0] + list(same0)
        keep, same, keep_reason, checked, failed_one = _choose_kept_lesson(
            lessons, ordered_group, merge, merge_ok=merge_ok
        )
        entry = {"kind": kind, "label": v.TIDY_DUPLICATE_LABELS[kind], "keep": keep, "same": same,
                 "lessons": [_lesson(lessons[i]) for i in [keep] + same]}
        if keep != keep0:  # the usual choice (approved, earliest, smallest id) is not explained: 9.3.0 output
            entry["keepReason"] = keep_reason
        if merge_ok:
            _with_command(entry, cmd(["learn"], [("--merge", keep), ("--same", same), ("--by", "NAME")]))
            if failed_one or checked is None:
                blockers_failed = True
                _with_command(entry, None, v.TIDY_BLOCKERS_FAILED)
            else:
                entry.update(checked)
                entry["blockerMessages"] = {i: [v.MERGE_REASONS.get(r, r) for r in reasons]
                                            for i, reasons in checked["blockers"].items()}
                if any(checked["blockers"].values()):
                    _with_command(entry, None, "; ".join(i + ": " + ", ".join(
                        r + " — " + v.MERGE_REASONS.get(r, r) for r in rs)
                        for i, rs in checked["blockers"].items()))
                lost = list(dict.fromkeys(p for paths in checked["lostFiles"].values() for p in paths))
                wanted = list(dict.fromkeys(list(lessons[keep]["item"].get("approvalPaths") or []) + lost))
                if entry.get("command") and lost and lessons[keep]["tier"] == APPROVED and len(wanted) <= 8:
                    paths_cmd = cmd(["learn"], [("--paths", keep), ("--by", "NAME")] + [("--path", p) for p in wanted])
                    if paths_cmd is not None:
                        entry["pathsCommand"] = paths_cmd
        else:
            _with_command(entry, None, v.TIDY_MERGE_COMMAND_UNAVAILABLE)
        duplicates.append(entry)
    if blockers_failed:
        notices.append(v.TIDY_BLOCKERS_FAILED)
        merge_ok = False
    duplicates.sort(key=lambda g: (-len(g["lessons"]), g["keep"]))

    # said again ------------------------------------------------------------------------------------------------
    said = []
    for lesson_id in visible:
        lesson = lessons[lesson_id]
        n = sight[lesson_id]["sessions"]
        if lesson["tier"] in (PENDING, HINT) and n >= 2:
            entry = dict(_lesson(lesson), **sight[lesson_id], sessionsLabel=_count(n, scan_complete),
                         label=v.TIDY_SAID_AGAIN_PENDING % _count(n, scan_complete))
            approve = cmd(["learn"], [("--approve", lesson_id), ("--by", "NAME")])
            reject = cmd(["learn"], [("--reject", lesson_id), ("--by", "NAME"), ("--reason", "REASON")])
            _with_command(entry, approve)
            entry["commands"] = [c for c in (approve, reject) if c is not None]
            said.append(entry)
    said.sort(key=lambda e: (-e["sessions"], e["id"]))
    again = []
    for lesson_id, lesson in lessons.items():
        if lesson["tier"] != REJECTED:
            continue
        when = _decision_time(lesson["node"])
        after = _sessions_after(records.get(lesson_id, []), when)
        if after:
            entry = dict(_lesson(lesson), **sight[lesson_id], rejectedAt=_iso(when), sessionsAfter=after,
                         sessionsLabel=_count(after, scan_complete),
                         label=v.TIDY_SAID_AGAIN_REJECTED % (_count(after, scan_complete), _iso(when)))
            reconsider = cmd(["learn"], [("--approve", lesson_id), ("--by", "NAME"), ("--reconsider", True)])
            _with_command(entry, reconsider)
            entry["commands"] = [reconsider] if reconsider else []
            again.append(entry)
    again.sort(key=lambda e: (-e["sessionsAfter"], e["id"]))

    # retire, unattached, promote -------------------------------------------------------------------------------
    approved = sorted(i for i in visible if lessons[i]["tier"] == APPROVED)
    rules = _instruction_rules(root) if approved else {}
    anchors, listed, page_state = _promoted_anchors(root) if approved else ({}, {}, "none")
    if page_state == "side-effects":
        notices.append(v.TIDY_LEDGER_SIDE_EFFECTS)
    elif page_state == "untrusted":
        notices.append(v.TIDY_PAGE_UNTRUSTED)
    page_unchecked = page_state in ("side-effects", "untrusted")
    retire, unattached, promote = [], [], []
    for lesson_id in approved:
        lesson = lessons[lesson_id]
        item = lesson["item"]
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
        elif page_unchecked and lesson_id in listed:
            details["anchor"] = {"anchor": listed[lesson_id], "reason": v.TIDY_NOT_CHECKED}
        current = lesson["current"] or []
        matched = [{"line": mb.safe_lesson_display(_strip(raw)), "rule": src} for raw in current
                   for src in rules.get(md.normalised_quote(raw), [])]
        if matched:
            reasons.append("now-a-rule")
            details["rules"] = matched
        approved_at = _decision_time(lesson["node"])
        if (scan_complete and approved_at is not None and (now - approved_at).days >= stale_days
                and not _sessions_after(records.get(lesson_id, []), approved_at)):
            reasons.append("old-unseen")
            details["approvedAt"] = _iso(approved_at)
            details["lastSaid"] = sight[lesson_id]["last"]
        if reasons:
            entry = dict(_lesson(lesson), reasons=reasons, labels=[v.TIDY_RETIRE_LABELS[r] for r in reasons],
                         details=details)
            retire.append(_with_command(entry, cmd(["learn"], [("--reject", lesson_id), ("--by", "NAME"),
                                                               ("--reason", "tidy: " + ", ".join(reasons))])))
        files = list(reached.get(lesson_id, []))
        if not files and loaded["attachmentComplete"]:
            entry = dict(_lesson(lesson), label=v.TIDY_UNATTACHED_LABEL)
            unattached.append(_with_command(entry, cmd(["learn"], [("--paths", lesson_id), ("--by", "NAME"),
                                                                   ("--path", "FILE")])))
        sessions = sight[lesson_id]["sessions"]
        if (files and current and not matched and sessions >= min_seen and not item.get("quarantined")
                and {"BLOCKS", "GOVERNS"} & set(item.get("edgeTypes", []))):
            promote.append(dict(_lesson(lesson), paths=files, sessions=sessions,
                                sessionsLabel=_count(sessions, scan_complete),
                                label=v.TIDY_PROMOTE_LABEL % _count(sessions, scan_complete)))
    if not loaded["attachmentComplete"]:
        notices.append(v.TIDY_ATTACHMENT_UNKNOWN)
    promote.sort(key=lambda e: (-e["sessions"], e["id"]))
    promote = _capped(promote, omitted, "promote")
    instruction_files = mw.discover_instruction_files(root) if promote else []
    for entry in promote:
        entry.update(_promote_block(root, lessons[entry["id"]], anchors, page_unchecked, instruction_files))
        _with_command(entry, cmd(["check"], []))
        entry["commandLabel"] = v.TIDY_PROMOTE_COMMAND_NOTE

    # lapsed merges ---------------------------------------------------------------------------------------------
    lapsed = []
    lapsed_ids = [i for i in sorted(merge["states"]) if merge["states"][i].get("state") == "lapsed" and i in lessons]
    omitted["lapsedMerges"] = max(0, len(lapsed_ids) - v.TIDY_SECTION_CAP)
    for lesson_id in lapsed_ids[:v.TIDY_SECTION_CAP]:
        state = merge["states"][lesson_id]
        into, reason = state.get("into"), state.get("reason")
        unmerge = cmd(["learn"], [("--unmerge", lesson_id), ("--by", "NAME")])
        checked = _checked_blockers(merge, into, [lesson_id]) if into and merge_ok else None
        again_cmd = (cmd(["learn"], [("--merge", into), ("--same", lesson_id), ("--by", "NAME")])
                     if checked is not None and not any(checked["blockers"].values()) else None)
        message = v.MERGE_REASONS.get(reason, reason)
        entry = dict(_lesson(lessons[lesson_id]), into=into, reason=reason, message=message,
                     label=(v.TIDY_LAPSED_LABEL % (into, reason + " — " + message) if into else v.MERGE_INVALID_LINE))
        _with_command(entry, unmerge)
        entry["commands"] = [c for c in (unmerge, again_cmd) if c is not None]
        lapsed.append(entry)

    merged_lost = []
    # The shared context means these checks never reload the queue, snapshots or tiers.
    for keep, members in sorted(merge["members"].items()):
        if keep not in lessons:
            continue
        group_paths = scan["reached"]
        lost_files = {i: sorted(set(group_paths.get(i, [])) - set(group_paths.get(keep, []))) for i in members}
        lost_files = {i: paths for i, paths in lost_files.items() if paths}
        if lost_files:
            entry = dict(_lesson(lessons[keep]), members=members, lostFiles=lost_files,
                         label=v.MERGE_LOST_FILES + ", ".join(sorted({p for ps in lost_files.values() for p in ps})))
            lost = [p for ps in lost_files.values() for p in ps]
            wanted = list(dict.fromkeys(list(lessons[keep]["item"].get("approvalPaths") or []) + lost))
            note = v.MERGE_PATH_APPROVAL if lessons[keep]["tier"] != APPROVED else v.MERGE_PATH_LIMIT
            repair = (cmd(["learn"], [("--paths", keep), ("--by", "NAME")] + [("--path", p) for p in wanted])
                      if lessons[keep]["tier"] == APPROVED and len(wanted) <= 8 else None)
            merged_lost.append(_with_command(entry, repair, note or v.TIDY_UNSAFE_ARGUMENT))

    # model proposals (only when .memlet/dream/runs/tidy-proposals.json exists) ---------------------------------
    seen_nids: Set[str] = set()
    dup_nids: Set[str] = set()
    for raw_node in (graph or {}).get("nodes", []):
        if isinstance(raw_node, dict) and isinstance(raw_node.get("id"), str):
            nid = raw_node["id"]
            if nid in seen_nids:
                dup_nids.add(nid)
            seen_nids.add(nid)
    wording_sets = [{keep} | set(same) for _, keep, same in groups]
    model_same = _model_proposals_fn()(
        root,
        lessons=lessons,
        nodes=nodes,
        merge=merge,
        merge_ok=merge_ok,
        cmd=None, _repo_arg=repo_arg,
        duplicate_ids=dup_nids,
        wording_groups=wording_sets,
    )
    if model_same is not None:
        if model_same.get("blockersFailed") and v.TIDY_BLOCKERS_FAILED not in notices:
            notices.append(v.TIDY_BLOCKERS_FAILED)
            merge_ok = False
        for note in model_same.get("notices", []):
            if note not in notices:
                notices.append(note)

    said_again_out = _capped(said + again, omitted, "saidAgain")
    retire_out = _capped(retire, omitted, "retire")
    unattached_out = _capped(unattached, omitted, "unattached")
    merged_lost_out = _capped(merged_lost, omitted, "mergedLostFiles")
    omitted_out: Dict[str, int] = {}
    for s in v.TIDY_SECTIONS:
        omitted_out[s] = omitted.get(s, 0)
        if s == "duplicates" and model_same is not None:
            omitted_out[v.TIDY_MODEL_SAME_SECTION] = model_same["omitted"]

    result = {"ok": True, "exitCode": 0,
              "duplicates": duplicates,
              **({v.TIDY_MODEL_SAME_SECTION: model_same["entries"]} if model_same is not None else {}),
              "saidAgain": said_again_out,
              "retire": retire_out,
              "unattached": unattached_out,
              "promote": promote,
              "lapsedMerges": lapsed,
              "mergedLostFiles": merged_lost_out,
              "omitted": omitted_out,
              "complete": complete, "notices": notices, "mergeChecks": merge_ok,
              "scanned": scan["scanned"], "work": work, "skipped": scan["skipped"]}
    result["formatted"] = render_markdown(result)
    return result


def _promote_block(root: Path, lesson: Dict[str, Any], page_anchors: Dict[str, Tuple[str, Dict[str, Any]]],
                   page_unchecked: bool, instruction_files: List[str]) -> Dict[str, Any]:
    """The complete current cited lines (never the display-truncated form), markup neutralised with the display
    sanitiser, and the anchor promotion would give: the promoted page's sticky anchor, else
    `memlet_bridge._resolve_lesson_anchor`. Omitted whole when longer than MAX_BLOCK_CHARS. Nothing is written."""
    out: Dict[str, Any] = {"instructionFiles": list(instruction_files), "suggestion": v.TIDY_PROMOTE_SUGGESTION,
                           "block": None, "escaped": False}
    lesson_id = lesson["id"]
    if lesson_id in page_anchors:
        anchor, anchor_note = page_anchors[lesson_id][0], None
    elif page_unchecked:
        anchor, anchor_note = None, v.TIDY_NOT_CHECKED
    else:
        found = mb._resolve_lesson_anchor(root, lesson["node"])
        anchor, anchor_note = ("%s::%s" % found if found else None), None
    out["suggestedAnchor"] = anchor
    if anchor_note:
        out["anchorNote"] = anchor_note
    if lesson["current"] is None:
        out["blockNote"] = v.TIDY_BLOCK_UNAVAILABLE
        return out
    source = [_strip(raw) for raw in lesson["current"]]
    shown = [mb.safe_lesson_display(line) for line in source]
    block = ["- " + shown[0]] + ["  " + line for line in shown[1:]]
    if anchor:
        block[-1] += " (`" + anchor + "`)"
    text = "\n".join(block) + "\n"
    if len(text) > MAX_BLOCK_CHARS:
        out["blockNote"] = v.TIDY_BLOCK_TOO_LONG % MAX_BLOCK_CHARS
        return out
    out["block"] = text
    if shown != source:
        out["escaped"] = True
        out["escapedNote"] = v.TIDY_BLOCK_ESCAPED
    return out


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
    roles = entry.get("lineRoles")
    for i, quote in enumerate(entry.get("quoteLines", [])):
        cite = entry["cites"][i] if i < len(entry.get("cites", [])) else None
        marker = ""
        if roles and i < len(roles):
            marker = (v.REPLACED_MARKER if roles[i] == "replaced" else v.CURRENT_MARKER) + " "
        out.append("  > " + marker + quote + (" (" + _code("{file}:{start}-{end}".format(**cite)) + ")" if cite else ""))
    return out


def _commands(entry: Dict[str, Any]) -> List[str]:
    if entry.get("command") is None:
        return ["  " + v.TIDY_NO_COMMAND_LABEL + entry.get("commandNote", v.TIDY_UNSAFE_ARGUMENT)]
    out = ["  " + v.TIDY_COMMAND_LABEL + _code(entry["command"])]
    out += ["  " + v.TIDY_ALSO_LABEL + _code(c) for c in entry.get("commands", []) if c != entry["command"]]
    return out


def render_markdown(result: Dict[str, Any]) -> str:
    lines = [v.TIDY_HEAD, "", v.TIDY_NOTHING_CHANGED, "",
             "- " + v.TIDY_SCAN_LINE % (result["scanned"]["files"], result["scanned"]["bytes"])]
    lines += ["- " + n for n in result["notices"]]
    sections = list(v.TIDY_SECTIONS)
    if v.TIDY_MODEL_SAME_SECTION in result:
        sections.insert(1, v.TIDY_MODEL_SAME_SECTION)
    for section in sections:
        entries = result[section]
        heading = (v.TIDY_MODEL_SAME_HEADING if section == v.TIDY_MODEL_SAME_SECTION
                   else v.TIDY_SECTION_HEADINGS[section])
        lines += ["", "### " + heading, ""]
        if not entries:
            lines.append("- " + v.TIDY_NONE)
        shown_disputed = False
        for e in entries:
            if section == v.TIDY_MODEL_SAME_SECTION:
                if e.get("disputed") and not shown_disputed:
                    lines.append("- " + v.TIDY_MODEL_DISPUTED_GROUP)
                    shown_disputed = True
                lines.append("- [%s; %s] %s %s (%s); same: %s" % (
                    e["label"], v.TIDY_MODEL_PROPOSAL_ID_NOTE % _code(e["id"]),
                    v.TIDY_KEEP_LABEL, _code(e["keep"]), e["lessons"][0]["tier"],
                    ", ".join(_code(x["id"]) + " (" + x["tier"] + ")" for x in e["lessons"][1:])))
                if e.get("keepReason"):
                    lines.append("  " + v.TIDY_KEEP_REASON_LABEL + e["keepReason"])
                for lesson in e["lessons"]:
                    lines.append("  " + _code(lesson["id"]) + ":")
                    lines += ["  " + q for q in _quotes(lesson)]
                has_blockers = any(e.get("blockers", {}).values())
                if has_blockers:
                    lines.append("  " + v.TIDY_BLOCKERS_LABEL + "; ".join(
                        _code(k) + ": " + ", ".join(r + " — " + v.MERGE_REASONS.get(r, r) for r in c)
                        for k, c in sorted(e["blockers"].items()) if c))
                if any(e.get("lostFiles", {}).values()):
                    lines.append("  " + v.TIDY_LOST_FILES_LABEL + "; ".join(
                        _code(k) + ": " + ", ".join(_code(p) for p in ps)
                        for k, ps in sorted(e["lostFiles"].items()) if ps))
                if not (has_blockers and e.get("command") is None):
                    lines += _commands(e)
                if e.get("pathsCommand"):
                    lines.append("  " + v.MERGE_ATTACH_LABEL + _code(e["pathsCommand"]))
                if e.get("dismissCommand"):
                    lines.append("  " + v.TIDY_MODEL_DISMISS_LABEL + _code(e["dismissCommand"]))
                continue
            if section == "duplicates":
                lines.append("- [%s] %s %s (%s); same: %s" % (
                    e["label"], v.TIDY_KEEP_LABEL, _code(e["keep"]), e["lessons"][0]["tier"],
                    ", ".join(_code(x["id"]) + " (" + x["tier"] + ")" for x in e["lessons"][1:])))
                if e.get("keepReason"):
                    lines.append("  " + v.TIDY_KEEP_REASON_LABEL + e["keepReason"])
                for lesson in e["lessons"]:
                    lines.append("  " + _code(lesson["id"]) + ":")
                    lines += ["  " + q for q in _quotes(lesson)]
                has_blockers = any(e.get("blockers", {}).values())
                if has_blockers:
                    lines.append("  " + v.TIDY_BLOCKERS_LABEL + "; ".join(
                        _code(k) + ": " + ", ".join(r + " — " + v.MERGE_REASONS.get(r, r) for r in c) for k, c in sorted(e["blockers"].items()) if c))
                if any(e.get("lostFiles", {}).values()):
                    lines.append("  " + v.TIDY_LOST_FILES_LABEL + "; ".join(
                        _code(k) + ": " + ", ".join(_code(p) for p in ps)
                        for k, ps in sorted(e["lostFiles"].items()) if ps))
                if not (has_blockers and e.get("command") is None):
                    lines += _commands(e)
                if e.get("pathsCommand"):
                    lines.append("  " + v.MERGE_ATTACH_LABEL + _code(e["pathsCommand"]))
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
                for match in d.get("rules", []):
                    lines.append("  rule: " + _code(match["rule"]) + " says " + _code(match["line"]))
                if d.get("approvedAt"):
                    lines.append("  approved: " + d["approvedAt"] + "; last said: " + str(d.get("lastSaid")))
            if section == "promote":
                lines.append("  " + e["suggestion"])
                if e["block"] is not None:
                    fence = _fence(e["block"])
                    lines += ["  " + fence + "markdown"] + ["  " + b for b in e["block"].splitlines()] + ["  " + fence]
                    if e.get("escaped"):
                        lines.append("  " + e["escapedNote"])
                else:
                    lines.append("  " + e["blockNote"])
                if e["suggestedAnchor"]:
                    lines.append("  " + v.TIDY_ANCHOR_SUGGESTION + _code(e["suggestedAnchor"]))
                else:
                    lines.append("  " + (v.TIDY_ANCHOR_NOT_CHECKED if e.get("anchorNote") else v.TIDY_NO_ANCHOR))
                lines.append("  " + (v.TIDY_INSTRUCTION_FILES + ", ".join(_code(f) for f in e["instructionFiles"])
                                     if e["instructionFiles"] else v.TIDY_NO_INSTRUCTION_FILES))
                if e.get("command") is not None:
                    lines.append("  " + e["commandLabel"] + ": " + _code(e["command"]))
                else:
                    lines += _commands(e)
                continue
            lines += _commands(e)
        if result["omitted"].get(section):
            lines.append("- " + v.TIDY_OMITTED % result["omitted"][section])
    return "\n".join(lines) + "\n"
