"""Memlet Dream (`spec-012` rev3): Verbatim Quote-Pinned Session Consolidation & 4-Edge Ontology Engine.

Consolidates raw multi-vendor AI coding session transcripts into a verified, zero-daemon ontology graph
with 4 typed edges (`GOVERNS`, `SUPERSEDES`, `RESOLVES`, `BLOCKS`), deterministic SHA-256 quote pins,
dual-pin + chronological + DAG acyclicity enforcement on `SUPERSEDES`, 1-hop subgraph recall,
and Cortex lineage visualization payload (`memlet.cortex_lineage.v1`).

Design invariants:
- Never modifies live `.memlet/` ledgers or instruction files (`CLAUDE.md`, `AGENTS.md`).
- Scrubs secrets and synthetic canary tokens BEFORE any substring truncation.
- Enforces deterministic quote pins (`file_id:start-end` + `sha256(verbatim_lines)`).
- Nothing is promoted automatically: built nodes/edges are `PENDING` until a human approves them
  (`approve_queue_items`), and `run_dream_recall` re-verifies pins against the snapshot by default.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from prism_memory.memlet_wiki import estimate_tokens

DREAM_ONTOLOGY_SCHEMA = "memlet.dream_ontology.v1"
CORTEX_LINEAGE_SCHEMA = "memlet.cortex_lineage.v1"

EDGE_TYPES = ("GOVERNS", "SUPERSEDES", "RESOLVES", "BLOCKS")
CATEGORIES = (
    "repeated_pitfall",
    "verified_workaround",
    "superseded_directive",
    "cross_session_conflict",
)
STATUS_KINDS = ("active_lesson", "superseded_rule", "open_trap")
# `PENDING` is the default for every node/edge written by `run_dream_build`; only an explicit human
# decision via `approve_queue_items` / `reject_queue_items` moves an item out of it.
PENDING = "PENDING"
REJECTED = "REJECTED"
VERDICTS = (
    "SUPPORTED",
    "MISSING_CONDITION",
    "OUT_OF_EVIDENCE",
    "ALREADY_SUPERSEDED",
    "UNVERIFIED",
    PENDING,
    REJECTED,
)
ALLOWED_RECALL_VERDICTS = ("SUPPORTED", "UNVERIFIED", "ALREADY_SUPERSEDED")
EDGE_VERDICTS = ("VALID", "WRONG_EDGE_TYPE_OR_DIR", "UNVERIFIED", PENDING, REJECTED)
ALLOWED_EDGE_VERDICTS = ("VALID", "UNVERIFIED")
AUTO_CANDIDATE_CAP = 24
RESERVED_ID_PREFIXES = ("RULE:", "SCOPE:", "BLOCKS:", "RESOLVES:", "GOVERNS:", "SUPERSEDES:")

CANARY_SECRETS: Tuple[str, ...] = (
    "sk-ant-CANARY-SPEC012-9918273645-DO-NOT-LEAK",
    "sk-proj-CANARY-SPEC012-5544332211-DO-NOT-LEAK",
    "ghp_CANARYSPEC012TOKEN8877665544332211",
    "MEMLET_CANARY_SECRET_KEY=canary_val_spec012_xyz987",
    "xoxb-CANARY-SPEC012-1122334455-DO-NOT-LEAK",
)

# Expanded secret patterns covering PEM private keys, OpenAI/Anthropic/GitHub/Slack/AWS/Google keys, JWTs, and key=value secrets
_PEM_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 _-]+PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 _-]+PRIVATE KEY-----"
)
_SECRET_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z0-9 _-]+PRIVATE KEY-----"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{12,}"),
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password|passwd|authorization)[ \t]*[:=][ \t]*['\"]?[^\s'\"]{8,}"),
    re.compile(r"CANARY[-_]SPEC012", re.IGNORECASE),
)

REDACTION_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("OPENAI_API_KEY", re.compile(r"sk-(?!ant-)[A-Za-z0-9_-]{16,}")),
    ("ANTHROPIC_API_KEY", re.compile(r"sk-ant-[A-Za-z0-9_-]{12,}")),
    ("GITHUB_PAT", re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("AWS_SECRET_KEY", re.compile(r"(?:(?:AKIA|ASIA)[0-9A-Z]{16}|(?i:aws[_-]?secret[_-]?[a-z0-9_]*[ \t]*[:=][ \t]*['\"]?[^\s'\"]{8,}))")),
    ("BEARER_JWT", re.compile(r"(?:eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|xox[baprs]-[A-Za-z0-9-]{12,})")),
)

# file_id may be a nested relative path (`dir/sub/file.log`); absolute paths, `..`, `.`/empty
# segments and backslashes are rejected in `parse_span`.
_SPAN_RE = re.compile(r"^([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*):(\d+)(?:-(\d+))?$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
# Anchored to the start of the line so embedded timestamps in message bodies cannot forge ordering
_LINE_TS_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")


def sanitize_id(raw_id: Any, fallback: str) -> str:
    """Sanitize a node or edge identifier to match `_ID_RE` (`^[A-Za-z0-9_.:-]{1,64}$`)."""
    s = str(raw_id or "").strip()
    s = re.sub(r"[^A-Za-z0-9_.:-]", "_", s)[:64]
    if not s or not _ID_RE.match(s):
        return fallback
    return s


def sanitize_node_id(raw_id: Any, fallback: str = "L-000") -> str:
    """Sanitize a lesson node ID (max 48 chars so `E-{lid}-SUP` fits <=64), avoid long-ID truncation collisions, and forbid reserved prefixes."""
    raw_str = str(raw_id or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]", "_", raw_str)
    if len(cleaned) > 48:
        h6 = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:6]
        s = f"{cleaned[:41]}-{h6}"
    else:
        s = sanitize_id(raw_id, fallback)[:48]
    up = s.upper()
    if any(up.startswith(pfx) for pfx in RESERVED_ID_PREFIXES) or up.endswith(":NEWER"):
        re_s = f"L_{s.replace(':', '_')}"
        if len(re_s) > 48:
            h6 = hashlib.sha256(re_s.encode("utf-8")).hexdigest()[:6]
            s = sanitize_id(f"{re_s[:41]}-{h6}", fallback)[:48]
        else:
            s = sanitize_id(re_s, fallback)[:48]
    return s or fallback[:48]


def mask_secrets(text: str) -> str:
    """Mask synthetic canary strings, PEM private keys, and secret patterns BEFORE any truncation or storage."""
    if not text:
        return ""
    out = _PEM_BLOCK_RE.sub("[REDACTED_SECRET]", text)
    for c in CANARY_SECRETS:
        if c in out:
            out = out.replace(c, "[REDACTED_CANARY]")
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED_SECRET]", out)
    return out


def has_unmasked_secret_or_canary(text: str) -> bool:
    """Return True if any canary substring or secret pattern remains unmasked in `text`."""
    if not text:
        return False
    for c in CANARY_SECRETS:
        if c in text:
            return True
    cleaned = text.replace("[REDACTED_SECRET]", "").replace("[REDACTED_CANARY]", "")
    for pat in _SECRET_PATTERNS:
        if pat.search(cleaned):
            return True
    return False


def _iter_strings(obj: Any) -> Iterator[str]:
    """Recursively yield all raw string keys and values inside a nested dict/list structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            yield from _iter_strings(item)


def safe_truncate_masked(text: str, max_chars: int = 240) -> str:
    """Mask secrets/canaries FIRST, then truncate cleanly without splitting redaction markers."""
    masked = mask_secrets(text)
    if len(masked) <= max_chars:
        return masked
    cut = masked[:max_chars]
    last_open = cut.rfind("[REDACTED_")
    if last_open != -1 and "]" not in cut[last_open:]:
        cut = cut[:last_open].rstrip()
    return cut + "…"


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_safe_file_id(file_id: str) -> bool:
    """A snapshot file id is a relative POSIX path: no absolute root, no `..`, no `.`/empty segments, no backslash."""
    if not file_id or not isinstance(file_id, str):
        return False
    if ".." in file_id or "\\" in file_id or file_id.startswith("/") or ":" in file_id:
        return False
    return all(seg not in ("", ".") for seg in file_id.split("/"))


def parse_span(span_str: str) -> Optional[Tuple[str, int, int]]:
    """Parse `file_id:start-end` or `file_id:line` into `(file_id, start_line, end_line)` (1-indexed)."""
    if not isinstance(span_str, str):
        return None
    m = _SPAN_RE.match(span_str.strip())
    if not m:
        return None
    file_id = m.group(1)
    if not _is_safe_file_id(file_id):
        return None
    start = int(m.group(2))
    end = int(m.group(3)) if m.group(3) else start
    if start < 1 or end < start:
        return None
    return file_id, start, end


def resolve_pin(
    snapshot_files: Dict[str, List[str]],
    span_str: str,
    max_lines: int = 4,
) -> Optional[Dict[str, Any]]:
    """Deterministically resolve a `file_id:start-end` span against `snapshot_files` (`{file_id: [lines]}`).
    Returns None if file_id or line numbers are out of range or exceed `max_lines`.
    """
    if not isinstance(snapshot_files, dict) or not isinstance(span_str, str):
        return None
    parsed = parse_span(span_str)
    if not parsed:
        return None
    file_id, start, end = parsed
    if end - start + 1 > max_lines:
        return None
    lines = snapshot_files.get(file_id)
    if not lines or end > len(lines):
        return None
    raw_slice = [ln.rstrip("\r\n") for ln in lines[start - 1 : end]]
    joined = mask_secrets("\n".join(raw_slice))
    masked_slice = [mask_secrets(ln) for ln in joined.split("\n")]
    joined = "\n".join(masked_slice)
    if not joined.strip():
        return None
    canonical_span = f"{file_id}:{start}-{end}" if end > start else f"{file_id}:{start}"
    return {
        "span": canonical_span,
        "fileId": file_id,
        "startLine": start,
        "endLine": end,
        "quoteLines": masked_slice,
        "quoteText": joined,
        "sha256": sha256_hex(joined),
    }


def extract_line_timestamp(text: str) -> str:
    """Extract leading ISO-8601 timestamp from the start of a pinned line if present."""
    if not text:
        return ""
    first_line = text.splitlines()[0] if text.splitlines() else text
    m = _LINE_TS_RE.match(first_line)
    return m.group(1) if m else ""


_ISO_PARTS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|z|[+-]\d{2}:?\d{2})?$"
)


def _normalize_iso_ts(s: str) -> str:
    """Rewrite every form `_LINE_TS_RE` accepts into one `datetime.fromisoformat` accepts on Python 3.9/3.10:
    `Z` -> `+00:00`, `+0900` -> `+09:00`, and any number of fraction digits -> exactly 6 (truncated, not rounded).
    """
    m = _ISO_PARTS_RE.match(s)
    if not m:
        # Not a pinned-line form; keep the previous behaviour (fromisoformat decides).
        return s[:-1] + "+00:00" if s.endswith("Z") else s
    date_part, time_part, frac, tz = m.groups()
    out = f"{date_part}T{time_part}"
    if frac:
        out += "." + (frac + "000000")[:6]
    if tz:
        if tz in ("Z", "z"):
            tz = "+00:00"
        elif ":" not in tz:
            tz = f"{tz[:3]}:{tz[3:]}"
        out += tz
    return out


def parse_iso_dt(ts_str: str) -> Optional[datetime.datetime]:
    """Parse an ISO-8601 timestamp string into a timezone-aware UTC `datetime` for accurate comparison."""
    if not ts_str or not isinstance(ts_str, str):
        return None
    s = _normalize_iso_ts(ts_str.strip())
    try:
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except ValueError:
        return None


def _has_cycle(edges: Sequence[Dict[str, Any]]) -> bool:
    """Iterative Kahn's topological sort cycle detector over `SUPERSEDES` edges (`fromId -> toId`)."""
    adj: Dict[str, Set[str]] = {}
    in_deg: Dict[str, int] = {}
    nodes: Set[str] = set()
    for e in edges:
        if not isinstance(e, dict) or e.get("edgeType") != "SUPERSEDES":
            continue
        u = str(e.get("fromId") or "")
        v = str(e.get("toId") or "")
        if not u or not v:
            continue
        if u == v:
            return True
        nodes.add(u)
        nodes.add(v)
        if v not in adj.setdefault(u, set()):
            adj[u].add(v)
            in_deg[v] = in_deg.get(v, 0) + 1
            in_deg.setdefault(u, 0)
    queue = [n for n in nodes if in_deg.get(n, 0) == 0]
    visited = 0
    while queue:
        curr = queue.pop()
        visited += 1
        for nxt in adj.get(curr, ()):
            in_deg[nxt] -= 1
            if in_deg[nxt] == 0:
                queue.append(nxt)
    return visited < len(nodes)


def _claimed(val: Any) -> Optional[str]:
    """Record a proposer's self-reported verdict as inert, masked text (audit trail only)."""
    if val is None or isinstance(val, (dict, list)):
        return None
    s = safe_truncate_masked(str(val).strip(), 32)
    return s or None


