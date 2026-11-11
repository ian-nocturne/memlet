"""Final safety round A: regressions for the reviewer's reproductions (A1-A7, N2), with real symlinks, real threads,
real FIFOs, subprocesses on both interpreters and timing limits. Stdlib unittest only; no network, no model call;
everything runs in scratch directories (HOME / TMPDIR of subprocesses point into scratch)."""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from memlet import redaction
from memlet import audit
from memlet import code_anchors as ca
from memlet import memlet_bridge as mb
from memlet import memlet_brief as mbr
from memlet import memlet_cortex as mc
from memlet import memlet_dream as md
from memlet import memlet_inspector as mi
from memlet import memlet_wiki as mw

ROOT = Path(__file__).resolve().parent
CANARY = "PRIVATE_ACQUISITION_PLAN_7Z9"
OTHER_PY = "/usr/bin/python3"
# Generous limits that still separate the fix from the reported behaviour (every reviewer input ran > 3 s before).
FAST = 1.5


def _tmp(test, prefix):
    d = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    test.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
    return d


def _git(repo, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null", GIT_AUTHOR_NAME="T",
               GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@example.invalid")
    subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, timeout=30, check=True, env=env)


def _transcript(path, *texts):
    path.write_text("\n".join(json.dumps({"type": "user", "message": {"role": "user", "content": t}})
                              for t in texts) + "\n", encoding="utf-8")
    return path


def _tree(p):
    return sorted(str(x.relative_to(p)) for x in p.rglob("*"))


class _Repo(unittest.TestCase):
    def make_repo(self, agents="# Rules\n\n- Keep `app.py::main` argument-free.\n", init=True, git=True):
        repo = _tmp(self, "final-a-repo-")
        (repo / "AGENTS.md").write_text(agents, encoding="utf-8")
        (repo / "app.py").write_text("def main():\n    return 1\n\n\ndef helper(x):\n    return x\n", encoding="utf-8")
        if git:
            _git(repo, "init", "-q")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "fixture")
        if init:
            self.assertTrue(mw.init_project(repo)["ok"])
        return repo

    def outside(self, name="outside"):
        return _tmp(self, f"final-a-{name}-")


# ---------------------------------------------------------------------------------------------------------------
# A1: managed writes never follow a symlink out of the repository


