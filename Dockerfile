FROM rust:1.88-slim-bookworm AS integer-ranker

WORKDIR /ranker
COPY rust/stonks-ranker/Cargo.toml rust/stonks-ranker/Cargo.lock ./
COPY rust/stonks-ranker/src ./src
RUN cargo test --locked && cargo build --locked --release

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY --from=integer-ranker /ranker/target/release/stonks-integer-ranker \
    /usr/local/bin/stonks-integer-ranker

RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY web ./web
RUN pip install .

ENV STONKS_INTEGER_RANKER_BIN="/usr/local/bin/stonks-integer-ranker"

EXPOSE 8080

FROM base AS test
COPY tests ./tests
RUN pip install "pytest>=8.0" "ruff>=0.9"
RUN pytest -q && ruff check src tests
CMD ["pytest", "-q"]

FROM base AS runtime
CMD ["uvicorn", "runner_web.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
