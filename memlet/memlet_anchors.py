"""Automatic anchor suggestions and sidecar anchors (Memlet 9.1, unit D2).

Memlet can only re-check a rule that names code as `path::symbol`, and almost no instruction file is written that way.
The rules do mention code, in backticks and in prose (`validate_token()`, `` `src/auth.py` ``). This module finds those
mentions and PROPOSES an anchor. A suggestion is a proposal for a human, like a Dream candidate: Memlet cannot prove
that a name means one piece of code, and accepting a suggestion is a human decision.

Suggestions (`suggest_anchors`, read-only, no model call). The rules of the discovered instruction files and the
hand-written wiki pages that carry no `::` anchor are scanned for backticked identifiers / paths and `name()` forms
(URLs are ignored; a mention inside a quotation or after "e.g." / "for example" is never suggested). Two levels:

* `file+symbol`: the rule names a code file and that file has exactly one definition of the symbol (and no other
  binding of the name). A named file that does not define the name never falls back to another file, and a path with
  two readings (from the repository root and from the instruction file's folder) is ambiguous. A leading `/`
  (`` `/src/auth.py` ``) means "from the repository root" only. `accept --all-unique` accepts these.
* `single-definition-found`: a distinctive name for which the scan found one definition and no other binding-like
  occurrence. The reason states the evidence (how many files were indexed, how many were scanned as text, what was
  not scanned: dependency / build folders and nested repositories, listed in `notScanned`). Accepted by id only.

What counts against a single definition: in parsed Python and TS/JS every binding (definitions at any depth,
assignments, loop / `with` / `except` / walrus targets, imports that do not resolve to the defining file itself,
wildcard imports and exports, `__all__`, object-literal members, class fields, repeated declarations); in every
file that is not parsed by an indexer (vendored, generated, hidden, oversize, unparsable, other languages) ANY
whole-token occurrence of the name after NFKC normalisation. Anything that could not be read or enumerated, and every
bound that was hit, makes the scan incomplete: then no `single-definition-found` is offered at all.

Everything else is listed under `ambiguous` with a reason and candidates, never guessed. Every bound that was hit is
reported (`truncated`, `truncation`, `scanned.skippedSources`, `scanned.indexIncomplete`).

Sidecar anchors. `accept_suggestions` stores `{id, source, sectionPath, sectionSha256, line, ruleSha256, anchor, pin}`
in `.memlet/config.json` (`sidecarAnchors`) under the Memlet write lock and records the acceptance in the ledger
(`sidecar_anchor_events`). The user's files are never edited. The config write is refused when the file changed since
it was read; that is checked again immediately before the replace, but a writer that does not take Memlet's lock can
still race the final step. A rule's identity is its text hash plus the complete chain of Markdown headings (ATX and
Setext) it sits under, in the same file: a line shift under the same headings stays attached (`lineShifted`; the
recorded line is informational), a different heading chain, file or text detaches it. On every read (no cache) an
entry is honoured only while its shape is valid, the ledger's latest event for it is an `accept` of exactly these
bytes, the rule text occurs exactly once in its file, it is read as a rule, has no written `::` anchor and still
mentions the symbol. Attached entries are then checked like a written `path::symbol` in `check`, `recall`,
`handover`, the hooks and MCP, marked `suggested` with their acceptance id.

Integrity, not authentication. Like Dream approvals, the ledger check proves only that an entry is byte-for-byte what
`memlet admin anchors accept` recorded in this ledger, and that it was not dropped since. It does not prove who
accepted it: a process that can rewrite both `.memlet/config.json` and the ledger can forge an acceptance, and copying
both files into another clone carries the acceptances with them. The mention check limits what any entry (forged or
not) can claim: a rule can only ever be anchored to a symbol it names.

Threat model. Memlet defends against hostile repository CONTENT (static symlinks, crafted files) and serialises its own
writers. It does not defend against another process of the same user racing a write: such a process could modify the
same files directly anyway.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import posixpath
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import memlet_wiki as mw
from .audit import BACKTICK_FILE_RE, EXCLUDED_DIRS, IGNORED_FILE_TOKENS, PYTHON_BUILTINS_AND_COMMON
from .code_anchors import (_TS_JS_EXTS, _balanced_end, _blank_js_comments_preserve_lines, _collect_scoped_ast_nodes,
                           _extract_ts_js_signatures)

from .vocabulary import FILE_SYMBOL, SINGLE_DEFINITION_FOUND

SIDECAR_KEY = "sidecarAnchors"
CONFIDENCES = (FILE_SYMBOL, SINGLE_DEFINITION_FOUND)  # strongest first; only the first is accepted in bulk
LEDGER_TABLE = "sidecar_anchor_events"
_LEDGER_SCHEMA = ("CREATE TABLE IF NOT EXISTS sidecar_anchor_events (id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT,"
                  " entry_sha256 TEXT, event_type TEXT, recorded_at TEXT)")
INTEGRITY_NOTICE = ("Integrity, not authentication: the ledger shows each entry is exactly what `anchors accept` "
                    "recorded here and was not dropped since; it does not show who accepted it. A process that can "
                    "rewrite both .memlet/config.json and the ledger can forge an acceptance, and copying both files "
                    "to another clone carries the acceptances along.")

# Bounds. Every bound that is hit is reported; see `_suggest` for which ones make the result incomplete.
MAX_INDEX_FILES = 5000              # code files parsed for anchorable definitions
MAX_INDEX_FILE_BYTES = 2 * 1024 * 1024  # same cap as the extractors: a larger file is scanned textually only
MAX_INDEX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_SCAN_FILE_BYTES = 8 * 1024 * 1024   # textual binding scan of code that is not indexed; larger: incomplete
MAX_SCAN_TOTAL_BYTES = 48 * 1024 * 1024
MAX_NOT_SCANNED_LISTED = 20         # pruned folders named in `notScanned` (the count is always complete)
MAX_WALK_ENTRIES = 100_000          # directory entries visited while walking the repository
MAX_RULES = 5000                    # candidate rules considered
MAX_RULE_SCAN_CHARS = 16_384        # a longer rule is not scanned completely, so nothing in it is suggested
MAX_MENTIONS_PER_RULE = 24
MAX_CANDIDATES_SHOWN = 8
MAX_SIDECAR_ENTRIES = 1000          # more accepted entries than this: readers reject all, accept refuses to write
MAX_SECTION_DEPTH = 8                 # display only; the identity hash covers the complete chain
MAX_SECTION_TITLE = 200
_RULE_DISPLAY = 160

_INDEX_EXTS = frozenset({".py"} | set(_TS_JS_EXTS))
_OTHER_CODE_EXTS = frozenset({".go", ".rs", ".rb", ".java", ".kt", ".kts", ".swift", ".c", ".h", ".cc", ".cpp", ".cxx",
                              ".hpp", ".hh", ".cs", ".php", ".scala", ".lua", ".sh", ".bash", ".zsh", ".pl", ".pm",
                              ".r", ".jl", ".ex", ".exs", ".erl", ".dart", ".vue", ".svelte", ".m", ".mm", ".groovy",
                              ".clj", ".hs", ".ml", ".fs", ".nim", ".zig", ".pyi", ".pyx", ".astro"})
_CODE_EXTS = _INDEX_EXTS | _OTHER_CODE_EXTS
# Files that are never code (documentation, data, styles, media): not scanned for name occurrences. Every other
# regular file that no indexer parses is scanned as text, so an unknown language still counts against a definition.
_NOT_CODE_EXTS = frozenset({".md", ".mdx", ".markdown", ".mdc", ".txt", ".rst", ".adoc", ".org", ".json", ".jsonl",
                            ".ndjson", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv", ".lock",
                            ".log", ".css", ".scss", ".sass", ".less", ".svg", ".xml", ".map", ".png", ".jpg", ".jpeg",
                            ".gif", ".ico", ".webp", ".bmp", ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".zst",
                            ".tar", ".jar", ".whl", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".mov",
                            ".wav", ".sqlite", ".sqlite3", ".db", ".pyc", ".so", ".dylib", ".dll", ".exe", ".o", ".a",
                            ".class", ".parquet", ".pkl", ".npy", ".bin"})
_NOT_CODE_NAMES = frozenset({"readme", "license", "licence", "changelog", "changes", "notice", "authors",
                             "contributing", "copying", "codeowners", "history", "news"})
# Not repository source (dependencies, build output, VCS / Memlet state): never walked.
_PRUNE_DIRS = frozenset(set(EXCLUDED_DIRS) | {mw.MEMLET_DIR, ".hg", ".svn", "bower_components", "jspm_packages"})
# Repository code that is never anchored to, but whose name occurrences still count against a single definition.
_SKIPPED_SOURCE_DIRS = frozenset({"vendor", "vendors", "third_party", "third-party", "generated", "__generated__"})
_GENERATED_NAME_RE = re.compile(r"(?:\.min\.(?:js|mjs|cjs)|\.bundle\.(?:js|mjs)|\.d\.(?:ts|mts|cts)|_pb2(?:_grpc)?\.py"
                                r"|\.(?:generated|gen)\.[A-Za-z]+)$", re.I)
# Generator markers only (case-sensitive `DO NOT EDIT`): a hand-written "do not edit lightly" keeps a file indexed.
_GENERATED_HEAD_RE = re.compile(r"@generated\b|\bDO NOT EDIT\b|(?i:\bauto-?generated\b|\bcode generated by\b)")
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs", "testing", "e2e", "fixtures", "__mocks__"})
_TEST_NAME_RE = re.compile(r"(?:^test_.*\.py$|_test\.py$|^conftest\.py$|\.(?:test|spec)\.[A-Za-z]+$)")

_SPAN_RE = re.compile(r"`([^`\n]{1,200})`")
_IDENT_RE = re.compile(r"(?P<name>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*){0,4})(?P<call>\([^()`]{0,120}\))?")
_PROSE_CALL_RE = re.compile(r"(?<![\w$.])(?P<name>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*){0,4})\(\)")
_EXAMPLE_RE = re.compile(r"\be\.\s?g\.|\bfor (?:example|instance)\b|\bexamples?\b", re.I)
_SYMBOL_RE = re.compile(r"[A-Za-z_$][\w.$-]{0,200}")
_HEX_RE = re.compile(r"[0-9a-f]{6,64}")
_NAME_SCAN_RE = re.compile(r"[A-Za-z_$][\w$]*")
_TEXT_DEF_BEFORE = re.compile(r"(?:^|[^\w$.])(?:def|function|func|fn|class|sub|interface|struct|enum|type|trait|"
                              r"const|let|var|val|module|object)\s*\*?\s+$")
_ASSIGN_AFTER = re.compile(r"[ \t]*(?:\?[ \t]*)?(?::[^=\n;,)]{0,80})?:?=(?![=>])")

# Names too generic to anchor on their own, even when they are defined only once: a rule saying "run `build` first"
# means a command, not the one function that happens to be called `build`. With a file mention they are fine.
COMMON_NAMES = frozenset(w.lower() for w in PYTHON_BUILTINS_AND_COMMON) | frozenset("""
main run test tests setup teardown init start stop build config configure settings index app application data utils
util helpers helper default get set put post patch update create delete remove add handler handle client server api
model models user users name value values key keys item items file files path paths log logger error errors result
results request response req res ctx context state store cache db database query schema types type id url src lib
core base common shared foo bar baz todo check verify validate parse load save read write close send fetch call exec
execute process render format dump component props router route routes service services manager options env
environment true false none null undefined self this new delete import export from async await yield lambda class def
function return default object string number boolean array map filter reduce list dict tuple main_ wrapper decorator
logging debug info warn warning install deploy release lint format clean serve dev prod production staging status
""".split())


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def suggestion_id(source: str, rule_sha: str, anchor: str, section_sha: str = "") -> str:
    """Stable id of a suggestion / sidecar entry: same file, heading chain (its `sectionSha256`), rule text and
    anchor give the same id."""
    return "anc-" + _sha256(json.dumps([source, section_sha or _section_sha(()), rule_sha, anchor], ensure_ascii=True))[:12]


def _mask(text: str) -> str:
    from .memlet_brief import _mask as mask
    return mask(text)


def _clip(text: str, limit: int = _RULE_DISPLAY) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ---------------------------------------------------------------------------------------------------------------
# Mentions


def _url_spans(text: str) -> List[Tuple[int, int]]:
    """`scheme://...` spans, found in linear time (no regex retried from every position)."""
    spans: List[Tuple[int, int]] = []
    i = text.find("://")
    while i != -1:
        s = i
        while s > 0 and (text[s - 1].isalnum() or text[s - 1] in "+.-"):
            s -= 1
        e = i + 3
        while e < len(text) and not text[e].isspace() and text[e] not in "`<>\"'":
            e += 1
        if s < i and text[s].isalpha():
            spans.append((s, e))
        i = text.find("://", e)
    return spans


def _example_mask(text: str, code_spans: List[Tuple[int, int]]) -> bytearray:
    """1 for every character inside a quotation ("…", '…', “…”, ‘…’, outside code spans) or after "e.g." / "for
    example" / "example". Linear: built once, then looked up per mention."""
    code = bytearray(len(text))
    for a, b in code_spans:
        code[a:b] = b"\x01" * (b - a)
    mask = bytearray(len(text))
    quotes = [i for i, ch in enumerate(text) if ch == '"' and not code[i]]
    for k in range(0, len(quotes) - 1, 2):
        a, b = quotes[k], quotes[k + 1] + 1
        mask[a:b] = b"\x01" * (b - a)
    # paired single quotes: an opener follows a non-alphanumeric character and precedes a non-space, a closer
    # follows a non-space and precedes a non-alphanumeric character; apostrophes inside words (don't, it's) are neither
    n, open_at = len(text), None
    for i, ch in enumerate(text):
        if ch not in "'‘’" or code[i]:
            continue
        before = text[i - 1] if i else " "
        after = text[i + 1] if i + 1 < n else " "
        if open_at is None:
            if ch in "'‘" and not before.isalnum() and not after.isspace():
                open_at = i
        elif ch in "'’" and not before.isspace() and not after.isalnum():
            mask[open_at:i + 1] = b"\x01" * (i + 1 - open_at)
            open_at = None
    i = text.find("“")
    while i != -1:
        j = text.find("”", i + 1)
        if j == -1:
            break
        mask[i:j + 1] = b"\x01" * (j + 1 - i)
        i = text.find("“", j + 1)
    m = _EXAMPLE_RE.search(text)
    if m:
        mask[m.start():] = b"\x01" * (len(text) - m.start())
    return mask


