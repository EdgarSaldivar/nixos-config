"""Decode, stream, timing and subtitle evidence before replacing a library file."""
import array
import json
import math
from pathlib import Path
import re
import statistics
import subprocess

from .core import Failure, Review, atomic_json, identity

LANG = {'en': 'eng', 'english': 'eng', 'ja': 'jpn', 'jp': 'jpn', 'japanese': 'jpn',
        'ko': 'kor', 'korean': 'kor', 'zh': 'zho', 'chi': 'zho', 'chinese': 'zho'}


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


def hdr(data):
    return video(data).get('color_transfer') in ('smpte2084', 'arib-std-b67')


def atmos(stream):
    return bool(re.search('atmos', str(stream.get('profile', '')) + str(stream.get('tags', {})), re.I))


def validate_streams(old, new, native, anime):
    ov, nv = video(old), video(new)
    if resolution(new) < resolution(old) or nv.get('width', 0) < ov.get('width', 0) * .95:
        raise Review('replacement resolution is lower')
    if nv.get('codec_name') not in ('hevc', 'h264', 'av1'):
        raise Review('unsupported replacement codec')
    for side in nv.get('side_data_list', []):
        if side.get('dv_profile') == 5 or (side.get('dv_profile') and not side.get('dv_bl_signal_compatibility_id', 0)):
            raise Review('Dolby Vision has no compatible fallback')
    if hdr(old) and not hdr(new):
        raise Review('HDR would be lost')
    oa, na = audio(old), audio(new)
    wanted = {language(x) for x in oa} & {'eng', native}
    if not wanted <= {language(x) for x in na}:
        raise Review('English or native audio would be lost')
    if not na:
        raise Review('no replacement audio')
    for lang in wanted:
        old_tracks = [x for x in oa if language(x) == lang]
        new_tracks = [x for x in na if language(x) == lang]
        if not anime and max(x.get('channels', 0) for x in new_tracks) < max(x.get('channels', 0) for x in old_tracks):
            raise Review('main audio channel layout would be reduced')
        if any(atmos(x) for x in old_tracks) and not any(atmos(x) for x in new_tracks):
            raise Review('Atmos metadata would be lost')
    def fps(stream):
        numerator, denominator = stream.get('avg_frame_rate', stream.get('r_frame_rate', '0/1')).split('/')
        return float(numerator) / max(float(denominator), 1)
    if abs(fps(ov) - fps(nv)) > .1:
        raise Review('frame cadence differs')


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
    if max(offsets) - min(offsets) > .04:
        raise Review('audio timing changes across the program')
    return statistics.mean(offsets)


def envelope(path, stream, at):
    raw = run(['ffmpeg', '-v', 'error', '-threads', '2', '-ss', str(max(0, at - 15)), '-i', str(path),
               '-t', '40', '-map', '0:' + str(stream), '-ac', '1', '-ar', '8000', '-f', 's16le', '-'])
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


def frame_similarity(a, b):
    # Normalize luminance; this checks picture identity, not an absolute perceptual quality score.
    def active(data):
        rows = [data[i:i + 160] for i in range(0, len(data), 160)]
        top, bottom = 0, len(rows)
        while top < bottom - 20 and sum(x > 12 for x in rows[top]) < 8:
            top += 1
        while bottom > top + 20 and sum(x > 12 for x in rows[bottom - 1]) < 8:
            bottom -= 1
        rows = rows[top:bottom]
        return b''.join(rows[min(len(rows) - 1, round(i * (len(rows) - 1) / 89))] for i in range(90))
    a, b = active(a), active(b)
    points = [(x, y) for x, y in zip(a, b) if x > 12 or y > 12]
    if len(points) < 500:
        raise Review('uninformative dark frame sample')
    return correlation([x for x, _ in points], [y for _, y in points])


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


def subtitle(path, stream, output, offset=0):
    bitmap = stream['codec_name'] in ('hdmv_pgs_subtitle', 'dvd_subtitle')
    if bitmap and (stream['codec_name'] != 'hdmv_pgs_subtitle' or abs(offset) > .04):
        raise Review('bitmap subtitle timing needs review')
    suffix = '.sup' if bitmap else '.srt'
    target = Path(str(output) + suffix)
    args = ['ffmpeg', '-v', 'error', '-itsoffset', str(offset), '-i', str(path),
            '-map', '0:' + str(stream['index']), '-c:s', 'copy' if bitmap else 'srt', '-y', str(target)]
    run(args, timeout=300)
    if not target.is_file() or target.stat().st_size < 100:
        raise Review('missing full subtitle asset')
    return target, None if bitmap else cues(target.read_text(encoding='utf-8-sig'))


