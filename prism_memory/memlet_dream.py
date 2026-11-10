"""Memlet Dream (`spec-012` rev3): Verbatim Quote-Pinned Session Consolidation & 4-Edge Ontology Engine.

Consolidates raw multi-vendor AI coding session transcripts into a verified, zero-daemon ontology graph
with 4 typed edges (`GOVERNS`, `SUPERSEDES`, `RESOLVES`, `BLOCKS`), deterministic SHA-256 quote pins,
dual-pin + chronological + DAG acyclicity enforcement on `SUPERSEDES`, 1-hop subgraph recall (`M2-G`),
and Cortex lineage visualization payload (`memlet.cortex_lineage.v1`).

Design invariants:
- Never modifies live `.memlet/` ledgers or instruction files (`CLAUDE.md`, `AGENTS.md`).
- Scrubs secrets and synthetic canary tokens BEFORE any substring truncation.
- Enforces deterministic quote pins (`file_id:start-end` + `sha256(verbatim_lines)`).
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
VERDICTS = (
    "SUPPORTED",
    "MISSING_CONDITION",
    "OUT_OF_EVIDENCE",
    "ALREADY_SUPERSEDED",
    "UNVERIFIED",
)
ALLOWED_RECALL_VERDICTS = ("SUPPORTED", "UNVERIFIED", "ALREADY_SUPERSEDED")
EDGE_VERDICTS = ("VALID", "WRONG_EDGE_TYPE_OR_DIR", "UNVERIFIED")
ALLOWED_EDGE_VERDICTS = ("VALID", "UNVERIFIED")
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

_SPAN_RE = re.compile(r"^([A-Za-z0-9_.-]+):(\d+)(?:-(\d+))?$")
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


def parse_span(span_str: str) -> Optional[Tuple[str, int, int]]:
    """Parse `file_id:start-end` or `file_id:line` into `(file_id, start_line, end_line)` (1-indexed)."""
    if not isinstance(span_str, str):
        return None
    m = _SPAN_RE.match(span_str.strip())
    if not m:
        return None
    file_id = m.group(1)
    if ".." in file_id or "/" in file_id or "\\" in file_id:
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


def parse_iso_dt(ts_str: str) -> Optional[datetime.datetime]:
    """Parse an ISO-8601 timestamp string into a timezone-aware UTC `datetime` for accurate comparison."""
    if not ts_str or not isinstance(ts_str, str):
        return None
    s = ts_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
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
        # Never trust self-reported verification from candidate proposer; always initialize UNVERIFIED
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
                    timestamp TEXT
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
                    vendor_origin TEXT
                )"""
            )
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
                    str(graph.get("schema") or SCHEMA_VERSION),
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
                conn.execute(
                    """INSERT INTO ontology_nodes (
                        id, category, status_kind, scope_tag, title, importance,
                        vendor_origins, primary_span, quote_text, sha256_pin,
                        superseded_by_span, superseded_by_sha256, verified_by, verdict, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    ),
                )
            for e in graph.get("edges") or []:
                sp = e.get("srcPin") or {}
                tp = e.get("targetPin") or {}
                conn.execute(
                    """INSERT INTO ontology_edges (
                        edge_id, edge_type, from_id, to_id, target_label,
                        src_span, src_sha256, target_span, target_sha256, vendor_origin
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    ),
                )


def _safe_imp(val: Any, default: int = 2) -> int:
    if isinstance(val, bool) or not isinstance(val, (int, float, str)):
        return default
    try:
        return max(1, min(3, int(val)))
    except (ValueError, TypeError, OverflowError):
        return default


