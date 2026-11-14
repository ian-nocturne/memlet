"""Unit N3 — Memlet 9.3: the skillbook (approved lessons packaged as an Agent Skill).

Invariants:
- Approved lessons only, from the single reader (`memlet_bridge.dream_items`).
- Never rewrites lesson text; quotes cited lines verbatim as block quotes after display sanitisation.
- Every Memlet label is on a line outside the block quote; nothing Memlet writes is inside `> ...`.
- No model call, no wall-clock timestamp anywhere in the output.
- Linear build cost in the number of lessons: each item is rendered at most once.
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
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import VERSION, memlet_bridge as mb, memlet_dream as md, memlet_wiki as mw, vocabulary as v

SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
SKILL_RESERVED_NAMES = frozenset({"true", "false", "null", "yes", "no", "on", "off", "y", "n"})
SKILL_LESSON_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
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

_check_repo_override: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "check_repo_override", default=None
)


def valid_skill_name(name: Any) -> bool:
    """Agent Skills specification + YAML plain-scalar safety (S1):
    1-64 chars, starts with a-z, only a-z, 0-9 and `-`, no leading/trailing/doubled hyphen,
    and not a YAML 1.1/1.2 boolean or null keyword."""
    return (
        isinstance(name, str)
        and 1 <= len(name) <= 64
        and name not in SKILL_RESERVED_NAMES
        and bool(SKILL_NAME_RE.fullmatch(name))
    )


def valid_lesson_id(lid: Any) -> bool:
    """Plain Memlet lesson id (Note 3): letters, digits, `-`, `_`, at most 64 characters."""
    return isinstance(lid, str) and bool(SKILL_LESSON_ID_RE.fullmatch(lid))


def _is_safe_skill_path(path: Any) -> bool:
    """Check whether a repository file path can be shown byte-exact inside a single-backtick code span (Note 1)."""
    if not isinstance(path, str) or not (1 <= len(path) <= 240):
        return False
    for ch in path:
        code = ord(ch)
        if (
            ch == "`"
            or ch in ("\n", "\r", "\u0085", "\u2028", "\u2029")
            or code < 0x20
            or 0x7F <= code <= 0x9F
            or unicodedata.category(ch) in ("Cc", "Cf", "Cs")
            or mb._hidden_character(ch)
        ):
            return False
    return True


def _is_safe_cite(c: Any) -> bool:
    """Verify citation dictionary has a safe file path and integer line bounds."""
    if not isinstance(c, dict):
        return False
    f = c.get("file")
    a, b = c.get("start"), c.get("end")
    if (
        not _is_safe_skill_path(f)
        or isinstance(a, bool)
        or not isinstance(a, int)
        or isinstance(b, bool)
        or not isinstance(b, int)
        or a < 1
        or b < a
    ):
        return False
    return True


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


def _sanitise_quote_line(text: Any) -> str:
    """Return the exact shared display sanitiser (`safe_lesson_display`) output for a quoted line (Note 2).
    Avoids double-applying `safe_lesson_display` when `dream_items` already sanitised `quoteLines`."""
    s = str(text)
    if (
        "⟶" in s
        or "<!--" in s
        or "-->" in s
        or len(s) > md.LESSON_TEXT_MAX_CHARS
        or any(mb._hidden_character(c) for c in s)
        or mb._ANSI.search(s)
        or mb._UNSAFE_STRING.search(s)
    ):
        s = mb.safe_lesson_display(s, limit=md.LESSON_TEXT_MAX_CHARS)
    return s


def _build_description(files: Sequence[str]) -> str:
    """Fixed text plus as many attached file paths (byte-exact inside code spans) as fit within 1024 characters."""
    base = v.SKILL_DESCRIPTION_BASE
    if not files:
        return base
    chosen: List[str] = []
    for path in files:
        if not _is_safe_skill_path(path):
            continue
        span = f"`{path}`"
        candidate = base + v.SKILL_DESCRIPTION_FILES_PREFIX + ", ".join(chosen + [span])
        if len(candidate) <= 1024 and len(yaml_quote(candidate)) <= 1024:
            chosen.append(span)
        else:
            break
    if not chosen:
        return base
    return base + v.SKILL_DESCRIPTION_FILES_PREFIX + ", ".join(chosen)


def _item_rank(item: Dict[str, Any]) -> int:
    """Order across and within groups: prohibitions first (0), then replaced instructions (1), then fixes (2), then the rest (3)."""
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
    f = str(c.get("file") or "")
    a, b = c.get("start"), c.get("end")
    return f"{f}:{a}" + (f"-{b}" if b != a else "")


def _render_item_lines(item: Dict[str, Any], item_files: Sequence[str], unsafe=False) -> List[str]:
    """Render one lesson item as markdown lines.
    Quotes use recall display escaping and hidden-character markers; relation and file labels stay outside."""
    rels = [r for r in v.BRIDGE_RELATIONS if r in (item.get("edgeTypes") or [])] or ["LESSON"]
    quotes = list(item.get("quoteLines") or [item.get("text") or ""])
    cites = list(item.get("cites") or [])
    quarantined = bool(item.get("quarantined"))
    roles = list(item.get("lineRoles") or ([None] * len(quotes)))
    parts = list(zip(roles, quotes, cites))
    if "replaced" in roles:
        parts = sorted(parts, key=lambda p: p[0] != "replaced")
    cite_strs = [_format_cite(c) for _, _, c in parts]
    file_strs = [f"`{f}`" for f in item_files]
    if unsafe:
        file_strs.append(v.SKILL_UNSAFE_FILES)
    if not file_strs:
        file_strs = [v.SKILL_ANY_FILE]
    head = v.SKILL_ITEM_HEAD % (
        ", ".join(rels),
        str(item.get("id") or ""),
        ", ".join(cite_strs),
        ", ".join(file_strs),
    )
    lines = [head]
    for role, quote, _cite in parts:
        if quarantined or role in ("replaced", "current"):
            if role == "replaced":
                lines.append(v.SKILL_ROLE_REPLACED_LINE)
            elif role == "current":
                lines.append(v.SKILL_ROLE_CURRENT_LINE)
        lines.append("  > " + _sanitise_quote_line(quote))
    return lines


def _render_body(
    repo_head: str,
    kept: Sequence[Dict[str, Any]],
    lesson_files: Dict[str, List[str]],
    ordered_files: Sequence[str],
    omitted: int,
    *,
    rendered_items: Optional[Dict[str, List[str]]] = None,
    unsafe_path_lessons: Optional[Set[str]] = None,
    omitted_reasons=None,
) -> str:
    lines: List[str] = [
        v.SKILL_BODY_TITLE,
        "",
        v.SKILL_SNAPSHOT_LINE % (repo_head, len(kept)),
        v.SKILL_QUOTE_NOTE,
    ]
    if not kept and omitted == 0 and not any((omitted_reasons or {}).values()):
        lines.extend(["", v.SKILL_EMPTY_LINE])
    elif kept:
        by_file: Dict[str, List[Dict[str, Any]]] = {}
        unattached: List[Dict[str, Any]] = []
        unsafe_items: List[Dict[str, Any]] = []
        unsafe_set = unsafe_path_lessons or set()
        for it in kept:
            lid = it["id"]
            lfiles = lesson_files.get(lid, ())
            for fpath in lfiles:
                by_file.setdefault(fpath, []).append(it)
            if lid in unsafe_set:
                unsafe_items.append(it)
            elif not lfiles:
                unattached.append(it)

        for fpath in ordered_files:
            group_items = by_file.get(fpath)
            if not group_items:
                continue
            group_items.sort(key=lambda it: (_item_rank(it), str(it.get("id") or "")))
            lines.extend(["", v.SKILL_FILE_HEADING % fpath])
            for it in group_items:
                lid = it["id"]
                item_lines = (
                    rendered_items[lid]
                    if rendered_items is not None and lid in rendered_items
                    else _render_item_lines(it, lesson_files.get(lid, ()), lid in unsafe_set)
                )
                lines.extend(item_lines)

        for unattached, heading in ((unattached, v.SKILL_ANY_FILE_HEADING), (unsafe_items, v.SKILL_UNSAFE_FILE_HEADING)):
            if not unattached:
                continue
            unattached.sort(key=lambda it: (_item_rank(it), str(it.get("id") or "")))
            lines.extend(["", heading])
            for it in unattached:
                lid = it["id"]
                item_lines = (
                    rendered_items[lid]
                    if rendered_items is not None and lid in rendered_items
                    else _render_item_lines(it, lesson_files.get(lid, ()), lid in unsafe_set)
                )
                lines.extend(item_lines)
    footer = _omission_line(omitted, omitted_reasons or {})
    if footer:
        lines.extend(["", footer])
    return "\n".join(lines) + "\n"


def _render_frontmatter(
    name: str,
    description: str,
    digest: str,
    repo_head: str,
    *,
    version: Optional[str] = None,
) -> str:
    ver = VERSION if version is None else version
    return (
        "\n".join(
            [
                "---",
                f"name: {name}",
                f"description: {yaml_quote(description)}",
                "metadata:",
                f"  memlet-version: {yaml_quote(ver)}",
                f"  memlet-digest: {yaml_quote(digest)}",
                f"  memlet-repo-head: {yaml_quote(repo_head)}",
                "---",
            ]
        )
        + "\n"
    )


def _assemble_skill_text(
    name: str,
    description: str,
    body: str,
    repo_head: str,
    *,
    version: Optional[str] = None,
) -> Tuple[str, str]:
    """Compute `memlet-digest` over every byte of the file except the digest value itself (S3/S4/Note 9)."""
    without_digest = _render_frontmatter(name, description, "", repo_head, version=version) + body
    digest = hashlib.sha256(without_digest.encode("utf-8")).hexdigest()
    text = _render_frontmatter(name, description, digest, repo_head, version=version) + body
    return text, digest


def _omission_line(budget, reasons):
    counts = dict(reasons, budget=budget)
    return (v.SKILL_OMITTED_REASONS % "; ".join("%s: %d" % (k, n) for k, n in counts.items() if n)
            if any(counts.values()) else "")


def _int_token_cost(n: int) -> int:
    return -(-len(str(n)) // 4)


@md.fresh_read
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
        root = _resolve_memlet_repo(repo_path)
        if root is None:
            raise ValueError(v.SKILL_STATUS_NO_REPO)
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
        read = mb.dream_items(root, tiers=(v.APPROVED,), _path_context=path_context)
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

    eligible: List[Dict[str, Any]] = []
    lesson_files: Dict[str, List[str]] = {}
    unsafe_path_lessons: Set[str] = set()
    file_counts: Dict[str, int] = {}

    omitted_invalid_id = 0
    omitted_ineligible = 0
    omitted_reverify = 0
    omitted_unsafe_paths = 0

    for item in read.get("items") or []:
        if not isinstance(item, dict) or item.get("tier") != v.APPROVED:
            continue
        lid = item.get("id")
        if not valid_lesson_id(lid):
            omitted_invalid_id += 1
            continue
        quotes = item.get("quoteLines") or []
        cites = item.get("cites") or []
        if not quotes or len(quotes) != len(cites) or any(not c.get("verified") or not _is_safe_cite(c) for c in cites):
            omitted_ineligible += 1
            continue
        relations = [r for r in v.BRIDGE_RELATIONS if r in (item.get("edgeTypes") or [])]
        if not relations:
            omitted_ineligible += 1
            continue
        roles = item.get("lineRoles")
        quarantined = bool(item.get("quarantined"))
        if "SUPERSEDES" in relations and (
            not quarantined or not isinstance(roles, list) or len(roles) != len(quotes)
        ):
            omitted_ineligible += 1
            continue
        if quarantined and (
            not isinstance(roles, list)
            or len(roles) != len(quotes)
            or not set(roles) <= {"replaced", "current"}
        ):
            omitted_ineligible += 1
            continue
        effective_roles = roles if quarantined else ["current"] * len(quotes)
        if item.get("replacementLabel") and "current" in effective_roles:
            # M3 / Note 4: A lesson whose current line carries the re-verify marker is omitted
            # just like in the Routlet capsule projection, and counted in notices.
            omitted_reverify += 1
            continue

        raw_files = list(dict.fromkeys(attached_map.get(lid, item.get("paths") or [])))
        safe_files: List[str] = []
        for fpath in raw_files:
            if _is_safe_skill_path(fpath):
                safe_files.append(fpath)
            else:
                omitted_unsafe_paths += 1
                unsafe_path_lessons.add(lid)
        lesson_files[lid] = safe_files
        for fpath in safe_files:
            file_counts[fpath] = file_counts.get(fpath, 0) + 1
        eligible.append(item)

    if omitted_invalid_id > 0:
        notices.append(v.SKILL_OMITTED_INVALID_ID_NOTICE % omitted_invalid_id)
    if omitted_ineligible > 0:
        notices.append(v.SKILL_OMITTED_INELIGIBLE_NOTICE % omitted_ineligible)
    if omitted_reverify > 0:
        notices.append(v.SKILL_OMITTED_REVERIFY_NOTICE % omitted_reverify)
    if omitted_unsafe_paths > 0:
        notices.append(v.SKILL_OMITTED_UNSAFE_PATH_NOTICE % omitted_unsafe_paths)

    omitted_reasons = {"invalid-id": omitted_invalid_id, "ineligible": omitted_ineligible, "reverify": omitted_reverify}
    all_ordered_files = sorted(file_counts, key=lambda f: (-file_counts[f], f))
    file_order_index = {f: idx for idx, f in enumerate(all_ordered_files)}

    def _lesson_priority_key(it: Dict[str, Any]) -> Tuple[int, int, int, str]:
        # S6: Rank by relation kind first (prohibitions, replaced, fixes, other),
        # then attached (0) before unattached (1) within the same kind.
        lfiles = lesson_files.get(it["id"], [])
        best_file_idx = min((file_order_index[f] for f in lfiles), default=len(all_ordered_files))
        return (_item_rank(it), 0 if lfiles else 1, best_file_idx, str(it.get("id") or ""))

    ordered_lessons = sorted(eligible, key=_lesson_priority_key)
    repo_head = mw._git_head(root) or v.SKILL_UNCOMMITTED

    # Precompute fixed header and footer line/token costs once (M1)
    header_base_tokens = (
        mw.estimate_tokens(v.SKILL_BODY_TITLE)
        + (mw.estimate_tokens(v.SKILL_SNAPSHOT_LINE % (repo_head, 0)) - 1)
        + mw.estimate_tokens(v.SKILL_QUOTE_NOTE)
    )
    any_heading_tokens = mw.estimate_tokens(v.SKILL_ANY_FILE_HEADING)
    file_heading_tokens: Dict[str, int] = {}

    total_lessons = len(ordered_lessons)
    rendered_map: Dict[str, List[str]] = {}

    if total_lessons == 0:
        # S7: even on an empty repository, max_lines and budget must be large enough
        # for the fixed header plus one minimal lesson item (at least 8 lines).
        min_one_item_tokens = (
            header_base_tokens
            + 1
            + any_heading_tokens
            + mw.estimate_tokens("- [GOVERNS] lesson DRM-1 (cites f:1; files: any file)\n  > x")
        )
        if max_lines < 8 or budget < min_one_item_tokens:
            return {
                "ok": False,
                "name": name,
                "text": "",
                "digest": "",
                "items": 0,
                "omitted": 0,
                "files": [],
                "notices": notices + [v.SKILL_BOUNDS_TOO_SMALL],
                "reason": v.SKILL_BOUNDS_TOO_SMALL,
                "error": v.SKILL_BOUNDS_TOO_SMALL,
            }
        kept: List[Dict[str, Any]] = []
        omitted = 0
    else:
        running_lines = 4
        running_tokens = header_base_tokens
        seen_files: Set[str] = set()
        seen_unattached = set()
        best_k = 0

        for idx, it in enumerate(ordered_lessons):
            lid = it["id"]
            lfiles = lesson_files.get(lid, [])
            in_any = (not lfiles) or (lid in unsafe_path_lessons)
            num_appearances = len(lfiles) + (1 if in_any else 0)

            # Fast upper-bound pruning: adding another item requires at least 2 lines per appearance.
            # If even without new headings or omitted footer we already exceed max_lines or budget, stop.
            if running_lines + 2 * num_appearances > max_lines and idx > 0:
                break

            item_lines = _render_item_lines(it, lfiles, lid in unsafe_path_lessons)
            rendered_map[lid] = item_lines
            item_line_count = len(item_lines)
            item_token_cost = mw.estimate_tokens("\n".join(item_lines))

            for fpath in lfiles:
                if fpath not in seen_files:
                    seen_files.add(fpath)
                    running_lines += 2
                    htok = file_heading_tokens.get(fpath)
                    if htok is None:
                        htok = mw.estimate_tokens(v.SKILL_FILE_HEADING % fpath)
                        file_heading_tokens[fpath] = htok
                    running_tokens += htok
            heading = v.SKILL_UNSAFE_FILE_HEADING if lid in unsafe_path_lessons else v.SKILL_ANY_FILE_HEADING
            if in_any and heading not in seen_unattached:
                seen_unattached.add(heading)
                running_lines += 2
                running_tokens += mw.estimate_tokens(heading)

            running_lines += num_appearances * item_line_count
            running_tokens += num_appearances * item_token_cost

            k = idx + 1
            rem = total_lessons - k
            footer = _omission_line(rem, omitted_reasons)
            cand_lines = running_lines + (2 if footer else 0)
            cand_tokens = (
                running_tokens
                + _int_token_cost(k)
                + mw.estimate_tokens(footer)
            )
            if cand_lines <= max_lines and cand_tokens <= budget:
                best_k = k
            elif running_lines > max_lines or running_tokens + _int_token_cost(k) > budget:
                # Even without the omitted footer, we have exceeded the cap; no larger k can fit.
                break

        if best_k == 0:
            # S7: max_lines or budget is too small for the fixed header plus one item.
            return {
                "ok": False,
                "name": name,
                "text": "",
                "digest": "",
                "items": 0,
                "omitted": total_lessons,
                "files": [],
                "notices": notices + [v.SKILL_BOUNDS_TOO_SMALL],
                "reason": v.SKILL_BOUNDS_TOO_SMALL,
                "error": v.SKILL_BOUNDS_TOO_SMALL,
            }

        kept = ordered_lessons[:best_k]
        omitted = total_lessons - best_k

    # Note 7: the description and `files` list only files that still have an item in the file.
    kept_file_counts: Dict[str, int] = {}
    for it in kept:
        for fpath in lesson_files.get(it["id"], ()):
            kept_file_counts[fpath] = kept_file_counts.get(fpath, 0) + 1
    kept_ordered_files = sorted(kept_file_counts, key=lambda f: (-kept_file_counts[f], f))

    body = _render_body(
        repo_head,
        kept,
        lesson_files,
        kept_ordered_files,
        omitted,
        rendered_items=rendered_map,
        unsafe_path_lessons=unsafe_path_lessons,
        omitted_reasons=omitted_reasons,
    )
    description = _build_description(kept_ordered_files)
    text, digest = _assemble_skill_text(name, description, body, repo_head)

    return {
        "ok": True,
        "name": name,
        "text": text,
        "digest": digest,
        "items": len(kept),
        "omitted": omitted + sum(omitted_reasons.values()),
        "omittedReasons": dict(omitted_reasons, budget=omitted),
        "files": list(kept_ordered_files),
        "notices": notices,
    }


def _inspect_skill_file(text: str) -> Dict[str, Any]:
    """Single shared inspector for `--write` and `--check` (S3 / S4 / Note 9).
    Returns a dict with `status` in `{"foreign", "edited", "stale_version", "ok"}`."""
    if not isinstance(text, str) or not text.startswith("---\n"):
        return {"status": "foreign"}
    end_idx = text.find("\n---\n", 4)
    if end_idx == -1:
        return {"status": "foreign"}
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
            raw_name = line[len("name:") :].strip()
            # `name` must be unquoted in Memlet-written files
            if raw_name.startswith('"') or raw_name.startswith("'"):
                extra_or_malformed = True
            name_val = yaml_unquote(raw_name)
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
        return {"status": "foreign"}
    if not all(k in metadata for k in required_meta):
        return {"status": "foreign"}

    meta_clean: Dict[str, str] = {}
    for k, val in metadata.items():
        if val is None or not str(val):
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

    if extra_or_malformed:
        return {"status": "edited"}

    recorded_version = meta_clean["memlet-version"]
    recorded_head = meta_clean["memlet-repo-head"]
    expected_text, expected_digest = _assemble_skill_text(
        name_val or "",
        desc_val or "",
        body,
        recorded_head,
        version=recorded_version,
    )
    if recorded_digest != expected_digest or text != expected_text:
        return {"status": "edited"}

    if recorded_version != VERSION:
        return {
            "status": "stale_version",
            "name": name_val,
            "description": desc_val,
            "metadata": meta_clean,
            "body": body,
            "version": recorded_version,
        }

    return {
        "status": "ok",
        "name": name_val,
        "description": desc_val,
        "metadata": meta_clean,
        "body": body,
        "version": recorded_version,
    }


def _check_no_symlinks_in_out(
    root: Path,
    out: Optional[Any],
    name: str,
    *,
    cwd: Optional[Path] = None,
) -> Tuple[Optional[Path], Optional[str]]:
    """Relative output is repository-relative; in-repo ancestors may never be symlinks."""
    if out is not None and (not isinstance(out, (str, os.PathLike)) or not str(out).strip() or "\x00" in str(out)):
        return None, v.SKILL_OUTSIDE_DIR_REFUSED
    raw = Path(v.SKILL_DEFAULT_DIR if out is None else out)
    if ".." in raw.parts:
        return None, v.SKILL_OUTSIDE_DIR_REFUSED
    base_dir = raw if raw.is_absolute() else root / raw
    # /tmp and /private/tmp (or a caller's alias of the repository) must obey
    # the same below-root rule without resolving away a hostile child symlink.
    for ancestor in [base_dir] + list(base_dir.parents):
        try:
            if ancestor.samefile(root):
                base_dir = root / base_dir.relative_to(ancestor)
                break
        except OSError:
            continue
    try:
        relative = base_dir.relative_to(root)
    except ValueError:
        relative = None
    if relative is not None:
        problem = mw.contained_path_problem(root, (relative / name / v.SKILL_FILENAME).as_posix(), leaf="file")
        if problem:
            return None, v.SKILL_SYMLINK_REFUSED if "symlink" in problem else problem
    if base_dir.exists() and not base_dir.is_dir():
        return None, str(base_dir) + " is not a directory"

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


def _check_existing_target(target: Path, name: str, force: bool) -> Optional[str]:
    """Validate an existing target file on disk (symlink, regular file, foreign or edited unless `force=True`)."""
    if not os.path.lexists(str(target)):
        return None
    try:
        st = os.lstat(str(target))
    except OSError as exc:
        return str(exc)
    if stat.S_ISLNK(st.st_mode):
        return v.SKILL_SYMLINK_REFUSED
    if not stat.S_ISREG(st.st_mode):
        return f"{target} is not a regular file"
    if force:
        return None
    raw_bytes = mw.read_regular_bytes(target, _MAX_SKILL_READ_BYTES)
    if raw_bytes is None:
        return v.SKILL_STATUS_UNREADABLE
    try:
        existing_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return v.SKILL_STATUS_FOREIGN
    inspected = _inspect_skill_file(existing_text)
    if inspected["status"] == "foreign":
        return v.SKILL_STATUS_FOREIGN
    if inspected["status"] == "edited" or inspected.get("name") != name:
        return v.SKILL_STATUS_EDITED
    return None


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
    if not base_dir.is_dir():
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


@md.fresh_read
def write_skill(repo_path=".", *, out=None, name="memlet-lessons", max_lines=500, budget=5000, force=False):
    """Serialize build through rename with the repository's existing write lock."""
    root = _resolve_memlet_repo(repo_path)
    if root is None:
        return {"ok": False, "written": False, "reason": v.SKILL_STATUS_NO_REPO, "error": v.SKILL_STATUS_NO_REPO}
    try:
        with mw.memlet_write_lock(root):
            return _write_skill_locked(root, out=out, name=name, max_lines=max_lines, budget=budget, force=force)
    except (OSError, ValueError) as exc:
        return {"ok": False, "written": False, "reason": str(exc), "error": str(exc)}


