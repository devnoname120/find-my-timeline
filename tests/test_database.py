from datetime import datetime

from find_my_timeline.database import LocationDatabase


def test_report_fingerprint_stability_and_variation(tmp_path):
    db = LocationDatabase(tmp_path / "locations.db")
    ts = datetime.fromtimestamp(1_710_000_000)

    fp1 = db.compute_report_fingerprint(
        entity_type="item",
        device_id="entity-1",
        source_backend="rustpush",
        timestamp=ts,
        latitude=47.1234567,
        longitude=8.1234567,
        horizontal_accuracy=5.0,
        key_index=42,
        location_id="loc-a",
    )
    fp2 = db.compute_report_fingerprint(
        entity_type="item",
        device_id="entity-1",
        source_backend="rustpush",
        timestamp=ts,
        latitude=47.1234567,
        longitude=8.1234567,
        horizontal_accuracy=5.0,
        key_index=42,
        location_id="loc-a",
    )
    fp3 = db.compute_report_fingerprint(
        entity_type="item",
        device_id="entity-1",
        source_backend="rustpush",
        timestamp=ts,
        latitude=47.1234567,
        longitude=8.1234567,
        horizontal_accuracy=5.0,
        key_index=43,
        location_id="loc-a",
    )

    assert fp1 == fp2
    assert fp1 != fp3


def test_duplicate_suppression_with_unique_fingerprint(tmp_path):
    db = LocationDatabase(tmp_path / "locations.db")
    db.upsert_device("entity-1", "Entity 1", entity_type="item", source_backend="rustpush")
    ts = datetime.fromtimestamp(1_710_000_000)
    fp = db.compute_report_fingerprint(
        entity_type="item",
        device_id="entity-1",
        source_backend="rustpush",
        timestamp=ts,
        latitude=47.0,
        longitude=8.0,
        horizontal_accuracy=10.0,
        key_index=1,
        location_id="loc-1",
    )

    first_id = db.record_location(
        device_id="entity-1",
        latitude=47.0,
        longitude=8.0,
        timestamp=ts,
        horizontal_accuracy=10.0,
        source_backend="rustpush",
        report_fingerprint=fp,
    )
    second_id = db.record_location(
        device_id="entity-1",
        latitude=47.0,
        longitude=8.0,
        timestamp=ts,
        horizontal_accuracy=10.0,
        source_backend="rustpush",
        report_fingerprint=fp,
    )

    assert first_id > 0
    assert second_id == first_id
    assert db.get_location_count("entity-1") == 1


def test_backfill_state_persists_and_updates(tmp_path):
    db = LocationDatabase(tmp_path / "locations.db")
    db.upsert_backfill_state("item-1", "cursor-1", completed=False)

    first = db.get_backfill_state("item-1")
    assert first is not None
    assert first["cursor"] == "cursor-1"
    assert first["completed"] == 0

    db.upsert_backfill_state("item-1", "cursor-2", completed=True)
    second = db.get_backfill_state("item-1")
    assert second is not None
    assert second["cursor"] == "cursor-2"
    assert second["completed"] == 1
