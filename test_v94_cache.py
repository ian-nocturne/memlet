"""Read memos are optional: dropping them cannot remove part of an answer."""
from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import shutil
import subprocess
import sys
import unittest
import warnings
from pathlib import Path
from unittest import mock

import test_v94_readstate as fixtures
import test_v94_cost as cost
from memlet import memlet_dream as md

MODES = ('normal', 'none', '3', '11', '37')


@contextlib.contextmanager
def cache_policy(mode):
    """All active _read_cache accesses reach bucket; inactive accesses are already empty."""
    original = md._ReadState.bucket
    accesses = [0]

    def bucket(state, name, factory=dict):
        accesses[0] += 1
        if mode == 'none':
            return factory()
        if mode != 'normal' and accesses[0] % int(mode) == 0:
            state._buckets.clear()
        return original(state, name, factory)

    with mock.patch.object(md._ReadState, 'bucket', bucket):
        yield accesses


def _bootstrap(mode):
    # Runs in each CLI/hook process too; no product switch or persistent setting.
    return '''from memlet import memlet_dream as md
mode = %r
original = md._ReadState.bucket
accesses = 0
def bucket(state, name, factory=dict):
    global accesses
    accesses += 1
    if mode == 'none':
        return factory()
    if mode != 'normal' and accesses %% int(mode) == 0:
        state._buckets.clear()
    return original(state, name, factory)
md._ReadState.bucket = bucket
md._utc_now_iso = lambda: '2026-06-01T00:00:00Z'
''' % mode


def _command_probe(context):
    """The independent comparison's explicit CLI/MCP/hook commands and include/budget cases."""
    source = cost._PROBE_SCRIPT
    for old, new in (('src/billing.py', 'billing.py'), ('src/auth.py', 'billing.py'),
                     ('src/router.py', 'stale.py'), ('DRM-APP-BLK', context['id_a']),
                     ('DRM-MULTILINE', context['id_b']), ('DRM-CHAIN-C', context['id_c'])):
        source = source.replace(old, new)
    source = source.replace('range(60, 321)', '(0, 60, 75, 95, 96, 97, 131, 132, 133, 202, 203, 225, 226, 320, 1200, 8000)')
    start = source.index('def run_hook_inproc(')
    end = source.index('\ndef neutralise_bridge_doc', start)
    source = source[:start] + '''def run_hook_inproc(event, payload, *, scope=None):
    argv = [sys.executable, '-B', str(tree_root / 'memory_cli.py'), 'admin', 'hook', event]
    if scope:
        argv += ['--scope', scope]
    result = subprocess.run(argv, input=json.dumps(payload), text=True, capture_output=True, cwd=repo)
    return {'code': result.returncode, 'stdout': norm(result.stdout), 'stderr': norm(result.stderr)}

''' + source[end:]
    pos = source.index('cli_commands = {')
    source = source[:pos] + '''import itertools
for width in range(7):
    for include in itertools.combinations(('rules', 'safety', 'staleRules', 'dream', 'hints', 'memory'), width):
        for budget in (60, 202, 8000):
            budget_sweep['all_include_' + str(include) + '_' + str(budget)] = mb.unified_recall(repo, 'refund', include=include, budget=budget)
''' + source[pos:]
    return source


