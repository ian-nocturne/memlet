"""Read evidence belongs to Memlet's synchronous stack, never to foreign callbacks."""

from __future__ import annotations

import concurrent.futures
import contextlib
import contextvars
import copy
import inspect
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, Tuple
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from memlet import (
    handover_bridge as hb,
    memlet_bridge as mb,
    memlet_cortex as mc,
    memlet_dream as md,
    memlet_dreamcycle as dc,
    memlet_dreamtidy as mdt,
    memlet_graph as mg,
    memlet_hooks as mh,
    memlet_skillbook as msb,
    memlet_tidy as mt,
    memlet_wiki as mw,
)

TARGET_MODULES: Tuple[Tuple[str, Any], ...] = (
    ("memlet_bridge", mb),
    ("memlet_dream", md),
    ("memlet_wiki", mw),
    ("memlet_graph", mg),
    ("memlet_tidy", mt),
    ("memlet_skillbook", msb),
    ("handover_bridge", hb),
    ("memlet_dreamtidy", mdt),
    ("memlet_dreamcycle", dc),
    ("memlet_hooks", mh),
    ("memlet_cortex", mc),
)

# Explicit allowlist of public functions in the 11 modules that do NOT read the queue,
# snapshots, config, promoted page, instruction files or ledger, with one-line reasons.
NON_READER_ALLOWLIST: Dict[Tuple[str, str], str] = {
    # memlet_bridge
    ("memlet_bridge", "agent_transcript_roots"): "Resolves user-home transcript directory paths from environment/home arguments only.",
    ("memlet_bridge", "attachment_metadata"): "Formats an in-memory attachmentIncomplete flag from an already-built context mapping.",
    ("memlet_bridge", "dream_settings"): "Extracts dream mode and agreement defaults from an in-memory config dict.",
    ("memlet_bridge", "hidden_hint_blocked"): "Screens a plain string against the static permission-phrase patterns.",
    ("memlet_bridge", "hint_item_label"): "Selects the display label string from an in-memory item dict.",
    ("memlet_bridge", "hint_label"): "Returns the constant hint section heading string.",
    ("memlet_bridge", "lesson_text"): "Formats display text from an in-memory lesson node dict.",
    ("memlet_bridge", "merge_state"): "Computes merge state purely from in-memory nodes and trust mappings.",
    ("memlet_bridge", "permission_phrase"): "Matches a string against static permission-loosening regular expressions.",
    ("memlet_bridge", "render_approved_lesson"): "Formats an already-projected approved lesson dict as Markdown.",
    ("memlet_bridge", "render_capsule_lesson"): "Formats an already-projected lesson dict for a handover capsule.",
    ("memlet_bridge", "render_recall_lesson"): "Formats an already-projected lesson dict for unified recall.",
    ("memlet_bridge", "safe_lesson_display"): "Masks secrets and strips control characters on an in-memory string.",
    ("memlet_bridge", "tier_config"): "Computes rejected-quote hashes purely from in-memory graph and config dicts.",
    ("memlet_bridge", "unsafe_contributed_string"): "Checks an in-memory string for control characters or unmasked secrets.",
    # memlet_dream
    ("memlet_dream", "approval_state"): "Verifies human approval digest fields on an in-memory node or edge dict.",
    ("memlet_dream", "category_label"): "Maps a category code string to its human-readable label.",
    ("memlet_dream", "citation_failure_notice"): "Formats a summary string from an in-memory list of citation failures.",
    ("memlet_dream", "citation_key"): "Builds a tuple key from an in-memory citation dict.",
    ("memlet_dream", "count_redactions_by_type"): "Counts redaction placeholders in an in-memory string.",
    ("memlet_dream", "edge_approval_digest"): "Computes SHA-256 approval digest from an in-memory edge dict.",
    ("memlet_dream", "estimate_tokens"): "Estimates token count purely from string length and character classes.",
    ("memlet_dream", "extract_candidates_from_snapshot"): "Extracts heuristic lesson candidates from an in-memory snapshot dict.",
    ("memlet_dream", "extract_candidates_with_report"): "Extracts heuristic lesson candidates and cap report from an in-memory snapshot dict.",
    ("memlet_dream", "extract_line_timestamp"): "Parses an ISO timestamp prefix from an in-memory log line.",
    ("memlet_dream", "call_foreign"): "Suspends read evidence while invoking caller-supplied code.",
    ("memlet_dream", "fresh_read"): "Decorator that installs a fresh per-read state at public entry points.",
    ("memlet_dream", "graph_sha256"): "Hashes an in-memory ontology graph dict canonically.",
    ("memlet_dream", "has_unmasked_secret_or_canary"): "Checks an in-memory string for secret patterns or canary tokens.",
    ("memlet_dream", "item_quotes_consistent"): "Checks quote consistency across pins of an in-memory node or edge dict.",
    ("memlet_dream", "lesson_display_fields"): "Extracts quote lines, citations and line roles from an in-memory node dict.",
    ("memlet_dream", "lesson_edge_sources"): "Extracts edge type and span metadata from an in-memory node dict.",
    ("memlet_dream", "lesson_lines"): "Collects ordered citation line dicts from an in-memory node dict.",
    ("memlet_dream", "lesson_quote_digest"): "Hashes normalised current quotes of an in-memory node dict.",
    ("memlet_dream", "lesson_text"): "Joins sanitised quote lines of an in-memory node dict.",
    ("memlet_dream", "mask_secrets"): "Redacts secret-shaped tokens in an in-memory string.",
    ("memlet_dream", "memlet_echo"): "Detects Memlet hook/CLI echo lines in an in-memory transcript string.",
    ("memlet_dream", "merge_approval_digest"): "Hashes merge-relevant fields of an in-memory node dict.",
    ("memlet_dream", "node_approval_digest"): "Hashes approval-relevant fields of an in-memory node dict.",
    ("memlet_dream", "normalised_quote"): "Normalises whitespace and case on an in-memory quote string.",
    ("memlet_dream", "parse_iso_dt"): "Parses an ISO-8601 string into a UTC datetime.",
    ("memlet_dream", "parse_span"): "Parses a file:start-end span string into components.",
    ("memlet_dream", "pin_quote_consistent"): "Verifies SHA-256 consistency within an in-memory pin dict.",
    ("memlet_dream", "queue_lock"): "Acquires an advisory flock file lock around queue mutations.",
    ("memlet_dream", "queue_lock_path"): "Computes the lockfile Path next to a queue file Path.",
    ("memlet_dream", "redact_secret_blocks"): "Redacts multiline PEM/key blocks in an in-memory string.",
    ("memlet_dream", "render_superseded_quarantine"): "Formats superseded nodes from an in-memory list as Markdown.",
    ("memlet_dream", "replaced_line_index"): "Indexes replaced citations and quotes across in-memory projected items.",
    ("memlet_dream", "safe_truncate_masked"): "Truncates and secret-masks an in-memory string.",
    ("memlet_dream", "sanitize_id"): "Validates and normalises an identifier string.",
    ("memlet_dream", "sanitize_node_id"): "Validates and normalises a graph node identifier string.",
    ("memlet_dream", "sha256_hex"): "Computes the hex SHA-256 digest of a string or bytes.",
    ("memlet_dream", "superseded_lesson"): "Checks whether an in-memory node dict represents a superseded lesson.",
    ("memlet_dream", "work_counters"): "Context manager that binds an optional work-counter dict for tests.",
    # memlet_wiki
    ("memlet_wiki", "contained_path_problem"): "Checks lexical and symlink path containment inside a root directory without read-state caches.",
    ("memlet_wiki", "content_tag"): "Hashes raw bytes into a content tag string.",
    ("memlet_wiki", "estimate_tokens"): "Estimates token count purely from an in-memory string.",
    ("memlet_wiki", "first_managed_problem"): "Returns the first managed_path_problem across relative paths without read-state caches.",
    ("memlet_wiki", "format_benchmark_markdown"): "Renders an in-memory benchmark report dict to Markdown.",
    ("memlet_wiki", "handover_signature"): "Computes the SHA-256 signature of an in-memory handover capsule dict.",
    ("memlet_wiki", "instruction_path_shape"): "Checks whether a relative path string matches instruction-file naming rules.",
    ("memlet_wiki", "instruction_source_allowed"): "Checks whether a path/text is an allowed non-generated instruction source without read-state caches.",
    ("memlet_wiki", "is_instruction_path"): "Checks whether a path inside root is a non-symlinked instruction path without read-state caches.",
    ("memlet_wiki", "is_memlet_skill"): "Checks frontmatter markers on an in-memory Markdown string.",
    ("memlet_wiki", "managed_path_problem"): "Checks symlink and containment safety of a `.memlet` path via lstat only.",
    ("memlet_wiki", "managed_target"): "Splits a target Path under `.memlet` into `(root, rel)`.",
    ("memlet_wiki", "memlet_write_lock"): "Acquires the `.memlet/.write.lock` file lock.",
    ("memlet_wiki", "read_regular_bytes"): "Reads bounded bytes from an uncached regular file descriptor with O_NOFOLLOW.",
    ("memlet_wiki", "read_regular_text"): "Decodes bounded UTF-8 text via read_regular_bytes without read-state caches.",
    ("memlet_wiki", "verify_handover_checksum"): "Verifies the checksum field of an in-memory handover capsule dict.",
    ("memlet_wiki", "verify_handover_markdown"): "Verifies the embedded checksum comment of an in-memory Markdown string.",
    ("memlet_wiki", "write_managed_bytes"): "Atomically writes bytes under `.memlet` and drops caches on the current call stack.",
    ("memlet_wiki", "write_managed_path"): "Atomically writes UTF-8 text to a target Path under `.memlet` and drops caches on the current call stack.",
    ("memlet_wiki", "write_managed_text"): "Atomically writes UTF-8 text under `.memlet` and drops caches on the current call stack.",
    # memlet_tidy
    ("memlet_tidy", "command"): "Formats a shell-quoted `memlet ...` CLI command string from arguments.",
    ("memlet_tidy", "kept_lesson_order"): "Orders candidate lesson IDs purely from an in-memory lessons mapping.",
    ("memlet_tidy", "render_markdown"): "Renders an already-computed tidy report dict to Markdown.",
    ("memlet_tidy", "session_key"): "Strips hash suffixes from a snapshot filename string.",
    # memlet_skillbook
    ("memlet_skillbook", "valid_lesson_id"): "Checks whether a value matches the safe lesson ID pattern.",
    ("memlet_skillbook", "valid_skill_name"): "Checks whether a string is a valid Agent Skill directory/frontmatter name.",
    ("memlet_skillbook", "yaml_quote"): "Quotes a scalar string safely for YAML frontmatter.",
    ("memlet_skillbook", "yaml_unquote"): "Unquotes a YAML frontmatter scalar string.",
    # handover_bridge
    ("handover_bridge", "integrity"): "Computes the integrity hash of an in-memory bridge value.",
    ("handover_bridge", "verify"): "Verifies the integrity hash of an in-memory bridge value.",
    # memlet_dreamtidy
    ("memlet_dreamtidy", "lessons_by_id_from_loaded"): "Builds lesson digest/visibility entries purely from in-memory loaded mappings.",
    ("memlet_dreamtidy", "proposal_digest"): "Computes the canonical SHA-256 digest of sorted lesson digests.",
    ("memlet_dreamtidy", "proposal_id"): "Formats a `TDP-...` identifier from sorted lesson digests.",
    ("memlet_dreamtidy", "proposal_state"): "Evaluates proposal state purely from an in-memory proposal record and lessons_by_id map.",
    ("memlet_dreamtidy", "valid_tidy_receipt"): "Verifies fields and SHA-256 seal of an in-memory tidy receipt dict.",
    # memlet_dreamcycle
    ("memlet_dreamcycle", "evidence_pins"): "Extracts unique pin dicts from an in-memory node dict.",
    ("memlet_dreamcycle", "fixture_items"): "Projects in-memory node dicts into compact fixture expectation records.",
    ("memlet_dreamcycle", "mask_fixture_text"): "Masks secrets, home paths and usernames in an in-memory string.",
    ("memlet_dreamcycle", "seal_receipt"): "Computes and attaches `receiptSha256` to an in-memory receipt dict.",
    ("memlet_dreamcycle", "trust_content"): "Extracts canonical content fields from an in-memory node dict.",
    ("memlet_dreamcycle", "trust_digest"): "Hashes canonical trust content of an in-memory node dict.",
    ("memlet_dreamcycle", "trusted_record"): "Verifies a proposer or verifier receipt dict against an in-memory node dict.",
    # memlet_hooks
    ("memlet_hooks", "default_cli_path"): "Returns the local `memory_cli.py` path adjacent to the package.",
    ("memlet_hooks", "git_hook_script"): "Renders the static shell script text for a Memlet git hook.",
    ("memlet_hooks", "hook_command"): "Formats the hook command string for `.claude/settings*.json`.",
    ("memlet_hooks", "memlet_hook_event"): "Extracts the Memlet hook event name from a command string if present.",
    ("memlet_hooks", "needs_backup"): "Checks in-memory bytes against recorded install hashes to decide backup creation.",
}


def _git(cwd: Path, *args: str, env: Dict[str, str]) -> None:
    res = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise RuntimeError(f"git {args} failed: {res.stderr}")


def _build_fixture_repo(root: Path, env: Dict[str, str]) -> Dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", env=env)
    _git(root, "config", "user.email", "q2@example.test", env=env)
    _git(root, "config", "user.name", "Q2 Tester", env=env)

    (root / "billing.py").write_text(
        "def refund(order_id):\n    return {'order': order_id, 'ok': True}\n\n"
        "def charge(order_id):\n    return {'order': order_id, 'charged': True}\n",
        encoding="utf-8",
    )
    (root / "CLAUDE.md").write_text(
        "# Project Rules\n\n"
        "- Never call `refund` in `billing.py` (`billing.py::refund`) without the retry wrapper.\n"
        "- Always log `charge` calls in `billing.py` (`billing.py::charge`).\n",
        encoding="utf-8",
    )
    _git(root, "add", "billing.py", "CLAUDE.md", env=env)
    _git(root, "commit", "-q", "-m", "initial", env=env)

    mw.init_project(root)
    mw.import_rules(root)

    tx1 = root.parent / f"{root.name}_s1.log"
    tx2 = root.parent / f"{root.name}_s2.log"
    tx3 = root.parent / f"{root.name}_s3.log"
    tx1.write_text(
        "2026-11-02T10:00:00Z [user] Never call refund() in billing.py without the retry wrapper; timeouts double-charge.\n",
        encoding="utf-8",
    )
    tx2.write_text(
        "2026-11-03T10:00:00Z [user] Always wrap refund() in billing.py with the retry helper so timeouts never charge twice.\n",
        encoding="utf-8",
    )
    tx3.write_text(
        "2026-11-04T10:00:00Z [user] Workaround: pass dry_run=True to charge() in billing.py when replaying invoices.\n",
        encoding="utf-8",
    )
    assert mb.learn_from_transcript(root, tx1)["ok"]
    id_a = mb.learn_pending(root, _record_review=False)["pendingNodes"][0]["id"]
    assert mb.learn_from_transcript(root, tx2)["ok"]
    id_b = [x["id"] for x in mb.learn_pending(root, _record_review=False)["pendingNodes"] if x["id"] != id_a][0]
    assert mb.learn_from_transcript(root, tx3)["ok"]
    id_c = [x["id"] for x in mb.learn_pending(root, _record_review=False)["pendingNodes"] if x["id"] not in (id_a, id_b)][0]

    assert mb.learn_approve(root, [id_a, id_b], approver="Alice", paths=["billing.py"])["ok"]
    msb.write_skill(root)

    # Create one open dream --tidy proposal so status/dismiss/report_section exercise real records.
    def _tidy_runner(_argv, _prompt, *, stdin_text="", **_kw):
        rows = [json.loads(line) for line in stdin_text.splitlines() if line.startswith('{"id"')]
        ids = [r["id"] for r in rows if "refund" in r["text"]]
        return json.dumps([{"same": ids}]) if len(ids) >= 2 else "[]"

    tidy_res = mdt.dream_tidy_run(root, via=("claude",), runner=_tidy_runner)
    assert tidy_res["ok"], tidy_res
    prop_id = tidy_res["addedProposals"][0]

    # Also ingest one raw session for dreamcycle status/next/run recipes.
    raw_sess = root.parent / f"{root.name}_dream_sess.jsonl"
    raw_sess.write_text(
        json.dumps({"type": "user", "timestamp": "2026-11-05T10:00:00Z", "message": {"role": "user", "content": "Never skip charge() audit in billing.py."}}) + "\n",
        encoding="utf-8",
    )
    ing = dc.dream_ingest(root, raw_sess)
    assert ing["ok"], ing

    return {
        "repo": root,
        "id_a": id_a,
        "id_b": id_b,
        "id_c": id_c,
        "prop_id": prop_id,
        "tx_extra": tx3,
        "raw_sess": raw_sess,
    }


