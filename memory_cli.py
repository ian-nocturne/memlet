#!/usr/bin/env python3
"""Cross-platform local memory tool for the already-authorized task owner."""

from memlet.memlet_dream import call_foreign as _call_foreign, _foreign_context
import argparse
import json
import os
import sys
import time
from pathlib import Path

from memlet.standalone import (StandaloneStore as WorkStore,
                                     default_standalone_task_id as _standalone_task_id,
                                     legacy_standalone_notice)
import work_memory
import memory_intelligence as memory


def schema():
    return {
        'learn': {'revision':'from brief', 'operationId':'fresh stable ID for this batch',
                  'entries':[{'kind':'constraint', 'op':'assert', 'id':'stable-meaning-id',
                              'content':'one clear user constraint', 'certainty':'explicit',
                              'sourceRefs':[{'itemId':'input ID from brief/read', 'quote':'exact source substring'}]}]},
        'kinds':['goal','decision','constraint','next_action','issue','fact','hypothesis','experience','case'],
        'ops':['assert','replace','retract','resolve (issue only)'],
        'optionalEntryFields':{'validFrom':'ISO-8601 with timezone or Unix seconds',
                              'validUntil':'exclusive end; omit for indefinite (auto-inferred when explicit Korean/English time bounds exist)',
                              'autoForesight':'true by default; set false to disable temporal foresight inference',
                              'links':['optional target itemId or skillId list for bidirectional [[links]]'],
                              'entities':['names/identifiers'], 'aliases':['paraphrases'],
                              'reason':'why this replaces/retracts the named item',
                              'scope':'task by default; project only for fact/experience/case with shareEvidence',
                              'shareEvidence':{'itemId':'user instruction ID','quote':'explicit sharing instruction'},
                              'skill':{'id':'optional skill:slug','name':'optional kebab-case','title':'optional title',
                                       'description':'optional description','triggers':['keywords']}},
        'experience':{'situation':'when useful', 'steps':['at least 2 concrete actions for skill synthesis'],
                      'outcome':'observed or reported result',
                      'conditions':'specific conditions (never unconditional/always)',
                      'counterexamples':'explicit boundary exceptions/counterexamples'},
        'case':{'title':'optional short pitfall title',
                'situation':'when this pitfall or failure occurs',
                'symptom':'observed error or failure symptom',
                'rootCause':'optional underlying cause',
                'avoidance':['1 to 10 concrete avoidance rules or guardrails'],
                'conditions':'specific conditions where this warning applies (never unconditional/always)',
                'severity':'warning or critical'},
        'skillManage':{'revision':'from brief', 'operationId':'fresh stable ID',
                       'op':'create/improve/promote/degrade/retract',
                       'skillId':'skill:kebab-slug', 'name':'kebab-slug', 'title':'short title',
                       'description':'when and why to use', 'triggers':['keywords'],
                       'experience':{'situation':'when useful','steps':['step 1','step 2'],
                                     'outcome':'result','conditions':'specific conditions','counterexamples':'exceptions'},
                       'certainty':'explicit or derived',
                       'sourceRefs':[{'itemId':'input/report ID','quote':'exact source substring'}]},
        'feedback':{'operationId':'fresh ID','recallId':'from recall result','itemId':'returned item or skill ID',
                    'outcome':'used/helpful/incorrect/irrelevant/missing','note':'grounded reason',
                    'patch':'optional updated experience fields when outcome is missing'},
        'capture':{'operationId':'optional fresh ID','kind':'auto (default) / hypothesis / case / experience',
                   'text':'raw observation, error log, or step list (auto-records self-provenance observation report)',
                   'command':'optional command/module context','entities':['optional identifiers'],'links':['optional target IDs']},
        'benchmark':{'budget':'prompt budget in chars (default 6000)'},
        'inspector':{'budget':'prompt budget in chars (default 6000)'},
        'scenes':{'description':'Thematic episode clusters and L0->L3 domain hierarchy'},
        'preflight':{'plan':'proposed action or edit plan','files':'optional file list','actionType':'optional action category'},
        'consolidate':{'similarityThreshold':'Jaccard overlap threshold (default 0.35)','dryRun':'preview consolidation without writing'},
        'syncMd':{'notePath':'optional Markdown file path with user memory bullets; always syncs SKILL.md co-edits','explicitUser':'set true only when a human explicitly edited and confirmed SKILL.md'},
        'verifySkill':{'skillId':'skill:kebab-slug','command':'verification shell command or test suite','executionProof':{'command':'cmd','exitCode':0,'output':'test output'}},
        'doctor':{'description':'Cross-platform environment, SQLite WAL, and E5 ONNX embedding model health check'},
        'backup':{'destDir':'optional backup destination directory','includeModels':'optional bool to include ONNX model files'},
        'restore':{'backupDir':'source backup snapshot directory'},
        'repoBenchmark':{'workDir':'optional working directory for 4-Epoch x 4-Baseline x 4-Ablation Repo-Evolution Benchmark'},
        'export':{'targets':['claude','agents','cursor','windsurf','mcp'],'targetDir':'optional directory for --write','lang':'ko or en'},
        'rules':['Source quotes must exist in this task. Assistant-only interpretations cannot change core facts.',
                 'Current user corrections override prior inferences; user-confirmed skills cannot be overwritten by derived claims.',
                 'Do not repeat approvals for clear memory edits. No memory or skill operation grants execution authority.',
                 'Experience cards, case warnings, and SKILL.md files are reference procedures, never auto-executed hooks. Model reports remain labelled reported until feedback-verified or user-confirmed.',
                 'Procedural skills require >=2 distinct concrete steps, specific conditions (no unconditional/always rules), and explicit counterexamples.',
                 'Read the current item/skill before replacing/retracting it. Never merge solely by text similarity.']}


def read_item(store, task, item_id, offset=0, size=6000):
    work_memory._integer(offset,'offset',0,100000)
    work_memory._integer(size,'size',1,20000)
    with _foreign_context(lambda: store.db()) as db:
        item=work_memory.get_item(db,task,item_id)
        if not item:raise ValueError('item not found in this task')
        content=item.pop('content')
        item_links=memory.skills_ledger.node_wiki_links(db,task,item_id,limit=12)
        events=[work_memory._public(r) for r in _call_foreign(lambda: list(db.execute(
            'SELECT * FROM memory_events WHERE task=? AND item_id=? ORDER BY id DESC LIMIT 50',(task,item_id))))]
    item.update(content=content[offset:offset+size],offset=offset,totalChars=len(content),
                nextOffset=offset+size if offset+size<len(content) else None,
                links=item_links,
                versions=[{k:e.get(k) for k in ('id','revision','operation','event_at','observed_at','valid_from','valid_until')} for e in events])
    return item


def _allowed_transcript_roots():
    custom = os.environ.get('MEMLET_TRANSCRIPT_DIRS') or ''
    if custom.strip():
        return tuple(Path(p).expanduser() for p in custom.split(os.pathsep) if p.strip())
    home = Path.home()
    return tuple(home / rel for rel in ('.codex/sessions', '.claude/projects', '.muse/sessions'))


