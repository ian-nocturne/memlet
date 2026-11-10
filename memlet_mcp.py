#!/usr/bin/env python3
"""Standalone MCP Server (JSON-RPC 2.0 over stdio) for Memlet v9.0.0.

Can be plugged into Claude Code, Codex, Cursor, Windsurf, or any MCP-compatible IDE/agent in 1 minute
without requiring an external web server.
"""
import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path

from memlet.standalone import StandaloneStore as WorkStore
import work_memory
import memory_intelligence as memory
from memory_cli import read_item, schema as cli_schema
from memlet.standalone import default_standalone_task_id, legacy_standalone_notice

SERVER_NAME = 'memlet'
from memlet import VERSION as SERVER_VERSION
PROTOCOL_VERSION = '2024-11-05'
MAX_MCP_TRANSCRIPT_BYTES = 8 * 1024 * 1024  # Reject before parsing or creating any state.


def mcp_tools():
    return [
        {
            'name': 'memlet_store_brief',
            'description': 'Get current task memory brief, revision number, core invariants (goal/decisions/constraints), active skills, pitfall cases, and recent source originals.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'task': {'type': 'string', 'description': 'Optional task ID override'},
                },
            },
        },
        {
            'name': 'memlet_store_recall',
            'description': 'Hybrid recall (Inverted Index + BM25 + Multi-Intent Facet Decomposition + Prospective Memory Triggers + local Int8 E5 + Entity + 2-hop PPR [[links]] + Coverage Reranker + Lateral Diversity Filter + Grounded Abstention Gate) with 2-stage Progressive Disclosure & Sufficiency Check.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': 'Search query'},
                    'budget': {'type': 'integer', 'default': 6000},
                    'shared': {'type': 'boolean', 'default': False},
                    'federated': {'type': 'boolean', 'default': False, 'description': 'Include sanitized cross-project federated archetypes'},
                    'at': {'type': 'string', 'description': 'Optional ISO-8601 timestamp for bi-temporal valid time'},
                    'knownAt': {'type': 'string', 'description': 'Optional ISO-8601 timestamp for bi-temporal transaction time'},
                    'task': {'type': 'string'},
                },
                'required': ['query'],
            },
        },
        {
            'name': 'memlet_store_read',
            'description': 'Read full content, [[links]], and version history of a specific memory item or Stage-2 index item.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string', 'description': 'Memory item ID (e.g., fact:..., case:..., report:...)'},
                    'offset': {'type': 'integer', 'default': 0},
                    'size': {'type': 'integer', 'default': 6000},
                    'task': {'type': 'string'},
                },
                'required': ['id'],
            },
        },
        {
            'name': 'memlet_store_capture',
            'description': '2-Tier Write Gear (Auto-Draft Mode + Tri-Factor Admission Control Gate + Contrastive Failure-to-Case Synthesizer): frictionless capture of raw observations, terminal errors, or multi-step troubleshooting without manual sourceRefs or revision.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'text': {'type': 'string', 'description': 'Raw observation, error log, or troubleshooting steps'},
                    'kind': {'type': 'string', 'enum': ['auto', 'hypothesis', 'case', 'experience'], 'default': 'auto'},
                    'command': {'type': 'string', 'description': 'Optional command or module context'},
                    'failedCommand': {'type': 'string', 'description': 'Optional failed command/symptom for contrastive synthesis'},
                    'fixedCommand': {'type': 'string', 'description': 'Optional fixed command/resolution for contrastive synthesis'},
                    'operationId': {'type': 'string'},
                    'entities': {'type': 'array', 'items': {'type': 'string'}},
                    'links': {'type': 'array', 'items': {'type': 'string'}},
                    'force': {'type': 'boolean', 'default': False},
                    'task': {'type': 'string'},
                },
                'required': ['text'],
            },
        },
        {
            'name': 'memlet_store_learn',
            'description': '2-Tier Write Gear (Strict Mode): source-hash verified learning of core constraints, facts, experiences (auto-synthesizing SKILL.md), and pitfall cases, with automatic contradiction/supersession resolution, Prospective Memory cue extraction, and Bidirectional Link Graph Evolution.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'revision': {'type': 'integer', 'description': 'Current task revision from memlet_store_brief'},
                    'operationId': {'type': 'string', 'description': 'Idempotency operation key'},
                    'entries': {'type': 'array', 'items': {'type': 'object'}},
                    'task': {'type': 'string'},
                },
                'required': ['revision', 'operationId', 'entries'],
            },
        },
        {
            'name': 'memlet_store_skills',
            'description': 'List or search domain procedural skills (SKILL.md) with AST signature-aware self-healing & file-hash staleness detection and optional federated cross-project archetype transfer.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'default': ''},
                    'skillId': {'type': 'string', 'description': 'Optional specific skillId to inspect in full'},
                    'includeInactive': {'type': 'boolean', 'default': False},
                    'shared': {'type': 'boolean', 'default': False},
                    'federated': {'type': 'boolean', 'default': False},
                    'task': {'type': 'string'},
                },
            },
        },
        {
            'name': 'memlet_store_skill_manage',
            'description': 'Create, improve, consolidate, promote, degrade, or retract a procedural skill (SKILL.md).',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'revision': {'type': 'integer'},
                    'operationId': {'type': 'string'},
                    'spec': {'type': 'object'},
                    'task': {'type': 'string'},
                },
                'required': ['revision', 'operationId', 'spec'],
            },
        },
        {
            'name': 'memlet_store_feedback',
            'description': 'Record grounded feedback (used/helpful/incorrect/irrelevant/missing) on a recalled item or skill. Triggers 2-stage skill promotion or auto-synthesizes a pitfall case on incorrect feedback.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'operationId': {'type': 'string'},
                    'recallId': {'type': 'string'},
                    'itemId': {'type': 'string'},
                    'outcome': {'type': 'string', 'enum': ['used', 'helpful', 'incorrect', 'irrelevant', 'missing']},
                    'note': {'type': 'string', 'default': ''},
                    'patch': {'type': 'object'},
                    'task': {'type': 'string'},
                },
                'required': ['operationId', 'recallId', 'itemId', 'outcome'],
            },
        },
        {
            'name': 'memlet_store_preflight',
            'description': 'Active Pre-Flight Guardrail & Constraint Verifier: deterministically verify a proposed action/plan and target files against active constraints, pitfall cases, hierarchical causal dependencies, and Prospective Memory triggers before execution.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'plan': {'type': 'string', 'description': 'Proposed action or edit plan'},
                    'files': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Target files to touch'},
                    'actionType': {'type': 'string', 'default': 'general'},
                    'task': {'type': 'string'},
                },
                'required': ['plan'],
            },
        },
        {
            'name': 'memlet_store_capsule',
            'description': 'Subagent Scoped Memory Capsule: generate an ultra-compact (<1,200 chars default) read-only memory capsule (Pre-Flight status + Pitfall cases + Prospective triggers + Scoped skills) tailored to a delegated worker plan and target files.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'plan': {'type': 'string', 'description': 'Delegated subagent task or plan'},
                    'files': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Target files for the subagent'},
                    'worker': {'type': 'string', 'default': 'subagent'},
                    'budget': {'type': 'integer', 'default': 1200},
                    'task': {'type': 'string'},
                },
                'required': ['plan'],
            },
        },
        {
            'name': 'memlet_store_inspector',
            'description': 'Visual Inspector & Quantitative Benchmark: returns memory/skill/case [[links]] graph nodes & edges, thematic episode clusters, domain hierarchy tree, Mermaid diagram, prompt budget gauge, staleness alerts, and ROI scorecard.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'budget': {'type': 'integer', 'default': 6000},
                    'task': {'type': 'string'},
                },
            },
        },
        {
            'name': 'memlet_store_scenes',
            'description': 'Get thematic episode clusters and 4-level domain hierarchy (L0 Task -> L1 Pillar -> L2 Scene -> L3 Node) with directed causal triples.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'task': {'type': 'string'},
                },
            },
        },
        {
            'name': 'memlet_store_consolidate',
            'description': 'Lossless Sleep Consolidation: merge fragmented hypothesis/draft clusters into consolidated semantic items while strictly protecting core invariants and user-confirmed rules (Identity Drift = 0.0).',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'similarityThreshold': {'type': 'number', 'default': 0.35},
                    'dryRun': {'type': 'boolean', 'default': False},
                    'task': {'type': 'string'},
                },
            },
        },
        {
            'name': 'memlet_store_sync_markdown',
            'description': 'Bi-directional Markdown Co-Editing Sync: ingest direct human edits from SKILL.md files on disk (promoting to user-confirmed) and optional Markdown notes.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'notePath': {'type': 'string'},
                    'task': {'type': 'string'},
                },
            },
        },
        {
            'name': 'memlet_store_export_rules',
            'description': 'Export or synchronize verified constraints, pitfall cases, and domain SKILL.md index into CLAUDE.md, AGENTS.md, .cursorrules, .windsurfrules, and MCP config.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'targets': {
                        'type': 'array',
                        'items': {'type': 'string', 'enum': ['claude', 'agents', 'cursor', 'windsurf', 'mcp']},
                    },
                    'targetDir': {'type': 'string'},
                    'writeFiles': {'type': 'boolean', 'default': False},
                    'lang': {'type': 'string', 'enum': ['ko', 'en'], 'default': 'ko'},
                    'task': {'type': 'string'},
                },
            },
        },
        {
            'name': 'memlet_store_heal_skills',
            'description': 'Strict AST Call-Contract Self-Healing for SKILL.md: inspect stale skills, re-anchor backward-compatible internal edits with verificationStatus=compatible-unverified, and block breaking call-contract changes.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'dryRun': {'type': 'boolean', 'default': False},
                    'task': {'type': 'string'},
                },
            },
        },
        {
            'name': 'memlet_store_verify_skill',
            'description': 'Execution-Backed Skill Verification: run a verification test/command or record a structured executionProof (exitCode, command, output) to promote or re-verify a skill and bind verificationEvidence.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'skillId': {'type': 'string', 'description': 'Skill ID or slug to verify'},
                    'command': {'type': 'string', 'description': 'Optional shell command to run for verification'},
                    'executionProof': {'type': 'object', 'description': 'Optional structured execution proof with exitCode, command, output'},
                    'operationId': {'type': 'string'},
                    'timeout': {'type': 'number', 'default': 15.0},
                    'task': {'type': 'string'},
                },
                'required': ['skillId'],
            },
        },
        {
            'name': 'memlet_store_doctor',
            'description': 'Cross-Platform Environment, SQLite WAL, & Embedding Model Health Doctor: inspect data root writability, SQLite WAL state, and E5 ONNX model availability.',
            'inputSchema': {
                'type': 'object',
                'properties': {},
            },
        },
        {
            'name': 'memlet_store_audit',
            'description': 'Zero-Setup Repository Procedure & Instruction Drift Auditor (memlet audit): scan CLAUDE.md, AGENTS.md, .cursorrules, docs, and SKILL.md for missing paths, missing scripts, missing symbols, and breaking call-signature drift.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'repoPath': {'type': 'string', 'description': 'Repository path to audit (defaults to store root)'},
                    'baseRef': {'type': 'string', 'description': 'Optional base git ref for diff blast radius analysis'},
                    'headRef': {'type': 'string', 'default': 'HEAD'},
                    'format': {'type': 'string', 'enum': ['json', 'markdown', 'sarif'], 'default': 'json'},
                },
            },
        },
    ] + memlet_tools()


