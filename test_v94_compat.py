#!/usr/bin/env python3
"""Memlet 9.4: byte-for-byte comparison with the published 9.3.0 release (commit 0ca681f).

The same probe runs once with the 9.3.0 tree read from git history and once with this tree, on four copies of one
varied repository (ordinary, hand-edited promoted page, empty Dream queue, corrupt Dream queue), and compares:
library readers, the CLI, recall at every budget from 60 to 320, all 25 MCP tool definitions and calls, every hook
event with and without --jit --guard --capsule, and the written promoted page, skill file and both Routlet files.
The only differences read away are the version string and, in `admin tidy` only, the attach-files label and the
suggested-kept-lesson display (README "From 9.3"). No work counts and no timings are asserted.

Moved from the 9.4 read-cost tests (`test_v94_cost.py` at 7e8c8a1, class TestV94Differential and its helpers);
skipped with a message when commit 0ca681f cannot be read from git history.
"""

import concurrent.futures
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from memlet import (
    memlet_anchors as ma,
    memlet_bridge as mb,
    memlet_dream as md,
    memlet_dreamcycle as dc,
    memlet_skillbook as sb,
    memlet_wiki as mw,
)

REPO_ROOT = Path(__file__).resolve().parent
BASE_COMMIT = "0ca681f"


def _isolated_env(scratch: Path) -> dict:
    """Isolated HOME/config/lock dirs while keeping PYTHONHOME and interpreter env vars intact."""
    home = scratch / "home"
    claude_cfg = scratch / "claude_cfg"
    codex_home = scratch / "codex_home"
    locks = scratch / "locks"
    tmp = scratch / "tmp"
    for d in (home, claude_cfg, codex_home, locks, tmp):
        d.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(claude_cfg),
        "CODEX_HOME": str(codex_home),
        "MEMLET_LOCK_DIR": str(locks),
        "TMPDIR": str(tmp),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def _git(repo: Path, *args: str, env: dict) -> None:
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def _seal(obj: dict, kind: str, *, decision: str = "approved", by: str = "reviewer",
          at: str = "2026-02-01T00:00:00Z", reason: str = "", paths=None) -> dict:
    obj.pop("approval", None)
    if paths is not None and kind == "node":
        obj["approvedPaths"] = list(paths)
    rec = {
        "decision": decision,
        "by": by,
        "at": at,
        "digest": md.node_approval_digest(obj) if kind == "node" else md.edge_approval_digest(obj),
    }
    if reason:
        rec["reason"] = reason
    if paths is not None and kind == "node":
        rec["paths"] = list(paths)
        rec["digest"] = md.node_approval_digest(obj)
    obj["approval"] = rec
    return obj


def _proposer_rec(node: dict, vendor: str, model: str) -> dict:
    return dc.seal_receipt({
        "vendor": vendor,
        "origin": "vendor-cli",
        "model": model,
        "isolation": "tool-free",
        "runId": "a" * 24,
        "at": "2026-02-01T00:00:00Z",
        "digest": dc.trust_digest(node),
    })


def _verifier_rec(node: dict, vendor: str, model: str, verdict: str = "SUPPORTED") -> dict:
    return dc.seal_receipt({
        "vendor": vendor,
        "origin": "vendor-cli",
        "model": model,
        "verdict": verdict,
        "isolation": "tool-free",
        "runId": "b" * 24,
        "at": "2026-02-01T00:00:00Z",
        "digest": dc.trust_digest(node),
    })


