"""Memlet 9.3 skillbook: package person-approved lessons as an Agent Skill (SKILL.md).

Invariants:
- Approved lessons only, from the single reader (`memlet_bridge.dream_items`).
- Never rewrites lesson text; quotes cited lines verbatim as block quotes after display sanitisation.
- No model call, no wall-clock timestamp anywhere in the output.
- Writes only when `write_skill` (`--write`) is called, atomically, refusing symlinks, invalid names,
  foreign files, and hand-edited files (unless `force=True`).
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import os
import re
import stat
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import VERSION, memlet_bridge as mb, memlet_dream as md, memlet_wiki as mw, vocabulary as v

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_MAX_SKILL_READ_BYTES = 4 * 1024 * 1024

_YAML_ESCAPE = {
    "\\": "\\\\",
    '"': '\\"',
    "\0": "\\0",
    "\a": "\\a",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\v": "\\v",
    "\f": "\\f",
    "\r": "\\r",
    "\x1b": "\\e",
    "\u0085": "\\N",
    "\u00a0": "\\_",
    "\u2028": "\\L",
    "\u2029": "\\P",
}
_YAML_UNESCAPE = {val[1:]: key for key, val in _YAML_ESCAPE.items()}

_KNOWN_SKILL_REPOS: Dict[str, str] = {}
_check_repo_override: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "check_repo_override", default=None
)


def valid_skill_name(name: Any) -> bool:
    """Agent Skills specification: 1-64 chars, only a-z, 0-9 and `-`, no leading/trailing/doubled hyphen."""
    return isinstance(name, str) and 1 <= len(name) <= 64 and bool(SKILL_NAME_RE.fullmatch(name))


def yaml_quote(value: Any) -> str:
    """Quote a string as a single-line YAML double-quoted scalar."""
    s = str(value)
    out = ['"']
    for ch in s:
        if ch in _YAML_ESCAPE:
            out.append(_YAML_ESCAPE[ch])
        elif (
            ord(ch) < 0x20
            or 0x7F <= ord(ch) <= 0x9F
            or unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn")
            or mb._hidden_character(ch)
        ):
            code = ord(ch)
            if code <= 0xFF:
                out.append(f"\\x{code:02x}")
            elif code <= 0xFFFF:
                out.append(f"\\u{code:04x}")
            else:
                out.append(f"\\U{code:08x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def yaml_unquote(raw: str) -> Optional[str]:
    """Parse a single-line YAML scalar produced by `yaml_quote` (or an unquoted skill name)."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if not (s.startswith('"') and s.endswith('"')):
        if "\n" in s or "\r" in s or any(c in s for c in "\"'\\"):
            return None
        return s
    if len(s) < 2:
        return None
    inner = s[1:-1]
    out: List[str] = []
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i]
        if ch == '"':
            return None
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= n:
            return None
        esc = inner[i]
        if esc in _YAML_UNESCAPE:
            out.append(_YAML_UNESCAPE[esc])
            i += 1
        elif esc == "x" and i + 2 < n:
            try:
                out.append(chr(int(inner[i + 1 : i + 3], 16)))
            except ValueError:
                return None
            i += 3
        elif esc == "u" and i + 4 < n:
            try:
                out.append(chr(int(inner[i + 1 : i + 5], 16)))
            except ValueError:
                return None
            i += 5
        elif esc == "U" and i + 8 < n:
            try:
                out.append(chr(int(inner[i + 1 : i + 9], 16)))
            except ValueError:
                return None
            i += 9
        else:
            return None
    return "".join(out)