def memlet_tools():
    """Bounded repository tools.

    Each tool carries MCP tool annotations. Hosts that gate tool calls on them (Codex blocks or prompts for every
    tool without `readOnlyHint` in its default approval mode) can then run the three read-only tools without
    asking, while `memlet_learn`, which writes the pending queue, still goes through the host's approval.
    Hosts that predate annotations ignore the extra key.
    """
    def string(limit=4096):
        return {'type': 'string', 'minLength': 1, 'maxLength': limit}

    def strings(limit, count, enum=None):
        item = string(limit)
        if enum:
            item['enum'] = enum
        return {'type': 'array', 'maxItems': count, 'uniqueItems': True, 'items': item}

    budget = {'type': 'integer', 'minimum': 1, 'maximum': 16000, 'default': 1200}
    specs = [
        ('memlet_check', 'Read-only: check changed anchors (default) or run the full repository audit.',
         {'changed': {'type': 'boolean', 'default': True}, 'base': string(256),
          'files': strings(4096, 256)}, []),
        ('memlet_recall', 'Read-only: recall project rules, approved verified lessons, and this server task memory.',
         {'query': string(8192), 'budget': budget,
          'include': strings(16, 3, ['rules', 'dream', 'memory'])}, ['query']),
        ('memlet_handover', 'Read-only: build a handover capsule; never writes HANDOVER.md.',
         {'next_step': string(8192), 'rejected': strings(4096, 64),
          'budget': {'type': 'integer', 'minimum': 500, 'maximum': 2000,
                     'description': 'Optional token budget; defaults to repository config or 500, as in the CLI.'},
          'mode': {'type': 'string', 'enum': ['verified', 'index'],
                   'description': 'Defaults to repository handover.mode, else verified; experimental defaults are refused.',
                   'minLength': 1, 'maxLength': 16}}, []),
        ('memlet_learn', 'Non-destructive, not read-only: requires an initialized repository (memlet init). Extract pending lessons from a regular '
         '.log/.jsonl file of at most 8 MiB, with no symlink components, inside the repository or known agent transcript roots '
         '(~/.codex/sessions, ~/.claude/projects, ~/.muse/sessions; '
         'MEMLET_TRANSCRIPT_DIRS overrides agent roots), or list pending lessons. '
         'Approval is CLI-only: memlet learn --approve ID --by NAME. No approve or reject action or approval arguments are allowed.',
         {'action': {'type': 'string', 'enum': ['transcript', 'pending'], 'minLength': 1, 'maxLength': 16},
          'transcript_path': string()}, ['action']),
    ]
    tools = [{'name': name, 'description': description,
             'inputSchema': {'type': 'object', 'properties': dict(repo_path=string(), **props),
                             'required': required, 'additionalProperties': False}}
            for name, description, props, required in specs]
    read_only = {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}
    for tool in tools[:-1]:
        tool['annotations'] = dict(read_only)
    tools[-1]['annotations'] = {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}
    tools[-1]['inputSchema']['allOf'] = [
        {'if': {'properties': {'action': {'const': 'transcript'}}, 'required': ['action']},
         'then': {'required': ['transcript_path']}}]
    return tools + _dream_tools()


