FROM python:3.12.11-slim-bookworm

ARG UV_VERSION=0.8.17

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
      ghostscript \
      libmagic1 \
      pngquant \
      poppler-utils \
      qpdf \
      tesseract-ocr \
      tesseract-ocr-eng \
      unpaper \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

ENV LEGAL_RAG_DATA_DIR=/data \
    LEGAL_RAG_MODEL_CACHE_DIR=/opt/models
RUN python -m lawyer_rag.model_setup && rm -rf /tmp/build-data

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data /opt/models \
    && chown -R app:app /app /data /opt/models

USER app
EXPOSE 8000
CMD ["uvicorn", "lawyer_rag.app:app", "--host", "0.0.0.0", "--port", "8000"]
