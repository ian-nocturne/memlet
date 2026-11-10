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


def dispatch(args):
    if args.status:
        if args.via or args.transcript or args.verify_with:
            return {"ok": False, "exitCode": 2, "formatted": "--status cannot be combined with proposal options"}
        result = dream_status(args.repo)
    elif not args.via:
        result = {"ok": False, "exitCode": 2, "error": "Choose --via VENDOR explicitly, or --status"}
    else:
        # Dry-run may ingest a specified transcript (snapshot only); it never calls auth or a model.
        if args.transcript:
            result = dream_ingest(args.repo, args.transcript)
            if not result["ok"]:
                return result
        result = dream_run(args.repo, via=tuple(v.strip() for v in args.via.split(",")), verify_with=args.verify_with,
                           max_batches=args.max_batches, dry_run=args.dry_run)
    result["command"] = "dream"
    result["formatted"] = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    return result
