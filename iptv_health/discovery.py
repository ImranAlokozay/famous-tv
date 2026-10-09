from __future__ import annotations

import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .checker import Transport, UrlLibTransport
from .m3u import normalize_name, normalize_url, parse_m3u
from .models import Candidate, PlaylistEntry


def _base_id(value: str) -> str:
    return (value or "").split("@", 1)[0]


def _marker(name: str, marker: str) -> bool:
    return bool(re.search(rf"\b{marker}\b", name, re.IGNORECASE))


def identity_score(entry: PlaylistEntry, candidate: Candidate) -> tuple[int, str]:
    if candidate.channel_id and _base_id(candidate.channel_id) == entry.identity_id:
        score, evidence = 100, "exact tvg-id/channel id"
    elif candidate.name and normalize_name(candidate.name) == normalize_name(entry.name):
        score, evidence = 90, "exact normalized channel name"
    else:
        return 0, "channel identity did not match"
    for marker in ("fast", "preview"):
        if candidate.name and _marker(candidate.name, marker) != _marker(entry.name, marker):
            return 0, f"{marker.upper()} identity marker mismatch"
    if candidate.category and entry.category and candidate.category.casefold() != entry.category.casefold():
        score -= 10
        evidence += "; category differs"
    if candidate.country and entry.country and candidate.country.casefold() != entry.country.casefold():
        score -= 5
        evidence += "; country differs"
    if candidate.languages and entry.languages and not ({x.casefold() for x in candidate.languages} &
                                                          {x.casefold() for x in entry.languages}):
        return 0, "language identity mismatch"
    return score, evidence


class DiscoveryProvider:
    name: str

    def load(self) -> None:
        raise NotImplementedError

    def candidates(self, entry: PlaylistEntry) -> list[Candidate]:
        raise NotImplementedError


class InventoryProvider(DiscoveryProvider):
    def __init__(self, name: str, path: Path, promotions_path: Path | None = None):
        self.name = name
        self.path = path
        self.promotions_path = promotions_path
        self.by_id: dict[str, dict[str, Any]] = {}

    def load(self) -> None:
        records = json.loads(self.path.read_text(encoding="utf-8"))
        if self.promotions_path and self.promotions_path.exists():
            promotions = json.loads(self.promotions_path.read_text(encoding="utf-8"))
            records_by_id = {record["id"]: record for record in records}
            for promotion in promotions:
                record = records_by_id.get(promotion["id"])
                if not record:
                    raise ValueError(f"Unknown promoted channel: {promotion['id']}")
                candidate = next((item for item in record.get("candidates", [])
                                  if item["url"] == promotion["old_url"]), None)
                if not candidate:
                    raise ValueError(f"Promoted candidate no longer matches: {promotion['id']}")
                candidate["url"] = promotion["new_url"]
                candidate["quality"] = promotion.get("replacement_resolution") or candidate.get("quality")
        self.by_id = {record["id"]: record for record in records}

    def candidates(self, entry: PlaylistEntry) -> list[Candidate]:
        record = self.by_id.get(entry.identity_id)
        if not record:
            return []
        result = []
        for item in record.get("candidates", []):
            headers = {}
            if item.get("user_agent"):
                headers["User-Agent"] = item["user_agent"]
            if item.get("referrer"):
                headers["Referer"] = item["referrer"]
            result.append(Candidate(
                url=item["url"], source=self.name, channel_id=record["id"],
                name=record.get("name", ""), country=record.get("country", ""),
                languages=item.get("languages") or record.get("languages", []),
                category=record.get("category", ""), resolution=item.get("quality") or "unknown",
                headers=headers))
        return result


class IptvOrgApiProvider(DiscoveryProvider):
    def __init__(self, name: str, url: str, transport: Transport, timeout: float):
        self.name, self.url, self.transport, self.timeout = name, url, transport, timeout
        self.by_id: dict[str, list[dict[str, Any]]] = {}

    def load(self) -> None:
        response = self.transport.fetch(self.url, {}, self.timeout, 40_000_000)
        records = json.loads(response.body.decode("utf-8"))
        by_id: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            if record.get("channel") and record.get("url"):
                by_id.setdefault(record["channel"], []).append(record)
        self.by_id = by_id

    def candidates(self, entry: PlaylistEntry) -> list[Candidate]:
        result = []
        for item in self.by_id.get(entry.identity_id, []):
            headers = {}
            if item.get("user_agent"):
                headers["User-Agent"] = item["user_agent"]
            if item.get("http_referrer"):
                headers["Referer"] = item["http_referrer"]
            result.append(Candidate(
                url=item["url"], source=self.name, channel_id=item["channel"],
                resolution=item.get("quality") or "unknown", headers=headers))
        return result


