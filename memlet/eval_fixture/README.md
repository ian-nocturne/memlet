# Sealed memory evaluation

`memlet admin eval init --repo . --sessions session_logs --dream-until 2026-01-01`
creates a run. Seal user-authored questions with `questions --run RUN --from questions.json`,
answer with `run --run RUN --via codex --max-calls 20`, score with `score --run RUN`,
and print `report --run RUN --format markdown`. `--repo` works before or after the action.
`run`, `questions --via`, and `score --judge` explicitly call a model through your own CLI.
They support `--dry-run` and `--max-calls` (per invocation). No model is called by default.

The default protocol is **prospective**. Items, nodes and their edges are admitted only
when every recorded creation/proposal, verification and approval timestamp is usable
and at or before the split. An item with no usable timestamp is excluded. Counts by
admission/exclusion reason are reported per context. Undated legacy memory will often
be excluded. `init --allow-post-split-memory` selects **retrospective**: later-approved
memory is allowed only when its verified citations lie wholly in the dream window.
Memory selected or approved after the split may reflect held-out outcomes. Every report
starts with its protocol and states this limitation for retrospective experiments.
These are local integrity checks, not authenticated historical evidence.

Sessions must be repo-contained regular JSON arrays (or objects with `records`) of
`timestamp`/`text` objects, or native Claude/Codex JSONL transcripts. Blank JSONL lines
are skipped; non-object lines fail. Hidden/tool text is excluded by the native reader.
Symlinks and files with more than one hard link are refused. Copy external sessions
into the repository first. Missing/descending timestamps, cross-split files and identical
content across windows fail closed. Timestamp grammar on both Python versions is
`YYYY-MM-DD[THH:MM[:SS[.f{1,9}]]][Z|±HH:MM]`; fractional precision beyond microseconds
is truncated. Naive times mean UTC. A named split file uses its last timestamp.
Limits: 200 files, 10,000 records/file, 64 KiB visible message, 1 MiB aggregate visible
text; native transcripts and other input files have a **2 MiB file limit**.

Questions: 1–200 objects `{id,text,citation:{file,start,end},expectedAny?,forbiddenAny?}`.
Citations use frozen visible lines starting at 1. Positive keys must have at least three
characters, occur casefolded in the cited held-out text, and occur in neither the question
nor fixed answer prompt. Literal token-boundary matching avoids substring credit.
Forbidden-only and keyless questions require a different-vendor judge. Model-authored
keys still require human review: hostile held-out text can influence their selection.
Answer prompts never contain keys or held-out citations. Model instruction compliance
is not guaranteed. Judged, deterministic and combined accuracies are shown separately.

First real answering freezes the manifest, sealed set, answering vendor/model and ALL
question × arm contexts. Resume rebuilds the whole matrix and names differing keys;
answers use saved contexts. Changed sources, seals, budgets or arms require a new run.
Contexts are stored per question and raw call output in bounded blobs, avoiding one
large contexts/state file. Every answer and score records both seals. A bounded metadata
projection is checked before a call. Returned raw text is saved before parsing; definite
failures are retryable and separately counted. Unknown outcomes remain `started` and
block duplicates. Resolve only after investigating with
`eval resolve --run RUN --resolve KEY --as failed|done-lost`: this logs an explicit
resolution; `failed` permits retry, `done-lost` prevents another call for that key.
Invalid completed question/judge output is preserved; it never becomes an unknown call.

Every arm shares a context token ceiling and estimator. Rawlog uses cached BM25 over
only dream-window lines. Flat uses only eligible quoted lessons. Graph uses existing
one-hop recall with source and approval checks; free-form title/scope headers are removed.
Handover uses the real capsule builder on an isolated eval-local instruction view.
Source quotes are masked before capsule generation; final clipping happens at line
boundaries and the text checksum is renewed and verified. Tiny budgets that cannot
carry its integrity header are refused. Contexts record truncation, original token
estimates, included/dropped lines and payload estimates; reports expose these per arm.
Payload for graph/handover counts whole included lesson quotes; other arms count all
context text. This makes boilerplate overhead visible, but is not a semantic token measure.
Missing `dream_items` uses the single `_approved_items_fallback`, reading real approved,
verified queue items; the contract function automatically takes over when installed.
Snapshot aliases must equal an entire dream session. No pending items are substituted.

Reports include Wilson intervals, small-sample flags (n < 30), vendor/model per arm,
protocol, hints, corpus/manifest/question digests, scoring-method accuracies, calls,
failed attempts, unresolved calls and omissions. Digests are not signatures against
someone able to rewrite all artifacts. Read-only requests for missing runs create nothing.

`eval selftest` runs offline with an injected deterministic vendor, real transcript ingest,
keyword proposal and library approval by `selftest` inside a temporary repository.
The three questions cover a retrievable lesson, a held-out-only leak token and a keyless
judge case. Every lesson arm succeeds and fails; none misses the lesson; every arm misses
the leak token. A different-vendor judge handles the keyless case, and a same-vendor
judge must be refused. Expected correct counts: none=1, handover=flat=graph=rawlog=2,
out of three questions. Adversarial tier/hash/verified probes must also be excluded.
These synthetic results establish no general accuracy claim.
