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
             "A trusted non-SUPPORTED verdict permanently vetoes the hint tier for that content; human approval still wins")
MODEL_AGREEMENT = "two models of the same vendor agreed"
VENDOR_AGREEMENT = "two vendors agreed"
JUDGE_UNAVAILABLE = ("Only claude can currently be launched, so a judge from a different vendor cannot exist in practice; "
                     "keyed questions are scored deterministically; judged scoring needs a second vendor with a tool-free mode.")


def suggested_label(item):
    """Only per-rule provenance counts; aggregate sidecarIds alone never label a written rule."""
    return SUGGESTED_ANCHOR + " " if (item.get("suggestedAnchors") or item.get("suggested") or
        any(isinstance(a, dict) and a.get("suggested") for a in item.get("anchors", []))) else ""

LESSON_SEPARATOR = " ⟶ "
# Schema: 3 source spans + 1 replacement + 4 edges with 2 endpoints, each at most 4 lines.
MAX_LESSON_LINES = 48