def rule_mentions(text: str) -> Dict[str, Any]:
    """Code mentions in one rule (bounded, linear). `files`: [{path, symbol, rootRelative, example}] from backticked
    paths (`` `src/a.py` ``, `` `src/a.py#name` ``, `` `/src/a.py` `` = from the repository root only). `idents`:
    [{name, call, example}] from backticked identifiers (`` `name` ``, `` `Cls.name` ``, `` `name(x)` ``) and prose
    `name()`. `example`: inside a quotation or after "e.g." / "for example". URLs and written `::` anchors are not
    mentions. `complete` is False when the rule is longer than MAX_RULE_SCAN_CHARS or has too many mentions."""
    complete = len(text or "") <= MAX_RULE_SCAN_CHARS
    text = (text or "")[:MAX_RULE_SCAN_CHARS]
    urls = _url_spans(text)
    if urls:
        chars = list(text)
        for a, b in urls:
            chars[a:b] = " " * (b - a)
        text = "".join(chars)
    spans = [(m.start(), m.end()) for m in _SPAN_RE.finditer(text)]
    zone = _example_mask(text, spans)

    def example(pos: int) -> bool:
        return bool(zone[pos]) if 0 <= pos < len(zone) else False
    files: List[Dict[str, Any]] = []
    idents: List[Dict[str, Any]] = []

    def add(lst: List[Dict[str, Any]], item: Dict[str, Any]) -> None:
        nonlocal complete
        if item in lst:
            return
        if len(files) + len(idents) >= MAX_MENTIONS_PER_RULE:
            complete = False
            return
        lst.append(item)

    for m in _SPAN_RE.finditer(text):
        span = m.group(1).strip()
        if not span or "::" in span:
            continue
        root_relative = span.startswith("/") and not span.startswith("//")
        fm = BACKTICK_FILE_RE.fullmatch("`" + (span[1:] if root_relative else span) + "`")
        ex = example(m.start())
        if fm:
            path = mw._clean_ref_path(fm.group("path"))
            if path.lower() in IGNORED_FILE_TOKENS:
                continue
            sym = fm.group("symbol")
            add(files, {"path": path, "symbol": sym.rstrip(".-") if sym else None, "rootRelative": root_relative,
                        "example": ex})
            if sym:
                add(idents, {"name": sym.rstrip(".-"), "call": False, "example": ex})
            continue
        im = _IDENT_RE.fullmatch(span)
        if im:
            add(idents, {"name": im.group("name"), "call": bool(im.group("call")), "example": ex})
    chars = list(text)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
    prose = "".join(chars)
    for m in _PROSE_CALL_RE.finditer(prose):
        name = m.group("name")
        if BACKTICK_FILE_RE.fullmatch("`" + name + "`"):
            continue  # `config.py()` is a file name, not a call
        add(idents, {"name": name, "call": True, "example": example(m.start())})
    return {"files": files, "idents": idents, "complete": complete}


def _distinctive(name: str, call_form: bool) -> bool:
    """Could this name only mean code? Dotted, snake_case, camelCase / compound PascalCase, or written as a call, and
    not a generic word. A plain capitalised word (`Convention`) is English, not code. Only for
    `single-definition-found`."""
    last = name.rsplit(".", 1)[-1]
    if last.lower() in COMMON_NAMES or len(last) < 3:
        return False
    if "." in name or "_" in last.strip("_") or call_form:
        return True
    return bool(re.search(r"[a-z0-9][A-Z]", last))


def mentions_symbol(text: str, symbol: str) -> bool:
    """Does the rule text itself mention `symbol` (its full dotted name or a dotted tail of it), outside examples?"""
    for i in rule_mentions(text)["idents"]:
        if not i["example"] and (symbol == i["name"] or symbol.endswith("." + i["name"])):
            return True
    return False


# ---------------------------------------------------------------------------------------------------------------
# Repository bindings


def _is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    return any(p.lower() in _TEST_DIRS for p in parts[:-1]) or bool(_TEST_NAME_RE.search(parts[-1]))


def _line_at(text: str, pos: int, cache: List[int]) -> int:
    """1-based line of `pos`; `cache` = [last_pos, last_line] for increasing positions (linear overall)."""
    last_pos, last_line = cache
    if pos < last_pos:
        last_pos, last_line = 0, 1
    last_line += text.count("\n", last_pos, pos)
    cache[0], cache[1] = pos, last_line
    return last_line


class _PyBindings(ast.NodeVisitor):
    """Every binding of a wanted name in one Python module, at any depth (`match` bodies included)."""

    def __init__(self, wanted: Set[str]) -> None:
        self.wanted, self.scope, self.out, self.exported = wanted, [], [], []

    def _add(self, name: str, line: int, kind: str, qual: Optional[str], **extra: Any) -> None:
        if name in self.wanted:
            self.out.append(dict(name=name, line=line, kind=kind, qual=qual, **extra))

    def _qual(self, name: str) -> str:
        return ".".join(self.scope + [name])

    def _def(self, node: Any, kind: str) -> None:
        self._add(node.name, node.lineno, kind, self._qual(node.name))
        for d in node.decorator_list:
            self.visit(d)
        if not isinstance(node, ast.ClassDef):
            self.visit(node.args)
        self.scope.append(node.name if isinstance(node, ast.ClassDef) else node.name + ".<locals>")
        for stmt in node.body:
            self.visit(stmt)
        self.scope.pop()

    def visit_FunctionDef(self, node: Any) -> None:
        self._def(node, "def")

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: Any) -> None:
        self._def(node, "class")

    def _target(self, t: Any, line: int) -> None:
        if isinstance(t, ast.Name):
            self._add(t.id, line, "assign", self._qual(t.id))
        elif isinstance(t, ast.Attribute):
            owner = t.value.id + "." if isinstance(t.value, ast.Name) else None
            self._add(t.attr, line, "assign", owner + t.attr if owner else None)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                self._target(e, line)
        elif isinstance(t, ast.Starred):
            self._target(t.value, line)

    def visit_Assign(self, node: Any) -> None:
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                self.exported += [(e.value, node.lineno) for e in node.value.elts
                                  if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            self._target(t, node.lineno)
        self.visit(node.value)

    def visit_AnnAssign(self, node: Any) -> None:
        self._target(node.target, node.lineno)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: Any) -> None:
        self._target(node.target, node.lineno)
        self.visit(node.value)

    def visit_NamedExpr(self, node: Any) -> None:
        self._target(node.target, getattr(node, "lineno", 0))
        self.visit(node.value)

    def visit_For(self, node: Any) -> None:
        self._target(node.target, node.lineno)
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_With(self, node: Any) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._target(item.optional_vars, node.lineno)
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: Any) -> None:
        if node.name:
            self._add(node.name, getattr(node, "lineno", 0), "assign", None)
        self.generic_visit(node)

    def visit_Global(self, node: Any) -> None:
        for name in node.names:
            self._add(name, node.lineno, "assign", None)

    visit_Nonlocal = visit_Global

    def visit_Import(self, node: Any) -> None:
        for a in node.names:
            bound = a.asname or a.name.split(".")[0]
            self._add(bound, node.lineno, "import-module", None, module=a.name, level=0, orig=None)

    def visit_ImportFrom(self, node: Any) -> None:
        for a in node.names:
            if a.name == "*":  # could bind any name: blocks every wanted name
                for name in self.wanted:
                    self._add(name, node.lineno, "wildcard", None, module=node.module or "", level=int(node.level or 0))
                continue
            self._add(a.asname or a.name, node.lineno, "import", None, module=node.module or "",
                      level=int(node.level or 0), orig=a.name)

    def visit_MatchAs(self, node: Any) -> None:  # Python 3.10+: `case NAME:` binds NAME
        if getattr(node, "name", None):
            self._add(node.name, getattr(node, "lineno", 0), "assign", None)
        self.generic_visit(node)

    def visit_MatchStar(self, node: Any) -> None:
        if getattr(node, "name", None):
            self._add(node.name, getattr(node, "lineno", 0), "assign", None)


def _py_analysis(text: str, wanted: Set[str]) -> Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """(anchorable definitions, bindings of wanted names), None when the module does not parse."""
    try:
        tree = ast.parse(text)
        nodes = _collect_scoped_ast_nodes(tree.body)
        v = _PyBindings(wanted)
        v.visit(tree)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    targets = []
    for qname, name, nested, node in nodes:
        if name not in wanted or "<locals>" in qname or not _SYMBOL_RE.fullmatch(qname):
            continue
        kind = "class" if isinstance(node, ast.ClassDef) else ("method" if nested else "function")
        targets.append({"symbol": qname, "name": name, "line": int(getattr(node, "lineno", 0) or 0), "kind": kind})
    bound = {o["name"] for o in v.out}
    for name, line in v.exported:  # `__all__` naming something this module does not bind itself
        if name in wanted and name not in bound:
            v.out.append({"name": name, "line": line, "kind": "export", "qual": None})
    return targets, v.out


