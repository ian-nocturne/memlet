"""Explicit Dream CLI; importing/registering never launches a vendor."""
import json
from memlet.cli_plugins import add_format_option
from memlet.memlet_dreamcycle import dream_ingest, dream_run, dream_status

COMMANDS = {"dream": {"help": "Propose cited lessons with an explicit vendor, or inspect waiting sessions", "product": True}}


def register(sub):
    parser = sub.add_parser("dream", help=COMMANDS["dream"]["help"])
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--via", help="Comma-separated proposer vendors (claude,codex,gemini)")
    parser.add_argument("--verify-with", choices=("claude", "codex", "gemini"), help="Verifier model: dream.models.verifier.<vendor>; same vendor needs dream.agreement=model and distinct models")
    parser.add_argument("--transcript")
    parser.add_argument("--max-batches", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    add_format_option(parser)


def _markdown(result):
    if not result["ok"]:
        return "## Dream failed\n\n- ERROR: " + result["error"] + "\n"
    if "waiting" in result:
        counts = result.get("counts", {})
        text = "## Dream status\n\n- %s session(s) waiting to dream\n" % result["waiting"]
        text += "- Approved: %s; unreviewed hints: %s; pending: %s\n" % tuple(counts.get(k, 0) for k in ("approved", "hint", "pending"))
    else:
        text = "## Dream %s\n\n- Batches: %s; calls: %s; failed batches: %s\n" % (
            "preview" if result.get("dryRun") else "run", result.get("batches", 0), result.get("calls", 0), result.get("failedBatches", 0))
        if result.get("dryRun"):
            text += "- Planned proposer calls: %s; existing verifier calls: %s\n" % (result.get("plannedProposalCalls", 0), result.get("plannedExistingVerificationCalls", 0))
            for prompt in result.get("prompts", []):
                text += "\n### %s / %s / %s\n\n" % (prompt["kind"], prompt["vendor"], prompt["model"])
                text += "\n".join("    " + line for line in (prompt["prompt"] + "\n\n" + prompt["stdinText"]).splitlines()) + "\n"
    text += "\n" + result.get("trustBoundary", "") + "\n"
    text += "".join("\n- " + notice + "\n" for notice in result.get("notices", []))
    return text


def dispatch(args):
    if args.status:
        if args.via or args.transcript or args.verify_with:
            result = {"ok": False, "exitCode": 2, "error": "--status cannot be combined with proposal options"}
        else:
            result = dream_status(args.repo)
    elif not args.via:
        result = {"ok": False, "exitCode": 2, "error": "Choose --via VENDOR explicitly, or --status"}
    else:
        result = dream_run(args.repo, via=tuple(v.strip() for v in args.via.split(",")), verify_with=args.verify_with,
                           max_batches=args.max_batches, dry_run=args.dry_run, transcript=args.transcript)
    result["command"] = "dream"
    result.setdefault("exitCode", 0 if result["ok"] else 2)
    if not result["ok"]:
        result.setdefault("error", "; ".join(result.get("errors", [])) or "Dream failed")
    result["formatted"] = _markdown(result)
    return result
