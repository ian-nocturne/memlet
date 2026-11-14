"""Read evidence cannot cross foreign code or an outer lock acquisition."""
import asyncio
import contextlib
import contextvars
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
import unittest
from unittest import mock

import test_v94_readstate as fixtures
from memlet import memlet_bridge as mb, memlet_dream as md, memlet_wiki as mw


# Shared by the current and published-release subprocesses; every runner is a local fake.
_VERIFY_RACE = r'''
import json,sys,subprocess
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from memlet import memlet_bridge as b, memlet_dream as d, memlet_dreamcycle as cycle, memlet_wiki as w
root=Path(sys.argv[2]); root.mkdir(parents=True)
(root/'AGENTS.md').write_text('- Never bypass review.\n')
assert w.init_project(root)['ok']
source=root.parent/'session.log'
source.write_text('2026-11-10T00:00:00Z [user] Never retry the broken cache.py path.\n')
assert cycle.dream_ingest(root,source)['ok']
def proposer(vendor,prompt,**kw):
    fid=json.loads(kw['stdin_text'].splitlines()[0])['id'].rsplit(':',1)[0]
    return json.dumps([{'category':'repeated_pitfall','title':'Broken cache path','scope_tag':'cache.py',
        'src_spans':[fid+':1'],'edges':[{'edge_type':'BLOCKS','target':'cache.py'}]}])
assert cycle.dream_run(root,via=('claude',),runner=proposer)['ok']
nid=next(n['id'] for n in b._load_queue(root)[0]['nodes'] if d.approval_state(n,'node')=='pending')
calls=[]
def verifier(vendor,prompt,**kw):
    if prompt==cycle.VERIFY_PROMPT:
        subprocess.run([sys.executable,'-B','-c',
            "import sys;sys.path.insert(0,sys.argv[1]);from memlet import memlet_bridge as b;assert b.learn_reject(sys.argv[2],[sys.argv[3]],approver='Human')['ok']",
            sys.argv[1],str(root),nid],check=True,capture_output=True)
        calls.append('rejected')
        return json.dumps({'verdict':'SUPPORTED'})
    return '[]'
result=cycle.dream_run(root,via=('claude',),verify_with='codex',runner=verifier)
node=next(n for n in b._load_queue(root)[0]['nodes'] if n['id']==nid)
print(json.dumps({'ok':result['ok'],'errors':result.get('errors',[]),
 'discarded':any('verdict discarded' in n for n in result.get('notices',[])),
 'calls':calls,'decision':d.approval_state(node,'node'),'verifications':len(node.get('verification',[]))},sort_keys=True))
'''


