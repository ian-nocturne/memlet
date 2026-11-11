"""Explicit handover --write projections, never a new source of trust.

Reader contract inspected 2026-11-11: Routlet scripts/memlet_bridge.py.
No runtime dependency on Routlet. Its reader does not verify our integrity digest,
propagate truncation flags, or re-check code; these files are bounded snapshots.
"""
import hashlib
import hmac
import json
import re
import unicodedata

from . import memlet_wiki as mw, memlet_bridge as mb, memlet_dream as md
from .vocabulary import (APPROVED, BRIDGE_RULE, BRIDGE_SUGGESTED_ANCHOR, BRIDGE_RELATIONS,
                         BRIDGE_BLOCKED_REASON, BRIDGE_REPLACED_REASON, BRIDGE_TRUNCATED,
                         BRIDGE_RULE_STATES, LESSON_SEPARATOR)

HANDOVER_FILE = ".memlet/handover.json"
CAPSULES_FILE = ".memlet/capsules.json"
HANDOVER_SCHEMA = "memlet.handover_bridge.v1"
CAPSULES_SCHEMA = "memlet.capsules.v1"
# Reader L30, L64-73, L483-490: both files <=1 MiB. Leave ample headroom.
MAX_BYTES = 128 * 1024
LIST_CAP = 40
# Reader L170, L566: attach normalizes/cuts at 200, compile normalizes/cuts at 120.
SUMMARY_CHARS = 120
_ALLOWED = BRIDGE_RULE_STATES[:2]
# Reader L274-284, L335-356: residual secret forms, also after control removal.
# Memlet masks first; residuals (including short keys and Bearer [REDACTED])
# cause the entry to be omitted, never a partial prefix to be exported.
_SECRET = re.compile(
    r"(?:sk-|sk_|xox[abprs]-)[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|AKIA[0-9A-Z]{8,}"
    r"|Bearer\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?:api[_-]?key|apikey|secret|token|password|passwd|authorization|auth[_-]?(?:token|key|secret))"
    r"[\"']?\s*[:=]\s*[\"']?[^\s\"',;]{12,}", re.I)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def integrity(value):
    """Unkeyed canonical SHA-256, integrity only, not authentication."""
    return hashlib.sha256(_canonical({k: v for k, v in value.items() if k != "integritySha256"}).encode("utf-8")).hexdigest()


def verify(value):
    if not isinstance(value, dict) or value.get("schema_version") != HANDOVER_SCHEMA:
        return False
    expected = value.get("integritySha256")
    return (isinstance(expected, str) and bool(re.fullmatch(r"[a-f0-9]{64}", expected))
            and hmac.compare_digest(expected, integrity(value)))


def _masked(value):
    if isinstance(value, str):
        return mb.safe_lesson_display(md.mask_secrets(value))
    if isinstance(value, list):
        return [_masked(v) for v in value]
    if isinstance(value, dict):
        return {k: _masked(v) for k, v in value.items()}
    return value


