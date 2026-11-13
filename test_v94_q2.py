"""Unit Q2 — Final audit regression and compatibility tests for Memlet 9.4.

Covers:
- B/M-1 (MUST): `--again` forgets current batch marks under the lock and walks all 5 batches (`--max-batches 2`),
  both via `--again`-then-plain runs and repeated `--again` runs, with accurate unexamined notices.
- B/M-2 (MUST): `admin tidy` omits `modelSaysSame` and emits no notice unless a regular proposals file exists.
- C/MF-1 (MUST): Plug-in admin commands (`graph`, `anchors`, `eval`, `tidy`, `skillbook`) render text on a real PTY
  when `--format` is omitted and JSON when piped or `--format json` is passed.
- C/MF-2 (MUST): `learn --paths` in both `modelSaysSame` and `duplicates` uses `MERGE_ATTACH_LABEL`, never `Or:`.
- C/MF-3 (MUST): `merge-blocked` dropped groups and `--status` state reasons use plain-language sentences in JSON and
  Markdown, with zero internal function names in any output.
- C/MF-4 (MUST): Model input tiers (approved/hint/pending only), row validation rules, `learn --unmerge` reopening,
  and `admin skillbook --check` on a 9.3.0-written skill file.
- B/S-1..S-3, B/N-2..N-5, C/SF-1, C/SF-2, C/SF-4..SF-6, C/N-2, and custom 9.3.0 (`0ca681f`) byte comparison on a
  fixture repository containing real hints.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import io
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest import mock

import memory_cli
from memlet import (
    VERSION,
    cli_dream,
    handover_bridge as hb,
    memlet_bridge as mb,
    memlet_dream as md,
    memlet_dreamcycle as dc,
    memlet_dreamtidy as mdtidy,
    memlet_skillbook as sb,
    memlet_tidy as mt,
    memlet_wiki as mw,
    vocabulary as v,
)

REPO_ROOT = Path(__file__).resolve().parent
CODE_ROOT = Path(os.environ.get("MEMLET_TEST_CODE_ROOT", str(REPO_ROOT))).resolve()
PUBLISHED_930 = "0ca681f"


def _run_inproc_cli(argv: Sequence[str]) -> Tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", ["memlet"] + list(argv)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = memory_cli.cli()
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 2)
    return code, out.getvalue(), err.getvalue()


def _rows_from_stdin(stdin_text: str) -> List[Dict[str, str]]:
    return [json.loads(line) for line in stdin_text.splitlines() if line.strip()]


class Q2FinalFixesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="memlet-q2-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        for sub in ("home", "tmp", "claude", "codex"):
            (self.base / sub).mkdir()
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.base / "home"),
                "TMPDIR": str(self.base / "tmp"),
                "CLAUDE_CONFIG_DIR": str(self.base / "claude"),
                "CODEX_HOME": str(self.base / "codex"),
                "GIT_CEILING_DIRECTORIES": str(self.base),
                "MEMLET_NO_DEPRECATION": "1",
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def child_env(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = dict(os.environ)
        if extra:
            env.update(extra)
        return env

    def make_repo(self, name: str = "repo", *, git: bool = False) -> Path:
        repo = self.base / name
        repo.mkdir()
        (repo / "AGENTS.md").write_text(
            "# Rules\n- Keep settlement calls in `src/billing.py` (`src/billing.py::settle`) idempotent.\n"
            "- Validate schema migrations in src/schema.py before deploy.\n",
            encoding="utf-8",
        )
        (repo / "src").mkdir()
        (repo / "src/billing.py").write_text("def settle(order_id):\n    return order_id\n", encoding="utf-8")
        (repo / "src/schema.py").write_text("def migrate():\n    return True\n", encoding="utf-8")
        (repo / "src/extra.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
        if git:
            subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, env=self.child_env())
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=str(repo), check=True, env=self.child_env())
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True, env=self.child_env())
            subprocess.run(["git", "add", "."], cwd=str(repo), check=True, env=self.child_env())
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True, env=self.child_env())
        res = mw.init_project(repo)
        self.assertTrue(res["ok"], res)
        return repo

    def learn_line(self, repo: Path, log_name: str, line: str) -> str:
        src = self.base / log_name
        src.write_text(line.rstrip("\n") + "\n", encoding="utf-8")
        before = {n["id"] for n in (mb._load_queue(repo)[0] or {}).get("nodes", [])}
        res = mb.learn_from_transcript(repo, src)
        self.assertTrue(res["ok"], res)
        added = [n["id"] for n in (mb._load_queue(repo)[0] or {}).get("nodes", []) if n["id"] not in before]
        self.assertEqual(1, len(added), line)
        return added[0]

    def _add_real_dream_lesson(
        self,
        repo: Path,
        log_name: str,
        line_text: str,
        *,
        category: str = "repeated_pitfall",
        edges: Sequence[Dict[str, str]] = (),
        verify_verdict: Optional[str] = None,
    ) -> str:
        src = self.base / log_name
        src.write_text(line_text.rstrip("\n") + "\n", encoding="utf-8")
        cfg, _ = mb._load_config(repo)
        cfg["dream"] = {"hints": True, "agreement": "model", "models": {"claude": "haiku", "verifier": {"claude": "sonnet"}}}
        mw.write_managed_text(repo, mw.CONFIG_PATH, json.dumps(cfg, indent=2) + "\n")
        ing = dc.dream_ingest(repo, src)
        self.assertTrue(ing["ok"], ing)
        before = {n["id"] for n in (mb._load_queue(repo)[0] or {}).get("nodes", [])}

        req_edges = [
            {"edge_type": e.get("edge_type") or e.get("edgeType", "GOVERNS"), "target": e.get("target", "src/billing.py")}
            for e in (edges or ({"edge_type": "BLOCKS" if verify_verdict else "GOVERNS", "target": "src/billing.py"},))
        ]

        def runner(_argv: Sequence[str], prompt: str, *, stdin_text: str = "", **_kw: Any) -> str:
            if "SUPPORTED" in prompt and verify_verdict is not None:
                return json.dumps({"verdict": verify_verdict})
            rows = [json.loads(ln) for ln in stdin_text.splitlines() if ln.strip()]
            target_row = rows[-1]
            return json.dumps([
                {
                    "category": category,
                    "title": "Rule from " + Path(log_name).stem.replace("_", " "),
                    "scope_tag": req_edges[0]["target"],
                    "src_spans": [target_row["id"]],
                    "edges": req_edges,
                }
            ])

        dr = dc.dream_run(
            repo,
            via=("claude",),
            verify_with="claude" if verify_verdict is not None else None,
            runner=runner,
        )
        self.assertTrue(dr["ok"], dr)
        after = [n["id"] for n in (mb._load_queue(repo)[0] or {}).get("nodes", []) if n["id"] not in before]
        self.assertEqual(1, len(after), dr)
        return after[0]

    # ------------------------------------------------------------------
    # MUST 1: B/M-1 (`--again` with 5 batches and `--max-batches 2`)
    # ------------------------------------------------------------------
    def test_must_b_m1_again_walks_all_five_batches_once(self) -> None:
        repo = self.make_repo("m1_repo")
        ids = [
            self.learn_line(
                repo,
                f"m1_{i:02d}.log",
                f"2026-11-01T09:{i:02d}:00Z [user] Never bypass settlement guard #{i} in src/billing.py during retries.",
            )
            for i in range(10)
        ]
        seen_batches: List[Tuple[str, ...]] = []

        def runner(_argv: Sequence[str], _prompt: str, *, stdin_text: str = "", **_kw: Any) -> str:
            seen_batches.append(tuple(r["text"] for r in _rows_from_stdin(stdin_text)))
            return "[]"

        with mock.patch.object(mdtidy, "MAX_BATCH_LESSONS", 2):
            # Initial complete examination across all 5 batches (10 lessons).
            init_r1 = mdtidy.dream_tidy_run(repo, via=("claude",), max_batches=10, runner=runner)
            self.assertTrue(init_r1["ok"])
            self.assertEqual(5, init_r1["batches"])
            self.assertEqual(0, init_r1["unexaminedLessons"])
            all_five = list(seen_batches)
            self.assertEqual(5, len(set(all_five)))

            # Plain run now skips all 10 lessons.
            seen_batches.clear()
            skip_r = mdtidy.dream_tidy_run(repo, via=("claude",), max_batches=2, runner=runner)
            self.assertEqual(0, skip_r["calls"])
            self.assertEqual(10, skip_r["alreadyExaminedLessons"])

            # Three `--again`-then-plain runs with `max_batches=2` examine every batch once.
            seen_batches.clear()
            a1 = mdtidy.dream_tidy_run(repo, via=("claude",), max_batches=2, again=True, runner=runner)
            self.assertEqual(2, a1["calls"])
            self.assertEqual(0, a1["alreadyExaminedLessons"])
            self.assertEqual(6, a1["unexaminedLessons"])
            self.assertIn(v.DREAM_TIDY_UNEXAMINED_NOTICE % 6, a1["notices"])

            p2 = mdtidy.dream_tidy_run(repo, via=("claude",), max_batches=2, again=False, runner=runner)
            self.assertEqual(2, p2["calls"])
            self.assertEqual(4, p2["alreadyExaminedLessons"])
            self.assertEqual(2, p2["unexaminedLessons"])
            self.assertIn(v.DREAM_TIDY_UNEXAMINED_NOTICE % 2, p2["notices"])

            p3 = mdtidy.dream_tidy_run(repo, via=("claude",), max_batches=2, again=False, runner=runner)
            self.assertEqual(1, p3["calls"])
            self.assertEqual(8, p3["alreadyExaminedLessons"])
            self.assertEqual(0, p3["unexaminedLessons"])
            self.assertEqual(all_five, seen_batches)

            # Three repeated `--again` runs with `max_batches=2` ALSO walk all 5 batches once.
            seen_batches.clear()
            ra1 = mdtidy.dream_tidy_run(repo, via=("claude",), max_batches=2, again=True, runner=runner)
            ra2 = mdtidy.dream_tidy_run(repo, via=("claude",), max_batches=2, again=True, runner=runner)
            ra3 = mdtidy.dream_tidy_run(repo, via=("claude",), max_batches=2, again=True, runner=runner)
            self.assertEqual([2, 2, 1], [ra1["calls"], ra2["calls"], ra3["calls"]])
            self.assertEqual([6, 2, 0], [ra1["unexaminedLessons"], ra2["unexaminedLessons"], ra3["unexaminedLessons"]])
            self.assertEqual(all_five, seen_batches)

            after_plain = mdtidy.dream_tidy_run(repo, via=("claude",), max_batches=2, again=False, runner=runner)
            self.assertEqual(0, after_plain["calls"])
            self.assertEqual(10, after_plain["alreadyExaminedLessons"])
        _ = ids

    # ------------------------------------------------------------------
    # MUST 2: B/M-2 (nothing changes before the first run)
    # ------------------------------------------------------------------
    def test_must_b_m2_section_absent_without_regular_proposals_file(self) -> None:
        repo = self.make_repo("m2_repo")
        self.learn_line(repo, "m2.log", "2026-11-01T09:00:00Z [user] Never skip idempotency check in src/billing.py.")
        c_base_j, base_json, _ = _run_inproc_cli(["admin", "tidy", "--repo", str(repo), "--format", "json"])
        c_base_m, base_md, _ = _run_inproc_cli(["admin", "tidy", "--repo", str(repo), "--format", "markdown"])
        self.assertEqual((0, 0), (c_base_j, c_base_m))
        self.assertNotIn(v.TIDY_MODEL_SAME_SECTION, json.loads(base_json))

        runs_path = repo / dc.RUNS_REL
        target = repo / mdtidy.PROPOSALS_REL
        ext_empty = self.base / "ext_empty_runs"
        ext_empty.mkdir()

        # 1) symlink at .memlet/dream/runs -> empty dir
        shutil.rmtree(runs_path, ignore_errors=True)
        runs_path.parent.mkdir(parents=True, exist_ok=True)
        runs_path.symlink_to(ext_empty, target_is_directory=True)
        _, j1, _ = _run_inproc_cli(["admin", "tidy", "--repo", str(repo), "--format", "json"])
        _, m1, _ = _run_inproc_cli(["admin", "tidy", "--repo", str(repo), "--format", "markdown"])
        self.assertEqual(base_json, j1)
        self.assertEqual(base_md, m1)
        runs_path.unlink()

        # 2) regular file at .memlet/dream/runs (not a directory, no proposals file behind it)
        runs_path.write_text("not-a-dir\n", encoding="utf-8")
        _, j2, _ = _run_inproc_cli(["admin", "tidy", "--repo", str(repo), "--format", "json"])
        _, m2, _ = _run_inproc_cli(["admin", "tidy", "--repo", str(repo), "--format", "markdown"])
        self.assertEqual(base_json, j2)
        self.assertEqual(base_md, m2)
        runs_path.unlink()

        # 3) directory at tidy-proposals.json
        runs_path.mkdir(parents=True, exist_ok=True)
        target.mkdir()
        _, j3, _ = _run_inproc_cli(["admin", "tidy", "--repo", str(repo), "--format", "json"])
        _, m3, _ = _run_inproc_cli(["admin", "tidy", "--repo", str(repo), "--format", "markdown"])
        self.assertEqual(base_json, j3)
        self.assertEqual(base_md, m3)
        target.rmdir()

        # 4) dangling symlink at tidy-proposals.json
        target.symlink_to(self.base / "nonexistent_proposals.json")
        _, j4, _ = _run_inproc_cli(["admin", "tidy", "--repo", str(repo), "--format", "json"])
        _, m4, _ = _run_inproc_cli(["admin", "tidy", "--repo", str(repo), "--format", "markdown"])
        self.assertEqual(base_json, j4)
        self.assertEqual(base_md, m4)
        target.unlink()

        # 5) regular invalid proposals file -> notice DOES appear
        target.write_text("{corrupt", encoding="utf-8")
        _, j5, _ = _run_inproc_cli(["admin", "tidy", "--repo", str(repo), "--format", "json"])
        parsed5 = json.loads(j5)
        self.assertEqual([], parsed5[v.TIDY_MODEL_SAME_SECTION])
        self.assertIn(v.TIDY_MODEL_PROPOSALS_UNREADABLE, parsed5["notices"])

    # ------------------------------------------------------------------
    # MUST 3: C/MF-1 (terminal vs pipe for all 5 plug-in admin commands)
    # ------------------------------------------------------------------
    def test_must_c_mf1_plugin_admin_commands_pty_vs_pipe(self) -> None:
        repo = self.make_repo("mf1_repo", git=True)
        lid = self.learn_line(repo, "mf1.log", "2026-11-01T09:00:00Z [user] Never skip settle check in src/billing.py.")
        self.assertTrue(mb.learn_approve(repo, [lid], approver="Robin")["ok"])

        commands = [
            ["admin", "tidy", "--repo", str(repo)],
            ["admin", "skillbook", "--repo", str(repo)],
            ["admin", "graph", "show", "src/billing.py", "--repo", str(repo)],
            ["admin", "anchors", "suggest", "--repo", str(repo)],
            ["admin", "eval", "selftest"],
        ]

        def run_pty(argv: Sequence[str]) -> Tuple[int, str]:
            master_fd, slave_fd = pty.openpty()
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(CODE_ROOT / "memory_cli.py"), *argv],
                    stdin=subprocess.DEVNULL,
                    stdout=slave_fd,
                    stderr=subprocess.PIPE,
                    cwd=str(repo),
                    env=self.child_env(),
                )
            finally:
                os.close(slave_fd)
            chunks: List[bytes] = []
            try:
                while True:
                    rlist, _, _ = select.select([master_fd], [], [], 5.0)
                    if not rlist:
                        if proc.poll() is not None:
                            break
                        continue
                    try:
                        data = os.read(master_fd, 65536)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                    if not data:
                        break
                    chunks.append(data)
            finally:
                os.close(master_fd)
            _, err_bytes = proc.communicate(timeout=30)
            self.assertEqual(b"", err_bytes)
            return proc.returncode, b"".join(chunks).decode("utf-8", errors="replace").replace("\r\n", "\n")

        for argv in commands:
            with self.subTest(cmd=argv):
                # Piped without --format -> JSON
                piped = subprocess.run(
                    [sys.executable, str(CODE_ROOT / "memory_cli.py"), *argv],
                    capture_output=True,
                    text=True,
                    cwd=str(repo),
                    env=self.child_env(),
                )
                self.assertEqual(0, piped.returncode, piped.stderr)
                parsed_json = json.loads(piped.stdout)
                self.assertTrue(parsed_json.get("ok"), parsed_json)

                # Real PTY without --format -> human text rendering, not JSON
                rc_pty, out_pty = run_pty(argv)
                self.assertEqual(0, rc_pty, out_pty)
                self.assertFalse(out_pty.lstrip().startswith("{"), f"Expected text on PTY for {argv}, got JSON:\n{out_pty[:300]}")
                piped_md = subprocess.run(
                    [sys.executable, str(CODE_ROOT / "memory_cli.py"), *argv, "--format", "markdown"],
                    capture_output=True,
                    text=True,
                    cwd=str(repo),
                    env=self.child_env(),
                )
                self.assertEqual(piped_md.stdout, out_pty)

                # Real PTY WITH explicit --format json -> JSON
                rc_pty_j, out_pty_j = run_pty([*argv, "--format", "json"])
                self.assertEqual(0, rc_pty_j)
                self.assertEqual(parsed_json, json.loads(out_pty_j))

    # ------------------------------------------------------------------
    # MUST 4: C/MF-2 (attach-files label in both sections; Or: for alternatives)
    # ------------------------------------------------------------------
    def test_must_c_mf2_attach_files_label_and_or_alternatives(self) -> None:
        repo = self.make_repo("mf2_repo")
        # Wording duplicate pair: kept approved lesson reaches src/billing.py; pending member also reaches src/extra.py
        w_keep = self.learn_line(
            repo, "mf2_wk.log", "2026-11-01T08:00:00Z [user] Never skip idempotency guard when settling orders."
        )
        w_mem = self.learn_line(
            repo, "mf2_wm.log", "2026-11-01T08:05:00Z [user] Never skip idempotency guard when settling orders."
        )
        self.assertTrue(mb.learn_approve(repo, [w_keep], approver="Robin", paths=["src/billing.py"])["ok"])
        self.assertTrue(mb.learn_approve(repo, [w_mem], approver="Robin", paths=["src/billing.py", "src/extra.py"])["ok"])

        # Model proposal pair with lost file on member
        m_keep = self.learn_line(
            repo, "mf2_mk.log", "2026-11-01T09:00:00Z [user] Never run raw DDL statements without migration locks."
        )
        m_mem = self.learn_line(
            repo, "mf2_mm.log", "2026-11-01T09:05:00Z [user] Do not execute schema changes outside the migration lock."
        )
        self.assertTrue(mb.learn_approve(repo, [m_keep], approver="Robin", paths=["src/schema.py"])["ok"])
        self.assertTrue(mb.learn_approve(repo, [m_mem], approver="Robin", paths=["src/schema.py", "src/extra.py"])["ok"])

        # Pending lesson said twice so `saidAgain` has true approve / reject alternatives
        s_pend = self.learn_line(
            repo, "mf2_s1.log", "2026-11-01T10:00:00Z [user] Never deploy src/billing.py on Friday evening."
        )
        src2 = self.base / "mf2_s2.log"
        src2.write_text("2026-11-02T10:00:00Z [user] Never deploy src/billing.py on Friday evening.\n", encoding="utf-8")
        self.assertTrue(dc.dream_ingest(repo, src2)["ok"])

        def runner(_argv: Sequence[str], _prompt: str, *, stdin_text: str = "", **_kw: Any) -> str:
            rows = _rows_from_stdin(stdin_text)
            r1 = next(r["id"] for r in rows if "raw DDL" in r["text"])
            r2 = next(r["id"] for r in rows if "execute schema changes" in r["text"])
            return json.dumps([{"same": [r1, r2]}])

        run_res = mdtidy.dream_tidy_run(repo, via=("claude",), runner=runner)
        self.assertTrue(run_res["ok"], run_res)

        rep = mt.tidy_report(repo)
        self.assertEqual(1, len(rep["duplicates"]))
        self.assertEqual(1, len(rep["modelSaysSame"]))
        self.assertIn("pathsCommand", rep["duplicates"][0])
        self.assertIn("pathsCommand", rep["modelSaysSame"][0])

        md_text = rep["formatted"]
        self.assertEqual(2, md_text.count("  " + v.MERGE_ATTACH_LABEL))
        for line in md_text.splitlines():
            if "--paths" in line:
                self.assertIn(v.MERGE_ATTACH_LABEL, line)
                self.assertFalse(line.lstrip().startswith("Or:"), line)
        # True approve/reject alternatives still use `Or: `
        self.assertTrue(any(ln.startswith("  Or: ") and "--reject" in ln for ln in md_text.splitlines()), md_text)
        _ = s_pend

    # ------------------------------------------------------------------
    # MUST 5: C/MF-3 (reasons are sentences; zero internal function names)
    # ------------------------------------------------------------------
    def test_must_c_mf3_sentences_for_merge_blocked_and_status_with_no_internal_names(self) -> None:
        repo = self.make_repo("mf3_repo")
        # Create two approved lessons with mutually incompatible approved relation types:
        # L_blocks has approved BLOCKS; L_resolves has approved RESOLVES -> neither can keep the other (`relation-types`)!
        l_blocks = self._add_real_dream_lesson(
            repo,
            "mf3_blocks.log",
            "2026-11-01T08:00:00Z [user] Never settle an unverified invoice in src/billing.py.",
            category="repeated_pitfall",
            edges=[{"edgeType": "BLOCKS", "target": "src/billing.py"}],
        )
        l_resolves = self._add_real_dream_lesson(
            repo,
            "mf3_resolves.log",
            "2026-11-01T08:05:00Z [user] Reject unverified invoices before calling settle in src/billing.py.",
            category="verified_workaround",
            edges=[{"edgeType": "RESOLVES", "target": "src/billing.py"}],
        )
        self.assertTrue(mb.learn_approve(repo, [l_blocks, l_resolves], approver="Robin")["ok"])

        # Also create pairs for open, merged, stale, dismissed states in `--status`
        o1 = self.learn_line(repo, "mf3_o1.log", "2026-11-01T09:00:00Z [user] Never bypass schema lock in src/schema.py.")
        o2 = self.learn_line(repo, "mf3_o2.log", "2026-11-01T09:01:00Z [user] Do not run migrations in src/schema.py unlocked.")
        m1 = self.learn_line(repo, "mf3_m1.log", "2026-11-01T09:10:00Z [user] Never mutate helper state in src/extra.py.")
        m2 = self.learn_line(repo, "mf3_m2.log", "2026-11-01T09:11:00Z [user] Never make helper in src/extra.py stateful.")
        st1 = self.learn_line(repo, "mf3_st1.log", "2026-11-01T09:20:00Z [user] Never hardcode billing currency in src/billing.py.")
        st2 = self.learn_line(repo, "mf3_st2.log", "2026-11-01T09:21:00Z [user] Never omit explicit currency code in src/billing.py.")
        dm1 = self.learn_line(repo, "mf3_dm1.log", "2026-11-01T09:30:00Z [user] Never ignore migration exit codes in src/schema.py.")
        dm2 = self.learn_line(repo, "mf3_dm2.log", "2026-11-01T09:31:00Z [user] Never skip checking every return code in src/schema.py.")

        def runner(_argv: Sequence[str], _prompt: str, *, stdin_text: str = "", **_kw: Any) -> str:
            rows = _rows_from_stdin(stdin_text)
            def rid(sub: str) -> str:
                return next(r["id"] for r in rows if sub in r["text"])
            return json.dumps([
                {"same": [rid("unverified invoice"), rid("unverified invoices")]},
                {"same": [rid("bypass schema lock"), rid("migrations in src/schema.py unlocked")]},
                {"same": [rid("mutate helper state"), rid("src/extra.py stateful")]},
                {"same": [rid("hardcode billing currency"), rid("explicit currency code")]},
                {"same": [rid("migration exit codes"), rid("every return code")]},
            ])

        run_res = mdtidy.dream_tidy_run(repo, via=("claude",), runner=runner)
        self.assertTrue(run_res["ok"], run_res)
        self.assertEqual(1, len(run_res["droppedGroups"]))
        dg = run_res["droppedGroups"][0]
        self.assertEqual("merge-blocked", dg["reason"])
        self.assertNotIn("merge_blockers", dg["reasonMeaning"])
        self.assertIn("learn --merge", dg["reasonMeaning"])
        self.assertIn("blockerMessages", dg)
        self.assertTrue(
            any("relation-types — " + v.MERGE_REASONS["relation-types"] in msgs for msgs in dg["blockerMessages"].values()),
            dg,
        )
        run_md = cli_dream._markdown(run_res)
        self.assertIn("relation-types — " + v.MERGE_REASONS["relation-types"], run_md)

        # Transition proposals into merged, stale, dismissed
        st_before = mdtidy.dream_tidy_status(repo)
        pid_by_member = {lid: p["id"] for p in st_before["proposals"] for lid in p["lessonIds"]}
        self.assertTrue(mdtidy.dream_tidy_dismiss(repo, [pid_by_member[dm1]])["ok"])
        self.assertTrue(mb.learn_merge(repo, m1, [m2], approver="Robin")["ok"])
        self.assertTrue(mb.learn_reject(repo, [st2], approver="Robin", reason="obsolete")["ok"])

        st_after = mdtidy.dream_tidy_status(repo)
        st_md = cli_dream._markdown(st_after)
        for code in ("already-merged", "lesson-not-shown", "dismissed"):
            self.assertEqual(v.DREAM_TIDY_STATE_REASONS[code], st_after["reasonMeanings"][code])
            self.assertIn(code + " — " + v.DREAM_TIDY_STATE_REASONS[code], st_md)
        for p in st_after["proposals"]:
            if p["reason"]:
                self.assertEqual(v.DREAM_TIDY_STATE_REASONS[p["reason"]], p["reasonMeaning"])

        tidy_rep = mt.tidy_report(repo)
        forbidden_names = (
            "merge_blockers",
            "kept_lesson_order",
            "_checked_blockers",
            "_choose_kept_lesson",
            "proposal_state",
            "_duplicates",
            "merge_view",
        )
        all_outputs = "\n".join([
            json.dumps(run_res),
            run_md,
            json.dumps(st_after),
            st_md,
            json.dumps(tidy_rep),
            tidy_rep["formatted"],
        ])
        for fn_name in forbidden_names:
            self.assertNotIn(fn_name, all_outputs)
        _ = (o1, o2, st1, dm2)

    # ------------------------------------------------------------------
    # MUST 6: C/MF-4 (tiers shown, row rules, unmerge reopen, 9.3.0 skill check)
    # ------------------------------------------------------------------
    def test_must_c_mf4_tiers_row_rules_unmerge_and_skillbook_930_check(self) -> None:
        repo = self.make_repo("mf4_repo", git=True)
        id_app = self.learn_line(repo, "mf4_app.log", "2026-11-01T08:00:00Z [user] Never skip settle lock in src/billing.py.")
        self.assertTrue(mb.learn_approve(repo, [id_app], approver="Robin")["ok"])
        id_hint = self._add_real_dream_lesson(
            repo,
            "mf4_hint.log",
            "2026-11-01T08:05:00Z [user] Avoid calling settle in src/billing.py without acquiring the billing lock.",
            category="repeated_pitfall",
            verify_verdict="SUPPORTED",
        )
        self.assertIn(id_hint, [it["id"] for it in mb.dream_items(repo, tiers=("hint",))["items"]])
        id_pend = self.learn_line(repo, "mf4_pend.log", "2026-11-01T08:10:00Z [user] Never run src/schema.py migrate unlocked.")
        id_rej = self.learn_line(repo, "mf4_rej.log", "2026-11-01T08:15:00Z [user] Never use rejected sentinel text in src/extra.py.")
        self.assertTrue(mb.learn_reject(repo, [id_rej], approver="Robin", reason="bad")["ok"])
        id_mem = self.learn_line(repo, "mf4_mem.log", "2026-11-01T08:20:00Z [user] Never use merged member sentinel text in src/billing.py.")
        self.assertTrue(mb.learn_merge(repo, id_app, [id_mem], approver="Robin")["ok"])

        captured_stdin: List[str] = []

        def runner(_argv: Sequence[str], _prompt: str, *, stdin_text: str = "", **_kw: Any) -> str:
            captured_stdin.append(stdin_text)
            # Group 1 repeats L1 inside one group -> counts once -> 2 distinct rows -> ACCEPTED!
            # Group 2 uses L3 alone -> fewer-than-two; Group 3 names L99 -> unknown-row-id.
            return json.dumps([
                {"same": ["L1", "L1", "L2"]},
                {"same": ["L3", "L3"]},
                {"same": ["L3", "L99"]},
            ])

        res = mdtidy.dream_tidy_run(repo, via=("claude",), runner=runner)
        self.assertTrue(res["ok"], res)
        self.assertEqual(1, len(captured_stdin))
        sent = captured_stdin[0]
        self.assertIn("settle lock", sent)         # approved
        self.assertIn("acquiring the billing", sent)  # hint
        self.assertIn("migrate unlocked", sent)    # pending
        self.assertNotIn("Rejected sentinel", sent)
        self.assertNotIn("Merged member sentinel", sent)
        self.assertEqual(1, len(res["addedProposals"]))
        self.assertEqual(["fewer-than-two", "unknown-row-id"], [d["reason"] for d in res["droppedGroups"]])

        # Unmerge reopens non-dismissed proposal; dismissed proposal stays dismissed across merge + unmerge
        pid = res["addedProposals"][0]
        self.assertTrue(mb.learn_merge(repo, id_app, [id_hint], approver="Robin")["ok"])
        self.assertEqual("merged", mdtidy.dream_tidy_status(repo)["proposals"][0]["state"])
        self.assertTrue(mb.learn_unmerge(repo, [id_hint], approver="Robin")["ok"])
        self.assertEqual("open", mdtidy.dream_tidy_status(repo)["proposals"][0]["state"])

        self.assertTrue(mdtidy.dream_tidy_dismiss(repo, [pid])["ok"])
        self.assertTrue(mb.learn_merge(repo, id_app, [id_hint], approver="Robin")["ok"])
        self.assertEqual("merged", mdtidy.dream_tidy_status(repo)["proposals"][0]["state"])
        self.assertTrue(mb.learn_unmerge(repo, [id_hint], approver="Robin")["ok"])
        self.assertEqual("dismissed", mdtidy.dream_tidy_status(repo)["proposals"][0]["state"])

        # `admin skillbook --check` on a skill file written by 9.3.0 answers "stale: written by Memlet 9.3.0" (exit 1)
        out_dir = repo / ".claude/skills"
        with mock.patch.object(sb, "VERSION", "9.3.0"):
            w_res = sb.write_skill(repo, out=out_dir, force=True)
        self.assertTrue(w_res["ok"], w_res)
        skill_file = Path(w_res["path"])
        c_chk, o_chk, _ = _run_inproc_cli(["admin", "skillbook", "--repo", str(repo), "--check", str(skill_file), "--format", "markdown"])
        self.assertEqual(1, c_chk)
        self.assertEqual("stale: written by Memlet 9.3.0\n", o_chk)
        self.assertTrue(sb.write_skill(repo, out=out_dir, force=True)["ok"])
        c_chk2, _, _ = _run_inproc_cli(["admin", "skillbook", "--repo", str(repo), "--check", str(skill_file), "--format", "markdown"])
        self.assertEqual(0, c_chk2)

        # Verify README.md and `dream --help` truthfully match all four MF-4 clauses
        readme_text = (CODE_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("approved, hint and pending lessons, never rejected ones or merged members (current lines only)", readme_text)
        self.assertIn("a row repeated inside one group counts once", readme_text)
        self.assertIn("once every lesson in it is shown separately and it was not dismissed", readme_text)
        self.assertIn("stale: written by Memlet 9.3.0", readme_text)
        _, help_out, _ = _run_inproc_cli(["dream", "--help"])
        self.assertIn("With --tidy:", help_out)
        _ = id_pend

    # ------------------------------------------------------------------
    # Should / Note items across Sections B & C
    # ------------------------------------------------------------------
    def test_b_and_c_should_and_note_items_end_to_end(self) -> None:
        # 1) B/S-2 & C/SF-4: partly successful run markdown, failedBatchLessons vs unexaminedLessons, next-step, proposal ids
        repo = self.make_repo("s2_sf4_repo")
        for i in range(6):
            self.learn_line(
                repo, f"s2_{i}.log", f"2026-11-01T09:{i:02d}:00Z [user] Never skip billing guard #{i} in src/billing.py."
            )
        call_n = {"n": 0}

        def partial_runner(_argv: Sequence[str], _prompt: str, *, stdin_text: str = "", **_kw: Any) -> str:
            call_n["n"] += 1
            if call_n["n"] == 1:
                return json.dumps([{"same": ["L1", "L2"]}])
            return "not valid json"

        with mock.patch.object(mdtidy, "MAX_BATCH_LESSONS", 2):
            part = mdtidy.dream_tidy_run(repo, via=("claude",), max_batches=2, runner=partial_runner)
            self.assertFalse(part["ok"])
        self.assertEqual(2, part["batches"])
        self.assertEqual(1, part["failedBatches"])
        self.assertEqual(2, part["failedBatchLessons"])   # batch 2 failed (2 lessons)
        self.assertEqual(2, part["unexaminedLessons"])    # batch 3 unreached because of max_batches=2 (2 lessons)
        self.assertIn(v.DREAM_TIDY_FAILED_BATCH_NOTICE % 2, part["notices"])
        self.assertIn(v.DREAM_TIDY_UNEXAMINED_NOTICE % 2, part["notices"])
        part_md = cli_dream._markdown(part)
        self.assertIn("failed batches: 1 (will be retried on the next run)", part_md)
        self.assertIn(part["addedProposals"][0], part_md)
        self.assertIn(v.DREAM_TIDY_TRUST_NOTICE, part_md)
        self.assertIn(v.DREAM_TIDY_NEXT_STEP, part_md)

        # C/SF-4: `--status` shows how many shown lessons have never been examined (4 of 6 after batch 1 succeeded)
        with mock.patch.object(mdtidy, "MAX_BATCH_LESSONS", 2):
            st_part = mdtidy.dream_tidy_status(repo)
        self.assertEqual(6, st_part["shownLessons"])
        self.assertEqual(4, st_part["unexaminedLessons"])
        self.assertIn("Shown lessons never examined: 4 (of 6)", cli_dream._markdown(st_part))

        # 2) C/SF-4: 1-lesson batch makes no model call and counts `nothingToCompare`
        solo_repo = self.make_repo("solo_repo")
        self.learn_line(solo_repo, "solo.log", "2026-11-01T09:00:00Z [user] Never skip schema lock in src/schema.py.")
        solo_res = mdtidy.dream_tidy_run(
            solo_repo,
            via=("claude",),
            runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("1-lesson batch must not call model")),
        )
        self.assertTrue(solo_res["ok"], solo_res)
        self.assertEqual(0, solo_res["calls"])
        self.assertEqual(1, solo_res["nothingToCompare"])
        self.assertIn(v.DREAM_TIDY_NOTHING_TO_COMPARE_NOTICE % 1, solo_res["notices"])

        # 3) B/S-3: dropped-group records keep newest 100 (evict oldest), and re-asking a batch replaces its old records
        drop_target: Dict[str, Any] = {}
        mdtidy._keep_dropped(
            drop_target,
            [mdtidy._dropped_record(f"b{i:023d}"[-24:], (i % 30) + 1, "unknown-row-id", []) for i in range(105)],
        )
        self.assertEqual(100, len(drop_target["droppedGroups"]))
        self.assertEqual(5, drop_target["droppedGroupsOmitted"])
        self.assertEqual(f"b{5:023d}"[-24:], drop_target["droppedGroups"][0]["batchId"])
        self.assertEqual(f"b{104:023d}"[-24:], drop_target["droppedGroups"][-1]["batchId"])

        rep_b_repo = self.make_repo("replace_batch_dropped")
        self.learn_line(rep_b_repo, "rb1.log", "2026-11-01T09:00:00Z [user] Never skip check A in src/billing.py.")
        self.learn_line(rep_b_repo, "rb2.log", "2026-11-01T09:01:00Z [user] Never skip check B in src/billing.py.")
        r_d1 = mdtidy.dream_tidy_run(rep_b_repo, via=("claude",), runner=lambda *a, **k: json.dumps([{"same": ["L1", "L9"]}, {"same": ["L1", "L2"]}]))
        self.assertEqual(1, len(r_d1["droppedGroups"]))
        r_d2 = mdtidy.dream_tidy_run(rep_b_repo, via=("claude",), again=True, runner=lambda *a, **k: "[]")
        self.assertEqual([], r_d2["droppedGroups"])
        self.assertEqual([], mdtidy.dream_tidy_status(rep_b_repo)["droppedGroups"])

        # 4) B/N-2 & C/SF-6:
        #    - First test SF-6 candidate rescue: earlier approved lesson has RESOLVES only; later approved lesson has
        #      BLOCKS + RESOLVES -> keeping candidate 2 succeeds with `keepReason == "the earlier ones cannot be kept: relation-types"`!
        sf6_repo = self.make_repo("sf6_repo")
        cand1 = self._add_real_dream_lesson(
            sf6_repo,
            "sf6_1.log",
            "2026-11-01T08:00:00Z [user] Settle retries in src/billing.py through the idempotency token.",
            category="verified_workaround",
            edges=[{"edgeType": "RESOLVES", "target": "src/billing.py"}],
        )
        cand2 = self._add_real_dream_lesson(
            sf6_repo,
            "sf6_2.log",
            "2026-11-01T09:00:00Z [user] Never settle retries in src/billing.py without the idempotency token.",
            category="repeated_pitfall",
            edges=[
                {"edgeType": "BLOCKS", "target": "src/billing.py"},
                {"edgeType": "RESOLVES", "target": "src/billing.py"},
            ],
        )
        self.assertTrue(mb.learn_approve(sf6_repo, [cand1, cand2], approver="Robin")["ok"])
        sf6_run = mdtidy.dream_tidy_run(
            sf6_repo, via=("claude",), runner=lambda *a, **k: json.dumps([{"same": ["L1", "L2"]}])
        )
        self.assertTrue(sf6_run["ok"], sf6_run)
        self.assertEqual(1, len(sf6_run["addedProposals"]))
        sf6_tidy = mt.tidy_report(sf6_repo)
        sf6_entry = sf6_tidy["modelSaysSame"][0]
        self.assertEqual(cand2, sf6_entry["keep"])
        self.assertEqual("the earlier ones cannot be kept: relation-types", sf6_entry["keepReason"])
        self.assertIsNotNone(sf6_entry["command"])
        self.assertIn("  Suggested because: the earlier ones cannot be kept: relation-types", sf6_tidy["formatted"])

        #    - Next test B/N-2: when `merge-blocked` drops a group (both lessons have merged members: `has-merged-members`),
        #      the batch is NOT marked examined, so once the person unmerges & rejects `n2_d` (unchanged batch examinedDigest!),
        #      a plain `dream --tidy` run re-asks the batch without `--again` and accepts the proposal!
        n2_repo = self.make_repo("n2_repo")
        n2_a = self.learn_line(n2_repo, "n2_a.log", "2026-11-01T08:00:00Z [user] Never charge unsettled orders in src/billing.py.")
        n2_b = self.learn_line(n2_repo, "n2_b.log", "2026-11-01T08:05:00Z [user] Do not bill unsettled orders in src/billing.py.")
        n2_c = self.learn_line(n2_repo, "n2_c.log", "2026-11-01T08:10:00Z [user] Never charge pending invoices in src/billing.py.")
        n2_d = self.learn_line(n2_repo, "n2_d.log", "2026-11-01T08:15:00Z [user] Never bill pending orders in src/billing.py.")
        self.assertTrue(mb.learn_approve(n2_repo, [n2_a, n2_b, n2_c, n2_d], approver="Robin")["ok"])
        self.assertTrue(mb.learn_merge(n2_repo, n2_a, [n2_c], approver="Robin")["ok"])
        self.assertTrue(mb.learn_merge(n2_repo, n2_b, [n2_d], approver="Robin")["ok"])

        n2_r1 = mdtidy.dream_tidy_run(
            n2_repo, via=("claude",), runner=lambda *a, **k: json.dumps([{"same": ["L1", "L2"]}])
        )
        self.assertEqual(1, n2_r1["calls"])
        self.assertEqual(["merge-blocked"], [d["reason"] for d in n2_r1["droppedGroups"]])

        # Person clears the blocker on n2_b via real learn_unmerge + learn_reject; shown batch [n2_a, n2_b] has identical digest!
        self.assertTrue(mb.learn_unmerge(n2_repo, [n2_d], approver="Robin")["ok"])
        self.assertTrue(mb.learn_reject(n2_repo, [n2_d], approver="Robin", reason="obsolete")["ok"])

        n2_r2 = mdtidy.dream_tidy_run(
            n2_repo, via=("claude",), runner=lambda *a, **k: json.dumps([{"same": ["L1", "L2"]}])
        )
        self.assertEqual(1, n2_r2["calls"])
        self.assertEqual(1, len(n2_r2["addedProposals"]))

        # 5) B/N-3, N-4, S-1 + C/SF-2, SF-5: agreement="model" without --verify-with runs; same-family message; help
        sf2_repo = self.make_repo("sf2_repo")
        self.learn_line(sf2_repo, "sf2_1.log", "2026-11-01T08:00:00Z [user] Never skip check one in src/billing.py.")
        self.learn_line(sf2_repo, "sf2_2.log", "2026-11-01T08:01:00Z [user] Never skip check two in src/billing.py.")
        cfg_sf2, _ = mb._load_config(sf2_repo)
        cfg_sf2["dream"] = {"agreement": "model", "models": {"claude": "claude-custom-unlisted"}}
        mw.write_managed_text(sf2_repo, mw.CONFIG_PATH, json.dumps(cfg_sf2, indent=2) + "\n")
        sf2_ok = mdtidy.dream_tidy_run(sf2_repo, via=("claude",), runner=lambda *a, **k: "[]")
        self.assertTrue(sf2_ok["ok"], sf2_ok)

        cfg_sf2["dream"] = {"agreement": "vendor", "models": {"claude": "sonnet"}}
        mw.write_managed_text(sf2_repo, mw.CONFIG_PATH, json.dumps(cfg_sf2, indent=2) + "\n")
        sf2_same = mdtidy.dream_tidy_run(sf2_repo, via=("claude",), verify_with="claude", runner=lambda *a, **k: "[]")
        self.assertFalse(sf2_same["ok"])
        self.assertNotIn("dream.agreement", sf2_same["error"])
        self.assertIn("dream --tidy --verify-with", sf2_same["error"])
        self.assertIn("default verifier", sf2_same["error"])

        # B/N-5: plain `dream` ALSO refuses `--save-fixture` inside `.memlet` BEFORE any call
        plain_inside = dc.dream_run(
            sf2_repo,
            via=("claude",),
            save_fixture=sf2_repo / ".memlet" / "cap",
            runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("plain dream must refuse .memlet pre-call")),
        )
        self.assertFalse(plain_inside["ok"])
        self.assertEqual(0, plain_inside["calls"])
        self.assertIn("fixture directory must be outside .memlet", plain_inside["errors"][0])

        # C/SF-5 & B/N-4 & B/S-1: `dream --help` checks
        c_help, help_txt, _ = _run_inproc_cli(["dream", "--help"])
        self.assertEqual(0, c_help)
        self.assertIn("Dream cycle:", help_txt)
        self.assertIn("With --tidy:", help_txt)
        self.assertIn("1..20, default 4; with --tidy 1..10, default 2", " ".join(help_txt.split()))
        for legacy_opt in ("--root", "--task", "--session", "--standalone"):
            self.assertNotIn(legacy_opt, help_txt)
        # Legacy task-store flags still parse without error when passed to `dream`
        c_leg, _, _ = _run_inproc_cli(["dream", str(sf2_repo), "--root", str(self.base / "store"), "--status", "--format", "json"])
        self.assertEqual(0, c_leg)
        parser = memory_cli._build_parser()
        sub = next(a for a in parser._actions if getattr(a, "choices", None) and "dream" in a.choices)
        for act in sub.choices["dream"]._actions:
            if act.help is memory_cli.argparse.SUPPRESS:
                continue
            self.assertTrue(act.help and act.help.strip(), f"Missing help on dream option {act.dest!r}")

        # 6) C/SF-1 & C/N-2: dismissing open (disputed) vs merged/stale (not recorded, exit 0), status summary & proposer
        sf1_repo = self.make_repo("sf1_repo")
        d_a1 = self.learn_line(sf1_repo, "d_a1.log", "2026-11-01T08:00:00Z [user] Never skip lock A in src/billing.py.")
        d_a2 = self.learn_line(sf1_repo, "d_a2.log", "2026-11-01T08:01:00Z [user] Never fail to acquire lock A in src/billing.py.")
        d_b1 = self.learn_line(sf1_repo, "d_b1.log", "2026-11-01T08:02:00Z [user] Never skip lock B in src/schema.py.")
        d_b2 = self.learn_line(sf1_repo, "d_b2.log", "2026-11-01T08:03:00Z [user] Never fail to acquire lock B in src/schema.py.")
        d_c1 = self.learn_line(sf1_repo, "d_c1.log", "2026-11-01T08:04:00Z [user] Never skip lock C in src/extra.py.")
        d_c2 = self.learn_line(sf1_repo, "d_c2.log", "2026-11-01T08:05:00Z [user] Never fail to acquire lock C in src/extra.py.")

        def sf1_runner(_argv: Sequence[str], prompt: str, *, stdin_text: str = "", **_kw: Any) -> str:
            if prompt == mdtidy.PROPOSE_PROMPT:
                rows = _rows_from_stdin(stdin_text)
                def rid(sub: str) -> str:
                    return next(r["id"] for r in rows if sub in r["text"])
                return json.dumps([
                    {"same": [rid("skip lock A"), rid("acquire lock A")]},
                    {"same": [rid("skip lock B"), rid("acquire lock B")]},
                    {"same": [rid("skip lock C"), rid("acquire lock C")]},
                ])
            return json.dumps({"verdict": "DIFFERENT"})

        sf1_run = mdtidy.dream_tidy_run(sf1_repo, via=("claude",), verify_with="claude", runner=sf1_runner)
        self.assertTrue(sf1_run["ok"], sf1_run)

        st_sf1 = mdtidy.dream_tidy_status(sf1_repo)
        st_sf1_md = cli_dream._markdown(st_sf1)
        self.assertIn("- Open: 3 (of them disputed: 3); stale: 0; merged: 0; dismissed: 0", st_sf1_md)
        self.assertEqual(3, st_sf1_md.count("proposer=claude/haiku"))

        pid_by_lid = {lid: p["id"] for p in st_sf1["proposals"] for lid in p["lessonIds"]}
        pid_open_disputed = pid_by_lid[d_a1]
        pid_merged = pid_by_lid[d_b1]
        pid_stale = pid_by_lid[d_c1]

        self.assertTrue(mb.learn_merge(sf1_repo, d_b1, [d_b2], approver="Robin")["ok"])
        self.assertTrue(mb.learn_reject(sf1_repo, [d_c2], approver="Robin", reason="retired")["ok"])

        proposals_file = sf1_repo / mdtidy.PROPOSALS_REL
        bytes_before = proposals_file.read_bytes()
        c_nd, o_nd, _ = _run_inproc_cli(["dream", str(sf1_repo), "--tidy", "--dismiss", pid_merged, pid_stale, "--format", "markdown"])
        self.assertEqual(0, c_nd)
        self.assertEqual(bytes_before, proposals_file.read_bytes())
        self.assertIn(f"- {pid_merged}: not dismissed: merged — {v.DREAM_TIDY_STATE_REASONS['already-merged']}", o_nd)
        self.assertIn(f"- {pid_stale}: not dismissed: stale — {v.DREAM_TIDY_STATE_REASONS['lesson-not-shown']}", o_nd)

        # Open disputed proposal CAN be dismissed
        c_od, o_od, _ = _run_inproc_cli(["dream", str(sf1_repo), "--tidy", "--dismiss", pid_open_disputed, "--format", "markdown"])
        self.assertEqual(0, c_od)
        self.assertIn(pid_open_disputed, o_od)
        _ = d_a2

    # ------------------------------------------------------------------
    # Compatibility: custom byte comparison against `0ca681f` with real hints
    # ------------------------------------------------------------------
    def test_compatibility_byte_comparison_with_930_on_fixture_with_real_hints(self) -> None:
        base_tree = self.base / f"base_{PUBLISHED_930}"
        base_tree.mkdir()
        archive = subprocess.run(
            ["git", "archive", PUBLISHED_930],
            cwd=str(REPO_ROOT),
            capture_output=True,
            env=self.child_env(),
        )
        if archive.returncode != 0:
            self.skipTest(f"9.3.0 byte comparison requires git commit {PUBLISHED_930}; skipped in export without history")
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
            for member in bundle.getmembers():
                dest = (base_tree / member.name).resolve()
                assert str(dest).startswith(str(base_tree.resolve()))
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    extracted = bundle.extractfile(member)
                    assert extracted is not None
                    dest.write_bytes(extracted.read())

        # Build a rich template repository with REAL verified hints, approved lessons, pending lessons,
        # a wording duplicate with `pathsCommand`, and a wording duplicate where candidate 2 rescues candidate 1!
        tmpl = self.make_repo("hint_template", git=True)
        hint_id = self._add_real_dream_lesson(
            tmpl,
            "real_hint.log",
            "2026-11-01T08:00:00Z [user] Check settlement idempotency key in src/billing.py before retrying timeouts.",
            category="repeated_pitfall",
            edges=[{"edgeType": "BLOCKS", "target": "src/billing.py"}],
            verify_verdict="SUPPORTED",
        )
        app_id = self.learn_line(
            tmpl, "app1.log", "2026-11-01T08:10:00Z [user] Never settle orders in src/billing.py without an audit row."
        )
        dup_a = self.learn_line(
            tmpl, "dupa.log", "2026-11-01T08:20:00Z [user] Never run migrate in src/schema.py without taking a backup."
        )
        dup_b = self.learn_line(
            tmpl, "dupb.log", "2026-11-01T08:25:00Z [user] Never run migrate in src/schema.py without taking a backup."
        )
        self.assertTrue(mb.learn_approve(tmpl, [app_id], approver="Robin", paths=["src/billing.py"])["ok"])
        self.assertTrue(mb.learn_approve(tmpl, [dup_a], approver="Robin", paths=["src/schema.py"])["ok"])
        self.assertTrue(mb.learn_approve(tmpl, [dup_b], approver="Robin", paths=["src/schema.py", "src/extra.py"])["ok"])
        self.assertTrue(mb.promote_approved(tmpl)["ok"])
        self.assertTrue(mw.build_handover(tmpl, write=True)["ok"])

        # Verify the hint is genuinely classified as `hint`!
        tiers = mb.dream_items(tmpl, tiers=("approved", "hint", "pending"))["items"]
        self.assertIn(hint_id, [it["id"] for it in tiers if it["tier"] == "hint"])

        probe_code = r"""
