"""R1: Git ref injection is refused at the shared audit boundary, without writes."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prism_memory.audit import audit_repository, _resolve_git_commit
from prism_memory_mcp import PrismMemoryMCPServer


class AuditRefSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'app.py').write_text('def main():\n    return 1\n')
        self.git('init', '-q')
        self.git('add', '.')
        self.commit('base')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.git('branch', 'base-branch')
        self.git('tag', 'base-tag')
        (self.repo / 'app.py').write_text('def main(required):\n    return required\n')
        self.git('add', '.')
        self.commit('head')
        (self.repo / 'app.py').write_text('def main(required, extra):\n    return extra\n')
        self.victim = self.root / 'outside-audit-output'
        self.victim.write_bytes(b'DO NOT OVERWRITE\n')
        self.option = '--output=' + str(self.victim)
        self.server = PrismMemoryMCPServer(self.root / 'store', default_task='audit/test')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo)] + list(args), text=True,
                                       stderr=subprocess.STDOUT)

    def commit(self, message):
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', message)

    def invalid(self, result):
        self.assertFalse(result['ok'], result)
        self.assertEqual(1, result['exitCode'])
        self.assertIn('invalid_git_ref', result['countsByRule'])
        self.assertEqual(b'DO NOT OVERWRITE\n', self.victim.read_bytes())

    def test_exact_output_option_through_library_base_and_head(self):
        self.invalid(audit_repository(self.repo, base_ref=self.option))
        self.invalid(audit_repository(self.repo, base_ref='HEAD~1', head_ref=self.option))
        self.invalid(audit_repository(self.repo, head_ref=self.option))

    def test_exact_output_option_through_cli_check_and_audit(self):
        cli = str(Path(__file__).resolve().parent / 'memory_cli.py')
        for verb, option in [('check', '--base'), ('audit', '--head')]:
            with self.subTest(verb=verb):
                # '=' delivers the exact option-like value to argparse rather than
                # letting argparse reject it before reaching the audit boundary.
                proc = subprocess.run([sys.executable, cli, verb, '.', option + '=' + self.option,
                                       '--format', 'json'], cwd=str(self.repo),
                                      capture_output=True, text=True, timeout=15)
                self.assertEqual(1, proc.returncode, proc.stderr)
                self.invalid(json.loads(proc.stdout))

    def test_exact_output_option_through_mcp_both_check_routes(self):
        for name, args in [('memlet_check', {'changed': False, 'base': self.option}),
                           ('prism_memory_audit', {'baseRef': self.option})]:
            key = 'repo_path' if name == 'memlet_check' else 'repoPath'
            args[key] = str(self.repo)
            response = self.server.handle_rpc({'id': 1, 'method': 'tools/call',
                                              'params': {'name': name, 'arguments': args}})
            self.invalid(response['result']['structuredContent'])
            if name == 'memlet_check':
                self.assertTrue(response['result']['isError'])
        # Changed-only check already rejected leading-dash refs; keep that gate.
        response = self.server.handle_rpc({'id': 1, 'method': 'tools/call', 'params': {
            'name': 'memlet_check', 'arguments': {'repo_path': str(self.repo), 'base': self.option}}})
        self.assertTrue(response['result']['isError'])
        self.assertEqual(b'DO NOT OVERWRITE\n', self.victim.read_bytes())

    def test_whitespace_control_empty_unknown_and_options_are_invalid_on_both_sides(self):
        for ref in ('', '--stat', '-R', 'HEAD\n', 'HEAD\t', 'HEAD x', 'HEAD\x00', 'HEAD\x7f',
                    'HEAD\x01', 'HEAD\u00a0', 'missing-ref'):
            for key in ('base_ref', 'head_ref'):
                with self.subTest(ref=repr(ref), side=key):
                    args = {'base_ref': 'HEAD~1'}
                    args[key] = ref
                    self.invalid(audit_repository(self.repo, **args))

    def test_branch_tag_relative_ref_and_sha_keep_diff_contract(self):
        for ref in ('base-branch', 'base-tag', 'HEAD~1', self.base):
            with self.subTest(ref=ref):
                result = audit_repository(self.repo, base_ref=ref, head_ref='HEAD')
                self.assertTrue(result['ok'], result)
                self.assertNotIn('error', result['gitDiff'])
                self.assertIn('app.py', result['gitDiff']['changedFiles'])
                self.assertEqual(['main'], list(result['gitDiff']['changedFiles']['app.py']['breakingSymbols']))
        for head in ('base-branch', 'base-tag', 'HEAD~1', self.base, 'WORKTREE'):
            with self.subTest(head=head):
                self.assertTrue(audit_repository(self.repo, base_ref=self.base, head_ref=head)['ok'])

    def test_old_git_fallback_never_puts_option_like_ref_in_option_position(self):
        real_run = subprocess.run
        calls = []

        def old_git(command, **kwargs):
            calls.append(command)
            if '--end-of-options' in command:
                return subprocess.CompletedProcess(command, 129, '', 'unknown option')
            return real_run(command, **kwargs)

        with patch('prism_memory.audit.subprocess.run', side_effect=old_git):
            self.assertEqual(self.base, _resolve_git_commit(self.repo, 'base-tag'))
            with self.assertRaises(ValueError):
                _resolve_git_commit(self.repo, self.option)
        self.assertEqual(2, len(calls))
        self.assertEqual(['git', 'rev-parse', '--verify', 'base-tag^{commit}'], calls[-1])
        self.assertEqual(b'DO NOT OVERWRITE\n', self.victim.read_bytes())


if __name__ == '__main__':
    unittest.main()
