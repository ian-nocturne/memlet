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

from .vocabulary import (HINT as TIER_HINT, REJECTED as TIER_REJECTED)
from .vocabulary import suggested_label

import contextlib
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .audit import (BACKTICK_FILE_RE, EXCLUDED_DIRS, EXPLICIT_ANCHOR_RE, IGNORED_FILE_TOKENS, LazyBlockPattern, SpanMatch,
                    audit_repository, iter_explicit_anchors)
from .code_anchors import (NEW_PIN, PYTHON_HASH_BASIS, TS_LEXICAL_BASIS, exact_symbol_fingerprints, extract_file_symbol_hashes,
                           extract_file_symbol_signatures, previous_python_symbol_hashes)

MEMLET_VERSION = "9.2.0"
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
# Generated status blocks (lazy BEGIN...END), matched in linear time: an unclosed opener is never retried against the
# rest of the page (20,000 unclosed markers took seconds with the plain lazy regex).
_STATUS_BLOCK_RE = LazyBlockPattern(r"<!-- memlet:status:(?:begin|start) -->.*?" + re.escape(STATUS_END),
                                    re.compile(r"<!-- memlet:status:(?:begin|start) -->"), re.compile(re.escape(STATUS_END)),
                                    STATUS_END)
_PAGE_STATUS_RE = LazyBlockPattern(re.escape(PAGE_STATUS_BEGIN) + r".*?" + re.escape(PAGE_STATUS_END) + r"\n?",
                                   re.compile(re.escape(PAGE_STATUS_BEGIN)), re.compile(re.escape(PAGE_STATUS_END)),
                                   PAGE_STATUS_END, suffix="\n")
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
_NESTED_MAX_DIRS = 3  # nested instruction files at most 3 directories below the root


def slim_output_files(root: Path) -> List[str]:
    """`management/agent-*.md` files that `memlet slim-plan` produced (exact name + its generated first line), so rules
    it moved out of AGENTS.md stay indexed, recalled and handed over. Any other `management/agent-*.md` is ignored."""
    from .memlet_slim import SLIM_OUTPUTS
    out = []
    for rel, header in sorted(SLIM_OUTPUTS.items()):
        p = root / rel
        if contained_path_problem(root, rel) or not p.is_file():
            continue
        text = read_regular_text(p, _MAX_SOURCE_BYTES)
        if text is None:
            continue
        first = text.split("\n", 1)[0].rstrip("\r")
        if first == header:
            out.append(rel)
    return out


def instruction_path_shape(rel: str) -> bool:
    """Would `discover_instruction_files` accept a file at `rel` (by path alone: names, globs, depth, excluded and
    scratch folders, secrets)? Existence, symlinks, nested repositories and slim ownership are checked by
    `is_instruction_path`."""
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    parts = rel.split("/")
    if not rel or rel.startswith("/") or ".." in parts or "" in parts or _is_secret_path(rel):
        return False
    if rel in TOP_INSTRUCTION_FILES:
        return True
    for pat in INSTRUCTION_GLOBS:
        pp = pat.split("/")
        if len(pp) == len(parts) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(parts, pp)):
            return True
    from .memlet_slim import SLIM_OUTPUTS
    if rel in SLIM_OUTPUTS:
        return True
    dirs = parts[:-1]
    return (parts[-1] in NESTED_INSTRUCTION_NAMES and len(dirs) <= _NESTED_MAX_DIRS
            and not any(d in EXCLUDED_DIRS or d == MEMLET_DIR or d in _SCRATCH_DIRS for d in dirs))


def is_instruction_path(root: Path, rel: str) -> bool:
    """`discover_instruction_files(root)` membership for one path without walking the tree (for fast hooks)."""
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    if not instruction_path_shape(rel):
        return False
    p = root / rel
    try:
        # one containment rule for fixed names, globs and nested files alike: no symlink in any component (an
        # instruction directory linked to text outside the repository is never read), a regular file inside root
        if contained_path_problem(root, rel) or not p.is_file():
            return False
    except OSError:
        return False
    from .memlet_slim import SLIM_OUTPUTS
    if rel in SLIM_OUTPUTS:
        return rel in slim_output_files(root)
    parts = rel.split("/")
    if rel not in TOP_INSTRUCTION_FILES and not any(
            len(pat.split("/")) == len(parts) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(parts, pat.split("/")))
            for pat in INSTRUCTION_GLOBS):
        cur = root
        for d in parts[:-1]:  # discovery never descends into symlinked folders or nested repositories
            cur = cur / d
            if cur.is_symlink() or (cur / ".git").exists():
                return False
    return True


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
            rel = _rel(root, p)
            # the same containment rule as `is_instruction_path`: no symlinked component anywhere on the path
            if contained_path_problem(root, rel) or not p.is_file():
                return
        except (OSError, ValueError):
            return
        if not _is_secret_path(rel):
            found.add(rel)

    for rel in TOP_INSTRUCTION_FILES:
        _add(root / rel)
    for pat in INSTRUCTION_GLOBS:
        for p in root.glob(pat):
            _add(p)
    for rel in slim_output_files(root):  # only files `memlet slim-plan` produced, never any management/agent-*.md
        _add(root / rel)
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


# ---------------------------------------------------------------------------
# One rule for every repository path Memlet reads or writes.
#
# * Containment (`contained_path_problem`): every component from the repository root down to the leaf is checked
#   with lstat. No component may be a symbolic link, every ancestor must be a real directory, an existing leaf must
#   be a regular file (or a real directory when one is asked for) and the location must resolve inside the root.
#   Instruction discovery, wiki pages, audit discovery and every Memlet-managed write use this one check.
# * Reads (`read_regular_text`): open without following a leaf symlink and without blocking (a FIFO is refused, not
#   waited on), verify a regular file with fstat and read at most a size cap.
# * Managed writes (`write_managed_text` / `write_managed_bytes` / `connect_managed_sqlite`): only below `.memlet/`,
#   refused (nothing written) when the containment check fails; text goes to a temp file in the same directory and
#   replaces the target atomically, so a leaf that turns into a symlink is replaced, never followed.

MAX_TEXT_READ_BYTES = 8 * 1024 * 1024
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_WIKI_PAGES = 500
MAX_WIKI_PAGE_BYTES = 1_000_000
MAX_WIKI_TOTAL_BYTES = 16 * 1024 * 1024
MAX_SOURCE_TOTAL_BYTES = 16 * 1024 * 1024


class UnsafePathError(ValueError):
    """A Memlet-managed location is a symlink, not a regular file / real directory, or outside the repository."""


class ConcurrentChangeError(ValueError):
    """`write_managed_bytes(..., if_match=...)`: the file changed since the caller read it; nothing was written."""


def content_tag(data: Optional[bytes]) -> str:
    """The `if_match` value for bytes a caller read (`"absent"` when the file did not exist)."""
    return "absent" if data is None else "sha256:" + hashlib.sha256(data).hexdigest()


def _rel_parts(rel: Any) -> Optional[Tuple[str, ...]]:
    rel = str(rel).replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    parts = tuple(rel.split("/"))
    if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts):
        return None
    return parts


def contained_path_problem(root: Path, rel: Any, *, leaf: str = "file") -> Optional[str]:
    """None when `root/rel` is a safe in-repository location, else why not (see the block comment above).
    A location that does not exist yet is fine as long as its existing ancestors are."""
    parts = _rel_parts(rel)
    if parts is None:
        return f"unsafe repository path: {str(rel)[:200]!r}"
    cur, exists = Path(root), True
    for i, part in enumerate(parts):
        cur = cur / part
        shown = "/".join(parts[:i + 1])
        try:
            st = os.lstat(str(cur))
        except FileNotFoundError:
            exists = False
            break
        except OSError as exc:
            return f"cannot inspect {shown}: {exc.strerror or exc}"
        if stat.S_ISLNK(st.st_mode):
            return f"refusing symlinked path {shown}"
        if i < len(parts) - 1 or leaf == "dir":
            if not stat.S_ISDIR(st.st_mode):
                return f"{shown} is not a directory"
        elif not stat.S_ISREG(st.st_mode):
            return f"{shown} is not a regular file"
    try:
        (cur if exists else cur.parent).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError, RuntimeError):
        return f"{'/'.join(parts)} resolves outside the repository"
    return None


def managed_path_problem(root: Path, rel: Any, *, leaf: str = "file") -> Optional[str]:
    """`contained_path_problem` for a location Memlet owns: it must be `.memlet` or below it."""
    parts = _rel_parts(rel)
    if parts is None or parts[0] != MEMLET_DIR:
        return f"not a Memlet-managed path: {str(rel)[:200]!r}"
    return contained_path_problem(root, rel, leaf=leaf)


def first_managed_problem(root: Path, rels: Iterable[str]) -> Optional[str]:
    for rel in rels:
        problem = managed_path_problem(root, rel, leaf="dir" if rel in (MEMLET_DIR, WIKI_DIR) else "file")
        if problem:
            return problem
    return None


def read_regular_text(p: Path, limit: int = MAX_TEXT_READ_BYTES, *, errors: str = "ignore") -> Optional[str]:
    """Text of a regular file of at most `limit` bytes, else None. A symlinked leaf is not followed and a FIFO or
    device is refused without blocking (O_NONBLOCK, then fstat)."""
    data = read_regular_bytes(p, limit)
    return None if data is None else data.decode("utf-8", errors=errors)


