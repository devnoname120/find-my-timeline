# find-my-rustpush-bridge

Bridge binary contract for the Python `rustpush` backend.

## Commands

- `bootstrap --state-dir ... --username ... [--password ...] [--validation-data ...]`
- `sync --state-dir ...`
- `backfill-items --state-dir ...`
- `listen-aps --state-dir ...`

## State Directory Layout

The bridge uses:

- `runtime/bridge_state.json`
- `runtime/backfill_cursor.json`
- `payloads/sync.json`
- `payloads/backfill.jsonl`
- `payloads/aps.ndjson`

`sync` expects one JSON object with an `entities` array.  
`backfill-items` reads progressive JSON chunks from `payloads/backfill.jsonl` and persists cursor/checkpoint state.  
`listen-aps` streams newline-delimited JSON events from `payloads/aps.ndjson`.

## Delegation

If `RUSTPUSH_BRIDGE_DELEGATE` is set, this binary forwards all commands/args to that executable and exits with the same status code.
The Python app treats delegation as the default runtime mode and only uses local file payload mode when explicitly allowed.
