"""Decode, stream, timing and subtitle evidence before replacing a library file."""
import array
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import statistics
import subprocess

from .core import Failure, Review, atomic_json, identity

LANG = {'en': 'eng', 'english': 'eng', 'ja': 'jpn', 'jp': 'jpn', 'japanese': 'jpn',
        'ko': 'kor', 'kr': 'kor', 'korean': 'kor', 'zh': 'zho', 'cn': 'zho', 'chi': 'zho', 'chinese': 'zho'}

AV1_PATTERN = re.compile(r'(?i)(?<![A-Za-z0-9])(?:AV[ ._-]?1|AV01|AOM)(?![A-Za-z0-9])')


def is_av1(value):
    return bool(AV1_PATTERN.search(str(value or '')))


def language(stream):
    tag = stream.get('tags', {}).get('language', 'und').lower()
    return LANG.get(tag, tag)


def run(args, timeout=240):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Review('media sample decode timeout') from None
    if result.returncode:
        raise Review('media sample decode failed')
    return result.stdout


def probe(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))


def video(data):
    return next(s for s in data['streams'] if s.get('codec_type') == 'video'
                and not s.get('disposition', {}).get('attached_pic'))


def resolution(data):
    # Scope cropped 3840x1600 cinema encodes correctly; don't rely on height alone.
    width = video(data).get('width', 0)
    return 2160 if width >= 3000 else 1080 if width >= 1600 else 720 if width >= 1200 else 480


def audio(data):
    return [s for s in data['streams'] if s.get('codec_type') == 'audio'
            and not s.get('disposition', {}).get('comment')
            and not re.search('commentary|description|descriptive', s.get('tags', {}).get('title', ''), re.I)]


def audio_search_pairs(old_audio, new_audio, native):
    common = {language(s) for s in old_audio} & {language(s) for s in new_audio}
    preferred = native if native in common else 'eng' if 'eng' in common else next(iter(sorted(common)), None)
    languages = ([preferred] if preferred else []) + sorted((common & {'eng', native}) - {preferred})
    pairs = [(lang, next(s for s in old_audio if language(s) == lang),
              next(s for s in new_audio if language(s) == lang), False) for lang in languages]
    if not pairs and old_audio and new_audio:
        # A sole untagged main track is an optional search hint, not a language
        # assertion. Different dubs/mixes can fail correlation without rejection.
        def main(streams):
            return min(streams, key=lambda s: (language(s) != native, language(s) != 'eng',
                                               not s.get('disposition', {}).get('default')))
        if (len(new_audio) == 1 and language(new_audio[0]) == 'und') or (
                len(old_audio) == 1 and language(old_audio[0]) == 'und'):
            pairs.append(('untagged-main', main(old_audio), main(new_audio), True))
    return pairs


def hdr(data):
    return video(data).get('color_transfer') in ('smpte2084', 'arib-std-b67')


def hdr_state(data):
    transfer = video(data).get('color_transfer')
    if hdr(data):
        return 'hdr'
    if transfer in ('bt709', 'smpte170m', 'bt470bg', 'gamma22', 'gamma28', 'iec61966-2-1'):
        return 'sdr'
    return 'unknown'


def frame_color_evidence(path, data):
    """Resolve missing container tags from decoded frames, never from bit depth."""
    observations = []
    duration = float(data['format']['duration'])
    for at in (duration * .15, duration * .65):
        frames = json.loads(run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-read_intervals', str(at) + '%+2', '-show_frames', '-show_entries',
            'frame=color_transfer,color_primaries,color_space', '-of', 'json', str(path)]))
        observations += frames.get('frames', [])
    evidence = {'frames': observations, 'state': 'unknown'}
    states = {hdr_state({'streams': [dict(frame, codec_type='video')]}) for frame in observations}
    if len(states) == 1 and 'unknown' not in states:
        evidence['state'] = next(iter(states))
        for key in ('color_transfer', 'color_primaries', 'color_space'):
            values = {frame.get(key) for frame in observations}
            if len(values) == 1 and next(iter(values)) not in (None, 'unknown', 'unspecified'):
                video(data)[key] = next(iter(values))
    return evidence


def atmos(stream):
    return bool(re.search('atmos', str(stream.get('profile', '')) + str(stream.get('tags', {})), re.I))


