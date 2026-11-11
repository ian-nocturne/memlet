"""9.3 tidy report (unit N2): read-only proposals to merge, look at again, retire or promote. No vendors, no writes."""
import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory_cli
from memlet import cli_plugins, memlet_bridge as mb, memlet_dream as md, memlet_dreamcycle as dc
from memlet import memlet_tidy as mt, memlet_wiki as mw, vocabulary as v

CLI = Path(__file__).resolve().parent / 'memory_cli.py'
LINE = 'Never call refund() in billing.py without the retry wrapper.'
FRIDAY = 'Do not deploy on fridays from the main branch ever.'
UTC = datetime.timezone.utc


def at(text):
    return datetime.datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=UTC)


@contextlib.contextmanager
def merge_absent():
    """This build may or may not carry unit N1's merge functions; force the absent case."""
    saved = {name: getattr(mb, name) for name in ('merge_view', 'merge_blockers') if hasattr(mb, name)}
    for name in saved:
        delattr(mb, name)
    try:
        yield
    finally:
        for name, fn in saved.items():
            setattr(mb, name, fn)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memlet-n2-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        for p in ('home', 'tmp'):
            (self.base / p).mkdir()
        env = dict(os.environ, HOME=str(self.base / 'home'), TMPDIR=str(self.base / 'tmp'),
                   CLAUDE_CONFIG_DIR=str(self.base / 'home/claude'), CODEX_HOME=str(self.base / 'home/codex'),
                   GIT_CEILING_DIRECTORIES=str(self.base), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                   MEMLET_LOCK_DIR=str(self.base / 'locks'), MEMLET_DREAMING='0',
                   MEMLET_HOME=str(self.base / 'unused'), MEMLET_TRANSCRIPT_DIRS=str(self.base))
        for key in ('PYTHONHOME', 'PYTHONEXECUTABLE', '__PYVENV_LAUNCHER__', 'CODEX_THREAD_ID', 'MEMLET_PARENT_SESSION_ID'):
            env.pop(key, None)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(tempfile, 'tempdir', str(self.base / 'tmp'))
        patch.start()
        self.addCleanup(patch.stop)
        absent = merge_absent()
        absent.__enter__()
        self.addCleanup(absent.__exit__, None, None, None)

    def git(self, repo, *args):
        subprocess.run(['git', '-C', str(repo)] + list(args), check=True, capture_output=True)

    def make_repo(self, name='repo', agents=None):
        repo = self.base / name
        repo.mkdir()
        (repo / 'AGENTS.md').write_text(agents or '# Rules\n- Always keep `anchor.py::guard` enabled.\n')
        (repo / 'anchor.py').write_text('def guard():\n    return True\n')
        (repo / 'billing.py').write_text('def refund(amount):\n    return amount\n')
        self.git(repo, 'init', '-q')
        self.git(repo, 'add', '.')
        self.git(repo, '-c', 'user.name=F', '-c', 'user.email=f@example.invalid',
                 '-c', 'core.hooksPath=' + str(self.base / 'no-hooks'), 'commit', '-qm', 'fixture')
        self.assertTrue(mw.init_project(repo)['ok'])
        return repo

    def learn(self, repo, name, *lines):
        t = self.base / name
        t.write_text(''.join(line + '\n' for line in lines))
        result = mb.learn_from_transcript(repo, t)
        self.assertTrue(result['ok'], result)
        return result['addedNodeIds']

    def node_ids_by_line(self, repo):
        return {md.lesson_lines(n)[0]['quote']: n['id'] for n in mb._load_queue(repo)[0]['nodes']}

    def snapshot(self, repo, name, *lines):
        p = repo / mb.SNAPSHOT_REL / name
        p.write_text(''.join(line + '\n' for line in lines))
        return p

    def save_graph(self, repo, graph):
        previous = md.graph_sha256(mb._load_queue(repo)[0])
        md._commit_queue_and_db(repo / mb.QUEUE_REL, json.dumps(graph), repo / mb.DB_REL, graph, replaces_sha=previous)

    def set_decision_time(self, repo, ids, stamp):
        graph = mb._load_queue(repo)[0]
        for n in graph['nodes']:
            if n['id'] in ids:
                n['approval']['at'] = stamp  # outside the approval digest
        self.save_graph(repo, graph)

    def report(self, repo, **kw):
        kw.setdefault('clock', lambda: at('2026-12-01T00:00:00Z'))
        result = mt.tidy_report(repo, **kw)
        self.assertTrue(result['ok'], result)
        return result

    def ids(self, entries):
        return [e['id'] for e in entries]

    def argv(self, cmd, repo):
        """The printed command as a shell would split it, with its trailing `--repo DIR` checked and removed."""
        argv = shlex.split(cmd)
        self.assertEqual(['--repo', str(repo)], argv[-2:], cmd)
        return argv[:-2]


