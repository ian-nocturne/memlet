"""A read-only projection, never a second authority for memory.

Rules use the wiki reader and classifier. Lessons use dream_items (including its
global supersession projection). Historical approval records are reconstructed
only from digest-bound decision events, then passed through that same reader.
No old source contents are read. No cache, clock, model, network or write occurs.

The file query indexes references before classifying code or verifying lessons;
it does not construct the project graph. Bounds deliberately fail closed when a
complete supersession view cannot be obtained. Citation nodes are lesson nodes
with recordType='citation'; parent lessons retain the queue's stable ids.
"""
import contextlib
import copy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess

from . import code_anchors as ca, memlet_anchors as ma, memlet_bridge as mb
from . import memlet_dream as md, memlet_wiki as mw
from .memlet_eval import _stamp
from .vocabulary import (APPROVED, HINT, GRAPH_WHY, GRAPH_HEADINGS, GRAPH_TIME_NOTICE,
                         GRAPH_HISTORY_NOTICE, GRAPH_OMITTED, HINT_HELP, SUGGESTED_ANCHOR)

NODE_KINDS = ("rule", "lesson", "symbol", "file")
GROUPS = ("governs", "blocks", "resolves", "replaced", "stale", "hints")
MAX_RULES = 5000
MAX_LESSONS = 5000
MAX_EDGES = 20000
MAX_EVENTS = 100000
MAX_RULE_CHARS = 16384
MAX_FORMATTED_CHARS = 8000
MAX_SUPERSESSION_SLICE = 512
_LITERAL_PATH = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]+(?![\w/.-])")


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
        raise ValueError("invalid as_of timestamp")
    return stamp


def _before(a, b):
    return a is not None and (b is None or _stamp(a) <= _stamp(b))


def _earliest(values):
    values = [v for v in values if v is not None]
    return min(values, key=_stamp) if values else None


def _latest(values):
    values = list(values)
    return max(values, key=_stamp) if values and all(values) else None


def _clean_path(root, value, *, exists=True):
    if (not isinstance(value, str) or not value or len(value) > 1000 or "\\" in value
            or any(mb._hidden_character(c) or ord(c) < 32 or ord(c) == 127 for c in value)):
        return None
    p = Path(value)
    if p.is_absolute():
        try:
            p = p.relative_to(root)
        except ValueError:
            return None
    rel = p.as_posix()
    if rel in ("", ".") or mw._unsafe_rel(rel) or mw.contained_path_problem(root, rel):
        return None
    if exists and not (root / rel).is_file():
        return None
    # Case-sensitive spelling even on a case-insensitive host. Directory names
    # are cached only inside this one validation, never as evidence across calls.
    cursor = root
    for part in p.parts:
        try:
            if cursor.exists() and part not in {x.name for x in cursor.iterdir()}:
                if exists:
                    return None
                break
        except OSError:
            return None
        cursor /= part
    return rel


class _Files:
    """One tracked-name index and one containment check per literal candidate."""
    def __init__(self, root):
        self.root, self.checked = root, {}
        self.names = set()
        self.in_git = True  # unknown git failures never enable implicit attachment
        try:
            result = subprocess.run(["git", "ls-files", "-z", "--cached"], cwd=str(root),
                                    capture_output=True, timeout=2)
            if result.returncode == 0 and len(result.stdout) <= 8 * 1024 * 1024:
                self.names = set(result.stdout.decode("utf-8", "strict").split("\0"))
            elif result.returncode == 128 and b"not a git repository" in result.stderr:
                self.in_git = False
        except (OSError, subprocess.SubprocessError, UnicodeError):
            pass

    def accepts(self, path):
        if path not in self.checked:
            self.checked[path] = (not self.in_git or path in self.names) and _clean_path(self.root, path) == path
        return self.checked[path]


def _candidate_paths(scope, text):
    # Candidate index only; NEVER sufficient to attach or approve a lesson.
    raw = [str(scope).split("::", 1)[0]] + [p for p, _ in mw._line_refs(text)]
    raw += _LITERAL_PATH.findall(text)
    return {p[2:] if p.startswith("./") else p for p in raw if p and len(p) <= 1000}