def _make_varied_repository(repo: Path, env: dict) -> None:
    """Build a rich repository covering every relation/tier/supersession/merge/anchor/attachment/hostile case."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", env=env)
    _git(repo, "config", "user.email", "test@example.invalid", env=env)
    _git(repo, "config", "user.name", "Tester", env=env)

    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src/auth.py").write_text(
        "def validate_token(token):\n    return bool(token)\n", encoding="utf-8"
    )
    (repo / "src/billing.py").write_text(
        "def charge(user_id):\n    return {'user': user_id}\n\n"
        "def refund(order_id):\n    return order_id\n",
        encoding="utf-8",
    )
    (repo / "src/router.py").write_text(
        "def route(task):\n    return task\n", encoding="utf-8"
    )
    (repo / "src/retry.py").write_text(
        "def with_backoff(fn):\n    return fn()\n", encoding="utf-8"
    )
    (repo / "src/extra.py").write_text(
        "def helper(value):\n    return value\n", encoding="utf-8"
    )
    (repo / "src/deleted_legacy.py").write_text(
        "def old_entry():\n    return 0\n", encoding="utf-8"
    )

    (repo / "AGENTS.md").write_text(
        "# Project Rules\n\n"
        "## Safety Rules\n\n"
        "- Always validate sessions through `src/auth.py::validate_token` before mutating state.\n"
        "- Charge accounts via `src/billing.py::charge` with `user_id`.\n"
        "- Route incoming jobs with `src/router.py::route`.\n"
        "- Wrap external calls in `with_backoff()` for transient errors.\n"
        "- Keep `src/billing.py` audit logs intact.\n\n"
        "## Dead-Ends\n\n"
        "- Retrying inside the webhook handler double-charges accounts.\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-q", "-m", "initial baseline", env=env)

    assert mw.init_project(repo)["ok"]

    # Accept sidecar suggestion for `with_backoff()` -> `src/retry.py::with_backoff`
    sug = ma.suggest_anchors(repo)
    sids = [s["id"] for s in sug.get("suggestions", [])]
    assert sids, sug
    assert ma.accept_suggestions(repo, sids)["ok"]

    # Mutate working tree after pinning to create:
    # - stale anchor (`src/billing.py::charge` signature broken)
    # - compatible-unverified anchor (`src/router.py::route` optional param added)
    # - unpinned symbol rule (`src/extra.py::helper`) + missing-path rule (`src/missing_mod.py`)
    # - deleted attachment target (`src/deleted_legacy.py` removed)
    (repo / "src/billing.py").write_text(
        "def charge(account_id, currency):\n    return {'account': account_id, 'currency': currency}\n\n"
        "def refund(order_id):\n    return order_id\n",
        encoding="utf-8",
    )
    (repo / "src/router.py").write_text(
        "def route(task, priority='normal'):\n    return task, priority\n", encoding="utf-8"
    )
    (repo / "src/deleted_legacy.py").unlink()
    with (repo / "AGENTS.md").open("a", encoding="utf-8") as fh:
        fh.write(
            "\n## Additional Observations\n\n"
            "- Normalize inputs with `src/extra.py::helper` when present.\n"
            "- Legacy migration script lived at `src/missing_mod.py`.\n"
            "Never bypass billing reconciliation checks during settlement runs without explicit signoff.\n"
        )

    # Enable hints in config so cross-vendor agreed unapproved BLOCKS/RESOLVES items project as hints.
    cfg_path = repo / mw.CONFIG_PATH
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["dream"] = {"hints": True, "agreement": "vendor"}
    cfg_path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    snap_lines = [
        "2026-01-10T00:00:01Z [user] Govern src/auth.py token expiry strictly at 15 minutes.",
        "2026-01-10T00:00:02Z [user] Never call refund() in src/billing.py without idempotency keys.",
        "2026-01-10T00:00:03Z [user] Workaround: flush src/router.py queue before draining workers.",
        "2026-01-10T00:00:04Z [user] Old policy A: retry failed charges three times immediately.",
        "2026-01-10T00:00:05Z [user] Mid policy B: retry failed charges once after 5 seconds.",
        "2026-01-10T00:00:06Z [user] Current policy C: never auto-retry charges in src/billing.py; enqueue reconciliation.",
        "2026-01-10T00:00:07Z [user] Incoming old rule: cache auth tokens in local files.",
        "2026-01-10T00:00:08Z [user] Incoming new rule: keep src/auth.py tokens in memory only.",
        "2026-01-10T00:00:09Z [user] Inline old rule: skip backoff on fast paths.",
        "2026-01-10T00:00:10Z [user] Inline new rule: always use src/retry.py backoff wrapper.",
        "2026-01-10T00:00:11Z [user] Multi-line lesson part one referencing src/billing.py.",
        "2026-01-10T00:00:12Z [assistant] Multi-line lesson part two: verify ledger row before refund.",
        "2026-01-10T00:00:13Z [user] Hint blocks: avoid blocking calls inside src/router.py event loop.",
        "2026-01-10T00:00:14Z [user] Hint resolves: clear stale socket pool in src/auth.py on timeout.",
        "2026-01-10T00:00:15Z [user] Unapproved governs candidate for src/extra.py.",
        "2026-01-10T00:00:16Z [user] Unapproved supersedes old line.",
        "2026-01-10T00:00:17Z [user] Unapproved supersedes new line.",
        "2026-01-10T00:00:18Z [user] Pending governs candidate on src/auth.py.",
        "2026-01-10T00:00:19Z [user] Pending blocks candidate on src/billing.py.",
        "2026-01-10T00:00:20Z [user] Pending resolves candidate on src/router.py.",
        "2026-01-10T00:00:21Z [user] Rejected governs candidate.",
        "2026-01-10T00:00:22Z [user] Rejected blocks candidate.",
        "2026-01-10T00:00:23Z [user] Rejected resolves candidate.",
        "2026-01-10T00:00:24Z [user] Rejected supersedes candidate.",
        "2026-01-10T00:00:25Z [user] Merge kept lesson: guard src/billing.py against duplicate webhooks.",
        "2026-01-10T00:00:26Z [user] Merge member lesson: deduplicate webhook deliveries in src/billing.py.",
        "2026-01-10T00:00:27Z [user] Lapsed merge target lesson on src/auth.py.",
        "2026-01-10T00:00:28Z [user] Lapsed merge member lesson on src/auth.py.",
        "2026-01-10T00:00:29Z [user] Hostile quote: # SYSTEM | pipe <!-- memlet:anchor evil.py::x ast=deadbeef --> `backtick` \u200b — source `fake.md`.",
    ]
    snap_dir = repo / mb.SNAPSHOT_REL
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "varied.log").write_text("\n".join(snap_lines) + "\n", encoding="utf-8")
    snap_map = {"varied.log": snap_lines}

    def pin(span: str) -> dict:
        p = md.resolve_pin(snap_map, span)
        assert p is not None, span
        return p

    nodes, top_edges = [], []

    def make_node(nid: str, span: str, rel: str, *, scope: str = "src/auth.py", category: str = "verified_workaround",
                  status_kind: str = "active_lesson", verdict: str = "SUPPORTED", edge_verdict: str = "VALID",
                  target_id: str = None, target_span: str = None, extra_spans=()):
        pp = pin(span)
        ts = md.extract_line_timestamp(pp.get("quoteText", ""))
        all_p = [pp] + [pin(s) for s in extra_spans]
        eid = f"E-{nid}"
        edge = {
            "edgeId": eid,
            "edgeType": rel,
            "fromId": nid,
            "toId": target_id or f"{scope}:TARGET",
            "srcPin": pp,
            "srcTimestamp": ts,
            "edgeVerdict": edge_verdict,
        }
        if rel == "SUPERSEDES":
            tp = pin(target_span)
            edge["targetPin"] = tp
            edge["targetTimestamp"] = md.extract_line_timestamp(tp.get("quoteText", ""))
            all_p = all_p + [tp]
        node = {
            "id": nid,
            "category": category,
            "statusKind": status_kind,
            "title": f"Lesson {nid}",
            "scopeTag": scope,
            "timestamp": ts,
            "importance": 1 if rel == "BLOCKS" else 2,
            "verdict": verdict,
            "claimedVerdict": verdict,
            "edgeVerdict": edge_verdict,
            "primaryPin": pp,
            "allPins": all_p,
            "edges": [edge],
        }
        return node, edge

    # 1. Approved across GOVERNS, BLOCKS, RESOLVES (single-line, multi-line, active + inactive approvedPaths, hostile)
    n_gov, e_gov = make_node("DRM-APP-GOV", "varied.log:1", "GOVERNS", scope="src/auth.py")
    _seal(e_gov, "edge"); _seal(n_gov, "node", paths=["src/auth.py", "src/deleted_legacy.py"])

    n_blk, e_blk = make_node("DRM-APP-BLK", "varied.log:2", "BLOCKS", scope="src/billing.py",
                             category="repeated_pitfall", status_kind="open_trap")
    _seal(e_blk, "edge"); _seal(n_blk, "node", paths=["src/billing.py"])

    n_res, e_res = make_node("DRM-APP-RES", "varied.log:3", "RESOLVES", scope="src/router.py")
    _seal(e_res, "edge"); _seal(n_res, "node")

    n_ml, e_ml = make_node("DRM-MULTILINE", "varied.log:11-12", "BLOCKS", scope="src/billing.py",
                           category="repeated_pitfall", status_kind="open_trap")
    _seal(e_ml, "edge"); _seal(n_ml, "node")

    n_hostile, e_hostile = make_node("DRM-HOSTILE", "varied.log:29", "BLOCKS", scope="src/auth.py",
                                     category="repeated_pitfall", status_kind="open_trap")
    _seal(e_hostile, "edge"); _seal(n_hostile, "node")
    nodes.extend([n_gov, n_blk, n_res, n_ml, n_hostile])
    top_edges.extend([e_gov, e_blk, e_res, e_ml, e_hostile])

    # 2. Supersession chain A <- B <- C (C supersedes B, B supersedes A)
    n_ca, e_ca = make_node("DRM-CHAIN-A", "varied.log:4", "GOVERNS", scope="src/billing.py")
    _seal(e_ca, "edge"); _seal(n_ca, "node")
    n_cb, e_cb = make_node("DRM-CHAIN-B", "varied.log:5", "SUPERSEDES", scope="src/billing.py",
                           target_id="DRM-CHAIN-A", target_span="varied.log:4", verdict="SUPPORTED")
    _seal(e_cb, "edge"); _seal(n_cb, "node")
    n_cc, e_cc = make_node("DRM-CHAIN-C", "varied.log:6", "SUPERSEDES", scope="src/billing.py",
                           target_id="DRM-CHAIN-B", target_span="varied.log:5", verdict="SUPPORTED")
    _seal(e_cc, "edge"); _seal(n_cc, "node")
    nodes.extend([n_ca, n_cb, n_cc])
    top_edges.extend([e_ca, e_cb, e_cc])

    # 3. Incoming supersession (INC-NEW -> INC-OLD) + inline supersession (INLINE-SUP -> synthetic target)
    n_io, e_io = make_node("DRM-INC-OLD", "varied.log:7", "BLOCKS", scope="src/auth.py")
    _seal(e_io, "edge"); _seal(n_io, "node")
    n_in, e_in = make_node("DRM-INC-NEW", "varied.log:8", "SUPERSEDES", scope="src/auth.py",
                           target_id="DRM-INC-OLD", target_span="varied.log:7")
    _seal(e_in, "edge"); _seal(n_in, "node")
    n_inl, e_inl = make_node("DRM-INLINE-SUP", "varied.log:10", "SUPERSEDES", scope="src/retry.py",
                             target_id="SYNTHETIC:OLD", target_span="varied.log:9")
    _seal(e_inl, "edge"); _seal(n_inl, "node")
    nodes.extend([n_io, n_in, n_inl])
    top_edges.extend([e_io, e_in, e_inl])

    # 4. Hints (cross-vendor agreed BLOCKS & RESOLVES with valid 24-hex runId) + cross-vendor unapproved GOVERNS & SUPERSEDES
    for nid, sp, rel, sc in (
        ("DRM-HINT-BLK", "varied.log:13", "BLOCKS", "src/router.py"),
        ("DRM-HINT-RES", "varied.log:14", "RESOLVES", "src/auth.py"),
        ("DRM-XVEN-GOV", "varied.log:15", "GOVERNS", "src/extra.py"),
    ):
        nh, eh = make_node(nid, sp, rel, scope=sc, verdict="SUPPORTED")
        nh["proposers"] = [_proposer_rec(nh, "claude", "claude-sonnet-4-5")]
        nh["verification"] = [_verifier_rec(nh, "codex", "gpt-5", "SUPPORTED")]
        nodes.append(nh); top_edges.append(eh)

    nh_sup, eh_sup = make_node("DRM-XVEN-SUP", "varied.log:17", "SUPERSEDES", scope="src/extra.py",
                               target_id="SYNTH:16", target_span="varied.log:16", verdict="SUPPORTED")
    nh_sup["proposers"] = [_proposer_rec(nh_sup, "claude", "claude-sonnet-4-5")]
    nh_sup["verification"] = [_verifier_rec(nh_sup, "codex", "gpt-5", "SUPPORTED")]
    nodes.append(nh_sup); top_edges.append(eh_sup)

    # 5. Pending (GOVERNS, BLOCKS, RESOLVES + XVEN-GOV + XVEN-SUP = 5 pending)
    for nid, sp, rel, sc in (
        ("DRM-PEND-GOV", "varied.log:18", "GOVERNS", "src/auth.py"),
        ("DRM-PEND-BLK", "varied.log:19", "BLOCKS", "src/billing.py"),
        ("DRM-PEND-RES", "varied.log:20", "RESOLVES", "src/router.py"),
    ):
        np_, ep_ = make_node(nid, sp, rel, scope=sc, verdict=md.PENDING, edge_verdict=md.PENDING)
        nodes.append(np_); top_edges.append(ep_)

    # 6. Rejected (all 4 relations)
    for nid, sp, rel in (
        ("DRM-REJ-GOV", "varied.log:21", "GOVERNS"),
        ("DRM-REJ-BLK", "varied.log:22", "BLOCKS"),
        ("DRM-REJ-RES", "varied.log:23", "RESOLVES"),
    ):
        nr, er = make_node(nid, sp, rel, scope="src/auth.py", verdict=md.REJECTED, edge_verdict=md.REJECTED)
        _seal(er, "edge", decision="rejected"); _seal(nr, "node", decision="rejected")
        nodes.append(nr); top_edges.append(er)

    nr_sup, er_sup = make_node("DRM-REJ-SUP", "varied.log:24", "SUPERSEDES", scope="src/auth.py",
                               target_id="SYNTH:16", target_span="varied.log:16",
                               verdict=md.REJECTED, edge_verdict=md.REJECTED)
    _seal(er_sup, "edge", decision="rejected"); _seal(nr_sup, "node", decision="rejected")
    nodes.append(nr_sup); top_edges.append(er_sup)

    # 7. Merge candidates (one holding merge, one lapsed merge)
    n_mk, e_mk = make_node("DRM-MERGE-KEEP", "varied.log:25", "BLOCKS", scope="src/billing.py",
                           category="repeated_pitfall", status_kind="open_trap")
    _seal(e_mk, "edge"); _seal(n_mk, "node", paths=["src/billing.py"])
    n_mm, e_mm = make_node("DRM-MERGE-MEM", "varied.log:26", "BLOCKS", scope="src/billing.py",
                           category="repeated_pitfall", status_kind="open_trap")
    _seal(e_mm, "edge"); _seal(n_mm, "node", paths=["src/billing.py"])

    n_lt, e_lt = make_node("DRM-LAPSE-TGT", "varied.log:27", "BLOCKS", scope="src/auth.py",
                           category="repeated_pitfall", status_kind="open_trap")
    _seal(e_lt, "edge"); _seal(n_lt, "node")
    n_lm, e_lm = make_node("DRM-LAPSE-MEM", "varied.log:28", "BLOCKS", scope="src/auth.py",
                           category="repeated_pitfall", status_kind="open_trap")
    _seal(e_lm, "edge"); _seal(n_lm, "node")

    nodes.extend([n_mk, n_mm, n_lt, n_lm])
    top_edges.extend([e_mk, e_mm, e_lt, e_lm])

    queue_path = repo / mb.QUEUE_REL
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    # Keep nested node["edges"] and top-level graph["edges"] identical so verify_ontology_contract passes!
    top_edges = [copy.deepcopy(e) for n in nodes for e in n.get("edges", [])]
    queue_path.write_text(
        json.dumps({"schema": md.DREAM_ONTOLOGY_SCHEMA, "nodes": nodes, "edges": top_edges}, indent=2) + "\n",
        encoding="utf-8",
    )

    assert mb.learn_merge(repo, keep="DRM-MERGE-KEEP", same=["DRM-MERGE-MEM"], approver="reviewer")["ok"]
    assert mb.learn_merge(repo, keep="DRM-LAPSE-TGT", same=["DRM-LAPSE-MEM"], approver="reviewer")["ok"]
    assert mb.learn_reject(repo, ["DRM-LAPSE-TGT"], approver="reviewer", reason="obsolete")["ok"]

    assert mb.promote_approved(repo)["ok"]
    assert sb.write_skill(repo, out=repo / ".claude/skills", force=True)["ok"]
    assert mw.build_handover(repo, write=True)["ok"]


def _assert_varied_fixture_states(tc: unittest.TestCase, repo: Path) -> None:
    """Assert every required tier, merge state, anchor state, ontology contract, promoted page, and skill state."""
    all_items = mb.dream_items(repo, tiers=("approved", "hint", "pending"))
    tc.assertTrue(all_items["ok"], all_items)
    tc.assertEqual({"approved": 13, "hint": 2, "pending": 5}, all_items["counts"])
    hint_ids = sorted(it["id"] for it in all_items["items"] if it["tier"] == "hint")
    tc.assertEqual(["DRM-HINT-BLK", "DRM-HINT-RES"], hint_ids)

    q_graph, q_why = mb._load_queue(repo)
    tc.assertIsNotNone(q_graph, q_why)
    snap_files, s_why = mb._load_snapshot(repo)
    tc.assertIsNotNone(snap_files, s_why)
    cfg_dict, _ = mw.read_config(repo)
    tiers_map = mb.dream_tiers(q_graph, snapshot=snap_files, config=cfg_dict)
    tier_summary = {}
    for st in tiers_map.values():
        tier_summary[st["tier"]] = tier_summary.get(st["tier"], 0) + 1
    tc.assertEqual({"approved": 14, "hint": 2, "pending": 5, "rejected": 5}, tier_summary)

    ont = md.verify_ontology_contract(q_graph, snap_files)
    tc.assertTrue(ont["ok"], ont)

    mv = mb.merge_view(repo)
    tc.assertTrue(mv["ok"], mv)
    tc.assertEqual("merged", mv["states"]["DRM-MERGE-MEM"]["state"])
    tc.assertEqual("DRM-MERGE-KEEP", mv["states"]["DRM-MERGE-MEM"]["into"])
    tc.assertEqual("lapsed", mv["states"]["DRM-LAPSE-MEM"]["state"])
    tc.assertEqual("keep-rejected", mv["states"]["DRM-LAPSE-MEM"]["reason"])

    par = mw.pinned_anchor_report(repo)
    tc.assertGreaterEqual(par["summary"]["active"], 1)
    tc.assertGreaterEqual(par["summary"]["compatible-unverified"], 1)
    tc.assertGreaterEqual(par["summary"]["stale"], 1)
    tc.assertGreaterEqual(par["summary"]["unpinned"], 1)
    tc.assertGreaterEqual(par.get("sidecar", {}).get("attached", 0), 1)
    tc.assertTrue(any(a.get("suggested") for a in par["anchors"]))

    p_fresh, p_stale = mb.promoted_rule_entries(repo)
    tc.assertGreaterEqual(len(p_fresh), 7)
    tc.assertEqual([], p_stale)

    sk_chk = sb.check_skill(repo / ".claude/skills/memlet-lessons/SKILL.md", repo_path=repo)
    tc.assertTrue(sk_chk["ok"], sk_chk)


_PROBE_SCRIPT = r"""
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

