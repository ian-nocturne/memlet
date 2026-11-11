# Memlet (`memlet`)

**Agent memory you can re-check.** Memlet ties each rule in `CLAUDE.md`, `AGENTS.md` or `.cursorrules` to the code it talks about and tells the agent when the two have drifted apart. It also turns finished sessions into lessons, each made of the exact lines it came from, and keeps them apart from what an agent treats as settled until a person approves them.

> Twin to **Routlet** as a sibling project: *Routlet picks the route. Memlet carries the checked context.*

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB.svg)](pyproject.toml)
[![Zero Dependencies](https://img.shields.io/badge/Core_Deps-Zero_(Stdlib_Only)-success.svg)](pyproject.toml)

## The problem

Every project that uses coding agents (Claude Code, Cursor, Codex) keeps a file of rules for them. The rules describe the code. Then the code changes, nobody updates the rules, and the agent keeps following instructions that are no longer true. Most memory tools make this worse in one specific way: they keep injecting a memory long after the code it describes has changed.

## What Memlet believes

Agent memory is converging on three ideas. Agents should **dream**: go back over a finished session and keep its lessons. Memories should form an **ontology**: typed relations instead of a flat pile of notes. And context should be **disclosed progressively**: a little by default, more on demand. Memlet adopts all three and adds one condition of its own:

> **A memory is only worth handing to an agent if it can be re-checked.**

Five principles follow from that. They come from the project's design notes; some were adopted only after an experiment failed.

1. **Every memory has an address you can re-check.** A rule points at code (`file::symbol` plus a fingerprint of that symbol). A lesson points at the exact transcript lines it came from (plus their SHA-256). What has no address is labelled unverified; what no longer matches its address is quarantined.
2. **The model proposes, code verifies, a person approves.** A dream is a proposal, not a memory. Whether a citation exists, hashes match, and time runs forwards is decided by deterministic code. Only a person, by name, can approve a lesson. Unapproved lessons that two trusted checkers agreed on may be shown as *hints*, but only in a separate group labelled "not approved by a person".
3. **Quote, never rewrite.** Rules and lessons are carried verbatim. We tried letting a model summarise the rules; the summaries dropped conditions, so that path was closed.
4. **Four relations are enough.** `GOVERNS` (a rule applies to this scope), `SUPERSEDES` (this instruction replaced that one), `RESOLVES` (this workaround fixes that failure), `BLOCKS` (this dead end forbids that action). Small enough to validate without a graph database, and enough to stop an old instruction from coming back as current.
5. **Send little, and say what was left out.** Small instruction sets go in whole; only large ones are tiered. Anything trimmed for budget is counted, never dropped silently.

All of it runs locally with the Python standard library and SQLite: no daemon and no graph database. A model is called only when you run a command that names one (`memlet dream --via claude`, `memlet admin eval run --via claude`) or the optional `admin index-build`; reads, hooks and MCP tools never launch one.

## Example 1: a rule and the code it names

Take two rules in a project's `AGENTS.md`. The second was written as `file::function`. The first just mentions the function, as most rules do:

```markdown
- Always charge customers through `charge()` in `<billing file>`.
- Refunds go through `<billing file>::refund`.
```

```console
$ memlet init .                    # remember what refund() looks like today
- 1 rule(s) could be anchored to code: `memlet admin anchors suggest`
$ memlet admin anchors suggest
- `anc-bbef684cd354` (file+symbol) `AGENTS.md:2`: Always charge customers through `charge()` in `<billing file>`.
  → `<billing file>::charge` at `<billing file>:1` (function) — the rule names `<billing file>` and `charge`, defined there once at line 1 (function)
$ memlet admin anchors accept --all-unique
- Your files were not edited; the anchors live in `.memlet/config.json`.
```

Weeks later someone changes `charge(user_id, amount)` to `charge(account_id, amount, currency)`. The first rule now describes a function that no longer exists in that form:

```console
$ memlet check --changed
STALE `<billing file>::charge` (signature_drift) — AGENTS.md:2: [suggested anchor] Always charge customers through `charge()` in `<billing file>`.
ACTIVE `<billing file>::refund` (ast_hash_match) — AGENTS.md:3: Refunds go through `<billing file>::refund`.
```

From then on the stale rule is set apart everywhere an agent would read it, and the rule whose code did not change keeps flowing:

```console
$ memlet recall "charge a customer"
### anchored code changed — do not apply without re-reading the rule
- [Billing] `AGENTS.md:2-2` (sha256 793dc9f2844e) [suggested anchor] [stale: signature_drift]:
  > - Always charge customers through `charge()` in `<billing file>`.

$ memlet handover . --next-step "Finish the billing refactor"
### Active constraints (anchored symbols unchanged since pinning)
- Refunds go through `<billing file>::refund`. (`AGENTS.md:3`)
### Quarantined stale anchors (not handed over)
- [suggested anchor] `<billing file>::charge`: signature_drift
```

`[suggested anchor]` means the author did not write that anchor: Memlet proposed it and a person accepted it. The rule file itself is never edited.

(Output shortened. `<billing file>` stands for the real path, a Python file under an `app` folder: this README is itself checked by `memlet check`, so it cannot name a file that does not exist in this repository. The commands and lines are otherwise what Memlet printed.)

## Example 2: a dream, and the four relations

This is a real run against the Claude CLI in a scratch repository. A session ended with these five lines:

```text
2026-11-11T10:00:00Z [user] Deploys go out with `make release` from the main branch.
2026-11-11T10:05:00Z [assistant] I ran make release and the smoke test failed because the cache was cold.
2026-11-11T10:06:00Z [user] From now on do not use make release; deploy only with <deploy script>, which warms the cache first.
2026-11-11T10:09:00Z [assistant] Deployed with <deploy script> and the smoke test passed.
2026-11-11T10:10:00Z [user] And the old tip about passing --skip-smoke is withdrawn, never use it.
```

```console
$ memlet dream --via claude --verify-with claude --transcript session2.log
## Dream run
- Batches: 1; calls: 4; failed batches: 0
- Rejected candidates: 0
```

Memlet started `claude -p` itself, with every tool off, in an empty temporary folder, and showed it each line with an id. The model may send back only a category, a scope, relation types, line citations and a short title. Here one model (haiku) proposed and a second (sonnet) checked each eligible proposal, with `dream.agreement: "model"` enabled for same-vendor hints. Nothing is approved yet:

```console
$ memlet learn --pending
- `DRM-57dcb2bf896f99ee` [superseded_directive]
  old_pin [SUPERSEDED — DO NOT APPLY AS CURRENT] [<snapshot>:1 87a2f09b]: replaced — do not apply as current: Deploys go out with `make release` from the main branch. (`<snapshot>:1-1`)
  replacement_pin [ACTIVE — APPLY THIS] [<snapshot>:3 621061f9]: current instruction: From now on do not use make release; deploy only with <deploy script>, which warms the cache first. (`<snapshot>:3-3`; SUPERSEDES source)
  tier=pending; no trusted agreement; proposers=claude/tool-free; verification=none
- `DRM-eb836c7e1511beda` [repeated_pitfall]
  > I ran make release and the smoke test failed because the cache was cold. (`<snapshot>:2-2`; BLOCKS source)
  tier=pending; vetoed by claude/sonnet: MISSING_CONDITION. The lesson omits a condition required by the cited evidence.; proposers=claude/tool-free; verification=claude:MISSING_CONDITION
- `DRM-cf04d035ece3c7b4` [workaround draft]
  > I ran make release and the smoke test failed because the cache was cold. (`<snapshot>:2-2`)
  > Deployed with <deploy script> and the smoke test passed. (`<snapshot>:4-4`; RESOLVES source)
  tier=hint; two models of the same vendor agreed; proposers=claude/tool-free; verification=claude:SUPPORTED
- `DRM-331374b872ebd5af` [repeated_pitfall]
  > And the old tip about passing --skip-smoke is withdrawn, never use it. (`<snapshot>:5-5`; BLOCKS source)
  tier=hint; two models of the same vendor agreed; proposers=claude/tool-free; verification=claude:SUPPORTED
```

After `memlet learn --approve DRM-57dcb2bf896f99ee DRM-cf04d035ece3c7b4 --by "ray"`, the two approved lessons and the remaining hint come back in separate groups, and the replaced instruction is quarantined next to the current one:

```console
$ memlet recall "deploy release"
### Approved lessons (human-approved Dream items; transcript citations re-verified)
- [DRM-57dcb2bf896f99ee] Superseded Quarantine (`SUPERSEDES`)
  [REPLACED — do not apply as current] Deploys go out with `make release` from the main branch. (`<snapshot>:1-1`)
  [CURRENT] From now on do not use make release; deploy only with <deploy script>, which warms the cache first. (`<snapshot>:3-3`) [unanchored: no_code_anchor]
- [DRM-cf04d035ece3c7b4] [RESOLVES]
  > I ran make release and the smoke test failed because the cache was cold. (`<snapshot>:2-2`)
  > Deployed with <deploy script> and the smoke test passed. (`<snapshot>:4-4`; RESOLVES source) [unanchored: no_code_anchor]

### Unreviewed hints — not approved by a person
Agreement basis is labelled per item; citations check out, but verify in code before relying on them. ...
- [hint; two models of the same vendor agreed]
  > And the old tip about passing --skip-smoke is withdrawn, never use it. (`<snapshot>:5-5`; BLOCKS source)

$ memlet handover . --format markdown
### Approved lessons (human-approved; citations re-verified in .memlet/dream/snapshot/)
- [REPLACED — do not apply as current] Deploys go out with `make release` from the main branch. ⟶ [CURRENT] From now on do not use make release; deploy only with <deploy script>, which warms the cache first. (<snapshot>:1, <snapshot>:3)
- [RESOLVES] I ran make release and the smoke test failed because the cache was cold. ⟶ Deployed with <deploy script> and the smoke test passed. (<snapshot>:2, <snapshot>:4)
- Omitted for token budget: hints 1
```

Things to notice:

- **The lesson is the transcript, not a summary.** It is line 2 (the failure) and line 4 (the fix), verbatim, joined by `RESOLVES`. The model chose the lines and the relation; it wrote none of the text.
- **A hint is not a lesson.** The `BLOCKS` hint on line 5 passed both models, but no person has read it. Hints stay in their own group, are trimmed first when space is short, and never become active constraints. Both checkers here were Claude models, so it is labelled as same-vendor agreement, the weaker kind.
- **The second model can disagree.** It judged the pitfall on line 2 `MISSING_CONDITION`. That veto is permanent for the hint tier; only a person can still approve it.
- **`SUPERSEDES` always needs a person.** Line 3 replaced line 1, so the pair is cited together and checked for time order, but only `BLOCKS` and `RESOLVES` can become hints. Once approved, `recall` and `handover` show the pair as a quarantine: the old line marked `[REPLACED — do not apply as current]`, the new one `[CURRENT]`. `GOVERNS`, the fourth relation, ties a lesson to a scope and likewise needs approval.

(Output shortened; `<snapshot>` stands for the stored copy of the transcript under `.memlet/dream/snapshot/`, and `<deploy script>` for a script path in the transcript, for the same reason as `<billing file>` above. Without a model, `memlet learn --transcript` proposes lessons with keyword rules instead; the queue and the approval step are the same.)

## From principle to code

| Principle | Built today (9.1) | Not yet, or weaker than it sounds |
| :--- | :--- | :--- |
| 1. An address you can re-check | `init`, `check`, `check --changed` / `--accept`; stale rules set apart in `recall`, `handover`, hooks and MCP; Python symbols compared by AST, including names rebound elsewhere in the module; `admin anchors` proposes anchors for rules that only mention a function; exact TS/JS comparison with tree-sitter if a repository opts in | TS/JS is a lexical scan by default and never `active`. Anchor suggestions are proposals, not proofs of uniqueness. |
| 2. Propose, verify, approve | `dream --via claude` with an optional second-model check; keyword `learn`; MCP hosts can submit as an untrusted proposer; trust tier (`approved`, `hint`, `pending`) computed on every read; approval only by name, never over MCP | Only the `claude` CLI can be launched, so the default two-vendor agreement cannot produce a hint today. Approvals and receipts are integrity records, not signatures. |
| 3. Quote, never rewrite | Lesson text is always the cited lines; a model writes only a title; restructuring (`admin slim-plan`) moves lines with a preservation proof | The model-written brief (`admin brief-build`) failed its fidelity check and is hidden. |
| 4. Four relations | Four-verb schema; `SUPERSEDES` needs both citations and forward time; no cycles; one-hop recall; an approved pair is shown as replaced / current in `recall` and `handover`; lineage view in `memlet ui` | Relation labels from a model are often coarse; review them before approving. |
| 5. Send little, say what was left out | `recall` (works without an index), handover budget with an `omitted` count, hints trimmed first; `admin eval` measures what each way of feeding memory is worth on your own sessions | No measured result is published. The size router (`admin rule-context --auto`) is not wired into the main commands or hooks. |

**What "verified" means here.** Memlet checks that an anchored symbol still exists and has not changed since it was pinned, and that a cited line still says what it said. It does not check that a rule or a lesson is true. A model's `SUPPORTED` verdict is an opinion about the citations, not an approval.

## Commands

| Command | What it does |
| :--- | :--- |
| `memlet init` | Records a baseline (a *pin*) for every code-anchored rule. `--hooks` installs the automatic loop. |
| `memlet check` | Audits instruction files and reports every pinned anchor that went stale; `--changed` is the fast version for hooks. |
| `memlet recall "<task>"` | Rules, approved lessons and (labelled) hints relevant to a task, each with its source and anchor status. |
| `memlet handover` | A small capsule for the next model: rules unchanged since pinning, stale ones quarantined. |
| `memlet learn` | Queues keyword-proposed lessons from a transcript; lists, approves and rejects queued lessons. |
| `memlet dream` | Asks a model you name to propose cited lessons; `--status` shows what is waiting. |
| `memlet ui` | Writes a self-contained HTML inspector. |
| `memlet admin <command>` | Everything else, including `anchors` and `eval`; run `memlet admin --help`. |

In a terminal these print readable text. When output is piped or captured they print JSON; `--format json|markdown` always wins.

---

## Quickstart

### 1. Install and initialise

```bash
pip install git+https://github.com/ian-nocturne/memlet.git@v9.1.1
memlet init .     # writes .memlet/config.json, .memlet/ledger.sqlite3, .memlet/wiki/INDEX.md
```

`memlet init` is safe to run again: existing pins and your settings in `.memlet/config.json` are kept, and only new anchors are added. `memlet init --force` re-pins everything. Add `.memlet/` to your `.gitignore` unless you want to share it; the Dream queue and transcript snapshots under it hold conversation text.

From a source checkout (Python 3.9 or newer):

```bash
pip install -e .
python3 -m unittest discover -s . -p "test_*.py" -t .
pip install -e ".[treesitter]"   # optional: exact TS/JS fingerprints (see section 3)
pip install -e ".[semantic]"     # optional: local ONNX e5-small vectors for the task-memory store
```

### 2. Check your instruction files

```bash
memlet check . --format markdown                       # exits 1 on errors (or pass --exit-zero)
memlet check . --base HEAD~1 --head HEAD               # git-diff blast radius
memlet check . --format sarif > memlet-check.sarif     # SARIF v2.1.0 for GitHub Code Scanning
memlet check --changed                                 # working tree vs HEAD; exits 1 when anything is stale
memlet check --accept "memlet/audit.py::audit_repository"   # accept one anchor's new state after re-reading its rule
```

After `memlet init`, a pinned anchor that went stale is an error, committed or not; anchors that are merely not verified are warnings. With nothing changed, `--changed` still lists anchors that are stale right now.

### 3. Anchor rules that only mention code (`memlet admin anchors`)

```bash
memlet admin anchors suggest                 # read-only list of proposals
memlet admin anchors accept anc-… anc-…      # accept by id
memlet admin anchors accept --all-unique     # accept every file+symbol proposal; --dry-run to preview
memlet admin anchors list                    # accepted anchors, whether they still attach, current status
memlet admin anchors drop anc-…              # remove one
```

- Two confidence levels. `file+symbol`: the rule names the file, and the file defines the name once. `single-definition-found`: the rule names only the function, and the scan found one definition; the folders it did not scan are listed. `--all-unique` accepts only `file+symbol`; check each `single-definition-found` and accept it by id.
- Dependency and build folders, version-control and Memlet folders, and nested repositories are not scanned, so "one definition" means one in what was scanned. A suggestion is a proposal, not a proof.
- Accepted anchors live in `.memlet/config.json` with a ledger entry. If the rule's text changes or moves to another section, the anchor detaches and `anchors list` says why.
- Exact TS/JS comparison: install the `treesitter` extra and add `"anchors": {"exactParsers": ["tree-sitter"]}` to `.memlet/config.json`. Installing the package alone changes nothing. Only new pins use it; re-pin an existing anchor (`memlet init --force` or `check --accept`) to move it. A pin made with one parser version reports `exact_parser_basis_repin_required` under another. `MEMLET_EXACT_PARSERS=0` turns autoloading off.

### 4. Recall and hand over

```bash
memlet recall "refund retry logic"
memlet handover . --next-step "Finish the refund refactor" \
  --rejected "Retrying inside the webhook handler double-charges" --format markdown
memlet handover . --next-step "Finish the refund refactor" --write   # also saves .memlet/wiki/HANDOVER.md
memlet handover --verify .memlet/wiki/HANDOVER.md                      # was it edited since?
memlet admin import . --dry-run --format markdown                      # bring existing rule files into .memlet/wiki/
```

`recall` returns rules (with anchor status; stale ones listed apart and counted in `staleAnchors`), approved lessons, hints in their own group, and task memories when you pass `--standalone` or `--task`. Without a built rule index the rules are parsed directly from the files, which works out of the box. (`memlet admin index-build` is optional and calls the `claude` CLI.)

The capsule carries `repoHead`, `activeConstraints`, `lexicalConstraints` (TS/JS lexical matches), `pathExistsAnchors` and `unpinnedAnchors` (carried, clearly not verified), `quarantinedStaleAnchors`, `rejectedApproaches`, `executionProof`, `nextAction`, `checksum`, and `authorityBoundary: "read-only-context-only: execution and external side-effect approvals are NOT inherited"`. The checksum detects edits; it does not prove who wrote the capsule.

Approved lessons travel in the capsule too (`approvedLessons`, after the active constraints): one line each, relation first, and a replaced instruction is marked `[REPLACED — do not apply as current]` next to `[CURRENT]`. When space is short, hints are dropped first, then approved lessons, then the rest. `omitted` counts what the token budget dropped; `listCapOmitted` counts what a per-group item cap dropped.

#### Routlet reads the handover

`memlet handover --write` also replaces `.memlet/handover.json` and `.memlet/capsules.json` for Routlet, under the same write lock as the Markdown page. They carry anchored active or compatible-unverified rules and approved lessons only; stale anchors are quarantined, and approved `BLOCKS` lessons and replaced instructions go under rejected approaches. No hint, pending lesson or model verdict enters either file.

These are snapshots: re-run `--write` after code or approvals change. Routlet compares commit heads for constraints but does not re-check working-tree edits; its reference summaries also retain no anchor status. Long quotes have a visible truncation marker and a source citation in the JSON, and omitted entries are counted. `memlet handover --verify .memlet/handover.json` checks integrity, not authentication; Routlet itself does not check that digest. A failed write lists which files were replaced; the three replacements are not atomic together.

### 5. Lessons: propose, check, approve

Two ways to propose, one queue (`.memlet/dream/APPROVAL-QUEUE.json`), one approval step.

```bash
# Keyword rules, no model call
memlet learn --transcript ~/.claude/projects/<project>/<session>.jsonl

# A model you name. Preview first: prompts, argv and planned calls, nothing launched
memlet dream --via claude --verify-with claude --transcript <session transcript> --dry-run
memlet dream --via claude --verify-with claude --transcript <session transcript>
memlet dream --status

# Review and decide; a name is required
memlet learn --pending
memlet learn --approve DRM-7306e549ab93 --by "ray"
memlet learn --reject  DRM-bfd29ed9b65e --by "ray" --reason "one-off workaround"
memlet learn --approve DRM-7306e549ab93 --by "ray" --reconsider   # change your mind later
```

**What `dream` does.** Memlet snapshots the transcript and launches the vendor CLI itself with every tool, MCP server and slash command off, no session persistence, an empty temporary working directory and a short environment allowlist. Each line is shown to the model as a JSON object, `{"id": "<file>:<n>", "text": "<line>"}`, and the transcript is marked as data, not instructions. The model returns category, scope, relations, citations (those ids) and a title, nothing else. A candidate that cites a line outside the batch, or cannot be pinned, is dropped; the reasons are printed at the top of the `memlet dream` summary and kept for `--status`. If every candidate in a batch is dropped, the batch counts as failed (non-zero exit) and is retried on the next explicit run. `--verify-with` asks a second model to judge each proposal (`SUPPORTED`, `MISSING_CONDITION`, `OUT_OF_EVIDENCE`, `WRONG_EDGE`). At most 20 batches (default 4), 64 calls and 300 seconds per run.

**Trust tiers.** Every reader computes a tier from the same queue and snapshot checks, every time:

- `approved`: a person approved it by name with `learn --approve`; its citations still verify. Approval is bound to the lesson's text and cited lines; if either changes, that one lesson goes back to pending.
- `hint`: not approved, but two trusted parties agreed on the same content, every citation verifies, it carries only `BLOCKS` or `RESOLVES` relations, nobody rejected the same quote, no trusted checker vetoed it, and its lines pass a permission-phrase screen. Hints appear only under "Unreviewed hints — not approved by a person" in `recall`, `handover` and, for the files you changed, `check --changed`. They are trimmed before verified content and never become active constraints or promoted wiki rules. Check the cited code and conditions before relying on one.
- `pending`: everything else.

A second model of the same vendor can check proposals under the default `dream.agreement: "vendor"`, provided its family differs from every proposer. Its verdict is a review aid: supported items stay pending and do not reach agents, while a trusted disagreement still vetoes hints. Switching to `"model"` later makes eligible supported items hints on the next read; switching back demotes them without another model call.

Agreement settings in `.memlet/config.json` (change `"vendor"` to `"model"` to allow same-vendor hints):

```json
{"dream": {"agreement": "vendor", "models": {"claude": "haiku", "verifier": {"claude": "sonnet"}}}}
```

- `dream.agreement` defaults to `"vendor"`: a hint needs agreement from different vendors. Only `claude` can be launched today, so with the default no hint can appear. `"model"` lets two different Claude model families agree (each model id must contain exactly one of `haiku`, `sonnet`, `opus`, `fable`, `mythos`, and the verifier must differ from every proposer); those hints say "two models of the same vendor agreed".
- A `MISSING_CONDITION`, `OUT_OF_EVIDENCE` or `WRONG_EDGE` verdict from a trusted checker vetoes the hint tier for that content permanently ("vetoed by VENDOR/MODEL: VERDICT"); a person can still approve it.
- `dream.hints: false` hides the hint tier everywhere.
- Lessons submitted by keyword rules or over MCP never count toward agreement.

**When a run goes wrong.** A batch whose candidates were all rejected is recorded as failed, with the reasons at the top of the summary. `dream` carries on with later batches, stops after three failed batches in a row, and retries failed ones only when you run it again; `memlet dream --skip-failed BATCH_ID` marks one as done. If a cited line changes after approval, the lesson goes back to pending: `memlet learn --pending` shows every cited line, changed or not; read them, then approve again with `--refresh-citations`.

**Transcripts.** Only what you typed and what the assistant finally answered is read. Tool payloads, hidden reasoning, progress narration and injected instruction files are skipped. These secret formats are redacted before anything is stored: OpenAI/Anthropic `sk-`, Stripe, GitHub, Slack, AWS access key ids, Google `AIza`, GitLab, npm, PyPI and Polar tokens, database connection URLs, URL credentials, JWTs, PEM/OpenSSH/PGP/PuTTY private-key blocks, and assignments whose name contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `CREDENTIAL` or `AUTH`. Other formats are not detected. Very large inputs are skipped rather than partly stored (files over 16 MiB, messages or lines over 16,384 characters), and the result lists what was skipped.

Approved lessons are written to `.memlet/wiki/dream-approved.md` with the text you reviewed. If you edit that page by hand, its rules stop being presented as verified until you approve again.

### 6. Make it automatic (hooks, MCP)

```bash
memlet init . --hooks --dry-run               # show exactly what would change
memlet init . --hooks                         # .claude/settings.local.json (personal)
memlet init . --hooks --dream                 # Stop/PreCompact only snapshot sessions for a later `memlet dream`
memlet init . --hooks --hooks-scope project   # shared .claude/settings.json; needs `memlet` on PATH
memlet init . --hooks --git-hooks             # also a warn-only pre-commit and a post-merge re-check
memlet init . --hooks-remove                  # remove only Memlet's entries
```

| Claude Code event | What Memlet does |
| :--- | :--- |
| `SessionStart` | Injects the pinned-and-unchanged rules, and one line counting stale anchors, lessons awaiting approval and (with `--dream`) hints and waiting sessions. Hint text is not injected. |
| `PostToolUse` (`Write\|Edit`) | Checks the edited file; if a rule anchored to it went stale, tells the model which rule and why. |
| `Stop`, `PreCompact` | Queues keyword lessons as pending, or with `--dream` only snapshots the transcript. No hook ever calls a model or approves anything. |

Hooks are opt-in and merged into your existing settings; other hooks and keys are left alone, a backup is written before the first change, and a failed install is rolled back. A hook never blocks a session: on any error it prints nothing and exits 0.

For hosts without lifecycle hooks (Codex, Cursor, any MCP client), register the `memlet-mcp` server (in Codex: a `[mcp_servers.memlet]` entry with `command = "memlet-mcp"`). It exposes 25 tools: `memlet_check`, `memlet_recall`, `memlet_handover` (never writes a file), `memlet_learn` (queue a transcript, list pending), `memlet_dream_next` and `memlet_dream_submit`, plus 19 `memlet_store_*` task-memory tools. With the two dream tools the host's own model can propose lessons; they are recorded under the untrusted proposer `host`, cannot verify, approve or set a tier, and never count toward agreement. Approving or rejecting is not available over MCP. `memlet_learn` reads only a regular `.log`/`.jsonl` file of at most 8 MiB inside the repository or under a known agent transcript folder (`~/.claude/projects`, `$CLAUDE_CONFIG_DIR/projects`, `~/.codex/sessions`, `~/.muse/sessions`); the file itself may not be a symbolic link.

### 7. Measure it yourself (`memlet admin eval`)

Whether memory helps depends on your sessions. `admin eval` runs a sealed, time-split experiment: lessons may come only from sessions before a date; questions come from sessions after it; each question is answered once per arm under the same context budget.

```bash
memlet admin eval selftest                                           # offline, fake vendors only
memlet admin eval init --dream-until 2026-10-01 --sessions session_logs --run exp1
memlet admin eval questions --run exp1 --from questions.json         # your questions, with literal answer keys
memlet admin eval run --run exp1 --via claude --dry-run              # then without --dry-run (calls the model)
memlet admin eval score --run exp1
memlet admin eval report --run exp1 --format markdown
```

Arms: `none` (no memory), `handover` (a capsule built from the approved lessons), `flat` (the admitted lessons as a list; hints only with `init --hints`, labelled), `graph` (approved lessons through one-hop typed recall), `rawlog` (BM25-selected lines from the earlier transcripts). Keyed questions are scored deterministically by literal token match. The report gives per-arm accuracy with a Wilson interval, small-sample warnings and every excluded item. `--judge` needs a model from a different vendor than the one that answered, so it cannot be used today. By default (`prospective`) only memory timestamped before the split is admitted; `--allow-post-split-memory` relaxes that and the report says so.

### 8. Inspect everything in one page (`memlet ui`)

`memlet ui .` writes `.memlet/inspector.html` (or `--out FILE`): anchor health, the benchmark, the current handover capsule, and the four-relation lineage graph with each lesson's tier and agreement label. No CDN, no network; every repository string is escaped and absolute host paths are left out.

---

## Command reference

| Command | Purpose |
| :--- | :--- |
| `memlet init [.] [--force] [--dry-run]` | Setup and baseline pins; re-running keeps existing pins and settings. |
| `memlet init --hooks [--dream] [--hooks-scope local\|project] [--git-hooks] [--dry-run]` / `--hooks-remove` | Opt-in Claude Code hooks (and git hooks); merge-only, removable. |
| `memlet check [.] [--base REF --head REF] [--format json\|markdown\|sarif]` | Static audit plus every pinned anchor; a stale pin exits `1`. |
| `memlet check --changed [--base REF]` / `--accept ANCHOR` / `--repin-legacy-ts` | Changed-files check; accept one anchor; re-pin TS/JS pins made by an earlier release. |
| `memlet recall "<task>" [--repo DIR] [--budget N]` | Rules with anchor status, approved lessons, labelled hints. |
| `memlet handover [.] [--next-step "..."] [--rejected "..."] [--write]` / `--verify FILE` | Handover capsule with a checksum; verify a saved one. |
| `memlet learn --transcript PATH` / `--pending` / `--approve ID... --by NAME [--reconsider\|--refresh-citations]` / `--reject ID... --by NAME` | Keyword proposals and the human approval gate. |
| `memlet dream [REPO] --via claude [--verify-with claude] [--transcript PATH] [--max-batches N] [--dry-run]` / `--status` | Model-proposed lessons; status, failures and dropped candidates. |
| `memlet ui [.] [--out\|--output FILE]` | Self-contained, escaped HTML inspector. |
| `memlet admin anchors suggest\|accept\|list\|drop` | Proposed anchors for rules that only mention code. |
| `memlet admin eval init\|questions\|run\|score\|report\|resolve\|selftest` | Sealed, time-split memory experiment. |
| `memlet admin --help` | Every other command: memory store, rules/wiki, Dream internals (`dream-build`, `dream-recall`), benchmarks, maintenance. |
| `memlet --version` | Prints `9.1.1`. |

Anchor states: `active` (pinned and unchanged; Python, or TS/JS with an opted-in exact parser), `compatible-unverified` (changed but signature-compatible, a TS/JS lexical match, or a Python name rebound elsewhere in the module: `symbol_rebound_in_module`), `stale` (removed, required arguments changed, or file moved), `unpinned` (no baseline yet), `path-exists` (the rule names a path only).

## Upgrading

- From 9.1.0: additive changes only; nothing changes without `handover --write` or `dream --verify-with`. The former writes the two Routlet snapshots; the latter now permits distinct models of the same vendor under `dream.agreement: "vendor"` as a review aid, while the hint policy stays the same.

### From 9.0

- Nothing changes unless you opt in. No command calls a model unless you run `memlet dream --via …` (or `admin eval` with `--via`). Hooks keep their 9.0 behaviour unless you re-install them with `--hooks --dream`. No anchor is suggested into use until you accept it. Exact TS/JS parsing stays off until you set `anchors.exactParsers`.
- New anchor reason `symbol_rebound_in_module`: a Python function or class that is reassigned elsewhere in its module is now `compatible-unverified`, not `active`. Hashes are unchanged; no re-pin is needed.
- Hints can show up in `recall`, `handover`, `check --changed`, `learn --pending`, `memlet ui` and the SessionStart count, always in their own labelled group. They can appear only after a `memlet dream` run with `dream.agreement: "model"`. `dream.hints: false` turns them off.
- Lessons with several cited lines now show every line, in order, with the line that supplies a relation marked.
- Approved lessons are now carried in the handover capsule (new key `approvedLessons`), and an approved replaced instruction is shown in `recall` and `handover` marked as replaced, next to the current one.
- If you opt into `anchors.exactParsers` and pin TS/JS anchors with 9.1, keep checking that repository with 9.1 or later: 9.0 reads those pins as `compatible-unverified` with a misleading reason.
- MCP gains `memlet_dream_next` and `memlet_dream_submit`; the 23 existing tool definitions are unchanged.
- Upgrading from 8.x: 9.0 removed the deployment-rescue, UI-scoring, starter-bundle and operational-security commands; executables are `memlet` and `memlet-mcp`; use `MEMLET_*` environment variables; the user store is `~/.memlet-store` and an older one is used only if you point at it (`--root` or `MEMLET_HOME`). Repository data from 8.5 remains readable. The Action accepts `mode: audit` only.

## Limits, stated plainly

- **One vendor.** Only the `claude` CLI can be launched, and only when it reports a claude.ai subscription login; settings that could select API-key or third-party auth are refused. `codex` and `gemini` are refused because neither CLI documents a mode with every tool off. So the default two-vendor agreement produces no hints, same-vendor hints are weaker evidence, and `admin eval score --judge` cannot be used. Launching a vendor needs POSIX process groups; elsewhere it is refused before anything starts.
- **The permission screen is a phrase list, not a proof.** It looks for English and Korean permission and bypass phrases in a hint's lines; a hint could still loosen a rule in words it does not know.
- **Integrity, not authentication.** Approvals, accepted anchors, checksums and vendor receipts detect later edits. They do not show who acted: a process that can rewrite `.memlet` can forge them.
- **A model's verdict is an opinion.** `SUPPORTED` means a second model found the citations sufficient. It is not approval.
- **Anchor suggestions are proposals.** Unscanned folders and nested repositories can hold other definitions.
- **Static only.** Python anchors are compared by AST; dynamic `getattr`, `exec`, `setattr` and monkeypatching are not evaluated, and the check that a pinned name is not rebound elsewhere in its module is static too. The default TS/JS scanner is lexical: JSX text with apostrophes and some regex literals read as syntax errors, getters/setters and overloads are not extracted, and `tsc` is not run.
- **No accuracy claim.** Recall quality and the value of lessons are not measured for this release; `admin eval` exists so you can measure them on your own sessions.

## How far it has been tested

- 797 unit tests pass on Python 3.9 and 3.13 on macOS, and again with the optional tree-sitter parser installed (core 0.23.2, grammars 0.23).
- `memlet dream` was run against the real Claude CLI in scratch repositories, about a dozen calls in total (Example 2 is one of those runs). Those runs changed the design twice: see the row format and the failed-batch rule in section 5.
- On 9.1 code, hooks installed with `--hooks --dream` ran in one headless Claude Code session (the checked rules arrived at session start, and the session was snapshotted at Stop without a model call), and six MCP tools, including `memlet_dream_next` and `memlet_dream_submit`, were called from one `codex exec` session; the submitted lesson stayed pending as an untrusted `host` proposal. The PostToolUse and PreCompact hooks were last exercised in a real session on 9.0-era code.
- The on-disk bridge was checked locally against Routlet’s actual reader, including commit drift and edits without a refreshed snapshot; no model was called.
- Linux, Windows, other MCP hosts and interactive sessions are untested.

---

## Where Memlet fits among agent memory tools

Memlet is not a general chat-memory SDK. It is built for one job: keeping a coding agent's memory honest about the current state of the repository.

| Tool | What it does well | What Memlet adds for coding agents |
| :--- | :--- | :--- |
| **ai-memory** | Git-backed Markdown wiki people can edit, broad hook coverage. | Each entry can carry a code anchor and is marked `stale` when the code changes. |
| **Mem0** | One-line SDK, automatic fact extraction, vector + graph retrieval. | No model on the default write path; constraints stored verbatim. |
| **Letta** (MemGPT) | Self-editing memory blocks, sleep-time reorganisation. | Sits on top of the CLI you already use instead of replacing the agent runtime. |
| **Zep / Graphiti** | Bi-temporal knowledge graph, fact supersession. | The same `at` / `known_at` / supersession model in one local SQLite file. |
| **claude-mem** | Session hooks, progressive disclosure. | Offline default path and anchor checks before anything is disclosed. |

*Comparison reflects each project's public repository and docs as of October 2026. Check each project's own documentation before choosing.*

## Reproducible refactoring and handover benchmark

`memlet admin benchmark --format markdown` builds four small repositories in a temporary directory, records rules before a refactor, applies it, and measures what each approach would still feed the next agent. No network or model calls; the `resultDigest` is identical on every run.

| Mode | Stale procedure injection | Optional-arg false positives | Handover dead-end prevention |
| :--- | :---: | :---: | :---: |
| Raw Markdown (static `CLAUDE.md` / `.cursorrules`) | 100% (3/3) | 0% (0/1) | 0% (0/3) |
| Unverified Wiki (timestamps, 90-day aging, file-exists check) | 66.7% (2/3) | 0% (0/1) | 66.7% (2/3) |
| Memlet (AST anchors + pins + handover quarantine) | **0% (0/3)** | **0% (0/1)** | **100% (3/3)** |

Read the numbers for what they are: four synthetic fixtures built to exercise specific failure modes. The dead-ends are recorded where only Memlet's mode reads them, so the last column follows from the setup; "Unverified Wiki" is a simplified model, not a run of any product.

## Real open-source repository audit

Before 9.0, `memlet check` was run against four public repositories at pinned commits (`vercel/ai` `e741656`, `openai-agents-python` `64cd5df`, `AutoGPT` `8d4a965`, `claude-code` `d014fc0`): 784 docs, 2,745 references, 10 `missing_path` findings (1 in `vercel/ai`, 9 in `AutoGPT`), each checked by hand and found real. These numbers predate 9.x and were not re-run; the checker's ignore list contains entries tuned on these repositories, and they are not a guarantee for others. Details: `real-oss-audit-results.json`.

## GitHub Action

```yaml
name: Memlet Check Gate
on: [pull_request]

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: ian-nocturne/memlet@v9.1.1
        with:
          mode: "audit" # only supported mode
          repo-path: "."
          base-ref: ${{ github.event.pull_request.base.sha }}
          fail-on-drift: "true"
```

Outputs are `status`, `finding-count` and `total-finding-count`; the default SARIF file is `memlet-check.sarif`. `format` is `markdown`, `json` or `sarif`; `python-version` optionally installs a Python (otherwise 3.9+ from the runner); `fail-on-drift` accepts only `true` or `false`. A result the Action cannot interpret fails closed with exit code `2`, and the SARIF file is never written through a symbolic link.

## Task-memory store

Beyond the repository commands, Memlet includes a local task-memory store under `memlet admin` (`capture`, `recall --query`, `brief`, `capsule`, `doctor` and related commands): a bi-temporal SQLite ledger (when something was true, `at`, and when it was learned, `known_at`, with supersession), BM25 retrieval with optional local vectors, Session Delta-Prompting (only what changed is sent while `coreHash` is unchanged), and procedural skills promoted only after an execution receipt. `memlet admin repo-benchmark` runs its repository-evolution benchmark; `memlet admin doctor` checks the environment and SQLite health.

## License and provenance

- Licensed under the **Apache License, Version 2.0** ([LICENSE](LICENSE)).
- Third-party notices and optional model licenses are documented in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
