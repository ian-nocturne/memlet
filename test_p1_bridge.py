"""P1 storage bridge (unit B2): unified recall over three stores, Dream -> wiki promotion, per-anchor accept,
changed-file check, Slim -> Index -> Recall, inspector audit keys. Stdlib unittest only, no network, no model call."""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memlet import memlet_bridge as mb
from memlet import memlet_dream as md
from memlet.memlet_wiki import _PAGE_STATUS_RE, build_handover, estimate_tokens, init_project, wiki_status

APP_PY = "def main():\n    return 1\n\n\ndef helper(x):\n    return x\n"


def _tmp(test, prefix):
    d = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    test.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
    return d


def _isolate_git(test, repo):
    # A temp directory may live inside the source checkout. Never discover
    # that enclosing repository while testing a directory without .git.
    home = _tmp(test, "p1-bridge-home-")
    isolation = mock.patch.dict(os.environ, {"HOME": str(home), "TMPDIR": str(home),
                                            "GIT_CEILING_DIRECTORIES": str(repo.parent),
                                            "GIT_CONFIG_GLOBAL": os.devnull,
                                            "GIT_CONFIG_SYSTEM": os.devnull})
    isolation.start()
    test.addCleanup(isolation.stop)


def _records(extra=()):
    recs = [
        {"type": "summary", "summary": "session", "leafUuid": "x"},
        {"type": "user", "timestamp": "2026-11-09T09:00:00Z",
         "message": {"role": "user", "content": "Never call main() in app.py from the test suite"}},
        {"type": "assistant", "timestamp": "2026-11-09T09:00:05Z",
         "message": {"role": "assistant", "type": "message", "content": [
             {"type": "thinking", "thinking": "hidden REASONING-XYZ"},
             {"type": "text", "text": "Understood."},
             {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cat TOOLINPUT-XYZ"}}]}},
        {"type": "user", "timestamp": "2026-11-09T09:00:06Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                   "content": "TOOLPAYLOAD-XYZ never leak this"}]}},
        {"type": "user", "timestamp": "2026-11-09T09:02:00Z",
         "message": {"role": "user", "content": "Do not push to the release branch without approval"}},
        {"type": "assistant", "timestamp": "2026-11-09T09:03:00Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Workaround: run the migration with a dry run first"}]}},
    ]
    return recs + list(extra)


def _write_jsonl(path, recs):
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")


class _RepoCase(unittest.TestCase):
    def setUp(self):
        self.repo = _tmp(self, "p1-bridge-repo-")
        _isolate_git(self, self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text(APP_PY)
        (self.repo / "AGENTS.md").write_text("# Rules\n\n- Always run the unit tests before committing.\n"
                                             "- Telegram notifications go through scripts/notify.sh only.\n")
        self.tdir = _tmp(self, "p1-bridge-transcripts-")
        self.transcript = self.tdir / "cc-session.jsonl"
        _write_jsonl(self.transcript, _records())

    def learn(self):
        res = mb.learn_from_transcript(self.repo, self.transcript)
        self.assertTrue(res["ok"], res)
        return res

    def ids_by_title(self):
        return {n["title"]: n["id"] for n in mb.learn_pending(self.repo)["pendingNodes"]}

    def pick(self, ids, needle):
        return next(v for k, v in ids.items() if needle in k)

    def queue(self):
        return json.loads((self.repo / mb.QUEUE_REL).read_text())

    def snapshot_file(self):
        files = sorted((self.repo / mb.SNAPSHOT_REL).iterdir())
        self.assertEqual(1, len(files))
        return files[0]


class LearnTests(_RepoCase):
    def test_learn_builds_pending_only_queue_from_visible_text(self):
        res = self.learn()
        self.assertEqual(3, len(res["addedNodeIds"]))
        self.assertEqual(3, res["pendingCount"])
        self.assertTrue((self.repo / mb.DB_REL).is_file())
        graph = self.queue()
        self.assertEqual(3, len(graph["nodes"]))
        for n in graph["nodes"]:
            self.assertEqual("pending", md.approval_state(n, "node"))
            self.assertEqual(md.PENDING, n["verdict"])
        snap = self.snapshot_file().read_text()
        self.assertIn("[user] Never call main() in app.py from the test suite", snap)
        for hidden in ("REASONING-XYZ", "TOOLINPUT-XYZ", "TOOLPAYLOAD-XYZ"):
            self.assertNotIn(hidden, snap)
            self.assertNotIn(hidden, json.dumps(graph))
        # the queue records the in-repo snapshot and a DB inside the queue directory, nothing else
        self.assertEqual(str(self.repo / mb.SNAPSHOT_REL), graph["queueMeta"]["snapshotPath"])
        self.assertEqual("ontology.sqlite3", graph["queueMeta"]["dbPath"])
        self.assertTrue(md.queue_db_in_sync(self.repo / mb.QUEUE_REL, self.repo / mb.DB_REL))
        # nothing is recalled before a human approves
        u = mb.unified_recall(self.repo, "main test suite release", budget=800)
        self.assertEqual([], u["sections"]["dream"])
        for nid in res["addedNodeIds"]:
            self.assertNotIn(nid, u["formatted"])

    def test_second_run_adds_nothing_and_keeps_approvals(self):
        first = self.learn()
        before = (self.repo / mb.QUEUE_REL).read_bytes()
        again = self.learn()
        self.assertEqual([], again["addedNodeIds"])
        self.assertFalse(again["written"])
        self.assertEqual(before, (self.repo / mb.QUEUE_REL).read_bytes())
        ids = self.ids_by_title()
        main_id = self.pick(ids, "main()")
        self.assertTrue(mb.learn_approve(self.repo, [main_id], approver="ray")["ok"])
        # an appended transcript adds only the new lesson; the approval and the old pins are untouched
        _write_jsonl(self.transcript, _records([
            {"type": "user", "timestamp": "2026-11-09T10:00:00Z",
             "message": {"role": "user", "content": "Never edit generated files by hand"}}]))
        third = self.learn()
        self.assertEqual(1, len(third["addedNodeIds"]))
        self.assertNotIn(main_id, third["addedNodeIds"])
        nodes = {n["id"]: n for n in self.queue()["nodes"]}
        self.assertEqual(4, len(nodes))
        self.assertEqual("approved", md.approval_state(nodes[main_id], "node"))
        self.assertEqual(sorted(first["addedNodeIds"] + third["addedNodeIds"]), sorted(nodes))
        self.assertEqual(3, third["pendingCount"])

    def test_refuses_symlinked_memlet_and_foreign_queue(self):
        target = _tmp(self, "p1-bridge-elsewhere-")
        os.symlink(target, self.repo / ".memlet")
        res = mb.learn_from_transcript(self.repo, self.transcript)
        self.assertFalse(res["ok"])
        self.assertIn("symlink", res["error"])
        self.assertEqual([], list(target.iterdir()))
        os.unlink(self.repo / ".memlet")
        # a queue built by plain dream-build (other snapshot) is never merged into or overwritten
        md.run_dream_build(str(self.tdir), out_json=str(self.repo / mb.QUEUE_REL))
        before = (self.repo / mb.QUEUE_REL).read_bytes()
        res = mb.learn_from_transcript(self.repo, self.transcript)
        self.assertFalse(res["ok"])
        self.assertEqual(before, (self.repo / mb.QUEUE_REL).read_bytes())

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_gitignore_notice_without_editing_gitignore(self):
        subprocess.run(["git", "init", "-q"], cwd=str(self.repo), check=True)
        (self.repo / ".gitignore").write_text("*.pyc\n")
        res = self.learn()
        self.assertTrue(any(".memlet/" in n and ".gitignore" in n for n in res["notices"]), res["notices"])
        self.assertEqual("*.pyc\n", (self.repo / ".gitignore").read_text())
        (self.repo / ".gitignore").write_text("*.pyc\n.memlet/\n")
        self.assertEqual([], self.learn()["notices"])


class PromoteTests(_RepoCase):
    def approve_two(self):
        self.learn()
        ids = self.ids_by_title()
        main_id, rel_id = self.pick(ids, "main()"), self.pick(ids, "release branch")
        res = mb.learn_approve(self.repo, [main_id, rel_id], approver="ray")
        self.assertTrue(res["ok"], res)
        self.assertEqual(sorted([main_id, rel_id]), sorted(res["promotion"]["promoted"]))
        return ids, main_id, rel_id

    def test_approve_promotes_into_handover_and_rule_recall(self):
        from memlet.memlet_recall import recall
        ids, main_id, rel_id = self.approve_two()
        page = (self.repo / mb.PROMOTED_PAGE).read_text()
        self.assertIn("`src/app.py::main`", page)               # scope names code that exists: a real anchor
        self.assertIn("<!-- memlet:anchor src/app.py::main ast=", page)
        self.assertIn(f"dream {main_id} · repeated_pitfall · scope app.py · cites .memlet/dream/snapshot/", page)
        cap = build_handover(self.repo, output_format="markdown")
        self.assertIn("Never call main() in app.py from the test suite",
                      " ".join(a["rule"] for a in cap["activeConstraints"]))
        self.assertIn("Do not push to the release branch without approval", cap["formatted"])  # dead-end, unanchored
        rec = recall(self.repo, "may I call main from the test suite", k=3)
        self.assertIn("Never call main() in app.py from the test suite", rec["formatted"])
        self.assertTrue(any(s.startswith(mb.PROMOTED_PAGE + ":") for s in rec["spans"]))
        wk_id = self.pick(ids, "Workaround")
        self.assertNotIn(wk_id, page)                             # still PENDING: never promoted
        self.assertNotIn("Workaround", rec["formatted"])
        # idempotent; `memlet wiki` does not mistake the promoted page for a hand edit
        self.assertEqual("unchanged", mb.promote_approved(self.repo)["op"])
        ws = wiki_status(self.repo)
        self.assertNotIn(mb.PROMOTED_PAGE, ws["userEditedPages"])
        self.assertEqual("unchanged", mb.promote_approved(self.repo)["op"])

    def test_edited_snapshot_line_drops_lesson_from_recall_and_page(self):
        from memlet.memlet_recall import recall
        _ids, main_id, rel_id = self.approve_two()
        snap = self.snapshot_file()
        lines = snap.read_text().splitlines()
        lines[1] = lines[1].replace("Never call main()", "Always call main()")
        snap.write_text("\n".join(lines) + "\n")
        rec = recall(self.repo, "call main from the test suite", k=3)
        self.assertNotIn("Never call main()", rec["formatted"])
        self.assertGreaterEqual(rec["stale"], 1)
        u = mb.unified_recall(self.repo, "main test suite", budget=800)
        self.assertEqual([rel_id], [n["id"] for n in u["sections"]["dream"]])
        self.assertTrue(u["sources"]["dream"]["available"])
        self.assertEqual([main_id], u["unverifiableNodeIds"])
        self.assertEqual(1, u["unverifiableCount"])
        self.assertIn("Citation verification failed", u["formatted"])
        cap = build_handover(self.repo, output_format="markdown")  # before any re-promotion
        self.assertNotIn("Never call main()", cap["formatted"])
        self.assertIn("Do not push to the release branch", cap["formatted"])  # its own citation still verifies
        prom = mb.promote_approved(self.repo)
        self.assertTrue(prom["ok"], prom)
        self.assertEqual([main_id], prom["removed"])
        self.assertEqual([rel_id], prom["promoted"])
        page = (self.repo / mb.PROMOTED_PAGE).read_text()
        self.assertNotIn(main_id, page)
        self.assertIn(rel_id, page)

    def test_reject_keeps_item_out_of_page_recall_and_pending(self):
        self.learn()
        ids = self.ids_by_title()
        wk_id = self.pick(ids, "Workaround")
        res = mb.learn_reject(self.repo, [wk_id], approver="ray", reason="not a lesson")
        self.assertTrue(res["ok"], res)
        self.assertNotIn(wk_id, [n["id"] for n in mb.learn_pending(self.repo)["pendingNodes"]])
        self.assertFalse((self.repo / mb.PROMOTED_PAGE).exists())  # nothing approved yet: no page created
        mb.learn_approve(self.repo, [self.pick(ids, "main()")], approver="ray")
        self.assertNotIn(wk_id, (self.repo / mb.PROMOTED_PAGE).read_text())
        u = mb.unified_recall(self.repo, "migration dry run workaround", budget=800)
        self.assertNotIn(wk_id, u["formatted"])
        self.assertNotIn(wk_id, json.dumps(u["sections"]))
        # a rejected item cannot be approved afterwards
        again = mb.learn_approve(self.repo, [wk_id], approver="ray")
        self.assertFalse(again["ok"])
        self.assertIn("already_rejected", again["error"])

    def test_hand_edit_of_promoted_page_is_detected_not_overwritten(self):
        _ids, main_id, rel_id = self.approve_two()
        page = self.repo / mb.PROMOTED_PAGE
        edited = page.read_text().replace("## Approved lessons", "## Approved lessons\n\n- my own note")
        page.write_text(edited)
        res = mb.promote_approved(self.repo)
        self.assertFalse(res["ok"])
        self.assertIn("edited by hand", res["error"])
        self.assertEqual(edited, page.read_text())
        wiki_status(self.repo)  # records the edit as user_edit in the ledger (and adds its status header)
        self.assertFalse(mb.promote_approved(self.repo)["ok"])
        self.assertEqual(edited, _PAGE_STATUS_RE.sub("", page.read_text()))

    def test_forged_promoted_line_is_never_quoted_by_recall(self):
        import hashlib
        from memlet.memlet_recall import recall
        _ids, main_id, _rel_id = self.approve_two()
        page = self.repo / mb.PROMOTED_PAGE
        text = page.read_text()
        old_body = next(l[2:] for l in text.splitlines() if l.startswith("- Never call main()"))
        new_body = old_body.replace("Never call main()", "Always call main()")
        old_h, new_h = (hashlib.sha256(b.encode()).hexdigest()[:16] for b in (old_body, new_body))
        page.write_text(text.replace(old_body, new_body).replace(f"text={old_h}", f"text={new_h}"))
        rec = recall(self.repo, "call main from the test suite", k=3)
        self.assertNotIn("Always call main()", rec["formatted"])
        self.assertGreaterEqual(rec["stale"], 1)

    def test_unknown_and_unverifiable_approvals_are_refused(self):
        self.learn()
        res = mb.learn_approve(self.repo, ["DRM-doesnotexist"], approver="ray")
        self.assertFalse(res["ok"])
        self.assertIn("unknown_id", res["error"])
        main_id = self.pick(self.ids_by_title(), "main()")
        snap = self.snapshot_file()
        snap.write_text(snap.read_text().replace("Never call main()", "Please call main()"))
        res = mb.learn_approve(self.repo, [main_id], approver="ray")
        self.assertFalse(res["ok"])
        self.assertIn("pin_does_not_verify", res["error"])
        self.assertEqual("pending", md.approval_state(next(n for n in self.queue()["nodes"] if n["id"] == main_id), "node"))


class UnifiedRecallTests(_RepoCase):
    def _store(self):
        from memlet.standalone import StandaloneStore
        import work_memory as ledger
        root = _tmp(self, "p1-bridge-store-")
        store = StandaloneStore(root)
        store.ensure_standalone_task("t/one")
        with store.db(True) as db:
            ledger.record(db, "t/one", 1, "src:f1", "fact", "fact:deploy", "assert",
                          "Deploys go through the staging pipeline on Fridays", {"kind": "test"}, 100)
        return root, store

    def _index(self):
        from memlet.memlet_index import build_index
        rep = build_index(self.repo, runner=lambda p: "- unit tests | 1 [src: AGENTS.md:3-3]\n"
                                                      "- telegram | 2 [src: AGENTS.md:4-4]\n")
        self.assertTrue(rep["ok"], rep)

    def test_rules_dream_and_memory_in_one_response(self):
        self._index()
        self.learn()
        ids = self.ids_by_title()
        main_id, rel_id, wk_id = self.pick(ids, "main()"), self.pick(ids, "release"), self.pick(ids, "Workaround")
        mb.learn_approve(self.repo, [main_id], approver="ray")
        mb.learn_reject(self.repo, [rel_id], approver="ray")
        root, _store = self._store()
        u = mb.unified_recall(self.repo, "main tests release deploy staging", budget=1200, store=str(root),
                              task="t/one")
        self.assertTrue(u["ok"], u)
        self.assertEqual(["AGENTS.md:3-3"], [i["source"] for i in u["sections"]["rules"]][:1])
        self.assertEqual([main_id], [i["id"] for i in u["sections"]["dream"]])
        self.assertTrue(u["sections"]["dream"][0]["source"].startswith(mb.SNAPSHOT_REL + "/"))
        self.assertEqual(["fact:deploy"], [i["id"] for i in u["sections"]["memory"]])
        for s in ("rules", "dream", "memory"):
            self.assertTrue(u["sources"][s]["available"], u["sources"])
        for never in (rel_id, wk_id):  # rejected and pending
            self.assertNotIn(never, u["formatted"])
            self.assertNotIn(never, json.dumps(u["sections"]))
        self.assertIn("Never call main() in app.py from the test suite", u["formatted"])
        self.assertIn("Deploys go through the staging pipeline", u["formatted"])
        self.assertLessEqual(u["tokenEstimate"], u["tokenBudget"])
        # the promoted copy of the lesson is not shown twice
        self.assertEqual(1, u["formatted"].count("Never call main() in app.py"))

    def test_missing_stores_degrade_to_unavailable(self):
        empty = _tmp(self, "p1-bridge-empty-")
        u = mb.unified_recall(empty, "anything", budget=600, store=str(empty / "nope"), task="t")
        self.assertTrue(u["ok"], u)
        for s in ("rules", "dream", "memory"):
            self.assertFalse(u["sources"][s]["available"])
            self.assertTrue(u["sources"][s]["reason"])
            self.assertEqual([], u["sections"][s])
        self.assertFalse((empty / "nope").exists())  # a missing store path is never created
        not_a_store = _tmp(self, "p1-bridge-notstore-")
        u2 = mb.unified_recall(empty, "anything", store=str(not_a_store), task="t")
        self.assertFalse(u2["sources"]["memory"]["available"])
        self.assertEqual([], list(not_a_store.iterdir()))       # no database is created by a read
        self.assertIn("Not consulted", u["formatted"])
        only = mb.unified_recall(empty, "x", include=("dream",))
        self.assertEqual("not requested", only["sources"]["rules"]["reason"])

    def test_budget_is_respected_at_every_small_budget(self):
        self._index()
        self.learn()
        ids = self.ids_by_title()
        mb.learn_approve(self.repo, [self.pick(ids, "main()"), self.pick(ids, "release")], approver="ray")
        _root, store = self._store()
        seen_items = set()
        for budget in list(range(0, 160, 7)) + [200, 300, 500]:
            u = mb.unified_recall(self.repo, "unit tests main release deploy", budget=budget, store=store, task="t/one")
            self.assertLessEqual(u["tokenEstimate"], budget, (budget, u["formatted"]))
            self.assertEqual(u["tokenEstimate"], estimate_tokens(u["formatted"]))
            seen_items.add(sum(len(v) for v in u["sections"].values()))
            if not u["ok"]:
                self.assertEqual("", u["formatted"])
        self.assertGreater(max(seen_items), 2)
        # unused budget flows: with dream and memory unavailable, the rules section can use the whole budget
        alone = mb.unified_recall(self.repo, "unit tests telegram", budget=300, include=("rules",))
        self.assertEqual(2, len(alone["sections"]["rules"]))


class AnchorTests(unittest.TestCase):
    def setUp(self):
        self.repo = _tmp(self, "p1-anchor-")
        _isolate_git(self, self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text(APP_PY)
        (self.repo / "src" / "other.py").write_text("def thing():\n    return 2\n")
        (self.repo / "CLAUDE.md").write_text(
            "# Rules\n\n- Entry point is `src/app.py::main`; keep it argument-free.\n"
            "- Use `src/app.py::helper` for conversions.\n"
            "- `src/other.py::thing` returns the constant.\n")
        self.assertTrue(init_project(self.repo)["ok"])
        # both app.py symbols drift (new required parameter): stale
        (self.repo / "src" / "app.py").write_text(
            "def main(argv):\n    return 1\n\n\ndef helper(x, y):\n    return x\n")

    def statuses(self):
        return {a["anchor"]: a["status"] for a in wiki_status(self.repo)["anchors"]}

    def test_accept_repins_only_the_named_anchor(self):
        self.assertEqual("stale", self.statuses()["src/app.py::main"])
        cfg_before = json.loads((self.repo / ".memlet" / "config.json").read_text())["pinnedAnchors"]
        dry = mb.accept_anchor(self.repo, ["src/app.py::main"], dry_run=True)
        self.assertTrue(dry["ok"] and dry["dryRun"])
        self.assertEqual("stale", self.statuses()["src/app.py::main"])
        res = mb.accept_anchor(self.repo, ["src/app.py::main"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(["src/app.py::main"], res["accepted"])
        st = self.statuses()
        self.assertEqual("active", st["src/app.py::main"])
        self.assertEqual("stale", st["src/app.py::helper"])        # the second stale anchor stays stale
        self.assertEqual("active", st["src/other.py::thing"])
        cfg = json.loads((self.repo / ".memlet" / "config.json").read_text())["pinnedAnchors"]
        self.assertEqual(cfg_before["src/app.py::helper"], cfg["src/app.py::helper"])
        self.assertNotEqual(cfg_before["src/app.py::main"], cfg["src/app.py::main"])
        cap = build_handover(self.repo)
        self.assertIn("src/app.py::helper", [a["anchor"] for a in cap["quarantinedStaleAnchors"]])
        self.assertNotIn("src/app.py::main", [a["anchor"] for a in cap["quarantinedStaleAnchors"]])
        self.assertEqual([], wiki_status(self.repo)["userEditedPages"])  # the pin rewrite is recorded, not a hand edit

    def test_unknown_anchor_is_an_error(self):
        res = mb.accept_anchor(self.repo, ["src/app.py::nope", "src/app.py::main"])
        self.assertFalse(res["ok"])
        self.assertEqual(["src/app.py::nope"], res["unknown"])
        self.assertIn("unknown", res["error"])

    def test_changed_check_reports_only_rules_touching_changed_files(self):
        res = mb.changed_check(self.repo, files=["src/app.py"])
        self.assertEqual(1, res["exitCode"])
        self.assertFalse(res["ok"])
        self.assertEqual(["src/app.py"], res["changedFiles"])
        anchors = sorted(a["anchor"] for a in res["affected"])
        self.assertEqual(["src/app.py::helper", "src/app.py::main"], anchors)
        self.assertEqual(2, res["staleCount"])
        self.assertEqual(2, len(res["formatted"].strip().splitlines()))
        self.assertTrue(all(a["source"].startswith("CLAUDE.md:") for a in res["affected"]))
        other = mb.changed_check(self.repo, files=[str(self.repo / "src" / "other.py")])
        self.assertEqual((0, True), (other["exitCode"], other["ok"]))
        self.assertEqual(["src/other.py::thing"], [a["anchor"] for a in other["affected"]])
        self.assertEqual("active", other["affected"][0]["status"])
        none = mb.changed_check(self.repo, files=["README.md"])
        self.assertEqual((0, [], ""), (none["exitCode"], none["affected"], none["formatted"]))
        mb.accept_anchor(self.repo, ["src/app.py::main"])
        after = mb.changed_check(self.repo, files=["src/app.py"])
        self.assertEqual(1, after["staleCount"])
        self.assertEqual(["src/app.py::helper"], [a["anchor"] for a in after["affected"] if a["status"] == "stale"])

    def test_changed_check_needs_files_outside_git(self):
        res = mb.changed_check(self.repo)
        self.assertFalse(res["ok"])
        self.assertEqual(2, res["exitCode"])
        self.assertIn("pass the changed files", res["error"])

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_changed_check_uses_git_diff_and_untracked_files(self):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@e")
        subprocess.run(["git", "init", "-q"], cwd=str(self.repo), check=True)
        (self.repo / ".gitignore").write_text(".memlet/\n")
        subprocess.run(["git", "add", "-A"], cwd=str(self.repo), check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=str(self.repo), check=True, env=env)
        self.assertEqual([], mb.changed_check(self.repo)["changedFiles"])
        (self.repo / "src" / "other.py").write_text("def thing(a):\n    return 2\n")
        (self.repo / "new.txt").write_text("x\n")
        res = mb.changed_check(self.repo)
        self.assertEqual(sorted(["src/other.py", "new.txt"]), sorted(res["changedFiles"]))
        self.assertEqual(["src/other.py::thing"], [a["anchor"] for a in res["affected"]])
        self.assertEqual(1, res["exitCode"])


class SlimIndexRecallTests(unittest.TestCase):
    AGENTS = ("**2026-11-01 Deploy freeze**\nDo not deploy on Fridays.\n\n"
              "**2026-10-01 Old pipeline**\nUse the old pipeline for hotfixes.\n\n"
              "# Standing rules\n\n- Never commit secrets to the repository.\n"
              "- Telegram notifications go through the notify script only.\n")

    def test_rule_moved_by_slim_plan_is_indexed_and_recalled(self):
        from memlet.memlet_index import build_index, load_verified_index
        from memlet.memlet_recall import recall
        from memlet.memlet_slim import RULES_REL, slim_plan
        from memlet.memlet_wiki import discover_instruction_files
        repo = _tmp(self, "p1-slim-repo-")
        (repo / "AGENTS.md").write_text(self.AGENTS)
        rep = build_index(repo, runner=lambda p: "- telegram notify | 2 [src: AGENTS.md:9-9]\n")
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(["AGENTS.md:9-9"], recall(repo, "telegram notification", k=1)["spans"])
        out = _tmp(self, "p1-slim-out-") / "proposal"
        plan = slim_plan(repo, out, keep_recent=1, split_rules=True)
        self.assertTrue(plan["ok"], plan)
        for rel in ("AGENTS.md", RULES_REL, "management/agent-directives-log.md"):  # apply the proposal
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(out / rel, repo / rel)
        self.assertIn(RULES_REL, discover_instruction_files(repo))
        # before re-indexing: the moved rule is not quoted, and recall says why
        before = recall(repo, "telegram notification", k=1)
        self.assertEqual([], before["spans"])
        self.assertIn("changed after the rule index was built", before["formatted"])
        self.assertIn(RULES_REL, before["indexStale"]["newFiles"])
        self.assertTrue(load_verified_index(repo)[2]["staleness"]["stale"])
        rules = (repo / RULES_REL).read_text().splitlines()
        n = next(i for i, l in enumerate(rules, start=1) if "Telegram" in l)
        rep = build_index(repo, runner=lambda p: f"- telegram notify | 2 [src: {RULES_REL}:{n}-{n}]\n")
        self.assertTrue(rep["ok"], rep)
        after = recall(repo, "telegram notification", k=1)
        self.assertEqual([f"{RULES_REL}:{n}-{n}"], after["spans"])
        self.assertIn("Telegram notifications go through the notify script only.", after["formatted"])
        self.assertNotIn("indexStale", after)
        self.assertFalse(load_verified_index(repo)[2]["staleness"]["stale"])

    def test_old_index_without_file_hashes_still_detects_changes(self):
        from memlet.memlet_index import INDEX_PAGE, build_index
        from memlet.memlet_recall import recall
        repo = _tmp(self, "p1-slim-old-")
        (repo / "AGENTS.md").write_text(self.AGENTS)
        build_index(repo, runner=lambda p: "- telegram notify | 2 [src: AGENTS.md:9-9]\n")
        page = repo / INDEX_PAGE
        text = page.read_text()
        start = text.index("<!-- memlet:index-meta ") + len("<!-- memlet:index-meta ")
        meta = json.loads(text[start:text.index(" -->", start)])
        meta.pop("fileSha256")
        page.write_text(text[:start] + json.dumps(meta, sort_keys=True) + text[text.index(" -->", start):])
        self.assertNotIn("indexStale", recall(repo, "telegram", k=1))
        (repo / "AGENTS.md").write_text(self.AGENTS + "- A new rule nobody indexed.\n")
        self.assertTrue(recall(repo, "telegram", k=1)["indexStale"]["inputChanged"])


class InspectorAuditKeyTests(unittest.TestCase):
    def test_real_audit_finding_shows_rule_id_and_doc_path(self):
        from memlet.memlet_inspector import _audit_block, _audit_card
        repo = _tmp(self, "p1-inspector-")
        (repo / "CLAUDE.md").write_text("# R\n\n- Run `scripts/missing_tool.py` before release.\n")
        block = _audit_block(repo)
        f = block["findings"][0]
        self.assertEqual("missing_path", f["rule"])
        self.assertEqual("CLAUDE.md", f["file"])
        self.assertEqual(3, f["line"])
        self.assertEqual("scripts/missing_tool.py", f["reference"])
        html = _audit_card({"audit": block})
        self.assertIn("missing_path", html)
        self.assertIn("CLAUDE.md:3", html)
        from unittest import mock
        legacy = {"findings": [{"rule": "old_rule", "file": "AGENTS.md", "line": 1, "reference": "x.py"}]}
        with mock.patch("memlet.memlet_inspector.audit_repository", return_value=legacy):
            old = _audit_block(repo)["findings"][0]
        self.assertEqual({"rule": "old_rule", "file": "AGENTS.md", "line": 1, "reference": "x.py"}, old)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------------------------------------------
# Fix round 1 (independent Wave 2 review): one regression group per finding, from the reviewer's inputs.

PEM_B64 = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="  # synthetic payload from the review, not a real key


def _git(repo, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return subprocess.run(["git"] + list(args), cwd=str(repo), check=True, capture_output=True, env=env)


class ConcurrencyFixTests(_RepoCase):
    """Finding 1: two concurrent successful approvals must never lose one decision."""

    def test_concurrent_approvals_keep_both_decisions_and_promotion(self):
        import threading
        from unittest import mock
        self.learn()
        ids = self.ids_by_title()
        a, b = self.pick(ids, "main()"), self.pick(ids, "release")
        inside, guard = [0], threading.Lock()
        both_in = threading.Event()
        orig = md._assert_db_replaceable

        def held_open(*args, **kw):  # the reviewer's window: after the generation check, before the write
            orig(*args, **kw)
            with guard:
                inside[0] += 1
                if inside[0] >= 2:
                    both_in.set()
            both_in.wait(timeout=1.0)  # without the queue lock the other writer arrives here and both proceed
            with guard:
                inside[0] -= 1

        results = {}
        with mock.patch.object(md, "_assert_db_replaceable", held_open):
            ts = [threading.Thread(target=lambda i=i: results.__setitem__(i, mb.learn_approve(self.repo, [i], approver="Reviewer")))
                  for i in (a, b)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(30)
        self.assertFalse(both_in.is_set(), "two writers were inside the queue critical section at once")
        self.assertTrue(results[a]["ok"] and results[b]["ok"], results)
        nodes = {n["id"]: n for n in self.queue()["nodes"]}
        self.assertEqual("approved", md.approval_state(nodes[a], "node"))
        self.assertEqual("approved", md.approval_state(nodes[b], "node"))
        self.assertTrue(md.queue_db_in_sync(self.repo / mb.QUEUE_REL, self.repo / mb.DB_REL))
        page = (self.repo / mb.PROMOTED_PAGE).read_text()
        self.assertIn(a, page)
        self.assertIn(b, page)  # the page does not lag the last committed queue

    def test_queue_lock_excludes_another_process(self):
        import sys
        q = self.repo / mb.QUEUE_REL
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from memlet import memlet_dream as md\n"
                "with md.queue_lock(sys.argv[2]):\n    print('locked', flush=True)\n    sys.stdin.readline()\n")
        child = subprocess.Popen([sys.executable, "-c", code, str(Path(__file__).resolve().parent), str(q)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual("locked", child.stdout.readline().strip())
            with self.assertRaises(TimeoutError):
                with md.queue_lock(q, timeout=0.3):
                    pass
            from unittest import mock
            with mock.patch.object(md, "QUEUE_LOCK_TIMEOUT", 0.3):
                blocked = mb.learn_from_transcript(self.repo, self.transcript)
        finally:
            child.stdin.write("\n")
            child.stdin.flush()
            child.wait(10)
        self.assertFalse(blocked["ok"])  # a writer never proceeds while another process holds the queue
        self.assertIn("Dream queue lock", blocked["error"])
        self.assertFalse((self.repo / mb.QUEUE_REL).exists())
        with md.queue_lock(q, timeout=5):
            pass
        self.learn()  # the holder is gone: the lock is free again


class PrivateKeyFixTests(_RepoCase):
    """Finding 2: no private-key material reaches snapshot, queue, DB, promoted page, recall or handover."""

    def _assert_clean(self):
        blobs = [p.read_bytes() for p in (self.repo / ".memlet").rglob("*") if p.is_file()]
        u = mb.unified_recall(self.repo, "expose credentials key", budget=2000)
        cap = build_handover(self.repo, output_format="markdown")
        for text in [b.decode("utf-8", "replace") for b in blobs] + [u["formatted"], json.dumps(u), cap["formatted"]]:
            self.assertNotIn(PEM_B64[:20], text)
            self.assertNotIn("PRIVATE KEY", text)

    def test_jsonl_pkcs8_block_in_a_real_envelope(self):
        _write_jsonl(self.transcript, _records([
            {"type": "user", "timestamp": "2026-11-09T11:00:00Z", "message": {"role": "user", "content":
             "Never expose credentials -----BEGIN PRIVATE KEY-----\n" + PEM_B64 + "\n-----END PRIVATE KEY-----"}}]))
        self.learn()
        nid = self.pick(self.ids_by_title(), "Never expose credentials")
        self.assertTrue(mb.learn_approve(self.repo, [nid], approver="ray")["ok"])
        self.assertIn("Never expose credentials [REDACTED_SECRET_BLOCK]", self.snapshot_file().read_text())
        self._assert_clean()

    def test_log_multiline_keys_keep_line_numbers(self):
        log = self.tdir / "rsa.log"
        log.write_text("2026-11-09T09:00:00Z [user] Never expose -----BEGIN RSA PRIVATE KEY-----\n" + PEM_B64 + "\n"
                       "-----END RSA PRIVATE KEY-----\n"
                       "2026-11-09T09:01:00Z [user] -----BEGIN OPENSSH PRIVATE KEY-----\n" + PEM_B64 + "\n" + PEM_B64 + "\n"
                       "-----END OPENSSH PRIVATE KEY-----\n"
                       "-----BEGIN PGP PRIVATE KEY BLOCK-----\n\n" + PEM_B64 + "\n-----END PGP PRIVATE KEY BLOCK-----\n"
                       "2026-11-09T09:02:00Z [user] Never run the migration twice\n")
        res = mb.learn_from_transcript(self.repo, log)
        self.assertTrue(res["ok"], res)
        stored = (self.repo / res["snapshotFiles"][0]["snapshotFile"]).read_text().splitlines()
        self.assertEqual(len(log.read_text().splitlines()), len(stored))
        self.assertEqual("2026-11-09T09:02:00Z [user] Never run the migration twice", stored[-1])
        nid = self.pick(self.ids_by_title(), "migration twice")
        span = next(n for n in self.queue()["nodes"] if n["id"] == nid)["primaryPin"]["span"]
        self.assertTrue(span.endswith(f":{len(stored)}"), span)  # pins still address the original line
        self.assertTrue(mb.learn_approve(self.repo, [nid], approver="ray")["ok"])
        self._assert_clean()


class ChangedCheckFixTests(unittest.TestCase):
    """Findings 3, 4, 9: rename ends, new nested instruction files, Memlet's own state."""

    def setUp(self):
        self.repo = _tmp(self, "p1-fix-changed-")
        _isolate_git(self, self.repo)
        (self.repo / "mod.py").write_text("def charge(x):\n    return x * 2\n")
        (self.repo / "AGENTS.md").write_text("# Rules\n- Always call `mod.py::charge` safely.\n")
        self.assertTrue(init_project(self.repo)["ok"])

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_rename_reports_the_old_path_stale(self):
        _git(self.repo, "init", "-q")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        _git(self.repo, "mv", "mod.py", "renamed é.py")
        res = mb.changed_check(self.repo)
        self.assertEqual(sorted(["mod.py", "renamed é.py"]), sorted(res["changedFiles"]))  # both ends, literal names
        self.assertEqual(1, res["exitCode"], res)
        self.assertEqual([("mod.py::charge", "stale")], [(a["anchor"], a["status"]) for a in res["affected"]])

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_deleted_file_and_memlet_state_in_git(self):
        _git(self.repo, "init", "-q")
        _git(self.repo, "add", "AGENTS.md", "mod.py")
        _git(self.repo, "commit", "-qm", "base")
        tr = self.repo.parent / (self.repo.name + "-t.jsonl")
        self.addCleanup(lambda: tr.unlink() if tr.exists() else None)
        _write_jsonl(tr, _records())
        self.assertTrue(mb.learn_from_transcript(self.repo, tr)["ok"])
        res = mb.changed_check(self.repo)  # only Memlet's own untracked state changed
        self.assertEqual([], res["changedFiles"])
        self.assertTrue(any(f.startswith(".memlet/dream/") for f in res["ignoredFiles"]))
        self.assertIn(".memlet/config.json", res["ignoredFiles"])
        self.assertEqual((0, ""), (res["exitCode"], res["formatted"]))
        (self.repo / ".memlet" / "wiki" / "notes.md").write_text("# Notes\n\n- Keep `mod.py::charge` pure.\n")
        (self.repo / "mod.py").unlink()
        res = mb.changed_check(self.repo)
        self.assertIn(".memlet/wiki/notes.md", res["changedFiles"])  # hand-written wiki pages are rule sources
        self.assertIn("mod.py", res["changedFiles"])                 # deletion
        stale = sorted(a["source"] for a in res["affected"] if a["status"] == "stale")
        self.assertEqual([".memlet/wiki/notes.md:3", "AGENTS.md:2"], stale)

    def test_new_nested_instruction_file_and_removed_one(self):
        nested = self.repo / "sub" / "AGENTS.md"
        nested.parent.mkdir()
        nested.write_text("# Rules\n- Never call `../mod.py::charge` without retry.\n")  # the reviewer's input
        (self.repo / "mod.py").write_text("def charge(x):\n    return x * 3\n")
        res = mb.changed_check(self.repo, files=["sub/AGENTS.md"])
        self.assertEqual(["sub/AGENTS.md:2"], [a["source"] for a in res["affected"]])
        self.assertEqual(1, res["exitCode"])  # `../mod.py` leaves the repository: stale, never silently clean
        nested.write_text("# Rules\n- Never call `mod.py::nope` without retry.\n")
        res = mb.changed_check(self.repo, files=["sub/AGENTS.md"])
        self.assertEqual([("mod.py::nope", "stale")], [(a["anchor"], a["status"]) for a in res["affected"]])
        # not an instruction file for discovery, so not one here either
        for rel in ("node_modules/x/AGENTS.md", "a/b/c/d/AGENTS.md"):
            p = self.repo / rel
            p.parent.mkdir(parents=True)
            p.write_text("- Never call `mod.py::nope`.\n")
            self.assertEqual([], mb.changed_check(self.repo, files=[rel])["affected"], rel)
        nested.unlink()
        res = mb.changed_check(self.repo, files=["sub/AGENTS.md"])
        self.assertEqual(["sub/AGENTS.md"], res["removedInstructionFiles"])
        self.assertEqual((0, []), (res["exitCode"], res["affected"]))
        self.assertIn("REMOVED instruction file `sub/AGENTS.md`", res["formatted"])


class BlankApproverFixTests(_RepoCase):
    """Finding 6: an empty or whitespace approver is refused with no mutation."""

    def test_blank_approver_refused_without_mutation(self):
        self.learn()
        nid = self.pick(self.ids_by_title(), "main()")
        before = {p: p.read_bytes() for p in (self.repo / ".memlet").rglob("*") if p.is_file()}
        for who in ("", "   ", None):
            for call in (mb.learn_approve, mb.learn_reject):
                res = call(self.repo, [nid], approver=who)
                self.assertFalse(res["ok"])
                self.assertEqual(2, res["exitCode"])
                self.assertIn("approver", res["error"])
                self.assertNotIn("approverDefaulted", res)
        after = {p: p.read_bytes() for p in (self.repo / ".memlet").rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.repo / mb.PROMOTED_PAGE).exists())
        self.assertEqual([], mb.unified_recall(self.repo, "main test suite")["sections"]["dream"])


class RecallFixTests(unittest.TestCase):
    """Finding 8 (zero-match queries) and lead finding A (deterministic rules without an index)."""

    def setUp(self):
        self.repo = _tmp(self, "p1-fix-recall-")
        (self.repo / "AGENTS.md").write_text(
            "# Rules\n\n- Always run the unit tests before committing,\n  including the slow integration suite.\n"
            "- Sales must approve before any deploy.\n- Telegram notifications go through scripts/notify.sh.\n\n"
            "## Secrets\n\n- Never paste API_TOKEN=abcdefgh1234567890 into chat.\n")

    def test_zero_match_query_returns_no_rule_matches_only_labelled_safety(self):
        from memlet.memlet_index import build_index
        build_index(self.repo, runner=lambda p: "- tests before commit | 1 [src: AGENTS.md:3-4]\n"
                                                "- sales approval | 1 [src: AGENTS.md:5-5]\n")
        u = mb.unified_recall(self.repo, "platypusquantum")
        self.assertEqual([], u["sections"]["rules"])
        self.assertEqual("no rule matches the query", u["sources"]["rules"]["reason"])
        self.assertEqual(sorted(["AGENTS.md:3-4", "AGENTS.md:5-5"]), sorted(i["source"] for i in u["sections"]["safety"]))
        self.assertIn("Always-on safety rules", u["formatted"])
        self.assertNotIn("matching the query", u["formatted"])
        hit = mb.unified_recall(self.repo, "sales deploy approval")
        self.assertEqual(["AGENTS.md:5-5"], [i["source"] for i in hit["sections"]["rules"]])
        self.assertEqual(["AGENTS.md:3-4"], [i["source"] for i in hit["sections"]["safety"]])

    def test_plain_init_gives_parsed_rules_without_any_model_call(self):
        from memlet.memlet_recall import recall
        self.assertTrue(init_project(self.repo)["ok"])
        u = mb.unified_recall(self.repo, "unit tests integration suite")
        self.assertTrue(u["sources"]["rules"]["available"], u["sources"])
        self.assertEqual("parsed", u["sources"]["rules"]["mode"])
        first = u["sections"]["rules"][0]
        self.assertEqual("AGENTS.md:3-4", first["source"])  # the whole item, continuation line included
        import hashlib
        lines = (self.repo / "AGENTS.md").read_text().splitlines()
        self.assertEqual(hashlib.sha256("\n".join(lines[2:4]).encode()).hexdigest(), first["sha256"])
        self.assertIn("parsed from instruction files; not an indexed/reviewed rule index", u["formatted"])
        self.assertEqual([], u["sections"]["safety"])
        self.assertFalse(u["sources"]["safety"]["available"])
        secret = mb.unified_recall(self.repo, "paste token chat")
        self.assertEqual(["AGENTS.md:10-10"], [i["source"] for i in secret["sections"]["rules"]])
        self.assertNotIn("abcdefgh1234567890", json.dumps(secret))
        for budget in (40, 60, 90):
            self.assertLessEqual(mb.unified_recall(self.repo, "unit tests", budget=budget)["tokenEstimate"], budget)
        self.assertEqual([], mb.unified_recall(self.repo, "platypusquantum")["sections"]["rules"])
        # the CLI's rule-recall keeps its index-only behaviour and key set
        r = recall(self.repo, "unit tests", k=3)
        self.assertEqual([], r["spans"])
        self.assertEqual({"ok", "command", "repoPath", "k", "stale", "spans", "spanKeys", "scores", "tokenEstimate",
                          "formatted"}, set(r))


class SlimOwnershipFixTests(unittest.TestCase):
    """Lead finding B: only files `memlet slim-plan` produced count as instructions."""

    def test_foreign_management_files_are_not_instructions(self):
        from memlet.memlet_slim import RULES_HEADER, RULES_REL
        from memlet.memlet_wiki import discover_instruction_files, is_instruction_path
        repo = _tmp(self, "p1-fix-slim-")
        (repo / "AGENTS.md").write_text("# Rules\n\n- Keep it simple.\n")
        (repo / "management").mkdir()
        (repo / "management" / "agent-sales.md").write_text("# Sales notes 2024\n\n- Never discount below 10%.\n")
        (repo / RULES_REL).write_text("# Our own agent rules\n\n- Not written by slim.\n")
        self.assertEqual(["AGENTS.md"], discover_instruction_files(repo))
        self.assertEqual(["AGENTS.md"], init_project(repo)["instructionFiles"])
        self.assertFalse(is_instruction_path(repo, "management/agent-sales.md"))
        self.assertFalse(is_instruction_path(repo, RULES_REL))
        (repo / RULES_REL).write_text(RULES_HEADER + "\n\n- Moved verbatim.\n")
        self.assertEqual(["AGENTS.md", RULES_REL], discover_instruction_files(repo))
        self.assertTrue(is_instruction_path(repo, RULES_REL))


# ---------------------------------------------------------------------------------------------------------------
# Wave 3 review R2: learn_from_transcript input bounds (unit C1 fix round 1)

import time as _time  # noqa: E402
from unittest import mock as _mock  # noqa: E402

_LOCKS = {}


def setUpModule():
    _LOCKS["prior"] = os.environ.get("MEMLET_LOCK_DIR")
    _LOCKS["dir"] = tempfile.mkdtemp(prefix="p1-bridge-locks-")
    os.environ["MEMLET_LOCK_DIR"] = _LOCKS["dir"]


def tearDownModule():
    if _LOCKS.get("prior") is None:
        os.environ.pop("MEMLET_LOCK_DIR", None)
    else:
        os.environ["MEMLET_LOCK_DIR"] = _LOCKS["prior"]
    shutil.rmtree(_LOCKS["dir"], ignore_errors=True)


class TranscriptBoundsTests(_RepoCase):
    def snapshot_text(self):
        d = self.repo / mb.SNAPSHOT_REL
        return "".join(p.read_text() for p in d.iterdir()) if d.is_dir() else ""

    def test_long_visible_message_is_skipped_whole_and_fast(self):
        _write_jsonl(self.transcript, _records([{"type": "user", "timestamp": "2026-11-09T10:00:00Z", "message": {
            "role": "user", "content": "Never call main() in app.py " + "z" * 100000}}]))
        start = _time.monotonic()
        res = mb.learn_from_transcript(self.repo, self.transcript)
        self.assertLess(_time.monotonic() - start, 3.0)
        self.assertTrue(res["ok"], res)
        self.assertEqual(1, res["skipped"]["overlongMessages"])
        self.assertNotIn("zzzz", self.snapshot_text())
        self.assertIn("release branch", self.snapshot_text())  # the ordinary messages are still learned
        self.assertTrue(any("transcript bounds" in n for n in res["notices"]))

    def test_long_log_lines_and_raw_jsonl_lines_are_skipped_whole(self):
        log = self.tdir / "session.log"
        log.write_text("2026-11-09T10:00:00Z RULE: never call main() in app.py\n" + ("z" * 1048576 + "\n") * 3)
        res = mb.learn_from_transcript(self.repo, log)
        self.assertTrue(res["ok"], res)
        self.assertEqual(3, res["skipped"]["overlongLines"])
        self.assertNotIn("zzzz", self.snapshot_text())
        with _mock.patch.object(mb, "TRANSCRIPT_MAX_LINE_CHARS", 1000):
            _write_jsonl(self.transcript, _records([{"type": "user", "message": {"role": "user", "content": "Y" * 2000}}]))
            res = mb.learn_from_transcript(self.repo, self.transcript)
        self.assertTrue(res["ok"], res)
        self.assertEqual(1, res["skipped"]["overlongLines"])
        self.assertNotIn("YYYY", self.snapshot_text())

    def test_size_total_and_work_caps(self):
        with _mock.patch.object(mb, "TRANSCRIPT_MAX_FILE_BYTES", 100):
            res = mb.learn_from_transcript(self.repo, self.transcript)
        self.assertFalse(res["ok"])
        self.assertEqual(2, res["exitCode"])
        self.assertEqual(1, res["skipped"]["overSizeFiles"])
        self.assertFalse((self.repo / mb.SNAPSHOT_REL).exists())
        second = self.tdir / "second.jsonl"
        _write_jsonl(second, _records())
        size = self.transcript.stat().st_size
        with _mock.patch.object(mb, "TRANSCRIPT_MAX_TOTAL_BYTES", size + 10):
            res = mb.learn_from_transcript(self.repo, self.tdir)
        self.assertTrue(res["ok"], res)
        self.assertEqual(1, res["skipped"]["overTotalSizeFiles"])
        self.assertEqual(1, len(res["snapshotFiles"]))
        with _mock.patch.object(mb, "TRANSCRIPT_MAX_WORK_CHARS", 10):
            third = self.tdir / "third.jsonl"
            _write_jsonl(third, _records())
            res = mb.learn_from_transcript(self.repo, third)
        self.assertTrue(res["ok"], res)
        self.assertEqual(1, res["skipped"]["overWorkCapFiles"])
        self.assertEqual([], res["snapshotFiles"])
