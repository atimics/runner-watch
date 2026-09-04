ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG MODEL_ID
ARG MODEL_REVISION
ARG SOURCE_REVISION
ARG IMAGE_REPOSITORY

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/sec-qwen/huggingface \
    UV_PROJECT_ENVIRONMENT=/opt/sec-qwen/.venv \
    PATH=/opt/sec-qwen/.venv/bin:${PATH} \
    RUNNER_WATCH_REVISION=${SOURCE_REVISION} \
    OCI_IMAGE_REPOSITORY=${IMAGE_REPOSITORY}

WORKDIR /opt/sec-qwen
COPY pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir --break-system-packages uv==0.12.3 \
    && uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
RUN test -n "${MODEL_ID}"
RUN test -n "${MODEL_REVISION}"
RUN test -n "${SOURCE_REVISION}"
RUN test -n "${IMAGE_REPOSITORY}"
RUN python -c 'import sys; from huggingface_hub import snapshot_download; snapshot_download(repo_id=sys.argv[1], revision=sys.argv[2])' "${MODEL_ID}" "${MODEL_REVISION}"

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

ENTRYPOINT ["sec-qwen"]