time.perf_counter = lambda: 100.0
time.time_ns = lambda: 1782864000000000000
tree_root = Path(sys.argv[1]).resolve()
repo = Path(sys.argv[2]).resolve()
variant = sys.argv[3]
sys.path.insert(0, str(tree_root))

import memory_cli
import memlet_mcp

# The version string is an accepted difference between releases: both sides report the compared release's version,
# so a skill file written by one side is "current", not "written by another version", for the other.
import importlib as _il
for _name, _attr in (("memlet", "VERSION"), ("memlet.contracts", "VERSION"), ("memlet.memlet_wiki", "MEMLET_VERSION"),
                     ("memlet.memlet_skillbook", "VERSION"), ("memlet_mcp", "SERVER_VERSION")):
    try:
        _m = _il.import_module(_name)
    except Exception:
        continue
    if hasattr(_m, _attr):
        setattr(_m, _attr, "9.3.0")
from memlet import (
    handover_bridge as hb,
    memlet_anchors as ma,
    memlet_bridge as mb,
    memlet_dream as md,
    memlet_dreamcycle as dc,
    memlet_graph as mg,
    memlet_hooks as mh,
    memlet_skillbook as sb,
    memlet_tidy as mt,
    memlet_wiki as mw,
)

FIXED_CLOCK = lambda: datetime.datetime(2026, 6, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
FIXED_ISO = "2026-06-01T00:00:00Z"
_LIVE_YEAR_RE = re.compile(r"202[6-9]-(?:0[7-9]|1[0-2])-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)")
_PERF_JSON_RE = re.compile(r'("(?:elapsedMs|avgMs|p50Ms|p95Ms|minMs|maxMs)"\s*:\s*)[0-9.]+')


def norm(obj, *, keep_as_of=False):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("elapsedMs", "avgMs", "p50Ms", "p95Ms", "minMs", "maxMs") and isinstance(v, (int, float)):
                out[k] = 0.0
            elif k in ("generatedAt", "updated_at", "created_at", "updatedAt", "createdAt") and isinstance(v, str):
                out[k] = FIXED_ISO
            elif k == "asOf" and not keep_as_of and isinstance(v, str):
                out[k] = FIXED_ISO
            else:
                out[k] = norm(v, keep_as_of=keep_as_of)
        return out
    if isinstance(obj, (list, tuple)):
        return [norm(x, keep_as_of=keep_as_of) for x in obj]
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, str):
        s = obj.replace(str(tree_root), "<CODE_TREE>").replace(str(repo.parent), "<PROBE_DIR>")
        s = _PERF_JSON_RE.sub(r"\g<1>0.0", s)
        s = _LIVE_YEAR_RE.sub(FIXED_ISO, s)
        return s
    return obj


