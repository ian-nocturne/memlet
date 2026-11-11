"""9.2 paths, advisory decisions and a replayable model-answer corpus; no vendors."""
import copy
import json
import os
import sqlite3
from pathlib import Path
from unittest import mock

from test_v911_bridge import Fixture
from memlet import memlet_bridge as mb, memlet_dream as md, memlet_dreamcycle as dc
from memlet import memlet_wiki as mw, handover_bridge as hb

CORPUS = Path(__file__).parent / 'memlet/dream_fixtures'


class Lessons(Fixture):
    def test_path_approval_readers_tamper_and_inactive(self):
        repo, nodes = self.fixture('pending', ('single-line',))
        nid = nodes['single-line']['id']
        self.assertTrue(mb.learn_approve(repo, [nid], approver='Reviewer', paths=['anchor.py','deploy.py'])['ok'])
        node = mb._load_queue(repo)[0]['nodes'][0]
        self.assertEqual(['anchor.py','deploy.py'], mb.lesson_paths(node, repo))
        self.assertEqual(node['approvedPaths'], node['approval']['paths'])
        self.assertEqual(['anchor.py','deploy.py'], mb.dream_items(repo)['items'][0]['paths'])
        self.assertTrue(mb.dream_items(repo,files=['anchor.py'])['items'])
        self.assertTrue(mb.unified_recall(repo, files=['anchor.py'])['sections']['dream'])
        self.assertTrue(mb.changed_check(repo,files=['anchor.py'])['approvedLessons'])
        hand,caps,_ = self.bridge(repo)
        self.assertEqual(['anchor.py','deploy.py'],caps['capsules'][0]['paths'])
        self.assertIn('paths anchor.py, deploy.py',(repo/mb.PROMOTED_PAGE).read_text())
        (repo/'anchor.py').rename(repo/'renamed.py')
        got=mb.dream_items(repo)
        self.assertEqual(['deploy.py'],got['items'][0]['paths'])
        self.assertIn('inactive approval paths: anchor.py',' '.join(got['notices']))
        self.assertEqual('approved',got['items'][0]['tier'])
        (repo/'anchor.py').write_text('replacement content')
        self.assertEqual(['anchor.py','deploy.py'],mb.lesson_paths(node,repo))
        graph=mb._load_queue(repo)[0];graph['nodes'][0]['approvedPaths']=['renamed.py'];self.save_graph(repo,graph)
        self.assertEqual([],mb.dream_items(repo)['items'])
        self.assertEqual([],mb.lesson_paths(graph['nodes'][0],repo))

    def test_rebuild_keeps_human_attachments(self):
        repo,nodes=self.fixture('approved',('single-line',));nid=nodes['single-line']['id']
        self.assertTrue(mb.learn_approve(repo,[nid],approver='Reviewer',reconsider=True,paths=['anchor.py'])['ok'])
        graph=mb._load_queue(repo)[0];built=copy.deepcopy(graph)
        for node in built['nodes']:
            node.pop('approval',None);node.pop('approvedPaths',None)
        snapshot,_=mb._load_snapshot(repo)
        merged,_=mb._merge(graph,built,snapshot,repo/mb.SNAPSHOT_REL)
        self.assertEqual(graph['nodes'],merged['nodes'])

    def test_invalid_attachments_and_reapproval_cli(self):
        repo,nodes=self.fixture('pending',('single-line',));nid=nodes['single-line']['id']
        (repo/'link.py').symlink_to(repo/'anchor.py');(repo/'dir').mkdir();(repo/'.env').write_text('secret')
        for path in ['Anchor.py','./anchor.py','../anchor.py',str(repo/'anchor.py'),'link.py','dir','*.py','.env','.git/config','missing.py','dir/../anchor.py','anchor.py/']:
            with self.subTest(path=path):
                before=(repo/mb.QUEUE_REL).read_bytes()
                self.assertFalse(mb.learn_approve(repo,[nid],approver='Reviewer',paths=[path])['ok'])
                self.assertEqual(before,(repo/mb.QUEUE_REL).read_bytes())
        self.assertFalse(mb.learn_approve(repo,[nid],approver='Reviewer',paths=['anchor.py']*9)['ok'])
        self.assertTrue(mb.learn_approve(repo,[nid],approver='Reviewer',paths=['anchor.py'])['ok'])
        self.assertFalse(mb.learn_approve(repo,[nid],approver='Reviewer',paths=['deploy.py'])['ok'])
        result=self.cli('learn','--paths',nid,'--by','Reviewer','--path','deploy.py','--repo',str(repo))
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(['deploy.py'],mb.lesson_paths(mb._load_queue(repo)[0]['nodes'][0],repo))

    def test_pending_hint_and_legacy_bytes(self):
        repo,nodes=self.fixture('approved',('single-line',));nid=nodes['single-line']['id']
        before=mb.unified_recall(repo)['formatted'];hand=mw.build_handover(repo,budget=2000,output_format='markdown')['formatted']
        self.assertTrue(mb.learn_approve(repo,[nid],approver='Reviewer',reconsider=True,paths=[])['ok'])
        self.assertEqual(before,mb.unified_recall(repo)['formatted'])
        self.assertEqual(hand,mw.build_handover(repo,budget=2000,output_format='markdown')['formatted'])
        graph=mb._load_queue(repo)[0];graph['nodes'][0]['approvedPaths']=['anchor.py'];self.save_graph(repo,graph)
        self.assertEqual([],mb.dream_items(repo,tiers=('pending','hint'))['items'][0].get('paths',[]))
        pending=mb.learn_pending(repo)
        self.assertEqual(['anchor.py'],pending['pendingNodes'][0]['approvalPaths'])
        self.assertIn('Approval paths: anchor.py',pending['formatted'])

    def test_no_empty_path_fields_in_legacy_reader_results(self):
        repo,nodes=self.fixture('pending',('single-line',))
        graph=mb._load_queue(repo)[0]
        for node in graph['nodes']:node['scopeTag']='general deployment'
        self.save_graph(repo,graph)
        ids=[n['id'] for n in graph['nodes']]
        self.assertTrue(mb.learn_approve(repo,ids,approver='Reviewer')['ok'])
        item=mb.dream_items(repo)['items'][0]
        self.assertNotIn('paths',item)
        self.assertNotIn('approvalPaths',item)
        recall=mb.unified_recall(repo)
        self.assertNotIn('paths',recall['sections']['dream'][0])
        self.assertNotIn('approvalPaths',recall['sections']['dream'][0])

    def test_pending_supersession_short_markers(self):
        repo,_=self.fixture('pending',('SUPERSEDES',))
        text=mb.learn_pending(repo)['formatted']
        self.assertIn('[REPLACED — do not apply as current]',text)
        self.assertIn('[CURRENT]',text)
        self.assertNotIn('old_pin',text)

    def test_decision_events_advisory_order_and_failure(self):
        repo,nodes=self.fixture('pending',('single-line',));nid=nodes['single-line']['id']
        self.assertEqual([],mb.decision_events(repo))
        self.assertTrue(mb.learn_approve(repo,[nid],approver='Reviewer',paths=['anchor.py'])['ok'])
        events=mb.decision_events(repo);node=mb._load_queue(repo)[0]['nodes'][0]
        self.assertEqual(2,len(events));self.assertEqual(node['approval']['at'],events[0]['at'])
        self.assertEqual(['anchor.py'],events[0]['paths'])
        self.assertTrue(mb.learn_approve(repo,[nid],approver='Reviewer',reconsider=True,paths=['deploy.py'])['ok'])
        self.assertIn('paths-changed',[e['decision'] for e in mb.decision_events(repo)])
        self.assertTrue(mb.learn_reject(repo,[nid],approver='Reviewer')['ok'])
        self.assertTrue(mb.learn_approve(repo,[nid],approver='Reviewer',reconsider=True)['ok'])
        events=mb.decision_events(repo);self.assertEqual(sorted(e['seq'] for e in events),[e['seq'] for e in events])
        before=mb.dream_items(repo)
        conn=sqlite3.connect(str(repo/'.memlet/ledger.sqlite3'));conn.execute('DELETE FROM dream_decision_events');conn.commit();conn.close()
        self.assertEqual(before,mb.dream_items(repo))
        with mock.patch.object(mb,'_decision_event_write',side_effect=sqlite3.OperationalError('fixture failure')):
            result=mb.learn_reject(repo,[nid],approver='Reviewer')
        self.assertFalse(result['ok']);self.assertIn('missing dream_decision_events',result['error'])
        self.assertEqual([],mb.dream_items(repo)['items'])

    def test_multiselect_paths_literal_order_and_edge_tier_gate(self):
        repo,nodes=self.fixture('pending',('single-line','BLOCKS'))
        ids=[n['id'] for n in nodes.values()]
        result=mb.learn_approve(repo,ids,approver='Reviewer',paths=['anchor.py'])
        self.assertTrue(result['ok'],result)
        self.assertIn('same paths apply to each',result['formatted'])
        self.assertTrue(all(i['paths']==['anchor.py','deploy.py'] for i in mb.dream_items(repo)['items']))
        graph=mb._load_queue(repo)[0];node=graph['nodes'][0]
        self.assertTrue(mb.learn_reject(repo,[node['edges'][0]['edgeId']],approver='Reviewer')['ok'])
        node=mb._load_queue(repo)[0]['nodes'][0]
        self.assertEqual([],mb.lesson_paths(node,repo))
        self.assertEqual([],next(i for i in mb.dream_items(repo,tiers=('pending',))['items'] if i['id']==node['id']).get('paths',[]))
        (repo/'nested').mkdir();(repo/'nested/.git').mkdir();(repo/'nested/code.py').write_text('code')
        (repo/'dirlink').symlink_to(repo/'nested',target_is_directory=True)
        self.assertFalse(mb.valid_lesson_path(repo,'nested/code.py'))
        self.assertFalse(mb.valid_lesson_path(repo,'dirlink/code.py'))

    def test_all_history_kinds_timestamps_and_forged_log(self):
        repo,nodes=self.fixture('pending',('single-line',));nid=nodes['single-line']['id']
        def decide(**kw):
            result=mb.learn_approve(repo,[nid],approver='Reviewer',**kw)
            self.assertTrue(result['ok'],result)
            node=mb._load_queue(repo)[0]['nodes'][0]
            self.assertEqual(node['approval']['at'],mb.decision_events(repo,nid)[-1]['at'])
        decide()
        decide(reconsider=True)
        decide(reconsider=True,paths=['anchor.py'])
        node=mb._load_queue(repo)[0]['nodes'][0]
        file=md.parse_span(node['primaryPin']['span'])[0]
        snapshot=repo/mb.SNAPSHOT_REL/file
        snapshot.write_text(snapshot.read_text().replace('requires','needs'))
        mb.learn_pending(repo)
        decide(refresh_citations=True)
        self.assertTrue(mb.learn_reject(repo,[nid],approver='Reviewer')['ok'])
        decide(reconsider=True)
        self.assertTrue(mb.learn_reject(repo,[nid],approver='Reviewer')['ok'])
        self.assertTrue(mb.learn_reject(repo,[nid],approver='Reviewer')['ok'])
        kinds=[e['decision'] for e in mb.decision_events(repo,nid)]
        self.assertEqual(['approved','reconsidered','paths-changed','citations-refreshed','revoked','reconsidered','revoked'],kinds)
        # A first rejection is distinct from revocation of a prior approval.
        other,_=self.fixture('other',('single-line',));other_id=mb._load_queue(other)[0]['nodes'][0]['id']
        self.assertTrue(mb.learn_reject(other,[other_id],approver='Reviewer')['ok'])
        self.assertEqual('rejected',mb.decision_events(other,other_id)[0]['decision'])
        conn=sqlite3.connect(str(repo/'.memlet/ledger.sqlite3'))
        conn.execute("INSERT INTO dream_decision_events (at,item_id,kind,decision,by,digest,paths) VALUES (?,?,?,?,?,?,?)",('2026-11-11T00:00:00Z',nid,'node','approved','Forged','forged','[]'))
        conn.commit();conn.close()
        self.assertEqual([],mb.dream_items(repo)['items'])

    def test_shared_path_context_tracking_hidden_and_whole_tokens(self):
        repo,nodes=self.fixture('pending',('single-line','BLOCKS'))
        (repo/'scratch.py').write_text('untracked')
        ids=[n['id'] for n in nodes.values()]
        self.assertTrue(mb.learn_approve(repo,ids,approver='Reviewer',paths=['scratch.py'])['ok'])
        graph=mb._load_queue(repo)[0]
        original=mb.subprocess.Popen
        with mock.patch.object(mb.subprocess,'Popen',wraps=original) as run, mock.patch.object(mb,'_load_snapshot',wraps=mb._load_snapshot) as snap:
            context=mb.lesson_path_context(repo)
            for node in graph['nodes']:
                for _ in range(10):
                    self.assertEqual(['scratch.py'],mb.lesson_paths(node,repo,context=context))
                    self.assertEqual([],mb.literal_lesson_paths(repo,'scratch.py','',context=context))
                    self.assertEqual(['deploy.py'],mb.literal_lesson_paths(repo,'','Use `deploy.py`.',context=context))
                    self.assertEqual([],mb.literal_lesson_paths(repo,'','Use prefixdeploy.py or deploy.py.bak.',context=context))
                    self.assertEqual([],mb.literal_lesson_paths(repo,'','bad\u200bdeploy.py',context=context))
            self.assertEqual(1,snap.call_count)
            self.assertEqual(1,sum(c.args[0][:2]==['git','ls-files'] for c in run.call_args_list))
        for path in ['bad\u200b.py','bad\n.py','Deploy.py']:
            if path!='Deploy.py':(repo/path).write_text('hidden')
            self.assertFalse(mb.valid_lesson_path(repo,path))
        elsewhere=self.base/'nongit';elsewhere.mkdir();(elsewhere/'plain.py').write_text('regular')
        context=mb.lesson_path_context(elsewhere)
        self.assertFalse(context['is_git'])
        self.assertEqual(['plain.py'],mb.literal_lesson_paths(elsewhere,'plain.py','',context=context))

    def test_literal_attachments_use_raw_cited_lines(self):
        for i,hidden in enumerate(('','\u200b','\U000e0061','\x01','\t')):
            with self.subTest(hidden=repr(hidden)):
                repo,_=self.fixture('raw'+str(i),('single-line',))
                graph=mb._load_queue(repo)[0]
                node=graph['nodes'][0];node['scopeTag']='general deployment'
                file,line,_=md.parse_span(node['primaryPin']['span'])
                snapshot_path=repo/mb.SNAPSHOT_REL/file
                lines=snapshot_path.read_text().split('\n')
                lines[line-1]+=' Use `anchor.py'+hidden+'`.'
                snapshot_path.write_text('\n'.join(lines))
                snapshot,_=mb._load_snapshot(repo)
                for n in graph['nodes']:
                    for key in ('primaryPin','supersededByPin'):
                        if isinstance(n.get(key),dict):n[key]=md.resolve_pin(snapshot,n[key]['span'])
                    n['allPins']=[md.resolve_pin(snapshot,p['span']) for p in n['allPins']]
                for edge in graph['edges']+[e for n in graph['nodes'] for e in n['edges']]:
                    for key in ('srcPin','targetPin','citedTargetPin'):
                        if isinstance(edge.get(key),dict):edge[key]=md.resolve_pin(snapshot,edge[key]['span'])
                self.save_graph(repo,graph)
                result=mb.learn_approve(repo,[node['id']],approver='Reviewer')
                self.assertTrue(result['ok'],result)
                item=mb.dream_items(repo)['items'][0]
                self.assertEqual([] if hidden else ['anchor.py'],item.get('paths',[]))
                self.assertEqual(not bool(hidden),bool(mb.dream_items(repo,files=['anchor.py'])['items']))
                self.assertEqual(not bool(hidden),bool(mb.unified_recall(repo,files=['anchor.py'])['sections']['dream']))
                self.assertEqual(not bool(hidden),bool(mb.changed_check(repo,files=['anchor.py'])['approvedLessons']))
                _,caps,_=self.bridge(repo)
                self.assertTrue(caps['capsules'])
                self.assertEqual([] if hidden else ['anchor.py'],caps['capsules'][0]['paths'])

    def test_only_approved_edges_add_literal_path_evidence(self):
        repo,_=self.fixture('pending',('single-line',))
        graph=mb._load_queue(repo)[0];node=graph['nodes'][0];node['scopeTag']='general deployment'
        self.save_graph(repo,graph)
        self.assertTrue(mb.learn_approve(repo,[node['id']],approver='Reviewer')['ok'])
        graph=mb._load_queue(repo)[0];node=graph['nodes'][0]
        file=md.parse_span(node['primaryPin']['span'])[0]
        path=repo/mb.SNAPSHOT_REL/file
        lines=path.read_text().splitlines()+['2026-11-11T10:30:00Z [user] Use `anchor.py`.']
        path.write_text('\n'.join(lines)+'\n')
        snapshot,_=mb._load_snapshot(repo)
        extra=copy.deepcopy(node['edges'][0]);extra.pop('approval',None)
        extra.update(edgeId=extra['edgeId']+'-extra',srcPin=md.resolve_pin(snapshot,file+':'+str(len(lines))),
                     srcTimestamp='2026-11-11T10:30:00Z',edgeVerdict=md.PENDING)
        node['edges'].append(extra);graph['edges'].append(copy.deepcopy(extra));self.save_graph(repo,graph)
        item=mb.dream_items(repo)['items'][0]
        self.assertEqual('approved',item['tier']);self.assertEqual([],item.get('paths',[]))
        self.assertTrue(mb.learn_approve(repo,[extra['edgeId']],approver='Reviewer')['ok'])
        self.assertEqual(['anchor.py'],mb.dream_items(repo)['items'][0]['paths'])

    def test_explicit_and_cascaded_decisions_log_each_item_once(self):
        repo,nodes=self.fixture('approved',('single-line',))
        node=nodes['single-line'];edge_id=node['edges'][0]['edgeId']
        before=len(mb.decision_events(repo))
        result=mb.learn_reject(repo,[edge_id,node['id']],approver='Reviewer')
        self.assertTrue(result['ok'],result)
        events=mb.decision_events(repo)[before:]
        self.assertEqual(2,len(events))
        self.assertEqual({(edge_id,'edge'),(node['id'],'node')},{(e['item_id'],e['kind']) for e in events})
        self.assertEqual({'revoked'},{e['decision'] for e in events})


