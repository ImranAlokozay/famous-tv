#!/usr/bin/env python3
"""Collect public cricket candidates and verify video; never modify the playlist.

Run with --output .cache/cricket-discovery. The JSON evidence and frame captures
are for editorial review. A source label alone never establishes channel identity.
No login, credential lines, DRM keys, or token stripping are used.
"""
import argparse
import concurrent.futures
import csv
import dataclasses
import datetime
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from iptv_health.checker import StreamChecker, UrlLibTransport, choose_variant, parse_master_variants
from iptv_health.m3u import parse_m3u


def normalized_label(name):
    name = name.casefold().strip()
    name = re.sub(r'\[[^\]]*\]|\([^)]*\)', '', name)
    name = re.sub(r'^\s*(?:\d+[.]|(?:in|pk|cr|bd|au|uk|usa)\s*[:|-])\s*', '', name)
    name = re.sub(r'\b(?:hd|sd|fhd|uhd|4k|\d{3,4}[pi]|backup)\b', '', name)
    return re.sub(r'[^a-z0-9+]+', '', name)


def credential_reason(url, options):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname:
        return 'Not an HTTP(S) stream'
    if parsed.username or parsed.password:
        return 'Embedded credentials'
    query = {key.casefold() for key, _ in urllib.parse.parse_qsl(parsed.query)}
    if query & {'token', 'ticket', 'password', 'username', 'mac', 'play_token', 'wmsauthsign', 'hdnts', 'hdnea', 'auth'}:
        return 'Credential/token/session-specific published URL; not reusable without access tokens'
    if re.search(r'/(?:live|iptv)/[^/]+/[^/]+/\d+(?:[./]|$)', parsed.path):
        return 'Credential-shaped IPTV line'
    parts = parsed.path.strip('/').split('/')
    if (re.fullmatch(r'/[A-Za-z0-9]{6,}/[A-Za-z0-9]{6,}/\d+(?:\.[A-Za-z0-9]+)?/?', parsed.path)
            and any(character.isdigit() for part in parts[:2] for character in part)):
        return 'Credential-shaped IPTV line'
    if re.search(r'/iptv/[A-Z0-9]{10,}/\d+/', parsed.path):
        return 'Account-token-shaped IPTV line'
    if any(re.search(r'license_key|license_type|drmlicense=|drmscheme=', item, re.I) for item in options):
        return 'DRM/license parameters required'
    if any(re.search(r'cookie=|authorization=|drmlicense=|drmscheme=', item, re.I) for item in options):
        return 'Published cookie, authorization, or DRM access parameters'
    return ''


def access_condition(url, options, headers=None):
    """An exclusion is an untested access concern, never proof of subscription/geo status."""
    reason = credential_reason(url, options)
    if any(key.casefold() in ('cookie', 'authorization') for key in (headers or {})):
        reason = 'Request requires copied cookies or authorization'
    conditions = {
        'Not an HTTP(S) stream': 'NON_HTTP_URL',
        'Embedded credentials': 'URL_USERINFO',
        'Credential/token/session-specific published URL; not reusable without access tokens': 'ACCESS_QUERY_KEYS',
        'Credential-shaped IPTV line': 'ACCOUNT_PATH',
        'Account-token-shaped IPTV line': 'ACCOUNT_TOKEN_PATH',
        'DRM/license parameters required': 'DRM_OPTIONS',
        'Published cookie, authorization, or DRM access parameters': 'ACCESS_OPTIONS',
        'Request requires copied cookies or authorization': 'ACCESS_HEADERS',
    }
    return conditions.get(reason, ''), reason


def classify_access(status, headers, body='', documented_headers_worked=False):
    text = body.casefold()
    if documented_headers_worked:
        return 'MISSING_HEADERS'
    if '#ext-x-key' in text and ('sample-aes' in text or 'widevine' in text or 'fairplay' in text):
        return 'DRM_PROTECTED'
    if '<mpd' in text and ('<contentprotection' in text or '<cenc:pssh' in text):
        return 'DRM_PROTECTED'
    if status == 401 or 'www-authenticate' in headers or (status == 403 and any(w in text for w in ('login required', 'authentication required', 'subscription required'))):
        return 'AUTH_REQUIRED'
    if status == 451 or (status == 403 and any(w in text for w in ('not available in your country', 'geo-block', 'geo restricted', 'geographically restricted'))):
        return 'GEO_RESTRICTED'
    if status == 429 or (status and 500 <= status < 600):
        return 'TEMPORARY_FAILURE'
    return 'UNKNOWN'