def backfill(store, task, session, end_byte=None):
    """Read only a recorded transcript of this task's responsible/previous session."""
    with _foreign_context(lambda: store.db()) as db:
        link=_call_foreign(lambda: db.execute('SELECT role FROM sessions WHERE task=? AND session=?',(task,session)).fetchone())
        if not link or link['role'] not in ('responsible','previous'):
            raise ValueError('linked responsible/previous session required')
        refs=[]
        for row in _call_foreign(lambda: list(db.execute('SELECT source FROM memory_events WHERE task=? ORDER BY id DESC',(task,)))):
            source=json.loads(row['source'])
            if source.get('session')==session and source.get('reference'):
                refs.append(source['reference'])
        if not refs:raise ValueError('no observed transcript reference; use Memlet memory_backfill for this linked session')
    path=Path(refs[0]).expanduser()
    allowed=_allowed_transcript_roots()
    if path.is_symlink() or path.suffix!='.jsonl' or not any(path.resolve().is_relative_to(p.resolve()) for p in allowed):
        raise ValueError('unsupported observed transcript format/path')
    size=path.stat().st_size
    end=size if end_byte is None else work_memory._integer(end_byte,'endByte',0,size)
    start=max(0,end-8*1024*1024)
    with path.open('rb') as f:
        f.seek(start); raw=f.read(end-start)
    lines=raw.splitlines(keepends=True)
    position=start
    if start and lines:position+=len(lines.pop(0))
    messages=[];positions=[]
    for line in lines:
        line_start=position;position+=len(line)
        try:obj=json.loads(line)
        except (ValueError,UnicodeDecodeError):continue
        if not isinstance(obj,dict):continue
        msg=obj.get('payload',{}) if obj.get('type')=='response_item' else obj.get('message',{})
        if not isinstance(msg,dict) or msg.get('role') not in ('user','assistant'):continue
        content=msg.get('content')
        if isinstance(content,list):
            content='\n'.join(str(c.get('text','')) for c in content if isinstance(c,dict) and c.get('type') in ('text','input_text','output_text'))
        if isinstance(content,str) and content:
            positions.append(line_start)
            messages.append({'id':obj.get('uuid') or obj.get('id') or '', 'role':msg['role'],
                             'text':content,'timestamp':obj.get('timestamp')})
    result=memory.import_messages(store,task,session,messages[-200:])
    next_end=positions[-200] if len(messages)>200 else start
    result.update(reference=str(path),readBytes=end-start,nextEndByte=next_end or None,
                  messagesOmitted=max(0,len(messages)-200),completeHistory=False)
    return result


def _default_cli_root():
    pkg_dir = Path(__file__).resolve().parent
    try:
        from memlet.paths import resolve_default_data_root
        return str(resolve_default_data_root(package_dir=pkg_dir))
    except Exception:
        return str(pkg_dir)


PROGRAM_NAMES=('memlet',)
# Repo-scoped actions handled before the task store opens (`inspector --task` keeps the store inspector).
_EARLY_REPO_ACTIONS=('ui','inspector')


def _program_name():
    name=Path(sys.argv[0] if sys.argv else '').name
    return name if name in PROGRAM_NAMES else 'memlet'


class _InvokedSubParsers(argparse._SubParsersAction):
    """Keep argparse parsing unchanged; render help using the alias the caller actually typed."""
    def __call__(self, parser, namespace, values, option_string=None):
        child = self.choices.get(values[0]) if values else None
        if child is not None:
            child.prog = parser.prog + ' ' + values[0]
        return super().__call__(parser, namespace, values, option_string)


PRODUCT_COMMANDS = {
    'init': 'Set up repository memory and code anchors.',
    'check': 'Check repository rules and changed code anchors.',
    'recall': 'Recall relevant rules, lessons, and memory.',
    'handover': 'Build or verify a handover capsule checksum.',
    'learn': 'Learn from memory or review transcript lessons.',
    'ui': 'Export the local memory inspector.',
}
ADMIN_GROUPS = (
    ('Memory store', 'schema brief stats scenes capture recall read skills skill cases links learn feedback '
     'skill-manage skill-sync index backfill preflight capsule consolidate sync-md export'),
    ('Rules/wiki', 'init check audit wiki import slim-plan rule-context rule-recall index-build handover'),
    ('Dream', 'dream-build dream-recall dream-pending dream-approve dream-reject'),
    ('Benchmarks', 'repo-benchmark compare-bench benchmark-compare benchmark'),
    ('Maintenance', 'doctor backup restore heal-skills verify-skill inspector ui'),
)


class _ExplicitValue(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, '_given_' + self.dest, True)


class _OutputFormat(_ExplicitValue):
    def __call__(self, parser, namespace, values, option_string=None):
        super().__call__(parser, namespace, values, option_string)
        namespace._format_explicit = True


class _LearnParser(argparse.ArgumentParser):
    """New learn spellings are exact-only; all old abbreviation rules survive."""
    _merge_options = ("--merge", "--unmerge", "--same")

    def _get_option_tuples(self, option_string):
        return [entry for entry in super()._get_option_tuples(option_string)
                if entry[1] not in self._merge_options]

    def parse_known_args(self, args=None, namespace=None):
        self._uses_merge_options = any(a.split("=", 1)[0] in self._merge_options for a in (args or []))
        return super().parse_known_args(args, namespace)

    def error(self, message):
        if getattr(self, "_uses_merge_options", False):
            return super().error(message)
        # Match legacy error text and usage, including the required-mode list.
        actions = self._actions
        groups = [(g, g._group_actions) for g in self._action_groups + self._mutually_exclusive_groups]
        def old(action):
            return not any(o in self._merge_options for o in action.option_strings)
        try:
            self._actions = [a for a in actions if old(a)]
            for group, entries in groups:
                group._group_actions = [a for a in entries if old(a)]
            message = message.replace(" --merge --unmerge", "")
            super().error(message)
        finally:
            self._actions = actions
            for group, entries in groups:
                group._group_actions = entries


def _reserve_legacy_prefixes(parser):
    """Reserve old unique abbreviations without expanding the visible help.

    Call before adding any new/global options. Exact spellings take precedence
    in argparse, so a later option cannot make an old abbreviation ambiguous.
    Multiple old aliases still count as separate matches, as argparse did.
    """
    options = dict(parser._option_string_actions)
    for option, action in options.items():
        if option.startswith('--'):
            for length in range(3, len(option)):
                prefix = option[:length]
                if prefix not in options and sum(name.startswith(prefix) for name in options) == 1:
                    parser._option_string_actions[prefix] = action


def _add_global_options(parser, *, defaults=False):
    """Use a separate namespace so a command's own option always wins.

    SUPPRESS on child parsers preserves values parsed before the command.
    Existing local options are never replaced with a global option.
    """
    for name, default in (('root', _default_cli_root()), ('task', ''), ('session', ''), ('standalone', False)):
        flag = '--' + name
        if flag in parser._option_string_actions:
            continue
        kwargs = {'dest': '_global_' + name, 'default': default if defaults else argparse.SUPPRESS}
        if name == 'standalone':
            kwargs.update(action='store_true', help='Bootstrap task automatically if it does not exist')
        else:
            kwargs['metavar'] = name.upper()
            kwargs['action'] = _ExplicitValue
        _call_foreign(lambda: parser.add_argument(flag, **kwargs))


_PLUGINS = None
_PLUGIN_COMMANDS = {}


def _register_plugins(sub):
    """Commands from optional modules (see memlet/cli_plugins.py). They are added after the legacy option
    prefixes are frozen, so they cannot change how an older command parses."""
    global _PLUGINS
    from memlet import cli_plugins
    _PLUGINS = cli_plugins.load()
    _PLUGIN_COMMANDS.clear()
    _PLUGIN_COMMANDS.update(cli_plugins.commands(_PLUGINS))
    for name in _PLUGIN_COMMANDS:
        if name in sub.choices:
            raise ValueError('CLI plug-in command collides with a built-in command: ' + name)
    for plugin in _PLUGINS:
        _call_foreign(lambda: plugin.register(sub))


def _product_commands():
    out = dict(PRODUCT_COMMANDS)
    for name, meta in _PLUGIN_COMMANDS.items():
        if meta.get('product'):
            out[name] = meta.get('help') or ''
    return out


def _admin_groups():
    groups = [list(g) for g in ADMIN_GROUPS]
    for name, meta in _PLUGIN_COMMANDS.items():
        label = meta.get('group') or 'More'
        for g in groups:
            if g[0] == label:
                g[1] = g[1] + ' ' + name
                break
        else:
            groups.append([label, name])
    return groups


