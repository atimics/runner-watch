FROM ghcr.io/astral-sh/uv:0.12.3 AS uv

FROM node:24-bookworm-slim AS desktop-renderer

RUN npm install --global pnpm@10.19.0
WORKDIR /desktop
COPY desktop/package.json desktop/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY desktop ./
RUN pnpm build

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

COPY --from=uv /uv /bin/uv
COPY --from=integer-ranker /ranker/target/release/stonks-integer-ranker \
    /usr/local/bin/stonks-integer-ranker

RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY web ./web
COPY --from=desktop-renderer /desktop/dist/renderer ./desktop/dist/renderer
RUN uv sync --locked --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH" \
    STONKS_INTEGER_RANKER_BIN="/usr/local/bin/stonks-integer-ranker"

EXPOSE 8080

FROM base AS test
COPY scripts ./scripts
COPY tests ./tests
COPY docs/privacy-operations.md ./docs/privacy-operations.md
COPY fly.toml Dockerfile ./
RUN uv sync --locked --extra dev --no-editable
RUN uv run --no-sync pytest -q && uv run --no-sync ruff check src tests
CMD ["uv", "run", "--no-sync", "pytest", "-q"]

FROM base AS runtime
CMD ["uvicorn", "runner_web.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers", "--no-access-log"]
