"""Plug-in point for CLI commands that live outside `memory_cli.py`.

A module named in `PLUGIN_MODULES` is used when it exists. It may define:

    COMMANDS = {"name": {"help": "one line", "product": False, "group": "Admin group label"}, ...}
    def register(sub): ...          # add one parser per name in COMMANDS with sub.add_parser(name, ...)
    def dispatch(args): ...         # return a result dict for its own commands, or None

`product: True` puts the command in the top-level help next to the built-in verbs; every command is also
reachable as `memlet admin <name>`. Result dicts follow the router's conventions: `ok`, `exitCode` when it
matters, and `formatted` (or `_cli_text`) for the text rendering. Use `add_format_option` so the router can
tell an explicit `--format` from the default and switch to text on a terminal.
"""

import argparse
import importlib
import importlib.util
from typing import Any, Dict, List, Optional

PLUGIN_MODULES = ("memlet.cli_dream", "memlet.cli_anchors", "memlet.cli_eval", "memlet.cli_graph", "memlet.cli_tidy",
                  "memlet.cli_skillbook")


class _Format(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace._format_explicit = True


def add_format_option(parser, choices=("json", "markdown"), default="json"):
    parser.add_argument("--format", action=_Format, default=default, choices=list(choices))


def load() -> List[Any]:
    """Import the plug-in modules that are present. A module that exists but fails to import raises."""
    found = []
    for name in PLUGIN_MODULES:
        if importlib.util.find_spec(name) is None:
            continue
        found.append(importlib.import_module(name))
    return found


def commands(plugins: List[Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for plugin in plugins:
        for name, meta in (getattr(plugin, "COMMANDS", None) or {}).items():
            if name in out:
                raise ValueError("CLI plug-in command defined twice: " + name)
            out[name] = dict(meta, _plugin=plugin)
    return out


def dispatch(plugins: List[Any], args) -> Optional[Dict[str, Any]]:
    for plugin in plugins:
        if getattr(args, "action", None) in (getattr(plugin, "COMMANDS", None) or {}):
            return plugin.dispatch(args)
    return None
