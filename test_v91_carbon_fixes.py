"""M5 connection ownership and optional tree-sitter minor compatibility regressions."""
import contextlib
import gc
import json
import os
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from memlet import code_anchors as ca
from memlet import memlet_wiki as mw
from memlet import paths, ts_exact


class PathsSqliteClosureTests(unittest.TestCase):
    def test_doctor_backup_restore_without_resource_warnings(self):
        with tempfile.TemporaryDirectory(prefix="m5-paths-") as tmp:
            root = Path(tmp)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                gc.collect()
                self.assertTrue(paths.doctor_environment(data_root=root / "empty")["ok"])
                self.assertFalse((root / "empty" / ".doctor_probe.sqlite3").exists())
                src = root / "source"
                (src / "state").mkdir(parents=True)
                with contextlib.closing(sqlite3.connect(str(src / "state/work.sqlite3"))) as conn:
                    with conn:
                        conn.execute("CREATE TABLE evidence (value TEXT)")
                        conn.execute("INSERT INTO evidence VALUES ('preserved')")
                self.assertTrue(paths.doctor_environment(data_root=src)["ok"])
                backup = root / "backup"
                self.assertTrue(paths.backup_data_root(src, backup)["dbBackedUp"])
                restored = paths.restore_data_root(backup, root / "restored")
                self.assertTrue(restored["integrityOk"])
                with contextlib.closing(sqlite3.connect(restored["restoredDbPath"])) as conn:
                    self.assertEqual([("preserved",)], conn.execute("SELECT value FROM evidence").fetchall())
                gc.collect()
            self.assertEqual([], [w for w in caught if issubclass(w.category, ResourceWarning)])

    def test_probe_connection_closed_before_windows_style_unlink(self):
        opened = []
        connect = sqlite3.connect
        unlink = Path.unlink

        class TrackedConnection(sqlite3.Connection):
            closed = False

            def close(self):
                super().close()
                self.closed = True

        def track(database, *args, **kwargs):
            conn = connect(database, *args, factory=TrackedConnection, **kwargs)
            opened.append((Path(database), conn))
            return conn

        deleted = []

        def exclusive_unlink(path, *args, **kwargs):
            if path.name == ".doctor_probe.sqlite3":
                matching = [conn for db, conn in opened if db == path]
                self.assertTrue(matching)
                if not all(conn.closed for conn in matching):
                    raise PermissionError("WinError 32: database still open")
                deleted.append(path)
            return unlink(path, *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="m5-unlink-") as tmp:
            with mock.patch.object(paths.sqlite3, "connect", side_effect=track), \
                    mock.patch.object(Path, "unlink", exclusive_unlink):
                result = paths.doctor_environment(data_root=Path(tmp))
            self.assertTrue(result["ok"])
            self.assertEqual(1, len(deleted))
            self.assertTrue(all(conn.closed for _, conn in opened))


class TreeSitterMinorCompatibilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="m5-parser-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        home = self.root / "home"
        home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(home), "TMPDIR": tmp.name,
                                         "MEMLET_LOCK_DIR": str(self.root / "locks")})
        env.start()
        self.addCleanup(env.stop)
        for patch in (mock.patch.dict(ca._EXACT_PROVIDERS, {}, clear=True),
                      mock.patch.dict(ca._EXACT_AUTOLOAD, {"done": True})):
            patch.start()
            self.addCleanup(patch.stop)
        ca._EXACT_CACHE.clear()
        self.addCleanup(ca._EXACT_CACHE.clear)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def opt_in(self):
        self.write(".memlet/config.json", json.dumps({"anchors": {"exactParsers": ["tree-sitter"]}}))

    def statuses(self):
        ca._EXACT_CACHE.clear()
        return {a["anchor"]: (a["status"], a["reason"]) for a in mw.pinned_anchor_report(self.root)["anchors"]}

    def provider(self, version):
        # Exercise the real constructor and version-bearing basis without optional modules.
        ts = mock.Mock()
        ts.Language.side_effect = lambda value: value
        provider = ts_exact.TreeSitterProvider(ts, mock.Mock(), mock.Mock(),
                                             {dist: version for dist in ts_exact.TESTED})
        provider.fingerprint = mock.Mock(return_value={"charge": {"ast": "a" * 64, "sig": "b" * 64}})
        return provider

    def test_version_gating_and_exact_basis(self):
        accepted = [("0.23.2",) * 3, ("0.24.0",) * 3, ("0.24.0", "0.23.2", "0.23.1")]
        modules = {"tree_sitter": mock.Mock(), "tree_sitter_typescript": mock.Mock(),
                   "tree_sitter_javascript": mock.Mock()}
        for versions in accepted:
            with self.subTest(versions=versions):
                mapping = dict(zip(ts_exact.TESTED, versions))
                with mock.patch("importlib.metadata.version", side_effect=mapping.__getitem__), \
                        mock.patch.object(ts_exact.importlib.util, "find_spec", return_value=object()), \
                        mock.patch.object(ts_exact.importlib, "import_module", side_effect=modules.__getitem__):
                    provider = ts_exact.load_provider()
                self.assertIsInstance(provider, ts_exact.TreeSitterProvider)
                self.assertEqual("tree-sitter-v1.core-{}.ts-{}.js-{}".format(*versions), provider.basis)
        for dist in ts_exact.TESTED:
            for bad in ("0.22.9", "0.25.0", None, ""):
                with self.subTest(dist=dist, bad=bad):
                    mapping = {d: "0.24.0" for d in ts_exact.TESTED}
                    mapping[dist] = bad
                    with mock.patch("importlib.metadata.version", side_effect=mapping.__getitem__), \
                            mock.patch.object(ts_exact.importlib.util, "find_spec", return_value=object()):
                        self.assertIsNone(ts_exact.load_provider())

    def test_registered_tree_sitter_without_config_stays_lexical(self):
        provider = self.provider("0.24.0")
        ca.register_exact_provider(provider)
        for ext in ("ts", "js"):
            self.write("pay." + ext, "export function charge(amount) { return amount; }\n")
        self.write("AGENTS.md", "- Call `pay.ts::charge`.\n- Call `pay.js::charge`.\n")
        mw.init_project(self.root)
        pins = mw._load_config_pins(self.root)
        for anchor in ("pay.ts::charge", "pay.js::charge"):
            self.assertEqual(ca.TS_LEXICAL_BASIS, pins[anchor]["hashBasis"])
            self.assertEqual("compatible-unverified", self.statuses()[anchor][0])
        provider.fingerprint.assert_not_called()

    def test_basis_upgrade_and_downgrade_require_repin_but_deletion_is_stale(self):
        base = self.root
        for before, after in (("0.23.2", "0.24.0"), ("0.24.0", "0.23.2")):
            with self.subTest(before=before, after=after):
                self.root = base / before
                self.root.mkdir()
                self.write("pay.ts", "export function charge(amount: number) { return amount; }\n")
                self.write("AGENTS.md", "- Call `pay.ts::charge`.\n")
                self.opt_in()
                ca.register_exact_provider(self.provider(before))
                mw.init_project(self.root)
                self.assertEqual(("active", "ast_hash_match"), self.statuses()["pay.ts::charge"])
                ca.register_exact_provider(self.provider(after))
                self.assertEqual(("compatible-unverified", "exact_parser_basis_repin_required"),
                                 self.statuses()["pay.ts::charge"])
                self.write("pay.ts", "export const other = 1;\n")
                self.assertEqual(("stale", "missing_symbol"), self.statuses()["pay.ts::charge"])

    def test_real_ts_tsx_js_parsing_and_edits(self):
        provider = ts_exact.load_provider()
        if provider is None:
            self.skipTest("optional tree-sitter and TS/JS grammars unavailable")
        ca.register_exact_provider(provider)
        self.opt_in()
        sources = {
            "pay.ts": "export function charge(amount: number) { return amount + 1; }\n",
            "view.tsx": "export function View(props: {name: string}) { return <div>{props.name}</div>; }\n",
            "pay.js": "export function charge(amount) { return amount + 1; }\n",
        }
        symbols = {"pay.ts": "charge", "view.tsx": "View", "pay.js": "charge"}
        for path, src in sources.items():
            self.write(path, src)
            self.assertIn(symbols[path], provider.fingerprint(path, src))
        self.write("AGENTS.md", "".join("- Call `{}::{}`.\n".format(p, symbols[p]) for p in sources))
        mw.init_project(self.root)
        self.assertTrue(all(s == ("active", "ast_hash_match") for s in self.statuses().values()))
        for path, src in sources.items():
            self.write(path, src.replace("amount + 1", "amount + 2").replace("<div>", "<span>").replace("</div>", "</span>"))
            self.assertEqual(("compatible-unverified", "body_changed_signature_compatible"),
                             self.statuses()[path + "::" + symbols[path]])
            changed = src.replace("amount: number", "amount: number, currency: string") if path.endswith(".ts") else (
                src.replace("props: {name: string}", "props: {name: string}, extra: number") if path.endswith(".tsx") else src.replace("(amount)", "(amount, currency)"))
            self.write(path, changed)
            self.assertEqual("stale", self.statuses()[path + "::" + symbols[path]][0], path)


if __name__ == "__main__":
    unittest.main()