class Corpus(Fixture):
    def setup_input(self, fixture, name):
        repo=self.root/name;repo.mkdir();(repo/'AGENTS.md').write_text('# Fixture\n')
        self.assertTrue(mw.init_project(repo)['ok'])
        config,_=mb._load_config(repo);config['dream']={'agreement':'model','models':{'claude':'haiku','verifier':{'claude':'sonnet'}}}
        mw.write_managed_text(repo,mw.CONFIG_PATH,json.dumps(config))
        if fixture['kind']=='propose':
            by_file={}
            for line in fixture['input'].splitlines():
                row=json.loads(line);file,num=row['id'].rsplit(':',1);by_file.setdefault(file,[]).append((int(num),row['text']))
            for file,lines in by_file.items():
                self.assertEqual(list(range(1,len(lines)+1)),[i for i,_ in lines])
                mw.write_managed_text(repo,mb.SNAPSHOT_REL+'/'+file,'\n'.join(t for _,t in lines)+'\n')
        return repo

    def replay(self, fixture, name):
        repo=self.setup_input(fixture,name)
        if fixture['kind']=='verify':
            evidence=json.loads(fixture['input'])
            by_file={}
            for row in evidence['context']:
                file,number=row['id'].rsplit(':',1)
                by_file.setdefault(file,{})[int(number)]=row['text']
            for file,lines in by_file.items():
                mw.write_managed_text(repo,mb.SNAPSHOT_REL+'/'+file,
                    '\n'.join(lines.get(i,'') for i in range(1,max(lines)+1))+'\n')
            node=copy.deepcopy(evidence['item'])
            node.update(statusKind='active_lesson',timestamp=md.extract_line_timestamp(node['primaryPin']['quoteText']),
                        verdict=md.PENDING,edgeVerdict=md.PENDING,verifiedBy='')
            node['proposers']=[dc._receipt('claude',node,'f'*24,{'model':'haiku'})]
            graph={'schema':md.DREAM_ONTOLOGY_SCHEMA,'nodes':[node],
                   'edges':copy.deepcopy(node['edges']),'queueMeta':{}}
            self.save_graph(repo,graph)
            calls=[]
            def runner(*args,**kw):
                if args[1]==dc.PROPOSE_PROMPT:
                    return '[]'
                calls.append(json.loads(kw['stdin_text']))
                return fixture['answer']
            report=dc.dream_run(repo,verify_with='claude',runner=runner)
            self.assertTrue(report['ok'],report)
            self.assertEqual([evidence],calls)
            values=[v['verdict'] for n in mb._load_queue(repo)[0]['nodes'] for v in n.get('verification',[])]
            expected=fixture['expected']['verdict']
            if expected is None:
                self.assertEqual([],values)
                self.assertEqual(1,report['droppedOutput'])
            else:
                self.assertEqual([expected],values)
            return
        report=dc.dream_run(repo,runner=lambda *a,**kw:fixture['answer'])
        graph=mb._load_queue(repo)[0] or {'nodes':[]}
        actual={'failed':not report['ok'],'items':dc.fixture_items(graph['nodes']),'rejectionCodes':[r['reasonCode'] for r in report.get('rejections',[])]}
        return actual

    def test_every_fixture_and_three_reversions(self):
        fixtures=sorted(CORPUS.glob('*.json'));self.assertGreaterEqual(len(fixtures),5)
        for i,path in enumerate(fixtures):
            with self.subTest(path=path.name):
                f=json.loads(path.read_text());self.assertIn(f['provenance'],('recorded','reconstructed'))
                actual=self.replay(f,'replay'+str(i))
                if actual is not None:self.assertEqual(f['expected'],actual)
        for name,patch in [('timestamp-tail',mock.patch.object(dc,'_span_id',side_effect=lambda x:x if md.parse_span(x) else None)),('json-fence',mock.patch.object(dc,'_model_json',side_effect=lambda t,**kw:dc._strict_json(t))),('fenced-items',mock.patch.object(dc,'_model_json',side_effect=lambda t,**kw:dc._strict_json(t.strip()[7:-3].strip())))]:
            f=json.loads((CORPUS/(name+'.json')).read_text())
            with patch:self.assertNotEqual(f['expected'],self.replay(f,'revert'+name))

    def test_explicit_capture_mask_no_overwrite_symlink(self):
        fixture=json.loads((CORPUS/'well-formed.json').read_text());repo=self.setup_input(fixture,'capture')
        target=self.base/'answers'
        report=dc.dream_run(repo,runner=lambda *a,**kw:fixture['answer'],save_fixture=target)
        self.assertTrue(report['ok'],report)
        files=list(target.glob('*.json'));self.assertEqual(1,len(files))
        saved=json.loads(files[0].read_text());self.assertEqual('recorded',saved['provenance']);self.assertEqual(fixture['expected'],saved['expected'])
        dc._save_answer(target,'claude','haiku','verify','/Users/example/private '+md.CANARY_SECRETS[0], '{"verdict":"SUPPORTED"}',{'verdict':'SUPPORTED'})
        self.assertEqual(2,len(list(target.glob('*.json'))))
        text=''.join(p.read_text() for p in target.glob('*.json'))
        self.assertNotIn('/Users/',text)
        self.assertNotIn(md.CANARY_SECRETS[0],text)
        for p in target.glob('*.json'):json.loads(p.read_text())
        link=self.base/'answer-link';link.symlink_to(target,target_is_directory=True)
        for bad in (repo/'.memlet/fixtures',link):
            with self.assertRaises(ValueError):dc._fixture_directory(bad)
        self.assertFalse((repo/'.memlet/fixtures').exists())

    def test_captured_rejection_replays_exact_code(self):
        fixture=json.loads((CORPUS/'well-formed.json').read_text())
        candidates=dc._model_json(fixture['answer'],unwrap_items=True)
        candidates[0]['src_spans']=['missing.log:999']
        candidates=[candidates[0]]
        repo=self.setup_input(fixture,'rejected-capture');target=self.base/'rejections'
        report=dc.dream_run(repo,runner=lambda *a,**kw:json.dumps(candidates),save_fixture=target)
        saved=json.loads(next(target.glob('*.json')).read_text())
        self.assertEqual(['unpinnable_citation'],saved['expected']['rejectionCodes'])
        self.assertEqual(saved['expected'],self.replay(saved,'rejected-replay'))