def frame_rate(data):
    if data.get('measured_frame_rate'):
        return data['measured_frame_rate']
    stream = video(data)
    numerator, denominator = stream.get('avg_frame_rate', stream.get('r_frame_rate', '0/1')).split('/')
    return float(numerator) / max(float(denominator), 1)


def measure_frame_rate(path, data):
    """Packet presentation times override rounded/misleading container rates."""
    duration = float(data['format']['duration'])
    rates = []
    for fraction in (.15, .4, .65, .85):
        packets = json.loads(run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-read_intervals', str(duration * fraction) + '%+8', '-show_packets',
            '-show_entries', 'packet=pts_time', '-of', 'json', str(path)]))
        times = sorted({float(p['pts_time']) for p in packets.get('packets', []) if 'pts_time' in p})
        if len(times) < 3 or times[-1] <= times[0]:
            raise Review('insufficient frame timestamp evidence')
        rates.append((len(times) - 1) / (times[-1] - times[0]))
    measured = statistics.median(rates)
    standards = [12, 15, 18, 20, 24000 / 1001, 24, 25, 30000 / 1001, 30,
                 48, 50, 60000 / 1001, 60, 100, 120000 / 1001, 120]
    nearest = min(standards, key=lambda rate: abs(rate - measured))
    # Millisecond mux timestamps introduce tiny sample-window rounding errors.
    nominal = nearest if abs(nearest - measured) < .012 else measured
    return {'header_rate': frame_rate(data), 'sample_rates': rates, 'rate': nominal}


def validate_streams(old, new, native, anime, allow_sdr_remediation=False):
    ov, nv = video(old), video(new)
    if resolution(new) < resolution(old) or nv.get('width', 0) < ov.get('width', 0) * .95:
        raise Review('replacement resolution is lower')
    if nv.get('codec_name') not in ('hevc', 'h264'):
        raise Review('unsupported replacement codec')
    for side in nv.get('side_data_list', []):
        if side.get('dv_profile') == 5 or (side.get('dv_profile') and not side.get('dv_bl_signal_compatibility_id', 0)):
            raise Review('Dolby Vision has no compatible fallback')
        if side.get('dv_profile') and side.get('dv_bl_signal_compatibility_id') in (1, 6):
            # HDR10-compatible Dolby Vision declares a PQ/BT.2020 base layer.
            # Check its own signaling, never the grading of the original file.
            expected = {'color_transfer': 'smpte2084', 'color_primaries': 'bt2020', 'color_space': 'bt2020nc'}
            if any(nv.get(key) not in (None, 'unknown', 'unspecified', value) for key, value in expected.items()):
                raise Review('Dolby Vision HDR fallback color signaling is inconsistent')
    if hdr(old) and not hdr(new):
        if hdr_state(new) == 'unknown':
            raise Review('replacement HDR is not established; color evidence is unknown')
        if not (allow_sdr_remediation and ov.get('codec_name') == 'av1'):
            raise Review('HDR would be lost')
    na = audio(new)
    if not na or not any(x.get('channels', 0) > 0 for x in na):
        raise Review('no replacement audio')


def audio_tradeoffs(old, new, native):
    old_audio, new_audio = audio(old), audio(new)
    changes = []
    for lang in sorted({language(x) for x in old_audio} & {'eng', native}):
        before = [x for x in old_audio if language(x) == lang]
        after = [x for x in new_audio if language(x) == lang]
        if not after:
            changes.append({'language': lang, 'change': 'audio language absent'})
            continue
        old_channels = max(x.get('channels', 0) for x in before)
        new_channels = max(x.get('channels', 0) for x in after)
        if new_channels < old_channels:
            changes.append({'language': lang, 'change': 'fewer channels', 'before': old_channels, 'after': new_channels})
        if any(atmos(x) for x in before) and not any(atmos(x) for x in after):
            changes.append({'language': lang, 'change': 'Atmos absent'})
    return changes


def correlation(a, b):
    if len(a) != len(b) or not a:
        return -1
    am, bm = sum(a) / len(a), sum(b) / len(b)
    av = sum((x - am)**2 for x in a)
    bv = sum((x - bm)**2 for x in b)
    if not av or not bv:
        return -1
    return sum((x - am) * (y - bm) for x, y in zip(a, b)) / math.sqrt(av * bv)


