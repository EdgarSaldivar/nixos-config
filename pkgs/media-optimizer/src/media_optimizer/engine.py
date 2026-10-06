"""Continuous bounded campaign. Slow external work runs outside the journal loop."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import errno
import json
import os
from pathlib import Path
import re
import time
import uuid

from .core import (Arr, Deluge, Failure, Journal, Review, atomic_json, fetch_torrent,
                   fingerprint, identity, stall_observation)
from . import qa

ACTIVE = {'submitting', 'downloading', 'verifying', 'importing'}
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.m4v', '.avi'}
NATIVE = {'Japanese': 'jpn', 'Korean': 'kor', 'Chinese': 'zho', 'English': 'eng'}


def runtime_minutes(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and re.fullmatch(r'\d+:\d{2}:\d{2}(?:\.\d+)?', value):
        hours, minutes, seconds = value.split(':')
        return int(hours) * 60 + int(minutes) + float(seconds) / 60
    return 0


def source_key(source):
    ids = source.get('episode_ids') or [source['item_id']]
    return source['app'] + ':' + ','.join(str(x) for x in ids)


def inventory(apps, config, include_all=False):
    records = []
    for name, app in apps.items():
        if name == 'radarr':
            for movie in app.request('movie'):
                f = movie.get('movieFile') or {}
                if not f.get('path'):
                    continue
                records.append({'app': name, 'item_id': movie['id'], 'file_id': f['id'], 'path': f['path'],
                                'title': movie['title'], 'size': f.get('size', 0),
                                'resolution': f.get('quality', {}).get('quality', {}).get('resolution', 0),
                                'runtime': f.get('mediaInfo', {}).get('runTime') or movie.get('runtime', 0),
                                'video_codec': f.get('mediaInfo', {}).get('videoCodec', ''),
                                'native': NATIVE.get(movie.get('originalLanguage', {}).get('name'), 'eng'),
                                'edition': f.get('edition', '')})
        else:
            series = app.request('series')
            def files(show):
                fs = app.request('episodefile', seriesId=show['id'])
                episodes = app.request('episode', seriesId=show['id'])
                by_file = {}
                for episode in episodes:
                    if episode.get('episodeFileId'):
                        by_file.setdefault(episode['episodeFileId'], []).append(episode)
                return show, fs, by_file
            with ThreadPoolExecutor(max_workers=4) as pool:
                for show, fs, by_file in pool.map(files, series):
                    for f in fs:
                        eps = by_file.get(f['id'], [])
                        if not eps:
                            continue
                        records.append({'app': name, 'item_id': eps[0]['id'], 'file_id': f['id'],
                                        'series_id': show['id'], 'season': eps[0]['seasonNumber'],
                                        'episode_ids': sorted(x['id'] for x in eps),
                                        'episode_numbers': sorted(x['episodeNumber'] for x in eps),
                                        'path': f.get('path') or str(Path(show['path']) / f['relativePath']),
                                        'title': show['title'], 'size': f.get('size', 0),
                                        'resolution': f.get('quality', {}).get('quality', {}).get('resolution', 0),
                                        'runtime': f.get('mediaInfo', {}).get('runTime') or show.get('runtime', 0),
                                        'video_codec': f.get('mediaInfo', {}).get('videoCodec', ''),
                                        'native': NATIVE.get(show.get('originalLanguage', {}).get('name'),
                                                             'jpn' if name == 'animearr' else 'eng')})
    protected = re.compile(config['protected_title_regex'])
    protected_inodes = set()
    valid = []
    for row in records:
        if Path(row['path']).suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        row['runtime'] = runtime_minutes(row['runtime'])
        row['codec_remediation'] = qa.is_av1(row['video_codec']) or qa.is_av1(Path(row['path']).name)
        try:
            row['identity'] = identity(row['path'])
            row['size'] = row['identity']['size']
        except (OSError, Failure):
            continue
        pair = row['identity']['device'], row['identity']['inode']
        if protected.search(row['title']):
            protected_inodes.add(pair)
        valid.append(row)
    seen = set()
    result = []
    for row in sorted(valid, key=lambda x: (not x['codec_remediation'], -x['size'])):
        pair = row['identity']['device'], row['identity']['inode']
        key = row['path'] if include_all else pair
        if key in seen or (not include_all and pair in protected_inodes and not row['codec_remediation']):
            continue
        seen.add(key)
        if include_all or row['codec_remediation']:
            result.append(row)
            continue
        target = config['preferred_mb_per_minute'][row['app']].get(str(row['resolution']))
        runtime = row.get('runtime')
        if not target or not isinstance(runtime, (int, float)) or runtime <= 0:
            continue
        if runtime * target * 1024**2 > row['size'] * (1 - config['minimum_savings']):
            continue
        result.append(row)
    return result


def permitted_rejections(release):
    patterns = ('existing file', 'existing episode', 'not an upgrade', 'custom format score',
                'quality for existing', 'already downloaded', 'cutoff already met')
    return all(any(term in str(r).lower() for term in patterns) for r in release.get('rejections', []))


def rank_releases(releases, source, season_sources, config):
    eligible = []
    for release in releases:
        title = release.get('title', '')
        if qa.is_av1(title) or any(qa.is_av1(f.get('name', '')) for f in release.get('customFormats', [])):
            continue
        if source.get('requires_hdr') and re.search(r'(?i)\bSDR\b', title):
            continue
        if release.get('protocol') != 'torrent' or int(release.get('seeders') or 0) < 1:
            continue
        if re.search(r'(?i)ai[ ._-]?(?:enhanced|upscal)|\bRIFE\b|interpolat|\bCAM\b|\bTELESYNC\b', title):
            continue
        if not permitted_rejections(release):
            continue
        res = release.get('quality', {}).get('quality', {}).get('resolution', 0)
        if res < source['resolution']:
            continue
        if res > source['resolution']:
            continue
        if source['app'] == 'radarr':
            if release.get('mappedMovieId') != source['item_id']:
                continue
            if (source.get('edition') or release.get('edition')) and source.get('edition', '').casefold() != release.get('edition', '').casefold():
                continue
            targets = [source]
        else:
            if release.get('mappedSeriesId') != source['series_id']:
                continue
            mapped = {x['id'] for x in release.get('mappedEpisodeInfo', []) if isinstance(x, dict) and x.get('id')}
            if release.get('fullSeason'):
                # Parsed series/season, and all old files remain independently
                # subject to the saving/quality gate when the pack is imported.
                if release.get('seasonNumber') != source['season']:
                    continue
                targets = season_sources
                expected = {x for t in targets for x in t['episode_ids']}
                if mapped and not expected <= mapped:
                    continue
            else:
                if mapped != set(source['episode_ids']):
                    continue
                targets = [source]
        size = release.get('size', 0)
        if not size or (not source.get('codec_remediation')
                        and size > sum(t['size'] for t in targets) * (1 - config['minimum_savings'])):
            continue
        score = int(release.get('customFormatScore') or 0)
        if score < 0:
            continue
        # A small CF delta (pack/HDR sub-bonus) does not override live seed
        # evidence. Codec and language tiers remain strong preferences.
        tier = score // 500
        seeds = int(release.get('seeders') or 0)
        availability = 0 if seeds < 4 else 1 if seeds < 16 else 2
        eligible.append((tier, availability, bool(release.get('fullSeason')), seeds, score, -size, release, targets))
    # Prefer advertised 7.1 only among otherwise equal candidates, and only
    # within 20% of the smallest comparable release. Stream QA is authoritative;
    # this title hint cannot outweigh codec/language, availability, or pack rank.
    smallest = {}
    for entry in eligible:
        group = entry[:5]
        smallest[group] = min(smallest.get(group, -entry[5]), -entry[5])
    def preference(entry):
        seven_one = bool(re.search(r'(?<![0-9])7[ ._-]1(?![A-Za-z0-9])', entry[-2]['title']))
        affordable = -entry[5] <= smallest[entry[:5]] * 1.2
        return entry[:5] + (seven_one and affordable, entry[5])
    eligible.sort(key=preference, reverse=True)
    return [(x[-2], x[-1]) for x in eligible]


def current_source(app, source):
    if source['app'] == 'radarr':
        f = app.request('movie/' + str(source['item_id'])).get('movieFile') or {}
    else:
        eps = [app.request('episode/' + str(x)) for x in source['episode_ids']]
        if len({x.get('episodeFileId') for x in eps}) != 1:
            raise Failure('episode file mapping changed')
        f = eps[0].get('episodeFile') or app.request('episodefile/' + str(eps[0]['episodeFileId']))
    return f


def source_unchanged(app, source):
    f = current_source(app, source)
    return f.get('id') == source['file_id'] and f.get('path') == source['path'] and identity(source['path']) == source['identity']


class Runner:
    def __init__(self, config, journal=None, apps=None, deluge=None):
        self.config = config
        self.journal = journal or Journal(config['state_dir'])
        self.apps = apps or {name: Arr(spec) for name, spec in config['apps'].items()}
        self.deluge = deluge or Deluge(config, self.apps)
        self.search_pool = ThreadPoolExecutor(max_workers=1)
        self.qa_pool = ThreadPoolExecutor(max_workers=config['verification_concurrency'])
        self.inventory_future = None
        self.search_future = None
        self.search_source = None
        self.qa_futures = {}
        self.inventory_at = 0
        self.records = []
        self.next_inventory = 0
        self.errors = []
        self.io_blocked_until = 0
        cache = Path(config['state_dir']) / 'inventory.json'
        if cache.exists():
            self.records = json.loads(cache.read_text())
            self.inventory_at = cache.stat().st_mtime
        self.records = self.merge_codec_repairs(self.records)
        self.seed_legacy_cooldowns()

    def merge_codec_repairs(self, records):
        repairs = Path(self.config['state_dir']) / 'codec-remediation.json'
        if repairs.exists():
            current = []
            for row in json.loads(repairs.read_text()):
                try:
                    if identity(row['path']) == row['identity']:
                        current.append(row)
                except (Failure, OSError):
                    continue
            paths = {r['path'] for r in current}
            return current + [r for r in records if r['path'] not in paths]
        return records

    def seed_legacy_cooldowns(self):
        if self.journal.setting('legacy_cooldowns_loaded', False):
            return
        for path in Path(self.config['legacy_state_dir']).glob('pilot-*.json'):
            data = json.loads(path.read_text())
            item_id = data.get('episodeId') or data.get('movieId')
            if not item_id and data.get('app') == 'radarr':
                item_id = data.get('original', {}).get('movieId')
            if not item_id:
                # Filename is an independently recorded exact pilot ID.
                match = re.search(r'-(\d+)\.json$', path.name)
                item_id = int(match[1]) if match else None
            if not item_id or not str(data.get('status', '')).startswith('imported'):
                continue
            until = data.get('cooldownUntil')
            try:
                end = float(until) if isinstance(until, (int, float)) else datetime.fromisoformat(until.replace('Z', '+00:00')).timestamp()
            except (ValueError, TypeError, AttributeError):
                end = time.time() + 30 * 86400
            with self.journal.db:
                self.journal.db.execute('INSERT OR IGNORE INTO cooldown VALUES(?,?)', (data['app'] + ':' + str(item_id), end))
        self.journal.set_setting('legacy_cooldowns_loaded', True)

    def error(self, message):
        self.errors = (self.errors + [{'at': time.time(), 'message': message}])[-20:]
        print(message, flush=True)

    def preflight(self):
        if not os.path.ismount('/storage'):
            raise Failure('storage pool is not mounted')
        stage = Path(self.config['stage_host'])
        stage.mkdir(parents=True, exist_ok=True)
        if stage.is_symlink() or os.stat(stage).st_dev != os.stat('/storage/Media').st_dev:
            raise Failure('staging is not on the library filesystem')
        for app in self.apps.values():
            settings = app.request('config/mediamanagement')
            if not settings.get('copyUsingHardlinks') or settings.get('recycleBin'):
                raise Failure('Arr hardlinks and zero recycle retention required')
        self.deluge.preflight()

    def concurrency(self):
        return self.journal.setting('concurrency', self.config['concurrency'])

    def find_releases(self, source):
        app = self.apps[source['app']]
        old_probe = qa.probe(source['path'])
        source = dict(source, resolution=qa.resolution(old_probe), runtime=float(old_probe['format']['duration']) / 60,
                      codec_remediation=qa.video(old_probe).get('codec_name') == 'av1',
                      requires_hdr=qa.hdr(old_probe))
        if source['app'] == 'radarr':
            releases = app.request('release', movieId=source['item_id'])
            season_sources = [source]
        else:
            season_sources = [x for x in self.records if x['app'] == source['app']
                              and x.get('series_id') == source['series_id'] and x.get('season') == source['season']
                              and x['resolution'] == source['resolution']
                              and (not source.get('codec_remediation') or x.get('codec_remediation'))]
            releases = app.request('release', episodeId=source['item_id'])
            # Sonarr episode searches commonly include full-season candidates.
            # A separate season search is only needed if none were returned.
            if len(season_sources) > 1 and not any(x.get('fullSeason') for x in releases):
                try:
                    releases += app.request('release', seriesId=source['series_id'], seasonNumber=source['season'])
                except Failure:
                    pass
        return source, rank_releases(releases, source, season_sources, self.config)

    def submit(self, source, candidates, torrents):
        key = source_key(source)
        alternatives = 0
        for release, targets in candidates:
            release_key = fingerprint(release)
            if self.journal.rejected(release_key):
                continue
            # Don't include recently replaced/busy episodes in a pack's import
            # targets. Its remaining payload can still seed as one download.
            busy = {source_key(t) for j in self.journal.jobs() if j['state'] in ACTIVE for t in j['targets']}
            targets = [x for x in targets if source_key(x) not in busy and not self.journal.cooling(source_key(x))]
            if not targets:
                continue
            try:
                raw, metadata = fetch_torrent(release)
                if metadata['hash'] in torrents:
                    self.journal.reject(release_key, 'torrent already exists outside this job')
                    continue
                if not source.get('codec_remediation') and metadata['size'] > sum(x['size'] for x in targets) * (1 - self.config['minimum_savings']):
                    self.journal.reject(release_key, 'actual payload has insufficient saving')
                    continue
                jid = uuid.uuid4().hex
                job = {'id': jid, 'state': 'submitting', 'hash': metadata['hash'], 'release_key': release_key,
                       'title': source['title'], 'app': source['app'], 'source_key': key,
                       'release_title': release['title'], 'created_at': time.time(),
                       'codec_remediation': bool(source.get('codec_remediation')),
                       'files': metadata['files'], 'size': metadata['size'], 'pack': bool(release.get('fullSeason')),
                       'targets': targets, 'tasks': [], 'attempt': self.journal.setting('retry:' + key, 0) + 1,
                       'source': source, 'logical_savings': 0, 'last_done': 0, 'progress_at': time.time()}
                Path(self.config['stage_host'], jid).mkdir(mode=0o755)
                self.journal.save(job)  # durable before any submission
                try:
                    result = self.deluge.add(job, raw)
                    if result and result.lower() != job['hash']:
                        raise Failure('Deluge returned an unexpected torrent hash')
                except Failure:
                    # A lost response is reconciled in the next cycle; do not
                    # repeat add or immediately discard the intent.
                    job['submit_uncertain'] = True
                    self.journal.save(job)
                return True
            except (Failure, OSError) as exc:
                self.journal.reject(release_key, str(exc) if isinstance(exc, Failure) else type(exc).__name__)
                alternatives += 1
                if alternatives >= 2:
                    break
        self.journal.cool(key, 1)
        return False

    def reconcile_submission(self, job, torrents):
        torrent = torrents.get(job['hash'])
        if torrent:
            self.deluge.configure(job, torrent)
            self.journal.save(job, 'downloading')
            return
        if time.time() - job['created_at'] > 180:
            # No resend of unknown outcomes. A fresh exact-hash/status query has
            # now established absence and this release is excluded on retry.
            self.fail(job, 'torrent submission absent after reconciliation', torrents)

    def fail(self, job, reason, torrents, review=False):
        job['error'] = reason
        self.journal.reject(job['release_key'], reason)
        torrent = torrents.get(job['hash'])
        if torrent:
            try:
                self.deluge.owned(job, torrent, allow_unlabelled=job['state'] == 'submitting')
            except Failure:
                torrent = None  # Concurrent normal grab won this hash; never remove it.
            if torrent:
                # Finish our own label assignment if an add reply was lost.
                if job['state'] == 'submitting':
                    self.deluge.configure(job, torrent)
                self.journal.save(job, 'cleaning_failed')
                self.deluge.remove(job, torrent)
        if job.get('attempt', 1) < 2:
            self.journal.set_setting('retry:' + job['source_key'], 1)
        else:
            self.journal.cool(job['source_key'], 1)
            self.journal.set_setting('retry:' + job['source_key'], 0)
        self.journal.save(job, 'needs_review' if review else 'failed')
        self.error(job['title'] + ': ' + reason)

    def resources(self, job):
        folder = str(Path(self.config['stage_host']) / job['id'])
        return self.apps[job['app']].request('manualimport', folder=folder, filterExistingFiles='false')

    def stage_tasks(self, job, torrent):
        known = {t['path'] for t in job['tasks']}
        progress = torrent.get('file_progress', [])
        complete = {f['path'] for f in job['files'] if (torrent.get('is_finished') or torrent.get('is_seed')
                    or (len(progress) > f['index'] and progress[f['index']] >= 1))
                    and Path(f['path']).suffix.lower() in VIDEO_EXTENSIONS}
        if not complete:
            return
        resources = self.resources(job)
        for resource in resources:
            path = resource.get('path', '')
            root = Path(self.config['stage_host']) / job['id']
            try:
                relative = str(Path(path).relative_to(root))
            except ValueError:
                raise Failure('manual import path escaped owned staging') from None
            if relative not in complete or path in known:
                continue
            if job['app'] == 'radarr':
                movie_id = (resource.get('movie') or {}).get('id')
                targets = [x for x in job['targets'] if x['item_id'] == movie_id]
            else:
                series_id = (resource.get('series') or {}).get('id')
                episodes = sorted(e['id'] for e in resource.get('episodes', []) if e.get('id'))
                targets = [x for x in job['targets'] if x['series_id'] == series_id and x['episode_ids'] == episodes]
            if len(targets) != 1:
                continue  # Unneeded pack episode, bonus, or ambiguous mapping.
            source = targets[0]
            task = {'path': path, 'source': source, 'state': 'pending',
                    'resource': {k: resource.get(k) for k in ['id', 'quality', 'languages', 'releaseGroup', 'indexerFlags', 'releaseType']}}
            # Protect an episode that is already better/smaller; skip independently.
            if not source.get('codec_remediation') and identity(path)['size'] > source['size'] * (1 - self.config['minimum_savings']):
                task['state'] = 'skipped'
            job['tasks'].append(task)
        self.journal.save(job)

    def consume_import(self, job, task):
        source = task['source']
        f = current_source(self.apps[job['app']], source)
        path = f.get('path')
        if not path or f.get('id') == source['file_id']:
            return False
        result = task['verification']
        same_file = lambda ident: all(ident[k] == result['new_identity'][k] for k in ('device', 'inode', 'size'))
        if not same_file(identity(path)) or not same_file(identity(task['path'])):
            raise Failure('import consumer is not the verified replacement hardlink')
        if qa.resolution(qa.probe(path)) != result['resolution']:
            raise Failure('import consumer resolution mismatch')
        task['subtitles'] = qa.install_subtitles(result, path)
        if Path(source['path']).exists() and identity(source['path']) == source['identity']:
            task['original_unlink_intent'] = True
            self.journal.save(job)
            Path(source['path']).unlink()
        task.update(state='imported', library_path=path, file_id=f['id'], imported_at=time.time())
        job['logical_savings'] = sum(t.get('verification', {}).get('logical_savings', 0)
                                    for t in job['tasks'] if t['state'] == 'imported')
        self.journal.cool(source_key(source), self.config['cooldown_days'])
        self.journal.set_setting('retry:' + source_key(source), 0)
        self.journal.save(job)
        return True

    def import_task(self, job, task):
        if task['state'] == 'importing':
            if self.consume_import(job, task):
                return
            command_id = task.get('command_id')
            if command_id:
                command = self.apps[job['app']].request('command/' + str(command_id))
                if str(command.get('status', '')).lower() in ('failed', 'aborted', 'completed'):
                    raise Failure('manual import ended without verified consumer replacement')
            elif time.time() - task['import_intent_at'] > 180:
                raise Review('manual import response lost; consumer did not change')
            return
        if not source_unchanged(self.apps[job['app']], task['source']):
            raise Review('source changed before import')
        if identity(task['path']) != task['verification']['new_identity']:
            raise Review('replacement changed before import')
        source = task['source']
        body = dict(task['resource'], path=task['path'], folderName='')
        if job['app'] == 'radarr':
            body['movieId'] = source['item_id']
        else:
            body.update(seriesId=source['series_id'], episodeIds=source['episode_ids'])
        # No Arr download history for this dedicated label: folder import with
        # copy mode exercises its normal hardlink/deletion path.
        task.update(state='importing', import_intent_at=time.time())
        self.journal.save(job)
        try:
            command = self.apps[job['app']].request('command', {'name': 'ManualImport', 'importMode': 'copy', 'files': [body]})
            task['command_id'] = command['id']
            self.journal.save(job)
        except Failure:
            # Reconcile the consumer/command before any second import.
            return

    def advance(self, job, torrents):
        if job['state'] == 'submitting':
            self.reconcile_submission(job, torrents)
            return
        torrent = torrents.get(job['hash'])
        if job['state'] in ('cleaning_failed', 'cleaning'):
            if torrent:
                self.deluge.remove(job, torrent)
            self.journal.save(job, 'failed' if job['state'] == 'cleaning_failed' else 'complete')
            return
        if job['state'] == 'seeding':
            if not torrent:
                self.journal.save(job, 'complete')
            elif torrent.get('ratio', 0) >= 2:
                for task in job['tasks']:
                    if task['state'] == 'imported':
                        actual = identity(task['library_path'])
                        if any(actual[k] != task['verification']['new_identity'][k] for k in ('device', 'inode', 'size')):
                            raise Failure('seed cleanup consumer changed')
                self.journal.save(job, 'cleaning')
                self.deluge.remove(job, torrent)
                self.journal.save(job, 'complete')
            return
        if job['state'] not in ACTIVE:
            return
        if not torrent:
            raise Failure('owned active torrent disappeared')
        self.deluge.owned(job, torrent)
        job['download'] = {k: torrent.get(k) for k in ['state', 'progress', 'num_seeds', 'num_peers',
                           'distributed_copies', 'download_payload_rate', 'ratio', 'total_done']}
        if stall_observation(job, torrent, time.time()):
            raise Failure('12-hour stall confirmed by three spaced observations')
        self.stage_tasks(job, torrent)
        for index, task in enumerate(job['tasks']):
            key = job['id'], index
            future = self.qa_futures.get(key)
            if task['state'] == 'verifying' and not future:
                task['state'] = 'pending'  # decoding is safe to repeat after restart
            if future and future.done():
                try:
                    task['verification'] = future.result()
                    task['state'] = 'verified'
                finally:
                    del self.qa_futures[key]
                self.journal.save(job)
            if task['state'] == 'pending' and len(self.qa_futures) < self.config['verification_concurrency']:
                task['state'] = 'verifying'
                self.journal.save(job)
                source = task['source']
                self.qa_futures[key] = self.qa_pool.submit(qa.verify, source['path'], task['path'],
                    str(Path(self.config['state_dir']) / 'qa' / job['id'] / str(index)), source['native'],
                    job['app'] == 'animearr', None if source.get('codec_remediation') else self.config['minimum_savings'])
            if task['state'] in ('verified', 'importing'):
                self.import_task(job, task)
        finished = torrent.get('is_finished') or torrent.get('is_seed')
        all_targets = {source_key(x) for x in job['targets']}
        mapped = {source_key(t['source']) for t in job['tasks']}
        if finished and not all_targets <= mapped:
            raise Review('completed torrent cannot map every selected episode/movie')
        if finished and job['tasks'] and all(t['state'] in ('imported', 'skipped') for t in job['tasks']):
            self.journal.save(job, 'seeding')
        else:
            self.journal.save(job)

    def tick(self):
        now = time.time()
        try:
            torrents = self.deluge.torrents()
        except Failure as exc:
            # An API outage is never counted as lack of torrent progress.
            for job in self.journal.jobs():
                if job['state'] in ACTIVE:
                    job.update(progress_at=now, stall_strikes=0)
                    self.journal.save(job)
            self.error(str(exc))
            self.status()
            return
        for job in self.journal.jobs():
            try:
                self.advance(job, torrents)
            except (Failure, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                    self.io_blocked_until = now + 300
                if job['state'] in ACTIVE:
                    self.fail(job, str(exc) if isinstance(exc, Failure) else type(exc).__name__, torrents, isinstance(exc, Review))
                else:
                    self.error(job['title'] + ': ' + (str(exc) if isinstance(exc, Failure) else type(exc).__name__))
        if self.inventory_future and self.inventory_future.done():
            try:
                self.records = self.merge_codec_repairs(self.inventory_future.result())
                self.inventory_at = now
                atomic_json(Path(self.config['state_dir']) / 'inventory.json', self.records)
            except (Failure, OSError) as exc:
                self.error('inventory: ' + (str(exc) if isinstance(exc, Failure) else type(exc).__name__))
                self.next_inventory = now + 600
            self.inventory_future = None
        if not self.inventory_future and now >= self.next_inventory and now - self.inventory_at > 6 * 3600:
            self.inventory_future = self.search_pool.submit(inventory, self.apps, self.config)
        active = [x for x in self.journal.jobs() if x['state'] in ACTIVE]
        if self.search_future and self.search_future.done():
            try:
                source, candidates = self.search_future.result()
                if len(active) < self.concurrency() and not self.journal.cooling(source_key(source)):
                    self.submit(source, candidates, torrents)
            except (Failure, OSError) as exc:
                self.error('search: ' + (str(exc) if isinstance(exc, Failure) else type(exc).__name__))
                self.journal.cool(source_key(self.search_source), 1 / 24)
            self.search_future = None
        active = [x for x in self.journal.jobs() if x['state'] in ACTIVE]
        busy = {source_key(t) for j in active for t in j['targets']}
        if (not self.search_future and not self.inventory_future and len(active) < self.concurrency()
                and not self.journal.setting('paused', False) and now >= self.io_blocked_until):
            source = next((x for x in self.records if source_key(x) not in busy and not self.journal.cooling(source_key(x))), None)
            if source:
                self.search_source = source
                self.search_future = self.search_pool.submit(self.find_releases, source)
        self.status()

    def status(self):
        jobs = self.journal.jobs()
        stage = Path(self.config['stage_host'])
        free = os.statvfs(stage)
        pending_repairs = 0
        for row in self.records:
            if row.get('codec_remediation'):
                try:
                    pending_repairs += identity(row['path']) == row['identity']
                except (Failure, OSError):
                    pass
        data = {'at': time.time(), 'concurrency': self.concurrency(), 'verification_concurrency': self.config['verification_concurrency'],
                'codec_repairs_pending': pending_repairs,
                'paused': self.journal.setting('paused', False), 'inventory_candidates': len(self.records),
                'inventory_at': self.inventory_at, 'searching': self.search_source['title'] if self.search_future else None,
                'verifying': len(self.qa_futures), 'active': sum(j['state'] in ACTIVE for j in jobs),
                'completed': sum(j['state'] in ('seeding', 'complete') for j in jobs),
                'logical_savings_bytes': sum(j.get('logical_savings', 0) for j in jobs),
                'measured_free_bytes': free.f_bavail * free.f_frsize,
                'errors': self.errors, 'jobs': [{k: j.get(k) for k in ['id', 'title', 'release_title', 'state', 'pack', 'codec_remediation', 'download',
                                                               'logical_savings', 'error']} for j in jobs]}
        atomic_json(Path(self.config['state_dir']) / 'status.json', data)
