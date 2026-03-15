use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::thread;
use std::time::Duration;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::{json, Value};

#[derive(Parser, Debug)]
#[command(name = "find-my-rustpush-bridge")]
#[command(about = "Rust bridge contract for rustpush-backed Find My timeline ingestion")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand, Debug)]
enum Commands {
    /// Persist bootstrap/session configuration in the state directory.
    Bootstrap {
        #[arg(long)]
        state_dir: PathBuf,
        #[arg(long)]
        username: String,
        #[arg(long)]
        password: Option<String>,
        #[arg(long)]
        validation_data: Option<PathBuf>,
        #[arg(long, default_value_t = false)]
        non_interactive: bool,
    },
    /// Emit one JSON payload containing an `entities` list.
    Sync {
        #[arg(long)]
        state_dir: PathBuf,
    },
    /// Emit one JSON payload chunk for progressive item backfill.
    #[command(name = "backfill-items")]
    BackfillItems {
        #[arg(long)]
        state_dir: PathBuf,
    },
    /// Stream newline-delimited APS event JSON payloads to stdout.
    #[command(name = "listen-aps")]
    ListenAps {
        #[arg(long)]
        state_dir: PathBuf,
    },
}

#[derive(Debug, Serialize, Deserialize, Default)]
struct BridgeState {
    username: String,
    password: Option<String>,
    validation_data: Option<String>,
    non_interactive: bool,
}

#[derive(Debug, Serialize, Deserialize, Default)]
struct BackfillCursorState {
    next_line: usize,
    checkpoints: BTreeMap<String, Option<String>>,
    completed_ids: BTreeSet<String>,
}

fn main() {
    if let Err(err) = run() {
        eprintln!("{err:#}");
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    if let Ok(delegate) = std::env::var("RUSTPUSH_BRIDGE_DELEGATE") {
        if !delegate.trim().is_empty() {
            let status = Command::new(delegate)
                .args(std::env::args().skip(1))
                .status()
                .context("failed to execute delegated bridge command")?;
            std::process::exit(status.code().unwrap_or(1));
        }
    }

    let cli = Cli::parse();
    match cli.command {
        Commands::Bootstrap {
            state_dir,
            username,
            password,
            validation_data,
            non_interactive,
        } => bootstrap(
            &state_dir,
            username,
            password,
            validation_data,
            non_interactive,
        ),
        Commands::Sync { state_dir } => sync(&state_dir),
        Commands::BackfillItems { state_dir } => backfill_items(&state_dir),
        Commands::ListenAps { state_dir } => listen_aps(&state_dir),
    }
}

fn bootstrap(
    state_dir: &Path,
    username: String,
    password: Option<String>,
    validation_data: Option<PathBuf>,
    non_interactive: bool,
) -> Result<()> {
    ensure_state_layout(state_dir)?;
    let state = BridgeState {
        username,
        password,
        validation_data: validation_data
            .as_ref()
            .map(|path| path.to_string_lossy().to_string()),
        non_interactive,
    };
    write_json_file(&state_file(state_dir), &state)?;
    print_json(&json!({"ok": true}))?;
    Ok(())
}

fn sync(state_dir: &Path) -> Result<()> {
    ensure_state_layout(state_dir)?;
    let sync_payload = payloads_dir(state_dir).join("sync.json");
    let payload = if sync_payload.exists() {
        let value: Value = read_json_file(&sync_payload)?;
        normalize_entities_payload(value)
    } else {
        json!({ "entities": [] })
    };
    print_json(&payload)?;
    Ok(())
}

fn backfill_items(state_dir: &Path) -> Result<()> {
    ensure_state_layout(state_dir)?;
    let payload_path = payloads_dir(state_dir).join("backfill.jsonl");
    let cursor_path = runtime_dir(state_dir).join("backfill_cursor.json");
    let mut cursor_state: BackfillCursorState = if cursor_path.exists() {
        read_json_file(&cursor_path)?
    } else {
        BackfillCursorState::default()
    };

    let mut chunks: Vec<Value> = Vec::new();
    if payload_path.exists() {
        let file = File::open(&payload_path)
            .with_context(|| format!("failed to open {}", payload_path.display()))?;
        let reader = BufReader::new(file);
        for line in reader.lines() {
            let line = line?;
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let parsed: Value = serde_json::from_str(trimmed)
                .with_context(|| format!("invalid JSON line in {}", payload_path.display()))?;
            chunks.push(parsed);
        }
    }

    let mut payload = if cursor_state.next_line < chunks.len() {
        let chunk = chunks[cursor_state.next_line].clone();
        cursor_state.next_line += 1;
        normalize_entities_payload(chunk)
    } else {
        json!({ "entities": [] })
    };

    let exhausted = cursor_state.next_line >= chunks.len();
    merge_backfill_metadata(&mut payload, &mut cursor_state);
    ensure_backfill_block(&mut payload, exhausted, &cursor_state);
    write_json_file(&cursor_path, &cursor_state)?;
    print_json(&payload)?;
    Ok(())
}

fn listen_aps(state_dir: &Path) -> Result<()> {
    ensure_state_layout(state_dir)?;
    let aps_path = payloads_dir(state_dir).join("aps.ndjson");
    let mut offset = 0usize;

    loop {
        if aps_path.exists() {
            let file = File::open(&aps_path)
                .with_context(|| format!("failed to open {}", aps_path.display()))?;
            let reader = BufReader::new(file);
            let mut lines = 0usize;

            for (idx, line) in reader.lines().enumerate() {
                lines = idx + 1;
                if idx < offset {
                    continue;
                }
                let line = line?;
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    continue;
                }
                if let Ok(mut value) = serde_json::from_str::<Value>(trimmed) {
                    if !value.is_object() {
                        value = json!({ "type": "aps.raw", "payload": value });
                    }
                    print_json_line(&value)?;
                }
            }

            if lines < offset {
                offset = 0;
            } else {
                offset = lines;
            }
        }

        thread::sleep(Duration::from_secs(1));
    }
}