def align(a, b, radius=1200, span=1000):
    center = len(b) // 2
    sample = b[center:center + span]
    best = (-1, 0)
    for shift in range(-radius, radius + 1):
        start = center + shift
        if start < 0:
            continue
        window = a[start:start + span]
        value = correlation(window, sample)
        if value > best[0]:
            best = (value, shift / 100)
    return {'correlation': best[0], 'old_minus_new': best[1]}


def consistent_alignment(results):
    if not results or min(x['correlation'] for x in results) < .85:
        raise Review('insufficient matching audio timing evidence')
    offsets = [x['old_minus_new'] for x in results]
    if max(offsets) - min(offsets) > .15:
        raise Review('audio timing changes across the program')
    return statistics.mean(offsets)


def resample_weak_audio(old_path, new_path, old_stream, new_stream, times, results, scale, duration, evidence):
    """A quiet or differently mixed scene gets nearby evidence at the same gate."""
    checked = list(results)
    attempts = []
    for index, (at, result) in enumerate(zip(times, results)):
        if result['correlation'] >= .85:
            continue
        for nearby in (at + 15, at - 15):
            if not 35 <= nearby <= duration - 45:
                continue
            match = align(envelope(old_path, old_stream, nearby),
                          envelope(new_path, new_stream, nearby * scale), radius=3000)
            attempts.append({'sample': index, 'at': nearby, **match})
            if match['correlation'] > checked[index]['correlation']:
                checked[index] = match
            if match['correlation'] >= .85:
                break
    atomic_json(evidence, {'times': times, 'initial': results, 'retries': attempts, 'alignment': checked})
    return checked


def envelope(path, stream, at):
    raw = run(['ffmpeg', '-v', 'error', '-threads', '2', '-ss', str(max(0, at - 35)), '-i', str(path),
               '-t', '80', '-map', '0:' + str(stream), '-ac', '1', '-ar', '8000', '-f', 's16le', '-'])
    samples = array.array('h')
    samples.frombytes(raw)
    return [math.sqrt(sum(x * x for x in samples[i:i + 80]) / 80)
            for i in range(0, len(samples) - 79, 80)]


def frame(path, at):
    raw = run(['ffmpeg', '-v', 'error', '-threads', '2', '-filter_threads', '1', '-ss', str(max(0, at)),
               '-i', str(path), '-map', '0:v:0', '-frames:v', '1', '-vf', 'scale=160:90,format=gray',
               '-f', 'rawvideo', '-'])
    if len(raw) != 160 * 90:
        raise Review('incomplete frame sample')
    return raw


def active_picture(data):
    rows = [data[i:i + 160] for i in range(0, len(data), 160)]
    top, bottom = 0, len(rows)
    while top < bottom - 20 and sum(x > 12 for x in rows[top]) < 8:
        top += 1
    while bottom > top + 20 and sum(x > 12 for x in rows[bottom - 1]) < 8:
        bottom -= 1
    rows = rows[top:bottom]
    return b''.join(rows[min(len(rows) - 1, round(i * (len(rows) - 1) / 89))] for i in range(90))


def frame_similarity(a, b):
    # Normalize luminance; this checks picture identity, not perceptual quality.
    # Dark scene content is not necessarily a letterbox. Try the original
    # geometry as well: tiny black-level differences can move a crop boundary.
    scores = []
    for left, right in ((a, b), (active_picture(a), active_picture(b))):
        points = [(x, y) for x, y in zip(left, right) if x > 12 or y > 12]
        if len(points) >= 500:
            scores.append(correlation([x for x, _ in points], [y for _, y in points]))
    if not scores:
        raise Review('uninformative dark frame sample')
    return max(scores)


@lru_cache(maxsize=128)
def picture_signature(data):
    """Small, smoothed luminance ranks tolerate different transfer curves."""
    data = active_picture(data)
    values = []
    for y in range(27):
        for x in range(48):
            cx, cy = round((x + .5) * 160 / 48 - .5), round((y + .5) * 90 / 27 - .5)
            values.append(round(sum(data[(cy + dy)*160 + cx + dx]
                                    for dy in (-1, 0, 1) for dx in (-1, 0, 1))/9))
    histogram = [0]*256
    for value in values:
        histogram[value] += 1
    ranks, count = [], 0
    for frequency in histogram:
        ranks.append((count + frequency/2)/len(values))
        count += frequency
    return tuple(ranks[value] for value in values)


