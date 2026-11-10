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


if __name__ == "__main__":
    unittest.main()
