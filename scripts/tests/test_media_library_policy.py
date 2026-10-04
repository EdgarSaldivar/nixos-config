import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'media-library-policy.py'
spec = importlib.util.spec_from_file_location('media_library_policy', MODULE)
mlp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mlp)
POLICY = mlp.load_policy()
# Small fixtures below exercise the main profiles; legacy targets are tested separately.
POLICY['targets'] = {'radarr': {'1': '1080', '7': '2160'},
                     'sonarr': {'1': '1080', '7': '2160'}, 'animearr': {'7': '1080'}}


def leaf(qid, name, resolution, allowed=True):
    return {'allowed': allowed, 'items': [], 'quality': {'id': qid, 'name': name, 'resolution': resolution}}


def group(qid, resolution, allowed=True):
    return {'id': 1002 if resolution == 1080 else 1003, 'name': f'WEB {resolution}p',
            'allowed': allowed, 'items': [leaf(qid, f'WEBDL-{resolution}p', resolution, allowed),
                                         leaf(15 if resolution == 1080 else 17, f'WEBRip-{resolution}p', resolution, allowed)]}


def profiles(app):
    remux_id = 30 if app == 'radarr' else 20
    p1080 = {'id': 1 if app != 'animearr' else 7, 'name': '1080p Preferred', 'cutoff': 7,
             'upgradeAllowed': True, 'minFormatScore': 100 if app == 'animearr' else 0,
             'formatItems': [], 'items': [leaf(9, 'HDTV-1080p', 1080), group(3, 1080),
                                          leaf(7, 'Bluray-1080p', 1080), leaf(remux_id, 'Bluray-1080p Remux', 1080, app == 'animearr'),
                                          group(18, 2160, False), leaf(19, 'Bluray-2160p', 2160, False)]}
    if app == 'animearr':
        p1080['cutoff'] = remux_id
        bd = ['Top SeaDex Muxers', 'SeaDex Muxers', 'SeaDex Muxers', 'SeaDex Muxers',
              'Remuxes', 'FanSubs', 'P2P/Scene', 'Mini Encodes']
        web = ['Muxers', 'Top FanSubs', 'Official Subs', 'Official Subs', 'FanSubs', 'FanSubs']
        p1080['formatItems'] = [
            *[{'format': 27 + i, 'name': f'Anime BD Tier {i + 1:02d} ({label})', 'score': 1400 - i * 100}
              for i, label in enumerate(bd)],
            *[{'format': 35 + i, 'name': f'Anime Web Tier {i + 1:02d} ({label})', 'score': 600 - i * 100}
              for i, label in enumerate(web)],
            {'format': 51, 'name': 'Multi-Audio', 'score': 1500},
            {'format': 49, 'name': 'Anime Dual Audio', 'score': 2000},
            {'format': 48, 'name': '10bit', 'score': 300},
            {'format': 43, 'name': 'Uncensored', 'score': 300},
            {'format': 44, 'name': 'v0', 'score': -51},
            {'format': 45, 'name': 'v1', 'score': 1},
            {'format': 26, 'name': 'v4', 'score': 4}]
        return [p1080]
    p4k = copy.deepcopy(p1080)
    p4k.update(id=7, name='4k Preferred', cutoff=19)
    p4k['items'][-2]['allowed'] = True
    for child in p4k['items'][-2]['items']:
        child['allowed'] = True
    p4k['items'][-1]['allowed'] = True
    return [p1080, p4k]


def fixture():
    result = {}
    for app in ('radarr', 'sonarr', 'animearr'):
        result[app] = {'qualityprofile': profiles(app), 'qualitydefinition': [
            {'id': 1, 'title': 'WEBDL-1080p', 'quality': {'id': 3, 'name': 'WEBDL-1080p'},
             'minSize': 5, 'preferredSize': 95, 'maxSize': 100},
            {'id': 2, 'title': 'WEBDL-2160p', 'quality': {'id': 18, 'name': 'WEBDL-2160p'},
             'minSize': 5, 'preferredSize': 200, 'maxSize': None}],
                       'customformat': [], 'releaseprofile': []}
    result['radarr']['releaseprofile'] = [
        {'id': 3, 'enabled': True, 'required': [], 'ignored': ['x264', '264', 'h264', 'h.264', 'No bad DV'], 'tags': [6]}]
    return result