def picture_similarity(a, b):
    """Bounded spatial registration; both luminance and edges must match."""
    direct = frame_similarity(a, b)
    if direct >= .98 or len(a) != 14400 or len(b) != 14400:
        return direct
    before, after = picture_signature(a), picture_signature(b)
    positions = [(x, y) for y in range(4, 23) for x in range(4, 44)]
    reference = [before[y*48+x] for x, y in positions]
    reference_edges = [before[y*48+x+1]-before[y*48+x-1] for x, y in positions]
    reference_edges += [before[(y+1)*48+x]-before[(y-1)*48+x] for x, y in positions]
    best = direct
    for zoom in (.96, 1, 1.04):
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                def point(x, y):
                    px, py = (x-23.5)*zoom+23.5+dx, (y-13)*zoom+13+dy
                    ix, iy = int(px), int(py)
                    fx, fy = px-ix, py-iy
                    return ((1-fy)*((1-fx)*after[iy*48+ix]+fx*after[iy*48+ix+1])
                            + fy*((1-fx)*after[(iy+1)*48+ix]+fx*after[(iy+1)*48+ix+1]))
                candidate = [point(x, y) for x, y in positions]
                luminance = correlation(reference, candidate)
                if luminance <= best:
                    continue
                edges = [point(x+1, y)-point(x-1, y) for x, y in positions]
                edges += [point(x, y+1)-point(x, y-1) for x, y in positions]
                best = max(best, min(luminance, correlation(reference_edges, edges)))
    return best


def frame_sequence(path, start, duration, rate):
    raw = run(['ffmpeg', '-v', 'error', '-threads', '2', '-filter_threads', '1',
               '-ss', str(max(0, start)), '-i', str(path), '-t', str(duration),
               '-map', '0:v:0', '-vf', f'fps={rate},scale=160:90,format=gray',
               '-f', 'rawvideo', '-'])
    size = 160 * 90
    if len(raw) % size:
        raise Review('incomplete picture timing evidence')
    return [raw[i:i + size] for i in range(0, len(raw), size)]


def picture_alignment(old_path, new_path, times, scale=1, radius=45, evidence=None, expected_times=None):
    # Different dubs cannot be correlated as if they were the same soundtrack.
    # Match actual pictures at multiple points, then refine to frame precision.
    results = []
    for sample, at in enumerate(times):
        reference = frame(old_path, at)
        expected = expected_times[sample] if expected_times is not None else at * scale
        start = max(0, expected - radius)
        coarse = frame_sequence(new_path, start, 2 * radius, 4)
        def best(frames, origin, rate):
            matches = []
            for index, candidate in enumerate(frames):
                try:
                    matches.append((frame_similarity(reference, candidate), origin + index / rate))
                except Review:
                    continue
            if not matches:
                raise Review('insufficient picture timing evidence')
            top = max(score for score, _ in matches)
            # Held animation frames and compression noise can produce nearly
            # identical scores over seconds. Prefer the nearest expected frame
            # within that high-confidence plateau, rather than inventing drift.
            band = .002 if top >= .98 else 0
            comparable = [m for m in matches if m[0] >= top - band]
            return min(comparable, key=lambda m: (abs(m[1] - expected), -m[0]))
        _, approximate = best(coarse, start, 4)
        refined_start = max(0, approximate - .3)
        refined = frame_sequence(new_path, refined_start, .6, 48)
        score, matched_at = best(refined, refined_start, 48)
        if score < .98:
            candidate = refined[round((matched_at-refined_start)*48)]
            score = picture_similarity(reference, candidate)
        results.append({'correlation': score, 'old_minus_new': at * scale - matched_at, 'new_at': matched_at})
        if evidence:
            atomic_json(evidence, {'times': times[:len(results)], 'alignment': results})
            Path(evidence).with_suffix('.original.gray').write_bytes(reference)
            Path(evidence).with_suffix('.replacement.gray').write_bytes(
                refined[round((matched_at-refined_start)*48)])
    if min(m['correlation'] for m in results) < .85:
        raise Review('sampled picture content does not match')
    try:
        offset = consistent_alignment(results)
    except Review:
        offset = None  # Different cuts do not establish a defective replacement.
    return results, offset


