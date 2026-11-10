"""9.1 integration, no models/network; all configuration and stores live in scratch."""
import copy
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from memlet import memlet_anchors as ma, memlet_bridge as mb, memlet_wiki as mw
from memlet import memlet_hooks as mh, memlet_dream as md, memlet_dreamcycle as dc
from memlet import memlet_eval as ev, vendor_cli as vc, code_anchors as ca, audit
from memlet.vocabulary import HINT_LABEL, HINT_HELP, LESSON_SEPARATOR, JUDGE_UNAVAILABLE
import memlet_mcp

CLI = Path(__file__).resolve().with_name('memory_cli.py')
CODE = 'def validate_token(token):\n    return bool(token)\n'


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memlet-i1-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'repo'
        self.root.mkdir()
        for p in ('home', 'tmp', 'bin'):
            (self.base / p).mkdir()
        env = dict(os.environ, HOME=str(self.base / 'home'), TMPDIR=str(self.base / 'tmp'),
                   CLAUDE_CONFIG_DIR=str(self.base / 'home/claude'), CODEX_HOME=str(self.base / 'home/codex'),
                   GIT_CEILING_DIRECTORIES=str(self.base), GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CONFIG_NOSYSTEM='1', MEMLET_LOCK_DIR=str(self.base / 'locks'), MEMLET_DREAMING='0',
                   MEMLET_HOME=str(self.base / 'unused'), MEMLET_TRANSCRIPT_DIRS=str(self.root))
        for key in ('PYTHONHOME', 'PYTHONEXECUTABLE', '__PYVENV_LAUNCHER__', 'CODEX_THREAD_ID', 'MEMLET_PARENT_SESSION_ID'):
            env.pop(key, None)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(tempfile, 'tempdir', str(self.base / 'tmp'))
        patch.start(); self.addCleanup(patch.stop)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, env=dict(os.environ))
        self.python = None
        for python in dict.fromkeys([sys.executable, shutil.which('python3'), '/usr/bin/python3']):
            if python and Path(python).is_file():
                probe = subprocess.run([python,'-c','pass'],env=dict(os.environ),capture_output=True)
                if probe.returncode == 0:
                    self.python = python
                    break
        self.assertIsNotNone(self.python)
        self.server = memlet_mcp.MemletMCPServer(root=self.base / 'store', default_task='i1')

    def write(self, path, text):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
        return p

    def cli(self, *args):
        return subprocess.run([self.python, str(CLI)] + list(args), cwd=self.root,
                              env=dict(os.environ), capture_output=True, text=True, timeout=40)

    def rpc(self, name, **args):
        result = self.server.handle_rpc({'jsonrpc':'2.0','id':1,'method':'tools/call',
              'params':{'name':name, 'arguments':dict(repo_path=str(self.root), **args)}})
        self.assertNotIn('error', result, result)
        return result['result']

    def hook(self, event, **payload):
        out = io.StringIO()
        self.assertEqual(0, mh.run_hook([event], stdin=io.StringIO(json.dumps(dict(cwd=str(self.root), **payload))),
                                      stdout=out, stderr=io.StringIO(), hard_exit=False))
        return json.loads(out.getvalue())['hookSpecificOutput']['additionalContext']

    def anchors(self):
        self.write('auth.py', CODE)
        self.write('AGENTS.md', '# Rules\n- WRITTEN always use `auth.py::validate_token`.\n')
        self.assertTrue(mw.init_project(self.root)['ok'])
        self.write('.memlet/wiki/auth.md', '# Authentication\n- ACCEPTED always call `validate_token()` in `auth.py`.\n')
        suggestions = ma.suggest_anchors(self.root)['suggestions']
        self.assertEqual(1, len(suggestions), suggestions)
        result = ma.accept_suggestions(self.root, [suggestions[0]['id']])
        self.assertTrue(result['ok'], result)
        return suggestions[0]['id']


class AnchorIntegration(Scratch):
    def test_wiki_drift_through_cli_hook_mcp(self):
        self.anchors()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, env=dict(os.environ))
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True, env=dict(os.environ))
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        '-c', 'core.hooksPath=' + str(self.base / 'no-hooks'), 'commit', '-qm', 'baseline'], check=True, env=dict(os.environ))
        for change, text, reason in [('signature', CODE.replace('(token)', '(token, issuer)', 1), 'signature_drift'),
                                     ('rename', CODE.replace('validate_token', 'renamed_token'), 'missing_symbol'),
                                     ('delete', None, 'missing_file')]:
            with self.subTest(change=change):
                if text is None: (self.root / 'auth.py').unlink()
                else: self.write('auth.py', text)
                cli = self.cli('check', '--changed', '--format', 'json')
                self.assertEqual(1, cli.returncode, cli.stderr)
                changed = json.loads(cli.stdout)
                accepted = next(a for a in changed['affected'] if 'auth.md' in a['source'])
                self.assertEqual(('stale', reason, True), (accepted['status'], accepted['reason'], accepted['suggested']))
                self.assertIn('[suggested anchor]', self.hook('post-tool-use', tool_name='Edit', tool_input={'file_path':str(self.root / 'auth.py')}))
                mcp = self.rpc('memlet_check', files=['auth.py'])
                self.assertIn(reason, mcp['content'][0]['text'])
                self.assertTrue(any('auth.md' in a['source'] for a in mcp['structuredContent']['affected']))

    def assert_labels(self, text):
        written = [line for line in text.splitlines() if 'WRITTEN' in line]
        accepted = [line for line in text.splitlines() if 'ACCEPTED' in line]
        self.assertTrue(written, text)
        self.assertTrue(accepted, text)
        self.assertTrue(all('[suggested anchor]' not in s for s in written), text)
        # Recall puts provenance on the citation header preceding the verbatim quote.
        lines = text.splitlines()
        for line in accepted:
            i = lines.index(line)
            self.assertIn('[suggested anchor]', line if '[suggested anchor]' in line else lines[max(0, i-1)])

    def test_renderer_table_shared_symbol_preserves_rule_provenance(self):
        sid = self.anchors()
        for state, code in [('active', CODE), ('compatible-unverified', CODE.replace('bool(token)', 'not bool(token)')),
                            ('stale', CODE.replace('(token)', '(token, issuer)', 1))]:
            with self.subTest(state=state):
                self.write('auth.py', code)
                handover = mw.build_handover(self.root, budget=5000, output_format='markdown')
                changed = mb.changed_check(self.root, files=['auth.py'])
                renderers = {
                    'handover': handover['formatted'],
                    'recall': mb.unified_recall(self.root, 'always', budget=5000, include=('rules',))['formatted'],
                    'changed': changed['formatted'],
                    'full-check': audit.audit_repository(self.root, output_format='markdown')['formatted'],
                    'mcp-recall': self.rpc('memlet_recall', query='always', budget=5000, include=['rules'])['content'][0]['text'],
                    'mcp-check': self.rpc('memlet_check', files=['auth.py'])['content'][0]['text'],
                    'mcp-handover': self.rpc('memlet_handover', budget=2000)['content'][0]['text'],
                }
                if state == 'active': renderers['SessionStart'] = self.hook('session-start')
                else: renderers['PostToolUse'] = self.hook('post-tool-use', tool_name='Edit', tool_input={'file_path':str(self.root/'auth.py')})
                if state == 'stale': renderers['no-changed-files'] = mb.changed_check(self.root, files=[])['formatted']
                for name, text in renderers.items():
                    with self.subTest(renderer=name): self.assert_labels(text)
                rows = {a['source'].split(':')[0]: a for a in changed['affected']}
                self.assertNotIn('suggested', rows['AGENTS.md'])
                self.assertEqual(sid, rows['.memlet/wiki/auth.md']['sidecarId'])


