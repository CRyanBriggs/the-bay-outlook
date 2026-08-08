from __future__ import annotations

import json
import io
import os
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .constants import COUNTY_BY_FIPS, PROJECT_ROOT
from .models import Observation
from .operations import OperationsStore, read_source_state
from .pipeline import combined_hash, persist
from .sources import acs, bea, cde, laus, qcew
from .storage import save_snapshot, sha256_bytes
from .warehouse import build_warehouse


TRANSIENT_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
ALLOWED_RESOLVERS = frozenset(
    {
        "rolling_year_window",
        "lagged_quarter",
        "current_bulk_vintage",
        "lagged_calendar_year",
        "latest_landing_page_release",
    }
)


class ConfigurationError(ValueError):
    pass


class CredentialUnavailable(RuntimeError):
    pass


class OverlappingRunError(RuntimeError):
    pass


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str | date | datetime | None, default: datetime) -> datetime:
    if value is None:
        parsed = default
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time(), UTC)
    else:
        text = value.strip()
        if len(text) == 10:
            parsed = datetime.fromisoformat(text).replace(tzinfo=UTC)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _read_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return _parse_datetime(value, datetime.now(UTC))
    except ValueError:
        return None


def _resolve_project_path(project_root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise ConfigurationError(f"Configured path must be project-relative: {value}")
    root = project_root.resolve()
    resolved = (root / relative).resolve()
    if resolved != root and root not in resolved.parents:
        raise ConfigurationError(f"Configured path escapes the project root: {value}")
    return resolved


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    initial_delay_seconds: float
    backoff_multiplier: float
    max_delay_seconds: float
    persistent_backoff_minutes: int
    persistent_backoff_max_minutes: int


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    source_id: str
    enabled: bool
    cadence: str
    check_interval_days: int
    resolver: str
    required_environment: tuple[str, ...]
    release_reference: str
    policy_note: str
    settings: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class OrchestrationConfig:
    schema_version: str
    state_database: Path
    staging_database: Path
    warehouse_database: Path
    lock_path: Path
    stale_after_minutes: int
    retry: RetryPolicy
    sources: tuple[SourcePolicy, ...]

    @property
    def source_by_id(self) -> dict[str, SourcePolicy]:
        return {source.source_id: source for source in self.sources}


def load_config(path: Path | None = None, *, project_root: Path = PROJECT_ROOT) -> OrchestrationConfig:
    config_path = path or project_root / "config" / "orchestration.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    schema_version = str(payload.get("schema_version", ""))
    if not schema_version.startswith("6."):
        raise ConfigurationError(f"Expected a Phase 6 config; found schema {schema_version!r}")

    retry_values = payload.get("retries", {})
    retry = RetryPolicy(
        max_attempts=int(retry_values.get("max_attempts", 3)),
        initial_delay_seconds=float(retry_values.get("initial_delay_seconds", 2)),
        backoff_multiplier=float(retry_values.get("backoff_multiplier", 2)),
        max_delay_seconds=float(retry_values.get("max_delay_seconds", 30)),
        persistent_backoff_minutes=int(retry_values.get("persistent_backoff_minutes", 60)),
        persistent_backoff_max_minutes=int(retry_values.get("persistent_backoff_max_minutes", 1440)),
    )
    if retry.max_attempts < 1 or retry.initial_delay_seconds < 0 or retry.backoff_multiplier < 1:
        raise ConfigurationError("Retry values must define at least one nonnegative, nonshrinking attempt policy")

    source_rows = payload.get("sources", [])
    sources: list[SourcePolicy] = []
    seen: set[str] = set()
    for row in source_rows:
        source_id = str(row.get("source_id", "")).strip()
        resolver = str(row.get("resolver", "")).strip()
        interval = int(row.get("check_interval_days", 0))
        if not source_id or source_id in seen:
            raise ConfigurationError(f"Source identifiers must be nonempty and unique: {source_id!r}")
        if resolver not in ALLOWED_RESOLVERS:
            raise ConfigurationError(f"Unsupported resolver for {source_id}: {resolver}")
        if interval < 1:
            raise ConfigurationError(f"check_interval_days must be positive for {source_id}")
        seen.add(source_id)
        sources.append(
            SourcePolicy(
                source_id=source_id,
                enabled=bool(row.get("enabled", True)),
                cadence=str(row.get("cadence", "")),
                check_interval_days=interval,
                resolver=resolver,
                required_environment=tuple(str(value) for value in row.get("required_environment", [])),
                release_reference=str(row.get("release_reference", "")),
                policy_note=str(row.get("policy_note", "")),
                settings=dict(row),
            )
        )
    if not sources:
        raise ConfigurationError("At least one orchestration source is required")

    lock_values = payload.get("lock", {})
    return OrchestrationConfig(
        schema_version=schema_version,
        state_database=_resolve_project_path(project_root, str(payload["state_database"])),
        staging_database=_resolve_project_path(project_root, str(payload["staging_database"])),
        warehouse_database=_resolve_project_path(project_root, str(payload["warehouse_database"])),
        lock_path=_resolve_project_path(project_root, str(lock_values["path"])),
        stale_after_minutes=int(lock_values.get("stale_after_minutes", 180)),
        retry=retry,
        sources=tuple(sources),
    )


@dataclass(frozen=True, slots=True)
class PlanItem:
    source_id: str
    target_release: str
    due: bool
    action: str
    reason: str
    next_check_at: str
    parameters: Mapping[str, Any]
    missing_environment: tuple[str, ...]
    release_reference: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_release": self.target_release,
            "due": self.due,
            "action": self.action,
            "reason": self.reason,
            "next_check_at": self.next_check_at,
            "parameters": dict(self.parameters),
            "missing_environment": list(self.missing_environment),
            "release_reference": self.release_reference,
        }


