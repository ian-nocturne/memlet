"""Unit Q6 regression tests: restore hooks' own-clock work limit, native _attachment_reader TypeError, and side-effect-free caller SQLite connections."""

from __future__ import annotations

import gc
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
import warnings
import weakref
from unittest import mock

HERE = Path(os.environ.get("MEMLET_TEST_TREE", str(Path(__file__).resolve().parent))).resolve()
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import memory_intelligence as mi
import memory_skills as ms
import work_memory as ledger
import work_store
from memlet import memlet_bridge as mb
from memlet import memlet_dream as md
from memlet import memlet_graph as mg
from memlet import memlet_tidy as mt
from memlet import memlet_wiki as mw
from memlet import standalone
from memlet import vocabulary as v


def _ceiling_dirs(scratch: Path) -> str:
    roots = {str(scratch.resolve()), str(scratch.resolve().parent), str(Path(tempfile.gettempdir()).resolve())}
    return os.pathsep.join(sorted(roots))


def _isolated_env(scratch: Path, extra: dict | None = None) -> dict:
    env = dict(os.environ)
    iso = (scratch / "iso_env").resolve()
    for sub in ("home", "tmp", "cfg", "codex", "store", "locks"):
        (iso / sub).mkdir(parents=True, exist_ok=True)
    git_global = iso / "home" / ".gitconfig"
    if not git_global.exists():
        git_global.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n", encoding="utf-8")
    env.update({
        "HOME": str(iso / "home"),
        "TMPDIR": str(iso / "tmp"),
        "CLAUDE_CONFIG_DIR": str(iso / "cfg"),
        "CODEX_HOME": str(iso / "codex"),
        "MEMLET_HOME": str(iso / "store"),
        "MEMLET_LOCK_DIR": str(iso / "locks"),
        "GIT_CEILING_DIRECTORIES": _ceiling_dirs(scratch),
        "GIT_CONFIG_NOSYSTEM": "1",
        "TZ": "UTC",
    })
    if extra:
        env.update(extra)
    return env


