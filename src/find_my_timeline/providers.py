"""Location provider backends.

`pyicloud` is the default backend.
`rustpush` is an optional backend that shells out to a Rust bridge binary.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .auth import AuthenticationError, ICloudAuth

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Raised for provider/bridge failures."""


@dataclass
class LocationSample:
    """A normalized location sample."""

    latitude: float
    longitude: float
    timestamp: datetime
    horizontal_accuracy: float | None = None
    vertical_accuracy: float | None = None
    position_type: str | None = None
    battery_level: float | None = None
    status: int | None = None
    confidence: int | None = None
    key_index: int | None = None
    location_id: str | None = None
    raw_json: dict[str, Any] | None = None


@dataclass
class TrackedEntity:
    """A normalized tracked entity with zero or more locations."""

    entity_id: str
    name: str
    entity_type: str
    source_backend: str
    device_display_name: str | None = None
    device_class: str | None = None
    battery_level: float | None = None
    battery_status: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    locations: list[LocationSample] = field(default_factory=list)


@dataclass
class BackfillResult:
    """Result of a backfill cycle."""

    entities: list[TrackedEntity] = field(default_factory=list)
    checkpoints: dict[str, str | None] = field(default_factory=dict)
    completed_ids: set[str] = field(default_factory=set)


class LocationProvider(ABC):
    """Backend interface for location providers."""

    backend_name: str

    @abstractmethod
    def authenticate(self, allow_2fa: bool = True) -> None:
        """Authenticate the backend."""

    @abstractmethod
    def fetch_entities(self) -> list[TrackedEntity]:
        """Fetch entities and available location samples."""

    def backfill_items(self) -> BackfillResult:
        """Optional item backfill hook."""
        return BackfillResult()

    def start_aps_listener(self, on_event: Callable[[dict[str, Any]], None]) -> None:
        """Optional APS listener hook."""

    def stop(self) -> None:
        """Stop/cleanup provider resources."""


class PyiCloudLocationProvider(LocationProvider):
    """Location provider backed by pyicloud."""

    backend_name = "pyicloud"

    def __init__(self, username: str, password: str | None = None):
        self.auth = ICloudAuth(username=username, password=password)

    def authenticate(self, allow_2fa: bool = True) -> None:
        self.auth.authenticate(allow_2fa=allow_2fa)

    def fetch_entities(self) -> list[TrackedEntity]:
        devices = self.auth.get_devices()
        entities: list[TrackedEntity] = []

        for device in devices:
            entity = TrackedEntity(
                entity_id=device["id"],
                name=device["name"],
                entity_type="device",
                source_backend=self.backend_name,
                device_display_name=device.get("device_display_name"),
                device_class=device.get("device_class"),
                battery_level=device.get("battery_level"),
                battery_status=device.get("battery_status"),
                metadata={},
                locations=[],
            )

            location = device.get("location")
            if location:
                timestamp_ms = location.get("timeStamp")
                if timestamp_ms:
                    timestamp = datetime.fromtimestamp(timestamp_ms / 1000)
                elif location.get("isOld", False):
                    timestamp = None
                else:
                    timestamp = datetime.now()

                latitude = location.get("latitude")
                longitude = location.get("longitude")
                if timestamp and latitude is not None and longitude is not None:
                    entity.locations.append(
                        LocationSample(
                            latitude=float(latitude),
                            longitude=float(longitude),
                            timestamp=timestamp,
                            horizontal_accuracy=_maybe_float(location.get("horizontalAccuracy")),
                            position_type=_maybe_str(location.get("positionType")),
                            battery_level=_maybe_float(device.get("battery_level")),
                            raw_json=location,
                        )
                    )

            entities.append(entity)

        return entities


@dataclass
class RustpushBridgeConfig:
    """Configuration for the Rust bridge subprocess."""

    bridge_bin: str
    state_dir: str
    validation_data_path: str | None = None
    sync_timeout_sec: int = 120