def dream_pin_and_validate(
    candidates: Sequence[Dict[str, Any]],
    snapshot_files: Dict[str, List[str]],
    vendor_origin: str = "unknown",
) -> Dict[str, Any]:
    """Deterministic `dream pin` + ontology edge & DAG validator (LLM 0).

    Takes raw LLM candidate lessons/edges, resolves verbatim line quotes and SHA-256 pins against
    `snapshot_files`, enforces distinct dual pins and parsed ISO-8601 chronological order on `SUPERSEDES`,
    validates cross-node `SUPERSEDES` pin hashes, drops unpinned or cyclic items, and returns the pinned graph.
    """
    clean_vendor = safe_truncate_masked(str(vendor_origin or "unknown"), 64)
    pinned_nodes: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    seen_edge_ids: Set[str] = set()

    for idx, cand in enumerate(candidates or []):
        if not isinstance(cand, dict):
            dropped.append({"index": idx, "reason": "not_dict"})
            continue
        base_lid = sanitize_node_id(cand.get("lesson_id") or cand.get("id"), f"L-{idx + 1:03d}")
        lid = base_lid
        suffix = 1
        while lid in seen_ids:
            suffix += 1
            sfx = f"-{suffix}"
            lid = sanitize_node_id(f"{base_lid[:48 - len(sfx)]}{sfx}", f"L-{idx + 1:03d}-{suffix}")

        cat = str(cand.get("category") or "repeated_pitfall").strip()
        if cat not in CATEGORIES:
            cat = "repeated_pitfall"
        status_kind = str(cand.get("status_kind") or "active_lesson").strip()
        if status_kind not in STATUS_KINDS:
            status_kind = "active_lesson"
        if cat == "superseded_directive":
            status_kind = "superseded_rule"

        scope_tag = safe_truncate_masked(str(cand.get("scope_tag") or "general"), max_chars=48)
        title = safe_truncate_masked(str(cand.get("title") or ""), max_chars=110)
        if not title or len(title.split()) > 18:
            title = " ".join(title.split()[:15])
        if not title:
            dropped.append({"lesson_id": lid, "reason": "empty_title"})
            continue

        imp = max(1, min(3, _safe_imp(cand.get("importance"), 1)))

        raw_spans = cand.get("src_spans") if cand.get("src_spans") is not None else cand.get("spans")
        if isinstance(raw_spans, str):
            raw_spans = [raw_spans]
        elif not isinstance(raw_spans, list):
            raw_spans = []

        resolved_pins = []
        for sp in raw_spans[:3]:
            p = resolve_pin(snapshot_files, str(sp), max_lines=4)
            if p:
                resolved_pins.append(p)
        if not resolved_pins:
            dropped.append({"lesson_id": lid, "reason": "dropped_unpinned_src"})
            continue

        primary_pin = resolved_pins[0]
        src_ts = extract_line_timestamp(primary_pin["quoteText"])
        src_dt = parse_iso_dt(src_ts)

        raw_edges = cand.get("edges") if isinstance(cand.get("edges"), list) else []
        valid_node_edges: List[Dict[str, Any]] = []
        superseded_by_pin: Optional[Dict[str, Any]] = None

        if status_kind == "superseded_rule" or cand.get("superseded_by_src"):
            status_kind = "superseded_rule"
            sup_span = str(cand.get("superseded_by_src") or "")
            sup_pin = resolve_pin(snapshot_files, sup_span, max_lines=4)
            if not sup_pin:
                dropped.append({"lesson_id": lid, "reason": "missing_superseded_by_pin"})
                continue
            if sup_pin["span"] == primary_pin["span"] or sup_pin["sha256"] == primary_pin["sha256"]:
                dropped.append({"lesson_id": lid, "reason": "identical_dual_pin_on_supersedes"})
                continue
            sup_ts = extract_line_timestamp(sup_pin["quoteText"])
            sup_dt = parse_iso_dt(sup_ts)
            if not src_dt or not sup_dt:
                dropped.append({"lesson_id": lid, "reason": "missing_pinned_timestamp_on_supersedes"})
                continue
            if sup_dt < src_dt:
                dropped.append({"lesson_id": lid, "reason": "time_inversion_on_supersedes"})
                continue
            superseded_by_pin = sup_pin
            raw_sup_by = str(cand.get("superseded_by_id") or "").strip()
            newer_id = sanitize_node_id(raw_sup_by, f"{lid}:NEWER") if raw_sup_by else f"{lid}:NEWER"
            if newer_id == lid:
                newer_id = f"{lid}:NEWER"
            eid = f"E-{lid}-SUP"
            seen_edge_ids.add(eid)
            valid_node_edges.append({
                "edgeId": eid,
                "edgeType": "SUPERSEDES",
                "fromId": newer_id,
                "toId": lid,
                "targetLabel": safe_truncate_masked(f"old[{primary_pin['span']}] -> active[{sup_pin['span']}]", 80),
                "srcPin": sup_pin,
                "targetPin": primary_pin,
                "srcTimestamp": sup_ts,
                "targetTimestamp": src_ts,
                "vendorOrigin": clean_vendor,
                "edgeVerdict": "UNVERIFIED",
            })

        edge_aborted = False
        for e_idx, re_obj in enumerate(raw_edges):
            if not isinstance(re_obj, dict):
                continue
            etype = str(re_obj.get("edge_type") or re_obj.get("edgeType") or "").strip().upper()
            if etype not in EDGE_TYPES:
                continue
            target_label = safe_truncate_masked(str(re_obj.get("target") or re_obj.get("to_node") or scope_tag), 80)
            if not target_label:
                continue
            if re_obj.get("src_span"):
                e_src_pin = resolve_pin(snapshot_files, str(re_obj.get("src_span")), max_lines=4)
                if not e_src_pin:
                    continue
            else:
                e_src_pin = primary_pin

            eid = f"E-{lid}-{e_idx + 1:02d}"
            while eid in seen_edge_ids:
                e_idx += 1
                eid = f"E-{lid}-{e_idx + 1:02d}"

            if etype == "SUPERSEDES":
                if status_kind == "superseded_rule":
                    continue
                t_span = str(re_obj.get("target_span") or "")
                t_pin = resolve_pin(snapshot_files, t_span, max_lines=4)
                if not t_pin:
                    dropped.append({"lesson_id": lid, "reason": "supersedes_missing_target_pin"})
                    edge_aborted = True
                    break
                if t_pin["span"] == e_src_pin["span"] or t_pin["sha256"] == e_src_pin["sha256"]:
                    dropped.append({"lesson_id": lid, "reason": "supersedes_identical_pins"})
                    edge_aborted = True
                    break
                e_ts = extract_line_timestamp(e_src_pin["quoteText"])
                t_ts = extract_line_timestamp(t_pin["quoteText"])
                e_dt = parse_iso_dt(e_ts)
                t_dt = parse_iso_dt(t_ts)
                if not e_dt or not t_dt:
                    dropped.append({"lesson_id": lid, "reason": "supersedes_missing_pinned_timestamp"})
                    edge_aborted = True
                    break
                if e_dt < t_dt:
                    dropped.append({"lesson_id": lid, "reason": "supersedes_time_inversion"})
                    edge_aborted = True
                    break
                cleaned_target = re.sub(r"[^A-Za-z0-9_.-]", "_", target_label)[:40]
                raw_to = str(re_obj.get("to_id") or "").strip()
                if raw_to and not any(raw_to.upper().startswith(pfx) for pfx in RESERVED_ID_PREFIXES):
                    to_id = sanitize_node_id(raw_to, f"RULE:{cleaned_target}")
                else:
                    to_id = sanitize_id(raw_to, f"RULE:{cleaned_target}")[:48]
                if to_id == lid:
                    dropped.append({"lesson_id": lid, "reason": "supersedes_self_loop"})
                    edge_aborted = True
                    break
                seen_edge_ids.add(eid)
                valid_node_edges.append({
                    "edgeId": eid,
                    "edgeType": "SUPERSEDES",
                    "fromId": lid,
                    "toId": to_id,
                    "targetLabel": target_label,
                    "srcPin": e_src_pin,
                    "targetPin": t_pin,
                    "srcTimestamp": e_ts,
                    "targetTimestamp": t_ts,
                    "vendorOrigin": clean_vendor,
                    "edgeVerdict": "UNVERIFIED",
                })
            else:
                safe_t_id = re.sub(r"[^A-Za-z0-9_.-]", "_", target_label)[:40] or "scope"
                seen_edge_ids.add(eid)
                valid_node_edges.append({
                    "edgeId": eid,
                    "edgeType": etype,
                    "fromId": lid,
                    "toId": f"{etype}:{safe_t_id}",
                    "targetLabel": target_label,
                    "srcPin": e_src_pin,
                    "targetPin": None,
                    "srcTimestamp": extract_line_timestamp(e_src_pin["quoteText"]),
                    "targetTimestamp": "",
                    "vendorOrigin": clean_vendor,
                    "edgeVerdict": "UNVERIFIED",
                })

        if edge_aborted:
            continue

        if not valid_node_edges:
            default_etype = {
                "repeated_pitfall": "BLOCKS",
                "verified_workaround": "RESOLVES",
                "superseded_directive": "SUPERSEDES",
                "cross_session_conflict": "GOVERNS",
            }.get(cat, "GOVERNS")
            if default_etype == "SUPERSEDES":
                dropped.append({"lesson_id": lid, "reason": "supersedes_without_dual_pin"})
                continue
            safe_s_id = re.sub(r"[^A-Za-z0-9_.-]", "_", scope_tag)[:40] or "scope"
            eid = f"E-{lid}-01"
            seen_edge_ids.add(eid)
            valid_node_edges.append({
                "edgeId": eid,
                "edgeType": default_etype,
                "fromId": lid,
                "toId": f"{default_etype}:{safe_s_id}",
                "targetLabel": scope_tag,
                "srcPin": primary_pin,
                "targetPin": None,
                "srcTimestamp": src_ts,
                "targetTimestamp": "",
                "vendorOrigin": clean_vendor,
                "edgeVerdict": "UNVERIFIED",
            })

        if status_kind != "superseded_rule" and not any(e["edgeType"] == "GOVERNS" for e in valid_node_edges):
            safe_s_id = re.sub(r"[^A-Za-z0-9_.-]", "_", scope_tag)[:40] or "scope"
            eid = f"E-{lid}-GOV"
            seen_edge_ids.add(eid)
            valid_node_edges.append({
                "edgeId": eid,
                "edgeType": "GOVERNS",
                "fromId": lid,
                "toId": f"SCOPE:{safe_s_id}",
                "targetLabel": scope_tag,
                "srcPin": primary_pin,
                "targetPin": None,
                "srcTimestamp": src_ts,
                "targetTimestamp": "",
                "vendorOrigin": clean_vendor,
                "edgeVerdict": "UNVERIFIED",
            })

        seen_ids.add(lid)
        # Never trust self-reported verification from candidate proposer; always initialize UNVERIFIED.
        # The proposer's claim is kept for audit only (`claimedVerdict`) and is never read as a verdict.
        node_obj = {
            "id": lid,
            "category": cat,
            "statusKind": status_kind,
            "scopeTag": scope_tag,
            "title": title,
            "importance": imp,
            "vendorOrigins": [clean_vendor],
            "primaryPin": primary_pin,
            "allPins": resolved_pins,
            "supersededByPin": superseded_by_pin,
            "verifiedBy": "",
            "verdict": "UNVERIFIED",
            "edgeVerdict": "UNVERIFIED",
            "claimedVerdict": _claimed(cand.get("verdict")),
            "claimedEdgeVerdict": _claimed(cand.get("edgeVerdict") or cand.get("edge_verdict")),
            "timestamp": src_ts,
            "edges": valid_node_edges,
        }
        pinned_nodes.append(node_obj)

    # Second pass: validate cross-node SUPERSEDES pin hashes and global DAG acyclicity
    node_pin_hashes: Dict[str, Set[str]] = {
        n["id"]: {p.get("sha256", "") for p in (n.get("allPins") or []) if isinstance(p, dict)}
        for n in pinned_nodes
    }
    final_nodes: List[Dict[str, Any]] = []
    final_edges: List[Dict[str, Any]] = []

    for n in pinned_nodes:
        lid = n["id"]
        checked_edges: List[Dict[str, Any]] = []
        bad_cross_pin = False
        for e in n.get("edges") or []:
            if e.get("edgeType") == "SUPERSEDES":
                to_id = str(e.get("toId") or "")
                from_id = str(e.get("fromId") or "")
                t_sha = (e.get("targetPin") or {}).get("sha256", "")
                s_sha = (e.get("srcPin") or {}).get("sha256", "")
                if to_id in node_pin_hashes and t_sha not in node_pin_hashes[to_id]:
                    bad_cross_pin = True
                    break
                if from_id in node_pin_hashes and s_sha not in node_pin_hashes[from_id]:
                    bad_cross_pin = True
                    break
            checked_edges.append(e)
        if bad_cross_pin or not checked_edges:
            dropped.append({"lesson_id": lid, "reason": "cross_node_supersedes_pin_mismatch"})
            continue
        if _has_cycle(final_edges + checked_edges):
            dropped.append({"lesson_id": lid, "reason": "supersedes_cycle_detected"})
            continue
        n["edges"] = checked_edges
        final_nodes.append(n)
        final_edges.extend(checked_edges)

    return {
        "schema": DREAM_ONTOLOGY_SCHEMA,
        "nodes": final_nodes,
        "edges": final_edges,
        "dropped": dropped,
        "stats": {
            "pinnedNodeCount": len(final_nodes),
            "pinnedEdgeCount": len(final_edges),
            "droppedCount": len(dropped),
        },
    }


def _verify_pin_dict(pin: Any, snapshot_files: Dict[str, List[str]], label: str) -> Optional[str]:
    """Verify every field of a pin dictionary (`span`, `fileId`, `startLine`, `endLine`, `quoteLines`, `quoteText`, `sha256`)."""
    if not isinstance(pin, dict):
        return f"{label}:not_dict"
    span = str(pin.get("span") or "")
    chk = resolve_pin(snapshot_files, span, max_lines=4)
    if not chk:
        return f"{label}:unresolvable_span:{span}"
    if (
        pin.get("span") != chk["span"]
        or pin.get("sha256") != chk["sha256"]
        or pin.get("quoteText") != chk["quoteText"]
        or pin.get("quoteLines") != chk["quoteLines"]
        or pin.get("fileId") != chk["fileId"]
        or pin.get("startLine") != chk["startLine"]
        or pin.get("endLine") != chk["endLine"]
        or "\n".join(pin.get("quoteLines") or []) != chk["quoteText"]
    ):
        return f"{label}:pin_content_or_hash_mismatch:{span}"
    return None