class FakeClient:
    def __init__(self, state, backup_status='completed', bad_readback=False):
        self.state = copy.deepcopy(state)
        self.backup_status = backup_status
        self.bad_readback = bad_readback
        self.writes = []
        self.backups = 0
        self.next_id = 500

    def collection(self, name):
        return copy.deepcopy(self.state[name])

    def item(self, name, item_id):
        if name == 'command':
            return {'id': item_id, 'status': self.backup_status}
        found = next(x for x in self.state[name] if x['id'] == item_id)
        result = copy.deepcopy(found)
        if self.bad_readback and self.writes:
            result['name'] = 'wrong' if name == 'customformat' else result.get('name')
            if name == 'qualityprofile':
                result['upgradeAllowed'] = True
            if name == 'qualitydefinition':
                result['preferredSize'] = -1
            if name == 'releaseprofile':
                result['ignored'] = ['wrong']
        return result

    def backup(self, timeout=300):
        self.backups += 1
        if self.backup_status != 'completed':
            raise mlp.PolicyError('backup did not complete before timeout')
        return 70 + self.backups

    def request(self, method, path, body=None):
        name = path.split('/')[0]
        if method == 'POST':
            result = copy.deepcopy(body)
            result['id'] = self.next_id
            self.next_id += 1
            self.state[name].append(result)
        elif method == 'PUT':
            item_id = int(path.split('/')[1])
            result = copy.deepcopy(body)
            self.state[name] = [result if x['id'] == item_id else x for x in self.state[name]]
        elif method == 'DELETE':
            item_id = int(path.split('/')[1])
            self.state[name] = [x for x in self.state[name] if x['id'] != item_id]
            result = {}
        else:
            raise AssertionError(method)
        self.writes.append((method, path, copy.deepcopy(body)))
        return copy.deepcopy(result)


def clients(state, **kwargs):
    return {app: FakeClient(value, **kwargs) for app, value in state.items()}


