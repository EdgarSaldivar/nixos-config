import json
from pathlib import Path
from io import BytesIO
import tempfile
import unittest
from unittest.mock import patch

from media_optimizer import qa, subtitles
from media_optimizer.core import Failure, Review


class SubtitleTests(unittest.TestCase):
    def test_hdr_unknown_is_distinct_and_only_actual_av1_can_use_sdr_fallback(self):
        a = {'codec_type': 'audio', 'channels': 2}
        v = {'codec_type': 'video', 'width': 1920, 'codec_name': 'hevc', 'pix_fmt': 'yuv420p10le'}
        old = {'streams': [v | {'color_transfer': 'smpte2084'}, a]}
        sdr = {'streams': [v | {'color_transfer': 'bt709'}, a]}
        unknown = {'streams': [v, a]}
        self.assertEqual(qa.hdr_state(unknown), 'unknown')
        with self.assertRaisesRegex(Review, 'not established'):
            qa.validate_streams(old, unknown, 'eng', False, True)
        with self.assertRaisesRegex(Review, 'HDR would be lost'):
            qa.validate_streams(old, sdr, 'eng', False, True)
        av1 = {'streams': [old['streams'][0] | {'codec_name': 'av1'}, a]}
        with self.assertRaisesRegex(Review, 'HDR would be lost'):
            qa.validate_streams(av1, sdr, 'eng', False)
        qa.validate_streams(av1, sdr, 'eng', False, True)

    def test_missing_hdr_tags_are_resolved_only_by_consistent_frame_evidence(self):
        data = {'streams': [{'codec_type': 'video', 'pix_fmt': 'yuv420p10le'}], 'format': {'duration': '1400'}}
        frame = {'color_transfer': 'smpte2084', 'color_primaries': 'bt2020', 'color_space': 'bt2020nc'}
        with patch.object(qa, 'run', return_value=json.dumps({'frames': [frame]}).encode()):
            evidence = qa.frame_color_evidence('new', data)
        self.assertEqual(evidence['state'], 'hdr')
        self.assertTrue(qa.hdr(data))
        unknown = {'streams': [{'codec_type': 'video', 'pix_fmt': 'yuv420p10le'}], 'format': {'duration': '1400'}}
        for frames in ([{}], [frame, frame | {'color_transfer': 'bt709'}]):
            with patch.object(qa, 'run', return_value=json.dumps({'frames': frames}).encode()):
                self.assertEqual(qa.frame_color_evidence('new', unknown)['state'], 'unknown')
            self.assertEqual(qa.hdr_state(unknown), 'unknown')

    def test_scene_search_hints_cover_duration_delta_and_prior_scene_offset(self):
        cadence = {'original': {'rate': 24}, 'replacement': {'rate': 24}}
        predictions = qa.scene_predictions(400, 1864, 1770, cadence, {'at': 280, 'new_at': 189})
        self.assertIn(306, predictions)
        self.assertEqual(predictions[0], 309)
        self.assertTrue(all(0 <= p <= 1766 for p in predictions))

    def test_dolby_hdr_fallback_rejects_conflicting_color_signaling(self):
        audio = {'codec_type': 'audio', 'channels': 2}
        base = {'codec_type': 'video', 'width': 1920, 'codec_name': 'hevc',
                'color_transfer': 'smpte2084', 'color_primaries': 'bt2020', 'color_space': 'bt2020nc'}
        old = {'streams': [base | {'color_transfer': 'bt709'}, audio]}
        for profile, fallback in ((8, 1), (7, 6)):
            dv = [{'dv_profile': profile, 'dv_bl_signal_compatibility_id': fallback}]
            for key in ('color_transfer', 'color_primaries', 'color_space'):
                with self.subTest(profile=profile, key=key), self.assertRaisesRegex(Review, 'color signaling'):
                    qa.validate_streams(old, {'streams': [base | {key: 'bt709', 'side_data_list': dv}, audio]}, 'eng', False)

    def test_valid_or_unspecified_dolby_color_signaling_and_plain_sdr_remain_eligible(self):
        audio = {'codec_type': 'audio', 'channels': 2}
        base = {'codec_type': 'video', 'width': 1920, 'codec_name': 'hevc'}
        old = {'streams': [base, audio]}
        variants = [base | {'color_transfer': 'bt709', 'color_primaries': 'bt709', 'color_space': 'bt709'}]
        for profile, fallback in ((8, 1), (7, 6)):
            dv = [{'dv_profile': profile, 'dv_bl_signal_compatibility_id': fallback}]
            variants.extend([base | {'side_data_list': dv},
                             base | {'side_data_list': dv, 'color_transfer': 'unknown', 'color_primaries': 'unspecified'},
                             base | {'side_data_list': dv, 'color_transfer': 'smpte2084',
                                     'color_primaries': 'bt2020', 'color_space': 'bt2020nc'}])
        variants.extend([base | {'side_data_list': [{'dv_profile': 8, 'dv_bl_signal_compatibility_id': 4}],
                                 'color_transfer': 'arib-std-b67', 'color_primaries': 'bt2020', 'color_space': 'bt2020nc'},
                         base | {'side_data_list': [{'dv_profile': 9, 'dv_bl_signal_compatibility_id': 2}],
                                 'color_transfer': 'bt709', 'color_primaries': 'bt709', 'color_space': 'bt709'}])
        for stream in variants:
            with self.subTest(stream=stream):
                qa.validate_streams(old, {'streams': [stream, audio]}, 'eng', False)

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

    def verify_fixture(self, root, old_audio, new_audio, subs=None, match=None, new_rate='24/1', new_duration='1400'):
        old_path, new_path = Path(root) / 'old.mkv', Path(root) / 'new.mkv'
        old_path.write_bytes(b'o' * 100)
        new_path.write_bytes(b'n' * 60)
        v = {'codec_type': 'video', 'width': 1920, 'codec_name': 'hevc', 'avg_frame_rate': '24/1'}
        old = {'streams': [v, old_audio] + (subs or []), 'format': {'duration': '1400'}}
        new = {'streams': [v | {'avg_frame_rate': new_rate}, new_audio], 'format': {'duration': new_duration}}
        match = match or {'correlation': .99, 'old_minus_new': 0}
        with patch.object(qa, 'probe', side_effect=[old, new]), \
                patch.object(qa, 'measure_frame_rate', side_effect=lambda path, data: {'rate': qa.frame_rate(data)}), \
                patch.object(qa, 'envelope', return_value=[]), \
                patch.object(qa, 'align', **({'side_effect': match} if isinstance(match, list) else {'return_value': match})), \
                patch.object(qa, 'frame', return_value=b''), \
                patch.object(qa, 'frame_similarity', return_value=.99), patch.object(qa, 'run', return_value=b''), \
                patch.object(qa, 'subtitle', side_effect=Review('unsupported subtitle sample')), \
                patch.object(qa, 'picture_alignment', return_value=([match] * 4, 0)) as pictures:
            result = qa.verify(str(old_path), str(new_path), str(Path(root) / 'qa'), native='jpn', anime=True)
            return result, pictures.called

    def test_weak_audio_mix_does_not_block_matching_pictures_and_preserves_evidence(self):
        a = {'codec_type': 'audio', 'index': 1, 'channels': 2, 'tags': {'language': 'jpn'}}
        low = {'correlation': .2, 'old_minus_new': 0}
        with tempfile.TemporaryDirectory() as root:
            result, pictures = self.verify_fixture(root, a, a, match=low)
            self.assertFalse(pictures)
            self.assertEqual(result['alignment_method'], 'independent local picture correspondence')
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

    def test_frame_rate_change_does_not_invent_a_subtitle_speed_change(self):
        a = {'codec_type': 'audio', 'index': 1, 'channels': 2, 'tags': {'language': 'jpn'}}
        with tempfile.TemporaryDirectory() as root:
            result, _ = self.verify_fixture(root, a, a, new_rate='24000/1001')
        self.assertEqual(result['timeline_scale'], 1)
        with tempfile.TemporaryDirectory() as root:
            result, _ = self.verify_fixture(root, a, a, new_rate='30/1')
        self.assertEqual(result['timeline_scale'], 1)

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
            self.assertFalse(pictures)
            self.assertEqual(result['alignment_method'], 'independent local picture correspondence')
            self.assertIn({'language': 'eng', 'change': 'audio language absent'}, result['audio_tradeoffs'])

    def test_picture_correspondence_still_requires_matching_content(self):
        reference = bytes((i % 180) + 30 for i in range(160 * 90))
        with patch.object(qa, 'frame', return_value=reference), \
                patch.object(qa, 'frame_sequence', return_value=[reference]), \
                patch.object(qa, 'frame_similarity', return_value=.99):
            matches, offset = qa.picture_alignment('old', 'new', [90, 600, 1200])
            self.assertEqual(len(matches), 3)
            self.assertAlmostEqual(offset, 45.3)
        with patch.object(qa, 'frame', return_value=reference), \
                patch.object(qa, 'frame_sequence', return_value=[reference]), \
                patch.object(qa, 'frame_similarity', return_value=.1), \
                patch.object(qa, 'picture_similarity', return_value=.1):
            with self.assertRaisesRegex(Review, 'picture content'):
                qa.picture_alignment('old', 'new', [90, 600, 1200])

    def test_untagged_audio_hint_is_recorded_and_weak_hint_does_not_reject_pictures(self):
        old_audio = {'codec_type': 'audio', 'index': 1, 'channels': 2, 'tags': {'language': 'jpn'}}
        new_audio = {'codec_type': 'audio', 'index': 2, 'channels': 2}
        with tempfile.TemporaryDirectory() as root:
            result, _ = self.verify_fixture(root, old_audio, new_audio,
                                            match={'correlation': .2, 'old_minus_new': 0})
            evidence = json.loads((Path(root)/'qa'/'audio-evidence.json').read_text())
        self.assertTrue(evidence['untagged_search_hint'])
        self.assertEqual(evidence['replacement_stream'], 2)
        self.assertEqual(len(result['frame_samples']), 4)
        self.assertNotIn('tags', new_audio)

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

    def test_local_scene_matches_accept_different_cuts_without_a_global_offset(self):
        def similarity(reference, candidate):
            shift = 1 if reference < 900 else 2
            return .999 if abs(candidate - reference - shift) < .01 else .1
        def sequence(path, start, duration, rate):
            return [start + i/rate for i in range(round(duration*rate))]
        with patch.object(qa, 'frame', side_effect=lambda path, at: at), \
                patch.object(qa, 'frame_sequence', side_effect=sequence), \
                patch.object(qa, 'frame_similarity', side_effect=similarity):
            matches, offset = qa.picture_alignment('old', 'new', [90, 600, 1200])
        self.assertIsNone(offset)
        self.assertGreater(min(m['correlation'] for m in matches), .98)

    def test_subtitle_mapping_comes_from_scenes_and_cut_changes_need_background_repair(self):
        times = [200, 600, 900, 1200]
        speed = [{'at': at, 'new_at': at * 1.001 - 2, 'correlation': .999} for at in times]
        mapping = qa.subtitle_timeline(speed)
        self.assertAlmostEqual(mapping['scale'], 1.001)
        self.assertAlmostEqual(mapping['offset'], 2)
        cuts = [{'at': at, 'new_at': at - shift, 'correlation': .999}
                for at, shift in zip(times, [.89, .89, 2.39, 2.39])]
        self.assertIsNone(qa.subtitle_timeline(cuts))
        self.assertIsNone(qa.subtitle_timeline([speed[0], speed[2], speed[1], speed[3]]))

    def test_uninformative_source_picture_gets_a_nearby_reference(self):
        with patch.object(qa, 'matching_frame', side_effect=[Review('dark reference'), (b'a', b'b', .999)]), \
                patch.object(qa, 'picture_alignment', side_effect=Review('dark reference')):
            sample, before, after = qa.content_sample('old', 'new', 600, 598, 1400)
        self.assertEqual(sample['at'], 602)
        self.assertEqual(sample['new_at'], 600)
        self.assertEqual((before, after), (b'a', b'b'))

    def test_playback_speed_hints_only_pass_when_pictures_match(self):
        def match(old, new, at, predicted):
            return b'a', b'b', .999 if abs(predicted - at * 25/24) < .01 else .1
        with patch.object(qa, 'matching_frame', side_effect=match):
            sample, _, _ = qa.content_sample('old', 'new', 6000, 6000, 7200, [6000 * 25/24])
        self.assertAlmostEqual(sample['new_at'], 6250)
        with patch.object(qa, 'matching_frame', return_value=(b'a', b'b', .1)), \
                patch.object(qa, 'picture_alignment', side_effect=Review('wrong program')):
            with self.assertRaisesRegex(Review, 'program content'):
                qa.content_sample('old', 'new', 6000, 6000, 7200, [6250])

    def test_runtime_difference_is_diagnostic_when_program_content_matches(self):
        a = {'codec_type': 'audio', 'index': 1, 'channels': 2, 'tags': {'language': 'jpn'}}
        with tempfile.TemporaryDirectory() as root:
            result, _ = self.verify_fixture(root, a, a, new_duration='1600')
        self.assertEqual(len(result['frame_samples']), 4)
        self.assertEqual(result['content_notes'][0]['replacement_seconds'], 1600)

    def test_full_verification_accepts_edit_shifts_without_installing_old_subtitles(self):
        a = {'codec_type': 'audio', 'index': 1, 'channels': 2, 'tags': {'language': 'jpn'}}
        subs = [{'codec_type': 'subtitle', 'codec_name': 'ass', 'index': 2, 'tags': {'language': 'eng'}}]
        matches = [{'correlation': .99, 'old_minus_new': x} for x in (.89, 2.39, 2.39, 2.39)]
        with tempfile.TemporaryDirectory() as root:
            result, _ = self.verify_fixture(root, a, a, subs=subs, match=matches)
            self.assertFalse(result['source_subtitle_timeline_usable'])
            self.assertIsNone(result['timeline_scale'])
            self.assertEqual(result['subtitles'], [])
            self.assertIn('background repair', result['subtitle_warnings'][0]['issue'])

    def test_subtitle_install_failure_is_reported_without_failing_video_import(self):
        result = {'subtitles': [{'path': '/absent.srt', 'language': 'eng', 'stream': 2, 'sdh': False}]}
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(qa.install_subtitles(result, str(Path(root) / 'movie.mkv')), [])
            self.assertEqual(result['subtitle_missing'], ['eng'])
            self.assertTrue(result['subtitle_warnings'])
