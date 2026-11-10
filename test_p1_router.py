"""Six-verb router contract: real compatibility cases and isolated bridge wiring."""
import argparse
import builtins
import contextlib
import gc
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


def invoke(argv, *, tty=False, stderr_tty=False, program='memlet', suppress=False, force_notice=False):
    out, err = Output(tty), Output(stderr_tty)
    env = {key: value for key, value in os.environ.items()
           if key not in ('CODEX_THREAD_ID', 'PRISM_PARENT_SESSION_ID', 'ANTIGRAVITY_AGENT',
                          'MEMLET_NO_DEPRECATION', 'MEMLET_DEPRECATION')}
    if suppress:
        env['MEMLET_NO_DEPRECATION'] = '1'
    if force_notice:
        env['MEMLET_DEPRECATION'] = '1'
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, 'argv', [program] + argv):
        # Python 3.13 warns when earlier tests' unclosed sqlite connections are
        # collected. Drain that unrelated garbage before capturing CLI stderr;
        # do not suppress ResourceWarnings raised by the invocation itself.
        gc.collect()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = memory_cli.cli()
            except SystemExit as exc:
                code = exc.code
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def installed_bridge(bridge):
    """Patch only the stub's cache entries, preserving unrelated lazy imports."""
    missing = object()
    name = 'prism_memory.memlet_bridge'
    previous_module = sys.modules.get(name, missing)
    previous_attribute = vars(prism_memory).get('memlet_bridge', missing)
    sys.modules[name] = bridge
    prism_memory.memlet_bridge = bridge
    try:
        yield
    finally:
        if previous_attribute is missing:
            delattr(prism_memory, 'memlet_bridge')
        else:
            prism_memory.memlet_bridge = previous_attribute
        if previous_module is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous_module


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

    def test_init_advertises_hooks_only_as_opt_in_flags(self):
        # Unit C1 (audit P1 `memlet init --hooks`) made the flags real; they stay opt-in and validated.
        self.assertIn('--hooks', invoke(['init', '--help'])[1])
        self.assertEqual(2, invoke(['init', '--git-hooks'])[0])
        self.assertEqual(2, invoke(['init', '--hooks', '--hooks-remove'])[0])

    def test_dream_build_help_explains_pending_jsonl(self):
        out = invoke(['admin', 'dream-build', '--help'])[1]
        self.assertIn('PENDING', out)
        self.assertIn('.jsonl', out)


