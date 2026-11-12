"""Memlet 9.4 `dream --tidy`: a model you name points out lessons that may say the same thing in different words.

The model only points (by short batch-local row ids); code checks against the shared reader, the tidy report's
wording duplicates and `merge_blockers`; a person merges (`memlet learn --merge`). Nothing here merges, hides,
retires, approves or changes any lesson, and no lesson text or model prose is ever stored in `.memlet/`.
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import memlet_bridge as mb
from . import memlet_dream as md
from . import memlet_dreamcycle as dc
from . import memlet_tidy as mt
from . import memlet_wiki as mw
from . import vendor_cli as vc
from . import vocabulary as v
from .vocabulary import APPROVED, HINT, PENDING

PROPOSALS_REL = dc.RUNS_REL + "/tidy-proposals.json"
DEFAULT_MAX_BATCHES = 2
MAX_BATCHES = 10
MAX_BATCH_LESSONS = 60
MAX_BATCH_CHARS = 24000
MAX_GROUPS_PER_BATCH = 30
MAX_CALLS = 32
MAX_WALL_SECONDS = 300
MAX_DROPPED_RECORDS = 100
MAX_DROPPED_BYTES = 32768
MAX_STORE_BYTES = 4 * 1024 * 1024

PROPOSE_PROMPT = v.DREAM_TIDY_PROPOSE_PROMPT
VERIFY_PROMPT = v.DREAM_TIDY_VERIFY_PROMPT

assert not PROPOSE_PROMPT.startswith("-") and not VERIFY_PROMPT.startswith("-")

_PROPOSAL_ID_RE = re.compile(r"\ATDP-[0-9a-f]{16}\Z")
_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_BATCH_ID_RE = re.compile(r"\A[0-9a-f]{24}\Z")
_LESSON_ID_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,64}\Z")
_GEN_LESSON_ID_RE = re.compile(r"\ADRM-[0-9a-f]{12,16}\Z")
_MALFORMED_DROP_REASONS = frozenset(("invalid-group", "unknown-row-id", "fewer-than-two", "duplicate-row-id"))
_STORE_TOP_KEYS = frozenset((
    "schema",
    "proposals",
    "dismissed",
    "droppedGroups",
    "droppedGroupsOmitted",
    "plan",
    "completedBatches",
    "examinedBatches",
    "failedBatches",
    "lastRun",
))
_PROPOSAL_KEYS = frozenset((
    "id",
    "lessonIds",
    "lessonDigests",
    "keep",
    "proposer",
    "verifier",
    "verdict",
    "receipts",
    "dismissed",
))
_RECEIPT_KEYS = frozenset((
    "vendor",
    "origin",
    "model",
    "isolation",
    "runId",
    "at",
    "kind",
    "proposalId",
    "digest",
    "verdict",
    "receiptSha256",
))
_RECEIPT_REQUIRED_KEYS = _RECEIPT_KEYS - frozenset(("verdict",))
_DROPPED_KEYS = frozenset(("batchId", "index", "reason", "reasonMeaning", "lessonIds", "blockers"))


# ---------------------------------------------------------------------------------------------------------------
# digests, receipts and strict record validation


def proposal_digest(lesson_digests: Iterable[str]) -> str:
    return dc._hash(sorted(str(d) for d in lesson_digests))


def proposal_id(lesson_digests: Iterable[str]) -> str:
    return "TDP-" + proposal_digest(lesson_digests)[:16]


def _valid_lesson_id(val: Any) -> bool:
    if type(val) is not str or not _LESSON_ID_RE.fullmatch(val):
        return False
    up = val.upper()
    return not any(up.startswith(pfx) for pfx in md.RESERVED_ID_PREFIXES) and not up.endswith(":NEWER")


def _make_receipt(
    vendor: str,
    model: str,
    isolation: str,
    run_id: str,
    pid: str,
    digest: str,
    kind: str,
    *,
    verdict: Optional[str] = None,
) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "vendor": vendor,
        "origin": "vendor-cli",
        "model": model,
        "isolation": isolation,
        "runId": run_id,
        "at": md._utc_now_iso(),
        "kind": kind,
        "proposalId": pid,
        "digest": digest,
    }
    if verdict is not None:
        rec["verdict"] = verdict
    return dc.seal_receipt(rec)


def valid_tidy_receipt(
    record: Any,
    pid: str,
    digest: str,
    kind: str,
    *,
    vendor: Optional[str] = None,
    model: Optional[str] = None,
    verdict: Optional[str] = None,
) -> bool:
    try:
        if type(record) is not dict:
            return False
        if not (set(record) <= _RECEIPT_KEYS and _RECEIPT_REQUIRED_KEYS <= set(record)):
            return False
        if any(type(k) is not str or type(val) is not str for k, val in record.items()):
            return False
        if kind == "propose" and "verdict" in record:
            return False
        if kind == "verify" and record.get("verdict") not in v.DREAM_TIDY_VERDICTS:
            return False
        body = {k: val for k, val in record.items() if k != "receiptSha256"}
        return (
            record.get("origin") == "vendor-cli"
            and record.get("vendor") in vc.VENDORS
            and record.get("isolation") == "tool-free"
            and vc.valid_model(record.get("model"))
            and md._valid_utc_stamp(record.get("at"))
            and bool(_BATCH_ID_RE.fullmatch(record["runId"]))
            and record.get("kind") == kind
            and record.get("proposalId") == pid
            and record.get("digest") == digest
            and (vendor is None or record.get("vendor") == vendor)
            and (model is None or record.get("model") == model)
            and (verdict is None or record.get("verdict") == verdict)
            and record.get("receiptSha256") == dc._hash(body)
        )
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


def _valid_proposal_record(proposal: Any) -> bool:
    if type(proposal) is not dict:
        return False
    if not (set(proposal) <= _PROPOSAL_KEYS and {"id", "lessonIds", "lessonDigests", "proposer", "receipts"} <= set(proposal)):
        return False
    if "dismissed" in proposal and type(proposal["dismissed"]) is not bool:
        return False
    if "keep" in proposal and proposal["keep"] is not None:
        if type(proposal["keep"]) is not str or len(proposal["keep"]) > 200:
            return False
    pid = proposal.get("id")
    lesson_ids = proposal.get("lessonIds")
    digests = proposal.get("lessonDigests")
    proposer = proposal.get("proposer")
    verifier = proposal.get("verifier")
    verdict = proposal.get("verdict")
    receipts = proposal.get("receipts")
    if (
        type(pid) is not str
        or not _PROPOSAL_ID_RE.fullmatch(pid)
        or type(lesson_ids) is not list
        or not 2 <= len(lesson_ids) <= MAX_BATCH_LESSONS
        or any(not _valid_lesson_id(x) for x in lesson_ids)
        or len(set(lesson_ids)) != len(lesson_ids)
        or type(digests) is not dict
        or len(digests) != len(lesson_ids)
        or any(type(k) is not str or type(val) is not str or not _DIGEST_RE.fullmatch(val) for k, val in digests.items())
        or set(digests) != set(lesson_ids)
    ):
        return False
    expected_digest = proposal_digest(digests.values())
    if pid != "TDP-" + expected_digest[:16]:
        return False
    if (
        type(proposer) is not dict
        or set(proposer) != {"vendor", "model"}
        or type(proposer["vendor"]) is not str
        or proposer["vendor"] not in vc.VENDORS
        or type(proposer["model"]) is not str
        or not vc.valid_model(proposer["model"])
        or type(receipts) is not list
        or not 1 <= len(receipts) <= 64
        or any(
            type(r) is not dict
            or r.get("kind") not in ("propose", "verify")
            or not valid_tidy_receipt(r, pid, expected_digest, r["kind"])
            for r in receipts
        )
        or not any(
            valid_tidy_receipt(
                r,
                pid,
                expected_digest,
                "propose",
                vendor=proposer["vendor"],
                model=proposer["model"],
            )
            for r in receipts
        )
    ):
        return False
    if verifier is None and verdict is None:
        return True
    if (
        type(verifier) is not dict
        or set(verifier) != {"vendor", "model", "verdict"}
        or type(verifier["vendor"]) is not str
        or verifier["vendor"] not in vc.VENDORS
        or type(verifier["model"]) is not str
        or not vc.valid_model(verifier["model"])
        or type(verdict) is not str
        or verdict not in v.DREAM_TIDY_VERDICTS
        or verifier["verdict"] != verdict
        or vc.same_model(proposer["vendor"], proposer["model"], verifier["vendor"], verifier["model"])
        or not any(
            valid_tidy_receipt(
                r,
                pid,
                expected_digest,
                "verify",
                vendor=verifier["vendor"],
                model=verifier["model"],
                verdict=verdict,
            )
            for r in receipts
        )
    ):
        return False
    return True


def _clean_dropped_group(rec: Any, known_ids: Optional[Set[str]] = None) -> Optional[Dict[str, Any]]:
    if type(rec) is not dict:
        return None
    if not (set(rec) <= _DROPPED_KEYS and {"batchId", "index", "reason", "lessonIds"} <= set(rec)):
        return None
    batch_id = rec["batchId"]
    index = rec["index"]
    reason = rec["reason"]
    lesson_ids = rec["lessonIds"]
    if (
        type(batch_id) is not str
        or not _BATCH_ID_RE.fullmatch(batch_id)
        or type(index) is not int
        or not 0 <= index <= MAX_GROUPS_PER_BATCH
        or type(reason) is not str
        or reason not in v.DREAM_TIDY_DROP_REASONS
        or ("reasonMeaning" in rec and type(rec["reasonMeaning"]) is not str)
        or type(lesson_ids) is not list
        or len(lesson_ids) > MAX_BATCH_LESSONS * 2
        or any(
            not _valid_lesson_id(x)
            or (known_ids is not None and x not in known_ids and not _GEN_LESSON_ID_RE.fullmatch(x))
            for x in lesson_ids
        )
    ):
        return None
    clean: Dict[str, Any] = {
        "batchId": batch_id,
        "index": index if index >= 1 else 1,
        "reason": reason,
        "reasonMeaning": v.DREAM_TIDY_DROP_REASONS[reason],
        "lessonIds": list(lesson_ids),
    }
    if "blockers" in rec:
        blockers = rec["blockers"]
        if type(blockers) is not dict:
            return None
        clean_blockers: Dict[str, List[str]] = {}
        for lid, codes in blockers.items():
            if (
                not _valid_lesson_id(lid)
                or (known_ids is not None and lid not in known_ids and not _GEN_LESSON_ID_RE.fullmatch(lid))
                or type(codes) is not list
                or len(codes) > 16
                or any(type(c) is not str or c not in v.MERGE_REASONS for c in codes)
            ):
                return None
            if codes:
                clean_blockers[lid] = list(codes)
        if clean_blockers:
            clean["blockers"] = clean_blockers
    return clean


def _clean_failed_batch(bid: Any, rec: Any) -> Optional[Dict[str, Any]]:
    if (
        type(bid) is not str
        or not _BATCH_ID_RE.fullmatch(bid)
        or type(rec) is not dict
        or set(rec) != {"vendor", "model", "reason", "at"}
        or type(rec["vendor"]) is not str
        or rec["vendor"] not in vc.VENDORS
        or type(rec["model"]) is not str
        or not vc.valid_model(rec["model"])
        or type(rec["reason"]) is not str
        or rec["reason"] not in v.DREAM_TIDY_BATCH_FAILURE_REASONS
        or type(rec["at"]) is not str
        or not md._valid_utc_stamp(rec["at"])
    ):
        return None
    return {
        "batchId": bid,
        "vendor": rec["vendor"],
        "model": rec["model"],
        "reason": rec["reason"],
        "reasonMeaning": v.DREAM_TIDY_BATCH_FAILURE_REASONS[rec["reason"]],
        "at": rec["at"],
    }


# ---------------------------------------------------------------------------------------------------------------
# the ONE proposal truth predicate


def proposal_state(
    proposal: Any,
    lessons_by_id: Dict[str, Dict[str, Any]],
    *,
    wording_groups: Optional[Sequence[Set[str]]] = None,
) -> Dict[str, Any]:
    """Decide on every read whether `proposal` is still true to the queue.

    Returns `{"state": "open"|"stale"|"merged"|"dismissed", "reason": code_or_None}`.
    """
    if not _valid_proposal_record(proposal):
        return {"state": "stale", "reason": "invalid-proposal"}
    if proposal.get("dismissed") is True:
        return {"state": "dismissed", "reason": "dismissed"}
    lesson_ids = proposal["lessonIds"]
    digests = proposal["lessonDigests"]
    if not isinstance(lessons_by_id, dict) or any(
        lid not in lessons_by_id
        or not isinstance(lessons_by_id[lid], dict)
        or not lessons_by_id[lid].get("exists", True)
        for lid in lesson_ids
    ):
        return {"state": "stale", "reason": "lesson-missing"}
    if any(lessons_by_id[lid].get("digest") != digests[lid] for lid in lesson_ids):
        return {"state": "stale", "reason": "lesson-changed"}
    roots = {lessons_by_id[lid].get("mergedInto") or lid for lid in lesson_ids}
    if len(roots) == 1:
        return {"state": "merged", "reason": "already-merged"}
    if any(not lessons_by_id[lid].get("shown") for lid in lesson_ids):
        return {"state": "stale", "reason": "lesson-not-shown"}
    if wording_groups is not None and any(set(lesson_ids).issubset(w) for w in wording_groups):
        return {"state": "stale", "reason": "already-in-tidy"}
    return {"state": "open", "reason": None}


# ---------------------------------------------------------------------------------------------------------------
# reader projection & store helpers


def _current_reader_lines(lesson: Dict[str, Any]) -> List[str]:
    lines = lesson.get("lines") or []
    if lines:
        return [ln["quote"] for ln in lines if ln.get("role") != "replaced" and ln.get("quote")]
    item = lesson.get("item") or {}
    quotes = item.get("quoteLines") or ([item["text"]] if item.get("text") else [])
    roles = item.get("lineRoles") or ["current"] * len(quotes)
    return [q for i, q in enumerate(quotes) if (roles[i] if i < len(roles) else "current") != "replaced" and q]


def _duplicate_node_ids(graph: Dict[str, Any]) -> Set[str]:
    seen: Set[str] = set()
    dups: Set[str] = set()
    for node in (graph or {}).get("nodes", []):
        if isinstance(node, dict) and isinstance(node.get("id"), str):
            nid = node["id"]
            if nid in seen:
                dups.add(nid)
            seen.add(nid)
    return dups


def lessons_by_id_from_loaded(
    lessons: Dict[str, Dict[str, Any]],
    nodes: Dict[str, Dict[str, Any]],
    merge: Dict[str, Any],
    *,
    duplicate_ids: Iterable[str] = (),
) -> Dict[str, Dict[str, Any]]:
    dups = set(duplicate_ids)
    holding = mt._holding_members(merge) if merge.get("available") else {}
    out: Dict[str, Dict[str, Any]] = {}
    for lid, node in nodes.items():
        if lid in dups or not isinstance(node, dict):
            continue
        lesson = lessons.get(lid)
        current_quotes = _current_reader_lines(lesson) if lesson is not None else []
        shown = bool(
            lesson is not None
            and lesson.get("tier") in (APPROVED, HINT, PENDING)
            and lid not in holding
            and current_quotes
        )
        out[lid] = {
            "id": lid,
            "exists": True,
            "shown": shown,
            "digest": md.merge_approval_digest(node),
            "mergedInto": holding.get(lid),
            "tier": lesson.get("tier") if lesson is not None else None,
            "readerText": "\n".join(current_quotes),
        }
    return out


def _load_repository_lessons(root: Path) -> Dict[str, Any]:
    _, graph, why = mt._load(root)
    loaded = mt._load_lessons(root, graph)
    if not loaded["ok"]:
        error = "; ".join(loaded["notices"]) or v.GRAPH_EVIDENCE_ERROR
        raise ValueError(error)
    merge = mt.merge_checks(root, loaded["nodes"], context={"graph": graph})
    dups = _duplicate_node_ids(graph)
    by_id = lessons_by_id_from_loaded(loaded["lessons"], loaded["nodes"], merge, duplicate_ids=dups)
    notices = ([why] if why else []) + [n for n in loaded["notices"] if n != why]
    if merge.get("notice"):
        notices.append(merge["notice"])
    return {
        "graph": graph,
        "lessons": loaded["lessons"],
        "nodes": loaded["nodes"],
        "merge": merge,
        "lessonsById": by_id,
        "notices": notices,
    }


def load_lessons_by_id(repo_path: Any = ".") -> Dict[str, Dict[str, Any]]:
    root = mw._resolve_root(repo_path)
    return _load_repository_lessons(root)["lessonsById"]


def _empty_store() -> Dict[str, Any]:
    return {
        "schema": v.DREAM_TIDY_SCHEMA,
        "proposals": [],
        "dismissed": [],
        "droppedGroups": [],
        "droppedGroupsOmitted": 0,
        "plan": [],
        "completedBatches": [],
        "examinedBatches": [],
        "failedBatches": {},
    }


def _sync_dismissed(store: Dict[str, Any]) -> Dict[str, Any]:
    dismissed = [x for x in store.get("dismissed", []) if type(x) is str and _PROPOSAL_ID_RE.fullmatch(x)]
    dismissed = list(dict.fromkeys(dismissed))
    dismissed_set = set(dismissed)
    proposals = []
    seen_valid_pids: Set[str] = set()
    for p in store.get("proposals", []):
        if type(p) is dict and _valid_proposal_record(p):
            pid = p["id"]
            if pid in seen_valid_pids:
                continue
            seen_valid_pids.add(pid)
            entry = copy.deepcopy(p)
            if pid in dismissed_set or entry.get("dismissed") is True:
                entry["dismissed"] = True
                if pid not in dismissed_set:
                    dismissed.append(pid)
                    dismissed_set.add(pid)
            proposals.append(entry)
    out = _empty_store()
    out["proposals"] = proposals
    out["dismissed"] = dismissed
    if type(store.get("droppedGroups")) is list:
        out["droppedGroups"] = [
            clean for rec in store["droppedGroups"] for clean in (_clean_dropped_group(rec),) if clean is not None
        ]
    if type(store.get("droppedGroupsOmitted")) is int and store["droppedGroupsOmitted"] >= 0:
        out["droppedGroupsOmitted"] = store["droppedGroupsOmitted"]
    if type(store.get("plan")) is list:
        out["plan"] = [
            {"batchId": rec["batchId"], "lessonCount": rec["lessonCount"]}
            for rec in store["plan"]
            if (
                type(rec) is dict
                and set(rec) == {"batchId", "lessonCount"}
                and type(rec["batchId"]) is str
                and bool(_BATCH_ID_RE.fullmatch(rec["batchId"]))
                and type(rec["lessonCount"]) is int
                and 0 <= rec["lessonCount"] <= MAX_BATCH_LESSONS
            )
        ]
    if type(store.get("completedBatches")) is list:
        out["completedBatches"] = list(dict.fromkeys(
            x for x in store["completedBatches"] if type(x) is str and bool(_BATCH_ID_RE.fullmatch(x))
        ))
    if type(store.get("examinedBatches")) is list:
        out["examinedBatches"] = list(dict.fromkeys(
            x for x in store["examinedBatches"] if type(x) is str and bool(_DIGEST_RE.fullmatch(x))
        ))
    if type(store.get("failedBatches")) is dict:
        clean_failed: Dict[str, Dict[str, Any]] = {}
        for bid, rec in store["failedBatches"].items():
            checked = _clean_failed_batch(bid, rec)
            if checked is not None:
                clean_failed[bid] = {k: checked[k] for k in ("vendor", "model", "reason", "at")}
        out["failedBatches"] = clean_failed
    if type(store.get("lastRun")) is dict:
        lr = store["lastRun"]
        if (
            set(lr) <= {"runId", "at", "batches", "calls", "failedBatches", "addedProposals", "unexaminedLessons", "alreadyExaminedLessons"}
            and type(lr.get("runId")) is str
            and bool(_BATCH_ID_RE.fullmatch(lr["runId"]))
            and type(lr.get("at")) is str
            and md._valid_utc_stamp(lr["at"])
            and all(
                type(lr.get(k, 0)) is int and lr.get(k, 0) >= 0
                for k in ("batches", "calls", "failedBatches", "addedProposals", "unexaminedLessons", "alreadyExaminedLessons")
            )
        ):
            out["lastRun"] = copy.deepcopy(lr)
    return out


def _existing_problem_path(root: Path, rel: str) -> Optional[Path]:
    """Walk `rel` with `lstat` only and return the first existing non-dir ancestor or bad leaf, else None."""
    cur = Path(root)
    parts = tuple(rel.split("/"))
    for idx, part in enumerate(parts):
        cur = cur / part
        try:
            st = os.lstat(str(cur))
        except FileNotFoundError:
            return None
        except OSError:
            return cur
        if stat.S_ISLNK(st.st_mode):
            return cur
        if idx < len(parts) - 1:
            if not stat.S_ISDIR(st.st_mode):
                return cur
        elif not stat.S_ISREG(st.st_mode):
            return cur
    return cur


def _read_bounded_store_text(target: Path) -> Tuple[str, Optional[str]]:
    """Bounded no-follow read of `target`: returns `("ok"|"absent"|"too-large"|"unreadable", text_or_None)`."""
    try:
        st_l = os.lstat(str(target))
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unreadable", None
    if stat.S_ISLNK(st_l.st_mode) or not stat.S_ISREG(st_l.st_mode):
        return "unreadable", None
    if st_l.st_size > MAX_STORE_BYTES:
        return "too-large", None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(target), flags)
    except (OSError, ValueError):
        return "unreadable", None
    try:
        st_f = os.fstat(fd)
        if not stat.S_ISREG(st_f.st_mode):
            return "unreadable", None
        if st_f.st_size > MAX_STORE_BYTES:
            return "too-large", None
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1 << 20, MAX_STORE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_STORE_BYTES:
                return "too-large", None
    except OSError:
        return "unreadable", None
    finally:
        os.close(fd)
    try:
        return "ok", b"".join(chunks).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "unreadable", None


def _load_store_for_read(root: Path, *, known_lesson_ids: Optional[Set[str]] = None) -> Dict[str, Any]:
    """Bounded, total read of `tidy-proposals.json` for read-only callers (`admin tidy` and `dream --tidy --status`)."""
    cur = Path(root)
    parts = tuple(PROPOSALS_REL.split("/"))
    for idx, part in enumerate(parts):
        cur = cur / part
        try:
            st = os.lstat(str(cur))
        except FileNotFoundError:
            return {"state": "absent"}
        except OSError:
            return {"state": "unreadable", "notice": v.TIDY_MODEL_PROPOSALS_UNREADABLE}
        if stat.S_ISLNK(st.st_mode):
            return {"state": "unreadable", "notice": v.TIDY_MODEL_PROPOSALS_UNREADABLE}
        if idx < len(parts) - 1:
            if not stat.S_ISDIR(st.st_mode):
                return {"state": "unreadable", "notice": v.TIDY_MODEL_PROPOSALS_UNREADABLE}
        elif not stat.S_ISREG(st.st_mode):
            return {"state": "unreadable", "notice": v.TIDY_MODEL_PROPOSALS_UNREADABLE}
    try:
        cur.parent.resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError, RuntimeError):
        return {"state": "unreadable", "notice": v.TIDY_MODEL_PROPOSALS_UNREADABLE}

    read_state, raw = _read_bounded_store_text(cur)
    if read_state == "absent":
        return {"state": "absent"}
    if read_state == "too-large":
        return {"state": "too-large", "notice": v.TIDY_MODEL_PROPOSALS_TOO_LARGE}
    if read_state != "ok" or raw is None:
        return {"state": "unreadable", "notice": v.TIDY_MODEL_PROPOSALS_UNREADABLE}
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError, TypeError, MemoryError):
        return {"state": "unreadable", "notice": v.TIDY_MODEL_PROPOSALS_UNREADABLE}
    if type(obj) is not dict or obj.get("schema") != v.DREAM_TIDY_SCHEMA:
        return {"state": "unreadable", "notice": v.TIDY_MODEL_PROPOSALS_UNREADABLE}
    if ("proposals" in obj and type(obj["proposals"]) is not list) or (
        "dismissed" in obj and type(obj["dismissed"]) is not list
    ):
        return {"state": "unreadable", "notice": v.TIDY_MODEL_PROPOSALS_UNREADABLE}

    invalid_records: Dict[str, int] = {}

    def bump_invalid(code: str, amount: int = 1) -> None:
        if amount > 0:
            invalid_records[code] = invalid_records.get(code, 0) + amount

    unknown_keys = set(obj) - _STORE_TOP_KEYS
    if unknown_keys:
        bump_invalid("invalid-store-field", len(unknown_keys))

    valid_dismissed: List[str] = []
    seen_dismissed: Set[str] = set()
    for x in obj.get("dismissed", []):
        if type(x) is str and _PROPOSAL_ID_RE.fullmatch(x):
            if x not in seen_dismissed:
                valid_dismissed.append(x)
                seen_dismissed.add(x)
        else:
            bump_invalid("invalid-dismissed")

    raw_proposals = obj.get("proposals", [])
    valid_proposals: List[Dict[str, Any]] = []
    seen_valid_pids: Set[str] = set()
    invalid_proposal_count = 0
    duplicate_proposal_count = 0
    for rec in raw_proposals:
        if not _valid_proposal_record(rec):
            invalid_proposal_count += 1
            continue
        if known_lesson_ids is not None and any(
            lid not in known_lesson_ids and not _GEN_LESSON_ID_RE.fullmatch(lid)
            for lid in rec["lessonIds"]
        ):
            invalid_proposal_count += 1
            continue
        pid = rec["id"]
        if pid in seen_valid_pids:
            duplicate_proposal_count += 1
            continue
        seen_valid_pids.add(pid)
        entry = copy.deepcopy(rec)
        if pid in seen_dismissed or entry.get("dismissed") is True:
            entry["dismissed"] = True
            if pid not in seen_dismissed:
                valid_dismissed.append(pid)
                seen_dismissed.add(pid)
        valid_proposals.append(entry)

    dropped_groups: List[Dict[str, Any]] = []
    if "droppedGroups" in obj:
        if type(obj["droppedGroups"]) is not list:
            bump_invalid("invalid-store-field")
        else:
            for rec in obj["droppedGroups"]:
                clean_d = _clean_dropped_group(rec, known_lesson_ids)
                if clean_d is None:
                    bump_invalid("invalid-dropped-group")
                else:
                    dropped_groups.append(clean_d)

    dropped_omitted = 0
    if "droppedGroupsOmitted" in obj:
        if type(obj["droppedGroupsOmitted"]) is int and obj["droppedGroupsOmitted"] >= 0:
            dropped_omitted = obj["droppedGroupsOmitted"]
        else:
            bump_invalid("invalid-store-field")

    failed_records: List[Dict[str, Any]] = []
    if "failedBatches" in obj:
        if type(obj["failedBatches"]) is not dict:
            bump_invalid("invalid-store-field")
        else:
            for bid in sorted(obj["failedBatches"], key=lambda k: k if type(k) is str else ""):
                clean_f = _clean_failed_batch(bid, obj["failedBatches"][bid])
                if clean_f is None:
                    bump_invalid("invalid-failed-batch")
                else:
                    failed_records.append(clean_f)

    for list_key in ("plan", "completedBatches", "examinedBatches"):
        if list_key in obj and type(obj[list_key]) is not list:
            bump_invalid("invalid-store-field")
    if "lastRun" in obj and type(obj["lastRun"]) is not dict:
        bump_invalid("invalid-store-field")

    return {
        "state": "ok",
        "proposals": valid_proposals,
        "dismissed": valid_dismissed,
        "droppedGroups": dropped_groups,
        "droppedGroupsOmitted": dropped_omitted,
        "failedRecords": failed_records,
        "invalidProposals": invalid_proposal_count,
        "duplicateProposals": duplicate_proposal_count,
        "invalidRecords": invalid_records,
    }


def _read_store(root: Path) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Return `(store_or_None, exists)`. Raises ValueError on symlink, unreadable or invalid store."""
    problem = mw.managed_path_problem(root, PROPOSALS_REL, leaf="file")
    if problem:
        raise ValueError(problem)
    target = root / PROPOSALS_REL
    if not os.path.lexists(str(target)):
        return None, False
    raw = mw.read_regular_text(target, MAX_STORE_BYTES, errors="strict")
    if raw is None:
        raise ValueError("Dream tidy proposals file is unreadable or exceeds bound")
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError, TypeError, MemoryError) as exc:
        raise ValueError("Dream tidy proposals file is invalid JSON") from exc
    if type(obj) is not dict or obj.get("schema") != v.DREAM_TIDY_SCHEMA:
        raise ValueError("Dream tidy proposals file has an invalid schema")
    if type(obj.get("proposals", [])) is not list or type(obj.get("dismissed", [])) is not list:
        raise ValueError("Dream tidy proposals file has invalid fields")
    return _sync_dismissed(obj), True