class TestReadEvidenceQ4(unittest.TestCase):
    maxDiff = None
    setUpClass = classmethod(fixtures.TestV94ReadState.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.TestV94ReadState.tearDownClass.__func__)
    clone = fixtures.TestV94ReadState.clone
    child = fixtures.TestV94ReadState.child

    def test_suspension_return_raise_and_copied_context_writes(self):
        for mode in ('return', 'raise', 'context', 'asyncio'):
            with self.subTest(mode=mode):
                c = self.clone('suspension-' + mode)
                def write():
                    self.assertTrue(mb.learn_reject(c['repo'], [c['id_a']], approver='Callback')['ok'])
                async def task():
                    write()
                def callback():
                    if mode == 'context':
                        contextvars.copy_context().run(write)
                    elif mode == 'asyncio':
                        asyncio.run(task())
                    elif mode == 'return':
                        # A completed write by another process cannot emit a local write signal.
                        self.child(c, "assert t.mb.learn_reject(c['repo'],[c['id_a']],approver='Child')['ok']")
                    else:
                        write()
                        raise RuntimeError('written before raising')
                with md._enter_read_scope():
                    before = mb.dream_items(c['repo'])
                    if mode == 'raise':
                        with self.assertRaisesRegex(RuntimeError, 'written before'):
                            md.call_foreign(callback)
                    else:
                        md.call_foreign(callback)
                    after = mb.dream_items(c['repo'])
                self.assertNotEqual(before, after)
                self.assertEqual(self.child(c, "print(json.dumps(t.mb.dream_items(c['repo'])))"), after)

    def test_record_iterable_reentry(self):
        for kind in ('iterator', 'iterable'):
            c = self.clone('record-' + kind)
            seen = []
            def records():
                seen.append([v.get() for v in md._read_context_vars()])
                copied = contextvars.copy_context()
                before = mb.dream_items(c['repo'])
                self.child(c, "assert t.mb.learn_reject(c['repo'],[c['id_a']],approver='Child')['ok']")
                fresh = self.child(c, "print(json.dumps(t.mb.dream_items(c['repo'])))")
                self.assertNotEqual(before, fresh)
                self.assertEqual(fresh, mb.dream_items(c['repo']))
                self.assertEqual(fresh, copied.run(mb.dream_items, c['repo']))
                yield from []
            class Records:
                def __iter__(self):
                    return records()
            with md._enter_read_scope():
                mb.replaced_rule_notices(c['repo'], records() if kind == 'iterator' else Records(), [])
            self.assertEqual([[None] * len(md._read_context_vars())], seen)

    def test_lock_starts_from_disk_and_nested_lock_keeps_current_evidence(self):
        c = self.clone('lock')
        original_lock = md.queue_lock
        changed = []
        @contextlib.contextmanager
        def intervening_lock(path, *args, **kwargs):
            if not changed:
                changed.append(True)
                self.child(c, "assert t.mb.learn_reject(c['repo'],[c['id_a']],approver='Child')['ok']")
            with original_lock(path, *args, **kwargs) as lock:
                yield lock
        # Drive a public writer after its caller has loaded the queue, with no foreign
        # suspension between that first load and the writer's lock acquisition.
        with md._enter_read_scope():
            mb.dream_items(c['repo'])
            with mock.patch.object(md, 'queue_lock', intervening_lock):
                result = mb.learn_paths(c['repo'], [c['id_a']], paths=['billing.py'], approver='Writer')
            self.assertFalse(result['ok'])
            self.assertIn('already approved', result['error'])
            after = mb.dream_items(c['repo'])
        self.assertEqual(self.child(c, "print(json.dumps(t.mb.dream_items(c['repo'])))"), after)
        with md._enter_read_scope() as state, original_lock(c['repo'] / mb.QUEUE_REL):
            state.bucket('marker')['loaded under lock'] = True
            with original_lock(c['repo'] / mb.QUEUE_REL):
                self.assertTrue(state.bucket('marker')['loaded under lock'])

    def test_explicit_snapshots_survive_suspension_and_lock(self):
        c = self.clone('held')
        held = md._ReadState()
        with md._enter_read_scope(held):
            before = mb.dream_items(c['repo'], _read_state=held)
            context = mb.lesson_path_context(c['repo'])
            self.assertTrue(mb.valid_lesson_path(c['repo'], 'billing.py', context=context))
            md.call_foreign(lambda: self.child(c, "assert t.mb.learn_reject(c['repo'],[c['id_a']],approver='Child')['ok']; (c['repo']/'billing.py').unlink()"))
            with md.queue_lock(c['repo'] / mb.QUEUE_REL):
                self.assertEqual(before, mb.dream_items(c['repo'], _read_state=held))
                self.assertTrue(mb.valid_lesson_path(c['repo'], 'billing.py', context=context))
        self.assertNotEqual(before, mb.dream_items(c['repo']))
        self.assertFalse(mb.valid_lesson_path(c['repo'], 'billing.py'))

    def test_plain_arguments_are_free_and_failed_protocol_preserves_validation(self):
        counts = {}
        @md.fresh_read
        def receive(value):
            return value is bad
        class BadIterable:
            def __iter__(self):
                raise ValueError('protocol rejected')
        bad = BadIterable()
        with md.work_counters(counts), md._enter_read_scope() as state:
            state.bucket('evidence')['present'] = True
            for value in (None, True, 1, 1.0, 's', b'b', Path('.'), [], (), {}, set(), frozenset()):
                self.assertFalse(receive(value))
            self.assertEqual(0, counts['foreignCallsWithEvidence'])
            self.assertTrue(state._buckets)
            self.assertTrue(receive(bad))
            self.assertEqual(1, counts['foreignCallsWithEvidence'])
            self.assertEqual({}, state._buckets)

    def published(self):
        baseline = self.scratch / 'published-q4'
        if baseline.exists():
            return baseline
        archive = subprocess.run(['git', 'archive', '0ca681f'], cwd=fixtures.HERE, env=self.env, capture_output=True)
        if archive.returncode:
            self.skipTest('Published commit 0ca681f cannot be read: ' + archive.stderr.decode(errors='replace'))
        baseline.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            if sys.version_info >= (3, 12):
                tar.extractall(baseline, filter='data')
            else:
                tar.extractall(baseline)
        return baseline

    def test_verifier_rejection_equals_published_release(self):
        results = []
        for label, tree in (('current', fixtures.HERE), ('published', self.published())):
            proc = subprocess.run([sys.executable, '-B', '-c', _VERIFY_RACE, str(tree), str(self.scratch / ('verify-' + label) / 'repo')],
                                  cwd=self.scratch, env=self.env, text=True, capture_output=True)
            self.assertEqual(0, proc.returncode, proc.stderr)
            results.append(json.loads(proc.stdout))
        self.assertEqual(results[0], results[1])
        self.assertEqual({'ok': True, 'errors': [], 'discarded': True, 'calls': ['rejected'],
                          'decision': 'rejected', 'verifications': 0}, results[0])

    def test_bad_inputs_equal_published_release(self):
        c = self.clone('bad-inputs')
        snippet = '''
import sys,json
from pathlib import Path, PurePosixPath
sys.path.insert(0,sys.argv[1])
from memlet import memlet_bridge as b, memlet_graph as g, memlet_wiki as w
r=Path(sys.argv[2])
values=[None,5,[Path('approved')],[],b'approved',{'approved':1}]
out=[]
for value in values:
 for call in (lambda:b.dream_items(r,tiers=value),lambda:b.dream_items(r,files=value),lambda:g.neighbors(r,'billing.py',tiers=value)):
  try: out.append(call())
  except Exception as exc:out.append([type(exc).__name__,str(exc)])
try:out.append(w.rule_anchor_pins(PurePosixPath(r)))
except Exception as exc:out.append([type(exc).__name__,str(exc)])
print(json.dumps(out,sort_keys=True))
'''
        values = []
        for tree in (fixtures.HERE, self.published()):
            proc = subprocess.run([sys.executable, '-B', '-c', snippet, str(tree), str(c['repo'])], cwd=self.scratch,
                                  env=self.env, text=True, capture_output=True)
            self.assertEqual(0, proc.returncode, proc.stderr)
            values.append(json.loads(proc.stdout))
        self.assertEqual(values[0], values[1])

    def test_ast_taint_mutations_are_detected(self):
        examples = [
            'def read(runner):\n run=runner\n return run()',
            'def read(clock):\n return getattr(clock,"now")()',
            'def read(runner):\n run=functools.partial(runner,1)\n return run()',
            'def read(runner):\n return functools.partial(runner,1)()',
            'class Service:\n def __init__(self,provider):\n  self.fetch=provider.fetch\n def read(self):\n  return self.fetch()',
            'def read(store):\n return [x for x in store.items()]',
            'def read(store):\n for x in store:\n  pass',
        ]
        for source in examples:
            with self.subTest(source=source):
                self.assertTrue(fixtures._foreign_call_violations(source, 'fixture.py'))

    def test_link_cache_identity_cross_process_and_constant_work(self):
        import memory_skills as skills
        skills._invalidate_task_link_cache()
        connections = []
        try:
            for label, n in (('A', 4), ('B', 800)):
                path = self.scratch / ('links-' + label + '.sqlite')
                db = sqlite3.connect(path)
                connections.append(db)
                db.row_factory = sqlite3.Row
                skills.ensure_schema(db)
                db.executemany('INSERT INTO memory_links VALUES (?,?,?,?,?,?,?)',
                    [('T', 'source', label + str(i), 'related', 1.0, '', 1.0) for i in range(n)])
                db.commit()
                # Simulate recycled object ids without depending on a particular allocator.
                with mock.patch.object(skills, 'id', lambda obj: 1, create=True):
                    graph = skills._load_task_link_graph(db, 'T')
                self.assertEqual({label + str(i) for i in range(n)}, {e[0] for e in graph['adj']['source']})
                steps = []
                db.set_progress_handler(lambda: steps.append(1) or 0, 1)
                try:
                    self.assertIs(graph, skills._load_task_link_graph(db, 'T'))
                finally:
                    db.set_progress_handler(None, 0)
                self.assertLessEqual(len(steps), 24, (label, len(steps)))
            db = connections[-1]
            subprocess.run([sys.executable, '-B', '-c',
                'import sqlite3,sys;d=sqlite3.connect(sys.argv[1]);d.execute("INSERT INTO memory_links VALUES (?,?,?,?,?,?,?)",("T","source","external","related",2.0,"",1.0));d.commit()',
                str(path)], env=self.env, cwd=self.scratch, check=True, capture_output=True)
            graph = skills._load_task_link_graph(db, 'T')
            self.assertIn('external', {e[0] for e in graph['adj']['source']})
            db.execute('UPDATE memory_links SET target_id=? WHERE target_id=?', ('temporary', 'external'))
            self.assertIn('temporary', {e[0] for e in skills._load_task_link_graph(db, 'T')['adj']['source']})
            db.rollback()
            self.assertIn('external', {e[0] for e in skills._load_task_link_graph(db, 'T')['adj']['source']})
            db.execute('UPDATE memory_links SET target_id=? WHERE target_id=?', ('local', 'external'))
            db.commit()
            self.assertIn('local', {e[0] for e in skills._load_task_link_graph(db, 'T')['adj']['source']})
        finally:
            skills._invalidate_task_link_cache()
            for db in connections:
                db.close()