# Extracted from `git archive cefe43c`: capture main() at parse_args before
# any handler runs, then walk each legacy parser's _actions. Keep this independent
# of the current parser and available in shallow clones without old Git objects.
LEGACY_OPTION_DESTINATIONS = {
    'schema': {'-h': 'help', '--help': 'help'},
    'brief': {'-h': 'help', '--help': 'help'},
    'stats': {'-h': 'help', '--help': 'help'},
    'scenes': {'-h': 'help', '--help': 'help'},
    'doctor': {'-h': 'help', '--help': 'help'},
    'backup': {'-h': 'help', '--help': 'help', '--dest-dir': 'dest_dir', '--include-models': 'include_models'},
    'restore': {'-h': 'help', '--help': 'help', '--backup-dir': 'backup_dir'},
    'repo-benchmark': {'-h': 'help', '--help': 'help', '--work-dir': 'work_dir'},
    'audit': {'-h': 'help', '--help': 'help', '--repo-path': 'repo_path', '--repo': 'repo_path', '--base': 'base', '--head': 'head', '--format': 'format', '--exit-zero': 'exit_zero'},
    'check': {'-h': 'help', '--help': 'help', '--repo-path': 'repo_path', '--repo': 'repo_path', '--base': 'base', '--head': 'head', '--format': 'format', '--exit-zero': 'exit_zero'},
    'rescue': {'-h': 'help', '--help': 'help', '--repo-path': 'repo_path', '--repo': 'repo_path', '--write': 'write', '--output-dir': 'output_dir', '--format': 'format', '--exit-zero': 'exit_zero'},
    'viberescue': {'-h': 'help', '--help': 'help', '--repo-path': 'repo_path', '--repo': 'repo_path', '--write': 'write', '--output-dir': 'output_dir', '--format': 'format', '--exit-zero': 'exit_zero'},
    'anti-slop': {'-h': 'help', '--help': 'help', '--repo-path': 'repo_path', '--repo': 'repo_path', '--lock': 'lock', '--force': 'force', '--archetype': 'archetype', '--format': 'format', '--exit-zero': 'exit_zero'},
    'init': {'-h': 'help', '--help': 'help', '--repo-path': 'repo_path', '--repo': 'repo_path', '--dry-run': 'dry_run', '--force': 'force', '--format': 'format'},
    'wiki': {'-h': 'help', '--help': 'help', '--repo-path': 'repo_path', '--repo': 'repo_path', '--dry-run': 'dry_run', '--format': 'format'},
    'import': {'-h': 'help', '--help': 'help', '--repo-path': 'repo_path', '--repo': 'repo_path', '--from': 'source', '--dry-run': 'dry_run', '--format': 'format'},
    'slim-plan': {'-h': 'help', '--help': 'help', '--out': 'out', '--keep-recent': 'keep_recent', '--split-rules': 'split_rules'},
    'dream-build': {'-h': 'help', '--help': 'help', '--candidates': 'candidates', '--snapshot': 'snapshot', '--out-json': 'out_json', '--out-db': 'out_db', '--vendor': 'vendor'},
    'dream': {'-h': 'help', '--help': 'help', '--candidates': 'candidates', '--snapshot': 'snapshot', '--out-json': 'out_json', '--out-db': 'out_db', '--vendor': 'vendor'},
    'dream-recall': {'-h': 'help', '--help': 'help', '--graph': 'graph', '--query': 'query', '--budget': 'budget', '--max-tokens': 'budget', '--snapshot': 'snapshot', '--out-md': 'out_md', '--format': 'format'},
    'rule-context': {'-h': 'help', '--help': 'help', '--core-budget': 'core_budget', '--index-budget': 'index_budget', '--auto': 'auto', '--threshold': 'threshold'},
    'rule-recall': {'-h': 'help', '--help': 'help', '--repo': 'repo', '--k': 'k', '--safety': 'safety'},
    'index-build': {'-h': 'help', '--help': 'help', '--via': 'via'},
    'brief-build': {'-h': 'help', '--help': 'help', '--via': 'via'},
    'handover': {'-h': 'help', '--help': 'help', '--repo-path': 'repo_path', '--repo': 'repo_path', '--next-step': 'next_step', '--rejected': 'rejected', '--format': 'format', '--write': 'write', '--mode': 'mode', '--budget': 'budget'},
    'compare-bench': {'-h': 'help', '--help': 'help', '--format': 'format'},
    'benchmark-compare': {'-h': 'help', '--help': 'help', '--format': 'format'},
    'opsec-check': {'-h': 'help', '--help': 'help', '--file': 'target_file', '--repo-path': 'target_file', '--repo': 'target_file', '--canon': 'canon', '--history': 'history', '--require-canon': 'require_canon'},
    'opsec-fingerprint': {'-h': 'help', '--help': 'help'},
    'heal-skills': {'-h': 'help', '--help': 'help', '--dry-run': 'dry_run'},
    'verify-skill': {'-h': 'help', '--help': 'help', '--id': 'id', '--command': 'command', '--proof-file': 'proof_file', '--operation-id': 'operation_id', '--timeout': 'timeout'},
    'preflight': {'-h': 'help', '--help': 'help', '--plan': 'plan', '--files': 'files', '--action-type': 'action_type'},
    'capsule': {'-h': 'help', '--help': 'help', '--plan': 'plan', '--files': 'files', '--worker': 'worker', '--budget': 'budget'},
    'consolidate': {'-h': 'help', '--help': 'help', '--threshold': 'threshold', '--dry-run': 'dry_run'},
    'sync-md': {'-h': 'help', '--help': 'help', '--note-path': 'note_path', '--explicit-user': 'explicit_user'},
    'benchmark': {'-h': 'help', '--help': 'help', '--budget': 'budget', '--format': 'format'},
    'inspector': {'-h': 'help', '--help': 'help', '--budget': 'budget', '--repo-path': 'repo_path', '--repo': 'repo_path', '--output': 'output', '--out': 'output', '--output-dir': 'output_dir'},
    'ui': {'-h': 'help', '--help': 'help', '--repo-path': 'repo_path', '--repo': 'repo_path', '--output': 'output', '--out': 'output', '--output-dir': 'output_dir'},
    'pack': {'-h': 'help', '--help': 'help', '--stack': 'stack', '--output': 'output', '--output-dir': 'output_dir', '--dry-run': 'dry_run', '--list': 'list', '--format': 'format'},
    'export': {'-h': 'help', '--help': 'help', '--targets': 'targets', '--target-dir': 'target_dir', '--write': 'write', '--lang': 'lang'},
    'capture': {'-h': 'help', '--help': 'help', '--file': 'file', '--text': 'text', '--kind': 'kind', '--command': 'command', '--operation-id': 'operation_id'},
    'recall': {'-h': 'help', '--help': 'help', '--query': 'query', '--at': 'at', '--known-at': 'known_at', '--before': 'before', '--after': 'after', '--shared': 'shared', '--federated': 'federated', '--budget': 'budget'},
    'read': {'-h': 'help', '--help': 'help', '--id': 'id', '--offset': 'offset', '--size': 'size'},
    'skills': {'-h': 'help', '--help': 'help', '--query': 'query', '--include-inactive': 'include_inactive', '--at': 'at', '--known-at': 'known_at', '--before': 'before', '--after': 'after', '--shared': 'shared', '--federated': 'federated', '--limit': 'limit', '--budget': 'budget'},
    'skill': {'-h': 'help', '--help': 'help', '--id': 'id'},
    'cases': {'-h': 'help', '--help': 'help', '--limit': 'limit'},
    'links': {'-h': 'help', '--help': 'help', '--id': 'id', '--limit': 'limit'},
    'learn': {'-h': 'help', '--help': 'help', '--file': 'file'},
    'feedback': {'-h': 'help', '--help': 'help', '--file': 'file'},
    'skill-manage': {'-h': 'help', '--help': 'help', '--file': 'file'},
    'skill-sync': {'-h': 'help', '--help': 'help'},
    'index': {'-h': 'help', '--help': 'help', '--limit': 'limit'},
    'backfill': {'-h': 'help', '--help': 'help', '--source-session': 'source_session', '--end-byte': 'end_byte'},
}