class PythonRebindings(Scratch):
    def classify(self, text, symbol='validate_token', initial=CODE):
        self.write('auth.py', initial)
        fp = mw._current_fingerprint(self.root, 'auth.py', symbol)
        self.write('auth.py', text)
        return mw.classify_anchor(self.root, {'path':'auth.py','symbol':symbol,'pin':fp})

    def test_binding_table(self):
        tails = ['validate_token = lambda x, y: False', 'validate_token: object = None', 'validate_token += 1',
                 '(validate_token := None)', 'for validate_token in []:\n    pass',
                 'with resource() as validate_token:\n    pass',
                 'try:\n    pass\nexcept Exception as validate_token:\n    pass',
                 'import json as validate_token', 'from json import loads as validate_token',
                 'def validate_token(x):\n    return False', 'class validate_token:\n    pass',
                 'def other():\n    global validate_token\n    validate_token = None',
                 'def other():\n    global validate_token\n    import json as validate_token',
                 'def other():\n    global validate_token\n    def validate_token(x):\n        return x']
        for tail in tails:
            with self.subTest(binding=tail):
                result = self.classify(CODE + '\n' + tail + '\n')
                self.assertEqual(('compatible-unverified','symbol_rebound_in_module'), (result['status'],result['reason']))

    def test_class_method_rebound_and_unrelated_controls(self):
        initial = 'class Token:\n    def validate(self, x):\n        return x\n'
        for text in [initial+'    validate = lambda self, x: False\n', initial+'Token.validate = lambda self, x: False\n']:
            self.assertEqual('symbol_rebound_in_module', self.classify(text, 'Token.validate', initial)['reason'])
        for tail in ['def unrelated(x):\n    return x', 'def unrelated():\n    validate_token = None',
                     'x = [validate_token for validate_token in ()]', 'class Unrelated:\n    validate_token = None']:
            self.assertEqual('active', self.classify(CODE+'\n'+tail+'\n')['status'])
        overloads = 'from typing import overload\n@overload\ndef validate_token(x: str) -> bool: ...\n@overload\ndef validate_token(x: bytes) -> bool: ...\n'+CODE
        self.assertEqual('active', self.classify(overloads, initial=overloads)['status'])
        # Every check reads bindings again, even when a stat-keyed fingerprint cache would match.
        self.write('auth.py', CODE+'#'+' '*42+'\n')
        fp = mw._current_fingerprint(self.root,'auth.py','validate_token')
        stamp = (self.root/'auth.py').stat()
        self.write('auth.py',CODE+'validate_token = None'.ljust(43)+'\n')
        os.utime(str(self.root/'auth.py'), ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        self.assertEqual('symbol_rebound_in_module', mw.classify_anchor(self.root, {'path':'auth.py','symbol':'validate_token','pin':fp})['reason'])


class EvalIntegration(Scratch):
    def prepare(self, keys=True):
        self.write('sessions/dream.json', json.dumps([{'timestamp':'2026-01-01','text':'Repair uses cedar.'}]))
        self.write('sessions/heldout.json', json.dumps([{'timestamp':'2026-01-03','text':'Cedar fixed it.'}]))
        ev.init_eval(self.root, run='r', sessions='sessions', dream_until='2026-01-02', budget=256)
        q = {'id':'q','text':'Which material worked?','citation':{'file':'sessions/heldout.json','start':1,'end':1}}
        if keys: q['expectedAny']=['cedar']
        self.write('questions.json',json.dumps([q]))
        ev.seal_questions(self.root,run='r',source='questions.json')

    def test_static_refusal_precedes_any_call_or_state(self):
        self.prepare()
        before = {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with mock.patch.object(vc, 'run_vendor', side_effect=AssertionError('must not launch')), mock.patch.object(vc.subprocess, 'run', side_effect=AssertionError('must not probe')):
            for op in (lambda: ev.run_eval(self.root,run='r',via='codex'),
                       lambda: ev.seal_questions(self.root,run='r',via='codex'),
                       lambda: ev.run_eval(self.root,run='r',via='gemini',dry_run=True)):
                with self.assertRaisesRegex(ValueError,'no verified tool-free'): op()
            for vendor in vc.VENDORS:
                with self.subTest(judge=vendor), self.assertRaisesRegex(ValueError, 'judge from a different vendor cannot exist in practice'):
                    ev.score_eval(self.root,run='r',judge=vendor)
        after = {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        cli = self.cli('admin','eval','run','--run','r','--via','codex')
        self.assertEqual(vc.static_vendor_refusal('codex'),json.loads(cli.stdout)['error'])
        self.assertEqual(2,cli.returncode)
        help_text = self.cli('admin','eval','score','--help').stdout
        self.assertIn(' '.join(JUDGE_UNAVAILABLE.split()),' '.join(help_text.split()))

    def test_question_preview_and_help_vocabulary(self):
        self.prepare()
        ev.init_eval(self.root, run='questions', sessions='sessions', dream_until='2026-01-02')
        with mock.patch.object(vc.subprocess, 'run', side_effect=AssertionError('dry-run must not launch')):
            preview = ev.seal_questions(self.root, run='questions', via='claude', dry_run=True)
        self.assertEqual('tool-free', preview['launches'][0]['isolation'])
        self.assertEqual(vc._claude_argv('haiku'), preview['launches'][0]['argv'][:-1])
        self.assertEqual([], ev._state(self.root, 'questions')['calls'])
        for args in [('--help',), ('admin', '--help')]:
            help_text = self.cli(*args).stdout
            self.assertIn('dream', help_text)
            self.assertNotIn('memlet dream:', help_text)
            for command in ('memlet admin anchors:', 'memlet admin eval:'):
                self.assertIn(command, help_text)
        from memlet import vocabulary as v
        self.assertIs(mb.HINT_LABEL, v.HINT_LABEL)
        self.assertEqual((v.FILE_SYMBOL, v.SINGLE_DEFINITION_FOUND), ma.CONFIDENCES)
        self.assertEqual(25, len(memlet_mcp.mcp_tools()))

    def fake_cli(self):
        script = self.base/'bin/claude'
        config = self.base/'home/claude'
        config.mkdir(exist_ok=True)
        auth = json.dumps({'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty',
                           'subscriptionType':'fixture','configDirectory':str(config)})
        script.write_text('#!/bin/sh\nif [ "$1" = auth ]; then\n printf "%s\\n" '+shlex.quote(auth)+'\nelse\n printf "%s\\n" cedar\nfi\n')
        script.chmod(0o700)
        return mock.patch.dict(os.environ, {'PATH':str(self.base/'bin')})

    def test_real_runner_shape_identity_and_dry_run_plan(self):
        self.prepare()
        # Real boundary + fake /bin/sh executable on PATH: no Python child under the allowlist.
        with self.fake_cli():
            real = ev._call('claude','fixture',None)
            self.assertEqual(('claude','haiku','cedar\n'), (real['vendor'],real['model'],real['text']))
            preview = ev.run_eval(self.root,run='r',via='claude',arms=['none'],dry_run=True,max_calls=1)
            self.assertEqual(vc._claude_argv('haiku'),preview['launches'][0]['argv'][:-1])
            self.assertEqual('tool-free',preview['launches'][0]['isolation'])
            self.assertIn('argv:',preview['formatted'])
            with mock.patch.object(vc,'_capture',wraps=vc._capture) as capture:
                result=ev.run_eval(self.root,run='r',via='claude',arms=['none'],max_calls=1)
                self.assertEqual(1,capture.call_count)
            self.assertEqual(1,result['answers'])
            answer=ev._state(self.root,'r')['answers']['q:none']
            self.assertEqual(('claude','haiku'),(answer['vendor'],answer['model']))
            self.assertEqual(1,ev.score_eval(self.root,run='r')['scores'])


class MultiLineLessons(Scratch):
    symptom = 'The nightly export keeps failing with a lock timeout.'
    fix = 'Waiting for the migration to finish and then running the export once fixed the timeout.'

    def prepare(self):
        self.write('export.py','def export_data():\n    return 1\n')
        self.write('AGENTS.md','# Instructions\n')
        self.assertTrue(mw.init_project(self.root)['ok'])
        source=self.write('session.log', '\n'.join([self.symptom,'intermediate line','another line',self.fix])+'\n')
        self.assertTrue(dc.dream_ingest(self.root,source)['ok'])
        batch=dc.dream_next(self.root)
        f=batch['rows'][0][0]
        candidate={'category':'verified_workaround','title':'Export after migration','scope_tag':'export.py::export_data',
                   'src_spans':[f+':1',f+':4'],'edges':[{'edge_type':'RESOLVES','target':'export.py','src_span':f+':4'}]}
        return candidate

    def test_all_readers_and_digests(self):
        candidate=self.prepare()
        result=dc.dream_run(self.root,via=('claude','codex'),runner=lambda *a,**k:json.dumps([candidate]))
        self.assertTrue(result['ok'],result)
        node=mb._load_queue(self.root)[0]['nodes'][0]
        before=(md.node_approval_digest(node),dc.trust_digest(node))
        item=mb.dream_items(self.root,tiers=('hint',))['items'][0]
        self.assertEqual(self.symptom+LESSON_SEPARATOR+self.fix,item['text'])
        self.assertEqual([1,4],[c['start'] for c in item['cites']])
        for text in [mb.unified_recall(self.root,'export',budget=5000)['formatted'],
                     mw.build_handover(self.root,budget=5000,output_format='markdown')['formatted'],
                     mb.changed_check(self.root,files=['export.py'])['formatted'], mb.learn_pending(self.root)['formatted']]:
            self.assertIn(self.symptom,text); self.assertIn(self.fix,text)
            self.assertIn('[RESOLVES]' if text.startswith('## Memlet Handover') else 'RESOLVES source',text)
        from memlet.memlet_cortex import _load_dream_lineage
        cortex=_load_dream_lineage({'path':str(self.root)})
        self.assertEqual([self.symptom,self.fix],cortex['nodes'][0]['quoteLines'])
        self.assertEqual(before,(md.node_approval_digest(node),dc.trust_digest(node)))
        approved=mb.learn_approve(self.root,[node['id']],approver='Fixture reviewer')
        self.assertTrue(approved['ok'],approved)
        handover=mw.build_handover(self.root,budget=5000,output_format='markdown')
        self.assertIn(self.fix,handover['formatted'])
        self.assertTrue(any(self.fix in r['rule'] for r in handover['activeConstraints']),handover)
        self.assertTrue(mw.verify_handover_markdown(handover['formatted']))
        changed=mb.changed_check(self.root,files=['export.py'])
        self.assertTrue(any(self.fix in r['rule'] for r in changed['affected']),changed)
        fresh=mb._load_queue(self.root)[0]['nodes'][0]
        self.assertEqual('approved',md.approval_state(fresh,'node'))
        mutated=copy.deepcopy(fresh); mutated['allPins'][-1]['sha256']='0'*64
        self.assertNotEqual(md.node_approval_digest(fresh),md.node_approval_digest(mutated))
        self.assertIsNotNone(mb._node_edges_verify(mutated, mb._load_snapshot(self.root)[0]))
        mutated["allPins"][-1]["quoteText"] = "changed fix"
        mutated["allPins"][-1]["quoteLines"] = ["changed fix"]
        self.assertNotEqual(dc.trust_digest(fresh),dc.trust_digest(mutated))
        self.assertNotEqual(md.lesson_quote_digest(fresh),md.lesson_quote_digest(dict(fresh,allPins=[fresh['primaryPin']])))
        # Rendering cannot manufacture the reserved separator from source text.
        self.assertNotIn('⟶',mb.safe_lesson_display('literal ⟶ source'))

    def test_full_quote_set_rejection_and_eval_arms(self):
        candidate = self.prepare()
        dc.dream_run(self.root, via=('claude','codex'), runner=lambda *a, **k: json.dumps([candidate]))
        graph = mb._load_queue(self.root)[0]
        original = graph['nodes'][0]
        self.assertTrue(mb.learn_reject(self.root, [original['id']], approver='Reviewer')['ok'])
        # Same symptom, a different cited set is not the rejected full set.
        graph = mb._load_queue(self.root)[0]
        config = mb.tier_config(graph, {})
        different = copy.deepcopy(original)
        different['allPins'] = different['allPins'][:1]
        self.assertNotIn(md.lesson_quote_digest(different), config['_dreamRejectedQuoteHashes'])
        self.assertIn(md.lesson_quote_digest(original), config['_dreamRejectedQuoteHashes'])
        self.assertTrue(mb.learn_approve(self.root, [original['id']], approver='Reviewer', reconsider=True)['ok'])
        snap = mb._load_snapshot(self.root)[0]
        manifest = {'run':'synthetic', 'protocol':'retrospective', 'hints':False, 'budget':4000,
                    'dreamUntil':'2026-01-02', 'dream':[{'file':f,'lines':lines} for f,lines in snap.items()]}
        contexts = ev.build_contexts(self.root, manifest, {'text':'nightly export timeout'})
        for arm in ('flat','graph','handover'):
            self.assertIn(self.symptom, contexts[arm]['text'], arm)
            self.assertIn(self.fix, contexts[arm]['text'], arm)
        self.assertTrue(mw.verify_handover_markdown(contexts['handover']['text']))
        # A tampered saved pin hash must not become a new display-time baseline.
        graph = mb._load_queue(self.root)[0]
        bad_hash = copy.deepcopy(graph)
        bad_hash['nodes'][0]['allPins'][-1]['sha256'] = '0' * 64
        mw.write_managed_text(self.root, mb.QUEUE_REL, json.dumps(bad_hash))
        items = mb.dream_items(self.root, tiers=('pending',))['items']
        self.assertTrue(items)
        self.assertFalse(items[0]['cites'][-1]['verified'])
        # A tampered pending quote is never marked verified by the display adapter.
        graph['nodes'][0]['allPins'][-1]['quoteText'] = 'wrong fix'
        graph['nodes'][0]['allPins'][-1]['quoteLines'] = ['wrong fix']
        mw.write_managed_text(self.root, mb.QUEUE_REL, json.dumps(graph))
        items = mb.dream_items(self.root, tiers=('pending',))['items']
        self.assertTrue(items)
        self.assertFalse(items[0]['cites'][-1]['verified'])

    def test_rejection_records_are_bounded_safe_and_persisted(self):
        candidate=self.prepare()
        bad=dict(candidate,category='repeated_pitfall',title='Withdraw force option',src_spans=candidate['src_spans'][:1],
                 edges=[{'edge_type':'SUPERSEDES','target':'old advice'}], narrative='UNSTORED MODEL NARRATIVE')
        result=dc.dream_run(self.root,runner=lambda *a,**k:json.dumps([bad]))
        self.assertFalse(result['ok'],result)
        self.assertEqual(1,result['rejectedCandidates'])
        record=result['rejections'][0]
        self.assertEqual('supersedes_missing_target_pin',record['reasonCode'])
        self.assertIn('BLOCKS',record['note'])
        self.assertNotIn('UNSTORED MODEL NARRATIVE',json.dumps(result))
        status=dc.dream_status(self.root)
        self.assertEqual(record,status['runs'][0]['rejections'][0])
        from memlet.cli_dream import _markdown
        self.assertIn(record['reasonCode'],_markdown(result))
        self.assertIn(record['reasonCode'],_markdown(status))
        self.assertIn('withdrawing line',dc.PROPOSE_PROMPT)
        target={}
        dc._keep_rejections(target,[record]*200)
        self.assertLessEqual(len(target['rejections']),dc.MAX_REJECTION_RECORDS)
        self.assertLessEqual(len(json.dumps(target['rejections'],ensure_ascii=False).encode()),dc.MAX_REJECTION_BYTES)
        self.assertEqual(200,len(target['rejections'])+target['rejectionsOmitted'])

    def test_hint_heading_and_caveat_identical(self):
        candidate=self.prepare()
        dc.dream_run(self.root,via=('claude','codex'),runner=lambda *a,**k:json.dumps([candidate]))
        for text in [mb.unified_recall(self.root,'export',budget=5000)['formatted'],
                     mw.build_handover(self.root,budget=5000,output_format='markdown')['formatted'],
                     mb.changed_check(self.root,files=['export.py'])['formatted']]:
            self.assertIn('### '+HINT_LABEL+'\n'+HINT_HELP+'\n',text)
            self.assertEqual(1,text.count(HINT_HELP))


class DreamPromptIntegration(Scratch):
    lines = [
        '2026-11-11T10:00:00Z [user] Deploys go out with make release.',
        '2026-11-11T10:05:00Z [assistant] The deploy failed because the image tag was stale.',
        '2026-11-11T10:06:00Z [user] Replace make release with scripts/deploy.sh for deploys.',
        '2026-11-11T10:09:00Z [assistant] Refreshing the image tag fixed the deploy failure.',
        '2026-11-11T10:10:00Z [user] Never retry deploys with a stale image tag.',
    ]
    meaning = 'the cited id does not match a line that was shown'

    def prepare(self, name='dream', lines=None):
        repo = self.root / name
        repo.mkdir()
        (repo / 'AGENTS.md').write_text('# Fixture\n', encoding='utf-8')
        self.assertTrue(mw.init_project(repo)['ok'])
        config, _ = mb._load_config(repo)
        config['dream'] = {'agreement': 'model', 'models': {'claude': 'haiku', 'verifier': {'claude': 'sonnet'}}}
        mw.write_managed_text(repo, mw.CONFIG_PATH, json.dumps(config))
        source = repo / 'session2.log'
        source.write_text('\n'.join(self.lines if lines is None else lines) + '\n', encoding='utf-8')
        self.assertTrue(dc.dream_ingest(repo, source)['ok'])
        batch = dc.dream_next(repo)
        return repo, batch, batch['rows'][0][0]

    def candidates(self, fid, tails=True):
        def span(n):
            return '%s:%d' % (fid, n) + (' ' + self.lines[n-1].split(' ')[0] if tails else '')
        return [
            {'category': 'superseded_directive', 'title': 'Deploy with scripts/deploy.sh, not make release',
             'scope_tag': 'deploy', 'src_spans': [span(1), span(3)], 'superseded_by_src': span(3),
             'edges': [{'edge_type': 'SUPERSEDES', 'target': 'make release', 'src_span': span(3), 'target_span': span(1)}]},
            {'category': 'verified_workaround', 'title': 'Refresh the image tag before deployment',
             'scope_tag': 'deploy', 'src_spans': [span(2), span(4)],
             'edges': [{'edge_type': 'RESOLVES', 'target': 'stale image tag', 'src_span': span(4), 'target_span': span(2)}]},
            {'category': 'repeated_pitfall', 'title': 'Do not retry with a stale image tag',
             'scope_tag': 'deploy', 'src_spans': [span(5)],
             'edges': [{'edge_type': 'BLOCKS', 'target': 'stale image tag', 'src_span': span(5)}]},
        ]

    def submit(self, repo, candidates, lane, verify=False, seen=None):
        if lane == 'host':
            batch = dc.dream_next(repo)
            return dc.dream_submit(repo, batch['runId'], batch['batchId'], candidates, proposer='host')
        def runner(vendor, prompt, **kwargs):
            if seen is not None:
                seen.append((prompt, kwargs['stdin_text']))
            return '{"verdict":"SUPPORTED"}' if prompt == dc.VERIFY_PROMPT else json.dumps(candidates)
        return dc.dream_run(repo, runner=runner, verify_with='claude' if verify else None)

    def state(self, repo, result):
        return json.loads((repo / dc.RUNS_REL / (result['runId'] + '.json')).read_text())

    def test_real_model_timestamp_spans_proposer_and_host(self):
        for lane in ('trusted', 'host'):
            with self.subTest(lane=lane):
                repo, batch, fid = self.prepare(lane)
                candidates = self.candidates(fid)
                before = copy.deepcopy(candidates)
                result = self.submit(repo, candidates, lane, verify=True)
                self.assertTrue(result['ok'], result)
                self.assertEqual(before, candidates, 'normalization must not mutate caller data')
                self.assertEqual((11, 0, 0), (result['normalizedSpans'], result['rejectedCandidates'], result['failedBatches']))
                state = self.state(repo, result)
                self.assertEqual(11, state['normalizedSpans'])
                self.assertEqual(1, len(state['completed']))
                graph, _ = mb._load_queue(repo)
                self.assertEqual(3, len(graph['nodes']))
                snapshot, _ = mb._load_snapshot(repo)
                self.assertTrue(md.verify_ontology_contract(graph, snapshot)['ok'])
                for node in graph['nodes']:
                    for pin in dc.evidence_pins(node):
                        self.assertIsNotNone(md.parse_span(pin['span']))
                        self.assertNotIn(' ', pin['span'])
                        self.assertIsNone(md._verify_pin_dict(pin, snapshot, 'test'))
                supersedes = next(n for n in graph['nodes'] if n['category'] == 'superseded_directive')
                self.assertEqual(fid + ':3', supersedes['supersededByPin']['span'])
                self.assertEqual(fid + ':1', supersedes['primaryPin']['span'])
                if lane == 'trusted':
                    # Replacement directives retain the existing human-review gate.
                    self.assertEqual(3, result['calls'])
                    self.assertEqual(2, mb.dream_items(repo)['counts']['hint'])
                else:
                    self.assertEqual(0, mb.dream_items(repo)['counts']['hint'])

    def test_mixed_all_invalid_and_empty_batches_both_lanes(self):
        for lane in ('trusted', 'host'):
            for case in ('mixed', 'all_invalid', 'empty'):
                with self.subTest(lane=lane, case=case):
                    repo, batch, fid = self.prepare(lane + '-' + case)
                    good = self.candidates(fid)[2]
                    bad = copy.deepcopy(good)
                    bad['src_spans'] = ['missing.log:1 2026-11-11T10:10:00Z']
                    candidates = [good, bad] if case == 'mixed' else [bad] if case == 'all_invalid' else []
                    result = self.submit(repo, candidates, lane)
                    failed = case == 'all_invalid'
                    self.assertEqual(not failed, result['ok'], result)
                    self.assertEqual(int(case != 'empty'), result['rejectedCandidates'])
                    self.assertEqual(int(failed), result['failedBatches'])
                    self.assertEqual(2 if failed else 0, result['exitCode'])
                    state = self.state(repo, result)
                    self.assertEqual(not failed, bool(state.get('completed')))
                    self.assertEqual(failed, bool(state.get('failed')))
                    progress_path = repo / (dc.STATE_REL if lane == 'trusted' else dc.HOST_STATE_REL)
                    sessions = json.loads(progress_path.read_text())['sessions'] if progress_path.exists() else {}
                    self.assertEqual(0 if failed else 5, sessions.get(fid, {}).get('dreamed', 0))
                    graph, _ = mb._load_queue(repo)
                    self.assertEqual(1 if case == 'mixed' else 0, len((graph or {}).get('nodes', [])))
                    if failed:
                        self.assertIn('remains undreamed', result['error'])
                        failure = next(iter(state['failed'].values()))
                        self.assertEqual(result['rejections'], failure['rejections'])
                        self.assertNotIn('rawOutput', failure)
                        self.assertEqual(1, dc.dream_status(repo)['failedBatches'])
                        if lane == 'trusted':
                            self.assertEqual(1, result['calls'], 'no retry within the failed invocation')
                            self.assertEqual(1, dc.dream_status(repo)['waiting'])
                        retried = self.submit(repo, [good], lane)
                        self.assertTrue(retried['ok'], retried)
                        self.assertEqual(result['runId'], retried['runId'])
                        self.assertFalse(self.state(repo, retried).get('failed'))
                        self.assertEqual(0, dc.dream_status(repo)['failedBatches'])

    def test_span_tail_never_stored_or_output_even_on_rejection(self):
        from memlet.cli_dream import _markdown
        for lane in ('trusted', 'host'):
            with self.subTest(lane=lane):
                repo, batch, fid = self.prepare(lane)
                good = self.candidates(fid, tails=False)[2]
                good['src_spans'] = [fid + ':5 DISCARDED_SOURCE_TAIL']
                good['edges'][0].update(src_span=fid + ':5 DISCARDED_EDGE_TAIL', target_span=fid + ':4 DISCARDED_TARGET_TAIL')
                supersedes = self.candidates(fid, tails=False)[0]
                supersedes['superseded_by_src'] += ' DISCARDED_SUPERSEDED_TAIL'
                missing = copy.deepcopy(good)
                missing['src_spans'] = ['missing.log:1 DISCARDED_MISSING_TAIL']
                schema = copy.deepcopy(good)
                schema['title'] = 'word ' * 16
                schema['src_spans'] = [fid + ':5 DISCARDED_SCHEMA_TAIL']
                unsafe = copy.deepcopy(good)
                unsafe['src_spans'] = [fid + ':5 DISCARDED_UNSAFE_TAIL\u200b']
                invalid = copy.deepcopy(good)
                invalid['src_spans'] = ['bad-id DISCARDED_MALFORMED_TAIL']
                result = self.submit(repo, [good, supersedes, missing, schema, unsafe, invalid], lane, verify=True)
                self.assertTrue(result['ok'], result)
                self.assertEqual(4, result['rejectedCandidates'])
                status = dc.dream_status(repo)
                outputs = [json.dumps(result), _markdown(result), json.dumps(status), _markdown(status),
                           mb.learn_pending(repo)['formatted'], mb.unified_recall(repo, 'deploy', budget=4000)['formatted'],
                           mw.build_handover(repo, output_format='markdown')['formatted']]
                for output in outputs:
                    self.assertNotIn('DISCARDED_', output)
                for path in (repo / '.memlet').rglob('*'):
                    if path.is_file():
                        self.assertNotIn(b'DISCARDED_', path.read_bytes(), str(path))

    def test_first_id_only_never_searches_tail_or_partial_id(self):
        for lane in ('trusted', 'host'):
            repo, batch, fid = self.prepare(lane)
            valid = self.candidates(fid, tails=False)[2]
            valid['src_spans'] = [fid + ':1-2 ' + fid + ':4']
            valid['edges'] = [{'edge_type': 'BLOCKS', 'target': 'deploy'}]
            bad = []
            for value in ('missing.log:1 ' + fid + ':5', 'prefix ' + fid + ':5', fid + ':1x ' + fid + ':5',
                          fid + ':0 ' + fid + ':5', fid + ':3-2 ' + fid + ':5', ' ' + fid + ':5',
                          fid + ':1-5 ' + fid + ':5', fid + ':999 ' + fid + ':5', '../' + fid + ':1 ' + fid + ':5'):
                candidate = copy.deepcopy(valid)
                candidate['src_spans'] = [value]
                bad.append(candidate)
            result = self.submit(repo, [valid] + bad, lane)
            self.assertTrue(result['ok'], result)
            self.assertEqual(len(bad), result['rejectedCandidates'])
            graph, _ = mb._load_queue(repo)
            self.assertEqual([fid + ':1-2'], [p['span'] for p in graph['nodes'][0]['allPins']])
            self.assertEqual([fid + ':1', fid + ':2'], [p['span'] for p in md.lesson_lines(graph['nodes'][0])])
            self.assertEqual(4, result['normalizedSpans'])

    def test_unsafe_characters_in_tail_still_fail_every_span_field(self):
        for lane in ('trusted', 'host'):
            repo, batch, fid = self.prepare(lane)
            bad = []
            for field in ('src_spans', 'superseded_by_src', 'src_span', 'target_span'):
                for char in ('\n', '\t', '\x1b', '\u200b', '\U000e0041'):
                    candidate = self.candidates(fid, tails=False)[0]
                    value = fid + ':3 harmless tail' + char
                    if field == 'src_spans':
                        candidate[field] = [value]
                    elif field == 'superseded_by_src':
                        candidate[field] = value
                    else:
                        candidate['edges'][0][field] = value
                    bad.append(candidate)
            result = self.submit(repo, bad, lane)
            self.assertFalse(result['ok'], result)
            self.assertEqual(0, result['normalizedSpans'])
            self.assertEqual(len(bad), result['rejectedCandidates'])
            self.assertEqual({'candidate_schema_invalid'}, {r['reasonCode'] for r in result['rejections']})
            self.assertFalse(self.state(repo, result).get('completed'))

    def test_proposer_preview_mcp_and_verifier_rows_match(self):
        repo, batch, fid = self.prepare()
        response = self.server.handle_rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'memlet_dream_next', 'arguments': {'repo_path': str(repo)}}})
        mcp = response['result']['structuredContent']
        expected = [{'id': '%s:%d' % (f, n), 'text': t} for f, n, t in mcp['rows'] if t is not None]
        self.assertEqual(expected, [json.loads(row) for row in mcp['stdinText'].splitlines()])
        before = {str(p): p.read_bytes() for p in (repo / '.memlet').rglob('*') if p.is_file()}
        preview = dc.dream_run(repo, dry_run=True)
        self.assertEqual(before, {str(p): p.read_bytes() for p in (repo / '.memlet').rglob('*') if p.is_file()})
        self.assertEqual(mcp['stdinText'], preview['prompts'][0]['stdinText'])
        seen = []
        result = self.submit(repo, self.candidates(fid), 'trusted', verify=True, seen=seen)
        self.assertTrue(result['ok'], result)
        self.assertEqual(3, len(seen))
        for prompt, data in seen:
            self.assertIn('cite the `id` values exactly', prompt)
            rows = json.loads(data)['context'] if prompt == dc.VERIFY_PROMPT else [json.loads(row) for row in data.splitlines()]
            self.assertEqual(expected, rows)
            # Each rendered row occupies its own line, even inside verifier's JSON envelope.
            for row in expected:
                self.assertIn(json.dumps(row, ensure_ascii=False), data.splitlines() if prompt == dc.PROPOSE_PROMPT
                              else [line.rstrip(',') for line in data.splitlines()])

    def test_json_rows_keep_bounds_masks_omissions_and_determinism(self):
        lines = ['Quote "data" \\ path. Never retry. sk-' + 'x' * 30, 'oversize ' * 150] + ['Short row %d' % n for n in range(12)]
        repo, _, fid = self.prepare(lines=lines)
        batch = dc.dream_next(repo, batch_tokens=128)
        self.assertEqual(batch, dc.dream_next(repo, batch_tokens=128))
        seen, omitted = [], 0
        while not batch['done']:
            rows = [json.loads(row) for row in batch['stdinText'].splitlines()]
            self.assertEqual([{'id': '%s:%d' % (f, n), 'text': t} for f, n, t in batch['rows'] if t is not None], rows)
            self.assertLessEqual(mw.estimate_tokens(batch['stdinText']), 128)
            self.assertNotIn('sk-' + 'x' * 30, batch['stdinText'])
            omitted += batch['omittedLines']
            seen.extend(rows)
            if any(t is None for _, _, t in batch['rows']):
                candidate = self.candidates(fid, tails=False)[2]
                candidate['src_spans'] = [fid + ':2 unused tail']
                candidate['edges'] = [{'edge_type': 'BLOCKS', 'target': 'deploy'}]
                failed = dc.dream_submit(repo, batch['runId'], batch['batchId'], [candidate], proposer='host')
                self.assertFalse(failed['ok'])
                self.assertEqual('citation_outside_batch', failed['rejections'][0]['reasonCode'])
            self.assertTrue(dc.dream_submit(repo, batch['runId'], batch['batchId'], [], proposer='host')['ok'])
            batch = dc.dream_next(repo, run_id=batch['runId'])
        self.assertEqual(1, omitted)
        self.assertEqual(len(lines) - 1, len(seen))
        self.assertNotIn(fid + ':2', [row['id'] for row in seen])

    def test_second_proposer_all_rejected_does_not_advance(self):
        repo, batch, fid = self.prepare()
        good = self.candidates(fid)[2]
        bad = dict(good, src_spans=['missing.log:1 timestamp'])
        result = dc.dream_run(repo, via=('claude', 'codex'), runner=lambda v, p, **k: json.dumps([good if v == 'claude' else bad]))
        self.assertFalse(result['ok'])
        self.assertEqual(2, result['calls'])
        self.assertEqual(1, len(result['items']))
        self.assertFalse(self.state(repo, result).get('completed'))
        self.assertEqual(1, dc.dream_status(repo)['waiting'])

    def test_mcp_host_all_rejected_then_timestamp_retry(self):
        repo, batch, fid = self.prepare()
        good = self.candidates(fid)[2]
        for candidates, expected in (([dict(good, src_spans=['missing.log:1 discarded'])], False), ([good], True)):
            response = self.server.handle_rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'memlet_dream_submit', 'arguments': {'repo_path': str(repo),
                           'run_id': batch['runId'], 'batch_id': batch['batchId'], 'candidates': candidates}}})
            result = response['result']['structuredContent']
            self.assertEqual(expected, result['ok'], response)
            self.assertEqual(expected, bool(self.state(repo, result).get('completed')))
        self.assertEqual(2, result['normalizedSpans'])
        self.assertFalse(self.state(repo, result).get('failed'))

    def test_cli_json_markdown_rejections_first_and_exit_codes_with_fake_cli(self):
        # Exercise the actual CLI boundary using only a fake /bin/sh executable.
        fake = self.base / 'bin/claude'
        config = self.base / 'home/claude'
        config.mkdir(exist_ok=True)
        auth = json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty',
                           'subscriptionType': 'fixture', 'configDirectory': str(config)})
        fake.write_text('#!/bin/sh\ncase "$1" in\n--version) printf "fixture-claude 1\\n";;\nauth) printf "%s\\n" ' + shlex.quote(auth) + ';;\n*) /bin/cat ' +
                        shlex.quote(str(self.base / 'answer.json')) + ';;\nesac\n', encoding='utf-8')
        fake.chmod(0o700)
        for case in ('failed', 'mixed', 'empty'):
            for fmt in ('json', 'markdown'):
                with self.subTest(case=case, format=fmt):
                    repo, batch, fid = self.prepare(case + '-' + fmt)
                    good = self.candidates(fid)[2]
                    bad = dict(good, src_spans=['missing.log:1 CLI_DISCARDED_TAIL'])
                    answer = [] if case == 'empty' else [bad] if case == 'failed' else [good, bad]
                    (self.base / 'answer.json').write_text(json.dumps(answer), encoding='utf-8')
                    with mock.patch.dict(os.environ, {'PATH': str(self.base / 'bin') + ':/usr/bin:/bin'}):
                        command = self.cli('dream', str(repo), '--via', 'claude', '--format', fmt)
                    self.assertEqual(2 if case == 'failed' else 0, command.returncode, command.stderr + command.stdout)
                    self.assertEqual('', command.stderr)
                    self.assertNotIn('CLI_DISCARDED_TAIL', command.stdout)
                    if fmt == 'json':
                        output = json.loads(command.stdout)
                        if case != 'empty':
                            self.assertEqual(['rejectedCandidates', 'rejectionSummary'], list(output)[:2])
                            self.assertEqual(self.meaning, output['rejectionSummary']['unpinnable_citation'])
                        text = output['formatted']
                    else:
                        text = command.stdout
                    if case != 'empty':
                        self.assertIn(self.meaning, text)
                        self.assertLess(text.index('Rejected candidates:'), text.index('ERROR:' if case == 'failed' else 'Batches:'))
                        status = self.cli('dream', str(repo), '--status', '--format', 'json')
                        report = json.loads(status.stdout)
                        self.assertEqual(['rejectedCandidates', 'rejectionSummary'], list(report)[:2])
                        self.assertIn(self.meaning, report['formatted'])
                        self.assertLess(report['formatted'].index('Rejected candidates:'), report['formatted'].index('waiting to dream'))




