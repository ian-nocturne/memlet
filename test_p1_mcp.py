"""C2 MCP contracts; baseline captured at a679b65 (before adding memlet tools)."""
import json

BASE_TOOLS = json.loads(r'''
[
  {
    "name": "prism_memory_brief",
    "description": "Get current task memory brief, revision number, core invariants (goal/decisions/constraints), active skills, pitfall cases, and recent source originals.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "task": {
          "type": "string",
          "description": "Optional task ID override"
        }
      }
    }
  },
  {
    "name": "prism_memory_recall",
    "description": "Hybrid recall (Inverted Index + BM25 + Multi-Intent Facet Decomposition + Prospective Memory Triggers + local Int8 E5 + Entity + 2-hop PPR [[links]] + Coverage Reranker + Lateral Diversity Filter + Grounded Abstention Gate) with 2-stage Progressive Disclosure & Sufficiency Check.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "Search query"
        },
        "budget": {
          "type": "integer",
          "default": 6000
        },
        "shared": {
          "type": "boolean",
          "default": false
        },
        "federated": {
          "type": "boolean",
          "default": false,
          "description": "Include sanitized cross-project federated archetypes"
        },
        "at": {
          "type": "string",
          "description": "Optional ISO-8601 timestamp for bi-temporal valid time"
        },
        "knownAt": {
          "type": "string",
          "description": "Optional ISO-8601 timestamp for bi-temporal transaction time"
        },
        "task": {
          "type": "string"
        }
      },
      "required": [
        "query"
      ]
    }
  },
  {
    "name": "prism_memory_read",
    "description": "Read full content, [[links]], and version history of a specific memory item or Stage-2 index item.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "id": {
          "type": "string",
          "description": "Memory item ID (e.g., fact:..., case:..., report:...)"
        },
        "offset": {
          "type": "integer",
          "default": 0
        },
        "size": {
          "type": "integer",
          "default": 6000
        },
        "task": {
          "type": "string"
        }
      },
      "required": [
        "id"
      ]
    }
  },
  {
    "name": "prism_memory_capture",
    "description": "2-Tier Write Gear (Auto-Draft Mode + Tri-Factor Admission Control Gate + Contrastive Failure-to-Case Synthesizer): frictionless capture of raw observations, terminal errors, or multi-step troubleshooting without manual sourceRefs or revision.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "text": {
          "type": "string",
          "description": "Raw observation, error log, or troubleshooting steps"
        },
        "kind": {
          "type": "string",
          "enum": [
            "auto",
            "hypothesis",
            "case",
            "experience"
          ],
          "default": "auto"
        },
        "command": {
          "type": "string",
          "description": "Optional command or module context"
        },
        "failedCommand": {
          "type": "string",
          "description": "Optional failed command/symptom for contrastive synthesis"
        },
        "fixedCommand": {
          "type": "string",
          "description": "Optional fixed command/resolution for contrastive synthesis"
        },
        "operationId": {
          "type": "string"
        },
        "entities": {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        "links": {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        "force": {
          "type": "boolean",
          "default": false
        },
        "task": {
          "type": "string"
        }
      },
      "required": [
        "text"
      ]
    }
  },
  {
    "name": "prism_memory_learn",
    "description": "2-Tier Write Gear (Strict Mode): source-hash verified learning of core constraints, facts, experiences (auto-synthesizing SKILL.md), and pitfall cases, with automatic contradiction/supersession resolution, Prospective Memory cue extraction, and Bidirectional Link Graph Evolution.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "revision": {
          "type": "integer",
          "description": "Current task revision from prism_memory_brief"
        },
        "operationId": {
          "type": "string",
          "description": "Idempotency operation key"
        },
        "entries": {
          "type": "array",
          "items": {
            "type": "object"
          }
        },
        "task": {
          "type": "string"
        }
      },
      "required": [
        "revision",
        "operationId",
        "entries"
      ]
    }
  },
  {
    "name": "prism_memory_skills",
    "description": "List or search domain procedural skills (SKILL.md) with AST signature-aware self-healing & file-hash staleness detection and optional federated cross-project archetype transfer.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "default": ""
        },
        "skillId": {
          "type": "string",
          "description": "Optional specific skillId to inspect in full"
        },
        "includeInactive": {
          "type": "boolean",
          "default": false
        },
        "shared": {
          "type": "boolean",
          "default": false
        },
        "federated": {
          "type": "boolean",
          "default": false
        },
        "task": {
          "type": "string"
        }
      }
    }
  },
  {
    "name": "prism_memory_skill_manage",
    "description": "Create, improve, consolidate, promote, degrade, or retract a procedural skill (SKILL.md).",
    "inputSchema": {
      "type": "object",
      "properties": {
        "revision": {
          "type": "integer"
        },
        "operationId": {
          "type": "string"
        },
        "spec": {
          "type": "object"
        },
        "task": {
          "type": "string"
        }
      },
      "required": [
        "revision",
        "operationId",
        "spec"
      ]
    }
  },
  {
    "name": "prism_memory_feedback",
    "description": "Record grounded feedback (used/helpful/incorrect/irrelevant/missing) on a recalled item or skill. Triggers 2-stage skill promotion or auto-synthesizes a pitfall case on incorrect feedback.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "operationId": {
          "type": "string"
        },
        "recallId": {
          "type": "string"
        },
        "itemId": {
          "type": "string"
        },
        "outcome": {
          "type": "string",
          "enum": [
            "used",
            "helpful",
            "incorrect",
            "irrelevant",
            "missing"
          ]
        },
        "note": {
          "type": "string",
          "default": ""
        },
        "patch": {
          "type": "object"
        },
        "task": {
          "type": "string"
        }
      },
      "required": [
        "operationId",
        "recallId",
        "itemId",
        "outcome"
      ]
    }
  },
  {
    "name": "prism_memory_preflight",
    "description": "Active Pre-Flight Guardrail & Constraint Verifier: deterministically verify a proposed action/plan and target files against active constraints, pitfall cases, hierarchical causal dependencies, and Prospective Memory triggers before execution.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "plan": {
          "type": "string",
          "description": "Proposed action or edit plan"
        },
        "files": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Target files to touch"
        },
        "actionType": {
          "type": "string",
          "default": "general"
        },
        "task": {
          "type": "string"
        }
      },
      "required": [
        "plan"
      ]
    }
  },
  {
    "name": "prism_memory_capsule",
    "description": "Subagent Scoped Memory Capsule: generate an ultra-compact (<1,200 chars default) read-only memory capsule (Pre-Flight status + Pitfall cases + Prospective triggers + Scoped skills) tailored to a delegated worker plan and target files.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "plan": {
          "type": "string",
          "description": "Delegated subagent task or plan"
        },
        "files": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Target files for the subagent"
        },
        "worker": {
          "type": "string",
          "default": "subagent"
        },
        "budget": {
          "type": "integer",
          "default": 1200
        },
        "task": {
          "type": "string"
        }
      },
      "required": [
        "plan"
      ]
    }
  },
  {
    "name": "prism_memory_inspector",
    "description": "Visual Inspector & Quantitative Benchmark: returns memory/skill/case [[links]] graph nodes & edges, thematic episode clusters, domain hierarchy tree, Mermaid diagram, prompt budget gauge, staleness alerts, and ROI scorecard.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "budget": {
          "type": "integer",
          "default": 6000
        },
        "task": {
          "type": "string"
        }
      }
    }
  },
  {
    "name": "prism_memory_scenes",
    "description": "Get thematic episode clusters and 4-level domain hierarchy (L0 Task -> L1 Pillar -> L2 Scene -> L3 Node) with directed causal triples.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "task": {
          "type": "string"
        }
      }
    }
  },
  {
    "name": "prism_memory_consolidate",
    "description": "Lossless Sleep Consolidation: merge fragmented hypothesis/draft clusters into consolidated semantic items while strictly protecting core invariants and user-confirmed rules (Identity Drift = 0.0).",
    "inputSchema": {
      "type": "object",
      "properties": {
        "similarityThreshold": {
          "type": "number",
          "default": 0.35
        },
        "dryRun": {
          "type": "boolean",
          "default": false
        },
        "task": {
          "type": "string"
        }
      }
    }
  },
  {
    "name": "prism_memory_sync_markdown",
    "description": "Bi-directional Markdown Co-Editing Sync: ingest direct human edits from SKILL.md files on disk (promoting to user-confirmed) and optional Markdown notes.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "notePath": {
          "type": "string"
        },
        "task": {
          "type": "string"
        }
      }
    }
  },
  {
    "name": "prism_memory_export_rules",
    "description": "Export or synchronize verified constraints, pitfall cases, and domain SKILL.md index into CLAUDE.md, AGENTS.md, .cursorrules, .windsurfrules, and MCP config.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "targets": {
          "type": "array",
          "items": {
            "type": "string",
            "enum": [
              "claude",
              "agents",
              "cursor",
              "windsurf",
              "mcp"
            ]
          }
        },
        "targetDir": {
          "type": "string"
        },
        "writeFiles": {
          "type": "boolean",
          "default": false
        },
        "lang": {
          "type": "string",
          "enum": [
            "ko",
            "en"
          ],
          "default": "ko"
        },
        "task": {
          "type": "string"
        }
      }
    }
  },
  {
    "name": "prism_memory_heal_skills",
    "description": "Strict AST Call-Contract Self-Healing for SKILL.md: inspect stale skills, re-anchor backward-compatible internal edits with verificationStatus=compatible-unverified, and block breaking call-contract changes.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "dryRun": {
          "type": "boolean",
          "default": false
        },
        "task": {
          "type": "string"
        }
      }
    }
  },
  {
    "name": "prism_memory_verify_skill",
    "description": "Execution-Backed Skill Verification: run a verification test/command or record a structured executionProof (exitCode, command, output) to promote or re-verify a skill and bind verificationEvidence.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "skillId": {
          "type": "string",
          "description": "Skill ID or slug to verify"
        },
        "command": {
          "type": "string",
          "description": "Optional shell command to run for verification"
        },
        "executionProof": {
          "type": "object",
          "description": "Optional structured execution proof with exitCode, command, output"
        },
        "operationId": {
          "type": "string"
        },
        "timeout": {
          "type": "number",
          "default": 15.0
        },
        "task": {
          "type": "string"
        }
      },
      "required": [
        "skillId"
      ]
    }
  },
  {
    "name": "prism_memory_doctor",
    "description": "Cross-Platform Environment, SQLite WAL, & Embedding Model Health Doctor: inspect data root writability, SQLite WAL state, and E5 ONNX model availability.",
    "inputSchema": {
      "type": "object",
      "properties": {}
    }
  },
  {
    "name": "prism_memory_audit",
    "description": "Zero-Setup Repository Procedure & Instruction Drift Auditor (deltaharness audit): scan CLAUDE.md, AGENTS.md, .cursorrules, docs, and SKILL.md for missing paths, missing scripts, missing symbols, and breaking call-signature drift.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "repoPath": {
          "type": "string",
          "description": "Repository path to audit (defaults to store root)"
        },
        "baseRef": {
          "type": "string",
          "description": "Optional base git ref for diff blast radius analysis"
        },
        "headRef": {
          "type": "string",
          "default": "HEAD"
        },
        "format": {
          "type": "string",
          "enum": [
            "json",
            "markdown",
            "sarif"
          ],
          "default": "json"
        }
      }
    }
  }
]
''')

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import memory_cli
import memory_intelligence
from prism_memory import memlet_bridge as bridge
from prism_memory import memlet_dream as dream
from prism_memory.memlet_wiki import init_project
from prism_memory_mcp import PrismMemoryMCPServer, SERVER_VERSION


class MemletMCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'app.py').write_text('def main():\n    return 1\n')
        (self.repo / 'AGENTS.md').write_text('# Rules\n\n- Keep `app.py::main` argument-free.\n')
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')
        self.assertTrue(init_project(self.repo)['ok'])
        self.server = PrismMemoryMCPServer(self.root / 'store', default_task='mcp/test')
        self.transcript = self.repo / 'session.jsonl'
        records = [
            {'type': 'user', 'timestamp': '2026-11-09T09:00:00Z',
             'message': {'role': 'user', 'content': 'Never call main() in app.py from the test suite'}},
            {'type': 'user', 'timestamp': '2026-11-09T09:02:00Z',
             'message': {'role': 'user', 'content': 'Do not push to the release branch without approval'}},
            {'type': 'assistant', 'timestamp': '2026-11-09T09:03:00Z',
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Workaround: run migration with a dry run first'}]}},
        ]
        self.transcript.write_text('\n'.join(json.dumps(r) for r in records) + '\n')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo)] + list(args), stderr=subprocess.STDOUT)

    def rpc(self, name, **args):
        args.setdefault('repo_path', str(self.repo))
        response = self.server.handle_rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                           'params': {'name': name, 'arguments': args}})
        self.assertNotIn('error', response, response)
        return response['result']

    def payload(self, name, **args):
        result = self.rpc(name, **args)
        self.assertFalse(result['isError'], result)
        return result['structuredContent']

    def learn(self):
        return self.payload('memlet_learn', action='transcript', transcript_path=str(self.transcript))

    def snapshot(self):
        return {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob('*') if p.is_file()}

    def cli(self, *args):
        with patch.object(sys, 'argv', ['memlet'] + list(args)):
            return memory_cli.main()

    def test_tool_list_preserves_all_19_baseline_tools(self):
        result = self.server.handle_rpc({'id': 1, 'method': 'tools/list'})['result']['tools']
        self.assertEqual(19, len(BASE_TOOLS))
        self.assertEqual(BASE_TOOLS, result[:19])  # descriptions and all other fields too
        self.assertEqual({'memlet_check', 'memlet_recall', 'memlet_handover', 'memlet_learn'},
                         {t['name'] for t in result[19:]})
        self.assertEqual(23, len(result))
        self.assertEqual('8.0.0', SERVER_VERSION)
        self.assertIn('Approval is CLI-only', result[-1]['description'])

    def test_check_edit_and_rename_stale(self):
        (self.repo / 'app.py').write_text('def main(required):\n    return 1\n')
        edited_response = self.rpc('memlet_check')
        self.assertTrue(edited_response['isError'])
        edited = edited_response['structuredContent']
        self.assertGreater(edited['staleCount'], 0)
        self.assertEqual(edited, bridge.changed_check(self.repo))
        self.git('mv', 'app.py', 'renamed.py')
        renamed_response = self.rpc('memlet_check')
        self.assertTrue(renamed_response['isError'])
        renamed = renamed_response['structuredContent']
        self.assertGreater(renamed['staleCount'], 0)
        self.assertIn('app.py', renamed['changedFiles'])
        self.assertIn('renamed.py', renamed['changedFiles'])

    def test_check_full_and_files_route(self):
        full = self.payload('memlet_check', changed=False)
        expected = memory_intelligence.audit_repo(self.repo, base_ref=None, head_ref='HEAD', output_format='json')
        self.assertEqual(set(expected), set(full))
        expected.pop('elapsedMs')
        full.pop('elapsedMs')
        self.assertEqual(expected, full)
        self.assertEqual(bridge.changed_check(self.repo, files=['app.py'], base='HEAD'),
                         self.payload('memlet_check', files=['app.py'], base='HEAD'))

    def test_recall_approved_only_pending_rejected_and_unverified_absent(self):
        self.learn()
        nodes = bridge.learn_pending(self.repo)['pendingNodes']
        main = next(n['id'] for n in nodes if 'main()' in n['title'])
        rejected = next(n['id'] for n in nodes if 'release' in n['title'])
        self.assertEqual([], self.payload('memlet_recall', query='main test suite', include=['dream'])['sections']['dream'])
        self.assertTrue(bridge.learn_approve(self.repo, [main], approver='test-human')['ok'])
        self.assertTrue(bridge.learn_reject(self.repo, [rejected], approver='test-human', reason='test')['ok'])
        result = self.payload('memlet_recall', query='main test suite release migration', budget=4000)
        items = result['sections']['dream']
        self.assertIn(main, [i['id'] for i in items])
        self.assertNotIn(rejected, [i['id'] for i in items])
        self.assertNotIn('migration', json.dumps(items))
        # Changed transcript evidence makes even an approved lesson unverified.
        for p in (self.repo / bridge.SNAPSHOT_REL).iterdir():
            p.write_text('tampered evidence\n')
        result = self.payload('memlet_recall', query='main test suite', include=['dream'])
        self.assertEqual([], result['sections']['dream'])

    def test_recall_uses_server_store_and_default_task(self):
        expected = bridge.unified_recall(self.repo, 'main', budget=1200,
                                         store=self.server.store, task=self.server.default_task)
        before = self.snapshot()
        result = self.payload('memlet_recall', query='main')
        self.assertEqual(expected, result)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(result['formatted'], self.rpc('memlet_recall', query='main')['content'][0]['text'])

    def test_handover_never_writes(self):
        before = self.snapshot()
        self.payload('memlet_handover', next_step='Run tests', rejected=['skip tests'], mode='verified', budget=1200)
        self.payload('memlet_handover', mode='index')
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.repo / 'HANDOVER.md').exists())
        self.assertFalse((self.repo / '.memlet' / 'HANDOVER.md').exists())

    def test_learn_transcript_pending_never_approves(self):
        original_files = {k: v for k, v in self.snapshot().items() if not k.startswith('.memlet/')}
        transcript_before = self.transcript.read_bytes()
        learned = self.learn()
        self.assertEqual(original_files, {k: v for k, v in self.snapshot().items() if not k.startswith('.memlet/')})
        self.assertEqual(transcript_before, self.transcript.read_bytes())
        self.assertGreater(learned['pendingCount'], 0)
        pending = self.payload('memlet_learn', action='pending')
        self.assertEqual(bridge.learn_pending(self.repo), pending)
        queue = json.loads((self.repo / bridge.QUEUE_REL).read_text())
        self.assertTrue(all(dream.approval_state(n, 'node') == 'pending' for n in queue['nodes']))
        self.assertEqual([], self.payload('memlet_recall', query='main release migration', include=['dream'])['sections']['dream'])

    def test_refused_actions_leave_queue_byte_identical(self):
        self.learn()
        before = self.snapshot()
        for args in [{'action': a} for a in ('approve', 'reject', 'unknown', '')] + [
                {'action': 'pending', 'approve': ['x']}, {'action': 'pending', 'by': 'human'},
                {'action': 'pending', 'reject': ['x']}]:
            with self.subTest(args=args):
                response = self.rpc('memlet_learn', **args)
                self.assertTrue(response['isError'])
                expected = 'action must be one of' if args.get('action') in ('unknown', '') else 'CLI-only'
                self.assertIn(expected, response['structuredContent']['error'])
                self.assertEqual(before, self.snapshot())

    def test_budget_and_required_fields(self):
        for tool in ('memlet_recall', 'memlet_handover'):
            for budget in (0, -1, 16001, True, '1200', 1.5):
                args = {'budget': budget}
                if tool == 'memlet_recall':
                    args['query'] = 'test'
                self.assertTrue(self.rpc(tool, **args)['isError'])
        for tool, args in [('memlet_recall', {}), ('memlet_learn', {}),
                           ('memlet_learn', {'action': 'transcript'})]:
            self.assertTrue(self.rpc(tool, **args)['isError'])
        for args in ({'include': ['unknown']}, {'query': ''}, {'query': 'x' * 8193},
                     {'include': ['rules'] * 4}):
            data = {'query': 'test'}
            data.update(args)
            self.assertTrue(self.rpc('memlet_recall', **data)['isError'])
        for args in ({'changed': 1}, {'files': ['x'] * 257}, {'base': 'x' * 257}):
            self.assertTrue(self.rpc('memlet_check', **args)['isError'])
        for mode in ('digest', 'brief', 'unknown'):
            self.assertTrue(self.rpc('memlet_handover', mode=mode)['isError'])

    def test_remaining_size_bounds_and_budget_boundaries(self):
        for tool, args in [
                ('memlet_check', {'files': ['x' * 4097]}),
                ('memlet_check', {'repo_path': 'x' * 4097}),
                ('memlet_handover', {'next_step': 'x' * 8193}),
                ('memlet_handover', {'rejected': ['x'] * 65}),
                ('memlet_handover', {'rejected': ['x' * 4097]}),
                ('memlet_handover', {'budget': 499}),
                ('memlet_handover', {'budget': 2001}),
                ('memlet_learn', {'action': 'transcript', 'transcript_path': 'x' * 4097})]:
            with self.subTest(tool=tool, key=list(args)[0]):
                self.assertTrue(self.rpc(tool, **args)['isError'])
        for budget in (1, 16000):
            response = self.rpc('memlet_recall', query='main', budget=budget)
            expected = bridge.unified_recall(self.repo, 'main', budget=budget,
                                             store=self.server.store, task=self.server.default_task)
            self.assertEqual(expected, response['structuredContent'])
            self.assertEqual(expected['ok'] is False, response['isError'])
        for budget in (500, 2000):
            self.payload('memlet_handover', budget=budget)

    def test_nonexistent_repository_and_symlinked_memlet(self):
        tools = [('memlet_check', {}), ('memlet_recall', {'query': 'test'}),
                 ('memlet_handover', {}), ('memlet_learn', {'action': 'pending'})]
        for tool, args in tools:
            self.assertTrue(self.rpc(tool, repo_path=str(self.root / 'missing'), **args)['isError'])
        other = self.root / 'other'
        other.mkdir()
        (other / '.memlet').symlink_to(self.repo / '.memlet', target_is_directory=True)
        before = self.snapshot()
        for tool, args in tools:
            result = self.rpc(tool, repo_path=str(other), **args)
            self.assertTrue(result['isError'])
            self.assertIn('symlink', result['structuredContent']['error'])
        self.assertEqual(before, self.snapshot())

    def test_transcript_path_safety(self):
        linkdir = self.root / 'linkdir'
        linkdir.symlink_to(self.root, target_is_directory=True)
        symlink = self.root / 'link.jsonl'
        symlink.symlink_to(self.transcript)
        before = self.snapshot()
        for path in (self.root, self.root / 'missing', symlink, linkdir / 'session.jsonl'):
            self.assertTrue(self.rpc('memlet_learn', action='transcript', transcript_path=str(path))['isError'])
        self.assertEqual(before, self.snapshot())

    def test_library_failure_is_tool_error(self):
        failure = {'ok': False, 'error': 'clean library failure', 'exitCode': 2}
        with patch.object(bridge, 'changed_check', return_value=failure):
            response = self.rpc('memlet_check')
        self.assertTrue(response['isError'])
        self.assertEqual(failure, response['structuredContent'])

    def test_default_repo_is_working_directory(self):
        old = Path.cwd()
        try:
            os.chdir(self.repo)
            response = self.server.handle_rpc({'id': 1, 'method': 'tools/call',
                                              'params': {'name': 'memlet_check', 'arguments': {}}})
        finally:
            os.chdir(old)
        self.assertEqual(str(self.repo), response['result']['structuredContent']['repoPath'])

    def test_cli_parity_check_recall_and_pending(self):
        check = self.payload('memlet_check')
        self.assertEqual(self.cli('check', str(self.repo), '--changed', '--format', 'json'), check)
        recall = self.payload('memlet_recall', query='main', include=['rules', 'dream'])
        cli_recall = self.cli('recall', 'main', '--repo', str(self.repo), '--format', 'json')
        self.assertEqual(set(cli_recall), set(recall))
        # Store availability differs unless explicitly requested by the CLI.
        self.assertEqual(cli_recall['sections']['rules'], recall['sections']['rules'])
        self.learn()
        pending = self.payload('memlet_learn', action='pending')
        self.assertEqual(self.cli('learn', '--pending', '--repo', str(self.repo), '--format', 'json'), pending)

    def test_learn_requires_initialized_repository_without_creating_state(self):
        uninitialized = self.root / 'uninitialized'
        uninitialized.mkdir()
        for action in ('transcript', 'pending'):
            args = {'action': action}
            if action == 'transcript':
                args['transcript_path'] = str(self.transcript)
            result = self.rpc('memlet_learn', repo_path=str(uninitialized), **args)
            self.assertTrue(result['isError'])
            self.assertIn('memlet init', result['structuredContent']['error'])
            self.assertEqual([], list(uninitialized.iterdir()))

    def test_external_transcript_is_refused_before_library_read_or_write(self):
        external = self.root / 'external.log'
        external.write_text('2026-11-09T10:00:00Z RULE: never call main() in app.py; PRIVATE_CANARY\n')
        before = self.snapshot()
        with patch.dict(os.environ, {'DELTAHARNESS_TRANSCRIPT_DIRS': '', 'PRISM_TRANSCRIPT_DIRS': ''}), \
                patch.object(bridge, 'learn_from_transcript') as learn:
            result = self.rpc('memlet_learn', action='transcript', transcript_path=str(external))
            learn.assert_not_called()
        self.assertTrue(result['isError'])
        self.assertIn('known agent transcript root', result['structuredContent']['error'])
        self.assertNotIn('PRIVATE_CANARY', json.dumps(result))
        self.assertEqual(before, self.snapshot())
        self.assertNotIn('PRIVATE_CANARY', json.dumps(self.payload('memlet_learn', action='pending')))

    def test_known_agent_roots_and_trusted_environment_override(self):
        home = self.root / 'home'
        with patch('prism_memory_mcp.Path.home', return_value=home), \
                patch.dict(os.environ, {'DELTAHARNESS_TRANSCRIPT_DIRS': '', 'PRISM_TRANSCRIPT_DIRS': ''}):
            for relative in ('.prism-memory/sessions', '.codex/sessions', '.claude/projects', '.muse/sessions'):
                directory = home / relative
                directory.mkdir(parents=True)
                transcript = directory / 'session.jsonl'
                transcript.write_bytes(self.transcript.read_bytes())
                self.payload('memlet_learn', action='transcript', transcript_path=str(transcript))
        custom = self.root / 'trusted'
        custom.mkdir()
        transcript = custom / 'session.jsonl'
        transcript.write_bytes(self.transcript.read_bytes())
        for variable in ('DELTAHARNESS_TRANSCRIPT_DIRS', 'PRISM_TRANSCRIPT_DIRS'):
            env = {'DELTAHARNESS_TRANSCRIPT_DIRS': '', 'PRISM_TRANSCRIPT_DIRS': '', variable: str(custom)}
            with patch.dict(os.environ, env):
                self.payload('memlet_learn', action='transcript', transcript_path=str(transcript))
                # The repository remains allowed alongside a host-configured root.
                self.learn()
                rejected = self.rpc('memlet_learn', action='transcript', transcript_path=str(home / '.codex/sessions/session.jsonl'))
                self.assertTrue(rejected['isError'])

    def test_oversized_review_transcript_refused_before_library_without_writes(self):
        large = self.repo / 'review-21mib.log'
        with large.open('w') as output:
            output.write('2026-11-09T10:00:00Z RULE: never call main() in app.py\n')
            for _ in range(21):
                output.write('z' * 1048576 + '\n')
        before = self.snapshot()
        with patch.object(bridge, 'learn_from_transcript') as learn:
            result = self.rpc('memlet_learn', action='transcript', transcript_path=str(large))
            learn.assert_not_called()
        self.assertTrue(result['isError'])
        self.assertIn('8 MiB', result['structuredContent']['error'])
        self.assertEqual(before, self.snapshot())

    def test_transcript_fifo_and_symlinked_ancestor_are_refused_without_reading(self):
        fifo = self.repo / 'named-pipe.log'
        os.mkfifo(str(fifo))
        directory = self.repo / 'linked-dir'
        directory.symlink_to(self.repo, target_is_directory=True)
        # snapshot() reads regular files only; do not read the FIFO itself.
        with patch.object(bridge, 'learn_from_transcript') as learn:
            for path in (fifo, directory / self.transcript.name, self.repo):
                result = self.rpc('memlet_learn', action='transcript', transcript_path=str(path))
                self.assertTrue(result['isError'])
            learn.assert_not_called()

    def test_handover_configured_index_matches_cli_when_mode_is_omitted(self):
        config = self.repo / '.memlet/config.json'
        data = json.loads(config.read_text())
        data['handover'] = {'mode': 'index'}
        config.write_text(json.dumps(data))
        result = self.payload('memlet_handover')
        expected = self.cli('handover', str(self.repo), '--format', 'json')
        self.assertEqual('index', result['mode'])
        self.assertEqual(expected, result)

    def test_experimental_configured_mode_is_explicitly_refused(self):
        config = self.repo / '.memlet/config.json'
        for mode in ('digest', 'brief'):
            data = json.loads(config.read_text())
            data['handover'] = {'mode': mode}
            config.write_text(json.dumps(data))
            before = self.snapshot()
            result = self.rpc('memlet_handover')
            self.assertTrue(result['isError'])
            self.assertIn('experimental', result['structuredContent']['error'])
            self.assertIn(mode, result['structuredContent']['error'])
            self.assertEqual(before, self.snapshot())
            self.assertEqual('index', self.payload('memlet_handover', mode='index')['mode'])


if __name__ == '__main__':
    unittest.main()