class SaidAgain(Base):
    def test_user_lines_only_and_assistant_repeat_is_not_said_again(self):
        repo = self.make_repo()
        first = self.learn(repo, 's1.log', '2026-11-01T10:00:00Z [user] ' + LINE)[0]
        self.snapshot(repo, 's2.log-0000000001.log', '2026-11-02T10:00:00Z [assistant] ' + LINE)
        self.assertEqual(1, mt.lesson_sightings(repo)['sightings'][first]['sessions'])
        self.assertNotIn(first, self.ids(self.report(repo)['saidAgain']))
        self.snapshot(repo, 's3.log-0000000002.log', '2026-11-03T10:00:00Z [user]   never CALL refund() in billing.py without the retry wrapper.')
        seen = mt.lesson_sightings(repo)['sightings'][first]
        self.assertEqual(2, seen['sessions'])
        self.assertEqual(('2026-11-01T10:00:00Z', '2026-11-03T10:00:00Z'), (seen['first'], seen['last']))
        self.assertEqual([{'file': 's1.log-' + mb._sha(str(self.base / 's1.log'))[:10] + '.log', 'line': 1},
                          {'file': 's3.log-0000000002.log', 'line': 1}], seen['where'])
        entry = self.report(repo)['saidAgain'][0]
        self.assertEqual((first, 'pending', 2, 2), (entry['id'], entry['tier'], entry['sessions'], entry['sessionsLabel']))
        self.assertEqual(['memlet', 'learn', '--approve', first, '--by', 'NAME'], self.argv(entry['command'], repo))

    def test_rewritten_transcript_is_one_session(self):
        repo = self.make_repo()
        t = self.base / 's1.log'
        nid = self.learn(repo, 's1.log', '2026-11-01T10:00:00Z [user] ' + LINE)[0]
        t.write_text('2026-11-01T09:00:00Z [user] hello\n2026-11-01T10:00:00Z [user] ' + LINE + '\n')
        self.assertTrue(mb.learn_from_transcript(repo, t)['ok'])
        files = sorted(p.name for p in (repo / mb.SNAPSHOT_REL).iterdir())
        self.assertEqual(2, len(files))
        self.assertEqual({'s1.log'}, {mt.session_key(f) for f in files})
        self.assertEqual(1, mt.lesson_sightings(repo)['sightings'][nid]['sessions'])
        self.assertEqual('sub/x.log', mt.session_key('sub/x.log-0123456789-abcdef0123.log'))
        self.assertEqual('notes.log', mt.session_key('notes.log'))

    def test_every_cited_line_of_a_multi_line_lesson_must_be_said(self):
        repo = self.make_repo()
        source = self.base / 'multi.log'
        source.write_text('2026-11-01T10:00:00Z [user] The smoke test fails when the cache is cold.\n'
                          '2026-11-01T10:01:00Z [user] Warm the cache with the prime script before every deploy.\n')
        self.assertTrue(dc.dream_ingest(repo, source)['ok'])
        fid = dc.dream_next(repo)['rows'][0][0]
        spans = [fid + ':1', fid + ':2']
        candidate = [{'category': 'verified_workaround', 'title': 'warm cache', 'scope_tag': 'deploy',
                      'src_spans': spans, 'edges': [{'edge_type': 'RESOLVES', 'target': 'cold cache',
                                                     'src_span': spans[1], 'target_span': spans[0]}]}]
        self.assertTrue(dc.dream_run(repo, via=('claude',), runner=lambda *a, **k: json.dumps(candidate))['ok'])
        nid = mb._load_queue(repo)[0]['nodes'][0]['id']
        self.snapshot(repo, 'only-one.log-0000000003.log', '2026-11-02T10:00:00Z [user] The smoke test fails when the cache is cold.')
        self.assertEqual(1, mt.lesson_sightings(repo)['sightings'][nid]['sessions'])
        self.snapshot(repo, 'both.log-0000000004.log', '2026-11-03T10:00:00Z [user] Warm the cache with the prime script before every deploy.',
                      '2026-11-03T10:05:00Z [user] The smoke test fails when the cache is cold.')
        seen = mt.lesson_sightings(repo)['sightings'][nid]
        self.assertEqual(2, seen['sessions'])
        self.assertEqual('2026-11-03T10:05:00Z', seen['last'])

    def test_rejected_lesson_said_again_only_after_its_rejection(self):
        repo = self.make_repo()
        nid = self.learn(repo, 's1.log', '2026-05-01T10:00:00Z [user] ' + LINE)[0]
        self.assertTrue(mb.learn_reject(repo, [nid], approver='Ray', reason='no')['ok'])
        self.set_decision_time(repo, {nid}, '2026-06-01T00:00:00Z')
        self.snapshot(repo, 'before.log-0000000005.log', '2026-05-20T10:00:00Z [user] ' + LINE)
        self.assertEqual([], self.report(repo)['saidAgain'])
        self.snapshot(repo, 'future.log-0000000006.log', '2027-05-20T10:00:00Z [user] ' + LINE)  # after the clock
        self.assertEqual([], self.report(repo)['saidAgain'])
        self.snapshot(repo, 'after.log-0000000007.log', '2026-07-01T10:00:00Z [user] ' + LINE)
        entry = self.report(repo)['saidAgain'][0]
        self.assertEqual((nid, 'rejected', 1, '2026-06-01T00:00:00Z'),
                         (entry['id'], entry['tier'], entry['sessionsAfter'], entry['rejectedAt']))
        self.assertEqual(['memlet', 'learn', '--approve', nid, '--by', 'NAME', '--reconsider'], self.argv(entry['command'], repo))
        self.assertIn(v.TIDY_SAID_AGAIN_REJECTED % (1, '2026-06-01T00:00:00Z'), entry['label'])


