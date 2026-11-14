"""Unit Q5 regression tests for Memlet read-state fixes (items 1-9)."""

from __future__ import annotations

import collections
import collections.abc
import enum
import gc
import importlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import warnings
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import memlet_mcp
import memory_intelligence as mi
import memory_skills as ms
import work_memory as ledger
from memlet import audit
from memlet import memlet_bridge as mb
from memlet import memlet_dream as md
from memlet import memlet_dreamcycle as mdc
from memlet import memlet_dreamtidy as mdt
from memlet import memlet_graph as mg
from memlet import memlet_hooks as mh
from memlet import memlet_skillbook as msb
from memlet import memlet_tidy as mt
from memlet import memlet_wiki as mw
from memlet import standalone
import test_v94_readstate as trs


if hasattr(enum, "StrEnum"):
    class _SampleStrEnum(enum.StrEnum):
        BILLING = "billing.py"
        LOCAL = "local"
        REFUND = "refund"
else:
    class _SampleStrEnum(str, enum.Enum):
        BILLING = "billing.py"
        LOCAL = "local"
        REFUND = "refund"


class _StrSub(str):
    pass


class _BytesSub(bytes):
    pass


def _ceiling_dirs(scratch: Path) -> str:
    roots = {str(scratch.resolve()), str(scratch.resolve().parent), str(Path(tempfile.gettempdir()).resolve())}
    return os.pathsep.join(sorted(roots))


def _isolated_env(scratch: Path, extra: dict | None = None) -> dict:
    # Preserve PYTHONHOME and related interpreter variables per test contract.
    env = dict(os.environ)
    iso = (scratch / "iso_env").resolve()
    for sub in ("home", "tmp", "cfg", "codex", "store", "locks"):
        (iso / sub).mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(iso / "home"),
        "TMPDIR": str(iso / "tmp"),
        "CLAUDE_CONFIG_DIR": str(iso / "cfg"),
        "CODEX_HOME": str(iso / "codex"),
        "MEMLET_HOME": str(iso / "store"),
        "MEMLET_LOCK_DIR": str(iso / "locks"),
        "GIT_CEILING_DIRECTORIES": _ceiling_dirs(scratch),
        "GIT_CONFIG_NOSYSTEM": "1",
        "TZ": "UTC",
    })
    if extra:
        env.update(extra)
    return env


