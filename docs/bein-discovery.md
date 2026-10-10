# beIN discovery — 10 October 2026

One verified unnumbered **beIN SPORTS (English)** feed was added at **432p**.
The published master exposes one rendition; it offered no 576p or 720p option.
FFprobe identified H.264 video and AAC audio, FFmpeg decoded 1,197 frames over
20 seconds without errors, and a later frame showed the football clock advancing.
The existing beIN SPORTS XTRA entry remains unchanged at 576p.

**beIN Sports 2 remains missing.** The review covered 114 catalogs and 1,009
candidates, including 184 labeled as beIN Sports 2 or its regional variants.
Sources include IPTV-org, GitHub code search, community Arabic/European/Asian
playlists, public directories, archived references and official beIN pages.
None of the numbered-channel candidates passed playback, exact identity and
reusable/live-feed checks. A decodable 1080p candidate redirected to an unrelated
on-demand placeholder; Spanish XTRA and the unnumbered English service were
not relabeled as Sports 2. A skipped or forbidden candidate is not proof that
the channel is unavailable everywhere.

Evidence:

- [Coverage and selected resolutions](../reports/bein_coverage.csv)
- [Catalog retrieval and candidate evidence](../reports/bein_discovery.json)
- [HTTP statuses, redirects, formats and skip conditions](../reports/bein_access_audit.csv)
- [Second verification frame](../reports/bein_verified_feed.jpg)

Rerun discovery with Python 3 and FFmpeg/FFprobe installed:

```sh
python scripts/discover_cricket.py --config config/bein_discovery.json \
  --output .cache/bein-discovery --workers 12 --timeout 12
```

This command only collects evidence. Visually review channel branding and live
progression before adding candidates to `curation/channels.json`. Keep source
provenance in `curation/policy.json` and target outcomes in
`curation/bein_targets.json`; then regenerate and validate:

```sh
python scripts/rebuild_playlist.py
python scripts/rebuild_playlist.py --check
python -m unittest discover -s tests -v
```

No access tokens, copied account passwords or DRM keys are used for new entries.
Normal documented Referer and User-Agent headers are supported. Third-party feed
availability can change, and successful checks here do not guarantee every TV
player or region. All 447 previous playlist entries and their relative order were
preserved; `/tv`, `vercel.json` and the deployment structure were not changed.