def _merge_state(node: Dict[str, Any], nodes_by_id: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Adapter for N1 `memlet_bridge.merge_state`; returns 'not available' when N1 is absent."""
    fn = getattr(mb, "merge_state", None)
    if callable(fn):
        return fn(node, nodes_by_id)
    return {"state": "none", "into": None, "reason": v.SKILL_MERGE_UNAVAILABLE}


def _sanitise_inline(value: Any, limit: int = 240) -> str:
    """Sanitise an inline identifier, file path, or citation span."""
    s = mb.safe_lesson_display(str(value), limit=limit)
    s = md.mask_secrets(s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"-{3,}", "--", s)
    s = re.sub(r"`{3,}", "``", s)
    s = re.sub(r"~{3,}", "~~", s)
    return s


def _sanitise_quote_line(text: Any) -> str:
    """Sanitise a cited line so it can never break out of a block quote, forge frontmatter,
    open/close a code fence, start a heading, inject an HTML comment, or carry control/bidi chars."""
    s = mb.safe_lesson_display(str(text), limit=md.LESSON_TEXT_MAX_CHARS)
    s = md.mask_secrets(s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"-{3,}", "--", s)
    s = re.sub(r"`{3,}", "``", s)
    s = re.sub(r"~{3,}", "~~", s)
    if re.match(r"^[>\s]*\.{3,}", s):
        s = re.sub(r"^[>\s]*\.{3,}", "..", s)
    while s.startswith((">", "#")) or re.match(r"^[>\s]+#", s):
        s = re.sub(r"^[>\s]+", "", s)
        if s.startswith("#"):
            s = "\\" + s
    return s


def _build_description(files: Sequence[str]) -> str:
    """Fixed text plus as many attached file paths as fit within 1024 characters (most lessons first)."""
    base = v.SKILL_DESCRIPTION_BASE
    if not files:
        return base
    chosen: List[str] = []
    for path in files:
        clean = _sanitise_inline(path, limit=240)
        if not clean:
            continue
        candidate = base + v.SKILL_DESCRIPTION_FILES_PREFIX + ", ".join(chosen + [clean])
        if len(candidate) <= 1024 and len(yaml_quote(candidate)) <= 1024:
            chosen.append(clean)
        else:
            break
    if not chosen:
        return base
    return base + v.SKILL_DESCRIPTION_FILES_PREFIX + ", ".join(chosen)


def _item_rank(item: Dict[str, Any]) -> int:
    """Within a group: prohibitions first (0), then replaced instructions (1), then fixes (2), then the rest (3)."""
    rels = item.get("edgeTypes") or []
    roles = item.get("lineRoles") or []
    if "BLOCKS" in rels and "replaced" not in roles:
        return 0
    if "replaced" in roles or "SUPERSEDES" in rels:
        return 1
    if "RESOLVES" in rels:
        return 2
    return 3


def _format_cite(c: Dict[str, Any]) -> str:
    f = _sanitise_inline(c.get("file") or "", limit=300)
    a, b = c.get("start"), c.get("end")
    return f"{f}:{a}" + (f"-{b}" if b != a else "")


def _render_item_lines(item: Dict[str, Any], item_files: Sequence[str]) -> List[str]:
    rels = [r for r in v.BRIDGE_RELATIONS if r in (item.get("edgeTypes") or [])] or ["LESSON"]
    quotes = list(item.get("quoteLines") or [item.get("text") or ""])
    cites = list(item.get("cites") or [])
    roles = list(item.get("lineRoles") or [None] * len(quotes))
    parts = list(zip(roles, quotes, cites))
    if "replaced" in roles:
        parts = sorted(parts, key=lambda p: p[0] != "replaced")
    cite_strs = [_format_cite(c) for _, _, c in parts]
    file_strs = [_sanitise_inline(f, limit=240) for f in item_files] if item_files else [v.SKILL_ANY_FILE]
    head = v.SKILL_ITEM_HEAD % (
        ", ".join(rels),
        _sanitise_inline(item.get("id") or "", limit=64),
        ", ".join(cite_strs),
        ", ".join(file_strs),
    )
    lines = [head]
    for role, quote, _cite in parts:
        label = (
            v.HOOK_ROLE_REPLACED
            if role == "replaced"
            else v.HOOK_ROLE_CURRENT
            if role == "current"
            else ""
        )
        lines.append("  > " + label + _sanitise_quote_line(quote))
    return lines


def _render_body(
    repo_head: str,
    kept: Sequence[Dict[str, Any]],
    lesson_files: Dict[str, List[str]],
    ordered_files: Sequence[str],
    omitted: int,
) -> str:
    lines: List[str] = [
        v.SKILL_BODY_TITLE,
        "",
        v.SKILL_SNAPSHOT_LINE % (repo_head, len(kept)),
        v.HOOK_QUOTE_NOTE,
    ]
    if not kept and omitted == 0:
        lines.extend(["", v.SKILL_EMPTY_LINE])
    elif kept:
        kept_ids = {it["id"] for it in kept}
        # File groups in ordered_files order (most lessons first, then alphabetical)
        for fpath in ordered_files:
            group_items = [it for it in kept if fpath in lesson_files.get(it["id"], ())]
            if not group_items:
                continue
            group_items.sort(key=lambda it: (_item_rank(it), str(it.get("id") or "")))
            lines.extend(["", v.SKILL_FILE_HEADING % _sanitise_inline(fpath, limit=240)])
            for it in group_items:
                lines.extend(_render_item_lines(it, lesson_files.get(it["id"], ())))
        unattached = [it for it in kept if not lesson_files.get(it["id"], ()) and it["id"] in kept_ids]
        if unattached:
            unattached.sort(key=lambda it: (_item_rank(it), str(it.get("id") or "")))
            lines.extend(["", v.SKILL_ANY_FILE_HEADING])
            for it in unattached:
                lines.extend(_render_item_lines(it, ()))
    if omitted > 0:
        lines.extend(["", v.HOOK_OMITTED % omitted])
    return "\n".join(lines) + "\n"


def _fits_body(body: str, max_lines: int, budget: int) -> bool:
    return len(body.splitlines()) <= max_lines and mw.estimate_tokens(body) <= budget


def _render_frontmatter(name: str, description: str, digest: str, repo_head: str) -> str:
    return (
        "\n".join(
            [
                "---",
                f"name: {name}",
                f"description: {yaml_quote(description)}",
                "metadata:",
                f"  memlet-version: {yaml_quote(VERSION)}",
                f"  memlet-digest: {yaml_quote(digest)}",
                f"  memlet-repo-head: {yaml_quote(repo_head)}",
                "---",
            ]
        )
        + "\n"
    )


def build_skill(repo_path=".", *, name="memlet-lessons", max_lines=500, budget=5000) -> Dict[str, Any]:
    """Build the Agent Skill (`SKILL.md`) in memory without writing to disk."""
    if not valid_skill_name(name):
        return {
            "ok": False,
            "name": str(name),
            "text": "",
            "digest": "",
            "items": 0,
            "omitted": 0,
            "files": [],
            "notices": [v.SKILL_INVALID_NAME],
            "reason": v.SKILL_INVALID_NAME,
            "error": v.SKILL_INVALID_NAME,
        }
    if (
        isinstance(max_lines, bool)
        or not isinstance(max_lines, int)
        or max_lines < 1
        or isinstance(budget, bool)
        or not isinstance(budget, int)
        or budget < 1
    ):
        return {
            "ok": False,
            "name": name,
            "text": "",
            "digest": "",
            "items": 0,
            "omitted": 0,
            "files": [],
            "notices": [v.SKILL_INVALID_BOUNDS],
            "reason": v.SKILL_INVALID_BOUNDS,
            "error": v.SKILL_INVALID_BOUNDS,
        }
    try:
        root = mw._resolve_root(repo_path)
    except (OSError, ValueError) as exc:
        msg = str(exc)
        return {
            "ok": False,
            "name": name,
            "text": "",
            "digest": "",
            "items": 0,
            "omitted": 0,
            "files": [],
            "notices": [msg],
            "reason": msg,
            "error": msg,
        }

    with mb._lesson_path_projection(root, None) as path_context:
        read = mb.dream_items(root, tiers=(v.APPROVED,))
        attached_map = dict(path_context.get("paths") or {})
        att_meta = mb.attachment_metadata(path_context)

    notices = list(read.get("notices") or [])
    if att_meta.get("attachmentReason") and att_meta["attachmentReason"] not in notices:
        notices.append(att_meta["attachmentReason"])

    if not read.get("ok"):
        reason = "; ".join(notices) or v.SKILL_STATUS_UNREADABLE
        return {
            "ok": False,
            "name": name,
            "text": "",
            "digest": "",
            "items": 0,
            "omitted": 0,
            "files": [],
            "notices": notices,
            "reason": reason,
            "error": reason,
        }

    # Check merge_state adapter if available on memlet_bridge
    merged_ids = set()
    if callable(getattr(mb, "merge_state", None)):
        try:
            queue_graph, _ = mb._load_queue(root)
            if isinstance(queue_graph, dict):
                nodes_by_id = {
                    str(n.get("id")): n
                    for n in queue_graph.get("nodes", [])
                    if isinstance(n, dict) and n.get("id")
                }
                for nid, node in nodes_by_id.items():
                    if _merge_state(node, nodes_by_id).get("state") == "merged":
                        merged_ids.add(nid)
        except (OSError, ValueError, TypeError, KeyError):
            pass

    eligible: List[Dict[str, Any]] = []
    lesson_files: Dict[str, List[str]] = {}
    file_counts: Dict[str, int] = {}

    for item in read.get("items") or []:
        if not isinstance(item, dict) or item.get("tier") != v.APPROVED:
            continue
        lid = str(item.get("id") or "")
        if not lid or lid in merged_ids:
            continue
        quotes = item.get("quoteLines") or []
        cites = item.get("cites") or []
        if not quotes or len(quotes) != len(cites) or any(not c.get("verified") for c in cites):
            continue
        relations = [r for r in v.BRIDGE_RELATIONS if r in (item.get("edgeTypes") or [])]
        roles = item.get("lineRoles")
        if "SUPERSEDES" in relations and (
            not item.get("quarantined") or not isinstance(roles, list) or len(roles) != len(quotes)
        ):
            continue
        if (
            item.get("quarantined")
            and "replaced" not in (roles or [])
            and not any(r in ("BLOCKS", "GOVERNS", "RESOLVES") for r in relations)
        ):
            if v.GRAPH_CURRENT_ONLY_NOTICE not in notices:
                notices.append(v.GRAPH_CURRENT_ONLY_NOTICE)
            continue
        files_for_item = list(dict.fromkeys(attached_map.get(lid, item.get("paths") or [])))
        lesson_files[lid] = files_for_item
        for fpath in files_for_item:
            file_counts[fpath] = file_counts.get(fpath, 0) + 1
        eligible.append(item)

    ordered_files = sorted(file_counts, key=lambda f: (-file_counts[f], f))
    file_order_index = {f: idx for idx, f in enumerate(ordered_files)}

    def _lesson_priority_key(it: Dict[str, Any]) -> Tuple[int, int, int, str]:
        lfiles = lesson_files.get(it["id"], [])
        best_file_idx = min((file_order_index[f] for f in lfiles), default=len(ordered_files))
        return (0 if lfiles else 1, _item_rank(it), best_file_idx, str(it.get("id") or ""))

    ordered_lessons = sorted(eligible, key=_lesson_priority_key)
    repo_head = mw._git_head(root) or v.SKILL_UNCOMMITTED

    kept = list(ordered_lessons)
    omitted = 0
    body = _render_body(repo_head, kept, lesson_files, ordered_files, omitted)
    while kept and not _fits_body(body, max_lines, budget):
        kept.pop()
        omitted = len(ordered_lessons) - len(kept)
        body = _render_body(repo_head, kept, lesson_files, ordered_files, omitted)

    if not kept and not _fits_body(body, max_lines, budget):
        # Compact header fallback for tiny max_lines / budget caps
        compact_lines = [
            v.SKILL_BODY_TITLE,
            v.SKILL_SNAPSHOT_LINE % (repo_head, 0),
            v.HOOK_QUOTE_NOTE,
        ]
        if omitted > 0:
            compact_lines.append(v.HOOK_OMITTED % omitted)
        else:
            compact_lines.append(v.SKILL_EMPTY_LINE)
        while len(compact_lines) > max_lines and len(compact_lines) > 1:
            # Keep the omitted line or snapshot line last
            compact_lines.pop(0)
        body = "\n".join(compact_lines) + "\n"

    kept_file_counts: Dict[str, int] = {}
    for it in kept:
        for fpath in lesson_files.get(it["id"], ()):
            kept_file_counts[fpath] = kept_file_counts.get(fpath, 0) + 1
    # Description and `files` list all attached files from the repository's approved lessons
    # (most lessons first, deterministic tie-break).
    description = _build_description(ordered_files)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    frontmatter = _render_frontmatter(name, description, digest, repo_head)
    text = frontmatter + body

    return {
        "ok": True,
        "name": name,
        "text": text,
        "digest": digest,
        "items": len(kept),
        "omitted": omitted,
        "files": list(ordered_files),
        "notices": notices,
    }


def _parse_skill_file(text: str) -> Optional[Dict[str, Any]]:
    """Parse a Memlet-written SKILL.md into `{name, description, metadata, body, body_digest, frontmatter_tampered}`.
    Returns None if the file was not written by Memlet."""
    if not isinstance(text, str) or not text.startswith("---\n"):
        return None
    end_idx = text.find("\n---\n", 4)
    if end_idx == -1:
        return None
    fm_block = text[4:end_idx]
    body = text[end_idx + 5 :]
    fm_lines = fm_block.split("\n")

    name_val: Optional[str] = None
    desc_val: Optional[str] = None
    in_metadata = False
    metadata: Dict[str, Optional[str]] = {}
    extra_or_malformed = False

    for line in fm_lines:
        if not line.strip():
            extra_or_malformed = True
            continue
        if in_metadata:
            if line.startswith("  ") and not line.startswith("   "):
                key, sep, raw_val = line[2:].partition(":")
                if not sep or not key.strip():
                    extra_or_malformed = True
                    continue
                metadata[key.strip()] = yaml_unquote(raw_val.strip())
                continue
            in_metadata = False
        if line.startswith("name:"):
            if name_val is not None:
                extra_or_malformed = True
            name_val = yaml_unquote(line[len("name:") :].strip())
        elif line.startswith("description:"):
            if desc_val is not None:
                extra_or_malformed = True
            desc_val = yaml_unquote(line[len("description:") :].strip())
        elif line.rstrip() == "metadata:":
            if metadata:
                extra_or_malformed = True
            in_metadata = True
        else:
            extra_or_malformed = True

    required_meta = ("memlet-version", "memlet-digest", "memlet-repo-head")
    if not any(k in metadata for k in required_meta):
        return None
    if not all(k in metadata for k in required_meta):
        return None

    meta_clean: Dict[str, str] = {}
    for k, val in metadata.items():
        if val is None:
            extra_or_malformed = True
            meta_clean[k] = ""
        else:
            meta_clean[k] = val

    if set(meta_clean.keys()) != set(required_meta):
        extra_or_malformed = True

    if not name_val or not valid_skill_name(name_val):
        extra_or_malformed = True
    if not desc_val or not (1 <= len(desc_val) <= 1024):
        extra_or_malformed = True

    recorded_digest = meta_clean.get("memlet-digest", "")
    if not _HEX64_RE.fullmatch(recorded_digest):
        extra_or_malformed = True

    body_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    expected_fm = _render_frontmatter(
        name_val or "",
        desc_val or "",
        recorded_digest,
        meta_clean.get("memlet-repo-head", ""),
    )
    if text[: end_idx + 5] != expected_fm and meta_clean.get("memlet-version") == VERSION:
        extra_or_malformed = True

    return {
        "name": name_val or "",
        "description": desc_val or "",
        "metadata": meta_clean,
        "body": body,
        "body_digest": body_digest,
        "frontmatter_tampered": extra_or_malformed,
    }


def _check_no_symlinks_in_out(root: Path, out: Optional[Any], name: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve and validate the output directory `base_dir` for `--write`.
    Refuses symlinks in `base_dir` or anywhere below `base_dir` (`<name>` and `<name>/SKILL.md`)."""
    if out is None:
        problem = mw.contained_path_problem(root, f"{v.SKILL_DEFAULT_DIR}/{name}/{v.SKILL_FILENAME}", leaf="file")
        if problem:
            return None, v.SKILL_SYMLINK_REFUSED if "symlink" in problem else problem
        return root / v.SKILL_DEFAULT_DIR, None

    if not isinstance(out, (str, os.PathLike)) or not str(out).strip() or "\x00" in str(out):
        return None, v.SKILL_OUTSIDE_DIR_REFUSED

    raw_out = Path(out)
    if not raw_out.is_absolute():
        rel_str = str(raw_out).replace("\\", "/")
        while rel_str.startswith("./"):
            rel_str = rel_str[2:]
        parts = tuple(p for p in rel_str.split("/") if p and p != ".")
        if not parts or any(p == ".." for p in parts):
            return None, v.SKILL_OUTSIDE_DIR_REFUSED
        problem = mw.contained_path_problem(
            root, "/".join(parts + (name, v.SKILL_FILENAME)), leaf="file"
        )
        if problem:
            return None, v.SKILL_SYMLINK_REFUSED if "symlink" in problem else problem
        base_dir = root.joinpath(*parts)
    else:
        if ".." in raw_out.parts:
            return None, v.SKILL_OUTSIDE_DIR_REFUSED
        base_dir = raw_out

    # Check if base_dir itself is a symlink (or a non-directory if it exists)
    if os.path.lexists(str(base_dir)):
        try:
            st = os.lstat(str(base_dir))
        except OSError as exc:
            return None, str(exc)
        if stat.S_ISLNK(st.st_mode):
            return None, v.SKILL_SYMLINK_REFUSED
        if not stat.S_ISDIR(st.st_mode):
            return None, f"{base_dir} is not a directory"

    # Check below base_dir: base_dir / name and base_dir / name / SKILL.md
    skill_dir = base_dir / name
    if os.path.lexists(str(skill_dir)):
        try:
            st = os.lstat(str(skill_dir))
        except OSError as exc:
            return None, str(exc)
        if stat.S_ISLNK(st.st_mode):
            return None, v.SKILL_SYMLINK_REFUSED
        if not stat.S_ISDIR(st.st_mode):
            return None, f"{skill_dir} is not a directory"

    target = skill_dir / v.SKILL_FILENAME
    if os.path.lexists(str(target)):
        try:
            st = os.lstat(str(target))
        except OSError as exc:
            return None, str(exc)
        if stat.S_ISLNK(st.st_mode):
            return None, v.SKILL_SYMLINK_REFUSED
        if not stat.S_ISREG(st.st_mode):
            return None, f"{target} is not a regular file"

    return base_dir, None


def _ensure_skill_dirs(base_dir: Path, name: str) -> Path:
    """Create `base_dir` and `base_dir / name` without following symlinks at `base_dir` or below."""
    missing: List[Path] = []
    cur = base_dir
    while not os.path.lexists(str(cur)):
        missing.append(cur)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    for d in reversed(missing):
        try:
            os.mkdir(str(d))
        except FileExistsError:
            pass
        st = os.lstat(str(d))
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise mw.UnsafePathError(v.SKILL_SYMLINK_REFUSED)

    st_base = os.lstat(str(base_dir))
    if stat.S_ISLNK(st_base.st_mode) or not stat.S_ISDIR(st_base.st_mode):
        raise mw.UnsafePathError(v.SKILL_SYMLINK_REFUSED)

    skill_dir = base_dir / name
    try:
        os.mkdir(str(skill_dir))
    except FileExistsError:
        pass
    st_skill = os.lstat(str(skill_dir))
    if stat.S_ISLNK(st_skill.st_mode) or not stat.S_ISDIR(st_skill.st_mode):
        raise mw.UnsafePathError(v.SKILL_SYMLINK_REFUSED)

    if skill_dir.resolve().parent != base_dir.resolve():
        raise mw.UnsafePathError(v.SKILL_OUTSIDE_DIR_REFUSED)
    return skill_dir


def write_skill(repo_path=".", *, out=None, name="memlet-lessons", force=False) -> Dict[str, Any]:
    """Write `DIR/<name>/SKILL.md` atomically, refusing invalid names, symlinks, and foreign/edited files."""
    if not valid_skill_name(name):
        return {
            "ok": False,
            "name": str(name),
            "written": False,
            "reason": v.SKILL_INVALID_NAME,
            "error": v.SKILL_INVALID_NAME,
        }
    try:
        root = mw._resolve_root(repo_path)
    except (OSError, ValueError) as exc:
        msg = str(exc)
        return {"ok": False, "name": name, "written": False, "reason": msg, "error": msg}

    base_dir, problem = _check_no_symlinks_in_out(root, out, name)
    if problem or base_dir is None:
        reason = problem or v.SKILL_SYMLINK_REFUSED
        return {"ok": False, "name": name, "written": False, "reason": reason, "error": reason}

    skill_dir = base_dir / name
    target = skill_dir / v.SKILL_FILENAME

    if os.path.lexists(str(target)) and not force:
        raw_bytes = mw.read_regular_bytes(target, _MAX_SKILL_READ_BYTES)
        if raw_bytes is None:
            return {
                "ok": False,
                "name": name,
                "written": False,
                "reason": v.SKILL_STATUS_UNREADABLE,
                "error": v.SKILL_STATUS_UNREADABLE,
            }
        try:
            existing_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "ok": False,
                "name": name,
                "written": False,
                "reason": v.SKILL_STATUS_FOREIGN,
                "error": v.SKILL_STATUS_FOREIGN,
            }
        parsed = _parse_skill_file(existing_text)
        if parsed is None:
            return {
                "ok": False,
                "name": name,
                "written": False,
                "reason": v.SKILL_STATUS_FOREIGN,
                "error": v.SKILL_STATUS_FOREIGN,
            }
        if (
            parsed["frontmatter_tampered"]
            or parsed["name"] != name
            or parsed["body_digest"] != parsed["metadata"].get("memlet-digest")
        ):
            return {
                "ok": False,
                "name": name,
                "written": False,
                "reason": v.SKILL_STATUS_EDITED,
                "error": v.SKILL_STATUS_EDITED,
            }

    built = build_skill(root, name=name)
    if not built.get("ok"):
        return dict(built, written=False)

    try:
        skill_dir = _ensure_skill_dirs(base_dir, name)
        target = skill_dir / v.SKILL_FILENAME
        if os.path.lexists(str(target)):
            st = os.lstat(str(target))
            if stat.S_ISLNK(st.st_mode):
                raise mw.UnsafePathError(v.SKILL_SYMLINK_REFUSED)
            if not stat.S_ISREG(st.st_mode):
                raise mw.UnsafePathError(v.SKILL_STATUS_UNREADABLE)
            mode = stat.S_IMODE(st.st_mode)
        else:
            mode = 0o644

        data = built["text"].encode("utf-8")
        fd, tmp = tempfile.mkstemp(prefix=".SKILL.md.memlet-", dir=str(skill_dir))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
            _, recheck_problem = _check_no_symlinks_in_out(root, out, name)
            if recheck_problem:
                raise mw.UnsafePathError(recheck_problem)
            os.replace(tmp, str(target))
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except (OSError, ValueError) as exc:
        msg = str(exc)
        return {"ok": False, "name": name, "written": False, "reason": msg, "error": msg}

    _KNOWN_SKILL_REPOS[str(target)] = str(root)
    with contextlib.suppress(OSError):
        _KNOWN_SKILL_REPOS[str(target.resolve())] = str(root)

    return {
        "ok": True,
        "name": name,
        "path": str(target),
        "written": True,
        "text": built["text"],
        "digest": built["digest"],
        "items": built["items"],
        "omitted": built["omitted"],
        "files": built["files"],
        "notices": built["notices"],
    }


