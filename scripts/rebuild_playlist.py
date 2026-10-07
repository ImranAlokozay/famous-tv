#!/usr/bin/env python3
"""Rebuild the curated playlist from reviewed metadata; never probe playback URLs."""

import argparse
import collections
import csv
import hashlib
import io
import json
import re
import sys
import unicodedata
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def normalize_name(name):
    name = unicodedata.normalize('NFKD', name).casefold()
    name = re.sub(r'\[(?:geo-blocked|not 24/7)\]|\(\d{3,4}[pi]\)', '', name)
    name = re.sub(r'\b(?:sd|hd|fhd|uhd)\b', '', name)
    return re.sub(r'[^a-z0-9]+', '', name)


def normalize_url(url):
    """Compare URLs without changing playback URLs or dropping signed queries."""
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or '').lower()
    port = p.port
    netloc = host if not port or (p.scheme, port) in [('https', 443), ('http', 80)] else f'{host}:{port}'
    return urllib.parse.urlunsplit((p.scheme.lower(), netloc, p.path, p.query, ''))


def resolution_rank(quality):
    # Source-reported stream quality, not the channel's broadcast format.
    m = re.fullmatch(r'(\d{3,4})([pi])', quality or '')
    if not m:
        return (4, 0)
    height = int(m[1])
    if height == 576:
        return (0, 0)
    if height == 720:
        return (1, 0)
    if height < 720:
        # Retain useful 360/480/540/etc. when the preferred editions are absent.
        return (2, -height)
    if height <= 1080:
        return (3, height)
    return (5, height)


def candidate_rank(candidate):
    official = any(s.startswith('official-') for s in candidate['sources'])
    labels = ' '.join(candidate.get('labels', [])).casefold()
    return (resolution_rank(candidate.get('quality')), not official,
            'geo-blocked' in labels, 'not 24/7' in labels,
            candidate['url'].startswith('http://'), candidate['url'])


def validate_candidate(candidate, channel, policy):
    url = candidate['url']
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ('https', 'http') or p.username or p.password:
        raise ValueError(f'Invalid playback URL: {channel["id"]}')
    if not p.path.endswith(('.m3u8', '.mpd')):
        raise ValueError(f'Not a direct HLS/DASH address: {channel["id"]}')
    if p.hostname not in channel['approved_hosts']:
        raise ValueError(f'Unreviewed host: {channel["id"]}')
    if any(ch in url for ch in '\r\n') or any(ch in url for ch in '<>'):
        raise ValueError(f'Unsafe playlist text: {channel["id"]}')
    if any(source not in policy['sources'] for source in candidate['sources']):
        raise ValueError(f'Unknown source: {channel["id"]}')
    allowed = policy['afghanistan_languages'] if channel['category'] == 'Afghanistan' else policy['languages']
    if channel['category'] == 'India':
        allowed = ['eng', 'hin']
    if not set(candidate['languages']) & set(channel['languages']) & set(allowed):
        raise ValueError(f'Language mismatch: {channel["id"]}')
    quality = candidate.get('quality')
    if quality is not None and not re.fullmatch(r'\d{3,4}[pi]', quality):
        raise ValueError(f'Invalid quality label: {channel["id"]}')
    for value in [channel['name'], channel['id'], channel['category'], channel.get('logo', ''),
                  candidate.get('user_agent') or '', candidate.get('referrer') or '']:
        if any(ch in value for ch in '\r\n"'):
            raise ValueError(f'Unsafe M3U attribute: {channel["id"]}')


