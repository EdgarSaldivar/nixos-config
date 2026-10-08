"""Local controls and a supervised continuous runner."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import signal
import sys
import time

from .core import Arr, Failure, Journal, atomic_json, load_config
from .engine import Runner, inventory, source_key
from . import qa


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/media-optimizer.json')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('run', 'once', 'preflight', 'pause', 'resume', 'audit-av1', 'subtitle-bridge'):
        sub.add_parser(name)
    for name in ('subtitle-seed', 'subtitle-setup'):
        sub.add_parser(name).add_argument('instance', choices=('main', 'anime'))
    status = sub.add_parser('status')
    status.add_argument('--json', action='store_true')
    concurrency = sub.add_parser('concurrency')
    concurrency.add_argument('count', type=int)
    plan = sub.add_parser('plan')
    plan.add_argument('--limit', type=int, default=10)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command.startswith('subtitle-'):
            from . import subtitles
            if args.command == 'subtitle-bridge':
                subtitles.bridge(config)
            elif args.command == 'subtitle-seed':
                subtitles.seed(config, args.instance)
            else:
                subtitles.setup_ready(config, args.instance)
            return 0
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
                print('AV1 files awaiting replacement:', data.get('codec_repairs_pending', 0))
                for job in data['jobs']:
                    if job['state'] == 'complete':
                        continue
                    download = job.get('download') or {}
                    print(f"{job['state']:14} {download.get('progress', 0) or 0:5.1f}% "
                          f"seeds={download.get('num_seeds', '?')} {job['title']}" +
                          (' [pack]' if job.get('pack') else '') + ('; ' + job['error'] if job.get('error') else ''))
                    if job.get('codec_remediation'):
                        print('  AV1 compatibility replacement')
                    if job.get('api_error'):
                        print('  Retaining payload for API retry:', job['api_error'])
                if data.get('at') and time.time() - data['at'] > 120:
                    print('Status is stale; inspect the systemd service.')
            return 0
        if args.command in ('plan', 'audit-av1'):
            apps = {name: Arr(spec) for name, spec in config['apps'].items()}
            records = inventory(apps, config, include_all=args.command == 'audit-av1')
            if args.command == 'audit-av1':
                suspects = [r for r in records if r['codec_remediation'] or not r['video_codec']]
                failures = []
                def inspect(row):
                    try:
                        data = qa.probe(row['path'])
                        if qa.video(data).get('codec_name') == 'av1':
                            return dict(row, codec_remediation=True, video_codec='av1', resolution=qa.resolution(data))
                    except Failure:
                        failures.append(row['path'])
                    return None
                with ThreadPoolExecutor(max_workers=4) as pool:
                    repairs = [r for r in pool.map(inspect, suspects) if r]
                atomic_json(Path(config['state_dir']) / 'codec-remediation.json', repairs)
                atomic_json(Path(config['state_dir']) / 'codec-audit.json',
                            {'scanned': len(records), 'probed': len(suspects), 'av1': len(repairs), 'failures': failures, 'at': time.time()})
                # Compatibility repair takes precedence over size optimization.
                with journal.db:
                    for row in repairs:
                        journal.db.execute('DELETE FROM cooldown WHERE key=?', (source_key(row),))
                for row in repairs:
                    print(json.dumps({k: row.get(k) for k in ('app', 'title', 'item_id', 'resolution', 'size')}))
                print(json.dumps({'scanned_files': len(records), 'probed_files': len(suspects), 'av1_files': len(repairs),
                                  'probe_failures': len(failures)}))
                return 1 if failures else 0
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
