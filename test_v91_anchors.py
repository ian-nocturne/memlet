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
        ma._INDEX_CACHE.clear()
        self.root = self.tmp / "repo"
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

    def accept_all(self):
        res = ma.accept_suggestions(self.root, all_unique=True)
        self.assertTrue(res["ok"], res)
        return res


class SuggestionPrecisionTests(_Repo):
    def test_unique_file_qualified_duplicated_and_prose_mentions(self):
        self.fixture()
        res = ma.suggest_anchors(self.root)
        got = {(s["source"], s["line"], s["anchor"], s["confidence"]) for s in res["suggestions"]}
        self.assertEqual({
            ("AGENTS.md", 2, "src/auth.py::validate_token", "unique-symbol"),        # backticked call form, defined once
            ("AGENTS.md", 3, "src/billing.py::charge_customer", "file+symbol"),     # file named in the same rule
            ("AGENTS.md", 9, "src/billing.py::apply_discount", "unique-symbol"),    # prose `name()` form
            ("AGENTS.md", 10, "src/auth.py::Session.refresh_session", "unique-symbol"),  # dotted name
            ("AGENTS.md", 11, "src/auth.py::Session", "file+symbol"),               # `path#Symbol`
        }, got)
        amb = {(a["line"], a["mention"]): a for a in res["ambiguous"]}
        # same name in tests and source: reported with both candidates, never guessed
        self.assertEqual("defined_in_tests_and_source", amb[(4, "charge_customer")]["reason"])
        self.assertEqual({"src/billing.py::charge_customer", "tests/test_billing.py::charge_customer"},
                         {c["anchor"] for c in amb[(4, "charge_customer")]["candidates"]})
        self.assertEqual("multiple_definitions", amb[(6, "save")]["reason"])
        self.assertEqual(2, amb[(6, "save")]["candidateCount"])
        # a common word that happens to be a unique symbol is not anchored on its own
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
        self.assertEqual(ma.suggestion_id("AGENTS.md", s["ruleSha256"], s["anchor"]), s["id"])
        self.assertRegex(s["id"], r"^anc-[0-9a-f]{12}$")
        for k in ("id", "source", "line", "ruleSha256", "rule", "anchor", "confidence", "reason"):
            self.assertIn(k, s)
        self.assertEqual({"path": "src/auth.py", "symbol": "validate_token", "line": 1, "kind": "function", "test": False},
                         s["target"])
        # the rule is shown next to the symbol's file:line
        self.assertIn("`AGENTS.md:2`: Always call `validate_token()`", a["formatted"])
        self.assertIn("at `src/auth.py:1` (function)", a["formatted"])
        # the id is stable when the rule only moves
        self.write("AGENTS.md", "# Intro\n\n" + AGENTS)
        moved = self.by_anchor(ma.suggest_anchors(self.root))["src/auth.py::validate_token"]
        self.assertEqual((s["id"], 4), (moved["id"], moved["line"]))

    def test_generated_vendored_nested_and_hidden_code_is_not_indexed(self):
        self.fixture()
        self.write("vendor/lib.py", "def validate_token(x):\n    return x\n")
        self.write("node_modules/pkg/index.js", "function validate_token(x) { return x; }\n")
        self.write("gen/api_client.py", "# @generated by protoc. DO NOT EDIT.\ndef validate_token(x):\n    return x\n")
        self.write("web/bundle.min.js", "function validate_token(x) { return x; }\n")
        self.write("nested/.git/HEAD", "ref: refs/heads/main\n")
        self.write("nested/copy.py", "def validate_token(x):\n    return x\n")
        self.write(".hidden/tool.py", "def validate_token(x):\n    return x\n")
        res = ma.suggest_anchors(self.root)
        self.assertEqual("unique-symbol", self.by_anchor(res)["src/auth.py::validate_token"]["confidence"])
        self.assertGreaterEqual(res["scanned"]["generatedSkipped"], 2)

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
        self.assertEqual({"id", "source", "line", "ruleSha256", "anchor", "pin"}, set(entry))
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
            res = ma.accept_suggestions(self.root, all_unique=True, dry_run=True)
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
            t = threading.Thread(target=lambda: order.append(ma.accept_suggestions(self.root, all_unique=True)["ok"]))
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
        self.assertIn({"anchor": "src/billing.py::charge_customer", "reason": "signature_drift", "suggested": True},
                      h["quarantinedStaleAnchors"])
        # recall sets it apart exactly as it does a written stale anchor
        rec = mb.unified_recall(self.root, "charge customer idempotent")
        stale = [i for i in rec["sections"]["staleRules"] if i["source"].startswith("AGENTS.md:3")]
        self.assertEqual(1, len(stale), rec["sections"])
        self.assertEqual([{"anchor": "src/billing.py::charge_customer", "status": "stale", "reason": "signature_drift",
                           "suggested": True}], stale[0]["anchors"])
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
                       "suggested": True}, h["reverifyBeforeUse"])
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

    def test_moving_a_rule_keeps_it_attached_and_moving_it_to_another_file_detaches(self):
        self.write("AGENTS.md", "# Preface\n\nSome words.\n" + AGENTS)
        idx = ma.sidecar_index(self.root)
        moved = {e["anchor"]: (e["currentLine"], e["moved"]) for e in idx["attached"]}
        self.assertEqual((6, True), moved["src/billing.py::charge_customer"])
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
        entry = {"id": ma.suggestion_id("AGENTS.md", sha, anchor), "source": "AGENTS.md", "line": 7, "ruleSha256": sha,
                 "anchor": anchor, "pin": {"ast": fp["ast"], "sig": fp["sig"], "req": fp["req"], "basis": fp["basis"]}}
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
        self.assertIn("{init,check,recall,handover,learn,ui,admin}", out)
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
        self.assertEqual({("pkg12/mod_1234.py::helper_number_1234", "unique-symbol"),
                          ("pkg0/mod_7.py::helper_number_7", "file+symbol"), ("pkg0/mod_5.py::helper_number_5", "unique-symbol")},
                         {(s["anchor"], s["confidence"]) for s in res["suggestions"]})
        with mock.patch.object(ma, "MAX_INDEX_FILES", 100):
            capped = ma.suggest_anchors(self.root)
        self.assertTrue(capped["truncated"])
        self.assertEqual(100, capped["scanned"]["codeFiles"])
        # uniqueness is unproven in a truncated index: never claimed; a file-qualified mention still resolves
        self.assertEqual({("pkg0/mod_7.py::helper_number_7", "file+symbol")},
                         {(s["anchor"], s["confidence"]) for s in capped["suggestions"]})
        self.assertIn("symbol_index_truncated_uniqueness_unproven", {a["reason"] for a in capped["ambiguous"]})
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

    def test_lexical_pin_keeps_its_status_when_a_provider_appears(self):
        mw.init_project(self.root)
        self.assertEqual(("compatible-unverified", "ts_js_lexical_scan_unverified", ca.TS_LEXICAL_BASIS), self.status())
        self.register(FakeExact())
        self.assertEqual(("compatible-unverified", "ts_js_lexical_scan_unverified", ca.TS_LEXICAL_BASIS), self.status())

    def test_exact_pin_is_active_and_never_silently_reclassified_when_the_provider_goes(self):
        prov = FakeExact()
        self.register(prov)
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

    def test_exact_pin_body_and_signature_changes(self):
        self.register(FakeExact())
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

    def test_a_crashing_provider_falls_back_to_the_lexical_scanner(self):
        class Boom(FakeExact):
            def fingerprint(self, path, text):
                raise RuntimeError("parser crashed")
        self.register(Boom())
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

    def test_real_parser_pins_are_active_and_track_changes(self):
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


if __name__ == "__main__":
    unittest.main()