class Duplicates(Base):
    def test_same_and_similar_text_and_the_word_count_floor(self):
        repo = self.make_repo()
        self.learn(repo, 'a.log', '2026-11-03T10:00:00Z [user] ' + LINE,
                   '2026-11-03T10:01:00Z [user] Never run the migration script on the production database.',
                   '2026-11-03T10:02:00Z [user] Never deploy on fridays.')
        self.learn(repo, 'b.log', '2026-11-01T10:00:00Z [user] ' + LINE,
                   '2026-11-01T10:01:00Z [user] Never run the migration script on the production database again.',
                   '2026-11-01T10:02:00Z [user] Never deploy on fridays!')
        ids = self.node_ids_by_line(repo)
        line_ids = sorted(i for q, i in ids.items() if q.endswith(LINE))
        late = ids['2026-11-03T10:00:00Z [user] ' + LINE]
        early = ids['2026-11-01T10:00:00Z [user] ' + LINE]
        groups = self.report(repo)['duplicates']
        same = next(g for g in groups if g['kind'] == 'same-text')
        self.assertEqual(sorted(line_ids), sorted([same['keep']] + same['same']))
        self.assertEqual(early, same['keep'])  # earliest line timestamp among pending lessons
        similar = next(g for g in groups if g['kind'] == 'similar-text')
        self.assertEqual(v.TIDY_DUPLICATE_LABELS['similar-text'], similar['label'])
        self.assertEqual(2, len(similar['lessons']))
        self.assertEqual(2, len(groups))  # four words are never similar-text, even with equal word sets
        self.assertNotIn('blockers', same)
        self.assertTrue(mb.learn_approve(repo, [late], approver='Ray')['ok'])
        same = next(g for g in self.report(repo)['duplicates'] if g['kind'] == 'same-text')
        self.assertEqual(late, same['keep'])  # an approved lesson first
        self.assertEqual(['memlet', 'learn', '--merge', late, '--same', early, '--by', 'NAME'],
                         self.argv(same['command'], repo))


