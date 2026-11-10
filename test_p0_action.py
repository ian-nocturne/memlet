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

from memlet import action_verdict as av  # noqa: E402

ACTION_YML = ROOT / "action.yml"
TESTS_YML = ROOT / ".github" / "workflows" / "tests.yml"


def afinding(severity="error", rule="missing_target"):
    return {"ruleId": rule, "severity": severity, "message": "Target missing"}


def audit_result(findings=(), **over):
    """An audit result whose summary fields agree with `findings`; `over` then overrides single fields."""
    findings = list(findings)
    errors = sum(1 for f in findings if isinstance(f, dict) and f.get("severity") == "error")
    base = {"ok": errors == 0, "errorCount": errors, "warningCount": len(findings) - errors, "scannedDocsCount": 2,
            "findings": findings, "findingCount": len(findings), "unverifiedDynamicCommandCount": 0}
    base.update(over)
    return base


class AuditVerdictTests(unittest.TestCase):
    """Audit keeps the blocking behaviour the inline Action code had."""

    def test_error_findings_block_as_drift(self):
        v = av.evaluate("audit", audit=audit_result([afinding(), afinding(), afinding("warning")]))
        self.assertEqual((1, "DRIFT_DETECTED", 2, 3), (v["exit_code"], v["status"], v["finding_count"], v["total_finding_count"]))

    def test_fail_on_drift_false_keeps_real_audit_drift_at_exit_zero(self):
        v = av.evaluate("audit", audit=audit_result([afinding()]), fail_on_drift=False)
        self.assertEqual((0, "DRIFT_DETECTED", 1), (v["exit_code"], v["status"], len(v["blockers"])))

    def test_no_instruction_docs_blocks_as_unverified(self):
        v = av.evaluate("audit", audit=audit_result(scannedDocsCount=0))
        self.assertEqual((1, "UNVERIFIED"), (v["exit_code"], v["status"]))
        self.assertEqual(["NO_INSTRUCTION_DOCS_FOUND"], [b["code"] for b in v["blockers"]])

    def test_dynamic_commands_block_as_unverified(self):
        v = av.evaluate("audit", audit=audit_result(unverifiedDynamicCommandCount=3))
        self.assertEqual((1, "UNVERIFIED"), (v["exit_code"], v["status"]))
        self.assertIn("3 dynamic command(s)", v["blockers"][0]["message"])

    def test_warning_only_findings_do_not_block(self):
        v = av.evaluate("audit", audit=audit_result([afinding("warning"), afinding("warning")]))
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



    def test_audit_missing_fields(self):
        for field in ("ok", "errorCount", "scannedDocsCount", "findings"):
            res = audit_result()
            del res[field]
            self._assert_closed(av.evaluate("audit", audit=res), "`%s`" % field)
        self._assert_closed(av.evaluate("audit", audit=audit_result(errorCount=True)), "`errorCount`")

    def test_missing_or_non_dict_result_for_a_selected_gate(self):
        self._assert_closed(av.evaluate("audit"), "audit: result is missing")

    def test_malformed_result_fails_even_when_fail_on_drift_is_false(self):
        v = av.evaluate("audit", audit={}, fail_on_drift=False)
        self._assert_closed(v, "boolean `ok`")

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            av.evaluate("everything")


