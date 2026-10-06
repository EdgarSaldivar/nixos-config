import errno
import json
from io import BytesIO
import os
from pathlib import Path
import random
import tempfile
import time
import unittest
from unittest.mock import patch

from media_optimizer import qa
from media_optimizer.core import (Deluge, Failure, Journal, Review, atomic_json, fetch_torrent, identity,
                                  load_config, stall_observation, torrent_metadata)
from media_optimizer.engine import Runner, inventory, rank_releases, runtime_minutes, source_key


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

    def test_stall_needs_twelve_hours_three_spaced_samples_and_resets(self):
        job = {'last_done': 10, 'progress_at': 0, 'stall_sample_at': 0}
        torrent = {'state': 'Downloading', 'total_done': 10}
        self.assertFalse(stall_observation(job, torrent, 43199))
        self.assertFalse(stall_observation(job, torrent, 43200))
        self.assertFalse(stall_observation(job, torrent, 43300))
        self.assertFalse(stall_observation(job, torrent, 45000))
        self.assertTrue(stall_observation(job, torrent, 46800))
        self.assertFalse(stall_observation(job, torrent | {'total_done': 11}, 46801))
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

    def test_consistent_but_wrong_native_dub_offset_fails_before_import(self):
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
        with patch('media_optimizer.qa.probe', return_value=data), patch('media_optimizer.qa.envelope', return_value=[]), \
                patch('media_optimizer.qa.align', side_effect=[zero] * 3 + [bad] * 3):
            with self.assertRaisesRegex(Review, 'out of sync'):
                qa.verify(str(old_path), str(new_path), str(self.root / 'qa'), native='jpn', anime=True)
        self.assertTrue(old_path.exists())

    def test_enospc_is_an_actual_error_not_a_space_reserve(self):
        self.runner.inventory_at = time.time()
        job = {'id': 'one', 'state': 'downloading', 'title': 'Movie', 'hash': 'a' * 40,
               'release_key': 'release', 'source_key': source_key(self.src), 'targets': [self.src]}
        self.journal.save(job)
        with patch.object(self.runner, 'advance', side_effect=OSError(errno.ENOSPC, 'disk full')):
            self.runner.tick()
        self.assertGreater(self.runner.io_blocked_until, time.time())
        self.assertEqual(self.journal.jobs()[0]['state'], 'failed')

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