def safe_call(fn, *, keep_as_of=False):
    try:
        return norm(fn(), keep_as_of=keep_as_of)
    except Exception as exc:
        return {"__exception__": type(exc).__name__, "message": norm(str(exc))}


def run_cli(argv):
    out_buf, err_buf = io.StringIO(), io.StringIO()
    old_argv = sys.argv[:]
    sys.argv = ["memlet", *argv]
    code = 0
    try:
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc = memory_cli.cli()
            code = int(rc or 0)
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old_argv
    res = {"code": code, "stdout": norm(out_buf.getvalue()), "stderr": norm(err_buf.getvalue())}
    if res["stdout"]:
        try:
            parsed = json.loads(res["stdout"])
            res["stdout"] = json.dumps(norm(parsed), sort_keys=True, ensure_ascii=False)
        except Exception:
            pass
    return res


def run_hook_inproc(event, payload, *, scope=None):
    out_buf, err_buf = io.StringIO(), io.StringIO()
    in_buf = io.BytesIO(json.dumps(payload).encode("utf-8"))
    argv = [event, "--scope", scope] if scope else [event]
    old_cwd = os.getcwd()
    try:
        os.chdir(repo)
        rc = mh.run_hook(argv, stdin=in_buf, stdout=out_buf, stderr=err_buf, hard_exit=False)
    finally:
        os.chdir(old_cwd)
    return {"code": int(rc or 0), "stdout": norm(out_buf.getvalue()), "stderr": norm(err_buf.getvalue())}