class A1ManagedWriteTests(_Repo):
    def test_init_without_force_refuses_a_config_leaf_symlink(self):
        repo = self.make_repo(init=False)
        victim = self.outside() / "victim.json"
        victim.write_text("{}")
        (repo / ".memlet").mkdir()
        (repo / mw.CONFIG_PATH).symlink_to(victim)
        res = mw.init_project(repo)
        self.assertFalse(res["ok"])
        self.assertEqual(2, res["exitCode"])
        self.assertIn("symlink", res["error"])
        self.assertEqual("{}", victim.read_text())
        self.assertEqual(["config.json"], _tree(repo / ".memlet"))  # only the pre-existing link
        self.assertFalse((repo / mw.WIKI_DIR).exists())
        self.assertFalse((repo / mw.LEDGER_PATH).exists())

    def test_init_force_refuses_an_index_leaf_symlink(self):
        repo = self.make_repo(init=False)
        victim = self.outside() / "INDEX.md"
        victim.write_text("KEEP OUTSIDE\n")
        (repo / mw.WIKI_DIR).mkdir(parents=True)
        (repo / mw.INDEX_PATH).symlink_to(victim)
        res = mw.init_project(repo, force=True)
        self.assertFalse(res["ok"])
        self.assertEqual("KEEP OUTSIDE\n", victim.read_text())
        self.assertFalse((repo / mw.CONFIG_PATH).exists())

    def test_init_refuses_a_symlinked_memlet_directory(self):
        repo = self.make_repo(init=False)
        out = self.outside()
        (repo / ".memlet").symlink_to(out, target_is_directory=True)
        res = mw.init_project(repo)
        self.assertFalse(res["ok"])
        self.assertEqual([], _tree(out))

    def test_init_refuses_a_symlinked_ledger(self):
        repo = self.make_repo(init=False)
        victim = self.outside() / "ledger.sqlite3"
        victim.write_bytes(b"KEEP")
        (repo / ".memlet").mkdir()
        (repo / mw.LEDGER_PATH).symlink_to(victim)
        res = mw.init_project(repo)
        self.assertFalse(res["ok"])
        self.assertEqual(b"KEEP", victim.read_bytes())
        self.assertFalse((repo / mw.CONFIG_PATH).exists())

    def test_wiki_import_repin_accept_refuse_unsafe_paths(self):
        repo = self.make_repo()
        victim = self.outside() / "victim.md"
        victim.write_text("KEEP\n")
        (repo / mw.INDEX_PATH).unlink()
        (repo / mw.INDEX_PATH).symlink_to(victim)
        before = (repo / mw.CONFIG_PATH).read_bytes()
        for res in (mw.wiki_status(repo), mw.import_rules(repo), mw.repin_legacy_ts_anchors(repo)):
            self.assertFalse(res["ok"], res)
            self.assertEqual(2, res["exitCode"])
        self.assertEqual("KEEP\n", victim.read_text())
        self.assertEqual(before, (repo / mw.CONFIG_PATH).read_bytes())
        # accept refuses a symlinked config
        cfg_victim = self.outside() / "cfg.json"
        cfg_victim.write_text("{}")
        (repo / mw.INDEX_PATH).unlink()
        (repo / mw.CONFIG_PATH).unlink()
        (repo / mw.CONFIG_PATH).symlink_to(cfg_victim)
        res = mb.accept_anchor(repo, ["app.py::main"])
        self.assertFalse(res["ok"])
        self.assertEqual("{}", cfg_victim.read_text())

    def test_handover_write_refuses_a_symlinked_page(self):
        repo = self.make_repo()
        victim = self.outside() / "HANDOVER.md"
        victim.write_text("KEEP\n")
        (repo / mw.HANDOVER_PAGE).symlink_to(victim)
        res = mw.build_handover(repo, write=True)
        self.assertFalse(res["ok"])
        self.assertFalse(res["written"])
        self.assertEqual("KEEP\n", victim.read_text())

    def test_managed_writer_refuses_leaf_and_parent_links_and_writes_atomically(self):
        repo = self.make_repo()
        victim = self.outside() / "v.md"
        victim.write_text("KEEP")
        (repo / mw.WIKI_DIR / "linked.md").symlink_to(victim)
        with self.assertRaises(mw.UnsafePathError):
            mw.write_managed_text(repo, ".memlet/wiki/linked.md", "X")
        self.assertEqual("KEEP", victim.read_text())
        with self.assertRaises(mw.UnsafePathError):
            mw.write_managed_text(repo, "AGENTS.md", "X")  # only below .memlet/
        mw.write_managed_text(repo, ".memlet/wiki/new.md", "fresh\n")
        self.assertEqual("fresh\n", (repo / ".memlet/wiki/new.md").read_text())
        self.assertEqual([], [p for p in (repo / mw.WIKI_DIR).iterdir() if ".memlet-" in p.name])  # no temp left
        os.mkfifo(repo / ".memlet/wiki/fifo.md")
        with self.assertRaises(mw.UnsafePathError):
            mw.write_managed_text(repo, ".memlet/wiki/fifo.md", "X")
        with self.assertRaises(mw.UnsafePathError):
            mw.connect_managed_sqlite(repo, ".memlet/wiki/linked.md")

    def test_sqlite_side_file_symlink_is_refused(self):
        repo = self.make_repo()
        victim = self.outside() / "j"
        victim.write_bytes(b"KEEP")
        (repo / (mw.LEDGER_PATH + "-journal")).symlink_to(victim)
        res = mw.init_project(repo, force=True)
        self.assertFalse(res["ok"])
        self.assertEqual(b"KEEP", victim.read_bytes())



    def test_inspector_default_output_symlink_is_refused(self):
        repo = self.make_repo()
        victim = self.outside() / "page.html"
        victim.write_text("KEEP")
        (repo / mi.INSPECTOR_FILE).symlink_to(victim)
        res = mi.export_inspector_html(str(repo))
        self.assertFalse(res["ok"])
        self.assertEqual("KEEP", victim.read_text())


# ---------------------------------------------------------------------------------------------------------------
# A2: discovery never reads through an ancestor symlink


