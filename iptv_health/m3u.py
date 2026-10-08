from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

from .models import PlaylistEntry

ATTRIBUTE_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
RESOLUTION_RE = re.compile(r'\((\d{3,4}[pi])\)\s*$', re.IGNORECASE)


def _name_from_extinf(line: str) -> str:
    quoted = False
    comma = -1
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ',' and not quoted:
            comma = index
    return line[comma + 1:].strip() if comma >= 0 else ""


def _headers(attributes: dict[str, str], options: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    aliases = {
        "http-user-agent": "User-Agent",
        "user-agent": "User-Agent",
        "http-referrer": "Referer",
        "http-referer": "Referer",
        "referrer": "Referer",
    }
    for key, value in attributes.items():
        if key.casefold() in aliases and value:
            headers[aliases[key.casefold()]] = value
    for option in options:
        if not option.startswith("#EXTVLCOPT:") or "=" not in option:
            continue
        key, value = option[len("#EXTVLCOPT:"):].split("=", 1)
        if key.casefold() in aliases and value:
            headers[aliases[key.casefold()]] = value
    return headers


def parse_m3u(text: str) -> tuple[list[str], list[PlaylistEntry]]:
    raw_lines = text.splitlines()
    if not raw_lines or not raw_lines[0].strip().startswith("#EXTM3U"):
        raise ValueError("Playlist must begin with #EXTM3U")
    entries: list[PlaylistEntry] = []
    index = 0
    while index < len(raw_lines):
        if not raw_lines[index].startswith("#EXTINF:"):
            index += 1
            continue
        start = index
        extinf = raw_lines[index]
        index += 1
        url_index = -1
        while index < len(raw_lines) and not raw_lines[index].startswith("#EXTINF:"):
            stripped = raw_lines[index].strip()
            if stripped and not stripped.startswith("#"):
                url_index = index
                index += 1
                break
            index += 1
        if url_index < 0:
            raise ValueError(f"Channel entry at line {start + 1} has no stream URL")
        while index < len(raw_lines) and not raw_lines[index].startswith("#EXTINF:"):
            index += 1
        block = raw_lines[start:index]
        local_url_index = url_index - start
        attrs = {key: value for key, value in ATTRIBUTE_RE.findall(extinf)}
        name = attrs.get("tvg-name") or _name_from_extinf(extinf)
        options = [line for line in block[1:local_url_index] if line.startswith("#")]
        tvg_id = attrs.get("tvg-id", "")
        identity_id = tvg_id.split("@", 1)[0]
        language_text = attrs.get("tvg-language", "")
        languages = [part.strip() for part in re.split(r"[;,]", language_text) if part.strip()]
        country = ""
        if "." in identity_id:
            suffix = identity_id.rsplit(".", 1)[1]
            if re.fullmatch(r"[A-Za-z]{2,3}", suffix):
                country = suffix.upper()
        display_name = _name_from_extinf(extinf)
        match = RESOLUTION_RE.search(display_name)
        entries.append(PlaylistEntry(
            index=len(entries), lines=block, url_line_index=local_url_index,
            url=raw_lines[url_index].strip(), extinf=extinf, name=name,
            attributes=attrs, options=options, headers=_headers(attrs, options),
            tvg_id=tvg_id, identity_id=identity_id,
            category=attrs.get("group-title", ""), logo=attrs.get("tvg-logo", ""),
            languages=languages, country=country,
            resolution=match.group(1).lower() if match else "unknown"))
    return raw_lines, entries


def load_playlist(path: Path) -> tuple[list[str], list[PlaylistEntry]]:
    return parse_m3u(path.read_text(encoding="utf-8"))


def render_playlist(raw_lines: list[str], entries: list[PlaylistEntry], replacements: dict[int, str]) -> str:
    lines = list(raw_lines)
    cursor = 0
    entry_by_start: list[tuple[int, PlaylistEntry]] = []
    for entry in entries:
        while cursor < len(lines) and lines[cursor] != entry.extinf:
            cursor += 1
        if cursor >= len(lines):
            raise ValueError(f"Could not locate channel block for {entry.name}")
        entry_by_start.append((cursor, entry))
        cursor += len(entry.lines)
    for start, entry in entry_by_start:
        if entry.index in replacements:
            lines[start + entry.url_line_index] = replacements[entry.index]
    return "\n".join(lines) + "\n"


def normalize_name(name: str) -> str:
    name = name.casefold()
    name = re.sub(r"\[(?:geo-blocked|not 24/7)\]", "", name)
    name = re.sub(r"\((?:\d{3,4}[pi]|hd|sd|fhd)\)", "", name)
    name = re.sub(r"\b(?:hd|sd|fhd|uhd)\b", "", name)
    return re.sub(r"[^a-z0-9]+", "", name)


def normalize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").casefold()
    port = parsed.port
    netloc = host if not port or (parsed.scheme, port) in (("https", 443), ("http", 80)) else f"{host}:{port}"
    return urllib.parse.urlunsplit((parsed.scheme.casefold(), netloc, parsed.path, parsed.query, ""))
