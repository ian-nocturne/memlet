"""Final product-claim regressions. All filesystem/CLI work is isolated; no model calls."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from memlet import memlet_bridge as mb, memlet_wiki as mw, memlet_dream as md
from memlet import memlet_recall as mr
from memlet.audit import audit_repository

HERE = Path(__file__).resolve().parent


class ClaimsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='final-claims-')).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.repo = self.tmp / 'repo'
        for d in (self.repo / 'app', self.tmp / 'home', self.tmp / 'tmp'):
            d.mkdir(parents=True)
        self.env = dict(os.environ, HOME=str(self.tmp / 'home'), TMPDIR=str(self.tmp / 'tmp'),
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_AUTHOR_NAME='test', GIT_COMMITTER_NAME='test',
                        GIT_AUTHOR_EMAIL='test@example.invalid', GIT_COMMITTER_EMAIL='test@example.invalid')
        (self.repo / 'app/billing.py').write_text('def charge(user_id, amount):\n    return amount\n')
        (self.repo / 'CLAUDE.md').write_text('# Billing\n\n\n\n\n\n- Charging must use `app/billing.py::charge` to charge a customer.\n')
        self.git('init', '-q')
        self.git('add', '.')
        self.git('commit', '-qm', 'baseline')
        self.assertTrue(mw.init_project(self.repo)['ok'])

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo)] + list(args), env=self.env,
                              check=True, capture_output=True, text=True)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(HERE / 'memory_cli.py')] + list(args),
                              cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=60)

    def drift(self):
        (self.repo / 'app/billing.py').write_text('def charge(account_id, amount, currency):\n    return amount\n')

    def server(self):
        from memlet_mcp import MemletMCPServer
        return MemletMCPServer(root=self.tmp / 'store', default_task='test/main')

    def learn(self, lines):
        path = self.repo / 'session.log'
        path.write_text('\n'.join('[user] ' + line for line in lines) + '\n')
        result = mb.learn_from_transcript(self.repo, path)
        self.assertTrue(result['ok'], result)
        return [n['id'] for n in mb.learn_pending(self.repo)['pendingNodes']]

    def approve(self, ids, **kwargs):
        result = mb.learn_approve(self.repo, ids, approver='Reviewer', **kwargs)
        self.assertTrue(result['ok'], result)
        return result

    def index_fixture(self):
        # Construct a real pinned index without calling index-build (even a fake runner).
        from memlet.memlet_index import parse_index, _render_page, INDEX_PAGE
        lines = (self.repo / 'CLAUDE.md').read_text().splitlines()
        entries, _ = parse_index('- billing charge customer | 1 [src: CLAUDE.md:7-7]', {'CLAUDE.md': lines})
        self.assertEqual(1, len(entries))
        (self.repo / INDEX_PAGE).write_text(_render_page(entries, {'sources': ['CLAUDE.md']}))

    def test_m1_parsed_cli_and_mcp_quarantine(self):
        self.drift()
        result = json.loads(self.cli('recall', 'charge a customer', '--format', 'json').stdout)
        self.assertEqual([], result['sections']['rules'])
        self.assertEqual(1, result['staleAnchors'])
        self.assertEqual('signature_drift', result['sections']['staleRules'][0]['anchorReason'])
        self.assertIn('anchored code changed — do not apply without re-reading the rule', result['formatted'])
        mcp = self.server().call_tool('memlet_recall', {'repo_path': str(self.repo), 'query': 'charge a customer'})
        self.assertEqual(result['sections']['staleRules'], mcp['sections']['staleRules'])

    def test_m1_index_and_legacy_recall_exclude_stale(self):
        self.index_fixture()
        before = mr.recall(self.repo, 'charge')
        self.assertTrue(before['spans'])
        self.drift()
        after = mr.recall(self.repo, 'charge')
        self.assertEqual(set(before), set(after))
        self.assertEqual([], after['spans'])
        self.assertNotIn('Charging must use', after['formatted'])
        result = mb.unified_recall(self.repo, 'charge')
        self.assertEqual([], result['sections']['rules'])
        self.assertEqual(1, result['staleAnchors'])

    def test_m1_non_stale_states_carry_reasons(self):
        (self.repo / 'app/billing.py').write_text('def charge(user_id, amount):\n    return amount + 1\n')
        result = mb.unified_recall(self.repo, 'charge')
        item = result['sections']['rules'][0]
        self.assertEqual('compatible-unverified', item['anchorStatus'])
        self.assertIn(item['anchorReason'], result['formatted'])
        (self.repo / 'CLAUDE.md').write_text('- Keep `app/billing.py` for billing.\n- Use `app/new.py::call` for billing.\n')
        (self.repo / 'app/new.py').write_text('def call():\n    pass\n')
        result = mb.unified_recall(self.repo, 'billing')
        self.assertEqual({'path-exists', 'unpinned'}, {r['anchorStatus'] for r in result['sections']['rules']})

    def test_m1_promoted_lesson_quarantine(self):
        ids = self.learn(['RULE: Never charge a customer without `app/billing.py::charge`.'])
        self.approve(ids)
        self.drift()
        result = mb.unified_recall(self.repo, 'charge customer', budget=3000)
        self.assertEqual([], result['sections']['dream'])
        self.assertTrue(any(it.get('id') in ids for it in result['sections']['staleRules']))
        self.assertNotIn('Never charge a customer', mr.recall(self.repo, 'charge')['formatted'])

    def test_m2_committed_stale_full_changed_cli_mcp(self):
        self.drift()
        self.git('add', 'app/billing.py')
        self.git('commit', '-qm', 'signature changed')
        for args in [('check', '.', '--format', 'markdown'), ('audit', '.', '--format', 'markdown'),
                     ('check', '--changed', '--format', 'markdown')]:
            result = self.cli(*args)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertIn('STALE', result.stdout)
            self.assertIn('signature_drift', result.stdout)
            self.assertNotIn('PASS', result.stdout)
        mcp = self.server().call_tool('memlet_check', {'repo_path': str(self.repo), 'changed': False})
        self.assertEqual(1, mcp['exitCode'])
        self.assertEqual(1, mcp['pinnedAnchors']['summary']['stale'])
        changed = mb.changed_check(self.repo)
        self.assertEqual([], changed['changedFiles'])
        self.assertEqual(1, changed['staleCount'])

    def test_m2_warnings_and_uninitialized_compatibility(self):
        (self.repo / 'app/billing.py').write_text('def charge(user_id, amount):\n    return amount + 1\n')
        result = audit_repository(self.repo)
        self.assertEqual(0, result['exitCode'])
        self.assertGreater(result['warningCount'], 0)
        shutil.rmtree(self.repo / '.memlet')
        self.assertNotIn('pinnedAnchors', audit_repository(self.repo))

    def test_s1_wiki_preserves_written_capsule_bytes(self):
        cap = mw.build_handover(self.repo, write=True)
        path = self.repo / mw.HANDOVER_PAGE
        before = path.read_bytes()
        self.assertTrue(mw.verify_handover_markdown(before.decode()))
        result = self.cli('admin', 'wiki', '.')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, path.read_bytes())
        self.assertTrue(mw.verify_handover_markdown(path.read_text()))

    def test_s2_citation_failure_is_per_item_and_reapprovable(self):
        ids = self.learn(['RULE: Never charge without `app/billing.py::charge`.',
                          'RULE: Never omit refunds from documentation.'])
        self.approve(ids)
        queue = json.loads((self.repo / mb.QUEUE_REL).read_text())
        broken = next(n for n in queue['nodes'] if 'charge' in n['title'])
        good = next(n for n in queue['nodes'] if n['id'] != broken['id'])
        path = self.repo / mb.SNAPSHOT_REL / broken['primaryPin']['fileId']
        path.write_text(path.read_text().replace('Never charge', 'Always charge'))
        pending = mb.learn_pending(self.repo)
        self.assertEqual([broken['id']], [n['id'] for n in pending['pendingNodes']])
        self.assertIn('Always charge', pending['pendingNodes'][0]['currentQuote'])
        self.assertTrue(pending['pendingNodes'][0]['citationReason'])
        recalled = mb.unified_recall(self.repo, 'refunds', budget=3000)
        self.assertIn(good['id'], [n['id'] for n in recalled['sections']['dream']])
        self.assertNotIn(broken['id'], [n['id'] for n in recalled['sections']['dream']])
        self.assertEqual([broken['id']], recalled['unverifiableNodeIds'])
        self.assertEqual(1, recalled['unverifiableCount'])
        self.assertIn('Citation verification failed', recalled['formatted'])
        for item in recalled['sections']['dream']:
            raw = next(n for n in queue['nodes'] if n['id'] == item['id'])
            self.assertIsNone(md._verify_item_pins(raw, 'node', md.load_snapshot(self.repo / mb.SNAPSHOT_REL)))
        self.assertFalse(mb.learn_approve(self.repo, [broken['id']], approver='Reviewer')['ok'])
        self.approve([broken['id']], refresh_citations=True)
        self.assertEqual(0, mb.learn_pending(self.repo)['counts']['pendingNodes'])
        self.assertIn('Always charge', (self.repo / mb.PROMOTED_PAGE).read_text())

    def test_s2_every_citation_changed_returns_nothing_with_notice(self):
        ids = self.learn(['RULE: Never charge before review.', 'RULE: Never omit refunds.'])
        self.approve(ids)
        for path in (self.repo / mb.SNAPSHOT_REL).iterdir():
            path.write_text(path.read_text().replace('Never', 'Always'))
        result = mb.unified_recall(self.repo, 'charge refunds', include=('dream',), budget=2000)
        self.assertEqual([], result['sections']['dream'])
        self.assertEqual(set(ids), set(result['unverifiableNodeIds']))
        self.assertEqual(2, result['unverifiableCount'])
        self.assertIn('Citation verification failed for 2 lesson(s)', result['formatted'])
        self.assertEqual(set(ids), {it['id'] for it in mb.learn_pending(self.repo)['pendingNodes']})
        direct = md.run_dream_recall(str(self.repo / mb.QUEUE_REL), snapshot_path=str(self.repo / mb.SNAPSHOT_REL))
        self.assertEqual([], direct['includedNodeIds'])
        self.assertEqual(set(ids), set(direct['unverifiableNodeIds']))
        self.assertIn('Citation verification failed', direct['formatted'])
        tiny = mb.unified_recall(self.repo, 'charge', include=('dream',), budget=5)
        self.assertFalse(tiny['ok'])
        self.assertEqual([], tiny['sections']['dream'])
        self.assertEqual(2, tiny['unverifiableCount'])
        self.assertLessEqual(tiny['tokenEstimate'], tiny['tokenBudget'])

    def test_s2_unreadable_or_unsafe_snapshot_returns_no_lessons(self):
        ids = self.learn(['RULE: Never charge before review.'])
        self.approve(ids)
        path = self.repo / mb.SNAPSHOT_REL
        moved = self.tmp / 'moved-snapshot'
        path.rename(moved)
        result = mb.unified_recall(self.repo, 'charge', include=('dream',))
        self.assertEqual([], result['sections']['dream'])
        self.assertFalse(result['sources']['dream']['available'])
        direct = md.run_dream_recall(str(self.repo / mb.QUEUE_REL))
        self.assertFalse(direct['ok'])
        self.assertEqual([], direct['includedNodeIds'])
        path.symlink_to(moved, target_is_directory=True)
        result = mb.unified_recall(self.repo, 'charge', include=('dream',))
        self.assertEqual([], result['sections']['dream'])
        self.assertIn('symlink', result['sources']['dream']['reason'])

    def test_s12_mcp_case_variant_approvals_are_cli_only_and_do_not_write(self):
        self.learn(['RULE: Never charge before review.'])
        queue = self.repo / mb.QUEUE_REL
        before = queue.read_bytes()
        server = self.server()
        for action in ('APPROVE', 'Approve', 'REJECT', 'Reject', 'aPpRoVe'):
            with self.subTest(action=action):
                with self.assertRaisesRegex(ValueError, 'CLI-only'):
                    server.call_tool('memlet_learn', {'repo_path': str(self.repo), 'action': action})
                self.assertEqual(before, queue.read_bytes())


    def test_s3_approved_rejected_idempotent_explicit_fresh_approval(self):
        ids = self.learn(['RULE: Never charge without `app/billing.py::charge`.'])
        self.approve(ids)
        result = mb.learn_reject(self.repo, ids, approver='Reviewer', reason='No longer valid')
        self.assertTrue(result['ok'], result)
        self.assertEqual(ids, result['applied'])
        self.assertNotIn(ids[0], (self.repo / mb.PROMOTED_PAGE).read_text())
        self.assertEqual([], mb.unified_recall(self.repo, 'charge')['sections']['dream'])
        self.assertEqual(ids, mb.learn_reject(self.repo, ids, approver='Reviewer')['unchanged'])
        self.assertFalse(mb.learn_approve(self.repo, ids, approver='Reviewer')['ok'])
        self.approve(ids, reconsider=True)
        self.assertIn(ids[0], (self.repo / mb.PROMOTED_PAGE).read_text())

    def test_s4_every_secret_family_never_persisted(self):
        secrets = ['sk_' + 'live_abcdefghijklmnopqrstuvwx', 'sk_' + 'test_abcdefghijklmnopqrstuvwx',
                   'rk_' + 'live_abcdefghijklmnopqrstuvwx', 'whsec_abcdefghijklmnopqrstuvwx',
                   'ghp_abcdefghijklmnopqrstuvwx', 'gho_abcdefghijklmnopqrstuvwx', 'ghs_abcdefghijklmnopqrstuvwx',
                   'github_pat_abcdefghijklmnopqrstuvwx']
        secrets += ['xox' + c + '-abcdefghijklmnopqrstuvwx' for c in 'abprs']
        secrets += ['AKIAABCDEFGHIJKLMNOP', 'ASIAABCDEFGHIJKLMNOP', 'AIza' + 'x' * 35]
        secrets += [key + sep + 'secretvalue12345' for key in ('STRIPE_KEY', 'MY_TOKEN', 'APP_SECRET', 'PASSWORD', 'PASSWD')
                    for sep in ('=', ': ')]
        for i, secret in enumerate(secrets):
            with self.subTest(secret_family=i):
                path = self.repo / ('secret%d.log' % i)
                path.write_text('[user] RULE: Never share billing credential family%d ' % i + secret + ' with customers.\n')
                result = mb.learn_from_transcript(self.repo, path)
                self.assertTrue(result['ok'], result)
                self.approve([n['id'] for n in mb.learn_pending(self.repo)['pendingNodes']])
                recall = mb.unified_recall(self.repo, 'billing credential', budget=4000)
                self.assertNotIn(secret, json.dumps(recall))
                secret_value = secret.split("=", 1)[-1].split(": ", 1)[-1]
                for f in (self.repo / '.memlet').rglob('*'):
                    if f.is_file():
                        self.assertNotIn(secret_value.encode(), f.read_bytes(), str(f))
        from memlet.memlet_brief import SECRET_MASK_FAMILIES
        self.assertTrue(any('Stripe' in f for f in SECRET_MASK_FAMILIES))
        from memlet import memlet_brief as brief
        import re
        oracle = re.compile(brief._EXTRA_SECRET_PATTERN, re.I)
        for sample in ('prefix_KEY=value12345 suffix', '한TOKEN=value12345',
                       'a' * 10000 + 'KEYTOKEN: value12345', 'TOKENsuffix=nope TOKEN: value12345',
                       'PuTTY-User-Key-File-2: x\n' * 2000, 'aws_secret_' * 2000,
                       'SECRET =\nvalue12345', 'KEY=value12345TOKEN=value67890'):
            self.assertEqual(oracle.sub('#', sample), brief._EXTRA_SECRET_RE.sub('#', sample))

    def test_s5_legacy_capsule_has_distinct_reason(self):
        path = self.repo / 'old.md'
        path.write_text('# Memlet Handover\n\n## Memlet Handover Capsule\n- Signature: old\n')
        result = self.cli('handover', '--verify', str(path), '--format', 'json')
        self.assertEqual(1, result.returncode)
        self.assertIn('legacy capsule: no text checksum to verify', json.loads(result.stdout)['reason'])

    def test_s6_sarif_stdout_is_document_and_action_api_preserved(self):
        self.drift()
        result = self.cli('check', '.', '--format', 'sarif')
        self.assertEqual(1, result.returncode)
        doc = json.loads(result.stdout)
        self.assertEqual('2.1.0', doc['version'])
        self.assertIn('runs', doc)
        self.assertNotIn('sarif', doc)
        self.assertEqual('2.1.0', audit_repository(self.repo, output_format='sarif')['sarif']['version'])


    def test_s8_task_error_is_english(self):
        result = self.cli('--root', str(self.tmp / 'store'), '--task', 'no/such', 'admin', 'brief')
        self.assertEqual(1, result.returncode)
        self.assertIn('Task not found', result.stderr)
        self.assertNotRegex(result.stderr, '[가-힣]')

    def test_s9_real_empty_inspector_is_honest(self):
        from memlet.memlet_inspector import build_inspector_payload, render_inspector_html
        (self.repo / 'CLAUDE.md').write_text('# Rules\n- Be careful.\n')
        shutil.rmtree(self.repo / '.memlet')
        mw.init_project(self.repo)
        payload = build_inspector_payload(str(self.repo))
        self.assertEqual('live', payload['defaultWorkspace'])
        self.assertEqual({'live'}, set(payload['workspaces']))
        self.assertEqual(0, payload['workspaces']['live']['anchorSummary']['total'])
        html = render_inspector_html(payload)
        self.assertNotIn('SIGNED', html)
        self.assertNotIn('Every rule passes the AST gate', html)
        self.assertNotIn('Token count and signature', html)
        self.assertNotIn("textContent='signature '", html)
        self.assertIn('unchanged since pinned', html)
        self.assertIn('app/billing.py::charge', html)
        self.assertEqual({'demo'}, set(build_inspector_payload()['workspaces']))

    def test_s10_long_quote_and_anchors_survive_review_and_promotion(self):
        quote = 'RULE: Never charge before review. ' + 'Read the documented conditions carefully. ' * 20 + 'Use `app/billing.py::charge`.'
        ids = self.learn([quote])
        pending = mb.learn_pending(self.repo)
        self.assertEqual(quote, pending['pendingNodes'][0]['quote'])
        self.assertIn(quote, pending['formatted'])
        self.approve(ids)
        self.assertIn(quote, (self.repo / mb.PROMOTED_PAGE).read_text())
        self.assertIn('workaround draft', md.category_label('verified_workaround'))
        self.assertNotIn('verified', md.category_label('verified_workaround'))
        self.assertTrue(all('verified' not in e['topic'] for e in mb.promoted_rule_entries(self.repo)[0]))
        long = {'primaryPin': {'quoteText': 'x' * 9000}}
        self.assertEqual(8193, len(mb.lesson_text(long)))
        self.assertTrue(mb.lesson_text(long).endswith('…'))

    def test_s11_annotation_reasons_python_and_typescript(self):
        for name, before, after in [('typed.py', 'def charge(a: str):\n    return a\n', 'def charge(a: int):\n    return a\n'),
                                    ('varargs.py', 'def charge(*a: str, **kw: str):\n    return a\n',
                                     'def charge(*a: int, **kw: int):\n    return a\n'),
                                    ('typed.ts', 'export function charge(user: string) { return user; }\n',
                                     'export function charge(user: number) { return user; }\n')]:
            with self.subTest(language=name):
                (self.repo / name).write_text(before)
                (self.repo / 'CLAUDE.md').write_text('- Use `' + name + '::charge` for charge requests.\n')
                mw.init_project(self.repo)
                (self.repo / name).write_text(after)
                anchor = next(a for a in mw.pinned_anchor_report(self.repo)['anchors'] if a['anchor'] == name + '::charge')
                self.assertEqual('compatible-unverified', anchor['status'])
                self.assertEqual('parameter_type_or_annotation_changed_signature_compatible', anchor['reason'])

    def test_s11_legacy_pins_report_only_the_change_they_can_prove(self):
        path = self.repo / 'app/billing.py'
        path.write_text('def charge(user_id: str, amount):\n    return amount\n')
        mw.init_project(self.repo, force=True)
        config_path = self.repo / mw.CONFIG_PATH
        config = json.loads(config_path.read_text())
        for pin in config['pinnedAnchors'].values():
            pin.pop('annotationHash', None)
            pin.pop('parameterShapeHash', None)
        config_path.write_text(json.dumps(config))
        path.write_text('def charge(user_id: int, amount):\n    return amount\n')
        result = mw.pinned_anchor_report(self.repo)['anchors'][0]
        self.assertEqual('compatible-unverified', result['status'])
        self.assertEqual('code_changed_signature_compatible', result['reason'])


    def test_s12_first_run_help_mcp_and_missing_file(self):
        empty = mb.learn_pending(self.repo)
        self.assertIn('memlet learn --transcript PATH', empty['formatted'])
        recall = mb.unified_recall(self.repo, 'charge')
        self.assertIn('memlet admin index-build', recall['formatted'])
        self.assertIn('model call', recall['formatted'])
        self.assertIn('already being used', recall['formatted'])
        self.assertIn('memlet admin wiki', mw.init_project(self.repo)['next'])
        for args, expected in [(('check', '--help'), 'usage: memlet check'),
                               (('admin', 'wiki', '--help'), 'usage: memlet admin wiki'),
                               (('admin', 'audit', '--help'), 'usage: memlet admin audit'),
                               (('admin', 'check', '--help'), 'usage: memlet admin check')]:
            self.assertIn(expected, self.cli(*args).stdout)
        with self.assertRaisesRegex(ValueError, 'Unknown arguments: trasncript_path'):
            self.server().call_tool('memlet_learn', {'repo_path': str(self.repo), 'action': 'transcript', 'trasncript_path': 'x'})
        result = self.cli('handover', '--verify', str(self.repo / 'absent.md'))
        self.assertEqual(1, result.returncode)
        self.assertEqual('', result.stdout)
        self.assertFalse(json.loads(result.stderr)['ok'])

    def test_s12_standalone_recall_notices_legacy_store(self):
        root = str(self.tmp / 'legacy-store')
        captured = self.cli('--root', root, '--standalone', '--task', 'standalone/main',
                            'capture', '--text', 'Decision: use a sqlite ledger.')
        self.assertEqual(0, captured.returncode, captured.stderr)
        recalled = self.cli('--root', root, '--standalone', 'recall', 'sqlite ledger')
        self.assertEqual(0, recalled.returncode, recalled.stderr)
        self.assertIn('standalone/main', recalled.stderr)

    def test_s3_cli_revoke_and_explicit_reconsider(self):
        ids = self.learn(['RULE: Never charge without review.'])
        self.approve(ids)
        result = self.cli('learn', '--reject', ids[0], '--by', 'Reviewer', '--reason', 'outdated')
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        result = self.cli('learn', '--approve', ids[0], '--by', 'Reviewer', '--reconsider')
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)

    def test_s3_admin_revocation_updates_managed_promoted_page(self):
        ids = self.learn(['RULE: Never charge before review.'])
        self.approve(ids)
        queue = str(self.repo / mb.QUEUE_REL)
        result = self.cli('admin', 'dream-reject', '--graph', queue, ids[0], '--by', 'Reviewer')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn(ids[0], (self.repo / mb.PROMOTED_PAGE).read_text())
        self.assertEqual([], mb.unified_recall(self.repo, 'charge')['sections']['dream'])
        result = self.cli('admin', 'dream-approve', '--graph', queue, ids[0], '--by', 'Reviewer', '--reconsider')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(ids[0], (self.repo / mb.PROMOTED_PAGE).read_text())


    def test_s2_failed_refresh_batch_does_not_change_any_item(self):
        ids = self.learn(['RULE: Never charge before review.', 'RULE: Never omit refunds.'])
        self.approve(ids)
        q = self.repo / mb.QUEUE_REL
        graph = json.loads(q.read_text())
        pin = graph['nodes'][0]['primaryPin']
        path = self.repo / mb.SNAPSHOT_REL / pin['fileId']
        lines = path.read_text().splitlines()
        lines[0] = '[user] RULE: Never charge before careful review.'
        lines[1] = ''
        path.write_text('\n'.join(lines) + '\n')
        before = q.read_bytes()
        result = mb.learn_approve(self.repo, ids, approver='Reviewer', refresh_citations=True)
        self.assertFalse(result['ok'])
        self.assertEqual([], result['applied'])
        self.assertEqual(before, q.read_bytes())


    def test_s14_node_approval_digest_binds_all_pin_identity(self):
        ids = self.learn(['RULE: Never charge without review.'])
        self.approve(ids)
        node = json.loads((self.repo / mb.QUEUE_REL).read_text())['nodes'][0]
        digest = md.node_approval_digest(node)
        for field in ('primaryPin', 'allPins', 'supersededByPin'):
            changed = copy.deepcopy(node)
            pin = copy.deepcopy(node['primaryPin'])
            pin['sha256'] = 'f' * 64
            if field == 'allPins':
                changed[field] = [pin]
            else:
                changed[field] = pin
            self.assertNotEqual(digest, md.node_approval_digest(changed), field)
            self.assertEqual('pending', md.approval_state(changed, 'node'), field)

    def test_s14_literal_transcript_limits_enforced(self):
        # Literal thresholds intentionally do not derive from the implementation constants.
        self.assertEqual(16 * 1024, mb.TRANSCRIPT_MAX_TEXT_CHARS)
        self.assertEqual(16 * 1024 * 1024, mb.TRANSCRIPT_MAX_FILE_BYTES)
        line = self.repo / 'oversized-line.log'
        line.write_text('RULE: Never ' + 'a' * (16 * 1024) + '\n')
        result = mb.learn_from_transcript(self.repo, line)
        self.assertFalse(result.get('addedNodeIds'))
        big = self.repo / 'oversized-file.log'
        with big.open('wb') as f:
            f.write(b'RULE: Never accept this oversized transcript.\n')
            f.seek(16 * 1024 * 1024)
            f.write(b'x')
        with patch.object(Path, 'read_text', side_effect=AssertionError('must reject before read')):
            result = mb.learn_from_transcript(self.repo, big)
        self.assertFalse(result['ok'])
        self.assertIn('size bound', result['error'])

    def test_s14_suffix_restriction_bridge_and_mcp(self):
        path = self.repo / 'wrong.txt'
        path.write_text('RULE: Never ingest this unsupported extension.\n')
        self.assertFalse(mb.learn_from_transcript(self.repo, path)['ok'])
        with self.assertRaisesRegex(ValueError, r'\.jsonl or \.log'):
            self.server().call_tool('memlet_learn', {'repo_path': str(self.repo), 'action': 'transcript',
                                                   'transcript_path': str(path)})


if __name__ == '__main__':
    unittest.main()
