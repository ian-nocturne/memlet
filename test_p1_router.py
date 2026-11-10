"""Six-verb router contract: real compatibility cases and isolated bridge wiring."""
import argparse
import builtins
import contextlib
import io
import json
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import memory_cli
import prism_memory
from test_cli_compat import LEGACY_COMMANDS, Sandbox, _shape


class Output(io.StringIO):
    def __init__(self, tty=False):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


def invoke(argv, *, tty=False, program='memlet', suppress=True):
    out, err = Output(tty), io.StringIO()
    env = {key: value for key, value in os.environ.items()
           if key not in ('CODEX_THREAD_ID', 'PRISM_PARENT_SESSION_ID', 'ANTIGRAVITY_AGENT',
                          'MEMLET_NO_DEPRECATION')}
    if suppress:
        env['MEMLET_NO_DEPRECATION'] = '1'
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, 'argv', [program] + argv):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = memory_cli.cli()
            except SystemExit as exc:
                code = exc.code
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def cwd(path):
    previous = Path.cwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(str(previous))


class RouterHelpTests(unittest.TestCase):
    def test_top_help_has_exactly_seven_entries(self):
        code, out, err = invoke(['--help'])
        self.assertEqual((0, ''), (code, err))
        entries = re.findall(r'^    ([a-z][a-z-]*)\s{2,}\S', out, re.M)
        self.assertEqual(['init', 'check', 'recall', 'handover', 'learn', 'ui', 'admin'], entries)
        self.assertIn('memlet admin --help', out)
        for hidden in ('brief-build', 'dream-build', 'digest', 'viberescue'):
            self.assertNotIn(hidden, out)

    def test_every_legacy_name_parses_at_both_levels(self):
        for name in LEGACY_COMMANDS:
            for prefix in ([], ['admin']):
                with self.subTest(command=prefix + [name]):
                    code, out, err = invoke(prefix + [name, '--help'])
                    self.assertEqual((0, ''), (code, err))
                    self.assertIn('usage:', out)

    def test_admin_groups_cover_all_visible_legacy_names(self):
        code, out, err = invoke(['admin', '--help'])
        self.assertEqual((0, ''), (code, err))
        for label, _ in memory_cli.ADMIN_GROUPS:
            self.assertIn(label + ':', out)
        for name in set(LEGACY_COMMANDS) - {'brief-build'}:
            self.assertIn(name, out.split())
        self.assertNotIn('brief-build', out)
        self.assertIn('failed', invoke(['brief-build', '--help'])[1])

    def test_handover_default_help_hides_failed_modes_and_says_checksum(self):
        out = invoke(['handover', '--help'])[1]
        self.assertNotRegex(out, r'\b(?:digest|brief|signed|signature)\b')
        self.assertIn('checksum', out)
        parser = memory_cli._build_parser()
        for mode in ('digest', 'brief'):
            self.assertEqual(mode, parser.parse_args(['handover', '--mode', mode]).mode)

    def test_init_does_not_advertise_or_accept_hooks(self):
        self.assertNotIn('--hooks', invoke(['init', '--help'])[1])
        self.assertEqual(2, invoke(['init', '--hooks'])[0])

    def test_dream_build_help_explains_pending_jsonl(self):
        out = invoke(['admin', 'dream-build', '--help'])[1]
        self.assertIn('PENDING', out)
        self.assertIn('.jsonl', out)


