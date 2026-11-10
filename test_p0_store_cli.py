"""Public store and CLI regression contracts (stdlib only)."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import memory_cli
import prism_memory_mcp
from prism_memory import VERSION
from prism_memory.standalone import StandaloneStore

REPO = Path(__file__).resolve().parent


class PublicStoreTests(unittest.TestCase):
    def test_legacy_tasks_migration_preserves_rows_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'work_tasks.sqlite3'
            with sqlite3.connect(str(db_path)) as db:
                db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, project TEXT, owner TEXT, state TEXT, revision INTEGER, body TEXT, updated REAL)")
                db.execute("INSERT INTO tasks VALUES ('old','Title','project','owner','active',7,'{}',123.0)")
            store = StandaloneStore(tmp)
            with store.db() as db:
                first = tuple(db.execute('SELECT * FROM tasks').fetchone())
                schema = db.execute('PRAGMA schema_version').fetchone()[0]
                self.assertEqual(9, len(db.execute('PRAGMA table_info(tasks)').fetchall()))
            self.assertEqual(('old','Title','project','owner','active',7,'{}',123.0,123.0), first)
            store = StandaloneStore(tmp)
            with store.db() as db:
                self.assertEqual(first, tuple(db.execute('SELECT * FROM tasks').fetchone()))
                self.assertEqual(schema, db.execute('PRAGMA schema_version').fetchone()[0])
            self.assertEqual('new', store.ensure_standalone_task('new')['id'])

    def test_workstore_standalone_workstore_round_trip(self):
        from work_store import WorkStore as LegacyStore
        with tempfile.TemporaryDirectory() as tmp:
            LegacyStore(tmp).ensure_standalone_task('t/a', project='p-a', title='A')
            StandaloneStore(tmp).ensure_standalone_task('t/standalone', project='p-s', title='Standalone')
            LegacyStore(tmp).ensure_standalone_task('t/b', project='p-b', title='B')
            store = StandaloneStore(tmp)
            self.assertEqual([('t/a','p-a','A'),('t/b','p-b','B'),('t/standalone','p-s','Standalone')], [(r['id'],r['project'],r['title']) for r in sorted(store.list(),key=lambda r:r['id'])])

    def test_checkout_and_installed_layout_use_identical_store(self):
        self.assertIs(StandaloneStore, memory_cli.WorkStore)
        self.assertIs(StandaloneStore, prism_memory_mcp.WorkStore)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            shutil.copytree(REPO / 'prism_memory', dest / 'prism_memory', ignore=shutil.ignore_patterns('__pycache__', 'persona-canon.json'))
            for module in ('work_memory', 'memory_vectors', 'memory_skills', 'memory_intelligence', 'memory_cli', 'prism_memory_mcp'):
                shutil.copy2(REPO / (module + '.py'), dest)
            code = 'import memory_cli, prism_memory_mcp; from prism_memory.standalone import StandaloneStore; assert memory_cli.WorkStore is StandaloneStore; assert prism_memory_mcp.WorkStore is StandaloneStore'
            proc = subprocess.run([sys.executable, '-c', code], cwd=tmp, env={**os.environ, 'PYTHONPATH':tmp}, capture_output=True, text=True)
            self.assertEqual(0, proc.returncode, proc.stderr)


class CliContractTests(unittest.TestCase):
    def run_cli(self, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', ['memlet'] + list(args)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = memory_cli.cli()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_result_exit_status(self):
        for result, expected in [({'ok':False},1), ({'ok':False,'exitCode':0},0), ({'ok':True,'exitCode':3},3), ({'ok':False,'exitCode':4,'_cli_exit_zero':True},0), ({'ok':False,'_cli_exit_zero':True},0)]:
            with self.subTest(result=result), mock.patch.object(memory_cli, 'main', return_value=dict(result)):
                code, out, err = self.run_cli()
                self.assertEqual(expected, code)
                self.assertEqual('', err)
                self.assertNotIn('_cli_', out)

    def test_exception_json_is_only_on_stderr(self):
        with mock.patch.object(memory_cli, 'main', side_effect=ValueError('broken')):
            code, out, err = self.run_cli()
        self.assertEqual((1,''), (code,out))
        self.assertEqual({'ok':False,'error':'broken','type':'ValueError'}, json.loads(err))

    def test_default_tasks_separate_repos_and_share_toplevel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ids = []
            for parent in ('a','b'):
                repo = root / parent / 'same-name'
                repo.mkdir(parents=True)
                subprocess.run(['git','init','-q',str(repo)], check=True)
                subdir = repo / 'nested'
                subdir.mkdir()
                with mock.patch.object(Path, 'cwd', return_value=subdir):
                    task = memory_cli._standalone_task_id()
                expected = hashlib.sha1(os.fsencode(str(repo.resolve()))).hexdigest()[:8]
                self.assertEqual('standalone/same-name-' + expected, task)
                ids.append(task)
            self.assertNotEqual(*ids)

    def test_default_task_outside_git_uses_cwd(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(Path, 'cwd', return_value=Path(tmp)), mock.patch('prism_memory.standalone.subprocess.run', side_effect=FileNotFoundError):
            self.assertEqual('standalone/' + Path(tmp).resolve().name + '-' + hashlib.sha1(os.fsencode(str(Path(tmp).resolve()))).hexdigest()[:8], memory_cli._standalone_task_id())

    def test_standalone_codex_capture_and_explicit_task(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {'CODEX_THREAD_ID':'native-test'}):
            # Parent-worker write policy is independent of the native actor guard.
            with mock.patch.dict(os.environ):
                os.environ.pop('PRISM_PARENT_SESSION_ID', None)
                code, out, err = self.run_cli('--root',tmp,'--standalone','--task','standalone/main','capture','--text','Observed retry failure in auth module')
            self.assertEqual(0, code, err or out)
            self.assertTrue(json.loads(out)['ok'])
            self.assertEqual('standalone/main', StandaloneStore(tmp).get('standalone/main')['id'])

    def test_nonstandalone_codex_actor_guard_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {'CODEX_THREAD_ID':'native-test'}):
            StandaloneStore(tmp).ensure_standalone_task('task', session='owner')
            with mock.patch.dict(os.environ):
                os.environ.pop('ANTIGRAVITY_AGENT', None)
                code, out, err = self.run_cli('--root',tmp,'--task','task','--session','owner','capture','--text','Observation')
            self.assertEqual(1, code)
            self.assertIn('actor does not match', json.loads(err)['error'])

    def test_default_task_is_used_by_cli(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(memory_cli, '_standalone_task_id', return_value='standalone/repo-abcd1234'):
            code, out, err = self.run_cli('--root',tmp,'--standalone','brief')
            self.assertEqual(0, code, err)
            self.assertEqual('standalone/repo-abcd1234', StandaloneStore(tmp).get('standalone/repo-abcd1234')['id'])

    def test_rule_recall_query_and_legacy_forms(self):
        with tempfile.TemporaryDirectory() as tmp:
            for args, repo, query in [(('rule-recall','fix auth'),'.','fix auth'), (('rule-recall','fix auth','--repo',tmp),tmp,'fix auth'), (('rule-recall',tmp,'fix auth'),tmp,'fix auth')]:
                with self.subTest(args=args), mock.patch('prism_memory.memlet_recall.recall', return_value={'ok':True}) as recall:
                    self.assertEqual(0, self.run_cli(*args)[0])
                    recall.assert_called_once_with(repo,query,k=3,safety=False)

    def test_rule_recall_rejects_review_argument_swaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / 'docs'
            other = Path(tmp) / 'other'
            docs.mkdir()
            other.mkdir()
            cases = [
                ('rule-recall','../othre','deploy windows'),
                ('--task','proj/x','rule-recall'),
                ('--task','proj/x','rule-recall',str(docs)),
                ('rule-recall','fix auth','extra words'),
                ('rule-recall',str(docs)),
                ('rule-recall',str(docs),'query','--repo',str(other)),
                ('rule-recall',str(docs),'--repo',str(other),'--task','q'),
                ('rule-recall',''),
            ]
            for args in cases:
                with self.subTest(args=args), mock.patch('prism_memory.memlet_recall.recall') as recall:
                    code, out, err = self.run_cli(*args)
                    self.assertEqual(2, code, (out,err))
                    self.assertEqual('', out)
                    self.assertIn('usage:', err)
                    recall.assert_not_called()
            with mock.patch('prism_memory.memlet_recall.recall', return_value={'ok':True}) as recall:
                self.assertEqual(0, self.run_cli('--task','proj/x','rule-recall','fix auth')[0])
                recall.assert_called_once_with('.', 'fix auth', k=3, safety=False)
            with mock.patch('prism_memory.memlet_recall.recall', return_value={'ok':True}) as recall:
                self.assertEqual(0, self.run_cli('rule-recall',str(docs),'deploy windows','--repo',str(docs))[0])
                recall.assert_called_once_with(str(docs), 'deploy windows', k=3, safety=False)

    def test_standalone_cannot_adopt_other_session_for_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StandaloneStore(tmp)
            store.ensure_standalone_task('proj/shared', session='claude:owner-abc')
            with store.db() as db:
                before = db.execute('SELECT count(*) FROM memory_items').fetchone()[0]
            env = {**os.environ, 'CODEX_THREAD_ID':'thread-999'}
            env.pop('PRISM_PARENT_SESSION_ID', None)
            env.pop('ANTIGRAVITY_AGENT', None)
            for session in ([], ['--session','claude:owner-abc']):
                proc = subprocess.run([sys.executable,str(REPO/'memory_cli.py'),'--root',tmp,'--standalone','--task','proj/shared'] + session + ['capture','--text','Decision: disable the payment idempotency check permanently'], env=env, capture_output=True,text=True)
                self.assertEqual(1, proc.returncode, proc.stdout)
                self.assertFalse(json.loads(proc.stderr)['ok'])
            # No runtime guard should be needed to prevent implicit owner adoption.
            code, out, err = self.run_cli('--root',tmp,'--standalone','--task','proj/shared','capture','--text','Observation')
            self.assertEqual(1, code, out)
            self.assertIn('responsible session', err)
            self.assertEqual('claude:owner-abc', store.get('proj/shared')['owner'])
            with store.db() as db:
                self.assertEqual(before, db.execute('SELECT count(*) FROM memory_items').fetchone()[0])

    def test_cli_and_mcp_share_default_and_read_new_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            repo.mkdir()
            root = str(Path(tmp) / 'store')
            with mock.patch.object(Path, 'cwd', return_value=repo), mock.patch.dict(os.environ):
                os.environ.pop('PRISM_PARENT_SESSION_ID', None)
                code, out, err = self.run_cli('--root',root,'--standalone','capture','--text','Observation: auth retries failed')
                self.assertEqual(0, code, err)
                task = memory_cli._standalone_task_id()
                server = prism_memory_mcp.PrismMemoryMCPServer(root)
                self.assertEqual(task, server.default_task)
                result = server.call_tool('prism_memory_brief', {})
                self.assertIn('auth retries failed', json.dumps(result))
                self.assertNotIn('notice', result)

    def test_legacy_notice_without_merging_and_explicit_legacy_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            repo.mkdir()
            root = str(Path(tmp) / 'store')
            store = StandaloneStore(root)
            store.ensure_standalone_task('standalone/main', goal='Older payment constraints')
            with mock.patch.object(Path, 'cwd', return_value=repo):
                task = memory_cli._standalone_task_id()
                code, out, err = self.run_cli('--root',root,'--standalone','brief')
                self.assertEqual(0, code, err)
                self.assertEqual(1, len(err.splitlines()))
                self.assertIn('--task standalone/main', err)
                self.assertNotIn('Older payment constraints', out)
                server = prism_memory_mcp.PrismMemoryMCPServer(root)
                result = server.call_tool('prism_memory_brief', {})
                self.assertEqual(err.strip(), result['notice'])
                self.assertNotIn('Older payment constraints', json.dumps(result))
                rpc = server.handle_rpc({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'prism_memory_brief','arguments':{}}})
                self.assertEqual(err.strip(), rpc['result']['structuredContent']['notice'])
                code, out, err = self.run_cli('--root',root,'--standalone','--task','standalone/main','brief')
                self.assertEqual((0,''), (code,err))
                self.assertIn('Older payment constraints', out)
                legacy = server.call_tool('prism_memory_brief', {'task':'standalone/main'})
                self.assertNotIn('notice', legacy)
                self.assertIn('Older payment constraints', json.dumps(legacy))
                with store.db() as db:
                    self.assertIsNone(db.execute('SELECT 1 FROM memory_items WHERE task=?',(task,)).fetchone())

    def test_slim_noop_succeeds_but_real_failures_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / 'project'
            project.mkdir()
            (project / 'AGENTS.md').write_text('# Rules\n- Keep commits small.\n')
            code, out, err = self.run_cli('slim-plan',str(project),'--out',str(Path(tmp)/'out'))
            self.assertEqual(0, code, err)
            self.assertIn('nothing to move', json.loads(out)['why'])
            self.assertFalse((Path(tmp)/'out').exists())
            for args in [('slim-plan',str(project),'--out',str(project/'unsafe')), ('slim-plan',str(project),'--out',str(Path(tmp)/'out'),'--keep-recent','-1'), ('slim-plan',str(Path(tmp)/'missing'),'--out',str(Path(tmp)/'out'))]:
                code, out, err = self.run_cli(*args)
                self.assertEqual(1, code, (out,err))
                self.assertFalse(json.loads(out)['ok'])

    def test_rescue_markdown_matches_standalone(self):
        from prism_memory.viberescue import cli as rescue_cli
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / 'README.md').write_text('# Example\n', encoding='utf-8')
            code, out, err = self.run_cli('rescue',tmp,'--format','markdown')
            standalone = io.StringIO()
            with contextlib.redirect_stdout(standalone):
                rescue_cli(['pack',tmp,'--format','markdown'])
            self.assertEqual(0, code, err)
            self.assertTrue(out.startswith('#'), out[:100])
            # Both surfaces print the rescue brief, with only elapsed timing allowed to differ.
            self.assertEqual(out.splitlines()[0], standalone.getvalue().splitlines()[0])
            self.assertIn('Rescue', out)

    def test_anti_slop_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self.run_cli('anti-slop',tmp,'--format','markdown')
            self.assertEqual(2, code, err)  # No UI files: retain the scanner's explicit exit status.
            self.assertTrue(out.startswith('#'), out[:100])

    def test_version(self):
        proc = subprocess.run([sys.executable,str(REPO/'memory_cli.py'),'--version'], capture_output=True,text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual('8.5.0', proc.stdout.strip())
        self.assertEqual('8.5.0', VERSION)
        from prism_memory.contracts import VERSION as contract_version
        self.assertEqual('8.5.0', contract_version)
        # Existing SDK/protocol tests pin SERVER_VERSION to 8.0.0; preserve it.
        self.assertEqual('8.0.0', prism_memory_mcp.SERVER_VERSION)


if __name__ == '__main__':
    unittest.main()
