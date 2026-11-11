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


@contextlib.contextmanager
def merge_stub(view, blockers):
    with mock.patch.object(mb, 'merge_view', view, create=True), \
            mock.patch.object(mb, 'merge_blockers', blockers, create=True):
        yield


def no_blockers(repo_path, keep, same, *, context=None):
    return {'ok': True, 'keep': keep, 'blockers': {}, 'lostFiles': {}}


def tree_state(root):
    """Content hash, mtime and mode of every file and directory below `root` (and of `root` itself)."""
    out = {'.': (None, os.lstat(str(root)).st_mtime_ns, os.lstat(str(root)).st_mode)}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames.sort()
        for name in sorted(filenames + dirnames):
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            data = (None if os.path.isdir(p) and not os.path.islink(p)
                    else hashlib.sha256(os.readlink(p).encode() if os.path.islink(p) else Path(p).read_bytes()).hexdigest())
            out[os.path.relpath(p, str(root))] = (data, st.st_mtime_ns, st.st_mode)
    return out


def all_commands(report):
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
    return found


_PARSER = []


def parser():
    if not _PARSER:
        with mock.patch.object(sys, 'argv', ['memlet']):
            _PARSER.append(memory_cli._build_parser())
    return _PARSER[0]


def subparser(name):
    import argparse
    sub = next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction))
    return sub.choices[name]


def parser_has(command, option):
    return any(option in a.option_strings for a in subparser(command)._actions)


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
        p.parent.mkdir(parents=True, exist_ok=True)
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

    def pinned(self, repo, name, lines, candidates):
        """A queue built by the real pinning and validation code from a snapshot file; no model."""
        p = self.snapshot(repo, name, *lines)
        graph = md.dream_pin_and_validate(candidates(p.name), {p.name: p.read_text().splitlines()},
                                          vendor_origin='fixture')
        (repo / mb.QUEUE_REL).write_text(json.dumps(graph))
        return graph

    def report(self, repo, **kw):
        kw.setdefault('clock', lambda: at('2026-12-01T00:00:00Z'))
        result = mt.tidy_report(repo, **kw)
        self.assertTrue(result['ok'], result)
        return result

    def ids(self, entries):
        return [e['id'] for e in entries]

    def parse(self, cmd):
        """The printed command through the real memlet argument parser (parse only, nothing runs)."""
        argv = shlex.split(cmd)
        self.assertEqual('memlet', argv[0], cmd)
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        self.assertEqual(argv, list(lexer), cmd)  # no unquoted shell operator anywhere
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                ns = parser().parse_args(argv[1:])
            except SystemExit:
                self.fail('parser refused %r: %s' % (cmd, err.getvalue()))
        sub = subparser(argv[1])
        for token in argv[2:]:
            if token.startswith('--') and '=' in token:
                flag, value = token.split('=', 1)
                action = next(a for a in sub._actions if flag in a.option_strings)
                bound = getattr(ns, action.dest)
                self.assertTrue(bound == value or (isinstance(bound, list) and value in bound), (cmd, flag, bound))
        return ns

    def check_commands(self, report):
        """Every printed command parses and binds; merge commands only where this build's parser has them."""
        found = all_commands(report)
        merge_supported = parser_has('learn', '--merge')
        for cmd in found:
            if not merge_supported and any(t.startswith(('--merge', '--unmerge', '--same'))
                                           for t in shlex.split(cmd)):
                continue
            self.parse(cmd)
        return found


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
        report = self.report(repo)
        entry = report['saidAgain'][0]
        self.assertEqual((first, 'pending', 2, 2), (entry['id'], entry['tier'], entry['sessions'], entry['sessionsLabel']))
        ns = self.parse(entry['command'])
        self.assertEqual(([first], 'NAME', str(repo)), (ns.approve, ns.by, ns.repo))
        self.assertIn('pending lessons and unreviewed hints', report['formatted'])

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
        self.assertEqual('x.log', mt.session_key('x.log-0123456789-abcdef0123.log'))
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
        ns = self.parse(entry['command'])
        self.assertEqual(([nid], True), (ns.approve, ns.reconsider))
        self.assertIn(v.TIDY_SAID_AGAIN_REJECTED % (1, '2026-06-01T00:00:00Z'), entry['label'])