def _clone_ctx(base_ctx: Dict[str, Any], dest: Path) -> Dict[str, Any]:
    shutil.copytree(base_ctx["repo"], dest, symlinks=True)
    out = dict(base_ctx, repo=dest)
    return out


def _canon(value: Any, repo: Path) -> Any:
    """Normalise volatile timestamps, run IDs and temporary repo paths for deterministic comparison."""
    repo_str = str(repo)
    repo_real = str(repo.resolve())

    def _walk(v: Any, key: str = "") -> Any:
        if key in ("wallSeconds", "date", "at", "updatedAt", "generatedAt", "runId", "head", "elapsedMs", "graphSha256", "replacesGraphSha256", "outJsonSha256", "outDbSha256"):
            return "<VOLATILE>"
        if isinstance(v, (bytes, bytearray)):
            return v.hex()
        if isinstance(v, Path):
            v = str(v)
        if isinstance(v, str):
            s = v.replace(repo_real, "<REPO>").replace(repo_str, "<REPO>")
            return s
        if isinstance(v, dict):
            if v.__class__.__name__ == "_LessonPathContext":
                return {k: _walk(val, k) for k, val in sorted(v.items(), key=lambda kv: str(kv[0])) if k != "work"}
            return {str(k): _walk(val, str(k)) for k, val in sorted(v.items(), key=lambda kv: str(kv[0]))}
        if isinstance(v, (list, tuple)):
            return [_walk(x, key) for x in v]
        if isinstance(v, set):
            return sorted(_walk(x, key) for x in v)
        if isinstance(v, sqlite3.Connection):
            v.close()
            return "<SQLITE_CONN>"
        if callable(v):
            return "<CALLABLE>"
        return v

    return _walk(value)


def _build_recipes() -> Tuple[Dict[Tuple[str, str], Callable[[Dict[str, Any]], Any]], set]:
    """Return `(recipes, mutating_keys)` covering every non-allowlisted public function in the 11 modules."""
    mutating = {
        ("memlet_bridge", "accept_anchor"),
        ("memlet_bridge", "learn_approve"),
        ("memlet_bridge", "learn_from_transcript"),
        ("memlet_bridge", "learn_merge"),
        ("memlet_bridge", "learn_paths"),
        ("memlet_bridge", "learn_pending"),
        ("memlet_bridge", "learn_reject"),
        ("memlet_bridge", "learn_unmerge"),
        ("memlet_bridge", "promote_approved"),
        ("memlet_dream", "approve_queue_items"),
        ("memlet_dream", "main"),
        ("memlet_dream", "record_citation_reviews"),
        ("memlet_dream", "reject_queue_items"),
        ("memlet_dream", "run_dream_build"),
        ("memlet_dream", "write_merge_decisions"),
        ("memlet_dream", "write_ontology_sqlite"),
        ("memlet_wiki", "connect_managed_sqlite"),
        ("memlet_wiki", "import_rules"),
        ("memlet_wiki", "init_project"),
        ("memlet_wiki", "repin_legacy_ts_anchors"),
        ("memlet_wiki", "run_refactoring_comparison_benchmark"),
        ("memlet_skillbook", "write_skill"),
        ("handover_bridge", "write"),
        ("memlet_dreamtidy", "dream_tidy_dismiss"),
        ("memlet_dreamtidy", "dream_tidy_run"),
        ("memlet_dreamcycle", "dream_ingest"),
        ("memlet_dreamcycle", "dream_next"),
        ("memlet_dreamcycle", "dream_run"),
        ("memlet_dreamcycle", "dream_skip_failed"),
        ("memlet_dreamcycle", "dream_submit"),
        ("memlet_hooks", "apply_claude"),
        ("memlet_hooks", "apply_git"),
        ("memlet_hooks", "hooks_command"),
        ("memlet_hooks", "init_hooks_command"),
        ("memlet_hooks", "run_hook"),
    }

    def _q_and_snap(repo: Path):
        q = json.loads((repo / mb.QUEUE_REL).read_text(encoding="utf-8"))
        snap = md.load_snapshot(repo / mb.SNAPSHOT_REL)
        cfg, _ = mw.read_config(repo)
        return q, snap, cfg or {}

    def _run_main_recall(ctx: Dict[str, Any]) -> Any:
        repo = ctx["repo"]
        out_md = repo.parent / f"{repo.name}_recall_out.md"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = md.main([
                "dream-recall",
                "--graph", str(repo / mb.QUEUE_REL),
                "--snapshot", str(repo / mb.SNAPSHOT_REL),
                "--query", "refund",
                "--budget", "1200",
                "--out-md", str(out_md),
            ])
        return {"rc": rc, "len": len(buf.getvalue())}

    def _run_dream_build_recipe(ctx: Dict[str, Any]) -> Any:
        repo = ctx["repo"]
        out_json = repo.parent / f"{repo.name}_built_q.json"
        return md.run_dream_build(str(repo / mb.SNAPSHOT_REL), out_json=str(out_json))

    def _write_sqlite_recipe(ctx: Dict[str, Any]) -> Any:
        repo = ctx["repo"]
        q, snap, _ = _q_and_snap(repo)
        db_out = repo.parent / f"{repo.name}_extra.sqlite"
        md.write_ontology_sqlite(db_out, q, snapshot_files=snap)
        return db_out.is_file()

    def _dream_submit_recipe(ctx: Dict[str, Any]) -> Any:
        repo = ctx["repo"]
        nxt = dc.dream_next(repo)
        return dc.dream_submit(repo, nxt["runId"], nxt["batchId"], [], proposer={"vendor": "claude", "model": "claude-sonnet-4-6", "origin": "untrusted", "isolation": "none"})

    def _dream_skip_recipe(ctx: Dict[str, Any]) -> Any:
        repo = ctx["repo"]
        nxt = dc.dream_next(repo)
        return dc.dream_skip_failed(repo, nxt["batchId"], dry_run=True)

    def _tidy_section_recipe(ctx: Dict[str, Any]) -> Any:
        repo = ctx["repo"]
        q, _, _ = _q_and_snap(repo)
        loaded = mt._load_lessons(repo, q)
        merge = mt.merge_checks(repo, loaded["nodes"], context={"graph": q})
        return mdt.tidy_report_section(
            repo,
            lessons=loaded["lessons"],
            nodes=loaded["nodes"],
            merge=merge,
            merge_ok=merge["available"],
            cmd=lambda w, o: mt.command(w, o, repo=None),
        )

    def _apply_claude_recipe(ctx: Dict[str, Any]) -> Any:
        repo = ctx["repo"]
        plan = mh.plan_claude(repo, "local", remove=False, prefix="python3 memory_cli.py", cfg={})
        mh.apply_claude(repo, "local", plan, {})
        return (repo / ".claude" / "settings.local.json").is_file()

    def _apply_git_recipe(ctx: Dict[str, Any]) -> Any:
        repo = ctx["repo"]
        plan = mh.plan_git(repo, "python3 memory_cli.py", remove=False, cfg={})
        return mh.apply_git(plan)

    def _run_hook_recipe(ctx: Dict[str, Any]) -> Any:
        repo = ctx["repo"]
        inp = io.StringIO(json.dumps({"cwd": str(repo), "session_id": "s1", "hook_event_name": "SessionStart", "source": "startup"}))
        out = io.StringIO()
        err = io.StringIO()
        rc = mh.run_hook("session-start", stdin=inp, stdout=out, stderr=err)
        return {"rc": rc, "out": out.getvalue()}

    recipes: Dict[Tuple[str, str], Callable[[Dict[str, Any]], Any]] = {
        # memlet_bridge (28)
        ("memlet_bridge", "accept_anchor"): lambda c: mb.accept_anchor(c["repo"], "billing.py::refund"),
        ("memlet_bridge", "changed_check"): lambda c: mb.changed_check(c["repo"], files=["billing.py"]),
        ("memlet_bridge", "decision_events"): lambda c: mb.decision_events(c["repo"]),
        ("memlet_bridge", "dream_items"): lambda c: mb.dream_items(c["repo"], tiers=("approved", "hint", "pending")),
        ("memlet_bridge", "dream_tiers"): lambda c: mb.dream_tiers(*_q_and_snap(c["repo"])[:1], snapshot=_q_and_snap(c["repo"])[1], config=_q_and_snap(c["repo"])[2]),
        ("memlet_bridge", "enrich_promoted_rules"): lambda c: mb.enrich_promoted_rules(c["repo"], []),
        ("memlet_bridge", "item_tier"): lambda c: mb.item_tier(_q_and_snap(c["repo"])[0]["nodes"][0], snapshot=_q_and_snap(c["repo"])[1], config=_q_and_snap(c["repo"])[2]),
        ("memlet_bridge", "item_trust"): lambda c: mb.item_trust(_q_and_snap(c["repo"])[0]["nodes"][0], snapshot=_q_and_snap(c["repo"])[1], config=_q_and_snap(c["repo"])[2]),
        ("memlet_bridge", "learn_approve"): lambda c: mb.learn_approve(c["repo"], [c["id_c"]], approver="Bob"),
        ("memlet_bridge", "learn_from_transcript"): lambda c: mb.learn_from_transcript(c["repo"], c["tx_extra"]),
        ("memlet_bridge", "learn_merge"): lambda c: mb.learn_merge(c["repo"], c["id_a"], [c["id_b"]], approver="Bob"),
        ("memlet_bridge", "learn_paths"): lambda c: mb.learn_paths(c["repo"], [c["id_a"]], paths=["billing.py"], approver="Bob"),
        ("memlet_bridge", "learn_pending"): lambda c: mb.learn_pending(c["repo"], _record_review=False),
        ("memlet_bridge", "learn_reject"): lambda c: mb.learn_reject(c["repo"], [c["id_c"]], approver="Bob", reason="test"),
        ("memlet_bridge", "learn_unmerge"): lambda c: mb.learn_unmerge(c["repo"], [c["id_b"]], approver="Bob"),
        ("memlet_bridge", "lesson_path_context"): lambda c: mb.valid_lesson_path(c["repo"], "billing.py", context=mb.lesson_path_context(c["repo"])),
        ("memlet_bridge", "lesson_paths"): lambda c: mb.lesson_paths(_q_and_snap(c["repo"])[0]["nodes"][0], c["repo"]),
        ("memlet_bridge", "literal_lesson_paths"): lambda c: mb.literal_lesson_paths(c["repo"], "", "Check billing.py now"),
        ("memlet_bridge", "merge_blockers"): lambda c: mb.merge_blockers(c["repo"], c["id_a"], (c["id_b"],)),
        ("memlet_bridge", "merge_path_repair"): lambda c: mb.merge_path_repair(c["repo"], c["id_a"], {c["id_b"]: ["billing.py"]}, {"graph": _q_and_snap(c["repo"])[0], "trust": mb.dream_tiers(_q_and_snap(c["repo"])[0], snapshot=_q_and_snap(c["repo"])[1], config=mw.read_config(c["repo"])[0])}),
        ("memlet_bridge", "merge_view"): lambda c: mb.merge_view(c["repo"]),
        ("memlet_bridge", "normalise_lesson_files"): lambda c: mb.normalise_lesson_files(c["repo"], ["billing.py"]),
        ("memlet_bridge", "promote_approved"): lambda c: mb.promote_approved(c["repo"]),
        ("memlet_bridge", "promoted_rule_entries"): lambda c: mb.promoted_rule_entries(c["repo"]),
        ("memlet_bridge", "replaced_rule_notices"): lambda c: mb.replaced_rule_notices(c["repo"], [], []),
        ("memlet_bridge", "transcript_symlink_problem"): lambda c: mb.transcript_symlink_problem(c["tx_extra"]),
        ("memlet_bridge", "unified_recall"): lambda c: mb.unified_recall(c["repo"], "refund", budget=1200),
        ("memlet_bridge", "valid_lesson_path"): lambda c: mb.valid_lesson_path(c["repo"], "billing.py"),
        # memlet_dream (26)
        ("memlet_dream", "approve_queue_items"): lambda c: md.approve_queue_items(c["repo"] / mb.QUEUE_REL, [c["id_c"]], "Bob", snapshot_path=c["repo"] / mb.SNAPSHOT_REL, db_path=c["repo"] / mb.DB_REL),
        ("memlet_dream", "approved_view"): lambda c: md.approved_view(_q_and_snap(c["repo"])[0]),
        ("memlet_dream", "build_cortex_lineage_payload"): lambda c: md.build_cortex_lineage_payload(_q_and_snap(c["repo"])[0], citation_snapshot=_q_and_snap(c["repo"])[1]),
        ("memlet_dream", "citation_failures"): lambda c: md.citation_failures(_q_and_snap(c["repo"])[0], _q_and_snap(c["repo"])[1]),
        ("memlet_dream", "citation_review"): lambda c: md.citation_review(_q_and_snap(c["repo"])[0]["nodes"][0], "node", _q_and_snap(c["repo"])[1]),
        ("memlet_dream", "citation_review_view"): lambda c: md.citation_review_view(_q_and_snap(c["repo"])[0], _q_and_snap(c["repo"])[1]),
        ("memlet_dream", "citation_verified_graph"): lambda c: md.citation_verified_graph(_q_and_snap(c["repo"])[0], _q_and_snap(c["repo"])[1]),
        ("memlet_dream", "dream_pin_and_validate"): lambda c: md.dream_pin_and_validate([], _q_and_snap(c["repo"])[1]),
        ("memlet_dream", "format_flat_memory"): lambda c: md.format_flat_memory(_q_and_snap(c["repo"])[0], snapshot_files=_q_and_snap(c["repo"])[1]),
        ("memlet_dream", "list_pending"): lambda c: md.list_pending(c["repo"] / mb.QUEUE_REL, snapshot_files=_q_and_snap(c["repo"])[1]),
        ("memlet_dream", "load_snapshot"): lambda c: md.load_snapshot(c["repo"] / mb.SNAPSHOT_REL),
        ("memlet_dream", "load_snapshot_with_report"): lambda c: md.load_snapshot_with_report(c["repo"] / mb.SNAPSHOT_REL),
        ("memlet_dream", "main"): _run_main_recall,
        ("memlet_dream", "queue_db_in_sync"): lambda c: md.queue_db_in_sync(c["repo"] / mb.QUEUE_REL, c["repo"] / mb.DB_REL),
        ("memlet_dream", "read_jsonl_transcript"): lambda c: md.read_jsonl_transcript(c["raw_sess"]),
        ("memlet_dream", "recall_subgraph"): lambda c: md.recall_subgraph(_q_and_snap(c["repo"])[0], query_text="refund", snapshot_files=_q_and_snap(c["repo"])[1]),
        ("memlet_dream", "record_citation_reviews"): lambda c: md.record_citation_reviews(c["repo"] / mb.QUEUE_REL, mb.learn_pending(c["repo"], _record_review=False)),
        ("memlet_dream", "reject_queue_items"): lambda c: md.reject_queue_items(c["repo"] / mb.QUEUE_REL, [c["id_c"]], "Bob", reason="test", db_path=c["repo"] / mb.DB_REL),
        ("memlet_dream", "resolve_pin"): lambda c: md.resolve_pin(_q_and_snap(c["repo"])[1], next(iter(_q_and_snap(c["repo"])[1])) + ":1"),
        ("memlet_dream", "run_dream_build"): _run_dream_build_recipe,
        ("memlet_dream", "run_dream_recall"): lambda c: md.run_dream_recall(str(c["repo"] / mb.QUEUE_REL), snapshot_path=str(c["repo"] / mb.SNAPSHOT_REL), query_text="refund"),
        ("memlet_dream", "supersession_display_node"): lambda c: md.supersession_display_node(_q_and_snap(c["repo"])[0]["nodes"][0], md.approved_view(_q_and_snap(c["repo"])[0])[0]),
        ("memlet_dream", "supersession_projection"): lambda c: md.supersession_projection(md.approved_view(_q_and_snap(c["repo"])[0])[0]),
        ("memlet_dream", "verify_ontology_contract"): lambda c: md.verify_ontology_contract(_q_and_snap(c["repo"])[0], _q_and_snap(c["repo"])[1]),
        ("memlet_dream", "write_merge_decisions"): lambda c: md.write_merge_decisions(c["repo"] / mb.QUEUE_REL, _q_and_snap(c["repo"])[0], [c["id_b"]], "Bob", keep=c["id_a"], db_path=c["repo"] / mb.DB_REL),
        ("memlet_dream", "write_ontology_sqlite"): _write_sqlite_recipe,
        # memlet_wiki (21)
        ("memlet_wiki", "build_handover"): lambda c: mw.build_handover(c["repo"]),
        ("memlet_wiki", "classify_anchor"): lambda c: mw.classify_anchor(c["repo"], {"path": "billing.py", "symbol": "refund", "raw": "billing.py::refund"}),
        ("memlet_wiki", "classify_rule_quotes"): lambda c: mw.classify_rule_quotes(c["repo"], [{"quote": "Check (`billing.py::refund`)", "source": "CLAUDE.md:3"}]),
        ("memlet_wiki", "connect_managed_sqlite"): lambda c: mw.connect_managed_sqlite(c["repo"], mb.DB_REL, readonly=True).close(),
        ("memlet_wiki", "discover_instruction_files"): lambda c: mw.discover_instruction_files(c["repo"]),
        ("memlet_wiki", "exact_parser_opt_in"): lambda c: mw.exact_parser_opt_in(c["repo"]),
        ("memlet_wiki", "import_rules"): lambda c: mw.import_rules(c["repo"], dry_run=True),
        ("memlet_wiki", "import_source_files"): lambda c: mw.import_source_files(c["repo"]),
        ("memlet_wiki", "init_project"): lambda c: mw.init_project(c["repo"], dry_run=True),
        ("memlet_wiki", "iter_wiki_pages"): lambda c: mw.iter_wiki_pages(c["repo"]),
        ("memlet_wiki", "pinned_anchor_report"): lambda c: mw.pinned_anchor_report(c["repo"]),
        ("memlet_wiki", "read_config"): lambda c: mw.read_config(c["repo"]),
        ("memlet_wiki", "read_managed_text"): lambda c: mw.read_managed_text(c["repo"], "config.json"),
        ("memlet_wiki", "repin_legacy_ts_anchors"): lambda c: mw.repin_legacy_ts_anchors(c["repo"], dry_run=True),
        ("memlet_wiki", "resolve_handover_budget"): lambda c: mw.resolve_handover_budget(c["repo"]),
        ("memlet_wiki", "resolve_handover_mode"): lambda c: mw.resolve_handover_mode(c["repo"]),
        ("memlet_wiki", "rule_anchor_pins"): lambda c: mw.rule_anchor_pins(c["repo"]),
        ("memlet_wiki", "run_refactoring_comparison_benchmark"): lambda _c: mw.run_refactoring_comparison_benchmark(),
        ("memlet_wiki", "slim_output_files"): lambda c: mw.slim_output_files(c["repo"]),
        ("memlet_wiki", "trusted_wiki_pages"): lambda c: mw.trusted_wiki_pages(c["repo"]),
        ("memlet_wiki", "wiki_status"): lambda c: mw.wiki_status(c["repo"], dry_run=True),
        # memlet_graph (2)
        ("memlet_graph", "build_graph"): lambda c: mg.build_graph(c["repo"]),
        ("memlet_graph", "neighbors"): lambda c: mg.neighbors(c["repo"], "billing.py", tiers=("approved", "hint", "pending")),
        # memlet_tidy (3)
        ("memlet_tidy", "lesson_sightings"): lambda c: mt.lesson_sightings(c["repo"]),
        ("memlet_tidy", "merge_checks"): lambda c: mt.merge_checks(c["repo"]),
        ("memlet_tidy", "tidy_report"): lambda c: mt.tidy_report(c["repo"]),
        # memlet_skillbook (3)
        ("memlet_skillbook", "build_skill"): lambda c: msb.build_skill(c["repo"]),
        ("memlet_skillbook", "check_skill"): lambda c: msb.check_skill(c["repo"] / ".memlet" / "skills" / "memlet-lessons" / "SKILL.md", repo_path=c["repo"]),
        ("memlet_skillbook", "write_skill"): lambda c: msb.write_skill(c["repo"]),
        # handover_bridge (2)
        ("handover_bridge", "build"): lambda c: hb.build(c["repo"], [], [], mb.dream_items(c["repo"])["items"], "HEAD", "2026-11-05T00:00:00Z"),
        ("handover_bridge", "write"): lambda c: hb.write(c["repo"], hb.HANDOVER_FILE, "{}\n"),
        # memlet_dreamtidy (5)
        ("memlet_dreamtidy", "dream_tidy_dismiss"): lambda c: mdt.dream_tidy_dismiss(c["repo"], [c["prop_id"]]),
        ("memlet_dreamtidy", "dream_tidy_run"): lambda c: mdt.dream_tidy_run(c["repo"], via=("claude",), dry_run=True),
        ("memlet_dreamtidy", "dream_tidy_status"): lambda c: mdt.dream_tidy_status(c["repo"]),
        ("memlet_dreamtidy", "load_lessons_by_id"): lambda c: mdt.load_lessons_by_id(c["repo"]),
        ("memlet_dreamtidy", "tidy_report_section"): _tidy_section_recipe,
        # memlet_dreamcycle (6)
        ("memlet_dreamcycle", "dream_ingest"): lambda c: dc.dream_ingest(c["repo"], c["raw_sess"], dry_run=True),
        ("memlet_dreamcycle", "dream_next"): lambda c: dc.dream_next(c["repo"]),
        ("memlet_dreamcycle", "dream_run"): lambda c: dc.dream_run(c["repo"], via=("claude",), runner=lambda *_a, **_k: "[]"),
        ("memlet_dreamcycle", "dream_skip_failed"): _dream_skip_recipe,
        ("memlet_dreamcycle", "dream_status"): lambda c: dc.dream_status(c["repo"]),
        ("memlet_dreamcycle", "dream_submit"): _dream_submit_recipe,
        # memlet_hooks (11)
        ("memlet_hooks", "apply_claude"): _apply_claude_recipe,
        ("memlet_hooks", "apply_git"): _apply_git_recipe,
        ("memlet_hooks", "find_root"): lambda c: mh.find_root({"cwd": str(c["repo"])}),
        ("memlet_hooks", "git_hooks_dir"): lambda c: mh.git_hooks_dir(c["repo"]),
        ("memlet_hooks", "hooks_command"): lambda c: mh.hooks_command(c["repo"], dry_run=True),
        ("memlet_hooks", "init_hooks_command"): lambda c: mh.init_hooks_command(c["repo"], dry_run=True),
        ("memlet_hooks", "plan_claude"): lambda c: mh.plan_claude(c["repo"], "local", remove=False, prefix="python3 memory_cli.py", cfg={}),
        ("memlet_hooks", "plan_git"): lambda c: mh.plan_git(c["repo"], "python3 memory_cli.py", remove=False, cfg={}),
        ("memlet_hooks", "probe_command"): lambda c: mh.probe_command([sys.executable, str(mh.default_cli_path())], cwd=c["repo"]),
        ("memlet_hooks", "resolve_command"): lambda c: mh.resolve_command("local", c["repo"]),
        ("memlet_hooks", "run_hook"): _run_hook_recipe,
        # memlet_cortex (1)
        ("memlet_cortex", "tab_cortex"): lambda c: mc.tab_cortex({"workspaces": {"live": {"id": "live", "path": str(c["repo"])}}}),
    }
    return recipes, mutating


