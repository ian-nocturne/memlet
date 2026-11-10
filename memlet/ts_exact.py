"""Optional exact TypeScript / JavaScript fingerprints with tree-sitter (`pip install "memlet[treesitter]"`).

Memlet's own TS/JS scanner is lexical, so anchors it fingerprints stay `compatible-unverified`. When the tree-sitter
core and the TypeScript and JavaScript grammars are installed, this provider (registered lazily by
`memlet.code_anchors.exact_provider`) parses each file with a real grammar and hashes the syntax tree of every
top-level function, arrow / function-expression constant, class (and its methods), interface and type alias. A pin
made with it records the basis `tree-sitter-v1.core-X.ts-Y.js-Z`; any version change is a new basis, so such pins
report `exact_parser_basis_repin_required` instead of being compared with hashes from another grammar.

Nothing here is imported unless the packages are installed; a file whose tree has syntax errors is not fingerprinted
(Memlet then uses the lexical scanner). Set `MEMLET_EXACT_PARSERS=0` to turn the auto-loading off.
"""

import hashlib
import importlib
import importlib.util
import json
import re
from typing import Any, Dict, List, Optional, Tuple

TESTED = {"tree-sitter": "0.23", "tree-sitter-typescript": "0.23", "tree-sitter-javascript": "0.23"}
_FUNC_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_FUNC_DECLS = {"function_declaration", "generator_function_declaration"}
_CLASS_DECLS = {"class_declaration", "abstract_class_declaration"}


def _version(dist: str) -> Optional[str]:
    try:
        from importlib.metadata import version
        return version(dist)
    except Exception:
        return None


def load_provider() -> Optional["TreeSitterProvider"]:
    """The provider, or None when tree-sitter or a grammar is missing, of an untested minor version, or broken."""
    names = ("tree_sitter", "tree_sitter_typescript", "tree_sitter_javascript")
    try:
        if any(importlib.util.find_spec(n) is None for n in names):
            return None
        versions = {dist: _version(dist) for dist in TESTED}
        if any(not v or not v.startswith(TESTED[dist] + ".") for dist, v in versions.items()):
            return None
        ts, tst, tsj = (importlib.import_module(n) for n in names)
        return TreeSitterProvider(ts, tst, tsj, versions)
    except Exception:
        return None