fn merge_backfill_metadata(payload: &mut Value, state: &mut BackfillCursorState) {
    let Some(backfill_obj) = payload
        .as_object()
        .and_then(|obj| obj.get("backfill"))
        .and_then(Value::as_object)
    else {
        return;
    };

    if let Some(checkpoints) = backfill_obj.get("checkpoints").and_then(Value::as_object) {
        for (entity_id, cursor) in checkpoints {
            state
                .checkpoints
                .insert(entity_id.clone(), cursor.as_str().map(ToOwned::to_owned));
        }
    }

    if let Some(completed_ids) = backfill_obj.get("completed_ids").and_then(Value::as_array) {
        for entity_id in completed_ids {
            if let Some(entity_id_str) = entity_id.as_str() {
                state.completed_ids.insert(entity_id_str.to_string());
            }
        }
    }
}

fn ensure_backfill_block(payload: &mut Value, done: bool, state: &BackfillCursorState) {
    let checkpoints = Value::Object(
        state
            .checkpoints
            .iter()
            .map(|(entity_id, cursor)| {
                (
                    entity_id.clone(),
                    cursor
                        .as_ref()
                        .map_or(Value::Null, |v| Value::String(v.clone())),
                )
            })
            .collect(),
    );
    let completed_ids = Value::Array(
        state
            .completed_ids
            .iter()
            .cloned()
            .map(Value::String)
            .collect(),
    );

    let obj = payload.as_object_mut().expect("payload always object");
    obj.insert(
        "backfill".to_string(),
        json!({
            "done": done,
            "checkpoints": checkpoints,
            "completed_ids": completed_ids
        }),
    );
}

fn normalize_entities_payload(mut value: Value) -> Value {
    if !value.is_object() {
        return json!({ "entities": [] });
    }
    let Some(obj) = value.as_object_mut() else {
        return json!({ "entities": [] });
    };
    if !obj.get("entities").is_some_and(Value::is_array) {
        obj.insert("entities".to_string(), Value::Array(vec![]));
    }
    value
}

fn ensure_state_layout(state_dir: &Path) -> Result<()> {
    fs::create_dir_all(state_dir)
        .with_context(|| format!("failed to create {}", state_dir.display()))?;
    fs::create_dir_all(payloads_dir(state_dir))
        .with_context(|| format!("failed to create {}", payloads_dir(state_dir).display()))?;
    fs::create_dir_all(runtime_dir(state_dir))
        .with_context(|| format!("failed to create {}", runtime_dir(state_dir).display()))?;
    Ok(())
}

fn payloads_dir(state_dir: &Path) -> PathBuf {
    state_dir.join("payloads")
}

fn runtime_dir(state_dir: &Path) -> PathBuf {
    state_dir.join("runtime")
}

fn state_file(state_dir: &Path) -> PathBuf {
    runtime_dir(state_dir).join("bridge_state.json")
}

fn read_json_file<T: DeserializeOwned>(path: &Path) -> Result<T> {
    let bytes = fs::read(path).with_context(|| format!("failed to read {}", path.display()))?;
    let value = serde_json::from_slice(&bytes)
        .with_context(|| format!("invalid JSON in {}", path.display()))?;
    Ok(value)
}

fn write_json_file<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let bytes = serde_json::to_vec_pretty(value)?;
    fs::write(path, bytes).with_context(|| format!("failed to write {}", path.display()))?;
    Ok(())
}

fn print_json(value: &Value) -> Result<()> {
    let stdout = io::stdout();
    let mut lock = stdout.lock();
    serde_json::to_writer(&mut lock, value)?;
    lock.write_all(b"\n")?;
    lock.flush()?;
    Ok(())
}

fn print_json_line(value: &Value) -> Result<()> {
    let stdout = io::stdout();
    let mut lock = stdout.lock();
    serde_json::to_writer(&mut lock, value)?;
    lock.write_all(b"\n")?;
    lock.flush()?;
    Ok(())
}
