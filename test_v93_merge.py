"""N1 merge decisions: hermetic repositories, real readers, no models or network."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

import test_v91_integration as fixtures
from memlet import memlet_bridge as mb, memlet_dream as md, memlet_wiki as mw
from memlet import memlet_graph as mg, memlet_hooks as mh, handover_bridge as hb
from memlet import memlet_cortex as cortex, memlet_dreamcycle as dc, vocabulary as v

STAMP = '2026-01-02T00:00:00Z'


class MergeTests(fixtures.Scratch):
    def setUp(self):
        super().setUp()
        self.write('src.py', 'def run():\n    return 1\n')
        self.write('other.py', 'def other():\n    return 1\n')
        self.write('AGENTS.md', '# Fixture\n')
        self.write('.gitignore', '.memlet/\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture', '-c', 'user.email=f@example.invalid',
                        '-c', 'core.hooksPath=' + str(self.base / 'no-hooks'), 'commit', '-qm', 'fixture'],
                       check=True, capture_output=True)
        self.assertTrue(mw.init_project(self.root)['ok'])
        self.lines = ['2026-01-01T00:0%d:00Z [user] Never retry lesson %s in src.py.' % (i, ident)
                      for i, ident in enumerate(('A', 'B', 'C'))]
        self.write(mb.SNAPSHOT_REL + '/s.log', '\n'.join(self.lines) + '\n')
        nodes = []
        for i, ident in enumerate(('A', 'B', 'C')):
            pin = md.resolve_pin({'s.log': self.lines}, 's.log:%d' % (i + 1))
            edge = dict(edgeId='E-' + ident, edgeType='BLOCKS', fromId=ident, toId='BLOCKS:' + ident,
                        targetLabel='fixture', srcPin=pin, srcTimestamp=md.extract_line_timestamp(pin['quoteText']), edgeVerdict='VALID')
            nodes.append(dict(id=ident, title='lesson ' + ident, category='repeated_pitfall', statusKind='open_trap',
                              scopeTag='src.py', primaryPin=pin, timestamp=md.extract_line_timestamp(pin['quoteText']), allPins=[pin], edges=[edge], verdict='SUPPORTED'))
        self.save(dict(schema=md.DREAM_ONTOLOGY_SCHEMA, nodes=nodes,
                       edges=[copy.deepcopy(e) for n in nodes for e in n['edges']]))
        with mock.patch.object(md, '_utc_now_iso', return_value=STAMP):
            self.assertTrue(mb.learn_approve(self.root, ['A', 'B', 'C'], approver='Reviewer')['ok'])
        self.original = self.graph()

    def graph(self):
        return mb._load_queue(self.root)[0]

    def save(self, graph):
        old = mb._load_queue(self.root)[0] if (self.root / mb.QUEUE_REL).exists() else None
        md._commit_queue_and_db(self.root / mb.QUEUE_REL, json.dumps(graph) + '\n', self.root / mb.DB_REL, graph,
                                replaces_sha=md.graph_sha256(old) if old else None)

    @staticmethod
    def seal(node):
        node['approval']['digest'] = md.node_approval_digest(node)
        for edge in node['edges']:
            edge['approval']['digest'] = md.edge_approval_digest(edge)

    def merge(self, keep='A', same=('B',)):
        result = mb.learn_merge(self.root, keep, same, approver='Reviewer')
        self.assertTrue(result['ok'], result)
        return result

    def state(self, ident='B'):
        return mb.merge_view(self.root)['states'][ident]

    def test_record_is_outside_all_approval_and_trust_digests(self):
        before = self.graph()
        self.merge(same=('B', 'C'))
        after = self.graph()
        for old, new in zip(before['nodes'], after['nodes']):
            self.assertEqual(md.node_approval_digest(old), md.node_approval_digest(new))
            self.assertEqual(dc.trust_digest(old), dc.trust_digest(new))
            self.assertEqual(old['approval'], new['approval'])
            self.assertEqual(old['edges'], new['edges'])
        self.assertEqual({'schema', 'into', 'by', 'at', 'memberDigest', 'keepDigest'}, set(after['nodes'][1]['merge']))
        item = mb.dream_items(self.root)['items'][0]
        self.assertEqual(['B', 'C'], item['mergedFrom'])
        self.assertEqual(['A'], [i['id'] for i in mb.dream_items(self.root)['items']])
        self.assertTrue(md.queue_db_in_sync(self.root / mb.QUEUE_REL, self.root / mb.DB_REL))
        self.assertEqual(['B', 'C'], mb.learn_unmerge(self.root, ['B', 'C'], approver='Reviewer')['applied'])
        self.assertNotIn('mergedFrom', mb.dream_items(self.root)['items'][0])

    def test_refusal_table_is_atomic_and_messages_are_fixed(self):
        def reject(node):
            node['approval']['decision'] = 'rejected'
        def pending(node):
            node['approval'].pop('digest')
        def replacement(graph, owner, incoming=False):
            edge = copy.deepcopy(graph['nodes'][owner]['edges'][0])
            edge.update(edgeId='SUPER', edgeType='SUPERSEDES', fromId='C' if incoming else graph['nodes'][owner]['id'],
                        toId=graph['nodes'][owner]['id'] if incoming else 'C')
            graph['edges'].append(edge)
        cases = [
            ('unknown-id', 'missing', ('B',), lambda g: None),
            ('unknown-id', 'A', ('B', 'missing'), lambda g: None),
            ('same-as-keep', 'A', ('B', 'A'), lambda g: None),
            ('keep-rejected', 'A', ('B',), lambda g: reject(g['nodes'][0])),
            ('member-rejected', 'A', ('B', 'C'), lambda g: reject(g['nodes'][2])),
            ('keep-not-approved', 'A', ('B',), lambda g: pending(g['nodes'][0])),
            ('supersedes', 'A', ('B',), lambda g: replacement(g, 0)),
            ('supersedes', 'A', ('B',), lambda g: replacement(g, 1)),
            ('supersedes', 'A', ('B',), lambda g: replacement(g, 0, True)),
            ('supersedes', 'A', ('B',), lambda g: replacement(g, 1, True)),
            ('relation-types', 'A', ('B',), lambda g: g['nodes'][0]['edges'][0].update(edgeType='RESOLVES')),
        ]
        for reason, keep, same, change in cases:
            with self.subTest(reason=reason, keep=keep, same=same):
                graph = copy.deepcopy(self.original); change(graph)
                if reason == 'relation-types':
                    self.seal(graph['nodes'][0]); graph['edges'][0] = copy.deepcopy(graph['nodes'][0]['edges'][0])
                self.save(graph)
                before = (self.root / mb.QUEUE_REL).read_bytes()
                events = list(mb.decision_events(self.root))
                read = mb.merge_blockers(self.root, keep, same)
                self.assertTrue(read['ok'], read)
                result = mb.learn_merge(self.root, keep, same, approver='Reviewer')
                self.assertFalse(result['ok'], result)
                self.assertIn(reason, [r['reason'] for r in result['refused']])
                self.assertIn(v.MERGE_REASONS[reason], result['error'])
                self.assertEqual(before, (self.root / mb.QUEUE_REL).read_bytes())
                self.assertEqual(events, mb.decision_events(self.root))
        for reason, keep, same in [('keep-is-member', 'B', ('C',)), ('member-has-members', 'C', ('A',))]:
            self.save(copy.deepcopy(self.original)); self.merge()
            before = (self.root / mb.QUEUE_REL).read_bytes()
            result = mb.learn_merge(self.root, keep, same, approver='Reviewer')
            self.assertIn(reason, [r['reason'] for r in result['refused']])
            self.assertEqual(before, (self.root / mb.QUEUE_REL).read_bytes())
        before = (self.root / mb.QUEUE_REL).read_bytes()
        for fn, args, kw, reason in [
            (mb.learn_merge, ('A', ['B']), {}, 'by-required'),
            (mb.learn_merge, ('', ['B']), {'approver':'R'}, 'keep-required'),
            (mb.learn_merge, ('A', []), {'approver':'R'}, 'same-required'),
            (mb.learn_unmerge, ([],), {'approver':'R'}, 'ids-required'),
            (mb.learn_unmerge, (['B'],), {}, 'by-required')]:
            result = fn(self.root, *args, **kw)
            self.assertEqual(reason, result['reason'])
            self.assertEqual(v.MERGE_REASONS[reason], result['error'])
            self.assertEqual(before, (self.root / mb.QUEUE_REL).read_bytes())
        result = mb.learn_unmerge(self.root, ['B', 'missing'], approver='R')
        self.assertFalse(result['ok']); self.assertEqual(before, (self.root / mb.QUEUE_REL).read_bytes())

    def lapse(self, reason, change):
        self.merge()
        saved = self.graph(); changed = copy.deepcopy(saved); change(changed); self.save(changed)
        self.assertEqual(reason, self.state()['reason'])
        self.assertEqual('lapsed', self.state()['state'])
        pending = mb.learn_pending(self.root, _record_review=False)
        self.assertEqual(reason, pending['lapsedMerges'][0]['reason'])
        self.assertIn(v.MERGE_REASONS[reason], pending['formatted'])
        # Restore the exact queue state: neither merge nor unmerge is run.
        self.save(saved)
        self.assertEqual('merged', self.state()['state'])
        self.assertNotIn('B', [i['id'] for i in mb.dream_items(self.root, tiers=('approved','hint','pending'))['items']])

    def test_lapse_member_digest_and_recovery(self):
        self.lapse('member-changed', lambda g: g['nodes'][1].update(title='changed member'))

    def test_lapse_keep_digest_and_recovery(self):
        self.lapse('keep-changed', lambda g: g['nodes'][0].update(title='changed keep'))

    def test_lapse_keep_missing_and_recovery(self):
        self.lapse('keep-missing', lambda g: g['nodes'].pop(0))

    def test_lapse_keep_rejected_or_revoked_and_recovery(self):
        self.lapse('keep-rejected', lambda g: g['nodes'][0]['approval'].update(decision='rejected'))
        self.assertTrue(mb.learn_reject(self.root, ['A'], approver='R')['ok'])
        self.assertEqual('keep-rejected', self.state()['reason'])
        self.assertIn('revoked', [r['decision'] for r in mb.decision_events(self.root, 'A')])
        self.assertIn('B', [i['id'] for i in mb.dream_items(self.root)['items']])

    def test_lapse_keep_became_member_and_recovery(self):
        def change(g):
            a, _, c = g['nodes']
            a['merge'] = dict(schema=v.MERGE_SCHEMA, into='C', by='R', at=STAMP,
                              memberDigest=md.node_approval_digest(a), keepDigest=md.node_approval_digest(c))
        self.lapse('keep-is-member', change)

    def test_lapse_approved_keep_falls_to_pending_and_recovery(self):
        # Node digest/approval stay valid; a rejected edge demotes the shared tier.
        self.lapse('keep-not-approved', lambda g: g['nodes'][0]['approval'].update(decision='pending'))

    def test_lapse_snapshot_not_just_stored_approval(self):
        self.merge()
        path = self.root / mb.SNAPSHOT_REL / 's.log'; before = path.read_bytes()
        path.write_text('\n'.join(['changed first line'] + self.lines[1:]) + '\n')
        self.assertEqual('relation-types', self.state()['reason'])
        self.assertIn('B', [i['id'] for i in mb.dream_items(self.root)['items']])
        path.write_bytes(before)
        self.assertEqual('merged', self.state()['state'])

    def test_merge_view_reuses_fresh_and_loaded_attachment_contexts(self):
        self.merge()
        self.assertEqual(mb.merge_view(self.root), mb.merge_view(self.root, context=mb.lesson_path_context(self.root)))
        g = self.graph(); g['nodes'][0]['edges'][0]['approval']['decision'] = 'rejected'; self.save(g)
        context = mb.lesson_path_context(self.root)
        self.assertEqual('relation-types', mb.merge_view(self.root, context=context)['states']['B']['reason'])
        with mock.patch.object(mb, 'dream_tiers', side_effect=AssertionError('reuse the computed tiers')):
            self.assertEqual('relation-types', mb.merge_view(self.root, context=context)['states']['B']['reason'])

    def test_rebuild_preserves_merge_decision_for_changed_content(self):
        self.merge()
        old = self.graph(); built = copy.deepcopy(old)
        built['nodes'][1]['title'] = 'changed rebuilt lesson'
        built['nodes'][1].pop('merge')
        rebuilt, _ = mb._merge(old, built, mb._load_snapshot(self.root)[0], self.root / mb.SNAPSHOT_REL)
        self.assertEqual(old['nodes'][1]['merge'], rebuilt['nodes'][1]['merge'])
        self.save(rebuilt)
        self.assertEqual('member-changed', self.state()['reason'])

    def test_pending_groups_and_pending_merges_do_not_invent_approval(self):
        g = self.graph()
        for n in g['nodes']:
            n.pop('approval')
            for e in n['edges']: e.pop('approval')
        g['edges'] = [copy.deepcopy(e) for n in g['nodes'] for e in n['edges']]; self.save(g)
        self.merge(same=('B', 'C'))
        result = mb.learn_pending(self.root, _record_review=False)
        self.assertEqual(['A'], [i['id'] for i in result['pendingNodes']])
        self.assertEqual(['B', 'C'], result['pendingNodes'][0]['mergedFrom'])
        self.assertEqual(['B', 'C'], [i['id'] for i in result['mergedLessons'][0]['members']])
        self.assertEqual(1, result['counts']['pendingNodes'])
        self.assertEqual(['E-A'], [i['edgeId'] for i in result['pendingEdges']])
        self.assertIn('  - `B` [pending] says the same as `A`', result['formatted'])
        self.assertEqual([], mb.dream_items(self.root)['items'])
        self.assertEqual(['A'], [i['id'] for i in mb.dream_items(self.root, tiers=('pending',))['items']])

    def test_cli_modes_prefixes_and_plain_errors(self):
        for args, message in [(['--merge','A','--by','R'], v.MERGE_REASONS['same-required']),
                              (['--merge','A','--same','B'], v.MERGE_REASONS['by-required']),
                              (['--same','B','--by','R'], v.MERGE_REASONS['same-without-merge']),
                              (['--pending','--same','B'], v.MERGE_REASONS['same-without-merge'])]:
            before = (self.root / mb.QUEUE_REL).read_bytes()
            got = self.cli('learn', *args, '--repo', str(self.root))
            self.assertEqual(2, got.returncode); self.assertIn(message, got.stderr)
            self.assertEqual(before, (self.root / mb.QUEUE_REL).read_bytes())
        result = self.cli('learn', '--merge','A','--same','B','C','--by','R','--repo',str(self.root))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(['B','C'], json.loads(result.stdout)['applied'])
        result = self.cli('learn', '--unmerge','B','C','--by','R','--repo',str(self.root))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        # --s was the legacy --session prefix; it must not become --same.
        result = self.cli('learn', '--pending','--s','legacy','--repo',str(self.root))
        self.assertIn('--session', result.stderr); self.assertNotIn('--same requires', result.stderr)

    def test_failed_queue_write_and_concurrent_writers(self):
        before = (self.root / mb.QUEUE_REL).read_bytes()
        events = list(mb.decision_events(self.root))
        replace = os.replace
        def fail(source, target):
            if Path(target) == self.root / mb.QUEUE_REL:
                raise OSError('injected queue replace failure')
            return replace(source, target)
        with mock.patch('os.replace', side_effect=fail):
            result = mb.learn_merge(self.root, 'A', ['B','C'], approver='R')
        self.assertFalse(result['ok']); self.assertEqual(before, (self.root / mb.QUEUE_REL).read_bytes())
        self.assertEqual(events, mb.decision_events(self.root))
        self.assertTrue(md.queue_db_in_sync(self.root / mb.QUEUE_REL, self.root / mb.DB_REL))
        barrier, results = threading.Barrier(2), []
        def run(ident):
            barrier.wait()
            results.append(mb.learn_merge(self.root, 'A', [ident], approver='R'))
        threads = [threading.Thread(target=run, args=(ident,)) for ident in ('B','C')]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertTrue(all(r['ok'] for r in results), results)
        self.assertEqual({'A':['B','C']}, mb.merge_view(self.root)['members'])
        before = (self.root / mb.QUEUE_REL).read_bytes()
        events = list(mb.decision_events(self.root))
        with mock.patch('os.replace', side_effect=fail):
            result = mb.learn_unmerge(self.root, ['B','C'], approver='R')
        self.assertFalse(result['ok'])
        self.assertEqual(before, (self.root / mb.QUEUE_REL).read_bytes())
        self.assertEqual(events, mb.decision_events(self.root))

    def test_historical_rows_hide_until_unmerged_and_never_change_approval(self):
        with mock.patch.object(md, '_utc_now_iso', return_value='2026-01-03T00:00:00Z'):
            self.merge()
        rows = mb.decision_events(self.root, 'B')
        self.assertEqual('merged', rows[-1]['decision']); self.assertEqual('node', rows[-1]['kind'])
        self.assertEqual(md._canonical_digest(self.graph()['nodes'][1]['merge']), rows[-1]['digest'])
        def ids(at):
            result = mg.build_graph(self.root, as_of=at)
            self.assertTrue(result['ok'], result)
            return {n['id'] for n in result['nodes'] if n['kind']=='lesson' and n.get('recordType') != 'citation'}
        self.assertIn('B', ids('2026-01-02T12:00:00Z'))
        self.assertNotIn('B', ids('2026-01-03T12:00:00Z'))
        # Current lapse doesn't rewrite the advisory historical decision.
        g = self.graph(); g['nodes'][0]['edges'][0]['approval']['decision']='rejected'
        g['edges'] = [copy.deepcopy(e) for n in g['nodes'] for e in n['edges']]; self.save(g)
        self.assertIn('B', [i['id'] for i in mb.dream_items(self.root)['items']])
        self.assertNotIn('B', ids('2026-01-03T12:00:00Z'))
        with mock.patch.object(md, '_utc_now_iso', return_value='2026-01-04T00:00:00Z'):
            self.assertTrue(mb.learn_unmerge(self.root, ['B'], approver='R')['ok'])
        self.assertIn('B', ids('2026-01-04T12:00:00Z'))
        self.assertNotIn('B', ids('2026-01-03T12:00:00Z'))
        rows = mb.decision_events(self.root, 'B')
        self.assertEqual(['approved','merged','unmerged'], [r['decision'] for r in rows])
        self.assertEqual(rows[-2]['digest'], rows[-1]['digest'])

    def hook_outputs(self, tag, file='src.py'):
        st = dict(jit=True, guard='deny', capsule=True, dream=False, tokens=2000)
        start = dict(session_id=tag, cwd=str(self.root), source='startup', hook_event_name='SessionStart')
        startup = mh._session_start_event(self.root, start, st)
        payload = dict(start, hook_event_name='PreToolUse', tool_name='Edit', permission_mode='default',
                       tool_input={'file_path':str(self.root / file)}, tool_use_id='test-edit')
        guard = mh._pre_tool_use(self.root, payload, st)
        jit = mh._pre_tool_use(self.root, payload, st)
        post = mh._post_tool_use_event(self.root, dict(payload,hook_event_name='PostToolUse'), st)
        compact = mh._pre_compact(self.root, payload, st)
        resume = mh._session_start_event(self.root, dict(start,source='resume'), st)
        after = mh._session_start_event(self.root, dict(start,source='compact'), st)
        return dict(startup=startup, guard=guard, jit=jit, post=post, compact=compact, resume=resume, after=after)

    def test_every_reader_before_holding_lapsed_unmerged(self):
        def check(tag, expected):
            reader = mb.dream_items(self.root)
            self.assertEqual(expected, {i['id'] for i in reader['items']}, tag)
            recall = mb.unified_recall(self.root, budget=10000)
            self.assertEqual(expected, {i['id'] for i in recall['sections']['dream']}, tag)
            hand = mw.build_handover(self.root, write=True, budget=10000)
            self.assertTrue(hand['ok'], hand); self.assertTrue(mw.verify_handover_checksum(hand))
            self.assertEqual(expected, {i['id'] for i in hand['approvedLessons']}, tag)
            markdown = mw.build_handover(self.root, budget=10000, output_format='markdown')['formatted']
            self.assertTrue(mw.verify_handover_markdown(markdown))
            changed = mb.changed_check(self.root, files=['src.py'])
            self.assertEqual(expected, {i['id'] for i in changed['approvedLessons']}, tag)
            graph = mg.build_graph(self.root)
            self.assertTrue(graph['ok'], graph)
            self.assertEqual(len(expected), graph['counts']['lessonItems'], tag)
            self.assertEqual(expected, {i['id'] for i in graph['nodes'] if i['kind']=='lesson' and i.get('recordType')!='citation'}, tag)
            neighbor = mg.neighbors(self.root, 'src.py', budget=10000)
            self.assertEqual(expected, {i['id'] for group in neighbor['groups'].values() for i in group if i['kind']=='lesson'}, tag)
            for ident in ('A','B','C'):
                by_id = mg.neighbors(self.root, ident, budget=10000)
                shown = {i['id'] for group in by_id.get('groups',{}).values() for i in group if i.get('kind')=='lesson'}
                self.assertEqual(ident in expected, ident in shown, (tag, ident, by_id))
            caps = json.loads((self.root / hb.CAPSULES_FILE).read_text())
            bridge = json.loads((self.root / hb.HANDOVER_FILE).read_text())
            self.assertEqual(expected, {i['id'] for i in caps['capsules']}, tag)
            self.assertEqual([], bridge['rejectedApproaches'], tag)  # this bridge list contains replacements only
            self.assertTrue(hb.verify(bridge)); self.assertEqual(hb.integrity(caps), caps['integritySha256'])
            self.assertEqual(expected, {i['id'] for i in dc.dream_status(self.root)['items'] if i['tier']=='approved'}, tag)
            self.assertEqual(expected, {i['id'] for i in cortex._load_dream_lineage({'path':str(self.root)})['nodes'] if i['tier']=='approved'}, tag)
            low = md.run_dream_recall(str(self.root / mb.QUEUE_REL), snapshot_path=str(self.root / mb.SNAPSHOT_REL), budget_tokens=10000)
            self.assertEqual(expected, set(low['includedNodeIds']), tag)
            from memlet import memlet_eval as ev
            snapshot, _ = mb._load_snapshot(self.root)
            manifest = dict(run='merge', protocol='retrospective', hints=False, budget=10000,
                            dreamUntil='2026-12-31', dream=[dict(file=f, lines=lines) for f, lines in snapshot.items()])
            contexts = ev.build_contexts(self.root, manifest, {'text':'lesson'})
            for arm in ('flat','graph','handover'):
                for ident in ('A','B','C'):
                    self.assertEqual(ident in expected, 'lesson ' + ident + ' in src.py' in contexts[arm]['text'], (tag,arm,ident))
            rpc = self.rpc('memlet_recall', query='lesson', budget=10000)
            self.assertEqual(expected, {i['id'] for i in rpc['structuredContent']['sections']['dream']}, tag)
            self.assertTrue(mb.promote_approved(self.root)['ok'])
            entries = mb.promoted_rule_entries(self.root)[0]
            self.assertEqual(expected, {i['dreamId'] for i in entries}, tag)
            page = (self.root / mb.PROMOTED_PAGE).read_text()
            hooks = self.hook_outputs(tag)
            self.assertIn('%d approved lesson(s)' % len(expected), json.dumps(hooks['startup']))
            for ident in ('A','B','C'):
                needle = 'lesson ' + ident + ' in src.py'
                for name, text in [('markdown',markdown), ('page',page)] + [(k,str(o)) for k,o in hooks.items() if k!='startup']:
                    self.assertEqual(ident in expected, needle in text, (tag,name,ident,text))
        check('before', {'A','B','C'})
        self.merge(); check('holds', {'A','C'})
        g = self.graph(); g['nodes'][0]['title'] = 'kept lesson reviewed again'
        self.seal(g['nodes'][0]); self.save(g)
        check('lapsed', {'A','B','C'})
        self.assertTrue(mb.learn_unmerge(self.root, ['B'], approver='R')['ok'])
        check('unmerged', {'A','B','C'})

    def test_lost_files_warning_and_guard_once_through_kept_lesson(self):
        # Both BLOCKS lessons reach src.py; only the member reaches other.py.
        self.assertTrue(mb.learn_paths(self.root, ['B'], approver='R', paths=['src.py','other.py'])['ok'])
        result = self.merge()
        self.assertEqual({'B':['other.py']}, result['lostFiles'])
        command = 'memlet learn --paths A --by Reviewer --path other.py' + mb._learn_repo_option(self.root)
        self.assertEqual(command, result['pathCommands']['A']); self.assertIn(command, result['formatted'])
        self.assertEqual([], mg.neighbors(self.root, 'other.py')['groups']['blocks'])
        out = self.hook_outputs('guard')
        guard = out['guard']['hookSpecificOutput']
        self.assertEqual('deny', guard['permissionDecision'])
        self.assertIn('lesson A in src.py', guard['permissionDecisionReason'])
        self.assertNotIn('lesson B in src.py', guard['permissionDecisionReason'])
        self.assertNotIn('permissionDecision', (out['jit'] or {}).get('hookSpecificOutput',{}))
        other = self.hook_outputs('lost', 'other.py')
        self.assertNotIn('permissionDecision', (other['guard'] or {}).get('hookSpecificOutput',{}))

    def test_cycle_and_invalid_record_fail_open_without_mutating_input(self):
        self.merge(); g = self.graph()
        a,b,_ = g['nodes']; a['merge'] = dict(b['merge'], into='B',memberDigest=md.node_approval_digest(a),keepDigest=md.node_approval_digest(b))
        self.save(g); before = (self.root / mb.QUEUE_REL).read_bytes()
        self.assertEqual('merge-cycle', self.state()['reason'])
        self.assertEqual(before, (self.root / mb.QUEUE_REL).read_bytes())
        g['nodes'][1]['merge'] = {'into':'A'}; self.save(g)
        self.assertEqual('invalid-record', self.state()['reason'])

    def test_holds_predicates_are_mutation_sensitive(self):
        import ast
        import inspect
        source = inspect.getsource(mb.merge_state)
        cases = {
            'member-changed':'test_lapse_member_digest_and_recovery',
            'keep-missing':'test_lapse_keep_missing_and_recovery',
            'keep-changed':'test_lapse_keep_digest_and_recovery',
            'keep-rejected':'test_lapse_keep_rejected_or_revoked_and_recovery',
            'keep-not-approved':'test_lapse_approved_keep_falls_to_pending_and_recovery',
            'keep-is-member':'test_lapse_keep_became_member_and_recovery',
        }
        for reason, method in cases.items():
            with self.subTest(mutant=reason):
                tree = ast.parse(source)
                changed = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.If) and any(isinstance(stmt, ast.Assign)
                            and isinstance(stmt.value, ast.Constant) and stmt.value.value == reason for stmt in node.body):
                        node.test = ast.Constant(False); changed.append(node)
                    if reason == 'keep-is-member' and isinstance(node, ast.IfExp):
                        if any(isinstance(part, ast.Constant) and part.value == reason for part in ast.walk(node.body)):
                            node.test = ast.Constant(False); changed.append(node)
                self.assertEqual(1, len(changed), reason)
                namespace = dict(mb.__dict__)
                exec(compile(ast.fix_missing_locations(tree), '<merge mutant>', 'exec'), namespace)
                result = unittest.TestResult()
                with mock.patch.object(mb, 'merge_state', namespace['merge_state']):
                    MergeTests(method).run(result)
                self.assertEqual(1, result.testsRun)
                self.assertTrue(result.failures or result.errors, reason + ' survived')


    def test_relations_hold_after_member_approval_and_keep_edge_changes(self):
        for mode in ('approve-member', 'reject-edge', 'drift-edge'):
            with self.subTest(mode=mode):
                g = copy.deepcopy(self.original)
                a, b, c = g['nodes']
                a['edges'][0]['edgeType'] = 'RESOLVES'
                self.seal(a)
                if mode == 'approve-member':
                    b.pop('approval'); b['edges'][0].pop('approval')
                else:
                    lines = self.lines + ['2026-01-01T00:09:00Z [user] Never retry separate rule in src.py.']
                    self.write(mb.SNAPSHOT_REL + '/s.log', '\n'.join(lines) + '\n')
                    pin = md.resolve_pin({'s.log': lines}, 's.log:4')
                    edge = copy.deepcopy(a['edges'][0])
                    edge.update(edgeId='E-A2', edgeType='BLOCKS', srcPin=pin,
                                srcTimestamp=md.extract_line_timestamp(pin['quoteText']))
                    a['edges'].append(edge); self.seal(a)
                g['edges'] = [copy.deepcopy(e) for n in g['nodes'] for e in n['edges']]; self.save(g)
                self.merge()
                if mode == 'approve-member':
                    self.assertTrue(mb.learn_approve(self.root, ['B'], approver='R')['ok'])
                elif mode == 'reject-edge':
                    self.assertTrue(mb.learn_reject(self.root, ['E-A2'], approver='R')['ok'])
                else:
                    self.write(mb.SNAPSHOT_REL + '/s.log', '\n'.join(self.lines + ['changed edge line']) + '\n')
                self.assertEqual('relation-types', self.state()['reason'])
                self.assertEqual('approved', next(i['tier'] for i in mb.dream_items(self.root)['items'] if i['id']=='B'))
                guard = self.hook_outputs('relations-' + mode)['guard']['hookSpecificOutput']
                self.assertEqual('deny', guard['permissionDecision'])
                self.assertIn('lesson B in src.py', guard['permissionDecisionReason'])
                self.write(mb.SNAPSHOT_REL + '/s.log', '\n'.join(self.lines) + '\n')

    def test_printed_paths_command_is_one_union_and_merge_survives_both_orders(self):
        import shlex
        self.write('third.py', 'pass\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'third.py'], check=True, capture_output=True)
        for ident, paths in [('A',['src.py']), ('B',['other.py']), ('C',['third.py'])]:
            self.assertTrue(mb.learn_paths(self.root, [ident], approver='R', paths=paths)['ok'])
        before = self.graph()
        for order in ('merge-first', 'paths-first'):
            self.save(copy.deepcopy(before))
            result = self.merge(same=('B','C'))
            self.assertEqual(['A'], list(result['pathCommands']))
            command = result['pathCommands']['A']
            self.assertEqual(1, result['formatted'].count('memlet learn --paths'))
            if order == 'paths-first':
                mb.learn_unmerge(self.root, ['B','C'], approver='R')
            run = self.cli(*shlex.split(command)[1:], '--repo', str(self.root))
            self.assertEqual(0, run.returncode, run.stderr + run.stdout)
            if order == 'paths-first': self.merge(same=('B','C'))
            self.assertEqual({'A':['B','C']}, mb.merge_view(self.root)['members'])
            self.assertEqual(['src.py','other.py','third.py'], self.graph()['nodes'][0]['approvedPaths'])
            self.assertEqual(['A'], [i['id'] for i in mb.dream_items(self.root, files=['third.py'])['items']])
            # A member attachment is also outside merge identity, while approvals remain sealed.
            before_paths = (self.root / mb.QUEUE_REL).read_bytes()
            self.assertFalse(mb.learn_paths(self.root, ['B'], approver='R', paths=['src.py'])['ok'])
            self.assertEqual(before_paths, (self.root / mb.QUEUE_REL).read_bytes())
            self.assertEqual('merged', self.state()['state'])
            old=json.loads(self.run_code(self.baseline(), "from memlet import memlet_bridge as b, memlet_dream as d; from pathlib import Path; import sys,json; print(json.dumps([d.approval_state(n,'node') for n in b._load_queue(Path(sys.argv[1]))[0]['nodes']]))"))
            self.assertEqual(['approved']*3, old)

    def test_path_guidance_limit_and_hint_member(self):
        for i in range(9): self.write('file%d.py'%i, 'pass\n')
        subprocess.run(['git','-C',str(self.root),'add','.'],check=True,capture_output=True)
        self.assertTrue(mb.learn_paths(self.root,['A'],approver='R',paths=['file0.py'])['ok'])
        self.assertTrue(mb.learn_paths(self.root,['B'],approver='R',paths=['file%d.py'%i for i in range(1,9)])['ok'])
        result=self.merge()
        self.assertEqual({},result['pathCommands']); self.assertIn('8 paths', result['formatted'])
        # Real hint receipts, then literal-only lost reach for a hint member.
        g=copy.deepcopy(self.original)
        lines=[self.lines[0],self.lines[1].replace('src.py','other.py'),self.lines[2]]
        self.write(mb.SNAPSHOT_REL+'/s.log','\n'.join(lines)+'\n')
        b=g['nodes'][1]; pin=md.resolve_pin({'s.log':lines},'s.log:2'); b.update(primaryPin=pin,allPins=[pin])
        b['edges'][0]['srcPin']=pin
        for n in g['nodes']:
            n.pop('approval',None)
            for e in n['edges']: e.pop('approval',None)
        b['proposers']=[dc.seal_receipt(dict(origin='vendor-cli',vendor='claude',model='haiku',isolation='tool-free',at=STAMP,runId='a'*24,digest=dc.trust_digest(b)))]
        b['verification']=[dc.seal_receipt(dict(origin='vendor-cli',vendor='codex',model='gpt-5',isolation='tool-free',at=STAMP,runId='a'*24,digest=dc.trust_digest(b),verdict='SUPPORTED'))]
        g['edges']=[copy.deepcopy(e) for n in g['nodes'] for e in n['edges']];self.save(g)
        result=self.merge()
        self.assertEqual({'B':['other.py']},result['lostFiles'])
        self.assertEqual({},result['pathCommands']);self.assertIn('once the kept lesson is approved',result['formatted'])

    def test_replacement_flags_and_rejected_member_lapse(self):
        for field,value in [('statusKind','superseded_rule'),('category','superseded_directive'),('verdict','ALREADY_SUPERSEDED'),('supersededByPin',self.original['nodes'][2]['primaryPin'])]:
            for index in (0,1):
                with self.subTest(field=field,index=index):
                    g=copy.deepcopy(self.original);g['nodes'][index][field]=value;self.seal(g['nodes'][index]);self.save(g)
                    self.assertIn('supersedes',mb.merge_blockers(self.root,'A',['B'])['blockers']['B'])
                    # Matching merge digests do not override a replacement's status.
                    a,b,_=g['nodes'];b['merge']=dict(schema=v.MERGE_SCHEMA,into='A',by='R',at=STAMP,
                        memberDigest=md.merge_approval_digest(b),keepDigest=md.merge_approval_digest(a));self.save(g)
                    self.assertNotEqual('merged',self.state()['state'])
        self.save(copy.deepcopy(self.original));self.merge()
        self.assertTrue(mb.learn_reject(self.root,['B'],approver='R')['ok'])
        self.assertEqual('member-rejected',self.state()['reason'])
        self.assertNotIn('mergedFrom',mb.dream_items(self.root)['items'][0])
        self.assertEqual('B',mb.learn_pending(self.root)['lapsedMerges'][0]['id'])

    def test_unverified_reads_never_apply_merges_and_flat_verified_does(self):
        self.merge();g=self.graph();nodes={n['id']:n for n in g['nodes']}
        self.assertNotEqual('merged',mb.merge_state(nodes['B'],nodes)['state'])
        for missing in ('A','B'):
            snap=mb._load_snapshot(self.root)[0]
            ns=mb._merge_nodes(g,mb.dream_tiers(g,snapshot=snap,config={}),snap)
            ns[missing].pop('tier')
            self.assertNotEqual('merged',mb.merge_state(ns['B'],ns)['state'])
        with mock.patch.object(mb, '_load_snapshot', return_value=(None, 'snapshot unavailable')):
            self.assertNotEqual('merged', self.state()['state'])
        no_merge=copy.deepcopy(g);no_merge['nodes'][1].pop('merge')
        self.assertEqual(md.recall_subgraph(no_merge,allow_unverified=True),md.recall_subgraph(g,allow_unverified=True))
        snap=mb._load_snapshot(self.root)[0]
        self.assertNotIn('[B]',md.format_flat_memory(g,snapshot_files=snap))
        self.assertIn('[B]',md.format_flat_memory(g,allow_unverified=True))

    def test_cycle_only_lapses_cycle_members(self):
        g=self.graph();a,b,c=g['nodes']
        for member,keep in ((a,c),(c,a),(b,a)):
            member['merge']=dict(schema=v.MERGE_SCHEMA,into=keep['id'],by='R',at=STAMP,
                memberDigest=md.node_approval_digest(member),keepDigest=md.node_approval_digest(keep))
        self.save(g);states=mb.merge_view(self.root)['states']
        self.assertEqual('merge-cycle',states['A']['reason']);self.assertEqual('merge-cycle',states['C']['reason'])
        self.assertEqual('merged',states['B']['state'])

    def test_duplicate_ids_fail_open_and_invalid_records_have_fixed_lines(self):
        with mock.patch.object(md, '_utc_now_iso', return_value='2026-01-03T00:00:00Z'): self.merge()
        g=self.graph();g['nodes'].append(copy.deepcopy(g['nodes'][1]));g['nodes'][-1].pop('merge')
        # Deliberate hand edit; the DB writer correctly refuses duplicate ids.
        (self.root/mb.QUEUE_REL).write_text(json.dumps(g))
        self.assertEqual('duplicate-id',self.state()['reason'])
        self.assertEqual(2,sum(i['id']=='B' for i in mb.dream_items(self.root)['items']))
        historical=mg.build_graph(self.root, as_of='2026-01-04T00:00:00Z')
        self.assertIn('B',[n['id'] for n in historical['nodes']])
        for value in (None,12,[],{}):
            g=copy.deepcopy(self.original);g['nodes'][1]['merge']={'into':value}
            (self.root/mb.QUEUE_REL).write_text(json.dumps(g))
            result=mb.learn_pending(self.root,_record_review=False)
            self.assertIn('invalid merge record',result['formatted']);self.assertNotIn('`None`',result['formatted'])

    def test_pending_hidden_counts_tiers_edges_and_citation_notice(self):
        self.merge();result=mb.learn_pending(self.root,_record_review=False)
        self.assertEqual(2,result['counts']['approvedNodes']);self.assertEqual(1,result['counts']['hiddenMergedNodes'])
        self.assertEqual(2,result['counts']['approvedEdges']);self.assertIn('`B` [approved]',result['formatted'])
        self.write(mb.SNAPSHOT_REL+'/s.log','\n'.join([self.lines[0],'changed member line',self.lines[2]])+'\n')
        result=mb.learn_pending(self.root,_record_review=False)
        member=result['mergedLessons'][0]['members'][0]
        self.assertEqual('pending',member['tier']);self.assertFalse(member['pinsVerify'])
        self.assertIn('`B` [pending]',result['formatted']);self.assertIn(member['citationReason'],result['formatted'])
        self.write(mb.SNAPSHOT_REL+'/s.log','\n'.join(self.lines)+'\n')
        g=self.graph();edge=copy.deepcopy(g['edges'][2]);edge.update(edgeId='INTO-B',toId='B');edge.pop('approval');g['edges'].append(edge);self.save(g)
        self.assertNotIn('INTO-B',[e['edgeId'] for e in mb.learn_pending(self.root,_record_review=False)['pendingEdges']])

    def test_retry_repairs_missing_decision_and_then_is_unchanged(self):
        for operation in ('merge','unmerge'):
            if operation=='unmerge' and self.state()['state']!='merged': self.merge()
            fn=lambda: (mb.learn_merge(self.root,'A',['B'],approver='R') if operation=='merge' else mb.learn_unmerge(self.root,['B'],approver='R'))
            with mock.patch.object(mb,'_decision_event_write',side_effect=OSError('injected ledger unavailable')):
                first=fn()
            self.assertFalse(first['ok']);self.assertEqual(['B'],first['applied'])
            second=fn();self.assertTrue(second['ok']);self.assertEqual(['B'],second['repaired'])
            self.assertIn('missing decision log row',second['formatted'])
            self.assertEqual(operation+'d',mb.decision_events(self.root,'B')[-1]['decision'])
            before=(self.root/mb.QUEUE_REL).read_bytes();events=list(mb.decision_events(self.root))
            third=fn();self.assertEqual(['B'],third['unchanged']);self.assertEqual([],third['applied'])
            self.assertEqual(events,mb.decision_events(self.root));self.assertEqual(before,(self.root/mb.QUEUE_REL).read_bytes())

    def test_history_rejection_known_member_order_future_and_bad_digest(self):
        with mock.patch.object(md,'_utc_now_iso',return_value='2026-01-03T00:00:00Z'): self.merge()
        got=mg.neighbors(self.root,'B',as_of='2026-01-03T12:00:00Z')
        self.assertTrue(got['ok'],got);self.assertIn('Merged into A at that time.',got['notices'])
        with mock.patch.object(md,'_utc_now_iso',return_value='2026-01-04T00:00:00Z'):
            self.assertTrue(mb.learn_reject(self.root,['A'],approver='R')['ok'])
        graph=mg.build_graph(self.root,as_of='2026-01-05T00:00:00Z')
        self.assertIn('B',[n['id'] for n in graph['nodes']])
        row=mb.decision_events(self.root,'B')[-1]
        mb._decision_event_write(self.root,[('2026-01-01T00:00:00Z','B','node','unmerged','R',row['digest'],'[]')])
        self.assertIn(v.GRAPH_ORDER_NOTICE,mg.build_graph(self.root)['notices'])
        mb._decision_event_write(self.root,[('2999-01-01T00:00:00Z','B','node','merged','R',row['digest'],'[]')])
        self.assertIn(v.GRAPH_FUTURE_NOTICE,mg.build_graph(self.root)['notices'])
        mb._decision_event_write(self.root,[('2026-01-06T00:00:00Z','C','node','merged','R','bad','[]')])
        history=mg._History(self.root,graph=self.graph())
        self.assertNotIn('C',history.merges);self.assertIn(v.GRAPH_CORRUPT_HISTORY_NOTICE,history.notices)
        # A syntactically valid digest without reconstructible target remains unknown.
        mb._decision_event_write(self.root,[('2026-01-07T00:00:00Z','C','node','merged','R','sha256:'+'a'*64,'[]')])
        result=mg.build_graph(self.root,as_of='2026-01-08T00:00:00Z')
        self.assertIn('C',[n['id'] for n in result['nodes']]);self.assertTrue(any('unknown' in n for n in result['notices']))

    def test_surviving_reader_and_record_mutants(self):
        self.merge()
        page=(self.root/mb.PROMOTED_PAGE).read_text()
        self.assertNotIn('lesson B in src.py',page)  # command itself refreshed it
        self.assertNotIn('B',[n['id'] for n in cortex._load_dream_lineage({'path':str(self.root)})['nodes']])
        mb.learn_unmerge(self.root,['B'],approver='R')
        self.assertIn('lesson B in src.py',(self.root/mb.PROMOTED_PAGE).read_text())
        # Leave the materialized page stale so its reader's own filter is exercised.
        g=self.graph();a,b,_=g['nodes'];b['merge']=dict(schema=v.MERGE_SCHEMA,into='A',by='R',at=STAMP,
            memberDigest=md.node_approval_digest(b),keepDigest=md.node_approval_digest(a));self.save(g)
        self.assertNotIn('B',[n['dreamId'] for n in mb.promoted_rule_entries(self.root)[0]])
        for field,value in [('by',''),('by',None),('at','bad'),('at',None)]:
            changed=copy.deepcopy(g);changed['nodes'][1]['merge'][field]=value;self.save(changed)
            self.assertEqual('invalid-record',self.state()['reason'])
        snap=mb._load_snapshot(self.root)[0]
        ns=mb._merge_nodes(g,mb.dream_tiers(g,snapshot=snap,config={}),snap)
        ns['A']['verdict']=md.REJECTED
        self.assertEqual('keep-rejected',mb.merge_state(ns['B'],ns)['reason'])
        g['nodes'][0]['verdict']=md.REJECTED;self.save(g)
        self.assertEqual('keep-rejected',self.state()['reason'])

    def test_subset_counts_only_approved_verified_relations(self):
        for status in ('pending','rejected'):
            for owner in (0,1):
                g=copy.deepcopy(self.original)
                for n in g['nodes']:
                    n['edges'][0]['edgeType']='RESOLVES';self.seal(n)
                edge=copy.deepcopy(g['nodes'][owner]['edges'][0]);edge.update(edgeId='EXTRA',edgeType='BLOCKS')
                if status=='pending':edge.pop('approval')
                else:edge['approval']['decision']='rejected'
                g['nodes'][owner]['edges'].append(edge)
                if owner==0:
                    g['nodes'][1]['edges'][0]['edgeType']='BLOCKS';self.seal(g['nodes'][1])
                g['edges']=[copy.deepcopy(e) for n in g['nodes'] for e in n['edges']];self.save(g)
                result=mb.merge_blockers(self.root,'A',['B'])
                self.assertTrue(result['ok'])
                self.assertEqual(owner==0,'relation-types' in result['blockers'].get('B',[]))

    def test_context_cost_2000_lessons_40_checks_and_promoted_queue_once(self):
        g=self.graph();source=g['nodes'][0];g['nodes']=[]
        for index in range(2000):
            n=copy.deepcopy(source);n['id']='N%d'%index
            n['edges'][0].update(edgeId='E%d'%index,fromId=n['id']);self.seal(n);g['nodes'].append(n)
        g['edges']=[n['edges'][0] for n in g['nodes']]
        snap=mb._load_snapshot(self.root)[0]
        context=mb.lesson_path_context(self.root)
        context.update(graph=g,snapshot=snap,config={})
        with mock.patch.object(mb,'merge_state',wraps=mb.merge_state) as state, mock.patch.object(mb,'dream_tiers',wraps=mb.dream_tiers) as tiers, mock.patch.object(mb,'lesson_path_context',side_effect=AssertionError('reuse passed attachment context')):
            for _ in range(40): self.assertTrue(mb.merge_blockers(self.root,'N0',['N1'],context=context)['ok'])
        self.assertEqual(2000,state.call_count);self.assertEqual(1,tiers.call_count)
        self.merge()
        with mock.patch.object(mb,'_load_queue',wraps=mb._load_queue) as load:
            mb.promoted_rule_entries(self.root)
        self.assertEqual(1,load.call_count)

    def test_920_all_learn_prefix_results_and_error_text(self):
        script=r'''
import argparse,contextlib,io,json,sys
import memory_cli
parser=memory_cli._build_parser()
learn=next(a for a in parser._actions if isinstance(a,argparse._SubParsersAction)).choices['learn']
names={o for a in learn._actions for o in a.option_strings if o.startswith('--') and o not in ('--merge','--unmerge','--same')}
prefixes=sorted({o[:i] for o in names for i in range(3,len(o)+1)})
result={}
for prefix in prefixes:
    for argv in (['learn',prefix],['learn',prefix,'X'],['learn','--pending',prefix,'X'],['learn','--approve','A','--by','R',prefix,'X']):
        err=io.StringIO()
        with contextlib.redirect_stderr(err),contextlib.redirect_stdout(io.StringIO()):
            try:
                ns=vars(parser.parse_args(argv));out=['ok',{k:v for k,v in ns.items() if k not in ('merge','unmerge','same') and not callable(v)}]
            except SystemExit as exc:out=['exit',exc.code,err.getvalue()]
        result[' '.join(argv)]=out
print(json.dumps(result,sort_keys=True,default=str))
'''
        old=json.loads(self.run_code(self.baseline(),script));new=json.loads(self.run_code(Path(__file__).parent,script))
        for argv, result in old.items():
            with self.subTest(argv=argv):self.assertEqual(result,new[argv])


    def test_baseline_unavailable_skips_clearly(self):
        with mock.patch('subprocess.run', return_value=subprocess.CompletedProcess(['git'], 128, b'', b'unknown revision')):
            with self.assertRaisesRegex(unittest.SkipTest, 'f7b224f.*export or shallow clone'):
                self.baseline()

    def baseline(self):
        target = self.base / 'baseline'
        if not target.exists():
            target.mkdir()
            archive = subprocess.run(['git', 'archive', 'f7b224f'], cwd=Path(__file__).parent,
                                     capture_output=True)
            if archive.returncode:
                self.skipTest('9.2.0 compatibility requires readable git commit f7b224f (not available in this export or shallow clone)')
            archive = archive.stdout
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                # Only this trusted, pinned local commit is extracted.
                for entry in bundle.getmembers():
                    self.assertNotIn('..', Path(entry.name).parts)
                    self.assertFalse(Path(entry.name).is_absolute())
                bundle.extractall(target, **({'filter':'data'} if hasattr(tarfile, 'data_filter') else {}))
        return target

    def run_code(self, code, script, stdin=''):
        env = dict(os.environ, PYTHONPATH=str(code))
        result = subprocess.run([self.python, '-c', script, str(self.root), str(code)],
                                cwd=self.base, env=env, input=stdin, capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_920_verifies_every_approval_and_shows_merged_members_separately(self):
        self.merge(same=('B','C'))
        script = """
