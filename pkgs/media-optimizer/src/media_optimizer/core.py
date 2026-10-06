"""Runtime configuration, durable intents, isolated clients and torrent metadata."""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPCookieProcessor, urlopen
import xml.etree.ElementTree as ET


class Failure(Exception):
    """A safe, credential-free operational error."""


class Review(Failure):
    """Ambiguous media evidence; isolate this title."""


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_suffix('.pending')
    with open(tmp, 'w') as stream:
        os.chmod(tmp, 0o600)
        json.dump(data, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def load_config(path):
    data = json.loads(Path(path).read_text())
    for name in ('concurrency', 'verification_concurrency'):
        if not isinstance(data.get(name), int) or data[name] < 1:
            raise Failure('invalid ' + name)
    if data.get('original_retention_days', 0) != 0:
        raise Failure('original retention is prohibited')
    for name in ('daily_bytes', 'speed_limit', 'free_floor_bytes'):
        if data.get(name) is not None:
            raise Failure('campaign throttles are not configured')
    stage = Path(data['stage_host']).resolve()
    if stage.name != 'optimization' or stage.parent.name != 'Torrents':
        raise Failure('invalid dedicated staging root')
    if data['deluge_label'] != 'media-optimizer':
        raise Failure('invalid optimizer category')
    return data


class Journal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory / 'journal.sqlite', timeout=30)
        os.chmod(self.directory / 'journal.sqlite', 0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, state TEXT NOT NULL,
            updated REAL NOT NULL, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS cooldown(key TEXT PRIMARY KEY, until REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS rejected(key TEXT PRIMARY KEY, until REAL NOT NULL, reason TEXT);
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        ''')
        self.db.commit()

    @contextmanager
    def lock(self):
        with open(self.directory / 'runner.lock', 'a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Failure('optimizer already running') from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def save(self, job, state=None):
        if state:
            job['state'] = state
        # Release URLs/guid and authentication material never enter the journal.
        encoded = json.dumps(job, sort_keys=True)
        if re.search(r'"(?:downloadUrl|magnetUrl|guid|api_key|password)"', encoded):
            raise Failure('sensitive job field prohibited')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO jobs VALUES(?,?,?,?)',
                            (job['id'], job['state'], time.time(), encoded))

    def jobs(self):
        return [json.loads(x[0]) for x in self.db.execute('SELECT data FROM jobs ORDER BY updated')]

    def setting(self, key, default):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))

    def cooling(self, key, now=None):
        row = self.db.execute('SELECT until FROM cooldown WHERE key=?', (key,)).fetchone()
        return bool(row and row[0] > (time.time() if now is None else now))

    def cool(self, key, days):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cooldown VALUES(?,?)', (key, time.time() + days * 86400))

    def reject(self, key, reason):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO rejected VALUES(?,?,?)',
                            (key, time.time() + 30 * 86400, reason))

    def rejected(self, key):
        row = self.db.execute('SELECT until FROM rejected WHERE key=?', (key,)).fetchone()
        return bool(row and row[0] > time.time())


class Arr:
    def __init__(self, spec):
        self.url = spec['url'].rstrip('/')
        self.key = ET.parse(spec['config_xml']).findtext('ApiKey')
        if not self.key:
            raise Failure('missing media application key')

    def request(self, path, body=None, method=None, **query):
        method = method or ('GET' if body is None else 'POST')
        url = self.url + '/api/v3/' + path
        if query:
            url += '?' + urlencode(query)
        req = Request(url, data=json.dumps(body).encode() if body is not None else None,
                      method=method, headers={'X-Api-Key': self.key, 'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=240 if path == 'release' else 45) as response:
                payload = response.read()
            return json.loads(payload) if payload else None
        except Exception as exc:
            raise Failure(f'Arr {method} {path.split("/")[0]}: {type(exc).__name__}') from None


def fingerprint(release):
    return hashlib.sha256((str(release.get('indexerId')) + '\0' + release.get('title', '')
                           + '\0' + str(release.get('size'))).encode()).hexdigest()


def torrent_metadata(raw):
    """Hash the original info bytes; reject private, unsafe or non-media payloads."""
    if len(raw) > 16 * 1024**2:
        raise Failure('torrent metadata too large')
    pos = 0
    info_slice = None

    def parse(depth=0):
        nonlocal pos, info_slice
        if depth > 40 or pos >= len(raw):
            raise Failure('invalid torrent metadata')
        start = pos
        token = raw[pos:pos + 1]
        if token == b'i':
            end = raw.index(b'e', pos)
            pos = end + 1
            return int(raw[start + 1:end])
        if token in (b'd', b'l'):
            pos += 1
            result = {} if token == b'd' else []
            while raw[pos:pos + 1] != b'e':
                if token == b'd':
                    key = parse(depth + 1)
                    if not isinstance(key, bytes) or key in result:
                        raise Failure('invalid torrent dictionary')
                    before = pos
                    value = parse(depth + 1)
                    if depth == 0 and key == b'info':
                        info_slice = raw[before:pos]
                    result[key] = value
                else:
                    result.append(parse(depth + 1))
            pos += 1
            return result
        end = raw.index(b':', pos)
        length = int(raw[pos:end])
        pos = end + 1 + length
        if length < 0 or pos > len(raw):
            raise Failure('invalid torrent string')
        return raw[end + 1:pos]

    try:
        root = parse()
        info = root[b'info']
        if pos != len(raw) or not info_slice or info.get(b'private', 0) != 0:
            raise Failure('private or malformed torrent rejected')
        name = info.get(b'name.utf-8', info[b'name']).decode('utf-8')
        entries = info.get(b'files', [{b'path': [], b'length': info.get(b'length')}])
        files = []
        for index, item in enumerate(entries):
            components = [name] + [x.decode('utf-8') for x in item.get(b'path.utf-8', item[b'path'])]
            if any(not x or x in ('.', '..') or '/' in x or '\\' in x or '\0' in x for x in components):
                raise Failure('unsafe torrent path')
            path = PurePosixPath(*components)
            if path.is_absolute() or b'l' in item.get(b'attr', b'') or b'symlink path' in item:
                raise Failure('unsafe torrent path')
            if path.suffix.lower() in ('.exe', '.scr', '.bat', '.cmd', '.com', '.ps1', '.sh', '.zip', '.rar'):
                raise Failure('non-media torrent payload rejected')
            length = item[b'length']
            if not isinstance(length, int) or length < 0:
                raise Failure('invalid torrent file size')
            files.append({'index': index, 'path': str(path), 'size': length})
        if not any(Path(f['path']).suffix.lower() in ('.mkv', '.mp4', '.m4v', '.avi') for f in files):
            raise Failure('no media files in torrent')
        return {'hash': hashlib.sha1(info_slice).hexdigest(), 'files': files,
                'size': sum(f['size'] for f in files)}
    except Failure:
        raise
    except Exception:
        raise Failure('invalid torrent metadata') from None


def fetch_torrent(release):
    url = release.get('downloadUrl', '')
    class Redirect(HTTPRedirectHandler):
        magnet = ''

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if newurl.startswith('magnet:'):
                self.magnet = newurl
                raise Failure('magnet metadata needed')
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    redirect = Redirect()
    raw = None
    if urlsplit(url).scheme in ('http', 'https'):
        try:
            with build_opener(redirect).open(Request(url, headers={'User-Agent': 'media-optimizer/0.1'}), timeout=30) as response:
                raw = response.read(16 * 1024**2 + 1)
        except Exception:
            pass
    expected = str(release.get('infoHash') or '').lower()
    magnet = redirect.magnet or release.get('magnetUrl', '')
    if not expected:
        expected = next((x.split(':')[-1].lower() for x in parse_qs(urlsplit(magnet).query).get('xt', [])
                         if x.startswith('urn:btih:')), '')
    if len(expected) == 32:
        try:
            expected = base64.b32decode(expected.upper()).hex()
        except ValueError:
            expected = ''
    if raw is None or not raw.startswith(b'd'):
        if not re.fullmatch('[a-f0-9]{40}', expected):
            raise Failure('public torrent metadata unavailable')
        # Metadata only, by hash: never submit an unchecked magnet to a client.
        try:
            request = Request('https://itorrents.org/torrent/' + expected.upper() + '.torrent',
                              headers={'User-Agent': 'Mozilla/5.0'})
            with urlopen(request, timeout=30) as response:
                raw = response.read(16 * 1024**2 + 1)
        except Exception:
            raise Failure('public torrent metadata cache unavailable') from None
    metadata = torrent_metadata(raw)
    if expected and metadata['hash'] != expected:
        raise Failure('torrent metadata hash mismatch')
    return raw, metadata


class Deluge:
    KEYS = ['name', 'state', 'progress', 'total_done', 'total_size', 'num_seeds',
            'num_peers', 'distributed_copies', 'download_payload_rate', 'ratio',
            'is_finished', 'is_seed', 'save_path', 'label', 'files', 'file_progress',
            'max_download_speed', 'stop_at_ratio', 'stop_ratio', 'is_auto_managed']

    def __init__(self, config, apps):
        self.config = config
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.apps = apps
        self.login()

    def login(self):
        spec = self.config['deluge_credentials']
        client = self.apps[spec['app']].request('downloadclient/' + str(spec['client_id']))
        password = next(x['value'] for x in client['fields'] if x['name'] == 'password')
        if not self.rpc('auth.login', [password], reauth=False):
            raise Failure('Deluge authentication failed')

    def rpc(self, method, params=None, reauth=True):
        req = Request(self.config['deluge_url'],
                      data=json.dumps({'id': 1, 'method': method, 'params': params or []}).encode(),
                      headers={'Content-Type': 'application/json'})
        try:
            result = json.load(self.opener.open(req, timeout=45))
        except Exception as exc:
            raise Failure('Deluge ' + method + ': ' + type(exc).__name__) from None
        if result.get('error'):
            if reauth and result['error'].get('code') == 1:
                self.login()
                return self.rpc(method, params, reauth=False)
            raise Failure('Deluge RPC ' + method + ' failed')
        return result['result']

    def preflight(self):
        plugins = self.rpc('core.get_enabled_plugins')
        if 'Label' not in plugins:
            raise Failure('Deluge Label plugin required')
        label = self.config['deluge_label']
        if label not in self.rpc('label.get_labels'):
            self.rpc('label.add', [label])

    def torrents(self):
        return self.rpc('core.get_torrents_status', [{}, self.KEYS])

    def owned(self, job, torrent, allow_unlabelled=False):
        expected = self.config['stage_deluge'].rstrip('/') + '/' + job['id']
        if torrent.get('save_path', '').rstrip('/') != expected:
            raise Failure('torrent staging identity mismatch')
        label = torrent.get('label', '')
        if label != self.config['deluge_label'] and not (allow_unlabelled and not label):
            raise Failure('refusing unrelated torrent category')

    def configure(self, job, torrent):
        self.owned(job, torrent, allow_unlabelled=job['state'] == 'submitting')
        self.rpc('label.set_torrent', [job['hash'], self.config['deluge_label']])
        self.rpc('core.set_torrent_options', [[job['hash']], {
            'max_download_speed': -1, 'max_upload_speed': -1,
            'stop_at_ratio': True, 'stop_ratio': 2.0, 'remove_at_ratio': False}])
        self.rpc('core.resume_torrent', [[job['hash']]])

    def add(self, job, raw):
        return self.rpc('core.add_torrent_file', ['optimization.torrent', base64.b64encode(raw).decode(), {
            'download_location': self.config['stage_deluge'].rstrip('/') + '/' + job['id'],
            'add_paused': True, 'max_download_speed': -1, 'max_upload_speed': -1,
            'stop_at_ratio': True, 'stop_ratio': 2.0, 'remove_at_ratio': False}])

    def remove(self, job, torrent):
        self.owned(job, torrent)
        return self.rpc('core.remove_torrent', [job['hash'], True])


def identity(path):
    st = os.stat(path, follow_symlinks=False)
    if not Path(path).is_file() or Path(path).is_symlink():
        raise Failure('media path is not a regular file')
    return {'device': st.st_dev, 'inode': st.st_ino, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns}


def stall_observation(job, torrent, now):
    done = torrent.get('total_done', 0)
    if done > job.get('last_done', -1):
        job.update(last_done=done, progress_at=now, stall_strikes=0, stall_sample_at=now)
        return False
    if torrent.get('state') != 'Downloading' or torrent.get('is_finished') or torrent.get('is_seed'):
        job.update(progress_at=now, stall_strikes=0, stall_sample_at=now)
        return False
    if now - job.get('progress_at', now) < 12 * 3600:
        return False
    if now - job.get('stall_sample_at', 0) >= 1800:
        job['stall_strikes'] = job.get('stall_strikes', 0) + 1
        job['stall_sample_at'] = now
    return job.get('stall_strikes', 0) >= 3
