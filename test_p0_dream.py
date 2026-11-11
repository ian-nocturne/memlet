"""P0 hardening tests for `memlet.memlet_dream` (audit P0-6, P0-7 timestamp item, JSONL reader).

Stdlib only; must pass on Python 3.9+.
"""

import contextlib
import io
import json
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from memlet import memlet_dream as md

MODULE_PATH = Path(md.__file__)


def _tmpdir(test: unittest.TestCase, prefix: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    test.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
    return d


def _quiet_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = md.main(argv)
    return rc, out.getvalue(), err.getvalue()


LOG_LINES = (
    "2026-11-09T09:00:00Z WARN: initial workaround --no-verify\n"
    "2026-11-09T10:00:00Z RULE: never pass --no-verify; run pytest first\n"
)

SELF_PROMOTING_CANDIDATES = [
    {
        "lesson_id": "L-CLI-OLD",
        "category": "verified_workaround",
        "scope_tag": "git-hooks",
        "title": "Temporary --no-verify workaround",
        "src_spans": ["s1.log:1-1"],
        "verdict": "SUPPORTED",
        "edgeVerdict": "VALID",
        "edges": [{"edge_type": "GOVERNS", "to_id": "SCOPE:git-hooks", "src_span": "s1.log:1-1"}],
    },
    {
        "lesson_id": "L-CLI-NEW",
        "category": "verified_workaround",
        "scope_tag": "git-hooks",
        "title": "Never pass --no-verify; run pytest first",
        "src_spans": ["s1.log:2-2"],
        "verdict": "VERIFIED",
        "edgeVerdict": "VALID",
        "edges": [
            {"edge_type": "SUPERSEDES", "to_id": "L-CLI-OLD", "src_span": "s1.log:2-2", "target_span": "s1.log:1-1"},
            {"edge_type": "GOVERNS", "to_id": "SCOPE:git-hooks", "src_span": "s1.log:2-2"},
        ],
    },
]


class _QueueFixture(unittest.TestCase):
    def _build(self, with_db=True):
        tmp = _tmpdir(self, "p0-dream-")
        logs = tmp / "logs"
        logs.mkdir()
        (logs / "s1.log").write_text(LOG_LINES, encoding="utf-8")
        cands = tmp / "cands.json"
        cands.write_text(json.dumps(SELF_PROMOTING_CANDIDATES), encoding="utf-8")
        out_json = tmp / "out" / "APPROVAL-QUEUE.json"
        out_db = tmp / "out" / "ontology.sqlite3" if with_db else None
        receipt = md.run_dream_build(
            snapshot_path=str(logs),
            candidates_path=str(cands),
            out_json_path=str(out_json),
            out_db_path=str(out_db) if out_db else None,
        )
        return tmp, logs, out_json, out_db, receipt


class PendingByDefaultTests(_QueueFixture):
    def test_build_marks_every_node_and_edge_pending(self):
        _tmp, _logs, out_json, out_db, receipt = self._build()
        self.assertTrue(receipt["approvalRequired"])
        graph = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertEqual(2, len(graph["nodes"]))
        for n in graph["nodes"]:
            self.assertEqual("PENDING", n["verdict"])
            self.assertEqual("PENDING", n["edgeVerdict"])
            self.assertNotIn("approval", n)
            for e in n["edges"]:
                self.assertEqual("PENDING", e["edgeVerdict"])
        for e in graph["edges"]:
            self.assertEqual("PENDING", e["edgeVerdict"])
        with contextlib.closing(sqlite3.connect(str(out_db))) as conn:
            self.assertEqual({("PENDING",)}, set(conn.execute("SELECT verdict FROM ontology_nodes").fetchall()))
            self.assertEqual({("PENDING",)}, set(conn.execute("SELECT edge_verdict FROM ontology_edges").fetchall()))

    def test_self_reported_verdicts_are_recorded_but_not_trusted(self):
        _tmp, logs, out_json, _db, _r = self._build()
        graph = json.loads(out_json.read_text(encoding="utf-8"))
        claims = {n["id"]: (n["claimedVerdict"], n["claimedEdgeVerdict"], n["verdict"]) for n in graph["nodes"]}
        self.assertEqual(("SUPPORTED", "VALID", "PENDING"), claims["L-CLI-OLD"])
        self.assertEqual(("VERIFIED", "VALID", "PENDING"), claims["L-CLI-NEW"])
        res = md.run_dream_recall(str(out_json), snapshot_path=str(logs), query_text="git-hooks pytest")
        self.assertTrue(res["ok"])
        self.assertEqual("sha256_verified", res["pinVerification"])
        self.assertEqual([], res["includedNodeIds"])
        self.assertEqual(2, res["excludedUndecidedOrRejected"]["pendingNodes"])
        self.assertNotIn("L-CLI", res["formatted"])

    def test_autonomous_build_is_pending_too(self):
        tmp = _tmpdir(self, "p0-auto-")
        logs = tmp / "logs"
        logs.mkdir()
        (logs / "a.log").write_text(LOG_LINES, encoding="utf-8")
        out_json = tmp / "q.json"
        md.run_dream_build(snapshot_path=str(logs), out_json_path=str(out_json))
        graph = json.loads(out_json.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(graph["nodes"]), 1)
        self.assertTrue(all(n["verdict"] == "PENDING" for n in graph["nodes"]))
        self.assertTrue(all(e["edgeVerdict"] == "PENDING" for e in graph["edges"]))

    def test_legacy_auto_promoted_queue_without_approval_is_not_recalled(self):
        _tmp, logs, out_json, _db, _r = self._build()
        graph = json.loads(out_json.read_text(encoding="utf-8"))
        # Simulate a queue written by the old builder: SUPPORTED/VALID with no human approval record.
        for n in graph["nodes"]:
            n["verdict"] = "ALREADY_SUPERSEDED" if n["statusKind"] == "superseded_rule" else "SUPPORTED"
            n["edgeVerdict"] = "VALID"
            for e in n["edges"]:
                e["edgeVerdict"] = "VALID"
        for e in graph["edges"]:
            e["edgeVerdict"] = "VALID"
        out_json.write_text(json.dumps(graph), encoding="utf-8")
        res = md.run_dream_recall(str(out_json), snapshot_path=str(logs), query_text="git-hooks")
        self.assertEqual([], res["includedNodeIds"])


class ApprovalApiTests(_QueueFixture):
    def test_list_pending_then_approve_makes_items_recallable(self):
        _tmp, logs, out_json, out_db, _r = self._build()
        pend = md.list_pending(str(out_json))
        self.assertEqual({"L-CLI-OLD", "L-CLI-NEW"}, {n["id"] for n in pend["pendingNodes"]})
        self.assertEqual(3, pend["counts"]["pendingEdges"])

        rec = md.approve_queue_items(str(out_json), ["L-CLI-OLD", "L-CLI-NEW"], approver="ray")
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(["L-CLI-OLD", "L-CLI-NEW"], rec["applied"])
        self.assertEqual("recorded", rec["snapshotSource"])
        self.assertRegex(rec["decidedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

        graph = json.loads(out_json.read_text(encoding="utf-8"))
        for n in graph["nodes"]:
            self.assertEqual("approved", n["approval"]["decision"])
            self.assertEqual("ray", n["approval"]["by"])
            self.assertEqual(md.node_approval_digest(n), n["approval"]["digest"])
            self.assertEqual("memlet.dream_approval.v1", n["approval"]["schema"])
            self.assertIn(n["verdict"], ("SUPPORTED", "ALREADY_SUPERSEDED"))
        self.assertTrue(all(e["edgeVerdict"] == "VALID" for e in graph["edges"]))
        self.assertTrue(md.verify_ontology_contract(graph, md.load_snapshot(str(logs)))["ok"])
        self.assertEqual(0, md.list_pending(graph)["counts"]["pendingNodes"])

        # SQLite stays consistent with the JSON.
        with contextlib.closing(sqlite3.connect(str(out_db))) as conn:
            rows = dict(conn.execute("SELECT id, verdict FROM ontology_nodes").fetchall())
            self.assertEqual({"L-CLI-OLD": "SUPPORTED", "L-CLI-NEW": "SUPPORTED"}, rows)
            self.assertEqual({("approved", "ray")}, set(conn.execute("SELECT decision, decided_by FROM ontology_nodes").fetchall()))
            self.assertEqual({("VALID",)}, set(conn.execute("SELECT edge_verdict FROM ontology_edges").fetchall()))

        res = md.run_dream_recall(str(out_json), query_text="git-hooks pytest")
        self.assertTrue(res["ok"])
        self.assertEqual("sha256_verified", res["pinVerification"])
        self.assertEqual({"L-CLI-OLD", "L-CLI-NEW"}, set(res["includedNodeIds"]))
        self.assertIn("[SUPERSEDED — DO NOT APPLY AS CURRENT]", res["formatted"])
        self.assertIn("[ACTIVE — APPLY THIS]", res["formatted"])

        # Same path through the module CLI with --out-md (mirrors the existing CLI test).
        out_md = out_json.parent / "capsule.md"
        rc, _o, _e = _quiet_main(["dream-recall", "--graph", str(out_json), "--snapshot", str(logs),
                                  "--query", "git-hooks", "pytest", "--out-md", str(out_md)])
        self.assertEqual(0, rc)
        self.assertIn("[ACTIVE — APPLY THIS]", out_md.read_text(encoding="utf-8"))

    def test_approval_is_idempotent(self):
        _tmp, _logs, out_json, _db, _r = self._build()
        md.approve_queue_items(str(out_json), ["L-CLI-OLD"], approver="ray")
        before = out_json.read_bytes()
        again = md.approve_queue_items(str(out_json), ["L-CLI-OLD"], approver="someone-else")
        self.assertEqual([], again["applied"])
        self.assertEqual(["L-CLI-OLD"], again["unchanged"])
        self.assertEqual(before, out_json.read_bytes())

    def test_approving_id_whose_span_hash_changed_is_refused(self):
        _tmp, logs, out_json, out_db, _r = self._build()
        (logs / "s1.log").write_text(
            "2026-11-09T09:00:00Z WARN: initial workaround --no-verify (edited later)\n"
            "2026-11-09T10:00:00Z RULE: never pass --no-verify; run pytest first\n",
            encoding="utf-8",
        )
        before = out_json.read_bytes()
        rec = md.approve_queue_items(str(out_json), ["L-CLI-OLD"], approver="ray")
        self.assertFalse(rec["ok"])
        self.assertEqual([], rec["applied"])
        self.assertTrue(rec["refused"][0]["reason"].startswith("pin_does_not_verify:"), rec)
        self.assertEqual(before, out_json.read_bytes())
        # L-CLI-NEW's own pin (line 2) still verifies, but its SUPERSEDES edge targets line 1: refused as a whole.
        rec_new = md.approve_queue_items(str(out_json), ["L-CLI-NEW"], approver="ray")
        self.assertEqual([], rec_new["applied"])
        self.assertIn("pin_does_not_verify", rec_new["refused"][0]["reason"])
        self.assertTrue(all(n["verdict"] == "PENDING" for n in json.loads(out_json.read_text(encoding="utf-8"))["nodes"]))

    def test_approval_without_locatable_snapshot_is_refused(self):
        tmp, logs, out_json, _db, _r = self._build()
        shutil.rmtree(logs)
        rec = md.approve_queue_items(str(out_json), ["L-CLI-OLD"], approver="ray")
        self.assertFalse(rec["ok"])
        self.assertEqual("no_snapshot_to_verify_pins", rec["refused"][0]["reason"])

    def test_reject_is_final_and_never_recalled(self):
        _tmp, logs, out_json, _db, _r = self._build()
        rej = md.reject_queue_items(str(out_json), ["L-CLI-NEW"], approver="ray", reason="not a real rule")
        self.assertEqual(["L-CLI-NEW"], rej["applied"])
        flip = md.approve_queue_items(str(out_json), ["L-CLI-NEW"], approver="ray")
        self.assertEqual("already_rejected", flip["refused"][0]["reason"])
        md.approve_queue_items(str(out_json), ["L-CLI-OLD"], approver="ray")
        res = md.run_dream_recall(str(out_json), snapshot_path=str(logs), query_text="git-hooks")
        self.assertEqual(["L-CLI-OLD"], res["includedNodeIds"])
        self.assertEqual(1, res["excludedUndecidedOrRejected"]["rejectedNodes"])
        self.assertNotIn("SUPERSEDED — DO NOT APPLY", res["formatted"])  # rejected supersession claim is not applied

    def test_unknown_id_and_missing_approver(self):
        _tmp, _logs, out_json, _db, _r = self._build()
        rec = md.approve_queue_items(str(out_json), ["NOPE"], approver="ray")
        self.assertEqual("unknown_id", rec["refused"][0]["reason"])
        with self.assertRaises(ValueError):
            md.approve_queue_items(str(out_json), ["L-CLI-OLD"], approver="  ")

    def test_old_schema_sqlite_is_migrated_on_approval(self):
        _tmp, _logs, out_json, out_db, _r = self._build()
        out_db.unlink()
        with contextlib.closing(sqlite3.connect(str(out_db))) as conn:
            with conn:
                conn.execute("CREATE TABLE ontology_nodes (id TEXT PRIMARY KEY, category TEXT NOT NULL, status_kind TEXT NOT NULL, "
                             "scope_tag TEXT NOT NULL, title TEXT NOT NULL, importance INTEGER NOT NULL, vendor_origins TEXT NOT NULL, "
                             "primary_span TEXT NOT NULL, quote_text TEXT NOT NULL, sha256_pin TEXT NOT NULL, superseded_by_span TEXT, "
                             "superseded_by_sha256 TEXT, verified_by TEXT, verdict TEXT, timestamp TEXT)")
                conn.execute("CREATE TABLE ontology_edges (edge_id TEXT PRIMARY KEY, edge_type TEXT NOT NULL, from_id TEXT NOT NULL, "
                             "to_id TEXT NOT NULL, target_label TEXT NOT NULL, src_span TEXT NOT NULL, src_sha256 TEXT NOT NULL, "
                             "target_span TEXT, target_sha256 TEXT, vendor_origin TEXT)")
        rec = md.approve_queue_items(str(out_json), ["L-CLI-OLD"], approver="ray")
        self.assertEqual(out_db.resolve(), Path(rec["dbPath"]).resolve())
        with contextlib.closing(sqlite3.connect(str(out_db))) as conn:
            rows = dict(conn.execute("SELECT id, decision FROM ontology_nodes").fetchall())
        self.assertEqual({"L-CLI-OLD": "approved", "L-CLI-NEW": None}, rows)

    def test_edge_level_approval(self):
        _tmp, _logs, out_json, _db, _r = self._build()
        graph = json.loads(out_json.read_text(encoding="utf-8"))
        eid = graph["edges"][0]["edgeId"]
        rec = md.approve_queue_items(str(out_json), [eid], approver="ray")
        self.assertEqual([eid], rec["applied"])
        graph = json.loads(out_json.read_text(encoding="utf-8"))
        copies = [e for e in graph["edges"] if e["edgeId"] == eid]
        copies += [e for n in graph["nodes"] for e in n["edges"] if e["edgeId"] == eid]
        self.assertEqual(2, len(copies))
        self.assertTrue(all(e["edgeVerdict"] == "VALID" and e["approval"]["by"] == "ray" for e in copies))


class RecallVerificationTests(_QueueFixture):
    def _approved(self):
        tmp, logs, out_json, out_db, r = self._build()
        md.approve_queue_items(str(out_json), ["L-CLI-OLD", "L-CLI-NEW"], approver="ray")
        return tmp, logs, out_json

    def test_tampered_snapshot_is_rejected_by_default_recall(self):
        _tmp, logs, out_json = self._approved()
        (logs / "s1.log").write_text(
            "2026-11-09T09:00:00Z WARN: initial workaround --no-verify\n"
            "2026-11-09T10:00:00Z RULE: always pass --no-verify\n",
            encoding="utf-8",
        )
        res = md.run_dream_recall(str(out_json), query_text="git-hooks")  # per-item snapshot verification
        self.assertEqual(["L-CLI-OLD"], res["includedNodeIds"])
        self.assertEqual(["L-CLI-NEW"], res["unverifiableNodeIds"])
        self.assertEqual(1, res["unverifiableCount"])
        self.assertIn("Citation verification failed", res["formatted"])
        rc, out, err = _quiet_main(["dream-recall", "--graph", str(out_json)])
        self.assertEqual(0, rc)
        self.assertEqual("", err)
        self.assertIn("Citation verification failed", out)

    def test_no_snapshot_returns_nothing_unless_explicit_opt_out(self):
        _tmp, logs, out_json = self._approved()
        shutil.rmtree(logs)
        res = md.run_dream_recall(str(out_json), query_text="git-hooks")
        self.assertFalse(res["ok"])
        self.assertEqual("unavailable_no_snapshot", res["pinVerification"])
        self.assertEqual([], res["includedNodeIds"])
        self.assertIn("VERIFICATION UNAVAILABLE", res["formatted"])
        self.assertNotIn("L-CLI", res["formatted"])
        self.assertEqual(3, res["exitCode"])  # honoured by memory_cli.cli()
        rc, out, _e = _quiet_main(["dream-recall", "--graph", str(out_json)])
        self.assertEqual(3, rc)
        self.assertIn("VERIFICATION UNAVAILABLE", out)

        opt = md.run_dream_recall(str(out_json), query_text="git-hooks", allow_unverified=True)
        self.assertTrue(opt["ok"])
        self.assertEqual("UNVERIFIED_OPT_OUT", opt["pinVerification"])
        self.assertIn("# UNVERIFIED: pins were NOT re-checked", opt["formatted"])
        self.assertEqual({"L-CLI-OLD", "L-CLI-NEW"}, set(opt["includedNodeIds"]))

    def test_opt_out_still_never_returns_pending(self):
        tmp, logs, out_json, _db, _r = self._build()
        shutil.rmtree(logs)
        opt = md.run_dream_recall(str(out_json), query_text="git-hooks", allow_unverified=True)
        self.assertEqual([], opt["includedNodeIds"])

    def test_hand_edited_approval_digest_does_not_bypass_binding(self):
        _tmp, logs, out_json = self._approved()
        graph = json.loads(out_json.read_text(encoding="utf-8"))
        graph["nodes"][0]["approval"]["digest"] = "sha256:" + "0" * 64
        out_json.write_text(json.dumps(graph), encoding="utf-8")
        res = md.run_dream_recall(str(out_json), snapshot_path=str(logs), query_text="git-hooks")
        self.assertNotIn(graph["nodes"][0]["id"], res["includedNodeIds"])


class ExtractorWordBoundaryTests(unittest.TestCase):
    def test_ban_does_not_match_banner_and_tests_pass_is_not_a_workaround(self):
        snap = {"s.log": [
            "2026-11-09T09:00:00Z [claude] Updated the banner component colours",
            "2026-11-09T09:05:00Z [claude] Tests pass now",
            "2026-11-09T09:06:00Z [codex] Verdict: PASS on all checks",
            "2026-11-09T09:07:00Z [claude] The forbidden-fruit theme is a joke name",
        ]}
        rep = md.extract_candidates_with_report(snap)
        titles = [c["title"] for c in rep["candidates"]]
        self.assertFalse(any("banner" in t for t in titles), titles)
        self.assertFalse(any("Tests pass now" in t for t in titles), titles)
        self.assertFalse(any("Verdict: PASS" in t for t in titles), titles)
        self.assertEqual(1, len(rep["candidates"]))  # `forbidden` is a whole word
        self.assertFalse(rep["truncated"])

    def test_whole_word_cues_still_fire(self):
        snap = {"s.log": [
            "2026-11-09T09:00:00Z [claude] We ban force-push on main",
            "2026-11-09T09:01:00Z [claude] Workaround: use zsh arrays",
            "2026-11-09T09:02:00Z [claude] 이 명령은 사용 금지",
        ]}
        cats = [c["category"] for c in md.extract_candidates_with_report(snap)["candidates"]]
        self.assertEqual(["repeated_pitfall", "verified_workaround", "repeated_pitfall"], cats)

    def test_cap_is_kept_and_truncation_reported(self):
        lines = [f"2026-11-09T09:{i:02d}:00Z [claude] never do thing {i}" for i in range(30)]
        rep = md.extract_candidates_with_report({"s.log": lines})
        self.assertEqual(24, len(rep["candidates"]))
        self.assertEqual(24, rep["cap"])
        self.assertTrue(rep["truncated"])
        self.assertEqual(6, rep["omittedCount"])
        self.assertEqual(24, len(md.extract_candidates_from_snapshot({"s.log": lines})))

        tmp = _tmpdir(self, "p0-cap-")
        (tmp / "logs").mkdir()
        (tmp / "logs" / "s.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        receipt = md.run_dream_build(snapshot_path=str(tmp / "logs"), out_json_path=str(tmp / "q.json"))
        self.assertTrue(receipt["candidatesTruncated"])
        self.assertEqual(6, receipt["candidatesOmittedCount"])
        self.assertEqual(24, receipt["candidateCap"])


class NestedPathTests(unittest.TestCase):
    def test_nested_span_pins_round_trip(self):
        tmp = _tmpdir(self, "p0-nested-")
        nested = tmp / "logs" / "team" / "day1"
        nested.mkdir(parents=True)
        (nested / "s1.log").write_text(LOG_LINES, encoding="utf-8")
        snap = md.load_snapshot(str(tmp / "logs"))
        self.assertEqual(["team/day1/s1.log"], list(snap))
        self.assertEqual(("team/day1/s1.log", 1, 2), md.parse_span("team/day1/s1.log:1-2"))
        pin = md.resolve_pin(snap, "team/day1/s1.log:2")
        self.assertIsNotNone(pin)
        self.assertEqual("team/day1/s1.log:2", pin["span"])

        cands = [{
            "lesson_id": "L-NEST",
            "category": "repeated_pitfall",
            "scope_tag": "git-hooks",
            "title": "Never pass --no-verify",
            "src_spans": ["team/day1/s1.log:2"],
        }]
        (tmp / "c.json").write_text(json.dumps(cands), encoding="utf-8")
        q = tmp / "q.json"
        md.run_dream_build(snapshot_path=str(tmp / "logs"), candidates_path=str(tmp / "c.json"), out_json_path=str(q))
        graph = json.loads(q.read_text(encoding="utf-8"))
        self.assertEqual("team/day1/s1.log:2", graph["nodes"][0]["primaryPin"]["span"])
        self.assertTrue(md.verify_ontology_contract(graph, snap)["ok"])
        self.assertEqual(["L-NEST"], md.approve_queue_items(str(q), ["L-NEST"], approver="ray")["applied"])
        self.assertEqual(["L-NEST"], md.run_dream_recall(str(q), query_text="no-verify")["includedNodeIds"])

    def test_unsafe_paths_still_rejected(self):
        for bad in ("/etc/x.log:1", "a/../b.log:1", "../x.log:1", "a//b.log:1", "./a.log:1",
                    "a/./b.log:1", "a\\b.log:1", "C:/x.log:1", "a/b/:1", "a..b:1", "..:1"):
            self.assertIsNone(md.parse_span(bad), bad)


CLAUDE_CODE_JSONL = [
    json.dumps({"type": "summary", "summary": "Session about hooks", "leafUuid": "x"}),
    json.dumps({"type": "user", "timestamp": "2026-11-09T09:00:00.123Z", "sessionId": "s",
                "message": {"role": "user", "content": "Please never pass --no-verify\nin this repo"}}),
    "{this is not json",
    json.dumps({"type": "assistant", "timestamp": "2026-11-09T09:00:05.456Z",
                "message": {"role": "assistant", "type": "message", "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "Understood."},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                ]}}),
    json.dumps({"type": "user", "timestamp": "2026-11-09T09:00:06Z",
                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a b"}]}}),
    "",
    json.dumps([1, 2, 3]),
    json.dumps({"type": "user", "timestamp": "2026-11-09T09:01:00Z",
                "message": {"role": "user", "content": "Tests pass now"}}),
]

CODEX_JSONL = [
    json.dumps({"timestamp": "2026-11-09T10:00:00.000Z", "type": "session_meta", "payload": {"id": "abc", "cwd": "/x"}}),
    json.dumps({"timestamp": "2026-11-09T10:00:01.000Z", "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Do not run git push"}]}}),
    json.dumps({"timestamp": "2026-11-09T10:00:02.000Z", "type": "response_item",
                "payload": {"type": "reasoning", "summary": []}}),
    json.dumps({"timestamp": "2026-11-09T10:00:03.000Z", "type": "response_item",
                "payload": {"type": "function_call", "name": "shell", "arguments": "{}"}}),
    json.dumps({"timestamp": "2026-11-09T10:00:04.000Z", "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Workaround: use a dry run"}]}}),
    json.dumps({"timestamp": "2026-11-09T10:00:05.000Z", "type": "event_msg", "payload": {"type": "token_count"}}),
    '{"timestamp": "2026-11-09T10:00:06.000Z", "type": "response_item", "payl',
    json.dumps({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "legacy codex line"}]}),
]


class JsonlTranscriptTests(unittest.TestCase):
    def _write(self, path: Path, lines) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_claude_code_jsonl(self):
        tmp = _tmpdir(self, "p0-cc-")
        f = tmp / "cc-session.jsonl"
        self._write(f, CLAUDE_CODE_JSONL)
        lines, stats = md.read_jsonl_transcript(f)
        self.assertEqual(len(CLAUDE_CODE_JSONL), len(lines))  # line N == physical line N
        self.assertEqual(2, stats["malformedLines"])  # bad JSON + top-level array
        self.assertEqual(3, stats["messages"])
        self.assertEqual(2, stats["nonMessageLines"])  # summary + tool_result-only record
        self.assertEqual("2026-11-09T09:00:00.123Z [user] Please never pass --no-verify in this repo", lines[1])
        self.assertEqual("2026-11-09T09:00:05.456Z [assistant] Understood.", lines[3])
        self.assertEqual("", lines[2])
        self.assertNotIn("hidden", "\n".join(lines))

        snap, report = md.load_snapshot_with_report(f)
        self.assertEqual(["cc-session.jsonl"], list(snap))
        self.assertEqual(2, report["jsonlMalformedLines"])
        pin = md.resolve_pin(snap, "cc-session.jsonl:2")
        self.assertEqual(md.sha256_hex(lines[1]), pin["sha256"])
        self.assertIsNone(md.resolve_pin(snap, "cc-session.jsonl:3"))  # malformed line cannot be pinned
        self.assertEqual("2026-11-09T09:00:00.123Z", md.extract_line_timestamp(pin["quoteText"]))

    def test_codex_jsonl(self):
        tmp = _tmpdir(self, "p0-codex-")
        f = tmp / "rollout.jsonl"
        self._write(f, CODEX_JSONL)
        lines, stats = md.read_jsonl_transcript(f)
        self.assertEqual(len(CODEX_JSONL), len(lines))
        self.assertEqual(1, stats["malformedLines"])
        self.assertEqual(3, stats["messages"])
        self.assertEqual(4, stats["nonMessageLines"])
        self.assertEqual("2026-11-09T10:00:01.000Z [user] Do not run git push", lines[1])
        self.assertEqual("2026-11-09T10:00:04.000Z [assistant] Workaround: use a dry run", lines[4])
        self.assertEqual("[user] legacy codex line", lines[7])

    def test_directory_with_nested_jsonl_builds_pinned_pending_queue_and_writes_only_requested_outputs(self):
        tmp = _tmpdir(self, "p0-jsonl-dir-")
        snap_dir = tmp / "transcripts"
        self._write(snap_dir / "claude" / "cc.jsonl", CLAUDE_CODE_JSONL)
        self._write(snap_dir / "codex" / "2026" / "rollout.jsonl", CODEX_JSONL)
        before = sorted(p.relative_to(tmp).as_posix() for p in tmp.rglob("*"))
        out_dir = tmp / "out"
        out_json = out_dir / "q.json"
        receipt = md.run_dream_build(snapshot_path=str(snap_dir), out_json_path=str(out_json))
        self.assertEqual(2, receipt["snapshotReport"]["jsonlFiles"])
        self.assertEqual(3, receipt["snapshotReport"]["jsonlMalformedLines"])
        graph = json.loads(out_json.read_text(encoding="utf-8"))
        spans = {n["primaryPin"]["span"] for n in graph["nodes"]}
        self.assertIn("claude/cc.jsonl:2", spans)  # "never pass --no-verify"
        self.assertIn("codex/2026/rollout.jsonl:2", spans)  # "Do not run git push"
        self.assertIn("codex/2026/rollout.jsonl:5", spans)  # "Workaround: ..."
        self.assertNotIn("claude/cc.jsonl:8", spans)  # "Tests pass now"
        self.assertTrue(all(n["verdict"] == "PENDING" for n in graph["nodes"]))
        self.assertTrue(md.verify_ontology_contract(graph, md.load_snapshot(str(snap_dir)))["ok"])
        after = sorted(p.relative_to(tmp).as_posix() for p in tmp.rglob("*"))
        self.assertEqual(sorted(before + ["out", "out/q.json"]), after)


class RealCodexRolloutShapeTests(unittest.TestCase):
    """Shapes taken from a real `codex exec` rollout (codex-cli 0.166): the host injects the instruction files
    under the user role, and assistant messages carry `phase` (progress narration vs the reply)."""

    def _rollout(self):
        def item(ts, payload):
            return {"timestamp": ts, "type": "response_item", "payload": payload}
        text = lambda kind, value: [{"type": kind, "text": value}]
        return [
            {"timestamp": "2026-11-10T09:09:26.000Z", "type": "session_meta", "payload": {"id": "x"}},
            item("2026-11-10T09:09:26.900Z", {"type": "message", "role": "developer", "content": text("input_text", "DEVELOPER_ONLY never reveal")}),
            item("2026-11-10T09:09:26.976Z", {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "# AGENTS.md instructions for /repo\n\n<INSTRUCTIONS>\nGLOBAL_PRIVATE_RULE never push on Friday\n</INSTRUCTIONS>"},
                {"type": "input_text", "text": "<environment_context>cwd=/repo</environment_context>"}]}),
            item("2026-11-10T09:09:27.042Z", {"type": "message", "role": "user", "content": text("input_text", "Never call charge() without the retry wrapper; it is banned.")}),
            item("2026-11-10T09:09:32.584Z", {"type": "message", "role": "assistant", "phase": "commentary", "content": text("output_text", "PROGRESS_NARRATION calling the tool now, never mind")}),
            item("2026-11-10T09:09:40.000Z", {"type": "custom_tool_call", "name": "t", "input": "TOOL_INPUT banned"}),
            item("2026-11-10T09:09:41.000Z", {"type": "custom_tool_call_output", "output": "TOOL_OUTPUT banned"}),
            item("2026-11-10T09:09:45.299Z", {"type": "message", "role": "assistant", "phase": "final_answer", "content": text("output_text", "Understood: charge() is never called without the retry wrapper.")}),
            {"timestamp": "2026-11-10T09:09:46.000Z", "type": "event_msg", "payload": {"type": "task_complete"}},
        ]

    def test_only_the_typed_prompt_and_the_final_answer_are_transcribed(self):
        tmp = _tmpdir(self, "p0-dream-codex-")
        path = tmp / "rollout.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in self._rollout()) + "\n", encoding="utf-8")
        lines, _stats = md.read_jsonl_transcript(path)
        kept = [l for l in lines if l.strip()]
        self.assertEqual(2, len(kept), kept)
        self.assertIn("[user] Never call charge() without the retry wrapper", kept[0])
        self.assertIn("[assistant] Understood: charge() is never called", kept[1])
        blob = "\n".join(lines)
        for leaked in ("GLOBAL_PRIVATE_RULE", "environment_context", "DEVELOPER_ONLY", "PROGRESS_NARRATION", "TOOL_INPUT", "TOOL_OUTPUT"):
            self.assertNotIn(leaked, blob)
        # physical line numbers are preserved: line N of the result is record N of the file
        self.assertEqual(len(self._rollout()), len(lines))

    def test_a_typed_message_that_merely_mentions_agents_md_is_kept(self):
        rec = {"timestamp": "2026-11-10T09:00:00Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Please update the AGENTS.md instructions for billing."}]}}
        self.assertIn("Please update the AGENTS.md instructions", md._jsonl_record_to_line(rec))


