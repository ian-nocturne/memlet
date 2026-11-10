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
        self.tmp = tempfile.TemporaryDirectory(dir='/private/tmp')
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
            self.assertNotIn('/eval/r/locks', os.environ.get('MEMLET_LOCK_DIR', ''))
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
        self.init(allow_post_split_memory=True); self.seal()
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
            return {'approvedAt': '2026-01-01', 'id': ident, 'tier': 'approved', 'text': text, 'category': 'verified_workaround',
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
        self.assertEqual(0, sum(r['deterministic'] for r in ev.report_eval(self.root, run='r')['arms']))

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
                'type': 'user', 'timestamp': stamp, 'message': {'role': 'user', 'content': text}}) + '\n\n')
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

    def test_definite_failures_raw_and_resolution(self):
        self.init(); self.seal()
        with patch.object(ev, '_call', side_effect=ev.DefiniteFailure('no call')):
            with self.assertRaises(ValueError): self.run_eval(max_calls=1)
        self.assertEqual('failed', ev._state(self.root, 'r')['calls'][0]['status'])
        self.run_eval(max_calls=1)
        self.assertEqual(1, ev.report_eval(self.root, run='r')['failedAttempts'])
        with patch.object(ev, '_call', side_effect=RuntimeError('unknown')):
            with self.assertRaises(RuntimeError): self.run_eval(max_calls=1)
        key = ev._state(self.root, 'r')['calls'][-1]['key']
        ev.resolve_call(self.root, run='r', resolve=key, as_status='failed')
        self.run_eval(max_calls=1)
        self.assertTrue(ev._state(self.root, 'r')['calls'][-2]['resolution']['explicit'])

    def test_vendor_model_fixed_and_identity(self):
        self.init(); self.seal(); self.run_eval(max_calls=1)
        with self.assertRaisesRegex(ValueError, 'vendor'):
            ev.run_eval(self.root, run='r', via='claude', context_builder=self.contexts)
        with patch.object(ev, '_call', return_value={'vendor': 'codex', 'model': 'changed', 'text': 'cedar'}):
            with self.assertRaisesRegex(ValueError, 'model'): self.run_eval(max_calls=1)
        self.assertEqual('done', ev._state(self.root, 'r')['calls'][-1]['status'])
        for bad in ('', 'openai'):
            with self.assertRaises(ValueError): ev._vendor(bad)
        self.assertEqual('codex', ev._vendor(' CODEX\n'))

    def test_response_vendor_mismatch_preserves_raw(self):
        self.init(); self.seal()
        with patch.object(ev, '_call', return_value={'vendor': 'claude', 'model': 'fake', 'text': 'paid output'}):
            with self.assertRaisesRegex(ValueError, 'identity'): self.run_eval(max_calls=1)
        c = ev._state(self.root, 'r')['calls'][0]
        self.assertEqual('done', c['status']); self.assertEqual('paid output', c['raw'])

    def test_invalid_judge_done_and_unresolved_guards(self):
        self.init(); self.seal(False); self.run_eval(max_calls=1)
        self.output = '```json {"correct":true}```'
        ev.score_eval(self.root, run='r', judge='claude')
        s = ev._state(self.root, 'r')
        self.assertTrue(s['scores']['q:none']['invalid'])
        self.assertEqual('done', s['calls'][-1]['status'])
        s['scores'] = {}; s['calls'][-1]['status'] = 'started'
        ev._write(self.root, 'r', 'state.json', s)
        with self.assertRaisesRegex(ValueError, 'unresolved judge'):
            ev.score_eval(self.root, run='r', judge='claude')

    def test_question_unknown_guard_and_raw_parse_failure(self):
        self.init()
        with patch.object(ev, '_call', side_effect=RuntimeError('unknown')):
            with self.assertRaises(RuntimeError): ev.seal_questions(self.root, run='r', via='claude')
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            ev.seal_questions(self.root, run='r', via='claude')
        ev.resolve_call(self.root, run='r', resolve='questions', as_status='failed')
        self.output = 'invalid JSON'
        with self.assertRaises(ValueError): ev.seal_questions(self.root, run='r', via='claude')
        self.assertEqual('invalid JSON', ev._state(self.root, 'r')['calls'][-1]['raw'])
        self.assertEqual('done', ev._state(self.root, 'r')['calls'][-1]['status'])

    def test_all_contexts_frozen_and_seals_bound(self):
        self.init(); self.seal(); self.run_eval(arms=['none'], max_calls=1)
        def changed(root, m, q):
            c = self.contexts(root,m,q); c['graph']['text'] += ' new'; return c
        with self.assertRaisesRegex(ValueError, 'q:graph'):
            ev.run_eval(self.root, run='r', via='codex', arms=['flat'], context_builder=changed)
        for name in ('questions.json', 'questions-seal.json'):
            (self.root / '.memlet/eval/r' / name).unlink()
        with self.assertRaisesRegex(ValueError, 'answers exist'): self.seal()
        self.put('questions.json', [{'id':'q','text':'What repaired the cache?', 'citation':{'file':'sessions/b.json','start':1,'end':1},'expectedAny':['cedar'],'forbiddenAny':['pine']}])
        q=json.loads((self.root/'questions.json').read_text())
        ev._write(self.root,'r','questions.json',q)
        ev._write(self.root,'r','questions-seal.json',{'sha256':ev.digest(q)})
        m = ev._manifest(self.root, 'r'); m['budget'] = 100
        ev._write(self.root, 'r', 'manifest.json', m)
        ev._write(self.root, 'r', 'manifest-seal.json', {'sha256': ev.digest(m)})
        with self.assertRaisesRegex(ValueError, 'seal'): ev.report_eval(self.root, run='r')

    def test_input_boundaries_grammar_and_no_read_side_effect(self):
        for bad in ('20260102', '2026-W01-1', '2026-01-01 00:00'):
            with self.assertRaises(ValueError): ev._stamp(bad)
        self.assertEqual('2025-12-31T15:00:00+00:00', ev._stamp('2026-01-01+09:00').isoformat())
        self.assertEqual(123456, ev._stamp('2026-01-01T00:00:00.123456789Z').microsecond)
        self.put('sessions/a.json', [{'timestamp':'2026-01-01','text':'a'}, {'timestamp':'2026-01-03','text':'b'}])
        with self.assertRaisesRegex(ValueError, 'crosses split'): self.init()
        (self.root / 'sessions/a.json').unlink()
        (self.root / 'external.json').write_text('[]')
        (self.root / 'sessions/a.json').symlink_to(self.root / 'external.json')
        with self.assertRaisesRegex(ValueError, 'symlink'): self.init()
        (self.root / 'sessions/a.json').unlink()
        os.link(self.root / 'external.json', self.root / 'sessions/a.json')
        with self.assertRaisesRegex(ValueError, 'hard-linked'): self.init()
        with self.assertRaises(ValueError): ev.report_eval(self.root, run='missing')
        self.assertFalse((self.root / '.memlet/eval/missing').exists())

    def test_temporal_protocol_and_line_clipping(self):
        m = {'protocol':'prospective','dreamUntil':'2026-01-02'}
        for item, reason in (({},'missing'), ({'approvedAt':'2026-03-01'},'post-split'), ({'createdAt':'invalid'},'invalid')):
            self.assertIn(reason, ev._temporal(item,m))
        self.assertTrue(ev._temporal({'createdAt':'2026-01-01','approval':{'at':'2026-01-01'}},m).startswith('admitted'))
        self.assertIn('post-split', ev._temporal({'createdAt':'2026-01-01','edges':[{'createdAt':'2026-03-01'}]},m))
        m['protocol']='retrospective'
        self.assertTrue(ev._temporal({'approvedAt':'2026-03-01'},m).startswith('admitted'))
        self.assertEqual('first', ev._clip('first\n' + 'long ' * 200, 5))

    def test_scoring_boundaries_keys_and_forbidden_only(self):
        for key, answer in (('yes','eyes'), ('cedar','cedarwood'), ('forty','fortytwo')):
            self.assertFalse(ev.deterministic_score({'expectedAny':[key]},answer))
        self.assertFalse(ev.deterministic_score({'forbiddenAny':['pine']},'Unknown.'))
        self.init()
        m=ev._manifest(self.root,'r')
        for key in ('ce','absent','tools'):
            q={'id':'q','text':'What worked?', 'citation':{'file':'sessions/b.json','start':1,'end':1},'expectedAny':[key]}
            with self.assertRaises(ValueError): ev._validate_questions([q],m)

    def test_cli_injected_vendor_markdown_input_errors(self):
        import argparse
        from memlet import cli_eval
        parser=argparse.ArgumentParser(); cli_eval.register(parser.add_subparsers())
        def cli(words): return cli_eval.dispatch(parser.parse_args(['eval']+words+['--repo',str(self.root)]))
        self.assertTrue(cli(['init','--run','r','--sessions','sessions','--dream-until','2026-01-02'])['ok'])
        self.seal()
        with patch.object(ev, 'build_contexts', self.contexts):
            self.assertTrue(cli(['run','--run','r','--via',' CODEX ','--max-calls','1'])['ok'])
        self.assertTrue(cli(['score','--run','r'])['ok'])
        r=cli(['report','--run','r','--format','markdown'])
        self.assertTrue(r['_cli_text'].startswith('Protocol: prospective'))
        import memory_cli, sys, io
        output=io.StringIO()
        with patch.object(sys,'argv',['memlet','admin','eval','report','--run','r','--repo',str(self.root),'--format','markdown']), contextlib.redirect_stdout(output):
            self.assertEqual(0,memory_cli.cli())
        self.assertTrue(output.getvalue().startswith('Protocol: prospective'))
        self.assertEqual(os.environ.get('MEMLET_LOCK_DIR'), None)
        (self.root/'sessions/bad.jsonl').write_text('\n[1,2]\n')
        r=cli(['init','--run','bad','--sessions','sessions','--dream-until','2026-01-02'])
        self.assertEqual(2,r['exitCode']); self.assertIn('objects',r['error'])

    def test_filters_and_secret_key_not_in_prompt(self):
        self.init(); self.seal()
        m=ev._manifest(self.root,'r'); q=ev._questions(self.root,'r')[0]
        item={'id':'x','tier':'approved','approvedAt':'2026-01-01','text':'Repair uses cedar.',
              'cites':[{'file':'sessions/a.json','start':1,'end':1,'verified':True,'sha256':ev.dream.sha256_hex('Repair uses cedar.')} ]}
        import copy
        for field,value in (('tier','pending'),('verified',False),('sha256','bad')):
            bad=copy.deepcopy(item)
            if field=='tier':bad[field]=value
            else:bad['cites'][0][field]=value
            with self.item_provider([bad]):
                self.assertNotIn('cedar',ev.build_contexts(self.root,m,q)['flat']['text'])
        self.run_eval(arms=['none'])
        self.assertNotIn('cedar', self.calls[0][1])
        self.assertNotIn('HELDOUT_CANARY_739', self.calls[0][1])

    def test_selftest_all_arms_and_installed_layout(self):
        import shutil, subprocess, sys
        for arm in ('flat','graph','handover'):
            def broken(root,m,q,arm=arm):
                c=ev.build_contexts(root,m,q); c[arm]['text']=''; c[arm]['tokens']=0; return c
            self.assertFalse(ev.eval_selftest(context_builder=broken)['ok'])
        site=self.root/'site'; site.mkdir()
        shutil.copytree(Path(ev.__file__).parent,site/'memlet')
        for name in ('work_memory','memory_vectors','memory_skills','memory_intelligence','memory_cli','memlet_mcp'):
            shutil.copy2(Path(ev.__file__).parent.parent/(name+'.py'),site/(name+'.py'))
        code='from memlet.memlet_eval import eval_selftest; import sys; sys.exit(0 if eval_selftest()["ok"] else 1)'
        r=subprocess.run([sys.executable,'-c',code],cwd=str(site),env=dict(os.environ,PYTHONPATH=str(site)),capture_output=True,text=True)
        self.assertEqual(0,r.returncode,r.stderr)

    def test_two_hundred_questions_bounded_layout_and_blob(self):
        self.init()
        qs=[{'id':'q'+str(i),'text':'Which material worked?', 'citation':{'file':'sessions/b.json','start':1,'end':1},'expectedAny':['cedar']} for i in range(200)]
        self.put('questions.json',qs)
        ev.seal_questions(self.root,run='r',source='questions.json')
        self.output='cedar ' * 10000
        self.run_eval(max_calls=1)
        self.assertEqual(200,len(list((self.root/'.memlet/eval/r/contexts').glob('*.json'))))
        state=(self.root/'.memlet/eval/r/state.json').read_text()
        self.assertNotIn('cedar cedar',state)
        self.assertTrue(ev._state(self.root,'r')['calls'][0]['raw'].startswith('cedar'))

    def test_missing_boundary_and_zero_question_dryrun(self):
        self.init()
        self.assertEqual(0,ev.seal_questions(self.root,run='r',via='codex',max_calls=0,dry_run=True)['calls'])
        self.seal()
        with patch.dict('sys.modules',{'memlet.vendor_cli':None}):
            with self.assertRaises(ImportError):self.run_eval(max_calls=1)
        self.assertEqual([],ev._state(self.root,'r')['calls'])

if __name__ == '__main__':
    unittest.main()
