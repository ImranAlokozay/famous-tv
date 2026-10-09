# famous-tv

The curated playlist is [public/tv.m3u](public/tv.m3u). The existing Vercel
configuration serves it at `/tv`. `vercel.json` is unchanged.

## Rebuild

Python 3.10+ and its standard library are sufficient. No dependencies, network
requests, stream probes, or health checker are needed:

```sh
python scripts/rebuild_playlist.py
python scripts/rebuild_playlist.py --check
python -m unittest discover -s tests
```

The generator reads the reviewed alternatives in [curation/channels.json](curation/channels.json),
the selection rules and source provenance in [curation/policy.json](curation/policy.json),
the requested channel coverage in [curation/targets.json](curation/targets.json),
the exact focused-pass targets in [curation/focused_targets.json](curation/focused_targets.json),
and reviewed URL-only health promotions in
[curation/health_promotions.json](curation/health_promotions.json).
It produces the playlist and [reports/playlist-report.md](reports/playlist-report.md),
[reports/playlist-report.json](reports/playlist-report.json), and
[reports/channels.csv](reports/channels.csv), plus the detailed
[reports/missing_famous_channels.csv](reports/missing_famous_channels.csv) and
[reports/resolution_replacements.csv](reports/resolution_replacements.csv). All outputs are deterministic.

The original root-level `famous_global_india_pakistan_afghanistan.m3u` is retained
as a historical reference. `/tv` serves `public/tv.m3u`, not that older file.

## Curation

Choose one editorial channel identity and one stream URL globally. Prefer
published **576i/576p**, then **720p**, then other known SD formats, then **1080**
when no reviewed lower resolution is available. An unknown label is kept unknown.
No URL suffixes or rendition paths are invented. Important 1080-only channels
remain in the playlist. Published quality describes source metadata, not an
enforced bandwidth limit; an adaptive master may choose other renditions.

The inventory favors English, Hindi, Urdu, Persian, and Arabic. The Afghanistan
section additionally permits Pashto and Dari, and preserves all 24 original
Afghan channel identities. India's section permits Hindi/English; DD Urdu is
placed outside that section. Different English and Arabic news editions have
separate names because their programming/audio differs. Country/resolution
copies of the same editorial stream are collapsed.

Movies/series have small provider caps; Stingray music and archival TVS sports
also have caps. Sports has no overall channel-count cap. Identified FAST brand
editions are named separately from subscription flagships. Anonymous premium
restreams, ticket/MAC URLs, incorrect channel identities, and nonpreferred
language editions are not used to fill gaps.

For additional channels, add a reviewed inventory record with explicit language,
source references, an approved host, and candidate URLs copied from an index or
official broadcaster page. Review provenance, feed identity, provider eligibility,
and audio language before including an address. A generic CDN hostname alone is
not enough to establish those facts. Edit the coverage targets when appropriate,
then rebuild. Use the original index URLs and provenance references in `policy.json`
to research updates. Source SHA-256 values document the catalogs used for this
snapshot; the smaller reviewed inventory is committed for offline reproduction.

Reports distinguish a found address from verified playback. Focused-pass additions and
accepted lower-resolution replacements received manifest, media-playlist, and
first-media-object response checks before their metadata was committed. Region
restrictions, eligibility, schedules, and expiry can apply.

## IPTV health checker and repair proposals

The reusable maintenance command checks every entry concurrently, follows redirects,
honors M3U request headers, inspects adaptive HLS renditions, and probes an actual media
object. Results are deliberately conservative: a single failure is not enough to label a
channel broken, and forbidden or geographically restricted streams are kept separate
from confirmed failures.

```sh
# Reports only; never writes a repaired playlist
python scripts/iptv_maintenance.py --mode health-only

# Check, search configured public catalogs, verify candidates, and build a proposal
python scripts/iptv_maintenance.py --mode check-repair

# Recheck only entries marked BROKEN in an earlier health report
python scripts/iptv_maintenance.py --mode repair-failed \
  --previous-report reports/iptv_health_report.json
```

Workers, per-request timeout, and retry count can be overridden with `--workers`,
`--timeout`, and `--retries`. Defaults and discovery sources live in
[`config/iptv_health.json`](config/iptv_health.json). New catalog integrations implement
the provider interface in [`iptv_health/discovery.py`](iptv_health/discovery.py); a source
failure is recorded without aborting the run.

Repair mode creates `public/tv_repaired.m3u` and detailed CSV/JSON health, repair, and
proposed-removal reports under `reports/`. The proposed playlist preserves entry order,
names, TVG metadata, logos, groups, options, and headers; it changes only a verified
broken entry's URL. Unrepaired entries remain present and are listed separately. It does
not modify `public/tv.m3u`, `vercel.json`, or deployment configuration.

### Run from an iPhone

1. Open this repository in the GitHub app or at github.com and select **Actions**.
2. Open **IPTV Health Check and Repair**, tap **Run workflow**, and choose `main`.
3. Choose `health-only`, `check-repair`, or `repair-failed`; keep the defaults initially.
4. Tap the green **Run workflow** button and open the new run to follow its progress.
5. When it finishes, download **iptv-health-results** from the run's **Artifacts** area.

For `repair-failed`, `previous_run_id` may contain the numeric ID from an earlier run's
URL (`/actions/runs/123456789`). Leave it blank to use the health report committed in the
repository. Workflow runs only create downloadable artifacts: they do not commit,
deploy, or replace the production `/tv` playlist.
