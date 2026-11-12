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
