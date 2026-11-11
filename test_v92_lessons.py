"""9.2 paths, advisory decisions and a replayable model-answer corpus; no vendors."""
import copy
import json
import os
import sqlite3
import subprocess
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

    def test_scope_never_attaches_across_readers_and_human_paths(self):
        lines = ('Never call helper() in util.py.backup from tests',
                 'Never call helper() in util.py\u200b from tests',
                 'Never call helper() from tests',
                 'Never call helper() in util.py from tests')
        for number, line in enumerate(lines):
            with self.subTest(line=line):
                repo,_ = self.fixture('scope'+str(number), ('single-line',))
                (repo/'util.py').write_text('def helper(): pass\n')
                subprocess.run(['git','-C',str(repo),'add','util.py'],check=True,capture_output=True)
                transcript = self.base/('scope'+str(number)+'.log')
                transcript.write_text('2026-11-11T09:00:00Z [user] '+line+'\n')
                result = mb.learn_from_transcript(repo, transcript)
                self.assertTrue(result['ok'],result)
                nid = result['addedNodeIds'][0]
                graph = mb._load_queue(repo)[0]
                node = next(n for n in graph['nodes'] if n['id']==nid)
                self.assertEqual('util.py' if number==3 else 'project_rules',node['scopeTag'])
                # Model proposers may supply any label, including a truncated one.
                node['scopeTag']='util.py'
                self.save_graph(repo,graph)
                self.assertEqual([],mb.literal_lesson_paths(repo,'util.py',''))
                self.assertTrue(mb.learn_approve(repo,[nid],approver='Reviewer')['ok'])

                def check(attached, human=False):
                    node = next(n for n in mb._load_queue(repo)[0]['nodes'] if n['id']==nid)
                    self.assertEqual(['util.py'] if human else [],mb.lesson_paths(node,repo))
                    item = next(i for i in mb.dream_items(repo)['items'] if i['id']==nid)
                    self.assertEqual(['util.py'] if human else [],item.get('paths',[]))
                    for items in (mb.dream_items(repo,files=['util.py'])['items'],
                                  mb.unified_recall(repo,files=['util.py'])['sections']['dream']):
                        self.assertEqual(attached,any(i['id']==nid for i in items))
                    changed=[i for i in mb.changed_check(repo,files=['util.py'])['approvedLessons'] if i['id']==nid]
                    self.assertEqual(1,len(changed))
                    self.assertEqual('path' if attached else 'scope',changed[0]['via'])
                    hand,caps,_ = self.bridge(repo)
                    capsule = next(c for c in caps['capsules'] if c['id']==nid)
                    self.assertEqual(['util.py'] if attached else [],capsule['paths'])
                    # Both bridge documents use the same lesson selection.
                    for entry in hand['activeConstraints']+hand['rejectedApproaches']:
                        if entry['id']==nid:
                            self.assertEqual(['util.py'] if attached else [],entry['paths'])

                check(number==3)
                if number<3:
                    result=self.cli('learn','--paths',nid,'--by','Reviewer','--path','util.py','--repo',str(repo))
                    self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                    check(True,human=True)

    def test_scope_hint_is_informational_only(self):
        repo,_=self.fixture('hint',('BLOCKS',))
        rows=mb.changed_check(repo,files=['deploy.py'])['hints']
        self.assertEqual(1,len(rows))
        self.assertEqual('scope',rows[0]['via'])
        self.assertNotIn('paths',rows[0])
        self.assertEqual([],mb.dream_items(repo,tiers=('hint','pending'),files=['deploy.py'])['items'])
        _,caps,_=self.bridge(repo)
        self.assertFalse(any(c['id']==rows[0]['id'] for c in caps['capsules']))

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
        self.assertTrue(all(i['paths']==['anchor.py'] for i in mb.dream_items(repo)['items']))
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
        self.assertEqual([],mb.literal_lesson_paths(elsewhere,'plain.py','',context=context))
        self.assertEqual(['plain.py'],mb.literal_lesson_paths(elsewhere,'ignored','Use plain.py',context=context))

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
                self.assertNotIn('paths', item)  # literal attachment does not alter default legacy bytes
                self.assertEqual(not bool(hidden),bool(mb.dream_items(repo,files=['anchor.py'])['items']))
                self.assertEqual(not bool(hidden),bool(mb.unified_recall(repo,files=['anchor.py'])['sections']['dream']))
                changed=mb.changed_check(repo,files=['anchor.py'])['approvedLessons']
                self.assertEqual(hidden in ('','\u200b','\U000e0061'),bool(changed))
                if changed:self.assertEqual('scope' if hidden else 'path',changed[0]['via'])
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
        self.assertEqual(['anchor.py'],mb.dream_items(repo, files=['anchor.py', 'deploy.py'])['items'][0]['paths'])

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
                changed=mb.changed_check(repo,files=files)['approvedLessons']
                self.assertEqual(expected or files==['DEPLOY.PY'],bool(changed))
                if changed:self.assertEqual('path' if expected else 'scope',changed[0]['via'])
        for read in (mb.dream_items,mb.unified_recall):
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
        self.assertEqual(['deploy.py'],mb.dream_items(repo, files=['anchor.py', 'deploy.py'])['items'][0]['paths'])
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
        self.assertTrue(mb.learn_approve(repo,[edge['edgeId']],approver='Reviewer')['ok'])
        graph=mb._load_queue(repo)[0];old,new=graph['nodes']
        file,line,_=md.parse_span(new['primaryPin']['span']);path=repo/mb.SNAPSHOT_REL/file
        lines=path.read_text().splitlines();lines[line-1]+=' changed';path.write_text('\n'.join(lines)+'\n')
        self.assertEqual([],mb.dream_items(repo)['items'])
        self.assertEqual([],mb.lesson_paths(old,repo))
        self.assertEqual([],mb.lesson_paths(old,repo,context=mb.lesson_path_context(repo)))

