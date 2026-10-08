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

from .core import (Arr, Deluge, Failure, Journal, Retryable, Review, atomic_json, fetch_torrent,
                   fingerprint, identity, stall_observation)
from . import qa

ACTIVE = {'submitting', 'downloading', 'verifying', 'importing'}
VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.m4v', '.avi'}
NATIVE = {'Japanese': 'jpn', 'Korean': 'kor', 'Chinese': 'zho', 'English': 'eng'}


def auxiliary_video(path):
    """Recognize release asset labels without treating title words as markers."""
    path = Path(path)
    auxiliary = {'sample', 'samples', 'extras', 'bonus', 'trailer', 'trailers', 'featurette', 'featurettes'}
    assets = auxiliary | {'screen', 'screens', 'screenshot', 'screenshots'}
    for part in path.parts[:-1]:
        labels = set(re.split(r'\s*[,;+&]\s*', part.casefold().strip()))
        if labels <= assets and labels & auxiliary:
            return True
    return bool(re.search(r'(?i)(?:^|[ ._-])(?:sample|trailer)$|'
                          r'\(\s*(?:samples?|trailers?)\s*\)|\[\s*(?:samples?|trailers?)\s*\]', path.stem))


def runtime_minutes(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and re.fullmatch(r'(?:\d+:)?\d+:\d{2}(?:\.\d+)?', value):
        parts = value.split(':')
        minutes, seconds = parts[-2:]
        hours = int(parts[0]) if len(parts) == 3 else 0
        return hours * 60 + int(minutes) + float(seconds) / 60
    return 0


def source_key(source):
    ids = source.get('episode_ids') or [source['item_id']]
    return source['app'] + ':' + ','.join(str(x) for x in ids)


def episode_keys(source):
    return {source['app'] + ':' + str(x) for x in source.get('episode_ids') or [source['item_id']]}


def episode_range(title):
    matches = list(re.finditer(r'(?i)(?<![a-z0-9])S(\d{1,2})E(\d{1,3})\s*[-~]\s*(?:S\1)?E?(\d{1,3})(?!\d)', title))
    if len(matches) != 1:
        return None
    season, first, last = map(int, matches[0].groups())
    return (season, first, last) if 0 < first < last else None


def pack_seasons(title):
    match = re.search(r'(?i)(?<![a-z0-9])S\d{1,2}(?:\s*[-+]\s*S?\d{1,2})+(?!\d)', title)
    if not match:
        return set()
    parts = re.split(r'([-+])', re.sub(r'(?i)[s\s]', '', match[0]))
    seasons = {int(parts[0])}
    previous = int(parts[0])
    for operator, number in zip(parts[1::2], parts[2::2]):
        number = int(number)
        if operator == '-':
            if number <= previous:
                return set()
            seasons.update(range(previous, number + 1))
        else:
            seasons.add(number)
        previous = number
    return seasons


def reserved_targets(job):
    rejected = {source_key(t['source']) for t in job.get('tasks', []) if t['state'] == 'rejected'}
    return [t for t in job['targets'] if source_key(t) not in rejected]


def pack_quality_warnings(job):
    if not job.get('pack'):
        return []
    warnings = []
    seasons = {t['source'].get('season') for t in job.get('tasks', [])}
    for season in sorted(s for s in seasons if s is not None):
        checked = [t for t in job['tasks'] if t['source'].get('season') == season
                   and t['state'] in {'verified', 'importing', 'imported', 'rejected'}]
        rejected = [t for t in checked if t['state'] == 'rejected']
        if len(checked) >= 8 and len(rejected) >= 4 and len(rejected) / len(checked) >= .3:
            reasons = sorted({t['error'] for t in rejected})
            warnings.append({'season': season, 'checked': len(checked), 'rejected': len(rejected),
                             'reason': 'high season rejection rate; inspect shared metadata/timing', 'errors': reasons})
    return warnings


def pack_file_map(files, targets, require_all=True):
    """Prove season-local episode coverage from unambiguous feature filenames."""
    mapping = {}
    for file in files:
        path = file['path']
        if Path(path).suffix.lower() not in VIDEO_EXTENSIONS or auxiliary_video(path):
            continue
        stem = Path(path).stem
        explicit = list(re.finditer(r'(?i)(?<![a-z0-9])S(\d{1,2})E(\d{1,3})(?!\d)', stem))
        # Multi-episode feature files need the consumer's exact mapping instead.
        if len(explicit) == 1 and not re.search(r'(?i)E\d+\s*[-~]\s*(?:S\d+)?E?\d|E\d+E\d+', stem):
            season, number = map(int, explicit[0].groups())
        elif not explicit:
            seasons = {int(value) for groups in re.findall(
                r'(?i)(\d{1,2})(?:st|nd|rd|th)\s+Season\b|Season[ ._-]+(\d{1,2})\b', path)
                for value in groups if value}
            numbers = re.findall(r'\s-\s(\d{1,3})(?=\s|\[|$)', stem)
            if len(seasons) != 1 or len(numbers) != 1:
                continue
            season, number = next(iter(seasons)), int(numbers[0])
        else:
            continue
        matches = [t for t in targets if t['season'] == season and t.get('episode_numbers') == [number]]
        if len(matches) == 1:
            if path in mapping:
                raise Failure('episode pack contains a duplicate feature path')
            mapping[path] = source_key(matches[0])
    keys = list(mapping.values())
    if len(keys) != len(set(keys)) or (require_all and set(keys) != {source_key(t) for t in targets}):
        raise Failure('episode pack filenames do not uniquely cover every selected episode')
    return mapping


def select_pack(files, candidates):
    mapping = pack_file_map(files, candidates, require_all=False)
    targets = [t for t in candidates if source_key(t) in mapping.values()]
    priorities = []
    stems = {Path(path).stem for path in mapping}
    for index, file in enumerate(files):
        if file['index'] != index:
            raise Failure('torrent file indices are not contiguous')
        path = Path(file['path'])
        subtitle = path.suffix.lower() in {'.ass', '.ssa', '.srt', '.vtt', '.sub', '.idx'}
        asset = path.suffix.lower() in {'.ttf', '.otf'} or (subtitle and any(path.stem.startswith(stem) for stem in stems))
        priorities.append(1 if file['path'] in mapping or asset else 0)
    size = sum(f['size'] for f, priority in zip(files, priorities) if priority)
    return targets, mapping, priorities, size


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


def rank_releases(releases, source, season_sources, config, series_sources=None):
    eligible = []
    for release in releases:
        title = release.get('title', '')
        if qa.is_av1(title) or any(qa.is_av1(f.get('name', '')) for f in release.get('customFormats', [])):
            continue
        if source.get('requires_hdr') and not source.get('codec_remediation') and re.search(r'(?i)\bSDR\b', title):
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
            span = episode_range(title)
            seasons = pack_seasons(title)
            if seasons:
                if source['season'] not in seasons:
                    continue
                targets = [t for t in (series_sources or season_sources) if t['season'] in seasons]
                release = dict(release, episodePack=True)
            elif span:
                season, first, last = span
                if season != source['season'] or (release.get('seasonNumber') not in (None, 0, season)):
                    continue
                targets = [t for t in season_sources if t.get('episode_numbers')
                           and all(first <= n <= last for n in t['episode_numbers'])]
                if source_key(source) not in {source_key(t) for t in targets}:
                    continue
                release = dict(release, episodePack=True)
            elif release.get('fullSeason'):
                # Parsed series/season, and all old files remain independently
                # subject to the saving/quality gate when the pack is imported.
                if release.get('seasonNumber') != source['season']:
                    continue
                targets = season_sources
                expected = {x for t in targets for x in t['episode_ids']}
                if mapped and not expected <= mapped:
                    continue
            else:
                explicit_seasons = {int(n) for n in re.findall(r'(?i)(?<![a-z0-9])S(\d{1,2})(?:E\d|\b)', title)}
                if explicit_seasons and source['season'] not in explicit_seasons:
                    continue
                if not set(source['episode_ids']) <= mapped:
                    continue
                targets = [t for t in season_sources if set(t['episode_ids']) <= mapped]
                if len(mapped) > len(source['episode_ids']):
                    release = dict(release, episodePack=True)
                if re.search(r'(?i)\b(?:batch|complete)\b', title):
                    release = dict(release, episodePack=True)
        size = release.get('size', 0)
        if not size or (not source.get('codec_remediation')
                        and not (release.get('episodePack') or release.get('fullSeason'))
                        and size > sum(t['size'] for t in targets) * (1 - config['minimum_savings'])):
            continue
        score = int(release.get('customFormatScore') or 0)
        if score < 0:
            continue
        advertised_hdr = not source.get('requires_hdr') or bool(re.search(
            r'(?i)\b(?:HDR(?:10\+?)?|HLG|DoVi|DV)\b', title))
        # A small CF delta (pack/HDR sub-bonus) does not override live seed
        # evidence. Codec and language tiers remain strong preferences.
        tier = score // 500
        seeds = int(release.get('seeders') or 0)
        availability = 0 if seeds < 4 else 1 if seeds < 16 else 2
        # Exhaust advertised HDR candidates before considering an SDR fallback
        # for an actual AV1 playback repair. Ordinary optimization preserves HDR.
        eligible.append((advertised_hdr, tier, availability, bool(release.get('fullSeason') or release.get('episodePack')), seeds, score, -size, release, targets))
    # Prefer advertised 7.1 only among otherwise equal candidates, and only
    # within 20% of the smallest comparable release. Stream QA is authoritative;
    # this title hint cannot outweigh codec/language, availability, or pack rank.
    smallest = {}
    for entry in eligible:
        group = entry[:6]
        smallest[group] = min(smallest.get(group, -entry[6]), -entry[6])
    def preference(entry):
        seven_one = bool(re.search(r'(?<![0-9])7[ ._-]1(?![A-Za-z0-9])', entry[-2]['title']))
        affordable = -entry[6] <= smallest[entry[:6]] * 1.2
        return entry[:6] + (seven_one and affordable, entry[6])
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
        self.search_replacement = None
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
            series_sources = [x for x in self.records if x['app'] == source['app']
                              and x.get('series_id') == source['series_id']
                              and x['resolution'] == source['resolution']]
            season_sources = [x for x in series_sources if x.get('season') == source['season']]
            releases = app.request('release', episodeId=source['item_id'])
            # Sonarr episode searches commonly include full-season candidates.
            # A separate season search is only needed if none were returned.
            if len(season_sources) > 1 and not any(x.get('fullSeason') or episode_range(x.get('title', '')) for x in releases):
                try:
                    releases += app.request('release', seriesId=source['series_id'], seasonNumber=source['season'])
                except Failure:
                    pass
        if source.get('recheck_hash'):
            releases = [r for r in releases if str(r.get('infoHash', '')).lower() == source['recheck_hash']
                        or r.get('title', '').casefold() == source['recheck_title'].casefold()]
        return source, rank_releases(releases, source, season_sources, self.config,
                                     series_sources if source['app'] != 'radarr' else None)

    def submit(self, source, candidates, torrents, replacement=None):
        key = source_key(source)
        alternatives = 0
        for release, targets in candidates:
            release_key = fingerprint(release)
            if self.journal.rejected(release_key):
                continue
            # Don't include recently replaced/busy episodes in a pack's import
            # targets. Its remaining payload can still seed as one download.
            busy = {k for j in self.journal.jobs() if j['state'] in ACTIVE
                    and (not replacement or j['id'] != replacement['id']) for t in reserved_targets(j) for k in episode_keys(t)}
            targets = [x for x in targets if not episode_keys(x) & busy and not self.journal.cooling(source_key(x))]
            if not targets:
                continue
            try:
                raw, metadata = fetch_torrent(release)
                if source.get('recheck_hash') and metadata['hash'] != source['recheck_hash']:
                    continue
                if self.journal.rejected('torrent:' + metadata['hash']):
                    continue
                if self.journal.rejected('torrent:' + metadata['hash'] + ':' + key):
                    continue
                if metadata['hash'] in torrents:
                    if not replacement or metadata['hash'] != replacement['hash']:
                        self.journal.reject(release_key, 'torrent already exists outside this job')
                    continue
                file_map, priorities, selected_size = {}, None, metadata['size']
                feature_count = sum(Path(f['path']).suffix.lower() in VIDEO_EXTENSIONS and not auxiliary_video(f['path'])
                                    for f in metadata['files'])
                if source['app'] != 'radarr' and (feature_count > 1 or release.get('episodePack') or release.get('fullSeason')):
                    available = {source_key(t): t for t in self.records if t['app'] == source['app']
                                 and t.get('series_id') == source['series_id'] and t['resolution'] == source['resolution']}
                    available.update({source_key(t): t for t in targets})
                    eligible = []
                    for t in available.values():
                        if episode_keys(t) & busy or self.journal.cooling(source_key(t)):
                            continue
                        if self.journal.rejected('torrent:' + metadata['hash'] + ':' + source_key(t)):
                            continue
                        try:
                            if identity(t['path']) == t['identity']:
                                eligible.append(t)
                        except (Failure, OSError):
                            continue
                    selected, proven, wanted, size = select_pack(metadata['files'], eligible)
                    file_sizes = {proven[f['path']]: f['size'] for f in metadata['files'] if f['path'] in proven}
                    selected = [t for t in selected if t.get('codec_remediation')
                                or file_sizes[source_key(t)] <= t['size'] * (1 - self.config['minimum_savings'])]
                    selected, proven, wanted, size = select_pack(metadata['files'], selected)
                    if source_key(source) not in {source_key(t) for t in selected}:
                        # Ambiguous absolute-numbered filenames can fall back to
                        # the consumer's exact episode mapping. A contradictory
                        # explicit season cannot be overridden by that fallback.
                        mapped = {e['id'] for e in release.get('mappedEpisodeInfo', []) if e.get('id')}
                        scoped = any(re.search(r'(?i)(?<![a-z0-9])S\d{1,2}E\d|\d(?:st|nd|rd|th)\s+Season|Season[ ._-]+\d', f['path'])
                                     for f in metadata['files'] if Path(f['path']).suffix.lower() in VIDEO_EXTENSIONS)
                        exact = {e for t in targets for e in t['episode_ids']} <= mapped
                        if release.get('episodePack') and (scoped or not exact):
                            raise Failure('episode pack cannot prove coverage of requested source')
                    else:
                        targets, file_map, priorities, selected_size = selected, proven, wanted, size
                if not source.get('codec_remediation') and selected_size > sum(x['size'] for x in targets) * (1 - self.config['minimum_savings']):
                    self.journal.reject(release_key, 'actual payload has insufficient saving')
                    continue
                sdr_fallback = bool(source.get('codec_remediation') and source.get('requires_hdr')
                                    and not re.search(r'(?i)\b(?:HDR(?:10\+?)?|HLG|DoVi|DV)\b', release['title']))
                if sdr_fallback:
                    targets = [dict(t, allow_sdr_remediation=True) if source_key(t) == key else t for t in targets]
                jid = uuid.uuid4().hex
                job = {'id': jid, 'state': 'submitting', 'hash': metadata['hash'], 'release_key': release_key,
                       'title': source['title'], 'app': source['app'], 'source_key': key,
                       'release_title': release['title'], 'created_at': time.time(),
                       'codec_remediation': bool(source.get('codec_remediation')),
                       'files': metadata['files'], 'size': metadata['size'], 'pack': bool(release.get('fullSeason') or release.get('episodePack')),
                       'episode_file_map': file_map,
                       'file_priorities': priorities, 'selected_size': selected_size,
                       'targets': targets, 'tasks': [], 'attempt': self.journal.setting('retry:' + key, 0) + 1,
                       'source': source, 'logical_savings': 0, 'last_done': 0, 'progress_at': time.time()}
                job['pack'] = job['pack'] or feature_count > 1
                Path(self.config['stage_host'], jid).mkdir(mode=0o755)
                if replacement:
                    self.yield_stalled(replacement, torrents)
                    replacement = None
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
                if source.get('recheck_hash'):
                    self.journal.set_setting('manual_rechecks', [x for x in self.journal.setting('manual_rechecks', [])
                                             if source_key(x) != key or x.get('recheck_hash') != source['recheck_hash']])
                return True
            except (Failure, OSError) as exc:
                self.journal.reject(release_key, str(exc) if isinstance(exc, Failure) else type(exc).__name__)
                alternatives += 1
                if alternatives >= 2:
                    break
        self.journal.cool(key, 1)
        return False

    def yield_stalled(self, job, torrents):
        """Only discard an owned stalled payload once other work is validated."""
        if job['state'] != 'downloading' or job.get('tasks'):
            raise Failure('stalled job is no longer safe to replace')
        torrent = torrents.get(job['hash'])
        if not torrent or not stall_observation(job, torrent, time.time(), self.config.get('stall_grace_seconds', 1800)):
            raise Failure('stalled job resumed progress')
        self.deluge.owned(job, torrent)
        reason = 'stalled download yielded its slot to an available candidate'
        job['error'] = reason
        self.journal.save(job, 'cleaning_failed')
        self.deluge.remove(job, torrent)
        # Unavailable today does not mean permanently unsuitable. Do not spend
        # the QA retry allowance on a temporary availability problem.
        for key in (job['release_key'], 'torrent:' + job['hash']):
            self.journal.reject(key, reason, days=6 / 24)
        self.journal.cool(job['source_key'], 1 / 24)
        self.journal.set_setting('retry:' + job['source_key'], 0)
        self.journal.save(job, 'stalled')
        self.error(job['title'] + ': ' + reason)

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
        if review or 'AV1 prohibited' in reason:
            self.journal.reject('torrent:' + job['hash'], reason)
        torrent = torrents.get(job['hash'])
        if torrent:
            try:
                self.deluge.owned(job, torrent, allow_unlabelled=job['state'] == 'submitting')
            except Failure:
                torrent = None  # Concurrent normal grab won this hash; never remove it.
            if torrent:
                if review and job.get('source', {}).get('recheck_hash'):
                    # An explicitly requested diagnosis keeps the incoming file
                    # paused for inspection. This never retains the original.
                    job['review_payload_retained'] = True
                    self.journal.save(job, 'needs_review')
                    self.deluge.hold_review(job, torrent)
                    self.error(job['title'] + ': ' + reason + '; requested recheck held for inspection')
                    return
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

    def reject_task(self, job, task, reason):
        key = source_key(task['source'])
        task.update(state='rejected', error=reason, rejected_at=time.time())
        # A bad episode does not make every other file in this hash unsuitable.
        self.journal.reject('torrent:' + job['hash'] + ':' + key, reason)
        if self.journal.setting('retry:' + key, 0) < 1:
            self.journal.set_setting('retry:' + key, 1)
        else:
            self.journal.cool(key, 1)
            self.journal.set_setting('retry:' + key, 0)
        job['episode_rejections'] = [{'source_key': source_key(t['source']), 'error': t['error']}
                                     for t in job['tasks'] if t['state'] == 'rejected']
        self.journal.save(job)
        self.error(job['title'] + ' ' + key + ': ' + reason + '; continuing other pack files')

    def stage_tasks(self, job, torrent):
        known = {t['path'] for t in job['tasks'] if t.get('path')}
        progress = torrent.get('file_progress', [])
        priorities = job.get('file_priorities')
        complete = {f['path'] for f in job['files'] if (not priorities or priorities[f['index']] > 0)
                    and (torrent.get('is_finished') or torrent.get('is_seed')
                    or (len(progress) > f['index'] and progress[f['index']] >= 1))
                    and Path(f['path']).suffix.lower() in VIDEO_EXTENSIONS and not auxiliary_video(f['path'])}
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
                features = [f for f in job['files'] if Path(f['path']).suffix.lower() in VIDEO_EXTENSIONS
                            and not auxiliary_video(f['path'])]
                if movie_id is None and len(job['targets']) == 1 and len(features) == 1:
                    # The release already mapped to this movie. Reprocess just
                    # its sole feature with that ID; GET movieId instead scans
                    # the existing library and must never be used here.
                    if not Path(path).resolve().is_relative_to(root.resolve()):
                        raise Failure('manual import path escaped owned staging')
                    identity(path)
                    target = job['targets'][0]
                    if target['item_id'] != job['source']['item_id']:
                        raise Failure('movie reprocess target contradicts release mapping')
                    body = {k: resource.get(k) for k in ('quality', 'languages', 'releaseGroup', 'indexerFlags')}
                    body.update(path=path, movieId=target['item_id'])
                    processed = self.apps[job['app']].request('manualimport', [body])
                    if len(processed) != 1 or processed[0].get('path') != path:
                        raise Failure('movie reprocess returned an unexpected file')
                    resource = processed[0]
                    movie_id = (resource.get('movie') or {}).get('id')
                targets = [x for x in job['targets'] if x['item_id'] == movie_id]
            else:
                series_id = (resource.get('series') or {}).get('id')
                episodes = sorted(e['id'] for e in resource.get('episodes', []) if e.get('id'))
                targets = [x for x in job['targets'] if x['series_id'] == series_id and x['episode_ids'] == episodes]
                proven = job.get('episode_file_map', {}).get(relative)
                if proven:
                    # The release matched this series and its torrent filenames
                    # proved season-local numbering. Sonarr can mistake that for
                    # absolute numbering; picture/audio QA still checks content.
                    targets = [x for x in job['targets'] if x['series_id'] == series_id and source_key(x) == proven]
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
        job['audio_tradeoffs'] = [c for t in job['tasks'] for c in t.get('verification', {}).get('audio_tradeoffs', [])]
        job['subtitle_missing'] = sorted({lang for t in job['tasks'] for lang in t.get('verification', {}).get('subtitle_missing', [])})
        job['subtitle_warnings'] = [c for t in job['tasks'] for c in t.get('verification', {}).get('subtitle_warnings', [])]
        job['content_notes'] = [c for t in job['tasks'] for c in t.get('verification', {}).get('content_notes', [])]
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

    def reconcile_rejections(self, job):
        """Retire a historical rejection once Arr confirms another replacement."""
        changed = False
        for task in job.get('tasks', []):
            if task['state'] != 'rejected':
                continue
            source = task['source']
            try:
                if identity(source['path']) == source['identity']:
                    continue
            except (Failure, OSError):
                pass
            try:
                consumer = current_source(self.apps[job['app']], source)
                if consumer.get('id') == source['file_id'] or not consumer.get('path'):
                    continue
                identity(consumer['path'])  # Missing files are not successful replacements.
            except (Failure, OSError):
                continue
            task.update(state='skipped', skip_reason='source already replaced by another release',
                        previous_rejection=task.pop('error'), rejection_resolved_at=time.time())
            changed = True
        if changed:
            job['episode_rejections'] = [{'source_key': source_key(t['source']), 'error': t['error']}
                                         for t in job['tasks'] if t['state'] == 'rejected']
            self.journal.save(job)

    def reconcile_terminal_review(self, job):
        if job['state'] not in ('needs_review', 'failed') or job.get('review_payload_retained'):
            return
        now = time.time()
        if job.get('review_checked_at', 0) + 300 > now:
            return
        job['review_checked_at'] = now
        self.reconcile_rejections(job)
        targets = job.get('targets') or [job.get('source')]
        for source in targets:
            if not source:
                return
            try:
                consumer = current_source(self.apps[job['app']], source)
                if consumer.get('id') == source['file_id'] or not consumer.get('path'):
                    break
                current = identity(consumer['path'])
                if all(current[k] == source['identity'][k] for k in ('device', 'inode', 'size')):
                    break
            except (Failure, OSError):
                break
        else:
            job.update(state='superseded', previous_error=job.pop('error', None),
                       rejection_resolved_at=now)
        self.journal.save(job)

    def advance(self, job, torrents):
        if job['state'] == 'seeding' or job['state'] in ACTIVE:
            self.reconcile_rejections(job)
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
        if job.get('file_priorities') and job.get('selected_size'):
            progress = torrent.get('file_progress', [])
            done = sum(f['size'] * progress[f['index']] for f in job['files']
                       if job['file_priorities'][f['index']] and len(progress) > f['index'])
            job['download']['selected_progress'] = 100 * done / job['selected_size']
        job['stalled'] = stall_observation(job, torrent, time.time(), self.config.get('stall_grace_seconds', 1800))
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
                except (Failure, OSError) as exc:
                    if not job.get('pack') or (isinstance(exc, OSError) and exc.errno == errno.ENOSPC):
                        raise
                    self.reject_task(job, task, str(exc) if isinstance(exc, Failure) else type(exc).__name__)
                finally:
                    del self.qa_futures[key]
                self.journal.save(job)
            if task['state'] == 'pending' and len(self.qa_futures) < self.config['verification_concurrency']:
                task['state'] = 'verifying'
                self.journal.save(job)
                source = task['source']
                self.qa_futures[key] = self.qa_pool.submit(qa.verify, source['path'], task['path'],
                    str(Path(self.config['state_dir']) / 'qa' / job['id'] / str(index)), source['native'],
                    job['app'] == 'animearr', None if source.get('codec_remediation') else self.config['minimum_savings'],
                    allow_sdr_remediation=source.get('allow_sdr_remediation', False))
            if task['state'] in ('verified', 'importing'):
                try:
                    self.import_task(job, task)
                except Review as exc:
                    if not job.get('pack') or task['state'] == 'importing':
                        raise
                    self.reject_task(job, task, str(exc))
        priorities = job.get('file_priorities')
        progress = torrent.get('file_progress', [])
        finished = (torrent.get('is_finished') or torrent.get('is_seed') or
                    (priorities and all(len(progress) > i and progress[i] >= 1 for i, p in enumerate(priorities) if p)))
        all_targets = {source_key(x) for x in job['targets']}
        mapped = {source_key(t['source']) for t in job['tasks']}
        if finished and not all_targets <= mapped:
            if not job.get('pack'):
                raise Review('completed torrent cannot map every selected episode/movie')
            for target in job['targets']:
                if source_key(target) not in mapped:
                    task = {'source': target, 'state': 'pending'}
                    job['tasks'].append(task)
                    self.reject_task(job, task, 'completed pack cannot map selected episode file')
        if finished and job['tasks'] and all(t['state'] in ('imported', 'skipped', 'rejected') for t in job['tasks']):
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
                    job.update(progress_at=now, stall_strikes=0, stalled=False)
                    self.journal.save(job)
            self.error(str(exc))
            self.status()
            return
        for job in self.journal.jobs():
            if job.get('api_retry_at', 0) > now:
                continue
            try:
                self.reconcile_terminal_review(job)
                self.advance(job, torrents)
                if job.get('api_error'):
                    for field in ('api_error', 'api_retry_at', 'api_retries'):
                        job.pop(field, None)
                    self.journal.save(job)
            except Retryable as exc:
                retries = job.get('api_retries', 0) + 1
                job.update(api_error=str(exc), api_retries=retries,
                           api_retry_at=now + min(300, 15 * 2**min(retries - 1, 5)))
                self.journal.save(job)
                if retries == 1:
                    self.error(job['title'] + ': ' + str(exc) + '; retaining payload for API retry')
            except (Failure, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                    self.io_blocked_until = now + 300
                    self.error(job['title'] + ': storage write failed; keeping payload for retry')
                    continue
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
                replacement = next((j for j in active if len(active) == self.concurrency() and j['id'] == self.search_replacement
                                    and j.get('stalled') and j['state'] == 'downloading' and not j.get('tasks')), None)
                if ((len(active) < self.concurrency() or replacement) and not self.journal.setting('paused', False)
                        and not self.journal.cooling(source_key(source))):
                    self.submit(source, candidates, torrents, replacement if len(active) >= self.concurrency() else None)
            except (Failure, OSError) as exc:
                self.error('search: ' + (str(exc) if isinstance(exc, Failure) else type(exc).__name__))
                self.journal.cool(source_key(self.search_source), 1 / 24)
            self.search_future = None
            self.search_replacement = None
        active = [x for x in self.journal.jobs() if x['state'] in ACTIVE]
        busy = {k for j in active for t in reserved_targets(j) for k in episode_keys(t)}
        stalled = next((j for j in active if j.get('stalled') and j['state'] == 'downloading'
                        and not j.get('tasks')), None)
        if (not self.search_future and not self.inventory_future
                and (len(active) < self.concurrency() or (len(active) == self.concurrency() and stalled))
                and not self.journal.setting('paused', False) and now >= self.io_blocked_until):
            requested = self.journal.setting('manual_rechecks', [])
            valid = []
            for source in requested:
                try:
                    if identity(source['path']) == source['identity']:
                        valid.append(source)
                    else:
                        self.error(source['title'] + ': source changed before requested recheck; request retired')
                except FileNotFoundError:
                    self.error(source['title'] + ': source missing before requested recheck; request retired')
            if valid != requested:
                self.journal.set_setting('manual_rechecks', valid)
            source = next((x for x in valid + self.records if not episode_keys(x) & busy
                           and not self.journal.cooling(source_key(x))), None)
            if not source and stalled and not self.journal.cooling(stalled['source_key']):
                source = stalled['source']  # Try another release of the rare title.
            if source:
                self.search_source = source
                self.search_replacement = stalled['id'] if len(active) >= self.concurrency() else None
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
                'requested_rechecks': len(self.journal.setting('manual_rechecks', [])),
                'imported_files': sum(t['state'] == 'imported' for j in jobs for t in j.get('tasks', [])),
                'rejected_files': sum(t['state'] == 'rejected' for j in jobs for t in j.get('tasks', [])),
                'unresolved_reviews': sum(j['state'] in ('needs_review', 'failed') for j in jobs),
                'resolved_reviews': sum(j.get('rejection_resolved_at') is not None for j in jobs),
                'codec_repairs_pending': pending_repairs,
                'paused': self.journal.setting('paused', False), 'inventory_candidates': len(self.records),
                'inventory_at': self.inventory_at, 'searching': self.search_source['title'] if self.search_future else None,
                'verifying': len(self.qa_futures), 'active': sum(j['state'] in ACTIVE for j in jobs),
                'completed': sum(j['state'] in ('seeding', 'complete') for j in jobs),
                'logical_savings_bytes': sum(j.get('logical_savings', 0) for j in jobs),
                'measured_free_bytes': free.f_bavail * free.f_frsize,
                'errors': self.errors, 'jobs': [dict({k: j.get(k) for k in ['id', 'title', 'release_title', 'state', 'pack', 'codec_remediation', 'download',
                                                               'logical_savings', 'stalled', 'audio_tradeoffs', 'subtitle_missing',
                                                               'subtitle_warnings', 'content_notes', 'episode_rejections', 'selected_size', 'error',
                                                               'previous_error', 'rejection_resolved_at', 'api_error', 'api_retry_at']},
                                                  pack_quality_warnings=pack_quality_warnings(j)) for j in jobs]}
        atomic_json(Path(self.config['state_dir']) / 'status.json', data)
