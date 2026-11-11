"""Zero-Setup Repository Procedure & Instruction Drift Auditor (`memlet check`, formerly `memlet audit`).

Scans repository instruction files (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`,
`.cursor/rules/*.mdc`, `.github/copilot-instructions.md`, `README.md`, `docs/**/*.md`,
`memory_skills/**/SKILL.md`) without requiring prior SQLite or task initialization.

Detects:
- `missing_path`: Referenced repository file path no longer exists.
- `missing_script`: `npm/pnpm/yarn/bun run <script>` or `make <target>` missing from `package.json` / `Makefile`.
- `missing_symbol`: Referenced function, class, method, interface, or script symbol missing from target file.
- `signature_drift`: Breaking parameter arity, required keyword argument, interface field, or git-diff contract drift.
- `unsupported_anchor`: Symbol anchor attached to a language file with `resolution == "unsupported"`.

Supports Git-diff blast radius analysis (`--base <ref> --head <ref>`) and outputs
structured JSON, human-readable Markdown, or GitHub Code Scanning SARIF v2.1.0.
"""

from __future__ import annotations

from pathlib import Path
from .vocabulary import suggested_label

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from .code_anchors import (
    extract_file_symbol_hashes,
    extract_file_symbol_signatures,
    is_breaking_symbol_change,
)

EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    "__pycache__",
    ".next",
    ".nuxt",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "site-packages",
}

KNOWN_CODE_EXTENSIONS = (
    "json",
    "yaml",
    "toml",
    "tsx",
    "jsx",
    "mjs",
    "cjs",
    "mdc",
    "sql",
    "yml",
    "py",
    "ts",
    "js",
    "sh",
    "md",
    "go",
    "rs",
    "tf",
)

EXT_PATTERN = "|".join(KNOWN_CODE_EXTENSIONS)

# Matches explicit `path/to/file.ext::Symbol` or `path/to/file.ext#Symbol`
EXPLICIT_ANCHOR_RE = re.compile(
    rf"(?P<path>(?:[A-Za-z0-9_.-]+/)*(?:Makefile|makefile|GNUmakefile)|[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.(?:{EXT_PATTERN})(?![A-Za-z0-9_]))"
    r"(?:(?:::|#)(?P<symbol>[A-Za-z_][A-Za-z0-9_.-]*))"
)

_ANCHOR_SEP_RE = re.compile(r"::|#")
_ANCHOR_PATH_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-/")
_ANCHOR_SYMBOL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
ANCHOR_PATH_MAX_CHARS = 1024


_SYMBOL_START_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_MAKEFILE_NAMES = ("Makefile", "makefile", "GNUmakefile")


def _leftmost_anchor_start(text: str, left: int, end: int) -> Optional[int]:
    """Leftmost start in [left, end) from which the path part of EXPLICIT_ANCHOR_RE matches and ends exactly at
    `end` (the separator), computed directly instead of retrying the path regex from every start (that was quadratic
    in the 1 KB window per separator). `text[left:end]` holds path characters only. The path is `/`-separated
    non-empty segments whose last one is `<1+ chars>.<ext>` or a Makefile name; so any start after the last empty
    segment (`//`) that is not itself a `/` works, as long as the last segment qualifies."""
    win = text[left:end]
    if not win or win.endswith("/"):
        return None
    dbl = win.rfind("//")
    s0 = dbl + 2 if dbl >= 0 else 0
    if s0 < len(win) and win[s0] == "/":
        s0 += 1
    if s0 >= len(win):
        return None
    last_slash = win.rfind("/")
    seg_start = max(last_slash + 1, s0)  # the last segment as seen from the leftmost start
    last_seg = win[seg_start:]
    whole_last = win[last_slash + 1:]
    candidates = []
    if any(whole_last.endswith("." + e) and len(last_seg) >= len(e) + 2 for e in KNOWN_CODE_EXTENSIONS):
        candidates.append(s0)
    if s0 <= last_slash and whole_last in _MAKEFILE_NAMES:
        candidates.append(s0)  # `(?:seg/)*Makefile` from the leftmost start
    # the name alone (zero `seg/` repetitions) may also start inside the last segment
    candidates += [len(win) - len(name) for name in _MAKEFILE_NAMES
                   if win.endswith(name) and len(win) - len(name) >= s0]
    return left + min(candidates) if candidates else None


def iter_explicit_anchors(text: str):
    """`EXPLICIT_ANCHOR_RE.finditer(text)` in linear time.

    Run on a whole line, the pattern retries its path part from every position of a long run of path
    characters, which is quadratic (a 100 KB line took over a minute). Every match contains a `::` or `#`
    separator, so each separator is handled once: the symbol must start right after it (else no match uses it),
    the path must end right before it, so the path-plus-separator part is searched only in the window of path
    characters to its left (at most `ANCHOR_PATH_MAX_CHARS`, far beyond any real path) with the search ending at the
    separator -- never from starts inside the long symbol run after it, which the earlier window search retried
    one by one (`a.py::` + 50,000 `-` took 21 s). The full match is then taken once at the found start. Matches are
    the same objects, with the same absolute spans, as `finditer` would yield for paths within that length.
    """
    last_end = 0
    n = len(text)
    for sep in _ANCHOR_SEP_RE.finditer(text):
        if sep.start() < last_end:
            continue
        if sep.end() >= n or text[sep.end()] not in _SYMBOL_START_CHARS:
            continue  # `[A-Za-z_]` must follow the separator: no match can use this one
        left = sep.start()
        floor = max(last_end, left - ANCHOR_PATH_MAX_CHARS)
        while left > floor and text[left - 1] in _ANCHOR_PATH_CHARS:
            left -= 1
        if left == sep.start():
            continue
        start = _leftmost_anchor_start(text, left, sep.start())
        if start is None:
            continue
        m = EXPLICIT_ANCHOR_RE.match(text, start)
        if m is not None and m.start() >= last_end:
            last_end = m.end()
            yield m

# Matches backticked file paths like `src/auth.ts`, `payment_gateway.py`, `Makefile`
BACKTICK_FILE_RE = re.compile(
    rf"`(?P<path>(?:\./)?(?:(?:[A-Za-z0-9_.-]+/)*(?:Makefile|makefile|GNUmakefile)|[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.(?:{EXT_PATTERN})(?![A-Za-z0-9_])))(?:::|#(?P<symbol>[A-Za-z_][A-Za-z0-9_.-]*))?`"
)

# ---------------------------------------------------------------------------------------------------------------
# Linear-time text scanning. A regex that retries a long unmatched run from every start position (a lazy `.*?`
# block per unclosed opener, `\[[^\]]+\]` per `[`, an unanchored `[a-z][a-z.+-]*://`) is quadratic on hostile input.
# The objects below give the same matches as the regex they replace (same leftmost-first semantics, same spans) but
# visit each character a bounded number of times. They expose the `re.Pattern` methods their callers use.


class SpanMatch:
    """A `re.Match` stand-in (group 0 / group 1, start, end, span) for matches found without `re`."""

    __slots__ = ("string", "_start", "_end")

    def __init__(self, string: str, start: int, end: int) -> None:
        self.string, self._start, self._end = string, start, end

    def group(self, *groups: Any) -> Any:
        if not groups:
            return self.string[self._start:self._end]
        if len(groups) == 1:
            if groups[0] not in (0, 1):
                raise IndexError("no such group")
            return self.string[self._start:self._end]
        return tuple(self.group(g) for g in groups)

    def __getitem__(self, g: Any) -> str:
        return self.group(g)

    def start(self, g: int = 0) -> int:
        return self._start

    def end(self, g: int = 0) -> int:
        return self._end

    def span(self, g: int = 0) -> Tuple[int, int]:
        return self._start, self._end


def _expand(repl: Any, m: Any) -> str:
    return repl(m) if callable(repl) else repl


class _LinearPattern:
    """Common `search` / `finditer` / `findall` / `sub` / `subn` over `_spans(text, pos)` (non-overlapping, in order).
    A string replacement is inserted literally (no backreferences)."""

    pattern = ""

    def _spans(self, text: str, pos: int = 0):  # pragma: no cover - abstract
        raise NotImplementedError

    def finditer(self, text: str, pos: int = 0):
        for s, e in self._spans(text, pos):
            yield SpanMatch(text, s, e)

    def search(self, text: str, pos: int = 0):
        return next(self.finditer(text, pos), None)

    def findall(self, text: str) -> List[str]:
        return [text[s:e] for s, e in self._spans(text)]

    def subn(self, repl: Any, text: str, count: int = 0) -> Tuple[str, int]:
        out: List[str] = []
        last = n = 0
        for s, e in self._spans(text):
            out.append(text[last:s])
            out.append(_expand(repl, SpanMatch(text, s, e)))
            last = e
            n += 1
            if count and n >= count:
                break
        if not n:
            return text, 0
        out.append(text[last:])
        return "".join(out), n

    def sub(self, repl: Any, text: str, count: int = 0) -> str:
        return self.subn(repl, text, count)[0]