def _write_store(root: Path, store: Dict[str, Any]) -> None:
    if mw.managed_path_problem(root, dc.RUNS_REL, leaf="dir"):
        raise ValueError("unsafe Dream runs path")
    if mw.managed_path_problem(root, PROPOSALS_REL, leaf="file"):
        raise ValueError("unsafe Dream tidy proposals path")
    clean = _sync_dismissed(store)
    text = json.dumps(clean, ensure_ascii=False, indent=2) + "\n"
    if len(text.encode("utf-8")) > MAX_STORE_BYTES:
        raise ValueError("Dream tidy proposals file exceeds byte limit")
    mw.write_managed_text(root, PROPOSALS_REL, text, new_mode=0o600)


# ---------------------------------------------------------------------------------------------------------------
# batching and Item 3 group validation


def _row_json(row_id: str, text: str) -> str:
    return json.dumps({"id": row_id, "text": text}, ensure_ascii=False)


def _build_batches(
    lessons: Dict[str, Dict[str, Any]],
    lessons_by_id: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Deterministic batches of at most 60 shown lessons and 24,000 characters."""
    shown_ids = [lid for lid, info in lessons_by_id.items() if info.get("shown") and info.get("readerText")]
    ordered_ids = mt.kept_lesson_order(lessons, shown_ids)
    batches: List[Dict[str, Any]] = []
    oversized: List[str] = []
    cur_ids: List[str] = []
    cur_rows: List[str] = []
    cur_chars = 0

    def flush() -> None:
        nonlocal cur_ids, cur_rows, cur_chars
        if not cur_ids:
            return
        row_map = {"L%d" % (idx + 1): lid for idx, lid in enumerate(cur_ids)}
        stdin_text = "\n".join(cur_rows)
        digest = dc._hash([[lid, lessons_by_id[lid]["digest"], lessons_by_id[lid]["readerText"]] for lid in cur_ids])
        examined_digest = "sha256:" + dc._hash(sorted(lessons_by_id[lid]["digest"] for lid in cur_ids))
        batches.append({
            "batchId": digest[:24],
            "digest": digest,
            "examinedDigest": examined_digest,
            "lessonIds": list(cur_ids),
            "rowMap": row_map,
            "stdinText": stdin_text,
        })
        cur_ids, cur_rows, cur_chars = [], [], 0

    for lid in ordered_ids:
        text = lessons_by_id[lid]["readerText"]
        solo = _row_json("L1", text)
        if len(solo) > MAX_BATCH_CHARS:
            oversized.append(lid)
            continue
        candidate_row = _row_json("L%d" % (len(cur_ids) + 1), text)
        added_chars = len(candidate_row) if not cur_rows else (cur_chars + 1 + len(candidate_row))
        if len(cur_ids) >= MAX_BATCH_LESSONS or added_chars > MAX_BATCH_CHARS:
            flush()
            candidate_row = solo
            added_chars = len(solo)
        cur_ids.append(lid)
        cur_rows.append(candidate_row)
        cur_chars = added_chars

    flush()
    return batches, oversized


def _dropped_record(
    batch_id: str,
    index: int,
    reason: str,
    lesson_ids: Sequence[str],
    *,
    blockers: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "batchId": batch_id,
        "index": index,
        "reason": reason,
        "reasonMeaning": v.DREAM_TIDY_DROP_REASONS.get(reason, reason),
        "lessonIds": list(lesson_ids),
    }
    if blockers:
        rec["blockers"] = blockers
    return rec


def _keep_dropped(target: Dict[str, Any], records: Sequence[Dict[str, Any]], key: str = "droppedGroups") -> None:
    kept = target.setdefault(key, [])
    total_before = len(kept) + int(target.get("droppedGroupsOmitted", 0))
    for record in records:
        if (
            len(kept) < MAX_DROPPED_RECORDS
            and len(json.dumps(kept + [record], ensure_ascii=False).encode("utf-8")) <= MAX_DROPPED_BYTES
        ):
            kept.append(record)
    target["droppedGroupsOmitted"] = total_before + len(records) - len(kept)


def _check_batch_groups(
    raw_groups: List[Any],
    batch_rows: Dict[str, str],
    lessons: Dict[str, Dict[str, Any]],
    nodes: Dict[str, Dict[str, Any]],
    tidy_duplicate_sets: Sequence[Set[str]],
    merge: Dict[str, Any],
    dismissed_set: Set[str],
    batch_id: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool]:
    """Validate every group proposed for one batch (Item 3). Never persists model text."""
    dropped: List[Dict[str, Any]] = []
    stage1: List[Tuple[int, List[str]]] = []

    for idx, raw_group in enumerate(raw_groups, 1):
        if (
            type(raw_group) is not dict
            or type(raw_group.get("same")) is not list
            or len(raw_group["same"]) > MAX_BATCH_LESSONS * 2
            or any(type(x) is not str for x in raw_group["same"])
        ):
            reason = "invalid-group"
            dropped.append(_dropped_record(batch_id, idx, reason, []))
            continue
        raw_ids = raw_group["same"]
        known_lessons = [batch_rows[x] for x in dict.fromkeys(raw_ids) if x in batch_rows]
        if any(x not in batch_rows for x in raw_ids):
            reason = "unknown-row-id"
            dropped.append(_dropped_record(batch_id, idx, reason, known_lessons))
            continue
        distinct_rids = list(dict.fromkeys(raw_ids))
        if len(distinct_rids) < 2:
            reason = "fewer-than-two"
            dropped.append(_dropped_record(batch_id, idx, reason, known_lessons))
            continue
        stage1.append((idx, distinct_rids))

    rid_counts: Dict[str, int] = {}
    for _, rids in stage1:
        for rid in rids:
            rid_counts[rid] = rid_counts.get(rid, 0) + 1
    dup_rids = {rid for rid, cnt in rid_counts.items() if cnt > 1}

    stage2: List[Tuple[int, List[str]]] = []
    for idx, rids in stage1:
        lesson_ids = [batch_rows[rid] for rid in rids]
        if any(rid in dup_rids for rid in rids):
            reason = "duplicate-row-id"
            dropped.append(_dropped_record(batch_id, idx, reason, lesson_ids))
            continue
        stage2.append((idx, lesson_ids))

    all_malformed = False
    if bool(raw_groups) and not stage2:
        all_malformed = True

    accepted: List[Dict[str, Any]] = []
    for idx, lesson_ids in stage2:
        ordered = mt.kept_lesson_order(lessons, lesson_ids)
        keep, same = ordered[0], ordered[1:]
        group_set = set(ordered)
        if any(group_set <= dup_set for dup_set in tidy_duplicate_sets):
            reason = "already-in-tidy"
            dropped.append(_dropped_record(batch_id, idx, reason, ordered))
            continue
        checked = mt._checked_blockers(merge, keep, same) if merge.get("available") else None
        if checked is None or any(checked["blockers"].values()):
            reason = "merge-blocked"
            blocker_codes = {k: list(vals) for k, vals in (checked or {}).get("blockers", {}).items() if vals}
            dropped.append(_dropped_record(batch_id, idx, reason, ordered, blockers=blocker_codes))
            continue
        digests = {lid: md.merge_approval_digest(nodes[lid]) for lid in ordered}
        pid = proposal_id(digests.values())
        if pid in dismissed_set:
            reason = "dismissed"
            dropped.append(_dropped_record(batch_id, idx, reason, ordered))
            continue
        accepted.append({
            "id": pid,
            "digest": proposal_digest(digests.values()),
            "lessonIds": ordered,
            "lessonDigests": digests,
            "keep": keep,
        })

    dropped.sort(key=lambda r: r["index"])
    return accepted, dropped, all_malformed


# ---------------------------------------------------------------------------------------------------------------
# active commands: dream_tidy_run, dream_tidy_dismiss, dream_tidy_status


def _tidy_duplicate_sets(lessons: Dict[str, Dict[str, Any]], lessons_by_id: Dict[str, Dict[str, Any]]) -> List[Set[str]]:
    visible = [lid for lid, info in lessons_by_id.items() if info.get("shown") and lessons[lid].get("current")]
    groups, _ = mt._duplicates(lessons, visible, {"similarityWork": 0})
    return [{keep} | set(members) for _, keep, members in groups]


def _select_plan_batches(
    batches: List[Dict[str, Any]],
    store: Dict[str, Any],
    *,
    again: bool = False,
) -> Tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:
    """Skip batches whose sorted-lesson-digest hash is already recorded (unless `again`); retry failed batches first."""
    current_ids = [b["batchId"] for b in batches]
    prior_plan = [p.get("batchId") for p in store.get("plan", []) if isinstance(p, dict)]
    completed = set(store.get("completedBatches", []))
    failed = store.get("failedBatches", {}) if isinstance(store.get("failedBatches"), dict) else {}
    examined = set(store.get("examinedBatches", []))
    resuming = bool(prior_plan) and prior_plan == current_ids and (len(completed) < len(prior_plan) or bool(failed))
    skipped: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    for b in batches:
        bid = b["batchId"]
        if not again and bid not in failed and (b.get("examinedDigest") in examined or (resuming and bid in completed)):
            skipped.append(b)
        else:
            pending.append(b)
    retry_ids = [b["batchId"] for b in pending if b["batchId"] in failed]
    # Failed batches are retried first on the next explicit run, followed by remaining unexamined batches.
    pending.sort(key=lambda b: (b["batchId"] not in failed, current_ids.index(b["batchId"])))
    return pending, retry_ids, skipped


def _reason_counts(records: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for rec in records:
        code = rec.get("reason")
        if isinstance(code, str) and code:
            counts[code] = counts.get(code, 0) + 1
    return counts


def dream_tidy_run(
    repo: Any = ".",
    *,
    via: Sequence[str] = ("claude",),
    verify_with: Optional[str] = None,
    max_batches: int = DEFAULT_MAX_BATCHES,
    runner: Optional[Callable[..., str]] = None,
    dry_run: bool = False,
    again: bool = False,
    save_fixture: Optional[Any] = None,
    clock: Optional[Callable[[], float]] = None,
) -> Dict[str, Any]:
    now_fn = clock or time.monotonic
    started = now_fn()
    report: Dict[str, Any] = {
        "ok": True,
        "tidy": True,
        "calls": 0,
        "batches": 0,
        "failedBatches": 0,
        "droppedOutput": 0,
        "droppedGroups": [],
        "droppedGroupsOmitted": 0,
        "droppedGroupCounts": {},
        "proposals": [],
        "addedProposals": [],
        "verifiedProposals": 0,
        "disputedProposals": 0,
        "unexaminedLessons": 0,
        "alreadyExaminedLessons": 0,
        "errors": [],
        "notices": [],
        "limits": {
            "batches": max_batches,
            "calls": MAX_CALLS,
            "wallSeconds": MAX_WALL_SECONDS,
            "batchLessons": MAX_BATCH_LESSONS,
            "batchChars": MAX_BATCH_CHARS,
        },
        "dryRun": dry_run,
        "again": bool(again),
        "prompts": [],
        "callLog": [],
        "trustBoundary": v.DREAM_TIDY_TRUST_NOTICE,
    }
    captures: List[Tuple[Any, ...]] = []

    def capture(*args: Any) -> None:
        if args[0] is not None:
            captures.append(args)

    try:
        root, config = dc._repo(repo)
        if isinstance(via, str):
            vendors = [v_name.strip() for v_name in via.split(",") if v_name.strip()]
        else:
            vendors = list(dict.fromkeys(v_name.strip() for v_name in via if isinstance(v_name, str) and v_name.strip()))
        vendors = list(dict.fromkeys(vendors))
        if len(vendors) != 1:
            raise ValueError(v.DREAM_TIDY_SINGLE_VENDOR if len(vendors) > 1 else "unknown vendor")
        proposer_vendor = vendors[0]
        if proposer_vendor not in vc.VENDORS or verify_with not in (None,) + vc.VENDORS:
            raise ValueError("unknown vendor")
        if type(max_batches) is not int or not 1 <= max_batches <= MAX_BATCHES:
            raise ValueError("max_batches must be 1..%d" % MAX_BATCHES)

        dream_cfg = config.get("dream") if isinstance(config.get("dream"), dict) else {}
        models = dream_cfg.get("models") if isinstance(dream_cfg.get("models"), dict) else {}
        _, agreement, cfg_notices = mb.dream_settings(config)
        report["notices"].extend(cfg_notices)
        verifier_models = models.get("verifier") if isinstance(models.get("verifier"), dict) else {}
        proposer_model = models.get(proposer_vendor, vc.DEFAULT_MODELS[proposer_vendor])
        verifier_model = (
            verifier_models.get(verify_with)
            or vc.DEFAULT_VERIFIER_MODELS.get(verify_with)
            or models.get(verify_with, vc.DEFAULT_MODELS.get(verify_with))
            if verify_with
            else None
        )
        chosen: List[Tuple[str, Any]] = [(proposer_vendor, proposer_model)]
        if verify_with:
            chosen.append((verify_with, verifier_model))
        if any(not vc.valid_model(m) for _, m in chosen):
            raise ValueError("invalid model id; expected [A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}")
        if agreement == "model" or verify_with:
            for index, (vendor_name, model_name) in enumerate(chosen):
                if vendor_name in vc.MODEL_FAMILIES and vc.model_family(vendor_name, model_name) is None:
                    key = "dream.models.verifier." + vendor_name if index >= 1 else "dream.models." + vendor_name
                    raise ValueError(
                        key
                        + " must contain exactly one known family token under dream.agreement="
                        + agreement
                        + "; no recognisable family; accepted families: "
                        + ", ".join(vc.MODEL_FAMILIES[vendor_name])
                    )
        if verify_with and vc.same_model(proposer_vendor, proposer_model, verify_with, verifier_model):
            raise ValueError("dream.models.verifier has the same family as a proposer under dream.agreement=" + agreement)

        if runner is None:
            for vendor_name in dict.fromkeys(vn for vn, _ in chosen):
                refusal = vc.static_vendor_refusal(vendor_name)
                if refusal:
                    raise ValueError(refusal)
                if not dry_run:
                    checked = vc.check_vendor(
                        vendor_name,
                        config=config,
                        timeout=max(0.01, MAX_WALL_SECONDS - (now_fn() - started)),
                    )
                    if not checked["ok"]:
                        raise ValueError(checked["reason"])

        loaded = _load_repository_lessons(root)
        for note in loaded["notices"]:
            if note not in report["notices"]:
                report["notices"].append(note)
        lessons = loaded["lessons"]
        nodes = loaded["nodes"]
        merge = loaded["merge"]
        lessons_by_id = loaded["lessonsById"]

        batches, oversized = _build_batches(lessons, lessons_by_id)
        if oversized:
            report["notices"].append(v.DREAM_TIDY_OVERSIZED_NOTICE % len(oversized))

        existing_store, _ = _read_store(root)
        store = existing_store if existing_store is not None else _empty_store()
        pending_batches, retry_ids, skipped_batches = _select_plan_batches(batches, store, again=bool(again))
        skipped_lesson_count = sum(len(b["lessonIds"]) for b in skipped_batches)
        report["alreadyExaminedLessons"] = skipped_lesson_count
        if skipped_lesson_count:
            report["notices"].append(v.DREAM_TIDY_ALREADY_EXAMINED_NOTICE % skipped_lesson_count)

        all_batch_ids = [b["batchId"] for b in batches]
        all_batch_id_set = set(all_batch_ids)
        resuming = (
            not again
            and [p.get("batchId") for p in store.get("plan", []) if isinstance(p, dict)] == all_batch_ids
            and (len(set(store.get("completedBatches", []))) < len(all_batch_ids) or bool(store.get("failedBatches")))
        )
        already_covered_batch_ids = {b["batchId"] for b in skipped_batches}
        if resuming:
            already_covered_batch_ids |= set(store.get("completedBatches", []))

        run_id = dc._hash(["tidy", [b["batchId"] for b in batches], proposer_vendor, proposer_model, md._utc_now_iso()])[:24]
        report["runId"] = run_id

        def budget() -> bool:
            if report["calls"] >= MAX_CALLS or now_fn() - started >= MAX_WALL_SECONDS:
                report["budgetStopped"] = True
                return False
            return True

        def prompt_record(vendor_name: str, model_name: str, kind: str, prompt: str, data: str) -> Dict[str, Any]:
            assert not prompt.startswith("-")
            return {
                "vendor": vendor_name,
                "model": model_name,
                "kind": kind,
                "prompt": prompt,
                "stdinText": data,
                **vc.launch_plan(vendor_name, prompt, model_name),
            }

        def call_model(vendor_name: str, model_name: str, kind: str, prompt: str, data: str, **identity: Any) -> Dict[str, Any]:
            report["calls"] += 1
            res = vc.run_vendor(
                vendor_name,
                prompt,
                stdin_text=data,
                model=model_name,
                timeout=min(120.0, max(0.01, MAX_WALL_SECONDS - (now_fn() - started))),
                runner=runner,
                config=config,
            )
            report["callLog"].append(
                dict(kind=kind, **identity, **{k: res[k] for k in ("vendor", "model", "isolation", "ok")})
            )
            return res

        def verification_stdin(lesson_id_list: Sequence[str], by_id: Dict[str, Dict[str, Any]]) -> str:
            return "\n".join(
                _row_json("L%d" % idx, by_id[lid]["readerText"])
                for idx, lid in enumerate(lesson_id_list, 1)
                if lid in by_id and by_id[lid].get("readerText")
            )

        existing_unverified = []
        if verify_with:
            for p in store.get("proposals", []):
                if (
                    proposal_state(p, lessons_by_id)["state"] == "open"
                    and p.get("verifier") is None
                    and not vc.same_model(p["proposer"]["vendor"], p["proposer"]["model"], verify_with, verifier_model)
                ):
                    existing_unverified.append(p)

        if dry_run:
            report["retryFailedBatches"] = list(retry_ids[:max_batches])
            planned_batch_ids = set(already_covered_batch_ids)
            for p in existing_unverified[:MAX_CALLS]:
                report["prompts"].append(
                    prompt_record(
                        verify_with,
                        verifier_model,
                        "tidy-verify",
                        VERIFY_PROMPT,
                        verification_stdin(p["lessonIds"], lessons_by_id),
                    )
                )
            planned_existing_verify = len(report["prompts"])
            for batch in pending_batches:
                if report["batches"] >= max_batches or len(report["prompts"]) + 1 > MAX_CALLS:
                    break
                report["batches"] += 1
                planned_batch_ids.add(batch["batchId"])
                report["prompts"].append(
                    prompt_record(proposer_vendor, proposer_model, "tidy-propose", PROPOSE_PROMPT, batch["stdinText"])
                )
            covered = {lid for b in batches if b["batchId"] in planned_batch_ids for lid in b["lessonIds"]}
            all_shown = [lid for b in batches for lid in b["lessonIds"]]
            unexamined = len([lid for lid in all_shown if lid not in covered]) + len(oversized)
            report["unexaminedLessons"] = unexamined
            if unexamined:
                report["notices"].append(v.DREAM_TIDY_UNEXAMINED_NOTICE % unexamined)
            propose_calls = sum(p["kind"] == "tidy-propose" for p in report["prompts"])
            max_new_verify = min(MAX_CALLS - len(report["prompts"]), propose_calls * MAX_GROUPS_PER_BATCH) if verify_with else 0
            report.update(
                plannedProposalCalls=propose_calls,
                plannedExistingVerificationCalls=planned_existing_verify,
                verificationCallRange=[planned_existing_verify, planned_existing_verify + max_new_verify] if verify_with else [0, 0],
            )
            if verify_with:
                v_plan = vc.launch_plan(verify_with, VERIFY_PROMPT, verifier_model)
                report.update(
                    verificationPrompt=VERIFY_PROMPT,
                    verifierVendor=verify_with,
                    verifierModel=verifier_model,
                    verifierArgv=v_plan["argv"],
                    verifierIsolation=v_plan["isolation"],
                )
            report["done"] = not pending_batches and not existing_unverified
        else:
            if not batches and not existing_unverified:
                report["done"] = True
            else:
                examined_batch_ids: Set[str] = set(already_covered_batch_ids)
                attempted_verify: Set[str] = set()
                checked_same = 0
                checked_diff = 0

                with md.queue_lock(root / mb.QUEUE_REL):
                    live_store, _ = _read_store(root)
                    state_store = live_store if live_store is not None else _empty_store()
                    if not resuming:
                        state_store["plan"] = [{"batchId": b["batchId"], "lessonCount": len(b["lessonIds"])} for b in batches]
                        state_store["completedBatches"] = [b["batchId"] for b in skipped_batches]
                        state_store["failedBatches"] = {
                            k: val
                            for k, val in state_store.get("failedBatches", {}).items()
                            if k in all_batch_id_set
                        }
                    elif not state_store.get("plan"):
                        state_store["plan"] = [{"batchId": b["batchId"], "lessonCount": len(b["lessonIds"])} for b in batches]
                    _write_store(root, state_store)

                def verify_open_proposals() -> None:
                    nonlocal checked_same, checked_diff
                    if not verify_with:
                        return
                    cur_store, _ = _read_store(root)
                    cur_loaded = _load_repository_lessons(root)
                    cur_by_id = cur_loaded["lessonsById"]
                    for prop in (cur_store or {}).get("proposals", []):
                        pid = prop.get("id")
                        if not isinstance(pid, str) or pid in attempted_verify:
                            continue
                        if proposal_state(prop, cur_by_id)["state"] != "open" or prop.get("verifier") is not None:
                            continue
                        if vc.same_model(prop["proposer"]["vendor"], prop["proposer"]["model"], verify_with, verifier_model):
                            attempted_verify.add(pid)
                            report["notices"].append(
                                v.DREAM_TIDY_VERIFIER_SKIPPED_FAMILY % (pid, verify_with, verifier_model)
                            )
                            continue
                        if not budget():
                            break
                        attempted_verify.add(pid)
                        v_data = verification_stdin(prop["lessonIds"], cur_by_id)
                        v_res = call_model(verify_with, verifier_model, "tidy-verify", VERIFY_PROMPT, v_data, proposalId=pid)
                        try:
                            if not v_res["ok"]:
                                raise ValueError("vendor-error")
                            parsed_v = dc._model_json(v_res["text"])
                            if (
                                not isinstance(parsed_v, dict)
                                or set(parsed_v) != {"verdict"}
                                or parsed_v["verdict"] not in v.DREAM_TIDY_VERDICTS
                            ):
                                raise ValueError("invalid-verdict")
                            verdict_val = parsed_v["verdict"]
                        except ValueError:
                            capture(save_fixture, verify_with, verifier_model, "tidy-verify", v_data, v_res["text"], {"verdict": None})
                            report["droppedOutput"] += 1
                            report["notices"].append(
                                v.DREAM_TIDY_VERIFIER_DROPPED % (pid, verify_with, verifier_model)
                            )
                            continue
                        capture(save_fixture, verify_with, verifier_model, "tidy-verify", v_data, v_res["text"], {"verdict": verdict_val})
                        with md.queue_lock(root / mb.QUEUE_REL):
                            locked_store, _ = _read_store(root)
                            locked_store = locked_store or _empty_store()
                            locked_by_id = _load_repository_lessons(root)["lessonsById"]
                            target = next((x for x in locked_store["proposals"] if x.get("id") == pid), None)
                            if target is None or proposal_state(target, locked_by_id)["state"] != "open":
                                report["droppedOutput"] += 1
                                continue
                            p_digest = proposal_digest(target["lessonDigests"].values())
                            v_receipt = _make_receipt(
                                verify_with,
                                verifier_model,
                                v_res["isolation"],
                                run_id,
                                pid,
                                p_digest,
                                "verify",
                                verdict=verdict_val,
                            )
                            target["verdict"] = verdict_val
                            target["verifier"] = {"vendor": verify_with, "model": verifier_model, "verdict": verdict_val}
                            target.setdefault("receipts", []).append(v_receipt)
                            _write_store(root, locked_store)
                        report["verifiedProposals"] += 1
                        if verdict_val == "SAME":
                            checked_same += 1
                        else:
                            checked_diff += 1
                            report["disputedProposals"] += 1

                if verify_with:
                    verify_open_proposals()

                consecutive_failures = 0
                for batch in pending_batches:
                    if report["batches"] >= max_batches:
                        break
                    if not budget():
                        break
                    report["batches"] += 1
                    bid = batch["batchId"]
                    ex_digest = batch["examinedDigest"]
                    examined_batch_ids.add(bid)
                    res = call_model(
                        proposer_vendor,
                        proposer_model,
                        "tidy-propose",
                        PROPOSE_PROMPT,
                        batch["stdinText"],
                        batchId=bid,
                    )
                    failure_code: Optional[str] = None
                    raw_groups: List[Any] = []
                    if not res["ok"]:
                        failure_code = "vendor-error"
                    else:
                        try:
                            parsed = dc._model_json(res["text"], unwrap_items=True)
                            if not isinstance(parsed, list) or len(parsed) > MAX_GROUPS_PER_BATCH:
                                raise ValueError("expected bounded group array")
                            raw_groups = parsed
                        except ValueError:
                            failure_code = "malformed-output"

                    if failure_code is not None:
                        capture(
                            save_fixture,
                            proposer_vendor,
                            proposer_model,
                            "tidy-propose",
                            batch["stdinText"],
                            res["text"],
                            {"failed": True, "groups": [], "droppedReasons": []},
                        )
                        report["droppedOutput"] += 1
                        report["failedBatches"] += 1
                        report["errors"].append(v.DREAM_TIDY_BATCH_FAILURE_REASONS[failure_code])
                        with md.queue_lock(root / mb.QUEUE_REL):
                            locked_store, _ = _read_store(root)
                            locked_store = locked_store or _empty_store()
                            locked_store["examinedBatches"] = [
                                d for d in locked_store.get("examinedBatches", []) if d != ex_digest
                            ]
                            locked_store.setdefault("failedBatches", {})[bid] = {
                                "vendor": proposer_vendor,
                                "model": proposer_model,
                                "reason": failure_code,
                                "at": md._utc_now_iso(),
                            }
                            _write_store(root, locked_store)
                        examined_batch_ids.discard(bid)
                        consecutive_failures += 1
                        if consecutive_failures >= 3:
                            report["consecutiveFailureStopped"] = True
                            break
                        continue

                    with md.queue_lock(root / mb.QUEUE_REL):
                        locked_store, _ = _read_store(root)
                        locked_store = locked_store or _empty_store()
                        live_loaded = _load_repository_lessons(root)
                        live_lessons = live_loaded["lessons"]
                        live_nodes = live_loaded["nodes"]
                        live_merge = live_loaded["merge"]
                        live_by_id = live_loaded["lessonsById"]
                        live_dups = _tidy_duplicate_sets(live_lessons, live_by_id)
                        dismissed_set = set(locked_store.get("dismissed", []))
                        # Filter batch_rows to lessons still present & shown with unchanged digest
                        valid_batch_rows = {
                            rid: lid
                            for rid, lid in batch["rowMap"].items()
                            if (
                                lid in live_nodes
                                and live_by_id.get(lid, {}).get("shown")
                                and live_by_id.get(lid, {}).get("digest") == lessons_by_id.get(lid, {}).get("digest")
                            )
                        }
                        accepted, dropped, all_malformed = _check_batch_groups(
                            raw_groups,
                            valid_batch_rows,
                            live_lessons,
                            live_nodes,
                            live_dups,
                            live_merge,
                            dismissed_set,
                            bid,
                        )
                        _keep_dropped(locked_store, dropped)
                        _keep_dropped(report, dropped)
                        capture(
                            save_fixture,
                            proposer_vendor,
                            proposer_model,
                            "tidy-propose",
                            batch["stdinText"],
                            res["text"],
                            {
                                "failed": all_malformed,
                                "proposalCount": len(accepted),
                                "droppedReasons": [r["reason"] for r in dropped],
                            },
                        )
                        if all_malformed:
                            locked_store["examinedBatches"] = [
                                d for d in locked_store.get("examinedBatches", []) if d != ex_digest
                            ]
                            locked_store.setdefault("failedBatches", {})[bid] = {
                                "vendor": proposer_vendor,
                                "model": proposer_model,
                                "reason": "all-groups-malformed",
                                "at": md._utc_now_iso(),
                            }
                            _write_store(root, locked_store)
                        else:
                            by_pid = {p["id"]: p for p in locked_store.get("proposals", []) if isinstance(p, dict) and "id" in p}
                            for cand in accepted:
                                pid = cand["id"]
                                p_receipt = _make_receipt(
                                    proposer_vendor,
                                    proposer_model,
                                    res["isolation"],
                                    run_id,
                                    pid,
                                    cand["digest"],
                                    "propose",
                                )
                                if pid in by_pid and _valid_proposal_record(by_pid[pid]):
                                    existing_p = by_pid[pid]
                                    if not any(
                                        r.get("kind") == "propose"
                                        and r.get("vendor") == proposer_vendor
                                        and r.get("model") == proposer_model
                                        and r.get("digest") == cand["digest"]
                                        for r in existing_p.get("receipts", [])
                                        if isinstance(r, dict)
                                    ):
                                        existing_p.setdefault("receipts", []).append(p_receipt)
                                else:
                                    new_prop = {
                                        "id": pid,
                                        "lessonIds": cand["lessonIds"],
                                        "lessonDigests": cand["lessonDigests"],
                                        "keep": cand["keep"],
                                        "proposer": {"vendor": proposer_vendor, "model": proposer_model},
                                        "verifier": None,
                                        "verdict": None,
                                        "receipts": [p_receipt],
                                    }
                                    locked_store.setdefault("proposals", []).append(new_prop)
                                    by_pid[pid] = new_prop
                                    report["addedProposals"].append(pid)
                                if pid not in report["proposals"]:
                                    report["proposals"].append(pid)
                            if bid not in locked_store.setdefault("completedBatches", []):
                                locked_store["completedBatches"].append(bid)
                            if ex_digest not in locked_store.setdefault("examinedBatches", []):
                                locked_store["examinedBatches"].append(ex_digest)
                            locked_store.setdefault("failedBatches", {}).pop(bid, None)
                            _write_store(root, locked_store)

                    if all_malformed:
                        examined_batch_ids.discard(bid)
                        report["failedBatches"] += 1
                        report["errors"].append(v.DREAM_TIDY_ALL_DROPPED_ERROR)
                        consecutive_failures += 1
                        if consecutive_failures >= 3:
                            report["consecutiveFailureStopped"] = True
                            break
                        continue

                    consecutive_failures = 0
                    if verify_with:
                        verify_open_proposals()

                covered_lessons = {lid for b in batches if b["batchId"] in examined_batch_ids for lid in b["lessonIds"]}
                all_shown_lessons = [lid for b in batches for lid in b["lessonIds"]]
                unexamined = len([lid for lid in all_shown_lessons if lid not in covered_lessons]) + len(oversized)
                report["unexaminedLessons"] = unexamined
                if unexamined:
                    report["notices"].append(v.DREAM_TIDY_UNEXAMINED_NOTICE % unexamined)
                if verify_with:
                    report["verificationSummary"] = v.DREAM_TIDY_VERIFICATION_SUMMARY % (
                        report["verifiedProposals"],
                        checked_same,
                        checked_diff,
                    )
                with md.queue_lock(root / mb.QUEUE_REL):
                    locked_store, _ = _read_store(root)
                    locked_store = locked_store or _empty_store()
                    locked_store["lastRun"] = {
                        "runId": run_id,
                        "at": md._utc_now_iso(),
                        "batches": report["batches"],
                        "calls": report["calls"],
                        "failedBatches": report["failedBatches"],
                        "addedProposals": len(report["addedProposals"]),
                        "unexaminedLessons": report["unexaminedLessons"],
                        "alreadyExaminedLessons": report["alreadyExaminedLessons"],
                    }
                    _write_store(root, locked_store)

    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
        report["errors"].append(str(exc))

    for args in captures:
        try:
            expected = args[-1]() if callable(args[-1]) else args[-1]
            dc._save_answer(*args[:-1], expected)
        except Exception as exc:  # noqa: BLE001
            report["notices"].append("fixture capture failed: " + str(exc))

    report["droppedGroupCounts"] = _reason_counts(report["droppedGroups"])
    report["wallSeconds"] = round(now_fn() - started, 3)
    report["ok"] = not report["errors"]
    report["exitCode"] = 0 if report["ok"] else 2
    if not report["ok"]:
        report["error"] = "; ".join(report["errors"])
    if not dry_run:
        report.pop("prompts", None)
    return report


def dream_tidy_dismiss(repo: Any = ".", proposal_ids: Sequence[str] = ()) -> Dict[str, Any]:
    """Record one or more proposal ids in `tidy-proposals.json`'s `dismissed` list. Never launches a model."""
    try:
        root, _ = dc._repo(repo)
        wanted = [str(x).strip() for x in proposal_ids if str(x).strip()]
        wanted = list(dict.fromkeys(wanted))
        if not wanted:
            raise ValueError(v.DREAM_TIDY_MODE_ERROR)
        for pid in wanted:
            if not _PROPOSAL_ID_RE.fullmatch(pid):
                raise ValueError(v.DREAM_TIDY_INVALID_PROPOSAL_ID % pid)
        with md.queue_lock(root / mb.QUEUE_REL):
            store, exists = _read_store(root)
            if not exists or store is None:
                raise ValueError(v.DREAM_TIDY_UNKNOWN_PROPOSAL % wanted[0])
            known = {p.get("id") for p in store.get("proposals", []) if isinstance(p, dict)} | set(store.get("dismissed", []))
            for pid in wanted:
                if pid not in known:
                    raise ValueError(v.DREAM_TIDY_UNKNOWN_PROPOSAL % pid)
            already = set(store.get("dismissed", []))
            applied, unchanged = [], []
            for pid in wanted:
                if pid in already:
                    unchanged.append(pid)
                else:
                    store.setdefault("dismissed", []).append(pid)
                    already.add(pid)
                    applied.append(pid)
            for prop in store.get("proposals", []):
                if isinstance(prop, dict) and prop.get("id") in already:
                    prop["dismissed"] = True
            if applied:
                _write_store(root, store)
        return {
            "ok": True,
            "tidy": True,
            "exitCode": 0,
            "dismissed": applied,
            "unchanged": unchanged,
        }
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        return dict(mb._fail(str(exc), 2), tidy=True)


def dream_tidy_status(repo: Any = ".") -> Dict[str, Any]:
    """Read-only status of `dream --tidy`: open/disputed/stale/merged/dismissed counts, dropped groups and failed batches."""
    try:
        root, _ = dc._repo(repo)
        loaded = _load_repository_lessons(root)
        lessons = loaded["lessons"]
        lessons_by_id = loaded["lessonsById"]
        wording_groups = _tidy_duplicate_sets(lessons, lessons_by_id)
        notices = list(loaded["notices"])
        counts = {"open": 0, "disputed": 0, "stale": 0, "merged": 0, "dismissed": 0}
        reasons: Dict[str, int] = {}
        evaluated: List[Dict[str, Any]] = []
        invalid_records: Dict[str, int] = {}
        duplicate_proposals = 0

        loaded_store = _load_store_for_read(root, known_lesson_ids=set(lessons_by_id))
        store_state = loaded_store["state"]
        if store_state in ("unreadable", "too-large"):
            note = loaded_store["notice"]
            if note not in notices:
                notices.append(note)
            dropped_groups: List[Dict[str, Any]] = []
            dropped_omitted = 0
            failed_records: List[Dict[str, Any]] = []
        elif store_state == "ok":
            invalid_proposals = loaded_store["invalidProposals"]
            if invalid_proposals:
                counts["stale"] += invalid_proposals
                reasons["invalid-proposal"] = reasons.get("invalid-proposal", 0) + invalid_proposals
            duplicate_proposals = loaded_store["duplicateProposals"]
            if duplicate_proposals:
                reasons["duplicate-proposal"] = reasons.get("duplicate-proposal", 0) + duplicate_proposals
            invalid_records = dict(loaded_store["invalidRecords"])

            seen_pids: Set[str] = set()
            for prop in loaded_store["proposals"]:
                st = proposal_state(prop, lessons_by_id, wording_groups=wording_groups)
                state_name = st["state"]
                reason = st["reason"]
                disputed = bool(state_name == "open" and prop.get("verdict") == "DIFFERENT")
                counts[state_name] = counts.get(state_name, 0) + 1
                if disputed:
                    counts["disputed"] += 1
                if reason:
                    reasons[reason] = reasons.get(reason, 0) + 1
                pid = prop["id"]
                seen_pids.add(pid)
                present_ids = [lid for lid in prop["lessonIds"] if lid in lessons]
                missing_ids = [lid for lid in prop["lessonIds"] if lid not in lessons]
                ordered_ids = (mt.kept_lesson_order(lessons, present_ids) if present_ids else []) + sorted(missing_ids)
                evaluated.append({
                    "id": pid,
                    "state": state_name,
                    "reason": reason,
                    "keep": ordered_ids[0],
                    "lessonIds": ordered_ids,
                    "proposer": {"vendor": prop["proposer"]["vendor"], "model": prop["proposer"]["model"]},
                    "verifier": copy.deepcopy(prop["verifier"]) if prop.get("verifier") else None,
                    "verdict": prop.get("verdict"),
                    "disputed": disputed,
                })
            for pid in loaded_store["dismissed"]:
                if pid not in seen_pids:
                    counts["dismissed"] += 1
                    reasons["dismissed"] = reasons.get("dismissed", 0) + 1
            dropped_groups = loaded_store["droppedGroups"]
            dropped_omitted = loaded_store["droppedGroupsOmitted"]
            failed_records = loaded_store["failedRecords"]
        else:
            dropped_groups = []
            dropped_omitted = 0
            failed_records = []

        return {
            "ok": True,
            "tidy": True,
            "exitCode": 0,
            "counts": counts,
            "reasons": reasons,
            "duplicateProposals": duplicate_proposals,
            "invalidRecords": invalid_records,
            "proposals": evaluated,
            "droppedGroups": dropped_groups,
            "droppedGroupCounts": _reason_counts(dropped_groups),
            "droppedGroupsOmitted": dropped_omitted,
            "failedBatches": len(failed_records),
            "failedBatchIds": [r["batchId"] for r in failed_records],
            "failedBatchRecords": failed_records,
            "notices": notices,
            "trustBoundary": v.DREAM_TIDY_TRUST_NOTICE,
        }
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        return dict(mb._fail(str(exc), 2), tidy=True)


# ---------------------------------------------------------------------------------------------------------------
# single read-only entry point for `memlet admin tidy`


def _safe_tidy_section(fn: Callable[..., Optional[Dict[str, Any]]]) -> Callable[..., Optional[Dict[str, Any]]]:
    def wrapper(*args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            return {"entries": [], "omitted": 0, "notices": [v.TIDY_MODEL_PROPOSALS_UNREADABLE], "blockersFailed": False}

    return wrapper


@_safe_tidy_section
def tidy_report_section(
    root: Path,
    *,
    lessons: Dict[str, Dict[str, Any]],
    nodes: Dict[str, Dict[str, Any]],
    merge: Dict[str, Any],
    merge_ok: bool,
    cmd: Callable[[Sequence[str], Sequence[Tuple[str, Any]]], Optional[str]],
    duplicate_ids: Iterable[str] = (),
    wording_groups: Optional[Sequence[Set[str]]] = None,
) -> Optional[Dict[str, Any]]:
    """Read `tidy-proposals.json` (if present) and project `modelSaysSame` for `memlet admin tidy`.

    Returns `None` when `tidy-proposals.json` does not exist so the report stays byte-identical to 9.3.0.
    Strictly read-only: opens no lock, creates no file or directory, calls no model, and never fails the report.
    """
    target = root / PROPOSALS_REL
    problem = mw.managed_path_problem(root, PROPOSALS_REL, leaf="file")
    if problem:
        if os.path.lexists(str(_existing_problem_path(root, PROPOSALS_REL) or target)):
            return {"entries": [], "omitted": 0, "notices": [v.TIDY_MODEL_PROPOSALS_UNREADABLE], "blockersFailed": False}
        return None
    if not os.path.lexists(str(target)):
        return None
    loaded_store = _load_store_for_read(root)
    if loaded_store["state"] == "absent":
        return None
    if loaded_store["state"] != "ok":
        return {
            "entries": [],
            "omitted": 0,
            "notices": [loaded_store["notice"]],
            "blockersFailed": False,
        }

    lessons_by_id = lessons_by_id_from_loaded(lessons, nodes, merge, duplicate_ids=duplicate_ids)
    active_wording = (
        [set(g) for g in wording_groups]
        if wording_groups is not None
        else _tidy_duplicate_sets(lessons, lessons_by_id)
    )
    raw_entries: List[Dict[str, Any]] = []
    blockers_failed = False

    for prop in loaded_store["proposals"]:
        st = proposal_state(prop, lessons_by_id, wording_groups=active_wording)
        if st["state"] != "open":
            continue
        ordered = mt.kept_lesson_order(lessons, prop["lessonIds"])
        keep, same = ordered[0], ordered[1:]
        proposer = {"vendor": prop["proposer"]["vendor"], "model": prop["proposer"]["model"]}
        verifier = copy.deepcopy(prop["verifier"]) if prop.get("verifier") else None
        verdict = prop.get("verdict")
        disputed = verdict == "DIFFERENT"
        proposer_label = v.TIDY_MODEL_PROPOSER_LABEL % (proposer["vendor"], proposer["model"])
        if verifier is None:
            verifier_label = v.TIDY_MODEL_UNVERIFIED_LABEL
        elif verdict == "SAME":
            verifier_label = v.TIDY_MODEL_SAME_VERDICT_LABEL % (verifier["vendor"], verifier["model"])
        else:
            verifier_label = v.TIDY_MODEL_DISPUTED_VERDICT_LABEL % (verifier["vendor"], verifier["model"])
        combined_label = (v.TIDY_MODEL_DISPUTED_PREFIX if disputed else "") + proposer_label + "; " + verifier_label

        overlap_set: Set[str] = set()
        group_member_set = set(ordered)
        for w_group in active_wording:
            inter = group_member_set & w_group
            if len(inter) >= 2:
                overlap_set.update(inter)
        overlap_ids = [lid for lid in ordered if lid in overlap_set]
        if overlap_ids:
            overlap_label = v.TIDY_MODEL_WORDING_OVERLAP_LABEL % ", ".join(overlap_ids)
            combined_label += "; " + overlap_label
        else:
            overlap_label = None

        dismiss_cmd = cmd(["dream", "--tidy", "--dismiss", prop["id"]], [])
        entry: Dict[str, Any] = {
            "id": prop["id"],
            "keep": keep,
            "same": same,
            "lessons": [mt._lesson(lessons[lid]) for lid in ordered],
            "proposer": proposer,
            "verifier": verifier,
            "verdict": verdict,
            "disputed": disputed,
            "proposerLabel": proposer_label,
            "verifierLabel": verifier_label,
            "label": combined_label,
            "dismissCommand": dismiss_cmd,
        }
        if overlap_ids:
            entry["wordingOverlap"] = overlap_ids
            entry["wordingOverlapLabel"] = overlap_label
        if merge_ok:
            mt._with_command(entry, cmd(["learn"], [("--merge", keep), ("--same", same), ("--by", "NAME")]))
            checked = mt._checked_blockers(merge, keep, same)
            if checked is None:
                blockers_failed = True
                mt._with_command(entry, None, v.TIDY_BLOCKERS_FAILED)
            else:
                entry.update(checked)
                entry["blockerMessages"] = {
                    i: [v.MERGE_REASONS.get(r, r) for r in reasons]
                    for i, reasons in checked["blockers"].items()
                }
                if any(checked["blockers"].values()):
                    mt._with_command(
                        entry,
                        None,
                        "; ".join(
                            i + ": " + ", ".join(r + " — " + v.MERGE_REASONS.get(r, r) for r in rs)
                            for i, rs in checked["blockers"].items()
                        ),
                    )
                lost = list(dict.fromkeys(p for paths in checked["lostFiles"].values() for p in paths))
                wanted = list(dict.fromkeys(list(lessons[keep]["item"].get("approvalPaths") or []) + lost))
                if entry.get("command") and lost and lessons[keep]["tier"] == APPROVED and len(wanted) <= 8:
                    paths_cmd = cmd(["learn"], [("--paths", keep), ("--by", "NAME")] + [("--path", p) for p in wanted])
                    if paths_cmd is not None:
                        entry["pathsCommand"] = paths_cmd
        else:
            mt._with_command(entry, None, v.TIDY_MERGE_COMMAND_UNAVAILABLE)
        raw_entries.append(entry)

    raw_entries.sort(key=lambda e: (1 if e["disputed"] else 0, -len(e["lessons"]), e["keep"], e["id"]))
    omitted_count = max(0, len(raw_entries) - v.TIDY_SECTION_CAP)
    return {
        "entries": raw_entries[: v.TIDY_SECTION_CAP],
        "omitted": omitted_count,
        "notices": [],
        "blockersFailed": blockers_failed,
    }