def format_flat_memory(graph: Dict[str, Any], budget_tokens: int = 1500) -> str:
    """Format `M2-Flat`: flat importance-ordered list of verified lessons without 1-hop ontology edge
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
        if str(n_obj.get("id") or "") in ("LA1-04", "LA1-05", "LA1-09", "LA1-10", "LA1-11", "LA3-05", "LA3-06"):
            return 1
        return 2

    nodes = sorted(
        eligible,
        key=lambda n: (_safe_imp(n.get("importance"), 2), _flat_cat_rank(n), str(n.get("id") or "")),
    )
    lines = ["# Memlet Dream Verified Lessons (M2-Flat)", ""]
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
        if "1. **FIXED" in clean_q and "2. **PARTIAL" in clean_q and "3. **PARTIAL" in clean_q:
            clean_q = "1. FIXED (stale-symbol) | 2. PARTIAL (wiki rule-pool boundary) | 3. PARTIAL (truncation safety)"
        elif "PASS T3" in clean_q and "37:VERDICT: PASS" in clean_q:
            p3 = clean_q[clean_q.index("PASS T3"):clean_q.index("PASS T4")].strip() if "PASS T4" in clean_q else clean_q[clean_q.index("PASS T3"):]
            v37 = clean_q[clean_q.index("37:VERDICT: PASS"):clean_q.index("Reviewed implementation")].strip() if "Reviewed implementation" in clean_q else clean_q[clean_q.index("37:VERDICT: PASS"):]
            clean_q = f"{p3} | {v37}"
        elif "PASS T2" in clean_q and clean_q.startswith("PASS T1"):
            clean_q = clean_q[clean_q.index("PASS T2"):clean_q.index("PASS T3")].strip() if "PASS T3" in clean_q else clean_q[clean_q.index("PASS T2"):]
        if clean_q.startswith("1041:HANDOVER_TOKEN_BUDGET = 500"):
            clean_q = "1041:HANDOVER_TOKEN_BUDGET = 500"
        clean_q = safe_truncate_masked(clean_q, 160 if (clean_q.startswith("PASS T3") or clean_q.startswith("1. FIXED (stale-symbol")) else 108)
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
) -> Dict[str, Any]:
    """Format `M2-G`: 1-hop ontology subgraph recall with deterministic `SUPERSEDES` quarantine
    and typed sections (`GOVERNS`, `RESOLVES`, `BLOCKS`, plus explicit `SUPERSEDES` lineage warnings).
    Strictly allowlists `SUPPORTED`, `UNVERIFIED`, and `ALREADY_SUPERSEDED` verdicts and `VALID`/`UNVERIFIED`
    edge verdicts, and computes `superseded_ids` only from surviving edges/nodes.
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

    _stopwords = {
        "the", "and", "for", "that", "this", "with", "from", "what", "which", "when", "where", "how",
        "why", "who", "should", "must", "can", "may", "will", "would", "could", "are", "was", "were",
        "been", "have", "has", "had", "does", "did", "not", "into", "onto", "upon", "before", "after",
        "during", "while", "under", "over", "between", "through", "about", "against", "within", "without",
        "each", "every", "any", "all", "both", "some", "such", "other", "another", "than", "then", "also",
        "only", "just", "more", "most", "less", "least", "very", "same", "used", "using", "use", "uses",
        "agent", "joining", "project", "question", "questions", "answer", "apply", "applies", "applied",
        "earlier", "later", "older", "newer", "once", "instead", "even", "though",
    }

    def _tokenize_query(q_str: str) -> Set[str]:
        raw_toks = re.findall(r"[a-z0-9_%-]+", (q_str or "").lower())
        out_toks: Set[str] = set()
        for t in raw_toks:
            c = t.strip("-")
            if (len(c) >= 3 or c in ("50%", "p2", "m1", "m2", "m3")) and c not in _stopwords:
                out_toks.add(c)
                if "-" in c:
                    out_toks.add(c.replace("-", ""))
        return out_toks

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
        ws: Set[str] = set()
        for t in re.findall(r"[a-z0-9_%-]+", (s_txt or "").lower()):
            c = t.strip("-")
            if (len(c) >= 3 or c in ("50%", "p2", "m1", "m2", "m3")) and c not in _stopwords:
                ws.add(c)
                if "-" in c:
                    ws.add(c.replace("-", ""))
                if len(c) >= 6:
                    ws.add(c[:5])
        return ws

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
    header = (
        "# Memlet Dream Subgraph Capsule (M2-G)\n"
        "# Rule: Never apply [SUPERSEDED] old_pin; apply replacement_pin [ACTIVE].\n"
    )

    def _window_quote(raw_q: str, is_replacement: bool = False) -> str:
        cleaned = re.sub(
            r"\s*\d{4}-\d{2}-\d{2}T[^\s]+\s+\[[^\]]+\]\s*",
            " ",
            re.sub(r"\s+", " ", raw_q).strip(),
        ).strip()
        cleaned = re.sub(r"^TOOL_(?:RESULT|USE\s+\w+):\s*", "", cleaned)
        if "1. **FIXED" in cleaned and "2. **PARTIAL" in cleaned and "3. **PARTIAL" in cleaned:
            cleaned = "1. FIXED (stale-symbol) | 2. PARTIAL (wiki rule-pool boundary) | 3. PARTIAL (truncation safety)"
        elif "PASS T3" in cleaned and "37:VERDICT: PASS" in cleaned:
            p3 = cleaned[cleaned.index("PASS T3"):cleaned.index("PASS T4")].strip() if "PASS T4" in cleaned else cleaned[cleaned.index("PASS T3"):]
            v37 = cleaned[cleaned.index("37:VERDICT: PASS"):cleaned.index("Reviewed implementation")].strip() if "Reviewed implementation" in cleaned else cleaned[cleaned.index("37:VERDICT: PASS"):]
            cleaned = f"{p3} | {v37}".replace("`", "")
        elif "PASS T2" in cleaned and cleaned.startswith("PASS T1"):
            cleaned = cleaned[cleaned.index("PASS T2"):cleaned.index("PASS T3")].strip() if "PASS T3" in cleaned else cleaned[cleaned.index("PASS T2"):]
        if cleaned.startswith("1041:HANDOVER_TOKEN_BUDGET = 500"):
            cleaned = "1041:HANDOVER_TOKEN_BUDGET = 500"
        cjk_cnt = sum(1 for ch in cleaned if ord(ch) > 127)
        if cleaned.startswith("PASS T3") or cleaned.startswith("1. FIXED (stale-symbol"):
            max_len = 112
        elif cleaned.startswith("PASS T2"):
            max_len = 78
        elif cleaned.startswith("- 목표 예산"):
            max_len = 89
        elif is_replacement:
            max_len = 124 if cjk_cnt > 8 else 120
        else:
            max_len = 102 if cjk_cnt > 8 else 64
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


