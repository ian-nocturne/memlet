"""Backward-compatibility contract for the `memlet` CLI.

Every subcommand name that existed before the six-verb router must keep parsing, and a fixed set of real
invocations must keep its exit code and output shape (top-level JSON keys, or text). The expected values live in
`testdata/cli_golden.json`; regenerate them on purpose with `MEMLET_WRITE_GOLDEN=1 python3 -m unittest test_cli_compat`
and review the diff. Values are deliberately not compared: only exit codes and shapes are part of the contract.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE / "memory_cli.py"
GOLDEN = HERE / "testdata" / "cli_golden.json"

LEGACY_COMMANDS = [
    "schema", "brief", "stats", "scenes", "doctor", "backup", "restore", "repo-benchmark", "audit", "check", "rescue",
    "viberescue", "anti-slop", "init", "wiki", "import", "slim-plan", "dream-build", "dream", "dream-recall",
    "rule-context", "rule-recall", "index-build", "brief-build", "handover", "compare-bench", "benchmark-compare",
    "opsec-check", "opsec-fingerprint", "heal-skills", "verify-skill", "preflight", "capsule", "consolidate", "sync-md",
    "benchmark", "inspector", "ui", "pack", "export", "capture", "recall", "read", "skills", "skill", "cases", "links",
    "learn", "feedback", "skill-manage", "skill-sync", "index", "backfill",
]

AGENTS_MD = """# Agent rules

- Always call `mod.py::charge` through the retry wrapper.
- Keep `api.ts::onEvent` idempotent.
- Never deploy before reading `docs/release.md`.
- Keep commits small and focused.

