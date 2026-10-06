import errno
import json
import os
from pathlib import Path
import random
import tempfile
import time
import unittest
from unittest.mock import patch

from media_optimizer import qa
from media_optimizer.core import (Deluge, Failure, Journal, Review, identity,
                                  load_config, stall_observation, torrent_metadata)
from media_optimizer.engine import Runner, rank_releases, source_key


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

    def test_no_quota_or_reserve_config(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.config))
        self.assertEqual(load_config(path)['concurrency'], 5)
        for key in ('daily_bytes', 'speed_limit', 'free_floor_bytes', 'original_retention_days'):
            path.write_text(json.dumps(self.config | {key: 1}))
            with self.assertRaises(Failure):
                load_config(path)

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

    def test_wrong_movie_resolution_language_and_artificial_frames_rejected(self):
        cases = [release(mappedMovieId=2), release(quality={'quality': {'resolution': 720}}),
                 release(rejections=['English is wanted, but found French']),
                 release(title='Movie.Ai-Enhanced.RIFE.60fps')]
        self.assertFalse(rank_releases(cases, self.src, [], self.config))
        allowed = release(rejections=['Existing file and the Quality profile does not allow upgrades'])
        self.assertTrue(rank_releases([allowed], self.src, [], self.config))

    def test_native_season_pack_and_exact_episode_mapping(self):
        src = self.src | {'app': 'sonarr', 'series_id': 20, 'season': 3, 'episode_ids': [100]}
        other = src | {'episode_ids': [101], 'item_id': 101}
        pack = release(mappedSeriesId=20, fullSeason=True, seasonNumber=3, size=10)
        single = release(mappedSeriesId=20, mappedEpisodeInfo=[{'id': 100}])
        ranked = rank_releases([single, pack], src, [src, other], self.config)
        self.assertTrue(ranked[0][0]['fullSeason'])
        self.assertEqual(len(ranked[0][1]), 2)
        self.assertFalse(rank_releases([pack | {'seasonNumber': 4}], src, [src, other], self.config))
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

    def test_dv_profile5_and_missing_native_audio_rejected(self):
        v = {'codec_type': 'video', 'width': 1920, 'height': 1080, 'codec_name': 'hevc', 'avg_frame_rate': '24/1'}
        eng = {'codec_type': 'audio', 'channels': 2, 'tags': {'language': 'eng'}}
        jp = eng | {'tags': {'language': 'jpn'}}
        old = {'streams': [v, eng, jp]}
        with self.assertRaises(Review):
            qa.validate_streams(old, {'streams': [v, eng]}, 'jpn', True)
        with self.assertRaises(Review):
            qa.validate_streams(old, {'streams': [v | {'side_data_list': [{'dv_profile': 5}]}, eng, jp]}, 'jpn', True)

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