def _attached(root, node, item, files, context=None):
    """E3 adapter: human paths and the shared literal attachment computation."""
    from .memlet_dreamcycle import evidence_pins
    evidence = node
    if item["tier"] == APPROVED:
        evidence = dict(node, edges=[e for e in node.get("edges", []) if md.approval_state(e, "edge") == APPROVED])
    # Attachment is tested against original cited bytes. Rendered quotes strip
    # hidden characters and would otherwise turn a poisoned path into a match.
    text = "\n".join(p["quote"] for p in md.lesson_lines(dict(allPins=evidence_pins(evidence))))
    scope = node.get("scopeTag", "") if item["tier"] == APPROVED else ""
    literal = getattr(mb, "literal_lesson_paths", None)
    if literal:
        paths = literal(root, scope, text, context=context) if context is not None else literal(root, scope, text)
    else:
        paths = []
        for p in sorted(_candidate_paths(scope, text)):
            named = p == scope.split("::", 1)[0].removeprefix("./")
            if not named:
                named = any(not any(mb._hidden_character(c) for c in text[max(0, m.start()-1):m.start()] +
                                    text[m.end():m.end()+1]) for m in re.finditer(
                                        r"(?<![\w/.-])" + re.escape(p) + r"(?![\w/.-])", text))
            if named and files.accepts(p):
                paths.append(p)
        paths = paths[:8]
    result = {p: GRAPH_WHY[3] for p in paths}
    if item["tier"] == APPROVED:
        scope_path = scope.split("::", 1)[0].removeprefix("./")
        if scope_path in result:
            result[scope_path] = GRAPH_WHY[2]
        reader = getattr(mb, "lesson_paths", None)
        paths = (reader(node, root, context=context) if context is not None else reader(node, root)) if reader else ()
        for p in paths:
            result[p] = GRAPH_WHY[2]
    return dict(sorted(result.items()))


