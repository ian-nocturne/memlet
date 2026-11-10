"""The CLI plug-in point: optional modules add commands without touching the router or older commands."""

import contextlib
import io
import json
import sys
import types
import unittest
from unittest.mock import patch

import memory_cli
from memlet import cli_plugins


def _fake_plugin():
    mod = types.ModuleType("memlet.cli_fake")
    mod.COMMANDS = {"shine": {"help": "Polish something.", "product": True},
                    "polish-admin": {"help": "Hidden helper.", "group": "Maintenance"}}
    mod.calls = []

    def register(sub):
        p = sub.add_parser("shine", help="Polish something.")
        p.add_argument("--times", type=int, default=1)
        cli_plugins.add_format_option(p)
        sub.add_parser("polish-admin")

    def dispatch(args):
        mod.calls.append((args.action, getattr(args, "times", None), args.root is not None))
        return {"ok": True, "command": args.action, "formatted": "shiny\n"}

    mod.register, mod.dispatch = register, dispatch
    return mod


def _run(argv, plugin):
    out, err = io.StringIO(), io.StringIO()
    with patch.object(cli_plugins, "load", return_value=[plugin] if plugin else []), \
            patch.object(sys, "argv", ["memlet"] + argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = memory_cli.cli()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class CliPluginTests(unittest.TestCase):
    def test_no_plugins_leaves_the_help_surface_alone(self):
        code, out, _ = _run(["--help"], None)
        self.assertEqual(0, code)
        self.assertIn("{init,check,recall,handover,learn,ui,admin}", out)

    def test_product_command_is_listed_and_dispatched(self):
        plugin = _fake_plugin()
        code, out, _ = _run(["--help"], plugin)
        self.assertIn("{init,check,recall,handover,learn,ui,shine,admin}", out)
        self.assertIn("Polish something.", out)
        self.assertNotIn("polish-admin", out.split("options:")[0])
        code, out, err = _run(["shine", "--times", "3"], plugin)
        self.assertEqual(0, code, err)
        self.assertEqual({"ok": True, "command": "shine", "formatted": "shiny\n"}, json.loads(out))
        self.assertEqual([("shine", 3, True)], plugin.calls)

    def test_admin_route_global_flags_and_markdown(self):
        plugin = _fake_plugin()
        code, out, err = _run(["admin", "shine", "--standalone", "--format", "markdown"], plugin)
        self.assertEqual(0, code, err)
        self.assertEqual("shiny\n", out)
        code, out, _ = _run(["admin", "--help"], plugin)
        self.assertIn("polish-admin", out)
        code, out, err = _run(["admin", "polish-admin"], plugin)
        self.assertEqual(0, code, err)

    def test_plugin_cannot_shadow_a_builtin_command(self):
        plugin = _fake_plugin()
        plugin.COMMANDS = {"recall": {"help": "x", "product": True}}
        code, _out, err = _run(["--help"], plugin)
        self.assertEqual(1, code)
        self.assertIn("collides with a built-in command", err)

    def test_failed_result_sets_the_exit_code(self):
        plugin = _fake_plugin()
        plugin.dispatch = lambda args: {"ok": False, "error": "nope"}
        code, out, _ = _run(["shine"], plugin)
        self.assertEqual(1, code)
        self.assertFalse(json.loads(out)["ok"])

    def test_legacy_dream_alias_is_gone_so_a_plugin_can_own_the_name(self):
        code, _out, err = _run(["dream", "--snapshot", "x", "--out-json", "y"], None)
        self.assertEqual(2, code)
        self.assertIn("invalid choice", err)
        code, _out, _ = _run(["admin", "dream-build", "--help"], None)
        self.assertEqual(0, code)


if __name__ == "__main__":
    unittest.main()
