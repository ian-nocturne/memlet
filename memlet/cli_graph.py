"""Read-only graph CLI; no model, external store, or writes."""
from . import cli_plugins, vocabulary as v

COMMANDS = {"graph": {"help": v.GRAPH_CLI_HELP,
                      "product": False, "group": "Rules/wiki"}}


def register(sub):
    parser = sub.add_parser("graph", prog="memlet admin graph", help=COMMANDS["graph"]["help"])
    nested = parser.add_subparsers(dest="graph_command")
    nested.required = True
    show = nested.add_parser("show", help=v.GRAPH_CLI_SHOW_HELP)
    show.add_argument("target", metavar="TARGET")
    show.add_argument("--repo", default=".")
    show.add_argument("--tiers", default="approved", help=v.GRAPH_CLI_TIERS_HELP)
    show.add_argument("--as-of", default=None, metavar="TIME")
    show.add_argument("--budget", type=int, default=600)
    cli_plugins.add_format_option(show)
    stats = nested.add_parser("stats", help=v.GRAPH_CLI_STATS_HELP)
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
                [v.GRAPH_STATS_LINE % (kind, n) for kind, n in counts["nodes"].items()] +
                [v.GRAPH_STATS_LINE % (kind, n) for kind, n in counts["edges"].items()] +
                [v.GRAPH_STATS_LINE % (v.GRAPH_STATS_UNATTACHED, counts["unattachedLessons"]),
                 v.GRAPH_STATS_LINE % (v.GRAPH_STATS_UNANCHORED, counts["unanchoredRules"])]) + "\n"
    result["exitCode"] = 0 if result["ok"] else 2
    if args.format == "markdown":
        result["_cli_text"] = result.get("formatted", "- " + result.get("reason", v.GRAPH_UNAVAILABLE) + "\n")
    return result
