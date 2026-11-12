"""Memlet 9.3: the units together, with the real functions on both sides (no stub of the merge reads).

The unit test files check each unit against the contract's shapes. These tests check what only shows when the units
meet: the command the tidy report prints is one the real `learn` parser accepts and that does what its label says,
a merge made that way is seen by the report and by every lesson reader, and a merge that lapses is reported.
"""
import contextlib
import io
import json
import shlex
import sys
import unittest
from unittest import mock

import memory_cli
import test_v93_tidy as tidy_tests
from memlet import memlet_bridge as mb, memlet_skillbook as sb, memlet_tidy as mt

REAL_MERGE_FUNCTIONS = mt._merge_functions  # captured before any test replaces the adapter's lookup
LINE = tidy_tests.LINE
OTHER = 'Never commit generated files under build/ in billing.py reviews.'


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, 'argv', ['memlet'] + list(argv)), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        try:
            code = memory_cli.cli()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class TidyAndMergeTogether(tidy_tests.Base):
    def setUp(self):
        super().setUp()  # the unit tests' default is "merge not available"; here the real functions are used
        real = mock.patch.object(mt, '_merge_functions', REAL_MERGE_FUNCTIONS)
        real.start()
        self.addCleanup(real.stop)
        self.assertTrue(all(callable(f) for f in mt._merge_functions()))

    def fixture(self):
        repo = self.make_repo()
        first = self.learn(repo, 'monday.log', '2026-11-02T10:00:00Z [user] ' + LINE)
        second = self.learn(repo, 'tuesday.log', '2026-11-03T10:00:00Z [user] ' + LINE)
        third = self.learn(repo, 'wednesday.log', '2026-11-04T10:00:00Z [user] ' + OTHER)
        self.assertEqual((1, 1, 1), (len(first), len(second), len(third)))
        self.assertNotEqual(first, second)  # the same sentence from two sessions is two lessons until a person merges
        return repo, first[0], second[0], third[0]

    def shown(self, repo, tiers=('approved', 'hint', 'pending')):
        return sorted(i['id'] for i in mb.dream_items(repo, tiers=tiers)['items'])

    def test_the_printed_merge_command_is_accepted_and_does_what_it_says(self):
        repo, first, second, third = self.fixture()
        report = mt.tidy_report(repo)
        self.assertTrue(report['ok'] and report['mergeChecks'], report.get('notices'))
        groups = [g for g in report['duplicates'] if g['kind'] == 'same-text']
        self.assertEqual(1, len(groups), report['duplicates'])
        group = groups[0]
        self.assertEqual(first, group['keep'])  # the earliest line is the suggested kept lesson
        self.assertEqual([second], group['same'])
        self.assertEqual({}, group['blockers'])
        argv = shlex.split(group['command'])
        self.assertEqual('memlet', argv[0])
        self.assertIn('NAME', group['command'])  # the person's name is theirs to fill in
        argv = [a.replace('NAME', 'Robin') for a in argv[1:]]
        before = self.shown(repo)
        code, out, err = run_cli(argv)
        self.assertEqual((0, ''), (code, err), out)
        result = json.loads(out)
        self.assertEqual([second], result['applied'])
        # every reader now shows the kept lesson and not the member
        self.assertEqual(sorted(set(before) - {second}), self.shown(repo))
        view = mb.merge_view(repo)
        self.assertEqual({'state': 'merged', 'into': first, 'reason': None}, view['states'][second])
        after = mt.tidy_report(repo)
        self.assertTrue(after['mergeChecks'])
        self.assertEqual([], [g for g in after['duplicates'] if g['kind'] == 'same-text'])
        # the two sessions of the group count once each for the kept lesson
        said = {e['id']: e for e in after['saidAgain']}
        self.assertIn(first, said)
        self.assertEqual(2, said[first]['sessions'])
        self.assertNotIn(second, said)
        pending = mb.learn_pending(repo, _record_review=False)
        self.assertEqual([[first, [second]]],
                         [[g['id'], [m['id'] for m in g['members']]] for g in pending['mergedLessons']])

    def test_a_refused_merge_is_shown_as_blocked_and_the_command_refuses(self):
        repo, first, second, third = self.fixture()
        self.assertTrue(mb.learn_reject(repo, [first], approver='Robin', reason='not a rule')['ok'])
        report = mt.tidy_report(repo)
        # a rejected lesson is not offered as a duplicate at all
        self.assertEqual([], [g for g in report['duplicates'] if first in [g['keep']] + list(g['same'])])
        refused = mb.merge_blockers(repo, first, [second])
        self.assertTrue(refused['ok'])  # computed; refused through blockers
        self.assertTrue(refused['blockers'][second])
        code, out, err = run_cli(['learn', '--merge', first, '--same', second, '--by', 'Robin', '--repo', str(repo)])
        self.assertNotEqual(0, code)
        self.assertEqual('none', mb.merge_view(repo)['states'][second]['state'])

    def test_a_lapsed_merge_is_reported_and_the_member_is_shown_again(self):
        repo, first, second, third = self.fixture()
        self.assertTrue(mb.learn_merge(repo, first, [second], approver='Robin')['ok'])
        self.assertNotIn(second, self.shown(repo))
        self.assertTrue(mb.learn_reject(repo, [first], approver='Robin', reason='changed my mind')['ok'])
        self.assertIn(second, self.shown(repo))  # nothing a person did not decide stays hidden
        report = mt.tidy_report(repo)
        lapsed = {e['id']: e for e in report['lapsedMerges']}
        self.assertIn(second, lapsed)
        self.assertEqual(first, lapsed[second]['into'])
        self.assertEqual('keep-rejected', lapsed[second]['reason'])
        for command in lapsed[second].get('commands') or []:
            argv = shlex.split(command)
            self.assertEqual('memlet', argv[0])
            memory_cli._build_parser().parse_args([a.replace('NAME', 'Robin') for a in argv[1:]])  # accepted by the real parser

    def test_the_skill_file_leaves_out_a_merged_member_and_brings_it_back_when_the_merge_lapses(self):
        repo, first, second, third = self.fixture()
        self.assertTrue(mb.learn_approve(repo, [first, second, third], approver='Robin')['ok'])
        both = sb.build_skill(repo)
        self.assertTrue(both['ok'], both)
        for ident in (first, second, third):
            self.assertIn('lesson %s ' % ident, both['text'])
        self.assertTrue(mb.learn_merge(repo, first, [second], approver='Robin')['ok'])
        merged = sb.build_skill(repo)
        self.assertIn('lesson %s ' % first, merged['text'])
        self.assertNotIn(second, merged['text'])  # not as an item, a label or a citation
        self.assertEqual(both['items'] - 1, merged['items'])
        self.assertTrue(mb.learn_unmerge(repo, [second], approver='Robin')['ok'])
        self.assertEqual(both['text'], sb.build_skill(repo)['text'])  # same state, same bytes

    def test_an_unknown_kept_lesson_is_named_once(self):
        repo, first, second, third = self.fixture()
        code, out, err = run_cli(['learn', '--merge', 'DRM-nope', '--same', first, second, '--by', 'Robin',
                                  '--repo', str(repo), '--format', 'markdown'])
        self.assertEqual(2, code)
        self.assertEqual(1, (out + err).count('DRM-nope: '), out + err)
        self.assertNotIn(first + ': ', out + err)

    def test_tidy_stays_read_only_with_merges_present(self):
        repo, first, second, third = self.fixture()
        self.assertTrue(mb.learn_merge(repo, first, [second], approver='Robin')['ok'])
        before = tidy_tests.tree_state(repo)
        for fmt in ('json', 'markdown'):
            code, out, err = run_cli(['admin', 'tidy', '--repo', str(repo), '--format', fmt])
            self.assertEqual((0, ''), (code, err))
            self.assertTrue(out.strip())
            self.assertEqual(before, tidy_tests.tree_state(repo), fmt)

    def printed(self, command):
        argv = [a.replace('NAME', 'Robin').replace('--path=FILE', '--path=billing.py')
                for a in shlex.split(command)[1:]]
        code, out, err = run_cli(argv)
        self.assertEqual((0, ''), (code, err), out)
        return json.loads(out)

    def test_similar_wording_members_add_distinct_sessions_through_real_cli(self):
        repo = self.make_repo()
        a = self.learn(repo, 'a.log', '2026-11-01T10:00:00Z [user] ' + LINE)[0]
        b = self.learn(repo, 'b.log', '2026-11-02T10:00:00Z [user] ' + LINE.replace('the retry', 'a retry'))[0]
        code, out, err = run_cli(['admin', 'tidy', '--repo', str(repo)])
        group = json.loads(out)['duplicates'][0]
        self.assertEqual('similar-text', group['kind'])
        self.printed(group['command'])
        code, out, err = run_cli(['admin', 'tidy', '--repo', str(repo), '--format', 'markdown'])
        self.assertEqual((0, ''), (code, err))
        self.assertIn('said in 2 session(s)', out)
        self.assertEqual([a], self.shown(repo))
        self.assertEqual(2, mt.lesson_sightings(repo)['sightings'][a]['sessions'])

    def test_path_changes_refuse_members_and_report_kept_lost_files(self):
        repo, a, b, _ = self.fixture()
        self.assertTrue(mb.learn_approve(repo, [a, b], approver='Robin', paths=['anchor.py'])['ok'])
        self.assertTrue(mb.learn_merge(repo, a, [b], approver='Robin')['ok'])
        queue = repo / mb.QUEUE_REL
        for args in (['--paths', b], ['--approve', b]):
            before = queue.read_bytes()
            code, out, err = run_cli(['learn', *args, '--path', 'billing.py', '--by', 'Robin', '--repo', str(repo)])
            self.assertEqual(2, code, out)
            refused = json.loads(out)
            self.assertEqual(a, refused['keep'])
            self.assertIn(a, refused['error'])
            self.assertEqual(before, queue.read_bytes())
            repaired = self.printed(refused['pathCommands'][a])
            self.assertIn('billing.py', repaired['paths'])
        changed = mb.learn_paths(repo, [a], approver='Robin', paths=['billing.py'])
        self.assertEqual({b: ['anchor.py']}, changed['lostFiles'])
        report = mt.tidy_report(repo)
        lost = report['mergedLostFiles'][0]
        self.assertEqual({b: ['anchor.py']}, lost['lostFiles'])
        self.printed(lost['command'])
        self.assertEqual([], mt.tidy_report(repo)['mergedLostFiles'])
        self.assertEqual('merged', mb.merge_view(repo)['states'][b]['state'])
        mb.learn_approve(repo, [a], approver='Robin', reconsider=True, paths=['billing.py'])
        self.assertIn(b, mt.tidy_report(repo)['mergedLostFiles'][0]['lostFiles'])

    def test_tidy_merge_then_paths_commands_from_other_directory(self):
        import os
        repo, a, b, _ = self.fixture()
        self.assertTrue(mb.learn_approve(repo, [a], approver='Robin', paths=['billing.py'])['ok'])
        self.assertTrue(mb.learn_approve(repo, [b], approver='Robin', paths=['anchor.py'])['ok'])
        renamed = self.base / "remote ' quoted"
        repo.rename(renamed)
        repo = renamed
        caller = self.make_repo('caller')
        old = os.getcwd()
        try:
            os.chdir(caller)
            report = mt.tidy_report(repo)
            group = report['duplicates'][0]
            result = self.printed(group['command'])
            command = result['pathCommands'][a]
            self.assertIn('--repo', command)
            self.printed(command)
            self.assertEqual([], mt.tidy_report(repo)['mergedLostFiles'])
            self.assertIn('anchor.py', mb.dream_items(repo)['items'][0]['paths'])
            self.assertFalse((caller / mb.QUEUE_REL).exists())
        finally:
            os.chdir(old)

    def test_blocked_duplicates_and_lapses_have_sentences_and_only_working_commands(self):
        from memlet import vocabulary as v
        repo, a, b, c = self.fixture()
        # The earliest lesson becomes a member; the later kept lesson has a holding member.
        self.assertTrue(mb.learn_merge(repo, b, [c], approver='Robin')['ok'])
        report = mt.tidy_report(repo)
        group = report['duplicates'][0]
        self.assertIsNone(group['command'])
        self.assertIn('member-has-members', group['blockers'][b])
        self.assertIn(v.MERGE_REASONS['member-has-members'], group['blockerMessages'][b])
        self.assertIn(v.MERGE_REASONS['member-has-members'], report['formatted'])
        self.assertTrue(mb.learn_reject(repo, [b], approver='Robin')['ok'])
        entry = mt.tidy_report(repo)['lapsedMerges'][0]
        self.assertEqual(v.MERGE_REASONS['keep-rejected'], entry['message'])
        self.assertEqual([entry['command']], entry['commands'])
        self.printed(entry['command'])
        self.assertEqual('none', mb.merge_view(repo)['states'][c]['state'])
        graph = mb._load_queue(repo)[0]
        next(n for n in graph['nodes'] if n['id'] == c)['merge'] = {'schema': 'broken'}
        self.save_graph(repo, graph)
        entry = mt.tidy_report(repo)['lapsedMerges'][0]
        self.assertEqual(v.MERGE_INVALID_LINE, entry['label'])
        self.assertNotIn('None', entry['label'])
        self.printed(entry['command'])

    def test_all_other_report_actions_execute_and_match_their_labels(self):
        repo, a, b, c = self.fixture()
        # Both alternatives are real decisions, each exercised from the appropriate state.
        said = next(e for e in mt.tidy_report(repo)['saidAgain'] if e['id'] == a)
        for command in said['commands']:
            result = self.printed(command)
            self.assertIn(said['id'], result['applied'] + result['unchanged'])
        self.set_decision_time(repo, [a], '2026-01-01T00:00:00Z')
        rejected = next(e for e in mt.tidy_report(repo)['saidAgain'] if e['id'] == a)
        self.assertIn('--reconsider', rejected['command'])
        self.printed(rejected['command'])
        # An approved unanchored lesson has a real paths command.
        loose = self.learn(repo, 'loose.log', '2026-11-01T00:00:00Z [user] Never skip the unique health probe.')[0]
        mb.learn_approve(repo, [loose], approver='Robin')
        entry = next(e for e in mt.tidy_report(repo)['unattached'] if e['id'] == loose)
        self.printed(entry['command'])
        self.assertEqual(['billing.py'], next(i for i in mb.dream_items(repo)['items'] if i['id'] == loose)['paths'])
        # Exact paste is recognized, and its check and retire commands run.
        report = mt.tidy_report(repo, min_seen=2)
        entry = next(e for e in report['promote'] if e['id'] == a)
        with (repo / 'AGENTS.md').open('a') as f:
            f.write('\n' + entry['block'])
        self.printed(entry['command'])
        report = mt.tidy_report(repo, min_seen=2)
        retire = next(e for e in report['retire'] if e['id'] == a)
        self.assertIn('now-a-rule', retire['reasons'])
        self.assertNotIn(a, [e['id'] for e in report['promote']])
        self.printed(retire['command'])
        self.assertNotIn(a, self.shown(repo, tiers=('approved',)))
        # A changed digest with compatible relations permits the report's re-merge alternative.
        mb.learn_merge(repo, b, [c], approver='Robin')
        graph = mb._load_queue(repo)[0]
        next(n for n in graph['nodes'] if n['id'] == c)['merge']['keepDigest'] = 'old'
        self.save_graph(repo, graph)
        lapse = mt.tidy_report(repo)['lapsedMerges'][0]
        self.assertEqual(2, len(lapse['commands']))
        self.printed(lapse['commands'][1])
        self.assertEqual('merged', mb.merge_view(repo)['states'][c]['state'])

    def test_skill_export_never_becomes_rules_and_rejected_lesson_disappears(self):
        from memlet import memlet_graph as mg, memlet_wiki as mw, audit
        repo, a, b, c = self.fixture()
        mb.learn_approve(repo, [a, b, c], approver='Robin')
        def readings():
            return [run_cli(args) for args in (
                ['recall', 'refund', '--repo', str(repo)],
                ['admin', 'graph', 'show', 'billing.py', '--repo', str(repo)],
                ['admin', 'graph', 'stats', '--repo', str(repo)])]
        before = readings()
        code, out, err = run_cli(['admin', 'skillbook', '--repo', str(repo), '--write', '--out', str(repo / '.claude/skills')])
        self.assertEqual((0, ''), (code, err), out)
        self.assertEqual(before, readings())
        rel = '.claude/skills/memlet-lessons/SKILL.md'
        self.assertNotIn(rel, mw.discover_instruction_files(repo))
        self.assertNotIn(rel, mw.import_source_files(repo))
        self.assertFalse(mw.is_instruction_path(repo, rel))
        self.assertEqual([], mw._rules_from_files(repo, [rel]))
        self.assertNotIn(repo / rel, audit._discover_instruction_docs(repo))
        extra = sb.write_skill(repo, out=repo / '.ai-memory')
        self.assertTrue(extra['ok'], extra)
        self.assertNotIn('.ai-memory/memlet-lessons/SKILL.md', mw.import_source_files(repo))
        for entry in mt.tidy_report(repo, min_seen=1)['promote']:
            self.assertNotIn(rel, entry['instructionFiles'])
        mb.learn_reject(repo, [a, b], approver='Robin')
        self.assertNotIn(LINE, run_cli(['recall', 'refund', '--repo', str(repo)])[1])
        skill = repo / rel
        # Ownership survives editing and old versions; an ordinary skill still supplies rules.
        skill.write_text(skill.read_text().replace('memlet-version:', 'old-version:') + '\n- Never fake this.\n')
        self.assertFalse(mw.is_instruction_path(repo, rel))
        # A large body cannot hide the frontmatter marker from bounded discovery.
        skill.write_text(skill.read_text() + 'x' * (mw._MAX_SOURCE_BYTES + 1))
        self.assertNotIn(rel, mw.discover_instruction_files(repo))
        self.assertNotIn(rel, mw.import_source_files(repo))
        self.assertNotIn(repo / rel, audit._discover_instruction_docs(repo))
        skill.write_text('---\nname: ordinary\nmetadata:\n  author: person\n---\n- Never fake this.\n')
        self.assertTrue(mw.is_instruction_path(repo, rel))

    def test_skill_writer_checker_and_caps_with_holding_merge(self):
        repo, a, b, c = self.fixture()
        mb.learn_reject(repo, [c], approver='Robin')
        c = self.learn(repo, 'unattached.log', '2026-11-05T00:00:00Z [user] Never publish unreviewed changes.')[0]
        mb.learn_approve(repo, [a, b, c], approver='Robin')
        mb.learn_paths(repo, [c], approver='Robin', paths=['anchor.py'])
        target = self.base / 'exports'
        before = sb.write_skill(repo, out=target)
        self.assertTrue(before['ok'], before)
        self.assertTrue(sb.check_skill(before['path'], repo_path=repo)['current'])
        mb.learn_merge(repo, a, [b], approver='Robin')
        self.assertFalse(sb.check_skill(before['path'], repo_path=repo)['current'])
        after = sb.write_skill(repo, out=target)
        self.assertTrue(after['ok'], after)
        self.assertNotIn(b, after['text'])
        self.assertTrue(sb.check_skill(after['path'], repo_path=repo)['current'])
        for options in ({'budget': 1}, {'max_lines': 1}):
            result = sb.write_skill(repo, out=target, **options)
            self.assertFalse(result['ok'], result)
        self.assertEqual(after['text'], __import__('pathlib').Path(after['path']).read_text())
        custom = sb.write_skill(repo, out=target, max_lines=10, budget=1000)
        self.assertTrue(custom['ok'], custom)
        self.assertTrue(sb.check_skill(custom['path'], repo_path=repo, max_lines=10, budget=1000)['current'])
        self.assertFalse(sb.check_skill(custom['path'], repo_path=repo)['current'])

    def test_relative_skill_output_repo_resolution_and_symlinks(self):
        import os
        from pathlib import Path
        repo, a, b, c = self.fixture()
        mb.learn_approve(repo, [a], approver='Robin')
        old = os.getcwd()
        try:
            os.chdir(self.base)
            result = sb.write_skill(repo, out='relative-export')
            self.assertTrue(result['ok'], result)
            self.assertEqual(str(repo / 'relative-export/memlet-lessons/SKILL.md'), result['path'])
        finally:
            os.chdir(old)
        outside = self.base / 'outside'
        outside.mkdir()
        (repo / 'linked').symlink_to(outside, target_is_directory=True)
        for out in ('linked/new/skills', repo / 'linked/new/skills'):
            self.assertFalse(sb.write_skill(repo, out=out)['ok'])
        alias = self.base / 'repo-alias'
        alias.symlink_to(repo, target_is_directory=True)
        self.assertFalse(sb.write_skill(repo, out=alias / 'linked/new/skills')['ok'])
        external = self.base / 'external-link'
        external.symlink_to(outside, target_is_directory=True)
        self.assertTrue(sb.write_skill(repo, out=external)['ok'])
        blank = self.base / 'blank'
        blank.mkdir()
        output = self.base / 'no-output'
        for args in (['--write', '--out', str(output)], ['--check', result['path']]):
            code, out, err = run_cli(['admin', 'skillbook', '--repo', str(blank), *args])
            self.assertEqual(1, code)
            self.assertIn('memlet init', out)
        self.assertFalse(output.exists())

    def test_unknown_keep_message_and_merged_per_line_citations(self):
        repo, a, b, _ = self.fixture()
        result = mb.learn_merge(repo, 'DRM-unknown', [b], approver='Robin')
        self.assertIn('DRM-unknown', result['error'])
        self.assertNotIn(b + ':', result['error'])
        mb.learn_merge(repo, a, [b], approver='Robin')
        pending = mb.learn_pending(repo, _record_review=False)
        group = pending['mergedLessons'][0]
        for item in [group] + group['members']:
            self.assertEqual(len(item['quoteLines']), len(item['cites']))
            self.assertIn(mb._pending_quotes(item).strip(), pending['formatted'])

    def test_skill_221_items_keeps_first_edit_guard(self):
        import subprocess, os
        repo = self.make_repo()
        lines = ['2026-11-01T10:00:00Z [user] Never retry case%04d.' % i for i in range(220)]
        lines.insert(0, '2026-11-01T10:00:00Z [user] ' + LINE)
        ids = []
        for start in range(0, len(lines), 22):
            ids.extend(self.learn(repo, 'many%d.log' % start, *lines[start:start+22]))
        self.assertEqual(221, len(ids))
        self.assertTrue(mb.learn_approve(repo, ids, approver='Robin')['ok'])
        code, out, err = run_cli(['init', str(repo), '--hooks', '--guard'])
        self.assertEqual(0, code, out)
        def hook(session):
            payload = {'cwd': str(repo), 'session_id': session, 'hook_event_name': 'PreToolUse',
                       'tool_name': 'Edit', 'tool_input': {'file_path': str(repo / 'billing.py')}, 'permission_mode': 'default'}
            subprocess.run([sys.executable, str(tidy_tests.CLI), 'admin', 'hook', 'session-start'],
                           input=json.dumps(dict(payload, hook_event_name='SessionStart', source='startup')),
                           text=True, capture_output=True, cwd=repo, env=dict(os.environ, MEMLET_HOOK_SCOPE='local'), check=True)
            p = subprocess.run([sys.executable, str(tidy_tests.CLI), 'admin', 'hook', 'pre-tool-use'],
                               input=json.dumps(payload), text=True, capture_output=True, cwd=repo,
                               env=dict(os.environ, MEMLET_HOOK_SCOPE='local'))
            self.assertEqual(0, p.returncode, p.stderr)
            return json.loads(p.stdout)['hookSpecificOutput']['permissionDecision']
        self.assertEqual('deny', hook('before-export'))
        result = sb.write_skill(repo, out=repo / '.claude/skills', max_lines=10000, budget=100000)
        self.assertEqual(221, result['items'])
        self.assertEqual('deny', hook('after-export'))

    def test_tidy_real_scale_has_constant_full_reads(self):
        from collections import Counter
        import copy, contextlib
        from unittest.mock import patch
        from memlet import memlet_dream as md
        repo = self.make_repo()
        snapshot = {'session%03d.log' % i: [] for i in range(300)}
        where = []
        for i in range(2000):
            f = 'session%03d.log' % (i % 300)
            snapshot[f].append('2026-01-01T00:00:00Z [user] Never retry token%04d.' % (i // 2))
            where.append((f, len(snapshot[f])))
        for f, lines in snapshot.items():
            self.snapshot(repo, f, *lines)
        nodes = []
        for i, (f, line) in enumerate(where):
            ident = 'DRM-%04d' % i
            pin = md.resolve_pin(snapshot, '%s:%d' % (f, line))
            e = dict(edgeId='E'+ident, edgeType='BLOCKS', fromId=ident, toId='BLOCKS:'+ident,
                     targetLabel='retry', srcPin=pin, srcTimestamp='2026-01-01T00:00:00Z', edgeVerdict='VALID')
            n = dict(id=ident, title='retry', category='repeated_pitfall', statusKind='open_trap', scopeTag='billing.py',
                     primaryPin=pin, timestamp='2026-01-01T00:00:00Z', allPins=[pin], edges=[e], verdict='SUPPORTED', approvedPaths=['billing.py'])
            n['approval'] = dict(schema='memlet.dream_approval.v1', decision='approved', by='Fixture', at='2026-02-01T00:00:00Z', digest=md.node_approval_digest(n), paths=['billing.py'])
            e['approval'] = dict(schema='memlet.dream_approval.v1', decision='approved', by='Fixture', at='2026-02-01T00:00:00Z', digest=md.edge_approval_digest(e))
            nodes.append(n)
        for i in range(0, 1000, 2):
            nodes[i+1]['merge'] = dict(schema='memlet.dream_merge.v1', into=nodes[i]['id'], by='Fixture', at='2026-03-01T00:00:00Z', memberDigest=md.merge_approval_digest(nodes[i+1]), keepDigest=md.merge_approval_digest(nodes[i]))
        graph = dict(schema=md.DREAM_ONTOLOGY_SCHEMA, nodes=nodes, edges=[copy.deepcopy(n['edges'][0]) for n in nodes])
        md._commit_queue_and_db(repo / mb.QUEUE_REL, json.dumps(graph), repo / mb.DB_REL, graph)
        counts = Counter()
        def measured(name):
            real = getattr(mb, name)
            def call(*args, **kwargs):
                counts[name] += 1
                self.assertLessEqual(counts[name], 4, (name, counts))
                return real(*args, **kwargs)
            return call
        with contextlib.ExitStack() as stack:
            for name in ('_load_queue', '_load_snapshot', 'dream_tiers'):
                stack.enter_context(patch.object(mb, name, measured(name)))
            report = mt.tidy_report(repo)
        self.assertTrue(report['ok'], report)
        self.assertEqual(40, len(report['duplicates']))
        self.assertEqual(460, report['omitted']['duplicates'])
        self.assertEqual({'_load_queue': 1, '_load_snapshot': 3, 'dream_tiers': 3}, dict(counts))

    def test_merged_lost_files_section_is_capped_with_real_members(self):
        from memlet import vocabulary as v
        repo = self.make_repo()
        for i in range(3):
            text = 'Never omit unique review %d.' % i
            a = self.learn(repo, 'keep%d.log' % i, '2026-11-01T00:00:00Z [user] ' + text)[0]
            b = self.learn(repo, 'member%d.log' % i, '2026-11-02T00:00:00Z [user] ' + text)[0]
            mb.learn_approve(repo, [a], approver='Robin', paths=['anchor.py'])
            mb.learn_approve(repo, [b], approver='Robin', paths=['billing.py'])
            mb.learn_merge(repo, a, [b], approver='Robin')
        full = mt.tidy_report(repo)
        self.assertEqual(3, len(full['mergedLostFiles']))
        with mock.patch.object(v, 'TIDY_SECTION_CAP', 2):
            capped = mt.tidy_report(repo)
        self.assertEqual(full['mergedLostFiles'][:2], capped['mergedLostFiles'])
        self.assertEqual(1, capped['omitted']['mergedLostFiles'])
        for entry in full['mergedLostFiles']:
            self.printed(entry['command'])
        self.assertEqual([], mt.tidy_report(repo)['mergedLostFiles'])

    def test_skill_build_to_rename_holds_repository_lock_for_two_writers(self):
        from memlet import memlet_dream as md
        import threading
        from concurrent.futures import ThreadPoolExecutor
        repo, a, b, c = self.fixture()
        mb.learn_approve(repo, [a, b, c], approver='Robin')
        target = self.base / 'concurrent'
        real_build, real_replace = sb.build_skill, sb.os.replace
        key = str(md.queue_lock_path(repo / mb.QUEUE_REL))
        events = []
        def build(*args, **kwargs):
            self.assertGreater(getattr(md._LOCK_DEPTH, 'held', {}).get(key, 0), 0)
            result = real_build(*args, **kwargs)
            events.append(('build', result['items']))
            return result
        def replace(src, dst):
            if str(dst).endswith('/SKILL.md'):
                self.assertGreater(getattr(md._LOCK_DEPTH, 'held', {}).get(key, 0), 0)
                events.append(('rename', None))
            return real_replace(src, dst)
        with mock.patch.object(sb, 'build_skill', build), mock.patch.object(sb.os, 'replace', replace):
            with ThreadPoolExecutor(max_workers=2) as pool:
                def newer():
                    mb.learn_merge(repo, a, [b], approver='Robin')
                    return sb.write_skill(repo, out=target)
                older = pool.submit(sb.write_skill, repo, out=target)
                newest = pool.submit(newer)
                self.assertTrue(older.result()['ok'])
                self.assertTrue(newest.result()['ok'])
        self.assertEqual(['build', 'rename', 'build', 'rename'], [e[0] for e in events])
        self.assertTrue(sb.check_skill(target / 'memlet-lessons/SKILL.md', repo_path=repo)['current'])
        self.assertNotIn(b, (target / 'memlet-lessons/SKILL.md').read_text())

    def test_multiline_merged_listing_has_separate_quotes_and_citations(self):
        from memlet import memlet_dreamcycle as dc
        repo = self.make_repo()
        for name in ('first', 'second'):
            source = self.base / (name + '.log')
            source.write_text('2026-11-01T10:00:00Z [user] The nightly import broke because the cache was cold.\n'
                              '2026-11-01T10:05:00Z [user] Warming the cache first made the nightly import work.\n')
            dc.dream_ingest(repo, source)
        batch = dc.dream_next(repo)
        spans = ['%s:%d' % (r[0], r[1]) for r in batch['rows']]
        candidates = [dict(category='verified_workaround', title='warm cache', scope_tag='billing.py',
                           src_spans=spans[i:i+2], edges=[dict(edge_type='RESOLVES', target='nightly import',
                           src_span=spans[i+1], target_span=spans[i])]) for i in (0, 2)]
        result = dc.dream_submit(repo, batch['runId'], batch['batchId'], candidates, proposer='host')
        self.assertTrue(result['ok'], result)
        ids = [i['id'] for i in mb.learn_pending(repo)['pendingNodes']]
        self.assertEqual(2, len(ids))
        mb.learn_merge(repo, ids[0], [ids[1]], approver='Robin')
        result = mb.learn_pending(repo, _record_review=False)
        group = result['mergedLessons'][0]
        text = result['formatted'].split('## Merged lessons', 1)[1]
        self.assertNotIn('⟶', text)
        for item in [group] + group['members']:
            self.assertEqual(2, len(item['quoteLines']))
            for quote, cite in zip(item['quoteLines'], item['cites']):
                self.assertIn(quote, text)
                self.assertIn('{file}:{start}-{end}'.format(**cite), text)

    def test_new_command_help_and_error_messages(self):
        for command, flags in [('tidy', ['--repo', '--stale-days', '--min-seen', '--format']),
                               ('skillbook', ['--repo', '--name', '--max-lines', '--budget', '--write', '--out', '--force', '--check', '--format'])]:
            code, out, err = run_cli(['admin', command, '--help'])
            self.assertEqual((0, ''), (code, err))
            for flag in flags:
                line = next(line for line in out.splitlines() if line.lstrip().startswith(flag))
                self.assertGreater(len(line.split('  ')[-1]), 1)
            self.assertIn('default:', out)
        code, out, err = run_cli(['admin', 'tidy', '--min-seen', '0'])
        self.assertEqual((2, ''), (code, out))
        self.assertIn('--min-seen', err)


if __name__ == '__main__':
    unittest.main()