def neutralise_bridge_doc(raw_text):
    doc = json.loads(raw_text)
    assert hb.verify(doc)
    doc.pop("integritySha256", None)
    return norm(doc)


queue_graph = snap_files = t_cfg = None
try:
    queue_graph, _ = mb._load_queue(repo)
    snap_files, _ = mb._load_snapshot(repo)
    cfg_dict, _ = mw.read_config(repo)
    t_cfg = mb.tier_config(queue_graph, cfg_dict) if queue_graph is not None else cfg_dict
except Exception:
    pass

skill_path = repo / ".claude/skills/memlet-lessons/SKILL.md"
full_graph = safe_call(lambda: mg.build_graph(repo))
rule_ids = sorted(n["id"] for n in full_graph.get("nodes", []) if isinstance(n, dict) and n.get("kind") == "rule")

lib_calls = {
    "dream_items_approved": lambda: mb.dream_items(repo),
    "dream_items_all": lambda: mb.dream_items(repo, tiers=("approved", "hint", "pending")),
    "dream_items_merged": lambda: mb.dream_items(repo, tiers=("approved", "hint", "pending"), include_merged=True),
    "dream_items_query": lambda: mb.dream_items(repo, tiers=("approved", "hint"), query="billing"),
    "dream_items_files": lambda: mb.dream_items(repo, tiers=("approved", "hint"), files=["src/billing.py", "src/router.py"]),
    "merge_view": lambda: mb.merge_view(repo),
    "merge_blockers": lambda: mb.merge_blockers(repo, "DRM-APP-BLK", ["DRM-MULTILINE"]),
    "decision_events": lambda: list(mb.decision_events(repo)),
    "learn_pending": lambda: mb.learn_pending(repo, _record_review=False),
    "promoted_rule_entries": lambda: list(mb.promoted_rule_entries(repo)),
    "unified_recall_default": lambda: mb.unified_recall(repo, "billing"),
    "unified_recall_wide": lambda: mb.unified_recall(repo, "", budget=32000),
    "unified_recall_tight": lambda: mb.unified_recall(repo, "token", budget=220),
    "unified_recall_files": lambda: mb.unified_recall(repo, "", files=["src/billing.py"], budget=4000),
    "changed_check": lambda: mb.changed_check(repo),
    "changed_check_files": lambda: mb.changed_check(repo, files=["src/billing.py", "src/router.py"]),
    "run_dream_recall": lambda: md.run_dream_recall(str(repo / mb.QUEUE_REL), snapshot_path=str(repo / mb.SNAPSHOT_REL), query_text="billing", budget_tokens=4000),
    "handover_verified_json": lambda: mw.build_handover(repo, output_format="json", budget=2000, mode="verified"),
    "handover_verified_tight": lambda: mw.build_handover(repo, output_format="markdown", budget=250, mode="verified"),
    "handover_digest_md": lambda: mw.build_handover(repo, output_format="markdown", budget=450, mode="digest"),
    "handover_index_empty": lambda: mw.build_handover(repo, output_format="json", budget=500, mode="index"),
    "pinned_anchor_report": lambda: mw.pinned_anchor_report(repo),
    "wiki_status": lambda: mw.wiki_status(repo),
    "audit_repository": lambda: mw.audit_repository(repo),
    "import_rules_dry": lambda: mw.import_rules(repo, dry_run=True),
    "graph_build": lambda: full_graph,
    "graph_neighbors_file": lambda: mg.neighbors(repo, "src/billing.py", tiers=("approved", "hint")),
    "graph_neighbors_sym": lambda: mg.neighbors(repo, "src/billing.py::charge", tiers=("approved", "hint")),
    "graph_neighbors_lesson": lambda: mg.neighbors(repo, "DRM-CHAIN-C", tiers=("approved", "hint")),
    "graph_neighbors_rule": lambda: mg.neighbors(repo, rule_ids[0], tiers=("approved", "hint")) if rule_ids else None,
    "tidy_report": lambda: mt.tidy_report(repo, clock=FIXED_CLOCK),
    "lesson_sightings": lambda: mt.lesson_sightings(repo, clock=FIXED_CLOCK),
    "skill_build": lambda: sb.build_skill(repo),
    "skill_check": lambda: sb.check_skill(skill_path, repo_path=repo),
    "sidecar_list": lambda: ma.list_sidecar_anchors(repo),
    "sidecar_suggest": lambda: ma.suggest_anchors(repo),
    "dream_status": lambda: dc.dream_status(repo),
}
lib_results = {k: safe_call(fn) for k, fn in lib_calls.items()}
lib_results["graph_build_as_of"] = safe_call(lambda: mg.build_graph(repo, as_of="2026-03-01T00:00:00Z"), keep_as_of=True)

if queue_graph is not None and snap_files is not None:
    lib_results["dream_tiers"] = safe_call(lambda: mb.dream_tiers(queue_graph, snapshot=snap_files, config=t_cfg))
    lib_results["item_trust"] = safe_call(lambda: {n["id"]: mb.item_trust(n, snapshot=snap_files, config=t_cfg) for n in queue_graph["nodes"]})
    lib_results["item_tier"] = safe_call(lambda: {n["id"]: mb.item_tier(n, snapshot=snap_files, config=t_cfg) for n in queue_graph["nodes"]})
    lib_results["recall_subgraph"] = safe_call(lambda: md.recall_subgraph(md.approved_view(queue_graph)[0], query_text="", budget_tokens=4000, snapshot_files=snap_files))
    lib_results["format_flat_memory"] = safe_call(lambda: md.format_flat_memory(queue_graph, budget_tokens=1500, snapshot_files=snap_files))
    lib_results["list_pending"] = safe_call(lambda: md.list_pending(queue_graph, snapshot_files=snap_files))
    lib_results["verify_ontology"] = safe_call(lambda: md.verify_ontology_contract(queue_graph, snapshot_files=snap_files))
    lib_results["supersession_projection"] = safe_call(lambda: [
        {str(k): sorted(v) for k, v in md.supersession_projection(md.approved_view(queue_graph)[0])[0].items()},
        {str(k): sorted(v) for k, v in md.supersession_projection(md.approved_view(queue_graph)[0])[1].items()},
    ])
    rules_for_hb = mw._rules_from_files(repo, mw.import_source_files(repo, "auto"))
    pins_for_hb = mw.rule_anchor_pins(repo)
    anchors_for_hb = mw._verify_rules(repo, rules_for_hb, pins_for_hb, pin_source="pinned")
    approved_for_hb = mb.dream_items(repo)["items"]
    lib_results["direct_bridges_fixed_clock"] = safe_call(
        lambda: hb.build(repo, rules_for_hb, anchors_for_hb, approved_for_hb, mw._git_head(repo), FIXED_ISO)
    )