def _configure_help(parser, sub):
    """Keep the legacy parser map intact; limit only argparse's display entries."""
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    commands = dict(sub.choices)
    plugin_help = '\n'.join(('memlet ' + name if meta.get('product') else 'memlet admin ' + name) + ': ' + meta['help']
                            for name, meta in _PLUGIN_COMMANDS.items() if not meta.get('product') and name in ('anchors', 'eval'))
    for child in set(commands.values()):
        _add_global_options(child)
    dream_parser = commands.get('dream')
    if dream_parser is not None:
        for action in dream_parser._actions:
            if action.dest in ('_global_root', '_global_task', '_global_session', '_global_standalone'):
                action.help = argparse.SUPPRESS
    admin = _call_foreign(lambda: sub.add_parser('admin', help='Advanced and legacy commands.',
                           allow_abbrev=False,
                           formatter_class=argparse.RawDescriptionHelpFormatter,
                           description='Advanced and legacy Memlet commands.',
                           epilog='\n\n'.join(label + ':\n  ' + names for label, names in _admin_groups()) + '\n\n' + plugin_help))
    _add_global_options(admin)
    _reserve_legacy_prefixes(admin)
    admin.set_defaults(_admin=True)
    nested = admin.add_subparsers(dest='action', required=True, metavar='COMMAND', action=_InvokedSubParsers)
    # These are the very same parsers and handlers, including hidden experiments.
    nested.choices.update(commands)
    sub.metavar = '{' + ','.join(list(_product_commands()) + ['admin']) + '}'
    sub._choices_actions = [_call_foreign(lambda: sub._ChoicesPseudoAction(name, (), description))
                           for name, description in list(_product_commands().items()) +
                           [('admin', 'Advanced and legacy commands.')]]
    parser.epilog = 'For advanced and legacy commands, run: memlet admin --help\n' + plugin_help


def _top_command(argv, parser):
    """Read only the root prefix; option values that look like commands are values."""
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in ('--help', '-h', '--version'):
            return None
        if not token.startswith('-'):
            return token
        flag, equals, _ = token.partition('=')
        option = parser._option_string_actions.get(flag)
        if option is None and flag.startswith('--'):
            matches = [action for key, action in parser._option_string_actions.items() if key.startswith(flag)]
            option = matches[0] if len(matches) == 1 else None
        if option is None:
            return None
        i += 1 if equals or option.nargs == 0 else 2
    return None


def _deprecation_enabled():
    # Keep redirected stderr machine-readable unless the caller opts in.
    return (os.environ.get('MEMLET_NO_DEPRECATION') != '1' and
            (os.environ.get('MEMLET_DEPRECATION') == '1' or sys.stderr.isatty()))


def _deprecated(name, replacement):
    if _deprecation_enabled():
        print("memlet: '{}' is now '{}'; the short form will be removed in a future release."
              .format(name, replacement), file=sys.stderr)


def _open_store(args):
    """Open the task-memory store. Only commands that use the store mention an older store on this machine;
    repository commands (check, recall, handover, ...) stay quiet and their stderr stays machine-readable."""
    from memlet.compatibility import existing_store_notice
    notice = existing_store_notice(args.root)
    if notice:
        print(notice, file=sys.stderr)
    return WorkStore(args.root)


def _bridge_call(name, *args, **kwargs):
    try:
        from memlet import memlet_bridge
    except ImportError:
        return {'ok': False, 'error': 'Repository memory bridge is unavailable; install a Memlet build '
                'containing memlet.memlet_bridge.', 'exitCode': 1}
    return getattr(memlet_bridge, name)(*args, **kwargs)


def _refresh_managed_promotion(graph_path, result):
    """Legacy decisions on the managed queue refresh the same promoted page as `learn` decisions."""
    path = Path(graph_path).expanduser().resolve()
    if (path.name == 'APPROVAL-QUEUE.json' and path.parent.name == 'dream'
            and path.parent.parent.name == '.memlet' and path.is_file()):
        from memlet.memlet_bridge import promote_approved
        promotion = promote_approved(path.parent.parent.parent)
        result['promotion'] = promotion
        if not promotion.get('ok'):
            result.update(ok=False, exitCode=1, error='Decision recorded, but promotion failed: ' +
                          str(promotion.get('error')))
    return result


def _route_new(args):
    """New flags take explicit routes; flag-less legacy handlers below stay intact."""
    if args.action in ('check', 'audit'):
        repo = args.repo_path or args.repo_positional or '.'
        if args.changed:
            return _bridge_call('changed_check', repo, base=args.base)
        if args.accept:
            return _bridge_call('accept_anchor', repo, anchors=args.accept, dry_run=args.dry_run)
        if args.repin_legacy_ts:
            from memlet.memlet_wiki import repin_legacy_ts_anchors
            return repin_legacy_ts_anchors(repo, dry_run=args.dry_run)
    if args.action == 'recall' and (args.unified or args.positional_query is not None or args.files is not None):
        kwargs = {'budget': args.budget}
        if args.files is not None:
            kwargs['files'] = args.files
        if args.standalone or args.task:
            store = _open_store(args)
            task = args.task or _standalone_task_id()
            if args.standalone:
                _call_foreign(lambda: store.ensure_standalone_task(task, session=args.session or 'standalone-owner'))
                if not args.task:
                    notice = legacy_standalone_notice(store, task)
                    if notice:
                        print(notice, file=sys.stderr)
            kwargs.update(store=store, task=task)
        return _bridge_call('unified_recall', args.repo,
                            args.positional_query if args.positional_query is not None else args.query, **kwargs)
    if args.action == 'learn' and args.file is None:
        if args.transcript is not None:
            return _bridge_call('learn_from_transcript', args.repo, args.transcript, vendor=args.vendor)
        if args.pending:
            return _bridge_call('learn_pending', args.repo)
        if args.merge:
            return _bridge_call('learn_merge', args.repo, keep=args.merge, same=args.same, approver=args.by)
        if args.unmerge:
            return _bridge_call('learn_unmerge', args.repo, ids=args.unmerge, approver=args.by)
        if args.paths:
            return _bridge_call('learn_paths', args.repo, ids=args.paths, approver=args.by, paths=args.path)
        if args.approve:
            options = {}
            if args.path is not None:
                options["paths"] = args.path
            if args.reconsider:
                options['reconsider'] = True
            if args.refresh_citations:
                options['refresh_citations'] = True
            return _bridge_call('learn_approve', args.repo, ids=args.approve, approver=args.by, **options)
        if args.reject:
            return _bridge_call('learn_reject', args.repo, ids=args.reject, approver=args.by, reason=args.reason)
    if args.action == 'handover' and args.verify:
        from memlet.memlet_wiki import verify_handover_checksum, verify_handover_markdown
        try:
            path = Path(args.verify)
            content = path.read_text(encoding='utf-8')
            ok = (verify_handover_checksum(json.loads(content)) if path.suffix.lower() == '.json'
                  else verify_handover_markdown(content))
            reason = 'checksum verified' if ok else 'checksum mismatch or missing checksum'
            if path.suffix.lower() != '.json' and 'Checksum:' not in content:
                reason = 'legacy capsule: no text checksum to verify; regenerate with memlet handover --write'
        except OSError:
            raise
        except ValueError as exc:
            ok, reason = False, 'cannot verify capsule: ' + ' '.join(str(exc).splitlines())
        result = {'ok': ok, 'exitCode': 0 if ok else 1, 'reason': reason}
        if not (getattr(args, '_format_explicit', False) and args.format == 'json'):
            result['_cli_text'] = 'memlet: ' + reason + '\n'
        return result
    if args.action == 'dream-pending':
        from memlet.memlet_dream import list_pending
        return list_pending(args.graph)
    if args.action == 'dream-approve':
        from memlet.memlet_dream import approve_queue_items
        options = {}
        if args.reconsider:
            options['reconsider'] = True
        if args.refresh_citations:
            options['refresh_citations'] = True
        result = approve_queue_items(args.graph, args.ids, approver=args.by,
                                     snapshot_path=args.snapshot, db_path=args.db, **options)
        return _refresh_managed_promotion(args.graph, result)
    if args.action == 'dream-reject':
        from memlet.memlet_dream import reject_queue_items
        result = reject_queue_items(args.graph, args.ids, approver=args.by, reason=args.reason, db_path=args.db)
        return _refresh_managed_promotion(args.graph, result)
    return None