class DeprecationTests(unittest.TestCase):
    def test_notice_exactly_once_on_stderr_and_suppressible(self):
        want = "memlet: 'schema' is now 'memlet admin schema'; the short form will be removed in a future release.\n"
        normal = invoke(['schema'], suppress=False)
        quiet = invoke(['schema'], suppress=True)
        admin = invoke(['admin', 'schema'], suppress=False)
        self.assertEqual(want, normal[2])
        self.assertEqual('', quiet[2])
        self.assertEqual('', admin[2])
        self.assertEqual(normal[:2], quiet[:2])
        self.assertEqual(quiet, admin)
        json.loads(normal[1])

    def test_legacy_help_warns_and_option_values_do_not_trigger_warnings(self):
        code, _, err = invoke(['--task', 'wiki', 'schema', '--help'], suppress=False)
        self.assertEqual(0, code)
        self.assertIn("'schema' is now", err)
        self.assertNotIn("'wiki' is now", err)
        self.assertEqual('', invoke(['--task', 'schema', 'recall', '--help'], suppress=False)[2])
        self.assertEqual('', invoke(['--help', 'schema'], suppress=False)[2])
        self.assertIn("'schema' is now", invoke(['--tas=foo', 'schema', '--help'], suppress=False)[2])

    def test_product_commands_and_admin_do_not_warn(self):
        for name in list(memory_cli.PRODUCT_COMMANDS) + ['admin']:
            self.assertEqual('', invoke([name, '--help'], suppress=False)[2])

    def test_legacy_executable_names_delegate_and_warn_once(self):
        expected = invoke(['schema'])
        for name in ('ieum-memory', 'prism-memory', 'deltaharness'):
            with self.subTest(program=name):
                code, out, err = invoke(['schema'], program='/bin/' + name, suppress=False)
                self.assertEqual(expected[:2], (code, out))
                self.assertEqual(name + " is now 'memlet'; this command name will be removed in a future release.\n", err)
                self.assertEqual(expected, invoke(['schema'], program=name))
        self.assertEqual('', invoke(['--version'], suppress=False)[2])

    def test_legacy_executable_restores_notice_environment_on_exit(self):
        for preset in (None, '0', '1'):
            with mock.patch.dict(os.environ), mock.patch.object(sys, 'argv', ['ieum-memory', '--help']):
                if preset is None:
                    os.environ.pop('MEMLET_NO_DEPRECATION', None)
                else:
                    os.environ['MEMLET_NO_DEPRECATION'] = preset
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        memory_cli.cli()
                self.assertEqual(preset, os.environ.get('MEMLET_NO_DEPRECATION'))

    def test_console_script_mappings_are_unchanged_per_coordinator_decision(self):
        project = (Path(__file__).parent / 'pyproject.toml').read_text(encoding='utf-8')
        for name in memory_cli.PROGRAM_NAMES:
            self.assertIn(name + ' = "memory_cli:cli"', project)
            self.assertIn(name + '-mcp = "prism_memory_mcp:main"', project)
        self.assertIn('viberescue = "prism_memory.viberescue:cli"', project)
        self.assertIn('anti-slop = "prism_memory.anti_slop:cli"', project)


class RealRouterTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def test_admin_equivalence_for_representative_real_commands(self):
        self.box.run(['init', '.'])
        store = ['--root', str(self.box.root), '--standalone']
        self.box.run(store + ['capture', '--text', 'Decision: use sqlite'])
        cases = [([], ['schema']), ([], ['wiki', '.']), ([], ['audit', '.']),
                 ([], ['import', '.', '--dry-run']), ([], ['rule-context', '.']),
                 ([], ['rule-recall', 'deploy release']), ([], ['rescue', '.']),
                 ([], ['anti-slop', '.']), ([], ['pack', '--list']),
                 ([], ['compare-bench', '--format', 'markdown']),
                 ([], ['opsec-fingerprint']), (store, ['brief']), (store, ['stats']),
                 (store, ['scenes']), (store, ['skills']), (store, ['cases']),
                 (store, ['links']), (store, ['read', '--id', 'missing']),
                 (store, ['consolidate', '--dry-run']), (store, ['doctor'])]
        self.assertGreaterEqual(len(cases), 12)
        for prefix, command in cases:
            with self.subTest(command=command):
                direct = self.box.run(prefix + command)
                admin = self.box.run(prefix + ['admin'] + command)
                self.assertEqual(direct.returncode, admin.returncode)
                self.assertEqual(_shape(direct.stdout), _shape(admin.stdout))
                self.assertEqual(_shape(direct.stderr), _shape(admin.stderr))

    def test_store_global_flags_work_before_after_and_inside_admin(self):
        flags = ['--root', str(self.box.root), '--standalone', '--task', 'router-task', '--session', 'writer']
        cases = [flags + ['capture', '--text', 'Decision: sqlite'],
                 ['capture', '--text', 'Decision: sqlite'] + flags,
                 ['admin'] + flags + ['capture', '--text', 'Decision: sqlite'],
                 ['admin', 'capture', '--text', 'Decision: sqlite'] + flags]
        for command in cases:
            with self.subTest(command=command):
                result = self.box.run(command)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertNotEqual('empty', _shape(result.stdout)['kind'])
        before = self.box.run(flags + ['recall', '--query', 'sqlite'])
        after = self.box.run(['recall', '--query', 'sqlite'] + flags)
        self.assertEqual((0, 0), (before.returncode, after.returncode))
        self.assertEqual(_shape(before.stdout), _shape(after.stdout))
        with memory_cli.WorkStore(self.box.root).db() as db:
            task = db.execute('SELECT owner FROM tasks WHERE id=?', ('router-task',)).fetchone()
            self.assertEqual('writer', task['owner'])

    def test_real_json_and_markdown_capsules_verify_and_tampering_fails(self):
        from prism_memory.memlet_wiki import build_handover, init_project
        init_project(str(self.box.repo))
        capsule = build_handover(str(self.box.repo), next_step='ship carefully', output_format='markdown')
        for suffix, content, tampered in (
            ('.json', json.dumps(capsule), json.dumps(dict(capsule, nextAction='edited'))),
            ('.md', capsule['formatted'], capsule['formatted'] + 'edited\n'),
        ):
            path = self.box.tmp / ('capsule' + suffix)
            path.write_text(content, encoding='utf-8')
            code, out, err = invoke(['handover', '--verify', str(path)])
            self.assertEqual((0, ''), (code, err))
            self.assertEqual('memlet: checksum verified\n', out)
            explicit = invoke(['handover', '--verify', str(path), '--format', 'json'])
            self.assertTrue(json.loads(explicit[1])['ok'])
            path.write_text(tampered, encoding='utf-8')
            code, out, err = invoke(['handover', '--verify', str(path)])
            self.assertEqual((1, ''), (code, err))
            self.assertEqual(1, len(out.splitlines()))
            self.assertIn('mismatch', out)

    def test_unreadable_and_malformed_capsules_fail_with_one_reason(self):
        path = self.box.tmp / 'absent.json'
        for content in (None, '{broken json', '[]'):
            if content is not None:
                path.write_text(content, encoding='utf-8')
            code, out, err = invoke(['handover', '--verify', str(path)])
            self.assertEqual((1, ''), (code, err))
            self.assertEqual(1, len(out.splitlines()))


