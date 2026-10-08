from __future__ import annotations

import concurrent.futures
import dataclasses
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Protocol

from .models import AttemptEvidence, HealthResult, HealthStatus, PlaylistEntry


@dataclasses.dataclass
class FetchResponse:
    status: int
    final_url: str
    headers: dict[str, str]
    body: bytes


class Transport(Protocol):
    def fetch(self, url: str, headers: dict[str, str], timeout: float,
              max_bytes: int, byte_range: bool = False) -> FetchResponse: ...


class UrlLibTransport:
    def fetch(self, url: str, headers: dict[str, str], timeout: float,
              max_bytes: int, byte_range: bool = False) -> FetchResponse:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Only credential-free HTTP(S) URLs are allowed")
        host = parsed.hostname.casefold()
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
            raise ValueError("Local network stream destinations are not allowed")
        try:
            literal = ipaddress.ip_address(host)
            if not literal.is_global:
                raise ValueError("Private, loopback, link-local, and reserved IP literals are not allowed")
        except ValueError as error:
            if "not allowed" in str(error):
                raise
        request_headers = {"User-Agent": "famous-tv-health-checker/1.0", "Accept": "*/*", **headers}
        if byte_range:
            request_headers["Range"] = f"bytes=0-{max_bytes - 1}"
        request = urllib.request.Request(url, headers=request_headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(max_bytes)
            return FetchResponse(
                status=getattr(response, "status", 200), final_url=response.geturl(),
                headers={key.casefold(): value for key, value in response.headers.items()}, body=body)


@dataclasses.dataclass
class Variant:
    url: str
    height: int | None
    bandwidth: int | None


@dataclasses.dataclass
class SingleCheck:
    status: HealthStatus
    reason: str
    http_status: int | None = None
    final_url: str = ""
    manifest_type: str = ""
    selected_variant_url: str = ""
    selected_resolution: str = "unknown"
    selected_bandwidth: int | None = None
    segment_url: str = ""
    segment_http_status: int | None = None
    bytes_received: int = 0
    verified_media: bool = False


def resolution_rank(height: int | None) -> tuple[int, int]:
    if height == 576:
        return (0, 0)
    if height == 720:
        return (1, 0)
    if height == 1080:
        return (2, 0)
    if height is not None and height < 720:
        return (3, -height)
    if height is not None:
        return (4, height)
    return (5, 0)


def quality_rank(quality: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{3,4})[pi]", quality or "", re.IGNORECASE)
    return resolution_rank(int(match.group(1)) if match else None)


def parse_master_variants(text: str, base_url: str) -> list[Variant]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants: list[Variant] = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF") or index + 1 >= len(lines):
            continue
        target = lines[index + 1]
        if target.startswith("#"):
            continue
        resolution = re.search(r"RESOLUTION=\d+x(\d+)", line, re.IGNORECASE)
        bandwidth = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", line, re.IGNORECASE)
        variants.append(Variant(
            url=urllib.parse.urljoin(base_url, target),
            height=int(resolution.group(1)) if resolution else None,
            bandwidth=int(bandwidth.group(1)) if bandwidth else None))
    return variants


def choose_variant(variants: list[Variant]) -> Variant:
    return min(variants, key=lambda item: (resolution_rank(item.height), item.bandwidth or 10**15, item.url))


def first_media_uri(text: str, base_url: str) -> str:
    map_match = re.search(r'#EXT-X-MAP:[^\n]*URI="([^"]+)"', text, re.IGNORECASE)
    if map_match:
        return urllib.parse.urljoin(base_url, map_match.group(1))
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return urllib.parse.urljoin(base_url, line)
    return ""


class StreamChecker:
    def __init__(self, transport: Transport | None = None, *, timeout: float = 12,
                 retries: int = 2, retry_backoff: float = 0.4,
                 max_manifest_bytes: int = 2_000_000,
                 segment_probe_bytes: int = 2048):
        self.transport = transport or UrlLibTransport()
        self.timeout = timeout
        self.retries = retries
        self.retry_backoff = retry_backoff
        self.max_manifest_bytes = max_manifest_bytes
        self.segment_probe_bytes = segment_probe_bytes

    def _fetch(self, url: str, headers: dict[str, str], *, segment: bool = False) -> FetchResponse:
        return self.transport.fetch(
            url, headers, self.timeout,
            self.segment_probe_bytes if segment else self.max_manifest_bytes,
            byte_range=segment)

    def _http_failure(self, status: int, location: str) -> SingleCheck:
        if status in (401, 403, 451):
            return SingleCheck(HealthStatus.GEO_BLOCKED_OR_FORBIDDEN,
                               f"{location} returned HTTP {status}", http_status=status)
        if status in (404, 410):
            return SingleCheck(HealthStatus.BROKEN,
                               f"{location} returned HTTP {status}", http_status=status)
        return SingleCheck(HealthStatus.UNKNOWN,
                           f"{location} returned HTTP {status}", http_status=status)

    def _network_failure(self, error: Exception, location: str) -> SingleCheck:
        if isinstance(error, urllib.error.HTTPError):
            return self._http_failure(error.code, location)
        if isinstance(error, (socket.timeout, TimeoutError)):
            return SingleCheck(HealthStatus.TIMEOUT, f"{location} timeout: {error}")
        if isinstance(error, urllib.error.URLError):
            reason = error.reason
            if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).casefold():
                return SingleCheck(HealthStatus.TIMEOUT, f"{location} timeout: {reason}")
            if isinstance(reason, socket.gaierror):
                return SingleCheck(HealthStatus.BROKEN, f"{location} DNS failure: {reason}")
            return SingleCheck(HealthStatus.UNKNOWN, f"{location} connection error: {reason}")
        if isinstance(error, socket.gaierror):
            return SingleCheck(HealthStatus.BROKEN, f"{location} DNS failure: {error}")
        if isinstance(error, ValueError):
            return SingleCheck(HealthStatus.BROKEN, str(error))
        return SingleCheck(HealthStatus.UNKNOWN, f"{location} network error: {error}")

    def _check_hls(self, response: FetchResponse, text: str,
                   headers: dict[str, str], depth: int = 0) -> SingleCheck:
        if depth > 2:
            return SingleCheck(HealthStatus.UNKNOWN, "Nested playlist depth exceeded",
                               http_status=response.status, final_url=response.final_url)
        variants = parse_master_variants(text, response.final_url)
        selected = choose_variant(variants) if variants else None
        playlist_response = response
        playlist_text = text
        if selected:
            try:
                playlist_response = self._fetch(selected.url, headers)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ValueError, OSError) as error:
                result = self._network_failure(error, "Selected HLS rendition")
                result.final_url = response.final_url
                result.manifest_type = "hls-master"
                result.selected_variant_url = selected.url
                result.selected_resolution = f"{selected.height}p" if selected.height else "unknown"
                result.selected_bandwidth = selected.bandwidth
                return result
            if playlist_response.status not in (200, 206):
                result = self._http_failure(playlist_response.status, "Selected HLS rendition")
                result.final_url = playlist_response.final_url
                return result
            playlist_text = playlist_response.body.decode("utf-8", "replace")
        if "#EXTM3U" not in playlist_text:
            return SingleCheck(HealthStatus.BROKEN, "HLS rendition was not an M3U playlist",
                               http_status=playlist_response.status, final_url=playlist_response.final_url,
                               manifest_type="hls-master" if selected else "hls-media")
        segment_url = first_media_uri(playlist_text, playlist_response.final_url)
        if not segment_url:
            nested = [line.strip() for line in playlist_text.splitlines()
                      if line.strip() and not line.startswith("#")]
            if nested:
                try:
                    nested_response = self._fetch(urllib.parse.urljoin(playlist_response.final_url, nested[0]), headers)
                    nested_text = nested_response.body.decode("utf-8", "replace")
                    return self._check_hls(nested_response, nested_text, headers, depth + 1)
                except Exception:
                    pass
            return SingleCheck(HealthStatus.PROBABLY_WORKING,
                               "Valid HLS manifest responded but no current media object was listed",
                               http_status=playlist_response.status, final_url=playlist_response.final_url,
                               manifest_type="hls-master" if selected else "hls-media",
                               selected_variant_url=selected.url if selected else "",
                               selected_resolution=f"{selected.height}p" if selected and selected.height else "unknown",
                               selected_bandwidth=selected.bandwidth if selected else None,
                               bytes_received=len(playlist_response.body))
        try:
            segment = self._fetch(segment_url, headers, segment=True)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ValueError, OSError) as error:
            result = self._network_failure(error, "HLS media object")
            result.final_url = playlist_response.final_url
            result.manifest_type = "hls-master" if selected else "hls-media"
            result.selected_variant_url = selected.url if selected else ""
            result.selected_resolution = f"{selected.height}p" if selected and selected.height else "unknown"
            result.selected_bandwidth = selected.bandwidth if selected else None
            result.segment_url = segment_url
            return result
        if segment.status not in (200, 206) or not segment.body:
            result = self._http_failure(segment.status, "HLS media object")
            result.segment_url = segment_url
            return result
        return SingleCheck(
            HealthStatus.WORKING, "HLS manifest, media playlist, and media object responded",
            http_status=response.status, final_url=response.final_url,
            manifest_type="hls-master" if selected else "hls-media",
            selected_variant_url=selected.url if selected else "",
            selected_resolution=f"{selected.height}p" if selected and selected.height else "unknown",
            selected_bandwidth=selected.bandwidth if selected else None,
            segment_url=segment_url, segment_http_status=segment.status,
            bytes_received=len(segment.body), verified_media=True)

    def _check_once(self, url: str, headers: dict[str, str]) -> SingleCheck:
        try:
            response = self._fetch(url, headers)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ValueError, OSError) as error:
            return self._network_failure(error, "Stream")
        if response.status not in (200, 206):
            result = self._http_failure(response.status, "Stream")
            result.final_url = response.final_url
            return result
        text = response.body.decode("utf-8", "replace")
        if text.lstrip().startswith("#EXTM3U"):
            path = urllib.parse.urlsplit(response.final_url).path.casefold()
            if "#EXT-X-" in text or path.endswith(".m3u8"):
                return self._check_hls(response, text, headers)
            nested_url = first_media_uri(text, response.final_url)
            if nested_url:
                try:
                    nested = self._fetch(nested_url, headers)
                    nested_text = nested.body.decode("utf-8", "replace")
                    if nested_text.lstrip().startswith("#EXTM3U"):
                        return self._check_hls(nested, nested_text, headers, 1)
                except Exception as error:
                    return SingleCheck(HealthStatus.UNKNOWN, f"Nested M3U target failed: {error}",
                                       http_status=response.status, final_url=response.final_url,
                                       manifest_type="m3u")
            return SingleCheck(HealthStatus.PROBABLY_WORKING,
                               "M3U playlist responded but contained no verifiable HLS media",
                               http_status=response.status, final_url=response.final_url,
                               manifest_type="m3u", bytes_received=len(response.body))
        if "<MPD" in text[:5000] or "application/dash+xml" in response.headers.get("content-type", ""):
            return SingleCheck(HealthStatus.PROBABLY_WORKING,
                               "DASH manifest responded; segment template was not probed",
                               http_status=response.status, final_url=response.final_url,
                               manifest_type="dash", bytes_received=len(response.body))
        content_type = response.headers.get("content-type", "").casefold()
        if response.body and (content_type.startswith("video/") or "octet-stream" in content_type):
            return SingleCheck(HealthStatus.PROBABLY_WORKING,
                               "Direct media response returned bytes",
                               http_status=response.status, final_url=response.final_url,
                               manifest_type="direct-media", bytes_received=len(response.body))
        return SingleCheck(HealthStatus.BROKEN, "Response was not a recognized playlist or media stream",
                           http_status=response.status, final_url=response.final_url,
                           bytes_received=len(response.body))

    def check_url(self, url: str, headers: dict[str, str] | None = None) -> SingleCheck:
        headers = headers or {}
        results: list[SingleCheck] = []
        for attempt in range(self.retries + 1):
            result = self._check_once(url, headers)
            results.append(result)
            if result.status in (HealthStatus.WORKING, HealthStatus.PROBABLY_WORKING,
                                 HealthStatus.GEO_BLOCKED_OR_FORBIDDEN):
                return result
            if attempt < self.retries and self.retry_backoff:
                time.sleep(self.retry_backoff * (2 ** attempt))
        counts = Counter(result.status for result in results)
        if counts[HealthStatus.TIMEOUT] == len(results):
            return results[-1]
        if counts[HealthStatus.BROKEN] == len(results) and len(results) >= 2:
            return results[-1]
        if counts[HealthStatus.GEO_BLOCKED_OR_FORBIDDEN] == len(results):
            return results[-1]
        last = results[-1]
        last.status = HealthStatus.UNKNOWN
        last.reason = "Mixed or insufficient failure evidence: " + "; ".join(result.reason for result in results)
        return last

    def check_entry(self, entry: PlaylistEntry, checked_at: str) -> HealthResult:
        evidence: list[AttemptEvidence] = []
        checks: list[SingleCheck] = []
        for attempt in range(self.retries + 1):
            started = time.monotonic()
            result = self._check_once(entry.url, entry.headers)
            checks.append(result)
            evidence.append(AttemptEvidence(
                attempt=attempt + 1, outcome=result.status.value, reason=result.reason,
                http_status=result.http_status, final_url=result.final_url,
                elapsed_ms=round((time.monotonic() - started) * 1000)))
            if result.status in (HealthStatus.WORKING, HealthStatus.PROBABLY_WORKING,
                                 HealthStatus.GEO_BLOCKED_OR_FORBIDDEN):
                break
            if attempt < self.retries and self.retry_backoff:
                time.sleep(self.retry_backoff * (2 ** attempt))
        statuses = [item.status for item in checks]
        final = checks[-1]
        if all(status == HealthStatus.TIMEOUT for status in statuses):
            status = HealthStatus.TIMEOUT
        elif all(status == HealthStatus.BROKEN for status in statuses) and len(statuses) >= 2:
            status = HealthStatus.BROKEN
        elif final.status in (HealthStatus.WORKING, HealthStatus.PROBABLY_WORKING,
                              HealthStatus.GEO_BLOCKED_OR_FORBIDDEN):
            status = final.status
        elif len(statuses) < 2:
            status = HealthStatus.UNKNOWN
        else:
            status = HealthStatus.UNKNOWN
            final.reason = "Mixed failure evidence: " + "; ".join(item.reason for item in checks)
        return HealthResult(
            entry_index=entry.index, channel_name=entry.name, category=entry.category,
            tvg_id=entry.tvg_id, identity_id=entry.identity_id,
            original_url=entry.url, original_resolution=entry.resolution,
            status=status, failure_reason="" if status == HealthStatus.WORKING else final.reason,
            attempts=evidence, final_url=final.final_url, manifest_type=final.manifest_type,
            selected_variant_url=final.selected_variant_url,
            selected_resolution=final.selected_resolution,
            selected_bandwidth=final.selected_bandwidth,
            segment_url=final.segment_url, segment_http_status=final.segment_http_status,
            bytes_received=final.bytes_received, verified_media=final.verified_media,
            checked_at=checked_at)

    def check_entries(self, entries: list[PlaylistEntry], *, workers: int, checked_at: str) -> list[HealthResult]:
        results: list[HealthResult] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self.check_entry, entry, checked_at): entry.index for entry in entries}
            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                try:
                    results.append(future.result())
                except Exception as error:  # one channel must never abort the run
                    entry = next(item for item in entries if item.index == index)
                    results.append(HealthResult(
                        entry_index=index, channel_name=entry.name, category=entry.category,
                        tvg_id=entry.tvg_id, identity_id=entry.identity_id,
                        original_url=entry.url, original_resolution=entry.resolution,
                        status=HealthStatus.UNKNOWN,
                        failure_reason=f"Unhandled checker error: {type(error).__name__}: {error}",
                        checked_at=checked_at))
        return sorted(results, key=lambda item: item.entry_index)