class RetireUnattachedPromote(Base):
    def approved(self, repo, *names_lines):
        out = []
        for name, line in names_lines:
            out += self.learn(repo, name, line)
        self.assertTrue(mb.learn_approve(repo, out, approver='Ray')['ok'])
        return out

    def test_now_a_rule_compares_instruction_rules_not_the_promoted_page(self):
        repo = self.make_repo(agents='# Rules\n- ' + FRIDAY + '\n')
        friday, refund = self.approved(repo, ('s1.log', '2026-11-01T10:00:00Z [user] ' + FRIDAY),
                                       ('s2.log', '2026-11-01T10:00:00Z [user] ' + LINE))
        self.assertIn(LINE, (repo / mb.PROMOTED_PAGE).read_text())
        retire = {e['id']: e for e in self.report(repo)['retire']}
        self.assertEqual(['now-a-rule'], retire[friday]['reasons'])
        self.assertEqual(['AGENTS.md:2'], retire[friday]['details']['rules'])
        self.assertNotIn(refund, retire)
        self.assertEqual(['memlet', 'learn', '--reject', friday, '--by', 'NAME', '--reason', 'tidy: now-a-rule'],
                         self.argv(retire[friday]['command'], repo))

    def test_attachment_inactive_and_anchored_code_changed(self):
        repo = self.make_repo()
        (repo / 'notes.py').write_text('x = 1\n')
        refund = self.approved(repo, ('s1.log', '2026-11-01T10:00:00Z [user] ' + LINE))[0]
        self.assertTrue(mb.learn_paths(repo, [refund], approver='Ray', paths=['notes.py'])['ok'])
        self.assertIn('memlet:anchor billing.py::refund', (repo / mb.PROMOTED_PAGE).read_text())
        self.assertEqual([], self.report(repo)['retire'])
        (repo / 'notes.py').unlink()
        (repo / 'billing.py').write_text('def refund(account, amount, currency):\n    return amount\n')
        entry = self.report(repo)['retire'][0]
        self.assertEqual(['attachment-inactive', 'anchored-code-changed'], entry['reasons'])
        self.assertEqual(['notes.py'], entry['details']['inactivePaths'])
        self.assertEqual({'anchor': 'billing.py::refund', 'reason': 'signature_drift'}, entry['details']['anchor'])

    def test_old_unseen_uses_the_injected_clock(self):
        repo = self.make_repo()
        nid = self.approved(repo, ('s1.log', '2026-01-01T10:00:00Z [user] ' + LINE))[0]
        self.set_decision_time(repo, {nid}, '2026-02-01T00:00:00Z')
        self.assertEqual([], self.report(repo, clock=lambda: at('2026-07-01T00:00:00Z'))['retire'])
        late = lambda: at('2026-09-01T00:00:00Z')  # 212 days after approval
        entry = self.report(repo, clock=late)['retire'][0]
        self.assertEqual(['old-unseen'], entry['reasons'])
        self.assertEqual('2026-02-01T00:00:00Z', entry['details']['approvedAt'])
        self.assertEqual([], self.report(repo, clock=late, stale_days=400)['retire'])
        self.assertEqual([], self.report(repo, clock=late, max_bytes=1)['retire'])  # an incomplete scan never judges
        self.snapshot(repo, 'later.log-0000000008.log', '2026-03-01T10:00:00Z [user] ' + LINE)
        self.assertEqual([], self.report(repo, clock=late)['retire'])

    def test_unattached_and_promote(self):
        repo = self.make_repo()
        friday, refund = self.approved(repo, ('s1.log', '2026-11-01T10:00:00Z [user] ' + FRIDAY),
                                       ('s2.log', '2026-11-01T10:00:00Z [user] ' + LINE))
        report = self.report(repo)
        self.assertEqual([friday], self.ids(report['unattached']))
        self.assertEqual(['memlet', 'learn', '--paths', friday, '--by', 'NAME', '--path', 'FILE'],
                         self.argv(report['unattached'][0]['command'], repo))
        self.assertEqual([], report['promote'])
        for n in range(2):
            self.snapshot(repo, 'again%d.log-000000001%d.log' % (n, n), '2026-11-0%dT10:00:00Z [user] %s' % (n + 2, LINE),
                          '2026-11-0%dT10:00:00Z [user] %s' % (n + 2, FRIDAY))
        report = self.report(repo)
        self.assertEqual([refund], self.ids(report['promote']))  # friday reaches no file
        entry = report['promote'][0]
        self.assertEqual((3, ['billing.py'], ['AGENTS.md']), (entry['sessions'], entry['paths'], entry['instructionFiles']))
        self.assertEqual('billing.py::refund', entry['suggestedAnchor'])
        self.assertEqual('- ' + LINE + ' (`billing.py::refund`)\n', entry['block'])
        self.assertEqual([], self.report(repo, min_seen=4)['promote'])
        # The anchor comes from the promotion's own resolver when the page has none for the lesson.
        with mock.patch.object(mt, '_promoted_anchors', return_value=({}, True)), \
                mock.patch.object(mb, '_resolve_lesson_anchor', return_value=('anchor.py', 'guard')) as resolver:
            entry = self.report(repo)['promote'][0]
        self.assertEqual('anchor.py::guard', entry['suggestedAnchor'])
        self.assertEqual(1, resolver.call_count)
        self.assertIn(v.TIDY_PROMOTE_SUGGESTION, self.report(repo)['formatted'])


