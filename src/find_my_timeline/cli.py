"""Command-line interface for Find My Timeline."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from threading import Thread

import click
from dotenv import load_dotenv

from .auth import AuthenticationError
from .database import LocationDatabase
from .poller import LocationPoller
from .providers import ProviderError, build_provider
from .web import create_app

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _getenv_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_config() -> dict[str, object]:
    """Get configuration from environment variables."""
    return {
        "username": os.getenv("ICLOUD_USERNAME"),
        "password": os.getenv("ICLOUD_PASSWORD"),
        "backend": os.getenv("LOCATION_BACKEND", "pyicloud").strip().lower(),
        "min_interval": int(os.getenv("POLL_MIN_INTERVAL", "7")),
        "max_interval": int(os.getenv("POLL_MAX_INTERVAL", "10")),
        "db_path": os.getenv("DATABASE_PATH", "./data/locations.db"),
        "web_host": os.getenv("WEB_HOST", "127.0.0.1"),
        "web_port": int(os.getenv("WEB_PORT", "5000")),
        "rustpush_bridge_bin": os.getenv(
            "RUSTPUSH_BRIDGE_BIN",
            "./rustpush_bridge/target/release/find-my-rustpush-bridge",
        ),
        "rustpush_state_dir": os.getenv(
            "RUSTPUSH_STATE_DIR",
            str(Path.home() / ".find-my-timeline" / "rustpush"),
        ),
        "rustpush_bridge_delegate": os.getenv("RUSTPUSH_BRIDGE_DELEGATE"),
        "rustpush_allow_contract_mode": _getenv_bool("RUSTPUSH_ALLOW_CONTRACT_MODE", False),
        "rustpush_validation_data_path": os.getenv("RUSTPUSH_VALIDATION_DATA_PATH"),
        "rustpush_sync_timeout_sec": int(os.getenv("RUSTPUSH_SYNC_TIMEOUT_SEC", "120")),
        "rustpush_aps_enabled": _getenv_bool("RUSTPUSH_APS_ENABLED", True),
        "rustpush_aps_refresh_interval_sec": int(os.getenv("RUSTPUSH_APS_REFRESH_INTERVAL_SEC", "5")),
    }


def build_location_provider(config: dict[str, object], username: str, password: str | None, backend: str):
    """Create a configured location provider instance."""
    return build_provider(
        backend=backend,
        username=username,
        password=password,
        rustpush_bridge_bin=str(config["rustpush_bridge_bin"]),
        rustpush_state_dir=str(config["rustpush_state_dir"]),
        rustpush_bridge_delegate=(
            str(config["rustpush_bridge_delegate"])
            if config["rustpush_bridge_delegate"]
            else None
        ),
        rustpush_allow_contract_mode=bool(config["rustpush_allow_contract_mode"]),
        rustpush_validation_data_path=(
            str(config["rustpush_validation_data_path"])
            if config["rustpush_validation_data_path"]
            else None
        ),
        rustpush_sync_timeout_sec=int(config["rustpush_sync_timeout_sec"]),
    )


@click.group()
@click.version_option(version="0.1.0")
def main():
    """Find My Timeline - Track Apple Find My location history over time."""


@main.command()
@click.option("--username", "-u", help="Apple ID username")
@click.option("--password", "-p", help="Apple ID password", hide_input=True)
@click.option("--backend", type=click.Choice(["pyicloud", "rustpush"]), help="Location backend")
@click.option("--validation-data", type=click.Path(exists=True), help="Validation data file (rustpush bootstrap)")
def auth(username, password, backend, validation_data):
    """Authenticate and persist backend session state."""
    config = get_config()
    username = username or str(config["username"] or "")
    if not username:
        username = click.prompt("Enter your Apple ID")

    password = password or (str(config["password"]) if config["password"] else None)
    backend = backend or str(config["backend"])

    if backend == "pyicloud" and not password:
        password = click.prompt("Enter your password", hide_input=True)

    if backend == "rustpush" and validation_data:
        config["rustpush_validation_data_path"] = validation_data

    click.echo(f"Authenticating backend={backend} as {username}...")

    try:
        provider = build_location_provider(config, username, password, backend)
        provider.authenticate(allow_2fa=True)
        click.echo("Authentication successful! Session saved.")

        entities = provider.fetch_entities()
        click.echo(f"\nFound {len(entities)} entity(ies):")
        for entity in entities:
            location_status = "Has locations" if entity.locations else "No locations"
            click.echo(f"  - [{entity.entity_type}] {entity.name} ({entity.entity_id}) - {location_status}")
    except ProviderError as exc:
        click.echo(f"Authentication failed: {exc}", err=True)
        sys.exit(1)


@main.command()
@click.option("--username", "-u", help="Apple ID username")
@click.option("--password", "-p", help="Apple ID password")
@click.option("--backend", type=click.Choice(["pyicloud", "rustpush"]), help="Location backend")
@click.option("--min-interval", type=int, help="Minimum polling interval in minutes")
@click.option("--max-interval", type=int, help="Maximum polling interval in minutes")
def poll(username, password, backend, min_interval, max_interval):
    """Start the location polling service."""
    config = get_config()

    username = username or str(config["username"] or "")
    if not username:
        click.echo("Error: username required. Set ICLOUD_USERNAME or pass --username", err=True)
        sys.exit(1)

    password = password or (str(config["password"]) if config["password"] else None)
    backend = backend or str(config["backend"])
    min_interval = min_interval or int(config["min_interval"])
    max_interval = max_interval or int(config["max_interval"])

    click.echo(f"Starting poller backend={backend} (interval: {min_interval}-{max_interval} minutes)")
    click.echo(f"Database: {config['db_path']}")

    provider = build_location_provider(config, username, password, backend)
    database = LocationDatabase(str(config["db_path"]))
    poller = LocationPoller(
        provider=provider,
        database=database,
        min_interval=min_interval,
        max_interval=max_interval,
        aps_enabled=backend == "rustpush" and bool(config["rustpush_aps_enabled"]),
        aps_refresh_interval_sec=int(config["rustpush_aps_refresh_interval_sec"]),
    )

    def on_poll(locations):
        if locations:
            click.echo(f"Recorded {len(locations)} location sample(s)")

    poller.on_poll(on_poll)

    try:
        poller.start()
    except KeyboardInterrupt:
        click.echo("\nStopping poller...")


@main.command("backfill-items")
@click.option("--username", "-u", help="Apple ID username")
@click.option("--password", "-p", help="Apple ID password")
@click.option("--backend", type=click.Choice(["rustpush"]), default="rustpush", show_default=True)
def backfill_items(username, password, backend):
    """Run rustpush item backfill (progressive historical crawl)."""
    config = get_config()

    username = username or str(config["username"] or "")
    if not username:
        click.echo("Error: username required. Set ICLOUD_USERNAME or pass --username", err=True)
        sys.exit(1)

    password = password or (str(config["password"]) if config["password"] else None)

    provider = build_location_provider(config, username, password, backend)
    database = LocationDatabase(str(config["db_path"]))
    poller = LocationPoller(provider=provider, database=database)

    click.echo("Running item backfill...")
    try:
        provider.authenticate(allow_2fa=False)
        result = provider.backfill_items()
        _, inserted = poller.ingest_entities(result.entities, source="backfill")

        for entity_id, cursor in result.checkpoints.items():
            database.upsert_backfill_state(
                entity_id=entity_id,
                cursor=cursor,
                completed=entity_id in result.completed_ids,
            )
        for entity_id in result.completed_ids:
            if entity_id not in result.checkpoints:
                database.upsert_backfill_state(entity_id=entity_id, cursor=None, completed=True)

    except (ProviderError, AuthenticationError) as exc:
        click.echo(f"Backfill failed: {exc}", err=True)
        sys.exit(1)

    click.echo(
        f"Backfill command completed: entities={len(result.entities)}, "
        f"inserted_locations={inserted}, checkpoints={len(result.checkpoints)}"
    )


@main.command()
@click.option("--host", "-h", help="Host to bind to")
@click.option("--port", "-p", type=int, help="Port to bind to")
def web(host, port):
    """Start the web interface."""
    config = get_config()

    host = host or str(config["web_host"])
    port = port or int(config["web_port"])

    database = LocationDatabase(str(config["db_path"]))
    app = create_app(database)

    click.echo(f"Starting web server at http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


@main.command()
@click.option("--username", "-u", help="Apple ID username")
@click.option("--password", "-p", help="Apple ID password")
@click.option("--backend", type=click.Choice(["pyicloud", "rustpush"]), help="Location backend")
@click.option("--host", help="Web server host")
@click.option("--port", type=int, help="Web server port")
def start(username, password, backend, host, port):
    """Start both poller and web interface."""
    config = get_config()

    username = username or str(config["username"] or "")
    if not username:
        click.echo("Error: username required. Set ICLOUD_USERNAME or pass --username", err=True)
        sys.exit(1)

    password = password or (str(config["password"]) if config["password"] else None)
    backend = backend or str(config["backend"])
    host = host or str(config["web_host"])
    port = port or int(config["web_port"])

    provider = build_location_provider(config, username, password, backend)
    database = LocationDatabase(str(config["db_path"]))

    poller = LocationPoller(
        provider=provider,
        database=database,
        min_interval=int(config["min_interval"]),
        max_interval=int(config["max_interval"]),
        aps_enabled=backend == "rustpush" and bool(config["rustpush_aps_enabled"]),
        aps_refresh_interval_sec=int(config["rustpush_aps_refresh_interval_sec"]),
    )

    app = create_app(database)

    click.echo("Starting Find My Timeline")
    click.echo(f"  Backend: {backend}")
    click.echo(f"  Polling interval: {config['min_interval']}-{config['max_interval']} minutes")
    click.echo(f"  Web interface: http://{host}:{port}")
    click.echo(f"  Database: {config['db_path']}")

    poller_thread = Thread(target=poller.start, daemon=True)
    poller_thread.start()

    try:
        app.run(host=host, port=port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        click.echo("\nShutting down...")
        poller.stop()


@main.command()
def stats():
    """Show database statistics."""
    config = get_config()
    db_path = Path(str(config["db_path"]))

    if not db_path.exists():
        click.echo("No database found. Run 'poll' first to start collecting data.")
        return

    database = LocationDatabase(db_path)
    devices = database.get_devices()
    total = database.get_location_count()

    click.echo(f"Database: {db_path}")
    click.echo(f"Total locations: {total:,}")
    click.echo(f"Tracked entities: {len(devices)}")

    for device in devices:
        count = database.get_location_count(device["id"])
        latest = database.get_latest_location(device["id"])
        latest_time = latest["timestamp"] if latest else "Never"

        click.echo(
            f"\n  {device['name']} ({device.get('device_display_name') or 'Unknown'}) "
            f"[{device.get('entity_type', 'device')}]"
        )
        click.echo(f"    Locations: {count:,}")
        click.echo(f"    Last seen: {latest_time}")


@main.command()
def devices():
    """List tracked entities."""
    config = get_config()
    db_path = Path(str(config["db_path"]))

    if not db_path.exists():
        click.echo("No database found. Run 'poll' first to start collecting data.")
        return

    database = LocationDatabase(db_path)
    device_list = database.get_devices()

    if not device_list:
        click.echo("No entities found.")
        return

    click.echo(f"Tracked entities ({len(device_list)}):\n")

    for device in device_list:
        latest = database.get_latest_location(device["id"])
        click.echo(f"  {device['name']}")
        click.echo(f"    Type: {device.get('entity_type', 'device')}")
        click.echo(f"    Display: {device.get('device_display_name') or 'Unknown'}")
        click.echo(f"    Source: {device.get('source_backend', 'unknown')}")
        click.echo(f"    ID: {device['id']}")
        if latest:
            click.echo(f"    Last location: ({latest['latitude']:.6f}, {latest['longitude']:.6f})")
            click.echo(f"    Last seen: {latest['timestamp']}")
        click.echo()


if __name__ == "__main__":
    main()
