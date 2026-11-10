"""Verdict logic for the Memlet Check GitHub Action (`action.yml`).

The Action used to carry its own pass/fail rules inline, and they had drifted from what the tool reports elsewhere:
a rescue result of `ENV_UNVERIFIED` (the normal state in CI, where no `.env` file exists) and an anti-slop scan of a
repository without UI files both failed the job. This module is the single place that turns gate results into a
verdict.

The `evaluate_*` functions, `combine`, `evaluate` and `render_report` are pure: result dicts in, plain dicts/strings
out, no I/O. `run_action` is the thin I/O wrapper the Action calls (`python -m prism_memory.action_verdict`).

Rules
- audit: unchanged. Error findings (or `ok: false`) block as drift; zero scanned instruction docs and statically
  unverifiable dynamic commands block as UNVERIFIED.
- rescue: `unverified_env_key` entries (the ENV_UNVERIFIED condition itself) are warnings; every other listed blocker
  blocks, whatever its severity (unchanged). With no such blocker, `STATIC_CHECKS_PASSED` passes and
  `ENV_UNVERIFIED` is a warning; `UNVERIFIED`, `NO_TARGETS_FOUND`, `BLOCKED` and `STATIC_CHECKS_WITH_WARNINGS` block.
- anti-slop: findings or a slop score >= 20 block (unchanged). "No UI or copy files" is a warning.
- Fail closed: a result is `MALFORMED_RESULT` (a blocker naming the field, exit 2 even when `fail-on-drift` is false,
  because the checker itself is broken rather than the repository drifted) when it is missing or not a dict, lacks
  a verdict field, carries a grade/verdict/readiness/severity outside the allowlists, has a score or count outside
  its domain, lists a finding or blocker without its identifying fields, or contradicts itself (summary counts vs
  the detail list, `ok` vs the counts, top-level vs `summary` readiness, readiness vs the blockers, grade vs score).
  Every such check runs before any warning shortcut.
- Inputs: `fail-on-drift` accepts only `true`/`false` (empty = `true`); anything else is a configuration error
  (exit 2). The SARIF file is never written through a symlink: a symlink destination is refused (exit 2) and the
  file is written to a temporary file in the same directory and moved into place atomically.

A warning never produces status `PASS`: a run with warnings and no blockers exits 0 with status `UNVERIFIED`.
"""

import json
import os
import pathlib
import tempfile
import sys
from typing import Any, Dict, List, Mapping, Optional, Tuple

VALID_MODES = ("audit", "rescue", "anti-slop", "all")
VALID_FORMATS = ("markdown", "json", "sarif")
MIN_PYTHON = (3, 9)

RESCUE_PASSED = "STATIC_CHECKS_PASSED"
RESCUE_ENV_UNVERIFIED = "ENV_UNVERIFIED"
RESCUE_ENV_KEY_KIND = "unverified_env_key"
RESCUE_KNOWN_READINESS = (
    "STATIC_CHECKS_PASSED",
    "STATIC_CHECKS_WITH_WARNINGS",
    "ENV_UNVERIFIED",
    "UNVERIFIED",
    "NO_TARGETS_FOUND",
    "BLOCKED",
)
SLOP_UNVERIFIED_GRADE = "UNVERIFIED"
SLOP_NO_FILES_VERDICT = "NO_UI_OR_COPY_FILES_FOUND"
SLOP_SCORE_LIMIT = 20
# grade -> the only verdict the scanner pairs with it, and the score band each grade covers (upper bound inclusive)
SLOP_GRADE_VERDICTS = {
    "A+": "STATIC_STYLE_RULES_PASSED",
    "A": "CLEAN_MINOR_POLISH",
    "B": "MODERATE_AI_TRACES",
    "C": "VISIBLE_AI_SLOP",
    "D": "HEAVY_AI_SLOP",
    "F": "UNMISTAKABLE_TEMPLATE_SLOP",
    "UNVERIFIED": "NO_UI_OR_COPY_FILES_FOUND",
}
SLOP_GRADE_BANDS = ((0, "A+"), (12, "A"), (28, "B"), (50, "C"), (75, "D"))
AUDIT_SEVERITIES = ("error", "warning")
RESCUE_SEVERITIES = ("critical", "high", "warning")

