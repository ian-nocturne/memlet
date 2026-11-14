"""Memlet 9.4: checks the lead added when the units were put together."""
import random
import unittest

from memlet import memlet_tidy as mt


def _scan_reference(lessons, visible):
    """The 9.3.0 grouping: for every kept lesson, scan every lesson. Kept here as the specification."""
    work = {"similarityWork": 0}
    order = mt.kept_lesson_order(lessons, visible)
    key = {i: "\n".join(mt.md.normalised_quote(raw) for raw in lessons[i]["current"]) for i in order}
    order = [i for i in order if key[i].strip()]
    pairs, complete = mt._similar_pairs(list(dict.fromkeys(key[i] for i in order)), work)
    out, taken = [], set()
    for keep in order:
        if keep in taken:
            continue
        taken.add(keep)
        members = [i for i in order if i not in taken and (key[i] == key[keep] or key[i] in pairs[key[keep]])]
        if not members:
            continue
        taken.update(members)
        kind = mt.v.TIDY_SAME_TEXT if all(key[i] == key[keep] for i in members) else mt.v.TIDY_SIMILAR_TEXT
        out.append((kind, keep, members))
    return out, complete


def _lessons(texts):
    lessons = {}
    for n, text in enumerate(texts):
        ident = "DRM-%04d" % n
        lessons[ident] = {"id": ident, "tier": "approved" if n % 3 == 0 else "pending", "current": [text],
                          "timestamp": "2026-11-%02dT00:00:00Z" % (1 + n % 28), "item": {"id": ident}}
    return lessons


class TidyGrouping(unittest.TestCase):
    WORDS = ["never", "always", "deploy", "refund", "export", "retry", "wrapper", "friday", "staging", "customer",
             "emails", "mask", "branch", "release", "smoke", "tests", "cache", "warm", "before", "after"]

    def texts(self, count, seed):
        rng = random.Random(seed)
        out = []
        for _ in range(count):
            words = rng.sample(self.WORDS, rng.randint(5, 8))
            out.append(" ".join(words))
            if rng.random() < 0.3:  # an exact repeat and a near repeat
                out.append(" ".join(words))
                out.append(" ".join(words + [rng.choice(self.WORDS)]))
        return out

    def test_lookup_gives_the_same_groups_in_the_same_order_as_a_full_scan(self):
        for seed in range(12):
            lessons = _lessons(self.texts(60, seed))
            visible = list(lessons)
            try:
                expected = _scan_reference(lessons, visible)
            except KeyError as exc:  # the helper's record shape changed: fail loudly, not silently
                self.fail("reference could not read the lesson records: %r" % (exc,))
            self.assertEqual(expected, mt._duplicates(lessons, visible, {"similarityWork": 0}), seed)

    def test_grouping_work_is_linear_when_nothing_matches(self):
        counts = []
        for size in (400, 1600):
            lessons = _lessons(["unique%d alpha%d beta%d gamma%d delta%d" % (n, n, n, n, n) for n in range(size)])
            counters = {}
            groups, complete = mt._duplicates(lessons, list(lessons), {"similarityWork": 0}, counters)
            self.assertEqual([], groups)
            counts.append(counters["groupingWork"])
        self.assertLessEqual(counts[1], counts[0] * 4 + 8)   # four times the lessons, four times the work
        self.assertLess(counts[1], 1600 * 4)                  # and nowhere near lessons squared (2,560,000)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------------------------------------------
# One long-lived process (an MCP server, a library caller) must answer like a fresh process after every decision,
# with the per-read indexes of 9.4 and the model proposals of `dream --tidy` in play together.

import json as _json
import os as _os
import subprocess as _subprocess
import sys as _sys
from pathlib import Path as _Path

import test_v93_tidy as _tidy_tests
from unittest import mock as _mock

_REAL_MERGE_FUNCTIONS = mt._merge_functions  # captured before any test replaces the tidy adapter's lookup
from memlet import handover_bridge as _hb, memlet_bridge as _mb, memlet_dreamtidy as _dt, memlet_graph as _graph, memlet_skillbook as _sb, memlet_wiki as _mw

_HERE = _Path(__file__).resolve().parent
def probe_view(repo):
    """What a reader would be told, through five readers. Run in this process and in a fresh child process."""
    from memlet import memlet_tidy as mt
    repo = str(repo)
    items = _mb.dream_items(repo, tiers=("approved", "hint", "pending"))["items"]
    nb = _graph.neighbors(repo, "billing.py", budget=4000)
    tidy = mt.tidy_report(repo)
    hand = _mw.build_handover(repo)
    lessons = hand.get("approvedLessons") or (hand.get("capsule") or {}).get("approvedLessons") or []
    view = {
        "items": sorted((i["id"], i["tier"], tuple(i.get("mergedFrom") or ())) for i in items),
        "recall": _mb.unified_recall(repo, "refund retry wrapper", budget=4000)["formatted"],
        "graph": {g: [i["id"] for i in v] for g, v in nb.get("groups", {}).items()},
        "tidy": {k: [e.get("keep") or e.get("id") for e in tidy.get(k, [])]
                 for k in ("duplicates", "saidAgain", "retire", "unattached", "promote", "lapsedMerges",
                           "mergedLostFiles", "modelSaysSame")},
        "handoverLessons": _json.dumps(lessons, sort_keys=True, default=str),
        "mergeStates": _mb.merge_view(repo).get("states", {}),
        "skillCount": _sb.build_skill(repo).get("includedCount", 0),
        "tidyStatusLessons": _dt.dream_tidy_status(repo).get("counts", {}),
        "inspectCfg": _mw.read_config(_Path(repo))[0],
    }
    return _json.loads(_json.dumps(view, sort_keys=True, default=list))


