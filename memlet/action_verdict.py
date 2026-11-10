"""Audit verdicts for the Memlet Check GitHub Action.

Malformed or contradictory checker results fail closed with exit 2, including
when fail-on-drift is false. SARIF writes refuse symbolic links and replace files
atomically. Report generation is pure; run_action handles the environment and I/O.
"""

import json
import os
import pathlib
import tempfile
import sys
from typing import Any, Dict, List, Mapping, Optional, Tuple

VALID_MODES = ("audit",)
VALID_FORMATS = ("markdown", "json", "sarif")
MIN_PYTHON = (3, 9)

AUDIT_SEVERITIES = ("error", "warning")

# Keys holding pre-rendered text; left out of the JSON report so it stays a data document.
_RENDERED_KEYS = ("formatted", "sarif")


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
    }


def validate_mode(mode: str) -> None:
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode '{mode}': this mode was removed in 9.0; use 'audit'.")


def evaluate(mode: str, audit: Any = None, fail_on_drift: bool = True) -> Dict[str, Any]:
    """Evaluate the sole audit gate; a missing result fails closed."""
    validate_mode(mode)
    return combine([evaluate_audit(audit)], fail_on_drift=fail_on_drift)


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
    audit_markdown: str = "",
    audit_sarif: Any = None,
) -> Tuple[str, str]:
    """Return (report, step summary) as Markdown, JSON, or SARIF."""
    validate_mode(mode)
    if fmt not in VALID_FORMATS:
        raise ValueError(f"invalid Memlet Check format '{fmt}'. Choose from {VALID_FORMATS}.")
    verdict_md = format_verdict_markdown(verdict)
    if fmt == "markdown":
        text = "\n\n---\n\n".join(p for p in (audit_markdown, verdict_md) if p)
        return text, text
    if fmt == "json":
        doc = {"tool": "memlet-check", "mode": mode, "format": "json",
               "verdict": dict(verdict), "audit": _data_only(audit)}
        text = json.dumps(doc, indent=2, ensure_ascii=False)
        return text, "```json\n" + text + "\n```"
    if not isinstance(audit_sarif, Mapping):
        raise ValueError("format 'sarif' was requested but no SARIF document was produced by the audit gate.")
    text = json.dumps(audit_sarif, indent=2, ensure_ascii=False)
    return text, "Audit report emitted as SARIF v2.1.0 (see the job log and the `sarif-file` output path).\n\n" + verdict_md


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
        stderr.write(f"::error title=Memlet Check::invalid mode '{mode}': this mode was removed in 9.0; use 'audit'.\n")
        return 2
    fmt = env.get("INPUT_FORMAT", "markdown").strip().lower() or "markdown"
    if fmt not in VALID_FORMATS:
        stderr.write(f"::error title=Memlet Check::invalid format '{fmt}'. Choose from {VALID_FORMATS}.\n")
        return 2

    repo_path = env.get("INPUT_REPO_PATH", ".").strip() or "."
    base_ref = env.get("INPUT_BASE_REF", "").strip() or None
    head_ref = env.get("INPUT_HEAD_REF", "HEAD").strip() or "HEAD"
    try:
        fail_on_drift = parse_fail_on_drift(env.get("INPUT_FAIL_ON_DRIFT"))
    except ValueError as exc:
        stderr.write(f"::error title=Memlet Check::{exc}\n")
        return 2
    sarif_file = env.get("INPUT_SARIF_FILE", "memlet-check.sarif").strip()
    if sarif_file:
        try:
            check_output_path(sarif_file)  # fail before running the gates; the write below checks again
        except OutputPathError as exc:
            stderr.write(f"::error title=Memlet Check::{exc}\n")
            return 2

    audit_res: Any = None
    audit_md = ""
    audit_sarif: Any = None

    from memlet.audit import audit_repository, format_audit_markdown, format_audit_sarif

    audit_res = audit_repository(repo_path, base_ref=base_ref, head_ref=head_ref, output_format="json")
    verdict = evaluate(mode, audit=audit_res, fail_on_drift=fail_on_drift)
    malformed = any(b["code"] == "MALFORMED_RESULT" for b in verdict["blockers"])
    audit_md = "Audit result is malformed; see verdict." if malformed else format_audit_markdown(audit_res)
    if sarif_file or fmt == "sarif":
        audit_sarif = {"version": "2.1.0", "runs": []} if malformed else format_audit_sarif(audit_res)
    if sarif_file:
        try:
            write_text_no_follow(sarif_file, json.dumps(audit_sarif, indent=2, ensure_ascii=False))
        except OutputPathError as exc:
            stderr.write(f"::error title=Memlet Check::{exc}\n")
            return 2
    report, summary = render_report(
        fmt, mode, verdict, audit=audit_res, audit_markdown=audit_md, audit_sarif=audit_sarif
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
        f"total_finding_count={verdict['total_finding_count']}\n",
    )
    return int(verdict["exit_code"])


if __name__ == "__main__":
    sys.exit(run_action())
