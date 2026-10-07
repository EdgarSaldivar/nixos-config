import json
from pathlib import Path
from io import BytesIO
import tempfile
import unittest
from unittest.mock import patch

from media_optimizer import qa, subtitles
from media_optimizer.core import Failure, Review


class SubtitleTests(unittest.TestCase):
    def test_bridge_strips_supplied_keys_and_cannot_proxy_arbitrary_routes(self):
        app = type('App', (), {'url': 'http://127.0.0.1:7878', 'key': 'SECRET'})()
        apps = {'radarr': app}
        _, url = subtitles.bridge_target('/radarr/api/v3/movie?apikey=SECRET&id=2', apps)
        self.assertEqual(url, 'http://127.0.0.1:7878/api/v3/movie?id=2')
        for path in ('/radarr/api/v3/config/host', '/radarr/api/v3/command',
                     '/radarr/api/v3/system/backup', '/radarr/api/v3/system/backup/download',
                     '/radarr/api/v3/../movie', '/radarr/api/v3/movie/%2e%2e/config',
                     '/other/api/v3/movie'):
            with self.subTest(path=path), self.assertRaises(Failure):
                subtitles.bridge_target(path, apps)

    def test_seed_has_no_upstream_keys_and_preserves_existing_settings(self):
        with tempfile.TemporaryDirectory() as root:
            config = {'subtitles': {'bridge_port': 18787, 'instances': {'anime': {'port': 16768, 'state_dir': root}}}}
            subtitles.seed(config, 'anime')
            target = Path(root) / 'config/config.yaml'
            data = json.loads(target.read_text())
            self.assertEqual(data['sonarr']['base_url'], '/animearr')
            self.assertEqual(data['sonarr']['apikey'], subtitles.LOCAL_TOKEN)
            self.assertFalse(data['general']['use_radarr'])
            self.assertEqual(data['general']['concurrent_jobs'], 1)
            target.write_text('{}')
            subtitles.seed(config, 'anime')
            self.assertEqual(target.read_text(), '{}')

    def test_profiles_prefer_english_plus_native_without_audio_exclusion(self):
        profiles = subtitles.profiles()
        self.assertEqual([p['items'][-1]['language'] for p in profiles], ['en', 'ja', 'ko', 'zh'])
        self.assertTrue(all(p['cutoff'] is None for p in profiles))
        self.assertTrue(all(x['audio_exclude'] == 'False' for p in profiles for x in p['items']))
        self.assertTrue(all(x['audio_only_include'] == 'False' for p in profiles for x in p['items']))

    def test_existing_api_profiles_are_idempotent_and_native_assignment_is_applied(self):
        spec = {'port': 16768}
        config = {'subtitles': {'bridge_port': 18787, 'instances': {'anime': spec}}}
        current = subtitles.profiles()
        for profile in current:
            profile['originalFormat'] = 1  # Bazarr serializes its boolean as an integer.
        calls = []
        def request(_spec, endpoint, body=None):
            calls.append((endpoint, body))
            if body is not None:
                return None
            return {'system/languages/profiles': current,
                    'system/settings': {'general': {'enabled_providers': subtitles.PROVIDERS}},
                    'series': {'data': [{'sonarrSeriesId': 99, 'profileId': 1}]}}[endpoint]
        titles = [{'id': 99, 'originalLanguage': {'name': 'Japanese'}}]
        with patch.object(subtitles, 'bazarr_request', side_effect=request), \
                patch.object(subtitles, 'urlopen', return_value=BytesIO(json.dumps(titles).encode())):
            subtitles.setup(config, 'anime')
        self.assertFalse(any(endpoint == 'system/settings' and body is not None for endpoint, body in calls))
        self.assertIn(('series', {'seriesid': [99], 'profileid': [2]}), calls)

    def verify_fixture(self, root, old_audio, new_audio, subs=None, match=None, new_rate='24/1'):
        old_path, new_path = Path(root) / 'old.mkv', Path(root) / 'new.mkv'
        old_path.write_bytes(b'o' * 100)
        new_path.write_bytes(b'n' * 60)
        v = {'codec_type': 'video', 'width': 1920, 'codec_name': 'hevc', 'avg_frame_rate': '24/1'}
        old = {'streams': [v, old_audio] + (subs or []), 'format': {'duration': '1400'}}
        new = {'streams': [v | {'avg_frame_rate': new_rate}, new_audio], 'format': {'duration': '1400'}}
        match = match or {'correlation': .99, 'old_minus_new': 0}
        with patch.object(qa, 'probe', side_effect=[old, new]), \
                patch.object(qa, 'measure_frame_rate', side_effect=lambda path, data: {'rate': qa.frame_rate(data)}), \
                patch.object(qa, 'envelope', return_value=[]), \
                patch.object(qa, 'align', return_value=match), patch.object(qa, 'frame', return_value=b''), \
                patch.object(qa, 'frame_similarity', return_value=.99), patch.object(qa, 'run', return_value=b''), \
                patch.object(qa, 'subtitle', side_effect=Review('unsupported subtitle sample')), \
                patch.object(qa, 'picture_alignment', return_value=([match] * 4, 0)) as pictures:
            result = qa.verify(str(old_path), str(new_path), str(Path(root) / 'qa'), native='jpn', anime=True)
            return result, pictures.called

    def test_weak_audio_mix_uses_picture_evidence_and_preserves_failed_audio_evidence(self):
        a = {'codec_type': 'audio', 'index': 1, 'channels': 2, 'tags': {'language': 'jpn'}}
        low = {'correlation': .2, 'old_minus_new': 0}
        with tempfile.TemporaryDirectory() as root:
            result, pictures = self.verify_fixture(root, a, a, match=low)
            self.assertTrue(pictures)
            self.assertEqual(result['alignment_method'], 'pictures; audio mix could not establish timing')
            evidence = json.loads((Path(root)/'qa'/'audio-evidence.json').read_text())
            self.assertEqual(len(evidence['alignment']), 4)
            self.assertEqual(evidence['alignment'][0]['correlation'], .2)

    def test_scene_checks_span_program_and_do_not_require_matching_credits(self):
        a = {'codec_type': 'audio', 'index': 1, 'channels': 2, 'tags': {'language': 'jpn'}}
        with tempfile.TemporaryDirectory() as root:
            result, _ = self.verify_fixture(root, a, a)
        samples = [x['at'] for x in result['frame_samples']]
        self.assertTrue(any(600 < x < 1000 for x in samples))
        self.assertTrue(any(1000 < x < 1250 for x in samples))
        self.assertFalse(any(x >= 1300 for x in samples))

    def test_weak_audio_scene_is_resampled_without_lowering_confidence_gate(self):
        good = {'correlation': .99, 'old_minus_new': -.03}
        weak = {'correlation': .82, 'old_minus_new': -.03}
        for retry, passes in ((good, True), (good | {'old_minus_new': 1}, False), (weak, False)):
            with self.subTest(retry=retry), tempfile.TemporaryDirectory() as root, \
                    patch.object(qa, 'envelope', return_value=[]), patch.object(qa, 'align', return_value=retry):
                results = qa.resample_weak_audio('old', 'new', 1, 2, [200, 600, 900, 1200],
                    [good, weak, good, good], 1, 1400, Path(root) / 'evidence.json')
                if passes:
                    self.assertAlmostEqual(qa.consistent_alignment(results), -.03)
                else:
                    with self.assertRaises(Review):
                        qa.consistent_alignment(results)
                evidence = json.loads((Path(root) / 'evidence.json').read_text())
                self.assertEqual(evidence['retries'][0]['at'], 615)

    def test_small_cadence_change_has_an_explicit_subtitle_timeline_scale(self):
        a = {'codec_type': 'audio', 'index': 1, 'channels': 2, 'tags': {'language': 'jpn'}}
        with tempfile.TemporaryDirectory() as root:
            result, _ = self.verify_fixture(root, a, a, new_rate='24000/1001')
        self.assertAlmostEqual(result['timeline_scale'], 1.001)

    def test_missing_or_unextractable_subtitles_do_not_block_replacement(self):
        a = {'codec_type': 'audio', 'index': 1, 'channels': 2, 'tags': {'language': 'jpn'}}
        for subs in ([], [{'codec_type': 'subtitle', 'codec_name': 'dvd_subtitle', 'index': 2, 'tags': {'language': 'eng'}}]):
            with self.subTest(subs=subs), tempfile.TemporaryDirectory() as root:
                result, pictures = self.verify_fixture(root, a, a, subs)
                self.assertEqual(result['subtitle_missing'], ['eng', 'jpn'])
                self.assertEqual(bool(result['subtitle_warnings']), bool(subs))
                self.assertFalse(pictures)

    def test_existing_styled_sidecar_survives_video_filename_change(self):
        a = {'codec_type': 'audio', 'index': 1, 'channels': 2, 'tags': {'language': 'jpn'}}
        with tempfile.TemporaryDirectory() as root:
            sidecar = Path(root) / 'old.eng.ass'
            styled = '[Script Info]\nTitle: Styled dialogue\n[V4+ Styles]\nStyle: Default,Example Font,40\n[Events]\nDialogue: 0,0:00:01.00,0:00:05.00,Default,,0,0,0,,{\\b1}Hello'
            sidecar.write_text(styled)
            result, _ = self.verify_fixture(root, a, a)
            paths = qa.install_subtitles(result, str(Path(root) / 'replacement.mkv'))
            self.assertEqual(len(paths), 1)
            self.assertEqual(Path(paths[0]).suffix, '.ass')
            self.assertEqual(Path(paths[0]).read_text(), styled)
            self.assertNotIn('eng', result['subtitle_missing'])

    def test_different_dubs_use_picture_alignment_instead_of_audio_language_gate(self):
        a = {'codec_type': 'audio', 'index': 1, 'channels': 8, 'tags': {'language': 'eng'}}
        b = a | {'channels': 2, 'tags': {'language': 'jpn'}}
        with tempfile.TemporaryDirectory() as root:
            result, pictures = self.verify_fixture(root, a, b)
            self.assertTrue(pictures)
            self.assertEqual(result['alignment_method'], 'pictures; different audio languages')
            self.assertIn({'language': 'eng', 'change': 'audio language absent'}, result['audio_tradeoffs'])

    def test_picture_timing_requires_matching_frames_at_consistent_offsets(self):
        reference = bytes((i % 180) + 30 for i in range(160 * 90))
        with patch.object(qa, 'frame', return_value=reference), \
                patch.object(qa, 'frame_sequence', return_value=[reference]), \
                patch.object(qa, 'frame_similarity', return_value=.99):
            matches, offset = qa.picture_alignment('old', 'new', [90, 600, 1200])
            self.assertEqual(len(matches), 3)
            self.assertAlmostEqual(offset, 45.3)
        with patch.object(qa, 'frame', return_value=reference), \
                patch.object(qa, 'frame_sequence', return_value=[reference]), \
                patch.object(qa, 'frame_similarity', return_value=.1):
            with self.assertRaisesRegex(Review, 'timing evidence'):
                qa.picture_alignment('old', 'new', [90, 600, 1200])

    def test_held_animation_frames_prefer_near_timestamp_over_tiny_score_difference(self):
        def similarity(reference, candidate):
            delta = candidate - reference
            if abs(delta) < .15:
                return .999
            if abs(delta - 3) < .15:
                return .9999
            return .1
        def sequence(path, start, duration, rate):
            return [start + i/rate for i in range(round(duration*rate))]
        with patch.object(qa, 'frame', side_effect=lambda path, at: at), \
                patch.object(qa, 'frame_sequence', side_effect=sequence), \
                patch.object(qa, 'frame_similarity', side_effect=similarity):
            matches, offset = qa.picture_alignment('old', 'new', [90, 600, 1200])
        self.assertLess(abs(offset), .04)
        self.assertGreater(min(m['correlation'] for m in matches), .99)

    def test_real_timing_change_still_fails_with_static_frame_tiebreaker(self):
        def similarity(reference, candidate):
            shift = 1 if reference < 900 else 2
            return .999 if abs(candidate - reference - shift) < .01 else .1
        def sequence(path, start, duration, rate):
            return [start + i/rate for i in range(round(duration*rate))]
        with patch.object(qa, 'frame', side_effect=lambda path, at: at), \
                patch.object(qa, 'frame_sequence', side_effect=sequence), \
                patch.object(qa, 'frame_similarity', side_effect=similarity):
            with self.assertRaisesRegex(Review, 'timing changes'):
                qa.picture_alignment('old', 'new', [90, 600, 1200])

    def test_subtitle_install_failure_is_reported_without_failing_video_import(self):
        result = {'subtitles': [{'path': '/absent.srt', 'language': 'eng', 'stream': 2, 'sdh': False}]}
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(qa.install_subtitles(result, str(Path(root) / 'movie.mkv')), [])
            self.assertEqual(result['subtitle_missing'], ['eng'])
            self.assertTrue(result['subtitle_warnings'])