class Bounds(Base):
    def test_caps_and_omitted(self):
        repo = self.make_repo()
        ids = []
        for f in range(3):
            ids += self.learn(repo, 'cap%d.log' % f, *['2026-11-01T10:%02d:00Z [user] Never touch the folder number %d%02d please.'
                                                       % (n, f, n) for n in range(15)])
        self.assertEqual(45, len(ids))
        self.assertTrue(mb.learn_approve(repo, ids, approver='Ray')['ok'])
        report = self.report(repo)
        self.assertEqual(40, len(report['unattached']))
        self.assertEqual(5, report['omitted']['unattached'])
        self.assertEqual(0, report['omitted']['retire'])
        self.assertIn(v.TIDY_OMITTED % 5, report['formatted'])
        self.assertEqual(set(v.TIDY_SECTIONS), set(report['omitted']))

    def test_tiny_max_bytes_is_incomplete_and_labelled_at_least(self):
        repo = self.make_repo()
        nid = self.learn(repo, 's1.log', '2026-11-01T10:00:00Z [user] ' + LINE)[0]
        self.snapshot(repo, 'big.log-0000000009.log', *['2026-11-02T10:00:00Z [user] padding %d' % n for n in range(200)])
        self.snapshot(repo, 's2.log-0000000010.log', '2026-11-02T10:00:00Z [user] ' + LINE)
        self.snapshot(repo, 's3.log-0000000011.log', '2026-11-03T10:00:00Z [user] ' + LINE)
        small = (repo / mb.SNAPSHOT_REL / 's2.log-0000000010.log').stat().st_size
        seen = mt.lesson_sightings(repo, max_bytes=3 * small)
        self.assertFalse(seen['complete'])
        self.assertLessEqual(seen['work']['bytesRead'], 3 * small)
        self.assertEqual(1, seen['skipped']['overBudget'])
        report = self.report(repo, max_bytes=3 * small)
        self.assertFalse(report['complete'])
        self.assertIn(v.TIDY_INCOMPLETE, report['notices'])
        entry = next(e for e in report['saidAgain'] if e['id'] == nid)
        self.assertEqual('at least 3', entry['sessionsLabel'])
        self.assertTrue(self.report(repo)['complete'])

    def test_symlink_huge_line_invalid_utf8_and_unreadable_file(self):
        repo = self.make_repo()
        nid = self.learn(repo, 's1.log', '2026-11-01T10:00:00Z [user] ' + LINE)[0]
        snap = repo / mb.SNAPSHOT_REL
        outside = self.base / 'outside.log'
        outside.write_text('2026-11-02T10:00:00Z [user] ' + LINE + '\n')
        (snap / 'link.log-0000000012.log').symlink_to(outside)
        (snap / 'linkdir').symlink_to(self.base, target_is_directory=True)
        (snap / 'huge.log-0000000013.log').write_bytes(b'[user] ' + b'x' * (mt.MAX_LINE_BYTES + 1) + b'\n'
                                                        + ('[user] ' + LINE + '\n').encode())
        (snap / 'bad.log-0000000014.log').write_bytes(b'[user] \xff\xfe broken\n')
        locked = snap / 'locked.log-0000000015.log'
        locked.write_text('[user] ' + LINE + '\n')
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        readable = os.access(str(locked), os.R_OK)  # a superuser can still read it
        seen = mt.lesson_sightings(repo)
        skipped = seen['skipped']
        self.assertEqual(2, skipped['symlinks'])
        self.assertEqual(1, skipped['hugeLines'])
        self.assertEqual(1, skipped['invalidUtf8Lines'])
        self.assertEqual(0 if readable else 1, skipped['unreadable'])
        self.assertEqual(4 if readable else 3, seen['work']['filesOpened'])
        self.assertEqual(seen['scanned']['files'], seen['work']['filesOpened'])
        self.assertEqual((3 if readable else 2), seen['sightings'][nid]['sessions'])  # s1 and huge; never the link
        self.assertEqual(readable, seen['complete'])

    def test_each_snapshot_is_opened_once_per_report(self):
        repo = self.make_repo()
        self.learn(repo, 's1.log', *['2026-11-01T10:%02d:00Z [user] Never touch the folder number %02d please.' % (n, n)
                                    for n in range(12)])
        for n in range(3):
            self.snapshot(repo, 'x%d.log-00000000%02d.log' % (n, 20 + n), '2026-11-02T10:00:00Z [user] Never touch the folder number 01 please.',
                          '2026-11-02T10:00:00Z [assistant] unrelated')
        opened = []
        real_open = os.open
        def spy(path, *a, **k):
            if str(path).startswith(str(repo / mb.SNAPSHOT_REL)) and sys._getframe(1).f_code.co_name == '_read_file':
                opened.append(Path(path).name)
            return real_open(path, *a, **k)
        with mock.patch.object(mt.os, 'open', spy):
            report = self.report(repo)
        self.assertEqual(4, len(opened))
        self.assertEqual(len(opened), len(set(opened)))
        self.assertEqual({'filesListed': 4, 'filesOpened': 4, 'linesRead': 18, 'linesCompared': 15},
                         {k: report['work'][k] for k in ('filesListed', 'filesOpened', 'linesRead', 'linesCompared')})
        self.assertEqual(sum((repo / mb.SNAPSHOT_REL / f).stat().st_size for f in opened), report['work']['bytesRead'])