class Supersession(Base):
    """F2: every line comes from the shared reader's projection, with its roles."""
    OLD = 'Never call refund() in billing.py without the old wrapper.'
    NEW = 'Never call refund() in billing.py without the new wrapper.'

    def incoming(self, agents, name='repo'):
        repo = self.make_repo(name=name, agents=agents)
        self.pinned(repo, 'pair-0123456789.log', ['2026-01-01T00:00:00Z [user] ' + self.OLD,
                                                  '2026-02-01T00:00:00Z [user] ' + self.NEW],
                    lambda f: [{'lesson_id': 'OLD', 'category': 'repeated_pitfall', 'scope_tag': 'billing.py',
                                'title': 'old rule', 'src_spans': [f + ':1']},
                               {'lesson_id': 'NEW', 'category': 'verified_workaround', 'scope_tag': 'billing.py',
                                'title': 'new rule', 'src_spans': [f + ':2'],
                                'edges': [{'edge_type': 'SUPERSEDES', 'to_id': 'OLD', 'src_span': f + ':2',
                                           'target_span': f + ':1'}]}])
        self.assertTrue(mb.learn_approve(repo, ['OLD', 'NEW'], approver='Ray')['ok'])
        for i in range(2):
            self.snapshot(repo, 'oldrepeat%d-0123456789.log' % i, '2026-03-01T00:00:00Z [user] ' + self.OLD)
        return repo

    def test_incoming_replacement_is_never_counted_promoted_or_a_rule_for_its_old_line(self):
        repo = self.incoming('# Rules\n- ' + self.OLD + '\n')
        reader = {i['id']: i for i in mb.dream_items(repo)['items']}
        self.assertEqual(['replaced', 'current'], reader['OLD']['lineRoles'])
        seen = mt.lesson_sightings(repo)['sightings']
        self.assertEqual((1, 1), (seen['OLD']['sessions'], seen['NEW']['sessions']))  # the old repeats never count
        report = self.report(repo, min_seen=1)
        for entry in report['promote']:
            self.assertNotIn(self.OLD, entry['block'] or '')
        self.assertNotIn('OLD', self.ids(report['promote']))  # a quarantined lesson is never a rule
        self.assertNotIn('OLD', self.ids(report['retire']))   # the old line equals a rule: not a reason
        repo2 = self.incoming('# Rules\n- ' + self.NEW + '\n', name='repo2')
        retire = {e['id']: e for e in self.report(repo2)['retire']}
        self.assertEqual(['now-a-rule'], retire['OLD']['reasons'])
        self.assertEqual([{'line': self.NEW, 'rule': 'AGENTS.md:2'}], retire['OLD']['details']['rules'])
        self.assertEqual(['replaced', 'current'], retire['OLD']['lineRoles'])
        text = self.report(repo2)['formatted']
        self.assertIn('  > ' + v.REPLACED_MARKER + ' ' + self.OLD, text)
        self.assertIn('  > ' + v.CURRENT_MARKER + ' ' + self.NEW, text)

    def test_chained_replacement_a_b_c(self):
        repo = self.make_repo()
        quotes = [self.OLD, 'Never call refund() in billing.py without the middle wrapper.',
                  'Never call refund() in billing.py without the final wrapper.']
        names = []
        for i, q in enumerate(quotes):
            names.append(self.snapshot(repo, 'chain%d-0123456789.log' % i, '2026-01-0%dT00:00:00Z [user] %s' % (i + 1, q)).name)
        mapping = {n: (repo / mb.SNAPSHOT_REL / n).read_text().splitlines() for n in names}
        cands = []
        for i in range(2):
            a, b = names[i] + ':1', names[i + 1] + ':1'
            cands.append({'lesson_id': 'PAIR%d' % i, 'category': 'superseded_directive', 'title': 'replace',
                          'scope_tag': 'billing.py', 'src_spans': [a, b], 'superseded_by_src': b,
                          'edges': [{'edge_type': 'SUPERSEDES', 'target': 'deploy', 'src_span': b, 'target_span': a}]})
        (repo / mb.QUEUE_REL).write_text(json.dumps(md.dream_pin_and_validate(cands, mapping)))
        self.assertTrue(mb.learn_approve(repo, ['PAIR0', 'PAIR1'], approver='Ray')['ok'])
        for i in range(2):
            self.snapshot(repo, 'middle-repeat%d-0123456789.log' % i, '2026-03-01T00:00:00Z [user] ' + quotes[1])
        reader = {i['id']: i for i in mb.dream_items(repo)['items']}
        self.assertEqual(['replaced', 'replaced'], reader['PAIR0']['lineRoles'])
        seen = mt.lesson_sightings(repo)['sightings']
        self.assertEqual(0, seen['PAIR0']['sessions'])  # no current line left
        self.assertEqual(1, seen['PAIR1']['sessions'])  # the middle line is replaced for PAIR1 too
        report = self.report(repo, min_seen=1)
        self.assertEqual([], report['duplicates'])
        self.assertEqual([], report['promote'])


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
        late = ids['2026-11-03T10:00:00Z [user] ' + LINE]
        early = ids['2026-11-01T10:00:00Z [user] ' + LINE]
        report = self.report(repo)
        groups = report['duplicates']
        same = next(g for g in groups if g['kind'] == 'same-text')
        self.assertEqual((early, [late]), (same['keep'], same['same']))  # earliest line time among pending
        similar = next(g for g in groups if g['kind'] == 'similar-text')
        self.assertEqual(v.TIDY_DUPLICATE_LABELS['similar-text'], similar['label'])
        self.assertEqual(2, len(similar['lessons']))
        self.assertEqual(2, len(groups))  # four words are never similar-text, even with equal word sets
        self.assertNotIn('blockers', same)
        self.assertIsNone(same['command'])  # no merge in this build: no command the parser would refuse
        self.assertEqual(v.TIDY_MERGE_COMMAND_UNAVAILABLE, same['commandNote'])
        self.assertTrue(mb.learn_approve(repo, [late], approver='Ray')['ok'])
        same = next(g for g in self.report(repo)['duplicates'] if g['kind'] == 'same-text')
        self.assertEqual(late, same['keep'])  # an approved lesson first
        self.assertTrue(mb.learn_reject(repo, [early], approver='Ray', reason='x')['ok'])
        self.assertNotIn(early, json.dumps(self.report(repo)['duplicates']))  # rejected lessons never take part

    def test_every_member_reaches_the_threshold_against_its_kept_lesson(self):
        repo = self.make_repo()
        texts = ['Never deploy alpha beta gamma', 'Never deploy alpha beta gamma delta', 'Never deploy alpha beta delta']
        ids = [self.learn(repo, 's%d.log' % i, '2026-01-0%dT00:00:00Z [user] %s' % (i + 1, t))[0]
               for i, t in enumerate(texts)]
        groups = self.report(repo)['duplicates']
        self.assertEqual([(ids[0], [ids[1]])], [(g['keep'], g['same']) for g in groups])  # C is 0.67 from A: not in

        def jac(a, b):
            x, y = set(mt._WORD_RE.findall(a.lower())), set(mt._WORD_RE.findall(b.lower()))
            return len(x & y) / len(x | y)
        self.assertLess(jac(texts[0], texts[2]), mt.SIMILAR_JACCARD)
        self.assertGreaterEqual(jac(texts[0], texts[1]), mt.SIMILAR_JACCARD)


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
        self.assertEqual([{'line': FRIDAY, 'rule': 'AGENTS.md:2'}], retire[friday]['details']['rules'])
        self.assertNotIn(refund, retire)
        ns = self.parse(retire[friday]['command'])
        self.assertEqual(([friday], 'tidy: now-a-rule'), (ns.reject, ns.reason))

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
        self.assertFalse(mt.tidy_report(repo, clock=late, stale_days=10 ** 100)['ok'])
        self.assertEqual([], self.report(repo, clock=late, stale_days=mt.MAX_STALE_DAYS)['retire'])
        self.snapshot(repo, 'later.log-0000000008.log', '2026-03-01T10:00:00Z [user] ' + LINE)
        self.assertEqual([], self.report(repo, clock=late)['retire'])

    def test_unattached_and_promote(self):
        repo = self.make_repo()
        friday, refund = self.approved(repo, ('s1.log', '2026-11-01T10:00:00Z [user] ' + FRIDAY),
                                       ('s2.log', '2026-11-01T10:00:00Z [user] ' + LINE))
        report = self.report(repo)
        self.assertEqual([friday], self.ids(report['unattached']))
        ns = self.parse(report['unattached'][0]['command'])
        self.assertEqual(([friday], ['FILE']), (ns.paths, ns.path))
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
        self.assertFalse(entry['escaped'])
        self.assertEqual(v.TIDY_PROMOTE_COMMAND_NOTE, entry['commandLabel'])
        self.parse(entry['command'])
        self.assertEqual([], self.report(repo, min_seen=4)['promote'])
        with mock.patch.object(mt, '_promoted_anchors', return_value=({}, {}, 'none')), \
                mock.patch.object(mb, '_resolve_lesson_anchor', return_value=('anchor.py', 'guard')) as resolver:
            entry = self.report(repo)['promote'][0]
        self.assertEqual('anchor.py::guard', entry['suggestedAnchor'])
        self.assertEqual(1, resolver.call_count)
        self.assertIn(v.TIDY_PROMOTE_SUGGESTION, self.report(repo)['formatted'])

    def test_promote_block_is_complete_escaped_or_omitted_whole(self):
        repo = self.make_repo()
        long_line = ('Never call refund() in billing.py ' + 'carefully ' * 900
                     + 'unless the emergency operator explicitly authorizes it.')
        markup = 'Never call refund() in billing.py with <!-- hidden --> parameters.'
        huge = 'Never call refund() in billing.py ' + 'slowly ' * 1800 + 'at all.'
        self.assertGreater(len(long_line), md.LESSON_TEXT_MAX_CHARS)
        self.assertGreater(len(huge), mt.MAX_BLOCK_CHARS)
        ids = self.approved(repo, ('l.log', '2026-11-01T10:00:00Z [user] ' + long_line),
                            ('m.log', '2026-11-01T10:00:00Z [user] ' + markup),
                            ('h.log', '2026-11-01T10:00:00Z [user] ' + huge))
        promote = {e['id']: e for e in self.report(repo, min_seen=1)['promote']}
        self.assertIn(long_line, promote[ids[0]]['block'])
        self.assertFalse(promote[ids[0]]['escaped'])
        self.assertTrue(promote[ids[0]]['quoteLines'][0].endswith('…'))  # the display form stays the reader's
        self.assertIn('&lt;!-- hidden --&gt;', promote[ids[1]]['block'])
        self.assertTrue(promote[ids[1]]['escaped'])
        self.assertEqual(v.TIDY_BLOCK_ESCAPED, promote[ids[1]]['escapedNote'])
        self.assertIsNone(promote[ids[2]]['block'])
        self.assertEqual(v.TIDY_BLOCK_TOO_LONG % mt.MAX_BLOCK_CHARS, promote[ids[2]]['blockNote'])
        text = self.report(repo, min_seen=1)['formatted']
        self.assertIn(v.TIDY_BLOCK_ESCAPED, text)
        self.assertIn(v.TIDY_BLOCK_TOO_LONG % mt.MAX_BLOCK_CHARS, text)


