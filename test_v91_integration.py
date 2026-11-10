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
            for command in ('memlet dream:', 'memlet admin anchors:', 'memlet admin eval:'):
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
            self.assertIn('RESOLVES source',text)
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
            self.assertEqual([fid + ':1', fid + ':2'], [p['span'] for p in graph['nodes'][0]['allPins']])
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


if __name__ == '__main__':
    unittest.main()