import json, sys
from pathlib import Path
from memlet import memlet_bridge as mb, memlet_dream as md
root = Path(sys.argv[1])
graph = mb._load_queue(root)[0]
print(json.dumps(dict(nodes=[md.approval_state(n,'node') for n in graph['nodes']],
                     edges=[md.approval_state(e,'edge') for n in graph['nodes'] for e in n['edges']],
                     ids=[n['id'] for n in mb.dream_items(root)['items']])))
"""
        old = json.loads(self.run_code(self.baseline(), script))
        self.assertEqual(['approved'] * 3, old['nodes'])
        self.assertEqual(['approved'] * 3, old['edges'])
        self.assertEqual(['A','B','C'], old['ids'])
        self.assertEqual(['A'], [i['id'] for i in mb.dream_items(self.root)['items']])

    def test_920_fixture_byte_identical_library_cli_mcp_hooks_bridges(self):
        import shutil
        baseline = self.baseline()
        graph = copy.deepcopy(self.original)
        for n in graph['nodes']:
            n.pop('approval')
            for e in n['edges']: e.pop('approval')
        graph['edges'] = [copy.deepcopy(e) for n in graph['nodes'] for e in n['edges']]
        # The pinned old runtime writes the fixture and its approvals itself.
        shutil.rmtree(self.root / '.memlet')
        setup = """