class Bounds(Base):
    def test_every_section_is_capped_with_an_omitted_count(self):
        pair = ['Never touch the folder one in prod.', 'Never touch the folder two in prod.']
        rules = ['Never call refund() in billing.py on fridays.', 'Never edit billing.py without review.']
        free = ['Never trust the cache on mondays at all.', 'Never trust the queue on tuesdays at all.']
        repo = self.make_repo(agents='# Rules\n' + ''.join('- %s\n' % r for r in rules))
        for i in range(2):  # two pending pairs: two duplicate groups, four said-again entries
            self.learn(repo, 'p%d.log' % i, *['2026-11-0%dT10:00:00Z [user] %s' % (i + 1, t) for t in pair])
        approved = self.learn(repo, 'r.log', *['2026-11-01T11:00:00Z [user] ' + t for t in rules + free])
        self.assertTrue(mb.learn_approve(repo, approved, approver='Ray')['ok'])
        ids = self.node_ids_by_line(repo)
        lapsed = [ids['2026-11-01T11:00:00Z [user] ' + t] for t in free]
        keep = ids['2026-11-01T11:00:00Z [user] ' + rules[0]]

        def view(repo_path, *, context=None):
            return {'ok': True, 'states': {i: {'state': 'lapsed', 'into': keep, 'reason': 'keep-changed'} for i in lapsed},
                    'members': {}}
        with merge_stub(view, no_blockers):
            full = self.report(repo, min_seen=1)
            with mock.patch.object(v, 'TIDY_SECTION_CAP', 1):
                capped = self.report(repo, min_seen=1)
        for section in v.TIDY_SECTIONS:
            with self.subTest(section=section):
                self.assertGreaterEqual(len(full[section]), 2)
                self.assertEqual(1, len(capped[section]))
                self.assertEqual(len(full[section]) - 1, capped['omitted'][section])

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

    def test_any_unconsidered_evidence_is_incomplete_and_never_proves_absence(self):
        """F4: a huge line, an undecodable line, `.jsonl`, a subdirectory, a symlink, an unreadable file."""
        repo = self.make_repo()
        nid = self.learn(repo, 's1.log', '2026-01-01T10:00:00Z [user] ' + LINE)[0]
        self.assertTrue(mb.learn_approve(repo, [nid], approver='Ray')['ok'])
        self.set_decision_time(repo, {nid}, '2026-02-01T00:00:00Z')
        snap = repo / mb.SNAPSHOT_REL
        self.assertEqual(['old-unseen'], self.report(repo)['retire'][0]['reasons'])
        outside = self.base / 'outside.log'
        outside.write_text('2026-06-01T10:00:00Z [user] ' + LINE + '\n')
        cases = {
            'hugeLines': lambda: (snap / 'huge-0000000013.log').write_bytes(
                ('2026-06-01T00:00:00Z [user] ' + ' ' * (mt.MAX_LINE_BYTES + 1) + LINE + '\n').encode()),
            'invalidUtf8Lines': lambda: (snap / 'bad-0000000014.log').write_bytes(b'[user] \xff\xfe broken\n'),
            'unsupported': lambda: (snap / 'later.jsonl').write_text(json.dumps(
                {'type': 'user', 'timestamp': '2026-06-01T00:00:00Z', 'message': {'role': 'user', 'content': LINE}}) + '\n'),
            'directories': lambda: self.snapshot(repo, 'sub/later-0000000015.log', '2026-06-01T10:00:00Z [user] ' + LINE),
            'symlinks': lambda: (snap / 'link-0000000016.log').symlink_to(outside),
        }
        for key, make in cases.items():
            with self.subTest(case=key):
                make()
                report = self.report(repo)
                self.assertFalse(report['complete'])
                self.assertEqual(1, report['skipped'][key])
                self.assertIn(v.TIDY_INCOMPLETE, report['notices'])
                self.assertEqual([], report['retire'])  # never old-unseen from partial evidence
                seen = mt.lesson_sightings(repo)
                self.assertFalse(seen['complete'])
                self.assertEqual(1, seen['sightings'][nid]['sessions'])  # never followed, never parsed
                for p in list(snap.iterdir()):
                    if not p.name.startswith('s1.log'):
                        shutil.rmtree(str(p)) if p.is_dir() and not p.is_symlink() else p.unlink()
        locked = snap / 'locked-0000000017.log'
        locked.write_text('[user] ' + LINE + '\n')
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        if not os.access(str(locked), os.R_OK):  # a superuser can still read it
            seen = mt.lesson_sightings(repo)
            self.assertEqual((False, 1), (seen['complete'], seen['skipped']['unreadable']))
        self.assertTrue(self.report(repo) and True)

    def test_snapshot_listing_and_file_count_are_bounded(self):
        repo = self.make_repo()
        nid = self.learn(repo, 's1.log', '2026-11-01T10:00:00Z [user] ' + LINE)[0]
        for n in range(5):
            self.snapshot(repo, 'many-%05d.log' % n, '[user] ' + LINE)
        with mock.patch.object(mt, 'MAX_FILES', 3):
            seen = mt.lesson_sightings(repo)
        self.assertFalse(seen['complete'])
        self.assertEqual(3, seen['work']['entriesListed'])
        self.assertLessEqual(seen['work']['filesOpened'], 3)
        self.assertEqual(1, seen['skipped']['overFileCount'])
        self.assertLessEqual(seen['sightings'][nid]['sessions'], 3)
        self.assertTrue(mt.lesson_sightings(repo)['complete'])

    def test_similarity_work_is_counted_and_bounded(self):
        repo = self.make_repo()
        self.learn(repo, 'a.log', '2026-11-01T10:00:00Z [user] Never run the migration script on the production database.')
        self.learn(repo, 'b.log', '2026-11-02T10:00:00Z [user] Never run the migration script on the production database again.')
        report = self.report(repo)
        self.assertEqual(1, len(report['duplicates']))
        self.assertGreater(report['work']['similarityWork'], 0)
        with mock.patch.object(mt, 'MAX_SIMILAR_WORK', 5):
            bounded = self.report(repo)
        self.assertEqual(5, bounded['work']['similarityWork'])
        self.assertFalse(bounded['complete'])
        self.assertIn(v.TIDY_SIMILAR_BOUND, bounded['notices'])

    def test_the_sightings_scan_opens_each_snapshot_once(self):
        """F7 as ruled: the scan opens each snapshot once; the shared reader's tier read is separate."""
        repo = self.make_repo()
        self.learn(repo, 's1.log', *['2026-11-01T10:%02d:00Z [user] Never touch the folder number %02d please.' % (n, n)
                                    for n in range(12)])
        for n in range(3):
            self.snapshot(repo, 'x%d.log-00000000%02d.log' % (n, 20 + n), '2026-11-02T10:00:00Z [user] Never touch the folder number 01 please.',
                          '2026-11-02T10:00:00Z [assistant] unrelated')
        opened = []
        real = mt._read

        def spy(dir_fd, name, *a):
            opened.append(name)
            return real(dir_fd, name, *a)
        with mock.patch.object(mt, '_read', spy):
            report = self.report(repo)
        self.assertEqual(4, len(opened))
        self.assertEqual(len(opened), len(set(opened)))
        self.assertEqual({'entriesListed': 4, 'filesOpened': 4, 'linesRead': 18, 'linesCompared': 15},
                         {k: report['work'][k] for k in ('entriesListed', 'filesOpened', 'linesRead', 'linesCompared')})
        self.assertEqual(sum((repo / mb.SNAPSHOT_REL / f).stat().st_size for f in opened), report['work']['bytesRead'])

    def test_repeated_lines_are_summarised_before_they_reach_lessons(self):
        """F8: 2,000 lessons wanting one quote said 20 times cost 2,000 hand-overs, not 40,000."""
        repo = self.make_repo()
        self.snapshot(repo, 'repeat-0123456789.log', *['[user] ' + LINE] * 20)
        wanted = {str(i): [md.normalised_quote(LINE)] for i in range(2000)}
        scan = mt._scan(repo, wanted, max_bytes=mt.MAX_BYTES, now=at('2026-12-01T00:00:00Z'))
        self.assertTrue(scan['complete'])
        self.assertEqual(2000, scan['work']['matchWork'])
        self.assertEqual(20, scan['work']['linesCompared'])
        self.assertEqual({1}, {len(r) for r in scan['records'].values()})
        with mock.patch.object(mt, 'MAX_MATCH_WORK', 1500):
            bounded = mt._scan(repo, wanted, max_bytes=mt.MAX_BYTES, now=at('2026-12-01T00:00:00Z'))
        self.assertFalse(bounded['complete'])
        self.assertEqual(1, bounded['skipped']['overMatchWork'])

    def test_a_replaced_snapshot_directory_is_never_followed(self):
        """F5: between listing and opening, the snapshot directory is swapped for a symlink to text outside it."""
        repo = self.make_repo()
        nid = self.learn(repo, 's1.log', '2026-11-01T10:00:00Z [user] ' + LINE)[0]
        snap = repo / mb.SNAPSHOT_REL
        self.snapshot(repo, 'race-0123456789.log', '[assistant] ' + LINE)
        outside = self.base / 'outside-dir'
        outside.mkdir()
        for name in os.listdir(str(snap)):
            (outside / name).write_text('[user] ' + LINE + '\n')
        real = mt._read
        swapped = []

        def intercept(dir_fd, name, *a):
            if not swapped:
                snap.rename(snap.with_name('snapshot-old'))
                snap.symlink_to(outside, target_is_directory=True)
                swapped.append(name)
            return real(dir_fd, name, *a)
        with mock.patch.object(mt, '_read', intercept):
            seen = mt.lesson_sightings(repo)
        self.assertTrue(swapped)
        self.assertEqual(1, seen['sightings'][nid]['sessions'])  # only the original s1 file, through its descriptor
        snap.unlink()
        snap.with_name('snapshot-old').rename(snap)
        os.makedirs(str(snap / 'sub'))
        (snap / 'sub' / 'race-0123456789.log').write_text('[user] ' + LINE + '\n')
        seen = mt.lesson_sightings(repo)
        self.assertEqual((1, False, 1), (seen['sightings'][nid]['sessions'], seen['complete'], seen['skipped']['directories']))


