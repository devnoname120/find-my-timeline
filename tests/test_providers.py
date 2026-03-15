from datetime import datetime

from find_my_timeline.providers import (
    RustpushBridgeConfig,
    RustpushBridgeProvider,
    _extract_timestamp,
    _parse_rustpush_sync_payload,
)


def test_parse_rustpush_sync_payload_flattens_multi_reports():
    payload = {
        "entities": [
            {
                "entity_id": "item-1",
                "entity_type": "item",
                "source_backend": "rustpush",
                "name": "Backpack Tag",
                "locations": [
                    {"latitude": 47.1, "longitude": 8.1, "reported_timestamp_ms": 1_710_000_000_000},
                    {"latitude": 47.2, "longitude": 8.2, "reported_timestamp_ms": 1_710_000_100_000},
                ],
            },
            {
                "entity_id": "device-1",
                "entity_type": "device",
                "source_backend": "rustpush",
                "name": "Paul's Mac",
                "location": {"latitude": 48.1, "longitude": 9.1, "timeStamp": 1_710_000_200_000},
                "raw_payload": {
                    "content": [
                        {"location": {"latitude": 48.2, "longitude": 9.2, "timeStamp": 1_710_000_300_000}},
                        {"latitude": 48.3, "longitude": 9.3, "timeStamp": 1_710_000_400_000},
                    ]
                },
            },
            {
                "entity_id": "person-1",
                "entity_type": "person",
                "source_backend": "rustpush",
                "name": "Alice",
                "raw_payload": {
                    "locations": [
                        {"id": "person-1", "location": {"latitude": 46.5, "longitude": 7.5, "timeStamp": 1_710_000_500_000}},
                        {"id": "person-1", "location": {"latitude": 46.6, "longitude": 7.6, "timeStamp": 1_710_000_600_000}},
                    ]
                },
            },
        ]
    }

    entities = _parse_rustpush_sync_payload(payload)
    assert len(entities) == 3

    by_id = {entity.entity_id: entity for entity in entities}
    assert len(by_id["item-1"].locations) == 2
    assert len(by_id["device-1"].locations) == 3
    assert len(by_id["person-1"].locations) == 2


def test_extract_timestamp_precedence_prefers_reported():
    raw = {
        "reported_timestamp_ms": 1_710_000_000_000,
        "timestamp": 1_810_000_000_000,
    }
    ts = _extract_timestamp(raw)
    assert ts == datetime.fromtimestamp(1_710_000_000_000 / 1000)


def test_extract_timestamp_parses_iso_and_numeric_strings():
    iso_ts = _extract_timestamp({"timestamp": "2026-01-02T03:04:05Z"})
    assert iso_ts is not None
    assert iso_ts.year == 2026
    assert iso_ts.month == 1
    assert iso_ts.day == 2

    ms_ts = _extract_timestamp({"locationTimestamp": "1710000123456"})
    assert ms_ts == datetime.fromtimestamp(1_710_000_123_456 / 1000)


def test_backfill_result_parses_checkpoints_and_completed_ids():
    class MockBridgeProvider(RustpushBridgeProvider):
        def _run_bridge(self, args, timeout):
            assert args[0] == "backfill-items"
            return (
                '{"entities":[{"entity_id":"item-1","entity_type":"item","source_backend":"rustpush",'
                '"name":"Tag","locations":[{"latitude":47.0,"longitude":8.0,"timestampMs":1710000000000}]}],'
                '"backfill":{"checkpoints":{"item-1":"cursor-123"},"completed_ids":["item-1"]}}'
            )

    provider = MockBridgeProvider(
        username="test@example.com",
        password="secret",
        config=RustpushBridgeConfig(
            bridge_bin="unused",
            state_dir="/tmp/unused",
            validation_data_path=None,
            sync_timeout_sec=30,
        ),
    )
    result = provider.backfill_items()

    assert len(result.entities) == 1
    assert result.checkpoints == {"item-1": "cursor-123"}
    assert result.completed_ids == {"item-1"}