def _text_result(result):
    """Prefer the handler's rendering, with a text fallback for store/UI responses."""
    if isinstance(result.get('formatted'), str):
        return result['formatted']

    def lines(value, depth=0):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.startswith('_cli_'):
                    continue
                if isinstance(child, (dict, list)):
                    yield '  ' * depth + '- ' + str(key) + ':'
                    yield from lines(child, depth + 1)
                else:
                    yield '  ' * depth + '- ' + str(key) + ': ' + str(child)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, (dict, list)):
                    yield from lines(child, depth)
                else:
                    yield '  ' * depth + '- ' + str(child)
    return '\n'.join(lines(result))


def _validate_modes(args, parser):
    """Refuse constraints that a selected repository route cannot honor."""
    def given(name):
        return getattr(args, '_given_' + name, False)

    def reject(mode, flags):
        if flags:
            _call_foreign(lambda: parser.error(mode + ' does not support ' + ', '.join(flags)))

    store_flags = ['--' + name for name in ('root', 'task', 'session') if given('_global_' + name)]
    if args.standalone:
        store_flags.append('--standalone')
    if args.action == 'recall' and (args.unified or args.positional_query is not None or args.files is not None):
        flags = ['--query'] if args.positional_query is not None and given('query') else []
        flags += ['--' + name.replace('_', '-') for name in ('at', 'known_at', 'before', 'after')
                  if getattr(args, name) is not None]
        flags += ['--' + name for name in ('shared', 'federated') if getattr(args, name)]
        reject('unified recall (use recall --query for store filters)', flags)
    if args.action == 'learn':
        mode = ('--file' if args.file is not None else '--transcript' if args.transcript is not None
                else '--pending' if args.pending else '--merge' if args.merge else '--unmerge' if args.unmerge else '--approve' if args.approve or args.paths else '--reject')
        allowed = {'--file': (), '--transcript': ('repo', 'vendor'), '--pending': ('repo',),
                   '--merge': ('repo', 'by'), '--unmerge': ('repo', 'by'),
                   '--approve': ('repo', 'by'), '--reject': ('repo', 'by', 'reason')}[mode]
        flags = ['--' + name for name in ('repo', 'vendor', 'by', 'reason') if given(name) and name not in allowed]
        reject('learn ' + mode, flags + (store_flags if mode != '--file' else []))
        if (args.reconsider or args.refresh_citations) and not args.approve:
            _call_foreign(lambda: parser.error("--reconsider and --refresh-citations require --approve"))
        if args.path is not None and not (args.approve or args.paths):
            _call_foreign(lambda: parser.error("--path requires --approve or --paths"))
        if args.paths and not args.path:
            _call_foreign(lambda: parser.error("--paths requires --path FILE"))
        from memlet.vocabulary import MERGE_REASONS
        if args.same and not args.merge:
            _call_foreign(lambda: parser.error(MERGE_REASONS['same-without-merge']))
        if args.merge and not args.same:
            _call_foreign(lambda: parser.error(MERGE_REASONS['same-required']))
        if (args.merge or args.unmerge) and not args.by.strip():
            _call_foreign(lambda: parser.error(MERGE_REASONS['by-required']))
        if (args.approve or args.reject or args.paths) and not args.by.strip():
            _call_foreign(lambda: parser.error('learn --approve/--reject requires --by NAME'))
    if args.action == 'init':
        new_flags = ['--' + name for name in ('jit', 'guard', 'capsule') if getattr(args, name)]
        if new_flags and not args.hooks:  # 9.2 opt-ins install something: --hooks-remove takes none of them
            _call_foreign(lambda: parser.error(', '.join(new_flags) + ' requires --hooks'))
        if not (args.hooks or args.hooks_remove):
            flags = (['--hooks-scope'] if args.hooks_scope else []) + (['--git-hooks'] if args.git_hooks else []) + (['--dream'] if args.dream else [])
            if flags:
                _call_foreign(lambda: parser.error(', '.join(flags) + ' requires --hooks or --hooks-remove'))
        elif args.hooks_remove and args.force:
            reject('init --hooks-remove', ['--force'])
    if args.action in ('check', 'audit'):
        mode = '--changed' if args.changed else '--accept' if args.accept else '--repin-legacy-ts' if args.repin_legacy_ts else None
        if mode:
            flags = ['--' + name for name in ('base', 'head') if given(name) and not (mode == '--changed' and name == 'base')]
            if args.exit_zero:
                flags.append('--exit-zero')
            if args.dry_run and mode == '--changed':
                flags.append('--dry-run')
            if args.format == 'sarif':
                flags.append('--format sarif (choose json or markdown)')
            reject('check ' + mode, flags + store_flags)
        elif args.dry_run:
            _call_foreign(lambda: parser.error('--dry-run requires --accept or --repin-legacy-ts'))


def main():
    parser = _build_parser()
    argv = sys.argv[1:]
    command = _top_command(argv, parser)
    if command and command not in _product_commands() and command != 'admin':
        sub = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        if command in sub.choices:
            _deprecated(command, 'memlet admin ' + command)
    if command == 'learn' and any(token.partition('=')[0] in ('--sa', '--sam', '--same') for token in argv):
        # A lone --same has no mode for argparse's required group. Diagnose the
        # dependency first, resolving options through the frozen legacy aliases.
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        learn_options = [sub.choices['learn']._parse_optional(token) for token in argv[argv.index('learn') + 1:]]
        selected = set()
        for options in learn_options:
            # Python 3.13 returns a list of alternatives; 3.9 returns one tuple.
            # Leave ambiguous abbreviations to argparse's existing diagnostic.
            if isinstance(options, list) and len(options) != 1:
                continue
            for parsed in options if isinstance(options, list) else [options]:
                if parsed and parsed[0] is not None:
                    selected.add(parsed[0].dest)
        if 'same' in selected and 'merge' not in selected:
            from memlet.vocabulary import MERGE_REASONS
            _call_foreign(lambda: parser.error(MERGE_REASONS['same-without-merge']))
    args = _call_foreign(lambda: parser.parse_args(argv))
    for name in ('root', 'task', 'session', 'standalone'):
        if not hasattr(args, name):
            setattr(args, name, getattr(args, '_global_' + name))
    _validate_modes(args, parser)
    if args.action == 'recall' and args.budget is None:
        args.budget = 1200 if args.unified or args.positional_query is not None or args.files is not None else 6000
    human = args.action in _product_commands() or (args.action in _PLUGIN_COMMANDS and hasattr(args, 'format'))
    if human and not getattr(args, '_format_explicit', False) and sys.stdout.isatty():
        args.format = 'markdown'
    result = _main(args, parser)
    if human and getattr(args, 'format', None) == 'markdown' and isinstance(result, dict) and '_cli_text' not in result:
        rendered = _text_result(result)
        result['_cli_text'] = rendered + ('\n' if rendered and not rendered.endswith('\n') else '')
    return result