def _write_skill_locked(
    repo_path=".",
    *,
    out=None,
    name="memlet-lessons",
    max_lines=500,
    budget=5000,
    force=False,
) -> Dict[str, Any]:
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
        initial_cwd = Path.cwd().resolve()
    except (OSError, ValueError) as exc:
        msg = str(exc)
        return {"ok": False, "name": name, "written": False, "reason": msg, "error": msg}

    base_dir, problem = _check_no_symlinks_in_out(root, out, name, cwd=initial_cwd)
    if problem or base_dir is None:
        reason = problem or v.SKILL_SYMLINK_REFUSED
        return {"ok": False, "name": name, "written": False, "reason": reason, "error": reason}

    skill_dir = base_dir / name
    target = skill_dir / v.SKILL_FILENAME

    existing_problem = _check_existing_target(target, name, force)
    if existing_problem:
        return {
            "ok": False,
            "name": name,
            "written": False,
            "reason": existing_problem + (v.SKILL_FORCE_NOTE if existing_problem in (v.SKILL_STATUS_EDITED, v.SKILL_STATUS_FOREIGN) else ""),
            "error": existing_problem,
        }

    built = build_skill(root, name=name, max_lines=max_lines, budget=budget)
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
            # Note 8: repeat the exists / edited / symlink checks immediately before the rename, after the build.
            _, recheck_problem = _check_no_symlinks_in_out(root, out, name, cwd=initial_cwd)
            if not recheck_problem:
                recheck_problem = _check_existing_target(target, name, force)
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