import json, sys
from pathlib import Path
from memlet import memlet_bridge as mb, memlet_dream as md, memlet_wiki as mw
root = Path(sys.argv[1]); graph, lines = json.loads(sys.stdin.read())
md._utc_now_iso = mw._utc_now = lambda: '2026-01-02T00:00:00Z'
assert mw.init_project(root)['ok']
snap = root / mb.SNAPSHOT_REL; snap.mkdir(parents=True,exist_ok=True)
(snap / 's.log').write_text('\\n'.join(lines) + '\\n')
md._commit_queue_and_db(root / mb.QUEUE_REL, json.dumps(graph), root / mb.DB_REL, graph)
assert mb.learn_approve(root, ['A','B','C'], approver='Reviewer')['ok']
"""
        self.run_code(baseline, setup, json.dumps([graph,self.lines]))
        script = """
import contextlib, io, json, shutil, sys
from pathlib import Path
from memlet import memlet_bridge as mb, memlet_dream as md, memlet_wiki as mw
from memlet import memlet_graph as mg, memlet_hooks as mh, handover_bridge as hb
from memlet import memlet_cortex as cortex, memlet_dreamcycle as dc, vendor_cli as vc
import memlet_mcp, memory_cli
root = Path(sys.argv[1]); code = Path(sys.argv[2])
md._utc_now_iso = mw._utc_now = lambda: '2026-01-02T00:00:00Z'
vc.run_vendor = lambda *a, **k: (_ for _ in ()).throw(AssertionError('no vendor'))
shutil.rmtree(root / mh.SESSIONS_REL, ignore_errors=True)
out = {}
out['library'] = [mb.dream_items(root), mb.learn_pending(root,_record_review=False),
                  mb.unified_recall(root,'lesson',budget=10000), mb.changed_check(root,files=['src.py']),
                  mw.build_handover(root,budget=10000,write=True), mg.build_graph(root),
                  mg.neighbors(root,'src.py',budget=10000), dc.dream_status(root),
                  cortex._load_dream_lineage({'path':str(root)}),
                  md.run_dream_recall(str(root/mb.QUEUE_REL),snapshot_path=str(root/mb.SNAPSHOT_REL))]
