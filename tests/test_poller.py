from datetime import datetime

from find_my_timeline.database import LocationDatabase
from find_my_timeline.poller import LocationPoller
from find_my_timeline.providers import (
    BackfillResult,
    LocationProvider,
    LocationSample,
    PyiCloudLocationProvider,
    TrackedEntity,
    _parse_rustpush_sync_payload,
    build_provider,
)


class StaticProvider(LocationProvider):
    backend_name = "rustpush"

    def __init__(self, entities):
        self._entities = entities

    def authenticate(self, allow_2fa: bool = True) -> None:
        return None

    def fetch_entities(self):
        return self._entities

    def backfill_items(self) -> BackfillResult:
        return BackfillResult(entities=self._entities)


def _sample_entity(entity_id: str, locations: list[LocationSample]) -> TrackedEntity:
    return TrackedEntity(
        entity_id=entity_id,
        name=entity_id,
        entity_type="item",
        source_backend="rustpush",
        locations=locations,
    )


def test_replay_identical_payload_chunks_is_idempotent(tmp_path):
    db = LocationDatabase(tmp_path / "locations.db")
    entities = [
        _sample_entity(
            "item-1",
            [
                LocationSample(latitude=47.0, longitude=8.0, timestamp=datetime.fromtimestamp(1_710_000_000)),
                LocationSample(latitude=47.1, longitude=8.1, timestamp=datetime.fromtimestamp(1_710_000_100)),
            ],
        )
    ]
    poller = LocationPoller(provider=StaticProvider(entities), database=db)

    poller.poll_once(source="poll")
    first_count = db.get_location_count("item-1")
    poller.poll_once(source="poll")
    second_count = db.get_location_count("item-1")

    assert first_count == 2
    assert second_count == 2


def test_mixed_single_and_multi_report_payload_inserts_all_unique_points(tmp_path):
    db = LocationDatabase(tmp_path / "locations.db")
    payload = {
        "entities": [
            {
                "entity_id": "item-2",
                "entity_type": "item",
                "source_backend": "rustpush",
                "name": "Item 2",
                "location": {"latitude": 46.0, "longitude": 7.0, "timeStamp": 1_710_000_000_000},
                "locations": [
                    {"latitude": 46.0, "longitude": 7.0, "timeStamp": 1_710_000_000_000},
                    {"latitude": 46.1, "longitude": 7.1, "timeStamp": 1_710_000_100_000},
                    {"latitude": 46.2, "longitude": 7.2, "timeStamp": 1_710_000_200_000},
                ],
            }
        ]
    }
    entities = _parse_rustpush_sync_payload(payload)
    poller = LocationPoller(provider=StaticProvider([]), database=db)
    _, inserted = poller.ingest_entities(entities, source="backfill")

    assert inserted == 3
    assert db.get_location_count("item-2") == 3


def test_aps_event_alone_does_not_write_coordinates_but_refresh_poll_does(tmp_path):
    db = LocationDatabase(tmp_path / "locations.db")
    entities = [
        _sample_entity(
            "person-1",
            [LocationSample(latitude=48.0, longitude=9.0, timestamp=datetime.fromtimestamp(1_710_000_300))],
        )
    ]
    poller = LocationPoller(provider=StaticProvider(entities), database=db, aps_enabled=True, aps_refresh_interval_sec=1)

    poller._on_aps_event({"type": "findmy.import"})
    assert db.get_location_count("person-1") == 0

    poller.poll_once(source="aps")
    assert db.get_location_count("person-1") == 1


def test_pyicloud_backend_default_unchanged():
    provider = build_provider(
        backend="pyicloud",
        username="user@example.com",
        password="secret",
        rustpush_bridge_bin="unused",
        rustpush_state_dir="unused",
        rustpush_validation_data_path=None,
        rustpush_sync_timeout_sec=120,
    )
    assert isinstance(provider, PyiCloudLocationProvider)