class ReviewRound1FailOpenTests(unittest.TestCase):
    """Review finding F5: every malformed or self-contradictory result is MALFORMED_RESULT / exit 2, with or
    without fail-on-drift. The first six inputs are the reviewer's, verbatim."""

    def _closed(self, mode, result, needle):
        for fail_on_drift in (True, False):
            v = av.evaluate(mode, fail_on_drift=fail_on_drift, **{mode.replace("-", "_"): result})
            self.assertEqual(2, v["exit_code"], v)
            self.assertNotEqual("PASS", v["status"])
            self.assertEqual(["MALFORMED_RESULT"], [b["code"] for b in v["blockers"]])
            self.assertEqual([], v["warnings"], "an inconsistency must be reported before any warning shortcut")
            self.assertIn(needle, v["blockers"][0]["message"])
            self.assertIn("failing closed", v["blockers"][0]["message"])

    # -- reviewer inputs


    def test_reviewer_audit_error_finding_with_zero_error_count(self):
        self._closed("audit", {"ok": True, "errorCount": 0, "scannedDocsCount": 1,
                               "findings": [{"severity": "error", "ruleId": "missing_target", "message": "Target missing"}]},
                     "`errorCount` is 0 but `findings` lists 1 error finding(s)")




    # -- the same classes of hole, one field at a time


    def test_audit_shapes_and_counts(self):
        self._closed("audit", audit_result([afinding()], ok=True), "`ok` is true but `errorCount` is 1")
        self._closed("audit", audit_result(ok=False), "`ok` is false but `errorCount` is 0")
        self._closed("audit", audit_result([afinding("warning")], errorCount=1), "`errorCount` is 1 but `findings` lists 0 error")
        self._closed("audit", audit_result([afinding("warning")], warningCount=0), "`warningCount` is 0 but `findings` lists 1 warning")
        self._closed("audit", audit_result([afinding("warning")], findingCount=9), "`findingCount` is 9")
        self._closed("audit", audit_result(findings=[{}]), "`findings[0].severity`")
        self._closed("audit", audit_result([afinding("catastrophic")]), "`findings[0].severity` is `catastrophic`")
        self._closed("audit", audit_result([{"severity": "warning"}]), "`findings[0]` has no `ruleId`")
        self._closed("audit", audit_result(findings=["oops"]), "`findings` list of objects")
        self._closed("audit", audit_result(errorCount=-1), "`errorCount` is not a non-negative integer")
        self._closed("audit", audit_result(scannedDocsCount=1.5), "`scannedDocsCount` is not a non-negative integer")
        self._closed("audit", audit_result(unverifiedDynamicCommandCount=-2), "`unverifiedDynamicCommandCount` is not a non-negative integer")


    def test_real_engine_results_are_never_classified_as_malformed(self):
        from memlet.audit import audit_repository
        empty = Path(tempfile.mkdtemp(prefix="a4-empty-"))
        self.addCleanup(lambda: shutil.rmtree(str(empty), ignore_errors=True))
        for target in (ROOT, empty):
            v = av.evaluate("audit", audit=audit_repository(str(target), output_format="json"))
            self.assertNotIn("MALFORMED_RESULT", [b["code"] for b in v["blockers"]])
            self.assertIn(v["exit_code"], (0, 1))


class FailOnDriftInputTests(unittest.TestCase):
    """Review finding F11: only `true` / `false`; a typo must not switch failures off."""

    def test_parse_accepts_only_true_false_and_empty(self):
        for raw in ("true", "TRUE", " True ", "", "   ", None):
            self.assertIs(True, av.parse_fail_on_drift(raw), repr(raw))
        for raw in ("false", "FALSE", "  False\n"):
            self.assertIs(False, av.parse_fail_on_drift(raw), repr(raw))
        for raw in ("treu", "1", "0", "yes", "no", "off", "flase", "true false"):
            with self.assertRaises(ValueError) as ctx:
                av.parse_fail_on_drift(raw)
            self.assertIn("Use 'true' or 'false'", str(ctx.exception))

    def test_pure_api_rejects_a_non_bool_flag(self):
        for bad in ("false", "treu", 0, None):
            with self.assertRaises(ValueError):
                av.evaluate("audit", audit=audit_result(), fail_on_drift=bad)