_TS_CLASS_RE = re.compile(r"\bclass[ \t]+([A-Za-z_$][\w$]*)[^{;\n]*\{")
_TS_IMPORT_RE = re.compile(r"\bimport\s+(?:type\s+)?([\w$\s{},*]{1,2000}?)\s+from\s*(['\"])([^'\"\n]{1,300})\2")
_TS_REEXPORT_RE = re.compile(r"\bexport\s+(?:type\s+)?(\{[^}]{0,2000}\})\s*from\s*(['\"])([^'\"\n]{1,300})\2")
_TS_EXPORT_LOCAL_RE = re.compile(r"\bexport\s+(?:type\s+)?(\{[^}]{0,2000}\})(?!\s*from\b)")
_TS_STAR_RE = re.compile(r"\bexport\s+\*\s*(?:as\s+([A-Za-z_$][\w$]*)\s*)?from\b")
_JS_ESCAPE_RE = re.compile(r"\\u(?:\{([0-9a-fA-F]{1,6})\}|([0-9a-fA-F]{4}))")
_TS_REQUIRE_RE = re.compile(r"\b(?:const|let|var)\s*(\{[^}]{0,2000}\})\s*=\s*require\(\s*(['\"])([^'\"\n]{1,300})\2")
_TS_REQUIRE1_RE = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(\s*(['\"])([^'\"\n]{1,300})\2")
_TS_MEMBER_BEFORE = re.compile(r"^[ \t]*(?:(?:public|private|protected|static|readonly|override|abstract|async|get|set|"
                               r"declare|export|default|accessor)[ \t]+)*\*?[ \t]*$")
_TS_METHOD_AFTER = re.compile(r"[ \t]*(?:<[^>\n]{0,120}>)?[ \t]*\([^)]{0,600}\)[ \t]*(?::[^{;\n]{0,160})?\{")
_TS_PROP_AFTER = re.compile(r"[ \t]*[?!]?[ \t]*[:;=]")      # `name:` / `name;` / `name =` (members, fields, keys)
_TS_MEMBER_PREFIX = re.compile(r"(?:^|[{,;}])[ \t]*(?:(?:public|private|protected|static|readonly|override|abstract|"
                               r"async|get|set|declare|accessor)[ \t]+)*\*?[ \t]*$")


def _decode_js_escapes(text: str) -> Optional[str]:
    """JS identifier escapes (`\\u0076`, `\\u{76}`) decoded, None when one cannot be decoded."""
    def rep(m: Any) -> str:
        cp = int(m.group(1) or m.group(2), 16)
        if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
            raise ValueError("bad escape")
        return chr(cp)
    try:
        return _JS_ESCAPE_RE.sub(rep, text)
    except ValueError:
        return None


def _ts_import_names(clause: str) -> List[Tuple[str, str]]:
    """[(bound, imported)] of an import / export / destructuring clause."""
    out: List[Tuple[str, str]] = []
    clause = clause.strip()
    inner = ""
    if "{" in clause:
        head, _, rest = clause.partition("{")
        inner = rest.split("}", 1)[0]
        clause = head
    for part in clause.split(","):
        part = part.strip()
        if part.startswith("*"):
            m = re.match(r"\*\s*as\s+([A-Za-z_$][\w$]*)", part)
            if m:
                out.append((m.group(1), "*"))
        elif re.fullmatch(r"[A-Za-z_$][\w$]*", part):
            out.append((part, "default"))
    for part in inner.split(","):
        m = re.fullmatch(r"\s*(?:type\s+)?([A-Za-z_$][\w$]*)(?:\s+as\s+([A-Za-z_$][\w$]*)|\s*:\s*([A-Za-z_$][\w$]*))?\s*",
                         part)
        if m:
            out.append((m.group(2) or m.group(3) or m.group(1), m.group(1)))
    return out


def _ts_analysis(text: str, wanted: Set[str]) -> Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """(anchorable definitions from the lexical scanner, bindings of wanted names), None when it does not scan."""
    try:
        ok, sigs = _extract_ts_js_signatures(text)
        masked = _blank_js_comments_preserve_lines(text, blank_strings=True)[0]
        code = _blank_js_comments_preserve_lines(text, blank_strings=False)[0]
    except (RecursionError, MemoryError, ValueError):
        return None
    if "\\u" in masked:  # `\u0076alidate_token` is the identifier validate_token
        decoded = _decode_js_escapes(masked)
        if decoded is None:
            return None
        masked = decoded
    if not ok:
        return None
    targets = []
    dotted_ids = {id(s) for k, s in sigs.items() if "." in k}
    for key in sorted(sigs):
        sig = sigs[key]
        last = key.rsplit(".", 1)[-1]
        if last not in wanted or key == "default" or not isinstance(sig, dict) or sig.get("kind") == "ambiguous_method":
            continue
        if "." not in key and id(sig) in dotted_ids:
            continue  # the short alias of `Class.method`: indexed through the qualified entry
        if _SYMBOL_RE.fullmatch(key):
            targets.append({"symbol": key, "name": last, "line": int(sig.get("line") or 0),
                            "kind": "method" if "." in key else str(sig.get("kind") or "symbol")})
    classes = []
    for cm in _TS_CLASS_RE.finditer(masked):
        end = _balanced_end(masked, cm.end() - 1)
        classes.append((cm.group(1), cm.end() - 1, end if end > 0 else len(masked)))
    out: List[Dict[str, Any]] = []
    depth, last_pos, lc = 0, 0, [0, 1]
    for m in _NAME_SCAN_RE.finditer(masked):
        name = m.group(0)
        if name not in wanted:
            continue
        s, e = m.span()
        chunk = masked[last_pos:s]
        depth += chunk.count("{") - chunk.count("}")
        last_pos = s
        line_start = masked.rfind("\n", max(0, s - 400), s) + 1
        before = masked[max(line_start, s - 120):s]
        kind = None
        if _TEXT_DEF_BEFORE.search(masked[max(0, s - 40):s]) or re.search(r"(?:^|[^\w$.])(?:get|set)[ \t]+$", before):
            kind = "def"
        elif _ASSIGN_AFTER.match(masked, e, e + 120):
            kind = "assign"
        elif _TS_MEMBER_PREFIX.search(before) and _TS_METHOD_AFTER.match(masked, e, e + 700):
            kind = "method"  # class or object-literal method, on its own line or inline
        elif _TS_MEMBER_PREFIX.search(before) and _TS_PROP_AFTER.match(masked, e, e + 40):
            kind = "property"  # object-literal key, class field, interface member
        if kind is None:
            continue
        owner = None
        for cname, a, b in classes:
            if a < s < b and (owner is None or a > owner[1]):
                owner = (cname, a)
        if owner and kind != "def":
            qual = f"{owner[0]}.{name}"
        elif owner and re.search(r"(?:^|[^\w$.])(?:get|set)[ \t]+$", before):
            qual = f"{owner[0]}.{name}"
        else:
            qual = name if depth <= 0 and kind == "def" else None
        out.append({"name": name, "line": _line_at(masked, s, lc), "kind": kind, "qual": qual})
    lc = [0, 1]
    for rx in (_TS_IMPORT_RE, _TS_REEXPORT_RE, _TS_REQUIRE_RE):
        for m in rx.finditer(code):
            for bound, orig in _ts_import_names(m.group(1)):
                if bound in wanted:
                    out.append({"name": bound, "line": _line_at(code, m.start(), [0, 1]), "kind": "import", "qual": None,
                                "module": m.group(3), "level": 0, "orig": orig})
    for m in _TS_EXPORT_LOCAL_RE.finditer(code):  # `export { foreign as name }`: a new name for another binding
        for bound, orig in _ts_import_names(m.group(1)):
            if bound in wanted and bound != orig:
                out.append({"name": bound, "line": _line_at(code, m.start(), [0, 1]), "kind": "export-alias", "qual": None})
    for m in _TS_STAR_RE.finditer(code):  # `export * from`: could bind any name
        if m.group(1):
            if m.group(1) in wanted:
                out.append({"name": m.group(1), "line": _line_at(code, m.start(), [0, 1]), "kind": "import-module", "qual": None})
            continue
        for name in wanted:
            out.append({"name": name, "line": _line_at(code, m.start(), [0, 1]), "kind": "wildcard", "qual": None})
    for m in _TS_REQUIRE1_RE.finditer(code):
        if m.group(1) in wanted:
            out.append({"name": m.group(1), "line": _line_at(code, m.start(), [0, 1]), "kind": "import-module",
                        "qual": None, "module": m.group(3), "level": 0, "orig": None})
    return targets, out


_TOKEN_RE = re.compile(r"[\w$]+")


def _text_bindings(text: str, wanted: Set[str]) -> List[Dict[str, Any]]:
    """Files no indexer parses: ANY whole-token occurrence of a wanted name, compared after NFKC normalisation (so
    `𝚟alidate_token` counts as `validate_token`), is a possible binding. No per-language patterns."""
    out: List[Dict[str, Any]] = []
    norm = unicodedata.normalize("NFKC", text)
    lc = [0, 1]
    for m in _TOKEN_RE.finditer(norm):
        if m.group(0) in wanted:
            out.append({"name": m.group(0), "line": _line_at(norm, m.start(), lc), "kind": "text", "qual": None})
    return out


def _exists_exact(root: Path, rel: str) -> bool:
    """`rel` is a regular, contained file whose spelling matches the directory entries exactly (case-sensitive)."""
    if mw._unsafe_rel(rel) or mw.contained_path_problem(root, rel) or not (root / rel).is_file():
        return False
    cur = root
    for part in rel.split("/"):
        try:
            if part not in os.listdir(str(cur)):
                return False
        except OSError:
            return False
        cur = cur / part
    return True


class SymbolIndex:
    """Anchorable definitions and every binding of the wanted names across the repository. Built once per command,
    from the current file contents (nothing is cached across calls)."""

    def __init__(self, root: Path, wanted: Set[str]) -> None:
        self.root = root
        self.wanted = set(wanted)
        self.targets: Dict[str, List[Dict[str, Any]]] = {}
        self.bindings: Dict[str, List[Dict[str, Any]]] = {}
        self.analysis: Dict[str, Optional[Tuple[Optional[List[Dict[str, Any]]], List[Dict[str, Any]]]]] = {}
        self.code_files: Set[str] = set()
        self.basenames: Dict[str, List[str]] = {}
        self.truncated: List[str] = []   # a bound stopped the walk: the index is incomplete
        self.incomplete: List[str] = []  # something relevant could not be read or scanned
        self.stats = {"codeFiles": 0, "symbols": 0, "bytes": 0, "skippedCodeFiles": 0, "scannedBytes": 0,
                      "generatedSkipped": 0, "oversizedSkipped": 0, "parseErrors": 0, "walkEntries": 0}
        self.not_scanned: List[str] = []  # pruned folders (dependencies, build output, nested repositories)
        if self.wanted:
            self._build()

    @property
    def complete(self) -> bool:
        return not (self.truncated or self.incomplete)

    def not_scanned_summary(self) -> Dict[str, Any]:
        """{count, paths (at most MAX_NOT_SCANNED_LISTED), reason}: folders left out for cost; they do not demote."""
        return {"count": len(self.not_scanned), "paths": self.not_scanned[:MAX_NOT_SCANNED_LISTED],
                "reason": "dependency / build folders and nested repositories are not scanned"}

    def _record(self, rel: str, targets: Optional[List[Dict[str, Any]]], occ: List[Dict[str, Any]]) -> None:
        self.analysis[rel] = (targets, occ)
        test = _is_test_path(rel)
        for t in targets or []:
            self.targets.setdefault(t["name"], []).append(dict(t, path=rel, anchor=f"{rel}::{t['symbol']}", test=test))
            self.stats["symbols"] += 1
        for o in occ:
            self.bindings.setdefault(o["name"], []).append(dict(o, path=rel, test=test))

    def _scan_text_only(self, rel: str, full: str, size: int, text: Optional[str] = None,
                        binary_skip: bool = False) -> None:
        if size > MAX_SCAN_FILE_BYTES:
            self.incomplete.append(f"not_scanned_oversize:{rel}")
            return
        if self.stats["scannedBytes"] + size > MAX_SCAN_TOTAL_BYTES:
            self.incomplete.append(f"scan_bytes>{MAX_SCAN_TOTAL_BYTES}")
            return
        if text is None:
            text = mw.read_regular_text(Path(full), MAX_SCAN_FILE_BYTES)
        if text is None:
            self.incomplete.append(f"unreadable:{rel}")
            return
        if binary_skip and "\x00" in text[:8192]:
            return  # a binary file of an unknown type: not code
        self.stats["scannedBytes"] += size
        self.stats["skippedCodeFiles"] += 1
        self._record(rel, None, _text_bindings(text, self.wanted))

    def _analyse(self, rel: str, text: str) -> Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
        return _py_analysis(text, self.wanted) if rel.lower().endswith(".py") else _ts_analysis(text, self.wanted)

    def _walk_error(self, exc: OSError) -> None:
        where = getattr(exc, "filename", None) or "?"
        try:
            where = Path(where).relative_to(self.root).as_posix()
        except ValueError:
            pass
        self.incomplete.append(f"walk_error:{where}:{exc.strerror or type(exc).__name__}")

    def _build(self) -> None:
        root = self.root
        seen = 0
        for cur, dirs, files in os.walk(root, onerror=self._walk_error):
            rel_dir = Path(cur).relative_to(root).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir
            if rel_dir and (Path(cur) / ".git").exists():
                dirs[:] = []
                self.not_scanned.append(rel_dir + "/ (nested repository)")
                continue  # a nested repository / worktree: its code belongs to that repository
            parts = rel_dir.split("/") if rel_dir else []
            skipped_dir = any(p in _SKIPPED_SOURCE_DIRS or p.startswith(".") for p in parts)
            kept = []
            for d in sorted(dirs):
                if os.path.islink(os.path.join(cur, d)):
                    continue  # never followed: not repository content
                if d in _PRUNE_DIRS:
                    if d != mw.MEMLET_DIR and not (d == ".git" and not rel_dir):
                        self.not_scanned.append(f"{rel_dir}/{d}/" if rel_dir else f"{d}/")
                    continue
                kept.append(d)
            dirs[:] = kept
            seen += len(dirs) + len(files)
            self.stats["walkEntries"] = seen
            if seen > MAX_WALK_ENTRIES:
                self.truncated.append(f"walk_entries>{MAX_WALK_ENTRIES}")
                return
            for fn in sorted(files):
                full = os.path.join(cur, fn)
                rel = f"{rel_dir}/{fn}" if rel_dir else fn
                ext = Path(fn).suffix.lower()
                code = ext in _CODE_EXTS
                if not code and (ext in _NOT_CODE_EXTS or fn.split(".")[0].lower() in _NOT_CODE_NAMES
                                 or (fn.startswith(".") and not ext)):
                    continue  # documentation, data, styles, media, config dotfiles: never code
                if mw._is_secret_path(rel) or mw.instruction_path_shape(rel):
                    continue  # secrets are never read; instruction files are the rules themselves
                try:
                    st = os.lstat(full)
                except OSError as exc:
                    self.incomplete.append(f"stat_error:{rel}:{exc.strerror or type(exc).__name__}")
                    continue
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                if code:
                    self.code_files.add(rel)
                    self.basenames.setdefault(fn, []).append(rel)
                if not code or skipped_dir or ext not in _INDEX_EXTS:
                    self._scan_text_only(rel, full, st.st_size, binary_skip=not code)
                    continue
                if _GENERATED_NAME_RE.search(fn):
                    self.stats["generatedSkipped"] += 1
                    self._scan_text_only(rel, full, st.st_size)
                    continue
                if st.st_size > MAX_INDEX_FILE_BYTES:
                    self.stats["oversizedSkipped"] += 1
                    self._scan_text_only(rel, full, st.st_size)
                    continue
                if self.stats["codeFiles"] >= MAX_INDEX_FILES:
                    self.truncated.append(f"code_files>{MAX_INDEX_FILES}")
                    return
                if self.stats["bytes"] + st.st_size > MAX_INDEX_TOTAL_BYTES:
                    self.truncated.append(f"code_bytes>{MAX_INDEX_TOTAL_BYTES}")
                    return
                text = mw.read_regular_text(Path(full), MAX_INDEX_FILE_BYTES)
                if text is None:
                    self.incomplete.append(f"unreadable:{rel}")
                    continue
                self.stats["codeFiles"] += 1
                self.stats["bytes"] += st.st_size
                if _GENERATED_HEAD_RE.search(text[:2048]):
                    self.stats["generatedSkipped"] += 1
                    self._scan_text_only(rel, full, st.st_size, text)
                    continue
                res = self._analyse(rel, text)
                if res is None:  # does not parse: no anchor into it could be active, but its names still count
                    self.stats["parseErrors"] += 1
                    self._scan_text_only(rel, full, st.st_size, text)
                else:
                    self._record(rel, res[0], res[1])

    def file_analysis(self, rel: str) -> Tuple[Optional[List[Dict[str, Any]]], List[Dict[str, Any]]]:
        """(anchorable definitions or None, bindings) of one file, parsed on demand when the walk only scanned it.
        Pruned folders (node_modules, .memlet, ...), symlinks and secrets are never read."""
        cached = self.analysis.get(rel)
        if cached is not None and cached[0] is not None:
            return cached
        parts = rel.split("/")
        if Path(rel).suffix.lower() not in _INDEX_EXTS or any(p in _PRUNE_DIRS for p in parts[:-1]) or mw._unsafe_rel(rel):
            return cached or (None, [])
        if mw.contained_path_problem(self.root, rel):
            return None, []
        text = mw.read_regular_text(self.root / rel, MAX_INDEX_FILE_BYTES)
        if text is None:
            return cached or (None, [])
        res = self._analyse(rel, text)
        out = (res[0], res[1]) if res else (None, _text_bindings(text, self.wanted))
        self.analysis[rel] = out
        return out

    def resolve_file(self, source: str, path: str, root_relative: bool) -> Optional[Dict[str, Any]]:
        """A mentioned path as repository files: {status: ok|ambiguous|missing, files}. None when it is not a code
        file (then it constrains nothing). Readings: from the root, and (unless written with a leading `/`) from the
        instruction file's folder; both existing is ambiguous. Neither: the one file with that path suffix."""
        path = mw._clean_ref_path(path)
        if Path(path).suffix.lower() not in _CODE_EXTS:
            return None
        if mw._unsafe_rel(path):
            return {"status": "missing", "files": [], "mention": path}
        readings = [path]
        parent = Path(source).parent.as_posix()
        if not root_relative and parent not in ("", ".") and not source.startswith(mw.MEMLET_DIR + "/"):
            readings.append(f"{parent}/{path}")
        existing = sorted({r for r in readings if not mw._unsafe_rel(r) and _exists_exact(self.root, r)})
        if len(existing) > 1:
            return {"status": "ambiguous", "files": existing, "mention": path}
        if existing:
            return {"status": "ok", "files": existing, "mention": path}
        if root_relative:
            return {"status": "missing", "files": [], "mention": path}
        tail = "/" + path
        hits = sorted(rel for rel in self.basenames.get(path.rsplit("/", 1)[-1], []) if rel == path or rel.endswith(tail))
        if len(hits) == 1:
            return {"status": "ok", "files": hits, "mention": path}
        return {"status": "ambiguous" if hits else "missing", "files": hits, "mention": path}

    def lookup(self, name: str) -> List[Dict[str, Any]]:
        last = name.rsplit(".", 1)[-1]
        return [t for t in self.targets.get(last, []) if t["symbol"] == name or t["symbol"].endswith("." + name)
                or "." not in name]

    # -- an import of the definition itself ----------------------------------------------------------------------

    @staticmethod
    def _import_is_target(occ: Dict[str, Any], target: Dict[str, Any]) -> bool:
        """Does this import provably name the target's OWN file? Only (a) a relative import / path that normalises to
        that file, or (b) an absolute Python dotted path equal to the target's path from the repository root or to a
        sibling module of the importing file. No suffix matching and no re-export hops: a module that re-exports is
        exempt only for its own direct import. Wildcards, TS path aliases and package specifiers never qualify."""
        if occ.get("kind") != "import" or occ.get("orig") != target["name"]:
            return False
        importer, module, level = occ["path"], occ.get("module") or "", int(occ.get("level") or 0)
        want = target["path"]
        if importer.lower().endswith(".py"):
            if not want.endswith(".py"):
                return False
            parts = [x for x in module.split(".") if x]
            if any(not re.fullmatch(r"[A-Za-z_]\w*", x) for x in parts):
                return False
            if level:
                base = posixpath.dirname(importer)
                for _ in range(level - 1):
                    if not base:
                        return False  # above the repository root
                    base = posixpath.dirname(base)
                stem = posixpath.join(base, *parts) if parts else base
                cands = {stem + ".py", posixpath.join(stem, "__init__.py")} if parts else {posixpath.join(stem, "__init__.py")}
            else:
                if not parts:
                    return False
                tail = "/".join(parts)
                sib = posixpath.join(posixpath.dirname(importer), tail)
                cands = {tail + ".py", tail + "/__init__.py", sib + ".py", sib + "/__init__.py"}
            return want in {c.lstrip("/") for c in cands}
        if not module.startswith((".", "/")) or module.startswith("/"):
            return False  # package specifiers and path aliases: code outside what the files show
        base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), module))
        if base.startswith(".."):
            return False
        cands = {base} | {base + e for e in _TS_JS_EXTS} | {posixpath.join(base, "index" + e) for e in _TS_JS_EXTS}
        stem, ext = posixpath.splitext(base)
        if ext in (".js", ".mjs", ".cjs", ".jsx"):
            cands |= {stem + e for e in (".ts", ".tsx", ".mts", ".cts")}
        return want in cands

    def other_bindings(self, mention: str, target: Dict[str, Any], occs: Optional[List[Dict[str, Any]]] = None
                       ) -> List[Dict[str, Any]]:
        """Bindings of the mentioned name that are not `target` itself or an import of it."""
        last = mention.rsplit(".", 1)[-1]
        out = []
        own = None
        for o in (self.bindings.get(last, []) if occs is None else occs):
            if o.get("kind") == "import" and self._import_is_target(o, target):
                continue
            if "." in mention and o.get("qual") and not (o["qual"] == mention or o["qual"].endswith("." + mention)):
                continue  # a dotted mention rules out bindings known to sit in another scope
            if (own is None and o["path"] == target["path"] and o.get("kind") in ("def", "class", "method")
                    and (o.get("qual") in (None, target["symbol"]))):
                own = o  # the definition itself
                continue
            out.append(o)
        if own is None:
            out.append({"name": last, "path": target["path"], "line": target["line"], "kind": "definition_not_seen",
                        "qual": target["symbol"]})
        return out