def _extract_commit_tree(commit: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    git_repo = Path(os.environ.get("MEMLET_GIT_REPO", str(HERE))).resolve()
    archive = subprocess.Popen(
        ["git", "-C", str(git_repo), "archive", commit],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    untar = subprocess.Popen(
        ["tar", "-x", "-C", str(dest)],
        stdin=archive.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    archive.stdout.close()
    _, untar_err = untar.communicate(timeout=60)
    _, arch_err = archive.communicate(timeout=60)
    if archive.returncode != 0 or untar.returncode != 0:
        raise AssertionError(
            f"git archive {commit} failed: "
            f"{arch_err.decode('utf-8', 'replace')} / {untar_err.decode('utf-8', 'replace')}"
        )
    return dest.resolve()


def _make_big_rules_repo(repo: Path, env: dict, rule_count: int = 200) -> Path:
    repo = repo.resolve()
    shutil.rmtree(repo, ignore_errors=True)
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(repo), env=env, check=True)
    (repo / "big.py").write_text(
        "".join(f"def fn_{i}(x):\n    return x + {i}\n\n" for i in range(rule_count)),
        encoding="utf-8",
    )
    (repo / "CLAUDE.md").write_text(
        "# Rules\n\n"
        + "".join(f"- Never change `fn_{i}` in `big.py` (`big.py::fn_{i}`) without review.\n" for i in range(rule_count)),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=str(repo), env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=str(repo), env=env, check=True)
    mw.init_project(repo)
    mw.import_rules(repo)
    return repo


class TestQ6Defects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._class_scratch = Path(tempfile.mkdtemp(prefix="memlet_q6_cls_")).resolve()
        cls._eedbd85_tree = _extract_commit_tree("eedbd85", cls._class_scratch / "eedbd85")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._class_scratch, ignore_errors=True)

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="memlet_q6_")).resolve()
        self.env = _isolated_env(self.scratch)

    def tearDown(self):
        ms._invalidate_task_link_cache()
        shutil.rmtree(self.scratch, ignore_errors=True)

    # ------------------------------------------------------------------
    # Fix 1 (Q5-M1): Memlet's own clock checked on every item; caller clock only at checkpoints
    # ------------------------------------------------------------------
    def test_q5_m1a_virtual_clock_neighbors_big_py_equals_eedbd85(self):
        base_repo = _make_big_rules_repo(self.scratch / "big200", self.env, rule_count=200)
        probe_script = (self.scratch / "run_vd.py").resolve()
        probe_script.write_text(
            textwrap.dedent(
                """\
                import json, sys
                from pathlib import Path
                code, repo_s, cost_s = sys.argv[1:4]
                cost = float(cost_s)
                sys.path.insert(0, code)
                from memlet import memlet_graph as mg, memlet_wiki as mw
                T = [0.0]
                mg._monotonic = lambda: T[0]
                orig = mw._verify_rules
                def slow_verify(root, rules, *a, **k):
                    T[0] += cost * max(1, len(rules))
                    return orig(root, rules, *a, **k)
                mw._verify_rules = slow_verify
                counters = {}
                res = mg.neighbors(Path(repo_s), "big.py", _counters=counters)
                print(json.dumps({
                    "ok": res.get("ok"),
                    "anchorsClassified": counters.get("anchorsClassified"),
                    "virtualSecondsUsed": round(T[0], 6),
                    "overrunSeconds": round(max(0.0, T[0] - mg.MAX_NEIGHBOR_SECONDS), 6),
                    "incomplete": res.get("incomplete"),
                    "notices": res.get("notices"),
                    "formatted": res.get("formatted"),
                }))
                """
            ),
            encoding="utf-8",
        )
        cost = 0.05
        outputs = {}
        for label, tree in (("eedbd85", self._eedbd85_tree), ("head", HERE)):
            repo_copy = (self.scratch / f"repo_{label}").resolve()
            shutil.copytree(base_repo, repo_copy)
            proc = subprocess.run(
                [sys.executable, "-B", str(probe_script), str(tree), str(repo_copy), str(cost)],
                cwd=str(self.scratch),
                env=self.env,
                capture_output=True,
                text=True,
                check=True,
            )
            outputs[label] = json.loads(proc.stdout)

        expected = outputs["eedbd85"]
        actual = outputs["head"]
        self.assertEqual(actual["anchorsClassified"], expected["anchorsClassified"])
        self.assertLessEqual(actual["overrunSeconds"], cost + 1e-9)
        self.assertEqual(actual["virtualSecondsUsed"], expected["virtualSecondsUsed"])
        self.assertEqual(actual["incomplete"], expected["incomplete"])
        self.assertEqual(actual["notices"], expected["notices"])
        self.assertEqual(actual["formatted"], expected["formatted"])

    def test_q5_m1a_all_graph_and_bridge_loops_respect_own_clock(self):
        repo = _make_big_rules_repo(self.scratch / "loops_repo", self.env, rule_count=20)

        # 1. _slice loop in memlet_graph.py
        T = [0.0]
        fake_nodes = [
            {
                "id": f"lesson-{i:024x}",
                "approval": {"state": "approved", "at": "2026-01-01T00:00:00Z", "by": "u"},
                "approvedPaths": ["big.py"],
                "primaryPin": {"span": "s.log:1-1", "sha256": "0" * 64, "quoteText": "1: check big.py"},
                "edges": [],
            }
            for i in range(30)
        ]
        orig_lesson_lines = md.lesson_lines

        def slow_lesson_lines(node):
            T[0] += 0.35
            return orig_lesson_lines(node)

        with mock.patch.object(mg, "_monotonic", side_effect=lambda: T[0]), \
                mock.patch.object(md, "lesson_lines", side_effect=slow_lesson_lines):
            work = mg._Work(bounded=True)
            sliced = mg._slice({"nodes": fake_nodes, "edges": []}, "big.py", None, work=work)
        self.assertEqual(sliced["nodes"], [])
        self.assertTrue(work.exhausted)
        self.assertLessEqual(T[0], mg.MAX_NEIGHBOR_SECONDS + 0.35 + 1e-9)

        # 2. Projected-items loop in memlet_graph._project_context
        T = [0.0]
        fake_items = [
            {
                "id": f"lesson-{i:024x}",
                "tier": "approved",
                "cites": [{"file": "s.log", "start": 1, "end": 1, "sha256": "0" * 64, "span": "s.log:1-1"}],
                "quoteLines": [f"rule line {i} on big.py"],
                "text": f"rule line {i} on big.py",
            }
            for i in range(20)
        ]
        fake_queue = {
            "nodes": [
                {
                    "id": it["id"],
                    "approval": {"state": "approved", "at": "2026-01-01T00:00:00Z", "by": "u"},
                    "approvedPaths": ["big.py"],
                    "primaryPin": {"span": "s.log:1-1", "sha256": "0" * 64, "quoteText": f"1: {it['text']}"},
                    "edges": [],
                }
                for it in fake_items
            ],
            "edges": [],
        }
        attached_calls = [0]

        def slow_attached(root, node, item, ctx):
            attached_calls[0] += 1
            T[0] += 0.45
            return {"big.py": mg.GRAPH_WHY[2]}

        with mock.patch.object(mg, "_monotonic", side_effect=lambda: T[0]), \
                mock.patch.object(mb, "dream_items", return_value={"ok": True, "items": fake_items, "notices": []}), \
                mock.patch.object(mg, "_sources", return_value=([], {}, {}, False)), \
                mock.patch.object(mg, "_attached", side_effect=slow_attached):
            work = mg._Work(bounded=True)
            with mb._lesson_path_projection(repo, work) as pctx, md._enter_read_scope() as rstate:
                nodes, _, _, _, _ = mg._project_context(
                    repo, None, path="big.py", symbol=None, ident=None,
                    queue=fake_queue, work=work, path_context=pctx, _read_state=rstate,
                )
        self.assertTrue(work.exhausted)
        self.assertLess(attached_calls[0], 10)
        self.assertLess(sum(1 for n in nodes.values() if n.get("kind") == "lesson"), 10)

        # 3. _History per-item recovery loop and replay row loop
        ledger_db = repo / mw.LEDGER_PATH
        ledger_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(ledger_db)) as lconn:
            lconn.execute(
                "CREATE TABLE IF NOT EXISTS dream_decision_events "
                "(seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, item_id TEXT, kind TEXT, decision TEXT, by TEXT, digest TEXT, paths TEXT)"
            )
            lconn.execute(
                "INSERT INTO dream_decision_events (at, item_id, kind, decision, by, digest, paths) "
                "VALUES ('2026-01-01T00:00:00Z', 'n0', 'node', 'approved', 'u', 'sha256:" + ("a" * 64) + "', '[]')"
            )
            lconn.commit()

        # 3a. _History truncated per-item query loop
        T = [0.0]
        item_query_calls = [0]

        def fake_decision_events_trunc(root_arg, item_id=None):
            if item_id is None:
                return mb.DecisionEvents([], skipped=0, truncated=True)
            item_query_calls[0] += 1
            T[0] += 0.5
            return mb.DecisionEvents([], skipped=0, truncated=False)

        many_graph = {"nodes": [{"id": f"n{i}", "edges": []} for i in range(20)], "edges": []}
        with mock.patch.object(mg, "_monotonic", side_effect=lambda: T[0]), \
                mock.patch.object(mb, "decision_events", side_effect=fake_decision_events_trunc):
            work = mg._Work(bounded=True)
            hist_a = mg._History(repo, work, graph=many_graph)
        self.assertTrue(work.exhausted)
        self.assertLess(item_query_calls[0], 10)
        self.assertIn(v.GRAPH_EVENTS_NOTICE, hist_a.notices)

        # 3b. _History row processing loop
        T = [0.0]
        stamp_calls = [0]
        orig_time = mg._time

        def slow_time(val):
            stamp_calls[0] += 1
            T[0] += 0.5
            return orig_time(val)

        event_rows = mb.DecisionEvents(
            [
                {
                    "seq": i + 1,
                    "at": f"2026-01-01T00:00:{i:02d}Z",
                    "item_id": f"n{i}",
                    "kind": "node",
                    "decision": "approved",
                    "by": "u",
                    "digest": "sha256:" + ("b" * 64),
                    "paths": [],
                }
                for i in range(20)
            ],
            skipped=0,
            truncated=False,
        )
        with mock.patch.object(mg, "_monotonic", side_effect=lambda: T[0]), \
                mock.patch.object(mb, "decision_events", return_value=event_rows), \
                mock.patch.object(mg, "_time", side_effect=slow_time):
            work = mg._Work(bounded=True)
            hist_b = mg._History(repo, work, graph={"nodes": [], "edges": []})
        self.assertTrue(work.exhausted)
        self.assertLess(stamp_calls[0], 10)
        self.assertEqual(hist_b.decisions, {})
        self.assertIn(v.GRAPH_EVENTS_NOTICE, hist_b.notices)

        # 4. _valid_lesson_path component walk loop in memlet_bridge.py
        deep_parts = [f"d{i}" for i in range(14)]
        deep_dir = repo.joinpath(*deep_parts)
        deep_dir.mkdir(parents=True, exist_ok=True)
        deep_leaf = deep_dir / "leaf.py"
        deep_leaf.write_text("x = 1\n", encoding="utf-8")
        rel_deep = "/".join(deep_parts + ["leaf.py"])

        T = [0.0]
        counters = {}
        with mock.patch.object(mg, "_monotonic", side_effect=lambda: T[0]):
            work = mg._Work(bounded=True, counters=counters)
            with mb._lesson_path_projection(repo, work) as ctx:
                orig_iterdir = Path.iterdir

                def slow_iterdir(self_path):
                    T[0] += 0.4
                    return orig_iterdir(self_path)

                with mock.patch.object(Path, "iterdir", slow_iterdir):
                    ok_deep = mb.valid_lesson_path(repo, rel_deep, context=ctx)
        self.assertFalse(ok_deep)
        self.assertTrue(work.exhausted)
        self.assertLess(sum(counters["directoryListings"].values()), 10)

        # 5. literal_lesson_paths and _lesson_literal_paths loops in memlet_bridge.py
        for idx in range(20):
            (repo / f"m{idx}.py").write_text(f"v = {idx}\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), env=self.env, check=True)
        subprocess.run(["git", "commit", "-qm", "files"], cwd=str(repo), env=self.env, check=True)

        T = [0.0]
        valid_calls = [0]
        orig_valid = mb.valid_lesson_path

        def slow_valid(root_arg, path_arg, *, context=None):
            valid_calls[0] += 1
            T[0] += 0.45
            return orig_valid(root_arg, path_arg, context=context)

        line_many = "Check " + " ".join(f"`m{idx}.py`" for idx in range(20))
        with mock.patch.object(mg, "_monotonic", side_effect=lambda: T[0]), \
                mock.patch.object(mb, "valid_lesson_path", side_effect=slow_valid):
            work = mg._Work(bounded=True)
            with mb._lesson_path_projection(repo, work) as ctx:
                mb.lesson_path_context(repo, context=ctx)
                found_paths = mb.literal_lesson_paths(repo, "repo", line_many, context=ctx)
        self.assertTrue(work.exhausted)
        self.assertLess(valid_calls[0], 10)
        self.assertLess(len(found_paths), 8)

        # 6. _lesson_literal_paths edge & evidence loops in memlet_bridge.py
        T = [0.0]
        resolve_calls = [0]

        def slow_resolve(snap, span):
            resolve_calls[0] += 1
            T[0] += 0.45
            return {"span": span, "sha256": "0" * 64, "quoteText": f"1: Check `m0.py`"}

        fake_node = {
            "id": "lesson-" + ("c" * 24),
            "scopeTag": "repo",
            "primaryPin": {"span": "s.log:1-20", "sha256": "0" * 64, "quoteText": "\n".join(f"{k+1}: Check `m{k}.py`" for k in range(20))},
            "edges": [],
        }
        cites_20 = [{"span": f"s.log:{k+1}-{k+1}", "sha256": "0" * 64, "quote": f"Check `m{k}.py`"} for k in range(20)]
        with mock.patch.object(mg, "_monotonic", side_effect=lambda: T[0]),                 mock.patch.object(md, "lesson_lines", return_value=cites_20),                 mock.patch.object(md, "_resolve_pin", side_effect=slow_resolve):
            work = mg._Work(bounded=True)
            with mb._lesson_path_projection(repo, work) as ctx:
                mb.lesson_path_context(repo, context=ctx)
                ctx["snapshot"] = {"s.log": [f"Check `m{k}.py`" for k in range(20)]}
                lit_out = mb._lesson_literal_paths(fake_node, repo, ctx)
        self.assertTrue(work.exhausted)
        self.assertLess(resolve_calls[0], 10)
        self.assertLessEqual(len(lit_out), 1)

    def test_q5_m1b_pre_tool_use_hook_bounded_equals_eedbd85(self):
        base_repo = _make_big_rules_repo(self.scratch / "hook_base", self.env, rule_count=30)
        cfg_path = base_repo / ".memlet" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["hooks"] = {
            "scopes": {
                "local": {"jit": True, "guard": "deny", "capsule": True, "dream": False, "tokens": 1200}
            }
        }
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

        mon_dir = (self.scratch / "hookmon").resolve()
        mon_dir.mkdir(parents=True, exist_ok=True)
        (mon_dir / "sitecustomize.py").write_text(
            textwrap.dedent(
                """\
                import time
                from memlet import memlet_wiki as mw
                _orig = mw._verify_rules
                _count = [0]
                _t0 = [None]
                def _slow_verify(root, rules, *a, **k):
                    if isinstance(rules, list) and len(rules) == 1:
                        if _t0[0] is None:
                            _t0[0] = time.monotonic()
                        _count[0] += 1
                        if _count[0] == 8:
                            rem = 2.55 - (time.monotonic() - _t0[0])
                            if rem > 0:
                                time.sleep(rem)
                        elif _count[0] > 8:
                            time.sleep(0.20)
                    return _orig(root, rules, *a, **k)
                mw._verify_rules = _slow_verify
                """
            ),
            encoding="utf-8",
        )

        outputs = {}
        for label, tree in (("eedbd85", self._eedbd85_tree), ("head", HERE)):
            repo_copy = (self.scratch / f"hook_repo_{label}").resolve()
            shutil.copytree(base_repo, repo_copy)
            payload = json.dumps({
                "session_id": "s1",
                "cwd": str(repo_copy),
                "hook_event_name": "PreToolUse",
                "source": "startup",
                "tool_name": "Edit",
                "tool_use_id": "u1",
                "tool_input": {
                    "file_path": str(repo_copy / "big.py"),
                    "old_string": "x + 1",
                    "new_string": "x + 2",
                },
                "tool_response": {"success": True},
            })
            hook_env = _isolated_env(
                self.scratch / f"env_{label}",
                {"PYTHONPATH": str(mon_dir) + os.pathsep + str(tree)},
            )
            proc = subprocess.run(
                [sys.executable, "-B", str(tree / "memory_cli.py"), "admin", "hook", "pre-tool-use", "--scope", "local"],
                input=payload,
                cwd=str(repo_copy),
                env=hook_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs[label] = proc.stdout

        self.assertTrue(outputs["eedbd85"].strip(), "Expected non-empty PreToolUse output on eedbd85")
        self.assertTrue(outputs["head"].strip(), "PreToolUse hook overran and printed 0 bytes on HEAD")
        parsed = json.loads(outputs["head"])
        self.assertEqual(parsed["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertIn("big.py", parsed["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(outputs["head"], outputs["eedbd85"])

    def test_q5_m1c_caller_clock_read_only_at_checkpoints(self):
        repo = _make_big_rules_repo(self.scratch / "caller_clk", self.env, rule_count=200)
        clock_calls = [0]

        def caller_clock():
            clock_calls[0] += 1
            return 100.0 + clock_calls[0] * 0.001

        counters = {}
        res = mg.neighbors(repo, "big.py", _clock=caller_clock, _counters=counters)
        self.assertTrue(res["ok"])
        self.assertEqual(counters["anchorsClassified"], mg.MAX_NEIGHBOR_ANCHORS)
        self.assertLessEqual(clock_calls[0], 12, f"Caller _clock read {clock_calls[0]} times (expected checkpoint-only)")

    # ------------------------------------------------------------------
    # Fix 2 (SF1): native TypeError through _attachment_reader equals eedbd85
    # ------------------------------------------------------------------
    def test_sf1_attachment_reader_native_typeerror_equals_eedbd85(self):
        repo = _make_big_rules_repo(self.scratch / "sf1_repo", self.env, rule_count=2)
        probe_script = (self.scratch / "run_sf1.py").resolve()
        probe_script.write_text(
            textwrap.dedent(
                """\
                import json, sys
                from pathlib import Path
                code, repo_s = sys.argv[1:3]
                repo = Path(repo_s)
                sys.path.insert(0, code)
                from memlet import memlet_bridge as mb, memlet_graph as mg, memlet_tidy as mt, memlet_wiki as mw
                cases = [
                    ("dream_items(bogus=1)", lambda: mb.dream_items(repo, bogus=1)),
                    ("dream_items(repo, x)", lambda: mb.dream_items(repo, "approved")),
                    ("dream_items(tiers=123)", lambda: mb.dream_items(repo, tiers=123)),
                    ("unified_recall(bogus=1)", lambda: mb.unified_recall(repo, "q", bogus=1)),
                    ("unified_recall(repo, q, 1)", lambda: mb.unified_recall(repo, "q", 1)),
                    ("changed_check(repo, 1, 2)", lambda: mb.changed_check(repo, 1, 2)),
                    ("changed_check(bogus=1)", lambda: mb.changed_check(repo, bogus=1)),
                    ("neighbors()", lambda: mg.neighbors()),
                    ("build_handover(x=1)", lambda: mw.build_handover(repo, x=1)),
                    ("tidy_report(1,2)", lambda: mt.tidy_report(repo, 2)),
                ]
                out = {}
                for label, fn in cases:
                    try:
                        fn()
                        out[label] = ["ok", ""]
                    except Exception as exc:
                        out[label] = [type(exc).__name__, str(exc)]
                print(json.dumps(out, sort_keys=True))
                """
            ),
            encoding="utf-8",
        )
        results = {}
        for label, tree in (("eedbd85", self._eedbd85_tree), ("head", HERE)):
            proc = subprocess.run(
                [sys.executable, "-B", str(probe_script), str(tree), str(repo)],
                cwd=str(self.scratch),
                env=self.env,
                capture_output=True,
                text=True,
                check=True,
            )
            results[label] = json.loads(proc.stdout)
        self.assertEqual(results["head"], results["eedbd85"])

    # ------------------------------------------------------------------
    # Fix 3 (SF2): no side effects on caller connection; weakref subclass for Memlet store connections
    # ------------------------------------------------------------------
    def test_sf2_caller_connection_no_side_effects_and_within_call_reuse(self):
        db_path = (self.scratch / "caller_plain.sqlite").resolve()
        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            ledger.ensure_schema(conn)
            mi.ensure_schema(conn)
            ms.ensure_schema(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, project TEXT, title TEXT, owner TEXT, revision INTEGER, updated REAL, summary TEXT, handoff TEXT, created REAL)"
            )
            conn.execute(
                "INSERT INTO tasks (id, project, title, owner, revision, updated, summary, handoff, created) "
                "VALUES ('T', 'proj', 'Title', 'owner-1', 1, 1.0, '{}', '{}', 1.0)"
            )
            for i in range(6):
                ledger.record(
                    conn, "T", 1, f"op-item-{i}", "fact", f"fact-{i}", "assert",
                    f"deploy server port {4300 + i} zero downtime check {i}",
                    {"kind": "observed-response", "session": "owner-1"},
                    event_at=10.0 + i,
                )
            for i in range(4):
                conn.execute(
                    "INSERT INTO memory_skills ("
                    "task, skill_id, name, experience_id, revision, version, event_id, status, evidence_state, scope, "
                    "title, description, triggers, steps, outcome, conditions, counterexamples, body, skill_path, file_sha256, "
                    "source, helpful_count, used_count, incorrect_count, missing_count, valid_from, valid_until, event_at, observed_at"
                    ") VALUES (?, ?, ?, ?, 1, 1, ?, 'active', 'user-confirmed', 'task', ?, ?, ?, ?, 'ok', 'when', 'never', 'body', ?, 'abc', '{}', 1, 1, 0, 0, NULL, NULL, 10.0, 10.0)",
                    (
                        "T",
                        f"skill-{i}",
                        f"skill-name-{i}",
                        f"exp-{i}",
                        i + 1,
                        f"Deploy skill {i}",
                        f"deploy server {i}",
                        '["deploy"]',
                        '["step"]',
                        str(self.scratch / f"skill_{i}.md"),
                    ),
                )
            conn.execute("INSERT INTO memory_links VALUES ('T', 'fact-0', 'fact-1', 'related', 1.0, '', 10.0)")
            conn.execute("INSERT INTO memory_links VALUES ('T', 'skill-0', 'skill-1', 'related', 1.0, '', 10.0)")
            conn.commit()
            ms._invalidate_task_link_cache()

            auth_events = []

            def authorizer(action, arg1, arg2, dbname, source):
                if action == getattr(sqlite3, "SQLITE_FUNCTION", 31):
                    auth_events.append((action, arg1, arg2))
                return sqlite3.SQLITE_OK

            traced = []
            conn.set_authorizer(authorizer)
            conn.set_trace_callback(traced.append)
            try:
                g1 = ms._load_task_link_graph(conn, "T")
                ppr_scores, _ = ms.ppr_propagate(conn, "T", {"fact-0": 1.0})
                wlinks = ms.node_wiki_links(conn, "T", "fact-0")
            finally:
                conn.set_trace_callback(None)
                conn.set_authorizer(None if sys.version_info >= (3, 11) else (lambda *a: sqlite3.SQLITE_OK))

            expected_graph_sql = "SELECT source_id, target_id, relation, weight FROM memory_links WHERE task='T' ORDER BY weight DESC, target_id ASC"
            self.assertEqual(
                traced,
                [expected_graph_sql, expected_graph_sql, expected_graph_sql],
                f"Unexpected SQL on caller connection: {traced!r}",
            )
            self.assertFalse(
                any("_memlet_link_token" in str(ev) for ev in auth_events),
                f"Unexpected SQL function activity on caller connection: {auth_events!r}",
            )
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("SELECT _memlet_link_token()")
            self.assertEqual(ms._TASK_LINK_CACHE, {})
            self.assertIn("fact-1", ppr_scores)
            self.assertEqual(wlinks, ["[[fact-1]]"])

            # External process write is seen immediately on plain caller connection (no cross-call cache)
            ext_script = (
                "import sqlite3, sys\n"
                "c = sqlite3.connect(sys.argv[1])\n"
                "c.execute(\"INSERT INTO memory_links VALUES ('T', 'fact-0', 'fact-ext', 'related', 2.0, '', 11.0)\")\n"
                "c.commit()\n"
                "c.close()\n"
            )
            subprocess.run(
                [sys.executable, "-B", "-c", ext_script, str(db_path)],
                cwd=str(self.scratch),
                env=self.env,
                check=True,
            )
            g2 = ms._load_task_link_graph(conn, "T")
            self.assertIsNot(g1, g2)
            self.assertEqual(ms.node_wiki_links(conn, "T", "fact-0"), ["[[fact-ext]]", "[[fact-1]]"])

            # Within-call reuse on a plain caller connection: retrieve runs the link-graph query once per call
            recall_trace_1 = []
            conn.set_trace_callback(recall_trace_1.append)
            try:
                r1 = mi.retrieve(conn, "T", "deploy server port", budget=4000, root=self.scratch)
            finally:
                conn.set_trace_callback(None)
            self.assertFalse(r1["abstained"])
            link_graph_queries_1 = [s for s in recall_trace_1 if s == expected_graph_sql]
            self.assertEqual(len(link_graph_queries_1), 1, f"Expected 1 link-graph query within single retrieve call, got: {recall_trace_1!r}")
            self.assertFalse(any("_memlet_link_token" in s or "data_version" in s or "schema_version" in s for s in recall_trace_1))

            recall_trace_2 = []
            conn.set_trace_callback(recall_trace_2.append)
            try:
                r2 = mi.retrieve(conn, "T", "deploy server port", budget=4000, root=self.scratch)
            finally:
                conn.set_trace_callback(None)
            self.assertFalse(r2["abstained"])
            link_graph_queries_2 = [s for s in recall_trace_2 if s == expected_graph_sql]
            self.assertEqual(len(link_graph_queries_2), 1, "Plain caller connection must not cache across calls, only within a call")
        finally:
            conn.close()

    def test_sf2_memlet_connection_weakref_gc_and_strong_ref_mutant_guard(self):
        ms._invalidate_task_link_cache()
        s_dir = (self.scratch / "standalone_store").resolve()
        w_dir = (self.scratch / "work_store").resolve()
        s_store = standalone.StandaloneStore(s_dir)
        s_store.ensure_standalone_task("T")
        w_store = work_store.WorkStore(w_dir)

        for label, factory in (("StandaloneStore", s_store.db), ("WorkStore", w_store.db)):
            ms._invalidate_task_link_cache()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                with factory(True) as db:
                    self.assertIsInstance(db, sqlite3.Connection)
                    ms.ensure_schema(db)
                    db.execute("INSERT OR REPLACE INTO memory_links VALUES ('T', 'A', 'B', 'related', 1.0, '', 1.0)")
                with factory(False) as db:
                    self.assertIsInstance(db, sqlite3.Connection)
                    conn_wref = weakref.ref(db)
                    self.assertIs(conn_wref(), db)
                    with self.assertRaises(sqlite3.OperationalError):
                        db.execute("SELECT _memlet_link_token()")
                    g1 = ms._load_task_link_graph(db, "T")
                    traced_hit = []
                    db.set_trace_callback(traced_hit.append)
                    try:
                        g2 = ms._load_task_link_graph(db, "T")
                    finally:
                        db.set_trace_callback(None)
                    self.assertIs(g1, g2, f"{label} connection should hit cross-call link cache while alive")
                    self.assertFalse(
                        any("memory_links" in s for s in traced_hit),
                        f"{label} cache hit should not re-query memory_links: {traced_hit!r}",
                    )
                del db
                gc.collect()

            self.assertIsNone(
                conn_wref(),
                f"{label} connection remained alive after close/drop (strong reference leaked)",
            )
            self.assertEqual(len(ms._TASK_LINK_CACHE), 1)
            dead_entry = next(iter(ms._TASK_LINK_CACHE.values()))
            self.assertIsInstance(dead_entry[0], weakref.ReferenceType)
            self.assertIsNone(dead_entry[0]())

            # Next lookup sweeps the dead weakref and misses cleanly
            with factory(False) as db2:
                g3 = ms._load_task_link_graph(db2, "T")
                self.assertIsNot(g3, g1)
                self.assertEqual(len(ms._TASK_LINK_CACHE), 1)
                live_entry = next(iter(ms._TASK_LINK_CACHE.values()))
                self.assertIs(live_entry[0](), db2)


if __name__ == "__main__":
    unittest.main()