def _h(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()


class TreeSitterProvider:
    name = "tree-sitter"  # what `.memlet/config.json` `anchors.exactParsers` lists to opt in
    languages = ("typescript", "javascript")

    def __init__(self, ts, tst, tsj, versions: Dict[str, Optional[str]]) -> None:
        self._langs = {"ts": ts.Language(tst.language_typescript()), "tsx": ts.Language(tst.language_tsx()),
                       "js": ts.Language(tsj.language())}
        self._parser_cls = ts.Parser
        raw = (f"tree-sitter-v1.core-{versions['tree-sitter']}.ts-{versions['tree-sitter-typescript']}"
               f".js-{versions['tree-sitter-javascript']}")
        self.basis = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:120]

    def _parser(self, path: str):
        p = path.lower()
        key = "tsx" if p.endswith(".tsx") else "ts" if p.endswith((".ts", ".mts", ".cts")) else "js"
        return self._parser_cls(self._langs[key])

    def fingerprint(self, path: str, text: str) -> Optional[Dict[str, Dict[str, str]]]:
        src = text.encode("utf-8")
        tree = self._parser(path).parse(src)
        root = tree.root_node
        if root.has_error:
            return None  # never fingerprint a tree that is not a complete parse
        found: Dict[str, List[Dict[str, str]]] = {}  # every declaration of a name; a repeated name is never fingerprinted

        def add(name: str, fp: Dict[str, str]) -> None:
            found.setdefault(name, []).append(fp)

        for child in root.named_children:
            node = child
            if node.type == "export_statement":
                node = node.child_by_field_name("declaration")
                if node is None:
                    continue
            t = node.type
            if t in _FUNC_DECLS:
                name = _name(node, src)
                if name:
                    add(name, self._func(node, node, src))
            elif t in ("lexical_declaration", "variable_declaration"):
                for d in node.named_children:
                    if d.type != "variable_declarator":
                        continue
                    name, value = _name(d, src), d.child_by_field_name("value")
                    if name and value is not None and value.type in _FUNC_VALUES:
                        add(name, self._func(d, value, src))
                    elif name:
                        add(name, {"ast": "", "sig": ""})  # another binding of the name: blocks it, never anchors it
            elif t in _CLASS_DECLS:
                name = _name(node, src)
                if not name:
                    continue
                body = node.child_by_field_name("body")
                methods, ctor = [], None
                for m in (body.named_children if body is not None else []):
                    if m.type not in ("method_definition", "abstract_method_signature", "public_field_definition",
                                      "field_definition", "method_signature"):
                        continue
                    mname = _name(m, src) or _field_name(m, src)
                    if not mname:
                        continue
                    if mname == "constructor" and m.type == "method_definition":
                        ctor = m
                        continue
                    if m.type in ("method_definition", "abstract_method_signature", "method_signature"):
                        fp = self._func(m, m, src)
                        if not mname.startswith(("_", "#")):
                            methods.append(mname)
                    else:
                        fp = {"ast": "", "sig": ""}  # a field of the same name
                    add(f"{name}.{mname}", fp)  # a getter and a setter, or a repeated method, give two entries
                params = ctor.child_by_field_name("parameters") if ctor is not None else None
                add(name, {"ast": _h(_dump(node, src)),
                           "sig": _h({"kind": "class", "methods": sorted(set(methods)),
                                      "ctor": _dump(params, src) if params is not None else None})})
            elif t == "interface_declaration":
                name = _name(node, src)
                body = node.child_by_field_name("body")
                if name:
                    add(name, {"ast": _h(_dump(node, src)),
                               "sig": _h({"kind": "interface", "body": _dump(body, src) if body is not None else None})})
            elif t == "type_alias_declaration":
                name = _name(node, src)
                value = node.child_by_field_name("value")
                if name:
                    fp = _h({"kind": "type", "value": _dump(value, src) if value is not None else None})
                    add(name, {"ast": _h(_dump(node, src)), "sig": fp})
            elif t in ("enum_declaration", "module", "internal_module"):
                name = _name(node, src)
                if name:
                    add(name, {"ast": "", "sig": ""})
        bindings = _binding_counts(root, src)
        if bindings is None:
            return None  # an identifier escape that cannot be decoded: fail closed for the whole file
        # a name bound anywhere else in the file (assignment at any depth, a nested or conditional declaration, a
        # parameter, an import, an escaped spelling of it) gets no exact fingerprint: the status then falls back to
        # the non-exact path and can never be `active` for a rebinding the parser did not attribute
        out = {name: fps[0] for name, fps in found.items()
               if len(fps) == 1 and fps[0]["ast"] and bindings.get(name.rsplit(".", 1)[-1], 0) <= 1}
        short: Dict[str, List[str]] = {}
        for name in found:
            if "." in name:
                short.setdefault(name.rsplit(".", 1)[-1], []).append(name)
        for mname, quals in short.items():  # the lexical scanner's short alias of a method declared once in this file
            if mname not in found and len(quals) == 1 and quals[0] in out:
                out[mname] = out[quals[0]]
        return out

    def _func(self, decl, fn, src) -> Dict[str, str]:
        sig = {"kind": "function", "async": any(c.type == "async" for c in fn.children)}
        for field in ("type_parameters", "parameters", "return_type"):
            n = fn.child_by_field_name(field)
            if n is None and field == "parameters":
                n = fn.child_by_field_name("parameter")  # `x => ...`
            sig[field] = _dump(n, src) if n is not None else None
        return {"ast": _h(_dump(decl, src)), "sig": _h(sig)}


def _name(node, src: bytes) -> Optional[str]:
    n = node.child_by_field_name("name")
    if n is None:
        return None
    name = src[n.start_byte:n.end_byte].decode("utf-8", "replace")
    return name if re.fullmatch(r"#?[A-Za-z_$][\w$]*", name) else None