class EvidenceTransport(UrlLibTransport):
    def __init__(self):
        self.requests = []

    def fetch(self, url, headers, timeout, max_bytes, byte_range=False):
        try:
            response = super().fetch(url, headers, timeout, max_bytes, byte_range)
            body = response.body.decode('utf-8', 'replace')
            detected = ('hls-master' if '#EXT-X-STREAM-INF' in body else
                        'hls-media' if '#EXTINF' in body and '#EXTM3U' in body else
                        'm3u' if body.lstrip().startswith('#EXTM3U') else
                        'dash' if '<MPD' in body else response.headers.get('content-type', 'unknown'))
            self.requests.append({'url': url, 'http_status': response.status,
                                  'redirect_destination': response.final_url,
                                  'response_headers': {k: v for k, v in response.headers.items() if k != 'set-cookie'},
                                  'detected_stream_format': detected,
                                  'access_classification': classify_access(response.status, response.headers, body)})
            return response
        except urllib.error.HTTPError as error:
            response_headers = {k.casefold(): v for k, v in error.headers.items() if k.casefold() != 'set-cookie'}
            body = error.read(4096).decode('utf-8', 'replace')
            self.requests.append({'url': url, 'http_status': error.code, 'redirect_destination': error.geturl(),
                                  'response_headers': response_headers, 'detected_stream_format': response_headers.get('content-type', 'unknown'),
                                  'access_classification': classify_access(error.code, response_headers, body)})
            raise
        except Exception as error:
            self.requests.append({'url': url, 'http_status': None, 'redirect_destination': '',
                                  'response_headers': {}, 'detected_stream_format': 'unknown',
                                  'access_classification': 'TEMPORARY_FAILURE' if isinstance(error, TimeoutError) else 'UNKNOWN',
                                  'error': f'{type(error).__name__}: {error}'})
            raise


