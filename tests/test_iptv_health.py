import json
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from iptv_health.checker import FetchResponse, SingleCheck, StreamChecker, UrlLibTransport, quality_rank
from iptv_health.discovery import DiscoveryRegistry, identity_score
from iptv_health.maintenance import RepairEngine, run_maintenance
from iptv_health.m3u import load_playlist, parse_m3u, render_playlist
from iptv_health.models import Candidate, HealthResult, HealthStatus

ROOT = Path(__file__).resolve().parents[1]


class FakeTransport:
    def __init__(self, values):
        self.values = {key: list(value) if isinstance(value, list) else [value] for key, value in values.items()}
        self.lock = threading.Lock()
        self.calls = []

    def fetch(self, url, headers, timeout, max_bytes, byte_range=False):
        with self.lock:
            self.calls.append((url, dict(headers), byte_range))
            values = self.values[url]
            value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return value


def response(url, text, status=200, content_type="application/vnd.apple.mpegurl"):
    return FetchResponse(status, url, {"content-type": content_type},
                         text.encode() if isinstance(text, str) else text)


class IptvHealthTests(unittest.TestCase):
    def test_m3u_parser_preserves_metadata_headers_and_order(self):
        raw, entries = load_playlist(ROOT / "tests/fixtures/sample.m3u")
        self.assertEqual(2, len(entries))
        first = entries[0]
        self.assertEqual("Example.us", first.identity_id)
        self.assertEqual("Sports", first.category)
        self.assertEqual("1080p", first.resolution)
        self.assertEqual("FixtureAgent", first.headers["User-Agent"])
        self.assertEqual("https://example.test/", first.headers["Referer"])
        rendered = render_playlist(raw, entries, {0: "https://replacement.example/576.m3u8"})
        self.assertIn('tvg-logo="https://logo.example/example.png"', rendered)
        self.assertIn("#EXTVLCOPT:http-referrer=https://example.test/", rendered)
        self.assertIn("https://replacement.example/576.m3u8", rendered)
        self.assertIn("https://news.example/media.m3u8", rendered)

    def test_m3u_header_attributes_are_accepted(self):
        text = '#EXTM3U x-tvg-url="https://example.test/guide.xml"\n#EXTINF:-1,Test\nhttps://example.test/live.m3u8\n'
        raw, entries = parse_m3u(text)
        self.assertEqual('#EXTM3U x-tvg-url="https://example.test/guide.xml"', raw[0])
        self.assertEqual(1, len(entries))

    def test_hls_master_selects_576_and_verifies_media_object(self):
        master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080