def verify(old_path, new_path, work, native='eng', anime=False, minimum_savings=.3):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    old_id = identity(old_path)
    new_id = identity(new_path)
    if new_id['size'] > old_id['size'] * (1 - minimum_savings):
        raise Review('replacement does not save the required space')
    old, new = probe(old_path), probe(new_path)
    atomic_json(work / 'original-probe.json', old)
    atomic_json(work / 'replacement-probe.json', new)
    validate_streams(old, new, native, anime)
    duration = float(old['format']['duration'])
    nduration = float(new['format']['duration'])
    if abs(duration - nduration) > max(90, duration * .02):
        raise Review('edition/runtime mismatch')
    times = sorted(set(round(x, 2) for x in (min(90, duration * .2), min(600, duration * .5),
                                             min(1200, duration * .75)) if x > 30))
    oa, na = audio(old), audio(new)
    common = {language(s) for s in oa} & {language(s) for s in na}
    lang = native if native in common else 'eng' if 'eng' in common else next(iter(common), None)
    if not lang:
        raise Review('no common audio track for timing verification')
    osource = next(s for s in oa if language(s) == lang)
    nsource = next(s for s in na if language(s) == lang)
    original_env = [envelope(old_path, osource['index'], at) for at in times]
    replacement_env = [envelope(new_path, nsource['index'], at) for at in times]
    results = [align(a, b) for a, b in zip(original_env, replacement_env)]
    offset = consistent_alignment(results)
    atomic_json(work / 'audio-evidence.json', {'times': times, 'original': original_env,
                                              'replacement': replacement_env, 'alignment': results})
    # Check each common language against its own source. A native dub shifted by
    # one second cannot be accepted solely because the English track is aligned.
    for check_lang in common & {'eng', native}:
        if check_lang == lang:
            continue
        ot = next(s for s in oa if language(s) == check_lang)
        nt = next(s for s in na if language(s) == check_lang)
        matches = [align(envelope(old_path, ot['index'], at), envelope(new_path, nt['index'], at)) for at in times]
        other_offset = consistent_alignment(matches)
        if abs(other_offset - offset) > .08:
            raise Review('English/native audio tracks are out of sync')
    frame_results = []
    for at in times + [max(20, duration - 90)]:
        a, b = frame(old_path, at), frame(new_path, at - offset)
        score = frame_similarity(a, b)
        if score < .85:
            raise Review('sampled pictures do not match')
        frame_results.append({'at': at, 'correlation': score})
        (work / ('original-' + str(at) + '.gray')).write_bytes(a)
        (work / ('replacement-' + str(at) + '.gray')).write_bytes(b)
        args = ['ffmpeg', '-v', 'error', '-xerror', '-threads', '2', '-ss', str(max(0, at - offset)),
                '-i', str(new_path), '-t', '4', '-map', '0:v:0']
        for stream in na:
            if language(stream) in ('eng', native):
                args += ['-map', '0:' + str(stream['index'])]
        run(args + ['-fps_mode', 'passthrough', '-enc_time_base:v', '1/1000', '-f', 'null', '-'])
    retained = []
    for stream in old['streams']:
        sl = language(stream)
        if stream.get('codec_type') != 'subtitle' or sl not in ('eng', native) or not full_sub(stream):
            continue
        # Retain the exact source's full dialogue as a small sidecar. This also
        # handles releases whose only embedded native subtitles are signs.
        target, parsed = subtitle(old_path, stream, work / ('retained-' + str(stream['index'])), -offset)
        if parsed is not None and len(parsed) < 30:
            continue
        retained.append({'path': str(target), 'language': sl, 'stream': stream['index'],
                         'sdh': bool(stream.get('disposition', {}).get('hearing_impaired')),
                         'cues': len(parsed) if parsed is not None else None})
    for sl in {language(s) for s in old['streams'] if s.get('codec_type') == 'subtitle'
               and language(s) in ('eng', native) and full_sub(s)}:
        if not any(x['language'] == sl for x in retained):
            raise Review('full source subtitles could not be retained')
    if identity(old_path) != old_id or identity(new_path) != new_id:
        raise Failure('file changed during verification')
    result = {'old_identity': old_id, 'new_identity': new_id, 'old_path': old_path,
              'new_path': new_path, 'audio_alignment': results, 'offset': offset,
              'frame_samples': frame_results, 'subtitles': retained,
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
        dest = Path(str(dest) + '.' + sub['language'] + ('.sdh' if sub['sdh'] else '')
                    + '.retained-' + str(sub['stream']) + source.suffix)
        copyfile(source, dest)
        if dest.read_bytes() != source.read_bytes():
            raise Failure('subtitle consumer readback mismatch')
        paths.append(str(dest))
    return paths
