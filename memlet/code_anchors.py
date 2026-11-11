"""Polyglot structural symbol anchoring, call-contract extraction, and breaking-change guard.

Supports:
- Python (.py): Exact AST call-contract & docstring-invariant body hash
- TypeScript / JavaScript (.ts, .tsx, .js, .jsx, .mjs, .cjs): Function, arrow function, class, method, interface, and type contracts
- package.json: npm/pnpm/yarn script contracts (`scripts.<name>`)
- Makefile / *.mk: Make target contracts
- Explicit resolution status (`exact`, `contract`, `unsupported`, `parse_error`) so unsupported files are never silently treated as verified symbols.
"""
import ast
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_FILE_HASH_AST_CACHE = {}
_FILE_SIGNATURE_CACHE = {}

_TS_JS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_MAKE_NAMES = {"makefile", "gnumakefile"}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _digest(value) -> str:
    return _sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True))


def cached_file_sha(file_path) -> str:
    """Fast stat-keyed file SHA-256 helper."""
    p = Path(file_path)
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        cached = _FILE_HASH_AST_CACHE.get(key)
        if cached is not None:
            return cached[0]
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        if len(_FILE_HASH_AST_CACHE) > 512:
            _FILE_HASH_AST_CACHE.clear()
        _FILE_HASH_AST_CACHE[key] = (sha, None)
        return sha
    except OSError:
        return ""


def _strip_docstring_from_body(body_nodes):
    """Return body nodes without a leading docstring Expr(Constant(str)) so comment/docstring-only edits are detected."""
    if (
        body_nodes
        and isinstance(body_nodes[0], ast.Expr)
        and isinstance(getattr(body_nodes[0], "value", None), ast.Constant)
        and isinstance(body_nodes[0].value.value, str)
    ):
        rest = body_nodes[1:]
        return rest if rest else [ast.Pass()]
    return body_nodes


# Python hash bases. `python-ast-v3` (current) hashes a canonical AST rendering that is identical on Python 3.9-3.13:
# every field in `_fields` order, `type_params` (added in 3.12) omitted while empty, strings rendered as ASCII JSON
# (repr() escapes characters newer than the interpreter's Unicode database, e.g. U+1F6DF on 3.9). `python-ast-v2`
# hashed `ast.dump`, whose output differs between interpreter versions (3.13 omits empty fields, 3.12 adds
# `type_params`), and the pre-v2 `legacy` basis hashed `ast.dump` without stripping docstrings. Pins made with an
# older basis are still matched by recomputing that basis on the running interpreter (same-runtime legacy match).
PYTHON_HASH_BASIS = "python-ast-v3"
PYTHON_PREVIOUS_BASES = ("python-ast-v2", "legacy")
_OMIT_WHEN_EMPTY = frozenset({"type_params"})


def canonical_ast_dump(node) -> str:
    """Version-stable rendering of an AST (see PYTHON_HASH_BASIS). Attributes (line numbers) are never included."""
    if isinstance(node, ast.AST):
        parts = []
        for name in node._fields:
            value = getattr(node, name, None)
            if name in _OMIT_WHEN_EMPTY and (value is None or value == []):
                continue
            parts.append(name + "=" + canonical_ast_dump(value))
        return type(node).__name__ + "(" + ", ".join(parts) + ")"
    if isinstance(node, list):
        return "[" + ", ".join(canonical_ast_dump(item) for item in node) + "]"
    if isinstance(node, str):
        return "s" + json.dumps(node, ensure_ascii=True)
    if isinstance(node, bytes):
        return "b" + node.hex()
    return repr(node)  # int, float, complex, bool, None, Ellipsis: stable across versions


def _docstring_stripped(node):
    cloned = copy.deepcopy(node)
    for child in ast.walk(cloned):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            child.body = _strip_docstring_from_body(child.body)
    return cloned


def normalized_symbol_ast_hash(node) -> str:
    """Compute a docstring/comment/lineno-invariant AST hash for a function or class (basis `python-ast-v3`:
    identical on every supported Python version)."""
    try:
        return _sha256_text(canonical_ast_dump(_docstring_stripped(node)))
    except Exception:
        return _sha256_text(canonical_ast_dump(node))


def normalized_symbol_ast_hash_v2(node) -> str:
    """The previous basis (`python-ast-v2`): `ast.dump` of the docstring-stripped node. Interpreter-specific; only
    used to recognise pins made with it on the running interpreter."""
    try:
        return _sha256_text(ast.dump(_docstring_stripped(node), annotate_fields=True, include_attributes=False))
    except Exception:
        return _sha256_text(ast.dump(node, annotate_fields=True, include_attributes=False))


