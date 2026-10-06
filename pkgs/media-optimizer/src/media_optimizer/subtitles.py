"""Local read-only Arr bridge and repeatable Bazarr language setup.

Existing application keys remain in memory; Bazarr uses a non-secret loopback
placeholder rather than copying those keys into its configuration or logs.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import time
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import Request, urlopen

from .core import Arr, Failure, atomic_json

LOCAL_TOKEN = 'local-media-subtitles'
LANGUAGES = [('English', 'en'), ('Japanese', 'ja'), ('Korean', 'ko'), ('Chinese', 'zh')]
READ_ENDPOINTS = {'system', 'series', 'episode', 'episodefile', 'movie', 'moviefile',
                  'tag', 'qualityprofile', 'languageprofile', 'language', 'rootfolder', 'profile'}


def bridge_target(path, apps):
    parsed = urlsplit(path)
    pieces = parsed.path.split('/')
    if len(pieces) < 5 or pieces[1] not in apps or pieces[2:4] != ['api', 'v3']:
        raise Failure('unsupported subtitle bridge route')
    if pieces[4] not in READ_ENDPOINTS or any(x in ('.', '..') or '%' in x for x in pieces):
        raise Failure('unsupported subtitle bridge endpoint')
    if pieces[4] == 'system' and pieces[4:] != ['system', 'status']:
        raise Failure('unsupported subtitle bridge system endpoint')
    app = apps[pieces[1]]
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() != 'apikey']
    suffix = '/'.join(pieces[4:])
    return app, app.url + '/api/v3/' + suffix + ('?' + urlencode(query) if query else '')


def bridge(config):
    apps = {name: Arr(spec) for name, spec in config['apps'].items()}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log URLs, headers, or keys.

        def do_GET(self):
            try:
                app, target = bridge_target(self.path, apps)
                request = Request(target, headers={'X-Api-Key': app.key})
                with urlopen(request, timeout=90) as response:
                    data = response.read()
                    content_type = response.headers.get('Content-Type', 'application/json')
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except HTTPError as exc:
                self.send_error(exc.code, 'Upstream application response')
            except Failure:
                self.send_error(404, 'Unsupported route')
            except Exception:
                self.send_error(502, 'Subtitle bridge unavailable')

    server = ThreadingHTTPServer(('127.0.0.1', config['subtitles']['bridge_port']), Handler)
    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def seed(config, instance):
    spec = config['subtitles']['instances'][instance]
    state = Path(spec['state_dir'])
    target = state / 'config' / 'config.yaml'
    if target.exists():
        return
    general = {'ip': '127.0.0.1', 'port': spec['port'], 'auto_update': False,
               'use_sonarr': True, 'use_radarr': instance == 'main',
               'instance_name': 'Media subtitles ' + instance, 'use_embedded_subs': True,
               'parse_embedded_audio_track': True, 'multithreading': False, 'concurrent_jobs': 1,
               'minimum_score': 90, 'minimum_score_movie': 85, 'adaptive_searching': True,
               'wanted_search_frequency': 6, 'wanted_search_frequency_movie': 6,
               'upgrade_manual': False, 'upgrade_subs': True, 'single_language': False,
               'enabled_providers': ['podnapisi', 'tvsubtitles', 'animetosho'],
               'serie_default_enabled': True, 'serie_default_profile': 1,
               'movie_default_enabled': instance == 'main', 'movie_default_profile': 1}
    def connection(app):
        return {'ip': '127.0.0.1', 'port': config['subtitles']['bridge_port'],
                'base_url': '/' + app, 'apikey': LOCAL_TOKEN, 'ssl': False,
                'series_sync': 15, 'movies_sync': 15, 'full_update': 'Daily', 'only_monitored': False}
    data = {'general': general, 'sonarr': connection('sonarr' if instance == 'main' else 'animearr'),
            'radarr': connection('radarr'), 'auth': {'type': None, 'apikey': LOCAL_TOKEN},
            'subsync': {'use_subsync': True, 'use_subsync_threshold': False, 'use_subsync_movie_threshold': False}}
    # JSON is valid YAML. This seed contains no existing application credentials.
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(target, data)


def bazarr_request(spec, endpoint, body=None):
    url = f"http://127.0.0.1:{spec['port']}/api/{endpoint}"
    data = urlencode(body, doseq=True).encode() if body is not None else None
    req = Request(url, data=data, headers={'X-API-KEY': LOCAL_TOKEN,
                                         'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urlopen(req, timeout=240) as response:
            payload = response.read()
        return json.loads(payload) if payload else None
    except Exception as exc:
        raise Failure('Bazarr ' + endpoint.split('/')[0] + ': ' + type(exc).__name__) from None


def profiles():
    result = []
    for index, (name, code) in enumerate(LANGUAGES, 1):
        codes = ['en'] if code == 'en' else ['en', code]
        result.append({'profileId': index, 'name': 'MLP English' + ('' if code == 'en' else ' + ' + name),
                       'cutoff': None, 'items': [{'id': i, 'language': lang, 'hi': 'False', 'forced': 'False',
                                                'audio_exclude': 'False'} for i, lang in enumerate(codes, 1)],
                       'mustContain': [], 'mustNotContain': [], 'originalFormat': True})
    return result


def setup(config, instance):
    spec = config['subtitles']['instances'][instance]
    desired = profiles()
    current = bazarr_request(spec, 'system/languages/profiles')
    other = [p for p in current if p['profileId'] not in range(1, 5)]
    if any(p['profileId'] in range(1, 5) and not p['name'].startswith('MLP ') for p in current):
        raise Failure('Bazarr profile IDs already owned by another configuration')
    if current != desired + other:
        bazarr_request(spec, 'system/settings', {'languages-enabled': [code for _, code in LANGUAGES],
                                               'languages-profiles': json.dumps(desired + other)})
    arrs = ['radarr', 'sonarr'] if instance == 'main' else ['animearr']
    for app in arrs:
        is_movie = app == 'radarr'
        endpoint = 'movies' if is_movie else 'series'
        id_name = 'radarrId' if is_movie else 'sonarrSeriesId'
        form_id = 'radarrid' if is_movie else 'seriesid'
        records = bazarr_request(spec, endpoint)['data']
        indexed = {r[id_name]: r for r in records}
        # Fetch through the bridge, never persist upstream keys in Bazarr.
        upstream = f"http://127.0.0.1:{config['subtitles']['bridge_port']}/{app}/api/v3/" + ('movie' if is_movie else 'series')
        with urlopen(upstream, timeout=90) as response:
            titles = json.load(response)
        changes = []
        ids = {name: i for i, (name, _) in enumerate(LANGUAGES, 1)}
        for title in titles:
            if title['id'] not in indexed:
                continue
            profile_id = ids.get(title.get('originalLanguage', {}).get('name'), 1)
            old_id = indexed[title['id']].get('profileId')
            if old_id is None or old_id in range(1, 5):
                if old_id != profile_id:
                    changes.append((title['id'], profile_id))
        # Bazarr scans/searches after assignment; bound each HTTP mutation batch.
        for at in range(0, len(changes), 25):
            batch = changes[at:at + 25]
            bazarr_request(spec, endpoint, {form_id: [p[0] for p in batch], 'profileid': [p[1] for p in batch]})
        print(f'{instance}/{app}: {len(records)} indexed titles, {len(changes)} language profiles assigned', flush=True)


def setup_ready(config, instance):
    deadline = time.monotonic() + 120
    while True:
        try:
            setup(config, instance)
            return
        except Failure:
            if time.monotonic() >= deadline:
                raise
            time.sleep(5)