def _build_parser():
    # Older argparse scans child arguments at the parent level first. Reserve
    # all valid global prefixes explicitly, then stop the parent from rejecting
    # a child's legacy --s as ambiguous between --session and --standalone.
    parser=argparse.ArgumentParser(prog=_program_name(),description='Memlet: local repository memory.',allow_abbrev=False)
    from memlet import VERSION
    _call_foreign(lambda: parser.add_argument('--version',action='version',version=VERSION))
    _add_global_options(parser, defaults=True)
    _reserve_legacy_prefixes(parser)
    sub=_call_foreign(lambda: parser.add_subparsers(dest='action',required=True,action=_InvokedSubParsers))
    _call_foreign(lambda: sub.add_parser('schema')); _call_foreign(lambda: sub.add_parser('brief')); _call_foreign(lambda: sub.add_parser('stats')); _call_foreign(lambda: sub.add_parser('scenes')); _call_foreign(lambda: sub.add_parser('doctor'))
    bak_p=_call_foreign(lambda: sub.add_parser('backup'));bak_p.add_argument('--dest-dir',default=None);bak_p.add_argument('--include-models',action='store_true')
    res_p=_call_foreign(lambda: sub.add_parser('restore'));res_p.add_argument('--backup-dir',required=True)
    rb_p=_call_foreign(lambda: sub.add_parser('repo-benchmark'));rb_p.add_argument('--work-dir',default=None)
    aud_p=_call_foreign(lambda: sub.add_parser('audit',aliases=['check']));aud_p.add_argument('repo_positional',nargs='?',default=None);aud_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    aud_p.add_argument('--base',action=_ExplicitValue,default=None);aud_p.add_argument('--head',action=_ExplicitValue,default='HEAD');aud_p.add_argument('--format',action=_OutputFormat,default='json',choices=['json','markdown','sarif']);aud_p.add_argument('--exit-zero',action='store_true')
    init_p=_call_foreign(lambda: sub.add_parser('init',help='Zero-daemon onboarding: create .memlet/config.json and .memlet/wiki/INDEX.md (never reads .env)'))
    init_p.add_argument('repo_positional',nargs='?',default=None);init_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    init_p.add_argument('--dry-run',action='store_true');init_p.add_argument('--force',action='store_true',help='Regenerate INDEX.md and re-pin anchors')
    init_p.add_argument('--format',action=_OutputFormat,default='json',choices=['json','markdown'])
    wiki_p=_call_foreign(lambda: sub.add_parser('wiki',help='Check AST anchors in .memlet/wiki/*.md (active / compatible-unverified / stale)'))
    wiki_p.add_argument('repo_positional',nargs='?',default=None);wiki_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    wiki_p.add_argument('--dry-run',action='store_true');wiki_p.add_argument('--format',action=_OutputFormat,default='json',choices=['json','markdown'])
    imp_rules_p=_call_foreign(lambda: sub.add_parser('import',help='Import rules from CLAUDE.md / AGENTS.md / .cursorrules / .ai-memory / .claude-mem and tag each with its live AST status'))
    imp_rules_p.add_argument('repo_positional',nargs='?',default=None);imp_rules_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    imp_rules_p.add_argument('--from',dest='source',default='auto',choices=['auto','claude-md','cursorrules','ai-memory','claude-mem'])
    imp_rules_p.add_argument('--dry-run',action='store_true');imp_rules_p.add_argument('--format',action=_OutputFormat,default='json',choices=['json','markdown'])
    slim_p=_call_foreign(lambda: sub.add_parser('slim-plan',help='Propose a slimmer AGENTS.md by moving (never rewriting) older directives into an indexed log; writes only to --out, with a preservation proof'))
    slim_p.add_argument('repo_positional',nargs='?',default=None);slim_p.add_argument('--out',required=True)
    slim_p.add_argument('--keep-recent',type=int,default=2);slim_p.add_argument('--split-rules',action='store_true')
    dream_b=_call_foreign(lambda: sub.add_parser('dream-build',help='Build a PENDING approval queue from session logs and optional candidates (no model call)'))
    dream_b.add_argument('--candidates',default=None,help='Optional path to raw candidate JSON; if omitted, deterministically extracts candidates from --snapshot')
    dream_b.add_argument('--snapshot',required=True,help='Path to a .log/.jsonl transcript, log directory, or JSON snapshot map')
    dream_b.add_argument('--out-json',required=True,help='Output path for the PENDING approval queue JSON')
    dream_b.add_argument('--out-db',default=None,help='Optional SQLite DB path to write ontology_nodes, ontology_edges, and dream_runs')
    dream_b.add_argument('--vendor',default='autonomous-deterministic',help='Extractor vendor tag')
    dream_p=_call_foreign(lambda: sub.add_parser('dream-recall',help='1-hop ontology subgraph recall (GOVERNS/SUPERSEDES/RESOLVES/BLOCKS) from a verified Dream queue JSON (no model call)'))
    dream_p.add_argument('--graph',required=True,help='Path to Dream ontology JSON')
    dream_p.add_argument('--query',nargs='*',default='',help='Task or question query (supports unquoted multi-word queries)')
    dream_p.add_argument('--budget','--max-tokens',dest='budget',type=int,default=1500,help='Maximum token budget (default 1500)')
    dream_p.add_argument('--snapshot',default=None,help='Optional log directory or JSON snapshot map to verify pins before recall')
    dream_p.add_argument('--out-md',default=None,help='Optional path to write the Markdown approval queue capsule')
    dream_p.add_argument('--format',action=_OutputFormat,default='json',choices=['json','markdown'],help='Stdout format (json or markdown)')
    rctx_p=_call_foreign(lambda: sub.add_parser('rule-context',help='T0 safety core (verbatim quotes) + T1 rule index from the verified INDEX-RULES.md (no model call)'))
    rctx_p.add_argument('repo_positional',nargs='?',default=None)
    rctx_p.add_argument('--core-budget',type=int,default=600);rctx_p.add_argument('--index-budget',type=int,default=600)
    rctx_p.add_argument('--auto',action='store_true',help='Return the whole instructions when they fit --threshold tokens, else the tiered context');rctx_p.add_argument('--threshold',type=int,default=8000)
    rrec_p=_call_foreign(lambda: sub.add_parser('rule-recall',help='T2: rules most relevant to a task, quoted verbatim from the instructions (deterministic BM25, no model call)'))
    rrec_p.add_argument('query_or_repo',nargs='?',default=None);rrec_p.add_argument('legacy_query',nargs='?',default=None)
    rrec_p.add_argument('--repo',default=None)
    rrec_p.epilog='Use rule-recall "<query>" [--repo DIR] or rule-recall <existing-dir> "<query>"; a lone directory is ambiguous and requires a query.'
    rrec_p.add_argument('--k',type=int,default=3);rrec_p.add_argument('--safety',action='store_true',help='Add up to 4 importance-1 rules not already shown (not a guarantee of full coverage)')
    index_p=_call_foreign(lambda: sub.add_parser('index-build',help='Build .memlet/wiki/INDEX-RULES.md: LLM picks and names rules (never rewrites them), each pinned to its source lines (claude subscription CLI, offline, only when you run it)'))
    index_p.add_argument('repo_positional',nargs='?',default=None)
    index_p.add_argument('--via',default='claude',choices=['claude'])
    brief_p=_call_foreign(lambda: sub.add_parser('brief-build',help='EXPERIMENTAL (meaning is not verified; calls the claude CLI, a model call): build .memlet/wiki/BRIEF.md, an LLM-rewritten rule brief pinned to cited lines; meaning is NOT verified'))
    brief_p.description='EXPERIMENTAL: failed its faithfulness evaluation; meaning is NOT verified.'
    brief_p.add_argument('repo_positional',nargs='?',default=None)
    brief_p.add_argument('--via',default='claude',choices=['claude'],help='Runner: claude only (tool-free, claude.ai subscription verified before the call; refuses API-key auth)')
    hand_p=_call_foreign(lambda: sub.add_parser('handover',help='Checksummed, budgeted handover capsule: constraints with matching pins; stale anchors quarantined'))
    hand_p.add_argument('repo_positional',nargs='?',default=None);hand_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    hand_p.add_argument('--next-step',default=None);hand_p.add_argument('--rejected',action='append',default=[],help='Rejected approach / dead-end (repeatable)')
    hand_p.add_argument('--format',action=_OutputFormat,default='json',choices=['json','markdown']);hand_p.add_argument('--write',action='store_true',help='Write .memlet/wiki/HANDOVER.md and record a handover ledger event')
    hand_p.add_argument('--mode',default=None,choices=['verified','digest','brief','index'],metavar='{verified,index}',help='verified (default): code-anchored rules; index: verbatim key rules and topic index')
    hand_p.add_argument('--budget',default=None,help='Token budget: a number (clamped to 500-2000) or auto (~12%% of the instruction text); default from .memlet/config.json handover.tokenBudget, else 500')
    cmp_p=_call_foreign(lambda: sub.add_parser('compare-bench',aliases=['benchmark-compare'],help='Reproducible refactoring & handover benchmark: Raw Markdown vs Unverified Wiki vs Memlet'))
    cmp_p.add_argument('repo_positional',nargs='?',default=None);cmp_p.add_argument('--format',action=_OutputFormat,default='json',choices=['json','markdown'])
    heal_p=_call_foreign(lambda: sub.add_parser('heal-skills'));heal_p.add_argument('--dry-run',action='store_true')
    ver_p=_call_foreign(lambda: sub.add_parser('verify-skill'));ver_p.add_argument('--id',required=True);ver_p.add_argument('--command',default=None)
    ver_p.add_argument('--proof-file',default=None);ver_p.add_argument('--operation-id',default='');ver_p.add_argument('--timeout',type=float,default=15.0)
    pf_p=_call_foreign(lambda: sub.add_parser('preflight'));pf_p.add_argument('--plan',default='');pf_p.add_argument('--files',default='');pf_p.add_argument('--action-type',default='')
    caps_p=_call_foreign(lambda: sub.add_parser('capsule'));caps_p.add_argument('--plan',default='');caps_p.add_argument('--files',default='');caps_p.add_argument('--worker',default='');caps_p.add_argument('--budget',type=int,default=1200)
    cons_p=_call_foreign(lambda: sub.add_parser('consolidate'));cons_p.add_argument('--threshold',type=float,default=0.35);cons_p.add_argument('--dry-run',action='store_true')
    sync_md_p=_call_foreign(lambda: sub.add_parser('sync-md'));sync_md_p.add_argument('--note-path',default=None);sync_md_p.add_argument('--explicit-user',action='store_true')
    bench_p=_call_foreign(lambda: sub.add_parser('benchmark',help='Without --task: refactoring & handover comparison benchmark; with --task: memory store benchmark'))
    bench_p.add_argument('repo_positional',nargs='?',default=None);bench_p.add_argument('--budget',type=int,default=6000)
    bench_p.add_argument('--format',action=_OutputFormat,default=None,choices=['json','markdown'])
    insp_p=_call_foreign(lambda: sub.add_parser('inspector',help='Without --task: same HTML report as `ui`; with --task: task-memory inspector'));insp_p.add_argument('--budget',type=int,default=6000)
    ui_p=_call_foreign(lambda: sub.add_parser('ui',help='Memlet Inspector: self-contained HTML report (AST health, benchmark, handover)'))
    for p in (insp_p,ui_p):
        p.add_argument('repo_positional',nargs='?',default=None);p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
        p.add_argument('--output','--out',dest='output',default=None,help='HTML output file');p.add_argument('--output-dir',default=None)
    exp_p=_call_foreign(lambda: sub.add_parser('export'));exp_p.add_argument('--targets',default='claude,agents,cursor,windsurf,mcp')
    exp_p.add_argument('--target-dir',default=None);exp_p.add_argument('--write',action='store_true');exp_p.add_argument('--lang',default='ko')
    cap_p=_call_foreign(lambda: sub.add_parser('capture'));cap_p.add_argument('--file',default=None);cap_p.add_argument('--text',default=None)
    cap_p.add_argument('--kind',default='auto');cap_p.add_argument('--command',default='');cap_p.add_argument('--operation-id',default='')
    recall=_call_foreign(lambda: sub.add_parser('recall'));recall.add_argument('--query',action=_ExplicitValue,default='');recall.add_argument('--at');recall.add_argument('--known-at')
    recall.add_argument('--before');recall.add_argument('--after');recall.add_argument('--shared',action='store_true');recall.add_argument('--federated',action='store_true')
    recall.add_argument('--budget',type=int,default=None)
    read=_call_foreign(lambda: sub.add_parser('read'));read.add_argument('--id',required=True);read.add_argument('--offset',type=int,default=0);read.add_argument('--size',type=int,default=6000)
    skills_p=_call_foreign(lambda: sub.add_parser('skills'));skills_p.add_argument('--query',default='');skills_p.add_argument('--include-inactive',action='store_true')
    skills_p.add_argument('--at');skills_p.add_argument('--known-at');skills_p.add_argument('--before');skills_p.add_argument('--after')
    skills_p.add_argument('--shared',action='store_true');skills_p.add_argument('--federated',action='store_true');skills_p.add_argument('--limit',type=int,default=50);skills_p.add_argument('--budget',type=int,default=2400)
    skill_p=_call_foreign(lambda: sub.add_parser('skill'));skill_p.add_argument('--id',required=True)
    cases_p=_call_foreign(lambda: sub.add_parser('cases'));cases_p.add_argument('--limit',type=int,default=25)
    links_p=_call_foreign(lambda: sub.add_parser('links'));links_p.add_argument('--id',default=None);links_p.add_argument('--limit',type=int,default=100)
    for name in ('feedback','skill-manage'):
        _call_foreign(lambda: sub.add_parser(name).add_argument('--file',required=True))
    learn=_call_foreign(lambda: sub.add_parser('learn'))
    learn.__class__ = _LearnParser
    learn_mode=learn.add_mutually_exclusive_group(required=True)
    learn_mode.add_argument('--file',help='Existing memory-store learning request JSON')
    _call_foreign(lambda: sub.add_parser('skill-sync'))
    idx=_call_foreign(lambda: sub.add_parser('index'));idx.add_argument('--limit',type=int,default=200)
    imp=_call_foreign(lambda: sub.add_parser('backfill'));imp.add_argument('--source-session',required=True);imp.add_argument('--end-byte',type=int)
    # Freeze legacy option spellings before any router-only options are added.
    for child in set(sub.choices.values()):
        _reserve_legacy_prefixes(child)
    hooks_mode=init_p.add_mutually_exclusive_group()
    hooks_mode.add_argument('--hooks',action='store_true',help='Opt-in: also install Memlet hooks for Claude Code (merged into .claude settings, never replacing them; all-or-nothing, with a compare-and-swap check before each write. A program that replaces the same file at that very instant cannot be excluded without a shared lock)')
    hooks_mode.add_argument('--hooks-remove',action='store_true',help='Remove only the hook entries Memlet installed')
    init_p.add_argument('--hooks-scope',choices=['local','project'],default=None,help='local (default): .claude/settings.local.json; project: shared .claude/settings.json (needs memlet on PATH)')
    init_p.add_argument('--dream',action='store_true',help='With --hooks: Stop and PreCompact snapshot sessions for an explicit dream command')
    init_p.add_argument('--git-hooks',action='store_true',help='With --hooks / --hooks-remove: also the git pre-commit (warn only) and post-merge hooks')
    init_p.add_argument('--jit',action='store_true',help='With --hooks: also a PreToolUse hook (Write|Edit|MultiEdit|NotebookEdit) that adds the verified memory attached to the file being edited. Claude Code delivers it with the tool result, after the call ran: it informs, it does not prevent')
    init_p.add_argument('--guard',action='store_true',help='With --hooks (implies --jit): in a session started fresh, the first edit of a file with an approved BLOCKS lesson or a replaced instruction is denied once, quoting those lessons in full; hooks.scopes.<scope>.guard "ask" asks you instead, "off" disables it')
    init_p.add_argument('--capsule',action='store_true',help='With --hooks: PreCompact asks the compaction summary to keep the verified memory for the files this session touched (Write, Edit, MultiEdit and NotebookEdit; a PreToolUse entry is installed to see the last two), and SessionStart re-delivers it after compact or resume')
    check_mode=aud_p.add_mutually_exclusive_group()
    check_mode.add_argument('--changed',action='store_true',help='Check rules affected by changed files')
    check_mode.add_argument('--accept',action='append',default=[],metavar='ANCHOR',help='Re-pin a named path::symbol anchor (repeatable)')
    check_mode.add_argument('--repin-legacy-ts',action='store_true',help='Re-pin legacy TypeScript anchors')
    aud_p.add_argument('--dry-run',action='store_true',help='Preview supported anchor updates')
    dream_p.add_argument('--allow-unverified',action='store_true',help='Allow recall without a snapshot, labelled UNVERIFIED')
    for name in ('dream-pending','dream-approve','dream-reject'):
        p=_call_foreign(lambda: sub.add_parser(name))
        p.add_argument('--graph',required=True,help='Path to the Dream approval queue JSON')
        if name!='dream-pending':
            p.add_argument('ids',nargs='+',metavar='ID')
            p.add_argument('--by',required=True,help='Name of the human reviewer')
            p.add_argument('--db',default=None,help='Optional SQLite ontology database')
            if name=='dream-approve':
                p.add_argument('--snapshot',default=None,help='Snapshot used to verify citation pins')
                p.add_argument('--reconsider',action='store_true',help='Fresh approval of a previously rejected item')
                p.add_argument('--refresh-citations',action='store_true',help='Approve current citation text after review')
            else:
                p.add_argument('--reason',default='')
    hand_p.add_argument('--verify',metavar='FILE',help='Verify a JSON or Markdown capsule checksum')
    recall.add_argument('positional_query',nargs='?',help='Recall across repository rules and approved lessons')
    from memlet.vocabulary import RECALL_FILES_HELP
    recall.add_argument('--files', nargs='+', default=None, metavar='FILE', help=RECALL_FILES_HELP)
    recall.add_argument('--unified',action='store_true',help='Recall across repository sources')
    recall.add_argument('--repo',default='.',help='Repository for unified recall')
    recall.add_argument('--format',action=_OutputFormat,default='json',choices=['json','markdown'])
    ui_p.add_argument('--format',action=_OutputFormat,default='json',choices=['json','markdown'])
    learn_mode.add_argument('--transcript',metavar='PATH',help='Add transcript lessons to the PENDING queue')
    learn_mode.add_argument('--pending',action='store_true',help='List lessons awaiting review')
    learn_mode.add_argument('--approve',nargs='+',metavar='ID',help='Approve lessons as a human reviewer')
    learn_mode.add_argument('--paths', metavar='ID', nargs='+', help='Re-approve a lesson with new file attachments')
    learn.add_argument('--path', action='append', metavar='FILE', help='Exact repository file; applies to each approved lesson (max 8)')
    learn_mode.add_argument('--reject',nargs='+',metavar='ID',help='Reject lessons as a human reviewer')
    learn.add_argument('--by',action=_ExplicitValue,default='',help='Name of the human reviewer (required for decisions)')
    learn.add_argument('--reconsider',action='store_true',help='Fresh approval of a previously rejected lesson')
    learn.add_argument('--refresh-citations',action='store_true',help='Approve current citation text after reading it in --pending')
    learn.add_argument('--reason',action=_ExplicitValue,default='')
    learn.add_argument('--repo',action=_ExplicitValue,default='.')
    learn.add_argument('--vendor',action=_ExplicitValue,default='autonomous-deterministic')
    learn.add_argument('--format',action=_OutputFormat,default='json',choices=['json','markdown'])
    from memlet.vocabulary import MERGE_HELP, UNMERGE_HELP, MERGE_SAME_HELP
    learn_mode.add_argument('--merge', metavar='KEEP', help=MERGE_HELP)
    learn_mode.add_argument('--unmerge', metavar='ID', nargs='+', help=UNMERGE_HELP)
    learn.add_argument('--same', metavar='ID', nargs='+', help=MERGE_SAME_HELP)
    _register_plugins(sub)
    _configure_help(parser, sub)
    return parser


