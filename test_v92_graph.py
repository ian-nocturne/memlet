"""E1 graph contract, hermetic fixtures; no model, network or user-store access."""
import copy
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import unittest
from unittest import mock

import test_v91_integration as fixtures
import memlet_mcp
from memlet import memlet_graph as graph, memlet_bridge as mb, memlet_dream as md
from memlet import memlet_wiki as mw, memlet_anchors as ma, memlet_dreamcycle as dc
from memlet import vendor_cli, cli_plugins, memlet_eval
from memlet.vocabulary import HINT_LABEL, HINT_HELP, GRAPH_WHY


class GraphTests(fixtures.Scratch):
    fixture = fixtures.ApprovedReadersIntegration.fixture
    save_graph = fixtures.ApprovedReadersIntegration.save_graph
    kinds = fixtures.ApprovedReadersIntegration.kinds

    def tracked(self, *paths):
        subprocess.run(['git', 'add', '--'] + list(paths), cwd=self.root, check=True, capture_output=True)

    def result(self, root=None, target='auth.py', **kw):
        result = graph.neighbors(root or self.root, target, **kw)
        self.assertTrue(result['ok'], result)
        self.assertLessEqual(len(result['formatted']), 8000)
        self.assertLessEqual(result['tokens'], kw.get('budget', 600))
        return result

    def test_nodes_edges_provenance_and_no_containment_relation(self):
        self.anchors()
        self.tracked('auth.py', 'AGENTS.md')
        self.write('other.py', 'def other():\n    return 1\n')
        self.write('CLAUDE.md', '- Plain unanchored rule.\n- Unpinned `other.py::other`.\n')
        built = graph.build_graph(self.root)
        self.assertTrue(built['ok'], built)
        self.assertEqual(set(graph.NODE_KINDS), set(built['counts']['nodes']))
        self.assertEqual(1, built['counts']['unanchoredRules'])
        nodes = {n['id']: n for n in built['nodes']}
        self.assertEqual(4, built['counts']['nodes']['rule'])
        self.assertTrue(all(e['type'] in md.EDGE_TYPES for e in built['edges']))
        suggested = [e for e in built['edges'] if e['basis'] == 'accepted-suggestion']
        self.assertEqual(1, len(suggested))
        self.assertIsNotNone(suggested[0]['validFrom'])
        for edge in built['edges']:
            self.assertIn(edge['src'], nodes)
            self.assertIn(edge['dst'], nodes)
            self.assertEqual('file', nodes[nodes[edge['dst']]['file']]['kind'])
        res = self.result()
        self.assertEqual(2, len(res['groups']['governs']))
        self.assertIn('[suggested anchor]', res['formatted'])
        self.assertTrue(all(i['why'] == 'anchor-in-file' for i in res['groups']['governs']))
        res = self.result(target='other.py')
        self.assertEqual([], res['groups']['governs'])
        self.assertEqual('unpinned', res['groups']['stale'][0]['status'])

    def test_determinism_and_ids_do_not_depend_on_unrelated_items(self):
        self.anchors()
        first = graph.build_graph(self.root)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(graph.build_graph(self.root), sort_keys=True))
        ids = {n['text']: n['id'] for n in first['nodes'] if n['kind'] == 'rule'}
        self.write('CLAUDE.md', '- An unrelated instruction.\n')
        after = graph.build_graph(self.root)
        self.assertEqual(ids, {n['text']: n['id'] for n in after['nodes'] if n.get('text') in ids})

    def test_symbol_is_narrower_than_file_and_id_has_targets(self):
        self.write('auth.py', 'def one():\n    return 1\ndef two():\n    return 2\n')
        self.write('AGENTS.md', '- First `auth.py::one`.\n- Second `auth.py::two`.\n')
        mw.init_project(self.root)
        file_result = self.result()
        self.assertEqual(2, len(file_result['groups']['governs']))
        symbol = self.result(target='auth.py::one')
        self.assertEqual(1, len(symbol['groups']['governs']))
        self.assertEqual('anchor-symbol', symbol['groups']['governs'][0]['why'])
        rule = symbol['groups']['governs'][0]
        by_id = self.result(target=rule['id'])
        self.assertEqual(['auth.py::one'], by_id['groups']['governs'][0]['targets'])

    def test_stale_same_stat_caches_are_not_proof(self):
        self.anchors()
        path = self.root / 'auth.py'
        before = path.stat()
        self.result()
        text = path.read_text().replace('validate_token', 'validate_other')
        self.assertEqual(len(text.encode()), before.st_size)
        path.write_text(text)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        res = self.result()
        self.assertEqual([], res['groups']['governs'])
        self.assertEqual(2, len(res['groups']['stale']))
        self.assertTrue(all(i['status'] == 'stale' for i in res['groups']['stale']))
        path.unlink()
        self.assertEqual(2, len(self.result()['groups']['stale']))

    def test_unsafe_targets_return_only_reason(self):
        self.write('auth.py', fixtures.CODE)
        self.write('.env', 'SECRET=fixture\n')
        (self.root / 'link.py').symlink_to(self.root / 'auth.py')
        (self.root / 'linked').symlink_to(self.root, target_is_directory=True)
        for target in ('../auth.py', str(self.base / 'outside.py'), 'link.py', 'linked/auth.py',
                       '.env', 'auth.py\u200b', 'rule-' + '0' * 24, 'DRM-unknown', 'auth.py::bad/symbol'):
            with self.subTest(target=target):
                res = graph.neighbors(self.root, target)
                self.assertEqual({'ok', 'reason'}, set(res))
                self.assertFalse(res['ok'])

    def test_all_eight_kinds_six_states_agree_with_existing_readers(self):
        # The explicit agreement table is executable: each row is a kind/state,
        # each column is an independently exercised existing reader.
        table = []
        for state in ('approved', 'hint', 'pending', 'rejected', 'edge-rejected', 'vetoed'):
            repo, nodes = self.fixture(state)
            read = mb.dream_items(repo, tiers=('approved', 'hint'))['items']
            built = graph.build_graph(repo)
            self.assertTrue(built['ok'], built)
            projected = {n['id']: n for n in built['nodes'] if n.get('recordType') == 'item'}
            recall = mb.unified_recall(repo, 'deploy release make', budget=32000)
            hand = mw.build_handover(repo, budget=32000)
            by_reader = {'dream_items': {i['id']: i['tier'] for i in read},
                         'graph': {i: n['tier'] for i, n in projected.items()},
                         'recall': {i['id']: 'approved' for i in recall['sections']['dream']},
                         'handover': {i['id']: 'approved' for i in hand['approvedLessons']}}
            for kind, node in nodes.items():
                row = {reader: values.get(node['id']) for reader, values in by_reader.items()}
                table.append((kind, state, row))
                self.assertEqual(row['dream_items'], row['graph'], (kind, state, row))
                self.assertEqual(row['graph'] == 'approved', row['recall'] == 'approved', (kind, state, row))
                self.assertEqual(row['graph'] == 'approved', row['handover'] == 'approved', (kind, state, row))
                if node['id'] in projected:
                    original = next(i for i in read if i['id'] == node['id'])
                    self.assertEqual(original.get('lineRoles'), projected[node['id']].get('lineRoles'))
                    self.assertEqual(original['cites'], projected[node['id']]['cites'])
            result = self.result(repo, 'deploy.py', tiers=('approved', 'hint'), budget=8000)
            for group in ('governs', 'blocks', 'resolves', 'replaced'):
                self.assertTrue(all(i['tier'] == 'approved' for i in result['groups'][group]))
        self.assertEqual(48, len(table))

    def test_hint_label_caveat_tier_and_pending_exclusion(self):
        repo, nodes = self.fixture('hint', ('BLOCKS',))
        # Give this lesson a literally cited tracked path, without changing its
        # receipt-bound evidence: use the actual quoted path as a new filename.
        node = nodes['BLOCKS']
        data = mb._load_queue(repo)[0]
        snapshot = mb._load_snapshot(repo)[0]
        fid = next(iter(snapshot))
        snapshot[fid][0] += ' `deploy.py`'
        (repo / mb.SNAPSHOT_REL / fid).write_text('\n'.join(snapshot[fid]) + '\n')
        changed = data['nodes'][0]
        pin = md.resolve_pin(snapshot, fid + ':1')
        changed['primaryPin'] = pin; changed['allPins'] = [pin]
        changed['edges'][0]['srcPin'] = pin
        data['edges'][0]['srcPin'] = pin
        changed['proposers'] = [dc._receipt(vendor, changed, 'f' * 24,
                                           {'model': 'haiku' if vendor == 'claude' else 'codex'})
                                for vendor in ('claude', 'codex')]
        self.save_graph(repo, data)
        self.assertEqual('hint', mb.dream_items(repo, tiers=('hint',))['items'][0]['tier'])
        default = self.result(repo, 'deploy.py')
        self.assertFalse(any(default['groups'].values()))
        hints = self.result(repo, 'deploy.py', tiers=('approved', 'hint'), budget=2000)
        self.assertEqual(1, len(hints['groups']['hints']))
        self.assertIn(HINT_LABEL, hints['formatted'])
        self.assertIn(HINT_HELP, hints['formatted'])
        self.assertTrue(all(not hints['groups'][k] for k in graph.GROUPS if k != 'hints'))
        self.assertFalse(graph.neighbors(repo, 'deploy.py', tiers=('pending',))['ok'])

    def test_approved_node_does_not_approve_its_pending_relations(self):
        repo, _ = self.fixture('approved', ('BLOCKS',))
        data = mb._load_queue(repo)[0]
        node = data['nodes'][0]
        for relation in ('GOVERNS', 'RESOLVES', 'SUPERSEDES'):
            edge = copy.deepcopy(node['edges'][0])
            edge.update(edgeId='PENDING-' + relation, edgeType=relation,
                        toId='INLINE-OLD-' + relation, edgeVerdict='PENDING')
            edge.pop('approval', None)
            if relation == 'SUPERSEDES':
                edge['targetPin'] = copy.deepcopy(node['primaryPin'])
            node['edges'].append(edge)
            data['edges'].append(copy.deepcopy(edge))
        self.save_graph(repo, data)
        self.assertEqual('approved', mb.dream_items(repo)['items'][0]['tier'])
        result = self.result(repo, 'deploy.py')
        self.assertEqual(1, len(result['groups']['blocks']))
        for group in ('governs', 'resolves', 'replaced', 'hints'):
            self.assertEqual([], result['groups'][group], group)
        built = graph.build_graph(repo)
        lesson_edges = [e for e in built['edges'] if e['src'] == node['id']]
        self.assertEqual(['BLOCKS'], [e['type'] for e in lesson_edges])

    def test_attachment_exact_tracked_regular_literal_only(self):
        repo, nodes = self.fixture('approved', ('BLOCKS',))
        data = mb._load_queue(repo)[0]
        n = data['nodes'][0]
        for scope in ('deploy.py.bak', 'DEPLOY.py', 'missing.py', '../deploy.py', 'link.py', 'untracked.py'):
            (repo / 'link.py').unlink(missing_ok=True)
            (repo / 'link.py').symlink_to(repo / 'deploy.py')
            (repo / 'untracked.py').write_text('fixture\n')
            n['scopeTag'] = scope
            n['approval']['digest'] = md.node_approval_digest(n)
            self.save_graph(repo, data)
            result = self.result(repo, 'deploy.py', budget=1000)
            self.assertEqual([], result['groups']['blocks'], scope)
            built = graph.build_graph(repo)
            self.assertEqual(1, built['counts']['unattachedLessons'], scope)
        n['scopeTag'] = 'deploy.py'; n['approval']['digest'] = md.node_approval_digest(n)
        self.save_graph(repo, data)
        item = self.result(repo, 'deploy.py')['groups']['blocks'][0]
        self.assertEqual('lesson-path', item['why'])

    def test_hidden_path_cannot_attach_after_display_sanitization(self):
        repo, _ = self.fixture('approved', ('BLOCKS',))
        data = mb._load_queue(repo)[0]
        node = data['nodes'][0]
        snapshot = mb._load_snapshot(repo)[0]
        fid = node['primaryPin']['fileId']
        snapshot[fid][0] += ' deploy.py\u200b'
        (repo / mb.SNAPSHOT_REL / fid).write_text('\n'.join(snapshot[fid]) + '\n')
        pin = md.resolve_pin(snapshot, fid + ':1')
        node.update(scopeTag='general', primaryPin=pin, allPins=[pin])
        node['approval']['digest'] = md.node_approval_digest(node)
        node['edges'][0]['srcPin'] = pin
        node['edges'][0]['approval']['digest'] = md.edge_approval_digest(node['edges'][0])
        data['edges'][0] = copy.deepcopy(node['edges'][0])
        self.save_graph(repo, data)
        self.assertEqual('approved', mb.dream_items(repo)['items'][0]['tier'])
        self.assertEqual([], self.result(repo, 'deploy.py')['groups']['blocks'])

    def pair_history(self):
        self.write('auth.py', 'def one():\n    return 1\n')
        old = 'Always use `auth.py::one` for deployment.'
        middle = 'Deploy with the warmed cache in auth.py.'
        newest = 'Deploy with the final checked cache in auth.py.'
        self.write('AGENTS.md', '- ' + old + '\n')
        mw.init_project(self.root); self.tracked('auth.py', 'AGENTS.md')
        for name, text, date in [('old.log', old, '2026-01-01'), ('middle.log', middle, '2026-01-02'),
                                 ('new.log', newest, '2026-01-03')]:
            dc.dream_ingest(self.root, self.write(name, date + 'T00:00:00Z [user] ' + text + '\n'))
        rows = dc.dream_next(self.root)['rows']
        fids = {f.split('.')[0]: f for f, _, _ in rows}
        candidates = []
        for first, second in [('old', 'middle'), ('middle', 'new')]:
            a, b = fids[first] + ':1', fids[second] + ':1'
            candidates.append(dict(category='superseded_directive', title=first + ' to ' + second,
                                   scope_tag='auth.py', src_spans=[a, b], superseded_by_src=b,
                                   edges=[dict(edge_type='SUPERSEDES', target='deploy', src_span=b, target_span=a)]))
        self.assertTrue(dc.dream_run(self.root, runner=lambda *a, **kw: json.dumps(candidates))['ok'])
        ids = [n['id'] for n in mb._load_queue(self.root)[0]['nodes']]
        self.assertTrue(mb.learn_approve(self.root, ids, approver='reviewer')['ok'])
        return ids, old, middle, newest

    def test_chained_cross_session_replacement_quarantines_matching_rule(self):
        ids, old, middle, newest = self.pair_history()
        built = graph.build_graph(self.root)
        self.assertTrue(built['ok'], built)
        result = self.result(budget=4000)
        self.assertEqual([], result['groups']['governs'])
        self.assertEqual(3, len(result['groups']['replaced']))
        self.assertNotIn('[CURRENT] ' + middle, result['formatted'])
        self.assertIn('[CURRENT] ' + newest, result['formatted'])
        rules = [n for n in built['nodes'] if n['kind'] == 'rule' and n['text'] == old]
        self.assertEqual(1, len(rules))
        self.assertFalse(rules[0]['current'])
        self.assertTrue(rules[0]['replacedBy'])
        self.assertTrue(any(e['basis'] == 'replaced-rule-text' and e['dst'] == rules[0]['id'] for e in built['edges']))
        by_id = self.result(target=rules[0]['id'], budget=4000)
        self.assertEqual([], by_id['groups']['governs'])
        self.assertTrue(any(i['kind'] == 'lesson' for i in by_id['groups']['replaced']))
        by_lesson = self.result(target=ids[0], budget=4000)
        self.assertTrue(any(i['kind'] == 'rule' for i in by_lesson['groups']['replaced']))
        for item in result['groups']['replaced']:
            self.assertIn(item['why'], GRAPH_WHY)

    def test_budget_whole_items_order_zero_and_character_cap(self):
        repo, _ = self.fixture('approved')
        full = self.result(repo, 'deploy.py', budget=8000)
        wanted = copy.deepcopy(full['groups'])
        wanted['resolves'] = []
        omitted = {g: len(full['groups'][g]) - len(wanted[g]) for g in graph.GROUPS}
        boundary = mw.estimate_tokens(graph._format(wanted, omitted))
        small = self.result(repo, 'deploy.py', budget=boundary)
        self.assertTrue(small['groups']['replaced'])
        self.assertEqual([], small['groups']['resolves'])
        for group, values in small['groups'].items():
            self.assertEqual(len(full['groups'][group]) - len(values), small['omitted'][group])
            self.assertTrue(all(i in full['groups'][group] for i in values))
        zero = self.result(repo, 'deploy.py', budget=0)
        self.assertEqual('', zero['formatted'])
        self.assertEqual(8, sum(zero['omitted'].values()))
        self.write('auth.py', fixtures.CODE)
        self.write('AGENTS.md', '- ' + 'x' * 9000 + ' `auth.py::validate_token`.\n')
        mw.init_project(self.root)
        huge = self.result(budget=100000)
        self.assertEqual([], huge['groups']['governs'])
        self.assertEqual(1, huge['omitted']['governs'])

    def event(self, node, at, decision='approved', kind='node'):
        item_id = node['id' if kind == 'node' else 'edgeId']
        digest = md.node_approval_digest(node) if kind == 'node' else md.edge_approval_digest(node)
        with contextlib.closing(sqlite3.connect(str(self.root / mw.LEDGER_PATH))) as db:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS dream_decision_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, '
                           'at TEXT, item_id TEXT, kind TEXT, decision TEXT, by TEXT, digest TEXT, paths TEXT)')
                db.execute('INSERT INTO dream_decision_events (at,item_id,kind,decision,by,digest,paths) VALUES (?,?,?,?,?,?,?)',
                           (at, item_id, kind, decision, 'reviewer', digest, json.dumps(node.get('paths', []))))

    def test_as_of_pin_approve_supersede_reject_and_reapprove(self):
        ids, old, middle, newest = self.pair_history()
        data = mb._load_queue(self.root)[0]
        first, second = sorted(data['nodes'], key=lambda n: n['primaryPin']['quoteText'])
        first = next(n for n in data['nodes'] if old in n['primaryPin']['quoteText'])
        second = next(n for n in data['nodes'] if middle in n['primaryPin']['quoteText'])
        config, _ = mw.read_config(self.root)
        config['pinnedAnchors']['auth.py::one']['pinnedAt'] = '2026-01-01T00:00:00Z'
        mw.write_managed_text(self.root, mw.CONFIG_PATH, json.dumps(config))
        for node, day in ((first, 2), (second, 3)):
            self.event(node, '2026-01-0%dT00:00:00Z' % day)
            for edge in node['edges']:
                self.event(edge, '2026-01-0%dT00:00:00Z' % day, kind='edge')
        self.event(second, '2026-01-04T00:00:00Z', 'rejected')
        for e in second['edges']:
            self.event(e, '2026-01-04T00:00:00Z', 'rejected', 'edge')
        self.assertTrue(mb.learn_reject(self.root, [second['id']], approver='reviewer')['ok'])
        for day, count in ((1, 0), (2, 1), (3, 2), (4, 1)):
            with self.subTest(day=day):
                built = graph.build_graph(self.root, as_of='2026-01-0%dT12:00:00Z' % day)
                self.assertTrue(built['ok'], built)
                self.assertEqual(count, built['counts']['lessonItems'])
                result = self.result(as_of='2026-01-0%dT12:00:00Z' % day, budget=4000)
                self.assertEqual(day == 1, bool(result['groups']['governs']))
                if day == 2:
                    self.assertNotIn(newest, result['formatted'])
                    self.assertIn('[CURRENT] ' + middle, result['formatted'])
                if day == 3:
                    self.assertNotIn('[CURRENT] ' + middle, result['formatted'])
                    cited = next(n for n in built['nodes'] if n.get('recordType') == 'citation' and n['text'] == middle)
                    self.assertEqual('2026-01-02T00:00:00Z', cited['validFrom'])
                    self.assertEqual('2026-01-03T00:00:00Z', cited['validTo'])
        self.assertTrue(mb.learn_approve(self.root, [second['id']], approver='reviewer', reconsider=True)['ok'])
        self.event(second, '2026-01-05T00:00:00Z')
        for e in second['edges']:
            self.event(e, '2026-01-05T00:00:00Z', kind='edge')
        after = graph.build_graph(self.root, as_of='2026-01-05T12:00:00Z')
        self.assertEqual(2, after['counts']['lessonItems'])
        node = next(n for n in after['nodes'] if n['id'] == second['id'])
        self.assertEqual([{'validFrom': '2026-01-03T00:00:00Z', 'validTo': '2026-01-04T00:00:00Z'},
                          {'validFrom': '2026-01-05T00:00:00Z', 'validTo': None}], node['validity'])

    def test_timestamp_grammar_unknown_since_and_future_knowledge(self):
        self.anchors()
        current = graph.build_graph(self.root)
        written = next(e for e in current['edges'] if e['basis'] == 'written-anchor')
        self.assertIsNone(written['validFrom'])
        past = graph.build_graph(self.root, as_of='2000-01-01')
        self.assertEqual([], past['nodes'])
        for value in ('20260101', '2026-W01-1', '2026-01-01 00:00:00', '2026-01-01T00:00:00+0000', 'bad'):
            self.assertFalse(graph.build_graph(self.root, as_of=value)['ok'], value)
        for value in ('2026-01-01', '2026-01-01Z', '2026-01-01T00:00', '2026-01-01T00:00:00.123456789Z'):
            self.assertEqual(memlet_eval._stamp(value), memlet_eval._stamp(graph._at(value)))

    def test_current_source_bytes_required_in_historical_query(self):
        repo, nodes = self.fixture('approved', ('BLOCKS',))
        item = nodes['BLOCKS']
        since = item['approval']['at']
        self.assertEqual(1, graph.build_graph(repo, as_of=since)['counts']['lessonItems'])
        file = repo / mb.SNAPSHOT_REL / item['primaryPin']['fileId']
        file.write_text('changed evidence\n')
        self.assertEqual(0, graph.build_graph(repo, as_of=since)['counts']['lessonItems'])

    def test_retained_pin_without_rule_is_a_symbol_not_a_governing_edge(self):
        self.write('auth.py', fixtures.CODE)
        self.write('AGENTS.md', '- Keep `auth.py::validate_token`.\n')
        mw.init_project(self.root)
        self.write('AGENTS.md', '# Removed rule\n')
        result = graph.build_graph(self.root)
        self.assertEqual(0, result['counts']['nodes']['rule'])
        self.assertEqual(1, result['counts']['nodes']['symbol'])
        self.assertEqual([], result['edges'])

    def test_current_drift_is_not_undated_historical_knowledge(self):
        self.write('auth.py', fixtures.CODE)
        self.write('AGENTS.md', '- Keep `auth.py::validate_token`.\n')
        mw.init_project(self.root)
        config, _ = mw.read_config(self.root)
        config['pinnedAnchors']['auth.py::validate_token']['pinnedAt'] = '2026-01-01T00:00:00Z'
        mw.write_managed_text(self.root, mw.CONFIG_PATH, json.dumps(config))
        self.assertTrue(self.result(as_of='2026-01-02')['groups']['governs'])
        self.write('auth.py', fixtures.CODE.replace('(token)', '(token, issuer)'))
        self.assertTrue(self.result()['groups']['stale'])
        historical = self.result(as_of='2026-01-02')
        self.assertFalse(any(historical['groups'].values()))

    def test_accepted_anchor_drop_has_no_fabricated_history(self):
        sid = self.anchors()
        before = graph.build_graph(self.root)
        accepted = next(e for e in before['edges'] if e['basis'] == 'accepted-suggestion')
        self.assertIsNotNone(accepted['validFrom'])
        self.assertTrue(ma.drop_sidecar_anchors(self.root, [sid])['ok'])
        after = graph.build_graph(self.root)
        self.assertFalse(any(e['basis'] == 'accepted-suggestion' for e in after['edges']))
        self.assertIn('deleted sidecar payloads', ' '.join(after['notices']))

    def test_literal_attachment_non_git_directory(self):
        folder = self.base / 'nongit'
        folder.mkdir()
        (folder / 'auth.py').write_text('# fixture\n')
        files = graph._Files(folder)
        self.assertFalse(files.in_git)
        self.assertTrue(files.accepts('auth.py'))
        self.assertFalse(files.accepts('AUTH.py'))

    def test_shared_attachment_context_and_untracked_human_path(self):
        repo, nodes = self.fixture('approved', ('BLOCKS',))
        target = repo / 'human.py'
        target.write_text('# deliberately untracked\n')
        data = mb._load_queue(repo)[0]
        data['nodes'][0]['approvedPaths'] = ['human.py']
        # The base version has no path approval field; the injected reader is
        # the E3 adapter boundary. Production never trusts this raw field itself.
        data['nodes'][0]['approval']['digest'] = md.node_approval_digest(data['nodes'][0])
        data['nodes'][0]['approval']['paths'] = ['human.py']
        self.save_graph(repo, data)
        shared = object()
        with mock.patch.object(mb, 'lesson_path_context', return_value=shared, create=True) as context, \
             mock.patch.object(mb, 'literal_lesson_paths', return_value=[], create=True), \
             mock.patch.object(mb, 'lesson_paths', return_value=['human.py'], create=True) as human:
            result = self.result(repo, 'human.py')
            self.assertEqual('lesson-path', result['groups']['blocks'][0]['why'])
            context.assert_called_once_with(repo)
            self.assertIs(shared, human.call_args.kwargs['context'])

    def test_hostile_rule_bounds_and_hidden_characters(self):
        self.write('auth.py', fixtures.CODE)
        self.write('AGENTS.md', '- hidden\u200b `auth.py::validate_token`.\n' +
                   '- long ' + 'x' * 20000 + ' `auth.py::validate_token`.\n')
        result = graph.build_graph(self.root)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['truncated'])
        self.assertEqual([], result['nodes'])
        self.write('AGENTS.md', ('- x\n' * 100000))
        before = time.perf_counter()
        result = graph.build_graph(self.root)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(result['counts']['nodes']['rule'], mw._MAX_RULES_PER_FILE)
        self.assertLess(time.perf_counter() - before, 8)
        self.write('AGENTS.md', 'x' * (mw._MAX_SOURCE_BYTES + 1))
        result = graph.build_graph(self.root)
        self.assertTrue(result['truncated'])
        self.assertEqual([], result['nodes'])

    def test_cli_shapes_exit_codes_markdown_and_plugin_group(self):
        self.anchors()
        for target in ('auth.py', 'auth.py::validate_token'):
            cli = self.cli('admin', 'graph', 'show', target, '--repo', str(self.root))
            self.assertEqual(0, cli.returncode, cli.stderr)
            result = json.loads(cli.stdout)
            self.assertEqual(set(graph.GROUPS), set(result['groups']))
            markdown = self.cli('admin', 'graph', 'show', target, '--repo', str(self.root), '--format', 'markdown')
            self.assertEqual(0, markdown.returncode, markdown.stderr)
            self.assertEqual(result['formatted'], markdown.stdout)
        stats = self.cli('admin', 'graph', 'stats', '--repo', str(self.root))
        self.assertEqual(0, stats.returncode, stats.stderr)
        self.assertEqual(2, json.loads(stats.stdout)['counts']['nodes']['rule'])
        bad = self.cli('admin', 'graph', 'show', '../outside')
        self.assertEqual(2, bad.returncode)
        self.assertFalse(json.loads(bad.stdout)['ok'])
        help_text = self.cli('admin', 'graph', 'show', '--help')
        for flag in ('--repo', '--tiers', '--as-of', '--budget', '--format'):
            self.assertIn(flag, help_text.stdout)
        self.assertEqual('Rules/wiki', cli_plugins.commands(cli_plugins.load())['graph']['group'])

    def test_no_model_or_write_and_mcp_definitions_unchanged(self):
        self.anchors()
        before = {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.root.rglob('*') if p.is_file() and '.git' not in p.parts}
        definitions = json.dumps(memlet_mcp.mcp_tools(), sort_keys=True)
        with mock.patch.object(vendor_cli, 'run_vendor', side_effect=AssertionError('model forbidden')):
            graph.build_graph(self.root)
            self.result()
        after = {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.root.rglob('*') if p.is_file() and '.git' not in p.parts}
        self.assertEqual(before, after)
        self.assertEqual(definitions, json.dumps(memlet_mcp.mcp_tools(), sort_keys=True))

    def test_large_repository_file_slice_cost_and_classifier_bound(self):
        # 5,000 real code files, 2,000 rules, 2,000 sealed approved lessons.
        # Build directly from deterministic fixtures: no vendor/runner involved.
        for number in range(5000):
            self.write('src/f%04d.py' % number, 'def run():\n    return 1\n')
        for number in range(10):
            self.write('rules%02d/AGENTS.md' % number, '\n'.join(
                '- Keep `src/f%04d.py::run` checked.' % i for i in range(number * 200, (number + 1) * 200)) + '\n')
        self.tracked('src')
        snapshot = ['2026-01-01T00:00:00Z [user] Keep src/f%04d.py checked.' % i for i in range(2000)]
        self.write(mb.SNAPSHOT_REL + '/large.log', '\n'.join(snapshot) + '\n')
        nodes, edges = [], []
        for number in range(2000):
            pin = md.resolve_pin({'large.log': snapshot}, 'large.log:%d' % (number + 1))
            edge = dict(edgeId='E%d' % number, edgeType='BLOCKS', fromId='DRM-%d' % number,
                        toId='BLOCKS:%d' % number, targetLabel='cold cache', srcPin=pin, edgeVerdict='VALID')
            node = dict(id='DRM-%d' % number, title='fixture', category='repeated_pitfall',
                        statusKind='open_trap', scopeTag='src/f%04d.py' % number,
                        primaryPin=pin, allPins=[pin], edges=[edge], verdict='SUPPORTED')
            for obj, kind in ((edge, 'edge'), (node, 'node')):
                obj['approval'] = dict(schema=md.APPROVAL_RECORD_SCHEMA, decision='approved', by='fixture',
                                       at='2026-01-02T00:00:00Z', digest=md.edge_approval_digest(obj)
                                       if kind == 'edge' else md.node_approval_digest(obj))
            nodes.append(node); edges.append(edge)
        self.write(mb.QUEUE_REL, json.dumps(dict(schema=md.DREAM_ONTOLOGY_SCHEMA, nodes=nodes, edges=edges)))
        real = mw.classify_anchor
        times = []
        with mock.patch.object(graph, 'build_graph', side_effect=AssertionError('whole graph forbidden')), \
             mock.patch.object(mw, 'classify_anchor', wraps=real) as classifier:
            for _ in range(2):
                start = time.perf_counter()
                result = self.result(target='src/f0001.py', budget=1000)
                times.append(time.perf_counter() - start)
                self.assertEqual(1, len(result['groups']['blocks']))
                self.assertEqual(1, len(result['groups']['stale']))
            self.assertEqual(2, classifier.call_count)
        print('E1 synthetic 5000 files / 2000 rules / 2000 lessons: %.3fs cold, %.3fs repeated' % tuple(times))
        self.assertLess(max(times), 8.0)  # generous CI ceiling; not a timing microbenchmark


if __name__ == '__main__':
    unittest.main()
