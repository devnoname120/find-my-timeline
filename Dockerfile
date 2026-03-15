FROM rust:1.88-slim AS rust-bridge-builder

WORKDIR /build/rustpush_bridge
COPY rustpush_bridge/Cargo.toml rustpush_bridge/Cargo.lock ./
COPY rustpush_bridge/src/ ./src/
RUN cargo build --release

FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for better caching
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY templates/ ./templates/

RUN pip install --no-cache-dir -e .

# Bundle rustpush bridge binary so rustpush backend works in Docker.
RUN mkdir -p /app/rustpush_bridge/target/release
COPY --from=rust-bridge-builder /build/rustpush_bridge/target/release/find-my-rustpush-bridge /app/rustpush_bridge/target/release/find-my-rustpush-bridge
RUN ln -sf /app/rustpush_bridge/target/release/find-my-rustpush-bridge /usr/local/bin/find-my-rustpush-bridge

# Session cookies and database will be mounted at runtime
VOLUME ["/root/.find-my-timeline", "/app/data"]

EXPOSE 5000

CMD ["find-my-timeline", "start"]