class RustpushBridgeProvider(LocationProvider):
    """Location provider backed by the rustpush bridge binary."""

    backend_name = "rustpush"

    def __init__(
        self,
        username: str,
        password: str | None,
        config: RustpushBridgeConfig,
    ):
        self.username = username
        self.password = password
        self.config = config
        self._aps_process: subprocess.Popen[str] | None = None
        self._aps_thread: threading.Thread | None = None

    def authenticate(self, allow_2fa: bool = True) -> None:
        args = [
            "bootstrap",
            "--state-dir",
            self.config.state_dir,
            "--username",
            self.username,
        ]
        if self.password:
            args.extend(["--password", self.password])
        if self.config.validation_data_path:
            args.extend(["--validation-data", self.config.validation_data_path])
        if not allow_2fa:
            args.append("--non-interactive")

        self._run_bridge(args, timeout=self.config.sync_timeout_sec)

    def fetch_entities(self) -> list[TrackedEntity]:
        output = self._run_bridge(
            ["sync", "--state-dir", self.config.state_dir],
            timeout=self.config.sync_timeout_sec,
        )
        data = json.loads(output or "{}")
        return _parse_rustpush_sync_payload(data)

    def backfill_items(self) -> BackfillResult:
        output = self._run_bridge(
            ["backfill-items", "--state-dir", self.config.state_dir],
            timeout=max(self.config.sync_timeout_sec, 300),
        )
        data = json.loads(output or "{}")
        checkpoints: dict[str, str | None] = {}
        completed_ids: set[str] = set()
        backfill_meta = data.get("backfill")
        if isinstance(backfill_meta, dict):
            raw_checkpoints = backfill_meta.get("checkpoints")
            if isinstance(raw_checkpoints, dict):
                for entity_id, cursor in raw_checkpoints.items():
                    entity_id_str = _maybe_str(entity_id)
                    if not entity_id_str:
                        continue
                    checkpoints[entity_id_str] = _maybe_str(cursor)

            raw_completed = backfill_meta.get("completed_ids")
            if isinstance(raw_completed, list):
                for entity_id in raw_completed:
                    entity_id_str = _maybe_str(entity_id)
                    if entity_id_str:
                        completed_ids.add(entity_id_str)

        return BackfillResult(
            entities=_parse_rustpush_sync_payload(data),
            checkpoints=checkpoints,
            completed_ids=completed_ids,
        )

    def start_aps_listener(self, on_event: Callable[[dict[str, Any]], None]) -> None:
        if self._aps_process is not None:
            return

        command = [
            self.config.bridge_bin,
            "listen-aps",
            "--state-dir",
            self.config.state_dir,
        ]

        try:
            self._aps_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise ProviderError(f"Failed to start APS listener: {exc}") from exc

        def _reader() -> None:
            assert self._aps_process is not None
            assert self._aps_process.stdout is not None
            for line in self._aps_process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    if isinstance(payload, dict):
                        on_event(payload)
                except json.JSONDecodeError:
                    logger.debug("Skipping non-JSON APS line: %s", line)

        self._aps_thread = threading.Thread(target=_reader, daemon=True)
        self._aps_thread.start()

    def stop(self) -> None:
        if self._aps_process is not None and self._aps_process.poll() is None:
            self._aps_process.terminate()
            try:
                self._aps_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._aps_process.kill()
        self._aps_process = None
        self._aps_thread = None

    def _run_bridge(self, args: list[str], timeout: int) -> str:
        command = [self.config.bridge_bin, *args]
        env = os.environ.copy()

        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except OSError as exc:
            raise ProviderError(f"Bridge binary failed to execute: {exc}") from exc

        if result.returncode != 0:
            stderr = result.stderr.strip() or "unknown bridge error"
            raise ProviderError(stderr)

        return result.stdout


def build_provider(
    backend: str,
    username: str,
    password: str | None,
    *,
    rustpush_bridge_bin: str,
    rustpush_state_dir: str,
    rustpush_validation_data_path: str | None,
    rustpush_sync_timeout_sec: int,
) -> LocationProvider:
    """Factory for location providers."""
    if backend == "pyicloud":
        return PyiCloudLocationProvider(username=username, password=password)

    if backend == "rustpush":
        return RustpushBridgeProvider(
            username=username,
            password=password,
            config=RustpushBridgeConfig(
                bridge_bin=rustpush_bridge_bin,
                state_dir=rustpush_state_dir,
                validation_data_path=rustpush_validation_data_path,
                sync_timeout_sec=rustpush_sync_timeout_sec,
            ),
        )

    raise ProviderError(f"Unsupported backend '{backend}'")


def _parse_rustpush_sync_payload(payload: dict[str, Any]) -> list[TrackedEntity]:
    """Parse bridge sync payload and flatten multi-location entries."""
    raw_entities = payload.get("entities", [])
    if not isinstance(raw_entities, list):
        return []

    entities: list[TrackedEntity] = []

    for raw_entity in raw_entities:
        if not isinstance(raw_entity, dict):
            continue

        entity_id = _maybe_str(raw_entity.get("entity_id") or raw_entity.get("id"))
        if not entity_id:
            continue

        entity_type = _maybe_str(raw_entity.get("entity_type") or raw_entity.get("type")) or "device"
        source_backend = _maybe_str(raw_entity.get("source_backend") or raw_entity.get("source")) or "rustpush"

        entity = TrackedEntity(
            entity_id=entity_id,
            name=_maybe_str(raw_entity.get("name")) or entity_id,
            entity_type=entity_type,
            source_backend=source_backend,
            device_display_name=_maybe_str(raw_entity.get("device_display_name")),
            device_class=_maybe_str(raw_entity.get("device_class")),
            battery_level=_maybe_float(raw_entity.get("battery_level")),
            battery_status=_maybe_str(raw_entity.get("battery_status")),
            metadata=raw_entity.get("metadata") if isinstance(raw_entity.get("metadata"), dict) else {},
            locations=[],
        )

        for loc in _extract_locations(raw_entity):
            sample = _location_sample_from_raw(loc)
            if sample is not None:
                entity.locations.append(sample)

        entities.append(entity)

    return entities