def select_channels(channels, policy):
    choices = []
    excluded = []
    for channel in channels:
        if channel['category'] not in policy['category_order']:
            raise ValueError(f'Invalid category: {channel["id"]}')
        if not channel['candidates']:
            excluded.append({'id': channel['id'], 'reason': 'No reviewed candidates'})
            continue
        for candidate in channel['candidates']:
            validate_candidate(candidate, channel, policy)
        candidates = [c for c in channel['candidates'] if resolution_rank(c.get('quality'))[0] < 5]
        if not candidates:
            excluded.append({'id': channel['id'], 'reason': 'Only resolutions above 1080 available'})
            continue
        choices.append(dict(channel, selected=min(candidates, key=candidate_rank)))
    choices.sort(key=lambda c: (c['priority'], candidate_rank(c['selected']), c['id']))
    seen_names, seen_urls, seen_identities = set(), set(), set()
    counts = collections.Counter()
    result = []
    for channel in choices:
        name = normalize_name(channel['name'])
        url = normalize_url(channel['selected']['url'])
        identity = channel.get('identity', channel['id'])
        if name in seen_names or url in seen_urls or identity in seen_identities:
            excluded.append({'id': channel['id'], 'reason': 'Duplicate normalized name, URL, or editorial identity'})
            continue
        key = (channel['category'], channel['provider'])
        cap = policy['provider_caps'].get(key[0], {}).get(key[1])
        if cap is not None and counts[key] >= cap:
            excluded.append({'id': channel['id'], 'reason': f'{key[1]} category cap ({cap})'})
            continue
        result.append(channel)
        seen_names.add(name)
        seen_urls.add(url)
        seen_identities.add(identity)
        counts[key] += 1
    order = {category: i for i, category in enumerate(policy['category_order'])}
    result.sort(key=lambda c: (order[c['category']], c['priority'], c['name'].casefold()))
    return result, excluded


def render_m3u(channels):
    lines = ['#EXTM3U']
    for channel in channels:
        stream = channel['selected']
        cid = channel.get('channel_id', channel['id'])
        feed = stream.get('feed')
        if feed:
            cid += '@' + feed
        name = channel['name']
        if stream.get('quality'):
            name += f' ({stream["quality"]})'
        attrs = {'tvg-id': cid, 'tvg-name': channel['name'], 'tvg-logo': channel.get('logo', ''),
                 'tvg-language': ';'.join(channel['languages']), 'group-title': channel['category']}
        for key in ['user_agent', 'referrer']:
            value = stream.get(key)
            if value:
                attrs['http-' + key.replace('_', '-')] = value
        lines.append('#EXTINF:-1 ' + ' '.join(f'{key}="{value}"' for key, value in attrs.items()) + ',' + name)
        if stream.get('user_agent'):
            lines.append('#EXTVLCOPT:http-user-agent=' + stream['user_agent'])
        if stream.get('referrer'):
            lines.append('#EXTVLCOPT:http-referrer=' + stream['referrer'])
        lines.append(stream['url'])
    return '\n'.join(lines) + '\n'