_ESCAPE_RE = re.compile(r"\\u(?:\{([0-9a-fA-F]{1,6})\}|([0-9a-fA-F]{4}))")
_ID_TYPES = {"identifier", "property_identifier", "shorthand_property_identifier", "private_property_identifier",
             "type_identifier", "shorthand_property_identifier_pattern", "statement_identifier"}
# (parent type, field) pairs and parent types whose identifier child is a binding of that name
_BINDING_FIELDS = {("function_declaration", "name"), ("generator_function_declaration", "name"),
                   ("function_expression", "name"), ("function", "name"), ("class_declaration", "name"),
                   ("abstract_class_declaration", "name"), ("class", "name"), ("variable_declarator", "name"),
                   ("assignment_expression", "left"), ("augmented_assignment_expression", "left"),
                   ("method_definition", "name"), ("method_signature", "name"), ("abstract_method_signature", "name"),
                   ("public_field_definition", "name"), ("field_definition", "property"), ("pair", "key"),
                   ("for_in_statement", "left"), ("catch_clause", "parameter"), ("required_parameter", "pattern"),
                   ("optional_parameter", "pattern"), ("interface_declaration", "name"),
                   ("type_alias_declaration", "name"), ("enum_declaration", "name"), ("import_specifier", "name"),
                   ("import_specifier", "alias"), ("namespace_import", None), ("import_clause", None),
                   ("export_specifier", "alias"), ("rest_pattern", None), ("assignment_pattern", "left"),
                   ("formal_parameters", None), ("array_pattern", None), ("object_pattern", None),
                   ("pair_pattern", "value"), ("object_assignment_pattern", "left")}
_BINDING_PARENTS = {pt for pt, f in _BINDING_FIELDS if f is None}


def _decode(name: str) -> Optional[str]:
    if "\\" not in name:
        return name
    def rep(m):
        cp = int(m.group(1) or m.group(2), 16)
        if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
            raise ValueError("bad escape")
        return chr(cp)
    try:
        out = _ESCAPE_RE.sub(rep, name)
    except ValueError:
        return None
    return None if "\\" in out else out


def _binding_counts(root, src: bytes) -> Optional[Dict[str, int]]:
    """How often each (escape-decoded) name is bound anywhere in the file; None when an escape cannot be decoded."""
    counts: Dict[str, int] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        for i in range(node.child_count - 1, -1, -1):
            stack.append(node.children[i])
        if node.type not in _ID_TYPES:
            continue
        parent = node.parent
        if parent is None:
            continue
        field = None
        for i in range(parent.child_count):
            if parent.children[i].id == node.id:
                field = parent.field_name_for_child(i)
                break
        member_assign = (parent.type == "member_expression" and field == "property" and parent.parent is not None
                         and parent.parent.type in ("assignment_expression", "augmented_assignment_expression"))
        if not ((parent.type, field) in _BINDING_FIELDS or parent.type in _BINDING_PARENTS or member_assign):
            continue
        name = _decode(src[node.start_byte:node.end_byte].decode("utf-8", "replace"))
        if name is None:
            return None
        counts[name] = counts.get(name, 0) + 1
    return counts


def _field_name(node, src: bytes) -> Optional[str]:
    n = node.child_by_field_name("property") or node.child_by_field_name("name")
    if n is None:
        return None
    name = src[n.start_byte:n.end_byte].decode("utf-8", "replace")
    return name if re.fullmatch(r"#?[A-Za-z_$][\w$]*", name) else None


def _dump(node, src: bytes) -> str:
    """Canonical rendering of a syntax tree: node types and leaf token text, comments and `;` dropped (so layout,
    comments and optional semicolons never change a hash; every token that changes behaviour does). Iterative."""
    out: List[str] = []
    stack = [(node, False)]
    while stack:
        n, closing = stack.pop()
        if closing:
            out.append(")")
            continue
        if n.type == "comment":
            continue
        if n.child_count == 0:
            if not n.is_named and n.type == ";":
                continue
            out.append(n.type + "=" + json.dumps(src[n.start_byte:n.end_byte].decode("utf-8", "replace")) + " ")
            continue
        out.append("(" + n.type + " ")
        stack.append((n, True))
        for c in reversed(n.children):
            stack.append((c, False))
    return "".join(out)