# Helper invoked inside a child process for A.2(a) fresh-process comparison.
def _child_eval_pure_readers(repo_dir: str, ids_json: str) -> Dict[str, Any]:
    meta = json.loads(ids_json)
    ctx = {
        "repo": Path(repo_dir),
        "id_a": meta["id_a"],
        "id_b": meta["id_b"],
        "id_c": meta["id_c"],
        "prop_id": meta["prop_id"],
        "tx_extra": Path(meta["tx_extra"]),
        "raw_sess": Path(meta["raw_sess"]),
    }
    recipes, mutating = _build_recipes()
    out: Dict[str, Any] = {}
    for key, fn in sorted(recipes.items()):
        if key in mutating:
            continue
        out[f"{key[0]}.{key[1]}"] = _canon(fn(ctx), ctx["repo"])
    return out



# These recipes exercise the undecorated routes which defeated the entry-point design.
from memlet import memlet_anchors as ma, memlet_inspector as mi, memlet_recall as mr, memlet_eval as me
import ast
import argparse
import importlib
import types
from collections.abc import Iterator
import memlet_mcp
import memory_cli

_ORIGINAL_RECIPES = _build_recipes


def _cli_recipe(module, argv, c):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action")
    module.register(sub)
    return module.dispatch(parser.parse_args([v.replace("{repo}", str(c["repo"])) for v in argv]))


def _build_recipes():
    recipes, mutating = _ORIGINAL_RECIPES()
    recipes.update({
        ("memlet_anchors", "suggest_anchors"): lambda c: ma.suggest_anchors(c["repo"]),
        ("memlet_anchors", "count_suggestions"): lambda c: ma.count_suggestions(c["repo"]),
        ("memlet_anchors", "sidecar_anchors"): lambda c: ma.sidecar_anchors(c["repo"]),
        ("memlet_anchors", "sidecar_index"): lambda c: ma.sidecar_index(c["repo"]),
        ("memlet_anchors", "list_sidecar_anchors"): lambda c: ma.list_sidecar_anchors(c["repo"]),
        ("memlet_anchors", "accept_suggestions"): lambda c: ma.accept_suggestions(c["repo"], all_unique=True, dry_run=True),
        ("memlet_anchors", "drop_sidecar_anchors"): lambda c: ma.drop_sidecar_anchors(c["repo"], dry_run=True),
        ("memlet_inspector", "build_inspector_payload"): lambda c: mi.build_inspector_payload(str(c["repo"])),
        ("memlet_inspector", "render_inspector_html"): lambda c: mi.render_inspector_html(_canon(mi.build_inspector_payload(str(c["repo"])), c["repo"])),
        ("memlet_recall", "parsed_rule_entries"): lambda c: mr.parsed_rule_entries(c["repo"]),
        ("memlet_recall", "build_context"): lambda c: mr.build_context(c["repo"]),
        ("memlet_recall", "recall"): lambda c: mr.recall(c["repo"], "refund"),
        ("memlet_recall", "recall_items"): lambda c: mr.recall_items(c["repo"], "refund"),
        ("memlet_eval", "report_eval"): lambda c: _expected_error(lambda: me.report_eval(c["repo"], run="missing")),
    })
    from memlet import code_anchors as ca, audit, compatibility, memlet_brief, memlet_index, paths
    recipes.update({
        ("audit", "audit_repository"): lambda c: audit.audit_repository(c["repo"]),
        ("audit", "audit_repo"): lambda c: audit.audit_repo(c["repo"]),
        ("compatibility", "existing_store_notice"): lambda c: compatibility.existing_store_notice(c["repo"]),
        ("memlet_brief", "load_verified_brief"): lambda c: memlet_brief.load_verified_brief(c["repo"]),
        ("memlet_index", "load_verified_index"): lambda c: memlet_index.load_verified_index(c["repo"]),
        ("code_anchors", "cached_file_sha"): lambda c: ca.cached_file_sha(c["repo"] / "billing.py"),
        ("code_anchors", "extract_file_symbol_signatures"): lambda c: ca.extract_file_symbol_signatures(c["repo"] / "billing.py"),
        ("code_anchors", "python_symbol_rebound"): lambda c: ca.python_symbol_rebound(c["repo"] / "billing.py", "refund"),
        ("code_anchors", "previous_python_symbol_hashes"): lambda c: ca.previous_python_symbol_hashes(c["repo"] / "billing.py"),
        ("code_anchors", "extract_file_symbol_hashes"): lambda c: ca.extract_file_symbol_hashes(c["repo"] / "billing.py"),
        ("code_anchors", "exact_symbol_fingerprints"): lambda c: ca.exact_symbol_fingerprints(c["repo"] / "billing.py", "python"),
        ("paths", "check_model_status"): lambda c: paths.check_model_status(c["repo"] / "absent-model"),
        ("memlet_eval", "build_contexts"): lambda c: me.build_contexts(c["repo"],
            {"dream": [], "budget": 1200, "hints": False, "run": "probe", "dreamUntil": "2026-01-01T00:00:00Z"},
            {"id": "q", "text": "refund"}),
    })
    commands = {
        "cli_anchors": ["anchors", "suggest", "--repo", "{repo}"],
        "cli_dream": ["dream", "{repo}", "--status"],
        "cli_graph": ["graph", "show", "billing.py", "--repo", "{repo}"],
        "cli_tidy": ["tidy", "--repo", "{repo}"],
        "cli_skillbook": ["skillbook", "--repo", "{repo}"],
        "cli_eval": ["eval", "report", "--repo", "{repo}", "--run", "missing"],
    }
    for name, argv in commands.items():
        mod = importlib.import_module("memlet." + name)
        recipes[(name, "dispatch")] = lambda c, mod=mod, argv=argv: _cli_recipe(mod, argv, c)
    # Only the explicitly selected read tools are dispatched; no command discovery/execution loop.
    read_tools = {
        "memlet_recall": {"query": "refund"}, "memlet_check": {"files": ["billing.py"]},
        "memlet_handover": {}, "memlet_learn": {"action": "pending"},
        "memlet_dream_next": {},
    }
    for name, args in read_tools.items():
        def call(c, name=name, args=args):
            # An inert store prevents construction or lookup of any real user memory store.
            server = memlet_mcp.MemletMCPServer(root=c["repo"], standalone=False, store=_EmptyStore())
            return server.call_tool(name, dict(args, repo_path=str(c["repo"])))
        recipes[("memlet_mcp", name)] = call
    return recipes, mutating


def _expected_error(fn):
    try:
        return fn()
    except (ValueError, OSError) as exc:
        return {"error": str(exc)}


class _EmptyStore:
    def memory(self, *args, **kwargs):
        return {"retrieval": {"items": []}}


def _reader_views(c):
    recipes, mutating = _build_recipes()
    return {".".join(k): _canon(_expected_error(lambda: f(c)), c["repo"]) for k, f in sorted(recipes.items()) if k not in mutating}


def _check_inventory(discovered, recipes, allowlist):
    missing = set(discovered) - set(recipes) - set(allowlist)
    if missing:
        raise AssertionError("Unlisted public functions: " + repr(sorted(missing)))
    if set(recipes) & set(allowlist):
        raise AssertionError("A recipe cannot also be allowlisted")
    if any(not reason.strip() for reason in allowlist.values()):
        raise AssertionError("Every exception needs a reason")