class TimestampTests(unittest.TestCase):
    def test_three_forms_parse(self):
        no_colon = md.parse_iso_dt("2026-10-30T19:00:00+0900")
        long_frac = md.parse_iso_dt("2026-10-30T10:00:00.1234567Z")
        zulu = md.parse_iso_dt("2026-10-30T10:00:00Z")
        for dt in (no_colon, long_frac, zulu):
            self.assertIsNotNone(dt)
            self.assertEqual(0, dt.utcoffset().total_seconds())
        self.assertEqual(zulu, no_colon)
        self.assertEqual(123456, long_frac.microsecond)
        self.assertIsNotNone(md.parse_iso_dt("2026-10-30 10:00:00.5-0130"))
        self.assertIsNotNone(md.parse_iso_dt("2026-10-30T10:00:00.12+09:00"))

    def test_supersedes_with_offset_without_colon_is_pinned(self):
        snap = {"t.log": [
            "2026-10-30T10:00:00+0900 [claude] Old rule: budget 400",
            "2026-10-30T10:00:00.1234567+0900 [claude] New rule replaces it",
        ]}
        g = md.dream_pin_and_validate([{
            "lesson_id": "L-TS",
            "category": "superseded_directive",
            "status_kind": "superseded_rule",
            "scope_tag": "budget",
            "title": "Budget 400 superseded",
            "src_spans": ["t.log:1"],
            "superseded_by_src": "t.log:2",
        }], snap)
        self.assertEqual(["L-TS"], [n["id"] for n in g["nodes"]], g["dropped"])
        self.assertTrue(md.verify_ontology_contract(g, snap)["ok"])


