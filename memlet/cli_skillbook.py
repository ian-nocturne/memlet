"""CLI plug-in for `memlet admin skillbook` (Unit N3: approved lessons packaged as an Agent Skill)."""

import argparse
from typing import Any, Dict

from . import cli_plugins, vocabulary as v

COMMANDS = {
    "skillbook": {
        "help": v.SKILL_CLI_HELP,
        "product": False,
        "group": "Rules/wiki",
    }
}


class _RepoAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace._repo_explicit = True


def register(sub) -> None:
    parser = sub.add_parser(
        "skillbook",
        prog="memlet admin skillbook",
        help=COMMANDS["skillbook"]["help"],
        description=COMMANDS["skillbook"]["help"],
    )
    parser.add_argument("--repo", action=_RepoAction, default=".", help="Memlet repository (default: current directory).")
    parser.add_argument("--name", default=v.SKILL_DEFAULT_NAME, help=v.SKILL_CLI_NAME_HELP)
    parser.add_argument("--max-lines", type=int, default=500, help="Maximum body lines (default: 500).")
    parser.add_argument("--budget", type=int, default=5000, help="Approximate body token budget (default: 5000).")
    parser.add_argument("--write", action="store_true", help=v.SKILL_CLI_WRITE_HELP)
    parser.add_argument("--out", default=None, metavar="DIR", help=v.SKILL_CLI_OUT_HELP)
    parser.add_argument("--force", action="store_true", help=v.SKILL_CLI_FORCE_HELP)
    parser.add_argument("--check", default=None, metavar="PATH", help=v.SKILL_CLI_CHECK_HELP)
    parser.add_argument("--format", choices=("json", "markdown"), default="json", help="Output format (default: json).")


def dispatch(args) -> Dict[str, Any]:
    from . import memlet_skillbook as sb

    if args.check is not None:
        if args.write or args.out is not None or args.force:
            res = {
                "ok": False,
                "exitCode": 2,
                "reason": "--check cannot be combined with --write, --out or --force",
                "error": "--check cannot be combined with --write, --out or --force",
            }
            if args.format == "markdown":
                res["_cli_text"] = "- " + res["reason"] + "\n"
            return res
        token = None
        if getattr(args, "_repo_explicit", False) or args.repo != ".":
            token = sb._check_repo_override.set(args.repo)
        try:
            res = dict(
                sb.check_skill(
                    args.check,
                    max_lines=args.max_lines,
                    budget=args.budget,
                )
            )
        finally:
            if token is not None:
                sb._check_repo_override.reset(token)
        if not res.get("current"):
            res["exitCode"] = 1
        if args.format == "markdown":
            res["_cli_text"] = str(res.get("reason") or v.SKILL_STATUS_UNREADABLE) + "\n"
        return res

    if (args.out is not None or args.force) and not args.write:
        res = {
            "ok": False,
            "exitCode": 2,
            "reason": "--out and --force require --write",
            "error": "--out and --force require --write",
        }
        if args.format == "markdown":
            res["_cli_text"] = "- " + res["reason"] + "\n"
        return res

    if args.write:
        res = dict(
            sb.write_skill(
                args.repo,
                out=args.out,
                name=args.name,
                max_lines=args.max_lines,
                budget=args.budget,
                force=args.force,
            )
        )
        if not res.get("ok"):
            res["exitCode"] = 1
        if args.format == "markdown":
            res["_cli_text"] = (
                (res["path"] + "\n")
                if res.get("ok")
                else ("- " + str(res.get("reason") or res.get("error") or "write failed") + "\n")
            )
        return res

    res = dict(
        sb.build_skill(
            args.repo,
            name=args.name,
            max_lines=args.max_lines,
            budget=args.budget,
        )
    )
    if not res.get("ok"):
        res["exitCode"] = 1
    if args.format == "markdown":
        res["_cli_text"] = (
            res["text"]
            if res.get("ok")
            else ("- " + str(res.get("reason") or res.get("error") or "build failed") + "\n")
        )
    return res