def content_sample(old_path, new_path, at, expected, duration, expected_alternatives=(), evidence=None):
    """Match a local scene independently; retry uninformative reference points."""
    attempts, best_pair, search_number = [], None, 0
    folder = Path(evidence) if evidence else None
    if folder:
        folder.mkdir(parents=True, exist_ok=True)
    def save(decision='searching'):
        if folder:
            atomic_json(folder/'attempts.json', {'checkpoint': at, 'decision': decision, 'attempts': attempts})
    def record(sampled_at, predicted, before, after, score):
        nonlocal best_pair
        attempts.append({'at': sampled_at, 'new_at': predicted, 'correlation': score})
        if best_pair is None or score > best_pair[0]['correlation']:
            best_pair = attempts[-1], before, after
        if folder:
            save()
            (folder/'original.gray').write_bytes(best_pair[1])
            (folder/'replacement.gray').write_bytes(best_pair[2])
    for sampled_at in (at, at + 2, at - 2):
        if not 0 < sampled_at < duration:
            continue
        predictions = [p for p in dict.fromkeys(round(hint + sampled_at - at, 6)
                       for hint in (expected, *expected_alternatives)) if p >= 0]
        for predicted in predictions:
            try:
                before, after, score = matching_frame(old_path, new_path, sampled_at, predicted)
                record(sampled_at, predicted, before, after, score)
                if score >= .98:
                    save('accepted')
                    return {'at': sampled_at, 'new_at': predicted, 'correlation': score}, before, after
            except Review as exc:
                attempts.append({'at': sampled_at, 'new_at': predicted, 'issue': str(exc)})
                save()
        for radius, predicted in ((radius, predicted) for radius in (2, 45) for predicted in predictions):
            try:
                search_evidence = folder/('search-' + str(search_number) + '.json') if folder else None
                search_number += 1
                matches, _ = picture_alignment(old_path, new_path, [sampled_at], radius=radius,
                                               expected_times=[predicted], evidence=search_evidence)
                matched_at = matches[0]['new_at']
                before, after, score = matching_frame(old_path, new_path, sampled_at, matched_at)
                record(sampled_at, matched_at, before, after, score)
                if score >= .85:
                    save('accepted')
                    return {'at': sampled_at, 'new_at': matched_at, 'correlation': score}, before, after
            except Review as exc:
                attempts.append({'at': sampled_at, 'new_at': predicted, 'radius': radius, 'issue': str(exc)})
                save()
    save('rejected')
    raise Review('sampled program content does not match')


def subtitle_timeline(samples):
    """Only transfer source subtitles when pictures support one linear mapping."""
    if len(samples) < 3:
        return None
    old = [m['at'] for m in samples]
    new = [m['new_at'] for m in samples]
    if any(b <= a for a, b in zip(new, new[1:])):
        return None
    if min(m['correlation'] for m in samples) < .98:
        return None
    mean_old, mean_new = statistics.mean(old), statistics.mean(new)
    variance = sum((at - mean_old)**2 for at in old)
    if not variance:
        return None
    scale = sum((a - mean_old) * (b - mean_new) for a, b in zip(old, new)) / variance
    if abs(scale - 1) < 1e-9:
        scale = 1
    offset = mean_old * scale - mean_new
    if abs(offset) < 1e-9:
        offset = 0
    if not .95 <= scale <= 1.05 or max(abs(b - (a * scale - offset)) for a, b in zip(old, new)) > .15:
        return None
    return {'scale': scale, 'offset': offset}


def matching_frame(old_path, new_path, at, expected):
    """Allow two frames of seek/rounding error, including around scene cuts."""
    reference = frame(old_path, at)
    candidates = [frame(new_path, expected)]
    candidates += frame_sequence(new_path, max(0, expected - .08), .16, 48)
    matches = []
    for candidate in candidates:
        try:
            matches.append((frame_similarity(reference, candidate), candidate))
        except Review:
            continue
    if not matches:
        raise Review('uninformative picture sample')
    score, candidate = max(matches, key=lambda item: item[0])
    if score < .98:
        matches = [(picture_similarity(reference, c), c) for value, c in matches if value >= score - .12]
        score, candidate = max(matches, key=lambda item: item[0])
    return reference, candidate, score


