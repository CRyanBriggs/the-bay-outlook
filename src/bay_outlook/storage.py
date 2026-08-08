from __future__ import annotations

import csv
import hashlib
import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from .models import Observation, Snapshot, SourceRelease


USER_AGENT = "TheBayOutlook/0.1 (economic-research-pipeline)"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sanitize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    safe_query = [(key, value) for key, value in query if key.casefold() not in {"key", "userid", "registrationkey"}]
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(safe_query), parsed.fragment)
    )


def download_bytes(url: str, timeout: int = 90) -> tuple[bytes, dict[str, str], str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read()
        headers = {key: value for key, value in response.headers.items()}
        return content, headers, response.geturl()


def post_json(url: str, payload: dict[str, object], timeout: int = 90) -> tuple[bytes, dict[str, str], str]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read()
        headers = {key: value for key, value in response.headers.items()}
        return content, headers, response.geturl()


def save_snapshot(
    root: Path,
    source_id: str,
    filename: str,
    content: bytes,
    source_url: str,
    source_release: str,
    response_headers: dict[str, str] | None = None,
    retrieved_at: str | None = None,
    dataset_status: str = "official",
) -> Snapshot:
    retrieved = retrieved_at or utc_now()
    digest = sha256_bytes(content)
    day = retrieved[:10]
    destination_dir = root / "raw" / source_id / day
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / filename
    if destination.exists() and sha256_bytes(destination.read_bytes()) != digest:
        destination = destination.with_name(f"{destination.stem}-{digest[:8]}{destination.suffix}")
    if not destination.exists():
        destination.write_bytes(content)

    metadata = {
        "source_id": source_id,
        "source_url": sanitize_url(source_url),
        "source_release": source_release,
        "retrieved_at": retrieved,
        "sha256": digest,
        "byte_count": len(content),
        "dataset_status": dataset_status,
        "response_headers": {
            key: value
            for key, value in (response_headers or {}).items()
            if key.casefold() in {"content-type", "content-length", "last-modified", "etag"}
        },
    }
    metadata_path = destination.with_suffix(destination.suffix + ".metadata.json")
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return Snapshot(
        source_id=source_id,
        source_url=metadata["source_url"],
        source_release=source_release,
        retrieved_at=retrieved,
        sha256=digest,
        path=destination,
        metadata_path=metadata_path,
        dataset_status=dataset_status,
    )


OBSERVATION_FIELDS = list(Observation.__dataclass_fields__)


def write_observations_csv(path: Path, observations: Iterable[Observation]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OBSERVATION_FIELDS)
        writer.writeheader()
        for observation in observations:
            writer.writerow(observation.as_dict())


def append_release_log(path: Path, release: SourceRelease) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "release_id",
        "source_id",
        "source_release_date",
        "retrieved_at",
        "raw_sha256",
        "raw_path",
        "processing_status",
        "validation_status",
        "observation_count",
        "revision_notes",
        "next_expected_release",
    ]
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(release.as_dict())
