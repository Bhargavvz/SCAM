# ---- UI
FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# ---- API + UI
FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ backend/
COPY --from=web /web/dist frontend/dist
ENV SOURCE_DB=/source/inventory_supply_chain_v1_1_ext.sqlite DATASET_DIR=/dataset MERIDIAN_DB=/app/data/meridian.sqlite MANIFEST_PATH=/app/data/manifest.json
WORKDIR /app/backend
EXPOSE 8100
CMD ["sh", "-c", "[ -f $MERIDIAN_DB ] || python -m app.build_db; exec uvicorn app.main:app --host 0.0.0.0 --port 8100"]
