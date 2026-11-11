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
        argv = [a.replace('NAME', 'Ray') for a in argv[1:]]
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
        self.assertTrue(mb.learn_reject(repo, [first], approver='Ray', reason='not a rule')['ok'])
        report = mt.tidy_report(repo)
        # a rejected lesson is not offered as a duplicate at all
        self.assertEqual([], [g for g in report['duplicates'] if first in [g['keep']] + list(g['same'])])
        refused = mb.merge_blockers(repo, first, [second])
        self.assertTrue(refused['ok'])  # computed; refused through blockers
        self.assertTrue(refused['blockers'][second])
        code, out, err = run_cli(['learn', '--merge', first, '--same', second, '--by', 'Ray', '--repo', str(repo)])
        self.assertNotEqual(0, code)
        self.assertEqual('none', mb.merge_view(repo)['states'][second]['state'])

    def test_a_lapsed_merge_is_reported_and_the_member_is_shown_again(self):
        repo, first, second, third = self.fixture()
        self.assertTrue(mb.learn_merge(repo, first, [second], approver='Ray')['ok'])
        self.assertNotIn(second, self.shown(repo))
        self.assertTrue(mb.learn_reject(repo, [first], approver='Ray', reason='changed my mind')['ok'])
        self.assertIn(second, self.shown(repo))  # nothing a person did not decide stays hidden
        report = mt.tidy_report(repo)
        lapsed = {e['id']: e for e in report['lapsedMerges']}
        self.assertIn(second, lapsed)
        self.assertEqual(first, lapsed[second]['into'])
        self.assertEqual('keep-rejected', lapsed[second]['reason'])
        for command in lapsed[second].get('commands') or []:
            argv = shlex.split(command)
            self.assertEqual('memlet', argv[0])
            memory_cli._build_parser().parse_args([a.replace('NAME', 'Ray') for a in argv[1:]])  # accepted by the real parser

    def test_the_skill_file_leaves_out_a_merged_member_and_brings_it_back_when_the_merge_lapses(self):
        repo, first, second, third = self.fixture()
        self.assertTrue(mb.learn_approve(repo, [first, second, third], approver='Ray')['ok'])
        both = sb.build_skill(repo)
        self.assertTrue(both['ok'], both)
        for ident in (first, second, third):
            self.assertIn('lesson %s ' % ident, both['text'])
        self.assertTrue(mb.learn_merge(repo, first, [second], approver='Ray')['ok'])
        merged = sb.build_skill(repo)
        self.assertIn('lesson %s ' % first, merged['text'])
        self.assertNotIn(second, merged['text'])  # not as an item, a label or a citation
        self.assertEqual(both['items'] - 1, merged['items'])
        self.assertTrue(mb.learn_unmerge(repo, [second], approver='Ray')['ok'])
        self.assertEqual(both['text'], sb.build_skill(repo)['text'])  # same state, same bytes

    def test_tidy_stays_read_only_with_merges_present(self):
        repo, first, second, third = self.fixture()
        self.assertTrue(mb.learn_merge(repo, first, [second], approver='Ray')['ok'])
        before = tidy_tests.tree_state(repo)
        for fmt in ('json', 'markdown'):
            code, out, err = run_cli(['admin', 'tidy', '--repo', str(repo), '--format', fmt])
            self.assertEqual((0, ''), (code, err))
            self.assertTrue(out.strip())
            self.assertEqual(before, tidy_tests.tree_state(repo), fmt)


if __name__ == '__main__':
    unittest.main()