class _History:
    def __init__(self, root):
        self.decisions, self.sidecars = {}, {}
        if mw.first_managed_problem(root, mw._LEDGER_FILES) or not (root / mw.LEDGER_PATH).is_file():
            return
        try:
            with contextlib.closing(mw.connect_managed_sqlite(root, mw.LEDGER_PATH, readonly=True)) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "dream_decision_events" in tables:
                    rows = db.execute("SELECT seq, at, item_id, kind, decision, by, digest, paths "
                                      "FROM dream_decision_events ORDER BY seq LIMIT ?", (MAX_EVENTS + 1,)).fetchall()
                    if len(rows) > MAX_EVENTS:
                        raise ValueError("decision history exceeds graph work bound")
                    for seq, at, ident, kind, decision, by, digest, paths in rows:
                        decision = {"reconsidered": APPROVED, "paths-changed": APPROVED,
                                    "citations-refreshed": APPROVED, "revoked": "rejected"}.get(decision, decision)
                        event = dict(at=_time(at), decision=decision, by=by, digest=digest, paths=paths)
                        self.decisions.setdefault((kind, ident), []).append(event)
                if ma.LEDGER_TABLE in tables:
                    rows = db.execute("SELECT entry_id, entry_sha256, event_type, recorded_at FROM " +
                                      ma.LEDGER_TABLE + " ORDER BY id LIMIT ?", (MAX_EVENTS + 1,)).fetchall()
                    if len(rows) > MAX_EVENTS:
                        raise ValueError("anchor history exceeds graph work bound")
                    for ident, digest, event, at in rows:
                        self.sidecars.setdefault(ident, []).append(dict(at=_time(at), digest=digest, decision=event))
        except sqlite3.Error:
            # A malformed history must never authorize reconstructed evidence.
            self.decisions, self.sidecars = {}, {}

    def decision(self, item, kind, as_of):
        ident = item.get("id" if kind == "node" else "edgeId")
        events = self.decisions.get((kind, ident), [])
        if not events:
            record = item.get("approval", {})
            return record if _before(_time(record.get("at")), as_of) else {}
        eligible = [r for r in events if _before(r["at"], as_of)]
        return eligible[-1] if eligible else {}

    def interval(self, item, kind, as_of):
        record = self.decision(item, kind, as_of)
        start = _time(record.get("at")) if record.get("decision") == APPROVED else None
        return start, None  # future decisions are intentionally not leaked

    def intervals(self, item, kind, as_of):
        """Retained validity intervals for this exact content; no future end dates.

        Rejection removes a lesson from the current projection. If the same
        content is later re-approved, this metadata still exposes the prior
        closed interval rather than pretending approval was uninterrupted.
        """
        ident = item.get("id" if kind == "node" else "edgeId")
        events = self.decisions.get((kind, ident), [])
        if not events:
            start, end = self.interval(item, kind, as_of)
            return [{"validFrom": start, "validTo": end}] if start else []
        result = []
        for record in events:
            if not _before(record["at"], as_of):
                continue
            if result and result[-1]["validTo"] is None:
                result[-1]["validTo"] = record["at"]
            if record["decision"] != APPROVED:
                continue
            candidate = copy.deepcopy(item)
            if kind == "node" and "paths" in record:
                try:
                    paths = json.loads(record["paths"])
                    if paths or "approvedPaths" in candidate:
                        candidate["approvedPaths"] = paths
                except (ValueError, TypeError):
                    continue
            digest = md.node_approval_digest(candidate) if kind == "node" else md.edge_approval_digest(candidate)
            if record.get("digest") == digest:
                result.append({"validFrom": record["at"], "validTo": None})
        return result

    def replay(self, graph, as_of):
        if as_of is None:
            return graph
        view = copy.deepcopy(graph)
        def restore(item, kind):
            record = self.decision(item, kind, as_of)
            item["approval"] = {k: record.get(k) for k in ("decision", "by", "digest", "at")}
            item["approval"]["schema"] = md.APPROVAL_RECORD_SCHEMA
            if "paths" in record:
                try:
                    paths = json.loads(record["paths"]) if isinstance(record["paths"], str) else record["paths"]
                    if isinstance(paths, list):
                        if paths or "approvedPaths" in item:
                            item["approvedPaths"] = paths
                        item["approval"]["paths"] = paths
                except (ValueError, TypeError):
                    item["approval"] = {}
            # The persisted verdict describes TODAY. approved_view/dream_items
            # recompute the historical verdict from the restored sealed record.
            item["verdict" if kind == "node" else "edgeVerdict"] = md.PENDING
            if kind == "node":
                item["proposers"], item["verification"] = [], []
        for node in view.get("nodes", []):
            restore(node, "node")
            for edge in node.get("edges", []):
                restore(edge, "edge")
        for edge in view.get("edges", []):
            restore(edge, "edge")
        return view

    def anchor_time(self, rule, anchor, config, pin):
        def matches(other):
            return bool(pin and other and mw._same_hash(pin.get("ast"), other.get("ast")))
        sid = rule.get("sidecarIds", {}).get(anchor)
        if sid:
            if not hasattr(self, "_sidecar_pins"):
                self._sidecar_pins = {e.get("id"): e.get("pin") for e in config.get("sidecarAnchors", [])
                                      if isinstance(e, dict)}
            events = self.sidecars.get(sid, [])
            if matches(self._sidecar_pins.get(sid)) and events and events[-1]["decision"] == "accept":
                return events[-1]["at"]
            return None
        stored = config.get("pinnedAnchors", {}).get(anchor, {})
        return _time(stored.get("pinnedAt")) if matches({"ast": stored.get("astHash")}) else None


def _sources(root, lesson_items):
    rules, pins, texts, truncated = [], {}, {}, False
    source_files = mw.import_source_files(root, "auto")
    sources = mw._source_texts(root, source_files)
    if len(sources) < len(source_files):
        truncated = True
    for rel, text in mw.trusted_wiki_pages(root):
        for anchor in mw._extract_anchors(text, rel):
            if anchor["pin"]:
                pins.setdefault(anchor["path"] + "::" + anchor["symbol"], anchor["pin"])
        if rel == mb.PROMOTED_PAGE:
            # Only the exact lines whose owning lesson survives the shared
            # trust/supersession reader are rules. No model-written page prose.
            live = {i["id"] for i in lesson_items if i["tier"] == APPROVED and not i.get("quarantined")}
            parsed = {r["line"]: r for r in mb._parse_promoted(text) if r["id"] in live}
            body = "\n".join(line if n in parsed else "" for n, line in enumerate(text.splitlines(), 1))
            sources.append((rel, body))
        elif not mw._is_generated_page(rel, text):
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