class LegacyPrefixTests(unittest.TestCase):
    @staticmethod
    def option_values(action):
        if action.nargs == 0:
            return []
        if action.choices:
            return [str(next(iter(action.choices)))]
        return ['1' if action.type in (int, float) else 'prefix-probe']

    def test_every_unique_base_prefix_resolves_to_its_legacy_destination(self):
        parser = memory_cli._build_parser()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        self.assertEqual(set(LEGACY_COMMANDS), set(LEGACY_OPTION_DESTINATIONS))
        count = 0
        for name, options in LEGACY_OPTION_DESTINATIONS.items():
            prefixes = set(options)
            for option in options:
                if option.startswith('--'):
                    prefixes.update(option[:length] for length in range(3, len(option)))
            for prefix in sorted(prefixes):
                matches = [option for option in options if option.startswith(prefix)]
                if prefix not in options and len(matches) != 1:
                    continue
                expected = options[prefix if prefix in options else matches[0]]
                with self.subTest(command=name, prefix=prefix, destination=expected):
                    resolved = sub.choices[name]._parse_optional(prefix)
                    # argparse 3.13 returns a list; 3.9 returns the single tuple.
                    action = (resolved[0] if isinstance(resolved, list) else resolved)[0]
                    self.assertEqual(expected, action.dest)
                    required = [a for a in sub.choices[name]._actions if a.required and a is not action]
                    for group in sub.choices[name]._mutually_exclusive_groups:
                        if group.required and action not in group._group_actions:
                            required.append(group._group_actions[0])
                    setup = []
                    for needed in required:
                        setup += [needed.option_strings[0]] + self.option_values(needed)
                    values = self.option_values(action)
                    for route in ([name], ['admin', name]):
                        argv = route + setup + [prefix] + values
                        if expected == 'help':
                            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as done:
                                parser.parse_args(argv)
                            self.assertEqual(0, done.exception.code)
                        else:
                            parsed = parser.parse_args(argv)
                            value = values[0] if values else True
                            if action.type is not None and values:
                                value = action.type(value)
                            if action.nargs == '*' or isinstance(action, argparse._AppendAction):
                                value = [value]
                            self.assertEqual(value, getattr(parsed, expected))
                count += 1
        self.assertEqual(1197, count)


