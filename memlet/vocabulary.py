"""Shared user-facing vocabulary for Dream, accepted anchors and evaluation."""
APPROVED, HINT, PENDING, REJECTED = "approved", "hint", "pending", "rejected"
TIERS = (APPROVED, HINT, PENDING)
SUGGESTED_ANCHOR = "[suggested anchor]"
SINGLE_DEFINITION_FOUND = "single-definition-found"
FILE_SYMBOL = "file+symbol"
PROSPECTIVE, RETROSPECTIVE = "prospective", "retrospective"
PROTOCOLS = (PROSPECTIVE, RETROSPECTIVE)
VETO_PREFIX = "vetoed by "
VETO_TEMPLATE = VETO_PREFIX + "%s/%s: %s"
HINT_LABEL = "Unreviewed hints — not approved by a person"
HINT_HELP = ("Agreement basis is labelled per item; citations check out, but verify in code before relying on them. "
             "Screened against a list of permission phrases, never shown among verified items; "
             "the phrase list is not a proof that a hint cannot loosen a rule. "
             "A trusted non-SUPPORTED verdict permanently vetoes the hint tier for that content; human approval still wins.")
VERDICT_HELP = {
    "SUPPORTED": "The cited evidence supports the lesson and its relation.",
    "MISSING_CONDITION": "The lesson omits a condition required by the cited evidence.",
    "OUT_OF_EVIDENCE": "The lesson makes a claim outside the cited evidence.",
    "WRONG_EDGE": "The cited evidence does not support the proposed relation.",
}
MODEL_AGREEMENT = "two models of the same vendor agreed"
VENDOR_AGREEMENT = "two vendors agreed"
SAME_VENDOR_REVIEW = ('a second model of the same vendor checked it; review aid, not a hint under '
                      'dream.agreement: "vendor"')
VERIFICATION_SUMMARY = "Second-model checks: %d item(s) checked; %d became hints; %d remain pending."
VENDOR_REVIEW_SUMMARY = ('Same-vendor checks are a review aid under dream.agreement: "vendor"; '
                         'they cannot make hints under the current setting.')
BRIDGE_RULE = "rule"
BRIDGE_RULE_STATES = ("active", "compatible-unverified", "stale", "unpinned", "path-exists", "unanchored")
BRIDGE_SUGGESTED_ANCHOR = "suggested-anchor"
BRIDGE_RELATIONS = ("SUPERSEDES", "BLOCKS", "RESOLVES", "GOVERNS")
BRIDGE_BLOCKED_REASON = "approved BLOCKS lesson"
BRIDGE_REPLACED_REASON = "replaced — do not apply as current"
BRIDGE_TRUNCATED = " [truncated; see source]"
JUDGE_UNAVAILABLE = ("Only claude can currently be launched, so a judge from a different vendor cannot exist in practice; "
                     "keyed questions are scored deterministically; judged scoring needs a second vendor with a tool-free mode.")


def suggested_label(item):
    """Only per-rule provenance counts; aggregate sidecarIds alone never label a written rule."""
    return SUGGESTED_ANCHOR + " " if (item.get("suggestedAnchors") or item.get("suggested") or
        any(isinstance(a, dict) and a.get("suggested") for a in item.get("anchors", []))) else ""

LESSON_SEPARATOR = " ⟶ "
# Schema: 3 source spans + 1 replacement + 4 edges with 2 endpoints, each at most 4 lines.
MAX_LESSON_LINES = 48

# 9.2 graph
GRAPH_WHY = ("anchor-in-file", "anchor-symbol", "lesson-path", "lesson-cites-path", "replaced-rule-text")
GRAPH_HEADINGS = {
    "governs": "Governs this code",
    "blocks": "Approved dead ends — do not repeat",
    "resolves": "Approved fixes",
    "replaced": "Replaced instructions — do not apply as current",
    "stale": "Anchors requiring re-verification — do not apply as governing",
    "hints": HINT_LABEL,
}
GRAPH_TIME_NOTICE = (
    "Times come only from retained pin pinnedAt fields, accepted-anchor ledger events, "
    "Dream approval records and decision events. 9.1.1 writes no pin timestamp or rule creation time; "
    "anchor_snapshots.updated_at is the latest observation, not the first stale observation. "
    "Hint receipts are dated but activation of the agreement policy is not, so hint start times are unknown. "
    "Unknown times are null. Historical queries exclude unknown-since evidence and re-check current source bytes; "
    "overwritten pins, deleted sidecar payloads and changed citation contents cannot be reconstructed. "
    "Historical anchors must still be active now; undated present drift is not projected into the past."
)
GRAPH_HISTORY_NOTICE = (
    "Historical hints are omitted: the repository does not retain changes to the hint agreement policy. "
    "Without decision events, only the retained approval can be dated; overwritten decisions cannot be reconstructed."
)
GRAPH_OMITTED = "Omitted for budget: "