def _extract_commit_tree(commit: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    git_repo = Path(os.environ.get("MEMLET_GIT_REPO", str(HERE))).resolve()
    archive = subprocess.Popen(
        ["git", "-C", str(git_repo), "archive", commit],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    untar = subprocess.Popen(
        ["tar", "-x", "-C", str(dest)],
        stdin=archive.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    archive.stdout.close()
    _, untar_err = untar.communicate(timeout=60)
    _, arch_err = archive.communicate(timeout=60)
    if archive.returncode != 0 or untar.returncode != 0:
        raise AssertionError(
            f"git archive {commit} failed (never skip): "
            f"{arch_err.decode('utf-8', 'replace')} / {untar_err.decode('utf-8', 'replace')}"
        )
    return dest


def _wrap_mapping(value, kind: str):
    if isinstance(value, dict):
        if kind == "OrderedDict":
            return collections.OrderedDict(value)
        if kind == "defaultdict":
            dd = collections.defaultdict(lambda: None)
            dd.update(value)
            return dd
        if kind == "MappingProxyType":
            return types.MappingProxyType(dict(value))
    return value


def _canon_result(value, repo: Path):
    if isinstance(value, collections.abc.Mapping) and not isinstance(value, dict):
        value = dict(value)
    return trs._canon(value, repo)


def _str_enum_for(value: str):
    if hasattr(enum, "StrEnum"):
        DynamicEnum = enum.StrEnum("DynamicEnum", {"VAL": value})
    else:
        class _Py39StrEnum(str, enum.Enum):
            def __str__(self):
                return str.__str__(self)
        DynamicEnum = _Py39StrEnum("DynamicEnum", {"VAL": value})
    return DynamicEnum.VAL


_NONEXACT_PROBE = r"""
import collections, enum, json, shutil, sys, types
from pathlib import Path

code_dir, fixture_repo, work_dir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
sys.path.insert(0, str(code_dir))

from memlet import memlet_bridge as mb, memlet_dream as md, memlet_graph as mg, memlet_hooks as mh, memlet_wiki as mw

if hasattr(enum, "StrEnum"):
    class SE(enum.StrEnum):
        FILE = "billing.py"
        SCOPE = "local"
        QUERY = "refund"
else:
    class SE(str, enum.Enum):
        def __str__(self):
            return str.__str__(self)
        FILE = "billing.py"
        SCOPE = "local"
        QUERY = "refund"

class S(str):
    pass

repo = (work_dir / "repo").resolve()
shutil.rmtree(repo, ignore_errors=True)
shutil.copytree(fixture_repo, repo, symlinks=True)

qtext = (repo / mb.QUEUE_REL).read_text(encoding="utf-8")
G = json.loads(qtext)
OG = json.loads(qtext, object_pairs_hook=collections.OrderedDict)
snap = md.load_snapshot(repo / mb.SNAPSHOT_REL)
osnap = collections.OrderedDict(snap)
dsnap = collections.defaultdict(list, snap)
cfg = mw.read_config(repo)[0] or {}
ocfg = json.loads(json.dumps(cfg), object_pairs_hook=collections.OrderedDict)
cfg_mp = types.MappingProxyType(dict(cfg))

def canon(v):
    if isinstance(v, Path):
        return str(v.resolve()).replace(str(repo), "<R>")
    if isinstance(v, dict):
        return {
            str(k): canon(x)
            for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))
            if k not in ("generatedAt", "elapsedMs", "at", "updatedAt", "date", "head", "wallSeconds", "recallId")
        }
    if isinstance(v, (list, tuple)):
        return [canon(x) for x in v]
    if isinstance(v, set):
        return sorted(map(str, v))
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v.replace(str(repo), "<R>") if isinstance(v, str) else v
    return "<" + type(v).__name__ + ">"

CASES = {
    # All 17 cases from q4_nonexact_types.py
    "approved_view(graph=dict)": lambda: md.approved_view(G),
    "approved_view(graph=OrderedDict)": lambda: md.approved_view(OG),
    "recall_subgraph(graph=OrderedDict)": lambda: md.recall_subgraph(OG, query_text="refund", snapshot_files=snap),
    "recall_subgraph(snapshot=OrderedDict)": lambda: md.recall_subgraph(G, query_text="refund", snapshot_files=osnap),
    "recall_subgraph(snapshot=defaultdict)": lambda: md.recall_subgraph(G, query_text="refund", snapshot_files=dsnap),
    "format_flat_memory(graph=OrderedDict)": lambda: md.format_flat_memory(OG, snapshot_files=snap),
    "citation_failures(snapshot=MappingProxy)": lambda: md.citation_failures(G, types.MappingProxyType(snap)),
    "dream_tiers(config=OrderedDict)": lambda: mb.dream_tiers(G, snapshot=snap, config=ocfg),
    "dream_tiers(graph=OrderedDict)": lambda: mb.dream_tiers(OG, snapshot=snap, config=cfg),
    "item_tier(node=OrderedDict)": lambda: mb.item_tier(OG["nodes"][0], snapshot=snap, config=cfg),
    "resolve_pin(span=str subclass)": lambda: md.resolve_pin(snap, S(next(iter(snap)) + ":1")),
    "dream_items(query=str subclass)": lambda: mb.dream_items(repo, query=S("refund")),
    "unified_recall(query=str subclass)": lambda: mb.unified_recall(repo, S("refund")),
    "neighbors(target=str subclass)": lambda: mg.neighbors(repo, S("billing.py")),
    "build_handover(next_step=str subclass)": lambda: mw.build_handover(repo, next_step=S("ship the refund fix")),
    "classify_anchor(anchor=OrderedDict)": lambda: mw.classify_anchor(
        repo, collections.OrderedDict(path="billing.py", symbol="refund", raw="billing.py::refund")
    ),
    "dream_items(repo_path=str subclass)": lambda: mb.dream_items(S(str(repo))),
    # All 8 cases from repro_7e8c8a1_stdlib_mappings_and_enums.py
    "carbon_find_root(OrderedDict)": lambda: mh.find_root(collections.OrderedDict([("cwd", str(repo))]), start=repo),
    "carbon_find_root(MappingProxyType)": lambda: mh.find_root(types.MappingProxyType({"cwd": str(repo)}), start=repo),
    "carbon_plan_claude(cfg=MappingProxyType)": lambda: Path(mh.plan_claude(repo, scope="local", cfg=types.MappingProxyType({"hooks": {}}))["path"]).name,
    "carbon_item_tier(OrderedDict,MappingProxy)": lambda: mb.item_tier(OG["nodes"][0], osnap, cfg_mp),
    "carbon_citation_review(OrderedDict,MappingProxy)": lambda: md.citation_review(OG["nodes"][0], types.MappingProxyType(snap)) == md.citation_review(G["nodes"][0], snap),
    "carbon_neighbors(StrEnum)": lambda: {"ok": mg.neighbors(repo, SE.FILE)["ok"], "reason": mg.neighbors(repo, SE.FILE).get("reason")},
    "carbon_plan_claude(scope=StrEnum)": lambda: Path(mh.plan_claude(repo, scope=SE.SCOPE)["path"]).name,
    "carbon_unified_recall(query=StrEnum)": lambda: str(mb.unified_recall(repo, query=SE.QUERY)["query"]) == "refund",
}
out = {}
for k, f in CASES.items():
    try:
        out[k] = canon(f())
    except Exception as exc:
        out[k] = "ERROR " + type(exc).__name__ + ": " + str(exc)[:150]
print(json.dumps(out, sort_keys=True))
"""


class TestQ5Defects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(tempfile.mkdtemp(prefix="memlet_v95_q5_")).resolve()
        cls.env = _isolated_env(cls.scratch)
        cls.fixture = cls.scratch / "base_fixture"
        cls.base_ctx = trs._build_fixture_repo(cls.fixture, cls.env)
        cls.tree_0ca681f = _extract_commit_tree("0ca681f", cls.scratch / "tree_0ca681f")
        cls.tree_eedbd85 = _extract_commit_tree("eedbd85", cls.scratch / "tree_eedbd85")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def _clone_fixture(self, name: str) -> dict:
        return trs._clone_ctx(self.base_ctx, (self.scratch / name).resolve())

    # ------------------------------------------------------------------
    # Fix 1 (MF-1 / Q4-M1 + TG2)
    # ------------------------------------------------------------------
    def test_fix1_materialise_read_argument_preserves_collections_and_materialises_iterators(self):
        class CustomMap(collections.abc.Mapping):
            def __init__(self, data):
                self._d = dict(data)
            def __getitem__(self, k):
                return self._d[k]
            def __iter__(self):
                return iter(self._d)
            def __len__(self):
                return len(self._d)

        class CustomSeq(collections.abc.Sequence):
            def __init__(self, items):
                self._items = tuple(items)
            def __getitem__(self, idx):
                return self._items[idx]
            def __len__(self):
                return len(self._items)

        class CustomSet(collections.abc.Set):
            def __init__(self, items):
                self._items = frozenset(items)
            def __contains__(self, x):
                return x in self._items
            def __iter__(self):
                return iter(self._items)
            def __len__(self):
                return len(self._items)

        class CustomIterable:
            def __iter__(self):
                yield "approved"
                yield "hint"

        class BrokenIterable:
            def __iter__(self):
                raise RuntimeError("boom")

        preserved = [
            _StrSub("billing.py"),
            _SampleStrEnum.BILLING,
            _BytesSub(b"abc"),
            bytearray(b"xyz"),
            memoryview(b"xyz"),
            collections.OrderedDict([("a", 1), ("b", 2)]),
            collections.defaultdict(int, {"a": 1}),
            types.MappingProxyType({"a": 1}),
            CustomMap({"a": 1}),
            CustomSeq(["a", "b"]),
            CustomSet(["a", "b"]),
            collections.namedtuple("Pair", ["x", "y"])("a", "b"),
        ]
        for obj in preserved:
            with self.subTest(kind=type(obj).__name__):
                self.assertIs(md._materialise_read_argument(obj), obj)

        gen = (x for x in ("approved", "hint"))
        self.assertEqual(md._materialise_read_argument(gen), ["approved", "hint"])
        self.assertEqual(md._materialise_read_argument(CustomIterable()), ["approved", "hint"])

        broken = BrokenIterable()
        self.assertIs(md._materialise_read_argument(broken), broken)

    def test_fix1_tg2_recipe_table_mapping_and_string_subtypes(self):
        recipes, mutating = trs._build_recipes()
        recipes.update({
            ("memlet_dreamtidy", "proposal_digest"): lambda c: mdt.proposal_digest(["digest"]),
            ("memlet_dreamtidy", "proposal_id"): lambda c: mdt.proposal_id(["digest"]),
            ("memlet_dreamtidy", "lessons_by_id_from_loaded"): lambda c: mdt.lessons_by_id_from_loaded({"a": {}}, {}, {}, duplicate_ids=()),
            ("memlet_tidy", "command"): lambda c: mt.command(["tidy"], [], repo="."),
            ("memlet_tidy", "kept_lesson_order"): lambda c: mt.kept_lesson_order(
                {"L1": {"tier": "approved", "current": []}}, ("L1",)
            ),
            ("memlet_wiki", "first_managed_problem"): lambda c: mw.first_managed_problem(c["repo"], [".memlet/config.json"]),
        })
        module_map = {
            "audit": audit,
            "code_anchors": importlib.import_module("memlet.code_anchors"),
            "indexing": importlib.import_module("memlet.indexing"),
            "memlet_bridge": mb,
            "memlet_brief": importlib.import_module("memlet.memlet_brief"),
            "memlet_cortex": importlib.import_module("memlet.memlet_cortex"),
            "memlet_dream": md,
            "memlet_dreamcycle": mdc,
            "memlet_dreamtidy": mdt,
            "memlet_eval": importlib.import_module("memlet.memlet_eval"),
            "memlet_graph": mg,
            "memlet_hooks": mh,
            "memlet_index": importlib.import_module("memlet.memlet_index"),
            "memlet_inspector": importlib.import_module("memlet.memlet_inspector"),
            "memlet_mcp": memlet_mcp,
            "memlet_recall": importlib.import_module("memlet.memlet_recall"),
            "memlet_skillbook": msb,
            "memlet_slim": importlib.import_module("memlet.memlet_slim"),
            "memlet_tidy": mt,
            "memlet_wiki": mw,
            "memory_cli": importlib.import_module("memory_cli"),
            "memory_intelligence": mi,
            "memory_skills": ms,
            "paths": importlib.import_module("memlet.paths"),
            "repo_benchmark": importlib.import_module("memlet.repo_benchmark"),
            "standalone": standalone,
            "ts_exact": importlib.import_module("memlet.ts_exact"),
            "vendor_cli": importlib.import_module("memlet.vendor_cli"),
        }
        # Internal ontology structures whose 9.3.0/9.4.0 bodies explicitly check isinstance(x, dict),
        # copy.deepcopy(x), or json.dumps(x) (so they take dict subclasses rather than general Mapping).
        dict_only_ontology_params = {
            ("memlet_bridge", "dream_tiers", "snapshot"),
            ("memlet_bridge", "item_tier", "node"),
            ("memlet_bridge", "item_tier", "snapshot"),
            ("memlet_bridge", "item_trust", "node"),
            ("memlet_bridge", "item_trust", "snapshot"),
            ("memlet_bridge", "lesson_paths", "node"),
            ("memlet_dream", "approved_view", "graph"),
            ("memlet_dream", "build_cortex_lineage_payload", "graph"),
            ("memlet_dream", "citation_failures", "snapshot"),
            ("memlet_dream", "citation_review", "snapshot"),
            ("memlet_dream", "citation_review_view", "graph"),
            ("memlet_dream", "citation_review_view", "snapshot"),
            ("memlet_dream", "citation_verified_graph", "graph"),
            ("memlet_dream", "citation_verified_graph", "snapshot"),
            ("memlet_dream", "format_flat_memory", "graph"),
            ("memlet_dream", "format_flat_memory", "snapshot_files"),
            ("memlet_dream", "list_pending", "snapshot_files"),
            ("memlet_dream", "recall_subgraph", "graph"),
            ("memlet_dream", "recall_subgraph", "snapshot_files"),
            ("memlet_dream", "resolve_pin", "snapshot_files"),
            ("memlet_dream", "verify_ontology_contract", "graph"),
            ("memlet_dream", "verify_ontology_contract", "snapshot_files"),
            ("memlet_dream", "write_merge_decisions", "graph"),
            ("memlet_dream", "write_ontology_sqlite", "graph"),
            ("memlet_dream", "write_ontology_sqlite", "snapshot_files"),
        }
        c_base = self._clone_fixture("tg2_shared")
        checked_mappings = 0
        checked_mapping_proxies_plain = 0
        checked_strings = 0
        mut_seq = 0

        def next_ctx(key):
            nonlocal mut_seq
            if key in mutating:
                mut_seq += 1
                return self._clone_fixture(f"tg2_mut_{mut_seq}")
            return c_base

        with mock.patch.dict(os.environ, self.env):
            for key, recipe in sorted(recipes.items()):
                mod_key, fn_name = key
                mod = module_map.get(mod_key)
                if mod is None or not hasattr(mod, fn_name):
                    continue
                original = getattr(mod, fn_name)
                if not callable(original):
                    continue
                try:
                    sig = inspect.signature(original)
                except (TypeError, ValueError):
                    continue
                captured = {}

                def spy(*args, _orig=original, _sig=sig, **kwargs):
                    b = _sig.bind(*args, **kwargs)
                    b.apply_defaults()
                    captured["bound"] = b
                    return _orig(*args, **kwargs)

                ctx_plain = next_ctx(key)
                with mock.patch.object(mod, fn_name, spy):
                    plain_raw = recipe(ctx_plain)
                if "bound" not in captured:
                    continue
                plain_ans = _canon_result(plain_raw, ctx_plain["repo"])
                bound_args = dict(captured["bound"].arguments)
                for param_name, param_val in bound_args.items():
                    param_obj = sig.parameters[param_name]
                    if param_obj.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                        continue
                    if isinstance(param_val, dict):
                        checked_mappings += 1
                        mkinds = ("OrderedDict", "defaultdict") if (mod_key, fn_name, param_name) in dict_only_ontology_params else ("OrderedDict", "defaultdict", "MappingProxyType")
                        for mkind in mkinds:
                            if mkind == "MappingProxyType":
                                checked_mapping_proxies_plain += 1
                            with self.subTest(fn=f"{mod_key}.{fn_name}", param=param_name, kind=mkind):
                                def replace_map(*a, _orig=original, _sig=sig, _p=param_name, _k=mkind, **kw):
                                    b2 = _sig.bind(*a, **kw)
                                    b2.apply_defaults()
                                    b2.arguments[_p] = _wrap_mapping(b2.arguments[_p], _k)
                                    return _orig(*b2.args, **b2.kwargs)
                                ctx_cur = next_ctx(key)
                                with mock.patch.object(mod, fn_name, replace_map):
                                    got = _canon_result(recipe(ctx_cur), ctx_cur["repo"])
                                self.assertEqual(got, plain_ans)
                    elif isinstance(param_val, str):
                        checked_strings += 1
                        for skind in ("StrSub", "StrEnum"):
                            with self.subTest(fn=f"{mod_key}.{fn_name}", param=param_name, kind=skind):
                                def replace_str(*a, _orig=original, _sig=sig, _p=param_name, _sk=skind, **kw):
                                    b2 = _sig.bind(*a, **kw)
                                    b2.apply_defaults()
                                    cur_str = b2.arguments[_p]
                                    b2.arguments[_p] = _StrSub(cur_str) if _sk == "StrSub" else _str_enum_for(cur_str)
                                    return _orig(*b2.args, **b2.kwargs)
                                ctx_cur = next_ctx(key)
                                with mock.patch.object(mod, fn_name, replace_str):
                                    got = _canon_result(recipe(ctx_cur), ctx_cur["repo"])
                                self.assertEqual(got, plain_ans)
        self.assertGreaterEqual(checked_mappings, 50)
        self.assertGreaterEqual(checked_mapping_proxies_plain, 25)
        self.assertGreaterEqual(checked_strings, 60)

    def test_fix1_reproductions_equal_0ca681f(self):
        def run_probe(code_tree: Path, tag: str) -> dict:
            wdir = (self.scratch / f"repro_{tag}").resolve()
            wdir.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                [sys.executable, "-B", "-c", _NONEXACT_PROBE, str(code_tree), str(self.fixture), str(wdir)],
                cwd=str(wdir),
                env=self.env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)

        expected = run_probe(self.tree_0ca681f, "0ca681f")
        actual = run_probe(HERE, "current")
        self.assertEqual(len(actual), 25)
        self.assertEqual(actual, expected)

    # ------------------------------------------------------------------
    # Fix 2 & Fix 3 (SF1 + Carbon SF-3.2 + TG3; kills N13 and call_foreign mutant)
    # ------------------------------------------------------------------
    def test_fix2_and_fix3_git_subprocesses_per_operation(self):
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        shim_root = (self.scratch / "git_shim_test").resolve()
        shim_bin = shim_root / "bin"
        shim_bin.mkdir(parents=True, exist_ok=True)
        shim_script = shim_bin / "git"
        shim_script.write_text(
            f'#!/bin/sh\nif [ -n "$MEMLET_GIT_SHIM_LOG" ]; then printf "%s\\n" "$*" >> "$MEMLET_GIT_SHIM_LOG"; fi\nexec "{real_git}" "$@"\n',
            encoding="utf-8",
        )
        shim_script.chmod(0o755)

        carbon_8e3be5b_caps = {
            "check": 1,
            "check-changed": 3,
            "recall": 1,
            "recall-files": 1,
            "handover": 4,
            "pending": 0,
            "tidy-json": 3,
            "tidy-md": 3,
            "graph-stats": 1,
            "graph-show": 1,
            "skillbook": 2,
            "tidy-status": 1,
            "session-start": 6,
            "pre-tool-use": 2,
            "post-tool-use": 2,
            "pre-compact": 1,
            "stop": 1,
        }
        cli_cmds = {
            "check": ["check"],
            "check-changed": ["check", "--changed"],
            "recall": ["recall", "refund"],
            "recall-files": ["recall", "refund", "--files", "billing.py"],
            "handover": ["handover"],
            "pending": ["learn", "--pending"],
            "tidy-json": ["admin", "tidy", "--format", "json"],
            "tidy-md": ["admin", "tidy", "--format", "markdown"],
            "graph-stats": ["admin", "graph", "stats"],
            "graph-show": ["admin", "graph", "show", "billing.py"],
            "skillbook": ["admin", "skillbook"],
            "tidy-status": ["dream", "--tidy", "--status"],
        }
        hooks = {
            "session-start": {"hook_event_name": "SessionStart", "source": "startup"},
            "pre-tool-use": {
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": "billing.py", "old_string": "a", "new_string": "b"},
            },
            "post-tool-use": {
                "hook_event_name": "PostToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": "billing.py", "content": "def refund(order_id):\n    return order_id\n"},
            },
            "pre-compact": {"hook_event_name": "PreCompact", "trigger": "manual"},
            "stop": {"hook_event_name": "Stop", "stop_hook_active": False},
        }

        def measure(tree: Path, tag: str) -> dict:
            counts = {}
            for name, argv in list(cli_cmds.items()) + [(k, None) for k in hooks]:
                repo = (shim_root / tag / name).resolve()
                shutil.rmtree(repo, ignore_errors=True)
                shutil.copytree(self.fixture, repo, symlinks=True)
                if name == "check-changed":
                    bp = repo / "billing.py"
                    bp.write_text(bp.read_text(encoding="utf-8") + "\n# modified\n", encoding="utf-8")
                log_path = shim_root / f"{tag}_{name}.log"
                log_path.write_text("", encoding="utf-8")
                env = _isolated_env(shim_root / f"env_{tag}_{name}", {
                    "PATH": str(shim_bin) + os.pathsep + os.environ.get("PATH", ""),
                    "MEMLET_GIT_SHIM_LOG": str(log_path),
                })
                if argv is None:
                    payload = dict(hooks[name], session_id=f"q5-{name}", cwd=str(repo), transcript_path=str(shim_root / "none.jsonl"))
                    if "tool_input" in payload:
                        payload["tool_input"] = dict(payload["tool_input"], file_path=str(repo / "billing.py"))
                    cmd = ["admin", "hook", name]
                    stdin_data = json.dumps(payload)
                else:
                    cmd = (argv + ["--repo", str(repo)]) if argv[0] != "dream" else (argv + [str(repo)])
                    stdin_data = ""
                proc = subprocess.run(
                    [sys.executable, "-B", str(tree / "memory_cli.py")] + cmd,
                    cwd=str(repo),
                    env=env,
                    input=stdin_data,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                self.assertIn(proc.returncode, (0, 1), proc.stderr)
                invocations = [ln.strip() for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                tracked_ls = sum(1 for ln in invocations if "ls-files -z" in ln)
                counts[name] = (len(invocations), tracked_ls, invocations)
            return counts

        eed_counts = measure(self.tree_eedbd85, "eedbd85")
        cur_counts = measure(HERE, "current")
        for op, (total_git, tracked_ls, invocations) in cur_counts.items():
            eed_total = eed_counts[op][0]
            cap_8e3 = carbon_8e3be5b_caps[op]
            with self.subTest(operation=op):
                self.assertLessEqual(total_git, eed_total, f"{op}: {total_git} > eedbd85 ({eed_total}): {invocations}")
                self.assertLessEqual(total_git, cap_8e3, f"{op}: {total_git} > 8e3be5b ({cap_8e3}): {invocations}")
                self.assertLessEqual(tracked_ls, 1, f"{op} ran tracked `git ls-files -z` {tracked_ls} times: {invocations}")

    def test_fix3_owned_git_and_foreign_subprocess_classification(self):
        # 1. Owned git helpers exist and neither suspend read state nor drop current evidence.
        self.assertTrue(hasattr(md, "_run_owned_git"))
        self.assertTrue(hasattr(md, "_read_owned_git_stream"))
        c = self._clone_fixture("fix3_evidence")
        repo = c["repo"]
        with mock.patch.dict(os.environ, self.env):
            for label, reader in (
                ("build_handover", lambda: mw.build_handover(repo)),
                ("changed_check", lambda: mb.changed_check(repo)),
                ("unified_recall", lambda: mb.unified_recall(repo, "refund")),
                ("tidy_report", lambda: mt.tidy_report(repo)),
                ("build_skill", lambda: msb.build_skill(repo)),
            ):
                counters = {}
                with self.subTest(reader=label):
                    with md.work_counters(counters):
                        res = reader()
                    self.assertTrue(isinstance(res, dict))
                    self.assertEqual(counters.get("foreignCallsWithEvidence", 0), 0, counters)
                    self.assertLessEqual(counters.get("queueDiskLoads", 0), 1, counters)

            # 2. Foreign subprocess site (mh.probe_command) MUST suspend read state and forget evidence.
            observed_states = []
            def fake_run(*_a, **_kw):
                observed_states.append(md._active_read_state())
                return subprocess.CompletedProcess(
                    ["sh"], 0, stdout=json.dumps({"memletHook": mh.HOOK_PROTOCOL}).encode("utf-8"), stderr=b""
                )

            with md._enter_read_scope():
                mb.dream_items(repo)
                state_before = md._active_read_state()
                self.assertTrue(bool(state_before._buckets))
                with mock.patch.object(mh.subprocess, "run", fake_run):
                    self.assertTrue(mh.probe_command("echo ok", repo))
                self.assertEqual(observed_states, [None])
                self.assertEqual(state_before._buckets, {})

    # ------------------------------------------------------------------
    # Fix 4 (Carbon SF-3.1: detect_git_branch outside per-item loops)
    # ------------------------------------------------------------------
    def test_fix4_detect_git_branch_outside_loops(self):
        def run_scale(n_items: int) -> dict:
            wdir = (self.scratch / f"fix4_scale_{n_items}").resolve()
            shutil.rmtree(wdir, ignore_errors=True)
            wdir.mkdir(parents=True, exist_ok=True)
            env_local = _isolated_env(wdir / "iso", {"MEMLET_HOME": str(wdir)})
            subprocess.run(["git", "init", "-q", "-b", "feature/q5-branch"], cwd=str(wdir), env=env_local, check=True)
            (wdir / "memory_settings.json").write_text('{"semanticEnabled": false}\n', encoding="utf-8")
            (wdir / "billing.py").write_text(
                "def process_refund(order_id, amount=0):\n    return (order_id, amount)\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, env_local):
                store = standalone.StandaloneStore(wdir)
                task_id = f"task-q5-{n_items}"
                owner = "sess-q5"
                store.create("q5", "Q5 branch loop test", owner, goal="Test branch loop", task=task_id)
                src_text = f"Use {wdir}/billing.py::process_refund safely during retries."
                with store.db(True) as db:
                    ledger.record(db, task_id, 1, "source-q5", "report", "report:q5", "assert", src_text, {"kind": "test-source"}, 1700000000.0)

                head_path = (wdir / ".git" / "HEAD").resolve()
                real_read_text = Path.read_text
                head_reads = [0]

                def counting_read_text(self_path: Path, *a, **kw):
                    try:
                        if self_path.resolve() == head_path:
                            head_reads[0] += 1
                    except OSError:
                        pass
                    return real_read_text(self_path, *a, **kw)

                counts = {}
                with mock.patch.object(Path, "read_text", counting_read_text):
                    # 1. memory_intelligence.learn across n_items entries (hypotheses + synthesized skills)
                    entries = []
                    for i in range(n_items):
                        entries.append({
                            "id": f"hypothesis:q5-{i}",
                            "kind": "hypothesis",
                            "captureMode": "auto-draft",
                            "content": f"Shared consolidation alpha beta gamma delta refund hypothesis cluster note {i % 2} variant {i}",
                            "entities": ["billing_refund"],
                            "sourceRefs": [{"itemId": "report:q5", "quote": src_text}],
                        })
                    for i in range(max(2, n_items // 2)):
                        entries.append({
                            "id": f"experience:q5-{i}",
                            "kind": "experience",
                            "sourceRefs": [{"itemId": "report:q5", "quote": src_text}],
                            "experience": {
                                "situation": f"Handle retry scenario {i} in {wdir}/billing.py::process_refund safely.",
                                "steps": [
                                    f"Call {wdir}/billing.py::process_refund for scenario {i}.",
                                    f"Verify return tuple for scenario {i} before committing.",
                                ],
                                "outcome": f"Scenario {i} refund completes idempotently.",
                                "conditions": f"Module {wdir}/billing.py and process_refund exist for case {i}.",
                                "counterexamples": f"Do not bypass validation in manual override {i}.",
                            },
                            "skill": {
                                "id": f"skill:q5-{i}",
                                "name": f"refund-guard-{i}",
                                "title": f"Refund Guard Skill {i}",
                            },
                        })
                    head_reads[0] = 0
                    learn_res = mi.learn(store, task_id, owner, 1, f"op-learn-{n_items}", entries)
                    self.assertTrue(len(learn_res.get("skills", [])) >= 2, learn_res)
                    counts["learn"] = head_reads[0]

                    # 2. consolidate_memories across multiple clusters
                    head_reads[0] = 0
                    cons_res = mi.consolidate_memories(store, task_id, session=owner, similarity_threshold=0.2, dry_run=False)
                    self.assertTrue(cons_res["ok"])
                    self.assertGreaterEqual(cons_res["consolidatedCount"], 1)
                    counts["consolidate_memories"] = head_reads[0]

                    # 3. memory_skills.sync_skill_markdowns across all co-edited skills
                    skills_dir = wdir / "skills" / task_id
                    if not skills_dir.exists():
                        skills_dir = wdir / ".memlet" / "skills" / task_id
                    md_files = list(wdir.rglob("SKILL.md"))
                    self.assertGreaterEqual(len(md_files), 2)
                    for md_file in md_files:
                        txt_md = md_file.read_text(encoding="utf-8")
                        md_file.write_text(txt_md.replace("# Refund Guard Skill", "# Co-edited Refund Guard Skill", 1), encoding="utf-8")
                    head_reads[0] = 0
                    sync_res = ms.sync_skill_markdowns(store, task_id, session=owner, explicit_user=True)
                    self.assertGreaterEqual(len(sync_res.get("syncedSkills", sync_res.get("updated", []))), 2)
                    counts["sync_skill_markdowns"] = head_reads[0]

                    # 4. heal_skills across compatible code evolution
                    (wdir / "billing.py").write_text(
                        "def process_refund(order_id, amount=0, currency='USD'):\n    return (order_id, amount, currency)\n",
                        encoding="utf-8",
                    )
                    head_reads[0] = 0
                    heal_res = mi.heal_skills(store, task_id, session=owner, dry_run=False)
                    self.assertTrue(heal_res["ok"])
                    counts["heal_skills"] = head_reads[0]

                    # 5. Inspector / benchmark (_score_rows / retrieve loop)
                    head_reads[0] = 0
                    insp_res = mi.inspector(store, task_id, budget=6000)
                    self.assertTrue(isinstance(insp_res, dict) and insp_res.get("ok") and "nodes" in insp_res.get("graph", {}))
                    counts["inspector"] = head_reads[0]
            return counts

        small = run_scale(3)
        large = run_scale(12)
        for op in ("learn", "consolidate_memories", "sync_skill_markdowns", "heal_skills", "inspector"):
            with self.subTest(operation=op):
                self.assertLessEqual(large[op], 1, f"{op} read .git/HEAD {large[op]} times: {large}")
                self.assertEqual(large[op], small[op], f"{op} grew with item count: {small[op]} -> {large[op]}")

    # ------------------------------------------------------------------
    # Fix 5 (SF2: link-graph cache wrapper, weakref, schema_version)
    # ------------------------------------------------------------------
    def test_fix5a_link_cache_duck_typed_wrapper_equals_0ca681f(self):
        probe_0ca = r"""
import json, sqlite3, sys
sys.path.insert(0, sys.argv[1])
import memory_skills as ms

raw = sqlite3.connect(":memory:")
raw.row_factory = sqlite3.Row
ms.ensure_schema(raw)
raw.execute("INSERT INTO memory_links VALUES ('T', 'src', 'dst1', 'related', 1.0, '', 0)")
raw.commit()

class Wrapper:
    def __init__(self, conn):
        self._conn = conn
    def execute(self, *a, **kw):
        return self._conn.execute(*a, **kw)
    def executemany(self, *a, **kw):
        return self._conn.executemany(*a, **kw)
    def executescript(self, *a, **kw):
        return self._conn.executescript(*a, **kw)
    def commit(self):
        return self._conn.commit()

wrapped = Wrapper(raw)
ms._invalidate_task_link_cache()
g1 = sorted(x[0] for x in ms._load_task_link_graph(wrapped, "T")["adj"].get("src", []))
raw.execute("INSERT INTO memory_links VALUES ('T', 'src', 'dst2', 'related', 1.0, '', 0)")
raw.commit()
ms._invalidate_task_link_cache()
g2 = sorted(x[0] for x in ms._load_task_link_graph(wrapped, "T")["adj"].get("src", []))
print(json.dumps([g1, g2]))
"""
        p_0ca = subprocess.run([sys.executable, "-B", "-c", probe_0ca, str(self.tree_0ca681f)], env=self.env, capture_output=True, text=True, timeout=30)
        p_cur = subprocess.run([sys.executable, "-B", "-c", probe_0ca, str(HERE)], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p_0ca.returncode, 0, p_0ca.stderr)
        self.assertEqual(p_cur.returncode, 0, p_cur.stderr)
        self.assertEqual(json.loads(p_cur.stdout), [["dst1"], ["dst1", "dst2"]])
        self.assertEqual(json.loads(p_cur.stdout), json.loads(p_0ca.stdout))

        # Also verify in-process that a duck-typed wrapper skips the cache without invalidation.
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        ms.ensure_schema(raw)
        raw.execute("INSERT INTO memory_links VALUES ('T', 'src', 'dst1', 'related', 1.0, '', 0)")
        raw.commit()

        class LocalWrapper:
            def __init__(self, conn):
                self._conn = conn
            def execute(self, *a, **kw):
                return self._conn.execute(*a, **kw)
            def executescript(self, *a, **kw):
                return self._conn.executescript(*a, **kw)
            def commit(self):
                return self._conn.commit()

        wrapped = LocalWrapper(raw)
        self.assertEqual([x[0] for x in ms._load_task_link_graph(wrapped, "T")["adj"]["src"]], ["dst1"])
        raw.execute("INSERT INTO memory_links VALUES ('T', 'src', 'dst2', 'related', 1.0, '', 0)")
        raw.commit()
        self.assertEqual(sorted(x[0] for x in ms._load_task_link_graph(wrapped, "T")["adj"]["src"]), ["dst1", "dst2"])
        raw.close()

    def test_fix5b_link_cache_weakref_releases_dropped_connections(self):
        wdir = (self.scratch / "fix5b_wal").resolve()
        wdir.mkdir(parents=True, exist_ok=True)
        ms._invalidate_task_link_cache()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            for i in range(40):
                db_path = wdir / f"wal_{i}.sqlite"
                conn = sqlite3.connect(str(db_path))
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                ms.ensure_schema(conn)
                conn.execute("INSERT INTO memory_links VALUES ('T', 'A', ?, 'related', 1.0, '', 0)", (f"B{i}",))
                conn.commit()
                graph = ms._load_task_link_graph(conn, "T")
                self.assertIs(graph, ms._load_task_link_graph(conn, "T"))
                del conn
            gc.collect()
        self.assertEqual(len(ms._TASK_LINK_CACHE), 0)
        open_targets = []
        fd_dir = Path("/dev/fd")
        if fd_dir.is_dir():
            for entry in os.listdir(str(fd_dir)):
                try:
                    target = os.readlink(str(fd_dir / entry))
                except OSError:
                    continue
                if "wal_" in target and ".sqlite" in target:
                    open_targets.append(target)
        self.assertEqual(open_targets, [])

    def test_fix5c_link_cache_schema_version_sees_drop_recreate(self):
        db_path = (self.scratch / "fix5c_drop.sqlite").resolve()
        conn = sqlite3.connect(str(db_path))
        try:
            conn.row_factory = sqlite3.Row
            ms.ensure_schema(conn)
            conn.execute("INSERT INTO memory_links VALUES ('T', 'A', 'OLD', 'related', 1.0, '', 0)")
            conn.commit()
            ms._invalidate_task_link_cache()
            before = [x[0] for x in ms._load_task_link_graph(conn, "T")["adj"].get("A", [])]
            self.assertEqual(before, ["OLD"])
            conn.execute("DROP TABLE memory_links")
            conn.commit()
            after_drop = [x[0] for x in ms._load_task_link_graph(conn, "T")["adj"].get("A", [])]
            self.assertEqual(after_drop, [])
            conn.execute("INSERT INTO memory_links VALUES ('T', 'A', 'NEW', 'related', 1.0, '', 0)")
            conn.commit()
            after_reinsert = [x[0] for x in ms._load_task_link_graph(conn, "T")["adj"].get("A", [])]
            self.assertEqual(after_reinsert, ["NEW"])
        finally:
            ms._invalidate_task_link_cache()
            conn.close()

    # ------------------------------------------------------------------
    # Fix 6 (SF3: caller-supplied _clock read only at deadline checkpoints)
    # ------------------------------------------------------------------
    def test_fix6_caller_clock_suspensions_and_misses_bounded(self):
        def build_scaled_repo(n_lessons: int) -> Path:
            repo = (self.scratch / f"fix6_repo_{n_lessons}").resolve()
            shutil.rmtree(repo, ignore_errors=True)
            shutil.copytree(self.fixture, repo, symlinks=True)
            q_path = repo / mb.QUEUE_REL
            queue = json.loads(q_path.read_text(encoding="utf-8"))
            base_node = queue["nodes"][0]
            nodes = []
            for i in range(n_lessons):
                clone = json.loads(json.dumps(base_node))
                clone["id"] = f"DRM-SCALE-{i:04d}"
                nodes.append(clone)
            queue["nodes"] = nodes
            q_path.write_text(json.dumps(queue, indent=2), encoding="utf-8")
            for k in range(n_lessons):
                (repo / f"mod_{k:03d}.py").write_text(f"def fn_{k}():\n    return {k}\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=str(repo), env=self.env, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "scale"], cwd=str(repo), env=self.env, check=True)
            return repo

        def measure(repo: Path) -> tuple[int, int]:
            clock_calls = [0]
            misses = [0]
            orig_bucket = md._ReadState.bucket

            def counting_bucket(state_self, name, factory=dict):
                if name not in state_self._buckets:
                    misses[0] += 1
                return orig_bucket(state_self, name, factory)

            def caller_clock():
                clock_calls[0] += 1
                return 0.0

            with mock.patch.dict(os.environ, self.env), mock.patch.object(md._ReadState, "bucket", counting_bucket):
                res = mg.neighbors(repo, "billing.py", _clock=caller_clock)
            self.assertTrue(res["ok"])
            return clock_calls[0], misses[0]

        repo_small = build_scaled_repo(25)
        repo_large = build_scaled_repo(140)
        susp_small, miss_small = measure(repo_small)
        susp_large, miss_large = measure(repo_large)
        self.assertLessEqual(susp_large, 12, f"caller clock suspensions too high: {susp_large}")
        self.assertEqual(susp_large, susp_small, f"suspensions grew with size: {susp_small} -> {susp_large}")
        self.assertEqual(miss_large, miss_small, f"bucket misses grew with size: {miss_small} -> {miss_large}")

    # ------------------------------------------------------------------
    # Fix 7 (Carbon SF-2: native TypeError message parity with eedbd85)
    # ------------------------------------------------------------------
    def test_fix7_native_typeerror_equals_eedbd85(self):
        probe = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from memlet import memlet_bridge as mb, memlet_dreamcycle as mdc

out = {}
for name, fn in (
    ("dream_ingest_bad_kw", lambda: mdc.dream_ingest(".", bad_kw="x")),
    ("item_tier_arity", lambda: mb.item_tier({}, None, {})),
):
    try:
        fn()
        out[name] = ["returned", ""]
    except Exception as exc:
        out[name] = [type(exc).__name__, str(exc)]
print(json.dumps(out, sort_keys=True))
"""
        p_eed = subprocess.run([sys.executable, "-B", "-c", probe, str(self.tree_eedbd85)], env=self.env, capture_output=True, text=True, timeout=30)
        p_cur = subprocess.run([sys.executable, "-B", "-c", probe, str(HERE)], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p_eed.returncode, 0, p_eed.stderr)
        self.assertEqual(p_cur.returncode, 0, p_cur.stderr)
        expected = json.loads(p_eed.stdout)
        actual = json.loads(p_cur.stdout)
        self.assertEqual(actual["dream_ingest_bad_kw"][0], "TypeError")
        self.assertEqual(actual["item_tier_arity"][0], "TypeError")
        self.assertEqual(actual, expected)

    # ------------------------------------------------------------------
    # Fix 8 (Caller-passed non-_LessonPathContext treated as absent)
    # ------------------------------------------------------------------
    def test_fix8_invalid_caller_path_context_ignored(self):
        c = self._clone_fixture("fix8_ctx")
        repo = c["repo"]
        with mock.patch.dict(os.environ, self.env):
            expected_items = mb.dream_items(repo)
            expected_changed = mb.changed_check(repo)
            expected_blockers = mb.merge_blockers(repo, c["id_a"], (c["id_b"],))
            for bogus in ({}, {"initialized": False}, "not-a-context", 42, []):
                with self.subTest(bogus=repr(bogus)):
                    got_items = mb.dream_items(repo, _path_context=bogus)
                    got_changed = mb.changed_check(repo, _path_context=bogus)
                    got_blockers = mb.merge_blockers(repo, c["id_a"], (c["id_b"],), context={"pathContext": bogus})
                    self.assertEqual(got_items, expected_items)
                    self.assertEqual(got_changed, expected_changed)
                    self.assertEqual(got_blockers, expected_blockers)

    # ------------------------------------------------------------------
    # Fix 9 (TG4 + single TestV94Differential in test_v94_compat.py)
    # ------------------------------------------------------------------
    def test_fix9_test_hygiene_tg4_and_single_differential_class(self):
        import test_v94_compat
        import test_v94_cost

        self.assertTrue(hasattr(test_v94_compat, "TestV94Differential"))
        self.assertFalse(hasattr(test_v94_cost, "TestV94Differential"))
        self.assertTrue(hasattr(test_v94_cost.TestV94WorkCounts, "test_foreign_calls_with_evidence_constant"))
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(HERE / "test_v94_cost.py"),
                "TestV94WorkCounts.test_foreign_calls_with_evidence_constant",
            ],
            cwd=str(HERE),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