def routing_check(policy):
    raw = (ROOT / 'vercel.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != policy['vercel_sha256']:
        raise ValueError('vercel.json differs from the reviewed original; no output written')
    config = json.loads(raw)
    if config.get('outputDirectory') != 'public' or not any(
            r.get('source') == '/tv' and r.get('destination') == '/tv.m3u'
            for r in config.get('rewrites', [])):
        raise ValueError('/tv no longer maps to public/tv.m3u')


def build_report(channels, excluded, targets, policy):
    counts = collections.Counter(c['category'] for c in channels)
    quality = collections.Counter(c['selected'].get('quality') or 'unknown' for c in channels)
    rows = []
    for c in channels:
        stream = c['selected']
        heights = {int(re.match(r'\d+', s['quality'])[0]) for s in c['candidates'] if s.get('quality')}
        height = int(re.match(r'\d+', stream['quality'])[0]) if stream.get('quality') else None
        lower = bool(height and any(h < height for h in heights))
        rejected_1080 = any(h >= 1080 for h in heights) and bool(height and height < 1080)
        rows.append({'id': c['id'], 'name': c['name'], 'category': c['category'], 'provider': c['provider'],
                     'languages': c['languages'], 'newly_added': not c['baseline_present'],
                     'resolution': stream.get('quality') or 'unknown', 'lower_resolution_alternative_existed': lower,
                     'preferred_576_or_720_alternative_existed': bool({576, 720} & heights),
                     'avoided_1080': rejected_1080, 'alternative_resolutions': sorted({s.get('quality') or 'unknown' for s in c['candidates']}),
                     'source_urls': [policy['sources'][s]['url'] for s in stream['sources']],
                     'website': c.get('website'), 'stream_url': stream['url'], 'labels': stream.get('labels', []),
                     'candidate_count': len(c['candidates']), 'access_notes': c.get('access_notes', '')})
    selected_ids = {c['id'] for c in channels}
    selected_by_id = {c['id']: c for c in channels}
    coverage = []
    for target in targets:
        matches = [cid for cid in target['ids'] if cid in selected_ids]
        coverage.append(dict(target, status='found' if matches else 'unavailable',
                             reason='Reviewed standalone address found; playback untested.' if matches else target['reason'],
                             found=[{'id': cid, 'name': selected_by_id[cid]['name'],
                                     'resolution': selected_by_id[cid]['selected'].get('quality') or 'unknown'} for cid in matches]))
    names = collections.Counter(normalize_name(c['name']) for c in channels)
    urls = collections.Counter(normalize_url(c['selected']['url']) for c in channels)
    # Sanity checks prevent accidental HD regression in a manually revised inventory.
    hd_regressions = [c['name'] for c in channels if resolution_rank(c['selected'].get('quality'))[0] == 3 and
                      any(resolution_rank(s.get('quality'))[0] < 3 for s in c['candidates'])]
    if hd_regressions:
        raise ValueError(f'Lower-resolution candidates were overlooked: {hd_regressions}')
    return {'snapshot_date': policy['snapshot_date'], 'total_channels': len(channels),
            'categories': {cat: counts[cat] for cat in policy['category_order']},
            'resolution_distribution': dict(sorted(quality.items())),
            'duplicate_names': [name for name, n in names.items() if n > 1],
            'duplicate_urls': [url for url, n in urls.items() if n > 1], 'hd_regressions': hd_regressions,
            'avoided_1080_channels': sum(row['avoided_1080'] for row in rows),
            'newly_added_channels': sum(row['newly_added'] for row in rows),
            'famous_channel_coverage': coverage, 'excluded': excluded, 'channels': rows,
            'verification': {'health_testing_performed': False, 'resolution_basis': 'Published source metadata; playback and bitrate not measured',
                             'routing': 'vercel.json outputDirectory=public; /tv -> /tv.m3u',
                             'vercel_sha256': policy['vercel_sha256'], 'live_deployment_tested': False}}


def markdown_report(report):
    lines = ['# Playlist curation report', '', f'Source snapshot: {report["snapshot_date"]}.', '',
             'The playlist contains public index entries and broadcaster/FAST distribution addresses. '
             'No stream manifests, media segments, playback, or health tests were requested. '
             'Found means a reviewed M3U-compatible address was found, not that playback is confirmed. '
             'Geo restrictions, provider eligibility, scheduled broadcasts, and expiring URLs may apply.', '',
             'Resolution is the published stream label, not a measured bitrate or a bandwidth limit. '
             'Adaptive master playlists can select other renditions during playback. '
             'Unknown stays unknown; SD/HD broadcast tags are not used to infer stream resolution.', '',
             f'Total: **{report["total_channels"]}**; new channel identities: **{report["newly_added_channels"]}**; '
             f'1080 alternatives avoided: **{report["avoided_1080_channels"]}**.', '',
             '| Category | Channels |', '| --- | ---: |']
    lines += [f'| {cat} | {count} |' for cat, count in report['categories'].items()]
    lines += ['', '| Published resolution | Channels |', '| --- | ---: |']
    lines += [f'| {resolution} | {count} |' for resolution, count in report['resolution_distribution'].items()]
    lines += ['', 'Duplicate normalized names: ' + str(len(report['duplicate_names'])) + '. '
              'Duplicate URLs: ' + str(len(report['duplicate_urls'])) + '. '
              '1080 selections with a reviewed lower-resolution candidate: ' + str(len(report['hd_regressions'])) + '.', '',
              '`vercel.json` is byte-for-byte unchanged. Its static configuration still serves '
              '`public/tv.m3u` at `/tv`. This is a routing-config check, not a production HTTP request.', '',
              '## Famous channels found', '', '| Searched channel | Selected edition and resolution |', '| --- | --- |']
    for target in report['famous_channel_coverage']:
        if target['status'] == 'found':
            lines.append('| ' + target['name'] + ' | ' + ', '.join(f'{c["name"]} ({c["resolution"]})' for c in target['found']) + ' |')
    lines += ['', '## Searched but unavailable as a standalone public M3U', '',
              'These are search outcomes within the documented sources, not claims that a channel is unavailable everywhere. '
              'A flagship and a similarly named FAST channel are separate entries. Official YouTube/web players '
              'without a stable permitted HLS address are documented here rather than inserted as playback URLs.', '',
              '| Channel | Reason | Broadcaster/distribution reference |', '| --- | --- | --- |']
    for target in report['famous_channel_coverage']:
        if target['status'] == 'unavailable':
            lines.append(f'| {target["name"]} | {target["reason"]} | {target.get("website", "")} |')
    lines += ['', '## Afghan preservation', '',
              'All 24 original Afghan channel identities remain. The inventory also records additional '
              'public Afghan feeds when found. Preserved addresses have their original index provenance; '
              'they have not been independently health-tested or all verified against broadcaster pages.', '',
              '## Sources, alternatives, and additions', '',
              'See [channels.csv](channels.csv) for every selected channel, including whether it is new, '
              'selected resolution, source used, and whether a lower-resolution alternative existed. '
              '[playlist-report.json](playlist-report.json) contains candidate counts, all alternative '
              'resolution labels, coverage, and excluded duplicate/cap entries. '
              'The reviewed candidates themselves are in [../curation/channels.json](../curation/channels.json).', '',
              '## Scope', '', 'Sports has no count cap. Language and duplicate filtering apply to every category. '
              'Movies and series have small provider caps. Regional Indian-language-only channels, '
              'foreign movie feeds, local-news clones, and unidentified premium restreams are excluded. '
              'No count is padded with filler.', '']
    return '\n'.join(lines)


def outputs(channels, policy, targets):
    routing_check(policy)
    selected, excluded = select_channels(channels, policy)
    if not selected:
        raise ValueError('Refusing an empty playlist')
    report = build_report(selected, excluded, targets, policy)
    buffer = io.StringIO(newline='')
    fields = ['name', 'category', 'provider', 'languages', 'newly_added', 'resolution',
              'lower_resolution_alternative_existed', 'preferred_576_or_720_alternative_existed',
              'alternative_resolutions', 'source_urls', 'website', 'stream_url', 'access_notes']
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator='\n')
    writer.writeheader()
    for row in report['channels']:
        writer.writerow({key: '; '.join(row[key]) if isinstance(row[key], list) else row[key] for key in fields})
    return {'public/tv.m3u': render_m3u(selected),
            'reports/playlist-report.json': json.dumps(report, ensure_ascii=False, indent=2) + '\n',
            'reports/playlist-report.md': markdown_report(report), 'reports/channels.csv': buffer.getvalue()}, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Offline check that committed outputs match the reviewed inventory')
    args = parser.parse_args()
    policy = json.loads((ROOT / 'curation/policy.json').read_text())
    channels = json.loads((ROOT / 'curation/channels.json').read_text())
    targets = json.loads((ROOT / 'curation/targets.json').read_text())
    generated, report = outputs(channels, policy, targets)
    for path, content in generated.items():
        target = ROOT / path
        if args.check:
            if not target.exists() or target.read_bytes() != content.encode('utf-8'):
                raise ValueError(f'{path} needs regeneration')
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode('utf-8'))
    print(json.dumps({key: report[key] for key in ['total_channels', 'categories', 'resolution_distribution',
                                                 'duplicate_names', 'duplicate_urls', 'avoided_1080_channels', 'newly_added_channels']}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