def _dream_tools():
    from memlet.memlet_dreamcycle import CANDIDATE_SCHEMA
    string = lambda limit: {"type": "string", "minLength": 1, "maxLength": limit}
    common = {"repo_path": string(4096), "run_id": string(24)}
    return [
        {"name": "memlet_dream_next", "description": "Read-only: next un-dreamed snapshot batch and candidate schema; initialized repository required.",
         "inputSchema": {"type": "object", "properties": dict(common, batch_tokens={"type": "integer", "minimum": 128, "maximum": 16000}), "required": [], "additionalProperties": False},
         "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        {"name": "memlet_dream_submit", "description": "Non-destructive: submit cited pending candidates as untrusted proposer host; cannot verify, approve, reject or set trust tiers.",
         "inputSchema": {"type": "object", "properties": dict(common, batch_id=string(24), candidates=CANDIDATE_SCHEMA),
                         "required": ["run_id", "batch_id", "candidates"], "additionalProperties": False},
         "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}}]


def _allowed_transcript_roots():
    """Same trusted host-configured roots as the CLI; tool arguments cannot expand them."""
    from memlet import memlet_bridge
    return memlet_bridge.agent_transcript_roots()


def _within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_memlet_arguments(name, args):
    schema = next(t['inputSchema'] for t in memlet_tools() if t['name'] == name)
    if not isinstance(args, dict):
        raise ValueError('arguments must be an object')
    unknown = set(args) - set(schema['properties'])
    if name == 'memlet_learn' and (str(args.get('action', '')).lower() in ('approve', 'reject') or unknown & {'approve', 'reject', 'by', 'approver', 'ids', 'reason'}):
        raise ValueError('Approval is CLI-only (memlet learn --approve ID --by NAME); MCP allows only transcript and pending, with no extra approval/rejection arguments')
    if unknown:
        raise ValueError('Unknown arguments: ' + ', '.join(sorted(unknown)))
    for key in schema['required']:
        if key not in args:
            raise ValueError('Missing required argument: ' + key)

    def validate(value, spec, key):
        kind = spec['type']
        valid = ((kind == 'string' and isinstance(value, str)) or
                 (kind == 'boolean' and type(value) is bool) or
                 (kind == 'integer' and type(value) is int) or
                 (kind == 'array' and isinstance(value, list)) or
                 (kind == 'object' and isinstance(value, dict)))
        if not valid:
            raise ValueError(key + ' must be ' + kind)
        if 'enum' in spec and value not in spec['enum']:
            raise ValueError(key + ' must be one of ' + ', '.join(spec['enum']))
        if kind == 'string' and not spec.get('minLength', 0) <= len(value) <= spec.get('maxLength', 8192):
            raise ValueError(key + ' length is out of range')
        if kind == 'integer' and not spec['minimum'] <= value <= spec['maximum']:
            raise ValueError('{} is out of range ({}..{})'.format(key, spec['minimum'], spec['maximum']))
        if kind == 'array':
            if len(value) > spec['maxItems']:
                raise ValueError(key + ' has too many items')
            for item in value:
                validate(item, spec['items'], key + ' item')
            if spec.get('uniqueItems') and len(set(value)) != len(value):
                raise ValueError(key + ' must contain unique items')
        if kind == 'object':
            if set(value) - set(spec['properties']) or not set(spec.get('required', [])) <= set(value):
                raise ValueError(key + ' has missing or unknown properties')
            for field, item in value.items():
                validate(item, spec['properties'][field], key + '.' + field)
    for key, value in args.items():
        validate(value, schema['properties'][key], key)
    if name == 'memlet_learn' and args['action'] == 'transcript' and 'transcript_path' not in args:
        raise ValueError('Missing required argument: transcript_path')


class MemletMCPServer:
    def __init__(self, root=None, default_task=None, default_session='standalone-owner', standalone=True, store=None):
        if root is None:
            try:
                from memlet.paths import resolve_default_data_root
                self.root = resolve_default_data_root(package_dir=Path(__file__).resolve().parent)
            except Exception:
                self.root = Path(__file__).resolve().parent
        else:
            self.root = Path(root).expanduser().resolve()
        from memlet.compatibility import existing_store_notice
        self._store_notice = existing_store_notice(self.root) if store is None else None
        self.store = store if store is not None else WorkStore(self.root)
        self._uses_derived_task = not default_task
        self.default_task = default_task or default_standalone_task_id()
        self.default_session = default_session
        self.standalone = standalone
        if self.standalone:
            t = self.store.ensure_standalone_task(self.default_task, session=self.default_session)
            if not self.default_session:
                self.default_session = t['owner']

    def _resolve_task_and_session(self, args):
        task_id = (args or {}).get('task') or self.default_task
        if self.standalone:
            task = self.store.ensure_standalone_task(task_id, session=self.default_session or 'standalone-owner')
        else:
            task = self.store.get(task_id)
        session = (args or {}).get('session') or self.default_session or task['owner']
        return task, session

    def call_tool(self, name, arguments):
        if name in {tool['name'] for tool in memlet_tools()}:
            return self._with_notice(self._call_memlet_tool(name, arguments if arguments is not None else {}))
        result = self._call_tool(name, arguments)
        tid = (arguments or {}).get('task') or self.default_task
        if self._uses_derived_task and tid == self.default_task and isinstance(result, dict):
            notice = legacy_standalone_notice(self.store, tid)
            if notice:
                result['notice'] = notice
        return self._with_notice(result)

    def _with_notice(self, result):
        if self._store_notice and isinstance(result, dict):
            result["notice"] = self._store_notice
            self._store_notice = None
        return result

    def _call_memlet_tool(self, name, args):
        from memlet import memlet_bridge as bridge
        from memlet.memlet_wiki import build_handover, resolve_handover_mode
        _validate_memlet_arguments(name, args)
        repo = Path(args.get('repo_path', Path.cwd())).expanduser().resolve()
        if not repo.is_dir():
            raise ValueError('repository path not found: ' + str(repo))
        if (repo / '.memlet').is_symlink():
            raise ValueError('refusing symlinked path .memlet')
        if name == 'memlet_check':
            if args.get('changed', True):
                return bridge.changed_check(repo, base=args.get('base'), files=args.get('files'))
            return memory.audit_repo(repo, base_ref=args.get('base'), head_ref='HEAD', output_format='json')
        if name == 'memlet_recall':
            return bridge.unified_recall(repo, args['query'], budget=args.get('budget', 1200),
                                         include=args.get('include', ('rules', 'dream', 'memory')),
                                         store=self.store, task=self.default_task)
        if name == 'memlet_handover':
            mode = resolve_handover_mode(repo, args.get('mode'))
            if mode not in ('verified', 'index'):
                raise ValueError('Configured handover mode ' + mode + ' is experimental; explicitly choose verified or index')
            return build_handover(repo, next_step=args.get('next_step'), rejected=args.get('rejected'),
                                  budget=args.get('budget'), mode=mode,
                                  output_format='json', write=False)
        if not (repo / '.memlet').is_dir():
            raise ValueError('Repository is not initialized; run memlet init before memlet_learn')
        if name in ('memlet_dream_next', 'memlet_dream_submit'):
            from memlet.memlet_dreamcycle import dream_next, dream_submit
            if name == 'memlet_dream_next':
                return dream_next(repo, run_id=args.get('run_id'), batch_tokens=args.get('batch_tokens', 6000))
            return dream_submit(repo, args['run_id'], args['batch_id'], args['candidates'], proposer='host')
        if args['action'] == 'pending':
            return bridge.learn_pending(repo)
        source = Path(args['transcript_path']).expanduser().absolute()
        problem = bridge.transcript_symlink_problem(source)
        if problem:
            raise ValueError('refusing symlinked transcript path: ' + problem)
        source = source.resolve()
        roots = (repo,) + tuple(p.resolve() for p in _allowed_transcript_roots())
        if not any(_within(source, root) for root in roots):
            raise ValueError('transcript_path must be inside the repository or a known agent transcript root')
        info = source.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('transcript_path must be an existing regular file')
        if info.st_size > MAX_MCP_TRANSCRIPT_BYTES:
            raise ValueError('transcript_path exceeds the 8 MiB MCP transcript limit')
        if source.suffix not in ('.jsonl', '.log'):
            raise ValueError('transcript_path must be a .jsonl or .log file')
        return bridge.learn_from_transcript(repo, source, vendor='autonomous-deterministic')

    def _call_tool(self, name, arguments):
        args = arguments or {}
        if name == 'memlet_store_doctor':
            return memory.doctor(self.store)
        if name == 'memlet_store_audit':
            return memory.audit_repo(
                repo_path=args.get('repoPath') or self.root,
                base_ref=args.get('baseRef'),
                head_ref=args.get('headRef', 'HEAD'),
                output_format=args.get('format', 'json'),
            )
        task, session = self._resolve_task_and_session(args)
        tid = task['id']

        if name == 'memlet_store_brief':
            res = self.store.memory(tid, include_retrieval=False)
            with self.store.db() as db:
                originals = [
                    work_memory._public(r)
                    for r in db.execute(
                        "SELECT * FROM memory_items WHERE task=? AND kind IN ('instruction','report') ORDER BY event_id DESC LIMIT 10",
                        (tid,),
                    )
                ]
            res['originals'] = [
                {'id': r['item_id'], 'kind': r['kind'], 'excerpt': r['content'][:2000], 'totalChars': len(r['content']), 'source': r['source']}
                for r in originals
            ]
            res.update(owner=task['owner'], project=task['project'], schema=cli_schema())
            return res

        if name == 'memlet_store_recall':
            res = self.store.memory(
                tid,
                args.get('query', ''),
                int(args.get('budget', 6000)),
                at=args.get('at'),
                known_at=args.get('knownAt'),
                shared=bool(args.get('shared', False)),
                federated=bool(args.get('federated', False)),
            )
            res['recallId'] = memory.record_recall(self.store, tid, session, 'mcp:' + str(time.time_ns()), args.get('query', ''), res['retrieval'])
            return res

        if name == 'memlet_store_read':
            return read_item(self.store, tid, args['id'], int(args.get('offset', 0)), int(args.get('size', 6000)))

        if name == 'memlet_store_capture':
            return memory.auto_capture(self.store, tid, session, args)

        if name == 'memlet_store_learn':
            return memory.learn(self.store, tid, session, int(args['revision']), args['operationId'], args['entries'])

        if name == 'memlet_store_skills':
            if args.get('skillId'):
                return memory.read_skill(self.store, tid, args['skillId'])
            return self.store.skills(
                tid,
                args.get('query', ''),
                include_inactive=bool(args.get('includeInactive', False)),
                shared=bool(args.get('shared', False)),
                federated=bool(args.get('federated', False)),
            )

        if name == 'memlet_store_skill_manage':
            return memory.manage_skill(self.store, tid, session, int(args['revision']), args['operationId'], args['spec'])

        if name == 'memlet_store_feedback':
            return memory.feedback(
                self.store,
                tid,
                session,
                args['operationId'],
                args['recallId'],
                args.get('itemId', ''),
                args['outcome'],
                args.get('note', ''),
                patch=args.get('patch'),
            )

        if name == 'memlet_store_preflight':
            return memory.preflight_check(
                self.store,
                tid,
                plan=args.get('plan', ''),
                files=args.get('files'),
                action_type=args.get('actionType'),
            )

        if name == 'memlet_store_capsule':
            return memory.build_subagent_capsule(
                self.store,
                tid,
                plan=args.get('plan', ''),
                files=args.get('files'),
                worker=args.get('worker', 'subagent'),
                budget=int(args.get('budget', 1200)),
            )

        if name == 'memlet_store_inspector':
            return memory.inspector(self.store, tid, budget=int(args.get('budget', 6000)))

        if name == 'memlet_store_scenes':
            return memory.scenes(self.store, tid)

        if name == 'memlet_store_consolidate':
            return memory.consolidate_memories(
                self.store,
                tid,
                session=session,
                similarity_threshold=float(args.get('similarityThreshold', 0.35)),
                dry_run=bool(args.get('dryRun', False)),
            )

        if name == 'memlet_store_sync_markdown':
            return memory.sync_markdown(
                self.store,
                tid,
                session=session,
                note_path=args.get('notePath'),
                explicit_user=args.get('explicitUser'),
            )

        if name == 'memlet_store_export_rules':
            return memory.export_rules(
                self.store,
                tid,
                targets=args.get('targets'),
                target_dir=args.get('targetDir'),
                write_files=bool(args.get('writeFiles', False)),
                lang=args.get('lang', 'ko'),
            )

        if name == 'memlet_store_heal_skills':
            return memory.heal_skills(
                self.store,
                tid,
                session=session,
                dry_run=bool(args.get('dryRun', False)),
            )

        if name == 'memlet_store_verify_skill':
            return memory.verify_skill(
                self.store,
                tid,
                session,
                args['skillId'],
                command=args.get('command'),
                execution_proof=args.get('executionProof'),
                operation_id=args.get('operationId'),
                timeout=float(args.get('timeout', 15.0)),
            )

        raise ValueError(f'Unknown MCP tool: {name}')

    def list_resources(self):
        tid = self.default_task
        return [
            {
                'uri': f'memlet://{tid}/inspector',
                'name': f'Memlet Inspector & Graph ({tid})',
                'mimeType': 'application/json',
            },
            {
                'uri': f'memlet://{tid}/benchmark',
                'name': f'Memlet Quantitative Benchmark ({tid})',
                'mimeType': 'application/json',
            },
            {
                'uri': f'memlet://{tid}/harness',
                'name': f'Memlet Domain Harness Markdown ({tid})',
                'mimeType': 'text/markdown',
            },
        ]

    def read_resource(self, uri):
        prefix = 'memlet://'
        if not str(uri).startswith(prefix):
            raise ValueError('Unsupported resource URI')
        rest = str(uri)[len(prefix):]
        if '/' not in rest:
            raise ValueError('Invalid resource URI path')
        tid, kind = rest.rsplit('/', 1)
        task, _ = self._resolve_task_and_session({'task': tid})
        if kind == 'inspector':
            data = memory.inspector(self.store, task['id'])
            return [{'uri': uri, 'mimeType': 'application/json', 'text': json.dumps(data, ensure_ascii=False, indent=2)}]
        if kind == 'benchmark':
            data = memory.benchmark(self.store, task['id'])
            return [{'uri': uri, 'mimeType': 'application/json', 'text': json.dumps(data, ensure_ascii=False, indent=2)}]
        if kind == 'harness':
            data = memory.export_rules(self.store, task['id'], targets=['agents'], write_files=False)
            return [{'uri': uri, 'mimeType': 'text/markdown', 'text': data['exports']['agents']['content']}]
        raise ValueError(f'Unknown resource kind: {kind}')

    def handle_rpc(self, request):
        if not isinstance(request, dict):
            return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
        method = request.get('method', '')
        req_id = request.get('id')
        params = request.get('params') or {}

        if method.startswith('notifications/'):
            return None

        try:
            if method == 'initialize':
                result = {
                    'protocolVersion': PROTOCOL_VERSION,
                    'capabilities': {'tools': {}, 'resources': {}},
                    'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
                }
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': mcp_tools()}
            elif method == 'tools/call':
                tool_name = params.get('name', '')
                tool_args = params.get('arguments') or {}
                payload = self.call_tool(tool_name, tool_args)
                result = {
                    'content': [{'type': 'text', 'text': (payload.get('formatted') if tool_name in {tool['name'] for tool in memlet_tools()} and isinstance(payload.get('formatted'), str) else json.dumps(payload, ensure_ascii=False))}],
                    'structuredContent': payload,
                    'isError': payload.get('ok') is False if tool_name in {tool['name'] for tool in memlet_tools()} else False,
                }
            elif method == 'resources/list':
                result = {'resources': self.list_resources()}
            elif method == 'resources/read':
                result = {'contents': self.read_resource(params.get('uri', ''))}
            else:
                return {
                    'jsonrpc': '2.0',
                    'id': req_id,
                    'error': {'code': -32601, 'message': f'Method not found: {method}'},
                }
            return {'jsonrpc': '2.0', 'id': req_id, 'result': self._with_notice(result)}
        except Exception as exc:
            if method == 'tools/call':
                err_payload = {'ok': False, 'error': str(exc), 'type': type(exc).__name__}
                return {
                    'jsonrpc': '2.0',
                    'id': req_id,
                    'result': {
                        'content': [{'type': 'text', 'text': json.dumps(err_payload, ensure_ascii=False)}],
                        'structuredContent': err_payload,
                        'isError': True,
                    },
                }
            return {
                'jsonrpc': '2.0',
                'id': req_id,
                'error': {'code': -32000, 'message': str(exc), 'data': {'type': type(exc).__name__}},
            }

    def serve_stdio(self):
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                resp = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + '\n')
                sys.stdout.flush()
                continue
            resp = self.handle_rpc(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + '\n')
                sys.stdout.flush()


def _default_mcp_root():
    pkg_dir = Path(__file__).resolve().parent
    try:
        from memlet.paths import resolve_default_data_root
        return str(resolve_default_data_root(package_dir=pkg_dir))
    except Exception:
        return str(pkg_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=_default_mcp_root())
    parser.add_argument('--task', default=None)
    parser.add_argument('--session', default='standalone-owner')
    parser.add_argument('--strict-task', action='store_true', help='Require existing task instead of auto-bootstrapping')
    parser.add_argument('--print-mcp-config', action='store_true', help='Print ready-to-paste MCP server JSON config and exit')
    parser.add_argument('--self-test', action='store_true', help='Run built-in protocol smoke test and exit')
    args = parser.parse_args()

    server = MemletMCPServer(
        args.root,
        default_task=args.task,
        default_session=args.session,
        standalone=not args.strict_task,
    )
    if args.print_mcp_config:
        exported = memory.export_rules(server.store, server.default_task, targets=['mcp'], write_files=False)
        print(json.dumps(exported['mcpConfig'], ensure_ascii=False, indent=2))
        return
    if args.self_test:
        init_res = server.handle_rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
        tools_res = server.handle_rpc({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
        insp_res = server.handle_rpc({
            'jsonrpc': '2.0',
            'id': 3,
            'method': 'tools/call',
            'params': {'name': 'memlet_store_inspector', 'arguments': {'task': args.task}},
        })
        print(json.dumps({
            'ok': True,
            'serverInfo': init_res['result']['serverInfo'],
            'toolCount': len(tools_res['result']['tools']),
            'inspectorSummary': insp_res['result']['structuredContent']['summary'],
        }, ensure_ascii=False))
        return
    server.serve_stdio()


if __name__ == '__main__':
    main()
