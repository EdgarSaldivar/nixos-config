"""Local controls and a supervised continuous runner."""
import argparse
import json
from pathlib import Path
import signal
import sys
import time

from .core import Arr, Failure, Journal, load_config
from .engine import Runner, inventory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/media-optimizer.json')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('run', 'once', 'preflight', 'pause', 'resume'):
        sub.add_parser(name)
    status = sub.add_parser('status')
    status.add_argument('--json', action='store_true')
    concurrency = sub.add_parser('concurrency')
    concurrency.add_argument('count', type=int)
    plan = sub.add_parser('plan')
    plan.add_argument('--limit', type=int, default=10)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        journal = Journal(config['state_dir'])
        if args.command == 'concurrency':
            if args.count < 1:
                raise Failure('concurrency must be positive')
            journal.set_setting('concurrency', args.count)
            print('Optimization concurrency:', args.count)
            return 0
        if args.command in ('pause', 'resume'):
            journal.set_setting('paused', args.command == 'pause')
            print('New optimization admissions', 'paused' if args.command == 'pause' else 'resumed')
            return 0
        if args.command == 'status':
            path = Path(config['state_dir']) / 'status.json'
            data = json.loads(path.read_text()) if path.exists() else {'jobs': [], 'active': 0, 'completed': 0, 'logical_savings_bytes': 0}
            data['concurrency'] = journal.setting('concurrency', config['concurrency'])
            data['paused'] = journal.setting('paused', False)
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                print(f"Optimizer {data['active']}/{data['concurrency']} slots; {data['completed']} completed; "
                      f"{data['logical_savings_bytes'] / 1024**3:.1f} GiB logical savings; paused={data['paused']}")
                if data.get('searching'):
                    print('Searching:', data['searching'])
                print('Verification workers:', data.get('verifying', 0))
                for job in data['jobs']:
                    if job['state'] == 'complete':
                        continue
                    download = job.get('download') or {}
                    print(f"{job['state']:14} {download.get('progress', 0) or 0:5.1f}% "
                          f"seeds={download.get('num_seeds', '?')} {job['title']}" +
                          (' [pack]' if job.get('pack') else '') + ('; ' + job['error'] if job.get('error') else ''))
                if data.get('at') and time.time() - data['at'] > 120:
                    print('Status is stale; inspect the systemd service.')
            return 0
        if args.command == 'plan':
            apps = {name: Arr(spec) for name, spec in config['apps'].items()}
            records = inventory(apps, config)
            for row in records[:args.limit]:
                print(json.dumps({k: row.get(k) for k in ['app', 'item_id', 'series_id', 'title', 'size', 'resolution']}))
            print('Candidates:', len(records))
            return 0
        runner = Runner(config, journal)
        with journal.lock():
            runner.preflight()
            if args.command == 'preflight':
                print('Public client, hardlinks, zero recycle retention and staging verified')
                return 0
            stopping = False
            def stop(_signum, _frame):
                nonlocal stopping
                stopping = True
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            while not stopping:
                try:
                    runner.tick()
                except Exception as exc:
                    runner.error(str(exc) if isinstance(exc, Failure) else 'cycle: ' + type(exc).__name__)
                if args.command == 'once':
                    break
                for _ in range(10):
                    if stopping:
                        break
                    time.sleep(1)
            runner.search_pool.shutdown(wait=False, cancel_futures=True)
            runner.qa_pool.shutdown(wait=False, cancel_futures=True)
        return 0
    except Exception as exc:
        print('media optimizer: ' + (str(exc) if isinstance(exc, Failure) else type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