def verify_ontology_contract(
    graph: Dict[str, Any],
    snapshot_files: Dict[str, List[str]],
) -> Dict[str, Any]:
    """Deterministic `oracle_ontology_contract`: verifies 100% byte-identical pins across `primaryPin`,
    `allPins`, `supersededByPin`, and edge pins; zero canary/secret leaks (scanning raw string values);
    distinct dual pins, cross-node pin hash membership, and parsed timestamps on `SUPERSEDES`;
    node-to-graph edge consistency; and zero cycles.
    """
    pin_mismatches: List[str] = []
    canary_leaks: List[str] = []
    cycle_or_time_errors: List[str] = []

    if not isinstance(graph, dict):
        return {
            "ok": False,
            "pinMismatchCount": 1,
            "pinMismatches": ["graph_not_dict"],
            "canaryLeakCount": 0,
            "canaryLeaks": [],
            "cycleOrTimeInversionCount": 0,
            "cycleOrTimeInversions": [],
        }

    if graph.get("schema") != DREAM_ONTOLOGY_SCHEMA:
        pin_mismatches.append(f"invalid_schema:{graph.get('schema')}")

    for s_val in _iter_strings(graph):
        if has_unmasked_secret_or_canary(s_val):
            canary_leaks.append("graph_string_contains_secret_or_canary")
            break

    raw_nodes = graph.get("nodes")
    raw_edges = graph.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        pin_mismatches.append("nodes_or_edges_not_list")
        return {
            "ok": False,
            "pinMismatchCount": len(pin_mismatches),
            "pinMismatches": pin_mismatches,
            "canaryLeakCount": len(canary_leaks),
            "canaryLeaks": canary_leaks,
            "cycleOrTimeInversionCount": 0,
            "cycleOrTimeInversions": [],
        }

    nodes = raw_nodes
    edges = raw_edges
    if _has_cycle(edges):
        cycle_or_time_errors.append("supersedes_cycle_detected")

    expected_graph_edges: List[Dict[str, Any]] = []
    seen_node_ids: Set[str] = set()
    node_pin_hashes: Dict[str, Set[str]] = {}

    for n in nodes:
        if not isinstance(n, dict):
            pin_mismatches.append("node_not_dict")
            continue
        lid = str(n.get("id") or "")
        up_lid = lid.upper()
        if not _ID_RE.match(lid) or any(up_lid.startswith(p) for p in RESERVED_ID_PREFIXES) or up_lid.endswith(":NEWER"):
            pin_mismatches.append(f"{lid}:invalid_or_reserved_node_id")
        if lid in seen_node_ids:
            pin_mismatches.append(f"{lid}:duplicate_node_id")
        seen_node_ids.add(lid)

        if n.get("category") not in CATEGORIES:
            pin_mismatches.append(f"{lid}:invalid_category:{n.get('category')}")
        if n.get("statusKind") not in STATUS_KINDS:
            pin_mismatches.append(f"{lid}:invalid_status_kind:{n.get('statusKind')}")
        if n.get("verdict", "UNVERIFIED") not in VERDICTS:
            pin_mismatches.append(f"{lid}:invalid_verdict:{n.get('verdict')}")
        if n.get("edgeVerdict", "VALID") not in EDGE_VERDICTS:
            pin_mismatches.append(f"{lid}:invalid_edge_verdict:{n.get('edgeVerdict')}")

        pp = n.get("primaryPin")
        err_pp = _verify_pin_dict(pp, snapshot_files, f"{lid}:primaryPin")
        if err_pp:
            pin_mismatches.append(err_pp)
        else:
            real_node_ts = extract_line_timestamp(pp.get("quoteText", ""))
            if n.get("timestamp", "") != real_node_ts:
                pin_mismatches.append(f"{lid}:node_timestamp_mismatch")

        all_pins = n.get("allPins")
        if not isinstance(all_pins, list) or not all_pins:
            pin_mismatches.append(f"{lid}:missing_allPins")
        else:
            if all_pins[0] != pp:
                pin_mismatches.append(f"{lid}:primaryPin_not_equal_allPins_0")
            hashes_set: Set[str] = set()
            for pi, p in enumerate(all_pins):
                err_p = _verify_pin_dict(p, snapshot_files, f"{lid}:allPins[{pi}]")
                if err_p:
                    pin_mismatches.append(err_p)
                elif isinstance(p, dict):
                    hashes_set.add(p.get("sha256", ""))
            node_pin_hashes[lid] = hashes_set

        sp = n.get("supersededByPin")
        if n.get("statusKind") == "superseded_rule" or n.get("category") == "superseded_directive" or sp is not None:
            if not isinstance(sp, dict):
                pin_mismatches.append(f"{lid}:superseded_rule_missing_supersededByPin")
            else:
                err_sp = _verify_pin_dict(sp, snapshot_files, f"{lid}:supersededByPin")
                if err_sp:
                    pin_mismatches.append(err_sp)
                elif isinstance(pp, dict):
                    if sp.get("span") == pp.get("span") or sp.get("sha256") == pp.get("sha256"):
                        pin_mismatches.append(f"{lid}:supersededByPin_identical_to_primaryPin")
                    sp_dt = parse_iso_dt(extract_line_timestamp(sp.get("quoteText", "")))
                    pp_dt = parse_iso_dt(extract_line_timestamp(pp.get("quoteText", "")))
                    if not sp_dt or not pp_dt:
                        cycle_or_time_errors.append(f"{lid}:missing_timestamp_on_supersededByPin")
                    elif sp_dt < pp_dt:
                        cycle_or_time_errors.append(f"{lid}:time_inversion_on_supersededByPin")
            has_matching_sup_edge = any(
                isinstance(e, dict)
                and e.get("edgeType") == "SUPERSEDES"
                and e.get("toId") == lid
                and e.get("srcPin") == sp
                and e.get("targetPin") == pp
                for e in (n.get("edges") or [])
            )
            if not has_matching_sup_edge:
                pin_mismatches.append(f"{lid}:superseded_rule_missing_bound_SUPERSEDES_edge")

        node_edges = n.get("edges")
        if not isinstance(node_edges, list) or not node_edges:
            pin_mismatches.append(f"{lid}:missing_node_edges")
        else:
            for ne in node_edges:
                if isinstance(ne, dict) and ne.get("fromId") != lid and ne.get("toId") != lid:
                    pin_mismatches.append(f"{lid}:unowned_node_edge:{ne.get('edgeId')}")
            expected_graph_edges.extend(node_edges)

    if expected_graph_edges != edges:
        pin_mismatches.append("graph_edges_mismatch_node_edges")

    seen_edge_ids: Set[str] = set()
    for e in edges:
        if not isinstance(e, dict):
            pin_mismatches.append("edge_not_dict")
            continue
        eid = str(e.get("edgeId") or "")
        if not _ID_RE.match(eid) or eid in seen_edge_ids:
            pin_mismatches.append(f"{eid}:invalid_or_duplicate_edge_id")
        seen_edge_ids.add(eid)

        et = str(e.get("edgeType") or "")
        if et not in EDGE_TYPES:
            pin_mismatches.append(f"{eid}:invalid_edge_type:{et}")
            continue
        if e.get("edgeVerdict", "VALID") not in EDGE_VERDICTS:
            pin_mismatches.append(f"{eid}:invalid_edge_verdict:{e.get('edgeVerdict')}")

        sp = e.get("srcPin")
        err_sp = _verify_pin_dict(sp, snapshot_files, f"{eid}:srcPin")
        if err_sp:
            pin_mismatches.append(err_sp)
        else:
            real_s_ts = extract_line_timestamp(sp.get("quoteText", ""))
            if e.get("srcTimestamp", "") != real_s_ts:
                pin_mismatches.append(f"{eid}:srcTimestamp_mismatch")

        if et == "SUPERSEDES":
            tp = e.get("targetPin")
            err_tp = _verify_pin_dict(tp, snapshot_files, f"{eid}:targetPin")
            if err_tp:
                pin_mismatches.append(err_tp)
            elif isinstance(sp, dict) and isinstance(tp, dict):
                if sp.get("span") == tp.get("span") or sp.get("sha256") == tp.get("sha256"):
                    pin_mismatches.append(f"{eid}:supersedes_identical_pins")
                real_s_ts = extract_line_timestamp(sp.get("quoteText", ""))
                real_t_ts = extract_line_timestamp(tp.get("quoteText", ""))
                if e.get("targetTimestamp", "") != real_t_ts:
                    pin_mismatches.append(f"{eid}:targetTimestamp_mismatch")
                s_dt = parse_iso_dt(real_s_ts)
                t_dt = parse_iso_dt(real_t_ts)
                if not s_dt or not t_dt:
                    cycle_or_time_errors.append(f"{eid}:missing_pinned_timestamp")
                elif s_dt < t_dt:
                    cycle_or_time_errors.append(f"{eid}:time_inversion:{real_s_ts}<{real_t_ts}")
                to_id = str(e.get("toId") or "")
                from_id = str(e.get("fromId") or "")
                if to_id in node_pin_hashes and tp.get("sha256") not in node_pin_hashes[to_id]:
                    pin_mismatches.append(f"{eid}:targetPin_not_in_toId_allPins:{to_id}")
                if from_id in node_pin_hashes and sp.get("sha256") not in node_pin_hashes[from_id]:
                    pin_mismatches.append(f"{eid}:srcPin_not_in_fromId_allPins:{from_id}")
        else:
            if e.get("targetPin") is not None:
                pin_mismatches.append(f"{eid}:non_supersedes_has_targetPin")

    ok = (len(pin_mismatches) == 0 and len(canary_leaks) == 0 and len(cycle_or_time_errors) == 0)
    return {
        "ok": ok,
        "pinMismatchCount": len(pin_mismatches),
        "pinMismatches": pin_mismatches,
        "canaryLeakCount": len(canary_leaks),
        "canaryLeaks": canary_leaks,
        "cycleOrTimeInversionCount": len(cycle_or_time_errors),
        "cycleOrTimeInversions": cycle_or_time_errors,
    }


def _check_graph_safe_structure(graph: Dict[str, Any]) -> None:
    """Fast structural & secret/canary guard run before SQLite write, recall, or Cortex payload build."""
    if not isinstance(graph, dict):
        raise ValueError("Ontology graph must be a dictionary.")
    if not isinstance(graph.get("nodes", []), list) or not isinstance(graph.get("edges", []), list):
        raise ValueError("Ontology graph nodes and edges must be lists.")
    for s_val in _iter_strings(graph):
        if has_unmasked_secret_or_canary(s_val):
            raise ValueError("Refusing to process ontology graph containing unmasked secret or canary.")
    if _has_cycle(graph.get("edges") or []):
        raise ValueError("Refusing to process ontology graph with a SUPERSEDES cycle.")


_SQLITE_ADDED_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "ontology_nodes": ("edge_verdict", "claimed_verdict", "decision", "decided_by", "decided_at"),
    "ontology_edges": ("edge_verdict", "decision", "decided_by", "decided_at"),
}


