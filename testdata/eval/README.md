# Memory eval fixtures and usage

The fixture is synthetic, has one question, and establishes no accuracy claim.
Its expected correct counts are none=0, handover=0, flat=0, graph=0, rawlog=1:
there are deliberately no approved lessons. A separate unit test constructs an
approved graph and verifies real capsule and one-hop recall output.

From a repository containing timestamped sessions:

```
memlet admin eval --repo . init --sessions session_logs --dream-until 2026-01-01 --budget 1200
memlet admin eval --repo . questions --run RUN --from questions.json
memlet admin eval --repo . run --run RUN --via codex --max-calls 20 --dry-run
memlet admin eval --repo . run --run RUN --via codex --max-calls 20
memlet admin eval --repo . score --run RUN
memlet admin eval --repo . report --run RUN --format markdown
memlet admin eval selftest
```

Input sessions are repo-contained JSON arrays (or objects with `records`) of
`timestamp` / `text` objects, or native Claude/Codex JSONL transcripts. Native
transcripts reuse Memlet's visible-message extractor; tool and hidden messages
are blank. Every record must have a timestamp; missing/descending times, files
crossing the split, and duplicate content across windows are rejected. Naive
ISO dates/times mean UTC. File splits use the named session's last timestamp.
No user transcript directories are discovered. Limit: 200 session files, 10,000
records per file, 64 KiB per visible message, 1 MiB aggregate visible text,
2 MiB per artifact. Sessions outside the repo must first be copied into it.

Questions are a JSON array of `{id,text,citation:{file,start,end},expectedAny?,forbiddenAny?}`.
Citations address the frozen, visible session lines, starting at 1. Keys are
case-insensitive **literal substring patterns**, never regular expressions.
A positive key occurring in question text is rejected. With no nonempty keys,
scoring requires an explicit different-vendor judge. Judge results remain model
judgments; they are separately counted. Transcript prompt injection cannot
change deterministic scoring keys, the split, or the manifest. Prompts label
all evidence as untrusted; model compliance itself is not guaranteed.

`questions --via VENDOR --count N --max-calls 1` is the explicit model author
path and sees only held-out evidence. `run` sees only text and dream context,
never citations/keys. All model operations use the lazy vendor_cli boundary;
each attempted call is durably counted before invocation. Unresolved calls
fail closed on resume rather than making an uncertain paid request twice.
`--max-calls` is per invocation, not a lifetime cap. Dry runs print prompts and
planned calls and do not invoke models; they may create eval-local locks/views.

All arms share one offline token estimate and hard context ceiling. The
handover arm runs the existing digest capsule builder on an eval-local view
containing only approved dream source quotes. This deliberately excludes live
instruction files, since their historical content cannot be proven from session
timestamps. It is a session-memory capsule experiment, not a full repository
handover benchmark. Hints, when enabled, are separately labelled in flat memory.
The graph arm uses existing approved one-hop recall with reverified frozen
pins; free-form title/scope/target headers are removed after selection because
they may have been authored after the split. Typed sections, source quotes and
supersession warnings remain. The rawlog arm uses deterministic BM25 ranking.

A bridge snapshot is eligible only if its entire contents equal one complete
frozen dream session. This strict alias rule can omit memories when transcript
preprocessing differs; omissions are reported, never treated as successful
retrieval. Missing `dream_items` is also reported; pending lessons are never
substituted. Resuming recomputes contexts and refuses changed source sessions,
context digests, or answer vendors. Use a new run after changing memory.

The report includes Wilson 95% intervals, n, correct counts, context estimates,
skips, deterministic/judge counts, attempted model calls (including unresolved),
vendors/models, version, budget, split, question seal and result digest. Samples
below 30 are explicitly small. Digests detect accidental edits; they are not
signatures against an actor able to rewrite all artifacts. Answer/state files
are local trusted experiment outputs, not signed audit records.
