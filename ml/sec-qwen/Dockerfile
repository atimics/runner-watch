ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG MODEL_ID
ARG MODEL_REVISION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/sec-qwen/huggingface \
    UV_PROJECT_ENVIRONMENT=/opt/sec-qwen/.venv \
    PATH=/opt/sec-qwen/.venv/bin:${PATH}

WORKDIR /opt/sec-qwen
COPY pyproject.toml uv.lock ./
RUN python -m pip install --no-cache-dir uv==0.12.3 \
    && uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
RUN test -n "${MODEL_ID}"
RUN test -n "${MODEL_REVISION}"
RUN python -c 'import os; from huggingface_hub import snapshot_download; snapshot_download(repo_id=os.environ["MODEL_ID"], revision=os.environ["MODEL_REVISION"])'

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

ENTRYPOINT ["sec-qwen"]