class BridgeWiringTests(unittest.TestCase):
    def setUp(self):
        self.bridge = types.ModuleType('prism_memory.memlet_bridge')
        for name in ('unified_recall', 'changed_check', 'accept_anchor', 'learn_from_transcript',
                     'learn_pending', 'learn_approve', 'learn_reject'):
            setattr(self.bridge, name, mock.Mock(side_effect=lambda *a, **k: {'ok': True, 'formatted': 'bridge text\n'}))
        self.modules = mock.patch.dict(sys.modules, {'prism_memory.memlet_bridge': self.bridge})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        parent = mock.patch.object(prism_memory, 'memlet_bridge', self.bridge, create=True)
        parent.start()
        self.addCleanup(parent.stop)

    def test_positional_and_unified_recall_call_bridge_without_store_by_default(self):
        self.assertEqual(0, invoke(['recall', 'fix auth', '--repo', '/repo', '--budget', '99'])[0])
        self.bridge.unified_recall.assert_called_once_with('/repo', 'fix auth', budget=99)
        self.bridge.unified_recall.reset_mock()
        invoke(['recall', '--unified', '--query', 'fix auth'])
        self.bridge.unified_recall.assert_called_once_with('.', 'fix auth', budget=1200)

    def test_unified_recall_only_opens_store_when_requested(self):
        with mock.patch.object(memory_cli, 'WorkStore') as store, mock.patch.object(memory_cli, '_standalone_task_id', return_value='repo-task'):
            invoke(['--root', '/store', 'recall', 'auth'])
            store.assert_not_called()
            invoke(['recall', 'auth', '--standalone', '--root', '/store', '--session', 'reviewer'])
            store.assert_called_once_with('/store')
            store.return_value.ensure_standalone_task.assert_called_once_with('repo-task', session='reviewer')
            self.bridge.unified_recall.assert_called_with('.', 'auth', budget=1200, store=store.return_value, task='repo-task')

    def test_unified_recall_explicit_task_passes_store_without_bootstrapping(self):
        with mock.patch.object(memory_cli, 'WorkStore') as store:
            invoke(['--task', 'existing', 'recall', '--unified', '--query', 'auth', '--root', '/store'])
            self.bridge.unified_recall.assert_called_once_with('.', 'auth', budget=1200, store=store.return_value, task='existing')
            store.return_value.ensure_standalone_task.assert_not_called()

    def test_old_query_only_recall_stays_on_memory_store(self):
        with mock.patch.object(memory_cli, 'WorkStore') as store, mock.patch.object(memory_cli.memory, 'record_recall', return_value='recall-id'):
            store.return_value.memory.return_value = {'retrieval': [], 'ok': True}
            invoke(['--task', 'task', 'recall', '--query', 'auth'])
            store.return_value.memory.assert_called_once_with('task', 'auth', 6000, at=None, known_at=None,
                                                               before=None, after=None, shared=False, federated=False)
            self.bridge.unified_recall.assert_not_called()

    def test_check_changed_and_base(self):
        invoke(['check', '/repo', '--changed', '--base', 'main', '--dry-run'])
        self.bridge.changed_check.assert_called_once_with('/repo', base='main')

    def test_check_accept_repeatable_and_dry_run(self):
        invoke(['check', '--repo', '/repo', '--accept', 'a.py::a', '--accept', 'b.py::b', '--dry-run'])
        self.bridge.accept_anchor.assert_called_once_with('/repo', anchors=['a.py::a', 'b.py::b'], dry_run=True)

    def test_check_repin_and_dry_run(self):
        with mock.patch('prism_memory.memlet_wiki.repin_legacy_ts_anchors', return_value={'ok': True}) as repin:
            invoke(['check', '/repo', '--repin-legacy-ts', '--dry-run'])
            repin.assert_called_once_with('/repo', dry_run=True)

    def test_check_without_new_flags_preserves_audit(self):
        with mock.patch.object(memory_cli.memory, 'audit_repo', return_value={'ok': True}) as audit:
            invoke(['check', '/repo'])
            audit.assert_called_once_with('/repo', base_ref=None, head_ref='HEAD', output_format='json')
            self.bridge.changed_check.assert_not_called()

    def test_learn_transcript_and_pending_need_no_task(self):
        invoke(['learn', '--transcript', 'session.jsonl', '--repo', '/repo', '--vendor', 'fixture'])
        self.bridge.learn_from_transcript.assert_called_once_with('/repo', 'session.jsonl', vendor='fixture')
        invoke(['learn', '--pending'])
        self.bridge.learn_pending.assert_called_once_with('.')

    def test_learn_approval_and_rejection_arguments(self):
        invoke(['learn', '--approve', 'one', 'two', '--by', 'Alice', '--repo', '/repo'])
        self.bridge.learn_approve.assert_called_once_with('/repo', ids=['one', 'two'], approver='Alice')
        invoke(['learn', '--reject', 'one', 'two', '--by', 'Bob', '--reason', 'not grounded'])
        self.bridge.learn_reject.assert_called_once_with('.', ids=['one', 'two'], approver='Bob', reason='not grounded')

    def test_learn_modes_are_exclusive_and_decisions_require_reviewer(self):
        for flags in (['--pending', '--transcript', 's.log'], ['--file', 's.json', '--pending'],
                      ['--approve', 'a'], ['--reject', 'a'], ['--approve', 'a', '--by', '  ']):
            self.assertEqual(2, invoke(['learn'] + flags)[0])
        self.bridge.learn_approve.assert_not_called()
        self.bridge.learn_reject.assert_not_called()

    def test_learn_file_still_calls_existing_store_handler(self):
        payload = {'revision': 4, 'operationId': 'op1', 'entries': [{'kind': 'constraint'}]}
        with mock.patch.object(memory_cli, 'WorkStore') as store, mock.patch.object(memory_cli.memory, 'learn', return_value={'ok': True}) as learn:
            store.return_value.get.return_value = {'owner': 'owner'}
            with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))):
                self.assertEqual(0, invoke(['--task', 'task', '--session', 'owner', 'learn', '--file', '-'])[0])
            learn.assert_called_once_with(store.return_value, 'task', 'owner', 4, 'op1', payload['entries'])
            self.bridge.learn_from_transcript.assert_not_called()

    def test_bridge_failure_exit_code_and_markdown_are_preserved(self):
        self.bridge.changed_check.side_effect = lambda *a, **k: {'ok': False, 'error': 'stale', 'exitCode': 3, 'formatted': 'stale anchor\n'}
        self.assertEqual((3, 'stale anchor\n', ''), invoke(['check', '--changed', '--format', 'markdown']))
        self.bridge.learn_pending.side_effect = lambda *a, **k: {'ok': False, 'error': 'missing queue'}
        self.assertEqual(1, invoke(['learn', '--pending'])[0])

    def test_missing_bridge_is_clear_json_error_without_traceback(self):
        original_import = builtins.__import__

        def without_bridge(name, globals=None, locals=None, fromlist=(), level=0):
            if name == 'prism_memory' and 'memlet_bridge' in fromlist:
                raise ImportError('bridge is not installed')
            return original_import(name, globals, locals, fromlist, level)

        with mock.patch.object(builtins, '__import__', side_effect=without_bridge):
            code, out, err = invoke(['recall', 'auth'])
        self.assertEqual((1, ''), (code, err))
        result = json.loads(out)
        self.assertFalse(result['ok'])
        self.assertIn('memlet_bridge', result['error'])
        self.assertNotIn('Traceback', out + err)