class Hostile(Base):
    def test_commands_run_only_memlet_with_hostile_bytes_as_one_argument(self):
        name = "repo $(touch pwned) `id`; 'q'\nnext"
        repo = self.make_repo(name=name)
        hostile = "Never call x; rm -rf / $(touch pwned) `id` 'it' \"q\" | cat > out & ok."
        odd = 'odd $(touch pwned); name.py'
        (repo / odd).write_text('x = 1\n')
        self.git(repo, 'add', odd)
        keep = self.learn(repo, 's1.log', '2026-11-01T10:00:00Z [user] ' + hostile)[0]
        other = self.learn(repo, 's2.log', '2026-11-02T10:00:00Z [user] ' + hostile)[0]
        self.assertTrue(mb.learn_approve(repo, [keep], approver='Ray', paths=[odd])['ok'])
        (repo / odd).unlink()  # attachment-inactive: the hostile path appears in details
        def view(repo_path, *, context=None):
            return {'ok': True, 'states': {}, 'members': {}}
        def blockers(repo_path, keep_id, same, *, context=None):
            return {'ok': True, 'keep': keep_id, 'blockers': {}, 'lostFiles': {m: [odd] for m in same}}
        with mock.patch.object(mb, 'merge_view', view, create=True), \
                mock.patch.object(mb, 'merge_blockers', blockers, create=True):
            report = self.report(str(repo))
        found = []
        def walk(value):
            if isinstance(value, dict):
                for k, x in value.items():
                    if k in ('command', 'pathsCommand') and isinstance(x, str):
                        found.append(x)
                    elif k == 'commands':
                        found.extend(x)
                    else:
                        walk(x)
            elif isinstance(value, list):
                for x in value:
                    walk(x)
        walk(report)
        self.assertGreaterEqual(len(found), 5)
        for cmd in found:
            argv = shlex.split(cmd)
            self.assertEqual('memlet', argv[0], cmd)
            self.assertEqual(str(repo), argv[argv.index('--repo') + 1])
            lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            self.assertEqual(argv, list(lexer), cmd)  # no unquoted operator anywhere
        paths = shlex.split(next(g for g in report['duplicates'])['pathsCommand'])
        self.assertEqual([odd], [paths[i + 1] for i, a in enumerate(paths) if a == '--path'])
        self.assertTrue(any(keep == e['id'] for e in report['retire']))
        self.assertIn(other, [x for g in report['duplicates'] for x in g['same']])
        for line in report['formatted'].splitlines():
            if hostile in line:
                self.assertTrue(line.startswith('    > ') or line.startswith('  > '), line)
        self.assertFalse((self.base / 'pwned').exists())


