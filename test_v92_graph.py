"""E1 graph contract, hermetic fixtures; no model, network or user-store access."""
import copy
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import test_v91_integration as fixtures
import memlet_mcp
from memlet import memlet_graph as graph, memlet_bridge as mb, memlet_dream as md
from memlet import memlet_wiki as mw, memlet_anchors as ma, memlet_dreamcycle as dc
from memlet import vendor_cli, cli_plugins, memlet_eval, handover_bridge as hb
from memlet.vocabulary import HINT_LABEL, HINT_HELP, GRAPH_WHY


# Frozen E3 e46bd70 rule: differential oracle, never imported by production.
_LEGACY_PATH_RULE = r'''
def valid_lesson_path(root, path):
    """Exact, contained regular file; an attachment names a location, not content identity.

    Recreating a removed file at that location reactivates it, just like a file anchor.
    No normalization is allowed for a human approval.
    """
    root = Path(root)
    if (not isinstance(path, str) or not path or len(path) > 240
            or any(c in path for c in "\\*?[]`<>") or unsafe_contributed_string(path) or path.startswith("/")
            or any(p in ("", ".", "..", ".git", ".memlet") for p in path.split("/"))
            or mw._is_secret_path(path) or mw.contained_path_problem(root, path)):
        return False
    cursor = root
    try:
        for part in path.split("/"):
            if part not in {p.name for p in cursor.iterdir()}:
                return False
            cursor = cursor / part
            if cursor != root / path and (cursor / ".git").exists():
                return False
        return cursor.is_file()
    except OSError:
        return False

def lesson_path_context(repo_path="."):
    """One bounded tracked-file lookup per projection; lazy citation context for human paths.

    An invalid/unreadable git work tree fails closed for automatic attachments.
    """
    root = mw._resolve_root(repo_path)
    is_git = any((p / ".git").exists() for p in [root] + list(root.parents))
    tracked = set()
    if is_git:
        process = None
        selector = selectors.DefaultSelector()
        try:
            process = subprocess.Popen(["git", "ls-files", "-z", "--cached", "--"], cwd=str(root),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            selector.register(process.stdout, selectors.EVENT_READ)
            data = bytearray()
            deadline = time.monotonic() + .5
            complete = False
            while time.monotonic() < deadline and len(data) <= 4 * 1024 * 1024:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    complete = True
                    break
                data.extend(chunk)
            if complete and len(data) <= 4 * 1024 * 1024 and process.wait(timeout=.1) == 0:
                tracked = set(data.decode("utf-8", "surrogateescape").split("\0"))
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            selector.close()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()
    return {"root": root, "is_git": is_git, "tracked": tracked, "loaded": False, "trust": {}}

def literal_lesson_paths(root, scope, text, *, context=None):
    """Extract whole path tokens from text; Git requires tracked files.

    ``scope`` is retained for caller compatibility but never attaches a path: it
    is only a display label and recall filter term, regardless of proposer/tier.
    """
    context = lesson_path_context(root) if context is None else context
    root = context["root"]
    raw = [p for p, _ in mw._line_refs(text)]
    # Enumerate maximal path-like tokens, then strip terminal sentence punctuation.
    raw += [m.group().rstrip(".") for m in re.finditer(r"(?<![\w/.-])(?:\./)?[\w.-]+(?:/[\w.-]+)*", text)]
    out = []
    punctuation = ".,;:!?)]\"'`"
    for path in raw:
        path = path.replace("\\", "/").removeprefix("./")
        named = False
        for match in re.finditer(r"(?<![\w/.-])(?:\./)?" + re.escape(path), text):
            before = text[match.start()-1:match.start()] if match.start() else ""
            tail = text[match.end():]
            after = tail[:1]
            terminal = re.match("[" + re.escape(punctuation) + "]+(?=\\s|$)", tail)
            boundary = not tail or tail[0].isspace() or terminal is not None
            if boundary and not any(unicodedata.category(c) in ("Cc", "Cf", "Mn", "Me") for c in before + after):
                named = True
                break
        if (named and len(path) <= 80 and valid_lesson_path(root, path) and path not in out
                and (not context["is_git"] or path in context["tracked"])):
            out.append(path)
    return out[:8]

def _lesson_literal_paths(node, root, context):
    """Match original hashed lines, never a sanitised display projection.

    Own citations were approved with the node; only approved-valid edges may add
    further evidence. Each line is matched separately so joins cannot invent tokens.
    """
    scope = node.get("scopeTag") or ""
    fields = md.lesson_display_fields(node)
    roles = fields.get("lineRoles", ["current"] * len(fields.get("cites", [])))
    paths = []
    evidence = list(zip(fields.get("cites", []), roles))
    replaced = {c["span"] for c, role in evidence if role == "replaced"}
    for edge in node.get("edges", []):
        if md.approval_state(edge, "edge") == TIER_APPROVED and edge.get("edgeVerdict") == "VALID":
            for pin in md._item_pins(edge, "edge") + [edge.get("citedTargetPin")]:
                if not isinstance(pin, dict) or md._verify_pin_dict(pin, context["snapshot"] or {}, "path"):
                    continue
                parsed = md.parse_span(pin.get("span", ""))
                if parsed:
                    for number in range(parsed[1], parsed[2] + 1):
                        span = "%s:%d" % (parsed[0], number)
                        if span not in replaced:
                            evidence.append((md.resolve_pin(context["snapshot"], span), "current"))
    for cite, role in evidence:
        if role != "current":
            continue
        pin = md.resolve_pin(context["snapshot"] or {}, cite["span"])
        if not pin or pin.get("sha256") != cite.get("sha256"):
            continue
        for line in str(pin.get("quoteText", "")).split("\n"):
            for path in literal_lesson_paths(root, scope, line, context=context):
                if path not in paths:
                    paths.append(path)
    return paths[:8]

def lesson_paths(node, repo_path=".", *, context=None):
    """Human-attached locations: approved tier, exact existing file; tracking is optional."""
    if not node.get("approvedPaths") or md.approval_state(node, "node") != TIER_APPROVED:
        return []
    context = lesson_path_context(repo_path) if context is None else context
    if not context["loaded"]:
        snapshot, _ = _load_snapshot(context["root"])
        config, _ = _load_config(context["root"])
        graph, _ = _load_queue(context["root"])
        context.update(loaded=True, snapshot=snapshot, config=tier_config(graph or {}, config),
                       trust=dream_tiers(graph or {}, snapshot=snapshot, config=config))
    state = context["trust"].get(node["id"])
    if state is None:
        state = {"tier": TIER_PENDING}
        context["trust"][node["id"]] = state
    if state["tier"] != TIER_APPROVED:
        return []
    return [p for p in node.get("approvedPaths", []) if valid_lesson_path(context["root"], p)]
'''


