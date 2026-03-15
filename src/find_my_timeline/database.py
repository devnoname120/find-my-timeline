"""SQLite database for storing location history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


class LocationDatabase:
    """Manages SQLite database for location history."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize and migrate the database schema."""
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    device_display_name TEXT,
                    device_class TEXT,
                    entity_type TEXT NOT NULL DEFAULT 'device',
                    source_backend TEXT NOT NULL DEFAULT 'pyicloud',
                    metadata_json TEXT,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS locations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    horizontal_accuracy REAL,
                    vertical_accuracy REAL,
                    position_type TEXT,
                    battery_level REAL,
                    status INTEGER,
                    confidence INTEGER,
                    key_index INTEGER,
                    source_backend TEXT NOT NULL DEFAULT 'pyicloud',
                    raw_json TEXT,
                    report_fingerprint TEXT,
                    timestamp TIMESTAMP NOT NULL,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (device_id) REFERENCES devices(id)
                );

                CREATE TABLE IF NOT EXISTS backfill_state (
                    entity_id TEXT PRIMARY KEY,
                    cursor TEXT,
                    completed INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_locations_device_id
                    ON locations(device_id);
                CREATE INDEX IF NOT EXISTS idx_locations_timestamp
                    ON locations(timestamp);
                CREATE INDEX IF NOT EXISTS idx_locations_device_timestamp
                    ON locations(device_id, timestamp);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_locations_fingerprint
                    ON locations(report_fingerprint);
                """
            )

            # Migration safety for existing databases.
            self._ensure_column(conn, "devices", "entity_type", "TEXT NOT NULL DEFAULT 'device'")
            self._ensure_column(conn, "devices", "source_backend", "TEXT NOT NULL DEFAULT 'pyicloud'")
            self._ensure_column(conn, "devices", "metadata_json", "TEXT")

            self._ensure_column(conn, "locations", "vertical_accuracy", "REAL")
            self._ensure_column(conn, "locations", "status", "INTEGER")
            self._ensure_column(conn, "locations", "confidence", "INTEGER")
            self._ensure_column(conn, "locations", "key_index", "INTEGER")
            self._ensure_column(conn, "locations", "source_backend", "TEXT NOT NULL DEFAULT 'pyicloud'")
            self._ensure_column(conn, "locations", "raw_json", "TEXT")
            self._ensure_column(conn, "locations", "report_fingerprint", "TEXT")

            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_locations_fingerprint
                ON locations(report_fingerprint)
                """
            )

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        """Get a database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        """Add a column if missing (simple migration path)."""
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _serialize_timestamp(value: datetime | str) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    @staticmethod
    def compute_report_fingerprint(
        *,
        entity_type: str,
        device_id: str,
        source_backend: str,
        timestamp: datetime,
        latitude: float,
        longitude: float,
        horizontal_accuracy: float | None = None,
        key_index: int | None = None,
        location_id: str | None = None,
    ) -> str:
        """Build a deterministic dedupe fingerprint."""
        parts = [
            entity_type,
            device_id,
            source_backend,
            str(int(timestamp.timestamp() * 1000)),
            f"{latitude:.7f}",
            f"{longitude:.7f}",
            "" if horizontal_accuracy is None else f"{horizontal_accuracy:.3f}",
            "" if key_index is None else str(key_index),
            "" if location_id is None else location_id,
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    def upsert_device(
        self,
        device_id: str,
        name: str,
        device_display_name: str | None = None,
        device_class: str | None = None,
        entity_type: str = "device",
        source_backend: str = "pyicloud",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert or update a tracked entity."""
        metadata_json = json.dumps(metadata, separators=(",", ":"), sort_keys=True) if metadata else None
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO devices (
                    id, name, device_display_name, device_class, entity_type,
                    source_backend, metadata_json, last_seen
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    device_display_name = excluded.device_display_name,
                    device_class = excluded.device_class,
                    entity_type = excluded.entity_type,
                    source_backend = excluded.source_backend,
                    metadata_json = excluded.metadata_json,
                    last_seen = CURRENT_TIMESTAMP
                """,
                (
                    device_id,
                    name,
                    device_display_name,
                    device_class,
                    entity_type,
                    source_backend,
                    metadata_json,
                ),
            )

    def record_location(
        self,
        device_id: str,
        latitude: float,
        longitude: float,
        timestamp: datetime,
        horizontal_accuracy: float | None = None,
        vertical_accuracy: float | None = None,
        position_type: str | None = None,
        battery_level: float | None = None,
        status: int | None = None,
        confidence: int | None = None,
        key_index: int | None = None,
        source_backend: str = "pyicloud",
        raw_json: dict[str, Any] | None = None,
        report_fingerprint: str | None = None,
    ) -> int:
        """Record a location point for an entity. Returns the location ID."""
        raw_json_text = json.dumps(raw_json, separators=(",", ":"), sort_keys=True) if raw_json else None

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO locations (
                    device_id, latitude, longitude, horizontal_accuracy, vertical_accuracy,
                    position_type, battery_level, status, confidence, key_index,
                    source_backend, raw_json, report_fingerprint, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    latitude,
                    longitude,
                    horizontal_accuracy,
                    vertical_accuracy,
                    position_type,
                    battery_level,
                    status,
                    confidence,
                    key_index,
                    source_backend,
                    raw_json_text,
                    report_fingerprint,
                    self._serialize_timestamp(timestamp),
                ),
            )
            if cursor.rowcount:
                return cursor.lastrowid

            if report_fingerprint:
                row = conn.execute(
                    "SELECT id FROM locations WHERE report_fingerprint = ?",
                    (report_fingerprint,),
                ).fetchone()
                if row:
                    return int(row[0])

            return 0

    def record_locations_batch(self, records: list[dict[str, Any]]) -> int:
        """Insert a batch of location rows. Returns number of inserted rows."""
        if not records:
            return 0

        prepared: list[tuple[Any, ...]] = []
        for record in records:
            raw_json = record.get("raw_json")
            raw_json_text = json.dumps(raw_json, separators=(",", ":"), sort_keys=True) if raw_json else None
            prepared.append(
                (
                    record["device_id"],
                    record["latitude"],
                    record["longitude"],
                    record.get("horizontal_accuracy"),
                    record.get("vertical_accuracy"),
                    record.get("position_type"),
                    record.get("battery_level"),
                    record.get("status"),
                    record.get("confidence"),
                    record.get("key_index"),
                    record.get("source_backend", "pyicloud"),
                    raw_json_text,
                    record.get("report_fingerprint"),
                    self._serialize_timestamp(record["timestamp"]),
                )
            )

        with self._get_connection() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO locations (
                    device_id, latitude, longitude, horizontal_accuracy, vertical_accuracy,
                    position_type, battery_level, status, confidence, key_index,
                    source_backend, raw_json, report_fingerprint, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                prepared,
            )
            return conn.total_changes - before

    def upsert_backfill_state(self, entity_id: str, cursor: str | None, completed: bool = False) -> None:
        """Persist or update an item backfill cursor/checkpoint."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO backfill_state (entity_id, cursor, completed, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(entity_id) DO UPDATE SET
                    cursor = excluded.cursor,
                    completed = excluded.completed,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (entity_id, cursor, 1 if completed else 0),
            )

    def get_backfill_state(self, entity_id: str) -> dict[str, Any] | None:
        """Get backfill checkpoint for one entity."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM backfill_state WHERE entity_id = ?",
                (entity_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_backfill_states(self) -> list[dict[str, Any]]:
        """Get all backfill checkpoints."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM backfill_state ORDER BY updated_at DESC").fetchall()
            return [dict(row) for row in rows]

    def get_devices(self) -> list[dict]:
        """Get all known tracked entities."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
            return [dict(row) for row in rows]

    def get_locations(
        self,
        device_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Get location history with optional filters."""
        query = "SELECT * FROM locations WHERE 1=1"
        params: list[Any] = []

        if device_id:
            query += " AND device_id = ?"
            params.append(device_id)

        if start_time:
            query += " AND timestamp >= ?"
            params.append(self._serialize_timestamp(start_time))

        if end_time:
            query += " AND timestamp <= ?"
            params.append(self._serialize_timestamp(end_time))

        query += " ORDER BY timestamp DESC"

        if limit:
            query += " LIMIT ?"
            params.append(limit)

        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def get_latest_location(self, device_id: str) -> dict | None:
        """Get the most recent location for a specific entity."""
        locations = self.get_locations(device_id=device_id, limit=1)
        return locations[0] if locations else None

    def get_location_count(self, device_id: str | None = None) -> int:
        """Get total number of recorded locations."""
        query = "SELECT COUNT(*) FROM locations"
        params: list[Any] = []

        if device_id:
            query += " WHERE device_id = ?"
            params.append(device_id)

        with self._get_connection() as conn:
            return int(conn.execute(query, params).fetchone()[0])
