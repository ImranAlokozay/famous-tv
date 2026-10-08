from __future__ import annotations

import concurrent.futures
import csv
import dataclasses
import datetime as dt
import io
import json
import os
import re
import tempfile
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from .checker import StreamChecker, quality_rank
from .discovery import DiscoveryRegistry
from .m3u import load_playlist, normalize_url, render_playlist
from .models import Candidate, HealthResult, HealthStatus, PlaylistEntry, RepairResult, as_dict


HEALTH_FIELDS = [
    "entry_index", "channel_name", "category", "tvg_id", "identity_id",
    "original_url", "original_resolution", "status", "failure_reason", "attempts",
    "final_url", "manifest_type", "selected_variant_url", "selected_resolution",
    "selected_bandwidth", "segment_url", "segment_http_status", "bytes_received",
    "verified_media", "checked_at",
]
REPAIR_FIELDS = [
    "entry_index", "channel_name", "category", "original_url", "original_resolution",
    "health_status", "failure_reason", "replacement_url", "replacement_source",
    "replacement_resolution", "candidate_streams_checked", "candidate_evidence",
    "verification_results", "repaired", "reason", "timestamp",
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _csv_text(rows: list[dict[str, Any]], fields: list[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: json.dumps(row[key], ensure_ascii=False, separators=(",", ":"))
                         if isinstance(row.get(key), (list, dict)) else row.get(key, "")
                         for key in fields})
    return output.getvalue()


def _health_summary(results: list[HealthResult]) -> dict[str, Any]:
    counts = Counter(result.status.value for result in results)
    uncertain = sum(counts[key] for key in (
        HealthStatus.PROBABLY_WORKING.value, HealthStatus.TIMEOUT.value,
        HealthStatus.GEO_BLOCKED_OR_FORBIDDEN.value, HealthStatus.UNKNOWN.value))
    return {
        "total_channels": len(results),
        "working": counts[HealthStatus.WORKING.value],
        "probably_working": counts[HealthStatus.PROBABLY_WORKING.value],
        "broken": counts[HealthStatus.BROKEN.value],
        "timeout": counts[HealthStatus.TIMEOUT.value],
        "geo_blocked_or_forbidden": counts[HealthStatus.GEO_BLOCKED_OR_FORBIDDEN.value],
        "unknown": counts[HealthStatus.UNKNOWN.value],
        "uncertain_total": uncertain,
        "by_status": dict(sorted(counts.items())),
    }


def _suspicious_variant(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    query_keys = {key.casefold() for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)}
    if query_keys & {"token", "session", "sessionid", "nimblesessionid", "hdnts", "hdnea",
                     "policy", "signature", "expires", "auth", "jwt", "tlSession".casefold()}:
        return True
    if re.search(r"/(?:session|token|cl)/", parsed.path, re.IGNORECASE):
        return True
    return False


def _reusable_lower_variant(candidate_url: str, variant_url: str, resolution: str) -> str:
    if not variant_url or _suspicious_variant(variant_url):
        return candidate_url
    return variant_url


def _candidate_declared_rank(candidate: Candidate) -> tuple[Any, ...]:
    return (quality_rank(candidate.resolution), -candidate.identity_score,
            candidate.bitrate or 10**15, candidate.source, candidate.url)


class RepairEngine:
    def __init__(self, checker: StreamChecker, registry: DiscoveryRegistry, *,
                 max_candidates: int = 10, candidate_workers: int = 6):
        self.checker = checker
        self.registry = registry
        self.max_candidates = max_candidates
        self.candidate_workers = candidate_workers

    def _verify(self, candidate: Candidate) -> tuple[Candidate, Any]:
        return candidate, self.checker.check_url(candidate.url, candidate.headers)

    def repair(self, entry: PlaylistEntry, health: HealthResult, timestamp: str,
               used_urls: set[str]) -> RepairResult:
        discovered = sorted(self.registry.discover(entry), key=_candidate_declared_rank)[:self.max_candidates]
        evidence: list[dict[str, Any]] = []
        verified: list[tuple[Candidate, Any, str, str]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.candidate_workers, max(1, len(discovered)))) as executor:
            futures = [executor.submit(self._verify, candidate) for candidate in discovered]
            for future in concurrent.futures.as_completed(futures):
                candidate, check = future.result()
                resolution = check.selected_resolution if check.selected_resolution != "unknown" else candidate.resolution
                replacement_url = _reusable_lower_variant(candidate.url, check.selected_variant_url, resolution)
                duplicate = normalize_url(replacement_url) in used_urls
                evidence.append({
                    "url": candidate.url, "source": candidate.source,
                    "declared_resolution": candidate.resolution,
                    "verified_resolution": resolution,
                    "health_status": check.status.value, "reason": check.reason,
                    "identity_score": candidate.identity_score,
                    "identity_evidence": candidate.identity_evidence,
                    "selected_variant_url": check.selected_variant_url,
                    "proposed_url": replacement_url,
                    "duplicate_rejected": duplicate,
                    "verified_media": check.verified_media,
                })
                if check.status == HealthStatus.WORKING and check.verified_media and not duplicate:
                    verified.append((candidate, check, replacement_url, resolution))
        if not verified:
            return RepairResult(
                entry_index=entry.index, channel_name=entry.name, category=entry.category,
                original_url=entry.url, original_resolution=entry.resolution,
                health_status=health.status.value, failure_reason=health.failure_reason,
                candidate_streams_checked=len(discovered), candidate_evidence=sorted(evidence, key=lambda x: x["url"]),
                repaired=False,
                reason="No identity-matched candidate produced verified HLS media without duplicating another channel",
                timestamp=timestamp)
        candidate, check, replacement_url, resolution = min(
            verified, key=lambda item: (quality_rank(item[3]), item[1].selected_bandwidth or item[0].bitrate or 10**15,
                                        -item[0].identity_score, item[0].source, item[2]))
        used_urls.add(normalize_url(replacement_url))
        return RepairResult(
            entry_index=entry.index, channel_name=entry.name, category=entry.category,
            original_url=entry.url, original_resolution=entry.resolution,
            health_status=health.status.value, failure_reason=health.failure_reason,
            replacement_url=replacement_url, replacement_source=candidate.source,
            replacement_resolution=resolution,
            candidate_streams_checked=len(discovered), candidate_evidence=sorted(evidence, key=lambda x: x["url"]),
            verification_results={
                "status": check.status.value, "reason": check.reason,
                "verified_media": check.verified_media, "manifest_type": check.manifest_type,
                "selected_variant_url": check.selected_variant_url,
                "selected_bandwidth": check.selected_bandwidth,
                "segment_url": check.segment_url, "segment_http_status": check.segment_http_status,
            },
            repaired=True, reason="Best verified same-channel candidate selected by 576p, 720p, then 1080p priority",
            timestamp=timestamp)