class LazyBlockPattern(_LinearPattern):
    """`BEGIN[\\s\\S]*?END<suffix>` in linear time.

    Every END match is located once (overlapping occurrences included, as the lazy scan would see them); each BEGIN
    found by a left-to-right search takes the first END that starts at or after the BEGIN's end, exactly as the lazy
    regex does. A BEGIN with no END after it means no later BEGIN has one either (BEGIN ends grow with their starts),
    so the scan stops instead of retrying every remaining opener against the rest of the text."""

    def __init__(self, pattern: str, begin: "re.Pattern[str]", end: "re.Pattern[str]", end_prefix: str,
                 suffix: Optional[str] = None, *, dotall: bool = True,
                 full: Optional["re.Pattern[str]"] = None) -> None:
        """`dotall=False`: the body cannot cross a newline (`.` without re.S); a BEGIN whose first END lies beyond
        the end of its line fails, and so does every later BEGIN on that line, so the scan resumes on the next line.
        `full`: the equivalent single regex, matched once at each found start to return real match objects (with
        named groups); it is anchored there and stops at the same first END, so it costs the span length."""
        self.pattern, self._begin, self._end, self._end_prefix, self._suffix = pattern, begin, end, end_prefix, suffix
        self._dotall, self._full = dotall, full

    def _end_spans(self, text: str) -> Tuple[List[int], List[int]]:
        starts, ends = [], []
        i = text.find(self._end_prefix)
        while i >= 0:
            m = self._end.match(text, i)
            if m:
                starts.append(i)
                ends.append(m.end())
            i = text.find(self._end_prefix, i + 1)
        return starts, ends

    def _spans(self, text: str, pos: int = 0):
        import bisect

        if self._end_prefix not in text:
            return
        starts, ends = self._end_spans(text)
        while True:
            m = self._begin.search(text, pos)
            if m is None:
                return
            i = bisect.bisect_left(starts, m.end())
            if not self._dotall:
                nl = text.find("\n", m.end())
                if i == len(starts) or (nl >= 0 and starts[i] >= nl):
                    if nl < 0:
                        return
                    pos = nl + 1  # no END on this line: no later BEGIN on it has one either
                    continue
            elif i == len(starts):
                return
            e = ends[i]
            if self._suffix and text.startswith(self._suffix, e):
                e += len(self._suffix)
            yield m.start(), e
            pos = e if e > m.start() else m.start() + 1

    def finditer(self, text: str, pos: int = 0):
        for s, e in self._spans(text, pos):
            if self._full is not None:
                m = self._full.match(text, s)
                if m is not None:
                    yield m
                    continue
            yield SpanMatch(text, s, e)


class _RunEnd:
    """End of the maximal run of `cls` characters that contains / starts at a position, memoised so that every
    start inside one run is answered in O(1) after the run was scanned once."""

    def __init__(self, text: str, cls_star: "re.Pattern[str]") -> None:
        self.text, self.rx, self.lo, self.hi = text, cls_star, -1, -1

    def __call__(self, q: int) -> int:
        if self.lo <= q <= self.hi:
            return self.hi
        self.lo, self.hi = q, self.rx.match(self.text, q).end()
        return self.hi


class _MemoSearch:
    """`rx.search(text, pos)` for non-decreasing `pos`, re-using the last answer while it is still the leftmost."""

    def __init__(self, text: str, rx: "re.Pattern[str]") -> None:
        self.text, self.rx, self.frm, self.m = text, rx, -1, None

    def __call__(self, pos: int):
        if 0 <= self.frm <= pos and (self.m is None or self.m.start() >= pos):
            return self.m
        self.frm, self.m = pos, self.rx.search(self.text, pos)
        return self.m


def regex_finder(rx: "re.Pattern[str]"):
    """Finder for alternatives whose single attempt is bounded (a literal prefix and no long run before a test that
    can fail): `rx.search` from each position."""
    def factory(text: str):
        search = _MemoSearch(text, rx)

        def find(pos: int):
            m = search(pos)
            return m.span() if m else None
        return find
    return factory


def prefix_run_finder(prefix: "re.Pattern[str]", cls_star: "re.Pattern[str]", min_len: int):
    """`PREFIX[cls]{min_len,}` (nothing after the run): the match ends at the end of the class run."""
    def factory(text: str):
        search, run = _MemoSearch(text, prefix), _RunEnd(text, cls_star)

        def find(pos: int):
            while True:
                m = search(pos)
                if m is None:
                    return None
                q = m.end()
                end = run(q)
                if end - q >= min_len:
                    return m.start(), end
                pos = m.start() + 1
        return find
    return factory


def run_tail_finder(prefix: "re.Pattern[str]", cls_star: "re.Pattern[str]", min_len: int, tail: "re.Pattern[str]"):
    """`PREFIX[cls]{min_len,}TAIL` where TAIL starts with a character outside `cls`: the greedy run can only end at
    the end of the class run, so every start inside one run shares one TAIL test; the leftmost start has the longest
    run. One TAIL test per run instead of one per start."""
    def factory(text: str):
        search, run = _MemoSearch(text, prefix), _RunEnd(text, cls_star)
        memo: Dict[int, Optional[int]] = {}

        def tail_end(b: int) -> Optional[int]:
            if b not in memo:
                memo.clear()
                m = tail.match(text, b)
                memo[b] = m.end() if m else None
            return memo[b]

        def find(pos: int):
            while True:
                m = search(pos)
                if m is None:
                    return None
                q = m.end()
                b = run(q)
                if b - q >= min_len:
                    t = tail_end(b)
                    if t is not None:
                        return m.start(), t
                pos = max(b, m.start() + 1)  # every later start in this run fails the same way
        return find
    return factory


class LinearAlternation(_LinearPattern):
    """Matches of `alt_1|alt_2|...` with `re` semantics (leftmost start wins; at one start the earlier alternative
    wins), one finder per alternative (or per group of alternatives that are safe together). Finders return their own
    leftmost `(start, end)` at or after `pos` and are called with non-decreasing `pos`."""

    def __init__(self, pattern: str, finders: List[Any], flags: int = 0) -> None:
        self.pattern, self.flags, self._factories = pattern, flags, list(finders)

    def _spans(self, text: str, pos: int = 0):
        finders = [f(text) for f in self._factories]
        cache: List[Any] = [None] * len(finders)
        n = len(text)
        while pos <= n:
            best = None
            for i, find in enumerate(finders):
                c = cache[i]
                if c is None or (c is not False and c[0] < pos):
                    c = cache[i] = find(pos) or False
                if c is not False and (best is None or c[0] < best[0]):
                    best = c
            if best is None:
                return
            yield best
            pos = best[1] if best[1] > best[0] else best[1] + 1


# Strip HTML comments `<!-- ... -->` (lazy, across lines) in linear time.
HTML_COMMENT_RE = LazyBlockPattern(r"<!--[\s\S]*?-->", re.compile(r"<!--"), re.compile(r"-->"), "-->")

# Matches markdown links `[label](path/to/file.ext)`
_MD_LINK_FILE_RX = re.compile(
    rf"\[[^\]]+\]\((?P<path>(?!https?://|mailto:|file://|#)(?:\./)?(?:(?:[A-Za-z0-9_.-]+/)*(?:Makefile|makefile|GNUmakefile)|[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.(?:{EXT_PATTERN})(?![A-Za-z0-9_])))(?:#(?P<anchor>[A-Za-z0-9_.-]+))?\)"
)


def _bracket_candidates(text: str, pos: int = 0):
    """Start positions where `\\[[^\\]]+\\]` can begin a match, one per `]`: the leftmost `[` after the previous `]`
    with at least one character before the `]`. Every other `[` before that `]` has the same continuation, so it
    matches iff this one does (the regex would retry each of them against the same text)."""
    seg = pos
    n = len(text)
    while seg < n:
        r = text.find("]", seg)
        if r < 0:
            return
        i = text.find("[", seg, r - 1) if r - 1 > seg else -1
        if i >= 0:
            nxt = yield i, r
            if nxt is not None:  # caller consumed a match ending at nxt
                seg = nxt
                continue
        seg = r + 1


class _MdLinkPattern(_LinearPattern):
    pattern = _MD_LINK_FILE_RX.pattern
    groupindex = _MD_LINK_FILE_RX.groupindex

    def finditer(self, text: str, pos: int = 0):
        gen = _bracket_candidates(text, pos)
        try:
            i, _r = next(gen)
            while True:
                m = _MD_LINK_FILE_RX.match(text, i)
                if m:
                    yield m
                    i, _r = gen.send(m.end())
                else:
                    i, _r = next(gen)
        except StopIteration:
            return

    def _spans(self, text: str, pos: int = 0):
        for m in self.finditer(text, pos):
            yield m.span()

    def match(self, text: str, pos: int = 0):
        return _MD_LINK_FILE_RX.match(text, pos)


MD_LINK_FILE_RE = _MdLinkPattern()

_REF_LINK_KEY_RX = re.compile(r"(\[[^\]]+\])\[[^\]]+\]")


def strip_ref_link_keys(line: str) -> str:
    """`re.sub(r"(\\[[^\\]]+\\])\\[[^\\]]+\\]", r"\\1", line)` in linear time: drop the `[ref]` of `[label][ref]`."""
    if "][" not in line:
        return line
    out, last = [], 0
    gen = _bracket_candidates(line)
    try:
        i, r = next(gen)
        while True:
            m = _REF_LINK_KEY_RX.match(line, i) if line.startswith("[", r + 1) else None
            if m:
                out += [line[last:i], m.group(1)]
                last = m.end()
                i, r = gen.send(m.end())
            else:
                i, r = next(gen)
    except StopIteration:
        pass
    out.append(line[last:])
    return "".join(out)

# Matches unbackticked repo paths that contain at least one directory separator `dir/file.ext`
SLASH_PATH_RE = re.compile(
    rf"(?<![A-Za-z0-9_/:.-])(?P<path>(?:[A-Za-z0-9_.-]+/)+(?:Makefile|makefile|GNUmakefile|[A-Za-z0-9_.-]+\.(?:{EXT_PATTERN})(?![A-Za-z0-9_])))(?:::([A-Za-z_][A-Za-z0-9_.-]*))?"
)

# Matches npm / pnpm / yarn / bun script invocations
# `npm run ${{ matrix.script }}` and similar runtime-interpolated commands (GitHub Actions expressions)
DYNAMIC_COMMAND_RE = re.compile(r"(?:npm|pnpm|yarn|bun)\s+run\s+\$\{\{[^}]*\}\}|\bmake\s+\$\{\{[^}]*\}\}")