class NoBenchmarkCodeTests(unittest.TestCase):
    def test_no_c8_specific_identifiers_remain(self):
        src = MODULE_PATH.read_text(encoding="utf-8")
        forbidden = [
            r"\bLA\d-\d{2}\b",            # hard-coded evaluation node ids
            r"PASS T\d",                  # per-log-string special cases
            r"37:VERDICT",
            r"Reviewed implementation",
            r"HANDOVER_TOKEN_BUDGET",
            r"1\. \*\*FIXED",
            r"목표 예산",
            r"\bM2-G\b",                  # experiment arm names in headers/docs
            r"\bM2-Flat\b",
            r"\"50%\"",                   # evaluation token whitelist
            r"\"m1\", \"m2\", \"m3\"",
            r"\"joining\"",               # evaluation-question stopwords
            r"\"question\"",
        ]
        for pat in forbidden:
            self.assertIsNone(re.search(pat, src), pat)



# ---------------------------------------------------------------------------------------------
# Fix round 1: regressions for the independent review findings (F1-F4, F7-F10, F13, F14).
# Inputs mirror /private/tmp/p0-review-dKqNCO/probe_*.py.
# ---------------------------------------------------------------------------------------------

import copy  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from unittest import mock  # noqa: E402

from memlet import memlet_cortex as mc  # noqa: E402
from memlet import memlet_inspector as mi  # noqa: E402

