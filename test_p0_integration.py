"""Lead integration regressions for the P0 wave: behaviour that only exists once the four units are merged."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from prism_memory.viberescue import build_rescue_bundle

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

    def test_env_unverified_does_not_mask_another_warning(self):
        # Reviewer counterexample: RLS enabled without a policy is a warning that exits 1 on its own.
        repo = self._repo()
        mig = repo / "supabase" / "migrations"
        mig.mkdir(parents=True)
        (mig / "0001_notes.sql").write_text(
            "CREATE TABLE public.notes (id int);\nALTER TABLE public.notes ENABLE ROW LEVEL SECURITY;\n", encoding="utf-8")
        res = build_rescue_bundle(repo)
        kinds = sorted(b.get("kind") for b in res["blockers"])
        self.assertIn("unverified_env_key", kinds)
        self.assertIn("supabase_rls_enabled_without_policy", kinds)
        self.assertEqual(1, res["exitCode"])
        self.assertFalse(res["ok"])
        r = subprocess.run([sys.executable, "-B", str(CLI), "rescue", str(repo)], capture_output=True, text=True)
        self.assertEqual(1, r.returncode, r.stderr)
        from prism_memory import action_verdict
        self.assertEqual(1, action_verdict.evaluate("rescue", rescue=res)["exit_code"])


if __name__ == "__main__":
    unittest.main()