# ---------------------------------------------------------------------------------------------------------------
# Rule sources


def _rule_body(line: str) -> str:
    """The rule text `memlet_wiki._extract_rules` takes from one line, before its display limit."""
    bullet = mw._BULLET_RE.match(line)
    return mw.PIN_RE.sub("", bullet.group("text") if bullet else line.strip()).strip()


def hand_written_page(rel: str, text: str) -> bool:
    return (rel.startswith(mw.WIKI_DIR + "/") and rel.endswith(".md") and not mw._is_generated_page(rel, text)
            and rel != mw.DREAM_PROMOTED_PAGE and rel not in mw._MARKED_GENERATED_PAGES)


def _page_body(text: str) -> str:
    return mw._STATUS_BLOCK_RE.sub(mw._blank_lines, mw._PAGE_STATUS_RE.sub(mw._blank_lines, text))


def _read_sources(root: Path) -> Tuple[List[Tuple[str, str]], List[Dict[str, Any]]]:
    """(rel, text) of the files whose rules can carry a sidecar anchor (discovered instruction files, hand-written
    wiki pages) and [{source, reason}] for every such file that a bound or an unreadable file kept out."""
    out: List[Tuple[str, str]] = []
    skipped: List[Dict[str, Any]] = []
    total = 0
    for rel in mw.discover_instruction_files(root):
        if mw.contained_path_problem(root, rel):
            continue
        text = mw.read_regular_text(root / rel, mw._MAX_SOURCE_BYTES)
        if text is None:
            try:
                size = os.lstat(str(root / rel)).st_size
            except OSError:
                size = -1
            skipped.append({"source": rel, "reason": f"over_{mw._MAX_SOURCE_BYTES}_bytes" if size > mw._MAX_SOURCE_BYTES
                            else "unreadable"})
            continue
        total += len(text)
        if total > mw.MAX_SOURCE_TOTAL_BYTES:
            skipped.append({"source": rel, "reason": f"sources_over_{mw.MAX_SOURCE_TOTAL_BYTES}_bytes"})
            continue
        out.append((rel, text))
    pages = mw.iter_wiki_pages(root)
    read = {rel for rel, _t in pages}
    for rel, text in pages:
        if hand_written_page(rel, text):
            out.append((rel, _page_body(text)))
    wiki = root / mw.WIKI_DIR
    if wiki.is_dir() and not mw.managed_path_problem(root, mw.WIKI_DIR, leaf="dir"):
        for n, p in enumerate(sorted(wiki.rglob("*.md"))):
            if n > mw.MAX_WIKI_PAGES * 2:
                skipped.append({"source": mw.WIKI_DIR, "reason": "too_many_pages"})
                break
            rel = mw._rel(root, p)
            if rel not in read and not p.is_symlink():
                skipped.append({"source": rel, "reason": "wiki_page_over_bounds_or_unreadable"})
    return out, skipped


def _source_text(root: Path, rel: str) -> Optional[str]:
    """Current text of one sidecar source, None when it is no longer a rule source Memlet reads."""
    if mw._unsafe_rel(rel) or mw.contained_path_problem(root, rel):
        return None
    if rel.startswith(mw.MEMLET_DIR + "/"):
        text = mw.read_regular_text(root / rel, mw.MAX_WIKI_PAGE_BYTES)
        return _page_body(text) if text is not None and hand_written_page(rel, text) else None
    if not mw.is_instruction_path(root, rel):
        return None
    return mw.read_regular_text(root / rel, mw._MAX_SOURCE_BYTES)