NPM_SCRIPT_RE = re.compile(
    r"(?:`|^|\s)(?:npm|pnpm|yarn|bun)\s+run\s+(?P<script>[A-Za-z0-9_:.-]+)"
)
YARN_SHORT_SCRIPT_RE = re.compile(
    r"`(?:yarn|pnpm)\s+(?P<script>(?!install\b|add\b|remove\b|dlx\b|exec\b|init\b|why\b|config\b|run\b|tsx\b|--|-)[A-Za-z0-9_:.-]+)`"
    r"|^\s*(?:\$\s*)?(?:yarn|pnpm)\s+(?P<script2>(?!install\b|add\b|remove\b|dlx\b|exec\b|init\b|why\b|config\b|run\b|tsx\b|create\b|up\b|update\b|link\b|unlink\b|list\b|outdated\b|audit\b|licenses\b|pack\b|publish\b|rebuild\b|store\b|env\b|patch\b|dedupe\b|fetch\b|import\b|approve-builds\b|--|-)[A-Za-z0-9_:.-]+)\s*$"
)

# Matches `make <target>` inside backticks or explicit shell command lines
MAKE_TARGET_RE = re.compile(
    r"`make\s+(?P<target>(?!-)[A-Za-z0-9_.-]+)`|^\s*(?:\$\s*)?make\s+(?P<target2>(?!-)[A-Za-z0-9_.-]+)\s*$"
)

MAKE_PROSE_STOPWORDS = {
    "a",
    "an",
    "the",
    "sure",
    "it",
    "this",
    "that",
    "existing",
    "new",
    "changes",
    "change",
    "use",
    "sense",
    "up",
    "out",
    "for",
    "from",
    "with",
    "into",
    "all",
    "any",
    "some",
    "more",
    "less",
    "your",
    "our",
    "their",
    "them",
}

# Matches inline function calls like `charge_customer(a, b)` or `chargeCustomer(u1, 100, { currency: 'USD' })`
INLINE_CALL_RE = re.compile(
    r"\b(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<args>[^()]*)\)"
)

# False-positive path tokens to ignore (version numbers, common tech nouns with dots, template placeholders)
IGNORED_FILE_TOKENS = {
    "node.js",
    "next.js",
    "vue.js",
    "react.js",
    "three.js",
    "chart.js",
    "d3.js",
    "express.js",
    "nest.js",
    "nuxt.js",
    "vite.js",
    "rollup.js",
    "webpack.js",
    "references/provenance.json",
    "claude.md",
    "agents.md",
    "skill.md",
    "design.md",
    "design-tokens.css",
    "memlet-check.sarif",
    "package.json",
    "makefile",
    "pyproject.toml",
    "kebab-case.ts",
    "kebab-case.tsx",
    "kebab-case.test.ts",
    "kebab-case.test-d.ts",
    "usepagename.ts",
    "usecomponentname.ts",
    "componentname/componentname.tsx",
}

PYTHON_BUILTINS_AND_COMMON = {
    "print",
    "len",
    "range",
    "str",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "set",
    "tuple",
    "min",
    "max",
    "sum",
    "round",
    "sorted",
    "enumerate",
    "zip",
    "isinstance",
    "getattr",
    "setattr",
    "hasattr",
    "open",
    "Path",
    "json",
    "if",
    "for",
    "while",
    "return",
    "assert",
    "require",
    "import",
}


MAX_AUDIT_DOC_BYTES = 2 * 1024 * 1024     # one instruction document; a larger one is skipped
MAX_AUDIT_TOTAL_BYTES = 32 * 1024 * 1024  # all documents of one audit together


def _read_doc_text(path: Path) -> Optional[str]:
    """A regular, non-symlinked document of at most MAX_AUDIT_DOC_BYTES, else None (never blocks on a FIFO)."""
    from .memlet_wiki import read_regular_text
    return read_regular_text(path, MAX_AUDIT_DOC_BYTES, errors="replace")


def _discover_instruction_docs(repo_root: Path) -> List[Path]:
    """Find instruction, rule, runbook, and skill markdown files in repo_root."""
    found: List[Path] = []
    seen: Set[str] = set()
    from .memlet_wiki import contained_path_problem  # the one in-repository containment rule (no symlink component)

    def _add(p: Path) -> None:
        try:
            rel = p.relative_to(repo_root).as_posix()
            if contained_path_problem(repo_root, rel) is not None or not p.is_file():
                return
            resolved = str(p.resolve())
            if resolved not in seen:
                seen.add(resolved)
                found.append(p)
        except (OSError, ValueError):
            pass

    top_Names = (
        "CLAUDE.md",
        "AGENTS.md",
        ".cursorrules",
        "README.md",
        "CONTRIBUTING.md",
        "RUNBOOK.md",
        ".github/copilot-instructions.md",
    )
    for rel in top_Names:
        _add(repo_root / rel)

    for glob_pat in (
        ".cursor/rules/*.md",
        ".cursor/rules/*.mdc",
        ".github/instructions/*.md",
        "docs/**/*.md",
        "runbooks/**/*.md",
        "memory_skills/**/SKILL.md",
    ):
        for p in sorted(repo_root.glob(glob_pat)):
            if any(part in EXCLUDED_DIRS for part in p.relative_to(repo_root).parts):
                continue
            _add(p)

    # Also check nested CLAUDE.md / AGENTS.md up to depth 3
    for p in sorted(repo_root.rglob("*.md")):
        try:
            rel_parts = p.relative_to(repo_root).parts
        except ValueError:
            continue
        if any(part in EXCLUDED_DIRS for part in rel_parts):
            continue
        if len(rel_parts) <= 4 and p.name in ("CLAUDE.md", "AGENTS.md", "SKILL.md"):
            _add(p)

    return found


def _parse_call_arguments(arg_str: str) -> Tuple[int, List[str], bool]:
    """Parse an inline call argument string `a, b, key=val` into (pos_count, kw_names, is_definition_like).

    If the call looks like a type signature definition `a: int, b: str = 1` rather than an invocation
    or formal parameter list, we still extract parameter count accurately.
    """
    raw = arg_str.strip()
    if not raw:
        return 0, [], False
    # Split top-level commas respecting brackets/braces/quotes
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    in_quote: Optional[str] = None
    for ch in raw:
        if in_quote:
            current.append(ch)
            if ch == in_quote:
                in_quote = None
        elif ch in ("'", '"'):
            in_quote = ch
            current.append(ch)
        elif ch in ("(", "[", "{", "<"):
            depth += 1
            current.append(ch)
        elif ch in (")", "]", "}", ">"):
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())

    pos_count = 0
    kw_names: List[str] = []
    is_def_like = False
    for p in parts:
        if not p or p in ("self", "cls"):
            continue
        if p.startswith("*") or p.startswith("..."):
            is_def_like = True
            continue
        # Check for Python/TS type annotation `param: Type` vs keyword arg `key=val`
        if ":" in p and ("=" not in p or p.index(":") < p.index("=")):
            is_def_like = True
            pos_count += 1
            continue
        if "=" in p and not p.startswith(("=", "!")):
            k = p.split("=", 1)[0].strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                kw_names.append(k)
                continue
        pos_count += 1
    return pos_count, kw_names, is_def_like


def _check_inline_call_vs_signature(
    sym_name: str,
    pos_count: int,
    kw_names: List[str],
    is_def_like: bool,
    sig: Dict[str, Any],
) -> Optional[str]:
    """Compare an inline call `sym(a, b, k=1)` in documentation against the current code signature `sig`."""
    kind = sig.get("kind", "function")
    if kind not in ("function", "async_function"):
        return None
    req_args = list(sig.get("reqArgs") or [])
    all_args = list(sig.get("allArgs") if sig.get("allArgs") is not None else req_args)
    min_pos = len(req_args)
    max_pos = len(all_args)
    has_varargs = bool(sig.get("hasVararg", sig.get("has_varargs", False)))
    has_varkw = bool(sig.get("hasKwarg", sig.get("has_varkw", False)))
    pos_names = all_args
    kwonly_req = set(sig.get("kwonlyReq") or sig.get("kwonly_req") or [])
    kwonly_all = set(sig.get("kwonlyAll") or sig.get("kwonly_opt") or [])

    if is_def_like:
        total_documented = pos_count + len(kw_names)
        if total_documented < min_pos + len(kwonly_req):
            return (
                f"Documented signature `{sym_name}` has {total_documented} arguments; the current code requires "
                f"{min_pos} positional arguments ({req_args}) and keyword arguments {sorted(kwonly_req)}."
            )
        return None

    satisfied_pos_by_kw = sum(1 for k in kw_names if k in pos_names)
    effective_pos = pos_count + satisfied_pos_by_kw
    if pos_count == 0 and not kw_names:
        # Empty `func()` in documentation prose is standard shorthand for naming a function
        return None
    if effective_pos < min_pos:
        return (
            f"Call to `{sym_name}` supplies {effective_pos} positional arguments, fewer than the required {min_pos}: {req_args}."
        )
    if not has_varargs and pos_count > max_pos:
        return (
            f"Call to `{sym_name}` supplies {pos_count} positional arguments, exceeding the maximum {max_pos}: {all_args}."
        )

    missing_req_kw = [k for k in sorted(kwonly_req) if k not in kw_names]
    if missing_req_kw:
        return (
            f"Call to `{sym_name}` is missing required keyword-only arguments: {missing_req_kw}."
        )

    if kw_names and not has_varkw and pos_names:
        allowed_kw = set(pos_names) | kwonly_req | kwonly_all
        unknown_kw = [k for k in kw_names if k not in allowed_kw]
        if unknown_kw:
            return (
                f"Call to `{sym_name}` uses unknown keyword arguments: {unknown_kw}."
            )
    return None