class GlobalAndFormatTests(unittest.TestCase):
    def test_rule_recall_global_task_never_becomes_query(self):
        with mock.patch('prism_memory.memlet_recall.recall', return_value={'ok': True}) as recall:
            for flags in (['--task', 'global-task', 'rule-recall', 'actual query'],
                          ['rule-recall', 'actual query', '--task', 'global-task'],
                          ['admin', '--task', 'global-task', 'rule-recall', 'actual query']):
                self.assertEqual(0, invoke(flags)[0])
                recall.assert_called_with('.', 'actual query', k=3, safety=False)
            recall.reset_mock()
            self.assertEqual(2, invoke(['--task', 'global-task', 'rule-recall'])[0])
            self.assertEqual(2, invoke(['rule-recall', '--task', 'global-task'])[0])
            recall.assert_not_called()

    def test_same_named_local_option_is_isolated_from_global_namespace(self):
        parser = argparse.ArgumentParser()
        memory_cli._add_global_options(parser, defaults=True)
        sub = parser.add_subparsers(dest='action')
        child = sub.add_parser('local')
        child.add_argument('--task', default='local-default')
        memory_cli._add_global_options(child)
        args = parser.parse_args(['--task', 'global', 'local'])
        self.assertEqual(('global', 'local-default'), (args._global_task, args.task))
        args = parser.parse_args(['--task', 'global', 'local', '--task', 'local-value'])
        self.assertEqual(('global', 'local-value'), (args._global_task, args.task))

    def test_human_default_only_on_tty_and_explicit_json_always_wins(self):
        commands = [['init'], ['check'], ['recall', '--query', 'x'], ['handover'], ['learn', '--file', '-'], ['ui']]
        with mock.patch.object(memory_cli, '_main', side_effect=lambda args, parser: {'ok': True, 'formatted': 'human rendering\n'}):
            for command in commands:
                with self.subTest(command=command):
                    self.assertEqual('human rendering\n', invoke(command, tty=True)[1])
                    self.assertTrue(json.loads(invoke(command, tty=False)[1])['ok'])
                    self.assertTrue(json.loads(invoke(command + ['--format=json'], tty=True)[1])['ok'])
                    self.assertEqual('human rendering\n', invoke(command + ['--format', 'markdown'])[1])
            self.assertTrue(json.loads(invoke(['wiki'], tty=True)[1])['ok'])
            self.assertTrue(json.loads(invoke(['admin', 'wiki'], tty=True)[1])['ok'])

    def test_human_fallback_for_store_and_ui_without_formatted_field(self):
        with mock.patch.object(memory_cli, '_main', return_value={'ok': True, 'path': '/repo/report.html'}):
            code, out, err = invoke(['ui'], tty=True)
        self.assertEqual((0, ''), (code, err))
        self.assertIn('/repo/report.html', out)
        self.assertNotEqual('{', out[0])

    def test_init_receives_the_selected_tty_format(self):
        with mock.patch('prism_memory.memlet_wiki.init_project', side_effect=lambda *a, **k: {'ok': True, 'formatted': 'init text'}) as init:
            self.assertEqual('init text', invoke(['init'], tty=True)[1])
            init.assert_called_with('.', dry_run=False, force=False, output_format='markdown')
            invoke(['init'], tty=False)
            init.assert_called_with('.', dry_run=False, force=False, output_format='json')