REPO = Path(__file__).resolve().parent
TWIN_LOG = "2026-11-09T09:00:00Z [user] Never skip tests\n2026-11-09T09:00:00Z [user] Never skip tests\n"
TWIN_CANDS = [{"lesson_id": "L-1", "category": "repeated_pitfall", "title": "Never skip tests",
               "scope_tag": "tests", "src_spans": ["s.log:1"]}]
PAIR_LOG = "2026-11-09T09:00:00Z [user] Old rule\n2026-11-09T10:00:00Z [user] New rule\n"
PAIR_CANDS = [
    {"lesson_id": "L-OLD", "category": "repeated_pitfall", "title": "Old rule", "src_spans": ["s.log:1"]},
    {"lesson_id": "L-NEW", "category": "verified_workaround", "title": "New rule", "src_spans": ["s.log:2"],
     "edges": [{"edge_type": "SUPERSEDES", "to_id": "L-OLD", "src_span": "s.log:2", "target_span": "s.log:1"}]},
]


def _mk_queue(test, log_text, cands, db_name=None, queue_subdir=None):
    w = _tmpdir(test, "p0-fix-")
    logs = w / "logs"
    logs.mkdir()
    (logs / "s.log").write_text(log_text, encoding="utf-8")
    cp = w / "c.json"
    cp.write_text(json.dumps(cands), encoding="utf-8")
    qdir = w / queue_subdir if queue_subdir else w
    qdir.mkdir(parents=True, exist_ok=True)
    q = qdir / "q.json"
    db = (w / db_name) if db_name else None
    md.run_dream_build(str(logs), str(cp), str(q), str(db) if db else None)
    return w, logs, q, db