class TestV94ReadState(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="memlet-readstack-")
        cls.scratch = Path(cls._tmp.name).resolve()
        cls.env = dict(os.environ)
        for key, name in (("HOME", "home"), ("TMPDIR", "tmp"), ("CLAUDE_CONFIG_DIR", "claude"), ("CODEX_HOME", "codex")):
            d = cls.scratch / name
            d.mkdir()
            cls.env[key] = str(d)
        cls.env["GIT_CEILING_DIRECTORIES"] = str(cls.scratch.parent)
        cls._env_patch = mock.patch.dict(os.environ, cls.env)
        cls._env_patch.start()
        cls.fixture = _build_fixture_repo(cls.scratch / "fixture", cls.env)

    @classmethod
    def tearDownClass(cls):
        cls._env_patch.stop()
        cls._tmp.cleanup()

    def clone(self, name):
        return _clone_ctx(self.fixture, self.scratch / name)

    def child(self, c, code):
        result = subprocess.run([sys.executable, "-B", "-c",
            "import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
            "import test_v94_readstate as t; c=json.loads(sys.argv[2]); "
            "c.update({k:Path(c[k]) for k in ('repo','tx_extra','raw_sess')}); " + code,
            str(HERE), json.dumps(c, default=str)], cwd=HERE, env=self.env,
            capture_output=True, text=True, timeout=180)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout) if result.stdout else None

    def test_inventory_guard_rejects_added_unlisted_function(self):
        recipes, _ = _build_recipes()
        discovered = {(label, name) for label, mod in TARGET_MODULES for name, fn in vars(mod).items()
                      if inspect.isfunction(fn) and fn.__module__ == mod.__name__ and not name.startswith("_")}
        _check_inventory(discovered, recipes, NON_READER_ALLOWLIST)
        covered_modules = {name for name, _ in TARGET_MODULES}
        self.assertEqual(set(), ({k for k in recipes if k[0] in covered_modules} | set(NON_READER_ALLOWLIST)) - discovered)
        self.assertTrue(all(len(reason.strip()) >= 12 for reason in NON_READER_ALLOWLIST.values()))
        with self.assertRaisesRegex(AssertionError, "unlisted_reader"):
            _check_inventory(discovered | {("memlet_bridge", "unlisted_reader")}, recipes, NON_READER_ALLOWLIST)

    def test_scope_restoration_and_lazy_rejection(self):
        self.assertFalse(hasattr(md, "dream_read_scope"))
        self.assertFalse(hasattr(md, "_LIVE_READ_STATES"))
        self.assertTrue(all(var.get() is None for var in md._read_context_vars()))
        with md._enter_read_scope() as state:
            with md._enter_read_scope() as nested:
                self.assertIs(state, nested)
            with self.assertRaisesRegex(RuntimeError, "probe"):
                md.call_foreign(lambda: (_ for _ in ()).throw(RuntimeError("probe")))
            self.assertIs(state, md._active_read_state())
        self.assertTrue(all(var.get() is None for var in md._read_context_vars()))
        def generator():
            yield 1
        with self.assertRaises(TypeError):
            md.fresh_read(generator)
        for fn in (lambda: iter([1]), lambda: map(str, [1]), lambda: (lambda: md._active_read_state())):
            with self.assertRaises(TypeError):
                md.fresh_read(fn)()
        # The returned public-reader values, including undecorated routes, are eager.
        recipes, mutating = _build_recipes()
        for key, read in sorted(recipes.items()):
            if key not in mutating:
                value = _expected_error(lambda: read(self.fixture))
                self.assertNotIsInstance(value, Iterator, key)
                self.assertFalse(callable(value), key)

    def test_foreign_reentry_whole_surface_and_copied_context(self):
        c = self.clone("reentry")
        copied, observed, errors = [], {}, []
        # Reproduce removed config pins in anchors and broken snapshot citation in Inspector together.
        (c["repo"] / "AGENTS.md").write_text("# Rules\n- Always use `refund()` in `billing.py` for refunds.\n")
        shutil.rmtree(c["repo"] / ".memlet/wiki")
        outer_test = self
        class Store:
            def memory(self, *args):
                try:
                    outer_test.assertTrue(all(v.get() is None for v in md._read_context_vars()))
                    copied.append(contextvars.copy_context())
                    outer_test.child(c,
                        "from memlet import memlet_bridge as b; "
                        "assert b.learn_reject(c['repo'],[c['id_a']],approver='Child')['ok']; "
                        "p=c['repo']/'.memlet/config.json'; cfg=json.loads(p.read_text()); "
                        "cfg['pinnedAnchors']={}; p.write_text(json.dumps(cfg)); "
                        "[(p.write_text('Changed citation\\n')) for p in (c['repo']/b.SNAPSHOT_REL).glob('*.log')]; "
                        "(c['repo']/'CLAUDE.md').write_text('# Rules\\n- Recheck every refund.\\n'); "
                        "(c['repo']/'billing.py').write_text('def refund(order, audit=True):\\n    return order, audit\\n')")
                    observed.update(_reader_views(c))
                except BaseException as exc:
                    errors.append(exc)
                    raise
                return {"retrieval": {"items": []}}
        with md.work_counters({}), md._enter_read_scope(), mb._lesson_path_projection(c["repo"], None):
            mb.dream_items(c["repo"])
            mw._load_config_pins(c["repo"])
            mb.unified_recall(c["repo"], "refund", store=Store(), task="test")
        if errors:
            raise errors[0]
        expected = self.child(c, "print(json.dumps(t._reader_views(c),sort_keys=True))")
        self.assertEqual(expected, observed)
        self.assertEqual(expected, copied[0].run(_reader_views, c))
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            self.assertEqual(expected, pool.submit(copied[0].copy().run, _reader_views, c).result(timeout=180))

    def test_config_pin_cache_and_disk_cache_isolate_repositories(self):
        a, b = self.clone("pins-a"), self.clone("pins-b")
        for c, value in ((a, "a" * 16), (b, "b" * 16)):
            p = c["repo"] / mw.CONFIG_PATH
            cfg = json.loads(p.read_text())
            cfg["pinnedAnchors"] = {"billing.py::refund": {"astHash": value}}
            p.write_text(json.dumps(cfg))
        with md._enter_read_scope():
            for c, value in ((a, "a" * 16), (b, "b" * 16), (a, "a" * 16)):
                self.assertEqual(value, mw._load_config_pins(c["repo"])["billing.py::refund"]["astHash"])
        # Consecutive calls read the current bytes even if the repository path is identical.
        old = mb.dream_items(a["repo"])
        self.child(a, "from memlet import memlet_bridge as b; assert b.learn_reject(c['repo'],[c['id_a']],approver='Child')['ok']")
        self.assertNotEqual(old, mb.dream_items(a["repo"]))
        self.assertEqual(self.child(a, "print(json.dumps(t.mb.dream_items(c['repo'])))"), mb.dream_items(a["repo"]))

    def test_same_stack_writes_drop_cached_evidence(self):
        recipes, _ = _build_recipes()
        writers = ["learn_approve", "learn_reject", "learn_merge", "learn_unmerge", "learn_paths", "learn_from_transcript"]
        for name in writers + ["tidy_run", "tidy_dismiss", "handover_write", "skillbook_write"]:
            with self.subTest(writer=name):
                c = (_build_fixture_repo(self.scratch / ("write-" + name), self.env)
                     if name == "learn_from_transcript" else self.clone("write-" + name))
                if name == "learn_unmerge":
                    self.assertTrue(mb.learn_merge(c["repo"], c["id_a"], [c["id_b"]], approver="Writer")["ok"])
                if name == "learn_from_transcript":
                    extra = self.scratch / "new.log"
                    extra.write_text("2026-11-06T00:00:00Z [user] Never omit audit logging in billing.py.\n")
                    c["tx_extra"] = extra
                with md._enter_read_scope(), mb._lesson_path_projection(c["repo"], None):
                    mb.dream_items(c["repo"])
                    mb.lesson_path_context(c["repo"])
                    mw._load_config_pins(c["repo"])
                    if name in writers:
                        result = recipes[("memlet_bridge", name)](c)
                    elif name == "tidy_run":
                        result = mdt.dream_tidy_run(c["repo"], again=True, runner=lambda *a, **kw: "[]")
                    elif name == "tidy_dismiss":
                        result = mdt.dream_tidy_dismiss(c["repo"], [c["prop_id"]])
                    elif name == "handover_write":
                        result = mw.build_handover(c["repo"], write=True)
                    else:
                        result = msb.write_skill(c["repo"])
                    self.assertTrue(result["ok"], result)
                    actual = _reader_views(c)
                expected = self.child(c, "print(json.dumps(t._reader_views(c),sort_keys=True))")
                self.assertEqual(expected, actual)

    def test_overlap_at_five_pause_points_same_and_other_repo(self):
        for point in ("before-cache", "after-cache", "after-queue", "after-snapshot", "after-computed"):
            for same in (True, False):
                with self.subTest(point=point, same=same):
                    c = self.clone("race-" + point + str(same))
                    other = self.clone("other-" + point + str(same))
                    self.assertTrue(mb.learn_merge(c["repo"], c["id_a"], [c["id_b"]], approver="Writer")["ok"])
                    code = "print(json.dumps(t.mb.dream_items(c['repo'],query='refund') if " + repr(point == "after-computed") + " else t.mb.merge_view(c['repo'])))"
                    before = self.child(c, code)
                    ready, done = threading.Barrier(2), threading.Barrier(2)
                    reader_thread, fired, errors = threading.current_thread(), [], []
                    def pause():
                        if threading.current_thread() is reader_thread and not fired:
                            fired.append(True)
                            ready.wait(30)
                            done.wait(30)
                    def writer():
                        try:
                            ready.wait(30)
                            dest = c if same else other
                            self.assertTrue(mb.learn_reject(dest["repo"], [dest["id_a"]], approver="Writer")["ok"])
                        except BaseException as exc:
                            errors.append(exc)
                        finally:
                            done.wait(30)
                    original = {"cache": md._read_cache, "queue": mb._load_queue, "snapshot": mb._load_snapshot,
                                "computed": mb._dream_items_reader_uncached}
                    def cache(name, *a, **kw):
                        if name == "loaded_queues" and point == "before-cache": pause()
                        result = original["cache"](name, *a, **kw)
                        if name == "loaded_queues" and point == "after-cache": pause()
                        return result
                    def wrap(kind):
                        def call(*a, **kw):
                            result = original[kind](*a, **kw)
                            if point == "after-" + kind: pause()
                            return result
                        return call
                    with md._enter_read_scope() as state:
                        mb.dream_items(c["repo"])
                        with mock.patch.object(md, "_read_cache", cache), mock.patch.object(mb, "_load_queue", wrap("queue")), \
                             mock.patch.object(mb, "_load_snapshot", wrap("snapshot")), \
                             mock.patch.object(mb, "_dream_items_reader_uncached", wrap("computed")):
                            thread = threading.Thread(target=writer)
                            thread.start()
                            result = (mb.dream_items(c["repo"], query="refund", _read_state=state) if point == "after-computed"
                                      else mb.merge_view(c["repo"], _read_state=state))
                            thread.join(40)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual([], errors)
                    self.assertEqual([True], fired)
                    after = self.child(c, code)
                    self.assertIn(result, [before, after])
                    fresh = mb.dream_items(c["repo"], query="refund") if point == "after-computed" else mb.merge_view(c["repo"])
                    self.assertEqual(after, fresh)

# Explicit inventory of callback and object interfaces; discovery below rejects new entries.
FOREIGN_INTERFACES = {
    "memlet.memlet_hooks.apply_claude": ("txn",),
    "memlet.memlet_hooks.apply_git": ("txn",),
    "memlet.ts_exact.TreeSitterProvider.__init__": ("ts", "tsj", "tst"),
    "memlet.audit.LinearAlternation.__init__": ("finders",),
    "memlet.memlet_dream.call_foreign": ("fn",),
    "memlet.memlet_dream.fresh_read": ("fn",),
    "memlet.memlet_dreamtidy.tidy_report_section": ("cmd",),
    'memlet.action_verdict.run_action': ('stderr', 'stdout'),
    'memlet.audit.LazyBlockPattern.sub': ('repl',),
    'memlet.audit.LazyBlockPattern.subn': ('repl',),
    'memlet.audit.LinearAlternation.sub': ('repl',),
    'memlet.audit.LinearAlternation.subn': ('repl',),
    'memlet.cli_anchors.register': ('sub',),
    'memlet.cli_dream.register': ('sub',),
    'memlet.cli_eval.register': ('sub',),
    'memlet.cli_graph.register': ('sub',),
    'memlet.cli_plugins.add_format_option': ('parser',),
    'memlet.cli_plugins.commands': ('plugins',),
    'memlet.cli_plugins.dispatch': ('plugins',),
    'memlet.cli_skillbook.register': ('sub',),
    'memlet.cli_tidy.register': ('sub',),
    'memlet.code_anchors.register_exact_provider': ('provider',),
    'memlet.code_anchors.unregister_exact_provider': ('provider',),
    'memlet.evaluation.verify_live_provenance_and_drift': ('db',),
    'memlet.evaluation.evaluate_temporal_guard': ('db',),
    'memlet.indexing.db_identity_key': ('db',),
    'memlet.indexing.get_cached_task_rows': ('db',),
    'memlet.indexing.build_or_get_lexical_index': ('cached_doc_tokens_fn',),
    'memlet.indexing.score_lexical_postings': ('fast_lexical_fn',),
    'memlet.indexing.check_link_sync_needed': ('db', 'ensure_schema_fn'),
    'memlet.indexing.record_link_sync_state': ('db',),
    'memlet.indexing.build_shared_edges_inverted': ('add_pair_fn',),
    'memlet.memlet_bridge.unified_recall': ('store',),
    'memlet.memlet_brief.build_brief': ('runner',),
    'memlet.memlet_dreamcycle.dream_run': ('runner',),
    'memlet.memlet_dreamtidy.dream_tidy_run': ('clock', 'runner'),
    'memlet.memlet_eval.seal_questions': ('runner',),
    'memlet.memlet_eval.run_eval': ('context_builder', 'runner'),
    'memlet.memlet_eval.score_eval': ('runner',),
    'memlet.memlet_eval.eval_selftest': ('context_builder',),
    'memlet.memlet_graph.neighbors': ('_clock',),
    'memlet.memlet_hooks.resolve_command': ('which',),
    'memlet.memlet_hooks.hooks_command': ('run_init', 'which'),
    'memlet.memlet_hooks.run_hook': ('stderr', 'stdin', 'stdout'),
    'memlet.memlet_index.build_index': ('runner',),
    'memlet.memlet_tidy.lesson_sightings': ('clock',),
    'memlet.memlet_tidy.tidy_report': ('clock',),
    'memlet.standalone.legacy_standalone_notice': ('store',),
    'memlet.standalone.StandaloneService.__init__': ('catalog', 'controls', 'flow', 'hub', 'prefs', 'registry'),
    'memlet.standalone.MemoryEngine.__init__': ('root_or_store',),
    'memlet.vendor_cli.run_vendor': ('runner',),
    'memory_cli.read_item': ('store',),
    'memory_cli.backfill': ('store',),
    'memlet_mcp.MemletMCPServer.__init__': ('store',),
    'memory_intelligence.ensure_schema': ('db',),
    'memory_intelligence.index_task': ('store',),
    'memory_intelligence.build_mem_scenes': ('db',),
    'memory_intelligence.build_mem_tree': ('db',),
    'memory_intelligence.scenes': ('store',),
    'memory_intelligence.retrieve': ('db',),
    'memory_intelligence.learn': ('store',),
    'memory_intelligence.record_recall': ('store',),
    'memory_intelligence.feedback': ('store',),
    'memory_intelligence.manage_skill': ('store',),
    'memory_intelligence.sync_skills': ('store',),
    'memory_intelligence.read_skill': ('store',),
    'memory_intelligence.cases': ('store',),
    'memory_intelligence.links': ('store',),
    'memory_intelligence.stats': ('store',),
    'memory_intelligence.import_messages': ('store',),
    'memory_intelligence.evaluate_admission_control': ('db',),
    'memory_intelligence.auto_capture': ('store',),
    'memory_intelligence.consolidate_memories': ('store',),
    'memory_intelligence.sync_markdown': ('store',),
    'memory_intelligence.attribute_turn_trajectory': ('store',),
    'memory_intelligence.attribute_trajectory': ('store',),
    'memory_intelligence.preflight_check': ('store',),
    'memory_intelligence.heal_skills': ('store',),
    'memory_intelligence.verify_skill': ('store',),
    'memory_intelligence.build_subagent_capsule': ('store',),
    'memory_intelligence.build_turn_context': ('store',),
    'memory_intelligence.benchmark': ('store',),
    'memory_intelligence.inspector': ('store',),
    'memory_intelligence.export_rules': ('store',),
    'memory_intelligence.doctor': ('store',),
    'memory_intelligence.backup': ('store',),
    'memory_intelligence.restore': ('store',),
    'memory_skills.heal_compatible_skills': ('db',),
    'memory_skills.ensure_schema': ('db',),
    'memory_skills.get_skill': ('db',),
    'memory_skills.skill_events': ('db',),
    'memory_skills.stage_skill_from_experience': ('db',),
    'memory_skills.apply_feedback': ('db',),
    'memory_skills.sync_task_links': ('db',),
    'memory_skills.ppr_propagate': ('db',),
    'memory_skills.node_wiki_links': ('db',),
    'memory_skills.get_links': ('db',),
    'memory_skills.retrieve_federated_archetypes': ('db',),
    'memory_skills.snapshot_skills': ('db',),
    'memory_skills.retrieve_skills': ('db',),
    'memory_skills.sync_task_skills': ('store',),
    'memory_skills.manage_skill': ('reference_validator', 'store'),
    'memory_skills.read_skill': ('store',),
    'memory_skills.sync_skill_markdowns': ('store',),
    'memory_skills.verify_skill_execution': ('store',),
    'memory_skills.skill_metrics': ('db',),
}

