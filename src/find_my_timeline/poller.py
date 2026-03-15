"""Location polling service with random intervals."""

from __future__ import annotations

import logging
import random
import signal
import threading
import time
from datetime import datetime
from typing import Any, Callable

from .auth import AuthenticationError
from .database import LocationDatabase
from .providers import LocationProvider, ProviderError, TrackedEntity

logger = logging.getLogger(__name__)


class LocationPoller:
    """Polls entity locations at random intervals and stores them."""

    def __init__(
        self,
        provider: LocationProvider,
        database: LocationDatabase,
        min_interval: int = 7,
        max_interval: int = 10,
        aps_enabled: bool = False,
        aps_refresh_interval_sec: int = 5,
    ):
        self.provider = provider
        self.database = database
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.aps_enabled = aps_enabled
        self.aps_refresh_interval_sec = aps_refresh_interval_sec

        self._running = False
        self._on_poll_callbacks: list[Callable[[list[dict[str, Any]]], None]] = []
        self._poll_lock = threading.Lock()
        self._aps_refresh_thread: threading.Thread | None = None

    def on_poll(self, callback: Callable[[list[dict[str, Any]]], None]) -> None:
        """Register a callback to be called after each poll."""
        self._on_poll_callbacks.append(callback)

    def _get_next_interval(self) -> float:
        """Get a random interval between min and max (in minutes)."""
        return random.uniform(self.min_interval, self.max_interval) * 60

    def _on_aps_event(self, event: dict[str, Any]) -> None:
        """Handle APS event notifications from provider listener."""
        event_type = event.get("type", "unknown")
        logger.debug("Received APS event: %s", event_type)

    def _run_aps_refresh_loop(self) -> None:
        """Run a fast refresh loop in APS mode (OpenBubbles-style polling cadence)."""
        while self._running:
            time.sleep(self.aps_refresh_interval_sec)
            if not self._running:
                break
            try:
                self.poll_once(source="aps")
            except Exception as exc:
                logger.error("APS refresh poll failed: %s", exc)

    def poll_once(self, source: str = "poll") -> list[dict[str, Any]]:
        """Poll all entities once and store deduplicated locations."""
        with self._poll_lock:
            try:
                entities = self.provider.fetch_entities()
            except (ProviderError, AuthenticationError) as exc:
                logger.error("Failed to fetch entities: %s", exc)
                return []
            except Exception as exc:
                logger.error("Unexpected provider error: %s", exc)
                return []

            recorded, _ = self._ingest_entities(entities, source=source)

            for callback in self._on_poll_callbacks:
                try:
                    callback(recorded)
                except Exception as exc:
                    logger.error("Callback error: %s", exc)

            return recorded

    def ingest_entities(self, entities: list[TrackedEntity], source: str = "manual") -> tuple[list[dict[str, Any]], int]:
        """Insert entities from an external sync/backfill flow."""
        with self._poll_lock:
            return self._ingest_entities(entities, source=source)

    def _ingest_entities(self, entities: list[TrackedEntity], source: str) -> tuple[list[dict[str, Any]], int]:
        recorded: list[dict[str, Any]] = []
        batch_rows: list[dict[str, Any]] = []
        seen_fingerprints: set[str] = set()

        for entity in entities:
            self._upsert_entity(entity)
            for sample in entity.locations:
                fingerprint = self.database.compute_report_fingerprint(
                    entity_type=entity.entity_type,
                    device_id=entity.entity_id,
                    source_backend=entity.source_backend,
                    timestamp=sample.timestamp,
                    latitude=sample.latitude,
                    longitude=sample.longitude,
                    horizontal_accuracy=sample.horizontal_accuracy,
                    key_index=sample.key_index,
                    location_id=sample.location_id,
                )

                if fingerprint in seen_fingerprints:
                    continue

                seen_fingerprints.add(fingerprint)
                batch_rows.append(
                    {
                        "device_id": entity.entity_id,
                        "latitude": sample.latitude,
                        "longitude": sample.longitude,
                        "horizontal_accuracy": sample.horizontal_accuracy,
                        "vertical_accuracy": sample.vertical_accuracy,
                        "position_type": sample.position_type,
                        "battery_level": sample.battery_level,
                        "status": sample.status,
                        "confidence": sample.confidence,
                        "key_index": sample.key_index,
                        "source_backend": entity.source_backend,
                        "raw_json": sample.raw_json,
                        "report_fingerprint": fingerprint,
                        "timestamp": sample.timestamp,
                    }
                )

                recorded.append(
                    {
                        "device_id": entity.entity_id,
                        "device_name": entity.name,
                        "entity_type": entity.entity_type,
                        "source": entity.source_backend,
                        "latitude": sample.latitude,
                        "longitude": sample.longitude,
                        "timestamp": sample.timestamp.isoformat(),
                        "accuracy": sample.horizontal_accuracy,
                        "confidence": sample.confidence,
                        "key_index": sample.key_index,
                        "poll_source": source,
                    }
                )

        inserted = self.database.record_locations_batch(batch_rows)
        if inserted:
            logger.info("Recorded %d location(s) [%s]", inserted, source)
        return recorded, inserted

    def _upsert_entity(self, entity: TrackedEntity) -> None:
        self.database.upsert_device(
            device_id=entity.entity_id,
            name=entity.name,
            device_display_name=entity.device_display_name,
            device_class=entity.device_class,
            entity_type=entity.entity_type,
            source_backend=entity.source_backend,
            metadata=entity.metadata,
        )

    def start(self, setup_signals: bool = True, allow_2fa: bool = False) -> None:
        """Start the polling loop. Blocks until stopped."""
        self._running = True

        if setup_signals:
            try:
                def handle_signal(signum, frame):
                    logger.info("Received signal %s, stopping...", signum)
                    self._running = False

                signal.signal(signal.SIGINT, handle_signal)
                signal.signal(signal.SIGTERM, handle_signal)
            except ValueError:
                pass

        logger.info(
            "Starting location poller backend=%s (interval: %s-%s minutes)",
            self.provider.backend_name,
            self.min_interval,
            self.max_interval,
        )

        try:
            self.provider.authenticate(allow_2fa=allow_2fa)
            logger.info("Authentication successful")
        except (ProviderError, AuthenticationError) as exc:
            logger.error("Authentication failed: %s", exc)
            return

        if self.aps_enabled and self.provider.backend_name == "rustpush":
            try:
                self.provider.start_aps_listener(self._on_aps_event)
                if self.aps_refresh_interval_sec > 0:
                    self._aps_refresh_thread = threading.Thread(
                        target=self._run_aps_refresh_loop,
                        daemon=True,
                    )
                    self._aps_refresh_thread.start()
                logger.info(
                    "APS mode enabled (refresh interval: %ss)",
                    self.aps_refresh_interval_sec,
                )
            except ProviderError as exc:
                logger.error("Failed to start APS mode: %s", exc)

        self.poll_once(source="initial")

        while self._running:
            interval = self._get_next_interval()
            next_poll_time = datetime.fromtimestamp(datetime.now().timestamp() + interval).strftime("%H:%M:%S")
            logger.info("Next poll in %.1f minutes (at %s)", interval / 60, next_poll_time)

            sleep_end = time.time() + interval
            while self._running and time.time() < sleep_end:
                time.sleep(min(1, sleep_end - time.time()))

            if self._running:
                self.poll_once(source="poll")

        logger.info("Poller stopped")
        self.provider.stop()

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        self.provider.stop()