def _resolve_memlet_repo(repo_path: Optional[Any] = None) -> Optional[Path]:
    """M2: `--check` never guesses a repository. The repository is `--repo DIR` when given,
    otherwise the current directory; no process-wide cache, no sibling search."""
    target = repo_path if repo_path is not None else (_check_repo_override.get() or ".")
    if not isinstance(target, (str, os.PathLike)) or not str(target).strip() or "\x00" in str(target):
        return None
    try:
        root = mw._resolve_root(target)
    except (OSError, ValueError):
        return None
    memlet_dir = root / mw.MEMLET_DIR
    try:
        if not os.path.lexists(str(memlet_dir)):
            return None
        st = os.lstat(str(memlet_dir))
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return None
    except OSError:
        return None
    return root


@md.fresh_read
def check_skill(path, *, repo_path=None, max_lines=500, budget=5000) -> Dict[str, Any]:
    """Check whether a written SKILL.md is `current`, `edited`, `stale: written by Memlet X`,
    `stale: lessons or code changed`, `no repository to compare with`, `not written by Memlet`,
    or `unreadable`. Never writes."""
    repo_root = _resolve_memlet_repo(repo_path)
    if repo_root is None:
        return {"ok": True, "current": False, "reason": v.SKILL_STATUS_NO_REPO}
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

    inspected = _inspect_skill_file(text)
    status = inspected["status"]
    if status == "foreign":
        return {"ok": True, "current": False, "reason": v.SKILL_STATUS_FOREIGN}
    if status == "edited":
        return {"ok": True, "current": False, "reason": v.SKILL_STATUS_EDITED}
    if status == "stale_version":
        return {
            "ok": True,
            "current": False,
            "reason": v.SKILL_STATUS_STALE_VERSION % inspected["version"],
        }

    repo_root = _resolve_memlet_repo(repo_path)
    if repo_root is None:
        return {"ok": True, "current": False, "reason": v.SKILL_STATUS_NO_REPO}

    rebuilt = build_skill(repo_root, name=inspected["name"], max_lines=max_lines, budget=budget)
    if not rebuilt.get("ok"):
        return {"ok": True, "current": False, "reason": v.SKILL_STATUS_STALE}

    if rebuilt["text"] == text:
        return {"ok": True, "current": True, "reason": v.SKILL_STATUS_CURRENT}

    # Note 5: ignore the recorded commit (memlet-repo-head and the body line that names it).
    # When only the commit differs, the answer is `current` with `headMoved: true`.
    recorded_head = inspected["metadata"]["memlet-repo-head"]
    current_head = mw._git_head(repo_root) or v.SKILL_UNCOMMITTED
    if recorded_head != current_head:
        rebuilt_inspected = _inspect_skill_file(rebuilt["text"])
        if rebuilt_inspected.get("status") == "ok":
            rebuilt_body_lines = rebuilt_inspected["body"].split("\n")
            expected_curr_snap = v.SKILL_SNAPSHOT_LINE % (current_head, rebuilt["items"])
            if len(rebuilt_body_lines) > 2 and rebuilt_body_lines[2] == expected_curr_snap:
                rebuilt_body_lines[2] = v.SKILL_SNAPSHOT_LINE % (recorded_head, rebuilt["items"])
                body_at_recorded = "\n".join(rebuilt_body_lines)
                text_at_recorded, _ = _assemble_skill_text(
                    inspected["name"],
                    rebuilt_inspected["description"],
                    body_at_recorded,
                    recorded_head,
                    version=VERSION,
                )
                if text_at_recorded == text:
                    return {
                        "ok": True,
                        "current": True,
                        "headMoved": True,
                        "reason": v.SKILL_STATUS_CURRENT,
                    }

    return {"ok": True, "current": False, "reason": v.SKILL_STATUS_STALE}
