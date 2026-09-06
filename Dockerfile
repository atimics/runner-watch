FROM ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc AS uv

FROM node:24-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS desktop-renderer

RUN npm install --global pnpm@10.19.0
WORKDIR /desktop
COPY desktop/package.json desktop/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY desktop ./
RUN pnpm build

FROM rust:1.88-slim-bookworm@sha256:38bc5a86d998772d4aec2348656ed21438d20fcdce2795b56ca434cf21430d89 AS integer-ranker

WORKDIR /ranker
COPY rust/stonks-ranker/Cargo.toml rust/stonks-ranker/Cargo.lock ./
COPY rust/stonks-ranker/src ./src
RUN cargo test --locked && cargo build --locked --release

FROM python:3.13-slim@sha256:7ce4b6dfe35e55397b7cda544f8a13f191b7ae28dc5aad71fe664dbc9bc2623f AS base

ARG APP_BUILD_SHA=dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_BUILD_SHA=${APP_BUILD_SHA}

WORKDIR /app

COPY --from=uv /uv /bin/uv
COPY --from=integer-ranker /ranker/target/release/stonks-integer-ranker \
    /usr/local/bin/stonks-integer-ranker

RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system runner \
    && useradd --system --gid runner --home-dir /app --no-create-home runner

COPY pyproject.toml uv.lock LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY web ./web
COPY --from=desktop-renderer /desktop/dist/renderer ./desktop/dist/renderer
RUN uv sync --locked --no-dev --no-editable
RUN chown -R runner:runner /app

ENV PATH="/app/.venv/bin:$PATH" \
    STONKS_INTEGER_RANKER_BIN="/usr/local/bin/stonks-integer-ranker"

EXPOSE 8080
USER runner

FROM base AS test
COPY desktop/src-tauri/Cargo.toml desktop/src-tauri/Cargo.lock ./desktop/src-tauri/
COPY desktop/src-tauri/vendor ./desktop/src-tauri/vendor
COPY desktop/src-tauri/patches ./desktop/src-tauri/patches
COPY scripts ./scripts
COPY tests ./tests
COPY cloudflare-router ./cloudflare-router
COPY ml/sec-qwen/src ./ml/sec-qwen/src
COPY .github/workflows/fly.yml .github/workflows/uptime.yml ./.github/workflows/
COPY app.py compose.local.yml fly.toml Dockerfile ./
RUN uv sync --locked --extra dev --no-editable
RUN uv run --no-sync pytest -q && uv run --no-sync ruff check src tests ml/sec-qwen/src
CMD ["uv", "run", "--no-sync", "pytest", "-q"]

FROM base AS runtime
CMD ["uvicorn", "runner_web.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers", "--no-access-log"]