def _promote(g, forged=False):
    for n in g["nodes"]:
        n["verdict"] = "SUPPORTED"
        n["edgeVerdict"] = "VALID"
        if forged:
            n["approval"] = {"decision": "approved", "pinSha256": n["primaryPin"]["sha256"]}
        for e in n["edges"]:
            e["edgeVerdict"] = "VALID"
            if forged:
                e["approval"] = {"decision": "approved", "pinSha256": e["srcPin"]["sha256"]}
    g["edges"] = copy.deepcopy([e for n in g["nodes"] for e in n["edges"]])


def _db_rows(db):
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        return conn.execute("SELECT id, verdict FROM ontology_nodes ORDER BY id").fetchall()


class F1ApprovalBindingTests(unittest.TestCase):
    def setUp(self):
        self.w, self.logs, self.q, _db = _mk_queue(self, TWIN_LOG, TWIN_CANDS)
        self.base = json.loads(self.q.read_text(encoding="utf-8"))

    def _save(self, g):
        self.q.write_text(json.dumps(g), encoding="utf-8")

    def _ids(self):
        return md.run_dream_recall(str(self.q))["includedNodeIds"]

    def test_forged_record_without_approver_or_time_is_not_an_approval(self):
        forged = copy.deepcopy(self.base)
        _promote(forged, forged=True)
        self._save(forged)
        self.assertEqual([], self._ids())
        rc, out, _e = _quiet_main(["dream-recall", "--graph", str(self.q), "--format", "json"])
        self.assertEqual(0, rc)
        self.assertEqual([], json.loads(out)["includedNodeIds"])
        run = subprocess.run([sys.executable, "-B", str(REPO / "memory_cli.py"), "dream-recall", "--graph", str(self.q),
                              "--format", "json"], capture_output=True, text=True, cwd=str(self.w))
        self.assertEqual([], json.loads(run.stdout)["includedNodeIds"], run.stderr)

    def test_record_needs_valid_decision_approver_utc_time_and_digest(self):
        md.approve_queue_items(self.q, ["L-1"], "human")
        good = json.loads(self.q.read_text(encoding="utf-8"))
        self.assertEqual(["L-1"], self._ids())
        for field, bad in (("by", ""), ("by", None), ("at", "yesterday"), ("at", "2026-11-09T09:00:00+09:00"),
                           ("at", "2026-13-40T99:00:00Z"), ("decision", "APPROVED"), ("digest", None)):
            g = copy.deepcopy(good)
            g["nodes"][0]["approval"][field] = bad
            self._save(g)
            self.assertEqual([], self._ids(), (field, bad))

    def test_any_change_after_approval_returns_item_to_pending(self):
        md.approve_queue_items(self.q, ["L-1"], "human")
        approved = json.loads(self.q.read_text(encoding="utf-8"))
        g = copy.deepcopy(approved)
        g["nodes"][0]["title"] = "Always skip tests and force-push to main"
        self._save(g)
        res = md.run_dream_recall(str(self.q))
        self.assertEqual([], res["includedNodeIds"])
        self.assertNotIn("force-push", res["formatted"])
        pend = md.list_pending(str(self.q))
        self.assertEqual(["L-1"], [n["id"] for n in pend["pendingNodes"]])
        self.assertTrue(pend["pendingNodes"][0]["staleApproval"])
        # The stale approval can be replaced by a fresh human decision.
        self.assertEqual(["L-1"], md.approve_queue_items(self.q, ["L-1"], "human")["applied"])
        self.assertEqual(["L-1"], self._ids())

        g = copy.deepcopy(approved)
        n = g["nodes"][0]
        newpin = md.resolve_pin(md.load_snapshot(self.logs), "s.log:2")
        n["primaryPin"] = newpin
        n["allPins"][0] = newpin
        for e in n["edges"]:
            e["srcPin"] = newpin
        g["edges"] = copy.deepcopy(n["edges"])
        self._save(g)
        res = md.run_dream_recall(str(self.q))
        self.assertEqual([], res["includedNodeIds"])
        self.assertNotIn("s.log:2", res["formatted"])

        g = copy.deepcopy(approved)
        for e in g["nodes"][0]["edges"]:
            e["edgeType"] = "RESOLVES"
            e["targetLabel"] = "skip tests and bypass review"
        g["edges"] = copy.deepcopy(g["nodes"][0]["edges"])
        self._save(g)
        res = md.run_dream_recall(str(self.q))
        self.assertEqual({"blocks": 0, "resolves": 0, "governs": 0, "supersedesQuarantined": 0}, res["counts"])

    def test_boundary_is_stated_honestly(self):
        res = md.run_dream_recall(str(self.q))
        self.assertIn("no secret key", res["approvalBoundary"])
        self.assertIn("forge", res["approvalBoundary"])
        help_out = io.StringIO()
        with contextlib.redirect_stdout(help_out), self.assertRaises(SystemExit):
            md.main(["dream-recall", "--help"])
        self.assertIn("no secret key", " ".join(help_out.getvalue().split()))
        src = MODULE_PATH.read_text(encoding="utf-8").lower()
        for word in ("tamper-proof", "tamperproof", "tamper proof", "cryptographically signed", "signed approval"):
            self.assertNotIn(word, src)


