"""P0-7 (unit A4): GitHub Action verdicts, the `format` input, Python selection, CI matrix and Python 3.9 syntax.

Stdlib only. `action.yml` and the workflow are checked as text (no YAML dependency).
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from prism_memory import action_verdict as av  # noqa: E402

ACTION_YML = ROOT / "action.yml"
TESTS_YML = ROOT / ".github" / "workflows" / "tests.yml"


def audit_result(**over):
    base = {"ok": True, "errorCount": 0, "warningCount": 0, "scannedDocsCount": 2, "findings": [], "unverifiedDynamicCommandCount": 0}
    base.update(over)
    return base


def rescue_result(readiness, blockers=None, ok=None):
    blockers = list(blockers or [])
    return {
        "ok": (readiness == "STATIC_CHECKS_PASSED") if ok is None else ok,
        "readiness": readiness,
        "summary": {"readiness": readiness, "totalBlockers": len(blockers)},
        "blockers": blockers,
        "rescueBriefMarkdown": "# Rescue brief (%s)" % readiness,
    }


def slop_result(**over):
    base = {"ok": True, "grade": "A+", "verdict": "STATIC_STYLE_RULES_PASSED", "slopScore": 0, "totalFindingCount": 0,
            "findings": [], "markdownReport": "# Anti-slop receipt"}
    base.update(over)
    return base


NO_UI = dict(ok=False, grade="UNVERIFIED", verdict="NO_UI_OR_COPY_FILES_FOUND", slopScore=None)
ENV_KEY = {"kind": "unverified_env_key", "severity": "warning", "file": "app.py", "line": 3, "key": "SERVICE_API_TOKEN"}
CRITICAL = {"kind": "client_secret_exposure", "severity": "critical", "file": "web.tsx", "line": 1}
LOCKFILES = {"kind": "competing_lockfiles", "severity": "warning", "file": "package-lock.json", "line": 1}


class RescueVerdictTests(unittest.TestCase):
    def test_env_unverified_is_a_warning_and_exits_zero(self):
        for blockers in ([], [ENV_KEY], [ENV_KEY, dict(ENV_KEY, key="OTHER_KEY")]):
            v = av.evaluate("rescue", rescue=rescue_result("ENV_UNVERIFIED", blockers, ok=False))
            self.assertEqual(0, v["exit_code"], v)
            self.assertEqual([], v["blockers"])
            self.assertEqual(["ENV_UNVERIFIED"], [w["code"] for w in v["warnings"]])
            self.assertEqual(0, v["finding_count"])
            self.assertEqual(len(blockers), v["total_finding_count"])
            # a warning is never reported as PASS
            self.assertEqual("UNVERIFIED", v["status"])

    def test_real_blocker_exits_non_zero(self):
        v = av.evaluate("rescue", rescue=rescue_result("BLOCKED", [CRITICAL], ok=False))
        self.assertEqual(1, v["exit_code"])
        self.assertEqual("DRIFT_DETECTED", v["status"])
        self.assertEqual(1, len(v["blockers"]))
        self.assertEqual(1, v["finding_count"])

    def test_real_blocker_next_to_env_keys_still_blocks(self):
        v = av.evaluate("rescue", rescue=rescue_result("BLOCKED", [CRITICAL, ENV_KEY], ok=False))
        self.assertEqual(1, v["exit_code"])
        self.assertEqual(1, v["finding_count"])
        self.assertEqual(2, v["total_finding_count"])
        self.assertEqual(["ENV_UNVERIFIED"], [w["code"] for w in v["warnings"]])

    def test_env_unverified_does_not_hide_other_warning_severity_blockers(self):
        # ENV_UNVERIFIED takes precedence over STATIC_CHECKS_WITH_WARNINGS in the bundle; the non-env entry blocked
        # before this change and still does.
        v = av.evaluate("rescue", rescue=rescue_result("ENV_UNVERIFIED", [ENV_KEY, LOCKFILES], ok=False))
        self.assertEqual(1, v["exit_code"])
        self.assertEqual(1, v["finding_count"])

    def test_other_non_passing_readiness_still_blocks(self):
        for readiness in ("UNVERIFIED", "NO_TARGETS_FOUND", "BLOCKED", "STATIC_CHECKS_WITH_WARNINGS"):
            v = av.evaluate("rescue", rescue=rescue_result(readiness, [], ok=False))
            self.assertEqual(1, v["exit_code"], readiness)
            self.assertEqual("UNVERIFIED", v["status"], readiness)
            self.assertEqual(1, v["finding_count"], readiness)
        v = av.evaluate("rescue", rescue=rescue_result("STATIC_CHECKS_WITH_WARNINGS", [LOCKFILES], ok=False))
        self.assertEqual(1, v["exit_code"])
        self.assertEqual("DRIFT_DETECTED", v["status"])

    def test_passed_is_pass(self):
        v = av.evaluate("rescue", rescue=rescue_result("STATIC_CHECKS_PASSED"))
        self.assertEqual((0, "PASS", [], []), (v["exit_code"], v["status"], v["blockers"], v["warnings"]))

    def test_fail_on_drift_false_reports_blockers_but_exits_zero(self):
        v = av.evaluate("rescue", rescue=rescue_result("BLOCKED", [CRITICAL], ok=False), fail_on_drift=False)
        self.assertEqual(0, v["exit_code"])
        self.assertEqual(1, len(v["blockers"]))
        self.assertEqual("DRIFT_DETECTED", v["status"])


class AntiSlopVerdictTests(unittest.TestCase):
    def test_no_ui_files_is_a_warning(self):
        v = av.evaluate("anti-slop", anti_slop=slop_result(**NO_UI))
        self.assertEqual(0, v["exit_code"])
        self.assertEqual([], v["blockers"])
        self.assertEqual(["NO_UI_OR_COPY_FILES_FOUND"], [w["code"] for w in v["warnings"]])
        self.assertEqual("UNVERIFIED", v["status"])
        self.assertEqual(0, v["slop_score"])

    def test_findings_and_high_score_still_block(self):
        v = av.evaluate("anti-slop", anti_slop=slop_result(ok=False, grade="D", verdict="HEAVY_AI_SLOP", slopScore=64, totalFindingCount=7))
        self.assertEqual((1, "DRIFT_DETECTED", 7, 64), (v["exit_code"], v["status"], v["finding_count"], v["slop_score"]))
        v = av.evaluate("anti-slop", anti_slop=slop_result(ok=False, grade="A", verdict="CLEAN_MINOR_POLISH", slopScore=3, totalFindingCount=1))
        self.assertEqual(1, v["exit_code"])
        v = av.evaluate("anti-slop", anti_slop=slop_result(ok=True, grade="C", slopScore=20, totalFindingCount=0))
        self.assertEqual((1, 1), (v["exit_code"], v["finding_count"]))

    def test_clean_scan_passes(self):
        v = av.evaluate("anti-slop", anti_slop=slop_result())
        self.assertEqual((0, "PASS"), (v["exit_code"], v["status"]))


class AuditVerdictTests(unittest.TestCase):
    """Audit keeps the blocking behaviour the inline Action code had."""

    def test_error_findings_block_as_drift(self):
        v = av.evaluate("audit", audit=audit_result(ok=False, errorCount=2, findings=[{}, {}, {}]))
        self.assertEqual((1, "DRIFT_DETECTED", 2, 3), (v["exit_code"], v["status"], v["finding_count"], v["total_finding_count"]))

    def test_ok_false_without_error_count_blocks(self):
        v = av.evaluate("audit", audit=audit_result(ok=False))
        self.assertEqual((1, "DRIFT_DETECTED"), (v["exit_code"], v["status"]))

    def test_no_instruction_docs_blocks_as_unverified(self):
        v = av.evaluate("audit", audit=audit_result(scannedDocsCount=0))
        self.assertEqual((1, "UNVERIFIED"), (v["exit_code"], v["status"]))
        self.assertEqual(["NO_INSTRUCTION_DOCS_FOUND"], [b["code"] for b in v["blockers"]])

    def test_dynamic_commands_block_as_unverified(self):
        v = av.evaluate("audit", audit=audit_result(unverifiedDynamicCommandCount=3))
        self.assertEqual((1, "UNVERIFIED"), (v["exit_code"], v["status"]))
        self.assertIn("3 dynamic command(s)", v["blockers"][0]["message"])

    def test_warning_only_findings_do_not_block(self):
        v = av.evaluate("audit", audit=audit_result(warningCount=2, findings=[{"severity": "warning"}, {"severity": "warning"}]))
        self.assertEqual((0, "PASS", 0, 2), (v["exit_code"], v["status"], v["finding_count"], v["total_finding_count"]))

    def test_clean_audit_passes_and_missing_dynamic_count_reads_as_zero(self):
        res = audit_result()
        del res["unverifiedDynamicCommandCount"]
        v = av.evaluate("audit", audit=res)
        self.assertEqual((0, "PASS", []), (v["exit_code"], v["status"], v["warnings"]))


class FailClosedTests(unittest.TestCase):
    def _assert_closed(self, v, needle):
        self.assertEqual(2, v["exit_code"], v)
        self.assertNotEqual("PASS", v["status"])
        msgs = " | ".join(b["message"] for b in v["blockers"])
        self.assertIn("failing closed", msgs)
        self.assertIn(needle, msgs)
        self.assertIn("MALFORMED_RESULT", [b["code"] for b in v["blockers"]])

    def test_rescue_missing_or_unknown_readiness(self):
        self._assert_closed(av.evaluate("rescue", rescue={"ok": True, "blockers": []}), "summary.readiness")
        self._assert_closed(av.evaluate("rescue", rescue=rescue_result("CLEAN_READY", ok=True)), "unknown readiness `CLEAN_READY`")
        self._assert_closed(av.evaluate("rescue", rescue={"ok": True, "summary": {"readiness": "ENV_UNVERIFIED"}}), "`blockers` list")
        self._assert_closed(av.evaluate("rescue", rescue=rescue_result("STATIC_CHECKS_PASSED", ok=False)), "`ok` is not true")

    def test_anti_slop_missing_or_unknown_fields(self):
        self._assert_closed(av.evaluate("anti-slop", anti_slop={"ok": True, "slopScore": 0, "totalFindingCount": 0}), "`grade`")
        self._assert_closed(av.evaluate("anti-slop", anti_slop=slop_result(ok=False, grade="UNVERIFIED", verdict="SOMETHING_ELSE", slopScore=None)), "unknown verdict `SOMETHING_ELSE`")
        self._assert_closed(av.evaluate("anti-slop", anti_slop=slop_result(slopScore=None)), "`slopScore`")
        res = slop_result()
        del res["ok"]
        self._assert_closed(av.evaluate("anti-slop", anti_slop=res), "`ok`")

    def test_audit_missing_fields(self):
        for field in ("ok", "errorCount", "scannedDocsCount", "findings"):
            res = audit_result()
            del res[field]
            self._assert_closed(av.evaluate("audit", audit=res), "`%s`" % field)
        self._assert_closed(av.evaluate("audit", audit=audit_result(errorCount=True)), "`errorCount`")

    def test_missing_or_non_dict_result_for_a_selected_gate(self):
        self._assert_closed(av.evaluate("audit"), "audit: result is missing")
        self._assert_closed(av.evaluate("rescue", rescue="STATIC_CHECKS_PASSED"), "rescue: result is missing")
        self._assert_closed(av.evaluate("anti-slop", anti_slop=[]), "anti-slop: result is missing")
        v = av.evaluate("all", audit=audit_result(), rescue=rescue_result("STATIC_CHECKS_PASSED"))
        self._assert_closed(v, "anti-slop: result is missing")

    def test_malformed_result_fails_even_when_fail_on_drift_is_false(self):
        v = av.evaluate("rescue", rescue={}, fail_on_drift=False)
        self._assert_closed(v, "summary.readiness")

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            av.evaluate("everything")


class CombinedModeTests(unittest.TestCase):
    def test_backend_repo_in_mode_all_passes_with_warnings(self):
        v = av.evaluate("all", audit=audit_result(), rescue=rescue_result("ENV_UNVERIFIED", [ENV_KEY], ok=False), anti_slop=slop_result(**NO_UI))
        self.assertEqual(0, v["exit_code"])
        self.assertEqual("UNVERIFIED", v["status"])
        self.assertEqual(["rescue", "anti-slop"], [w["gate"] for w in v["warnings"]])
        self.assertTrue({"blockers", "warnings", "exit_code"} <= set(v), sorted(v))

    def test_drift_in_one_gate_wins_over_warnings_in_others(self):
        v = av.evaluate("all", audit=audit_result(ok=False, errorCount=1, findings=[{}]),
                        rescue=rescue_result("ENV_UNVERIFIED", [ENV_KEY], ok=False), anti_slop=slop_result(**NO_UI))
        self.assertEqual((1, "DRIFT_DETECTED", 1), (v["exit_code"], v["status"], v["finding_count"]))
        self.assertEqual(2, len(v["warnings"]))


class RenderReportTests(unittest.TestCase):
    def setUp(self):
        self.audit = audit_result(formatted="x")
        self.rescue = rescue_result("ENV_UNVERIFIED", [ENV_KEY], ok=False)
        self.slop = slop_result(**NO_UI)
        self.verdict = av.evaluate("all", audit=self.audit, rescue=self.rescue, anti_slop=self.slop)
        self.kw = dict(audit=self.audit, rescue=self.rescue, anti_slop=self.slop, audit_markdown="# Audit report",
                       audit_sarif={"version": "2.1.0", "runs": []})

    def test_markdown(self):
        text, summary = av.render_report("markdown", "all", self.verdict, **self.kw)
        self.assertEqual(text, summary)
        for needle in ("# Audit report", "# Rescue brief (ENV_UNVERIFIED)", "# Anti-slop receipt", "## Memlet Check verdict", "**Warning** (`rescue`)"):
            self.assertIn(needle, text)

    def test_json_is_one_parseable_document_without_rendered_text(self):
        text, summary = av.render_report("json", "all", self.verdict, **self.kw)
        doc = json.loads(text)
        self.assertEqual(["tool", "mode", "format", "verdict", "audit", "rescue", "antiSlop"], list(doc))
        self.assertEqual(0, doc["verdict"]["exit_code"])
        self.assertNotIn("rescueBriefMarkdown", doc["rescue"])
        self.assertNotIn("markdownReport", doc["antiSlop"])
        self.assertNotIn("formatted", doc["audit"])
        self.assertTrue(summary.startswith("```json\n") and summary.endswith("\n```"))
        only_rescue = json.loads(av.render_report("json", "rescue", self.verdict, **self.kw)[0])
        self.assertNotIn("audit", only_rescue)
        self.assertNotIn("antiSlop", only_rescue)

    def test_sarif_is_the_audit_sarif_document(self):
        text, summary = av.render_report("sarif", "all", self.verdict, **self.kw)
        self.assertEqual({"version": "2.1.0", "runs": []}, json.loads(text))
        self.assertIn("# Rescue brief (ENV_UNVERIFIED)", summary)
        self.assertIn("## Memlet Check verdict", summary)

    def test_sarif_without_audit_and_unknown_format_are_errors(self):
        with self.assertRaises(ValueError) as ctx:
            av.render_report("sarif", "rescue", self.verdict, **self.kw)
        self.assertIn("needs the audit gate", str(ctx.exception))
        with self.assertRaises(ValueError):
            av.render_report("xml", "all", self.verdict, **self.kw)


def _run_git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t"] + list(args), cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class RunActionEndToEndTests(unittest.TestCase):
    """Real gates on throwaway repositories (no network)."""

    def _repo(self, critical=False):
        tmp = Path(tempfile.mkdtemp(prefix="a4-action-"))
        self.addCleanup(lambda: shutil.rmtree(str(tmp), ignore_errors=True))
        repo = tmp / "backend"
        repo.mkdir()
        (repo / "AGENTS.md").write_text("# Agent rules\n\nRun `python app.py` to start.\n", encoding="utf-8")
        (repo / "app.py").write_text('import os\n\nTOKEN = os.environ["SERVICE_API_TOKEN"]\nprint("ok")\n', encoding="utf-8")
        if critical:
            (repo / "web.py").write_text('import os\n\nKEY = os.environ["NEXT_PUBLIC_STRIPE_SECRET_KEY"]\n', encoding="utf-8")
        if shutil.which("git"):
            _run_git(repo, "init", "-q", ".")
            _run_git(repo, "add", "-A")
            _run_git(repo, "commit", "-q", "-m", "init")
        return tmp, repo

    def _env(self, tmp, repo, mode, fmt, **extra):
        env = {"INPUT_MODE": mode, "INPUT_FORMAT": fmt, "INPUT_REPO_PATH": str(repo), "INPUT_SARIF_FILE": str(tmp / "out.sarif"),
               "GITHUB_OUTPUT": str(tmp / "gh_output.txt"), "GITHUB_STEP_SUMMARY": str(tmp / "summary.md")}
        env.update(extra)
        return env

    def _run(self, env):
        out, err = io.StringIO(), io.StringIO()
        code = av.run_action(env=env, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_backend_repo_without_env_file_or_ui_passes_mode_all_with_warnings(self):
        tmp, repo = self._repo()
        code, out, err = self._run(self._env(tmp, repo, "all", "markdown"))
        self.assertEqual(0, code, err)
        self.assertIn("::warning title=Memlet Check::rescue: ENV_UNVERIFIED", err)
        self.assertIn("::warning title=Memlet Check::anti-slop: NO_UI_OR_COPY_FILES_FOUND", err)
        self.assertNotIn("::error", err)
        outputs = (tmp / "gh_output.txt").read_text(encoding="utf-8")
        self.assertIn("status=UNVERIFIED\n", outputs)
        self.assertIn("finding_count=0\n", outputs)
        self.assertIn("## Memlet Check verdict", out)
        self.assertIn("## Memlet Check verdict", (tmp / "summary.md").read_text(encoding="utf-8"))
        self.assertTrue((tmp / "out.sarif").is_file())

    def test_real_critical_blocker_fails_the_run(self):
        tmp, repo = self._repo(critical=True)
        code, _out, err = self._run(self._env(tmp, repo, "rescue", "markdown"))
        self.assertEqual(1, code, err)
        self.assertIn("::error title=Memlet Check::rescue:", err)
        self.assertIn("status=DRIFT_DETECTED\n", (tmp / "gh_output.txt").read_text(encoding="utf-8"))
        code, _out, _err = self._run(self._env(tmp, repo, "rescue", "markdown", INPUT_FAIL_ON_DRIFT="false"))
        self.assertEqual(0, code)

    def test_format_input_selects_the_report_format(self):
        tmp, repo = self._repo()
        code, out, _err = self._run(self._env(tmp, repo, "all", "json"))
        self.assertEqual(0, code)
        doc = json.loads(out)
        self.assertEqual("json", doc["format"])
        self.assertEqual("ENV_UNVERIFIED", doc["rescue"]["summary"]["readiness"])
        self.assertEqual("NO_UI_OR_COPY_FILES_FOUND", doc["antiSlop"]["verdict"])
        code, out, _err = self._run(self._env(tmp, repo, "all", "sarif"))
        self.assertEqual(0, code)
        self.assertEqual("2.1.0", json.loads(out)["version"])
        code, out, _err = self._run(self._env(tmp, repo, "audit", "markdown"))
        self.assertEqual(0, code)
        with self.assertRaises(ValueError):
            json.loads(out)

    def test_invalid_inputs_exit_two_with_a_clear_message(self):
        tmp, repo = self._repo()
        code, _out, err = self._run(self._env(tmp, repo, "all", "xml"))
        self.assertEqual(2, code)
        self.assertIn("invalid format 'xml'", err)
        code, _out, err = self._run(self._env(tmp, repo, "everything", "markdown"))
        self.assertEqual(2, code)
        self.assertIn("invalid mode 'everything'", err)
        code, _out, err = self._run(self._env(tmp, repo, "rescue", "sarif"))
        self.assertEqual(2, code)
        self.assertIn("format 'sarif' needs mode 'audit' or 'all'", err)

    @unittest.skipUnless(shutil.which("bash"), "bash is required to execute the composite step script")
    def test_action_yml_run_script_executes_end_to_end(self):
        """Run the exact `run:` script from action.yml with the interpreter under test first on PATH."""
        script = _action_run_script()
        tmp, repo = self._repo()
        bindir = tmp / "bin"
        bindir.mkdir()
        os.symlink(sys.executable, str(bindir / "python3"))
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.update(self._env(tmp, repo, "all", "json"))
        env.update({"ACTION_PATH": str(ROOT), "PATH": str(bindir) + os.pathsep + env.get("PATH", "")})
        proc = subprocess.run(["bash", "-c", script], cwd=str(tmp), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("UNVERIFIED", json.loads(proc.stdout)["verdict"]["status"])
        self.assertIn("status=UNVERIFIED\n", (tmp / "gh_output.txt").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("bash"), "bash is required to execute the composite step script")
    def test_action_yml_run_script_fails_clearly_without_a_usable_python(self):
        script = _action_run_script()
        tmp = Path(tempfile.mkdtemp(prefix="a4-nopy-"))
        self.addCleanup(lambda: shutil.rmtree(str(tmp), ignore_errors=True))
        bindir = tmp / "bin"
        bindir.mkdir()
        for name in ("python3", "python"):  # stand-ins for an interpreter older than 3.9
            stub = bindir / name
            stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            stub.chmod(0o755)
        env = {"PATH": str(bindir), "ACTION_PATH": str(ROOT)}
        proc = subprocess.run([shutil.which("bash"), "-c", script], cwd=str(tmp), env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(2, proc.returncode, proc.stderr)
        self.assertIn("No Python >= 3.9 found", proc.stdout)
        self.assertIn("python-version", proc.stdout)


def _action_run_script():
    """The `run: |` block of the gate step, dedented. Text-based on purpose (stdlib has no YAML parser)."""
    lines = ACTION_YML.read_text(encoding="utf-8").split("\n")
    start = next(i for i, l in enumerate(lines) if l.strip() == "run: |")
    indent = len(lines[start]) - len(lines[start].lstrip()) + 2
    body = []
    for l in lines[start + 1:]:
        if l.strip() and len(l) - len(l.lstrip()) < indent:
            break
        body.append(l[indent:])
    return "\n".join(body)


class ActionYmlWiringTests(unittest.TestCase):
    def setUp(self):
        self.text = ACTION_YML.read_text(encoding="utf-8")
        self.script = _action_run_script()

    def test_action_calls_the_verdict_module(self):
        self.assertIn("-m prism_memory.action_verdict", self.script)
        # the verdict rules no longer live inline in the YAML
        for inline in ("STATIC_CHECKS_PASSED", "has_blocker", "build_rescue_bundle", "scan_anti_slop"):
            self.assertNotIn(inline, self.text)

    def test_action_passes_the_format_input_through(self):
        self.assertIn("INPUT_FORMAT: ${{ inputs.format }}", self.text)
        self.assertRegex(self.text, r"(?m)^  format:\n")

    def test_every_env_name_read_by_run_action_is_set_by_the_step(self):
        src = (ROOT / "prism_memory" / "action_verdict.py").read_text(encoding="utf-8")
        names = set(re.findall(r'env\.get\("(INPUT_[A-Z_]+)"', src))
        self.assertEqual({"INPUT_MODE", "INPUT_FORMAT", "INPUT_REPO_PATH", "INPUT_BASE_REF", "INPUT_HEAD_REF",
                          "INPUT_FAIL_ON_DRIFT", "INPUT_SARIF_FILE"}, names)
        for name in names:
            self.assertRegex(self.text, r"(?m)^        %s: \$\{\{ inputs\.[a-z-]+ \}\}$" % name)

    def test_setup_python_runs_only_when_python_version_is_set(self):
        self.assertRegex(self.text, r"(?m)^  python-version:\n    description: .*\n    required: false\n    default: \"\"\n")
        m = re.search(r"(?m)^    - name: [^\n]*\n      if: \$\{\{ inputs\.python-version != '' \}\}\n      uses: actions/setup-python@v\d+\n"
                      r"      with:\n        python-version: \$\{\{ inputs\.python-version \}\}\n", self.text)
        self.assertIsNotNone(m, "setup-python must be conditional on the python-version input")
        self.assertEqual(1, self.text.count("actions/setup-python@"))
        self.assertNotRegex(self.text, r"python-version:\s*\"?3\.\d+")
        self.assertIn("sys.version_info >= (3, 9)", self.script)

    def test_outputs_are_unchanged(self):
        for out in ("status", "finding_count", "total_finding_count", "slop_score"):
            self.assertIn("steps.run-gate.outputs.%s }}" % out, self.text)


class TestsWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = TESTS_YML.read_text(encoding="utf-8")

    def test_matrix_covers_python_39_to_313_on_push_and_pull_request(self):
        self.assertIn('python-version: ["3.9", "3.10", "3.11", "3.12", "3.13"]', self.text)
        self.assertRegex(self.text, r"(?m)^on:\n  push:\n  pull_request:\n")
        self.assertIn("python -m unittest", self.text)
        self.assertIn('"test_*.py"', self.text)

    def test_read_only_permissions_no_secrets_and_major_version_pins(self):
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read\n\n")
        self.assertNotIn("write", self.text)
        self.assertNotIn("secrets.", self.text)
        uses = re.findall(r"uses:\s*(\S+)", self.text)
        self.assertEqual(["actions/checkout@v4", "actions/setup-python@v5"], uses)
        for u in uses:
            self.assertRegex(u, r"^[\w.-]+/[\w.-]+@v\d+$")


class SlimPython39Tests(unittest.TestCase):
    """`memlet_slim` must import on 3.9 and render exactly what the pre-fix code rendered on 3.12/3.13."""

    # sha256 of the canonical JSON of the rendered files below, produced by the UNMODIFIED base-commit
    # `prism_memory/memlet_slim.py` (9689983) on Python 3.12 and on Python 3.13 (both gave this digest).
    GOLDEN_SHA256 = "0c280bcec264267685e99f23029afdfe386a383639e987a2caf1da9fdc701ea5"
    FIXTURE = (
        "> **2026-10-30 Directive A**\n> never push without approval.\n\n"
        "> **2026-10-29 Directive B `code` <tag>**\n> body B.\n\n"
        "> **2026-10-28 Directive C**\n> body C.\n\n"
        "# Rules\n\n- Keep commits small.\n\n"
        "##   Money `budget`  <b>\n\n- No paid API.\n\n"
        "###\tTabbed\\heading\n- item\n"
        "###### Deep # hash ## inside\n\n- last\n"
    )

    def _rendered(self):
        from prism_memory.memlet_slim import plan, render
        lines = self.FIXTURE.split("\n")
        out = {}
        for keep, split in ((1, True), (0, True), (1, False)):
            p = plan(lines, keep_recent=keep, split_rules=split)
            self.assertTrue(p["ok"], p)
            out["k%d-%s" % (keep, "split" if split else "nosplit")] = {rel: render(lines, slots) for rel, slots in sorted(p["files"].items())}
        return out

    def test_module_compiles_and_imports_on_this_interpreter(self):
        src = (ROOT / "prism_memory" / "memlet_slim.py").read_text(encoding="utf-8")
        compile(src, "memlet_slim.py", "exec")
        import prism_memory.memlet_slim as slim
        self.assertTrue(callable(slim.slim_plan))

    def test_no_backslash_inside_fstring_expressions(self):
        # Python < 3.12 rejects a backslash inside the {...} part of an f-string. On those versions compile() above
        # already proves it; this scan keeps the proof alive on 3.12+ where the syntax became legal.
        src = (ROOT / "prism_memory" / "memlet_slim.py").read_text(encoding="utf-8")
        offenders = []
        for n, line in enumerate(src.split("\n"), 1):
            for m in re.finditer(r"""\bf(?P<q>["'])(?P<body>.*?)(?<!\\)(?P=q)""", line):
                for expr in re.findall(r"(?<!\{)\{([^{}]*)\}(?!\})", m.group("body")):
                    if "\\" in expr:
                        offenders.append((n, expr))
        self.assertEqual([], offenders)

    def test_rules_index_output_is_byte_identical_to_the_pre_fix_output(self):
        out = self._rendered()
        blob = json.dumps(out, ensure_ascii=False, sort_keys=True, indent=1)
        self.assertEqual(self.GOLDEN_SHA256, hashlib.sha256(blob.encode("utf-8")).hexdigest())
        # the lines the changed statement produces, spelled out (heading markers and any whitespace run stripped)
        index = [l for l in out["k1-split"]["AGENTS.md"] if "management/agent-rules.md:" in l]
        self.assertEqual([
            "- Rules → `management/agent-rules.md:3-5`",
            "- Money  budget   b → `management/agent-rules.md:7-9`",
            "- Tabbed\\heading → `management/agent-rules.md:11-12`",
            "- Deep # hash ## inside → `management/agent-rules.md:13-15`",
        ], index)


if __name__ == "__main__":
    unittest.main()