class A2AncestorSymlinkReadTests(_Repo):
    def setUp(self):
        self.repo = self.make_repo()
        self.out = self.outside()
        (self.out / "copilot-instructions.md").write_text(f"- {CANARY} must use `app.py::main`.\n")
        (self.out / "x.md").write_text(f"- {CANARY} glob rule `app.py::main`.\n")
        (self.repo / ".github").symlink_to(self.out, target_is_directory=True)
        (self.repo / ".cursor").mkdir()
        (self.repo / ".cursor" / "rules").symlink_to(self.out, target_is_directory=True)

    def assertNoCanary(self, value, label):
        self.assertNotIn(CANARY, json.dumps(value, ensure_ascii=False), label)

    def test_discovery_and_membership_reject_ancestor_links(self):
        self.assertEqual(["AGENTS.md"], mw.discover_instruction_files(self.repo))
        self.assertFalse(mw.is_instruction_path(self.repo, ".github/copilot-instructions.md"))
        self.assertFalse(mw.is_instruction_path(self.repo, ".cursor/rules/x.md"))
        self.assertIsNotNone(mw.contained_path_problem(self.repo, ".github/copilot-instructions.md"))
        docs = [p.relative_to(self.repo).as_posix() for p in audit._discover_instruction_docs(self.repo)]
        self.assertEqual(["AGENTS.md"], docs)

    def test_recall_handover_check_and_session_context_do_not_expose_outside_text(self):
        rec = mb.unified_recall(self.repo, CANARY, include=("rules",))
        self.assertNoCanary(rec["sections"], "recall sections")
        self.assertNotIn(CANARY, rec["formatted"])
        self.assertNoCanary(mw.build_handover(self.repo, budget=2000), "handover")
        self.assertNoCanary(mw.build_handover(self.repo, budget=2000, mode="digest"), "digest handover")
        self.assertNoCanary(mb.changed_check(self.repo, files=[".github/copilot-instructions.md"]), "check")
        self.assertNoCanary(audit.audit_repository(self.repo), "audit")
        self.assertNoCanary(mbr._source_lines(self.repo), "brief/index sources")

    def test_mcp_tools_do_not_expose_outside_text(self):
        from memlet_mcp import MemletMCPServer
        server = MemletMCPServer(_tmp(self, "final-a-store-"), default_task="review")

        def rpc(name, **args):
            return server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})["result"]
        rec = rpc("memlet_recall", repo_path=str(self.repo), query="acquisition plan must use main")
        self.assertNoCanary(rec, "memlet_recall")
        self.assertNoCanary(rpc("memlet_handover", repo_path=str(self.repo), budget=2000), "memlet_handover")
        self.assertNoCanary(rpc("memlet_check", repo_path=str(self.repo), changed=False), "memlet_check")


# ---------------------------------------------------------------------------------------------------------------
# A3: anchor accept and Dream promotion are serialised; nothing approved is lost


