"""P0 hardening tests for `prism_memory.memlet_dream` (audit P0-6, P0-7 timestamp item, JSONL reader).

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

from prism_memory import memlet_dream as md

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
            self.assertEqual(n["primaryPin"]["sha256"], n["approval"]["pinSha256"])
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
        with self.assertRaises(ValueError):
            md.run_dream_recall(str(out_json), query_text="git-hooks")  # recorded snapshot, default verification
        rc, _o, err = _quiet_main(["dream-recall", "--graph", str(out_json)])
        self.assertEqual(1, rc)
        self.assertIn("verification failed", err)

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

    def test_hand_edited_verdict_after_approval_does_not_bypass_pin_binding(self):
        _tmp, logs, out_json = self._approved()
        graph = json.loads(out_json.read_text(encoding="utf-8"))
        graph["nodes"][0]["approval"]["pinSha256"] = "0" * 64
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