class SarifWriteTests(unittest.TestCase):
    """Review finding F12: the SARIF file is never written through a symlink."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="a4-sarif-"))
        self.addCleanup(lambda: shutil.rmtree(str(self.tmp), ignore_errors=True))
        self.victim = self.tmp / "outside-file.txt"
        self.victim.write_text("KEEP THIS FILE\n", encoding="utf-8")

    def test_symlink_destination_is_refused_and_target_untouched(self):
        link = self.tmp / "out.sarif"
        os.symlink(str(self.victim), str(link))
        with self.assertRaises(av.OutputPathError) as ctx:
            av.write_text_no_follow(str(link), "{}")
        self.assertIn("symbolic link", str(ctx.exception))
        self.assertEqual("KEEP THIS FILE\n", self.victim.read_text(encoding="utf-8"))
        self.assertTrue(link.is_symlink())
        dangling = self.tmp / "dangling.sarif"
        os.symlink(str(self.tmp / "does-not-exist-yet.txt"), str(dangling))
        with self.assertRaises(av.OutputPathError):
            av.write_text_no_follow(str(dangling), "{}")
        self.assertFalse((self.tmp / "does-not-exist-yet.txt").exists())

    def test_directory_or_missing_parent_is_refused(self):
        (self.tmp / "adir").mkdir()
        with self.assertRaises(av.OutputPathError):
            av.write_text_no_follow(str(self.tmp / "adir"), "{}")
        with self.assertRaises(av.OutputPathError):
            av.write_text_no_follow(str(self.tmp / "missing" / "out.sarif"), "{}")

    def test_regular_write_is_atomic_replace_and_leaves_no_temp_file(self):
        dest = self.tmp / "out.sarif"
        av.write_text_no_follow(str(dest), "first")
        av.write_text_no_follow(str(dest), "second \u2192")
        self.assertEqual("second \u2192", dest.read_text(encoding="utf-8"))
        self.assertEqual(["out.sarif", "outside-file.txt"], sorted(p.name for p in self.tmp.iterdir()))
        self.assertTrue(os.stat(str(dest)).st_mode & 0o400)

    def test_symlink_swapped_in_after_the_check_is_replaced_not_followed(self):
        """The write itself (temp file + os.replace) never opens the destination, so a link that appears between
        the check and the write is replaced as a directory entry."""
        dest = self.tmp / "out.sarif"
        real_check = av.check_output_path

        def racing_check(path):
            target = real_check(path)
            os.symlink(str(self.victim), str(target))
            return target

        av.check_output_path = racing_check
        self.addCleanup(lambda: setattr(av, "check_output_path", real_check))
        av.write_text_no_follow(str(dest), "{\"sarif\": true}")
        self.assertEqual("KEEP THIS FILE\n", self.victim.read_text(encoding="utf-8"))
        self.assertFalse(dest.is_symlink())
        self.assertEqual("{\"sarif\": true}", dest.read_text(encoding="utf-8"))


class RenderReportTests(unittest.TestCase):
    def setUp(self):
        self.audit = audit_result(formatted="x")
        self.verdict = av.evaluate("audit", audit=self.audit)
        self.kw = dict(audit=self.audit, audit_markdown="# Audit report",
                       audit_sarif={"version": "2.1.0", "runs": []})

    def test_markdown(self):
        text, summary = av.render_report("markdown", "audit", self.verdict, **self.kw)
        self.assertEqual(text, summary)
        for needle in ("# Audit report", "## Memlet Check verdict"):
            self.assertIn(needle, text)

    def test_json_is_one_parseable_document_without_rendered_text(self):
        text, summary = av.render_report("json", "audit", self.verdict, **self.kw)
        doc = json.loads(text)
        self.assertEqual(["tool", "mode", "format", "verdict", "audit"], list(doc))
        self.assertEqual(0, doc["verdict"]["exit_code"])
        self.assertNotIn("formatted", doc["audit"])
        self.assertTrue(summary.startswith("```json\n") and summary.endswith("\n```"))

    def test_sarif_is_the_audit_sarif_document(self):
        text, summary = av.render_report("sarif", "audit", self.verdict, **self.kw)
        self.assertEqual({"version": "2.1.0", "runs": []}, json.loads(text))
        self.assertIn("## Memlet Check verdict", summary)

    def test_sarif_without_audit_and_unknown_format_are_errors(self):
        with self.assertRaises(ValueError) as ctx:
            av.render_report("sarif", "removed", self.verdict, **self.kw)
        self.assertIn("removed in 9.0", str(ctx.exception))
        with self.assertRaises(ValueError):
            av.render_report("xml", "audit", self.verdict, **self.kw)


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
            (repo / "AGENTS.md").write_text('Call `service.py::charge` for billing.\n', encoding="utf-8")
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

    def test_backend_repo_passes_audit(self):
        tmp, repo = self._repo()
        code, out, err = self._run(self._env(tmp, repo, "audit", "markdown"))
        self.assertEqual(0, code, err)
        self.assertNotIn("::error", err)
        outputs = (tmp / "gh_output.txt").read_text(encoding="utf-8")
        self.assertIn("status=PASS\n", outputs)
        self.assertIn("finding_count=0\n", outputs)
        self.assertIn("## Memlet Check verdict", out)
        self.assertIn("## Memlet Check verdict", (tmp / "summary.md").read_text(encoding="utf-8"))
        self.assertTrue((tmp / "out.sarif").is_file())

    def test_real_critical_blocker_fails_the_run(self):
        tmp, repo = self._repo(critical=True)
        code, _out, err = self._run(self._env(tmp, repo, "audit", "markdown"))
        self.assertEqual(1, code, err)
        self.assertIn("::error title=Memlet Check::audit:", err)
        self.assertIn("status=DRIFT_DETECTED\n", (tmp / "gh_output.txt").read_text(encoding="utf-8"))
        code, _out, _err = self._run(self._env(tmp, repo, "audit", "markdown", INPUT_FAIL_ON_DRIFT="false"))
        self.assertEqual(0, code)

    def test_format_input_selects_the_report_format(self):
        tmp, repo = self._repo()
        code, out, _err = self._run(self._env(tmp, repo, "audit", "json"))
        self.assertEqual(0, code)
        doc = json.loads(out)
        self.assertEqual("json", doc["format"])
        code, out, _err = self._run(self._env(tmp, repo, "audit", "sarif"))
        self.assertEqual(0, code)
        self.assertEqual("2.1.0", json.loads(out)["version"])
        code, out, _err = self._run(self._env(tmp, repo, "audit", "markdown"))
        self.assertEqual(0, code)
        with self.assertRaises(ValueError):
            json.loads(out)

    def test_invalid_inputs_exit_two_with_a_clear_message(self):
        tmp, repo = self._repo()
        code, _out, err = self._run(self._env(tmp, repo, "audit", "xml"))
        self.assertEqual(2, code)
        self.assertIn("invalid format 'xml'", err)
        code, _out, err = self._run(self._env(tmp, repo, "everything", "markdown"))
        self.assertEqual(2, code)
        self.assertIn("invalid mode 'everything'", err)
        code, _out, err = self._run(self._env(tmp, repo, "removed", "sarif"))
        self.assertEqual(2, code)
        self.assertIn("removed in 9.0", err)

    def test_fail_on_drift_typo_is_a_configuration_error_not_a_silent_opt_out(self):
        """F11 with the reviewer's values, on a repo with a real critical blocker."""
        tmp, repo = self._repo(critical=True)
        expected = {"true": 1, "false": 0, "": 1, "  FALSE ": 0, "treu": 2, "1": 2}
        for flag, want in expected.items():
            code, out, err = self._run(self._env(tmp, repo, "audit", "json", INPUT_FAIL_ON_DRIFT=flag))
            self.assertEqual(want, code, (flag, err))
            if want == 2:
                self.assertIn("invalid fail-on-drift value '%s'" % flag, err)
                self.assertEqual("", out)
            else:
                self.assertEqual("DRIFT_DETECTED", json.loads(out)["verdict"]["status"])
        env = self._env(tmp, repo, "audit", "json")
        env.pop("INPUT_FAIL_ON_DRIFT", None)
        self.assertEqual(1, self._run(env)[0])

    def test_sarif_symlink_is_refused_for_default_and_explicit_paths(self):
        """F12 with the reviewer's setup: the default file name is a symlink in the working directory."""
        tmp, repo = self._repo()
        victim = tmp / "outside-file.txt"
        victim.write_text("KEEP THIS FILE\n", encoding="utf-8")
        workdir = tmp / "cwd"
        workdir.mkdir()
        os.symlink(str(victim), str(workdir / "memlet-check.sarif"))
        os.symlink(str(victim), str(tmp / "explicit.sarif"))
        old_cwd = os.getcwd()
        os.chdir(str(workdir))
        self.addCleanup(lambda: os.chdir(old_cwd))
        default_env = {"INPUT_MODE": "audit", "INPUT_REPO_PATH": str(repo), "INPUT_FORMAT": "json"}
        for env in (default_env, dict(default_env, INPUT_SARIF_FILE=str(tmp / "explicit.sarif"))):
            code, out, err = self._run(env)
            self.assertEqual(2, code, err)
            self.assertIn("refusing to write SARIF", err)
            self.assertIn("symbolic link", err)
            self.assertEqual("", out)
            self.assertEqual("KEEP THIS FILE\n", victim.read_text(encoding="utf-8"))
        code, _out, err = self._run(dict(default_env, INPUT_SARIF_FILE=str(tmp / "fine.sarif")))
        self.assertEqual(0, code, err)
        self.assertEqual("2.1.0", json.loads((tmp / "fine.sarif").read_text(encoding="utf-8"))["version"])
        self.assertEqual("KEEP THIS FILE\n", victim.read_text(encoding="utf-8"))

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
        env.update(self._env(tmp, repo, "audit", "json"))
        env.update({"ACTION_PATH": str(ROOT), "PATH": str(bindir) + os.pathsep + env.get("PATH", "")})
        proc = subprocess.run(["bash", "-c", script], cwd=str(tmp), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("PASS", json.loads(proc.stdout)["verdict"]["status"])
        self.assertIn("status=PASS\n", (tmp / "gh_output.txt").read_text(encoding="utf-8"))

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
        self.assertIn("-m memlet.action_verdict", self.script)
        # the verdict rules no longer live inline in the YAML
        for inline in ("has_blocker",):
            self.assertNotIn(inline, self.text)

    def test_action_passes_the_format_input_through(self):
        self.assertIn("INPUT_FORMAT: ${{ inputs.format }}", self.text)
        self.assertRegex(self.text, r"(?m)^  format:\n")

    def test_every_env_name_read_by_run_action_is_set_by_the_step(self):
        src = (ROOT / "memlet" / "action_verdict.py").read_text(encoding="utf-8")
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
        for out in ("status", "finding_count", "total_finding_count"):
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
        # review answer 7: the checkout token is not left in the workspace git config
        self.assertRegex(self.text, r"uses: actions/checkout@v4\n        with:\n          persist-credentials: false\n")
        for u in uses:
            self.assertRegex(u, r"^[\w.-]+/[\w.-]+@v\d+$")


class SlimPython39Tests(unittest.TestCase):
    """`memlet_slim` must import on 3.9 and render exactly what the pre-fix code rendered on 3.12/3.13."""

    # sha256 of the canonical JSON of the rendered files below, produced by the UNMODIFIED base-commit
    # `memlet/memlet_slim.py` (9689983) on Python 3.12 and on Python 3.13 (both gave this digest).
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
        from memlet.memlet_slim import plan, render
        lines = self.FIXTURE.split("\n")
        out = {}
        for keep, split in ((1, True), (0, True), (1, False)):
            p = plan(lines, keep_recent=keep, split_rules=split)
            self.assertTrue(p["ok"], p)
            out["k%d-%s" % (keep, "split" if split else "nosplit")] = {rel: render(lines, slots) for rel, slots in sorted(p["files"].items())}
        return out

    def test_module_compiles_and_imports_on_this_interpreter(self):
        src = (ROOT / "memlet" / "memlet_slim.py").read_text(encoding="utf-8")
        compile(src, "memlet_slim.py", "exec")
        import memlet.memlet_slim as slim
        self.assertTrue(callable(slim.slim_plan))

    def test_no_backslash_inside_fstring_expressions(self):
        # Python < 3.12 rejects a backslash inside the {...} part of an f-string. On those versions compile() above
        # already proves it; this scan keeps the proof alive on 3.12+ where the syntax became legal.
        src = (ROOT / "memlet" / "memlet_slim.py").read_text(encoding="utf-8")
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