class DreamWiringTests(unittest.TestCase):
    def test_allow_unverified_only_passed_when_requested(self):
        with mock.patch('prism_memory.memlet_dream.run_dream_recall', return_value={'ok': True}) as recall:
            invoke(['admin', 'dream-recall', '--graph', 'queue.json', '--allow-unverified', '--query', 'fix', 'auth'])
            recall.assert_called_once_with(graph_path='queue.json', query_text='fix auth', budget_tokens=1500,
                                           snapshot_path=None, out_md=None, allow_unverified=True)
            recall.reset_mock()
            invoke(['admin', 'dream-recall', '--graph', 'queue.json'])
            self.assertNotIn('allow_unverified', recall.call_args.kwargs)

    def test_pending_approve_reject_delegate_to_dream_queue_apis(self):
        with mock.patch('prism_memory.memlet_dream.list_pending', return_value={'ok': True}) as pending:
            invoke(['admin', 'dream-pending', '--graph', 'q.json'])
            pending.assert_called_once_with('q.json')
        with mock.patch('prism_memory.memlet_dream.approve_queue_items', return_value={'ok': True}) as approve:
            invoke(['admin', 'dream-approve', '--graph', 'q.json', 'n1', 'n2', '--by', 'Alice', '--snapshot', 'logs', '--db', 'q.db'])
            approve.assert_called_once_with('q.json', ['n1', 'n2'], approver='Alice', snapshot_path='logs', db_path='q.db')
        with mock.patch('prism_memory.memlet_dream.reject_queue_items', return_value={'ok': True}) as reject:
            invoke(['admin', 'dream-reject', '--graph', 'q.json', 'n1', 'n2', '--by', 'Bob', '--reason', 'duplicate', '--db', 'q.db'])
            reject.assert_called_once_with('q.json', ['n1', 'n2'], approver='Bob', reason='duplicate', db_path='q.db')


if __name__ == '__main__':
    unittest.main()