class ReviewRegressions(Fixture):
    def test_exact_case_and_shared_file_normalisation(self):
        repo,nodes=self.fixture('pending',('single-line',))
        (repo/'MyFile.py').write_text('x')
        nid=nodes['single-line']['id']
        self.assertTrue(mb.learn_approve(repo,[nid],approver='Reviewer',paths=['MyFile.py'])['ok'])
        alias=self.base/'alias';alias.symlink_to(repo,target_is_directory=True)
        for files,expected in [('MyFile.py',True),([str(repo/'MyFile.py')],True),([str(alias/'MyFile.py')],True),(['myfile.py'],False),(['DEPLOY.PY'],False)]:
            with self.subTest(files=files):
                self.assertEqual(expected,bool(mb.dream_items(repo,files=files)['items']))
                self.assertEqual(expected,bool(mb.unified_recall(repo,files=files)['sections']['dream']))
                self.assertEqual(expected,bool(mb.changed_check(repo,files=files)['approvedLessons']))
        for read in (mb.dream_items,mb.unified_recall,mb.changed_check):
            for outside in (str(self.base/'outside.py'),'../outside.py'):
                self.assertTrue(any('outside repository' in n for n in read(repo,files=[outside])['notices']))

    def test_raw_token_boundaries_and_dot_prefix(self):
        repo,_=self.fixture('pending',('single-line',))
        context=mb.lesson_path_context(repo)
        for suffix in ('', '.', ',', ';', ':', '!', '?', ')', ']', '"', "'", '`'):
            for prefix in ('','./'):
                for tail in ('',' next'):
                    self.assertEqual(['deploy.py'],mb.literal_lesson_paths(repo,'','Edit '+prefix+'deploy.py'+suffix+tail,context=context))
        for text in ('deploy.py.bak','deploy.pyc','a/deploy.py/b','xdeploy.py','deploy.py.next'):
            self.assertEqual([],mb.literal_lesson_paths(repo,'','Edit '+text,context=context))
        for char in ('\x01','\t','\u200b','\U000e0061','\u0301','\ufe0f','\u20dd'):
            for text in (char+'deploy.py','deploy.py'+char):
                self.assertEqual([],mb.literal_lesson_paths(repo,'','Edit '+text,context=context))

    def test_independent_digest_and_record_path_tampering(self):
        repo,nodes=self.fixture('pending',('single-line',));nid=nodes['single-line']['id']
        self.assertTrue(mb.learn_approve(repo,[nid],approver='Reviewer',paths=['anchor.py'])['ok'])
        original=mb._load_queue(repo)[0]
        for both in (False,True):
            graph=copy.deepcopy(original);n=graph['nodes'][0]
            n['approval']['paths']=['deploy.py']
            if both:n['approvedPaths']=['deploy.py']
            self.save_graph(repo,graph)
            self.assertEqual('pending',md.approval_state(n,'node'))
            self.assertEqual([],mb.lesson_paths(n,repo))
            self.assertEqual([],mb.dream_items(repo)['items'])

    def test_paths_idempotent_multiselect_and_unused_flags(self):
        repo,nodes=self.fixture('pending',('single-line','BLOCKS'));ids=[n['id'] for n in nodes.values()]
        self.assertTrue(mb.learn_approve(repo,ids,approver='Reviewer',paths=['anchor.py'])['ok'])
        before=list(mb.decision_events(repo))
        result=mb.learn_paths(repo,ids,approver='Reviewer',paths=['anchor.py'])
        self.assertTrue(result['ok'],result);self.assertEqual([],result['applied'])
        self.assertEqual(set(ids),set(result['unchanged']));self.assertEqual(before,mb.decision_events(repo))
        result=self.cli('learn','--paths',*ids,'--by','Reviewer','--path','deploy.py','--repo',str(repo))
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        for mode in (['--pending'],['--reject',ids[0],'--by','Reviewer'],['--paths',ids[0],'--by','Reviewer','--path','deploy.py'],['--file','unused'],['--transcript','unused']):
            for flag in ('--reconsider','--refresh-citations'):
                self.assertEqual(2,self.cli('learn',*mode,flag).returncode)

    def test_custom_queue_refuses_paths(self):
        repo,nodes=self.fixture('pending',('single-line',));queue=repo/'custom.json'
        queue.write_bytes((repo/mb.QUEUE_REL).read_bytes())
        with self.assertRaisesRegex(ValueError,'paths require a queue at'):
            md.approve_queue_items(queue,[nodes['single-line']['id']],'Reviewer',snapshot_path=str(repo/mb.SNAPSHOT_REL),paths=['anchor.py'])

    def test_events_skip_bad_rows_and_newest_cap(self):
        repo,nodes=self.fixture('pending',('single-line',))
        self.assertTrue(mb.learn_approve(repo,[nodes['single-line']['id']],approver='Reviewer')['ok'])
        conn=sqlite3.connect(str(repo/'.memlet/ledger.sqlite3'))
        conn.executemany('INSERT INTO dream_decision_events(at,item_id,kind,decision,by,digest,paths) VALUES (?,?,?,?,?,?,?)', [('now','id','node','approved','Reviewer','digest','[]')]*4100)
        conn.execute("UPDATE dream_decision_events SET paths='invalid' WHERE seq=4101");conn.commit();conn.close()
        events=mb.decision_events(repo)
        self.assertTrue(events.truncated);self.assertEqual(1,events.skipped)
        self.assertEqual(4095,len(events));self.assertEqual(7,events[0]['seq']);self.assertEqual(4102,events[-1]['seq'])

    def test_mixed_edge_reader_agreement_table(self):
        repo,nodes=self.fixture('approved',('BLOCKS',));graph=mb._load_queue(repo)[0];node=graph['nodes'][0]
        for kind in ('SUPERSEDES','RESOLVES','GOVERNS'):
            edge=copy.deepcopy(node['edges'][0]);edge.pop('approval',None)
            edge.update(edgeId=edge['edgeId']+kind,edgeType=kind,toId='inline-target',edgeVerdict='PENDING')
            node['edges'].append(edge);graph['edges'].append(copy.deepcopy(edge))
        self.save_graph(repo,graph)
        item=mb.dream_items(repo)['items'][0]
        self.assertEqual(['BLOCKS'],item['edgeTypes']);self.assertFalse(item.get('quarantined'))
        hand,caps,report=self.bridge(repo)
        readers={'dream':mb.dream_items(repo)['items'],'recall':mb.unified_recall(repo)['sections']['dream'],'changed':mb.changed_check(repo,files=['deploy.py'])['approvedLessons'],'handover':report['approvedLessons']}
        for name,items in readers.items():
            with self.subTest(reader=name):
                self.assertEqual([node['id']],[i['id'] for i in items]);self.assertEqual(['BLOCKS'],items[0]['edgeTypes'])
                self.assertFalse(items[0].get('quarantined'))
        self.assertNotIn(node['id'],[i['id'] for i in mb.learn_pending(repo)['pendingNodes']])
        self.assertIn(node['id'],(repo/mb.PROMOTED_PAGE).read_text())
        self.assertNotIn('memlet-supersedes',json.dumps(caps));self.assertNotIn('memlet-resolves',json.dumps(caps));self.assertNotIn('memlet-governs',json.dumps(caps))