def _resolve_git_commit(repo_root: Path, ref: str) -> str:
    """Resolve non-option commit refs; diff/show never receive caller refs.

    Old Git without rev-parse --end-of-options uses --verify alone, safely:
    the nonempty ref has already been checked for a leading dash and separators.
    """
    if (not isinstance(ref, str) or not ref or ref.startswith("-") or
            any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in ref)):
        raise ValueError("invalid Git ref: option-like, empty, or contains whitespace/control characters")
    for separator in (["--end-of-options"], []):
        proc = subprocess.run(
            ["git", "rev-parse", "--verify"] + separator + [ref + "^{commit}"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=3.0,
        )
        oid = proc.stdout.strip()
        if proc.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", oid):
            return oid.lower()
    raise ValueError("cannot resolve Git commit ref: " + ref)


def _git_show_text(repo_root: Path, ref: str, rel_path: str) -> Optional[str]:
    try:
        p_show = subprocess.run(
            ["git", "show", "--no-ext-diff", "--no-textconv", f"{ref}:{rel_path}", "--"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if p_show.returncode != 0:
        return None
    return p_show.stdout


def _signatures_from_text(content: str, file_name: str) -> Tuple[Dict[str, Any], Dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="memlet-ref-") as td:
        tmp_path = Path(td) / file_name
        tmp_path.write_text(content, encoding="utf-8")
        sigs = extract_file_symbol_signatures(tmp_path).get("symbols") or {}
        hashes = extract_file_symbol_hashes(tmp_path)
    return sigs, hashes


def _compute_git_diff_changes(
    repo_root: Path,
    base_ref: str,
    head_ref: str = "HEAD",
) -> Dict[str, Any]:
    """Inspect git diff between base_ref and head_ref and compute file & symbol contract deltas.

    Both sides are read from git objects (`git show <ref>:<path>`), so the result is anchored to the
    requested commits even when the working tree differs. Pass `head_ref="WORKTREE"` to compare the
    base commit against the current working tree on disk instead.
    """
    use_worktree = isinstance(head_ref, str) and head_ref.upper() == "WORKTREE"
    diff_info: Dict[str, Any] = {
        "baseRef": base_ref,
        "headRef": head_ref,
        "headSource": "worktree" if use_worktree else "git_object",
        "changedFiles": {},
        "worktreeDiffersFromHead": [],
    }
    try:
        base_oid = _resolve_git_commit(repo_root, base_ref)
        head_oid = None if use_worktree else _resolve_git_commit(repo_root, head_ref)
        diff_cmd = ["git", "diff", "--no-ext-diff", "--no-textconv", "--name-status", base_oid]
        if head_oid is not None:
            diff_cmd.append(head_oid)
        diff_cmd.append("--")
        proc = subprocess.run(
            diff_cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if proc.returncode != 0:
            diff_info["error"] = (proc.stderr or proc.stdout or "").strip()
            return diff_info
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        diff_info["error"] = str(exc)
        return diff_info

    for raw_line in (proc.stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0].strip()
        rel_path = parts[-1].strip()
        old_rel_path = parts[1].strip() if len(parts) > 2 else rel_path

        file_delta: Dict[str, Any] = {
            "status": status,
            "path": rel_path,
            "deletedSymbols": [],
            "breakingSymbols": {},
            "compatibleSymbols": [],
        }

        # Load base file content via `git show <base_ref>:<old_rel_path>`
        base_content: Optional[str] = None
        if not status.startswith("A"):
            base_content = _git_show_text(repo_root, base_oid, old_rel_path)

        head_path = repo_root / rel_path
        if status.startswith("D"):
            diff_info["changedFiles"][rel_path] = file_delta
            continue

        # Load head file content from the requested commit (or the working tree for WORKTREE)
        head_content: Optional[str]
        if use_worktree:
            try:
                head_content = head_path.read_text(encoding="utf-8", errors="ignore") if head_path.is_file() else None
            except OSError:
                head_content = None
        else:
            head_content = _git_show_text(repo_root, head_oid, rel_path)
            try:
                disk_text = head_path.read_text(encoding="utf-8", errors="ignore") if head_path.is_file() else None
            except OSError:
                disk_text = None
            if head_content is not None and disk_text != head_content:
                diff_info["worktreeDiffersFromHead"].append(rel_path)
        if head_content is None:
            diff_info["changedFiles"][rel_path] = file_delta
            continue

        if base_content is not None:
            base_sigs, base_hashes = _signatures_from_text(base_content, Path(old_rel_path).name)
            curr_sigs, curr_hashes = _signatures_from_text(head_content, head_path.name)

            for sym, old_sig in base_sigs.items():
                if sym not in curr_sigs:
                    file_delta["deletedSymbols"].append(sym)
                elif base_hashes.get(sym) != curr_hashes.get(sym):
                    is_brk, reason = is_breaking_symbol_change(old_sig, curr_sigs[sym])
                    if is_brk:
                        file_delta["breakingSymbols"][sym] = reason
                    else:
                        file_delta["compatibleSymbols"].append(sym)

        diff_info["changedFiles"][rel_path] = file_delta
    return diff_info


_COMPOUND_SUFFIXES = (".gz", ".bz2", ".xz", ".zst", ".zip", ".tar", ".tgz", ".br")
_RUNTIME_ROOTS = {"state", "data", "outputs", "output", "out", "cache", ".cache", "tmp", "logs", "log", "runs",
                  "artifacts", "work", "receipts", "telemetry", "var", "test-results", "ci-shards", "coverage", "playwright-report",
                  "results", "rehearsal_logs"}
_INSTRUCTION_NAMES = {"agents.md", "claude.md", "gemini.md", "skill.md", ".cursorrules", "copilot-instructions.md"}
# A reference is only treated as intentional when the marker is attached to *that* occurrence.
_ATTACHED_BEFORE_RE = re.compile(
    r"(?:--out(?:put)?(?:-dir|-file)?[=\s]+|(?:^|\s)-o\s+|>\s*|→\s*|->\s*|\b(?:writes?|saves?|stores?|emits?|dumps?)\b[^`\x22\x27\n]{0,30}?\bto\s+|\bwrites?\s+|\bsaved?\s+(?:to|as)\s+|"
    r"\b(?:output|outputs|produces|generates)\s*:?\s*|\be\.g\.,?\s*|\bfor\s+example,?\s*|예\s*:\s*|예시\s*:?\s*|출력\s*:\s*|생성\s*:\s*)"
    r"[`\"'(\[]*$",
    re.IGNORECASE,
)
_ATTACHED_AFTER_RE = re.compile(r"^[`\"')\]]*\s*(?:에|로|으로)\s*(?:저장|기록|작성|생성|출력)", re.IGNORECASE)
_REQUIRED_NEAR_RE = re.compile(r"\b(?:required|must|read|load|requires?|before)\b|필수|반드시|읽(?:어|고|은|기)|먼저", re.IGNORECASE)
_HISTORICAL_SEGMENTS = {"archive", "archives", "snapshot", "snapshots", "context", "history", "postmortems"}
_HISTORICAL_NAME_RE = re.compile(r"(?:REPORT|AUDIT|RETRO|POSTMORTEM|REVIEW|ACCEPTANCE|EVIDENCE|PREREG)[^/]*\.md$", re.IGNORECASE)
_DATED_NAME_RE = re.compile(r"(?:19|20)\d{2}-?[01]\d-?[0-3]\d")  # dated review/report snapshots, e.g. *_20261016.md
_FOREIGN_SEGMENTS = {"bug-reproductions", "reproductions", "upstream", "vendor", "third_party", "third-party"}
_RUNTIME_DATA_EXTS = (".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".log", ".sqlite", ".sqlite3", ".db", ".parquet")


def _occurrences(line: str, clean_p: str):
    """Yield (before, after) context for every standalone-looking occurrence of `clean_p` in `line`."""
    for m in re.finditer(re.escape(clean_p), line):
        yield line[max(0, m.start() - 60):m.start()], line[m.end():m.end() + 30]


def _occurrence_is_not_a_repo_path(before: str, after: str) -> Optional[str]:
    if re.search(r"(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|~)/?$", before) or re.search(r"\$\{?$", before):
        return "shell_variable_or_home_expansion"
    if re.search(r"file://[^\s)`'\"]*$", before):
        return "file_uri_external"
    if any(after.startswith(sfx) for sfx in _COMPOUND_SUFFIXES):
        return "compound_suffix_phantom"
    return None


def _classify_unresolved_reference(line: str, clean_p: str, doc_rel: str, root: Path, doc_dir: Path) -> Optional[Tuple[str, str]]:
    """Explain why an unresolved reference is not a confirmed broken path (conservative, per occurrence).

    None keeps the `missing_path` error. ("skip", why) only when *every* occurrence of the path on the line is part
    of a shell variable, a file:// URI or a longer compressed file name. ("warning", why) only for an occurrence with
    an attached example/output marker, a template placeholder, or a non-instruction historical document — and never
    when the same line says the file is required / must be read."""
    occ = list(_occurrences(line, clean_p))
    if occ:
        reasons = [_occurrence_is_not_a_repo_path(b, a) for b, a in occ]
        if all(reasons):
            return ("skip", reasons[0])
    if re.search(r"YYYY|MM-DD|<[^>]+>|\*|\{[^}]*\}", clean_p):
        return ("skip", "template_placeholder")
    required = bool(_REQUIRED_NEAR_RE.search(line))
    if required:
        return None
    if any(_ATTACHED_BEFORE_RE.search(b) or _ATTACHED_AFTER_RE.search(a) for b, a in occ):
        if clean_p.split("/", 1)[0].lower() in _RUNTIME_ROOTS:
            return ("warning", "generated_output_path")
        return ("warning", "example_or_planned_output")
    if re.search(r"(?:\bDrive\b|드라이브|Google\s*Drive|iCloud)\s*`?" + re.escape(clean_p), line, re.IGNORECASE):
        return ("warning", "external_drive_reference")
    doc_name = doc_rel.rsplit("/", 1)[-1].lower()
    if doc_name not in _INSTRUCTION_NAMES:
        # data files under a runtime root (state/…/status.json) are written by the program at run time
        if clean_p.split("/", 1)[0].lower() in _RUNTIME_ROOTS and clean_p.lower().endswith(_RUNTIME_DATA_EXTS):
            return ("warning", "runtime_data_file")
        segs = {s.lower() for s in doc_rel.split("/")[:-1]}
        if segs & _FOREIGN_SEGMENTS:
            return ("warning", "foreign_codebase_reference")
        if segs & _HISTORICAL_SEGMENTS or _HISTORICAL_NAME_RE.search(doc_rel) or _DATED_NAME_RE.search(doc_name):
            return ("warning", "historical_document_reference")
    return None


def _node_accepts_syntax(path: Path) -> Optional[bool]:
    """`node --check` verdict for JS files when Node is installed; None when it cannot be asked."""
    if path.suffix.lower() not in (".js", ".mjs", ".cjs"):
        return None
    node = shutil.which("node")
    if not node:
        return None
    try:
        return subprocess.run([node, "--check", str(path)], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def audit_repository(
    repo_path: str | Path,
    *,
    base_ref: Optional[str] = None,
    head_ref: str = "HEAD",
    output_format: str = "json",
) -> Dict[str, Any]:
    """Zero-setup repository instruction & skill procedure audit (`memlet audit`)."""
    t0 = time.perf_counter()
    root = Path(repo_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Repository path to audit not found: {root}")

    docs = _discover_instruction_docs(root)
    sig_cache: Dict[str, Dict[str, Any]] = {}
    hash_cache: Dict[str, Dict[str, str]] = {}

    def _get_sigs(p: Path) -> Dict[str, Any]:
        key = str(p.resolve())
        if key not in sig_cache:
            sig_cache[key] = extract_file_symbol_signatures(p)
        return sig_cache[key]

    def _get_hashes(p: Path) -> Dict[str, str]:
        key = str(p.resolve())
        if key not in hash_cache:
            hash_cache[key] = extract_file_symbol_hashes(p)
        return hash_cache[key]

    # Pre-load root package.json scripts and Makefile targets if present
    pkg_json_path = root / "package.json"
    pkg_scripts: Optional[Dict[str, Any]] = None
    if pkg_json_path.is_file():
        pkg_scripts = _get_sigs(pkg_json_path).get("symbols") or {}

    # Pre-load workspace package.json scripts (packages/*, apps/*, examples/*)
    workspace_pkg_scripts: Dict[str, Dict[str, Any]] = {}
    for ws_glob in ("packages/*/package.json", "apps/*/package.json", "examples/*/package.json"):
        for wp in sorted(root.glob(ws_glob)):
            if wp.is_file():
                rel_dir = str(wp.parent.relative_to(root)).replace("\\", "/")
                workspace_pkg_scripts[rel_dir] = _get_sigs(wp).get("symbols") or {}

    makefile_path = root / "Makefile"
    make_targets: Optional[Dict[str, Any]] = None
    if makefile_path.is_file():
        make_targets = _get_sigs(makefile_path).get("symbols") or {}

    # Pre-index repo files by basename and relative path suffix for monorepo/subpackage resolution
    by_basename: Dict[str, List[Path]] = {}
    all_rel_paths: List[Tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in EXCLUDED_DIRS and not d.startswith(".git") and not d.startswith(".venv")
        ]
        for fn in filenames:
            p_full = Path(dirpath) / fn
            try:
                rel_s = str(p_full.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            by_basename.setdefault(fn, []).append(p_full)
            all_rel_paths.append((rel_s, p_full))

    git_diff_data: Optional[Dict[str, Any]] = None
    if base_ref is not None:
        git_diff_data = _compute_git_diff_changes(root, base_ref=base_ref, head_ref=head_ref)
    elif head_ref != "HEAD" and head_ref != "WORKTREE":
        # No-Git zero-setup auditing remains available when refs are omitted.
        try:
            _resolve_git_commit(root, head_ref)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            git_diff_data = {"baseRef": base_ref, "headRef": head_ref,
                             "changedFiles": {}, "error": str(exc)}

    findings: List[Dict[str, Any]] = []
    seen_finding_keys: Set[Tuple[Any, ...]] = set()
    blast_radius: List[Dict[str, Any]] = []
    total_references_checked = 0
    commands_checked = 0
    dynamic_unverified_commands: List[Dict[str, Any]] = []

    def _add_finding(
        *,
        rule_id: str,
        severity: str,
        doc_rel: str,
        line_no: int,
        target_path: str,
        symbol: Optional[str],
        message: str,
        remediation: str,
    ) -> None:
        key = (rule_id, doc_rel, line_no, target_path, symbol or "")
        if key in seen_finding_keys:
            return
        seen_finding_keys.add(key)
        findings.append({
            "ruleId": rule_id,
            "severity": severity,
            "docPath": doc_rel,
            "line": line_no,
            "targetPath": target_path,
            "symbol": symbol,
            "message": message,
            "remediation": remediation,
        })

    if git_diff_data and git_diff_data.get("error"):
        _add_finding(
            rule_id="invalid_git_ref",
            severity="error",
            doc_rel="(git-diff)",
            line_no=1,
            target_path=str(base_ref or ""),
            symbol=str(head_ref or "HEAD"),
            message=f"Cannot resolve Git diff references (`{base_ref}..{head_ref}`): {git_diff_data['error']}",
            remediation="Specify existing Git branches, tags, or commit SHAs with `--base` and `--head`.",
        )

    cd_cmd_re = re.compile(r"(?:^|[`;&|]\s*|\$\s*)cd\s+([A-Za-z0-9_./-]+)")
    pkg_dir_hint_re = re.compile(
        r"(?:inside\s+(?:the\s+)?package\s+(?:directory|folder)|in\s+(?:the\s+)?package\s+(?:directory|folder)|in\s+each\s+package|package\s+(?:directory|folder)|packages/[A-Za-z0-9_.-]+|examples/[A-Za-z0-9_.-]+|apps/[A-Za-z0-9_.-]+)",
        re.IGNORECASE,
    )
    tutorial_create_re = re.compile(
        r"(?:create\s+(?:a\s+|an\s+)?(?:new\s+)?(?:[A-Za-z0-9_+-]+\s+)?(?:file|server|script|block|module|component)|"
        r"save\s+(?:it\s+|new\s+cases\s+)?(?:as|under)|"
        r"for\s+example,\s+`[^`]+`\s+generates\s+`|"
        r"touch\s+|cat\s*<<|파일을\s*생성|새로\s*만들)",
        re.IGNORECASE,
    )
    prohibitive_line_re = re.compile(
        r"(?:\b(?:do\s+not|don't|never|avoid)\s+(?:create|use|add|place|put|import|reference|write|commit)\b|"
        r"\b(?:bad\s+example|anti-pattern|instead\s+of\s+creating)\b)",
        re.IGNORECASE,
    )
    external_repo_re = re.compile(
        r"(?:\bin\s+the\s+(?:public\s+|private\s+)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\s+repo\b|"
        r"\bnot\s+here\s*:|\bin\s+another\s+repo(?:sitory)?\b)",
        re.IGNORECASE,
    )
    ref_link_def_re = re.compile(r"^\s*\[[^\]]+\]:\s*https?://", re.IGNORECASE)
    code_fence_title_re = re.compile(r"^\s*```[A-Za-z0-9_-]*\s+title=", re.IGNORECASE)
    run_bare_re = re.compile(
        r"(?:^\s*(?:\$\s*)?(?:run|execute|python3?|node|tsx|ts-node|bash|sh|zsh|pytest|ruby|go\s+run)\b|"
        r"\b(?:run|execute)\s+`[^`]+`|\b(?:python3?|node|tsx|ts-node|bash|sh|pytest)\s+[A-Za-z0-9_.-]+)",
        re.IGNORECASE,
    )
    root_or_other_ctx_re = re.compile(
        r"(?:\bin\s+the\s+root\b|\broot\s+folder\b|\brepo(?:sitory)?\s+root\b|"
        r"\bother\s+(?:\w+\s+)?examples?\b|\banother\s+example\b)",
        re.IGNORECASE,
    )

    doc_bytes_left = MAX_AUDIT_TOTAL_BYTES
    for doc_path in docs:
        try:
            doc_rel = str(doc_path.relative_to(root))
            raw_doc_text = _read_doc_text(doc_path)
            if raw_doc_text is None:  # not a regular file, unreadable or over the per-document bound
                continue
            doc_bytes_left -= len(raw_doc_text)
            if doc_bytes_left < 0:  # cumulative bound: one audit reads at most MAX_AUDIT_TOTAL_BYTES of documents
                break
            # Strip HTML comments <!-- ... --> while preserving exact line numbers (linear in the text)
            clean_doc_text = HTML_COMMENT_RE.sub(
                lambda m: "\n" * m.group(0).count("\n"),
                raw_doc_text,
            )
            lines = clean_doc_text.splitlines()
        except OSError:
            continue

        active_cwd: Path = doc_path.parent
        explicit_cd: bool = False
        in_fence: bool = False
        package_context_active: bool = False
        tutorial_created_files: Set[str] = set()
        recent_tutorial_lines: int = 0

        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped:
                continue

            if recent_tutorial_lines > 0:
                recent_tutorial_lines -= 1

            if stripped.startswith("```"):
                if in_fence:
                    in_fence = False
                    active_cwd = doc_path.parent
                    explicit_cd = False
                else:
                    in_fence = True

            # Reset active_cwd on Markdown headings or when explicitly switching back to root/other example
            if stripped.startswith("#") or root_or_other_ctx_re.search(line):
                active_cwd = doc_path.parent
                explicit_cd = False
                if stripped.startswith("#"):
                    package_context_active = bool(pkg_dir_hint_re.search(stripped))
                else:
                    package_context_active = True

            if pkg_dir_hint_re.search(line):
                package_context_active = True

            # Track `cd <subdir>` changes within code blocks or command instructions
            for m_cd in cd_cmd_re.finditer(line):
                cd_arg = m_cd.group(1).strip().strip("`'\"")
                if not cd_arg:
                    continue
                for base_cand in (active_cwd, root, doc_path.parent):
                    cand_dir = (base_cand / cd_arg).resolve()
                    if cand_dir.is_dir() and cand_dir.is_relative_to(root):
                        active_cwd = cand_dir
                        explicit_cd = active_cwd.resolve() != doc_path.parent.resolve()
                        break

            # 0. Dynamic commands (`npm run ${{ matrix.script }}`) cannot be resolved statically: report them as unverified.
            for m_dyn in DYNAMIC_COMMAND_RE.finditer(line):
                dynamic_unverified_commands.append({"doc": doc_rel, "line": idx, "command": m_dyn.group(0).strip()[:120]})
                _add_finding(
                    rule_id="dynamic_command_unverified",
                    severity="warning",
                    doc_rel=doc_rel,
                    line_no=idx,
                    target_path=doc_rel,
                    symbol=None,
                    message=f"`{doc_rel}:{idx}` uses runtime value `{m_dyn.group(0).strip()[:60]}` (`${{{{ }}}}`), which cannot be checked statically.",
                    remediation="List matrix values in the document or workflow, or name each script explicitly so it can be checked.",
                )

            # 1. Check npm / pnpm / yarn / bun script references against active_cwd or root package.json
            if pkg_scripts is not None or workspace_pkg_scripts:
                script_matches: List[str] = []
                for m_npm in NPM_SCRIPT_RE.finditer(line):
                    script_matches.append(m_npm.group("script"))
                for m_yarn in YARN_SHORT_SCRIPT_RE.finditer(line):
                    scr_val = m_yarn.group("script") or m_yarn.group("script2")
                    if scr_val:
                        script_matches.append(scr_val)
                for scr in script_matches:
                    total_references_checked += 1
                    commands_checked += 1
                    cwd_pkg = active_cwd / "package.json"
                    cwd_scripts = (_get_sigs(cwd_pkg).get("symbols") or {}) if cwd_pkg.is_file() else {}
                    found_in_cwd = scr in cwd_scripts
                    found_in_root = (not explicit_cd) and (pkg_scripts is not None and scr in pkg_scripts)
                    found_in_ws = False
                    if not explicit_cd and not found_in_cwd and not found_in_root and package_context_active and workspace_pkg_scripts:
                        found_in_ws = any(scr in ws_s for ws_s in workspace_pkg_scripts.values())
                    if not (found_in_cwd or found_in_root or found_in_ws):
                        target_pkg_rel = (
                            str(cwd_pkg.relative_to(root)).replace("\\", "/")
                            if cwd_pkg.is_file() and cwd_pkg.is_relative_to(root)
                            else "package.json"
                        )
                        avail_keys = sorted((cwd_scripts if explicit_cd else (cwd_scripts or pkg_scripts or {})).keys())[:8]
                        _add_finding(
                            rule_id="missing_script",
                            severity="error",
                            doc_rel=doc_rel,
                            line_no=idx,
                            target_path=target_pkg_rel,
                            symbol=scr,
                            message=f"Script `{scr}` referenced at `{doc_rel}:{idx}` is absent from `scripts` in `{target_pkg_rel}`.",
                            remediation=f"Update documented commands to match `scripts` in `{target_pkg_rel}`: {avail_keys}.",
                        )

            # 2. Check `make <target>` references against Makefile
            if make_targets is not None:
                for m_mk in MAKE_TARGET_RE.finditer(line):
                    tgt = m_mk.group("target") or m_mk.group("target2")
                    if not tgt or tgt.lower() in MAKE_PROSE_STOPWORDS:
                        continue
                    total_references_checked += 1
                    commands_checked += 1
                    if tgt not in make_targets:
                        _add_finding(
                            rule_id="missing_script",
                            severity="error",
                            doc_rel=doc_rel,
                            line_no=idx,
                            target_path="Makefile",
                            symbol=tgt,
                            message=f"Make target `{tgt}` referenced at `{doc_rel}:{idx}` is not defined in `Makefile`.",
                            remediation=f"Align documented commands with the targets in `Makefile`: {sorted(make_targets.keys())[:8]}.",
                        )

            # Skip file reference extraction on Markdown reference-link definitions, code fence title headers, prohibitive/anti-pattern lines, or explicit external-repo lines
            if (
                ref_link_def_re.match(line)
                or code_fence_title_re.match(line)
                or prohibitive_line_re.search(line)
                or external_repo_re.search(line)
            ):
                continue

            is_tutorial_create_line = bool(tutorial_create_re.search(line))
            if is_tutorial_create_line:
                recent_tutorial_lines = 4

            # Strip Markdown reference-style link keys `[label][ref_id]` so `ref_id` is not misparsed as a local path
            line_for_paths = strip_ref_link_keys(line)

            # 3. Extract file & explicit symbol references on this line
            line_refs: List[Tuple[str, Optional[str]]] = []
            for m_exp in iter_explicit_anchors(line_for_paths):
                sym = m_exp.group("symbol")
                if sym and re.match(r"^L\d+", sym):
                    sym = None
                line_refs.append((m_exp.group("path"), sym))
            for m_bt in BACKTICK_FILE_RE.finditer(line_for_paths):
                sym = m_bt.group("symbol")
                if sym and re.match(r"^L\d+", sym):
                    sym = None
                line_refs.append((m_bt.group("path"), sym))
            for m_md in MD_LINK_FILE_RE.finditer(line_for_paths):
                sym = m_md.group("anchor")
                if sym and re.match(r"^L\d+", sym):
                    sym = None
                line_refs.append((m_md.group("path"), sym))
            for m_sl in SLASH_PATH_RE.finditer(line_for_paths):
                line_refs.append((m_sl.group("path"), m_sl.group(2)))

            seen_line_paths: Set[Tuple[str, Optional[str]]] = set()
            resolved_line_files: List[Tuple[str, Path, Dict[str, Any]]] = []
            for raw_p, exp_sym in line_refs:
                clean_p = raw_p.strip()
                while clean_p.startswith("./"):
                    clean_p = clean_p[2:]
                clean_p = clean_p.lstrip("/")
                if not clean_p or (exp_sym is None and clean_p.lower() in IGNORED_FILE_TOKENS):
                    continue
                if is_tutorial_create_line:
                    tutorial_created_files.add(clean_p)
                    continue
                if clean_p in tutorial_created_files:
                    continue
                if clean_p.lower().endswith((".md", ".mdx", ".rst", ".txt", ".html", ".htm")):
                    # Markdown section heading anchors (#section-name) are not code symbol anchors
                    exp_sym = None
                if clean_p.startswith((
                    "http://",
                    "https://",
                    "github.com/",
                    "www.github.com/",
                    "gitlab.com/",
                    "raw.githubusercontent.com/",
                    "Users/",
                    "var/",
                    "tmp/",
                    "private/",
                    "node_modules/",
                    "dist/",
                    "build/",
                    ".cache/",
                    ".auth/",
                    "output/",
                    "work/",
                    "memories/",
                    "wt/",
                )):
                    continue
                if f"@{clean_p}" in line or re.match(r"^[A-Z][A-Z0-9_]{1,}/", clean_p):
                    continue
                first_seg = clean_p.split("/", 1)[0]
                if "/" in clean_p and ("." in first_seg or first_seg in ("React", "Vue", "Angular", "Svelte")):
                    continue
                lower_p = clean_p.lower()
                if (
                    "path/to/" in lower_p
                    or "/path/" in lower_p
                    or "my_provider/" in lower_p
                    or "my_block" in lower_p
                    or "/__generated__/" in lower_p
                    or lower_p.startswith("__generated__/")
                    or "2025-06-15-" in lower_p
                    or lower_p in ("src/config/database.ts", "src/db/client.ts")
                    or lower_p.endswith(("/foo.py", "/test_foo.py", "/bar.py", "/example.py", "/example.ts"))
                    or lower_p in ("foo.py", "bar.py", "test_foo.py", "example.py", "example.ts")
                ):
                    continue
                if (clean_p, exp_sym) in seen_line_paths:
                    continue
                seen_line_paths.add((clean_p, exp_sym))
                total_references_checked += 1

                # Resolve relative to active_cwd (from `cd <subdir>`), repo root, doc parent, .github/workflows, or monorepo subpaths
                candidate = active_cwd / clean_p
                if not explicit_cd:
                    if not candidate.is_file() and (root / clean_p).is_file():
                        candidate = root / clean_p
                    if not candidate.is_file() and (doc_path.parent / clean_p).is_file():
                        candidate = doc_path.parent / clean_p
                    if not candidate.is_file() and "/" not in clean_p and clean_p.endswith((".yml", ".yaml")):
                        wf_cand = root / ".github" / "workflows" / clean_p
                        if wf_cand.is_file():
                            candidate = wf_cand
                    if not candidate.is_file():
                        if "/" in clean_p:
                            suffix_match = "/" + clean_p.lstrip("/")
                            for rel_s, p_full in all_rel_paths:
                                if rel_s == clean_p or rel_s.endswith(suffix_match):
                                    candidate = p_full
                                    break
                        elif clean_p in by_basename and exp_sym is None:
                            candidate = by_basename[clean_p][0]
                else:
                    if not candidate.is_file() and (root / clean_p).is_file():
                        try:
                            rel_cwd = str(active_cwd.resolve().relative_to(root)).replace("\\", "/")
                            if clean_p.startswith(rel_cwd + "/"):
                                candidate = root / clean_p
                        except ValueError:
                            pass

                if not candidate.exists() or not candidate.is_file():
                    # Ignore bare filenames without directory prefix unless an explicit symbol anchor or execution verb on a code file is present
                    if exp_sym is None and "/" not in clean_p:
                        if raw_p.strip().startswith("./") or not clean_p.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")) or not run_bare_re.search(line):
                            continue
                    verdict = _classify_unresolved_reference(line, clean_p, doc_rel, root, doc_path.parent)
                    if verdict and verdict[0] == "skip":
                        continue
                    if verdict:
                        _add_finding(
                            rule_id="unverified_path",
                            severity="warning",
                            doc_rel=doc_rel,
                            line_no=idx,
                            target_path=clean_p,
                            symbol=exp_sym,
                            message=f"`{clean_p}` at `{doc_rel}:{idx}` is absent from the repository but may be intentional ({verdict[1]}).",
                            remediation="Keep examples, plans, generated outputs, and historical references; correct the path if it is a current instruction.",
                        )
                        continue
                    _add_finding(
                        rule_id="missing_path",
                        severity="error",
                        doc_rel=doc_rel,
                        line_no=idx,
                        target_path=clean_p,
                        symbol=exp_sym,
                        message=f"File `{clean_p}` referenced at `{doc_rel}:{idx}` was not found in the repository.",
                        remediation=f"Replace deleted or moved path `{clean_p}` with the current file path.",
                    )
                    continue

                try:
                    rel_target = (
                        str(candidate.resolve().relative_to(root))
                        if candidate.resolve().is_relative_to(root)
                        else str(candidate.relative_to(root))
                    )
                except ValueError:
                    rel_target = clean_p
                sig_info = _get_sigs(candidate)
                resolution = sig_info.get("__resolution__", "unsupported")
                syntax_ok = sig_info.get("__syntax_ok__", True)
                symbols_map = sig_info.get("symbols") or {}
                resolved_line_files.append((rel_target, candidate, sig_info))

                if resolution == "parse_error" or (not syntax_ok and resolution in ("exact", "contract")):
                    if exp_sym is None and _node_accepts_syntax(candidate):
                        _add_finding(
                            rule_id="parser_disagreement",
                            severity="warning",
                            doc_rel=doc_rel,
                            line_no=idx,
                            target_path=rel_target,
                            symbol=exp_sym,
                            message=f"`{rel_target}` passed the Node syntax check but could not be parsed by the Memlet lightweight parser.",
                            remediation="Review symbol anchors manually.",
                        )
                        continue
                    _add_finding(
                        rule_id="syntax_error",
                        severity="error",
                        doc_rel=doc_rel,
                        line_no=idx,
                        target_path=rel_target,
                        symbol=exp_sym,
                        message=f"Code file `{rel_target}` referenced at `{doc_rel}:{idx}` has a syntax/parse error.",
                        remediation=f"Fix parentheses, strings, or syntax errors in `{rel_target}` first.",
                    )
                    continue

                if exp_sym:
                    if resolution == "unsupported":
                        _add_finding(
                            rule_id="unsupported_anchor",
                            severity="warning",
                            doc_rel=doc_rel,
                            line_no=idx,
                            target_path=rel_target,
                            symbol=exp_sym,
                            message=f"Anchor `{rel_target}::{exp_sym}` at `{doc_rel}:{idx}` uses a language unsupported by the AST/contract parser (`{sig_info.get('__language__', 'unknown')}`).",
                            remediation="Files in this language require manual review (`unsupported_anchor`) when their hash changes.",
                        )
                    elif exp_sym not in symbols_map:
                        _add_finding(
                            rule_id="missing_symbol",
                            severity="error",
                            doc_rel=doc_rel,
                            line_no=idx,
                            target_path=rel_target,
                            symbol=exp_sym,
                            message=f"Symbol `{exp_sym}` referenced at `{doc_rel}:{idx}` does not exist in `{rel_target}`.",
                            remediation=f"Update the document to match current symbols in `{rel_target}`: {sorted(symbols_map.keys())[:8]}.",
                        )

            # 4. If at least one supported code file was referenced on this line, inspect inline `func(...)` calls on the same line!
            for rel_target, candidate, sig_info in resolved_line_files:
                resolution = sig_info.get("__resolution__", "unsupported")
                if resolution not in ("exact", "contract"):
                    continue
                symbols_map = sig_info.get("symbols") or {}
                target_stem = Path(rel_target).stem
                is_example_script = rel_target.startswith("examples/") or "/examples/" in rel_target
                for m_call in INLINE_CALL_RE.finditer(line):
                    call_name = m_call.group("name")
                    if call_name in PYTHON_BUILTINS_AND_COMMON:
                        continue
                    if call_name.startswith("__") and not call_name.endswith("__"):
                        continue
                    pos_cnt, kw_list, is_def_like = _parse_call_arguments(m_call.group("args"))
                    if call_name in symbols_map:
                        drift_msg = _check_inline_call_vs_signature(
                            call_name, pos_cnt, kw_list, is_def_like, symbols_map[call_name]
                        )
                        if drift_msg:
                            _add_finding(
                                rule_id="signature_drift",
                                severity="error",
                                doc_rel=doc_rel,
                                line_no=idx,
                                target_path=rel_target,
                                symbol=call_name,
                                message=f"Call contract mismatch for `{rel_target}::{call_name}` at `{doc_rel}:{idx}`: {drift_msg}",
                                remediation=f"Update arguments to match the current `{call_name}` signature in `{rel_target}`.",
                            )
                    else:
                        # Only flag `missing_symbol` for inline calls when explicitly qualified with target module stem
                        # (explicit `path::symbol` and `path#symbol` anchors are already checked above).
                        is_stem_qualified = f"{target_stem}.{call_name}(" in line
                        if len(resolved_line_files) == 1 and is_stem_qualified:
                            _add_finding(
                                rule_id="missing_symbol",
                                severity="error",
                                doc_rel=doc_rel,
                                line_no=idx,
                                target_path=rel_target,
                                symbol=call_name,
                                message=f"Symbol `{call_name}(...)` called at `{doc_rel}:{idx}` was not found in `{rel_target}`.",
                                remediation=f"Correct the reference to renamed or deleted function `{call_name}` in `{rel_target}`.",
                            )

                # 5. Git-diff blast radius mapping for this line
                if git_diff_data and rel_target in git_diff_data.get("changedFiles", {}):
                    f_delta = git_diff_data["changedFiles"][rel_target]
                    impacted_syms: List[str] = []
                    for del_sym in f_delta.get("deletedSymbols", []):
                        if re.search(rf"\b{re.escape(del_sym)}\b", line):
                            impacted_syms.append(f"deleted:{del_sym}")
                            _add_finding(
                                rule_id="missing_symbol",
                                severity="error",
                                doc_rel=doc_rel,
                                line_no=idx,
                                target_path=rel_target,
                                symbol=del_sym,
                                message=f"`{doc_rel}:{idx}` still references `{rel_target}::{del_sym}`, deleted in Git diff (`{base_ref}..{head_ref}`).",
                                remediation=f"Remove the deleted `{del_sym}` procedure at `{doc_rel}:{idx}` or update it to a replacement API.",
                            )
                    for brk_sym, brk_reason in (f_delta.get("breakingSymbols") or {}).items():
                        if re.search(rf"\b{re.escape(brk_sym)}\b", line):
                            impacted_syms.append(f"breaking:{brk_sym}({brk_reason})")
                            _add_finding(
                                rule_id="signature_drift",
                                severity="error",
                                doc_rel=doc_rel,
                                line_no=idx,
                                target_path=rel_target,
                                symbol=brk_sym,
                                message=f"Git diff (`{base_ref}..{head_ref}`) changed the call contract of `{rel_target}::{brk_sym}` (`{brk_reason}`), breaking the procedure at `{doc_rel}:{idx}`.",
                                remediation=f"Update `{brk_sym}` usage at `{doc_rel}:{idx}` to the current commit contract and verify it again.",
                            )
                    blast_radius.append({
                        "docPath": doc_rel,
                        "line": idx,
                        "targetPath": rel_target,
                        "gitStatus": f_delta.get("status"),
                        "impactedSymbols": impacted_syms,
                    })

        # 6. If this document is a `SKILL.md` with a sibling `references/provenance.json`, audit its stored anchors too!
        if doc_path.name == "SKILL.md":
            prov_path = doc_path.parent / "references" / "provenance.json"
            if prov_path.is_file():
                try:
                    prov = json.loads(prov_path.read_text(encoding="utf-8"))
                    ref_files = prov.get("referencedFiles") or []
                    for entry in ref_files:
                        if not isinstance(entry, dict) or not entry.get("path"):
                            continue
                        total_references_checked += 1
                        raw_entry_path = str(entry["path"])
                        p_ref = Path(raw_entry_path)
                        if not p_ref.is_absolute():
                            p_ref = root / raw_entry_path
                        rel_ref = (
                            str(p_ref.resolve().relative_to(root))
                            if p_ref.exists() and p_ref.resolve().is_relative_to(root)
                            else raw_entry_path
                        )
                        if not p_ref.is_file():
                            _add_finding(
                                rule_id="missing_path",
                                severity="error",
                                doc_rel=doc_rel,
                                line_no=1,
                                target_path=rel_ref,
                                symbol=None,
                                message=f"File `{rel_ref}` tracked by `{doc_rel}` (`provenance.json`) does not exist.",
                                remediation="Update the skill reference path or regenerate the skill.",
                            )
                            continue
                        sig_info = _get_sigs(p_ref)
                        curr_sigs = sig_info.get("symbols") or {}
                        curr_hashes = _get_hashes(p_ref)
                        resolution = sig_info.get("__resolution__", "unsupported")
                        tracked_syms = entry.get("symbols") if isinstance(entry.get("symbols"), dict) else {}
                        tracked_sigs = entry.get("symbolSignatures") if isinstance(entry.get("symbolSignatures"), dict) else {}
                        if resolution == "unsupported" and tracked_syms:
                            for s_name in tracked_syms:
                                _add_finding(
                                    rule_id="unsupported_anchor",
                                    severity="warning",
                                    doc_rel=doc_rel,
                                    line_no=1,
                                    target_path=rel_ref,
                                    symbol=s_name,
                                    message=f"Anchor `{rel_ref}::{s_name}` in `{doc_rel}` uses an unsupported language (`{sig_info.get('__language__', 'unknown')}`).",
                                    remediation="Link an anchor in a supported language (Python/TS/JS/Makefile/package.json) or an execution verification command.",
                                )
                        for s_name, old_sha in tracked_syms.items():
                            if s_name not in curr_hashes:
                                _add_finding(
                                    rule_id="missing_symbol",
                                    severity="error",
                                    doc_rel=doc_rel,
                                    line_no=1,
                                    target_path=rel_ref,
                                    symbol=s_name,
                                    message=f"Symbol `{rel_ref}::{s_name}` tracked by `{doc_rel}` (`provenance.json`) was deleted.",
                                    remediation=f"Update the procedure in `{doc_rel}` to match current symbols.",
                                )
                            elif curr_hashes.get(s_name) != old_sha and s_name in tracked_sigs and s_name in curr_sigs:
                                is_brk, brk_reason = is_breaking_symbol_change(tracked_sigs[s_name], curr_sigs[s_name])
                                if is_brk:
                                    _add_finding(
                                        rule_id="signature_drift",
                                        severity="error",
                                        doc_rel=doc_rel,
                                        line_no=1,
                                        target_path=rel_ref,
                                        symbol=s_name,
                                        message=f"Signature `{rel_ref}::{s_name}` in `{doc_rel}` (`provenance.json`) changed incompatibly (`{brk_reason}`).",
                                        remediation=f"Update the procedure in `{doc_rel}` and run `verify` again.",
                                    )
                except (OSError, json.JSONDecodeError):
                    pass

    pinned = None
    from memlet import memlet_wiki as mw
    if (root / mw.CONFIG_PATH).is_file():
        pinned = mw.pinned_anchor_report(root)
        for a in pinned["anchors"]:
            if a["status"] == "active":
                continue
            source = (a["sources"] or [mw.CONFIG_PATH + ":1"])[0]
            rule = next((r for r in a.get("rules", []) if r["source"] == source), {})
            label = suggested_label(rule).rstrip()
            doc, _, line = source.rpartition(":")
            _add_finding(rule_id="pinned_anchor_" + a["status"].replace("-", "_"),
                         severity="error" if a["status"] == "stale" else "warning",
                         doc_rel=doc, line_no=int(line) if line.isdigit() else 1,
                         target_path=a["path"], symbol=a["symbol"],
                         message=f"{a['status'].upper()} `{a['anchor']}` ({a['reason']})"
                                 + (" " + label if label else ""),
                         remediation="Re-read the rule and code; explicitly accept the anchor after review.")
        for d in (pinned.get("sidecar") or {}).get("detached") or []:  # accepted sidecar anchors that no longer attach
            anchor = str(d.get("anchor") or "")
            _add_finding(rule_id="sidecar_anchor_" + str(d.get("state") or "detached"), severity="warning",
                         doc_rel=str(d.get("source") or mw.CONFIG_PATH), line_no=1,
                         target_path=anchor.rsplit("::", 1)[0], symbol=anchor.rsplit("::", 1)[-1] if "::" in anchor else None,
                         message=f"{str(d.get('state') or 'detached').upper()} sidecar anchor `{d.get('id')}` `{anchor}` ({d.get('reason')})",
                         remediation="Run `memlet admin anchors list`; drop the entry or accept a fresh suggestion.")

    error_count = sum(1 for f in findings if f["severity"] == "error")
    warning_count = sum(1 for f in findings if f["severity"] == "warning")
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    by_rule: Dict[str, int] = {}
    for f in findings:
        by_rule[f["ruleId"]] = by_rule.get(f["ruleId"], 0) + 1

    report: Dict[str, Any] = {
        "ok": error_count == 0,
        "exitCode": 0 if error_count == 0 else 1,
        "repoPath": str(root),
        "scannedDocsCount": len(docs),
        "scannedInstructionFiles": len(docs),
        "scannedDocs": [str(p.relative_to(root)) for p in docs],
        "referencesChecked": total_references_checked,
        "totalReferencesChecked": total_references_checked,
        "commandsChecked": commands_checked,
        "unverifiedDynamicCommandCount": len(dynamic_unverified_commands),
        "unverifiedDynamicCommands": dynamic_unverified_commands[:50],
        "findingsCount": len(findings),
        "findingCount": len(findings),
        "errorCount": error_count,
        "warningCount": warning_count,
        "countsByRule": by_rule,
        "findings": findings,
        "blastRadius": blast_radius,
        "gitDiff": git_diff_data,
        "elapsedMs": elapsed_ms,
    }

    if pinned is not None:
        report["pinnedAnchors"] = pinned
    fmt = (output_format or "json").strip().lower()
    if fmt == "markdown":
        report["formatted"] = format_audit_markdown(report)
    elif fmt == "sarif":
        report["sarif"] = format_audit_sarif(report)
        report["formatted"] = json.dumps(report["sarif"], ensure_ascii=False, indent=2)
    return report


def format_audit_markdown(report: Dict[str, Any]) -> str:
    """Format an `audit_repository` report as a GitHub/PR Markdown summary."""
    status_badge = "PASS (0 broken contracts)" if report.get("ok") else f"FAIL ({report.get('errorCount', 0)} errors)"
    lines = [
        f"## Memlet Check · Procedure & Instruction Audit — {status_badge}",
        "",
        f"- **Repository**: `{report.get('repoPath')}`",
        f"- **Scanned Docs**: {report.get('scannedDocsCount', 0)} files ({report.get('referencesChecked', 0)} references checked in {report.get('elapsedMs', 0)} ms)",
        f"- **Findings**: {report.get('errorCount', 0)} errors, {report.get('warningCount', 0)} warnings",
        "",
    ]
    if "pinnedAnchors" in report:
        lines.append("- Pinned anchors: " + json.dumps(report["pinnedAnchors"]["summary"], sort_keys=True))
        for a in report["pinnedAnchors"]["anchors"]:
            head = f"- {a['status'].upper()} `{a['anchor']}` ({a['reason']})"
            if a.get("rules"):
                lines.extend(head + f" — {suggested_label(r)}{r['rule']} (`{r['source']}`)" for r in a["rules"])
            else:
                lines.append(head)
    findings = report.get("findings") or []
    if not findings:
        lines.append("All instruction documents (`CLAUDE.md`, `AGENTS.md`, `SKILL.md`, etc.) match the current code and script signatures.")
    else:
        lines.extend([
            "| Severity | Rule | Location | Target | Message |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])
        for f in findings:
            loc = f"`{f['docPath']}:{f['line']}`"
            tgt = f"`{f['targetPath']}::{f['symbol']}`" if f.get("symbol") else f"`{f['targetPath']}`"
            lines.append(f"| **{f['severity'].upper()}** | `{f['ruleId']}` | {loc} | {tgt} | {f['message']} |")
    blast = report.get("blastRadius") or []
    if blast:
        lines.extend([
            "",
            "### Git-Diff Blast Radius",
            "",
        ])
        for b in blast:
            syms = ", ".join(b.get("impactedSymbols") or []) or "file-level reference"
            lines.append(f"- `{b['docPath']}:{b['line']}` ← `{b['targetPath']}` (`{b.get('gitStatus')}`: {syms})")
    return "\n".join(lines) + "\n"


def format_audit_sarif(report: Dict[str, Any]) -> Dict[str, Any]:
    """Format an `audit_repository` report as SARIF v2.1.0 for GitHub Code Scanning."""
    rules = [
        {
            "id": "missing_path",
            "name": "MissingReferencedFilePath",
            "shortDescription": {"text": "Instruction document references a non-existent repository file path."},
        },
        {
            "id": "missing_script",
            "name": "MissingPackageOrMakeScript",
            "shortDescription": {"text": "Instruction document invokes a script/target missing from package.json or Makefile."},
        },
        {
            "id": "missing_symbol",
            "name": "MissingCodeSymbol",
            "shortDescription": {"text": "Instruction document references a function, class, or interface missing from target code file."},
        },
        {
            "id": "signature_drift",
            "name": "BreakingCallSignatureDrift",
            "shortDescription": {"text": "Instruction document call contract drifts from current code signature."},
        },
        {
            "id": "unsupported_anchor",
            "name": "UnsupportedLanguageAnchor",
            "shortDescription": {"text": "Symbol anchor attached to a file without structural AST contract support."},
        },
        {
            "id": "invalid_git_ref",
            "name": "InvalidGitComparisonRef",
            "shortDescription": {"text": "Specified Git base or head ref could not be resolved by git diff."},
        },
        {
            "id": "syntax_error",
            "name": "TargetFileSyntaxError",
            "shortDescription": {"text": "Referenced source file contains a syntax or parse error."},
        },
    ]
    results = []
    for f in report.get("findings") or []:
        level = "error" if f.get("severity") == "error" else "warning"
        results.append({
            "ruleId": f["ruleId"],
            "level": level,
            "message": {"text": f"{f['message']} ({f.get('remediation', '')})".strip()},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f["docPath"]},
                        "region": {"startLine": max(1, int(f.get("line") or 1))},
                    }
                }
            ],
        })
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Memlet",
                        "informationUri": "https://github.com/ian-nocturne/memlet",
                        "version": "9.3.0",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


audit_repo = audit_repository


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="memlet check", description="Memlet Check · Procedure & Instruction Auditor")
    parser.add_argument("repo_positional", nargs="?", default=None, help="Target repository path")
    parser.add_argument("--repo-path", default=None, help="Target repository path")
    parser.add_argument("--base", default=None, help="Git base ref for blast-radius diff")
    parser.add_argument("--head", default="HEAD", help="Git head ref for blast-radius diff")
    parser.add_argument("--format", default="json", choices=["json", "markdown", "sarif"], help="Output format")
    parser.add_argument("--exit-zero", action="store_true", help="Always return exit code 0 even when errors are found")
    args = parser.parse_args(argv)

    target = args.repo_path or args.repo_positional or "."
    res = audit_repository(target, base_ref=args.base, head_ref=args.head, output_format=args.format)
    if args.format in ("markdown", "sarif") and "formatted" in res:
        print(res["formatted"])
    else:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    if args.exit_zero:
        return 0
    return int(res.get("exitCode", 0 if res.get("ok", True) else 1))


if __name__ == "__main__":
    import sys
    sys.exit(main())

