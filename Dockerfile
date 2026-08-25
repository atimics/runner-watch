FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY web ./web
RUN pip install .

EXPOSE 8080

FROM base AS test
COPY tests ./tests
RUN pip install "pytest>=8.0" "ruff>=0.9"
RUN pytest -q && ruff check src tests
CMD ["pytest", "-q"]

FROM base AS runtime
CMD ["uvicorn", "runner_web.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