_INTERFACE_NAMES = set("db store provider runner clock _clock context_builder reference_validator cached_doc_tokens_fn "
    "fast_lexical_fn ensure_schema_fn add_pair_fn which run_init stdin stdout stderr plugins sub parser root_or_store "
    "hub registry controls flow catalog prefs repl finders txn ts tsj tst".split())


def _public_interfaces():
    result = {}
    paths = sorted((HERE / "memlet").glob("*.py")) + [HERE / n for n in
        ("memory_cli.py", "memlet_mcp.py", "memory_intelligence.py", "memory_skills.py")]
    for path in paths:
        name = ".".join(path.relative_to(HERE).with_suffix("").parts)
        module = importlib.import_module(name)
        for attr, obj in vars(module).items():
            if attr.startswith("_") or getattr(obj, "__module__", None) != name:
                continue
            functions = [(attr, obj)] if inspect.isfunction(obj) else []
            if inspect.isclass(obj):
                functions = [(attr + "." + n, f) for n, f in inspect.getmembers(obj, inspect.isfunction)
                             if n == "__init__" or not n.startswith("_")]
            for qualname, fn in functions:
                params = inspect.signature(fn).parameters
                interfaces = set(params) & _INTERFACE_NAMES
                # Unnamed new callback parameters are also discovered from their annotations and calls.
                interfaces |= {n for n, p in params.items() if "Callable" in str(p.annotation)}
                try:
                    source = ast.parse(__import__("textwrap").dedent(inspect.getsource(inspect.unwrap(fn))))
                except OSError:  # dataclass-generated constructor: no callable body to inspect
                    source = ast.parse("")
                interfaces |= {node.func.id for node in ast.walk(source) if isinstance(node, ast.Call)
                               and isinstance(node.func, ast.Name) and node.func.id in params
                               and node.func.id not in {"self", "cls"}}
                data_methods = {"get", "items", "keys", "values", "setdefault", "update", "pop", "append", "extend",
                    "add", "discard", "remove", "clear", "copy", "sort", "count", "index", "split", "rsplit", "splitlines",
                    "strip", "lstrip", "rstrip", "replace", "startswith", "endswith", "lower", "upper", "casefold", "find", "rfind",
                    "encode", "decode", "join", "format", "isascii", "hex", "partition", "rpartition", "is_integer", "isdecimal", "isdigit", "isalpha", "isalnum", "isspace", "intersection", "union",
                    "difference", "issubset", "intersection_update", "difference_update", "isdisjoint",
                    "resolve", "expanduser", "relative_to", "as_posix", "as_uri", "exists", "is_file", "is_dir", "is_symlink",
                    "lstat", "stat", "read_text", "read_bytes", "write_text", "write_bytes", "mkdir", "glob", "rglob", "iterdir",
                    "with_name", "with_suffix", "unlink", "open", "absolute", "match", "search", "finditer", "findall", "fullmatch",
                    "sub", "subn", "group", "groups", "groupdict", "start", "end", "span", "isoformat", "astimezone", "timestamp"}
                interfaces |= {node.func.value.id for node in ast.walk(source) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in params and node.func.value.id not in {"self", "cls"}
                    and node.func.attr not in data_methods}
                if interfaces:
                    result[name + "." + qualname] = (fn, tuple(sorted(interfaces)))
    return result


# These are interfaces with no user-code invocation, constructors/decorators, or forbidden live execution.
FOREIGN_ALLOWLIST = {
    "memlet.memlet_dream.fresh_read": "The callable is a Memlet reader definition; the decorator's eager-only contract has dedicated tests.",
    "memlet.memlet_brief.build_brief": "The dispatch forbids brief-build; its runner adapter and vendor boundary are checked structurally.",
    "memlet.memlet_index.build_index": "The dispatch forbids index-build; its runner adapter and vendor boundary are checked structurally.",
    "memlet.memlet_hooks.run_hook": "Hooks execute only via JSON-piped CLI in the differential; all stream callbacks are checked structurally.",
    "memlet.code_anchors.unregister_exact_provider": "Compares provider identity and deletes registration; never calls the provider.",
    "memlet.indexing.record_link_sync_state": "Uses db_identity_key; its database callback is covered by the DB recipe.",
    "memlet.evaluation.evaluate_temporal_guard": "The db parameter is unused; this function projects input rows only.",
    "memlet.memlet_eval.eval_selftest": "An orchestration of the run_eval recipe; the selftest patches its vendor boundary itself.",
    "memlet.standalone.StandaloneService.__init__": "Retains optional services without invoking them; the service's foreign call sites are checked by AST.",
    "memlet.standalone.WorkService.__init__": "Alias of StandaloneService constructor, which only retains injected optional services.",
}


class _Reached(BaseException):
    pass


class _Probe:
    def __init__(self, record):
        object.__setattr__(self, "_record", record)

    def __call__(self, *args, **kwargs):
        record = object.__getattribute__(self, "_record")
        record.append(([var.get() for var in md._read_context_vars()], contextvars.copy_context()))
        raise _Reached()

    def __getattr__(self, name):
        self()


class TestForeignBoundaries(TestV94ReadState):
    # Reuse the isolated fixture, not the parent test methods.
    maxDiff = None


# Separate fixture sharing avoids duplicating the read-surface matrix in discovery.
for _name in list(vars(TestV94ReadState)):
    if _name.startswith("test_"):
        setattr(TestForeignBoundaries, _name, None)


def _foreign_inventory_test(self):
    discovered = _public_interfaces()
    actual = {k: v[1] for k, v in discovered.items()}
    self.assertEqual(FOREIGN_INTERFACES, actual)
    _check_inventory(actual, FOREIGN_INTERFACES, {})
    with self.assertRaisesRegex(AssertionError, "new_callback"):
        _check_inventory(dict(actual, new_callback=("callback",)), FOREIGN_INTERFACES, {})


TestForeignBoundaries.test_inventory = _foreign_inventory_test


def _probe_recipes(c, probe):
    from memlet import indexing, vendor_cli, code_anchors, cli_plugins, action_verdict
    return {
        "memlet.memlet_dream.call_foreign": lambda: md.call_foreign(probe),
        "memlet.memlet_bridge.unified_recall": lambda: mb.unified_recall(c["repo"], "refund", store=probe, task="test"),
        "memlet.memlet_tidy.tidy_report": lambda: mt.tidy_report(c["repo"], clock=probe),
        "memlet.memlet_tidy.lesson_sightings": lambda: mt.lesson_sightings(c["repo"], clock=probe),
        "memlet.memlet_graph.neighbors": lambda: mg.neighbors(c["repo"], "billing.py", _clock=probe),
        "memlet.memlet_dreamtidy.dream_tidy_run": lambda: mdt.dream_tidy_run(c["repo"], clock=probe, runner=probe),
        "memlet.memlet_dreamcycle.dream_run": lambda: dc.dream_run(c["repo"], runner=probe),
        "memlet.vendor_cli.run_vendor": lambda: vendor_cli.run_vendor("claude", "fixture", runner=probe),
        "memlet.code_anchors.register_exact_provider": lambda: code_anchors.register_exact_provider(probe),
        "memlet.memlet_hooks.resolve_command": lambda: mh.resolve_command("local", c["repo"], which=probe),
        "memlet.memlet_hooks.hooks_command": lambda: mh.hooks_command(c["repo"], which=probe, run_init=probe),
        "memlet.action_verdict.run_action": lambda: action_verdict.run_action({"INPUT_MODE": "invalid"}, stderr=probe),
        "memlet.cli_plugins.add_format_option": lambda: cli_plugins.add_format_option(probe),
        "memlet.cli_plugins.commands": lambda: cli_plugins.commands([probe]),
        "memlet.cli_plugins.dispatch": lambda: cli_plugins.dispatch([probe], types.SimpleNamespace(action="test")),
        "memlet.indexing.build_or_get_lexical_index": lambda: indexing.build_or_get_lexical_index(
            [{"content": "refund", "source": {}, "item_id": "one"}], "test", probe, {}),
        "memlet.indexing.build_shared_edges_inverted": lambda: indexing.build_shared_edges_inverted(
            {k: {"files": {"billing.py"}, "entities": set(), "sourceRefs": set()} for k in ("a", "b")}, ["a", "b"], probe),
        "memlet.indexing.check_link_sync_needed": lambda: indexing.check_link_sync_needed(probe, "test", ensure_schema_fn=probe),
        "memlet.standalone.MemoryEngine.__init__": lambda: importlib.import_module("memlet.standalone").MemoryEngine(probe),
        "memlet_mcp.MemletMCPServer.__init__": lambda: memlet_mcp.MemletMCPServer(root=c["repo"], store=probe),
    }


def _inside_foreign_test(self):
    c = self.clone("probes")
    recipes = _probe_recipes(c, None)
    discovered = _public_interfaces()
    unexercised = {}
    copied_contexts = []
    for key, (fn, params) in sorted(discovered.items()):
        if key in FOREIGN_ALLOWLIST:
            continue
        records = []
        probe = _Probe(records)
        invoke = _probe_recipes(c, probe).get(key)
        if invoke is None:
            if key.endswith(".register") and "sub" in params:
                invoke = lambda fn=fn: fn(probe)
            elif "repl" in params:
                # Inherited regex-style APIs need a concrete pattern object, tested below.
                unexercised[key] = "Pattern instance recipe"
                continue
            elif "db" in params or "store" in params:
                values = {"db": probe, "store": probe, "task": "test", "task_id": "test", "tid": "test", "session": "tester",
                    "sid": "tester", "item_id": "item", "skill_id": "skill:probe", "skill_or_exp_id": "skill", "revision": 1,
                    "operation": "probe", "entries": [{"kind": "fact", "content": "fixture"}], "spec": {}, "reference_validator": probe, "request_id": "request",
                    "query": "refund", "retrieval": {"items": []}, "recall_id": "recall", "outcome": "helpful", "messages": [],
                    "payload": {"text": "refund"}, "current_query": "refund", "view": {"retrieval": {"items": []}, "task": {}, "current": []}, "all_active": [], "core_kinds": set(),
                    "backup_dir": str(c["repo"]), "root": c["repo"], "raw_text": "refund billing failure", "event_key": "event", "exp_item_id": "item",
                    "op": "create", "card": {}, "source": {}, "seed_scores": {"item": 1.0}, "execution_proof": {}, "prior_primary_ids": ["item"], "recall_kinds": {"fact"}, "node_id": "item", "current_task": "test"}
                signature = inspect.signature(fn)
                kwargs = {name: values[name] for name, p in signature.parameters.items()
                          if name in values and (p.default is inspect.Parameter.empty or name in params or name in {"execution_proof", "prior_primary_ids"})}
                if "self" not in signature.parameters:
                    invoke = lambda fn=fn, kwargs=kwargs: fn(**kwargs)
        if invoke is None:
            unexercised[key] = "Needs a concrete prepared callback recipe"
            continue
        with self.subTest(interface=key), md._enter_read_scope(), md.work_counters({}), mb._lesson_path_projection(c["repo"], None):
            token = mb._ACTIVE_TRUST_PROJ_SINK.set([])
            try:
                try:
                    invoke()
                except _Reached:
                    pass
                except Exception as exc:
                    unexercised[key] = type(exc).__name__ + ": " + str(exc)
                if not records:
                    unexercised.setdefault(key, "Input path returned without invoking the supplied interface")
                for states, copied in records:
                    copied_contexts.append(copied)
                    self.assertEqual([None] * len(md._read_context_vars()), states, key)
                    self.assertEqual([None] * len(md._read_context_vars()), copied.run(lambda: [v.get() for v in md._read_context_vars()]), key)
            finally:
                mb._ACTIVE_TRUST_PROJ_SINK.reset(token)
    self.assertEqual({}, unexercised)
    self.child(c, "from memlet import memlet_bridge as b; assert b.learn_reject(c['repo'],[c['id_a']],approver='Child')['ok']")
    expected = self.child(c, "print(json.dumps(t.mb.dream_items(c['repo'])))")
    for copied in copied_contexts:
        self.assertEqual(expected, copied.run(mb.dream_items, c["repo"]))


TestForeignBoundaries.test_every_interface_suspends = _inside_foreign_test


# Exceptions name concrete Memlet code/data, never an arbitrary invocation interface.
_OWNED_CALLS = {
    ("memlet_dreamtidy.py", "_sync_dismissed", "iterate store.get('dismissed', [])"): "The proposal store is a concrete JSON dictionary.",
    ("memlet_dreamtidy.py", "_sync_dismissed", "iterate store.get('proposals', [])"): "The proposal store is a concrete JSON dictionary.",
    ("memlet_dreamtidy.py", "_select_plan_batches", "iterate store.get('plan', [])"): "The proposal store is a concrete JSON dictionary.",
    ("audit.py", "_spans", "f"): "Factories are Memlet pattern-table functions or the suspended caller adapter.",
    ("audit.py", "_spans", "find"): "Finders are Memlet functions or the suspended caller adapter.",
    ("memlet_bridge.py", "lesson_path_context", "work.clock"): "Memlet _Work wraps injected clocks at construction.",
    ("memlet_graph.py", "__init__", "self.clock"): "Memlet _Work wraps injected clocks at construction.",
    ("memlet_graph.py", "available", "self.clock"): "Memlet _Work wraps injected clocks at construction.",
    ("memlet_graph.py", "__init__", "db.execute"): "History opens its own concrete sqlite connection.",
    ("cli_plugins.py", "commands", "iterate plugins"): "The plugin table is a concrete list of Memlet modules.",
    ("cli_plugins.py", "dispatch", "iterate plugins"): "The plugin table is a concrete list of Memlet modules.",
}


def _owned_exception(node, expression, parents, filename):
    scope = node
    while scope in parents:
        scope = parents[scope]
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return (Path(filename).name, scope.name, expression) in _OWNED_CALLS
    return False