_CHILD = ("import json, sys; sys.path.insert(0, sys.argv[1]); import test_v94_integration as T; "
          "print(json.dumps(T.probe_view(sys.argv[2]), sort_keys=True))")


class OneProcessAnswersLikeAFreshOne(_tidy_tests.Base):
    A = 'Never call refund() in billing.py without the retry wrapper; it double-charges on timeouts.'
    B = 'Always wrap refund() calls in billing.py with the retry helper, never call it bare, or a timeout charges the customer twice.'
    C = 'Never export customer emails from billing.py reports without masking them first.'

    def setUp(self):
        super().setUp()  # the tidy unit tests default to "merge not available"; here the real functions are used
        real = _mock.patch.object(mt, '_merge_functions', _REAL_MERGE_FUNCTIONS)
        real.start()
        self.addCleanup(real.stop)

    def fresh(self, repo):
        out = _subprocess.run([_sys.executable, '-B', '-c', _CHILD, str(_HERE), str(repo)], capture_output=True,
                              text=True, env=dict(_os.environ), cwd=str(repo), timeout=300)
        self.assertEqual(0, out.returncode, out.stderr[-2000:])
        return _json.loads(out.stdout)

    def here(self, repo):
        return probe_view(repo)

    def same(self, repo, step):
        self.assertEqual(self.fresh(repo), self.here(repo), step)

    def test_reads_after_each_decision_and_each_model_proposal(self):
        repo = self.make_repo()
        a = self.learn(repo, 'mon.log', '2026-11-02T10:00:00Z [user] ' + self.A)[0]
        b = self.learn(repo, 'wed.log', '2026-11-04T10:00:00Z [user] ' + self.B)[0]
        c = self.learn(repo, 'fri.log', '2026-11-06T10:00:00Z [user] ' + self.C)[0]
        self.same(repo, 'after learning')
        self.assertTrue(_mb.learn_approve(repo, [a, c], approver='Robin')['ok'])
        self.same(repo, 'after approving two')

        def runner(_argv, _prompt, *, stdin_text='', **_kw):
            rows = [_json.loads(line) for line in stdin_text.splitlines() if line.startswith('{"id"')]
            ids = [r['id'] for r in rows if 'refund' in r['text']]
            return _json.dumps([{'same': ids}])

        run = _dt.dream_tidy_run(repo, via=('claude',), runner=runner)
        self.assertTrue(run['ok'], run)
        self.assertEqual(1, len(run['addedProposals']), run)
        here = self.here(repo)
        self.assertEqual([a], here['tidy']['modelSaysSame'])        # the approved one is suggested as kept
        self.same(repo, 'after a model proposal')
        self.assertTrue(_mb.learn_approve(repo, [b], approver='Robin')['ok'])
        self.same(repo, 'after approving the other lesson of the proposal')
        self.assertTrue(_mb.learn_merge(repo, a, [b], approver='Robin')['ok'])
        here = self.here(repo)
        self.assertEqual([], here['tidy']['modelSaysSame'])         # merged: the proposal is no longer open
        self.assertNotIn(b, [i[0] for i in here['items']])
        self.same(repo, 'after the merge')
        self.assertTrue(_mb.learn_reject(repo, [a], approver='Robin', reason='changed my mind')['ok'])
        here = self.here(repo)
        self.assertIn(b, [i[0] for i in here['items']])              # the merge lapsed: the member is back
        self.assertNotIn(a, here['graph'].get('blocks', []))
        self.same(repo, 'after rejecting the kept lesson')
        self.assertTrue(_mb.learn_unmerge(repo, [b], approver='Robin')['ok'])
        self.same(repo, 'after the unmerge')

    def test_public_readers_ignore_ambient_read_state_after_external_change(self):
        repo = self.make_repo()
        a = self.learn(repo, 'mon.log', '2026-11-02T10:00:00Z [user] ' + self.A)[0]
        b = self.learn(repo, 'wed.log', '2026-11-04T10:00:00Z [user] ' + self.B)[0]
        self.assertTrue(_mb.learn_approve(repo, [a, b], approver='Robin')['ok'])
        self.assertTrue(_mb.learn_merge(repo, a, [b], approver='Robin')['ok'])
        callback_ran = []
        callback_err = []

        class _ExternalMutationStore:
            def memory(store_self, *_a, **_kw):
                try:
                    reject_code = (
                        "import sys; sys.path.insert(0, sys.argv[1]); "
                        "from memlet import memlet_bridge as mb; "
                        "res = mb.learn_reject(sys.argv[2], [sys.argv[3]], approver='Robin', reason='out-of-process'); "
                        "assert res['ok'], res"
                    )
                    out = _subprocess.run(
                        [_sys.executable, '-B', '-c', reject_code, str(_HERE), str(repo), a],
                        capture_output=True,
                        text=True,
                        env=dict(_os.environ),
                        cwd=str(repo),
                        timeout=120,
                    )
                    self.assertEqual(0, out.returncode, out.stderr[-2000:])
                    after = self.here(repo)
                    self.assertNotIn(a, [i[0] for i in after['items']])
                    self.same(repo, 'inside unified_recall store callback after external child-process reject')
                    callback_ran.append(True)
                except BaseException as exc:
                    callback_err.append(exc)
                    raise
                return {"retrieval": {"items": []}}

        res = _mb.unified_recall(
            repo, "refund retry wrapper", budget=4000, store=_ExternalMutationStore(), task="t1"
        )
        if callback_err:
            raise callback_err[0]
        self.assertTrue(res['ok'], res)
        self.assertEqual([True], callback_ran)
