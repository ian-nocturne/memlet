"""Explicit Dream CLI; importing/registering never launches a vendor."""
from . import vocabulary as v
from .vocabulary import (APPROVED as TIER_APPROVED, HINT as TIER_HINT, PENDING as TIER_PENDING)
import json
from memlet.cli_plugins import add_format_option
from memlet.memlet_dreamcycle import REJECTION_MEANINGS, dream_ingest, dream_run, dream_status, dream_skip_failed

COMMANDS = {"dream": {"help": "Propose cited lessons with an explicit vendor, or inspect waiting sessions", "product": True, "group": "Dream"}}


def register(sub):
    from memlet.memlet_dreamcycle import TRUST_BOUNDARY
    parser = sub.add_parser("dream", help=COMMANDS["dream"]["help"], epilog=TRUST_BOUNDARY)
    parser.add_argument("repo", nargs="?", default=".")
    parser.add_argument("--tidy", action="store_true", help=v.DREAM_TIDY_HELP)
    parser.add_argument("--via", help="Comma-separated proposer vendors (claude,codex,gemini)")
    parser.add_argument("--verify-with", choices=("claude", "codex", "gemini"), help="Verifier model: dream.models.verifier.<vendor>; distinct families can check within one vendor; dream.agreement=model is needed only for same-vendor hints")
    parser.add_argument("--transcript")
    parser.add_argument("--save-fixture", metavar="DIR", help="Explicitly save masked model answers outside .memlet (never overwrite)")
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dismiss", nargs="+", action="extend", metavar="PROPOSAL_ID", help=v.DREAM_TIDY_DISMISS_HELP)
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


def _dropped_groups_lines(result):
    lines = []
    for rec in result.get("droppedGroups", []):
        lessons = ", ".join(rec.get("lessonIds") or []) or "none"
        lines.append("- Dropped [%s]: %s (batch %s, group #%s; lessons: %s)" % (
            rec["reason"], rec.get("reasonMeaning", rec["reason"]),
            rec.get("batchId", "?"), rec.get("index", 0), lessons))
    if result.get("droppedGroupsOmitted"):
        lines.append("- %s more dropped group record(s) omitted by the count/byte bounds" % result["droppedGroupsOmitted"])
    return "\n".join(lines) + ("\n" if lines else "")


def _tidy_markdown(result):
    dropped = _dropped_groups_lines(result)
    total_dropped = len(result.get("droppedGroups", [])) + int(result.get("droppedGroupsOmitted", 0))
    if not result["ok"]:
        text = "## Dream failed\n\n"
        if total_dropped:
            text += "- Dropped groups: %s\n" % total_dropped + dropped + "\n"
        text += "- ERROR: " + result["error"] + "\n"
        if result.get("droppedOutput"):
            text += "- Dropped output: %s\n" % result["droppedOutput"]
        return text
    if "dismissed" in result:
        text = "## Dream tidy dismiss\n\n"
        text += "- Dismissed: %s\n" % (", ".join(result["dismissed"]) if result["dismissed"] else "none")
        if result.get("unchanged"):
            text += "- Already dismissed: %s\n" % ", ".join(result["unchanged"])
        return text
    if "counts" in result:
        counts = result.get("counts", {})
        text = "## Dream tidy status\n\n"
        text += "- Open: %s; disputed: %s; stale: %s; merged: %s; dismissed: %s\n" % (
            counts.get("open", 0), counts.get("disputed", 0), counts.get("stale", 0),
            counts.get("merged", 0), counts.get("dismissed", 0))
        for code, cnt in sorted((result.get("reasons") or {}).items()):
            meaning = v.DREAM_TIDY_STATE_REASONS.get(code, code)
            text += "- State reason [%s]: %s (%s)\n" % (code, cnt, meaning)
        text += "- Dropped groups: %s\n" % total_dropped
        if dropped:
            text += dropped
        text += "- Failed batches: %s\n" % result.get("failedBatches", 0)
        for fb in result.get("failedBatchRecords", []):
            text += "- Failed batch %s (%s/%s): %s — %s\n" % (
                fb["batchId"], fb.get("vendor", "?"), fb.get("model", "?"),
                fb.get("reason", "?"), fb.get("reasonMeaning", fb.get("reason", "?")))
        for item in result.get("proposals", []):
            tag = " [disputed]" if item.get("disputed") else ""
            ver = ("; verifier=%s/%s (%s)" % (item["verifier"]["vendor"], item["verifier"]["model"], item["verdict"])
                   if item.get("verifier") else "")
            reason_note = (" (%s)" % item["reason"]) if item.get("reason") else ""
            text += "- %s: state=%s%s%s; keep=%s; lessons=%s%s\n" % (
                item["id"], item["state"], reason_note, tag, item.get("keep", "?"),
                ",".join(item.get("lessonIds") or []), ver)
        text += "\n" + result.get("trustBoundary", "") + "\n"
        text += "".join("\n- " + notice + "\n" for notice in result.get("notices", []))
        return text

    text = "## Dream tidy %s\n\n" % ("preview" if result.get("dryRun") else "run")
    text += "- Batches: %s; calls: %s; failed batches: %s\n" % (
        result.get("batches", 0), result.get("calls", 0), result.get("failedBatches", 0))
    text += "- Proposals: %s (new: %s); unexamined lessons: %s\n" % (
        len(result.get("proposals", [])), len(result.get("addedProposals", [])), result.get("unexaminedLessons", 0))
    if result.get("dryRun"):
        v_range = result.get("verificationCallRange") or [0, 0]
        text += "- Planned proposer calls: %s; existing verifier calls: %s; verifier call range: %s..%s\n" % (
            result.get("plannedProposalCalls", 0), result.get("plannedExistingVerificationCalls", 0),
            v_range[0], v_range[1])
        for prompt in result.get("prompts", []):
            text += "\n### %s / %s / %s\n\n" % (prompt["kind"], prompt["vendor"], prompt["model"])
            text += "- Isolation: " + prompt["isolation"] + "\n- argv: " + json.dumps(prompt["argv"]) + "\n"
            text += "\n".join("    " + line for line in (prompt["prompt"] + "\n\n" + prompt["stdinText"]).splitlines()) + "\n"
        if result.get("verificationPrompt") and not any(p.get("kind") == "tidy-verify" for p in result.get("prompts", [])):
            text += "\n### tidy-verify / %s / %s\n\n" % (result["verifierVendor"], result["verifierModel"])
            text += "- Isolation: " + result["verifierIsolation"] + "\n- argv: " + json.dumps(result["verifierArgv"]) + "\n"
            text += "\n".join("    " + line for line in result["verificationPrompt"].splitlines()) + "\n"
    for bid in result.get("retryFailedBatches", []):
        text += "- Would retry failed batch: " + bid + "\n"
    text += "- Dropped groups: %s\n" % total_dropped
    if dropped:
        text += dropped
    if result.get("droppedOutput"):
        text += "- Dropped output: %s\n" % result["droppedOutput"]
    if result.get("verificationSummary"):
        text += "- " + result["verificationSummary"] + "\n"
    text += "\n" + result.get("trustBoundary", "") + "\n"
    text += "".join("\n- " + notice + "\n" for notice in result.get("notices", []))
    return text