class DeprecationTests(unittest.TestCase):
    def test_notice_exactly_once_on_stderr_and_suppressible(self):
        want = "memlet: 'schema' is now 'memlet admin schema'; the short form will be removed in a future release.\n"
        normal = invoke(['schema'], force_notice=True)
        quiet = invoke(['schema'], suppress=True)
        admin = invoke(['admin', 'schema'], force_notice=True)
        self.assertEqual(want, normal[2])
        self.assertEqual('', quiet[2])
        self.assertEqual('', admin[2])
        self.assertEqual(normal[:2], quiet[:2])
        self.assertEqual(quiet, admin)
        self.assertEqual(quiet, invoke(['schema']))
        self.assertEqual(quiet, invoke(['schema'], tty=True))  # stdout TTY is irrelevant
        self.assertEqual(want, invoke(['schema'], stderr_tty=True)[2])
        self.assertEqual(quiet, invoke(['schema'], stderr_tty=True, suppress=True, force_notice=True))
        json.loads(normal[1])

    def test_legacy_help_warns_and_option_values_do_not_trigger_warnings(self):
        code, _, err = invoke(['--task', 'wiki', 'schema', '--help'], force_notice=True)
        self.assertEqual(0, code)
        self.assertIn("'schema' is now", err)
        self.assertNotIn("'wiki' is now", err)
        self.assertEqual('', invoke(['--task', 'schema', 'recall', '--help'], force_notice=True)[2])
        self.assertEqual('', invoke(['--help', 'schema'], force_notice=True)[2])
        self.assertIn("'schema' is now", invoke(['--tas=foo', 'schema', '--help'], force_notice=True)[2])

    def test_product_commands_and_admin_do_not_warn(self):
        for name in list(memory_cli.PRODUCT_COMMANDS) + ['admin']:
            self.assertEqual('', invoke([name, '--help'], force_notice=True)[2])

    def test_legacy_executable_names_delegate_and_warn_once(self):
        expected = invoke(['schema'])
        for name in ('ieum-memory', 'prism-memory', 'deltaharness'):
            with self.subTest(program=name):
                code, out, err = invoke(['schema'], program='/bin/' + name, force_notice=True)
                self.assertEqual(expected[:2], (code, out))
                self.assertEqual(name + " is now 'memlet'; this command name will be removed in a future release.\n", err)
                self.assertEqual(expected, invoke(['schema'], program=name))
                self.assertEqual(err, invoke(['schema'], program=name, stderr_tty=True)[2])
                self.assertEqual(expected, invoke(['schema'], program=name, suppress=True, force_notice=True, stderr_tty=True))
                with mock.patch.object(memory_cli, 'main', side_effect=ValueError('broken')):
                    code, out, error = invoke(['capture'], program=name)
                self.assertEqual((1, ''), (code, out))
                self.assertEqual({'ok': False, 'error': 'broken', 'type': 'ValueError'}, json.loads(error))
        self.assertEqual('', invoke(['--version'], force_notice=True)[2])

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

    def test_real_bridge_cli_learning_approval_drift_and_checksum_lifecycle(self):
        def run_json(argv, expected=0):
            result = self.box.run(argv)
            self.assertEqual(expected, result.returncode, result.stderr or result.stdout)
            self.assertEqual('', result.stderr)
            return json.loads(result.stdout)

        run_json(['init'])
        transcript = self.box.tmp / 'claude-session.jsonl'
        lesson = 'Never call charge() in mod.py without a retry wrapper.'
        transcript.write_text(json.dumps({'type': 'user', 'timestamp': '2026-11-09T09:00:00Z',
                                         'message': {'role': 'user', 'content': lesson}}) + '\n', encoding='utf-8')
        learned = run_json(['learn', '--transcript', str(transcript)])
        pending = run_json(['learn', '--pending'])
        ids = [node['id'] for node in pending['pendingNodes']]
        self.assertTrue(ids)
        self.assertEqual(set(learned['addedNodeIds']), set(ids))
        self.assertEqual([], run_json(['recall', 'charge'])['sections']['dream'])
        queue = self.box.repo / '.memlet/dream/APPROVAL-QUEUE.json'
        before = queue.read_bytes()
        for reviewer in ([], ['--by', '  ']):
            refused = self.box.run(['learn', '--approve'] + ids + reviewer)
            self.assertEqual(2, refused.returncode)
            self.assertIn('--by NAME', refused.stderr)
            self.assertEqual(before, queue.read_bytes())
        run_json(['learn', '--approve'] + ids + ['--by', 'Reviewer'])
        recalled = run_json(['recall', 'charge'])
        self.assertEqual(set(ids), {item['id'] for item in recalled['sections']['dream']})
        self.assertIn(lesson, json.dumps(run_json(['handover'])))
        source = self.box.repo / 'mod.py'
        edited = source.read_text(encoding='utf-8').replace('def charge(amount):', 'def charge(amount, currency):')
        source.write_text(edited, encoding='utf-8')
        changed = run_json(['check', '--changed'], expected=1)
        self.assertGreater(changed['staleCount'], 0)
        self.assertTrue(any(item['anchor'] == 'mod.py::charge' for item in changed['affected']))
        self.assertTrue(run_json(['check', '--accept', 'mod.py::charge'])['ok'])
        written = self.box.run(['handover', '--format', 'markdown', '--write'])
        self.assertEqual(0, written.returncode, written.stderr or written.stdout)
        capsule = self.box.repo / '.memlet/wiki/HANDOVER.md'
        self.assertTrue(capsule.is_file())
        self.assertEqual(0, self.box.run(['handover', '--verify', str(capsule)]).returncode)
        capsule.write_text(capsule.read_text(encoding='utf-8') + '\nEdited capsule.\n', encoding='utf-8')
        self.assertEqual(1, self.box.run(['handover', '--verify', str(capsule)]).returncode)