class Merge(Base):
    def setup(self):
        repo = self.make_repo()
        keep = self.learn(repo, 's1.log', '2026-11-01T10:00:00Z [user] ' + LINE)[0]
        member = self.learn(repo, 's2.log', '2026-11-02T10:00:00Z [user] Never call refund() from billing.py unless wrapped in retry.')[0]
        lapsed = self.learn(repo, 's3.log', '2026-11-03T10:00:00Z [user] ' + FRIDAY)[0]
        twin = self.learn(repo, 's4.log', '2026-11-04T10:00:00Z [user] ' + FRIDAY)[0]
        self.assertTrue(mb.learn_approve(repo, [keep], approver='Ray')['ok'])
        return repo, keep, member, lapsed, twin

    def test_absent_merge_functions_are_reported(self):
        repo, keep, member, lapsed, twin = self.setup()
        report = self.report(repo)
        self.assertIn(v.TIDY_MERGE_UNAVAILABLE, report['notices'])
        self.assertFalse(report['mergeChecks'])
        self.assertEqual([], report['lapsedMerges'])
        self.assertTrue(report['duplicates'])
        for group in report['duplicates']:
            self.assertNotIn('blockers', group)
            self.assertNotIn('lostFiles', group)
        self.assertIn(v.TIDY_MERGE_UNAVAILABLE, mt.lesson_sightings(repo)['notices'])

    def test_present_merge_functions_follow_the_contract_shapes(self):
        repo, keep, member, lapsed, twin = self.setup()
        calls = []
        def view(repo_path, *, context=None):
            calls.append(('view', Path(repo_path)))
            return {'ok': True, 'states': {member: {'state': 'merged', 'into': keep, 'reason': None},
                                           lapsed: {'state': 'lapsed', 'into': keep, 'reason': 'keep-changed'},
                                           keep: {'state': 'none', 'into': None, 'reason': None}},
                    'members': {keep: [member]}}
        def blockers(repo_path, keep_id, same, *, context=None):
            calls.append(('blockers', keep_id, tuple(same)))
            return {'ok': True, 'keep': keep_id, 'blockers': {m: ['member-approved-keep-not'] for m in same},
                    'lostFiles': {m: ['anchor.py'] for m in same}}
        with mock.patch.object(mb, 'merge_view', view, create=True), \
                mock.patch.object(mb, 'merge_blockers', blockers, create=True):
            report = self.report(repo)
            self.assertEqual(1, sum(c[0] == 'view' for c in calls))  # one merge read per report
            seen = mt.lesson_sightings(repo)['sightings']
        self.assertTrue(report['mergeChecks'])
        self.assertNotIn(v.TIDY_MERGE_UNAVAILABLE, report['notices'])
        self.assertEqual(2, seen[keep]['sessions'])  # the member's session counts for the kept lesson
        self.assertEqual(1, seen[member]['sessions'])
        everywhere = json.dumps({k: report[k] for k in ('duplicates', 'saidAgain', 'retire', 'unattached', 'promote')})
        self.assertNotIn(member, everywhere)
        self.assertEqual([lapsed], self.ids(report['lapsedMerges']))
        entry = report['lapsedMerges'][0]
        self.assertEqual((keep, 'keep-changed'), (entry['into'], entry['reason']))
        self.assertEqual(['memlet', 'learn', '--unmerge', lapsed, '--by', 'NAME'], self.argv(entry['command'], repo))
        self.assertEqual(['memlet', 'learn', '--merge', keep, '--same', lapsed, '--by', 'NAME'],
                         self.argv(entry['commands'][1], repo))
        group = report['duplicates'][0]
        self.assertEqual(sorted([lapsed, twin]), sorted([group['keep']] + group['same']))
        self.assertEqual({m: ['member-approved-keep-not'] for m in group['same']}, group['blockers'])
        self.assertEqual({m: ['anchor.py'] for m in group['same']}, group['lostFiles'])
        self.assertNotIn('pathsCommand', group)  # the kept lesson is not approved
        self.assertIn(('blockers', group['keep'], tuple(group['same'])), calls)
        md_text = report['formatted']
        self.assertIn(v.TIDY_BLOCKERS_LABEL, md_text)
        self.assertIn(v.TIDY_LAPSED_LABEL % (keep, 'keep-changed'), md_text)

    def test_unreadable_merge_view_is_said(self):
        repo, *_ = self.setup()
        with mock.patch.object(mb, 'merge_view', lambda *a, **k: {'ok': False}, create=True), \
                mock.patch.object(mb, 'merge_blockers', lambda *a, **k: {}, create=True):
            report = self.report(repo)
        self.assertIn(v.TIDY_MERGE_UNREADABLE, report['notices'])
        self.assertFalse(report['mergeChecks'])


def tree_state(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames.sort()
        for name in sorted(filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]):
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            data = os.readlink(p).encode() if os.path.islink(p) else Path(p).read_bytes()
            out[os.path.relpath(p, str(root))] = (hashlib.sha256(data).hexdigest(), st.st_mtime_ns, st.st_mode)
        for d in dirnames:
            p = os.path.join(dirpath, d)
            out[os.path.relpath(p, str(root)) + '/'] = (None, os.lstat(p).st_mtime_ns, os.lstat(p).st_mode)
    return out