class Hostile(Base):
    def test_commands_parse_and_bind_with_hostile_ids_paths_and_repository(self):
        name = "repo $(touch pwned) `id`; 'q'\nnext"
        repo = self.make_repo(name=name)
        hostile = "Never call x; rm -rf / $(touch pwned) `id` 'it' \"q\" | cat > out & ok."
        odd = '-odd $(touch pwned); name.py'
        (repo / odd).write_text('x = 1\n')
        self.git(repo, 'add', '--', odd)
        keep = self.learn(repo, 's1.log', '2026-11-01T10:00:00Z [user] ' + hostile)[0]
        other = self.learn(repo, 's2.log', '2026-11-02T10:00:00Z [user] ' + hostile)[0]
        self.assertTrue(mb.learn_approve(repo, [keep], approver='Ray', paths=[odd])['ok'])
        # An approved id that looks like an option, accepted by the queue API.
        graph = mb._load_queue(repo)[0]
        dash = '--approve=VICTIM'
        for node in graph['nodes']:
            if node['id'] == keep:
                node['id'] = dash
                for e in node.get('edges', []):
                    if e.get('fromId') == keep:
                        e['fromId'] = dash
        for e in graph.get('edges', []):
            if e.get('fromId') == keep:
                e['fromId'] = dash
        self.save_graph(repo, graph)
        self.assertTrue(mb.learn_approve(repo, [dash], approver='Ray', paths=[odd], reconsider=True)['ok'])
        (repo / odd).unlink()  # attachment-inactive: the hostile path appears in details
        lost = ['-lost $(x).py']

        def blockers(repo_path, keep_id, same, *, context=None):
            return {'ok': True, 'keep': keep_id, 'blockers': {}, 'lostFiles': {m: lost for m in same}}
        with merge_stub(lambda repo_path, *, context=None: {'ok': True, 'states': {}, 'members': {}}, blockers):
            report = self.report(str(repo))
        found = self.check_commands(report)
        self.assertGreaterEqual(len(found), 4)
        retire = next(e for e in report['retire'] if e['id'] == dash)
        ns = self.parse(retire['command'])
        self.assertEqual(([dash], None, 'NAME', str(repo)), (ns.reject, ns.approve, ns.by, ns.repo))
        group = report['duplicates'][0]
        self.assertEqual((dash, [other]), (group['keep'], group['same']))
        ns = self.parse(group['pathsCommand'])
        self.assertEqual(([dash], [odd] + lost), (ns.paths, ns.path))
        self.assertEqual(['memlet', 'learn', '--merge=' + dash, '--same=' + other, '--by=NAME', '--repo=' + str(repo)],
                         shlex.split(group['command']))
        for line in report['formatted'].splitlines():
            if hostile in line:
                self.assertTrue(line.lstrip().startswith('> '), line)
        self.assertFalse((self.base / 'pwned').exists())

    def test_no_command_when_several_values_include_one_starting_with_a_dash(self):
        self.assertIsNone(mt.command(['learn'], [('--same', ['a', '-b'])]))
        self.assertIsNone(mt.command(['learn'], [('--reject', 'a\x00b')]))
        self.assertEqual("memlet learn --same=-b", mt.command(['learn'], [('--same', ['-b'])]))
        self.assertEqual('memlet learn --same a b', mt.command(['learn'], [('--same', ['a', 'b'])]))
        repo = self.make_repo()
        ids = [self.learn(repo, 's%d.log' % i, '2026-11-0%dT10:00:00Z [user] %s' % (i + 1, LINE))[0] for i in range(3)]
        graph = mb._load_queue(repo)[0]
        for node in graph['nodes']:
            if node['id'] == ids[2]:
                node['id'] = '-dash'
        self.save_graph(repo, graph)
        with merge_stub(lambda repo_path, *, context=None: {'ok': True, 'states': {}, 'members': {}}, no_blockers):
            report = self.report(repo)
        group = report['duplicates'][0]
        self.assertEqual(2, len(group['same']))
        self.assertIsNone(group['command'])
        self.assertEqual(v.TIDY_UNSAFE_ARGUMENT, group['commandNote'])
        self.assertIn(v.TIDY_NO_COMMAND_LABEL + v.TIDY_UNSAFE_ARGUMENT, report['formatted'])


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
        with merge_stub(view, blockers):
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
        self.assertEqual(['memlet', 'learn', '--unmerge=' + lapsed, '--by=NAME', '--repo=' + str(repo)],
                         shlex.split(entry['command']))
        self.assertEqual(['memlet', 'learn', '--merge=' + keep, '--same=' + lapsed, '--by=NAME', '--repo=' + str(repo)],
                         shlex.split(entry['commands'][1]))
        group = report['duplicates'][0]
        self.assertEqual(sorted([lapsed, twin]), sorted([group['keep']] + group['same']))
        self.assertEqual({m: ['member-approved-keep-not'] for m in group['same']}, group['blockers'])
        self.assertEqual({m: ['anchor.py'] for m in group['same']}, group['lostFiles'])
        self.assertNotIn('pathsCommand', group)  # the kept lesson is not approved
        self.assertIn(('blockers', group['keep'], tuple(group['same'])), calls)
        md_text = report['formatted']
        self.assertIn(v.TIDY_BLOCKERS_LABEL, md_text)
        self.assertIn(v.TIDY_LAPSED_LABEL % (keep, 'keep-changed'), md_text)

    def test_inconsistent_or_failing_merge_functions_hide_nothing(self):
        repo, keep, member, lapsed, twin = self.setup()
        baseline = self.report(repo)

        def sections(report):
            return {s: [e.get('id') or (e['keep'], e['same']) for e in report[s]] for s in v.TIDY_SECTIONS}

        def raising(*a, **k):
            raise RuntimeError('adapter failed')
        cases = {
            'not-ok': (lambda *a, **k: {'ok': False}, v.TIDY_MERGE_UNREADABLE),
            'raises': (raising, v.TIDY_MERGE_UNREADABLE),
            'unknown-keep': (lambda *a, **k: {'ok': True, 'states': {keep: {'state': 'merged', 'into': 'MISSING', 'reason': None}},
                                              'members': {'MISSING': [keep]}}, v.TIDY_MERGE_SKIPPED),
            'unknown-member': (lambda *a, **k: {'ok': True, 'states': {}, 'members': {keep: ['MISSING']}}, v.TIDY_MERGE_SKIPPED),
            'unknown-lapsed': (lambda *a, **k: {'ok': True, 'states': {'MISSING': {'state': 'lapsed', 'into': keep, 'reason': 'x'}},
                                                'members': {}}, v.TIDY_MERGE_SKIPPED),
            'bad-shape': (lambda *a, **k: {'ok': True, 'states': {member: 'merged'}, 'members': {}}, v.TIDY_MERGE_SKIPPED),
            'member-state-mismatch': (lambda *a, **k: {'ok': True, 'states': {member: {'state': 'lapsed', 'into': keep, 'reason': 'x'}},
                                                       'members': {keep: [member]}}, v.TIDY_MERGE_SKIPPED),
        }
        for label, (view, notice) in cases.items():
            with self.subTest(case=label):
                with merge_stub(view, no_blockers):
                    report = self.report(repo)
                    seen = mt.lesson_sightings(repo)['sightings']
                self.assertFalse(report['mergeChecks'])
                self.assertIn(notice, report['notices'])
                self.assertEqual([], report['lapsedMerges'])
                self.assertEqual((1, 1), (seen[keep]['sessions'], seen[member]['sessions']))
                self.assertEqual(sections(baseline), sections(report))  # nothing hidden, nothing added
        ok_view = lambda *a, **k: {'ok': True, 'states': {}, 'members': {}}
        for label, fn in (('raises', raising), ('not-ok', lambda *a, **k: {'ok': False, 'blockers': {'x': ['y']}}),
                          ('bad-key', lambda r, k, same, **kw: {'ok': True, 'blockers': {'NOT-A-MEMBER': ['y']}, 'lostFiles': {}})):
            with self.subTest(blockers=label):
                with merge_stub(ok_view, fn):
                    report = self.report(repo)
                self.assertFalse(report['mergeChecks'])
                self.assertIn(v.TIDY_BLOCKERS_FAILED, report['notices'])
                self.assertNotIn('blockers', report['duplicates'][0])
        refusal = lambda r, k, same, **kw: {'ok': True, 'keep': k, 'blockers': {m: ['keep-rejected'] for m in same},
                                            'lostFiles': {}}
        with merge_stub(ok_view, refusal):
            report = self.report(repo)
        self.assertTrue(report['mergeChecks'])
        self.assertEqual({report['duplicates'][0]['same'][0]: ['keep-rejected']}, report['duplicates'][0]['blockers'])