class BridgeWiringTests(unittest.TestCase):
    def setUp(self):
        self.bridge = types.ModuleType('prism_memory.memlet_bridge')
        for name in ('unified_recall', 'changed_check', 'accept_anchor', 'learn_from_transcript',
                     'learn_pending', 'learn_approve', 'learn_reject'):
            setattr(self.bridge, name, mock.Mock(side_effect=lambda *a, **k: {'ok': True, 'formatted': 'bridge text\n'}))
        installed = installed_bridge(self.bridge)
        installed.__enter__()
        self.addCleanup(installed.__exit__, None, None, None)

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
        invoke(['check', '/repo', '--changed', '--base', 'main'])
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
        # Nested injection must restore both import locations even on failure.
        alternate = types.ModuleType('prism_memory.memlet_bridge')
        with self.assertRaisesRegex(RuntimeError, 'cleanup'):
            with installed_bridge(alternate):
                self.assertIs(alternate, sys.modules['prism_memory.memlet_bridge'])
                self.assertIs(alternate, prism_memory.memlet_bridge)
                raise RuntimeError('cleanup')
        self.assertIs(self.bridge, sys.modules['prism_memory.memlet_bridge'])
        self.assertIs(self.bridge, prism_memory.memlet_bridge)
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


class ModeConflictTests(unittest.TestCase):
    def assert_refused(self, argv, flag):
        with mock.patch.object(memory_cli, '_main') as dispatch:
            code, out, err = invoke(argv)
        self.assertEqual((2, ''), (code, out), err)
        self.assertIn(flag, err)
        dispatch.assert_not_called()

    def test_unified_recall_rejects_store_filters_and_duplicate_query(self):
        for query in (['recall', 'charge'], ['recall', '--unified', '--query', 'charge']):
            for flags in (['--at', '2000-01-01'], ['--known-at', '2000-01-01'], ['--before', '2000-01-01'],
                          ['--after', '2000-01-01'], ['--shared'], ['--federated'], ['--s']):
                with self.subTest(argv=query + flags):
                    self.assert_refused(query + flags, '--shared' if flags == ['--s'] else flags[0])
        for value in ('wrong', ''):
            self.assert_refused(['recall', 'charge', '--query', value], '--query')
        self.assert_refused(['admin', 'recall', 'charge', '--q', 'wrong'], '--query')

    def test_learn_rejects_ignored_mode_options(self):
        cases = [(['--transcript', 's.jsonl'], ['--by', 'Reviewer']),
                 (['--transcript', 's.jsonl'], ['--reason', 'unused']),
                 (['--pending'], ['--vendor', 'unused']), (['--pending'], ['--by', '']),
                 (['--pending'], ['--reason', '']),
                 (['--approve', 'id', '--by', 'Reviewer'], ['--vendor', 'unused']),
                 (['--approve', 'id', '--by', 'Reviewer'], ['--reason', 'unused']),
                 (['--reject', 'id', '--by', 'Reviewer'], ['--vendor', 'unused']),
                 (['--file', '-'], ['--repo', '.']), (['--file', '-'], ['--by', 'Reviewer'])]
        for mode, extra in cases:
            with self.subTest(mode=mode, extra=extra):
                self.assert_refused(['learn'] + mode + extra, extra[0])
        for flags in (['--root', '/unused'], ['--task', 'unused'], ['--session', 'unused'], ['--standalone']):
            self.assert_refused(flags + ['learn', '--pending'], flags[0])
            self.assert_refused(['learn', '--pending'] + flags, flags[0])

    def test_new_check_modes_reject_ignored_options(self):
        for mode in (['--changed'], ['--accept', 'mod.py::charge'], ['--repin-legacy-ts']):
            for flags in (['--head', 'HEAD'], ['--exit-zero'], ['--format', 'sarif'], ['--task', 'unused']):
                with self.subTest(mode=mode, flags=flags):
                    self.assert_refused(['check'] + mode + flags, flags[0])
            if mode != ['--changed']:
                self.assert_refused(['check'] + mode + ['--base', 'HEAD'], '--base')
        self.assert_refused(['check', '--changed', '--dry-run'], '--dry-run')
        self.assert_refused(['check', '--dry-run'], '--dry-run')


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
