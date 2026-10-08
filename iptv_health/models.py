from __future__ import annotations

import dataclasses
import enum
from typing import Any


class HealthStatus(str, enum.Enum):
    WORKING = "WORKING"
    PROBABLY_WORKING = "PROBABLY_WORKING"
    BROKEN = "BROKEN"
    TIMEOUT = "TIMEOUT"
    GEO_BLOCKED_OR_FORBIDDEN = "GEO_BLOCKED_OR_FORBIDDEN"
    UNKNOWN = "UNKNOWN"


@dataclasses.dataclass
class PlaylistEntry:
    index: int
    lines: list[str]
    url_line_index: int
    url: str
    extinf: str
    name: str
    attributes: dict[str, str]
    options: list[str]
    headers: dict[str, str]
    tvg_id: str = ""
    identity_id: str = ""
    category: str = ""
    logo: str = ""
    languages: list[str] = dataclasses.field(default_factory=list)
    country: str = ""
    resolution: str = "unknown"


@dataclasses.dataclass
class AttemptEvidence:
    attempt: int
    outcome: str
    reason: str = ""
    http_status: int | None = None
    final_url: str = ""
    elapsed_ms: int = 0


@dataclasses.dataclass
class HealthResult:
    entry_index: int
    channel_name: str
    category: str
    tvg_id: str
    identity_id: str
    original_url: str
    original_resolution: str
    status: HealthStatus
    failure_reason: str = ""
    attempts: list[AttemptEvidence] = dataclasses.field(default_factory=list)
    final_url: str = ""
    manifest_type: str = ""
    selected_variant_url: str = ""
    selected_resolution: str = "unknown"
    selected_bandwidth: int | None = None
    segment_url: str = ""
    segment_http_status: int | None = None
    bytes_received: int = 0
    verified_media: bool = False
    checked_at: str = ""


@dataclasses.dataclass
class Candidate:
    url: str
    source: str
    channel_id: str = ""
    name: str = ""
    country: str = ""
    languages: list[str] = dataclasses.field(default_factory=list)
    category: str = ""
    resolution: str = "unknown"
    bitrate: int | None = None
    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    identity_evidence: str = ""
    identity_score: int = 0


@dataclasses.dataclass
class RepairResult:
    entry_index: int
    channel_name: str
    category: str
    original_url: str
    original_resolution: str
    health_status: str
    failure_reason: str
    replacement_url: str = ""
    replacement_source: str = ""
    replacement_resolution: str = "unknown"
    candidate_streams_checked: int = 0
    candidate_evidence: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    verification_results: dict[str, Any] = dataclasses.field(default_factory=dict)
    repaired: bool = False
    reason: str = ""
    timestamp: str = ""


def as_dict(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value):
        return {field.name: as_dict(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {key: as_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_dict(item) for item in value]
    return value