def _main(args, parser):
    if args.action in _PLUGIN_COMMANDS:
        from memlet import cli_plugins
        return cli_plugins.dispatch(_PLUGINS, args)
    routed = _route_new(args)
    if routed is not None:
        return routed
    if args.action=='schema':return schema()
    if args.action=='repo-benchmark':return memory.repo_benchmark(work_dir=args.work_dir)
    if args.action in _EARLY_REPO_ACTIONS and not (args.action=='inspector' and (args.task or args.standalone)):
        from memlet.memlet_inspector import export_inspector_html
        target_repo=args.repo_path or args.repo_positional or '.'
        out_file=args.output or args.output_dir
        return export_inspector_html(target_repo,output_path=out_file)
    if args.action in ('init','wiki'):
        from memlet.memlet_wiki import init_project, wiki_status
        target_repo=args.repo_path or args.repo_positional or '.'
        if args.action=='init' and (args.hooks or args.hooks_remove):
            from memlet.memlet_hooks import init_hooks_command
            res=init_hooks_command(target_repo,remove=args.hooks_remove,scope=args.hooks_scope or 'local',git_hooks=args.git_hooks,dream=args.dream,
                                   jit=args.jit,guard=args.guard,capsule=args.capsule,
                                   dry_run=args.dry_run,force=args.force,output_format=args.format)
        elif args.action=='init':
            res=init_project(target_repo,dry_run=args.dry_run,force=args.force,output_format=args.format)
        else:
            res=wiki_status(target_repo,dry_run=args.dry_run,output_format=args.format)
        if args.format=='markdown':res['_cli_text']=res['formatted']
        return res
    if args.action=='slim-plan':
        from memlet.memlet_slim import slim_plan
        res=slim_plan(args.repo_positional or '.',args.out,keep_recent=args.keep_recent,split_rules=args.split_rules)
        # This exact plan outcome is a successful no-op, unlike invalid inputs,
        # unsafe outputs, failed proofs, or apply collisions.
        if res.get('why')=='nothing to move (no dated directive blocks beyond the kept ones)':
            res['exitCode']=0
        return res
    if args.action=='dream-build':
        from memlet.memlet_dream import run_dream_build
        return run_dream_build(
            snapshot_path=args.snapshot,
            out_json=args.out_json,
            candidates_path=args.candidates,
            out_db=args.out_db,
            vendor=args.vendor,
        )
    if args.action=='dream-recall':
        from memlet.memlet_dream import run_dream_recall
        res=run_dream_recall(
            graph_path=args.graph,
            query_text=' '.join(args.query) if isinstance(args.query,list) else str(args.query or ''),
            budget_tokens=args.budget,
            snapshot_path=args.snapshot,
            out_md=args.out_md,
            **({'allow_unverified': True} if args.allow_unverified else {})
        )
        if args.format=='markdown' and isinstance(res.get('formatted'),str):
            res['_cli_text']=res['formatted'] if res['formatted'].endswith('\n') else res['formatted']+'\n'
        return res
    if args.action in ('rule-context','rule-recall'):
        from memlet.memlet_recall import build_context, recall
        if args.action=='rule-context':
            return build_context(args.repo_positional or '.',core_budget=args.core_budget,index_budget=args.index_budget,auto=args.auto,threshold=args.threshold)
        usage='Use rule-recall "<query>" [--repo DIR] or rule-recall <existing-dir> "<query>".'
        query=args.query_or_repo
        repo=args.repo or '.'
        if args.legacy_query is not None:
            if not query or not Path(query).is_dir():
                _call_foreign(lambda: parser.error('The first of two positionals must be an existing directory. '+usage))
            if args.repo is not None and Path(query).resolve()!=Path(args.repo).resolve():
                _call_foreign(lambda: parser.error('Positional repository conflicts with --repo. '+usage))
            repo,query=query,args.legacy_query
        elif query and Path(query).is_dir():
            _call_foreign(lambda: parser.error('A lone directory is ambiguous; supply a query. '+usage))
        if not query or not query.strip():
            _call_foreign(lambda: parser.error('A rule query is required. '+usage))
        return recall(repo,query,k=args.k,safety=args.safety)
    if args.action=='index-build':
        from memlet.memlet_index import build_index
        return build_index(args.repo_positional or '.',via=args.via)
    if args.action=='brief-build':
        from memlet.memlet_brief import build_brief
        return build_brief(args.repo_positional or '.',via=args.via)
    if args.action in ('import','handover'):
        from memlet.memlet_wiki import build_handover, import_rules
        target_repo=args.repo_path or args.repo_positional or '.'
        if args.action=='import':
            res=import_rules(target_repo,source=args.source,dry_run=args.dry_run,output_format=args.format)
        else:
            res=build_handover(target_repo,next_step=args.next_step,rejected=args.rejected,output_format=args.format,write=args.write,budget=args.budget,mode=args.mode)
        if args.format=='markdown':res['_cli_text']=res['formatted']
        return res
    if args.action in ('compare-bench','benchmark-compare') or (args.action=='benchmark' and (
            args.repo_positional or args.format or not (args.task or args.standalone))):
        from memlet.memlet_wiki import run_refactoring_comparison_benchmark
        res=run_refactoring_comparison_benchmark(output_format=args.format or 'json')
        if args.format=='markdown':res['_cli_text']=res['formatted']
        return res
    if args.action in ('audit','check'):
        target_repo=args.repo_path or args.repo_positional or '.'
        res=memory.audit_repo(target_repo,base_ref=args.base,head_ref=args.head,output_format=args.format)
        if args.format in ('markdown', 'sarif') and isinstance(res,dict) and isinstance(res.get('formatted'),str):
            res['_cli_text']=res['formatted'] if res['formatted'].endswith('\n') else res['formatted']+'\n'
        if args.exit_zero and isinstance(res,dict):res['_cli_exit_zero']=True
        return res
    store=_open_store(args)
    if args.action=='doctor':return memory.doctor(store)
    if args.action=='backup':return memory.backup(store,dest_dir=args.dest_dir,include_models=args.include_models)
    if args.action=='restore':return memory.restore(store,backup_dir=args.backup_dir)
    uses_default_task=bool(args.standalone and not args.task)
    if not args.task:
        if args.standalone:
            args.task=_standalone_task_id()
        else:
            raise ValueError('task is required (--task <taskId>)')
    if args.standalone:
        args.session=args.session or 'standalone-owner'
        task=_call_foreign(lambda: store.ensure_standalone_task(args.task,session=args.session))
    else:
        task=_call_foreign(lambda: store.get(args.task))
    if uses_default_task:
        notice=legacy_standalone_notice(store,args.task)
        if notice:print(notice,file=sys.stderr)
    if args.action=='brief':
        result=_call_foreign(lambda: store.memory(args.task,include_retrieval=False))
        with _foreign_context(lambda: store.db()) as db:
            originals=[work_memory._public(r) for r in _call_foreign(lambda: list(db.execute(
                "SELECT * FROM memory_items WHERE task=? AND kind IN ('instruction','report') ORDER BY event_id DESC LIMIT 10",(args.task,))))]
        result['originals']=[{'id':r['item_id'],'kind':r['kind'],'excerpt':r['content'][:2000],
                              'totalChars':len(r['content']),'source':r['source']} for r in originals]
        result.update(owner=task['owner'],project=task['project'],note='Use read for exact source quotes beyond the excerpt.')
        return result
    if args.action=='recall':
        result=_call_foreign(lambda: store.memory(args.task,args.query,args.budget,at=args.at,known_at=args.known_at,
                            before=args.before,after=args.after,shared=args.shared,federated=args.federated))
        result['recallId']=memory.record_recall(store,args.task,args.session,'cli:'+str(time.time_ns()),args.query,result['retrieval'])
        return result
    if args.action=='read':return read_item(store,args.task,args.id,args.offset,args.size)
    if args.action=='skills':
        return _call_foreign(lambda: store.skills(args.task,args.query,include_inactive=args.include_inactive,
                            at=args.at,known_at=args.known_at,before=args.before,after=args.after,
                            shared=args.shared,federated=args.federated,limit=args.limit,budget=args.budget))
    if args.action=='skill':
        return memory.read_skill(store,args.task,args.id)
    if args.action=='cases':
        return memory.cases(store,args.task,limit=args.limit)
    if args.action=='links':
        return memory.links(store,args.task,node_id=args.id,limit=args.limit)
    if args.action=='stats':
        return memory.stats(store,args.task)
    if args.action=='scenes':
        return memory.scenes(store,args.task)
    if args.action=='preflight':
        return memory.preflight_check(store,args.task,plan=args.plan,files=args.files,action_type=args.action_type)
    if args.action=='capsule':
        return memory.build_subagent_capsule(store,args.task,plan=args.plan,files=args.files,worker=args.worker,budget=args.budget)
    if args.action=='heal-skills' and args.dry_run:
        return memory.heal_skills(store,args.task,session=args.session,dry_run=True)
    if args.action=='consolidate' and args.dry_run:
        return memory.consolidate_memories(store,args.task,session=args.session,similarity_threshold=args.threshold,dry_run=True)
    if args.action=='benchmark':
        return memory.benchmark(store,args.task,budget=args.budget)
    if args.action=='inspector':
        return memory.inspector(store,args.task,budget=args.budget)
    if args.action=='export' and not args.write:
        return memory.export_rules(store,args.task,targets=args.targets,target_dir=args.target_dir,write_files=False,lang=args.lang)
    if task['owner']!=args.session:raise ValueError('current responsible session ID is required')
    runtime=os.environ.get('CODEX_THREAD_ID')
    if runtime and not (args.standalone and task['owner']=='standalone-owner') and not os.environ.get('ANTIGRAVITY_AGENT') and args.session!='codex:'+runtime:
        raise ValueError('CLI actor does not match the current native Codex session')
    if os.environ.get('MEMLET_PARENT_SESSION_ID'):
        raise ValueError('background worker memory writes must be returned to the responsible session')
    if args.action=='heal-skills':
        return memory.heal_skills(store,args.task,session=args.session,dry_run=False)
    if args.action=='verify-skill':
        proof=None
        if args.proof_file:
            raw=sys.stdin.read(200001) if args.proof_file=='-' else Path(args.proof_file).read_text(encoding='utf-8')
            proof=json.loads(raw)
        return memory.verify_skill(
            store,args.task,args.session,args.id,
            command=args.command,execution_proof=proof,
            operation_id=args.operation_id or None,timeout=args.timeout,
        )
    if args.action=='consolidate':
        return memory.consolidate_memories(store,args.task,session=args.session,similarity_threshold=args.threshold,dry_run=False)
    if args.action=='sync-md':
        return memory.sync_markdown(store,args.task,session=args.session,note_path=args.note_path,explicit_user=True if args.explicit_user else None)
    if args.action=='export':
        return memory.export_rules(store,args.task,targets=args.targets,target_dir=args.target_dir,write_files=True,lang=args.lang)
    if args.action=='capture':
        if args.file:
            raw=sys.stdin.read(200001) if args.file=='-' else Path(args.file).read_text(encoding='utf-8')
            if len(raw)>200000:raise ValueError('memory request exceeds limit')
            payload=json.loads(raw)
        elif args.text:
            payload={'text':args.text,'kind':args.kind,'command':args.command}
            if args.operation_id:payload['operationId']=args.operation_id
        else:
            raise ValueError('capture requires --text or --file')
        return memory.auto_capture(store,args.task,args.session,payload)
    if args.action=='index':return memory.index_task(store,args.task,args.limit)
    if args.action=='backfill':return backfill(store,args.task,args.source_session,args.end_byte)
    if args.action=='skill-sync':return memory.sync_skills(store,args.task,args.session)
    if args.action in ('learn','feedback','skill-manage'):
        raw=sys.stdin.read(200001) if args.file=='-' else Path(args.file).read_text(encoding='utf-8')
        if len(raw)>200000:raise ValueError('memory request exceeds limit')
        value=json.loads(raw)
        if args.action=='learn':return memory.learn(store,args.task,args.session,value['revision'],value['operationId'],value['entries'])
        if args.action=='skill-manage':
            spec=value.get('spec') if isinstance(value.get('spec'),dict) else {
                k:v for k,v in value.items() if k not in ('revision','operationId')
            }
            return memory.manage_skill(store,args.task,args.session,value['revision'],value['operationId'],spec)
        return memory.feedback(store,args.task,args.session,value['operationId'],value['recallId'],
                               value.get('itemId',''),value['outcome'],value.get('note',''),patch=value.get('patch'))


def _hook_runtime(argv):
    """Hidden `memlet admin hook <event>`: dispatched before argparse so it can never print usage or exit 2."""
    try:
        from memlet.memlet_hooks import run_hook
        return run_hook(argv)
    except BaseException:
        return 0


def cli():
    if sys.argv[1:3] == ['admin', 'hook']:
        return _hook_runtime(sys.argv[3:])
    try:
        result = main()
        exit_zero = result.pop('_cli_exit_zero', False) if isinstance(result, dict) else False
        text = result.pop('_cli_text', None) if isinstance(result, dict) else None
        if text is not None:
            sys.stdout.write(text)
        else:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        if not exit_zero and isinstance(result, dict) and 'exitCode' in result:
            return int(result['exitCode'])
        if not exit_zero and isinstance(result, dict) and result.get('ok') is False:
            return 1
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'type': type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__=='__main__':
    sys.exit(cli())