def _find_repo_for_skill(skill_path: Path, expected_head: Optional[str] = None) -> Optional[Path]:
    """Locate the repository root for `check_skill(path)` without modifying on-disk state."""
    override = _check_repo_override.get()
    if override:
        with contextlib.suppress(OSError, ValueError):
            return mw._resolve_root(override)

    for key in (str(skill_path),):
        if key in _KNOWN_SKILL_REPOS:
            cand = Path(_KNOWN_SKILL_REPOS[key])
            if cand.is_dir():
                return cand
    with contextlib.suppress(OSError):
        resolved_key = str(skill_path.resolve())
        if resolved_key in _KNOWN_SKILL_REPOS:
            cand = Path(_KNOWN_SKILL_REPOS[resolved_key])
            if cand.is_dir():
                return cand

    # Walk ancestors of skill_path looking for .memlet or .git
    with contextlib.suppress(OSError):
        cur = skill_path.parent.resolve()
        while True:
            if (cur / ".memlet").is_dir() or (cur / ".git").exists():
                return cur
            if cur.parent == cur:
                break
            cur = cur.parent

    # Check sibling directories of ancestors (up to 3 levels up, e.g. scratch test layout)
    with contextlib.suppress(OSError):
        anc = skill_path.parent.resolve()
        for _ in range(3):
            parent = anc.parent
            if parent == anc:
                break
            candidates: List[Path] = []
            for child in sorted(parent.iterdir()):
                if child != anc and child.is_dir() and not child.is_symlink():
                    if (child / ".memlet").is_dir() or (child / ".git").exists():
                        candidates.append(child)
            if len(candidates) == 1:
                return candidates[0]
            if expected_head and candidates:
                for cand in candidates:
                    if (mw._git_head(cand) or v.SKILL_UNCOMMITTED) == expected_head:
                        return cand
            anc = parent

    with contextlib.suppress(OSError, ValueError):
        return mw._resolve_root(".")
    return None