def _foreign_call_violations(source, filename):
    """Check callback callee provenance, including the aliases used by injection seams."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    interfaces = _INTERFACE_NAMES | {"now_fn", "cmd", "out", "err", "wdb"}
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        # Gather the enclosing function's parameters (nested adapters retain their outer parameters).
        scope, params = node, set()
        while scope in parents:
            scope = parents[scope]
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                params |= {p.arg for p in scope.args.args + scope.args.posonlyargs + scope.args.kwonlyargs}
                if isinstance(scope, ast.FunctionDef) and scope.name == "register" and "cli_" in filename:
                    # Objects returned by an injected parser are part of the same foreign protocol.
                    params |= {target.id for n in ast.walk(scope) if isinstance(n, ast.Assign)
                               for target in n.targets if isinstance(target, ast.Name)}
        base = callee
        while isinstance(base, ast.Attribute):
            base = base.value
        direct = isinstance(callee, ast.Name) and callee.id in params and callee.id in interfaces
        method = isinstance(callee, ast.Attribute) and isinstance(base, ast.Name) and base.id in params and (base.id in interfaces or ("cli_" in filename and callee.attr in {"add_parser", "add_subparsers", "add_argument", "add_mutually_exclusive_group"}))
        alias = ast.unparse(callee) in {"now_fn", "self.clock", "work.clock", "store.memory", "plugin.dispatch", "plugin.register"}
        if isinstance(callee, ast.BoolOp):
            alias |= any(isinstance(v, ast.Name) and v.id in interfaces & params for v in callee.values)
        if isinstance(callee, ast.Attribute):
            receiver = ast.unparse(callee.value)
            alias |= receiver in {"self.store", "self.registry", "self.hub", "self.controls", "self.flow"}
            alias |= receiver == "provider" and filename.endswith("code_anchors.py")
            alias |= receiver in {"db", "wdb", "manager"} and not filename.endswith("compatibility.py")
        if filename.endswith("memlet_hooks.py") and isinstance(callee, ast.Attribute):
            alias |= ast.unparse(callee.value) in {"out", "err"} and callee.attr in {"write", "flush"}
        if filename.endswith("memlet_dream.py") and isinstance(callee, ast.Name) and callee.id == "factory":
            ancestor = node
            while ancestor in parents:
                ancestor = parents[ancestor]
                if isinstance(ancestor, ast.FunctionDef) and ancestor.name == "_foreign_context":
                    alias = True
                    break
        if filename.endswith("audit.py") and isinstance(callee, ast.Name) and callee.id in {"f", "find"}:
            alias = True
        if isinstance(callee, ast.Attribute) and ast.unparse(callee.value) == "ledger":
            alias |= any(isinstance(a, ast.Name) and a.id in {"db", "wdb", "conn"} for a in node.args)
        if not (direct or method or alias):
            continue
        if filename.endswith("memlet_slim.py") and isinstance(base, ast.Name) and base.id == "out":
            continue  # concrete pathlib.Path destination, not a stream
        if isinstance(callee, ast.Attribute) and callee.attr in {"get", "items", "keys", "values", "setdefault", "update", "pop", "append", "extend"} and ast.unparse(callee.value) not in {"self.store", "self.registry", "self.hub", "self.controls", "self.flow"}:
            continue  # plain data mappings; not invocation interfaces
        if filename.endswith("memlet_tidy.py") and ast.unparse(callee) == "cmd":
            continue  # locally defined command formatter, never a parameter
        protected, scope = False, node
        while scope in parents:
            scope = parents[scope]
            if isinstance(scope, ast.Call) and ast.unparse(scope.func) in {"_call_foreign", "call_foreign", "md.call_foreign", "_foreign_context"}:
                protected = True
                break
        if not protected and not _owned_exception(node, ast.unparse(callee), parents, filename):
            violations.append((node.lineno, ast.unparse(callee)))
    receivers = {"store", "provider", "plugin", "root_or_store", "self.store", "self.registry", "ts", "tst", "tsj"}
    for node in ast.walk(tree):
        external = isinstance(node, ast.Attribute) and ast.unparse(node.value) in receivers and node.attr in {
            "root", "db_path", "path", "basis", "languages", "name", "Parser"}
        external |= (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "hasattr"} and bool(node.args)
            and ast.unparse(node.args[0]) in receivers)
        if not external:
            continue
        protected, scope = False, node
        while scope in parents:
            scope = parents[scope]
            if isinstance(scope, ast.Call) and ast.unparse(scope.func) in {"_call_foreign", "call_foreign", "md.call_foreign", "_foreign_context"}:
                protected = True
                break
        if not protected:
            violations.append((node.lineno, ast.unparse(node)))
    return sorted(set(violations + _tainted_foreign_calls(source, filename)))


def _tainted_foreign_calls(source, filename):
    WRAP = {"_call_foreign", "call_foreign", "md.call_foreign", "_foreign_context"}
    tree = ast.parse(source)
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    def protected(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.Call) and ast.unparse(node.func) in WRAP:
                return True
        return False
    def mentions(expr, names):
        return any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(expr)) or \
               any(isinstance(n, ast.Attribute) and ast.unparse(n) in names for n in ast.walk(expr))
    out = []
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)] + [tree]
    for holder in classes:
        self_taint = set()
        funcs = [n for n in ast.walk(holder) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for _ in range(2):  # constructor attributes first, then their use in other methods
            for fn in funcs:
                params = {a.arg for a in fn.args.args + fn.args.posonlyargs + fn.args.kwonlyargs}
                taint = (params & _INTERFACE_NAMES) | self_taint
                for node in ast.walk(fn):
                    if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None and not protected(node.value) \
                            and not (isinstance(node.value, ast.Call) and ast.unparse(node.value.func) in WRAP) and mentions(node.value, taint):
                        for tg in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                            if isinstance(tg, ast.Name): taint.add(tg.id)
                            elif isinstance(tg, ast.Attribute) and ast.unparse(tg.value) == "self":
                                self_taint.add(ast.unparse(tg)); taint.add(ast.unparse(tg))
                for node in ast.walk(fn):
                    bad = None
                    if isinstance(node, ast.Call) and not protected(node):
                        f = node.func
                        if isinstance(f, ast.Name) and f.id in taint and f.id not in params & _INTERFACE_NAMES: bad = ast.unparse(f)
                        elif isinstance(f, ast.Attribute) and ast.unparse(f) in taint: bad = ast.unparse(f)
                        elif isinstance(f, ast.Call) and mentions(f, taint): bad = ast.unparse(f)
                        elif isinstance(f, ast.Call) and ast.unparse(f.func) in {"getattr"} and f.args and mentions(f.args[0], taint): bad = ast.unparse(f)
                    if isinstance(node, (ast.For, ast.comprehension)) and not protected(node.iter if isinstance(node, ast.comprehension) else node):
                        it = node.iter
                        direct = params & _INTERFACE_NAMES
                        if (isinstance(it, ast.Name) and it.id in direct) or (isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute) and isinstance(it.func.value, ast.Name) and it.func.value.id in direct):
                            bad = "iterate " + ast.unparse(it)
                    if bad and not _owned_exception(node, bad, parents, filename):
                        out.append((getattr(node, "lineno", 0), bad))
    return sorted(set(out))


def _ast_boundaries_test(self):
    paths = sorted((HERE / "memlet").glob("*.py")) + [HERE / n for n in
        ("memory_cli.py", "memlet_mcp.py", "memory_intelligence.py", "memory_skills.py")]
    for path in paths:
        with self.subTest(path=path.name):
            self.assertEqual([], _foreign_call_violations(path.read_text(), str(path)))
    # Mutation-test the checker itself; removing any suspension from a parameter call must be detected.
    for parameter in ("store", "runner", "clock", "stdin", "provider", "db"):
        expression = parameter + ".read()" if parameter in {"store", "stdin", "provider", "db"} else parameter + "()"
        good = "def reader(" + parameter + "):\n    return _call_foreign(lambda: " + expression + ")\n"
        bad = "def reader(" + parameter + "):\n    return " + expression + "\n"
        self.assertEqual([], _foreign_call_violations(good, "fixture.py"))
        self.assertTrue(_foreign_call_violations(bad, "fixture.py"))


TestForeignBoundaries.test_all_foreign_calls_are_suspended = _ast_boundaries_test


def _explicit_snapshot_test(self):
    import tarfile
    archive = subprocess.run(["git", "archive", "0ca681f"], cwd=HERE, capture_output=True)
    if archive.returncode:
        self.skipTest("Published commit 0ca681f cannot be read: " + archive.stderr.decode(errors="replace"))
    baseline = self.scratch / "published"
    baseline.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        try:
            tar.extractall(baseline, filter="data")
        except TypeError:
            tar.extractall(baseline)
    snippet = """
import json, sys, threading, asyncio
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from memlet import memlet_bridge as b
r = Path(sys.argv[2]); ident = sys.argv[3]
n = next(n for n in json.loads((r/b.QUEUE_REL).read_text())['nodes'] if n['id']==ident)
ctx = b.lesson_path_context(r)
out = {'before': b.lesson_paths(n,r,context=ctx)}
b.learn_reject(r,[ident],approver='Snapshot')
out.update(held=b.lesson_paths(n,r,context=ctx),fresh=b.lesson_paths(n,r))
ctx2=b.lesson_path_context(r)
out['validBefore']=b.valid_lesson_path(r,'billing.py',context=ctx2)
(r/'billing.py').unlink()
def check():
    return [b.valid_lesson_path(r,'billing.py',context=ctx2),b.valid_lesson_path(r,'billing.py')]
