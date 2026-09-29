# Two-stage build: React UI (Node) -> Python runtime that serves UI + API on :8000.
# Works on x86_64 and on Oracle's Ampere (arm64) shapes.
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY agent agent
COPY api api
COPY demo demo
COPY eval eval
COPY interface interface
COPY dataset dataset
COPY --from=web /web/dist web/dist
RUN mkdir -p runtime data demo/out eval/out \
    && useradd --create-home --uid 1000 app && chown -R app:app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "75"]