# Full hint budget boundary sweep (60..320) across unified_recall include variants and recall CLI on ordinary & edited
budget_sweep = {}
_BOUNDARY_BUDGETS = {
    60, 70, 75, 85, 95, 96, 97, 98, 100, 110, 120, 131, 132, 133,
    140, 160, 179, 180, 181, 200, 202, 203, 204, 224, 225, 226, 228, 255, 280, 300, 320,
}
if variant == "ordinary":
    for b_val in range(60, 321):
        budget_sweep[f"ur_hints_{b_val}"] = mb.unified_recall(repo, "event loop", budget=b_val, include=("hints",))
        budget_sweep[f"ur_hints_mem_{b_val}"] = mb.unified_recall(repo, "event loop", budget=b_val, include=("hints", "memory"))
        budget_sweep[f"ur_dream_mem_{b_val}"] = mb.unified_recall(repo, "event loop", budget=b_val, include=("dream", "memory"))
        fmt_pick = "markdown" if b_val % 2 == 0 else "json"
        budget_sweep[f"cli_recall_{fmt_pick}_{b_val}"] = run_cli(
            ["recall", "event loop", "--repo", str(repo), "--budget", str(b_val), "--format", fmt_pick]
        )
        if b_val in _BOUNDARY_BUDGETS or b_val % 10 == 0:
            budget_sweep[f"ur_rules_hints_{b_val}"] = mb.unified_recall(repo, "event loop", budget=b_val, include=("rules", "hints"))
            budget_sweep[f"ur_rules_dream_mem_{b_val}"] = mb.unified_recall(repo, "event loop", budget=b_val, include=("rules", "dream", "memory"))
        if b_val in _BOUNDARY_BUDGETS:
            other_fmt = "json" if b_val % 2 == 0 else "markdown"
            budget_sweep[f"cli_recall_{other_fmt}_{b_val}"] = run_cli(
                ["recall", "event loop", "--repo", str(repo), "--budget", str(b_val), "--format", other_fmt]
            )
elif variant == "edited_promoted":
    for b_val in (60, 75, 96, 100, 110, 132, 140, 180, 202, 203, 225, 228, 280, 320):
        budget_sweep[f"ur_hints_mem_{b_val}"] = mb.unified_recall(repo, "event loop", budget=b_val, include=("hints", "memory"))
        budget_sweep[f"ur_all_{b_val}"] = mb.unified_recall(repo, "src/auth.py", budget=b_val, include=("rules", "dream", "memory"))
        budget_sweep[f"cli_md_{b_val}"] = run_cli(
            ["recall", "event loop", "--repo", str(repo), "--budget", str(b_val), "--format", "markdown"]
        )

cli_commands = {
    "help": ["--help"],
    "admin_help": ["admin", "--help"],
    "check_json": ["check", str(repo), "--format", "json"],
    "check_md": ["check", str(repo), "--format", "markdown"],
    "check_changed": ["check", str(repo), "--changed", "--format", "json"],
    "recall_json": ["recall", "billing", "--repo", str(repo), "--format", "json"],
    "recall_md": ["recall", "billing", "--repo", str(repo), "--format", "markdown"],
    "recall_files": ["recall", "--files", "src/billing.py", "--repo", str(repo), "--format", "markdown"],
    "handover_json": ["handover", str(repo), "--format", "json"],
    "handover_md": ["handover", str(repo), "--format", "markdown"],
    "handover_digest": ["handover", str(repo), "--mode", "digest", "--format", "markdown"],
    "learn_pending_json": ["learn", "--pending", "--repo", str(repo), "--format", "json"],
    "learn_pending_md": ["learn", "--pending", "--repo", str(repo), "--format", "markdown"],
    "dream_status_json": ["dream", str(repo), "--status", "--format", "json"],
    "dream_status_md": ["dream", str(repo), "--status", "--format", "markdown"],
    "wiki_dry_json": ["admin", "wiki", str(repo), "--dry-run", "--format", "json"],
    "anchors_list": ["admin", "anchors", "list", "--repo", str(repo), "--format", "json"],
    "anchors_suggest": ["admin", "anchors", "suggest", "--repo", str(repo), "--format", "json"],
    "graph_show_json": ["admin", "graph", "show", "src/billing.py", "--repo", str(repo), "--tiers", "approved,hint", "--format", "json"],
    "graph_show_md": ["admin", "graph", "show", "src/billing.py", "--repo", str(repo), "--tiers", "approved,hint", "--format", "markdown"],
    "graph_impact_json": ["admin", "graph", "impact", "src/billing.py", "--repo", str(repo), "--format", "json"],
    "graph_stats_json": ["admin", "graph", "stats", "--repo", str(repo), "--format", "json"],
    "graph_stats_md": ["admin", "graph", "stats", "--repo", str(repo), "--format", "markdown"],
    "tidy_json": ["admin", "tidy", "--repo", str(repo), "--format", "json"],
    "tidy_md": ["admin", "tidy", "--repo", str(repo), "--format", "markdown"],
    "skillbook_json": ["admin", "skillbook", "--repo", str(repo), "--format", "json"],
    "skillbook_md": ["admin", "skillbook", "--repo", str(repo), "--format", "markdown"],
    "skillbook_check": ["admin", "skillbook", "--repo", str(repo), "--check", str(skill_path), "--format", "json"],
}
cli_results = {k: run_cli(v) for k, v in cli_commands.items()}

# All 25 MCP tools: definitions + calling every single one of the 25 tools
srv = memlet_mcp.MemletMCPServer(root=repo.parent / f"mcp_store_{variant}", default_task="diff-task")
tools_list_resp = srv.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
tool_names = [t["name"] for t in tools_list_resp["result"]["tools"]]
assert len(tool_names) == 25, len(tool_names)

mcp_arg_map = {
    "memlet_check": {"repo_path": str(repo)},
    "memlet_recall": {"repo_path": str(repo), "query": "billing", "budget": 4000},
    "memlet_handover": {"repo_path": str(repo), "budget": 2000},
    "memlet_learn": {"repo_path": str(repo), "action": "pending"},
    "memlet_dream_next": {"repo_path": str(repo)},
    "memlet_dream_submit": {"repo_path": str(repo), "run_id": "a" * 24, "batch_id": "b" * 24, "candidates": []},
    "memlet_store_brief": {},
    "memlet_store_recall": {"query": "billing"},
    "memlet_store_read": {"id": "missing"},
    "memlet_store_capture": {"text": ""},
    "memlet_store_learn": {"revision": 0, "operationId": "diff-op", "entries": []},
    "memlet_store_skills": {},
    "memlet_store_skill_manage": {"revision": 0, "operationId": "diff-op", "spec": {}},
    "memlet_store_feedback": {"operationId": "diff-op", "recallId": "missing", "outcome": "unknown"},
    "memlet_store_preflight": {"plan": "diff-review"},
    "memlet_store_capsule": {"plan": "diff-review"},
    "memlet_store_inspector": {},
    "memlet_store_scenes": {},
    "memlet_store_consolidate": {"dryRun": True},
    "memlet_store_sync_markdown": {"explicitUser": False},
    "memlet_store_export_rules": {"writeFiles": False, "targetDir": str(repo.parent / "mcp_export")},
    "memlet_store_heal_skills": {"dryRun": True},
    "memlet_store_verify_skill": {"skillId": "missing"},
    "memlet_store_doctor": {},
    "memlet_store_audit": {"repoPath": str(repo)},
}
assert set(mcp_arg_map.keys()) == set(tool_names), set(mcp_arg_map.keys()) ^ set(tool_names)
mcp_results = {"tools_list": tools_list_resp}
for idx_t, t_name in enumerate(tool_names, start=2):
    mcp_results[f"call_{t_name}"] = safe_call(
        lambda t_name=t_name, idx_t=idx_t: srv.handle_rpc(
            {"jsonrpc": "2.0", "id": idx_t, "method": "tools/call", "params": {"name": t_name, "arguments": mcp_arg_map[t_name]}}
        )
    )
