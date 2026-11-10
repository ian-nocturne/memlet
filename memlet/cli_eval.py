"""CLI plug-in for sealed memory experiments."""
import argparse
from . import cli_plugins
from . import memlet_eval as ev

COMMANDS = {'eval': {'help': 'Measure memory on sealed held-out questions.', 'product': False, 'group': 'Benchmarks'}}


def register(sub):
    parser = sub.add_parser('eval', help=COMMANDS['eval']['help'])
    parser.add_argument('--repo', default='.')
    commands = parser.add_subparsers(dest='eval_action', required=True)
    for name in ('init', 'questions', 'run', 'score', 'report', 'selftest', 'resolve'):
        p = commands.add_parser(name)
        p.add_argument('--repo', default=argparse.SUPPRESS)
        cli_plugins.add_format_option(p)
        if name == 'selftest':
            continue
        p.add_argument('--run', required=name != 'init')
        if name == 'init':
            p.add_argument('--dream-until', required=True)
            p.add_argument('--sessions', default='session_logs', help='In-repo JSON/JSONL sessions; native transcripts limited to 2 MiB per file.')
            p.add_argument('--budget', type=int, default=1200)
            p.add_argument('--hints', action='store_true')
            p.add_argument('--allow-post-split-memory', action='store_true')
        if name in ('questions', 'run', 'score'):
            p.add_argument('--max-calls', type=int, default=1 if name == 'questions' else 100)
            p.add_argument('--dry-run', action='store_true')
        if name == 'questions':
            group = p.add_mutually_exclusive_group(required=True)
            group.add_argument('--from', dest='source')
            group.add_argument('--via', help="Calls a model through your own vendor CLI.")
            p.add_argument('--count', type=int, default=10)
        if name == 'run':
            p.add_argument('--via', required=True, help="Calls a model through your own vendor CLI.")
            p.add_argument('--arms', nargs='+', choices=ev.ARMS)
        if name == 'score':
            p.add_argument('--judge', help="Calls a judge model through your own vendor CLI.")
        if name == 'resolve':
            p.add_argument('--resolve', required=True)
            p.add_argument('--as', dest='as_status', required=True, choices=('failed', 'done-lost'))


def dispatch(args):
    values = vars(args)
    op = values['eval_action']
    try:
        if op == 'selftest':
            return ev.eval_selftest()
        options = {'run': values.get('run')}
        names = {'init': ('dream_until', 'sessions', 'budget', 'hints', 'allow_post_split_memory'),
                 'questions': ('source', 'via', 'count', 'max_calls', 'dry_run'),
                 'run': ('via', 'arms', 'max_calls', 'dry_run'),
                 'score': ('judge', 'max_calls', 'dry_run'), 'report': (), 'resolve': ('resolve', 'as_status')}[op]
        options.update({k: values[k] for k in names})
        res = {'init': ev.init_eval, 'questions': ev.seal_questions, 'run': ev.run_eval,
                'score': ev.score_eval, 'report': ev.report_eval, 'resolve': ev.resolve_call}[op](values['repo'], **options)
        if values.get('format') == 'markdown' and 'formatted' in res:
            res['_cli_text'] = res['formatted'] + '\n'
        return res
    except (ImportError, AttributeError, ValueError, OSError, KeyError, TypeError, TimeoutError) as exc:
        return {'ok': False, 'exitCode': 2, 'error': str(exc)}