# Resource/protocol results are allowed only at sites which keep using them through
# an explicit suspension. An arbitrary cursor or parser result is not plain data.
_FOREIGN_RESULT_ALLOWLIST = {
    ("memlet.cli_anchors", "register"): "Argparse parser/action objects are configured only through suspended parser calls.",
    ("memlet.cli_dream", "register"): "Argparse parser/action objects are configured only through suspended parser calls.",
    ("memlet.cli_eval", "register"): "Argparse parser/action objects are configured only through suspended parser calls.",
    ("memlet.cli_graph", "register"): "Argparse parser/action objects are configured only through suspended parser calls.",
    ("memlet.cli_skillbook", "register"): "Argparse parser/action objects are configured only through suspended parser calls.",
    ("memlet.cli_tidy", "register"): "Argparse parser/action objects are configured only through suspended parser calls.",
    ("memlet.cli_plugins", "add_format_option"): "Argparse retains the concrete Memlet format action; the reader does not invoke it.",
    ('memlet.memlet_dream', '_foreign_context'): 'The injected resource manager and entered resource are used through suspended protocols.',
    ('memlet.memlet_dream', '_normalise_read_arguments'): 'A failed input protocol retains the original object so the reader owns validation.',
    ('memlet.audit', 'make'): 'The caller finder returned by its factory is invoked only through the suspended adapter.',
}