def _fresh_classification(root, rules, pins):
    paths = {str(root / path) for r in rules for path in _rule_files(root, r)}
    # 9.1 extractors contain stat-keyed caches. Do not treat those as proof:
    # discard entries for the selected files before calling the single classifier.
    for cache in (ca._FILE_HASH_AST_CACHE, ca._FILE_SIGNATURE_CACHE):
        for key in list(cache):
            if key[0] in paths:
                cache.pop(key, None)
    return mw._verify_rules(root, rules, pins, pin_source="pinned")


def _queue(root):
    if mb._symlinked(root, (mb.QUEUE_REL, mb.SNAPSHOT_REL)):
        raise ValueError("unsafe Dream input")
    graph, _ = mb._load_queue(root)
    graph = graph or {"nodes": [], "edges": []}
    if len(graph.get("nodes", [])) > MAX_LESSONS or len(graph.get("edges", [])) > MAX_EDGES:
        raise ValueError("Dream graph exceeds graph work bound")
    return graph


def _slice(graph, path, ident):
    """Index raw references without assigning any tier or trusting any text.

    All supersessions and rejected evidence remain in the slice so an unrelated
    source cannot resurrect a repeated old instruction or a rejected hint.
    """
    selected, supersessions = set(), set()
    by_file = {}
    for node in graph.get("nodes", []):
        nid = node.get("id")
        text = "\n".join(p["quote"] for p in md.lesson_lines(node))
        paths = _candidate_paths(node.get("scopeTag", ""), text)
        paths.update(p for p in node.get("approvedPaths", []) if isinstance(p, str))
        for p in paths:
            by_file.setdefault(p, set()).add(nid)
        edges = node.get("edges", [])
        if any(e.get("edgeType") == "SUPERSEDES" for e in edges):
            supersessions.add(nid)
        if node.get("approval", {}).get("decision") == "rejected" or any(
                e.get("approval", {}).get("decision") == "rejected" for e in edges):
            selected.add(nid)
    if len(supersessions) > MAX_SUPERSESSION_SLICE:
        raise ValueError("supersession slice exceeds graph work bound")
    selected.update(supersessions)
    selected.update(by_file.get(path, ()))
    if ident:
        selected.add(ident)
    # Include all node endpoints of selected edges, in linear time.
    adjacency = {}
    for e in graph.get("edges", []):
        if e.get("edgeType") == "SUPERSEDES":
            a, b = e.get("fromId"), e.get("toId")
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)
    pending = list(selected)
    while pending:
        for other in adjacency.get(pending.pop(), ()):
            if other not in selected:
                selected.add(other)
                pending.append(other)
    return dict(graph, nodes=[n for n in graph.get("nodes", []) if n.get("id") in selected],
                edges=[e for e in graph.get("edges", [])
                       if e.get("fromId") in selected or e.get("toId") in selected])