class ApprovedReadersIntegration(Scratch):
    """The live five-line queue shape, built by ingest + injected proposers, never copied."""
    kinds = ('RESOLVES', 'BLOCKS', 'GOVERNS', 'SUPERSEDES', 'multi-line', 'single-line',
             'cross_session_conflict', 'open_trap')

    def save_graph(self,repo,graph):
        previous=md.graph_sha256(mb._load_queue(repo)[0])
        md._commit_queue_and_db(repo/mb.QUEUE_REL,json.dumps(graph),repo/mb.DB_REL,graph,replaces_sha=previous)

    def fixture(self, state='approved', kinds=None):
        repo = self.root / state
        repo.mkdir()
        (repo / 'AGENTS.md').write_text('# Fixture\n- Always keep `anchor.py::guard` enabled.\n')
        (repo / 'anchor.py').write_text('def guard():\n    return True\n')
        (repo / 'deploy.py').write_text('# scope file with no symbol anchor\n')
        self.assertTrue(mw.init_project(repo)['ok'])
        subprocess.run(['git','-C',str(repo),'init','-q'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(repo),'add','AGENTS.md','anchor.py','deploy.py'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                        '-c','core.hooksPath='+str(self.base/'no-hooks'),'commit','-qm','fixture'],check=True,capture_output=True)
        (repo/'deploy.py').write_text('# changed scope file\n')
        config, _ = mb._load_config(repo)
        config['dream'] = {'agreement': 'model', 'models': {'claude':'haiku', 'verifier':{'claude':'sonnet'}}}
        mw.write_managed_text(repo, mw.CONFIG_PATH, json.dumps(config))
        lines, specs = [], []
        for kind in kinds or self.kinds:
            start = len(lines) + 1
            text = 'Deploy %s requires a warm cache before the smoke check.' % kind
            if kind == 'SUPERSEDES':
                text = 'Deploys go out with `make release` from the main branch.'
            lines.append('2026-11-11T10:%02d:00Z [user] %s' % (len(lines), text))
            if kind in ('RESOLVES', 'multi-line', 'SUPERSEDES'):
                text = ('From now on do not use make release; deploy only with scripts/deploy.sh, which warms the cache first.'
                        if kind == 'SUPERSEDES' else 'Deploy %s succeeds after warming the cache; the smoke test passed.' % kind)
                lines.append('2026-11-11T10:%02d:00Z [assistant] %s' % (len(lines), text))
            specs.append((kind, start, len(lines)))
        if kinds == ('RESOLVES','BLOCKS'):
            lines = [line + ' Preserve the warm cache through the entire smoke test.' * 18 for line in lines]
        source = repo / 'session.log'
        source.write_text('\n'.join(lines) + '\n')
        self.assertTrue(dc.dream_ingest(repo, source)['ok'])
        fid = dc.dream_next(repo)['rows'][0][0]
        candidates = []
        for kind, start, end in specs:
            edge = kind if kind in ('RESOLVES','BLOCKS','GOVERNS','SUPERSEDES') else 'RESOLVES'
            cat = {'SUPERSEDES':'superseded_directive', 'BLOCKS':'repeated_pitfall',
                   'GOVERNS':'repeated_pitfall', 'cross_session_conflict':'cross_session_conflict',
                   'open_trap':'verified_workaround'}.get(kind, 'verified_workaround')
            spans = ['%s:%d' % (fid, n) for n in range(start, end + 1)]
            item = {'category':cat, 'title':kind, 'scope_tag':'deploy.py', 'src_spans':spans,
                    'edges':[{'edge_type':edge, 'target':'deploy cache ' + kind, 'src_span':spans[-1]}]}
            if start != end:
                item['edges'][0]['target_span'] = spans[0]
            if kind == 'SUPERSEDES': item['superseded_by_src'] = spans[-1]
            candidates.append(item)
        def runner(vendor, prompt, **kwargs):
            return json.dumps(candidates)
        self.assertTrue(dc.dream_run(repo, via=('claude',) if state in ('pending','vetoed') else ('claude','codex'),
                                     runner=runner)['ok'])
        graph = mb._load_queue(repo)[0]
        self.assertEqual(len(candidates), len(graph['nodes']))
        for node in graph['nodes']:
            if node['title']=='open_trap': node['statusKind']='open_trap'
        self.save_graph(repo,graph)
        ids = [n['id'] for n in graph['nodes']]
        if state in ('approved','edge-rejected'):
            self.assertTrue(mb.learn_approve(repo, ids, approver='Fixture reviewer')['ok'])
        if state == 'rejected':
            self.assertTrue(mb.learn_reject(repo, ids, approver='Fixture reviewer')['ok'])
        if state == 'edge-rejected':
            self.assertTrue(mb.learn_reject(repo, [n['edges'][0]['edgeId'] for n in graph['nodes']], approver='Fixture reviewer')['ok'])
        if state == 'vetoed':
            for n in graph['nodes']:
                record = dc._receipt('claude', n, 'f'*24, {'model':'sonnet'})
                n['verification'] = [dc.seal_receipt(dict(record, verdict='MISSING_CONDITION'))]
            self.save_graph(repo,graph)
        return repo, {n['title']:n for n in mb._load_queue(repo)[0]['nodes']}

    def test_kind_state_reader_matrix(self):
        for state in ('approved','hint','pending','rejected','edge-rejected','vetoed'):
            with self.subTest(state=state):
                repo, nodes = self.fixture(state)
                before = {n['id']:(md.node_approval_digest(n), dc.trust_digest(n), copy.deepcopy(n['allPins'])) for n in nodes.values()}
                projection = mb.dream_items(repo, tiers=('approved','hint','pending'))
                actual = {i['id']:i for i in projection['items']}
                approved = set(n['id'] for n in nodes.values()) if state == 'approved' else set()
                recall = mb.unified_recall(repo, 'deploy release make', budget=32000)
                handovers = {mode:mw.build_handover(repo, mode=mode, budget=32000, output_format='markdown')
                             for mode in ('verified','digest')}
                changed = mb.changed_check(repo, files=['deploy.py'])
                pending = mb.learn_pending(repo)
                promotion = mb.promote_approved(repo)
                self.assertTrue(promotion['ok'], promotion)
                page = (repo / mb.PROMOTED_PAGE).read_text() if (repo / mb.PROMOTED_PAGE).exists() else ''
                session = mh._session_start(repo, {}, {'dream':True, 'tokens':4000})
                session_text = (session or {}).get('hookSpecificOutput', {}).get('additionalContext','')
                post = mh._post_tool_use(repo, {'tool_input':{'file_path':str(repo / 'deploy.py')}}, {})
                post_text = (post or {}).get('hookSpecificOutput', {}).get('additionalContext','')
                snap = mb._load_snapshot(repo)[0]
                contexts = ev.build_contexts(repo, {'run':'matrix', 'protocol':'retrospective', 'hints':False,
                    'budget':32000,'dreamUntil':'2026-12-01','dream':[{'file':f,'lines':ls} for f,ls in snap.items()]},
                    {'text':'deploy release make cache'})
                groups = {'dream_items': [i for i in actual.values() if i['tier']=='approved'],
                          'recall JSON':recall['sections']['dream'], 'check --changed':changed['approvedLessons']}
                for mode, hand in handovers.items():
                    groups['handover '+mode] = hand['approvedLessons']
                    self.assertTrue(mw.verify_handover_checksum(hand), mode)
                    self.assertTrue(mw.verify_handover_markdown(hand['formatted']), mode)
                    self.assertEqual(0,hand['executionProof']['unanchoredRulesOmitted'])
                    self.assertFalse(any('make release' in i['rule'] for i in hand['activeConstraints']+hand.get('projectRules', [])))
                for reader, items in groups.items():
                    with self.subTest(reader=reader): self.assertEqual(approved, {i['id'] for i in items})
                texts = {'recall markdown':recall['formatted'], 'promoted page':page, 'PostToolUse':post_text,
                         'check markdown':changed['formatted']}
                texts.update({'handover '+mode:hand['formatted'] for mode, hand in handovers.items()})
                texts.update({arm:contexts[arm]['text'] for arm in ('flat','graph','handover')})
                for kind, node in nodes.items():
                    with self.subTest(kind=kind):
                        expected_tier = ('approved' if state=='approved' else 'hint' if state=='hint' and kind not in ('GOVERNS','SUPERSEDES')
                                         else 'pending')
                        if state=='rejected': self.assertNotIn(node['id'],actual)
                        else: self.assertEqual(expected_tier,actual[node['id']]['tier'])
                        fields = md.lesson_display_fields(node)
                        for reader, text in texts.items():
                            if state=='approved':
                                for quote in fields['quoteLines']:
                                    self.assertIn(quote,text,(kind,reader))
                            elif reader in ('promoted page','flat','graph','handover'):
                                self.assertNotIn(fields['quoteLines'][0],text,(kind,reader))
                        if state=='approved': self.assertNotIn(node['id'], {i['id'] for i in pending['pendingNodes']})
                        if state in ('hint','pending','vetoed'):
                            self.assertIn(node['id'], {i['id'] for i in pending['pendingNodes']})
                        if state=='approved' and kind=='SUPERSEDES':
                            for reader,text in texts.items():
                                if reader in ('recall markdown','handover verified','handover digest','handover'):
                                    self.assertIn('[REPLACED — do not apply as current]',text,reader)
                                    self.assertIn('[CURRENT]',text,reader)
                                    self.assertNotIn('old_pin',text,reader)
                                else:
                                    self.assertIn('replaced — do not apply as current',text,reader)
                                    self.assertIn('current instruction',text,reader)
                                    self.assertIn('SUPERSEDES source',text,reader)
                            self.assertEqual(['replaced','current'],actual[node['id']]['lineRoles'])
                            self.assertTrue(actual[node['id']]['quarantined'])
                        if node['id'] in actual:
                            for cite in actual[node['id']]['cites']:
                                self.assertTrue(cite['verified'])
                                full_line=snap[Path(cite['file']).name][cite['start']-1]
                                self.assertEqual(md.sha256_hex(full_line),cite['sha256'])
                self.assertEqual(approved,set(promotion['promoted']))
                if approved:
                    self.assertIn('%d approved lesson(s)' % len(approved),session_text)
                    self.assertIn('## Replaced instructions (do not apply as current)',page)
                    self.assertIn('<!-- memlet:dream-page ',page)
                for node in nodes.values(): self.assertNotIn(md.lesson_text(node),session_text)
                self.assertEqual([],mb.changed_check(repo,files=['unrelated.py'])['approvedLessons'])
                self.assertFalse(any('make release' in i['quote'] for i in recall['sections']['rules']))
                for node in mb._load_queue(repo)[0]['nodes']:
                    self.assertEqual(before[node['id']],(md.node_approval_digest(node),dc.trust_digest(node),node['allPins']))
                if state=='pending': self.assertIn('verification=none',pending['formatted'])
                if state=='approved':
                    for args in [('recall','deploy release make','--repo',str(repo),'--budget','32000'),
                                 ('handover',str(repo),'--budget','32000'),
                                 ('handover',str(repo),'--mode','digest','--budget','32000'),
                                 ('check',str(repo),'--changed')]:
                        for fmt in ('json','markdown'):
                            result = self.cli(*args,'--format',fmt)
                            self.assertEqual(0,result.returncode,(args,fmt,result.stdout,result.stderr))
                            self.assertIn('scripts/deploy.sh',result.stdout)
                            if fmt=='json': json.loads(result.stdout)

    def test_capsule_kind_priority_and_newest_approval(self):
        repo,nodes=self.fixture('approved',('RESOLVES','BLOCKS','GOVERNS','SUPERSEDES','multi-line'))
        graph=mb._load_queue(repo)[0]
        for node in graph['nodes']:
            node['approval']['at']='2026-01-02T00:00:00Z' if node['title']=='multi-line' else '2026-01-01T00:00:00Z'
        self.save_graph(repo,graph)
        hand=mw.build_handover(repo,budget=2000)
        self.assertEqual([nodes[k]['id'] for k in ('SUPERSEDES','BLOCKS','multi-line','RESOLVES','GOVERNS')],
                         [i['id'] for i in hand['approvedLessons']])
        original=mb.dream_items(repo)['items']
        with mock.patch.object(mb,'dream_items',return_value={'items':list(reversed(original))}):
            self.assertEqual(hand['approvedLessons'],mw.build_handover(repo,budget=2000)['approvedLessons'])

    def test_default_capsule_carries_live_shape_and_tight_priority(self):
        repo,nodes=self.fixture('approved',('RESOLVES','SUPERSEDES','BLOCKS'))
        graph=mb._load_queue(repo)[0]
        hint=next(n for n in graph['nodes'] if n['title']=='BLOCKS')
        hint.pop('approval');hint['verdict']='PENDING'
        self.save_graph(repo,graph)
        original={i['id']:i for i in mb.dream_items(repo,tiers=('approved','hint'))['items']}
        expected=[nodes[k]['id'] for k in ('SUPERSEDES','RESOLVES')]
        for fmt in ('markdown','json'):
            hand=mw.build_handover(repo,output_format=fmt)
            self.assertEqual(expected,[i['id'] for i in hand['approvedLessons']])
            self.assertTrue(mw.verify_handover_checksum(hand))
            markdown=mw._render_handover(hand,mw.handover_signature(hand))
            self.assertTrue(mw.verify_handover_markdown(markdown))
            self.assertLessEqual(hand['tokenEstimate'],500)
            for item in hand['approvedLessons']:
                self.assertIn(item['text'],markdown)
                self.assertNotIn('old_pin',item['text'])
                for key in ('quoteLines','cites','edgeSources'):
                    self.assertEqual(original[item['id']][key],item[key])
            self.assertEqual(1,markdown.count('[REPLACED — do not apply as current]'))
            self.assertEqual(1,markdown.count('[CURRENT]'))
        seen=set()
        for budget in range(500,950,5):
            hand=mw.build_handover(repo,budget=budget,rejected=['Avoid retry '+str(i)+' '+('old approach '*12) for i in range(4)],output_format='markdown')
            ids=[i['id'] for i in hand['approvedLessons']]
            self.assertEqual(expected[:len(ids)],ids)
            self.assertEqual(2-len(ids),hand['omitted']['approvedLessons'])
            if len(ids)<2:self.assertFalse(hand['hints'])
            if hand['omitted']['activeConstraints']:self.assertFalse(ids)
            self.assertTrue(mw.verify_handover_checksum(hand))
            seen.add(len(ids))
        self.assertEqual({0,1,2},seen)
        recall=mb.unified_recall(repo,'deploy',budget=5000)['formatted']
        self.assertEqual(1,recall.count('[REPLACED — do not apply as current]'))
        self.assertEqual(1,recall.count('[CURRENT]'))
        self.assertNotIn('old_pin',recall)

    def test_budget_order_checksum_and_omissions(self):
        repo,nodes=self.fixture('approved',('RESOLVES','BLOCKS'))
        # Return one item to the trusted hint tier by removing its human decision.
        graph=mb._load_queue(repo)[0]
        graph['nodes'][1].pop('approval')
        graph['nodes'][1]['verdict']='PENDING'
        self.save_graph(repo,graph)
        self.assertTrue(mb.promote_approved(repo)['ok'])
        for mode in ('verified','digest'):
            full=mw.build_handover(repo,mode=mode,budget=10000,output_format='markdown')
            self.assertEqual((1,1,1),(len(full['hints']),len(full['approvedLessons']),len(full['activeConstraints'])))
            self.assertLess(full['formatted'].index('### Active constraints'),full['formatted'].index('### Approved lessons'))
            self.assertLess(full['formatted'].index('### Approved lessons'),full['formatted'].index('### '+HINT_LABEL))
            previous=full
            seen=set()
            for budget in range(full['tokenEstimate'],300,-30):
                hand=mw.build_handover(repo,mode=mode,budget=budget,output_format='markdown')
                seen.add((len(hand['hints']),len(hand['approvedLessons']),len(hand['activeConstraints'])))
                if hand['omitted']['approvedLessons']: self.assertFalse(hand['hints'])
                if hand['omitted']['activeConstraints']: self.assertFalse(hand['approvedLessons'])
                for key in ('hints','approvedLessons','activeConstraints'):
                    self.assertEqual(len(full[key]),len(hand[key])+hand['omitted'][key])
                self.assertTrue(mw.verify_handover_checksum(hand))
                self.assertTrue(mw.verify_handover_markdown(hand['formatted']))
                previous=hand
            self.assertIn((0,1,1),seen)
            self.assertIn((0,0,1),seen)
            tampered=copy.deepcopy(full);tampered['approvedLessons'][0]['text']='tampered'
            self.assertFalse(mw.verify_handover_checksum(tampered))
            self.assertFalse(mw.verify_handover_markdown(full['formatted'].replace('warm cache','cold cache')))

    def test_verdict_vocabulary_help_and_selftest_markdown(self):
        from memlet.vocabulary import VERDICT_HELP
        self.assertEqual(set(dc.VERDICTS),set(VERDICT_HELP))
        self.assertTrue(HINT_HELP.endswith('.'))
        repo,nodes=self.fixture('pending',('BLOCKS',))
        graph=mb._load_queue(repo)[0]; node=graph['nodes'][0]
        for verdict,sentence in VERDICT_HELP.items():
            record=dc._receipt('claude',node,'a'*24,{'model':'sonnet'})
            node['verification']=[dc.seal_receipt(dict(record,verdict=verdict))]
            self.save_graph(repo,graph)
            if verdict!='SUPPORTED':
                for text in (mb.learn_pending(repo)['formatted'],json.dumps(mb.dream_items(repo,tiers=('pending',))),
                             json.dumps(dc.dream_status(repo))): self.assertIn(sentence,text)
        for command in ('anchors','eval'):
            self.assertIn('usage: memlet admin '+command,self.cli('admin',command,'--help').stdout)
        help_text=' '.join(self.cli('init','--help').stdout.split())
        self.assertIn('Stop and PreCompact snapshot',help_text)
        result=self.cli('admin','eval','selftest','--format','markdown')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertTrue(result.stdout.startswith('## Memlet eval selftest'),result.stdout[:200])
        self.assertIn('prospective',result.stdout)
        self.assertIn('retrospective',result.stdout)
        self.assertIn('Arm | n | accuracy',result.stdout)

    def test_legacy_cross_node_supersession_uses_the_same_quarantine(self):
        repo,_=self.fixture('pending',('multi-line',))
        snap=mb._load_snapshot(repo)[0];fid=next(iter(snap))
        graph=md.dream_pin_and_validate([
            {'lesson_id':'OLD','category':'repeated_pitfall','scope_tag':'deploy.py','title':'Old deploy rule',
             'src_spans':[fid+':1']},
            {'lesson_id':'NEW','category':'verified_workaround','scope_tag':'deploy.py','title':'Current deploy rule',
             'src_spans':[fid+':2'],'edges':[{'edge_type':'SUPERSEDES','to_id':'OLD',
                'src_span':fid+':2','target_span':fid+':1'}]}],snap)
        self.save_graph(repo,graph)
        self.assertTrue(mb.learn_approve(repo,['OLD','NEW'],approver='Reviewer')['ok'])
        items={i['id']:i for i in mb.dream_items(repo)['items']}
        self.assertTrue(items['OLD']['quarantined'])
        self.assertNotIn('quarantined',items['NEW'])
        self.assertEqual(['replaced','current'],items['OLD']['lineRoles'])
        self.assertNotIn('OLD',{e['dreamId'] for e in mb.promoted_rule_entries(repo)[0]})
        for result in (mb.unified_recall(repo,'deploy',budget=5000),
                       mw.build_handover(repo,budget=2000,output_format='markdown')):
            self.assertIn('[REPLACED — do not apply as current]',result['formatted'])
            self.assertIn('[CURRENT]',result['formatted'])
        # A changed replacement cannot leave its old rule active in the approved group.
        (repo/mb.SNAPSHOT_REL/fid).write_text(snap[fid][0]+'\nchanged replacement\n')
        self.assertEqual([],mb.dream_items(repo)['items'])