commands = [['recall','lesson','--repo',str(root)], ['learn','--pending','--repo',str(root)],
            ['check',str(root),'--changed'],
            ['handover',str(root)], ['handover',str(root),'--format','markdown'],
            ['admin','graph','show','src.py','--repo',str(root)],
            ['admin','graph','stats','--repo',str(root)],
            ['dream',str(root),'--status']]
out['cli'] = []
for args in commands:
    sys.argv = ['memlet'] + args
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try: status = memory_cli.cli()
        except SystemExit as exc: status = exc.code
    assert status == 0, (args,status,stdout.getvalue(),stderr.getvalue())
    out['cli'].append([status,stdout.getvalue(),stderr.getvalue()])
server = memlet_mcp.MemletMCPServer(root=root.parent/'store', default_task='i1')
out['tools'] = server.handle_rpc({'jsonrpc':'2.0','id':1,'method':'tools/list'})
out['mcp'] = []
for name,args in [('memlet_recall',{'query':'lesson','budget':10000}),('memlet_handover',{'budget':10000}),
                  ('memlet_check',{'files':['src.py']})]:
    out['mcp'].append(server.handle_rpc({'jsonrpc':'2.0','id':1,'method':'tools/call',
        'params':{'name':name,'arguments':dict(repo_path=str(root),**args)}}))
