"""9.0 product surface and lossless opening of files emitted by 8.5.0."""
import ast
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from memlet import VERSION
from memlet import compatibility as compat
from memlet import memlet_bridge as bridge, memlet_hooks as hooks, memlet_wiki as wiki
from memlet.paths import resolve_default_data_root, resolve_embedding_model_dir, backup_data_root, restore_data_root
from memlet.standalone import StandaloneStore, MemoryEngine, StandaloneService
from memlet_mcp import MemletMCPServer
import memory_intelligence as memory

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / 'testdata/v8_5_fixture.json'

# The pattern list is the scanner's only self-exemption. Literal removed names
# also drive negative CLI/MCP/environment tests; no production alias is implied.
PATTERN_STRINGS = {
    'banned': r'viberescue|anti-slop|anti_slop|antislop|harness pack|opsec|prism|ieum|deltaharness|nocturne|patchlet|stylet|reachlet',
    'commands': ('rescue', 'viberescue', 'anti-slop', 'pack', 'opsec-check', 'opsec-fingerprint'),
    'environment': ('PRISM_MEMORY_HOME', 'PRISM_MEMORY_MODEL_DIR', 'DELTAHARNESS_TRANSCRIPT_DIRS', 'PRISM_TRANSCRIPT_DIRS', 'PRISM_PARENT_SESSION_ID', 'PRISM_PROTECTED_PATHS', 'DELTAHARNESS_PROTECTED_PATHS', 'DELTAHARNESS_NODE', 'DELTAHARNESS_TYPESCRIPT'),
    'old_tool': 'prism_memory_brief',
    'repository': 'ian-nocturne/deltaharness',
    'author': '{name = "Ian (Nocturne Lab)"}',
}


