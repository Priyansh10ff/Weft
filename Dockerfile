# Weft: one image with the API, the website and the background workers.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libgomp1: faster-whisper (CTranslate2). curl: the health check.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --create-home --uid 10001 weft \
 && mkdir -p /data \
 && chown weft:weft /data
USER weft

# Bake the MiniLM embedding model (about 80 MB) into the image so the first
# start needs no download. A build without network access still succeeds and
# the model is fetched on first use instead.
RUN python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction as E; E()(['warm up'])" \
 || echo "Embedding model not baked in; it will download on first start."

COPY --chown=weft:weft . .

ENV MEDIA_STORAGE_ROOT=/data \
    KNOWLEDGE_DB_PATH=/data/knowledge.db \
    CHROMA_PATH=/data/chroma \
    PORT=8000
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# One worker: background jobs and the SQLite connection live in this process.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --no-access-log"]