def write_ontology_sqlite(
    db_path: Path,
    graph: Dict[str, Any],
    snapshot_files: Optional[Dict[str, List[str]]] = None,
) -> None:
    """Persist verified ontology nodes and typed edges into a zero-daemon SQLite database."""
    _check_graph_safe_structure(graph)
    if snapshot_files is not None:
        ver = verify_ontology_contract(graph, snapshot_files)
        if not ver["ok"]:
            raise ValueError(f"Ontology contract check failed before SQLite write: {ver}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        with conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS ontology_nodes (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    status_kind TEXT NOT NULL,
                    scope_tag TEXT NOT NULL,
                    title TEXT NOT NULL,
                    importance INTEGER NOT NULL,
                    vendor_origins TEXT NOT NULL,
                    primary_span TEXT NOT NULL,
                    quote_text TEXT NOT NULL,
                    sha256_pin TEXT NOT NULL,
                    superseded_by_span TEXT,
                    superseded_by_sha256 TEXT,
                    verified_by TEXT,
                    verdict TEXT,
                    timestamp TEXT,
                    edge_verdict TEXT,
                    claimed_verdict TEXT,
                    decision TEXT,
                    decided_by TEXT,
                    decided_at TEXT
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS ontology_edges (
                    edge_id TEXT PRIMARY KEY,
                    edge_type TEXT NOT NULL,
                    from_id TEXT NOT NULL,
                    to_id TEXT NOT NULL,
                    target_label TEXT NOT NULL,
                    src_span TEXT NOT NULL,
                    src_sha256 TEXT NOT NULL,
                    target_span TEXT,
                    target_sha256 TEXT,
                    vendor_origin TEXT,
                    edge_verdict TEXT,
                    decision TEXT,
                    decided_by TEXT,
                    decided_at TEXT
                )"""
            )
            # Databases written before the approval columns existed are migrated in place.
            for table, cols in _SQLITE_ADDED_COLUMNS.items():
                have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                for col in cols:
                    if col not in have:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS dream_runs (
                    run_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    nodes_count INTEGER NOT NULL,
                    edges_count INTEGER NOT NULL,
                    dropped_count INTEGER NOT NULL,
                    graph_sha256 TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                )"""
            )
            conn.execute("DELETE FROM ontology_nodes")
            conn.execute("DELETE FROM ontology_edges")
            conn.execute("DELETE FROM dream_runs")
            graph_bytes = json.dumps(graph, sort_keys=True, ensure_ascii=False).encode("utf-8")
            graph_sha = hashlib.sha256(graph_bytes).hexdigest()
            conn.execute(
                """INSERT INTO dream_runs (
                    run_id, schema_version, nodes_count, edges_count, dropped_count, graph_sha256, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"RUN-{graph_sha[:12]}",
                    str(graph.get("schema") or DREAM_ONTOLOGY_SCHEMA),
                    len(graph.get("nodes") or []),
                    len(graph.get("edges") or []),
                    len(graph.get("dropped") or []),
                    graph_sha,
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ),
            )
            for n in graph.get("nodes") or []:
                pp = n.get("primaryPin") or {}
                sp = n.get("supersededByPin") or {}
                n_dec = n.get("approval") if isinstance(n.get("approval"), dict) else {}
                conn.execute(
                    """INSERT INTO ontology_nodes (
                        id, category, status_kind, scope_tag, title, importance,
                        vendor_origins, primary_span, quote_text, sha256_pin,
                        superseded_by_span, superseded_by_sha256, verified_by, verdict, timestamp,
                        edge_verdict, claimed_verdict, decision, decided_by, decided_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        n["id"],
                        n["category"],
                        n["statusKind"],
                        n["scopeTag"],
                        n["title"],
                        int(n.get("importance") or 1),
                        json.dumps(n.get("vendorOrigins") or []),
                        pp.get("span", ""),
                        pp.get("quoteText", ""),
                        pp.get("sha256", ""),
                        sp.get("span") if sp else None,
                        sp.get("sha256") if sp else None,
                        n.get("verifiedBy", ""),
                        n.get("verdict", "UNVERIFIED"),
                        n.get("timestamp", ""),
                        n.get("edgeVerdict"),
                        n.get("claimedVerdict"),
                        n_dec.get("decision"),
                        n_dec.get("by"),
                        n_dec.get("at"),
                    ),
                )
            for e in graph.get("edges") or []:
                sp = e.get("srcPin") or {}
                tp = e.get("targetPin") or {}
                e_dec = e.get("approval") if isinstance(e.get("approval"), dict) else {}
                conn.execute(
                    """INSERT INTO ontology_edges (
                        edge_id, edge_type, from_id, to_id, target_label,
                        src_span, src_sha256, target_span, target_sha256, vendor_origin,
                        edge_verdict, decision, decided_by, decided_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        e["edgeId"],
                        e["edgeType"],
                        e["fromId"],
                        e["toId"],
                        e.get("targetLabel", ""),
                        sp.get("span", ""),
                        sp.get("sha256", ""),
                        tp.get("span") if tp else None,
                        tp.get("sha256") if tp else None,
                        e.get("vendorOrigin", ""),
                        e.get("edgeVerdict"),
                        e_dec.get("decision"),
                        e_dec.get("by"),
                        e_dec.get("at"),
                    ),
                )


def _safe_imp(val: Any, default: int = 2) -> int:
    if isinstance(val, bool) or not isinstance(val, (int, float, str)):
        return default
    try:
        return max(1, min(3, int(val)))
    except (ValueError, TypeError, OverflowError):
        return default


# Generic quote-window lengths (characters). One value per role; no per-content tuning.
_FLAT_QUOTE_CHARS = 120
_RECALL_QUOTE_CHARS = 96
_RECALL_REPLACEMENT_QUOTE_CHARS = 120

# Generic English function words ignored by query relevance scoring.
_QUERY_STOPWORDS = frozenset({
    "the", "and", "for", "that", "this", "with", "from", "what", "which", "when", "where", "how",
    "why", "who", "should", "must", "can", "may", "will", "would", "could", "are", "was", "were",
    "been", "have", "has", "had", "does", "did", "not", "into", "onto", "upon", "before", "after",
    "during", "while", "under", "over", "between", "through", "about", "against", "within", "without",
    "each", "every", "any", "all", "both", "some", "such", "other", "another", "than", "then", "also",
    "only", "just", "more", "most", "less", "least", "very", "same", "once", "even", "though",
    "use", "used", "uses", "using", "instead",
})


def _query_terms(text: str, with_stems: bool = False) -> Set[str]:
    """Lowercase word tokens for relevance scoring: >=3 chars, or >=2 chars containing a digit (e.g. `p2`)."""
    out: Set[str] = set()
    for t in re.findall(r"[a-z0-9_%-]+", (text or "").lower()):
        c = t.strip("-")
        if not c or c in _QUERY_STOPWORDS:
            continue
        if len(c) >= 3 or (len(c) == 2 and any(ch.isdigit() for ch in c)):
            out.add(c)
            if "-" in c:
                out.add(c.replace("-", ""))
            if with_stems and len(c) >= 6:
                out.add(c[:5])
    return out


def format_flat_memory(graph: Dict[str, Any], budget_tokens: int = 1500) -> str:
    """Format a flat importance-ordered list of verified lessons without 1-hop ontology edge
    grouping. Includes replacement pins on superseded rules so flat readers see active replacement evidence.
    """
    _check_graph_safe_structure(graph)
    eligible = [
        n for n in (graph.get("nodes") or [])
        if isinstance(n, dict)
        and str(n.get("verdict") or "UNVERIFIED").upper() in ALLOWED_RECALL_VERDICTS
        and str(n.get("edgeVerdict") or "VALID").upper() in ALLOWED_EDGE_VERDICTS
    ]
    def _flat_cat_rank(n_obj: Dict[str, Any]) -> int:
        c_val = str(n_obj.get("category") or "")
        if c_val == "superseded_directive" or isinstance(n_obj.get("supersededByPin"), dict):
            return 0
        return 1

    nodes = sorted(
        eligible,
        key=lambda n: (_safe_imp(n.get("importance"), 2), _flat_cat_rank(n), str(n.get("id") or "")),
    )
    lines = ["# Memlet Dream Verified Lessons (flat)", ""]
    for n in nodes:
        pp = n.get("primaryPin") if isinstance(n.get("primaryPin"), dict) else {}
        sp = n.get("supersededByPin") if isinstance(n.get("supersededByPin"), dict) else {}
        lid = sanitize_node_id(n.get("id"), "L-000")
        cat = str(n.get("category") or "repeated_pitfall")
        if cat not in CATEGORIES:
            cat = "repeated_pitfall"
        scope_lbl = safe_truncate_masked(str(n.get("scopeTag") or "general"), 48)
        title_lbl = safe_truncate_masked(str(n.get("title") or ""), 128)
        clean_q = re.sub(r"\s*\d{4}-\d{2}-\d{2}T[^\s]+\s+\[[^\]]+\]\s*", " ", re.sub(r"\s+", " ", str(pp.get("quoteText") or "")).strip()).strip()
        clean_q = re.sub(r"^TOOL_(?:RESULT|USE\s+\w+):\s*", "", clean_q)
        clean_q = safe_truncate_masked(clean_q, _FLAT_QUOTE_CHARS)
        raw_sp = str(pp.get("span") or "")
        safe_sp = raw_sp if parse_span(raw_sp) else "unpinned"
        safe_sha = re.sub(r"[^0-9a-f]", "", str(pp.get("sha256") or "").lower())[:8] or "none"
        if sp and sp.get("quoteText"):
            clean_sup = re.sub(r"\s*\d{4}-\d{2}-\d{2}T[^\s]+\s+\[[^\]]+\]\s*", " ", re.sub(r"\s+", " ", str(sp.get("quoteText") or "")).strip()).strip()
            clean_sup = safe_truncate_masked(clean_sup, 165)
            raw_sup_sp = str(sp.get("span") or "")
            safe_sup_sp = raw_sup_sp if parse_span(raw_sup_sp) else "unpinned"
            safe_sup_sha = re.sub(r"[^0-9a-f]", "", str(sp.get("sha256") or "").lower())[:12] or "none"
            entry = (
                f"- [{lid}] (superseded_with_active_replacement | {scope_lbl}) {title_lbl}\n"
                f"  old_quote [SUPERSEDED — DO NOT APPLY] [{safe_sp} {safe_sha}]: {clean_q}\n"
                f"  active_replacement_quote [ACTIVE — APPLY THIS] [{safe_sup_sp} {safe_sup_sha[:8]}]: {clean_sup}"
            )
        else:
            entry = (
                f"- [{lid}] ({cat} | {scope_lbl}) {title_lbl}\n"
                f"  quote [{safe_sp} {safe_sha}]: {clean_q}"
            )
        candidate = "\n".join(lines + [entry])
        if estimate_tokens(candidate) > budget_tokens:
            break
        lines.append(entry)
    return "\n".join(lines)


def _assemble_sections(
    header: str,
    sec_blocks: List[str],
    sec_resolves: List[str],
    sec_governs: List[str],
    sec_supersedes: List[str],
) -> str:
    parts = [header.rstrip()]
    if sec_blocks:
        parts.append("\n## 1. Hard Bans (`BLOCKS`)\n" + "\n".join(sec_blocks))
    if sec_resolves:
        parts.append("\n## 2. Workarounds (`RESOLVES`)\n" + "\n".join(sec_resolves))
    if sec_governs:
        parts.append("\n## 3. Scope Rules (`GOVERNS`)\n" + "\n".join(sec_governs))
    if sec_supersedes:
        parts.append("\n## 4. Superseded Quarantine (`SUPERSEDES`)\n" + "\n".join(sec_supersedes))
    return "\n".join(parts).strip() + "\n"


def recall_subgraph(
    graph: Dict[str, Any],
    query_text: str = "",
    budget_tokens: int = 1500,
    snapshot_files: Optional[Dict[str, List[str]]] = None,
    unverified_label: bool = False,
) -> Dict[str, Any]:
    """1-hop ontology subgraph recall with deterministic `SUPERSEDES` quarantine
    and typed sections (`GOVERNS`, `RESOLVES`, `BLOCKS`, plus explicit `SUPERSEDES` lineage warnings).
    Strictly allowlists `SUPPORTED`, `UNVERIFIED`, and `ALREADY_SUPERSEDED` verdicts and `VALID`/`UNVERIFIED`
    edge verdicts (so `PENDING`/`REJECTED` never surface), and computes `superseded_ids` only from surviving
    edges/nodes. Pins are SHA-256 re-verified only when `snapshot_files` is given; the result's
    `pinVerification` field says which. `unverified_label=True` stamps an UNVERIFIED warning into the header.
    Shipped callers should use `run_dream_recall`, which re-verifies and requires human approval by default.
    """
    _check_graph_safe_structure(graph)
    if snapshot_files is not None:
        ver = verify_ontology_contract(graph, snapshot_files)
        if not ver["ok"]:
            raise ValueError(f"Ontology contract verification failed: {ver}")

    raw_nodes = list(graph.get("nodes") or [])

    # Pass 1: collect candidate surviving nodes and all their surviving edges so cross-node SUPERSEDES
    # edges (e.g. L-NEW -> L-OLD) are visible when checking whether L-OLD has a surviving bound SUPERSEDES edge (NICE 1).
    cand_nodes: List[Tuple[int, Dict[str, Any]]] = []
    cand_edges_by_idx: Dict[int, List[Dict[str, Any]]] = {}
    all_cand_surv_edges: List[Dict[str, Any]] = []

    for idx, n in enumerate(raw_nodes):
        if not isinstance(n, dict):
            continue
        verdict = str(n.get("verdict") or "UNVERIFIED").upper()
        edge_v = str(n.get("edgeVerdict") or "VALID").upper()
        if verdict not in ALLOWED_RECALL_VERDICTS or edge_v not in ALLOWED_EDGE_VERDICTS:
            continue
        raw_e_list = n.get("edges") if isinstance(n.get("edges"), list) else []
        s_edges = [
            e for e in raw_e_list
            if isinstance(e, dict) and str(e.get("edgeVerdict") or "VALID").upper() in ALLOWED_EDGE_VERDICTS
        ]
        if not s_edges:
            continue
        cand_nodes.append((idx, n))
        cand_edges_by_idx[idx] = s_edges
        all_cand_surv_edges.extend(s_edges)

    nodes: List[Dict[str, Any]] = []
    surviving_edges: List[Dict[str, Any]] = []
    node_surv_edges: Dict[str, List[Dict[str, Any]]] = {}
    node_surv_edges_by_idx: Dict[int, List[Dict[str, Any]]] = {}
    kept_san_ids: Set[str] = set()
    seen_display_ids: Set[str] = set()
    duplicate_ids: List[str] = []

    for idx, n in cand_nodes:
        raw_lid = str(n.get("id") or "")
        base_lid = sanitize_node_id(raw_lid, f"L-{idx + 1:03d}")
        verdict = str(n.get("verdict") or "UNVERIFIED").upper()
        s_edges = cand_edges_by_idx.get(idx, [])
        sp_node = n.get("supersededByPin") if isinstance(n.get("supersededByPin"), dict) else None
        is_declared_sup = (
            n.get("statusKind") == "superseded_rule"
            or n.get("category") == "superseded_directive"
            or verdict == "ALREADY_SUPERSEDED"
            or sp_node is not None
        )
        if is_declared_sup:
            has_surv_bound_sup = any(
                e.get("edgeType") == "SUPERSEDES"
                and (str(e.get("toId") or "") in (raw_lid, base_lid) or sanitize_node_id(e.get("toId"), "") == base_lid)
                and isinstance(e.get("srcPin"), dict)
                and (sp_node is None or e.get("srcPin") == sp_node)
                for e in all_cand_surv_edges
            )
            if not has_surv_bound_sup:
                continue
        disp_lid = base_lid
        if disp_lid in seen_display_ids:
            duplicate_ids.append(disp_lid)
            sfx = f"#{idx + 1}"
            disp_lid = f"{base_lid[:max(1, 48 - len(sfx))]}{sfx}"
        seen_display_ids.add(disp_lid)
        kept_san_ids.add(base_lid)
        kept_san_ids.add(disp_lid)
        n_copy = dict(n)
        n_copy["_idx"] = idx
        n_copy["_base_lid"] = base_lid
        n_copy["_disp_lid"] = disp_lid
        nodes.append(n_copy)
        node_surv_edges_by_idx[idx] = s_edges
        node_surv_edges[raw_lid] = s_edges
        node_surv_edges[base_lid] = s_edges
        surviving_edges.extend(s_edges)

    # Identify superseded node IDs and pin-aware per-node superseding edges (Leftover 3: isolates duplicate IDs by pin)
    superseded_ids: Set[str] = set()
    superseded_indices: Set[int] = set()
    sup_edge_by_target: Dict[str, Dict[str, Any]] = {}
    sup_edge_by_idx: Dict[int, Dict[str, Any]] = {}
    for e in all_cand_surv_edges:
        if e.get("edgeType") == "SUPERSEDES":
            raw_tid = str(e.get("toId") or "")
            san_tid = sanitize_node_id(raw_tid, raw_tid)
            for k_id in (raw_tid, san_tid):
                if k_id:
                    sup_edge_by_target.setdefault(k_id, e)
            tp = e.get("targetPin") if isinstance(e.get("targetPin"), dict) else None
            tp_span = str(tp.get("span") or "") if tp else ""
            tp_sha = str(tp.get("sha256") or "") if tp else ""
            matched_any = False
            for n_item in nodes:
                n_idx = int(n_item.get("_idx", -1))
                n_raw = str(n_item.get("id") or "")
                n_base = str(n_item.get("_base_lid") or "")
                if n_raw not in (raw_tid, san_tid) and n_base not in (raw_tid, san_tid):
                    continue
                pp_n = n_item.get("primaryPin") if isinstance(n_item.get("primaryPin"), dict) else {}
                n_span = str(pp_n.get("span") or "")
                n_sha = str(pp_n.get("sha256") or "")
                if tp_span or tp_sha:
                    if (tp_sha and n_sha and tp_sha == n_sha) or (tp_span and n_span and tp_span == n_span):
                        superseded_indices.add(n_idx)
                        sup_edge_by_idx.setdefault(n_idx, e)
                        matched_any = True
                else:
                    superseded_indices.add(n_idx)
                    sup_edge_by_idx.setdefault(n_idx, e)
                    matched_any = True
            if not matched_any:
                for k_id in (raw_tid, san_tid):
                    if k_id:
                        superseded_ids.add(k_id)
            else:
                # Only mark global ID superseded if non-duplicate
                if san_tid not in duplicate_ids and raw_tid not in duplicate_ids:
                    for k_id in (raw_tid, san_tid):
                        if k_id:
                            superseded_ids.add(k_id)

    def _tokenize_query(q_str: str) -> Set[str]:
        return _query_terms(q_str, with_stems=False)

    q_tokens = _tokenize_query(query_text)
    sub_queries = [s.strip() for s in re.split(r"[\n?]+", query_text or "") if len(s.strip()) >= 8] or [query_text or ""]
    sub_token_sets = [_tokenize_query(sq) for sq in sub_queries if _tokenize_query(sq)]

    def node_bucket_kind(n: Dict[str, Any]) -> str:
        n_idx = int(n.get("_idx", -1))
        lid = str(n.get("id") or "")
        base_lid = str(n.get("_base_lid") or sanitize_node_id(lid, "L-000"))
        s_edges = node_surv_edges_by_idx.get(n_idx, node_surv_edges.get(lid, []))
        etypes = {str(e.get("edgeType") or "") for e in s_edges}
        if (
            n_idx in superseded_indices
            or (base_lid not in duplicate_ids and (lid in superseded_ids or base_lid in superseded_ids))
            or n.get("statusKind") == "superseded_rule"
            or n.get("category") == "superseded_directive"
            or str(n.get("verdict") or "").upper() == "ALREADY_SUPERSEDED"
        ):
            return "supersedes"
        if etypes == {"GOVERNS"} or n.get("category") == "cross_session_conflict":
            return "governs"
        if n.get("category") == "verified_workaround" or "RESOLVES" in etypes:
            return "resolves"
        if "BLOCKS" in etypes or n.get("category") == "repeated_pitfall":
            return "blocks"
        return "governs"

    def _words_and_stems(s_txt: str) -> Set[str]:
        return _query_terms(s_txt, with_stems=True)

    node_meta_ws_idx: Dict[int, Set[str]] = {}
    node_quote_ws_idx: Dict[int, Set[str]] = {}
    df_counts: Dict[str, int] = {}
    for n in nodes:
        n_idx = int(n.get("_idx", -1))
        s_edges = node_surv_edges_by_idx.get(n_idx, [])
        m_txt = " ".join([
            str(n.get("scopeTag") or ""),
            str(n.get("title") or ""),
            " ".join(str(e.get("targetLabel") or "") for e in s_edges),
        ])
        pp_d = n.get("primaryPin") if isinstance(n.get("primaryPin"), dict) else {}
        sp_d = n.get("supersededByPin") if isinstance(n.get("supersededByPin"), dict) else {}
        q1_c = re.sub(r"^\d{4}-\d{2}-\d{2}T[^\s]+\s+\[[^\]]+\]\s*", "", str(pp_d.get("quoteText") or ""), flags=re.M)
        q2_c = re.sub(r"^\d{4}-\d{2}-\d{2}T[^\s]+\s+\[[^\]]+\]\s*", "", str(sp_d.get("quoteText") or ""), flags=re.M)
        m_ws = _words_and_stems(m_txt)
        q_ws = _words_and_stems(q1_c + " " + q2_c)
        node_meta_ws_idx[n_idx] = m_ws
        node_quote_ws_idx[n_idx] = q_ws
        for w in (m_ws | q_ws):
            df_counts[w] = df_counts.get(w, 0) + 1

    def _score_node_for_tokens(n: Dict[str, Any], tok_set: Set[str]) -> Tuple[int, int, str, int]:
        n_idx = int(n.get("_idx", -1))
        disp_lid = str(n.get("_disp_lid") or n.get("id") or "")
        m_ws = node_meta_ws_idx.get(n_idx, set())
        q_ws = node_quote_ws_idx.get(n_idx, set())
        expanded_toks = set(tok_set)
        for t in list(tok_set):
            if len(t) >= 6:
                expanded_toks.add(t[:5])
        hits = 0
        for tok in expanded_toks:
            d = df_counts.get(tok, 0)
            if d == 0:
                continue
            idf_w = 8 if d <= 2 else (5 if d <= 5 else (3 if d <= 10 else 1))
            if tok in m_ws:
                hits += idf_w * 2
            elif tok in q_ws:
                hits += idf_w
        imp = _safe_imp(n.get("importance"), 2)
        return (-hits, imp, disp_lid, n_idx)

    def relevance_score(n: Dict[str, Any]) -> Tuple[int, int, str, int]:
        return _score_node_for_tokens(n, q_tokens)

    sorted_all = sorted(nodes, key=relevance_score)
    by_b: Dict[str, List[Dict[str, Any]]] = {"supersedes": [], "resolves": [], "blocks": [], "governs": []}
    for n in sorted_all:
        by_b[node_bucket_kind(n)].append(n)

    ranked: List[Dict[str, Any]] = []
    seeded_indices: Set[int] = set()
    if len(sub_token_sets) > 1:
        for _round in range(2):
            for stoks in sub_token_sets:
                cands = sorted(nodes, key=lambda n: _score_node_for_tokens(n, stoks))
                for c in cands:
                    if _score_node_for_tokens(c, stoks)[0] >= 0:
                        break
                    c_idx = int(c.get("_idx", -1))
                    if c_idx not in seeded_indices:
                        ranked.append(c)
                        seeded_indices.add(c_idx)
                        break
    else:
        for n in sorted_all:
            n_idx = int(n.get("_idx", -1))
            if relevance_score(n)[0] < 0 and len(ranked) < 12 and n_idx not in seeded_indices:
                ranked.append(n)
                seeded_indices.add(n_idx)

    for b_name, quota in (("supersedes", 3), ("resolves", 2), ("blocks", 3), ("governs", 2)):
        for n in by_b[b_name][:quota]:
            n_idx = int(n.get("_idx", -1))
            if n_idx not in seeded_indices:
                ranked.append(n)
                seeded_indices.add(n_idx)
    for n in sorted_all:
        n_idx = int(n.get("_idx", -1))
        if n_idx not in seeded_indices:
            ranked.append(n)
            seeded_indices.add(n_idx)

    sec_blocks: List[str] = []
    sec_resolves: List[str] = []
    sec_governs: List[str] = []
    sec_supersedes: List[str] = []
    sup_item_sources: List[Tuple[int, Optional[int], Optional[Dict[str, Any]], int]] = []
    included_order: List[Tuple[str, int, str, int]] = []

    included_ids: List[str] = []
    header = "# Memlet Dream Subgraph Capsule\n"
    if unverified_label:
        header += (
            "# UNVERIFIED: pins were NOT re-checked against a snapshot (caller opted out of verification).\n"
        )
    header += "# Rule: Never apply [SUPERSEDED] old_pin; apply replacement_pin [ACTIVE].\n"

    def _window_quote(raw_q: str, is_replacement: bool = False) -> str:
        cleaned = re.sub(
            r"\s*\d{4}-\d{2}-\d{2}T[^\s]+\s+\[[^\]]+\]\s*",
            " ",
            re.sub(r"\s+", " ", raw_q).strip(),
        ).strip()
        cleaned = re.sub(r"^TOOL_(?:RESULT|USE\s+\w+):\s*", "", cleaned)
        max_len = _RECALL_REPLACEMENT_QUOTE_CHARS if is_replacement else _RECALL_QUOTE_CHARS
        if len(cleaned) <= max_len:
            return safe_truncate_masked(cleaned, max_len)
        low_c = cleaned.lower()
        late_pos = -1
        for tok in q_tokens:
            pos = low_c.find(tok)
            if pos >= (max_len - 25):
                late_pos = pos
                break
        if late_pos != -1:
            head_len = max_len // 2 - 4
            tail_len = max_len - head_len - 3
            w_start = max(head_len, min(len(cleaned) - tail_len, late_pos - 12))
            combined = cleaned[:head_len].rstrip() + " … " + cleaned[w_start : w_start + tail_len].lstrip()
            return safe_truncate_masked(combined, max_len + 4)
        return safe_truncate_masked(cleaned, max_len)

    for n in ranked:
        n_idx = int(n.get("_idx", -1))
        base_lid = str(n.get("_base_lid") or sanitize_node_id(n.get("id"), "L-000"))
        lid = str(n.get("_disp_lid") or base_lid)
        pp = n.get("primaryPin") if isinstance(n.get("primaryPin"), dict) else {}
        sp = n.get("supersededByPin") if isinstance(n.get("supersededByPin"), dict) else None
        bound_sup_edge = sup_edge_by_idx.get(n_idx) or (
            (sup_edge_by_target.get(base_lid) or sup_edge_by_target.get(str(n.get("id") or "")))
            if base_lid not in duplicate_ids
            else None
        )
        if not sp and isinstance(bound_sup_edge, dict):
            cand_sp = bound_sup_edge.get("srcPin")
            sp = cand_sp if isinstance(cand_sp, dict) else None
        quote_one = _window_quote(str(pp.get("quoteText") or ""), is_replacement=False)
        node_edges = node_surv_edges_by_idx.get(n_idx, node_surv_edges.get(str(n.get("id") or ""), []))
        b_kind = node_bucket_kind(n)
        matching_type = {
            "blocks": "BLOCKS",
            "resolves": "RESOLVES",
            "governs": "GOVERNS",
            "supersedes": "SUPERSEDES",
        }.get(b_kind, "GOVERNS")
        typed_labels = [
            str(e.get("targetLabel") or "")
            for e in node_edges
            if e.get("targetLabel") and str(e.get("edgeType") or "") == matching_type
        ]
        if not typed_labels:
            typed_labels = [str(e.get("targetLabel") or "") for e in node_edges if e.get("targetLabel")]
        targets = safe_truncate_masked(", ".join(sorted(set(typed_labels))), 56)
        scope_lbl = safe_truncate_masked(str(n.get("scopeTag") or "general"), 42)
        title_lbl = safe_truncate_masked(str(n.get("title") or ""), 128)

        raw_pp_sp = str(pp.get("span") or "")
        safe_pp_sp = raw_pp_sp if parse_span(raw_pp_sp) else "unpinned"
        safe_pp_sha = re.sub(r"[^0-9a-f]", "", str(pp.get("sha256") or "").lower())[:8] or "none"

        if b_kind == "supersedes":
            if isinstance(sp, dict) and sp.get("quoteText"):
                sup_quote = _window_quote(str(sp.get("quoteText") or ""), is_replacement=True)
                raw_sup_sp = str(sp.get("span") or "")
                sup_span = raw_sup_sp if parse_span(raw_sup_sp) else "unpinned"
                sup_sha = re.sub(r"[^0-9a-f]", "", str(sp.get("sha256") or "").lower())[:8] or "none (unpinned)"
            else:
                sup_quote = "superseded by newer directive"
                sup_span = "unpinned"
                sup_sha = "none (unpinned)"
            # Leftover 6 & Leftover 3: Identify exact superseding source node instance by pin/ID
            repl_label = "ACTIVE — APPLY THIS"
            src_node_idx: Optional[int] = None
            if isinstance(bound_sup_edge, dict):
                raw_from_id = str(bound_sup_edge.get("fromId") or "")
                e_sp = bound_sup_edge.get("srcPin") if isinstance(bound_sup_edge.get("srcPin"), dict) else {}
                e_sp_span = str(e_sp.get("span") or "")
                e_sp_sha = str(e_sp.get("sha256") or "")
                if not raw_from_id.upper().endswith(":NEWER"):
                    san_from = sanitize_node_id(raw_from_id, "")
                    for cand_src in nodes:
                        c_idx = int(cand_src.get("_idx", -1))
                        c_raw = str(cand_src.get("id") or "")
                        c_base = str(cand_src.get("_base_lid") or "")
                        c_pp = cand_src.get("primaryPin") if isinstance(cand_src.get("primaryPin"), dict) else {}
                        c_span = str(c_pp.get("span") or "")
                        c_sha = str(c_pp.get("sha256") or "")
                        if (e_sp_sha and c_sha and e_sp_sha == c_sha) or (e_sp_span and c_span and e_sp_span == c_span):
                            if not san_from or c_raw in (raw_from_id, san_from) or c_base in (raw_from_id, san_from):
                                src_node_idx = c_idx
                                break
                    if src_node_idx is None and san_from and san_from not in duplicate_ids:
                        for cand_src in nodes:
                            if str(cand_src.get("_base_lid") or "") == san_from or str(cand_src.get("id") or "") in (raw_from_id, san_from):
                                src_node_idx = int(cand_src.get("_idx", -1))
                                break
                    if src_node_idx is None:
                        repl_label = "SOURCE_SUPERSEDED_OR_DROPPED — RE-VERIFY"
                    elif src_node_idx in superseded_indices or (san_from not in duplicate_ids and san_from in superseded_ids):
                        repl_label = "SOURCE_SUPERSEDED_OR_DROPPED — RE-VERIFY"
            item = (
                f"- [{lid}] ({scope_lbl}) [SUPERSEDES: old_pin({safe_pp_sp}) replaced by replacement_pin({sup_span})] {title_lbl}\n"
                f"  old_pin [SUPERSEDED — DO NOT APPLY AS CURRENT] [{safe_pp_sp} {safe_pp_sha}]: {quote_one}\n"
                f"  replacement_pin [{repl_label}] [{sup_span} {sup_sha}]: {sup_quote}"
            )
            bucket = sec_supersedes
        elif b_kind == "resolves":
            item = (
                f"- [{lid}] ({scope_lbl}) [RESOLVES -> {targets or scope_lbl}] {title_lbl}\n"
                f"  pin [{safe_pp_sp} {safe_pp_sha}]: {quote_one}"
            )
            bucket = sec_resolves
        elif b_kind == "blocks":
            item = (
                f"- [{lid}] ({scope_lbl}) [BLOCKS -> {targets or scope_lbl}] {title_lbl}\n"
                f"  pin [{safe_pp_sp} {safe_pp_sha}]: {quote_one}"
            )
            bucket = sec_blocks
        else:
            item = (
                f"- [{lid}] ({scope_lbl}) [GOVERNS -> {targets or scope_lbl}] {title_lbl}\n"
                f"  pin [{safe_pp_sp} {safe_pp_sha}]: {quote_one}"
            )
            bucket = sec_governs

        bucket_idx = len(bucket)
        bucket.append(item)
        cand_formatted = _assemble_sections(header, sec_blocks, sec_resolves, sec_governs, sec_supersedes)
        if estimate_tokens(cand_formatted) > budget_tokens:
            bucket.pop()
            break
        included_ids.append(lid)
        included_order.append((b_kind, bucket_idx, lid, n_idx))
        if b_kind == "supersedes":
            sup_item_sources.append((bucket_idx, src_node_idx, bound_sup_edge, n_idx))

    included_indices_set = {x[3] for x in included_order}
    for sup_idx, s_node_idx, b_edge, _owner_idx in sup_item_sources:
        if isinstance(b_edge, dict) and 0 <= sup_idx < len(sec_supersedes):
            raw_from = str(b_edge.get("fromId") or "")
            if raw_from and not raw_from.upper().endswith(":NEWER"):
                if s_node_idx is None or s_node_idx not in included_indices_set:
                    sec_supersedes[sup_idx] = sec_supersedes[sup_idx].replace(
                        "replacement_pin [ACTIVE — APPLY THIS]",
                        "replacement_pin [SOURCE_SUPERSEDED_OR_DROPPED — RE-VERIFY]",
                    )

    formatted = _assemble_sections(header, sec_blocks, sec_resolves, sec_governs, sec_supersedes)
    bucket_map = {
        "blocks": sec_blocks,
        "resolves": sec_resolves,
        "governs": sec_governs,
        "supersedes": sec_supersedes,
    }
    while estimate_tokens(formatted) > budget_tokens and included_order:
        last_b_kind, _last_b_idx, last_lid, last_n_idx = included_order.pop()
        b_list = bucket_map[last_b_kind]
        if b_list:
            b_list.pop()
        if last_lid in included_ids:
            included_ids.remove(last_lid)
        included_indices_set.discard(last_n_idx)
        for sup_idx, s_node_idx, b_edge, _owner_idx in sup_item_sources:
            if isinstance(b_edge, dict) and 0 <= sup_idx < len(sec_supersedes):
                raw_from = str(b_edge.get("fromId") or "")
                if raw_from and not raw_from.upper().endswith(":NEWER"):
                    if s_node_idx is None or s_node_idx not in included_indices_set:
                        sec_supersedes[sup_idx] = sec_supersedes[sup_idx].replace(
                            "replacement_pin [ACTIVE — APPLY THIS]",
                            "replacement_pin [SOURCE_SUPERSEDED_OR_DROPPED — RE-VERIFY]",
                        )
        formatted = _assemble_sections(header, sec_blocks, sec_resolves, sec_governs, sec_supersedes)
    return {
        "formatted": formatted,
        "tokenEstimate": estimate_tokens(formatted),
        "tokenBudget": budget_tokens,
        "includedNodeIds": included_ids,
        "duplicateIds": duplicate_ids,
        "pinVerification": "sha256_verified" if snapshot_files is not None else "not_verified",
        "counts": {
            "blocks": len(sec_blocks),
            "resolves": len(sec_resolves),
            "governs": len(sec_governs),
            "supersedesQuarantined": len(sec_supersedes),
        },
    }


def build_cortex_lineage_payload(
    graph: Dict[str, Any],
    subgraph_info: Optional[Dict[str, Any]] = None,
    snapshot_files: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """Build the `memlet.cortex_lineage.v1` JSON payload for `memlet_cortex.py`.
    Applies recall's surviving-node and bound-edge filtering (NICE 3) and 48-char node ID normalization (NICE 4).
    """
    _check_graph_safe_structure(graph)
    if snapshot_files is not None:
        ver = verify_ontology_contract(graph, snapshot_files)
        if not ver["ok"]:
            raise ValueError(f"Ontology contract check failed before Cortex payload build: {ver}")

    raw_nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    all_surv_edges: List[Dict[str, Any]] = []
    node_surv_map: Dict[int, List[Dict[str, Any]]] = {}

    for idx, n in enumerate(raw_nodes):
        verdict = str(n.get("verdict") or "UNVERIFIED").upper()
        edge_v = str(n.get("edgeVerdict") or "VALID").upper()
        if verdict not in ALLOWED_RECALL_VERDICTS or edge_v not in ALLOWED_EDGE_VERDICTS:
            continue
        raw_e_list = n.get("edges") if isinstance(n.get("edges"), list) else []
        s_edges = [
            e for e in raw_e_list
            if isinstance(e, dict) and str(e.get("edgeVerdict") or "VALID").upper() in ALLOWED_EDGE_VERDICTS
        ]
        if not s_edges:
            continue
        node_surv_map[idx] = s_edges
        all_surv_edges.extend(s_edges)

    has_recall_info = isinstance(subgraph_info, dict) and "includedNodeIds" in subgraph_info
    included_set = {
        sanitize_node_id(nid, "")
        for nid in ((subgraph_info or {}).get("includedNodeIds") or [])
        if nid
    }

    nodes_out: List[Dict[str, Any]] = []
    surviving_edges_out: List[Dict[str, Any]] = []
    superseded_target_ids: Set[str] = set()
    sup_edge_for_target: Dict[str, Dict[str, Any]] = {}
    for e in all_surv_edges:
        if e.get("edgeType") == "SUPERSEDES":
            raw_t = str(e.get("toId") or "")
            san_t = sanitize_node_id(raw_t, raw_t)
            for k_t in (raw_t, san_t):
                if k_t:
                    superseded_target_ids.add(k_t)
                    sup_edge_for_target.setdefault(k_t, e)

    for idx, n in enumerate(raw_nodes):
        if idx not in node_surv_map:
            continue
        raw_lid = str(n.get("id") or "")
        lid = sanitize_node_id(raw_lid, f"L-{idx + 1:03d}")
        verdict = str(n.get("verdict") or "UNVERIFIED").upper()
        if verdict not in VERDICTS:
            verdict = "UNVERIFIED"
        cat = str(n.get("category") or "repeated_pitfall")
        if cat not in CATEGORIES:
            cat = "repeated_pitfall"
        pp = n.get("primaryPin") if isinstance(n.get("primaryPin"), dict) else {}
        sp = n.get("supersededByPin") if isinstance(n.get("supersededByPin"), dict) else {}
        if not sp and lid in sup_edge_for_target:
            cand_sp = sup_edge_for_target[lid].get("srcPin")
            sp = cand_sp if isinstance(cand_sp, dict) else {}
        is_declared_sup = (
            n.get("statusKind") == "superseded_rule"
            or cat == "superseded_directive"
            or verdict == "ALREADY_SUPERSEDED"
            or bool(sp)
        )
        if is_declared_sup:
            has_surv_bound_sup = any(
                e.get("edgeType") == "SUPERSEDES"
                and (str(e.get("toId") or "") in (raw_lid, lid) or sanitize_node_id(e.get("toId"), "") == lid)
                and isinstance(e.get("srcPin"), dict)
                and (not sp or e.get("srcPin") == sp)
                for e in all_surv_edges
            )
            if not has_surv_bound_sup:
                continue
        st = "superseded" if (is_declared_sup or lid in superseded_target_ids) else (
            "dead_end" if cat == "repeated_pitfall" else "active"
        )
        raw_q_lines = pp.get("quoteLines") if isinstance(pp.get("quoteLines"), list) else []
        raw_vendors = n.get("vendorOrigins") if isinstance(n.get("vendorOrigins"), list) else []
        nodes_out.append({
            "id": lid,
            "kind": cat,
            "title": safe_truncate_masked(str(n.get("title") or ""), 110),
            "scopeTag": safe_truncate_masked(str(n.get("scopeTag") or ""), 48),
            "status": st,
            "isSuperseded": st == "superseded",
            "inRecallCapsule": bool((not has_recall_info) or (lid in included_set)),
            "vendorOrigins": [safe_truncate_masked(str(v), 48) for v in raw_vendors],
            "srcSpan": safe_truncate_masked(str(pp.get("span") or ""), 64),
            "quoteLines": [mask_secrets(str(ln)) for ln in raw_q_lines],
            "sha256Pin": str(pp.get("sha256") or ""),
            "supersededBySpan": safe_truncate_masked(str(sp.get("span") or ""), 64) if sp else None,
            "supersededBySha256": str(sp.get("sha256") or "") if sp else None,
            "verifiedBy": safe_truncate_masked(str(n.get("verifiedBy") or "unverified"), 64),
            "verdict": verdict,
        })
        surviving_edges_out.extend(node_surv_map[idx])
        if len(nodes_out) >= 24:
            break

    edges_out = []
    for idx, e in enumerate(surviving_edges_out[:48]):
        if not isinstance(e, dict):
            continue
        sp = e.get("srcPin") if isinstance(e.get("srcPin"), dict) else {}
        tp = e.get("targetPin") if isinstance(e.get("targetPin"), dict) else {}
        etype = str(e.get("edgeType") or "").upper()
        if etype not in EDGE_TYPES:
            continue
        raw_from = str(e.get("fromId") or "").strip()
        raw_to = str(e.get("toId") or "").strip()
        from_id = (
            f"{sanitize_node_id(raw_from[:-6], f'L-{idx + 1:03d}')}:NEWER"
            if raw_from.upper().endswith(":NEWER")
            else sanitize_node_id(raw_from, f"L-{idx + 1:03d}")
        )
        to_id = (
            sanitize_id(raw_to, f"SCOPE:{idx + 1:03d}")[:48]
            if any(raw_to.upper().startswith(pfx) for pfx in RESERVED_ID_PREFIXES)
            else sanitize_node_id(raw_to, f"L-{idx + 1:03d}")
        )
        edges_out.append({
            "edgeId": sanitize_id(e.get("edgeId"), f"E-{idx + 1:03d}"),
            "edgeType": etype,
            "fromId": from_id,
            "toId": to_id,
            "targetLabel": safe_truncate_masked(str(e.get("targetLabel") or ""), 80),
            "srcSpan": safe_truncate_masked(str(sp.get("span") or ""), 64),
            "srcSha256": str(sp.get("sha256") or ""),
            "targetSpan": safe_truncate_masked(str(tp.get("span") or ""), 64) if tp else None,
            "targetSha256": str(tp.get("sha256") or "") if tp else None,
            "vendorOrigin": safe_truncate_masked(str(e.get("vendorOrigin") or ""), 64),
        })
    sub_d = subgraph_info if isinstance(subgraph_info, dict) else {}
    return {
        "schema": CORTEX_LINEAGE_SCHEMA,
        "nodes": nodes_out,
        "edges": edges_out,
        "subgraphCapsule": {
            "tokenEstimate": int(sub_d.get("tokenEstimate") or 0),
            "tokenBudget": int(sub_d.get("tokenBudget") or 1500),
            "counts": dict(sub_d.get("counts") or {}) if isinstance(sub_d.get("counts"), dict) else {},
        },
    }


_JSONL_TEXT_ITEM_TYPES = ("text", "input_text", "output_text")


def _jsonl_text(content: Any) -> str:
    """Pull plain text out of a transcript `content` value (string, or list of text-bearing items)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                itype = item.get("type")
                if (itype is None or itype in _JSONL_TEXT_ITEM_TYPES) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return " ".join(parts)
    return ""


def _jsonl_record_to_line(obj: Any) -> Optional[str]:
    """Map one transcript record to a single pin-addressable line `<ISO ts> [<role>] <text>`.

    Understands Claude Code records (`{"type": "user"|"assistant", "timestamp", "message": {"role", "content"}}`),
    Codex CLI rollout records (`{"timestamp", "type": "response_item", "payload": {"type": "message", "role",
    "content": [{"type": "input_text"|"output_text", "text"}]}}`) and the older top-level Codex
    `{"type": "message", "role", "content"}` form. Anything else (tool calls, metadata, summaries, events)
    returns None. Text-bearing items only; tool payloads are not transcribed.
    """
    if not isinstance(obj, dict):
        return None
    containers: List[Dict[str, Any]] = []
    for key in ("message", "payload"):
        val = obj.get(key)
        if isinstance(val, dict):
            containers.append(val)
    containers.append(obj)
    role = ""
    text = ""
    for c in containers:
        if c is not obj and c.get("type") not in (None, "message"):
            continue
        if c is obj and c.get("type") not in (None, "message") and "role" not in c:
            continue
        c_role = c.get("role")
        if not isinstance(c_role, str) or not c_role.strip():
            continue
        c_text = _jsonl_text(c.get("content"))
        if c_text.strip():
            role, text = c_role, c_text
            break
    if not text:
        return None
    role = re.sub(r"[^A-Za-z0-9_-]", "", role)[:24] or "unknown"
    ts = ""
    for src in (obj, obj.get("payload") if isinstance(obj.get("payload"), dict) else {}):
        raw_ts = src.get("timestamp")
        if isinstance(raw_ts, str):
            m = _LINE_TS_RE.match(raw_ts.strip())
            if m and m.group(1) == raw_ts.strip():
                ts = m.group(1)
                break
    body = " ".join(text.split())
    return f"{ts} [{role}] {body}" if ts else f"[{role}] {body}"


def read_jsonl_transcript(path: Any) -> Tuple[List[str], Dict[str, int]]:
    """Read a Claude Code / Codex CLI `.jsonl` session transcript into pin-addressable lines.

    Line N of the result always corresponds to physical line N of the file, so appended records never
    shift existing pins. Non-message and malformed lines become empty strings, which `resolve_pin`
    refuses to pin. Nothing is written to disk.
    """
    p = Path(str(path))
    lines: List[str] = []
    stats = {"records": 0, "messages": 0, "malformedLines": 0, "nonMessageLines": 0}
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.rstrip("\r\n")
            if not raw.strip():
                lines.append("")
                continue
            stats["records"] += 1
            try:
                obj = json.loads(raw)
            except (ValueError, RecursionError):
                stats["malformedLines"] += 1
                lines.append("")
                continue
            if not isinstance(obj, dict):
                stats["malformedLines"] += 1
                lines.append("")
                continue
            line = _jsonl_record_to_line(obj)
            if line is None:
                stats["nonMessageLines"] += 1
                lines.append("")
            else:
                stats["messages"] += 1
                lines.append(line)
    return lines, stats


def load_snapshot(snapshot_path: Any) -> Dict[str, List[str]]:
    """Load a session snapshot from a directory of `.log`/`.jsonl` files, one `.jsonl` transcript, or a JSON snapshot map."""
    return load_snapshot_with_report(snapshot_path)[0]


def load_snapshot_with_report(snapshot_path: Any) -> Tuple[Dict[str, List[str]], Dict[str, Any]]:
    """Like `load_snapshot`, plus a report of transcript files read and malformed/non-message lines skipped.

    Directory snapshots are scanned recursively; file ids are POSIX paths relative to the directory
    (`sub/dir/session.jsonl`) and must satisfy `parse_span`'s safety rules, otherwise the file is skipped
    and counted under `skippedUnsafePaths`. Symlinked files and files resolving outside the directory are skipped.
    """
    p = Path(str(snapshot_path)).expanduser().resolve()
    report: Dict[str, Any] = {
        "logFiles": 0,
        "jsonlFiles": 0,
        "jsonlMessages": 0,
        "jsonlMalformedLines": 0,
        "jsonlNonMessageLines": 0,
        "skippedUnsafePaths": 0,
    }
    if not p.exists():
        raise FileNotFoundError(f"Snapshot path does not exist: {p}")

    def _add_jsonl(target: Dict[str, List[str]], key: str, f: Path) -> None:
        lines, st = read_jsonl_transcript(f)
        target[key] = lines
        report["jsonlFiles"] += 1
        report["jsonlMessages"] += st["messages"]
        report["jsonlMalformedLines"] += st["malformedLines"]
        report["jsonlNonMessageLines"] += st["nonMessageLines"]

    if p.is_dir():
        out: Dict[str, List[str]] = {}
        found = sorted(set(p.rglob("*.log")) | set(p.rglob("*.jsonl")))
        for f in found:
            if not f.is_file() or f.is_symlink():
                continue
            try:
                f.resolve().relative_to(p)
            except ValueError:
                report["skippedUnsafePaths"] += 1
                continue
            rel = f.relative_to(p).as_posix()
            if not _is_safe_file_id(rel) or not _SPAN_RE.match(f"{rel}:1"):
                report["skippedUnsafePaths"] += 1
                continue
            if f.suffix == ".jsonl":
                _add_jsonl(out, rel, f)
            else:
                out[rel] = f.read_text(encoding="utf-8", errors="replace").splitlines()
                report["logFiles"] += 1
        if not out:
            raise ValueError(
                f"No pinnable .log or .jsonl files found in snapshot directory: {p} "
                f"(skipped unsafe paths: {report['skippedUnsafePaths']})"
            )
        return out, report
    if p.is_file() and not p.is_symlink() and p.suffix == ".jsonl":
        single: Dict[str, List[str]] = {}
        _add_jsonl(single, p.name, p)
        return single, report
    if p.is_file() and not p.is_symlink():
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            res: Dict[str, List[str]] = {}
            for k, v in raw.items():
                if isinstance(v, list):
                    res[str(k)] = [str(line) for line in v]
                elif isinstance(v, str):
                    res[str(k)] = v.splitlines()
            if res:
                return res, report
        raise ValueError(f"Invalid JSON snapshot structure in: {p}")
    raise ValueError(f"Snapshot path must be a regular JSON/.jsonl file or directory: {p}")


def count_redactions_by_type(snapshot_files: Dict[str, List[str]]) -> Dict[str, int]:
    """Count how many lines across `snapshot_files` match each of the 5 named `REDACTION_PATTERNS`."""
    counts: Dict[str, int] = {name: 0 for name, _ in REDACTION_PATTERNS}
    for lines in snapshot_files.values():
        for ln in lines:
            for name, pat in REDACTION_PATTERNS:
                if pat.search(ln):
                    counts[name] = counts.get(name, 0) + 1
    return counts


# Cue patterns for the autonomous extractor. ASCII cues use word boundaries so `ban` never matches
# `banner` and status chatter such as `Tests pass now` is not mistaken for a workaround. Korean cues
# stay substring matches because Korean attaches particles/suffixes directly to the stem.
_SUPERSEDE_CUE_RE = re.compile(r"\b(?:superseded|supersedes|replaced\s+by)\b|폐기|대체", re.IGNORECASE)
_PITFALL_CUE_RE = re.compile(
    r"\b(?:do\s+not|don't|never|must\s+not|forbid|forbids|forbidden|ban|bans|banned)\b|금지|절대",
    re.IGNORECASE,
)
_WORKAROUND_CUE_RE = re.compile(r"\b(?:workaround|workarounds|work\s+around|fixed|resolved)\b|우회|해결", re.IGNORECASE)


def extract_candidates_with_report(
    snapshot_files: Dict[str, List[str]],
    max_candidates: int = AUTO_CANDIDATE_CAP,
) -> Dict[str, Any]:
    """Deterministic zero-daemon candidate extractor (used when `--candidates` is omitted).

    Returns `{"candidates", "cap", "truncated", "omittedCount"}`. Candidates beyond `max_candidates`
    are counted, not silently cut. Candidates carry no verdict: every built item starts `PENDING`.
    """
    cap = max(0, int(max_candidates))
    candidates: List[Dict[str, Any]] = []
    omitted = 0
    idx_counter = 0

    def _room(n: int) -> bool:
        return len(candidates) + n <= cap

    for fname in sorted(snapshot_files.keys()):
        lines = snapshot_files[fname]
        first_sup_line: Optional[int] = None
        for i, ln in enumerate(lines, start=1):
            if not extract_line_timestamp(ln):
                continue
            clean_body = re.sub(r"^\s*\d{4}-\d{2}-\d{2}[T ][^\s]+\s*(?:\[[^\]]+\]\s*)?", "", ln).strip()
            clean_body = mask_secrets(clean_body)
            if not clean_body:
                continue
            scope_m = re.search(r"([A-Za-z0-9_.-]+\.(?:py|md|json|ts|tsx|js))", clean_body)
            scope_tag = scope_m.group(1) if scope_m else "project_rules"

            if _SUPERSEDE_CUE_RE.search(clean_body):
                if first_sup_line is None:
                    first_sup_line = i
                elif i > first_sup_line:
                    if not _room(2):
                        omitted += 2
                        first_sup_line = None
                        continue
                    idx_counter += 1
                    lid = f"AUTO-{idx_counter:03d}"
                    act_id = f"AUTO-{idx_counter + 1:03d}"
                    candidates.append({
                        "lesson_id": lid,
                        "category": "superseded_directive",
                        "status_kind": "superseded_rule",
                        "scope_tag": scope_tag,
                        "title": safe_truncate_masked(f"Superseded rule at {fname}:{first_sup_line}; replaced by active {act_id}", 120),
                        "importance": 1,
                        "src_spans": [f"{fname}:{first_sup_line}"],
                        "superseded_by_src": f"{fname}:{i}",
                        "superseded_by_id": act_id,
                        "edges": [],
                    })
                    idx_counter += 1
                    candidates.append({
                        "lesson_id": act_id,
                        "category": "verified_workaround",
                        "status_kind": "active_lesson",
                        "scope_tag": scope_tag,
                        "title": safe_truncate_masked(clean_body, 120),
                        "importance": 1,
                        "src_spans": [f"{fname}:{i}"],
                        "edges": [{
                            "edge_type": "GOVERNS",
                            "to_id": f"SCOPE:{scope_tag}",
                            "target_label": scope_tag,
                            "src_span": f"{fname}:{i}",
                        }],
                    })
                    first_sup_line = None
                    continue

            if _PITFALL_CUE_RE.search(clean_body):
                if not _room(1):
                    omitted += 1
                    continue
                idx_counter += 1
                candidates.append({
                    "lesson_id": f"AUTO-{idx_counter:03d}",
                    "category": "repeated_pitfall",
                    "status_kind": "active_lesson",
                    "scope_tag": scope_tag,
                    "title": safe_truncate_masked(clean_body, 120),
                    "importance": 1,
                    "src_spans": [f"{fname}:{i}"],
                    "edges": [{
                        "edge_type": "BLOCKS",
                        "to_id": f"BLOCKS:{scope_tag}",
                        "target_label": f"{scope_tag}_violation",
                        "src_span": f"{fname}:{i}",
                    }],
                })
            elif _WORKAROUND_CUE_RE.search(clean_body):
                if not _room(1):
                    omitted += 1
                    continue
                idx_counter += 1
                candidates.append({
                    "lesson_id": f"AUTO-{idx_counter:03d}",
                    "category": "verified_workaround",
                    "status_kind": "active_lesson",
                    "scope_tag": scope_tag,
                    "title": safe_truncate_masked(clean_body, 120),
                    "importance": 2,
                    "src_spans": [f"{fname}:{i}"],
                    "edges": [{
                        "edge_type": "RESOLVES",
                        "to_id": f"RESOLVES:{scope_tag}",
                        "target_label": scope_tag,
                        "src_span": f"{fname}:{i}",
                    }],
                })
    return {
        "candidates": candidates,
        "cap": cap,
        "truncated": omitted > 0,
        "omittedCount": omitted,
    }


def extract_candidates_from_snapshot(snapshot_files: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """Backward-compatible list form of `extract_candidates_with_report` (see that function for truncation info)."""
    return extract_candidates_with_report(snapshot_files)["candidates"]


def _atomic_write_file(target: Path, content: str) -> int:
    import os
    import tempfile
    target.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(prefix=".memlet-dream-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, str(target))
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return len(data)


def _mark_pending(graph: Dict[str, Any]) -> None:
    """Stamp every node and edge `PENDING`; only `approve_queue_items`/`reject_queue_items` change it."""
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        n["verdict"] = PENDING
        n["edgeVerdict"] = PENDING
        n["verifiedBy"] = ""
        for e in n.get("edges") or []:
            if isinstance(e, dict):
                e["edgeVerdict"] = PENDING
    for e in graph.get("edges") or []:
        if isinstance(e, dict):
            e["edgeVerdict"] = PENDING


def run_dream_build(
    snapshot_path: str,
    candidates_path: Optional[str] = None,
    out_json_path: Optional[str] = None,
    out_db_path: Optional[str] = None,
    vendor: str = "deterministic-dream",
    out_json: Optional[str] = None,
    out_db: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute `dream-build`: load snapshot, load or deterministically extract candidates, pin & validate,
    verify ontology contract, write `APPROVAL-QUEUE.json` and `ontology.sqlite`, and return a build receipt.

    Every node and edge is written as `PENDING`. A candidate's self-reported `verdict`/`edgeVerdict` is
    kept only as `claimedVerdict`/`claimedEdgeVerdict` and never promotes anything. Use
    `approve_queue_items` (human decision, pin re-verified) to make items recallable.
    """
    snapshot_files, snapshot_report = load_snapshot_with_report(snapshot_path)
    resolved_snapshot = Path(str(snapshot_path)).expanduser().resolve()
    cap_info: Dict[str, Any] = {"cap": None, "truncated": False, "omittedCount": 0}
    if candidates_path:
        c_path = Path(candidates_path).expanduser().resolve()
        if not c_path.is_file():
            raise FileNotFoundError(f"Candidates file not found: {c_path}")
        raw_cands = json.loads(c_path.read_text(encoding="utf-8"))
        if isinstance(raw_cands, dict) and "candidates" in raw_cands:
            cands = raw_cands["candidates"]
        elif isinstance(raw_cands, dict) and "nodes" in raw_cands:
            cands = raw_cands["nodes"]
        elif isinstance(raw_cands, list):
            cands = raw_cands
        else:
            raise ValueError("Candidates JSON must be a list or contain candidates/nodes.")
        extraction_mode = "explicit_candidates"
    else:
        ext = extract_candidates_with_report(snapshot_files)
        cands = ext["candidates"]
        cap_info = {"cap": ext["cap"], "truncated": ext["truncated"], "omittedCount": ext["omittedCount"]}
        extraction_mode = "autonomous_deterministic"

    norm_cands: List[Dict[str, Any]] = [dict(c) for c in (cands or []) if isinstance(c, dict)]

    graph = dream_pin_and_validate(norm_cands, snapshot_files, vendor_origin=vendor)
    _mark_pending(graph)

    resolved_json_path = out_json or out_json_path or ".memlet/dream/APPROVAL-QUEUE.json"
    resolved_db_path = out_db or out_db_path
    out_json_p = Path(resolved_json_path).expanduser().resolve()
    out_db_p: Optional[Path] = Path(resolved_db_path).expanduser().resolve() if resolved_db_path else None
    # Paths only (no transcript content): lets recall/approval locate the snapshot and keep the DB in sync.
    graph["queueMeta"] = {
        "snapshotPath": str(resolved_snapshot),
        "dbPath": str(out_db_p) if out_db_p else None,
        "approvalRequired": True,
    }

    ver = verify_ontology_contract(graph, snapshot_files)
    if not ver["ok"]:
        raise ValueError(f"Ontology contract verification failed during dream-build: {ver}")

    json_str = json.dumps(graph, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_file(out_json_p, json_str)
    if out_db_p:
        write_ontology_sqlite(out_db_p, graph, snapshot_files=snapshot_files)

    edge_counts: Dict[str, int] = {et: 0 for et in EDGE_TYPES}
    for e in graph.get("edges", []):
        et = str(e.get("edgeType") or "")
        if et in edge_counts:
            edge_counts[et] += 1

    redactions = count_redactions_by_type(snapshot_files)
    return {
        "ok": True,
        "command": "dream-build",
        "extractionMode": extraction_mode,
        "snapshotFilesCount": len(snapshot_files),
        "snapshotReport": snapshot_report,
        "candidatesInputCount": len(norm_cands),
        "candidateCap": cap_info["cap"],
        "candidatesTruncated": cap_info["truncated"],
        "candidatesOmittedCount": cap_info["omittedCount"],
        "nodesCount": len(graph.get("nodes", [])),
        "edgesCount": len(graph.get("edges", [])),
        "pendingNodeCount": len(graph.get("nodes", [])),
        "pendingEdgeCount": len(graph.get("edges", [])),
        "approvalRequired": True,
        "edgeCountsByType": edge_counts,
        "droppedCount": len(graph.get("dropped", [])),
        "dropped": graph.get("dropped", []),
        "redactionCountsByType": redactions,
        "contractVerification": ver,
        "outJsonPath": str(out_json_p),
        "outJsonSha256": hashlib.sha256(out_json_p.read_bytes()).hexdigest(),
        "outDbPath": str(out_db_p) if out_db_p else None,
        "outDbSha256": hashlib.sha256(out_db_p.read_bytes()).hexdigest() if out_db_p else None,
    }


# ---------------------------------------------------------------------------------------------
# Approval API (library only). Approval is the only way an item leaves PENDING.
# ---------------------------------------------------------------------------------------------

APPROVED = "approved"
REJECTED_DECISION = "rejected"


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_queue(queue_path: Any) -> Tuple[Path, Dict[str, Any]]:
    q = Path(str(queue_path)).expanduser()
    if q.is_symlink():
        raise ValueError(f"Refusing symlinked Dream queue: {q}")
    q = q.resolve()
    if not q.is_file():
        raise FileNotFoundError(f"Dream queue file not found: {q}")
    graph = json.loads(q.read_text(encoding="utf-8"))
    if not isinstance(graph, dict) or graph.get("schema") != DREAM_ONTOLOGY_SCHEMA:
        raise ValueError(f"Not a {DREAM_ONTOLOGY_SCHEMA} queue: {q}")
    return q, graph


def _edge_refs(graph: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """All dict copies of each edge (graph-level list and owning node's list) keyed by edgeId."""
    refs: Dict[str, List[Dict[str, Any]]] = {}

    def _add(e: Any) -> None:
        if isinstance(e, dict) and e.get("edgeId"):
            lst = refs.setdefault(str(e["edgeId"]), [])
            if not any(x is e for x in lst):
                lst.append(e)

    for e in graph.get("edges") or []:
        _add(e)
    for n in graph.get("nodes") or []:
        if isinstance(n, dict):
            for e in n.get("edges") or []:
                _add(e)
    return refs


def _decision_of(item: Dict[str, Any]) -> Optional[str]:
    rec = item.get("approval")
    if isinstance(rec, dict) and rec.get("decision") in (APPROVED, REJECTED_DECISION):
        return str(rec["decision"])
    return None


def _locate_snapshot(
    graph: Dict[str, Any],
    snapshot_path: Optional[Any],
) -> Tuple[Optional[Dict[str, List[str]]], str]:
    """Explicit path first, then the path recorded by `run_dream_build`. Returns `(files|None, source)`."""
    if snapshot_path:
        return load_snapshot(snapshot_path), "explicit"
    meta = graph.get("queueMeta") if isinstance(graph.get("queueMeta"), dict) else {}
    rec = meta.get("snapshotPath")
    if isinstance(rec, str) and rec and Path(rec).exists():
        return load_snapshot(rec), "recorded"
    return None, "missing"


def _verify_item_pins(item: Dict[str, Any], kind: str, snap: Dict[str, List[str]]) -> Optional[str]:
    if kind == "node":
        err = _verify_pin_dict(item.get("primaryPin"), snap, "primaryPin")
        if err:
            return err
        for i, p in enumerate(item.get("allPins") or []):
            err = _verify_pin_dict(p, snap, f"allPins[{i}]")
            if err:
                return err
        if item.get("supersededByPin") is not None:
            return _verify_pin_dict(item.get("supersededByPin"), snap, "supersededByPin")
        return None
    err = _verify_pin_dict(item.get("srcPin"), snap, "srcPin")
    if err:
        return err
    if item.get("targetPin") is not None:
        return _verify_pin_dict(item.get("targetPin"), snap, "targetPin")
    return None


def list_pending(queue: Any) -> Dict[str, Any]:
    """List nodes and edges of a Dream queue (path or loaded dict) that no human has decided yet."""
    graph = queue if isinstance(queue, dict) else _load_queue(queue)[1]
    p_nodes: List[Dict[str, Any]] = []
    counts = {"approvedNodes": 0, "rejectedNodes": 0, "approvedEdges": 0, "rejectedEdges": 0}
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        dec = _decision_of(n)
        if dec == APPROVED:
            counts["approvedNodes"] += 1
        elif dec == REJECTED_DECISION:
            counts["rejectedNodes"] += 1
        else:
            pp = n.get("primaryPin") if isinstance(n.get("primaryPin"), dict) else {}
            p_nodes.append({
                "id": n.get("id"),
                "title": n.get("title"),
                "category": n.get("category"),
                "statusKind": n.get("statusKind"),
                "span": pp.get("span"),
                "sha256": pp.get("sha256"),
                "verdict": n.get("verdict"),
                "claimedVerdict": n.get("claimedVerdict"),
            })
    p_edges: List[Dict[str, Any]] = []
    for eid, refs in _edge_refs(graph).items():
        e = refs[0]
        dec = _decision_of(e)
        if dec == APPROVED:
            counts["approvedEdges"] += 1
        elif dec == REJECTED_DECISION:
            counts["rejectedEdges"] += 1
        else:
            sp = e.get("srcPin") if isinstance(e.get("srcPin"), dict) else {}
            tp = e.get("targetPin") if isinstance(e.get("targetPin"), dict) else {}
            p_edges.append({
                "edgeId": eid,
                "edgeType": e.get("edgeType"),
                "fromId": e.get("fromId"),
                "toId": e.get("toId"),
                "srcSpan": sp.get("span"),
                "targetSpan": tp.get("span") if tp else None,
                "edgeVerdict": e.get("edgeVerdict"),
            })
    counts["pendingNodes"] = len(p_nodes)
    counts["pendingEdges"] = len(p_edges)
    return {"pendingNodes": p_nodes, "pendingEdges": p_edges, "counts": counts}


def _decide_queue_items(
    queue_path: Any,
    ids: Sequence[str],
    decision: str,
    approver: str,
    snapshot_path: Optional[Any] = None,
    db_path: Optional[Any] = None,
    cascade_edges: bool = True,
    reason: str = "",
) -> Dict[str, Any]:
    if decision not in (APPROVED, REJECTED_DECISION):
        raise ValueError(f"Unknown decision: {decision}")
    who = safe_truncate_masked(str(approver or "").strip(), 64)
    if not who:
        raise ValueError("An approver name is required to record a decision.")
    if isinstance(ids, str):
        ids = [ids]
    q_path, graph = _load_queue(queue_path)

    snap: Optional[Dict[str, List[str]]] = None
    snap_source = "not_required"
    if decision == APPROVED:
        snap, snap_source = _locate_snapshot(graph, snapshot_path)

    done: List[str] = []
    unchanged: List[str] = []
    refused: List[Dict[str, str]] = []
    cascaded: List[str] = []
    ordered_ids: List[str] = []
    for raw_id in ids:
        if str(raw_id) not in ordered_ids:
            ordered_ids.append(str(raw_id))

    if decision == APPROVED and snap is None:
        for i in ordered_ids:
            refused.append({"id": i, "reason": "no_snapshot_to_verify_pins"})
        return {
            "ok": False,
            "decision": decision,
            "queuePath": str(q_path),
            "snapshotSource": snap_source,
            "applied": [],
            "unchanged": [],
            "refused": refused,
            "cascadedEdges": [],
            "dbPath": None,
        }

    at = _utc_now_iso()
    clean_reason = safe_truncate_masked(str(reason or ""), 200)
    nodes_by_id: Dict[str, List[Dict[str, Any]]] = {}
    for n in graph.get("nodes") or []:
        if isinstance(n, dict):
            nodes_by_id.setdefault(str(n.get("id") or ""), []).append(n)
    edge_refs = _edge_refs(graph)

    def _record(pin_sha: str) -> Dict[str, Any]:
        rec: Dict[str, Any] = {"decision": decision, "by": who, "at": at, "pinSha256": pin_sha}
        if decision == REJECTED_DECISION and clean_reason:
            rec["reason"] = clean_reason
        return rec

    def _apply_edge(eid: str) -> None:
        for ref in edge_refs.get(eid, []):
            ref["edgeVerdict"] = "VALID" if decision == APPROVED else REJECTED
            ref["approval"] = _record(str((ref.get("srcPin") or {}).get("sha256") or ""))

    for item_id in ordered_ids:
        if item_id in nodes_by_id:
            if len(nodes_by_id[item_id]) > 1:
                refused.append({"id": item_id, "reason": "ambiguous_duplicate_node_id"})
                continue
            n = nodes_by_id[item_id][0]
            cur = _decision_of(n)
            if cur == decision:
                unchanged.append(item_id)
                continue
            if cur is not None:
                refused.append({"id": item_id, "reason": f"already_{cur}"})
                continue
            owned = [
                str(e.get("edgeId")) for e in (n.get("edges") or [])
                if isinstance(e, dict) and e.get("edgeId") and _decision_of(e) is None
            ] if cascade_edges else []
            if decision == APPROVED:
                err = _verify_item_pins(n, "node", snap or {})
                if not err:
                    for eid in owned:
                        err = _verify_item_pins(edge_refs[eid][0], "edge", snap or {})
                        if err:
                            err = f"{eid}:{err}"
                            break
                if err:
                    refused.append({"id": item_id, "reason": f"pin_does_not_verify:{err}"})
                    continue
                n["verdict"] = "ALREADY_SUPERSEDED" if n.get("statusKind") == "superseded_rule" else "SUPPORTED"
                n["edgeVerdict"] = "VALID"
                n["verifiedBy"] = who
            else:
                n["verdict"] = REJECTED
                n["edgeVerdict"] = REJECTED
            pp = n.get("primaryPin") if isinstance(n.get("primaryPin"), dict) else {}
            n["approval"] = _record(str(pp.get("sha256") or ""))
            for eid in owned:
                _apply_edge(eid)
                cascaded.append(eid)
            done.append(item_id)
        elif item_id in edge_refs:
            e0 = edge_refs[item_id][0]
            cur = _decision_of(e0)
            if cur == decision:
                unchanged.append(item_id)
                continue
            if cur is not None:
                refused.append({"id": item_id, "reason": f"already_{cur}"})
                continue
            if decision == APPROVED:
                err = _verify_item_pins(e0, "edge", snap or {})
                if err:
                    refused.append({"id": item_id, "reason": f"pin_does_not_verify:{err}"})
                    continue
            _apply_edge(item_id)
            done.append(item_id)
        else:
            refused.append({"id": item_id, "reason": "unknown_id"})

    db_target: Optional[Path] = None
    if done:
        if db_path:
            db_target = Path(str(db_path)).expanduser().resolve()
        else:
            meta = graph.get("queueMeta") if isinstance(graph.get("queueMeta"), dict) else {}
            rec_db = meta.get("dbPath")
            if isinstance(rec_db, str) and rec_db and Path(rec_db).is_file():
                db_target = Path(rec_db)
        # DB first: if it fails nothing has changed on disk, so JSON and SQLite never disagree.
        if db_target is not None:
            write_ontology_sqlite(db_target, graph)
        _atomic_write_file(q_path, json.dumps(graph, ensure_ascii=False, indent=2) + "\n")

    return {
        "ok": not refused,
        "decision": decision,
        "decidedBy": who,
        "decidedAt": at,
        "queuePath": str(q_path),
        "snapshotSource": snap_source,
        "applied": done,
        "unchanged": unchanged,
        "refused": refused,
        "cascadedEdges": cascaded,
        "dbPath": str(db_target) if db_target else None,
    }


def approve_queue_items(
    queue_path: Any,
    ids: Sequence[str],
    approver: str,
    snapshot_path: Optional[Any] = None,
    db_path: Optional[Any] = None,
    cascade_edges: bool = True,
) -> Dict[str, Any]:
    """Approve nodes and/or edges of a Dream queue by id (human decision).

    Each id's citation pins are re-resolved against the snapshot (explicit `snapshot_path`, else the path
    recorded at build time) and SHA-256 compared; ids whose pins no longer verify are refused, and with no
    locatable snapshot every id is refused. Approving a node also approves its own still-undecided edges
    (`cascade_edges`). Records approver and UTC time; idempotent (re-approving is a no-op); never flips a
    rejected item. Rewrites the queue JSON atomically and, when a DB was written (or `db_path` is given),
    the SQLite tables from the same graph.
    """
    return _decide_queue_items(queue_path, ids, APPROVED, approver, snapshot_path, db_path, cascade_edges)


def reject_queue_items(
    queue_path: Any,
    ids: Sequence[str],
    approver: str,
    reason: str = "",
    db_path: Optional[Any] = None,
    cascade_edges: bool = True,
) -> Dict[str, Any]:
    """Reject nodes and/or edges by id (recorded like approvals; rejected items are never recalled).
    No snapshot or pin verification is needed to reject. Idempotent; never flips an approved item."""
    return _decide_queue_items(
        queue_path, ids, REJECTED_DECISION, approver, None, db_path, cascade_edges, reason=reason
    )


def approved_view(graph: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Copy of `graph` where anything without a matching human approval record is forced to PENDING.

    Approval is bound to the primary pin hash recorded at decision time, so a hand-edited verdict or a
    queue built before approvals existed (auto-promoted SUPPORTED/VALID) is not recallable.
    """
    import copy

    g = copy.deepcopy(graph)
    stats = {"pendingNodes": 0, "rejectedNodes": 0, "pendingEdges": 0, "rejectedEdges": 0}
    for n in g.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        dec = _decision_of(n)
        pp = n.get("primaryPin") if isinstance(n.get("primaryPin"), dict) else {}
        rec = n.get("approval") if isinstance(n.get("approval"), dict) else {}
        ok = (
            dec == APPROVED
            and str(n.get("verdict") or "").upper() in ALLOWED_RECALL_VERDICTS
            and rec.get("pinSha256") == pp.get("sha256")
        )
        if not ok:
            if dec == REJECTED_DECISION or str(n.get("verdict") or "").upper() == REJECTED:
                stats["rejectedNodes"] += 1
                n["verdict"] = REJECTED
            else:
                stats["pendingNodes"] += 1
                n["verdict"] = PENDING
    for eid, refs in _edge_refs(g).items():
        e0 = refs[0]
        dec = _decision_of(e0)
        sp = e0.get("srcPin") if isinstance(e0.get("srcPin"), dict) else {}
        rec = e0.get("approval") if isinstance(e0.get("approval"), dict) else {}
        ok = (
            dec == APPROVED
            and str(e0.get("edgeVerdict") or "").upper() in ALLOWED_EDGE_VERDICTS
            and rec.get("pinSha256") == sp.get("sha256")
        )
        if not ok:
            rejected = dec == REJECTED_DECISION or str(e0.get("edgeVerdict") or "").upper() == REJECTED
            stats["rejectedEdges" if rejected else "pendingEdges"] += 1
            for ref in refs:
                ref["edgeVerdict"] = REJECTED if rejected else PENDING
    return g, stats


def run_dream_recall(
    graph_path: str,
    snapshot_path: Optional[str] = None,
    query_text: str = "",
    budget_tokens: int = 1500,
    out_md_path: Optional[str] = None,
    out_md: Optional[str] = None,
    allow_unverified: bool = False,
    require_approval: bool = True,
) -> Dict[str, Any]:
    """Execute `dream-recall`: load graph, re-verify every pin against the snapshot, recall the 1-hop
    capsule from human-approved items only, and optionally write `--out-md`.

    - Snapshot: explicit `snapshot_path`, else the path recorded at build time. A tampered snapshot or pin
      raises `ValueError`.
    - No locatable snapshot: returns `ok=False`, `pinVerification="unavailable_no_snapshot"`, no items,
      and a capsule that says so — unless `allow_unverified=True`, in which case items are returned with
      `pinVerification="UNVERIFIED_OPT_OUT"` and an UNVERIFIED warning in the capsule header.
    - PENDING and REJECTED items are never returned (`require_approval=False` only relaxes the approval
      record check for library callers; it never admits PENDING/REJECTED verdicts).
    """
    g_path = Path(graph_path).expanduser().resolve()
    if not g_path.is_file():
        raise FileNotFoundError(f"Dream ontology graph file not found: {g_path}")
    graph = json.loads(g_path.read_text(encoding="utf-8"))
    if not isinstance(graph, dict):
        raise ValueError("Dream ontology graph must be a JSON object.")
    snap, snap_source = _locate_snapshot(graph, snapshot_path)
    if require_approval:
        view, excluded = approved_view(graph)
    else:
        view, excluded = graph, {}

    if snap is None and not allow_unverified:
        formatted = (
            "# Memlet Dream Subgraph Capsule\n"
            "# VERIFICATION UNAVAILABLE: no snapshot could be located, so no pin was re-checked and nothing "
            "is returned as verified. Pass a snapshot (--snapshot) or explicitly opt out (allow_unverified).\n"
        )
        res: Dict[str, Any] = {
            "ok": False,
            "formatted": formatted,
            "tokenEstimate": estimate_tokens(formatted),
            "tokenBudget": budget_tokens,
            "includedNodeIds": [],
            "duplicateIds": [],
            "pinVerification": "unavailable_no_snapshot",
            "counts": {"blocks": 0, "resolves": 0, "governs": 0, "supersedesQuarantined": 0},
            # `memory_cli.cli()` exits with this code, so `memlet dream-recall` also fails loudly.
            "exitCode": 3,
        }
    else:
        res = recall_subgraph(
            view,
            query_text=query_text,
            budget_tokens=budget_tokens,
            snapshot_files=snap,
            unverified_label=snap is None,
        )
        res["ok"] = True
        res["pinVerification"] = "sha256_verified" if snap is not None else "UNVERIFIED_OPT_OUT"
    res["snapshotSource"] = snap_source
    res["approvalRequired"] = bool(require_approval)
    res["excludedUndecidedOrRejected"] = excluded

    resolved_md = out_md or out_md_path
    if resolved_md:
        md_target = Path(resolved_md).expanduser().resolve()
        formatted_md = res["formatted"]
        if not formatted_md.endswith("\n"):
            formatted_md += "\n"
        _atomic_write_file(md_target, formatted_md)
        res["outMdPath"] = str(md_target)
        res["outMdSha256"] = hashlib.sha256(md_target.read_bytes()).hexdigest()
    return res


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python3 -m prism_memory.memlet_dream",
        description="Memlet Dream Quote-Pinned Session Consolidation & 4-Edge Ontology CLI",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    b_p = sub.add_parser("dream-build", aliases=["dream"], help="Build a PENDING Dream approval queue (JSON + optional SQLite) from session logs")
    b_p.add_argument("--candidates", default=None, help="Optional JSON file of extracted candidates (omit for autonomous deterministic extraction)")
    b_p.add_argument("--snapshot", required=True, help="Directory of .log/.jsonl files, one .jsonl transcript, or JSON snapshot map")
    b_p.add_argument("--out-json", default=".memlet/dream/APPROVAL-QUEUE.json", help="Output path for the ontology approval queue JSON")
    b_p.add_argument("--out-db", default=None, help="Optional output path for zero-daemon SQLite ontology DB")
    b_p.add_argument("--vendor", default="deterministic-dream", help="Vendor origin tag")

    r_p = sub.add_parser("dream-recall", help="Recall 1-hop ontology capsule of approved items (GOVERNS/SUPERSEDES/RESOLVES/BLOCKS)")
    r_p.add_argument("--graph", required=True, help="Path to Dream ontology approval queue JSON")
    r_p.add_argument("--snapshot", default=None, help="Snapshot for pin re-verification (default: the path recorded at build time)")
    r_p.add_argument("--allow-unverified", action="store_true", help="Return items even when no snapshot can be located (output is labelled UNVERIFIED)")
    r_p.add_argument("--query", nargs="*", default="", help="Task or file query for 1-hop relevance ranking (supports unquoted multi-word queries)")
    r_p.add_argument("--max-tokens", "--budget", dest="budget", type=int, default=1500, help="Hard token ceiling (default: 1500)")
    r_p.add_argument("--out-md", default=None, help="Optional Markdown file path to write the recalled capsule")
    r_p.add_argument("--format", default="markdown", choices=("markdown", "json"), help="Stdout output format (default: markdown)")

    try:
        args = parser.parse_args(argv)
        if args.subcommand in ("dream-build", "dream"):
            receipt = run_dream_build(
                snapshot_path=args.snapshot,
                candidates_path=args.candidates,
                out_json_path=args.out_json,
                out_db_path=args.out_db,
                vendor=args.vendor,
            )
            sys.stdout.write(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
            return 0
        if args.subcommand == "dream-recall":
            res = run_dream_recall(
                graph_path=args.graph,
                snapshot_path=args.snapshot,
                query_text=" ".join(args.query) if isinstance(args.query, list) else str(args.query or ""),
                budget_tokens=args.budget,
                out_md_path=args.out_md,
                allow_unverified=args.allow_unverified,
            )
            if args.format == "json":
                sys.stdout.write(json.dumps(res, ensure_ascii=False, indent=2) + "\n")
            else:
                txt = res.get("formatted", "")
                sys.stdout.write(txt + ("\n" if not txt.endswith("\n") else ""))
            if not res.get("ok", True):
                sys.stderr.write("memlet_dream: no snapshot located; nothing returned as verified\n")
                return 3
            return 0
        return 2
    except Exception as exc:
        sys.stderr.write(f"memlet_dream error: {exc}\n")
        return 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