# Keys holding pre-rendered text; left out of the JSON report so it stays a data document.
_RENDERED_KEYS = ("rescueBriefMarkdown", "onePromptRescueMarkdown", "markdownReport", "patchPromptMarkdown", "formatted", "sarif")


def _item(gate: str, code: str, message: str) -> Dict[str, str]:
    return {"gate": gate, "code": code, "message": message}


def _gate(gate: str) -> Dict[str, Any]:
    return {
        "gate": gate,
        "blockers": [],
        "warnings": [],
        "drift": False,
        "malformed": False,
        "blocking_count": 0,
        "total_count": 0,
        "slop_score": 0,
    }


def _fail_closed(gate: str, message: str) -> Dict[str, Any]:
    out = _gate(gate)
    out["blockers"].append(_item(gate, "MALFORMED_RESULT", f"{gate}: {message}; failing closed."))
    out["malformed"] = True
    out["blocking_count"] = 1
    return out


def _as_int(value: Any) -> Optional[int]:
    """Counts must be real integers (or integer strings). bool and None are rejected so a missing field never reads as 0."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _count_field(result: Mapping[str, Any], key: str, required: bool = True) -> Tuple[Optional[int], Optional[str]]:
    """Read a non-negative integer count. Returns `(value, error)`; an absent optional field is `(None, None)`."""
    if key not in result or result.get(key) is None:
        return (None, f"result has no integer `{key}` field") if required else (None, None)
    value = _as_int(result.get(key))
    if value is None or value < 0:
        return None, f"`{key}` is not a non-negative integer"
    return value, None


def _object_list(result: Mapping[str, Any], key: str) -> Optional[List[Mapping[str, Any]]]:
    items = result.get(key)
    if not isinstance(items, list) or any(not isinstance(i, Mapping) for i in items):
        return None
    return items


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def evaluate_audit(result: Any) -> Dict[str, Any]:
    gate = "audit"
    if not isinstance(result, Mapping):
        return _fail_closed(gate, "result is missing or not an object")
    ok = result.get("ok")
    if not isinstance(ok, bool):
        return _fail_closed(gate, "result has no boolean `ok` field")
    err_cnt, err = _count_field(result, "errorCount")
    if err:
        return _fail_closed(gate, err)
    docs, err = _count_field(result, "scannedDocsCount")
    if err:
        return _fail_closed(gate, err)
    findings = _object_list(result, "findings")
    if findings is None:
        return _fail_closed(gate, "result has no `findings` list of objects")
    dynamic, err = _count_field(result, "unverifiedDynamicCommandCount", required=False)
    if err:
        return _fail_closed(gate, err)
    dynamic = dynamic or 0

    # Shape of every finding, then summary counts against the detail list. A summary that says "0 errors" next
    # to an error-severity finding is a contradiction, never a pass.
    for idx, f in enumerate(findings):
        if f.get("severity") not in AUDIT_SEVERITIES:
            return _fail_closed(gate, f"`findings[{idx}].severity` is `{f.get('severity')}` (known: {', '.join(AUDIT_SEVERITIES)})")
        if not _nonempty_str(f.get("ruleId")):
            return _fail_closed(gate, f"`findings[{idx}]` has no `ruleId`")
    listed_errors = sum(1 for f in findings if f.get("severity") == "error")
    listed_warnings = len(findings) - listed_errors
    if err_cnt != listed_errors:
        return _fail_closed(gate, f"`errorCount` is {err_cnt} but `findings` lists {listed_errors} error finding(s)")
    warn_cnt, err = _count_field(result, "warningCount", required=False)
    if err:
        return _fail_closed(gate, err)
    if warn_cnt is not None and warn_cnt != listed_warnings:
        return _fail_closed(gate, f"`warningCount` is {warn_cnt} but `findings` lists {listed_warnings} warning finding(s)")
    for key in ("findingCount", "findingsCount"):
        total, err = _count_field(result, key, required=False)
        if err:
            return _fail_closed(gate, err)
        if total is not None and total != len(findings):
            return _fail_closed(gate, f"`{key}` is {total} but `findings` lists {len(findings)} finding(s)")
    if ok != (err_cnt == 0):
        return _fail_closed(gate, f"`ok` is {str(ok).lower()} but `errorCount` is {err_cnt}")

    out = _gate(gate)
    out["blocking_count"] = err_cnt
    out["total_count"] = len(findings)
    if err_cnt > 0:
        out["drift"] = True
        out["blockers"].append(_item(gate, "DRIFT_DETECTED", f"audit: {err_cnt} error finding(s) in instruction documents"))
    elif docs == 0:
        out["blockers"].append(_item(gate, "NO_INSTRUCTION_DOCS_FOUND", "audit: NO_INSTRUCTION_DOCS_FOUND"))
    if dynamic > 0:
        # Commands built from runtime expressions (e.g. `npm run ${{ matrix.script }}`) were not checked.
        out["blockers"].append(
            _item(gate, "DYNAMIC_COMMANDS_UNVERIFIED", f"audit: {dynamic} dynamic command(s) not verifiable statically")
        )
    return out


def evaluate_rescue(result: Any) -> Dict[str, Any]:
    gate = "rescue"
    if not isinstance(result, Mapping):
        return _fail_closed(gate, "result is missing or not an object")
    summary = result.get("summary")
    if summary is not None and not isinstance(summary, Mapping):
        return _fail_closed(gate, "`summary` is not an object")
    summary = summary or {}
    top_readiness = result.get("readiness")
    sum_readiness = summary.get("readiness")
    if top_readiness is None and sum_readiness is None:
        return _fail_closed(gate, "result has no `summary.readiness` field")
    for label, value in (("readiness", top_readiness), ("summary.readiness", sum_readiness)):
        if value is None:
            continue
        if not _nonempty_str(value):
            return _fail_closed(gate, f"`{label}` is not a string")
        if value not in RESCUE_KNOWN_READINESS:
            return _fail_closed(gate, f"unknown readiness `{value}` (known: {', '.join(RESCUE_KNOWN_READINESS)})")
    if top_readiness is not None and sum_readiness is not None and top_readiness != sum_readiness:
        return _fail_closed(gate, f"`readiness` is `{top_readiness}` but `summary.readiness` is `{sum_readiness}`")
    readiness = sum_readiness if sum_readiness is not None else top_readiness
    ok = result.get("ok")
    if not isinstance(ok, bool):
        return _fail_closed(gate, "result has no boolean `ok` field")
    blockers = _object_list(result, "blockers")
    if blockers is None:
        return _fail_closed(gate, "result has no `blockers` list of objects")

    for idx, b in enumerate(blockers):
        if not _nonempty_str(b.get("kind")):
            return _fail_closed(gate, f"`blockers[{idx}]` has no `kind`")
        if b.get("severity") not in RESCUE_SEVERITIES:
            return _fail_closed(gate, f"`blockers[{idx}].severity` is `{b.get('severity')}` (known: {', '.join(RESCUE_SEVERITIES)})")
        if b.get("kind") == RESCUE_ENV_KEY_KIND and b.get("severity") != "warning":
            return _fail_closed(gate, f"`blockers[{idx}]` is `{RESCUE_ENV_KEY_KIND}` with severity `{b.get('severity')}` (must be `warning`)")

    # `unverified_env_key` entries are how the bundle lists the ENV_UNVERIFIED condition itself (always severity
    # "warning"); they are reported as warnings. Every other entry is a real blocker and blocks as before.
    env_only = [b for b in blockers if b.get("kind") == RESCUE_ENV_KEY_KIND]
    real = [b for b in blockers if b.get("kind") != RESCUE_ENV_KEY_KIND]
    critical = sum(1 for b in blockers if b.get("severity") == "critical")
    crit_high = sum(1 for b in real if b.get("severity") in ("critical", "high"))

    # Summary counts against the detail list, then readiness against the blockers it was derived from. All of
    # this runs before the ENV_UNVERIFIED warning shortcut below.
    for key, expected in (("totalBlockers", len(blockers)), ("criticalCount", critical), ("warningCount", len(blockers) - critical)):
        value, err = _count_field(summary, key, required=False)
        if err:
            return _fail_closed(gate, "summary: " + err)
        if value is not None and value != expected:
            return _fail_closed(gate, f"`summary.{key}` is {value} but `blockers` gives {expected}")
    if ok != (readiness == RESCUE_PASSED):
        return _fail_closed(gate, f"`ok` is {str(ok).lower()} but readiness is `{readiness}`")
    if readiness == RESCUE_PASSED and blockers:
        return _fail_closed(gate, f"readiness is `{RESCUE_PASSED}` but `blockers` lists {len(blockers)} item(s)")
    if (critical > 0) != (readiness == "BLOCKED"):
        return _fail_closed(gate, f"readiness is `{readiness}` but `blockers` lists {critical} critical item(s)")
    if readiness == RESCUE_ENV_UNVERIFIED and not env_only:
        return _fail_closed(gate, f"readiness is `{RESCUE_ENV_UNVERIFIED}` but `blockers` lists no `{RESCUE_ENV_KEY_KIND}` item")
    if readiness == "STATIC_CHECKS_WITH_WARNINGS" and not blockers:
        return _fail_closed(gate, "readiness is `STATIC_CHECKS_WITH_WARNINGS` but `blockers` is empty")

    out = _gate(gate)
    out["total_count"] = len(blockers)
    env_warning = _item(
        gate,
        RESCUE_ENV_UNVERIFIED,
        f"rescue: ENV_UNVERIFIED - {len(env_only)} environment variable(s) referenced in code but no local .env file "
        "is present to verify them (expected in CI); not a failure",
    )
    if real:
        out["blocking_count"] = crit_high or len(real)
        out["drift"] = True
        out["blockers"].append(
            _item(gate, readiness, f"rescue: {len(real)} static blocker(s), {crit_high} critical/high (readiness {readiness})")
        )
        if env_only:
            out["warnings"].append(env_warning)
        return out
    if readiness == RESCUE_PASSED:
        return out
    if readiness == RESCUE_ENV_UNVERIFIED:
        out["warnings"].append(env_warning)
        return out
    out["blocking_count"] = 1
    out["blockers"].append(_item(gate, readiness, f"rescue: {readiness}"))
    return out


def _slop_grade_for(score: int) -> str:
    for limit, grade in SLOP_GRADE_BANDS:
        if score <= limit:
            return grade
    return "F"


def evaluate_anti_slop(result: Any) -> Dict[str, Any]:
    gate = "anti-slop"
    if not isinstance(result, Mapping):
        return _fail_closed(gate, "result is missing or not an object")
    grade = result.get("grade")
    if not _nonempty_str(grade):
        return _fail_closed(gate, "result has no `grade` field")
    if grade not in SLOP_GRADE_VERDICTS:
        return _fail_closed(gate, f"unknown grade `{grade}` (known: {', '.join(SLOP_GRADE_VERDICTS)})")
    verdict = result.get("verdict")
    if verdict != SLOP_GRADE_VERDICTS[grade]:
        return _fail_closed(gate, f"grade `{grade}` has unknown verdict `{verdict}` (expected `{SLOP_GRADE_VERDICTS[grade]}`)")
    ok = result.get("ok")
    if not isinstance(ok, bool):
        return _fail_closed(gate, "result has no boolean `ok` field")
    total, err = _count_field(result, "totalFindingCount")
    if err:
        return _fail_closed(gate, err)
    findings = _object_list(result, "findings")
    if findings is None:
        return _fail_closed(gate, "result has no `findings` list of objects")
    for idx, f in enumerate(findings):
        if not _nonempty_str(f.get("category")):
            return _fail_closed(gate, f"`findings[{idx}]` has no `category`")
    if len(findings) > total or (result.get("truncated") is not True and len(findings) != total):
        return _fail_closed(gate, f"`totalFindingCount` is {total} but `findings` lists {len(findings)} finding(s)")
    for key, expected in (("findingCount", total), ("returnedFindingCount", len(findings))):
        value, err = _count_field(result, key, required=False)
        if err:
            return _fail_closed(gate, err)
        if value is not None and value != expected:
            return _fail_closed(gate, f"`{key}` is {value} but {expected} was expected from the other fields")

    out = _gate(gate)
    out["total_count"] = total
    if grade == SLOP_UNVERIFIED_GRADE:
        # "Nothing to scan" is a warning only when every field agrees that nothing was scanned.
        if result.get("slopScore") is not None:
            return _fail_closed(gate, f"grade is UNVERIFIED but `slopScore` is `{result.get('slopScore')}`")
        if total != 0:
            return _fail_closed(gate, f"grade is UNVERIFIED but `totalFindingCount` is {total}")
        if ok:
            return _fail_closed(gate, "grade is UNVERIFIED but `ok` is true")
        scanned, err = _count_field(result, "scannedFiles", required=False)
        if err:
            return _fail_closed(gate, err)
        if scanned:
            return _fail_closed(gate, f"grade is UNVERIFIED but `scannedFiles` is {scanned}")
        out["warnings"].append(
            _item(gate, SLOP_NO_FILES_VERDICT, "anti-slop: NO_UI_OR_COPY_FILES_FOUND - nothing to scan, gate skipped")
        )
        return out
    score = _as_int(result.get("slopScore"))
    if score is None:
        return _fail_closed(gate, "result has no integer `slopScore` field")
    if score < 0 or score > 100:
        return _fail_closed(gate, f"`slopScore` is {score}, outside 0-100")
    if _slop_grade_for(score) != grade:
        return _fail_closed(gate, f"`slopScore` {score} is grade `{_slop_grade_for(score)}` but `grade` is `{grade}`")
    if ok != (total == 0):
        return _fail_closed(gate, f"`ok` is {str(ok).lower()} but `totalFindingCount` is {total}")
    out["slop_score"] = score
    if score >= SLOP_SCORE_LIMIT or total > 0:
        out["drift"] = True
        out["blocking_count"] = total or 1
        out["blockers"].append(_item(gate, "SLOP_DETECTED", f"anti-slop: {total} finding(s), slop score {score} (grade {grade})"))
    return out


def parse_fail_on_drift(raw: Optional[str]) -> bool:
    """`true` / `false` only (case-insensitive, surrounding space ignored); missing or empty means the default `true`.

    Anything else raises ValueError: a typo such as `treu` must not silently turn failures off.
    """
    text = "" if raw is None else str(raw).strip().lower()
    if text in ("", "true"):
        return True
    if text == "false":
        return False
    raise ValueError(f"invalid fail-on-drift value '{raw}'. Use 'true' or 'false'.")


def combine(gates: List[Dict[str, Any]], fail_on_drift: bool = True) -> Dict[str, Any]:
    if not isinstance(fail_on_drift, bool):
        raise ValueError(f"fail_on_drift must be a bool, got {fail_on_drift!r}; parse inputs with parse_fail_on_drift().")
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    for g in gates:
        blockers += g["blockers"]
        warnings += g["warnings"]
    has_drift = any(g["drift"] for g in gates)
    malformed = any(g["malformed"] for g in gates)
    if malformed:
        exit_code = 2
    elif blockers and fail_on_drift:
        exit_code = 1
    else:
        exit_code = 0
    # Real findings win over UNVERIFIED; anything unverified (blocker or warning) is never reported as PASS.
    status = "DRIFT_DETECTED" if has_drift else ("UNVERIFIED" if (blockers or warnings) else "PASS")
    return {
        "blockers": blockers,
        "warnings": warnings,
        "exit_code": exit_code,
        "status": status,
        "finding_count": sum(g["blocking_count"] for g in gates),
        "total_finding_count": sum(g["total_count"] for g in gates),
        "slop_score": max([g["slop_score"] for g in gates] or [0]),
    }


def evaluate(mode: str, audit: Any = None, rescue: Any = None, anti_slop: Any = None, fail_on_drift: bool = True) -> Dict[str, Any]:
    """Verdict for one Action run. A gate that `mode` selects but that has no result fails closed."""
    if mode not in VALID_MODES:
        raise ValueError(f"invalid Memlet Check mode '{mode}'. Choose from {VALID_MODES}.")
    gates: List[Dict[str, Any]] = []
    if mode in ("audit", "all"):
        gates.append(evaluate_audit(audit))
    if mode in ("rescue", "all"):
        gates.append(evaluate_rescue(rescue))
    if mode in ("anti-slop", "all"):
        gates.append(evaluate_anti_slop(anti_slop))
    return combine(gates, fail_on_drift=fail_on_drift)


def format_verdict_markdown(verdict: Mapping[str, Any]) -> str:
    lines = ["## Memlet Check verdict", "", f"- **Status**: `{verdict['status']}` (exit code `{verdict['exit_code']}`)"]
    for b in verdict["blockers"]:
        lines.append(f"- **Blocker** (`{b['gate']}`): {b['message']}")
    for w in verdict["warnings"]:
        lines.append(f"- **Warning** (`{w['gate']}`): {w['message']}")
    return "\n".join(lines)


def _data_only(result: Any) -> Any:
    if not isinstance(result, Mapping):
        return result
    return {k: v for k, v in result.items() if k not in _RENDERED_KEYS}


def render_report(
    fmt: str,
    mode: str,
    verdict: Mapping[str, Any],
    audit: Any = None,
    rescue: Any = None,
    anti_slop: Any = None,
    audit_markdown: str = "",
    audit_sarif: Any = None,
) -> Tuple[str, str]:
    """Return `(report, step_summary)` in the format chosen by the `format` input.

    - markdown: the gate reports plus a verdict section; the step summary is the same text.
    - json: one JSON document (verdict plus each active gate's raw result); the step summary fences it.
    - sarif: the audit SARIF document (audit is the only gate with a SARIF form). Other active gates still count
      toward the verdict and appear, as Markdown, in the step summary only.
    """
    if fmt not in VALID_FORMATS:
        raise ValueError(f"invalid Memlet Check format '{fmt}'. Choose from {VALID_FORMATS}.")
    if mode not in VALID_MODES:
        raise ValueError(f"invalid Memlet Check mode '{mode}'. Choose from {VALID_MODES}.")
    with_audit = mode in ("audit", "all")
    with_rescue = mode in ("rescue", "all")
    with_slop = mode in ("anti-slop", "all")
    rescue_md = rescue.get("rescueBriefMarkdown", "") if with_rescue and isinstance(rescue, Mapping) else ""
    slop_md = anti_slop.get("markdownReport", "") if with_slop and isinstance(anti_slop, Mapping) else ""
    verdict_md = format_verdict_markdown(verdict)

    if fmt == "markdown":
        parts = [audit_markdown if with_audit else "", rescue_md, slop_md, verdict_md]
        text = "\n\n---\n\n".join(p for p in parts if p)
        return text, text

    if fmt == "json":
        doc: Dict[str, Any] = {"tool": "memlet-check", "mode": mode, "format": "json", "verdict": dict(verdict)}
        if with_audit:
            doc["audit"] = _data_only(audit)
        if with_rescue:
            doc["rescue"] = _data_only(rescue)
        if with_slop:
            doc["antiSlop"] = _data_only(anti_slop)
        text = json.dumps(doc, indent=2, ensure_ascii=False)
        return text, "```json\n" + text + "\n```"

    if not with_audit:
        raise ValueError(f"format 'sarif' needs the audit gate (mode 'audit' or 'all'); mode '{mode}' has no SARIF output.")
    if not isinstance(audit_sarif, Mapping):
        raise ValueError("format 'sarif' was requested but no SARIF document was produced by the audit gate.")
    text = json.dumps(audit_sarif, indent=2, ensure_ascii=False)
    summary_parts = ["Audit report emitted as SARIF v2.1.0 (see the job log and the `sarif-file` output path).", rescue_md, slop_md, verdict_md]
    return text, "\n\n---\n\n".join(p for p in summary_parts if p)


class OutputPathError(Exception):
    """The requested output path cannot be written safely."""


def check_output_path(path: str) -> pathlib.Path:
    """Refuse to write through a symlink or onto a non-file. Returns the path to write."""
    target = pathlib.Path(path)
    if target.is_symlink():
        raise OutputPathError(f"refusing to write SARIF to '{path}': it is a symbolic link. Remove the link or choose another sarif-file.")
    if target.exists() and not target.is_file():
        raise OutputPathError(f"refusing to write SARIF to '{path}': it exists and is not a regular file.")
    parent = target.parent
    if not parent.is_dir():
        raise OutputPathError(f"cannot write SARIF to '{path}': directory '{parent}' does not exist.")
    return target


def write_text_no_follow(path: str, text: str) -> None:
    """Write `text` to `path` without ever following a symlink at `path`.

    The content goes to a new temporary file (O_EXCL) in the same directory and is moved over the destination
    with `os.replace`, which replaces the directory entry itself: if a symlink appears there after the check,
    the link is replaced and its target is left untouched.
    """
    target = check_output_path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix="." + target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(text)
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp_name, 0o666 & ~umask)  # mkstemp creates 0600; match what a plain open() would have given
        os.replace(tmp_name, str(target))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _append(path: Optional[str], text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(text)


def run_action(env: Optional[Mapping[str, str]] = None, stdout: Any = None, stderr: Any = None) -> int:
    """Entry point for `action.yml`: read `INPUT_*`, run the selected gates, print the report, return the exit code."""
    env = os.environ if env is None else env
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr

    if sys.version_info < MIN_PYTHON:
        stderr.write(
            "::error title=Memlet Check::Python >= 3.9 is required, found %d.%d. Set the `python-version` input "
            "(for example '3.11') to let the Action install one.\n" % sys.version_info[:2]
        )
        return 2

    mode = env.get("INPUT_MODE", "audit").strip().lower() or "audit"
    if mode not in VALID_MODES:
        stderr.write(f"::error title=Memlet Check::invalid mode '{mode}'. Choose from {VALID_MODES}.\n")
        return 2
    fmt = env.get("INPUT_FORMAT", "markdown").strip().lower() or "markdown"
    if fmt not in VALID_FORMATS:
        stderr.write(f"::error title=Memlet Check::invalid format '{fmt}'. Choose from {VALID_FORMATS}.\n")
        return 2
    if fmt == "sarif" and mode not in ("audit", "all"):
        stderr.write(f"::error title=Memlet Check::format 'sarif' needs mode 'audit' or 'all'; mode '{mode}' has no SARIF output.\n")
        return 2

    repo_path = env.get("INPUT_REPO_PATH", ".").strip() or "."
    base_ref = env.get("INPUT_BASE_REF", "").strip() or None
    head_ref = env.get("INPUT_HEAD_REF", "HEAD").strip() or "HEAD"
    try:
        fail_on_drift = parse_fail_on_drift(env.get("INPUT_FAIL_ON_DRIFT"))
    except ValueError as exc:
        stderr.write(f"::error title=Memlet Check::{exc}\n")
        return 2
    sarif_file = env.get("INPUT_SARIF_FILE", "deltaharness-audit.sarif").strip()
    if sarif_file and mode in ("audit", "all"):
        try:
            check_output_path(sarif_file)  # fail before running the gates; the write below checks again
        except OutputPathError as exc:
            stderr.write(f"::error title=Memlet Check::{exc}\n")
            return 2

    audit_res: Any = None
    rescue_res: Any = None
    slop_res: Any = None
    audit_md = ""
    audit_sarif: Any = None

    if mode in ("audit", "all"):
        from prism_memory.audit import audit_repository, format_audit_markdown, format_audit_sarif

        audit_res = audit_repository(repo_path, base_ref=base_ref, head_ref=head_ref, output_format="json")
        audit_md = format_audit_markdown(audit_res)
        if sarif_file or fmt == "sarif":
            audit_sarif = format_audit_sarif(audit_res)
        if sarif_file:
            try:
                write_text_no_follow(sarif_file, json.dumps(audit_sarif, indent=2, ensure_ascii=False))
            except OutputPathError as exc:
                stderr.write(f"::error title=Memlet Check::{exc}\n")
                return 2
    if mode in ("rescue", "all"):
        from prism_memory.viberescue import build_rescue_bundle

        rescue_res = build_rescue_bundle(repo_path, write_files=False)
    if mode in ("anti-slop", "all"):
        from prism_memory.anti_slop import scan_anti_slop

        slop_res = scan_anti_slop(repo_path, max_findings=0)

    verdict = evaluate(mode, audit=audit_res, rescue=rescue_res, anti_slop=slop_res, fail_on_drift=fail_on_drift)
    report, summary = render_report(
        fmt, mode, verdict, audit=audit_res, rescue=rescue_res, anti_slop=slop_res, audit_markdown=audit_md, audit_sarif=audit_sarif
    )
    stdout.write(report + "\n")

    # Annotations go to stderr so stdout stays exactly the report in the chosen format.
    for b in verdict["blockers"]:
        stderr.write(f"::error title=Memlet Check::{b['message']}\n")
    for w in verdict["warnings"]:
        stderr.write(f"::warning title=Memlet Check::{w['message']}\n")

    _append(env.get("GITHUB_STEP_SUMMARY"), summary + "\n")
    _append(
        env.get("GITHUB_OUTPUT"),
        f"status={verdict['status']}\n"
        f"finding_count={verdict['finding_count']}\n"
        f"total_finding_count={verdict['total_finding_count']}\n"
        f"slop_score={verdict['slop_score']}\n",
    )
    return int(verdict["exit_code"])


if __name__ == "__main__":
    sys.exit(run_action())
