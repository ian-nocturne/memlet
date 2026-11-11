"""9.1.1 writer contract, stdlib/injected runners only; no Routlet dependency.

Contract references are to the reader's scripts/memlet_bridge.py inspected on
2026-11-11 (line citations also live next to the production projections).
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
from unittest import mock

import test_v91_integration as integration
from memlet import handover_bridge as hb, memlet_bridge as mb, memlet_dreamcycle as dc
from memlet import memlet_wiki as mw, memlet_dream as md, vendor_cli as vc, memlet_hooks as mh
from memlet import cli_dream, memlet_cortex
from memlet.vocabulary import SAME_VENDOR_REVIEW, BRIDGE_TRUNCATED


class Fixture(integration.Scratch):
    kinds = integration.ApprovedReadersIntegration.kinds
    fixture = integration.ApprovedReadersIntegration.fixture
    save_graph = integration.ApprovedReadersIntegration.save_graph

    def agreement(self, repo, mode):
        cfg = json.loads((repo / mw.CONFIG_PATH).read_text())
        cfg['dream']['agreement'] = mode
        mw.write_managed_text(repo, mw.CONFIG_PATH, json.dumps(cfg))

    def run_check(self, repo, verdict='SUPPORTED'):
        calls = []
        def runner(vendor, prompt, **kw):
            calls.append((vendor, prompt, kw))
            self.assertEqual(dc.VERIFY_PROMPT, prompt)
            return json.dumps({'verdict': verdict})
        report = dc.dream_run(repo, verify_with='claude', runner=runner)
        self.assertTrue(report['ok'], report)
        return report, calls

    def bridge(self, repo, **kw):
        report = mw.build_handover(repo, write=True, budget=2000, **kw)
        self.assertTrue(report['ok'], report)
        hand = json.loads((repo / hb.HANDOVER_FILE).read_text())
        caps = json.loads((repo / hb.CAPSULES_FILE).read_text())
        return hand, caps, report


class SameVendorReview(Fixture):
    def test_real_receipts_tier_table_switches_both_directions(self):
        repo, nodes = self.fixture('pending', ('BLOCKS',))
        self.agreement(repo, 'vendor')
        report, calls = self.run_check(repo)
        self.assertEqual(1, len(calls))
        self.assertEqual(1, report['checkedItems'])
        self.assertIn('0 became hints', report['verificationSummary'])
        self.assertIn('dream.agreement: "vendor"', cli_dream._markdown(report))
        node = mb._load_queue(repo)[0]['nodes'][0]
        self.assertEqual('SUPPORTED', node['verification'][0]['verdict'])
        self.assertTrue(dc.trusted_record(node['verification'][0], node))
        before = (repo / mb.QUEUE_REL).read_bytes()
        for mode, tier in [('vendor', 'pending'), ('model', 'hint'), ('vendor', 'pending'), ('model', 'hint')]:
            self.agreement(repo, mode)
            item = mb.dream_items(repo, tiers=('pending','hint'))['items'][0]
            self.assertEqual(tier, item['tier'])
        self.assertEqual(before, (repo / mb.QUEUE_REL).read_bytes())
        self.assertEqual(0, self.run_check(repo)[0]['checkedItems'])

    def test_same_family_is_refused_in_both_modes(self):
        repo, _ = self.fixture('pending', ('BLOCKS',))
        for mode in ('vendor','model'):
            self.agreement(repo, mode)
            cfg = json.loads((repo / mw.CONFIG_PATH).read_text())
            cfg['dream']['models']['verifier']['claude'] = 'claude-haiku-4-5'
            mw.write_managed_text(repo, mw.CONFIG_PATH, json.dumps(cfg))
            with mock.patch.object(vc, 'run_vendor', side_effect=AssertionError('no launch')):
                report = dc.dream_run(repo, verify_with='claude', dry_run=True)
            self.assertFalse(report['ok'])
            self.assertEqual('dream.models.verifier must differ from every proposer model under dream.agreement=model', report['error'])

    def test_review_labels_pending_status_cortex_inspector(self):
        repo, _ = self.fixture('pending', ('BLOCKS',))
        self.agreement(repo, 'vendor')
        self.run_check(repo)
        self.assertIn(SAME_VENDOR_REVIEW, mb.learn_pending(repo)['formatted'])
        status = dc.dream_status(repo)
        self.assertEqual(SAME_VENDOR_REVIEW, status['items'][0]['agreementLabel'])
        self.assertIn(SAME_VENDOR_REVIEW, cli_dream._markdown(status))
        lineage = memlet_cortex._load_dream_lineage({'path': str(repo)})
        self.assertEqual(SAME_VENDOR_REVIEW, lineage['nodes'][0]['agreementLabel'])
        output = self.cli('dream', str(repo), '--status', '--format', 'markdown')
        self.assertEqual(0, output.returncode, output.stderr)
        self.assertIn(SAME_VENDOR_REVIEW, output.stdout)
        output = self.cli('ui', str(repo))
        self.assertEqual(0, output.returncode, output.stderr)
        self.assertIn('review aid', (repo / '.memlet/inspector.html').read_text())

    def test_agent_facing_outputs_byte_identical_supported_and_veto(self):
        for verdict in ('SUPPORTED','MISSING_CONDITION'):
            repo, _ = self.fixture('pending', ('BLOCKS',))
            # Move the fixture so both cases keep the existing fixture builder.
            renamed = repo.with_name('support-case' if verdict == 'SUPPORTED' else 'veto-case')
            repo.rename(renamed); repo = renamed
            self.agreement(repo, 'vendor')
            def outputs():
                recall = mb.unified_recall(repo, 'deploy cache', budget=4000)
                handover = mw.build_handover(repo, output_format='markdown', budget=2000)
                changed = mb.changed_check(repo, files=['deploy.py'])
                texts = [recall['formatted'], handover['formatted'], changed['formatted'],
                         json.dumps([recall,handover,changed], ensure_ascii=False, sort_keys=True),
                         json.dumps(mh._session_start(repo, {}, {'dream': True, 'tokens': 2000}), sort_keys=True)]
                for name, args in [('memlet_recall', {'query':'deploy cache','budget':4000}),
                                   ('memlet_handover', {'budget':2000})]:
                    result = self.server.handle_rpc({'jsonrpc':'2.0','id':1,'method':'tools/call',
                        'params':{'name':name,'arguments':dict(repo_path=str(repo), **args)}})
                    texts.append(result['result']['content'][0]['text'])
                return [s.encode('utf-8') for s in texts]
            before = outputs()
            self.run_check(repo, verdict)
            self.assertEqual(before, outputs(), verdict)
            for text in outputs():
                self.assertNotIn(verdict.encode(), text)

    def test_veto_stays_pending_under_each_setting(self):
        repo, _ = self.fixture('pending', ('BLOCKS',))
        self.agreement(repo, 'vendor')
        self.run_check(repo, 'MISSING_CONDITION')
        for mode in ('vendor','model','vendor'):
            self.agreement(repo, mode)
            item = mb.dream_items(repo, tiers=('pending','hint'))['items'][0]
            self.assertEqual('pending', item['tier'])
            self.assertIn('vetoed by claude/sonnet: MISSING_CONDITION', item['agreementLabel'])
            self.assertIn('MISSING_CONDITION', cli_dream._markdown(dc.dream_status(repo)))

    def test_preview_existing_and_future_verifier_calls_without_launch(self):
        repo, _ = self.fixture('pending', ('BLOCKS',))
        for mode in ('vendor','model'):
            self.agreement(repo, mode)
            before = (repo / mb.QUEUE_REL).read_bytes()
            with mock.patch.object(vc, 'run_vendor', side_effect=AssertionError('no call')):
                report = dc.dream_run(repo, verify_with='claude', dry_run=True)
            self.assertTrue(report['ok'], report)
            self.assertEqual(1, report['plannedExistingVerificationCalls'])
            self.assertEqual('verify', report['prompts'][0]['kind'])
            self.assertEqual('sonnet', report['verifierModel'])
            self.assertEqual(before, (repo / mb.QUEUE_REL).read_bytes())
        source = self.write('fresh.log', 'Never retry the broken deploy.py path.\n')
        self.agreement(repo, 'vendor')
        report = dc.dream_run(repo, verify_with='claude', dry_run=True, transcript=source)
        self.assertTrue(report['ok'], report)
        self.assertGreater(report['verificationCallRange'][1], report['verificationCallRange'][0])

    def test_vendor_refusals_unchanged(self):
        repo, _ = self.fixture('pending', ('BLOCKS',))
        self.agreement(repo, 'vendor')
        for vendor in ('codex','gemini'):
            with mock.patch.object(vc, 'run_vendor', side_effect=AssertionError('no call')):
                report = dc.dream_run(repo, verify_with=vendor, dry_run=True)
            self.assertEqual(vc.static_vendor_refusal(vendor), report['error'])


class BridgeContract(Fixture):
    def test_eight_kind_six_state_table(self):
        for state in ('approved','hint','pending','rejected','edge-rejected','vetoed'):
            repo, nodes = self.fixture(state)
            hand, caps, report = self.bridge(repo)
            payload = json.dumps([hand, caps], ensure_ascii=False)
            self.assertNotIn('verification', payload)
            self.assertNotIn('MISSING_CONDITION', payload)
            entries = {e['id']: e for e in caps['capsules']}
            rejects = {e['id']: e for e in hand['rejectedApproaches']}
            for kind, node in nodes.items():
                nid = node['id']
                with self.subTest(state=state, kind=kind):
                    if state != 'approved':
                        self.assertNotIn(nid, payload)
                    elif kind == 'BLOCKS':
                        self.assertNotIn(nid, entries)
                        self.assertEqual('approved BLOCKS lesson', rejects[nid]['reason'])
                    elif kind == 'SUPERSEDES':
                        self.assertIn(nid, entries)
                        self.assertIn(nid+'-old', rejects)
                        self.assertEqual('replaced — do not apply as current', rejects[nid+'-old']['reason'])
                        self.assertNotIn('Deploys go out with', json.dumps(caps))
                        self.assertNotIn('Deploys go out with', json.dumps(hand['activeConstraints']))
                    else:
                        self.assertIn(nid, entries)
                        self.assertNotIn(nid, rejects)
                    if state == 'approved':
                        entry = rejects[nid] if kind == 'BLOCKS' else entries[nid]
                        self.assertIn('approved', entry['tags'])
                        self.assertTrue(entry['cites'])
                        self.assertIn('.memlet/dream/snapshot/', entry['source'])
                        if kind != 'BLOCKS': self.assertEqual(['deploy.py'], entry['paths'])
            self.assertEqual(1, len(hand['activeConstraints']))
            self.assertEqual([], report['filesNotWritten'])
            self.assertTrue(mw.verify_handover_checksum(report))

    def test_exact_keys_types_and_integrity(self):
        repo, _ = self.fixture('approved')
        hand, caps, _ = self.bridge(repo)
        # Reader L493-558: head + three lists; L50-57 requires the capsules list.
        self.assertEqual({'schema_version','repoHead','updated_at','authorityBoundary','activeConstraints',
                          'rejectedApproaches','quarantinedStaleAnchors','omitted','integritySha256'}, set(hand))
        self.assertEqual({'schema_version','capsules','omitted','integritySha256'}, set(caps))
        self.assertEqual('memlet.handover_bridge.v1', hand['schema_version'])
        self.assertEqual('memlet.capsules.v1', caps['schema_version'])
        self.assertEqual(mw.AUTHORITY_BOUNDARY, hand['authorityBoundary'])
        self.assertRegex(hand['repoHead'], r'^[a-f0-9]{40}$')
        self.assertRegex(hand['updated_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertTrue(hb.verify(hand))
        self.assertEqual(hb.integrity(caps), caps['integritySha256'])
        for e in hand['activeConstraints']:
            # Reader L516-550 consumes id/rule/anchor_status/paths and anchor on drift.
            self.assertEqual({'id','anchor','anchor_status','paths','rule','source','tags','truncated'}, set(e))
            for k in ('id','anchor','anchor_status','rule','source'): self.assertIsInstance(e[k], str)
            self.assertIsInstance(e['truncated'], bool)
            self.assertTrue(all(isinstance(p, str) for p in e['paths']))
        for e in hand['rejectedApproaches']:
            # Reader L553-555 consumes strings id/approach/reason/source.
            self.assertEqual({'id','approach','reason','source','cites','tags','truncated'}, set(e))
            for k in ('id','approach','reason','source'): self.assertIsInstance(e[k], str)
            self.assertTrue(all(set(c) == {'file','start','end','sha256'} for c in e['cites']))
            self.assertTrue(all(type(c['start']) is int and type(c['end']) is int for c in e['cites']))
        for e in caps['capsules']:
            # Reader L121-138 and L166-171 score paths/tags and emit summary/time.
            self.assertEqual({'id','paths','tags','summary','updated_at','source','truncated'} |
                             ({'cites'} if 'cites' in e else set()), set(e))
            for k in ('id','summary','updated_at','source'): self.assertIsInstance(e[k], str)
            self.assertTrue(all(isinstance(v, str) for v in e['paths']+e['tags']))
        corrupted = copy.deepcopy(hand); corrupted['activeConstraints'][0]['rule'] += ' changed'
        self.assertFalse(hb.verify(corrupted))

    def test_rule_states_and_accepted_provenance(self):
        self.anchors()
        for state, code in [('active', integration.CODE), ('compatible-unverified', integration.CODE.replace('bool(token)','not bool(token)')),
                            ('stale', integration.CODE.replace('(token)', '(token, issuer)', 1))]:
            self.write('auth.py', code)
            hand, caps, _ = self.bridge(self.root)
            if state == 'stale':
                self.assertEqual([], hand['activeConstraints'])
                self.assertEqual([], caps['capsules'])
                q = hand['quarantinedStaleAnchors'][0]
                self.assertEqual({'anchor','path','status','reason','tags','truncated'}, set(q))
                self.assertEqual('stale', q['status'])
            else:
                self.assertEqual(2, len(hand['activeConstraints']))
                for e in hand['activeConstraints']:
                    self.assertEqual(state, e['anchor_status'])
                    self.assertEqual(['auth.py'], e['paths'])
                    self.assertEqual('ACCEPTED' in e['rule'], 'suggested-anchor' in e['tags'])
                self.assertEqual(2, len(caps['capsules']))
        self.write('auth.py', integration.CODE)
        self.write('extra.py', 'def extra():\n    return True\n')
        self.write('CLAUDE.md', '- UNPINNED `extra.py::extra`.\n- PATHONLY `extra.py`.\n- UNANCHORED text.\n')
        hand, caps, _ = self.bridge(self.root)
        for word in ('UNPINNED','PATHONLY','UNANCHORED'):
            self.assertNotIn(word, json.dumps([hand, caps]))
        self.assertEqual(3, hand['omitted']['ineligibleRules'])

    def test_mixed_bad_anchor_cannot_carry_rule(self):
        self.anchors()
        self.write('extra.py', 'def extra():\n    return True\n')
        self.write('CLAUDE.md', '- MIXED `auth.py::validate_token` and `extra.py::extra`.\n')
        self.write('auth.py', integration.CODE.replace('bool(token)','not bool(token)'))
        hand, caps, _ = self.bridge(self.root)
        self.assertNotIn('MIXED', json.dumps([hand, caps]))

    def test_only_write_creates_bridge_and_other_readers_do_not_refresh(self):
        repo, _ = self.fixture('approved')
        def reads():
            mw.build_handover(repo)
            mb.unified_recall(repo, 'deploy')
            mb.changed_check(repo, files=['deploy.py'])
            dc.dream_status(repo)
            mh._session_start(repo, {}, {'dream': True, 'tokens': 2000})
            for name, extra in [('memlet_handover', {}), ('memlet_recall', {'query':'deploy'})]:
                self.server.handle_rpc({'jsonrpc':'2.0','id':1,'method':'tools/call',
                    'params':{'name':name,'arguments':dict(repo_path=str(repo), **extra)}})
        reads()
        for rel in (hb.HANDOVER_FILE,hb.CAPSULES_FILE): self.assertFalse((repo/rel).exists())
        self.bridge(repo)
        before = {rel: (repo/rel).read_bytes() for rel in (hb.HANDOVER_FILE,hb.CAPSULES_FILE)}
        reads()
        self.assertEqual(before, {rel: (repo/rel).read_bytes() for rel in before})
        self.assertEqual(0, self.cli('handover', str(repo)).returncode)
        self.assertEqual(before, {rel: (repo/rel).read_bytes() for rel in before})

    def test_no_commit_is_unverified(self):
        self.anchors()
        hand, _, _ = self.bridge(self.root)
        self.assertEqual('unverified', hand['repoHead'])

    def test_verbatim_not_title_and_explicit_truncation(self):
        repo, nodes = self.fixture('approved', ('RESOLVES','BLOCKS'))
        hand, caps, _ = self.bridge(repo)
        lessons = [e for e in caps['capsules'] if 'approved' in e['tags']]
        self.assertEqual(1, len(lessons))
        self.assertTrue(lessons[0]['truncated'])
        self.assertLessEqual(len(lessons[0]['summary']), 120)
        self.assertTrue(lessons[0]['summary'].endswith(BRIDGE_TRUNCATED))
        self.assertTrue(hand['rejectedApproaches'][0]['approach'].endswith(BRIDGE_TRUNCATED))
        self.assertLessEqual(len(hand['rejectedApproaches'][0]['approach']), 110)
        self.assertTrue(lessons[0]['summary'].startswith('Deploy RESOLVES requires'))
        self.assertNotEqual('RESOLVES', lessons[0]['summary'])
        self.assertEqual(2, len(lessons[0]['cites']))
        # Recover the complete instruction beyond the parser's 300-char display slice.
        self.write('auth.py', integration.CODE)
        rule = 'Keep `auth.py::validate_token` enabled. ' + 'Preserve every condition. ' * 50
        self.write('AGENTS.md', '- ' + rule + '\n')
        mw.init_project(self.root)
        hand, caps, _ = self.bridge(self.root)
        self.assertTrue(hand['activeConstraints'][0]['truncated'])
        self.assertEqual('AGENTS.md:1', caps['capsules'][0]['source'])
        self.assertTrue(rule.startswith(caps['capsules'][0]['summary'].split(BRIDGE_TRUNCATED)[0]))

    def test_lesson_paths_exist_and_are_literal(self):
        self.write('deploy.py', 'pass\n')
        self.write('src/existing.py', 'pass\n')
        (self.root/'escape.py').symlink_to(self.base/'outside.py')
        self.assertEqual(['deploy.py','src/existing.py'], hb._paths(self.root, 'deploy.py',
            'Use `src/existing.py` with nonexistent.py, escape.py, ../outside.py.'))
        self.assertEqual([], hb._paths(self.root, 'not-there.py', 'No file named.'))

    def test_mask_then_omit_residual_secrets_before_clipping(self):
        self.write('auth.py', integration.CODE)
        self.write('AGENTS.md', '- MASKED `auth.py::validate_token` sk-' + 'A'*60 + '.\n'
                   '- DROP `auth.py::validate_token` Bearer placeholder.\n'
                   '- SHORT `auth.py::validate_token` sk-abcdefgh.\n'
                   '- HIDDEN `auth.py::validate_token` sk-abc\u200bdefgh.\n'
                   '- LATE `auth.py::validate_token` ' + 'safe words '*50 + ' Bearer placeholder.\n')
        mw.init_project(self.root)
        hand, caps, _ = self.bridge(self.root)
        payload = json.dumps([hand,caps])
        self.assertNotIn('A'*60, payload)
        self.assertNotIn('Bearer', payload)
        self.assertNotIn('sk-abcdefgh', payload)
        self.assertGreaterEqual(hand['omitted']['secretEntries'], 3)
        self.assertGreaterEqual(caps['omitted']['secretEntries'], 3)
        self.assertTrue(any('MASKED' in e['summary'] for e in caps['capsules']))

    def test_caps_omissions_and_encoded_byte_bound(self):
        self.write('auth.py', integration.CODE)
        self.write('AGENTS.md', ''.join('- Rule %d `auth.py::validate_token` 한글.\n' % n for n in range(70)))
        mw.init_project(self.root)
        hand, caps, _ = self.bridge(self.root)
        self.assertEqual(40, len(hand['activeConstraints']))
        self.assertEqual(30, hand['omitted']['activeConstraints'])
        self.assertEqual(40, len(caps['capsules']))
        self.assertEqual(30, caps['omitted']['capsules'])
        with mock.patch.object(hb, 'MAX_BYTES', 4000):
            hand, caps, _ = self.bridge(self.root)
            for rel in (hb.HANDOVER_FILE,hb.CAPSULES_FILE):
                self.assertLessEqual((self.root/rel).stat().st_size, 4000)
        self.assertGreater(hand['omitted']['activeConstraints'], 30)
        self.assertGreater(caps['omitted']['capsules'], 30)
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            hb.write(self.root, hb.CAPSULES_FILE, 'a'*(hb.MAX_BYTES+1))

    def test_determinism_stable_ids_order_and_full_replacement(self):
        repo, _ = self.fixture('approved')
        with mock.patch.object(mw, '_utc_now', return_value='2026-11-11T00:00:00Z'):
            hand, caps, _ = self.bridge(repo)
            before = {r: (repo/r).read_bytes() for r in (mw.HANDOVER_PAGE,hb.HANDOVER_FILE,hb.CAPSULES_FILE)}
            with mock.patch.object(mw, 'write_managed_text', wraps=mw.write_managed_text) as writer:
                self.bridge(repo)
            self.assertEqual(set(before), {c.args[1] for c in writer.call_args_list})
            self.assertEqual(before, {r: (repo/r).read_bytes() for r in before})
        with mock.patch.object(mw, '_utc_now', return_value='2026-11-12T00:00:00Z'):
            hand2,caps2,_ = self.bridge(repo)
        def normalized(doc):
            doc = copy.deepcopy(doc); doc.pop('integritySha256')
            doc.pop('updated_at', None)
            for e in doc.get('capsules', []): e.pop('updated_at', None)
            return doc
        self.assertEqual(normalized(hand), normalized(hand2))
        self.assertEqual(normalized(caps), normalized(caps2))
        original_id = caps['capsules'][0]['id']
        text = (repo/'AGENTS.md').read_text()
        (repo/'AGENTS.md').write_text('- Added `anchor.py::guard` rule.\n'+text)
        hand3,caps3,_ = self.bridge(repo)
        self.assertIn(original_id, [e['id'] for e in caps3['capsules']])
        (repo/'AGENTS.md').write_text(text)
        for rel in (mw.HANDOVER_PAGE,hb.HANDOVER_FILE,hb.CAPSULES_FILE):
            (repo/rel).write_text('old incomplete write')
        self.bridge(repo)
        self.assertTrue(hb.verify(json.loads((repo/hb.HANDOVER_FILE).read_text())))

    def test_partial_failure_reports_each_file_and_recovers(self):
        self.anchors()
        original = mw.write_managed_text
        paths = [mw.HANDOVER_PAGE,hb.HANDOVER_FILE,hb.CAPSULES_FILE]
        for i, target in enumerate(paths):
            for rel in paths:
                if (self.root/rel).exists(): (self.root/rel).unlink()
            def fail(root, rel, text, **kw):
                if rel == target: raise OSError('injected write failure')
                return original(root, rel, text, **kw)
            with mock.patch.object(mw, 'write_managed_text', side_effect=fail):
                report = mw.build_handover(self.root, write=True, budget=2000)
            self.assertFalse(report['ok'], target)
            self.assertEqual(paths[:i], report['filesWritten'])
            self.assertEqual(paths[i:], report['filesNotWritten'])
            self.assertIn('written: '+(', '.join(paths[:i]) or 'none'), report['error'])
            for rel in paths: self.assertEqual(rel in paths[:i], (self.root/rel).exists())
            self.bridge(self.root)

    def test_symlink_and_containment_refusals(self):
        self.anchors()
        outside = self.base/'outside.json'; outside.write_text('untouched')
        for rel in (hb.HANDOVER_FILE,hb.CAPSULES_FILE,mw.HANDOVER_PAGE):
            path = self.root/rel
            if path.exists(): path.unlink()
            path.symlink_to(outside)
            report = mw.build_handover(self.root, write=True)
            self.assertFalse(report['ok'])
            self.assertEqual([], report['filesWritten'])
            self.assertEqual('untouched', outside.read_text())
            path.unlink()
        with self.assertRaises(mw.UnsafePathError): hb.write(self.root, '../outside.json', '{}')
        with self.assertRaises(mw.UnsafePathError): hb.write(self.root, '.memlet/../outside.json', '{}')
        (self.root/'.memlet').rename(self.root/'saved')
        (self.root/'.memlet').symlink_to(self.root/'saved', target_is_directory=True)
        report = mw.build_handover(self.root, write=True)
        self.assertFalse(report['ok'])
        self.assertEqual([], report['filesWritten'])

    def test_cli_verify_bridge_and_legacy_capsules(self):
        self.anchors()
        hand,caps,report = self.bridge(self.root)
        report_file = self.write('capsule.json', json.dumps(report))
        for rel in (hb.HANDOVER_FILE,mw.HANDOVER_PAGE,str(report_file)):
            result = self.cli('handover', '--verify', str(self.root/rel), '--format','json')
            self.assertEqual(0, result.returncode, result.stdout+result.stderr)
        hand['activeConstraints'][0]['rule'] += ' corrupt'
        self.write(hb.HANDOVER_FILE, json.dumps(hand))
        result = self.cli('handover','--verify',str(self.root/hb.HANDOVER_FILE),'--format','json')
        self.assertEqual(1,result.returncode)

    def test_existing_capsule_and_markdown_are_unaffected_by_write(self):
        self.anchors()
        before = mw.build_handover(self.root, output_format='markdown', budget=2000)
        # Measured from 18dc905 in an isolated checkout with this exact fixture;
        # the full normalized JSON + Markdown and all 25 MCP definitions also
        # byte-compared equal in the local release compatibility check.
        self.assertEqual('sha256:23062ca27213378b6dae80cf4f4b909681f5fd39d668375faf15981f194af408', before['checksum'])
        _,_,after = self.bridge(self.root, output_format='markdown')
        self.assertEqual(before['formatted'], after['formatted'])
        for key in before['signedFields'] + ['capsuleSignature','checksum','markdownChecksum']:
            self.assertEqual(before[key], after[key], key)
        self.assertEqual('# Memlet Handover\n\n'+before['formatted'], (self.root/mw.HANDOVER_PAGE).read_text())

    def test_stale_approval_citations_are_never_exported(self):
        repo, nodes = self.fixture('approved', ('single-line',))
        source = next((repo/mb.SNAPSHOT_REL).glob('*.txt'), None)
        if source is None:
            source = next(p for p in (repo/mb.SNAPSHOT_REL).iterdir() if p.is_file())
        source.write_text(source.read_text() + 'not enough to alter earlier lines\n')
        hand,caps,_ = self.bridge(repo)
        self.assertTrue(any('approved' in e['tags'] for e in caps['capsules']))
        source.write_text('changed citation\n')
        hand,caps,_ = self.bridge(repo)
        self.assertFalse(any('approved' in e['tags'] for e in caps['capsules']))
        self.assertEqual([], hand['rejectedApproaches'])

    def test_replaced_source_cannot_reenter_as_current_capsule(self):
        repo,_ = self.fixture('approved', ('SUPERSEDES',))
        lesson = mb.dream_items(repo)['items'][0]
        lesson['replacementLabel'] = 'SOURCE_SUPERSEDED_OR_DROPPED — RE-VERIFY'
        docs = hb.build(repo, [], [], [lesson], None, '2026-11-11T00:00:00Z')
        hand = json.loads(docs[hb.HANDOVER_FILE]); caps = json.loads(docs[hb.CAPSULES_FILE])
        self.assertEqual([], caps['capsules'])
        self.assertEqual(1, caps['omitted']['capsules'])
        self.assertEqual(1, len(hand['rejectedApproaches']))

    def test_writer_and_projection_share_memlet_lock(self):
        self.anchors()
        import contextlib
        held = []
        @contextlib.contextmanager
        def lock(root):
            held.append(root)
            try: yield
            finally: held.pop()
        original_build, original_write = hb.build, hb.write
        def build(*args):
            self.assertTrue(held)
            return original_build(*args)
        def write(*args):
            self.assertTrue(held)
            return original_write(*args)
        with mock.patch.object(mw, 'memlet_write_lock', lock), mock.patch.object(hb,'build',side_effect=build), mock.patch.object(hb,'write',side_effect=write):
            self.bridge(self.root)
        self.assertEqual([], held)

    def test_masked_addresses_are_omitted_not_rerouted(self):
        filename = 'sk-' + 'A'*60 + '.py'
        self.write(filename, 'def guard():\n    return True\n')
        self.write('AGENTS.md', '- Keep `%s::guard` enabled.\n' % filename)
        mw.init_project(self.root)
        hand,caps,_ = self.bridge(self.root)
        self.assertEqual([], hand['activeConstraints'])
        self.assertEqual([], caps['capsules'])
        self.assertEqual(1, caps['omitted']['secretEntries'])