class TestOptionalReadMemos(unittest.TestCase):
    setUpClass = classmethod(fixtures.TestV94ReadState.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.TestV94ReadState.tearDownClass.__func__)
    clone = fixtures.TestV94ReadState.clone

    def test_whole_recipe_matrix_without_or_with_evicted_memos(self):
        recipes, _ = fixtures._build_recipes()
        self.assertGreaterEqual(len(recipes), 146)
        baseline = None
        timestamp = md._utc_now_iso()
        for mode in MODES:
            answers = {}
            with cache_policy(mode) as accesses, mock.patch.object(md, '_utc_now_iso', return_value=timestamp), warnings.catch_warnings():
                warnings.simplefilter('ignore', ResourceWarning)
                for key, recipe in recipes.items():
                    context = self.clone('cache-recipe')
                    stdout, stderr = io.StringIO(), io.StringIO()
                    try:
                        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                            answer = fixtures._canon(recipe(context), context['repo'])
                        answers[key] = (answer, fixtures._canon(stdout.getvalue(), context['repo']),
                                        fixtures._canon(stderr.getvalue(), context['repo']))
                    finally:
                        shutil.rmtree(context['repo'])
            self.assertGreater(accesses[0], 100)
            if baseline is None:
                baseline = answers
            else:
                for key in recipes:
                    with self.subTest(mode=mode, recipe=key):
                        self.assertEqual(baseline[key], answers[key])

    def test_cli_hook_differential_without_or_with_evicted_memos(self):
        monitor = self.scratch / 'cache-monitor'
        monitor.mkdir()
        script = self.scratch / 'cache-probe.py'
        script.write_text(_command_probe(self.fixture))
        baseline = None
        for mode in MODES:
            context = self.clone('cache-active')
            env_root = self.scratch / 'cache-cli-env'
            env = cost._isolated_env(env_root)
            env['PYTHONPATH'] = os.pathsep.join((str(monitor), str(fixtures.HERE)))
            (monitor / 'sitecustomize.py').write_text(_bootstrap(mode))
            try:
                result = subprocess.run([sys.executable, '-B', str(script), str(fixtures.HERE),
                                         str(context['repo']), 'ordinary'], cwd=self.scratch, env=env,
                                        capture_output=True, text=True, timeout=600)
                self.assertEqual(0, result.returncode, result.stderr)
                answers = json.loads(result.stdout)
                if baseline is None:
                    baseline = answers
                    self.assertEqual(28, len(answers['cli']))
                    self.assertEqual(2, len(answers['hooks']))
                else:
                    for section in baseline:
                        for key in baseline[section]:
                            with self.subTest(mode=mode, section=section, case=key):
                                self.assertEqual(baseline[section][key], answers[section][key])
            finally:
                shutil.rmtree(context['repo'])
                shutil.rmtree(env_root)
                store = self.scratch / 'mcp_store_ordinary'
                if store.exists():
                    shutil.rmtree(store)

# Deliberate accumulators are passed by the caller or scoped outside _ReadState.
NON_MEMO_ACCUMULATORS = {
    'attachment projection': 'Explicit per-call output: paths and completeness metadata consumed by the rendering caller.',
    'TRUST_PROJECTIONS': 'Scoped output list for the current node, with a recomputation fallback if no projection was emitted.',
    'WORK_COUNTERS': 'Caller-owned instrumentation dictionary; forgetting evidence must not erase measured work.',
}


def _accumulators_survive_evidence_drops(self):
    mb = fixtures.mb
    context = self.clone('accumulator-drops')
    counters, sink = {}, []
    token = md._TRUST_PROJECTIONS.set(sink)
    try:
        with md.work_counters(counters), md._enter_read_scope() as state, mb._lesson_path_projection(context['repo'], None) as projection:
            mb.dream_items(context['repo'], _path_context=projection)
            paths = {key: list(value) for key, value in projection['paths'].items()}
            self.assertTrue(paths)
            sink.append('completed projection')
            md.call_foreign(lambda: self.assertTrue(all(var.get() is None for var in md._read_context_vars())))
            self.assertFalse(state._buckets)
            self.assertEqual(paths, projection['paths'])
            self.assertEqual(['completed projection'], sink)
            self.assertEqual(1, counters['foreignCallsWithEvidence'])
            md._read_cache('lock probe')['evidence'] = True
            with md.queue_lock(context['repo'] / 'accumulator.lock'):
                self.assertFalse(state._buckets)
                self.assertEqual(paths, projection['paths'])
                self.assertEqual(['completed projection'], sink)
                self.assertEqual(1, counters['foreignCallsWithEvidence'])
    finally:
        md._TRUST_PROJECTIONS.reset(token)


TestOptionalReadMemos.test_accumulators_survive_evidence_drops = _accumulators_survive_evidence_drops


if __name__ == '__main__':
    unittest.main()