1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1024x576
576.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2200000,RESOLUTION=1280x720
720.m3u8
"""
        media = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nsegment.ts\n"
        transport = FakeTransport({
            "https://test.example/master.m3u8": response("https://test.example/master.m3u8", master),
            "https://test.example/576.m3u8": response("https://test.example/576.m3u8", media),
            "https://test.example/segment.ts": response("https://test.example/segment.ts", b"video", 206, "video/mp2t"),
        })
        result = StreamChecker(transport, retries=0).check_url("https://test.example/master.m3u8")
        self.assertEqual(HealthStatus.WORKING, result.status)
        self.assertEqual("576p", result.selected_resolution)
        self.assertTrue(result.verified_media)

    def test_fmp4_map_is_accepted_as_media_evidence(self):
        media = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6,\nchunk.m4s\n'
        transport = FakeTransport({
            "https://test.example/media.m3u8": response("https://test.example/media.m3u8", media),
            "https://test.example/init.mp4": response("https://test.example/init.mp4", b"init", 206, "video/mp4"),
        })
        result = StreamChecker(transport, retries=0).check_url("https://test.example/media.m3u8")
        self.assertEqual(HealthStatus.WORKING, result.status)
        self.assertEqual("https://test.example/init.mp4", result.segment_url)

    def test_retry_recovers_after_one_hard_failure(self):
        error = urllib.error.HTTPError("https://test.example/live.m3u8", 404, "missing", {}, None)
        media = "#EXTM3U\n#EXTINF:6,\nsegment.ts\n"
        transport = FakeTransport({
            "https://test.example/live.m3u8": [error, response("https://test.example/live.m3u8", media)],
            "https://test.example/segment.ts": response("https://test.example/segment.ts", b"video", 206, "video/mp2t"),
        })
        _, entries = parse_m3u('#EXTM3U\n#EXTINF:-1 tvg-id="Test.us",Test\nhttps://test.example/live.m3u8\n')
        result = StreamChecker(transport, retries=1, retry_backoff=0).check_entry(entries[0], "now")
        self.assertEqual(HealthStatus.WORKING, result.status)
        self.assertEqual(2, len(result.attempts))

    def test_repeated_http_errors_are_classified_conservatively(self):
        missing = urllib.error.HTTPError("https://x.example/a.m3u8", 404, "missing", {}, None)
        forbidden = urllib.error.HTTPError("https://x.example/b.m3u8", 403, "forbidden", {}, None)
        broken = StreamChecker(FakeTransport({"https://x.example/a.m3u8": missing}), retries=1,
                               retry_backoff=0).check_url("https://x.example/a.m3u8")
        geo = StreamChecker(FakeTransport({"https://x.example/b.m3u8": forbidden}), retries=2,
                            retry_backoff=0).check_url("https://x.example/b.m3u8")
        one_failure = StreamChecker(FakeTransport({"https://x.example/a.m3u8": missing}), retries=0,
                                    retry_backoff=0).check_url("https://x.example/a.m3u8")
        self.assertEqual(HealthStatus.BROKEN, broken.status)
        self.assertEqual(HealthStatus.GEO_BLOCKED_OR_FORBIDDEN, geo.status)
        self.assertEqual(HealthStatus.UNKNOWN, one_failure.status)

    def test_timeout_classification(self):
        transport = FakeTransport({"https://x.example/a.m3u8": TimeoutError("timed out")})
        result = StreamChecker(transport, retries=1, retry_backoff=0).check_url("https://x.example/a.m3u8")
        self.assertEqual(HealthStatus.TIMEOUT, result.status)

    def test_hls_media_timeout_is_retried_and_classified(self):
        media = "#EXTM3U\n#EXTINF:6,\nsegment.ts\n"
        transport = FakeTransport({
            "https://x.example/live.m3u8": response("https://x.example/live.m3u8", media),
            "https://x.example/segment.ts": TimeoutError("timed out"),
        })
        result = StreamChecker(transport, retries=1, retry_backoff=0).check_url(
            "https://x.example/live.m3u8")
        self.assertEqual(HealthStatus.TIMEOUT, result.status)

    def test_transport_rejects_local_destinations(self):
        with self.assertRaisesRegex(ValueError, "not allowed"):
            UrlLibTransport().fetch("http://127.0.0.1/live.m3u8", {}, 1, 100)
        with self.assertRaisesRegex(ValueError, "not allowed"):
            UrlLibTransport().fetch("http://localhost/live.m3u8", {}, 1, 100)

    def test_concurrent_entry_checking_preserves_playlist_order(self):
        class ConcurrentChecker(StreamChecker):
            def __init__(self):
                super().__init__(FakeTransport({}), retries=0)
                self.active = 0
                self.maximum = 0
                self.lock = threading.Lock()

            def _check_once(self, url, headers):
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                time.sleep(0.03)
                with self.lock:
                    self.active -= 1
                return SingleCheck(HealthStatus.WORKING, "ok", verified_media=True)

        text = "#EXTM3U\n" + "".join(
            f'#EXTINF:-1 tvg-id="C{i}.us",C{i}\nhttps://example.com/{i}.m3u8\n' for i in range(6))
        _, entries = parse_m3u(text)
        checker = ConcurrentChecker()
        results = checker.check_entries(entries, workers=3, checked_at="now")
        self.assertGreaterEqual(checker.maximum, 2)
        self.assertEqual(list(range(6)), [result.entry_index for result in results])

    def test_discovery_requires_same_identity_and_rejects_fast_substitution(self):
        _, entries = parse_m3u('#EXTM3U\n#EXTINF:-1 tvg-id="ESPN.us",ESPN\nhttps://old.example/a.m3u8\n')
        exact = Candidate("https://new.example/a.m3u8", "test", channel_id="ESPN.us", name="ESPN")
        fast = Candidate("https://new.example/fast.m3u8", "test", name="ESPN FAST")
        other = Candidate("https://new.example/other.m3u8", "test", name="ESPN2")
        self.assertEqual(100, identity_score(entries[0], exact)[0])
        self.assertEqual(0, identity_score(entries[0], fast)[0])
        self.assertEqual(0, identity_score(entries[0], other)[0])

    def test_extensible_m3u_discovery_provider(self):
        catalog = '#EXTM3U\n#EXTINF:-1 tvg-id="Test.us" tvg-name="Test",Test (720p)\nhttps://new.example/test.m3u8\n'
        transport = FakeTransport({"https://catalog.example/list.m3u": response("https://catalog.example/list.m3u", catalog)})
        with tempfile.TemporaryDirectory() as directory:
            registry = DiscoveryRegistry([
                {"name": "catalog", "type": "m3u", "url": "https://catalog.example/list.m3u"}
            ], Path(directory), transport=transport)
            registry.load()
            _, entries = parse_m3u('#EXTM3U\n#EXTINF:-1 tvg-id="Test.us",Test\nhttps://old.example/test.m3u8\n')
            found = registry.discover(entries[0])
        self.assertEqual(1, len(found))
        self.assertEqual("exact tvg-id/channel id", found[0].identity_evidence)

    def test_inventory_discovery_applies_promoted_urls(self):
        records = [{
            "id": "Test.us", "name": "Test", "country": "US", "languages": ["eng"],
            "category": "Sports", "candidates": [{
                "url": "https://old.example/test.m3u8", "quality": "1080p",
                "languages": ["eng"], "user_agent": None, "referrer": None,
            }],
        }]
        promotions = [{
            "id": "Test.us", "old_url": "https://old.example/test.m3u8",
            "new_url": "https://new.example/test.m3u8", "replacement_resolution": "576p",
        }]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "channels.json").write_text(json.dumps(records))
            (root / "promotions.json").write_text(json.dumps(promotions))
            registry = DiscoveryRegistry([{
                "name": "inventory", "type": "inventory", "path": "channels.json",
                "promotions_path": "promotions.json",
            }], root)
            registry.load()
            _, entries = parse_m3u(
                '#EXTM3U\n#EXTINF:-1 tvg-id="Test.us" tvg-language="eng" group-title="Sports",Test\n'
                'https://current.example/test.m3u8\n')
            found = registry.discover(entries[0])
        self.assertEqual(1, len(found))
        self.assertEqual("https://new.example/test.m3u8", found[0].url)
        self.assertEqual("576p", found[0].resolution)

    def test_discovery_source_loading_retries_transient_failure(self):
        catalog = '#EXTM3U\n#EXTINF:-1 tvg-id="Test.us",Test\nhttps://new.example/test.m3u8\n'
        unavailable = urllib.error.HTTPError(
            "https://catalog.example/list.m3u", 503, "unavailable", {}, None)
        transport = FakeTransport({
            "https://catalog.example/list.m3u": [
                unavailable, response("https://catalog.example/list.m3u", catalog)
            ]
        })
        with tempfile.TemporaryDirectory() as directory:
            registry = DiscoveryRegistry(
                [{"name": "catalog", "type": "m3u", "url": "https://catalog.example/list.m3u"}],
                Path(directory), transport=transport, source_retries=1, retry_backoff=0)
            registry.load()
        self.assertEqual([], registry.errors)
        self.assertEqual(2, len(transport.calls))

    def test_resolution_ranking(self):
        self.assertLess(quality_rank("576p"), quality_rank("720p"))
        self.assertLess(quality_rank("720p"), quality_rank("1080p"))

    def test_repair_selects_verified_576_and_prevents_duplicate(self):
        _, entries = parse_m3u('#EXTM3U\n#EXTINF:-1 tvg-id="Test.us" group-title="Sports",Test (1080p)\nhttps://old.example/test.m3u8\n')
        candidates = [
            Candidate("https://new.example/1080.m3u8", "source", channel_id="Test.us", name="Test", resolution="1080p", identity_score=100),
            Candidate("https://new.example/576.m3u8", "source", channel_id="Test.us", name="Test", resolution="576p", identity_score=100),
        ]

        class Registry:
            def discover(self, entry):
                return candidates

        class Checker:
            def check_url(self, url, headers):
                quality = "576p" if "576" in url else "1080p"
                return SingleCheck(HealthStatus.WORKING, "verified", selected_resolution=quality,
                                   verified_media=True, segment_http_status=206)

        health = HealthResult(0, "Test", "Sports", "Test.us", "Test.us",
                              entries[0].url, "1080p", HealthStatus.BROKEN, "404")
        engine = RepairEngine(Checker(), Registry(), max_candidates=5, candidate_workers=2)
        repaired = engine.repair(entries[0], health, "now", {normalize for normalize in []})
        self.assertTrue(repaired.repaired)
        self.assertEqual("576p", repaired.replacement_resolution)
        duplicate = engine.repair(entries[0], health, "now", {candidate.url for candidate in candidates})
        self.assertFalse(duplicate.repaired)

    def test_repair_uses_stable_variant_but_not_session_variant(self):
        _, entries = parse_m3u('#EXTM3U\n#EXTINF:-1 tvg-id="Test.us",Test\nhttps://old.example/test.m3u8\n')

        class Registry:
            def __init__(self, url):
                self.url = url

            def discover(self, entry):
                return [Candidate(self.url, "source", channel_id="Test.us", name="Test",
                                  identity_score=100)]

        class Checker:
            def __init__(self, variant):
                self.variant = variant

            def check_url(self, url, headers):
                return SingleCheck(HealthStatus.WORKING, "verified", selected_resolution="576p",
                                   selected_variant_url=self.variant, verified_media=True,
                                   segment_http_status=206)

        health = HealthResult(0, "Test", "", "Test.us", "Test.us", entries[0].url,
                              "1080p", HealthStatus.BROKEN, "404")
        stable = RepairEngine(
            Checker("https://new.example/low/index.m3u8"),
            Registry("https://new.example/master.m3u8"), candidate_workers=1)
        stable_result = stable.repair(entries[0], health, "now", set())
        self.assertEqual("https://new.example/low/index.m3u8", stable_result.replacement_url)

        session = RepairEngine(
            Checker("https://new.example/session/abc/index.m3u8"),
            Registry("https://new.example/master.m3u8"), candidate_workers=1)
        session_result = session.repair(entries[0], health, "now", set())
        self.assertEqual("https://new.example/master.m3u8", session_result.replacement_url)

    def test_report_and_repaired_playlist_generation(self):
        playlist = '#EXTM3U\n#EXTINF:-1 tvg-id="Broken.us" tvg-logo="logo" group-title="Sports",Broken (1080p)\nhttps://old.example/broken.m3u8\n#EXTINF:-1 tvg-id="Good.us" group-title="News",Good (720p)\nhttps://old.example/good.m3u8\n'
        config = {
            "health": {"workers": 2, "timeout_seconds": 1, "retries": 1},
            "repair": {"max_candidates_per_channel": 2, "candidate_workers": 1},
            "sources": [],
        }

        class FakeChecker:
            def __init__(self, **kwargs):
                pass

            def check_entries(self, entries, workers, checked_at):
                values = []
                for entry in entries:
                    status = HealthStatus.BROKEN if entry.name.startswith("Broken") else HealthStatus.WORKING
                    values.append(HealthResult(entry.index, entry.name, entry.category, entry.tvg_id,
                                               entry.identity_id, entry.url, entry.resolution, status,
                                               "repeated 404" if status == HealthStatus.BROKEN else "",
                                               checked_at=checked_at))
                return values

            def check_url(self, url, headers):
                return SingleCheck(HealthStatus.WORKING, "verified", selected_resolution="576p",
                                   verified_media=True, segment_http_status=206)

        class FakeRegistry:
            def __init__(self, *args, **kwargs):
                self.errors = []

            def load(self):
                pass

            def discover(self, entry):
                return [Candidate("https://new.example/broken-576.m3u8", "fixture",
                                  channel_id=entry.identity_id, name=entry.name,
                                  resolution="576p", identity_score=100,
                                  identity_evidence="exact tvg-id/channel id")]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "public").mkdir()
            (root / "config").mkdir()
            (root / "public/tv.m3u").write_text(playlist)
            (root / "config/iptv_health.json").write_text(json.dumps(config))
            with mock.patch("iptv_health.maintenance.StreamChecker", FakeChecker), \
                 mock.patch("iptv_health.maintenance.DiscoveryRegistry", FakeRegistry):
                result = run_maintenance(
                    root=root, mode="check-repair", playlist_path=root / "public/tv.m3u",
                    config_path=root / "config/iptv_health.json", now=lambda: "2026-01-01T00:00:00Z")
            repaired = (root / "public/tv_repaired.m3u").read_text()
            health = json.loads((root / "reports/iptv_health_report.json").read_text())
            repair = json.loads((root / "reports/iptv_repair_report.json").read_text())
            removals = json.loads((root / "reports/iptv_proposed_removals.json").read_text())
        self.assertEqual(2, health["summary"]["total_channels"])
        self.assertEqual(1, result["repairs"]["successfully_repaired"])
        self.assertEqual(1, repair["summary"]["sports_repaired"])
        self.assertEqual(0, removals["count"])
        self.assertIn('tvg-logo="logo"', repaired)
        self.assertIn("https://new.example/broken-576.m3u8", repaired)
        self.assertIn("https://old.example/good.m3u8", repaired)

    def test_unrepaired_broken_channel_is_retained_and_proposed(self):
        playlist = '#EXTM3U\n#EXTINF:-1 tvg-id="Broken.us" group-title="Sports",Broken\nhttps://old.example/broken.m3u8\n'
        config = {
            "health": {"workers": 1, "timeout_seconds": 1, "retries": 1},
            "repair": {"max_candidates_per_channel": 2, "candidate_workers": 1},
            "sources": [],
        }

        class FakeChecker:
            def __init__(self, **kwargs):
                pass

            def check_entries(self, entries, workers, checked_at):
                entry = entries[0]
                return [HealthResult(entry.index, entry.name, entry.category, entry.tvg_id,
                                     entry.identity_id, entry.url, entry.resolution,
                                     HealthStatus.BROKEN, "repeated 404", checked_at=checked_at)]

        class EmptyRegistry:
            def __init__(self, *args, **kwargs):
                self.errors = []

            def load(self):
                pass

            def discover(self, entry):
                return []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "public").mkdir()
            (root / "config").mkdir()
            (root / "public/tv.m3u").write_text(playlist)
            (root / "config/iptv_health.json").write_text(json.dumps(config))
            with mock.patch("iptv_health.maintenance.StreamChecker", FakeChecker), \
                 mock.patch("iptv_health.maintenance.DiscoveryRegistry", EmptyRegistry):
                run_maintenance(root=root, mode="check-repair",
                                playlist_path=root / "public/tv.m3u",
                                config_path=root / "config/iptv_health.json",
                                now=lambda: "2026-01-01T00:00:00Z")
            repaired = (root / "public/tv_repaired.m3u").read_text()
            proposed = json.loads((root / "reports/iptv_proposed_removals.json").read_text())
        self.assertEqual(playlist, repaired)
        self.assertEqual(1, proposed["count"])
        self.assertFalse(proposed["channels"][0]["repaired"])

    def test_repair_failed_rechecks_only_prior_broken_entries(self):
        playlist = '#EXTM3U\n#EXTINF:-1 tvg-id="A.us",A\nhttps://old.example/a.m3u8\n#EXTINF:-1 tvg-id="B.us",B\nhttps://old.example/b.m3u8\n'
        config = {
            "health": {"workers": 2, "timeout_seconds": 1, "retries": 1},
            "repair": {"max_candidates_per_channel": 2, "candidate_workers": 1},
            "sources": [],
        }
        previous = {
            "channels": [
                {"entry_index": 0, "channel_name": "A", "category": "", "tvg_id": "A.us",
                 "identity_id": "A.us", "original_url": "https://old.example/a.m3u8",
                 "original_resolution": "unknown", "status": "BROKEN", "failure_reason": "404"},
                {"entry_index": 1, "channel_name": "B", "category": "", "tvg_id": "B.us",
                 "identity_id": "B.us", "original_url": "https://old.example/b.m3u8",
                 "original_resolution": "unknown", "status": "WORKING", "failure_reason": ""},
            ]
        }

        class RecoveredChecker:
            checked_names = []

            def __init__(self, **kwargs):
                pass

            def check_entries(self, entries, workers, checked_at):
                self.checked_names[:] = [entry.name for entry in entries]
                entry = entries[0]
                return [HealthResult(entry.index, entry.name, entry.category, entry.tvg_id,
                                     entry.identity_id, entry.url, entry.resolution,
                                     HealthStatus.WORKING, checked_at=checked_at)]

        class EmptyRegistry:
            def __init__(self, *args, **kwargs):
                self.errors = []

            def load(self):
                pass

            def discover(self, entry):
                return []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "public").mkdir()
            (root / "config").mkdir()
            (root / "reports").mkdir()
            (root / "public/tv.m3u").write_text(playlist)
            (root / "config/iptv_health.json").write_text(json.dumps(config))
            prior = root / "reports/prior.json"
            prior.write_text(json.dumps(previous))
            with mock.patch("iptv_health.maintenance.StreamChecker", RecoveredChecker), \
                 mock.patch("iptv_health.maintenance.DiscoveryRegistry", EmptyRegistry):
                result = run_maintenance(root=root, mode="repair-failed",
                                         playlist_path=root / "public/tv.m3u",
                                         config_path=root / "config/iptv_health.json",
                                         previous_report=prior,
                                         now=lambda: "2026-01-01T00:00:00Z")
            health = json.loads((root / "reports/iptv_health_report.json").read_text())
        self.assertEqual(["A"], RecoveredChecker.checked_names)
        self.assertEqual(2, health["summary"]["working"])
        self.assertEqual(0, result["repairs"]["broken_channels_considered"])


if __name__ == "__main__":
    unittest.main()