class StandaloneSurfaceTests(unittest.TestCase):
    def test_no_retired_product_names_in_tracked_sources(self):
        tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=str(ROOT)).decode().split('\0')
        own = ast.parse(Path(__file__).read_text())
        pattern_node = next(n for n in own.body if isinstance(n, ast.Assign)
                            and any(isinstance(t, ast.Name) and t.id == 'PATTERN_STRINGS' for t in n.targets))
        own_pattern_lines = range(pattern_node.lineno, pattern_node.end_lineno + 1)
        # Legal attribution and a frozen historical external-audit artifact are
        # expressly retained by the release specification.
        whole_file_allowlist = {'LICENSE', 'real-oss-audit-results.json',
                                # Actual 8.5 writer output, including old IDs, markers
                                # and database filenames, proves read compatibility.
                                'testdata/v8_5_fixture.json'}
        # Only constant declarations with a documented on-disk/read purpose may
        # carry old spellings; importing code must refer to these constants.
        compatibility_constants = {'LEGACY_STORE_DIRECTORY', 'LEGACY_DATABASE_FILENAME', 'LEGACY_HARNESS_MARKERS',
                                   'LEGACY_HOOK_PROGRAMS', 'LEGACY_CONTEXT_TAG', 'LEGACY_HANDOVER_SCHEMA',
                                   'LEGACY_HANDOVER_TITLE'}
        bad = []
        for name in tracked:
            if not name or name in whole_file_allowlist:
                continue
            p = ROOT / name
            if not p.is_file():  # a staged deletion has no working file
                continue
            for line_no, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1):
                if name == Path(__file__).name and line_no in own_pattern_lines:
                    continue
                if name == 'memlet/compatibility.py' and line.split(' = ', 1)[0] in compatibility_constants:
                    continue
                if name == 'pyproject.toml' and line.strip() == PATTERN_STRINGS['author']:
                    continue  # Explicit owner-specified author attribution, not product branding.
                # The repository's URL/Actions owner-repo identifier is outside
                # this change. Remove only that exact token before scanning.
                line = line.replace(PATTERN_STRINGS['repository'], '')
                if re.search(PATTERN_STRINGS['banned'], line, re.I):
                    bad.append('%s:%d: %s' % (name, line_no, line))
            if re.search(PATTERN_STRINGS['banned'], name, re.I):
                bad.append('retired filename: ' + name)
        self.assertEqual([], bad)

    def test_exact_console_scripts_and_shared_version(self):
        text = (ROOT / 'pyproject.toml').read_text()
        section = text.split('[project.scripts]\n', 1)[1].split('\n[', 1)[0]
        entries = dict(re.findall(r'^([\w-]+)\s*=\s*"([^"]+)"', section, re.M))
        self.assertEqual({'memlet': 'memory_cli:cli', 'memlet-mcp': 'memlet_mcp:main'}, entries)
        self.assertEqual('9.0.0', VERSION)
        self.assertIn('version = "' + VERSION + '"', text)
        self.assertEqual(VERSION, wiki.MEMLET_VERSION)

    def test_mcp_exact_tools_server_resources_and_no_old_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = MemletMCPServer(root=tmp)
            result = server.handle_rpc({'id': 1, 'method': 'tools/list'})['result']
            names = [t['name'] for t in result['tools']]
            self.assertEqual(25, len(names))
            self.assertEqual(25, len(set(names)))
            self.assertTrue(all(n.startswith('memlet_') for n in names))
            self.assertEqual(19, sum(n.startswith('memlet_store_') for n in names))
            init = server.handle_rpc({'id': 2, 'method': 'initialize'})['result']
            self.assertEqual({'name': 'memlet', 'version': VERSION}, init['serverInfo'])
            self.assertTrue(all(r['uri'].startswith('memlet://') for r in server.list_resources()))
            old = server.handle_rpc({'id': 3, 'method': 'tools/call',
                                     'params': {'name': PATTERN_STRINGS['old_tool'], 'arguments': {}}})
            self.assertTrue(old['result']['isError'])

    def test_removed_commands_are_argparse_unknown_commands(self):
        for prefix in ([], ['admin']):
            for command in PATTERN_STRINGS['commands']:
                with self.subTest(command=prefix + [command]):
                    proc = subprocess.run([sys.executable, str(ROOT / 'memory_cli.py')] + prefix + [command],
                                          capture_output=True, text=True)
                    self.assertEqual(2, proc.returncode, proc.stderr)
                    self.assertIn('invalid choice:', proc.stderr)
                    self.assertIn(repr(command), proc.stderr)
                    self.assertEqual('', proc.stdout)

    def test_every_non_audit_action_mode_is_configuration_error(self):
        from memlet.action_verdict import run_action
        for mode in PATTERN_STRINGS['commands'] + ('all', 'other'):
            err, out = io.StringIO(), io.StringIO()
            code = run_action({'INPUT_MODE': mode}, stderr=err, stdout=out)
            self.assertEqual(2, code)
            self.assertIn('removed in 9.0', err.getvalue())
            self.assertEqual('', out.getvalue())

    def test_action_malformed_results_fail_closed_before_rendering(self):
        from memlet.action_verdict import run_action
        for value in (None, [], {"ok": True}, {"ok": False, "errorCount": 0}):
            for fmt in ('json', 'markdown', 'sarif'):
                out, err = io.StringIO(), io.StringIO()
                with patch('memlet.audit.audit_repository', return_value=value):
                    result = run_action({'INPUT_FORMAT': fmt, 'INPUT_FAIL_ON_DRIFT': 'false',
                                         'INPUT_SARIF_FILE': ''}, stdout=out, stderr=err)
                self.assertEqual(2, result)
                self.assertIn('failing closed', err.getvalue())
                if fmt == 'json':
                    self.assertEqual('UNVERIFIED', json.loads(out.getvalue())['verdict']['status'])

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the Inspector script syntax check')
    def test_inspector_remaining_scripts_are_valid_javascript(self):
        from memlet import memlet_inspector, memlet_cortex
        script = memlet_inspector._JS + '\n' + memlet_cortex.JS
        proc = subprocess.run(['node', '--check'], input=script, text=True, capture_output=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        payload = memlet_inspector.build_inspector_payload(str(ROOT))
        self.assertEqual({'ok', 'schema', 'memletVersion', 'repoName', 'repoPath', 'defaultWorkspace',
                          'showcaseReason', 'workspaces', 'benchmark', 'simulator', 'license',
                          'externalNetworkCalls', 'errors'}, set(payload))


class StoreSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='v9-home-')).resolve()
        self.addCleanup(lambda: shutil.rmtree(str(self.tmp), ignore_errors=True))
        self.env = patch.dict(os.environ, {'HOME': str(self.tmp), 'MEMLET_HOME': '', 'MEMLET_MODEL_DIR': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_default_store_is_distinct_from_repository_memory_and_old_environment(self):
        repo = self.tmp / 'repo'
        (repo / '.memlet').mkdir(parents=True)
        (repo / compat.LEGACY_STORE_DIRECTORY).mkdir()
        old_vars = {name: str(self.tmp / 'wrong') for name in PATTERN_STRINGS['environment']}
        with patch.dict(os.environ, old_vars):
            self.assertEqual(self.tmp / '.memlet-store', resolve_default_data_root(cwd=repo, module_dir=repo))
            self.assertEqual(self.tmp / '.memlet-store/models/e5-small', resolve_embedding_model_dir(module_dir=repo))
        old_home = self.tmp / compat.LEGACY_STORE_DIRECTORY
        (old_home / 'state').mkdir(parents=True)
        (old_home / 'state/work.sqlite3').write_bytes(b'never open automatically')
        # Even a source checkout inside the old user store cannot select it by
        # module-directory discovery; the user must explicitly choose it.
        self.assertEqual(self.tmp / '.memlet-store', resolve_default_data_root(module_dir=old_home))
        (old_home / 'memory_models/e5-small').mkdir(parents=True)
        (old_home / 'memory_models/e5-small/manifest.json').write_text('{}')
        self.assertEqual(self.tmp / '.memlet-store/models/e5-small', resolve_embedding_model_dir(module_dir=old_home))
        selected = self.tmp / 'chosen'
        with patch.dict(os.environ, {'MEMLET_HOME': str(selected)}):
            self.assertEqual(selected, resolve_default_data_root(cwd=repo))
            self.assertEqual(repo, resolve_default_data_root(explicit_root=repo))

    def test_empty_store_notice_never_opens_old_contents_and_is_once_per_surface(self):
        old = self.tmp / compat.LEGACY_STORE_DIRECTORY
        old.mkdir()
        sentinel = old / 'user-data'
        sentinel.write_bytes(b'KEEP ORIGINAL DATA')
        proc = subprocess.run([sys.executable, str(ROOT / 'memory_cli.py'), '--standalone', 'brief'],
                              capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(1, proc.stderr.count('An existing store is at'))
        self.assertIn('--root', proc.stderr)
        self.assertIn('MEMLET_HOME', proc.stderr)
        self.assertIn(str(old), proc.stderr)
        server = MemletMCPServer()
        first = server.handle_rpc({'id': 1, 'method': 'initialize'})['result']
        self.assertIn(str(old), first['notice'])
        self.assertNotIn('notice', server.handle_rpc({'id': 2, 'method': 'tools/list'})['result'])
        self.assertEqual(b'KEEP ORIGINAL DATA', sentinel.read_bytes())
        self.assertEqual(['user-data'], [p.name for p in old.iterdir()])
        # A direct MCP tool caller also receives exactly one notice.
        direct = MemletMCPServer()
        self.assertIn('notice', direct.call_tool('memlet_store_brief', {}))
        self.assertNotIn('notice', direct.call_tool('memlet_store_brief', {}))
        MemoryEngine(direct.store).capture('Decision: retain the checked SQLite memory.', force=True)
        self.assertIsNone(compat.existing_store_notice(self.tmp / '.memlet-store'))
        self.assertIsNone(compat.existing_store_notice(self.tmp / 'explicit'))


class PreviousWriterFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='v9-fixture-')).resolve()
        self.addCleanup(lambda: shutil.rmtree(str(self.tmp), ignore_errors=True))
        home = self.tmp / 'home'
        home.mkdir()
        isolation = patch.dict(os.environ, {'HOME': str(home), 'TMPDIR': str(self.tmp),
                                           'GIT_CEILING_DIRECTORIES': str(self.tmp),
                                           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull})
        isolation.start()
        self.addCleanup(isolation.stop)
        self.fixture = json.loads(FIXTURE.read_text())
        self.assertEqual('8.5.0', self.fixture['writtenBy'])
        self.repo = self.tmp / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        for relative, data in self.fixture['files'].items():
            p = self.tmp / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            if 'sql' in data:
                with sqlite3.connect(str(p)) as db:
                    db.executescript(data['sql'])
            else:
                p.write_text(data['text'], encoding='utf-8')
            if relative.startswith('repo/.git/hooks/'):
                p.chmod(0o755)

    def test_config_wiki_ledger_and_signed_capsule_open_and_round_trip(self):
        self.assertEqual(compat.LEGACY_HANDOVER_SCHEMA, self.fixture['capsule']['schema'])
        self.assertTrue(wiki.verify_handover_checksum(self.fixture['capsule']))
        old_markdown = (self.repo / wiki.HANDOVER_PAGE).read_text()
        self.assertTrue(wiki.verify_handover_markdown(old_markdown))
        self.assertTrue(wiki.init_project(self.repo)['ok'])
        self.assertTrue(wiki.wiki_status(self.repo)['ok'])
        with sqlite3.connect(str(self.repo / wiki.LEDGER_PATH)) as db:
            self.assertEqual('ok', db.execute('PRAGMA integrity_check').fetchone()[0])
        new = wiki.build_handover(self.repo, write=True, output_format="markdown")
        self.assertTrue(new['ok'])
        self.assertTrue(wiki.verify_handover_checksum(new))
        self.assertEqual('memlet-handover/v1', new['schema'])
        self.assertTrue(new['formatted'].startswith('## Memlet Handover Capsule\n'))
        self.assertTrue(wiki.verify_handover_markdown((self.repo / wiki.HANDOVER_PAGE).read_text()))

    def test_dream_queue_database_snapshot_approval_and_recall_round_trip(self):
        before = bridge.learn_pending(self.repo)
        self.assertTrue(before['ok'], before)
        self.assertGreater(before['counts']['pendingNodes'], 0)
        queue = self.repo / bridge.QUEUE_REL
        graph = json.loads(queue.read_text())
        ids = [n['id'] for n in graph['nodes']]
        approved = bridge.learn_approve(self.repo, ids, approver='fixture-reviewer')
        self.assertTrue(approved['ok'], approved)
        self.assertEqual(0, bridge.learn_pending(self.repo)['counts']['pendingNodes'])
        with sqlite3.connect(str(self.repo / bridge.DB_REL)) as db:
            self.assertEqual('ok', db.execute('PRAGMA integrity_check').fetchone()[0])
        reopened = json.loads(queue.read_text())
        self.assertEqual(ids, [n['id'] for n in reopened['nodes']])
        self.assertTrue((self.repo / bridge.SNAPSHOT_REL).is_dir())

    def test_store_and_backup_keep_items_and_write_current_schema(self):
        store = StandaloneStore(self.tmp / 'store')
        self.assertEqual(compat.LEGACY_DATABASE_FILENAME, store.db_path.name)
        with store.db() as db:
            rows = [tuple(row) for row in db.execute('SELECT item_id, content FROM memory_items ORDER BY item_id')]
        self.assertTrue(rows)
        self.assertTrue(any('preserve the code-anchored SQLite memory' in content for _id, content in rows))
        engine = MemoryEngine(store, task='fixture/task', session='fixture-owner')
        self.assertTrue(engine.brief())
        engine.capture('Decision: use the upgraded memory with unchanged provenance.', force=True)
        with store.db() as db:
            now = [tuple(row) for row in db.execute('SELECT item_id, content FROM memory_items ORDER BY item_id')]
        self.assertTrue(set(rows) <= set(now))
        restored = self.tmp / 'restored'
        self.assertTrue(restore_data_root(self.tmp / 'backup', restored)['ok'])
        reopened = StandaloneStore(restored)
        with reopened.db() as db:
            self.assertEqual(rows, [tuple(row) for row in db.execute('SELECT item_id, content FROM memory_items ORDER BY item_id')])
        backup_data_root(store.root, self.tmp / 'new-backup', db_path=store.db_path)
        self.assertEqual('memlet.backup.v1', json.loads((self.tmp / 'new-backup/backup_manifest.json').read_text())['schema'])

    def test_stored_context_wrappers_remain_strippable_and_are_not_reimported(self):
        service = StandaloneService(self.tmp / 'store')
        self.assertIn(compat.LEGACY_CONTEXT_TAG, self.fixture['context'])
        self.assertEqual(self.fixture['original'], service.strip_memory_for_display(self.fixture['context']))
        result = memory.import_messages(service.store, 'fixture/task', 'fixture-owner',
                                        [{'role': 'assistant', 'text': self.fixture['context']}])
        self.assertEqual(0, result['imported'])

    def test_managed_rule_markers_are_replaced_and_removed_in_both_spellings(self):
        old = (self.tmp / 'rules/CLAUDE.md').read_text()
        self.assertIn(compat.LEGACY_HARNESS_MARKERS[0], old)
        for text in (old, old.replace(compat.LEGACY_HARNESS_MARKERS[0], memory._HARNESS_BEGIN)
                     .replace(compat.LEGACY_HARNESS_MARKERS[1], memory._HARNESS_END)):
            user = 'USER BEFORE\n\n' + text + '\nUSER AFTER\n'
            replacement = memory._HARNESS_BEGIN + '\ncurrent rules\n' + memory._HARNESS_END
            updated = memory._upsert_harness_block(user, replacement)
            self.assertEqual(1, updated.count(memory._HARNESS_BEGIN))
            self.assertNotIn(compat.LEGACY_HARNESS_MARKERS[0], updated)
            self.assertIn('USER BEFORE', updated)
            self.assertIn('USER AFTER', updated)
            removed = memory._upsert_harness_block(user, '')
            self.assertNotIn('HARNESS_BEGIN', removed)
            self.assertNotIn('HARNESS_END', removed)
            self.assertEqual('USER BEFORE\n\nUSER AFTER\n', removed)

    def test_old_settings_and_git_hooks_update_and_remove_with_ownership(self):
        old_settings = json.loads((self.repo / '.claude/settings.local.json').read_text())
        # 8.5 normally wrote the current executable or the absolute script path;
        # even earlier managed executable names remain recognizable for removal.
        for name in ('memlet',) + compat.LEGACY_HOOK_PROGRAMS:
            self.assertEqual('stop', hooks.memlet_hook_event({'type': 'command', 'command': name + ' admin hook stop || true'}))
        prefix = '%s %s' % (sys.executable, ROOT / 'memory_cli.py')
        self.assertEqual('stop', hooks.memlet_hook_event({'type': 'command', 'command': prefix + ' admin hook stop || true'}))
        for event in hooks.RUNTIME_EVENTS:
            proc = subprocess.run([sys.executable, str(ROOT / 'memory_cli.py'), 'admin', 'hook', event],
                                  cwd=str(self.repo), input='{}', text=True, capture_output=True)
            self.assertEqual(0, proc.returncode)
        self.assertTrue(old_settings['hooks'])
        with patch.object(hooks, 'resolve_command', return_value={'prefix': prefix, 'form': 'absolute', 'detail': 'fixture'}):
            update = hooks.hooks_command(self.repo, git_hooks=True)
        self.assertTrue(update['ok'], update)
        self.assertIn('memory_cli.py admin hook', (self.repo / '.git/hooks/pre-commit').read_text())
        removed = hooks.hooks_command(self.repo, remove=True, git_hooks=True)
        self.assertTrue(removed['ok'], removed)
        self.assertFalse((self.repo / '.git/hooks/pre-commit').exists())
        self.assertFalse((self.repo / '.git/hooks/post-merge').exists())
        settings = self.repo / '.claude/settings.local.json'
        self.assertTrue(not settings.exists() or not json.loads(settings.read_text()).get('hooks'))


if __name__ == '__main__':
    unittest.main()