def run_cli(argv, plugins=None):
    out, err = io.StringIO(), io.StringIO()
    patches = [mock.patch.object(sys, 'argv', ['memlet'] + argv)]
    if plugins is not None:
        patches.append(mock.patch.object(cli_plugins, 'load', return_value=plugins))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        try:
            code = memory_cli.cli()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class ReadOnlyAndCompatibility(Base):
    def fixture(self):
        repo = self.make_repo(agents='# Rules\n- Always keep `anchor.py::guard` enabled.\n- ' + FRIDAY + '\n')
        a = self.learn(repo, 's1.log', '2026-11-01T10:00:00Z [user] ' + LINE, '2026-11-01T10:01:00Z [user] ' + FRIDAY)
        self.learn(repo, 's2.log', '2026-11-02T10:00:00Z [user] ' + LINE)
        self.assertTrue(mb.learn_approve(repo, a, approver='Ray')['ok'])
        mw.build_handover(repo, write=True)
        return repo

    def test_report_and_cli_leave_repository_and_memlet_byte_identical(self):
        repo = self.fixture()
        ledger = repo / mw.LEDGER_PATH
        with contextlib.closing(sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True)) as conn:
            tables = conn.execute('SELECT name, sql FROM sqlite_master ORDER BY name').fetchall()
        before = tree_state(repo)
        self.assertIn(mb.QUEUE_REL, before)
        self.assertIn(mw.LEDGER_PATH, before)
        connects = []
        real = sqlite3.connect
        def spy(target, *a, **k):
            connects.append((str(target), k.get('uri')))
            return real(target, *a, **k)
        with mock.patch.object(sqlite3, 'connect', spy):
            report = self.report(repo)
        self.assertTrue(report['duplicates'] and report['retire'] and report['saidAgain'])
        for target, uri in connects:
            self.assertTrue(uri and target.endswith('?mode=ro'), target)
        self.assertEqual(before, tree_state(repo))
        result = subprocess.run([sys.executable, str(CLI), 'admin', 'tidy', '--repo', str(repo), '--format', 'markdown'],
                                cwd=str(repo), env=dict(os.environ), capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(result.stdout.startswith(v.TIDY_HEAD + '\n\n' + v.TIDY_NOTHING_CHANGED + '\n'))
        self.assertEqual(before, tree_state(repo))
        with contextlib.closing(real(ledger.as_uri() + '?mode=ro', uri=True)) as conn:
            self.assertEqual(tables, conn.execute('SELECT name, sql FROM sqlite_master ORDER BY name').fetchall())
        self.assertEqual(before, tree_state(repo))

    def test_existing_commands_print_the_same_bytes_with_the_tidy_plug_in(self):
        repo = self.fixture()
        backup = self.base / 'backup'
        shutil.copytree(str(repo), str(backup), symlinks=True)
        everything = cli_plugins.load()
        without = [p for p in everything if p.__name__ != 'memlet.cli_tidy']
        self.assertEqual(len(everything) - 1, len(without))
        commands = [['check', str(repo), '--format', 'json'], ['check', '--changed', '--repo', str(repo)],
                    ['recall', 'refund retry', '--repo', str(repo)], ['learn', '--pending', '--repo', str(repo)],
                    ['handover', str(repo), '--format', 'markdown'], ['dream', str(repo), '--status'],
                    ['admin', 'graph', 'stats', '--repo', str(repo)],
                    ['admin', 'graph', 'show', 'billing.py', '--repo', str(repo), '--format', 'markdown']]
        cwd = os.getcwd()
        os.chdir(str(repo))
        self.addCleanup(os.chdir, cwd)
        for argv in commands:
            outputs = []
            for plugins in (without, everything):
                shutil.rmtree(str(repo))
                shutil.copytree(str(backup), str(repo), symlinks=True)
                outputs.append(run_cli(argv, plugins))
            # Elapsed-time fields (`...Ms`) differ between any two runs, with or without the plug-in.
            outputs = [(code, re.sub(r'"(\w*Ms)": [0-9.]+', r'"\1": 0', out), err) for code, out, err in outputs]
            with self.subTest(argv=argv[:3]):
                self.assertTrue(outputs[0][1].strip())
                self.assertEqual(outputs[0], outputs[1])
        code, out, _ = run_cli(['admin', '--help'], everything)
        self.assertIn('tidy', out.split('Rules/wiki:', 1)[1].split('\n\n', 1)[0])
        meta = cli_plugins.commands(everything)['tidy']
        self.assertEqual((False, 'Rules/wiki', v.TIDY_CLI_HELP), (meta['product'], meta['group'], meta['help']))

    def test_cli_json_and_argument_errors(self):
        repo = self.fixture()
        code, out, err = run_cli(['admin', 'tidy', '--repo', str(repo), '--format', 'json'])
        self.assertEqual(0, code, err)
        data = json.loads(out)
        self.assertEqual('tidy', data['command'])
        for key in ('duplicates', 'saidAgain', 'retire', 'unattached', 'promote', 'lapsedMerges', 'omitted',
                    'complete', 'notices', 'formatted'):
            self.assertIn(key, data)
        code, out, _ = run_cli(['admin', 'tidy', '--repo', str(repo), '--min-seen', '0'])
        self.assertEqual(2, code)
        self.assertEqual(v.TIDY_INVALID_ARGUMENT, json.loads(out)['error'])
        self.assertFalse(mt.tidy_report(repo, stale_days=-1)['ok'])
        empty = self.make_repo(name='empty')
        report = self.report(empty)
        self.assertEqual([], report['duplicates'] + report['saidAgain'] + report['promote'])
        self.assertTrue(report['formatted'].startswith(v.TIDY_HEAD + '\n\n' + v.TIDY_NOTHING_CHANGED))


if __name__ == '__main__':
    unittest.main()
