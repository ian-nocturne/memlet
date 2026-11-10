"""Memlet 9.1 D2: automatic anchor suggestions, sidecar anchors and the exact-parser seam.

Every fixture is a scratch repository with its own HOME, TMPDIR, GIT_CEILING_DIRECTORIES and lock directory; no test
reads or writes the real home, calls a model or the network.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import memory_cli
from memlet import code_anchors as ca
from memlet import memlet_anchors as ma
from memlet import memlet_bridge as mb
from memlet import memlet_wiki as mw

AUTH = '''def validate_token(token, *, leeway=0):
    """Check a bearer token."""
    return bool(token)


class Session:
    def save(self):
        return 1

    def refresh_session(self):
        return 2


def run():
    pass
'''
BILLING = '''def charge_customer(customer, amount):
    return amount


def save(x):
    return x


def apply_discount(order, rate):
    return order
'''
TEST_HELPERS = '''def charge_customer(c, a):
    return 0


def test_anything():
    assert True
'''
AGENTS = """# Rules
- Always call `validate_token()` before trusting a request.
- In `src/billing.py`, `charge_customer` must be idempotent.
- Never call `charge_customer` twice in one request.
- Use `run` to start the service.
- Persist with `save`.
- Be kind to reviewers and validate the token before merging.
- Already anchored: `src/auth.py::validate_token` stays as written.
- Discounts go through apply_discount() only.
- Sessions refresh with `Session.refresh_session`.
- `src/auth.py#Session` is the only session type.
"""


def _env(tmp: Path):
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(home), TMPDIR=str(tmp), GIT_CEILING_DIRECTORIES=str(tmp),
               MEMLET_LOCK_DIR=str(tmp / "locks"), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    for k in ("CODEX_THREAD_ID", "MEMLET_PARENT_SESSION_ID", "ANTIGRAVITY_AGENT"):
        env.pop(k, None)
    return env


class _Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v91-anchors-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(str(self.tmp), True))
        iso = mock.patch.dict(os.environ, _env(self.tmp), clear=True)
        iso.start()
        self.addCleanup(iso.stop)
        # the optional exact parsers stay out of these tests (the seam has its own tests below)
        prov = mock.patch.dict(ca._EXACT_PROVIDERS, {}, clear=True)
        prov.start()
        self.addCleanup(prov.stop)
        auto = mock.patch.dict(ca._EXACT_AUTOLOAD, {"done": True})
        auto.start()
        self.addCleanup(auto.stop)
        ca._EXACT_CACHE.clear()
        self.root = self.tmp / "repo"
        self.root.mkdir()

    def tearDown_files(self):
        import shutil
        shutil.rmtree(str(self.root))
        self.root.mkdir()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def fixture(self):
        self.write("src/auth.py", AUTH)
        self.write("src/billing.py", BILLING)
        self.write("tests/test_billing.py", TEST_HELPERS)
        self.write("AGENTS.md", AGENTS)

    def tree(self, include_memlet=False):
        out = {}
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root).as_posix()
            if not include_memlet and (rel == ".memlet" or rel.startswith(".memlet/")):
                continue
            out[rel] = p.read_bytes() if p.is_file() else None
        return out

    def by_anchor(self, res):
        return {s["anchor"]: s for s in res["suggestions"]}

    def all_ids(self):
        return [s["id"] for s in ma.suggest_anchors(self.root, limit=None)["suggestions"]]

    def accept_all(self):
        """Accept every current suggestion BY ID (a human decision; --all-unique takes only file+symbol)."""
        ids = [s["id"] for s in ma.suggest_anchors(self.root, limit=None)["suggestions"]]
        res = ma.accept_suggestions(self.root, ids)
        self.assertTrue(res["ok"], res)
        return res


class SuggestionPrecisionTests(_Repo):
    def test_single_definition_file_qualified_duplicated_and_prose_mentions(self):
        self.fixture()
        res = ma.suggest_anchors(self.root)
        got = {(s["source"], s["line"], s["anchor"], s["confidence"]) for s in res["suggestions"]}
        self.assertEqual({
            ("AGENTS.md", 2, "src/auth.py::validate_token", "single-definition-found"),        # backticked call form, defined once
            ("AGENTS.md", 3, "src/billing.py::charge_customer", "file+symbol"),     # file named in the same rule
            ("AGENTS.md", 9, "src/billing.py::apply_discount", "single-definition-found"),    # prose `name()` form
            ("AGENTS.md", 10, "src/auth.py::Session.refresh_session", "single-definition-found"),  # dotted name
            ("AGENTS.md", 11, "src/auth.py::Session", "file+symbol"),               # `path#Symbol`
        }, got)
        amb = {(a["line"], a["mention"]): a for a in res["ambiguous"]}
        # same name in tests and source: reported with both candidates, never guessed
        self.assertEqual("defined_in_tests_and_source", amb[(4, "charge_customer")]["reason"])
        self.assertEqual({"src/billing.py::charge_customer", "tests/test_billing.py::charge_customer"},
                         {c["anchor"] for c in amb[(4, "charge_customer")]["candidates"]})
        self.assertEqual("multiple_definitions", amb[(6, "save")]["reason"])
        self.assertEqual(2, amb[(6, "save")]["candidateCount"])
        # a common word that happens to be defined once is not anchored on its own
        self.assertEqual("name_too_generic_to_anchor_without_a_file", amb[(5, "run")]["reason"])
        # prose without backticks or `name()` ("validate the token") and written anchors are left alone
        self.assertFalse([s for s in res["suggestions"] if s["line"] in (7, 8)])
        self.assertEqual(1, res["scanned"]["rulesWithWrittenAnchors"])
        self.assertFalse(res["truncated"])

    def test_suggestion_shape_ids_and_determinism(self):
        self.fixture()
        a, b = ma.suggest_anchors(self.root), ma.suggest_anchors(self.root)
        self.assertEqual(a, b)
        s = self.by_anchor(a)["src/auth.py::validate_token"]
        line = AGENTS.splitlines()[1]
        body = line[2:]
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), s["ruleSha256"])
        self.assertEqual(["Rules"], s["sectionPath"])
        self.assertEqual(ma._section_sha(["Rules"]), s["sectionSha256"])
        self.assertEqual(ma.suggestion_id("AGENTS.md", s["ruleSha256"], s["anchor"], s["sectionSha256"]), s["id"])
        self.assertRegex(s["id"], r"^anc-[0-9a-f]{12}$")
        for k in ("id", "source", "line", "ruleSha256", "rule", "anchor", "confidence", "reason"):
            self.assertIn(k, s)
        self.assertEqual({"path": "src/auth.py", "symbol": "validate_token", "line": 1, "kind": "function", "test": False},
                         s["target"])
        # the rule is shown next to the symbol's file:line
        self.assertIn("`AGENTS.md:2`: Always call `validate_token()`", a["formatted"])
        self.assertIn("at `src/auth.py:1` (function)", a["formatted"])
        # the id is stable when the rule only shifts under the same headings
        self.write("AGENTS.md", "# Intro\n\n" + AGENTS)
        moved = self.by_anchor(ma.suggest_anchors(self.root))["src/auth.py::validate_token"]
        self.assertEqual((s["id"], 4), (moved["id"], moved["line"]))

    def test_dependency_folders_and_nested_repositories_are_not_repository_code(self):
        self.fixture()
        self.write("node_modules/pkg/index.js", "function validate_token(x) { return x; }\n")
        self.write("dist/app.js", "function validate_token(x) { return x; }\n")
        self.write("nested/.git/HEAD", "ref: refs/heads/main\n")
        self.write("nested/copy.py", "def validate_token(x):\n    return x\n")
        res = ma.suggest_anchors(self.root)
        self.assertEqual("single-definition-found", self.by_anchor(res)["src/auth.py::validate_token"]["confidence"])

    def test_skipped_code_is_never_a_target_and_counts_against_a_single_definition(self):
        for rel, text in (("vendor/lib.py", "def validate_token(x):\n    return x\n"),
                          ("gen/api_client.py", "# @generated by protoc. DO NOT EDIT.\ndef validate_token(x):\n    return x\n"),
                          ("web/bundle.min.js", "function validate_token(x) { return x; }\n"),
                          (".hidden/tool.py", "def validate_token(x):\n    return x\n")):
            with self.subTest(rel=rel):
                self.tearDown_files()
                self.fixture()
                self.write(rel, text)
                res = ma.suggest_anchors(self.root)
                self.assertNotIn("src/auth.py::validate_token", self.by_anchor(res))
                amb = [a for a in res["ambiguous"] if a["mention"] == "validate_token" and a["line"] == 2]
                self.assertEqual(["other_bindings_of_the_name"], [a["reason"] for a in amb])
                self.assertIn(rel, json.dumps(amb[0]["candidates"]))

    def test_symlinked_code_and_rule_files_are_not_read(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "evil.py").write_text("def exfiltrate_secrets():\n    pass\n")
        self.write("src/ok.py", "def harmless_helper():\n    pass\n")
        os.symlink(str(outside), str(self.root / "linked"))
        os.symlink(str(outside / "evil.py"), str(self.root / "src" / "evil.py"))
        self.write("AGENTS.md", "- Call `exfiltrate_secrets()` and `harmless_helper()`.\n- In `src/evil.py` use `exfiltrate_secrets`.\n")
        res = ma.suggest_anchors(self.root)
        self.assertEqual({"src/ok.py::harmless_helper"}, set(self.by_anchor(res)))

    def test_wiki_pages_are_sources_but_generated_pages_are_not(self):
        self.fixture()
        self.write("AGENTS.md", "# Rules\n- Nothing here.\n")
        self.write(".memlet/wiki/notes.md", "# Notes\n- Tokens: call `validate_token()` first.\n")
        self.write(".memlet/wiki/imported-rules.md", "# Imported Rules\n- `apply_discount()` copy.\n")
        res = ma.suggest_anchors(self.root)
        self.assertEqual({(".memlet/wiki/notes.md", "src/auth.py::validate_token")},
                         {(s["source"], s["anchor"]) for s in res["suggestions"]})

    def test_repeated_rule_text_is_ambiguous(self):
        self.fixture()
        self.write("AGENTS.md", "- Always call `validate_token()`.\n- Other.\n- Always call `validate_token()`.\n")
        res = ma.suggest_anchors(self.root)
        self.assertEqual([], res["suggestions"])
        self.assertEqual({"rule_text_repeated_in_file"}, {a["reason"] for a in res["ambiguous"]})


class AcceptTests(_Repo):
    def test_accept_changes_no_user_file_and_records_the_entry(self):
        self.fixture()
        mw.init_project(self.root)
        before = self.tree()
        before_pins = json.loads((self.root / mw.CONFIG_PATH).read_text())["pinnedAnchors"]
        sid = self.by_anchor(ma.suggest_anchors(self.root))["src/auth.py::validate_token"]["id"]
        res = ma.accept_suggestions(self.root, [sid])
        self.assertTrue(res["ok"], res)
        self.assertEqual([sid], res["accepted"])
        self.assertEqual(before, self.tree())  # byte-identical outside .memlet/
        cfg = json.loads((self.root / mw.CONFIG_PATH).read_text())
        [entry] = cfg[ma.SIDECAR_KEY]
        self.assertEqual({"id", "source", "sectionPath", "sectionSha256", "line", "ruleSha256", "anchor", "pin"}, set(entry))
        self.assertEqual(("AGENTS.md", 2, "src/auth.py::validate_token"), (entry["source"], entry["line"], entry["anchor"]))
        self.assertEqual(mw.PYTHON_HASH_BASIS, entry["pin"]["basis"])
        self.assertEqual(before_pins, cfg["pinnedAnchors"])  # written pins are not touched
        with contextlib.closing(sqlite3.connect(str(self.root / mw.LEDGER_PATH))) as conn:
            rows = conn.execute("SELECT entry_id, entry_sha256, event_type FROM sidecar_anchor_events").fetchall()
        self.assertEqual([(sid, ma.entry_digest(entry), "accept")], rows)
        self.assertEqual([sid], [e["id"] for e in ma.sidecar_anchors(self.root)])
        # accepted suggestions are no longer suggested
        self.assertNotIn(sid, {s["id"] for s in ma.suggest_anchors(self.root)["suggestions"]})

    def test_all_unique_on_an_uninitialised_repo_writes_only_memlet(self):
        self.fixture()
        before = self.tree()
        res = self.accept_all()
        self.assertEqual(5, len(res["accepted"]))
        self.assertEqual(before, self.tree())
        self.assertEqual(5, len(ma.sidecar_anchors(self.root)))

    def test_dry_run_writes_nothing_at_all(self):
        self.fixture()
        for initialised in (False, True):
            if initialised:
                mw.init_project(self.root)
            before = self.tree(include_memlet=True)
            res = ma.accept_suggestions(self.root, self.all_ids(), dry_run=True)
            self.assertTrue(res["ok"] and res["dryRun"], res)
            self.assertEqual(5, len(res["accepted"]))
            self.assertTrue(all(a["op"].startswith("would-") for a in res["actions"]))
            self.assertEqual(before, self.tree(include_memlet=True))
            self.assertEqual([], ma.sidecar_anchors(self.root))
        sid = ma.suggest_anchors(self.root)["suggestions"][0]["id"]
        self.accept_all()
        before = self.tree(include_memlet=True)
        res = ma.drop_sidecar_anchors(self.root, [sid], dry_run=True)
        self.assertEqual([sid], res["dropped"])
        self.assertEqual(before, self.tree(include_memlet=True))

    def test_unknown_and_ambiguous_ids_are_refused(self):
        self.fixture()
        before = self.tree(include_memlet=True)
        res = ma.accept_suggestions(self.root, ["anc-000000000000"])
        self.assertFalse(res["ok"])
        self.assertEqual(["anc-000000000000"], res["unknown"])
        self.assertEqual(before, self.tree(include_memlet=True))
        self.assertFalse(ma.accept_suggestions(self.root)["ok"])  # neither ids nor --all-unique

    def test_accept_reuses_an_existing_written_pin_and_drop_removes_entries(self):
        self.fixture()
        mw.init_project(self.root)  # pins the written `src/auth.py::validate_token` (AGENTS.md:8)
        written = mw._load_config_pins(self.root)["src/auth.py::validate_token"]
        res = self.accept_all()
        sid = self.by_anchor({"suggestions": res["entries"]})["src/auth.py::validate_token"]["id"]
        self.assertIn(sid, res["reusedExistingPin"])
        entry = next(e for e in ma.sidecar_anchors(self.root) if e["id"] == sid)
        self.assertEqual(written["astHash"], entry["pin"]["ast"])
        drop = ma.drop_sidecar_anchors(self.root, [sid, "anc-ffffffffffff"])
        self.assertEqual(([sid], ["anc-ffffffffffff"], False), (drop["dropped"], drop["unknown"], drop["ok"]))
        self.assertNotIn(sid, {e["id"] for e in ma.sidecar_anchors(self.root)})
        self.assertIn(sid, {s["id"] for s in ma.suggest_anchors(self.root)["suggestions"]})

    def test_accept_waits_for_the_shared_write_lock(self):
        self.fixture()
        mw.init_project(self.root)
        import threading
        order = []
        with mw.memlet_write_lock(self.root):
            ids = self.all_ids()
            t = threading.Thread(target=lambda: order.append(ma.accept_suggestions(self.root, ids)["ok"]))
            t.start()
            time.sleep(0.3)
            order.append("holder")  # another writer (pin / promote) still holds the lock and changes the config
            cfg = json.loads((self.root / mw.CONFIG_PATH).read_text())
            cfg["writtenMeanwhile"] = True
            mw.write_managed_text(self.root, mw.CONFIG_PATH, json.dumps(cfg))
        t.join(30)
        self.assertEqual(["holder", True], order)
        cfg = json.loads((self.root / mw.CONFIG_PATH).read_text())
        self.assertTrue(cfg.get("writtenMeanwhile"))  # accept re-read the config under the lock: nothing lost
        self.assertEqual(5, len(cfg[ma.SIDECAR_KEY]))

    def test_hand_written_do_not_edit_comment_does_not_hide_a_definition(self):
        self.fixture()
        self.write("src/other.py", "# Do not edit this lightly.\ndef validate_token(x):\n    return x\n")
        res = ma.suggest_anchors(self.root)
        self.assertNotIn("src/auth.py::validate_token", self.by_anchor(res))
        self.assertIn("multiple_definitions", {a["reason"] for a in res["ambiguous"] if a["mention"] == "validate_token"})

    def test_unreadable_or_symlinked_state_is_refused(self):
        self.fixture()
        (self.root / ".memlet").mkdir()
        (self.root / mw.CONFIG_PATH).write_text("{not json")
        before = self.tree(include_memlet=True)
        res = ma.accept_suggestions(self.root, all_unique=True)
        self.assertFalse(res["ok"])
        self.assertEqual(before, self.tree(include_memlet=True))
        (self.root / mw.CONFIG_PATH).unlink()
        target = self.tmp / "elsewhere.json"
        target.write_text("{}")
        os.symlink(str(target), str(self.root / mw.CONFIG_PATH))
        res = ma.accept_suggestions(self.root, all_unique=True)
        self.assertFalse(res["ok"])
        self.assertEqual("{}", target.read_text())


class SidecarLifecycleTests(_Repo):
    def setUp(self):
        super().setUp()
        self.fixture()
        mw.init_project(self.root)
        self.accept_all()

    def handover(self):
        return mw.build_handover(self.root, write=False)

    def sid(self, anchor):
        return next(e["id"] for e in ma.sidecar_anchors(self.root) if e["anchor"] == anchor)

    def test_accepted_rule_is_active_and_marked_suggested_everywhere(self):
        h = self.handover()
        active = {a["source"]: a for a in h["activeConstraints"]}
        self.assertEqual(["src/auth.py::validate_token"], active["AGENTS.md:2"]["anchors"])
        self.assertEqual(["src/auth.py::validate_token"], active["AGENTS.md:2"]["suggestedAnchors"])
        self.assertNotIn("suggestedAnchors", active["AGENTS.md:8"])  # the written anchor is not "suggested"
        # the file mention that resolved a file+symbol anchor became that anchor (not a separate path-exists ref)
        self.assertEqual(["src/billing.py::charge_customer"], active["AGENTS.md:3"]["anchors"])
        self.assertTrue(mw.verify_handover_checksum(h))
        rep = mw.pinned_anchor_report(self.root)
        a = {x["anchor"]: x for x in rep["anchors"]}
        self.assertEqual("active", a["src/billing.py::charge_customer"]["status"])
        self.assertTrue(a["src/billing.py::charge_customer"]["suggested"])
        self.assertEqual(["AGENTS.md:3"], a["src/billing.py::charge_customer"]["sources"])
        self.assertNotIn("suggested", a["src/auth.py::validate_token"])
        listed = ma.list_sidecar_anchors(self.root)
        self.assertEqual({"attached": 5, "detached": 0, "rejected": 0}, listed["counts"])
        self.assertEqual({"active"}, {e["status"] for e in listed["entries"]})

    def test_signature_change_turns_the_sidecar_rule_stale_like_a_written_anchor(self):
        self.write("src/billing.py", BILLING.replace("def charge_customer(customer, amount):",
                                                     "def charge_customer(customer, amount, currency):"))
        h = self.handover()
        self.assertNotIn("AGENTS.md:3", {a["source"] for a in h["activeConstraints"]})
        self.assertIn({"anchor": "src/billing.py::charge_customer", "reason": "signature_drift", "suggested": True,
                       "sidecarIds": [self.sid("src/billing.py::charge_customer")]}, h["quarantinedStaleAnchors"])
        # recall sets it apart exactly as it does a written stale anchor
        rec = mb.unified_recall(self.root, "charge customer idempotent")
        stale = [i for i in rec["sections"]["staleRules"] if i["source"].startswith("AGENTS.md:3")]
        self.assertEqual(1, len(stale), rec["sections"])
        self.assertEqual([{"anchor": "src/billing.py::charge_customer", "status": "stale", "reason": "signature_drift",
                           "suggested": True, "sidecarId": self.sid("src/billing.py::charge_customer")}], stale[0]["anchors"])
        self.assertNotIn("AGENTS.md:3-3", {i["source"] for i in rec["sections"]["rules"]})
        # the changed-file hook check and `memlet check`
        chk = mb.changed_check(self.root, files=["src/billing.py"])
        self.assertEqual(1, chk["exitCode"])
        self.assertIn(("src/billing.py::charge_customer", "stale", "AGENTS.md:3"),
                      {(a["anchor"], a["status"], a["source"]) for a in chk["affected"]})
        from memlet.audit import audit_repository
        audit = audit_repository(self.root)
        self.assertIn(("pinned_anchor_stale", "AGENTS.md", 3),
                      {(f["ruleId"], f["docPath"], f["line"]) for f in audit["findings"]})

    def test_body_change_is_compatible_unverified(self):
        self.write("src/billing.py", BILLING.replace("    return amount\n", "    return amount * 1\n", 1))
        h = self.handover()
        self.assertIn({"anchor": "src/billing.py::charge_customer", "reason": "body_changed_signature_compatible",
                       "suggested": True, "sidecarIds": [self.sid("src/billing.py::charge_customer")]}, h["reverifyBeforeUse"])
        self.assertNotIn("AGENTS.md:3", {a["source"] for a in h["activeConstraints"]})

    def test_rule_edit_detaches_and_is_never_reattached(self):
        self.write("AGENTS.md", AGENTS.replace("must be idempotent", "should usually be idempotent"))
        idx = ma.sidecar_index(self.root)
        detached = {e["anchor"]: e["reason"] for e in idx["detached"]}
        self.assertEqual({"src/billing.py::charge_customer": "rule_text_changed_or_removed"}, detached)
        h = self.handover()
        self.assertNotIn("AGENTS.md:3", {a["source"] for a in h["activeConstraints"]})
        self.assertEqual("unanchored", next(r for r in mw._rules_from_files(self.root, ["AGENTS.md"])
                                            if r["line"] == 3).get("status", "unanchored"))
        self.assertIn("rule_text_changed_or_removed", json.dumps(mw.pinned_anchor_report(self.root)["sidecar"]))
        from memlet.audit import audit_repository
        self.assertIn("sidecar_anchor_detached", {f["ruleId"] for f in audit_repository(self.root)["findings"]})
        # the edited rule gets a fresh suggestion with a new id; the old entry stays detached until dropped
        fresh = {s["anchor"]: s for s in ma.suggest_anchors(self.root)["suggestions"]}
        self.assertIn("src/billing.py::charge_customer", fresh)
        self.write("AGENTS.md", AGENTS)  # restoring the exact text re-attaches: it is the same text, not different text
        self.assertEqual([], ma.sidecar_index(self.root)["detached"])

    def test_line_shift_stays_attached_and_moving_to_another_file_detaches(self):
        self.write("AGENTS.md", "# Preface\n\nSome words.\n" + AGENTS)
        idx = ma.sidecar_index(self.root)
        moved = {e["anchor"]: (e["currentLine"], e["lineShifted"]) for e in idx["attached"]}
        self.assertEqual((6, True), moved["src/billing.py::charge_customer"])  # same heading chain: a line shift
        h = self.handover()
        self.assertIn("AGENTS.md:6", {a["source"] for a in h["activeConstraints"]})
        lines = AGENTS.splitlines(True)
        self.write("AGENTS.md", "".join(l for l in lines if "charge_customer` must" not in l))
        self.write("CLAUDE.md", "".join(l for l in lines if "charge_customer` must" in l))
        idx = ma.sidecar_index(self.root)
        self.assertIn(("src/billing.py::charge_customer", "rule_text_changed_or_removed"),
                      {(e["anchor"], e["reason"]) for e in idx["detached"]})
        self.assertNotIn("CLAUDE.md:1", {a["source"] for a in self.handover()["activeConstraints"]})

    def test_hand_edited_entry_is_rejected(self):
        cfg = json.loads((self.root / mw.CONFIG_PATH).read_text())
        e = next(x for x in cfg[ma.SIDECAR_KEY] if x["anchor"] == "src/billing.py::charge_customer")
        e["pin"]["ast"] = "0" * 16  # e.g. "re-pinning" by hand to hide drift
        (self.root / mw.CONFIG_PATH).write_text(json.dumps(cfg))
        idx = ma.sidecar_index(self.root)
        self.assertIn((e["id"], "not_recorded_by_accept"), {(d["id"], d["reason"]) for d in idx["detached"]})
        self.assertNotIn("AGENTS.md:3", {a["source"] for a in self.handover()["activeConstraints"]})
        self.assertEqual("rejected", next(r for r in ma.list_sidecar_anchors(self.root)["entries"] if r["id"] == e["id"])["state"])

    def test_forged_entry_cannot_make_an_unrelated_rule_look_verified(self):
        """Even an attacker who writes both the config entry and its ledger record cannot point a harmless rule at
        a symbol it does not mention."""
        rule = "Be kind to reviewers and validate the token before merging."
        sha = hashlib.sha256(rule.encode()).hexdigest()
        anchor = "src/auth.py::validate_token"
        fp = mw._current_fingerprint(self.root, "src/auth.py", "validate_token")
        entry = {"id": ma.suggestion_id("AGENTS.md", sha, anchor, ma._section_sha(["Rules"])), "source": "AGENTS.md",
                 "line": 7, "sectionPath": ["Rules"], "sectionSha256": ma._section_sha(["Rules"]), "ruleSha256": sha,
                 "anchor": anchor,
                 "pin": {"ast": fp["ast"], "sig": fp["sig"], "req": fp["req"], "basis": fp["basis"]}}
        cfg = json.loads((self.root / mw.CONFIG_PATH).read_text())
        cfg[ma.SIDECAR_KEY].append(entry)
        (self.root / mw.CONFIG_PATH).write_text(json.dumps(cfg))
        with contextlib.closing(sqlite3.connect(str(self.root / mw.LEDGER_PATH))) as conn, conn:
            conn.execute("INSERT INTO sidecar_anchor_events (entry_id, entry_sha256, event_type, recorded_at) "
                         "VALUES (?, ?, 'accept', 'x')", (entry["id"], ma.entry_digest(entry)))
        idx = ma.sidecar_index(self.root)
        self.assertIn((entry["id"], "rule_does_not_mention_symbol"), {(d["id"], d["reason"]) for d in idx["detached"]})
        self.assertNotIn("AGENTS.md:7", {a["source"] for a in self.handover()["activeConstraints"]})

    def test_malformed_sidecar_config_never_breaks_readers(self):
        for bad in ("nope", [1, None, {"id": "x"}], [{"id": "anc-000000000000", "source": "../x", "ruleSha256": "a" * 64,
                                                       "anchor": "/etc/passwd::x", "pin": {"ast": "zz"}}]):
            cfg = json.loads((self.root / mw.CONFIG_PATH).read_text())
            cfg[ma.SIDECAR_KEY] = bad
            (self.root / mw.CONFIG_PATH).write_text(json.dumps(cfg))
            h = self.handover()
            self.assertTrue(h["ok"], h)
            self.assertNotIn("AGENTS.md:2", {a["source"] for a in h["activeConstraints"]})
            self.assertEqual([], ma.sidecar_anchors(self.root))
            mw.pinned_anchor_report(self.root)
            ma.list_sidecar_anchors(self.root)

    def test_init_and_import_keep_sidecar_entries_and_do_not_copy_their_pins(self):
        before = json.loads((self.root / mw.CONFIG_PATH).read_text())[ma.SIDECAR_KEY]
        mw.init_project(self.root)
        mw.import_rules(self.root)
        cfg = json.loads((self.root / mw.CONFIG_PATH).read_text())
        self.assertEqual(before, cfg[ma.SIDECAR_KEY])
        self.assertNotIn("src/billing.py::charge_customer", cfg["pinnedAnchors"])
        page = (self.root / mw.IMPORT_PAGE).read_text()
        self.assertNotIn("memlet:anchor src/billing.py::charge_customer", page)
        self.assertEqual(5, len(ma.sidecar_index(self.root)["attached"]))

    def test_reaccepting_an_attached_entry_repins_it_after_review(self):
        self.write("src/billing.py", BILLING.replace("def charge_customer(customer, amount):",
                                                     "def charge_customer(customer, amount, currency):"))
        sid = next(e["id"] for e in ma.sidecar_anchors(self.root) if e["anchor"] == "src/billing.py::charge_customer")
        res = ma.accept_suggestions(self.root, [sid])
        self.assertEqual(([], [sid]), (res["accepted"], res["repinned"]))
        self.assertIn("AGENTS.md:3", {a["source"] for a in self.handover()["activeConstraints"]})


class CliAndInitTests(_Repo):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["memlet"] + argv), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                code = memory_cli.cli()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_admin_anchors_commands(self):
        self.fixture()
        code, out, err = self.run_cli(["admin", "anchors", "suggest", "--repo", str(self.root)])
        self.assertEqual(0, code, err)
        data = json.loads(out)
        self.assertEqual(5, data["suggestionCount"])
        code, out, _ = self.run_cli(["admin", "anchors", "suggest", "--repo", str(self.root), "--format", "markdown"])
        self.assertTrue(out.startswith("## Memlet anchor suggestions"), out)
        before = self.tree(include_memlet=True)
        code, out, _ = self.run_cli(["admin", "anchors", "accept", "--all-unique", "--dry-run", "--repo", str(self.root)])
        self.assertEqual(0, code)
        self.assertEqual(before, self.tree(include_memlet=True))
        sid = data["suggestions"][0]["id"]
        code, out, _ = self.run_cli(["admin", "anchors", "accept", sid, "--repo", str(self.root)])
        self.assertEqual(0, code, out)
        code, out, _ = self.run_cli(["admin", "anchors", "list", "--repo", str(self.root), "--format", "markdown"])
        self.assertIn(f"`{sid}`", out)
        code, out, _ = self.run_cli(["admin", "anchors", "accept", sid, "--all-unique", "--repo", str(self.root)])
        self.assertEqual(2, code)
        code, out, _ = self.run_cli(["admin", "anchors", "drop", sid, "--repo", str(self.root)])
        self.assertEqual(0, code, out)
        code, out, _ = self.run_cli(["admin", "anchors", "drop", sid, "--repo", str(self.root)])
        self.assertEqual(1, code)
        code, out, _ = self.run_cli(["anchors", "list", "--repo", str(self.root)])  # also reachable directly
        self.assertEqual(0, code)

    def test_anchors_is_an_admin_command_in_the_rules_group(self):
        code, out, _ = self.run_cli(["--help"])
        self.assertEqual(0, code)
        self.assertIn("{init,check,recall,handover,learn,ui,dream,admin}", out)
        code, out, _ = self.run_cli(["admin", "--help"])
        rules_group = out.split("Rules/wiki:", 1)[1].split("\n\n", 1)[0]
        self.assertIn("anchors", rules_group)

    def test_init_suggests_and_never_accepts(self):
        self.fixture()
        res = mw.init_project(self.root, output_format="markdown")
        self.assertIn("- 5 rule(s) could be anchored to code: `memlet admin anchors suggest`\n", res["formatted"])
        self.assertEqual({"rules": 5, "command": "memlet admin anchors suggest"}, res["anchorSuggestions"])
        self.assertNotIn(ma.SIDECAR_KEY, json.loads((self.root / mw.CONFIG_PATH).read_text()))
        self.write("AGENTS.md", "- Plain rule only.\n")
        res = mw.init_project(self.root, output_format="markdown")
        self.assertNotIn("could be anchored", res["formatted"])
        self.assertNotIn("anchorSuggestions", res)


class BoundedWorkTests(_Repo):
    def test_a_few_thousand_files_stay_bounded_and_deterministic(self):
        for i in range(3000):
            self.write(f"pkg{i // 100}/mod_{i}.py", f"def helper_number_{i}(x):\n    return x\n\n\nclass Thing{i}:\n    pass\n")
        self.write("AGENTS.md", "- Call `helper_number_1234()` first.\n- In `pkg0/mod_7.py`, `helper_number_7` is pure.\n"
                                "- Use `helper_number_5()`.\n")
        t0 = time.monotonic()
        res = ma.suggest_anchors(self.root)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 120.0)  # real margin: the machine runs other heavy jobs
        self.assertEqual(3000, res["scanned"]["codeFiles"])
        self.assertFalse(res["truncated"])
        self.assertEqual({("pkg12/mod_1234.py::helper_number_1234", "single-definition-found"),
                          ("pkg0/mod_7.py::helper_number_7", "file+symbol"), ("pkg0/mod_5.py::helper_number_5", "single-definition-found")},
                         {(s["anchor"], s["confidence"]) for s in res["suggestions"]})
        with mock.patch.object(ma, "MAX_INDEX_FILES", 100):
            capped = ma.suggest_anchors(self.root)
        self.assertTrue(capped["truncated"])
        self.assertEqual(100, capped["scanned"]["codeFiles"])
        # uniqueness is unproven in a truncated index: never claimed; a file-qualified mention still resolves
        self.assertEqual({("pkg0/mod_7.py::helper_number_7", "file+symbol")},
                         {(s["anchor"], s["confidence"]) for s in capped["suggestions"]})
        self.assertIn("scan_incomplete_single_definition_not_established", {a["reason"] for a in capped["ambiguous"]})
        self.assertLessEqual(len(ma.suggest_anchors(self.root, limit=0)["suggestions"]), 0)

    def test_adversarial_lines_and_files_stay_fast(self):
        self.write("src/a.py", "def target_function_name():\n    pass\n")
        long_rule = "- " + ("`x` " * 60000) + "`target_function_name()` " + ("a" * 200000) + "(" * 50000 + "\n"
        self.write("AGENTS.md", long_rule + "- " + "`" * 100000 + "\n- " + "b(" * 100000 + "\n")
        self.write("src/huge.py", "x = 1\n" * 400000)  # 2.4 MB: over the per-file cap, skipped
        self.write("src/deep.py", "(" * 100000 + "\n")    # does not parse
        t0 = time.monotonic()
        res = ma.suggest_anchors(self.root)
        self.assertLess(time.monotonic() - t0, 60.0)
        self.assertEqual(1, res["scanned"]["oversizedSkipped"])
        self.assertTrue(res["ok"])


# ---------------------------------------------------------------------------------------------------------------
# Exact-parser seam, with a stdlib test double


class FakeExact:
    """Test double for an exact TS/JS parser: fingerprints `function name(params) { body }` by regex."""
    languages = ("typescript", "javascript")
    name = "test-exact"

    def __init__(self, basis="test-exact-v1"):
        self.basis = basis
        self.calls = 0

    def fingerprint(self, path, text):
        self.calls += 1
        if "SYNTAX_ERROR" in text:
            return None
        out = {}
        for m in re.finditer(r"function\s+(\w+)\s*\(([^)]*)\)\s*\{([^}]*)\}", text):
            norm = lambda s: re.sub(r"\s+", "", s)
            out[m.group(1)] = {"ast": hashlib.sha256((m.group(1) + norm(m.group(2)) + norm(m.group(3))).encode()).hexdigest(),
                               "sig": hashlib.sha256(norm(m.group(2)).encode()).hexdigest()}
        return out


def _opt_in(root, names):
    p = root / mw.CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(p.read_text()) if p.is_file() else {}
    cfg["anchors"] = {"exactParsers": list(names)}
    p.write_text(json.dumps(cfg))


class ExactSeamTests(_Repo):
    TS = "export function f(a: number) {\n  return a + 1;\n}\n"

    def setUp(self):
        super().setUp()
        self.write("app.ts", self.TS)
        self.write("AGENTS.md", "# Rules\n- Always call `app.ts::f`.\n")

    def status(self):
        ca._EXACT_CACHE.clear()
        a = next(x for x in mw.pinned_anchor_report(self.root)["anchors"] if x["anchor"] == "app.ts::f")
        return a["status"], a["reason"], a["pinnedHashBasis"]

    def register(self, provider):
        ca.register_exact_provider(provider)
        self.addCleanup(ca.unregister_exact_provider, provider)

    def test_an_available_provider_changes_nothing_without_opt_in(self):  # R8
        prov = FakeExact()
        self.register(prov)
        with mock.patch.dict(os.environ, {ca.EXACT_PROVIDER_ENV: "1"}):  # no environment variable turns it on
            mw.init_project(self.root)
        self.assertEqual(("compatible-unverified", "ts_js_lexical_scan_unverified", ca.TS_LEXICAL_BASIS), self.status())
        self.assertEqual(0, prov.calls)  # never even asked

    def test_legacy_pin_without_basis_keeps_legacy_behaviour_with_a_provider_present(self):  # R8
        self.register(FakeExact())
        _opt_in(self.root, ["test-exact"])  # even with opt-in: a stored pin without basis is legacy, not "new"
        fp = mw._current_fingerprint(self.root, "app.ts", "f", basis=None)
        cfg = json.loads((self.root / mw.CONFIG_PATH).read_text())
        cfg["pinnedAnchors"] = {"app.ts::f": {"astHash": "0123456789abcdef", "sigHash": fp["sig"], "reqSigHash": fp["req"]}}
        (self.root / mw.CONFIG_PATH).write_text(json.dumps(cfg))
        self.assertEqual(("compatible-unverified", "legacy_ts_basis_repin_required", None), self.status())
        self.assertIsNot(ca.NEW_PIN, None)

    def test_lexical_pin_keeps_its_status_when_a_provider_appears(self):
        mw.init_project(self.root)
        self.assertEqual(("compatible-unverified", "ts_js_lexical_scan_unverified", ca.TS_LEXICAL_BASIS), self.status())
        self.register(FakeExact())
        _opt_in(self.root, ["test-exact"])
        self.assertEqual(("compatible-unverified", "ts_js_lexical_scan_unverified", ca.TS_LEXICAL_BASIS), self.status())

    def test_exact_pin_is_active_and_never_silently_reclassified_when_the_provider_goes(self):
        prov = FakeExact()
        self.register(prov)
        _opt_in(self.root, ["test-exact"])
        mw.init_project(self.root)
        self.assertEqual(("active", "ast_hash_match", "test-exact-v1"), self.status())
        h = mw.build_handover(self.root)
        self.assertIn("AGENTS.md:2", {a["source"] for a in h["activeConstraints"]})
        self.assertEqual([], h["lexicalConstraints"])
        ca.unregister_exact_provider(prov)
        self.assertEqual(("compatible-unverified", "exact_parser_basis_repin_required", "test-exact-v1"), self.status())
        self.register(FakeExact(basis="test-exact-v2"))  # another version is another basis
        self.assertEqual(("compatible-unverified", "exact_parser_basis_repin_required", "test-exact-v1"), self.status())
        self.register(prov)
        self.assertEqual(("active", "ast_hash_match", "test-exact-v1"), self.status())
        _opt_in(self.root, [])  # dropping the opt-in keeps checking pins already made with that parser
        self.assertEqual(("active", "ast_hash_match", "test-exact-v1"), self.status())

    def test_exact_pin_body_and_signature_changes(self):
        self.register(FakeExact())
        _opt_in(self.root, ["test-exact"])
        mw.init_project(self.root)
        self.write("app.ts", self.TS.replace("a + 1", "a + 2"))
        self.assertEqual(("compatible-unverified", "body_changed_signature_compatible"), self.status()[:2])
        self.write("app.ts", self.TS.replace("(a: number)", "(a: number, b: string)"))
        self.assertEqual(("stale", "signature_drift"), self.status()[:2])
        self.write("app.ts", "// SYNTAX_ERROR marker in a comment\n" + self.TS)  # the provider cannot parse it now
        self.assertEqual(("compatible-unverified", "exact_parser_basis_repin_required"), self.status()[:2])
        self.write("app.ts", "export const g = 1;\n")
        self.assertEqual(("stale", "missing_symbol"), self.status()[:2])  # a deleted symbol is stale on any basis

    def test_python_is_unaffected_and_bad_providers_are_refused(self):
        self.register(FakeExact())
        _opt_in(self.root, ["test-exact"])
        self.write("mod.py", "def g(x):\n    return x\n")
        self.write("AGENTS.md", "- Call `mod.py::g`.\n")
        mw.init_project(self.root)
        a = next(x for x in mw.pinned_anchor_report(self.root)["anchors"] if x["anchor"] == "mod.py::g")
        self.assertEqual(("active", mw.PYTHON_HASH_BASIS), (a["status"], a["pinnedHashBasis"]))
        for basis in (ca.TS_LEXICAL_BASIS, "legacy", "bad basis!", None):
            bad = FakeExact()
            bad.basis = basis
            with self.assertRaises(ValueError):
                ca.register_exact_provider(bad)
        nameless = FakeExact()
        nameless.name = ""
        with self.assertRaises(ValueError):
            ca.register_exact_provider(nameless)

    def test_a_crashing_provider_falls_back_to_the_lexical_scanner(self):
        class Boom(FakeExact):
            def fingerprint(self, path, text):
                raise RuntimeError("parser crashed")
        self.register(Boom())
        _opt_in(self.root, ["test-exact"])
        mw.init_project(self.root)
        self.assertEqual(("compatible-unverified", "ts_js_lexical_scan_unverified", ca.TS_LEXICAL_BASIS), self.status())


def _tree_sitter_available():
    try:
        from memlet import ts_exact
        return ts_exact.load_provider() is not None
    except Exception:
        return False


@unittest.skipUnless(_tree_sitter_available(), "tree-sitter and its TS/JS grammars are not installed (memlet[treesitter])")
class TreeSitterProviderTests(_Repo):
    TS = ("// header\nexport async function charge(amount: number, currency?: string): Promise<number> {\n"
          "  return amount;\n}\nexport const helper = (x: number) => x + 1;\n"
          "export class Ledger {\n  record(entry: string) {\n    return entry;\n  }\n}\n")

    def setUp(self):
        super().setUp()
        from memlet import ts_exact
        self.provider = ts_exact.load_provider()
        ca.register_exact_provider(self.provider)
        self.addCleanup(ca.unregister_exact_provider, self.provider)
        self.write("pay.ts", self.TS)
        self.write("AGENTS.md", "- Call `pay.ts::charge`.\n- Use `pay.ts::helper`.\n- Use `pay.ts::Ledger.record`.\n")

    def statuses(self):
        ca._EXACT_CACHE.clear()
        return {a["anchor"]: (a["status"], a["reason"]) for a in mw.pinned_anchor_report(self.root)["anchors"]}

    def test_installed_but_not_opted_in_pins_lexically(self):  # R8
        mw.init_project(self.root)
        self.assertEqual({"compatible-unverified"}, {s for s, _r in self.statuses().values()})
        self.assertEqual(ca.TS_LEXICAL_BASIS, mw._load_config_pins(self.root)["pay.ts::charge"]["hashBasis"])

    def test_real_parser_pins_are_active_and_track_changes(self):
        _opt_in(self.root, ["tree-sitter"])
        mw.init_project(self.root)
        self.assertEqual({"pay.ts::charge": ("active", "ast_hash_match"), "pay.ts::helper": ("active", "ast_hash_match"),
                          "pay.ts::Ledger.record": ("active", "ast_hash_match")}, self.statuses())
        basis = mw._load_config_pins(self.root)["pay.ts::charge"]["hashBasis"]
        self.assertTrue(basis.startswith("tree-sitter-v1."), basis)
        self.write("pay.ts", self.TS.replace("// header", "// a different comment").replace("  return amount;", "    return amount"))
        self.assertEqual("active", self.statuses()["pay.ts::charge"][0])  # comments, layout and `;` only
        self.write("pay.ts", self.TS.replace("return amount;", "return amount * 2;"))
        self.assertEqual(("compatible-unverified", "body_changed_signature_compatible"), self.statuses()["pay.ts::charge"])
        self.write("pay.ts", self.TS.replace("amount: number, currency?: string", "amount: number, currency: string"))
        self.assertEqual("stale", self.statuses()["pay.ts::charge"][0])
        self.write("pay.ts", self.TS + "function broken( {\n")  # no parser accepts it: stale, as without a provider
        self.assertEqual(("stale", "syntax_error"), self.statuses()["pay.ts::charge"])
        self.write("pay.ts", self.TS)
        ca.unregister_exact_provider(self.provider)
        self.assertEqual(("compatible-unverified", "exact_parser_basis_repin_required"), self.statuses()["pay.ts::charge"])

    def test_duplicate_declarations_are_never_active_under_the_exact_parser(self):  # R4 (provider side)
        _opt_in(self.root, ["tree-sitter"])
        one = "function validate_token(x) {\n  return 1;\n}\n"
        self.write("a.js", one)
        self.write("AGENTS.md", "- Use `a.js::validate_token`.\n")
        mw.init_project(self.root)
        self.assertEqual(("active", "ast_hash_match"), self.statuses()["a.js::validate_token"])
        # a second declaration appears and the effective one changes: never active again
        self.write("a.js", one + "function validate_token(x, y) {\n  return 3;\n}\n")
        self.assertNotEqual("active", self.statuses()["a.js::validate_token"][0])
        # a new pin on a duplicated name is not made by the exact parser at all
        self.tearDown_files()
        self.write("a.js", one + "function validate_token(x) {\n  return 2;\n}\n")
        self.write("AGENTS.md", "- Use `a.js::validate_token`.\n")
        _opt_in(self.root, ["tree-sitter"])
        mw.init_project(self.root)
        self.assertEqual(ca.TS_LEXICAL_BASIS, mw._load_config_pins(self.root)["a.js::validate_token"]["hashBasis"])
        self.write("a.js", one + "function validate_token(x, y) {\n  return 3;\n}\n")
        self.assertNotEqual("active", self.statuses()["a.js::validate_token"][0])
        for src in ("class Auth {\n  validate_token(x) {\n    return 1;\n  }\n  validate_token(x) {\n    return 2;\n  }\n}\n",
                    "class Auth {\n  get access_token() {\n    return this.t;\n  }\n  set access_token(v) {\n    this.t = v;\n  }\n}\n"):
            fp = self.provider.fingerprint("a.js", src)
            self.assertFalse({k for k in fp if k.endswith(("validate_token", "access_token"))}, fp)


    def test_c_rebinding_after_acceptance_is_never_active(self):  # N5
        base = "function validate_token(x) { return x; }\n"
        cases = {"assignment": base + "validate_token = (x,y) => false;\n",
                 "conditional": base + "if (true) { function validate_token(x,y) { return false; } }\n",
                 "escaped-duplicate": base + "function \\u0076alidate_token(x,y) { return false; }\n",
                 "plain-duplicate": base + "function validate_token(x,y) { return false; }\n",
                 "undecodable-escape": base + "function \\u{110000}other() {}\n"}
        for name, text in list(cases.items()) + [("unrelated-edit", base + "function helper(a) { return a; }\n")]:
            with self.subTest(case=name):
                self.tearDown_files()
                _opt_in(self.root, ["tree-sitter"])
                self.write("a.js", base)
                self.write("AGENTS.md", "- Always use `validate_token()`.\n")
                self.accept_all()
                self.assertEqual("active", ma.list_sidecar_anchors(self.root)["entries"][0]["status"])
                self.write("a.js", text)
                ca._EXACT_CACHE.clear()
                status = ma.list_sidecar_anchors(self.root)["entries"][0]["status"]
                active_rules = {a["source"] for a in mw.build_handover(self.root)["activeConstraints"]}
                if name == "unrelated-edit":
                    self.assertEqual("active", status)
                else:
                    self.assertNotEqual("active", status)
                    self.assertNotIn("AGENTS.md:1", active_rules)


# ---------------------------------------------------------------------------------------------------------------
# Fix round 1: one regression per reviewer finding (reproductions from /private/tmp/d2-review-IlVOoz)

NORMAL = "def validate_token(x):\n    return x\n"
RULE = "- Always call `validate_token()` before trusting input.\n"


class ReviewRegressionTests(_Repo):
    def simple(self, extra=None, rule=RULE):
        self.write("a.py", NORMAL)
        self.write("AGENTS.md", rule)
        for rel, text in (extra or {}).items():
            self.write(rel, text)

    def anchors_of(self, res):
        return {(s["anchor"], s["confidence"]) for s in res["suggestions"]}

    def reasons(self, res, mention="validate_token"):
        return {a["reason"] for a in res["ambiguous"] if a["mention"] == mention}

    # R1 --------------------------------------------------------------------------------------------------------
    def test_r1_same_size_same_mtime_rewrite_in_one_process_detaches(self):
        self.write("a.py", NORMAL)
        f = self.write("AGENTS.md", "- Always use `validate_token()`.\n")
        self.accept_all()
        before = mb.unified_recall(self.root, "validate token")
        self.assertIn("active", json.dumps(before["sections"]["rules"]))
        st, old = f.stat(), f.read_bytes()
        new = b"- Always disable all checks."
        f.write_bytes(new + b" " * (len(old) - len(new) - 1) + b"\n")
        os.utime(str(f), ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual((st.st_size, st.st_mtime_ns), (f.stat().st_size, f.stat().st_mtime_ns))
        after = mb.unified_recall(self.root, "disable checks")
        items = [i for s in ("rules", "safety", "staleRules") for i in after["sections"][s]]
        self.assertTrue(items)
        for i in items:
            self.assertNotEqual("active", i["anchorStatus"], i)
            self.assertEqual([], i["anchors"])
        self.assertEqual("rule_text_changed_or_removed", ma.list_sidecar_anchors(self.root)["entries"][0]["reason"])

    def test_r1_a_quote_whose_digest_differs_never_attaches(self):
        self.write("a.py", NORMAL)
        self.write("AGENTS.md", "- Always use `validate_token()`.\n")
        self.accept_all()
        entries = [{"id": "AGENTS.md:1-1", "quote": "- Always disable all checks."},
                   {"id": "AGENTS.md:1-1", "quote": "- Always use `validate_token()`."}]
        mw.classify_rule_quotes(self.root, entries)
        self.assertEqual(("unanchored", []), (entries[0]["anchorStatus"], entries[0]["anchors"]))
        self.assertEqual("active", entries[1]["anchorStatus"])

    # R2 --------------------------------------------------------------------------------------------------------
    def test_r2_every_other_binding_blocks_single_definition_found(self):
        cases = {
            "nested": {"nested.py": "def factory():\n    def validate_token(y, z):\n        return y\n    return validate_token\n"},
            "attribute": {"other.py": "class Handler:\n    validate_token = lambda x, y: x\n"},
            "assigned-attribute": {"other.py": "class Handler:\n    pass\nHandler.validate_token = lambda x, y: x\n"},
            "reexport": {"other.py": "from third_party.external import validate_token\n__all__ = ['validate_token']\n"},
            "export-only": {"other.py": "__all__ = ['validate_token']\n"},
            "match": {"other.py": "match MODE:\n    case 1:\n        def validate_token(y, z):\n            return y\n"},
            "generated-dir": {"generated/dup.py": NORMAL},
            "vendor-dir": {"vendor/dup.py": NORMAL},
            "hidden-dir": {".private/dup.py": NORMAL},
            "generated-head": {"dup.py": "# DO NOT EDIT\n" + NORMAL},
            "oversize": {"dup.py": NORMAL + "#" + "x" * (2 * 1024 * 1024)},
            "other-language": {"tool.rb": "def validate_token(x)\n  x\nend\n"},
            "ts-duplicate": {"dup.ts": "function validate_token(x) { return x; }\n"},
            "unparsable": {"broken.py": "def validate_token(x):\n    return (\n"},
        }
        for name, extra in cases.items():
            with self.subTest(case=name):
                self.tearDown_files()
                self.simple(extra)
                res = ma.suggest_anchors(self.root)
                self.assertNotIn(("a.py::validate_token", "single-definition-found"), self.anchors_of(res))
                self.assertTrue(self.reasons(res) & {"other_bindings_of_the_name", "multiple_definitions",
                                                     "scan_incomplete_single_definition_not_established"}, res["ambiguous"])
                acc = ma.accept_suggestions(self.root, all_unique=True)
                self.assertEqual([], acc.get("accepted"), acc)

    def test_r2_imports_of_the_definition_itself_do_not_block(self):
        # only imports that provably name the defining FILE: relative, root dotted path, or a sibling module
        self.simple({"tests/test_a.py": "import os\nfrom a import validate_token\n",
                     "pkg/__init__.py": "from a import validate_token\n__all__ = ['validate_token']\n",
                     "sub/rel.py": "from ..a import validate_token\n", "sub/__init__.py": ""})
        self.assertEqual({("a.py::validate_token", "single-definition-found")}, self.anchors_of(ma.suggest_anchors(self.root)))
        for rel, text in (("pkg/use.py", "from pkg import validate_token\n"),        # a re-export hop
                          ("pkg/rel2.py", "from . import validate_token\n"),          # the package, not a.py
                          ("pkg/alias.py", "from a import other as validate_token\n"),
                          ("pkg/star.py", "from a import *\n")):                      # wildcards always block
            with self.subTest(rel=rel):
                self.write(rel, text)
                self.assertEqual({"other_bindings_of_the_name"}, self.reasons(ma.suggest_anchors(self.root)))
                (self.root / rel).unlink()
        # reviewer N1: a filename suffix is not module identity (`from a import` vs repository `pkg/a.py`)
        self.tearDown_files()
        self.write("pkg/a.py", NORMAL)
        self.write("pkg/__init__.py", "")
        self.write("consumer.py", "from a import validate_token\n")
        self.write("AGENTS.md", RULE)
        self.assertEqual({"other_bindings_of_the_name"}, self.reasons(ma.suggest_anchors(self.root)))
        self.write("pkg/sibling.py", "from a import validate_token\n")  # a sibling module of the importer: exempt
        self.write("consumer.py", "from pkg.a import validate_token\n")  # the root dotted path: exempt
        self.assertEqual({("pkg/a.py::validate_token", "single-definition-found")}, self.anchors_of(ma.suggest_anchors(self.root)))
        self.tearDown_files()
        self.write("lib/auth.ts", "export function validate_token(x: string) {\n  return x;\n}\n")
        self.write("web/use.ts", "import { validate_token } from '../lib/auth';\nexport { validate_token as vt } from '../lib/auth';\n")
        self.write("AGENTS.md", RULE)
        self.assertEqual({("lib/auth.ts::validate_token", "single-definition-found")}, self.anchors_of(ma.suggest_anchors(self.root)))
        for text in ("import { validate_token } from 'auth-lib';\n", "import { validate_token } from '@/lib/auth';\n",
                     "export * from '../lib/auth';\n"):
            with self.subTest(text=text):
                self.write("web/use.ts", text)
                self.assertEqual({"other_bindings_of_the_name"}, self.reasons(ma.suggest_anchors(self.root)))

    def test_r2_an_unreadable_code_file_makes_the_index_incomplete(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can read everything")
        self.simple()
        p = self.write("vendor/locked.py", NORMAL)
        os.chmod(str(p), 0)
        self.addCleanup(os.chmod, str(p), 0o644)
        res = ma.suggest_anchors(self.root)
        self.assertEqual({"scan_incomplete_single_definition_not_established"}, self.reasons(res))
        self.assertTrue(res["truncated"])
        self.assertIn("unreadable:vendor/locked.py", res["truncation"])

    # R3 --------------------------------------------------------------------------------------------------------
    def test_r3_a_named_file_without_the_symbol_never_falls_back(self):
        self.simple({"other.py": "pass\n"}, "- In `other.py`, always use `validate_token()`.\n")
        res = ma.suggest_anchors(self.root)
        self.assertEqual(set(), self.anchors_of(res))
        self.assertEqual({"named_file_does_not_define_symbol"}, self.reasons(res))
        self.simple(rule="- In `missing.py`, always use `validate_token()`.\n")
        self.assertEqual({"named_file_not_found"}, self.reasons(ma.suggest_anchors(self.root)))

    def test_r3_two_readings_of_a_path_are_ambiguous_and_a_leading_slash_means_the_root(self):
        self.write("auth.py", NORMAL)
        self.write("src/auth.py", "def validate_token(x, y):\n    return y\n")
        self.write("src/AGENTS.md", "- In `auth.py`, always use `validate_token()`.\n")
        res = ma.suggest_anchors(self.root)
        self.assertEqual(set(), self.anchors_of(res))
        self.assertEqual({"file_mention_has_several_interpretations"}, self.reasons(res))
        self.assertEqual([], ma.accept_suggestions(self.root, all_unique=True)["accepted"])
        self.write("src/AGENTS.md", "- In `/auth.py`, always use `validate_token()`.\n")
        self.assertEqual({("auth.py::validate_token", "file+symbol")}, self.anchors_of(ma.suggest_anchors(self.root)))
        self.write("src/AGENTS.md", "- In `src/auth.py`, always use `validate_token()`.\n")
        self.assertEqual({("src/auth.py::validate_token", "file+symbol")}, self.anchors_of(ma.suggest_anchors(self.root)))

    # R4 (index side) -------------------------------------------------------------------------------------------
    def test_r4_duplicate_declarations_are_ambiguous(self):
        cases = {
            "functions": ("a.js", "function validate_token(x) {\n  return 1;\n}\n\nfunction validate_token(x) {\n  return 2;\n}\n",
                          "validate_token"),
            "methods": ("a.js", "class Auth {\n  validate_token(x) {\n    return 1;\n  }\n\n  validate_token(x) {\n    return 2;\n  }\n}\n",
                        "Auth.validate_token"),
            "accessors": ("a.js", "class Auth {\n  get access_token() {\n    return this.t;\n  }\n\n  set access_token(v) {\n    this.t = v;\n  }\n}\n",
                          "Auth.access_token"),
            "python": ("a.py", "def validate_token(x):\n    return 1\n\n\ndef validate_token(x):\n    return 2\n", "validate_token"),
        }
        for name, (rel, src, sym) in cases.items():
            for rule in (f"- Use `{sym}()`.\n", f"- In `{rel}`, use `{sym}()`.\n"):
                with self.subTest(case=name, rule=rule):
                    self.tearDown_files()
                    self.write(rel, src)
                    self.write("AGENTS.md", rule)
                    res = ma.suggest_anchors(self.root)
                    self.assertEqual([], res["suggestions"], res["suggestions"])
                    self.assertTrue(res["ambiguous"] or not res["suggestions"])

    # R5 --------------------------------------------------------------------------------------------------------
    def test_r5_the_entry_cap_is_enforced_before_any_write(self):
        self.fixture()
        mw.init_project(self.root)
        before = (self.root / mw.CONFIG_PATH).read_bytes()
        with mock.patch.object(ma, "MAX_SIDECAR_ENTRIES", 3):
            res = ma.accept_suggestions(self.root, self.all_ids())
            self.assertFalse(res["ok"])
            self.assertIn("over the limit", res["error"])
            self.assertEqual(before, (self.root / mw.CONFIG_PATH).read_bytes())
            ids = [s["id"] for s in ma.suggest_anchors(self.root)["suggestions"]][:3]
            self.assertTrue(ma.accept_suggestions(self.root, ids)["ok"])
        stored = (self.root / mw.CONFIG_PATH).read_bytes()
        with mock.patch.object(ma, "MAX_SIDECAR_ENTRIES", 2):  # an existing over-cap state
            self.assertEqual([], ma.sidecar_index(self.root)["attached"])  # readers fail closed, no prefix is read
            for attempt in (ma.accept_suggestions(self.root, self.all_ids()), ma.accept_suggestions(self.root, [ids[0]])):
                self.assertFalse(attempt["ok"])
                self.assertEqual(stored, (self.root / mw.CONFIG_PATH).read_bytes())
            self.assertTrue(ma.drop_sidecar_anchors(self.root, [ids[0]])["ok"])  # recovery stays possible
        self.assertEqual(2, len(json.loads((self.root / mw.CONFIG_PATH).read_text())[ma.SIDECAR_KEY]))

    def test_r5_reviewer_quota_reproduction_loses_nothing(self):
        files = {"a.py": NORMAL}
        for j in range(6):
            files[f"d{j}/AGENTS.md"] = "".join(f"- For rule {j}-{i}, always use `validate_token()`.\n" for i in range(200))
        for rel, text in files.items():
            self.write(rel, text)
        self.assertEqual(1200, ma.suggest_anchors(self.root, limit=None)["suggestionCount"])
        before = self.tree(include_memlet=True)
        res = ma.accept_suggestions(self.root, self.all_ids())
        self.assertFalse(res["ok"])
        self.assertEqual(before, self.tree(include_memlet=True))

    # R6 --------------------------------------------------------------------------------------------------------
    def test_r6_duplicate_rule_text_beyond_the_reader_cap_is_ambiguous_and_reported(self):
        rule = "- Always use `validate_token()`.\n"
        self.write("a.py", NORMAL)
        self.write("AGENTS.md", rule + "".join(f"- Policy {i}: do the right thing.\n" for i in range(199)) + rule)
        res = ma.suggest_anchors(self.root)
        self.assertEqual([], res["suggestions"])
        self.assertEqual({"rule_text_repeated_in_file"}, self.reasons(res))
        self.assertTrue(res["truncated"])
        self.assertIn("rules_per_file>=200:AGENTS.md", res["truncation"])
        # accepted while unique, then a copy appears beyond the cap: detached
        self.write("AGENTS.md", rule)
        self.accept_all()
        self.write("AGENTS.md", rule + "".join(f"- Policy {i}: do the right thing.\n" for i in range(250)) + rule)
        self.assertEqual("rule_text_repeated_in_file", ma.list_sidecar_anchors(self.root)["entries"][0]["reason"])

    def test_r6_skipped_oversize_instruction_file_is_reported_and_all_unique_fails_closed(self):
        self.write("a.py", NORMAL)
        self.write("AGENTS.md", RULE)
        self.write("CLAUDE.md", RULE.replace("Always", "Also") + "x" * 1_000_001)
        res = ma.suggest_anchors(self.root)
        self.assertTrue(res["truncated"])
        self.assertEqual([{"source": "CLAUDE.md", "reason": "over_1000000_bytes"}], res["scanned"]["skippedSources"])
        before = self.tree(include_memlet=True)
        acc = ma.accept_suggestions(self.root, all_unique=True)
        self.assertFalse(acc["ok"])
        self.assertIn("incomplete", acc["error"])
        self.assertEqual(before, self.tree(include_memlet=True))
        self.assertTrue(ma.accept_suggestions(self.root, [res["suggestions"][0]["id"]])["ok"])  # by id still works

    # R7 --------------------------------------------------------------------------------------------------------
    def test_r7_a_drop_revokes_and_restoring_the_old_config_stays_rejected(self):
        self.simple()
        self.accept_all()
        cfg = (self.root / mw.CONFIG_PATH).read_bytes()
        sid = ma.sidecar_anchors(self.root)[0]["id"]
        self.assertTrue(ma.drop_sidecar_anchors(self.root, [sid])["ok"])
        (self.root / mw.CONFIG_PATH).write_bytes(cfg)  # replay: the accepted entry is back, its accept row too
        self.assertEqual([], ma.sidecar_anchors(self.root))
        self.assertEqual([("rejected", "not_recorded_by_accept")],
                         [(e["state"], e["reason"]) for e in ma.list_sidecar_anchors(self.root)["entries"]])

    def test_r7_hand_edited_line_is_rejected_and_the_boundary_is_stated(self):
        self.simple()
        self.accept_all()
        cfg = json.loads((self.root / mw.CONFIG_PATH).read_text())
        cfg[ma.SIDECAR_KEY][0]["line"] = 999
        (self.root / mw.CONFIG_PATH).write_text(json.dumps(cfg))
        self.assertEqual("not_recorded_by_accept", ma.list_sidecar_anchors(self.root)["entries"][0]["reason"])
        import inspect
        self.assertNotIn("never trusted", inspect.getsource(ma))
        self.assertIn("Integrity, not authentication", ma.__doc__)
        self.assertIn("can forge an acceptance", ma.list_sidecar_anchors(self.root)["formatted"])

    def test_r7_copying_config_and_ledger_carries_acceptances_as_documented(self):
        import shutil
        self.simple()
        self.accept_all()
        clone = self.tmp / "clone"
        clone.mkdir()
        for rel in ("a.py", "AGENTS.md"):
            shutil.copy(str(self.root / rel), str(clone / rel))
        shutil.copytree(str(self.root / ".memlet"), str(clone / ".memlet"))
        self.assertEqual(1, len(ma.sidecar_index(clone)["attached"]))  # integrity, not authentication
        self.assertIn("copying both files", ma.list_sidecar_anchors(clone)["integrityNotice"])
        (clone / mw.LEDGER_PATH).unlink()  # config alone does not carry it
        self.assertEqual([], ma.sidecar_index(clone)["attached"])

    # R11 -------------------------------------------------------------------------------------------------------
    def test_r11_a_config_changed_since_it_was_read_is_refused(self):
        self.simple()
        mw.init_project(self.root)
        real = mw.write_managed_text

        def swap(root, rel, text, **kw):
            if rel == mw.CONFIG_PATH:
                (root / rel).unlink()
                (root / rel).write_text('{"concurrent_setting": "MUST_KEEP"}')
            return real(root, rel, text, **kw)
        ids = self.all_ids()
        with mock.patch.object(mw, "write_managed_text", side_effect=swap):
            res = ma.accept_suggestions(self.root, ids)
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("concurrentChange"))
        self.assertEqual({"concurrent_setting": "MUST_KEEP"}, json.loads((self.root / mw.CONFIG_PATH).read_text()))
        self.assertEqual({}, ma._ledger_records(self.root))
        # the shared writer keeps its old behaviour without if_match
        mw.write_managed_text(self.root, mw.CONFIG_PATH, "{}")
        with self.assertRaises(mw.ConcurrentChangeError):
            mw.write_managed_text(self.root, mw.CONFIG_PATH, "{}", if_match=mw.content_tag(b"other"))

    def test_r11_ledger_failure_after_the_config_write_says_what_was_written_and_how_to_recover(self):
        self.simple()
        mw.init_project(self.root)
        real = mw.connect_managed_sqlite

        def fail(root, rel, **kw):
            if not kw.get("readonly"):
                raise sqlite3.OperationalError("injected disk error at ledger commit")
            return real(root, rel, **kw)
        ids = self.all_ids()
        with mock.patch.object(mw, "connect_managed_sqlite", side_effect=fail):
            res = ma.accept_suggestions(self.root, ids)
        self.assertFalse(res["ok"])
        self.assertTrue(res["partialWrite"])
        self.assertEqual([mw.CONFIG_PATH], res["written"])
        self.assertEqual([{"op": "update", "path": mw.CONFIG_PATH}], res["actions"])
        self.assertIn("new or changed: not in effect until accepted again", res["error"])
        self.assertIn("memlet admin anchors accept", res["error"])
        self.assertEqual([], ma.sidecar_index(self.root)["attached"])  # fail closed
        sid = res["entries"][0]["id"] if res.get("entries") else None
        retry = ma.accept_suggestions(self.root, [sid])  # the documented recovery
        self.assertTrue(retry["ok"], retry)
        self.assertEqual(1, len(ma.sidecar_index(self.root)["attached"]))

    # R12 -------------------------------------------------------------------------------------------------------
    def test_r12_a_move_under_other_headings_detaches_and_a_line_shift_does_not(self):
        self.simple(rule="- Always use `validate_token()`.\n")
        self.accept_all()
        self.write("AGENTS.md", "\n\n- Always use `validate_token()`.\n")
        [row] = ma.list_sidecar_anchors(self.root)["entries"]
        self.assertEqual(("attached", 3, 1, True), (row["state"], row["line"], row["recordedLine"], row["lineShifted"]))
        self.write("AGENTS.md", "# Different Scope\n\n- Always use `validate_token()`.\n")
        [row] = ma.list_sidecar_anchors(self.root)["entries"]
        self.assertEqual(("detached", "rule_moved_to_another_section"), (row["state"], row["reason"]))

    def test_r12_entries_from_the_older_format_are_rejected_until_accepted_again(self):
        self.simple()
        self.accept_all()
        cfg = json.loads((self.root / mw.CONFIG_PATH).read_text())
        del cfg[ma.SIDECAR_KEY][0]["sectionPath"]
        (self.root / mw.CONFIG_PATH).write_text(json.dumps(cfg))
        self.assertEqual("older_entry_format_accept_again", ma.list_sidecar_anchors(self.root)["entries"][0]["reason"])

    # other probes ----------------------------------------------------------------------------------------------
    def test_urls_examples_quotes_and_plain_english_words_are_not_suggested(self):
        self.write("a.py", NORMAL + "\n\nclass Convention:\n    pass\n")
        for rule, want in (("- Read https://example.invalid/validate_token() before merging.\n", None),
                           ("- See `https://example.invalid/validate_token()`.\n", None),
                           ('- Example only: "use `validate_token()`" is obsolete text.\n', "mention_in_example_or_quotation"),
                           ("- Prefer helpers, e.g. `validate_token()`.\n", "mention_in_example_or_quotation"),
                           ("- Always follow `Convention`.\n", "name_too_generic_to_anchor_without_a_file")):
            with self.subTest(rule=rule):
                self.write("AGENTS.md", rule)
                res = ma.suggest_anchors(self.root)
                self.assertEqual([], res["suggestions"])
                mention = "Convention" if "Convention" in rule else "validate_token"
                self.assertEqual({want} if want else set(), self.reasons(res, mention))

    def test_a_rule_too_long_to_scan_is_never_suggested(self):
        self.write("other.py", NORMAL)
        self.simple(rule="- Always use `validate_token()` " + "filler " * 3000 + "in `other.py`.\n")
        res = ma.suggest_anchors(self.root)
        self.assertEqual([], res["suggestions"])
        self.assertEqual({"rule_too_long_to_scan_completely"}, self.reasons(res))

    # R10 groundwork --------------------------------------------------------------------------------------------
    def test_r10_every_record_carries_provenance(self):
        self.simple()
        self.accept_all()
        sid = ma.sidecar_anchors(self.root)[0]["id"]
        [r] = [r for r in mw._rules_from_files(self.root, ["AGENTS.md"])]
        self.assertEqual((["a.py::validate_token"], {"a.py::validate_token": sid}), (r["suggestedAnchors"], r["sidecarIds"]))
        h = mw.build_handover(self.root)
        [item] = h["activeConstraints"]
        self.assertEqual((["a.py::validate_token"], [sid]), (item["suggestedAnchors"], item["sidecarIds"]))
        rep = {a["anchor"]: a for a in mw.pinned_anchor_report(self.root)["anchors"]}["a.py::validate_token"]
        self.assertEqual((True, [sid]), (rep["suggested"], rep["sidecarIds"]))
        entries = [{"id": "AGENTS.md:1-1", "quote": RULE.rstrip("\n")}]
        mw.classify_rule_quotes(self.root, entries)
        self.assertEqual(sid, entries[0]["anchors"][0]["sidecarId"])



# ---------------------------------------------------------------------------------------------------------------
# Fix round 2: reproductions from /private/tmp/d2-review2-tpe4GL (replay.sh cases)


class Round2RegressionTests(_Repo):
    simple = ReviewRegressionTests.simple
    anchors_of = ReviewRegressionTests.anchors_of
    reasons = ReviewRegressionTests.reasons

    def blocked(self, extra, rule=RULE):
        self.tearDown_files()
        self.simple(extra, rule)
        res = ma.suggest_anchors(self.root)
        self.assertNotIn(("a.py::validate_token", "single-definition-found"), self.anchors_of(res), extra)
        return res

    # A1-A3 -----------------------------------------------------------------------------------------------------
    def test_a_suggestions_state_evidence_not_uniqueness(self):
        self.simple({"node_modules/pkg/index.js": "function validate_token(x) { return x; }\n",
                     "nested/.git/HEAD": "ref: x\n", "nested/copy.py": NORMAL, "build/tool.py": NORMAL})
        res = ma.suggest_anchors(self.root)
        [sg] = res["suggestions"]
        self.assertEqual("single-definition-found", sg["confidence"])
        self.assertRegex(sg["reason"], r"one definition of `validate_token` found .* in \d+ indexed file\(s\); no other "
                                       r"binding-like occurrence in \d+ text-scanned file\(s\); not scanned: ")
        for word in ("exactly one binding", "unique", "uniqueness"):
            self.assertNotIn(word, sg["reason"])
            self.assertNotIn(word, json.dumps([a["reason"] for a in res["ambiguous"]]))
        for want in ("node_modules/", "build/", "nested/ (nested repository)"):
            self.assertIn(want, sg["notScanned"]["paths"])
        self.assertEqual(sg["notScanned"], res["notScanned"])
        self.assertEqual(3, res["notScanned"]["count"])
        self.assertFalse(res["truncated"])  # pruned folders are listed, not demoting
        self.assertNotIn("unique", ma.CONFIDENCES[1])

    def test_a_all_unique_accepts_only_file_plus_symbol(self):
        self.fixture()
        res = ma.suggest_anchors(self.root)
        self.assertEqual({"file+symbol": 2, "single-definition-found": 3}, res["countsByConfidence"])
        self.assertIn("- 2 file+symbol: `memlet admin anchors accept --all-unique` (accepts only these)", res["formatted"])
        self.assertIn("- 3 single-definition-found: by id only, after checking each: `memlet admin anchors accept ID…`",
                      res["formatted"])
        acc = ma.accept_suggestions(self.root, all_unique=True)
        self.assertTrue(acc["ok"], acc)
        self.assertEqual({"src/billing.py::charge_customer", "src/auth.py::Session"},
                         {e["anchor"] for e in acc["entries"]})
        self.assertEqual(3, len(acc["notAcceptedInBulk"]))
        self.assertIn("--all-unique takes only file+symbol", acc["formatted"])
        one = next(s["id"] for s in ma.suggest_anchors(self.root)["suggestions"] if s["confidence"] == "single-definition-found")
        self.assertTrue(ma.accept_suggestions(self.root, [one])["ok"])  # by id: a human decision

    def test_a3_an_unreadable_or_unlistable_directory_makes_the_scan_incomplete(self):
        self.simple({"restricted/dup.py": NORMAL})
        real = os.scandir

        def denied(path, *a):
            if str(path) == str(self.root / "restricted"):
                raise PermissionError(13, "Permission denied", str(path))
            return real(path, *a)
        with mock.patch.object(os, "scandir", side_effect=denied):
            res = ma.suggest_anchors(self.root)
        self.assertEqual({"scan_incomplete_single_definition_not_established"}, self.reasons(res))
        self.assertTrue(res["truncated"])
        self.assertTrue(any(t.startswith("walk_error:restricted") for t in res["truncation"]), res["truncation"])
        if not (hasattr(os, "geteuid") and os.geteuid() == 0):  # the real chmod case too
            (self.root / "restricted").chmod(0)
            self.addCleanup((self.root / "restricted").chmod, 0o755)
            res = ma.suggest_anchors(self.root)
            self.assertEqual([], ma.accept_suggestions(self.root, self.all_ids() or ["anc-000000000000"]).get("accepted"))
            self.assertEqual({"scan_incomplete_single_definition_not_established"}, self.reasons(res))

    # A4 (N2) ---------------------------------------------------------------------------------------------------
    def test_a4_binding_forms_in_parsed_and_text_only_code_count(self):
        cases = {
            "py-for": {"b.py": "for validate_token in [lambda x: False]:\n    pass\n"},
            "py-with": {"b.py": "with something as validate_token:\n    pass\n"},
            "py-except": {"b.py": "try:\n    pass\nexcept ValueError as validate_token:\n    pass\n"},
            "py-global": {"b.py": "def f():\n    global validate_token\n"},
            "ts-reexport-alias": {"b.ts": 'export { foreign_token as validate_token } from "external";\n'},
            "ts-local-export-alias": {"b.ts": "function foreign_token(x) { return false; }\nexport { foreign_token as validate_token };\n"},
            "js-inline-object-method": {"b.js": "const handlers = { validate_token(x) { return false; } };\n"},
            "js-object-property": {"b.js": "const handlers = { validate_token: (x) => false };\n"},
            "ts-class-field": {"b.ts": "class A {\n  validate_token: (x: string) => boolean;\n}\n"},
            "js-escaped": {"b.js": "function \\u0076alidate_token(x) { return false; }\n"},
            "text-js-object": {"vendor/dup.js": "const handlers = {\n  validate_token(x) { return false; }\n};\n"},
            "text-java": {"vendor/dup.java": "class Auth { public boolean validate_token(String x) { return false; } }\n"},
            "text-go": {"vendor/dup.go": "func (h Handler) validate_token(x string) bool { return false }\n"},
            "text-rust": {"vendor/dup.rs": "pub fn validate_token(x: &str) -> bool { false }\n"},
            "text-unicode": {"vendor/dup.py": "def 𝚟alidate_token(x): return False\n"},
            "unicode-parsed": {"b.py": "def 𝚟alidate_token(x): return False\n"},
            "unknown-language": {"b.hx": "class Handler { function validate_token(x) { return false; } }\n"},
            "binary-python": {"b.py": "\0\1def validate_token(x): pass\n"},
            "text-mere-mention": {"vendor/use.js": "callIt(validate_token);\n"},  # any whole token counts there
        }
        for name, extra in cases.items():
            with self.subTest(case=name):
                res = self.blocked(extra)
                self.assertTrue(self.reasons(res) & {"other_bindings_of_the_name", "multiple_definitions"}, res["ambiguous"])

    def test_a4_documentation_and_data_files_do_not_count(self):
        self.simple({"docs/guide.md": "Call validate_token() first.\n", "data.json": '{"validate_token": 1}\n',
                     "README": "validate_token\n", ".cursorrules": "- Use `validate_token()`.\n"})
        self.assertEqual({("a.py::validate_token", "single-definition-found")}, self.anchors_of(ma.suggest_anchors(self.root)))

    # A5 (N1) ---------------------------------------------------------------------------------------------------
    def test_a5_reviewer_import_runtime_cases_block(self):
        self.blocked({"bridge.py": "from a import validate_token\nfrom plugin import *\n",
                      "consumer.py": "from bridge import validate_token\n"})
        self.tearDown_files()
        self.write("pkg/a.py", NORMAL)
        self.write("pkg/__init__.py", "")
        self.write("consumer.py", "from a import validate_token\n")
        self.write("AGENTS.md", RULE)
        res = ma.suggest_anchors(self.root)
        self.assertEqual([], res["suggestions"])
        self.assertEqual({"other_bindings_of_the_name"}, self.reasons(res))

    # B (N4) ----------------------------------------------------------------------------------------------------
    def test_b_heading_identity(self):
        rule = "- Always use `validate_token()`.\n"
        detach = [("setext", "Scope A\n=======\n" + rule, "Scope B\n=======\n" + rule),
                  ("setext-h2", "Scope A\n-------\n" + rule, "Scope B\n-------\n" + rule),
                  ("long-title", "# " + "A" * 200 + " FIRST\n" + rule, "# " + "A" * 200 + " SECOND\n" + rule),
                  ("literal-hash", "# C#\n" + rule, "# C\n" + rule),
                  ("atx-rename", "# Scope A\n" + rule, "# Scope B\n" + rule),
                  ("depth", "".join("#" * i + f" Level {i}\n" for i in range(1, 7)) + rule,
                   "# CHANGED\n" + "".join("#" * i + f" Level {i}\n" for i in range(2, 7)) + rule)]
        stay = [("closing-hashes", "# Scope\n" + rule, "# Scope ###\n" + rule),
                ("whitespace", "# Scope\n" + rule, "  #  Scope   \n" + rule),
                ("crlf", "# Scope\n" + rule, ("# Scope\n" + rule).replace("\n", "\r\n")),
                ("fenced", "# Scope\n```\n# Fake A\n```\n" + rule, "# Scope\n```\n# Fake B\n```\n" + rule),
                ("mixed-fences", "# Scope\n````md\n~~~\n# Fake\n~~~\n````\n" + rule,
                 "# Scope\n````md\n~~~\n# Other\n~~~\n````\n" + rule)]
        for name, before, after in detach + stay:
            with self.subTest(case=name):
                self.tearDown_files()
                self.write("a.py", NORMAL)
                self.write("AGENTS.md", before)
                self.accept_all()
                (self.root / "AGENTS.md").write_bytes(after.encode())
                [row] = ma.list_sidecar_anchors(self.root)["entries"]
                if (name, before, after) in detach:
                    self.assertEqual(("detached", "rule_moved_to_another_section"), (row["state"], row["reason"]))
                else:
                    self.assertEqual("attached", row["state"], row)
        self.assertEqual(["C#"], ma._heading_chains(["# C#"])[0] and list(ma._heading_chains(["# C#"])[0]))

    # C (N5) is in TreeSitterProviderTests (needs the parser) -----------------------------------------------------

    # D (N6) ----------------------------------------------------------------------------------------------------
    def test_d_no_compare_and_swap_claims(self):
        import inspect
        for text in (inspect.getsource(ma), inspect.getsource(mw.write_managed_bytes), mw.ConcurrentChangeError.__doc__,
                     inspect.getsource(sys.modules[__name__])):
            self.assertNotIn("is never " + "overwritten", text)
        self.assertIn("not an atomic compare-and-swap", mw.write_managed_bytes.__doc__)
        doc = " ".join(ma.__doc__.split())
        self.assertIn("can still race the final step", doc)
        self.assertIn("does not defend against another process of the same user", doc)

    # N7 --------------------------------------------------------------------------------------------------------
    def test_n7_partial_write_reports_the_real_state_per_entry(self):
        self.simple()
        [sid] = self.accept_all()["accepted"]
        with mock.patch.object(ma, "_ledger_events", side_effect=sqlite3.OperationalError("injected ledger failure")):
            res = ma.accept_suggestions(self.root, [sid])  # an unchanged re-pin
        self.assertTrue(res["partialWrite"])
        self.assertEqual({sid: "unchanged from a recorded acceptance: still in effect"}, res["entryStates"])
        self.assertNotIn("To recover", res["error"])
        self.assertEqual(("attached", "active"), tuple(ma.list_sidecar_anchors(self.root)["entries"][0][k] for k in ("state", "status")))
        self.write("a.py", "def validate_token(x, y):\n    return y\n")  # a changed re-pin
        with mock.patch.object(ma, "_ledger_events", side_effect=sqlite3.OperationalError("injected ledger failure")):
            res = ma.accept_suggestions(self.root, [sid])
        self.assertEqual({sid: "new or changed: not in effect until accepted again"}, res["entryStates"])
        self.assertIn(f"memlet admin anchors accept {sid}", res["error"])
        self.assertEqual("rejected", ma.list_sidecar_anchors(self.root)["entries"][0]["state"])

    # N8 --------------------------------------------------------------------------------------------------------
    def test_n8_a_config_that_would_outgrow_the_reader_cap_is_refused_before_writing(self):
        self.simple()
        (self.root / ".memlet").mkdir()
        (self.root / mw.CONFIG_PATH).write_text(json.dumps({"padding": "x" * (mw.MAX_CONFIG_BYTES - 50)}))
        before = (self.root / mw.CONFIG_PATH).read_bytes()
        res = ma.accept_suggestions(self.root, self.all_ids())
        self.assertFalse(res["ok"])
        self.assertIn("over the", res["error"])
        self.assertIn("old file is intact", res["error"])
        self.assertEqual(before, (self.root / mw.CONFIG_PATH).read_bytes())
        (self.root / mw.CONFIG_PATH).write_text(json.dumps({"padding": "x" * (mw.MAX_CONFIG_BYTES + 10)}))
        for res in (ma.accept_suggestions(self.root, ["anc-000000000000"]), ma.drop_sidecar_anchors(self.root, ["anc-000000000000"])):
            self.assertFalse(res["ok"])
            self.assertIn("editing it by hand", res["error"])

    # N9 / N10 --------------------------------------------------------------------------------------------------
    def test_n9_single_quoted_examples_but_not_apostrophes(self):
        self.write("a.py", NORMAL)
        self.write("AGENTS.md", "- Treat 'use `validate_token()`' as obsolete advice.\n")
        self.assertEqual({"mention_in_example_or_quotation"}, self.reasons(ma.suggest_anchors(self.root)))
        self.write("AGENTS.md", "- Don't skip `validate_token()`; it's required by the users' sessions.\n")
        self.assertEqual({("a.py::validate_token", "single-definition-found")}, self.anchors_of(ma.suggest_anchors(self.root)))

    def test_n10_a_rule_too_long_to_scan_makes_the_result_incomplete(self):
        self.write("a.py", NORMAL + "\n\ndef refresh_session(x):\n    return x\n")
        self.write("AGENTS.md", "- In `a.py`, use `refresh_session()`.\n- Always use `validate_token()` " + "filler " * 3000 + ".\n")
        res = ma.suggest_anchors(self.root)
        self.assertTrue(res["truncated"])
        self.assertIn("rule_not_scanned_completely:AGENTS.md:2", res["truncation"])
        acc = ma.accept_suggestions(self.root, all_unique=True)
        self.assertFalse(acc["ok"])
        self.assertIn("incomplete", acc["error"])
        self.assertTrue(ma.accept_suggestions(self.root, self.all_ids())["ok"])  # the complete one, by id

if __name__ == "__main__":
    unittest.main()