class A3AcceptPromotionRaceTests(_Repo):
    def setUp(self):
        self.repo = self.make_repo()
        t = _transcript(_tmp(self, "final-a-t-") / "s.jsonl", "Never call main() in app.py without validating input",
                        "Do not call main() in app.py with empty credentials")
        self.assertTrue(mb.learn_from_transcript(self.repo, t)["ok"])
        self.ids = [n["id"] for n in mb.learn_pending(self.repo)["pendingNodes"]]
        self.assertEqual(2, len(self.ids))
        self.assertTrue(mb.learn_approve(self.repo, [self.ids[0]], approver="Reviewer")["ok"])
        (self.repo / "app.py").write_text("def main():\n    return 2\n\n\ndef helper(x):\n    return x + 1\n")

    def page_ids(self):
        return sorted(r["id"] for r in mb._parse_promoted((self.repo / mb.PROMOTED_PAGE).read_text()))

    def test_accept_paused_inside_its_page_write_cannot_lose_a_concurrent_approval(self):
        ready, go, order, result = threading.Event(), threading.Event(), [], {}
        original = mb._write_text_atomic
        page = self.repo / mb.PROMOTED_PAGE

        def paused(path, text):
            if threading.current_thread().name == "accept" and path == page:
                ready.set()
                self.assertTrue(go.wait(20))
            order.append((threading.current_thread().name, path.name))
            return original(path, text)

        def accept():
            result["accept"] = mb.accept_anchor(self.repo, ["app.py::main"])

        def approve():
            result["approve"] = mb.learn_approve(self.repo, [self.ids[1]], approver="Reviewer")

        with mock.patch.object(mb, "_write_text_atomic", paused):
            ta = threading.Thread(target=accept, name="accept")
            ta.start()
            self.assertTrue(ready.wait(20))
            tb = threading.Thread(target=approve, name="approve")
            tb.start()
            time.sleep(0.5)
            self.assertTrue(tb.is_alive(), "the approval must wait for the accept's write lock")
            go.set()
            ta.join(30)
            tb.join(30)
        self.assertTrue(result["accept"]["ok"], result["accept"])
        self.assertTrue(result["approve"]["ok"], result["approve"])
        self.assertEqual(sorted(self.ids), self.page_ids())
        self.assertIsNone(mb._page_edit_problem(self.repo, page.read_text()))
        names = [n for n, _ in order]
        self.assertEqual("accept", names[0])
        self.assertEqual("approve", names[-1])  # approval's promotion ran after the accept's whole transaction
        cap = mw.build_handover(self.repo, budget=2000)
        self.assertEqual(2, len([a for a in cap["activeConstraints"] if mb.PROMOTED_PAGE in a["source"]]))

    def test_two_concurrent_accepts_of_different_anchors_both_survive(self):
        (self.repo / mw.WIKI_DIR / "notes.md").write_text(
            "# Notes\n\n- Use `app.py::main` carefully.\n- Use `app.py::helper` for x.\n")
        self.assertTrue(mw.wiki_status(self.repo)["ok"])
        self.assertTrue(mw.init_project(self.repo)["ok"])
        barrier = threading.Barrier(2)
        results = {}

        def run(anchor):
            barrier.wait(10)
            results[anchor] = mb.accept_anchor(self.repo, [anchor])

        threads = [threading.Thread(target=run, args=(a,)) for a in ("app.py::main", "app.py::helper")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertTrue(all(r["ok"] for r in results.values()), results)
        cfg = json.loads((self.repo / mw.CONFIG_PATH).read_text())
        for anchor in ("app.py::main", "app.py::helper"):
            path, sym = anchor.split("::")
            cur = mw._current_fingerprint(self.repo, path, sym)["ast"]
            self.assertEqual(cur, cfg["pinnedAnchors"][anchor]["astHash"], anchor)
        statuses = {a["anchor"]: a["status"] for a in mw.wiki_status(self.repo)["anchors"]}
        self.assertEqual("active", statuses["app.py::main"])
        self.assertEqual("active", statuses["app.py::helper"])


# ---------------------------------------------------------------------------------------------------------------
# A4: bounded parsing and reads


class A4BoundedWorkTests(_Repo):
    def timed(self, fn):
        # CPU time of this process, not wall time: the bound guards against a quadratic regression and must not
        # fail because other jobs are running on the machine.
        start = time.process_time()
        res = fn()
        return res, time.process_time() - start

    def test_reviewer_inputs_finish_fast_through_the_mcp_entry_points(self):
        from memlet_mcp import MemletMCPServer
        repo = self.make_repo()
        server = MemletMCPServer(_tmp(self, "final-a-store-"), default_task="review")

        def rpc(name, **args):
            return server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": dict(args, repo_path=str(repo))}})["result"]
        cases = [("# x" + " " * 5000 + "!", "memlet_recall", {"query": "x"}),
                 ("# x" + " " * 5000 + "!", "memlet_handover", {}),
                 ("# x" + " " * 5000 + "!", "memlet_check", {"files": ["AGENTS.md"]}),
                 ("[" * 200000, "memlet_check", {"changed": False}),
                 ("- " + "z" * 100000, "memlet_recall", {"query": "z"})]
        for body, tool, args in cases:
            (repo / "AGENTS.md").write_text(body + "\n")
            res, dt = self.timed(lambda: rpc(tool, **args))
            self.assertLess(dt, FAST, (tool, len(body), dt))
            self.assertIn("content", res)
        (repo / "AGENTS.md").write_text("- Keep `app.py::main` argument-free.\n")
        (repo / mw.WIKI_DIR / "notes.md").write_text("<!-- memlet:status:begin -->\n" * 20000)
        res, dt = self.timed(lambda: rpc("memlet_handover"))
        self.assertLess(dt, FAST, dt)
        self.assertFalse(res.get("isError"), res)

    def test_parsers_are_linear_and_keep_their_results(self):
        for text in ("# x" + " " * 100000 + "!", " " * 100000 + "!", "<!-- memlet:status:begin -->\n" * 50000):
            _r, dt = self.timed(lambda: (mw._HEADING_RE.match(text), mw._DIGEST_SOURCE_TAIL_RE.sub("", text),
                                          mw._STATUS_BLOCK_RE.sub("", text), mw._PAGE_STATUS_RE.sub("", text)))
            self.assertLess(dt, FAST)
        self.assertEqual("Title", mw._HEADING_RE.match("## Title ##").group("title"))
        self.assertEqual("a", mw._DIGEST_SOURCE_TAIL_RE.sub("", "a — source `x.md:1` tail"))
        for text in ("[" * 200000, "[a]" * 60000, "a.py::" + "-" * 100000, "a.py#" + "-" * 100000):
            _r, dt = self.timed(lambda: (list(audit.MD_LINK_FILE_RE.finditer(text)), audit.strip_ref_link_keys(text),
                                          list(audit.iter_explicit_anchors(text))))
            self.assertLess(dt, FAST, (text[:8], dt))
        self.assertEqual(["x.py"], [m.group("path") for m in audit.MD_LINK_FILE_RE.finditer("[[a](x.py)")])

    def test_masking_is_linear_and_redacts_the_same(self):
        for text in ("z" * 200000, "eyJ" * 70000, "-eyJ" * 50000, "KEY" * 70000, "a" * 200000 + "://x",
                     "-----BEGIN A-----\n" * 20000):
            _r, dt = self.timed(lambda: mbr._mask(text))
            self.assertLess(dt, FAST, (text[:6], dt))
        secret = ("token=abcdefghijklmnop1234 KEY: s3cr3tvalue1 https://user:pass@host "
                  "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop ghp_" + "a" * 36)
        masked = mbr._mask(secret)
        for leak in ("abcdefghijklmnop1234", "s3cr3tvalue1", "user:pass", "eyJhbGciOiJIUzI1NiJ9", "ghp_aaaa"):
            self.assertNotIn(leak, masked)
        orig = re.compile(mbr._EXTRA_SECRET_PATTERN, re.I)
        for sample in (secret, "x://a:b@c d", "MY_API_KEY = 'abcdefghij'", "sk-" + "a" * 20, "eyj" * 9):
            self.assertEqual(re.compile(redaction._SECRET_VALUE_PATTERN).sub("#", sample),
                             redaction.SECRET_VALUE_REDACT_RE.sub("#", sample))
            self.assertEqual(orig.sub("#", sample), mbr._EXTRA_SECRET_RE.sub("#", sample))

    def test_config_reads_never_block_on_a_fifo_and_are_size_capped(self):
        repo = self.make_repo()
        cfg = repo / mw.CONFIG_PATH
        cfg.unlink()
        os.mkfifo(cfg)
        res, dt = self.timed(lambda: mw.build_handover(repo, budget=2000))
        self.assertLess(dt, FAST)
        self.assertIn("activeConstraints", res)
        self.assertIsNotNone(mw.read_config(repo)[1])
        cfg.unlink()
        cfg.write_text(json.dumps({"pad": "x" * (mw.MAX_CONFIG_BYTES + 1)}))
        self.assertIsNone(mw.read_config(repo)[0])
        self.assertIn("larger", mw.read_config(repo)[1])

    def test_fifo_config_handover_in_a_child_process_returns(self):
        repo = self.make_repo()
        (repo / mw.CONFIG_PATH).unlink()
        os.mkfifo(repo / mw.CONFIG_PATH)
        scratch = _tmp(self, "final-a-home-")
        env = dict(os.environ, HOME=str(scratch), TMPDIR=str(scratch), PYTHONPATH=str(ROOT))
        code = ("import json,sys; from memlet_mcp import MemletMCPServer; "
                f"s=MemletMCPServer({str(scratch / 'store')!r}, default_task='review'); "
                f"r=s.call_tool('memlet_handover', {{'repo_path': {str(repo)!r}}}); print('RETURNED')")
        start = time.monotonic()
        r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=20, env=env,
                           cwd=str(scratch))
        self.assertIn("RETURNED", r.stdout, r.stderr[-500:])
        self.assertLess(time.monotonic() - start, 10)

    def test_wiki_page_set_and_snapshot_reads_are_bounded(self):
        repo = self.make_repo()
        big = repo / mw.WIKI_DIR / "big.md"
        big.write_text("- x\n" * (mw.MAX_WIKI_PAGE_BYTES // 4 + 10))
        self.assertNotIn(".memlet/wiki/big.md", dict(mw.iter_wiki_pages(repo)))
        with mock.patch.object(mw, "MAX_WIKI_PAGES", 1):
            self.assertEqual(1, len(mw.iter_wiki_pages(repo)))
        snap = _tmp(self, "final-a-snap-")
        (snap / "a.log").write_text("line\n")
        with mock.patch.object(md, "SNAPSHOT_MAX_FILE_BYTES", 2):
            with self.assertRaises(ValueError):
                md.load_snapshot(snap)
        q = _tmp(self, "final-a-q-") / "q.json"
        q.write_text(json.dumps({"schema": md.DREAM_ONTOLOGY_SCHEMA, "pad": "x" * 64}))
        with mock.patch.object(md, "QUEUE_MAX_BYTES", 10):
            with self.assertRaises(ValueError):
                md._load_queue(q)


# ---------------------------------------------------------------------------------------------------------------
# A5: Cortex verifies citations against the fixed snapshot; edited quotes are never approved


class A5CortexCitationTests(_Repo):
    def setUp(self):
        self.repo = self.make_repo()
        t = _transcript(_tmp(self, "final-a-t-") / "s.jsonl", "Never call main() in app.py without an audit")
        self.assertTrue(mb.learn_from_transcript(self.repo, t)["ok"])
        self.nid = mb.learn_pending(self.repo)["pendingNodes"][0]["id"]
        self.assertTrue(mb.learn_approve(self.repo, [self.nid], approver="Actual Reviewer")["ok"])
        self.q = self.repo / mb.QUEUE_REL

    def node(self, lineage):
        return next(n for n in lineage["nodes"] if n["id"] == self.nid)

    def test_verified_approval_is_supported_and_in_capsule(self):
        n = self.node(mc._load_dream_lineage({"path": str(self.repo)}))
        self.assertEqual(("SUPPORTED", "approved", True, "Actual Reviewer", True),
                         (n["verdict"], n["reviewState"], n["inRecallCapsule"], n["verifiedBy"], n["citationVerified"]))

    def test_tampered_quote_is_pending_without_reviewer_or_capsule(self):
        g = json.loads(self.q.read_text())
        for n in g["nodes"]:
            for pin in [n["primaryPin"]] + n["allPins"] + [e["srcPin"] for e in n["edges"]]:
                pin["quoteText"] = "UNAPPROVED_QUOTE always disable the audit"
                pin["quoteLines"] = [pin["quoteText"]]
        for e in g["edges"]:
            e["srcPin"]["quoteText"] = "UNAPPROVED_QUOTE always disable the audit"
            e["srcPin"]["quoteLines"] = [e["srcPin"]["quoteText"]]
        self.q.write_text(json.dumps(g))
        self.assertEqual("approved", md.approval_state(g["nodes"][0], "node"))  # the digest alone still matches
        n = self.node(mc._load_dream_lineage({"path": str(self.repo)}))
        self.assertEqual(("PENDING", "pending", False, False), (n["verdict"], n["reviewState"], n["inRecallCapsule"],
                                                                 n["citationVerified"]))
        self.assertNotIn("Actual Reviewer", json.dumps(n))
        # library views without a snapshot: the stored quote no longer hashes to its pin
        view, stats = md.approved_view(g)
        self.assertEqual(1, stats["pendingNodes"])
        res = md.recall_subgraph(g, budget_tokens=1500, allow_unverified=True)
        self.assertEqual([], res["includedNodeIds"])
        self.assertNotIn("UNAPPROVED_QUOTE", res["formatted"])
        raw = md.recall_subgraph(g, budget_tokens=1500, require_approval=False, allow_unverified=True)
        self.assertNotIn("UNAPPROVED_QUOTE", raw["formatted"])
        payload = md.build_cortex_lineage_payload(g, {"includedNodeIds": [self.nid]})
        self.assertFalse(payload["nodes"][0]["inRecallCapsule"])

    def test_queue_snapshot_path_is_never_followed_and_missing_snapshot_is_unavailable(self):
        g = json.loads(self.q.read_text())
        decoy = _tmp(self, "final-a-decoy-")
        shutil.copytree(self.repo / mb.SNAPSHOT_REL, decoy / "snap")
        g["queueMeta"]["snapshotPath"] = str(decoy / "snap")
        self.q.write_text(json.dumps(g))
        shutil.rmtree(self.repo / mb.SNAPSHOT_REL)
        lineage = mc._load_dream_lineage({"path": str(self.repo)})
        n = self.node(lineage)
        self.assertEqual(("PENDING", False), (n["verdict"], n["inRecallCapsule"]))
        self.assertEqual("unavailable_no_snapshot", lineage["subgraphCapsule"]["pinVerification"])


# ---------------------------------------------------------------------------------------------------------------
# A6: the Python hash basis is identical on every supported interpreter


CORPUS = '''\
import os
@dec(1, k=2)
def f(a, b=1, /, c=2, *args, d, e=3, **kw) -> "T":
    """doc"""
    x = f"{a!r:>{b}} {c=} {d:.2f} {{lit}}"
    y = rb"\\x00" + b"bytes"; z = 1j + 1.5e-3 + 0x10
    lam = lambda p, *q, r=1, **s: (p, q, r, s)
    sl = a[1:2:3, ..., ::-1]
    if (n := len(args)) > 2 and not a is b or c in d < e:
        pass
    try:
        raise ValueError("v") from None
    except (TypeError, ValueError) as err:
        assert err, "m"
    with open(a) as fa, open(b):
        pass
    return {k: v for k, v in kw.items() if k}
async def g(x):
    async with x as y:
        await y
    async for z in y:
        print(*z, sep="", **{})
class C(Base, metaclass=M):
    """cls doc"""
    attr: int = 1
    def method(self, x: int = 0) -> None: ...
def emoji():
    return "\U0001f6df \\u2713 \\u00e9"
'''


class A6CrossVersionHashTests(unittest.TestCase):
    CODE = ("import json,sys; sys.path.insert(0, %r); from memlet import code_anchors as ca, memlet_wiki as mw; "
            "from pathlib import Path; p=Path(%r); r=Path(%r); "
            "print(json.dumps({'hashes': ca.extract_file_symbol_hashes(p), 'basis': ca.PYTHON_HASH_BASIS, "
            "'fp': mw._current_fingerprint(r, 'corpus.py', 'f')}))")

    def run_under(self, py, repo):
        env = dict(os.environ, HOME=str(repo), TMPDIR=str(repo))
        env.pop("PYTHONPATH", None)
        if py != sys.executable:  # the running interpreter's own home/launcher settings must not leak into the other
            for k in ("PYTHONHOME", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
                env.pop(k, None)
        r = subprocess.run([py, "-B", "-c", self.CODE % (str(ROOT), str(repo / "corpus.py"), str(repo))],
                           capture_output=True, text=True, timeout=60, env=env, cwd=str(repo))
        self.assertEqual(0, r.returncode, r.stderr[-800:])
        return json.loads(r.stdout)

    @unittest.skipUnless(os.path.exists(OTHER_PY), "needs /usr/bin/python3")
    def test_hashes_match_on_both_interpreters(self):
        repo = _tmp(self, "final-a-a6-")
        (repo / "corpus.py").write_text(CORPUS, encoding="utf-8")
        a, b = self.run_under(sys.executable, repo), self.run_under(OTHER_PY, repo)
        self.assertEqual("python-ast-v3", a["basis"])
        self.assertGreaterEqual(len(a["hashes"]), 5)
        self.assertEqual(a["hashes"], b["hashes"])
        self.assertEqual(a["fp"], b["fp"])
        self.assertEqual("python-ast-v3", a["fp"]["basis"])

    def test_previous_basis_pins_still_match_and_migrate(self):
        repo = _tmp(self, "final-a-a6m-")
        (repo / "app.py").write_text("def main():\n    return 1\n")
        old = ca.extract_file_symbol_hashes(repo / "app.py", basis="python-ast-v2")["main"]
        cur = ca.extract_file_symbol_hashes(repo / "app.py")["main"]
        self.assertNotEqual(old, cur)
        anchor = {"path": "app.py", "symbol": "main", "pin": {"ast": old[:16], "basis": "python-ast-v2"}}
        res = mw.classify_anchor(repo, anchor)
        self.assertEqual(("active", "legacy_ast_hash_match"), (res["status"], res["reason"]))
        (repo / "AGENTS.md").write_text("- Keep `app.py::main`.\n")
        (repo / ".memlet").mkdir()
        (repo / mw.CONFIG_PATH).write_text(json.dumps({"pinnedAnchors": {"app.py::main": {
            "astHash": old[:16], "hashBasis": "python-ast-v2"}}}))
        self.assertTrue(mw.init_project(repo)["ok"])
        pin = json.loads((repo / mw.CONFIG_PATH).read_text())["pinnedAnchors"]["app.py::main"]
        self.assertEqual(("python-ast-v3", cur[:16]), (pin["hashBasis"], pin["astHash"]))

    def test_skill_reference_tracked_with_v2_survives_an_unrelated_edit(self):
        import memory_skills
        repo = _tmp(self, "final-a-a6s-")
        p = repo / "mod.py"
        p.write_text("def keep():\n    return 1\n\n\ndef other():\n    return 2\n")
        v2 = ca.extract_file_symbol_hashes(p, basis="python-ast-v2")["keep"]
        p.write_text("def keep():\n    return 1\n\n\ndef other():\n    return 3\n")
        previous = ca.previous_python_symbol_hashes(p)
        self.assertTrue(any(h.get("keep") == v2 for h in previous))
        self.assertTrue(hasattr(memory_skills, "_REFERENCED_PATH_RE"))


# ---------------------------------------------------------------------------------------------------------------
# A7: a hand edit of the promoted page is not trusted by readers until an explicit accept


class A7PromotedPageOwnershipTests(_Repo):
    def test_pin_only_edit_is_not_presented_as_verified_until_accept(self):
        repo = self.make_repo(agents="# Rules\n\n- Keep changes small.\n")
        t = _transcript(_tmp(self, "final-a-t-") / "s.jsonl", "Never call main() in app.py without an audit")
        self.assertTrue(mb.learn_from_transcript(repo, t)["ok"])
        nid = mb.learn_pending(repo)["pendingNodes"][0]["id"]
        self.assertTrue(mb.learn_approve(repo, [nid], approver="Actual Reviewer")["ok"])
        (repo / "app.py").write_text("def main():\n    return 9\n\n\ndef helper(x):\n    return x\n")
        page = repo / mb.PROMOTED_PAGE
        fp = mw._current_fingerprint(repo, "app.py", "main")
        pin = {"ast": fp["ast"], "sig": fp["sig"], "req": fp["req"], "basis": fp["basis"]}
        page.write_text(mw.PIN_RE.sub(mb._pin_comment("app.py::main", pin).strip(), page.read_text()))
        cap = mw.build_handover(repo, budget=2000)  # what session start injects
        self.assertEqual([], [a for a in cap["activeConstraints"] if mb.PROMOTED_PAGE in a["source"]])
        self.assertEqual([], mb.promoted_rule_entries(repo)[0])
        self.assertNotIn(nid, json.dumps(mb.unified_recall(repo, "audit main")["sections"]["rules"]))
        acc = mb.accept_anchor(repo, ["app.py::main"])
        self.assertTrue(acc["ok"], acc)
        cap = mw.build_handover(repo, budget=2000)
        self.assertEqual(1, len([a for a in cap["activeConstraints"] if mb.PROMOTED_PAGE in a["source"]]))


# ---------------------------------------------------------------------------------------------------------------
# N2: one backup before the store migration


class N2MigrationBackupTests(unittest.TestCase):
    def test_legacy_store_is_backed_up_once_before_migration(self):
        from memlet.standalone import StandaloneStore
        root = _tmp(self, "final-a-n2-")
        db = root / "work.sqlite3"
        with sqlite3.connect(str(db)) as conn:
            conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', project TEXT NOT NULL "
                         "DEFAULT 'default', owner TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'active', "
                         "revision INTEGER NOT NULL DEFAULT 1, body TEXT NOT NULL DEFAULT '{}', updated REAL NOT NULL DEFAULT 0)")
            conn.execute("INSERT INTO tasks (id, title, updated) VALUES ('t/a', 'kept', 5)")
        store = StandaloneStore(root)
        backups = sorted(root.glob("work.sqlite3.bak-pre-migration-*"))
        self.assertEqual(1, len(backups))
        self.assertEqual(str(backups[0]), store.migration["backup"])
        with sqlite3.connect(str(backups[0])) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
            self.assertNotIn("created", cols)
            self.assertEqual([("t/a", "kept")], conn.execute("SELECT id, title FROM tasks").fetchall())
        again = StandaloneStore(root)
        self.assertIsNone(again.migration)
        self.assertEqual(1, len(sorted(root.glob("work.sqlite3.bak-pre-migration-*"))))


if __name__ == "__main__":
    unittest.main()
