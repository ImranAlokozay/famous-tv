#!/usr/bin/env python3
"""Rebuild the curated playlist from reviewed, reproducible metadata."""

import argparse
import copy
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


def apply_resolution_overrides(channels, overrides):
    """Add reviewed stable child renditions without rewriting the baseline inventory."""
    channels = copy.deepcopy(channels)
    by_id = {channel['id']: channel for channel in channels}
    for override in overrides:
        channel = by_id.get(override['id'])
        if channel is None:
            raise ValueError(f'Unknown resolution override channel: {override["id"]}')
        old = next((candidate for candidate in channel['candidates']
                    if candidate['url'] == override['old_url'] and candidate.get('quality') == override['old_resolution']), None)
        if old is None:
            raise ValueError(f'Resolution override no longer matches baseline: {override["id"]}')
        candidate = copy.deepcopy(old)
        candidate.update(url=override['new_url'], quality=override['new_resolution'],
                         sources=list(dict.fromkeys(old['sources'] + [override['source']])))
        candidate['labels'] = list(dict.fromkeys(candidate.get('labels', []) + ['focused stable lower-resolution rendition']))
        channel['candidates'].append(candidate)
        host = urllib.parse.urlsplit(override['new_url']).hostname
        if host not in channel['approved_hosts']:
            channel['approved_hosts'].append(host)
    return channels


def apply_health_promotions(channels, promotions):
    """Promote verified replacement URLs while preserving playlist metadata."""
    channels = copy.deepcopy(channels)
    by_id = {channel['id']: channel for channel in channels}
    for promotion in promotions:
        channel = by_id.get(promotion['id'])
        if channel is None:
            raise ValueError(f'Unknown health promotion channel: {promotion["id"]}')
        candidate = next((item for item in channel['candidates']
                          if item['url'] == promotion['old_url']), None)
        if candidate is None:
            raise ValueError(f'Health promotion no longer matches baseline: {promotion["id"]}')
        candidate['url'] = promotion['new_url']
        candidate['sources'] = list(dict.fromkeys(candidate['sources'] + [promotion['source']]))
        candidate['labels'] = list(dict.fromkeys(
            candidate.get('labels', []) + ['verified automated health-check replacement']))
        host = urllib.parse.urlsplit(promotion['new_url']).hostname
        if host not in channel['approved_hosts']:
            channel['approved_hosts'].append(host)
    return channels


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
                     'languages': c['languages'], 'famous': c.get('famous', False), 'newly_added': not c['baseline_present'],
                     'focused_pass_added': c.get('focused_pass_added', False),
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
                             reason='Reviewed standalone address found; newly added focused-pass streams received a one-time manifest and media-object check.' if matches else target['reason'],
                             found=[{'id': cid, 'name': selected_by_id[cid]['name'],
                                     'resolution': selected_by_id[cid]['selected'].get('quality') or 'unknown'} for cid in matches]))
    names = collections.Counter(normalize_name(c['name']) for c in channels)
    urls = collections.Counter(normalize_url(c['selected']['url']) for c in channels)
    # Sanity checks prevent accidental HD regression in a manually revised inventory.
    hd_regressions = [c['name'] for c in channels if resolution_rank(c['selected'].get('quality'))[0] == 3 and
                      any(resolution_rank(s.get('quality'))[0] < 3 for s in c['candidates'])]
    if hd_regressions:
        raise ValueError(f'Lower-resolution candidates were overlooked: {hd_regressions}')
    bucket = collections.Counter()
    for c in channels:
        q = c['selected'].get('quality')
        if q in ('576i', '576p'):
            bucket['576/576p'] += 1
        elif q in ('720i', '720p'):
            bucket['720p'] += 1
        elif q in ('1080i', '1080p'):
            bucket['1080p'] += 1
        elif q is None:
            bucket['unknown'] += 1
        else:
            bucket['other_known'] += 1
    sports = [c for c in channels if c['category'] == 'Sports']
    sports_bucket = collections.Counter()
    for c in sports:
        q = c['selected'].get('quality')
        key = '576/576p' if q in ('576i', '576p') else '720p' if q in ('720i', '720p') else '1080p' if q in ('1080i', '1080p') else 'unknown' if q is None else 'other_known'
        sports_bucket[key] += 1
    focused = [row for row in rows if row['focused_pass_added']]
    source_contributions = collections.Counter(url for row in focused for url in row['source_urls'])
    host_contributions = collections.Counter(urllib.parse.urlsplit(row['stream_url']).hostname for row in focused)
    return {'snapshot_date': policy['snapshot_date'], 'total_channels': len(channels),
            'categories': {cat: counts[cat] for cat in policy['category_order']},
            'resolution_distribution': dict(sorted(quality.items())),
            'resolution_buckets': {key: bucket[key] for key in ['576/576p', '720p', '1080p', 'unknown', 'other_known']},
            'country_section_counts': {key: counts[key] for key in ['India', 'Pakistan', 'Afghanistan']},
            'pluto_channel_count': sum(c['provider'] == 'Pluto TV' or 'pluto' in c['selected']['url'].casefold() for c in channels),
            'sports_summary': {'total': len(sports), 'resolution_buckets': {key: sports_bucket[key] for key in ['576/576p', '720p', '1080p', 'unknown', 'other_known']}},
            'focused_pass_additions': focused,
            'focused_pass_source_contributions': dict(source_contributions),
            'focused_pass_host_contributions': dict(host_contributions),
            'famous_1080_fallbacks': [row['name'] for row in rows if row['famous'] and row['resolution'] in ('1080i', '1080p')],
            'duplicate_names': [name for name, n in names.items() if n > 1],
            'duplicate_urls': [url for url, n in urls.items() if n > 1], 'hd_regressions': hd_regressions,
            'avoided_1080_channels': sum(row['avoided_1080'] for row in rows),
            'newly_added_channels': sum(row['newly_added'] for row in rows),
            'famous_channel_coverage': coverage, 'excluded': excluded, 'channels': rows,
            'verification': {'health_testing_performed': False,
                             'focused_candidate_testing': 'One-time manifest, selected media-playlist, and first-media-object response checks for focused additions and lower-resolution replacements; no reusable health checker',
                             'resolution_basis': 'Published source metadata plus inspected master-manifest rendition labels for focused-pass additions; bitrate not measured',
                             'routing': 'vercel.json outputDirectory=public; /tv -> /tv.m3u',
                             'vercel_sha256': policy['vercel_sha256'], 'live_deployment_tested': False}}


