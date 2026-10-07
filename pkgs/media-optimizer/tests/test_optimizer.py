import errno
from concurrent.futures import Future
import json
from io import BytesIO
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from media_optimizer import qa
from media_optimizer.core import (Deluge, Failure, Journal, Review, atomic_json, fetch_torrent, identity,
                                  load_config, stall_observation, torrent_metadata)
from media_optimizer.engine import (Runner, inventory, pack_file_map, pack_quality_warnings, pack_seasons, rank_releases,
                                    reserved_targets, runtime_minutes, select_pack, source_key)


def bencode(value):
    if isinstance(value, int):
        return b'i' + str(value).encode() + b'e'
    if isinstance(value, bytes):
        return str(len(value)).encode() + b':' + value
    if isinstance(value, list):
        return b'l' + b''.join(bencode(x) for x in value) + b'e'
    return b'd' + b''.join(bencode(k) + bencode(v) for k, v in sorted(value.items())) + b'e'


def settings(root):
    return {'state_dir': str(root / 'state'), 'legacy_state_dir': str(root / 'legacy'),
            'concurrency': 5, 'verification_concurrency': 1, 'original_retention_days': 0,
            'stage_host': str(root / 'Torrents' / 'optimization'), 'stage_deluge': '/data/optimization',
            'deluge_label': 'media-optimizer', 'minimum_savings': .3, 'cooldown_days': 30,
            'protected_title_regex': r'(?i)Game of Thrones|Lord of the Rings|Dune',
            'preferred_mb_per_minute': {'radarr': {'1080': 35}}, 'apps': {}}


def source(path, app='radarr', item=1):
    return {'app': app, 'item_id': item, 'file_id': 10, 'path': str(path), 'size': path.stat().st_size,
            'identity': identity(path), 'title': 'Movie', 'resolution': 1080, 'native': 'eng', 'edition': ''}


def release(**kwargs):
    value = {'title': 'Movie.1080p.HEVC', 'protocol': 'torrent', 'seeders': 1,
             'quality': {'quality': {'resolution': 1080}}, 'size': 6, 'mappedMovieId': 1,
             'indexerId': 2, 'customFormatScore': 1000, 'rejections': []}
    value.update(kwargs)
    return value


class FakeArr:
    def __init__(self, file=None):
        self.file = file
        self.posts = []
        self.lose_import = False

    def request(self, path, body=None, **kwargs):
        if path.startswith('movie/'):
            return {'movieFile': self.file}
        if path == 'command':
            self.posts.append(body)
            if self.lose_import:
                raise Failure('lost response')
            return {'id': 77}
        if path.startswith('command/'):
            return {'status': 'started'}
        raise AssertionError(path)


class FakeDeluge:
    def __init__(self, config):
        self.config = config
        self.data = {}
        self.adds = 0
        self.removes = []
        self.lose_add = False

    def torrents(self):
        return self.data

    def add(self, job, raw):
        self.adds += 1
        self.data[job['hash']] = {'save_path': '/data/optimization/' + job['id'], 'label': '', 'state': 'Paused'}
        if self.lose_add:
            raise Failure('lost response')
        return job['hash']

    def owned(self, job, torrent, allow_unlabelled=False):
        return Deluge.owned(self, job, torrent, allow_unlabelled)

    def configure(self, job, torrent):
        self.owned(job, torrent, True)
        torrent.update(label='media-optimizer', state='Downloading')

    def remove(self, job, torrent):
        self.owned(job, torrent)
        self.removes.append(job['hash'])
        del self.data[job['hash']]


class OptimizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = settings(self.root)
        Path(self.config['stage_host']).mkdir(parents=True)
        self.old = self.root / 'original.mkv'
        self.old.write_bytes(b'o' * 10)
        self.src = source(self.old)
        self.app = FakeArr({'id': 10, 'path': str(self.old)})
        self.journal = Journal(self.config['state_dir'])
        self.deluge = FakeDeluge(self.config)
        self.runner = Runner(self.config, self.journal, {'radarr': self.app}, self.deluge)

    def tearDown(self):
        self.runner.search_pool.shutdown(wait=True)
        self.runner.qa_pool.shutdown(wait=True)
        self.journal.db.close()
        self.temp.cleanup()

    def test_public_metadata_and_exact_original_infohash(self):
        import hashlib
        info = {b'name': b'movie.mkv', b'length': 6, b'piece length': 16384, b'pieces': b'a' * 20}
        raw = bencode({b'info': info})
        result = torrent_metadata(raw)
        self.assertEqual(result['hash'], hashlib.sha1(bencode(info)).hexdigest())
        self.assertEqual(result['files'][0]['path'], 'movie.mkv')

    def test_private_unsafe_and_executable_torrents_rejected(self):
        cases = [{b'name': b'movie.mkv', b'length': 6, b'private': 1},
                 {b'name': b'../movie.mkv', b'length': 6},
                 {b'name': b'pack', b'files': [{b'path': [b'..', b'movie.mkv'], b'length': 6}]},
                 {b'name': b'pack', b'files': [{b'path': [b'movie.mkv'], b'length': 6},
                                            {b'path': [b'installer.exe'], b'length': 1}]},
                 {b'name': b'pack', b'files': [{b'path': [b'movie.mkv'], b'length': 6, b'attr': b'l'}]}]
        for info in cases:
            with self.subTest(info=info), self.assertRaises(Failure):
                torrent_metadata(bencode({b'info': info}))

    def test_cache_fallback_uses_accepted_header_and_verifies_hash(self):
        import hashlib
        info = {b'name': b'movie.mkv', b'length': 6}
        raw = bencode({b'info': info})
        info_hash = hashlib.sha1(bencode(info)).hexdigest()
        def cache(request, timeout):
            self.assertEqual(request.get_header('User-agent'), 'Mozilla/5.0')
            self.assertEqual(timeout, 30)
            return BytesIO(raw)
        with patch('media_optimizer.core.urlopen', side_effect=cache):
            data, metadata = fetch_torrent({'infoHash': info_hash})
            self.assertEqual(data, raw)
            self.assertEqual(metadata['hash'], info_hash)
            with self.assertRaisesRegex(Failure, 'hash mismatch'):
                fetch_torrent({'infoHash': '0' * 40})

    def test_no_quota_or_reserve_config(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.config))
        self.assertEqual(load_config(path)['concurrency'], 5)
        for key in ('daily_bytes', 'speed_limit', 'free_floor_bytes', 'original_retention_days'):
            path.write_text(json.dumps(self.config | {key: 1}))
            with self.assertRaises(Failure):
                load_config(path)

    def test_live_arr_timespan_runtime_is_minutes(self):
        self.assertAlmostEqual(runtime_minutes('2:48:18'), 168.3)
        self.assertAlmostEqual(runtime_minutes('0:24:30.500'), 24.5083333)
        self.assertEqual(runtime_minutes(90), 90)
        self.assertEqual(runtime_minutes('unknown'), 0)

    def test_intent_survives_reopen_and_sensitive_fields_never_saved(self):
        self.journal.save({'id': 'one', 'state': 'submitting', 'hash': 'a' * 40})
        reopened = Journal(self.config['state_dir'])
        self.assertEqual(reopened.jobs()[0]['state'], 'submitting')
        with self.assertRaises(Failure):
            reopened.save({'id': 'secret', 'state': 'submitting', 'downloadUrl': 'https://example/?apikey=SECRET'})
        self.assertNotIn(b'SECRET', (Path(self.config['state_dir']) / 'journal.sqlite').read_bytes())
        reopened.db.close()

    def test_exclusive_runner_lock(self):
        other = Journal(self.config['state_dir'])
        with self.journal.lock():
            with self.assertRaises(Failure), other.lock():
                pass
        other.db.close()

    def test_one_seed_eligible_and_more_seeds_preferred_within_tier(self):
        low = release(seeders=1, customFormatScore=1150)
        available = release(seeders=30, customFormatScore=1100, title='available')
        ranked = rank_releases([low, available], self.src, [], self.config)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0][0]['title'], 'available')
        hevc = release(seeders=1)
        h264 = release(seeders=100, customFormatScore=0, title='h264')
        self.assertEqual(rank_releases([h264, hevc], self.src, [], self.config)[0][0], hevc)

    def test_seven_one_preference_does_not_buy_excess_size_or_override_seeds(self):
        five = release(title='Movie.HEVC.DDP5.1', size=5)
        seven = release(title='Movie.HEVC.DDP7.1', size=6)
        self.assertEqual(rank_releases([five, seven], self.src, [], self.config)[0][0], seven)
        expensive = seven | {'size': 7}
        self.assertEqual(rank_releases([expensive, five], self.src, [], self.config)[0][0], five)
        available = five | {'seeders': 2}
        self.assertEqual(rank_releases([seven, available], self.src, [], self.config)[0][0], available)
        # Compatibility repairs have no saving gate, so the audio preference
        # must still decline a much larger release.
        self.assertEqual(rank_releases([seven | {'size': 50}, five],
                                       self.src | {'codec_remediation': True}, [], self.config)[0][0], five)

    def test_wrong_movie_resolution_language_and_artificial_frames_rejected(self):
        cases = [release(mappedMovieId=2), release(quality={'quality': {'resolution': 720}}),
                 release(rejections=['English is wanted, but found French']),
                 release(title='Movie.Ai-Enhanced.RIFE.60fps')]
        self.assertFalse(rank_releases(cases, self.src, [], self.config))
        allowed = release(rejections=['Existing file and the Quality profile does not allow upgrades'])
        self.assertTrue(rank_releases([allowed], self.src, [], self.config))

    def test_av1_aliases_and_mixed_codec_titles_are_blocked(self):
        for name in ('AV1', 'AV01', 'SVT-AV1', 'AOM', 'AV_1', 'HEVC.AV1'):
            self.assertFalse(rank_releases([release(title='Movie.'+name)], self.src, [], self.config))
        self.assertTrue(rank_releases([release(title='Movie.H264')], self.src, [], self.config))
        self.assertFalse(rank_releases([release(title='Movie.SDR')], self.src | {'requires_hdr': True}, [], self.config))

    def test_actual_av1_is_blocked_even_with_a_misleading_title(self):
        old = {'streams': [{'codec_type': 'video', 'codec_name': 'hevc', 'width': 1920}]}
        new = {'streams': [{'codec_type': 'video', 'codec_name': 'av1', 'width': 1920}]}
        with self.assertRaisesRegex(Review, 'unsupported replacement codec'):
            qa.validate_streams(old, new, 'eng', False)

    def test_av1_compatibility_repair_can_grow_and_still_preserves_resolution(self):
        large = release(size=20)
        self.assertFalse(rank_releases([large], self.src, [], self.config))
        repair = self.src | {'codec_remediation': True}
        self.assertTrue(rank_releases([large], repair, [], self.config))
        self.assertFalse(rank_releases([large | {'quality': {'quality': {'resolution': 720}}}], repair, [], self.config))
        meta = {'hash': 'a' * 40, 'size': 20, 'files': [{'path': 'new.mkv', 'index': 0, 'size': 20}]}
        with patch('media_optimizer.engine.fetch_torrent', return_value=(b'data', meta)):
            self.assertTrue(self.runner.submit(repair, [(large, [repair])], {}))

    def test_small_protected_av1_files_are_included_for_compatibility_repair(self):
        app = FakeArr()
        record = {'id': 1, 'title': 'Dune', 'runtime': 90,
                  'movieFile': {'id': 10, 'path': str(self.old), 'mediaInfo': {'videoCodec': 'AV1'},
                                'quality': {'quality': {'resolution': 1080}}}}
        with patch.object(app, 'request', return_value=[record]):
            rows = inventory({'radarr': app}, self.config)
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]['codec_remediation'])
            record['movieFile']['mediaInfo']['videoCodec'] = 'HEVC'
            self.assertEqual(inventory({'radarr': app}, self.config), [])

    def test_codec_audit_includes_distinct_library_paths_to_same_inode(self):
        alias = self.root/'alias.mkv'
        os.link(self.old, alias)
        records = [{'id': i, 'title': 'Movie', 'runtime': 90,
                    'movieFile': {'id': i, 'path': str(p), 'mediaInfo': {'videoCodec': 'AV1'},
                                  'quality': {'quality': {'resolution': 1080}}}}
                   for i, p in enumerate((self.old, alias), 1)]
        with patch.object(self.app, 'request', return_value=records):
            self.assertEqual(len(inventory({'radarr': self.app}, self.config)), 1)
            self.assertEqual(len(inventory({'radarr': self.app}, self.config, include_all=True)), 2)

    def test_compatibility_qa_waives_only_size_gate(self):
        larger = self.root / 'larger.mkv'
        larger.write_bytes(b'n' * 20)
        with self.assertRaisesRegex(Review, 'required space'):
            qa.verify(str(self.old), str(larger), str(self.root/'qa'))
        with patch('media_optimizer.qa.probe', return_value={}), \
             patch('media_optimizer.qa.validate_streams', side_effect=Review('stream checks still required')):
            with self.assertRaisesRegex(Review, 'stream checks still required'):
                qa.verify(str(self.old), str(larger), str(self.root/'qa'), minimum_savings=None)

    def test_completion_moves_disabled_only_for_owned_torrent(self):
        client = Deluge.__new__(Deluge)
        client.config = self.config
        calls = []
        client.rpc = lambda method, params: calls.append((method, params))
        job = {'id': 'one', 'state': 'submitting', 'hash': 'a' * 40}
        client.configure(job, {'save_path': '/data/optimization/one', 'label': ''})
        options = next(p for m, p in calls if m == 'core.set_torrent_options')
        self.assertEqual(options[0], ['a' * 40])
        self.assertIs(options[1]['move_completed'], False)

    def test_audited_codec_repairs_survive_refresh_and_disappear_after_source_changes(self):
        repair = self.src | {'codec_remediation': True}
        atomic_json(Path(self.config['state_dir'])/'codec-remediation.json', [repair])
        self.assertEqual(self.runner.merge_codec_repairs([self.src]), [repair])
        self.old.write_bytes(b'changed')
        self.assertEqual(self.runner.merge_codec_repairs([]), [])

    def test_native_season_pack_and_exact_episode_mapping(self):
        src = self.src | {'app': 'sonarr', 'series_id': 20, 'season': 3, 'episode_ids': [100]}
        other = src | {'episode_ids': [101], 'item_id': 101}
        pack = release(mappedSeriesId=20, fullSeason=True, seasonNumber=3, size=10)
        single = release(mappedSeriesId=20, mappedEpisodeInfo=[{'id': 100}])
        ranked = rank_releases([single, pack], src, [src, other], self.config)
        self.assertTrue(ranked[0][0]['fullSeason'])
        self.assertEqual(len(ranked[0][1]), 2)
        self.assertFalse(rank_releases([pack | {'seasonNumber': 4}], src, [src, other], self.config))
        self.assertTrue(rank_releases([pack | {'mappedEpisodeInfo': [{'id': 100}, {'id': 101}, {'id': 102}]}],
                                      src, [src, other], self.config))
        self.assertFalse(rank_releases([pack | {'mappedEpisodeInfo': [{'id': 101}]}], src, [src, other], self.config))
        self.assertFalse(rank_releases([single | {'mappedEpisodeInfo': [{'id': 999}]}], src, [src], self.config))

    def test_episode_range_packs_cover_shows_and_anime_despite_last_episode_parse(self):
        for app in ('sonarr', 'animearr'):
            rows = [self.src | {'app': app, 'series_id': 4, 'season': 2, 'item_id': n + 61,
                                'episode_ids': [n + 61], 'episode_numbers': [n], 'codec_remediation': True}
                    for n in range(1, 26)]
            batch = release(title='Show S2E01-25 1080p HEVC BATCH', mappedSeriesId=4,
                            seasonNumber=2, fullSeason=False, mappedEpisodeInfo=[{'id': 86}])
            ranked = rank_releases([batch], rows[-1], rows, self.config)
            self.assertEqual(len(ranked[0][1]), 25)
            self.assertTrue(ranked[0][0]['episodePack'])
            for bad in (batch | {'mappedSeriesId': 5}, batch | {'seasonNumber': 1},
                        batch | {'title': 'Show S1E01-25 1080p HEVC'},
                        batch | {'title': 'Show S2E01-12 1080p HEVC'},
                        batch | {'title': 'Show S2E25-01 1080p HEVC', 'mappedEpisodeInfo': []}):
                self.assertFalse(rank_releases([bad], rows[-1], rows, self.config))
            partial = batch | {'title': 'Show S02E01-E12 1080p HEVC'}
            self.assertEqual(len(rank_releases([partial], rows[0], rows, self.config)[0][1]), 12)

    def test_pack_filename_proof_requires_complete_unique_correct_season_coverage(self):
        rows = [self.src | {'app': 'animearr', 'series_id': 4, 'season': 2, 'item_id': n + 61,
                            'episode_ids': [n + 61], 'episode_numbers': [n]} for n in (1, 2)]
        files = [{'path': f'Show 2nd Season - {n:02} [HEVC].mkv'} for n in (1, 2)]
        mapping = pack_file_map(files + [{'path': 'Samples/Show S02E01.mkv'}], rows)
        self.assertEqual(set(mapping.values()), {'animearr:62', 'animearr:63'})
        self.assertEqual(set(pack_file_map([{'path': f'Show.S02E{n:02}.mkv'} for n in (1, 2)], rows).values()),
                         set(mapping.values()))
        for bad in (files[:1], files + files[:1], [{'path': 'Show S01E01.mkv'}, files[1]],
                    [{'path': 'Show - 01.mkv'}, files[1]], [{'path': 'Show S02E01-E02.mkv'}]):
            with self.assertRaises(Failure):
                pack_file_map(bad, rows)

    def test_proven_batch_mapping_corrects_absolute_numbering_but_checks_series(self):
        root = Path(self.config['stage_host'])/'batch'
        root.mkdir()
        path = root/'Show 2nd Season - 11 [HEVC].mkv'
        path.write_bytes(b'n' * 6)
        src = self.src | {'app': 'animearr', 'series_id': 4, 'season': 2, 'item_id': 72,
                          'episode_ids': [72], 'episode_numbers': [11]}
        job = {'id': 'batch', 'app': 'animearr', 'state': 'downloading', 'tasks': [], 'targets': [src],
               'files': [{'path': path.name, 'index': 0}], 'episode_file_map': {path.name: source_key(src)}}
        resource = {'path': str(path), 'series': {'id': 4}, 'episodes': [{'id': 47}]}
        with patch.object(self.runner, 'resources', return_value=[resource | {'series': {'id': 5}}]):
            self.runner.stage_tasks(job, {'is_finished': True})
        self.assertEqual(job['tasks'], [])
        with patch.object(self.runner, 'resources', return_value=[resource]):
            self.runner.stage_tasks(job, {'is_finished': True})
        self.assertEqual(job['tasks'][0]['source']['episode_ids'], [72])
        self.assertEqual(job['tasks'][0]['state'], 'pending')

    def test_overlapping_multi_episode_sources_cannot_duplicate_active_pack(self):
        busy = self.src | {'app': 'sonarr', 'series_id': 4, 'season': 2, 'episode_ids': [72, 73]}
        single = busy | {'episode_ids': [72]}
        self.journal.save({'id': 'batch', 'state': 'downloading', 'targets': [busy]})
        with patch('media_optimizer.engine.fetch_torrent') as fetch:
            self.assertFalse(self.runner.submit(single, [(release(), [single])], {}))
        fetch.assert_not_called()

    def test_unproven_episode_pack_is_never_added_to_downloader(self):
        src = self.src | {'app': 'sonarr', 'series_id': 4, 'season': 2, 'episode_ids': [72], 'episode_numbers': [11]}
        meta = {'hash': 'a' * 40, 'size': 6, 'files': [{'path': 'Show S01E11.mkv', 'index': 0}]}
        with patch('media_optimizer.engine.fetch_torrent', return_value=(b'data', meta)):
            self.assertFalse(self.runner.submit(src, [(release(episodePack=True), [src])], {}))
        self.assertEqual(self.deluge.adds, 0)

    def test_verified_episode_pack_is_one_job_with_all_targets_and_durable_mapping(self):
        rows = [self.src | {'app': 'sonarr', 'series_id': 4, 'season': 2, 'item_id': n + 61,
                            'episode_ids': [n + 61], 'episode_numbers': [n]} for n in (1, 2)]
        batch = release(title='Show S02E01-02 HEVC', mappedSeriesId=4, mappedEpisodeInfo=[{'id': 63}])
        meta = {'hash': 'a' * 40, 'size': 12,
                'files': [{'path': f'Show S02E{n:02}.mkv', 'index': n - 1, 'size': 6} for n in (1, 2)]}
        with patch('media_optimizer.engine.fetch_torrent', return_value=(b'data', meta)):
            self.assertTrue(self.runner.submit(rows[-1], rank_releases([batch], rows[-1], rows, self.config), {}))
        jobs = self.journal.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]['pack'])
        self.assertEqual(jobs[0]['targets'], rows)
        self.assertEqual(len(jobs[0]['episode_file_map']), 2)
        self.assertEqual(self.deluge.adds, 1)

    def test_parser_mapped_multiple_episodes_expand_only_the_covered_targets(self):
        rows = [self.src | {'app': 'sonarr', 'series_id': 4, 'season': 2, 'item_id': n + 61,
                            'episode_ids': [n + 61], 'episode_numbers': [n]} for n in (1, 2, 3)]
        batch = release(mappedSeriesId=4, mappedEpisodeInfo=[{'id': 62}, {'id': 63}])
        ranked = rank_releases([batch], rows[0], rows, self.config)
        self.assertEqual(ranked[0][1], rows[:2])
        self.assertTrue(ranked[0][0]['episodePack'])
        self.assertFalse(rank_releases([batch], rows[2], rows, self.config))

    def test_multi_season_pack_allows_one_several_or_all_seasons(self):
        rows = [self.src | {'app': 'sonarr', 'series_id': 4, 'season': season, 'item_id': season * 10 + n,
                            'episode_ids': [season * 10 + n], 'episode_numbers': [n]}
                for season in (1, 2, 3) for n in (1, 2)]
        batch = release(title='Show (S1-2+3) 1080p HEVC Batch', mappedSeriesId=4,
                        mappedEpisodeInfo=[{'id': 11}], size=36)
        self.assertEqual(pack_seasons(batch['title']), {1, 2, 3})
        self.assertEqual(pack_seasons('Show S01-S03'), {1, 2, 3})
        self.assertEqual(pack_seasons('Show S01E01-03'), set())
        ranked = rank_releases([batch], rows[0], rows[:2], self.config, rows)
        self.assertEqual(ranked[0][1], rows)
        files = [{'path': f'Show/S{r["season"]:02}E{r["episode_numbers"][0]:02}-Episode Title [CRC].mkv', 'index': i, 'size': 6}
                 for i, r in enumerate(rows)]
        for targets in ([rows[0]], rows[:2], rows[:4], rows):
            selected, mapping, priorities, size = select_pack(files, targets)
            self.assertEqual(selected, targets)
            self.assertEqual(len(mapping), len(targets))
            self.assertEqual(sum(priorities), len(targets))
            self.assertEqual(size, 6 * len(targets))

    def test_arr_minutes_seconds_runtime_keeps_large_imports_eligible_for_optimization(self):
        self.assertAlmostEqual(runtime_minutes('24:32'), 24 + 32 / 60)
        self.assertAlmostEqual(runtime_minutes('01:24:32'), 84 + 32 / 60)
        self.assertAlmostEqual(runtime_minutes('124:32.5'), 124 + 32.5 / 60)
        self.assertEqual(runtime_minutes('unknown'), 0)

    def test_only_available_multi_season_pack_selects_needed_file_without_whole_payload_size_gate(self):
        src = self.src | {'app': 'sonarr', 'series_id': 4, 'season': 2, 'episode_ids': [72], 'episode_numbers': [11]}
        batch = release(title='Show S01-S03 HEVC Batch', mappedSeriesId=4, mappedEpisodeInfo=[{'id': 72}], size=1006)
        meta = {'hash': 'a' * 40, 'size': 1006,
                'files': [{'path': 'Show S02E11.mkv', 'index': 0, 'size': 6},
                          {'path': 'Show S01E01.mkv', 'index': 1, 'size': 1000}]}
        ranked = rank_releases([batch], src, [src], self.config, [src])
        self.assertEqual(len(ranked), 1)
        with patch('media_optimizer.engine.fetch_torrent', return_value=(b'data', meta)):
            self.assertTrue(self.runner.submit(src, ranked, {}))
        job = self.journal.jobs()[0]
        self.assertEqual(job['file_priorities'], [1, 0])
        self.assertEqual(job['selected_size'], 6)

    def test_pack_selection_keeps_matching_subtitles_fonts_and_excludes_other_episodes_and_samples(self):
        src = self.src | {'app': 'sonarr', 'series_id': 4, 'season': 2, 'episode_ids': [72], 'episode_numbers': [11]}
        paths = ['Show S02E11.mkv', 'Show S02E11.eng.ass', 'Show S02E12.mkv', 'Samples/Show S02E11.mkv', 'fonts/style.ttf']
        files = [{'path': p, 'index': i, 'size': 1} for i, p in enumerate(paths)]
        _, _, priorities, size = select_pack(files, [src])
        self.assertEqual(priorities, [1, 1, 0, 0, 1])
        self.assertEqual(size, 3)

    def test_deluge_sets_file_selection_before_resuming_only_owned_torrent(self):
        client = Deluge.__new__(Deluge)
        client.config = self.config
        calls = []
        client.rpc = lambda method, params: calls.append((method, params))
        job = {'id': 'one', 'state': 'submitting', 'hash': 'a' * 40, 'file_priorities': [1, 0, 1]}
        client.configure(job, {'save_path': '/data/optimization/one', 'label': ''})
        self.assertEqual(calls[-2], ('core.set_torrent_options', [['a' * 40], {'file_priorities': [1, 0, 1]}]))
        self.assertEqual(calls[-1][0], 'core.resume_torrent')
        calls.clear()
        with self.assertRaises(Failure):
            client.configure(job, {'save_path': '/data/books', 'label': 'books'})
        self.assertEqual(calls, [])

    def test_av1_pack_can_also_shrink_large_non_av1_episode_without_waiving_its_saving_gate(self):
        rows = [self.src | {'app': 'sonarr', 'series_id': 4, 'season': 2, 'item_id': n + 61,
                            'episode_ids': [n + 61], 'episode_numbers': [n], 'codec_remediation': n == 1}
                for n in (1, 2, 3)]
        self.runner.records = rows
        meta = {'hash': 'a' * 40, 'size': 24, 'files': [
            {'path': f'Show S02E{n:02}.mkv', 'index': n - 1, 'size': size} for n, size in [(1, 10), (2, 6), (3, 8)]]}
        batch = release(title='Show S02E01-03 HEVC', episodePack=True)
        with patch('media_optimizer.engine.fetch_torrent', return_value=(b'data', meta)):
            self.assertTrue(self.runner.submit(rows[0], [(batch, rows)], {}))
        job = self.journal.jobs()[0]
        self.assertEqual(job['targets'], rows[:2])
        self.assertEqual(job['file_priorities'], [1, 1, 0])

    def test_rare_ambiguous_pack_retains_exact_native_mapping_when_only_option(self):
        src = self.src | {'app': 'animearr', 'series_id': 4, 'season': 2, 'episode_ids': [72],
                          'episode_numbers': [11], 'codec_remediation': True}
        batch = release(title='Show Batch 1080p HEVC', episodePack=True, mappedEpisodeInfo=[{'id': 72}])
        meta = {'hash': 'a' * 40, 'size': 200, 'files': [
            {'path': f'Show - {n:02}.mkv', 'index': n - 1, 'size': 100} for n in (1, 2)]}
        with patch('media_optimizer.engine.fetch_torrent', return_value=(b'data', meta)):
            self.assertTrue(self.runner.submit(src, [(batch, [src])], {}))
        job = self.journal.jobs()[0]
        self.assertEqual(job['targets'], [src])
        self.assertIsNone(job['file_priorities'])
        self.assertEqual(job['selected_size'], 200)
        self.assertTrue(job['pack'])

    def test_per_episode_rejection_skips_same_hash_without_globally_rejecting_healthy_files(self):
        src = self.src | {'app': 'sonarr', 'series_id': 4, 'season': 2, 'episode_ids': [72], 'episode_numbers': [11]}
        self.journal.reject('torrent:' + 'a' * 40 + ':sonarr:72', 'bad episode')
        meta = {'hash': 'a' * 40, 'size': 6, 'files': [{'path': 'Show S02E11.mkv', 'index': 0, 'size': 6}]}
        with patch('media_optimizer.engine.fetch_torrent', return_value=(b'data', meta)):
            self.assertFalse(self.runner.submit(src, [(release(), [src])], {}))
        self.assertFalse(self.journal.rejected('torrent:' + 'a' * 40))
        self.assertEqual(self.deluge.adds, 0)

    def test_pack_episode_qa_failure_leaves_other_files_running_and_releases_only_bad_reservation(self):
        src = self.src | {'app': 'sonarr', 'series_id': 4, 'season': 2, 'episode_ids': [72], 'episode_numbers': [11]}
        other = src | {'item_id': 73, 'episode_ids': [73], 'episode_numbers': [12]}
        job = {'id': 'one', 'hash': 'a' * 40, 'title': 'Show', 'app': 'sonarr', 'pack': True,
               'state': 'downloading', 'targets': [src, other], 'tasks': [
                   {'source': src, 'state': 'verifying', 'path': 'bad.mkv'},
                   {'source': other, 'state': 'verifying', 'path': 'good.mkv'}]}
        failed, healthy = Future(), Future()
        failed.set_exception(Review('sampled pictures do not match'))
        self.runner.qa_futures = {('one', 0): failed, ('one', 1): healthy}
        torrent = {'save_path': '/data/optimization/one', 'label': 'media-optimizer', 'state': 'Downloading', 'total_done': 5}
        with patch.object(self.runner, 'stage_tasks'):
            self.runner.advance(job, {job['hash']: torrent})
        self.assertEqual([t['state'] for t in job['tasks']], ['rejected', 'verifying'])
        self.assertEqual(reserved_targets(job), [other])
        self.assertTrue(self.journal.rejected('torrent:' + job['hash'] + ':sonarr:72'))
        self.assertFalse(self.journal.rejected('torrent:' + job['hash']))
        self.assertFalse(self.journal.cooling('sonarr:72'))
        self.assertEqual(self.deluge.removes, [])
        healthy.cancel()

    def test_partial_pack_completes_when_selected_files_finish_despite_unselected_file_progress(self):
        src = self.src | {'app': 'sonarr', 'series_id': 4, 'season': 2, 'episode_ids': [72], 'episode_numbers': [11]}
        job = {'id': 'one', 'hash': 'a' * 40, 'title': 'Show', 'app': 'sonarr', 'pack': True,
               'state': 'downloading', 'targets': [src], 'file_priorities': [1, 0],
               'tasks': [{'source': src, 'state': 'imported'}]}
        torrent = {'save_path': '/data/optimization/one', 'label': 'media-optimizer', 'file_progress': [1, 0]}
        with patch.object(self.runner, 'stage_tasks'):
            self.runner.advance(job, {job['hash']: torrent})
        self.assertEqual(job['state'], 'seeding')

    def test_unmapped_pack_episode_is_rejected_without_discarding_imported_files(self):
        src = self.src | {'app': 'sonarr', 'series_id': 4, 'season': 2, 'episode_ids': [72], 'episode_numbers': [11]}
        other = src | {'item_id': 73, 'episode_ids': [73], 'episode_numbers': [12]}
        job = {'id': 'one', 'hash': 'a' * 40, 'title': 'Show', 'app': 'sonarr', 'pack': True,
               'state': 'downloading', 'targets': [src, other], 'tasks': [{'source': src, 'state': 'imported'}]}
        torrent = {'save_path': '/data/optimization/one', 'label': 'media-optimizer', 'is_finished': True}
        with patch.object(self.runner, 'stage_tasks'):
            self.runner.advance(job, {job['hash']: torrent})
        self.assertEqual(job['state'], 'seeding')
        self.assertEqual(job['tasks'][1]['state'], 'rejected')
        self.assertEqual(self.deluge.removes, [])

    def test_known_wrong_season_cannot_pass_single_episode_parser_mapping(self):
        src = self.src | {'app': 'animearr', 'series_id': 4, 'season': 1, 'episode_ids': [72], 'episode_numbers': [11]}
        wrong = release(title='Show S03E11 1080p HEVC', mappedSeriesId=4, mappedEpisodeInfo=[{'id': 72}])
        self.assertEqual(rank_releases([wrong], src, [src], self.config), [])

    def test_lost_add_response_reconciles_without_duplicate(self):
        self.deluge.lose_add = True
        meta = {'hash': 'a' * 40, 'size': 6, 'files': [{'path': 'new.mkv', 'index': 0, 'size': 6}]}
        with patch('media_optimizer.engine.fetch_torrent', return_value=(b'data', meta)):
            self.assertTrue(self.runner.submit(self.src, [(release(), [self.src])], {}))
        job = self.journal.jobs()[0]
        self.assertEqual(job['state'], 'submitting')
        self.runner.reconcile_submission(job, self.deluge.torrents())
        self.assertEqual(self.journal.jobs()[0]['state'], 'downloading')
        self.assertEqual(self.deluge.adds, 1)

    def test_existing_unrelated_hash_is_never_touched(self):
        meta = {'hash': 'a' * 40, 'size': 6, 'files': []}
        with patch('media_optimizer.engine.fetch_torrent', return_value=(b'data', meta)):
            self.assertFalse(self.runner.submit(self.src, [(release(), [self.src])], {'a' * 40: {'label': 'radarr'}}))
        self.assertEqual(self.deluge.adds, 0)
        self.assertEqual(self.deluge.removes, [])

    def test_owned_guard_rejects_books_normal_category_and_wrong_stage(self):
        job = {'id': 'one', 'hash': 'a' * 40}
        for torrent in [{'save_path': '/data/books', 'label': 'media-optimizer'},
                        {'save_path': '/data/optimization/one', 'label': 'radarr'}]:
            with self.assertRaises(Failure):
                self.deluge.remove(job, torrent)
        self.assertEqual(self.deluge.removes, [])

    def test_concurrent_normal_grab_does_not_hold_optimizer_slot(self):
        job = {'id': 'one', 'state': 'submitting', 'title': 'Movie', 'hash': 'a' * 40,
               'release_key': 'release', 'source_key': source_key(self.src)}
        normal = {'save_path': '/data/completed', 'label': 'radarr'}
        self.runner.fail(job, 'concurrent normal grab', {'a' * 40: normal})
        self.assertEqual(self.journal.jobs()[0]['state'], 'failed')
        self.assertEqual(self.deluge.removes, [])

    def test_stall_needs_thirty_minutes_and_a_spaced_confirmation_and_resets(self):
        job = {'last_done': 10, 'progress_at': 0, 'stall_sample_at': 0}
        torrent = {'state': 'Downloading', 'total_done': 10}
        self.assertFalse(stall_observation(job, torrent, 1799))
        self.assertFalse(stall_observation(job, torrent, 1800))
        self.assertFalse(stall_observation(job, torrent, 1900))
        self.assertTrue(stall_observation(job, torrent, 2100))
        self.assertFalse(stall_observation(job, torrent | {'total_done': 11}, 2101))
        self.assertEqual(job['stall_strikes'], 0)

    def test_paused_queued_and_seeds_are_not_stalls(self):
        for state in ['Paused', 'Queued', 'Seeding', 'Checking']:
            job = {'last_done': 10, 'progress_at': 0, 'stall_strikes': 3}
            self.assertFalse(stall_observation(job, {'state': state, 'total_done': 10}, 100000))

    def test_audio_offset_alignment_and_one_second_dub_error(self):
        rng = random.Random(42)
        a = [rng.random() for _ in range(4000)]
        b = [0] * 100 + a[:-100]
        result = qa.align(a, b, radius=120, span=300)
        self.assertGreater(result['correlation'], .99)
        self.assertAlmostEqual(result['old_minus_new'], -1)
        with self.assertRaises(Review):
            qa.consistent_alignment([result, {'correlation': .99, 'old_minus_new': 0}])

    def test_cover_art_and_cropped_4k_resolution(self):
        p = {'streams': [{'codec_type': 'video', 'width': 600, 'disposition': {'attached_pic': 1}},
                         {'codec_type': 'video', 'width': 3840, 'height': 1604}]}
        self.assertEqual(qa.resolution(p), 2160)

    def test_black_bars_do_not_hide_matching_cinema_picture(self):
        rows = [bytes([30 + (x * 7 + y * 11) % 180 for x in range(160)]) for y in range(60)]
        original = bytes(160 * 15) + b''.join(rows) + bytes(160 * 15)
        crop = b''.join(rows[round(i * 59 / 89)] for i in range(90))
        self.assertGreater(qa.frame_similarity(original, crop), .99)

    def test_dv_profile5_rejected_but_missing_native_audio_is_a_tradeoff(self):
        v = {'codec_type': 'video', 'width': 1920, 'height': 1080, 'codec_name': 'hevc', 'avg_frame_rate': '24/1'}
        eng = {'codec_type': 'audio', 'channels': 2, 'tags': {'language': 'eng'}}
        jp = eng | {'tags': {'language': 'jpn'}}
        old = {'streams': [v, eng, jp]}
        new = {'streams': [v, eng]}
        qa.validate_streams(old, new, 'jpn', True)
        self.assertEqual(qa.audio_tradeoffs(old, new, 'jpn'), [{'language': 'jpn', 'change': 'audio language absent'}])
        with self.assertRaises(Review):
            qa.validate_streams(old, {'streams': [v | {'side_data_list': [{'dv_profile': 5}]}, eng, jp]}, 'jpn', True)

    def test_seven_one_opus_to_five_one_and_atmos_base_is_allowed(self):
        v = {'codec_type': 'video', 'width': 3840, 'codec_name': 'hevc', 'avg_frame_rate': '24/1'}
        old_audio = {'codec_type': 'audio', 'codec_name': 'opus', 'channels': 8, 'tags': {'language': 'eng'}}
        new_audio = old_audio | {'codec_name': 'eac3', 'channels': 6}
        for profile in ('Dolby Digital Plus', 'Dolby Digital Plus + Dolby Atmos'):
            with self.subTest(profile=profile):
                qa.validate_streams({'streams': [v, old_audio]},
                                    {'streams': [v, new_audio | {'profile': profile}]}, 'eng', False)

    def test_surround_to_mono_or_stereo_is_allowed_and_reported(self):
        v = {'codec_type': 'video', 'width': 1920, 'codec_name': 'hevc', 'avg_frame_rate': '24/1'}
        a = {'codec_type': 'audio', 'channels': 8, 'tags': {'language': 'eng'}}
        for anime in (False, True):
            for channels in (1, 2, 4, 6):
                with self.subTest(anime=anime, channels=channels):
                    qa.validate_streams({'streams': [v, a]},
                                        {'streams': [v, a | {'channels': channels}]}, 'eng', anime)
                    self.assertEqual(qa.audio_tradeoffs({'streams': [v, a]}, {'streams': [v, a | {'channels': channels}]}, 'eng'),
                                     [{'language': 'eng', 'change': 'fewer channels', 'before': 8, 'after': channels}])
            with self.assertRaisesRegex(Review, 'no replacement audio'):
                qa.validate_streams({'streams': [v, a]}, {'streams': [v, a | {'channels': 0}]}, 'eng', anime)

    def test_main_tracks_report_tradeoffs_without_counting_commentary(self):
        v = {'codec_type': 'video', 'width': 1920, 'codec_name': 'hevc', 'avg_frame_rate': '24/1'}
        en = {'codec_type': 'audio', 'channels': 8, 'tags': {'language': 'eng'}}
        jp = en | {'tags': {'language': 'jpn'}}
        old = {'streams': [v, en, jp]}
        commentary = jp | {'tags': {'language': 'jpn', 'title': 'Commentary'}}
        new = {'streams': [v, en, jp | {'channels': 2}, commentary]}
        qa.validate_streams(old, new, 'jpn', True)
        self.assertEqual(qa.audio_tradeoffs(old, new, 'jpn'),
                         [{'language': 'jpn', 'change': 'fewer channels', 'before': 8, 'after': 2}])
        qa.validate_streams(old, {'streams': [v, en | {'channels': 6}, jp | {'channels': 6}]}, 'jpn', True)

    def test_atmos_is_optional_and_loss_is_reported_per_language(self):
        v = {'codec_type': 'video', 'width': 1920, 'codec_name': 'hevc', 'avg_frame_rate': '24/1'}
        a = {'codec_type': 'audio', 'channels': 8, 'profile': 'Dolby TrueHD + Dolby Atmos', 'tags': {'language': 'eng'}}
        new = a | {'channels': 6, 'profile': 'Dolby Digital Plus + Dolby Atmos'}
        qa.validate_streams({'streams': [v, a]}, {'streams': [v, new]}, 'eng', False)
        plain = new | {'profile': 'Dolby Digital Plus'}
        for extras in ([], [new | {'tags': {'language': 'fra'}}]):
            with self.subTest(extras=extras):
                qa.validate_streams({'streams': [v, a]}, {'streams': [v, plain] + extras}, 'eng', False)
                changes = qa.audio_tradeoffs({'streams': [v, a]}, {'streams': [v, plain] + extras}, 'eng')
                self.assertIn({'language': 'eng', 'change': 'Atmos absent'}, changes)

    def test_stereo_quad_and_mono_sources_remain_eligible(self):
        v = {'codec_type': 'video', 'width': 1920, 'codec_name': 'hevc', 'avg_frame_rate': '24/1'}
        a = {'codec_type': 'audio', 'tags': {'language': 'eng'}}
        for channels in (1, 2, 4):
            old = {'streams': [v, a | {'channels': channels}]}
            qa.validate_streams(old, old, 'eng', False)
            if channels > 1:
                qa.validate_streams(old, {'streams': [v, a | {'channels': channels - 1}]}, 'eng', False)

    def test_dub_timing_difference_from_source_is_not_a_replacement_sync_verdict(self):
        old_path = self.root / 'old.mkv'
        new_path = self.root / 'new.mkv'
        old_path.write_bytes(b'o' * 100)
        new_path.write_bytes(b'n' * 60)
        v = {'codec_type': 'video', 'index': 0, 'width': 1920, 'height': 1080,
             'codec_name': 'hevc', 'avg_frame_rate': '24/1'}
        eng = {'codec_type': 'audio', 'index': 1, 'channels': 2, 'tags': {'language': 'eng'}}
        jp = eng | {'index': 2, 'tags': {'language': 'jpn'}}
        data = {'streams': [v, eng, jp], 'format': {'duration': '1400'}}
        zero = {'correlation': .99, 'old_minus_new': 0}
        bad = {'correlation': .99, 'old_minus_new': -1}
        with patch('media_optimizer.qa.probe', return_value=data), \
                patch('media_optimizer.qa.measure_frame_rate', return_value={'rate': 24}), \
                patch('media_optimizer.qa.envelope', return_value=[]), \
                patch('media_optimizer.qa.align', side_effect=[zero] * 4 + [bad] * 4), \
                patch.object(qa, 'matching_frame', return_value=(b'', b'', .99)), \
                patch.object(qa, 'run', return_value=b''):
            result = qa.verify(str(old_path), str(new_path), str(self.root / 'qa'), native='jpn', anime=True)
        self.assertEqual(result['audio_correspondence']['eng'], [bad] * 4)
        self.assertEqual(len(result['frame_samples']), 4)
        self.assertTrue(old_path.exists())

    def test_measured_cadence_is_evidence_not_a_source_frame_rate_requirement(self):
        video = {'codec_type': 'video', 'width': 1920, 'codec_name': 'hevc', 'avg_frame_rate': '500/21'}
        audio = {'codec_type': 'audio', 'channels': 2}
        data = {'streams': [video, audio], 'format': {'duration': '1472'}}
        packets = {'packets': [{'pts_time': str(round(n * 1001 / 24000, 3))} for n in range(192)]}
        with patch.object(qa, 'run', return_value=json.dumps(packets).encode()) as run:
            measured = qa.measure_frame_rate('input.mkv', data)
        self.assertEqual(run.call_count, 4)
        self.assertAlmostEqual(measured['header_rate'], 500 / 21)
        self.assertAlmostEqual(measured['rate'], 24000 / 1001)
        new = data | {'measured_frame_rate': measured['rate']}
        old = data | {'measured_frame_rate': 24000 / 1001}
        qa.validate_streams(old, new, 'jpn', True)
        qa.validate_streams(old, data | {'measured_frame_rate': 30}, 'jpn', True)

    def test_missing_packet_timestamps_do_not_silently_trust_header(self):
        with patch.object(qa, 'run', return_value=b'{"packets": []}'):
            with self.assertRaisesRegex(Review, 'timestamp evidence'):
                qa.measure_frame_rate('input.mkv', {'format': {'duration': '1200'}})

    def test_stable_ninety_millisecond_dub_difference_passes_full_verification(self):
        old_path, new_path = self.root/'dub-old.mkv', self.root/'dub-new.mkv'
        old_path.write_bytes(b'o'*100)
        new_path.write_bytes(b'n'*60)
        v = {'codec_type': 'video', 'width': 1920, 'codec_name': 'hevc', 'avg_frame_rate': '24/1'}
        jp = {'codec_type': 'audio', 'index': 1, 'channels': 2, 'tags': {'language': 'jpn'}}
        en = jp | {'index': 2, 'tags': {'language': 'eng'}}
        data = {'streams': [v, jp, en], 'format': {'duration': '1400'}}
        native = {'correlation': .99, 'old_minus_new': -.03}
        english = {'correlation': .99, 'old_minus_new': .06}
        with patch.object(qa, 'probe', return_value=data), \
                patch.object(qa, 'measure_frame_rate', return_value={'rate': 24}), \
                patch.object(qa, 'envelope', return_value=[]), \
                patch.object(qa, 'align', side_effect=[native]*4 + [english]*4), \
                patch.object(qa, 'matching_frame', return_value=(b'', b'', .99)), \
                patch.object(qa, 'run', return_value=b''):
            result = qa.verify(str(old_path), str(new_path), str(self.root/'dub-qa'), native='jpn')
        self.assertEqual(len(result['frame_samples']), 4)
        self.assertAlmostEqual(result['offset'], -.03)

    def test_high_partial_season_rejection_rate_is_reported_without_overriding_good_imports(self):
        tasks = [{'source': {'season': 2}, 'state': 'rejected', 'error': 'frame cadence differs'} for _ in range(4)]
        tasks += [{'source': {'season': 2}, 'state': 'imported'} for _ in range(6)]
        warnings = pack_quality_warnings({'pack': True, 'tasks': tasks})
        self.assertEqual(warnings[0]['checked'], 10)
        self.assertEqual(warnings[0]['rejected'], 4)
        self.assertEqual(pack_quality_warnings({'pack': True, 'tasks': tasks[1:]}), [])

    def stalled_job(self):
        job = {'id': 'stuck', 'state': 'downloading', 'title': 'Rare Movie', 'hash': 'b' * 40,
               'release_key': 'stuck-release', 'source_key': 'radarr:99', 'source': self.src | {'item_id': 99},
               'targets': [self.src | {'item_id': 99}], 'tasks': [], 'last_done': 10,
               'progress_at': time.time() - 3600, 'stall_strikes': 2, 'stall_sample_at': time.time(), 'stalled': True}
        self.journal.save(job)
        self.deluge.data[job['hash']] = {'save_path': '/data/optimization/stuck', 'label': 'media-optimizer',
                                        'state': 'Downloading', 'total_done': 10}
        return job

    def test_stalled_payload_is_removed_only_after_valid_alternative_metadata(self):
        job = self.stalled_job()
        bad = {'hash': 'a' * 40, 'size': 20, 'files': []}
        with patch('media_optimizer.engine.fetch_torrent', return_value=(b'data', bad)):
            self.assertFalse(self.runner.submit(self.src, [(release(), [self.src])], self.deluge.data, job))
        self.assertEqual(self.deluge.removes, [])
        self.assertEqual(self.journal.jobs()[0]['state'], 'downloading')
        good = bad | {'size': 6}
        self.journal.db.execute('DELETE FROM cooldown WHERE key=?', (source_key(self.src),))
        with patch('media_optimizer.engine.fetch_torrent', return_value=(b'data', good)):
            self.assertTrue(self.runner.submit(self.src, [(release(title='another'), [self.src])], self.deluge.data, job))
        self.assertEqual(self.deluge.removes, ['b' * 40])
        self.assertEqual(self.deluge.adds, 1)
        self.assertTrue(self.old.exists())
        self.assertEqual(next(j for j in self.journal.jobs() if j['id']=='stuck')['state'], 'stalled')
        until = self.journal.db.execute('SELECT until FROM rejected WHERE key=?', ('stuck-release',)).fetchone()[0]
        self.assertLess(until-time.time(), 6 * 3600 + 1)

    def test_stalled_download_with_no_alternative_keeps_its_partial_payload(self):
        job = self.stalled_job()
        self.assertFalse(self.runner.submit(self.src, [], self.deluge.data, job))
        self.assertEqual(self.deluge.removes, [])
        self.assertEqual(self.journal.jobs()[0]['state'], 'downloading')

    def test_recent_byte_progress_cancels_stall_replacement(self):
        job = self.stalled_job()
        self.deluge.data[job['hash']]['total_done'] = 11
        with self.assertRaisesRegex(Failure, 'resumed progress'):
            self.runner.yield_stalled(job, self.deluge.data)
        self.assertEqual(self.deluge.removes, [])

    def test_full_concurrency_can_search_when_one_download_is_stalled(self):
        self.stalled_job()
        for i in range(4):
            self.journal.save({'id': str(i), 'state': 'downloading', 'targets': [self.src | {'item_id': i + 10}]})
        self.runner.records = [self.src]
        self.runner.inventory_at = time.time()
        with patch.object(self.runner, 'advance'), patch.object(self.runner, 'find_releases', return_value=(self.src, [])):
            self.runner.tick()
        self.assertIsNotNone(self.runner.search_future)
        self.assertEqual(self.runner.search_replacement, 'stuck')

    def test_lowering_concurrency_drains_existing_jobs_before_stall_admissions(self):
        self.stalled_job()
        self.journal.save({'id': 'other', 'state': 'downloading', 'targets': [self.src | {'item_id': 10}]})
        self.journal.set_setting('concurrency', 1)
        self.runner.records = [self.src]
        self.runner.inventory_at = time.time()
        with patch.object(self.runner, 'advance'), patch.object(self.runner, 'find_releases') as find:
            self.runner.tick()
        find.assert_not_called()

    def test_same_rejected_torrent_from_another_indexer_is_not_downloaded_again(self):
        self.journal.reject('torrent:' + 'a'*40, 'HDR would be lost')
        meta = {'hash': 'a'*40, 'size': 6, 'files': []}
        with patch('media_optimizer.engine.fetch_torrent', return_value=(b'data', meta)):
            self.assertFalse(self.runner.submit(self.src, [(release(indexerId=7), [self.src])], {}))
        self.assertEqual(self.deluge.adds, 0)

    def test_existing_hdr_prefers_hdr_evidence_over_an_unlabelled_codec_bonus(self):
        hdr = release(title='Movie.HDR.H264', customFormatScore=150)
        unknown = release(title='Movie.HEVC', customFormatScore=1000)
        ranked = rank_releases([unknown, hdr], self.src | {'requires_hdr': True}, [], self.config)
        self.assertEqual(ranked[0][0], hdr)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(rank_releases([hdr, unknown], self.src, [], self.config)[0][0], unknown)

    def test_bundled_sample_mapped_to_same_movie_does_not_enter_qa(self):
        root = Path(self.config['stage_host'])/'sample-job'
        root.mkdir()
        sample, feature = root/'Movie.sample.mkv', root/'Movie.mkv'
        sample.write_bytes(b's')
        feature.write_bytes(b'n'*6)
        job = {'id': 'sample-job', 'app': 'radarr', 'state': 'downloading', 'tasks': [], 'targets': [self.src],
               'files': [{'path': sample.name, 'index': 0}, {'path': feature.name, 'index': 1}]}
        resources = [{'path': str(p), 'movie': {'id': 1}} for p in (sample, feature)]
        with patch.object(self.runner, 'resources', return_value=resources):
            self.runner.stage_tasks(job, {'is_finished': True})
        self.assertEqual([x['path'] for x in job['tasks']], [str(feature)])

    def test_stall_preemption_and_lost_add_reply_never_exceed_five_pipeline_slots(self):
        stalled = self.stalled_job()
        stalled['files'] = []
        self.journal.save(stalled)
        for i in range(4):
            jid = 'live' + str(i)
            h = str(i) * 40
            self.journal.save({'id': jid, 'hash': h, 'title': 'Live', 'state': 'downloading', 'files': [],
                               'tasks': [], 'targets': [self.src | {'item_id': i + 10}]})
            self.deluge.data[h] = {'save_path': '/data/optimization/' + jid, 'label': 'media-optimizer',
                                  'state': 'Downloading', 'total_done': 100}
        self.runner.inventory_at = time.time()
        self.runner.search_source = self.src
        self.runner.search_replacement = stalled['id']
        self.runner.search_future = Future()
        self.runner.search_future.set_result((self.src, [(release(), [self.src])]))
        self.deluge.lose_add = True
        meta = {'hash': 'a'*40, 'size': 6, 'files': []}
        with patch('media_optimizer.engine.fetch_torrent', return_value=(b'data', meta)):
            self.runner.tick()
        active = [j for j in self.journal.jobs() if j['state'] in ('submitting', 'downloading', 'verifying', 'importing')]
        self.assertEqual(len(active), 5)
        self.assertEqual(len(self.deluge.data), 5)
        self.runner.tick()
        self.assertEqual(self.deluge.adds, 1)
        self.assertEqual(len([j for j in self.journal.jobs() if j['state']=='downloading']), 5)

    def test_source_subtitle_mapping_requires_uniform_timing_but_video_does_not(self):
        matches = [{'correlation': .98, 'old_minus_new': x} for x in (.02, .06, -.01, .04)]
        self.assertAlmostEqual(qa.consistent_alignment(matches), .0275)
        with self.assertRaisesRegex(Review, 'timing changes'):
            qa.consistent_alignment(matches + [{'correlation': .99, 'old_minus_new': 2}])

    def test_picture_matching_tolerates_a_seek_landing_across_a_scene_cut(self):
        correct = bytes((i % 180) + 30 for i in range(160*90))
        wrong = bytes(reversed(correct))
        with patch.object(qa, 'frame', side_effect=[correct, wrong]), \
                patch.object(qa, 'frame_sequence', return_value=[wrong, correct]):
            _, candidate, score = qa.matching_frame('old', 'new', 600, 600)
        self.assertEqual(candidate, correct)
        self.assertGreater(score, .99)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_decoded_scenes_accept_changed_credits_but_reject_wrong_video(self):
        old = self.root/'test-original.mkv'
        new = self.root/'test-replacement.mkv'
        wrong = self.root/'test-wrong.mkv'
        base = ['ffmpeg', '-v', 'error', '-threads', '2', '-filter_threads', '1']
        subprocess.run(base + ['-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24:duration=24',
                              '-f', 'lavfi', '-i', 'sine=frequency=440:duration=24', '-c:v', 'ffv1',
                              '-c:a', 'aac', '-metadata:s:a:0', 'language=eng', str(old)], check=True)
        subprocess.run(base + ['-i', str(old), '-vf', "drawbox=color=black:t=fill:enable='gte(t,22)'",
                              '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '24', '-threads', '2',
                              '-c:a', 'copy', str(new)], check=True)
        old_probe, new_probe = qa.probe(old), qa.probe(new)
        qa.video(new_probe)['avg_frame_rate'] = '500/21'
        with patch.object(qa, 'probe', side_effect=[old_probe, new_probe]):
            result = qa.verify(str(old), str(new), str(self.root/'real-qa'))
        self.assertEqual(len(result['frame_samples']), 4)
        self.assertAlmostEqual(result['cadence']['replacement']['rate'], 24)
        self.assertAlmostEqual(result['cadence']['replacement']['header_rate'], 500/21)
        self.assertGreater(min(x['correlation'] for x in result['frame_samples']), .9)
        self.assertGreater(result['logical_savings'], 0)
        subprocess.run(base + ['-i', str(old), '-vf', 'drawbox=color=black:t=fill',
                              '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '2',
                              '-c:a', 'copy', str(wrong)], check=True)
        with self.assertRaises(Review):
            qa.verify(str(old), str(wrong), str(self.root/'wrong-qa'))
        self.assertTrue(old.exists())

    def test_enospc_is_an_actual_error_not_a_space_reserve(self):
        self.runner.inventory_at = time.time()
        job = {'id': 'one', 'state': 'downloading', 'title': 'Movie', 'hash': 'a' * 40,
               'release_key': 'release', 'source_key': source_key(self.src), 'targets': [self.src]}
        self.journal.save(job)
        with patch.object(self.runner, 'advance', side_effect=OSError(errno.ENOSPC, 'disk full')):
            self.runner.tick()
        self.assertGreater(self.runner.io_blocked_until, time.time())
        self.assertEqual(self.journal.jobs()[0]['state'], 'downloading')
        self.assertEqual(self.deluge.removes, [])

    def test_subtitle_cues_and_signs_not_full_dialogue(self):
        parsed = qa.cues('1\n00:00:10,000 --> 00:00:12,000\n<i>Hello</i>\n\n')
        self.assertEqual(parsed, [(10, 12, 'Hello')])
        self.assertFalse(qa.full_sub({'tags': {'title': 'English Signs & Songs'}}))

    def test_source_changed_prevents_import(self):
        task = {'state': 'verified', 'source': self.src, 'path': str(self.old), 'verification': {'new_identity': identity(self.old)}}
        self.app.file = {'id': 11, 'path': str(self.old)}
        with self.assertRaises(Review):
            self.runner.import_task({'id': 'one', 'app': 'radarr'}, task)
        self.assertEqual(self.app.posts, [])

    def test_consumer_hardlink_verified_and_original_is_unlinked(self):
        stage = self.root / 'new.mkv'
        library = self.root / 'library.mkv'
        stage.write_bytes(b'n' * 6)
        os.link(stage, library)
        self.app.file = {'id': 11, 'path': str(library)}
        result = {'new_identity': identity(stage), 'resolution': 1080, 'subtitles': [], 'logical_savings': 4}
        task = {'state': 'importing', 'source': self.src, 'path': str(stage), 'verification': result}
        job = {'id': 'one', 'app': 'radarr', 'state': 'downloading', 'tasks': [task]}
        with patch('media_optimizer.engine.qa.probe', return_value={'streams': [{'codec_type': 'video', 'width': 1920}]}):
            self.assertTrue(self.runner.consume_import(job, task))
        self.assertFalse(self.old.exists())
        self.assertEqual(identity(stage)['inode'], identity(library)['inode'])
        self.assertEqual(task['state'], 'imported')
        self.assertEqual(job['logical_savings'], 4)
        self.assertFalse(any('recovery' in str(x) for x in self.root.rglob('*')))

    def test_lost_import_response_records_intent_and_never_resends(self):
        stage = self.root / 'new.mkv'
        stage.write_bytes(b'n' * 6)
        self.app.lose_import = True
        task = {'state': 'verified', 'source': self.src, 'path': str(stage),
                'verification': {'new_identity': identity(stage)}, 'resource': {}}
        job = {'id': 'one', 'app': 'radarr', 'state': 'downloading', 'tasks': [task]}
        self.runner.import_task(job, task)
        self.runner.import_task(job, task)
        self.assertEqual(len(self.app.posts), 1)
        self.assertEqual(self.journal.jobs()[0]['tasks'][0]['state'], 'importing')

    def test_global_concurrency_five_blocks_new_searches(self):
        self.runner.records = [self.src]
        self.runner.inventory_at = time.time()
        for i in range(5):
            self.journal.save({'id': str(i), 'state': 'downloading', 'targets': [self.src | {'item_id': i + 10}]})
        with patch.object(self.runner, 'advance'), patch.object(self.runner, 'find_releases') as find:
            self.runner.tick()
        self.assertIsNone(self.runner.search_future)
        find.assert_not_called()

    def test_api_outage_resets_stall_evidence(self):
        job = {'id': 'one', 'state': 'downloading', 'stall_strikes': 2}
        self.journal.save(job)
        with patch.object(self.deluge, 'torrents', side_effect=Failure('API outage')):
            self.runner.tick()
        self.assertEqual(self.journal.jobs()[0]['stall_strikes'], 0)

    def test_rejected_release_gets_one_alternative_then_cooldown(self):
        job = {'id': 'one', 'state': 'downloading', 'title': 'Movie', 'hash': 'a' * 40,
               'release_key': 'r', 'source_key': source_key(self.src), 'attempt': 1}
        self.runner.fail(job, 'bad mux', {})
        self.assertFalse(self.journal.cooling(source_key(self.src)))
        self.assertEqual(self.journal.setting('retry:' + source_key(self.src), 0), 1)
        job.update(id='two', attempt=2, release_key='r2')
        self.runner.fail(job, 'bad alternative', {})
        self.assertTrue(self.journal.cooling(source_key(self.src)))


if __name__ == '__main__':
    unittest.main()
