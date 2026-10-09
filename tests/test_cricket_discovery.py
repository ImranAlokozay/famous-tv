import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cricket_discovery', ROOT / 'scripts/discover_cricket.py')
cricket = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cricket)


class CricketDiscoveryTests(unittest.TestCase):
    def test_exact_brand_editions_are_not_conflated(self):
        normalize = cricket.normalized_label
        self.assertEqual(normalize('Star Sports Select 1 (1080p)'), normalize('Star Sports Select 1 SD'))
        self.assertNotEqual(normalize('Willow'), normalize('Willow Extra'))
        self.assertNotEqual(normalize('Willow Sports'), normalize('Willow'))
        self.assertNotEqual(normalize('Star Sports 1'), normalize('Star Sports 1 Hindi'))

    def test_normal_public_paths_and_headers_are_not_rejected(self):
        self.assertEqual(('', ''), cricket.access_condition('http://103.151.60.162:2122/play/a026/index.m3u8?hls', [], {'Referer': 'https://example.org/', 'User-Agent': 'Mozilla/5.0'}))
        self.assertEqual('', cricket.credential_reason('https://cdn.example.org/opaque-channel-id/index.m3u8', []))

    def test_access_conditions_are_explicit(self):
        fixtures = [
            ('http://user:password@example.org/a.m3u8', [], 'URL_USERINFO'),
            ('https://example.org/a.m3u8?token=secret', [], 'ACCESS_QUERY_KEYS'),
            ('https://example.org/live/user/password/123.m3u8', [], 'ACCOUNT_PATH'),
            ('https://example.org/iptv/QRDWGTBMDHSDGK/19146/index.m3u8', [], 'ACCOUNT_TOKEN_PATH'),
            ('https://example.org/a.mpd', ['#KODIPROP:inputstream.adaptive.license_key=secret'], 'DRM_OPTIONS'),
        ]
        for url, options, expected in fixtures:
            self.assertEqual(expected, cricket.access_condition(url, options)[0])

    def test_forbidden_does_not_prove_geo_or_subscription(self):
        classify = cricket.classify_access
        self.assertEqual('UNKNOWN', classify(403, {}, 'Forbidden'))
        self.assertEqual('AUTH_REQUIRED', classify(401, {}))
        self.assertEqual('GEO_RESTRICTED', classify(403, {}, 'Not available in your country'))
        self.assertEqual('TEMPORARY_FAILURE', classify(503, {}))
        self.assertEqual('MISSING_HEADERS', classify(200, {}, documented_headers_worked=True))
        self.assertEqual('DRM_PROTECTED', classify(200, {}, '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="widevine"'))

    def test_skipped_access_is_unknown_and_never_requested(self):
        candidate = dict(channel='PTV Sports', url='https://example.org/a.m3u8?token=secret', headers={}, options=[])
        with patch.object(cricket, 'StreamChecker') as checker, tempfile.TemporaryDirectory() as directory:
            result = cricket.probe_candidate(candidate, Path(directory), 1)
        checker.assert_not_called()
        self.assertEqual('UNKNOWN', result['access_classification'])
        self.assertIsNone(result['http_status'])
        self.assertEqual('NOT_REQUESTED', result['detected_stream_format'])
        self.assertNotIn('secret', result['url'])

    def test_http_evidence_preserves_status_redirect_and_format(self):
        from iptv_health.checker import FetchResponse
        response = FetchResponse(200, 'https://cdn.example.org/master.m3u8',
                                 {'content-type': 'application/vnd.apple.mpegurl', 'set-cookie': 'private'},
                                 b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720\n720.m3u8')
        transport = cricket.EvidenceTransport()
        with patch.object(cricket.UrlLibTransport, 'fetch', return_value=response):
            transport.fetch('https://example.org/live', {'Referer': 'https://example.org/'}, 1, 4096)
        row = transport.requests[0]
        self.assertEqual(200, row['http_status'])
        self.assertEqual(response.final_url, row['redirect_destination'])
        self.assertEqual('hls-master', row['detected_stream_format'])
        self.assertNotIn('set-cookie', row['response_headers'])

    def test_master_snapshot_follows_low_bandwidth_rendition(self):
        from iptv_health.checker import FetchResponse
        master = FetchResponse(200, 'https://example.org/master.m3u8', {},
                               b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n720.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=720x576\n576.m3u8')
        media = FetchResponse(200, 'https://example.org/576.m3u8', {},
                              b'#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:44\n#EXTINF:6,\nseg.ts')
        with patch.object(cricket.UrlLibTransport, 'fetch', side_effect=[master, media]) as fetch:
            snapshot = cricket.live_snapshot('https://example.org/master.m3u8', {}, 1)
        self.assertEqual('https://example.org/576.m3u8', fetch.call_args_list[1].args[0])
        self.assertEqual(44, snapshot['media_sequence'])
        self.assertFalse(snapshot['endlist'])

    def test_skip_audit_states_untested_and_exact_code_condition(self):
        candidate = dict(channel='PTV Sports', url='https://example.org/a.m3u8?token=secret', headers={}, options=[], source='https://example.org/catalog')
        import csv
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            result = cricket.probe_candidate(candidate, output, 1)
            cricket.write_access_report([result], output)
            with (output / 'access_audit.csv').open() as handle:
                row = next(csv.DictReader(handle))
        self.assertIn('query keys intersect', row['skip_code_condition'])
        self.assertEqual('UNKNOWN', row['access_classification'])
        self.assertEqual('', row['http_status'])
        self.assertEqual('NOT_REQUESTED', row['detected_stream_format'])
        self.assertNotIn('secret', str(row))

    def test_audio_only_is_not_video_verification(self):
        check = cricket.dataclasses.make_dataclass('Check', [('status', object), ('verified_media', bool), ('selected_variant_url', str), ('selected_bandwidth', int)])
        from iptv_health.models import HealthStatus
        evidence = check(HealthStatus.WORKING, True, '', 120000)
        from types import SimpleNamespace
        probe = SimpleNamespace(returncode=0, stderr='', stdout='{"streams":[{"codec_type":"audio","codec_name":"aac"}]}')
        candidate = dict(channel='Cricket', url='https://example.org/a.m3u8', headers={}, options=[])
        with patch.object(cricket, 'StreamChecker') as checker, patch.object(cricket.subprocess, 'run', return_value=probe), tempfile.TemporaryDirectory() as directory:
            checker.return_value.check_url.return_value = evidence
            result = cricket.probe_candidate(candidate, Path(directory), 1)
        self.assertEqual('NO_VERIFIED_VIDEO', result['result'])
        self.assertFalse(result['identity_verified'])


if __name__ == '__main__':
    unittest.main()