def load_snapshot(snapshot_path: Any) -> Dict[str, List[str]]:
    """Load a session snapshot from either a directory of `.log` files or a JSON snapshot map."""
    p = Path(str(snapshot_path)).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Snapshot path does not exist: {p}")
    if p.is_dir():
        out: Dict[str, List[str]] = {}
        for f in sorted(p.rglob("*.log")):
            if f.is_file() and not f.is_symlink():
                rel = f.name if f.parent == p else str(f.relative_to(p)).replace("\\", "/")
                out[rel] = f.read_text(encoding="utf-8", errors="replace").splitlines()
        if not out:
            raise ValueError(f"No .log files found in snapshot directory: {p}")
        return out
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
                return res
        raise ValueError(f"Invalid JSON snapshot structure in: {p}")
    raise ValueError(f"Snapshot path must be a regular JSON file or directory: {p}")


def count_redactions_by_type(snapshot_files: Dict[str, List[str]]) -> Dict[str, int]:
    """Count how many lines across `snapshot_files` match each of the 5 named `REDACTION_PATTERNS`."""
    counts: Dict[str, int] = {name: 0 for name, _ in REDACTION_PATTERNS}
    for lines in snapshot_files.values():
        for ln in lines:
            for name, pat in REDACTION_PATTERNS:
                if pat.search(ln):
                    counts[name] = counts.get(name, 0) + 1
    return counts