def read_regular_bytes(p: Path, limit: int = MAX_TEXT_READ_BYTES) -> Optional[bytes]:
    """Bytes of a regular file of at most `limit` bytes, else None (see `read_regular_text`)."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(p), flags)
    except (OSError, ValueError):
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        chunks, total = [], 0
        while True:
            b = os.read(fd, min(1 << 20, limit + 1 - total))
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if total > limit:
                return None
    except OSError:
        return None
    finally:
        os.close(fd)
    return b"".join(chunks)


def read_managed_text(root: Path, rel: str, limit: int = MAX_TEXT_READ_BYTES) -> Optional[str]:
    """`read_regular_text` of a Memlet-managed file, None when it is missing, unsafe, unreadable or over `limit`."""
    if managed_path_problem(root, rel):
        return None
    return read_regular_text(root / rel, limit)


def read_config(root: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(`.memlet/config.json` as an object, error). (None, None) when it does not exist. Never follows a symlink,
    never blocks on a FIFO, never reads more than MAX_CONFIG_BYTES."""
    problem = managed_path_problem(root, CONFIG_PATH)
    if problem:
        return None, ("refusing a symlinked .memlet/config.json" if "symlink" in problem and "config.json" in problem
                      else problem)
    p = root / CONFIG_PATH
    if not os.path.lexists(str(p)):
        return None, None
    text = read_regular_text(p, MAX_CONFIG_BYTES)
    if text is None:
        return None, f".memlet/config.json is unreadable or larger than {MAX_CONFIG_BYTES} bytes"
    try:
        cfg = json.loads(text)
    except ValueError as exc:
        return None, f".memlet/config.json is unreadable: {exc}"
    return (cfg, None) if isinstance(cfg, dict) else (None, ".memlet/config.json is not an object")


def _ensure_managed_dir(root: Path, parts: Tuple[str, ...]) -> Path:
    cur = Path(root)
    for i, part in enumerate(parts):
        cur = cur / part
        try:
            os.mkdir(str(cur))
        except FileExistsError:
            pass
        st = os.lstat(str(cur))
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise UnsafePathError(f"refusing {'/'.join(parts[:i + 1])}: not a real directory")
    return cur


