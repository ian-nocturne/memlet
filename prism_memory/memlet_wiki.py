"""Memlet zero-daemon onboarding (`memlet init`) and AST-anchored Markdown wiki (`memlet wiki`).

`memlet init [path] [--dry-run]` inspects a repository and (unless `--dry-run`) creates
`.memlet/config.json`, `.memlet/wiki/INDEX.md` and the local ledger `.memlet/ledger.sqlite3`,
pinning every `file::Symbol` anchor found in agent instruction files (`CLAUDE.md`, `AGENTS.md`, ...)
to its current AST, full-signature and required-signature hash.

`memlet wiki [path] [--dry-run]` reads every Markdown page under `.memlet/wiki/` (human edits
included), re-resolves each anchor against the current code, rewrites the generated status block of
`INDEX.md` and a bounded per-page status header on every other page, and records pages whose body
changed since the last recorded hash as `user_edit` events in the ledger. Anchor states:

- `active`: symbol resolves and its pinned AST hash still matches.
- `unpinned`: symbol resolves, but no stored baseline exists.
- `path-exists`: the referenced path exists; no symbol hash was compared.
- `compatible-unverified`: the scanner is approximate, or the body changed while the pinned
  call signature is unchanged, only optional parameters were added (required signature unchanged),
  or its language has no structural extractor.
- `stale`: file or symbol is gone, the file no longer parses, or the required signature changed.

`memlet import [path] [--from auto|claude-md|cursorrules|ai-memory|claude-mem] [--dry-run]` pulls rules
from existing instruction / memory files into `.memlet/wiki/imported-rules.md`, tagging every rule with
the live state of the code it references. `memlet handover` builds a checksummed, budgeted (<500 token)
handover capsule that carries only verified constraints and recorded dead-ends to the next model.
`run_refactoring_comparison_benchmark()` (`memlet benchmark`) replays four refactoring scenarios in a
temporary directory and compares raw Markdown, an unverified wiki, and Memlet.

Command output is deterministic (ledger rows carry UTC timestamps, reports do not); no command
starts a daemon, calls a network or LLM, or opens `.env*` files or other secret material.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .audit import BACKTICK_FILE_RE, EXCLUDED_DIRS, EXPLICIT_ANCHOR_RE, IGNORED_FILE_TOKENS, audit_repository
from .code_anchors import extract_file_symbol_hashes, extract_file_symbol_signatures

MEMLET_VERSION = "8.4.0"
CONFIG_SCHEMA_VERSION = 1
MEMLET_DIR = ".memlet"
WIKI_DIR = ".memlet/wiki"
CONFIG_PATH = ".memlet/config.json"
INDEX_PATH = ".memlet/wiki/INDEX.md"
LEDGER_PATH = ".memlet/ledger.sqlite3"
ANCHOR_STATES = ("active", "compatible-unverified", "stale", "path-exists", "unpinned")

STATUS_BEGIN = "<!-- memlet:status:begin -->"
STATUS_END = "<!-- memlet:status:end -->"
PAGE_STATUS_BEGIN = "<!-- memlet-page-status:start -->"
PAGE_STATUS_END = "<!-- memlet-page-status:end -->"
PIN_RE = re.compile(
    r"<!--\s*memlet:anchor\s+(?P<path>[^\s:]+)::(?P<symbol>[A-Za-z_$][\w.$-]*)"
    r"\s+ast=(?P<ast>[0-9a-f]{6,64})(?:\s+sig=(?P<sig>[0-9a-f]{6,64}))?(?:\s+req=(?P<req>[0-9a-f]{6,64}))?(?:\s+basis=(?P<basis>[A-Za-z0-9_.-]+))?\s*-->"
)
_STATUS_BLOCK_RE = re.compile(r"<!-- memlet:status:(?:begin|start) -->.*?" + re.escape(STATUS_END), re.S)
_PAGE_STATUS_RE = re.compile(re.escape(PAGE_STATUS_BEGIN) + r".*?" + re.escape(PAGE_STATUS_END) + r"\n?", re.S)
_LEDGER_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS anchor_snapshots (anchor TEXT PRIMARY KEY, path TEXT, symbol TEXT, status TEXT,"
    " ast_hash TEXT, sig_hash TEXT, req_sig_hash TEXT, updated_at TEXT)",
    "CREATE TABLE IF NOT EXISTS wiki_events (id INTEGER PRIMARY KEY AUTOINCREMENT, page TEXT, content_sha256 TEXT,"
    " event_type TEXT, recorded_at TEXT)",
)
_HASH_LEN = 16

# Agent instruction files (rules agents actually load), as opposed to general docs.
TOP_INSTRUCTION_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
    ".github/copilot-instructions.md",
)
INSTRUCTION_GLOBS = (".cursor/rules/*.md", ".cursor/rules/*.mdc", ".github/instructions/*.md")
NESTED_INSTRUCTION_NAMES = ("CLAUDE.md", "AGENTS.md", "SKILL.md")
_SCRATCH_DIRS = {"_workspace", "worktrees", ".worktrees"}


def _is_secret_path(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].lower()
    return name.startswith(".env") or name.endswith((".pem", ".key")) or name in ("id_rsa", "id_ed25519", ".npmrc", ".pypirc", ".netrc")


def _rel(root: Path, p: Path) -> str:
    return str(p.relative_to(root)).replace("\\", "/")


def _git_head(root: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", sha) else None


def discover_instruction_files(root: Path, exclude_snapshots: bool = False) -> List[str]:
    """Return sorted repo-relative paths of agent instruction files (never `.env*`).

    exclude_snapshots=True also drops copies kept under snapshot folders, matched by whole folder name only
    (runs, outputs, artifacts, archive(s), snapshot(s), tmp, cache, backup(s), bak, old, or names ending
    _backup/-bak/.old): they are old snapshots, not current instructions. Dated names such as backup_20261101 are
    NOT matched, and current folders like backup-service/ are kept. Default unchanged."""
    found = set()

    def _add(p: Path) -> None:
        try:
            if p.is_symlink() or not p.is_file():
                return
            rel = _rel(root, p)
        except (OSError, ValueError):
            return
        if not _is_secret_path(rel):
            found.add(rel)

    for rel in TOP_INSTRUCTION_FILES:
        _add(root / rel)
    for pat in INSTRUCTION_GLOBS:
        for p in root.glob(pat):
            _add(p)
    # Walk with pruning instead of rglob: skip excluded dirs, scratch worktree folders, and any nested git
    # repository/worktree (a sub-directory holding its own `.git`), whose instructions belong to that repo.
    names = set(NESTED_INSTRUCTION_NAMES)
    for cur, dirs, files in os.walk(root):
        rel_parts = Path(cur).relative_to(root).parts
        if rel_parts and (root / Path(*rel_parts) / ".git").exists():
            dirs[:] = []
            continue
        if len(rel_parts) >= 3:  # same depth as before: files at most 3 directories below the root
            dirs[:] = []
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and d != MEMLET_DIR and d not in _SCRATCH_DIRS]
        for f in files:
            if f in names:
                _add(Path(cur) / f)
    if exclude_snapshots:
        found = {f for f in found if not any(_SNAPSHOT_SEGMENT_RE.search(seg) for seg in f.split("/")[:-1])}
    return sorted(found)


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _extract_anchors(text: str, source: str) -> List[Dict[str, Any]]:
    """Find `path::Symbol` anchors and `memlet:anchor` pins in a Markdown document."""
    out: List[Dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for m in PIN_RE.finditer(line):
            out.append({"path": m.group("path"), "symbol": m.group("symbol"), "source": source,
                        "line": line_no, "pin": {"ast": m.group("ast"), "sig": m.group("sig"), "req": m.group("req"), "basis": m.group("basis")}})
        scrubbed = PIN_RE.sub("", line)
        for m in EXPLICIT_ANCHOR_RE.finditer(scrubbed):
            if "::" not in m.group(0):
                continue
            path = m.group("path")
            out.append({"path": path[2:] if path.startswith("./") else path,
                        "symbol": m.group("symbol").rstrip(".-"), "source": source, "line": line_no, "pin": None})
    return out


def _merge_anchors(raw: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for a in raw:
        key = (a["path"], a["symbol"])
        entry = merged.setdefault(key, {"path": a["path"], "symbol": a["symbol"], "pin": None, "sources": []})
        loc = f"{a['source']}:{a['line']}"
        if a["pin"] and entry["pin"] is None:
            entry["pin"] = a["pin"]
        if not a["pin"] and loc not in entry["sources"]:
            entry["sources"].append(loc)
    return [merged[k] for k in sorted(merged)]


def _req_sig_hash(sig: Dict[str, Any]) -> Optional[str]:
    """Hash of the required call contract only: adding a defaulted parameter leaves it unchanged."""
    if "reqArgs" not in sig:
        return None
    payload = {k: sig.get(k) for k in ("kind", "isAsync", "reqArgs", "kwonlyReq", "methods")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:_HASH_LEN]


def _current_fingerprint(root: Path, rel_path: str, symbol: str) -> Dict[str, Any]:
    """Resolve an anchor against the working tree; returns resolution plus current hashes."""
    if rel_path.startswith("/") or ".." in Path(rel_path).parts or _is_secret_path(rel_path):
        return {"resolution": "rejected", "reason": "path_outside_repo_or_secret"}
    target = root / rel_path
    try:
        target.resolve().relative_to(root)
    except (OSError, ValueError):
        return {"resolution": "rejected", "reason": "path_outside_repo_or_secret"}
    sig_info = extract_file_symbol_signatures(target)
    if not sig_info.get("__file_exists__"):
        return {"resolution": "missing", "reason": "missing_file"}
    resolution = sig_info.get("__resolution__")
    if resolution == "parse_error" or not sig_info.get("__syntax_ok__", True):
        return {"resolution": "parse_error", "reason": "syntax_error"}
    if resolution in ("unsupported", "oversized"):
        return {"resolution": resolution, "reason": "unsupported_language"}
    sig = (sig_info.get("symbols") or {}).get(symbol)
    if sig is None:
        return {"resolution": "missing", "reason": "missing_symbol"}
    ast_hash = (extract_file_symbol_hashes(target).get(symbol) or sig.get("normAstHash") or sig.get("sigHash") or "")
    return {
        "resolution": "exact",
        "basis": ("ts-js-lexical-v3" if sig_info.get("__language__") in ("typescript", "javascript")
                  else "python-ast-v2" if sig_info.get("__language__") == "python" else "contract-v1"),
        "hashExact": sig_info.get("__language__") not in ("typescript", "javascript"),
        "hashReason": "ts_js_lexical_scan_unverified",
        "ast": ast_hash[:_HASH_LEN],
        "sig": (sig.get("sigHash") or "")[:_HASH_LEN],
        "req": _req_sig_hash(sig),
    }


def _same_hash(a: Optional[str], b: Optional[str]) -> bool:
    n = min(len(a or ""), len(b or ""), _HASH_LEN)
    return n >= 6 and a[:n] == b[:n]


def classify_anchor(root: Path, anchor: Dict[str, Any]) -> Dict[str, Any]:
    cur = _current_fingerprint(root, anchor["path"], anchor["symbol"])
    pin = anchor.get("pin")
    legacy_match = False
    if pin and cur.get("ast") and not _same_hash(cur["ast"], pin.get("ast")) and Path(anchor["path"]).suffix.lower() == ".py":
        legacy_hash = extract_file_symbol_hashes(root / anchor["path"], legacy=True).get(anchor["symbol"])
        legacy_match = _same_hash(legacy_hash, pin.get("ast"))
    if cur["resolution"] in ("missing", "parse_error", "rejected"):
        status, reason = "stale", cur["reason"]
    elif cur["resolution"] != "exact":
        status, reason = "compatible-unverified", cur["reason"]
    elif not pin or not pin.get("ast"):
        status, reason = "unpinned", "no_stored_pin"
    elif _same_hash(cur["ast"], pin.get("ast")) or legacy_match:
        if cur.get("hashExact", True):
            status, reason = "active", "legacy_ast_hash_match" if legacy_match else "ast_hash_match"
        else:
            status, reason = "compatible-unverified", cur["hashReason"]
    elif not cur.get("hashExact", True) and pin.get("basis") in (None, "legacy"):
        status, reason = "compatible-unverified", "legacy_ts_basis_repin_required"
    elif not (pin.get("sig") and cur["sig"]) or _same_hash(cur["sig"], pin["sig"]):
        status, reason = "compatible-unverified", "body_changed_signature_compatible"
    elif pin.get("req") and _same_hash(cur.get("req"), pin["req"]):
        status, reason = "compatible-unverified", "optional_args_added_signature_compatible"
    else:
        status, reason = "stale", "signature_drift"
    return {
        "anchor": f"{anchor['path']}::{anchor['symbol']}",
        "path": anchor["path"],
        "symbol": anchor["symbol"],
        "status": status,
        "reason": reason,
        "pinned": bool(pin),
        "currentAstHash": cur.get("ast"),
        "currentSigHash": cur.get("sig"),
        "currentReqSigHash": cur.get("req"),
        "currentHashBasis": cur.get("basis"),
        "pinnedHashBasis": pin.get("basis") if pin else None,
        "sources": list(anchor.get("sources") or []),
    }


def _summarize(anchors: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {s: 0 for s in ANCHOR_STATES}
    for a in anchors:
        counts[a["status"]] += 1
    counts["total"] = len(anchors)
    return counts


def _migrate_legacy_comments(root: Path, text: str) -> str:
    """Rewrite only Python pins that still match the legacy AST basis."""
    def replace(m):
        anchor = {"path": m.group("path"), "symbol": m.group("symbol"),
                  "pin": {"ast": m.group("ast"), "sig": m.group("sig"), "req": m.group("req"), "basis": m.group("basis")}}
        result = classify_anchor(root, anchor)
        if result["reason"] != "legacy_ast_hash_match":
            return m.group(0)
        sig = f" sig={result['currentSigHash']}" if result.get("currentSigHash") else ""
        req = f" req={result['currentReqSigHash']}" if result.get("currentReqSigHash") else ""
        return f"<!-- memlet:anchor {result['anchor']} ast={result['currentAstHash']}{sig}{req} basis={result['currentHashBasis']} -->"
    return PIN_RE.sub(replace, text)


def _audit_summary(root: Path) -> Dict[str, Any]:
    try:
        rep = audit_repository(root)
    except Exception as exc:  # onboarding must not fail on an audit edge case
        return {"scannedDocsCount": 0, "referencesChecked": 0, "errorCount": 0, "warningCount": 0,
                "countsByRule": {}, "error": type(exc).__name__}
    return {
        "scannedDocsCount": rep.get("scannedDocsCount", 0),
        "referencesChecked": rep.get("referencesChecked", 0),
        "errorCount": rep.get("errorCount", 0),
        "warningCount": rep.get("warningCount", 0),
        "countsByRule": dict(sorted((rep.get("countsByRule") or {}).items())),
    }


def _status_block(anchors: List[Dict[str, Any]], summary: Dict[str, int]) -> str:
    lines = [
        STATUS_BEGIN,
        "## Anchor health",
        "",
        f"- active: {summary['active']} · compatible-unverified: {summary['compatible-unverified']} · stale: {summary['stale']} (total {summary['total']})",
        f"- Unverified observations: {summary['path-exists']} path-exists · {summary['unpinned']} unpinned",
        "",
    ]
    if anchors:
        lines += ["| Status | Anchor | Reason |", "| :--- | :--- | :--- |"]
        lines += [f"| `{a['status']}` | `{a['anchor']}` | {a['reason']} |" for a in anchors]
    else:
        lines.append("No `file::Symbol` anchors yet. Write one in any page, e.g. `src/app.py::main`.")
    lines.append(STATUS_END)
    return "\n".join(lines)


def _render_index(head: Optional[str], instruction_files: List[str], anchors: List[Dict[str, Any]],
                  classified: List[Dict[str, Any]], summary: Dict[str, int], audit: Dict[str, Any]) -> str:
    lines = [
        "# Memlet Wiki Index",
        "",
        "Generated by `memlet init`. Pages under `.memlet/wiki/` are plain Markdown: edit or add them freely,",
        "then run `memlet wiki` to re-check every `` `path::Symbol` `` anchor against the current code.",
        "Pins (`memlet:anchor` comments) record the AST and signature hash seen at pin time; delete a pin and",
        "run `memlet init --force` to re-pin after you have verified a change.",
        "",
        f"- Repository head: `{head}`" if head else "- Repository head: (not a git repository)",
        "- Instruction files: " + (", ".join(f"`{f}`" for f in instruction_files) if instruction_files else "(none found)"),
        f"- `memlet check`: {audit['scannedDocsCount']} docs, {audit['referencesChecked']} references, "
        f"{audit['errorCount']} errors, {audit['warningCount']} warnings",
        "",
        "## Code anchors",
        "",
    ]
    if not anchors:
        lines.append("(no anchors found in instruction files)")
    for a in anchors:
        key = f"{a['path']}::{a['symbol']}"
        src = ", ".join(f"`{s}`" for s in a["sources"])
        lines.append(f"- `{key}` (from {src})")
        if a["pin"]:
            sig = f" sig={a['pin']['sig']}" if a["pin"]["sig"] else ""
            req = f" req={a['pin']['req']}" if a["pin"].get("req") else ""
            lines.append(f"  <!-- memlet:anchor {key} ast={a['pin']['ast']}{sig}{req} basis={a['pin'].get('basis') or 'legacy'} -->")
    lines += ["", _status_block(classified, summary), ""]
    return "\n".join(lines)


def _page_header(counts: Dict[str, int]) -> str:
    observations = (f", path-exists={counts['path-exists']}, unpinned={counts['unpinned']}"
                    if counts.get("path-exists") or counts.get("unpinned") else "")
    return "\n".join([
        PAGE_STATUS_BEGIN,
        f"> **Memlet AST Status**: active={counts['active']}, compatible-unverified={counts['compatible-unverified']}, stale={counts['stale']}{observations}",
        PAGE_STATUS_END,
    ])


def _with_page_header(text: str, counts: Dict[str, int]) -> str:
    """Replace the page status header in place, or insert it after the `# Title` line (else at the top)."""
    block = _page_header(counts) + "\n"
    if _PAGE_STATUS_RE.search(text):
        return _PAGE_STATUS_RE.sub(lambda _m: block, text, count=1)
    first, _, rest = text.partition("\n")
    if first.startswith("# "):
        return first + "\n" + block + rest
    return block + text


def _page_body_sha256(text: str) -> str:
    """Hash of the human-owned body: generated status header/blocks are excluded."""
    body = _STATUS_BLOCK_RE.sub("", _PAGE_STATUS_RE.sub("", text))
    return hashlib.sha256(body.rstrip().encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger_ready(conn: sqlite3.Connection) -> None:
    for stmt in _LEDGER_SCHEMA:
        conn.execute(stmt)


def _last_page_hashes(conn: sqlite3.Connection) -> Dict[str, str]:
    rows = conn.execute(
        "SELECT page, content_sha256 FROM wiki_events WHERE id IN (SELECT MAX(id) FROM wiki_events GROUP BY page)"
    )
    return {page: sha for page, sha in rows}


def _record_page(conn: sqlite3.Connection, page: str, sha: str, event_type: str, now: str) -> None:
    conn.execute(
        "INSERT INTO wiki_events (page, content_sha256, event_type, recorded_at) VALUES (?, ?, ?, ?)",
        (page, sha, event_type, now),
    )


def _sync_snapshots(conn: sqlite3.Connection, classified: List[Dict[str, Any]], now: str) -> bool:
    """Upsert the latest observed state of every anchor; returns True when any row changed."""
    existing = {
        row[0]: tuple(row[1:])
        for row in conn.execute("SELECT anchor, path, symbol, status, ast_hash, sig_hash, req_sig_hash FROM anchor_snapshots")
    }
    changed = False
    for c in classified:
        row = (c["path"], c["symbol"], c["status"], c["currentAstHash"], c["currentSigHash"], c["currentReqSigHash"])
        if existing.get(c["anchor"]) != row:
            conn.execute("INSERT OR REPLACE INTO anchor_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (c["anchor"], *row, now))
            changed = True
    return changed


def _read_ledger_hashes(ledger: Path) -> Dict[str, str]:
    """Read-only view of the last recorded page hashes (used by `--dry-run`)."""
    if ledger.is_symlink() or not ledger.is_file():
        return {}
    try:
        with contextlib.closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as conn:
            return _last_page_hashes(conn)
    except sqlite3.Error:
        return {}


def _validated_config_pins(config):
    """Return usable pins and diagnostics for hand-edited config entries."""
    entries = config.get("pinnedAnchors", {})
    if not isinstance(entries, dict):
        return {}, [{"anchor": None, "reason": "pinnedAnchors_must_be_object"}]
    pins, skipped = {}, []
    for key, pin in entries.items():
        valid = isinstance(key, str) and "::" in key and isinstance(pin, dict)
        valid = valid and isinstance(pin.get("astHash"), str) and bool(re.fullmatch(r"[0-9a-f]{6,64}", pin["astHash"]))
        if valid:
            valid = all(pin.get(k) is None or (isinstance(pin[k], str) and bool(re.fullmatch(r"[0-9a-f]{6,64}", pin[k])))
                        for k in ("sigHash", "reqSigHash"))
            valid = valid and (pin.get("hashBasis") is None or
                              (isinstance(pin["hashBasis"], str) and bool(re.fullmatch(r"[A-Za-z0-9_.-]+", pin["hashBasis"]))))
        if valid:
            pins[key] = pin
        else:
            skipped.append({"anchor": key, "reason": "invalid_pin_shape"})
    return pins, skipped


def _load_config_pins(root: Path) -> Dict[str, Any]:
    try:
        config = json.loads((root / CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return _validated_config_pins(config)[0] if isinstance(config, dict) else {}


def _format_markdown(title: str, report: Dict[str, Any]) -> str:
    s = report["anchorSummary"]
    lines = [
        f"## {title}",
        "",
        f"- **Repository**: `{report['repoPath']}`",
        f"- **Head**: `{report.get('repoHead') or 'n/a'}`",
        f"- **Anchors**: {s['active']} active · {s['compatible-unverified']} compatible-unverified · {s['stale']} stale",
        f"- Unverified observations: {s['path-exists']} path-exists · {s['unpinned']} unpinned",
        "- New pins record the current baseline; they do not verify the rule's meaning.",
    ]
    if "instructionFiles" in report:
        lines.append("- **Instruction files**: " + (", ".join(f"`{f}`" for f in report["instructionFiles"]) or "(none)"))
    if report.get("skippedPins"):
        lines.append("- Skipped invalid pins: " + ", ".join(str(p.get("anchor") or "pinnedAnchors") for p in report["skippedPins"]))
    if report.get("pages") is not None:
        lines.append("- **Wiki pages**: " + (", ".join(f"`{p}`" for p in report["pages"]) or "(none)"))
    if report.get("actions"):
        lines += ["", "### Actions", ""] + [f"- `{a['op']}` `{a['path']}`" for a in report["actions"]]
    if report.get("anchors"):
        lines += ["", "| Status | Anchor | Reason |", "| :--- | :--- | :--- |"]
        lines += [f"| `{a['status']}` | `{a['anchor']}` | {a['reason']} |" for a in report["anchors"]]
    return "\n".join(lines) + "\n"


def _base_config(head: Optional[str], instruction_files: List[str]) -> Dict[str, Any]:
    return {
        "schemaVersion": CONFIG_SCHEMA_VERSION,
        "tool": "memlet",
        "version": MEMLET_VERSION,
        "repoHead": head,
        "instructionFiles": instruction_files,
        "wikiDir": WIKI_DIR,
        "indexPage": INDEX_PATH,
        "ledgerDb": LEDGER_PATH,
        "pinnedAnchors": {},
        "anchorStates": list(ANCHOR_STATES),
        "secretsPolicy": "never-read-env-or-secret-files",
        "daemon": False,
    }


def _resolve_root(repo_path) -> Path:
    root = Path(repo_path or ".").expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"repository path not found: {root}")
    return root


def init_project(repo_path=".", *, dry_run: bool = False, force: bool = False, output_format: str = "json") -> Dict[str, Any]:
    """`memlet init`: onboard a repo into `.memlet/` (read-only when `dry_run`)."""
    root = _resolve_root(repo_path)
    head = _git_head(root)
    instruction_files = discover_instruction_files(root)
    raw: List[Dict[str, Any]] = []
    for rel in instruction_files:
        raw.extend(_extract_anchors(_read_text(root / rel), rel))
    config_path = root / CONFIG_PATH
    config = _base_config(head, instruction_files)
    backup = None
    if config_path.is_file():
        try:
            existing = json.loads(_read_text(config_path))
            if not isinstance(existing, dict):
                raise ValueError("config must be a JSON object")
        except (ValueError, OSError) as exc:
            if not force:
                error = "Cannot read .memlet/config.json as an object; nothing changed. Use init --force to back it up and recover: " + str(exc)
                report = {"ok": False, "exitCode": 2, "command": "init", "dryRun": bool(dry_run),
                          "repoPath": str(root), "error": error, "actions": []}
                if output_format == "markdown":
                    report["formatted"] = "## Memlet Init\n\n" + error + "\n"
                return report
            backup = config_path.with_name("config.json.bak-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
            existing = {}
        config.update(existing)
    config.update(repoHead=head, instructionFiles=instruction_files, anchorStates=list(ANCHOR_STATES))
    stored, skipped_pins = _validated_config_pins(config)
    page_pins = {}
    wiki_dir = root / WIKI_DIR
    if wiki_dir.is_dir() and not wiki_dir.is_symlink():
        for page in sorted(wiki_dir.rglob("*.md")):
            if page.is_symlink() or not page.is_file():
                continue
            for a in _extract_anchors(_read_text(page), _rel(root, page)):
                if a["pin"]:
                    page_pins.setdefault(f"{a['path']}::{a['symbol']}", a["pin"])
                    raw.append(a)
    for key in stored:
        if "::" in key:
            path, symbol = key.rsplit("::", 1)
            raw.append({"path": path, "symbol": symbol, "pin": None, "source": CONFIG_PATH, "line": 1})
    anchors = []
    pinned_now = set()
    for a in _merge_anchors(raw):
        key = f"{a['path']}::{a['symbol']}"
        cp = stored.get(key) or {}
        pin = page_pins.get(key) or a.get("pin") or (
            {"ast": cp.get("astHash"), "sig": cp.get("sigHash"), "req": cp.get("reqSigHash"), "basis": cp.get("hashBasis")} if cp.get("astHash") else None)
        cur = _current_fingerprint(root, a["path"], a["symbol"])
        if force or not pin:
            pin = ({"ast": cur["ast"], "sig": cur["sig"] or None, "req": cur["req"], "basis": cur["basis"]}
                   if cur["resolution"] == "exact" and cur.get("ast") else None)
            if pin:
                pinned_now.add(key)
        elif classify_anchor(root, dict(a, pin=pin))["reason"] == "legacy_ast_hash_match":
            pin = {"ast": cur["ast"], "sig": cur["sig"] or None, "req": cur["req"], "basis": cur["basis"]}
        anchors.append(dict(a, pin=pin))
        if pin and (key not in stored or force):
            stored[key] = dict(cp, astHash=pin["ast"], sigHash=pin.get("sig"), reqSigHash=pin.get("req"), hashBasis=pin.get("basis") or "legacy")
        elif force:
            stored.pop(key, None)
        elif cp.get("astHash"):
            stored_pin = {"ast": cp["astHash"], "sig": cp.get("sigHash"), "req": cp.get("reqSigHash"), "basis": cp.get("hashBasis")}
            if classify_anchor(root, dict(a, pin=stored_pin))["reason"] == "legacy_ast_hash_match":
                stored[key] = dict(cp, astHash=cur["ast"], sigHash=cur["sig"], reqSigHash=cur["req"], hashBasis=cur["basis"])
    classified = [dict(classify_anchor(root, a), pinnedNow=not dry_run and f"{a['path']}::{a['symbol']}" in pinned_now,
                       wouldPin=dry_run and f"{a['path']}::{a['symbol']}" in pinned_now) for a in anchors]
    summary = _summarize(classified)
    audit = _audit_summary(root)
    config["pinnedAnchors"] = stored
    index_text = _render_index(head, instruction_files, anchors, classified, summary, audit)
    index_additions = False
    if not force and (root / INDEX_PATH).is_file():
        old_index = _read_text(root / INDEX_PATH)
        known = {(a["path"], a["symbol"]) for a in _extract_anchors(old_index, INDEX_PATH)}
        added = [a for a in anchors if (a["path"], a["symbol"]) not in known]
        if added:
            lines = ["", "## Additional code anchors", ""]
            for a in added:
                key = f"{a['path']}::{a['symbol']}"
                lines.append(f"- `{key}`")
                pin = a.get("pin")
                if pin:
                    sig = f" sig={pin['sig']}" if pin.get("sig") else ""
                    req = f" req={pin['req']}" if pin.get("req") else ""
                    lines.append(f"  <!-- memlet:anchor {key} ast={pin['ast']}{sig}{req} basis={pin.get('basis') or 'legacy'} -->")
            index_text = old_index.rstrip() + "\n" + "\n".join(lines) + "\n"
            index_additions = True
    config_text = json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    actions: List[Dict[str, str]] = []
    if backup is not None:
        if not dry_run:
            with backup.open("xb") as out:
                out.write(config_path.read_bytes())
        actions.append({"op": "would-backup" if dry_run else "backup", "path": _rel(root, backup)})

    plan = [(CONFIG_PATH, config_text, True), (INDEX_PATH, index_text, force or index_additions)]
    for rel, text, overwrite in plan:
        target = root / rel
        exists = target.is_file()
        if exists and target.read_text(encoding="utf-8", errors="ignore") == text:
            op = "unchanged"
        elif exists and not overwrite:
            op = "kept"
        else:
            op = "update" if exists else "create"
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        if dry_run and op in ("create", "update"):
            op = "would-" + op
        actions.append({"op": op, "path": rel})

    pin_migrations = {}
    if wiki_dir.is_dir() and not wiki_dir.is_symlink():
        for page in sorted(wiki_dir.rglob("*.md")):
            if page.is_symlink() or not page.is_file():
                continue
            old = _read_text(page)
            def migrate(m):
                key = f"{m.group('path')}::{m.group('symbol')}"
                cp = stored.get(key)
                if not cp:
                    return m.group(0)
                sig = f" sig={cp['sigHash']}" if cp.get("sigHash") else ""
                req = f" req={cp['reqSigHash']}" if cp.get("reqSigHash") else ""
                return f"<!-- memlet:anchor {key} ast={cp['astHash']}{sig}{req} basis={cp.get('hashBasis') or 'legacy'} -->"
            new = PIN_RE.sub(migrate, old) if force else _migrate_legacy_comments(root, old)
            if new != old:
                if not dry_run:
                    page.write_text(new, encoding="utf-8")
                    pin_migrations[_rel(root, page)] = (_page_body_sha256(old), _page_body_sha256(new))
                actions.append({"op": "would-update" if dry_run else "update", "path": _rel(root, page)})

    ledger = root / LEDGER_PATH
    index_written = any(a["path"] == INDEX_PATH and a["op"] in ("create", "update") for a in actions)
    if ledger.is_symlink():
        op = "kept"
    elif dry_run:
        op = "would-update" if ledger.is_file() else "would-create"
    else:
        existed = ledger.is_file()
        now = _utc_now()
        with contextlib.closing(sqlite3.connect(str(ledger))) as conn:
            with conn:
                _ledger_ready(conn)
                changed = _sync_snapshots(conn, classified, now)
                index = root / INDEX_PATH
                if index.is_file() and not index.is_symlink():
                    sha = _page_body_sha256(_read_text(index))
                    last = _last_page_hashes(conn).get(INDEX_PATH)
                    # A kept INDEX.md with a recorded hash is left for `memlet wiki` to sync as a user edit.
                    if last != sha and (index_written or last is None):
                        _record_page(conn, INDEX_PATH, sha, "init", now)
                        changed = True
                for rel, (before, after) in pin_migrations.items():
                    if rel == INDEX_PATH:
                        continue  # The index was recorded above in this same transaction.
                    last = _last_page_hashes(conn).get(rel)
                    if last != before and last != after:
                        _record_page(conn, rel, before, "user_edit", now)
                    if last != after:
                        _record_page(conn, rel, after, "pin_migration", now)
                        changed = True
        op = "update" if existed and changed else ("unchanged" if existed else "create")
    actions.append({"op": op, "path": LEDGER_PATH})

    report: Dict[str, Any] = {
        "ok": True,
        "exitCode": 0,
        "command": "init",
        "dryRun": bool(dry_run),
        "repoPath": str(root),
        "repoHead": head,
        "instructionFiles": instruction_files,
        "config": config,
        "skippedPins": skipped_pins,
        "actions": actions,
        "anchorSummary": summary,
        "baselineNotice": "New pins record the current baseline; they do not verify the rule's meaning.",
        "anchors": classified,
        "audit": audit,
        "next": ["memlet wiki", "memlet check"],
    }
    if (output_format or "json").lower() == "markdown":
        report["formatted"] = _format_markdown("Memlet Init" + (" (dry run)" if dry_run else ""), report)
    return report


def repin_legacy_ts_anchors(repo_path=".", *, dry_run: bool = False) -> Dict[str, Any]:
    """Explicitly re-pin only legacy TS/JS baselines; versioned stale pins stay untouched.

    This accepts the current code as the new lexical baseline, not verification
    of its behavior. The dry run previews identical selections without writes.
    """
    root = _resolve_root(repo_path)
    config_path = root / CONFIG_PATH
    config = json.loads(_read_text(config_path)) if config_path.is_file() else _base_config(_git_head(root), [])
    if not isinstance(config, dict):
        raise ValueError("memlet config must be a JSON object")
    pins, skipped = _validated_config_pins(config)
    selected, cache = set(), {}

    def replacement(key, basis):
        if basis not in (None, "legacy") or "::" not in key:
            return None
        path, symbol = key.rsplit("::", 1)
        if Path(path).suffix.lower() not in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            return None
        if key not in cache:
            cache[key] = _current_fingerprint(root, path, symbol)
        cur = cache[key]
        if cur["resolution"] != "exact" or not cur.get("ast"):
            return None
        selected.add(key)
        return {"ast": cur["ast"], "sig": cur["sig"], "req": cur["req"], "basis": cur["basis"]}

    updated = dict(config.get("pinnedAnchors") or {}) if isinstance(config.get("pinnedAnchors", {}), dict) else {}
    for key, cp in pins.items():
        pin = replacement(key, cp.get("hashBasis"))
        if pin:
            updated[key] = dict(cp, astHash=pin["ast"], sigHash=pin["sig"], reqSigHash=pin["req"], hashBasis=pin["basis"])
    pages = []
    wiki_dir = root / WIKI_DIR
    if wiki_dir.is_dir() and not wiki_dir.is_symlink():
        for page in sorted(wiki_dir.rglob("*.md")):
            if page.is_symlink() or not page.is_file():
                continue
            before = _read_text(page)
            def replace(m):
                key = f"{m.group('path')}::{m.group('symbol')}"
                pin = replacement(key, m.group("basis"))
                if not pin:
                    return m.group(0)
                sig = f" sig={pin['sig']}" if pin.get("sig") else ""
                req = f" req={pin['req']}" if pin.get("req") else ""
                return f"<!-- memlet:anchor {key} ast={pin['ast']}{sig}{req} basis={pin['basis']} -->"
            after = PIN_RE.sub(replace, before)
            if before != after:
                pages.append((page, before, after))
    actions = []
    if updated != config.get("pinnedAnchors", {}):
        config["pinnedAnchors"] = updated
        if not dry_run:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        actions.append({"op": "would-update" if dry_run else "update", "path": CONFIG_PATH})
    for page, before, after in pages:
        if not dry_run:
            page.write_text(after, encoding="utf-8")
        actions.append({"op": "would-update" if dry_run else "update", "path": _rel(root, page)})
    ledger = root / LEDGER_PATH
    if pages and not dry_run and not ledger.is_symlink():
        with contextlib.closing(sqlite3.connect(str(ledger))) as conn:
            with conn:
                _ledger_ready(conn)
                last, now = _last_page_hashes(conn), _utc_now()
                for page, before, after in pages:
                    rel = _rel(root, page)
                    old_hash, new_hash = _page_body_sha256(before), _page_body_sha256(after)
                    if last.get(rel) != old_hash:
                        _record_page(conn, rel, old_hash, "user_edit", now)
                    _record_page(conn, rel, new_hash, "pin_migration", now)
    return {"ok": True, "exitCode": 0, "dryRun": bool(dry_run), "repinnedAnchors": sorted(selected),
            "skippedPins": skipped, "actions": actions,
            "baselineNotice": "Current TS/JS code becomes a new lexical baseline; rule meaning is not verified."}


def wiki_status(repo_path=".", *, dry_run: bool = False, output_format: str = "json") -> Dict[str, Any]:
    """`memlet wiki`: re-check AST anchors in `.memlet/wiki/*.md`, refresh the INDEX status block and
    per-page status headers, and sync human page edits into `.memlet/ledger.sqlite3`."""
    root = _resolve_root(repo_path)
    wiki_dir = root / WIKI_DIR
    initialized = wiki_dir.is_dir() and not wiki_dir.is_symlink()
    raw: List[Dict[str, Any]] = []
    pages: List[str] = []
    texts: Dict[str, str] = {}
    if initialized:
        for p in sorted(wiki_dir.rglob("*.md")):
            if p.is_symlink() or not p.is_file():
                continue
            rel = _rel(root, p)
            pages.append(rel)
            texts[rel] = _read_text(p)
            raw.extend(_extract_anchors(_STATUS_BLOCK_RE.sub("", _PAGE_STATUS_RE.sub("", texts[rel])), rel))
    else:
        # Not initialized: read-only preview over instruction files, nothing is written.
        for rel in discover_instruction_files(root):
            raw.extend(_extract_anchors(_read_text(root / rel), rel))
    original_hashes = {rel: _page_body_sha256(text) for rel, text in texts.items()}
    merged = _merge_anchors(raw)
    if initialized:
        # Pins written before `req=` existed fall back to the required-signature hash in config.json.
        config_pins = _load_config_pins(root)
        for a in merged:
            cp = config_pins.get(f"{a['path']}::{a['symbol']}")
            if not a["pin"] and isinstance(cp, dict) and cp.get("astHash"):
                a["pin"] = {"ast": cp["astHash"], "sig": cp.get("sigHash"), "req": cp.get("reqSigHash"), "basis": cp.get("hashBasis")}
            if a["pin"] and not a["pin"].get("req") and isinstance(cp, dict) and _same_hash(a["pin"]["ast"], cp.get("astHash")):
                a["pin"] = dict(a["pin"], req=cp.get("reqSigHash"))
    classified = [classify_anchor(root, a) for a in merged]
    summary = _summarize(classified)

    actions: List[Dict[str, str]] = []
    index = root / INDEX_PATH
    if initialized and index.is_file() and not index.is_symlink():
        old = _read_text(index)
        block = _status_block(classified, summary)
        new = _STATUS_BLOCK_RE.sub(lambda _m: block, old) if _STATUS_BLOCK_RE.search(old) else old.rstrip("\n") + "\n\n" + block + "\n"
        new = _migrate_legacy_comments(root, new)
        if new == old:
            actions.append({"op": "unchanged", "path": INDEX_PATH})
        elif dry_run:
            actions.append({"op": "would-update", "path": INDEX_PATH})
        else:
            index.write_text(new, encoding="utf-8")
            texts[INDEX_PATH] = new
            actions.append({"op": "update", "path": INDEX_PATH})

    status_by_key = {c["anchor"]: c["status"] for c in classified}
    page_summaries: Dict[str, Dict[str, int]] = {}
    for a in raw:
        counts = page_summaries.setdefault(a["source"], {s: 0 for s in ANCHOR_STATES})
        counts[status_by_key[f"{a['path']}::{a['symbol']}"]] += 0 if a["pin"] else 1

    # Every page other than INDEX.md carries a bounded status header for its own anchors.
    for rel in pages:
        if rel == INDEX_PATH:
            continue
        old = texts[rel]
        new = _with_page_header(old, page_summaries.get(rel) or {s: 0 for s in ANCHOR_STATES})
        new = _migrate_legacy_comments(root, new)
        if new == old:
            actions.append({"op": "unchanged", "path": rel})
        elif dry_run:
            actions.append({"op": "would-update", "path": rel})
        else:
            (root / rel).write_text(new, encoding="utf-8")
            texts[rel] = new
            actions.append({"op": "update", "path": rel})

    config_path = root / CONFIG_PATH
    if initialized and config_path.is_file() and not config_path.is_symlink():
        try:
            config = json.loads(_read_text(config_path))
        except (ValueError, OSError):
            config = None
        pins = config.get("pinnedAnchors") if isinstance(config, dict) else None
        migrated = False
        for key, cp in list(pins.items()) if isinstance(pins, dict) else []:
            if not isinstance(cp, dict) or "::" not in key:
                continue
            path, symbol = key.rsplit("::", 1)
            result = classify_anchor(root, {"path": path, "symbol": symbol,
                                            "pin": {"ast": cp.get("astHash"), "sig": cp.get("sigHash"), "req": cp.get("reqSigHash"), "basis": cp.get("hashBasis")}})
            if result["reason"] == "legacy_ast_hash_match":
                pins[key] = dict(cp, astHash=result["currentAstHash"], sigHash=result["currentSigHash"],
                                 reqSigHash=result["currentReqSigHash"], hashBasis=result["currentHashBasis"])
                migrated = True
        if migrated:
            if not dry_run:
                config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            actions.append({"op": "would-update" if dry_run else "update", "path": CONFIG_PATH})

    # Bi-directional sync: a page body that differs from its last recorded hash is a human edit.
    ledger = root / LEDGER_PATH
    ledger_synced = False
    user_edited: List[str] = []
    if initialized and not ledger.is_symlink():
        page_hashes = {rel: _page_body_sha256(texts[rel]) for rel in pages}
        if dry_run:
            last = _read_ledger_hashes(ledger)
            user_edited = [rel for rel in pages if last.get(rel) != original_hashes[rel] and not (rel == INDEX_PATH and rel not in last)]
        else:
            existed = ledger.is_file()
            now = _utc_now()
            with contextlib.closing(sqlite3.connect(str(ledger))) as conn:
                with conn:
                    _ledger_ready(conn)
                    last = _last_page_hashes(conn)
                    changed = False
                    for rel in pages:
                        before, after = original_hashes[rel], page_hashes[rel]
                        if last.get(rel) != before:
                            event = "init" if rel == INDEX_PATH and rel not in last else "user_edit"
                            _record_page(conn, rel, before, event, now)
                            if event == "user_edit":
                                user_edited.append(rel)
                            changed = True
                        if before != after:
                            _record_page(conn, rel, after, "pin_migration", now)
                            changed = True
                    changed = _sync_snapshots(conn, classified, now) or changed
            ledger_synced = True
            actions.append({"op": "update" if existed and changed else ("unchanged" if existed else "create"), "path": LEDGER_PATH})

    report: Dict[str, Any] = {
        "ok": True,
        "exitCode": 0,
        "command": "wiki",
        "dryRun": bool(dry_run),
        "initialized": initialized,
        "repoPath": str(root),
        "repoHead": _git_head(root),
        "wikiDir": WIKI_DIR,
        "pages": pages if initialized else None,
        "actions": actions,
        "ledgerSynced": ledger_synced,
        "userEditedPages": user_edited,
        "anchorSummary": summary,
        "pageSummaries": [dict(page=k, **v) for k, v in sorted(page_summaries.items())],
        "anchors": classified,
    }
    if not initialized:
        report["hint"] = "Run `memlet init` to create .memlet/wiki/; showing a read-only preview of instruction-file anchors."
    if (output_format or "json").lower() == "markdown":
        report["formatted"] = _format_markdown("Memlet Wiki", report)
    return report


# ---------------------------------------------------------------------------
# `memlet import`: bring existing instruction / memory files under AST verification.

IMPORT_PAGE = ".memlet/wiki/imported-rules.md"
HANDOVER_PAGE = ".memlet/wiki/HANDOVER.md"
IMPORT_SOURCES = ("auto", "claude-md", "cursorrules", "ai-memory", "claude-mem")
_MEMORY_DIRS = {"ai-memory": ".ai-memory", "claude-mem": ".claude-mem"}
BRIEF_PAGE = ".memlet/wiki/BRIEF.md"  # written by `memlet brief-build`; never read back as wiki rule text
RULE_INDEX_PAGE = ".memlet/wiki/INDEX-RULES.md"  # written by `memlet index-build`; never read back as wiki rule text
_GENERATED_PAGES = (INDEX_PATH, IMPORT_PAGE, HANDOVER_PAGE)
# BRIEF.md / INDEX-RULES.md count as generated only when they carry their generator's marker: a page a person wrote
# under the same name keeps being read as ordinary wiki rules.
_MARKED_GENERATED_PAGES = {BRIEF_PAGE: "<!-- memlet:brief-meta ", RULE_INDEX_PAGE: "<!-- memlet:index-meta "}


def _is_generated_page(rel: str, text: str) -> bool:
    return rel in _GENERATED_PAGES or (rel in _MARKED_GENERATED_PAGES and _MARKED_GENERATED_PAGES[rel] in text)
RULE_STATES = ANCHOR_STATES + ("unanchored",)
_MAX_SOURCE_FILES = 400
_MAX_SOURCE_BYTES = 1_000_000
_MAX_RULES_PER_FILE = 200
_RULE_TEXT_LIMIT = 300
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(?P<text>\S.*)$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")
_DEAD_END_HEADING_RE = re.compile(r"\b(?:rejected|dead[\s-]?ends?)\b", re.I)


def _memory_dir_files(root: Path, dirname: str) -> List[str]:
    """Markdown files under a memory-tool directory (`.ai-memory/`, `.claude-mem/`), bounded and secret-free."""
    base = root / dirname
    if base.is_symlink() or not base.is_dir():
        return []
    out: List[str] = []
    for p in sorted(base.rglob("*.md")):
        if len(out) >= _MAX_SOURCE_FILES:
            break
        try:
            rel = _rel(root, p)
            p.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        parts = Path(rel).parts
        if p.is_symlink() or not p.is_file() or len(parts) > 8 or _is_secret_path(rel):
            continue
        if any(part in EXCLUDED_DIRS for part in parts):
            continue
        out.append(rel)
    return out


def import_source_files(root: Path, source: str = "auto") -> List[str]:
    """Repo-relative files `memlet import --from <source>` reads (never `.env*`)."""
    source = (source or "auto").lower()
    if source not in IMPORT_SOURCES:
        raise ValueError(f"unknown import source: {source} (expected one of {', '.join(IMPORT_SOURCES)})")
    instruction_files = discover_instruction_files(root)
    if source == "auto":
        picked = instruction_files + _memory_dir_files(root, ".ai-memory") + _memory_dir_files(root, ".claude-mem")
    elif source == "claude-md":
        picked = [f for f in instruction_files if f.rsplit("/", 1)[-1] in ("CLAUDE.md", "AGENTS.md", "GEMINI.md")]
    elif source == "cursorrules":
        picked = [f for f in instruction_files if f == ".cursorrules" or f.startswith(".cursor/rules/")]
    else:
        picked = _memory_dir_files(root, _MEMORY_DIRS[source])
    return sorted(set(picked))


def _clean_ref_path(path: str) -> str:
    while path.startswith("./"):
        path = path[2:]
    return path


def _line_refs(text: str) -> List[Tuple[str, Optional[str]]]:
    """`path::Symbol` anchors and backticked file paths in one rule, in order of appearance."""
    refs: List[Tuple[str, Optional[str]]] = []
    scrubbed = PIN_RE.sub("", text)
    for m in EXPLICIT_ANCHOR_RE.finditer(scrubbed):
        if "::" in m.group(0):
            refs.append((_clean_ref_path(m.group("path")), m.group("symbol").rstrip(".-")))
    for m in BACKTICK_FILE_RE.finditer(scrubbed):
        path = _clean_ref_path(m.group("path"))
        if not m.group("symbol") and path.lower() not in IGNORED_FILE_TOKENS:
            refs.append((path, None))
    return list(dict.fromkeys(refs))


def _extract_rules(text: str, source: str) -> List[Dict[str, Any]]:
    """List items (or, in a file without lists, plain lines) outside code fences, with their section."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":  # `.mdc` / memory-file front matter
        for i in range(1, min(len(lines), 60)):
            if lines[i].strip() == "---":
                lines = [""] * (i + 1) + lines[i + 1:]
                break
    has_bullets = any(_BULLET_RE.match(line) for line in lines)
    rules: List[Dict[str, Any]] = []
    section, in_fence = "", False
    for line_no, line in enumerate(lines, start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        heading = _HEADING_RE.match(line)
        if in_fence or heading:
            section = heading.group("title").strip() if heading and not in_fence else section
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(("<!--", "|", ">")):
            continue
        bullet = _BULLET_RE.match(line)
        body = PIN_RE.sub("", bullet.group("text") if bullet else stripped).strip()
        refs = _line_refs(body)
        if not body or not (bullet or not has_bullets or refs):
            continue
        rules.append({"source": source, "line": line_no, "section": section,
                      "text": body[:_RULE_TEXT_LIMIT], "refs": refs})
        if len(rules) >= _MAX_RULES_PER_FILE:
            break
    return rules


def _rules_from_files(root: Path, files: Iterable[str]) -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    for rel in files:
        p = root / rel
        try:
            if p.stat().st_size > _MAX_SOURCE_BYTES:
                continue
        except OSError:
            continue
        rules.extend(_extract_rules(_read_text(p), rel))
    return rules


def _unsafe_rel(path: str) -> bool:
    return path.startswith("/") or ".." in Path(path).parts or _is_secret_path(path)


def _resolve_ref_path(root: Path, source: str, path: str) -> str:
    """Root-relative first; a nested instruction file may also reference paths relative to itself."""
    if _unsafe_rel(path) or (root / path).exists():
        return path
    parent = Path(source).parent.as_posix()
    if parent not in ("", "."):
        candidate = f"{parent}/{path}"
        if not _unsafe_rel(candidate) and (root / candidate).exists():
            return candidate
    return path


def _classify_path(root: Path, path: str) -> Dict[str, Any]:
    if _unsafe_rel(path):
        status, reason = "stale", "path_outside_repo_or_secret"
    else:
        try:
            (root / path).resolve().relative_to(root)
            exists = (root / path).exists()
        except (OSError, ValueError):
            exists = False
        status, reason = ("path-exists", "path_exists") if exists else ("stale", "missing_path")
    return {"anchor": path, "path": path, "symbol": None, "status": status, "reason": reason, "pinned": False,
            "pinSource": None, "currentAstHash": None, "currentSigHash": None, "currentReqSigHash": None}


def _config_pins(root: Path) -> Dict[str, Dict[str, Optional[str]]]:
    pins = {}
    for key, cp in _load_config_pins(root).items():
        if isinstance(cp, dict) and cp.get("astHash"):
            pins[key] = {"ast": cp["astHash"], "sig": cp.get("sigHash"), "req": cp.get("reqSigHash"), "basis": cp.get("hashBasis")}
    return pins


def _verify_rules(root: Path, rules: List[Dict[str, Any]], pins: Dict[str, Dict[str, Any]],
                  pin_source: str = "config") -> List[Dict[str, Any]]:
    """Classify each referenced anchor once and tag every rule with its worst anchor state.

    Observing a symbol does not establish a baseline; only stored pins are compared."""
    cache: Dict[str, Dict[str, Any]] = {}
    for r in rules:
        keys: List[str] = []
        for raw_path, symbol in r["refs"]:
            path = _resolve_ref_path(root, r["source"], raw_path)
            key = f"{path}::{symbol}" if symbol else path
            if key not in cache:
                if symbol:
                    pin, src = pins.get(key), pin_source
                    entry = classify_anchor(root, {"path": path, "symbol": symbol, "pin": pin})
                    if entry["reason"] == "legacy_ast_hash_match":
                        pin = {"ast": entry["currentAstHash"], "sig": entry["currentSigHash"], "req": entry["currentReqSigHash"], "basis": entry["currentHashBasis"]}
                    entry.update(pin=pin, pinSource=src if pin else None)
                else:
                    entry = _classify_path(root, path)
                entry["sources"] = []
                cache[key] = entry
            loc = f"{r['source']}:{r['line']}"
            if loc not in cache[key]["sources"]:
                cache[key]["sources"].append(loc)
            if key not in keys:
                keys.append(key)
        states = {cache[k]["status"] for k in keys}
        r["anchors"] = keys
        r["status"] = next((s for s in ("stale", "compatible-unverified", "unpinned", "path-exists", "active") if s in states), "unanchored")
        r["id"] = "rule-" + hashlib.sha256(f"{r['source']}\n{r['text']}".encode("utf-8")).hexdigest()[:12]
    return [cache[k] for k in sorted(cache)]


def _public_anchor(a: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in a.items() if k != "pin"}


def _public_rule(r: Dict[str, Any], by_key: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "id": r["id"], "source": r["source"], "line": r["line"], "section": r["section"], "text": r["text"],
        "status": r["status"],
        "anchors": [{"anchor": k, "status": by_key[k]["status"], "reason": by_key[k]["reason"]} for k in r["anchors"]],
    }


def _rule_counts(rules: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {s: 0 for s in RULE_STATES}
    for r in rules:
        counts[r["status"]] += 1
    return counts


def _render_import_page(files: List[str], rules: List[Dict[str, Any]], by_key: Dict[str, Dict[str, Any]],
                        counts: Dict[str, int], broken: List[str]) -> str:
    lines = [
        "# Imported Rules",
        "",
        "<!-- Generated by `memlet import`; re-running it regenerates this page. Keep your own notes on other pages. -->",
        "",
        "- Sources: " + (", ".join(f"`{f}`" for f in files) if files else "(none found)"),
        f"- Rules: {len(rules)} imported · {counts['active']} active · {counts['compatible-unverified']} compatible-unverified"
        f" · {counts['stale']} stale · {counts['path-exists']} path-exists · {counts['unpinned']} unpinned · {counts['unanchored']} unanchored",
        "- Baselines: explicit init/import records current hashes; it does not verify the rule's meaning.",
        "- Broken paths: " + (", ".join(f"`{b}`" for b in broken) if broken else "none"),
    ]
    pinned_here = set()
    for source in files:
        page_rules = [r for r in rules if r["source"] == source]
        if not page_rules:
            continue
        lines += ["", f"## `{source}`", ""]
        for r in page_rules:
            notes = [f"`{k}`: {by_key[k]['reason']}" for k in r["anchors"] if by_key[k]["status"] != "active"]
            suffix = f"L{r['line']}" + ("; " + "; ".join(notes) if notes else "")
            lines.append(f"- `{r['status']}` {r['text']} _({suffix})_")
            for k in r["anchors"]:
                pin = by_key[k].get("pin")
                if pin and k not in pinned_here:
                    pinned_here.add(k)
                    sig = f" sig={pin['sig']}" if pin.get("sig") else ""
                    req = f" req={pin['req']}" if pin.get("req") else ""
                    lines.append(f"  <!-- memlet:anchor {k} ast={pin['ast']}{sig}{req} basis={pin.get('basis') or 'legacy'} -->")
    return "\n".join(lines) + "\n"


def _write_generated_page(root: Path, rel: str, text: str, dry_run: bool) -> str:
    target = root / rel
    if any(p.is_symlink() for p in (root / MEMLET_DIR, root / WIKI_DIR, target)):
        return "kept"
    exists = target.is_file()
    if exists and _PAGE_STATUS_RE.sub("", _read_text(target)) == text:
        return "unchanged"
    if dry_run:
        return "would-update" if exists else "would-create"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return "update" if exists else "create"


def _escape_cell(text: str, limit: int = 100) -> str:
    text = text if len(text) <= limit else text[: limit - 1] + "…"
    return text.replace("|", "\\|")


def import_rules(repo_path=".", *, source: str = "auto", dry_run: bool = False, output_format: str = "json") -> Dict[str, Any]:
    """`memlet import`: import rules from `CLAUDE.md` / `AGENTS.md` / `.cursorrules` / `.cursor/rules/*.mdc` /
    `.ai-memory/**/*.md` / `.claude-mem/**/*.md` and audit every code anchor they mention right away."""
    root = _resolve_root(repo_path)
    source = (source or "auto").lower()
    head = _git_head(root)
    files = import_source_files(root, source)
    rules = _rules_from_files(root, files)
    pins = _config_pins(root)
    pinned_now = set()
    # Import is an explicit baseline-recording event, like init. Never heal an existing pin.
    for r in rules:
        for raw_path, symbol in r["refs"]:
            if not symbol:
                continue
            path = _resolve_ref_path(root, r["source"], raw_path)
            key = f"{path}::{symbol}"
            if key in pins:
                continue
            cur = _current_fingerprint(root, path, symbol)
            if cur["resolution"] == "exact" and cur.get("ast"):
                pins[key] = {"ast": cur["ast"], "sig": cur["sig"], "req": cur["req"], "basis": cur["basis"]}
                pinned_now.add(key)
    anchors = _verify_rules(root, rules, pins)
    for a in anchors:
        a["pinnedNow"] = not dry_run and a["anchor"] in pinned_now
        a["wouldPin"] = dry_run and a["anchor"] in pinned_now
    by_key = {a["anchor"]: a for a in anchors}
    counts = _rule_counts(rules)
    broken = sorted({a["path"] for a in anchors if a["reason"] in ("missing_path", "missing_file")})
    page_text = _render_import_page(files, rules, by_key, counts, broken)

    actions = [{"op": _write_generated_page(root, IMPORT_PAGE, page_text, dry_run), "path": IMPORT_PAGE}]
    cfg_path = root / CONFIG_PATH
    config: Any = None
    if cfg_path.is_file() and not cfg_path.is_symlink():
        try:
            config = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = False  # unreadable config is left alone
    if config is False or cfg_path.is_symlink():
        actions.append({"op": "kept", "path": CONFIG_PATH})
    else:
        existed = isinstance(config, dict)
        config = config if existed else _base_config(head, discover_instruction_files(root))
        pinned = _validated_config_pins(config)[0]
        for a in anchors:
            # Store newly recorded baselines, including lexical TS/JS pins; migrate proven legacy matches.
            if a["symbol"] and a.get("pin") and (a["anchor"] not in pinned or a["reason"] == "legacy_ast_hash_match"):
                pinned[a["anchor"]] = {"astHash": a["pin"]["ast"], "sigHash": a["pin"]["sig"], "reqSigHash": a["pin"]["req"], "hashBasis": a["pin"].get("basis") or "legacy"}
        config["pinnedAnchors"] = dict(sorted(pinned.items()))
        config["imports"] = {"from": source, "page": IMPORT_PAGE, "sourceFiles": files}
        text = json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if existed and _read_text(cfg_path) == text:
            op = "unchanged"
        elif dry_run:
            op = "would-update" if existed else "would-create"
        else:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(text, encoding="utf-8")
            op = "update" if existed else "create"
        actions.append({"op": op, "path": CONFIG_PATH})

    ledger = root / LEDGER_PATH
    if ledger.is_symlink():
        op = "kept"
    elif dry_run:
        op = "would-update" if ledger.is_file() else "would-create"
    else:
        existed = ledger.is_file()
        now = _utc_now()
        with contextlib.closing(sqlite3.connect(str(ledger))) as conn:
            with conn:
                _ledger_ready(conn)
                changed = _sync_snapshots(conn, [a for a in anchors if a["symbol"]], now)
                sha = _page_body_sha256(page_text)
                if _last_page_hashes(conn).get(IMPORT_PAGE) != sha:
                    _record_page(conn, IMPORT_PAGE, sha, "import", now)
                    changed = True
        op = "update" if existed and changed else ("unchanged" if existed else "create")
    actions.append({"op": op, "path": LEDGER_PATH})

    report: Dict[str, Any] = {
        "ok": True,
        "exitCode": 0,
        "command": "import",
        "dryRun": bool(dry_run),
        "from": source,
        "repoPath": str(root),
        "repoHead": head,
        "page": IMPORT_PAGE,
        "sourcesScanned": files,
        "sourcesScannedCount": len(files),
        "importedCount": len(rules),
        "activeCount": counts["active"],
        "pathExistsCount": counts["path-exists"],
        "unpinnedCount": counts["unpinned"],
        "baselineNotice": "New pins record the current baseline; they do not verify the rule's meaning.",
        "compatibleUnverifiedCount": counts["compatible-unverified"],
        "staleCount": counts["stale"],
        "unanchoredCount": counts["unanchored"],
        "brokenPathsCount": len(broken),
        "brokenPaths": broken,
        "anchorSummary": _summarize(anchors),
        "rules": [_public_rule(r, by_key) for r in rules],
        "anchors": [_public_anchor(a) for a in anchors],
        "actions": actions,
        "next": ["memlet wiki", "memlet handover"],
    }
    if (output_format or "json").lower() == "markdown":
        lines = [
            "## Memlet Import" + (" (dry run)" if dry_run else ""),
            "",
            f"- **Repository**: `{root}`",
            f"- **Head**: `{head or 'n/a'}`",
            f"- **Sources scanned**: {len(files)}" + (" (" + ", ".join(f"`{f}`" for f in files) + ")" if files else ""),
            "- New pins record the current baseline; they do not verify the rule's meaning.",
            f"- **Rules**: {len(rules)} imported · {counts['active']} active · {counts['compatible-unverified']} compatible-unverified"
            f" · {counts['stale']} stale · {counts['unanchored']} unanchored · {len(broken)} broken paths",
            "",
            "### Actions",
            "",
        ] + [f"- `{a['op']}` `{a['path']}`" for a in actions]
        if rules:
            lines += ["", "| Status | Rule | Source |", "| :--- | :--- | :--- |"]
            lines += [f"| `{r['status']}` | {_escape_cell(r['text'])} | `{r['source']}:{r['line']}` |" for r in rules]
        report["formatted"] = "\n".join(lines) + "\n"
    return report


# ---------------------------------------------------------------------------
# `memlet handover`: a checksummed capsule that only carries context Memlet could verify.

HANDOVER_SCHEMA = "routlet-memlet-handover/v1"
HANDOVER_TOKEN_BUDGET = 500  # default; per call (`--budget`) or per repo (`config.json` handover.tokenBudget)
HANDOVER_BUDGET_MIN, HANDOVER_BUDGET_MAX = 500, 2000
HANDOVER_AUTO_RATIO = 0.12  # `auto`: about 12% of the instruction text the capsule stands in for, clamped to [min, max]
_HANDOVER_QUARANTINE_FLOOR = 3  # stale anchors kept in the capsule under budget pressure, as long as the budget allows
_HANDOVER_ANCHOR_LIMIT = 96


def _bounded_anchor(anchor: str) -> str:
    """Keep anchors identifiable but bounded: long paths become `head…#sha8::symbol`."""
    if len(anchor) <= _HANDOVER_ANCHOR_LIMIT:
        return anchor
    digest = hashlib.sha256(anchor.encode("utf-8")).hexdigest()[:8]
    path, _, symbol = anchor.rpartition("::")
    keep = max(16, _HANDOVER_ANCHOR_LIMIT - len(symbol) - 14)
    return f"{path[:keep]}…#{digest}::{symbol}" if symbol else f"{anchor[:_HANDOVER_ANCHOR_LIMIT - 10]}…#{digest}"
AUTHORITY_BOUNDARY = "read-only-context-only: execution and external side-effect approvals are NOT inherited"
HANDOVER_SIGNED_FIELDS = ("schema", "repoHead", "activeConstraints", "reverifyBeforeUse", "quarantinedStaleAnchors",
                          "rejectedApproaches", "executionProof", "nextAction", "authorityBoundary", "omitted")
_HANDOVER_LIST_CAP = 40
DIGEST_SIGNED_FIELDS = ("mode", "projectRules")
HANDOVER_MODES = ("verified", "digest", "brief", "index")
INDEX_SIGNED_FIELDS = ("mode", "quotedRules", "ruleIndex", "staleIndex", "indexSource")
BRIEF_SIGNED_FIELDS = ("mode", "briefRules", "staleBrief", "briefSource")
_DIGEST_TEXT_LIMIT = 200
_DIGEST_STALE_SHOWN = 3
# instruction copies inside run outputs / backups are snapshots of old rules, not the project's current instructions
# Snapshot folders for opt-in discovery exclusion: whole-segment names only, so current folders such as
# `backup-service/` or `output-format/` keep their instructions; `pre_v10_backup/`, `runs/`, `outputs/` do not.
_SNAPSHOT_SEGMENT_RE = re.compile(
    r"^(?:runs?|outputs?|artifacts?|archives?|snapshots?|tmp|\.?cache|backups?|bak|old)$"
    r"|[_.-](?:backup|bak|old)$|^(?:pre|old)[_-].*[_-](?:backup|bak)$", re.I)
_DIGEST_SKIP_SEGMENT_RE = re.compile(r"^(?:runs?|outputs?|artifacts?|archives?|snapshots?|tmp|\.?cache)$|backup|\bbak\b|_old$|^old$", re.I)
_DIGEST_STRONG_RE = re.compile(
    r"\b(?:must(?:\s+not)?|never|always|do\s+not|don't|only|required?|forbidden|prohibited|mandatory|shall|no\s+\w+\s+without)\b"
    r"|금지|반드시|필수|절대|하지\s*(?:마|않)|만\s|승인|허용(?:하지|되지)|안\s*돼|않아요|말아요", re.I)
_DIGEST_RISK_RE = re.compile(
    r"\b(?:secrets?|credentials?|tokens?|api\s*keys?|\.env|paid|billing|live|deploy|push|commit|delete|rm\s+-rf|force|"
    r"approv\w*|budget|quota|production|real\s+money|trading|publish|upload)\b|비밀|키|결제|유료|과금|실거래|배포|푸시|커밋|삭제|승인|게시|업로드|한도",
    re.I)
_DIGEST_SECTION_RE = re.compile(r"rule|constraint|safety|guard|policy|principle|must|never|don't|do not|forbid|금지|원칙|규칙|주의|필수|안전", re.I)
_DIGEST_SOURCE_TAIL_RE = re.compile(r"\s+—\s+source\s+`[^`]*`.*$")
_HANDOVER_TEXT_LIMIT = 160
_NEXT_ACTION_LIMIT = 300
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]")
_HEX_RUN_RE = re.compile(r"[0-9a-f]{16,}")


def estimate_tokens(text: str) -> int:
    """Conservative offline token estimate: ~4 chars per word piece, 2 per hex digest char pair, 1 per symbol."""
    total = 0
    for m in _TOKEN_RE.finditer(text):
        s = m.group(0)
        if len(s) == 1:
            total += 1
        elif _HEX_RUN_RE.fullmatch(s):
            total += -(-len(s) // 2)
        else:
            total += -(-len(s) // 4)
    return total


def _signed_fields(capsule: Dict[str, Any]) -> Tuple[str, ...]:
    # digest fields join the signature only in digest mode, so the default capsule's signature is unchanged
    extra = {"digest": DIGEST_SIGNED_FIELDS, "brief": BRIEF_SIGNED_FIELDS, "index": INDEX_SIGNED_FIELDS}.get(capsule.get("mode"), ())
    observations = tuple(k for k in ("pathExistsAnchors", "unpinnedAnchors", "lexicalConstraints") if k in capsule)
    return HANDOVER_SIGNED_FIELDS + extra + observations


def handover_signature(capsule: Dict[str, Any]) -> str:
    """Compatibility name for the unkeyed SHA-256 checksum of capsule content."""
    payload = {k: capsule.get(k) for k in _signed_fields(capsule)}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_handover_checksum(capsule: Dict[str, Any]) -> bool:
    """Pure integrity check of capsule content, not authentication or code verification.

    Both checksum aliases must agree when present. Legacy capsules containing
    only capsuleSignature remain verifiable. If formatted Markdown is present,
    it must exactly equal the rendering of the checksummed fields. CLI metadata
    remains outside the checksum; this is integrity, not authentication.
    """
    if not isinstance(capsule, dict):
        return False
    if capsule.get("mode") not in (None,) + HANDOVER_MODES:
        return False
    expected = capsule.get("checksum", capsule.get("capsuleSignature"))
    if not isinstance(expected, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        return False
    if "capsuleSignature" in capsule and capsule["capsuleSignature"] != expected:
        return False
    if any(k not in capsule for k in _signed_fields(capsule)):
        return False
    if "signedFields" in capsule and capsule["signedFields"] != list(_signed_fields(capsule)):
        return False
    try:
        if expected != handover_signature(capsule):
            return False
        if "formatted" in capsule or "markdownChecksum" in capsule:
            rendered = _render_handover(capsule, expected)
            if "formatted" in capsule and capsule["formatted"] != rendered:
                return False
            if "markdownChecksum" in capsule and capsule["markdownChecksum"] != _markdown_checksum(rendered)[0]:
                return False
        return True
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


_MARKDOWN_CHECKSUM_RE = re.compile(r"(?m)^- Checksum: `(sha256:[0-9a-f]{64})` \(integrity, not authentication\)$")
_MARKDOWN_CHECKSUM_PLACEHOLDER = "sha256:" + "0" * 64


def _markdown_checksum(text: str):
    """Return the embedded and calculated text checksums, with the checksum slot zeroed."""
    prefix = "# Memlet Handover\n\n"
    if text.startswith(prefix):
        text = text[len(prefix):]
    if not text.startswith("## Routlet × Memlet Verified Handover Capsule\n"):
        return None
    matches = list(_MARKDOWN_CHECKSUM_RE.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    canonical = text[:match.start(1)] + _MARKDOWN_CHECKSUM_PLACEHOLDER + text[match.end(1):]
    return match.group(1), "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_handover_markdown(text: str) -> bool:
    """Verify standalone capsule/HANDOVER.md text integrity, not authentication.

    Every rendered byte except the fixed checksum slot is hashed. The optional
    fixed file title carries no rule content. This text checksum is distinct from
    the JSON capsule checksum, which covers canonical structured fields. Legacy
    pages containing only that JSON checksum cannot pass this text verifier.
    """
    pair = _markdown_checksum(text) if isinstance(text, str) else None
    return bool(pair and pair[0] == pair[1])


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _blank_lines(m: "re.Match[str]") -> str:
    """Replace a generated block with blank lines so rule line numbers stay accurate."""
    return "\n" * m.group(0).count("\n")


def _git_modified_count(root: Path) -> Optional[int]:
    """Working-tree entries from `git status --porcelain`, ignoring Memlet's own `.memlet/` files."""
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                             cwd=str(root), capture_output=True, text=True, timeout=10,
                             env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))  # read-only: never take .git/index.lock
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    count = 0
    for line in out.stdout.splitlines():
        path = line[3:].strip().strip('"').split(" -> ")[-1]
        if path and not (path == MEMLET_DIR + "/" or path.startswith(MEMLET_DIR + "/")):
            count += 1
    return count


def _render_handover(c: Dict[str, Any], signature: str) -> str:
    proof = c["executionProof"]
    health = proof["anchorHealth"]
    modified = proof["modifiedFilesCount"]
    lines = [
        "## Routlet × Memlet Verified Handover Capsule",
        "",
        f"- Repo head: `{c['repoHead'] or 'n/a'}`",
        f"- Checksum: `{_MARKDOWN_CHECKSUM_PLACEHOLDER}` (integrity, not authentication)",
        f"- Authority: {c['authorityBoundary']}",
        "",
        "### Active constraints (anchored symbols unchanged since pinning)",
    ]
    lines += [f"- {a['rule']} (`{a['source']}`" + ("; TRUNCATED — read the source line before acting" if a.get("truncated") else "")
              + ")" for a in c["activeConstraints"]] or ["- (none)"]
    if c.get("lexicalConstraints"):
        lines += ["", "### TS/JS constraints (lexical hash match only; re-check before use)"]
        lines += [f"- {a['rule']} (`{a['source']}`)" for a in c["lexicalConstraints"]]
    if c.get("mode") == "index":
        src = c.get("indexSource") or {}
        lines += ["", "### Key rules (quoted verbatim from the instruction files; source unchanged since the index was built)"]
        for a in c.get("quotedRules") or []:
            lines.append(f"- [{a['topic']}] `{a['source']}`:")
            lines += [f"  > {q}" for q in a["quote"].splitlines()]
        if not c.get("quotedRules"):
            lines.append("- (none — run `memlet index-build` to create the index)" if not src else "- (none fit the budget)")
        lines += ["", "### Rule index — BEFORE acting on any of these topics, read the cited lines "
                  f"(topics chosen by {src.get('via') or 'n/a'} at {src.get('builtAt') or 'n/a'}; not listing a rule here does "
                  "not mean it does not exist)"]
        lines += [f"- {a['topic']} → `{a['source']}`" for a in c.get("ruleIndex") or []] or ["- (none)"]
        if c["omitted"].get("ruleIndex"):
            lines.append(f"- {c['omitted']['ruleIndex']} more index entr(y/ies) omitted for the token budget; read the "
                         "project instructions for anything not listed here")
        held = len(c.get("staleIndex") or []) + c["omitted"].get("staleIndex", 0)
        if held:
            lines.append(f"- WARNING: {held} indexed rule(s) changed since the index was built (re-read before relying on "
                         "them): " + "; ".join(f"{a['topic']} `{a['source']}`" for a in (c.get("staleIndex") or [])))
    if c.get("mode") == "brief":
        src = c.get("briefSource") or {}
        lines += ["", f"### Project brief (LLM-written by {src.get('via') or 'n/a'} at {src.get('builtAt') or 'n/a'}; cited "
                  "instruction text unchanged since then — meaning and code behaviour are NOT verified; follow unless current "
                  "instructions say otherwise)"]
        lines += [f"- {a['rule']} (`{a['source']}`)" for a in c.get("briefRules") or []] or [
            "- (none — run `memlet brief-build` to create one)"]
        if c["omitted"].get("briefRules"):
            lines.append(f"- {c['omitted']['briefRules']} lower-priority brief line(s) omitted for the token budget; read the "
                         "project instructions for anything not listed here")
        held = len(c.get("staleBrief") or []) + c["omitted"].get("staleBrief", 0)
        if held:
            lines.append(f"- WARNING: {held} brief line(s) held back because their cited instructions changed: "
                         + "; ".join(f"`{a['source']}`" for a in (c.get("staleBrief") or [])) + " — re-read those lines")
    if c.get("mode") == "digest":
        lines += ["", "### Project rules (from instructions; not code-anchored — follow unless current instructions say otherwise)"]
        lines += [f"- {a['rule']} (`{a['source']}`" + (f"; path not found: {', '.join(a['pathNotFound'])}" if a.get("pathNotFound")
                  else "") + ("; TRUNCATED — read the source line before acting" if a.get("truncated") else "") + ")"
                  for a in c.get("projectRules") or []] or ["- (none)"]
        if c["omitted"].get("projectRules"):
            lines.append(f"- {c['omitted']['projectRules']} lower-priority rule(s) omitted for the token budget; read the "
                         "project instructions for anything not listed here")
    for key, label in (("pathExistsAnchors", "Paths exist (not verified)"),
                       ("unpinnedAnchors", "Unpinned symbols (not verified)")):
        if c.get(key):
            lines += ["", "### " + label]
            lines += [(f"- {a['rule']} (`{a['source']}`; {a['reason']}"
                       + ("; TRUNCATED — read the source line before acting" if a.get("truncated") else "") + ")")
                      if "rule" in a else f"- `{a['anchor']}`: {a['reason']}" for a in c[key]]
    if c["reverifyBeforeUse"]:
        lines += ["", "### Re-verify before use (compatible-unverified)"]
        lines += [f"- `{a['anchor']}`: {a['reason']}" for a in c["reverifyBeforeUse"]]
    lines += ["", "### Quarantined stale anchors (not handed over)"]
    lines += [f"- `{a['anchor']}`: {a['reason']}" for a in c["quarantinedStaleAnchors"]] or ["- (none)"]
    hidden_stale = c["omitted"].get("quarantinedStaleAnchors", 0)
    if hidden_stale:
        lines.append(f"- WARNING: {hidden_stale} more stale anchor(s) are quarantined but not listed here; do not follow any "
                     "rule whose anchor is not in the active list (run `memlet wiki` for the full list)")
    lines += ["", "### Rejected approaches (do not retry)"]
    lines += [f"- {a['approach']}" for a in c["rejectedApproaches"]] or ["- (none)"]
    lines += [
        "",
        "### Execution proof",
        f"- Modified files: {'n/a' if modified is None else modified} · anchors: {health['active']} active · "
        f"{health['compatible-unverified']} compatible-unverified · {health['stale']} stale",
        f"- Rules blocked as stale: {proof['staleRulesBlocked']} · unanchored rules not carried: {proof['unanchoredRulesOmitted']}",
    ]
    dropped = {k: v for k, v in c["omitted"].items() if v}
    if dropped:
        lines.append("- Omitted for token budget: " + ", ".join(f"{k} {v}" for k, v in sorted(dropped.items())))
    lines += ["", "### Next action", f"- {c['nextAction'] or '(not specified)'}"]
    body = "\n".join(lines) + "\n"
    # Keep a single visible checksum within the token budget. The JSON checksum
    # argument remains accepted for compatibility; the Markdown has its own basis.
    checksum = _markdown_checksum(body)[1]
    return body.replace(_MARKDOWN_CHECKSUM_PLACEHOLDER, checksum, 1)


def resolve_handover_budget(root: Path, budget=None) -> Tuple[int, str]:
    """(token budget, where it came from). Order: explicit argument > config.json `handover.tokenBudget` > default.

    A number is clamped to [HANDOVER_BUDGET_MIN, HANDOVER_BUDGET_MAX]; `auto` scales with the instruction text."""
    source = "argument"
    if budget is None:
        cfg_path = root / CONFIG_PATH
        try:
            cfg = None if cfg_path.is_symlink() else json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = None
        hand = cfg.get("handover") if isinstance(cfg, dict) else None
        budget = hand.get("tokenBudget") if isinstance(hand, dict) else None
        source = "config"
    if budget is None:
        return HANDOVER_TOKEN_BUDGET, "default"
    if str(budget).strip().lower() == "auto":
        text = "".join(_read_text(root / f) for f in discover_instruction_files(root))
        want = int(round(estimate_tokens(text) * HANDOVER_AUTO_RATIO))
        return max(HANDOVER_BUDGET_MIN, min(HANDOVER_BUDGET_MAX, want)), f"{source}:auto"
    try:
        if isinstance(budget, bool) or (isinstance(budget, float) and not budget.is_integer()):
            raise ValueError(budget)
        n = int(budget)
    except (TypeError, ValueError, OverflowError):  # 1e999 parses to infinity; non-integral or junk -> default
        return HANDOVER_TOKEN_BUDGET, f"{source}:invalid->default"
    return max(HANDOVER_BUDGET_MIN, min(HANDOVER_BUDGET_MAX, n)), source


def resolve_handover_mode(root: Path, mode=None) -> str:
    """Explicit argument > config.json `handover.mode` > `verified`. Unknown values fall back to `verified`."""
    if mode is None:
        try:
            cfg_path = root / CONFIG_PATH
            cfg = None if cfg_path.is_symlink() else json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = None
        hand = cfg.get("handover") if isinstance(cfg, dict) else None
        mode = hand.get("mode") if isinstance(hand, dict) else None
    mode = str(mode or "verified").strip().lower()
    return mode if mode in HANDOVER_MODES else "verified"


def _digest_norm(text: str) -> str:
    text = _DIGEST_SOURCE_TAIL_RE.sub("", text or "")
    return re.sub(r"[\W_]+", " ", text).strip().lower()


def _digest_compact(text: str, limit: int = _DIGEST_TEXT_LIMIT) -> Tuple[str, bool]:
    """(text, complete). Strip markdown decoration; shorten only at a sentence end and only when the dropped tail
    carries no rule wording (approval, prohibition, risk). Otherwise keep the whole rule: a condition is never cut."""
    complete = len(text or "") < _RULE_TEXT_LIMIT  # judged on the reader's raw slice, before any decoration is removed
    text = _DIGEST_SOURCE_TAIL_RE.sub("", text or "")
    text = re.sub(r"\*\*|__|(?<!\w)[*_](?!\s)|(?<!\s)[*_](?!\w)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text, complete
    ends = [m.end() for m in re.finditer(r"(?:[.!?]|요\.|다\.)\s", text) if limit // 2 <= m.end() <= limit]
    for cut in reversed(ends):
        tail = text[cut:]
        if not (_DIGEST_STRONG_RE.search(tail) or _DIGEST_RISK_RE.search(tail)):
            return text[:cut].rstrip(), complete
    return text, complete


def _digest_score(rule: Dict[str, Any], primary: set) -> float:
    text, section, src = rule["text"], rule.get("section") or "", rule["source"]
    score = 0.0
    score += 3.0 * min(2, len(_DIGEST_STRONG_RE.findall(text)))
    score += 2.0 * min(2, len(_DIGEST_RISK_RE.findall(text)))
    score += 2.0 if _DIGEST_SECTION_RE.search(section) else 0.0
    score += 2.0 if src in primary else 0.0
    score += 1.0 if "`" in text else 0.0  # names an exact path / command / flag
    score += max(0.0, 1.5 - rule["line"] / 200.0)  # earlier in the file reads as more central
    return round(score, 3)


def _digest_prose(root: Path, files: Iterable[str], known: set) -> List[Dict[str, Any]]:
    """Prose lines that state a rule (strong wording) in files whose rules are otherwise bullets — `_extract_rules`
    skips such lines, yet instruction files often put their most important rule in a paragraph."""
    out: List[Dict[str, Any]] = []
    for rel in files:
        lines = _read_text(root / rel).splitlines()
        if not any(_BULLET_RE.match(l) for l in lines):
            continue  # plain-line files are already fully extracted
        section, in_fence, front = "", False, bool(lines and lines[0].strip() == "---")
        for no, line in enumerate(lines, start=1):
            if front:
                front = not (no > 1 and line.strip() == "---")
                continue
            if _FENCE_RE.match(line):
                in_fence = not in_fence
                continue
            h = _HEADING_RE.match(line)
            if in_fence or h:
                section = h.group("title").strip() if h and not in_fence else section
                continue
            s = line.strip()
            if (len(s) < 40 or _BULLET_RE.match(line) or s.startswith(("<!--", "|", ">", "`"))
                    or not _DIGEST_STRONG_RE.search(s) or (rel, no) in known):
                continue
            if _line_refs(PIN_RE.sub("", s)) or _DEAD_END_HEADING_RE.search(section):
                continue  # a referenced path/symbol must go through anchor verification; dead-ends travel elsewhere
            out.append({"source": rel, "line": no, "section": section, "text": s[:_RULE_TEXT_LIMIT], "refs": [],
                        "status": "unanchored", "anchors": []})
    return out


def _digest_rules(constraints: List[Dict[str, Any]], skip_sources: set, anchor_by_key=None,
                  extra: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Instruction rules an agent needs that the verified list does not carry, ranked and de-duplicated.

    Unanchored rules qualify. So do rules held back only because a referenced *file* is missing (no code symbol
    drifted): the instruction may still hold, so it travels flagged instead of silently vanishing. Rules whose
    code anchors drifted stay blocked, as in verified mode."""
    anchor_by_key = anchor_by_key or {}
    pool = list(constraints) + list(extra or [])
    primary = {r["source"] for r in pool if r["source"].rsplit("/", 1)[-1] in ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
               and "/" not in r["source"]}
    seen, out = set(), []
    for r in pool:
        if r["source"] in skip_sources or r["source"].startswith(WIKI_DIR):
            continue
        missing = []
        if r["status"] == "stale":
            anchors = [anchor_by_key.get(a) for a in r.get("anchors") or []]
            if not anchors or any(a is None for a in anchors):
                continue
            stale = [a for a in anchors if a.get("status") == "stale"]
            # only a missing *file* reference qualifies; any drifted code symbol keeps the rule blocked
            if not stale or any(a.get("symbol") or a.get("reason") != "missing_path" for a in stale):
                continue
            missing = [a["path"] for a in stale]
        elif r["status"] != "unanchored":
            continue
        norm = _digest_norm(r["text"])
        if len(norm) < 12 or norm in seen:
            continue
        seen.add(norm)
        rule_text, complete = _digest_compact(r["text"])
        item = {"rule": rule_text, "source": f"{r['source']}:{r['line']}",
                "lineSha256": hashlib.sha256(r["text"].encode("utf-8")).hexdigest()[:12],
                "score": _digest_score(r, primary)}
        if missing:
            item["pathNotFound"] = sorted(set(missing))[:3]
        if not complete:
            item["truncated"] = True
        out.append(item)
    out.sort(key=lambda x: (-x["score"], x["source"]))
    return out


def build_handover(repo_path=".", *, next_step: Optional[str] = None, rejected: Optional[Iterable[str]] = None,
                   output_format: str = "json", write: bool = False, budget=None, mode=None) -> Dict[str, Any]:
    """`memlet handover`: deterministic, checksummed (<500 token) Routlet × Memlet handover capsule.

    Only rules whose anchors are all `active` are carried as constraints; `stale` anchors are quarantined
    and `compatible-unverified` ones are listed for re-verification. Recorded dead-ends (bullets under a
    `Rejected Approaches` / `Dead-Ends` heading) and `--rejected` items travel as rejected approaches."""
    root = _resolve_root(repo_path)
    token_budget, budget_source = resolve_handover_budget(root, budget)
    mode = resolve_handover_mode(root, mode)
    digest = mode == "digest"
    brief = mode == "brief"
    index = mode == "index"
    head = _git_head(root)
    # digest reads only discovered instruction files: memory-note folders may hold credentials in prose
    sources = discover_instruction_files(root) if (digest or brief or index) else import_source_files(root, "auto")
    skip_sources = {s for s in sources if any(_DIGEST_SKIP_SEGMENT_RE.search(seg) for seg in s.split("/")[:-1])} if digest else set()
    rules = _rules_from_files(root, [s for s in sources if s not in skip_sources])
    page_pins: Dict[str, Dict[str, Any]] = {}
    wiki_dir = root / WIKI_DIR
    if wiki_dir.is_dir() and not wiki_dir.is_symlink():
        for p in sorted(wiki_dir.rglob("*.md")):
            if p.is_symlink() or not p.is_file():
                continue
            rel = _rel(root, p)
            text = _read_text(p)
            for a in _extract_anchors(text, rel):
                if a["pin"]:
                    page_pins.setdefault(f"{a['path']}::{a['symbol']}", a["pin"])
            if not _is_generated_page(rel, text) and not (digest or brief or index):  # alt modes: wiki gives pins only
                body = _STATUS_BLOCK_RE.sub(_blank_lines, _PAGE_STATUS_RE.sub(_blank_lines, text))
                rules.extend(_extract_rules(body, rel))
    dead_ends = [r for r in rules if _DEAD_END_HEADING_RE.search(r["section"])]
    dead_ids = {id(r) for r in dead_ends}
    constraints, seen = [], set()
    for r in rules:
        norm = _digest_norm(r["text"]) if digest else re.sub(r"\s+", " ", r["text"]).strip().lower()
        if id(r) in dead_ids or norm in seen:
            continue
        seen.add(norm)
        constraints.append(r)
    pins = _config_pins(root)
    pins.update(page_pins)
    anchors = _verify_rules(root, constraints, pins, pin_source="pinned")
    by_key = {a["anchor"]: a for a in anchors}
    def _active(r):
        if not digest:
            return {"rule": _clip(r["text"], _HANDOVER_TEXT_LIMIT), "anchors": r["anchors"], "source": f"{r['source']}:{r['line']}"}
        text, complete = _digest_compact(r["text"])
        item = {"rule": text, "anchors": r["anchors"], "source": f"{r['source']}:{r['line']}"}
        if not complete:
            item["truncated"] = True
        return item
    def _observation(r):
        text, complete = _digest_compact(r["text"], _HANDOVER_TEXT_LIMIT)
        key = next(k for k in r["anchors"] if by_key[k]["status"] == r["status"])
        item = {"rule": text, "source": f"{r['source']}:{r['line']}", "anchors": r["anchors"],
                "anchor": _bounded_anchor(key), "reason": by_key[key]["reason"]}
        if not complete:
            item["truncated"] = True
        return item
    lists: Dict[str, List[Dict[str, Any]]] = {
        "activeConstraints": [_active(r) for r in constraints if r["status"] == "active"],
        "lexicalConstraints": [_active(r) for r in constraints
                               if r["anchors"] and any(by_key[k]["reason"] == "ts_js_lexical_scan_unverified" for k in r["anchors"])
                               and all(by_key[k]["status"] == "active" or by_key[k]["reason"] == "ts_js_lexical_scan_unverified"
                                       for k in r["anchors"])],
        "reverifyBeforeUse": [{"anchor": _bounded_anchor(a["anchor"]), "reason": a["reason"]}
                              for a in anchors if a["status"] == "compatible-unverified"],
        "quarantinedStaleAnchors": [{"anchor": _bounded_anchor(a["anchor"]), "reason": a["reason"]}
                                    for a in anchors if a["status"] == "stale"],
        "pathExistsAnchors": [_observation(r) for r in constraints if r["status"] == "path-exists"],
        "unpinnedAnchors": [_observation(r) for r in constraints if r["status"] == "unpinned"],
        "rejectedApproaches": [],
    }
    seen_rejected = set()
    for text, src in [(t, "user") for t in (rejected or [])] + [(r["text"], f"{r['source']}:{r['line']}") for r in dead_ends]:
        text = _clip(str(text), _HANDOVER_TEXT_LIMIT)
        if text and text.lower() not in seen_rejected:
            seen_rejected.add(text.lower())
            lists["rejectedApproaches"].append({"approach": text, "source": src})
    if digest:
        known = {(r["source"], r["line"]) for r in rules}
        lists["projectRules"] = _digest_rules(constraints, skip_sources, by_key,
                                              _digest_prose(root, [s for s in sources if s not in skip_sources], known))
        primary = {r["source"] for r in constraints if "/" not in r["source"]}
        by_src = {f"{r['source']}:{r['line']}": r for r in constraints}
        for a in lists["activeConstraints"]:  # code-verified rules earn a bonus but compete on the same scale
            a["score"] = round(_digest_score(by_src[a["source"]], primary) + 2.0, 3) if a["source"] in by_src else 2.0
        lists["activeConstraints"].sort(key=lambda a: -a["score"])
        # one entry per rule across both lists: the same rule also appears as a (clipped) copy in code-anchors.md
        key = lambda s: _digest_norm(s.rstrip("…"))[:80]
        seen_keys = set()
        for name in ("activeConstraints", "projectRules"):
            kept = []
            for a in lists[name]:
                k = key(a["rule"])
                if k not in seen_keys:
                    seen_keys.add(k)
                    kept.append(a)
            lists[name] = kept
    if digest or brief or index:  # every instruction-derived text field leaves masked, not only the special lists
        from prism_memory.memlet_brief import _mask
        for items in lists.values():
            for a in items:
                for f in ("rule", "approach"):
                    if isinstance(a.get(f), str):
                        a[f] = _mask(a[f])
    caps = {k: _HANDOVER_LIST_CAP for k in lists}
    index_meta = None
    if index:
        from prism_memory.memlet_index import QUOTE_MAX_LINES, load_verified_index
        fresh_idx, stale_idx, index_meta = load_verified_index(root)
        lists["quotedRules"] = [{"topic": e["topic"], "quote": e["quote"], "source": e["source"]}
                                for e in fresh_idx if e["importance"] == 1 and e["lines"] <= QUOTE_MAX_LINES]
        quoted = {e["source"] for e in lists["quotedRules"]}
        lists["ruleIndex"] = [{"topic": e["topic"], "source": e["source"]} for e in fresh_idx if e["source"] not in quoted]
        lists["staleIndex"] = [{"topic": e["topic"], "source": e["source"]} for e in stale_idx]
        caps.update({"quotedRules": 200, "ruleIndex": 400, "staleIndex": _DIGEST_STALE_SHOWN,
                     "quarantinedStaleAnchors": _DIGEST_STALE_SHOWN})
    brief_meta = None
    if brief:
        from prism_memory.memlet_brief import load_verified_brief
        fresh, stale_brief, brief_meta = load_verified_brief(root)
        lists["briefRules"], lists["staleBrief"] = fresh, stale_brief
        caps["briefRules"], caps["staleBrief"] = 200, _DIGEST_STALE_SHOWN
        caps["quarantinedStaleAnchors"] = _DIGEST_STALE_SHOWN
    if digest:
        caps["quarantinedStaleAnchors"] = _DIGEST_STALE_SHOWN  # the WARNING line still carries the full count
        caps["projectRules"] = 200
    if digest or brief or index:  # displayed strings only; membership above already used the real citations
        from prism_memory.memlet_brief import _mask
        for items in lists.values():
            for a in items:
                for f in ("rule", "topic", "source", "anchor", "approach"):
                    if isinstance(a.get(f), str):
                        a[f] = _mask(a[f])
                for f in ("anchors", "pathNotFound"):
                    if isinstance(a.get(f), list):
                        a[f] = [_mask(x) if isinstance(x, str) else x for x in a[f]]
    omitted = {k: max(0, len(v) - caps[k]) for k, v in lists.items()}
    lists = {k: v[:caps[k]] for k, v in lists.items()}
    counts = _rule_counts(constraints)
    proof = {
        "modifiedFilesCount": _git_modified_count(root),
        "anchorHealth": _summarize(anchors),
        "staleRulesBlocked": counts["stale"],
        "unanchoredRulesOmitted": counts["unanchored"],
    }
    next_action = _clip(next_step, _NEXT_ACTION_LIMIT) if next_step else None
    # Trim the least safety-critical items first until the rendered capsule fits the token budget. Quarantined stale
    # anchors go last and keep a floor: the next model must still learn which rules it must not follow.
    if index:  # a quote that does not fit is demoted to an index entry first; index tail goes last
        drop_order = ("reverifyBeforeUse", "activeConstraints", "quotedRules", "ruleIndex", "rejectedApproaches", "staleIndex",
                      "quarantinedStaleAnchors")
    elif brief:  # the brief is ranked by importance: its tail goes after anchored extras; safety lists are kept longest
        drop_order = ("reverifyBeforeUse", "activeConstraints", "briefRules", "rejectedApproaches", "staleBrief",
                      "quarantinedStaleAnchors")
    elif digest:  # lowest-ranked project rules go first; verified anchored rules and safety lists are kept longest
        drop_order = ("reverifyBeforeUse", "projectRules", "activeConstraints", "rejectedApproaches", "quarantinedStaleAnchors")
    else:
        drop_order = ("reverifyBeforeUse", "activeConstraints", "rejectedApproaches", "quarantinedStaleAnchors")
    drop_order = ("lexicalConstraints", "pathExistsAnchors", "unpinnedAnchors") + drop_order
    floors = {"quarantinedStaleAnchors": min(_HANDOVER_QUARANTINE_FLOOR, caps["quarantinedStaleAnchors"])}
    while True:
        capsule = {"schema": HANDOVER_SCHEMA, "repoHead": head, **lists, "executionProof": proof,
                   "nextAction": next_action, "authorityBoundary": AUTHORITY_BOUNDARY, "omitted": dict(omitted)}
        if digest:
            capsule["mode"] = "digest"
        if index:
            capsule["mode"] = "index"
            capsule["indexSource"] = ({k: index_meta.get(k) for k in ("via", "builtAt", "inputSha256")} if index_meta
                                      else None)
        if brief:
            capsule["mode"] = "brief"
            capsule["briefSource"] = ({k: brief_meta.get(k) for k in ("via", "builtAt", "inputSha256")} if brief_meta
                                      else None)
        signature = handover_signature(capsule)
        markdown = _render_handover(capsule, signature)
        tokens = estimate_tokens(markdown)
        victim = next((k for k in drop_order if len(lists[k]) > floors.get(k, 0)), None)
        if digest and victim in ("projectRules", "activeConstraints"):
            ranked = [k for k in ("projectRules", "activeConstraints") if lists[k]]
            victim = min(ranked, key=lambda k: lists[k][-1]["score"])  # drop the globally least important rule
        if tokens < token_budget:
            break
        if victim is None:
            # The floor is a preference, the budget is a contract: shrink the floor; the WARNING line keeps the count.
            if floors["quarantinedStaleAnchors"] > 0:
                floors["quarantinedStaleAnchors"] -= 1
                continue
            break
        if index and victim == "quotedRules":  # never lose a top rule: keep it reachable as an index entry
            demoted = lists[victim][-1]
            lists["ruleIndex"] = [{"topic": demoted["topic"], "source": demoted["source"]}] + lists["ruleIndex"]
        lists[victim] = lists[victim][:-1]
        omitted[victim] += 1

    actions: List[Dict[str, str]] = []
    if write:
        page = "# Memlet Handover\n\n" + markdown
        op = _write_generated_page(root, HANDOVER_PAGE, page, dry_run=False)
        actions.append({"op": op, "path": HANDOVER_PAGE})
        ledger = root / LEDGER_PATH
        if ledger.is_symlink():
            actions.append({"op": "kept", "path": LEDGER_PATH})
        else:
            existed = ledger.is_file()
            now = _utc_now()
            with contextlib.closing(sqlite3.connect(str(ledger))) as conn:
                with conn:
                    _ledger_ready(conn)
                    _sync_snapshots(conn, [a for a in anchors if a["symbol"]], now)
                    _record_page(conn, HANDOVER_PAGE, _page_body_sha256(page), "handover", now)
            actions.append({"op": "update" if existed else "create", "path": LEDGER_PATH})

    over = tokens >= token_budget
    report: Dict[str, Any] = {
        "ok": not over,
        "exitCode": 2 if over else 0,
        "command": "handover",
        "repoPath": str(root),
        **capsule,
        "capsuleSignature": signature,
        "checksum": signature,
        "markdownChecksum": _markdown_checksum(markdown)[0],
        "signedFields": list(_signed_fields(capsule)),
        "tokenEstimate": tokens,
        "tokenBudget": token_budget,
        "budgetExceeded": over,
        "written": bool(write),
        "actions": actions,
    }
    if budget_source != "default":  # additive only when a budget was chosen: the default report stays byte-identical
        report["tokenBudgetSource"] = budget_source
    if (output_format or "json").lower() == "markdown":
        report["formatted"] = markdown
    return report


# ---------------------------------------------------------------------------
# Reproducible head-to-head refactoring & handover benchmark (`memlet benchmark`).

BENCHMARK_ID = "memlet-refactoring-handover/v1"
BENCH_NOW = "2026-10-29"  # fixed clock so the unverified wiki's aging rule is reproducible
BENCH_ENTRY_DATE = "2026-10-20"
BENCH_AGING_DAYS = 90
BENCHMARK_MODES = (
    ("raw-markdown", "Raw Markdown", "Static CLAUDE.md / .cursorrules injected verbatim; no code check."),
    ("unverified-wiki", "Unverified Wiki", "Timestamped Markdown wiki with 90-day aging and a file-exists check; "
                                           "no AST, signature, or required-parameter check."),
    ("memlet", "Memlet", "AST anchors + reqSigHash pins + handover quarantine."),
)
BENCHMARK_SCENARIOS: Tuple[Dict[str, Any], ...] = (
    {
        "id": "signature-break",
        "title": "Breaking change: def charge(user_id) -> def charge(account_id, currency)",
        "before": {"billing.py": "def charge(user_id):\n    return {'user': user_id}\n"},
        "after": {"billing.py": "def charge(account_id, currency):\n    return {'account': account_id, 'currency': currency}\n"},
        "rules": ["Charge customers with `billing.py::charge` as `charge(user_id)`."],
        "stale": ["billing.py::charge"],
    },
    {
        "id": "optional-arg",
        "title": "Optional argument: def route(task) -> def route(task, effort=\"high\")",
        "before": {"router.py": "def route(task):\n    return task\n"},
        "after": {"router.py": "def route(task, effort=\"high\"):\n    return task, effort\n"},
        "rules": ["Dispatch every task through `router.py::route`."],
        "compatible": ["router.py::route"],
    },
    {
        "id": "file-move",
        "title": "File move: legacy_auth.py -> auth/session.py",
        "before": {"legacy_auth.py": "def create_session(user):\n    return user\n"},
        "after": {"legacy_auth.py": None, "auth/session.py": "def create_session(user):\n    return user\n"},
        "rules": ["Create login sessions with `legacy_auth.py::create_session`."],
        "stale": ["legacy_auth.py::create_session"],
    },
    {
        "id": "cross-vendor-handover",
        "title": "Cross-vendor handover after a refactor, with recorded dead-ends",
        "before": {"payments.py": "def refund(charge_id):\n    return charge_id\n\n\ndef legacy_retry(charge_id):\n    return refund(charge_id)\n"},
        "after": {"payments.py": "def refund(charge_id):\n    return charge_id\n"},
        "rules": ["Issue refunds via `payments.py::refund`.", "Retry failed refunds with `payments.py::legacy_retry`."],
        "stale": ["payments.py::legacy_retry"],
        "deadEnds": [
            "Retrying refunds inside the webhook handler double-charges customers.",
            "Mocking the payment gateway in integration tests hid the idempotency bug.",
        ],
        "handover": True,
    },
)


def _apply_files(root: Path, files: Dict[str, Optional[str]]) -> None:
    for rel, text in files.items():
        target = root / rel
        if text is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")


def _rate(hit: int, total: int) -> float:
    return round(hit / total, 4) if total else 0.0


def _days_between(a: str, b: str) -> int:
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


def _bench_raw(root: Path, sc: Dict[str, Any]) -> Dict[str, Any]:
    text = _read_text(root / "CLAUDE.md")
    injected = [r["text"] for r in _extract_rules(text, "CLAUDE.md")]
    return {"injected": injected, "handoverContext": text, "handoverActive": injected, "rejected": []}


def _bench_unverified_wiki(root: Path, sc: Dict[str, Any]) -> Dict[str, Any]:
    entries = [{"text": t, "updated": BENCH_ENTRY_DATE, "kind": "rule"} for t in sc["rules"]]
    entries += [{"text": t, "updated": BENCH_ENTRY_DATE, "kind": "note"} for t in sc.get("deadEnds", [])]
    kept = []
    for e in entries:
        fresh = _days_between(e["updated"], BENCH_NOW) <= BENCH_AGING_DAYS
        paths_ok = all((root / p).exists() for p, _sym in _line_refs(e["text"]))
        if fresh and paths_ok:
            kept.append(e)
    injected = [e["text"] for e in kept if e["kind"] == "rule"]
    context = "\n".join(e["text"] for e in kept)
    return {"injected": injected, "handoverContext": context, "handoverActive": injected, "rejected": []}


def _bench_memlet(root: Path, sc: Dict[str, Any]) -> Dict[str, Any]:
    imported = import_rules(root, source="claude-md", dry_run=True)
    injected = [r["text"] for r in imported["rules"] if r["status"] != "stale"]
    out: Dict[str, Any] = {"injected": injected, "handoverContext": "", "handoverActive": injected, "rejected": []}
    if sc.get("handover"):
        cap = build_handover(root, next_step="Finish the refund flow refactor.")
        out["handoverContext"] = _render_handover(cap, cap["capsuleSignature"])
        out["handoverActive"] = [a["rule"] for a in cap["activeConstraints"]]
        out["rejected"] = [a["approach"] for a in cap["rejectedApproaches"]]
        out["tokenEstimate"] = cap["tokenEstimate"]
    return out


def _score_mode(mode: str, sc: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    def mentions(texts: Iterable[str], anchor: str) -> bool:
        return any(anchor in t for t in texts)

    stale = sc.get("stale", [])
    compatible = sc.get("compatible", [])
    score = {
        "scenario": sc["id"],
        "staleInjected": sum(mentions(result["injected"], a) for a in stale),
        "staleTotal": len(stale),
        "falsePositives": sum(not mentions(result["injected"], a) for a in compatible),
        "compatibleTotal": len(compatible),
        "handoverPrevented": 0,
        "handoverHazards": 0,
    }
    if sc.get("handover"):
        hazards = 0
        prevented = 0
        for dead_end in sc.get("deadEnds", []):
            hazards += 1
            carried = dead_end in result["rejected"] if mode == "memlet" else dead_end in result["handoverContext"]
            prevented += int(carried)
        for anchor in stale:
            hazards += 1
            prevented += int(not mentions(result["handoverActive"], anchor))
        score.update(handoverPrevented=prevented, handoverHazards=hazards)
    return score


def run_refactoring_comparison_benchmark(output_format: str = "json") -> Dict[str, Any]:
    """Replay 4 refactoring scenarios in a temp dir and score raw Markdown, an unverified wiki, and Memlet.

    Deterministic apart from `elapsedMs`; no network, no LLM, nothing written outside the temp dir."""
    runners = {"raw-markdown": _bench_raw, "unverified-wiki": _bench_unverified_wiki, "memlet": _bench_memlet}
    scores: Dict[str, List[Dict[str, Any]]] = {m: [] for m, _l, _d in BENCHMARK_MODES}
    elapsed: Dict[str, float] = {m: 0.0 for m in scores}
    with tempfile.TemporaryDirectory(prefix="memlet-bench-") as td:
        for sc in BENCHMARK_SCENARIOS:
            root = Path(td, sc["id"]).resolve()
            root.mkdir()
            _apply_files(root, sc["before"])
            (root / "CLAUDE.md").write_text("# Rules\n\n" + "".join(f"- {r}\n" for r in sc["rules"]), encoding="utf-8")
            init_project(root)  # pins the pre-refactor AST / signature hashes
            if sc.get("deadEnds"):
                notes = "# Session notes\n\n## Dead-Ends\n\n" + "".join(f"- {d}\n" for d in sc["deadEnds"])
                (root / WIKI_DIR / "session.md").write_text(notes, encoding="utf-8")
            _apply_files(root, sc["after"])
            for mode, runner in runners.items():
                started = time.perf_counter()
                result = runner(root, sc)
                elapsed[mode] += (time.perf_counter() - started) * 1000
                scores[mode].append(_score_mode(mode, sc, result))

    modes = []
    for mode, label, description in BENCHMARK_MODES:
        rows = scores[mode]
        total = {k: sum(r[k] for r in rows) for k in ("staleInjected", "staleTotal", "falsePositives",
                                                       "compatibleTotal", "handoverPrevented", "handoverHazards")}
        modes.append({
            "mode": mode,
            "label": label,
            "description": description,
            "staleProcedureInjectionRate": _rate(total["staleInjected"], total["staleTotal"]),
            "staleProceduresInjected": total["staleInjected"],
            "staleProceduresTotal": total["staleTotal"],
            "optionalArgFalsePositiveRate": _rate(total["falsePositives"], total["compatibleTotal"]),
            "optionalArgFalsePositives": total["falsePositives"],
            "optionalArgEvolutionsTotal": total["compatibleTotal"],
            "handoverDeadEndPreventionRate": _rate(total["handoverPrevented"], total["handoverHazards"]),
            "handoverHazardsPrevented": total["handoverPrevented"],
            "handoverHazardsTotal": total["handoverHazards"],
            "externalLlmCalls": 0,
            "elapsedMs": round(elapsed[mode], 2),
            "perScenario": rows,
        })
    digest_src = [{k: v for k, v in m.items() if k != "elapsedMs"} for m in modes]
    report: Dict[str, Any] = {
        "ok": True,
        "exitCode": 0,
        "command": "benchmark",
        "benchmark": BENCHMARK_ID,
        "fixture": f"self-contained temp dir, {len(BENCHMARK_SCENARIOS)} scenarios, fixed clock {BENCH_NOW}",
        "scenarios": [{"id": s["id"], "title": s["title"], "staleAnchors": s.get("stale", []),
                       "compatibleAnchors": s.get("compatible", []), "deadEnds": len(s.get("deadEnds", []))}
                      for s in BENCHMARK_SCENARIOS],
        "modes": modes,
        "externalLlmCalls": 0,
        "resultDigest": "sha256:" + hashlib.sha256(json.dumps(digest_src, sort_keys=True).encode("utf-8")).hexdigest(),
    }
    if (output_format or "json").lower() == "markdown":
        report["formatted"] = format_benchmark_markdown(report)
    return report


def format_benchmark_markdown(report: Dict[str, Any]) -> str:
    def pct(rate: float, hit: int, total: int) -> str:
        return f"{rate * 100:.1f}% ({hit}/{total})".replace(".0%", "%")

    lines = [
        "## Memlet Refactoring & Handover Benchmark",
        "",
        f"- **Benchmark**: `{report['benchmark']}` ({report['fixture']})",
        f"- **External LLM calls**: {report['externalLlmCalls']}",
        f"- **Result digest**: `{report['resultDigest']}`",
        "",
        "| Mode | Stale procedure injection | Optional-arg false positives | Handover dead-end prevention | Elapsed |",
        "| :--- | :---: | :---: | :---: | ---: |",
    ]
    for m in report["modes"]:
        lines.append(
            f"| {m['label']} | {pct(m['staleProcedureInjectionRate'], m['staleProceduresInjected'], m['staleProceduresTotal'])} | "
            f"{pct(m['optionalArgFalsePositiveRate'], m['optionalArgFalsePositives'], m['optionalArgEvolutionsTotal'])} | "
            f"{pct(m['handoverDeadEndPreventionRate'], m['handoverHazardsPrevented'], m['handoverHazardsTotal'])} | "
            f"{m['elapsedMs']:.1f} ms |"
        )
    lines += ["", "Scenarios:", ""] + [f"{i}. {s['title']}" for i, s in enumerate(report["scenarios"], start=1)]
    lines += ["", "Modes:", ""] + [f"- **{m['label']}**: {m['description']}" for m in report["modes"]]
    return "\n".join(lines) + "\n"