def markdown_report(report):
    lines = ['# Playlist curation report', '', f'Source snapshot: {report["snapshot_date"]}.', '',
             'The playlist contains public index entries and broadcaster/FAST distribution addresses. '
             'Focused additions and accepted lower-resolution replacements received manifest, media-playlist, and first-media-object response checks. '
             'The full playlist was not health-tested and no reusable health checker was added. '
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
              '[missing_famous_channels.csv](missing_famous_channels.csv) records each requested target still missing, '
              'the sources searched, closest selected alternatives, observed resolution labels, and final reason. '
              '[resolution_replacements.csv](resolution_replacements.csv) records all 149 baseline 1080p channels reviewed, '
              'accepted lower renditions, URLs, sources, and why retained 1080p entries remained. '
              '[cricket_coverage.csv](cricket_coverage.csv) records the focused cricket search, feed type, selected resolution, '
              'source provenance, and live verification result for every requested cricket outlet. '
              'The reviewed candidates themselves are in [../curation/channels.json](../curation/channels.json).', '',
              '## Scope', '', 'Sports has no count cap. Language and duplicate filtering apply to every category. '
              'Movies and series have small provider caps. Regional Indian-language-only channels, '
              'foreign movie feeds, local-news clones, and unidentified premium restreams are excluded. '
              'No count is padded with filler.', '']
    return '\n'.join(lines)


def cricket_coverage_report(selected, targets, policy):
    """Create a reproducible record of cricket coverage without treating substitutes as flagships."""
    selected_by_id = {channel['id']: channel for channel in selected}
    common_sources = policy.get('focused_research_sources', [])
    rows = []
    for target in targets:
        match = next((selected_by_id[cid] for cid in target['ids'] if cid in selected_by_id), None)
        feed_type = target['feed_type']
        if match:
            stream = match['selected']
            lowered = feed_type.casefold()
            availability = ('AVAILABLE_AUDIO' if 'audio' in lowered else
                            'AVAILABLE_GENERAL_BROADCASTER' if 'general' in lowered or 'public national' in lowered else
                            'AVAILABLE')
            source = '; '.join(policy['sources'][key]['url'] for key in stream['sources'])
            rows.append({
                'requested_channel': target['name'], 'country': target['country'],
                'availability': availability, 'playlist_channel': match['name'],
                'actual_feed_type': feed_type,
                'resolution': stream.get('quality') or 'audio/unknown',
                'stream_url': stream['url'], 'url_source': source,
                'verification_result': target['verification_result'],
                'alternatives_checked': '; '.join(target.get('resolutions_found', [])) or 'Selected public feed verified',
                'final_reason': '', 'added_in_this_pass': bool(target.get('added_in_this_pass'))})
        else:
            searched = list(dict.fromkeys(common_sources + ([target['website']] if target.get('website') else [])))
            rows.append({
                'requested_channel': target['name'], 'country': target['country'],
                'availability': 'UNAVAILABLE', 'playlist_channel': '',
                'actual_feed_type': feed_type, 'resolution': 'unavailable',
                'stream_url': '', 'url_source': '; '.join(searched),
                'verification_result': target['verification_result'],
                'alternatives_checked': '; '.join(target.get('resolutions_found', [])) or 'No acceptable public rendition',
                'final_reason': target.get('reason', 'No verified reusable public feed was found.'),
                'added_in_this_pass': False})
    return rows