import json, subprocess, sys
from pathlib import Path
code_root = Path(sys.argv[1])
repo = Path(sys.argv[2])
sys.path.insert(0, str(code_root))
from memlet import handover_bridge as hb, memlet_bridge as mb, memlet_graph as mg, memlet_tidy as mt, memlet_wiki as mw

def cli(args):
    p = subprocess.run([sys.executable, str(code_root / "memory_cli.py"), *args], capture_output=True, text=True, cwd=str(repo))
    return {"rc": p.returncode, "out": p.stdout, "err": p.stderr}

hook_in = json.dumps({"session_id": "s-hint", "cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"})
hook_p = subprocess.run(
    [sys.executable, str(code_root / "memory_cli.py"), "admin", "hook", "session-start"],
    input=hook_in, capture_output=True, text=True, cwd=str(repo),
)

out = {
    "recall_md": cli(["recall", "billing idempotency", "--repo", str(repo), "--format", "markdown"]),
    "recall_json": cli(["recall", "billing idempotency", "--repo", str(repo), "--format", "json"]),
    "ur_hints": mb.unified_recall(repo, "billing idempotency", budget=800, include=("rules", "dream", "hints")),
    "pending_md": cli(["learn", "--pending", "--repo", str(repo), "--format", "markdown"]),
    "handover_md": cli(["handover", str(repo), "--format", "markdown"]),
    "graph_show_md": cli(["admin", "graph", "show", "src/billing.py", "--repo", str(repo), "--tiers", "approved,hint", "--format", "markdown"]),
    "graph_piped": cli(["admin", "graph", "stats", "--repo", str(repo)]),
    "anchors_piped": cli(["admin", "anchors", "suggest", "--repo", str(repo)]),
    "skillbook_piped": cli(["admin", "skillbook", "--repo", str(repo)]),
    "check_changed": cli(["check", str(repo), "--changed", "--format", "json"]),
    "hook_session_start": {"rc": hook_p.returncode, "out": hook_p.stdout, "err": hook_p.stderr},
    "tidy_piped": cli(["admin", "tidy", "--repo", str(repo)]),
    "tidy_md": cli(["admin", "tidy", "--repo", str(repo), "--format", "markdown"]),
}
sys.stdout.write(json.dumps(out))
"""
        runs: Dict[str, Any] = {}
        for label, tree in (("base", base_tree), ("head", REPO_ROOT)):
            repo_copy = self.base / f"run_{label}"
            shutil.copytree(tmpl, repo_copy)
            proc = subprocess.run(
                [sys.executable, "-c", probe_code, str(tree), str(repo_copy)],
                capture_output=True,
                text=True,
                env=self.child_env(),
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            raw = proc.stdout.replace(str(repo_copy), "<REPO>")
            if label == "head":
                raw = raw.replace(VERSION, "9.3.0")
            runs[label] = json.loads(raw)

        # Confirm real hints actually appeared in recall, unified_recall, learn --pending, and graph show!
        self.assertIn("Check settlement idempotency key", runs["head"]["recall_md"]["out"])
        self.assertIn(hint_id, runs["head"]["recall_json"]["out"])
        self.assertIn(hint_id, json.dumps(runs["head"]["ur_hints"]))
        self.assertIn(hint_id, runs["head"]["pending_md"]["out"])
        self.assertIn("Check settlement idempotency key", runs["head"]["graph_show_md"]["out"])

        # Every non-tidy output is 100% byte-identical to 9.3.0 (`0ca681f`)!
        for k in (
            "recall_md",
            "recall_json",
            "ur_hints",
            "pending_md",
            "handover_md",
            "graph_show_md",
            "graph_piped",
            "anchors_piped",
            "check_changed",
            "hook_session_start",
        ):
            self.assertEqual(runs["base"][k], runs["head"][k], k)

        sb_base = json.loads(runs["base"]["skillbook_piped"]["out"])
        sb_head = json.loads(runs["head"]["skillbook_piped"]["out"])
        self.assertEqual(
            runs["base"]["skillbook_piped"]["out"],
            runs["head"]["skillbook_piped"]["out"].replace(sb_head["digest"], sb_base["digest"]),
        )

        # Tidy differs ONLY by `To attach them to the kept lesson:` (MF-2); the kept lesson is explained only when it is not the usual choice (SF-6)
        t_base = json.loads(runs["base"]["tidy_piped"]["out"])
        t_head = json.loads(runs["head"]["tidy_piped"]["out"])
        for d in t_head["duplicates"]:
            self.assertNotIn("keepReason", d)  # the usual choice (approved, earliest) is not explained: 9.3.0 output
        self.assertNotIn("Suggested because", runs["head"]["tidy_md"]["out"])
        norm_md = runs["head"]["tidy_md"]["out"].replace("  " + v.MERGE_ATTACH_LABEL, "  Or: ")
        self.assertEqual(runs["base"]["tidy_md"]["out"], norm_md)
        t_head["formatted"] = norm_md
        self.assertEqual(t_base, t_head)


if __name__ == "__main__":
    unittest.main()