def _load_previous(path: Path) -> dict[int, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {int(row["entry_index"]): row for row in payload.get("channels", [])}


def _health_from_dict(row: dict[str, Any]) -> HealthResult:
    data = dict(row)
    data["status"] = HealthStatus(data["status"])
    data["attempts"] = []
    allowed = {field.name for field in dataclasses.fields(HealthResult)}
    return HealthResult(**{key: value for key, value in data.items() if key in allowed})


def run_maintenance(*, root: Path, mode: str, playlist_path: Path, config_path: Path,
                    workers: int | None = None, timeout: float | None = None,
                    retries: int | None = None, previous_report: Path | None = None,
                    now: Callable[[], str] = utc_now) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    health_config = config["health"]
    repair_config = config["repair"]
    workers = workers or int(health_config["workers"])
    timeout = timeout or float(health_config["timeout_seconds"])
    retries = retries if retries is not None else int(health_config["retries"])
    timestamp = now()
    raw_lines, entries = load_playlist(playlist_path)
    checker = StreamChecker(
        timeout=timeout, retries=retries,
        retry_backoff=float(health_config.get("retry_backoff_seconds", 0.4)),
        max_manifest_bytes=int(health_config.get("max_manifest_bytes", 2_000_000)),
        segment_probe_bytes=int(health_config.get("segment_probe_bytes", 2048)))

    previous: dict[int, dict[str, Any]] = {}
    if mode == "repair-failed":
        if previous_report is None or not previous_report.exists():
            raise ValueError("repair-failed mode requires an existing health report")
        previous = _load_previous(previous_report)
        target_entries = [entry for entry in entries
                          if previous.get(entry.index, {}).get("status") == HealthStatus.BROKEN.value]
    else:
        target_entries = entries
    checked = checker.check_entries(target_entries, workers=workers, checked_at=timestamp)
    checked_by_index = {result.entry_index: result for result in checked}
    if previous:
        health_results = []
        for entry in entries:
            if entry.index in checked_by_index:
                health_results.append(checked_by_index[entry.index])
            elif entry.index in previous:
                health_results.append(_health_from_dict(previous[entry.index]))
            else:
                health_results.append(HealthResult(
                    entry_index=entry.index, channel_name=entry.name, category=entry.category,
                    tvg_id=entry.tvg_id, identity_id=entry.identity_id,
                    original_url=entry.url, original_resolution=entry.resolution,
                    status=HealthStatus.UNKNOWN, failure_reason="No prior result available",
                    checked_at=timestamp))
    else:
        health_results = checked

    health_rows = [as_dict(result) for result in health_results]
    health_payload = {
        "schema_version": 1, "generated_at": timestamp, "mode": mode,
        "source_playlist": str(playlist_path.relative_to(root)),
        "configuration": {"workers": workers, "timeout_seconds": timeout, "retries": retries},
        "summary": _health_summary(health_results), "channels": health_rows,
    }
    reports = root / "reports"
    _atomic_write(reports / "iptv_health_report.json", json.dumps(health_payload, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(reports / "iptv_health_report.csv", _csv_text(health_rows, HEALTH_FIELDS))

    result = {"health": health_payload["summary"], "repairs": None, "source_errors": []}
    if mode == "health-only":
        return result

    registry = DiscoveryRegistry(
        config["sources"], root,
        timeout=float(repair_config.get("source_timeout_seconds", 30)),
        source_retries=int(repair_config.get("source_retries", 2)),
        retry_backoff=float(repair_config.get("source_retry_backoff_seconds", 1.0)))
    registry.load()
    engine = RepairEngine(
        checker, registry, max_candidates=int(repair_config.get("max_candidates_per_channel", 10)),
        candidate_workers=int(repair_config.get("candidate_workers", 6)))
    used_urls = {normalize_url(entry.url) for entry in entries}
    health_by_index = {value.entry_index: value for value in health_results}
    broken = [(entry, health_by_index[entry.index]) for entry in entries
              if health_by_index[entry.index].status == HealthStatus.BROKEN]
    broken.sort(key=lambda pair: (pair[0].category != "Sports", pair[0].index))
    repair_results = [engine.repair(entry, health, timestamp, used_urls) for entry, health in broken]
    replacements = {repair.entry_index: repair.replacement_url for repair in repair_results if repair.repaired}
    repaired_text = render_playlist(raw_lines, entries, replacements)
    _atomic_write(root / "public/tv_repaired.m3u", repaired_text)

    repair_rows = [as_dict(item) for item in sorted(repair_results, key=lambda item: item.entry_index)]
    repaired_count = sum(item.repaired for item in repair_results)
    sports_repaired = sum(item.repaired and item.category == "Sports" for item in repair_results)
    converted = sum(item.repaired and item.original_resolution == "1080p"
                    and item.replacement_resolution in ("576i", "576p", "720i", "720p")
                    for item in repair_results)
    repair_payload = {
        "schema_version": 1, "generated_at": timestamp, "mode": mode,
        "source_playlist": str(playlist_path.relative_to(root)),
        "output_playlist": "public/tv_repaired.m3u",
        "summary": {
            "broken_channels_considered": len(repair_results),
            "successfully_repaired": repaired_count,
            "without_verified_replacement": len(repair_results) - repaired_count,
            "sports_repaired": sports_repaired,
            "repaired_1080_to_576_or_720": converted,
        },
        "source_errors": registry.errors,
        "channels": repair_rows,
    }
    _atomic_write(reports / "iptv_repair_report.json", json.dumps(repair_payload, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(reports / "iptv_repair_report.csv", _csv_text(repair_rows, REPAIR_FIELDS))
    proposed = [row for row in repair_rows if not row["repaired"]]
    proposed_payload = {
        "schema_version": 1, "generated_at": timestamp,
        "note": "Proposals only. No channel was removed from either playlist.",
        "count": len(proposed), "channels": proposed,
    }
    _atomic_write(reports / "iptv_proposed_removals.json", json.dumps(proposed_payload, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(reports / "iptv_proposed_removals.csv", _csv_text(proposed, REPAIR_FIELDS))
    result["repairs"] = repair_payload["summary"]
    result["source_errors"] = registry.errors
    return result
