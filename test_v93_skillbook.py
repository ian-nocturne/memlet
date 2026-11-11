"""Unit N3 — Memlet 9.3 skillbook tests (`memlet/memlet_skillbook.py`, `memlet/cli_skillbook.py`)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory_cli
import memlet_mcp
from memlet import (
    VERSION,
    cli_plugins,
    cli_skillbook,
    memlet_anchors as ma,
    memlet_bridge as mb,
    memlet_dream as md,
    memlet_dreamcycle as dc,
    memlet_graph as mg,
    memlet_skillbook as sb,
    memlet_wiki as mw,
    vendor_cli,
    vocabulary as v,
)

REPO_ROOT = Path(__file__).resolve().parent
SPEC_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def validate_skill_spec(
    test: unittest.TestCase,
    text: str,
    *,
    expected_name: str = "memlet-lessons",
    skill_file_path: Path | None = None,
    max_lines: int = 500,
    budget: int = 5000,
) -> dict:
    """Validate the Agent Skills specification rules on a generated SKILL.md string (and on-disk path if given)."""
    test.assertIsInstance(text, str)
    test.assertTrue(text.startswith("---\n"), "SKILL.md must start with YAML frontmatter delimiter ---")
    end_idx = text.find("\n---\n", 4)
    test.assertNotEqual(-1, end_idx, "SKILL.md must close YAML frontmatter with ---")

    if skill_file_path is not None:
        test.assertEqual("SKILL.md", skill_file_path.name)
        test.assertEqual(expected_name, skill_file_path.parent.name, "directory name must equal skill name")

    fm_text = text[4:end_idx]
    body = text[end_idx + 5 :]

    fm_lines = fm_text.split("\n")
    parsed_name = None
    parsed_desc = None
    metadata = {}
    in_meta = False
    for line in fm_lines:
        test.assertTrue(line.strip(), "no blank lines inside frontmatter")
        if in_meta and line.startswith("  ") and not line.startswith("   "):
            k, sep, raw_v = line[2:].partition(":")
            test.assertTrue(sep, f"invalid metadata entry: {line!r}")
            val = sb.yaml_unquote(raw_v.strip())
            test.assertIsInstance(val, str, f"metadata value for {k!r} must be a valid string")
            metadata[k.strip()] = val
            continue
        in_meta = False
        if line.startswith("name:"):
            parsed_name = sb.yaml_unquote(line[len("name:") :].strip())
        elif line.startswith("description:"):
            raw_desc = line[len("description:") :].strip()
            test.assertLessEqual(len(raw_desc), 1024, "raw description scalar exceeds 1024 chars")
            parsed_desc = sb.yaml_unquote(raw_desc)
        elif line == "metadata:":
            in_meta = True
        else:
            test.fail(f"unexpected frontmatter line: {line!r}")

    test.assertIsInstance(parsed_name, str)
    test.assertGreaterEqual(len(parsed_name), 1)
    test.assertLessEqual(len(parsed_name), 64)
    test.assertRegex(parsed_name, SPEC_NAME_RE)
    test.assertNotIn("--", parsed_name)
    test.assertFalse(parsed_name.startswith("-"))
    test.assertFalse(parsed_name.endswith("-"))
    test.assertEqual(expected_name, parsed_name)

    test.assertIsInstance(parsed_desc, str)
    test.assertGreaterEqual(len(parsed_desc), 1)
    test.assertLessEqual(len(parsed_desc), 1024)

    test.assertEqual(
        {"memlet-version", "memlet-digest", "memlet-repo-head"},
        set(metadata.keys()),
    )
    for k, val in metadata.items():
        test.assertIsInstance(k, str)
        test.assertIsInstance(val, str)

    expected_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    test.assertEqual(expected_digest, metadata["memlet-digest"])
    test.assertEqual(VERSION, metadata["memlet-version"])

    body_lines = body.splitlines()
    test.assertLessEqual(len(body_lines), max_lines, "body line count exceeds max_lines")
    test.assertLessEqual(mw.estimate_tokens(body), budget, "body token estimate exceeds budget")

    # Check body safety: no wall-clock ISO timestamps, no HTML comments, no code fences, no raw frontmatter delimiters
    test.assertNotRegex(text, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
    test.assertNotIn("<!--", body)
    test.assertNotIn("-->", body)
    test.assertNotIn("```", body)
    test.assertNotIn("~~~", body)
    for bline in body_lines:
        test.assertNotEqual("---", bline.strip(), "body line must never be a frontmatter delimiter")
        for ch in bline:
            test.assertFalse(mb._hidden_character(ch), f"hidden/bidi character {ord(ch):#x} in body")
            test.assertGreaterEqual(ord(ch), 0x20, f"control character {ord(ch):#x} in body")
            test.assertFalse(0x7F <= ord(ch) <= 0x9F, f"C1 control character {ord(ch):#x} in body")

    return {
        "name": parsed_name,
        "description": parsed_desc,
        "metadata": metadata,
        "body": body,
        "body_lines": body_lines,
    }


class SkillbookTests(unittest.TestCase):
    _python = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="memlet-n3-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "repo"
        self.out_dir = self.base / "skills-out"
        self.root.mkdir()
        self.out_dir.mkdir()
        for sub in ("home", "tmp", "locks", "unused"):
            (self.base / sub).mkdir()
        env = dict(
            os.environ,
            HOME=str(self.base / "home"),
            TMPDIR=str(self.base / "tmp"),
            CLAUDE_CONFIG_DIR=str(self.base / "home/claude"),
            CODEX_HOME=str(self.base / "home/codex"),
            GIT_CEILING_DIRECTORIES=str(self.base),
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="Test",
            GIT_AUTHOR_EMAIL="test@example.com",
            GIT_COMMITTER_NAME="Test",
            GIT_COMMITTER_EMAIL="test@example.com",
            MEMLET_LOCK_DIR=str(self.base / "locks"),
            MEMLET_DREAMING="0",
            MEMLET_HOME=str(self.base / "unused"),
            MEMLET_TRANSCRIPT_DIRS=str(self.root),
        )
        for key in (
            "PYTHONHOME",
            "PYTHONEXECUTABLE",
            "__PYVENV_LAUNCHER__",
            "CODEX_THREAD_ID",
            "MEMLET_PARENT_SESSION_ID",
        ):
            env.pop(key, None)
        p_env = mock.patch.dict(os.environ, env, clear=True)
        p_env.start()
        self.addCleanup(p_env.stop)
        p_tmp = mock.patch.object(tempfile, "tempdir", str(self.base / "tmp"))
        p_tmp.start()
        self.addCleanup(p_tmp.stop)
        orig_run_vendor = vendor_cli.run_vendor

        def _guarded_run_vendor(*args, **kwargs):
            if kwargs.get("runner") is None:
                raise AssertionError("real model call forbidden in N3")
            return orig_run_vendor(*args, **kwargs)

        p_vendor = mock.patch.object(vendor_cli, "run_vendor", side_effect=_guarded_run_vendor)
        p_vendor.start()
        self.addCleanup(p_vendor.stop)
        sb._KNOWN_SKILL_REPOS.clear()

        subprocess.run(["git", "init", "-q", str(self.root)], check=True, env=dict(os.environ))
        if SkillbookTests._python is None:
            for cand in dict.fromkeys([sys.executable, shutil.which("python3"), "/usr/bin/python3"]):
                if cand and Path(cand).is_file():
                    probe = subprocess.run([cand, "-c", "pass"], env=dict(os.environ), capture_output=True)
                    if probe.returncode == 0:
                        SkillbookTests._python = cand
                        break
        self.python = SkillbookTests._python
        self.assertIsNotNone(self.python)

    def write(self, rel: str, content: str) -> Path:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def commit(self, msg: str = "fixture") -> str:
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", msg, "--allow-empty"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        return mw._git_head(self.root)

    def invoke_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["memlet"] + argv), contextlib.redirect_stdout(
            out
        ), contextlib.redirect_stderr(err):
            try:
                code = memory_cli.cli()
            except SystemExit as exc:
                code = int(exc.code) if exc.code is not None else 0
        return code, out.getvalue(), err.getvalue()

    def build_full_fixture(self) -> dict:
        """Build a fixture with:
        - every relation (BLOCKS, SUPERSEDES, RESOLVES, GOVERNS),
        - a replaced instruction (old line marked, current line next to it),
        - a multi-line lesson,
        - a hint and a pending lesson (both absent from the file),
        - a lesson attached to two files,
        - an unattached lesson.
        """
        self.write(
            "src/billing.py",
            "def refund(order_id):\n    return order_id\n\ndef charge(amount):\n    return amount\n",
        )
        self.write("src/retry.py", "def with_backoff(fn):\n    return fn()\n")
        self.write(
            "AGENTS.md",
            "# Repository Rules\n- Refunds go through `src/billing.py::refund`.\n- Retries use `src/retry.py::with_backoff`.\n",
        )
        head = self.commit("initial code and rules")
        self.assertTrue(mw.init_project(self.root)["ok"])

        cfg_path = self.root / mw.CONFIG_PATH
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["dream"] = {
            "hints": True,
            "agreement": "model",
            "models": {"claude": "haiku", "verifier": {"claude": "sonnet"}},
        }
        mw.write_managed_text(self.root, mw.CONFIG_PATH, json.dumps(cfg, indent=2, sort_keys=True) + "\n")

        lines = [
            "2026-01-01T00:00:01Z [user] Never call refund() in src/billing.py without the idempotency token.",
            "2026-01-01T00:00:02Z [user] Retry failed charges in src/billing.py with a fixed 10ms sleep.",
            "2026-01-01T00:00:03Z [user] Retry failed charges in src/billing.py with exponential backoff instead of fixed sleep.",
            "2026-01-01T00:00:04Z [user] Flush the billing audit buffer before closing src/billing.py workers.",
            "2026-01-01T00:00:05Z [user] Wait for inflight callbacks to finish before tearing down the client.",
            "2026-01-01T00:00:06Z [user] Keep timeout constants aligned between billing and retry modules.",
            "2026-01-01T00:00:07Z [user] Always run the offline unit test suite before committing changes.",
            "2026-01-01T00:00:08Z [user] Never block the event loop inside src/retry.py.",
            "2026-01-01T00:00:09Z [user] Unapproved pending note about src/billing.py logging format.",
        ]
        source = self.write("session1.log", "\n".join(lines) + "\n")
        self.assertTrue(dc.dream_ingest(self.root, source)["ok"])
        batch = dc.dream_next(self.root)
        fid = batch["rows"][0][0]

        candidates = [
            {
                "category": "repeated_pitfall",
                "title": "Never call refund without token",
                "scope_tag": "src/billing.py",
                "src_spans": [f"{fid}:1"],
                "edges": [{"edge_type": "BLOCKS", "target": "missing_idempotency", "src_span": f"{fid}:1"}],
            },
            {
                "category": "superseded_directive",
                "title": "Exponential backoff replaces fixed sleep",
                "scope_tag": "src/billing.py",
                "src_spans": [f"{fid}:2", f"{fid}:3"],
                "superseded_by_src": f"{fid}:3",
                "edges": [
                    {
                        "edge_type": "SUPERSEDES",
                        "target": "fixed_sleep_retry",
                        "src_span": f"{fid}:3",
                        "target_span": f"{fid}:2",
                    }
                ],
            },
            {
                "category": "verified_workaround",
                "title": "Flush buffer and wait for callbacks",
                "scope_tag": "src/billing.py",
                "src_spans": [f"{fid}:4", f"{fid}:5"],
                "edges": [
                    {
                        "edge_type": "RESOLVES",
                        "target": "worker_shutdown",
                        "src_span": f"{fid}:5",
                        "target_span": f"{fid}:4",
                    }
                ],
            },
            {
                "category": "verified_workaround",
                "title": "Keep timeout constants aligned",
                "scope_tag": "src",
                "src_spans": [f"{fid}:6"],
                "edges": [{"edge_type": "GOVERNS", "target": "timeout_alignment", "src_span": f"{fid}:6"}],
            },
            {
                "category": "verified_workaround",
                "title": "Run offline unit tests",
                "scope_tag": "general",
                "src_spans": [f"{fid}:7"],
                "edges": [{"edge_type": "GOVERNS", "target": "offline_tests", "src_span": f"{fid}:7"}],
            },
            {
                "category": "repeated_pitfall",
                "title": "Avoid blocking calls in event loops",
                "scope_tag": "src/retry.py",
                "src_spans": [f"{fid}:8"],
                "edges": [{"edge_type": "BLOCKS", "target": "blocking_loop", "src_span": f"{fid}:8"}],
            },
            {
                "category": "verified_workaround",
                "title": "Pending note about logging",
                "scope_tag": "src/billing.py",
                "src_spans": [f"{fid}:9"],
                "edges": [{"edge_type": "GOVERNS", "target": "log_format", "src_span": f"{fid}:9"}],
            },
        ]

        def runner(vendor, prompt, **kwargs):
            if prompt == dc.VERIFY_PROMPT:
                return json.dumps({"verdict": "SUPPORTED"})
            return json.dumps(candidates)

        run_res = dc.dream_run(self.root, runner=runner, verify_with="claude")
        self.assertTrue(run_res["ok"], run_res)

        queue_nodes = mb._load_queue(self.root)[0]["nodes"]
        by_title = {n["title"]: n["id"] for n in queue_nodes}
        id_blocks = by_title["Never call refund without token"]
        id_sup = by_title["Exponential backoff replaces fixed sleep"]
        id_res = by_title["Flush buffer and wait for callbacks"]
        id_two = by_title["Keep timeout constants aligned"]
        id_unatt = by_title["Run offline unit tests"]
        id_hint = by_title["Avoid blocking calls in event loops"]
        id_pend = by_title["Pending note about logging"]

        # Approve the first 5 lessons; attach id_two to two files; leave id_unatt unattached;
        # leave id_hint as a hint (BLOCKS + two-model agreement) and id_pend as pending (GOVERNS cannot be a hint).
        self.assertTrue(
            mb.learn_approve(self.root, [id_blocks, id_sup, id_res, id_unatt], approver="alice")["ok"]
        )
        self.assertTrue(
            mb.learn_approve(
                self.root, [id_two], approver="alice", paths=["src/billing.py", "src/retry.py"]
            )["ok"]
        )

        return {
            "head": head,
            "fid": fid,
            "ids": {
                "blocks": id_blocks,
                "sup": id_sup,
                "res": id_res,
                "two": id_two,
                "unatt": id_unatt,
                "hint": id_hint,
                "pend": id_pend,
            },
        }

    def test_fixture_relations_grouping_ordering_and_exclusions(self):
        info = self.build_full_fixture()
        ids = info["ids"]

        all_read = mb.dream_items(self.root, tiers=(v.APPROVED, v.HINT, v.PENDING))
        self.assertTrue(all_read["ok"])
        self.assertEqual({v.APPROVED: 5, v.HINT: 1, v.PENDING: 1}, all_read["counts"])

        res = sb.build_skill(self.root)
        self.assertTrue(res["ok"], res)
        self.assertEqual(
            {"ok", "name", "text", "digest", "items", "omitted", "files", "notices"},
            set(res.keys()),
        )
        self.assertEqual("memlet-lessons", res["name"])
        self.assertEqual(5, res["items"])
        self.assertEqual(0, res["omitted"])
        # src/billing.py has 4 lessons; src/retry.py has 1 lesson -> src/billing.py is first
        self.assertEqual(["src/billing.py", "src/retry.py"], res["files"])

        parsed = validate_skill_spec(self, res["text"], expected_name="memlet-lessons")
        self.assertEqual(info["head"], parsed["metadata"]["memlet-repo-head"])
        self.assertIn("src/billing.py, src/retry.py", parsed["description"])

        body = parsed["body"]
        # First body lines state it is a snapshot, name the commit, and recommend memlet recall --files FILE
        first_lines = "\n".join(parsed["body_lines"][:4])
        self.assertIn("snapshot", first_lines.lower())
        self.assertIn(info["head"], first_lines)
        self.assertIn("memlet recall --files FILE", first_lines)

        # Group order: src/billing.py, then src/retry.py, then any file
        idx_billing = body.index("## `src/billing.py`")
        idx_retry = body.index("## `src/retry.py`")
        idx_any = body.index("## any file")
        self.assertLess(idx_billing, idx_retry)
        self.assertLess(idx_retry, idx_any)

        # Within src/billing.py group: prohibitions (BLOCKS) -> replaced (SUPERSEDES) -> fixes (RESOLVES) -> rest (GOVERNS)
        billing_section = body[idx_billing:idx_retry]
        pos_blocks = billing_section.index(ids["blocks"])
        pos_super = billing_section.index(ids["sup"])
        pos_resolv = billing_section.index(ids["res"])
        pos_twofile = billing_section.index(ids["two"])
        self.assertLess(pos_blocks, pos_super)
        self.assertLess(pos_super, pos_resolv)
        self.assertLess(pos_resolv, pos_twofile)

        # Replaced instruction has old line marked as not current, right next to the current line
        self.assertIn(
            "  > [replaced — do not apply as current] Retry failed charges in src/billing.py with a fixed 10ms sleep.\n"
            "  > [current instruction] Retry failed charges in src/billing.py with exponential backoff instead of fixed sleep.",
            billing_section,
        )

        # Multi-line lesson has both cited lines block-quoted
        self.assertIn(
            "  > Flush the billing audit buffer before closing src/billing.py workers.\n"
            "  > Wait for inflight callbacks to finish before tearing down the client.",
            billing_section,
        )

        # Lesson attached to two files appears in both src/billing.py and src/retry.py sections with both files listed
        retry_section = body[idx_retry:idx_any]
        self.assertIn(ids["two"], retry_section)
        self.assertIn("files: src/billing.py, src/retry.py", billing_section)
        self.assertIn("files: src/billing.py, src/retry.py", retry_section)

        # Unattached lesson appears under ## any file
        any_section = body[idx_any:]
        self.assertIn(ids["unatt"], any_section)
        self.assertIn("files: any file", any_section)

        # Hint, pending lesson, and model verdicts are absent from the file
        self.assertNotIn(ids["hint"], res["text"])
        self.assertNotIn("Never block the event loop inside src/retry.py", res["text"])
        self.assertNotIn(ids["pend"], res["text"])
        self.assertNotIn("Unapproved pending note", res["text"])
        self.assertNotIn("SUPPORTED", res["text"])
        self.assertNotIn("ALREADY_SUPERSEDED", res["text"])

    def test_merge_state_adapter_skips_merged_lessons_when_present_and_works_when_absent(self):
        info = self.build_full_fixture()
        ids = info["ids"]
        if not hasattr(mb, "merge_state"):
            state = sb._merge_state({"id": ids["two"]}, {})
            self.assertEqual({"state": "none", "into": None, "reason": "not available"}, state)

        def fake_merge_state(node, nodes_by_id):
            if node.get("id") == ids["two"]:
                return {"state": "merged", "into": ids["blocks"], "reason": None}
            return {"state": "none", "into": None, "reason": None}

        with mock.patch.object(mb, "merge_state", side_effect=fake_merge_state, create=True):
            res = sb.build_skill(self.root)
        self.assertTrue(res["ok"])
        self.assertEqual(4, res["items"])
        self.assertNotIn(ids["two"], res["text"])
        validate_skill_spec(self, res["text"])

    def test_determinism_build_twice_and_after_rerunning_learn_transcript(self):
        self.write("src/app.py", "def run():\n    return True\n")
        self.commit("add app.py")
        self.assertTrue(mw.init_project(self.root)["ok"])

        transcript = self.root / "session.log"
        transcript.write_text(
            "2026-02-01T10:00:00Z [user] Never call run() in src/app.py without initializing config first.\n"
            "2026-02-01T10:01:00Z [user] Workaround: fixed src/app.py crash by resetting the cache before run().\n",
            encoding="utf-8",
        )
        learn1 = mb.learn_from_transcript(self.root, transcript)
        self.assertTrue(learn1["ok"], learn1)
        pending = mb.learn_pending(self.root, _record_review=False)
        ids = [it["id"] for it in pending["pendingNodes"]]
        self.assertTrue(ids)
        app = mb.learn_approve(self.root, [ids[0]], approver="ray", paths=["src/app.py"])
        self.assertTrue(app["ok"], app)

        b1 = sb.build_skill(self.root)
        b2 = sb.build_skill(self.root)
        self.assertEqual(b1, b2)
        self.assertEqual(b1["text"].encode("utf-8"), b2["text"].encode("utf-8"))
        validate_skill_spec(self, b1["text"])

        learn2 = mb.learn_from_transcript(self.root, transcript)
        self.assertTrue(learn2["ok"], learn2)
        b3 = sb.build_skill(self.root)
        self.assertEqual(b1["text"].encode("utf-8"), b3["text"].encode("utf-8"))
        self.assertEqual(b1["digest"], b3["digest"])
        validate_skill_spec(self, b3["text"])

    def test_hostile_quotes_and_hostile_filenames_are_neutralised(self):
        self.write("src/normal.py", "x = 1\n")
        hostile_rel = "src/odd:file#1---name.py"
        self.write(hostile_rel, "y = 2\n")
        self.commit("add files")
        self.assertTrue(mw.init_project(self.root)["ok"])

        long_line = "Never call dangerous_op() in src/normal.py " + ("A" * 20000)
        snap_lines = [
            "2026-01-01T00:00:01Z [user] ---",
            "2026-01-01T00:00:02Z [user] ```python import os ``` and ~~~bash",
            "2026-01-01T00:00:03Z [user] # Top Heading and > # Nested Heading",
            "2026-01-01T00:00:04Z [user] <!-- inject comment --> and control \x07\x1b[31mred\x1b[0m and bidi \u202e\u2066override",
            "2026-01-01T00:00:05Z [user] " + long_line,
            "2026-01-01T00:00:06Z [user] ...",
        ]
        snap_name = "hostile.log-2222222222.log"
        self.write(f"{mb.SNAPSHOT_REL}/{snap_name}", "\n".join(snap_lines) + "\n")
        snap_map = {snap_name: snap_lines}

        nodes, edges = [], []
        for idx in range(1, len(snap_lines) + 1):
            pin = md.resolve_pin(snap_map, f"{snap_name}:{idx}")
            self.assertIsNotNone(pin, f"pin {idx} should resolve")
            eid = f"E-h{idx}"
            nid = f"DRM-h{idx:02d}"
            edge = {
                "edgeId": eid,
                "edgeType": "BLOCKS",
                "fromId": nid,
                "toId": f"BLOCKS:h{idx}",
                "targetLabel": "hostile",
                "srcPin": pin,
                "edgeVerdict": "VALID",
            }
            edge["approval"] = {
                "schema": md.APPROVAL_RECORD_SCHEMA,
                "decision": "approved",
                "by": "sec",
                "at": "2026-01-02T00:00:00Z",
                "digest": md.edge_approval_digest(edge),
            }
            paths_list = ["src/normal.py", hostile_rel]
            node = {
                "id": nid,
                "title": "hostile test",
                "category": "repeated_pitfall",
                "statusKind": "open_trap",
                "scopeTag": "src/normal.py",
                "primaryPin": pin,
                "allPins": [pin],
                "edges": [edge],
                "verdict": "SUPPORTED",
                "approvedPaths": paths_list,
            }
            node["approval"] = {
                "schema": md.APPROVAL_RECORD_SCHEMA,
                "decision": "approved",
                "by": "sec",
                "at": "2026-01-02T00:00:00Z",
                "paths": list(paths_list),
                "digest": md.node_approval_digest(node),
            }
            nodes.append(node)
            edges.append(edge)

        self.write(
            mb.QUEUE_REL,
            json.dumps({"schema": md.DREAM_ONTOLOGY_SCHEMA, "nodes": nodes, "edges": edges}, indent=2) + "\n",
        )

        # Also verify that learn_paths refuses hostile paths
        bad_attach = mb.learn_paths(self.root, ["DRM-h01"], approver="sec", paths=["../outside.py"])
        self.assertFalse(bad_attach["ok"])

        res = sb.build_skill(self.root, budget=5000)
        self.assertTrue(res["ok"], res)
        parsed = validate_skill_spec(self, res["text"], budget=5000)

        self.assertIn("src/normal.py", res["files"])
        self.assertIn(hostile_rel, res["files"])

        quote_lines = [line for line in parsed["body_lines"] if line.startswith("  > ")]
        self.assertTrue(quote_lines)
        for qline in quote_lines:
            after = qline[4:]
            self.assertFalse(after.lstrip().startswith("#"), f"quote started heading: {qline!r}")
            self.assertFalse(after.lstrip().startswith(">"), f"quote started nested blockquote: {qline!r}")
            self.assertFalse(after.lstrip().startswith("---"), f"quote started delimiter: {qline!r}")
            self.assertFalse(after.lstrip().startswith("..."), f"quote started yaml end: {qline!r}")
            self.assertNotIn("```", after)
            self.assertNotIn("~~~", after)
            self.assertNotIn("<!--", after)
            self.assertNotIn("-->", after)
            self.assertLessEqual(len(after), md.LESSON_TEXT_MAX_CHARS + 64)

        self.assertIn("[contains hidden characters]", parsed["body"])

    def test_caps_whole_items_only_omitted_line_max_lines_and_budget(self):
        self.write("src/mod.py", "def f():\n    return 1\n")
        self.commit("add mod.py")
        self.assertTrue(mw.init_project(self.root)["ok"])

        snap_lines = []
        for i in range(1, 21):
            snap_lines.append(
                f"2026-01-01T00:00:{i:02d}Z [user] Multi-line rule {i} part A for src/mod.py must stay intact."
            )
            snap_lines.append(
                f"2026-01-01T00:00:{i:02d}Z [user] Multi-line rule {i} part B for src/mod.py must never be split."
            )
        snap_name = "caps.log-3333333333.log"
        self.write(f"{mb.SNAPSHOT_REL}/{snap_name}", "\n".join(snap_lines) + "\n")
        snap_map = {snap_name: snap_lines}

        nodes, edges = [], []
        for i in range(1, 21):
            line_a = (i - 1) * 2 + 1
            line_b = line_a + 1
            pin = md.resolve_pin(snap_map, f"{snap_name}:{line_a}-{line_b}")
            eid = f"E-cap{i:02d}"
            nid = f"DRM-cap{i:02d}"
            edge = {
                "edgeId": eid,
                "edgeType": "BLOCKS" if i <= 5 else "GOVERNS",
                "fromId": nid,
                "toId": f"TARGET:{i}",
                "targetLabel": f"cap_{i}",
                "srcPin": pin,
                "edgeVerdict": "VALID",
            }
            edge["approval"] = {
                "schema": md.APPROVAL_RECORD_SCHEMA,
                "decision": "approved",
                "by": "tester",
                "at": "2026-01-02T00:00:00Z",
                "digest": md.edge_approval_digest(edge),
            }
            node = {
                "id": nid,
                "title": f"Cap rule {i}",
                "category": "repeated_pitfall",
                "statusKind": "open_trap",
                "scopeTag": "src/mod.py",
                "primaryPin": pin,
                "allPins": [pin],
                "edges": [edge],
                "verdict": "SUPPORTED",
            }
            node["approval"] = {
                "schema": md.APPROVAL_RECORD_SCHEMA,
                "decision": "approved",
                "by": "tester",
                "at": "2026-01-02T00:00:00Z",
                "digest": md.node_approval_digest(node),
            }
            nodes.append(node)
            edges.append(edge)

        self.write(
            mb.QUEUE_REL,
            json.dumps({"schema": md.DREAM_ONTOLOGY_SCHEMA, "nodes": nodes, "edges": edges}, indent=2) + "\n",
        )

        # 1. Test max_lines cap
        res_lines = sb.build_skill(self.root, max_lines=22, budget=5000)
        self.assertTrue(res_lines["ok"])
        self.assertGreater(res_lines["items"], 0)
        self.assertGreater(res_lines["omitted"], 0)
        self.assertEqual(20, res_lines["items"] + res_lines["omitted"])
        parsed_lines = validate_skill_spec(self, res_lines["text"], max_lines=22, budget=5000)
        self.assertEqual(
            v.HOOK_OMITTED % res_lines["omitted"],
            parsed_lines["body_lines"][-1],
        )
        for i in range(1, 21):
            has_id = f"DRM-cap{i:02d}" in parsed_lines["body"]
            has_a = f"Multi-line rule {i} part A" in parsed_lines["body"]
            has_b = f"Multi-line rule {i} part B" in parsed_lines["body"]
            self.assertEqual(has_id, has_a)
            self.assertEqual(has_id, has_b)

        # 2. Test token budget cap
        res_budget = sb.build_skill(self.root, max_lines=500, budget=400)
        self.assertTrue(res_budget["ok"])
        self.assertGreater(res_budget["items"], 0)
        self.assertGreater(res_budget["omitted"], 0)
        self.assertEqual(20, res_budget["items"] + res_budget["omitted"])
        parsed_budget = validate_skill_spec(self, res_budget["text"], max_lines=500, budget=400)
        self.assertEqual(
            v.HOOK_OMITTED % res_budget["omitted"],
            parsed_budget["body_lines"][-1],
        )

        # 3. Description length cap when many attached files exist (up to 8 per lesson across multiple lessons)
        all_many_paths = []
        for idx in range(60):
            rel_p = f"src/long_package_name_{idx:02d}/module_file_{idx:02d}.py"
            self.write(rel_p, "x = 1\n")
            all_many_paths.append(rel_p)
        self.commit("many files")
        for idx, node in enumerate(nodes[:8]):
            chunk = all_many_paths[idx * 7 : (idx + 1) * 7]
            node["approvedPaths"] = chunk
            node["approval"]["paths"] = list(chunk)
            node["approval"]["digest"] = md.node_approval_digest(node)
        self.write(
            mb.QUEUE_REL,
            json.dumps({"schema": md.DREAM_ONTOLOGY_SCHEMA, "nodes": nodes, "edges": edges}, indent=2) + "\n",
        )
        res_many = sb.build_skill(self.root)
        self.assertTrue(res_many["ok"])
        validate_skill_spec(self, res_many["text"])

    def test_write_refusals_atomic_write_and_check_reasons(self):
        info = self.build_full_fixture()
        ids = info["ids"]

        for bad_name in ("", "-leading", "trailing-", "double--hyphen", "Upper", "under_score", "../escape", "a/b", "a" * 65):
            with self.subTest(bad_name=bad_name):
                b = sb.build_skill(self.root, name=bad_name)
                self.assertFalse(b["ok"])
                w = sb.write_skill(self.root, out=self.out_dir, name=bad_name)
                self.assertFalse(w["ok"])
                self.assertFalse(w.get("written"))

        w_def = sb.write_skill(self.root)
        self.assertTrue(w_def["ok"], w_def)
        def_path = self.root / ".memlet/skills/memlet-lessons/SKILL.md"
        self.assertEqual(str(def_path), w_def["path"])
        self.assertTrue(def_path.is_file())
        validate_skill_spec(self, def_path.read_text(encoding="utf-8"), skill_file_path=def_path)
        self.assertEqual(
            {"ok": True, "current": True, "reason": "current"},
            sb.check_skill(def_path),
        )

        w_out = sb.write_skill(self.root, out=self.out_dir, name="custom-skill")
        self.assertTrue(w_out["ok"], w_out)
        out_path = self.out_dir / "custom-skill" / "SKILL.md"
        self.assertEqual(str(out_path), w_out["path"])
        validate_skill_spec(
            self,
            out_path.read_text(encoding="utf-8"),
            expected_name="custom-skill",
            skill_file_path=out_path,
        )
        self.assertEqual(
            {"ok": True, "current": True, "reason": "current"},
            sb.check_skill(out_path),
        )

        real_elsewhere = self.base / "elsewhere"
        real_elsewhere.mkdir()
        sym_out = self.base / "sym-out"
        sym_out.symlink_to(real_elsewhere)
        self.assertFalse(sb.write_skill(self.root, out=sym_out)["ok"])

        sym_sub_out = self.base / "sym-sub-out"
        sym_sub_out.mkdir()
        (sym_sub_out / "memlet-lessons").symlink_to(real_elsewhere)
        self.assertFalse(sb.write_skill(self.root, out=sym_sub_out, force=True)["ok"])

        sym_leaf_out = self.base / "sym-leaf-out"
        (sym_leaf_out / "memlet-lessons").mkdir(parents=True)
        elsewhere_file = real_elsewhere / "target.md"
        elsewhere_file.write_text("original\n", encoding="utf-8")
        (sym_leaf_out / "memlet-lessons" / "SKILL.md").symlink_to(elsewhere_file)
        self.assertFalse(sb.write_skill(self.root, out=sym_leaf_out, force=True)["ok"])
        self.assertEqual("original\n", elsewhere_file.read_text(encoding="utf-8"))
        self.assertEqual(
            {"ok": True, "current": False, "reason": "unreadable"},
            sb.check_skill(sym_leaf_out / "memlet-lessons" / "SKILL.md"),
        )

        foreign_dir = self.out_dir / "foreign-skill"
        foreign_dir.mkdir(parents=True)
        foreign_file = foreign_dir / "SKILL.md"
        foreign_file.write_text("---\nname: foreign-skill\ndescription: Hand written\n---\n# Custom\n", encoding="utf-8")
        self.assertEqual(
            {"ok": True, "current": False, "reason": "not written by Memlet"},
            sb.check_skill(foreign_file),
        )
        w_foreign = sb.write_skill(self.root, out=self.out_dir, name="foreign-skill", force=False)
        self.assertFalse(w_foreign["ok"])
        self.assertEqual("not written by Memlet", w_foreign["reason"])
        self.assertIn("# Custom", foreign_file.read_text(encoding="utf-8"))
        w_foreign_force = sb.write_skill(self.root, out=self.out_dir, name="foreign-skill", force=True)
        self.assertTrue(w_foreign_force["ok"])
        self.assertEqual(
            {"ok": True, "current": True, "reason": "current"},
            sb.check_skill(foreign_file),
        )

        original_bytes = out_path.read_bytes()
        out_path.write_text(out_path.read_text(encoding="utf-8") + "\nHand-added note.\n", encoding="utf-8")
        self.assertEqual(
            {"ok": True, "current": False, "reason": "edited"},
            sb.check_skill(out_path),
        )
        w_edited = sb.write_skill(self.root, out=self.out_dir, name="custom-skill", force=False)
        self.assertFalse(w_edited["ok"])
        self.assertEqual("edited", w_edited["reason"])
        out_path.write_bytes(original_bytes)
        self.assertTrue(sb.check_skill(out_path)["current"])

        with mock.patch("os.replace", side_effect=OSError("simulated atomic rename failure")):
            w_fail = sb.write_skill(self.root, out=self.out_dir, name="custom-skill", force=True)
        self.assertFalse(w_fail["ok"])
        self.assertEqual(original_bytes, out_path.read_bytes())
        leftovers = [p.name for p in out_path.parent.iterdir() if p.name != "SKILL.md"]
        self.assertEqual([], leftovers, "temporary file must be cleaned up on atomic write failure")

        self.assertTrue(mb.learn_reject(self.root, [ids["unatt"]], approver="alice", reason="obsolete")["ok"])
        self.assertEqual(
            {"ok": True, "current": False, "reason": "stale: lessons or code changed"},
            sb.check_skill(out_path),
        )
        w_refresh = sb.write_skill(self.root, out=self.out_dir, name="custom-skill", force=False)
        self.assertTrue(w_refresh["ok"], w_refresh)
        self.assertEqual(
            {"ok": True, "current": True, "reason": "current"},
            sb.check_skill(out_path),
        )

        self.assertEqual(
            {"ok": True, "current": False, "reason": "unreadable"},
            sb.check_skill(self.out_dir / "nonexistent" / "SKILL.md"),
        )
        self.assertEqual(
            {"ok": True, "current": False, "reason": "unreadable"},
            sb.check_skill(self.out_dir),
        )

    def test_cli_skillbook_plugin_registration_modes_and_exit_codes(self):
        self.build_full_fixture()
        loaded = cli_plugins.commands(cli_plugins.load())
        self.assertIn("skillbook", loaded)
        self.assertEqual("Rules/wiki", loaded["skillbook"]["group"])
        self.assertFalse(loaded["skillbook"]["product"])

        code, out_json, err = self.invoke_cli(["admin", "skillbook", "--repo", str(self.root)])
        self.assertEqual(0, code, err)
        parsed_json = json.loads(out_json)
        self.assertEqual(sb.build_skill(self.root), parsed_json)
        self.assertFalse((self.root / ".memlet/skills").exists())

        code, out_md, err = self.invoke_cli(
            ["admin", "skillbook", "--repo", str(self.root), "--format", "markdown"]
        )
        self.assertEqual(0, code, err)
        self.assertEqual(parsed_json["text"], out_md)
        validate_skill_spec(self, out_md)

        code, _, _ = self.invoke_cli(["admin", "skillbook", "--repo", str(self.root), "--out", str(self.out_dir)])
        self.assertEqual(2, code)

        code, w_out, err = self.invoke_cli(
            ["admin", "skillbook", "--repo", str(self.root), "--write", "--out", str(self.out_dir)]
        )
        self.assertEqual(0, code, err)
        w_data = json.loads(w_out)
        skill_path = Path(w_data["path"])
        self.assertTrue(skill_path.is_file())

        code, c_md, err = self.invoke_cli(
            ["admin", "skillbook", "--check", str(skill_path), "--format", "markdown"]
        )
        self.assertEqual(0, code, err)
        self.assertEqual("current\n", c_md)

        skill_path.write_text(skill_path.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
        code, c_md, err = self.invoke_cli(
            ["admin", "skillbook", "--check", str(skill_path), "--format", "markdown"]
        )
        self.assertEqual(1, code, err)
        self.assertEqual("edited\n", c_md)

    def test_byte_identical_compatibility_with_v92_1181ec5(self):
        """Archive 1181ec5 (9.2.0) into a scratch dir, build a fixture repository with 9.2.0, and verify that
        every existing library, CLI and MCP reader in this worktree produces byte-identical output."""
        v92_src = self.base / "v92_code"
        v92_src.mkdir()
        archive = subprocess.run(
            ["git", "archive", "1181ec5"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        )
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
            try:
                tar.extractall(path=v92_src, filter="data")
            except TypeError:
                tar.extractall(path=v92_src)

        fixture_repo = self.base / "compat_repo"
        fixture_repo.mkdir()
        subprocess.run(["git", "init", "-q", str(fixture_repo)], check=True, env=dict(os.environ))
        (fixture_repo / "src").mkdir()
        (fixture_repo / "src/billing.py").write_text(
            "def refund(order_id):\n    return order_id\n\ndef charge(amount):\n    return amount\n",
            encoding="utf-8",
        )
        (fixture_repo / "AGENTS.md").write_text(
            "# Rules\n- Refunds go through `src/billing.py::refund`.\n- Always call `charge()` before invoicing.\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "-A"], cwd=fixture_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "compat fixture"],
            cwd=fixture_repo,
            check=True,
            capture_output=True,
        )
        transcript = fixture_repo / "session.log"
        transcript.write_text(
            "2026-01-01T00:00:01Z [user] Never call refund() in src/billing.py without the retry wrapper.\n"
            "2026-01-01T00:00:02Z [user] Workaround: fixed src/billing.py timeout by flushing the socket.\n",
            encoding="utf-8",
        )

        setup_script = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from memlet import memlet_wiki as mw, memlet_bridge as mb, memlet_anchors as ma

repo = Path(sys.argv[2])
assert mw.init_project(repo)["ok"]
sug = ma.suggest_anchors(repo)
ids = [s["id"] for s in sug.get("suggestions", [])]
if ids:
    assert ma.accept_suggestions(repo, ids)["ok"]
assert mb.learn_from_transcript(repo, repo / "session.log")["ok"]
pend = mb.learn_pending(repo, _record_review=False)
pids = [it["id"] for it in pend["pendingNodes"]]
assert mb.learn_approve(repo, pids[:1], approver="ray", paths=["src/billing.py"])["ok"]
assert mw._build_handover(repo, write=True)["ok"]
"""
        subprocess.run(
            [self.python, "-c", setup_script, str(v92_src), str(fixture_repo)],
            check=True,
            env=dict(os.environ),
            capture_output=True,
            text=True,
        )

        # Snapshot .memlet after 9.2.0 setup so both v92 and v93 probes see the exact same on-disk state at the same path
        memlet_backup = self.base / "memlet_backup"
        shutil.copytree(fixture_repo / ".memlet", memlet_backup)

        probe_script = """
import contextlib, io, json, sys, time
from pathlib import Path
time.perf_counter = lambda: 100.0
sys.path.insert(0, sys.argv[1])
import memory_cli, memlet_mcp
from memlet import memlet_wiki as mw, memlet_bridge as mb, memlet_graph as mg, memlet_anchors as ma

repo = Path(sys.argv[2])
srv = memlet_mcp.MemletMCPServer(root=repo)

def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    old = sys.argv[:]
    try:
        sys.argv = ["memlet"] + argv
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = memory_cli.cli()
            except SystemExit as exc:
                code = int(exc.code) if exc.code is not None else 0
    finally:
        sys.argv = old
    return {"code": code, "stdout": out.getvalue(), "stderr": err.getvalue()}

payload = {
    "lib": {
        "dream_items": mb.dream_items(repo, tiers=("approved", "hint", "pending")),
        "learn_pending": mb.learn_pending(repo, _record_review=False),
        "unified_recall": mb.unified_recall(repo, "refund"),
        "changed_check": mb.changed_check(repo),
        "wiki_status": mw.wiki_status(repo),
        "handover": mw._build_handover(repo),
        "graph_build": mg.build_graph(repo),
        "graph_neighbors": mg.neighbors(repo, "src/billing.py", tiers=("approved", "hint")),
        "anchors_suggest": ma.suggest_anchors(repo),
        "anchors_list": ma.list_sidecar_anchors(repo),
        "bridge_handover": (repo / ".memlet/handover.json").read_text(encoding="utf-8"),
        "bridge_capsules": (repo / ".memlet/capsules.json").read_text(encoding="utf-8"),
    },
    "cli": {
        "check_json": run_cli(["check", str(repo), "--format", "json"]),
        "check_md": run_cli(["check", str(repo), "--format", "markdown"]),
        "check_changed": run_cli(["check", str(repo), "--changed", "--format", "json"]),
        "recall_json": run_cli(["recall", "refund", "--repo", str(repo), "--format", "json"]),
        "recall_md": run_cli(["recall", "refund", "--repo", str(repo), "--format", "markdown"]),
        "handover_json": run_cli(["handover", str(repo), "--format", "json"]),
        "handover_md": run_cli(["handover", str(repo), "--format", "markdown"]),
        "learn_pending_json": run_cli(["learn", "--pending", "--repo", str(repo), "--format", "json"]),
        "learn_pending_md": run_cli(["learn", "--pending", "--repo", str(repo), "--format", "markdown"]),
        "dream_status_json": run_cli(["dream", str(repo), "--status", "--format", "json"]),
        "dream_status_md": run_cli(["dream", str(repo), "--status", "--format", "markdown"]),
        "graph_show_json": run_cli(["admin", "graph", "show", "src/billing.py", "--repo", str(repo), "--format", "json"]),
        "graph_show_md": run_cli(["admin", "graph", "show", "src/billing.py", "--repo", str(repo), "--format", "markdown"]),
        "graph_stats_json": run_cli(["admin", "graph", "stats", "--repo", str(repo), "--format", "json"]),
        "graph_stats_md": run_cli(["admin", "graph", "stats", "--repo", str(repo), "--format", "markdown"]),
        "anchors_suggest": run_cli(["admin", "anchors", "suggest", "--repo", str(repo), "--format", "json"]),
        "anchors_list": run_cli(["admin", "anchors", "list", "--repo", str(repo), "--format", "json"]),
    },
    "mcp": {
        "tools": memlet_mcp.mcp_tools(),
        "check": srv.call_tool("memlet_check", {"repo_path": str(repo)}),
        "recall": srv.call_tool("memlet_recall", {"query": "refund", "repo_path": str(repo)}),
        "handover": srv.call_tool("memlet_handover", {"repo_path": str(repo)}),
        "learn": srv.call_tool("memlet_learn", {"action": "pending", "repo_path": str(repo)}),
    },
}
sys.stdout.write(json.dumps(payload, sort_keys=True, ensure_ascii=False))
"""
        r_v92 = subprocess.run(
            [self.python, "-c", probe_script, str(v92_src), str(fixture_repo)],
            check=True,
            env=dict(os.environ),
            capture_output=True,
            text=True,
        )
        shutil.rmtree(fixture_repo / ".memlet")
        shutil.copytree(memlet_backup, fixture_repo / ".memlet")
        r_v93 = subprocess.run(
            [self.python, "-c", probe_script, str(REPO_ROOT), str(fixture_repo)],
            check=True,
            env=dict(os.environ),
            capture_output=True,
            text=True,
        )
        d92 = json.loads(r_v92.stdout)
        d93 = json.loads(r_v93.stdout)
        for section in ("lib", "cli", "mcp"):
            for key in d92[section]:
                with self.subTest(section=section, key=key):
                    self.assertEqual(d92[section][key], d93[section][key])
        self.assertEqual(r_v92.stdout.encode("utf-8"), r_v93.stdout.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