def _extract_locations(raw_entity: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract all location-like dicts from entity payload fields."""
    collected: list[dict[str, Any]] = []

    # Single-location fields.
    for key in ("location", "last_location", "lastLocation"):
        direct = raw_entity.get(key)
        if isinstance(direct, dict):
            collected.append(direct)

    # Some payloads inline the location object on the entity itself.
    if _looks_like_location(raw_entity):
        collected.append(raw_entity)

    # Multi-location/history collections.
    list_keys = (
        "locations",
        "location_history",
        "locationHistory",
        "locationReports",
        "reports",
        "history",
        "location_payload",
        "locationPayload",
    )
    for key in list_keys:
        locations = raw_entity.get(key)
        if not isinstance(locations, list):
            continue
        for item in locations:
            if isinstance(item, dict):
                if _looks_like_location(item):
                    collected.append(item)
                for nested_key in ("location", "lastLocation", "last_location"):
                    nested = item.get(nested_key)
                    if isinstance(nested, dict):
                        collected.append(nested)

    # Raw response payloads often carry additional reports.
    for key in ("raw_payload", "raw_response", "raw", "payload", "response"):
        raw_payload = raw_entity.get(key)
        if isinstance(raw_payload, (dict, list)):
            collected.extend(_find_location_dicts(raw_payload))

    return collected


def _find_location_dicts(value: Any) -> list[dict[str, Any]]:
    """Recursively find location-like objects inside nested payloads."""
    found: list[dict[str, Any]] = []

    if isinstance(value, dict):
        if _looks_like_location(value):
            found.append(value)
        for nested in value.values():
            found.extend(_find_location_dicts(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_find_location_dicts(nested))

    return found


def _looks_like_location(value: dict[str, Any]) -> bool:
    lat = _maybe_float(value.get("latitude", value.get("lat")))
    lon = _maybe_float(value.get("longitude", value.get("long", value.get("lng"))))
    return lat is not None and lon is not None


def _location_sample_from_raw(raw: dict[str, Any]) -> LocationSample | None:
    lat = _maybe_float(raw.get("latitude", raw.get("lat")))
    lon = _maybe_float(raw.get("longitude", raw.get("long", raw.get("lng"))))
    if lat is None or lon is None:
        return None

    timestamp = _extract_timestamp(raw)
    if timestamp is None:
        return None

    return LocationSample(
        latitude=lat,
        longitude=lon,
        timestamp=timestamp,
        horizontal_accuracy=_maybe_float(
            raw.get("horizontal_accuracy", raw.get("horizontalAccuracy", raw.get("accuracy")))
        ),
        vertical_accuracy=_maybe_float(raw.get("vertical_accuracy", raw.get("verticalAccuracy"))),
        position_type=_maybe_str(raw.get("position_type", raw.get("positionType"))),
        battery_level=_maybe_float(raw.get("battery_level", raw.get("batteryLevel"))),
        status=_maybe_int(raw.get("status")),
        confidence=_maybe_int(raw.get("confidence")),
        key_index=_maybe_int(raw.get("key_index", raw.get("keyIndex", raw.get("idx")))),
        location_id=_maybe_str(raw.get("location_id", raw.get("locationId"))),
        raw_json=raw,
    )


def _extract_timestamp(raw: dict[str, Any]) -> datetime | None:
    # Prefer explicit "reported" values over generic timestamps.
    candidates = [
        raw.get("reported_timestamp_ms"),
        raw.get("reportedTimestampMs"),
        raw.get("reported_timestamp"),
        raw.get("reportedTimestamp"),
        raw.get("timestamp_ms"),
        raw.get("timestampMs"),
        raw.get("location_timestamp"),
        raw.get("locationTimestamp"),
        raw.get("timeStamp"),
        raw.get("timestamp"),
        raw.get("secure_location_ts"),
        raw.get("secureLocationTs"),
    ]

    for candidate in candidates:
        if candidate is None:
            continue

        parsed = _parse_timestamp_candidate(candidate)
        if parsed is not None:
            return parsed

    return None


def _parse_timestamp_candidate(value: Any) -> datetime | None:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            return _parse_timestamp_candidate(int(stripped))
        try:
            return _parse_timestamp_candidate(float(stripped))
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            return None

    if isinstance(value, (int, float)):
        value_f = float(value)
        # microseconds
        if value_f > 1e14:
            return datetime.fromtimestamp(value_f / 1_000_000)
        # milliseconds
        if value_f > 1e11:
            return datetime.fromtimestamp(value_f / 1_000)
        # seconds
        if value_f > 1e9:
            return datetime.fromtimestamp(value_f)
        return None

    return None


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _maybe_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)
