"""CLI plug-in for sealed memory experiments."""
from . import cli_plugins
from . import memlet_eval as ev

COMMANDS = {'eval': {'help': 'Measure memory on sealed held-out questions.', 'product': False, 'group': 'Benchmarks'}}


def register(sub):
    parser = sub.add_parser('eval', help=COMMANDS['eval']['help'])
    parser.add_argument('--repo', default='.')
    commands = parser.add_subparsers(dest='eval_action', required=True)
    for name in ('init', 'questions', 'run', 'score', 'report', 'selftest'):
        p = commands.add_parser(name)
        cli_plugins.add_format_option(p)
        if name == 'selftest':
            continue
        p.add_argument('--run', required=name != 'init')
        if name == 'init':
            p.add_argument('--dream-until', required=True)
            p.add_argument('--sessions', default='session_logs', help='In-repo JSON sessions directory (timestamp/text records).')
            p.add_argument('--budget', type=int, default=1200)
            p.add_argument('--hints', action='store_true')
        if name in ('questions', 'run', 'score'):
            p.add_argument('--max-calls', type=int, default=1 if name == 'questions' else 100)
            p.add_argument('--dry-run', action='store_true')
        if name == 'questions':
            group = p.add_mutually_exclusive_group(required=True)
            group.add_argument('--from', dest='source')
            group.add_argument('--via')
            p.add_argument('--count', type=int, default=10)
        if name == 'run':
            p.add_argument('--via', required=True)
            p.add_argument('--arms', nargs='+', choices=ev.ARMS)
        if name == 'score':
            p.add_argument('--judge')


def dispatch(args):
    values = vars(args)
    op = values['eval_action']
    try:
        if op == 'selftest':
            return ev.eval_selftest()
        options = {'run': values.get('run')}
        names = {'init': ('dream_until', 'sessions', 'budget', 'hints'),
                 'questions': ('source', 'via', 'count', 'max_calls', 'dry_run'),
                 'run': ('via', 'arms', 'max_calls', 'dry_run'),
                 'score': ('judge', 'max_calls', 'dry_run'), 'report': ()}[op]
        options.update({k: values[k] for k in names})
        return {'init': ev.init_eval, 'questions': ev.seal_questions, 'run': ev.run_eval,
                'score': ev.score_eval, 'report': ev.report_eval}[op](values['repo'], **options)
    except (ValueError, OSError, KeyError, TypeError, TimeoutError) as exc:
        return {'ok': False, 'exitCode': 2, 'error': str(exc)}
