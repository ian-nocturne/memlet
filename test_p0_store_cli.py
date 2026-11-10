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
            code = memory_cli.cli()
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
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(Path, 'cwd', return_value=Path(tmp)), mock.patch.object(memory_cli.subprocess, 'run', side_effect=FileNotFoundError):
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
            for args, repo, query in [(('rule-recall','fix auth'),'.','fix auth'), (('rule-recall','fix auth','--repo',tmp),tmp,'fix auth'), (('rule-recall',tmp,'--task','fix auth'),tmp,'fix auth'), (('--task','fix auth','rule-recall',tmp),tmp,'fix auth'), (('rule-recall',tmp,'fix auth'),tmp,'fix auth')]:
                with self.subTest(args=args), mock.patch('prism_memory.memlet_recall.recall', return_value={'ok':True}) as recall:
                    self.assertEqual(0, self.run_cli(*args)[0])
                    recall.assert_called_once_with(repo,query,k=3,safety=False)

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
        self.assertEqual('8.4.0', proc.stdout.strip())
        self.assertEqual('8.4.0', VERSION)


if __name__ == '__main__':
    unittest.main()