def fetch_source(source, aliases, output, timeout):
    row = dict(source, candidates=0, retrieved_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    candidates = []
    try:
        req = urllib.request.Request(source['url'], headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read(8_000_000)
            row.update(http_status=response.status, final_url=response.geturl(), sha256=hashlib.sha256(body).hexdigest())
        text = body.decode('utf-8-sig', 'replace')
        cache = output / 'sources' / (hashlib.sha256(source['url'].encode()).hexdigest()[:16] + '.txt')
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(body)
        if source.get('format') == 'website':
            row['result'] = 'Page inspected; requires channel-specific extraction/editorial review'
            row['hls_references'] = sorted(set(re.findall(r'https?[^\s"\'<>]+\.m3u8[^\s"\'<>]*', text)))[:20]
            return row, candidates
        # Regional lists sometimes have headers without EXTM3U or an unterminated trailing entry.
        if not text.lstrip().startswith('#EXTM3U'):
            text = '#EXTM3U\n' + text
        blocks = re.split(r'(?m)(?=^#EXTINF:)', text)
        for block in blocks[1:]:
            try:
                _, entries = parse_m3u('#EXTM3U\n' + block)
            except ValueError:
                continue
            for entry in entries:
                name = aliases.get(normalized_label(entry.name))
                if not name:
                    continue
                url, separator, inline_headers = entry.url.partition('|')
                headers = dict(entry.headers)
                if separator:
                    entry.options.append(inline_headers)
                    for key, value in urllib.parse.parse_qsl(inline_headers):
                        if key.casefold() in ('referer', 'referrer', 'user-agent'):
                            headers['User-Agent' if key.casefold() == 'user-agent' else 'Referer'] = value
                candidates.append({'channel': name, 'catalog_label': entry.name,
                                   'url': url, 'headers': headers, 'options': entry.options,
                                   'tvg_id': entry.tvg_id, 'source': source['url'],
                                   'source_name': source['name'], 'reported_resolution': entry.resolution})
        row.update(result='Catalog parsed', candidates=len(candidates))
    except Exception as error:
        row['result'] = f'{type(error).__name__}: {error}'
    return row, candidates


def media_args(url, headers, timeout):
    args = ['-rw_timeout', str(int(timeout * 1_000_000))]
    if headers:
        args += ['-headers', ''.join(f'{key}: {value}\r\n' for key, value in headers.items())]
    return args + ['-i', url]


def live_snapshot(url, headers, timeout):
    transport = UrlLibTransport()
    for _ in range(3):
        response = transport.fetch(url, headers, timeout, 2_000_000)
        text = response.body.decode('utf-8', 'replace')
        variants = parse_master_variants(text, response.final_url)
        if variants:
            url = choose_variant(variants).url
            continue
        sequence = re.search(r'#EXT-X-MEDIA-SEQUENCE:(\d+)', text)
        target = re.search(r'#EXT-X-TARGETDURATION:(\d+)', text)
        return {'sha256': hashlib.sha256(response.body).hexdigest(),
                'media_sequence': int(sequence[1]) if sequence else None,
                'target_duration': int(target[1]) if target else 6,
                'endlist': '#EXT-X-ENDLIST' in text,
                'segment_count': text.count('#EXTINF:')}
    raise ValueError('Too many nested master playlists')


def probe_candidate(candidate, output, timeout):
    row = {**candidate, 'verified_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
           'identity_verified': False, 'identity_evidence': 'Source label only; requires visual/editorial verification'}
    row['candidate_id'] = hashlib.sha256((candidate['channel'] + candidate['url']).encode()).hexdigest()[:20]
    row['published_query_keys'] = sorted(key for key, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(candidate['url']).query))
    condition, reason = access_condition(candidate['url'], candidate['options'], candidate['headers'])
    if reason:
        row.update(result='SKIPPED_ACCESS', reason=reason, skip_condition=condition,
                   access_classification='DRM_PROTECTED' if condition == 'DRM_OPTIONS' else 'UNKNOWN',
                   http_requests=[], http_status=None, redirect_destination='', response_headers={},
                   detected_stream_format='NOT_REQUESTED',
                   retest_advice='Find a credential-free distribution or obtain provider documentation. No HTTP request made with copied account/access parameters; subscription/geo status unconfirmed.')
        # Never publish copied credential/token URLs in evidence reports.
        parsed = urllib.parse.urlsplit(candidate['url'])
        row['url'] = urllib.parse.urlunsplit((parsed.scheme, parsed.hostname or '', '/[access-controlled]', '', ''))
        row['options'], row['headers'] = [], {}
        return row
    try:
        transport = EvidenceTransport()
        row['http_requests'] = transport.requests
        check = StreamChecker(transport=transport, timeout=timeout, retries=1, retry_backoff=.1).check_url(candidate['url'], candidate['headers'])
        row['access_classification'] = transport.requests[-1]['access_classification'] if transport.requests else 'UNKNOWN'
        if candidate['headers'] and check.verified_media:
            baseline = EvidenceTransport()
            try:
                baseline.fetch(candidate['url'], {}, timeout, 4096)
            except Exception:
                pass
            row['without_documented_headers'] = baseline.requests
            if baseline.requests and baseline.requests[-1]['http_status'] == 403:
                row['access_classification'] = 'MISSING_HEADERS'
        row['http_hls_evidence'] = dataclasses.asdict(check)
        row['http_hls_evidence']['status'] = check.status.value
        if row['access_classification'] == 'DRM_PROTECTED':
            row.update(result='DRM_PROTECTED', reason='Manifest declares protected media; no license or decryption keys were used')
            return row
        if not check.verified_media:
            row.update(result=check.status.value, reason=check.reason)
            return row
        selected_url = check.selected_variant_url or candidate['url']
        # Keep the stable published endpoint when its selected rendition is session-specific.
        if credential_reason(selected_url, []):
            selected_url = candidate['url']
        row['tested_url'] = selected_url
        args = media_args(selected_url, candidate['headers'], timeout)
        probe = subprocess.run(['ffprobe', '-v', 'error', '-analyzeduration', '2500000', '-probesize', '3000000',
                                *args, '-show_entries', 'stream=codec_type,codec_name,width,height,bit_rate,r_frame_rate:format=bit_rate',
                                '-of', 'json'], capture_output=True, text=True, timeout=timeout * 2 + 5)
        row['ffprobe_exit_code'] = probe.returncode
        row['ffprobe_error'] = probe.stderr[-1800:]
        row['ffprobe'] = json.loads(probe.stdout or '{}')
        video = next((stream for stream in row['ffprobe'].get('streams', []) if stream.get('codec_type') == 'video'), None)
        if not video:
            row.update(result='NO_VERIFIED_VIDEO', reason='FFprobe found no decodable video stream')
            return row
        row.update(resolution=f"{video.get('height', 0)}p", codec=video.get('codec_name'),
                   bitrate=video.get('bit_rate') or row['ffprobe'].get('format', {}).get('bit_rate') or check.selected_bandwidth,
                   bitrate_basis='FFprobe stream/format bitrate or HLS advertised bandwidth; no inferred bitrate')
        decode = subprocess.run(['ffmpeg', '-v', 'error', *args, '-map', '0:v:0', '-frames:v', '3',
                                 '-an', '-progress', 'pipe:1', '-f', 'null', '-'],
                                capture_output=True, text=True, timeout=timeout * 2 + 5)
        frames = re.findall(r'(?m)^frame=(\d+)', decode.stdout)
        row.update(ffmpeg_exit_code=decode.returncode, decoded_frames=max(map(int, frames), default=0),
                   ffmpeg_error=decode.stderr[-1800:])
        if decode.returncode != 0 or row['decoded_frames'] < 3:
            row.update(result='VIDEO_DECODE_FAILED', reason='FFmpeg did not decode three video frames')
            return row
        key = hashlib.sha256(candidate['url'].encode()).hexdigest()[:16]
        image_path = output / 'frames' / f'{key}.jpg'
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image = subprocess.run(['ffmpeg', '-v', 'error', '-y', *args, '-map', '0:v:0', '-frames:v', '1',
                                str(image_path)], capture_output=True, text=True, timeout=timeout * 2 + 5)
        if image.returncode == 0 and image_path.exists():
            row['frame_path'] = str(image_path)
        row.update(result='VIDEO_VERIFIED_PENDING_IDENTITY', reason='FFprobe found video and FFmpeg decoded three frames; editorial channel identity check pending')
        try:
            first = live_snapshot(candidate['url'], candidate['headers'], timeout)
            time.sleep(min(12, max(3, first['target_duration'] * 2)))
            second = live_snapshot(candidate['url'], candidate['headers'], timeout)
            row['live_evidence'] = {'first': first, 'second': second,
                                    'result': 'ON_DEMAND' if second['endlist'] else
                                    'MANIFEST_ADVANCED' if first['sha256'] != second['sha256'] else 'NO_ADVANCE_OBSERVED'}
        except Exception as error:
            row['live_evidence'] = {'result': 'UNKNOWN', 'reason': str(error)}
    except Exception as error:
        row.update(result='PROBE_ERROR', reason=f'{type(error).__name__}: {error}')
    return row


def write_access_report(results, output):
    predicates = {
        'NON_HTTP_URL': "scheme not in ('https', 'http') or hostname absent",
        'URL_USERINFO': 'parsed.username or parsed.password',
        'ACCESS_QUERY_KEYS': "query keys intersect {token,ticket,password,username,mac,play_token,wmsauthsign,hdnts,hdnea,auth}",
        'ACCOUNT_PATH': (r're.search(/(?:live|iptv)/[^/]+/[^/]+/\d+(?:[./]|$), path) or '
                         r're.fullmatch(/[A-Za-z0-9]{6,}/[A-Za-z0-9]{6,}/\d+(?:\.[A-Za-z0-9]+)?/?, path) '
                         'and any(character.isdigit() for part in path.strip(/).split(/)[:2] for character in part)'),
        'ACCOUNT_TOKEN_PATH': r'path matches /iptv/[A-Z0-9]{10,}/\d+/',
        'DRM_OPTIONS': 'options match license_key|license_type|drmlicense=|drmscheme=',
        'ACCESS_OPTIONS': 'options contain cookie= or authorization=',
        'ACCESS_HEADERS': 'headers contain Cookie or Authorization',
    }
    fields = ['candidate_id', 'channel', 'catalog_label', 'url', 'source', 'result', 'skip_condition',
              'access_classification', 'http_status', 'redirect_destination', 'response_headers',
              'detected_stream_format', 'reason', 'retest_advice', 'published_query_keys', 'skip_code_condition']
    rows = []
    for result in results:
        row = {key: result.get(key, '') for key in fields}
        row['skip_code_condition'] = predicates.get(row['skip_condition'], '')
        requests = result.get('http_requests', [])
        if requests:
            row.update({key: requests[0].get(key, '') for key in ('http_status', 'redirect_destination', 'response_headers', 'detected_stream_format')})
        elif result['result'] != 'SKIPPED_ACCESS':
            check = result.get('http_hls_evidence', {})
            row.update(http_status=check.get('http_status'), redirect_destination=check.get('final_url', ''),
                       detected_stream_format=check.get('manifest_type', 'unknown'), response_headers={},
                       retest_advice='Earlier evidence lacks full response headers; rerun without --reuse-evidence for fresh HTTP audit.')
        rows.append(row)
    (output / 'access_audit.json').write_text(json.dumps(rows, indent=2) + '\n')
    with (output / 'access_audit.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row, response_headers=json.dumps(row['response_headers'], sort_keys=True)))


def configured_candidate(candidate):
    """Preserve publicly documented playback headers/options on explicit candidates."""
    return dict(candidate, options=list(candidate.get('options', [])),
                headers=dict(candidate.get('headers', {})), sources=[candidate['source']],
                catalog_label=candidate.get('catalog_label', candidate['channel']),
                reported_resolution=candidate.get('reported_resolution', 'unknown'),
                tvg_id=candidate.get('tvg_id', ''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/cricket_discovery.json')
    parser.add_argument('--output', type=Path, default=ROOT / '.cache/cricket-discovery')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--catalogs-only', action='store_true')
    parser.add_argument('--reuse-evidence', action='store_true', help='Reuse prior results from this output directory; timestamps remain explicit')
    parser.add_argument('--channel', action='append', help='Verify only this exact canonical channel (repeatable)')
    args = parser.parse_args()
    if args.workers < 1 or args.timeout <= 0:
        parser.error('workers and timeout must be positive')
    config = json.loads(args.config.read_text())
    prior = {}
    evidence_path = args.output / 'discovery.json'
    if args.reuse_evidence and evidence_path.exists():
        for row in json.loads(evidence_path.read_text())['results']:
            prior[(row['channel'], row['url'], tuple(sorted(row['headers'].items())))] = row
    aliases = {normalized_label(alias): name for name, names in config['aliases'].items() for alias in names}
    args.output.mkdir(parents=True, exist_ok=True)
    sources, candidates = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch_source, source, aliases, args.output, args.timeout) for source in config['sources']]
        for future in concurrent.futures.as_completed(futures):
            source, found = future.result()
            sources.append(source)
            candidates.extend(found)
            print(source['name'], source['result'], len(found), flush=True)
    deduplicated = {}
    for candidate in candidates:
        key = (candidate['channel'], candidate['url'], tuple(sorted(candidate['headers'].items())))
        if key not in deduplicated:
            deduplicated[key] = dict(candidate, sources=[])
        deduplicated[key]['sources'].append(candidate['source'])
    candidates = list(deduplicated.values())
    for candidate in config.get('candidates', []):
        candidates.append(configured_candidate(candidate))
    unique = {}
    for candidate in candidates:
        key = (candidate['channel'], candidate['url'], tuple(sorted(candidate['headers'].items())))
        if key not in unique:
            unique[key] = candidate
        else:
            unique[key]['sources'] = list(dict.fromkeys(unique[key]['sources'] + candidate['sources']))
    candidates = list(unique.values())
    if args.channel:
        candidates = [row for row in candidates if row['channel'] in args.channel]
    results = []
    if not args.catalogs_only:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = []
            for candidate in candidates:
                key = (candidate['channel'], candidate['url'], tuple(sorted(candidate['headers'].items())))
                cached = prior.get(key)
                if cached and not access_condition(candidate['url'], candidate['options'], candidate['headers'])[1]:
                    results.append(cached)
                else:
                    futures.append(pool.submit(probe_candidate, candidate, args.output, args.timeout))
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                results.append(result)
                print(result['channel'], result['result'], result.get('resolution', ''), flush=True)
    payload = {'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'sources': sorted(sources, key=lambda item: item['name']),
               'candidate_count': len(candidates), 'results': sorted(results, key=lambda item: (item['channel'], item['url']))}
    (args.output / 'discovery.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n')
    write_access_report(results, args.output)
    print(json.dumps({'sources': len(sources), 'candidate_count': len(candidates),
                      'video_verified_pending_identity': sum(r['result'] == 'VIDEO_VERIFIED_PENDING_IDENTITY' for r in results)}, indent=2))


if __name__ == '__main__':
    main()
