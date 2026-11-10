"""Unit C1: `memlet init --hooks` (installer) and the hidden runtime `memlet admin hook <event>`.

Every test works in throwaway directories under the system temp dir (HOME is redirected too); nothing touches a real
~/.claude or project settings. Runtime tests pipe synthetic Claude Code payloads through the real CLI entry point.
Stdlib unittest only; no network, no model call.
"""

import contextlib
import hashlib
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import memory_cli
from prism_memory import memlet_bridge as mb
from prism_memory import memlet_hooks as mh
from prism_memory.memlet_wiki import estimate_tokens

HERE = Path(__file__).resolve().parent
CLI = HERE / "memory_cli.py"
HAS_GIT = shutil.which("git") is not None
LAUNCHER_ENV = ("PYTHONHOME", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__")

AGENTS = ("# Rules\n\n"
          "- Always call `mod.py::charge` through the retry wrapper.\n"
          "- Keep `util.py::helper` pure.\n")
MOD = "def charge(amount):\n    return amount * 2\n"
MOD_STALE = "def charge(amount, currency):\n    return amount * 2\n"
UTIL = "def helper(x):\n    return x\n"
FOREIGN = ('{\n    "permissions": {"allow": ["Bash(ls)"]},\n'
           '    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo foreign-stop"}]}],\n'
           '              "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo foreign-bash"}]}]},\n'
           '    "model": "opus"\n}\n')
TRANSCRIPT = [
    {"type": "summary", "summary": "session", "leafUuid": "x"},
    {"type": "user", "timestamp": "2026-11-09T09:00:00Z",
     "message": {"role": "user", "content": "Never call charge() in mod.py from the test suite"}},
    {"type": "assistant", "timestamp": "2026-11-09T09:00:05Z",
     "message": {"role": "assistant", "type": "message", "content": [
         {"type": "thinking", "thinking": "hidden REASONING-XYZ"},
         {"type": "text", "text": "Understood."},
         {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cat TOOLINPUT-XYZ"}}]}},
    {"type": "user", "timestamp": "2026-11-09T09:00:06Z",
     "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                               "content": "TOOLPAYLOAD-XYZ never leak this"}]}},
    {"type": "user", "timestamp": "2026-11-09T09:02:00Z",
     "message": {"role": "user", "content": "Do not push to the release branch without approval"}},
    {"type": "assistant", "timestamp": "2026-11-09T09:03:00Z",
     "message": {"role": "assistant", "content": [{"type": "text", "text": "Workaround: run the migration with a dry run first"}]}},
]


def _working_python():
    """An interpreter that runs without this process's launcher variables (what Claude Code / git would see)."""
    env = {k: v for k, v in os.environ.items() if k not in LAUNCHER_ENV}
    for cand in (sys.executable, shutil.which("python3"), "/usr/bin/python3"):
        if not cand or not os.path.isabs(cand):
            continue
        try:
            r = subprocess.run([cand, "-c", "import sys; print(sys.version_info[:2] >= (3, 9))"], env=env,
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout.strip() == "True":
            return cand
    return None


PYTHON = _working_python()


def _tree(root):
    """Every path below root (symlinks not followed) with its bytes: proves 'nothing written'."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            if p.is_symlink():
                out[rel] = ("link", os.readlink(str(p)))
            elif p.is_file():
                out[rel] = ("file", p.read_bytes())
            else:
                out[rel] = ("dir", None)
    return out


def _git_env(home):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               HOME=str(home))
    for k in ("CODEX_THREAD_ID", "PRISM_PARENT_SESSION_ID", "ANTIGRAVITY_AGENT"):
        env.pop(k, None)
    return env


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p1-hooks-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "AGENTS.md").write_text(AGENTS)
        (self.repo / "mod.py").write_text(MOD)
        (self.repo / "util.py").write_text(UTIL)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.path = str(self.bin) + os.pathsep + "/usr/bin:/bin"

    # -- helpers -------------------------------------------------------------------------------------------------
    def env(self, **extra):
        env = _git_env(self.home)
        env["PATH"] = self.path
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(extra)
        return env

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(["git"] + list(args), cwd=str(cwd or self.repo), env=self.env(), capture_output=True,
                              text=True, check=check, timeout=60)

    def git_init(self):
        self.git("init", "-q", ".")
        (self.repo / ".gitignore").write_text(".memlet/\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

    def cli(self, *argv, stdin="", cwd=None, env=None):
        r = subprocess.run([sys.executable, "-B", str(CLI)] + list(argv), cwd=str(cwd or self.repo), input=stdin,
                           capture_output=True, text=True, env=env or self.env(), timeout=120)
        return r

    def install(self, *extra):
        r = self.cli("init", ".", "--hooks", *extra)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def settings(self, scope="local"):
        return self.repo / mh.SCOPES[scope]

    def hook(self, event, payload, cwd=None, raw=None, env=None):
        stdin = raw if raw is not None else json.dumps(payload)
        r = self.cli("admin", "hook", event, stdin=stdin, cwd=cwd, env=env)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual("", r.stderr)
        out = r.stdout.strip()
        return json.loads(out) if out else None

    def fake_memlet(self, body=None):
        p = self.bin / "memlet"
        p.write_text(body or "#!/bin/sh\nexec {} {} \"$@\"\n".format(shlex.quote(PYTHON), shlex.quote(str(CLI))))
        p.chmod(0o755)
        return p

    def write_transcript(self, name="cc-session.jsonl", recs=TRANSCRIPT):
        d = self.tmp / "transcripts"
        d.mkdir(exist_ok=True)
        p = d / name
        p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
        return p


def _commands(doc):
    return [h.get("command") for ev in (doc.get("hooks") or {}).values() for g in ev for h in g.get("hooks", [])]


@unittest.skipUnless(PYTHON, "no interpreter runs outside this process's launcher environment")
class InstallerTests(_Case):
    def test_plain_init_installs_nothing_and_has_no_hooks_key(self):
        r = self.cli("init", ".")
        self.assertEqual(0, r.returncode, r.stderr)
        rep = json.loads(r.stdout)
        self.assertNotIn("hooks", rep)
        self.assertFalse((self.repo / ".claude").exists())
        self.assertNotIn("hooks", json.loads((self.repo / ".memlet" / "config.json").read_text()))
        self.assertNotIn("--hooks", r.stdout)

    @unittest.skipUnless(HAS_GIT, "git not installed")
    def test_dry_run_writes_nothing_and_prints_the_planned_change(self):
        self.git_init()
        (self.repo / ".claude").mkdir()
        self.settings().write_text(FOREIGN)
        before = _tree(self.repo)
        hooks_before = sorted(os.listdir(str(self.repo / ".git" / "hooks")))
        r = self.cli("init", ".", "--hooks", "--dry-run", "--git-hooks")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual(before, _tree(self.repo))
        rep = json.loads(r.stdout)
        self.assertTrue(rep["dryRun"])
        self.assertEqual([{"op": "would-backup", "path": ".claude/settings.local.json.bak-<UTC timestamp>"},
                          {"op": "would-update", "path": ".claude/settings.local.json"}], rep["hooks"]["actions"][:2])
        self.assertEqual(["would-create", "would-create"], [a["op"] for a in rep["hooks"]["actions"][2:]])
        self.assertEqual(hooks_before, sorted(os.listdir(str(self.repo / ".git" / "hooks"))))
        self.assertIn("+", rep["hooks"]["diff"])
        self.assertIn("admin hook session-start", rep["hooks"]["diff"])
        md = self.cli("init", ".", "--hooks", "--dry-run", "--format", "markdown")
        self.assertIn("Memlet hooks (dry run)", md.stdout)
        self.assertIn("```diff", md.stdout)
        self.assertEqual(before, _tree(self.repo))

    def test_install_into_missing_file_creates_it_with_the_exact_shape(self):
        self.git_init()
        rep = self.install()
        self.assertEqual("create", rep["hooks"]["claude"]["op"])
        doc = json.loads(self.settings().read_text())
        self.assertEqual(["hooks"], list(doc))
        self.assertEqual(["SessionStart", "PostToolUse", "Stop", "PreCompact"], list(doc["hooks"]))
        want = {"SessionStart": ("session-start", 10, None), "PostToolUse": ("post-tool-use", 10, "Write|Edit"),
                "Stop": ("stop", 20, None), "PreCompact": ("pre-compact", 20, None)}
        for event, (cli_event, timeout, matcher) in want.items():
            groups = doc["hooks"][event]
            self.assertEqual(1, len(groups))
            g = groups[0]
            self.assertEqual(["matcher", "hooks"] if matcher else ["hooks"], list(g))
            if matcher:
                self.assertEqual(matcher, g["matcher"])
            self.assertEqual(1, len(g["hooks"]))
            h = g["hooks"][0]
            self.assertEqual(["type", "command", "timeout"], list(h))
            self.assertEqual(("command", timeout), (h["type"], h["timeout"]))
            self.assertIn(" admin hook " + cli_event, h["command"])
            self.assertEqual(cli_event, mh.memlet_hook_event(h))
        self.assertTrue(any("not git-ignored" in n or "ignore rules do not cover" in n for n in rep["hooks"]["notices"]))
        self.assertEqual("absolute", rep["hooks"]["hookCommand"]["form"])
        cfg_text = (self.repo / ".memlet" / "config.json").read_text()
        self.assertNotIn(str(HERE), cfg_text)  # the record never stores this machine's paths
        self.assertEqual("absolute", json.loads(cfg_text)["hooks"]["installed"]["claude"]["local"]["commandForm"])
        self.assertEqual([], list(self.settings().parent.glob("*.bak-*")))  # nothing to back up

    def test_install_into_empty_object_and_remove_restores_its_bytes(self):
        (self.repo / ".claude").mkdir()
        original = b"{ }"
        self.settings().write_bytes(original)
        self.install()
        self.assertEqual(4, len(_commands(json.loads(self.settings().read_text()))))
        r = self.cli("init", ".", "--hooks-remove")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual(original, self.settings().read_bytes())

    def test_foreign_hooks_and_keys_are_preserved_in_order_then_restored_byte_for_byte(self):
        (self.repo / ".claude").mkdir()
        self.settings().write_text(FOREIGN)
        original = self.settings().read_bytes()
        self.install()
        doc = json.loads(self.settings().read_text())
        self.assertEqual(["permissions", "hooks", "model"], list(doc))
        self.assertEqual({"allow": ["Bash(ls)"]}, doc["permissions"])
        self.assertEqual("opus", doc["model"])
        self.assertEqual(["Stop", "PostToolUse", "SessionStart", "PreCompact"], list(doc["hooks"]))
        self.assertEqual("echo foreign-stop", doc["hooks"]["Stop"][0]["hooks"][0]["command"])
        self.assertEqual("echo foreign-bash", doc["hooks"]["PostToolUse"][0]["hooks"][0]["command"])
        self.assertEqual("Bash", doc["hooks"]["PostToolUse"][0]["matcher"])
        self.assertEqual("stop", mh.memlet_hook_event(doc["hooks"]["Stop"][1]["hooks"][0]))
        # second run: byte-identical settings and config, no new backup
        cfg = (self.repo / ".memlet" / "config.json").read_bytes()
        installed = self.settings().read_bytes()
        rep = self.install()
        self.assertEqual(installed, self.settings().read_bytes())
        self.assertEqual(cfg, (self.repo / ".memlet" / "config.json").read_bytes())
        self.assertEqual([{"op": "unchanged", "path": ".claude/settings.local.json"}], rep["hooks"]["actions"])
        backups = list(self.settings().parent.glob("settings.local.json.bak-*"))
        self.assertEqual(1, len(backups))
        self.assertEqual(original, backups[0].read_bytes())
        r = self.cli("init", ".", "--hooks-remove")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual(original, self.settings().read_bytes())
        self.assertNotIn("hooks", json.loads((self.repo / ".memlet" / "config.json").read_text()))
        again = self.cli("init", ".", "--hooks-remove")
        self.assertEqual(0, again.returncode)
        self.assertEqual(original, self.settings().read_bytes())

    def test_remove_after_a_user_edit_keeps_the_edit_and_foreign_hooks(self):
        (self.repo / ".claude").mkdir()
        self.settings().write_text(FOREIGN)
        self.install()
        doc = json.loads(self.settings().read_text())
        doc["env"] = {"A": "1"}
        doc["hooks"]["Stop"][1]["hooks"].append({"type": "command", "command": "echo user-added"})
        self.settings().write_text(json.dumps(doc, indent=4))
        r = self.cli("init", ".", "--hooks-remove")
        self.assertEqual(0, r.returncode, r.stderr)
        after = json.loads(self.settings().read_text())
        self.assertEqual({"A": "1"}, after["env"])
        self.assertEqual(["echo foreign-stop", "echo user-added", "echo foreign-bash"], _commands(after))
        self.assertEqual(["Stop", "PostToolUse"], list(after["hooks"]))
        self.assertEqual(2, len(list(self.settings().parent.glob("*.bak-*"))))  # the user's edit was backed up

    def test_remove_of_a_file_memlet_created_deletes_it_and_the_directory(self):
        self.install()
        self.assertTrue(self.settings().is_file())
        r = self.cli("init", ".", "--hooks-remove")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertFalse((self.repo / ".claude").exists())

    def test_malformed_or_unsupported_settings_are_refused_without_any_change(self):
        cases = {
            "syntax": b'{"hooks": {', "trailing-comma": b'{"a": 1,}', "array": b"[]", "string": b'"x"',
            "duplicate-key": b'{"a": 1, "a": 2}', "nan": b'{"a": NaN}', "bom": b'\xef\xbb\xbf{}',
            "empty": b"", "hooks-not-object": b'{"hooks": []}', "event-not-array": b'{"hooks": {"Stop": {}}}',
            "not-utf8": b'{"a": "\xff"}',
        }
        for label, raw in cases.items():
            with self.subTest(case=label):
                shutil.rmtree(str(self.repo / ".claude"), ignore_errors=True)
                shutil.rmtree(str(self.repo / ".memlet"), ignore_errors=True)
                (self.repo / ".claude").mkdir()
                self.settings().write_bytes(raw)
                before = _tree(self.repo)
                r = self.cli("init", ".", "--hooks")
                self.assertEqual(2, r.returncode, r.stdout)
                self.assertFalse(json.loads(r.stdout)["ok"])
                self.assertEqual(before, _tree(self.repo))  # no backup, no .memlet, no settings change
                r = self.cli("init", ".", "--hooks-remove")
                # valid JSON with an odd `hooks` shape holds no Memlet entry: removal is a no-op, not an error
                self.assertEqual(0 if label in ("hooks-not-object", "event-not-array") else 2, r.returncode, r.stdout)
                self.assertEqual(before, _tree(self.repo))

    def test_symlinked_claude_dir_and_settings_file_are_refused(self):
        target = self.tmp / "elsewhere"
        target.mkdir()
        (target / "settings.local.json").write_text("{}")
        (self.repo / ".claude").symlink_to(target, target_is_directory=True)
        before, outside = _tree(self.repo), _tree(target)
        r = self.cli("init", ".", "--hooks")
        self.assertEqual(2, r.returncode)
        self.assertIn("symlink", json.loads(r.stdout)["error"])
        self.assertEqual((before, outside), (_tree(self.repo), _tree(target)))
        (self.repo / ".claude").unlink()
        (self.repo / ".claude").mkdir()
        self.settings().symlink_to(target / "settings.local.json")
        before = _tree(self.repo)
        r = self.cli("init", ".", "--hooks")
        self.assertEqual(2, r.returncode)
        self.assertEqual((before, outside), (_tree(self.repo), _tree(target)))
        self.assertEqual(2, self.cli("init", ".", "--hooks-remove").returncode)

    def test_project_scope_refuses_without_memlet_on_path(self):
        before = _tree(self.repo)
        r = self.cli("init", ".", "--hooks", "--hooks-scope", "project")
        self.assertEqual(2, r.returncode)
        self.assertIn("`memlet` is not on PATH", json.loads(r.stdout)["error"])
        self.assertEqual(before, _tree(self.repo))

    def test_project_scope_refuses_a_memlet_on_path_without_the_hook_entry(self):
        self.fake_memlet("#!/bin/sh\necho 'usage: memlet' >&2\nexit 2\n")
        r = self.cli("init", ".", "--hooks", "--hooks-scope", "project")
        self.assertEqual(2, r.returncode)
        self.assertIn("does not answer", json.loads(r.stdout)["error"])
        self.assertFalse((self.repo / ".claude").exists())
        # and the written `|| true` protects a session from exactly this kind of executable
        r = subprocess.run(["/bin/sh", "-c", mh.hook_command("memlet", "stop")], input="{}", text=True,
                           env=self.env(), capture_output=True)
        self.assertEqual(0, r.returncode)

    def test_project_scope_writes_the_portable_command_when_memlet_is_on_path(self):
        self.fake_memlet()
        rep = self.install("--hooks-scope", "project")
        self.assertEqual("portable", rep["hooks"]["hookCommand"]["form"])
        doc = json.loads(self.settings("project").read_text())
        self.assertEqual(["memlet admin hook session-start || true", "memlet admin hook post-tool-use || true",
                          "memlet admin hook stop || true", "memlet admin hook pre-compact || true"], _commands(doc))
        self.assertFalse(self.settings("local").exists())
        env = {k: v for k, v in self.env().items() if k not in LAUNCHER_ENV}
        r = subprocess.run(["/bin/sh", "-c", _commands(doc)[0]], input=json.dumps({"cwd": str(self.repo)}),
                           text=True, env=env, cwd=str(self.repo), capture_output=True, timeout=60)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("mod.py::charge", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_local_scope_prefers_memlet_on_path(self):
        self.fake_memlet()
        rep = self.install()
        self.assertEqual("portable", rep["hooks"]["hookCommand"]["form"])
        self.assertTrue(all(c.startswith("memlet admin hook ") for c in _commands(json.loads(self.settings().read_text()))))

    def test_absolute_command_with_spaces_and_quotes_runs_through_sh(self):
        code = self.tmp / "code dir" / "it's here"
        code.mkdir(parents=True)
        for f in HERE.glob("*.py"):
            if not f.name.startswith("test_"):
                shutil.copy2(str(f), str(code / f.name))
        shutil.copytree(str(HERE / "prism_memory"), str(code / "prism_memory"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        wrapper_dir = self.tmp / "py dir" / "o'neil"
        wrapper_dir.mkdir(parents=True)
        wrapper = wrapper_dir / "python wrapper"
        wrapper.write_text("#!/bin/sh\nexec {} \"$@\"\n".format(shlex.quote(PYTHON)))
        wrapper.chmod(0o755)
        (self.repo / ".memlet").mkdir()
        (self.repo / ".memlet" / "config.json").write_text("{}")
        rep = mh.hooks_command(self.repo, which=lambda name: None, interpreter=str(wrapper),
                               cli_path=str(code / "memory_cli.py"))
        self.assertTrue(rep["ok"], rep)
        cmd = _commands(json.loads(self.settings().read_text()))[0]
        self.assertEqual([str(wrapper), str(code / "memory_cli.py"), "admin", "hook", "session-start", "||", "true"],
                         shlex.split(cmd))
        # init for real (via the spaced copy), then run the written command the way Claude Code does
        r = subprocess.run([PYTHON, str(code / "memory_cli.py"), "init", "."], cwd=str(self.repo), env=self.env(),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(0, r.returncode, r.stderr)
        env = {k: v for k, v in self.env().items() if k not in LAUNCHER_ENV}
        r = subprocess.run(["/bin/sh", "-c", cmd], input=json.dumps({"cwd": str(self.repo)}), text=True, env=env,
                           cwd=str(self.repo), capture_output=True, timeout=60)
        self.assertEqual((0, ""), (r.returncode, r.stderr))
        self.assertIn("AGENTS.md:3", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])
        # the real CLI from the spaced copy picks a working interpreter and quotes the spaced script path
        r = subprocess.run([PYTHON, str(code / "memory_cli.py"), "init", ".", "--hooks"], cwd=str(self.repo),
                           env=self.env(), capture_output=True, text=True, timeout=120)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        cmd = _commands(json.loads(self.settings().read_text()))[1]
        self.assertEqual(str(code / "memory_cli.py"), shlex.split(cmd)[1])
        (self.repo / "mod.py").write_text(MOD_STALE)
        r = subprocess.run(["/bin/sh", "-c", cmd], text=True, env=env, cwd=str(self.repo), capture_output=True,
                           input=json.dumps({"cwd": str(self.repo), "tool_input": {"file_path": str(self.repo / "mod.py")}}),
                           timeout=60)
        self.assertEqual((0, ""), (r.returncode, r.stderr))
        self.assertIn("mod.py::charge", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_a_change_made_after_planning_is_never_overwritten(self):
        (self.repo / ".claude").mkdir()
        self.settings().write_text("{}")
        plan = mh.plan_claude(self.repo, "local", remove=False, prefix="memlet", cfg=None)
        self.settings().write_text('{"permissions": {"allow": ["Bash(npm test)"]}}')  # e.g. Claude Code itself
        with self.assertRaises(mh.Refused):
            mh.apply_claude(self.repo, "local", plan, {})
        self.assertEqual('{"permissions": {"allow": ["Bash(npm test)"]}}', self.settings().read_text())
        self.assertEqual([], list(self.settings().parent.glob("*.bak-*")))

    def test_recognition_is_strict(self):
        yes = ["memlet admin hook stop", "memlet admin hook stop || true",
               "'/usr/bin/python3' '/opt/x y/memory_cli.py' admin hook pre-compact || true"]
        no = ["mytool admin hook stop", "memlet admin hook stop && rm -rf x", "echo memlet admin hook stop",
              "memlet admin hook unknown", "python3 memory_cli.py admin hook stop", "memlet admin hook 'stop"]
        for c in yes:
            self.assertTrue(mh.memlet_hook_event({"type": "command", "command": c}), c)
        for c in no:
            self.assertIsNone(mh.memlet_hook_event({"type": "command", "command": c}), c)
        self.assertIsNone(mh.memlet_hook_event({"type": "prompt", "command": "memlet admin hook stop"}))

    def test_lookalike_foreign_entries_and_equal_groups_survive_install_and_remove(self):
        (self.repo / ".claude").mkdir()
        doc = {"hooks": {"Stop": [{"hooks": []}, {"hooks": [{"type": "command", "command": "mytool admin hook stop"}]}],
                         "SessionStart": [{"hooks": [{"type": "command", "command": "memlet admin hook session-start",
                                                      "timeout": 33}]}]}}
        raw = json.dumps(doc).encode()
        self.settings().write_bytes(raw)
        (self.repo / ".memlet").mkdir()
        (self.repo / ".memlet" / "config.json").write_text("{}")
        rep = mh.hooks_command(self.repo, which=lambda name: None, interpreter=PYTHON)
        self.assertTrue(rep["ok"], rep)
        after = json.loads(self.settings().read_text())
        self.assertEqual(33, after["hooks"]["SessionStart"][0]["hooks"][0]["timeout"])  # user's timeout kept
        self.assertEqual(1, len(after["hooks"]["SessionStart"]))
        self.assertEqual({"hooks": []}, after["hooks"]["Stop"][0])
        rep = mh.hooks_command(self.repo, remove=True)
        self.assertTrue(rep["ok"], rep)
        final = json.loads(self.settings().read_text())
        self.assertEqual([{"hooks": []}, {"hooks": [{"type": "command", "command": "mytool admin hook stop"}]}],
                         final["hooks"]["Stop"])
        # SessionStart existed before the install, so its (now empty) array stays; the arrays Memlet created go
        self.assertEqual(["Stop", "SessionStart"], list(final["hooks"]))
        self.assertEqual([], final["hooks"]["SessionStart"])

    def test_flag_validation(self):
        for argv in (["init", ".", "--git-hooks"], ["init", ".", "--hooks-scope", "project"],
                     ["init", ".", "--hooks", "--hooks-remove"], ["init", ".", "--hooks-remove", "--force"],
                     ["init", ".", "--hooks", "--hooks-scope", "team"]):
            with self.subTest(argv=argv):
                r = self.cli(*argv)
                self.assertEqual(2, r.returncode)
        self.assertFalse((self.repo / ".claude").exists())
        self.assertFalse((self.repo / ".memlet").exists())


@unittest.skipUnless(PYTHON, "no interpreter runs outside this process's launcher environment")
class RuntimeTests(_Case):
    def setUp(self):
        super().setUp()
        r = self.cli("init", ".")
        self.assertEqual(0, r.returncode, r.stderr)

    def payload(self, **kw):
        base = {"session_id": "s1", "cwd": str(self.repo)}
        base.update(kw)
        return base

    def test_session_start_injects_verified_constraints_within_budget(self):
        out = self.hook("session-start", self.payload(hook_event_name="SessionStart", source="startup"))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual("SessionStart", out["hookSpecificOutput"]["hookEventName"])
        self.assertIn("- Always call `mod.py::charge` through the retry wrapper. (AGENTS.md:3)", ctx)
        self.assertIn("Not verified", ctx)
        self.assertIn("0 stale", ctx)
        self.assertLessEqual(estimate_tokens(ctx), 400)
        self.assertNotIn("systemMessage", out)

    def test_session_start_reports_stale_and_pending_counts_but_never_pending_text(self):
        (self.repo / "mod.py").write_text(MOD_STALE)
        self.assertTrue(mb.learn_from_transcript(self.repo, self.write_transcript())["ok"])
        out = self.hook("session-start", self.payload())
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("Always call", ctx.split("Not verified")[0])
        self.assertIn("Keep `util.py::helper` pure.", ctx)
        self.assertIn("1 stale", ctx)
        self.assertIn("3 lesson(s) pending", ctx)
        for pending_text in ("release branch", "Never call charge", "migration"):
            self.assertNotIn(pending_text, ctx)
        self.assertIn("stale", out["systemMessage"])

    def test_session_start_budget_is_configurable(self):
        many = "".join(f"- Keep `util.py::helper` pure in case {i} of a long list of similar rules.\n" for i in range(60))
        (self.repo / "AGENTS.md").write_text(AGENTS + many)
        self.assertEqual(0, self.cli("init", ".").returncode)
        out = self.hook("session-start", self.payload())
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(estimate_tokens(ctx), 400)
        self.assertIn("more verified", ctx)
        cfg_path = self.repo / ".memlet" / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["hooks"] = {"sessionStartTokenBudget": 120}  # values below 50 are ignored (default applies)
        cfg_path.write_text(json.dumps(cfg))
        small = self.hook("session-start", self.payload())["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(estimate_tokens(small), 120)
        self.assertLess(len(small), len(ctx))

    def test_session_start_without_rules_or_without_memlet_prints_nothing(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        before = _tree(bare)
        self.assertIsNone(self.hook("session-start", {"cwd": str(bare)}, cwd=bare))
        self.assertEqual(before, _tree(bare))
        self.assertFalse((bare / ".memlet").exists())
        for event in mh.RUNTIME_EVENTS:
            self.assertIsNone(self.hook(event, {"cwd": str(bare), "transcript_path": str(self.write_transcript()),
                                                "tool_input": {"file_path": str(bare / "x.py")}}, cwd=bare))
        self.assertEqual(before, _tree(bare))
        empty = self.tmp / "empty-rules"
        empty.mkdir()
        (empty / "README.md").write_text("hi\n")
        self.assertEqual(0, self.cli("init", ".", cwd=empty).returncode)
        self.assertIsNone(self.hook("session-start", {"cwd": str(empty)}, cwd=empty))

    def test_post_tool_use_names_the_stale_rule(self):
        (self.repo / "mod.py").write_text(MOD_STALE)
        out = self.hook("post-tool-use", self.payload(tool_name="Edit", tool_input={"file_path": str(self.repo / "mod.py")},
                                                      tool_response={"filePath": str(self.repo / "mod.py")}))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual("PostToolUse", out["hookSpecificOutput"]["hookEventName"])
        self.assertIn("STALE `mod.py::charge`", ctx)
        self.assertIn("AGENTS.md:3", ctx)
        self.assertIn("signature_drift", ctx)
        self.assertIn("Always call `mod.py::charge` through the retry wrapper.", ctx)
        self.assertIn("AGENTS.md:3", out["systemMessage"])
        # fallback: tool_response.filePath when tool_input has none; relative path resolved against cwd
        out2 = self.hook("post-tool-use", self.payload(tool_input={}, tool_response={"filePath": str(self.repo / "mod.py")}))
        self.assertIn("mod.py::charge", out2["hookSpecificOutput"]["additionalContext"])
        out3 = self.hook("post-tool-use", self.payload(tool_input={"file_path": "mod.py"}))
        self.assertIn("mod.py::charge", out3["hookSpecificOutput"]["additionalContext"])

    def test_post_tool_use_on_an_unrelated_or_still_valid_file_prints_nothing(self):
        (self.repo / "notes.txt").write_text("x\n")
        for path in (self.repo / "notes.txt", self.repo / "util.py", self.tmp / "outside.py"):
            with self.subTest(path=path.name):
                self.assertIsNone(self.hook("post-tool-use", self.payload(tool_input={"file_path": str(path)})))

    def test_post_tool_use_does_not_walk_the_repository(self):
        calls = []
        real_rglob = Path.rglob

        def spy(self_path, pattern):
            calls.append(str(self_path))
            return real_rglob(self_path, pattern)

        (self.repo / "mod.py").write_text(MOD_STALE)
        payload = json.dumps(self.payload(tool_input={"file_path": str(self.repo / "mod.py")}))
        out = io.StringIO()
        walks = []
        real_walk = os.walk

        def walk_spy(top, *a, **k):
            walks.append(str(top))
            return real_walk(top, *a, **k)

        with mock.patch.object(Path, "rglob", spy), mock.patch("os.walk", walk_spy), \
                mock.patch("os.getcwd", return_value=str(self.repo)):
            mh.run_hook(["post-tool-use"], stdin=io.BytesIO(payload.encode()), stdout=out, hard_exit=False)
        self.assertIn("mod.py::charge", out.getvalue())
        self.assertTrue(all(c.startswith(str(self.repo / ".memlet")) for c in calls), calls)
        self.assertEqual([], walks)

    def test_stop_queues_pending_lessons_only(self):
        transcript = self.write_transcript()
        out = self.hook("stop", self.payload(transcript_path=str(transcript), hook_event_name="Stop",
                                             stop_hook_active=False))
        self.assertEqual(["systemMessage"], list(out))
        self.assertIn("3 new lesson(s) queued as PENDING", out["systemMessage"])
        self.assertIn("memlet learn --pending", out["systemMessage"])
        pend = mb.learn_pending(self.repo)
        self.assertEqual(3, pend["counts"]["pendingNodes"])
        self.assertEqual(0, pend["counts"]["approvedNodes"])
        queue = json.loads((self.repo / mb.QUEUE_REL).read_text())
        from prism_memory.memlet_dream import approval_state
        self.assertEqual(3, len(queue["nodes"]))
        self.assertFalse(any(approval_state(n, "node") == "approved" for n in queue["nodes"]))
        self.assertFalse((self.repo / mb.PROMOTED_PAGE).exists())
        snap = "".join(p.read_text() for p in (self.repo / mb.SNAPSHOT_REL).iterdir())
        for hidden in ("REASONING-XYZ", "TOOLINPUT-XYZ", "TOOLPAYLOAD-XYZ"):
            self.assertNotIn(hidden, snap)
        self.assertIsNone(self.hook("stop", self.payload(transcript_path=str(transcript))))  # nothing new
        more = TRANSCRIPT + [{"type": "user", "timestamp": "2026-11-09T10:00:00Z",
                              "message": {"role": "user", "content": "Never edit the generated schema file by hand"}}]
        transcript.write_text("\n".join(json.dumps(r) for r in more) + "\n")
        out = self.hook("pre-compact", self.payload(transcript_path=str(transcript), trigger="auto"))
        self.assertIn("1 new lesson(s)", out["systemMessage"])
        self.assertEqual(4, mb.learn_pending(self.repo)["counts"]["pendingNodes"])
        self.assertEqual(0, mb.learn_pending(self.repo)["counts"]["approvedNodes"])

    def test_stop_ignores_unusable_transcripts(self):
        t = self.write_transcript()
        link = self.tmp / "link.jsonl"
        link.symlink_to(t)
        log = self.tmp / "x.log"
        log.write_text("RULE: something\n")
        for tp in (str(link), str(log), "relative.jsonl", str(self.tmp / "missing.jsonl"), str(self.tmp)):
            with self.subTest(tp=tp):
                self.assertIsNone(self.hook("stop", self.payload(transcript_path=tp)))
        self.assertFalse((self.repo / mb.QUEUE_REL).exists())

    def test_stop_skips_a_transcript_over_the_cap_and_logs_without_its_text(self):
        cfg_path = self.repo / ".memlet" / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["hooks"] = {"maxTranscriptBytes": 100}
        cfg_path.write_text(json.dumps(cfg))
        self.assertIsNone(self.hook("stop", self.payload(transcript_path=str(self.write_transcript()))))
        self.assertFalse((self.repo / mb.QUEUE_REL).exists())
        log = (self.repo / mh.LOG_REL).read_text()
        self.assertIn("over the cap", log)
        self.assertNotIn("release branch", log)

    def test_bad_stdin_and_odd_fields_never_break(self):
        raws = ["", "   ", "not json", "[1, 2]", "null", "42", '"text"', '{"cwd": ', "\x00\xff", "{}" * 3]
        for event in mh.RUNTIME_EVENTS:
            for raw in raws:
                with self.subTest(event=event, raw=raw):
                    self.assertIsNone(self.hook(event, None, raw=raw))
        odd = [{}, {"cwd": 5}, {"cwd": None, "tool_input": "x"}, {"cwd": ["a"], "tool_input": {"file_path": [1]}},
               {"tool_input": {"file_path": None}, "tool_response": {"filePath": 7}},
               {"transcript_path": {"a": 1}}, {"transcript_path": "\x00"}, {"cwd": "relative/dir"},
               {"cwd": str(self.repo / "mod.py")}, {"cwd": "/nonexistent/dir"}, {"tool_input": {"file_path": ""}},
               {"tool_input": {"file_path": "x" * 10000}}, {"transcript_path": ""}]
        for event in mh.RUNTIME_EVENTS:
            for payload in odd:
                with self.subTest(event=event, payload=str(payload)[:60]):
                    out = self.hook(event, payload)
                    if out is not None:  # session-start may still answer from the process cwd
                        self.assertEqual("session-start", event)
                        self.assertIn("hookSpecificOutput", out)
        log = (self.repo / mh.LOG_REL).read_text()
        self.assertIn("malformed", log)
        self.assertNotIn("not json", log)

    def test_unknown_events_extra_arguments_and_help_are_silent(self):
        for argv in (["admin", "hook"], ["admin", "hook", "nope"], ["admin", "hook", "stop", "extra"],
                     ["admin", "hook", "--help"], ["admin", "hook", "-h"], ["admin", "hook", "Stop"]):
            with self.subTest(argv=argv):
                r = self.cli(*argv, stdin="{}")
                self.assertEqual((0, "", ""), (r.returncode, r.stdout, r.stderr))

    def test_hidden_from_every_help_listing(self):
        for argv in (["--help"], ["admin", "--help"], ["init", "--help"], ["admin", "init", "--help"]):
            r = self.cli(*argv)
            self.assertEqual(0, r.returncode)
            self.assertNotIn("admin hook", r.stdout)
            if "init" not in argv:
                self.assertNotRegex(r.stdout, r"\bhook\b")
        r = self.cli("hook", "--help")
        self.assertEqual(2, r.returncode)  # no top-level `hook` command either

    def test_injected_internal_exception_still_exits_zero_and_logs_no_payload_text(self):
        site = self.tmp / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            "import sys\nsys.path.insert(0, %r)\n"
            "from prism_memory import memlet_bridge, memlet_wiki\n"
            "def boom(*a, **k):\n    raise RuntimeError('SECRET-TRANSCRIPT-TEXT ' + repr(a))\n"
            "memlet_bridge.changed_check = boom\nmemlet_bridge.learn_from_transcript = boom\n"
            "memlet_wiki.build_handover = boom\n" % str(HERE))
        env = self.env(PYTHONPATH=str(site))
        (self.repo / "mod.py").write_text(MOD_STALE)
        payload = self.payload(tool_input={"file_path": str(self.repo / "mod.py")},
                               transcript_path=str(self.write_transcript()))
        for event in mh.RUNTIME_EVENTS:
            with self.subTest(event=event):
                self.assertIsNone(self.hook(event, payload, env=env))
        log = (self.repo / mh.LOG_REL).read_text()
        self.assertEqual(4, log.count("error: RuntimeError"))
        self.assertNotIn("SECRET", log)
        # in-process as well, through memory_cli.cli()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(mb, "changed_check", side_effect=KeyError("SECRET")), \
                mock.patch("os.getcwd", return_value=str(self.repo)), \
                mock.patch.object(sys, "argv", ["memlet", "admin", "hook", "post-tool-use"]), \
                mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = memory_cli.cli()
        self.assertEqual((0, "", ""), (code, out.getvalue(), err.getvalue()))
        self.assertIn("post-tool-use error: KeyError", (self.repo / mh.LOG_REL).read_text())

    def test_library_prints_cannot_corrupt_stdout(self):
        def noisy(*a, **k):
            print("NOISE on stdout")
            print("NOISE on stderr", file=sys.stderr)
            return {"ok": True, "exitCode": 0, "affected": [], "changedFiles": []}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(mb, "changed_check", noisy), mock.patch("os.getcwd", return_value=str(self.repo)):
            code = mh.run_hook(["post-tool-use"], stdin=io.BytesIO(json.dumps(self.payload(
                tool_input={"file_path": str(self.repo / "mod.py")})).encode()), stdout=out, stderr=err, hard_exit=False)
        self.assertEqual((0, "", ""), (code, out.getvalue(), err.getvalue()))

    def test_self_imposed_deadline_ends_a_slow_run_with_exit_zero(self):
        site = self.tmp / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            "import sys, time\nsys.path.insert(0, %r)\n"
            "from prism_memory import memlet_bridge\n"
            "def slow(*a, **k):\n    time.sleep(30)\n"
            "memlet_bridge.changed_check = slow\n" % str(HERE))
        cfg_path = self.repo / ".memlet" / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["hooks"] = {"deadlineSeconds": 1}
        cfg_path.write_text(json.dumps(cfg))
        start = time.monotonic()
        self.assertIsNone(self.hook("post-tool-use", self.payload(tool_input={"file_path": str(self.repo / "mod.py")}),
                                    env=self.env(PYTHONPATH=str(site))))
        self.assertLess(time.monotonic() - start, 6)
        self.assertIn("timeout", (self.repo / mh.LOG_REL).read_text())

    def test_hooks_log_is_size_capped(self):
        log = self.repo / mh.LOG_REL
        log.write_text(("old line\n" * 9000))
        self.assertGreater(log.stat().st_size, mh.LOG_CAP_BYTES)
        self.assertIsNone(self.hook("stop", None, raw="garbage"))
        self.assertLessEqual(log.stat().st_size, mh.LOG_CAP_BYTES)
        self.assertTrue(log.read_text().endswith("skip: empty, malformed or oversized stdin\n"))

    def test_root_is_payload_cwd_inside_git_else_process_cwd(self):
        if not HAS_GIT:
            self.skipTest("git not installed")
        self.git_init()
        sub = self.repo / "pkg"
        sub.mkdir()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        out = self.hook("session-start", {"cwd": str(sub)}, cwd=elsewhere)  # git subdir -> walks up to .memlet
        self.assertIn("mod.py::charge", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.hook("session-start", {"cwd": str(elsewhere)}, cwd=elsewhere))
        out = self.hook("session-start", {"cwd": str(elsewhere)}, cwd=self.repo)  # not git -> process cwd
        self.assertIn("mod.py::charge", out["hookSpecificOutput"]["additionalContext"])
        self.assertFalse((elsewhere / ".memlet").exists())

    def test_installed_commands_run_end_to_end_through_sh(self):
        self.install()
        doc = json.loads(self.settings().read_text())
        env = {k: v for k, v in self.env().items() if k not in LAUNCHER_ENV}
        cmds = {mh.memlet_hook_event(h): h["command"] for ev in doc["hooks"].values() for g in ev for h in g["hooks"]}
        (self.repo / "mod.py").write_text(MOD_STALE)
        cases = {
            "session-start": ({"cwd": str(self.repo), "hook_event_name": "SessionStart"}, "hookSpecificOutput"),
            "post-tool-use": ({"cwd": str(self.repo), "tool_name": "Write",
                               "tool_input": {"file_path": str(self.repo / "mod.py"), "content": MOD_STALE}},
                              "hookSpecificOutput"),
            "stop": ({"cwd": str(self.repo), "transcript_path": str(self.write_transcript())}, "systemMessage"),
            "pre-compact": ({"cwd": str(self.repo), "transcript_path": str(self.write_transcript())}, None),
        }
        for event, (payload, key) in cases.items():
            with self.subTest(event=event):
                r = subprocess.run(["/bin/sh", "-c", cmds[event]], input=json.dumps(payload), text=True, env=env,
                                   cwd=str(self.repo), capture_output=True, timeout=60)
                self.assertEqual((0, ""), (r.returncode, r.stderr))
                if key is None:
                    self.assertEqual("", r.stdout)
                else:
                    self.assertIn(key, json.loads(r.stdout))


@unittest.skipUnless(PYTHON and HAS_GIT, "needs git and a launcher-independent interpreter")
class GitHookTests(_Case):
    def setUp(self):
        super().setUp()
        self.git_init()

    def hooks_dir(self):
        return self.repo / ".git" / "hooks"

    def test_git_hooks_only_with_the_flag(self):
        self.install()
        for name in ("pre-commit", "post-merge"):
            self.assertFalse((self.hooks_dir() / name).exists())
        rep = self.install("--git-hooks")
        for name in ("pre-commit", "post-merge"):
            p = self.hooks_dir() / name
            self.assertTrue(p.is_file())
            self.assertIn(mh.GIT_MARKER, p.read_text())
            self.assertTrue(os.stat(str(p)).st_mode & stat.S_IXUSR)
        self.assertEqual({"pre-commit": "create", "post-merge": "create"},
                         {i["hook"]: i["op"] for i in rep["hooks"]["gitHooks"]["items"]})
        again = self.install("--git-hooks")
        self.assertEqual({"pre-commit": "unchanged", "post-merge": "unchanged"},
                         {i["hook"]: i["op"] for i in again["hooks"]["gitHooks"]["items"]})

    def test_foreign_hook_is_untouched_and_removal_removes_only_memlets(self):
        foreign = self.hooks_dir() / "pre-commit"
        foreign.write_text("#!/bin/sh\necho foreign pre-commit\nexit 0\n")
        foreign.chmod(0o755)
        original = foreign.read_bytes()
        rep = self.install("--git-hooks")
        items = {i["hook"]: i for i in rep["hooks"]["gitHooks"]["items"]}
        self.assertEqual("skipped", items["pre-commit"]["op"])
        self.assertIn("not written by Memlet", items["pre-commit"]["reason"])
        self.assertEqual(original, foreign.read_bytes())
        self.assertTrue((self.hooks_dir() / "post-merge").is_file())
        r = self.cli("init", ".", "--hooks-remove")
        self.assertEqual(0, r.returncode)
        self.assertTrue((self.hooks_dir() / "post-merge").is_file())  # needs --git-hooks to be removed
        self.assertTrue(any("--git-hooks" in n for n in json.loads(r.stdout)["notices"]))
        r = self.cli("init", ".", "--hooks-remove", "--git-hooks")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertFalse((self.hooks_dir() / "post-merge").exists())
        self.assertEqual(original, foreign.read_bytes())

    def test_pre_commit_warns_but_never_blocks(self):
        self.install("--git-hooks")
        (self.repo / "mod.py").write_text(MOD_STALE)
        self.git("add", "mod.py")
        env = {k: v for k, v in self.env().items() if k not in LAUNCHER_ENV}
        r = subprocess.run(["git", "commit", "-q", "-m", "change charge"], cwd=str(self.repo), env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("memlet: warning", r.stderr)
        self.assertIn("mod.py::charge", r.stderr)
        self.assertEqual("change charge", self.git("log", "-1", "--format=%s").stdout.strip())
        # unrelated commit: silent
        (self.repo / "notes.txt").write_text("x\n")
        self.git("add", "notes.txt")
        r = subprocess.run(["git", "commit", "-q", "-m", "notes"], cwd=str(self.repo), env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual((0, ""), (r.returncode, r.stderr))

    def test_post_merge_prints_one_summary_line(self):
        self.install("--git-hooks")
        self.git("checkout", "-q", "-b", "feature")
        (self.repo / "mod.py").write_text(MOD_STALE)
        self.git("commit", "-q", "-am", "feature change", "--no-verify")
        self.git("checkout", "-q", "-")
        env = {k: v for k, v in self.env().items() if k not in LAUNCHER_ENV}
        r = subprocess.run(["git", "merge", "-q", "--no-edit", "feature"], cwd=str(self.repo), env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(0, r.returncode, r.stderr)
        lines = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.startswith("memlet:")]
        self.assertEqual(1, len(lines), r.stdout + r.stderr)
        self.assertIn("post-merge check", lines[0])
        self.assertIn("1 stale", lines[0])

    def test_core_hooks_path_is_honoured(self):
        self.git("config", "core.hooksPath", ".githooks")
        rep = self.install("--git-hooks")
        self.assertTrue((self.repo / ".githooks" / "pre-commit").is_file())
        self.assertFalse((self.hooks_dir() / "pre-commit").exists())
        self.assertTrue(any("inside the working tree" in n for n in rep["hooks"]["notices"]))
        r = self.cli("init", ".", "--hooks-remove", "--git-hooks")
        self.assertEqual(0, r.returncode)
        self.assertFalse((self.repo / ".githooks" / "pre-commit").exists())

    def test_git_hooks_outside_a_work_tree_are_skipped_with_a_reason(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "AGENTS.md").write_text(AGENTS)
        r = self.cli("init", ".", "--hooks", "--git-hooks", cwd=plain)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual("not inside a git work tree", json.loads(r.stdout)["hooks"]["gitHooks"]["skipped"])


if __name__ == "__main__":
    unittest.main()