class MediaPolicyTests(unittest.TestCase):
    def test_legacy_profiles_are_all_tuned_without_assignment_changes(self):
        state = fixture()
        for app in ('radarr', 'sonarr', 'animearr'):
            legacy = copy.deepcopy(state[app]['qualityprofile'][-1])
            legacy['id'] = 5
            legacy['name'] = 'Legacy 4K'
            state[app]['qualityprofile'].append(legacy)
        legacy_hd = copy.deepcopy(state['sonarr']['qualityprofile'][0])
        legacy_hd['id'] = 4
        state['sonarr']['qualityprofile'].append(legacy_hd)
        policy = mlp.load_policy()
        operations = mlp.make_plan(state, policy)['operations']
        updates = [o for o in operations if o['collection'] == 'qualityprofile']
        self.assertEqual({(o['app'], str(o['id'])) for o in updates},
                         {(app, profile) for app, ids in policy['targets'].items() for profile in ids})
        for op in updates:
            self.assertEqual(op['before']['name'], op['desired']['name'])
            self.assertFalse(op['desired']['upgradeAllowed'])

    def test_drift_during_backups_prevents_config_writes(self):
        state = fixture()
        api = clients(state)
        normal_backup = api['animearr'].backup
        def changing_backup(**kwargs):
            result = normal_backup(**kwargs)
            api['sonarr'].state['qualityprofile'][0]['name'] = 'Changed while backing up'
            return result
        api['animearr'].backup = changing_backup
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(mlp.PolicyError, 'during backups'):
                mlp.apply_plan(mlp.make_plan(state, POLICY), api, POLICY, Path(directory) / 'progress')
        self.assertFalse(any(c.writes for c in api.values()))

    def test_remux_minimum_above_soft_target_is_preserved(self):
        state = fixture()
        state['sonarr']['qualitydefinition'][0]['minSize'] = 35
        op = next(o for o in mlp.make_plan(state, POLICY)['operations']
                  if o['app'] == 'sonarr' and o['collection'] == 'qualitydefinition' and o['id'] == 1)
        self.assertEqual(op['desired']['minSize'], 35)
        self.assertEqual(op['desired']['preferredSize'], 35)
        self.assertIsNone(op['desired']['maxSize'])

    def test_underscore_separators_do_not_hide_dv_or_languages(self):
        import re
        specs = {x['name']: x['regex'] for x in POLICY['customFormats']}
        self.assertIsNotNone(re.search(specs['MLP DV without HDR fallback'], 'Movie_1080p_DV_HEVC'))
        self.assertIsNone(re.search(specs['MLP DV without HDR fallback'], 'Movie_DV_HDR10_HEVC'))
        self.assertIsNotNone(re.search(specs['MLP HDR compatible DV'], 'Movie_DV_Profile_8.1_HEVC'))
        self.assertIsNotNone(re.search(specs['MLP Anime English and native audio'], 'Anime_EN_JP_HEVC'))

    def test_nested_groups_cutoff_and_remux_order(self):
        plan = mlp.make_plan(fixture(), POLICY)
        ops = [x for x in plan['operations'] if x['collection'] == 'qualityprofile' and x['app'] == 'radarr']
        self.assertEqual(len(ops), 2)
        for op in ops:
            items = op['desired']['items']
            groups = {x.get('id'): x for x in items if x.get('id')}
            self.assertEqual([x['quality']['id'] for x in groups[1002]['items']], [3, 15, 7])
            self.assertLess(next(i for i,x in enumerate(items) if x.get('quality', {}).get('id') == 30),
                            next(i for i,x in enumerate(items) if x.get('id') == 1002))
            self.assertFalse(next(x for x in items if x.get('quality', {}).get('id') == 30)['allowed'])
            self.assertFalse(any(x.get('quality', {}).get('id') == 0 for x in mlp.flatten(items)))
        self.assertEqual({x['mode']: x['desired']['cutoff'] for x in ops}, {'1080': 1002, '2160': 1003})
        self.assertTrue(next(x for x in next(x for x in ops if x['mode'] == '2160')['desired']['items']
                             if x.get('id') == 1002)['allowed'])

    def test_1080_target_rejects_enabled_4k(self):
        state = fixture()
        state['radarr']['qualityprofile'][0]['items'][-1]['allowed'] = True
        with self.assertRaises(mlp.PolicyError):
            mlp.make_plan(state, POLICY)

    def test_anime_tiers_and_no_audio_double_bonus(self):
        plan = mlp.make_plan(fixture(), POLICY)
        op = next(x for x in plan['operations'] if x['app'] == 'animearr' and x['collection'] == 'qualityprofile')
        scores = {x['name']: x['score'] for x in op['desired']['formatItems']}
        self.assertEqual(scores['Anime BD Tier 01 (Top SeaDex Muxers)'], 140)
        self.assertEqual(scores['Anime Web Tier 06 (FanSubs)'], 10)
        self.assertEqual([scores[x['name']] for x in op['desired']['formatItems']
                          if x['name'].startswith(('Anime BD Tier ', 'Anime Web Tier '))],
                         list(range(140, 0, -10)))
        self.assertEqual(scores['10bit'], 75)
        self.assertEqual(scores['Uncensored'], 100)
        self.assertEqual(scores['v0'], -51)
        self.assertEqual(scores['v1'], 1)
        self.assertEqual(scores['v4'], 4)
        self.assertEqual(scores['Multi-Audio'], 0)
        self.assertEqual(scores['Anime Dual Audio'], 0)
        self.assertEqual(scores['MLP Anime English and native audio'], 3000)
        self.assertEqual(op['desired']['minFormatScore'], 0)

    def test_codec_ban_removed_exactly_and_empty_rule_deleted(self):
        state = fixture()
        state['radarr']['releaseprofile'].append({'id': 4, 'enabled': True, 'required': [], 'ignored': ['H264'], 'tags': [9]})
        ops = [x for x in mlp.make_plan(state, POLICY)['operations'] if x['collection'] == 'releaseprofile']
        self.assertEqual(ops[0]['desired']['ignored'], ['No bad DV'])
        self.assertEqual(ops[0]['desired']['tags'], [6])
        self.assertEqual(ops[1]['desired']['ignored'], [])
        self.assertEqual(ops[1]['action'], 'delete')
        self.assertEqual(ops[1]['desired']['tags'], [9])

    def test_delete_codec_only_rule_and_verify_absence(self):
        state = fixture()
        state['radarr']['releaseprofile'][0]['ignored'] = ['x264', 'h264']
        api = clients(state)
        with tempfile.TemporaryDirectory() as directory:
            result = mlp.apply_plan(mlp.make_plan(state, POLICY), api, POLICY, Path(directory) / 'progress')
        self.assertEqual(api['radarr'].state['releaseprofile'], [])
        deletion = next(x for x in result['completed'] if x['action'] == 'delete')
        self.assertTrue(deletion['verified'])
        self.assertEqual(mlp.make_plan(mlp.fetch_snapshot(api), POLICY)['operations'], [])

    def test_cf_spec_and_regex_heuristics(self):
        specs = {x['name']: x for x in POLICY['customFormats']}
        payload = mlp.cf_payload(specs['MLP HEVC'])
        self.assertEqual(payload['specifications'][0]['implementation'], 'ReleaseTitleSpecification')
        self.assertEqual(payload['specifications'][0]['fields'], [{'name': 'value', 'value': specs['MLP HEVC']['regex']}])
        match = lambda name, title: bool(__import__('re').search(specs[name]['regex'], title))
        self.assertTrue(match('MLP HDR compatible DV', 'Movie.DV.HDR10+.2160p'))
        self.assertTrue(match('MLP HDR compatible DV', 'Movie.DoVi.Profile8.1.2160p'))
        self.assertTrue(match('MLP HDR', 'Movie.Profile8.1.DoVi.2160p'))
        self.assertTrue(match('MLP DV without HDR fallback', 'Movie.DoVi.Profile8.2160p'))
        self.assertFalse(match('MLP DV without HDR fallback', 'Movie.DV.HDR.2160p'))
        self.assertFalse(match('MLP Anime English and native audio', 'Anime.Dual.Multi.Audio'))
        self.assertTrue(match('MLP Anime English and native audio', 'Anime.ENG.JPN'))
        self.assertLess(specs['MLP DV without HDR fallback']['score'], -5000)

    def test_real_returned_ids_and_idempotence(self):
        state = fixture()
        api = clients(state)
        plan = mlp.make_plan(state, POLICY)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress.json'
            result = mlp.apply_plan(plan, api, POLICY, path)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for app, client in api.items():
                ids = {x['name']: x['id'] for x in client.state['customformat']}
                for p in client.state['qualityprofile']:
                    for item in p['formatItems']:
                        if item['name'] in ids:
                            self.assertEqual(item['format'], ids[item['name']])
                            self.assertGreater(item['format'], 0)
                self.assertEqual(client.backups, 1)
            new_state = mlp.fetch_snapshot(api)
            self.assertEqual(mlp.make_plan(new_state, POLICY)['operations'], [])

    def test_backup_timeout_prevents_all_config_writes(self):
        state = fixture()
        api = clients(state, backup_status='queued')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress.json'
            with self.assertRaises(mlp.PolicyError):
                mlp.apply_plan(mlp.make_plan(state, POLICY), api, POLICY, path, 0)
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_text())['status'], 'failed')
            self.assertTrue(all(not c.writes for c in api.values()))

    def test_backup_covers_all_targets_even_for_one_app_change(self):
        state = fixture()
        first = clients(state)
        with tempfile.TemporaryDirectory() as directory:
            mlp.apply_plan(mlp.make_plan(state, POLICY), first, POLICY, Path(directory) / 'first.json')
            settled = mlp.fetch_snapshot(first)
            settled['radarr']['qualitydefinition'][0]['preferredSize'] = 99
            second = clients(settled)
            plan = mlp.make_plan(settled, POLICY)
            self.assertEqual({x['app'] for x in plan['operations']}, {'radarr'})
            mlp.apply_plan(plan, second, POLICY, Path(directory) / 'second.json')
            self.assertTrue(all(c.backups == 1 for c in second.values()))
            self.assertFalse(second['sonarr'].writes)
            self.assertFalse(second['animearr'].writes)

    def test_drift_refusal_before_backup(self):
        state = fixture()
        api = clients(state)
        api['radarr'].state['qualityprofile'][0]['cutoff'] = 1002
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(mlp.PolicyError):
                mlp.apply_plan(mlp.make_plan(state, POLICY), api, POLICY, Path(directory) / 'progress')
        self.assertTrue(all(c.backups == 0 and not c.writes for c in api.values()))

    def test_untrusted_plan_operations_rejected(self):
        state = fixture()
        plan = mlp.make_plan(state, POLICY)
        plan['operations'].append({'app': 'radarr', 'collection': 'qualityprofile', 'action': 'delete', 'id': 1})
        api = clients(state)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(mlp.PolicyError):
                mlp.apply_plan(plan, api, POLICY, Path(directory) / 'progress')
        self.assertTrue(all(not c.writes for c in api.values()))

    def test_cli_fixture_plan_requires_explicit_hash_on_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture_path = Path(directory) / 'fixture.json'
            plan_path = Path(directory) / 'plan.json'
            policy_path = Path(directory) / 'policy.json'
            fixture_path.write_text(json.dumps(fixture()))
            policy_path.write_text(json.dumps(POLICY))
            with redirect_stdout(io.StringIO()):
                mlp.main(['plan', '--policy', str(policy_path), '--fixture', str(fixture_path), '--output', str(plan_path)])
            self.assertEqual(plan_path.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(mlp.PolicyError, 'explicit reviewed hash'):
                mlp.main(['apply', '--reviewed', str(plan_path), '--review-hash', 'wrong',
                          '--progress', str(Path(directory) / 'progress.json')])

    def test_readback_mismatch_survives_with_returned_id(self):
        state = fixture()
        api = clients(state, bad_readback=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress.json'
            with self.assertRaises(mlp.PolicyError):
                mlp.apply_plan(mlp.make_plan(state, POLICY), api, POLICY, path)
            record = json.loads(path.read_text())
            self.assertEqual(record['status'], 'failed')
            self.assertEqual(record['completed'][0]['id'], 500)
            self.assertFalse(record['completed'][0]['verified'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_lost_write_response_records_inflight_operation(self):
        class LostResponse(FakeClient):
            def request(self, method, path, body=None):
                super().request(method, path, body)
                raise mlp.PolicyError('API response lost')

        state = fixture()
        api = clients(state)
        api['radarr'] = LostResponse(state['radarr'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress.json'
            with self.assertRaises(mlp.PolicyError):
                mlp.apply_plan(mlp.make_plan(state, POLICY), api, POLICY, path)
            record = json.loads(path.read_text())
            self.assertEqual(record['status'], 'failed')
            self.assertEqual(record['inFlight']['app'], 'radarr')
            self.assertEqual(record['inFlight']['collection'], 'customformat')
            self.assertEqual(record['completed'], [])

    def test_unlimited_max_size_and_shared_definition(self):
        state = fixture()
        operations = [x for x in mlp.make_plan(state, POLICY)['operations'] if x['collection'] == 'qualitydefinition']
        radarr = [x for x in operations if x['app'] == 'radarr']
        self.assertEqual({x['desired']['preferredSize'] for x in radarr}, {35, 140})
        self.assertTrue(all(x['desired']['maxSize'] is None for x in radarr))
        self.assertTrue(all(x['desired']['minSize'] == 5 for x in radarr))
        self.assertEqual([x['desired']['preferredSize'] for x in operations if x['app'] == 'animearr'], [15, 60])

    def test_inventory_protection_inode_and_runtime(self):
        base = {'app': 'radarr', 'resolution': 2160, 'size': 200 * 1024**3,
                'mediaInfo': {'runTime': 100}, 'device': 2, 'inode': 3, 'path': '/library/a', 'itemId': 4, 'fileId': 5}
        protected = dict(base, title='The Lord of the Rings', inode=4)
        protected_alias = dict(base, title='Unrelated alias', inode=4, path='/alias')
        duplicate = dict(base, title='Other Movie', path='/other/hardlink', itemId=6, fileId=7)
        unique = dict(base, title='Other Movie', inode=8, path='/library/b', itemId=8, fileId=9)
        rows = mlp.inventory_candidates([dict(base, title='Movie'), protected_alias, protected, duplicate, unique], POLICY)
        self.assertEqual(len(rows), 2)
        self.assertEqual({x['path'] for x in rows}, {'/library/a', '/library/b'})
        self.assertTrue(all(x['resolution'] == 2160 and x['runtimeMinutes'] == 100 for x in rows))
        self.assertTrue(all(x['mediaInfo']['runTime'] == 100 for x in rows))
        self.assertEqual(rows[0]['softTargetBytes'], 100 * 140 * 1024**2)


if __name__ == '__main__':
    unittest.main()
