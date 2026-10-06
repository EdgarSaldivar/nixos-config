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


def hdr(data):
    return video(data).get('color_transfer') in ('smpte2084', 'arib-std-b67')


def atmos(stream):
    return bool(re.search('atmos', str(stream.get('profile', '')) + str(stream.get('tags', {})), re.I))


def validate_streams(old, new, native, anime):
    ov, nv = video(old), video(new)
    if resolution(new) < resolution(old) or nv.get('width', 0) < ov.get('width', 0) * .95:
        raise Review('replacement resolution is lower')
    if nv.get('codec_name') not in ('hevc', 'h264'):
        raise Review('unsupported replacement codec')
    for side in nv.get('side_data_list', []):
        if side.get('dv_profile') == 5 or (side.get('dv_profile') and not side.get('dv_bl_signal_compatibility_id', 0)):
            raise Review('Dolby Vision has no compatible fallback')
    if hdr(old) and not hdr(new):
        raise Review('HDR would be lost')
    na = audio(new)
    if not na or not any(x.get('channels', 0) > 0 for x in na):
        raise Review('no replacement audio')
    def fps(stream):
        numerator, denominator = stream.get('avg_frame_rate', stream.get('r_frame_rate', '0/1')).split('/')
        return float(numerator) / max(float(denominator), 1)
    if abs(fps(ov) - fps(nv)) > .1:
        raise Review('frame cadence differs')


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


def frame_sequence(path, start, duration, rate):
    raw = run(['ffmpeg', '-v', 'error', '-threads', '2', '-filter_threads', '1',
               '-ss', str(max(0, start)), '-i', str(path), '-t', str(duration),
               '-map', '0:v:0', '-vf', f'fps={rate},scale=160:90,format=gray',
               '-f', 'rawvideo', '-'])
    size = 160 * 90
    if len(raw) % size:
        raise Review('incomplete picture timing evidence')
    return [raw[i:i + size] for i in range(0, len(raw), size)]


def picture_alignment(old_path, new_path, times):
    # Different dubs cannot be correlated as if they were the same soundtrack.
    # Match actual pictures at multiple points, then refine to frame precision.
    results = []
    for at in times:
        reference = frame(old_path, at)
        start = max(0, at - 12)
        coarse = frame_sequence(new_path, start, 24, 4)
        def best(frames, origin, rate):
            matches = []
            for index, candidate in enumerate(frames):
                try:
                    matches.append((frame_similarity(reference, candidate), origin + index / rate))
                except Review:
                    continue
            if not matches:
                raise Review('insufficient picture timing evidence')
            return max(matches)
        _, approximate = best(coarse, start, 4)
        refined_start = max(0, approximate - .3)
        score, matched_at = best(frame_sequence(new_path, refined_start, .6, 48), refined_start, 48)
        results.append({'correlation': score, 'old_minus_new': at - matched_at})
    return results, consistent_alignment(results)


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
    styled = stream['codec_name'] in ('ass', 'ssa')
    suffix = '.sup' if bitmap else '.ass' if styled else '.srt'
    target = Path(str(output) + suffix)
    args = ['ffmpeg', '-v', 'error', '-itsoffset', str(offset), '-i', str(path),
            '-map', '0:' + str(stream['index']), '-c:s', 'copy' if bitmap else 'ass' if styled else 'srt', '-y', str(target)]
    run(args, timeout=300)
    if not target.is_file() or target.stat().st_size < 100:
        raise Review('missing full subtitle asset')
    return target, None if bitmap or styled else cues(target.read_text(encoding='utf-8-sig'))


def verify(old_path, new_path, work, native='eng', anime=False, minimum_savings=.3):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    old_id = identity(old_path)
    new_id = identity(new_path)
    if minimum_savings is not None and new_id['size'] > old_id['size'] * (1 - minimum_savings):
        raise Review('replacement does not save the required space')
    old, new = probe(old_path), probe(new_path)
    atomic_json(work / 'original-probe.json', old)
    atomic_json(work / 'replacement-probe.json', new)
    validate_streams(old, new, native, anime)
    if minimum_savings is None and video(old).get('codec_name') != 'av1':
        raise Review('size waiver requires actual AV1 source video')
    duration = float(old['format']['duration'])
    nduration = float(new['format']['duration'])
    if abs(duration - nduration) > max(90, duration * .02):
        raise Review('edition/runtime mismatch')
    times = sorted(set(round(x, 2) for x in (min(90, duration * .2), min(600, duration * .5),
                                             min(1200, duration * .75)) if x > 30))
    oa, na = audio(old), audio(new)
    common = {language(s) for s in oa} & {language(s) for s in na}
    lang = native if native in common else 'eng' if 'eng' in common else next(iter(common), None)
    if lang:
        osource = next(s for s in oa if language(s) == lang)
        nsource = next(s for s in na if language(s) == lang)
        original_env = [envelope(old_path, osource['index'], at) for at in times]
        replacement_env = [envelope(new_path, nsource['index'], at) for at in times]
        results = [align(a, b) for a, b in zip(original_env, replacement_env)]
        offset = consistent_alignment(results)
        alignment_method = 'common audio language'
        atomic_json(work / 'audio-evidence.json', {'times': times, 'original': original_env,
                                                  'replacement': replacement_env, 'alignment': results})
    else:
        results, offset = picture_alignment(old_path, new_path, times)
        alignment_method = 'pictures; different audio languages'
        atomic_json(work / 'picture-timing-evidence.json', {'times': times, 'alignment': results})
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
            args += ['-map', '0:' + str(stream['index'])]
        run(args + ['-fps_mode', 'passthrough', '-enc_time_base:v', '1/1000', '-f', 'null', '-'])
    retained, warnings = [], []
    for stream in old['streams']:
        sl = language(stream)
        if stream.get('codec_type') != 'subtitle' or sl not in ('eng', native):
            continue
        # Retain the exact source's full dialogue as a small sidecar. This also
        # handles releases whose only embedded native subtitles are signs.
        try:
            target, parsed = subtitle(old_path, stream, work / ('retained-' + str(stream['index'])), -offset)
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
            if source.suffix.lower() == '.sup' and abs(offset) > .04:
                raise Review('external bitmap subtitle timing needs review')
            if abs(offset) <= .04:
                from shutil import copyfile
                copyfile(source, target)
            else:
                run(['ffmpeg', '-v', 'error', '-itsoffset', str(-offset), '-i', str(source),
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
              'alignment_method': alignment_method, 'audio_tradeoffs': audio_tradeoffs(old, new, native),
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