def _approved_relations(graph, items):
    """Keep the sole computed tier; do not promote an unapproved edge with it.

    Legacy dream_items may expose all edge types of an approved node, including
    pending edges. Its inline supersession display also predates this gate.
    Re-use its display/projection computations on the approved-valid edge view;
    this adapter becomes a no-op once the shared reader filters those edges.
    """
    approved_ids = {i["id"] for i in items if i["tier"] == APPROVED}
    if not any(n.get("id") in approved_ids and any(md.approval_state(e, "edge") != APPROVED
               for e in n.get("edges", [])) for n in graph.get("nodes", [])):
        return items
    approved = md.approved_view(graph)[0]
    approved["nodes"] = [n for n in approved.get("nodes", []) if n.get("id") in approved_ids]
    for node in approved["nodes"]:
        node["edges"] = [e for e in node.get("edges", []) if e.get("edgeVerdict") == "VALID"]
    approved["edges"] = [e for e in approved.get("edges", []) if e.get("edgeVerdict") == "VALID"
                         and (e.get("fromId") in approved_ids or e.get("toId") in approved_ids)]
    projection = md.supersession_projection(approved)
    by_id = {n["id"]: n for n in approved["nodes"]}
    out = []
    for item in items:
        if item["tier"] != APPROVED:
            out.append(item)
            continue
        node = md.supersession_display_node(by_id[item["id"]], approved, projection)
        fields = mb._lesson_fields(node)
        verified = {md.citation_key(c): c.get("verified") is True for c in item["cites"]}
        fields["cites"] = [dict(c, verified=verified.get(md.citation_key(c), False)) for c in fields.get("cites", [])]
        if not fields["cites"] or not all(c["verified"] for c in fields["cites"]):
            continue
        clean = {k: v for k, v in item.items() if k not in
                 ("quarantined", "lineRoles", "lineReplacements", "replacementLabel")}
        out.append(dict(clean, **fields, text=md.lesson_text(node),
                        edgeTypes=sorted({e["edgeType"] for e in node.get("edges", [])})))
    return out


def _project(root, as_of, *, path=None, symbol=None, ident=None, queue=None):
    history = _History(root)
    config, error = mw.read_config(root)
    config = config or {}
    graph = history.replay(_queue(root) if queue is None else queue, as_of)
    if path is not None or ident is not None:
        graph = _slice(graph, path, ident)
    read = mb.dream_items(root, tiers=(APPROVED,) if as_of else (APPROVED, HINT), _graph=graph)
    if not read["ok"]:
        raise ValueError("Dream evidence could not be verified")
    read["items"] = _approved_relations(graph, read["items"])
    notices = [GRAPH_TIME_NOTICE] + ([GRAPH_HISTORY_NOTICE] if as_of else [])
    notices.extend(read.get("notices", []))
    if error:
        notices.append(error)
    raw = {n["id"]: n for n in graph.get("nodes", [])}
    rules, pins, texts, truncated = _sources(root, read["items"])
    safe_rules = [r for r in rules if len(r["refs"]) <= 128 and _rule_text(r, texts) is not None]
    truncated |= len(safe_rules) < len(rules)
    rules = safe_rules
    if path is not None:
        # A promoted rule is already carried by its verified lesson in this
        # slice; avoid re-entering the full Dream reader via enrich_promoted_rules.
        rules = [r for r in rules if r["source"] != mb.PROMOTED_PAGE and path in _rule_files(root, r)]
    elif ident is not None and ident.startswith("rule-"):
        rules = [r for r in rules if _rule_id(r) == ident]
    elif ident is not None:
        # Matching replaced instruction rules are needed for a lesson-id query.
        old = md.replaced_line_index(read["items"])[1]
        rules = [r for r in rules if md.normalised_quote(_rule_text(r, texts) or "") in old]
    anchors = {a["anchor"]: a for a in _fresh_classification(root, rules, pins)}
    nodes, edges, attachments = {}, [], {}
    context_factory = getattr(mb, "lesson_path_context", None)
    path_context = context_factory(root) if context_factory else None
    files = _Files(root) if path_context is None else None
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
        node = raw[item["id"]]
        start, end = history.interval(node, "node", as_of)
        if item["tier"] == HINT:
            # Exact hint first-observed time cannot be reconstructed from policy.
            start = None
        if as_of and not _before(start, as_of):
            continue
        attached = _attached(root, node, item, files, path_context)
        attachments[item["id"]] = attached
        item = dict(item, kind="lesson", recordType="item", validFrom=start, validTo=end,
                    current=not item.get("quarantined", False),
                    validity=history.intervals(node, "node", as_of),
                    source=item["cites"][0]["file"], paths=list(attached))
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
                safe_rule_paths[p] = _clean_path(root, p, exists=False)
        allowed = [k for k in keys if k in anchors and safe_rule_paths.get(anchors[k]["path"])]
        starts = {k: history.anchor_time(rule, k, config, pins.get(k)) for k in allowed}
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
                 current=not replacing, validFrom=start, validTo=ended))
        for key in allowed:
            anchor = anchors[key]
            fid = file_node(anchor["path"], starts[key])
            if anchor.get("symbol"):
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
                safe_rule_paths[rel] = _clean_path(root, rel, exists=False)
            if not safe_rule_paths[rel]:
                continue
            since = history.anchor_time({}, key, config, pins[key])
            if as_of and not _before(since, as_of):
                continue
            for cache in (ca._FILE_HASH_AST_CACHE, ca._FILE_SIGNATURE_CACHE):
                for cached in list(cache):
                    if cached[0] == str(root / rel):
                        cache.pop(cached, None)
            anchor = mw.classify_anchor(root, dict(path=rel, symbol=name, pin=pins[key]))
            if as_of and anchor["status"] != "active":
                continue
            add(dict(id=_id("symbol", key), kind="symbol", anchor=key, symbol=name,
                     file=file_node(rel, since), status=anchor["status"], reason=anchor["reason"],
                     validFrom=since, validTo=None))
    # Deduplicate identical edges without position-dependent ids.
    edges = list({json.dumps(e, sort_keys=True): e for e in edges}.values())
    return nodes, sorted(edges, key=lambda e: (e["type"], e["src"], e["dst"])), attachments, notices, truncated


