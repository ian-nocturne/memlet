"""Eval contract tests: scratch-only inputs, injected vendors, no model/network."""
import json
import contextlib
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from memlet import memlet_eval as ev, memlet_wiki as wiki, memlet_bridge as bridge


class EvalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {'HOME': str(self.root), 'TMPDIR': str(self.root),
                                         'GIT_CEILING_DIRECTORIES': str(self.root), 'MEMLET_HOME': str(self.root / 'unused')})
        self.env.start()
        self.addCleanup(self.env.stop)
        (self.root / 'sessions').mkdir()
        self.put('sessions/a.json', [{'timestamp': '2026-01-01', 'text': 'Repair uses cedar.'}])
        self.put('sessions/b.json', [{'timestamp': '2026-01-03', 'text': 'Repair cedar succeeded. HELDOUT_CANARY_739'}])
        self.calls = []
        mod = types.ModuleType('memlet.vendor_cli')
        def vendor(v, prompt, **kw):
            self.calls.append((v, prompt))
            return {'ok': True, 'vendor': v, 'model': 'fake', 'text': self.output}
        mod.run_vendor = vendor
        self.patch = patch.dict('sys.modules', {'memlet.vendor_cli': mod})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.output = 'cedar'

    def put(self, path, obj):
        (self.root / path).write_text(json.dumps(obj))

    def init(self, **kw):
        return ev.init_eval(self.root, run='r', sessions='sessions', dream_until='2026-01-02', budget=256, **kw)

    def seal(self, keys=True):
        q = {'id': 'q', 'text': 'What repaired the cache?', 'citation': {'file': 'sessions/b.json', 'start': 1, 'end': 1}}
        if keys:
            q.update(expectedAny=['cedar'], forbiddenAny=['pine'])
        self.put('questions.json', [q])
        return ev.seal_questions(self.root, run='r', source='questions.json')

    def contexts(self, root, m, q):
        return {a: {'text': '' if a == 'none' else 'Repair uses cedar.', 'tokens': 5 if a != 'none' else 0,
                    'budget': m['budget'], 'omittedItems': []} for a in ev.ARMS}

    def run_eval(self, **kw):
        return ev.run_eval(self.root, run='r', via='codex', context_builder=self.contexts, **kw)

    def item_provider(self, items):
        if hasattr(bridge, 'dream_items'):
            return contextlib.nullcontext()
        return patch.object(bridge, 'dream_items', create=True, return_value={'ok': True, 'items': items})

    def test_canary_equal_budgets_and_answerer_no_keys(self):
        self.init(); self.seal()
        m = ev._manifest(self.root, 'r'); q = ev._questions(self.root, 'r')[0]
        with self.item_provider([]):
            built = ev.build_contexts(self.root, m, q)
        for c in built.values():
            self.assertNotIn('HELDOUT_CANARY_739', c['text'])
            self.assertEqual(256, c['budget'])
            self.assertLessEqual(wiki.estimate_tokens(c['text']), 256)
        self.run_eval()
        self.assertTrue(all('expectedAny' not in p and 'forbiddenAny' not in p for _, p in self.calls))

    def test_snapshot_citations_filter_heldout_and_graph_uses_existing_recall(self):
        self.init(); self.seal()
        m = ev._manifest(self.root, 'r'); q = ev._questions(self.root, 'r')[0]
        lines = m['dream'][0]['lines']
        wiki.write_managed_text(self.root, '.memlet/dream/snapshot/a.log', '\n'.join(lines) + '\n')
        wiki.write_managed_text(self.root, '.memlet/dream/snapshot/b.log', 'HELDOUT_CANARY_739\n')
        graph = ev.dream.dream_pin_and_validate([{
            'lesson_id': 'L-CACHE', 'category': 'verified_workaround', 'scope_tag': 'cache',
            'title': 'Repair cache FUTUREANSWER739', 'src_spans': ['a.log:1-1'],
            'edges': [{'edge_type': 'RESOLVES', 'to_id': 'SCOPE:cache', 'src_span': 'a.log:1-1'}]
        }], {'a.log': lines})
        for n in graph['nodes']:
            n['approval'] = {'decision': 'approved', 'by': 'fixture-human', 'at': '2026-01-01T00:00:00Z',
                             'digest': ev.dream.node_approval_digest(n)}
        for e in graph['edges']:
            e['approval'] = {'decision': 'approved', 'by': 'fixture-human', 'at': '2026-01-01T00:00:00Z',
                             'digest': ev.dream.edge_approval_digest(e)}
        wiki.write_managed_text(self.root, '.memlet/dream/APPROVAL-QUEUE.json', json.dumps(graph))
        def item(file, text, ident):
            return {'id': ident, 'tier': 'approved', 'text': text, 'category': 'verified_workaround',
                    'edgeTypes': ['RESOLVES'], 'cites': [{'file': file, 'start': 1, 'end': 1,
                    'verified': True, 'sha256': ev.dream.sha256_hex(text), 'extra': 'ignored'}]}
        items = [item('.memlet/dream/snapshot/a.log', lines[0], 'L-CACHE'),
                 item('.memlet/dream/snapshot/b.log', 'HELDOUT_CANARY_739', 'L-LEAK')]
        with self.item_provider(items):
            built = ev.build_contexts(self.root, m, q)
        self.assertIn('cedar', built['flat']['text'])
        self.assertIn('cedar', built['graph']['text'])
        self.assertNotIn('FUTUREANSWER739', built['graph']['text'])
        # Large budget admits the same approved quote in the real capsule.
        m['budget'] = 1200
        with self.item_provider(items):
            large = ev.build_contexts(self.root, m, q)
        self.assertIn('cedar', large['handover']['text'])
        for context in built.values():
            self.assertNotIn('HELDOUT_CANARY_739', context['text'])

    def test_seal_tampering_and_manifest_tampering(self):
        self.init(); self.seal()
        ev._write(self.root, 'r', 'questions.json', [])
        with self.assertRaisesRegex(ValueError, 'sealed'):
            self.run_eval()
        ev._write(self.root, 'r', 'manifest.json', {})
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            ev.report_eval(self.root, run='r')

    def test_resume_and_changed_context(self):
        self.init(); self.seal()
        self.assertEqual(2, self.run_eval(max_calls=2)['answers'])
        self.assertEqual(5, self.run_eval()['answers'])
        self.assertEqual(0, self.run_eval()['calls'])
        self.assertEqual(5, len(self.calls))
        def changed(root, m, q):
            result = self.contexts(root, m, q); result['flat']['text'] += ' changed'; return result
        with self.assertRaisesRegex(ValueError, 'context'):
            ev.run_eval(self.root, run='r', via='codex', context_builder=changed)

    def test_changed_source_refused(self):
        self.init(); self.seal(); self.run_eval(max_calls=1)
        self.put('sessions/a.json', [{'timestamp': '2026-01-01', 'text': 'Changed'}])
        with self.assertRaisesRegex(ValueError, 'memory changed'):
            self.run_eval()

    def test_oversized_arm_refused_before_call(self):
        self.init(); self.seal()
        def bad(root, m, q):
            result = self.contexts(root, m, q); result['flat']['budget'] += 1; return result
        with self.assertRaisesRegex(ValueError, 'budget'):
            ev.run_eval(self.root, run='r', via='codex', context_builder=bad)
        self.assertEqual([], self.calls)

    def test_deterministic_keys_literals_and_forbidden(self):
        self.assertTrue(ev.deterministic_score({'expectedAny': ['CEDAR']}, 'use cedar'))
        self.assertFalse(ev.deterministic_score({'expectedAny': ['cedar'], 'forbiddenAny': ['pine']}, 'cedar pine'))
        self.assertFalse(ev.deterministic_score({'expectedAny': ['(a+)+$']}, 'a' * 50000))
        self.init(); self.seal(); self.run_eval()
        r = ev.score_eval(self.root, run='r')
        self.assertEqual(5, r['scores']); self.assertEqual(0, r['calls'])
        report = ev.report_eval(self.root, run='r')
        self.assertEqual(5, sum(r['deterministic'] for r in report['arms']))
        self.assertEqual(5, report['calls'])
        self.assertEqual(report['resultDigest'], ev.report_eval(self.root, run='r')['resultDigest'])

    def test_judge_vendor_and_call_bound(self):
        self.init(); self.seal(False); self.run_eval()
        with self.assertRaisesRegex(ValueError, 'different vendor'):
            ev.score_eval(self.root, run='r', judge='codex')
        self.output = '{"correct": true}'
        result = ev.score_eval(self.root, run='r', judge='claude', max_calls=2)
        self.assertEqual(2, result['calls']); self.assertEqual(2, result['scores'])
        result = ev.score_eval(self.root, run='r', judge='claude')
        self.assertEqual(3, result['calls'])
        self.assertEqual(5, sum(r['judged'] for r in ev.report_eval(self.root, run='r')['arms']))

    def test_dry_run_and_max_calls(self):
        self.init(); self.seal()
        result = self.run_eval(dry_run=True, max_calls=2)
        self.assertEqual(2, len(result['prompts'])); self.assertEqual([], self.calls)
        self.assertEqual(0, self.run_eval(max_calls=0)['calls'])
        with self.assertRaises(ValueError):
            self.run_eval(max_calls=-1)

    def test_question_author_sees_only_heldout(self):
        self.init()
        r = ev.seal_questions(self.root, run='r', via='claude', count=1, dry_run=True)
        self.assertNotIn('Repair uses cedar.', r['prompts'][0])
        self.assertIn('HELDOUT_CANARY_739', r['prompts'][0])
        self.assertEqual([], self.calls)
        with self.assertRaisesRegex(ValueError, 'max-calls'):
            ev.seal_questions(self.root, run='r', via='claude', max_calls=0)

    def test_uncertain_call_not_repeated(self):
        self.init(); self.seal()
        with patch.object(ev, '_call', side_effect=ValueError('failure')):
            with self.assertRaises(ValueError):
                self.run_eval()
        with self.assertRaisesRegex(ValueError, 'unresolved call'):
            self.run_eval()

    def test_bad_timestamps_and_duplicate_windows(self):
        self.put('sessions/a.json', [{'text': 'missing'}])
        with self.assertRaises(KeyError): self.init()
        self.put('sessions/a.json', [{'timestamp': '2026-01-01', 'text': 'one'}, {'timestamp': '2025-01-01', 'text': 'two'}])
        with self.assertRaisesRegex(ValueError, 'out-of-order'): self.init()
        self.put('sessions/a.json', [{'timestamp': '2026-01-01', 'text': 'Repair cedar succeeded. HELDOUT_CANARY_739'}])
        with self.assertRaisesRegex(ValueError, 'both windows'): self.init()

    def test_question_quote_key_and_bad_citation_rejected(self):
        self.init()
        q = {'id': 'q', 'text': 'Is cedar right?', 'citation': {'file': 'sessions/b.json', 'start': 1, 'end': 1}, 'expectedAny': ['cedar']}
        self.put('questions.json', [q])
        with self.assertRaisesRegex(ValueError, 'key appears'): ev.seal_questions(self.root, run='r', source='questions.json')
        q['text'] = 'What works?'; q['citation']['file'] = 'sessions/a.json'; self.put('questions.json', [q])
        with self.assertRaisesRegex(ValueError, 'outside'): ev.seal_questions(self.root, run='r', source='questions.json')

    def test_native_jsonl_visible_text_and_missing_time_refusal(self):
        for f in (self.root / 'sessions').glob('*.json'):
            f.unlink()
        for name, stamp, text in [('a', '2026-01-01T00:00:00Z', 'Never skip cache tests.'),
                                  ('b', '2026-01-03T00:00:00Z', 'Held-out result.')]:
            (self.root / ('sessions/' + name + '.jsonl')).write_text(json.dumps({
                'type': 'user', 'timestamp': stamp, 'message': {'role': 'user', 'content': text}}) + '\n')
        result = self.init()
        self.assertIn('Never skip cache tests.', result['manifest']['dream'][0]['lines'][0])
        (self.root / 'sessions/a.jsonl').write_text('{"type":"user","message":{"role":"user","content":"missing"}}\n')
        with self.assertRaisesRegex(ValueError, 'missing transcript timestamp'):
            ev.init_eval(self.root, run='new', sessions='sessions', dream_until='2026-01-02')

    def test_wilson_hand_values(self):
        lo, hi = ev.wilson(5, 10)
        self.assertAlmostEqual(.236593090512564, lo)
        self.assertAlmostEqual(.763406909487436, hi)
        self.assertAlmostEqual(.722467200137111, ev.wilson(10, 10)[0])
        self.assertEqual([0., 1.], ev.wilson(0, 0))

    def test_writes_only_under_eval_and_symlink_refusal(self):
        self.init(); self.seal(); self.run_eval(); ev.score_eval(self.root, run='r'); ev.report_eval(self.root, run='r')
        for f in self.root.rglob('*'):
            if f.is_file() and f not in [self.root / 'questions.json'] and 'sessions' not in f.parts:
                self.assertTrue(f.is_relative_to(self.root / '.memlet/eval'), str(f))
        (self.root / '.memlet/eval/escape').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            ev.init_eval(self.root, run='escape', sessions='sessions', dream_until='2026-01-02')

    def test_selftest_and_broken_arm(self):
        self.assertTrue(ev.eval_selftest()['ok'])
        def broken(root, m, q):
            result = ev.build_contexts(root, m, q); result['rawlog']['text'] = ''; result['rawlog']['tokens'] = 0; return result
        self.assertFalse(ev.eval_selftest(context_builder=broken)['ok'])


if __name__ == '__main__':
    unittest.main()
