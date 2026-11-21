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

The playlist selects English and Hindi audio in the general sections. The
Pakistan section also permits Urdu. The Afghanistan section retains its existing
language exceptions and preserves all 24 original Afghan channel identities.
Other reviewed languages remain in the inventory for provenance but are excluded
from the generated playlist. Country/resolution copies of the same editorial
stream are collapsed.

The November 21, 2026 Pakistan expansion adds ARY Zindagi, JooMusic, Lahore News,
and Madani Channel Urdu. Manifest and media-object checks succeeded for the latter
three; ARY Zindagi's official published URL could not be verified because of a TLS
certificate error. These checks do not verify decoded video/audio or future
availability. Evidence is in [reports/pakistan-expansion.json](reports/pakistan-expansion.json).

The same day's initial premium-channel review adds Sony MAX and Zee Cinema APAC from
public third-party catalog links after successful manifest/media checks, H.264/AAC
decoding, visual branding review, and live media progression checks. Broadcaster
authorization is unconfirmed; Hindi language is channel metadata rather than an
independent audio review. HBO's candidate timed out and was not added. Details are
in [reports/premium-discovery.json](reports/premium-discovery.json).

A broader pass checked 104 candidates from 11 catalogs and a further 23 HBO
candidates from four catalogs. It adds &pictures, Colors Cineplex, MNX, Movies
Now, Sony PIX, Star Gold, Star Gold 2, Star Movies, Star Movies Select, Sony Sports
Ten 1/2/5, Star Sports 1 Hindi, and Star Sports 2. Each accepted URL passed manifest
and media checks, three-frame video decoding, matching on-screen branding review,
and increasing live media sequences. Broadcaster authorization remains unconfirmed;
language is channel/catalog metadata rather than an independent spoken-audio review.
The one decoded HBO feed carries a Czech audio tag and was excluded. Static
manifests and a mislabeled English feed offered as Hindi were also excluded.
Research evidence is in
[reports/premium-expanded-discovery.json](reports/premium-expanded-discovery.json).
The existing research command supports this configuration too (it writes research
outputs only and never edits the production playlist):

```sh
python scripts/discover_cricket.py --config config/premium_discovery.json \
  --output .cache/premium-discovery --workers 12 --timeout 5
```

At the user's explicit request, the playlist also includes the index's exact
`HBOAsia.sg@SD` entry: **HBO Asia SD (720p) [Geo-blocked]**. The original published
short link is retained. It resolved to a dynamic DASH manifest on November 21,
2026; media playback, spoken audio, and broadcaster authorization are unverified.
The `SD` feed name and `720p` quality both come from the source. Only explicitly
listed `reviewed_redirect_urls` may bypass the generator's direct-manifest URL
suffix requirement; ordinary unreviewed short links remain rejected.

Movies/series have small provider caps; Stingray music and archival TVS sports
also have caps. Sports has no overall channel-count cap. Identified FAST brand
editions are named separately from subscription flagships. Copied account credentials,
ticket/MAC URLs, incorrect channel identities, and nonpreferred language editions
are not used to fill gaps. Reviewed third-party public distributions are permitted;
their source is recorded without implying broadcaster authorization or guaranteed rights.

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

## Focused cricket discovery and video verification

The additional catalog list, exact aliases, and published candidate references are in
[`config/cricket_discovery.json`](config/cricket_discovery.json). This is a read-only
research command, not another production health checker:

```sh
# Requires ffprobe and ffmpeg on PATH; the existing Python checker is reused.
python scripts/discover_cricket.py --workers 12 --timeout 10
# Optional focused retest; results stay separate from the full audit.
python scripts/discover_cricket.py --channel 'PTV Sports' --output .cache/ptv-review
```

It fetches community/GitHub/regional catalogs and records website retrieval results,
tests manifests and media objects, probes the video codec/resolution, decodes video
frames, checks live-manifest progression, and captures frames for identity review.
Website discovery is not automatic player/API extraction: a fetched page is not
reported as a tested feed. Outputs under `.cache/cricket-discovery` include
`discovery.json`, `access_audit.csv`, `access_audit.json`, and frame captures.
`--reuse-evidence` is opt-in and retains original timestamps; use a fresh run for
current HTTP evidence. This command never modifies `public/tv.m3u`.

`SKIPPED_ACCESS` records the exact code condition, a redacted URL, and a unique
candidate ID. Account/token-shaped paths are access concerns, not proof of a
subscription or geo-block. These are not requested with copied credentials, so
their status/redirect/headers are explicitly untested, not fabricated. A generic
403 remains `UNKNOWN`; `GEO_RESTRICTED`, `AUTH_REQUIRED`, `DRM_PROTECTED`,
`MISSING_HEADERS`, and `TEMPORARY_FAILURE` require appropriate evidence.
Only published normal playback headers are honored. There is no token stripping,
credential generation, DRM-key extraction, or geo-restriction bypass.

The committed [`reports/cricket_discovery.json`](reports/cricket_discovery.json) and
[`reports/cricket_access_audit.csv`](reports/cricket_access_audit.csv) document this
research snapshot. [`reports/cricket_coverage.csv`](reports/cricket_coverage.csv)
records accepted channels and unresolved targets. A public URL or decoded frame
alone is insufficient: the editorial review must reject unrelated channels,
fixed highlights clips, and mislabeled FAST editions. Accepted streams are added
to the reviewed inventory and regenerated offline. Public mirrors can expire or
have regional/rights limitations; these tests do not certify every LG/Android player.

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