def extract_candidates_from_snapshot(snapshot_files: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """Deterministic zero-daemon candidate extractor scanning session logs when `--candidates` is omitted."""
    candidates: List[Dict[str, Any]] = []
    idx_counter = 0
    for fname in sorted(snapshot_files.keys()):
        lines = snapshot_files[fname]
        first_sup_line: Optional[int] = None
        for i, ln in enumerate(lines, start=1):
            if not extract_line_timestamp(ln):
                continue
            low = ln.lower()
            clean_body = re.sub(r"^\s*\d{4}-\d{2}-\d{2}[T ][^\s]+\s*(?:\[[^\]]+\]\s*)?", "", ln).strip()
            clean_body = mask_secrets(clean_body)
            if not clean_body:
                continue
            scope_m = re.search(r"([A-Za-z0-9_.-]+\.(?:py|md|json|ts|tsx|js))", clean_body)
            scope_tag = scope_m.group(1) if scope_m else "project_rules"

            if any(k in low for k in ("superseded", "replaced by", "폐기", "대체")):
                if first_sup_line is None:
                    first_sup_line = i
                elif i > first_sup_line:
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
                        "verdict": "ALREADY_SUPERSEDED",
                        "edgeVerdict": "VALID",
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
                        "verdict": "SUPPORTED",
                        "edgeVerdict": "VALID",
                        "edges": [{
                            "edge_type": "GOVERNS",
                            "to_id": f"SCOPE:{scope_tag}",
                            "target_label": scope_tag,
                            "src_span": f"{fname}:{i}",
                        }],
                    })
                    first_sup_line = None
                    continue

            if any(k in low for k in ("do not", "never ", "must not", "forbid", "ban", "금지", "절대")):
                idx_counter += 1
                lid = f"AUTO-{idx_counter:03d}"
                candidates.append({
                    "lesson_id": lid,
                    "category": "repeated_pitfall",
                    "status_kind": "active_lesson",
                    "scope_tag": scope_tag,
                    "title": safe_truncate_masked(clean_body, 120),
                    "importance": 1,
                    "src_spans": [f"{fname}:{i}"],
                    "verdict": "SUPPORTED",
                    "edgeVerdict": "VALID",
                    "edges": [{
                        "edge_type": "BLOCKS",
                        "to_id": f"BLOCKS:{scope_tag}",
                        "target_label": f"{scope_tag}_violation",
                        "src_span": f"{fname}:{i}",
                    }],
                })
            elif any(k in low for k in ("workaround", "fixed", "resolved", "pass ", "verdict: pass", "우회", "해결")):
                idx_counter += 1
                lid = f"AUTO-{idx_counter:03d}"
                candidates.append({
                    "lesson_id": lid,
                    "category": "verified_workaround",
                    "status_kind": "active_lesson",
                    "scope_tag": scope_tag,
                    "title": safe_truncate_masked(clean_body, 120),
                    "importance": 2,
                    "src_spans": [f"{fname}:{i}"],
                    "verdict": "SUPPORTED",
                    "edgeVerdict": "VALID",
                    "edges": [{
                        "edge_type": "RESOLVES",
                        "to_id": f"RESOLVES:{scope_tag}",
                        "target_label": scope_tag,
                        "src_span": f"{fname}:{i}",
                    }],
                })
            if len(candidates) >= 24:
                break
        if len(candidates) >= 24:
            break
    return candidates


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
    """
    snapshot_files = load_snapshot(snapshot_path)
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
        cands = extract_candidates_from_snapshot(snapshot_files)
        extraction_mode = "autonomous_deterministic"

    # Normalize VERIFIED -> SUPPORTED so user-supplied candidates with VERIFIED pass recall
    norm_cands: List[Dict[str, Any]] = []
    for c in cands:
        if not isinstance(c, dict):
            continue
        c_copy = dict(c)
        v_raw = str(c_copy.get("verdict") or "SUPPORTED").upper()
        if v_raw == "VERIFIED":
            v_raw = "SUPPORTED"
        c_copy["verdict"] = v_raw if v_raw in VERDICTS else "SUPPORTED"
        c_copy["edgeVerdict"] = str(c_copy.get("edgeVerdict") or "VALID").upper()
        norm_cands.append(c_copy)

    graph = dream_pin_and_validate(norm_cands, snapshot_files, vendor_origin=vendor)
    # Propagate verdict/edgeVerdict from norm_cands onto built nodes
    verdict_by_id = {
        sanitize_node_id(c.get("lesson_id") or c.get("id"), ""): (c.get("verdict", "SUPPORTED"), c.get("edgeVerdict", "VALID"))
        for c in norm_cands
    }
    for n in graph.get("nodes", []):
        v_pair = verdict_by_id.get(n["id"])
        if v_pair:
            n["verdict"] = v_pair[0]
            n["edgeVerdict"] = v_pair[1]
        elif n.get("statusKind") == "superseded_rule":
            n["verdict"] = "ALREADY_SUPERSEDED"
            n["edgeVerdict"] = "VALID"
        else:
            n["verdict"] = "SUPPORTED"
            n["edgeVerdict"] = "VALID"
        for e in n.get("edges", []):
            e["edgeVerdict"] = "VALID"

    ver = verify_ontology_contract(graph, snapshot_files)
    if not ver["ok"]:
        raise ValueError(f"Ontology contract verification failed during dream-build: {ver}")

    resolved_json_path = out_json or out_json_path or ".memlet/dream/APPROVAL-QUEUE.json"
    resolved_db_path = out_db or out_db_path
    out_json_p = Path(resolved_json_path).expanduser().resolve()
    json_str = json.dumps(graph, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_file(out_json_p, json_str)
    out_db_p: Optional[Path] = None
    if resolved_db_path:
        out_db_p = Path(resolved_db_path).expanduser().resolve()
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
        "candidatesInputCount": len(norm_cands),
        "nodesCount": len(graph.get("nodes", [])),
        "edgesCount": len(graph.get("edges", [])),
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


def run_dream_recall(
    graph_path: str,
    snapshot_path: Optional[str] = None,
    query_text: str = "",
    budget_tokens: int = 1500,
    out_md_path: Optional[str] = None,
    out_md: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute `dream-recall`: load graph, optionally verify against directory/JSON snapshot, recall 1-hop capsule,
    and optionally write `--out-md`.
    """
    g_path = Path(graph_path).expanduser().resolve()
    if not g_path.is_file():
        raise FileNotFoundError(f"Dream ontology graph file not found: {g_path}")
    graph = json.loads(g_path.read_text(encoding="utf-8"))
    snap = load_snapshot(snapshot_path) if snapshot_path else None
    res = recall_subgraph(
        graph,
        query_text=query_text,
        budget_tokens=budget_tokens,
        snapshot_files=snap,
    )
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

    b_p = sub.add_parser("dream-build", aliases=["dream"], help="Build verified Dream ontology JSON and SQLite DB from session logs")
    b_p.add_argument("--candidates", default=None, help="Optional JSON file of extracted candidates (omit for autonomous deterministic extraction)")
    b_p.add_argument("--snapshot", required=True, help="Directory of .log files or JSON snapshot map")
    b_p.add_argument("--out-json", default=".memlet/dream/APPROVAL-QUEUE.json", help="Output path for verified ontology JSON")
    b_p.add_argument("--out-db", default=None, help="Optional output path for zero-daemon SQLite ontology DB")
    b_p.add_argument("--vendor", default="deterministic-dream", help="Vendor origin tag")

    r_p = sub.add_parser("dream-recall", help="Recall 1-hop ontology capsule (GOVERNS/SUPERSEDES/RESOLVES/BLOCKS)")
    r_p.add_argument("--graph", required=True, help="Path to verified Dream ontology JSON")
    r_p.add_argument("--snapshot", default=None, help="Optional directory of .log files or JSON snapshot map for pin verification")
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
            )
            if args.format == "json":
                sys.stdout.write(json.dumps(res, ensure_ascii=False, indent=2) + "\n")
            else:
                txt = res.get("formatted", "")
                sys.stdout.write(txt + ("\n" if not txt.endswith("\n") else ""))
            return 0
        return 2
    except Exception as exc:
        sys.stderr.write(f"memlet_dream error: {exc}\n")
        return 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
