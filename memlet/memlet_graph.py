"""A read-only projection, never a second authority for memory.

Rules use the wiki reader and classifier. Lessons use dream_items (including its
global supersession projection). Historical approval records are reconstructed
only from digest-bound decision events, then passed through that same reader.
No old source contents are read. No model, network or write occurs. Caches are call-local or content-addressed.
The wall clock is used only to reject future events and bound hook work.

The file query indexes references before classifying code or verifying lessons;
it does not construct the project graph. Bounds deliberately fail closed when a
complete supersession view cannot be obtained. Citation nodes are lesson nodes
with recordType='citation'; parent lessons retain the queue's stable ids.
"""
import contextlib
import copy
import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import unicodedata

from . import code_anchors as ca, memlet_anchors as ma, memlet_bridge as mb
from . import memlet_dream as md, memlet_wiki as mw
from .memlet_eval import _stamp
from . import vocabulary as v
from .vocabulary import (APPROVED, HINT, GRAPH_WHY, GRAPH_HEADINGS, GRAPH_TIME_NOTICE,
                         GRAPH_HISTORY_NOTICE, GRAPH_OMITTED, HINT_HELP, SUGGESTED_ANCHOR)

NODE_KINDS = ("rule", "lesson", "symbol", "file")
GROUPS = ("governs", "blocks", "resolves", "replaced", "stale", "hints")
MAX_RULES = 5000
MAX_LESSONS = 5000
MAX_EDGES = 20000
MAX_EVENTS = 100000
MAX_HISTORY_QUERIES = 256
MAX_RULE_CHARS = 16384
MAX_FORMATTED_CHARS = 8000
MAX_SUPERSESSION_SLICE = 512
MAX_NEIGHBOR_CANDIDATES = 256
MAX_NEIGHBOR_ANCHORS = 256
MAX_NEIGHBOR_EVENTS = 4096
MAX_NEIGHBOR_SECONDS = 2.5
_monotonic = time.monotonic  # separately injectable; never patches the stdlib clock