def check_skill(path) -> Dict[str, Any]:
    """Check whether a written SKILL.md is `current`, `edited`, `stale: lessons or code changed`,
    `not written by Memlet`, or `unreadable`. Never writes."""
    if not isinstance(path, (str, os.PathLike)) or not str(path).strip() or "\x00" in str(path):
        return {"ok": True, "current": False, "reason": v.SKILL_STATUS_UNREADABLE}

    p = Path(path)
    try:
        if not os.path.lexists(str(p)):
            return {"ok": True, "current": False, "reason": v.SKILL_STATUS_UNREADABLE}
        st = os.lstat(str(p))
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return {"ok": True, "current": False, "reason": v.SKILL_STATUS_UNREADABLE}
        if os.path.lexists(str(p.parent)) and stat.S_ISLNK(os.lstat(str(p.parent)).st_mode):
            return {"ok": True, "current": False, "reason": v.SKILL_STATUS_UNREADABLE}
    except OSError:
        return {"ok": True, "current": False, "reason": v.SKILL_STATUS_UNREADABLE}

    raw_bytes = mw.read_regular_bytes(p, _MAX_SKILL_READ_BYTES)
    if raw_bytes is None:
        return {"ok": True, "current": False, "reason": v.SKILL_STATUS_UNREADABLE}
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": True, "current": False, "reason": v.SKILL_STATUS_UNREADABLE}

    parsed = _parse_skill_file(text)
    if parsed is None:
        return {"ok": True, "current": False, "reason": v.SKILL_STATUS_FOREIGN}

    if (
        parsed["frontmatter_tampered"]
        or parsed["body_digest"] != parsed["metadata"].get("memlet-digest")
    ):
        return {"ok": True, "current": False, "reason": v.SKILL_STATUS_EDITED}

    repo_root = _find_repo_for_skill(p, expected_head=parsed["metadata"].get("memlet-repo-head"))
    if repo_root is None:
        return {"ok": True, "current": False, "reason": v.SKILL_STATUS_STALE}

    rebuilt = build_skill(repo_root, name=parsed["name"])
    if not rebuilt.get("ok"):
        return {"ok": True, "current": False, "reason": v.SKILL_STATUS_STALE}

    if rebuilt["text"] == text:
        return {"ok": True, "current": True, "reason": v.SKILL_STATUS_CURRENT}

    current_head = mw._git_head(repo_root) or v.SKILL_UNCOMMITTED
    if (
        rebuilt["digest"] == parsed["metadata"].get("memlet-digest")
        and current_head == parsed["metadata"].get("memlet-repo-head")
    ):
        return {"ok": True, "current": False, "reason": v.SKILL_STATUS_EDITED}

    return {"ok": True, "current": False, "reason": v.SKILL_STATUS_STALE}
