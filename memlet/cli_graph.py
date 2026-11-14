"""Read-only graph CLI; no model, external store, or writes."""

from memlet.memlet_dream import call_foreign as _call_foreign

from . import cli_plugins, vocabulary as v

COMMANDS = {"graph": {"help": v.GRAPH_CLI_HELP,
                      "product": False, "group": "Rules/wiki"}}


def register(sub):
    parser = _call_foreign(lambda: sub.add_parser("graph", prog="memlet admin graph", help=COMMANDS["graph"]["help"]))
    nested = _call_foreign(lambda: parser.add_subparsers(dest="graph_command"))
    _call_foreign(setattr, nested, "required", True)
    show = _call_foreign(lambda: nested.add_parser("show", help=v.GRAPH_CLI_SHOW_HELP))
    _call_foreign(lambda: show.add_argument("target", metavar="TARGET"))
    _call_foreign(lambda: show.add_argument("--repo", default="."))
    _call_foreign(lambda: show.add_argument("--tiers", default="approved", help=v.GRAPH_CLI_TIERS_HELP))
    _call_foreign(lambda: show.add_argument("--as-of", default=None, metavar="TIME"))
    _call_foreign(lambda: show.add_argument("--budget", type=int, default=600))
    cli_plugins.add_format_option(show)
    stats = _call_foreign(lambda: nested.add_parser("stats", help=v.GRAPH_CLI_STATS_HELP))
    _call_foreign(lambda: stats.add_argument("--repo", default="."))
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