def timestamp(value):
    hours, minutes, seconds = value.replace(',', '.').split(':')
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def cues(text):
    result = []
    for block in re.split(r'\r?\n\s*\r?\n', text.lstrip('\ufeff').strip()):
        match = re.search(r'(\d+:\d+:\d+[,.]\d+)\s*-->\s*(\d+:\d+:\d+[,.]\d+)\s*\n(.+)', block, re.S)
        if match:
            words = re.sub(r'<[^>]+>|\{[^}]+\}', '', match[3])
            words = re.sub(r'\s+', ' ', words).strip()
            if words:
                result.append((timestamp(match[1]), timestamp(match[2]), words))
    return result


def full_sub(stream):
    return not stream.get('disposition', {}).get('forced') and not re.search(
        r'sign|song|forced|comment', stream.get('tags', {}).get('title', ''), re.I)


def subtitle(path, stream, output, offset=0, scale=1):
    bitmap = stream['codec_name'] in ('hdmv_pgs_subtitle', 'dvd_subtitle')
    if bitmap and (stream['codec_name'] != 'hdmv_pgs_subtitle' or abs(offset) > .04 or abs(scale - 1) > .00001):
        raise Review('bitmap subtitle timing needs review')
    styled = stream['codec_name'] in ('ass', 'ssa')
    suffix = '.sup' if bitmap else '.ass' if styled else '.srt'
    target = Path(str(output) + suffix)
    args = ['ffmpeg', '-v', 'error', '-itsoffset', str(offset / scale), '-itsscale', str(scale), '-i', str(path),
            '-map', '0:' + str(stream['index']), '-c:s', 'copy' if bitmap else 'ass' if styled else 'srt', '-y', str(target)]
    run(args, timeout=300)
    if not target.is_file() or target.stat().st_size < 100:
        raise Review('missing full subtitle asset')
    return target, None if bitmap or styled else cues(target.read_text(encoding='utf-8-sig'))


def scene_predictions(at, duration, replacement_duration, cadence, previous=None):
    predictions = [at, at * replacement_duration / duration,
                   at + replacement_duration - duration]
    if previous:
        predictions.insert(0, at + previous['new_at'] - previous['at'])
    old_rate, new_rate = cadence['original'].get('rate'), cadence['replacement'].get('rate')
    if old_rate and new_rate:
        predictions.append(at * old_rate / new_rate)
    return [p for p in dict.fromkeys(predictions) if 0 <= p <= replacement_duration - 4]


