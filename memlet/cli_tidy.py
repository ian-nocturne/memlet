"""`memlet admin tidy`: a read-only report of lessons a person might merge, retire or promote; no model, no writes."""
from . import cli_plugins, vocabulary as v

COMMANDS = {"tidy": {"help": v.TIDY_CLI_HELP, "product": False, "group": "Rules/wiki"}}


def register(sub):
    parser = sub.add_parser("tidy", prog="memlet admin tidy", help=COMMANDS["tidy"]["help"])
    parser.add_argument("--repo", default=".")
    parser.add_argument("--stale-days", type=int, default=180, metavar="N")
    parser.add_argument("--min-seen", type=int, default=3, metavar="N")
    cli_plugins.add_format_option(parser)


def dispatch(args):
    from . import memlet_tidy
    result = memlet_tidy.tidy_report(args.repo, stale_days=args.stale_days, min_seen=args.min_seen)
    result["command"] = "tidy"
    result.setdefault("exitCode", 0 if result["ok"] else 2)
    if args.format == "markdown":
        result["_cli_text"] = result.get("formatted", "- ERROR: " + str(result.get("error")) + "\n")
    return result
