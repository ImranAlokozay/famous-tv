import copy
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rebuild', ROOT / 'scripts/rebuild_playlist.py')
rebuild = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rebuild)


class CurationTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads((ROOT / 'curation/policy.json').read_text())
        self.channels = json.loads((ROOT / 'curation/channels.json').read_text())

    def channel(self, qualities):
        c = copy.deepcopy(next(c for c in self.channels if c['id'] == 'CBSSportsHQ.us'))
        sample = c['candidates'][0]
        c['candidates'] = [dict(sample, quality=q, url=f'https://{c["approved_hosts"][0]}/{i}.m3u8')
                           for i, q in enumerate(qualities)]
        return c

    def selected_quality(self, qualities):
        chosen, _ = rebuild.select_channels([self.channel(qualities)], self.policy)
        return chosen[0]['selected']['quality']

    def test_576_wins_over_other_formats(self):
        self.assertEqual('576i', self.selected_quality(['1080p', None, '720p', '576i']))
        self.assertEqual('576p', self.selected_quality(['360p', '576p', '720p']))

    def test_720_and_1080_fallbacks_keep_famous_channels(self):
        self.assertEqual('720p', self.selected_quality(['1080p', '720p']))
        self.assertEqual('1080p', self.selected_quality(['1080p']))
        self.assertEqual('1080p', self.selected_quality([None, '1080p']))
        self.assertIsNone(self.selected_quality([None]))
        self.assertEqual('480p', self.selected_quality(['1080p', '480p']))

    def test_duplicate_resolution_country_and_url_variants(self):
        original = self.channel(['720p'])
        variant = copy.deepcopy(original)
        variant['id'] += '-regional'
        variant['name'] += ' HD'
        variant['candidates'][0]['url'] = f'https://{variant["approved_hosts"][0]}/different.m3u8'
        same_url = copy.deepcopy(original)
        same_url['id'] += '-copy'
        same_url['name'] = 'Different listing for the same stream'
        selected, excluded = rebuild.select_channels([original, variant, same_url], self.policy)
        self.assertEqual(1, len(selected))
        self.assertEqual(2, len(excluded))

    def test_language_host_and_attribute_guards(self):
        for field, value in [('languages', ['tam']), ('url', 'https://unknown.example/index.m3u8'),
                             ('url', 'https://example.com/watch'), ('referrer', 'https://example.com/\n#EXTINF:-1,Injected')]:
            c = self.channel(['720p'])
            c['candidates'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                rebuild.select_channels([c], self.policy)

    def test_provider_caps_and_priority(self):
        channel = self.channel(['720p'])
        channel['category'] = 'Movies'
        channel['provider'] = 'Pluto TV'
        copies = []
        for i in range(8):
            c = copy.deepcopy(channel)
            c.update(id=f'channel-{i}', name=f'Channel {i}', priority=i)
            c['candidates'][0]['url'] = f'https://{c["approved_hosts"][0]}/{i}.m3u8'
            copies.append(c)
        selected, _ = rebuild.select_channels(copies, self.policy)
        self.assertEqual(4, len(selected))
        self.assertEqual({'channel-0', 'channel-1', 'channel-2', 'channel-3'}, {c['id'] for c in selected})

    def test_snapshot_preserves_afghanistan_sports_and_language_scope(self):
        selected, _ = rebuild.select_channels(self.channels, self.policy)
        ids = {c['id'] for c in selected}
        self.assertTrue(set(self.policy['baseline_afghanistan_ids']) <= ids)
        self.assertEqual(24, len(self.policy['baseline_afghanistan_ids']))
        self.assertGreaterEqual(sum(c['category'] == 'Sports' for c in selected), 70)
        for c in selected:
            if c['category'] == 'India':
                self.assertTrue(set(c['languages']) <= {'eng', 'hin'})
            if c['category'] == 'Movies':
                self.assertTrue(set(c['languages']) <= set(self.policy['languages']))

    def test_offline_reproducibility_and_header(self):
        targets = json.loads((ROOT / 'curation/targets.json').read_text())
        targets.extend(json.loads((ROOT / 'curation/focused_targets.json').read_text()))
        cricket_targets = json.loads((ROOT / 'curation/cricket_targets.json').read_text())
        cricket_target_names = {target['name'] for target in cricket_targets}
        targets = [target for target in targets if target['name'] not in cricket_target_names]
        targets.extend(dict(target, group='Sports', category='Sports') for target in cricket_targets)
        bein_targets = json.loads((ROOT / 'curation/bein_targets.json').read_text())
        bein_names = {target['name'] for target in bein_targets}
        targets = [target for target in targets if target['name'] not in bein_names]
        targets.extend(dict(target, group='Sports', category='Sports') for target in bein_targets)
        overrides = json.loads((ROOT / 'curation/resolution_overrides.json').read_text())
        promotions = json.loads((ROOT / 'curation/health_promotions.json').read_text())
        generated, report = rebuild.outputs(
            self.channels, self.policy, targets, overrides, promotions, cricket_targets, bein_targets)
        self.assertTrue(generated['public/tv.m3u'].startswith('#EXTM3U\n'))
        self.assertEqual(report['total_channels'], generated['public/tv.m3u'].count('#EXTINF:'))
        for path, content in generated.items():
            self.assertEqual(content.encode(), (ROOT / path).read_bytes(), path)
        self.assertEqual([], report['duplicate_names'])
        self.assertEqual([], report['duplicate_urls'])
        self.assertEqual([], report['hd_regressions'])
        self.assertIn('reports/missing_famous_channels.csv', generated)
        self.assertIn('all_sources_searched', generated['reports/missing_famous_channels.csv'].splitlines()[0])
        self.assertEqual(150, report['resolution_review']['baseline_1080_reviewed'])
        self.assertIn('reports/resolution_replacements.csv', generated)
        self.assertIn('reports/cricket_coverage.csv', generated)
        self.assertEqual(5, report['cricket_coverage']['added_in_this_pass'])
        additions = [row for row in report['cricket_coverage']['channels'] if row['added_in_this_pass']]
        self.assertEqual({'Star Sports 1', 'Star Sports Select 1', 'Star Sports Select 2',
                          'A Sports Pakistan', 'Willow TV'}, {row['requested_channel'] for row in additions})
        for row in additions:
            self.assertTrue(row['decoded_video_verified'])
            self.assertEqual('MANIFEST_ADVANCED', row['live_verification'])
            self.assertIn('not FAST', row['actual_feed_type'])
            self.assertTrue(row['identity_evidence'])
        abc = next(row for row in report['cricket_coverage']['channels']
                   if row['requested_channel'] == 'ABC Cricket (Audio)')
        self.assertEqual('AVAILABLE_AUDIO', abc['availability'])
        self.assertIn('audio-only', abc['actual_feed_type'])
        self.assertEqual(5, len(promotions))
        for promotion in promotions:
            self.assertIn(promotion['new_url'], generated['public/tv.m3u'])
            self.assertNotIn(promotion['old_url'], generated['public/tv.m3u'])

        coverage = {row['requested_channel']: row for row in report['bein_coverage']['channels']}
        self.assertEqual('AVAILABLE', coverage['beIN SPORTS (English)']['availability'])
        self.assertTrue(coverage['beIN SPORTS (English)']['decoded_video_verified'])
        self.assertEqual('UNAVAILABLE', coverage['beIN Sports 2']['availability'])
        self.assertEqual('', coverage['beIN Sports 2']['stream_url'])
        self.assertIn('beINSPORTSXTRA.us', {c['id'] for c in self.channels})


if __name__ == '__main__':
    unittest.main()
