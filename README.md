# Memlet (`memlet`)

**Agent memory that checks whether the code it remembers is still the code that is there — a code-anchored memory engine and instruction drift guard for coding agents.**

> Twin to **Routlet** under Ian · Nocturne Lab: *Routlet picks the route. Memlet carries the checked context.*
> Formerly **DeltaHarness** / **Ieum Memory** / PRISM Memory. The legacy executables `ieum-memory`, `deltaharness`, and `prism-memory` still work for this release and print a one-line notice when run in a terminal.

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB.svg)](pyproject.toml)
[![Zero Dependencies](https://img.shields.io/badge/Core_Deps-Zero_(Stdlib_Only)-success.svg)](pyproject.toml)

Coding agents (`Claude Code`, `Cursor`, `Codex`, `Windsurf`) silently break when repository files move, function signatures change, `.env` keys go missing, or context compaction wipes architectural constraints. Most memory tools make this worse in one specific way: they keep injecting a memory long after the code it describes has changed.

**Memlet** anchors memories and instructions to the code they talk about. It quarantines a rule whose anchored code changed, labels everything it could not check, and never presents either as current. It is one pure-Python package (`Apache-2.0`, stdlib-only core) with six everyday commands:

| Command | What it does |
| :--- | :--- |
| `memlet init` | Records a baseline (a *pin*) for every code-anchored rule. `--hooks` installs the automatic loop. |
| `memlet check` | Audits instruction files against the repository and reports every pinned anchor that went stale; `--changed` is the fast version for hooks. |
| `memlet recall "<task>"` | Returns the rules, human-approved lessons, and memories relevant to a task, each with its source and anchor status; stale rules are set apart. |
| `memlet handover` | Builds a small capsule for the next model: unchanged-since-pinned rules, with stale ones quarantined. |
| `memlet learn` | Queues lessons from a session transcript as **pending**; nothing is used until a person approves it. |
| `memlet ui` | Writes a self-contained HTML inspector. |

Everything else (the task-memory store, Dream internals, `rescue`, `anti-slop`, `pack`, benchmarks) lives under `memlet admin <command>`; run `memlet admin --help` for the list.

**What "verified" means here.** Memlet checks that an anchored symbol still exists and has not changed since it was pinned. It does not check that the rule's sentence is true. Python symbols are compared by AST. TypeScript/JavaScript symbols are compared by a lexical scan, which cannot prove equivalence, so a TS/JS match is always reported as `compatible-unverified`, never `active`.

---

## 🧭 Where Memlet Fits Among Agent Memory Tools

There are good agent memory tools already. Memlet is not trying to be a general chat-memory SDK; it is built for one job: **keeping a coding agent's memory honest about the current state of the repository.**

| Tool | What it does well | What Memlet adds for coding agents |
| :--- | :--- | :--- |
| **ai-memory** | Single binary, Git-backed Markdown wiki that people can read and edit, no external LLM by default, broad agent hook coverage. | Each wiki entry carries a code anchor (file, symbol signature, commit). When the anchored symbol changes, the entry is marked `stale` instead of being injected as-is. |
| **Mem0** | One-line SDK, automatic fact extraction from conversations, vector + graph retrieval. | No LLM call on the default write path, so negations and exact constraints are stored verbatim. Current state is a deterministic projection of a lossless ledger tied to branches and commits. |
| **Letta** (MemGPT) | Self-editing core/archival memory blocks, agent file system, sleep-time reorganization. | Sits on top of the CLI/IDE you already use (Claude Code, Codex, Cursor) instead of replacing the agent runtime. Procedural skills are promoted only after an execution receipt. |
| **Zep / Graphiti** | Bi-temporal knowledge graph (event time vs. ingestion time), fact supersession and edge invalidation. | The same `at` / `known_at` / supersession model in a single-file local SQLite ledger with in-memory postings — no graph database, no extraction pipeline to operate. |
| **claude-mem** | Session lifecycle hooks, progressive disclosure (short index first, full body on demand). | Fully offline default path, Session Delta-Prompting (send only what changed), and anchor checks before anything is disclosed. |

*Comparison reflects each project's public repository and docs as of October 2026. Features change quickly — check each project's own documentation before choosing.*

### How Memlet combines the pieces

- **Human-readable Markdown wiki, synced both ways (`.memlet/wiki/`)** — `memlet init` writes `.memlet/config.json` (pinned anchor hashes), `.memlet/wiki/INDEX.md`, and a local SQLite ledger `.memlet/ledger.sqlite3` (`anchor_snapshots` + `wiki_events`). People read, diff, and edit `.memlet/wiki/*.md` directly; `memlet admin wiki` compares each page body with its last recorded hash and records a changed or new page as a `user_edit` event in that ledger.
- **Zero-LLM, local, bi-temporal ledger** — Every memory keeps when it was true (`at`) and when the system learned it (`known_at`), with `supersedes` / `retract` lineage. Point-in-time recall (`--at`, `--known-at`) and "why did this change?" both come from the ledger, not from a model's summary.
- **Symbol & signature anchoring** — Rules that reference code as `path::symbol` store a hash of that symbol. `memlet init` and `memlet import` record the baseline; read commands never create one. On each check an anchor resolves to:
  - `active` — a stored pin exists and the symbol is unchanged (Python only);
  - `compatible-unverified` — the body changed, an optional argument was added, a parameter type or annotation changed, or the match is a TS/JS lexical match; re-verify before trusting;
  - `stale` — symbol removed, required arguments changed, or file moved; quarantined by `handover`, `recall`, the hooks and the MCP tools, and reported as an error by `check`;
  - `unpinned` — the symbol exists but no baseline was recorded yet;
  - `path-exists` — the rule names a path only, so nothing was hashed.
- **Session Delta-Prompting** — Within one session, if `coreHash` is unchanged only the delta is sent; a compaction or session restart triggers a fresh baseline.
- **Admission gate for the task-memory store** — Each captured item is scored on three factors (utility, novelty, confidence) and low-information text is not stored. Items captured from a model are stored as hypotheses, not as rules.
- **Import what you already have** — `memlet import` reads rules from `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.cursor/rules/*.mdc`, `.ai-memory/**/*.md`, and `.claude-mem/**/*.md`, checks every code anchor in them right away, and writes the result to one wiki page with a status tag per rule.
- **Cross-Vendor Handover Capsules** — When you hit a rate limit and move from one agent to another, `memlet handover` builds a capsule under 500 tokens by default: constraints whose anchors are `active`, separately labelled groups for anything not verified, stale anchors listed as quarantined, rejected approaches (dead ends), modified-file count, and the next step. The capsule carries a SHA-256 checksum (integrity only, not a signature) that `memlet handover --verify FILE` re-checks. Execution and billing approval are never carried over to the next model. (`memlet capsule` remains for budgeted task-memory capsules.)

---

## ⚡ 1-Minute Quickstart

### 1. Install & Initialize (Zero External Dependencies Required)

```bash
pip install git+https://github.com/ian-nocturne/deltaharness.git@v8.4.0

# One-command, zero-daemon setup: .memlet/config.json, .memlet/ledger.sqlite3, .memlet/wiki/INDEX.md
memlet init .
```

`memlet init` is safe to run again: existing pins and your own settings in `.memlet/config.json` are kept, and only new anchors are added. `memlet init --force` re-pins everything. Add `.memlet/` to your `.gitignore` unless you want to share it; the Dream queue and transcript snapshots under it hold conversation text.

In a terminal the six commands print readable text. When output is piped or captured they print JSON, as before; `--format json|markdown` always wins.

From a source checkout, run the test suite (Python 3.9 or newer):

```bash
pip install -e .
python3 -m unittest discover -s . -p "test_*.py" -t .
```

*(Optional)* Enable local ONNX `e5-small` semantic vector acceleration:
```bash
pip install -e ".[semantic]"
```

### 2. Check Your Repo Instructions (`CLAUDE.md` / `AGENTS.md` / `.cursorrules`)

```bash
# Human-readable report (exits 1 if errors > 0, or pass --exit-zero).
# After `memlet init`, a pinned anchor that went stale is an error here, committed or not;
# anchors that are merely not verified are warnings.
memlet check . --format markdown

# Git-diff blast radius check (reports invalid_git_ref if a ref does not exist)
memlet check . --base HEAD~1 --head HEAD --format markdown

# Export SARIF v2.1.0 for GitHub Code Scanning (stdout is the SARIF document itself)
memlet check . --format sarif > memlet-check.sarif

# Fast check of the files you changed (working tree vs HEAD, renames and deletions included).
# With nothing changed it still lists anchors that are stale right now. Exits 1 when anything is stale.
memlet check --changed

# Accept one anchor's new state after you have re-read its rule (instead of re-pinning everything)
memlet check --accept "prism_memory/audit.py::audit_repository"
```

### 3. Recall What Applies to a Task (`memlet recall`)

```bash
memlet recall "refund retry logic"
```

One response from up to three sources, each item with a file:line or id you can open:

- **Rules** from your instruction files and wiki pages, each with the status of its code anchors. A rule whose anchor is `stale` is never listed with the others: it appears under "anchored code changed — do not apply without re-reading the rule" and is counted in `staleAnchors`. Without a built rule index the rules are parsed directly from the files and labelled as such; that works out of the box. (`memlet admin index-build` is optional and calls the `claude` CLI, a model call.)
- **Approved lessons** from the Dream queue. Pending, rejected, or no-longer-verifiable lessons are never returned; if some lessons fail verification the response says which and why, and the rest are still returned.
- **Task memories**, only when you pass `--standalone` or `--task`.

### 4. Import Existing Rules & Hand Over Context (`memlet admin import` / `memlet handover`)

```bash
# Preview: tag every imported rule with the status the real run would give it
memlet admin import . --dry-run --format markdown

# Write .memlet/wiki/imported-rules.md, pin anchors in .memlet/config.json, log an import event
memlet admin import . --from auto        # or: claude-md | cursorrules | ai-memory | claude-mem

# Handover capsule for the next model (Claude / Codex / Gemini); stale anchors are quarantined
memlet handover . --next-step "Finish the refund refactor" \
  --rejected "Retrying inside the webhook handler double-charges" --format markdown

# Also save it as .memlet/wiki/HANDOVER.md, then check later that nobody edited it
memlet handover . --next-step "Finish the refund refactor" --write
memlet handover --verify .memlet/wiki/HANDOVER.md
```

The capsule carries `repoHead`, `activeConstraints` (pinned and unchanged), `lexicalConstraints` (TS/JS lexical matches; re-check before use), `pathExistsAnchors` and `unpinnedAnchors` (rule text carried, clearly not verified), `quarantinedStaleAnchors`, `rejectedApproaches` (your `--rejected` items plus bullets under `## Rejected Approaches` / `## Dead-Ends` in wiki pages), `executionProof`, `nextAction`, `checksum`, and `authorityBoundary: "read-only-context-only: execution and external side-effect approvals are NOT inherited"`. `capsuleSignature` is kept as an older name for the same checksum. The checksum detects edits; it does not prove who wrote the capsule.

### 5. Learn From a Session, With a Human Approval Gate (`memlet learn`)

```bash
# Queue candidate lessons from a Claude Code or Codex CLI transcript (.jsonl) or a .log file
memlet learn --transcript ~/.claude/projects/<project>/<session>.jsonl

# See what is waiting
memlet learn --pending

# Approve or reject by id; a name is required
memlet learn --approve DRM-7306e549ab93 --by "ray"
memlet learn --reject  DRM-bfd29ed9b65e --by "ray" --reason "one-off workaround"

# Change your mind later: reject an approved lesson, or re-approve a rejected one
memlet learn --reject  DRM-7306e549ab93 --by "ray" --reason "no longer true"
memlet learn --approve DRM-7306e549ab93 --by "ray" --reconsider
```

- Extraction is deterministic keyword matching, with no model call. Treat the queue as a draft to review, not as findings.
- Nothing in the queue reaches `recall`, `handover`, hooks, or the MCP tools until it is approved. Approval is bound to the lesson's text and its cited transcript lines: if either changes afterwards, that one lesson goes back to pending (`memlet learn --pending` shows the approved and the current text and the reason) while the other approved lessons keep working. After re-reading it, approve again with `--refresh-citations`.
- Approved lessons are written to `.memlet/wiki/dream-approved.md` so `handover` and `recall` read them like any other rule. The promoted text is the text you reviewed (up to 8,192 characters, then cut with an explicit `…`). If you edit that page by hand, its rules stop being presented as verified until you approve or accept again.
- Only visible user and assistant text is read from a transcript. Tool payloads and hidden reasoning are skipped, and these secret formats are redacted before anything is stored: OpenAI/Anthropic `sk-`, Stripe, GitHub, Slack, AWS access key ids, Google `AIza`, GitLab, npm, PyPI and Polar tokens, database connection URLs, URL credentials, JWTs, PEM/OpenSSH/PGP/PuTTY private-key blocks, and assignments whose name contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `CREDENTIAL` or `AUTH`. Other secret formats are not detected, and secrets stored by an earlier release are not removed. Very large inputs are skipped rather than partly stored: files over 16 MiB, and single messages or log lines over 16,384 characters; the result lists what was skipped.
- The approval record is an integrity check, not authentication: anyone who can rewrite the whole queue file can forge one.

### 6. Make It Automatic (`memlet init --hooks`, MCP)

```bash
memlet init . --hooks --dry-run     # show exactly what would change
memlet init . --hooks               # install into .claude/settings.local.json (personal)
memlet init . --hooks --hooks-scope project   # shared .claude/settings.json; needs `memlet` on PATH
memlet init . --hooks --git-hooks   # also a warn-only pre-commit and a post-merge re-check
memlet init . --hooks-remove        # remove only Memlet's entries
```

Hooks are strictly opt-in and are merged into your existing Claude Code settings; other hooks and keys are left alone, and a backup is written before the first change. Installation is all-or-nothing: if any part cannot be written, everything this run wrote is rolled back. Indentation, line endings and the final newline of your settings file are kept; other whitespace may be normalised (the backup holds the original bytes). `init --hooks` also runs the normal `init` so the hooks have a `.memlet/` to work with.

| Claude Code event | What Memlet does |
| :--- | :--- |
| `SessionStart` | Injects the pinned-and-unchanged rules, and one line counting stale anchors and lessons awaiting approval. |
| `PostToolUse` (`Write\|Edit`) | Checks the edited file; if a rule anchored to it went stale, tells the model which rule and why. |
| `Stop`, `PreCompact` | Queues lessons from the session transcript as pending. Never approves anything. |

A hook never blocks a session: on any error it prints nothing and exits 0. These hooks were tested with synthetic event payloads, not inside a live Claude Code session.

For hosts without lifecycle hooks (Codex, Cursor, any MCP client), `memlet-mcp` exposes four tools next to the existing `prism_memory_*` tools: `memlet_check`, `memlet_recall`, `memlet_handover` (never writes a file), and `memlet_learn` (queue a transcript, list pending). Approving or rejecting a lesson is deliberately not available over MCP; it is a human decision made with `memlet learn --approve`. `memlet_learn` works only in a repository where `memlet init` has run, and reads a transcript only if it is a regular `.log`/`.jsonl` file of at most 8 MiB inside the repository or under a known agent transcript folder (`~/.claude/projects`, `~/.codex/sessions`, `~/.muse/sessions`, `~/.prism-memory/sessions`).

### 7. Package a Stuck Project for Static Triage (`memlet admin rescue`)

When an AI-coded app says *"Fixed!"* in chat but fails on login, DB, or deployment:

```bash
# Generates secret-scrubbed RESCUE_BRIEF.md & ONE_PROMPT_RESCUE.md
viberescue pack . --write

# Or run via the memlet CLI
memlet admin rescue . --format markdown
```

**Readiness States (Honest Static Classification)**:
- `NO_TARGETS_FOUND` (exit `2`): Empty directory or zero scannable code/SQL/config files.
- `BLOCKED` (exit `1`): One or more critical static blockers (e.g., client secret exposure, missing `.env` key when `.env` is present, table without `ENABLE ROW LEVEL SECURITY`).
- `ENV_UNVERIFIED` (exit `0`, `ok: false`): The only warnings are environment variables that could not be checked because no local `.env` file is present. If any other blocking warning is present the exit code is `1`. Memlet's own `.memlet/` folder is not scanned.
- `STATIC_CHECKS_PASSED` (exit `0`): All static code, SQL, lockfile, and instruction checks passed. This is a static result, not a guarantee that the live deployment works.

### 8. Score & Fix "AI Slop" UI and Copy (`memlet admin anti-slop`)

Check your frontend or landing page before shipping:

```bash
# Compute 0-100 Slop Score and file:line violations (exits 2 on empty dir, 1 on findings)
anti-slop scan . --format markdown

# Or run via the memlet CLI
memlet admin anti-slop . --format markdown

# Lock a Reference-First Design System (linear-precision, stripe-editorial, or apple-minimal)
# Protects existing DESIGN.md / design-tokens.css unless --force is passed (creates .bak)
anti-slop lock . --archetype linear-precision
```

### 9. Inspect Memory, Dream Ontology & Routing in One Page (`memlet ui`)

The **Nocturne Memory & Routing Inspector** renders one self-contained HTML file (no CDN, no network, no daemon) with the repository's AST anchor health, the refactoring & handover benchmark, the current handover capsule, the 4-edge Dream Ontology lineage graph (`ontologyLineage` when `.memlet/dream/APPROVAL-QUEUE.json` is present; items nobody approved, or whose citation no longer verifies, are shown as pending, never as supported), and the Routlet performance / balanced / efficiency matrix. Every repository string is HTML-escaped and absolute host paths are left out. A repository without any `path::symbol` rule shows its real, empty state and one hint on how to write such a rule.

```bash
memlet ui .                                   # writes .memlet/inspector.html after `memlet init` (alias: memlet inspector .)
memlet ui . --out memlet-inspector.html       # --out and --output are both supported
```

### 10. Dream Internals (`memlet admin dream-build` / `dream-recall`) — experimental

`memlet learn` is the supported way to use Dream. The lower-level commands remain for building a queue from your own candidate JSON:

```bash
QUEUE=/tmp/memlet-dream/APPROVAL-QUEUE.json   # any path outside .memlet/dream/, which `memlet learn` owns

# Build a PENDING approval queue (+ optional SQLite tables) from logs or .jsonl transcripts
memlet admin dream-build --snapshot ./session_logs --out-json "$QUEUE"

memlet admin dream-pending --graph "$QUEUE"
memlet admin dream-approve --graph "$QUEUE" --by "ray" <ID>...

# 1-hop subgraph recall: approved items only, cited spans re-verified by SHA-256
memlet admin dream-recall --graph "$QUEUE" --query git hooks --max-tokens 1500 --format markdown
```

**Contract & Scope Notes:**
- **Everything starts pending.** A candidate's own verdict is recorded as `claimedVerdict` and never trusted. Queues built by earlier releases must be approved again before anything is recalled.
- **Recall re-verifies by default.** Each cited `file:start-end` span is re-hashed against the snapshot. With no snapshot, recall returns nothing and exits `3`; `--allow-unverified` is an explicit, labelled opt-out.
- **Typed edges.** `GOVERNS`, `SUPERSEDES`, `RESOLVES`, `BLOCKS`. A superseded rule is rendered as `old_pin [SUPERSEDED — DO NOT APPLY AS CURRENT]` next to its `replacement_pin`.
- **Quality is not measured for this release.** An earlier internal experiment reported recall accuracy for a pipeline that used model-written candidates plus benchmark-specific tuning. That tuning was removed and the experiment was not re-run, so no accuracy number is claimed here.

### 11. Start a New Project With a Stack Harness (`memlet admin pack`)

The **Routlet × Memlet Harness Pack** writes one bundle per stack (`nextjs-supabase`, `fastapi-postgres`, `swift-ios`) with 7 files: a `CLAUDE.md` constitution (anchor conventions, session delta rules, human gates), a cross-vendor `AGENTS.md`, a `SKILL.md` with pre-flight checks, receipt and rollback, a `PITFALLS.md` of 5 recorded dead-ends that `memlet handover` carries as rejected approaches, a Routlet routing preset, Linear / Stripe / Toss design tokens (every text pair >= 4.5:1), and a `MANIFEST.json` with SHA-256 checksums.

```bash
memlet admin pack --list                                   # stack metadata
memlet admin pack --stack all --format markdown            # compute files and digests in memory, write nothing
memlet admin pack --stack nextjs-supabase --output ./harness-packs
```

### Command Reference

| Command | Purpose |
| :--- | :--- |
| `memlet init [.] [--force] [--dry-run]` | Zero-daemon setup and baseline pins. Re-running keeps existing pins and settings. |
| `memlet init --hooks [--hooks-scope local\|project] [--git-hooks] [--dry-run]` / `--hooks-remove` | Opt-in Claude Code hooks (and git hooks); merge-only, removable. |
| `memlet check [.] [--base REF --head REF] [--format json\|markdown\|sarif]` | Static instruction audit plus the state of every pinned anchor; a stale pin exits `1`. |
| `memlet check --changed [--base REF]` / `--accept ANCHOR` / `--repin-legacy-ts` | Changed-files check (falls back to currently stale pins); accept one anchor; re-pin TS/JS pins made by an earlier release. |
| `memlet recall "<task>" [--repo DIR] [--budget N]` | Rules with anchor status + approved lessons (+ task memories with `--standalone`/`--task`); stale rules listed separately. |
| `memlet handover [.] [--next-step "..."] [--rejected "..."] [--write]` / `--verify FILE` | Handover capsule with a checksum; verify a saved one. |
| `memlet learn --transcript PATH` / `--pending` / `--approve ID... --by NAME [--reconsider\|--refresh-citations]` / `--reject ID... --by NAME` | Pending lesson queue with a human approval gate; approvals can be revoked. |
| `memlet ui [.] [--out\|--output FILE]` | Self-contained, escaped HTML inspector. |
| `memlet admin --help` | Every other command, grouped: memory store, rules/wiki, Dream, rescue / anti-slop / pack, benchmarks, maintenance. |
| `memlet --version` | Prints `8.4.0`. |

**Upgrading from an earlier release**

- Old top-level names such as `memlet wiki`, `memlet import`, `memlet rescue` still work unchanged. In a terminal they print one line pointing to `memlet admin <name>`; in pipes and CI nothing extra is printed. Set `MEMLET_NO_DEPRECATION=1` to silence it.
- A command that returns `ok: false` now exits `1` (it used to exit `0` in some cases), and an unexpected error prints its JSON on stderr instead of stdout.
- `memlet --standalone` without `--task` now uses one task per repository. Memories stored earlier under the shared task are still there: read them with `--task standalone/main`.
- `rule-recall` takes the query first: `memlet admin rule-recall "<query>" [--repo DIR]`. The older `rule-recall <dir> "<query>"` form still works.
- Read commands no longer create pins. If `memlet handover` shows rules under "unpinned", run `memlet init` once.
- TS/JS pins made by an earlier release are reported as `compatible-unverified` (`legacy_ts_basis_repin_required`) until you run `memlet check --repin-legacy-ts`.
- User-facing messages are English by default.
- `memlet check` in an initialised repository now also reports pinned anchors (`pinnedAnchors` in the JSON) and exits `1` when one is stale. `--format sarif` prints the SARIF document itself; earlier releases printed a JSON envelope with the document under `sarif`.
- `rule-recall` no longer quotes a rule whose anchor is stale.
- Python pins are stored with a new hash basis that is identical on Python 3.9–3.13. Pins from earlier releases still match on the interpreter that made them and are migrated the next time `memlet init` or `memlet admin wiki` runs.
- A Dream queue you built yourself at `.memlet/dream/APPROVAL-QUEUE.json` (the path earlier documentation used) keeps working for `learn --pending` and `--approve`, but `memlet learn --transcript` refuses to mix into it: move it aside first. Its items start as pending.
- A `HANDOVER.md` written by an earlier release has no text checksum; `memlet handover --verify` says so instead of reporting a mismatch. Regenerate it with `memlet handover --write`.
- The first time the memory store is opened it is migrated in place (one column added); a copy named `<db>.bak-pre-migration-<UTC>` is written first. An older Memlet cannot write to the migrated store.
- Managed writes under `.memlet/` are refused when `.memlet` or anything below it is a symbolic link, and instruction files reached through a symbolic link or outside the repository are ignored.
- Instruction files larger than 1 MB are skipped.

---

## 🧪 Reproducible Refactoring & Handover Benchmark

`memlet benchmark --format markdown` builds four small repositories in a temporary directory, records rules before a refactor, applies the refactor, and measures what each approach would still feed the next agent. It makes no network or LLM calls and prints a `resultDigest` that is identical on every run (only `elapsedMs` varies).

Scenarios: (1) breaking signature change `def charge(user_id)` → `def charge(account_id, currency)`; (2) optional argument `def route(task)` → `def route(task, effort="high")`; (3) file move from a legacy auth module to an `auth/` package; (4) cross-vendor handover after a refactor, with two recorded dead-ends.

| Mode | Stale procedure injection | Optional-arg false positives | Handover dead-end prevention |
| :--- | :---: | :---: | :---: |
| Raw Markdown (static `CLAUDE.md` / `.cursorrules`) | 100% (3/3) | 0% (0/1) | 0% (0/3) |
| Unverified Wiki (timestamps, 90-day aging, file-exists check) | 66.7% (2/3) | 0% (0/1) | 66.7% (2/3) |
| Memlet (AST anchors + `reqSigHash` pins + handover quarantine) | **0% (0/3)** | **0% (0/1)** | **100% (3/3)** |

All three modes make 0 external LLM calls. Read the numbers for what they are: the dead-ends are recorded where only Memlet's mode reads them, so the 0% in the first row follows from the setup rather than from a measurement of another tool; these are four synthetic fixtures built to exercise specific failure modes, and "Unverified Wiki" is a simplified model of a timestamped Markdown wiki, not a run of any particular product. The unverified wiki's 0% false-positive rate comes from not checking signatures at all, which is also why it lets the breaking change through.

---

## 📊 Real-World Open-Source Repository Audit Verification

> **Category sample:** the public-repository numbers below come from 4 repositories pinned at fixed commits (`vercel/ai`, `openai-agents-python`, `AutoGPT`, `claude-code`; 784 docs, 2,745 references, 10 findings). They are not a guarantee for other repositories. "0 false positives" means the 10 findings in this sample were checked by hand. The numbers were recorded before this release's changes and were not re-run for it; the checker's ignore list also contains entries tuned on these four repositories.

`memlet check` was run against 4 full public AI repositories at the pinned commits below (see `real-oss-audit-results.json`) to verify both true-positive drift detection and monorepo/tutorial false-positive suppression:

| Repository (pinned commit) | Scanned Docs | References Checked | Errors Found | Verification Summary |
| :--- | :---: | :---: | :---: | :--- |
| `vercel/ai` (`e741656`) | 17 (`AGENTS.md`, `CONTRIBUTING.md`, `skills/**`) | 150 | **1 (`missing_path`)** | Detects 1 real stale path (`CONTRIBUTING.md:86` → `src/stream-text/openai.ts`, refactored to `src/stream-text/openai/basic.ts`) while suppressing negative examples (`Do not create...`) and resolving monorepo workspace scripts with **0 false positives in this sample**. |
| `openai/openai-agents-python` (`64cd5df`) | 414 (`AGENTS.md`, `docs/**`) | 1,161 | **0** | `Makefile` targets, `pyproject.toml`, and 414 multi-language docs resolve with **0 false positives in this sample**. |
| `Significant-Gravitas/AutoGPT` (`8d4a965`) | 351 (`CLAUDE.md`, `AGENTS.md`, `.claude/skills/**`, `docs/**`) | 1,432 | **9 (`missing_path`)** | Detects 9 real deleted/moved paths in the Significant-Gravitas/AutoGPT repo (e.g., `.github/copilot-instructions.md:173` → `frontend/lib/supabase/middleware.ts`, `docs/AGENTS.md:18` → `docs/integrations/basic.md`, `docs/content/forge/get-started.md:22` → `classic/forge/agent/forge_agent.py`, `.claude/skills/vercel-react-best-practices/SKILL.md:114` → `rules/_sections.md`) while suppressing external-repo and code-fence `title=` examples with **0 false positives in this sample**. |
| `anthropics/claude-code` (`d014fc0`) | 2 (`CLAUDE.md`, `README.md`) | 2 | **0** | Resolves `.github/workflows/claude.yml` and repository instructions with **0 false positives in this sample**. |

---

## 🔍 Scope & Static Analysis Boundaries

Memlet's checker is a **fast, deterministic, zero-network static analyzer** (`~10–60ms` on typical repos; `~0.4–5s` on 350+ doc monorepos). Knowing what it checks—and what requires a runtime test runner—keeps CI gates trustworthy. Passing static checks is not a live production guarantee:

| Capability | Static Method Used | Known Boundary |
| :--- | :--- | :--- |
| **Python Symbols** | Stdlib `ast.parse` (functions, classes, methods, args, defaults, decorators) | Dynamic `getattr` / runtime monkeypatching is not evaluated. |
| **TS/JS Symbols** | Comment-stripped lexical scanner with balanced-delimiter parameter and return-type parsing (`functions`, `arrow functions`, `React.memo`/`forwardRef`, `classes`, `interfaces`, `types`) | Not a parser: a hash match is reported as `compatible-unverified`, never `active`. JSX text with apostrophes and some regex literals are read as syntax errors; getters/setters and overload signatures are not extracted. Does not run `tsc`. |
| **SQL RLS Audit** | Cross-migration aggregation of `CREATE TABLE`, `ENABLE ROW LEVEL SECURITY`, `CREATE POLICY`, and multiline `USING (true)` across all `.sql` files | Evaluates static SQL migration files; does not connect to a live remote Supabase Postgres instance. |
| **WCAG Contrast & Slop** | Static CSS custom property resolver (`:root` / `[data-theme="dark"]`) + WCAG 2.1 relative luminance contrast ratio + HTML nesting depth parser | Evaluates static CSS/HTML/JSX rules; does not launch a headless Chromium layout engine. |

---

## 🤖 GitHub Action Integration

Add `.github/workflows/memlet-check.yml` to gate Pull Requests on instruction & memory drift, deploy blockers, or UI slop:

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
      - uses: ian-nocturne/deltaharness@v8.4.0
        with:
          mode: "audit" # 'audit', 'rescue', 'anti-slop', or 'all'
          repo-path: "."
          base-ref: ${{ github.event.pull_request.base.sha }}
          fail-on-drift: "true"
```

Outputs (`status`, `finding-count`, `total-finding-count`, `slop-score`) and the default SARIF file name (`deltaharness-audit.sarif`) are unchanged. What changed in this release:

- `format` (`markdown`, `json`, `sarif`) now selects the report that is produced; it was previously ignored.
- New input `python-version`. The Action no longer installs Python 3.11 by itself: it uses the runner's Python (3.9 or newer) unless you set this input (`python-version: "3.11"` restores the old behaviour).
- `fail-on-drift` accepts only `true` or `false`; any other value is a configuration error.
- `rescue` with only unverifiable environment variables, and `anti-slop` on a repository with no UI files, are warnings: the job passes and `status` is `UNVERIFIED`. Real blockers still fail the job.
- A result the Action cannot interpret fails closed with exit code `2`.
- The SARIF file is never written through a symbolic link.

---

## 🧠 Bi-Temporal Memory & Self-Healing Skill Engine

Beyond static checking, Memlet includes a complete local-first memory & procedural skill runtime (`StandaloneStore` + `MemoryEngine`):

- **Sub-15ms Hybrid Retrieval**: In-memory BM25 inverted postings + optional binary float32 vector matrix + 2-Hop Personalized PageRank associative recall.
- **Session Delta-Prompting**: Compresses multi-turn agent context by transmitting only state deltas when `coreHash` is unchanged.
- **Execution-Verified Procedural Skills**: Synthesizes reusable `SKILL.md` domain harnesses from solved debugging trajectories and automatically demotes stale skills when referenced code anchors drift.

```bash
# Run the 4-Epoch × 4-Baseline Repo-Evolution Benchmark
memlet admin repo-benchmark

# Check environment & SQLite WAL health
memlet admin doctor
```

---

## 📄 License & Provenance

- Licensed under the **Apache License, Version 2.0** ([LICENSE](LICENSE)).
- Third-party notices and optional model licenses are documented in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