def _atx_heading(line: str) -> Optional[Tuple[int, str]]:
    """(level, canonical title) of a CommonMark ATX heading, else None. A closing `#` run counts only after a space or
    tab (`# C#` is the title `C#`, `# Scope ###` is `Scope`). Direct scan, no backtracking regex."""
    s = line.rstrip("\r\n")
    i = 0
    while i < len(s) and i < 4 and s[i] == " ":
        i += 1
    if i > 3:
        return None
    j = i
    while j < len(s) and s[j] == "#":
        j += 1
    if not 1 <= j - i <= 6 or (j < len(s) and s[j] not in " \t"):
        return None
    content = s[j:].strip(" \t")
    k = len(content)
    while k > 0 and content[k - 1] == "#":
        k -= 1
    if k == 0:
        content = ""
    elif k < len(content) and content[k - 1] in " \t":
        content = content[:k].rstrip(" \t")
    return j - i, re.sub(r"[ \t]+", " ", content)


_SETEXT_RE = re.compile(r" {0,3}(=+|-+)[ \t]*")
_FENCE_OPEN_RE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")


def _heading_chains(lines: List[str]) -> List[Tuple[str, ...]]:
    """For every line, the complete chain of headings it sits under: ATX and Setext headings, outside code fences
    (a fence closes only with the same character, at least as long, as CommonMark requires) and front matter."""
    out: List[Tuple[str, ...]] = []
    stack: List[Tuple[int, str]] = []
    front_end = -1
    if lines and lines[0].strip() == "---":
        for i in range(1, min(len(lines), 60)):
            if lines[i].strip() == "---":
                front_end = i
                break
    fence: Optional[Tuple[str, int]] = None
    prev_para: Optional[str] = None  # the previous line, when it can be the text of a Setext heading

    def push(level: int, title: str) -> None:
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))

    for idx, line in enumerate(lines):
        bare = line.rstrip("\r\n")
        if idx <= front_end:
            out.append(())
            continue
        if fence is not None:
            m = _FENCE_OPEN_RE.fullmatch(bare)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not m.group(2).strip():
                fence = None
            out.append(tuple(t for _l, t in stack))
            prev_para = None
            continue
        m = _FENCE_OPEN_RE.fullmatch(bare)
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = (m.group(1)[0], len(m.group(1)))
            out.append(tuple(t for _l, t in stack))
            prev_para = None
            continue
        atx = _atx_heading(bare)
        if atx:
            push(*atx)
            prev_para = None
        elif prev_para is not None and _SETEXT_RE.fullmatch(bare):
            push(1 if bare.strip()[0] == "=" else 2, prev_para)
            prev_para = None
        else:
            stripped = bare.strip()
            plain = (stripped and not mw._BULLET_RE.match(bare) and not stripped.startswith((">", "<!--", "|"))
                     and len(bare) - len(bare.lstrip(" ")) < 4)
            prev_para = re.sub(r"[ \t]+", " ", stripped) if plain else None
        out.append(tuple(t for _l, t in stack))
    return out


def _section_sha(chain: Iterable[str]) -> str:
    return _sha256(json.dumps(list(chain), ensure_ascii=True))


def _source_rules(rel: str, text: str) -> Dict[str, Any]:
    """Rules of one source as every reader sees them, plus what identity checks need: the heading chain of each rule,
    its full text, and how often each rule text occurs anywhere in the file (independent of the reader's cap)."""
    lines = text.splitlines()
    rules = mw._extract_rules(text, rel)
    chains = _heading_chains(lines)
    for r in rules:
        i = r["line"] - 1
        r["fullText"] = _rule_body(lines[i]) if 0 <= i < len(lines) else r["text"]
        chain = list(chains[i]) if 0 <= i < len(chains) else []
        r["sectionSha256"] = _section_sha(chain)  # identity: the complete canonical titles
        r["sectionPath"] = [t[:MAX_SECTION_TITLE] for t in chain][-MAX_SECTION_DEPTH:]  # display only
    counts: Dict[str, int] = {}
    for line in lines:
        body = _rule_body(line)
        if body:
            h = _sha256(body)
            counts[h] = counts.get(h, 0) + 1
    return {"rules": rules, "counts": counts, "cut": len(rules) >= mw._MAX_RULES_PER_FILE}


def _written_anchor(rule: Dict[str, Any]) -> bool:
    return any(sym for _p, sym in rule.get("refs") or [])


# ---------------------------------------------------------------------------------------------------------------
# Suggestions


def _cand(c: Dict[str, Any]) -> Dict[str, Any]:
    if "anchor" in c:
        return {"anchor": c["anchor"], "line": c["line"], "kind": c["kind"], "test": bool(c.get("test"))}
    return {"anchor": f"{c['path']}:{c['line']}", "line": c["line"], "kind": c.get("kind"), "test": bool(c.get("test")),
            "binding": c.get("qual") or c.get("name")}