st = dict(jit=True,guard='deny',capsule=True,dream=False,tokens=2000)
start = dict(session_id='compat',cwd=str(root),source='startup',hook_event_name='SessionStart')
payload = dict(start,hook_event_name='PreToolUse',tool_name='Edit',permission_mode='default',
               tool_input={'file_path':str(root/'src.py')},tool_use_id='edit')
out['hooks'] = [mh._session_start_event(root,start,st), mh._pre_tool_use(root,payload,st),
                mh._pre_tool_use(root,payload,st), mh._post_tool_use_event(root,payload,st),
                str(mh._pre_compact(root,payload,st)),
                mh._session_start_event(root,dict(start,source='resume'),st),
                mh._session_start_event(root,dict(start,source='compact'),st)]
out['bridges'] = [(root / p).read_text() for p in (hb.HANDOVER_FILE,hb.CAPSULES_FILE)]
print(json.dumps(out,sort_keys=True,ensure_ascii=False))
"""
        before = (self.root / mb.QUEUE_REL).read_bytes()
        saved = self.base / 'fixture-memlet'
        shutil.copytree(self.root / '.memlet', saved)
        old = self.run_code(baseline, script)
        shutil.rmtree(self.root / '.memlet')
        shutil.copytree(saved, self.root / '.memlet')
        new = self.run_code(Path(__file__).parent, script)
        old_parts, new_parts = json.loads(old), json.loads(new)
        self.assertEqual(25, len(old_parts['tools']['result']['tools']))
        for key in old_parts:
            self.assertEqual(old_parts[key], new_parts[key], key)
        self.assertEqual(old.encode(), new.encode())
        self.assertEqual(before, (self.root / mb.QUEUE_REL).read_bytes())


if __name__ == '__main__':
    unittest.main()
