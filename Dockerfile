FROM --platform=linux/amd64 node:22.16.0-bookworm-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM --platform=linux/amd64 python:3.12.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-im.txt && useradd --uid 10001 --create-home app
COPY backend/app ./app
COPY --from=web /web/dist ./static
ENV STATIC_DIR=/app/static
USER 10001
EXPOSE 18200
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18200", "--workers", "1"]