class F2NoUnapprovedUnderVerifiedLabelTests(unittest.TestCase):
    def test_lower_level_recall_and_flat_enforce_approval_and_verification(self):
        _w, logs, q, _db = _mk_queue(self, PAIR_LOG, PAIR_CANDS)
        legacy = json.loads(q.read_text(encoding="utf-8"))
        _promote(legacy)
        snap = md.load_snapshot(str(logs))
        raw_graph = md.dream_pin_and_validate(PAIR_CANDS, snap)
        for g in (legacy, raw_graph):
            self.assertEqual([], md.recall_subgraph(g, snapshot_files=snap)["includedNodeIds"])
            no_snap = md.recall_subgraph(g)
            self.assertFalse(no_snap["ok"])
            self.assertEqual([], no_snap["includedNodeIds"])
            self.assertEqual([], md.recall_subgraph(g, allow_unverified=True)["includedNodeIds"])
            flat = md.format_flat_memory(g, snapshot_files=snap)
            self.assertNotIn("L-OLD", flat)
            self.assertNotIn("Verified Lessons", flat)
            self.assertNotIn("L-OLD", md.format_flat_memory(g))
            # Explicit raw opt-in is allowed, but labelled UNAPPROVED, never "Verified"/"Approved".
            raw = md.recall_subgraph(g, snapshot_files=snap, require_approval=False)
            self.assertIn("L-OLD", raw["includedNodeIds"])
            self.assertIn("# UNAPPROVED", raw["formatted"])
            self.assertEqual("UNAPPROVED_RAW", raw["reviewState"])
            raw_flat = md.format_flat_memory(g, snapshot_files=snap, require_approval=False)
            self.assertTrue(raw_flat.startswith("# Memlet Dream UNAPPROVED Candidates (flat)"))
            self.assertNotIn("Verified Lessons", raw_flat)
            self.assertNotIn("Approved Lessons", raw_flat)

    def test_cortex_and_inspector_label_unapproved_items_pending(self):
        w, _logs, q, _db = _mk_queue(self, PAIR_LOG, PAIR_CANDS)
        legacy = json.loads(q.read_text(encoding="utf-8"))
        _promote(legacy)
        ws = w / "ws"
        (ws / ".memlet" / "dream").mkdir(parents=True)
        (ws / ".memlet" / "dream" / "APPROVAL-QUEUE.json").write_text(json.dumps(legacy), encoding="utf-8")
        lineage = mc._load_dream_lineage({"path": str(ws)})
        self.assertEqual({"L-OLD", "L-NEW"}, {n["id"] for n in lineage["nodes"]})
        for n in lineage["nodes"]:
            self.assertEqual("PENDING", n["verdict"])
            self.assertEqual("pending", n["reviewState"])
            self.assertEqual("awaiting review", n["verifiedBy"])
            self.assertFalse(n["inRecallCapsule"])
        self.assertEqual(2, lineage["pendingReviewCount"])
        view = mi._live_workspace(ws, [])
        self.assertEqual({("L-OLD", "PENDING", False), ("L-NEW", "PENDING", False)},
                         {(n["id"], n["verdict"], n["inRecallCapsule"]) for n in view["ontologyLineage"]["nodes"]})
        self.assertIn('n.reviewState==="approved"', mc.JS)
        self.assertIn("PENDING · AWAITING REVIEW", mc.JS)

    def test_cortex_shows_approved_items_as_supported_and_in_capsule(self):
        w, _logs, q, _db = _mk_queue(self, PAIR_LOG, PAIR_CANDS, queue_subdir="ws/.memlet/dream")
        q.rename(q.parent / "APPROVAL-QUEUE.json")
        q = q.parent / "APPROVAL-QUEUE.json"
        md.approve_queue_items(q, ["L-OLD", "L-NEW"], "human")
        lineage = mc._load_dream_lineage({"path": str(w / "ws")})
        states = {n["id"]: (n["verdict"], n["reviewState"], n["inRecallCapsule"], n["verifiedBy"]) for n in lineage["nodes"]}
        # final round A5: the UI verifies citations against the fixed in-repo snapshot (.memlet/dream/snapshot); this
        # fixture keeps its transcripts elsewhere, so the approval is shown but its citation is not verified
        self.assertEqual(("PENDING", "pending", False, "citation not verified"), states["L-OLD"])
        self.assertEqual("unavailable_no_snapshot", lineage["subgraphCapsule"]["pinVerification"])  # UI never follows queue paths


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "chmod does not restrict root")
class F3AtomicPersistenceTests(unittest.TestCase):
    def test_failed_approval_leaves_json_and_db_unchanged(self):
        w, logs, q, db = _mk_queue(self, TWIN_LOG, TWIN_CANDS, db_name="ontology.sqlite", queue_subdir="readonly")
        self.assertTrue(md.queue_db_in_sync(q, db))
        q.parent.chmod(0o500)
        try:
            with self.assertRaises(OSError):
                md.approve_queue_items(q, ["L-1"], "human", db_path=db)
        finally:
            q.parent.chmod(0o700)
        self.assertEqual("PENDING", json.loads(q.read_text(encoding="utf-8"))["nodes"][0]["verdict"])
        self.assertEqual([("L-1", "PENDING")], _db_rows(db))
        self.assertTrue(md.queue_db_in_sync(q, db))

    def test_failed_json_replace_rolls_back_the_db(self):
        _w, _logs, q, db = _mk_queue(self, TWIN_LOG, TWIN_CANDS, db_name="out/ontology.sqlite", queue_subdir="out")
        before = q.read_bytes()
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                md.approve_queue_items(q, ["L-1"], "human")
        self.assertEqual(before, q.read_bytes())
        self.assertEqual([("L-1", "PENDING")], _db_rows(db))
        self.assertTrue(md.queue_db_in_sync(q, db))
        self.assertEqual([], [p.name for p in q.parent.iterdir() if p.name.startswith(".memlet-dream-")])

    def test_build_into_unwritable_queue_dir_does_not_create_db(self):
        w = _tmpdir(self, "p0-f3b-")
        (w / "logs").mkdir()
        (w / "logs" / "s.log").write_text(TWIN_LOG, encoding="utf-8")
        (w / "c.json").write_text(json.dumps(TWIN_CANDS), encoding="utf-8")
        qd = w / "ro"
        qd.mkdir()
        qd.chmod(0o500)
        try:
            with self.assertRaises(OSError):
                md.run_dream_build(str(w / "logs"), str(w / "c.json"), str(qd / "q.json"), str(w / "db.sqlite"))
        finally:
            qd.chmod(0o700)
        self.assertFalse((qd / "q.json").exists())
        self.assertTrue(not (w / "db.sqlite").exists() or _db_rows(w / "db.sqlite") == [])

    def test_never_disagree_claim_removed(self):
        self.assertNotIn("never disagree", MODULE_PATH.read_text(encoding="utf-8"))


class F4DbWriteAuthorityTests(unittest.TestCase):
    def test_queue_metadata_cannot_redirect_approval_into_another_db(self):
        w, logs, q, _db = _mk_queue(self, PAIR_LOG, PAIR_CANDS)
        base = json.loads(q.read_text(encoding="utf-8"))
        victim = w / "another-project.sqlite"
        md.write_ontology_sqlite(victim, base, snapshot_files=md.load_snapshot(logs))
        before = _db_rows(victim)
        q2 = w / "attacker-queue.json"
        g = copy.deepcopy(base)
        g["nodes"] = g["nodes"][:1]
        g["edges"] = copy.deepcopy(g["nodes"][0]["edges"])
        g["queueMeta"]["dbPath"] = str(victim)  # same directory as the queue, but holds another graph
        q2.write_text(json.dumps(g), encoding="utf-8")
        q2_before = q2.read_bytes()
        with self.assertRaises(ValueError) as ctx:
            md.approve_queue_items(q2, ["L-OLD"], "human")
        self.assertIn("different graph", str(ctx.exception))
        self.assertEqual(before, _db_rows(victim))
        self.assertEqual(q2_before, q2.read_bytes())

    def test_recorded_db_outside_queue_dir_or_symlinked_is_refused(self):
        w, _logs, q, _db = _mk_queue(self, TWIN_LOG, TWIN_CANDS, queue_subdir="qdir")
        outside = w / "elsewhere.sqlite"
        g = json.loads(q.read_text(encoding="utf-8"))
        g["queueMeta"]["dbPath"] = str(outside)
        md.write_ontology_sqlite(outside, g)  # same graph, but outside the queue directory
        q.write_text(json.dumps(g), encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            md.approve_queue_items(q, ["L-1"], "human")
        self.assertIn("Pass db_path explicitly", str(ctx.exception))
        self.assertEqual([("L-1", "PENDING")], _db_rows(outside))
        # The caller may still opt in explicitly.
        self.assertEqual(["L-1"], md.approve_queue_items(q, ["L-1"], "human", db_path=outside)["applied"])
        self.assertEqual([("L-1", "SUPPORTED")], _db_rows(outside))

        w2, _l2, q2, _d2 = _mk_queue(self, TWIN_LOG, TWIN_CANDS, queue_subdir="qdir")
        target = w2 / "target.sqlite"
        g2 = json.loads(q2.read_text(encoding="utf-8"))
        md.write_ontology_sqlite(target, g2)
        (q2.parent / "link.sqlite").symlink_to(target)
        g2["queueMeta"]["dbPath"] = "link.sqlite"
        q2.write_text(json.dumps(g2), encoding="utf-8")
        with self.assertRaises(ValueError):
            md.approve_queue_items(q2, ["L-1"], "human")
        with self.assertRaises(ValueError):
            md.approve_queue_items(q2, ["L-1"], "human", db_path=q2.parent / "link.sqlite")

    def test_build_refuses_to_wipe_a_db_holding_another_graph_but_rebuilds_its_own(self):
        w, logs, q, db = _mk_queue(self, TWIN_LOG, TWIN_CANDS, db_name="out/db.sqlite", queue_subdir="out")
        self.assertEqual("db.sqlite", json.loads(q.read_text(encoding="utf-8"))["queueMeta"]["dbPath"])
        md.run_dream_build(str(logs), str(w / "c.json"), str(q), str(db))  # same queue: prior generation, allowed
        other_q = w / "other" / "q.json"
        with self.assertRaises(ValueError):
            md.run_dream_build(str(logs), None, str(other_q), str(db))
        self.assertFalse(other_q.exists())
        self.assertTrue(md.queue_db_in_sync(q, db))


class F7TranscriptRoleAllowlistTests(unittest.TestCase):
    def test_hidden_channels_and_non_user_roles_are_dropped_in_every_envelope(self):
        ts = "2026-11-09T10:00:00.1234567Z"
        recs = [
            {"type": "response_item", "timestamp": ts, "payload": {"type": "message", "role": "assistant", "channel": "analysis",
                                                                   "content": [{"type": "output_text", "text": "PRIVATE_ANALYSIS_CHANNEL"}]}},
            {"type": "message", "timestamp": ts, "role": "tool", "content": "PRIVATE_LEGACY_TOOL"},
            {"type": "message", "timestamp": ts, "role": "system", "content": "PRIVATE_SYSTEM"},
            {"type": "response_item", "timestamp": ts, "payload": {"type": "message", "role": "developer",
                                                                   "content": [{"type": "input_text", "text": "PRIVATE_DEV"}]}},
            {"type": "message", "timestamp": ts, "role": "assistant", "channel": "commentary", "content": "PRIVATE_COMMENTARY"},
            {"type": "message", "timestamp": ts, "role": "assistant", "recipient": "functions.shell", "content": "PRIVATE_CALL"},
            {"type": "response_item", "timestamp": ts, "payload": {"type": "function_call", "role": "assistant", "arguments": "PRIVATE_FN"}},
            {"type": "user", "timestamp": ts, "isMeta": True, "message": {"role": "user", "content": "PRIVATE_META"}},
            {"type": "assistant", "timestamp": ts, "message": {"role": "tool", "content": "PRIVATE_ROLE_MISMATCH"}},
            {"type": "response_item", "timestamp": ts, "payload": {"type": "message", "role": "assistant", "channel": "final",
                                                                   "content": [{"type": "output_text", "text": "Completed the change"}]}},
        ]
        f = _tmpdir(self, "p0-f7-") / "t.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
        lines, stats = md.read_jsonl_transcript(f)
        self.assertNotIn("PRIVATE", "\n".join(lines))
        self.assertEqual(f"{ts} [assistant] Completed the change", lines[-1])
        self.assertEqual(1, stats["messages"])


class F8TranscriptShapesTests(unittest.TestCase):
    TS = "2026-11-09T10:00:00.1234567Z"

    def _read(self, recs):
        f = _tmpdir(self, "p0-f8-") / "t.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
        return md.read_jsonl_transcript(f)

    def test_event_messages_item_completed_and_nested_timestamp(self):
        lines, stats = self._read([
            {"type": "event_msg", "timestamp": self.TS, "payload": {"type": "user_message", "message": "User event text", "images": []}},
            {"type": "event_msg", "timestamp": self.TS, "payload": {"type": "agent_message", "message": "Assistant event text"}},
            {"type": "event_msg", "timestamp": self.TS, "payload": {"type": "agent_reasoning", "text": "PRIVATE_EVENT_REASONING"}},
            {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "CLI JSON completion text"}},
            {"type": "item.completed", "item": {"id": "item_1", "type": "reasoning", "text": "PRIVATE_ITEM_REASONING"}},
            {"type": "assistant", "message": {"type": "message", "role": "assistant", "timestamp": self.TS, "content": "Timestamp only in message"}},
        ])
        self.assertEqual([
            f"{self.TS} [user] User event text",
            f"{self.TS} [assistant] Assistant event text",
            "",
            "[assistant] CLI JSON completion text",
            "",
            f"{self.TS} [assistant] Timestamp only in message",
        ], lines)
        self.assertEqual(4, stats["messages"])

    def test_events_are_not_duplicated_when_rollout_has_response_items(self):
        lines, stats = self._read([
            {"type": "response_item", "timestamp": self.TS, "payload": {"type": "message", "role": "user",
                                                                        "content": [{"type": "input_text", "text": "Do not run git push"}]}},
            {"type": "event_msg", "timestamp": self.TS, "payload": {"type": "user_message", "message": "Do not run git push"}},
        ])
        self.assertEqual([f"{self.TS} [user] Do not run git push", ""], lines)
        self.assertEqual(1, stats["skippedDuplicateEvents"])

    def test_timestamp_less_lines_are_citable_but_cannot_order_a_supersession(self):
        snap = {"t.jsonl": ["[user] Never skip tests", "[user] old flow superseded", "[user] flow replaced by new flow"]}
        self.assertIsNotNone(md.resolve_pin(snap, "t.jsonl:1"))
        cands = md.extract_candidates_from_snapshot(snap)
        self.assertEqual(["repeated_pitfall"], [c["category"] for c in cands])
        g = md.dream_pin_and_validate([{"lesson_id": "L-S", "category": "superseded_directive", "status_kind": "superseded_rule",
                                        "title": "flow superseded", "src_spans": ["t.jsonl:2"], "superseded_by_src": "t.jsonl:3"}], snap)
        self.assertEqual([], g["nodes"])
        self.assertEqual("missing_pinned_timestamp_on_supersedes", g["dropped"][0]["reason"])