class GraphTests(fixtures.Scratch):
    _graph_test_python = None

    def setUp(self):
        # Same hermetic roots as Scratch, without its unused MCP server/store.
        # Probe the test interpreter once per process, not once per graph case.
        self.tmp = tempfile.TemporaryDirectory(prefix='memlet-graph-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'repo'
        self.root.mkdir()
        for name in ('home', 'tmp', 'bin'):
            (self.base / name).mkdir()
        env = dict(os.environ, HOME=str(self.base / 'home'), TMPDIR=str(self.base / 'tmp'),
                   CLAUDE_CONFIG_DIR=str(self.base / 'home/claude'), CODEX_HOME=str(self.base / 'home/codex'),
                   GIT_CEILING_DIRECTORIES=str(self.base), GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CONFIG_NOSYSTEM='1', MEMLET_LOCK_DIR=str(self.base / 'locks'), MEMLET_DREAMING='0',
                   MEMLET_HOME=str(self.base / 'unused'), MEMLET_TRANSCRIPT_DIRS=str(self.root))
        for key in ('PYTHONHOME', 'PYTHONEXECUTABLE', '__PYVENV_LAUNCHER__', 'CODEX_THREAD_ID', 'MEMLET_PARENT_SESSION_ID'):
            env.pop(key, None)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(tempfile, 'tempdir', str(self.base / 'tmp'))
        patch.start(); self.addCleanup(patch.stop)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, env=dict(os.environ))
        if GraphTests._graph_test_python is None:
            for python in dict.fromkeys([sys.executable, shutil.which('python3'), '/usr/bin/python3']):
                if python and Path(python).is_file():
                    probe = subprocess.run([python, '-c', 'pass'], env=dict(os.environ), capture_output=True)
                    if probe.returncode == 0:
                        GraphTests._graph_test_python = python
                        break
        self.python = GraphTests._graph_test_python
        self.assertIsNotNone(self.python)
        # Functional and work-bound assertions must not depend on machine load.
        patch = mock.patch.object(graph, '_monotonic', return_value=0.0)
        patch.start(); self.addCleanup(patch.stop)

    def cli(self, *args):
        if 'graph' not in args:
            return super().cli(*args)
        script = ("import sys,runpy; sys.path.insert(0,sys.argv[1]); "
                  "from memlet import memlet_graph; memlet_graph._monotonic=lambda:0.0; "
                  "sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')")
        return subprocess.run([self.python, '-c', script, str(fixtures.CLI.parent), str(fixtures.CLI)] + list(args),
                              cwd=self.root, env=dict(os.environ), capture_output=True, text=True, timeout=60)

    def fixture(self, state='approved', kinds=None, *, cited_path=True):
        # The amended contract requires a path in cited bytes. Keep the shared
        # eight-kind fixture's trust/pin construction, adding a literal path
        # before ingest; scope-only adversarial cases explicitly opt out.
        ingest = dc.dream_ingest
        def cited_ingest(repo, source, *args, **kwargs):
            if cited_path:
                source = Path(source)
                source.write_text("\n".join(line + ' `deploy.py`' for line in source.read_text().splitlines()) + "\n")
            return ingest(repo, source, *args, **kwargs)
        with mock.patch.object(dc, 'dream_ingest', side_effect=cited_ingest):
            return fixtures.ApprovedReadersIntegration.fixture(self, state, kinds)

    save_graph = fixtures.ApprovedReadersIntegration.save_graph
    kinds = fixtures.ApprovedReadersIntegration.kinds

    def tracked(self, *paths):
        subprocess.run(['git', 'add', '--'] + list(paths), cwd=self.root, check=True, capture_output=True)

    def result(self, root=None, target='auth.py', **kw):
        result = graph.neighbors(root or self.root, target, **kw)
        self.assertTrue(result['ok'], result)
        self.assertLessEqual(len(result['formatted']), 8000)
        self.assertLessEqual(result['tokens'], kw.get('budget', 600))
        return result

    def test_shared_rule_differential_e3_attachment_tables(self):
        import test_v92_lessons as e3
        legacy = dict(vars(mb))
        exec(_LEGACY_PATH_RULE, legacy)
        compared = {'valid_lesson_path': 0, 'literal_lesson_paths': 0, 'lesson_paths': 0}
        def checked(name):
            current = getattr(mb, name)
            def check(*args, **kwargs):
                result = current(*args, **kwargs)
                old_kw = dict(kwargs)
                if name == 'valid_lesson_path':
                    old_kw.pop('context', None)
                expected = legacy[name](*args, **old_kw)
                self.assertEqual(expected, result, (name, args[1:] if len(args) > 1 else args))
                compared[name] += 1
                return result
            return check
        # Execute E3's existing tables unchanged; compare every individual path
        # and attachment read on those same fixtures, including transitions.
        cases = [(e3.Lessons, 'test_scope_never_attaches_across_readers_and_human_paths'),
                 (e3.Lessons, 'test_multiselect_paths_literal_order_and_edge_tier_gate'),
                 (e3.Lessons, 'test_literal_attachments_use_raw_cited_lines'),
                 (e3.ReviewRegressions, 'test_raw_token_boundaries_and_dot_prefix')]
        with contextlib.ExitStack() as stack:
            for name in compared:
                stack.enter_context(mock.patch.object(mb, name, side_effect=checked(name)))
            for cls, method in cases:
                result = unittest.TestResult()
                cls(method).run(result)
                self.assertEqual([], result.errors, method)
                self.assertEqual([], result.failures, method)
        self.assertTrue(all(n > 0 for n in compared.values()), compared)
        print('I5 differential E3 path tables:', compared)

    def assert_reader_agreement(self, repo, target):
        with mb._lesson_path_projection(repo, None) as context:
            approved = {i['id']: dict(i, paths=context['paths'].get(i['id'], [])) for i in mb.dream_items(repo, _path_context=context)['items']}
        built = graph.build_graph(repo)
        self.assertTrue(built['ok'], built)
        projected = {i['id']: i for i in built['nodes'] if i.get('recordType') == 'item' and i['tier'] == 'approved'}
        self.assertEqual(set(approved), set(projected))
        for ident, item in approved.items():
            self.assertEqual(item.get('paths', []), projected[ident]['paths'])
            self.assertEqual(item['edgeTypes'], projected[ident]['edgeTypes'])
            self.assertEqual(item.get('lineRoles'), projected[ident].get('lineRoles'))
        selected = {i for i, item in approved.items() if target in item.get('paths', [])}
        self.assertEqual(selected, {i['id'] for i in mb.dream_items(repo, files=[target])['items']})
        recall = mb.unified_recall(repo, files=[target], budget=32000)
        self.assertEqual(selected, {i['id'] for i in recall['sections']['dream']})
        changed = mb.changed_check(repo, files=[target])
        self.assertEqual(selected, {i['id'] for i in changed['approvedLessons'] if i['via'] == 'path'})
        self.assertTrue(all(i['via'] in ('path', 'scope') for i in changed['approvedLessons']))
        hand = mw.build_handover(repo, budget=32000, write=True)
        self.assertEqual(set(approved), {i['id'] for i in hand['approvedLessons']})
        capsules = json.loads((repo / hb.CAPSULES_FILE).read_text())['capsules']
        exported = json.loads((repo / hb.HANDOVER_FILE).read_text())
        for capsule in capsules:
            item = approved[capsule['id']]
            self.assertEqual(item.get('paths', []), capsule['paths'])
            self.assertTrue(all('memlet-' + relation.lower() in capsule['tags'] for relation in item['edgeTypes']))
        for rejected in exported['rejectedApproaches']:
            ident = next(i for i in approved if rejected['id'].startswith(i + '-old'))
            self.assertIn('replaced', projected[ident]['lineRoles'])
        for item in approved.values():
            if not item.get('quarantined'):
                self.assertIn(item['id'], {i['id'] for i in capsules})

    def test_recall_hint_files_normalisation_and_unfiltered_bytes(self):
        repo, nodes = self.fixture('hint', ('BLOCKS',))
        (repo / 'other.py').write_text('x = 1\n')
        original = mb.dream_items
        # Reproduce the old no-files branch exactly, then compare all bytes.
        def old_hints(*args, **kwargs):
            if kwargs.get('tiers') == ('hint',):
                kwargs.pop('files', None)
            return original(*args, **kwargs)
        with mock.patch.object(mb, 'dream_items', side_effect=old_hints):
            before = mb.unified_recall(repo, budget=8000)
        self.assertEqual(before, mb.unified_recall(repo, budget=8000))
        self.assertEqual([nodes['BLOCKS']['id']], [i['id'] for i in before['sections']['hints']])
        alias = self.base / 'recall-alias'; alias.symlink_to(repo, target_is_directory=True)
        for files in (['other.py'], str(repo / 'other.py'), [str(alias / 'other.py')], ['OTHER.py']):
            self.assertEqual([], mb.unified_recall(repo, files=files, budget=8000)['sections']['hints'])
        expected = {nodes['BLOCKS']['id']}
        selected = mb.dream_items(repo, tiers=('hint',), files=['deploy.py'])['items']
        self.assertEqual(expected, {i['id'] for i in selected})
        self.assertEqual(['deploy.py'], selected[0]['paths'])
        self.assertNotIn('paths', mb.dream_items(repo, tiers=('hint',))['items'][0])
        self.assertEqual(expected, {i['id'] for i in mb.unified_recall(repo, files=['deploy.py'])['sections']['hints']})
        self.assertEqual(expected, {i['id'] for i in self.result(repo, 'deploy.py', tiers=('approved', 'hint'))['groups']['hints']})
        self.assert_reader_agreement(repo, 'deploy.py')
        for path in (hb.HANDOVER_FILE, hb.CAPSULES_FILE):
            self.assertNotIn(nodes['BLOCKS']['id'], (repo / path).read_text())

    def test_scope_only_reader_agreement(self):
        repo, _ = self.fixture('approved', ('BLOCKS',), cited_path=False)
        self.assert_reader_agreement(repo, 'deploy.py')
        self.assertFalse(any(self.result(repo, 'deploy.py')['groups'].values()))
        self.assertEqual('scope', mb.changed_check(repo, files=['deploy.py'])['approvedLessons'][0]['via'])

    def test_nodes_edges_provenance_and_no_containment_relation(self):
        self.anchors()
        self.tracked('auth.py', 'AGENTS.md')
        self.write('other.py', 'def other():\n    return 1\n')
        self.write('CLAUDE.md', '- Plain unanchored rule.\n- Unpinned `other.py::other`.\n')
        built = graph.build_graph(self.root)
        self.assertTrue(built['ok'], built)
        self.assertEqual(set(graph.NODE_KINDS), set(built['counts']['nodes']))
        self.assertEqual(1, built['counts']['unanchoredRules'])
        nodes = {n['id']: n for n in built['nodes']}
        self.assertEqual(4, built['counts']['nodes']['rule'])
        self.assertTrue(all(e['type'] in md.EDGE_TYPES for e in built['edges']))
        suggested = [e for e in built['edges'] if e['basis'] == 'accepted-suggestion']
        self.assertEqual(1, len(suggested))
        self.assertIsNotNone(suggested[0]['validFrom'])
        for edge in built['edges']:
            self.assertIn(edge['src'], nodes)
            self.assertIn(edge['dst'], nodes)
            self.assertEqual('file', nodes[nodes[edge['dst']]['file']]['kind'])
        res = self.result()
        self.assertEqual(2, len(res['groups']['governs']))
        self.assertIn('[suggested anchor]', res['formatted'])
        self.assertTrue(all(i['why'] == 'anchor-in-file' for i in res['groups']['governs']))
        res = self.result(target='other.py')
        self.assertEqual([], res['groups']['governs'])
        self.assertEqual('unpinned', res['groups']['stale'][0]['status'])

    def test_determinism_and_ids_do_not_depend_on_unrelated_items(self):
        self.anchors()
        first = graph.build_graph(self.root)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(graph.build_graph(self.root), sort_keys=True))
        ids = {n['text']: n['id'] for n in first['nodes'] if n['kind'] == 'rule'}
        self.write('CLAUDE.md', '- An unrelated instruction.\n')
        after = graph.build_graph(self.root)
        self.assertEqual(ids, {n['text']: n['id'] for n in after['nodes'] if n.get('text') in ids})

    def test_symbol_is_narrower_than_file_and_id_has_targets(self):
        self.write('auth.py', 'def one():\n    return 1\ndef two():\n    return 2\n')
        self.write('AGENTS.md', '- First `auth.py::one`.\n- Second `auth.py::two`.\n')
        mw.init_project(self.root)
        file_result = self.result()
        self.assertEqual(2, len(file_result['groups']['governs']))
        symbol = self.result(target='auth.py::one')
        self.assertEqual(1, len(symbol['groups']['governs']))
        self.assertEqual('anchor-symbol', symbol['groups']['governs'][0]['why'])
        rule = symbol['groups']['governs'][0]
        by_id = self.result(target=rule['id'])
        self.assertEqual(['auth.py::one'], by_id['groups']['governs'][0]['targets'])
        self.assertEqual('target-item', by_id['groups']['governs'][0]['why'])

    def test_stale_same_stat_caches_are_not_proof(self):
        self.anchors()
        path = self.root / 'auth.py'
        before = path.stat()
        self.result()
        text = path.read_text().replace('validate_token', 'validate_other')
        self.assertEqual(len(text.encode()), before.st_size)
        path.write_text(text)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        res = self.result()
        self.assertEqual([], res['groups']['governs'])
        self.assertEqual(2, len(res['groups']['stale']))
        self.assertTrue(all(i['status'] == 'stale' for i in res['groups']['stale']))
        path.unlink()
        self.assertEqual(2, len(self.result()['groups']['stale']))

    def test_unsafe_targets_return_only_reason(self):
        self.write('auth.py', fixtures.CODE)
        self.write('.env', 'SECRET=fixture\n')
        (self.root / 'link.py').symlink_to(self.root / 'auth.py')
        (self.root / 'linked').symlink_to(self.root, target_is_directory=True)
        for target in ('../auth.py', str(self.base / 'outside.py'), 'link.py', 'linked/auth.py',
                       '.env', 'auth.py\u200b', 'rule-' + '0' * 24, 'DRM-unknown', 'auth.py::bad/symbol'):
            with self.subTest(target=target):
                res = graph.neighbors(self.root, target)
                self.assertEqual({'ok', 'reason'}, set(res))
                self.assertFalse(res['ok'])

    def test_all_eight_kinds_six_states_agree_with_existing_readers(self):
        # The explicit agreement table is executable: each row is a kind/state,
        # each column is an independently exercised existing reader.
        table = []
        for state in ('approved', 'hint', 'pending', 'rejected', 'edge-rejected', 'vetoed'):
            repo, nodes = self.fixture(state)
            with mb._lesson_path_projection(repo, None) as context:
                read = mb.dream_items(repo, tiers=('approved', 'hint'), _path_context=context)['items']
                attachments = dict(context['paths'])
            built = graph.build_graph(repo)
            self.assertTrue(built['ok'], built)
            projected = {n['id']: n for n in built['nodes'] if n.get('recordType') == 'item'}
            recall = mb.unified_recall(repo, 'deploy release make', files=['deploy.py'], budget=32000)
            changed = mb.changed_check(repo, files=['deploy.py'])
            hand = mw.build_handover(repo, budget=32000, write=True)
            routlet_hand = json.loads((repo / hb.HANDOVER_FILE).read_text())
            capsules = json.loads((repo / hb.CAPSULES_FILE).read_text())['capsules']
            by_reader = {'dream_items': {i['id']: i['tier'] for i in read},
                         'graph': {i: n['tier'] for i, n in projected.items()},
                         'recall': {i['id']: 'approved' for i in recall['sections']['dream']},
                         'handover': {i['id']: 'approved' for i in hand['approvedLessons']},
                         'changed': {i['id']: 'approved' for i in changed['approvedLessons']},
                         'capsules': {i['id']: 'approved' for i in capsules}}
            for kind, node in nodes.items():
                row = {reader: values.get(node['id']) for reader, values in by_reader.items()}
                table.append((kind, state, row))
                self.assertEqual(row['dream_items'], row['graph'], (kind, state, row))
                self.assertEqual(row['graph'] == 'approved', row['recall'] == 'approved', (kind, state, row))
                self.assertEqual(row['graph'] == 'approved', row['handover'] == 'approved', (kind, state, row))
                for reader in ('changed', 'capsules'):
                    self.assertEqual(row['graph'] == 'approved', row[reader] == 'approved', (kind, state, row))
                if node['id'] in projected:
                    original = next(i for i in read if i['id'] == node['id'])
                    self.assertEqual(original.get('lineRoles'), projected[node['id']].get('lineRoles'))
                    self.assertEqual(original['cites'], projected[node['id']]['cites'])
                    if original['tier'] == 'approved':
                        capsule = next(i for i in capsules if i['id'] == node['id'])
                        self.assertEqual(attachments.get(original['id'], []), capsule['paths'])
                        self.assertEqual(attachments.get(original['id'], []), projected[node['id']]['paths'])
                    else:
                        self.assertNotIn('paths', original)  # E3 hints are display only.
            old = routlet_hand['rejectedApproaches']
            self.assertEqual(state == 'approved', bool(old))
            for entry in old:
                self.assertTrue(entry['id'].startswith(nodes['SUPERSEDES']['id'] + '-old'))
            # The same explicit file selection must also exclude unrelated
            # hints; before I5, recall's hints branch ignored files entirely.
            (repo / 'unrelated.py').write_text('x = 1\n')
            filtered = mb.dream_items(repo, tiers=('approved', 'hint'), files=['unrelated.py'])
            filtered_recall = mb.unified_recall(repo, files=['unrelated.py'], budget=32000)
            filtered_graph = self.result(repo, 'unrelated.py', tiers=('approved', 'hint'), budget=8000)
            self.assertEqual([], filtered['items'])
            self.assertEqual([], filtered_recall['sections']['dream'])
            self.assertEqual([], filtered_recall['sections']['hints'])
            self.assertFalse(any(filtered_graph['groups'].values()))
            result = self.result(repo, 'deploy.py', tiers=('approved', 'hint'), budget=8000)
            filtered_hints = {i['id'] for i in mb.dream_items(repo, tiers=('hint',), files=['deploy.py'])['items']}
            self.assertEqual(filtered_hints, {i['id'] for i in recall['sections']['hints']}, state)
            self.assertEqual(filtered_hints, {i['id'] for i in result['groups']['hints']}, state)
            for group in ('governs', 'blocks', 'resolves', 'replaced'):
                self.assertTrue(all(i['tier'] == 'approved' for i in result['groups'][group]))
        self.assertEqual(48, len(table))

    def test_hint_label_caveat_tier_and_pending_exclusion(self):
        repo, nodes = self.fixture('hint', ('BLOCKS',))
        # Give this lesson a literally cited tracked path, without changing its
        # receipt-bound evidence: use the actual quoted path as a new filename.
        node = nodes['BLOCKS']
        data = mb._load_queue(repo)[0]
        snapshot = mb._load_snapshot(repo)[0]
        fid = next(iter(snapshot))
        snapshot[fid][0] += ' `deploy.py`'
        (repo / mb.SNAPSHOT_REL / fid).write_text('\n'.join(snapshot[fid]) + '\n')
        changed = data['nodes'][0]
        pin = md.resolve_pin(snapshot, fid + ':1')
        changed['primaryPin'] = pin; changed['allPins'] = [pin]
        changed['edges'][0]['srcPin'] = pin
        data['edges'][0]['srcPin'] = pin
        changed['proposers'] = [dc._receipt(vendor, changed, 'f' * 24,
                                           {'model': 'haiku' if vendor == 'claude' else 'codex'})
                                for vendor in ('claude', 'codex')]
        self.save_graph(repo, data)
        self.assertEqual('hint', mb.dream_items(repo, tiers=('hint',))['items'][0]['tier'])
        default = self.result(repo, 'deploy.py')
        self.assertFalse(any(default['groups'].values()))
        hints = self.result(repo, 'deploy.py', tiers=('approved', 'hint'), budget=2000)
        self.assertEqual(1, len(hints['groups']['hints']))
        self.assertIn(HINT_LABEL, hints['formatted'])
        self.assertIn(HINT_HELP, hints['formatted'])
        self.assertTrue(all(not hints['groups'][k] for k in graph.GROUPS if k != 'hints'))
        self.assertFalse(graph.neighbors(repo, 'deploy.py', tiers=('pending',))['ok'])

    def test_approved_node_does_not_approve_its_pending_relations(self):
        repo, _ = self.fixture('approved', ('BLOCKS',))
        data = mb._load_queue(repo)[0]
        node = data['nodes'][0]
        for relation in ('GOVERNS', 'RESOLVES', 'SUPERSEDES'):
            edge = copy.deepcopy(node['edges'][0])
            edge.update(edgeId='PENDING-' + relation, edgeType=relation,
                        toId='INLINE-OLD-' + relation, edgeVerdict='PENDING')
            edge.pop('approval', None)
            if relation == 'SUPERSEDES':
                edge['targetPin'] = copy.deepcopy(node['primaryPin'])
            node['edges'].append(edge)
            data['edges'].append(copy.deepcopy(edge))
        self.save_graph(repo, data)
        self.assertEqual('approved', mb.dream_items(repo)['items'][0]['tier'])
        result = self.result(repo, 'deploy.py')
        self.assertEqual(1, len(result['groups']['blocks']))
        for group in ('governs', 'resolves', 'replaced', 'hints'):
            self.assertEqual([], result['groups'][group], group)
        built = graph.build_graph(repo)
        lesson_edges = [e for e in built['edges'] if e['src'] == node['id']]
        self.assertEqual(['BLOCKS'], [e['type'] for e in lesson_edges])
        self.assert_reader_agreement(repo, 'deploy.py')

    def test_attachment_exact_tracked_regular_literal_only(self):
        repo, nodes = self.fixture('approved', ('BLOCKS',), cited_path=False)
        data = mb._load_queue(repo)[0]
        n = data['nodes'][0]
        for scope in ('deploy.py.bak', 'DEPLOY.py', 'missing.py', '../deploy.py', 'link.py', 'untracked.py'):
            (repo / 'link.py').unlink(missing_ok=True)
            (repo / 'link.py').symlink_to(repo / 'deploy.py')
            (repo / 'untracked.py').write_text('fixture\n')
            n['scopeTag'] = scope
            n['approval']['digest'] = md.node_approval_digest(n)
            self.save_graph(repo, data)
            result = self.result(repo, 'deploy.py', budget=1000)
            self.assertEqual([], result['groups']['blocks'], scope)
            built = graph.build_graph(repo)
            self.assertEqual(1, built['counts']['unattachedLessons'], scope)
        # A scope alone still cannot attach; add the path to the cited bytes.
        n['scopeTag'] = 'deploy.py'
        snapshot = mb._load_snapshot(repo)[0]
        fid = n['primaryPin']['fileId']
        snapshot[fid][0] += ' `deploy.py`'
        (repo / mb.SNAPSHOT_REL / fid).write_text('\n'.join(snapshot[fid]) + '\n')
        pin = md.resolve_pin(snapshot, fid + ':1')
        n.update(primaryPin=pin, allPins=[pin])
        n['edges'][0]['srcPin'] = pin
        n['edges'][0]['approval']['digest'] = md.edge_approval_digest(n['edges'][0])
        data['edges'][0] = copy.deepcopy(n['edges'][0])
        n['approval']['digest'] = md.node_approval_digest(n)
        self.save_graph(repo, data)
        item = self.result(repo, 'deploy.py')['groups']['blocks'][0]
        self.assertEqual('lesson-cites-path', item['why'])

    def test_hidden_path_cannot_attach_after_display_sanitization(self):
        repo, _ = self.fixture('approved', ('BLOCKS',), cited_path=False)
        data = mb._load_queue(repo)[0]
        node = data['nodes'][0]
        snapshot = mb._load_snapshot(repo)[0]
        fid = node['primaryPin']['fileId']
        snapshot[fid][0] += ' deploy.py\u200b'
        (repo / mb.SNAPSHOT_REL / fid).write_text('\n'.join(snapshot[fid]) + '\n')
        pin = md.resolve_pin(snapshot, fid + ':1')
        node.update(scopeTag='general', primaryPin=pin, allPins=[pin])
        node['approval']['digest'] = md.node_approval_digest(node)
        node['edges'][0]['srcPin'] = pin
        node['edges'][0]['approval']['digest'] = md.edge_approval_digest(node['edges'][0])
        data['edges'][0] = copy.deepcopy(node['edges'][0])
        self.save_graph(repo, data)
        self.assertEqual('approved', mb.dream_items(repo)['items'][0]['tier'])
        self.assertEqual([], self.result(repo, 'deploy.py')['groups']['blocks'])

    def pair_history(self, approve=True):
        self.write('auth.py', 'def one():\n    return 1\n')
        old = 'Always use `auth.py::one` for deployment.'
        middle = 'Deploy with the warmed cache in auth.py.'
        newest = 'Deploy with the final checked cache in auth.py.'
        self.write('AGENTS.md', '- ' + old + '\n')
        mw.init_project(self.root); self.tracked('auth.py', 'AGENTS.md')
        for name, text, date in [('old.log', old, '2026-01-01'), ('middle.log', middle, '2026-01-02'),
                                 ('new.log', newest, '2026-01-03')]:
            dc.dream_ingest(self.root, self.write(name, date + 'T00:00:00Z [user] ' + text + '\n'))
        rows = dc.dream_next(self.root)['rows']
        fids = {f.split('.')[0]: f for f, _, _ in rows}
        candidates = []
        for first, second in [('old', 'middle'), ('middle', 'new')]:
            a, b = fids[first] + ':1', fids[second] + ':1'
            candidates.append(dict(category='superseded_directive', title=first + ' to ' + second,
                                   scope_tag='auth.py', src_spans=[a, b], superseded_by_src=b,
                                   edges=[dict(edge_type='SUPERSEDES', target='deploy', src_span=b, target_span=a)]))
        self.assertTrue(dc.dream_run(self.root, runner=lambda *a, **kw: json.dumps(candidates))['ok'])
        ids = [n['id'] for n in mb._load_queue(self.root)[0]['nodes']]
        if approve:
            self.assertTrue(mb.learn_approve(self.root, ids, approver='reviewer')['ok'])
        return ids, old, middle, newest

    def test_chained_cross_session_replacement_quarantines_matching_rule(self):
        ids, old, middle, newest = self.pair_history()
        self.assert_reader_agreement(self.root, 'auth.py')
        built = graph.build_graph(self.root)
        self.assertTrue(built['ok'], built)
        result = self.result(budget=4000)
        self.assertEqual([], result['groups']['governs'])
        self.assertEqual(3, len(result['groups']['replaced']))
        self.assertNotIn('[CURRENT] ' + middle, result['formatted'])
        self.assertIn('[CURRENT] ' + newest, result['formatted'])
        rules = [n for n in built['nodes'] if n['kind'] == 'rule' and n['text'] == old]
        self.assertEqual(1, len(rules))
        self.assertFalse(rules[0]['current'])
        self.assertTrue(rules[0]['replacedBy'])
        self.assertTrue(any(e['basis'] == 'replaced-rule-text' and e['dst'] == rules[0]['id'] for e in built['edges']))
        by_id = self.result(target=rules[0]['id'], budget=4000)
        self.assertEqual([], by_id['groups']['governs'])
        self.assertTrue(any(i['kind'] == 'lesson' for i in by_id['groups']['replaced']))
        by_lesson = self.result(target=ids[0], budget=4000)
        self.assertTrue(any(i['kind'] == 'rule' for i in by_lesson['groups']['replaced']))
        for item in result['groups']['replaced']:
            self.assertIn(item['why'], GRAPH_WHY)

    def test_budget_whole_items_order_zero_and_character_cap(self):
        repo, _ = self.fixture('approved')
        full = self.result(repo, 'deploy.py', budget=8000)
        wanted = copy.deepcopy(full['groups'])
        wanted['resolves'] = []
        omitted = {g: len(full['groups'][g]) - len(wanted[g]) for g in graph.GROUPS}
        boundary = mw.estimate_tokens(graph._format(wanted, omitted))
        small = self.result(repo, 'deploy.py', budget=boundary)
        self.assertTrue(small['groups']['replaced'])
        self.assertEqual([], small['groups']['resolves'])
        for group, values in small['groups'].items():
            self.assertEqual(len(full['groups'][group]) - len(values), small['omitted'][group])
            self.assertTrue(all(i in full['groups'][group] for i in values))
        zero = self.result(repo, 'deploy.py', budget=0)
        self.assertEqual('', zero['formatted'])
        self.assertEqual(8, sum(zero['omitted'].values()))
        self.write('auth.py', fixtures.CODE)
        self.write('AGENTS.md', '- ' + 'x' * 9000 + ' `auth.py::validate_token`.\n')
        mw.init_project(self.root)
        huge = self.result(budget=100000)
        self.assertEqual([], huge['groups']['governs'])
        self.assertEqual(1, huge['omitted']['governs'])

    def event(self, node, at, decision='approved', kind='node'):
        item_id = node['id' if kind == 'node' else 'edgeId']
        digest = md.node_approval_digest(node) if kind == 'node' else md.edge_approval_digest(node)
        with contextlib.closing(sqlite3.connect(str(self.root / mw.LEDGER_PATH))) as db:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS dream_decision_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, '
                           'at TEXT, item_id TEXT, kind TEXT, decision TEXT, by TEXT, digest TEXT, paths TEXT)')
                db.execute('INSERT INTO dream_decision_events (at,item_id,kind,decision,by,digest,paths) VALUES (?,?,?,?,?,?,?)',
                           (at, item_id, kind, decision, 'reviewer', digest, json.dumps(node.get('approvedPaths', []))))

    def test_as_of_pin_approve_supersede_reject_and_reapprove(self):
        ids, old, middle, newest = self.pair_history(approve=False)
        data = mb._load_queue(self.root)[0]
        first, second = sorted(data['nodes'], key=lambda n: n['primaryPin']['quoteText'])
        first = next(n for n in data['nodes'] if old in n['primaryPin']['quoteText'])
        second = next(n for n in data['nodes'] if middle in n['primaryPin']['quoteText'])
        config, _ = mw.read_config(self.root)
        config['pinnedAnchors']['auth.py::one']['pinnedAt'] = '2026-01-01T00:00:00Z'
        mw.write_managed_text(self.root, mw.CONFIG_PATH, json.dumps(config))
        for node, day in ((first, 2), (second, 3)):
            with mock.patch.object(md, '_utc_now_iso', return_value='2026-01-0%dT00:00:00Z' % day):
                self.assertTrue(mb.learn_approve(self.root, [node['id']], approver='reviewer')['ok'])
        with mock.patch.object(md, '_utc_now_iso', return_value='2026-01-04T00:00:00Z'):
            self.assertTrue(mb.learn_reject(self.root, [second['id']], approver='reviewer')['ok'])
        for day, count in ((1, 0), (2, 1), (3, 2), (4, 1)):
            with self.subTest(day=day):
                built = graph.build_graph(self.root, as_of='2026-01-0%dT12:00:00Z' % day)
                self.assertTrue(built['ok'], built)
                self.assertEqual(count, built['counts']['lessonItems'])
                result = self.result(as_of='2026-01-0%dT12:00:00Z' % day, budget=4000)
                self.assertEqual(day == 1, bool(result['groups']['governs']))
                if day == 2:
                    self.assertNotIn(newest, result['formatted'])
                    self.assertIn('[CURRENT] ' + middle, result['formatted'])
                if day == 3:
                    self.assertNotIn('[CURRENT] ' + middle, result['formatted'])
                    cited = next(n for n in built['nodes'] if n.get('recordType') == 'citation' and n['text'] == middle)
                    self.assertEqual('2026-01-02T00:00:00Z', cited['validFrom'])
                    self.assertEqual('2026-01-03T00:00:00Z', cited['validTo'])
        with mock.patch.object(md, '_utc_now_iso', return_value='2026-01-05T00:00:00Z'):
            self.assertTrue(mb.learn_approve(self.root, [second['id']], approver='reviewer', reconsider=True)['ok'])
        after = graph.build_graph(self.root, as_of='2026-01-05T12:00:00Z')
        self.assertEqual(2, after['counts']['lessonItems'])
        node = next(n for n in after['nodes'] if n['id'] == second['id'])
        self.assertEqual([{'validFrom': '2026-01-03T00:00:00Z', 'validTo': '2026-01-04T00:00:00Z'},
                          {'validFrom': '2026-01-05T00:00:00Z', 'validTo': None}], node['validity'])

    def test_real_path_decisions_replay_and_newest_log_recovery(self):
        node = self.time_fixture(approved=False)
        nid = node['id']
        self.write('human.py', '# untracked\n')
        self.write('second.py', '# untracked\n')
        def decide(day, fn, **kwargs):
            with mock.patch.object(md, '_utc_now_iso', return_value='2026-01-0%dT00:00:00Z' % day):
                result = fn(self.root, [nid], approver='Reviewer', **kwargs)
                self.assertTrue(result['ok'], result)
        decide(2, mb.learn_approve, paths=['human.py'])
        # The real CLI --paths dispatcher, with only its timestamp source injected.
        script = ("import sys,runpy; sys.path.insert(0,sys.argv[1]); "
                  "from memlet import memlet_dream; memlet_dream._utc_now_iso=lambda:'2026-01-03T00:00:00Z'; "
                  "sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')")
        cli = subprocess.run([self.python, '-c', script, str(fixtures.CLI.parent), str(fixtures.CLI),
                              'learn', '--paths', nid, '--by', 'Reviewer', '--path', 'second.py', '--repo', str(self.root)],
                             capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(0, cli.returncode, cli.stderr + cli.stdout)
        decide(4, mb.learn_reject)
        decide(5, mb.learn_approve, reconsider=True)
        events = mb.decision_events(self.root, nid)
        self.assertEqual(['approved', 'paths-changed', 'revoked', 'reconsidered'], [e['decision'] for e in events])
        for day, expected in ((1, None), (2, ['human.py', 'deploy.py']),
                              (3, ['second.py', 'deploy.py']), (4, None), (5, ['second.py', 'deploy.py'])):
            with self.subTest(day=day):
                items = self.graph_lessons(as_of='2026-01-0%dT12:00:00Z' % day)
                self.assertEqual(expected, items.get(nid, {}).get('paths'))
        self.assertEqual([{'validFrom': '2026-01-02T00:00:00Z', 'validTo': '2026-01-03T00:00:00Z'},
                          {'validFrom': '2026-01-03T00:00:00Z', 'validTo': '2026-01-04T00:00:00Z'},
                          {'validFrom': '2026-01-05T00:00:00Z', 'validTo': None}], self.graph_lessons()[nid]['validity'])
        # Flood only unrelated items. The bridge's global newest window now
        # excludes every real decision, but per-item histories remain complete.
        with contextlib.closing(sqlite3.connect(str(self.root / mw.LEDGER_PATH))) as db, db:
            db.executemany('INSERT INTO dream_decision_events(at,item_id,kind,decision,by,digest,paths) VALUES (?,?,?,?,?,?,?)',
                           [('2026-01-06T00:00:00Z', 'unrelated', 'node', 'rejected', 'Reviewer', '', '[]')] * 4100)
        self.assertTrue(mb.decision_events(self.root).truncated)
        with mock.patch.object(mb, 'decision_events', wraps=mb.decision_events) as reader:
            self.assertEqual(['human.py', 'deploy.py'], self.graph_lessons(as_of='2026-01-02T12:00:00Z')[nid]['paths'])
            self.assertIn(mock.call(self.root, item_id=nid), reader.call_args_list)
            self.assertIn(mock.call(self.root, item_id=node['edges'][0]['edgeId']), reader.call_args_list)

        snapshot_file = self.root / mb.SNAPSHOT_REL / 'review.log'
        snapshot_file.write_text(snapshot_file.read_text().replace('Never edit', 'Never change'))
        mb.learn_pending(self.root)
        decide(6, mb.learn_approve, refresh_citations=True)
        self.assertEqual('citations-refreshed', mb.decision_events(self.root, nid)[-1]['decision'])
        self.assertEqual({}, self.graph_lessons(as_of='2026-01-05T12:00:00Z'))
        refreshed = self.graph_lessons(as_of='2026-01-06T12:00:00Z')[nid]
        self.assertEqual('2026-01-06T00:00:00Z', refreshed['validFrom'])

    def test_timestamp_grammar_unknown_since_and_future_knowledge(self):
        self.anchors()
        current = graph.build_graph(self.root)
        written = next(e for e in current['edges'] if e['basis'] == 'written-anchor')
        self.assertIsNone(written['validFrom'])
        past = graph.build_graph(self.root, as_of='2000-01-01')
        self.assertEqual([], past['nodes'])
        for value in ('20260101', '2026-W01-1', '2026-01-01 00:00:00', '2026-01-01T00:00:00+0000', 'bad'):
            self.assertFalse(graph.build_graph(self.root, as_of=value)['ok'], value)
        for value in ('2026-01-01', '2026-01-01Z', '2026-01-01T00:00', '2026-01-01T00:00:00.123456789Z'):
            self.assertEqual(memlet_eval._stamp(value), memlet_eval._stamp(graph._at(value)))

    def test_current_source_bytes_required_in_historical_query(self):
        repo, nodes = self.fixture('approved', ('BLOCKS',))
        item = nodes['BLOCKS']
        since = item['approval']['at']
        self.assertEqual(1, graph.build_graph(repo, as_of=since)['counts']['lessonItems'])
        file = repo / mb.SNAPSHOT_REL / item['primaryPin']['fileId']
        file.write_text('changed evidence\n')
        self.assertEqual(0, graph.build_graph(repo, as_of=since)['counts']['lessonItems'])

    def test_retained_pin_without_rule_is_a_symbol_not_a_governing_edge(self):
        self.write('auth.py', fixtures.CODE)
        self.write('AGENTS.md', '- Keep `auth.py::validate_token`.\n')
        mw.init_project(self.root)
        self.write('AGENTS.md', '# Removed rule\n')
        result = graph.build_graph(self.root)
        self.assertEqual(0, result['counts']['nodes']['rule'])
        self.assertEqual(1, result['counts']['nodes']['symbol'])
        self.assertEqual([], result['edges'])

    def test_current_drift_is_not_undated_historical_knowledge(self):
        self.write('auth.py', fixtures.CODE)
        self.write('AGENTS.md', '- Keep `auth.py::validate_token`.\n')
        mw.init_project(self.root)
        config, _ = mw.read_config(self.root)
        config['pinnedAnchors']['auth.py::validate_token']['pinnedAt'] = '2026-01-01T00:00:00Z'
        mw.write_managed_text(self.root, mw.CONFIG_PATH, json.dumps(config))
        self.assertTrue(self.result(as_of='2026-01-02')['groups']['governs'])
        self.write('auth.py', fixtures.CODE.replace('(token)', '(token, issuer)'))
        self.assertTrue(self.result()['groups']['stale'])
        historical = self.result(as_of='2026-01-02')
        self.assertFalse(any(historical['groups'].values()))

    def test_accepted_anchor_drop_has_no_fabricated_history(self):
        sid = self.anchors()
        before = graph.build_graph(self.root)
        accepted = next(e for e in before['edges'] if e['basis'] == 'accepted-suggestion')
        self.assertIsNotNone(accepted['validFrom'])
        self.assertTrue(ma.drop_sidecar_anchors(self.root, [sid])['ok'])
        after = graph.build_graph(self.root)
        self.assertFalse(any(e['basis'] == 'accepted-suggestion' for e in after['edges']))
        self.assertIn('deleted sidecar payloads', ' '.join(after['notices']))

    def test_literal_attachment_non_git_directory(self):
        folder = self.base / 'nongit'
        folder.mkdir()
        (folder / 'auth.py').write_text('# fixture\n')
        context = mb.lesson_path_context(folder)
        self.assertFalse(context['is_git'])
        self.assertEqual(['auth.py'], mb.literal_lesson_paths(folder, '', 'auth.py', context=context))
        self.assertEqual([], mb.literal_lesson_paths(folder, '', 'AUTH.py', context=context))

    def test_shared_attachment_context_and_untracked_human_path(self):
        repo, nodes = self.fixture('approved', ('BLOCKS',))
        (repo / 'human.py').write_text('# deliberately untracked\n')
        nid = nodes['BLOCKS']['id']
        self.assertTrue(mb.learn_paths(repo, [nid], approver='Reviewer', paths=['human.py'])['ok'])
        contexts = []
        real = mb.lesson_path_context
        def capture(*args, **kwargs):
            context = real(*args, **kwargs)
            contexts.append(context)
            return context
        with mock.patch.object(mb, 'lesson_path_context', side_effect=capture), \
             mock.patch.object(mb, 'lesson_paths', wraps=mb.lesson_paths) as human, \
             mock.patch.object(mb, '_load_snapshot', wraps=mb._load_snapshot) as snapshot, \
             mock.patch.object(mb, '_load_queue', wraps=mb._load_queue) as queue, \
             mock.patch.object(mb, 'dream_tiers', wraps=mb.dream_tiers) as tiers:
            counters = {}
            result = self.result(repo, 'human.py', _counters=counters)
            self.assertEqual('lesson-path', result['groups']['blocks'][0]['why'])
            self.assertEqual(1, len(contexts))
            self.assertTrue(all(call.kwargs['context'] is contexts[0] for call in human.call_args_list))
            self.assertEqual((1, 1, 1), (snapshot.call_count, queue.call_count, tiers.call_count))
            self.assertEqual((1, 1, 1, 1), tuple(counters[k] for k in ('snapshotReads', 'queueReads', 'tierPasses', 'gitCalls')))
        self.assertEqual(['human.py', 'deploy.py'], mb.dream_items(repo)['items'][0]['paths'])
        self.assert_reader_agreement(repo, 'human.py')

    def test_hostile_rule_bounds_and_hidden_characters(self):
        self.write('auth.py', fixtures.CODE)
        self.write('AGENTS.md', '- hidden\u200b `auth.py::validate_token`.\n' +
                   '- long ' + 'x' * 20000 + ' `auth.py::validate_token`.\n')
        result = graph.build_graph(self.root)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['truncated'])
        self.assertEqual([], result['nodes'])
        self.write('AGENTS.md', ('- x\n' * 100000))
        counters = {}
        result = graph.build_graph(self.root, _counters=counters)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(result['counts']['nodes']['rule'], mw._MAX_RULES_PER_FILE)
        self.assertEqual(0, counters['anchorsClassified'])
        self.assertEqual(0, counters['lessonCandidatesValidated'])
        self.assertEqual(1, counters['gitCalls'])
        self.write('AGENTS.md', 'x' * (mw._MAX_SOURCE_BYTES + 1))
        result = graph.build_graph(self.root)
        self.assertTrue(result['truncated'])
        self.assertEqual([], result['nodes'])

    def test_cli_shapes_exit_codes_markdown_and_plugin_group(self):
        self.anchors()
        for target in ('auth.py', 'auth.py::validate_token'):
            cli = self.cli('admin', 'graph', 'show', target, '--repo', str(self.root))
            self.assertEqual(0, cli.returncode, cli.stderr)
            result = json.loads(cli.stdout)
            self.assertEqual(set(graph.GROUPS), set(result['groups']))
            markdown = self.cli('admin', 'graph', 'show', target, '--repo', str(self.root), '--format', 'markdown')
            self.assertEqual(0, markdown.returncode, markdown.stderr)
            self.assertEqual(result['formatted'], markdown.stdout)
        stats = self.cli('admin', 'graph', 'stats', '--repo', str(self.root))
        self.assertEqual(0, stats.returncode, stats.stderr)
        self.assertEqual(2, json.loads(stats.stdout)['counts']['nodes']['rule'])
        bad = self.cli('admin', 'graph', 'show', '../outside')
        self.assertEqual(2, bad.returncode)
        self.assertFalse(json.loads(bad.stdout)['ok'])
        help_text = self.cli('admin', 'graph', 'show', '--help')
        for flag in ('--repo', '--tiers', '--as-of', '--budget', '--format'):
            self.assertIn(flag, help_text.stdout)
        self.assertEqual('Rules/wiki', cli_plugins.commands(cli_plugins.load())['graph']['group'])

    def test_no_model_or_write_and_mcp_definitions_unchanged(self):
        self.anchors()
        before = {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.root.rglob('*') if p.is_file() and '.git' not in p.parts}
        definitions = json.dumps(memlet_mcp.mcp_tools(), sort_keys=True)
        with mock.patch.object(vendor_cli, 'run_vendor', side_effect=AssertionError('model forbidden')):
            graph.build_graph(self.root)
            self.result()
        after = {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.root.rglob('*') if p.is_file() and '.git' not in p.parts}
        self.assertEqual(before, after)
        self.assertEqual(definitions, json.dumps(memlet_mcp.mcp_tools(), sort_keys=True))

    def test_large_repository_file_slice_cost_and_classifier_bound(self):
        # 5,000 real code files, 2,000 rules, 2,000 sealed approved lessons.
        # Build directly from deterministic fixtures: no vendor/runner involved.
        for number in range(5000):
            self.write('src/f%04d.py' % number, 'def run():\n    return 1\n')
        for number in range(10):
            self.write('rules%02d/AGENTS.md' % number, '\n'.join(
                '- Keep `src/f%04d.py::run` checked.' % i for i in range(number * 200, (number + 1) * 200)) + '\n')
        self.tracked('src')
        snapshot = ['2026-01-01T00:00:00Z [user] Keep src/f%04d.py checked.' % i for i in range(2000)]
        self.write(mb.SNAPSHOT_REL + '/large.log', '\n'.join(snapshot) + '\n')
        nodes, edges = [], []
        for number in range(2000):
            pin = md.resolve_pin({'large.log': snapshot}, 'large.log:%d' % (number + 1))
            edge = dict(edgeId='E%d' % number, edgeType='BLOCKS', fromId='DRM-%d' % number,
                        toId='BLOCKS:%d' % number, targetLabel='cold cache', srcPin=pin, edgeVerdict='VALID')
            node = dict(id='DRM-%d' % number, title='fixture', category='repeated_pitfall',
                        statusKind='open_trap', scopeTag='src/f%04d.py' % number,
                        primaryPin=pin, allPins=[pin], edges=[edge], verdict='SUPPORTED')
            for obj, kind in ((edge, 'edge'), (node, 'node')):
                obj['approval'] = dict(schema=md.APPROVAL_RECORD_SCHEMA, decision='approved', by='fixture',
                                       at='2026-01-02T00:00:00Z', digest=md.edge_approval_digest(obj)
                                       if kind == 'edge' else md.node_approval_digest(obj))
            nodes.append(node); edges.append(edge)
        self.write(mb.QUEUE_REL, json.dumps(dict(schema=md.DREAM_ONTOLOGY_SCHEMA, nodes=nodes, edges=edges)))
        real = mw.classify_anchor
        times = []
        with mock.patch.object(graph, 'build_graph', side_effect=AssertionError('whole graph forbidden')), \
             mock.patch.object(mw, 'classify_anchor', wraps=real) as classifier:
            for _ in range(2):
                start = time.perf_counter()
                counters = {}
                result = self.result(target='src/f0001.py', budget=1000, _counters=counters)
                times.append(time.perf_counter() - start)
                self.assertEqual(1, len(result['groups']['blocks']))
                self.assertEqual(1, len(result['groups']['stale']))
                self.assertEqual(1, counters['anchorsClassified'])
                self.assertEqual(1, counters['lessonCandidatesValidated'])
                self.assertEqual(1, counters['gitCalls'])
                self.assertTrue(all(n <= 1 for n in counters['directoryListings'].values()))
                self.assertLessEqual(counters['rebindingFilesParsed'], 1)
            self.assertEqual(2, classifier.call_count)
        print('E1 synthetic 5000 files / 2000 rules / 2000 lessons: %.3fs cold, %.3fs repeated' % tuple(times))


    @staticmethod
    def seal(obj, kind='node', at='2026-01-02T00:00:00Z'):
        obj['approval'] = dict(schema=md.APPROVAL_RECORD_SCHEMA, decision='approved', by='reviewer', at=at,
                               digest=md.node_approval_digest(obj) if kind == 'node' else md.edge_approval_digest(obj))
        return obj

    def queue_node(self, ident, pin, scope='general', approved=True):
        edge = dict(edgeId='E-' + ident, edgeType='BLOCKS', fromId=ident, toId='BLOCKS:' + ident,
                    targetLabel='fixture', srcPin=pin, edgeVerdict='VALID')
        node = dict(id=ident, title='fixture', category='repeated_pitfall', statusKind='open_trap',
                    scopeTag=scope, primaryPin=pin, allPins=[pin], edges=[edge], verdict='SUPPORTED')
        if approved:
            self.seal(edge, 'edge'); self.seal(node)
        return node

    def queue_fixture(self, lines, build, *, database=True):
        self.write(mb.SNAPSHOT_REL + '/review.log', '\n'.join(lines) + '\n')
        pin = lambda n: md.resolve_pin({'review.log': lines}, 'review.log:%d' % n)
        nodes, top = build(pin)
        data = dict(schema=md.DREAM_ONTOLOGY_SCHEMA, nodes=nodes,
                    edges=[copy.deepcopy(e) for n in nodes for e in n['edges']] + top)
        if database:
            self.save_graph(self.root, data)
        else:
            # Cost fixtures exercise the real JSON queue reader. An ontology DB
            # is not a graph authority; avoid paying to populate unused rows.
            self.write(mb.QUEUE_REL, json.dumps(data))
        return nodes

    def time_fixture(self, approved=True):
        self.write('deploy.py', 'x = 1\n')
        self.write('AGENTS.md', '# Rules\n')
        mw.init_project(self.root); self.tracked('deploy.py', 'AGENTS.md')
        return self.queue_fixture(['2026-01-01T00:00:00Z [user] Never edit deploy.py by hand.'],
            lambda pin: ([self.queue_node('DRM-A', pin(1), 'deploy.py', approved)], []))[0]

    def graph_lessons(self, **kwargs):
        result = graph.build_graph(self.root, **kwargs)
        self.assertTrue(result['ok'], result)
        if kwargs.get('as_of'):
            self.assertTrue(result['historical'])
            self.assertIn(graph.GRAPH_HISTORY_NOTICE, result['notices'])
        return {n['id']: n for n in result['nodes'] if n.get('recordType') == 'item'}

    def test_review_unlogged_current_rejection_cannot_be_resurrected(self):
        node = self.time_fixture()
        self.event(node, '2026-01-02T00:00:00Z')
        for e in node['edges']:
            self.event(e, '2026-01-02T00:00:00Z', kind='edge')
        self.assertTrue(mb.learn_reject(self.root, [node['id']], approver='reviewer')['ok'])
        record = mb._load_queue(self.root)[0]['nodes'][0]['approval']
        for at in (record['at'], '2099-01-01'):
            self.assertEqual({}, self.graph_lessons(as_of=at))
            result = self.result(target='deploy.py', as_of=at)
            self.assertTrue(result['historical'])
            self.assertFalse(any(result['groups'].values()))
        self.assertEqual({}, self.graph_lessons())
        self.assertIn(node['id'], self.graph_lessons(as_of='2026-06-01'))

    def test_review_current_record_wins_with_or_without_events(self):
        node = self.time_fixture()
        for logged in (False, True):
            if logged:
                self.event(node, '2026-02-01T00:00:00Z')
                self.event(node, '2026-04-01T00:00:00Z')
                for e in node['edges']:
                    self.event(e, '2026-02-01T00:00:00Z', kind='edge')
            for decision in ('rejected', 'revoked', 'pending'):
                data = mb._load_queue(self.root)[0]
                data['nodes'][0]['approval'].update(decision=decision, at='2026-03-01T00:00:00Z')
                self.save_graph(self.root, data)
                for at in ('2026-03-01', '2026-05-01', '2099-01-01'):
                    self.assertEqual({}, self.graph_lessons(as_of=at), (logged, decision, at))

    def test_review_forged_events_cannot_raise_present_tier_m6b(self):
        node = self.time_fixture(approved=False)
        for e in node['edges']:
            self.event(e, '2026-02-01T00:00:00Z', kind='edge')
        self.event(node, '2026-02-01T00:00:00Z')
        self.assertEqual({}, self.graph_lessons())
        self.assertFalse(any(self.result(target='deploy.py')['groups'].values()))
        # A matching local event can describe the past, but is explicitly
        # unauthenticated and cannot be passed to a hook as a present result.
        self.assertEqual('approved', self.graph_lessons(as_of='2026-06-01')[node['id']]['tier'])
        result = self.result(target='deploy.py', as_of='2026-06-01')
        self.assertTrue(result['historical'])
        self.assertIn(graph.GRAPH_HISTORY_NOTICE, result['notices'])
        data = mb._load_queue(self.root)[0]
        data['nodes'] = []; data['edges'] = []
        self.save_graph(self.root, data)
        self.assertEqual({}, self.graph_lessons(as_of='2026-06-01'))

    def test_review_event_digest_must_match_recomputable_content(self):
        node = self.time_fixture(approved=False)
        for e in node['edges']:
            self.event(e, '2026-02-01T00:00:00Z', kind='edge')
        self.event(node, '2026-02-01T00:00:00Z')
        with contextlib.closing(sqlite3.connect(str(self.root / mw.LEDGER_PATH))) as db, db:
            db.execute("UPDATE dream_decision_events SET digest=? WHERE kind='node'", ('sha256:' + '0' * 64,))
        self.assertEqual({}, self.graph_lessons(as_of='2026-06-01'))
        data = mb._load_queue(self.root)[0]
        self.seal(data['nodes'][0])
        for e in data['nodes'][0]['edges'] + data['edges']:
            self.seal(e, 'edge')
        self.save_graph(self.root, data)
        item = self.graph_lessons()[node['id']]
        self.assertEqual('2026-01-02T00:00:00Z', item['validFrom'])
        self.assertEqual([{'validFrom': item['validFrom'], 'validTo': None}], item['validity'])

    def test_review_sequence_time_conflict_is_unknown_and_ties_use_seq(self):
        from memlet import vocabulary as v
        node = self.time_fixture(approved=False)
        self.event(node, '2026-03-01T00:00:00Z', 'rejected')
        self.event(node, '2026-02-01T00:00:00Z')
        for e in node['edges']:
            self.event(e, '2026-02-01T00:00:00Z', kind='edge')
        self.assertEqual({}, self.graph_lessons(as_of='2026-04-01'))
        self.assertIn(v.GRAPH_ORDER_NOTICE, graph.build_graph(self.root, as_of='2026-04-01')['notices'])
        with contextlib.closing(sqlite3.connect(str(self.root / mw.LEDGER_PATH))) as db, db:
            db.execute('DELETE FROM dream_decision_events')
        self.event(node, '2026-02-01T00:00:00.5Z')
        self.event(node, '2026-02-01T00:00:00.500000Z', 'rejected')
        for e in node['edges']:
            self.event(e, '2026-02-01T00:00:00Z', kind='edge')
        result = graph.build_graph(self.root, as_of='2026-04-01')
        self.assertEqual(0, result['counts']['lessonItems'])
        self.assertNotIn(v.GRAPH_ORDER_NOTICE, result['notices'])

    def test_review_fractional_offset_invalid_and_nullable_event_times(self):
        node = self.time_fixture(approved=False)
        for at, expected in (('2026-02-01T01:00:00+02:00', '2026-01-31T23:00:00Z'),
                             ('2026-02-01T00:00:00.5Z', '2026-02-01T00:00:00.500000Z'),
                             ('garbage', None), ('', None)):
            self.event(node, at)
            for e in node['edges']:
                self.event(e, at, kind='edge')
            items = self.graph_lessons(as_of='2026-03-01')
            self.assertEqual(expected, items.get(node['id'], {}).get('validFrom'))
            if expected:
                before = graph._stamp(expected) - __import__('datetime').timedelta(microseconds=1)
                self.assertEqual({}, self.graph_lessons(as_of=before.isoformat()))
            with contextlib.closing(sqlite3.connect(str(self.root / mw.LEDGER_PATH))) as db, db:
                db.execute('UPDATE dream_decision_events SET paths=NULL')
            result = graph.build_graph(self.root, as_of='2026-03-01')
            self.assertEqual(0, result['counts']['lessonItems'])
            self.assertIn(graph.v.GRAPH_CORRUPT_HISTORY_NOTICE, result['notices'])
            with contextlib.closing(sqlite3.connect(str(self.root / mw.LEDGER_PATH))) as db, db:
                db.execute('DELETE FROM dream_decision_events')

    def test_review_future_events_do_not_date_present_and_corrupt_ledger_is_local(self):
        from memlet import vocabulary as v
        node = self.time_fixture()
        self.event(node, '2099-01-01T00:00:00Z')
        built = graph.build_graph(self.root)
        self.assertIn(v.GRAPH_FUTURE_NOTICE, built['notices'])
        item = self.graph_lessons()[node['id']]
        self.assertEqual('2026-01-02T00:00:00Z', item['validFrom'])
        self.assertFalse(any(x['validFrom'].startswith('2099') for x in item['validity']))
        (self.root / mw.LEDGER_PATH).write_bytes(b'not a database' * 100)
        self.assertEqual('approved', self.graph_lessons()[node['id']]['tier'])
        self.assertIn(v.GRAPH_CORRUPT_HISTORY_NOTICE, self.result(target='deploy.py')['notices'])

    def test_review_top_level_and_inline_supersession_agreement(self):
        self.write('deploy.py', 'def main():\n    return 1\n')
        old = 'Always deploy through `deploy.py::main` from the main branch.'
        self.write('AGENTS.md', '- ' + old + '\n')
        mw.init_project(self.root); self.tracked('deploy.py', 'AGENTS.md')
        # X is replaced through a top-level-only edge unrelated by id to Z;
        # Z repeats exactly X's quote. Include both approved hand-edited shapes.
        lines = ['2026-01-01T00:00:00Z [user] ' + old,
                 '2026-01-02T00:00:00Z [user] From now on use the checked release wrapper.',
                 '2026-01-03T00:00:00Z [user] ' + old]
        for inline in (False, True):
            def build(pin):
                nodes = [self.queue_node('DRM-X', pin(1)), self.queue_node('DRM-Y', pin(2)),
                         self.queue_node('DRM-Z', pin(3), 'deploy.py')]
                edge = dict(edgeId='E-SUP', edgeType='SUPERSEDES', fromId='DRM-Y', toId='DRM-X',
                            targetLabel='old', srcPin=pin(2), targetPin=pin(1), edgeVerdict='VALID')
                self.seal(edge, 'edge')
                if inline:
                    nodes[1]['edges'].append(edge)
                return nodes, [edge] if not inline else []
            self.queue_fixture(lines, build)
            read = {n['id']: n for n in mb.dream_items(self.root)['items']}
            built = self.graph_lessons()
            self.assertTrue(read['DRM-Z']['quarantined'])
            self.assertIsNotNone(built['DRM-Z']['validTo'])
            for target in ('deploy.py', 'DRM-Z'):
                result = self.result(target=target, budget=8000)
                self.assertNotIn('DRM-Z', [n['id'] for n in result['groups']['blocks']])
                if target == 'DRM-Z':
                    z = next(n for n in result['groups']['replaced'] if n['id'] == 'DRM-Z')
                    self.assertEqual(read['DRM-Z']['lineRoles'], z['lineRoles'])
                    self.assertEqual(read['DRM-Z']['cites'], z['cites'])
                # Under the scope amendment a fully replaced citation cannot
                # attach Z to a file; its ID query still shares the global role.
            result = self.result(target='deploy.py', budget=8000)
            self.assertTrue(any(n['kind'] == 'rule' for n in result['groups']['replaced']))
            self.assertFalse(result['groups']['governs'])

    def test_review_rejected_quote_twins_agree_in_id_slice_m18(self):
        repo, nodes = self.fixture('hint', ('BLOCKS',))
        original = mb._load_queue(repo)[0]
        for shape in ('approval', 'verdict', 'edge-verdict'):
            data = copy.deepcopy(original)
            h = data['nodes'][0]
            twin = copy.deepcopy(h)
            twin.update(id='DRM-twin', proposers=[], verification=[])
            for e in twin['edges']:
                e.update(edgeId='E-twin', fromId='DRM-twin')
                e.pop('approval', None)
            if shape == 'approval':
                twin['approval'] = dict(decision='rejected')
            elif shape == 'verdict':
                twin['verdict'] = 'REJECTED'
            else:
                twin['edges'][0]['edgeVerdict'] = 'REJECTED'
            data['nodes'].append(twin); data['edges'].extend(copy.deepcopy(twin['edges']))
            self.save_graph(repo, data)
            read = {n['id']: n for n in mb.dream_items(repo, tiers=('approved', 'hint', 'pending'))['items']}
            self.assertEqual('pending', read[h['id']]['tier'], shape)
            self.assertFalse(graph.neighbors(repo, h['id'], tiers=('approved', 'hint'))['ok'], shape)
            self.assertFalse(any(self.result(repo, 'deploy.py', tiers=('approved', 'hint'))['groups'].values()))
            self.assertEqual(0, graph.build_graph(repo)['counts']['lessonItems'])
            self.assert_reader_agreement(repo, 'deploy.py')

    def test_review_literal_adjacency_and_attachment_table(self):
        self.time_fixture()
        for path in ('src/deploy.py', 'mydeploy.py', 'ignored/out.py', 'café.py', 'new.py'):
            self.write(path, 'x = 1\n')
        self.write('.gitignore', 'ignored/\n')
        self.tracked('src/deploy.py', 'mydeploy.py', 'café.py')
        subprocess.run(['git', 'add', '-f', 'ignored/out.py'], cwd=self.root, check=True, capture_output=True)
        self.write('ignored/untracked.py', 'x\n')
        (self.root / 'link.py').symlink_to(self.root / 'deploy.py')
        (self.root / 'dirlink').symlink_to(self.root / 'src', target_is_directory=True)
        cases = [('plain', 'Never edit deploy.py by hand', 'general', ['deploy.py']),
                 ('backticks', 'Never edit `deploy.py`.', 'general', ['deploy.py']),
                 ('substring', 'Never edit mydeploy.pyc or xdeploy.py', 'general', []),
                 ('directory', 'Never edit src/deploy.py', 'general', ['src/deploy.py']),
                 ('case', 'Never edit DEPLOY.py', 'general', []),
                 ('symlink', 'Never edit link.py', 'general', []),
                 ('dirlink', 'Never edit dirlink/deploy.py', 'general', []),
                 ('dotdot', 'Never edit ../repo/deploy.py or src/../deploy.py', 'general', []),
                 ('absolute', 'Never edit ' + str(self.root / 'deploy.py'), 'general', []),
                 ('absolute scope', 'Never edit it', str(self.root / 'deploy.py'), []),
                 ('untracked', 'Never edit new.py', 'general', []),
                 ('ignored', 'Never edit ignored/untracked.py', 'general', []),
                 ('tracked ignored', 'Never edit ignored/out.py', 'general', ['ignored/out.py']),
                 ('scope', 'Never edit it', 'deploy.py', []),
                 ('scope case', 'Never edit it', 'DEPLOY.py', []),
                 ('scope dot', 'Never edit it', './deploy.py', []),
                 ('scope symbol', 'Never edit it', 'deploy.py::main', []),
                 ('scope directory', 'Never edit it', 'src/', []),
                 ('title only', 'Never edit it', 'general', []),
                 ('NFD', 'Never edit cafe\u0301.py', 'general', []),
                 ('NBSP', 'Never edit deploy.py\u00a0by hand', 'general', ['deploy.py']),
                 ('line', 'See deploy.py:12 for the bug', 'general', [])]
        for ch in ('\x07', '\x1b', '\u200b', '\U000E0041', '\ufe0f', '\u0301', '\u20dd'):
            for before in (True, False):
                cases.append((repr(ch), 'Never edit ' + (ch + 'deploy.py' if before else 'deploy.py' + ch) + ' by hand', 'general', []))
        for name, quote, scope, expected in cases:
            with self.subTest(case=name, quote=quote):
                def build(pin):
                    n = self.queue_node('DRM-A', pin(1), scope)
                    n['title'] = 'deploy.py'; self.seal(n)
                    return [n], []
                self.queue_fixture(['2026-01-01T00:00:00Z [user] ' + quote], build)
                self.assertEqual(expected, self.graph_lessons()['DRM-A']['paths'])

    def test_review_pending_edge_pins_cannot_attach_m22(self):
        self.time_fixture()
        lines = ['2026-01-01T00:00:00Z [user] Never use the cold cache.',
                 '2026-01-02T00:00:00Z [assistant] The fix is in deploy.py for now.']
        def build(pin):
            n = self.queue_node('DRM-A', pin(1))
            n['timestamp'] = md.extract_line_timestamp(pin(1)['quoteText'])
            n['edges'][0]['srcTimestamp'] = n['timestamp']
            n['edges'].append(dict(edgeId='E-P', edgeType='RESOLVES', fromId='DRM-A', toId='RESOLVES:x',
                                   targetLabel='deploy.py', srcPin=pin(2), srcTimestamp=md.extract_line_timestamp(pin(2)['quoteText']), edgeVerdict='PENDING'))
            return [n], []
        self.queue_fixture(lines, build)
        self.assertEqual([], self.graph_lessons()['DRM-A']['paths'])
        self.assertFalse(any(self.result(target='deploy.py')['groups'].values()))
        self.assertTrue(mb.learn_approve(self.root, ['E-P'], approver='Reviewer')['ok'])
        result = self.result(target='deploy.py')
        self.assertEqual(['DRM-A'], [i['id'] for i in result['groups']['blocks']])
        self.assert_reader_agreement(self.root, 'deploy.py')

    def test_file_slice_keeps_literal_at_start_of_another_cited_line(self):
        self.write('deploy.py', 'x = 1\n'); self.tracked('deploy.py')
        def build(pin):
            node = self.queue_node('DRM-line-start', pin(1))
            node['allPins'].append(pin(2)); self.seal(node)
            return [node], []
        self.queue_fixture(['Never use the cold cache.', 'deploy.py requires review.'], build)
        self.assertEqual(['deploy.py'], mb.dream_items(self.root, files=['deploy.py'])['items'][0]['paths'])
        self.assertEqual(['DRM-line-start'], [i['id'] for i in self.result(target='deploy.py')['groups']['blocks']])
        self.assert_reader_agreement(self.root, 'deploy.py')

    def test_hint_selection_uses_current_lines_after_global_replacement(self):
        self.pair_history()
        data = mb._load_queue(self.root)[0]
        old_pin = next(n['primaryPin'] for n in data['nodes'] if 'warmed cache' in n['primaryPin']['quoteText'])
        hint = self.queue_node('DRM-hint-replaced', old_pin, approved=False)
        hint['timestamp'] = md.extract_line_timestamp(old_pin['quoteText'])
        hint['edges'][0]['srcTimestamp'] = hint['timestamp']
        hint['proposers'] = [dc._receipt(vendor, hint, 'f' * 24, {'model': 'haiku' if vendor == 'claude' else 'codex'})
                             for vendor in ('claude', 'codex')]
        data['nodes'].append(hint); data['edges'].extend(copy.deepcopy(hint['edges']))
        self.save_graph(self.root, data)
        item = next(i for i in mb.dream_items(self.root, tiers=('hint',))['items'] if i['id'] == hint['id'])
        self.assertEqual(['replaced'], item['lineRoles'])
        self.assertEqual([], mb.dream_items(self.root, tiers=('hint',), files=['auth.py'])['items'])
        self.assertEqual([], mb.unified_recall(self.root, files=['auth.py'])['sections']['hints'])
        self.assertEqual([], self.result(target='auth.py', tiers=('approved', 'hint'))['groups']['hints'])

    def test_review_hint_scope_does_not_attach_m23_and_id_why(self):
        repo, nodes = self.fixture('hint', ('BLOCKS',), cited_path=False)
        node = nodes['BLOCKS']
        self.assertEqual('deploy.py', node['scopeTag'])
        self.assertEqual('hint', mb.dream_items(repo, tiers=('hint',))['items'][0]['tier'])
        self.assertFalse(any(self.result(repo, 'deploy.py', tiers=('approved', 'hint'))['groups'].values()))
        self.assertEqual([], next(n for n in graph.build_graph(repo)['nodes'] if n.get('id') == node['id'])['paths'])
        own = self.result(repo, node['id'], tiers=('approved', 'hint'))['groups']['hints'][0]
        self.assertEqual('target-item', own['why'])
        self.assertEqual([], mb.dream_items(repo, tiers=('hint',), files=['deploy.py'])['items'])
        self.assertEqual([], mb.unified_recall(repo, files=['deploy.py'])['sections']['hints'])
        self.assert_reader_agreement(repo, 'deploy.py')
        for path in (hb.HANDOVER_FILE, hb.CAPSULES_FILE):
            self.assertNotIn(node['id'], (repo / path).read_text())


    def test_review_alias_ancestor_exact_case_and_target_safety(self):
        self.anchors()
        alias = self.base / 'project-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        for repo in (self.root, alias):
            got = self.result(repo, str(alias / 'auth.py'))
            self.assertEqual(self.result()['resolved'], got['resolved'])
            self.assertEqual(2, len(got['groups']['governs']))
        self.write('src/sub/x.py', 'x\n')
        for target in ('AUTH.py', 'src/SUB/x.py', str(alias / 'AUTH.py')):
            result = graph.neighbors(self.root, target)
            self.assertFalse(result['ok'], target)
            self.assertIn('spelling', result['reason'])
        (self.root / 'leaf.py').symlink_to(self.root / 'auth.py')
        for target in (str(alias / 'leaf.py'), 'src', 'src/sub', 'nosuch.py', '\x00auth.py',
                       'auth.py::', 'auth.py::x::y', 'b' * 2000, str(self.base / 'outside')):
            result = graph.neighbors(self.root, target)
            self.assertEqual({'ok', 'reason'}, set(result))
            self.assertFalse(result['ok'])
            self.assertNotIn(str(self.base), result['reason'])

    def test_review_hidden_rule_exclusion_explained_and_multirelation_rendered(self):
        from memlet import vocabulary as v
        self.write('auth.py', fixtures.CODE)
        self.write('AGENTS.md', '- Never\u200b bypass `auth.py::validate_token`.\n')
        mw.init_project(self.root)
        hand = mw.build_handover(self.root, budget=4000)
        self.assertTrue(hand['activeConstraints'])
        result = self.result()
        self.assertFalse(any(result['groups'].values()))
        self.assertIn(v.GRAPH_RULE_EXCLUSION_NOTICE, result['notices'])
        self.assertNotIn(v.GRAPH_RULE_BOUND_NOTICE, result['notices'])
        repo, nodes = self.fixture('hint', ('BLOCKS',))
        data = mb._load_queue(repo)[0]
        n = data['nodes'][0]
        e = copy.deepcopy(n['edges'][0]); e.update(edgeId='E-SECOND', edgeType='RESOLVES', toId='RESOLVES:x')
        n['edges'].append(e); data['edges'].append(copy.deepcopy(e))
        n['proposers'] = [dc._receipt(vendor, n, 'f' * 24, {'model': 'haiku' if vendor == 'claude' else 'codex'})
                          for vendor in ('claude', 'codex')]
        self.save_graph(repo, data)
        result = self.result(repo, n['id'], tiers=('approved', 'hint'), budget=2000)
        self.assertIn('BLOCKS', result['formatted'])
        self.assertIn('RESOLVES', result['formatted'])

    def test_review_path_exists_heading_and_rule_in_other_file(self):
        self.write('auth.py', fixtures.CODE)
        self.write('other.py', 'def run():\n    return 1\n')
        self.write('AGENTS.md', '- Only path `auth.py`.\n- Never touch auth.py; keep `other.py::run` stable.\n'
                   '- Both `auth.py::validate_token` and `auth.py`.\n- Missing `auth.py::nosuch`.\n')
        mw.init_project(self.root)
        result = self.result(budget=4000)
        self.assertIn('not verified against code', result['formatted'])
        self.assertNotIn('requiring re-verification', result['formatted'])
        self.assertFalse(result['groups']['governs'])
        self.assertEqual(['path-exists', 'path-exists', 'stale'], sorted(i['status'] for i in result['groups']['stale']))
        self.assertEqual(1, len(self.result(target='other.py')['groups']['governs']))
        self.assertEqual(['stale'], [i['status'] for i in self.result(target='auth.py::nosuch')['groups']['stale']])

    def test_review_chain_validto_and_detached_anchor(self):
        self.pair_history()
        built = graph.build_graph(self.root)
        old = [n for n in built['nodes'] if n.get('quarantined')]
        self.assertTrue(old)
        self.assertTrue(all(n['validTo'] for n in old))
        self.anchors()
        self.write('.memlet/wiki/auth.md', '# Authentication\n- ACCEPTED changed `validate_token()` in `auth.py`.\n')
        result = self.result()
        self.assertFalse(any(n.get('suggested') for n in result['groups']['governs']))

    def test_review_feature_bounds_do_not_refuse_repository(self):
        from memlet import vocabulary as v
        node = self.time_fixture()
        self.write('AGENTS.md', '- Keep `deploy.py`.\n')
        self.event(node, '2026-01-02T00:00:00Z')
        self.event(node, '2026-01-03T00:00:00Z')
        with mock.patch.object(graph, 'MAX_EVENTS', 1):
            result = self.result(target='deploy.py')
            self.assertTrue(result['groups']['blocks'])
            self.assertTrue(result['groups']['stale'])
            self.assertIn(v.GRAPH_EVENTS_NOTICE, result['notices'])
        real_read = mb.os.read
        def oversized(fd, size):
            # Popen also reads its exec-error pipe; only replace git stdout.
            return b'x' * (4 * 1024 * 1024 + 1) if size == 65536 else real_read(fd, size)
        with mock.patch.object(mb.os, 'read', side_effect=oversized):
            result = self.result(target='deploy.py')
            self.assertTrue(result['groups']['stale'])
            self.assertFalse(result['groups']['blocks'])
            self.assertIn(v.GRAPH_FILES_NOTICE, result['notices'])
            self.assertIn('blocks', result['incompleteGroups'])
        with mock.patch.object(mb.subprocess, 'Popen', side_effect=subprocess.TimeoutExpired('git', .5)):
            result = self.result(target='deploy.py')
            self.assertIn(v.GRAPH_FILES_NOTICE, result['notices'])

    def test_review_supersession_bound_keeps_code_classification(self):
        from memlet import vocabulary as v
        self.pair_history()
        with mock.patch.object(graph, 'MAX_SUPERSESSION_SLICE', 1):
            result = self.result(target='auth.py')
            self.assertFalse(result['groups']['governs'])
            self.assertTrue(result['groups']['stale'])
            self.assertIn(v.GRAPH_SUPERSESSION_NOTICE, result['notices'])
            self.assertIn('blocks', result['incompleteGroups'])
            self.assertIn('replaced', result['incompleteGroups'])
            self.assertTrue(graph.build_graph(self.root)['ok'])

    def test_review_bounds_current_record_fallback_and_path_free_refusals(self):
        self.time_fixture()
        with mock.patch.object(mb, '_load_queue', side_effect=ValueError('oversized queue ' + str(self.root / mb.QUEUE_REL))):
            for result in (graph.build_graph(self.root), graph.neighbors(self.root, 'deploy.py')):
                self.assertFalse(result['ok'])
                self.assertNotIn(str(self.root), result['reason'])
        for constant, value in (('MAX_NEIGHBOR_CANDIDATES', 0), ('MAX_NEIGHBOR_SECONDS', 0)):
            with mock.patch.object(graph, constant, value):
                result = self.result(target='deploy.py')
                self.assertTrue(result['truncated'])
                self.assertIn('blocks', result['incompleteGroups'])
                self.assertIn('replaced', result['incompleteGroups'])
                self.assertIn(graph.v.GRAPH_WORK_NOTICE, result['notices'])
                self.assertFalse(any(result['groups'].values()))

    def test_review_multipath_cost_directory_scan_and_stats(self):
        # The reviewer's exact cost shape; no real home, network, model or index build.
        for i in range(5000):
            self.write('src/f%04d.py' % i, 'def run():\n    return 1\n')
        for k in range(10):
            self.write('rules%02d/AGENTS.md' % k, '\n'.join('- Keep `src/f%04d.py::run` checked.' % i
                       for i in range(k * 200, (k + 1) * 200)) + '\n')
        self.tracked('src')
        lines = ['2026-01-01T00:00:00Z [user] Keep src/f0001.py checked. ' + ' '.join(
                 'src/f%04d.py' % ((i * 7 + j) % 5000) for j in range(20)) for i in range(2000)]
        self.queue_fixture(lines, lambda pin: ([self.queue_node('DRM-%d' % i, pin(i + 1)) for i in range(2000)], []), database=False)
        times = []
        real = Path.iterdir
        for _ in range(2):
            calls = {}
            def listed(path):
                calls[path] = calls.get(path, 0) + 1
                return real(path)
            start = time.perf_counter()
            with mock.patch.object(Path, 'iterdir', listed):
                counters = {}
                result = self.result(target='src/f0001.py', budget=1000, _counters=counters)
            times.append(time.perf_counter() - start)
            self.assertTrue(result['groups']['blocks'])
            self.assertTrue(result['truncated'])
            self.assertIn('blocks', result['incompleteGroups'])
            self.assertIn('replaced', result['incompleteGroups'])
            self.assertLessEqual(calls.get(self.root / 'src', 0), 1)
            self.assertEqual({'.': 1, 'src': 1}, counters['directoryListings'])
            self.assertEqual((1, 1, 1), tuple(counters[k] for k in ('snapshotReads', 'queueReads', 'tierPasses')))
            self.assertEqual(1, counters['gitCalls'])
            self.assertEqual(1, counters['anchorsClassified'])
            self.assertEqual(graph.MAX_NEIGHBOR_CANDIDATES, counters['lessonCandidatesValidated'])
            self.assertLessEqual(counters['rebindingFilesParsed'], 1)
        counters = {}
        start = time.perf_counter(); built = graph.build_graph(self.root, _counters=counters); full = time.perf_counter() - start
        self.assertEqual(2000, counters['anchorsClassified'])
        self.assertEqual(2000, counters['lessonCandidatesValidated'])
        self.assertEqual(1, counters['gitCalls'])
        self.assertEqual({'.': 1, 'src': 1}, counters['directoryListings'])
        self.assertEqual((1, 1, 1), tuple(counters[k] for k in ('snapshotReads', 'queueReads', 'tierPasses')))
        self.assertLessEqual(counters['rebindingFilesParsed'], 1)
        self.assertTrue(built['ok'], built)
        self.assertEqual(2000, built['counts']['lessonItems'])
        start = time.perf_counter(); stats = self.cli('admin', 'graph', 'stats', '--repo', str(self.root)); cli = time.perf_counter() - start
        self.assertEqual(0, stats.returncode, stats.stderr)
        self.assertEqual(built['counts'], json.loads(stats.stdout)['counts'])
        print('E1 review multipath 5000/2000/2000: neighbors %.3fs / %.3fs; build %.3fs; stats %.3fs' % (*times, full, cli))
        self.assertLess(max(times + [full, cli]), 60.0)  # sole wall-clock smoke ceiling

    def test_review_2000_anchor_file_bounded_symbol_narrowed_and_digest_cache(self):
        from memlet import code_anchors as ca
        self.write('big.py', ''.join('def f%d():\n    return %d\n' % (i, i) for i in range(2000)))
        for k in range(10):
            self.write('r%d/AGENTS.md' % k, '\n'.join('- Keep `big.py::f%d` stable.' % i for i in range(k * 200, (k + 1) * 200)) + '\n')
        self.tracked('big.py')
        ca._PYTHON_BINDING_CACHE.clear()
        start = time.perf_counter()
        with mock.patch.object(mw, 'classify_anchor', wraps=mw.classify_anchor) as classify:
            counters = {}
            result = self.result(target='big.py', budget=1000, _counters=counters)
            self.assertEqual(graph.MAX_NEIGHBOR_ANCHORS, counters['anchorsClassified'])
            self.assertEqual(1, counters['rebindingFilesParsed'])
            self.assertEqual(0, counters['lessonCandidatesValidated'])
            self.assertEqual(1, counters['gitCalls'])
            self.assertLessEqual(classify.call_count, graph.MAX_NEIGHBOR_ANCHORS)
        file_time = time.perf_counter() - start
        self.assertTrue(result['truncated'])
        self.assertIn('governs', result['incompleteGroups'])
        start = time.perf_counter()
        with mock.patch.object(mw, 'classify_anchor', wraps=mw.classify_anchor) as classify:
            counters = {}
            result = self.result(target='big.py::f7', _counters=counters)
            self.assertEqual(1, counters['anchorsClassified'])
            self.assertEqual(0, counters['rebindingFilesParsed'])
            self.assertEqual(1, classify.call_count)
        symbol_time = time.perf_counter() - start
        self.assertEqual(1, len(result['groups']['stale']))
        self.assertNotIn(graph.v.GRAPH_WORK_NOTICE, result['notices'])
        start = time.perf_counter()
        with mock.patch.object(ca, '_python_binding_analysis', wraps=ca._python_binding_analysis) as parse:
            for i in range(2000):
                self.assertEqual('unpinned', mw.classify_anchor(self.root, dict(path='big.py', symbol='f%d' % i, pin=None))['status'])
            self.assertEqual(0, parse.call_count)  # already cached by content, not stat
        all_time = time.perf_counter() - start
        print('E1 review 2000 anchors: file %.3fs; symbol %.3fs; all classify %.3fs' % (file_time, symbol_time, all_time))
        path = self.root / 'big.py'; stamp = path.stat()
        content = path.read_text(); changed = content.replace('    return 7\n', '    f7 = 7  \n', 1)
        self.assertEqual(len(content), len(changed))
        path.write_text(changed); os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        with mock.patch.object(ca, '_python_binding_analysis', wraps=ca._python_binding_analysis) as parse:
            ca.python_symbol_rebound(path, 'f7')
            self.assertEqual(1, parse.call_count)
        # Qualified/short-name resolution and wildcard rebinding stay unchanged.
        path.write_text('class C:\n    def run(self): pass\nfrom x import *\n')
        self.assertTrue(ca.python_symbol_rebound(path, 'run'))
        self.assertTrue(ca.python_symbol_rebound(path, 'C.run'))
        for i in range(35):
            path.write_text('def f(): return %d\n' % i); ca.python_symbol_rebound(path, 'f')
        self.assertLessEqual(len(ca._PYTHON_BINDING_CACHE), 32)


    def test_review_rejected_and_oversized_queues_degrade_within_bounds(self):
        self.write('deploy.py', 'x\n'); self.tracked('deploy.py')
        lines = ['2026-01-01T00:00:00Z [user] Never edit deploy.py by hand.']
        lines += ['2026-01-01T00:00:00Z [user] Rejected %d %s' % (i, ' lorem' * 60) for i in range(4900)]
        def build(pin):
            nodes = [self.queue_node('DRM-A', pin(1), 'deploy.py')]
            for i in range(4900):
                p = pin(i + 2)
                n = self.queue_node('DRM-R%d' % i, p, approved=False)
                n['allPins'] = [p, p, p]
                n['approval'] = dict(schema=md.APPROVAL_RECORD_SCHEMA, decision='rejected', by='reviewer',
                                     at='2026-01-02T00:00:00Z', digest=md.node_approval_digest(n))
                nodes.append(n)
            return nodes, []
        self.queue_fixture(lines, build, database=False)
        counters = {}
        start = time.perf_counter(); result = self.result(target='deploy.py', _counters=counters); elapsed = time.perf_counter() - start
        self.assertEqual(1, len(result['groups']['blocks']))
        self.assertEqual(1, counters['lessonCandidatesValidated'])
        self.assertEqual(1, counters['gitCalls'])
        print('E1 review 4900 rejected large queue: %d bytes; %.3fs' % ((self.root / mb.QUEUE_REL).stat().st_size, elapsed))
        self.write(mb.QUEUE_REL, ' ' * (md.QUEUE_MAX_BYTES + 1))
        result = graph.neighbors(self.root, 'deploy.py')
        self.assertFalse(result['ok'])
        self.assertNotIn(str(self.root), result['reason'])

    def test_review_supersession_chain_500_and_over_bound_513(self):
        self.write('deploy.py', 'x\n'); self.tracked('deploy.py')
        for size in (500, 513):
            lines = ['2026-01-01T00:00:00Z [user] Deploy rule version %d in deploy.py' % i for i in range(size + 1)]
            def build(pin):
                nodes = []
                for i in range(size):
                    old, new = pin(i + 1), pin(i + 2)
                    e = dict(edgeId='E-S%d' % i, edgeType='SUPERSEDES', fromId='DRM-S%d' % i,
                             toId='SUP:%d' % i, targetLabel='x', srcPin=new, targetPin=old, edgeVerdict='VALID')
                    n = dict(id='DRM-S%d' % i, title='fixture', category='superseded_directive',
                             statusKind='superseded_rule', scopeTag='general', primaryPin=old,
                             allPins=[old, new], supersededByPin=new, edges=[e], verdict='ALREADY_SUPERSEDED')
                    self.seal(e, 'edge'); self.seal(n); nodes.append(n)
                return nodes, []
            self.queue_fixture(lines, build, database=False)
            counters = {}
            start = time.perf_counter(); result = self.result(target='deploy.py', _counters=counters); elapsed = time.perf_counter() - start
            print('E1 review supersession chain %d: %.3fs' % (size, elapsed))
            self.assertEqual(500 if size == 500 else 0, counters['lessonCandidatesValidated'])
            self.assertTrue(result['truncated'])
            self.assertIn('replaced', result['incompleteGroups'])
            if size == 513:
                self.assertFalse(any(result['groups'].values()))
                self.assertIn(graph.v.GRAPH_SUPERSESSION_NOTICE, result['notices'])
                by_id = self.result(target='DRM-S0')
                self.assertTrue(by_id['truncated'])
                self.assertIn('replaced', by_id['incompleteGroups'])
            else:
                self.assertNotIn(graph.v.GRAPH_SUPERSESSION_NOTICE, result['notices'])
                self.assertTrue(result['groups']['replaced'])
                self.assertTrue(all(i.get('quarantined') for i in result['groups']['replaced']))

    def test_review_events_and_anchor_events_bounds_skip_only_history(self):
        self.anchors()
        node = self.time_fixture()
        self.event(node, '2026-01-02T00:00:00Z')
        with contextlib.closing(sqlite3.connect(str(self.root / mw.LEDGER_PATH))) as db, db:
            row = db.execute('SELECT at,item_id,kind,decision,by,digest,paths FROM dream_decision_events LIMIT 1').fetchone()
            db.executemany('INSERT INTO dream_decision_events (at,item_id,kind,decision,by,digest,paths) VALUES (?,?,?,?,?,?,?)',
                           [row] * graph.MAX_EVENTS)
        result = self.result(target='deploy.py')
        self.assertTrue(result['groups']['blocks'])
        self.assertIn(graph.v.GRAPH_EVENTS_NOTICE, result['notices'])
        with mock.patch.object(graph, 'MAX_EVENTS', 0):
            result = self.result(target='auth.py')
            self.assertTrue(result['groups']['governs'])
            self.assertIn(graph.v.GRAPH_ANCHOR_EVENTS_NOTICE, result['notices'])

    def test_review_budget_marks_partial_critical_groups(self):
        repo, _ = self.fixture('approved')
        result = self.result(repo, 'deploy.py', budget=0)
        self.assertTrue(result['truncated'])
        self.assertIn('blocks', result['incompleteGroups'])
        self.assertIn('replaced', result['incompleteGroups'])


    def test_review_accepted_anchor_time_digest_and_future_row(self):
        sid = self.anchors()
        with contextlib.closing(sqlite3.connect(str(self.root / mw.LEDGER_PATH))) as db, db:
            db.execute('UPDATE ' + ma.LEDGER_TABLE + ' SET recorded_at=? WHERE entry_id=?',
                       ('2099-01-01T00:00:00Z', sid))
        built = graph.build_graph(self.root)
        edge = next(e for e in built['edges'] if e['basis'] == 'accepted-suggestion')
        self.assertIsNone(edge['validFrom'])
        self.assertIn(graph.v.GRAPH_FUTURE_NOTICE, built['notices'])
        config, _ = mw.read_config(self.root)
        history = graph._History(self.root)
        record = history.sidecars[sid][0]
        record.update(at='2026-01-01T00:00:00Z', digest='mismatched')
        rule = dict(sidecarIds={'auth.py::validate_token': sid})
        pin = next(e['pin'] for e in config['sidecarAnchors'] if e['id'] == sid)
        self.assertIsNone(history.anchor_time(rule, 'auth.py::validate_token', config, pin))


    def test_review_future_pinned_anchor_cannot_date_present(self):
        self.anchors()
        config, _ = mw.read_config(self.root)
        for record in config['pinnedAnchors'].values():
            record['pinnedAt'] = '2099-01-01T00:00:00Z'
        self.write(mw.CONFIG_PATH, json.dumps(config))
        built = graph.build_graph(self.root)
        edges = [e for e in built['edges'] if e['basis'] == 'written-anchor']
        self.assertTrue(edges)
        self.assertTrue(all(e['validFrom'] is None for e in edges))
        self.assertIn(graph.v.GRAPH_FUTURE_NOTICE, built['notices'])
        self.assertTrue(self.result()['groups']['governs'])



    def test_addendum_worst_anchor_agrees_with_handover_on_both_files(self):
        rule = 'Always call `mod.py::charge` only after `util.py::helper` validates the input.'
        for state, helper in (('stale', 'def helper(x, required):\n    return x\n'),
                              ('unpinned', 'def helper(x):\n    return x\n'),
                              ('compatible-unverified', 'def helper(x, extra=None):\n    return x\n')):
            repo = self.root / state; repo.mkdir()
            (repo / 'mod.py').write_text('def charge(x):\n    return x\n')
            (repo / 'util.py').write_text('def helper(x):\n    return x\n')
            (repo / 'AGENTS.md').write_text('- ' + (rule if state != 'unpinned' else 'Keep `mod.py::charge`.') + '\n')
            mw.init_project(repo)
            (repo / 'AGENTS.md').write_text('- ' + rule + '\n')
            (repo / 'util.py').write_text(helper)
            report = mw.pinned_anchor_report(repo)
            self.assertEqual({'mod.py::charge': 'active', 'util.py::helper': state},
                             {a['anchor']: a['status'] for a in report['anchors']})
            rules = mw._extract_rules('- ' + rule, 'AGENTS.md')
            mw._verify_rules(repo, rules, mw.rule_anchor_pins(repo))
            self.assertEqual(state, rules[0]['status'])
            hand = mw.build_handover(repo, budget=8000)
            self.assertFalse(any(c['rule'] == rule for c in hand['activeConstraints']))
            for target in ('mod.py', 'util.py', 'mod.py::charge', 'util.py::helper'):
                result = self.result(repo, target)
                self.assertFalse(result['groups']['governs'], (state, target))
                self.assertEqual(1, len(result['groups']['stale']))
                self.assertEqual(state, result['groups']['stale'][0]['status'])

    def test_addendum_multirelation_single_restrictive_group_and_valid_relations(self):
        node = self.time_fixture()
        base = mb._load_queue(self.root)[0]
        for relations, expected in ((('BLOCKS', 'RESOLVES', 'GOVERNS'), 'blocks'),
                                     (('GOVERNS', 'RESOLVES'), 'governs'), (('RESOLVES',), 'resolves')):
            data = copy.deepcopy(base)
            n = data['nodes'][0]; n['edges'] = []
            for relation in relations:
                e = copy.deepcopy(node['edges'][0]); e.update(edgeId='E-' + relation, edgeType=relation,
                                                              toId=relation + ':target')
                self.seal(e, 'edge'); n['edges'].append(e)
            pending = copy.deepcopy(node['edges'][0]); pending.update(edgeId='E-pending', edgeType='RESOLVES')
            pending.pop('approval', None); n['edges'].append(pending)
            self.seal(n); data['edges'] = copy.deepcopy(n['edges']); self.save_graph(self.root, data)
            result = self.result(target='deploy.py', budget=2000)
            memberships = [(g, i) for g, items in result['groups'].items() for i in items if i['id'] == n['id']]
            self.assertEqual([expected], [g for g, _ in memberships])
            item = memberships[0][1]
            self.assertEqual(set(relations), set(item['relations']))
            self.assertEqual('approved', item['tier'])
            self.assertIn(item['relation'], relations)
            for relation in relations:
                self.assertIn(relation, result['formatted'])
        # All existing eight-kind fixtures obey group/tier/relation consistency.
        repo, _ = self.fixture('approved')
        result = self.result(repo, 'deploy.py', budget=8000)
        all_ids = [i['id'] for items in result['groups'].values() for i in items]
        self.assertEqual(len(all_ids), len(set(all_ids)))
        for group, relation in (('blocks', 'BLOCKS'), ('resolves', 'RESOLVES'), ('governs', 'GOVERNS')):
            for i in result['groups'][group]:
                if i['kind'] == 'lesson':
                    self.assertEqual('approved', i['tier']); self.assertIn(relation, i['relations'])
                else:
                    self.assertEqual('active', i['status'])
        for i in result['groups']['replaced']:
            self.assertTrue(i.get('quarantined') or i.get('replacedBy'))

    def test_addendum_whole_quotes_and_explicit_critical_incomplete_markers(self):
        repo, _ = self.fixture('approved', ('RESOLVES', 'BLOCKS'))
        full = self.result(repo, 'deploy.py', budget=8000)
        for items in full['groups'].values():
            for item in items:
                for line in item['quoteLines']:
                    self.assertIn(line, full['formatted'])
        for budget in (0, 1, 120):
            result = self.result(repo, 'deploy.py', budget=budget)
            self.assertEqual(bool(result['omitted']['blocks']), result['blocksIncomplete'])
            self.assertEqual(bool(result['omitted']['replaced']), result['replacedIncomplete'])
            for group, items in result['groups'].items():
                for item in items:
                    self.assertIn(item, full['groups'][group])
                    for line in item['quoteLines']:
                        self.assertIn(line, result['formatted'])
        item = dict(id='DRM-long', kind='lesson', tier='approved', relation='BLOCKS', relations=['BLOCKS'],
                    quoteLines=['x' * 9000 + 'KEEP-THIS-TAIL', 'SECOND-CITED-LINE'],
                    cites=[dict(file='snapshot/a.log', start=1), dict(file='snapshot/a.log', start=2)])
        rendered = graph._render_item(item)
        self.assertIn('KEEP-THIS-TAIL', rendered); self.assertIn('SECOND-CITED-LINE', rendered)
        groups = {g: [] for g in graph.GROUPS}; groups['blocks'] = [item]
        omitted, formatted = graph._trim(groups, 8000)
        self.assertEqual(1, omitted['blocks']); self.assertFalse(groups['blocks'])
        self.assertNotIn('SECOND-CITED-LINE', formatted)
        with mock.patch.object(graph, 'MAX_NEIGHBOR_CANDIDATES', 0):
            result = self.result(repo, 'deploy.py')
            self.assertTrue(result['blocksIncomplete']); self.assertTrue(result['replacedIncomplete'])
            self.assertIn(graph.v.GRAPH_INCOMPLETE_MARKER, result['formatted'])


    def test_amendment_no_scope_attaches_any_tier_even_to_poisoned_or_longer_path(self):
        self.write('util.py', 'def helper(): pass\n'); self.tracked('util.py')
        for approved in (True, False):
            for quote in ('Never call helper() in util.py.backup from tests',
                          'Never call helper() in util.py\u200b from tests',
                          'Never call helper() from tests'):
                with self.subTest(approved=approved, quote=quote):
                    def build(pin):
                        n = self.queue_node('DRM-A', pin(1), 'util.py', approved)
                        if not approved:
                            n['proposers'] = [dc._receipt(vendor, n, 'f' * 24,
                                {'model': 'haiku' if vendor == 'claude' else 'codex'}) for vendor in ('claude', 'codex')]
                        return [n], []
                    self.queue_fixture(['2026-01-01T00:00:00Z [user] ' + quote], build)
                    built = graph.build_graph(self.root)
                    items = [n for n in built['nodes'] if n.get('recordType') == 'item']
                    self.assertEqual(1, len(items))
                    self.assertEqual('approved' if approved else 'hint', items[0]['tier'])
                    self.assertEqual([], items[0]['paths'])
                    result = self.result(target='util.py', tiers=('approved', 'hint'))
                    self.assertFalse(any(result['groups'].values()))
                    # The raw-reference candidate index also ignores scope.
                    self.assertNotIn('DRM-A', [n['id'] for n in graph._slice(mb._load_queue(self.root)[0], 'util.py', None)['nodes']])


    def test_amendment_replaced_citation_cannot_supply_a_literal_path(self):
        self.write('deploy.py', 'x\n'); self.tracked('deploy.py')
        lines = ['2026-01-01T00:00:00Z [user] Never edit deploy.py by hand.',
                 '2026-01-02T00:00:00Z [user] Use the checked wrapper instead.']
        def build(pin):
            old, new = pin(1), pin(2)
            edge = dict(edgeId='E-S', edgeType='SUPERSEDES', fromId='DRM-S', toId='SUP:old',
                        targetLabel='old', srcPin=new, targetPin=old, edgeVerdict='VALID')
            node = dict(id='DRM-S', title='fixture', category='superseded_directive', statusKind='superseded_rule',
                        scopeTag='deploy.py', primaryPin=old, allPins=[old, new], supersededByPin=new,
                        edges=[edge], verdict='ALREADY_SUPERSEDED')
            self.seal(edge, 'edge'); self.seal(node)
            return [node], []
        self.queue_fixture(lines, build)
        item = self.graph_lessons()['DRM-S']
        self.assertEqual([], item['paths'])
        self.assertFalse(any(self.result(target='deploy.py')['groups'].values()))
        self.assertTrue(self.result(target='DRM-S')['groups']['replaced'])


    def test_ruling_current_record_wins_reordered_log_and_names_conflicted_item(self):
        node = self.time_fixture()
        self.event(node, '2026-03-01T00:00:00Z', 'rejected')
        self.event(node, '2026-02-01T00:00:00Z', 'approved')
        for edge in node['edges']:
            self.event(edge, '2026-02-01T00:00:00Z', kind='edge')
        result = graph.build_graph(self.root, as_of='2026-04-01')
        self.assertEqual('approved', next(n['tier'] for n in result['nodes'] if n.get('id') == node['id']))
        self.assertIn(graph.v.GRAPH_ORDER_ITEM_NOTICE % node['id'], result['notices'])
        self.assertEqual({}, self.graph_lessons(as_of='2026-01-01'))
        # Mirror: an advisory later approval never overrides a current rejection.
        data = mb._load_queue(self.root)[0]
        data['nodes'][0]['approval'].update(decision='rejected', at='2026-03-01T00:00:00Z')
        self.save_graph(self.root, data)
        self.event(node, '2026-04-01T00:00:00Z', 'approved')
        for at in ('2026-03-01', '2026-04-01', '2099-01-01'):
            result = self.result(target='deploy.py', as_of=at)
            self.assertFalse(any(result['groups'].values()))
            self.assertIn(graph.v.GRAPH_ORDER_ITEM_NOTICE % node['id'], result['notices'])


    def test_review_injected_clock_stops_with_exact_incomplete_groups(self):
        self.time_fixture()
        clock = mock.Mock(side_effect=[0.0, 0.0, 3.0])
        counters = {}
        result = self.result(target='deploy.py', _clock=clock, _counters=counters)
        self.assertEqual(3, clock.call_count)
        self.assertEqual({g: [] for g in graph.GROUPS}, result['groups'])
        self.assertTrue(result['truncated'])
        self.assertEqual(sorted(graph.GROUPS), result['incompleteGroups'])
        self.assertTrue(result['blocksIncomplete'])
        self.assertTrue(result['replacedIncomplete'])
        self.assertIn(graph.v.GRAPH_WORK_NOTICE, result['notices'])
        self.assertEqual(0, counters['anchorsClassified'])
        self.assertEqual(0, counters['lessonCandidatesValidated'])
        self.assertEqual(0, counters['gitCalls'])
        self.assertEqual(0, counters['snapshotReads'])

    def test_shared_rule_caches_unreadable_directory_once(self):
        self.write('one.py', 'x\n'); self.write('two.py', 'x\n')
        context = mb.lesson_path_context(self.root)
        with mock.patch.object(Path, 'iterdir', side_effect=PermissionError('fixture')) as listed:
            self.assertFalse(mb.valid_lesson_path(self.root, 'one.py', context=context))
            self.assertFalse(mb.valid_lesson_path(self.root, 'two.py', context=context))
            self.assertEqual(1, listed.call_count)

    def test_decision_item_queries_have_an_independent_cap(self):
        node = self.time_fixture()
        self.event(node, '2026-01-02T00:00:00Z')
        data = dict(nodes=[dict(id='DRM-%d' % i, edges=[]) for i in range(10)], edges=[])
        with mock.patch.object(graph, 'MAX_HISTORY_QUERIES', 2), \
             mock.patch.object(mb, 'decision_events', side_effect=[mb.DecisionEvents(truncated=True), [], []]) as reader:
            history = graph._History(self.root, graph=data)
        self.assertEqual(3, reader.call_count)  # one global read plus two item reads
        self.assertIn(graph.v.GRAPH_EVENTS_NOTICE, history.notices)
        self.assertEqual({}, history.decisions)

    def test_clock_bound_covers_shared_directory_work(self):
        node = self.time_fixture()
        self.write('src/extra.py', 'x = 1\n'); self.tracked('src/extra.py')
        data = mb._load_queue(self.root)[0]
        # A human path makes shared validation traverse a directory not needed
        # by target-id resolution or rule discovery.
        self.assertTrue(mb.learn_paths(self.root, [node['id']], approver='Reviewer', paths=['src/extra.py'])['ok'])
        clock = [0.0]
        real = Path.iterdir
        def listed(path):
            result = real(path)
            if path == self.root:
                clock[0] = 3.0
            return result
        counters = {}
        with mock.patch.object(Path, 'iterdir', listed):
            result = self.result(target=node['id'], _clock=lambda: clock[0], _counters=counters)
        self.assertEqual(sorted(graph.GROUPS), result['incompleteGroups'])
        self.assertFalse(any(result['groups'].values()))
        self.assertEqual({'.': 1}, counters['directoryListings'])
        self.assertEqual(1, counters['snapshotReads'])
        self.assertEqual(1, counters['queueReads'])
        self.assertEqual(1, counters['gitCalls'])

    def test_review_long_cited_line_token_scan_is_linear(self):
        text = ('do not repeat ' * 150000) + 'deploy.py'
        start = time.perf_counter()
        paths = mb._lesson_path_candidates(text)
        elapsed = time.perf_counter() - start
        self.assertEqual({'do', 'not', 'repeat', 'deploy.py'}, paths)
        print('E1 long cited line %d bytes: %.3fs token scan' % (len(text), elapsed))



    def test_final_managed_dream_pages_are_not_rules_and_every_id_is_queryable(self):
        repo, _ = self.fixture('approved', ('BLOCKS', 'GOVERNS', 'SUPERSEDES'))
        built = graph.build_graph(repo)
        self.assertTrue(built['ok'], built)
        self.assertEqual(1, built['counts']['nodes']['rule'])
        self.assertFalse(any(n.get('path') == mb.PROMOTED_PAGE and n['kind'] == 'rule' for n in built['nodes']))
        for node in built['nodes']:
            with self.subTest(kind=node['kind'], id=node['id']):
                response = graph.neighbors(repo, node['id'], budget=8000)
                self.assertTrue(response['ok'], response)
        result = self.cli('admin', 'graph', 'stats', '--repo', str(repo))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, json.loads(result.stdout)['counts']['nodes']['rule'])

    def test_final_historical_markdown_and_empty_history_are_explicit(self):
        from memlet import vocabulary as v
        self.time_fixture()
        for cutoff, empty in [('2025-01-01T00:00:00Z', True), ('2026-01-03T00:00:00Z', False)]:
            result = self.cli('admin', 'graph', 'show', 'deploy.py', '--repo', str(self.root),
                              '--as-of', cutoff, '--format', 'markdown', '--budget', '8000')
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(result.stdout.startswith(v.GRAPH_HISTORICAL_HEAD % cutoff), result.stdout)
            self.assertIn(v.GRAPH_HISTORY_NOTICE, result.stdout)
            self.assertNotIn('9.1.1', result.stdout)
            if empty:
                self.assertIn(v.GRAPH_HISTORICAL_EMPTY % cutoff, result.stdout)
            else:
                self.assertIn('Never edit deploy.py', result.stdout)

    def test_final_current_only_quarantined_lesson_does_not_poison_blocks(self):
        from memlet import memlet_hooks as hooks, vocabulary as v
        self.write('deploy.py', 'x = 1\n')
        self.write('AGENTS.md', '# Rules\n')
        mw.init_project(self.root)
        self.tracked('deploy.py', 'AGENTS.md')
        lines = ['2026-01-01T00:00:00Z [user] Never edit deploy.py by hand.',
                 '2026-01-02T00:00:00Z [user] Edit deploy.py only via gen_v1.',
                 '2026-01-03T00:00:00Z [user] Edit deploy.py only via gen_v2.',
                 '2026-01-04T00:00:00Z [user] Never commit deploy.py with debug flags on.']
        def build(pin):
            nodes = [self.queue_node('DRM-' + name, pin(i+1), 'general') for i, name in enumerate(('X', 'S', 'T', 'Z'))]
            for n, target, to in ((nodes[1], pin(1), 'DRM-X'), (nodes[2], pin(2), 'DRM-S')):
                n.update(category='superseded_directive', statusKind='active_lesson')
                edge = dict(edgeId='E-' + n['id'], edgeType='SUPERSEDES', fromId=n['id'], toId=to,
                            targetLabel='fixture', srcPin=n['primaryPin'], targetPin=target, edgeVerdict='VALID')
                n['edges'] = [self.seal(edge, 'edge')]
                self.seal(n)
            return nodes, []
        self.queue_fixture(lines, build)
        read = {i['id']: i for i in mb.dream_items(self.root)['items']}
        self.assertTrue(read['DRM-T']['quarantined'])
        self.assertEqual(['current'], read['DRM-T']['lineRoles'])
        result = graph.neighbors(self.root, 'deploy.py', budget=8000)
        self.assertTrue(result['ok'], result)
        self.assertIn('DRM-Z', [i['id'] for i in result['groups']['blocks']])
        self.assertNotIn('DRM-T', [i['id'] for i in result['groups']['replaced']])
        self.assertIn(v.GRAPH_CURRENT_ONLY_NOTICE, result['notices'])
        self.assertIsNotNone(hooks._checked_neighbors(result, ('approved',)))

if __name__ == '__main__':
    unittest.main()
