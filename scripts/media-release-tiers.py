#!/usr/bin/env python3
"""Generate *arr release-group tier custom formats from a Dictionarry database.

Reads local checkouts of github.com/Dictionarry-Hub/schema and
github.com/Dictionarry-Hub/database (SQL only, nothing is executed) and writes
policies/media-release-tiers.json for scripts/media-library-policy.py.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

# 1080p follows Dictionarry's space-oriented Compact profile; 2160p its Efficient one.
LADDERS = (('1080p Compact', '1080p'), ('2160p Efficient', '2160p'))
# Catch-alls and x264 Bluray fallbacks are not encoder tiers.
EXCLUDED = {'1080p WEB-DL HEVC', '1080p WEB-DL (Efficient)', '2160p WEB-DL (Efficient)',
            '1080p Balanced Tier 1', '1080p Balanced Tier 2'}
BAND = 800000  # Dictionarry's release-tier score band
# Tier steps of 1000 survive the optimizer's 500-point score grouping and
# outrank the smaller audio/HDR/subtitle bonuses between adjacent tiers.
STEP = 1000
BAN = -30000
SOURCES = {'sonarr': {'television': 1, 'web_dl': 3, 'webrip': 4, 'dvd': 5, 'bluray': 6, 'bluray_raw': 7},
           'radarr': {'television': 6, 'web_dl': 7, 'webrip': 8, 'dvd': 5, 'bluray': 9}}
RELEASE_TYPES = {'season_pack': 3}


def build(schema, database):
    db = sqlite3.connect(':memory:')
    ordered = lambda folder: sorted(folder.glob('*.sql'), key=lambda p: int(p.name.split('.')[0]))
    skipped = 0
    for path in ordered(schema / 'ops') + ordered(database / 'ops'):
        statement = ''
        for line in path.read_text().splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                try:
                    db.execute(statement)
                except sqlite3.IntegrityError:
                    skipped += 1  # an op re-adding an existing row; the row stands
                statement = ''
    db.commit()
    return db, skipped


def conditions(db, cf, app):
    patterns = dict(db.execute('select name, pattern from regular_expressions'))
    lookup = lambda table, column, name: db.execute(
        f'select {column} from {table} where custom_format_name=? and condition_name=?', (cf, name)).fetchone()
    result = []
    for name, kind, negate, required in db.execute(
            "select name, type, negate, required from custom_format_conditions where custom_format_name=? "
            "and arr_type in ('all', ?) order by id", (cf, app)):
        if kind in ('release_group', 'release_title'):
            implementation = 'ReleaseGroupSpecification' if kind == 'release_group' else 'ReleaseTitleSpecification'
            value = patterns[lookup('condition_patterns', 'regular_expression_name', name)[0]]
        elif kind == 'resolution':
            implementation, value = 'ResolutionSpecification', int(lookup('condition_resolutions', 'resolution', name)[0].rstrip('p'))
        elif kind == 'source':
            implementation, value = 'SourceSpecification', SOURCES[app][lookup('condition_sources', 'source', name)[0]]
        elif kind == 'release_type' and app == 'sonarr':
            implementation, value = 'ReleaseTypeSpecification', RELEASE_TYPES[lookup('condition_release_types', 'release_type', name)[0]]
        else:
            raise SystemExit(f'unsupported condition {kind} in {cf} for {app}')
        result.append({'name': name, 'implementation': implementation, 'negate': bool(negate),
                       'required': bool(required), 'value': value})
    return result


def tiers(db):
    formats, top = [], {}
    for app in ('sonarr', 'radarr'):
        for profile, resolution in LADDERS:
            rows = []
            for cf, score in db.execute(
                    "select custom_format_name, score from quality_profile_custom_formats where quality_profile_name=? "
                    "and arr_type in ('all', ?) and score >= ? order by score desc, custom_format_name",
                    (profile, app, BAND)):
                specs = conditions(db, cf, app)
                scoped = {s['value'] for s in specs if s['implementation'] == 'ResolutionSpecification' and not s['negate']}
                if cf not in EXCLUDED and scoped == {int(resolution.rstrip('p'))}:
                    rows.append((cf, score, specs))
            levels = sorted({score for _, score, _ in rows}, reverse=True)
            for cf, score, specs in rows:
                value = (len(levels) - levels.index(score)) * STEP
                formats.append({'name': 'MLP ' + cf, 'apps': [app], 'score': value, 'specifications': specs})
            if resolution == '1080p':
                top[app] = len(levels) * STEP
        banned = conditions(db, 'Banned Groups', app)
        if any(s['implementation'] != 'ReleaseGroupSpecification' or s['negate'] or s['required'] for s in banned):
            raise SystemExit('Banned Groups is no longer a plain group list')
        formats.append({'name': 'MLP Banned groups', 'apps': [app], 'score': BAN, 'allProfiles': True, 'specifications': banned})
    names = [(f['apps'][0], f['name']) for f in formats]
    if len(names) != len(set(names)):
        raise SystemExit('duplicate generated custom format names')
    return formats, top


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--schema-commit', required=True)
    parser.add_argument('--database-commit', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    db, skipped = build(args.schema, args.database)
    formats, top = tiers(db)
    args.output.write_text(json.dumps({
        'source': {'database': 'Dictionarry-Hub/database@' + args.database_commit,
                   'schema': 'Dictionarry-Hub/schema@' + args.schema_commit,
                   'ladders': dict(LADDERS), 'excluded': sorted(EXCLUDED), 'skippedDuplicateRows': skipped},
        'step': STEP, 'topTier': top, 'customFormats': formats}, indent=1, sort_keys=True) + '\n')
    print(f'{len(formats)} custom formats; top tier {top}; {skipped} duplicate rows skipped', file=sys.stderr)


if __name__ == '__main__':
    main()