def _poisoned(value):
    if isinstance(value, str):
        stripped = ''.join(c for c in value if unicodedata.category(c) != 'Cf')
        bare = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', stripped)
        return any(_SECRET.search(v) or md.has_unmasked_secret_or_canary(v)
                   for v in (value, stripped, bare, ' '.join(stripped.split())))
    if isinstance(value, dict):
        return any(_poisoned(k) or _poisoned(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_poisoned(v) for v in value)
    return False


def _clip(text, limit):
    # Check normalized length too: neither reader may silently cut this again.
    if len(text) <= limit:
        return text, False
    return text[:limit - len(BRIDGE_TRUNCATED)].rstrip() + BRIDGE_TRUNCATED, True


def _id(kind, identity):
    return kind + "-" + hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()[:24]


def _source(cites):
    # Full citations also remain in `cites`. Use a single range per snapshot for
    # the reader's 120-character rejectedApproaches.source (L555).
    groups = {}
    for c in cites:
        a, b = groups.get(c["file"], (c["start"], c["end"]))
        groups[c["file"]] = (min(a, c["start"]), max(b, c["end"]))
    return ", ".join("%s:%d-%d" % (f, a, b) for f, (a, b) in groups.items())


def _paths(root, scope, text):
    # No repository walk and no guessed paths: literal paths named by scope or
    # cited text only, with containment and a regular-file existence check.
    raw = [scope.split("::", 1)[0]] + [p for p, _ in mw._line_refs(text)]
    raw += re.findall(r"(?<![\w/])(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]+", text)
    paths = []
    for path in raw:
        path = path.replace("\\", "/").removeprefix("./")
        if (path and len(path) <= 80 and not mw.contained_path_problem(root, path)
                and (root / path).is_file() and path not in paths):
            paths.append(path)
    return paths[:8]


def _rule_text(root, rule, cache):
    source = rule["source"]
    if source not in cache:
        cache[source] = (mw.read_regular_text(root / source, mw._MAX_SOURCE_BYTES) or "").splitlines() if not mw.contained_path_problem(root, source) else []
    lines = cache[source]
    if not 0 < rule["line"] <= len(lines):
        return None
    line = lines[rule["line"] - 1]
    bullet = mw._BULLET_RE.match(line)
    text = mw.PIN_RE.sub("", bullet.group("text") if bullet else line.strip()).strip()
    # The rule parser bounds its display slice at 300; recover only the exact
    # instruction line whose complete hash was classified for this handover.
    return text if hashlib.sha256(text.encode("utf-8")).hexdigest() == rule["ruleSha256"] else None


def build(root, rules, anchors, approved, head, updated_at):
    """Project classified rules + tier-provider-approved lessons in capsule order."""
    # Reader L493-558: strings repoHead, id/rule/anchor_status, list[str] paths;
    # rejected dicts use id/approach/reason/source; stale dicts anchor/path/reason.
    # L258, L474-475: reader emits its own boundary; keep Memlet's full wording.
    hand = {"schema_version": HANDOVER_SCHEMA, "repoHead": head or "unverified",
            "updated_at": updated_at, "authorityBoundary": mw.AUTHORITY_BOUNDARY,
            "activeConstraints": [], "rejectedApproaches": [], "quarantinedStaleAnchors": [],
            "omitted": {"activeConstraints": 0, "rejectedApproaches": 0, "quarantinedStaleAnchors": 0,
                        "ineligibleRules": 0, "secretEntries": 0}}
    # Reader L50-57, L121-138, L166-171: id, paths, tags, summary, updated_at.
    caps = {"schema_version": CAPSULES_SCHEMA, "capsules": [],
            "omitted": {"capsules": 0, "secretEntries": 0}}

    def admit(document, key, entry, limits):
        masked = _masked(entry)
        # Masking must never turn a citation or a route into a different address.
        address_changed = any(masked.get(k) != entry.get(k) for k in
                              ("id", "paths", "path", "anchor", "source", "cites"))
        entry = masked
        if address_changed or _poisoned(entry):
            document["omitted"]["secretEntries"] += 1
            document["omitted"][key] += 1
            return
        truncated = False
        for field, limit in limits.items():
            entry[field], clipped = _clip(entry[field], limit)
            truncated |= clipped
        entry["truncated"] = truncated
        if len(document[key]) >= LIST_CAP:
            document["omitted"][key] += 1
        else:
            document[key].append(entry)

    by_key = {a["anchor"]: a for a in anchors}
    cache = {}
    for rule in rules:
        # Promoted pages are a rendering of lessons, not another authority path.
        if rule["source"] == mw.DREAM_PROMOTED_PAGE:
            continue
        keys = rule.get("anchors", [])
        if not keys or any(by_key[k]["status"] not in _ALLOWED or not by_key[k].get("symbol") for k in keys):
            hand["omitted"]["ineligibleRules"] += 1
            continue
        paths = list(dict.fromkeys(by_key[k]["path"] for k in keys))
        text = _rule_text(root, rule, cache)
        if text is None or len(paths) > 8 or any(len(p) > 80 for p in paths):
            hand["omitted"]["activeConstraints"] += 1
            caps["omitted"]["capsules"] += 1
            continue
        status = rule["status"]
        tags = [BRIDGE_RULE, status]
        if rule.get("suggestedAnchors"):
            tags.append(BRIDGE_SUGGESTED_ANCHOR)
        ident = _id(BRIDGE_RULE, [rule["source"], rule["ruleSha256"], sorted(keys), rule.get("sidecarIds", {})])
        source = "%s:%s" % (rule["source"], rule["line"])
        entry = {"id": ident, "anchor": keys[0], "anchor_status": status, "paths": paths,
                 "rule": text, "source": source, "tags": tags}
        # Reader L521 rule <=140; L566 reference summary <=120. Extra source,
        # tags, truncated are retained on disk but ignored by the reader.
        admit(hand, "activeConstraints", entry, {"rule": 140})
        admit(caps, "capsules", {"id": ident, "paths": paths, "tags": tags, "summary": text,
              "updated_at": updated_at, "source": source}, {"summary": SUMMARY_CHARS})
    for anchor in anchors:
        if anchor["status"] != "stale":
            continue
        tags = [BRIDGE_RULE, "stale"]
        if anchor.get("suggested"):
            tags.append(BRIDGE_SUGGESTED_ANCHOR)
        admit(hand, "quarantinedStaleAnchors", {"anchor": anchor["anchor"], "path": anchor["path"],
              "status": "stale", "reason": anchor["reason"], "tags": tags},
              {"anchor": 80, "path": 80, "reason": 90})

    for lesson in approved:
        if lesson.get("tier") != APPROVED:
            continue
        quotes, cites = lesson.get("quoteLines", []), lesson.get("cites", [])
        if not quotes or len(quotes) != len(cites) or any(not c.get("verified") for c in cites):
            caps["omitted"]["capsules"] += 1
            continue
        relations = [r for r in BRIDGE_RELATIONS if r in lesson.get("edgeTypes", [])]
        roles = lesson.get("lineRoles", ["current"] * len(quotes))
        # Fail closed on an unlabelled supersession; never export an old quote
        # as current just because a legacy projection omitted its role.
        if "SUPERSEDES" in relations and (not lesson.get("quarantined") or len(roles) != len(quotes)):
            caps["omitted"]["capsules"] += 1
            continue
        groups = [(False, [i for i, role in enumerate(roles) if role != "replaced"]),
                  (True, [i for i, role in enumerate(roles) if role == "replaced"])]
        for replaced, indices in groups:
            if not indices:
                continue
            if not replaced and lesson.get("replacementLabel"):
                # An incoming supersession whose source was itself replaced or
                # dropped is displayed for re-verification, never for applying.
                caps["omitted"]["capsules"] += 1
                continue
            selected = [cites[i] for i in indices]
            text = LESSON_SEPARATOR.join(quotes[i] for i in indices)
            source = _source(selected)
            ident = lesson["id"] + ("-old" if replaced else "")
            status_entry = {"quote": text, "source": source}
            mw.classify_rule_quotes(root, [status_entry])
            tags = relations + [APPROVED, status_entry["anchorStatus"]]
            cite_fields = [{k: c[k] for k in ("file", "start", "end", "sha256")} for c in selected]
            if replaced or "BLOCKS" in relations:
                # Reader L384-387 renders these as "DO NOT REPEAT", never active.
                admit(hand, "rejectedApproaches", {"id": ident, "approach": text,
                      "reason": BRIDGE_REPLACED_REASON if replaced else BRIDGE_BLOCKED_REASON,
                      "source": source, "cites": cite_fields, "tags": tags}, {"approach": 110, "source": 120})
            elif relations:
                admit(caps, "capsules", {"id": ident, "paths": _paths(root, lesson.get("scope", ""), text),
                      "tags": tags, "summary": text, "source": source, "cites": cite_fields,
                      "updated_at": updated_at}, {"summary": SUMMARY_CHARS})

    # Bound encoded bytes independently of text length (Unicode and paths count).
    for document, lists in ((hand, ("activeConstraints", "rejectedApproaches", "quarantinedStaleAnchors")),
                            (caps, ("capsules",))):
        while len((_canonical(document) + "\n").encode("utf-8")) + 100 > MAX_BYTES:
            key = next((k for k in lists if document[k]), None)
            if key is None:
                raise ValueError("bridge metadata exceeds byte limit")
            document[key].pop()
            document["omitted"][key] += 1
        document["integritySha256"] = integrity(document)
    return {HANDOVER_FILE: _canonical(hand) + "\n", CAPSULES_FILE: _canonical(caps) + "\n"}


def write(root, rel, text):
    if rel not in (HANDOVER_FILE, CAPSULES_FILE):
        raise mw.UnsafePathError("not a managed handover bridge file")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("bridge file exceeds byte limit: " + rel)
    return mw.write_managed_text(root, rel, text)
