FROM node:22-bookworm-slim AS history-builder
WORKDIR /build
COPY vendor/dukascopy-node/package.json vendor/dukascopy-node/pnpm-lock.yaml vendor/dukascopy-node/pnpm-workspace.yaml ./
COPY vendor/dukascopy-node/tsconfig.json ./
COPY vendor/dukascopy-node/src ./src
RUN npm install --global pnpm@10.12.4 && pnpm install --frozen-lockfile && pnpm build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MPLBACKEND=Agg MALLOC_ARENA_MAX=2
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# The Python provider invokes this compiled Node engine for historical OHLCV data.
COPY --from=history-builder /build/dist /app/vendor/dukascopy-node/dist
COPY --from=history-builder /build/node_modules /app/vendor/dukascopy-node/node_modules
RUN chmod +x scripts/entrypoint.sh
ENTRYPOINT ["scripts/entrypoint.sh"]
CMD ["python", "-m", "goldbot", "run"]
