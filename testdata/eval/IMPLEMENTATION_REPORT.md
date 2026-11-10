# D3 — sealed memory evaluation

## Changed files

- `memlet/memlet_eval.py`: experiment library and public `eval_selftest()`.
- `memlet/cli_eval.py`: auto-loaded `eval` command, Benchmarks admin group.
- `test_v91_eval.py`: 17 scratch-only, deterministic tests.
- `testdata/eval/{dream,heldout,questions}.json`: synthetic source/question fixtures.
- `testdata/eval/README.md`: usage, format, constraints and interpretation.
- This report.

## Spec status

1. **Done:** temporal split, immutable masked snapshots, fail-closed missing/out-of-order timestamps, file-overlap/content-duplication protection, sealed questions and per-step hash verification. Arms share one token estimator/ceiling, contexts and estimates are stored. Approved/hint eligibility delegates to the lazy bridge contract. Snapshot aliases require whole-session equality.
2. **Done:** init, imported/model-authored questions, bounded/resumable answering, deterministic or different-vendor model scoring, Wilson reports, and offline selftest. Model calls exist only behind `run_vendor`; attempts are persisted before calls. Dry runs print prompts and planned counts; call caps are per invocation. Unknown call outcomes cannot be retried automatically.
3. **Done with explicitly narrowed interpretation:** handover is the existing digest capsule over approved session-source quotes in an isolated view, rather than the entire live repository capsule. Historical instruction-file versions cannot be proven from session timestamps, so live instructions are excluded. Graph invokes existing approved typed one-hop recall and keeps verified pin quotes/typed sections, stripping free-form metadata headers that could have been authored after the split. Graph currently includes approved lessons only; `--hints` adds separately labelled flat hints. Rawlog is deterministic BM25 over frozen dream text.
4. **Done:** report gives counts, accuracy, Wilson 95%, mean estimated context tokens, all required provenance, attempted calls (including unknown outcomes), omissions/skips and a stable result digest; n<30 is labelled small. No accuracy claim is made. Deterministic fake-model fixture checks the complete offline pipeline and fails for an injected broken rawlog arm.

## Existing tests touched

None.

## New test groups

- Canary, equal budgets and prompt shape: held-out text cannot enter contexts; answerers do not receive keys.
- Citation integration: real typed recall and real digest capsule consume an approved fixture quote; held-out snapshot and future-title canaries are excluded; extra citation fields are ignored and hashes/verified flags checked.
- Seals/manifest: post-seal edits fail on run/report.
- Resume/change/uncertain call: completed answers are reused; changed contexts/source sessions refuse mixing; unknown outcomes refuse duplicate calls.
- Scoring: literal positive/negative patterns, pathological regex-looking strings treated literally, separate judge vendors, bounded judge counts, stable report digests.
- Dry-run/caps/authorship: no model calls on previews or zero caps; author prompts contain only held-out data.
- Temporal data: missing/descending timestamps, duplicate sessions across windows, native JSONL extraction, missing native timestamps.
- Wilson: hand values for 5/10, 10/10 and empty samples.
- Containment: all generated artifacts/locks stay under `.memlet/eval/`; symlinked output refused.
- Selftest: expected five-arm table, plus deliberately broken-arm failure.

## Result/config/disk changes

No config keys or existing disk formats changed. New `.memlet/eval/<run>/` artifacts:
`manifest.json`, `manifest-seal.json`, `questions.json`, `questions-seal.json`,
`contexts.json`, `state.json`, `locks/`, and `view/AGENTS.md`. All writes use
managed helpers under the Dream queue lock; its lock directory is explicitly
routed into the eval run. Dry-run can write eval-local locks/views but calls no
model. Artifact ceiling is 2 MiB; oversize writes fail before replacement.

Library entry points: `init_eval`, `seal_questions`, `run_eval`, `score_eval`,
`report_eval`, `eval_selftest`; validation errors raise ValueError (or input
shape errors). CLI converts input/validation failures to `ok:false,exitCode:2`.
Report additions include `arms` (`n`, `correct`, `accuracy`, `wilson95`,
`meanContextTokens`, `deterministic`, `judged`, `skipped`, `smallSample`), split,
sealedSetHash, budget, memletVersion, calls/callCounts, vendorsModels,
questionVendorsModels, judgeVendorsModels, unresolvedCalls, omitted, note,
resultDigest and formatted output. No approvals are created by production eval.

## Wiring requests

No router changes: `cli_plugins.py` already discovers `memlet.cli_eval`.
The lead should ensure `testdata/eval/` is included in distribution artifacts:
selftest deliberately fails with a missing-fixture message if packaging omits it.
D1's citation addendum is implemented (file/start/end/sha256/verified, unknown
keys ignored). D1 vendor integration is lazy because its module is absent in
this branch. Tests replace that absent vendor boundary with a deterministic
fake; no real CLI/provider integration was exercised. Bridge provider stubs are
used only while its contract function is absent.

## Risks and limits

- The bundled selftest has no approved lessons: only rawlog is expected to pass.
  It is a plumbing/selftest fixture, not evidence that graph memory is better.
  Approved capsule/graph behavior has a separate integration test.
- Whole-snapshot equality is intentionally strict and may omit real lessons
  when preprocessing differs; users should use native transcript JSONL or
  matching exported visible-session lines. Omissions are visible in reports.
- Eval snapshots are frozen and future source edits refuse resume rather than
  adopting changed data. Regenerate under a new run ID after changes.
- Digests are integrity checks, not signatures; an attacker able to rewrite
  both sealed artifacts and hashes can forge them. Local answer/state artifacts
  are trusted experiment outputs, not authenticated audit logs.
- Prompt delimiting is a defense, not proof of model resistance to injection;
  deterministic keys/manifest remain outside model control. Judge results are
  explicitly separated from deterministic scores.
- The capsule and graph rendering choices above narrow the experiment for
  temporal safety and must be retained in any published provenance.
- Existing suite emits ResourceWarning messages about unrelated SQLite/file
  handles. No existing warnings/tests were suppressed or weakened.

## Verification

Commands (twice each, output filtered to the required summary lines):

```
python3 -m unittest discover -s . -p "test_*.py" -t . 2>&1 | grep -E "^(Ran |OK|FAILED|ERROR:|FAIL:)"
Ran 558 tests in 131.229s
OK
Ran 558 tests in 113.217s
OK

/usr/bin/python3 -m unittest discover -s . -p "test_*.py" -t . 2>&1 | grep -E "^(Ran |OK|FAILED|ERROR:|FAIL:)"
Ran 558 tests in 193.861s
OK
Ran 558 tests in 156.376s
OK
```

The system interpreter was launched with PYTHONHOME, PYTHONEXECUTABLE and
__PYVENV_LAUNCHER__ unset. Raw logs are in `/private/tmp/d3-final-python-{1,2}.log`
and `/private/tmp/d3-final-system-{1,2}.log`. The runs above cover the final
17-test suite; focused tests were also rerun on both interpreters after the
last capsule Git-ceiling isolation edit (17 tests, OK on each).

`python3 memory_cli.py check . --format json`: ok=true, errorCount=0,
warningCount=0, exitCode=0. `git diff --check`: clean. CLI `admin eval selftest`
returns ok=true and exactly the expected five-arm table. No model/network was
used in tests or verification.
