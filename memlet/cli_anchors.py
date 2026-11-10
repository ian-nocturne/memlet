"""`memlet admin anchors`: suggest, accept, list and drop sidecar code anchors (see `memlet.memlet_anchors`).

    memlet admin anchors suggest [--repo PATH] [--limit N] [--format markdown]
    memlet admin anchors accept ID... | --all-unique [--dry-run] [--repo PATH]
    memlet admin anchors list [--repo PATH]
    memlet admin anchors drop ID... [--dry-run] [--repo PATH]

No subcommand calls a model or the network; only `accept` and `drop` write, and only inside `.memlet/`.
"""

from typing import Any, Dict

from memlet import cli_plugins

COMMANDS = {"anchors": {"help": "Suggest code anchors for rules that mention code; accept, list or drop them.",
                        "product": False, "group": "Rules/wiki"}}


def register(sub) -> None:
    import argparse
    p = sub.add_parser("anchors", help=COMMANDS["anchors"]["help"], formatter_class=argparse.RawDescriptionHelpFormatter,
                       description="Rules that mention code in backticks or as name() but carry no `path::symbol` "
                                   "anchor can be anchored without editing your files: suggestions are resolved "
                                   "against the repository's symbols, and accepted anchors live in .memlet/config.json.",
                       epilog="A path in a rule is read from the repository root and from the instruction file's "
                              "folder; when both exist the mention is ambiguous. Write a leading `/` (`/src/auth.py`) "
                              "to mean the repository root only.\n\n"
                              "Integrity, not authentication: the ledger check shows an accepted entry is exactly what "
                              "`anchors accept` recorded in this repository's ledger and was not dropped since. It does "
                              "not show who accepted it: a process that can rewrite both .memlet/config.json and the "
                              "ledger can forge an acceptance, and copying both files to another clone carries the "
                              "acceptances along.")
    nested = p.add_subparsers(dest="anchors_command", metavar="{suggest,accept,list,drop}")
    nested.required = True
    s = nested.add_parser("suggest", help="List rules that could be anchored (read-only).")
    s.add_argument("--repo", default=".", help="Repository path (default: current directory)")
    s.add_argument("--limit", type=int, default=200, help="At most this many suggestions and ambiguous entries")
    cli_plugins.add_format_option(s)
    a = nested.add_parser("accept", help="Accept suggestions by id, or every current suggestion with --all-unique.")
    a.add_argument("ids", nargs="*", metavar="ID")
    a.add_argument("--all-unique", action="store_true", help="Accept every current (unambiguous) suggestion")
    a.add_argument("--dry-run", action="store_true", help="Show what would be accepted; write nothing")
    a.add_argument("--repo", default=".")
    cli_plugins.add_format_option(a)
    lst = nested.add_parser("list", help="Accepted sidecar anchors and whether they still attach.")
    lst.add_argument("--repo", default=".")
    cli_plugins.add_format_option(lst)
    d = nested.add_parser("drop", help="Remove accepted sidecar anchors by id.")
    d.add_argument("ids", nargs="*", metavar="ID")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--repo", default=".")
    cli_plugins.add_format_option(d)


def dispatch(args) -> Dict[str, Any]:
    from memlet import memlet_anchors as ma
    cmd = getattr(args, "anchors_command", None)
    repo = getattr(args, "repo", ".") or "."
    try:
        if cmd == "suggest":
            res = ma.suggest_anchors(repo, limit=args.limit)
        elif cmd == "accept":
            if args.ids and args.all_unique:
                return {"ok": False, "exitCode": 2, "command": "anchors accept",
                        "error": "give either suggestion ids or --all-unique, not both",
                        "formatted": "- ERROR: give either suggestion ids or --all-unique, not both\n"}
            res = ma.accept_suggestions(repo, args.ids, all_unique=args.all_unique, dry_run=args.dry_run)
        elif cmd == "list":
            res = ma.list_sidecar_anchors(repo)
        elif cmd == "drop":
            res = ma.drop_sidecar_anchors(repo, args.ids, dry_run=args.dry_run)
        else:
            return {"ok": False, "exitCode": 2, "error": f"unknown anchors command: {cmd}", "formatted": ""}
    except ValueError as exc:  # repository path not found
        res = {"ok": False, "exitCode": 2, "command": f"anchors {cmd}", "error": str(exc), "formatted": f"- ERROR: {exc}\n"}
    if getattr(args, "format", "json") == "markdown" and isinstance(res.get("formatted"), str):
        res["_cli_text"] = res["formatted"]  # an admin command: the router renders only product commands itself
    return res
