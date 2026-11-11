"""Read-only graph CLI; no model, external store, or writes."""
from . import cli_plugins

COMMANDS = {"graph": {"help": "Inspect the typed, time-aware memory attached to code.",
                      "product": False, "group": "Rules/wiki"}}


def register(sub):
    parser = sub.add_parser("graph", prog="memlet admin graph", help=COMMANDS["graph"]["help"])
    nested = parser.add_subparsers(dest="graph_command")
    nested.required = True
    show = nested.add_parser("show", help="Rules and lessons attached to a file, symbol or memory id.")
    show.add_argument("target", metavar="TARGET")
    show.add_argument("--repo", default=".")
    show.add_argument("--tiers", default="approved", help="approved or approved,hint")
    show.add_argument("--as-of", default=None, metavar="TIME")
    show.add_argument("--budget", type=int, default=600)
    cli_plugins.add_format_option(show)
    stats = nested.add_parser("stats", help="Node, relation and unattached memory counts.")
    stats.add_argument("--repo", default=".")
    cli_plugins.add_format_option(stats)


def dispatch(args):
    from . import memlet_graph as graph
    if args.graph_command == "show":
        result = graph.neighbors(args.repo, args.target, tiers=tuple(args.tiers.split(",")),
                                 budget=args.budget, as_of=args.as_of)
    else:
        built = graph.build_graph(args.repo)
        result = ({"ok": True, "counts": built["counts"], "notices": built["notices"],
                   "truncated": built["truncated"]} if built["ok"] else built)
        if result["ok"]:
            counts = result["counts"]
            result["formatted"] = "\n".join(
                ["- " + kind + ": " + str(n) for kind, n in counts["nodes"].items()] +
                ["- " + kind + ": " + str(n) for kind, n in counts["edges"].items()] +
                ["- Lessons attached to no file: " + str(counts["unattachedLessons"]),
                 "- Rules with no anchor: " + str(counts["unanchoredRules"])]) + "\n"
    result["exitCode"] = 0 if result["ok"] else 2
    if args.format == "markdown":
        result["_cli_text"] = result.get("formatted", "- " + result.get("reason", "graph unavailable") + "\n")
    return result
