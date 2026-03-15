# Find My Timeline

Track historical Find My location data over time.

![Preview](preview.png)
![Preview Detail](preview2.png)
![Preview Timeline](preview3.png)

## Backends

- `pyicloud` (default): current-state polling via pyicloud.
- `rustpush` (optional): bridge-based ingestion path designed for location timelines with item/device/person entities and multi-report payload flattening.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp .env.example .env
```

## Quick Start

```bash
# 1) Authenticate backend session
find-my-timeline auth

# 2) Run poller + web UI
find-my-timeline start

# 3) Open:
# http://127.0.0.1:5000
```

## Commands

| Command | Description |
|---------|-------------|
| `auth` | Authenticate and persist session state |
| `poll` | Start polling only |
| `web` | Start web UI only |
| `start` | Start poller + web UI |
| `backfill-items` | Run rustpush item backfill cycle |
| `stats` | Show DB statistics |
| `devices` | List tracked entities |

## rustpush Bridge

The repo includes a Rust bridge binary contract at `rustpush_bridge/` with commands:

- `bootstrap`
- `sync`
- `backfill-items`
- `listen-aps`

By default, the bundled bridge reads payload files from its state directory (`payloads/*.json*`) and persists backfill cursor state.  
Set `RUSTPUSH_BRIDGE_DELEGATE` to forward bridge commands to an external runtime binary.

Build it for local non-Docker runs:

```bash
cd rustpush_bridge
cargo build --release
```

Set in `.env`:

- `LOCATION_BACKEND=rustpush`
- `RUSTPUSH_BRIDGE_BIN=./rustpush_bridge/target/release/find-my-rustpush-bridge`
- `RUSTPUSH_STATE_DIR=~/.find-my-timeline/rustpush`

Docker images already bundle `find-my-rustpush-bridge`, so no local Cargo build is required for containerized runs.

APS behavior follows OpenBubbles-style handling:

- APS events are treated as update signals (not direct coordinate writes).
- APS mode triggers extra fast refresh polling (`RUSTPUSH_APS_REFRESH_INTERVAL_SEC`, default 5s).
- Base polling cadence remains random 7-10 minutes.

## Configuration

Set in `.env` or pass CLI flags:

- `ICLOUD_USERNAME`, `ICLOUD_PASSWORD`
- `LOCATION_BACKEND`
- `POLL_MIN_INTERVAL`, `POLL_MAX_INTERVAL`
- `DATABASE_PATH`
- `WEB_HOST`, `WEB_PORT`
- `RUSTPUSH_BRIDGE_BIN`
- `RUSTPUSH_STATE_DIR`
- `RUSTPUSH_VALIDATION_DATA_PATH`
- `RUSTPUSH_SYNC_TIMEOUT_SEC`
- `RUSTPUSH_APS_ENABLED`
- `RUSTPUSH_APS_REFRESH_INTERVAL_SEC`

## Docker

### First-time setup

```bash
cp .env.example .env
# edit .env (at minimum: ICLOUD_USERNAME)

mkdir -p session data
docker compose run --rm find-my-timeline find-my-timeline auth
# enter 2FA code when prompted
```

### Run service

```bash
docker compose up -d --build
# open http://127.0.0.1:5000
```

### Use rustpush backend in Docker

Set these in `.env` before starting:

```bash
LOCATION_BACKEND=rustpush
```

Notes:

- `docker-compose.yml` forces `RUSTPUSH_BRIDGE_BIN` to the in-container binary path.
- Rustpush state persists in `./session/rustpush` on the host (mounted from `/root/.find-my-timeline/rustpush`).
- For the bundled bridge contract mode, place payload files under `./session/rustpush/payloads/`:
  - `sync.json`
  - `backfill.jsonl`
  - `aps.ndjson`
- To use an external runtime bridge inside the container, set `RUSTPUSH_BRIDGE_DELEGATE` in `.env`.

### Re-authenticate (session expired)

```bash
docker compose run --rm find-my-timeline find-my-timeline auth
```

### Stop

```bash
docker compose down
```