class FinalReaderFixes(Fixture):
    """Final-audit reproductions; all inputs live in the fixture sandbox."""
    def keyword_repo(self, texts, *, approve=True):
        self.write('AGENTS.md', '# Rules\n- Always keep `anchor.py::guard` enabled.\n')
        self.write('anchor.py', 'def guard():\n    return True\n')
        self.write('deploy.py', '# root\n')
        self.write('src/deploy.py', '# nested\n')
        self.assertTrue(mw.init_project(self.root)['ok'])
        subprocess.run(['git', '-C', str(self.root), 'add', 'AGENTS.md', 'anchor.py', 'deploy.py', 'src/deploy.py'],
                       check=True, capture_output=True)
        source = self.base / 'stream.jsonl'
        source.write_text('\n'.join(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': t}})
                                    for t in texts) + '\n')
        learned = mb.learn_from_transcript(self.root, source)
        self.assertTrue(learned['ok'], learned)
        if approve:
            self.assertTrue(mb.learn_approve(self.root, learned['addedNodeIds'], approver='Reviewer')['ok'])
        return learned['addedNodeIds']

    def mixed_edges(self):
        repo, _ = self.fixture('approved', ('BLOCKS',))
        data = mb._load_queue(repo)[0]
        node = data['nodes'][0]
        for relation in ('GOVERNS', 'RESOLVES'):
            edge = copy.deepcopy(node['edges'][0])
            edge.update(edgeId='PENDING-' + relation, edgeType=relation, toId='INLINE-' + relation, edgeVerdict='PENDING')
            edge.pop('approval', None)
            node['edges'].append(edge)
            data['edges'].append(copy.deepcopy(edge))
        self.save_graph(repo, data)
        return repo, node['id'], node['edges'][0]['edgeId']

    def test_paths_reseal_only_reviewed_relations_cli_and_log(self):
        repo, nid, approved_edge = self.mixed_edges()
        result = self.cli('learn', '--paths', nid, '--by', 'Reviewer', '--path', 'anchor.py', '--repo', str(repo))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual([approved_edge], record['resealedEdges'])
        self.assertEqual([{'id': 'PENDING-GOVERNS', 'state': 'pending'},
                          {'id': 'PENDING-RESOLVES', 'state': 'pending'}], record['untouchedEdges'])
        edges = mb._load_queue(repo)[0]['nodes'][0]['edges']
        self.assertEqual(['approved', 'pending', 'pending'], [md.approval_state(e, 'edge') for e in edges])
        self.assertEqual(['BLOCKS'], mb.dream_items(repo)['items'][0]['edgeTypes'])
        decisions = {e['item_id']: e['decision'] for e in mb.decision_events(repo)}
        self.assertEqual('reconsidered', decisions[approved_edge])
        self.assertEqual('left-pending', decisions['PENDING-GOVERNS'])
        self.assertIn('Re-sealed relations:', record['formatted'])
        self.assertIn('PENDING-RESOLVES (pending)', record['formatted'])

    def test_reconsider_cannot_overturn_rejected_relation_911_noop(self):
        repo, nid, approved_edge = self.mixed_edges()
        self.assertTrue(mb.learn_reject(repo, ['PENDING-RESOLVES'], approver='Reviewer')['ok'])
        before = (repo / mb.QUEUE_REL).read_bytes()
        result = mb.learn_approve(repo, [nid], approver='Reviewer', reconsider=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual([nid], result['unchanged'])
        self.assertEqual([], result['cascadedEdges'])
        self.assertEqual(before, (repo / mb.QUEUE_REL).read_bytes())
        self.assertEqual('pending', mb.dream_items(repo, tiers=('pending',))['items'][0]['tier'])
        self.assertIn({'id': 'PENDING-RESOLVES', 'state': 'rejected'}, result['untouchedEdges'])
        self.assertEqual('left-rejected', mb.decision_events(repo, 'PENDING-RESOLVES')[-1]['decision'])
        resealed = mb.learn_approve(repo, [nid], approver='Reviewer', reconsider=True, paths=['anchor.py'])
        self.assertEqual([approved_edge], resealed['resealedEdges'])
        self.assertEqual(['approved', 'pending', 'rejected'],
                         [md.approval_state(e, 'edge') for e in mb._load_queue(repo)[0]['nodes'][0]['edges']])
        self.assertEqual([], mb.dream_items(repo)['items'])

    def test_explicit_relation_review_in_reseal_batch_is_logged_as_a_decision(self):
        repo, nid, approved_edge = self.mixed_edges()
        result = mb.learn_approve(repo, [nid, 'PENDING-GOVERNS'], approver='Reviewer', reconsider=True)
        self.assertTrue(result['ok'], result)
        self.assertNotIn('PENDING-GOVERNS', [e['id'] for e in result['untouchedEdges']])
        self.assertEqual('reconsidered', mb.decision_events(repo, 'PENDING-GOVERNS')[-1]['decision'])
        self.assertEqual(['BLOCKS', 'GOVERNS'], mb.dream_items(repo)['items'][0]['edgeTypes'])

    def test_reconsider_only_reseals_current_approved_edges(self):
        repo, nid, approved_edge = self.mixed_edges()
        result = mb.learn_approve(repo, [nid], approver='Reviewer', reconsider=True)
        self.assertEqual([approved_edge], result['cascadedEdges'])
        self.assertEqual(['approved', 'pending', 'pending'],
                         [md.approval_state(e, 'edge') for e in mb._load_queue(repo)[0]['nodes'][0]['edges']])

    def test_leading_punctuation_table_all_attachment_consumers(self):
        from memlet import memlet_graph as mg
        bad = ['src\\deploy.py', 'C:\\x\\deploy.py', 'pkg:deploy.py', 'a/deploy.py', '~deploy.py', '%deploy.py',
               '@deploy.py', '+deploy.py', '=deploy.py', '-deploy.py', '.deploy.py', '?deploy.py',
               '\u200bdeploy.py', '\u034fdeploy.py']
        good = ['deploy.py', '"deploy.py"', "'deploy.py'", '`deploy.py`', '(deploy.py)', '[deploy.py]',
                '{deploy.py ', '<deploy.py ', './deploy.py', 'src/deploy.py']
        self.assertNotIn('deploy.py', mb._lesson_path_candidates('Never edit \x01deploy.py by hand.'))
        ids = self.keyword_repo(['Never edit ' + p + ' by hand.' for p in bad + good])
        self.assertEqual(len(bad + good), len(ids))
        expected = {nid: [] if i < len(bad) else ['src/deploy.py'] if i == len(bad + good)-1 else ['deploy.py']
                    for i, nid in enumerate(ids)}
        default = mb.dream_items(self.root)
        self.assertTrue(all('paths' not in i for i in default['items']))
        by_id = {i['id']: i for i in mb.dream_items(self.root, files=['deploy.py', 'src/deploy.py'])['items']}
        for nid, paths in expected.items():
            self.assertEqual(paths, by_id.get(nid, {}).get('paths', []), nid)
        for path in ('deploy.py', 'src/deploy.py'):
            with mock.patch.object(mg, '_monotonic', return_value=0):
                neighbors = mg.neighbors(self.root, path, budget=8000)
            self.assertTrue(neighbors['ok'], neighbors)
            self.assertEqual({nid for nid, paths in expected.items() if path in paths},
                             {i['id'] for i in neighbors['groups']['blocks']})
        _, caps, _ = self.bridge(self.root)
        self.assertEqual(expected, {i['id']: i['paths'] for i in caps['capsules']})

    def test_rule_file_normalisation_cli_mcp_and_lesson_exactness(self):
        ids = self.keyword_repo(['Never edit anchor.py by hand.'])
        (self.root / 'alias.py').symlink_to('anchor.py')
        for path in ('anchor.py', ' anchor.py', 'anchor.py\n', '././anchor.py', str(self.root / 'alias.py')):
            with self.subTest(path=path):
                result = mb.changed_check(self.root, files=[path])
                self.assertIn('anchor.py::guard', [i['anchor'] for i in result['affected']])
                rpc = self.rpc('memlet_check', files=[path])['structuredContent']
                self.assertIn('anchor.py::guard', [i['anchor'] for i in rpc['affected']])
                if path != 'anchor.py':
                    self.assertEqual([], mb.dream_items(self.root, files=[path])['items'])

    def test_attachment_unavailable_is_visible_everywhere_and_human_paths_survive(self):
        from memlet.vocabulary import ATTACHMENT_INCOMPLETE
        ids = self.keyword_repo(['Never edit deploy.py by hand.'])
        self.assertTrue(mb.learn_paths(self.root, ids, approver='Reviewer', paths=['anchor.py'])['ok'])
        # Size exhaustion is deterministic and exercises the real git reader.
        with mock.patch.object(mb, 'LITERAL_FILES_MAX_BYTES', 1):
            reads = [mb.dream_items(self.root), mb.unified_recall(self.root, files=['anchor.py'], budget=8000),
                     mb.changed_check(self.root, files=['anchor.py']), mw.build_handover(self.root, budget=8000, output_format='markdown')]
            for result in reads:
                self.assertTrue(result['attachmentIncomplete'], result)
                self.assertEqual(ATTACHMENT_INCOMPLETE, result['attachmentReason'])
            self.assertEqual(['anchor.py'], reads[0]['items'][0]['paths'])
            self.assertEqual(ids, [i['id'] for i in reads[1]['sections']['dream']])
            self.assertTrue(mw.verify_handover_checksum(reads[-1]))
            self.assertIn(ATTACHMENT_INCOMPLETE, reads[-1]['formatted'])
            hand, caps, _ = self.bridge(self.root)
            for doc in (hand, caps):
                self.assertTrue(doc['attachmentIncomplete'])
                self.assertEqual(1, doc['omitted']['literalAttachments'])
                self.assertTrue(hb.verify(doc))
            self.assertEqual(['anchor.py'], caps['capsules'][0]['paths'])
        with mock.patch.object(mb, 'LITERAL_FILES_MAX_BYTES', 1):
            tiny = mb.unified_recall(self.root, files=['anchor.py'], budget=10)
            self.assertTrue(tiny['attachmentIncomplete'])
            self.assertLessEqual(tiny['tokenEstimate'], tiny['tokenBudget'])
        self.assertNotIn('attachmentIncomplete', mw.build_handover(self.root, budget=8000))
        with mock.patch.object(mb.subprocess, 'Popen', side_effect=OSError('git unavailable')):
            self.assertTrue(mb.dream_items(self.root)['attachmentIncomplete'])
        ctx = mb.lesson_path_context(self.root, remaining=0)
        self.assertTrue(ctx['attachmentIncomplete'])

    def test_recall_files_cli_selects_lessons_and_anchored_rules(self):
        ids = self.keyword_repo(['Never edit deploy.py by hand.', 'Never edit anchor.py by hand.'])
        result = self.cli('recall', 'Never', '--files', 'deploy.py', '--repo', str(self.root), '--budget', '8000')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual([ids[0]], [i['id'] for i in parsed['sections']['dream']])
        self.assertFalse(parsed['sections']['rules'])
        help_text = self.cli('recall', '--help').stdout
        self.assertIn('attachment rule', help_text)
        import memory_cli
        legacy = memory_cli._build_parser().parse_args(['recall', '--f'])
        self.assertTrue(legacy.federated)
        self.assertIsNone(legacy.files)
        conflict = self.cli('recall', 'Never', '--files', 'deploy.py', '--at', '2000-01-01')
        self.assertEqual(2, conflict.returncode)
        self.assertIn('--at', conflict.stderr)

    def test_live_transcript_echo_and_duplicate_quote_are_counted(self):
        quote = 'Never call refund() in billing.py without the retry wrapper; it double-charges on timeouts.'
        ids = self.keyword_repo([quote])
        # Same visible assistant-message shape and relevant prose as the live
        # stream fixture; no host paths or provider execution are involved.
        live = {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text':
            '1. **Denial:** Yes. The first Edit call was denied by a Memlet PreToolUse hook. '
            'The reason, quoted from the start: "Memlet stopped the first edit of `billing.py` in this session; '
            'the call did not run. A person approved the lessons below for this file." '
            'The cited approved lesson (DRM-ba4128230195) says: "' + quote + '"'}]}}
        source = self.base / 'echo.jsonl'
        duplicate = {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': quote}]}}
        source.write_text(json.dumps(live) + '\n' + json.dumps(duplicate) + '\n')
        lines, stats = md.read_jsonl_transcript(source)
        self.assertEqual('', lines[0])
        self.assertEqual(1, stats['skippedMemletEcho'])
        learned = mb.learn_from_transcript(self.root, source)
        self.assertTrue(learned['ok'], learned)
        self.assertEqual([], learned['addedNodeIds'])
        self.assertEqual(2, learned['skippedMemletEcho'])
        self.assertIn('skipped: 2', learned['formatted'])
        ingested = dc.dream_ingest(self.root, source)
        self.assertEqual(1, ingested['skippedMemletEcho'])
        self.assertEqual(ids, [n['id'] for n in mb._load_queue(self.root)[0]['nodes']])

    def test_masked_duplicate_echo_skips_only_the_same_whole_sentence(self):
        from memlet.vocabulary import MEMLET_ECHO_NOTICE
        secrets = ['sk_' + 'live_abcdefghijklmnopqrstuvwx', 'ghp_abcdefghijklmnopqrstuvwx']
        sentence = 'Never share billing credential %s with customers.'
        first_ids = self.keyword_repo([sentence % secrets[0]])
        source = self.base / 'masked-echo.jsonl'
        source.write_text(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': sentence % secrets[1]}}) + '\n')
        learned = mb.learn_from_transcript(self.root, source)
        self.assertTrue(learned['ok'], learned)
        self.assertEqual([], learned['addedNodeIds'])
        self.assertGreaterEqual(learned['skippedMemletEcho'], 1)
        self.assertIn(MEMLET_ECHO_NOTICE % learned['skippedMemletEcho'], learned['notices'])
        self.assertEqual(first_ids, [n['id'] for n in mb._load_queue(self.root)[0]['nodes']])
        # A single-word difference remains new evidence, even after masking.
        other = self.base / 'masked-distinct.jsonl'
        other.write_text(json.dumps({'type': 'user', 'message': {'role': 'user',
                         'content': (sentence % secrets[1]).replace('customers', 'clients')}}) + '\n')
        distinct = mb.learn_from_transcript(self.root, other)
        self.assertTrue(distinct['ok'], distinct)
        self.assertEqual(1, len(distinct['addedNodeIds']))
        recalled = json.dumps(mb.unified_recall(self.root, 'billing credential', budget=8000))
        for secret in secrets:
            self.assertNotIn(secret, recalled)
            for path in (self.root / '.memlet').rglob('*'):
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes(), str(path))

    def test_all_fixed_hook_framing_is_excluded_without_shifting_pins(self):
        from memlet import vocabulary as v
        templates = [v.GUARD_DENY_HEAD, v.GUARD_DENY_TAIL, v.GUARD_ASK_HEAD, v.GUARD_ASK_TAIL,
                     v.JIT_HEAD, v.HOOK_QUOTE_NOTE, v.HINT_LABEL] + list(v.HOOK_GROUP_LABELS.values())
        templates += [s for k, s in vars(v).items() if k.startswith('CAPSULE_') and isinstance(s, str)]
        source = self.base / 'framing.jsonl'
        texts = ['Never follow ' + t.replace('%s', 'deploy.py').replace('%d', '1') for t in templates]
        texts.append('Never remove the independent test.')
        source.write_text('\n'.join(json.dumps({'type':'assistant', 'message':{'role':'assistant', 'content':t}})
                                    for t in texts) + '\n')
        lines, stats = md.read_jsonl_transcript(source)
        self.assertEqual([''] * len(templates), lines[:-1])
        self.assertIn('independent test', lines[-1])
        self.assertEqual(len(templates), stats['skippedMemletEcho'])