class FinalAuditIntegration(Scratch):
    fixture = ApprovedReadersIntegration.fixture
    save_graph = ApprovedReadersIntegration.save_graph
    kinds = ApprovedReadersIntegration.kinds
    prepare = DreamPromptIntegration.prepare
    lines = DreamPromptIntegration.lines

    def test_primary_pin_cross_session_and_per_candidate_ontology(self):
        self.write('AGENTS.md', '# Fixture\n')
        mw.init_project(self.root)
        for name, text in [('b-old.log','2026-01-01T00:00:00Z [user] Deploy with make release.'),
                           ('a-new.log','2026-02-01T00:00:00Z [user] Deploy only with scripts/deploy.sh.')]:
            dc.dream_ingest(self.root, self.write(name, text+'\n'))
        batch = dc.dream_next(self.root)
        files = {f.split('-')[0]: f for f, _, _ in batch['rows']}
        old, new = files['b']+':1', files['a']+':1'
        pair = {'category':'superseded_directive','title':'replace deployment','scope_tag':'deploy',
                'src_spans':[old,new], 'superseded_by_src':new,
                'edges':[{'edge_type':'SUPERSEDES','target':'old deploy','src_span':new,'target_span':old}]}
        good = {'category':'repeated_pitfall','title':'keep checks','scope_tag':'deploy','src_spans':[new],
                'edges':[{'edge_type':'BLOCKS','target':'cold cache','src_span':new}]}
        invalid = dict(good, title='ontology rejection')
        real = md.verify_ontology_contract
        def verify(graph, snapshot):
            if len(graph['nodes']) == 1 and graph['nodes'][0]['title']=='ontology rejection':
                return {'ok':False}
            return real(graph, snapshot)
        with mock.patch.object(md,'verify_ontology_contract',side_effect=verify):
            result=dc.dream_run(self.root, runner=lambda *a,**k:json.dumps([pair,invalid,good]))
        self.assertTrue(result['ok'],result)
        self.assertEqual(1,result['rejectedCandidates'])
        node=next(n for n in mb._load_queue(self.root)[0]['nodes'] if n['category']=='superseded_directive')
        self.assertEqual(old,node['primaryPin']['span'])
        self.assertEqual(node['primaryPin'],node['allPins'][0])
        self.assertTrue(node['timestamp'].startswith('2026-01-01'))
        self.assertEqual(new,node['supersededByPin']['span'])
        self.assertTrue(mb.learn_approve(self.root,[node['id']],approver='reviewer')['ok'])
        item=mb.dream_items(self.root)['items'][0]
        self.assertIn('replaced — do not apply as current',mb.render_approved_lesson(item))
        # Active sources with an inline OLD endpoint must label the old and new pins correctly too.
        source=copy.deepcopy(node)
        source.update(statusKind='active_lesson',category='verified_workaround',verdict='SUPPORTED',
                      primaryPin=node['supersededByPin'],supersededByPin=None)
        source['edges'][0].update(fromId=source['id'],toId=source['id']+':OLD')
        projected=md.supersession_display_node(source,{'nodes':[source],'edges':source['edges']})
        fields=md.lesson_display_fields(projected)
        roles=dict(zip(fields['quoteLines'],fields['lineRoles']))
        self.assertEqual('replaced',roles['Deploy with make release.'])
        self.assertEqual('current',roles['Deploy only with scripts/deploy.sh.'])
        inline_repo,_,fid=self.prepare('inline',lines=['2026-01-01T00:00:00Z [user] Deploy with make release.',
                                                     '2026-02-01T00:00:00Z [user] Deploy only with scripts/deploy.sh.'])
        active={'category':'verified_workaround','title':'current deploy','scope_tag':'deploy', 'src_spans':[fid+':2'],
                'edges':[{'edge_type':'SUPERSEDES','target':'old deploy','src_span':fid+':2','target_span':fid+':1'}]}
        result=dc.dream_run(inline_repo,runner=lambda *a,**kw:json.dumps([active]))
        self.assertTrue(result['ok'],result)
        current=mb._load_queue(inline_repo)[0]['nodes'][0]
        self.assertEqual(fid+':2',current['primaryPin']['span'])
        self.assertTrue(current['timestamp'].startswith('2026-02-01'))
        self.assertTrue(mb.learn_approve(inline_repo,[current['id']],approver='reviewer')['ok'])
        snapshot=mb._load_snapshot(inline_repo)[0]
        contexts=ev.build_contexts(inline_repo,{'run':'inline','protocol':'retrospective','hints':False,'budget':32000,
            'dreamUntil':'2026-12-01','dream':[{'file':f,'lines':ls} for f,ls in snapshot.items()]},{'text':'deploy'})
        texts=[mb.unified_recall(inline_repo,'deploy',budget=4000)['formatted'],
               mw.build_handover(inline_repo,budget=4000,output_format='markdown')['formatted'],
               md.run_dream_recall(inline_repo/mb.QUEUE_REL,snapshot_path=inline_repo/mb.SNAPSHOT_REL,query_text='deploy',budget_tokens=4000)['formatted']]
        texts += [contexts[arm]['text'] for arm in ('flat','graph','handover')]
        for i,text in enumerate(texts):
            if i in (0,1,5):
                self.assertIn('[REPLACED — do not apply as current] Deploy with make release.',text)
                self.assertIn('[CURRENT] Deploy only with scripts/deploy.sh.',text)
            else:
                self.assertIn('replaced — do not apply as current: Deploy with make release.',text)
                self.assertIn('current instruction: Deploy only with scripts/deploy.sh.',text)

    def test_fresh_review_lists_every_line_and_binds_the_displayed_digest(self):
        repo,nodes=self.fixture('approved',('multi-line',))
        node=nodes['multi-line']; path=repo/mb.SNAPSHOT_REL/Path(node['primaryPin']['span'].split(':')[0])
        original=path.read_text().splitlines()
        def change(text):
            path.write_text(original[0]+'\n2026-11-11T10:01:00Z [assistant] '+text+'\n')
        change('Never skip the required deploy safety check.')
        mh._session_start(repo,{}, {'dream':True,'tokens':4000})
        self.assertFalse((repo/mb.DREAM_DIR/md.CITATION_REVIEWS_FILE).exists())
        self.assertFalse(mb.learn_approve(repo,[node['id']],approver='reviewer',refresh_citations=True)['ok'])
        stale=mb.learn_pending(repo)
        row=stale['pendingNodes'][0]
        self.assertEqual([False,True],[c['changed'] for c in row['currentCitations']])
        self.assertIn('[unchanged]',stale['formatted']); self.assertIn('[changed]',stale['formatted'])
        self.assertIn('Never skip the required deploy safety check.',stale['formatted'])
        digest=row['reviewDigest']
        change('Never bypass the updated safety check either.')
        mh._session_start(repo,{}, {'dream':True,'tokens':4000})  # counts cannot silently refresh the review receipt
        refused=mb.learn_approve(repo,[node['id']],approver='reviewer',refresh_citations=True)
        self.assertFalse(refused['ok'],refused)
        self.assertIn('fresh_review_required_or_changed',str(refused))
        fresh=mb.learn_pending(repo)
        self.assertNotEqual(digest,fresh['pendingNodes'][0]['reviewDigest'])
        approved=mb.learn_approve(repo,[node['id']],approver='reviewer',refresh_citations=True)
        self.assertTrue(approved['ok'],approved)
        current=mb.dream_items(repo)['items'][0]
        self.assertIn('updated safety check',current['text'])
        self.assertEqual('approved',current['tier'])

    def test_legacy_keyword_supersession_and_rejected_edge_cortex(self):
        from memlet import memlet_cortex as cortex
        self.write('AGENTS.md','# Fixture\n'); mw.init_project(self.root)
        transcript=self.write('legacy.log','2026-01-01T00:00:00Z [user] The deploy instruction using make release is superseded.\n'
                              '2026-01-02T00:00:00Z [user] The deploy instruction is replaced by scripts/deploy.sh.\n')
        learned=mb.learn_from_transcript(self.root,transcript)
        ids=learned['addedNodeIds']; self.assertTrue(ids,learned)
        self.assertTrue(mb.learn_approve(self.root,ids,approver='legacy reviewer')['ok'])
        items=mb.dream_items(self.root)['items']
        self.assertTrue(any(i.get('quarantined') for i in items),items)
        self.assertEqual(len(ids),dc.dream_status(self.root)['counts']['approved'])
        self.assertFalse(mb.learn_pending(self.root)['pendingNodes'])
        self.assertIn('Superseded Quarantine',mb.unified_recall(self.root,'deploy release',budget=4000)['formatted'])
        lineage=cortex._load_dream_lineage({'path':str(self.root)})
        self.assertTrue(all(n['reviewState']=='approved' for n in lineage['nodes']))
        repo,nodes=self.fixture('edge-rejected',('BLOCKS',))
        item=next(iter(nodes.values()))
        pending=mb.learn_pending(repo)
        self.assertEqual(item['id'],pending['pendingNodes'][0]['id'])
        self.assertIn('rejected edge',pending['formatted'])
        lineage=cortex._load_dream_lineage({'path':str(repo)})['nodes'][0]
        self.assertEqual(('pending','PENDING',False),(lineage['reviewState'],lineage['verdict'],lineage['inRecallCapsule']))
        admin=md.run_dream_recall(repo/mb.QUEUE_REL,snapshot_path=repo/mb.SNAPSHOT_REL,query_text='deploy',budget_tokens=4000)
        self.assertNotIn(item['id'],admin['includedNodeIds'])

    def test_recall_hints_only_use_leftover_budget_and_handover_caps_are_separate(self):
        repo,nodes=self.fixture('approved')
        hint_repo,hint_nodes=self.fixture('hint',('single-line',))
        hint=mb.dream_items(hint_repo,tiers=('hint',))['items'][0]
        approved=mb.dream_items(repo)['items']
        original=mb.dream_items
        def provider(path,*args,**kwargs):
            result=original(path,*args,**kwargs)
            if 'hint' in kwargs.get('tiers',()): result['items'].append(hint)
            return result
        baseline={b:mb.unified_recall(repo,'deploy',budget=b) for b in (400,600,2000)}
        with mock.patch.object(mb,'dream_items',side_effect=provider):
            for budget in baseline:
                actual=mb.unified_recall(repo,'deploy',budget=budget)
                self.assertEqual([i['id'] for i in baseline[budget]['sections']['dream']],
                                 [i['id'] for i in actual['sections']['dream']],budget)
        for mode in ('verified','digest'):
            lessons=[dict(approved[-1],id='A%d'%n) for n in range(55)]+[dict(hint,id='H%d'%n) for n in range(53)]
            cap=mw.build_handover(repo,budget=32000,mode=mode,output_format='markdown',_lesson_items=lessons)
            limit=mw._HANDOVER_LIST_CAP
            self.assertEqual({'count':55-limit,'cap':limit},cap['listCapOmitted']['approvedLessons'])
            self.assertEqual({'count':53-limit,'cap':limit},cap['listCapOmitted']['hints'])
            self.assertEqual(55,len(cap['approvedLessons'])+cap['omitted']['approvedLessons']+55-limit)
            self.assertEqual(53,len(cap['hints'])+cap['omitted']['hints']+53-limit)
            self.assertIn('over the %d-item cap' % limit,cap['formatted'])
            self.assertTrue(mw.verify_handover_checksum(cap))
            tampered=copy.deepcopy(cap); tampered['listCapOmitted']['hints']['count']+=1
            self.assertFalse(mw.verify_handover_checksum(tampered))

    def test_failed_batches_continue_stop_after_three_retry_and_explicit_skip(self):
        repo,_,_=self.prepare(lines=['2026-01-01T00:00:00Z [user] Never drop cache checks '+('words '*50)]*6)
        real_plan=dc._plan
        # Use the library planner with a small bound to exercise multiple batches cheaply.
        with mock.patch.object(dc,'_plan',side_effect=lambda snap,starts,tokens:real_plan(snap,starts,128)):
            calls=[]
            def runner(*a,**kw):
                calls.append(kw['stdin_text'])
                return 'bad json' if len(calls)==1 else '[]'
            result=dc.dream_run(repo,runner=runner,max_batches=4)
            self.assertEqual(4,result['calls'],result)
            self.assertEqual(1,result['failedBatches'])
            status=dc.dream_status(repo); failed=status['runs'][0]['failedBatchIds'][0]
            self.assertIn(failed,dc.dream_run(repo,dry_run=True)['retryFailedBatches'])
            seen=[]
            again=dc.dream_run(repo,runner=lambda *a,**k:seen.append(k['stdin_text']) or '[]',max_batches=1)
            self.assertTrue(again['ok'],again); self.assertEqual(calls[0],seen[0])
        other,_,_=self.prepare('all-fail',lines=['2026-01-01T00:00:00Z [user] Never drop cache checks '+('words '*50)]*6)
        with mock.patch.object(dc,'_plan',side_effect=lambda snap,starts,tokens:real_plan(snap,starts,128)):
            bad=dc.dream_run(other,runner=lambda *a,**k:'bad',max_batches=6)
        self.assertEqual(3,bad['calls']); self.assertTrue(bad['consecutiveFailureStopped'])
        failed=dc.dream_status(other)['runs'][0]['failedBatchIds'][0]
        with mock.patch.object(vc,'run_vendor',side_effect=AssertionError('no model')) as no_model:
            skip=dc.dream_skip_failed(other,failed)
            self.assertTrue(skip['ok'],skip); no_model.assert_not_called()
        entry=dc.dream_status(other)['runs'][0]['skippedBatches'][0]
        self.assertEqual('completed-with-rejections',entry['status']); self.assertTrue(md._valid_utc_stamp(entry['at']))
        self.assertIn('completed-with-rejections',__import__('memlet.cli_dream',fromlist=['_markdown'])._markdown(dc.dream_status(other)))

    def test_symbol_only_anchor_changed_consumers_and_pattern_bindings(self):
        self.write('auth.py',CODE); self.write('AGENTS.md','# Fixture\n'); mw.init_project(self.root)
        self.write('.memlet/wiki/auth.md','# Auth\n- Always use `validate_token()` before accepting a token.\n')
        suggestion=ma.suggest_anchors(self.root)['suggestions'][0]
        self.assertEqual('single-definition-found',suggestion['confidence'])
        self.assertTrue(ma.accept_suggestions(self.root,[suggestion['id']])['ok'])
        subprocess.run(['git','add','.'],cwd=self.root,check=True,capture_output=True)
        subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                        '-c','core.hooksPath='+str(self.base/'no-hooks'),'commit','-qm','baseline'],cwd=self.root,check=True,capture_output=True)
        self.write('auth.py',CODE.replace('(token)','(token, issuer)',1))
        cli=self.cli('check','--changed','--format','json')
        self.assertEqual(1,cli.returncode,cli.stderr); self.assertIn('signature_drift',cli.stdout)
        self.assertIn('[suggested anchor]',self.hook('post-tool-use',tool_name='Edit',tool_input={'file_path':str(self.root/'auth.py')}))
        self.assertIn('signature_drift',self.rpc('memlet_check',files=['auth.py'])['content'][0]['text'])
        base='class C:\n    def foo(self):\n        return 1\n'
        cases=[('foo = lambda self: 999\n',True),('for foo in []:\n    pass\n',True),('import x as foo\n',True),
               ('def other():\n    foo = 1\n',False),('def unrelated():\n    return 2\n',False),
               ('class D:\n    def foo(self):\n        return 2\n',False)]
        if sys.version_info >= (3,10):
            cases += [('match 1:\n    case foo:\n        pass\n',True),
                      ('match []:\n    case [*foo]:\n        pass\n',True),
                      ('match {}:\n    case {**foo}:\n        pass\n',True)]
        self.write('method.py',base)
        self.write('AGENTS.md','# Rules\n- Always use `method.py::foo`.\n')
        mw.init_project(self.root)
        for suffix,rebound in cases:
            path=self.write('method.py',base+suffix)
            self.assertEqual(rebound,ca.python_symbol_rebound(path,'foo'),suffix)
            anchor=next(a for a in audit.audit_repository(self.root)['pinnedAnchors']['anchors'] if a['symbol']=='foo')
            if rebound:
                self.assertEqual(('compatible-unverified','symbol_rebound_in_module'),(anchor['status'],anchor['reason']),suffix)
            elif 'class D:' not in suffix:
                self.assertEqual('active',anchor['status'],suffix)
        # Multiple class methods retain the existing composite short-symbol hash.
        self.write('method.py',base+'class D:\n    def foo(self):\n        return 2\n')
        hashes=ca.extract_file_symbol_hashes(self.root/'method.py')
        self.assertEqual(ca._digest({name:hashes[name] for name in ('C.foo','D.foo')}),hashes['foo'])

    def test_all_hooks_are_model_free_and_span_numbers_are_ascii(self):
        self.write('AGENTS.md','# Fixture\n'); mw.init_project(self.root)
        source=self.write('session.log','2026-01-01T00:00:00Z [user] Never bypass cache checks.\n')
        with mock.patch.object(vc,'run_vendor',side_effect=AssertionError('hook launched a model')) as vendor, \
             mock.patch.object(mh,'find_root',return_value=self.root), \
             mock.patch.object(mh,'_dispatch',wraps=mh._dispatch) as dispatched:
            for event in mh.RUNTIME_EVENTS+mh.GIT_EVENTS:
                out=io.StringIO()
                self.assertEqual(0,mh.run_hook([event],stdin=io.StringIO(json.dumps({'cwd':str(self.root),
                    'transcript_path':str(source),'tool_name':'Edit','tool_input':{'file_path':str(self.root/'auth.py')}})),
                    stdout=out,stderr=io.StringIO(),hard_exit=False),event)
            for event in ('stop','pre-compact'):
                mh._learn(self.root,{'transcript_path':str(source)},{'dream':True},event)
            mh._session_start(self.root,{}, {'dream':True,'tokens':4000})
            self.assertEqual(list(mh.RUNTIME_EVENTS+mh.GIT_EVENTS),[call.args[0] for call in dispatched.call_args_list])
            vendor.assert_not_called()
        for span in ('x.log:١','x.log:１','x.log:1-٢','x.log:1-２'):
            self.assertIsNone(md.parse_span(span),span)
        self.assertEqual(('x.log',1,2),md.parse_span('x.log:1-2'))

    def test_eval_one_queue_snapshot_fixed_stdin_and_input_errors(self):
        repo,nodes=self.fixture('approved',('multi-line',))
        snap=mb._load_snapshot(repo)[0]
        manifest={'run':'frozen','protocol':'retrospective','hints':False,'budget':32000,'dreamUntil':'2026-12-01',
                  'dream':[{'file':f,'lines':ls} for f,ls in snap.items()]}
        real_read=mw.read_managed_text; reads=[]
        def read(root,path,*args,**kw):
            result=real_read(root,path,*args,**kw)
            if str(root)==str(repo) and str(path)==mb.QUEUE_REL: reads.append(result)
            return result
        with mock.patch.object(mw,'read_managed_text',side_effect=read):
            contexts=ev.build_contexts(repo,manifest,{'text':'deploy'})
        self.assertEqual(1,len(reads))
        quote=md.lesson_text(nodes['multi-line']).split(LESSON_SEPARATOR)[0]
        for arm in ('flat','graph','handover'): self.assertIn(quote,contexts[arm]['text'])
        self.write('sessions/a.json',json.dumps([{'timestamp':'2026-01-01','text':'Repair uses cedar.'}]))
        self.write('sessions/b.json',json.dumps([{'timestamp':'2026-01-03','text':'Repair cedar succeeded.'}]))
        ev.init_eval(self.root,run='r',sessions='sessions',dream_until='2026-01-02',budget=30000)
        question={'id':'q','text':'What repaired the cache?','citation':{'file':'sessions/b.json','start':1,'end':1}}
        self.write('questions.json',json.dumps([question])); ev.seal_questions(self.root,run='r',source='questions.json')
        evidence=('cedar '*15000).strip()
        def builder(root,m,q):
            return {a:{'text':evidence if a!='none' else '', 'budget':m['budget'],'omittedItems':[]} for a in ev.ARMS}
        preview=ev.run_eval(self.root,run='r',via='claude',arms=['flat'],dry_run=True,context_builder=builder)
        self.assertEqual(ev.ANSWER_PREFIX,preview['launches'][0]['argv'][-1])
        self.assertGreater(preview['launches'][0]['stdinBytes'],65536)
        seen=[]
        result=ev.run_eval(self.root,run='r',via='claude',arms=['flat'],context_builder=builder,
                           runner=lambda v,p,**kw:seen.append((p,kw['stdin_text'])) or 'cedar')
        self.assertTrue(result['ok']); self.assertEqual(ev.ANSWER_PREFIX,seen[0][0])
        self.assertEqual(evidence,json.loads(seen[0][1])['evidence'])
        self.assertEqual(ev.digest({'prompt':seen[0][0],'stdin':seen[0][1]}),ev._state(self.root,'r')['calls'][0]['promptDigest'])
        with mock.patch.object(vc,'static_vendor_refusal',return_value=None):
            judge=ev.score_eval(self.root,run='r',judge='codex',dry_run=True)
        self.assertEqual(ev.JUDGE_PROMPT,judge['prompts'][0]); self.assertIn('reference',json.loads(judge['stdinInputs'][0]))
        self.assertLessEqual(len(json.dumps(ev._clip('x'+' '*1100000,32000)).encode()),ev.EVAL_CONTEXT_WIRE_LIMIT)
        ev.init_eval(self.root,run='validation',sessions='sessions',dream_until='2026-01-02')
        for field in ('timestamp','text'):
            self.write('malformed/a.json',json.dumps([{k:v for k,v in {'timestamp':'2026-01-01','text':'cedar'}.items() if k!=field}]))
            with self.assertRaisesRegex(ValueError,'record 1 requires field '+field):
                ev.init_eval(self.root,run='invalid',sessions='malformed',dream_until='2026-01-02')
        for field in ('id','text','citation'):
            for value in ('missing',None,True,[]):
                bad=dict(question)
                if value=='missing': bad.pop(field)
                else: bad[field]=value
                self.write('bad.json',json.dumps([bad]))
                cli=self.cli('admin','eval','questions','--run','validation','--from','bad.json','--format','markdown')
                self.assertEqual(2,cli.returncode,cli.stderr)
                self.assertIn('record 1',cli.stdout); self.assertIn(field,cli.stdout)
                self.assertTrue(cli.stdout.startswith('- ERROR:'),cli.stdout)

    def test_eval_markdown_every_action_platform_refusal_and_help(self):
        from argparse import Namespace
        from memlet import cli_eval
        actions={'init':('init_eval',{'dream_until':'2026-01-01','sessions':'sessions','budget':1200,'hints':False,'allow_post_split_memory':False}),
                 'questions':('seal_questions',{'source':'q','via':None,'count':1,'max_calls':0,'dry_run':False}),
                 'run':('run_eval',{'via':'claude','arms':None,'max_calls':0,'dry_run':False}),
                 'score':('score_eval',{'judge':None,'max_calls':0,'dry_run':False}),
                 'report':('report_eval',{}),'resolve':('resolve_call',{'resolve':'q','as_status':'failed','by':'tester','reason':'known'}),
                 'selftest':('eval_selftest',{})}
        for action,(function,options) in actions.items():
            args=Namespace(eval_action=action,repo=str(self.root),run='r',format='markdown',**options)
            for fail in (False,True):
                with mock.patch.object(ev,function,side_effect=ValueError('clear error') if fail else None,
                                       return_value={'ok':True,'calls':0}):
                    result=cli_eval.dispatch(args)
                self.assertIn('_cli_text',result,(action,fail)); self.assertFalse(result['_cli_text'].startswith('{'))
                if fail: self.assertIn('ERROR:',result['_cli_text'])
        with mock.patch.object(vc,'_posix_runner_available',return_value=False), \
             mock.patch.object(subprocess,'Popen') as popen, mock.patch.object(subprocess,'run') as run:
            self.assertEqual(vc.PLATFORM_REFUSAL,vc.check_vendor('claude')['reason'])
            self.assertEqual(vc.PLATFORM_REFUSAL,vc.run_vendor('claude','prompt')['error'])
            with self.assertRaisesRegex(ValueError,'POSIX'): vc._capture(['fake'],b'',1,10,str(self.root))
            popen.assert_not_called(); run.assert_not_called()
        top=self.cli('--help').stdout; admin=self.cli('admin','--help').stdout
        self.assertNotIn('memlet dream:',top+admin); self.assertIn('Dream:\n  dream',admin)
        self.assertNotIn('More:\n  dream',admin)


if __name__ == '__main__':
    unittest.main()
