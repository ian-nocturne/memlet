"""Lead integration regressions for the P0 wave: behaviour that only exists once the four units are merged."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from prism_memory import memlet_cortex
from prism_memory.viberescue import build_rescue_bundle
from test_p0_dream import _QueueFixture

CLI = Path(__file__).resolve().parent / "memory_cli.py"


class RescueEnvUnverifiedExitTests(unittest.TestCase):
    def _repo(self):
        tmp = Path(tempfile.mkdtemp(prefix="p0-int-"))
        self.addCleanup(lambda: shutil.rmtree(str(tmp), ignore_errors=True))
        repo = tmp / "backend"
        repo.mkdir()
        (repo / "AGENTS.md").write_text("# Agent rules\n\nRun `python app.py` to start.\n", encoding="utf-8")
        (repo / "app.py").write_text('import os\n\nTOKEN = os.environ["SERVICE_API_TOKEN"]\nprint("ok")\n', encoding="utf-8")
        return repo

    def test_env_unverified_exits_zero_but_is_not_reported_as_ok(self):
        res = build_rescue_bundle(self._repo())
        self.assertEqual("ENV_UNVERIFIED", res["readiness"])
        self.assertEqual(0, res["exitCode"])
        self.assertFalse(res["ok"])

    def test_cli_exit_code_matches_the_bundle(self):
        r = subprocess.run([sys.executable, "-B", str(CLI), "rescue", str(self._repo())], capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual("ENV_UNVERIFIED", doc["readiness"])
        self.assertFalse(doc["ok"])


class CortexShowsOnlyApprovedDreamTests(_QueueFixture):
    def _workspace_with_queue(self, mutate=None):
        tmp, logs, out_json, _db, _r = self._build(with_db=False)
        graph = json.loads(out_json.read_text(encoding="utf-8"))
        if mutate:
            mutate(graph)
        ws = tmp / "ws"
        (ws / ".memlet" / "dream").mkdir(parents=True)
        (ws / ".memlet" / "dream" / "APPROVAL-QUEUE.json").write_text(json.dumps(graph), encoding="utf-8")
        return ws, [n["id"] for n in graph["nodes"]]

    @staticmethod
    def _auto_promote(graph):
        for n in graph["nodes"]:
            n["verdict"] = "SUPPORTED"
            n["edgeVerdict"] = "VALID"
            for e in n.get("edges") or []:
                e["edgeVerdict"] = "VALID"
        for e in graph.get("edges") or []:
            e["edgeVerdict"] = "VALID"

    def test_auto_promoted_queue_matches_a_pending_queue_in_cortex(self):
        pending_ws, ids = self._workspace_with_queue()
        legacy_ws, _ = self._workspace_with_queue(self._auto_promote)
        self.assertTrue(ids)
        pending = memlet_cortex._load_dream_lineage({"path": str(pending_ws)})
        legacy = memlet_cortex._load_dream_lineage({"path": str(legacy_ws)})
        self.assertEqual(json.dumps(pending, sort_keys=True), json.dumps(legacy, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