mcp_results["resource_inspector"] = safe_call(lambda: srv.read_resource("memlet://diff-task/inspector"))

# Every hook event WITHOUT and WITH --jit --guard --capsule
cfg_path = repo / mw.CONFIG_PATH
orig_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
transcript_src = repo / "hook_session.log"
transcript_src.write_text("2026-02-10T00:00:01Z [user] Check src/billing.py settlement invariants.\n", encoding="utf-8")


def collect_all_hook_events(mode_label, scope_cfg):
    cfg_copy = json.loads(json.dumps(orig_cfg))
    cfg_copy.setdefault("hooks", {})["scopes"] = {"local": scope_cfg}
    cfg_path.write_text(json.dumps(cfg_copy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sess_dir = repo / ".memlet/sessions"
    if sess_dir.exists():
        shutil.rmtree(sess_dir)
    sid = f"sess-{mode_label}"
    base_pl = {"session_id": sid, "cwd": str(repo), "transcript_path": str(transcript_src)}
    edit_pl = dict(base_pl, tool_name="Edit", tool_use_id="tu-1",
                   tool_input={"file_path": str(repo / "src/billing.py"), "old_string": "a", "new_string": "b"})
    out = {
        "probe": run_hook_inproc("probe", {}),
        "session_start_startup": run_hook_inproc("session-start", dict(base_pl, hook_event_name="SessionStart", source="startup"), scope="local"),
        "pre_tool_use_first": run_hook_inproc("pre-tool-use", dict(edit_pl, hook_event_name="PreToolUse"), scope="local"),
        "pre_tool_use_second": run_hook_inproc("pre-tool-use", dict(edit_pl, hook_event_name="PreToolUse"), scope="local"),
        "post_tool_use": run_hook_inproc("post-tool-use", dict(edit_pl, hook_event_name="PostToolUse"), scope="local"),
        "pre_compact": run_hook_inproc("pre-compact", dict(base_pl, hook_event_name="PreCompact"), scope="local"),
        "session_start_compact": run_hook_inproc("session-start", dict(base_pl, hook_event_name="SessionStart", source="compact"), scope="local"),
        "session_start_resume": run_hook_inproc("session-start", dict(base_pl, hook_event_name="SessionStart", source="resume"), scope="local"),
        "stop": run_hook_inproc("stop", dict(base_pl, hook_event_name="Stop"), scope="local"),
    }
    for git_ev in mh.GIT_EVENTS:
        out[f"git_{git_ev}"] = run_hook_inproc(git_ev, {})
    return out


hook_results = {
    "without_jit_guard_capsule": collect_all_hook_events(
        "plain", {"jit": False, "guard": "off", "capsule": False, "dream": False, "tokens": 1200}
    ),
    "with_jit_guard_capsule": collect_all_hook_events(
        "full", {"jit": True, "guard": "deny", "capsule": True, "dream": False, "tokens": 1200}
    ),
}
cfg_path.write_text(json.dumps(orig_cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")

files_results = {}
if variant == "ordinary":
    assert mb.promote_approved(repo)["ok"]
    assert sb.write_skill(repo, out=repo / ".claude/skills", force=True)["ok"]
    assert mw.build_handover(repo, write=True)["ok"]
    files_results = {
        "promoted_page": (repo / mb.PROMOTED_PAGE).read_text(encoding="utf-8"),
        "skill_file": skill_path.read_text(encoding="utf-8"),
        "bridge_handover_neutralised": neutralise_bridge_doc((repo / hb.HANDOVER_FILE).read_text(encoding="utf-8")),
        "bridge_capsules_neutralised": neutralise_bridge_doc((repo / hb.CAPSULES_FILE).read_text(encoding="utf-8")),
        "tidy_markdown": norm(mt.tidy_report(repo, clock=FIXED_CLOCK)["formatted"]),
    }

sys.stdout.write(json.dumps({
    "lib": lib_results,
    "budgets": norm(budget_sweep),
    "cli": cli_results,
    "mcp": mcp_results,
    "hooks": hook_results,
    "files": files_results,
}, sort_keys=True, ensure_ascii=False))
"""


class TestV94Differential(unittest.TestCase):
    """Byte-for-byte differential proof against commit 0ca681f (v9.3.0) across ordinary, edited, empty, and corrupt repos."""

    def test_byte_for_byte_equivalence_with_v930(self):
        archive = subprocess.run(
            ["git", "archive", BASE_COMMIT],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        if archive.returncode != 0 or not archive.stdout:
            self.skipTest(
                f"the comparison with 9.3.0 needs the published commit {BASE_COMMIT} in git history "
                f"(run from a clone that has it); git said: {archive.stderr.decode('utf-8', 'replace').strip()}"
            )

        with tempfile.TemporaryDirectory(prefix="memlet-v94-diff-") as td:
            scratch = Path(td).resolve()
            env = _isolated_env(scratch)
            base_src = scratch / "base_v930"
            base_src.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
                try:
                    tar.extractall(path=base_src, filter="data")
                except TypeError:
                    tar.extractall(path=base_src)

            staging_repo = scratch / "staging_repo"
            _make_varied_repository(staging_repo, env)
            _assert_varied_fixture_states(self, staging_repo)

            variants = ("ordinary", "edited_promoted", "empty_queue", "corrupt_queue")

            def _run_one_probe(variant_and_tree):
                variant, label, code_tree = variant_and_tree
                probe_dir = scratch / f"probe_{variant}_{label}"
                probe_dir.mkdir(parents=True, exist_ok=True)
                sub_env = _isolated_env(probe_dir / "env")
                active_repo = probe_dir / f"active_{variant}"
                shutil.copytree(staging_repo, active_repo)
                if variant == "edited_promoted":
                    pp = active_repo / mb.PROMOTED_PAGE
                    edited_txt = pp.read_text(encoding="utf-8").replace("Never", "Always", 1)
                    assert pp.read_text(encoding="utf-8") != edited_txt
                    pp.write_text(edited_txt, encoding="utf-8")
                elif variant == "empty_queue":
                    (active_repo / mb.QUEUE_REL).write_text(
                        json.dumps({"schema": md.DREAM_ONTOLOGY_SCHEMA, "nodes": [], "edges": []}) + "\n",
                        encoding="utf-8",
                    )
                elif variant == "corrupt_queue":
                    (active_repo / mb.QUEUE_REL).write_text("{corrupt-json", encoding="utf-8")
                proc = subprocess.run(
                    [sys.executable, "-c", _PROBE_SCRIPT, str(code_tree), str(active_repo), variant],
                    env=sub_env,
                    capture_output=True,
                    text=True,
                )
                return variant, label, proc

            jobs = [
                (variant, label, code_tree)
                for variant in variants
                for label, code_tree in (("base", base_src), ("head", REPO_ROOT))
            ]
            all_outputs = {v: {} for v in variants}
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                for variant, label, proc in pool.map(_run_one_probe, jobs):
                    self.assertEqual(
                        0,
                        proc.returncode,
                        f"{variant}/{label} probe failed:\nSTDOUT:\n{proc.stdout[:1000]}\nSTDERR:\n{proc.stderr}",
                    )
                    all_outputs[variant][label] = proc.stdout

            for variant in variants:
                outputs = all_outputs[variant]
                base_parsed = json.loads(outputs["base"])
                head_parsed = json.loads(outputs["head"])
                # The version string is an accepted difference. Some results print it from literals the probe
                # cannot pin (the task-memory store, the benchmark record), so a result is read with this release's
                # version as the compared release's, unless the compared release's own result names this release
                # (a skill file written by this release and checked by the older one).
                from memlet import VERSION as _this_version
                for _top in head_parsed:
                    if not isinstance(head_parsed[_top], dict):
                        continue
                    for _key in list(head_parsed[_top]):
                        if _this_version in json.dumps(base_parsed.get(_top, {}).get(_key)):
                            continue
                        head_parsed[_top][_key] = json.loads(
                            json.dumps(head_parsed[_top][_key]).replace(_this_version, "9.3.0"))
                self.assertEqual(25, len(base_parsed["mcp"]["tools_list"]["result"]["tools"]))
                self.assertEqual(
                    json.dumps(base_parsed["mcp"]["tools_list"], sort_keys=True).encode("utf-8"),
                    json.dumps(head_parsed["mcp"]["tools_list"], sort_keys=True).encode("utf-8"),
                )
                if variant == "edited_promoted":
                    # Confirm hand-edited promoted page actually produced stale promoted rules
                    p_fresh, p_stale = head_parsed["lib"]["promoted_rule_entries"]
                    self.assertEqual([], p_fresh)
                    self.assertGreater(len(p_stale), 0)

                def _normalize_tidy_md_to_930(md_text: str, duplicates: list) -> str:
                    note_by_first_id = {}
                    for d in duplicates:
                        if d.get("blockers") and any(d["blockers"].values()) and d.get("command") is None and d.get("commandNote"):
                            first_lid = sorted(k for k, vals in d["blockers"].items() if vals)[0]
                            note_by_first_id[first_lid] = d["commandNote"]
                    out_lines = []
                    in_dups = False
                    pending_no_cmd = None
                    src_lines = md_text.splitlines()
                    for idx_ln, ln in enumerate(src_lines):
                        if ln.startswith("### "):
                            if pending_no_cmd is not None:
                                out_lines.append(pending_no_cmd)
                                pending_no_cmd = None
                            in_dups = (ln == "### Lessons that may say the same thing (merge?)")
                        if in_dups:
                            if pending_no_cmd is not None and not ln.startswith("  Files a member reaches and the kept lesson does not: "):
                                out_lines.append(pending_no_cmd)
                                pending_no_cmd = None
                            if ln.startswith("  Suggested because: "):
                                reason_txt = ln[len("  Suggested because: "):]
                                self.assertTrue(
                                    reason_txt in ("approved", "earliest")
                                    or reason_txt.startswith("the earlier ones cannot be kept: "),
                                    reason_txt,
                                )
                                continue
                            if ln.startswith("  To attach them to the kept lesson: "):
                                ln = "  Or: " + ln[len("  To attach them to the kept lesson: "):]
                            out_lines.append(ln)
                            if pending_no_cmd is not None and ln.startswith("  Files a member reaches and the kept lesson does not: "):
                                out_lines.append(pending_no_cmd)
                                pending_no_cmd = None
                            if ln.startswith("  Merge would be refused: "):
                                for lid_k, note_v in note_by_first_id.items():
                                    if f"`{lid_k}`:" in ln:
                                        pending_no_cmd = "  No command: " + note_v
                                        break
                            continue
                        out_lines.append(ln)
                    if pending_no_cmd is not None:
                        out_lines.append(pending_no_cmd)
                    return "\n".join(out_lines) + ("\n" if md_text.endswith("\n") else "")

                t_val = head_parsed.get("lib", {}).get("tidy_report", {})
                if isinstance(t_val, dict) and t_val.get("ok") and "duplicates" in t_val:
                    dups_copy = [dict(d) for d in t_val["duplicates"]]
                    for d in t_val["duplicates"]:
                        self.assertNotIn("keepReason", d)  # the usual choice is not explained: 9.3.0 output
                    t_val["formatted"] = _normalize_tidy_md_to_930(t_val["formatted"], dups_copy)
                    if "tidy_markdown" in head_parsed.get("files", {}):
                        head_parsed["files"]["tidy_markdown"] = _normalize_tidy_md_to_930(
                            head_parsed["files"]["tidy_markdown"], dups_copy
                        )
                cli_tidy_j = head_parsed.get("cli", {}).get("tidy_json", {})
                if isinstance(cli_tidy_j, dict) and cli_tidy_j.get("code") == 0 and cli_tidy_j.get("stdout"):
                    cj = json.loads(cli_tidy_j["stdout"])
                    if cj.get("ok") and "duplicates" in cj:
                        dups_cj = [dict(d) for d in cj["duplicates"]]
                        for d in cj["duplicates"]:
                            self.assertNotIn("keepReason", d)
                        cj["formatted"] = _normalize_tidy_md_to_930(cj["formatted"], dups_cj)
                        cli_tidy_j["stdout"] = json.dumps(cj, ensure_ascii=False, sort_keys=True)
                        cli_tidy_m = head_parsed.get("cli", {}).get("tidy_md", {})
                        if isinstance(cli_tidy_m, dict) and cli_tidy_m.get("code") == 0:
                            cli_tidy_m["stdout"] = _normalize_tidy_md_to_930(cli_tidy_m["stdout"], dups_cj)

                for top_key in base_parsed:
                    if base_parsed[top_key] != head_parsed[top_key]:
                        for sub_key in base_parsed[top_key]:
                            self.assertEqual(
                                base_parsed[top_key][sub_key],
                                head_parsed[top_key][sub_key],
                                f"Mismatch in {variant}.{top_key}.{sub_key}",
                            )
                # Every key was compared above; the whole documents are equal once the version is read as above.
                self.assertEqual(json.dumps(base_parsed, sort_keys=True).encode("utf-8"),
                                 json.dumps(head_parsed, sort_keys=True).encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
