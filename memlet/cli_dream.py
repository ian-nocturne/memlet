"""Explicit Dream CLI; importing/registering never launches a vendor."""
from .vocabulary import (APPROVED as TIER_APPROVED, HINT as TIER_HINT, PENDING as TIER_PENDING)
import json
from memlet.cli_plugins import add_format_option
from memlet.memlet_dreamcycle import REJECTION_MEANINGS, dream_ingest, dream_run, dream_status, dream_skip_failed

COMMANDS = {"dream": {"help": "Propose cited lessons with an explicit vendor, or inspect waiting sessions", "product": True, "group": "Dream"}}


def register(sub):
    from memlet.memlet_dreamcycle import TRUST_BOUNDARY
    parser = sub.add_parser("dream", help=COMMANDS["dream"]["help"], epilog=TRUST_BOUNDARY)
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--via", help="Comma-separated proposer vendors (claude,codex,gemini)")
    parser.add_argument("--verify-with", choices=("claude", "codex", "gemini"), help="Verifier model: dream.models.verifier.<vendor>; same vendor needs dream.agreement=model and distinct models")
    parser.add_argument("--transcript")
    parser.add_argument("--max-batches", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--skip-failed", metavar="BATCH_ID", help="Record a failed batch as completed-with-rejections; no model call")
    add_format_option(parser)


def _rejections(result):
    lines = []
    for item in result.get("rejections", []):
        lines.append("- Rejected [%s]: %s. %s (%s; %s; cites %s): %s" % (
            item["reasonCode"], REJECTION_MEANINGS.get(item["reasonCode"], REJECTION_MEANINGS["invalid_candidate"]),
            item["title"], item["category"], ", ".join(item["edgeTypes"]),
            ", ".join(item["spans"]), item.get("note", "")))
    if result.get("rejectionsOmitted"):
        lines.append("- %s more rejection record(s) omitted by the count/byte bounds" % result["rejectionsOmitted"])
    return "\n".join(lines) + ("\n" if lines else "")


def _rejection_summary(result):
    if not result.get("rejectedCandidates"):
        return ""
    text = "- Rejected candidates: %s\n" % result["rejectedCandidates"]
    text += _rejections(result)
    for run in result.get("runs", []):
        if run.get("rejectedCandidates"):
            text += "- Run %s (%s):\n" % (run["runId"], run["lane"])
            text += _rejections(run)
    return text + "\n"


def _markdown(result):
    rejections = _rejection_summary(result)
    if not result["ok"]:
        text = "## Dream failed\n\n" + rejections + "- ERROR: " + result["error"] + "\n"
        if result.get("droppedOutput"):
            text += "- Dropped output: %s\n" % result["droppedOutput"]
        return text
    if "waiting" in result:
        counts = result.get("counts", {})
        text = "## Dream status\n\n" + rejections + "- %s session(s) waiting to dream\n" % result["waiting"]
        text += "- Approved: %s; unreviewed hints: %s; pending: %s\n" % tuple(counts.get(k, 0) for k in (TIER_APPROVED, TIER_HINT, TIER_PENDING))
        text += "- Failed batches: %s\n" % result.get("failedBatches", 0)
        for run in result.get("runs", []):
            text += "- [%s] %s: %s completed; %s failed\n" % (run["lane"], run["runId"], run["completedBatches"], run["failedBatches"])
    else:
        text = "## Dream %s\n\n" % ("preview" if result.get("dryRun") else "run")
        text += rejections + "- Batches: %s; calls: %s; failed batches: %s\n" % (
            result.get("batches", 0), result.get("calls", 0), result.get("failedBatches", 0))
        if result.get("dryRun"):
            text += "- Planned proposer calls: %s; existing verifier calls: %s\n" % (result.get("plannedProposalCalls", 0), result.get("plannedExistingVerificationCalls", 0))
            for prompt in result.get("prompts", []):
                text += "\n### %s / %s / %s\n\n" % (prompt["kind"], prompt["vendor"], prompt["model"])
                text += "- Isolation: " + prompt["isolation"] + "\n- argv: " + json.dumps(prompt["argv"]) + "\n"
                text += "\n".join("    " + line for line in (prompt["prompt"] + "\n\n" + prompt["stdinText"]).splitlines()) + "\n"
    for bid in result.get("retryFailedBatches", []):
        text += "- Would retry failed batch: " + bid + "\n"
    for run in [result] + result.get("runs", []):
        for entry in run.get("skippedBatches", []):
            text += "- Batch %s: completed-with-rejections at %s\n" % (entry["batchId"], entry["at"])
    if result.get("droppedOutput"):
        text += "- Dropped output: %s\n" % result["droppedOutput"]
    if not rejections:
        text += "- Rejected candidates: 0\n"
    text += "\n" + result.get("trustBoundary", "") + "\n"
    text += "".join("\n- " + notice + "\n" for notice in result.get("notices", []))
    return text


def dispatch(args):
    if args.skip_failed:
        if args.status or args.via or args.transcript or args.verify_with:
            result = {"ok": False, "exitCode": 2, "error": "--skip-failed cannot be combined with proposal/status options"}
        else:
            result = dream_skip_failed(args.repo, args.skip_failed, dry_run=args.dry_run)
    elif args.status:
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
    if result.get("rejectedCandidates"):
        records = result.get("rejections", []) + [r for run in result.get("runs", []) for r in run.get("rejections", [])]
        reasons = {r["reasonCode"]: REJECTION_MEANINGS.get(r["reasonCode"], REJECTION_MEANINGS["invalid_candidate"]) for r in records}
        # Insertion order puts counts and plain-language reasons first in CLI JSON.
        result = {"rejectedCandidates": result["rejectedCandidates"], "rejectionSummary": reasons, **result}
    result["formatted"] = _markdown(result)
    return result
