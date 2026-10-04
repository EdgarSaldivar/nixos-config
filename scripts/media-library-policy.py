#!/usr/bin/env python3
"""Offline-reviewable *arr configuration planner; no media actions."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

POLICY = Path(__file__).resolve().parents[1] / 'policies/media-library-policy.json'
COLLECTIONS = ('qualityprofile', 'qualitydefinition', 'customformat', 'releaseprofile')
IGNORED_CODEC_TERMS = {'x264', '264', 'h264', 'h.264'}


class PolicyError(Exception):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def load_policy(path=POLICY):
    policy = json.loads(Path(path).read_text())
    if policy.get('schema') != 1 or policy.get('automaticGrab') is not False:
        raise PolicyError('invalid policy schema or automaticGrab setting')
    return policy


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.media-policy-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_snapshot(snapshot, policy):
    for app, targets in policy['targets'].items():
        if app not in snapshot:
            raise PolicyError(f'missing app access: {app}')
        for collection in COLLECTIONS:
            if not isinstance(snapshot[app].get(collection), list):
                raise PolicyError(f'missing {app}/{collection} access')
        profiles = {str(p['id']): p for p in snapshot[app]['qualityprofile']}
        for profile_id, mode in targets.items():
            if mode not in ('1080', '2160') or profile_id not in profiles:
                raise PolicyError(f'missing target profile {app}:{profile_id}:{mode}')
            if mode == '1080' and any(resolution(q) >= 2160 for q in enabled_qualities(profiles[profile_id]['items'])):
                raise PolicyError(f'1080 target has enabled 4K quality: {app}:{profile_id}')


def flatten(items):
    for item in items:
        if item.get('quality'):
            yield item
        yield from flatten(item.get('items', []))


def enabled_qualities(items, parent_allowed=True):
    for item in items:
        enabled = parent_allowed and item.get('allowed', False)
        if item.get('quality') and enabled:
            yield item
        yield from enabled_qualities(item.get('items', []), enabled)


def resolution(item):
    q = item.get('quality', {})
    if q.get('resolution'):
        return int(q['resolution'])
    name = q.get('name', item.get('name', ''))
    match = re.search(r'(480|720|1080|2160)p?', name, re.I)
    return int(match.group(1)) if match else 0


def quality_name(item):
    return item.get('quality', {}).get('name', '')


def is_bad_quality(item):
    return bool(re.search(r'CAM|WORKPRINT|TELECINE|TELESYNC|\bTS\b|SCREENER|BR-DISK|RAW-HD', quality_name(item), re.I))


def tune_quality(items):
    items = copy.deepcopy(items)
    # Groups retain their actual API IDs. Move a flat Bluray encode into its WEB group.
    cutoff_map = {}
    for res in (1080, 2160):
        group = next((x for x in items if x.get('id') and x.get('name') == f'WEB {res}p'), None)
        if not group or not group.get('allowed'):
            continue
        encodes = [x for x in items if resolution(x) == res and re.search(r'bluray|blu-ray', quality_name(x), re.I)
                   and not re.search(r'remux|disk', quality_name(x), re.I)]
        for encode in encodes:
            items.remove(encode)
            if not any(x.get('quality', {}).get('id') == encode.get('quality', {}).get('id') for x in group['items']):
                group['items'].append(encode)
            cutoff_map[encode['quality']['id']] = group['id']
        # Keep existing remux availability, placing it below the comparable group.
        remuxes = [x for x in items if resolution(x) == res and re.search('remux', quality_name(x), re.I)]
        for remux in remuxes:
            items.remove(remux)
        insert_at = items.index(group)
        items[insert_at:insert_at] = remuxes
    for item in flatten(items):
        if is_bad_quality(item):
            item['allowed'] = False
    return items, cutoff_map


def valid_cutoff(items, cutoff):
    for item in items:
        if item.get('id') == cutoff:
            return bool(item.get('allowed') and any(enabled_qualities(item.get('items', []))))
    for item in enabled_qualities(items):
        if item.get('quality', {}).get('id') == cutoff:
            return True
    return False


def cf_payload(spec):
    return {'name': spec['name'], 'includeCustomFormatWhenRenaming': False,
            'specifications': [{'name': 'Release Title', 'implementation': 'ReleaseTitleSpecification',
                                'negate': False, 'required': True, 'fields': [{'name': 'value', 'value': spec['regex']}]}]}


def owned_cf_equal(current, wanted):
    def owned_specs(cf):
        return [(spec.get('name'), spec.get('implementation'), spec.get('negate'), spec.get('required'),
                 next((field.get('value') for field in spec.get('fields', []) if field.get('name') == 'value'), None))
                for spec in cf.get('specifications', [])]
    return (current.get('name') == wanted['name']
            and current.get('includeCustomFormatWhenRenaming', False) == wanted['includeCustomFormatWhenRenaming']
            and owned_specs(current) == owned_specs(wanted))


def anime_score(name, old_score, policy, source_max):
    if name in policy['animeScores']:
        return policy['animeScores'][name]
    if name.startswith(('Anime BD Tier ', 'Anime Web Tier ')):
        # Tier scores are alternatives within a source family; preserve relative order.
        return max(1, round(old_score * policy['animeSourceMax'] / source_max))
    return old_score


def desired_profile(profile, app, mode, policy):
    result = copy.deepcopy(profile)
    result['upgradeAllowed'] = False
    result['minFormatScore'] = 0
    result['items'], cutoff_map = tune_quality(profile['items'])
    result['cutoff'] = cutoff_map.get(profile['cutoff'], profile['cutoff'])
    if not valid_cutoff(result['items'], result['cutoff']):
        raise PolicyError(f'cutoff is not an enabled quality/group: {app}:{profile["id"]}')
    formats = [copy.deepcopy(x) for x in profile.get('formatItems', [])]
    if app == 'animearr':
        tier_scores = [x.get('score', 0) for x in formats if x.get('name', '').startswith(('Anime BD Tier ', 'Anime Web Tier '))]
        already_normalized = tier_scores and max(tier_scores) <= policy['animeSourceMax']
        for item in formats:
            if not (already_normalized and item.get('name', '').startswith(('Anime BD Tier ', 'Anime Web Tier '))):
                item['score'] = anime_score(item.get('name', ''), item.get('score', 0), policy, max(tier_scores or [1]))
    else:
        formats = [x for x in formats if x.get('name') not in ('MLP Anime English and native audio',)]
    result['formatItems'] = formats
    return result


def build_operations(snapshot, policy):
    validate_snapshot(snapshot, policy)
    operations = []
    for app, targets in policy['targets'].items():
        state = snapshot[app]
        by_name = {x['name']: x for x in state['customformat']}
        if len(by_name) != len(state['customformat']):
            raise PolicyError(f'duplicate custom format names: {app}')
        for spec in policy['customFormats']:
            if spec.get('animeOnly') and app != 'animearr':
                continue
            wanted = cf_payload(spec)
            current = by_name.get(spec['name'])
            if current is None:
                operations.append({'app': app, 'collection': 'customformat', 'action': 'create', 'name': spec['name'], 'desired': wanted})
            elif not owned_cf_equal(current, wanted):
                wanted['id'] = current['id']
                operations.append({'app': app, 'collection': 'customformat', 'action': 'update', 'id': current['id'], 'name': spec['name'], 'before': current, 'desired': wanted})
        for profile in state['qualityprofile']:
            mode = targets.get(str(profile['id']))
            if mode is None:
                continue
            desired = desired_profile(profile, app, mode, policy)
            existing_formats = {x.get('name'): x for x in desired['formatItems']}
            for spec in policy['customFormats']:
                if spec.get('animeOnly') and app != 'animearr':
                    continue
                old_cf = by_name.get(spec['name'])
                # New IDs remain symbolic until creation returns a real ID.
                new_item = {'format': old_cf['id'] if old_cf else {'newCustomFormat': spec['name']}, 'name': spec['name'], 'score': spec['score']}
                if spec['name'] in existing_formats:
                    existing_formats[spec['name']].update(new_item)
                else:
                    desired['formatItems'].append(new_item)
            keys = ('cutoff', 'upgradeAllowed', 'minFormatScore', 'items', 'formatItems')
            if any(profile.get(k) != desired.get(k) for k in keys):
                operations.append({'app': app, 'collection': 'qualityprofile', 'action': 'update', 'id': profile['id'], 'mode': mode, 'before': profile, 'desired': desired})
        for definition in state['qualitydefinition']:
            res = resolution(definition)
            mode = str(res)
            if mode not in policy['preferredMBPerMinute'][app]:
                continue
            minimum = definition.get('minSize') or 0
            target = max(minimum, policy['preferredMBPerMinute'][app][mode])
            desired = copy.deepcopy(definition)
            desired['preferredSize'] = target
            desired['maxSize'] = None
            if any(definition.get(k) != desired[k] for k in ('preferredSize', 'maxSize')):
                operations.append({'app': app, 'collection': 'qualitydefinition', 'action': 'update', 'id': definition['id'], 'before': definition, 'desired': desired})
        for release in state['releaseprofile']:
            old = release.get('ignored', [])
            kept = [term for term in old if term.lower() not in IGNORED_CODEC_TERMS]
            if kept != old:
                desired = copy.deepcopy(release)
                desired['ignored'] = kept
                action = 'delete' if not kept and not release.get('required') else 'update'
                operations.append({'app': app, 'collection': 'releaseprofile', 'action': action, 'id': release['id'], 'before': release, 'desired': desired})
    return operations


def make_plan(snapshot, policy):
    plan = {'schema': 1, 'policyHash': digest(policy), 'stateHash': digest(snapshot),
            'operations': build_operations(snapshot, policy)}
    plan['reviewHash'] = digest(plan)
    return plan


class APIClient:
    def __init__(self, url, api_key):
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment or not api_key:
            raise PolicyError('invalid URL or missing API key')
        self.url = url.rstrip('/')
        self.api_key = api_key

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.url + '/api/v3/' + path, data=data, method=method,
                          headers={'X-Api-Key': self.api_key, 'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=20) as response:
                content = response.read()
            return json.loads(content) if content else {}
        except Exception as exc:
            # Never expose response bodies, request headers, or URLs with credentials.
            raise PolicyError(f'API {method} {path.split("/")[0]} failed ({type(exc).__name__})') from None

    def collection(self, name):
        return self.request('GET', name)

    def item(self, name, item_id):
        return self.request('GET', f'{name}/{item_id}')

    def backup(self, timeout=300, interval=2):
        result = self.request('POST', 'command', {'name': 'Backup'})
        command_id = result.get('id')
        if not isinstance(command_id, int):
            raise PolicyError('backup command returned no ID')
        deadline = time.monotonic() + timeout
        while True:
            command = self.item('command', command_id)
            status = str(command.get('status', '')).lower()
            if status == 'completed':
                return command_id
            if status in ('failed', 'aborted', 'cancelled'):
                raise PolicyError('backup failed')
            if time.monotonic() >= deadline:
                raise PolicyError('backup did not complete before timeout')
            time.sleep(min(interval, max(0, deadline - time.monotonic())))


def fetch_snapshot(clients):
    return {app: {name: client.collection(name) for name in COLLECTIONS} for app, client in clients.items()}


def bind_format_ids(value, ids):
    result = copy.deepcopy(value)
    if 'formatItems' in result:
        for item in result['formatItems']:
            if isinstance(item.get('format'), dict):
                name = item['format'].get('newCustomFormat')
                if name not in ids:
                    raise PolicyError(f'new custom format has no returned ID: {name}')
                item['format'] = ids[name]
    return result


def readback_matches(actual, desired, collection):
    if collection == 'customformat':
        return actual.get('id') == desired.get('id') and owned_cf_equal(actual, desired)
    if collection == 'qualityprofile':
        def shape(items):
            return [(item.get('id'), item.get('name'), item.get('allowed'),
                     item.get('quality', {}).get('id'), item.get('quality', {}).get('name'),
                     item.get('quality', {}).get('resolution'), shape(item.get('items', []))) for item in items]
        return (all(actual.get(key) == desired.get(key) for key in ('id', 'cutoff', 'upgradeAllowed', 'minFormatScore'))
                and shape(actual.get('items', [])) == shape(desired.get('items', []))
                and [(x.get('format'), x.get('score')) for x in actual.get('formatItems', [])]
                == [(x.get('format'), x.get('score')) for x in desired.get('formatItems', [])])
    keys = {'qualitydefinition': ('id', 'minSize', 'preferredSize', 'maxSize'),
            'releaseprofile': ('id', 'enabled', 'ignored', 'required', 'tags')}[collection]
    return all(actual.get(key) == desired.get(key) for key in keys)


def apply_plan(reviewed, clients, policy, progress_path, backup_timeout=300):
    snapshot = fetch_snapshot(clients)  # all access failures precede every mutation
    current = make_plan(snapshot, policy)
    if (reviewed.get('reviewHash') != digest({k: v for k, v in reviewed.items() if k != 'reviewHash'})
            or current != reviewed):
        raise PolicyError('reviewed plan hash or current state differs; replan')
    operations = current['operations']  # never replay operation bodies from untrusted input
    if not operations:
        return {'status': 'no changes', 'completed': []}
    progress = {'schema': 1, 'reviewHash': current['reviewHash'], 'status': 'pending backups',
                'before': snapshot, 'backups': {}, 'completed': [], 'pending': operations, 'inFlight': None}
    atomic_json(progress_path, progress)
    try:
        for app in policy['targets']:
            progress['backups'][app] = clients[app].backup(timeout=backup_timeout)
            atomic_json(progress_path, progress)
        if fetch_snapshot(clients) != snapshot:
            raise PolicyError('configuration changed during backups; replan')
        progress['status'] = 'writing'
        atomic_json(progress_path, progress)
        ids = {(app, cf['name']): cf['id'] for app, state in snapshot.items() for cf in state['customformat']}
        for op in operations:
            client = clients[op['app']]
            desired = bind_format_ids(op['desired'], {name: cf_id for (app, name), cf_id in ids.items() if app == op['app']})
            progress['inFlight'] = {'app': op['app'], 'collection': op['collection'],
                                    'action': op['action'], 'id': op.get('id'),
                                    'name': op.get('name'), 'desired': desired}
            atomic_json(progress_path, progress)
            if op['action'] == 'create':
                result = client.request('POST', op['collection'], desired)
                item_id = result.get('id')
                if not isinstance(item_id, int) or item_id <= 0:
                    raise PolicyError('custom format creation returned no real ID')
                desired['id'] = item_id
                ids[op['app'], op['name']] = item_id
            else:
                item_id = op['id']
                if op['action'] == 'delete':
                    client.request('DELETE', f'{op["collection"]}/{item_id}')
                else:
                    client.request('PUT', f'{op["collection"]}/{item_id}', desired)
            # Persist returned IDs and attempted write before readback, even on mismatch.
            entry = {'app': op['app'], 'collection': op['collection'], 'action': op['action'],
                     'id': item_id, 'name': op.get('name'), 'before': op.get('before'), 'desired': desired,
                     'verified': False}
            progress['completed'].append(entry)
            progress['pending'] = operations[len(progress['completed']):]
            progress['inFlight'] = None
            atomic_json(progress_path, progress)
            if op['action'] == 'delete':
                matches = all(x['id'] != item_id for x in client.collection(op['collection']))
            else:
                actual = client.item(op['collection'], item_id)
                matches = readback_matches(actual, desired, op['collection'])
            if not matches:
                raise PolicyError(f'readback mismatch: {op["app"]}/{op["collection"]}/{item_id}')
            entry['verified'] = True
            atomic_json(progress_path, progress)
        progress['status'] = 'completed'
        atomic_json(progress_path, progress)
    except Exception as exc:
        progress['status'] = 'failed'
        progress['error'] = str(exc) if isinstance(exc, PolicyError) else type(exc).__name__
        atomic_json(progress_path, progress)
        raise
    return progress


def inventory_candidates(records, policy):
    seen = set()
    result = []
    campaign = policy['campaign']
    protected_inodes = {(row['device'], row['inode']) for row in records
                        if row.get('device') is not None and row.get('inode') is not None
                        and re.search(campaign['protectedTitleRegex'], str(row.get('title') or ''), re.I)}
    for row in records:
        title = str(row.get('title') or '')
        if re.search(campaign['protectedTitleRegex'], title, re.I):
            continue
        device, inode = row.get('device'), row.get('inode')
        key = (device, inode) if device is not None and inode is not None else None
        if key and (key in seen or key in protected_inodes):
            continue
        mode = str(row.get('resolution', '')).replace('p', '')
        app = row.get('app')
        target = policy['preferredMBPerMinute'].get(app, {}).get(mode)
        media_info = row.get('mediaInfo') or {}
        runtime = media_info.get('runTime')
        size = row.get('size')
        if not target or not isinstance(runtime, (int, float)) or runtime <= 0 or not isinstance(size, int) or size <= 0:
            continue
        estimate = int(runtime * target * 1024**2)
        saving = max(0, size - estimate)
        if saving / size < campaign['ordinaryMinimumSavingsFraction']:
            continue
        if key:
            seen.add(key)
        result.append({k: row.get(k) for k in ('app', 'itemId', 'fileId', 'path', 'resolution', 'device', 'inode')} |
                      {'title': title, 'mediaInfo': {'runTime': runtime}, 'runtimeMinutes': runtime,
                       'currentBytes': size, 'softTargetBytes': estimate,
                       'potentialLogicalSavingsBytes': saving})
    return sorted(result, key=lambda x: x['potentialLogicalSavingsBytes'], reverse=True)


def credentials(args, policy):
    clients = {}
    provided_urls = dict(x.split('=', 1) for x in args.url)
    provided_keys = dict(x.split('=', 1) for x in args.key)
    for app in policy['targets']:
        prefix = f'MEDIA_POLICY_{app.upper()}_'
        url = provided_urls.get(app) or os.environ.get(prefix + 'URL')
        key = provided_keys.get(app) or os.environ.get(prefix + 'API_KEY')
        if not url or not key:
            raise PolicyError(f'missing caller supplied access for {app}')
        clients[app] = APIClient(url, key)
    return clients


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'apply'):
        p = sub.add_parser(name)
        p.add_argument('--url', action='append', default=[], metavar='APP=URL')
        p.add_argument('--key', action='append', default=[], metavar='APP=API_KEY')
        p.add_argument('--policy', type=Path, default=POLICY)
        if name == 'plan':
            p.add_argument('--fixture', type=Path, help='offline snapshot JSON')
            p.add_argument('--output', type=Path, required=True)
        else:
            p.add_argument('--reviewed', type=Path, required=True)
            p.add_argument('--review-hash', required=True, help='exact reviewHash printed by plan')
            p.add_argument('--progress', type=Path, required=True)
            p.add_argument('--backup-timeout', type=int, default=300)
    p = sub.add_parser('inventory')
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--policy', type=Path, default=POLICY)
    args = parser.parse_args(argv)
    policy = load_policy(args.policy)
    if args.command == 'inventory':
        atomic_json(args.output, {'candidates': inventory_candidates(json.loads(args.input.read_text()), policy), 'campaign': policy['campaign']})
    elif args.command == 'plan':
        snapshot = json.loads(args.fixture.read_text()) if args.fixture else fetch_snapshot(credentials(args, policy))
        plan = make_plan(snapshot, policy)
        atomic_json(args.output, plan)
        print(f'{len(plan["operations"])} operations; review hash {plan["reviewHash"]}')
    else:
        reviewed = json.loads(args.reviewed.read_text())
        if args.review_hash != reviewed.get('reviewHash'):
            raise PolicyError('explicit reviewed hash does not match plan')
        result = apply_plan(reviewed, credentials(args, policy), policy, args.progress, args.backup_timeout)
        print(f'{result["status"]}; progress {args.progress}')


if __name__ == '__main__':
    try:
        main()
    except (PolicyError, ValueError, OSError, KeyError, TypeError) as exc:
        # Paths and API keys are deliberately absent from errors.
        print(f'media policy: {exc if isinstance(exc, PolicyError) else type(exc).__name__}', file=sys.stderr)
        sys.exit(1)