def _node_call_signature(node):
    """Extract complete call-contract signature metadata from a Python AST FunctionDef/AsyncFunctionDef/ClassDef."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raw_posonly = [a.arg for a in getattr(node.args, "posonlyargs", []) or []]
        raw_regular = [a.arg for a in getattr(node.args, "args", []) or []]
        pos_names = list(raw_posonly) + list(raw_regular)
        if pos_names and pos_names[0] in ("self", "cls"):
            if raw_posonly and raw_posonly[0] in ("self", "cls"):
                raw_posonly = raw_posonly[1:]
            elif raw_regular and raw_regular[0] in ("self", "cls"):
                raw_regular = raw_regular[1:]
            pos_names = pos_names[1:]
        num_defaults = len(getattr(node.args, "defaults", []) or [])
        req_count = max(0, len(pos_names) - num_defaults)
        req_args = pos_names[:req_count]
        opt_args = pos_names[req_count:]
        kwonly_nodes = list(getattr(node.args, "kwonlyargs", []) or [])
        kw_defaults = list(getattr(node.args, "kw_defaults", []) or [])
        kwonly_req = [
            kw.arg for kw, d in zip(kwonly_nodes, kw_defaults)
            if d is None
        ]
        kwonly_all = [kw.arg for kw in kwonly_nodes]
        has_vararg = bool(getattr(node.args, "vararg", None))
        has_kwarg = bool(getattr(node.args, "kwarg", None))
        is_async = isinstance(node, ast.AsyncFunctionDef)
        sig_payload = {
            "kind": "function",
            "isAsync": is_async,
            "posonlyArgs": raw_posonly,
            "regularArgs": raw_regular,
            "reqArgs": req_args,
            "optArgs": opt_args,
            "allArgs": pos_names,
            "kwonlyReq": kwonly_req,
            "kwonlyAll": kwonly_all,
            "hasVararg": has_vararg,
            "hasKwarg": has_kwarg,
            "normAstHash": normalized_symbol_ast_hash(node),
        }
        sig_payload["annotationHash"] = _digest({
            "args": {a.arg: ast.dump(a.annotation, include_attributes=False) if a.annotation else None
                     for a in list(getattr(node.args, "posonlyargs", [])) + list(node.args.args) + list(node.args.kwonlyargs)
                     + [a for a in (node.args.vararg, node.args.kwarg) if a is not None]},
            "returns": ast.dump(node.returns, include_attributes=False) if node.returns else None,
        })
        sig_payload["sigHash"] = _digest({
            "kind": "function",
            "isAsync": is_async,
            "posonlyArgs": raw_posonly,
            "reqArgs": req_args,
            "allArgs": pos_names,
            "kwonlyReq": kwonly_req,
            "kwonlyAll": kwonly_all,
            "hasVararg": has_vararg,
            "hasKwarg": has_kwarg,
        })
        sig_payload["line"] = int(getattr(node, "lineno", 0) or 0)
        return sig_payload
    if isinstance(node, ast.ClassDef):
        methods = sorted({
            child.name
            for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not (child.name.startswith("_") and child.name != "__init__")
        })
        init_sig = None
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == "__init__":
                init_sig = _node_call_signature(child)
                break
        sig_payload = {
            "kind": "class",
            "methods": methods,
            "posonlyArgs": init_sig["posonlyArgs"] if init_sig else [],
            "regularArgs": init_sig["regularArgs"] if init_sig else [],
            "reqArgs": init_sig["reqArgs"] if init_sig else [],
            "optArgs": init_sig["optArgs"] if init_sig else [],
            "allArgs": init_sig["allArgs"] if init_sig else [],
            "kwonlyReq": init_sig["kwonlyReq"] if init_sig else [],
            "kwonlyAll": init_sig["kwonlyAll"] if init_sig else [],
            "hasVararg": init_sig["hasVararg"] if init_sig else False,
            "hasKwarg": init_sig["hasKwarg"] if init_sig else False,
            "normAstHash": normalized_symbol_ast_hash(node),
        }
        sig_payload["sigHash"] = _digest({
            "kind": "class",
            "methods": methods,
            "posonlyArgs": sig_payload["posonlyArgs"],
            "reqArgs": sig_payload["reqArgs"],
            "allArgs": sig_payload["allArgs"],
            "kwonlyReq": sig_payload["kwonlyReq"],
            "kwonlyAll": sig_payload["kwonlyAll"],
        })
        sig_payload["line"] = int(getattr(node, "lineno", 0) or 0)
        return sig_payload
    return None


def _collect_scoped_ast_nodes(body, prefix="", out=None):
    if out is None:
        out = []
    for node in body or []:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = getattr(node, "name", "")
            if not name:
                continue
            qname = f"{prefix}.{name}" if prefix else name
            out.append((qname, name, bool(prefix), node))
            child_prefix = f"{qname}.<locals>" if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else qname
            _collect_scoped_ast_nodes(getattr(node, "body", []), child_prefix, out)
        else:
            for attr in ("body", "orelse", "finalbody"):
                sub_body = getattr(node, attr, None)
                if isinstance(sub_body, list):
                    _collect_scoped_ast_nodes(sub_body, prefix, out)
            for handler in getattr(node, "handlers", None) or []:
                h_body = getattr(handler, "body", None)
                if isinstance(h_body, list):
                    _collect_scoped_ast_nodes(h_body, prefix, out)
    return out


def _split_top_level_commas(text: str):
    """Split parameter string by top-level commas, ignoring commas inside <...>, (...), {...}, [...]."""
    parts = []
    buf = []
    depth_angle = 0
    depth_paren = 0
    depth_brace = 0
    depth_brack = 0
    masked, _ = _blank_js_comments_preserve_lines(text or "", blank_strings=True)
    for idx, ch in enumerate(masked):
        if ch == "<":
            depth_angle += 1
        elif ch == ">" and depth_angle > 0 and (idx == 0 or masked[idx - 1] != "="):
            depth_angle -= 1
        elif ch == "(":
            depth_paren += 1
        elif ch == ")" and depth_paren > 0:
            depth_paren -= 1
        elif ch == "{":
            depth_brace += 1
        elif ch == "}" and depth_brace > 0:
            depth_brace -= 1
        elif ch == "[":
            depth_brack += 1
        elif ch == "]" and depth_brack > 0:
            depth_brack -= 1
        elif ch == "," and depth_angle == 0 and depth_paren == 0 and depth_brace == 0 and depth_brack == 0:
            token = "".join(buf).strip()
            if token:
                parts.append(token)
            buf = []
            continue
        buf.append(text[idx])
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_ts_js_params(raw_params: str):
    """Parse TypeScript/JavaScript parameter list into reqArgs, optArgs, allArgs, hasVararg."""
    req_args = []
    opt_args = []
    all_args = []
    has_vararg = False
    for idx, tok in enumerate(_split_top_level_commas(raw_params)):
        cleaned = _blank_js_comments_preserve_lines(tok)[0].strip()
        if not cleaned:
            continue
        # Remove access modifiers in TS constructors (public, private, protected, readonly)
        cleaned = re.sub(r"^(?:public|private|protected|readonly)\s+", "", cleaned).strip()
        if cleaned.startswith("..."):
            has_vararg = True
            continue
        has_default = False
        depth = 0
        masked = _blank_js_comments_preserve_lines(cleaned, blank_strings=True)[0]
        for i, c in enumerate(masked):
            if c in "<({[":
                depth += 1
            elif c in ">)}]" and depth > 0 and not (c == ">" and i and masked[i - 1] == "="):
                depth -= 1
            elif c == "=" and depth == 0 and (i == 0 or cleaned[i - 1] != "=") and (i + 1 >= len(cleaned) or cleaned[i + 1] not in ("=", ">")):
                has_default = True
                cleaned = cleaned[:i].strip()
                break
        # Now split name from `: Type` at depth 0
        param_head = cleaned.split(":", 1)[0].strip()
        is_optional = has_default or param_head.endswith("?")
        param_name = param_head.rstrip("?").strip()
        if not param_name or param_name == "this":
            continue
        if param_name.startswith("{") or param_name.startswith("["):
            param_name = f"destructured_{idx}"
        all_args.append(param_name)
        if is_optional and not opt_args and req_args:
            opt_args.append(param_name)
        elif is_optional:
            opt_args.append(param_name)
        else:
            req_args.append(param_name)
    return req_args, opt_args, all_args, has_vararg


def _extract_brace_block(src_text: str, open_brace_idx: int) -> str:
    """Extract balanced `{ ... }` block starting at open_brace_idx."""
    if open_brace_idx < 0 or open_brace_idx >= len(src_text) or src_text[open_brace_idx] != "{":
        return ""
    depth = 0
    in_str = None
    escaped = False
    for idx in range(open_brace_idx, len(src_text)):
        ch = src_text[idx]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_str:
            if ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src_text[open_brace_idx:idx + 1]
    return src_text[open_brace_idx:]


def _blank_js_comments_preserve_lines(text: str, *, blank_strings: bool = False, literal_spans=None) -> Tuple[str, bool]:
    """Replace JS/TS comments (and optionally string/template/regex interiors) with spaces (preserving newlines) and check syntax balance."""
    out: list[str] = []
    n = len(text)
    i = 0
    in_str: Optional[str] = None
    in_regex = False
    regex_start = 0
    string_start = 0
    template_starts = []
    in_regex_class = False
    in_line_comment = False
    in_block_comment = False
    stack: list[str] = []
    pairs = {")": "(", "}": "{", "]": "["}
    syntax_ok = True
    last_non_ws = ""
    last_word = ""
    ctrl_parens: list[bool] = []  # one flag per open "(": opened right after if/while/for/with
    block_braces: list[bool] = []  # one flag per open "{": True = statement block, False = object literal
    regex_after_ctrl = False      # a `/` right after `if (...)` starts a regex, not a division

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append("\n")
            else:
                out.append(" ")
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                out.append("  ")
                in_block_comment = False
                i += 2
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
            continue

        if in_regex:
            if ch == "\\" and i + 1 < n:
                out.append("  " if blank_strings else text[i:i + 2])
                i += 2
                continue
            if ch == "\n":
                # Unterminated regex literal at newline is a JS/TS syntax error
                syntax_ok = False
                in_regex = False
                in_regex_class = False
                out.append("\n")
            elif ch == "[" and not in_regex_class:
                in_regex_class = True
                out.append(" " if blank_strings else ch)
            elif ch == "]" and in_regex_class:
                in_regex_class = False
                out.append(" " if blank_strings else ch)
            elif ch == "/" and not in_regex_class:
                if literal_spans is not None:
                    literal_spans.append((regex_start, i + 1))
                in_regex = False
                last_non_ws = "/"
                out.append("/")
            else:
                out.append(" " if blank_strings else ch)
            i += 1
            continue

        if in_str is not None:
            if in_str == "`" and ch == "$" and nxt == "{":
                # Template interpolation `${ ... }` is executable code, never string content.
                if literal_spans is not None:
                    literal_spans.append((template_starts[-1], i + 2))
                out.append("${")
                stack.append("${")
                in_str = None
                last_non_ws = "{"
                last_word = ""
                i += 2
                continue
            if ch == "\\" and i + 1 < n:
                out.append("  " if blank_strings else text[i:i + 2])
                i += 2
                continue
            if ch == "\n":
                if in_str in ("'", '"'):
                    syntax_ok = False
                    in_str = None
                out.append("\n")
            elif ch == in_str:
                start = template_starts.pop() if in_str == "`" else string_start
                if literal_spans is not None:
                    literal_spans.append((start, i + 1))
                out.append(ch)
                in_str = None
                last_non_ws = ch
            else:
                out.append(" " if blank_strings else ch)
            i += 1
            continue

        if ch == "/" and nxt == "/":
            out.append("  ")
            in_line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "*":
            out.append("  ")
            in_block_comment = True
            i += 2
            continue

        # Regex literal start (never after `<` or `>` or before `>` which is JSX `/>`)
        if (
            ch == "/"
            and nxt not in ("/", "*", "=", ">", "\n", "\r", "")
            and (
                not last_non_ws
                or last_non_ws in "=(,:[!&|?{;~+-*%^"
                or last_word in ("return", "case", "throw", "delete", "void", "typeof", "yield", "await", "in", "of", "new", "else", "do")
                or regex_after_ctrl
            )
        ):
            in_regex = True
            regex_start = i
            in_regex_class = False
            regex_after_ctrl = False
            out.append("/")
            i += 1
            continue

        if ch in ("'", '"', "`"):
            in_str = ch
            string_start = i
            if ch == "`":
                template_starts.append(i)
            out.append(ch)
            i += 1
            continue

        if ch == "}" and stack and stack[-1] == "${":
            stack.pop()
            out.append("}")
            in_str = "`"
            template_starts[-1] = i
            i += 1
            continue

        closed_ctrl = False
        if ch in ("(", "{", "["):
            if ch == "(":
                ctrl_parens.append(last_word in ("if", "while", "for", "with"))
            elif ch == "{":
                # Block if it follows `)`, `;`, `{`, `}`, `=>`, start of input, or else/do/try/finally;
                # otherwise (after `=`, `(`, `,`, `:`, `return`, `?` ...) it is an object literal.
                k = i - 1
                while k >= 0 and text[k].isspace():
                    k -= 1
                prev = text[k] if k >= 0 else ""
                arrow = prev == ">" and k >= 1 and text[k - 1] == "="
                block_braces.append(prev in ("", ")", ";", "{", "}") or arrow or last_word in ("else", "do", "try", "finally"))
            stack.append(ch)
        elif ch in (")", "}", "]"):
            if not stack or stack[-1] != pairs[ch]:
                syntax_ok = False
            else:
                stack.pop()
                if ch == ")" and ctrl_parens:
                    closed_ctrl = ctrl_parens.pop()
                elif ch == "}" and block_braces:
                    closed_ctrl = block_braces.pop()  # a `/` after a closed statement block starts a regex

        if ch.isalpha() or ch in ("_", "$"):
            # Track identifier words for `return /regex/`
            j = i
            while j < n and (text[j].isalnum() or text[j] in ("_", "$")):
                j += 1
            last_word = text[i:j]
            last_non_ws = text[j - 1]
            regex_after_ctrl = False
            out.append(text[i:j])
            i = j
            continue

        if not ch.isspace():
            last_non_ws = ch
            last_word = ""
            regex_after_ctrl = closed_ctrl
        out.append(ch)
        i += 1

    if in_block_comment or in_str is not None or in_regex or stack:
        syntax_ok = False

    blanked = "".join(out)
    # Detect empty assignment `const x = ;` or `x = ;`
    if syntax_ok and re.search(r"(?<![=!<>+\-*/%&|^])=\s*;", blanked):
        syntax_ok = False

    return blanked, syntax_ok


def _strip_js_comments(text: str) -> str:
    blanked, _ = _blank_js_comments_preserve_lines(text, blank_strings=False)
    return "\n".join(ln.strip() for ln in blanked.splitlines() if ln.strip())


def _js_hash_tokens(text: str) -> List[str]:
    """Comment/indentation-invariant tokens, retaining literals and line breaks.

    This is a lexical approximation, not a JS parser: automatic semicolon
    insertion, regex/division ambiguity and JSX prevent claiming AST exactness.
    """
    spans = []
    clean, _ = _blank_js_comments_preserve_lines(text, literal_spans=spans)
    pattern = re.compile(r"[A-Za-z_$][\w$]*|(?:\d+(?:\.\d+)?)|===|!==|=>|==|!=|<=|>=|\+\+|--|&&|\|\||\?\?|\*\*|\.\.\.|[^\s]")
    tokens = []
    end = 0

    def append(start, stop):
        nonlocal end
        if tokens and "\n" in clean[end:start]:
            tokens.append("\n")  # Preserve automatic semicolon insertion boundaries.
        tokens.append(clean[start:stop])
        end = stop

    for start, stop in sorted(spans) + [(len(clean), len(clean))]:
        for match in pattern.finditer(clean, end, start):
            append(match.start(), match.end())
        if stop > start:
            append(start, stop)  # Literal contents, including whitespace, are indivisible.
    return tokens


def _balanced_end(mask: str, start: int) -> int:
    """Return the end (exclusive) of a balanced group in lexically masked JS."""
    pairs = {"(": ")", "[": "]", "{": "}", "<": ">"}
    if start >= len(mask) or mask[start] not in pairs:
        return -1
    # Angle brackets are only structural when explicitly scanning generics;
    # comparisons inside a parameter default must not hide the closing ')'.
    opening = mask[start]
    depth = 0
    for i in range(start, len(mask)):
        ch = mask[i]
        if ch == opening:
            depth += 1
        elif ch == pairs[opening] and not (ch == ">" and i and mask[i - 1] == "="):
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _skip_js_space(mask: str, pos: int) -> int:
    while pos < len(mask) and mask[pos].isspace():
        pos += 1
    return pos


def _ts_parameters(mask: str, pos: int):
    """Locate a complete parameter list, including nested callback/generic types."""
    pos = _skip_js_space(mask, pos)
    if pos < len(mask) and mask[pos] == "<":
        pos = _balanced_end(mask, pos)
        if pos < 0:
            return None
        pos = _skip_js_space(mask, pos)
    if pos < len(mask) and mask[pos] == "(":
        end = _balanced_end(mask, pos)
        return (pos + 1, end - 1, end) if end >= 0 else None
    m = re.match(r"[A-Za-z_$][\w$]*", mask[pos:])
    return (pos, pos + m.end(), pos + m.end()) if m else None


def _ts_body_start(mask: str, pos: int, arrow: bool):
    """Skip a return annotation, including object, callback and generic types."""
    pos = _skip_js_space(mask, pos)
    annotation = pos < len(mask) and mask[pos] == ":"
    type_start = pos + 1 if annotation else pos
    if annotation:
        pos += 1
    while pos < len(mask):
        pos = _skip_js_space(mask, pos)
        if mask.startswith("=>", pos):
            if arrow:
                return _skip_js_space(mask, pos + 2), pos
            pos += 2
            continue
        if pos >= len(mask) or mask[pos] == ";":
            return None
        ch = mask[pos]
        if ch == "{":
            prefix = mask[type_start:pos].rstrip()
            # An object type starts an annotation or follows a type operator.
            is_type = annotation and (not prefix or prefix[-1:] in ("|", "&", ":", "?") or prefix.endswith("=>") or re.search(r"\b(?:extends|keyof|typeof)\s*$", prefix))
            if not is_type and not arrow:
                return pos, pos
            if not annotation and arrow:
                return None
        if ch in "([{<":
            end = _balanced_end(mask, pos)
            if end < 0:
                return None
            pos = end
        elif not annotation:
            return None
        else:
            pos += 1
    return None


def _ts_annotation_hash(raw_params: str, return_type: str, type_parameters: str) -> str:
    annotations = []
    for token in _split_top_level_commas(raw_params):
        mask = _blank_js_comments_preserve_lines(token, blank_strings=True)[0]
        depth = 0
        for i, c in enumerate(mask):
            if c in "<({[":
                depth += 1
            elif c in ">)}]" and depth and not (c == ">" and i and mask[i - 1] == "="):
                depth -= 1
            elif c == "=" and depth == 0 and mask[i:i + 2] not in ("=>", "=="):
                token = token[:i]
                break
        annotations.append(_js_hash_tokens(token.split(":", 1)[1] if ":" in token else ""))
    return _digest({"parameters": annotations, "return": _js_hash_tokens(return_type),
                    "generics": _js_hash_tokens(type_parameters)})


def _build_ts_func_sig(is_async: bool, raw_params: str, body_text: str, return_type: str = "", type_parameters: str = "") -> dict:
    req_args, opt_args, all_args, has_vararg = _parse_ts_js_params(raw_params)
    contract = {"kind": "function", "isAsync": bool(is_async),
                "parameters": _js_hash_tokens(raw_params), "returnType": _js_hash_tokens(return_type),
                "typeParameters": _js_hash_tokens(type_parameters)}
    sig = {
        "kind": "function", "isAsync": bool(is_async), "posonlyArgs": [],
        "regularArgs": all_args, "reqArgs": req_args, "optArgs": opt_args, "allArgs": all_args,
        "kwonlyReq": [], "kwonlyAll": [], "hasVararg": has_vararg, "hasKwarg": False,
        "normAstHash": _digest({"contract": contract, "body": _js_hash_tokens(body_text)}),
        "sigHash": _digest(contract),
        "annotationHash": _ts_annotation_hash(raw_params, return_type, type_parameters),
        "hashExact": False, "hashReason": "ts_js_lexical_scan_unverified",
    }
    return sig


def _extract_ts_js_signatures(raw_src_text: str) -> Tuple[bool, dict]:
    """Extract contracts with balanced parameter scans and literal-preserving hashes."""
    src_text, syntax_ok = _blank_js_comments_preserve_lines(raw_src_text, blank_strings=True)
    sigs = {}

    def _line_of(idx: int) -> int:
        while idx < len(src_text) and src_text[idx] in " \t\r\n":
            idx += 1
        return src_text.count("\n", 0, idx) + 1

    def _callable(pos, is_async=False, arrow=False):
        params = _ts_parameters(src_text, pos)
        if params is None:
            return None
        pstart, pend, end = params
        body = _ts_body_start(src_text, end, arrow)
        if body is None:
            return None
        bstart, rend = body
        if bstart < len(src_text) and src_text[bstart] == "{":
            bend = _balanced_end(src_text, bstart)
            if bend < 0:
                return None
        else:
            # Expression arrows: scan balanced groups until a statement/wrapper boundary.
            bend = bstart
            while bend < len(src_text) and src_text[bend] not in ";,)}":
                if src_text[bend] in "([{":
                    next_pos = _balanced_end(src_text, bend)
                    if next_pos < 0:
                        return None
                    bend = next_pos
                else:
                    bend += 1
        sig = _build_ts_func_sig(is_async, raw_src_text[pstart:pend], raw_src_text[bstart:bend],
                                 raw_src_text[end:rend], raw_src_text[pos:pstart - 1] if src_text[_skip_js_space(src_text, pos):].startswith("<") else "")
        return sig

    func_pat = re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?(default[ \t]+)?(?:declare[ \t]+)?(async[ \t]+)?"
        r"function[ \t]*\*?[ \t]+([A-Za-z_$][\w$]*)")
    for m in func_pat.finditer(src_text):
        sig = _callable(m.end(), bool(m.group(2)))
        if sig:
            sig["line"] = _line_of(m.start())
            sigs[m.group(3)] = sig
            if m.group(1):
                sigs["default"] = sig

    # Assignments, anonymous defaults and nested HOC wrappers share the same scanner.
    assign_pat = re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+([A-Za-z_$][\w$]*)[ \t]*"
        r"(?::[^=\n]+)?=|^[ \t]*export[ \t]+default[ \t]+")
    for m in assign_pat.finditer(src_text):
        name = m.group(1) or "default"
        if name in sigs:
            continue
        pos = _skip_js_space(src_text, m.end())
        for _ in range(32):  # bounded nesting of wrappers such as memo(forwardRef(...))
            am = re.match(r"async\b\s*", src_text[pos:])
            fnpos = pos + am.end() if am else pos
            fm = re.match(r"function\b\s*\*?\s*([A-Za-z_$][\w$]*)?", src_text[fnpos:])
            sig = _callable(fnpos + fm.end() if fm else fnpos, bool(am), arrow=not bool(fm))
            if sig:
                sig["line"] = _line_of(m.start())
                sigs[name] = sig
                if fm and fm.group(1):
                    sigs[fm.group(1)] = sig
                break
            wrapper = re.match(r"[A-Za-z_$][\w$.]*\s*", src_text[pos:])
            if not wrapper:
                break
            pos += wrapper.end()
            if pos < len(src_text) and src_text[pos] == "<":
                pos = _balanced_end(src_text, pos)
                if pos < 0:
                    break
            pos = _skip_js_space(src_text, pos)
            if pos >= len(src_text) or src_text[pos] != "(":
                break
            pos = _skip_js_space(src_text, pos + 1)

    # 3. Classes and their methods: [export] [default] [abstract] class Name
    class_pat = re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:abstract[ \t]+)?class[ \t]+([A-Za-z_$][A-Za-z0-9_$]*)[^{;\n]*\{"
    )
    method_pat = re.compile(
        r"(?m)^[ \t]*(?:(?:public|private|protected|static|readonly|override|abstract)[ \t]+)*(async[ \t]+)?([A-Za-z_$][A-Za-z0-9_$]*)[ \t]*(?=[<(])"
    )
    reserved_keywords = {"if", "for", "while", "switch", "catch", "function", "return", "sizeof", "typeof"}
    by_short_method = {}
    for cm in class_pat.finditer(src_text):
        cls_name = cm.group(1)
        brace_pos = cm.end() - 1
        cls_block = _extract_brace_block(src_text, brace_pos)
        methods = []
        ctor_sig = None
        for mm in method_pat.finditer(cls_block):
            m_async = bool(mm.group(1))
            m_name = mm.group(2)
            if m_name in reserved_keywords:
                continue
            msig = _callable(brace_pos + mm.end(), m_async)
            if msig is None:
                continue
            msig["line"] = _line_of(brace_pos + mm.start())
            if m_name == "constructor":
                ctor_sig = msig
                continue
            if not m_name.startswith("_") and not m_name.startswith("#"):
                methods.append(m_name)
            qname = f"{cls_name}.{m_name}"
            sigs[qname] = msig
            if m_name not in sigs:
                by_short_method.setdefault(m_name, []).append((qname, msig))
        methods_sorted = sorted(set(methods))
        cls_sig = {
            "kind": "class",
            "methods": methods_sorted,
            "posonlyArgs": [],
            "regularArgs": ctor_sig["regularArgs"] if ctor_sig else [],
            "reqArgs": ctor_sig["reqArgs"] if ctor_sig else [],
            "optArgs": ctor_sig["optArgs"] if ctor_sig else [],
            "allArgs": ctor_sig["allArgs"] if ctor_sig else [],
            "kwonlyReq": [],
            "kwonlyAll": [],
            "hasVararg": ctor_sig["hasVararg"] if ctor_sig else False,
            "hasKwarg": False,
            "normAstHash": _digest(_js_hash_tokens(raw_src_text[cm.start():brace_pos + len(cls_block)])),
        }
        cls_sig["sigHash"] = _digest({
            "kind": "class",
            "methods": methods_sorted,
            "reqArgs": cls_sig["reqArgs"],
            "allArgs": cls_sig["allArgs"],
        })
        cls_sig["line"] = _line_of(cm.start())
        sigs[cls_name] = cls_sig

    for short_name, entries in by_short_method.items():
        if short_name in sigs:
            continue
        if len(entries) == 1:
            sigs[short_name] = entries[0][1]
        else:
            sub_sigs = {qn: sg for qn, sg in entries}
            sigs[short_name] = {
                "kind": "ambiguous_method",
                "qualified": sorted(sub_sigs.keys()),
                "subSignatures": sub_sigs,
                "sigHash": _digest({qn: sg.get("sigHash") for qn, sg in sorted(sub_sigs.items())}),
                "normAstHash": _digest({qn: sg.get("normAstHash") for qn, sg in sorted(sub_sigs.items())}),
            }

    # 4. Interfaces: [export] interface Name { field: type; optField?: type; }
    iface_pat = re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?interface[ \t]+([A-Za-z_$][A-Za-z0-9_$]*)[^{;\n]*\{"
    )
    prop_pat = re.compile(r"(?m)^[ \t]*(?:readonly[ \t]+)?([A-Za-z_$][A-Za-z0-9_$]*)(\?)?[ \t]*:")
    for im in iface_pat.finditer(src_text):
        iface_name = im.group(1)
        brace_pos = im.end() - 1
        iface_block = _extract_brace_block(src_text, brace_pos)
        req_fields = []
        opt_fields = []
        for pm in prop_pat.finditer(iface_block):
            fname = pm.group(1)
            is_opt = bool(pm.group(2))
            if is_opt:
                opt_fields.append(fname)
            else:
                req_fields.append(fname)
        all_fields = req_fields + opt_fields
        isig = {
            "kind": "interface",
            "isAsync": False,
            "posonlyArgs": [],
            "regularArgs": all_fields,
            "reqArgs": req_fields,
            "optArgs": opt_fields,
            "allArgs": all_fields,
            "kwonlyReq": [],
            "kwonlyAll": [],
            "hasVararg": False,
            "hasKwarg": False,
            "normAstHash": _digest(_js_hash_tokens(raw_src_text[im.start():brace_pos + len(iface_block)])),
        }
        isig["sigHash"] = _digest({
            "kind": "interface",
            "reqArgs": req_fields,
            "allArgs": all_fields,
        })
        isig["line"] = _line_of(im.start())
        sigs[iface_name] = isig

    # 5. Type aliases: [export] type Name = ...
    type_pat = re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?type[ \t]+([A-Za-z_$][A-Za-z0-9_$]*)[ \t]*(?:<[^=]+>)?[ \t]*=[ \t]*([^\n;]+)"
    )
    for tm in type_pat.finditer(src_text):
        tname = tm.group(1)
        if tname in sigs:
            continue
        rhs = _js_hash_tokens(raw_src_text[tm.start(2):tm.end(2)])
        tsig = {
            "kind": "type",
            "isAsync": False,
            "posonlyArgs": [],
            "regularArgs": [],
            "reqArgs": [],
            "optArgs": [],
            "allArgs": [],
            "kwonlyReq": [],
            "kwonlyAll": [],
            "hasVararg": False,
            "hasKwarg": False,
            "normAstHash": _digest(rhs),
            "sigHash": _digest(rhs),
        }
        tsig["line"] = _line_of(tm.start())
        sigs[tname] = tsig

    return syntax_ok, sigs


def _extract_package_json_signatures(src_text: str):
    """Extract npm/pnpm/yarn script contracts from package.json."""
    try:
        data = json.loads(src_text)
    except Exception:
        return False, {}
    if not isinstance(data, dict):
        return False, {}
    scripts = data.get("scripts")
    sigs = {}
    if isinstance(scripts, dict):
        for s_name, cmd in sorted(scripts.items()):
            cmd_str = str(cmd or "").strip()
            h = _sha256_text(cmd_str)
            entry = {
                "kind": "script",
                "command": cmd_str,
                "isAsync": False,
                "posonlyArgs": [],
                "regularArgs": [],
                "reqArgs": [],
                "optArgs": [],
                "allArgs": [],
                "kwonlyReq": [],
                "kwonlyAll": [],
                "hasVararg": False,
                "hasKwarg": False,
                "normAstHash": h,
                "sigHash": h,
            }
            sigs[s_name] = entry
            sigs[f"script:{s_name}"] = entry
    return True, sigs


def _extract_makefile_signatures(src_text: str) -> dict:
    """Extract Make target contracts from Makefile / *.mk."""
    sigs = {}
    target_pat = re.compile(r"(?m)^([A-Za-z0-9_.-]+)[ \t]*:(?!=)[ \t]*([^\n#]*)")
    for m in target_pat.finditer(src_text):
        tname = m.group(1).strip()
        if tname.startswith("."):
            continue
        prereqs = [x.strip() for x in (m.group(2) or "").split() if x.strip()]
        # Collect indented recipe lines immediately following
        rest_lines = src_text[m.end():].splitlines()
        recipe = []
        for ln in rest_lines:
            if ln.startswith("\t") or ln.startswith("  "):
                recipe.append(ln.strip())
            elif not ln.strip() or ln.strip().startswith("#"):
                continue
            else:
                break
        body_hash = _sha256_text("\n".join(prereqs + recipe))
        entry = {
            "kind": "make_target",
            "prereqs": prereqs,
            "isAsync": False,
            "posonlyArgs": [],
            "regularArgs": [],
            "reqArgs": [],
            "optArgs": [],
            "allArgs": [],
            "kwonlyReq": [],
            "kwonlyAll": [],
            "hasVararg": False,
            "hasKwarg": False,
            "normAstHash": body_hash,
            "sigHash": _digest({"kind": "make_target", "target": tname, "prereqs": prereqs}),
        }
        sigs[tname] = entry
        sigs[f"make:{tname}"] = entry
    return sigs


def extract_file_symbol_signatures(file_path):
    """Extract call-contract signatures for functions/classes/interfaces/scripts/targets in file_path."""
    p = Path(file_path)
    try:
        if p.is_symlink() or not p.is_file():
            return {
                "__file_exists__": False,
                "__syntax_ok__": False,
                "__resolution__": "missing",
                "__language__": "unknown",
                "symbols": {},
            }
        st = p.stat()
        if st.st_size > 2 * 1024 * 1024:
            return {
                "__file_exists__": True,
                "__syntax_ok__": True,
                "__resolution__": "oversized",
                "__language__": "unknown",
                "symbols": {},
            }
        key = (str(p), st.st_mtime_ns, st.st_size)
        cached = _FILE_SIGNATURE_CACHE.get(key)
        if cached is not None:
            return cached
        raw_bytes = p.read_bytes()
        src_text = raw_bytes.decode("utf-8", errors="ignore")
    except OSError:
        return {
            "__file_exists__": False,
            "__syntax_ok__": False,
            "__resolution__": "missing",
            "__language__": "unknown",
            "symbols": {},
        }

    sigs = {}
    syntax_ok = True
    resolution = "unsupported"
    language = "unsupported"
    suffix_lower = p.suffix.lower()
    name_lower = p.name.lower()

    if suffix_lower == ".py":
        language = "python"
        resolution = "exact"
        try:
            tree = ast.parse(src_text)
            scoped_nodes = _collect_scoped_ast_nodes(tree.body)
            top_level_names = {name for qname, name, is_nested, _ in scoped_nodes if not is_nested}
            by_short = {}
            for qname, name, is_nested, node in scoped_nodes:
                sig = _node_call_signature(node)
                if sig is None:
                    continue
                sigs[qname] = sig
                if is_nested and name not in top_level_names:
                    by_short.setdefault(name, []).append((qname, sig))
            for short_name, entries in by_short.items():
                if len(entries) == 1:
                    sigs[short_name] = entries[0][1]
                else:
                    sub_sigs = {qn: sg for qn, sg in entries}
                    sigs[short_name] = {
                        "kind": "ambiguous_method",
                        "qualified": sorted(sub_sigs.keys()),
                        "subSignatures": sub_sigs,
                        "sigHash": _digest({qn: sg.get("sigHash") for qn, sg in sorted(sub_sigs.items())}),
                        "normAstHash": _digest({qn: sg.get("normAstHash") for qn, sg in sorted(sub_sigs.items())}),
                    }
        except SyntaxError:
            syntax_ok = False
            resolution = "parse_error"
    elif suffix_lower in _TS_JS_EXTS:
        language = "typescript" if suffix_lower in (".ts", ".tsx") else "javascript"
        syntax_ok, sigs = _extract_ts_js_signatures(src_text)
        resolution = "contract" if syntax_ok else "parse_error"
    elif name_lower == "package.json":
        language = "package_json"
        ok_json, sigs = _extract_package_json_signatures(src_text)
        syntax_ok = ok_json
        resolution = "contract" if ok_json else "parse_error"
    elif name_lower in _MAKE_NAMES or suffix_lower == ".mk":
        language = "makefile"
        resolution = "contract"
        sigs = _extract_makefile_signatures(src_text)
    else:
        resolution = "unsupported"
        language = suffix_lower.lstrip(".") or "unknown"

    res = {
        "__file_exists__": True,
        "__syntax_ok__": syntax_ok,
        "__resolution__": resolution,
        "__language__": language,
        "symbols": sigs,
    }
    if len(_FILE_SIGNATURE_CACHE) > 512:
        _FILE_SIGNATURE_CACHE.clear()
    _FILE_SIGNATURE_CACHE[key] = res
    return res


_PYTHON_BINDING_CACHE = {}  # <= 32 contents, keyed only by SHA-256 of freshly read bytes
_PYTHON_BINDING_PARSE_COUNT = 0


def python_symbol_rebound(file_path, symbol):
    """Content-addressed module binding scan, independent of stat-keyed caches.

    Hashes still describe a definition, not subsequent bindings. An overload stub
    is not an implementation: as before, one implementation after typing-style
    overload stubs can be active. No execution or import resolution is attempted.
    """
    p = Path(file_path)
    try:
        if p.is_symlink() or p.stat().st_size > 2 * 1024 * 1024:
            return True
        content = p.read_bytes()
        if len(content) > 2 * 1024 * 1024:
            return True
        digest = hashlib.sha256(content).digest()
        cached = _PYTHON_BINDING_CACHE.get(digest)
        if cached is None:
            cached = _python_binding_analysis(content.decode("utf-8", errors="ignore"))
            if len(_PYTHON_BINDING_CACHE) >= 32:
                _PYTHON_BINDING_CACHE.pop(next(iter(_PYTHON_BINDING_CACHE)))
            _PYTHON_BINDING_CACHE[digest] = cached
        definitions, bindings = cached
        qualified = definitions.get(symbol, set())
        if not qualified:
            return False
        return (any(bindings.get(q, 0) > 1 for q in qualified)
                or symbol not in qualified and bindings.get(symbol, 0) > 0)
    except (OSError, SyntaxError, ValueError, RecursionError):
        return True  # never affirm a cached definition when the fresh scan failed


def _python_binding_analysis(content):
    global _PYTHON_BINDING_PARSE_COUNT
    _PYTHON_BINDING_PARSE_COUNT += 1
    tree = ast.parse(content)
    definitions = _collect_scoped_ast_nodes(tree.body)
    qualified = {q for q, _, _, _ in definitions}
    # Include exact qualified names and the aliases accepted by the old scan.
    names = {q: {q} for q in qualified}
    for q, short, _, _ in definitions:
        if short not in qualified:
            names.setdefault(short, set()).add(q)
    bindings = {}

    def overload_stub(node):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        decorated = any(isinstance(d, ast.Name) and d.id == "overload" or
                        isinstance(d, ast.Attribute) and d.attr == "overload" for d in node.decorator_list)
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        return decorated and all(isinstance(n, ast.Pass) or isinstance(n, ast.Expr) and
                                 isinstance(n.value, ast.Constant) and n.value.value is Ellipsis for n in body)

    class Bindings(ast.NodeVisitor):
        def __init__(self, prefix="", globals_=()):
            self.prefix, self.globals = prefix, set(globals_)

        def bind(self, name):
            key = name if name.split(".")[0] in self.globals or not self.prefix else self.prefix + "." + name
            bindings[key] = bindings.get(key, 0) + 1

        def visit_Name(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.bind(node.id)

        def visit_Attribute(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                parts, cur = [node.attr], node.value
                while isinstance(cur, ast.Attribute):
                    parts.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    self.bind(".".join([cur.id] + list(reversed(parts))))
            self.visit(node.value)

        def visit_Import(self, node):
            for alias in node.names:
                self.bind(alias.asname or alias.name.split(".")[0])

        def visit_ImportFrom(self, node):
            for alias in node.names:
                if alias.name == "*":
                    # A wildcard may overwrite any name in this scope.
                    for key in qualified:
                        if not self.prefix or key.startswith(self.prefix + "."):
                            bindings[key] = bindings.get(key, 0) + 1
                else:
                    self.bind(alias.asname or alias.name)

        def visit_MatchAs(self, node):
            if node.name:
                self.bind(node.name)
            self.generic_visit(node)

        visit_MatchStar = visit_MatchAs

        def visit_MatchMapping(self, node):
            if node.rest:
                self.bind(node.rest)
            self.generic_visit(node)

        def visit_ExceptHandler(self, node):
            if node.name:
                self.bind(node.name)
            self.generic_visit(node)

        def scope(self, node, function):
            if not overload_stub(node):
                self.bind(node.name)
            # Defaults, decorators and bases are evaluated in the enclosing scope.
            for d in node.decorator_list:
                self.visit(d)
            if function:
                for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                    self.visit(d)
            else:
                for d in node.bases + [k.value for k in node.keywords]:
                    self.visit(d)
            name = node.name if not self.prefix or node.name in self.globals else self.prefix + "." + node.name
            prefix = name + ".<locals>" if function else name
            globals_ = set()
            def declarations(n):
                if isinstance(n, ast.Global):
                    globals_.update(n.names)
                elif not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    for child in ast.iter_child_nodes(n):
                        declarations(child)
            for n in node.body:
                declarations(n)
            child = Bindings(prefix, globals_)
            if function:
                for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs + [a for a in (node.args.vararg, node.args.kwarg) if a]:
                    child.bind(a.arg)
            for n in node.body:
                child.visit(n)

        def visit_FunctionDef(self, node):
            self.scope(node, True)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            self.scope(node, False)

        def visit_Lambda(self, node):
            for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                self.visit(d)
            # Lambda bindings cannot rebind the surrounding scope.

        def visit_ListComp(self, node):
            # Comprehension iteration variables have their own scope; walrus
            # bindings escape to this scope, including inside the expression.
            for n in ast.walk(node):
                if isinstance(n, ast.NamedExpr):
                    self.visit(n.target)
        visit_SetComp = visit_ListComp
        visit_DictComp = visit_ListComp
        visit_GeneratorExp = visit_ListComp

    Bindings().visit(tree)
    return names, bindings


def previous_python_symbol_hashes(file_path) -> List[Dict[str, str]]:
    """Per-symbol hashes of a Python file under every previous basis (`PYTHON_PREVIOUS_BASES` order), computed on
    the running interpreter, to recognise a pin or tracked hash made with an older basis."""
    return [extract_file_symbol_hashes(file_path, basis=b) for b in PYTHON_PREVIOUS_BASES]


def extract_file_symbol_hashes(file_path, *, legacy: bool = False, basis: Optional[str] = None):
    """Extract per-symbol SHA256 hashes from a source file via AST (Python) or polyglot contract extractor.
    Python symbols use the current basis (`PYTHON_HASH_BASIS`) unless `basis` names a previous one
    (`python-ast-v2`, or `legacy`, same as `legacy=True`)."""
    if legacy:
        basis = "legacy"
    if basis == PYTHON_HASH_BASIS:
        basis = None
    legacy = basis is not None  # any previous basis: computed fresh, never cached
    p = Path(file_path)
    try:
        if p.is_symlink() or not p.is_file():
            return {}
        st = p.stat()
        if st.st_size > 2 * 1024 * 1024:
            return {}
        key = (str(p), st.st_mtime_ns, st.st_size)
        cached = _FILE_HASH_AST_CACHE.get(key)
        if not legacy and cached is not None and cached[1] is not None:
            return cached[1]
        raw_bytes = p.read_bytes()
        sha = cached[0] if cached is not None else hashlib.sha256(raw_bytes).hexdigest()
        src_text = raw_bytes.decode("utf-8", errors="ignore")
    except OSError:
        return {}
    symbol_hashes = {}
    suffix_lower = p.suffix.lower()
    if suffix_lower == ".py":
        try:
            tree = ast.parse(src_text)
            lines = src_text.splitlines()
            scoped_nodes = _collect_scoped_ast_nodes(tree.body)
            top_level_names = {name for qname, name, is_nested, _ in scoped_nodes if not is_nested}
            by_short = {}
            for qname, name, is_nested, node in scoped_nodes:
                try:
                    if basis == "legacy":
                        hval = _sha256_text(ast.dump(node, annotate_fields=True, include_attributes=False))
                    elif basis == "python-ast-v2":
                        hval = normalized_symbol_ast_hash_v2(node)
                    else:
                        hval = normalized_symbol_ast_hash(node)
                except Exception:
                    start_ln = max(0, getattr(node, "lineno", 1) - 1)
                    end_ln = getattr(node, "end_lineno", start_ln + 1)
                    block = "\n".join(ln.strip() for ln in lines[start_ln:end_ln] if ln.strip() and not ln.strip().startswith("#"))
                    hval = _sha256_text(block)
                symbol_hashes[qname] = hval
                if is_nested and name not in top_level_names:
                    by_short.setdefault(name, []).append((qname, hval))
            for short_name, entries in by_short.items():
                if len(entries) == 1:
                    symbol_hashes[short_name] = entries[0][1]
                else:
                    symbol_hashes[short_name] = _digest({qn: hv for qn, hv in sorted(entries)})
            if not legacy:
                _FILE_HASH_AST_CACHE[key] = (sha, symbol_hashes)
            return symbol_hashes
        except SyntaxError:
            pass
    sig_info = extract_file_symbol_signatures(p)
    for sym_name, sig in (sig_info.get("symbols") or {}).items():
        symbol_hashes[sym_name] = sig.get("normAstHash") or sig.get("sigHash") or ""
    _FILE_HASH_AST_CACHE[key] = (sha, symbol_hashes)
    return symbol_hashes


# ---------------------------------------------------------------------------------------------------------------
# Exact-parser seam. The lexical TS/JS scanner above is an approximation, so a TS/JS anchor it fingerprints can at
# best be `compatible-unverified`. A per-language *exact* provider (a real parser) may fingerprint the same symbols
# under its own hash-basis name; an anchor pinned under that basis can then be `active`.
#
# * New TS/JS pins are lexical unless the repository opted in: `.memlet/config.json` `anchors.exactParsers` lists the
#   provider's `name` (e.g. ["tree-sitter"]). Installing a package changes nothing by itself, and no environment
#   variable can turn exact pinning on; `MEMLET_EXACT_PARSERS=0` is a kill switch for auto-loading optional parsers.
# * A pin always records the basis that made it and is re-checked under that same basis. A pin made by an exact
#   provider is checked by that provider when it can be imported (opt-in or not); when it is missing, cannot parse
#   the file, or is another version (another basis name) the anchor is `…_basis_repin_required`, never silently
#   re-classified with another basis. A stored pin without a basis is a legacy pin, which is not the same as no pin
#   (`NEW_PIN`).
#
# Provider protocol (duck-typed):
#     name:      what `anchors.exactParsers` lists to opt in ("tree-sitter")
#     languages: tuple of language names it covers ("typescript", "javascript")
#     basis:     hash-basis name recorded in pins, `[A-Za-z0-9_.-]+`, must change whenever its hashes could change
#     fingerprint(path: str, text: str) -> Optional[Dict[symbol, {"ast": hex, "sig": hex}]]
#                None when the file cannot be parsed exactly; a symbol declared more than once must be left out.

TS_LEXICAL_BASIS = "ts-js-lexical-v3"
NEW_PIN = object()  # `basis` of a fingerprint for a pin that does not exist yet (never confused with a legacy pin)
_BASIS_RE = re.compile(r"[A-Za-z0-9_.-]{1,120}")
_EXACT_PROVIDERS: Dict[str, Any] = {}
_EXACT_AUTOLOAD = {"done": False}
_EXACT_CACHE: Dict[Tuple[Any, ...], Optional[Dict[str, Dict[str, str]]]] = {}
EXACT_PROVIDER_ENV = "MEMLET_EXACT_PARSERS"  # "0" / "off": never auto-load optional parsers (a kill switch only)


def register_exact_provider(provider) -> None:
    """Register `provider` for each of its languages (replacing a previous one). Raises ValueError on a bad basis."""
    basis = getattr(provider, "basis", None)
    if not isinstance(basis, str) or not _BASIS_RE.fullmatch(basis) or basis in (TS_LEXICAL_BASIS, PYTHON_HASH_BASIS,
                                                                                    "legacy", "contract-v1"):
        raise ValueError(f"invalid exact-provider basis: {basis!r}")
    if not isinstance(getattr(provider, "name", None), str) or not provider.name:
        raise ValueError("an exact provider needs a name")
    for lang in getattr(provider, "languages", ()) or ():
        _EXACT_PROVIDERS[str(lang)] = provider
    _EXACT_CACHE.clear()


def unregister_exact_provider(provider=None, language: Optional[str] = None) -> None:
    """Remove `provider` (from every language) or whatever provider serves `language`."""
    for lang, p in list(_EXACT_PROVIDERS.items()):
        if (provider is not None and p is provider) or (language is not None and lang == language):
            del _EXACT_PROVIDERS[lang]
    _EXACT_CACHE.clear()


def _autoload_exact_providers() -> None:
    if _EXACT_AUTOLOAD["done"]:
        return
    _EXACT_AUTOLOAD["done"] = True
    import os
    if str(os.environ.get(EXACT_PROVIDER_ENV, "")).strip().lower() in ("0", "off", "false", "no"):
        return
    try:
        from . import ts_exact  # optional extra `memlet[treesitter]`; imports nothing third-party unless installed
        provider = ts_exact.load_provider()
    except Exception:  # an optional parser must never break the default path
        return
    if provider is not None:
        for lang in provider.languages:
            _EXACT_PROVIDERS.setdefault(lang, provider)


def exact_provider(language: str):
    """The exact provider for `language`, or None. Only called when an exact basis is actually needed (a pin made
    with one, or a repository that opted in), so an installed but unused parser is never even imported."""
    if language not in _EXACT_PROVIDERS:
        _autoload_exact_providers()
    return _EXACT_PROVIDERS.get(language)


def exact_symbol_fingerprints(file_path, language: str, *, basis: Optional[str] = None,
                              allowed: Optional[Iterable[str]] = None
                              ) -> Tuple[Optional[str], Optional[Dict[str, Dict[str, str]]]]:
    """(basis, {symbol: {"ast", "sig"}}) from the exact provider for `language`. `basis`: only a provider with exactly
    that basis may answer (else (its basis or None, None)). `allowed`: provider names the repository opted into (else
    (None, None)). (basis, None) when the provider could not parse the file. Reads a regular, non-symlinked file of
    at most 2 MB."""
    provider = exact_provider(language)
    if provider is None:
        return None, None
    if allowed is not None and getattr(provider, "name", None) not in set(allowed):
        return None, None
    if basis is not None and provider.basis != basis:
        return provider.basis, None
    p = Path(file_path)
    try:
        st = p.lstat()
    except OSError:
        return provider.basis, None
    key = (str(p), st.st_mtime_ns, st.st_size, st.st_ino, provider.basis, id(provider))
    from .memlet_wiki import read_regular_text  # contained reader: no symlink leaf, no FIFO, size cap
    text = read_regular_text(p, 2 * 1024 * 1024)
    if text is None:
        return provider.basis, None
    key = key + (hashlib.sha256(text.encode("utf-8")).hexdigest(),)  # never trust stat metadata alone
    if key in _EXACT_CACHE:
        return provider.basis, _EXACT_CACHE[key]
    try:
        raw = provider.fingerprint(str(p), text)
    except Exception:
        raw = None
    result = None
    if isinstance(raw, dict):
        result = {}
        for sym, fp in raw.items():
            if isinstance(sym, str) and isinstance(fp, dict) and isinstance(fp.get("ast"), str) and re.fullmatch(r"[0-9a-f]{16,64}", fp["ast"]):
                sig = fp.get("sig")
                result[sym] = {"ast": fp["ast"], "sig": sig if isinstance(sig, str) and re.fullmatch(r"[0-9a-f]{16,64}", sig) else ""}
    if len(_EXACT_CACHE) > 512:
        _EXACT_CACHE.clear()
    _EXACT_CACHE[key] = result
    return provider.basis, result


def is_breaking_symbol_change(old_sig, curr_sig):
    """Determine if a symbol evolution breaks existing call contracts (returns (is_breaking, reason)).
    Covers positional, optional, keyword-only, posonly (/), vararg (*args/**kwargs), async, class method, and TS interface contracts.
    """
    if curr_sig is None:
        return True, "symbol_deleted"
    if not isinstance(old_sig, dict) or not isinstance(curr_sig, dict):
        return True, "missing_signature_baseline"
    if old_sig.get("kind") == "ambiguous_method" or curr_sig.get("kind") == "ambiguous_method":
        old_subs = old_sig.get("subSignatures") or {}
        curr_subs = curr_sig.get("subSignatures") or {}
        if not old_subs or not curr_subs:
            return True, "ambiguous_symbol_scope_changed"
        any_evolved = False
        for qn, old_s in sorted(old_subs.items()):
            if qn not in curr_subs:
                return True, f"qualified_symbol_deleted:{qn}"
            brk, rsn = is_breaking_symbol_change(old_s, curr_subs[qn])
            if brk:
                return True, f"{rsn}:{qn}"
            if rsn != "docstring_or_comment_only":
                any_evolved = True
        return False, ("compatible_evolution" if any_evolved else "docstring_or_comment_only")
    if old_sig.get("kind") and curr_sig.get("kind") and old_sig["kind"] != curr_sig["kind"]:
        return True, f"symbol_kind_changed:{old_sig['kind']}->{curr_sig['kind']}"
    if ("isAsync" in old_sig or "isAsync" in curr_sig) and bool(old_sig.get("isAsync")) != bool(curr_sig.get("isAsync")):
        return True, "async_contract_changed"

    if old_sig.get("kind") == "interface":
        old_req_set = set(old_sig.get("reqArgs") or [])
        curr_req_set = set(curr_sig.get("reqArgs") or [])
        old_all_set = set(old_sig.get("allArgs") or [])
        curr_all_set = set(curr_sig.get("allArgs") or [])
        added_req = sorted(curr_req_set - old_req_set)
        if added_req:
            return True, f"interface_required_field_added:{','.join(added_req)}"
        removed_fields = sorted(old_all_set - curr_all_set)
        if removed_fields:
            return True, f"interface_field_removed:{','.join(removed_fields)}"
        if old_sig.get("normAstHash") == curr_sig.get("normAstHash"):
            return False, "docstring_or_comment_only"
        return False, "compatible_evolution"

    old_req = list(old_sig.get("reqArgs") or [])
    curr_req = list(curr_sig.get("reqArgs") or [])
    old_all = list(old_sig.get("allArgs") if old_sig.get("allArgs") is not None else old_req)
    curr_all = list(curr_sig.get("allArgs") if curr_sig.get("allArgs") is not None else curr_req)

    # 1. New required positional parameters added -> breaking
    if len(curr_req) > len(old_req):
        added = [a for a in curr_req if a not in old_req] or curr_req[len(old_req):]
        return True, f"required_positional_added:{','.join(added)}"

    # 2. Current required positional parameters must match old positional prefix
    if curr_req != old_req[:len(curr_req)]:
        return True, f"required_positional_renamed:{','.join(old_req)}->{','.join(curr_req)}"

    # 3. Any previously accepted positional or optional parameter removed, renamed, or reordered -> breaking
    if len(curr_all) < len(old_all) or curr_all[:len(old_all)] != old_all:
        return True, f"parameter_removed_or_reordered:{','.join(old_all)}->{','.join(curr_all)}"

    # 4. Regular parameter converted to positional-only (`def f(x) -> def f(x, /)`) -> breaks keyword callers `f(x=1)`
    old_posonly = set(old_sig.get("posonlyArgs") or [])
    curr_posonly = set(curr_sig.get("posonlyArgs") or [])
    newly_posonly = (curr_posonly - old_posonly) & set(old_all)
    if newly_posonly:
        return True, f"parameter_became_posonly:{','.join(sorted(newly_posonly))}"

    # 5. Required keyword-only parameters added -> breaking
    old_kw_req = set(old_sig.get("kwonlyReq") or [])
    curr_kw_req = set(curr_sig.get("kwonlyReq") or [])
    if curr_kw_req - old_kw_req:
        return True, f"required_kwonly_added:{','.join(sorted(curr_kw_req - old_kw_req))}"

    # 6. Existing keyword-only parameters removed -> breaking
    old_kw_all = set(old_sig.get("kwonlyAll") or old_kw_req)
    curr_kw_all = set(curr_sig.get("kwonlyAll") or curr_kw_req)
    removed_kw = old_kw_all - curr_kw_all
    if removed_kw:
        return True, f"kwonly_parameter_removed:{','.join(sorted(removed_kw))}"

    # 7. *args or **kwargs removed -> breaking
    if (bool(old_sig.get("hasVararg")) and not bool(curr_sig.get("hasVararg"))) or (
        bool(old_sig.get("hasKwarg")) and not bool(curr_sig.get("hasKwarg"))
    ):
        return True, "vararg_or_kwarg_removed"

    # 8. For classes, public methods removed -> breaking
    if old_sig.get("kind") == "class":
        old_methods = set(old_sig.get("methods") or [])
        curr_methods = set(curr_sig.get("methods") or [])
        removed_methods = old_methods - curr_methods
        if removed_methods:
            return True, f"class_methods_removed:{','.join(sorted(removed_methods))}"

    # 9. Distinguish docstring/comment-only AST change vs executable body evolution
    old_norm = old_sig.get("normAstHash")
    curr_norm = curr_sig.get("normAstHash")
    if old_norm and curr_norm and old_norm == curr_norm and old_sig.get("sigHash") == curr_sig.get("sigHash"):
        return False, "docstring_or_comment_only"
    return False, "compatible_evolution"