def _counts(nodes, edges):
    return {"nodes": {kind: sum(n["kind"] == kind for n in nodes) for kind in NODE_KINDS},
            "edges": {kind: sum(e["type"] == kind for e in edges) for kind in md.EDGE_TYPES},
            "lessonItems": sum(n.get("recordType") == "item" for n in nodes),
            "unattachedLessons": sum(n.get("recordType") == "item" and not n.get("paths") for n in nodes),
            "unanchoredRules": sum(n["kind"] == "rule" and not n.get("anchors") for n in nodes)}


def build_graph(repo_path=".", *, as_of=None):
    try:
        root, at = mw._resolve_root(repo_path), _at(as_of)
        nodes, edges, _, notices, truncated = _project(root, at)
        ordered = [nodes[k] for k in sorted(nodes)]
        return dict(ok=True, asOf=at, nodes=ordered, edges=edges, counts=_counts(ordered, edges),
                    notices=notices, truncated=truncated)
    except (ValueError, OSError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        return {"ok": False, "reason": str(exc)}


def _render_item(item):
    if item["kind"] == "lesson":
        return mb.render_capsule_lesson(dict(item, edgeTypes=[item["relation"]]), hint=item.get("tier") == HINT)
    label = (SUGGESTED_ANCHOR + " ") if item.get("suggested") else ""
    if item.get("replacedBy"):
        label += "[REPLACED — do not apply as current] "
    else:
        label += "[" + item.get("status", "unverified") + "] "
    return "- " + label + item["text"] + " (`" + item["source"] + "`)"


def _format(groups, omitted):
    sections = []
    for group in GROUPS:
        if groups[group]:
            heading = "### " + GRAPH_HEADINGS[group]
            if group == "hints":
                heading += "\n" + HINT_HELP
            sections.append(heading + "\n" + "\n".join(_render_item(i) for i in groups[group]))
    if any(omitted.values()):
        sections.append("- " + GRAPH_OMITTED + ", ".join(k + " " + str(n) for k, n in omitted.items() if n))
    return "\n\n".join(sections) + ("\n" if sections else "")


def _trim(groups, budget):
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
    rendered = _format(groups, omitted)
    for group in ("hints", "resolves", "governs", "stale", "blocks", "replaced"):
        while groups[group] and (mw.estimate_tokens(rendered) > budget or len(rendered) > MAX_FORMATTED_CHARS):
            groups[group].pop()
            omitted[group] += 1
            rendered = _format(groups, omitted)
    if mw.estimate_tokens(rendered) > budget or len(rendered) > MAX_FORMATTED_CHARS:
        rendered = ""  # omission counts remain in JSON, even for a zero budget
    return omitted, rendered


def neighbors(repo_path=".", target="", *, tiers=(APPROVED,), budget=600, as_of=None):
    try:
        root, at = mw._resolve_root(repo_path), _at(as_of)
        if isinstance(tiers, str):
            tiers = tuple(tiers.split(","))
        if not tiers or set(tiers) - {APPROVED, HINT}:
            raise ValueError("tiers must contain only approved and hint")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ValueError("budget must be a non-negative integer")
        if not isinstance(target, str) or not target:
            raise ValueError("target is required")
        ident, queue = None, None
        if "::" not in target and "/" not in target and md._ID_RE.fullmatch(target) and not (root / target).exists():
            if re.fullmatch(r"rule-[a-f0-9]{24}", target):
                ident = target
            else:
                queue = _queue(root)
                if any(n.get("id") == target for n in queue.get("nodes", [])):
                    ident = target
        path, symbol = None, None
        if ident is None:
            path, sep, symbol = target.partition("::")
            symbol = symbol if sep else None
            if symbol is not None and not ma._SYMBOL_RE.fullmatch(symbol):
                raise ValueError("invalid symbol target")
            path = _clean_path(root, path, exists=False)
            if path is None:
                raise ValueError("target is outside the repository, unsafe, or not an existing regular file")
        nodes, edges, attachments, notices, truncated = _project(root, at, path=path, symbol=symbol, ident=ident, queue=queue)
        if ident is not None:
            if ident not in nodes or nodes[ident]["kind"] not in ("rule", "lesson"):
                raise ValueError("unknown rule or lesson id")
            resolved = dict(kind=nodes[ident]["kind"], id=ident)
        else:
            if not (root / path).is_file() and not any(n.get("kind") == "file" and n.get("path") == path
                                                       for n in nodes.values()):
                raise ValueError("unknown file target")
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
                    "agreementBasis", "agreementLabel", "current", "validFrom", "validTo") if k in node}
            item.update(relation=relation, why=why, **extra)
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
            connected.update(nodes[ident].get("replacedBy", []))
        replaced_for_target = {i for r in nodes.values() if r.get("kind") == "rule"
                               and (not ident or r["id"] in connected) for i in r.get("replacedBy", [])}
        for node in nodes.values():
            if node["kind"] == "rule":
                selected = [a for a in node["anchors"] if (a == path + "::" + symbol if symbol else
                            a.split("::", 1)[0] == path)] if path else node["anchors"]
                if not selected or (ident and node["id"] not in connected):
                    continue
                why = GRAPH_WHY[1] if symbol else GRAPH_WHY[0]
                if node.get("replacedBy"):
                    include(node, "SUPERSEDES", GRAPH_WHY[4], "replaced", targets=selected)
                else:
                    # Aggregate rule status is the same worst-anchor state as
                    # recall/handover. One active anchor cannot rescue a stale rule.
                    include(node, "GOVERNS", why, "governs" if node["status"] == "active" else "stale", targets=selected)
            elif node.get("recordType") == "item":
                why = attachments.get(node["id"], {}).get(path)
                if ident and node["id"] in connected:
                    why = GRAPH_WHY[2]
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
                elif node.get("quarantined"):
                    include(node, "SUPERSEDES", why, "replaced", targets=node.get("paths", []))
                else:
                    for relation in relations:
                        group = {"GOVERNS": "governs", "BLOCKS": "blocks", "RESOLVES": "resolves"}.get(relation)
                        if group:
                            include(node, relation, why, group, targets=node.get("paths", []))
        for group in groups:
            groups[group].sort(key=lambda i: (i["id"], i["relation"]))
        omitted, formatted = _trim(groups, min(budget, 8000))
        if truncated:
            notices.append("Rule input bounds reached; inspect the source for omitted rules.")
        return dict(ok=True, target=target, resolved=resolved, groups=groups, omitted=omitted,
                    tokens=mw.estimate_tokens(formatted), formatted=formatted, notices=notices)
    except (ValueError, OSError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        return {"ok": False, "reason": str(exc)}