class F9SurrogateTests(unittest.TestCase):
    def test_lone_surrogate_line_is_malformed_and_build_succeeds(self):
        w = _tmpdir(self, "p0-f9-")
        p = w / "surrogate.jsonl"
        p.write_text('{"type":"user","timestamp":"2026-11-09T09:00:00Z","message":{"role":"user","content":"Never skip tests \\ud800"}}\n'
                     '{"type":"user","timestamp":"2026-11-09T09:00:01Z","message":{"role":"user","content":"Never push on Friday"}}\n',
                     encoding="utf-8")
        lines, stats = md.read_jsonl_transcript(p)
        self.assertEqual(["", "2026-11-09T09:00:01Z [user] Never push on Friday"], lines)
        self.assertEqual(1, stats["malformedLines"])
        receipt = md.run_dream_build(str(p), out_json_path=str(w / "q.json"))
        self.assertEqual(1, receipt["nodesCount"])


class F10KoreanCueTests(unittest.TestCase):
    def _cats(self, body):
        return [c["category"] for c in md.extract_candidates_from_snapshot({"s.log": ["2026-11-09T09:00:00Z [user] " + body]})]

    def test_listed_false_positives_do_not_fire(self):
        for body in ("아직 미해결 상태다", "해결되지 않았다", "해결이 안 됐다", "고객은 우회전을 했다", "절대경로를 출력했다",
                     "절대 경로를 출력했다", "금지는 해제됐다", "대체로 테스트가 통과한다"):
            self.assertEqual([], self._cats(body), body)

    def test_suffix_matching_and_curly_apostrophe_still_fire(self):
        self.assertEqual(["repeated_pitfall"], self._cats("지금 금지되어 있다"))
        self.assertEqual(["repeated_pitfall"], self._cats("절대 하지 마"))
        self.assertEqual(["verified_workaround"], self._cats("해결책을 찾았다"))
        self.assertEqual(["verified_workaround"], self._cats("우회해서 처리했다"))
        self.assertEqual(["repeated_pitfall"], self._cats("Don\u2019t skip tests"))

    def test_supersession_pairs_require_shared_scope(self):
        unrelated = ["2026-11-09T09:00:00Z [user] 대체로 테스트가 통과한다", "2026-11-09T10:00:00Z [user] 화면은 대체로 파란색이다"]
        self.assertEqual([], md.extract_candidates_from_snapshot({"s.log": unrelated}))
        unrelated_en = ["2026-11-09T09:00:00Z [user] The deploy checklist is superseded",
                        "2026-11-09T10:00:00Z [user] Logging format replaced by structured events"]
        self.assertEqual([], md.extract_candidates_from_snapshot({"s.log": unrelated_en}))
        related = ["2026-11-09T09:00:00Z [user] The 400-token handover budget is superseded",
                   "2026-11-09T10:00:00Z [user] handover budget replaced by auto sizing"]
        cats = [c["category"] for c in md.extract_candidates_from_snapshot({"s.log": related})]
        self.assertEqual(["superseded_directive", "verified_workaround"], cats)
        related_ko = ["2026-11-09T09:00:00Z [user] 기존 배포 규칙은 폐기한다", "2026-11-09T10:00:00Z [user] 새 배포 규칙으로 대체한다"]
        self.assertEqual(2, len(md.extract_candidates_from_snapshot({"s.log": related_ko})))


class F13BudgetTests(unittest.TestCase):
    def test_estimate_never_exceeds_budget(self):
        _w, logs, q, _db = _mk_queue(self, PAIR_LOG, PAIR_CANDS)
        md.approve_queue_items(q, ["L-OLD", "L-NEW"], "human")
        for budget in (0, 1, 32, 100, 1500):
            r = md.run_dream_recall(str(q), budget_tokens=budget)
            self.assertLessEqual(r["tokenEstimate"], budget, budget)
            self.assertLessEqual(md.estimate_tokens(r["formatted"]), budget, budget)
            if r.get("budgetTooSmall"):
                self.assertFalse(r["ok"])
                self.assertEqual("", r["formatted"])
                self.assertGreater(r["minimumBudget"], budget)
        self.assertTrue(md.run_dream_recall(str(q), budget_tokens=1)["budgetTooSmall"])
        self.assertEqual(["L-OLD", "L-NEW"], sorted(md.run_dream_recall(str(q), budget_tokens=1500)["includedNodeIds"], reverse=True))
        shutil.rmtree(logs)
        for budget in (0, 1, 12, 1500):
            r = md.run_dream_recall(str(q), budget_tokens=budget)
            self.assertFalse(r["ok"])
            self.assertLessEqual(r["tokenEstimate"], budget, budget)
        self.assertEqual("", md.format_flat_memory({"schema": md.DREAM_ONTOLOGY_SCHEMA, "nodes": [], "edges": []},
                                                   budget_tokens=1, allow_unverified=True))
        with self.assertRaises(ValueError):
            md.recall_subgraph({"nodes": [], "edges": []}, budget_tokens="lots")
        rc, _o, _e = _quiet_main(["dream-recall", "--graph", str(q), "--max-tokens", "1", "--allow-unverified"])
        self.assertEqual(2, rc)


class F14SpanDigitsTests(unittest.TestCase):
    def test_overlong_line_numbers_do_not_parse_on_any_python(self):
        self.assertIsNone(md.parse_span("valid.log:" + "9" * 5000))
        self.assertIsNone(md.parse_span("valid.log:1-" + "9" * 5000))
        self.assertIsNone(md.parse_span("valid.log:1234567890"))
        self.assertEqual(("valid.log", 123456789, 123456789), md.parse_span("valid.log:123456789"))