def _id(kind, value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return kind + "-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return _stamp(value).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        return None


def _at(value):
    if value is None:
        return None
    stamp = _time(value)
    if stamp is None:
        raise ValueError(v.GRAPH_INVALID_TIME)
    return stamp


def _before(a, b):
    return a is not None and (b is None or _stamp(a) <= _stamp(b))


def _earliest(values):
    values = [v for v in values if v is not None]
    return min(values, key=_stamp) if values else None


def _latest(values):
    values = list(values)
    return max(values, key=_stamp) if values and all(values) else None


class _Work:
    """Hook-only limits; incomplete groups are explicit even when nothing fits."""
    def __init__(self, bounded=False, clock=None, counters=None):
        self.bounded = bounded
        self.clock = clock or _monotonic
        self.deadline = self.clock() + MAX_NEIGHBOR_SECONDS if bounded else None
        self.counters = counters
        self.parse_start = ca._PYTHON_BINDING_PARSE_COUNT
        if counters is not None:
            counters.update(directoryListings={}, gitCalls=0, anchorsClassified=0,
                            lessonCandidatesValidated=0, rebindingFilesParsed=0,
                            snapshotReads=0, queueReads=0, tierPasses=0)
        self.incomplete = set()
        self.notices = []
        self.directories = {}
        self.known_ids = {}
        self.exhausted = False

    def count(self, key, amount=1):
        if self.counters is not None:
            self.counters[key] += amount

    def finish(self):
        if self.counters is not None:
            self.counters["rebindingFilesParsed"] = ca._PYTHON_BINDING_PARSE_COUNT - self.parse_start

    def skip(self, notice, groups=GROUPS):
        if notice not in self.notices:
            self.notices.append(notice)
        self.incomplete.update(groups)

    def available(self):
        if self.exhausted:
            return False
        if self.bounded and self.clock() >= self.deadline:
            self.exhausted = True
            self.skip(v.GRAPH_WORK_NOTICE)
            return False
        return True


def _path_adjacent(c):
    return unicodedata.category(c) in ("Cc", "Cf", "Mn", "Me") or mb._hidden_character(c)


def _clean_path(root, value, *, exists=True, directories=None, indexed=False, counters=None):
    if (not isinstance(value, str) or not value or len(value) > 1000 or "\\" in value
            or any(_path_adjacent(c) for c in value)):
        return None
    p = Path(value)
    if ".." in p.parts:
        return None
    if p.is_absolute():
        # Resolve ancestor aliases (e.g. a symlinked home), never the leaf.
        # The containment walk below still refuses a symlink leaf or a relative
        # path traversing a symlink inside the repository.
        try:
            p = (p.parent.resolve() / p.name).relative_to(root.resolve())
        except (ValueError, OSError, RuntimeError):
            return None
    rel = p.as_posix()
    if rel in ("", ".") or mw._unsafe_rel(rel) or mw.contained_path_problem(root, rel):
        return None
    if exists and not (root / rel).is_file():
        return None
    if not indexed:
        directories = {} if directories is None else directories
        cursor = root
        for part in p.parts:
            try:
                if cursor not in directories:
                    directories[cursor] = set()
                    is_directory = cursor.is_dir()
                    if is_directory and counters is not None:
                        key = cursor.relative_to(root).as_posix()
                        counts = counters["directoryListings"]
                        counts[key] = counts.get(key, 0) + 1
                    directories[cursor] = {x.name for x in cursor.iterdir()} if is_directory else set()
                names = directories[cursor]
                if part not in names:
                    if any(n.casefold() == part.casefold() for n in names):
                        return None
                    if exists:
                        return None
                    break  # deleted files may still have anchored memory
            except OSError:
                return None
            cursor /= part
    return rel


def _attached(root, node, item, context):
    """Approved paths are the reader's result; hint paths are display-only."""
    if item["tier"] == APPROVED:
        human = set(mb.lesson_paths(node, root, context=context))
        return {p: GRAPH_WHY[2] if p in human else GRAPH_WHY[3] for p in context["paths"].get(item["id"], [])}
    # The shared reader retains legacy unfiltered hint bytes, and exposes its
    # literal selection through the call-local context for display here only.
    return {p: GRAPH_WHY[3] for p in context.get("hintPaths", {}).get(item["id"], [])}


class _History:
    def __init__(self, root, work=None, graph=None):
        work = work or _Work()
        event_bound = min(MAX_EVENTS, MAX_NEIGHBOR_EVENTS) if work.bounded else MAX_EVENTS
        self.decisions, self.sidecars, self.conflicts = {}, {}, set()
        self.merges = {}
        self.notices = []
        self.originals = {}
        self.now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        if mw.first_managed_problem(root, mw._LEDGER_FILES) or not (root / mw.LEDGER_PATH).is_file():
            return
        try:
            with contextlib.closing(mw.connect_managed_sqlite(root, mw.LEDGER_PATH, readonly=True)) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "dream_decision_events" in tables:
                    rows = mb.decision_events(root)
                    if getattr(rows, "truncated", False):
                        # The public reader returns the newest 4096, not a page
                        # of the oldest history. Recover complete per-item sets;
                        # a truncated item is explicitly unknown, never a prefix.
                        ids = {n.get("id") for n in (graph or {}).get("nodes", [])}
                        ids.update(e.get("edgeId") for n in (graph or {}).get("nodes", []) for e in n.get("edges", []))
                        ids.update(e.get("edgeId") for e in (graph or {}).get("edges", []))
                        recovered = []
                        if not ids:
                            self.notice(v.GRAPH_EVENTS_NOTICE)
                        for number, ident in enumerate(sorted(i for i in ids if isinstance(i, str))):
                            if number >= min(event_bound, MAX_HISTORY_QUERIES):
                                self.notice(v.GRAPH_EVENTS_NOTICE)
                                break
                            if not work.available() or len(recovered) >= event_bound:
                                self.notice(v.GRAPH_EVENTS_NOTICE)
                                break
                            item_rows = mb.decision_events(root, item_id=ident)
                            if getattr(item_rows, "truncated", False):
                                self.notice(v.GRAPH_EVENTS_NOTICE)
                                continue
                            if getattr(item_rows, "skipped", 0):
                                self.notice(v.GRAPH_CORRUPT_HISTORY_NOTICE)
                                continue
                            if len(recovered) + len(item_rows) > event_bound:
                                self.notice(v.GRAPH_EVENTS_NOTICE)
                                break
                            recovered.extend(item_rows)
                        rows = sorted(recovered, key=lambda r: r["seq"])
                    elif getattr(rows, "skipped", 0):
                        self.notice(v.GRAPH_CORRUPT_HISTORY_NOTICE)
                        rows = []
                    if len(rows) > event_bound:
                        self.notice(v.GRAPH_EVENTS_NOTICE)
                        rows = []
                    for row in rows:
                        if not work.available():
                            self.decisions, self.conflicts = {}, set()
                            self.notice(v.GRAPH_EVENTS_NOTICE)
                            break
                        # An observation of an untouched relation is not a new decision.
                        if row["decision"] in ("left-pending", "left-rejected", "left-approved"):
                            continue
                        seq, ident, kind = row["seq"], row["item_id"], row["kind"]
                        decision = {"reconsidered": APPROVED, "paths-changed": APPROVED,
                                    "citations-refreshed": APPROVED, "revoked": "rejected"}.get(row["decision"], row["decision"])
                        at = _time(row["at"])
                        if at is None:
                            continue
                        if kind == "node" and decision in ("merged", "unmerged"):
                            self.merges.setdefault(ident, []).append(dict(at=at, seq=seq, decision=decision))
                            continue  # merge rows never change an approval interval or digest
                        key = (kind, ident)
                        events = self.decisions.setdefault(key, [])
                        if events and _stamp(at) < _stamp(events[-1]["at"]):
                            self.conflicts.add(key)
                            self.notice(v.GRAPH_ORDER_NOTICE)
                            self.notice(v.GRAPH_ORDER_ITEM_NOTICE % mb.safe_lesson_display(str(ident), limit=160))
                        events.append(dict(seq=seq, at=at, decision=decision, by=row["by"], digest=row["digest"], paths=row["paths"]))
                    for events in self.decisions.values():
                        events.sort(key=lambda r: (_stamp(r["at"]), r["seq"]))
                if ma.LEDGER_TABLE in tables:
                    rows = db.execute("SELECT entry_id, entry_sha256, event_type, recorded_at FROM " +
                                      ma.LEDGER_TABLE + " ORDER BY id LIMIT ?", (event_bound + 1,)).fetchall()
                    if len(rows) > event_bound:
                        self.notice(v.GRAPH_ANCHOR_EVENTS_NOTICE)
                        rows = []
                    for ident, digest, event, at in rows:
                        self.sidecars.setdefault(ident, []).append(dict(at=_time(at), digest=digest, decision=event))
        except sqlite3.Error:
            self.decisions, self.sidecars = {}, {}
            self.notice(v.GRAPH_CORRUPT_HISTORY_NOTICE)

    def notice(self, notice):
        if notice not in self.notices:
            self.notices.append(notice)

    def _matches(self, item, kind, record):
        # Only content still available to recompute is admissible. A digest in
        # a log row is not proof that old, deleted content ever existed.
        candidate = item
        if kind == "node" and record.get("paths") is not None:
            try:
                paths = record["paths"]
                paths = json.loads(paths) if isinstance(paths, str) else paths
                if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
                    return False
                candidate = dict(item, approvedPaths=paths)
            except (ValueError, TypeError):
                return False
        digest = md.node_approval_digest(candidate) if kind == "node" else md.edge_approval_digest(candidate)
        if record.get("digest") == digest:
            return True
        # E3 preserves legacy no-path digest bytes. Empty paths in the log
        # cannot distinguish absent from explicitly empty; verify both forms.
        if kind == "node" and candidate.get("approvedPaths") == []:
            legacy = dict(candidate)
            legacy.pop("approvedPaths", None)
            return record.get("digest") == md.node_approval_digest(legacy)
        return False

    def _original(self, item, kind):
        return self.originals.get((kind, item.get("id" if kind == "node" else "edgeId")), item)

    def _events(self, item, kind, as_of):
        item = self._original(item, kind)
        ident = item.get("id" if kind == "node" else "edgeId")
        cutoff = as_of or self.now
        result = []
        for r in self.decisions.get((kind, ident), []):
            if as_of is None and not _before(r["at"], self.now):
                self.notice(v.GRAPH_FUTURE_NOTICE)
                continue
            if _before(r["at"], cutoff) and (r["decision"] == "rejected" or self._matches(item, kind, r)):
                result.append(r)
        current = item.get("approval", {})
        if isinstance(current, dict) and _before(_time(current.get("at")), cutoff):
            # A current rejection/non-approval is authoritative even if content
            # changed. An approval still needs the shared digest/state check.
            at = _time(current["at"])
            rec = dict(current, at=at, seq=float("inf"))
            rec["decision"] = md.approval_state(item, kind)
            result.append(rec)
        result.sort(key=lambda r: (_stamp(r["at"]), r.get("seq", 0)))
        return result

    def decision(self, item, kind, as_of):
        item = self._original(item, kind)
        events = self._events(item, kind, as_of)
        current = item.get("approval", {})
        cutoff = as_of or self.now
        if isinstance(current, dict) and _before(_time(current.get("at")), cutoff):
            # F2: after the current record, an unauthenticated row cannot
            # contradict the current queue. The current record wins all ties.
            return dict(current, at=_time(current["at"]), decision=md.approval_state(item, kind))
        key = (kind, item.get("id" if kind == "node" else "edgeId"))
        if key in self.conflicts:
            return {}
        return events[-1] if events else {}

    def interval(self, item, kind, as_of):
        record = self.decision(item, kind, as_of)
        start = _time(record.get("at")) if record.get("decision") == APPROVED else None
        return start, None

    def intervals(self, item, kind, as_of):
        item = self._original(item, kind)
        events = self._events(item, kind, as_of)
        current_at = _time(item.get("approval", {}).get("at"))
        # No event beyond the current record can contradict the present view.
        if current_at:
            events = [r for r in events if _stamp(r["at"]) <= _stamp(current_at)]
        key = (kind, item.get("id" if kind == "node" else "edgeId"))
        if key in self.conflicts:
            start, _ = self.interval(item, kind, as_of)
            return [{"validFrom": start, "validTo": None}] if start else []
        result = []
        for record in events:
            at = record["at"]
            if result and result[-1]["validTo"] is None:
                if record["decision"] == APPROVED and _stamp(result[-1]["validFrom"]) == _stamp(at):
                    continue
                result[-1]["validTo"] = at
            if record["decision"] == APPROVED and self._matches(item, kind, record):
                result.append({"validFrom": at, "validTo": None})
        return result

    def replay(self, graph, as_of):
        if as_of is None:
            return graph  # events NEVER decide a present tier
        for n in graph.get("nodes", []):
            self.originals[("node", n.get("id"))] = n
            for e in n.get("edges", []):
                self.originals[("edge", e.get("edgeId"))] = e
        for e in graph.get("edges", []):
            self.originals[("edge", e.get("edgeId"))] = e
        view = copy.deepcopy(graph)
        def restore(item, kind):
            record = self.decision(item, kind, as_of)
            # Eligibility was compared at full precision. The legacy shared
            # reader accepts seconds only; this adapter encoding is not a time
            # comparison and is never exposed as the event's actual timestamp.
            sealed_at = _time(record.get("at"))
            if sealed_at:
                sealed_at = _stamp(sealed_at).strftime("%Y-%m-%dT%H:%M:%SZ")
            item["approval"] = {k: record.get(k) for k in ("decision", "by", "digest")}
            item["approval"].update(at=sealed_at, schema=md.APPROVAL_RECORD_SCHEMA)
            if record.get("paths") is not None:
                paths = record["paths"]
                paths = json.loads(paths) if isinstance(paths, str) else paths
                item["approval"]["paths"] = paths
                if kind == "node":
                    item["approvedPaths"] = paths
                    if paths == [] and record.get("digest") != md.node_approval_digest(item):
                        item.pop("approvedPaths", None)
            item["verdict" if kind == "node" else "edgeVerdict"] = (
                ("ALREADY_SUPERSEDED" if md.superseded_lesson(item) else "SUPPORTED") if kind == "node"
                else "VALID") if record.get("decision") == APPROVED else md.PENDING
            if kind == "node":
                item["proposers"], item["verification"] = [], []
        hidden = set()
        for ident, rows in self.merges.items():
            past = sorted((r for r in rows if _before(r["at"], as_of)), key=lambda r: (_stamp(r["at"]), r["seq"]))
            if past and past[-1]["decision"] == "merged":
                hidden.add(ident)
        view["nodes"] = [n for n in view.get("nodes", []) if n.get("id") not in hidden]
        view["edges"] = [e for e in view.get("edges", []) if e.get("fromId") not in hidden and e.get("toId") not in hidden]
        for node in view.get("nodes", []):
            node.pop("merge", None)  # present records cannot hide a lesson before its historical merged row
            restore(node, "node")
            for edge in node.get("edges", []):
                restore(edge, "edge")
        for edge in view.get("edges", []):
            restore(edge, "edge")
        return view

    def anchor_time(self, rule, anchor, config, pin, as_of=None):
        def matches(other):
            return bool(pin and other and mw._same_hash(pin.get("ast"), other.get("ast")))
        sid = rule.get("sidecarIds", {}).get(anchor)
        if sid:
            if not hasattr(self, "_sidecar_entries"):
                self._sidecar_entries = {e.get("id"): e for e in config.get("sidecarAnchors", [])
                                         if isinstance(e, dict)}
            entry = self._sidecar_entries.get(sid)
            if not entry or not matches(entry.get("pin")):
                return None
            events = []
            for event in self.sidecars.get(sid, []):
                if as_of is None and event["at"] and not _before(event["at"], self.now):
                    self.notice(v.GRAPH_FUTURE_NOTICE)
                elif _before(event["at"], as_of or self.now):
                    if event["decision"] == "drop" or event["digest"] == ma.entry_digest(entry):
                        events.append(event)
            events.sort(key=lambda e: _stamp(e["at"]))
            return events[-1]["at"] if events and events[-1]["decision"] == "accept" else None
        stored = config.get("pinnedAnchors", {}).get(anchor, {})
        at = _time(stored.get("pinnedAt")) if matches({"ast": stored.get("astHash")}) else None
        if as_of is None and at and not _before(at, self.now):
            self.notice(v.GRAPH_FUTURE_NOTICE)
            return None
        return at


def _sources(root, lesson_items):
    rules, pins, texts, truncated = [], {}, {}, False
    source_files = mw.import_source_files(root, "auto")
    sources = mw._source_texts(root, source_files)
    if len(sources) < len(source_files):
        truncated = True
    for rel, text in mw.trusted_wiki_pages(root):
        if rel == mb.PROMOTED_PAGE or mw._is_generated_page(rel, text):
            continue
        for anchor in mw._extract_anchors(text, rel):
            if anchor["pin"]:
                pins.setdefault(anchor["path"] + "::" + anchor["symbol"], anchor["pin"])
        if rel != mb.PROMOTED_PAGE and not mw._is_generated_page(rel, text):
            sources.append((rel, text))
    for rel, text in sources:
        texts[rel] = text.splitlines()
        body = mw._STATUS_BLOCK_RE.sub(mw._blank_lines, mw._PAGE_STATUS_RE.sub(mw._blank_lines, text))
        parsed = mw._extract_rules(body, rel)
        truncated |= len(parsed) >= mw._MAX_RULES_PER_FILE
        room = MAX_RULES - len(rules)
        rules.extend(parsed[:room])
        if len(parsed) > room:
            truncated = True
        if len(rules) >= MAX_RULES:
            break
    mw._attach_sidecar(root, rules)
    config_pins = mw._config_pins(root)
    config_pins.update(pins)
    return rules, config_pins, texts, truncated


def _rule_text(rule, texts):
    lines = texts.get(rule["source"], [])
    if not 0 < rule["line"] <= len(lines):
        return None
    line = lines[rule["line"] - 1]
    if len(line) > MAX_RULE_CHARS:
        return None
    bullet = mw._BULLET_RE.match(line)
    text = mw.PIN_RE.sub("", bullet.group("text") if bullet else line.strip()).strip()
    if (hashlib.sha256(text.encode()).hexdigest() != rule["ruleSha256"]
            or any(mb._hidden_character(c) for c in text) or md.has_unmasked_secret_or_canary(text)):
        return None
    return text


def _rule_id(rule):
    # Full hash, never the display prefix or global position. Adding unrelated
    # rules, or moving a source line, cannot rename this node.
    return _id("rule", [rule["source"], rule["ruleSha256"]])


def _rule_files(root, rule):
    return {mw._resolve_ref_path(root, rule["source"], path) for path, _ in rule["refs"]}


def _fresh_classification(root, rules, pins, work):
    paths = {str(root / path) for r in rules for path in _rule_files(root, r)}
    # 9.1 extractors contain stat-keyed caches. Do not treat those as proof:
    # discard entries for the selected files before calling the single classifier.
    for cache in (ca._FILE_HASH_AST_CACHE, ca._FILE_SIGNATURE_CACHE):
        for key in list(cache):
            if key[0] in paths:
                cache.pop(key, None)
    if not work.bounded:
        classified = mw._verify_rules(root, rules, pins, pin_source="pinned")
        work.count("anchorsClassified", len(classified))
        return classified
    completed, anchors, consumed = [], {}, 0
    for r in rules:
        refs = {(mw._resolve_ref_path(root, r["source"], p), sym) for p, sym in r["refs"]}
        if consumed + len(refs) > MAX_NEIGHBOR_ANCHORS or not work.available():
            work.skip(v.GRAPH_WORK_NOTICE)
            break
        # Retain the shared classifier and its worst-anchor rule status. Work
        # is checked between complete rules: no partly classified rule escapes.
        classified = mw._verify_rules(root, [r], pins, pin_source="pinned")
        work.count("anchorsClassified", len(classified))
        for a in classified:
            anchors[a["anchor"]] = a
        consumed += len(refs)
        completed.append(r)
    rules[:] = completed
    return list(anchors.values())


def _queue(root, work=None):
    if work:
        work.count("queueReads")
    if mb._symlinked(root, (mb.QUEUE_REL, mb.SNAPSHOT_REL)):
        raise ValueError(v.GRAPH_UNSAFE_DREAM)
    try:
        graph, _ = mb._load_queue(root)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        raise ValueError(v.GRAPH_QUEUE_UNREADABLE) from exc
    graph = graph or {"nodes": [], "edges": []}
    if len(graph.get("nodes", [])) > MAX_LESSONS or len(graph.get("edges", [])) > MAX_EDGES:
        raise ValueError(v.GRAPH_QUEUE_BOUND)
    return graph


def _supersession_ids(graph):
    result = set()
    for n in graph.get("nodes", []):
        for e in n.get("edges", []):
            if e.get("edgeType") == "SUPERSEDES":
                result.update((n.get("id"), e.get("fromId"), e.get("toId")))
    for e in graph.get("edges", []):
        if e.get("edgeType") == "SUPERSEDES":
            result.update((e.get("fromId"), e.get("toId")))
    # Synthetic relation endpoints do not cost a queue node.
    return result & {n.get("id") for n in graph.get("nodes", [])}


def _slice(graph, path, ident, work=None):
    """Slice by references, retaining the exact full-queue veto/supersession set."""
    work = work or _Work()
    selected, supersessions = set(), _supersession_ids(graph)
    candidates = []
    rejected = mb.tier_config(graph, {}).get("_dreamRejectedQuoteHashes", set())
    quote_hashes = {}
    for node in graph.get("nodes", []):
        if not work.available():
            # Incomplete veto/context indexing cannot authorize a prefix.
            return dict(graph, nodes=[], edges=[])
        nid = node.get("id")
        paths = {path for part in md.lesson_lines(node) for line in part["quote"].split("\n")
                 for path in mb._lesson_path_candidates(line)}
        # An approved edge may cite a different line from its owning node.
        # This only selects candidates; the shared reader still verifies pins,
        # current roles, the node tier and the exact path before attaching it.
        for edge in node.get("edges", []):
            if md.approval_state(edge, "edge") == APPROVED and edge.get("edgeVerdict") == "VALID":
                for pin in md._item_pins(edge, "edge") + [edge.get("citedTargetPin")]:
                    if isinstance(pin, dict):
                        for line in str(pin.get("quoteText", "")).split("\n"):
                            paths.update(mb._lesson_path_candidates(line))
        paths.update(p for p in node.get("approvedPaths", []) if isinstance(p, str))
        if path in paths or nid == ident:
            candidates.append(nid)
        if rejected:
            quote_hashes[nid] = md.lesson_quote_digest(node)
    if work.bounded and len(candidates) > MAX_NEIGHBOR_CANDIDATES:
        candidates = candidates[:MAX_NEIGHBOR_CANDIDATES]
        work.skip(v.GRAPH_WORK_NOTICE)
    selected.update(candidates)
    selected.update(supersessions)
    if ident:
        selected.add(ident)
    # Keep each selected merge component: both validity dependencies and mergedFrom
    # must agree with a full read even when the kept lesson reaches another file.
    adjacent = {}
    for node in graph.get("nodes", []):
        rec = node.get("merge")
        if isinstance(rec, dict) and isinstance(rec.get("into"), str):
            a, b = node.get("id"), rec["into"]
            adjacent.setdefault(a, set()).add(b)
            adjacent.setdefault(b, set()).add(a)
    pending = list(selected)
    while pending:
        for other in adjacent.get(pending.pop(), ()):
            if other not in selected:
                selected.add(other)
                pending.append(other)
    # Use tier_config's predicate, including verdict-only edge/node rejections.
    # Keep the matching twins themselves because dream_items builds this set.
    relevant_rejected = {quote_hashes[i] for i in selected if quote_hashes.get(i) in rejected}
    selected.update(i for i, digest in quote_hashes.items() if digest in relevant_rejected)
    return dict(graph, nodes=[n for n in graph.get("nodes", []) if n.get("id") in selected],
                edges=[e for e in graph.get("edges", [])
                       if e.get("fromId") in selected or e.get("toId") in selected])


def _project(root, as_of, *, path=None, symbol=None, ident=None, queue=None, work=None):
    work = work or _Work()
    with mb._lesson_path_projection(root, work) as context:
        return _project_context(root, as_of, path=path, symbol=symbol, ident=ident,
                                queue=queue, work=work, path_context=context)


def _project_context(root, as_of, *, path, symbol, ident, queue, work, path_context):
    queue = _queue(root, work) if queue is None else queue
    history = _History(root, work, graph=queue)
    config, error = mw.read_config(root)
    config = config or {}
    graph = history.replay(queue, as_of)
    work.known_ids.update({n["id"]: "lesson" for n in graph.get("nodes", [])})
    replacement_unknown = len(_supersession_ids(graph)) > MAX_SUPERSESSION_SLICE
    if replacement_unknown:
        # Every lesson/rule-currentness decision depends on this global set.
        # Keep code classification available, but never label it governing.
        work.skip(v.GRAPH_SUPERSESSION_NOTICE)
        graph = dict(graph, nodes=[], edges=[])
    if path is not None or ident is not None:
        graph = _slice(graph, path, ident, work)
    if not work.available():
        graph = dict(graph, nodes=[], edges=[])
    work.count("lessonCandidatesValidated", len(graph.get("nodes", [])))
    read = (mb.dream_items(root, tiers=(APPROVED,) if as_of else (APPROVED, HINT), _graph=graph)
            if work.available() else dict(ok=True, items=[], notices=[]))
    if not read["ok"]:
        raise ValueError(v.GRAPH_EVIDENCE_ERROR)
    notices = [GRAPH_TIME_NOTICE] + ([GRAPH_HISTORY_NOTICE] if as_of else [])
    notices.extend(read.get("notices", []))
    if error:
        notices.append(v.GRAPH_CONFIG_NOTICE)
    raw = {n["id"]: n for n in graph.get("nodes", [])}
    rules, pins, texts, truncated = _sources(root, read["items"])
    work.known_ids.update({_rule_id(r): "rule" for r in rules})
    safe_rules = [r for r in rules if len(r["refs"]) <= 128 and _rule_text(r, texts) is not None]
    if len(safe_rules) < len(rules):
        work.skip(v.GRAPH_RULE_EXCLUSION_NOTICE, ("governs", "stale", "replaced"))
    if truncated:
        work.skip(v.GRAPH_RULE_BOUND_NOTICE, ("governs", "stale", "replaced"))
    truncated |= len(safe_rules) < len(rules)
    rules = safe_rules
    if path is not None:
        # A promoted rule is already carried by its verified lesson in this
        # slice; avoid re-entering the full Dream reader via enrich_promoted_rules.
        rules = [r for r in rules if r["source"] != mb.PROMOTED_PAGE and path in _rule_files(root, r)
                 and (not symbol or any(sym == symbol and mw._resolve_ref_path(root, r["source"], p) == path
                                        for p, sym in r["refs"]))]
    elif ident is not None and ident.startswith("rule-"):
        rules = [r for r in rules if _rule_id(r) == ident]
    elif ident is not None:
        # Matching replaced instruction rules are needed for a lesson-id query.
        old = md.replaced_line_index(read["items"])[1]
        rules = [r for r in rules if md.normalised_quote(_rule_text(r, texts) or "") in old]
    anchors = {a["anchor"]: a for a in _fresh_classification(root, rules, pins, work)}
    nodes, edges, attachments = {}, [], {}
    replaced_projection = md.replaced_line_index(read["items"])[1]
    lesson_times = {i["id"]: history.interval(raw[i["id"]], "node", as_of)[0] for i in read["items"]}
    safe_rule_paths = {}
    def add(node):
        nodes[node["id"]] = node
        return node["id"]
    def file_node(path, since):
        fid = _id("file", path)
        if fid not in nodes:
            add(dict(id=fid, kind="file", path=path, validFrom=since, validTo=None))
        else:
            nodes[fid]["validFrom"] = _earliest([nodes[fid]["validFrom"], since])
        return fid
    for item in read["items"]:
        if not work.available():
            break
        node = raw[item["id"]]
        start, end = history.interval(node, "node", as_of)
        if item["tier"] == HINT:
            # Exact hint first-observed time cannot be reconstructed from policy.
            start = None
        if as_of and not _before(start, as_of):
            continue
        attached = _attached(root, node, item, path_context)
        attachments[item["id"]] = attached
        item = dict(item, kind="lesson", recordType="item", validFrom=start, validTo=end,
                    current=not item.get("quarantined", False),
                    validity=history.intervals(node, "node", as_of),
                    source=item["cites"][0]["file"], paths=list(attached))
        if item.get("quarantined"):
            item["validTo"] = _earliest(lesson_times.get(i) for q in item.get("quoteLines", [])
                                        for i in replaced_projection.get(md.normalised_quote(q), ()))
        add(item)
        for p in attached:
            file_node(p, start)
        if item["tier"] != APPROVED:
            continue
        for edge in node.get("edges", []):
            if md.approval_state(edge, "edge") != APPROVED:
                continue
            edge_start, edge_end = history.interval(edge, "edge", as_of)
            since = _latest([start, edge_start])
            if as_of and not _before(since, as_of):
                continue
            relation = edge.get("edgeType")
            if relation in ("GOVERNS", "BLOCKS", "RESOLVES"):
                replaced_at = _earliest(lesson_times.get(i) for q in item.get("quoteLines", [])
                                        for i in replaced_projection.get(md.normalised_quote(q), ()))
                if item.get("quarantined"):
                    item["validTo"] = replaced_at
                for p, why in attached.items():
                    edges.append(dict(type=relation, src=item["id"], dst=file_node(p, start), status=APPROVED,
                                      basis=why, validFrom=since, validTo=replaced_at or edge_end,
                                      validity=history.intervals(edge, "edge", as_of)))
            elif relation == "SUPERSEDES":
                endpoints = []
                for pin, role in ((edge.get("srcPin"), "current"), (edge.get("targetPin"), "replaced")):
                    if not isinstance(pin, dict):
                        break
                    line_fields = mb._lesson_fields(dict(primaryPin=pin))
                    cites = line_fields.get("cites", [])
                    if not cites:
                        break
                    lid = _id("line", [md.citation_key(c) for c in cites])
                    # Global line roles from the sole supersession projection.
                    is_old = role == "replaced" or any(md.normalised_quote(q) in replaced_projection
                                                      for q in line_fields["quoteLines"])
                    ended = _earliest(lesson_times.get(i) for q in line_fields["quoteLines"]
                                      for i in replaced_projection.get(md.normalised_quote(q), ())) if is_old else None
                    existing = nodes.get(lid, {})
                    parents = sorted(set(existing.get("lessonIds", [])) | {item["id"]})
                    add(dict(id=lid, kind="lesson", recordType="citation", lessonId=parents[0], lessonIds=parents,
                             tier=APPROVED, status="replaced" if is_old else "current",
                             text="\n".join(line_fields["quoteLines"]), source=cites[0]["file"],
                             validFrom=_earliest([start, existing.get("validFrom")]), validTo=ended, **line_fields))
                    endpoints.append(lid)
                if len(endpoints) == 2:
                    edges.append(dict(type=relation, src=endpoints[0], dst=endpoints[1], lessonId=item["id"],
                                      status=APPROVED, basis="approved-citations", validFrom=since, validTo=edge_end,
                                      validity=history.intervals(edge, "edge", as_of)))
    replaced_text = md.replaced_line_index([n for n in nodes.values() if n.get("recordType") == "item"])[1]
    line_ids = {}
    for n in nodes.values():
        if n.get("recordType") == "citation" and n.get("status") == "replaced":
            for q in n["quoteLines"]:
                line_ids.setdefault(md.normalised_quote(q), set()).add(n["id"])
    for rule in rules:
        text = _rule_text(rule, texts)
        if text is None:
            truncated = True
            continue
        keys = rule.get("anchors", [])
        for k in keys:
            if k in anchors and anchors[k]["path"] not in safe_rule_paths:
                p = anchors[k]["path"]
                safe_rule_paths[p] = _clean_path(root, p, exists=False, directories=work.directories, counters=work.counters)
        allowed = [k for k in keys if k in anchors and safe_rule_paths.get(anchors[k]["path"])]
        starts = {k: history.anchor_time(rule, k, config, pins.get(k), as_of) for k in allowed}
        if as_of:
            # A stale/compatible observation made today has no historical
            # timestamp. Do not project that present knowledge into the past.
            allowed = [k for k in allowed if _before(starts[k], as_of) and anchors[k]["status"] == "active"]
            if not allowed:
                continue
        start = _earliest(starts[k] for k in allowed)
        rid = _rule_id(rule)
        replacing = sorted(replaced_text.get(md.normalised_quote(text), set()))
        ended = _earliest(nodes[i]["validFrom"] for i in replacing if i in nodes)
        cite = dict(file=rule["source"], start=rule["line"], end=rule["line"],
                    sha256=rule["ruleSha256"], verified=True)
        add(dict(id=rid, kind="rule", status=rule["status"], text=text, quoteLines=[text], cites=[cite],
                 source=rule["source"] + ":" + str(rule["line"]), anchors=allowed,
                 suggested=bool(rule.get("suggestedAnchors")), suggestedAnchors=rule.get("suggestedAnchors", []),
                 sidecarIds=rule.get("sidecarIds", {}), replacedBy=replacing,
                 current=None if replacement_unknown else not replacing, replacementUnknown=replacement_unknown,
                 validFrom=start, validTo=ended))
        for key in allowed:
            anchor = anchors[key]
            fid = file_node(anchor["path"], starts[key])
            if anchor.get("symbol") and not replacement_unknown:
                sid = _id("symbol", key)
                add(dict(id=sid, kind="symbol", anchor=key, symbol=anchor["symbol"], file=fid,
                         status=anchor["status"], reason=anchor["reason"], validFrom=starts[key], validTo=None))
                edges.append(dict(type="GOVERNS", src=rid, dst=sid, status=anchor["status"],
                                  basis="accepted-suggestion" if key in rule.get("suggestedAnchors", []) else "written-anchor",
                                  validFrom=starts[key], validTo=ended))
        for lid in sorted(line_ids.get(md.normalised_quote(text), ())):
            edges.append(dict(type="SUPERSEDES", src=lid, dst=rid, status=APPROVED,
                              basis=GRAPH_WHY[4], validFrom=ended, validTo=None))
    if path is None and ident is None:
        # Pins can survive removal of their instruction. Keep those anchored
        # symbols in a full projection, without inventing a governing rule.
        orphan_keys = sorted(k for k in pins if "::" in k and _id("symbol", k) not in nodes)
        truncated |= len(orphan_keys) > MAX_RULES
        for key in orphan_keys[:MAX_RULES]:
            rel, name = key.rsplit("::", 1)
            if rel not in safe_rule_paths:
                safe_rule_paths[rel] = _clean_path(root, rel, exists=False, directories=work.directories, counters=work.counters)
            if not safe_rule_paths[rel]:
                continue
            since = history.anchor_time({}, key, config, pins[key], as_of)
            if as_of and not _before(since, as_of):
                continue
            for cache in (ca._FILE_HASH_AST_CACHE, ca._FILE_SIGNATURE_CACHE):
                for cached in list(cache):
                    if cached[0] == str(root / rel):
                        cache.pop(cached, None)
            anchor = mw.classify_anchor(root, dict(path=rel, symbol=name, pin=pins[key]))
            work.count("anchorsClassified")
            if as_of and anchor["status"] != "active":
                continue
            add(dict(id=_id("symbol", key), kind="symbol", anchor=key, symbol=name,
                     file=file_node(rel, since), status=anchor["status"], reason=anchor["reason"],
                     validFrom=since, validTo=None))
    # Deduplicate identical edges without position-dependent ids.
    edges = list({json.dumps(e, sort_keys=True): e for e in edges}.values())
    notices.extend(history.notices)
    notices.extend(work.notices)
    return nodes, sorted(edges, key=lambda e: (e["type"], e["src"], e["dst"])), attachments, list(dict.fromkeys(notices)), bool(truncated or work.notices or history.notices)


def _counts(nodes, edges):
    return {"nodes": {kind: sum(n["kind"] == kind for n in nodes) for kind in NODE_KINDS},
            "edges": {kind: sum(e["type"] == kind for e in edges) for kind in md.EDGE_TYPES},
            "lessonItems": sum(n.get("recordType") == "item" for n in nodes),
            "unattachedLessons": sum(n.get("recordType") == "item" and not n.get("paths") for n in nodes),
            "unanchoredRules": sum(n["kind"] == "rule" and not n.get("anchors") for n in nodes)}


def _refusal(exc):
    message = str(exc)
    if message in v.GRAPH_REFUSALS:
        return message
    for kind, reason in ((OSError, v.GRAPH_FILE_UNREADABLE), (KeyError, v.GRAPH_MISSING_FIELD),
                         (TypeError, v.GRAPH_BAD_TYPE), (AttributeError, v.GRAPH_BAD_TYPE),
                         (RecursionError, v.GRAPH_NESTING_BOUND), (ValueError, v.GRAPH_BAD_VALUE)):
        if isinstance(exc, kind):
            return reason
    return v.GRAPH_UNAVAILABLE


def build_graph(repo_path=".", *, as_of=None, _counters=None):
    work = _Work(counters=_counters)
    try:
        root, at = mw._resolve_root(repo_path), _at(as_of)
        nodes, edges, _, notices, truncated = _project(root, at, work=work)
        ordered = [nodes[k] for k in sorted(nodes)]
        return dict(ok=True, asOf=at, historical=at is not None, incompleteGroups=sorted(work.incomplete), nodes=ordered, edges=edges, counts=_counts(ordered, edges),
                    notices=notices, truncated=truncated)
    except (ValueError, OSError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        return dict(ok=False, reason=_refusal(exc), **(dict(historical=True, notices=[GRAPH_HISTORY_NOTICE])
                    if as_of is not None else {}))
    finally:
        work.finish()


def _render_item(item):
    if item["kind"] == "lesson":
        # Whole quoted lines and every citation are rendered. Size is handled
        # only by _trim, which removes whole items, never by truncating prose.
        quotes = [mb.safe_lesson_display(q) for q in item.get("quoteLines", [item.get("text", "")])]
        cites = [Path(c["file"]).name + ":" + str(c["start"]) for c in item.get("cites", [])]
        relations = item.get("relations") or item["relation"].split(",")
        prefix = v.GRAPH_RELATION_MARKER % ", ".join(relations)
        if item.get("quarantined"):
            parts = sorted(zip(item.get("lineRoles", []), quotes, cites), key=lambda p: p[0] != "replaced")
            quotes = [(v.GRAPH_REPLACED_MARKER if role == "replaced" else v.GRAPH_CURRENT_MARKER) + quote
                      for role, quote, cite in parts]
            cites = [cite for role, quote, cite in parts]
        if item.get("tier") == HINT:
            prefix += v.GRAPH_HINT_MARKER % mb.hint_item_label(item)
        return "- " + prefix + v.LESSON_SEPARATOR.join(quotes) + " (" + ", ".join(cites) + ")"
    label = (SUGGESTED_ANCHOR + " ") if item.get("suggested") else ""
    if item.get("replacedBy"):
        label += v.GRAPH_REPLACED_MARKER
    else:
        label += v.GRAPH_STATUS_LABELS.get(item.get("status"), v.GRAPH_STATUS_LABELS["unverified"])
    return "- " + label + item["text"] + " (`" + item["source"] + "`)"


def _format(groups, omitted, incomplete=()):
    sections = ["- " + v.GRAPH_INCOMPLETE_MARKER + ", ".join(sorted(incomplete))] if incomplete else []
    for group in GROUPS:
        if groups[group]:
            heading = "### " + GRAPH_HEADINGS[group]
            if group == "hints":
                heading += "\n" + HINT_HELP
            sections.append(heading + "\n" + "\n".join(_render_item(i) for i in groups[group]))
    if any(omitted.values()):
        sections.append("- " + GRAPH_OMITTED + ", ".join(k + " " + str(n) for k, n in omitted.items() if n))
    return "\n\n".join(sections) + ("\n" if sections else "")


def _trim(groups, budget, incomplete=()):
    omitted = {g: 0 for g in GROUPS}
    # Linear pre-trim using additive costs; final estimator includes headings.
    costs = {g: [(mw.estimate_tokens(text), len(text)) for text in map(_render_item, groups[g])] for g in GROUPS}
    total = sum(sum(v[0] for v in values) for values in costs.values())
    chars = sum(sum(v[1] for v in values) for values in costs.values())
    for group in ("hints", "resolves", "governs", "stale", "blocks", "replaced"):
        while groups[group] and (total > budget or chars > MAX_FORMATTED_CHARS):
            groups[group].pop()
            omitted[group] += 1
            tokens, length = costs[group].pop()
            total -= tokens
            chars -= length
    rendered = _format(groups, omitted, incomplete)
    for group in ("hints", "resolves", "governs", "stale", "blocks", "replaced"):
        while groups[group] and (mw.estimate_tokens(rendered) > budget or len(rendered) > MAX_FORMATTED_CHARS):
            groups[group].pop()
            omitted[group] += 1
            rendered = _format(groups, omitted, incomplete)
    if mw.estimate_tokens(rendered) > budget or len(rendered) > MAX_FORMATTED_CHARS:
        rendered = ""  # omission counts remain in JSON, even for a zero budget
    return omitted, rendered


def neighbors(repo_path=".", target="", *, tiers=(APPROVED,), budget=600, as_of=None,
              _clock=None, _counters=None):
    work = _Work(bounded=True, clock=_clock, counters=_counters)
    try:
        root, at = mw._resolve_root(repo_path), _at(as_of)
        if isinstance(tiers, str):
            tiers = tuple(tiers.split(","))
        if not tiers or set(tiers) - {APPROVED, HINT}:
            raise ValueError(v.GRAPH_INVALID_TIERS)
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ValueError(v.GRAPH_INVALID_BUDGET)
        if not isinstance(target, str) or not target:
            raise ValueError(v.GRAPH_TARGET_REQUIRED)
        ident, queue = None, None
        if "::" not in target and "/" not in target and md._ID_RE.fullmatch(target) and not (root / target).exists():
            if re.fullmatch(r"(?:file|symbol|line)-[a-f0-9]{24}", target):
                full_nodes, _, _, _, _ = _project(root, at, queue=queue, work=work)
                found = full_nodes.get(target)
                if found is None:
                    raise ValueError(v.GRAPH_UNKNOWN_ID)
                if found["kind"] in ("file", "symbol"):
                    target_path = found["anchor"] if found["kind"] == "symbol" else found["path"]
                    return neighbors(repo_path, target_path, tiers=tiers, budget=budget, as_of=as_of,
                                     _clock=_clock, _counters=_counters)
                return neighbors(repo_path, found["lessonId"], tiers=tiers, budget=budget, as_of=as_of,
                                 _clock=_clock, _counters=_counters)
            elif re.fullmatch(r"rule-[a-f0-9]{24}", target):
                ident = target
            else:
                queue = _queue(root, work)
                if any(n.get("id") == target for n in queue.get("nodes", [])):
                    ident = target
        path, symbol = None, None
        if ident is None:
            path, sep, symbol = target.partition("::")
            symbol = symbol if sep else None
            if symbol is not None and not ma._SYMBOL_RE.fullmatch(symbol):
                raise ValueError(v.GRAPH_INVALID_SYMBOL)
            path = _clean_path(root, path, exists=False, directories=work.directories, counters=work.counters)
            if path is None:
                raise ValueError(v.GRAPH_INVALID_TARGET)
        nodes, edges, attachments, notices, truncated = _project(root, at, path=path, symbol=symbol, ident=ident, queue=queue, work=work)
        if ident is not None:
            if ident not in nodes:
                if ident not in work.known_ids or (at is None and not work.incomplete):
                    raise ValueError(v.GRAPH_UNKNOWN_ID)
                resolved = dict(kind=work.known_ids[ident], id=ident)
            else:
                resolved = dict(kind=nodes[ident]["kind"], id=ident)
        else:
            if not (root / path).is_file() and not any(n.get("kind") == "file" and n.get("path") == path
                                                       for n in nodes.values()):
                raise ValueError(v.GRAPH_UNKNOWN_FILE)
            resolved = dict(kind="symbol" if symbol else "file", id=_id("symbol", path + "::" + symbol)
                            if symbol else _id("file", path))
        groups, seen = {g: [] for g in GROUPS}, set()
        def include(node, relation, why, group, **extra):
            if node.get("tier") and node["tier"] not in tiers:
                return
            key = (group, node["id"], relation)
            if key in seen:
                return
            seen.add(key)
            item = {k: node[k] for k in ("id", "kind", "tier", "status", "text", "quoteLines", "cites", "source",
                    "suggested", "replacedBy", "quarantined", "lineRoles", "lineReplacements", "replacementLabel",
                    "agreementBasis", "agreementLabel", "current", "replacementUnknown", "validFrom", "validTo") if k in node}
            item.update(relation=relation, relations=node.get("edgeTypes") or [relation], why=why, **extra)
            groups[group].append(item)
        connected = {ident} if ident else set()
        if ident:
            for e in edges:
                if e.get("lessonId") == ident:
                    connected.update((e["src"], e["dst"]))
            adjacency = {}
            for e in edges:
                if e["type"] == "SUPERSEDES":
                    adjacency.setdefault(e["src"], set()).add(e["dst"])
                    adjacency.setdefault(e["dst"], set()).add(e["src"])
            pending = list(connected)
            while pending:
                for other in adjacency.get(pending.pop(), ()):
                    if other not in connected:
                        connected.add(other)
                        pending.append(other)
            connected.update(i for n in list(connected) if n in nodes for i in nodes[n].get("lessonIds", []))
            connected.update(nodes.get(ident, {}).get("replacedBy", []))
        replaced_for_target = {i for r in nodes.values() if r.get("kind") == "rule"
                               and (not ident or r["id"] in connected) for i in r.get("replacedBy", [])}
        for node in nodes.values():
            if node["kind"] == "rule":
                selected = [a for a in node["anchors"] if (a == path + "::" + symbol if symbol else
                            a.split("::", 1)[0] == path)] if path else node["anchors"]
                if not selected or (ident and node["id"] not in connected):
                    continue
                why = GRAPH_WHY[5] if ident == node["id"] else GRAPH_WHY[1] if symbol else GRAPH_WHY[0]
                if node.get("replacedBy"):
                    include(node, "SUPERSEDES", GRAPH_WHY[5] if ident == node["id"] else GRAPH_WHY[4], "replaced", targets=selected)
                else:
                    # Aggregate rule status is the same worst-anchor state as
                    # recall/handover. One active anchor cannot rescue a stale rule.
                    include(node, "GOVERNS", why, "governs" if node["status"] == "active" and not node.get("replacementUnknown") else "stale", targets=selected)
            elif node.get("recordType") == "item":
                why = attachments.get(node["id"], {}).get(path)
                if ident and node["id"] in connected:
                    why = GRAPH_WHY[5] if node["id"] == ident else GRAPH_WHY[6]
                if why is None:
                    # A file's anchored rule may be replaced by a lesson whose
                    # transcript never mentions the file: the explicit text link.
                    if node["id"] in replaced_for_target:
                        why = GRAPH_WHY[4]
                    else:
                        continue
                relations = node.get("edgeTypes", [])
                if node["tier"] == HINT:
                    include(node, ",".join(relations), why, "hints", targets=node.get("paths", []))
                elif "replaced" in node.get("lineRoles", []):
                    include(node, "SUPERSEDES", why, "replaced", targets=node.get("paths", []))
                else:
                    # One approved item appears once, with all its approved
                    # relations. A prohibition wins over advice or a fix.
                    relation = next((r for r in ("BLOCKS", "GOVERNS", "RESOLVES") if r in relations), None)
                    if relation:
                        group = {"GOVERNS": "governs", "BLOCKS": "blocks", "RESOLVES": "resolves"}[relation]
                        current = dict(node, quarantined=False, current=True)
                        include(current, relation, why, group, targets=node.get("paths", []))
                    elif node.get("quarantined"):
                        notices.append(v.GRAPH_CURRENT_ONLY_NOTICE)
        for group in groups:
            groups[group].sort(key=lambda i: (i["id"], i["relation"]))
        omitted, formatted = _trim(groups, min(budget, 8000), work.incomplete)
        # Output budgeting is a second source of incomplete groups, independent
        # of scan limits. In particular partial blocks/replaced are never whole.
        incomplete = work.incomplete | {g for g, count in omitted.items() if count}
        if at is not None:
            body = formatted or v.GRAPH_HISTORICAL_EMPTY % at + "\n"
            formatted = v.GRAPH_HISTORICAL_HEAD % at + "\n" + body + "\n" + "\n".join(notices) + "\n"
        return dict(ok=True, target=target, resolved=resolved, groups=groups, omitted=omitted,
                    historical=at is not None, truncated=bool(truncated or incomplete), incompleteGroups=sorted(incomplete),
                    blocksIncomplete="blocks" in incomplete, replacedIncomplete="replaced" in incomplete,
                    tokens=mw.estimate_tokens(formatted), formatted=formatted, notices=notices)
    except (ValueError, OSError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        return dict(ok=False, reason=_refusal(exc), **(dict(historical=True, notices=[GRAPH_HISTORY_NOTICE])
                    if as_of is not None else {}))
    finally:
        work.finish()