class CaptureFailures(Corpus):
    def test_capture_failure_never_changes_batch(self):
        fixture=json.loads((CORPUS/'well-formed.json').read_text())
        for i,error in enumerate((OSError('unwritable'),FileExistsError('existing'),ValueError('masking'))):
            repo=self.setup_input(fixture,'failure'+str(i))
            def fail(*args):
                states=list((repo/dc.RUNS_REL).glob('*.json'))
                self.assertTrue(states)
                self.assertTrue(any(json.loads(p.read_text()).get('completed') for p in states))
                raise error
            with mock.patch.object(dc,'_save_answer',side_effect=fail):
                report=dc.dream_run(repo,runner=lambda *a,**kw:fixture['answer'],save_fixture=self.base/'answers')
            self.assertTrue(report['ok'],report);self.assertEqual(0,report['exitCode']);self.assertTrue(report['items'])
            self.assertTrue(any('fixture capture failed' in n for n in report['notices']))

class SupersessionRegressions(Fixture):
    def test_replaced_only_paths_never_attach_current(self):
        repo,nodes=self.fixture('pending',('SUPERSEDES',));graph=mb._load_queue(repo)[0];node=graph['nodes'][0]
        file=md.parse_span(node['primaryPin']['span'])[0];path=repo/mb.SNAPSHOT_REL/file
        path.write_text('2026-11-11T10:00:00Z [user] Deploy with anchor.py.\n2026-11-11T10:01:00Z [user] From now on use deploy.py.\n')
        snapshot,_=mb._load_snapshot(repo)
        node['scopeTag']='general deployment'
        for item in [node]+node['edges']+graph['edges']:
            for key in ('primaryPin','supersededByPin','srcPin','targetPin','citedTargetPin'):
                if isinstance(item.get(key),dict):item[key]=md.resolve_pin(snapshot,item[key]['span'])
            if 'allPins' in item:item['allPins']=[md.resolve_pin(snapshot,p['span']) for p in item['allPins']]
        self.save_graph(repo,graph)
        self.assertTrue(mb.learn_approve(repo,[node['id']],approver='Reviewer')['ok'])
        self.assertEqual(['deploy.py'],mb.dream_items(repo)['items'][0]['paths'])
        for read,key in ((mb.dream_items,'items'),(mb.changed_check,'approvedLessons')):
            self.assertEqual([],read(repo,files=['anchor.py'])[key])
        self.assertEqual([],mb.unified_recall(repo,files=['anchor.py'])['sections']['dream'])
        hand,caps,_=self.bridge(repo)
        self.assertEqual(['deploy.py'],caps['capsules'][0]['paths'])

    def test_incoming_stale_supersession_demotes_standalone_paths(self):
        repo,nodes=self.fixture('approved',('BLOCKS','single-line'));graph=mb._load_queue(repo)[0]
        old,new=graph['nodes'];edge=new['edges'][0]
        edge.update(edgeType='SUPERSEDES',toId=old['id'],targetPin=copy.deepcopy(old['primaryPin']))
        graph['edges']=[copy.deepcopy(e) for n in graph['nodes'] for e in n['edges']]
        self.save_graph(repo,graph)
        self.assertTrue(mb.learn_approve(repo,[old['id'],new['id']],approver='Reviewer',reconsider=True,paths=['anchor.py'])['ok'])
        graph=mb._load_queue(repo)[0];old,new=graph['nodes']
        file,line,_=md.parse_span(new['primaryPin']['span']);path=repo/mb.SNAPSHOT_REL/file
        lines=path.read_text().splitlines();lines[line-1]+=' changed';path.write_text('\n'.join(lines)+'\n')
        self.assertEqual([],mb.dream_items(repo)['items'])
        self.assertEqual([],mb.lesson_paths(old,repo))
        self.assertEqual([],mb.lesson_paths(old,repo,context=mb.lesson_path_context(repo)))
