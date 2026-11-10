"""`iter_explicit_anchors` must return exactly what `EXPLICIT_ANCHOR_RE.finditer` returns, in linear time."""

import random
import time
import unittest

from prism_memory.audit import ANCHOR_PATH_MAX_CHARS, EXPLICIT_ANCHOR_RE, iter_explicit_anchors


def _view(matches):
    return [(m.span(), m.group("path"), m.group("symbol")) for m in matches]


class AnchorScanEquivalenceTests(unittest.TestCase):
    CASES = [
        "",
        "plain text with no anchors at all",
        "Always call `mod.py::charge` through the retry wrapper.",
        "see src/auth/session.ts::login and src/auth/session.ts#L12, then Makefile::build",
        "a.py::b#c d.py#e::f ::g #h x.py:: y.py# z.py::9bad",
        "./rel/x.py::f ~/home/x.py::f /abs/dir/x.py::f ../up/x.py::f",
        "(pkg/mod.py::Class.method), [pkg/mod.py::fn]. `a-b_c.d/e.f.tsx::Comp`",
        "x.py::a.b-c. trailing.dot.py::name.- next.py::ok",
        "http://example.com/path/file.py#fragment and file.py::sym#frag",
        "nested::colons::a.py::f::g.py::h and ##a.py##b",
        "tools/Makefile#target GNUmakefile::all notMakefile::x Makefilex::y",
        "한글 경로 src/앱.py::함수 and src/app.py::fn 끝",
        "a.pyx::f a.py2::f a.py_::f a.py::f",
    ]

    def test_documented_cases_match_finditer(self):
        for text in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(_view(EXPLICIT_ANCHOR_RE.finditer(text)), _view(iter_explicit_anchors(text)))

    def test_random_strings_match_finditer(self):
        rng = random.Random(20261110)
        pieces = ["a", "b", "z", "/", ".", "-", "_", ":", "::", "#", " ", "`", "py", "ts", ".py", ".ts", ".md",
                  "Makefile", "x.py::f", "src/", "9", "(", ")", "\n", ",", "L12", "A.b"]
        for _ in range(4000):
            text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 40)))
            self.assertEqual(_view(EXPLICIT_ANCHOR_RE.finditer(text)), _view(iter_explicit_anchors(text)), repr(text))

    def test_path_at_the_length_limit_is_still_found(self):
        path = "d/" * ((ANCHOR_PATH_MAX_CHARS - 10) // 2) + "x.py"
        text = "rule " + path + "::fn end"
        self.assertEqual(_view(EXPLICIT_ANCHOR_RE.finditer(text)), _view(iter_explicit_anchors(text)))


class AnchorScanLinearTimeTests(unittest.TestCase):
    def _timed(self, text):
        start = time.perf_counter()
        found = list(iter_explicit_anchors(text))
        return time.perf_counter() - start, found

    def test_long_runs_of_path_characters_are_fast(self):
        # The reviewer's shape: a rule followed by 100,000 path characters and no separator, then worse shapes.
        for label, text in [
            ("z-run", "Always call `mod.py::charge`. " + "z" * 100000),
            ("slashes", "a/" * 50000 + "x.py::f"),
            ("dots", "a." * 50000 + "py::f"),
            ("separators", "#" * 100000 + " mod.py::f"),
            ("colons", "::" * 50000 + "mod.py::f"),
            ("many-anchors", "mod.py::f " * 10000),
        ]:
            with self.subTest(shape=label):
                seconds, found = self._timed(text)
                self.assertLess(seconds, 2.0, label)
                self.assertTrue(found, label)

    def test_long_line_keeps_the_real_anchor(self):
        _seconds, found = self._timed("Always call `mod.py::charge`. " + "z" * 100000)
        self.assertEqual([("mod.py", "charge")], [(m.group("path"), m.group("symbol")) for m in found])


if __name__ == "__main__":
    unittest.main()
