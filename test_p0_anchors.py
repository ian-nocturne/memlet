"""P0-3/P0-4/P0-7: truthful anchors, preserved baselines and capsule checksums."""
import ast
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from prism_memory.code_anchors import (
    _extract_ts_js_signatures, extract_file_symbol_hashes,
    extract_file_symbol_signatures, normalized_symbol_ast_hash,
)
from prism_memory.memlet_wiki import (
    CONFIG_PATH, INDEX_PATH, _config_pins, _verify_rules, build_handover,
    classify_anchor, handover_signature, import_rules, init_project,
    verify_handover_checksum, wiki_status,
)


class AnchorRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def python_repo(self):
        self.write("mod.py", 'def f(x):\n    """original documentation"""\n    return x\n')
        self.write("AGENTS.md", "# Rules\n- Always call `mod.py::f`.\n")

    def ts_sig(self, source, name="f"):
        ok, symbols = _extract_ts_js_signatures(source)
        self.assertTrue(ok, source)
        self.assertIn(name, symbols, source)
        return symbols[name]

    def assert_ts_change(self, before, after, name="f", signature=False):
        a, b = self.ts_sig(before, name), self.ts_sig(after, name)
        self.assertNotEqual(a["normAstHash"], b["normAstHash"])
        if signature:
            self.assertNotEqual(a["sigHash"], b["sigHash"])

    def test_ts_literal_edit_changes_hash(self):
        self.assert_ts_change('function f() { return "allow"; }', 'function f() { return "deny!"; }')

    def test_ts_parameter_type_edit_changes_hash(self):
        self.assert_ts_change('function f(x:string) { return x; }', 'function f(x:number) { return x; }', signature=True)

    def test_ts_default_edit_changes_hash(self):
        self.assert_ts_change('function f(x=1) { return x; }', 'function f(x=2) { return x; }', signature=True)

    def test_ts_object_return_annotation_does_not_hide_body(self):
        self.assert_ts_change('function f(): {a:number} { return {a:1}; }', 'function f(): {a:number} { return {a:2}; }')

    def test_ts_return_type_itself_changes_hash(self):
        self.assert_ts_change('function f(): {a:number} { return value; }', 'function f(): {a:string} { return value; }', signature=True)

    def test_ts_asi_line_break_changes_hash(self):
        self.assert_ts_change('function f() { return 1; }', 'function f() { return\n1; }')

    def test_ts_comments_indentation_and_horizontal_spacing_are_ignored(self):
        before = 'function f(x: string = "a b") {\n  return x + "allow";\n}'
        after = '// comment\nfunction f( x : string = "a b" ) { // note\n    /* explanation */ return x+"allow";   \n}\n'
        self.assertEqual(self.ts_sig(before)["normAstHash"], self.ts_sig(after)["normAstHash"])

    def test_ts_literal_whitespace_is_significant(self):
        self.assert_ts_change('function f() { return "a b"; }', 'function f() { return "ab"; }')

    def test_ts_callbacks_captured_in_every_declaration_shape(self):
        params = 'cb:(x:number)=>void, required:string'
        forms = [
            ('function f(' + params + ') { return required; }', 'f'),
            ('const f = (' + params + ') => { return required; };', 'f'),
            ('export default function(' + params + ') { return required; }', 'default'),
            ('export default (' + params + ') => { return required; };', 'default'),
            ('const f = memo(forwardRef((' + params + ') => { return required; }));', 'f'),
            ('class C {\n f(' + params + '): {a:number} { return {a:1}; }\n}', 'C.f'),
        ]
        for source, name in forms:
            with self.subTest(source=source):
                sig = self.ts_sig(source, name)
                self.assertEqual(['cb', 'required'], sig['allArgs'])
                self.assertEqual(['cb', 'required'], sig['reqArgs'])

    def test_ts_nested_generics_strings_templates_comments_in_parameters(self):
        sig = self.ts_sig('function f<T extends Map<string, Array<number>>>(cb:Map<string, (x:number)=>void>, '
                          'text=") ,", label=`a,) ${nested("(")}`, /* , ) */ required:string) { return required; }')
        self.assertEqual(['cb', 'text', 'label', 'required'], sig['allArgs'])
        self.assertEqual(['cb', 'required'], sig['reqArgs'])

    def test_ts_generic_object_return_body_is_hashed(self):
        self.assert_ts_change('function f(): Promise<{a:number}> { return one; }',
                              'function f(): Promise<{a:number}> { return two; }')

    def test_ts_hash_match_is_honestly_lexical(self):
        self.write('mod.ts', 'function f(x:string) { return "allow"; }')
        self.write('AGENTS.md', '- Always call `mod.ts::f`.\n')
        result = init_project(self.root)
        anchor = result['anchors'][0]
        self.assertEqual('compatible-unverified', anchor['status'])
        self.assertEqual('ts_js_lexical_scan_unverified', anchor['reason'])
        self.assertTrue(anchor['pinnedNow'])
        cap = build_handover(self.root, budget=2000, output_format='markdown')
        self.assertEqual([], cap['activeConstraints'])
        self.assertEqual(['mod.ts::f'], cap['lexicalConstraints'][0]['anchors'])
        self.assertIn('lexical hash match only; re-check before use', cap['formatted'])
        self.assertTrue(verify_handover_checksum(cap))

    def test_ts_stale_not_carried_as_lexical_match(self):
        self.write('mod.ts', 'function f(x:string) { return x; }')
        self.write('AGENTS.md', '- Always call `mod.ts::f`.\n')
        init_project(self.root)
        self.write('mod.ts', 'function f(x:string, required:string) { return x; }')
        cap = build_handover(self.root, budget=2000)
        self.assertEqual([], cap['lexicalConstraints'])
        self.assertEqual('mod.ts::f', cap['quarantinedStaleAnchors'][0]['anchor'])

    def test_python_extractors_use_one_docstring_free_basis(self):
        self.python_repo()
        node = ast.parse((self.root / 'mod.py').read_text()).body[0]
        self.assertEqual(normalized_symbol_ast_hash(node), extract_file_symbol_hashes(self.root / 'mod.py')['f'])
        self.assertEqual(normalized_symbol_ast_hash(node), extract_file_symbol_signatures(self.root / 'mod.py')['symbols']['f']['normAstHash'])

    def test_python_docstring_only_edit_remains_active(self):
        self.python_repo()
        init_project(self.root)
        self.write('mod.py', 'def f(x):\n    """new documentation"""\n    return x\n')
        self.assertEqual('active', wiki_status(self.root, dry_run=True)['anchors'][0]['status'])
        self.assertEqual(1, len(build_handover(self.root)['activeConstraints']))

    def test_python_class_nested_docstrings_are_ignored(self):
        p = self.write('mod.py', 'class C:\n    """class doc"""\n    def f(self):\n        """method doc"""\n        return 1\n')
        before = extract_file_symbol_hashes(p)
        self.write('mod.py', p.read_text().replace('method doc', 'another method doc').replace('class doc', 'another class doc'))
        self.assertEqual(before, extract_file_symbol_hashes(p))

    def test_legacy_pin_matches_and_init_migrates_it(self):
        self.python_repo()
        node = ast.parse((self.root / 'mod.py').read_text()).body[0]
        legacy = hashlib.sha256(ast.dump(node, annotate_fields=True, include_attributes=False).encode()).hexdigest()
        self.assertEqual(legacy, extract_file_symbol_hashes(self.root / 'mod.py', legacy=True)['f'])
        normal = extract_file_symbol_hashes(self.root / 'mod.py')['f']
        self.assertNotEqual(legacy, normal)
        anchor = {'path': 'mod.py', 'symbol': 'f', 'pin': {'ast': legacy}}
        self.assertEqual('active', classify_anchor(self.root, anchor)['status'])
        self.write(CONFIG_PATH, json.dumps({'pinnedAnchors': {'mod.py::f': {'astHash': legacy}}, 'custom': 42}))
        self.write(INDEX_PATH, '- Use `mod.py::f`.\n<!-- memlet:anchor mod.py::f ast=' + legacy + ' -->\n')
        init_project(self.root)
        cfg = json.loads((self.root / CONFIG_PATH).read_text())
        self.assertEqual(normal[:16], cfg['pinnedAnchors']['mod.py::f']['astHash'])
        self.assertNotIn(legacy, (self.root / INDEX_PATH).read_text())
        self.assertEqual(42, cfg['custom'])

    def test_wiki_write_migrates_legacy_page_and_config_without_repinning_drift(self):
        self.python_repo()
        init_project(self.root)
        legacy = extract_file_symbol_hashes(self.root / 'mod.py', legacy=True)['f']
        config_path = self.root / CONFIG_PATH
        cfg = json.loads(config_path.read_text())
        current = cfg['pinnedAnchors']['mod.py::f']['astHash']
        cfg['pinnedAnchors']['mod.py::f']['astHash'] = legacy
        config_path.write_text(json.dumps(cfg))
        page = self.root / INDEX_PATH
        page.write_text(page.read_text().replace('ast=' + current, 'ast=' + legacy))
        before_config, before_page = config_path.read_bytes(), page.read_bytes()
        self.assertEqual('active', wiki_status(self.root, dry_run=True)['anchors'][0]['status'])
        self.assertEqual(before_config, config_path.read_bytes())
        self.assertEqual(before_page, page.read_bytes())
        wiki_status(self.root)
        self.assertEqual(current, _config_pins(self.root)['mod.py::f']['ast'])
        self.assertNotIn(legacy, page.read_text())
        self.write('mod.py', 'def f(x):\n    """changed documentation"""\n    return x\n')
        self.assertEqual('active', import_rules(self.root, dry_run=True)['anchors'][0]['status'])

    def test_path_only_anchor_is_never_verified(self):
        self.write('guide.md', 'instructions\n')
        self.write('AGENTS.md', '- Always read `guide.md`.\n')
        report = import_rules(self.root, dry_run=True)
        self.assertEqual('path-exists', report['anchors'][0]['status'])
        cap = build_handover(self.root, budget=2000, output_format='markdown')
        self.assertEqual([], cap['activeConstraints'])
        self.assertEqual('guide.md', cap['pathExistsAnchors'][0]['anchor'])
        self.assertIn('Paths exist (not verified)', cap['formatted'])
        self.assertNotIn('AST-verified', cap['formatted'])

    def test_unpinned_symbol_read_paths_do_not_store_baselines(self):
        self.python_repo()
        self.assertEqual('unpinned', classify_anchor(self.root, {'path': 'mod.py', 'symbol': 'f'})['status'])
        self.assertEqual('unpinned', wiki_status(self.root, dry_run=True)['anchors'][0]['status'])
        self.assertEqual('unpinned', import_rules(self.root, dry_run=True)['anchors'][0]['status'])
        cap = build_handover(self.root, budget=2000, output_format='markdown')
        self.assertEqual([], cap['activeConstraints'])
        self.assertEqual('mod.py::f', cap['unpinnedAnchors'][0]['anchor'])
        self.assertIn('Unpinned symbols (not verified)', cap['formatted'])
        self.assertFalse((self.root / '.memlet').exists())

    def test_dry_run_init_is_unpinned(self):
        self.python_repo()
        result = init_project(self.root, dry_run=True)
        self.assertEqual('unpinned', result['anchors'][0]['status'])
        self.assertFalse(result['anchors'][0]['pinnedNow'])
        self.assertFalse((self.root / '.memlet').exists())

    def test_init_and_import_explicitly_record_baselines(self):
        for command in (init_project, import_rules):
            with self.subTest(command=command.__name__):
                self.python_repo()
                report = command(self.root)
                self.assertEqual('active', report['anchors'][0]['status'])
                self.assertTrue(report['anchors'][0]['pinnedNow'])
                self.assertIn('baseline', report['baselineNotice'])
                self.assertTrue(_config_pins(self.root)['mod.py::f']['ast'])
                import shutil
                shutil.rmtree(self.root / '.memlet')

    def test_init_twice_preserves_pins_and_arbitrary_config(self):
        self.python_repo()
        init_project(self.root)
        p = self.root / CONFIG_PATH
        cfg = json.loads(p.read_text())
        cfg.update(imports={'from': 'claude-md'}, handover={'tokenBudget': 1200, 'mode': 'digest'}, custom={'nested': [1, 2]})
        cfg['pinnedAnchors']['mod.py::f']['customPinKey'] = 'keep'
        p.write_text(json.dumps(cfg))
        self.write('mod.py', 'def f(x, required):\n    return x\n')
        second = init_project(self.root)
        after = json.loads(p.read_text())
        for key in ('pinnedAnchors', 'imports', 'handover', 'custom'):
            self.assertEqual(cfg[key], after[key])
        self.assertEqual('stale', second['anchors'][0]['status'])
        self.assertFalse(second['anchors'][0]['pinnedNow'])
        cap = build_handover(self.root, mode='verified', budget=2000)
        self.assertEqual([], cap['activeConstraints'])
        self.assertEqual('mod.py::f', cap['quarantinedStaleAnchors'][0]['anchor'])

    def test_page_pin_wins_over_conflicting_config_pin(self):
        self.python_repo()
        init_project(self.root)
        self.write('mod.py', 'def f(x, required):\n    return x\n')
        p = self.root / CONFIG_PATH
        cfg = json.loads(p.read_text())
        cfg['pinnedAnchors']['mod.py::f']['astHash'] = extract_file_symbol_hashes(self.root / 'mod.py')['f'][:12]
        p.write_text(json.dumps(cfg))
        self.assertEqual('stale', wiki_status(self.root, dry_run=True)['anchors'][0]['status'])
        cap = build_handover(self.root)
        self.assertEqual([], cap['activeConstraints'])
        self.assertEqual('mod.py::f', cap['quarantinedStaleAnchors'][0]['anchor'])

    def test_legacy_migration_preserves_independent_config_and_page_pins(self):
        self.python_repo()
        init_project(self.root)
        p = self.root / CONFIG_PATH
        cfg = json.loads(p.read_text())
        normal = cfg['pinnedAnchors']['mod.py::f']['astHash']
        legacy = extract_file_symbol_hashes(self.root / 'mod.py', legacy=True)['f']
        # A matching legacy page pin must not be replaced with a conflicting config pin.
        cfg['pinnedAnchors']['mod.py::f']['astHash'] = '0' * 16
        p.write_text(json.dumps(cfg))
        page = self.root / INDEX_PATH
        page.write_text(page.read_text().replace('ast=' + normal, 'ast=' + legacy))
        init_project(self.root)
        self.assertIn('ast=' + normal, page.read_text())
        self.assertEqual('0' * 16, _config_pins(self.root)['mod.py::f']['ast'])
        self.assertEqual(1, len(build_handover(self.root)['activeConstraints']))
        # Conversely, migrating config cannot change a conflicting page baseline.
        cfg['pinnedAnchors']['mod.py::f']['astHash'] = legacy
        p.write_text(json.dumps(cfg))
        page.write_text(page.read_text().replace('ast=' + normal, 'ast=' + '0' * 16))
        init_project(self.root)
        self.assertEqual(normal, _config_pins(self.root)['mod.py::f']['ast'])
        self.assertIn('ast=' + '0' * 16, page.read_text())
        self.assertEqual([], build_handover(self.root)['activeConstraints'])

    def test_second_init_adds_new_anchor_without_resetting_old(self):
        self.python_repo()
        init_project(self.root)
        old = _config_pins(self.root)['mod.py::f']
        self.write('mod.py', 'def f(x, required):\n    return x\n\ndef g():\n    return 2\n')
        self.write('AGENTS.md', '- Always call `mod.py::f`.\n- Also call `mod.py::g`.\n')
        result = init_project(self.root)
        self.assertEqual(old, _config_pins(self.root)['mod.py::f'])
        self.assertEqual({'f': 'stale', 'g': 'active'}, {a['symbol']: a['status'] for a in result['anchors']})
        self.assertEqual(1, len(build_handover(self.root)['activeConstraints']))
        self.assertEqual({'f', 'g'}, {a['symbol'] for a in wiki_status(self.root, dry_run=True)['anchors']})

    def test_force_repins_all_pages(self):
        self.python_repo()
        init_project(self.root)
        old = _config_pins(self.root)['mod.py::f']
        self.write('.memlet/wiki/notes.md', '- Use `mod.py::f`.\n<!-- memlet:anchor mod.py::f ast=' + old['ast'] + ' -->\n')
        self.write('mod.py', 'def f(x, required):\n    return x\n')
        result = init_project(self.root, force=True)
        self.assertEqual('active', result['anchors'][0]['status'])
        self.assertNotEqual(old['ast'], _config_pins(self.root)['mod.py::f']['ast'])
        self.assertEqual('active', wiki_status(self.root, dry_run=True)['anchors'][0]['status'])
        self.assertEqual([], build_handover(self.root)['quarantinedStaleAnchors'])

    def test_mixed_rule_cannot_promote_path_exists_to_active(self):
        self.python_repo()
        self.write('guide.md', 'instructions\n')
        init_project(self.root)
        rules = [{'source': 'AGENTS.md', 'line': 1, 'text': 'mixed', 'refs': [('mod.py', 'f'), ('guide.md', None)]}]
        _verify_rules(self.root, rules, _config_pins(self.root))
        self.assertEqual('path-exists', rules[0]['status'])

    def test_capsule_checksum_and_legacy_alias(self):
        self.python_repo()
        init_project(self.root)
        cap = build_handover(self.root, output_format='markdown')
        self.assertEqual(cap['checksum'], cap['capsuleSignature'])
        self.assertEqual(cap['checksum'], handover_signature(cap))
        self.assertTrue(verify_handover_checksum(cap))
        self.assertIn('- Checksum:', cap['formatted'])
        self.assertNotIn('- Signature:', cap['formatted'])
        legacy = dict(cap)
        del legacy['checksum']
        self.assertTrue(verify_handover_checksum(legacy))

    def test_checksum_rejects_content_and_alias_tampering(self):
        self.python_repo()
        init_project(self.root)
        cap = build_handover(self.root)
        for field, value in [('nextAction', 'tampered'), ('activeConstraints', []), ('checksum', 'sha256:' + '0' * 64),
                             ('capsuleSignature', 'sha256:' + 'f' * 64), ('unpinnedAnchors', [{'anchor': 'invented'}]),
                             ('lexicalConstraints', [{'rule': 'invented'}]), ('mode', 'unknown'), ('signedFields', [])]:
            with self.subTest(field=field):
                changed = copy.deepcopy(cap)
                changed[field] = value
                self.assertFalse(verify_handover_checksum(changed))
        del cap['authorityBoundary']
        self.assertFalse(verify_handover_checksum(cap))
        for value in (None, {}, 'text', {'checksum': []}):
            self.assertFalse(verify_handover_checksum(value))

    def test_lexical_constraints_trim_before_verified_constraints(self):
        self.python_repo()
        self.write('mod.ts', 'function f() { return "allow"; }')
        self.write('AGENTS.md', '- Always call `mod.py::f`.\n' + ''.join(
            '- Always call `mod.ts::f` for important operation %d and retain the full audit log.\n' % i for i in range(40)))
        init_project(self.root)
        cap = build_handover(self.root)
        self.assertLess(cap['tokenEstimate'], cap['tokenBudget'])
        self.assertGreater(cap['omitted']['lexicalConstraints'], 0)
        self.assertEqual(1, len(cap['activeConstraints']))


if __name__ == '__main__':
    unittest.main()