def _markdown(result):
    if result.get("tidy"):
        return _tidy_markdown(result)
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
        for item in result.get("items", []):
            text += "- %s: tier=%s; %s\n" % (item["id"], item["tier"], item["agreementLabel"])
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
    if result.get("verificationSummary"):
        text += "- " + result["verificationSummary"] + "\n"
    if not rejections:
        text += "- Rejected candidates: 0\n"
    text += "\n" + result.get("trustBoundary", "") + "\n"
    text += "".join("\n- " + notice + "\n" for notice in result.get("notices", []))
    return text


def dispatch(args):
    tidy = bool(getattr(args, "tidy", False))
    dismiss = getattr(args, "dismiss", None)
    max_batches = getattr(args, "max_batches", None)
    if dismiss and not tidy:
        result = {"ok": False, "exitCode": 2, "error": v.DREAM_DISMISS_REQUIRES_TIDY}
    elif tidy:
        from memlet.memlet_dreamtidy import DEFAULT_MAX_BATCHES, dream_tidy_dismiss, dream_tidy_run, dream_tidy_status
        if args.transcript or args.skip_failed:
            result = {"ok": False, "exitCode": 2, "error": v.DREAM_TIDY_CONFLICT_ERROR, "tidy": True}
        elif dismiss:
            if (args.status or args.via or args.verify_with or args.dry_run
                    or args.save_fixture is not None or max_batches is not None):
                result = {"ok": False, "exitCode": 2, "error": v.DREAM_TIDY_DISMISS_CONFLICT, "tidy": True}
            else:
                result = dream_tidy_dismiss(args.repo, dismiss)
        elif args.status:
            if (args.via or args.verify_with or args.dry_run
                    or args.save_fixture is not None or max_batches is not None):
                result = {"ok": False, "exitCode": 2, "error": v.DREAM_TIDY_STATUS_CONFLICT, "tidy": True}
            else:
                result = dream_tidy_status(args.repo)
        elif not args.via:
            result = {"ok": False, "exitCode": 2, "error": v.DREAM_TIDY_MODE_ERROR, "tidy": True}
        else:
            options = {"save_fixture": args.save_fixture} if args.save_fixture is not None else {}
            batches_bound = DEFAULT_MAX_BATCHES if max_batches is None else max_batches
            result = dream_tidy_run(
                args.repo, **options,
                via=tuple(x.strip() for x in args.via.split(",")),
                verify_with=args.verify_with, max_batches=batches_bound, dry_run=args.dry_run)
    elif args.skip_failed:
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
        options = {"save_fixture": args.save_fixture} if args.save_fixture is not None else {}
        batches_bound = 4 if max_batches is None else max_batches
        result = dream_run(args.repo, **options, via=tuple(x.strip() for x in args.via.split(",")), verify_with=args.verify_with,
                           max_batches=batches_bound, dry_run=args.dry_run, transcript=args.transcript)
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