@dataclass(frozen=True, slots=True)
class RawAsset:
    filename: str
    content: bytes
    source_url: str
    response_headers: Mapping[str, str] = field(default_factory=dict)
    contributes_to_fingerprint: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.content)


@dataclass(frozen=True, slots=True)
class FetchedPackage:
    source_id: str
    target_release: str
    source_vintage: str
    source_release_date: str
    assets: tuple[RawAsset, ...]


@dataclass(frozen=True, slots=True)
class SourceHandler:
    fetch: Callable[[PlanItem], FetchedPackage]
    normalize: Callable[[FetchedPackage, str], Sequence[Observation]]


class RunLock:
    def __init__(
        self,
        path: Path,
        *,
        stale_after_minutes: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.path = path
        self.stale_after = timedelta(minutes=stale_after_minutes)
        self.clock = clock
        self.token = uuid.uuid4().hex
        self.acquired = False

    def _stale(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            created = _read_timestamp(str(payload.get("created_at", "")))
        except (OSError, ValueError, json.JSONDecodeError):
            created = None
        if created is None:
            try:
                created = datetime.fromtimestamp(self.path.stat().st_mtime, UTC)
            except OSError:
                return False
        return self.clock().astimezone(UTC) - created > self.stale_after

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if not self._stale():
                    raise OverlappingRunError(f"Another orchestration run holds {self.path}")
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            payload = {
                "token": self.token,
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "created_at": _iso(self.clock()),
            }
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
            self.acquired = True
            return self
        raise OverlappingRunError(f"Could not acquire orchestration lock {self.path}")

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if not self.acquired:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("token") == self.token:
                self.path.unlink()
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            pass


def _quarter_end(year: int, quarter: int) -> date:
    return date(year, quarter * 3, (31, 30, 30, 31)[quarter - 1])


def _latest_lagged_quarter(as_of: date, lag_days: int) -> tuple[int, int]:
    threshold = as_of - timedelta(days=lag_days)
    quarter = (threshold.month - 1) // 3 + 1
    year = threshold.year
    if _quarter_end(year, quarter) > threshold:
        quarter -= 1
        if quarter == 0:
            year -= 1
            quarter = 4
    return year, quarter


def _latest_lagged_year(as_of: date, lag_days: int) -> int:
    threshold = as_of - timedelta(days=lag_days)
    return threshold.year if date(threshold.year, 12, 31) <= threshold else threshold.year - 1


def _target_for(policy: SourcePolicy, as_of: datetime) -> tuple[str, dict[str, Any]]:
    if policy.resolver == "rolling_year_window":
        lookback = int(policy.settings.get("lookback_years", 1))
        start_year = as_of.year - lookback
        return f"LAUS-rolling-{start_year}-{as_of.year}", {"start_year": start_year, "end_year": as_of.year}
    if policy.resolver == "lagged_quarter":
        year, quarter = _latest_lagged_quarter(as_of.date(), int(policy.settings["release_lag_days"]))
        return f"{year}-Q{quarter}", {"year": year, "quarter": quarter}
    if policy.resolver == "current_bulk_vintage":
        return "current-bulk-vintage", {}
    if policy.resolver == "lagged_calendar_year":
        year = _latest_lagged_year(as_of.date(), int(policy.settings["release_lag_days"]))
        return f"ACS5-{year}", {"year": year}
    if policy.resolver == "latest_landing_page_release":
        return "latest-published", {}
    raise ConfigurationError(f"No target resolver for {policy.source_id}")


def _bea_vintage(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            years = []
            for name in archive.namelist():
                match = re.search(r"CAGDP1_CA_\d{4}_(\d{4})\.csv$", name)
                if match:
                    years.append(int(match.group(1)))
        return f"CAGDP1-through-{max(years)}" if years else "CAGDP1-current"
    except (OSError, ValueError, zipfile.BadZipFile):
        return "CAGDP1-current"


def _fetch_laus(plan: PlanItem) -> FetchedPackage:
    start_year = int(plan.parameters["start_year"])
    end_year = int(plan.parameters["end_year"])
    assets = tuple(
        RawAsset(
            filename=f"laus-{start_year}-{end_year}-part{index}.json",
            content=content,
            source_url=laus.API_URL,
            metadata={"part": index},
        )
        for index, content in enumerate(laus.fetch_payloads(start_year, end_year), start=1)
    )
    return FetchedPackage(laus.SOURCE_ID, plan.target_release, plan.target_release, "", assets)


def _normalize_laus(package: FetchedPackage, retrieved_at: str) -> Sequence[Observation]:
    rows: list[Observation] = []
    for asset in package.assets:
        rows.extend(
            laus.normalize(
                asset.content,
                raw_sha256=asset.sha256,
                retrieved_at=retrieved_at,
                source_release=package.source_vintage,
            )
        )
    return rows


def _fetch_qcew(plan: PlanItem) -> FetchedPackage:
    year = int(plan.parameters["year"])
    quarter = int(plan.parameters["quarter"])
    assets: list[RawAsset] = []
    for fips in sorted(COUNTY_BY_FIPS):
        content, headers, final_url = qcew.fetch(year, quarter, fips)
        assets.append(
            RawAsset(
                filename=f"qcew-{year}-Q{quarter}-{fips}.csv",
                content=content,
                source_url=final_url,
                response_headers=headers,
                metadata={"county_fips": fips},
            )
        )
    return FetchedPackage(qcew.SOURCE_ID, plan.target_release, plan.target_release, "", tuple(assets))


def _normalize_qcew(package: FetchedPackage, retrieved_at: str) -> Sequence[Observation]:
    rows: list[Observation] = []
    for asset in package.assets:
        rows.extend(
            qcew.normalize(
                asset.content,
                county_fips=str(asset.metadata["county_fips"]),
                raw_sha256=asset.sha256,
                retrieved_at=retrieved_at,
                source_release=package.source_vintage,
            )
        )
    return rows


def _fetch_bea(plan: PlanItem) -> FetchedPackage:
    content, headers, final_url = bea.fetch()
    vintage = _bea_vintage(content)
    asset = RawAsset("CAGDP1.zip", content, final_url, headers)
    return FetchedPackage(bea.SOURCE_ID, plan.target_release, vintage, "", (asset,))


def _normalize_bea(package: FetchedPackage, retrieved_at: str) -> Sequence[Observation]:
    asset = package.assets[0]
    return bea.normalize(
        asset.content,
        raw_sha256=asset.sha256,
        retrieved_at=retrieved_at,
        source_release=package.source_vintage,
    )


def _fetch_acs(plan: PlanItem) -> FetchedPackage:
    year = int(plan.parameters["year"])
    if not os.getenv("CENSUS_API_KEY"):
        raise CredentialUnavailable("CENSUS_API_KEY is required for live ACS data queries")
    content, headers, final_url = acs.fetch(year)
    asset = RawAsset(f"acs5-housing-{year}.json", content, final_url, headers, metadata={"year": year})
    return FetchedPackage(acs.SOURCE_ID, plan.target_release, f"ACS5-{year}", "", (asset,))


def _normalize_acs(package: FetchedPackage, retrieved_at: str) -> Sequence[Observation]:
    asset = package.assets[0]
    return acs.normalize(
        asset.content,
        year=int(asset.metadata["year"]),
        raw_sha256=asset.sha256,
        retrieved_at=retrieved_at,
        source_release=package.source_vintage,
    )


def _fetch_cde(plan: PlanItem) -> FetchedPackage:
    release, content, headers, final_url = cde.fetch_latest()
    assets = (
        RawAsset(
            filename="cde-cgr-release-page.html",
            content=release.page_content,
            source_url=release.page_url,
            response_headers=release.page_headers,
            contributes_to_fingerprint=False,
            metadata={"role": "release_page"},
        ),
        RawAsset(
            filename=Path(urllib.parse.urlsplit(final_url).path).name
            or f"cgr12mo{release.academic_year[-2:]}.txt",
            content=content,
            source_url=final_url,
            response_headers=headers,
            metadata={"role": "data"},
        ),
    )
    return FetchedPackage(
        cde.SOURCE_ID,
        plan.target_release,
        release.source_vintage,
        release.release_date,
        assets,
    )


def _normalize_cde(package: FetchedPackage, retrieved_at: str) -> Sequence[Observation]:
    asset = next(asset for asset in package.assets if asset.metadata.get("role") == "data")
    return cde.normalize(
        asset.content,
        raw_sha256=asset.sha256,
        retrieved_at=retrieved_at,
        source_release=package.source_vintage,
    )


def default_handlers() -> dict[str, SourceHandler]:
    return {
        laus.SOURCE_ID: SourceHandler(_fetch_laus, _normalize_laus),
        qcew.SOURCE_ID: SourceHandler(_fetch_qcew, _normalize_qcew),
        bea.SOURCE_ID: SourceHandler(_fetch_bea, _normalize_bea),
        acs.SOURCE_ID: SourceHandler(_fetch_acs, _normalize_acs),
        cde.SOURCE_ID: SourceHandler(_fetch_cde, _normalize_cde),
    }


def _existing_release_by_hash(database_path: Path, source_id: str, content_sha256: str) -> str:
    if not database_path.exists():
        return ""
    try:
        uri = f"file:{database_path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'source_releases'"
            ).fetchone()
            if not exists:
                return ""
            row = connection.execute(
                """
                SELECT release_id FROM source_releases
                WHERE source_id = ? AND raw_sha256 = ?
                  AND processing_status = 'processed' AND validation_status = 'passed'
                ORDER BY retrieved_at DESC LIMIT 1
                """,
                (source_id, content_sha256),
            ).fetchone()
            return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""


def _slug(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", value)


def _safe_error(error: BaseException, environment: Mapping[str, str]) -> str:
    message = str(error)
    for name, value in environment.items():
        if value and len(value) >= 4:
            message = message.replace(value, f"[{name}_REDACTED]")
    message = re.sub(
        r"(?i)(key|userid|registrationkey)=([^&\s]+)",
        lambda match: f"{match.group(1)}=[REDACTED]",
        message,
    )
    return message[:2000]


def _is_transient(error: BaseException) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in TRANSIENT_HTTP_CODES
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))


class Orchestrator:
    def __init__(
        self,
        *,
        project_root: Path = PROJECT_ROOT,
        config_path: Path | None = None,
        handlers: Mapping[str, SourceHandler] | None = None,
        environment: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        warehouse_builder: Callable[..., dict[str, Any]] = build_warehouse,
    ):
        self.project_root = project_root.resolve()
        self.config = load_config(config_path, project_root=self.project_root)
        self.handlers = default_handlers()
        if handlers:
            self.handlers.update(handlers)
        self.environment = dict(os.environ if environment is None else environment)
        self.clock = clock
        self.sleep = sleep
        self.warehouse_builder = warehouse_builder

    def _now(self) -> datetime:
        value = self.clock()
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    def plan(
        self,
        *,
        as_of: str | date | datetime | None = None,
        source_ids: Sequence[str] | None = None,
        force: bool = False,
    ) -> list[PlanItem]:
        effective = _parse_datetime(as_of, self._now())
        selected = set(source_ids or [source.source_id for source in self.config.sources])
        unknown = selected - set(self.config.source_by_id)
        if unknown:
            raise ConfigurationError(f"Unknown orchestration sources: {sorted(unknown)}")
        plans: list[PlanItem] = []
        for policy in self.config.sources:
            if policy.source_id not in selected:
                continue
            target, parameters = _target_for(policy, effective)
            state = read_source_state(self.config.state_database, policy.source_id)
            last_checked = _read_timestamp(str((state or {}).get("last_checked_at", "")))
            next_retry = _read_timestamp(str((state or {}).get("next_retry_at", "")))
            interval_due = last_checked + timedelta(days=policy.check_interval_days) if last_checked else effective

            if not policy.enabled:
                due, action, reason = False, "disabled", "source_disabled"
                next_check = interval_due
            elif force:
                due, action, reason = True, "fetch", "forced"
                next_check = effective
            elif next_retry and effective < next_retry:
                due, action, reason = False, "wait", "retry_backoff"
                next_check = next_retry
            elif last_checked is None:
                due, action, reason = True, "fetch", "never_checked"
                next_check = effective
            elif effective >= interval_due:
                due, action, reason = True, "fetch", "check_interval_elapsed"
                next_check = effective
            else:
                due, action, reason = False, "wait", "not_due"
                next_check = interval_due

            missing = tuple(name for name in policy.required_environment if not self.environment.get(name))
            if due and missing:
                action = "blocked_credential"
                reason = "missing_required_environment"
            plans.append(
                PlanItem(
                    source_id=policy.source_id,
                    target_release=target,
                    due=due,
                    action=action,
                    reason=reason,
                    next_check_at=_iso(next_check),
                    parameters=parameters,
                    missing_environment=missing,
                    release_reference=policy.release_reference,
                )
            )
        return plans

    def plan_report(
        self,
        *,
        as_of: str | date | datetime | None = None,
        source_ids: Sequence[str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        effective = _parse_datetime(as_of, self._now())
        plans = self.plan(as_of=effective, source_ids=source_ids, force=force)
        return {
            "schema_version": self.config.schema_version,
            "as_of": _iso(effective),
            "force": force,
            "due_count": sum(item.due for item in plans),
            "blocked_count": sum(item.action == "blocked_credential" for item in plans),
            "plans": [item.as_dict() for item in plans],
        }

    def _attempt_values(
        self,
        *,
        completed_at: str,
        status: str,
        source_vintage: str = "",
        content_sha256: str = "",
        release_id: str = "",
        observation_count: int = 0,
        transient: bool = False,
        next_retry_at: str = "",
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        return {
            "completed_at": completed_at,
            "status": status,
            "source_vintage": source_vintage,
            "content_sha256": content_sha256,
            "release_id": release_id,
            "observation_count": observation_count,
            "transient": int(transient),
            "next_retry_at": next_retry_at,
            "error_class": type(error).__name__ if error else "",
            "error_message": _safe_error(error, self.environment) if error else "",
        }

    def _persistent_retry_at(self, source_id: str, now: datetime) -> str:
        state = read_source_state(self.config.state_database, source_id) or {}
        failures = int(state.get("consecutive_failures", 0))
        minutes = min(
            self.config.retry.persistent_backoff_minutes * (2**failures),
            self.config.retry.persistent_backoff_max_minutes,
        )
        return _iso(now + timedelta(minutes=minutes))

    def _next_poll_at(self, source_id: str, now: datetime) -> str:
        policy = self.config.source_by_id[source_id]
        return _iso(now + timedelta(days=policy.check_interval_days))

    def _block_credential(
        self,
        store: OperationsStore,
        run_id: str,
        plan: PlanItem,
    ) -> dict[str, Any]:
        started = self._now()
        attempt_id = store.start_attempt(
            {
                "run_id": run_id,
                "source_id": plan.source_id,
                "target_release": plan.target_release,
                "attempt_number": 1,
                "due_reason": plan.reason,
                "started_at": _iso(started),
            }
        )
        names = ", ".join(plan.missing_environment)
        error = CredentialUnavailable(f"Required environment variable is unavailable: {names}")
        completed = self._now()
        next_check = self._next_poll_at(plan.source_id, completed)
        store.finish_attempt(
            attempt_id,
            self._attempt_values(
                completed_at=_iso(completed),
                status="blocked_credential",
                next_retry_at=next_check,
                error=error,
            ),
        )
        message = _safe_error(error, self.environment)
        store.update_source_state(
            source_id=plan.source_id,
            checked_at=_iso(completed),
            status="blocked_credential",
            target_release=plan.target_release,
            next_retry_at=next_check,
            error_message=message,
        )
        return {
            "source_id": plan.source_id,
            "target_release": plan.target_release,
            "status": "blocked_credential",
            "attempt_count": 1,
            "next_retry_at": next_check,
            "error": message,
        }

    def _execute_source(
        self,
        store: OperationsStore,
        run_id: str,
        plan: PlanItem,
    ) -> dict[str, Any]:
        handler = self.handlers.get(plan.source_id)
        if handler is None:
            return self._record_handler_failure(store, run_id, plan)
        retry = self.config.retry
        for attempt_number in range(1, retry.max_attempts + 1):
            started = self._now()
            attempt_id = store.start_attempt(
                {
                    "run_id": run_id,
                    "source_id": plan.source_id,
                    "target_release": plan.target_release,
                    "attempt_number": attempt_number,
                    "due_reason": plan.reason,
                    "started_at": _iso(started),
                }
            )
            package: FetchedPackage | None = None
            fingerprint = ""
            try:
                package = handler.fetch(plan)
                if package.source_id != plan.source_id:
                    raise ValueError(
                        f"Handler returned {package.source_id} for planned source {plan.source_id}"
                    )
                fingerprint_assets = [asset.sha256 for asset in package.assets if asset.contributes_to_fingerprint]
                if not fingerprint_assets:
                    raise ValueError(f"{plan.source_id} returned no fingerprint-bearing assets")
                fingerprint = combined_hash(fingerprint_assets)
                existing_release = _existing_release_by_hash(
                    self.config.staging_database, plan.source_id, fingerprint
                )
                if existing_release:
                    completed = self._now()
                    store.finish_attempt(
                        attempt_id,
                        self._attempt_values(
                            completed_at=_iso(completed),
                            status="skipped_duplicate",
                            source_vintage=package.source_vintage,
                            content_sha256=fingerprint,
                            release_id=existing_release,
                        ),
                    )
                    store.record_fingerprint(
                        source_id=plan.source_id,
                        target_release=plan.target_release,
                        source_vintage=package.source_vintage,
                        content_sha256=fingerprint,
                        release_id=existing_release,
                        first_seen_at=_iso(completed),
                    )
                    store.update_source_state(
                        source_id=plan.source_id,
                        checked_at=_iso(completed),
                        status="skipped_duplicate",
                        target_release=plan.target_release,
                        source_vintage=package.source_vintage,
                        content_sha256=fingerprint,
                        release_id=existing_release,
                        successful=True,
                    )
                    return {
                        "source_id": plan.source_id,
                        "target_release": plan.target_release,
                        "source_vintage": package.source_vintage,
                        "status": "skipped_duplicate",
                        "attempt_count": attempt_number,
                        "content_sha256": fingerprint,
                        "release_id": existing_release,
                        "observation_count": 0,
                    }

                retrieved_at = _iso(self._now())
                raw_paths: list[str] = []
                for asset in package.assets:
                    if Path(asset.filename).name != asset.filename:
                        raise ValueError(f"Unsafe raw asset filename: {asset.filename}")
                    snapshot = save_snapshot(
                        self.project_root / "data",
                        plan.source_id,
                        asset.filename,
                        asset.content,
                        asset.source_url,
                        package.source_vintage,
                        response_headers=dict(asset.response_headers),
                        retrieved_at=retrieved_at,
                    )
                    raw_paths.append(
                        str(snapshot.path.resolve().relative_to((self.project_root / "data").resolve()))
                    )

                observations = list(handler.normalize(package, retrieved_at))
                observation_fingerprint = combined_hash(row.raw_sha256 for row in observations)
                if observation_fingerprint != fingerprint:
                    raise ValueError(
                        f"Normalized provenance mismatch for {plan.source_id}: "
                        f"{observation_fingerprint} != {fingerprint}"
                    )
                run_token = run_id.rsplit("-", 1)[-1][:8]
                release_id = f"{plan.source_id}-{_slug(retrieved_at)}-{run_token}"
                processed_path = (
                    self.project_root
                    / "data"
                    / "processed"
                    / f"{plan.source_id.casefold()}-{_slug(retrieved_at)}-{run_token}.csv"
                )
                release = persist(
                    observations=observations,
                    release_id=release_id,
                    source_id=plan.source_id,
                    source_release_date=package.source_release_date,
                    retrieved_at=retrieved_at,
                    raw_path=";".join(raw_paths),
                    processed_path=processed_path,
                    database_path=self.config.staging_database,
                    release_log_path=self.project_root / "data" / "data_release_log.csv",
                    revision_notes="Phase 6 automated retrieval with content-fingerprint deduplication.",
                    next_expected_release=self._next_poll_at(plan.source_id, self._now()),
                )
                completed = self._now()
                store.finish_attempt(
                    attempt_id,
                    self._attempt_values(
                        completed_at=_iso(completed),
                        status="succeeded",
                        source_vintage=package.source_vintage,
                        content_sha256=fingerprint,
                        release_id=release.release_id,
                        observation_count=release.observation_count,
                    ),
                )
                store.record_fingerprint(
                    source_id=plan.source_id,
                    target_release=plan.target_release,
                    source_vintage=package.source_vintage,
                    content_sha256=fingerprint,
                    release_id=release.release_id,
                    first_seen_at=_iso(completed),
                )
                store.update_source_state(
                    source_id=plan.source_id,
                    checked_at=_iso(completed),
                    status="succeeded",
                    target_release=plan.target_release,
                    source_vintage=package.source_vintage,
                    content_sha256=fingerprint,
                    release_id=release.release_id,
                    successful=True,
                    loaded=True,
                )
                return {
                    "source_id": plan.source_id,
                    "target_release": plan.target_release,
                    "source_vintage": package.source_vintage,
                    "status": "succeeded",
                    "attempt_count": attempt_number,
                    "content_sha256": fingerprint,
                    "release_id": release.release_id,
                    "observation_count": release.observation_count,
                }
            except Exception as error:
                completed = self._now()
                transient = _is_transient(error)
                source_vintage = package.source_vintage if package else ""
                if transient and attempt_number < retry.max_attempts:
                    delay = min(
                        retry.initial_delay_seconds * (retry.backoff_multiplier ** (attempt_number - 1)),
                        retry.max_delay_seconds,
                    )
                    retry_at = _iso(completed + timedelta(seconds=delay))
                    store.finish_attempt(
                        attempt_id,
                        self._attempt_values(
                            completed_at=_iso(completed),
                            status="retry_scheduled",
                            source_vintage=source_vintage,
                            content_sha256=fingerprint,
                            transient=True,
                            next_retry_at=retry_at,
                            error=error,
                        ),
                    )
                    self.sleep(delay)
                    continue

                status = "failed_transient" if transient else "failed_permanent"
                next_check = (
                    self._persistent_retry_at(plan.source_id, completed)
                    if transient
                    else self._next_poll_at(plan.source_id, completed)
                )
                store.finish_attempt(
                    attempt_id,
                    self._attempt_values(
                        completed_at=_iso(completed),
                        status=status,
                        source_vintage=source_vintage,
                        content_sha256=fingerprint,
                        transient=transient,
                        next_retry_at=next_check,
                        error=error,
                    ),
                )
                message = _safe_error(error, self.environment)
                store.update_source_state(
                    source_id=plan.source_id,
                    checked_at=_iso(completed),
                    status=status,
                    target_release=plan.target_release,
                    source_vintage=source_vintage,
                    content_sha256=fingerprint,
                    next_retry_at=next_check,
                    error_message=message,
                )
                return {
                    "source_id": plan.source_id,
                    "target_release": plan.target_release,
                    "source_vintage": source_vintage,
                    "status": status,
                    "attempt_count": attempt_number,
                    "content_sha256": fingerprint,
                    "next_retry_at": next_check,
                    "error": message,
                }
        raise AssertionError("Retry loop exited without an outcome")

    def _record_handler_failure(
        self,
        store: OperationsStore,
        run_id: str,
        plan: PlanItem,
    ) -> dict[str, Any]:
        started = self._now()
        attempt_id = store.start_attempt(
            {
                "run_id": run_id,
                "source_id": plan.source_id,
                "target_release": plan.target_release,
                "attempt_number": 1,
                "due_reason": plan.reason,
                "started_at": _iso(started),
            }
        )
        error = ConfigurationError(f"No handler is registered for {plan.source_id}")
        completed = self._now()
        next_check = self._next_poll_at(plan.source_id, completed)
        store.finish_attempt(
            attempt_id,
            self._attempt_values(
                completed_at=_iso(completed),
                status="failed_permanent",
                next_retry_at=next_check,
                error=error,
            ),
        )
        message = _safe_error(error, self.environment)
        store.update_source_state(
            source_id=plan.source_id,
            checked_at=_iso(completed),
            status="failed_permanent",
            target_release=plan.target_release,
            next_retry_at=next_check,
            error_message=message,
        )
        return {
            "source_id": plan.source_id,
            "target_release": plan.target_release,
            "status": "failed_permanent",
            "attempt_count": 1,
            "next_retry_at": next_check,
            "error": message,
        }

    def run(
        self,
        *,
        as_of: str | date | datetime | None = None,
        source_ids: Sequence[str] | None = None,
        force: bool = False,
        refresh_warehouse: bool = True,
        trigger_name: str = "manual",
    ) -> dict[str, Any]:
        effective = _parse_datetime(as_of, self._now())
        plans = self.plan(as_of=effective, source_ids=source_ids, force=force)
        run_id = f"phase6-{_slug(_iso(self._now()))}-{uuid.uuid4().hex[:12]}"
        selected = [plan.source_id for plan in plans]
        store = OperationsStore(self.config.state_database)
        with RunLock(
            self.config.lock_path,
            stale_after_minutes=self.config.stale_after_minutes,
            clock=self._now,
        ):
            store.initialize()
            store.start_run(
                {
                    "run_id": run_id,
                    "started_at": _iso(self._now()),
                    "as_of": _iso(effective),
                    "trigger_name": trigger_name,
                    "force_run": int(force),
                    "warehouse_requested": int(refresh_warehouse),
                    "selected_sources_json": json.dumps(selected),
                    "plan_json": json.dumps([plan.as_dict() for plan in plans], sort_keys=True),
                }
            )

            outcomes: list[dict[str, Any]] = []
            loaded = duplicate = blocked = failures = 0
            not_due = sum(not plan.due for plan in plans)
            warehouse_report: dict[str, Any] | None = None
            warehouse_refreshed = False
            errors: list[str] = []

            for plan in plans:
                if not plan.due:
                    continue
                if plan.action == "blocked_credential":
                    outcome = self._block_credential(store, run_id, plan)
                else:
                    outcome = self._execute_source(store, run_id, plan)
                outcomes.append(outcome)
                if outcome["status"] == "succeeded":
                    loaded += 1
                elif outcome["status"] == "skipped_duplicate":
                    duplicate += 1
                elif outcome["status"] == "blocked_credential":
                    blocked += 1
                    errors.append(f"{plan.source_id}: {outcome.get('error', '')}")
                elif outcome["status"].startswith("failed_"):
                    failures += 1
                    errors.append(f"{plan.source_id}: {outcome.get('error', '')}")

            if loaded and refresh_warehouse:
                refresh_id = store.start_warehouse_refresh(run_id, _iso(self._now()))
                try:
                    warehouse_report = self.warehouse_builder(
                        self.config.staging_database,
                        self.config.warehouse_database,
                        include_fixtures=False,
                    )
                    fact_count = int(warehouse_report.get("table_counts", {}).get("fact_observation", 0))
                    failed_checks = int(warehouse_report.get("failed_quality_check_count", 0))
                    store.finish_warehouse_refresh(
                        refresh_id,
                        completed_at=_iso(self._now()),
                        status="succeeded",
                        fact_count=fact_count,
                        failed_quality_check_count=failed_checks,
                    )
                    warehouse_refreshed = True
                except Exception as error:
                    message = _safe_error(error, self.environment)
                    store.finish_warehouse_refresh(
                        refresh_id,
                        completed_at=_iso(self._now()),
                        status="failed",
                        error_message=message,
                    )
                    failures += 1
                    errors.append(f"warehouse: {message}")

            successful_work = loaded + duplicate
            if failures or blocked:
                status = "partial" if successful_work else "failed"
            else:
                status = "succeeded"
            completed_at = _iso(self._now())
            store.finish_run(
                run_id,
                {
                    "completed_at": completed_at,
                    "status": status,
                    "warehouse_refreshed": int(warehouse_refreshed),
                    "loaded_count": loaded,
                    "duplicate_count": duplicate,
                    "blocked_count": blocked,
                    "failure_count": failures,
                    "not_due_count": not_due,
                    "error_summary": " | ".join(errors)[:4000],
                },
            )
            return {
                "schema_version": self.config.schema_version,
                "run_id": run_id,
                "as_of": _iso(effective),
                "completed_at": completed_at,
                "status": status,
                "loaded_count": loaded,
                "duplicate_count": duplicate,
                "blocked_count": blocked,
                "failure_count": failures,
                "not_due_count": not_due,
                "warehouse_refreshed": warehouse_refreshed,
                "outcomes": outcomes,
                "warehouse": warehouse_report,
            }
