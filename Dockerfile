# Alternate deployment path. The primary route on the GPD Pocket is the systemd
# unit in deploy/, which runs the app in place; this exists for portability.
#
#   docker build -t aeroq .
#   docker run -p 8000:8000 -v aeroq-data:/app/backend/data --env-file .env aeroq

# --- Build the frontend -----------------------------------------------------
FROM node:22-slim AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- Runtime ----------------------------------------------------------------
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/app/backend/data/aeroq.db

WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
COPY --from=frontend /build/dist ./frontend/dist

RUN useradd --create-home --uid 10001 aeroq \
    && mkdir -p /app/backend/data \
    && chown -R aeroq:aeroq /app
USER aeroq

WORKDIR /app/backend
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"

# One worker: the per-airport locks that collapse concurrent requests into a
# single API call are in-process, so a second worker could double-spend budget.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