def write_managed_bytes(root: Path, rel: str, data: bytes, *, create_only: bool = False,
                        new_mode: int = 0o644, if_match: Optional[str] = None) -> int:
    """Write `data` to `root/rel` (below `.memlet/`), refusing (UnsafePathError, nothing written) when the location
    fails `managed_path_problem`. Replaces atomically through a temp file in the same directory and keeps an existing
    file's mode; `create_only` creates a new file exclusively (O_EXCL | O_NOFOLLOW) instead.

    `if_match` (optional; default None keeps the unconditional behaviour): the `content_tag` of the bytes the caller
    read. The write is refused (ConcurrentChangeError, nothing written) when the file changed since it was read; that
    is checked again immediately before the replace, but this is not an atomic compare-and-swap: a writer that does
    not take Memlet's lock can still race the final step."""
    problem = managed_path_problem(root, rel)
    if problem:
        raise UnsafePathError(problem)
    parts = _rel_parts(rel)
    parent = _ensure_managed_dir(root, parts[:-1])
    target = parent / parts[-1]
    if create_only:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), new_mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return len(data)
    try:
        mode = stat.S_IMODE(os.lstat(str(target)).st_mode)
    except FileNotFoundError:
        mode = new_mode
    fd, tmp = tempfile.mkstemp(prefix="." + parts[-1][:40] + ".memlet-", dir=str(parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        problem = managed_path_problem(root, rel)  # re-checked just before the replace
        if problem:
            raise UnsafePathError(problem)
        if if_match is not None:
            current = read_regular_bytes(target, 64 * 1024 * 1024) if os.path.lexists(str(target)) else None
            if (os.path.lexists(str(target)) and current is None) or content_tag(current) != if_match:
                raise ConcurrentChangeError(f"{rel} changed since Memlet read it")
        os.replace(tmp, str(target))  # renames over the leaf: a symlink there would be replaced, never followed
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return len(data)


def write_managed_text(root: Path, rel: str, text: str, **kw: Any) -> int:
    return write_managed_bytes(root, rel, text.encode("utf-8"), **kw)


def managed_target(target: Path) -> Tuple[Path, str]:
    """(repository root, `.memlet/...` relative path) of an absolute path below a `.memlet` directory."""
    parts = Path(target).parts
    idx = [i for i, p in enumerate(parts) if p == MEMLET_DIR]
    if not idx:
        raise UnsafePathError(f"not a Memlet-managed path: {target}")
    return Path(*parts[:idx[-1]]), "/".join(parts[idx[-1]:])


def write_managed_path(target: Path, text: str, **kw: Any) -> int:
    root, rel = managed_target(target)
    return write_managed_text(root, rel, text, **kw)


def connect_managed_sqlite(root: Path, rel: str, *, readonly: bool = False, **kw: Any) -> sqlite3.Connection:
    """sqlite3 connection to a Memlet-managed database, after checking the database and its journal/WAL/SHM side files
    (none may be a symlink or a non-regular file). `readonly` never creates anything."""
    for suffix in ("", "-journal", "-wal", "-shm"):
        problem = managed_path_problem(root, rel + suffix)
        if problem:
            raise UnsafePathError(problem)
    path = root / rel
    if readonly:
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, **kw)
    _ensure_managed_dir(root, _rel_parts(rel)[:-1])
    return sqlite3.connect(str(path), **kw)


def iter_wiki_pages(root: Path) -> List[Tuple[str, str]]:
    """(repo-relative path, text) of the Markdown pages under `.memlet/wiki/`, in path order, each passing the
    containment rule. Bounded: at most MAX_WIKI_PAGES pages, MAX_WIKI_PAGE_BYTES per page (a larger page is skipped)
    and MAX_WIKI_TOTAL_BYTES in total (later pages are skipped)."""
    if managed_path_problem(root, WIKI_DIR, leaf="dir") or not (root / WIKI_DIR).is_dir():
        return []
    out: List[Tuple[str, str]] = []
    total = 0
    for p in sorted((root / WIKI_DIR).rglob("*.md")):
        if len(out) >= MAX_WIKI_PAGES:
            break
        try:
            rel = _rel(root, p)
        except ValueError:
            continue
        if contained_path_problem(root, rel):
            continue
        text = read_regular_text(p, MAX_WIKI_PAGE_BYTES)
        if text is None:
            continue
        total += len(text)
        if total > MAX_WIKI_TOTAL_BYTES:
            break
        out.append((rel, text))
    return out


def trusted_wiki_pages(root: Path) -> List[Tuple[str, str]]:
    """`iter_wiki_pages` for readers that present rules or pins as verified (handover, changed-file check): the
    Memlet-owned promoted Dream page is left out entirely (rules and pins) unless it is exactly what Memlet last
    wrote there (ledger hash), the same ownership rule promotion applies before overwriting it. A hand edit of that
    page, e.g. a re-written anchor comment, is not trusted until an explicit `accept` / promotion records it."""
    out = []
    for rel, text in iter_wiki_pages(root):
        if rel == DREAM_PROMOTED_PAGE:
            from memlet.memlet_bridge import _page_edit_problem
            if _page_edit_problem(root, text, for_reader=True):
                continue
        out.append((rel, text))
    return out


def memlet_write_lock(root: Path):
    """The one lock every writer of Memlet's wiki state takes (config pins, wiki pages, the promoted Dream page and
    the ledger): the Dream queue lock, so anchor accept, init / wiki / import / handover --write and Dream
    promotion are serialised against each other. Re-entrant within a thread."""
    from memlet import memlet_dream as md
    return md.queue_lock(root / MEMLET_DIR / "dream" / "APPROVAL-QUEUE.json")


def _read_text(p: Path, limit: int = MAX_TEXT_READ_BYTES) -> str:
    """Text of a regular file (never a FIFO, never through a symlinked leaf), "" when unreadable or over `limit`."""
    text = read_regular_text(p, limit)
    return "" if text is None else text


def _extract_anchors(text: str, source: str) -> List[Dict[str, Any]]:
    """Find `path::Symbol` anchors and `memlet:anchor` pins in a Markdown document."""
    out: List[Dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for m in PIN_RE.finditer(line):
            out.append({"path": m.group("path"), "symbol": m.group("symbol"), "source": source,
                        "line": line_no, "pin": {"ast": m.group("ast"), "sig": m.group("sig"), "req": m.group("req"), "basis": m.group("basis")}})
        scrubbed = PIN_RE.sub("", line)
        for m in iter_explicit_anchors(scrubbed):
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


def exact_parser_opt_in(root: Path) -> List[str]:
    """Exact parsers this repository opted into for NEW TS/JS pins: `.memlet/config.json` `anchors.exactParsers`
    (e.g. ["tree-sitter"]). Empty by default; installing a parser package alone never changes how anything is pinned."""
    cfg, _err = read_config(root)
    anchors = cfg.get("anchors") if isinstance(cfg, dict) else None
    names = anchors.get("exactParsers") if isinstance(anchors, dict) else None
    return [n for n in names if isinstance(n, str) and n][:8] if isinstance(names, list) else []


def _current_fingerprint(root: Path, rel_path: str, symbol: str, basis: Any = NEW_PIN) -> Dict[str, Any]:
    """Resolve an anchor against the working tree; returns resolution plus current hashes.

    TS/JS: `basis` is the hash basis of the pin being checked, `NEW_PIN` for a pin that does not exist yet. A new pin
    is lexical unless the repository opted into an exact parser (`exact_parser_opt_in`). A lexical pin, and a legacy
    pin stored without a basis (`None` / "legacy", never confused with `NEW_PIN`), is always re-checked lexically. A
    pin made by an exact provider is re-checked only by that same provider (same basis name); when it is unavailable
    the result carries `basisUnavailable` and is never compared."""
    if rel_path.startswith("/") or ".." in Path(rel_path).parts or _is_secret_path(rel_path):
        return {"resolution": TIER_REJECTED, "reason": "path_outside_repo_or_secret"}
    target = root / rel_path
    try:
        target.resolve().relative_to(root)
    except (OSError, ValueError):
        return {"resolution": TIER_REJECTED, "reason": "path_outside_repo_or_secret"}
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
    if target.suffix.lower() == ".py":
        from .code_anchors import python_symbol_rebound
        if python_symbol_rebound(target, symbol):
            return {"resolution": "rebound", "reason": "symbol_rebound_in_module"}
    ast_hash = (extract_file_symbol_hashes(target).get(symbol) or sig.get("normAstHash") or sig.get("sigHash") or "")
    language = sig_info.get("__language__")
    lexical_ts = language in ("typescript", "javascript")
    fp_basis = TS_LEXICAL_BASIS if lexical_ts else PYTHON_HASH_BASIS if language == "python" else "contract-v1"
    hash_exact, hash_reason, sig_hash, basis_unavailable = not lexical_ts, "ts_js_lexical_scan_unverified", sig.get("sigHash") or "", None
    if lexical_ts and basis is not NEW_PIN and basis not in (None, "legacy", TS_LEXICAL_BASIS):
        exact_basis, exact = exact_symbol_fingerprints(target, language, basis=basis)
        if exact_basis != basis or exact is None or symbol not in exact:
            basis_unavailable = basis  # the parser that made this pin cannot re-check it now
        else:
            fp_basis, hash_exact, hash_reason = exact_basis, True, "exact_parser"
            ast_hash, sig_hash = exact[symbol]["ast"], exact[symbol]["sig"] or sig_hash
    elif lexical_ts and basis is NEW_PIN and exact_parser_opt_in(root):
        exact_basis, exact = exact_symbol_fingerprints(target, language, allowed=exact_parser_opt_in(root))
        if exact and symbol in exact:
            fp_basis, hash_exact, hash_reason = exact_basis, True, "exact_parser"
            ast_hash, sig_hash = exact[symbol]["ast"], exact[symbol]["sig"] or sig_hash
    out = {
        "resolution": "exact",
        "basis": fp_basis,
        "hashExact": hash_exact,
        "hashReason": hash_reason,
        "ast": ast_hash[:_HASH_LEN],
        "sig": sig_hash[:_HASH_LEN],
        "req": _req_sig_hash(sig),
        "types": sig.get("annotationHash"),
        "shape": hashlib.sha256(json.dumps({k: sig.get(k) for k in ("allArgs", "kwonlyAll", "hasVararg", "hasKwarg")}, sort_keys=True).encode()).hexdigest()[:_HASH_LEN],
    }
    if basis_unavailable:
        out["basisUnavailable"] = basis_unavailable
    return out


def _same_hash(a: Optional[str], b: Optional[str]) -> bool:
    n = min(len(a or ""), len(b or ""), _HASH_LEN)
    return n >= 6 and a[:n] == b[:n]


def classify_anchor(root: Path, anchor: Dict[str, Any]) -> Dict[str, Any]:
    pin = anchor.get("pin")
    # a stored pin is checked under its own basis (None = a legacy pin); no pin at all is NEW_PIN
    cur = _current_fingerprint(root, anchor["path"], anchor["symbol"], basis=pin.get("basis") if pin else NEW_PIN)
    # Optional diagnostic metadata is used only when it belongs to exactly this baseline.
    if pin and not pin.get("types"):
        cp = _load_config_pins(root).get(f"{anchor['path']}::{anchor['symbol']}") or {}
        if _same_hash(pin.get("ast"), cp.get("astHash")):
            pin = dict(pin, types=cp.get("annotationHash"), shape=cp.get("parameterShapeHash"))
    annotation_changed = bool(pin and pin.get("types") and cur.get("types")
                              and not _same_hash(pin["types"], cur["types"]))
    same_shape = bool(pin and _same_hash(pin.get("shape"), cur.get("shape")))
    legacy_match = False
    if pin and cur.get("ast") and not _same_hash(cur["ast"], pin.get("ast")) and Path(anchor["path"]).suffix.lower() == ".py":
        # a pin made with a previous Python basis (`python-ast-v2`, pre-v2 `legacy`) still matches when that basis,
        # recomputed on this interpreter, gives the pinned hash: unchanged code never turns stale by a basis bump
        legacy_match = any(_same_hash(h.get(anchor["symbol"]), pin.get("ast"))
                           for h in previous_python_symbol_hashes(root / anchor["path"]))
    if cur["resolution"] in ("missing", "parse_error", TIER_REJECTED):
        status, reason = "stale", cur["reason"]
    elif cur["resolution"] != "exact":
        status, reason = "compatible-unverified", cur["reason"]
    elif not pin or not pin.get("ast"):
        status, reason = "unpinned", "no_stored_pin"
    elif cur.get("basisUnavailable"):
        # pinned by an exact parser that is absent now (or a different version): never compared with another basis
        status, reason = "compatible-unverified", "exact_parser_basis_repin_required"
    elif _same_hash(cur["ast"], pin.get("ast")) or legacy_match:
        if cur.get("hashExact", True):
            status, reason = "active", "legacy_ast_hash_match" if legacy_match else "ast_hash_match"
        else:
            status, reason = "compatible-unverified", cur["hashReason"]
    elif not cur.get("hashExact", True) and pin.get("basis") in (None, "legacy"):
        status, reason = "compatible-unverified", "legacy_ts_basis_repin_required"
    elif annotation_changed and same_shape and _same_hash(pin.get("req"), cur.get("req")):
        status, reason = "compatible-unverified", "parameter_type_or_annotation_changed_signature_compatible"
    elif not (pin.get("sig") and cur["sig"]) or _same_hash(cur["sig"], pin["sig"]):
        status, reason = "compatible-unverified", ("body_changed_signature_compatible" if pin.get("types")
                                                    else "code_changed_signature_compatible")
    elif pin.get("req") and _same_hash(cur.get("req"), pin["req"]):
        status, reason = "compatible-unverified", ("optional_args_added_signature_compatible" if pin.get("shape") and not same_shape
                                                    else "signature_changed_required_arguments_compatible")
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
    try:
        root, rel = managed_target(ledger)
        with contextlib.closing(connect_managed_sqlite(root, rel, readonly=True)) as conn:
            return _last_page_hashes(conn)
    except (sqlite3.Error, OSError, ValueError):
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
    config, _err = read_config(root)
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


_LEDGER_FILES = (LEDGER_PATH, LEDGER_PATH + "-journal", LEDGER_PATH + "-wal", LEDGER_PATH + "-shm")
_WIKI_STATE_PATHS = (MEMLET_DIR, CONFIG_PATH, WIKI_DIR, INDEX_PATH) + _LEDGER_FILES


def _refused(command: str, root: Path, problem: str, dry_run: bool = False, output_format: str = "json",
             **extra: Any) -> Dict[str, Any]:
    """The clean refusal of a wiki mutation whose Memlet-managed location is unsafe: nothing was written."""
    error = f"{problem}; nothing was written (Memlet never writes through a symlink or outside the repository)"
    report = {"ok": False, "exitCode": 2, "command": command, "dryRun": bool(dry_run), "repoPath": str(root),
              "error": error, "actions": []}
    report.update(extra)
    if (output_format or "json").lower() == "markdown":
        report["formatted"] = f"## Memlet {command.capitalize()}\n\n- ERROR: {error}\n"
    return report


def _guarded(command: str, root: Path, dry_run: bool, output_format: str, paths: Iterable[str], fn):
    """Run a wiki mutation: refuse up front when any fixed managed path is unsafe, then hold the Memlet write lock
    (shared with Dream promotion and anchor accept) for the whole read-modify-write, unless it is a dry run."""
    problem = first_managed_problem(root, paths)
    if problem:
        return _refused(command, root, problem, dry_run, output_format)
    try:
        if dry_run:
            return fn()
        with memlet_write_lock(root):
            return fn()
    except UnsafePathError as exc:
        return _refused(command, root, str(exc), dry_run, output_format)


def init_project(repo_path=".", *, dry_run: bool = False, force: bool = False, output_format: str = "json") -> Dict[str, Any]:
    """`memlet init`: onboard a repo into `.memlet/` (read-only when `dry_run`). Refused (ok false, nothing written)
    when `.memlet`, its config, wiki, index or ledger is a symlink or not a regular file / real directory."""
    root = _resolve_root(repo_path)
    return _guarded("init", root, dry_run, output_format, _WIKI_STATE_PATHS,
                    lambda: _init_project(root, dry_run=dry_run, force=force, output_format=output_format))


def _init_project(root: Path, *, dry_run: bool, force: bool, output_format: str) -> Dict[str, Any]:
    head = _git_head(root)
    instruction_files = discover_instruction_files(root)
    raw: List[Dict[str, Any]] = []
    for rel, src_text in _source_texts(root, instruction_files):
        raw.extend(_extract_anchors(src_text, rel))
    config_path = root / CONFIG_PATH
    config = _base_config(head, instruction_files)
    backup = None
    if config_path.is_file():
        try:
            existing_text = read_regular_text(config_path, MAX_CONFIG_BYTES)
            if existing_text is None:
                raise ValueError(f"not a regular file of at most {MAX_CONFIG_BYTES} bytes")
            existing = json.loads(existing_text)
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
    for page_rel, page_text in iter_wiki_pages(root):
        for a in _extract_anchors(page_text, page_rel):
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
            pin = ({"ast": cur["ast"], "sig": cur["sig"] or None, "req": cur["req"], "basis": cur["basis"], "types": cur.get("types"), "shape": cur.get("shape")}
                   if cur["resolution"] == "exact" and cur.get("ast") else None)
            if pin:
                pinned_now.add(key)
        elif classify_anchor(root, dict(a, pin=pin))["reason"] == "legacy_ast_hash_match":
            pin = {"ast": cur["ast"], "sig": cur["sig"] or None, "req": cur["req"], "basis": cur["basis"], "types": cur.get("types"), "shape": cur.get("shape")}
        anchors.append(dict(a, pin=pin))
        if pin and (key not in stored or force):
            stored[key] = dict(cp, astHash=pin["ast"], sigHash=pin.get("sig"), reqSigHash=pin.get("req"), hashBasis=pin.get("basis") or "legacy",
                               annotationHash=pin.get("types"), parameterShapeHash=pin.get("shape"))
        elif force:
            stored.pop(key, None)
        elif cp.get("astHash"):
            stored_pin = {"ast": cp["astHash"], "sig": cp.get("sigHash"), "req": cp.get("reqSigHash"), "basis": cp.get("hashBasis")}
            if classify_anchor(root, dict(a, pin=stored_pin))["reason"] == "legacy_ast_hash_match":
                stored[key] = dict(cp, astHash=cur["ast"], sigHash=cur["sig"], reqSigHash=cur["req"], hashBasis=cur["basis"], annotationHash=cur.get("types"), parameterShapeHash=cur.get("shape"))
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
            data = read_regular_bytes(config_path, MAX_CONFIG_BYTES * 4)
            if data is None:
                raise UnsafePathError(".memlet/config.json is not a regular file of a size Memlet backs up")
            write_managed_bytes(root, _rel(root, backup), data, create_only=True)
        actions.append({"op": "would-backup" if dry_run else "backup", "path": _rel(root, backup)})

    plan = [(CONFIG_PATH, config_text, True), (INDEX_PATH, index_text, force or index_additions)]
    for rel, text, overwrite in plan:
        target = root / rel
        exists = target.is_file()
        if exists and _read_text(target) == text:
            op = "unchanged"
        elif exists and not overwrite:
            op = "kept"
        else:
            op = "update" if exists else "create"
            if not dry_run:
                write_managed_text(root, rel, text)
        if dry_run and op in ("create", "update"):
            op = "would-" + op
        actions.append({"op": op, "path": rel})

    pin_migrations = {}
    for page_rel, old in iter_wiki_pages(root):  # read after the writes above: INDEX.md may just have been written
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
                write_managed_text(root, page_rel, new)
                pin_migrations[page_rel] = (_page_body_sha256(old), _page_body_sha256(new))
            actions.append({"op": "would-update" if dry_run else "update", "path": page_rel})

    ledger = root / LEDGER_PATH
    index_written = any(a["path"] == INDEX_PATH and a["op"] in ("create", "update") for a in actions)
    if dry_run:
        op = "would-update" if ledger.is_file() else "would-create"
    else:
        existed = ledger.is_file()
        now = _utc_now()
        with contextlib.closing(connect_managed_sqlite(root, LEDGER_PATH)) as conn:
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
        "next": ["memlet admin wiki", "memlet check"],
    }
    hint = _anchor_suggestion_hint(root)
    if hint:  # additive, only when there is something to suggest; init never accepts a suggestion itself
        report["anchorSuggestions"] = hint
    if (output_format or "json").lower() == "markdown":
        report["formatted"] = _format_markdown("Memlet Init" + (" (dry run)" if dry_run else ""), report)
        if hint:
            report["formatted"] += f"- {hint['rules']} rule(s) could be anchored to code: `{hint['command']}`\n"
    return report


def _anchor_suggestion_hint(root: Path) -> Optional[Dict[str, Any]]:
    try:
        from .memlet_anchors import count_suggestions
        n = count_suggestions(root)
    except Exception:  # onboarding must not fail on a suggestion edge case
        return None
    return {"rules": n, "command": "memlet admin anchors suggest"} if n else None


def repin_legacy_ts_anchors(repo_path=".", *, dry_run: bool = False) -> Dict[str, Any]:
    """Explicitly re-pin only legacy TS/JS baselines; versioned stale pins stay untouched.

    This accepts the current code as the new lexical baseline, not verification
    of its behavior. The dry run previews identical selections without writes.
    """
    root = _resolve_root(repo_path)
    return _guarded("repin", root, dry_run, "json", _WIKI_STATE_PATHS, lambda: _repin_legacy_ts(root, dry_run))


def _repin_legacy_ts(root: Path, dry_run: bool) -> Dict[str, Any]:
    config_path = root / CONFIG_PATH
    config, err = read_config(root)
    if err:
        raise ValueError(f"memlet config must be a JSON object ({err})")
    if config is None:
        config = _base_config(_git_head(root), [])
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
        return {"ast": cur["ast"], "sig": cur["sig"], "req": cur["req"], "basis": cur["basis"], "types": cur.get("types"), "shape": cur.get("shape")}

    updated = dict(config.get("pinnedAnchors") or {}) if isinstance(config.get("pinnedAnchors", {}), dict) else {}
    for key, cp in pins.items():
        pin = replacement(key, cp.get("hashBasis"))
        if pin:
            updated[key] = dict(cp, astHash=pin["ast"], sigHash=pin["sig"], reqSigHash=pin["req"], hashBasis=pin["basis"], annotationHash=pin.get("types"), parameterShapeHash=pin.get("shape"))
    pages = []
    for page_rel, before in iter_wiki_pages(root):
        page = root / page_rel

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
            write_managed_text(root, CONFIG_PATH, json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        actions.append({"op": "would-update" if dry_run else "update", "path": CONFIG_PATH})
    for page, before, after in pages:
        if not dry_run:
            write_managed_text(root, _rel(root, page), after)
        actions.append({"op": "would-update" if dry_run else "update", "path": _rel(root, page)})
    if pages and not dry_run:
        with contextlib.closing(connect_managed_sqlite(root, LEDGER_PATH)) as conn:
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
    if not (wiki_dir.is_dir() or os.path.lexists(str(wiki_dir))) or dry_run:
        return _wiki_status(root, dry_run=dry_run, output_format=output_format)
    return _guarded("wiki", root, dry_run, output_format, _WIKI_STATE_PATHS,
                    lambda: _wiki_status(root, dry_run=dry_run, output_format=output_format))


def _wiki_status(root: Path, *, dry_run: bool, output_format: str) -> Dict[str, Any]:
    wiki_dir = root / WIKI_DIR
    initialized = wiki_dir.is_dir() and not managed_path_problem(root, WIKI_DIR, leaf="dir")
    raw: List[Dict[str, Any]] = []
    pages: List[str] = []
    texts: Dict[str, str] = {}
    if initialized:
        for rel, text in iter_wiki_pages(root):
            pages.append(rel)
            texts[rel] = text
            raw.extend(_extract_anchors(_STATUS_BLOCK_RE.sub("", _PAGE_STATUS_RE.sub("", texts[rel])), rel))
    else:
        # Not initialized: read-only preview over instruction files, nothing is written.
        for rel, src_text in _source_texts(root, discover_instruction_files(root)):
            raw.extend(_extract_anchors(src_text, rel))
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
    if initialized and INDEX_PATH in texts:
        old = texts[INDEX_PATH]
        block = _status_block(classified, summary)
        new = _STATUS_BLOCK_RE.sub(lambda _m: block, old) if _STATUS_BLOCK_RE.search(old) else old.rstrip("\n") + "\n\n" + block + "\n"
        new = _migrate_legacy_comments(root, new)
        if new == old:
            actions.append({"op": "unchanged", "path": INDEX_PATH})
        elif dry_run:
            actions.append({"op": "would-update", "path": INDEX_PATH})
        else:
            write_managed_text(root, INDEX_PATH, new)
            texts[INDEX_PATH] = new
            actions.append({"op": "update", "path": INDEX_PATH})

    status_by_key = {c["anchor"]: c["status"] for c in classified}
    page_summaries: Dict[str, Dict[str, int]] = {}
    for a in raw:
        counts = page_summaries.setdefault(a["source"], {s: 0 for s in ANCHOR_STATES})
        counts[status_by_key[f"{a['path']}::{a['symbol']}"]] += 0 if a["pin"] else 1

    # Checksummed/generated pages must retain their exact generated bytes.
    for rel in pages:
        if rel in (INDEX_PATH, HANDOVER_PAGE) or _MARKDOWN_CHECKSUM_RE.search(texts[rel]) or _is_generated_page(rel, texts[rel]):
            continue
        old = texts[rel]
        new = _with_page_header(old, page_summaries.get(rel) or {s: 0 for s in ANCHOR_STATES})
        new = _migrate_legacy_comments(root, new)
        if new == old:
            actions.append({"op": "unchanged", "path": rel})
        elif dry_run:
            actions.append({"op": "would-update", "path": rel})
        else:
            write_managed_text(root, rel, new)
            texts[rel] = new
            actions.append({"op": "update", "path": rel})

    config_path = root / CONFIG_PATH
    if initialized and config_path.is_file() and not managed_path_problem(root, CONFIG_PATH):
        config, _cfg_err = read_config(root)
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
                write_managed_text(root, CONFIG_PATH, json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            actions.append({"op": "would-update" if dry_run else "update", "path": CONFIG_PATH})

    # Bi-directional sync: a page body that differs from its last recorded hash is a human edit.
    ledger = root / LEDGER_PATH
    ledger_synced = False
    user_edited: List[str] = []
    if initialized and not first_managed_problem(root, _LEDGER_FILES):
        page_hashes = {rel: _page_body_sha256(texts[rel]) for rel in pages}
        if dry_run:
            last = _read_ledger_hashes(ledger)
            user_edited = [rel for rel in pages if last.get(rel) != original_hashes[rel] and not (rel == INDEX_PATH and rel not in last)]
        else:
            existed = ledger.is_file()
            now = _utc_now()
            with contextlib.closing(connect_managed_sqlite(root, LEDGER_PATH)) as conn:
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
        report[TIER_HINT] = "Run `memlet init` to create .memlet/wiki/; showing a read-only preview of instruction-file anchors."
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
DREAM_PROMOTED_PAGE = ".memlet/wiki/dream-approved.md"  # memlet_bridge.PROMOTED_PAGE: read as rules only while verified
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
class _HeadingMatch:
    __slots__ = ("_title",)

    def __init__(self, title: str) -> None:
        self._title = title

    def group(self, name: Any = 0) -> str:
        return self._title


class _HeadingPattern:
    """`^\\s{0,3}#{1,6}\\s+(?P<title>.*?)\\s*#*\\s*$` as a direct scan (the regex backtracked quadratically over a
    long run of spaces before a non-space, non-`#` character: 5 KB took seconds). Same matches and same `title`:
    up to 3 leading whitespace characters, 1-6 `#`, at least one whitespace character, then the rest with trailing
    whitespace, a trailing `#` run and whitespace before it removed. Only `match` / `search` with `group("title")`."""

    pattern = r"^\s{0,3}#{1,6}\s+(?P<title>.*?)\s*#*\s*$"

    def match(self, line: str, pos: int = 0) -> Optional[_HeadingMatch]:
        if pos:
            return None
        n = len(line)
        i = 0
        while i < n and i < 4 and line[i].isspace():
            i += 1
        if i > 3:
            return None
        j = i
        while j < n and line[j] == "#":
            j += 1
        if not 1 <= j - i <= 6 or j >= n or not line[j].isspace():
            return None
        # `\\s+` is greedy and the title lazy: the title is what remains once trailing whitespace, one trailing `#`
        # run and the whitespace before it are removed; `.` cannot cross a newline, `\\s` can (so a trailing newline
        # is absorbed, a newline inside the title is no match)
        title = line[j:].strip().rstrip("#").rstrip()
        return None if "\n" in title else _HeadingMatch(title)

    search = match


_HEADING_RE = _HeadingPattern()
_FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")
_DEAD_END_HEADING_RE = re.compile(r"\b(?:rejected|dead[\s-]?ends?)\b", re.I)


def _memory_dir_files(root: Path, dirname: str) -> List[str]:
    """Markdown files under a memory-tool directory (`.ai-memory/`, `.claude-mem/`), bounded and secret-free."""
    base = root / dirname
    if contained_path_problem(root, dirname, leaf="dir") or not base.is_dir():
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
        if contained_path_problem(root, rel) or not p.is_file() or len(parts) > 8 or _is_secret_path(rel):
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
    for m in iter_explicit_anchors(scrubbed):
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
        # ruleSha256: the whole rule text (before the display limit), the identity a sidecar anchor is bound to
        rules.append({"source": source, "line": line_no, "section": section,
                      "text": body[:_RULE_TEXT_LIMIT], "refs": refs,
                      "ruleSha256": hashlib.sha256(body.encode("utf-8")).hexdigest()})
        if len(rules) >= _MAX_RULES_PER_FILE:
            break
    return rules


def _source_texts(root: Path, files: Iterable[str]) -> List[Tuple[str, str]]:
    """(rel, text) of instruction / memory source files that pass the containment rule, each at most
    _MAX_SOURCE_BYTES (a larger file is skipped) and MAX_SOURCE_TOTAL_BYTES together (later files are skipped)."""
    out: List[Tuple[str, str]] = []
    total = 0
    for rel in files:
        if contained_path_problem(root, rel):
            continue
        text = read_regular_text(root / rel, _MAX_SOURCE_BYTES)
        if text is None:
            continue
        total += len(text)
        if total > MAX_SOURCE_TOTAL_BYTES:
            break
        out.append((rel, text))
    return out


def _rules_from_files(root: Path, files: Iterable[str]) -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    for rel, text in _source_texts(root, files):
        rules.extend(_extract_rules(text, rel))
    _attach_sidecar(root, rules)
    return rules


def _attach_sidecar(root: Path, rules: List[Dict[str, Any]]) -> None:
    """Accepted sidecar anchors (`memlet admin anchors accept`) become references of the rules they belong to, so
    every reader checks them exactly like a written `path::symbol` (see `memlet_anchors`)."""
    from .memlet_anchors import attach_sidecar_refs
    attach_sidecar_refs(root, rules)


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
    """Recorded baselines from `.memlet/config.json`: `pinnedAnchors`, then (lowest precedence) the pins of attached
    sidecar anchors, so a written pin of the same anchor always governs."""
    config, _err = read_config(root)
    pins = {}
    if not isinstance(config, dict):
        return pins
    for key, cp in _validated_config_pins(config)[0].items():
        if isinstance(cp, dict) and cp.get("astHash"):
            pins[key] = {"ast": cp["astHash"], "sig": cp.get("sigHash"), "req": cp.get("reqSigHash"), "basis": cp.get("hashBasis")}
    if config.get("sidecarAnchors"):
        from .memlet_anchors import sidecar_index
        for key, pin in sidecar_index(root, config)["pins"].items():
            pins.setdefault(key, dict(pin))
    return pins


def _verify_rules(root: Path, rules: List[Dict[str, Any]], pins: Dict[str, Dict[str, Any]],
                  pin_source: str = "config") -> List[Dict[str, Any]]:
    """Classify each referenced anchor once and tag every rule with its worst anchor state.

    Observing a symbol does not establish a baseline; only stored pins are compared. Accepted sidecar anchors are
    attached first; an anchor that only sidecar entries supply is marked `suggested: true`."""
    _attach_sidecar(root, rules)
    from .memlet_bridge import enrich_promoted_rules
    enrich_promoted_rules(root, rules)
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
    suggested = {k for r in rules for k in r.get("suggestedAnchors") or []}
    if suggested:
        # per-anchor provenance: `sidecarIds` = every acceptance supplying it; `suggested` only when no rule wrote it
        written = {k for r in rules for k in r["anchors"] if k not in (r.get("suggestedAnchors") or [])}
        for r in rules:
            for k, sid in (r.get("sidecarIds") or {}).items():
                if k in cache:
                    cache[k]["sidecarIds"] = sorted(set(cache[k].get("sidecarIds") or []) | {sid})
        for k in suggested - written:
            if k in cache:
                cache[k]["suggested"] = True
    return [cache[k] for k in sorted(cache)]


def rule_anchor_pins(root: Path) -> Dict[str, Dict[str, Any]]:
    """The same baseline precedence for handover, recall and checks: wiki pins override config."""
    pins = _config_pins(root)
    page_pins = {}
    for rel, text in trusted_wiki_pages(root):
        for a in _extract_anchors(text, rel):
            if a["pin"]:
                page_pins.setdefault(f"{a['path']}::{a['symbol']}", a["pin"])
    pins.update(page_pins)
    return pins


def classify_rule_quotes(root: Path, entries: List[Dict[str, Any]]) -> None:
    """Tag every quoted span using handover's classifier, without re-pinning anything."""
    pins = rule_anchor_pins(root)
    rules = []
    from .memlet_anchors import attach_sidecar_to_span
    for e in entries:
        cite = e.get("id", e.get("source", ""))
        source, sep, span = cite.rpartition(":")
        if not sep:
            source, span = cite, ""
        refs = []
        for r in _extract_rules(e.get("quote", ""), source):
            refs.extend(r["refs"])
        rule = {"source": source, "line": 1, "text": e.get("quote", ""), "refs": refs}
        m = re.fullmatch(r"(\d{1,7})(?:-(\d{1,7}))?", span)
        if m:  # the quoted span `file:first-last`: sidecar anchors of rules inside it (verified against the file)
            attach_sidecar_to_span(root, rule, source, int(m.group(1)), int(m.group(2) or m.group(1)))
        rules.append(rule)
    anchors = _verify_rules(root, rules, pins, pin_source="pinned")
    by_key = {a["anchor"]: a for a in anchors}
    for e, r in zip(entries, rules):
        e["anchorStatus"] = r["status"]
        e["anchors"] = [dict({"anchor": k, "status": by_key[k]["status"], "reason": by_key[k]["reason"]},
                             **({"suggested": True, "sidecarId": (r.get("sidecarIds") or {}).get(k)}
                                if k in (r.get("suggestedAnchors") or []) else {}))
                        for k in r["anchors"]]
        e["anchorReason"] = "; ".join(dict.fromkeys(a["reason"] for a in e["anchors"])) or "no_code_anchor"


def _suggested_fields(rule, key=None):
    keys = rule.get("suggestedAnchors") or []
    if not keys:
        return {}
    ids = rule.get("sidecarIds") or {}
    return {"suggested": True, "suggestedAnchors": list(keys), "sidecarIds": dict(ids),
            "sidecarId": ids.get(key or keys[0])}


def _anchor_rule_rows(rules, key):
    return [{"rule": _clip(r["text"], _HANDOVER_TEXT_LIMIT), "source": f"{r['source']}:{r['line']}",
             **_suggested_fields(r, key)} for r in rules if key in r.get("anchors", [])]


def _anchor_lines(items):
    lines = []
    for a in items:
        if a.get("rules"):
            lines.extend(f"- `{a['anchor']}`: {a['reason']} — {suggested_label(r)}{r['rule']} (`{r['source']}`)"
                         for r in a["rules"])
        else:
            lines.append(f"- {suggested_label(a)}`{a['anchor']}`: {a['reason']}")
    return lines


def pinned_anchor_report(root: Path) -> Dict[str, Any]:
    """Read-only current classification of all recorded pins and discovered symbol anchors."""
    pins = rule_anchor_pins(root)
    raw, rules = [], []
    for rel, text in list(_source_texts(root, discover_instruction_files(root))) + list(trusted_wiki_pages(root)):
        raw.extend(_extract_anchors(text, rel))
        if not _is_generated_page(rel, text):
            rules.extend(_extract_rules(_STATUS_BLOCK_RE.sub(_blank_lines, _PAGE_STATUS_RE.sub(_blank_lines, text)), rel))
    _verify_rules(root, rules, pins, pin_source="pinned")
    merged = {f"{a['path']}::{a['symbol']}": a for a in _merge_anchors(raw)}
    for key, pin in pins.items():
        if "::" in key:
            path, symbol = key.rsplit("::", 1)
            merged.setdefault(key, {"path": path, "symbol": symbol, "sources": []})["pin"] = pin
    from .memlet_anchors import sidecar_index
    side = sidecar_index(root)
    side_sources: Dict[str, List[str]] = {}
    side_ids: Dict[str, List[str]] = {}
    for e in side["attached"]:  # an accepted sidecar anchor is reported at its rule, like a written anchor
        side_sources.setdefault(e["anchor"], []).append(f"{e['source']}:{e['currentLine']}")
        side_ids.setdefault(e["anchor"], []).append(e["id"])
    anchors = []
    for k in sorted(merged):
        a = classify_anchor(root, merged[k])
        if k in side_sources:
            written = bool(a["sources"])
            a["sources"] = a["sources"] + [s for s in side_sources[k] if s not in a["sources"]]
            a["sidecarIds"] = sorted(side_ids[k])
            a["rules"] = _anchor_rule_rows(rules, k)
            if not written:
                a["suggested"] = True
        anchors.append(a)
    counts = {state: sum(a["status"] == state for a in anchors) for state in ANCHOR_STATES}
    counts["total"] = len(anchors)
    report = {"summary": counts, "anchors": anchors}
    if side["attached"] or side["detached"]:  # additive, only once sidecar anchors exist
        report["sidecar"] = {"attached": len(side["attached"]),
                             "detached": [{k: e.get(k) for k in ("id", "source", "anchor", "state", "reason")}
                                          for e in side["detached"]]}
    return report


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
                if pin and k not in pinned_here and not by_key[k].get("suggested"):
                    pinned_here.add(k)
                    sig = f" sig={pin['sig']}" if pin.get("sig") else ""
                    req = f" req={pin['req']}" if pin.get("req") else ""
                    lines.append(f"  <!-- memlet:anchor {k} ast={pin['ast']}{sig}{req} basis={pin.get('basis') or 'legacy'} -->")
    return "\n".join(lines) + "\n"


def _write_generated_page(root: Path, rel: str, text: str, dry_run: bool) -> str:
    target = root / rel
    if managed_path_problem(root, rel):
        return "kept"  # a symlinked / non-regular component: never written through
    exists = target.is_file()
    if exists and _PAGE_STATUS_RE.sub("", _read_text(target)) == text:
        return "unchanged"
    if dry_run:
        return "would-update" if exists else "would-create"
    write_managed_text(root, rel, text)
    return "update" if exists else "create"


def _escape_cell(text: str, limit: int = 100) -> str:
    text = text if len(text) <= limit else text[: limit - 1] + "…"
    return text.replace("|", "\\|")


def import_rules(repo_path=".", *, source: str = "auto", dry_run: bool = False, output_format: str = "json") -> Dict[str, Any]:
    """`memlet import`: import rules from `CLAUDE.md` / `AGENTS.md` / `.cursorrules` / `.cursor/rules/*.mdc` /
    `.ai-memory/**/*.md` / `.claude-mem/**/*.md` and audit every code anchor they mention right away."""
    root = _resolve_root(repo_path)
    source = (source or "auto").lower()
    if source not in IMPORT_SOURCES:
        import_source_files(root, source)  # raises the usual ValueError for an unknown source
    return _guarded("import", root, dry_run, output_format, _WIKI_STATE_PATHS + (IMPORT_PAGE,),
                    lambda: _import_rules(root, source, dry_run, output_format))


def _import_rules(root: Path, source: str, dry_run: bool, output_format: str) -> Dict[str, Any]:
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
                pins[key] = {"ast": cur["ast"], "sig": cur["sig"], "req": cur["req"], "basis": cur["basis"], "types": cur.get("types"), "shape": cur.get("shape")}
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
    if cfg_path.is_file():
        config, cfg_err = read_config(root)
        if cfg_err:
            config = False  # unreadable config is left alone
    if config is False:
        actions.append({"op": "kept", "path": CONFIG_PATH})
    else:
        existed = isinstance(config, dict)
        config = config if existed else _base_config(head, discover_instruction_files(root))
        pinned = _validated_config_pins(config)[0]
        for a in anchors:
            # Store newly recorded baselines, including lexical TS/JS pins; migrate proven legacy matches.
            # a sidecar-only anchor keeps its baseline in `sidecarAnchors`; copying it here would outlive a `drop`
            if a["symbol"] and a.get("pin") and not a.get("suggested") and (a["anchor"] not in pinned or a["reason"] == "legacy_ast_hash_match"):
                pinned[a["anchor"]] = {"astHash": a["pin"]["ast"], "sigHash": a["pin"]["sig"], "reqSigHash": a["pin"]["req"], "hashBasis": a["pin"].get("basis") or "legacy",
                                         "annotationHash": a["pin"].get("types"), "parameterShapeHash": a["pin"].get("shape")}
        config["pinnedAnchors"] = dict(sorted(pinned.items()))
        config["imports"] = {"from": source, "page": IMPORT_PAGE, "sourceFiles": files}
        text = json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if existed and _read_text(cfg_path) == text:
            op = "unchanged"
        elif dry_run:
            op = "would-update" if existed else "would-create"
        else:
            write_managed_text(root, CONFIG_PATH, text)
            op = "update" if existed else "create"
        actions.append({"op": op, "path": CONFIG_PATH})

    ledger = root / LEDGER_PATH
    if dry_run:
        op = "would-update" if ledger.is_file() else "would-create"
    else:
        existed = ledger.is_file()
        now = _utc_now()
        with contextlib.closing(connect_managed_sqlite(root, LEDGER_PATH)) as conn:
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
        "next": ["memlet admin wiki", "memlet handover"],
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

from .compatibility import LEGACY_HANDOVER_SCHEMA, LEGACY_HANDOVER_TITLE

HANDOVER_SCHEMA = "memlet-handover/v1"
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
_DIGEST_SOURCE_TAIL_AFTER_DASH_RE = re.compile(r"\s+source\s+`[^`]*`.*$")


class _DigestSourceTailPattern:
    """`\\s+—\\s+source\\s+`[^`]*`.*$` without retrying every whitespace start: a match must start at the first
    whitespace character of the run that ends at an em dash, so each em dash is tried once (100 KB of spaces took
    seconds with the regex). `search` / `sub` with the same spans as the regex."""

    pattern = r"\s+—\s+source\s+`[^`]*`.*$"

    def search(self, text: str, pos: int = 0) -> Optional[SpanMatch]:
        d = text.find("—", pos)
        while d >= 0:
            s = d
            while s > pos and text[s - 1].isspace():
                s -= 1
            if s < d:
                m = _DIGEST_SOURCE_TAIL_AFTER_DASH_RE.match(text, d + 1)
                if m:
                    return SpanMatch(text, s, m.end())
            d = text.find("—", d + 1)
        return None

    def sub(self, repl: Any, text: str, count: int = 0) -> str:
        m = self.search(text)  # the match runs to the end of the text, so there is at most one
        if m is None:
            return text
        return text[:m.start()] + (repl(m) if callable(repl) else repl) + text[m.end():]


_DIGEST_SOURCE_TAIL_RE = _DigestSourceTailPattern()
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
    observations = tuple(k for k in ("pathExistsAnchors", "unpinnedAnchors", "lexicalConstraints", "hints", "approvedLessons", "listCapOmitted", "replacedRuleNotices") if k in capsule)
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
    if capsule.get("schema_version") in ("memlet.handover_bridge.v1", "memlet.capsules.v1"):
        from .handover_bridge import verify
        return verify(capsule)
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
    if not text.startswith(("## Memlet Handover Capsule\n", LEGACY_HANDOVER_TITLE + "\n")):
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
        LEGACY_HANDOVER_TITLE if c.get("schema") == LEGACY_HANDOVER_SCHEMA else "## Memlet Handover Capsule",
        "",
        f"- Repo head: `{c['repoHead'] or 'n/a'}`",
        f"- Checksum: `{_MARKDOWN_CHECKSUM_PLACEHOLDER}` (integrity, not authentication)",
        f"- Authority: {c['authorityBoundary']}",
        "",
        "### Active constraints (anchored symbols unchanged since pinning)",
    ]
    from .memlet_bridge import _lesson_quote_block
    for a in c["activeConstraints"]:
        if a.get("quoteLines"):
            lines.append(f"- {suggested_label(a)}(`{a['source']}`)\n" + _lesson_quote_block(a))
        else:
            lines.append(f"- {suggested_label(a)}{a['rule']} (`{a['source']}`" +
                         ("; TRUNCATED — read the source line before acting" if a.get("truncated") else "") + ")")
    if not c["activeConstraints"]:
        lines.append("- (none)")
    if c.get("replacedRuleNotices"):
        lines.extend("- " + notice for notice in c["replacedRuleNotices"])
    if c.get("approvedLessons"):
        lines += ["", "### Approved lessons (human-approved; citations re-verified in .memlet/dream/snapshot/)"]
        lines += [i["text"] for i in c["approvedLessons"]]
    if c.get("hints"):
        from memlet.memlet_bridge import hint_label
        from .vocabulary import HINT_HELP
        lines += ["", "### " + hint_label(c["hints"]), HINT_HELP]
        if not c.get("approvedLessons"):
            lines.append("Citations: .memlet/dream/snapshot/")
        lines += [h["text"] for h in c["hints"]]

    if c.get("lexicalConstraints"):
        lines += ["", "### TS/JS constraints (lexical hash match only; re-check before use)"]
        lines += [f"- {suggested_label(a)}{a['rule']} (`{a['source']}`)" for a in c["lexicalConstraints"]]
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
        held = (len(c.get("staleIndex") or []) + c["omitted"].get("staleIndex", 0)
                + c.get("listCapOmitted", {}).get("staleIndex", {}).get("count", 0))
        if held:
            lines.append(f"- WARNING: {held} indexed rule(s) changed since the index was built (re-read before relying on "
                         "them): " + "; ".join(f"{a['topic']} `{a['source']}`" for a in (c.get("staleIndex") or [])))
        if c.get("indexOutdated"):
            lines.append("- WARNING: instructions changed after indexing; re-run `memlet index-build`")
    if c.get("mode") == "brief":
        src = c.get("briefSource") or {}
        lines += ["", f"### Project brief (LLM-written by {src.get('via') or 'n/a'} at {src.get('builtAt') or 'n/a'}; cited "
                  "instruction text unchanged since then — meaning and code behaviour are NOT verified; follow unless current "
                  "instructions say otherwise)"]
        lines += [f"- {suggested_label(a)}{a['rule']} (`{a['source']}`)" for a in c.get("briefRules") or []] or [
            "- (none — run `memlet brief-build` to create one)"]
        if c["omitted"].get("briefRules"):
            lines.append(f"- {c['omitted']['briefRules']} lower-priority brief line(s) omitted for the token budget; read the "
                         "project instructions for anything not listed here")
        held = (len(c.get("staleBrief") or []) + c["omitted"].get("staleBrief", 0)
                + c.get("listCapOmitted", {}).get("staleBrief", {}).get("count", 0))
        if held:
            lines.append(f"- WARNING: {held} brief line(s) held back because their cited instructions changed: "
                         + "; ".join(f"`{a['source']}`" for a in (c.get("staleBrief") or [])) + " — re-read those lines")
    if c.get("mode") == "digest":
        lines += ["", "### Project rules (from instructions; not code-anchored — follow unless current instructions say otherwise)"]
        lines += [f"- {suggested_label(a)}{a['rule']} (`{a['source']}`" + (f"; path not found: {', '.join(a['pathNotFound'])}" if a.get("pathNotFound")
                  else "") + ("; TRUNCATED — read the source line before acting" if a.get("truncated") else "") + ")"
                  for a in c.get("projectRules") or []] or ["- (none)"]
        if c["omitted"].get("projectRules"):
            lines.append(f"- {c['omitted']['projectRules']} lower-priority rule(s) omitted for the token budget; read the "
                         "project instructions for anything not listed here")
    for key, label in (("pathExistsAnchors", "Paths exist (not verified)"),
                       ("unpinnedAnchors", "Unpinned symbols (not verified)")):
        if c.get(key):
            lines += ["", "### " + label]
            lines += [(f"- {suggested_label(a)}{a['rule']} (`{a['source']}`; {a['reason']}"
                       + ("; TRUNCATED — read the source line before acting" if a.get("truncated") else "") + ")")
                      if "rule" in a else f"- `{a['anchor']}`: {a['reason']}" for a in c[key]]
    if c["reverifyBeforeUse"]:
        lines += ["", "### Re-verify before use (compatible-unverified)"]
        lines += _anchor_lines(c["reverifyBeforeUse"])
    lines += ["", "### Quarantined stale anchors (not handed over)"]
    lines += _anchor_lines(c["quarantinedStaleAnchors"]) or ["- (none)"]
    hidden_stale = (c["omitted"].get("quarantinedStaleAnchors", 0)
                    + c.get("listCapOmitted", {}).get("quarantinedStaleAnchors", {}).get("count", 0))
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
    for key, value in sorted(c.get("listCapOmitted", {}).items()):
        lines.append("- Omitted over the %d-item cap: %s %d" % (value["cap"], key, value["count"]))
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
        cfg, _err = read_config(root)  # regular file, size-capped, never a FIFO or a symlink
        hand = cfg.get("handover") if isinstance(cfg, dict) else None
        budget = hand.get("tokenBudget") if isinstance(hand, dict) else None
        source = "config"
    if budget is None:
        return HANDOVER_TOKEN_BUDGET, "default"
    if str(budget).strip().lower() == "auto":
        text = "".join(t for _rel_path, t in _source_texts(root, discover_instruction_files(root)))
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
        cfg, _err = read_config(root)
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
    for rel, src_text in _source_texts(root, files):
        lines = src_text.splitlines()
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


def _mark_suggested_items(lists: Dict[str, List[Dict[str, Any]]], constraints: List[Dict[str, Any]],
                          by_key: Dict[str, Dict[str, Any]]) -> None:
    """Say where an anchor came from: rule items whose anchors include accepted sidecar anchors carry
    `suggestedAnchors`, anchor items supplied only by sidecar entries carry `suggested: true`. Adds keys only when
    sidecar anchors are involved, so capsules without them are byte-identical."""
    by_src = {f"{r['source']}:{r['line']}": r for r in constraints if r.get("suggestedAnchors")}
    for items in lists.values():
        for item in items:
            r = by_src.get(item.get("source"))
            if r is not None and "anchors" in item:  # per rule: exactly the anchors this rule got from sidecars
                item["suggestedAnchors"] = [_bounded_anchor(k) for k in r["suggestedAnchors"]]
                item["sidecarIds"] = [r["sidecarIds"][k] for k in r["suggestedAnchors"] if k in (r.get("sidecarIds") or {})]
            elif "reason" in item and "anchor" in item and (by_key.get(item["anchor"]) or {}).get("sidecarIds"):
                rows = _anchor_rule_rows(constraints, item["anchor"])
                if rows and all(row.get("suggested") for row in rows):
                    item["suggested"] = True
                item["sidecarIds"] = sorted({sid for row in rows for sid in row.get("sidecarIds", {}).values()})
                if any(not row.get("suggested") for row in rows):
                    item["rules"] = rows


def build_handover(repo_path=".", *, next_step: Optional[str] = None, rejected: Optional[Iterable[str]] = None,
                   output_format: str = "json", write: bool = False, budget=None, mode=None, _lesson_items=None) -> Dict[str, Any]:
    options = dict(next_step=next_step, rejected=rejected, output_format=output_format,
                   budget=budget, mode=mode, _lesson_items=_lesson_items)
    if not write:
        return _build_handover(repo_path, **options)
    root = _resolve_root(repo_path)
    try:
        # Build and replace all projections under the same re-entrant write lock.
        with memlet_write_lock(root):
            return _build_handover(root, write=True, **options)
    except (OSError, ValueError) as exc:
        from .handover_bridge import HANDOVER_FILE, CAPSULES_FILE
        report = _build_handover(root, **options)
        paths = [HANDOVER_PAGE, HANDOVER_FILE, CAPSULES_FILE]
        report.update(ok=False, exitCode=2, written=False, filesWritten=[], filesNotWritten=paths,
                      error=str(exc) + "; written: none; not written: " + ", ".join(paths))
        return report


def _build_handover(repo_path=".", *, next_step=None, rejected=None, output_format="json", write=False,
                    budget=None, mode=None, _lesson_items=None):
    """`memlet handover`: deterministic, checksummed (<500 token) Memlet handover capsule.

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
    for rel, text in trusted_wiki_pages(root):
        for a in _extract_anchors(text, rel):
            if a["pin"]:
                page_pins.setdefault(f"{a['path']}::{a['symbol']}", a["pin"])
        if not _is_generated_page(rel, text) and not (digest or brief or index):  # alt modes: wiki gives pins only
            body = _STATUS_BLOCK_RE.sub(_blank_lines, _PAGE_STATUS_RE.sub(_blank_lines, text))
            page_rules = _extract_rules(body, rel)
            if rel == DREAM_PROMOTED_PAGE:  # promoted Dream lessons travel only while approval and citation verify
                from memlet.memlet_bridge import promoted_rule_entries
                live = {int(e["source"].rsplit(":", 1)[1]) for e in promoted_rule_entries(root)[0]}
                page_rules = [r for r in page_rules if r["line"] in live]
            rules.extend(page_rules)
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
            item = {"rule": _clip(r["text"], _HANDOVER_TEXT_LIMIT), "anchors": r["anchors"], "source": f"{r['source']}:{r['line']}"}
            if r.get("quoteLines"):
                from .vocabulary import LESSON_SEPARATOR
                item.update({k: r[k] for k in ("quoteLines", "cites", "edgeSources")})
                item["quoteLines"] = [_clip(line, _HANDOVER_TEXT_LIMIT) for line in r["quoteLines"]]
                item["rule"] = LESSON_SEPARATOR.join(item["quoteLines"])
            return item
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
    _mark_suggested_items(lists, constraints, by_key)
    from memlet.memlet_bridge import dream_items, render_capsule_lesson, _load_queue
    lesson_items = (dream_items(root, tiers=("approved", TIER_HINT)).get("items", [])
                    if _lesson_items is None else _lesson_items)
    # Metadata only: eligibility remains exclusively the verified tier provider's decision.
    try:
        graph = _load_queue(root)[0] or {}
    except (OSError, ValueError, TypeError):
        graph = {}
    approvals = {n["id"]: str((n.get("approval") or {}).get("at") or "")
                 for n in graph.get("nodes", [])}
    approved = sorted((i for i in lesson_items if i.get("tier") == "approved"), key=lambda i: i["id"])
    approved.sort(key=lambda i: approvals.get(i["id"], ""), reverse=True)
    priority = ("SUPERSEDES", "BLOCKS", "RESOLVES", "GOVERNS")
    approved.sort(key=lambda i: min([priority.index(k) for k in i.get("edgeTypes", []) if k in priority] or [4]))
    from .memlet_bridge import replaced_rule_notices
    replacement_notices = replaced_rule_notices(root, constraints, approved)
    lists["approvedLessons"] = [dict(i, text=render_capsule_lesson(i)) for i in approved]
    # Only items the tier computation itself calls a hint, whatever a caller's provider returns.
    lists["hints"] = [{"id": h["id"], "text": render_capsule_lesson(h, hint=True), "cites": h["cites"], "label": h.get("agreementLabel") or "unreviewed hint",
                         **{k: h[k] for k in ("quoteLines", "edgeSources") if k in h}}
                      for h in lesson_items if h.get("tier") == TIER_HINT]
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
        from memlet.memlet_brief import _mask
        for items in lists.values():
            for a in items:
                for f in ("rule", "approach"):
                    if isinstance(a.get(f), str):
                        a[f] = _mask(a[f])
    caps = {k: _HANDOVER_LIST_CAP for k in lists}
    index_meta = None
    if index:
        from memlet.memlet_index import QUOTE_MAX_LINES, load_verified_index
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
        from memlet.memlet_brief import load_verified_brief
        fresh, stale_brief, brief_meta = load_verified_brief(root)
        lists["briefRules"], lists["staleBrief"] = fresh, stale_brief
        caps["briefRules"], caps["staleBrief"] = 200, _DIGEST_STALE_SHOWN
        caps["quarantinedStaleAnchors"] = _DIGEST_STALE_SHOWN
    if digest:
        caps["quarantinedStaleAnchors"] = _DIGEST_STALE_SHOWN  # the WARNING line still carries the full count
        caps["projectRules"] = 200
    if digest or brief or index:  # displayed strings only; membership above already used the real citations
        from memlet.memlet_brief import _mask
        for items in lists.values():
            for a in items:
                for f in ("rule", "topic", "source", "anchor", "approach"):
                    if isinstance(a.get(f), str):
                        a[f] = _mask(a[f])
                for f in ("anchors", "pathNotFound"):
                    if isinstance(a.get(f), list):
                        a[f] = [_mask(x) if isinstance(x, str) else x for x in a[f]]
    # Preserve the capsule's rule order (including digest ranking) in the bridge,
    # before token trimming. Compatible rules follow the active constraints.
    bridge_order = {item["source"]: i for i, item in enumerate(lists["activeConstraints"])} if write else {}
    bridge_rules = sorted(constraints, key=lambda r: bridge_order.get(f"{r['source']}:{r['line']}", len(bridge_order))) if write else []
    capped = {k: max(0, len(v) - caps[k]) for k, v in lists.items()}
    omitted = {k: 0 for k in lists}
    lists = {k: v[:caps[k]] for k, v in lists.items()}
    counts = _rule_counts(constraints)
    proof = {
        "modifiedFilesCount": _git_modified_count(root),
        "anchorHealth": _summarize(anchors),
        "staleRulesBlocked": counts["stale"],
        "unanchoredRulesOmitted": sum(r["status"] == "unanchored" and r["source"] != DREAM_PROMOTED_PAGE for r in constraints),
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
    drop_order = ("hints", "approvedLessons", "lexicalConstraints", "pathExistsAnchors", "unpinnedAnchors") + drop_order
    floors = {"quarantinedStaleAnchors": min(_HANDOVER_QUARANTINE_FLOOR, caps["quarantinedStaleAnchors"])}
    while True:
        capsule = {"schema": HANDOVER_SCHEMA, "repoHead": head, **lists, "executionProof": proof,
                   "nextAction": next_action, "authorityBoundary": AUTHORITY_BOUNDARY, "omitted": dict(omitted)}
        if replacement_notices:
            capsule["replacedRuleNotices"] = replacement_notices
        if any(capped.values()):
            capsule["listCapOmitted"] = {k: {"count": v, "cap": caps[k]} for k, v in capped.items() if v}
        if digest:
            capsule["mode"] = "digest"
        if index:
            capsule["mode"] = "index"
            capsule["indexSource"] = ({k: index_meta.get(k) for k in ("via", "builtAt", "inputSha256")} if index_meta
                                      else None)
            if index_meta and (index_meta.get("staleness") or {}).get("stale"):
                capsule["indexOutdated"] = True  # additive, only when stale: unchanged capsules stay byte-identical
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
        if tokens <= token_budget:
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
    write_refused: Optional[str] = None
    files_written, files_not_written, file_status = [], [], {}
    if write:
        from . import handover_bridge as hb
        write_paths = [HANDOVER_PAGE, hb.HANDOVER_FILE, hb.CAPSULES_FILE]
        files_not_written = list(write_paths)
        page = "# Memlet Handover\n\n" + markdown
        problem = first_managed_problem(root, (MEMLET_DIR, WIKI_DIR) + tuple(write_paths) + _LEDGER_FILES)
        if problem:  # nothing is written through a symlinked / non-regular component; the capsule is still returned
            actions += [{"op": "kept", "path": p} for p in write_paths + [LEDGER_PATH]]
            write_refused = problem
        else:
            write_refused = None
            ledger = root / LEDGER_PATH
            try:
                with memlet_write_lock(root):
                    now = _utc_now()
                    # Use the same classified rules and approved lesson order,
                    # before the Markdown token budget trims either group.
                    bridges = hb.build(root, bridge_rules, anchors, approved, head, now)
                    # Explicit --write replaces all three even when bytes match;
                    # the generic page helper can skip unchanged/unsafe targets.
                    existed = (root / HANDOVER_PAGE).is_file()
                    write_managed_text(root, HANDOVER_PAGE, page)
                    op = "update" if existed else "create"
                    actions.append({"op": op, "path": HANDOVER_PAGE})
                    files_written.append(HANDOVER_PAGE)
                    files_not_written.remove(HANDOVER_PAGE)
                    for rel, text in bridges.items():
                        existed = (root / rel).is_file()
                        hb.write(root, rel, text)
                        actions.append({"op": "update" if existed else "create", "path": rel})
                        files_written.append(rel)
                        files_not_written.remove(rel)
                    files_not_written.append(LEDGER_PATH)
                    existed = ledger.is_file()
                    with contextlib.closing(connect_managed_sqlite(root, LEDGER_PATH)) as conn:
                        with conn:
                            _ledger_ready(conn)
                            _sync_snapshots(conn, [a for a in anchors if a["symbol"]], now)
                            _record_page(conn, HANDOVER_PAGE, _page_body_sha256(page), "handover", now)
                    actions.append({"op": "update" if existed else "create", "path": LEDGER_PATH})
                    files_not_written.remove(LEDGER_PATH)
            except (OSError, ValueError, sqlite3.Error) as exc:
                write_refused = str(exc)
                if files_written:
                    for rel in (hb.HANDOVER_FILE, hb.CAPSULES_FILE):
                        if rel not in files_not_written:
                            continue
                        try:
                            problem = first_managed_problem(root, (rel,))
                            if problem:
                                raise OSError(problem)
                            (root / rel).unlink(missing_ok=True)
                            file_status[rel] = "not written and removed"
                        except OSError:
                            file_status[rel] = "not written and still in place (stale, still read by Routlet)"
        for rel in write_paths + ([LEDGER_PATH] if LEDGER_PATH in files_not_written else []):
            file_status.setdefault(rel, "written" if rel in files_written else
                                   ("not written and still in place (stale, still read by Routlet)"
                                    if rel in (hb.HANDOVER_FILE, hb.CAPSULES_FILE) and (root / rel).exists()
                                    else "not written"))

    over = tokens >= token_budget
    refused_write = write and write_refused is not None
    report: Dict[str, Any] = {
        "ok": not over and not refused_write,
        "exitCode": 2 if (over or refused_write) else 0,
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
    if write:
        report.update(filesWritten=files_written, filesNotWritten=files_not_written, fileStatus=file_status)
    if refused_write:
        report["written"] = False
        report["error"] = (str(write_refused) + "; written: " + (", ".join(files_written) or "none")
                           + "; not written: " + (", ".join(files_not_written) or "none")
                           + "; " + "; ".join(path + ": " + status for path, status in file_status.items()))
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
    return {"injected": injected, "handoverContext": text, "handoverActive": injected, TIER_REJECTED: []}


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
    return {"injected": injected, "handoverContext": context, "handoverActive": injected, TIER_REJECTED: []}


def _bench_memlet(root: Path, sc: Dict[str, Any]) -> Dict[str, Any]:
    imported = import_rules(root, source="claude-md", dry_run=True)
    injected = [r["text"] for r in imported["rules"] if r["status"] != "stale"]
    out: Dict[str, Any] = {"injected": injected, "handoverContext": "", "handoverActive": injected, TIER_REJECTED: []}
    if sc.get("handover"):
        cap = build_handover(root, next_step="Finish the refund flow refactor.")
        out["handoverContext"] = _render_handover(cap, cap["capsuleSignature"])
        out["handoverActive"] = [a["rule"] for a in cap["activeConstraints"]]
        out[TIER_REJECTED] = [a["approach"] for a in cap["rejectedApproaches"]]
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
            carried = dead_end in result[TIER_REJECTED] if mode == "memlet" else dead_end in result["handoverContext"]
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