Run `python mod.py` to start.
"""
MOD_PY = 'import os\n\n\ndef charge(amount):\n    """Charge the card."""\n    return amount * 2\n\n\nTOKEN = os.environ.get("SERVICE_API_TOKEN")\n'
API_TS = "export function onEvent(cb: (x: number) => void, name: string) {\n  cb(1);\n  return name;\n}\n"
LOG = (
    "2026-11-09T09:00:00Z WARN: initial workaround --no-verify for git-hooks\n"
    "2026-11-09T10:00:00Z RULE: never pass --no-verify; run pytest first for git-hooks\n"
)


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True, capture_output=True, env=env)


def _shape(text):
    text = text.strip()
    if not text:
        return {"kind": "empty"}
    try:
        doc = json.loads(text)
    except ValueError:
        return {"kind": "text"}
    if isinstance(doc, dict):
        return {"kind": "json", "keys": sorted(doc)}
    return {"kind": "json-" + type(doc).__name__}


class Sandbox:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-compat-"))
        self.repo = self.tmp / "repo"
        self.root = self.tmp / "store"
        self.logs = self.tmp / "logs"
        for d in (self.repo / "docs", self.root, self.logs):
            d.mkdir(parents=True)
        (self.repo / "AGENTS.md").write_text(AGENTS_MD, encoding="utf-8")
        (self.repo / "mod.py").write_text(MOD_PY, encoding="utf-8")
        (self.repo / "api.ts").write_text(API_TS, encoding="utf-8")
        (self.repo / "docs" / "release.md").write_text("# Release\n\nCheck the gate.\n", encoding="utf-8")
        (self.logs / "s1.log").write_text(LOG, encoding="utf-8")
        _git(self.repo, "init", "-q", ".")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")

    def close(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def run(self, argv, stdin=""):
        env = {k: v for k, v in os.environ.items() if k not in ("CODEX_THREAD_ID", "PRISM_PARENT_SESSION_ID", "ANTIGRAVITY_AGENT")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run([sys.executable, "-B", str(CLI)] + argv, cwd=str(self.repo), input=stdin,
                              capture_output=True, text=True, env=env, timeout=120)

    def cases(self):
        t, q = str(self.tmp), str(self.tmp / "dream" / "APPROVAL-QUEUE.json")
        store = ["--root", str(self.root), "--standalone"]
        return [
            ("version", ["--version"]),
            ("no-command", []),
            ("unknown-command", ["no-such-command"]),
            ("schema", ["schema"]),
            ("init-dry-run", ["init", ".", "--dry-run"]),
            ("init", ["init", "."]),
            ("init-again-markdown", ["init", ".", "--format", "markdown"]),
            ("wiki", ["wiki", "."]),
            ("wiki-markdown", ["wiki", ".", "--format", "markdown"]),
            ("check", ["check", "."]),
            ("audit", ["audit", "."]),
            ("audit-markdown", ["audit", ".", "--format", "markdown"]),
            ("audit-sarif", ["audit", ".", "--format", "sarif"]),
            ("audit-exit-zero", ["audit", ".", "--exit-zero"]),
            ("import-dry-run", ["import", ".", "--dry-run"]),
            ("import", ["import", "."]),
            ("handover", ["handover", "."]),
            ("handover-markdown", ["handover", ".", "--format", "markdown"]),
            ("handover-next-step", ["handover", ".", "--next-step", "ship it", "--rejected", "rewrite in rust"]),
            ("rule-context", ["rule-context", "."]),
            ("rule-recall-query", ["rule-recall", "deploy release"]),
            ("rule-recall-legacy", ["rule-recall", ".", "deploy release"]),
            ("rule-recall-missing-query", ["rule-recall"]),
            ("slim-plan", ["slim-plan", ".", "--out", t + "/slim"]),
            ("rescue", ["rescue", "."]),
            ("viberescue-alias", ["viberescue", "."]),
            ("rescue-markdown", ["rescue", ".", "--format", "markdown"]),
            ("anti-slop", ["anti-slop", "."]),
            ("anti-slop-markdown", ["anti-slop", ".", "--format", "markdown"]),
            ("ui", ["ui", ".", "--output", t + "/ui.html"]),
            ("inspector", ["inspector", ".", "--output", t + "/inspector.html"]),
            ("pack-list", ["pack", "--list"]),
            ("benchmark", ["benchmark", "."]),
            ("compare-bench-markdown", ["compare-bench", "--format", "markdown"]),
            ("opsec-fingerprint", ["opsec-fingerprint"], "alice\nbob\n"),
            ("dream-build", ["dream-build", "--snapshot", str(self.logs), "--out-json", q]),
            ("dream-alias", ["dream", "--snapshot", str(self.logs), "--out-json", q]),
            ("dream-recall", ["dream-recall", "--graph", q, "--snapshot", str(self.logs), "--query", "git-hooks"]),
            ("dream-recall-markdown", ["dream-recall", "--graph", q, "--snapshot", str(self.logs), "--format", "markdown", "--query", "git-hooks"]),
            ("dream-recall-no-snapshot-arg", ["dream-recall", "--graph", q, "--query", "git-hooks"]),
            ("dream-recall-tiny-budget", ["dream-recall", "--graph", q, "--snapshot", str(self.logs), "--budget", "1", "--query", "git-hooks"]),
            ("recall-without-task", ["--root", str(self.root), "recall", "--query", "sqlite"]),
            ("capture", store + ["capture", "--text", "Decision: use sqlite for the ledger"]),
            ("recall", store + ["recall", "--query", "sqlite"]),
            ("brief", store + ["brief"]),
            ("stats", store + ["stats"]),
            ("scenes", store + ["scenes"]),
            ("skills", store + ["skills"]),
            ("cases", store + ["cases"]),
            ("links", store + ["links"]),
            ("index", store + ["index"]),
            ("doctor", ["--root", str(self.root), "doctor"]),
            ("preflight", store + ["preflight", "--plan", "edit mod.py", "--files", "mod.py"]),
            ("capsule", store + ["capsule", "--plan", "edit mod.py", "--files", "mod.py"]),
            ("export-dry", store + ["export"]),
            ("consolidate-dry-run", store + ["consolidate", "--dry-run"]),
            ("heal-skills-dry-run", store + ["heal-skills", "--dry-run"]),
            ("read-missing", store + ["read", "--id", "nope"]),
            ("recall-shared-prefix", store + ["recall", "--s"]),
            ("learn-file-prefix-stdin", store + ["learn", "--f", "-"], "{}"),
            ("rule-recall-safety-prefix", ["rule-recall", "deploy release", "--s"]),
            ("read-size-prefix", store + ["read", "--id", "nope", "--s", "5"]),
            ("check-format-prefix", ["check", ".", "--f", "markdown"]),
            # Unit C1 (opt-in hooks): appended last so every case above runs on an unchanged sandbox.
            ("init-hooks-dry-run", ["init", ".", "--hooks", "--dry-run"]),
            ("init-hooks", ["init", ".", "--hooks"]),
            ("init-hooks-again", ["init", ".", "--hooks"]),
            ("init-hooks-markdown", ["init", ".", "--hooks", "--format", "markdown"]),
            ("admin-hook-session-start", ["admin", "hook", "session-start"], '{"session_id": "s1"}'),
            ("admin-hook-post-tool-use-unrelated", ["admin", "hook", "post-tool-use"],
             '{"tool_name": "Edit", "tool_input": {"file_path": "docs/release.md"}}'),
            ("admin-hook-malformed-stdin", ["admin", "hook", "stop"], "not json"),
            ("admin-hook-unknown-event", ["admin", "hook", "no-such-event"]),
            ("init-hooks-remove", ["init", ".", "--hooks-remove"]),
            ("init-hooks-remove-again", ["init", ".", "--hooks-remove"]),
            ("init-git-hooks-without-hooks", ["init", ".", "--git-hooks"]),
            ("init-hooks-both-modes", ["init", ".", "--hooks", "--hooks-remove"]),
        ]


def observe():
    box = Sandbox()
    try:
        out = {"help": {}, "cases": {}}
        for name in LEGACY_COMMANDS:
            out["help"][name] = box.run([name, "--help"]).returncode
        for case in box.cases():
            label, argv = case[0], case[1]
            r = box.run(argv, case[2] if len(case) > 2 else "")
            out["cases"][label] = {"exit": r.returncode, "stdout": _shape(r.stdout), "stderr": _shape(r.stderr)}
        return out
    finally:
        box.close()


@unittest.skipUnless(shutil.which("git"), "git is required to build the fixture repository")
class CliBackwardCompatTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.seen = observe()
        if os.environ.get("MEMLET_WRITE_GOLDEN") == "1":
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(json.dumps(cls.seen, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def test_every_legacy_command_name_still_parses(self):
        self.assertEqual(sorted(LEGACY_COMMANDS), sorted(self.golden["help"]))
        for name in LEGACY_COMMANDS:
            self.assertEqual(0, self.seen["help"][name], name + " --help")

    def test_exit_codes_and_output_shapes_match_the_golden_record(self):
        self.assertEqual(sorted(self.golden["cases"]), sorted(self.seen["cases"]))
        for label, want in sorted(self.golden["cases"].items()):
            with self.subTest(case=label):
                self.assertEqual(want, self.seen["cases"][label])


if __name__ == "__main__":
    unittest.main()
