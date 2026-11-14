"""CLI plug-in for sealed memory experiments."""

from memlet.memlet_dream import call_foreign as _call_foreign

import argparse
from .vocabulary import JUDGE_UNAVAILABLE
from . import cli_plugins
from . import memlet_eval as ev

COMMANDS = {'eval': {'help': 'Measure memory on sealed held-out questions.', 'product': False, 'group': 'Benchmarks'}}


def register(sub):
    parser = _call_foreign(lambda: sub.add_parser('eval', prog='memlet admin eval', help=COMMANDS['eval']['help']))
    _call_foreign(lambda: parser.add_argument('--repo', default='.'))
    commands = _call_foreign(lambda: parser.add_subparsers(dest='eval_action', required=True))
    for name in ('init', 'questions', 'run', 'score', 'report', 'selftest', 'resolve'):
        p = _call_foreign(lambda: commands.add_parser(name))
        _call_foreign(lambda: p.add_argument('--repo', default=argparse.SUPPRESS))
        cli_plugins.add_format_option(p)
        if name == 'selftest':
            continue
        _call_foreign(lambda: p.add_argument('--run', required=name != 'init'))
        if name == 'init':
            _call_foreign(lambda: p.add_argument('--dream-until', required=True))
            _call_foreign(lambda: p.add_argument('--sessions', default='session_logs', help='In-repo JSON/JSONL sessions; native transcripts limited to 2 MiB per file.'))
            _call_foreign(lambda: p.add_argument('--budget', type=int, default=1200))
            _call_foreign(lambda: p.add_argument('--hints', action='store_true'))
            _call_foreign(lambda: p.add_argument('--allow-post-split-memory', action='store_true'))
        if name in ('questions', 'run', 'score'):
            _call_foreign(lambda: p.add_argument('--max-calls', type=int, default=1 if name == 'questions' else 100))
            _call_foreign(lambda: p.add_argument('--dry-run', action='store_true'))
        if name == 'questions':
            group = _call_foreign(lambda: p.add_mutually_exclusive_group(required=True))
            _call_foreign(lambda: group.add_argument('--from', dest='source'))
            _call_foreign(lambda: group.add_argument('--via', help="Calls a model through your own vendor CLI."))
            _call_foreign(lambda: p.add_argument('--count', type=int, default=10))
        if name == 'run':
            _call_foreign(lambda: p.add_argument('--via', required=True, help="Calls a model through your own vendor CLI."))
            _call_foreign(lambda: p.add_argument('--arms', nargs='+', choices=ev.ARMS))
        if name == 'score':
            _call_foreign(lambda: p.add_argument('--judge', help=JUDGE_UNAVAILABLE))
        if name == 'resolve':
            _call_foreign(lambda: p.add_argument('--resolve', required=True))
            _call_foreign(lambda: p.add_argument('--by', required=True))
            _call_foreign(lambda: p.add_argument('--reason', required=True))
            _call_foreign(lambda: p.add_argument('--as', dest='as_status', required=True, choices=('failed', 'done-lost')))


def dispatch(args):
    values = vars(args)
    op = values['eval_action']
    try:
        if op == 'selftest':
            res = ev.eval_selftest()
            if values.get('format') == 'markdown':
                rows = ["## Memlet eval selftest", "", "Result: " + ("PASS" if res.get('ok') else "FAIL")]
                for protocol, result in res.get('protocols', {}).items():
                    rows += ["", "### " + protocol, result.get('report', {}).get('formatted', '') or str(result.get('error', ''))]
                res['_cli_text'] = '\n'.join(rows) + '\n'
            return res
        options = {'run': values.get('run')}
        names = {'init': ('dream_until', 'sessions', 'budget', 'hints', 'allow_post_split_memory'),
                 'questions': ('source', 'via', 'count', 'max_calls', 'dry_run'),
                 'run': ('via', 'arms', 'max_calls', 'dry_run'),
                 'score': ('judge', 'max_calls', 'dry_run'), 'report': (), 'resolve': ('resolve', 'as_status', 'by', 'reason')}[op]
        options.update({k: values[k] for k in names})
        res = {'init': ev.init_eval, 'questions': ev.seal_questions, 'run': ev.run_eval,
                'score': ev.score_eval, 'report': ev.report_eval, 'resolve': ev.resolve_call}[op](values['repo'], **options)
        if values.get('format') == 'markdown':
            from memory_cli import _text_result
            res['_cli_text'] = (res.get('formatted') or _text_result(res)) + '\n'
        return res
    except (ImportError, AttributeError, ValueError, OSError, KeyError, TypeError, TimeoutError) as exc:
        res = {'ok': False, 'exitCode': 2, 'error': str(exc)}
        if values.get('format') == 'markdown':
            res['_cli_text'] = '- ERROR: ' + str(exc) + '\n'
        return res