def verify(old_path, new_path, work, native='eng', anime=False, minimum_savings=.3,
           allow_sdr_remediation=False):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    old_id = identity(old_path)
    new_id = identity(new_path)
    if minimum_savings is not None and new_id['size'] > old_id['size'] * (1 - minimum_savings):
        raise Review('replacement does not save the required space')
    old, new = probe(old_path), probe(new_path)
    atomic_json(work / 'original-probe.json', old)
    if hdr(old) and hdr_state(new) == 'unknown':
        atomic_json(work / 'replacement-color-evidence.json', frame_color_evidence(new_path, new))
    atomic_json(work / 'replacement-probe.json', new)
    validate_streams(old, new, native, anime, allow_sdr_remediation)
    cadence = {}
    for name, path, data in (('original', old_path, old), ('replacement', new_path, new)):
        try:
            cadence[name] = measure_frame_rate(path, data)
        except Review as exc:
            cadence[name] = {'issue': str(exc)}
    atomic_json(work / 'cadence-evidence.json', cadence)
    if minimum_savings is None and video(old).get('codec_name') != 'av1':
        raise Review('size waiver requires actual AV1 source video')
    duration = float(old['format']['duration'])
    nduration = float(new['format']['duration'])
    content_notes = []
    if hdr(old) and hdr_state(new) == 'sdr':
        content_notes.append({'change': 'HDR to SDR for AV1 playback repair',
                              'original_hdr': 'hdr', 'replacement_hdr': 'sdr'})
    if abs(duration - nduration) > max(90, duration * .02):
        content_notes.append({'change': 'runtime differs; content checked at local scene positions',
                              'original_seconds': duration, 'replacement_seconds': nduration})
    # Credits, logos and distributor slates can differ in otherwise identical
    # releases. Verify scenes throughout the program rather than end credits.
    times = [round(duration * fraction, 2) for fraction in (.15, .4, .65, .85)]
    oa, na = audio(old), audio(new)
    pairs = audio_search_pairs(oa, na, native)
    lang = pairs[0][0] if pairs else None
    correspondence = {}
    # Source soundtracks provide optional local search hints, not an A/V-sync
    # verdict. A bad source or different dub cannot invalidate the new release.
    for check_lang, osource, nsource, untagged in pairs:
        try:
            original_env = [envelope(old_path, osource['index'], at) for at in times]
            replacement_env = [envelope(new_path, nsource['index'], at) for at in times]
            matches = [align(a, b, radius=3000) for a, b in zip(original_env, replacement_env)]
            evidence_name = 'audio' if check_lang == lang else 'audio-' + check_lang
            atomic_json(work / (evidence_name + '-evidence.json'), {'times': times, 'original': original_env,
                       'replacement': replacement_env, 'alignment': matches, 'untagged_search_hint': untagged,
                       'original_stream': osource['index'], 'replacement_stream': nsource['index']})
            correspondence[check_lang] = resample_weak_audio(old_path, new_path, osource['index'], nsource['index'],
                times, matches, 1, duration, work / (evidence_name + '-resampling-evidence.json'))
        except Review as exc:
            atomic_json(work / ('audio-' + check_lang + '-issue.json'), {'issue': str(exc)})
    results = correspondence.get(lang, [])
    frame_results = []
    for index, at in enumerate(times):
        hint = results[index]['old_minus_new'] if len(results) > index and results[index]['correlation'] >= .85 else 0
        predictions = scene_predictions(at, duration, nduration, cadence,
                                        frame_results[-1] if frame_results else None)
        sample, before, after = content_sample(old_path, new_path, at, at - hint, duration, predictions,
                                              evidence=work/('checkpoint-' + str(index)))
        (work / ('original-' + str(sample['at']) + '.gray')).write_bytes(before)
        (work / ('replacement-' + str(sample['at']) + '.gray')).write_bytes(after)
        frame_results.append(sample)
        atomic_json(work / 'frame-evidence.json', frame_results)
        args = ['ffmpeg', '-v', 'error', '-xerror', '-threads', '2', '-ss', str(max(0, sample['new_at'])),
                '-i', str(new_path), '-t', '4', '-map', '0:v:0']
        for stream in na:
            args += ['-map', '0:' + str(stream['index'])]
        run(args + ['-fps_mode', 'passthrough', '-enc_time_base:v', '1/1000', '-f', 'null', '-'])
    mapping = subtitle_timeline(frame_results)
    scale = mapping['scale'] if mapping else None
    offset = mapping['offset'] if mapping else None
    retained, warnings = [], []
    for stream in old['streams']:
        sl = language(stream)
        if stream.get('codec_type') != 'subtitle' or sl not in ('eng', native):
            continue
        # Retain the exact source's full dialogue as a small sidecar. This also
        # handles releases whose only embedded native subtitles are signs.
        try:
            if mapping is None:
                raise Review('source subtitle timeline differs; use replacement subtitles or background repair')
            target, parsed = subtitle(old_path, stream, work / ('retained-' + str(stream['index'])), -offset, scale)
        except (Failure, OSError) as exc:
            warnings.append({'language': sl, 'stream': stream['index'], 'issue': str(exc) if isinstance(exc, Failure) else type(exc).__name__})
            continue
        retained.append({'path': str(target), 'language': sl, 'stream': stream['index'],
                         'sdh': bool(stream.get('disposition', {}).get('hearing_impaired')),
                         'forced': not full_sub(stream), 'full': full_sub(stream),
                         'cues': len(parsed) if parsed is not None else None})
    fonts = []
    # Keep existing language-labelled sidecars when the video filename changes.
    # A subtitle problem remains a background repair, not a video rejection.
    for index, source in enumerate(Path(old_path).parent.glob(Path(old_path).stem + '.*')):
        if source.suffix.lower() not in ('.srt', '.ass', '.ssa', '.vtt', '.sup'):
            continue
        match = re.search(r'(?i)(?<![A-Za-z0-9])(eng|english|en|jpn|japanese|ja|jp|kor|korean|ko|kr|zho|chinese|zh|chi)(?![A-Za-z0-9])',
                          source.name[len(Path(old_path).stem):])
        if not match:
            continue
        sl = LANG.get(match[1].lower(), match[1].lower())
        if sl not in ('eng', native):
            continue
        suffix = '.ass' if source.suffix.lower() in ('.ass', '.ssa') else source.suffix.lower()
        target = work / ('external-' + str(index) + suffix)
        try:
            if mapping is None:
                raise Review('source subtitle timeline differs; use replacement subtitles or background repair')
            if source.suffix.lower() == '.sup' and (abs(offset) > .04 or abs(scale - 1) > .00001):
                raise Review('external bitmap subtitle timing needs review')
            if abs(offset) <= .04 and abs(scale - 1) <= .00001:
                from shutil import copyfile
                copyfile(source, target)
            else:
                run(['ffmpeg', '-v', 'error', '-itsoffset', str(-offset / scale), '-itsscale', str(scale), '-i', str(source),
                     '-map', '0:s:0', '-c:s', 'ass' if suffix == '.ass' else 'srt', '-y', str(target)])
            retained.append({'path': str(target), 'language': sl, 'stream': 'external-' + str(index),
                             'sdh': bool(re.search(r'(?i)\b(?:sdh|hi)\b', source.name)),
                             'forced': not full_sub({'tags': {'title': source.name}}),
                             'full': full_sub({'tags': {'title': source.name}}), 'cues': None})
        except (Failure, OSError) as exc:
            warnings.append({'language': sl, 'issue': str(exc) if isinstance(exc, Failure) else type(exc).__name__})
    if any(Path(s['path']).suffix == '.ass' for s in retained):
        for stream in old['streams']:
            name = Path(stream.get('tags', {}).get('filename', '')).name
            if stream.get('codec_type') != 'attachment' or Path(name).suffix.lower() not in ('.ttf', '.otf'):
                continue
            target = work / ('font-' + str(stream['index']) + '-' + name)
            try:
                run(['ffmpeg', '-v', 'error', '-dump_attachment:' + str(stream['index']), str(target),
                     '-i', str(old_path), '-t', '0', '-map', '0:v:0', '-c', 'copy', '-f', 'null', '-'])
                if target.is_file():
                    fonts.append(str(target))
            except (Failure, OSError):
                warnings.append({'issue': 'subtitle font extraction failed', 'stream': stream['index']})
    present = {language(s) for s in new['streams'] if s.get('codec_type') == 'subtitle' and full_sub(s)}
    present |= {s['language'] for s in retained if s['full']}
    missing = sorted({'eng', native} - present)
    if identity(old_path) != old_id or identity(new_path) != new_id:
        raise Failure('file changed during verification')
    result = {'old_identity': old_id, 'new_identity': new_id, 'old_path': old_path,
              'new_path': new_path, 'audio_alignment': results, 'offset': offset,
              'alignment_method': 'independent local picture correspondence',
              'audio_correspondence': correspondence, 'audio_tradeoffs': audio_tradeoffs(old, new, native),
              'timeline_scale': scale,
              'source_subtitle_timeline_usable': mapping is not None,
              'content_notes': content_notes,
              'cadence': cadence,
              'frame_samples': frame_results, 'subtitles': retained, 'subtitle_fonts': fonts,
              'subtitle_missing': missing, 'subtitle_warnings': warnings,
              'old_duration': duration, 'new_duration': nduration,
              'resolution': resolution(new), 'logical_savings': old_id['size'] - new_id['size']}
    atomic_json(work / 'verified.json', result)
    return result


def install_subtitles(result, library):
    from shutil import copyfile
    paths = []
    for sub in result['subtitles']:
        source = Path(sub['path'])
        dest = Path(library).with_suffix('')
        dest = Path(str(dest) + '.' + sub['language'] + ('.forced' if sub.get('forced') else '') + ('.sdh' if sub['sdh'] else '')
                    + '.retained-' + str(sub['stream']) + source.suffix)
        try:
            copyfile(source, dest)
            if dest.read_bytes() != source.read_bytes():
                raise Failure('subtitle consumer readback mismatch')
            paths.append(str(dest))
        except (Failure, OSError) as exc:
            result.setdefault('subtitle_warnings', []).append({'language': sub['language'], 'issue': type(exc).__name__})
            result['subtitle_missing'] = sorted(set(result.get('subtitle_missing', [])) | {sub['language']})
    for font in result.get('subtitle_fonts', []):
        dest = Path(library).parent / '.fonts' / Path(font).name
        try:
            dest.parent.mkdir(exist_ok=True)
            copyfile(font, dest)
        except OSError:
            result.setdefault('subtitle_warnings', []).append({'issue': 'subtitle font installation failed'})
    return paths