t=threading.Thread(target=lambda:out.update(thread=check()));t.start();t.join()
async def child(): out['task']=check()
asyncio.run(child())
print(json.dumps(out,sort_keys=True))
"""
    answers = []
    for label, tree in (("baseline", baseline), ("current", HERE)):
        c = self.clone("explicit-" + label)
        proc = subprocess.run([sys.executable, "-B", "-c", snippet, str(tree), str(c["repo"]), c["id_a"]],
                              cwd=HERE, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(0, proc.returncode, proc.stderr)
        answers.append(json.loads(proc.stdout))
    self.assertEqual(answers[0], answers[1])
    self.assertEqual([True, False], answers[1]["thread"])
    self.assertEqual([True, False], answers[1]["task"])
    self.assertNotEqual(answers[1]["held"], answers[1]["fresh"])


TestV94ReadState.test_explicit_context_matches_published_snapshot = _explicit_snapshot_test


def _raw_pin_key_test(self):
    snapshot = {"one.log": ["2026-01-01T00:00:00Z [user] First wording"]}
    with md._enter_read_scope():
        first = md._resolve_pin(snapshot, "one.log:1")
        snapshot["one.log"][0] = "2026-01-01T00:00:00Z [user] Other wording"
        second = md._resolve_pin(snapshot, "one.log:1")
    self.assertNotEqual(first["sha256"], second["sha256"])


TestV94ReadState.test_pin_key_includes_bytes = _raw_pin_key_test


def _eval_probe(c, probe, mode):
    from memlet import vendor_cli
    root = c["repo"] / ("eval-probe-" + mode)
    (root / "sessions").mkdir(parents=True)
    (root / "sessions/a.json").write_text(json.dumps([{"timestamp": "2026-01-01", "text": "Repair uses cedar."}]))
    (root / "sessions/b.json").write_text(json.dumps([{"timestamp": "2026-01-03", "text": "Cedar repaired the cache."}]))
    me.init_eval(root, run="r", sessions="sessions", dream_until="2026-01-02", budget=256)
    with mock.patch.object(vendor_cli, "static_vendor_refusal", return_value=None):
        if mode == "questions":
            return me.seal_questions(root, run="r", via="claude", count=1, runner=probe)
        (root / "questions.json").write_text(json.dumps([{"id": "q", "text": "What repaired the cache?",
            "citation": {"file": "sessions/b.json", "start": 1, "end": 1}}]))
        me.seal_questions(root, run="r", source="questions.json")
        if mode == "context":
            return me.run_eval(root, run="r", via="claude", context_builder=probe, runner=probe)
        if mode == "answer":
            return me.run_eval(root, run="r", via="claude", runner=probe)
        me.run_eval(root, run="r", via="claude", runner=lambda *a, **k: "cedar")
        return me.score_eval(root, run="r", judge="codex", runner=probe)


def _section_probe(c, probe):
    q = mb._load_queue(c["repo"])[0]
    loaded = mt._load_lessons(c["repo"], q)
    merge = mt.merge_checks(c["repo"], loaded["nodes"], context={"graph": q})
    return mdt.tidy_report_section(c["repo"], lessons=loaded["lessons"], nodes=loaded["nodes"],
                                   merge=merge, merge_ok=True, cmd=probe)


_BASIC_PROBE_RECIPES = _probe_recipes


def _probe_recipes(c, probe):
    from memlet import indexing, audit
    import re
    from collections import Counter
    recipes = _BASIC_PROBE_RECIPES(c, probe)
    for method in ("sub", "subn"):
        recipes["memlet.audit.LazyBlockPattern." + method] = lambda method=method: getattr(
            audit.LazyBlockPattern("a.*?b", re.compile("a"), re.compile("b"), "b"), method)(probe, "ab")
        recipes["memlet.audit.LinearAlternation." + method] = lambda method=method: getattr(
            audit.LinearAlternation("a", [audit.regex_finder(re.compile("a"))]), method)(probe, "a")
    recipes.update({
        "memlet.ts_exact.TreeSitterProvider.__init__": lambda: importlib.import_module("memlet.ts_exact").TreeSitterProvider(probe, probe, probe, {}),
        "memlet.memlet_hooks.apply_claude": lambda: mh.apply_claude(c["repo"], "local", mh.plan_claude(c["repo"], "local", remove=False, prefix="fixture", cfg={}), {}, txn=probe),
        "memlet.memlet_hooks.apply_git": lambda: mh.apply_git(mh.plan_git(c["repo"], "fixture", remove=False, cfg={}), txn=probe),
        "memlet.audit.LinearAlternation.__init__": lambda: audit.LinearAlternation("a", [probe]).findall("a"),
        "memlet.indexing.score_lexical_postings": lambda: indexing.score_lexical_postings([{}],
            {"entries": [(Counter(refund=1), 1, "refund", [])], "avg_len": 1, "doc_freq": {"refund": 1},
             "postings": {"refund": [(0, 1)]}}, "refund", ["refund"], [], {"refund"}, probe),
        "memlet.memlet_eval.seal_questions": lambda: _eval_probe(c, probe, "questions"),
        "memlet.memlet_eval.run_eval": lambda: _eval_probe(c, probe, "context"),
        "memlet.memlet_eval.score_eval": lambda: _eval_probe(c, probe, "judge"),
        "memlet.memlet_dreamtidy.tidy_report_section": lambda: _section_probe(c, probe),
    })
    return recipes


# New parent regressions were added after declaration; do not execute them twice.
TestForeignBoundaries.test_explicit_context_matches_published_snapshot = None
TestForeignBoundaries.test_pin_key_includes_bytes = None


def _derived_parser_boundaries_test(self):
    records = []
    class Parser:
        def __getattr__(self, name):
            records.append([v.get() for v in md._read_context_vars()])
            return self
        def __call__(self, *a, **kw):
            records.append([v.get() for v in md._read_context_vars()])
            return self
        def __setattr__(self, name, value):
            records.append([v.get() for v in md._read_context_vars()])
    for name in ('anchors', 'dream', 'eval', 'graph', 'skillbook', 'tidy'):
        with self.subTest(plugin=name), md._enter_read_scope(), md.work_counters({}):
            importlib.import_module('memlet.cli_' + name).register(Parser())
    self.assertTrue(records)
    self.assertEqual([[None] * len(md._read_context_vars())] * len(records), records)


TestForeignBoundaries.test_derived_parser_objects_are_suspended = _derived_parser_boundaries_test


def _file_stat_collision_test(self):
    from memlet import code_anchors as ca
    c = self.clone("same-stat")
    p = c["repo"] / "billing.py"
    old = "def refund(a):\n    return 1\n"
    new = "def refund(b):\n    return 2\n"
    self.assertEqual(len(old), len(new))
    p.write_text(old)
    stamp = p.stat()
    readers = (ca.cached_file_sha, ca.extract_file_symbol_signatures, ca.extract_file_symbol_hashes)
    before = [f(p) for f in readers]
    p.write_text(new)
    os.utime(p, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    after = [f(p) for f in readers]
    self.assertTrue(all(a != b for a, b in zip(before, after)))
    actual = _canon(after, c["repo"])
    expected = self.child(c, "from memlet import code_anchors as a; print(json.dumps(t._canon([f(c['repo']/'billing.py') for f in (a.cached_file_sha,a.extract_file_symbol_signatures,a.extract_file_symbol_hashes)],c['repo'])))")
    self.assertEqual(expected, actual)


TestV94ReadState.test_file_reads_ignore_previous_call_stat_collision = _file_stat_collision_test
TestForeignBoundaries.test_file_reads_ignore_previous_call_stat_collision = None


def _context_manager_boundaries_test(self):
    events, copies = [], []
    def record(name):
        events.append((name, [v.get() for v in md._read_context_vars()]))
        copies.append(contextvars.copy_context())
    class Manager:
        def __enter__(self):
            record('enter')
            return 42
        def __exit__(self, *exc):
            record('exit')
    def factory():
        record('factory')
        return Manager()
    with md._enter_read_scope() as state, md.work_counters({}):
        with md._foreign_context(factory) as value:
            self.assertEqual(42, value)
            self.assertIs(state, md._active_read_state())
        with self.assertRaisesRegex(ValueError, 'boundary'):
            with md._foreign_context(factory):
                raise ValueError('boundary')
    self.assertEqual(['factory','enter','exit'] * 2, [name for name, _ in events])
    self.assertTrue(all(states == [None] * len(md._read_context_vars()) for _, states in events))
    self.assertTrue(all(c.run(md._active_read_state) is None for c in copies))


TestForeignBoundaries.test_context_manager_boundary_includes_exit = _context_manager_boundaries_test


def _implicit_protocol_boundary_test(self):
    from memlet import standalone, code_anchors
    states = []
    def record():
        states.append([var.get() for var in md._read_context_vars()])
    class Registry:
        def __bool__(self):
            record()
            return True
        def relation(self, sid):
            record()
            return True
    service = object.__new__(standalone.StandaloneService)
    service.registry = Registry()
    class Provider:
        basis = "fixture-basis-v1"
        name = "fixture"
        @property
        def languages(self):
            def produce():
                record()
                yield "fixture-language"
            return produce()
    provider = Provider()
    try:
        with md._enter_read_scope(), md.work_counters({}):
            self.assertEqual("formatted", service.augment_turn("session", "raw", "formatted", "request"))
            code_anchors.register_exact_provider(provider)
    finally:
        code_anchors.unregister_exact_provider(provider)
    self.assertEqual(3, len(states))
    self.assertEqual([[None] * len(md._read_context_vars())] * 3, states)


TestForeignBoundaries.test_implicit_protocol_and_lazy_provider_values_are_suspended = _implicit_protocol_boundary_test


def _foreign_write_invalidates_resumed_read_test(self):
    c = self.clone('foreign-writer')
    with md._enter_read_scope() as state, mb._lesson_path_projection(c['repo'], None):
        before = mb.dream_items(c['repo'])
        def write():
            self.assertTrue(all(v.get() is None for v in md._read_context_vars()))
            return mb.learn_reject(c['repo'], [c['id_a']], approver='Callback')
        self.assertTrue(md.call_foreign(write)['ok'])
        self.assertIs(state, md._active_read_state())
        after = mb.dream_items(c['repo'])
        self.assertNotEqual(before, after)
    self.assertEqual(self.child(c, "print(json.dumps(t.mb.dream_items(c['repo'])))"), after)


TestV94ReadState.test_own_write_in_foreign_callback_clears_resumed_snapshot = _foreign_write_invalidates_resumed_read_test
TestForeignBoundaries.test_own_write_in_foreign_callback_clears_resumed_snapshot = None


# Input protocols are consumed before evidence is installed. This inventory is
# independent of the normaliser: new annotated/string-collection parameters fail
# until a real invocation recipe or a specific data-only exception is supplied.
_AST_ITERATED_PARAMETERS = {'memlet.handover_bridge.build': ('anchors', 'approved', 'rules'),
 'memlet.memlet_bridge.accept_anchor': ('anchors',),
 'memlet.memlet_bridge.changed_check': ('files',),
 'memlet.memlet_bridge.dream_items': ('tiers',),
 'memlet.memlet_bridge.enrich_promoted_rules': ('rules',),
 'memlet.memlet_bridge.learn_approve': ('ids', 'paths'),
 'memlet.memlet_bridge.learn_merge': ('same',),
 'memlet.memlet_bridge.learn_paths': ('ids', 'paths'),
 'memlet.memlet_bridge.learn_reject': ('ids',),
 'memlet.memlet_bridge.learn_unmerge': ('ids',),
 'memlet.memlet_bridge.merge_blockers': ('same',),
 'memlet.memlet_bridge.merge_path_repair': ('extra',),
 'memlet.memlet_bridge.replaced_rule_notices': ('approved', 'rules'),
 'memlet.memlet_bridge.unified_recall': ('files', 'include'),
 'memlet.memlet_bridge.valid_lesson_path': ('path',),
 'memlet.memlet_dream.approve_queue_items': ('ids', 'paths'),
 'memlet.memlet_dream.build_cortex_lineage_payload': ('graph',),
 'memlet.memlet_dream.dream_pin_and_validate': ('candidates',),
 'memlet.memlet_dream.format_flat_memory': ('graph',),
 'memlet.memlet_dream.recall_subgraph': ('graph',),
 'memlet.memlet_dream.reject_queue_items': ('ids',),
 'memlet.memlet_dream.verify_ontology_contract': ('graph',),
 'memlet.memlet_dream.write_merge_decisions': ('graph', 'ids'),
 'memlet.memlet_dream.write_ontology_sqlite': ('graph',),
 'memlet.memlet_dreamcycle.dream_submit': ('candidates',),
 'memlet.memlet_dreamtidy.dream_tidy_dismiss': ('proposal_ids',),
 'memlet.memlet_dreamtidy.dream_tidy_run': ('via',),
 'memlet.memlet_dreamtidy.tidy_report_section': ('duplicate_ids', 'wording_groups'),
 'memlet.memlet_graph.neighbors': ('tiers',),
 'memlet.memlet_hooks.plan_claude': ('events',),
 'memlet.memlet_tidy.lesson_sightings': ('ids',),
 'memlet.memlet_tidy.merge_checks': ('ids',),
 'memlet.memlet_wiki.classify_rule_quotes': ('entries',)}

# Earlier string-input recipes remain covered in addition to AST-discovered iteration.
_INPUT_ITERABLES = {'memlet.audit.main': ('argv',),
 'memlet.code_anchors.exact_symbol_fingerprints': ('allowed',),
 'memlet.handover_bridge.build': ('anchors', 'approved', 'rules'),
 'memlet.memlet_anchors.accept_suggestions': ('ids',),
 'memlet.memlet_anchors.drop_sidecar_anchors': ('ids',),
 'memlet.memlet_bridge.accept_anchor': ('anchors',),
 'memlet.memlet_bridge.changed_check': ('files',),
 'memlet.memlet_bridge.dream_items': ('files', 'tiers'),
 'memlet.memlet_bridge.enrich_promoted_rules': ('rules',),
 'memlet.memlet_bridge.learn_approve': ('ids', 'paths'),
 'memlet.memlet_bridge.learn_merge': ('same',),
 'memlet.memlet_bridge.learn_paths': ('ids', 'paths'),
 'memlet.memlet_bridge.learn_reject': ('ids',),
 'memlet.memlet_bridge.learn_unmerge': ('ids',),
 'memlet.memlet_bridge.merge_blockers': ('same',),
 'memlet.memlet_bridge.merge_path_repair': ('extra',),
 'memlet.memlet_bridge.normalise_lesson_files': ('files',),
 'memlet.memlet_bridge.replaced_rule_notices': ('approved', 'rules'),
 'memlet.memlet_bridge.unified_recall': ('files', 'include'),
 'memlet.memlet_bridge.valid_lesson_path': ('path',),
 'memlet.memlet_dream.approve_queue_items': ('ids', 'paths'),
 'memlet.memlet_dream.build_cortex_lineage_payload': ('graph',),
 'memlet.memlet_dream.dream_pin_and_validate': ('candidates',),
 'memlet.memlet_dream.format_flat_memory': ('graph',),
 'memlet.memlet_dream.main': ('argv',),
 'memlet.memlet_dream.recall_subgraph': ('graph',),
 'memlet.memlet_dream.reject_queue_items': ('ids',),
 'memlet.memlet_dream.verify_ontology_contract': ('graph',),
 'memlet.memlet_dream.write_merge_decisions': ('graph', 'ids'),
 'memlet.memlet_dream.write_ontology_sqlite': ('graph',),
 'memlet.memlet_dreamcycle.dream_run': ('via',),
 'memlet.memlet_dreamcycle.dream_submit': ('candidates',),
 'memlet.memlet_dreamtidy.dream_tidy_dismiss': ('proposal_ids',),
 'memlet.memlet_dreamtidy.dream_tidy_run': ('via',),
 'memlet.memlet_dreamtidy.lessons_by_id_from_loaded': ('duplicate_ids',),
 'memlet.memlet_dreamtidy.proposal_digest': ('lesson_digests',),
 'memlet.memlet_dreamtidy.proposal_id': ('lesson_digests',),
 'memlet.memlet_dreamtidy.tidy_report_section': ('duplicate_ids', 'wording_groups'),
 'memlet.memlet_eval.run_eval': ('via',),
 'memlet.memlet_eval.seal_questions': ('via',),
 'memlet.memlet_graph.neighbors': ('tiers',),
 'memlet.memlet_hooks.plan_claude': ('events',),
 'memlet.memlet_hooks.run_hook': ('argv',),
 'memlet.memlet_inspector.main': ('argv',),
 'memlet.memlet_recall.recall': ('exclude',),
 'memlet.memlet_recall.recall_items': ('exclude',),
 'memlet.memlet_tidy.command': ('words',),
 'memlet.memlet_tidy.kept_lesson_order': ('ids',),
 'memlet.memlet_tidy.lesson_sightings': ('ids',),
 'memlet.memlet_tidy.merge_checks': ('ids',),
 'memlet.memlet_wiki.build_handover': ('rejected',),
 'memlet.memlet_wiki.classify_rule_quotes': ('entries',),
 'memlet.memlet_wiki.first_managed_problem': ('rels',),
 'memory_intelligence.build_subagent_capsule': ('files',),
 'memory_intelligence.preflight_check': ('files',)}
_INPUT_ALLOWLIST = {
    'memlet.audit.main': 'CLI argv is consumed by argparse before any reader installs evidence.',
    'memlet.memlet_inspector.main': 'CLI argv is consumed by argparse before any reader installs evidence.',
    'memlet.memlet_eval.seal_questions': 'via is one vendor name string, not a collection.',
    'memlet.memlet_eval.run_eval': 'via is one vendor name string, not a collection.',
    'memlet.memlet_hooks.run_hook': 'Hooks run only through JSON-piped CLI; its argv is already a built-in list.',
    'memory_intelligence.preflight_check': 'files accepts str or concrete list; other values are ignored without invoking their iterator.',
    'memory_intelligence.build_subagent_capsule': 'files is forwarded to preflight_check, which accepts str or concrete list and ignores iterators.',
}


def _input_inventory():
    """Discover iteration in reader bodies and their own delegated helper bodies."""
    import textwrap
    cache = {}
    def iterated(fn, seen=frozenset()):
        fn = inspect.unwrap(fn)
        if fn in cache:
            return cache[fn]
        if fn in seen:
            return set()
        seen = seen | {fn}
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            signature = inspect.signature(fn)
        except (OSError, TypeError, ValueError):
            return set()
        params = set(signature.parameters)
        aliases = {p: {p} for p in params}
        def roots(expr):
            if isinstance(expr, ast.Name):
                return aliases.get(expr.id, set())
            if isinstance(expr, ast.BoolOp):
                return set.union(set(), *(roots(v) for v in expr.values))
            if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id in {
                    'list', 'tuple', 'set', 'frozenset', 'sorted', 'enumerate', 'zip', 'iter'}:
                return set.union(set(), *(roots(v) for v in expr.args))
            return set()
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases[target.id] = aliases.get(target.id, set()) | roots(node.value)
            if isinstance(node, (ast.For, ast.comprehension)):
                found |= roots(node.iter)
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in {'list', 'tuple', 'sorted', 'set', 'frozenset', 'iter'}:
                for arg in node.args:
                    found |= roots(arg)
            # No eval: only known Python functions in this module or imported Memlet modules.
            target = None
            if isinstance(node.func, ast.Name):
                target = fn.__globals__.get(node.func.id)
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                module = fn.__globals__.get(node.func.value.id)
                if inspect.ismodule(module):
                    target = vars(module).get(node.func.attr)
            if not inspect.isfunction(target) or not target.__module__.startswith('memlet.'):
                continue
            needed = iterated(target, seen)
            try:
                bound = inspect.signature(target).bind_partial(*node.args, **{kw.arg: kw.value for kw in node.keywords if kw.arg})
            except TypeError:
                continue
            for name, value in bound.arguments.items():
                if name in needed and isinstance(value, ast.AST):
                    found |= roots(value)
        cache[fn] = found
        return found
    found = {}
    paths = sorted((HERE / 'memlet').glob('*.py')) + [HERE / n for n in
        ('memory_cli.py', 'memlet_mcp.py', 'memory_intelligence.py', 'memory_skills.py')]
    for path in paths:
        module = importlib.import_module('.'.join(path.relative_to(HERE).with_suffix('').parts))
        for name, fn in vars(module).items():
            if not inspect.isfunction(fn) or fn.__module__ != module.__name__ or not getattr(fn, '_is_fresh_read', False):
                continue
            params = iterated(fn)
            if params:
                found[module.__name__ + '.' + name] = tuple(sorted(params))
    return found


def _input_protocol_inventory_test(self):
    self.assertEqual(_AST_ITERATED_PARAMETERS, _input_inventory())
    recipes, _ = _build_recipes()
    recipes.update({
        ('memlet_dreamtidy', 'proposal_digest'): lambda c: mdt.proposal_digest(['digest']),
        ('memlet_dreamtidy', 'proposal_id'): lambda c: mdt.proposal_id(['digest']),
        ('memlet_dreamtidy', 'lessons_by_id_from_loaded'): lambda c: mdt.lessons_by_id_from_loaded({}, {}, {}, duplicate_ids=()),
        ('memlet_tidy', 'command'): lambda c: mt.command(['tidy'], []),
        ('memlet_tidy', 'kept_lesson_order'): lambda c: mt.kept_lesson_order({}, ()),
        ('memlet_wiki', 'first_managed_problem'): lambda c: mw.first_managed_problem(c['repo'], ['.memlet/config.json']),
    })
    copied = []
    dream_reader = mb.dream_items
    for qualified, parameters in _INPUT_ITERABLES.items():
        if qualified in _INPUT_ALLOWLIST:
            self.assertGreater(len(_INPUT_ALLOWLIST[qualified]), 12)
            continue
        module_name, function_name = qualified.rsplit('.', 1)
        module = importlib.import_module(module_name)
        original = getattr(module, function_name)
        recipe = recipes[(module_name.rsplit('.', 1)[-1], function_name)]
        for parameter in parameters:
            with self.subTest(function=qualified, parameter=parameter):
                c = self.clone('input-' + qualified.replace('.', '-') + '-' + parameter)
                probe = self.clone('input-probe-' + qualified.replace('.', '-') + '-' + parameter)
                observed = []
                intercepted = []
                def invoke(*args, **kwargs):
                    bound = inspect.signature(original).bind(*args, **kwargs)
                    bound.apply_defaults()
                    value = bound.arguments[parameter]
                    values = () if value is None else (value,) if isinstance(value, str) else tuple(value)
                    def generate():
                        observed.append([v.get() for v in md._read_context_vars()])
                        copied_context = contextvars.copy_context()
                        copied.append(copied_context)
                        # Re-entry may legitimately call the intercepted helper again.
                        # Restore only the interception shim during the caller's reads.
                        with mock.patch.object(module, function_name, original):
                            before = dream_reader(probe['repo'])
                            self.child(probe, "assert t.mb.learn_reject(c['repo'],[c['id_a']],approver='Child')['ok']")
                            after = dream_reader(probe['repo'])
                            fresh = self.child(probe, "print(json.dumps(t.mb.dream_items(c['repo'])))")
                            self.assertNotEqual(before, fresh)
                            self.assertEqual(fresh, after)
                            self.assertEqual(fresh, copied_context.run(dream_reader, probe['repo']))
                        yield from values
                    bound.arguments[parameter] = generate()
                    intercepted.append(True)
                    # Nested own entry must also suspend input conversion.
                    scope = md._enter_read_scope() if getattr(original, '_is_fresh_read', False) else contextlib.nullcontext()
                    with scope:
                        return original(*bound.args, **bound.kwargs)
                with mock.patch.object(module, function_name, invoke):
                    # A record/dict parameter may reject a list; entry must still consume its generator.
                    try:
                        recipe(c)
                    except (TypeError, AttributeError, ValueError, KeyError):
                        if parameter not in _AST_ITERATED_PARAMETERS.get(qualified, ()):
                            raise
                self.assertTrue(intercepted, qualified)
                if getattr(original, '_is_fresh_read', False):
                    self.assertTrue(observed, (qualified, parameter))
                self.assertTrue(all(row == [None] * len(md._read_context_vars()) for row in observed))
    probe = self.clone('input-copied-reader')
    self.child(probe, "assert t.mb.learn_reject(c['repo'],[c['id_a']],approver='Child')['ok']")
    fresh = self.child(probe, "print(json.dumps(t.mb.dream_items(c['repo'])))")
    self.assertTrue(copied)
    for context in copied:
        self.assertEqual(fresh, context.run(mb.dream_items, probe['repo']))


def _pathlike_and_generator_copy_test(self):
    c = self.clone('input-path-copy')
    copies, states = [], []
    class Root:
        def __fspath__(self):
            states.append([v.get() for v in md._read_context_vars()])
            copies.append(contextvars.copy_context())
            return str(c['repo'])
    def tiers():
        states.append([v.get() for v in md._read_context_vars()])
        copies.append(contextvars.copy_context())
        yield 'approved'
    before = mb.dream_items(Root(), tiers=tiers())
    self.assertEqual([[None] * len(md._read_context_vars())] * 2, states)
    self.child(c, "assert t.mb.learn_reject(c['repo'],[c['id_a']],approver='Child')['ok']")
    fresh = self.child(c, "print(json.dumps(t.mb.dream_items(c['repo'])))")
    self.assertNotEqual(before, fresh)
    for context in copies:
        self.assertEqual(fresh, context.run(mb.dream_items, c['repo']))
    # An annotated collection under a new name is also materialised at entry.
    @md.fresh_read
    def new_reader(new_collection: Iterable[str]):
        self.assertIsNotNone(md._active_read_state())
        return list(new_collection)
    def new_values():
        self.assertTrue(all(v.get() is None for v in md._read_context_vars()))
        yield 'value'
    self.assertEqual(['value'], new_reader(new_values()))
    @md.fresh_read
    def typed_reader(new_collection: Iterable[str]):
        return list(new_collection)
    class Values:
        def __iter__(self):
            self_test.assertTrue(all(v.get() is None for v in md._read_context_vars()))
            yield 'value'
    self_test = self
    self.assertEqual(['value'], typed_reader(Values()))
    class Stream:
        def __iter__(self):
            raise AssertionError('callback stream must not be consumed as data')
        def __next__(self):
            raise AssertionError('callback stream must not be consumed as data')
        def read(self):
            self_test.assertTrue(all(v.get() is None for v in md._read_context_vars()))
            return 'stream data'
    stream = Stream()
    @md.fresh_read
    def stream_reader(stdin):
        self.assertIs(stream, stdin)
        return md.call_foreign(lambda: stdin.read())
    self.assertEqual('stream data', stream_reader(stream))


TestV94ReadState.test_input_iterable_inventory = _input_protocol_inventory_test
TestV94ReadState.test_pathlike_and_generator_copies_read_fresh = _pathlike_and_generator_copy_test
TestForeignBoundaries.test_input_iterable_inventory = None
TestForeignBoundaries.test_pathlike_and_generator_copies_read_fresh = None


def _branch_reader_freshness_test(self):
    import memory_skills
    c = self.clone('branch-reader')
    head = c['repo'] / '.git' / 'HEAD'
    head.write_text('ref: refs/heads/before\n')
    self.assertEqual('before', memory_skills.detect_git_branch(c['repo']))
    self.child(c, "(c['repo']/'.git/HEAD').write_text('ref: refs/heads/after\\n')")
    fresh = self.child(c, "import memory_skills; print(json.dumps(memory_skills.detect_git_branch(c['repo'])))")
    self.assertEqual('after', fresh)
    self.assertEqual(fresh, memory_skills.detect_git_branch(c['repo']))


TestV94ReadState.test_branch_reader_does_not_keep_repository_cache = _branch_reader_freshness_test
TestForeignBoundaries.test_branch_reader_does_not_keep_repository_cache = None


def _returned_parser_nodes_are_suspended_test(self):
    from memlet.ts_exact import TreeSitterProvider
    c = self.clone('returned-parser-nodes')
    observed, copied = [], []
    def observe(label):
        observed.append(label)
        self.assertTrue(all(v.get() is None for v in md._read_context_vars()), label)
        copied.append(contextvars.copy_context())
    class Node:
        @property
        def child_count(self):
            observe('node child count')
            return 0
        @property
        def type(self):
            observe('node type')
            return 'program'
        @property
        def has_error(self):
            observe('node error flag')
            return False
        @property
        def named_children(self):
            observe('node children')
            return []
    class Tree:
        @property
        def root_node(self):
            observe('tree root')
            return Node()
    class Parser:
        def __init__(self, language):
            observe('parser constructor')
        def parse(self, source):
            observe('parser parse')
            return Tree()
    def language():
        observe('language factory')
        return 'language'
    def wrap_language(value):
        observe('language wrapper')
        return value
    ts = types.SimpleNamespace(Parser=Parser, Language=wrap_language)
    tst = types.SimpleNamespace(language_typescript=language, language_tsx=language)
    tsj = types.SimpleNamespace(language=language)
    with md._enter_read_scope() as state, md.work_counters({}):
        mb.dream_items(c['repo'])
        provider = TreeSitterProvider(ts, tst, tsj, {
            'tree-sitter': 'test', 'tree-sitter-typescript': 'test', 'tree-sitter-javascript': 'test'})
        self.assertEqual({}, provider.fingerprint('probe.ts', ''))
        self.assertIs(state, md._active_read_state())
    self.assertEqual({'language factory', 'language wrapper', 'parser constructor', 'parser parse',
                      'tree root', 'node error flag', 'node children', 'node child count', 'node type'}, set(observed))
    self.child(c, "assert t.mb.learn_reject(c['repo'],[c['id_a']],approver='Child')['ok']")
    fresh = self.child(c, "print(json.dumps(t.mb.dream_items(c['repo'])))")
    for context in copied:
        self.assertEqual(fresh, context.run(mb.dream_items, c['repo']))


TestForeignBoundaries.test_returned_parser_and_nodes_are_suspended = _returned_parser_nodes_are_suspended_test


_LEGACY_MCP_READ_RECIPES = {
    'memlet_store_recall': {'query': 'refund'},
    'memlet_store_brief': {},
    'memlet_store_read': {'id': 'experience:refund'},
    'memlet_store_skills': {'query': 'refund'},
    'memlet_store_preflight': {'plan': 'Review refund', 'files': ['billing.py']},
    'memlet_store_capsule': {'plan': 'Review refund', 'files': ['billing.py']},
    'memlet_store_inspector': {},
    'memlet_store_scenes': {},
    'memlet_store_doctor': {},
    'memlet_store_audit': {},
    'memlet_store_export_rules': {'targets': ['agents'], 'writeFiles': False, 'lang': 'en'},
    'memlet_store_consolidate': {'dryRun': True},
    'memlet_store_heal_skills': {'dryRun': True},
}
_LEGACY_MCP_WRITE_ALLOWLIST = {
    'memlet_store_capture': 'Captures observations into the memory ledger; no read-only mode.',
    'memlet_store_learn': 'Applies learned entries to the memory ledger; no read-only mode.',
    'memlet_store_skill_manage': 'Creates or changes skill records; no read-only mode.',
    'memlet_store_feedback': 'Records feedback and may update skills; no read-only mode.',
    'memlet_store_sync_markdown': 'Ingests Markdown changes into the store; no read-only mode.',
    'memlet_store_verify_skill': 'Executes a command or records verification evidence; no read-only mode.',
}
_LEGACY_MCP_TASK = 'readstate-legacy'
_LEGACY_MCP_OWNER = 'readstate-owner'
_LEGACY_MCP_TIME = 1793500000.0


@contextlib.contextmanager
def _legacy_mcp_clock():
    # Keep recall telemetry idempotent and age calculations equal in both processes.
    with mock.patch('time.time', return_value=_LEGACY_MCP_TIME), mock.patch(
            'time.time_ns', return_value=int(_LEGACY_MCP_TIME * 1000000000)):
        yield


def _legacy_mcp_store(repo):
    from memlet.standalone import StandaloneStore
    root = Path(repo).resolve()
    if root != Path(os.environ['MEMLET_HOME']).resolve():
        raise AssertionError('The legacy fixture must use its explicit scratch MEMLET_HOME')
    return StandaloneStore(root)


def _legacy_mcp_fixture(repo):
    import memory_intelligence as memory
    import work_memory as ledger
    store = _legacy_mcp_store(repo)
    (repo / 'memory_settings.json').write_text('{"semanticEnabled": false}\n')
    with _legacy_mcp_clock():
        store.create('readstate', 'Refund procedures', _LEGACY_MCP_OWNER,
                     goal='Review refund safely', task=_LEGACY_MCP_TASK)
        source = f'Use {repo}/billing.py::refund to return the order and review the result.'
        with store.db(True) as db:
            ledger.record(db, _LEGACY_MCP_TASK, 1, 'source-refund', 'report', 'report:refund',
                          'assert', source, {'kind': 'test-source'}, _LEGACY_MCP_TIME)
        result = memory.learn(store, _LEGACY_MCP_TASK, _LEGACY_MCP_OWNER, 1, 'learn-refund', [{
            'id': 'experience:refund', 'kind': 'experience',
            'sourceRefs': [{'itemId': 'report:refund', 'quote': source}],
            'experience': {
                'situation': f'Review a refund using {repo}/billing.py::refund for an order.',
                'steps': [f'Pass the order to {repo}/billing.py::refund for an order.', 'Review the returned order before continuing.'],
                'outcome': 'The refund result matches the requested order.',
                'conditions': f'The {repo}/billing.py module and refund function exist.',
                'counterexamples': 'Do not use this procedure when the billing module is absent.',
            },
        }])
        if not result.get('skills'):
            raise AssertionError(result)
        skill = memory.read_skill(store, _LEGACY_MCP_TASK, 'skill:refund')
        if not any('refund' in row.get('symbols', {}) for row in skill['source']['referencedFiles']):
            raise AssertionError(('Fixture did not attach the actual refund symbol', skill['source']))
    return store



def _legacy_mcp_canon(value, repo):
    # Inspector runs a local benchmark; compare its evidence and counts, not time.
    def walk(item, parent=''):
        if isinstance(item, dict):
            return {key: ('<VOLATILE>' if parent == 'latencyMs' and key in
                          {'avgMs', 'maxMs', 'minMs', 'p50Ms', 'p95Ms'} else walk(val, key))
                    for key, val in item.items()}
        if isinstance(item, list):
            return [walk(val, parent) for val in item]
        return item
    return walk(_canon(value, repo))


def _legacy_mcp_views(repo):
    repo = Path(repo).resolve()
    with _legacy_mcp_clock():
        server = memlet_mcp.MemletMCPServer(root=repo, store=_legacy_mcp_store(repo),
            default_task=_LEGACY_MCP_TASK, default_session=_LEGACY_MCP_OWNER, standalone=False)
        result = {}
        # An explicit recipe table, not discovery-and-dispatch of arbitrary commands.
        # Recall comes first so idempotent telemetry is present before inspector reads it.
        for name, arguments in _LEGACY_MCP_READ_RECIPES.items():
            args = dict(arguments)
            if name == 'memlet_store_audit':
                args['repoPath'] = str(repo)
            value = server.call_tool(name, args)
            if isinstance(value, Iterator) or callable(value):
                raise AssertionError('Lazy legacy reader: ' + name)
            result[name] = _legacy_mcp_canon(value, repo)
        result['memlet_store_skills:detail'] = _legacy_mcp_canon(server.call_tool(
            'memlet_store_skills', {'skillId': 'skill:refund'}), repo)
        return result


class TestLegacyMCPReadState(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='memlet-legacy-readstate-')
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name).resolve()
        self.env = dict(os.environ)
        for variable in ('HOME', 'TMPDIR', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'MEMLET_HOME', 'MEMLET_MODEL_DIR'):
            directory = self.scratch / variable.lower()
            directory.mkdir()
            self.env[variable] = str(directory)
        self.env['GIT_CEILING_DIRECTORIES'] = str(self.scratch.parent)
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def child(self, repo, code):
        process = subprocess.run([sys.executable, '-B', '-c',
            'import importlib,json,sys; from pathlib import Path; '
            'sys.path[:0]=sys.argv[1:3]; t=importlib.import_module(sys.argv[3]); '
            'repo=Path(sys.argv[4]); ' + code,
            str(HERE), str(Path(__file__).resolve().parent), __name__, str(repo)],
            cwd=HERE, env=dict(os.environ), capture_output=True, text=True, timeout=180)
        self.assertEqual(0, process.returncode, process.stderr)
        return json.loads(process.stdout) if process.stdout else None

    def test_legacy_read_tool_inventory(self):
        discovered = {tool['name'] for tool in memlet_mcp.mcp_tools()
                      if tool['name'].startswith('memlet_store_')}
        self.assertEqual(discovered, set(_LEGACY_MCP_READ_RECIPES) | set(_LEGACY_MCP_WRITE_ALLOWLIST))
        self.assertFalse(set(_LEGACY_MCP_READ_RECIPES) & set(_LEGACY_MCP_WRITE_ALLOWLIST))
        self.assertTrue(all(len(reason) >= 12 for reason in _LEGACY_MCP_WRITE_ALLOWLIST.values()))

    def test_legacy_read_tools_reenter_after_source_edits(self):
        from memlet import code_anchors as anchors
        for edit in ('signature', 'same-stat'):
            with self.subTest(edit=edit):
                repo = self.scratch / edit
                _build_fixture_repo(repo, dict(os.environ))
                with mock.patch.dict(os.environ, {'MEMLET_HOME': str(repo)}):
                    _legacy_mcp_fixture(repo)
                    before = _legacy_mcp_views(repo)
                    self.assertFalse(before['memlet_store_skills:detail']['stale'])
                    observed, copied = {}, []
                    outer_test = self
                    class Store:
                        def memory(self, *args, **kwargs):
                            outer_test.assertTrue(all(v.get() is None for v in md._read_context_vars()))
                            copied.append(contextvars.copy_context())
                            change = ("data.replace('order_id', 'order_no')" if edit == 'same-stat'
                                      else "data.replace('def refund(order_id):', 'def refund(order_id, audit):')")
                            stats = outer_test.child(repo,
                                "import os; p=repo/'billing.py'; st=p.stat(); data=p.read_text(); "
                                "p.write_text(" + change + "); "
                                + ("os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns)); " if edit == 'same-stat' else '')
                                + "print(json.dumps([st.st_size,p.stat().st_size,st.st_mtime_ns,p.stat().st_mtime_ns]))")
                            if edit == 'same-stat':
                                outer_test.assertEqual(stats[0], stats[1])
                                outer_test.assertEqual(stats[2], stats[3])
                            observed.update(_legacy_mcp_views(repo))
                            return {'retrieval': {'items': []}}
                    with md._enter_read_scope(), md.work_counters({}):
                        mb.dream_items(repo)
                        anchors.cached_file_sha(repo / 'billing.py')
                        anchors.extract_file_symbol_signatures(repo / 'billing.py')
                        anchors.extract_file_symbol_hashes(repo / 'billing.py')
                        mb.unified_recall(repo, 'refund', store=Store(), task='probe')
                    fresh = self.child(repo, 'print(json.dumps(t._legacy_mcp_views(repo),sort_keys=True))')
                    self.assertEqual(fresh, observed)
                    self.assertTrue(observed['memlet_store_skills:detail']['stale'])
                    self.assertNotEqual(before['memlet_store_skills:detail'], observed['memlet_store_skills:detail'])
                    self.assertEqual(fresh, copied[0].run(_legacy_mcp_views, repo))
                    with concurrent.futures.ThreadPoolExecutor(1) as pool:
                        self.assertEqual(fresh, pool.submit(
                            copied[0].copy().run, _legacy_mcp_views, repo).result(timeout=180))