class ReadOnlyAndCompatibility(Base):
    def fixture(self):
        repo = self.make_repo(agents='# Rules\n- Always keep `anchor.py::guard` enabled.\n- ' + FRIDAY + '\n')
        a = self.learn(repo, 's1.log', '2026-11-01T10:00:00Z [user] ' + LINE, '2026-11-01T10:01:00Z [user] ' + FRIDAY)
        self.learn(repo, 's2.log', '2026-11-02T10:00:00Z [user] ' + LINE)
        self.assertTrue(mb.learn_approve(repo, a, approver='Ray')['ok'])
        mw.build_handover(repo, write=True)
        return repo

    def cli_state_unchanged(self, repo, *extra):
        for fmt in ('json', 'markdown'):
            before = tree_state(repo)
            result = subprocess.run([sys.executable, str(CLI), 'admin', 'tidy', '--repo', str(repo), '--format', fmt]
                                    + list(extra),
                                    cwd=str(repo), env=dict(os.environ), capture_output=True, text=True, timeout=120)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(before, tree_state(repo), fmt)
            yield fmt, result.stdout

    def test_report_and_cli_leave_repository_and_memlet_byte_identical(self):
        repo = self.fixture()
        ledger = repo / mw.LEDGER_PATH
        with contextlib.closing(sqlite3.connect(ledger.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
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
        self.assertTrue(connects)
        for target, uri in connects:
            self.assertTrue(uri and target.endswith('?mode=ro&immutable=1'), target)
        self.assertEqual(before, tree_state(repo))
        outputs = dict(self.cli_state_unchanged(repo))
        self.assertTrue(outputs['markdown'].startswith(v.TIDY_HEAD + '\n\n' + v.TIDY_NOTHING_CHANGED + '\n'))
        with contextlib.closing(real(ledger.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
            self.assertEqual(tables, conn.execute('SELECT name, sql FROM sqlite_master ORDER BY name').fetchall())
        self.assertEqual(before, tree_state(repo))

    def test_a_wal_mode_ledger_is_never_opened(self):
        """F1: SQLite may create `-wal` / `-shm` even for a read-only connection; the report must not open it."""
        repo = self.fixture()
        (repo / 'billing.py').write_text('def refund(account, amount, currency):\n    return amount\n')
        (repo / 'AGENTS.md').write_text('# Rules\n- ' + LINE + '\n')
        self.assertIn('memlet:anchor billing.py::refund', (repo / mb.PROMOTED_PAGE).read_text())
        self.assertIn('anchored-code-changed', json.dumps(self.report(repo)['retire']))  # judged while readable
        with contextlib.closing(sqlite3.connect(str(repo / mw.LEDGER_PATH))) as conn:
            self.assertEqual('wal', conn.execute('PRAGMA journal_mode=WAL').fetchone()[0])
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        for suffix in ('-wal', '-shm'):
            p = repo / (mw.LEDGER_PATH + suffix)
            if p.exists():
                p.unlink()
        self.assertEqual(2, (repo / mw.LEDGER_PATH).read_bytes()[18])
        before = tree_state(repo)
        outputs = dict(self.cli_state_unchanged(repo, '--min-seen', '2'))
        data = json.loads(outputs['json'])
        self.assertIn(v.TIDY_LEDGER_SIDE_EFFECTS, data['notices'])
        self.assertEqual([(None, v.TIDY_NOT_CHECKED)], [(e['suggestedAnchor'], e['anchorNote']) for e in data['promote']])
        anchors = [e['details'].get('anchor') for e in data['retire']]
        self.assertIn({'anchor': 'billing.py::refund', 'reason': v.TIDY_NOT_CHECKED}, anchors)
        self.assertNotIn('anchored-code-changed', json.dumps([e['reasons'] for e in data['retire']]))
        self.assertIn(v.TIDY_LEDGER_SIDE_EFFECTS, outputs['markdown'])
        for suffix in ('-wal', '-shm', '-journal'):
            self.assertFalse(os.path.lexists(str(repo / mw.LEDGER_PATH) + suffix))
        self.assertEqual(before, tree_state(repo))
        open(str(repo / mw.LEDGER_PATH) + '-journal', 'wb').close()  # a journal next to a rollback ledger
        self.assertEqual('side-effects', mt._ledger_rows(repo)[1])

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
        for days in ('1' + '0' * 100, '-1', str(mt.MAX_STALE_DAYS + 1)):
            code, out, err = run_cli(['admin', 'tidy', '--repo', str(repo), '--stale-days', days])
            self.assertEqual(2, code)
            self.assertIn('usage:', err)
            self.assertNotIn('Traceback', err)
        self.assertFalse(mt.tidy_report(repo, stale_days=-1)['ok'])
        empty = self.make_repo(name='empty')
        report = self.report(empty)
        self.assertEqual([], report['duplicates'] + report['saidAgain'] + report['promote'])
        self.assertTrue(report['formatted'].startswith(v.TIDY_HEAD + '\n\n' + v.TIDY_NOTHING_CHANGED))


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


if __name__ == '__main__':
    unittest.main()
