FROM --platform=linux/amd64 node:22.16.0-bookworm-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Official Feishu/DingTalk CLIs. Their installers download the platform binary with curl and unpack a bundled archive
# with unzip (both absent from slim images), and the copies installed on a developer machine are for another OS,
# so they are always fetched here for linux/amd64.
FROM --platform=linux/amd64 node:22.16.0-bookworm-slim AS cli
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip && rm -rf /var/lib/apt/lists/*
RUN npm install --no-audit --no-fund --prefix /opt/platform-cli @larksuite/cli@1.0.97 dingtalk-workspace-cli@1.0.62 \
    && /opt/platform-cli/node_modules/@larksuite/cli/bin/lark-cli --version \
    && /opt/platform-cli/node_modules/dingtalk-workspace-cli/vendor/dws --version

FROM --platform=linux/amd64 python:3.12.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-im.txt && useradd --uid 10001 --create-home app
COPY backend/app ./app
COPY --from=web /web/dist ./static
# Both are single native binaries; no Node runtime is needed to run them.
COPY --from=cli /opt/platform-cli/node_modules/@larksuite/cli/bin/lark-cli /opt/platform-cli/lark-cli
COPY --from=cli /opt/platform-cli/node_modules/dingtalk-workspace-cli/vendor/dws /opt/platform-cli/dws
# Private attachment storage shared by the API and the attachment worker through one named volume.
RUN mkdir -p /data/attachments && chown 10001:10001 /data/attachments && chmod 700 /data/attachments
ENV STATIC_DIR=/app/static ATTACHMENT_ROOT=/data/attachments \
    PLATFORM_LARK_CLI=/opt/platform-cli/lark-cli PLATFORM_DWS_CLI=/opt/platform-cli/dws
USER 10001
EXPOSE 18200
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18200", "--workers", "1"]