def _plain_foreign_data(value):
    if type(value) in {type(None), bool, int, float, str, bytes, Path, type(Path())}:
        return True
    if type(value) in {list, tuple, set, frozenset, sqlite3.Row}:
        return all(_plain_foreign_data(item) for item in value)
    if type(value) is dict:
        return all(_plain_foreign_data(k) and _plain_foreign_data(v) for k, v in value.items())
    return False


def _result_violation(result, site):
    if _plain_foreign_data(result):
        return False
    if site not in _FOREIGN_RESULT_ALLOWLIST:
        return True
    if site[1] in {'register', 'add_format_option'}:
        kind = type(result).__module__ + '.' + type(result).__qualname__
        return kind not in {
            'argparse.ArgumentParser', 'argparse._StoreAction', 'argparse._StoreTrueAction',
            'argparse._SubParsersAction', 'argparse._ExtendAction', 'argparse._MutuallyExclusiveGroup',
            'memlet.cli_plugins._Format', 'memlet.cli_skillbook._RepoAction',
        }
    return False


def _foreign_result_test(self):
    fixtures._public_interfaces()  # Load the explicit audited module inventory.
    recipes, _ = fixtures._build_recipes()
    failures = set()
    original = md.call_foreign
    def check(fn, *args, **kwargs):
        caller = sys._getframe(1)
        site = (caller.f_globals.get('__name__'), caller.f_code.co_name)
        result = original(fn, *args, **kwargs)
        if _result_violation(result, site):
            failures.add((site, type(result).__module__ + '.' + type(result).__qualname__))
        return result
    with contextlib.ExitStack() as stack:
        for name, module in list(sys.modules.items()):
            if module is None or not (name.startswith('memlet.') or name in {'memory_skills', 'memory_intelligence', 'memory_cli', 'memlet_mcp'}):
                continue
            for attr, value in list(vars(module).items()):
                if value is original:
                    stack.enter_context(mock.patch.object(module, attr, check))
        for key, recipe in sorted(recipes.items()):
            fixtures._expected_error(lambda: recipe(self.clone('results-' + '-'.join(key))))
    self.assertEqual([], sorted(failures))
    self.assertTrue(all(len(reason) >= 12 for reason in _FOREIGN_RESULT_ALLOWLIST.values()))
    with sqlite3.connect(':memory:') as db:
        cursor = db.execute('SELECT 1')
        self.assertTrue(_result_violation(cursor, ('fixture', 'read')))
        self.assertTrue(_result_violation(cursor, ('memlet.cli_graph', 'register')))
        self.assertFalse(_result_violation(cursor.fetchall(), ('fixture', 'read')))
    self.assertTrue(_result_violation(object(), ('fixture', 'parser')))


TestReadEvidenceQ4.test_foreign_results_are_plain_or_explicitly_allowlisted = _foreign_result_test