class M3uCatalogProvider(DiscoveryProvider):
    def __init__(self, name: str, url: str, transport: Transport, timeout: float):
        self.name, self.url, self.transport, self.timeout = name, url, transport, timeout
        self.by_id: dict[str, list[Candidate]] = {}
        self.by_name: dict[str, list[Candidate]] = {}

    def load(self) -> None:
        response = self.transport.fetch(self.url, {}, self.timeout, 40_000_000)
        _, entries = parse_m3u(response.body.decode("utf-8", "replace"))
        for item in entries:
            candidate = Candidate(
                url=item.url, source=self.name, channel_id=item.identity_id,
                name=item.name, country=item.country, languages=item.languages,
                category=item.category, resolution=item.resolution, headers=item.headers)
            if item.identity_id:
                self.by_id.setdefault(item.identity_id, []).append(candidate)
            self.by_name.setdefault(normalize_name(item.name), []).append(candidate)

    def candidates(self, entry: PlaylistEntry) -> list[Candidate]:
        values = list(self.by_id.get(entry.identity_id, []))
        values.extend(self.by_name.get(normalize_name(entry.name), []))
        unique: dict[str, Candidate] = {}
        for candidate in values:
            unique.setdefault(normalize_url(candidate.url), candidate)
        return list(unique.values())


class DiscoveryRegistry:
    def __init__(self, source_specs: list[dict[str, Any]], root: Path,
                 transport: Transport | None = None, timeout: float = 30,
                 source_retries: int = 2, retry_backoff: float = 1.0):
        self.transport = transport or UrlLibTransport()
        self.source_retries = source_retries
        self.retry_backoff = retry_backoff
        self.providers: list[DiscoveryProvider] = []
        self.errors: list[dict[str, str]] = []
        factories = {
            "inventory": lambda spec: InventoryProvider(
                spec["name"], root / spec["path"],
                root / spec["promotions_path"] if spec.get("promotions_path") else None),
            "iptv_org_api": lambda spec: IptvOrgApiProvider(spec["name"], spec["url"], self.transport, timeout),
            "m3u": lambda spec: M3uCatalogProvider(spec["name"], spec["url"], self.transport, timeout),
        }
        for spec in source_specs:
            if not spec.get("enabled", True):
                continue
            source_type = spec.get("type")
            if source_type not in factories:
                self.errors.append({"source": spec.get("name", "unknown"),
                                    "error": f"Unsupported provider type: {source_type}"})
                continue
            self.providers.append(factories[source_type](spec))

    def load(self) -> None:
        for provider in self.providers:
            for attempt in range(self.source_retries + 1):
                try:
                    provider.load()
                    break
                except Exception as error:
                    if attempt == self.source_retries:
                        self.errors.append({
                            "source": provider.name,
                            "error": f"Failed after {attempt + 1} attempts: {type(error).__name__}: {error}",
                        })
                    elif self.retry_backoff:
                        time.sleep(self.retry_backoff * (2 ** attempt))

    def discover(self, entry: PlaylistEntry) -> list[Candidate]:
        found: list[Candidate] = []
        seen: set[str] = set()
        for provider in self.providers:
            try:
                values = provider.candidates(entry)
            except Exception as error:
                self.errors.append({"source": provider.name,
                                    "error": f"candidate lookup: {type(error).__name__}: {error}"})
                continue
            for candidate in values:
                normalized = normalize_url(candidate.url)
                if normalized == normalize_url(entry.url) or normalized in seen:
                    continue
                score, evidence = identity_score(entry, candidate)
                if not score:
                    continue
                if candidate.headers and any(entry.headers.get(key) != value for key, value in candidate.headers.items()):
                    # Repaired output replaces only the URL and intentionally preserves original M3U options.
                    continue
                candidate.identity_score = score
                candidate.identity_evidence = evidence
                found.append(candidate)
                seen.add(normalized)
        return found