def _resolve(index: SymbolIndex, rule: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """(suggestions, ambiguous, unresolved count) for one rule (see the module docstring for the rules)."""
    mentions = rule["mentions"]
    infos = [(f, index.resolve_file(rule["source"], f["path"], f["rootRelative"])) for f in mentions["files"]
             if not f["example"]]
    infos = [(f, i) for f, i in infos if i is not None]
    named = sorted({rel for _f, i in infos if i["status"] == "ok" for rel in i["files"]})
    unclear = [i for _f, i in infos if i["status"] != "ok"]
    sugg, amb, unresolved = [], [], 0
    for ident in mentions["idents"]:
        name = ident["name"]
        if ident["example"]:
            amb.append(_ambiguous(rule, name, "mention_in_example_or_quotation", index.lookup(name)))
            continue
        if infos:  # the rule names code files: only those may hold the symbol
            if unclear:
                reason = ("file_mention_has_several_interpretations" if any(i["status"] == "ambiguous" for i in unclear)
                          else "named_file_not_found")
                amb.append(_ambiguous(rule, name, reason, [{"anchor": f, "line": 0, "kind": "file"} for i in unclear
                                                            for f in i["files"]] or index.lookup(name)))
                continue
            in_targets = []
            for rel in named:
                targets, _occ = index.file_analysis(rel)
                for t in targets or []:
                    if t["symbol"] == name or t["symbol"].endswith("." + name):
                        in_targets.append(dict(t, path=rel, anchor=f"{rel}::{t['symbol']}", test=_is_test_path(rel)))
            if not in_targets:
                amb.append(_ambiguous(rule, name, "named_file_does_not_define_symbol", index.lookup(name)))
                continue
            if len(in_targets) > 1:
                amb.append(_ambiguous(rule, name, "several_matches_in_named_file", in_targets))
                continue
            t = in_targets[0]
            _targets, occ = index.file_analysis(t["path"])
            others = index.other_bindings(name, t, [dict(o, path=t["path"]) for o in occ if o["name"] == name.rsplit(".", 1)[-1]])
            if others:
                amb.append(_ambiguous(rule, name, "several_bindings_in_named_file", [t] + others))
                continue
            sugg.append(_suggestion(rule, t, FILE_SYMBOL, name,
                                    f"the rule names `{t['path']}` and `{name}`, defined there once at line {t['line']} ({t['kind']})"))
            continue
        cands = sorted(index.lookup(name), key=lambda e: (e["path"], e["line"], e["symbol"]))
        if not cands:
            if index.bindings.get(name.rsplit(".", 1)[-1]):
                amb.append(_ambiguous(rule, name, "bound_only_where_memlet_cannot_anchor",
                                      index.bindings[name.rsplit(".", 1)[-1]]))
            else:
                unresolved += 1
            continue
        if len(cands) > 1:
            reason = "defined_in_tests_and_source" if any(c["test"] for c in cands) and not all(c["test"] for c in cands) \
                else "multiple_definitions"
            amb.append(_ambiguous(rule, name, reason, cands))
            continue
        if not _distinctive(name, ident["call"]):
            amb.append(_ambiguous(rule, name, "name_too_generic_to_anchor_without_a_file", cands))
            continue
        if not index.complete:
            amb.append(_ambiguous(rule, name, "scan_incomplete_single_definition_not_established", cands))
            continue
        t = cands[0]
        others = index.other_bindings(name, t)
        if others:
            amb.append(_ambiguous(rule, name, "other_bindings_of_the_name", [t] + others))
            continue
        ns = index.not_scanned_summary()
        reason = (f"one definition of `{name}` found ({t['path']}:{t['line']}, {t['kind']}) in "
                  f"{index.stats['codeFiles']} indexed file(s); no other binding-like occurrence in "
                  f"{index.stats['skippedCodeFiles']} text-scanned file(s); not scanned: "
                  + (", ".join(ns["paths"]) + (f" (+{ns['count'] - len(ns['paths'])} more)" if ns["count"] > len(ns["paths"]) else "")
                     if ns["count"] else "nothing")
                  + ("; that definition is in a test file" if t["test"] else "") + ". A proposal: check it before accepting.")
        sg = _suggestion(rule, t, SINGLE_DEFINITION_FOUND, name, reason)
        sg["notScanned"] = ns
        sugg.append(sg)
    return sugg, amb, unresolved


def _suggestion(rule, target, confidence, mention, reason) -> Dict[str, Any]:
    anchor = f"{target['path']}::{target['symbol']}"
    return {"id": suggestion_id(rule["source"], rule["ruleSha256"], anchor, rule["sectionSha256"]),
            "source": rule["source"], "line": rule["line"], "sectionPath": list(rule["sectionPath"]),
            "sectionSha256": rule["sectionSha256"],
            "ruleSha256": rule["ruleSha256"], "rule": _clip(_mask(rule["fullText"])), "anchor": anchor,
            "confidence": confidence, "reason": reason, "mention": mention,
            "target": {"path": target["path"], "symbol": target["symbol"], "line": target["line"],
                       "kind": target["kind"], "test": bool(target.get("test"))}}


def _ambiguous(rule, mention, reason, cands) -> Dict[str, Any]:
    return {"source": rule["source"], "line": rule["line"], "ruleSha256": rule["ruleSha256"],
            "rule": _clip(_mask(rule["fullText"])), "mention": mention, "reason": reason, "candidateCount": len(cands),
            "candidates": [_cand(c) for c in cands[:MAX_CANDIDATES_SHOWN]]}


def _suggest(root: Path, limit: Optional[int]) -> Dict[str, Any]:
    sources, skipped = _read_sources(root)
    accepted = {e["id"] for e in sidecar_anchors(root)}
    rules: List[Dict[str, Any]] = []
    truncation: List[str] = [f"source_skipped:{s['source']}:{s['reason']}" for s in skipped]
    counts: Dict[str, Dict[str, int]] = {}
    for rel, text in sources:
        info = _source_rules(rel, text)
        counts[rel] = info["counts"]
        if info["cut"]:
            truncation.append(f"rules_per_file>={mw._MAX_RULES_PER_FILE}:{rel}")
        for r in info["rules"]:
            if len(rules) >= MAX_RULES:
                truncation.append(f"rules>{MAX_RULES}")
                break
            r["mentions"] = rule_mentions(r["fullText"])
            rules.append(r)
        if len(rules) >= MAX_RULES:
            break
    wanted = {i["name"].rsplit(".", 1)[-1] for r in rules if not _written_anchor(r) for i in r["mentions"]["idents"]}
    index = SymbolIndex(root, wanted)
    sugg: List[Dict[str, Any]] = []
    amb: List[Dict[str, Any]] = []
    unresolved = already = written = 0
    for r in rules:
        if _written_anchor(r):
            written += 1
            continue
        if not r["mentions"]["complete"]:
            truncation.append(f"rule_not_scanned_completely:{r['source']}:{r['line']}")  # too long / too many mentions
            if r["mentions"]["idents"]:
                amb.append(_ambiguous(r, r["mentions"]["idents"][0]["name"], "rule_too_long_to_scan_completely", []))
            continue
        s, a, u = _resolve(index, r)
        unresolved += u
        if s and counts[r["source"]].get(r["ruleSha256"], 0) > 1:
            amb.append(dict(_ambiguous(r, s[0]["mention"], "rule_text_repeated_in_file", []),
                            candidates=[{"anchor": x["anchor"], "line": x["target"]["line"], "kind": x["target"]["kind"],
                                         "test": x["target"]["test"]} for x in s]))
            s = []
        seen = set()
        for x in s:
            if x["anchor"] in seen:
                continue
            seen.add(x["anchor"])
            if x["id"] in accepted:
                already += 1
                continue
            sugg.append(x)
        amb.extend(a)
    sugg.sort(key=lambda x: (x["source"], x["line"], x["anchor"]))
    amb.sort(key=lambda x: (x["source"], x["line"], x["mention"]))
    truncation += index.truncated + index.incomplete
    over_limit = limit is not None and (len(sugg) > limit or len(amb) > limit)
    if over_limit:
        truncation.append("limit")
    scanned = dict(index.stats, rules=len(rules), rulesWithWrittenAnchors=written, sources=len(sources),
                   unresolvedMentions=unresolved, skippedSources=skipped,
                   indexIncomplete=index.truncated + index.incomplete)
    by_level = {c: sum(1 for x in sugg if x["confidence"] == c) for c in CONFIDENCES}
    return {"ok": True, "exitCode": 0, "command": "anchors suggest", "repoPath": str(root),
            "countsByConfidence": by_level, "notScanned": index.not_scanned_summary(),
            "suggestions": sugg if limit is None else sugg[:limit], "ambiguous": amb if limit is None else amb[:limit],
            "suggestionCount": len(sugg), "ambiguousCount": len(amb), "alreadyAccepted": already, "scanned": scanned,
            "truncated": bool(truncation), "truncation": truncation}


def suggest_anchors(repo_path=".", *, limit=200) -> Dict[str, Any]:
    """Suggested `path::symbol` anchors for rules that mention code without writing an anchor. Read-only, no model
    call. {ok, suggestions:[{id, source, line, sectionPath, ruleSha256, rule, anchor, confidence, reason, mention,
    target}], ambiguous:[{source, line, ruleSha256, rule, mention, reason, candidates}], scanned, truncated,
    truncation, ...}. `truncated` is True whenever any bound was hit or something relevant could not be read."""
    root = mw._resolve_root(repo_path)
    res = _suggest(root, max(0, int(limit)) if limit is not None else None)
    res["formatted"] = format_suggestions(res)
    return res


def count_suggestions(root: Path) -> int:
    """How many rules could be anchored now (for `memlet init`'s one-line hint)."""
    return len({(s["source"], s["line"]) for s in _suggest(root, None)["suggestions"]})


def format_suggestions(res: Dict[str, Any]) -> str:
    sc = res.get("scanned") or {}
    levels = res.get("countsByConfidence") or {}
    ns = res.get("notScanned") or {}
    lines = ["## Memlet anchor suggestions", "",
             "These are proposals for a human to check, not verified facts.",
             f"- Scanned {sc.get('rules', 0)} rule(s) in {sc.get('sources', 0)} file(s); {sc.get('codeFiles', 0)} code "
             f"file(s) indexed, {sc.get('skippedCodeFiles', 0)} scanned as text",
             f"- Not scanned: {ns.get('count', 0)} folder(s)" + (": " + ", ".join(ns.get("paths") or []) if ns.get("count") else ""),
             f"- {res.get('suggestionCount', 0)} suggestion(s) · {res.get('ambiguousCount', 0)} ambiguous (not suggested)"
             f" · {res.get('alreadyAccepted', 0)} already accepted"]
    if res.get("truncated"):
        lines.append("- INCOMPLETE: " + ", ".join((res.get("truncation") or [])[:8])
                     + ("…" if len(res.get("truncation") or []) > 8 else "")
                     + " — `accept --all-unique` is refused until this is resolved")
    if res.get("suggestions"):
        lines += ["", "### Suggestions", ""]
        for s in res["suggestions"]:
            t = s["target"]
            lines.append(f"- `{s['id']}` ({s['confidence']}) `{s['source']}:{s['line']}`: {s['rule']}")
            lines.append(f"  → `{s['anchor']}` at `{t['path']}:{t['line']}` ({t['kind']}{', test file' if t['test'] else ''}) — {s['reason']}")
    if res.get("ambiguous"):
        lines += ["", "### Ambiguous (never guessed)", ""]
        for a in res["ambiguous"]:
            cands = ", ".join(f"`{c['anchor']}` (line {c['line']}{', ' + str(c['kind']) if c.get('kind') else ''}"
                              f"{', test' if c.get('test') else ''})" for c in a["candidates"])
            more = a.get("candidateCount", 0) - len(a["candidates"])
            lines.append(f"- `{a['source']}:{a['line']}` `{a['mention']}`: {a['reason']}"
                         + (f" — candidates: {cands}" if cands else "") + (f" (+{more} more)" if more > 0 else ""))
    lines += ["", "### Accepting", "",
              f"- {levels.get(FILE_SYMBOL, 0)} {FILE_SYMBOL}: `memlet admin anchors accept --all-unique` (accepts only these)",
              f"- {levels.get(SINGLE_DEFINITION_FOUND, 0)} {SINGLE_DEFINITION_FOUND}: by id only, after checking each: "
              "`memlet admin anchors accept ID…`",
              "- Nothing is changed by this command. Accepting stores the anchor in `.memlet/config.json`; your files are "
              "never edited."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------------------------
# Sidecar entries


def _valid_hex(v: Any, required: bool = False) -> bool:
    if v is None:
        return not required
    return isinstance(v, str) and bool(_HEX_RE.fullmatch(v))


def _valid_rel(rel: Any) -> bool:
    return isinstance(rel, str) and 0 < len(rel) <= 1024 and mw._rel_parts(rel) is not None and not mw._unsafe_rel(rel)


def _validate_entry(e: Any) -> Optional[str]:
    """None when `e` is a well-formed sidecar entry, else why not."""
    if not isinstance(e, dict):
        return "entry_not_an_object"
    if "sectionPath" not in e or "sectionSha256" not in e:
        return "older_entry_format_accept_again"
    if not (isinstance(e.get("sectionSha256"), str) and re.fullmatch(r"[0-9a-f]{64}", e["sectionSha256"])):
        return "invalid_sectionSha256"
    sp = e.get("sectionPath")
    if not (isinstance(sp, list) and len(sp) <= MAX_SECTION_DEPTH
            and all(isinstance(t, str) and len(t) <= MAX_SECTION_TITLE for t in sp)):
        return "invalid_sectionPath"
    if not _valid_rel(e.get("source")):
        return "invalid_source"
    if not (isinstance(e.get("ruleSha256"), str) and re.fullmatch(r"[0-9a-f]{64}", e["ruleSha256"])):
        return "invalid_ruleSha256"
    anchor = e.get("anchor")
    if not (isinstance(anchor, str) and "::" in anchor):
        return "invalid_anchor"
    path, symbol = anchor.rsplit("::", 1)
    if not _valid_rel(path) or path.split("/")[0] == mw.MEMLET_DIR or not _SYMBOL_RE.fullmatch(symbol):
        return "invalid_anchor"
    pin = e.get("pin")
    if not isinstance(pin, dict) or not _valid_hex(pin.get("ast"), True) or not all(
            _valid_hex(pin.get(k)) for k in ("sig", "req", "types", "shape")):
        return "invalid_pin"
    basis = pin.get("basis")
    if basis is not None and not (isinstance(basis, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", basis)):
        return "invalid_pin"
    if e.get("id") != suggestion_id(e["source"], e["ruleSha256"], anchor, e["sectionSha256"]):
        return "id_does_not_match_entry"
    line = e.get("line")
    if not (isinstance(line, int) and not isinstance(line, bool) and line > 0):
        return "invalid_line"
    return None


def entry_digest(e: Dict[str, Any]) -> str:
    """What the ledger records for an accepted entry: every stored field (the recorded line included, so a hand edit
    of it is detected rather than reported as if accepted)."""
    pin = {k: e["pin"].get(k) for k in ("ast", "sig", "req", "basis", "types", "shape")}
    return _sha256(json.dumps({"id": e["id"], "source": e["source"], "sectionPath": list(e["sectionPath"]),
                               "sectionSha256": e["sectionSha256"],
                               "line": e["line"], "ruleSha256": e["ruleSha256"], "anchor": e["anchor"], "pin": pin},
                              sort_keys=True))


def _ledger_records(root: Path) -> Dict[str, Tuple[str, str]]:
    """{entry id: (sha256, event)} of the LATEST ledger event per sidecar entry (a later drop revokes an accept);
    {} without a readable ledger."""
    if mw.first_managed_problem(root, mw._LEDGER_FILES) or not (root / mw.LEDGER_PATH).is_file():
        return {}
    try:
        with contextlib.closing(mw.connect_managed_sqlite(root, mw.LEDGER_PATH, readonly=True)) as conn:
            rows = conn.execute(f"SELECT entry_id, entry_sha256, event_type FROM {LEDGER_TABLE} WHERE id IN "
                                f"(SELECT MAX(id) FROM {LEDGER_TABLE} GROUP BY entry_id)").fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return {}
    return {str(i): (str(s), str(ev)) for i, s, ev in rows}


def _raw_entries(config: Optional[Dict[str, Any]]) -> Tuple[List[Any], Optional[str]]:
    """(every stored entry, problem). Never truncated: callers that write keep the complete list."""
    raw = config.get(SIDECAR_KEY) if isinstance(config, dict) else None
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return [], f"{SIDECAR_KEY}_must_be_a_list"
    return raw, (f"{SIDECAR_KEY}_over_{MAX_SIDECAR_ENTRIES}_entries" if len(raw) > MAX_SIDECAR_ENTRIES else None)


def _checked_entries(root: Path, config: Optional[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(entries the ledger records, rejected [{id, anchor, source, reason}]). Over the entry cap every entry is
    rejected (fail closed) rather than reading a prefix."""
    raw, problem = _raw_entries(config)
    if problem:
        return [], [{"id": e.get("id") if isinstance(e, dict) else None,
                     "anchor": e.get("anchor") if isinstance(e, dict) else None,
                     "source": e.get("source") if isinstance(e, dict) else None, "reason": problem}
                    for e in raw[:MAX_SIDECAR_ENTRIES]] or [{"id": None, "anchor": None, "source": None, "reason": problem}]
    if not raw:
        return [], []
    records = _ledger_records(root)
    good, rejected, seen = [], [], set()
    for e in raw:
        why = _validate_entry(e)
        if why is None and e["id"] in seen:
            why = "duplicate_entry"
        if why is None:
            rec = records.get(e["id"])
            if rec is None or rec[1] != "accept" or rec[0] != entry_digest(e):
                why = "not_recorded_by_accept"  # edited, copied without its ledger row, or dropped since
        if why:
            rejected.append({"id": e.get("id") if isinstance(e, dict) else None,
                             "anchor": e.get("anchor") if isinstance(e, dict) else None,
                             "source": e.get("source") if isinstance(e, dict) else None, "reason": why})
            continue
        seen.add(e["id"])
        good.append({"id": e["id"], "source": e["source"], "sectionPath": list(e["sectionPath"]),
                     "sectionSha256": e["sectionSha256"], "line": e["line"],
                     "ruleSha256": e["ruleSha256"], "anchor": e["anchor"],
                     "pin": {k: e["pin"].get(k) for k in ("ast", "sig", "req", "basis", "types", "shape")}})
    return good, rejected


def sidecar_anchors(root) -> List[Dict[str, Any]]:
    """Accepted sidecar entries `{id, source, sectionPath, line, ruleSha256, anchor, pin}` that are well-formed and
    whose latest ledger event is their accept (whether or not their rule still matches; see `sidecar_index`)."""
    root = Path(root)
    config, err = mw.read_config(root)
    if err or not isinstance(config, dict):
        return []
    return _checked_entries(root, config)[0]


_EMPTY_INDEX = {"attached": [], "detached": [], "byLocation": {}, "pins": {}}


def sidecar_index(root: Path, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Where each sidecar entry attaches now, computed from the current file contents on every call (no cache: stat
    metadata never stands in for the text). {attached: [entry + currentLine, lineShifted], detached: [entry + state +
    reason], byLocation: {(source, line): [entries]}, pins: {anchor: pin}}."""
    root = Path(root)
    if config is None:
        config, err = mw.read_config(root)
        if err:
            return dict(_EMPTY_INDEX)
    raw, _p = _raw_entries(config)
    if not raw:
        return dict(_EMPTY_INDEX)
    entries, rejected = _checked_entries(root, config)
    detached = [dict(r, state="rejected") for r in rejected]
    attached = []
    by_source: Dict[str, Optional[Dict[str, Any]]] = {}
    for e in entries:
        if e["source"] not in by_source:
            text = _source_text(root, e["source"])
            by_source[e["source"]] = None if text is None else _source_rules(e["source"], text)
        info = by_source[e["source"]]
        reason, hit = None, None
        if info is None:
            reason = "source_missing_unreadable_or_not_a_rule_source"
        else:
            n = info["counts"].get(e["ruleSha256"], 0)
            hits = [r for r in info["rules"] if r["ruleSha256"] == e["ruleSha256"]]
            if n == 0:
                reason = "rule_text_changed_or_removed"
            elif n > 1:
                reason = "rule_text_repeated_in_file"
            elif not hits:
                reason = "rule_not_read_as_a_rule"  # e.g. beyond the reader's per-file limit, or no longer a list item
            else:
                hit = hits[0]
                if hit["sectionSha256"] != e["sectionSha256"]:
                    reason = "rule_moved_to_another_section"
                elif _written_anchor(hit):
                    reason = "rule_has_written_anchor"
                elif not mentions_symbol(hit["fullText"], e["anchor"].rsplit("::", 1)[1]):
                    reason = "rule_does_not_mention_symbol"
        if reason:
            detached.append(dict(e, state="detached", reason=reason))
            continue
        attached.append(dict(e, currentLine=hit["line"], lineShifted=hit["line"] != e["line"]))
    by_loc: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    pins: Dict[str, Dict[str, Any]] = {}
    for a in attached:
        by_loc.setdefault((a["source"], a["currentLine"]), []).append(a)
        pins.setdefault(a["anchor"], dict(a["pin"]))
    return {"attached": attached, "detached": detached, "byLocation": by_loc, "pins": pins}


def _attach(root: Path, r: Dict[str, Any], entries: List[Dict[str, Any]]) -> None:
    anchors = [e["anchor"] for e in entries]
    paths = {a.rsplit("::", 1)[0] for a in anchors}
    # the file mention that made a file+symbol anchor becomes that anchor, as if `path::symbol` had been written
    refs = [(p, s) for p, s in r["refs"] if s or mw._resolve_ref_path(root, r["source"], p) not in paths]
    for a in anchors:
        ref = tuple(a.rsplit("::", 1))
        if ref not in refs:
            refs.append(ref)
    r["refs"] = refs
    r["suggestedAnchors"] = sorted(set(r.get("suggestedAnchors") or []) | set(anchors))
    ids = dict(r.get("sidecarIds") or {})
    for e in entries:
        ids[e["anchor"]] = e["id"]
    r["sidecarIds"] = dict(sorted(ids.items()))


def _written_anchor_refs(r: Dict[str, Any]) -> bool:
    suggested = set(r.get("suggestedAnchors") or [])
    return any(s and f"{p}::{s}" not in suggested for p, s in r.get("refs") or [])


def attach_sidecar_refs(root: Path, rules: Iterable[Dict[str, Any]]) -> None:
    """Give every rule that an attached sidecar entry belongs to (same source, current line and rule SHA-256, no
    written `::` anchor) that entry's anchor as a reference. Idempotent; rules without `ruleSha256` are left alone.
    Sets `suggestedAnchors` (anchors) and `sidecarIds` ({anchor: acceptance id}) on the rule."""
    rules = list(rules)
    if not rules:
        return
    idx = sidecar_index(Path(root))
    if not idx["attached"]:
        return
    for r in rules:
        entries = idx["byLocation"].get((r.get("source"), r.get("line")))
        if not entries or r.get("ruleSha256") != entries[0]["ruleSha256"] or _written_anchor_refs(r):
            continue
        _attach(Path(root), r, entries)


def attach_sidecar_to_span(root: Path, rule: Dict[str, Any], source: str, first: int, last: int) -> None:
    """For a quoted span `source:first-last` (recall): attach the sidecar anchors of the rules quoted in it, only when
    the quoted rule's own digest equals the accepted one (a quote of changed or masked text never attaches)."""
    idx = sidecar_index(Path(root))
    if not idx["attached"] or _written_anchor_refs(rule):
        return
    for qr in mw._extract_rules(rule.get("text") or "", source):
        line = first + qr["line"] - 1
        if line > last:
            break
        entries = idx["byLocation"].get((source, line))
        if entries and entries[0]["ruleSha256"] == qr["ruleSha256"] and not _written_anchor(qr):
            _attach(Path(root), rule, entries)


# ---------------------------------------------------------------------------------------------------------------
# accept / drop / list


def _refusal(command: str, root: Path, error: str, dry_run: bool, **extra: Any) -> Dict[str, Any]:
    res = {"ok": False, "exitCode": 2, "command": command, "repoPath": str(root), "dryRun": bool(dry_run),
           "error": error, "actions": []}
    res.update(extra)
    res["formatted"] = f"## Memlet {command}\n\n- ERROR: {error}\n"
    return res


_STATE_PATHS = (mw.MEMLET_DIR, mw.CONFIG_PATH) + mw._LEDGER_FILES


def _guard(command: str, root: Path, dry_run: bool, fn, **extra: Any) -> Dict[str, Any]:
    problem = mw.first_managed_problem(root, _STATE_PATHS)
    if problem:
        return _refusal(command, root, f"{problem}; nothing was written", dry_run, **extra)
    try:
        if dry_run:
            return fn()
        with mw.memlet_write_lock(root):  # the same lock as init / wiki / import / Dream promotion / wiki accept
            return fn()
    except mw.ConcurrentChangeError as exc:
        return _refusal(command, root, f"{exc}; nothing was written. Run the command again (it re-reads the "
                        "current .memlet/config.json)", dry_run, concurrentChange=True, **extra)  # see the threat model
    except mw.UnsafePathError as exc:
        return _refusal(command, root, f"{exc}; nothing was written", dry_run, **extra)
    except (OSError, ValueError, sqlite3.Error, TimeoutError) as exc:
        return _refusal(command, root, f"{command} failed before anything was written: {exc}", dry_run, **extra)


def _read_config_bound(root: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str], str]:
    """(config, error, if_match tag of the exact bytes read)."""
    problem = mw.managed_path_problem(root, mw.CONFIG_PATH)
    if problem:
        return None, problem, ""
    p = root / mw.CONFIG_PATH
    if not os.path.lexists(str(p)):
        return None, None, mw.content_tag(None)
    data = mw.read_regular_bytes(p, mw.MAX_CONFIG_BYTES)
    if data is None:
        try:
            size = os.lstat(str(p)).st_size
        except OSError:
            size = -1
        if size > mw.MAX_CONFIG_BYTES:
            return None, (f".memlet/config.json is {size} bytes, over the {mw.MAX_CONFIG_BYTES}-byte limit Memlet reads, "
                          "so Memlet cannot change it safely; nothing was written. Recover by editing it by hand: "
                          "shrink or remove the unrelated key that grew"), ""
        return None, ".memlet/config.json is unreadable", ""
    try:
        cfg = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        return None, f".memlet/config.json is unreadable: {exc}", ""
    if not isinstance(cfg, dict):
        return None, ".memlet/config.json is not an object", ""
    return cfg, None, mw.content_tag(data)


def _config_text(config: Dict[str, Any]) -> str:
    return json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _too_big(text: str) -> Optional[str]:
    """An error when the serialised config would exceed what every reader accepts (checked before any write)."""
    size = len(text.encode("utf-8"))
    if size > mw.MAX_CONFIG_BYTES:
        return (f"the new .memlet/config.json would be {size} bytes, over the {mw.MAX_CONFIG_BYTES}-byte limit every "
                "Memlet reader accepts; nothing was written and the old file is intact. Shrink the largest unrelated key "
                "by hand, or drop entries, then run the command again")
    return None


def _ledger_events(root: Path, events: List[Tuple[str, str, str]], snaps: List[Dict[str, Any]]) -> None:
    now = mw._utc_now()
    with contextlib.closing(mw.connect_managed_sqlite(root, mw.LEDGER_PATH)) as conn:
        with conn:
            mw._ledger_ready(conn)
            conn.execute(_LEDGER_SCHEMA)
            for entry_id, sha, event in events:
                conn.execute(f"INSERT INTO {LEDGER_TABLE} (entry_id, entry_sha256, event_type, recorded_at) VALUES (?, ?, ?, ?)",
                             (entry_id, sha, event, now))
            if snaps:
                mw._sync_snapshots(conn, snaps, now)


def _pins_elsewhere(root: Path) -> Dict[str, Dict[str, Any]]:
    """Baselines of written anchors (config `pinnedAnchors` and trusted wiki page pins), without sidecar pins."""
    pins = {}
    for key, cp in mw._load_config_pins(root).items():
        if isinstance(cp, dict) and cp.get("astHash"):
            pins[key] = {"ast": cp["astHash"], "sig": cp.get("sigHash"), "req": cp.get("reqSigHash"),
                         "basis": cp.get("hashBasis"), "types": cp.get("annotationHash"), "shape": cp.get("parameterShapeHash")}
    for rel, text in mw.trusted_wiki_pages(root):
        for a in mw._extract_anchors(text, rel):
            if a["pin"]:
                pins[f"{a['path']}::{a['symbol']}"] = a["pin"]
    return pins


def accept_suggestions(repo_path=".", ids=(), *, all_unique=False, dry_run=False) -> Dict[str, Any]:
    """Accept suggestions as sidecar anchors: by id (any level), or with `all_unique` every current `file+symbol`
    suggestion (never a `single-definition-found` one). A pinning event, like `init`. Suggestions are recomputed under the write lock, so an id always refers to the current rule text.
    Accepting the id of an already attached entry re-pins it to the current code (after the human reviewed it).
    Refused without writing: an existing or resulting state over MAX_SIDECAR_ENTRIES, `all_unique` on an incomplete
    scan, a config.json changed by another program meanwhile. Never edits a file outside `.memlet/`."""
    root = mw._resolve_root(repo_path)
    ids = [str(i).strip() for i in (ids or []) if str(i).strip()]
    if not ids and not all_unique:
        return _refusal("anchors accept", root, "give suggestion ids or --all-unique", dry_run, accepted=[])
    return _guard("anchors accept", root, dry_run, lambda: _accept_locked(root, ids, bool(all_unique), bool(dry_run)),
                  accepted=[])


def _accept_locked(root: Path, ids: List[str], all_unique: bool, dry_run: bool) -> Dict[str, Any]:
    config, err, tag = _read_config_bound(root)
    if err:
        return _refusal("anchors accept", root, f"{err}; nothing was written", dry_run, accepted=[])
    raw, problem = _raw_entries(config)
    if problem:
        return _refusal("anchors accept", root, f"`{SIDECAR_KEY}` in .memlet/config.json: {problem}; nothing was "
                        "written (drop entries or fix the list by hand)", dry_run, accepted=[])
    found = _suggest(root, None)
    if all_unique and found["truncated"]:
        return _refusal("anchors accept", root, "the scan was incomplete (" + ", ".join(found["truncation"][:6])
                        + "); --all-unique would not mean all. Accept suggestions by id instead; nothing was written",
                        dry_run, accepted=[], truncation=found["truncation"])
    current = {s["id"]: s for s in found["suggestions"]}
    attached = {e["id"]: e for e in sidecar_index(root, config)["attached"]}
    # bulk acceptance takes only file+symbol; a single-definition-found proposal needs its id (a human look)
    selected_new = [i for i, s in current.items() if s["confidence"] == FILE_SYMBOL] if all_unique else []
    not_bulk = sorted(i for i, s in current.items() if s["confidence"] != FILE_SYMBOL) if all_unique else []
    repins, unknown = [], []
    for i in ids:
        if i in current:
            if i not in selected_new:
                selected_new.append(i)
        elif i in attached:
            repins.append(i)
        else:
            unknown.append(i)
    elsewhere = _pins_elsewhere(root)
    new_entries, refused, accepted, repinned, reused = {}, [], [], [], []
    for i in sorted(selected_new) + sorted(repins):
        s = current.get(i) or attached[i]
        path, symbol = s["anchor"].rsplit("::", 1)
        if s["anchor"] in elsewhere:
            if i in repins:
                refused.append({"id": i, "anchor": s["anchor"], "reason": "anchor_is_pinned_by_a_written_anchor; re-pin "
                                "it there (memlet init --force or memlet accept)"})
                continue
            pin = dict(elsewhere[s["anchor"]])
            reused.append(i)
        else:
            fp = mw._current_fingerprint(root, path, symbol)
            if fp.get("resolution") != "exact" or not fp.get("ast"):
                refused.append({"id": i, "anchor": s["anchor"], "reason": fp.get("reason") or fp.get("resolution") or "unresolvable"})
                continue
            pin = {"ast": fp["ast"], "sig": fp.get("sig") or None, "req": fp.get("req"), "basis": fp.get("basis"),
                   "types": fp.get("types"), "shape": fp.get("shape")}
        new_entries[i] = {"id": i, "source": s["source"], "sectionPath": list(s["sectionPath"]),
                          "sectionSha256": s["sectionSha256"],
                          "line": s.get("currentLine") or s["line"], "ruleSha256": s["ruleSha256"],
                          "anchor": s["anchor"], "pin": pin}
        (repinned if i in repins else accepted).append(i)
    kept = [e for e in raw if not (isinstance(e, dict) and e.get("id") in new_entries)]
    if len(kept) + len(new_entries) > MAX_SIDECAR_ENTRIES:
        return _refusal("anchors accept", root, f"accepting {len(accepted)} more would store {len(kept) + len(new_entries)}"
                        f" entries, over the limit of {MAX_SIDECAR_ENTRIES}; nothing was written (accept fewer ids or "
                        "drop entries first)", dry_run, accepted=[])
    merged = sorted(kept + [new_entries[i] for i in sorted(new_entries)],
                    key=lambda e: str(e.get("id")) if isinstance(e, dict) else "")
    actions: List[Dict[str, str]] = []
    res = {"command": "anchors accept", "repoPath": str(root), "dryRun": dry_run, "accepted": accepted,
           "notAcceptedInBulk": not_bulk,
           "repinned": repinned, "reusedExistingPin": sorted(reused), "unknown": unknown, "refused": refused,
           "entries": [new_entries[i] for i in sorted(new_entries)], "actions": actions,
           "baselineNotice": "Accepting records the current code as the baseline of each rule; it does not verify "
                             "that the rule means that code.", "integrityNotice": INTEGRITY_NOTICE}
    if new_entries:
        if config is None:
            config = mw._base_config(mw._git_head(root), mw.discover_instruction_files(root))
        config = dict(config)
        config[SIDECAR_KEY] = merged
        big = _too_big(_config_text(config))
        if big:
            return _refusal("anchors accept", root, big, dry_run, accepted=[])
        if dry_run:
            actions += [{"op": "would-update", "path": mw.CONFIG_PATH}, {"op": "would-update", "path": mw.LEDGER_PATH}]
        else:
            # config first (bound to the bytes read), then the ledger: an entry without its ledger row is rejected by
            # every reader, so a failure in between can never leave an unrecorded acceptance in effect
            mw.write_managed_text(root, mw.CONFIG_PATH, _config_text(config), if_match=tag)
            actions.append({"op": "update", "path": mw.CONFIG_PATH})
            snaps = [mw.classify_anchor(root, {"path": e["anchor"].rsplit("::", 1)[0], "symbol": e["anchor"].rsplit("::", 1)[1],
                                               "pin": e["pin"]}) for e in new_entries.values()]
            records_before = _ledger_records(root)
            try:
                _ledger_events(root, [(i, entry_digest(e), "accept") for i, e in sorted(new_entries.items())], snaps)
            except (sqlite3.Error, OSError, ValueError) as exc:
                states = {}
                for i, e in sorted(new_entries.items()):
                    rec = records_before.get(i)
                    states[i] = ("unchanged from a recorded acceptance: still in effect"
                                 if rec is not None and rec[1] == "accept" and rec[0] == entry_digest(e)
                                 else "new or changed: not in effect until accepted again")
                pending = [i for i, st in states.items() if st.startswith("new")]
                res.update(ok=False, exitCode=1, partialWrite=True, written=[mw.CONFIG_PATH], notWritten=[mw.LEDGER_PATH],
                           entryStates=states,
                           error=(f"the ledger could not be written ({exc}) after .memlet/config.json was updated. Per "
                                  "entry: " + "; ".join(f"{i}: {st}" for i, st in states.items()) + "."
                                  + (" To recover, fix the ledger and run `memlet admin anchors accept " + " ".join(pending)
                                     + "` again, or remove them with `memlet admin anchors drop " + " ".join(pending) + "`."
                                     if pending else "")))
                res["formatted"] = f"## Memlet anchors accept\n\n- ERROR: {res['error']}\n- written: `{mw.CONFIG_PATH}`\n"
                return res
            actions.append({"op": "update", "path": mw.LEDGER_PATH})
    ok = not unknown and not refused
    res.update(ok=ok, exitCode=0 if ok else 1)
    if not ok:
        res["error"] = "; ".join(filter(None, ["unknown id(s): " + ", ".join(unknown) if unknown else "",
                                                "refused: " + ", ".join(f"{r['id']} ({r['reason']})" for r in refused) if refused else ""]))
    verb = "would accept" if dry_run else "accepted"
    lines = [f"## Memlet anchors accept{' (dry run)' if dry_run else ''}", ""]
    lines += [f"- {verb} `{e['id']}`: `{e['source']}:{e['line']}` → `{e['anchor']}` (pin ast={e['pin']['ast']}, basis={e['pin'].get('basis')})"
              for e in (new_entries[i] for i in accepted)]
    lines += [f"- {'would re-pin' if dry_run else 're-pinned'} `{i}` → `{new_entries[i]['anchor']}`" for i in repinned]
    lines += [f"- UNKNOWN `{i}` (not a current suggestion or attached entry; run `memlet admin anchors suggest`)" for i in unknown]
    lines += [f"- REFUSED `{r['id']}` `{r['anchor']}`: {r['reason']}" for r in refused]
    if not_bulk:
        lines.append(f"- {len(not_bulk)} {SINGLE_DEFINITION_FOUND} suggestion(s) were not accepted: --all-unique takes "
                     f"only {FILE_SYMBOL}; check each and accept it by id (`memlet admin anchors accept ID…`)")
    if not (accepted or repinned or unknown or refused):
        lines.append("- Nothing to accept.")
    lines.append("- Your files were not edited" + (" and nothing was written." if dry_run else "; the anchors live in `.memlet/config.json`."))
    res["formatted"] = "\n".join(lines) + "\n"
    return res


def drop_sidecar_anchors(repo_path=".", ids=(), *, dry_run=False) -> Dict[str, Any]:
    """Remove sidecar entries by id (attached, detached or rejected alike; also on an over-limit state, to recover).
    The drop is recorded in the ledger first, so an old config.json restored later stays rejected."""
    root = mw._resolve_root(repo_path)
    ids = [str(i).strip() for i in (ids or []) if str(i).strip()]
    if not ids:
        return _refusal("anchors drop", root, "give the id(s) to drop (see `memlet admin anchors list`)", dry_run, dropped=[])
    return _guard("anchors drop", root, dry_run, lambda: _drop_locked(root, ids, dry_run), dropped=[])


def _drop_locked(root: Path, ids: List[str], dry_run: bool) -> Dict[str, Any]:
    config, err, tag = _read_config_bound(root)
    if err:
        return _refusal("anchors drop", root, f"{err}; nothing was written", dry_run, dropped=[])
    raw, _problem = _raw_entries(config)
    present = {e.get("id") for e in raw if isinstance(e, dict)}
    dropped = [i for i in ids if i in present]
    unknown = [i for i in ids if i not in present]
    actions: List[Dict[str, str]] = []
    res = {"command": "anchors drop", "repoPath": str(root), "dryRun": dry_run, "dropped": dropped, "unknown": unknown,
           "actions": actions}
    if dropped:
        config = dict(config)
        config[SIDECAR_KEY] = [e for e in raw if not (isinstance(e, dict) and e.get("id") in dropped)]
        big = _too_big(_config_text(config))
        if big:
            return _refusal("anchors drop", root, big, dry_run, dropped=[])
        if dry_run:
            actions += [{"op": "would-update", "path": mw.LEDGER_PATH}, {"op": "would-update", "path": mw.CONFIG_PATH}]
        else:
            # ledger first: once the drop is recorded the entries are rejected even if config.json is not rewritten
            _ledger_events(root, [(i, "", "drop") for i in dropped], [])
            actions.append({"op": "update", "path": mw.LEDGER_PATH})
            try:
                mw.write_managed_text(root, mw.CONFIG_PATH, _config_text(config), if_match=tag)
            except (mw.ConcurrentChangeError, mw.UnsafePathError, OSError) as exc:
                res.update(ok=False, exitCode=1, partialWrite=True, written=[mw.LEDGER_PATH], notWritten=[mw.CONFIG_PATH],
                           error=(f"the drop of {', '.join(dropped)} was recorded in the ledger, so those entries are no "
                                  f"longer in effect, but .memlet/config.json could not be rewritten ({exc}); run "
                                  "`memlet admin anchors drop " + " ".join(dropped) + "` again to remove them from it"))
                res["formatted"] = f"## Memlet anchors drop\n\n- ERROR: {res['error']}\n"
                return res
            actions.append({"op": "update", "path": mw.CONFIG_PATH})
    ok = not unknown
    res.update(ok=ok, exitCode=0 if ok else 1)
    if unknown:
        res["error"] = "unknown id(s): " + ", ".join(unknown)
    lines = [f"## Memlet anchors drop{' (dry run)' if dry_run else ''}", ""]
    lines += [f"- {'would drop' if dry_run else 'dropped'} `{i}`" for i in dropped] + [f"- UNKNOWN `{i}`" for i in unknown]
    res["formatted"] = "\n".join(lines) + "\n"
    return res


def list_sidecar_anchors(repo_path=".") -> Dict[str, Any]:
    """Every sidecar entry with its state: `attached` (with the anchor's current status, checked like a written
    anchor), `detached` (rule edited, moved to another heading or file, repeated, or no longer naming the symbol) or
    `rejected` (bad shape, older format, or not what the ledger's latest event accepted). Read-only."""
    root = mw._resolve_root(repo_path)
    config, err = mw.read_config(root)
    if err:
        return _refusal("anchors list", root, err, False, entries=[])
    idx = sidecar_index(root, config)
    pins = mw.rule_anchor_pins(root)
    rows = []
    for e in idx["attached"]:
        path, symbol = e["anchor"].rsplit("::", 1)
        c = mw.classify_anchor(root, {"path": path, "symbol": symbol, "pin": pins.get(e["anchor"]) or e["pin"]})
        rows.append({"id": e["id"], "state": "attached", "source": e["source"], "line": e["currentLine"],
                     "sectionPath": e["sectionPath"], "recordedLine": e["line"], "lineShifted": e["lineShifted"],
                     "anchor": e["anchor"], "suggested": True, "status": c["status"], "reason": c["reason"],
                     "pinBasis": e["pin"].get("basis")})
    for e in idx["detached"]:
        rows.append({"id": e.get("id"), "state": e.get("state", "detached"), "source": e.get("source"),
                     "line": None, "recordedLine": e.get("line"), "anchor": e.get("anchor"), "suggested": True,
                     "status": None, "reason": e.get("reason")})
    rows.sort(key=lambda r: (str(r.get("id")), r["state"]))
    counts = {s: sum(r["state"] == s for r in rows) for s in ("attached", "detached", "rejected")}
    res = {"ok": True, "exitCode": 0, "command": "anchors list", "repoPath": str(root), "entries": rows, "counts": counts,
           "integrityNotice": INTEGRITY_NOTICE}
    lines = ["## Memlet sidecar anchors", "",
             f"- {counts['attached']} attached · {counts['detached']} detached · {counts['rejected']} rejected",
             f"- {INTEGRITY_NOTICE}"]
    for r in rows:
        if r["state"] == "attached":
            lines.append(f"- `{r['id']}` `{r['source']}:{r['line']}` → `{r['anchor']}`: {r['status']} ({r['reason']})"
                         + (f" (line shifted; accepted at line {r['recordedLine']}, informational)" if r["lineShifted"] else ""))
        else:
            lines.append(f"- `{r['id']}` {r['state'].upper()} `{r.get('source')}` → `{r.get('anchor')}`: {r['reason']}")
    if not rows:
        lines.append("- No sidecar anchors. `memlet admin anchors suggest` lists rules that could be anchored.")
    res["formatted"] = "\n".join(lines) + "\n"
    return res