class PrivateKeyBlockTests(unittest.TestCase):
    """B2 fix round 1, finding 2: key blocks are redacted whole, line counts kept, in every snapshot form."""

    B64 = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="

    def test_unqualified_and_qualified_markers_are_masked(self):
        for kind in ("PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "DSA PRIVATE KEY", "OPENSSH PRIVATE KEY",
                     "ENCRYPTED PRIVATE KEY", "PGP PRIVATE KEY BLOCK"):
            folded = f"Never expose -----BEGIN {kind}----- {self.B64} -----END {kind}----- ok"
            out = md.mask_secrets(folded)
            self.assertNotIn(self.B64, out, kind)
            self.assertNotIn("PRIVATE KEY", out, kind)
            self.assertFalse(md.has_unmasked_secret_or_canary(out))
            block = f"a\n-----BEGIN {kind}-----\n{self.B64}\n{self.B64}\n-----END {kind}-----\nb"
            red = md.redact_secret_blocks(block)
            self.assertEqual(block.count("\n"), red.count("\n"), kind)
            self.assertEqual(["a"] + [md.SECRET_BLOCK_MARK] * 4 + ["b"], red.split("\n"))
        unterminated = md.redact_secret_blocks("-----BEGIN PRIVATE KEY-----\n" + self.B64 + "\n\nnext line")
        self.assertEqual([md.SECRET_BLOCK_MARK, md.SECRET_BLOCK_MARK, "", "next line"], unterminated.split("\n"))
        ppk = md.redact_secret_blocks("PuTTY-User-Key-File-2: ssh-rsa\nPrivate-Lines: 1\n" + self.B64
                                      + "\nPrivate-MAC: abcdef\nafter")
        self.assertEqual([md.SECRET_BLOCK_MARK] * 4 + ["after"], ppk.split("\n"))

    def test_log_and_json_snapshots_never_pin_a_key_body(self):
        tmp = Path(tempfile.mkdtemp(prefix="p0-pem-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        text = ("2026-11-09T09:00:00Z [user] Never expose -----BEGIN RSA PRIVATE KEY-----\n" + self.B64
                + "\n-----END RSA PRIVATE KEY-----\n2026-11-09T09:01:00Z [user] Do not commit keys\n")
        (tmp / "logs").mkdir()
        (tmp / "logs" / "s.log").write_text(text)
        (tmp / "map.json").write_text(json.dumps({"m.log": text.splitlines(), "s.log": text}))
        for snap in (md.load_snapshot(tmp / "logs"), md.load_snapshot(tmp / "map.json")):
            for lines in snap.values():
                self.assertEqual(4, len(lines))
                self.assertNotIn(self.B64, "\n".join(lines))
                self.assertEqual("2026-11-09T09:01:00Z [user] Do not commit keys", lines[3])
            fid = sorted(snap)[0]
            self.assertEqual(md.SECRET_BLOCK_MARK, md.resolve_pin(snap, f"{fid}:2")["quoteText"])
            self.assertNotIn(self.B64, json.dumps(md.resolve_pin(snap, f"{fid}:1-3")))


class QueueLockTests(unittest.TestCase):
    """B2 fix round 1, finding 1: one re-entrant lock per queue, shared by every spelling of its path."""

    def test_reentrant_and_path_spelling_independent(self):
        tmp = Path(tempfile.mkdtemp(prefix="p0-lock-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        q = tmp / "d" / "Q.json"
        q.parent.mkdir()
        self.assertEqual(md.queue_lock_path(q), md.queue_lock_path(tmp / "d" / ".." / "d" / "Q.json"))
        with md.queue_lock(q):
            with md.queue_lock(tmp / "d" / ".." / "d" / "Q.json", timeout=0.1):  # same thread: re-entrant
                pass
        self.assertEqual([], [p.name for p in q.parent.iterdir()])  # nothing is written next to the queue



# ---------------------------------------------------------------------------------------------------------------
# Wave 3 review R2: linear secret masking and bounded transcript reading (unit C1 fix round 1)

import time as _time  # noqa: E402

_LOCKS = {}


def setUpModule():
    _LOCKS["prior"] = os.environ.get("MEMLET_LOCK_DIR")
    _LOCKS["dir"] = tempfile.mkdtemp(prefix="p0-dream-locks-")
    os.environ["MEMLET_LOCK_DIR"] = _LOCKS["dir"]


def tearDownModule():
    if _LOCKS.get("prior") is None:
        os.environ.pop("MEMLET_LOCK_DIR", None)
    else:
        os.environ["MEMLET_LOCK_DIR"] = _LOCKS["prior"]
    shutil.rmtree(_LOCKS["dir"], ignore_errors=True)


class LinearMaskingTests(unittest.TestCase):
    ADVERSARIAL = {
        "long-plain-line": "Never call main() " + "z" * 300000,
        "dashes": "-" * 300000,
        "end-marker-prefixes": "-----END " * 40000,
        "begin-marker-prefixes": "-----BEGIN " * 40000,
        "label-run": "-----BEGIN " + "A" * 300000,
        "unclosed-begins": "-----BEGIN RSA PRIVATE KEY-----\nabc\n" * 20000,
        "jwt-like-run": "eyJ" * 100000,
        "putty-headers": "PuTTY-User-Key-File-2: x\n" * 30000,
        "aws-secret-run": "aws_secret_" * 30000,
    }

    def test_masking_is_linear_on_adversarial_lines(self):
        for label, text in self.ADVERSARIAL.items():
            with self.subTest(case=label):
                # CPU time of this process, not wall time: the bound guards against a quadratic regression and must not
                # fail because other jobs are running on the machine.
                start = _time.process_time()
                md.mask_secrets(text)
                self.assertLess(_time.process_time() - start, 1.0, label)

    def test_block_redaction_still_covers_every_key_shape(self):
        body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"
        cases = {
            "pkcs8": f"a\n-----BEGIN PRIVATE KEY-----\n{body}\n{body}\n-----END PRIVATE KEY-----\nb",
            "rsa-one-line": f"x -----BEGIN RSA PRIVATE KEY----- {body} -----END RSA PRIVATE KEY----- y",
            "openssh": f"-----BEGIN OPENSSH PRIVATE KEY-----\n{body}\n-----END OPENSSH PRIVATE KEY-----",
            "pgp": f"-----BEGIN PGP PRIVATE KEY BLOCK-----\n\n{body}\n-----END PGP PRIVATE KEY BLOCK-----",
            "truncated": f"start\n-----BEGIN EC PRIVATE KEY-----\n{body}\n{body}",
            "truncated-then-paragraph": f"-----BEGIN PRIVATE KEY-----\n{body}\n\nlater text",
            "ppk": f"PuTTY-User-Key-File-3: ssh-ed25519\nPrivate-Lines: 1\n{body}\nPrivate-MAC: abcdef\nafter",
            "end-only": f"{body}{body}-----END PRIVATE KEY-----",
            "two-blocks": (f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----\nmid\n"
                           f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"),
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                out = md.mask_secrets(text)
                self.assertNotIn(body, out)
                self.assertEqual(text.count("\n"), out.count("\n"))  # line numbers (pins) unchanged
        self.assertIn("later text", md.mask_secrets(cases["truncated-then-paragraph"]))
        self.assertIn("mid", md.mask_secrets(cases["two-blocks"]))
        self.assertTrue(md.mask_secrets(cases["ppk"]).endswith("after"))

    def test_bounded_jsonl_reader_skips_overlong_lines_and_messages_whole(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(tmp), True)
        f = tmp / "t.jsonl"
        recs = [{"type": "user", "message": {"role": "user", "content": "short rule"}},
                {"type": "user", "message": {"role": "user", "content": "Q" * 5000}},
                {"type": "user", "message": {"role": "user", "content": "tail " + "R" * 50}}]
        raw_long = json.dumps({"type": "user", "message": {"role": "user", "content": "P" * 20000}})
        f.write_text("\n".join([json.dumps(recs[0]), json.dumps(recs[1]), raw_long, json.dumps(recs[2])]) + "\n")
        lines, stats = md.read_jsonl_transcript(f, max_line_chars=10000, max_text_chars=1000)
        self.assertEqual(4, len(lines))
        self.assertIn("short rule", lines[0])
        self.assertEqual(["", ""], lines[1:3])
        self.assertIn("tail", lines[3])
        self.assertEqual((1, 1), (stats["overlongLines"], stats["overlongMessages"]))
        unbounded, _st = md.read_jsonl_transcript(f)
        self.assertIn("P" * 100, unbounded[2])  # default (other callers): unchanged behaviour

    def test_queue_lock_directory_can_be_overridden(self):
        p = md.queue_lock_path(Path(tempfile.gettempdir()) / "x" / "Q.json")
        self.assertEqual(Path(_LOCKS["dir"]).resolve(), p.parent.resolve())

if __name__ == "__main__":
    unittest.main(verbosity=2)