def outputs(channels, policy, targets, resolution_overrides=None, health_promotions=None, cricket_targets=None):
    routing_check(policy)
    resolution_overrides = resolution_overrides or []
    health_promotions = health_promotions or []
    baseline_selected, _ = select_channels(channels, policy)
    baseline_1080 = {channel['id']: channel for channel in baseline_selected
                     if channel['selected'].get('quality') in ('1080i', '1080p')}
    channels = apply_resolution_overrides(channels, resolution_overrides)
    channels = apply_health_promotions(channels, health_promotions)
    selected, excluded = select_channels(channels, policy)
    if not selected:
        raise ValueError('Refusing an empty playlist')
    report = build_report(selected, excluded, targets, policy)
    cricket_rows = cricket_coverage_report(selected, cricket_targets or [], policy)
    report['cricket_coverage'] = {
        'requested': len(cricket_rows),
        'available': sum(row['availability'] != 'UNAVAILABLE' for row in cricket_rows),
        'missing': sum(row['availability'] == 'UNAVAILABLE' for row in cricket_rows),
        'added_in_this_pass': sum(row['added_in_this_pass'] for row in cricket_rows),
        'channels': cricket_rows}
    selected_by_id = {channel['id']: channel for channel in selected}
    overrides_by_id = {override['id']: override for override in resolution_overrides}
    promotions_by_id = {promotion['id']: promotion for promotion in health_promotions}
    resolution_reviews = []
    search_scope = ('Exact name plus 576/576p/SD/720/720p/M3U/M3U8/HLS across current candidates, '
                    'IPTV indexes, GitHub repositories/code search, broadcaster pages, regional lists, '
                    'public stream directories, alternate feeds, and master-manifest renditions')
    for cid, old in sorted(baseline_1080.items(), key=lambda item: item[1]['name'].casefold()):
        final = selected_by_id[cid]
        override = overrides_by_id.get(cid)
        promotion = promotions_by_id.get(cid)
        changed = final['selected']['url'] != old['selected']['url']
        alternatives = override['alternatives_checked'] if override else sorted(
            {candidate.get('quality') or 'unknown' for candidate in old['candidates']})
        resolution_reviews.append({
            'channel_name': old['name'], 'category': old['category'],
            'old_resolution': old['selected'].get('quality') or 'unknown',
            'new_resolution': (promotion.get('replacement_resolution') if promotion else
                               final['selected'].get('quality')) or 'unknown',
            'old_stream_url': old['selected']['url'], 'new_stream_url': final['selected']['url'],
            'alternatives_checked': alternatives + [search_scope],
            'source_used': policy['sources'][(override or promotion)['source']]['url']
            if (override or promotion) else '; '.join(policy['focused_research_sources']),
            'reason_for_replacement': (override or promotion)['reason'] if changed else '',
            'reason_if_1080_remained': '' if changed else
                'No stable responding 576p or 720p rendition with acceptable provenance survived the focused review; the important/current 1080p channel was retained.'})
    report['resolution_review'] = {
        'baseline_1080_reviewed': len(resolution_reviews),
        'downgraded_total': sum(row['old_resolution'] == '1080p' and row['new_resolution'] != '1080p' for row in resolution_reviews),
        'downgraded_to_576': sum(row['new_resolution'] in ('576i', '576p') for row in resolution_reviews),
        'downgraded_to_720': sum(row['new_resolution'] in ('720i', '720p') for row in resolution_reviews),
        'channels': resolution_reviews}
    buffer = io.StringIO(newline='')
    fields = ['name', 'category', 'provider', 'languages', 'famous', 'newly_added', 'focused_pass_added', 'resolution',
              'lower_resolution_alternative_existed', 'preferred_576_or_720_alternative_existed',
              'alternative_resolutions', 'source_urls', 'website', 'stream_url', 'access_notes']
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator='\n')
    writer.writeheader()
    for row in report['channels']:
        writer.writerow({key: '; '.join(row[key]) if isinstance(row[key], list) else row[key] for key in fields})
    group_category = {'Sony': 'India', 'Colors': 'India', 'Zee': 'India', 'Star': 'India',
                      'Sports': 'Sports', 'Kids': 'Kids', 'News': 'News', 'Documentary': 'Documentary',
                      'Premium': 'Movies', 'Entertainment': 'Entertainment', 'Music': 'Music',
                      'Afghanistan': 'Afghanistan', 'Pakistan': 'Pakistan'}
    group_country = {'Sony': 'India', 'Colors': 'India', 'Zee': 'India', 'Star': 'India',
                     'Afghanistan': 'Afghanistan', 'Pakistan': 'Pakistan'}
    found_by_group = collections.defaultdict(list)
    for target in report['famous_channel_coverage']:
        if target['status'] == 'found':
            found_by_group[target['group']].append(target['name'])
    missing_buffer = io.StringIO(newline='')
    missing_fields = ['channel_name', 'category', 'country', 'all_sources_searched',
                      'closest_alternatives_found', 'resolutions_found', 'final_reason']
    missing_writer = csv.DictWriter(missing_buffer, fieldnames=missing_fields, lineterminator='\n')
    missing_writer.writeheader()
    common_sources = policy.get('focused_research_sources', [])
    for target in report['famous_channel_coverage']:
        if target['status'] != 'unavailable':
            continue
        searched = list(dict.fromkeys(target.get('catalogs_searched', []) + common_sources + ([target['website']] if target.get('website') else [])))
        alternatives = target.get('closest_alternatives') or found_by_group.get(target['group'], [])[:8]
        missing_writer.writerow({
            'channel_name': target['name'],
            'category': target.get('category') or group_category.get(target['group'], target['group']),
            'country': target.get('country') or group_country.get(target['group'], 'International'),
            'all_sources_searched': '; '.join(searched),
            'closest_alternatives_found': '; '.join(alternatives) if alternatives else 'None',
            'resolutions_found': '; '.join(target.get('resolutions_found', [])) or 'No acceptable responding public rendition',
            'final_reason': target['reason']})
    replacement_buffer = io.StringIO(newline='')
    replacement_fields = ['channel_name', 'category', 'old_resolution', 'new_resolution',
                          'old_stream_url', 'new_stream_url', 'alternatives_checked', 'source_used',
                          'reason_for_replacement', 'reason_if_1080_remained']
    replacement_writer = csv.DictWriter(replacement_buffer, fieldnames=replacement_fields, lineterminator='\n')
    replacement_writer.writeheader()
    for row in resolution_reviews:
        replacement_writer.writerow({key: '; '.join(row[key]) if isinstance(row[key], list) else row[key]
                                     for key in replacement_fields})
    cricket_buffer = io.StringIO(newline='')
    cricket_fields = ['requested_channel', 'country', 'availability', 'playlist_channel', 'actual_feed_type',
                      'resolution', 'stream_url', 'url_source', 'verification_result', 'alternatives_checked',
                      'final_reason', 'added_in_this_pass']
    cricket_writer = csv.DictWriter(cricket_buffer, fieldnames=cricket_fields, lineterminator='\n')
    cricket_writer.writeheader()
    for row in cricket_rows:
        cricket_writer.writerow({key: row[key] for key in cricket_fields})
    return {'public/tv.m3u': render_m3u(selected),
            'reports/playlist-report.json': json.dumps(report, ensure_ascii=False, indent=2) + '\n',
            'reports/playlist-report.md': markdown_report(report), 'reports/channels.csv': buffer.getvalue(),
            'reports/missing_famous_channels.csv': missing_buffer.getvalue(),
            'reports/resolution_replacements.csv': replacement_buffer.getvalue(),
            'reports/cricket_coverage.csv': cricket_buffer.getvalue()}, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Offline check that committed outputs match the reviewed inventory')
    args = parser.parse_args()
    policy = json.loads((ROOT / 'curation/policy.json').read_text())
    channels = json.loads((ROOT / 'curation/channels.json').read_text())
    targets = json.loads((ROOT / 'curation/targets.json').read_text())
    focused_targets = ROOT / 'curation/focused_targets.json'
    if focused_targets.exists():
        targets.extend(json.loads(focused_targets.read_text()))
    cricket_targets_path = ROOT / 'curation/cricket_targets.json'
    cricket_targets = json.loads(cricket_targets_path.read_text()) if cricket_targets_path.exists() else []
    cricket_target_names = {target['name'] for target in cricket_targets}
    targets = [target for target in targets if target['name'] not in cricket_target_names]
    targets.extend(dict(target, group='Sports', category='Sports') for target in cricket_targets)
    resolution_overrides = json.loads((ROOT / 'curation/resolution_overrides.json').read_text())
    health_promotions = json.loads((ROOT / 'curation/health_promotions.json').read_text())
    generated, report = outputs(channels, policy, targets, resolution_overrides, health_promotions, cricket_targets)
    for path, content in generated.items():
        target = ROOT / path
        if args.check:
            if not target.exists() or target.read_bytes() != content.encode('utf-8'):
                raise ValueError(f'{path} needs regeneration')
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode('utf-8'))
    print(json.dumps({key: report[key] for key in ['total_channels', 'categories', 'resolution_distribution',
                                                 'duplicate_names', 'duplicate_urls', 'avoided_1080_channels',
                                                 'newly_added_channels']}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
