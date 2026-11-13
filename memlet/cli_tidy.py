"""`memlet admin tidy`: a read-only report of lessons a person might merge, retire or promote; no model, no writes."""
import argparse

from . import cli_plugins, vocabulary as v

COMMANDS = {"tidy": {"help": v.TIDY_CLI_HELP, "product": False, "group": "Rules/wiki"}}


def _days(text):
    """A plain usage error (exit 2), never a traceback, for a value the report cannot represent."""
    from .memlet_tidy import MAX_STALE_DAYS
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid int value: %r" % text)
    if not 0 <= value <= MAX_STALE_DAYS:
        raise argparse.ArgumentTypeError("must be from 0 to %d" % MAX_STALE_DAYS)
    return value


def _min_seen(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid int value: %r" % text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be an integer of at least 1")
    return value


def register(sub):
    parser = sub.add_parser("tidy", prog="memlet admin tidy", help=COMMANDS["tidy"]["help"], description=COMMANDS["tidy"]["help"])
    parser.add_argument("--repo", default=".", help="Repository to inspect (default: current directory).")
    parser.add_argument("--stale-days", type=_days, default=180, metavar="N", help="Age for old unseen lessons in days (default: 180).")
    parser.add_argument("--min-seen", type=_min_seen, default=3, metavar="N", help="Sessions needed for promotion (default: 3).")
    cli_plugins.add_format_option(parser, help="Output format (default: json).")


def dispatch(args):
    from . import memlet_tidy
    result = memlet_tidy.tidy_report(args.repo, stale_days=args.stale_days, min_seen=args.min_seen)
    result["command"] = "tidy"
    result.setdefault("exitCode", 0 if result["ok"] else 2)
    if args.format == "markdown":
        result["_cli_text"] = result.get("formatted", "- ERROR: " + str(result.get("error")) + "\n")
    return result
